# simulator/tests/test_settings_email.py
"""
Email settings — regression tests.

Verifies:
  - DEBUG=True  → filebased email backend (checked against running settings)
  - DEBUG=False → smtp backend by default
  - DEBUG=False without EMAIL_HOST raises ImproperlyConfigured (not in test mode)
  - All email env vars are read correctly
  - DEFAULT_FROM_EMAIL and SERVER_EMAIL are configurable
  - dev_emails path is used for development

Uses subprocess isolation for any test that changes DEBUG or EMAIL_* env vars,
following the same pattern as test_settings_security.py.
"""
import subprocess
import sys

from django.conf import settings
from django.test import TestCase

_TEST_SECRET = "subprocess-email-test-key-not-for-production"


def _run(extra_env: dict, assertion: str) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
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


# ── In-process (current dev env: DEBUG=True) ─────────────────────────────────

class DevEmailBackendTests(TestCase):
    """Running environment (DEBUG=True) uses filebased backend."""

    def test_dev_uses_filebased_backend(self):
        if not settings.DEBUG:
            self.skipTest("Only relevant when DEBUG=True")
        self.assertIn(
            "filebased",
            settings.EMAIL_BACKEND,
            f"DEBUG=True must default to filebased backend, got: {settings.EMAIL_BACKEND}",
        )

    def test_dev_file_path_contains_dev_emails(self):
        if not settings.DEBUG:
            self.skipTest("Only relevant when DEBUG=True")
        self.assertIn(
            "dev_emails",
            settings.EMAIL_FILE_PATH,
            f"EMAIL_FILE_PATH must point to dev_emails/, got: {settings.EMAIL_FILE_PATH}",
        )

    def test_default_from_email_is_set(self):
        self.assertTrue(
            settings.DEFAULT_FROM_EMAIL,
            "DEFAULT_FROM_EMAIL must never be empty",
        )

    def test_server_email_is_set(self):
        self.assertTrue(
            settings.SERVER_EMAIL,
            "SERVER_EMAIL must never be empty",
        )


# ── Subprocess: production SMTP default ──────────────────────────────────────

class ProdSmtpDefaultTests(TestCase):
    """DEBUG=False with EMAIL_HOST set must use SMTP backend."""

    def test_prod_uses_smtp_backend_by_default(self):
        result = _run(
            {"DEBUG": "False", "EMAIL_HOST": "smtp.sendgrid.net", "EMAIL_HOST_USER": ""},
            "assert 'smtp' in settings.EMAIL_BACKEND.lower(), "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prod_email_backend_is_smtp_not_filebased(self):
        result = _run(
            {"DEBUG": "False", "EMAIL_HOST": "smtp.sendgrid.net", "EMAIL_HOST_USER": ""},
            "assert 'filebased' not in settings.EMAIL_BACKEND, "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prod_custom_backend_override(self):
        result = _run(
            {
                "DEBUG": "False",
                "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
                "EMAIL_HOST_USER": "",
            },
            "assert 'locmem' in settings.EMAIL_BACKEND, "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ── Subprocess: guard for missing EMAIL_HOST ──────────────────────────────────

class ProdMissingEmailHostTests(TestCase):
    """DEBUG=False + SMTP + no EMAIL_HOST must raise ImproperlyConfigured."""

    def test_missing_email_host_raises_outside_test_mode(self):
        import os
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
        env["DEBUG"]                  = "False"
        env["EMAIL_HOST"]             = ""
        # sys.argv[1] = 'runserver' — NOT test mode
        script = (
            "import sys; sys.argv = ['manage.py', 'runserver']; "
            "from django.conf import settings; _ = settings.EMAIL_BACKEND"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "Expected non-zero exit when EMAIL_HOST is missing and DEBUG=False outside tests.",
        )
        self.assertIn("EMAIL_HOST", result.stderr)

    def test_missing_email_host_does_not_raise_in_test_mode(self):
        """manage.py test must not be blocked by the email guard."""
        import os
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
        env["DEBUG"]                  = "False"
        env["EMAIL_HOST"]             = ""
        # sys.argv[1] = 'test' — test mode should bypass the guard
        script = (
            "import sys; sys.argv = ['manage.py', 'test']; "
            "from django.conf import settings; "
            "assert 'smtp' in settings.EMAIL_BACKEND.lower()"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Test mode must bypass the email guard.\nstderr: {result.stderr}",
        )

    def test_missing_email_host_with_custom_backend_does_not_raise(self):
        """Overriding to a non-SMTP backend suppresses the guard."""
        result = _run(
            {
                "DEBUG": "False",
                "EMAIL_HOST": "",
                "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
            },
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.locmem.EmailBackend'",
        )
        self.assertEqual(
            result.returncode, 0,
            f"Non-SMTP backend must not trigger the EMAIL_HOST guard.\nstderr: {result.stderr}",
        )


# ── Subprocess: individual env vars ──────────────────────────────────────────

class EmailEnvVarTests(TestCase):
    """Each EMAIL_* env var must be read and applied correctly."""

    def test_email_host_from_env(self):
        result = _run(
            {"EMAIL_HOST": "smtp.mailgun.org"},
            "assert settings.EMAIL_HOST == 'smtp.mailgun.org', "
            "repr(settings.EMAIL_HOST)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_port_from_env(self):
        result = _run(
            {"EMAIL_PORT": "465"},
            "assert settings.EMAIL_PORT == 465, repr(settings.EMAIL_PORT)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_use_tls_false_from_env(self):
        result = _run(
            {"EMAIL_USE_TLS": "False"},
            "assert settings.EMAIL_USE_TLS is False, repr(settings.EMAIL_USE_TLS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_use_ssl_true_from_env(self):
        result = _run(
            {"EMAIL_USE_SSL": "true"},
            "assert settings.EMAIL_USE_SSL is True, repr(settings.EMAIL_USE_SSL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_timeout_from_env(self):
        result = _run(
            {"EMAIL_TIMEOUT": "30"},
            "assert settings.EMAIL_TIMEOUT == 30, repr(settings.EMAIL_TIMEOUT)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_host_user_from_env(self):
        result = _run(
            {"EMAIL_HOST_USER": "apikey"},
            "assert settings.EMAIL_HOST_USER == 'apikey', "
            "repr(settings.EMAIL_HOST_USER)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_host_password_from_env(self):
        result = _run(
            {"EMAIL_HOST_PASSWORD": "secret-token-xyz"},
            "assert settings.EMAIL_HOST_PASSWORD == 'secret-token-xyz', "
            "repr(settings.EMAIL_HOST_PASSWORD)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_from_email_from_env(self):
        result = _run(
            {"DEFAULT_FROM_EMAIL": "hello@moneybroker.com"},
            "assert settings.DEFAULT_FROM_EMAIL == 'hello@moneybroker.com', "
            "repr(settings.DEFAULT_FROM_EMAIL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_email_from_env(self):
        result = _run(
            {"SERVER_EMAIL": "alerts@moneybroker.com"},
            "assert settings.SERVER_EMAIL == 'alerts@moneybroker.com', "
            "repr(settings.SERVER_EMAIL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_email_falls_back_to_default_from_email(self):
        """If SERVER_EMAIL is not set, it defaults to DEFAULT_FROM_EMAIL."""
        result = _run(
            {"DEFAULT_FROM_EMAIL": "ops@moneybroker.com", "SERVER_EMAIL": ""},
            "assert settings.SERVER_EMAIL == 'ops@moneybroker.com', "
            "repr(settings.SERVER_EMAIL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_from_email_fallback_when_not_set(self):
        """When DEFAULT_FROM_EMAIL is absent, it falls back to noreply@moneybrokers.app."""
        result = _run(
            {"DEFAULT_FROM_EMAIL": "", "EMAIL_HOST_USER": ""},
            "assert settings.DEFAULT_FROM_EMAIL == 'noreply@moneybrokers.app', "
            "repr(settings.DEFAULT_FROM_EMAIL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
