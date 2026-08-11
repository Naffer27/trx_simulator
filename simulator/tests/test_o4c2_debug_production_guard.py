# simulator/tests/test_o4c2_debug_production_guard.py
"""
Microbloque O.4c-2 — DEBUG Production Guard.

Covers the guard added to trx_simulator/settings.py that refuses to
boot when APP_ENV is "staging" or "production" and DEBUG=True. APP_ENV
is the sole source of truth for this guard — DB_NAME and
SENTRY_ENVIRONMENT are never consulted (O.4c Fase 0 §6/§7).

Same subprocess-isolation technique as test_o4c1_app_env_sqlite_guard.py
and the project's pre-existing precedent (test_settings_email.py,
test_settings_security.py, test_nowpayments_secret.py) — a settings.py
guard runs once at module-import time and cannot be exercised via
override_settings().
"""
import os
import subprocess
import sys

from cryptography.fernet import Fernet
from django.test import TestCase

_TEST_SECRET = "subprocess-debugguard-test-key-not-for-production-x"
# O.4c-4 — see test_o4c1_app_env_sqlite_guard.py's identical constant
# for the full rationale: keeps "boots clean" staging/production
# subprocesses here from tripping the unrelated, newer
# TOTP_ENCRYPTION_KEY guard.
_VALID_TOTP_KEY = Fernet.generate_key().decode()
# O.4c-5 — same rationale, for the newer REDIS_URL guard. Direct
# assignment, NOT .setdefault(): this project's real .env has a real
# REDIS_URL set, which .setdefault() would silently fail to override.
_VALID_REDIS_URL = "redis://127.0.0.1:6379/0"


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    # Keep the other production guards satisfied by default so this file
    # only ever exercises the APP_ENV/DEBUG guard.
    env.setdefault("EMAIL_HOST", "smtp.example.com")
    env.setdefault("NOWPAYMENTS_IPN_SECRET", "unit-test-ipn-secret")
    env.setdefault("TOTP_ENCRYPTION_KEY", _VALID_TOTP_KEY)
    env["REDIS_URL"] = _VALID_REDIS_URL
    env.update(extra_env)
    script = (
        f"import sys; sys.argv = ['manage.py', '{argv1}']; "
        "from django.conf import settings; " + assertion
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )


# ─────────────────────────────────────────────
# development — DEBUG may be either value
# ─────────────────────────────────────────────

class DevelopmentDebugEitherValueTests(TestCase):

    def test_development_debug_true_allowed(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "assert settings.DEBUG is True",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_debug_false_allowed(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "assert settings.DEBUG is False",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# staging / production — DEBUG=True refused
# ─────────────────────────────────────────────

class StagingProductionDebugGuardTests(TestCase):

    def test_staging_debug_true_raises(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "DB_NAME": "trx_staging"}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("APP_ENV", result.stderr)
        self.assertIn("DEBUG", result.stderr)

    def test_production_debug_true_raises(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": "trx_prod"}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("APP_ENV", result.stderr)
        self.assertIn("DEBUG", result.stderr)

    def test_staging_debug_false_with_db_name_allowed(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging"}, "runserver",
            "assert settings.DEBUG is False and settings.APP_ENV == 'staging'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_debug_false_with_db_name_allowed(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod"}, "runserver",
            "assert settings.DEBUG is False and settings.APP_ENV == 'production'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_error_message_identifies_app_env_and_debug_for_vps_diagnosis(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": "trx_prod"}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0)
        # Exact values, not just the field names, so an operator reading
        # a VPS boot failure sees precisely what's misconfigured.
        self.assertIn("APP_ENV", result.stderr)
        self.assertIn("production", result.stderr)
        self.assertIn("DEBUG=True", result.stderr)


# ─────────────────────────────────────────────
# O.4c-1's SQLite guard must still fire independently of this one
# ─────────────────────────────────────────────

class SqliteGuardStillIndependentTests(TestCase):

    def test_staging_debug_false_no_db_name_still_blocked_by_o4c1(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_production_debug_false_no_db_name_still_blocked_by_o4c1(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_staging_debug_true_and_no_db_name_reports_debug_guard_first(self):
        # Both guards would fire — DEBUG is checked immediately after
        # APP_ENV validation, before the DATABASES block, so this
        # documents (and locks in) which message an operator actually
        # sees first when both are misconfigured simultaneously.
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)


# ─────────────────────────────────────────────
# APP_ENV invalid — still blocked (O.4c-1 contract unchanged)
# ─────────────────────────────────────────────

class AppEnvInvalidStillBlockedTests(TestCase):

    def test_invalid_app_env_still_raises_regardless_of_debug(self):
        result = _run(
            {"APP_ENV": "prod", "DEBUG": "True", "DB_NAME": "trx_prod"}, "runserver",
            "_ = settings.APP_ENV",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("APP_ENV", result.stderr)
        # Must fail on APP_ENV validation, not reach the DEBUG guard at all.
        self.assertNotIn("DEBUG=True is not allowed", result.stderr)


# ─────────────────────────────────────────────
# manage.py test compatibility
# ─────────────────────────────────────────────

class TestRunnerExceptionTests(TestCase):

    def test_staging_debug_true_does_not_raise_under_test(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "DB_NAME": "trx_staging"}, "test",
            "assert settings.DEBUG is True",
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test must bypass the APP_ENV/DEBUG guard.\nstderr: {result.stderr}",
        )

    def test_production_debug_true_does_not_raise_under_test(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": ""}, "test",
            "assert settings.DEBUG is True",
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test must bypass both O.4c guards.\nstderr: {result.stderr}",
        )

    def test_current_repo_test_invocation_unaffected(self):
        # In-process sanity check: this test running at all already
        # proves the real local suite (development, whatever DEBUG
        # happens to be) is unaffected by either O.4c guard.
        from django.conf import settings
        self.assertIn(settings.APP_ENV, {"development", "staging", "production"})


# ─────────────────────────────────────────────
# Existing guards — no regression
# ─────────────────────────────────────────────

class ExistingGuardsNoRegressionTests(TestCase):

    def test_secret_key_guard_still_raises_when_missing(self):
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = ""  # load_dotenv() never overrides a present var
        env["APP_ENV"] = "development"
        env["DEBUG"] = "True"
        env["DB_NAME"] = ""
        script = (
            "import sys; sys.argv = ['manage.py', 'runserver']; "
            "from django.conf import settings; _ = settings.SECRET_KEY"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_email_host_guard_still_raises_when_missing(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "False", "DB_NAME": "", "EMAIL_HOST": ""},
            "runserver", "_ = settings.EMAIL_HOST",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("EMAIL_HOST", result.stderr)

    def test_nowpayments_guard_still_raises_when_missing(self):
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "False", "DB_NAME": "",
                "NOWPAYMENTS_IPN_SECRET": "",
            },
            "runserver", "_ = settings.NOWPAYMENTS_IPN_SECRET",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("NOWPAYMENTS_IPN_SECRET", result.stderr)

    def test_email_and_nowpayments_guards_skipped_under_test(self):
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "True", "DB_NAME": "",
                "EMAIL_HOST": "", "NOWPAYMENTS_IPN_SECRET": "",
            },
            "test",
            "assert settings.EMAIL_HOST == '' and settings.NOWPAYMENTS_IPN_SECRET == ''",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
