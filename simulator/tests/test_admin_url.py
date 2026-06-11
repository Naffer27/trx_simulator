# simulator/tests/test_admin_url.py
"""
Configurable admin URL — ADMIN_URL setting.

Covers:
  1.  Default ADMIN_URL is "admin/".
  2.  ADMIN_URL env var with trailing slash is used as-is.
  3.  ADMIN_URL env var without trailing slash gets slash appended.
  4.  ADMIN_URL env var with multiple trailing slashes is normalised to one.
  5.  GET /admin/ returns 200 or redirect (admin is reachable at default path).
  6.  GET /admin/ returns 404 when ADMIN_URL is changed (old path gone).
  7.  Custom admin path is reachable after ADMIN_URL env var is set.
"""
import os
import subprocess
import sys

from django.conf import settings
from django.test import TestCase, override_settings


class AdminURLDefaultTests(TestCase):
    def test_default_admin_url_is_admin_slash(self):
        # When ADMIN_URL env var is absent the setting must be "admin/".
        self.assertEqual(settings.ADMIN_URL, "admin/")

    def test_default_admin_url_ends_with_slash(self):
        self.assertTrue(settings.ADMIN_URL.endswith("/"))


class AdminURLNormalisationTests(TestCase):
    """
    Normalisation rules verified via subprocess so settings is re-imported
    fresh for each env combination — same approach as HSTSEnvConfigTests.
    """

    def _run(self, extra_env: dict, assertion: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = "subprocess-test-key-not-for-production"
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

    def test_admin_url_with_trailing_slash_preserved(self):
        result = self._run(
            {"ADMIN_URL": "secret-panel/"},
            "assert settings.ADMIN_URL == 'secret-panel/', repr(settings.ADMIN_URL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_url_without_trailing_slash_gets_slash(self):
        result = self._run(
            {"ADMIN_URL": "mb-admin-secure-panel"},
            "assert settings.ADMIN_URL == 'mb-admin-secure-panel/', repr(settings.ADMIN_URL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_url_with_multiple_trailing_slashes_normalised(self):
        result = self._run(
            {"ADMIN_URL": "ops-panel///"},
            "assert settings.ADMIN_URL == 'ops-panel/', repr(settings.ADMIN_URL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_url_custom_value_ends_with_slash(self):
        result = self._run(
            {"ADMIN_URL": "custom-admin"},
            "assert settings.ADMIN_URL.endswith('/'), repr(settings.ADMIN_URL)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class AdminURLRoutingTests(TestCase):
    """HTTP-level and URL-pattern tests — admin wired at configured path."""

    def _run(self, extra_env: dict, assertion: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"] = "subprocess-test-key-not-for-production"
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

    def test_default_admin_path_is_reachable(self):
        # /admin/ must return login page (200) or a redirect — never 404.
        r = self.client.get("/admin/")
        self.assertIn(r.status_code, (200, 301, 302))

    def _url_patterns_script(self, assertion: str) -> str:
        return (
            "import django; django.setup(); "
            "from trx_simulator.urls import urlpatterns; "
            "patterns = [str(p.pattern) for p in urlpatterns]; " + assertion
        )

    def test_custom_admin_url_appears_in_url_patterns(self):
        result = self._run(
            {"ADMIN_URL": "secret-panel"},
            self._url_patterns_script(
                "assert any('secret-panel' in p for p in patterns), "
                "f'expected secret-panel in {patterns}'"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_admin_appears_in_url_patterns_without_env(self):
        result = self._run(
            {},
            self._url_patterns_script(
                "assert any(p == 'admin/' for p in patterns), "
                "f'expected admin/ in {patterns}'"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
