"""Public registration remains open even with a legacy beta-code setting."""
import secrets
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

User = get_user_model()

REG_URL     = "/register/"
_TEST_CODE  = secrets.token_urlsafe(24)  # random per test run — never hardcoded

_BASE_POST = {
    "username":  "betauser",
    "email":     "beta@example.com",
    "password1": "StrongBeta!99",
    "password2": "StrongBeta!99",
}

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")


def _post(client, extra=None):
    data = {**_BASE_POST, **(extra or {})}
    return client.post(REG_URL, data)


# ── 1. Open registration (no BROKER_ACCESS_CODE) ─────────────────────────────

@override_settings(BROKER_ACCESS_CODE="")
class OpenRegistrationTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def test_get_does_not_show_access_code_field(self):
        resp = self.client.get(REG_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="access_code"')
        self.assertNotContains(resp, 'Access Code')
        self.assertNotContains(resp, 'Beta Access')

    def test_post_without_access_code_creates_user(self):
        _post(self.client)
        self.assertTrue(User.objects.filter(username="betauser").exists())

    def test_post_without_access_code_redirects(self):
        resp = _post(self.client)
        self.assertEqual(resp.status_code, 302)


# A legacy server value must not silently close public registration.
@override_settings(BROKER_ACCESS_CODE=_TEST_CODE)
class LegacyCodeRegistrationTests(OpenRegistrationTests):
    def test_legacy_code_is_not_rendered(self):
        self.assertNotContains(self.client.get(REG_URL), _TEST_CODE)

    def test_submitted_access_code_is_ignored(self):
        response = _post(self.client, {"access_code": "obsolete-code"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="betauser").exists())
