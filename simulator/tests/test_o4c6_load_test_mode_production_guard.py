# simulator/tests/test_o4c6_load_test_mode_production_guard.py
"""
Microbloque O.4c-6 — LOAD_TEST_MODE Production Guard.

Covers the guard added to trx_simulator/settings.py that refuses to
boot when APP_ENV is "production" and LOAD_TEST_MODE=True.

Deliberately DIFFERENT shape from every other O.4c guard: gated on
APP_ENV == "production" ONLY, never "APP_ENV in {staging, production}".
MED-3 Fase 0 confirmed this is an intentional, evidence-based
deviation — simulator/ratelimit.py's own _load_test_mode() docstring
says "only valid in staging .env files", deploy/.env.staging.template
documents it as an expected staging workflow, and
load_tests/run_load_test.sh is designed to run against a staging-like
deployment. Staging is therefore deliberately EXEMPT from this guard —
LOAD_TEST_MODE=True in staging is a legitimate, already-documented
workflow, not a misconfiguration.

Same subprocess-isolation technique as test_o4c1_app_env_sqlite_guard.py
through test_o4c5_redis_url_production_guard.py — a settings.py guard
runs once at module-import time and cannot be exercised via
override_settings() (which is exactly how the 5 pre-existing
LOAD_TEST_MODE tests in this project already exercise rate_check()/
rate_peek()'s own runtime behavior — a completely different, unaffected
mechanism from this import-time guard).

Deliberately does NOT touch simulator/ratelimit.py — rate_check()/
rate_peek()/_load_test_mode()'s own semantics are exercised here
exactly as they already exist, unmodified.
"""
import os
import subprocess
import sys

from cryptography.fernet import Fernet
from django.test import TestCase

_TEST_SECRET = "subprocess-loadtestguard-test-key-not-for-production"
_VALID_TOTP_KEY = Fernet.generate_key().decode()
_VALID_REDIS_URL = "redis://127.0.0.1:6379/0"


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    # Direct assignment for every guard this file doesn't exercise —
    # NOT .setdefault(): this project's real .env has DEBUG=True and a
    # real REDIS_URL set, both of which .setdefault() would silently
    # fail to override (confirmed empirically in O.4c-4/O.4c-5's own
    # history). Keep the other production guards satisfied by default
    # so this file only ever exercises the LOAD_TEST_MODE guard.
    env["EMAIL_HOST"] = "smtp.example.com"
    env["NOWPAYMENTS_IPN_SECRET"] = "unit-test-ipn-secret"
    env["DB_NAME"] = "trx_test_db"
    env["DEBUG"] = "False"
    env["TOTP_ENCRYPTION_KEY"] = _VALID_TOTP_KEY
    env["REDIS_URL"] = _VALID_REDIS_URL
    env["LOAD_TEST_MODE"] = "False"
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


# Merely `from django.conf import settings` never triggers settings.py
# to actually execute — Django's LazySettings only imports the real
# module on first ATTRIBUTE access (confirmed empirically in O.4c-4's
# own history — a bare print('OK') produced false negatives).
_OK_ASSERTION = "settings.APP_ENV; print('OK')"


# ─────────────────────────────────────────────
# development — unaffected regardless of LOAD_TEST_MODE
# ─────────────────────────────────────────────

class DevelopmentUnaffectedTests(TestCase):

    def test_development_load_test_mode_true_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "LOAD_TEST_MODE": "True"},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_load_test_mode_false_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "LOAD_TEST_MODE": "False"},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# staging — deliberately EXEMPT (approved deviation)
# ─────────────────────────────────────────────

class StagingExemptTests(TestCase):

    def test_staging_load_test_mode_true_boots(self):
        result = _run(
            {"APP_ENV": "staging", "LOAD_TEST_MODE": "True"}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staging_load_test_mode_true_flag_is_actually_true(self):
        result = _run(
            {"APP_ENV": "staging", "LOAD_TEST_MODE": "True"}, "runserver",
            "assert settings.LOAD_TEST_MODE is True; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staging_load_test_mode_false_boots(self):
        result = _run(
            {"APP_ENV": "staging", "LOAD_TEST_MODE": "False"}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# production — guard active
# ─────────────────────────────────────────────

class ProductionGuardTests(TestCase):

    def test_production_load_test_mode_true_fails(self):
        result = _run(
            {"APP_ENV": "production", "LOAD_TEST_MODE": "True"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOAD_TEST_MODE", result.stderr)
        self.assertIn("production", result.stderr)

    def test_production_load_test_mode_false_boots(self):
        result = _run(
            {"APP_ENV": "production", "LOAD_TEST_MODE": "False"}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_load_test_mode_1_fails(self):
        # Confirms the same truthy-string parsing LOAD_TEST_MODE itself
        # already uses ("1"/"true"/"yes") is what the guard reacts to,
        # not a hardcoded literal "True" comparison.
        result = _run(
            {"APP_ENV": "production", "LOAD_TEST_MODE": "1"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOAD_TEST_MODE", result.stderr)


# ─────────────────────────────────────────────
# manage.py test / _IN_TEST_RUN exemption
# ─────────────────────────────────────────────

class TestRunExemptionTests(TestCase):

    def test_production_load_test_mode_true_boots_under_test_argv(self):
        result = _run(
            {"APP_ENV": "production", "LOAD_TEST_MODE": "True"}, "test", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Regression — other O.4c guards keep their exact contract, and don't
# fire spuriously when only LOAD_TEST_MODE is the issue (or vice versa)
# ─────────────────────────────────────────────

class NoInterferenceWithExistingGuardsTests(TestCase):

    def test_production_debug_true_still_fails_on_debug_guard_not_load_test_mode(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "LOAD_TEST_MODE": "False"},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)
        self.assertNotIn("LOAD_TEST_MODE", result.stderr)

    def test_production_no_db_name_still_fails_on_db_guard_not_load_test_mode(self):
        result = _run(
            {"APP_ENV": "production", "DB_NAME": "", "LOAD_TEST_MODE": "False"},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_NAME must be set", result.stderr)
        self.assertNotIn("LOAD_TEST_MODE", result.stderr)

    def test_production_no_redis_url_still_fails_on_redis_guard_not_load_test_mode(self):
        result = _run(
            {"APP_ENV": "production", "REDIS_URL": "", "LOAD_TEST_MODE": "False"},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL must be set", result.stderr)
        self.assertNotIn("LOAD_TEST_MODE", result.stderr)

    def test_production_no_totp_key_still_fails_on_totp_guard_not_load_test_mode(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": "", "LOAD_TEST_MODE": "False"},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)
        self.assertNotIn("LOAD_TEST_MODE", result.stderr)

    def test_load_test_mode_true_does_not_mask_earlier_guards(self):
        # DEBUG is checked earlier in settings.py than LOAD_TEST_MODE —
        # with both wrong, DEBUG's guard must fire, not LOAD_TEST_MODE's.
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "LOAD_TEST_MODE": "True"},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)

    def test_all_guards_satisfied_boots_completely_clean(self):
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod",
                "TOTP_ENCRYPTION_KEY": _VALID_TOTP_KEY, "REDIS_URL": _VALID_REDIS_URL,
                "LOAD_TEST_MODE": "False",
                "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'production' and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql') "
            "and settings.REDIS_URL == " + repr(_VALID_REDIS_URL) + " "
            "and settings.LOAD_TEST_MODE is False; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staging_all_guards_satisfied_with_load_test_mode_true_boots_clean(self):
        result = _run(
            {
                "APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging",
                "TOTP_ENCRYPTION_KEY": _VALID_TOTP_KEY, "REDIS_URL": _VALID_REDIS_URL,
                "LOAD_TEST_MODE": "True",
                "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'staging' and settings.LOAD_TEST_MODE is True; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_still_allows_sqlite_debug_true_and_load_test_mode(self):
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "True", "DB_NAME": "",
                "LOAD_TEST_MODE": "True",
            },
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# rate_check()/rate_peek() semantics unmodified (runtime, not import-time)
# ─────────────────────────────────────────────

class RateLimitSemanticsUnmodifiedTests(TestCase):
    """In-process (APP_ENV=development by default here — the import-time
    guard never fires), proving rate_check()/rate_peek()'s own
    LOAD_TEST_MODE bypass behavior is completely untouched by O.4c-6."""

    def test_rate_check_bypasses_when_load_test_mode_true(self):
        from django.test import override_settings
        from simulator.ratelimit import rate_check
        with override_settings(LOAD_TEST_MODE=True):
            allowed, count = rate_check("o4c6_test_key", limit=1, window=60)
        self.assertTrue(allowed)
        self.assertEqual(count, 0)

    def test_rate_peek_bypasses_when_load_test_mode_true(self):
        from django.test import override_settings
        from simulator.ratelimit import rate_check, rate_peek
        rate_check("o4c6_peek_key", limit=100, window=60)
        with override_settings(LOAD_TEST_MODE=True):
            self.assertEqual(rate_peek("o4c6_peek_key"), 0)
