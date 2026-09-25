# simulator/tests/test_payment_webhook_inbox.py
"""
BROKER-ECONOMICS-04C.3 (FASE B) — dedicated adversarial suite.

Covers the full FASE A matrix: durable evidence capture for every
signature-valid, JSON-parseable payment IPN, DB-enforced idempotency via
a content-based fingerprint (never payment_id alone), fail-open
behavior, and — critically — zero change to existing deposit-credit /
challenge-activation / Wallet behavior.
"""
import json
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase

from simulator.models import (
    ChallengeEnrollment, Deposit, PaymentWebhookEvent, Wallet, WalletTransaction,
)
from simulator.payment_webhook_inbox import capture_payment_webhook_event
from simulator.tests.factories import make_challenge_product, make_deposit, make_user, make_wallet

CALLBACK_URL = "/deposit/callback/"
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


def _ipn_with_fee(payment_id, payment_status, order_id="", amount="100.00"):
    body = json.loads(_ipn(payment_id, payment_status, order_id, amount))
    body["fee"] = {"currency": "btc", "depositFee": 0.0001, "withdrawalFee": 0, "serviceFee": 0.0002}
    body["outcome_amount"] = 0.0009
    body["outcome_currency"] = "btc"
    return json.dumps(body)


def _make_challenge_deposit(user, product, payment_id="cp_pay_001", credited=False, pay_amount=None):
    return Deposit.objects.create(
        user=user, amount_usd=product.price_usd, crypto_currency="btc",
        nowpayments_payment_id=payment_id, status="pending", credited=credited,
        challenge_product=product,
        pay_amount=pay_amount if pay_amount is not None else product.price_usd,
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


# ── 1. Capture service — direct unit tests ──────────────────────────────────

class CaptureServiceCoreTests(TestCase):
    def test_valid_payload_creates_exactly_one_row(self):
        payload = json.loads(_ipn("pid1", "finished", "1"))
        capture_payment_webhook_event(payload)
        self.assertEqual(PaymentWebhookEvent.objects.count(), 1)

    def test_fee_present_preserved_verbatim(self):
        payload = json.loads(_ipn_with_fee("pid2", "finished", "1"))
        row = capture_payment_webhook_event(payload)
        self.assertEqual(row.raw_payload["fee"], payload["fee"])
        self.assertEqual(row.raw_payload["fee"]["serviceFee"], 0.0002)

    def test_fee_absent_preserved_without_fabrication(self):
        payload = json.loads(_ipn("pid3", "finished", "1"))
        row = capture_payment_webhook_event(payload)
        self.assertNotIn("fee", row.raw_payload)
        self.assertNotIn("fee", payload)  # never added to the source dict either

    def test_provider_defaults_to_nowpayments(self):
        payload = json.loads(_ipn("pid4", "waiting", "1"))
        row = capture_payment_webhook_event(payload)
        self.assertEqual(row.provider, "nowpayments")

    def test_payment_id_order_id_status_recorded(self):
        payload = json.loads(_ipn("pid5", "confirming", "42"))
        row = capture_payment_webhook_event(payload)
        self.assertEqual(row.payment_id, "pid5")
        self.assertEqual(row.order_id, "42")
        self.assertEqual(row.payment_status, "confirming")

    def test_identical_replay_creates_exactly_one_row(self):
        payload = json.loads(_ipn("pid6", "finished", "1"))
        r1 = capture_payment_webhook_event(payload)
        r2 = capture_payment_webhook_event(payload)
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(PaymentWebhookEvent.objects.count(), 1)

    def test_genuine_status_transition_creates_distinct_rows(self):
        """Same payment_id, real lifecycle progression — two real events,
        not a duplicate."""
        p1 = json.loads(_ipn("pid7", "waiting", "1"))
        p2 = json.loads(_ipn("pid7", "finished", "1"))
        capture_payment_webhook_event(p1)
        capture_payment_webhook_event(p2)
        self.assertEqual(
            PaymentWebhookEvent.objects.filter(payment_id="pid7").count(), 2,
        )

    def test_fingerprint_is_not_payment_id_alone(self):
        """Two different payloads sharing the same payment_id must not
        collide on fingerprint — proven by the transition test above
        producing 2 rows, re-asserted here via explicit fingerprints."""
        p1 = json.loads(_ipn("pid8", "waiting", "1"))
        p2 = json.loads(_ipn("pid8", "finished", "1"))
        r1 = capture_payment_webhook_event(p1)
        r2 = capture_payment_webhook_event(p2)
        self.assertNotEqual(r1.event_fingerprint, r2.event_fingerprint)

    def test_direct_duplicate_fingerprint_write_raises_integrity_error(self):
        payload = json.loads(_ipn("pid9", "finished", "1"))
        row = capture_payment_webhook_event(payload)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentWebhookEvent.objects.create(
                    provider="nowpayments", event_fingerprint=row.event_fingerprint,
                    payment_id="pid9", order_id="1", payment_status="finished", raw_payload=payload,
                )

    def test_fail_open_returns_none_on_internal_error(self):
        """A capture-internal failure must never raise — it returns None."""
        with patch(
            "simulator.payment_webhook_inbox.PaymentWebhookEvent.objects.create",
            side_effect=Exception("simulated DB outage"),
        ):
            result = capture_payment_webhook_event(json.loads(_ipn("pid10", "finished", "1")))
        self.assertIsNone(result)
        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="pid10").count(), 0)


# ── 2. Concurrency — real DB race ───────────────────────────────────────────

class ConcurrencyTests(TransactionTestCase):
    def test_two_threads_identical_delivery_exactly_one_row(self):
        payload = json.loads(_ipn("pid_conc", "finished", "1"))
        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            return capture_payment_webhook_event(payload)

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_attempt, barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="pid_conc").count(), 1)


# ── 3. Real end-to-end via deposit_callback ─────────────────────────────────

class DepositCallbackIntegrationTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("0"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_valid_finished_ipn_captures_exactly_one_row(self, _sig):
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="dep_1", status="pending")
        body = _ipn("dep_1", "finished", deposit.pk, "100.00")
        r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="dep_1").count(), 1)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_invalid_signature_zero_rows(self, _sig_unused):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=False):
            body = _ipn("dep_bad_sig", "finished", "1", "100.00")
            r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="dep_bad_sig").count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_malformed_json_zero_rows(self, _sig):
        r = self.client.post(CALLBACK_URL, "{not valid json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PaymentWebhookEvent.objects.count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_unknown_payment_evidence_still_captured(self, _sig):
        body = _ipn("unrecognized_payment_id", "finished", "999999", "100.00")
        r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(
            PaymentWebhookEvent.objects.filter(payment_id="unrecognized_payment_id").count(), 1,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_webhook_delivery_exactly_one_row(self, _sig):
        deposit = make_deposit(self.user, amount_usd=Decimal("50.00"), payment_id="dep_dup", status="pending")
        body = _ipn("dep_dup", "finished", deposit.pk, "50.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="dep_dup").count(), 1)


# ── 4. Existing behavior unchanged ──────────────────────────────────────────

class ExistingBehaviorUnchangedTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.user = make_user()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_deposit_credit_amount_and_status_unchanged(self, _sig):
        wallet = make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("75.00"), payment_id="dep_credit", status="pending")
        body = _ipn("dep_credit", "finished", deposit.pk, "75.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        deposit.refresh_from_db()
        wallet.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertEqual(wallet.available_balance, Decimal("75.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT).count(), 1,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_challenge_activation_unchanged(self, _sig):
        product = make_challenge_product(price_usd=Decimal("199.00"))
        deposit = _make_challenge_deposit(self.user, product, "cp_ok_1")
        body = _ipn("cp_ok_1", "finished", deposit.pk, str(product.price_usd))
        r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_challenge_revenue_writer_still_fires_exactly_once(self, _sig):
        from simulator.models import BrokerLedger
        product = make_challenge_product(price_usd=Decimal("250.00"))
        deposit = _make_challenge_deposit(self.user, product, "cp_ok_2")
        body = _ipn("cp_ok_2", "finished", deposit.pk, str(product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("250.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_capture_creates_zero_broker_ledger_rows_itself(self, _sig):
        """Isolates the capture step's own effect — a plain wallet
        deposit IPN (no challenge involved) must create zero BrokerLedger
        rows of any kind."""
        from simulator.models import BrokerLedger
        make_wallet(self.user, initial_balance=Decimal("0"))
        deposit = make_deposit(self.user, amount_usd=Decimal("10.00"), payment_id="dep_no_ledger", status="pending")
        body = _ipn("dep_no_ledger", "finished", deposit.pk, "10.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(BrokerLedger.objects.count(), 0)


# ── 5. Rollback survival ─────────────────────────────────────────────────────

class RollbackSurvivalTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_evidence_survives_downstream_crediting_failure(self, _sig):
        """Simulates a failure inside the existing atomic crediting block
        (after capture already ran) — the evidence row must remain."""
        user = make_user()
        deposit = make_deposit(user, amount_usd=Decimal("30.00"), payment_id="dep_rollback", status="pending")
        body = _ipn("dep_rollback", "finished", deposit.pk, "30.00")

        with patch(
            "simulator.views.credit_wallet", side_effect=Exception("simulated crediting failure"),
        ):
            with self.assertRaises(Exception):
                self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(PaymentWebhookEvent.objects.filter(payment_id="dep_rollback").count(), 1)
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)  # the downstream failure did roll back correctly
