# simulator/tests/test_register_csrf.py
"""
CSRF protection on /register/ — regression tests.

After removing @csrf_exempt from register_view, these tests confirm:
  1. GET /register/ loads (200).
  2. POST without CSRF token is rejected (403) when enforcement is on.
  3. POST with CSRF token creates the user and all expected objects.
  4. The register template contains {% csrf_token %}.
  5. Successful registration creates User, Wallet, EmailVerification.
  6. Successful registration does NOT create TradingAccount, Purchase,
     or ChallengeEnrollment.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from simulator.models import (
    ChallengeEnrollment, EmailVerification, Purchase, TradingAccount, Wallet,
)

User = get_user_model()

REG_URL = "/register/"

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")

_VALID_DATA = {
    "username":  "csrfreg_user",
    "email":     "csrfreg@example.com",
    "password1": "CsrfTest!Pass1",
    "password2": "CsrfTest!Pass1",
}


class RegisterGetTest(TestCase):
    def test_get_loads(self):
        resp = self.client.get(REG_URL)
        self.assertEqual(resp.status_code, 200)


class RegisterCsrfEnforcementTest(TestCase):
    """POST without a CSRF token must be rejected with 403."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()
        # enforce_csrf_checks=True bypasses Django's test-mode CSRF bypass
        self.csrf_client = Client(enforce_csrf_checks=True)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def test_post_without_csrf_token_returns_403(self):
        resp = self.csrf_client.post(REG_URL, _VALID_DATA)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(User.objects.filter(username="csrfreg_user").exists())


class RegisterCsrfTemplateTest(TestCase):
    """The register template must include the csrf_token tag."""

    def test_register_template_contains_csrf_token(self):
        resp = self.client.get(REG_URL)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # Django renders {% csrf_token %} as a hidden input named csrfmiddlewaretoken
        self.assertIn("csrfmiddlewaretoken", content)


class RegisterSuccessTest(TestCase):
    """POST with valid data (CSRF handled by test client) must create all expected objects."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def _register(self):
        return self.client.post(REG_URL, _VALID_DATA)

    def test_register_creates_user(self):
        self._register()
        self.assertTrue(User.objects.filter(username="csrfreg_user").exists())

    def test_register_creates_wallet(self):
        self._register()
        user = User.objects.get(username="csrfreg_user")
        self.assertTrue(Wallet.objects.filter(user=user).exists())

    def test_register_creates_email_verification(self):
        self._register()
        user = User.objects.get(username="csrfreg_user")
        ev = EmailVerification.objects.get(user=user)
        self.assertFalse(ev.verified, "New registrations must have unverified email")

    def test_register_does_not_create_trading_account(self):
        self._register()
        user = User.objects.get(username="csrfreg_user")
        self.assertEqual(TradingAccount.objects.filter(user=user).count(), 0)

    def test_register_does_not_create_purchase(self):
        self._register()
        user = User.objects.get(username="csrfreg_user")
        self.assertEqual(Purchase.objects.filter(user=user).count(), 0)

    def test_register_does_not_create_challenge_enrollment(self):
        self._register()
        user = User.objects.get(username="csrfreg_user")
        self.assertEqual(ChallengeEnrollment.objects.filter(user=user).count(), 0)

    def test_register_redirects_after_success(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/login/", resp["Location"])
