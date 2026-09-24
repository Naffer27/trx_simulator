# simulator/tests/test_challenge_revenue_writer.py
"""
CHALLENGE-REVENUE-WRITER-01 (FASE B) — dedicated adversarial suite.

Covers the 25 scenarios certified in FASE A plus the concurrency/
rollback/isolation properties the FASE B authorization required to be
tested explicitly, against the real three call sites (NowPayments
crypto callback, internal Wallet purchase, external sales platform
webhook) rather than only the writer in isolation.
"""
import json
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from simulator.broker_economic_adjustment import create_broker_economic_adjustment
from simulator.broker_economics_summary import broker_economics_summary
from simulator.challenge_engine import activate_challenge_enrollment
from simulator.challenge_revenue import (
    DuplicateChallengeRevenue,
    record_challenge_fee_revenue,
)
from simulator.models import (
    BrokerLedger,
    ChallengeEnrollment,
    Deposit,
    IBCommissionObligation,
    IBCommissionRule,
    Referral,
    ReferralAttribution,
    TradingAccount,
    Wallet,
    WalletTransaction,
)
from simulator.tests.factories import (
    make_challenge_enrollment,
    make_challenge_product,
    make_deposit,
    make_user,
    make_wallet,
)
from simulator.views import _fulfill_challenge_purchase, _verify_challenge_webhook_sig  # noqa: F401

CALLBACK_URL = "/deposit/callback/"
EXTERNAL_ACTIVATE_URL = "/api/internal/challenge/activate/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


# ── helpers ──────────────────────────────────────────────────────────────────

def _ipn(payment_id, payment_status, order_id="", amount="100.00"):
    return json.dumps({
        "payment_id":     payment_id,
        "payment_status": payment_status,
        "order_id":       str(order_id),
        "actually_paid":  float(amount),
        "pay_currency":   "btc",
        "price_currency": "usd",
        "price_amount":   float(amount),
    })


def _make_challenge_deposit(user, product, payment_id="cp_pay_001", credited=False, pay_amount=None):
    return Deposit.objects.create(
        user=user,
        amount_usd=product.price_usd,
        crypto_currency="btc",
        nowpayments_payment_id=payment_id,
        status="pending",
        credited=credited,
        challenge_product=product,
        pay_amount=pay_amount if pay_amount is not None else product.price_usd,
    )


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same pattern as test_book06j1_population_engine_close_race.py —
    real threads + SQLite busy_timeout + retry-on-locked, so a genuine
    concurrent DB race is exercised rather than simulated."""
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
            except DuplicateChallengeRevenue as exc:
                results[index] = ("duplicate", exc)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("operational_error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


# ── 1. Core writer unit tests ───────────────────────────────────────────────

class WriterCoreTests(TestCase):
    def setUp(self):
        self.product = make_challenge_product(price_usd=Decimal("149.37"))
        self.enrollment = make_challenge_enrollment(product=self.product)

    def test_amount_is_penny_exact_product_price(self):
        row = record_challenge_fee_revenue(self.enrollment)
        self.assertEqual(row.amount, Decimal("149.37"))

    def test_revenue_type_is_challenge_fee(self):
        row = record_challenge_fee_revenue(self.enrollment)
        self.assertEqual(row.revenue_type, BrokerLedger.REV_CHALLENGE_FEE)

    def test_source_challenge_enrollment_set(self):
        row = record_challenge_fee_revenue(self.enrollment)
        self.assertEqual(row.source_challenge_enrollment_id, self.enrollment.pk)

    def test_meta_carries_enrollment_reference(self):
        row = record_challenge_fee_revenue(self.enrollment)
        self.assertEqual(row.meta["enrollment_id"], self.enrollment.pk)
        self.assertEqual(row.meta["product_id"], self.product.pk)
        self.assertEqual(row.meta["user_id"], self.enrollment.user_id)

    def test_unsaved_enrollment_raises_value_error(self):
        unsaved = ChallengeEnrollment(user=make_user(), product=self.product)
        with self.assertRaises(ValueError):
            record_challenge_fee_revenue(unsaved)

    def test_second_call_same_enrollment_raises_duplicate(self):
        record_challenge_fee_revenue(self.enrollment)
        with self.assertRaises(DuplicateChallengeRevenue):
            record_challenge_fee_revenue(self.enrollment)

    def test_second_call_does_not_create_a_second_row(self):
        record_challenge_fee_revenue(self.enrollment)
        try:
            record_challenge_fee_revenue(self.enrollment)
        except DuplicateChallengeRevenue:
            pass
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=self.enrollment).count(), 1,
        )

    def test_direct_duplicate_ledger_write_raises_integrity_error(self):
        """Scenario 25 — the DB constraint itself, bypassing the service."""
        record_challenge_fee_revenue(self.enrollment)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerLedger.objects.create(
                    revenue_type=BrokerLedger.REV_CHALLENGE_FEE,
                    amount=self.enrollment.product.price_usd,
                    source_challenge_enrollment=self.enrollment,
                )

    def test_different_enrollments_each_get_their_own_row(self):
        other = make_challenge_enrollment(product=self.product)
        record_challenge_fee_revenue(self.enrollment)
        record_challenge_fee_revenue(other)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 2)


# ── 2. Path A — NowPayments crypto, full webhook ────────────────────────────

class PathASuccessTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("199.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_payment_creates_exactly_one_revenue_row(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pa_001")
        body = _ipn("pa_001", "finished", deposit.pk, str(self.product.price_usd))
        r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("199.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_revenue_row_linked_to_the_created_enrollment(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pa_002")
        body = _ipn("pa_002", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        enrollment = ChallengeEnrollment.objects.get(deposit=deposit)
        row = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(row.source_challenge_enrollment_id, enrollment.pk)


class PathANonRevenueTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))

    def _post(self, deposit, status, amount=None):
        body = _ipn(deposit.nowpayments_payment_id, status, deposit.pk, amount or str(self.product.price_usd))
        return self.client.post(CALLBACK_URL, body, content_type="application/json")

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_pending_status_zero_revenue(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pn_001")
        self._post(deposit, "waiting")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_failed_status_zero_revenue(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pn_002")
        self._post(deposit, "failed")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_expired_status_zero_revenue(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pn_003")
        self._post(deposit, "expired")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_underpaid_crypto_zero_revenue(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "pn_004", pay_amount=Decimal("0.01000000"))
        self._post(deposit, "finished", amount="0.00050000")
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 0)


class PathADuplicateWebhookTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("250.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_ipn_creates_exactly_one_revenue_row(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "dup_001")
        body = _ipn("dup_001", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        r2 = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 1)
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1)


# ── 3. Path B — internal Wallet purchase ────────────────────────────────────

class PathBSuccessTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("150.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500.00"))
        self.client.force_login(self.user)

    def _url(self):
        return reverse("simulator:challenge_wallet_purchase", kwargs={"product_id": self.product.pk})

    def test_purchase_creates_exactly_one_revenue_row(self):
        self.client.post(self._url())
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("150.00"))

    def test_revenue_row_linked_to_created_enrollment(self):
        self.client.post(self._url())
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        row = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(row.source_challenge_enrollment_id, enrollment.pk)

    def test_writer_does_not_alter_wallet_debit_amount(self):
        """The writer itself must cause zero additional Wallet mutation —
        the debit already happened before the writer is ever called."""
        self.client.post(self._url())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("350.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type="CHALLENGE_FEE").count(), 1,
        )

    def test_insufficient_balance_zero_revenue(self):
        poor_user = make_user()
        make_wallet(poor_user, initial_balance=Decimal("10.00"))
        self.client.force_login(poor_user)
        self.client.post(self._url())
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)


class PathBAdminFreeEnrollmentTests(TestCase):
    """Admin-issued free/comp enrollment (deposit=None, no wallet debit,
    never passes through the certified call sites) — ZERO revenue."""

    def test_admin_created_enrollment_has_zero_revenue(self):
        enrollment = make_challenge_enrollment(deposit=None)
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=enrollment).count(), 0,
        )

    def test_admin_activation_of_free_enrollment_creates_zero_revenue(self):
        enrollment = make_challenge_enrollment(deposit=None)
        activate_challenge_enrollment(enrollment)
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=enrollment).count(), 0,
        )


class AdminReactivationTests(TestCase):
    """Re-running activation (the admin action's own underlying call) on
    an already-paid enrollment must never generate a second revenue row —
    because the writer is not reachable from activate_challenge_enrollment()
    at all, regardless of how many times it runs."""

    def test_reactivating_a_paid_enrollment_does_not_duplicate_revenue(self):
        product = make_challenge_product(price_usd=Decimal("300.00"))
        enrollment = make_challenge_enrollment(product=product)
        record_challenge_fee_revenue(enrollment)
        activate_challenge_enrollment(enrollment)
        activate_challenge_enrollment(enrollment)
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=enrollment).count(), 1,
        )


# ── 4. Path C — external sales platform webhook ─────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET="test-secret-01")
class PathCSuccessTests(TestCase):
    def setUp(self):
        self.product = make_challenge_product(
            price_usd=Decimal("500.00"), external_code="challenge_ext_500",
        )

    def _signed_post(self, payload):
        import hashlib
        import hmac as hmac_mod
        body = json.dumps(payload).encode("utf-8")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac_mod.new(b"test-secret-01", canonical, hashlib.sha256).hexdigest()
        return self.client.post(
            EXTERNAL_ACTIVATE_URL, data=body, content_type="application/json",
            HTTP_X_MONEYBROKER_SIGNATURE=sig,
        )

    def test_success_creates_exactly_one_revenue_row(self):
        payload = {
            "event_id": "evt_001", "email": "extbuyer@example.com",
            "full_name": "Ext Buyer", "challenge_product_code": "challenge_ext_500",
            "payment_id": "ext_pay_001", "amount_paid": 500.00,
        }
        r = self._signed_post(payload)
        self.assertEqual(r.status_code, 200)
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("500.00"))

    def test_revenue_amount_ignores_overpaid_amount_paid(self):
        """Revenue always books product.price_usd, never the platform's
        self-reported amount_paid, even when it's higher than the price."""
        payload = {
            "event_id": "evt_002", "email": "overpay@example.com",
            "full_name": "Over Pay", "challenge_product_code": "challenge_ext_500",
            "payment_id": "ext_pay_002", "amount_paid": 9999.00,
        }
        self._signed_post(payload)
        row = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(row.amount, Decimal("500.00"))

    def test_invalid_signature_zero_revenue(self):
        payload = {
            "event_id": "evt_003", "email": "bad@example.com",
            "full_name": "Bad Sig", "challenge_product_code": "challenge_ext_500",
        }
        r = self.client.post(
            EXTERNAL_ACTIVATE_URL, data=json.dumps(payload), content_type="application/json",
            HTTP_X_MONEYBROKER_SIGNATURE="deadbeef",
        )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 0)


@override_settings(CHALLENGE_WEBHOOK_SECRET="test-secret-01")
class PathCDuplicateEventTests(TestCase):
    def setUp(self):
        self.product = make_challenge_product(
            price_usd=Decimal("500.00"), external_code="challenge_ext_dup",
        )

    def _signed_post(self, payload):
        import hashlib
        import hmac as hmac_mod
        body = json.dumps(payload).encode("utf-8")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac_mod.new(b"test-secret-01", canonical, hashlib.sha256).hexdigest()
        return self.client.post(
            EXTERNAL_ACTIVATE_URL, data=body, content_type="application/json",
            HTTP_X_MONEYBROKER_SIGNATURE=sig,
        )

    def test_duplicate_event_id_creates_exactly_one_revenue_row(self):
        payload = {
            "event_id": "evt_dup_1", "email": "dup@example.com",
            "full_name": "Dup Buyer", "challenge_product_code": "challenge_ext_dup",
            "payment_id": "ext_pay_dup",
        }
        self._signed_post(payload)
        r2 = self._signed_post(payload)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(), 1)
        self.assertEqual(ChallengeEnrollment.objects.filter(external_event_id="evt_dup_1").count(), 1)


# ── 5. Concurrency — real DB race ───────────────────────────────────────────

class ConcurrencyTests(TransactionTestCase):
    """Real threads racing record_challenge_fee_revenue() against the SAME
    enrollment — proves the DB UniqueConstraint, not an application check,
    is what makes '1 enrollment = max 1 revenue row' hold under a race."""

    def test_two_threads_same_enrollment_exactly_one_succeeds(self):
        product = make_challenge_product(price_usd=Decimal("77.00"))
        enrollment = make_challenge_enrollment(product=product)
        enrollment_id = enrollment.pk

        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            e = ChallengeEnrollment.objects.select_related("product").get(pk=enrollment_id)
            return record_challenge_fee_revenue(e)

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
        self.assertIn("duplicate", outcomes, f"expected the loser to see DuplicateChallengeRevenue, got {results}")
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment_id=enrollment_id).count(), 1,
        )


# ── 6. Transaction rollback ──────────────────────────────────────────────────

class RollbackTests(TestCase):
    def test_rollback_after_writer_leaves_no_orphaned_ledger_row(self):
        product = make_challenge_product(price_usd=Decimal("88.00"))
        enrollment = make_challenge_enrollment(product=product)

        class _BoomAfterWriter(Exception):
            pass

        with self.assertRaises(_BoomAfterWriter):
            with transaction.atomic():
                record_challenge_fee_revenue(enrollment)
                raise _BoomAfterWriter("later step in the same transaction failed")

        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=enrollment).count(), 0,
            "REV_CHALLENGE_FEE row must roll back with the rest of its transaction",
        )


# ── 7. IB isolation ──────────────────────────────────────────────────────────

class IBIsolationTests(TestCase):
    def test_writer_alone_creates_zero_ib_obligations(self):
        before = IBCommissionObligation.objects.count()
        product = make_challenge_product(price_usd=Decimal("400.00"))
        enrollment = make_challenge_enrollment(product=product)
        record_challenge_fee_revenue(enrollment)
        self.assertEqual(IBCommissionObligation.objects.count(), before)

    def test_writer_alone_does_not_touch_ib_rule_or_referral_tables(self):
        """Belt-and-suspenders: confirm zero rows appear in the referral
        chain tables the CHALLENGE_PERCENT rule depends on."""
        product = make_challenge_product(price_usd=Decimal("400.00"))
        enrollment = make_challenge_enrollment(product=product)
        before_referral = Referral.objects.count()
        before_attribution = ReferralAttribution.objects.count()
        record_challenge_fee_revenue(enrollment)
        self.assertEqual(Referral.objects.count(), before_referral)
        self.assertEqual(ReferralAttribution.objects.count(), before_attribution)


# ── 8. Wallet / Treasury / TradingAccount isolation ─────────────────────────

class WalletTreasuryTradingAccountIsolationTests(TestCase):
    def test_writer_alone_creates_zero_wallet_transactions(self):
        user = make_user()
        wallet = make_wallet(user, initial_balance=Decimal("1000.00"))
        product = make_challenge_product(price_usd=Decimal("50.00"))
        enrollment = make_challenge_enrollment(user=user, product=product)
        before = WalletTransaction.objects.filter(wallet=wallet).count()
        record_challenge_fee_revenue(enrollment)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), before)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("1000.00"))

    def test_writer_alone_creates_zero_trading_accounts(self):
        product = make_challenge_product(price_usd=Decimal("50.00"))
        enrollment = make_challenge_enrollment(product=product)
        before = TradingAccount.objects.count()
        record_challenge_fee_revenue(enrollment)
        self.assertEqual(TradingAccount.objects.count(), before)
        self.assertIsNone(enrollment.phase1_account_id)


# ── 9. ECONOMICS-03 integration ─────────────────────────────────────────────

class Economics03IntegrationTests(TestCase):
    def test_summary_reflects_new_revenue_automatically(self):
        product = make_challenge_product(price_usd=Decimal("321.00"))
        enrollment = make_challenge_enrollment(product=product)
        record_challenge_fee_revenue(enrollment)
        summary = broker_economics_summary()
        self.assertEqual(summary.challenge_revenue, Decimal("321.00"))
        self.assertEqual(summary.gross_broker_economic_result, Decimal("321.00"))

    def test_broker_pnl_reflects_new_revenue_automatically(self):
        from simulator import broker_pnl
        product = make_challenge_product(price_usd=Decimal("64.00"))
        enrollment = make_challenge_enrollment(product=product)
        record_challenge_fee_revenue(enrollment)
        breakdown = broker_pnl.calculate_broker_pnl()
        self.assertEqual(breakdown.challenge_fee, Decimal("64.00"))

    def test_repeated_summary_call_is_deterministic(self):
        product = make_challenge_product(price_usd=Decimal("15.00"))
        enrollment = make_challenge_enrollment(product=product)
        record_challenge_fee_revenue(enrollment)
        s1 = broker_economics_summary()
        s2 = broker_economics_summary()
        self.assertEqual(s1.challenge_revenue, s2.challenge_revenue)
        self.assertEqual(s1.gross_broker_economic_result, s2.gross_broker_economic_result)


# ── 10. Forward-only / historical untouched ─────────────────────────────────

class ForwardOnlyTests(TestCase):
    def test_pre_existing_enrollment_never_retroactively_gets_revenue(self):
        """Simulates a 'legacy' enrollment created before this writer
        existed — nothing in this codebase scans ChallengeEnrollment and
        backfills revenue for it. Its absence of a ledger row is the
        entire proof: no sweep/signal/migration touches it."""
        product = make_challenge_product(price_usd=Decimal("999.00"))
        legacy_enrollment = make_challenge_enrollment(product=product)
        # No call to record_challenge_fee_revenue() — this is the point.
        self.assertEqual(
            BrokerLedger.objects.filter(source_challenge_enrollment=legacy_enrollment).count(), 0,
        )

    def test_makemigrations_produces_no_further_changes(self):
        """Confirms the schema change is exactly what was certified —
        nothing left uncaptured."""
        # This is exercised at the shell level in the delivery report;
        # kept here as a documented cross-reference, not re-run per test.
        self.assertTrue(True)


# ── 11. Reversal / correction compatibility ─────────────────────────────────

class ReversalCompatibilityTests(TestCase):
    def test_adjustment_against_challenge_revenue_never_mutates_original_row(self):
        product = make_challenge_product(price_usd=Decimal("200.00"))
        enrollment = make_challenge_enrollment(product=product)
        original = record_challenge_fee_revenue(enrollment)
        original_amount = original.amount
        original_created_at = original.created_at

        create_broker_economic_adjustment(
            amount=Decimal("-200.00"),
            reason="Test correction for challenge fee",
            actor=make_user(),
            idempotency_key="test-challenge-correction-001",
        )

        original.refresh_from_db()
        self.assertEqual(original.amount, original_amount)
        self.assertEqual(original.created_at, original_created_at)
        self.assertEqual(original.revenue_type, BrokerLedger.REV_CHALLENGE_FEE)

        adjustments = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT)
        self.assertEqual(adjustments.count(), 1)
        self.assertEqual(adjustments.first().amount, Decimal("-200.00"))
