# simulator/tests/test_site_url_emails.py
"""
SITE_URL email link tests.

Verification emails must use settings.SITE_URL to build the verify link,
never request.build_absolute_uri() which would produce localhost/testserver URLs
that Gmail spam-filters.

Covers:
  1.  Registration email contains SITE_URL, not localhost/testserver
  2.  Registration email contains SITE_URL when overridden to production domain
  3.  Resend verification email contains SITE_URL
  4.  Registration email does not contain 127.0.0.1 or testserver in link
"""
from unittest.mock import patch, call

from django.test import TestCase, override_settings

from simulator.tests.factories import make_user

REG_URL    = "/register/"
RESEND_URL = "/resend-verification/"

PROD_SITE  = "https://moneybrokers.test"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_CELERY    = patch("simulator.tasks.send_email_async.delay")

_VALID_REGISTER = {
    "username":  "siteurl_user",
    "email":     "siteurl@example.com",
    "password1": "StrongPass!77",
    "password2": "StrongPass!77",
}


class RegistrationEmailSiteUrlTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @override_settings(SITE_URL=PROD_SITE)
    def test_registration_verification_email_uses_site_url(self):
        """Verification link must start with SITE_URL, not testserver."""
        with _PATCH_CELERY as mock_delay:
            self.client.post(REG_URL, _VALID_REGISTER)

        verify_calls = [
            c for c in mock_delay.call_args_list
            if "verifica" in c.kwargs.get("subject", "").lower()
        ]
        self.assertTrue(len(verify_calls) >= 1, "Verification email must be queued")

        body = verify_calls[0].kwargs["message"]
        self.assertIn(
            PROD_SITE,
            body,
            f"Verification link must contain SITE_URL '{PROD_SITE}'",
        )

    @override_settings(SITE_URL=PROD_SITE)
    def test_registration_email_does_not_contain_localhost(self):
        """Verification link must not contain localhost or 127.0.0.1."""
        with _PATCH_CELERY as mock_delay:
            self.client.post(REG_URL, _VALID_REGISTER)

        verify_calls = [
            c for c in mock_delay.call_args_list
            if "verifica" in c.kwargs.get("subject", "").lower()
        ]
        self.assertTrue(len(verify_calls) >= 1)
        body = verify_calls[0].kwargs["message"]

        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn("localhost", body)
        self.assertNotIn("testserver", body)

    @override_settings(SITE_URL="http://127.0.0.1:8000")
    def test_registration_email_uses_configured_site_url_in_dev(self):
        """Even in dev, the URL comes from SITE_URL setting, not build_absolute_uri."""
        with _PATCH_CELERY as mock_delay:
            self.client.post(REG_URL, _VALID_REGISTER)

        verify_calls = [
            c for c in mock_delay.call_args_list
            if "verifica" in c.kwargs.get("subject", "").lower()
        ]
        self.assertTrue(len(verify_calls) >= 1)
        body = verify_calls[0].kwargs["message"]

        # Must contain the configured SITE_URL (not testserver which build_absolute_uri would produce)
        self.assertIn("http://127.0.0.1:8000/verify-email/", body)
        self.assertNotIn("testserver", body)


class ResendVerificationEmailSiteUrlTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(email="resendtest@example.com", email_verified=False)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @override_settings(SITE_URL=PROD_SITE)
    def test_resend_verification_uses_site_url(self):
        """Resend verification view must also use SITE_URL."""
        with _PATCH_CELERY as mock_delay:
            self.client.post(RESEND_URL)

        verify_calls = [
            c for c in mock_delay.call_args_list
            if "verifica" in c.kwargs.get("subject", "").lower()
        ]
        self.assertTrue(len(verify_calls) >= 1, "Resend must queue a verification email")

        body = verify_calls[0].kwargs["message"]
        self.assertIn(PROD_SITE, body)
        self.assertNotIn("testserver", body)
        self.assertNotIn("127.0.0.1", body)
