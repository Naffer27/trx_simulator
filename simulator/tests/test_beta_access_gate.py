# simulator/tests/test_beta_access_gate.py
"""
Bloque J.5 — Beta Access Gate.

Tests:
  Without BROKER_ACCESS_CODE (open registration):
    1. GET /register/ renders without access_code field.
    2. POST with valid credentials succeeds and creates user.
    3. POST without access_code field still succeeds (no gate active).

  With BROKER_ACCESS_CODE set (closed beta):
    4. GET /register/ renders with access_code field visible.
    5. POST with wrong code → 200 (form redisplayed), no user created.
    6. POST with correct code → user created and redirected.
    7. Error message on wrong code is generic — does NOT contain the real code.
    8. HTML response on wrong code does NOT contain the real code anywhere.
    9. POST with empty access_code → no user created.
   10. access_code field uses type=password (value not visible in HTML).
"""
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


# ── 2. Closed beta (BROKER_ACCESS_CODE set) ───────────────────────────────────

@override_settings(BROKER_ACCESS_CODE=_TEST_CODE)
class ClosedBetaRegistrationTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def test_get_shows_access_code_field(self):
        resp = self.client.get(REG_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="access_code"')

    def test_get_shows_beta_access_section(self):
        resp = self.client.get(REG_URL)
        self.assertContains(resp, 'Beta Access')

    def test_access_code_field_is_type_password(self):
        resp = self.client.get(REG_URL)
        content = resp.content.decode()
        self.assertIn('type="password"', content)
        # Specifically the access_code input must be password type
        self.assertIn('name="access_code"', content)
        idx = content.find('name="access_code"')
        surrounding = content[max(0, idx - 120):idx + 120]
        self.assertIn('type="password"', surrounding)

    def test_wrong_code_does_not_create_user(self):
        _post(self.client, {"access_code": "wrong-code"})
        self.assertFalse(User.objects.filter(username="betauser").exists())

    def test_wrong_code_returns_200_with_form(self):
        resp = _post(self.client, {"access_code": "wrong-code"})
        self.assertEqual(resp.status_code, 200)

    def test_empty_code_does_not_create_user(self):
        _post(self.client, {"access_code": ""})
        self.assertFalse(User.objects.filter(username="betauser").exists())

    def test_correct_code_creates_user(self):
        _post(self.client, {"access_code": _TEST_CODE})
        self.assertTrue(User.objects.filter(username="betauser").exists())

    def test_correct_code_redirects(self):
        resp = _post(self.client, {"access_code": _TEST_CODE})
        self.assertEqual(resp.status_code, 302)

    def test_error_message_does_not_contain_real_code(self):
        resp = _post(self.client, {"access_code": "wrong-code"})
        self.assertNotContains(resp, _TEST_CODE)

    def test_html_response_does_not_contain_real_code(self):
        resp = _post(self.client, {"access_code": "wrong-code"})
        self.assertNotIn(_TEST_CODE.encode(), resp.content)

    def test_error_message_is_generic(self):
        resp = _post(self.client, {"access_code": "wrong-code"})
        self.assertContains(resp, "Invalid access code")

    def test_post_without_access_code_field_does_not_create_user(self):
        # Simulates a client that bypasses the field entirely
        _post(self.client)
        self.assertFalse(User.objects.filter(username="betauser").exists())
