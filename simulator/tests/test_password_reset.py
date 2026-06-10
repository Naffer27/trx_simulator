# simulator/tests/test_password_reset.py
"""
Forgot Password flow — regression tests.

Covers:
  1.  Login template has a working link to password reset page
  2.  Password reset form page loads (GET)
  3.  Submitting a valid email creates an outbound email
  4.  Reset email contains the confirm link
  5.  Confirm page loads with valid uidb64/token
  6.  Confirm page sets new password (POST)
  7.  Login works with new password after reset
  8.  Submitting unknown email does NOT reveal it (same done page, no email sent)
  9.  Expired/invalid token shows "Link Expired" without setting password
  10. Normal login still works unchanged
"""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from simulator.tests.factories import make_user

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))

User = get_user_model()

PASSWORD_RESET_URL  = "/password-reset/"
PASSWORD_RESET_DONE = "/password-reset/done/"
LOGIN_URL           = "/login/"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ForgotPasswordLinkTests(TestCase):
    """The login page must have a real link pointing to the password reset form."""

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_login_has_forgot_password_link(self):
        resp = self.client.get(LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Forgot password", content)
        self.assertIn(PASSWORD_RESET_URL, content)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetFormTests(TestCase):
    """The /password-reset/ form page must load and process submissions."""

    def test_form_page_loads(self):
        resp = self.client.get(PASSWORD_RESET_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Reset Password")

    def test_submit_valid_email_sends_email(self):
        user = make_user(email="reset_me@example.com")
        resp = self.client.post(PASSWORD_RESET_URL, {"email": "reset_me@example.com"})
        self.assertRedirects(resp, PASSWORD_RESET_DONE, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_contains_reset_link(self):
        user = make_user(email="resetlink@example.com")
        self.client.post(PASSWORD_RESET_URL, {"email": "resetlink@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("/password-reset/confirm/", body)

    def test_unknown_email_shows_done_page_no_email(self):
        """Security: unknown email must show the same done page without revealing existence."""
        resp = self.client.post(PASSWORD_RESET_URL, {"email": "nobody@example.com"})
        self.assertRedirects(resp, PASSWORD_RESET_DONE, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

    def test_done_page_loads(self):
        resp = self.client.get(PASSWORD_RESET_DONE)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Check Your Email")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetConfirmTests(TestCase):
    """The confirm page must validate the token and allow setting a new password."""

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def _get_confirm_url(self, email):
        """Trigger a reset for *email* and extract the confirm URL from outbox."""
        self.client.post(PASSWORD_RESET_URL, {"email": email})
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        match = re.search(r"(/password-reset/confirm/[^\s]+)", body)
        self.assertIsNotNone(match, "No confirm URL found in email body")
        return match.group(1)

    def test_confirm_page_loads_with_valid_token(self):
        user = make_user(email="confirm_load@example.com")
        url = self._get_confirm_url("confirm_load@example.com")
        # Django redirects the initial GET to a session-based URL
        resp = self.client.get(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Set New Password")

    def test_confirm_sets_new_password(self):
        user = make_user(email="new_pass@example.com")
        url = self._get_confirm_url("new_pass@example.com")
        # Follow the initial redirect to get the session-backed URL
        resp = self.client.get(url, follow=True)
        confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url
        resp2 = self.client.post(confirm_url, {
            "new_password1": "NewSecure!Pass77",
            "new_password2": "NewSecure!Pass77",
        })
        self.assertRedirects(
            resp2,
            "/password-reset/complete/",
            fetch_redirect_response=False,
        )

    def test_login_works_with_new_password(self):
        user = make_user(username="pwreset_user", email="pwreset@example.com",
                         password="OldPass!123")
        url = self._get_confirm_url("pwreset@example.com")
        resp = self.client.get(url, follow=True)
        confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url
        self.client.post(confirm_url, {
            "new_password1": "BrandNew!Pass99",
            "new_password2": "BrandNew!Pass99",
        })
        # Old password must no longer work
        self.client.logout()
        resp_old = self.client.post(LOGIN_URL, {
            "username": "pwreset_user",
            "password": "OldPass!123",
        })
        self.assertNotIn("_auth_user_id", self.client.session)
        # New password must work
        resp_new = self.client.post(LOGIN_URL, {
            "username": "pwreset_user",
            "password": "BrandNew!Pass99",
        })
        self.assertEqual(resp_new.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_invalid_token_shows_link_expired(self):
        resp = self.client.get("/password-reset/confirm/bad-uid/bad-token/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Link Expired")

    def test_complete_page_loads(self):
        resp = self.client.get("/password-reset/complete/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Password Updated")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NormalLoginUnchangedTests(TestCase):
    """Password reset flow must not disturb normal username/password login."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(username="stable_user", email="stable@example.com",
                              password="StablePass!1")

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_login_still_works(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "stable_user",
            "password": "StablePass!1",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_email_still_works(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "stable@example.com",
            "password": "StablePass!1",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
