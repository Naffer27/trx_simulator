# simulator/tests/test_o4c4_totp_encryption_key_production_guard.py
"""
Microbloque O.4c-4 — TOTP_ENCRYPTION_KEY Production Guard.

Covers the guard added to trx_simulator/settings.py that refuses to
boot when APP_ENV is "staging" or "production" and TOTP_ENCRYPTION_KEY
is missing, empty, or not a valid Fernet key (including the literal
CHANGE_ME_FERNET_KEY placeholder from deploy/.env.staging.template).
APP_ENV (not DEBUG) is the sole source of truth — same discipline as
every other O.4c guard.

Same subprocess-isolation technique as test_o4c1_app_env_sqlite_guard.py
and test_o4c2_debug_production_guard.py — a settings.py guard runs once
at module-import time and cannot be exercised via override_settings().

Deliberately does NOT touch simulator/two_factor.py — _encrypt_secret()/
_decrypt_secret()/_get_fernet() are exercised here exactly as they
already exist, to prove the guard changes nothing about how b64:/
legacy-unprefixed/fernet: secrets are read, only whether the PROCESS
is allowed to start at all in staging/production.
"""
import base64
import os
import subprocess
import sys

import pyotp
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from simulator.two_factor import _decrypt_secret, _encrypt_secret, verify_totp_code

_TEST_SECRET = "subprocess-totpkeyguard-test-key-not-for-production"
_VALID_FERNET_KEY = Fernet.generate_key().decode()


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    # Confirmed the real .env/shell has no TOTP_ENCRYPTION_KEY set, but
    # never rely on that implicitly — always start from a clean slate so
    # "absent" tests are genuinely absent regardless of the environment
    # this test suite happens to run in.
    env.pop("TOTP_ENCRYPTION_KEY", None)
    # Keep the other production guards satisfied by default so this file
    # only ever exercises the TOTP_ENCRYPTION_KEY guard, never an
    # unrelated one. Direct assignment, NOT .setdefault(): this
    # project's own local .env has DEBUG=True set for real dev use —
    # .setdefault() would silently no-op against that inherited value
    # (confirmed empirically while writing this file, the same failure
    # mode already documented for python-dotenv in O.4c-1's own history).
    # Assigning here, BEFORE env.update(extra_env) below, still lets any
    # test's explicit extra_env override these defaults correctly.
    env["EMAIL_HOST"] = "smtp.example.com"
    env["NOWPAYMENTS_IPN_SECRET"] = "unit-test-ipn-secret"
    env["DB_NAME"] = "trx_test_db"
    env["DEBUG"] = "False"
    # O.4c-5 — keeps "boots clean" cases here from tripping the newer,
    # unrelated REDIS_URL guard (added after this file). Direct
    # assignment: this project's real .env has a real REDIS_URL set.
    env["REDIS_URL"] = "redis://127.0.0.1:6379/0"
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
# module on first ATTRIBUTE access. Every assertion here must force
# that access, or a subprocess would report success (rc=0) without the
# guard ever having run at all — confirmed empirically while writing
# this file (a bare "print('OK')" assertion produced false negatives
# for every "should fail" case).
_OK_ASSERTION = "settings.APP_ENV; print('OK')"


# ─────────────────────────────────────────────
# development — unaffected regardless of key
# ─────────────────────────────────────────────

class DevelopmentUnaffectedTests(TestCase):

    def test_development_with_key_absent_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_with_invalid_key_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "TOTP_ENCRYPTION_KEY": "not-a-valid-key"},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_with_placeholder_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "TOTP_ENCRYPTION_KEY": "CHANGE_ME_FERNET_KEY"},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# staging — guard active
# ─────────────────────────────────────────────

class StagingGuardTests(TestCase):

    def test_staging_key_absent_fails(self):
        result = _run({"APP_ENV": "staging"}, "runserver", _OK_ASSERTION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_staging_key_empty_fails(self):
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_staging_key_invalid_fails(self):
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": "not-a-valid-key"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid Fernet key", result.stderr)

    def test_staging_placeholder_fails(self):
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": "CHANGE_ME_FERNET_KEY"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid Fernet key", result.stderr)
        self.assertIn("do not use a placeholder", result.stderr)

    def test_staging_valid_key_boots(self):
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": _VALID_FERNET_KEY}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# production — guard active
# ─────────────────────────────────────────────

class ProductionGuardTests(TestCase):

    def test_production_key_absent_fails(self):
        result = _run({"APP_ENV": "production"}, "runserver", _OK_ASSERTION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_production_key_empty_fails(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_production_key_invalid_fails(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": "garbage"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid Fernet key", result.stderr)

    def test_production_placeholder_fails(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": "CHANGE_ME_FERNET_KEY"}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid Fernet key", result.stderr)

    def test_production_valid_key_boots(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": _VALID_FERNET_KEY}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# manage.py test — exemption
# ─────────────────────────────────────────────

class TestRunExemptionTests(TestCase):

    def test_staging_without_key_boots_under_test_argv(self):
        result = _run({"APP_ENV": "staging"}, "test", _OK_ASSERTION)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_without_key_boots_under_test_argv(self):
        result = _run({"APP_ENV": "production"}, "test", _OK_ASSERTION)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_with_placeholder_boots_under_test_argv(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": "CHANGE_ME_FERNET_KEY"}, "test", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Non-interference with pre-existing O.4c guards
# ─────────────────────────────────────────────

class NoInterferenceWithExistingGuardsTests(TestCase):

    def test_staging_debug_true_still_fails_on_debug_guard_not_totp(self):
        # Valid TOTP key present — must still fail, but on the O.4c-2
        # DEBUG guard, not a spurious TOTP failure.
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "TOTP_ENCRYPTION_KEY": _VALID_FERNET_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)
        self.assertNotIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_staging_no_db_name_still_fails_on_db_guard_not_totp(self):
        result = _run(
            {"APP_ENV": "staging", "DB_NAME": "", "TOTP_ENCRYPTION_KEY": _VALID_FERNET_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_NAME must be set", result.stderr)
        self.assertNotIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_development_still_allows_sqlite_and_debug_true(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Legacy secret compatibility — unaffected by the guard's existence
# ─────────────────────────────────────────────

class LegacySecretCompatibilityTests(TestCase):
    """These run in-process (APP_ENV=development by default here — the
    guard never fires), proving _encrypt_secret()/_decrypt_secret()/
    verify_totp_code() behave exactly as before for every historical
    format, completely untouched by this block."""

    def test_b64_historical_secret_still_decrypts_and_verifies(self):
        raw = pyotp.random_base32()
        stored = "b64:" + base64.b64encode(raw.encode()).decode()
        self.assertEqual(_decrypt_secret(stored), raw)
        code = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp_code(stored, code))

    def test_legacy_unprefixed_secret_still_verifies(self):
        raw = pyotp.random_base32()
        code = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp_code(raw, code))

    @override_settings(TOTP_ENCRYPTION_KEY="")
    def test_no_key_configured_still_falls_back_to_b64_on_encrypt(self):
        raw = pyotp.random_base32()
        stored = _encrypt_secret(raw)
        self.assertTrue(stored.startswith("b64:"))
        self.assertEqual(_decrypt_secret(stored), raw)

    @override_settings(TOTP_ENCRYPTION_KEY=_VALID_FERNET_KEY)
    def test_valid_key_still_produces_and_reads_fernet_prefixed_secret(self):
        raw = pyotp.random_base32()
        stored = _encrypt_secret(raw)
        self.assertTrue(stored.startswith("fernet:"))
        self.assertEqual(_decrypt_secret(stored), raw)
        code = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp_code(stored, code))

    @override_settings(TOTP_ENCRYPTION_KEY="CHANGE_ME_FERNET_KEY")
    def test_placeholder_key_at_runtime_falls_back_to_b64_like_before(self):
        # In-process behavior of two_factor.py itself is untouched by
        # O.4c-4 — an invalid key still degrades gracefully to b64: here
        # (the boot-time guard is what prevents this combination from
        # ever reaching a running staging/production process at all).
        raw = pyotp.random_base32()
        stored = _encrypt_secret(raw)
        self.assertTrue(stored.startswith("b64:"))


# ─────────────────────────────────────────────
# Guard does not touch the database
# ─────────────────────────────────────────────

class NoModelMutationTests(TestCase):

    def test_guard_source_has_no_model_or_database_references(self):
        import inspect
        import trx_simulator.settings as _settings_module
        source = inspect.getsource(_settings_module)
        # The TOTP_ENCRYPTION_KEY guard block itself — isolate it by its
        # unique marker comment to avoid false positives from unrelated
        # parts of the same (very long) settings.py file.
        start = source.index("O.4c-4 — Guard")
        end = source.index("TOTP_STAFF_REQUIRED = os.getenv")
        guard_source = source[start:end] if end > start else source[start:start + 2000]
        for forbidden in ("TOTPDevice", "objects.create", "objects.filter", ".save(", "import django.db"):
            self.assertNotIn(forbidden, guard_source, f"unexpected {forbidden!r} in guard source")
