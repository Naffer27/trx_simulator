# simulator/tests/test_settings_security.py
"""
Security assertions on runtime Django settings.

These tests run against the *active* settings (already loaded), so they
catch regressions where a bad value slips through the env → settings pipeline.
Subprocess tests isolate env changes that would require reimporting settings.

Covered assertions
------------------
- SECRET_KEY is present, non-empty, ≥40 chars, not hardcoded insecure fallback
- Missing SECRET_KEY raises ImproperlyConfigured outside test mode
- Test mode allows missing SECRET_KEY (uses fixed test-only value)
- HSTS defaults are safe (0 / False) — no HSTS active in local dev
- HSTS can be enabled via SECURE_HSTS_SECONDS env var
- SECURE_SSL_REDIRECT defaults False, can be enabled via env in non-DEBUG
- SECURE_SSL_REDIRECT stays False even if env=true when DEBUG=True
- Cookie secure settings are boolean
"""
import subprocess
import sys

from django.conf import settings
from django.test import TestCase


class SecretKeyTests(TestCase):
    def test_secret_key_is_set(self):
        self.assertTrue(settings.SECRET_KEY)

    def test_secret_key_not_hardcoded_insecure(self):
        self.assertNotIn(
            "django-insecure",
            settings.SECRET_KEY,
            "Hardcoded insecure SECRET_KEY fallback is active — "
            "set DJANGO_SECRET_KEY in your environment.",
        )

    def test_secret_key_minimum_length(self):
        self.assertGreaterEqual(
            len(settings.SECRET_KEY),
            40,
            "SECRET_KEY is too short — regenerate with get_random_secret_key().",
        )

    def test_secret_key_not_empty_string(self):
        self.assertNotEqual(settings.SECRET_KEY.strip(), "")


class SecretKeyMissingRaisesTest(TestCase):
    """
    Verifies that starting Django without DJANGO_SECRET_KEY raises
    ImproperlyConfigured when not running under `manage.py test`.
    Uses subprocess isolation to avoid polluting the live settings import.
    """

    def test_missing_secret_key_raises_improperly_configured(self):
        import os
        # Set DJANGO_SECRET_KEY="" to override what load_dotenv() would read from .env.
        # load_dotenv() does NOT override existing env vars, so an explicit empty string
        # exercises the "key not set" branch without needing to delete the .env file.
        env = dict(os.environ)
        env["DJANGO_SECRET_KEY"] = ""
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                # Simulate a non-test invocation (sys.argv[1] != 'test')
                "import sys; sys.argv = ['manage.py', 'runserver']; "
                "from django.conf import settings; _ = settings.SECRET_KEY",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "Expected non-zero exit when DJANGO_SECRET_KEY is missing outside tests.",
        )
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_test_mode_allows_missing_secret_key(self):
        """manage.py test without DJANGO_SECRET_KEY must NOT crash."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"}
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                # Simulate test invocation (sys.argv[1] == 'test')
                "import sys; sys.argv = ['manage.py', 'test']; "
                "from django.conf import settings; "
                "assert settings.SECRET_KEY, 'SECRET_KEY empty in test mode'",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test should not crash when DJANGO_SECRET_KEY is unset.\n"
            f"stderr: {result.stderr}",
        )


class CookieSecurityTests(TestCase):
    # Django forces DEBUG=False during test runs, but SESSION_COOKIE_SECURE
    # and CSRF_COOKIE_SECURE are evaluated at settings import time using the
    # DEBUG value from the environment (True in dev). We can't compare them to
    # `not settings.DEBUG` here because those are different evaluation moments.
    # Instead we assert that the settings exist and are boolean.

    def test_session_cookie_secure_is_boolean(self):
        self.assertIsInstance(settings.SESSION_COOKIE_SECURE, bool)

    def test_csrf_cookie_secure_is_boolean(self):
        self.assertIsInstance(settings.CSRF_COOKIE_SECURE, bool)


# ── HSTS defaults (safe in local dev) ────────────────────────────────────────

class HSTSDefaultTests(TestCase):
    """
    HSTS settings must default to safe values so local dev is never broken
    and production admins must explicitly opt in.
    """

    def test_hsts_seconds_default_is_zero(self):
        # Zero = HSTS header not sent. Safe default; must be set explicitly in prod.
        self.assertEqual(
            settings.SECURE_HSTS_SECONDS, 0,
            "SECURE_HSTS_SECONDS must default to 0 — enabling it prematurely "
            "locks users out of HTTP.",
        )

    def test_hsts_include_subdomains_default_false(self):
        self.assertFalse(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)

    def test_hsts_preload_default_false(self):
        self.assertFalse(settings.SECURE_HSTS_PRELOAD)

    def test_ssl_redirect_default_false(self):
        self.assertFalse(settings.SECURE_SSL_REDIRECT)


class HSTSEnvConfigTests(TestCase):
    """
    HSTS and SSL redirect can be toggled via env vars.
    Uses subprocess isolation so settings are re-imported with custom env.
    """

    _BASE_ENV_SCRIPT = (
        "import sys, os; "
        "sys.argv = ['manage.py', 'test']; "
        "{extra_env}"
        "from django.conf import settings; "
        "{assertion}"
    )

    def _run(self, extra_env: dict, assertion: str):
        import os
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = "subprocess-test-key-not-for-production"
        env.update(extra_env)
        script = (
            "import sys; sys.argv = ['manage.py', 'test']; "
            "from django.conf import settings; " + assertion
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_hsts_seconds_can_be_set_via_env(self):
        result = self._run(
            {"SECURE_HSTS_SECONDS": "31536000"},
            "assert settings.SECURE_HSTS_SECONDS == 31536000, "
            f"repr(settings.SECURE_HSTS_SECONDS)",
        )
        self.assertEqual(
            result.returncode, 0,
            f"SECURE_HSTS_SECONDS=31536000 should be loaded.\nstderr: {result.stderr}",
        )

    def test_hsts_include_subdomains_can_be_enabled(self):
        result = self._run(
            {"SECURE_HSTS_INCLUDE_SUBDOMAINS": "true"},
            "assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is True, "
            "repr(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hsts_preload_can_be_enabled(self):
        result = self._run(
            {"SECURE_HSTS_PRELOAD": "true"},
            "assert settings.SECURE_HSTS_PRELOAD is True, "
            "repr(settings.SECURE_HSTS_PRELOAD)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssl_redirect_enabled_when_debug_false_and_env_true(self):
        result = self._run(
            {"DEBUG": "False", "SECURE_SSL_REDIRECT": "true"},
            "assert settings.SECURE_SSL_REDIRECT is True, "
            "repr(settings.SECURE_SSL_REDIRECT)",
        )
        self.assertEqual(
            result.returncode, 0,
            f"SECURE_SSL_REDIRECT should be True when DEBUG=False and env=true.\n"
            f"stderr: {result.stderr}",
        )

    def test_ssl_redirect_stays_false_in_debug_even_if_env_true(self):
        result = self._run(
            {"DEBUG": "True", "SECURE_SSL_REDIRECT": "true"},
            "assert settings.SECURE_SSL_REDIRECT is False, "
            "repr(settings.SECURE_SSL_REDIRECT)",
        )
        self.assertEqual(
            result.returncode, 0,
            f"SECURE_SSL_REDIRECT must be False when DEBUG=True regardless of env.\n"
            f"stderr: {result.stderr}",
        )
