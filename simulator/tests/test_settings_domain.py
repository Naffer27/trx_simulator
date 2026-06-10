# simulator/tests/test_settings_domain.py
"""
Production domain configuration — regression tests.

Verifies that ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS are correctly populated
from the DOMAIN, ALLOWED_HOSTS_EXTRA, and CSRF_TRUSTED_ORIGINS_EXTRA env vars.

All tests that change env vars use subprocess isolation so they don't pollute
the live settings import (same pattern as test_settings_security.py).
"""
import subprocess
import sys

from django.conf import settings
from django.test import TestCase

_BASE_ENV_KEY = "DJANGO_SETTINGS_MODULE"
_TEST_SECRET  = "subprocess-test-key-not-for-production"


class LocalhostDefaultsTests(TestCase):
    """Base hosts are always present — verified against the running settings."""

    def test_localhost_in_allowed_hosts(self):
        self.assertIn("localhost", settings.ALLOWED_HOSTS)

    def test_127_in_allowed_hosts(self):
        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)

    def test_csrf_has_localhost_http_origin(self):
        self.assertIn("http://localhost:8000", settings.CSRF_TRUSTED_ORIGINS)

    def test_csrf_has_127_origin(self):
        self.assertIn("http://127.0.0.1:8000", settings.CSRF_TRUSTED_ORIGINS)

    def test_no_empty_string_in_allowed_hosts(self):
        self.assertNotIn("", settings.ALLOWED_HOSTS,
                         "Empty string in ALLOWED_HOSTS would match every host")

    def test_no_empty_string_in_csrf_origins(self):
        self.assertNotIn("", settings.CSRF_TRUSTED_ORIGINS)


class DomainEnvTests(TestCase):
    """DOMAIN env var must add bare domain and www. to both ALLOWED_HOSTS and CSRF."""

    def _run(self, env_overrides: dict, assertion: str) -> subprocess.CompletedProcess:
        import os
        env = dict(os.environ)
        env[_BASE_ENV_KEY]    = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = _TEST_SECRET
        env["DEBUG"]             = "False"
        env.update(env_overrides)
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

    # ── ALLOWED_HOSTS ─────────────────────────────────────────────────────────

    def test_domain_adds_bare_host(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert 'moneybroker.com' in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_adds_www_host(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert 'www.moneybroker.com' in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_domain_does_not_add_empty_host(self):
        result = self._run(
            {},
            "assert '' not in settings.ALLOWED_HOSTS, "
            "'Empty string must not be in ALLOWED_HOSTS'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_base_hosts_present_in_production_mode(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert '127.0.0.1' in settings.ALLOWED_HOSTS and "
            "'localhost' in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    # ── CSRF_TRUSTED_ORIGINS ──────────────────────────────────────────────────

    def test_domain_adds_https_origin(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert 'https://moneybroker.com' in settings.CSRF_TRUSTED_ORIGINS, "
            "repr(settings.CSRF_TRUSTED_ORIGINS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_adds_https_www_origin(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert 'https://www.moneybroker.com' in settings.CSRF_TRUSTED_ORIGINS, "
            "repr(settings.CSRF_TRUSTED_ORIGINS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_domain_does_not_add_empty_csrf_origin(self):
        result = self._run(
            {},
            "assert '' not in settings.CSRF_TRUSTED_ORIGINS and "
            "'https://' not in settings.CSRF_TRUSTED_ORIGINS, "
            "'Bare https:// or empty string must not appear in CSRF_TRUSTED_ORIGINS'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    # ── CSV extras ────────────────────────────────────────────────────────────

    def test_allowed_hosts_extra_csv_parsed(self):
        result = self._run(
            {"ALLOWED_HOSTS_EXTRA": "staging.moneybroker.com, api.moneybroker.com"},
            "assert 'staging.moneybroker.com' in settings.ALLOWED_HOSTS and "
            "'api.moneybroker.com' in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_allowed_hosts_extra_strips_whitespace(self):
        result = self._run(
            {"ALLOWED_HOSTS_EXTRA": "  edge.moneybroker.com  "},
            "assert 'edge.moneybroker.com' in settings.ALLOWED_HOSTS and "
            "'  edge.moneybroker.com  ' not in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_allowed_hosts_extra_no_empty_strings(self):
        result = self._run(
            {"ALLOWED_HOSTS_EXTRA": "a.com,,b.com,"},
            "assert '' not in settings.ALLOWED_HOSTS, "
            "repr(settings.ALLOWED_HOSTS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_csrf_extra_csv_parsed(self):
        result = self._run(
            {"CSRF_TRUSTED_ORIGINS_EXTRA": "https://admin.moneybroker.com"},
            "assert 'https://admin.moneybroker.com' in settings.CSRF_TRUSTED_ORIGINS, "
            "repr(settings.CSRF_TRUSTED_ORIGINS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_csrf_extra_no_empty_strings(self):
        result = self._run(
            {"CSRF_TRUSTED_ORIGINS_EXTRA": "https://a.com,,"},
            "assert '' not in settings.CSRF_TRUSTED_ORIGINS, "
            "repr(settings.CSRF_TRUSTED_ORIGINS)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    # ── Production smoke test ─────────────────────────────────────────────────

    def test_debug_false_with_domain_does_not_crash(self):
        result = self._run(
            {"DOMAIN": "moneybroker.com"},
            "assert settings.DEBUG is False",
        )
        self.assertEqual(
            result.returncode, 0,
            f"Settings must load without error when DEBUG=False and DOMAIN is set.\n"
            f"stderr: {result.stderr}",
        )
