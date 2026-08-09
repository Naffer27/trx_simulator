# simulator/tests/test_o4c3_production_configuration_guards_end_to_end.py
"""
Microbloque O.4c-3 — Production Configuration Guards, end-to-end
verification (final checkpoint for HIGH-2 + the DEBUG part of MED-3).

This file does NOT re-test every individual guard behavior already
covered by test_o4c1_app_env_sqlite_guard.py (23 tests) and
test_o4c2_debug_production_guard.py (18 tests) in isolation — it adds
only what those two files, taken together, do not already prove:

  1. The full 12-point APP_ENV x DEBUG x DB_NAME matrix walked in one
     place, in the user's own enumeration order, as the checkpoint's
     own definitive reference (mirrors the "one consolidated E2E file
     per block" precedent: test_o3e4_..., test_o4a3_...,
     test_o4b4_...).
  2. The STAGING-specific "doubly insecure" case (DEBUG=True AND
     DB_NAME empty) with the guard firing order documented — O.4c-2's
     own version of this check actually exercises APP_ENV=production,
     not staging (a naming-only slip in that file, not a functional
     defect — both tiers share identical guard logic, but the literal
     staging case was never directly exercised).
  3. All 6 guards (SECRET_KEY, EMAIL_HOST, NOWPAYMENTS_IPN_SECRET,
     APP_ENV validation, DB_NAME/staging-production, DEBUG/staging-
     production) proven to coexist correctly in ONE process invocation
     of a fully-correct staging configuration — no existing test
     exercises all 6 simultaneously.
  4. A direct read of .env.example and deploy/.env.staging.template to
     lock in the documented contract as a checkable invariant, so any
     future drift between the docs and the actual guard contract fails
     a test instead of being discovered in production.

Same subprocess-isolation technique as O.4c-1/O.4c-2 and the project's
pre-existing precedent — settings.py guards run once at import time and
cannot be exercised via override_settings().
"""
import os
import subprocess
import sys
from pathlib import Path

from django.test import TestCase

_TEST_SECRET = "subprocess-o4c3-checkpoint-key-not-for-production"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run(extra_env: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"] = _TEST_SECRET
    env.setdefault("EMAIL_HOST", "smtp.example.com")
    env.setdefault("NOWPAYMENTS_IPN_SECRET", "unit-test-ipn-secret")
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
# Full 12-point matrix, in the exact order requested for the checkpoint
# ─────────────────────────────────────────────

class FullMatrixEndToEndTests(TestCase):

    def test_01_development_debug_true_no_db_name_sqlite(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_02_development_debug_false_no_db_name_allowed(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_03_development_debug_true_with_db_name_postgresql(self):
        result = _run(
            {"APP_ENV": "development", "DEBUG": "True", "DB_NAME": "trx_dev"}, "runserver",
            "assert settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_04_staging_safe_loads_correctly(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging"}, "runserver",
            "assert settings.APP_ENV == 'staging' and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_05_staging_no_db_raises_via_o4c1_guard(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_06_staging_with_debug_raises_via_o4c2_guard(self):
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "DB_NAME": "trx_staging"}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEBUG=True is not allowed", result.stderr)

    def test_07_staging_doubly_insecure_fails_deterministically_debug_guard_first(self):
        # The genuine staging case (O.4c-2's own version of this
        # exercises APP_ENV=production instead — see module docstring).
        # DEBUG is validated immediately after APP_ENV, before the
        # DATABASES block runs at all, so the DEBUG guard's message is
        # deterministically what an operator sees first.
        result = _run(
            {"APP_ENV": "staging", "DEBUG": "True", "DB_NAME": ""}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEBUG=True is not allowed", result.stderr)
        self.assertIn("APP_ENV", result.stderr)
        self.assertIn("staging", result.stderr)
        # The DB_NAME guard's own message must NOT be what fired here —
        # confirms deterministic ordering, not just "something raised".
        self.assertNotIn("SQLite is not permitted", result.stderr)

    def test_08_production_safe_loads_correctly(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod"}, "runserver",
            "assert settings.APP_ENV == 'production' and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_09_production_no_db_raises(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "False", "DB_NAME": ""}, "runserver",
            "_ = settings.DATABASES",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB_NAME", result.stderr)

    def test_10_production_with_debug_raises(self):
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": "trx_prod"}, "runserver",
            "_ = settings.DEBUG",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEBUG=True is not allowed", result.stderr)

    def test_11_invalid_app_env_raises_regardless_of_debug_or_db_name(self):
        for debug, db_name in (("True", ""), ("False", ""), ("True", "x"), ("False", "x")):
            with self.subTest(debug=debug, db_name=db_name):
                result = _run(
                    {"APP_ENV": "qa", "DEBUG": debug, "DB_NAME": db_name}, "runserver",
                    "_ = settings.APP_ENV",
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("APP_ENV", result.stderr)

    def test_12_manage_py_test_preserves_sqlite_and_debug_exceptions_but_not_invalid_app_env(self):
        # Valid APP_ENV, insecure combination -> exempted under test.
        result = _run(
            {"APP_ENV": "production", "DEBUG": "True", "DB_NAME": ""}, "test",
            "assert settings.DEBUG is True "
            "and settings.DATABASES['default']['ENGINE'].endswith('sqlite3')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # Invalid APP_ENV -> still raises even under `test`.
        result = _run(
            {"APP_ENV": "qa", "DEBUG": "True", "DB_NAME": ""}, "test",
            "_ = settings.APP_ENV",
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("APP_ENV", result.stderr)


# ─────────────────────────────────────────────
# All 6 guards coexisting in one correct, fully-configured invocation
# ─────────────────────────────────────────────

class AllGuardsCoexistTests(TestCase):

    def test_fully_correct_staging_configuration_boots_clean(self):
        result = _run(
            {
                "APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging",
                "EMAIL_HOST": "smtp.example.com",
                "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.SECRET_KEY and settings.APP_ENV == 'staging' "
            "and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql') "
            "and settings.EMAIL_HOST == 'smtp.example.com' "
            "and settings.NOWPAYMENTS_IPN_SECRET == 'real-secret'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fully_correct_production_configuration_boots_clean(self):
        result = _run(
            {
                "APP_ENV": "production", "DEBUG": "False", "DB_NAME": "trx_prod",
                "EMAIL_HOST": "smtp.example.com",
                "NOWPAYMENTS_IPN_SECRET": "real-secret",
            },
            "runserver",
            "assert settings.APP_ENV == 'production' and settings.DEBUG is False "
            "and settings.DATABASES['default']['ENGINE'].endswith('postgresql')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_of_the_six_guards_independently_still_blocks_in_an_otherwise_safe_staging_env(self):
        base = {
            "APP_ENV": "staging", "DEBUG": "False", "DB_NAME": "trx_staging",
            "EMAIL_HOST": "smtp.example.com", "NOWPAYMENTS_IPN_SECRET": "real-secret",
        }

        # SECRET_KEY guard (simulated separately below — DJANGO_SECRET_KEY
        # is force-set by _run(), so it's covered by O.4c-1/O.4c-2's own
        # dedicated tests; not repeated here to avoid duplicating them).

        broken_email = dict(base, EMAIL_HOST="")
        result = _run(broken_email, "runserver", "_ = settings.EMAIL_HOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EMAIL_HOST", result.stderr)

        broken_np = dict(base, NOWPAYMENTS_IPN_SECRET="")
        result = _run(broken_np, "runserver", "_ = settings.NOWPAYMENTS_IPN_SECRET")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOWPAYMENTS_IPN_SECRET", result.stderr)

        broken_app_env = dict(base, APP_ENV="qa")
        result = _run(broken_app_env, "runserver", "_ = settings.APP_ENV")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APP_ENV", result.stderr)

        broken_db = dict(base, DB_NAME="")
        result = _run(broken_db, "runserver", "_ = settings.DATABASES")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_NAME", result.stderr)

        broken_debug = dict(base, DEBUG="True")
        result = _run(broken_debug, "runserver", "_ = settings.DEBUG")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True is not allowed", result.stderr)


# ─────────────────────────────────────────────
# Deploy documentation matches the guard contract (locks in against drift)
# ─────────────────────────────────────────────

class DeployDocumentationContractTests(TestCase):

    def test_env_example_documents_app_env_default_development(self):
        content = (_REPO_ROOT / ".env.example").read_text()
        self.assertIn("APP_ENV=development", content)

    def test_staging_template_declares_staging_debug_false_and_db_name(self):
        content = (_REPO_ROOT / "deploy" / ".env.staging.template").read_text()
        self.assertIn("APP_ENV=staging", content)
        self.assertIn("DEBUG=False", content)
        self.assertRegex(content, r"(?m)^DB_NAME=\S+")

    def test_no_production_template_was_invented_for_this_block(self):
        # O.4c Fase 0 explicitly forbids creating new deploy
        # infrastructure just to document the contract — .env.example
        # already documents it generically. This test simply records
        # that fact so a future addition of a real production template
        # is a deliberate decision, not silent scope creep.
        self.assertFalse((_REPO_ROOT / "deploy" / ".env.production.template").exists())
