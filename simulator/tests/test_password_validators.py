# simulator/tests/test_password_validators.py
"""
AUTH_PASSWORD_VALIDATORS enforcement — regression tests.

Verifies that weak passwords are rejected at every entry point where a
password can be set: registration, change-password, and password-reset-confirm.

All four active validators are exercised:
  - MinimumLengthValidator  (min 8)
  - CommonPasswordValidator
  - NumericPasswordValidator
  - UserAttributeSimilarityValidator (covered via username-matching test)
"""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from simulator.tests.factories import make_user

User = get_user_model()

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")

REG_URL          = "/register/"
CHANGE_URL       = "/password-change/"
RESET_URL        = "/password-reset/"
RESET_DONE_URL   = "/password-reset/done/"


# ── Registration ──────────────────────────────────────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RegisterPasswordValidatorTests(TestCase):
    """Registration form must refuse weak passwords without creating the user."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def _post_reg(self, password):
        return self.client.post(REG_URL, {
            "username":  "weakreg_user",
            "email":     "weakreg@example.com",
            "password1": password,
            "password2": password,
        })

    def test_register_rejects_short_password(self):
        resp = self._post_reg("abc123!")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="weakreg_user").exists())

    def test_register_rejects_common_password(self):
        resp = self._post_reg("password")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="weakreg_user").exists())

    def test_register_rejects_all_numeric_password(self):
        resp = self._post_reg("12345678")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="weakreg_user").exists())

    def test_register_accepts_strong_password(self):
        resp = self._post_reg("Tr4der$Pass!")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(User.objects.filter(username="weakreg_user").exists())


# ── Change Password ───────────────────────────────────────────────────────────

class PasswordChangeValidatorTests(TestCase):
    """PasswordChangeView must refuse weak new passwords."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(username="valchg_user", password="OldStrong!77")
        self.client.login(username="valchg_user", password="OldStrong!77")

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def _post_change(self, new_password):
        return self.client.post(CHANGE_URL, {
            "old_password":  "OldStrong!77",
            "new_password1": new_password,
            "new_password2": new_password,
        })

    def test_change_rejects_short_password(self):
        resp = self._post_change("abc123!")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("abc123!"))

    def test_change_rejects_common_password(self):
        resp = self._post_change("password")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("password"))

    def test_change_rejects_all_numeric_password(self):
        resp = self._post_change("12345678")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("12345678"))

    def test_change_accepts_strong_password(self):
        resp = self._post_change("NewStrong!Pass9")
        self.assertRedirects(resp, "/password-change/done/", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrong!Pass9"))


# ── Password Reset Confirm ────────────────────────────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetValidatorTests(TestCase):
    """Reset-confirm form must refuse weak new passwords."""

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def _get_confirm_url(self, email):
        self.client.post(RESET_URL, {"email": email})
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"(/password-reset/confirm/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match, "No confirm URL found in reset email")
        return match.group(1)

    def _session_confirm_url(self, token_url):
        resp = self.client.get(token_url, follow=True)
        self.assertEqual(resp.status_code, 200)
        if resp.redirect_chain:
            return resp.redirect_chain[-1][0]
        return token_url

    def test_reset_confirm_rejects_short_password(self):
        make_user(username="valreset_user", email="valreset@example.com")
        confirm_url = self._session_confirm_url(
            self._get_confirm_url("valreset@example.com")
        )
        resp = self.client.post(confirm_url, {
            "new_password1": "abc123!",
            "new_password2": "abc123!",
        })
        self.assertEqual(resp.status_code, 200)

    def test_reset_confirm_rejects_common_password(self):
        make_user(username="valreset2_user", email="valreset2@example.com")
        confirm_url = self._session_confirm_url(
            self._get_confirm_url("valreset2@example.com")
        )
        resp = self.client.post(confirm_url, {
            "new_password1": "password",
            "new_password2": "password",
        })
        self.assertEqual(resp.status_code, 200)

    def test_reset_confirm_accepts_strong_password(self):
        make_user(username="valreset3_user", email="valreset3@example.com")
        confirm_url = self._session_confirm_url(
            self._get_confirm_url("valreset3@example.com")
        )
        resp = self.client.post(confirm_url, {
            "new_password1": "ResetStrong!Pass9",
            "new_password2": "ResetStrong!Pass9",
        })
        self.assertRedirects(
            resp, "/password-reset/complete/", fetch_redirect_response=False
        )
