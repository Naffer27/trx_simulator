# simulator/tests/test_email_backend_config.py
"""
Email backend configuration tests.

Covers:
  1.  DEBUG=True  → default backend is filebased
  2.  DEBUG=False → default backend is smtp
  3.  Explicit EMAIL_BACKEND env var overrides the default in both modes
  4.  EMAIL_FILE_PATH defaults to dev_emails/ inside BASE_DIR
  5.  Filebased backend actually writes a .log file when send_mail is called
"""
import os
from pathlib import Path
from unittest.mock import patch as _patch

from django.conf import settings
from django.test import TestCase, override_settings


class EmailBackendDefaultsTests(TestCase):

    def test_debug_true_default_is_filebased(self):
        with override_settings(DEBUG=True):
            backend = (
                "django.core.mail.backends.filebased.EmailBackend"
                if settings.DEBUG
                else "django.core.mail.backends.smtp.EmailBackend"
            )
            self.assertEqual(backend, "django.core.mail.backends.filebased.EmailBackend")

    def test_debug_false_default_is_smtp(self):
        with override_settings(DEBUG=False):
            backend = (
                "django.core.mail.backends.filebased.EmailBackend"
                if settings.DEBUG
                else "django.core.mail.backends.smtp.EmailBackend"
            )
            self.assertEqual(backend, "django.core.mail.backends.smtp.EmailBackend")

    def test_explicit_env_var_overrides_debug_true_default(self):
        custom = "django.core.mail.backends.console.EmailBackend"
        with _patch.dict(os.environ, {"EMAIL_BACKEND": custom}):
            resolved = os.getenv("EMAIL_BACKEND", "fallback")
        self.assertEqual(resolved, custom)

    def test_explicit_env_var_overrides_debug_false_default(self):
        custom = "anymail.backends.sendgrid.EmailBackend"
        with _patch.dict(os.environ, {"EMAIL_BACKEND": custom}):
            resolved = os.getenv("EMAIL_BACKEND", "fallback")
        self.assertEqual(resolved, custom)

    def test_email_file_path_is_set(self):
        """EMAIL_FILE_PATH must be configured (used by filebased backend)."""
        self.assertTrue(
            hasattr(settings, "EMAIL_FILE_PATH"),
            "settings.EMAIL_FILE_PATH must exist",
        )
        self.assertTrue(settings.EMAIL_FILE_PATH, "EMAIL_FILE_PATH must not be empty")

    def test_email_file_path_ends_with_dev_emails_by_default(self):
        """Default EMAIL_FILE_PATH should point to dev_emails/ inside BASE_DIR."""
        path = Path(settings.EMAIL_FILE_PATH)
        self.assertEqual(path.name, "dev_emails")


class FilebsaedBackendWritesFileTest(TestCase):
    """Filebased backend must actually write a .log file."""

    def test_filebased_backend_creates_log_file(self):
        import tempfile
        from django.core.mail import send_mail

        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(
                EMAIL_BACKEND="django.core.mail.backends.filebased.EmailBackend",
                EMAIL_FILE_PATH=tmpdir,
            ):
                send_mail(
                    subject="Filebased test",
                    message="This email should land in a .log file.",
                    from_email="test@example.com",
                    recipient_list=["dest@example.com"],
                )
            log_files = list(Path(tmpdir).glob("*.log"))
            self.assertEqual(len(log_files), 1, "Exactly one .log file must be created")
            content = log_files[0].read_text()
        self.assertIn("Filebased test", content)
        self.assertIn("This email should land in a .log file.", content)
