# simulator/tests/test_o4c5_redis_url_production_guard.py
"""
Microbloque O.4c-5 — REDIS_URL Production Guard.

Covers the guard added to trx_simulator/settings.py that refuses to
boot when APP_ENV is "staging" or "production" and REDIS_URL is
missing or empty. APP_ENV (not DEBUG) is the sole source of truth —
same discipline as every other O.4c guard, and the same shape
specifically as O.4c-1's DB_NAME guard (presence-only, no format/
content validation — MED-3 Fase 0 §12 concluded format validation
isn't needed here: an invalid REDIS_URL fails loudly at connection
time, unlike TOTP_ENCRYPTION_KEY's silent insecure-fallback case).

Same subprocess-isolation technique as test_o4c1_app_env_sqlite_guard.py
and test_o4c4_totp_encryption_key_production_guard.py — a settings.py
guard runs once at module-import time and cannot be exercised via
override_settings().

Every test here sets REDIS_URL EXPLICITLY (never relies on .pop() to
simulate absence) — confirmed empirically while writing this file that
python-dotenv's load_dotenv() re-populates an env var that is absent
from the passed environment from the real .env file (this project's
real .env has a real REDIS_URL set), the exact same failure mode
already documented for DJANGO_SECRET_KEY/DEBUG in O.4c-1/O.4c-4's own
history. Setting REDIS_URL="" explicitly sidesteps this entirely and is
functionally identical to "absent" for this guard's `not REDIS_URL`
check.

Deliberately does NOT attempt any Redis connectivity in any test here —
a REDIS_URL pointing at an unreachable address must still boot cleanly
(see NoConnectivityAttemptTests below), proving the guard validates
configuration only.
"""
import os
import subprocess
import sys

from cryptography.fernet import Fernet
from django.test import TestCase

_TEST_SECRET = "subprocess-redisurlguard-test-key-not-for-production"
_VALID_TOTP_KEY = Fernet.generate_key().decode()
_VALID_REDIS_URL = "redis://127.0.0.1:6379/0"


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    # Direct assignment for every guard this file doesn't exercise —
    # NOT .setdefault()/.pop(): this project's real .env has DEBUG=True
    # and a real REDIS_URL set, both of which .setdefault() would
    # silently fail to override (confirmed empirically in O.4c-4's own
    # history). Keep the other production guards satisfied by default
    # so this file only ever exercises the REDIS_URL guard.
    env["EMAIL_HOST"] = "smtp.example.com"
    env["NOWPAYMENTS_IPN_SECRET"] = "unit-test-ipn-secret"
    env["DB_NAME"] = "trx_test_db"
    env["DEBUG"] = "False"
    env["TOTP_ENCRYPTION_KEY"] = _VALID_TOTP_KEY
    env["REDIS_URL"] = ""
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
# A, F — development unaffected regardless of REDIS_URL
# ─────────────────────────────────────────────

class DevelopmentUnaffectedTests(TestCase):

    def test_development_with_redis_url_absent_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_channel_layer_falls_back_to_in_memory(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": ""}, "runserver",
            "assert settings.CHANNEL_LAYERS['default']['BACKEND'] == "
            "'channels.layers.InMemoryChannelLayer'; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_with_redis_url_present_boots(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "REDIS_URL": _VALID_REDIS_URL},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_with_redis_url_uses_redis_channel_layer(self):
        result = _run(
            {"APP_ENV": "development", "DB_NAME": "", "REDIS_URL": _VALID_REDIS_URL},
            "runserver",
            "assert settings.CHANNEL_LAYERS['default']['BACKEND'] == "
            "'channels_redis.core.RedisChannelLayer'; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# B, D — staging
# ─────────────────────────────────────────────

class StagingGuardTests(TestCase):

    def test_staging_redis_url_absent_fails(self):
        result = _run({"APP_ENV": "staging"}, "runserver", _OK_ASSERTION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL", result.stderr)

    def test_staging_redis_url_empty_fails(self):
        result = _run(
            {"APP_ENV": "staging", "REDIS_URL": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL", result.stderr)

    def test_staging_redis_url_present_boots(self):
        result = _run(
            {"APP_ENV": "staging", "REDIS_URL": _VALID_REDIS_URL}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# C, E — production
# ─────────────────────────────────────────────

class ProductionGuardTests(TestCase):

    def test_production_redis_url_absent_fails(self):
        result = _run({"APP_ENV": "production"}, "runserver", _OK_ASSERTION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL", result.stderr)

    def test_production_redis_url_empty_fails(self):
        result = _run(
            {"APP_ENV": "production", "REDIS_URL": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL", result.stderr)

    def test_production_redis_url_present_boots(self):
        result = _run(
            {"APP_ENV": "production", "REDIS_URL": _VALID_REDIS_URL}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# G — manage.py test / _IN_TEST_RUN exemption
# ─────────────────────────────────────────────

class TestRunExemptionTests(TestCase):

    def test_staging_without_redis_url_boots_under_test_argv(self):
        result = _run({"APP_ENV": "staging"}, "test", _OK_ASSERTION)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_without_redis_url_boots_under_test_argv(self):
        result = _run({"APP_ENV": "production"}, "test", _OK_ASSERTION)
        self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# H — no connectivity attempt
# ─────────────────────────────────────────────

class NoConnectivityAttemptTests(TestCase):

    def test_unreachable_redis_url_still_boots_clean(self):
        # A syntactically valid but completely unreachable address —
        # if the guard (or anything else in settings.py) ever attempted
        # a real connection, this would hang or raise. It must not.
        result = _run(
            {"APP_ENV": "production", "REDIS_URL": "redis://10.255.255.1:9999/0"},
            "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_source_has_no_redis_client_or_network_calls(self):
        import inspect
        import trx_simulator.settings as _settings_module
        source = inspect.getsource(_settings_module)
        start = source.index("O.4c-5 — Guard")
        end = source.index("if REDIS_URL:")
        guard_source = source[start:end]
        for forbidden in ("redis.from_url", ".ping(", "socket.", "connect(", "import redis"):
            self.assertNotIn(forbidden, guard_source, f"unexpected {forbidden!r} in guard source")


# ─────────────────────────────────────────────
# I — regression: other O.4c guards keep their exact contract,
# and don't fire spuriously when only REDIS_URL is the issue
# ─────────────────────────────────────────────

class NoInterferenceWithExistingGuardsTests(TestCase):

    def test_staging_debug_true_still_fails_on_debug_guard_not_redis(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "REDIS_URL": _VALID_REDIS_URL},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)
        self.assertNotIn("REDIS_URL", result.stderr)

    def test_staging_no_db_name_still_fails_on_db_guard_not_redis(self):
        result = _run(
            {"APP_ENV": "staging", "DB_NAME": "", "REDIS_URL": _VALID_REDIS_URL},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_NAME must be set", result.stderr)
        self.assertNotIn("REDIS_URL", result.stderr)

    def test_staging_no_totp_key_still_fails_on_totp_guard_not_redis(self):
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": "", "REDIS_URL": _VALID_REDIS_URL},
            "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOTP_ENCRYPTION_KEY", result.stderr)

    def test_staging_missing_redis_and_totp_key_fails_on_redis_first(self):
        # REDIS_URL is checked earlier in settings.py (line ~243) than
        # TOTP_ENCRYPTION_KEY (line ~440+) — with both missing, the
        # REDIS_URL guard must be the one that actually fires.
        result = _run(
            {"APP_ENV": "staging", "TOTP_ENCRYPTION_KEY": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_URL", result.stderr)

    def test_all_guards_satisfied_boots_completely_clean(self):
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod",
                "TOTP_ENCRYPTION_KEY": _VALID_TOTP_KEY, "REDIS_URL": _VALID_REDIS_URL,
                "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'production' and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql') "
            "and settings.REDIS_URL == " + repr(_VALID_REDIS_URL) + "; print('OK')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_still_allows_sqlite_debug_true_and_no_redis(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver", _OK_ASSERTION,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
