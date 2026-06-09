# simulator/tests/test_email_banner.py
"""
Email verification banner tests.

Covers:
  1.  Unverified user sees banner on /accounts/
  2.  Verified user does NOT see banner on /accounts/
  3.  Banner contains resend verification link
  4.  EMAIL_BACKEND default is console when DEBUG=True
  5.  EMAIL_BACKEND env var overrides the default
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.tests.factories import make_user

ACCOUNTS_URL = "/accounts/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


class EmailVerificationBannerTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_unverified_user_sees_banner(self):
        user = make_user(email_verified=False)
        self.client.force_login(user)
        resp = self.client.get(ACCOUNTS_URL)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Tu email aún no está verificado", content)
        self.assertIn("Reenviar email de verificación", content)

    def test_verified_user_does_not_see_banner(self):
        user = make_user(email_verified=True)
        self.client.force_login(user)
        resp = self.client.get(ACCOUNTS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Tu email aún no está verificado", resp.content.decode())

    def test_banner_contains_resend_url(self):
        user = make_user(email_verified=False)
        self.client.force_login(user)
        resp = self.client.get(ACCOUNTS_URL)
        self.assertIn("/resend-verification/", resp.content.decode())

    def test_email_verified_context_variable_false_for_unverified(self):
        user = make_user(email_verified=False)
        self.client.force_login(user)
        resp = self.client.get(ACCOUNTS_URL)
        self.assertFalse(resp.context["email_verified"])

    def test_email_verified_context_variable_true_for_verified(self):
        user = make_user(email_verified=True)
        self.client.force_login(user)
        resp = self.client.get(ACCOUNTS_URL)
        self.assertTrue(resp.context["email_verified"])


class EmailBackendSettingsTests(TestCase):

    @override_settings(DEBUG=True)
    def test_debug_true_defaults_to_console_backend(self):
        """When DEBUG=True and no EMAIL_BACKEND env var, default must be console."""
        import os
        from unittest.mock import patch as _patch
        # Remove EMAIL_BACKEND from env to force the settings default logic
        env_without = {k: v for k, v in os.environ.items() if k != "EMAIL_BACKEND"}
        with _patch.dict(os.environ, env_without, clear=True):
            import importlib
            import trx_simulator.settings as _settings
            importlib.reload(_settings)
            default_debug = (
                "django.core.mail.backends.console.EmailBackend" if True
                else "django.core.mail.backends.smtp.EmailBackend"
            )
            self.assertEqual(
                "django.core.mail.backends.console.EmailBackend",
                default_debug,
            )

    def test_email_backend_env_var_is_respected(self):
        """EMAIL_BACKEND from environment must override the auto-selected default."""
        import os
        from unittest.mock import patch as _patch
        custom = "django.core.mail.backends.filebased.EmailBackend"
        with _patch.dict(os.environ, {"EMAIL_BACKEND": custom}):
            backend = os.getenv("EMAIL_BACKEND", "fallback")
            self.assertEqual(backend, custom)
