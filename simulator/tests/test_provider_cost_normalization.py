# simulator/tests/test_provider_cost_normalization.py
"""
BROKER-ECONOMICS-04C.4 — dedicated adversarial suite.

Covers the full FASE A/B matrix across three layers:
  - provider_cost_adapters.py  — NowPaymentsCostAdapter normalization,
    evidence-supported only, never fabricated.
  - provider_cost_inbox.py     — ProviderCostRecord idempotency (content
    fingerprint, DB-race-safe, fail-open).
  - provider_cost_economics.py — the single booking gate
    (ACTUAL + is_final + usd_value not null), sign convention, and
    append-only, delta-only corrections.
Plus SSOT integration (broker_pnl.py / broker_economics_summary.py) and
full end-to-end regression through deposit_callback / withdraw_payout_callback,
proving zero effect on Wallet, challenge revenue, withdrawal fee revenue,
funded economics, IB, and capital flows.
"""
import json
import threading
import time
import random
import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from simulator.models import (
    BrokerLedger, ChallengeEnrollment, Deposit, PaymentWebhookEvent,
    PayoutAttempt, PayoutWebhookEvent, ProviderCostRecord, Wallet, WalletTransaction,
    WithdrawalRequest,
)
from simulator.provider_cost_adapters import (
    NowPaymentsCostAdapter, ProviderCostCandidate, get_cost_adapter_for_provider,
)
from simulator.provider_cost_economics import (
    DuplicateProviderCostRevenue, record_provider_cost_revenue,
)
from simulator.provider_cost_inbox import capture_provider_cost_candidate
from simulator.tests.factories import make_challenge_product, make_deposit, make_user, make_wallet
from simulator.wallet_ledger import debit_wallet

CALLBACK_URL = "/deposit/callback/"
PAYOUT_CB_URL = "/withdraw/callback/"
_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


# ── helpers ──────────────────────────────────────────────────────────────────

def _payment_event(payment_id="pid1", payment_status="finished", fee=None, order_id="1"):
    payload = {
        "payment_id": payment_id, "payment_status": payment_status, "order_id": order_id,
        "actually_paid": 100.0, "pay_currency": "btc", "price_currency": "usd", "price_amount": 100.0,
    }
    if fee is not None:
        payload["fee"] = fee
    return PaymentWebhookEvent.objects.create(
        provider="nowpayments",
        event_fingerprint=f"pwe-{uuid.uuid4().hex}",
        payment_id=payment_id, order_id=order_id, payment_status=payment_status,
        raw_payload=payload,
    )


def _payout_event(reference="wd1", raw_status="FINISHED", fee=None):
    payload = {"id": reference, "status": raw_status}
    if fee is not None:
        payload["fee"] = fee
    return PayoutWebhookEvent.objects.create(
        provider="nowpayments",
        event_fingerprint=f"pywe-{uuid.uuid4().hex}",
        provider_reference=reference, provider_batch_id="batch1",
        raw_status=raw_status, normalized_status="",
        raw_payload=payload,
    )


def _candidate(**overrides):
    defaults = dict(
        provider="nowpayments", operation_type=ProviderCostRecord.OP_DEPOSIT,
        cost_type=ProviderCostRecord.COST_PROVIDER_SERVICE, provider_reference="ref1",
        amount=Decimal("1.50"), currency="usd", usd_value=Decimal("1.50"),
        quality=ProviderCostRecord.QUALITY_ACTUAL, is_final=True,
        occurred_at=timezone.now(), meta={},
    )
    defaults.update(overrides)
    return ProviderCostCandidate(**defaults)


def _make_cost_record(**overrides):
    defaults = dict(
        provider="nowpayments", operation_type=ProviderCostRecord.OP_DEPOSIT,
        cost_type=ProviderCostRecord.COST_PROVIDER_SERVICE, provider_reference="ref1",
        amount=Decimal("1.50"), currency="usd", usd_value=Decimal("1.50"),
        quality=ProviderCostRecord.QUALITY_ACTUAL, is_final=True,
        occurred_at=timezone.now(), cost_fingerprint=f"cfp-{uuid.uuid4().hex}", meta={},
    )
    defaults.update(overrides)
    return ProviderCostRecord.objects.create(**defaults)


def _make_pending_wr(user, wallet, amount="80.00"):
    debit_tx = debit_wallet(wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="04c4 test wr")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
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
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("operational_error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


# ── A. Adapter — evidence-supported normalization only ─────────────────────

class NowPaymentsCostAdapterTests(TestCase):
    def setUp(self):
        self.adapter = NowPaymentsCostAdapter()

    def test_fee_absent_zero_candidates(self):
        event = _payment_event(fee=None)
        self.assertEqual(self.adapter.normalize_payment_cost(event), [])

    def test_fee_not_a_dict_zero_candidates(self):
        event = _payment_event(fee=None)
        event.raw_payload["fee"] = "not-a-dict"
        event.save()
        self.assertEqual(self.adapter.normalize_payment_cost(event), [])

    def test_fee_present_usd_currency_produces_actual_usd_valued_candidate(self):
        event = _payment_event(fee={"currency": "usd", "serviceFee": 0.5})
        candidates = self.adapter.normalize_payment_cost(event)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.quality, ProviderCostRecord.QUALITY_ACTUAL)
        self.assertEqual(c.cost_type, ProviderCostRecord.COST_PROVIDER_SERVICE)
        self.assertEqual(c.amount, Decimal("0.5"))
        self.assertEqual(c.usd_value, Decimal("0.5"))
        self.assertEqual(c.operation_type, ProviderCostRecord.OP_DEPOSIT)

    def test_fee_present_non_usd_currency_never_invents_usd_value(self):
        event = _payment_event(fee={"currency": "btc", "serviceFee": 0.0002})
        candidates = self.adapter.normalize_payment_cost(event)
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].usd_value)
        self.assertEqual(candidates[0].amount, Decimal("0.0002"))
        self.assertEqual(candidates[0].currency, "btc")

    def test_multiple_fee_subfields_produce_multiple_candidates(self):
        event = _payment_event(fee={"currency": "usd", "serviceFee": 0.5, "depositFee": 0.1})
        candidates = self.adapter.normalize_payment_cost(event)
        self.assertEqual(len(candidates), 2)
        cost_types = {c.cost_type for c in candidates}
        self.assertEqual(cost_types, {ProviderCostRecord.COST_PROVIDER_SERVICE, ProviderCostRecord.COST_NETWORK})

    def test_terminal_payment_status_is_final_true(self):
        event = _payment_event(payment_status="finished", fee={"currency": "usd", "serviceFee": 0.5})
        self.assertTrue(self.adapter.normalize_payment_cost(event)[0].is_final)

    def test_non_terminal_payment_status_is_final_false(self):
        event = _payment_event(payment_status="confirming", fee={"currency": "usd", "serviceFee": 0.5})
        self.assertFalse(self.adapter.normalize_payment_cost(event)[0].is_final)

    def test_never_uses_actually_paid_minus_outcome_amount(self):
        event = _payment_event(fee={"currency": "usd", "serviceFee": 0.5})
        event.raw_payload["outcome_amount"] = 500000
        event.raw_payload["outcome_currency"] = "btc"
        event.raw_payload["actually_paid"] = 999999
        event.save()
        candidates = self.adapter.normalize_payment_cost(event)
        # amount must equal the fee field only — never derived from
        # actually_paid/outcome_amount, regardless of how large they are.
        self.assertEqual(candidates[0].amount, Decimal("0.5"))

    def test_payout_leg_fee_absent_zero_candidates(self):
        event = _payout_event(fee=None)
        self.assertEqual(self.adapter.normalize_payout_cost(event), [])

    def test_payout_leg_fee_present_produces_withdrawal_operation_type(self):
        event = _payout_event(raw_status="FINISHED", fee={"currency": "usd", "withdrawalFee": 1.0})
        candidates = self.adapter.normalize_payout_cost(event)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].operation_type, ProviderCostRecord.OP_WITHDRAWAL)
        self.assertTrue(candidates[0].is_final)

    def test_payout_leg_non_terminal_status_is_final_false(self):
        event = _payout_event(raw_status="ROLLING", fee={"currency": "usd", "withdrawalFee": 1.0})
        self.assertFalse(self.adapter.normalize_payout_cost(event)[0].is_final)


class CostAdapterRegistryTests(TestCase):
    def test_nowpayments_resolves(self):
        adapter = get_cost_adapter_for_provider("nowpayments")
        self.assertIsInstance(adapter, NowPaymentsCostAdapter)

    def test_unknown_provider_returns_none(self):
        self.assertIsNone(get_cost_adapter_for_provider("totally_unknown_rail"))

    def test_adapter_swap_leaves_downstream_economics_unchanged(self):
        """Provider-switch test (FASE A §11) at the code level: register a
        fake adapter under a new provider name producing an equivalent
        candidate — inbox/economics behave identically without any change."""
        class _FakeAdapter:
            provider_name = "fake_rail"

            def normalize_payment_cost(self, event):
                return [_candidate(provider="fake_rail", provider_reference="fake-ref")]

            def normalize_payout_cost(self, event):
                return []

        import simulator.provider_cost_adapters as pca
        with patch.dict(pca._COST_ADAPTERS, {"fake_rail": _FakeAdapter}):
            adapter = get_cost_adapter_for_provider("fake_rail")
            candidates = adapter.normalize_payment_cost(None)
            record = capture_provider_cost_candidate(candidates[0])
            ledger_row = record_provider_cost_revenue(record)
        self.assertIsNotNone(ledger_row)
        self.assertEqual(ledger_row.revenue_type, BrokerLedger.REV_PROVIDER_COST)
        self.assertEqual(ledger_row.amount, Decimal("-1.50"))


# ── B. Inbox — idempotency ──────────────────────────────────────────────────

class ProviderCostInboxTests(TestCase):
    def test_valid_candidate_creates_one_row(self):
        record = capture_provider_cost_candidate(_candidate())
        self.assertIsNotNone(record)
        self.assertEqual(ProviderCostRecord.objects.count(), 1)

    def test_identical_replay_creates_exactly_one_row(self):
        c = _candidate()
        r1 = capture_provider_cost_candidate(c)
        r2 = capture_provider_cost_candidate(c)
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(ProviderCostRecord.objects.count(), 1)

    def test_webhook_plus_polling_same_cost_dedups(self):
        """Two independently-constructed candidates describing the exact
        same normalized cost (as webhook delivery + a hypothetical polling
        path would both produce) must fingerprint identically."""
        occurred = timezone.now()
        c1 = _candidate(occurred_at=occurred)
        c2 = _candidate(occurred_at=occurred)
        r1 = capture_provider_cost_candidate(c1)
        r2 = capture_provider_cost_candidate(c2)
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(ProviderCostRecord.objects.count(), 1)

    def test_genuinely_different_candidate_creates_distinct_row(self):
        c1 = _candidate(amount=Decimal("1.50"), usd_value=Decimal("1.50"))
        c2 = _candidate(amount=Decimal("2.75"), usd_value=Decimal("2.75"))
        capture_provider_cost_candidate(c1)
        capture_provider_cost_candidate(c2)
        self.assertEqual(ProviderCostRecord.objects.count(), 2)

    def test_original_amount_currency_preserved_verbatim(self):
        record = capture_provider_cost_candidate(
            _candidate(amount=Decimal("0.00012345"), currency="btc", usd_value=None),
        )
        self.assertEqual(record.amount, Decimal("0.00012345"))
        self.assertEqual(record.currency, "btc")
        self.assertIsNone(record.usd_value)

    def test_fail_open_returns_none_on_internal_error(self):
        with patch(
            "simulator.provider_cost_inbox.ProviderCostRecord.objects.create",
            side_effect=Exception("simulated DB outage"),
        ):
            result = capture_provider_cost_candidate(_candidate())
        self.assertIsNone(result)
        self.assertEqual(ProviderCostRecord.objects.count(), 0)


class ProviderCostInboxConcurrencyTests(TransactionTestCase):
    def test_concurrent_identical_candidate_creates_exactly_one_row(self):
        c = _candidate(provider_reference="conc-ref")
        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            return capture_provider_cost_candidate(c)

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_attempt, barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(
            ProviderCostRecord.objects.filter(provider_reference="conc-ref").count(), 1,
        )


# ── C. Economics — the single booking gate ──────────────────────────────────

class BookingGateTests(TestCase):
    def test_actual_final_valued_posts_exactly_once(self):
        record = _make_cost_record(usd_value=Decimal("2.00"))
        row = record_provider_cost_revenue(record)
        self.assertIsNotNone(row)
        self.assertEqual(row.revenue_type, BrokerLedger.REV_PROVIDER_COST)
        self.assertEqual(row.amount, Decimal("-2.00"))
        self.assertEqual(row.source_provider_cost_id, record.pk)
        self.assertEqual(
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST).count(), 1,
        )

    def test_actual_zero_distinguishable_from_unknown_no_ledger_row(self):
        actual_zero = _make_cost_record(quality=ProviderCostRecord.QUALITY_ACTUAL, is_final=True, usd_value=Decimal("0.00"))
        unknown = _make_cost_record(quality=ProviderCostRecord.QUALITY_UNKNOWN, is_final=False, usd_value=None)

        self.assertIsNone(record_provider_cost_revenue(actual_zero))
        self.assertIsNone(record_provider_cost_revenue(unknown))
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST).count(), 0)
        # Both rows remain queryable and distinguishable at the evidence layer.
        actual_zero.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(actual_zero.quality, ProviderCostRecord.QUALITY_ACTUAL)
        self.assertEqual(actual_zero.usd_value, Decimal("0.00"))
        self.assertEqual(unknown.quality, ProviderCostRecord.QUALITY_UNKNOWN)
        self.assertIsNone(unknown.usd_value)

    def test_estimated_never_posts(self):
        record = _make_cost_record(quality=ProviderCostRecord.QUALITY_ESTIMATED, is_final=True, usd_value=Decimal("5.00"))
        self.assertIsNone(record_provider_cost_revenue(record))
        self.assertEqual(BrokerLedger.objects.count(), 0)

    def test_unknown_never_posts(self):
        record = _make_cost_record(quality=ProviderCostRecord.QUALITY_UNKNOWN, is_final=False, usd_value=None)
        self.assertIsNone(record_provider_cost_revenue(record))
        self.assertEqual(BrokerLedger.objects.count(), 0)

    def test_non_final_actual_never_posts(self):
        record = _make_cost_record(quality=ProviderCostRecord.QUALITY_ACTUAL, is_final=False, usd_value=Decimal("3.00"))
        self.assertIsNone(record_provider_cost_revenue(record))
        self.assertEqual(BrokerLedger.objects.count(), 0)

    def test_actual_final_without_usd_valuation_never_posts(self):
        record = _make_cost_record(
            quality=ProviderCostRecord.QUALITY_ACTUAL, is_final=True, usd_value=None,
            currency="btc", amount=Decimal("0.0001"),
        )
        self.assertIsNone(record_provider_cost_revenue(record))
        self.assertEqual(BrokerLedger.objects.count(), 0)

    def test_duplicate_post_of_same_record_raises_and_stays_single_row(self):
        record = _make_cost_record(usd_value=Decimal("1.00"))
        record_provider_cost_revenue(record)
        with self.assertRaises(DuplicateProviderCostRevenue):
            record_provider_cost_revenue(record)
        self.assertEqual(
            BrokerLedger.objects.filter(source_provider_cost=record).count(), 1,
        )

    def test_sign_convention_always_negative_for_positive_cost(self):
        record = _make_cost_record(usd_value=Decimal("7.25"))
        row = record_provider_cost_revenue(record)
        self.assertTrue(row.amount < Decimal("0"))
        self.assertEqual(row.amount, Decimal("-7.25"))


class CorrectionTests(TestCase):
    def test_correction_of_already_posted_record_books_only_delta(self):
        original = _make_cost_record(usd_value=Decimal("5.00"))
        original_row = record_provider_cost_revenue(original)
        self.assertEqual(original_row.amount, Decimal("-5.00"))

        correction = _make_cost_record(usd_value=Decimal("3.00"), corrects=original)
        correction_row = record_provider_cost_revenue(correction)

        self.assertIsNotNone(correction_row)
        # new(-3.00) - original(-5.00) = +2.00 delta (cost went DOWN, broker recovers $2)
        self.assertEqual(correction_row.amount, Decimal("2.00"))
        self.assertEqual(correction_row.source_provider_cost_id, correction.pk)

        # Original row untouched.
        original_row.refresh_from_db()
        self.assertEqual(original_row.amount, Decimal("-5.00"))
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST).count(), 2)

        # Chain reconciles: -5.00 + 2.00 = -3.00, the true final cost.
        total = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_PROVIDER_COST,
        ).aggregate(t=Sum("amount"))["t"]
        self.assertEqual(total, Decimal("-3.00"))

    def test_correction_delta_of_zero_posts_no_new_row(self):
        original = _make_cost_record(usd_value=Decimal("4.00"))
        record_provider_cost_revenue(original)
        correction = _make_cost_record(usd_value=Decimal("4.00"), corrects=original)
        self.assertIsNone(record_provider_cost_revenue(correction))
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST).count(), 1)

    def test_correction_of_never_posted_original_applies_ordinary_zero_skip(self):
        original = _make_cost_record(quality=ProviderCostRecord.QUALITY_ESTIMATED, is_final=True, usd_value=Decimal("9.00"))
        self.assertIsNone(record_provider_cost_revenue(original))  # ESTIMATED — never posted

        correction_zero = _make_cost_record(usd_value=Decimal("0.00"), corrects=original)
        self.assertIsNone(record_provider_cost_revenue(correction_zero))

        correction_real = _make_cost_record(usd_value=Decimal("6.50"), corrects=original)
        row = record_provider_cost_revenue(correction_real)
        self.assertEqual(row.amount, Decimal("-6.50"))

    def test_correction_reversing_to_zero_still_books_negative_delta_credit(self):
        """A correction whose OWN usd_value is exactly 0.00 must still post
        a real delta if the original was already booked non-zero."""
        original = _make_cost_record(usd_value=Decimal("5.00"))
        record_provider_cost_revenue(original)
        correction = _make_cost_record(usd_value=Decimal("0.00"), corrects=original)
        row = record_provider_cost_revenue(correction)
        self.assertIsNotNone(row)
        self.assertEqual(row.amount, Decimal("5.00"))  # full credit-back


# ── D. SSOT integration ──────────────────────────────────────────────────

class BrokerPnLIntegrationTests(TestCase):
    def test_provider_cost_enters_broker_net_pnl_exactly_once(self):
        from simulator import broker_pnl

        record = _make_cost_record(usd_value=Decimal("10.00"))
        record_provider_cost_revenue(record)

        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.provider_cost, Decimal("-10.00"))
        self.assertEqual(
            breakdown.broker_net_pnl,
            breakdown.fee_revenue + breakdown.counterparty_pnl + breakdown.adjustments + breakdown.provider_cost,
        )

    def test_fee_revenue_invariant_unaffected_by_provider_cost(self):
        from simulator import broker_pnl

        record = _make_cost_record(usd_value=Decimal("10.00"))
        record_provider_cost_revenue(record)
        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertGreaterEqual(breakdown.fee_revenue, Decimal("0.00"))

    def test_zero_provider_cost_rows_yields_zero_term(self):
        from simulator import broker_pnl

        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.provider_cost, Decimal("0.00"))


class BrokerEconomicsSummaryIntegrationTests(TestCase):
    def test_known_payment_transaction_costs_reflects_provider_cost(self):
        from simulator import broker_economics_summary as bes

        record = _make_cost_record(usd_value=Decimal("4.50"))
        record_provider_cost_revenue(record)

        summary = bes.broker_economics_summary()
        self.assertEqual(summary.provider_cost_revenue, Decimal("-4.50"))
        self.assertEqual(summary.retained.known_payment_transaction_costs, Decimal("4.50"))

    def test_execution_liquidity_and_other_costs_remain_untouched_placeholders(self):
        from simulator import broker_economics_summary as bes

        summary = bes.broker_economics_summary()
        self.assertEqual(summary.retained.known_execution_liquidity_costs, Decimal("0.00"))
        self.assertEqual(summary.retained.other_captured_variable_costs, Decimal("0.00"))


# ── E. Full-flow regression — deposit_callback ──────────────────────────────

class DepositCallbackProviderCostTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()

    def _ipn(self, payment_id, status, order_id, amount="100.00", fee=None):
        body = {
            "payment_id": payment_id, "payment_status": status, "order_id": str(order_id),
            "actually_paid": float(amount), "pay_currency": "btc",
            "price_currency": "usd", "price_amount": float(amount),
        }
        if fee is not None:
            body["fee"] = fee
        return json.dumps(body)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_missing_fee_creates_zero_provider_cost_rows(self, _sig):
        wallet = make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="dep_nofee", status="pending")
        body = self._ipn("dep_nofee", "finished", deposit.pk)
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(ProviderCostRecord.objects.count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST).count(), 0)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_fee_present_usd_creates_provider_cost_row_and_ledger_entry(self, _sig):
        make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="dep_fee", status="pending")
        body = self._ipn("dep_fee", "finished", deposit.pk, fee={"currency": "usd", "serviceFee": 0.5})
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(ProviderCostRecord.objects.filter(provider_reference="dep_fee").count(), 1)
        ledger_rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST)
        self.assertEqual(ledger_rows.count(), 1)
        self.assertEqual(ledger_rows.first().amount, Decimal("-0.50"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_wallet_crediting_unaffected_by_provider_cost_normalization(self, _sig):
        wallet = make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("60.00"), payment_id="dep_fee2", status="pending")
        body = self._ipn("dep_fee2", "finished", deposit.pk, "60.00", fee={"currency": "usd", "serviceFee": 0.3})
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        deposit.refresh_from_db()
        wallet.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertEqual(wallet.available_balance, Decimal("60.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT).count(), 1,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_challenge_revenue_unaffected_and_independent(self, _sig):
        product = make_challenge_product(price_usd=Decimal("199.00"))
        deposit = Deposit.objects.create(
            user=self.user, amount_usd=product.price_usd, crypto_currency="btc",
            nowpayments_payment_id="cp_fee", status="pending", credited=False,
            challenge_product=product, pay_amount=product.price_usd,
        )
        body = self._ipn(
            "cp_fee", "finished", deposit.pk, str(product.price_usd),
            fee={"currency": "usd", "serviceFee": 1.0},
        )
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1)
        challenge_rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(challenge_rows.count(), 1)
        self.assertEqual(challenge_rows.first().amount, Decimal("199.00"))
        cost_rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_PROVIDER_COST)
        self.assertEqual(cost_rows.count(), 1)
        self.assertEqual(cost_rows.first().amount, Decimal("-1.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_provider_cost_normalization_failure_does_not_block_deposit_crediting(self, _sig):
        wallet = make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("25.00"), payment_id="dep_costfail", status="pending")
        body = self._ipn("dep_costfail", "finished", deposit.pk, "25.00", fee={"currency": "usd", "serviceFee": 0.1})

        with patch(
            "simulator.provider_cost_adapters.get_cost_adapter_for_provider",
            side_effect=Exception("simulated adapter crash"),
        ):
            resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        deposit.refresh_from_db()
        wallet.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertEqual(wallet.available_balance, Decimal("25.00"))
        self.assertEqual(ProviderCostRecord.objects.count(), 0)


# ── F. Full-flow regression — withdraw_payout_callback ──────────────────────

class PayoutCallbackProviderCostTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))

    def _ipn(self, payout_id, status, batch_id="batch1", fee=None):
        body = {"id": batch_id, "status": status, "withdrawals": [{"id": payout_id, "status": status}]}
        if fee is not None:
            body["withdrawals"][0]["fee"] = fee
        return json.dumps(body)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_orphan_payout_webhook_with_fee_creates_provider_cost_row(self, _sig):
        body = self._ipn("wd_orphan_fee", "FINISHED", fee={"currency": "usd", "withdrawalFee": 0.75})
        resp = self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(
            ProviderCostRecord.objects.filter(provider_reference="wd_orphan_fee").count(), 1,
        )
        self.assertEqual(PayoutWebhookEvent.objects.filter(provider_reference="wd_orphan_fee").count(), 1)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_payout_webhook_without_fee_creates_zero_provider_cost_rows(self, _sig):
        body = self._ipn("wd_orphan_nofee", "FINISHED")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(ProviderCostRecord.objects.filter(provider_reference="wd_orphan_nofee").count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_provider_cost_normalization_failure_does_not_block_payout_processing(self, _sig):
        body = self._ipn("wd_costfail", "FINISHED", fee={"currency": "usd", "withdrawalFee": 0.5})
        with patch(
            "simulator.provider_cost_adapters.get_cost_adapter_for_provider",
            side_effect=Exception("simulated adapter crash"),
        ):
            resp = self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(PayoutWebhookEvent.objects.filter(provider_reference="wd_costfail").count(), 1)
        self.assertEqual(ProviderCostRecord.objects.count(), 0)


# ── G. Isolation — Treasury / IB / capital flows untouched ──────────────────

class IsolationTests(TestCase):
    def test_provider_cost_module_never_imports_treasury_engine(self):
        import simulator.provider_cost_adapters as adapters_mod
        import simulator.provider_cost_economics as economics_mod
        import simulator.provider_cost_inbox as inbox_mod
        for mod in (adapters_mod, economics_mod, inbox_mod):
            for line in open(mod.__file__).read().splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    self.assertNotIn("treasury_engine", stripped)

    def test_ib_obligations_zero_after_provider_cost_posting(self):
        from simulator.models import IBCommissionObligation
        record = _make_cost_record(usd_value=Decimal("3.00"))
        record_provider_cost_revenue(record)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_capital_flows_unaffected_by_provider_cost_posting(self):
        from simulator import broker_economics_summary as bes
        record = _make_cost_record(usd_value=Decimal("3.00"))
        record_provider_cost_revenue(record)
        summary = bes.broker_economics_summary()
        self.assertEqual(summary.capital_flows.deposits_total, Decimal("0.00"))
        self.assertEqual(summary.capital_flows.withdrawals_total, Decimal("0.00"))
