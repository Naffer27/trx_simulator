# simulator/tests/test_withdrawal_economics.py
"""
WITHDRAWAL-ECONOMICS-01 (FASE B) — dedicated adversarial suite.

Covers the full §T matrix certified in FASE A plus the three explicit
legacy-compatibility scenarios from the FASE B authorization: a
WithdrawalRequest created before this block existed (fee_rate/
fee_amount/net_amount all None) must NEVER acquire a retroactive fee,
must NEVER produce a retroactive REV_WITHDRAW_FEE row, and must NEVER
have its payout amount silently reduced.
"""
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from simulator.broker_economic_adjustment import create_broker_economic_adjustment
from simulator.broker_economics_summary import broker_economics_summary
from simulator.models import (
    BrokerLedger, IBCommissionObligation, PayoutAttempt, TreasuryOperationRequest,
    Wallet, WalletTransaction, WithdrawalFeeConfig, WithdrawalRequest,
)
from simulator.payout_orchestrator import apply_provider_webhook_event
from simulator.payout_providers import ProviderPayoutEvent
from simulator.tests.factories import (
    make_kyc_approved, make_totp_device, make_user, make_verified_withdrawal_wallet,
    make_wallet,
)
from simulator.tests.withdrawal_flow_helpers import (
    PATCH_EMAIL, PATCH_RATELIMIT, PATCH_TOTP, full_withdraw_flow,
)
from simulator.withdrawal_economics import (
    DuplicateWithdrawalFeeRevenue, calculate_withdrawal_fee, record_withdrawal_fee_revenue,
)

_seq = 0


# ── helpers ──────────────────────────────────────────────────────────────────

def _next_addr():
    global _seq
    _seq += 1
    return f"bc1qtestwitheco{_seq:026d}"


def _make_wr(user, amount="200.00", *, fee_rate=None, fee_amount=None, net_amount=None,
             status=WithdrawalRequest.STATUS_PROCESSING):
    """Direct construction (bypasses the OTP view) — same pattern as
    test_fix02a2_webhook.py's own _make_wr, extended with the new fee
    snapshot fields. fee_rate/fee_amount/net_amount all default to None
    — i.e. a LEGACY row with no fee policy ever evaluated, unless the
    caller explicitly passes a snapshot (simulating a post-FASE-B row)."""
    from simulator.wallet_ledger import debit_wallet
    wallet, _ = Wallet.objects.get_or_create(user=user)
    debit_tx = debit_wallet(wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="t")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address=_next_addr(), status=status, debit_tx=debit_tx,
        fee_rate=fee_rate, fee_amount=fee_amount, net_amount=net_amount,
    )


def _make_attempt(wr, *, status, attempt_number=1, provider_reference="", provider_batch_id=""):
    return PayoutAttempt.objects.create(
        withdrawal_request=wr, provider="nowpayments", attempt_number=attempt_number,
        idempotency_key=f"key-{wr.pk}-{attempt_number}-{provider_reference or 'x'}",
        requested_amount_usd=wr.net_amount or wr.amount_usd, requested_asset="btc",
        destination_address=wr.wallet_address, status=status,
        submitted_at=timezone.now(), provider_reference=provider_reference,
        provider_batch_id=provider_batch_id,
    )


def _event(*, reference="", batch="", status=PayoutAttempt.STATUS_PROCESSING, raw="ROLLING"):
    return ProviderPayoutEvent(
        provider="nowpayments", provider_reference=reference, provider_batch_id=batch,
        normalized_status=status, raw_status=raw, provider_amount=None,
        occurred_at=timezone.now(),
    )


def _complete(wr, *, ref="ref-complete"):
    """Drive wr (already PROCESSING with a PayoutAttempt) to COMPLETED via
    the real webhook application path — the certified call site."""
    return apply_provider_webhook_event(_event(reference=ref, status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))


def _fail(wr, *, ref="ref-fail"):
    return apply_provider_webhook_event(_event(reference=ref, status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))


def _set_fee_config(percent=None, enabled=True):
    cfg = WithdrawalFeeConfig.get_current()
    if percent is not None:
        cfg.percent = Decimal(str(percent))
    cfg.enabled = enabled
    cfg.save()
    return cfg


def _admin_request(admin_user):
    from django.contrib.messages.storage.cookie import CookieStorage
    request = RequestFactory().post("/admin/")
    request.user = admin_user
    request._messages = CookieStorage(request)
    return request


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same pattern as the Challenge Revenue Writer's ConcurrencyTests —
    real threads, real SQLite locking, retry-on-locked."""
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
            except DuplicateWithdrawalFeeRevenue as exc:
                results[index] = ("duplicate", exc)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("operational_error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


# ── 1. Fee calculation ───────────────────────────────────────────────────────

class FeeCalculationTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)

    def test_1250_at_1_5_percent(self):
        rate, fee, net = calculate_withdrawal_fee(Decimal("1250.00"))
        self.assertEqual(fee, Decimal("18.75"))
        self.assertEqual(net, Decimal("1231.25"))
        self.assertEqual(rate, Decimal("1.50"))

    def test_100_at_1_5_percent(self):
        _, fee, net = calculate_withdrawal_fee(Decimal("100.00"))
        self.assertEqual(fee, Decimal("1.50"))
        self.assertEqual(net, Decimal("98.50"))

    def test_25_at_1_5_percent_half_even_rounding(self):
        """Raw = 0.375 — ROUND_HALF_EVEN rounds the tie to 0.38 (even)."""
        _, fee, net = calculate_withdrawal_fee(Decimal("25.00"))
        self.assertEqual(fee, Decimal("0.38"))
        self.assertEqual(net, Decimal("24.62"))

    def test_20_at_1_5_percent(self):
        _, fee, net = calculate_withdrawal_fee(Decimal("20.00"))
        self.assertEqual(fee, Decimal("0.30"))
        self.assertEqual(net, Decimal("19.70"))

    def test_configurable_rate(self):
        _set_fee_config(percent="2.00", enabled=True)
        rate, fee, net = calculate_withdrawal_fee(Decimal("100.00"))
        self.assertEqual(rate, Decimal("2.00"))
        self.assertEqual(fee, Decimal("2.00"))
        self.assertEqual(net, Decimal("98.00"))

    def test_disabled_config_zero_fee(self):
        _set_fee_config(percent="1.50", enabled=False)
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        self.assertEqual(rate, Decimal("0.00"))
        self.assertEqual(fee, Decimal("0.00"))
        self.assertEqual(net, Decimal("1000.00"))

    def test_gross_equals_fee_plus_net(self):
        _, fee, net = calculate_withdrawal_fee(Decimal("777.77"))
        self.assertEqual(fee + net, Decimal("777.77"))

    def test_returns_decimal_never_float(self):
        rate, fee, net = calculate_withdrawal_fee(Decimal("100.00"))
        self.assertIsInstance(rate, Decimal)
        self.assertIsInstance(fee, Decimal)
        self.assertIsInstance(net, Decimal)


# ── 2. Fee snapshot at creation (real end-to-end flow) ──────────────────────

class FeeSnapshotOnCreationTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        make_totp_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def _flow(self, amount):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=amount)

    def test_new_withdrawal_snapshots_fee_fields(self):
        self._flow("1250.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.fee_rate, Decimal("1.50"))
        self.assertEqual(wr.fee_amount, Decimal("18.75"))
        self.assertEqual(wr.net_amount, Decimal("1231.25"))

    def test_gross_amount_usd_unchanged_semantics(self):
        """amount_usd keeps meaning GROSS debited — MODEL A."""
        self._flow("1250.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1250.00"))

    def test_wallet_debited_by_gross_not_net(self):
        self._flow("1250.00")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000.00") - Decimal("1250.00"))

    def test_rate_change_does_not_alter_existing_request(self):
        self._flow("1000.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        original_fee = wr.fee_amount
        original_net = wr.net_amount

        _set_fee_config(percent="5.00", enabled=True)

        wr.refresh_from_db()
        self.assertEqual(wr.fee_amount, original_fee)
        self.assertEqual(wr.net_amount, original_net)
        self.assertEqual(wr.fee_rate, Decimal("1.50"))


# ── 3. Writer core ───────────────────────────────────────────────────────────

class WriterCoreTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("5000"))

    def test_amount_equals_fee_amount(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        row = record_withdrawal_fee_revenue(wr)
        self.assertEqual(row.amount, Decimal("15.00"))

    def test_revenue_type_is_withdraw_fee(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        row = record_withdrawal_fee_revenue(wr)
        self.assertEqual(row.revenue_type, BrokerLedger.REV_WITHDRAW_FEE)

    def test_source_withdrawal_set(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        row = record_withdrawal_fee_revenue(wr)
        self.assertEqual(row.source_withdrawal_id, wr.pk)

    def test_meta_carries_withdrawal_reference(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        row = record_withdrawal_fee_revenue(wr)
        self.assertEqual(row.meta["withdrawal_id"], wr.pk)
        self.assertEqual(row.meta["gross_amount"], "1000.00")

    def test_legacy_row_no_snapshot_is_noop(self):
        """fee_rate/fee_amount/net_amount all None — never book a
        retroactive fee for a withdrawal that predates this block."""
        wr = _make_wr(self.user, "1000.00")  # no fee kwargs — legacy
        result = record_withdrawal_fee_revenue(wr)
        self.assertIsNone(result)
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)

    def test_disabled_fee_zero_amount_is_noop(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("0.00"),
                      fee_amount=Decimal("0.00"), net_amount=Decimal("1000.00"))
        result = record_withdrawal_fee_revenue(wr)
        self.assertIsNone(result)
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)

    def test_unsaved_withdrawal_raises_value_error(self):
        unsaved = WithdrawalRequest(user=self.user, amount_usd=Decimal("10"))
        with self.assertRaises(ValueError):
            record_withdrawal_fee_revenue(unsaved)

    def test_second_call_raises_duplicate(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        record_withdrawal_fee_revenue(wr)
        with self.assertRaises(DuplicateWithdrawalFeeRevenue):
            record_withdrawal_fee_revenue(wr)

    def test_second_call_does_not_create_second_row(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        record_withdrawal_fee_revenue(wr)
        try:
            record_withdrawal_fee_revenue(wr)
        except DuplicateWithdrawalFeeRevenue:
            pass
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 1)

    def test_direct_duplicate_ledger_write_raises_integrity_error(self):
        wr = _make_wr(self.user, "1000.00", fee_rate=Decimal("1.50"),
                      fee_amount=Decimal("15.00"), net_amount=Decimal("985.00"))
        record_withdrawal_fee_revenue(wr)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerLedger.objects.create(
                    revenue_type=BrokerLedger.REV_WITHDRAW_FEE, amount=wr.fee_amount, source_withdrawal=wr,
                )


# ── 4. Canonical earning event — COMPLETED only ─────────────────────────────

class CanonicalEventTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("2000"))

    def _snapshotted_wr(self, amount="1000.00"):
        rate, fee, net = calculate_withdrawal_fee(Decimal(amount))
        return _make_wr(self.user, amount, fee_rate=rate, fee_amount=fee, net_amount=net)

    def test_pending_zero_revenue(self):
        wr = self._snapshotted_wr()
        wr.status = WithdrawalRequest.STATUS_PENDING
        wr.save(update_fields=["status"])
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)

    def test_processing_zero_revenue(self):
        self._snapshotted_wr()  # status defaults to PROCESSING in _make_wr
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_WITHDRAW_FEE).count(), 0)

    def test_completed_books_exactly_one_revenue(self):
        wr = self._snapshotted_wr("1000.00")
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-a")
        _complete(wr, ref="ref-a")
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_WITHDRAW_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("15.00"))
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_COMPLETED)

    def test_failed_zero_revenue_full_gross_refund(self):
        wr = self._snapshotted_wr("1000.00")
        self.wallet.refresh_from_db()  # _make_wr debited it via a separate query
        before = self.wallet.available_balance
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-b")
        _fail(wr, ref="ref-b")
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + wr.amount_usd)  # full GROSS, not net

    def test_rejected_zero_revenue_full_gross_refund(self):
        admin = make_user(is_staff=True, is_superuser=True)
        wr = self._snapshotted_wr("1000.00")
        wr.status = WithdrawalRequest.STATUS_PENDING
        wr.save(update_fields=["status"])
        self.wallet.refresh_from_db()  # _make_wr debited it via a separate query
        before = self.wallet.available_balance

        from simulator.admin import WithdrawalRequestAdmin, reject_withdrawals
        from django.contrib.admin.sites import AdminSite
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        with patch("simulator.tasks.send_email_async.delay"):
            reject_withdrawals(ma, _admin_request(admin), WithdrawalRequest.objects.filter(pk=wr.pk))

        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + wr.amount_usd)


# ── 5. Duplicate / retry / replay ───────────────────────────────────────────

class DuplicateCompletionTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("2000"))

    def test_duplicate_completed_webhook_exactly_one_revenue(self):
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(self.user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-dup")
        _complete(wr, ref="ref-dup")
        _complete(wr, ref="ref-dup")  # duplicate webhook delivery
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 1)


# ── 6. Concurrency — real DB race ───────────────────────────────────────────

class ConcurrencyTests(TransactionTestCase):
    def test_two_threads_same_withdrawal_exactly_one_succeeds(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        wr_id = wr.pk

        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            w = WithdrawalRequest.objects.get(pk=wr_id)
            return record_withdrawal_fee_revenue(w)

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
        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal_id=wr_id).count(), 1)


# ── 7. Wallet accounting — do not redefine ──────────────────────────────────

class WalletAccountingTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("2000"))

    def test_single_withdraw_transaction_no_second_fee_debit(self):
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(self.user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-wa")
        _complete(wr, ref="ref-wa")
        withdraw_txs = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW)
        self.assertEqual(withdraw_txs.count(), 1)
        self.assertEqual(withdraw_txs.first().amount, Decimal("-1000.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).exclude(tx_type=WalletTransaction.TX_DEPOSIT).count(),
            1,
        )


# ── 8. Payout estimate uses net_amount ──────────────────────────────────────

class _CapturingAdapter:
    provider_name = "nowpayments"

    def __init__(self):
        self.calls = []

    def estimate(self, amount_usd, asset):
        self.calls.append(amount_usd)
        return Decimal("0.001")

    def create_payout(self, attempt, *, callback_url=""):
        class _Result:
            accepted = True
            provider_reference = "wd-capture-test"
            provider_batch_id = "batch-capture-test"
            provider_amount = Decimal("0.001")
            raw_status = "CREATED"
        return _Result()


class PayoutEstimateBasisTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("2000"))

    def test_estimate_uses_net_amount_for_new_withdrawal(self):
        from simulator.payout_orchestrator import submit_withdrawal_to_provider
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(self.user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net,
                      status=WithdrawalRequest.STATUS_PENDING)
        adapter = _CapturingAdapter()
        submit_withdrawal_to_provider(wr, adapter=adapter, actor=None, callback_url="http://x/cb")
        self.assertEqual(adapter.calls, [Decimal("985.00")])

    def test_estimate_falls_back_to_gross_for_legacy_withdrawal(self):
        from simulator.payout_orchestrator import submit_withdrawal_to_provider
        wr = _make_wr(self.user, "1000.00", status=WithdrawalRequest.STATUS_PENDING)  # no snapshot
        adapter = _CapturingAdapter()
        submit_withdrawal_to_provider(wr, adapter=adapter, actor=None, callback_url="http://x/cb")
        self.assertEqual(adapter.calls, [Decimal("1000.00")])


# ── 9. Legacy compatibility — critical ───────────────────────────────────────

class LegacyCompatibilityTests(TestCase):
    def setUp(self):
        _set_fee_config(percent="1.50", enabled=True)
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("2000"))

    def test_legacy_withdrawal_completed_acquires_zero_fee(self):
        wr = _make_wr(self.user, "1000.00")  # no snapshot — pre-FASE-B row
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-legacy")
        _complete(wr, ref="ref-legacy")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_WITHDRAW_FEE).count(), 0)

    def test_legacy_payout_amount_unchanged(self):
        """crypto_amount recorded at submission reflects the FULL gross —
        never silently reduced by a fee that was never snapshotted."""
        from simulator.payout_orchestrator import submit_withdrawal_to_provider
        wr = _make_wr(self.user, "1000.00", status=WithdrawalRequest.STATUS_PENDING)
        adapter = _CapturingAdapter()
        submit_withdrawal_to_provider(wr, adapter=adapter, actor=None, callback_url="http://x/cb")
        self.assertEqual(adapter.calls, [Decimal("1000.00")])

    def test_legacy_duplicate_completion_still_zero_fee(self):
        wr = _make_wr(self.user, "1000.00")
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-legacy-dup")
        _complete(wr, ref="ref-legacy-dup")
        _complete(wr, ref="ref-legacy-dup")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_WITHDRAW_FEE).count(), 0)


# ── 10. IB isolation ─────────────────────────────────────────────────────────

class IBIsolationTests(TestCase):
    def test_writer_alone_creates_zero_ib_obligations(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        before = IBCommissionObligation.objects.count()
        record_withdrawal_fee_revenue(wr)
        self.assertEqual(IBCommissionObligation.objects.count(), before)


# ── 11. Treasury isolation ──────────────────────────────────────────────────

class TreasuryIsolationTests(TestCase):
    def test_writer_alone_creates_zero_treasury_requests(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        before = TreasuryOperationRequest.objects.count()
        record_withdrawal_fee_revenue(wr)
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)


# ── 12. ECONOMICS-03 integration ────────────────────────────────────────────

class Economics03IntegrationTests(TestCase):
    def test_summary_reflects_new_revenue_automatically(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        record_withdrawal_fee_revenue(wr)
        summary = broker_economics_summary()
        self.assertEqual(summary.withdrawal_fee_revenue, Decimal("15.00"))
        self.assertEqual(summary.gross_broker_economic_result, Decimal("15.00"))

    def test_broker_pnl_reflects_new_revenue_automatically(self):
        from simulator import broker_pnl
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("500.00"))
        wr = _make_wr(user, "500.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        record_withdrawal_fee_revenue(wr)
        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.withdraw_fee, Decimal("7.50"))

    def test_provider_cost_remains_unrepresented_never_netted(self):
        """Confirms this writer never invents/subtracts a provider cost —
        the booked amount is fee_amount exactly, gross fee revenue."""
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        row = record_withdrawal_fee_revenue(wr)
        self.assertEqual(row.amount, fee)  # never fee - some invented cost


# ── 13. Rollback ─────────────────────────────────────────────────────────────

class RollbackTests(TestCase):
    def test_rollback_after_writer_leaves_no_orphaned_ledger_row(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)

        class _BoomAfterWriter(Exception):
            pass

        with self.assertRaises(_BoomAfterWriter):
            with transaction.atomic():
                record_withdrawal_fee_revenue(wr)
                raise _BoomAfterWriter("later step in the same transaction failed")

        self.assertEqual(BrokerLedger.objects.filter(source_withdrawal=wr).count(), 0)


# ── 14. Reversal / correction compatibility ─────────────────────────────────

class ReversalCompatibilityTests(TestCase):
    def test_adjustment_against_withdrawal_revenue_never_mutates_original(self):
        _set_fee_config(percent="1.50", enabled=True)
        user = make_user()
        make_wallet(user, initial_balance=Decimal("2000"))
        rate, fee, net = calculate_withdrawal_fee(Decimal("1000.00"))
        wr = _make_wr(user, "1000.00", fee_rate=rate, fee_amount=fee, net_amount=net)
        original = record_withdrawal_fee_revenue(wr)
        original_amount = original.amount

        create_broker_economic_adjustment(
            amount=-Decimal("15.00"), reason="Test correction for withdrawal fee",
            actor=make_user(), idempotency_key="test-withdrawal-correction-001",
        )

        original.refresh_from_db()
        self.assertEqual(original.amount, original_amount)
        self.assertEqual(original.revenue_type, BrokerLedger.REV_WITHDRAW_FEE)
        self.assertEqual(
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1,
        )


# ── 15. Security gates unchanged ────────────────────────────────────────────

class SecurityGatesUnchangedTests(TestCase):
    """Direct confirmation that KYC/2FA/$20-minimum gates are untouched by
    this block — full regression coverage lives in the existing dedicated
    files (test_kyc_withdraw_gate.py, test_withdraw_2fa.py,
    test_withdrawal_minimum.py), re-run as part of FASE B validation."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("5000"))
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_no_kyc_blocks_withdrawal(self):
        make_totp_device(self.user)
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd="100.00")
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_minimum_20_still_enforced(self):
        make_kyc_approved(self.user)
        make_totp_device(self.user)
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd="10.00")
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)


# ── 16. Config admin — mandatory reason / superuser-only ───────────────────

class WithdrawalFeeConfigModelTests(TestCase):
    def test_singleton_pk_always_one(self):
        cfg = WithdrawalFeeConfig.get_current()
        self.assertEqual(cfg.pk, 1)
        cfg2 = WithdrawalFeeConfig(percent=Decimal("3.00"))
        cfg2.save()
        self.assertEqual(WithdrawalFeeConfig.objects.count(), 1)

    def test_delete_refused(self):
        cfg = WithdrawalFeeConfig.get_current()
        with self.assertRaises(ValueError):
            cfg.delete()

    def test_admin_requires_superuser(self):
        from simulator.admin import WithdrawalFeeConfigAdmin
        from django.contrib.admin.sites import AdminSite
        ma = WithdrawalFeeConfigAdmin(WithdrawalFeeConfig, AdminSite())
        staff = make_user(is_staff=True, is_superuser=False)
        req = _admin_request(staff)
        self.assertFalse(ma.has_change_permission(req))
        self.assertFalse(ma.has_module_permission(req))

    def test_admin_form_requires_reason(self):
        from simulator.admin import WithdrawalFeeConfigForm
        form = WithdrawalFeeConfigForm(data={"percent": "2.00", "enabled": True, "reason": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("reason", form.errors)
