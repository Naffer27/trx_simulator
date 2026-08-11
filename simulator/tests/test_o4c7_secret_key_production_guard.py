# simulator/tests/test_o4c7_secret_key_production_guard.py
"""
Microbloque O.4c-7 — DJANGO_SECRET_KEY Production Guard.

Covers the guard added to trx_simulator/settings.py that refuses to
boot when APP_ENV is "staging"/"production" and DJANGO_SECRET_KEY does
not meet Django's own SECRET_KEY security requirements (security.W009,
django/core/checks/security/base.py): len(key) < 50, or
len(set(key)) < 5, or key.startswith("django-insecure-").

This is a *second*, independent guard layered on top of the
pre-existing presence/empty check (settings.py L.24-36, unconditional
across every real environment, unmodified by O.4c-7). The new guard
only evaluates once SECRET_KEY is already known to be non-empty, and
only when APP_ENV in {"staging", "production"} — O.4c-7 Fase 0 §6/§7.
development is deliberately unaffected by the new guard so a .env
copied straight from .env.example (whose own placeholder is non-empty
but weak) keeps booting locally.

Same subprocess-isolation technique as test_o4c1_app_env_sqlite_guard.py
through test_o4c6_load_test_mode_production_guard.py — a settings.py
guard runs once at module-import time and cannot be exercised via
override_settings().
"""
import os
import subprocess
import sys

from cryptography.fernet import Fernet
from django.test import TestCase

# A key that satisfies the new guard: >=50 chars, >=5 unique chars,
# does not start with "django-insecure-".
_VALID_SECRET_KEY = "o4c7-valid-secret-key-for-subprocess-tests-abcdefghij"

# The exact placeholder shipped in deploy/.env.staging.template — confirmed
# in O.4c-7 Fase 0 to be 25 chars, well under the 50-char minimum.
_PLACEHOLDER_STAGING_TEMPLATE = "CHANGE_ME_50_RANDOM_CHARS"

# The exact placeholder shipped in .env.example — a second, independently
# discovered placeholder confirmed in O.4c-7 Fase 0 to also fail Django's
# own check (20 chars), proving the length/entropy algorithm generalizes
# instead of needing a blocklist of specific known strings.
_PLACEHOLDER_ENV_EXAMPLE = "your-secret-key-here"

# >=50 chars but starts with the startproject-generated insecure prefix.
_DJANGO_INSECURE_PREFIXED = (
    "django-insecure-zyxwvutsrqponmlkjihgfedcba0123456789"
)

# >=50 chars but only 4 unique characters — fails the uniqueness floor
# even though it clears the length floor.
_LOW_UNIQUE_CHARS_KEY = "a" * 46 + "bcda"

_VALID_TOTP_KEY = Fernet.generate_key().decode()
_VALID_REDIS_URL = "redis://127.0.0.1:6379/0"


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    # Direct assignment for every guard this file doesn't exercise —
    # NOT .setdefault(): this project's real .env has DEBUG=True and a
    # real REDIS_URL set, both of which .setdefault() would silently
    # fail to override (confirmed empirically in O.4c-4/O.4c-5's own
    # history). Keep the other production guards satisfied by default
    # so this file only ever exercises the SECRET_KEY guard, unless a
    # test explicitly overrides one to check non-interference.
    env["DJANGO_SECRET_KEY"] = _VALID_SECRET_KEY
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
# development — unaffected regardless of SECRET_KEY quality
# ─────────────────────────────────────────────

class DevelopmentUnaffectedTests(TestCase):

    def test_development_empty_secret_key_still_raises_existing_guard(self):
        # The pre-existing presence/empty guard (L.24-36) is unconditional —
        # O.4c-7 must not touch or relax it.
        result = _run(
            {"APP_ENV": "development", "DJANGO_SECRET_KEY": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertIn("is not set", result.stderr)

    def test_development_placeholder_secret_key_boots(self):
        result = _run(
            {"APP_ENV": "development", "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_env_example_placeholder_boots(self):
        result = _run(
            {"APP_ENV": "development", "DJANGO_SECRET_KEY": _PLACEHOLDER_ENV_EXAMPLE},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_valid_secret_key_boots(self):
        result = _run(
            {"APP_ENV": "development", "DJANGO_SECRET_KEY": _VALID_SECRET_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# staging — guard active
# ─────────────────────────────────────────────

class StagingGuardTests(TestCase):

    def test_staging_empty_secret_key_raises_existing_guard(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertIn("is not set", result.stderr)

    def test_staging_change_me_placeholder_fails(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertIn("minimum security requirements", result.stderr)
        self.assertIn("staging", result.stderr)

    def test_staging_valid_secret_key_boots(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": _VALID_SECRET_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# production — guard active
# ─────────────────────────────────────────────

class ProductionGuardTests(TestCase):

    def test_production_empty_secret_key_raises_existing_guard(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertIn("is not set", result.stderr)

    def test_production_env_example_placeholder_fails(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": _PLACEHOLDER_ENV_EXAMPLE},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertIn("minimum security requirements", result.stderr)
        self.assertIn("production", result.stderr)

    def test_production_valid_secret_key_boots(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": _VALID_SECRET_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Edge cases: django-insecure- prefix and low character uniqueness,
# both independently of raw length
# ─────────────────────────────────────────────

class WeakKeyEdgeCaseTests(TestCase):

    def test_staging_django_insecure_prefix_with_50_plus_chars_fails(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": _DJANGO_INSECURE_PREFIXED},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_production_django_insecure_prefix_with_50_plus_chars_fails(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": _DJANGO_INSECURE_PREFIXED},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_staging_low_unique_characters_with_50_plus_chars_fails(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": _LOW_UNIQUE_CHARS_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_production_low_unique_characters_with_50_plus_chars_fails(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": _LOW_UNIQUE_CHARS_KEY},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)


# ─────────────────────────────────────────────
# manage.py test / _IN_TEST_RUN exemption
# ─────────────────────────────────────────────

class TestRunExemptionTests(TestCase):

    def test_staging_weak_secret_key_boots_under_test_argv(self):
        result = _run(
            {"APP_ENV": "staging", "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE},
            "test", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_weak_secret_key_boots_under_test_argv(self):
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE},
            "test", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_empty_secret_key_boots_under_test_argv(self):
        # Empty + test argv falls back to the fixed test-only secret
        # (L.24-36) — itself 48 chars, two short of the new guard's
        # 50-char floor. The new guard's own _IN_TEST_RUN exemption
        # must still apply regardless, so this must boot clean.
        result = _run(
            {"APP_ENV": "production", "DJANGO_SECRET_KEY": ""},
            "test", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Interaction with the other O.4c guards — no interference either way
# ─────────────────────────────────────────────

class NoInterferenceWithExistingGuardsTests(TestCase):

    def test_production_debug_true_fires_before_secret_key_guard(self):
        # DEBUG's guard (O.4c-2) is defined earlier in settings.py than
        # the new SECRET_KEY guard (O.4c-7) — with both wrong, DEBUG's
        # guard must fire first.
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "True",
                "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE,
            },
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)

    def test_production_weak_secret_key_fires_before_db_name_guard(self):
        # The SECRET_KEY guard is defined earlier in settings.py than
        # DB_NAME's guard — with both wrong, SECRET_KEY's guard must
        # fire first.
        result = _run(
            {
                "APP_ENV": "production", "DB_NAME": "",
                "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE,
            },
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertNotIn("DB_NAME must be set", result.stderr)

    def test_production_valid_secret_key_still_fails_on_db_name_guard(self):
        result = _run(
            {"APP_ENV": "production", "DB_NAME": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_NAME must be set", result.stderr)

    def test_production_valid_secret_key_still_fails_on_redis_url_guard(self):
        result = _run(
            {"APP_ENV": "production", "REDIS_URL": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL must be set", result.stderr)

    def test_production_valid_secret_key_still_fails_on_totp_key_guard(self):
        result = _run(
            {"APP_ENV": "production", "TOTP_ENCRYPTION_KEY": ""},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_all_guards_satisfied_boots_completely_clean_in_production(self):
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod",
                "DJANGO_SECRET_KEY": _VALID_SECRET_KEY,
                "TOTP_ENCRYPTION_KEY": _VALID_TOTP_KEY, "REDIS_URL": _VALID_REDIS_URL,
                "LOAD_TEST_MODE": "False",
                "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'production' and settings.DEBUG is False "
            "and settings.SECRET_KEY == " + repr(_VALID_SECRET_KEY) + " "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql') "
            "and settings.REDIS_URL == " + repr(_VALID_REDIS_URL) + "; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_guards_satisfied_boots_completely_clean_in_staging(self):
        result = _run(
            {
                "APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging",
                "DJANGO_SECRET_KEY": _VALID_SECRET_KEY,
                "TOTP_ENCRYPTION_KEY": _VALID_TOTP_KEY, "REDIS_URL": _VALID_REDIS_URL,
                "LOAD_TEST_MODE": "True",
                "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'staging' and "
            "settings.SECRET_KEY == " + repr(_VALID_SECRET_KEY) + "; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_still_allows_weak_secret_key_debug_true_and_sqlite(self):
        result = _run(
            {
                "APP_ENV": "development", "DEBUG": "True", "DB_NAME": "",
                "DJANGO_SECRET_KEY": _PLACEHOLDER_STAGING_TEMPLATE,
                "LOAD_TEST_MODE": "True",
            },
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
