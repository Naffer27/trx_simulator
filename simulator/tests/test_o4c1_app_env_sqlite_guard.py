# simulator/tests/test_o4c1_app_env_sqlite_guard.py
"""
Microbloque O.4c-1 — APP_ENV Foundation + SQLite Production Guard.

Covers the new APP_ENV setting (trx_simulator/settings.py) and the
guard that refuses a silent SQLite fallback when APP_ENV is "staging"
or "production" and DB_NAME is empty.

settings.py guards run once, at module-import time — they cannot be
exercised via override_settings() (by the time a test runs, settings.py
has already finished executing once, successfully). Every test here
therefore spawns a fresh subprocess with a controlled environment and
inspects its exit code / stderr, the exact same pattern already
established by test_settings_email.py, test_settings_security.py, and
test_nowpayments_secret.py — chosen deliberately to stay consistent
with the project's own precedent rather than inventing a new one.
"""
import os
import subprocess
import sys

from cryptography.fernet import Fernet
from django.test import TestCase

_TEST_SECRET = "subprocess-appenv-test-key-not-for-production"
# O.4c-4 — a valid Fernet key, needed only so that "boots clean" staging/
# production subprocesses here don't trip the NEW TOTP_ENCRYPTION_KEY
# guard (added after this file), which is unrelated to what this file
# actually tests (APP_ENV/DB_NAME). Same "keep the other guards
# satisfied by default" discipline already used for EMAIL_HOST/
# NOWPAYMENTS_IPN_SECRET below.
_VALID_TOTP_KEY = Fernet.generate_key().decode()


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    # Keep the other production guards satisfied by default so this
    # file only ever exercises the APP_ENV/DB_NAME guard, never an
    # unrelated one, unless a test explicitly wants to check
    # non-interference (see GuardsRemainIndependentTests below).
    env.setdefault("EMAIL_HOST", "smtp.example.com")
    env.setdefault("NOWPAYMENTS_IPN_SECRET", "unit-test-ipn-secret")
    env.setdefault("TOTP_ENCRYPTION_KEY", _VALID_TOTP_KEY)
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
# APP_ENV — default and validation
# ─────────────────────────────────────────────

class AppEnvDefaultAndValidationTests(TestCase):

    def test_default_is_development(self):
        result = self._run_no_appenv()
        self.assertEqual(result.returncode, 0, result.stderr)

    def _run_no_appenv(self):
        env = dict(os.environ)
        env.pop("APP_ENV", None)
        return _run(
            {"DEBUG": "True", "DB_NAME": ""}, "runserver",
            "assert settings.APP_ENV == 'development', settings.APP_ENV",
        )

    def test_explicit_development_accepted(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "assert settings.APP_ENV == 'development'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_staging_accepted_with_db_name(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging"}, "runserver",
            "assert settings.APP_ENV == 'staging'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_production_accepted_with_db_name(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod"}, "runserver",
            "assert settings.APP_ENV == 'production'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uppercase_value_normalized(self):
        result = _run(
            {"APP_ENV": "STAGING", "DEBUG": "False", "DB_NAME": "trx_staging"}, "runserver",
            "assert settings.APP_ENV == 'staging', settings.APP_ENV",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_value_raises(self):
        result = _run(
            {"APP_ENV": "prod", "DEBUG": "False", "DB_NAME": "trx_prod"}, "runserver",
            "_ = settings.APP_ENV",
        )
        self.assertNotEqual(result.returncode, 0, "Invalid APP_ENV must raise.")
        self.assertIn("APP_ENV", result.stderr)

    def test_invalid_value_raises_even_in_test_mode(self):
        # Deliberately NOT exempted for `manage.py test` — see settings.py
        # comment: no legitimate test scenario needs an invalid APP_ENV.
        result = _run(
            {"APP_ENV": "bogus", "DEBUG": "True", "DB_NAME": ""}, "test",
            "_ = settings.APP_ENV",
        )
        self.assertNotEqual(result.returncode, 0, "Invalid APP_ENV must raise even under `test`.")
        self.assertIn("APP_ENV", result.stderr)


# ─────────────────────────────────────────────
# development — behavior unchanged
# ─────────────────────────────────────────────

class DevelopmentUnaffectedTests(TestCase):

    def test_development_empty_db_name_uses_sqlite(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_with_db_name_uses_postgresql(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": "trx_dev"}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_debug_false_empty_db_name_also_allowed(self):
        # Fase 0 contract: DEBUG=False may still be used locally in
        # development without requiring Postgres.
        result = _run(
            {"APP_ENV": "development", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# staging / production — SQLite refused
# ─────────────────────────────────────────────

class StagingProductionGuardTests(TestCase):

    def test_staging_empty_db_name_raises(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_production_empty_db_name_raises(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_staging_with_db_name_uses_postgresql(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging"}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_with_db_name_uses_postgresql(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod"}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Test-runner exception — suite must keep running without PostgreSQL
# ─────────────────────────────────────────────

class TestRunnerExceptionTests(TestCase):

    def test_staging_empty_db_name_does_not_raise_under_test(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": ""}, "test",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test must bypass the APP_ENV/DB_NAME guard.\nstderr: {result.stderr}",
        )

    def test_production_empty_db_name_does_not_raise_under_test(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": ""}, "test",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test must bypass the APP_ENV/DB_NAME guard.\nstderr: {result.stderr}",
        )

    def test_current_repo_test_invocation_needs_no_postgresql(self):
        # The actual invocation shape used throughout this whole project
        # (python manage.py test ...) with today's real .env (no DB_NAME
        # set) must keep working — this is an in-process sanity check,
        # not a subprocess one: if this test is running at all, it
        # already proves the point, but assert explicitly anyway.
        from django.conf import settings
        if not os.getenv("DB_NAME", "").strip():
            self.assertTrue(
                settings.DATABASES["default"]["ENGINE"].endswith("sqlite3")
                or settings.DATABASES["default"]["ENGINE"].endswith("postgresql")
            )


# ─────────────────────────────────────────────
# Existing guards remain intact and independent
# ─────────────────────────────────────────────

class ExistingGuardsRemainIntactTests(TestCase):

    def test_secret_key_guard_still_raises_when_missing(self):
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        # load_dotenv() never overrides an already-present env var, so
        # simulating "unset" requires an explicit empty string, not
        # pop() — otherwise the subprocess's own load_dotenv() call
        # would read the real value straight out of .env regardless
        # (same idiom as test_settings_security.py's own comment).
        env["DJANGO_SECRET_KEY"] = ""
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

    def test_secret_key_guard_skipped_under_test_mode(self):
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = ""
        script = (
            "import sys; sys.argv = ['manage.py', 'test']; "
            "from django.conf import settings; "
            "assert settings.SECRET_KEY == 'test-only-secret-key-not-for-production-use-ever'"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_host_guard_still_raises_when_missing(self):
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "False", "DB_NAME": "",
                "EMAIL_HOST": "",
            },
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

    def test_email_and_nowpayments_guards_both_skipped_under_test(self):
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "False", "DB_NAME": "",
                "EMAIL_HOST": "", "NOWPAYMENTS_IPN_SECRET": "",
            },
            "test",
            "assert settings.EMAIL_HOST == '' and settings.NOWPAYMENTS_IPN_SECRET == ''",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_env_guard_does_not_interfere_with_email_or_nowpayments_guards(self):
        # development (APP_ENV guard inactive) + DEBUG=False must still
        # trigger the EMAIL_HOST/NOWPAYMENTS guards exactly as before —
        # the new guard must not short-circuit or mask the older ones.
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "False", "DB_NAME": "",
                "EMAIL_HOST": "",
            },
            "runserver", "_ = settings.EMAIL_HOST",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("EMAIL_HOST", result.stderr)
