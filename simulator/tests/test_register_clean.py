# simulator/tests/test_register_clean.py
"""
Register clean-up tests.

Normal /register/ must NOT create challenge objects.
External webhook /api/internal/challenge/activate/ must STILL create them.
"""
import hashlib
import hmac
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from simulator.models import (
    ChallengeEnrollment, EmailVerification, Purchase, TradingAccount, Wallet,
)
from simulator.tests.factories import make_challenge_product

User = get_user_model()

REG_URL      = "/register/"
WEBHOOK_URL  = "/api/internal/challenge/activate/"
TEST_SECRET  = "test-webhook-secret-32-bytes-long!!"

_VALID_REGISTER = {
    "username":  "cleanreg_user",
    "email":     "cleanreg@example.com",
    "password1": "StrongPass!77",
    "password2": "StrongPass!77",
}

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")


def _sign(payload: dict, secret: str = TEST_SECRET) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()


def _webhook_post(client, payload: dict):
    body = json.dumps(payload)
    return client.post(
        WEBHOOK_URL,
        body,
        content_type="application/json",
        HTTP_X_MONEYBROKER_SIGNATURE=_sign(payload),
    )


# ── 1. Normal register — must NOT create challenge objects ────────────────────

class RegisterDoesNotCreateChallengeObjects(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def _register(self):
        return self.client.post(REG_URL, _VALID_REGISTER)

    def test_register_creates_user(self):
        self._register()
        self.assertTrue(User.objects.filter(username="cleanreg_user").exists())

    def test_register_creates_email_verification(self):
        self._register()
        user = User.objects.get(username="cleanreg_user")
        self.assertTrue(EmailVerification.objects.filter(user=user).exists())

    def test_register_creates_wallet(self):
        self._register()
        user = User.objects.get(username="cleanreg_user")
        self.assertTrue(Wallet.objects.filter(user=user).exists())

    def test_register_does_not_create_trading_account(self):
        self._register()
        user = User.objects.get(username="cleanreg_user")
        self.assertEqual(
            TradingAccount.objects.filter(user=user).count(), 0,
            "Normal registration must not create any TradingAccount",
        )

    def test_register_does_not_create_purchase(self):
        self._register()
        user = User.objects.get(username="cleanreg_user")
        self.assertEqual(
            Purchase.objects.filter(user=user).count(), 0,
            "Normal registration must not create any Purchase (legacy CODE-xxx)",
        )

    def test_register_does_not_create_challenge_enrollment(self):
        self._register()
        user = User.objects.get(username="cleanreg_user")
        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=user).count(), 0,
            "Normal registration must not create any ChallengeEnrollment",
        )

    def test_register_redirects_to_accounts_not_login(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/login/", resp["Location"])


# ── 2. External webhook — must STILL create challenge objects ─────────────────

@override_settings(CHALLENGE_WEBHOOK_SECRET=TEST_SECRET)
class ExternalWebhookStillCreatesChallenge(TestCase):

    def setUp(self):
        _PATCH_EMAIL_ASYNC.start()
        self.product = make_challenge_product(external_code="challenge_10k_clean")

    def tearDown(self):
        _PATCH_EMAIL_ASYNC.stop()

    def _payload(self, event_id="clean_evt_001"):
        return {
            "event_id":               event_id,
            "email":                  "extuser@cleantest.com",
            "full_name":              "Clean Trader",
            "challenge_product_code": self.product.external_code,
            "payment_id":             "clean_pay_001",
        }

    def test_webhook_creates_user_if_new(self):
        _webhook_post(self.client, self._payload())
        self.assertTrue(
            User.objects.filter(email="extuser@cleantest.com").exists(),
            "Webhook must create a new user when email is unknown",
        )

    def test_webhook_creates_challenge_enrollment(self):
        _webhook_post(self.client, self._payload())
        user = User.objects.get(email="extuser@cleantest.com")
        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=user).count(), 1,
            "Webhook must create exactly one ChallengeEnrollment",
        )

    def test_webhook_creates_challenge_trading_account(self):
        _webhook_post(self.client, self._payload())
        user = User.objects.get(email="extuser@cleantest.com")
        enrollment = ChallengeEnrollment.objects.get(user=user)
        self.assertIsNotNone(
            enrollment.phase1_account_id,
            "Webhook enrollment must have a linked Phase 1 TradingAccount",
        )
        account = TradingAccount.objects.get(pk=enrollment.phase1_account_id)
        self.assertEqual(account.user, user)

    def test_webhook_returns_200_ok(self):
        resp = _webhook_post(self.client, self._payload())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
