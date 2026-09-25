# simulator/tests/test_funded_economics.py
"""
BROKER-ECONOMICS-04B (FASE B) — dedicated adversarial suite.

Covers the full §S matrix certified in FASE A: recognition only at
FundedPayoutRequest COMPLETED for both FUNDED_SIM and FUNDED_INTERNAL,
the double-counting invariant against REV_COUNTERPARTY_PNL, DB-enforced
idempotency, IB/Wallet/Treasury isolation, broker_pnl/
broker_economics_summary integration, and forward-only behavior.
"""
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase

from simulator.broker_economic_adjustment import create_broker_economic_adjustment
from simulator.broker_economics_summary import broker_economics_summary
from simulator.funded_economics import (
    DuplicateFundedProfitShareRevenue, record_funded_broker_cut_revenue,
)
from simulator.funded_payouts import (
    InsufficientFundedBalance, approve_internal_payout, approve_sim_payout,
    handle_internal_payout_webhook,
)
from simulator.models import (
    BrokerLedger, FundedConfig, FundedPayoutRequest, IBCommissionObligation,
    TradingAccount, TreasuryOperationRequest, Wallet, WalletTransaction,
    WithdrawalRequest,
)
from simulator.tests.factories import (
    make_account, make_challenge_enrollment, make_funded_config, make_user, make_wallet,
)

_seq = 0


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_funded_account(user, *, initial=Decimal("100000.00"), cycle_profit=Decimal("10000.00")):
    """A FUNDED TradingAccount that has already 'traded up' by cycle_profit
    over its initial_balance baseline."""
    account = make_account(user=user, account_type="FUNDED", tier="10K", balance=initial)
    new_balance = initial + cycle_profit
    TradingAccount.objects.filter(pk=account.pk).update(balance=new_balance, equity=new_balance)
    account.refresh_from_db()
    return account


def _make_fpr(
    user, *, funded_type=FundedConfig.FUNDED_SIM, cycle_profit=Decimal("10000.00"),
    split_pct=Decimal("80.00"), initial=Decimal("100000.00"),
    status=FundedPayoutRequest.ST_PENDING, crypto_currency="", wallet_address="",
):
    global _seq
    _seq += 1
    account = _make_funded_account(user, initial=initial, cycle_profit=cycle_profit)
    enrollment = make_challenge_enrollment(user=user)
    fc = make_funded_config(enrollment=enrollment, funded_type=funded_type, profit_split_pct=split_pct)
    trader_cut = (cycle_profit * split_pct / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut = cycle_profit - trader_cut
    fpr = FundedPayoutRequest.objects.create(
        enrollment=enrollment, funded_account=account, funded_config=fc, user=user,
        cycle_profit=cycle_profit, trader_cut=trader_cut, broker_cut=broker_cut,
        profit_split_pct=split_pct, balance_snapshot=account.balance,
        initial_balance_snapshot=initial, funded_type=funded_type,
        crypto_currency=crypto_currency, wallet_address=wallet_address, status=status,
    )
    return fpr, account


def _admin():
    return make_user(is_staff=True, is_superuser=True)


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same pattern as the Challenge/Withdrawal writers' ConcurrencyTests."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = ("ok", fn())
                return
            except IntegrityError as exc:
                results[index] = ("integrity_error", exc)
                return
            except DuplicateFundedProfitShareRevenue as exc:
                results[index] = ("duplicate", exc)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("operational_error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


_NP_ESTIMATE = "simulator.nowpayments.estimate_price"
_NP_TOKEN = "simulator.nowpayments._get_jwt_token"
_NP_PAYOUT = "simulator.nowpayments.create_payout_with_token"


def _complete_internal(fpr, admin, *, batch_id="batch-1", payout_id="payout-1"):
    """Drive a FUNDED_INTERNAL FPR from PENDING through Phase 1 (approval)
    and Phase 2 (real webhook confirmation), mocking only the external
    NowPayments HTTP boundary — everything else is the real code path."""
    with patch(_NP_ESTIMATE, return_value=Decimal("0.001")), \
         patch(_NP_TOKEN, return_value="fake-jwt"), \
         patch(_NP_PAYOUT, return_value={
             "id": batch_id, "status": "CREATED",
             "withdrawals": [{"id": payout_id}],
         }):
        approve_internal_payout(fpr, admin, callback_url="http://test/cb")
    fpr.refresh_from_db()
    wr = fpr.withdrawal_request
    handle_internal_payout_webhook(fpr, wr, WithdrawalRequest.STATUS_COMPLETED, payout_id)
    fpr.refresh_from_db()
    return fpr


# ── 1. Writer core ───────────────────────────────────────────────────────────

class WriterCoreTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.fpr, self.account = _make_fpr(self.user)

    def test_amount_equals_broker_cut(self):
        row = record_funded_broker_cut_revenue(self.fpr)
        self.assertEqual(row.amount, Decimal("2000.00"))

    def test_revenue_type_is_funded_profit_share(self):
        row = record_funded_broker_cut_revenue(self.fpr)
        self.assertEqual(row.revenue_type, BrokerLedger.REV_FUNDED_PROFIT_SHARE)

    def test_source_funded_payout_and_account_set(self):
        row = record_funded_broker_cut_revenue(self.fpr)
        self.assertEqual(row.source_funded_payout_id, self.fpr.pk)
        self.assertEqual(row.source_account_id, self.account.pk)

    def test_meta_carries_funded_type_and_reference(self):
        row = record_funded_broker_cut_revenue(self.fpr)
        self.assertEqual(row.meta["funded_payout_request_id"], self.fpr.pk)
        self.assertEqual(row.meta["funded_type"], FundedConfig.FUNDED_SIM)
        self.assertEqual(row.meta["cycle_profit"], "10000.00")
        self.assertEqual(row.meta["trader_cut"], "8000.00")

    def test_unsaved_fpr_raises_value_error(self):
        unsaved = FundedPayoutRequest(user=self.user)
        with self.assertRaises(ValueError):
            record_funded_broker_cut_revenue(unsaved)

    def test_zero_broker_cut_is_noop(self):
        fpr, _ = _make_fpr(self.user, split_pct=Decimal("100.00"))  # trader gets 100%, broker_cut=0
        result = record_funded_broker_cut_revenue(fpr)
        self.assertIsNone(result)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)

    def test_second_call_raises_duplicate(self):
        record_funded_broker_cut_revenue(self.fpr)
        with self.assertRaises(DuplicateFundedProfitShareRevenue):
            record_funded_broker_cut_revenue(self.fpr)

    def test_second_call_does_not_create_second_row(self):
        record_funded_broker_cut_revenue(self.fpr)
        try:
            record_funded_broker_cut_revenue(self.fpr)
        except DuplicateFundedProfitShareRevenue:
            pass
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=self.fpr).count(), 1)

    def test_direct_duplicate_ledger_write_raises_integrity_error(self):
        record_funded_broker_cut_revenue(self.fpr)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerLedger.objects.create(
                    revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE,
                    amount=self.fpr.broker_cut, source_funded_payout=self.fpr,
                )


# ── 2. FUNDED_SIM completion — real service call ────────────────────────────

class FundedSimCompletionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = _admin()

    def test_completed_sim_payout_books_exactly_one_recognition(self):
        fpr, account = _make_fpr(self.user, funded_type=FundedConfig.FUNDED_SIM)
        approve_sim_payout(fpr, self.admin)
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("2000.00"))

    def test_meta_records_funded_sim_type(self):
        fpr, _ = _make_fpr(self.user, funded_type=FundedConfig.FUNDED_SIM)
        approve_sim_payout(fpr, self.admin)
        row = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE)
        self.assertEqual(row.meta["funded_type"], FundedConfig.FUNDED_SIM)

    def test_recognized_at_the_same_moment_initial_balance_resets(self):
        fpr, account = _make_fpr(self.user, funded_type=FundedConfig.FUNDED_SIM)
        approve_sim_payout(fpr, self.admin)
        account.refresh_from_db()
        # initial_balance now == balance - trader_cut == original + broker_cut
        self.assertEqual(account.initial_balance, Decimal("102000.00"))
        self.assertTrue(BrokerLedger.objects.filter(source_funded_payout=fpr).exists())


# ── 3. FUNDED_INTERNAL completion — real service + real webhook path ───────

class FundedInternalCompletionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = _admin()

    def test_webhook_completed_books_exactly_one_recognition(self):
        fpr, account = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfunded000000000000000000000000000",
        )
        _complete_internal(fpr, self.admin)
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("2000.00"))

    def test_meta_records_funded_internal_type(self):
        fpr, _ = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfunded111111111111111111111111111",
        )
        _complete_internal(fpr, self.admin)
        row = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE)
        self.assertEqual(row.meta["funded_type"], FundedConfig.FUNDED_INTERNAL)

    def test_approval_phase1_alone_creates_zero_recognition(self):
        """Phase 1 (APPROVED) is fully reversible — recognition must wait
        for the webhook-confirmed COMPLETED."""
        fpr, _ = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfunded222222222222222222222222222",
        )
        with patch(_NP_ESTIMATE, return_value=Decimal("0.001")), \
             patch(_NP_TOKEN, return_value="fake-jwt"), \
             patch(_NP_PAYOUT, return_value={"id": "b1", "status": "CREATED", "withdrawals": [{"id": "p1"}]}):
            approve_internal_payout(fpr, self.admin, callback_url="http://test/cb")
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)


# ── 4. Non-recognition states ────────────────────────────────────────────────

class NonRecognitionStateTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = _admin()

    def test_pending_zero_revenue(self):
        fpr, _ = _make_fpr(self.user, status=FundedPayoutRequest.ST_PENDING)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)

    def test_approved_zero_revenue(self):
        fpr, _ = _make_fpr(self.user, status=FundedPayoutRequest.ST_APPROVED)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)

    def test_rejected_zero_revenue(self):
        fpr, _ = _make_fpr(self.user, status=FundedPayoutRequest.ST_REJECTED)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)

    def test_cancelled_zero_revenue(self):
        fpr, _ = _make_fpr(self.user, status=FundedPayoutRequest.ST_CANCELLED)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)

    def test_failed_presend_zero_revenue_trader_cut_restored(self):
        """estimate_price fails before any NowPayments call — reversed."""
        fpr, account = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfundedfail0000000000000000000000000",
        )
        with patch(_NP_ESTIMATE, side_effect=Exception("estimate down")):
            with self.assertRaises(Exception):
                approve_internal_payout(fpr, self.admin, callback_url="http://test/cb")
        fpr.refresh_from_db()
        self.assertEqual(fpr.status, FundedPayoutRequest.ST_FAILED)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("110000.00"))  # trader_cut restored

    def test_failed_confirmed_webhook_zero_revenue(self):
        fpr, account = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfundedfail1111111111111111111111111",
        )
        with patch(_NP_ESTIMATE, return_value=Decimal("0.001")), \
             patch(_NP_TOKEN, return_value="fake-jwt"), \
             patch(_NP_PAYOUT, return_value={"id": "b2", "status": "CREATED", "withdrawals": [{"id": "p2"}]}):
            approve_internal_payout(fpr, self.admin, callback_url="http://test/cb")
        fpr.refresh_from_db()
        wr = fpr.withdrawal_request
        handle_internal_payout_webhook(fpr, wr, WithdrawalRequest.STATUS_FAILED, "p2")
        fpr.refresh_from_db()
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)


# ── 5. Duplicate / retry ─────────────────────────────────────────────────────

class DuplicateCompletionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = _admin()

    def test_duplicate_webhook_completed_exactly_one_recognition(self):
        fpr, _ = _make_fpr(
            self.user, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfundeddup0000000000000000000000000",
        )
        _complete_internal(fpr, self.admin, batch_id="b3", payout_id="p3")
        wr = fpr.withdrawal_request
        handle_internal_payout_webhook(fpr, wr, WithdrawalRequest.STATUS_COMPLETED, "p3")  # replay
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 1)

    def test_double_call_approve_sim_payout_raises_already_processed(self):
        from simulator.funded_payouts import FundedPayoutAlreadyProcessed
        fpr, _ = _make_fpr(self.user, funded_type=FundedConfig.FUNDED_SIM)
        approve_sim_payout(fpr, self.admin)
        with self.assertRaises(FundedPayoutAlreadyProcessed):
            approve_sim_payout(fpr, self.admin)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 1)


# ── 6. Concurrency — real DB race ───────────────────────────────────────────

class ConcurrencyTests(TransactionTestCase):
    def test_two_threads_same_fpr_exactly_one_succeeds(self):
        user = make_user()
        fpr, _ = _make_fpr(user)
        fpr_id = fpr.pk

        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            f = FundedPayoutRequest.objects.get(pk=fpr_id)
            return record_funded_broker_cut_revenue(f)

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_attempt, barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        outcomes = [r[0] if r else None for r in results]
        self.assertEqual(outcomes.count("ok"), 1, f"expected exactly 1 success, got {results}")
        self.assertIn("duplicate", outcomes, f"expected the loser to see a duplicate, got {results}")
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout_id=fpr_id).count(), 1)


# ── 7. Double-counting invariant — the central FASE A proof, as a test ─────

class DoubleCountingInvariantTests(TestCase):
    def test_counterparty_pnl_plus_broker_cut_equals_negative_trader_cut(self):
        """cycle_profit=10000, trader_cut=8000 (80%), broker_cut=2000.
        REV_COUNTERPARTY_PNL=-10000 (booked here to simulate the already-
        certified trading-side writer) + REV_FUNDED_PROFIT_SHARE=+2000
        (this module) must net to exactly -8000 — the broker's true cash
        outflow for the cycle."""
        user = make_user()
        fpr, account = _make_fpr(user, cycle_profit=Decimal("10000.00"), split_pct=Decimal("80.00"))

        # Simulate the ALREADY-CERTIFIED, pre-existing counterparty write
        # this same trading activity produces (broker_ledger.py, untouched
        # by this block) — booked here directly to keep this test isolated
        # from BOOK-02's own writer.
        BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
            amount=-fpr.cycle_profit, source_account=account,
        )

        record_funded_broker_cut_revenue(fpr)

        counterparty = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, source_account=account,
        ).aggregate(t=Sum("amount"))["t"]
        profit_share = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE, source_funded_payout=fpr,
        ).aggregate(t=Sum("amount"))["t"]

        self.assertEqual(counterparty, Decimal("-10000.00"))
        self.assertEqual(profit_share, Decimal("2000.00"))
        self.assertEqual(counterparty + profit_share, -fpr.trader_cut)
        self.assertEqual(counterparty + profit_share, Decimal("-8000.00"))

    def test_original_counterparty_row_never_mutated(self):
        user = make_user()
        fpr, account = _make_fpr(user)
        cp = BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
            amount=-fpr.cycle_profit, source_account=account,
        )
        original_amount = cp.amount
        record_funded_broker_cut_revenue(fpr)
        cp.refresh_from_db()
        self.assertEqual(cp.amount, original_amount)
        self.assertEqual(cp.revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)


# ── 8. Snapshot integrity ────────────────────────────────────────────────────

class SnapshotIntegrityTests(TestCase):
    def test_config_change_after_request_does_not_alter_booked_amount(self):
        user = make_user()
        fpr, _ = _make_fpr(user, cycle_profit=Decimal("10000.00"), split_pct=Decimal("80.00"))
        original_broker_cut = fpr.broker_cut

        # Change the live config AFTER the request was created/snapshotted.
        fpr.funded_config.profit_split_pct = Decimal("50.00")
        fpr.funded_config.save(update_fields=["profit_split_pct"])

        fpr.refresh_from_db()
        self.assertEqual(fpr.broker_cut, original_broker_cut)  # snapshot untouched
        row = record_funded_broker_cut_revenue(fpr)
        self.assertEqual(row.amount, original_broker_cut)  # never recomputed from live config


# ── 9. IB isolation ──────────────────────────────────────────────────────────

class IBIsolationTests(TestCase):
    def test_writer_alone_creates_zero_ib_obligations(self):
        user = make_user()
        fpr, _ = _make_fpr(user)
        before = IBCommissionObligation.objects.count()
        record_funded_broker_cut_revenue(fpr)
        self.assertEqual(IBCommissionObligation.objects.count(), before)


# ── 10. Wallet / Treasury isolation ─────────────────────────────────────────

class WalletTreasuryIsolationTests(TestCase):
    def test_writer_alone_creates_zero_wallet_transactions(self):
        user = make_user()
        wallet = make_wallet(user, initial_balance=Decimal("500.00"))
        fpr, _ = _make_fpr(user)
        before = WalletTransaction.objects.filter(wallet=wallet).count()
        record_funded_broker_cut_revenue(fpr)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), before)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("500.00"))

    def test_writer_alone_creates_zero_treasury_requests(self):
        user = make_user()
        fpr, _ = _make_fpr(user)
        before = TreasuryOperationRequest.objects.count()
        record_funded_broker_cut_revenue(fpr)
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)

    def test_full_sim_completion_debits_only_trader_cut_from_wallet(self):
        user = make_user()
        wallet = make_wallet(user, initial_balance=Decimal("0.00"))
        admin = _admin()
        fpr, _ = _make_fpr(user, cycle_profit=Decimal("10000.00"), split_pct=Decimal("80.00"))
        approve_sim_payout(fpr, admin)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("8000.00"))  # trader_cut only


# ── 11. broker_pnl.py integration ───────────────────────────────────────────

class BrokerPnlIntegrationTests(TestCase):
    def test_broker_pnl_includes_funded_profit_share_exactly_once(self):
        from simulator import broker_pnl
        user = make_user()
        fpr, _ = _make_fpr(user, cycle_profit=Decimal("500.00"), split_pct=Decimal("80.00"))
        record_funded_broker_cut_revenue(fpr)
        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.funded_profit_share, Decimal("100.00"))
        self.assertEqual(breakdown.fee_revenue, Decimal("100.00"))
        self.assertEqual(breakdown.broker_net_pnl, Decimal("100.00"))

    def test_counterparty_pnl_field_unaffected(self):
        from simulator import broker_pnl
        user = make_user()
        fpr, _ = _make_fpr(user)
        record_funded_broker_cut_revenue(fpr)
        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.counterparty_pnl, Decimal("0.00"))


# ── 12. broker_economics_summary.py integration ─────────────────────────────

class BrokerEconomicsSummaryIntegrationTests(TestCase):
    def test_summary_exposes_funded_profit_share_revenue(self):
        user = make_user()
        fpr, _ = _make_fpr(user, cycle_profit=Decimal("1000.00"), split_pct=Decimal("80.00"))
        record_funded_broker_cut_revenue(fpr)
        s = broker_economics_summary()
        self.assertEqual(s.funded_profit_share_revenue, Decimal("200.00"))
        self.assertEqual(s.gross_broker_economic_result, Decimal("200.00"))

    def test_capital_flows_unchanged(self):
        user = make_user()
        fpr, _ = _make_fpr(user)
        record_funded_broker_cut_revenue(fpr)
        s = broker_economics_summary()
        self.assertEqual(s.capital_flows.deposits_total, Decimal("0.00"))
        self.assertEqual(s.capital_flows.withdrawals_total, Decimal("0.00"))

    def test_retained_status_still_partial(self):
        user = make_user()
        fpr, _ = _make_fpr(user)
        record_funded_broker_cut_revenue(fpr)
        s = broker_economics_summary()
        self.assertEqual(s.retained.status, "PARTIAL")

    def test_coverage_note_confirms_not_a_duplicate_of_counterparty_pnl(self):
        s = broker_economics_summary()
        note = s.funded_profit_share_coverage.note
        self.assertIn("SEPARATE economic fact", note)
        self.assertIn("counterparty_pnl", note)


# ── 13. Forward-only ─────────────────────────────────────────────────────────

class ForwardOnlyTests(TestCase):
    def test_legacy_completed_payout_never_retroactively_recognized(self):
        """A FundedPayoutRequest already COMPLETED (simulating one that
        predates this writer) must never acquire a recognition row unless
        the writer is explicitly, newly invoked on it — there is no sweep
        that would do this automatically."""
        user = make_user()
        fpr, _ = _make_fpr(user, status=FundedPayoutRequest.ST_COMPLETED)
        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)


# ── 14. Rollback ─────────────────────────────────────────────────────────────

class RollbackTests(TestCase):
    def test_rollback_after_writer_leaves_no_orphaned_row(self):
        user = make_user()
        fpr, _ = _make_fpr(user)

        class _BoomAfterWriter(Exception):
            pass

        with self.assertRaises(_BoomAfterWriter):
            with transaction.atomic():
                record_funded_broker_cut_revenue(fpr)
                raise _BoomAfterWriter("later step in the same transaction failed")

        self.assertEqual(BrokerLedger.objects.filter(source_funded_payout=fpr).count(), 0)


# ── 15. Reversal compatibility ───────────────────────────────────────────────

class ReversalCompatibilityTests(TestCase):
    def test_adjustment_against_funded_revenue_never_mutates_original(self):
        user = make_user()
        fpr, _ = _make_fpr(user, cycle_profit=Decimal("10000.00"), split_pct=Decimal("80.00"))
        original = record_funded_broker_cut_revenue(fpr)
        original_amount = original.amount

        create_broker_economic_adjustment(
            amount=-Decimal("2000.00"), reason="Test correction for funded profit share",
            actor=make_user(), idempotency_key="test-funded-correction-001",
        )

        original.refresh_from_db()
        self.assertEqual(original.amount, original_amount)
        self.assertEqual(original.revenue_type, BrokerLedger.REV_FUNDED_PROFIT_SHARE)
        self.assertEqual(
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1,
        )


# ── 16. FUNDED_SIM vs FUNDED_INTERNAL stay distinguishable ─────────────────

class FundedTypeDistinguishabilityTests(TestCase):
    def test_sim_and_internal_rows_carry_distinct_funded_type_in_meta(self):
        user_sim = make_user()
        user_internal = make_user()
        admin = _admin()

        fpr_sim, _ = _make_fpr(user_sim, funded_type=FundedConfig.FUNDED_SIM)
        approve_sim_payout(fpr_sim, admin)

        fpr_internal, _ = _make_fpr(
            user_internal, funded_type=FundedConfig.FUNDED_INTERNAL,
            crypto_currency="btc", wallet_address="bc1qtestfundeddistinct00000000000000000000",
        )
        _complete_internal(fpr_internal, admin, batch_id="b4", payout_id="p4")

        row_sim = BrokerLedger.objects.get(source_funded_payout=fpr_sim)
        row_internal = BrokerLedger.objects.get(source_funded_payout=fpr_internal)
        self.assertEqual(row_sim.meta["funded_type"], FundedConfig.FUNDED_SIM)
        self.assertEqual(row_internal.meta["funded_type"], FundedConfig.FUNDED_INTERNAL)
        self.assertNotEqual(row_sim.meta["funded_type"], row_internal.meta["funded_type"])
