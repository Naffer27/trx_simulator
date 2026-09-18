# simulator/tests/test_settings_email.py
"""
Email settings — regression tests.

Verifies:
  - DEBUG=True  → filebased email backend (checked against running settings)
  - DEBUG=False → smtp backend by default (outside test mode — see
    FIX-TEST-EMAIL-BACKEND-ISOLATION-01 note below)
  - DEBUG=False without EMAIL_HOST raises ImproperlyConfigured (not in test mode)
  - All email env vars are read correctly
  - DEFAULT_FROM_EMAIL and SERVER_EMAIL are configurable
  - dev_emails path is used for development
  - FIX-TEST-EMAIL-BACKEND-ISOLATION-01: EMAIL_BACKEND is authoritatively
    forced to locmem whenever _IN_TEST_RUN (sys.argv[1] == "test"), even
    if .env explicitly requests smtp — and left completely untouched
    outside test mode.

Uses subprocess isolation for any test that changes DEBUG, EMAIL_*, or
sys.argv — following the same pattern as test_settings_security.py.

IMPORTANT (why subprocess, not in-process, for the smtp-vs-locmem
assertions): Django's own test runner (django.test.utils.
setup_test_environment(), called by DiscoverRunner.run_tests()) ALSO
unconditionally forces settings.EMAIL_BACKEND to locmem for the
duration of any `manage.py test` run — but only once the full test
runner has started, and only for the actual test process. The
subprocess scripts below do a bare `sys.argv=[...]; from django.conf
import settings` — they never call setup_test_environment() at all —
so what they observe is PURELY this project's own settings.py
resolution logic (the _IN_TEST_RUN-gated override added in
FIX-TEST-EMAIL-BACKEND-ISOLATION-01), never confounded by Django's
separate, later runtime protection. This is the only way to prove
*this project's own* fix is doing the work, not just re-observe
Django's unrelated built-in guarantee.
"""
import subprocess
import sys

from django.conf import settings
from django.test import TestCase

_TEST_SECRET = "subprocess-email-test-key-not-for-production"


def _run(extra_env: dict, assertion: str) -> subprocess.CompletedProcess:
    """Subprocess with sys.argv=['manage.py', 'test'] — _IN_TEST_RUN=True."""
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


def _run_outside_test_mode(extra_env: dict, assertion: str) -> subprocess.CompletedProcess:
    """Subprocess with sys.argv=['manage.py', 'runserver'] — _IN_TEST_RUN=False.
    Use this whenever the assertion needs to observe what this project's
    settings.py resolves OUTSIDE of test mode (e.g. the real production
    smtp default) — the FIX-TEST-EMAIL-BACKEND-ISOLATION-01 override is a
    no-op here by design."""
    import os
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
    env.update(extra_env)
    script = (
        "import sys; sys.argv = ['manage.py', 'runserver']; "
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
    """Running environment (DEBUG=True) uses filebased backend.

    NOTE: manage.py test runs with settings.DEBUG forced to False by
    Django's own DiscoverRunner by default (debug_mode=False unless
    --debug-mode is passed) — unrelated to this fix, pre-existing Django
    behavior — so these two DEBUG-gated checks self-skip under the
    normal `manage.py test` invocation this project uses. Left as-is,
    not something FIX-TEST-EMAIL-BACKEND-ISOLATION-01 changes or needs
    to change.
    """

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


# ── FIX-TEST-EMAIL-BACKEND-ISOLATION-01 — requirement A + connection class ──

class TestModeEmailBackendOverrideTests(TestCase):
    """TEST MODE (_IN_TEST_RUN=True) + env explicitly requests smtp ->
    EMAIL_BACKEND must resolve to locmem. This is the core guarantee
    FIX-TEST-EMAIL-BACKEND-ISOLATION-01 adds — reproduces the exact live
    condition observed in this repo (.env sets EMAIL_BACKEND=smtp,
    EMAIL_HOST=smtp.gmail.com) and proves the override wins."""

    def test_env_smtp_forced_to_locmem_in_test_mode(self):
        result = _run(
            {"DEBUG": "False",
             "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com", "EMAIL_HOST_USER": "x", "EMAIL_HOST_PASSWORD": "y"},
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.locmem.EmailBackend', repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_env_smtp_forced_to_locmem_even_with_debug_true(self):
        """DEBUG=True would normally default to filebased anyway, but the
        explicit env override to smtp must still be caught and forced to
        locmem in test mode — the override applies regardless of what
        the underlying default would have been."""
        result = _run(
            {"DEBUG": "True",
             "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com"},
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.locmem.EmailBackend', repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connection_class_is_actually_locmem_not_just_the_string(self):
        """Proves the real, usable mail connection object — not just the
        settings string — resolves to locmem's EmailBackend class."""
        result = _run(
            {"DEBUG": "False",
             "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com"},
            "from django.core.mail import get_connection; "
            "conn = get_connection(); "
            "assert type(conn).__module__ == 'django.core.mail.backends.locmem', "
            "type(conn).__module__; "
            "assert type(conn).__name__ == 'EmailBackend', type(conn).__name__",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ── FIX-TEST-EMAIL-BACKEND-ISOLATION-01 — requirement B ──────────────────────

class NonTestModeEmailBackendTests(TestCase):
    """NON-TEST MODE (_IN_TEST_RUN=False, e.g. sys.argv=['manage.py',
    'runserver']) + env requests smtp -> EMAIL_BACKEND must remain smtp,
    completely unaffected by the FIX-TEST-EMAIL-BACKEND-ISOLATION-01
    override. Production/normal local behavior must be untouched."""

    def test_env_smtp_respected_outside_test_mode(self):
        result = _run_outside_test_mode(
            {"DEBUG": "False",
             "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com"},
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.smtp.EmailBackend', repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_env_smtp_respected_outside_test_mode_debug_true(self):
        """Even the DEBUG=True case (where filebased would otherwise be
        the default) must still honor an explicit smtp override outside
        test mode — this project's existing "override any default by
        setting EMAIL_BACKEND explicitly" contract is untouched."""
        result = _run_outside_test_mode(
            {"DEBUG": "True",
             "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com"},
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.smtp.EmailBackend', repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ── Subprocess: production SMTP default (requirement C) ──────────────────────

class ProdSmtpDefaultTests(TestCase):
    """DEBUG=False with EMAIL_HOST set must use SMTP backend BY DEFAULT
    (no explicit EMAIL_BACKEND override) — verified OUTSIDE test mode,
    so the FIX-TEST-EMAIL-BACKEND-ISOLATION-01 override can never make
    this assertion vacuous. This is deliberately the counterpart to
    TestModeEmailBackendOverrideTests: same production defaulting logic,
    opposite _IN_TEST_RUN value, opposite (correct) outcome."""

    def test_prod_uses_smtp_backend_by_default(self):
        result = _run_outside_test_mode(
            {"DEBUG": "False", "EMAIL_HOST": "smtp.sendgrid.net", "EMAIL_HOST_USER": ""},
            "assert 'smtp' in settings.EMAIL_BACKEND.lower(), "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prod_email_backend_is_smtp_not_filebased(self):
        result = _run_outside_test_mode(
            {"DEBUG": "False", "EMAIL_HOST": "smtp.sendgrid.net", "EMAIL_HOST_USER": ""},
            "assert 'filebased' not in settings.EMAIL_BACKEND, "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prod_custom_backend_override(self):
        """An explicit non-default EMAIL_BACKEND is respected outside
        test mode — genuinely exercised here (not coincidentally true
        because test mode would force locmem regardless)."""
        result = _run_outside_test_mode(
            {
                "DEBUG": "False",
                "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
                "EMAIL_HOST_USER": "",
            },
            "assert 'locmem' in settings.EMAIL_BACKEND, "
            "repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(
            result.returncode, 0, result.stderr,
        )


# ── Subprocess: guard for missing EMAIL_HOST (requirement D) ─────────────────

class ProdMissingEmailHostTests(TestCase):
    """DEBUG=False + SMTP + no EMAIL_HOST must raise ImproperlyConfigured
    outside test mode; inside test mode the settings import must remain
    allowed (the guard is skipped) AND the resolved backend must be
    locmem (FIX-TEST-EMAIL-BACKEND-ISOLATION-01) — not smtp, which would
    mean the guard was skipped but the unsafe backend was still active."""

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
        """manage.py test must not be blocked by the email guard, AND
        (FIX-TEST-EMAIL-BACKEND-ISOLATION-01) the backend that ends up
        active while the guard is bypassed must be locmem, never smtp."""
        result = _run(
            {"DEBUG": "False", "EMAIL_HOST": ""},
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.locmem.EmailBackend', repr(settings.EMAIL_BACKEND)",
        )
        self.assertEqual(
            result.returncode, 0,
            f"Test mode must bypass the email guard AND force locmem.\nstderr: {result.stderr}",
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


# ── FIX-TEST-CELERY-EMAIL-QUEUE-ISOLATION-01 ──────────────────────────────────
#
# manage.py test must never enqueue a real message into the shared local
# Redis Celery broker. Empirically confirmed before this fix: unmocked
# @shared_task .delay() calls during tests landed in the exact same
# `celery` queue key a real dev/prod-pointed worker reads from
# (simulator.send_email to unverified@test.com via
# test_email_verification.py, and to refund@test.com via
# test_withdrawals.py::PayoutCallbackRefundTests).
#
# A historical test-originated message already existed in that queue
# before this fix was written — these tests use before/after LLEN
# comparisons, never an absolute LLEN==0 assertion, so they remain
# correct regardless of that pre-existing message's presence and never
# consume/pop it (a plain LLEN is a read-only O(1) Redis command).

def _run_full_django(extra_env: dict, body: str) -> subprocess.CompletedProcess:
    """Subprocess with sys.argv=['manage.py', 'test'] AND a full
    django.setup() — needed to import simulator.tasks (Celery/app
    registry), unlike the bare `from django.conf import settings` used
    by _run() above."""
    import os
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
    env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
    env.update(extra_env)
    script = (
        "import sys; sys.argv = ['manage.py', 'test']; "
        "import django; django.setup(); "
        "from django.conf import settings\n" + body
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )


class CeleryEagerModeInTestRunTests(TestCase):
    """Requirements A/B — test mode forces both eager execution and
    centrally, via the same _IN_TEST_RUN flag already used for
    EMAIL_BACKEND. CELERY_TASK_EAGER_PROPAGATES is deliberately NOT
    forced (FIX-TEST-CELERY-EMAIL-QUEUE-ISOLATION-01 final correction)
    — see test_eager_propagates_remains_default_in_test_mode below for
    why: Task.apply()'s own `throw` parameter defaults to
    task_eager_propagates for EVERY .apply() call (not just email
    tasks), and forcing it to True broke 2 genuine, pre-existing
    Treasury-monitoring tests that rely on .apply()'s default
    non-propagating contract."""

    def test_always_eager_true_in_test_mode(self):
        result = _run(
            {}, "assert settings.CELERY_TASK_ALWAYS_EAGER is True, "
                "getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', 'MISSING')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_eager_propagates_remains_default_in_test_mode(self):
        """ALWAYS_EAGER alone is sufficient to keep .delay() off the real
        broker — EAGER_PROPAGATES must stay at Celery's own default
        (False) even in test mode, so Task.apply()'s default `throw`
        behavior (and every existing test relying on it) is preserved."""
        result = _run(
            {}, "assert getattr(settings, 'CELERY_TASK_EAGER_PROPAGATES', False) is False, "
                "getattr(settings, 'CELERY_TASK_EAGER_PROPAGATES', 'MISSING')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CeleryEagerModeOutsideTestRunTests(TestCase):
    """Requirement C — production/non-test Celery configuration is
    completely untouched: no eager override, real async dispatch via
    the real broker exactly as today."""

    def test_always_eager_not_set_outside_test_mode(self):
        result = _run_outside_test_mode(
            {}, "assert getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False) is False, "
                "getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', 'MISSING')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_eager_propagates_not_set_outside_test_mode(self):
        result = _run_outside_test_mode(
            {}, "assert getattr(settings, 'CELERY_TASK_EAGER_PROPAGATES', False) is False, "
                "getattr(settings, 'CELERY_TASK_EAGER_PROPAGATES', 'MISSING')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_broker_url_unchanged_outside_test_mode(self):
        """Sanity: the real broker URL itself is never touched by this fix."""
        result = _run_outside_test_mode(
            {"REDIS_URL": "redis://127.0.0.1:6379/0"},
            "assert settings.CELERY_BROKER_URL == 'redis://127.0.0.1:6379/0', "
            "settings.CELERY_BROKER_URL",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CeleryDelayDoesNotTouchRealQueueTests(TestCase):
    """Requirement D — a real @shared_task .delay() call executed during
    manage.py test does not enqueue into the normal Redis `celery`
    queue. Uses before/after LLEN, never an absolute assertion, because
    a historical test-originated message may already be present (and
    must not be assumed away or consumed by this test)."""

    def test_delay_does_not_increase_real_broker_queue_length(self):
        result = _run_full_django(
            {},
            "import redis\n"
            "r = redis.Redis.from_url(settings.CELERY_BROKER_URL)\n"
            "before = r.llen('celery')\n"
            "from simulator.tasks import send_email_async\n"
            "send_email_async.delay(subject='eager-isolation-check', message='body', "
            "recipient_list=['eager-check@test.invalid'])\n"
            "after = r.llen('celery')\n"
            "assert after == before, (before, after)\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CeleryDelayUsesLocmemNotRealSmtpTests(TestCase):
    """Requirement E — send_email_async invoked eagerly during test mode
    uses the FIX-TEST-EMAIL-BACKEND-ISOLATION-01-forced locmem backend
    and cannot reach external SMTP. Uses a safe, non-routable fake
    recipient (.invalid TLD, reserved by RFC 2606 — never a real,
    deliverable address) and inspects django.core.mail.outbox, which
    only ever populates for the locmem backend."""

    def test_delay_eager_execution_lands_in_locmem_outbox(self):
        result = _run_full_django(
            {"EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
             "EMAIL_HOST": "smtp.gmail.com"},
            "from django.core import mail\n"
            "assert settings.EMAIL_BACKEND == "
            "'django.core.mail.backends.locmem.EmailBackend', settings.EMAIL_BACKEND\n"
            "from simulator.tasks import send_email_async\n"
            "send_email_async.delay(subject='eager-locmem-check', message='body', "
            "recipient_list=['eager-check@test.invalid'])\n"
            "assert len(mail.outbox) == 1, len(mail.outbox)\n"
            "assert mail.outbox[0].subject == 'eager-locmem-check', mail.outbox[0].subject\n"
            "assert mail.outbox[0].to == ['eager-check@test.invalid'], mail.outbox[0].to\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
