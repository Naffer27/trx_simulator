# simulator/tests/test_external_challenge_webhook.py
"""
Phase 4E — External Challenge Activation Webhook tests.

Endpoint: POST /api/internal/challenge/activate/
Security: HMAC-SHA256 via X-MoneyBroker-Signature.
Idempotency: external_event_id (strict) + external_payment_id (secondary).
"""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from simulator.models import ChallengeEnrollment, TradingAccount
from simulator.tests.factories import make_challenge_product

User = get_user_model()

ENDPOINT = "/api/internal/challenge/activate/"
TEST_SECRET = "test-webhook-secret-32-bytes-long!!"


def _sign(payload: dict, secret: str = TEST_SECRET) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _post(client, payload: dict, secret: str = TEST_SECRET, override_sig: str | None = None):
    body = json.dumps(payload)
    sig = override_sig if override_sig is not None else _sign(payload, secret)
    return client.post(
        ENDPOINT,
        body,
        content_type="application/json",
        HTTP_X_MONEYBROKER_SIGNATURE=sig,
    )


def _make_product(external_code="challenge_10k", **kwargs):
    return make_challenge_product(external_code=external_code, **kwargs)


def _valid_payload(product, event_id="evt_001", payment_id="pay_001",
                   email="trader@external.com", full_name="Jane Doe",
                   amount_paid=None):
    return {
        "event_id":              event_id,
        "email":                 email,
        "full_name":             full_name,
        "challenge_product_code": product.external_code,
        "payment_id":            payment_id,
        "amount_paid":           float(amount_paid or product.price_usd),
        "currency":              "USD",
        "paid_at":               "2026-06-04T10:00:00Z",
    }


# ── Security ──────────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class SignatureTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_valid_signature_returns_200(self):
        payload = _valid_payload(self.product)
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 200)

    def test_missing_signature_returns_401(self):
        payload = _valid_payload(self.product)
        body = json.dumps(payload)
        r = self.client.post(ENDPOINT, body, content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_wrong_signature_returns_401(self):
        payload = _valid_payload(self.product)
        r = _post(self.client, payload, override_sig="deadbeef" * 8)
        self.assertEqual(r.status_code, 401)

    def test_tampered_body_returns_401(self):
        payload = _valid_payload(self.product)
        sig = _sign(payload)
        payload["amount_paid"] = 0.01   # tamper after signing
        body = json.dumps(payload)
        r = self.client.post(
            ENDPOINT, body, content_type="application/json",
            HTTP_X_MONEYBROKER_SIGNATURE=sig,
        )
        self.assertEqual(r.status_code, 401)

    def test_unconfigured_secret_returns_401(self):
        with self.settings(CHALLENGE_WEBHOOK_SECRET=""):
            payload = _valid_payload(self.product)
            r = _post(self.client, payload)
            self.assertEqual(r.status_code, 401)

    def test_get_method_returns_405(self):
        r = self.client.get(ENDPOINT)
        self.assertEqual(r.status_code, 405)


# ── User creation ─────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class UserCreationTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_creates_user_when_not_exists(self):
        payload = _valid_payload(self.product, email="newtrader@ext.com", event_id="evt_u1")
        _post(self.client, payload)
        self.assertTrue(User.objects.filter(email="newtrader@ext.com").exists())

    def test_reuses_existing_user_by_email(self):
        existing = User.objects.create_user("existing_u", "trader@ext.com", "pass")
        payload  = _valid_payload(self.product, email="trader@ext.com", event_id="evt_u2")
        _post(self.client, payload)
        enrollment = ChallengeEnrollment.objects.get(external_event_id="evt_u2")
        self.assertEqual(enrollment.user_id, existing.pk)
        # No second user should be created
        self.assertEqual(User.objects.filter(email="trader@ext.com").count(), 1)

    def test_response_includes_user_created_flag(self):
        payload = _valid_payload(self.product, email="brand_new@ext.com", event_id="evt_u3")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        self.assertTrue(data["user_created"])

    def test_existing_user_user_created_false(self):
        User.objects.create_user("old_user", "old@ext.com", "pass")
        payload = _valid_payload(self.product, email="old@ext.com", event_id="evt_u4")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        self.assertFalse(data["user_created"])


# ── Enrollment creation ───────────────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class EnrollmentCreationTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_creates_enrollment(self):
        payload = _valid_payload(self.product, event_id="evt_e1")
        _post(self.client, payload)
        self.assertEqual(ChallengeEnrollment.objects.filter(external_event_id="evt_e1").count(), 1)

    def test_enrollment_has_correct_product(self):
        payload = _valid_payload(self.product, event_id="evt_e2")
        _post(self.client, payload)
        enrollment = ChallengeEnrollment.objects.get(external_event_id="evt_e2")
        self.assertEqual(enrollment.product_id, self.product.pk)

    def test_enrollment_stores_external_ids(self):
        payload = _valid_payload(self.product, event_id="evt_e3", payment_id="pay_e3")
        _post(self.client, payload)
        enrollment = ChallengeEnrollment.objects.get(external_event_id="evt_e3")
        self.assertEqual(enrollment.external_event_id, "evt_e3")
        self.assertEqual(enrollment.external_payment_id, "pay_e3")

    def test_response_contains_enrollment_id(self):
        payload = _valid_payload(self.product, event_id="evt_e4")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        self.assertIn("enrollment_id", data)
        self.assertTrue(ChallengeEnrollment.objects.filter(pk=data["enrollment_id"]).exists())


# ── Phase 1 account creation ──────────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class Phase1AccountTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_creates_phase1_account(self):
        payload = _valid_payload(self.product, event_id="evt_a1")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        self.assertIsNotNone(data["account_id"])
        account = TradingAccount.objects.get(pk=data["account_id"])
        self.assertEqual(account.account_type, "CHALLENGE")
        self.assertEqual(account.phase, "Fase 1")

    def test_account_is_active(self):
        payload = _valid_payload(self.product, event_id="evt_a2")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        account = TradingAccount.objects.get(pk=data["account_id"])
        self.assertEqual(account.status, TradingAccount.STATUS_ACTIVE)

    def test_response_contains_login_url(self):
        payload = _valid_payload(self.product, event_id="evt_a3")
        r = _post(self.client, payload)
        data = json.loads(r.content)
        self.assertIn("login_url", data)
        self.assertIn("/login/", data["login_url"])


# ── Idempotency ───────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class IdempotencyTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_duplicate_event_id_returns_idempotent(self):
        payload = _valid_payload(self.product, event_id="evt_idem1")
        _post(self.client, payload)
        r2 = _post(self.client, payload)
        data = json.loads(r2.content)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(data["idempotent"])

    def test_duplicate_event_id_does_not_create_second_enrollment(self):
        payload = _valid_payload(self.product, event_id="evt_idem2")
        _post(self.client, payload)
        _post(self.client, payload)
        self.assertEqual(
            ChallengeEnrollment.objects.filter(external_event_id="evt_idem2").count(), 1
        )

    def test_duplicate_payment_id_returns_idempotent(self):
        payload1 = _valid_payload(self.product, event_id="evt_idem3a", payment_id="pay_shared")
        payload2 = _valid_payload(self.product, event_id="evt_idem3b", payment_id="pay_shared")
        _post(self.client, payload1)
        r2 = _post(self.client, payload2)
        data = json.loads(r2.content)
        self.assertTrue(data["idempotent"])

    def test_duplicate_payment_id_does_not_create_second_enrollment(self):
        payload1 = _valid_payload(self.product, event_id="evt_idem4a", payment_id="pay_dup")
        payload2 = _valid_payload(self.product, event_id="evt_idem4b", payment_id="pay_dup")
        _post(self.client, payload1)
        _post(self.client, payload2)
        self.assertEqual(
            ChallengeEnrollment.objects.filter(external_payment_id="pay_dup").count(), 1
        )

    def test_idempotent_response_contains_enrollment_id(self):
        payload = _valid_payload(self.product, event_id="evt_idem5")
        r1 = _post(self.client, payload)
        r2 = _post(self.client, payload)
        d1 = json.loads(r1.content)
        d2 = json.loads(r2.content)
        self.assertEqual(d1["enrollment_id"], d2["enrollment_id"])


# ── Validation: product / amount ──────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class ValidationTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    def test_unknown_product_code_returns_400(self):
        payload = _valid_payload(self.product, event_id="evt_v1")
        payload["challenge_product_code"] = "nonexistent_code"
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)
        self.assertIn("not found", json.loads(r.content)["error"])

    def test_inactive_product_returns_400(self):
        inactive = _make_product(external_code="inactive_code", is_active=False)
        payload = _valid_payload(inactive, event_id="evt_v2")
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)

    def test_amount_below_price_returns_400(self):
        payload = _valid_payload(self.product, event_id="evt_v3")
        payload["amount_paid"] = float(self.product.price_usd) - 1.0
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)
        self.assertIn("less than", json.loads(r.content)["error"])

    def test_amount_equal_to_price_succeeds(self):
        payload = _valid_payload(self.product, event_id="evt_v4",
                                 amount_paid=float(self.product.price_usd))
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 200)

    def test_missing_email_returns_400(self):
        payload = _valid_payload(self.product, event_id="evt_v5")
        del payload["email"]
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)

    def test_missing_product_code_returns_400(self):
        payload = _valid_payload(self.product, event_id="evt_v6")
        del payload["challenge_product_code"]
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)

    def test_missing_event_id_returns_400(self):
        payload = _valid_payload(self.product)
        del payload["event_id"]
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 400)

    def test_amount_none_skips_amount_check(self):
        """amount_paid is optional — if omitted, no price check is done."""
        payload = _valid_payload(self.product, event_id="evt_v7")
        del payload["amount_paid"]
        r = _post(self.client, payload)
        self.assertEqual(r.status_code, 200)


# ── Rollback on activation failure ────────────────────────────────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class RollbackTests(TestCase):
    def setUp(self):
        self.product = _make_product()

    @patch("simulator.views._ce_activate", side_effect=RuntimeError("activate failed"))
    def test_activation_failure_creates_no_enrollment(self, _mock):
        payload = _valid_payload(self.product, event_id="evt_rb1")
        try:
            _post(self.client, payload)
        except Exception:
            pass
        self.assertEqual(
            ChallengeEnrollment.objects.filter(external_event_id="evt_rb1").count(), 0
        )

    @patch("simulator.views._ce_activate", side_effect=RuntimeError("activate failed"))
    def test_activation_failure_creates_no_phase1_account(self, _mock):
        payload = _valid_payload(self.product, event_id="evt_rb2",
                                 email="rb2@ext.com")
        try:
            _post(self.client, payload)
        except Exception:
            pass
        user = User.objects.filter(email="rb2@ext.com").first()
        if user:
            self.assertEqual(
                TradingAccount.objects.filter(user=user, account_type="CHALLENGE").count(), 0
            )
