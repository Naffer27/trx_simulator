# simulator/tests/test_login_access_code_removed.py
"""
Access Code removal from login — regression tests.

Covers:
  1.  Login template has no "Access Code" text
  2.  Login template has no CODE-123-4567 placeholder
  3.  Login template has no "optional in dev" text
  4.  Login template has "Forgot password" link
  5.  Login with username + password succeeds
  6.  Login with email + password succeeds
  7.  Login with wrong password fails (no access code needed)
  8.  Login with unknown email fails cleanly
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.tests.factories import make_user

User = get_user_model()

LOGIN_URL = "/login/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


class LoginTemplateTests(TestCase):
    """The login page must not expose any Access Code field or text."""

    def setUp(self):
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def _page(self):
        resp = self.client.get(LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_no_access_code_label(self):
        self.assertNotIn("Access Code", self._page())

    def test_no_codigo_opcional(self):
        content = self._page()
        self.assertNotIn("Código opcional", content)
        self.assertNotIn("optional in dev", content)

    def test_no_code_placeholder_format(self):
        self.assertNotIn("CODE-123-4567", self._page())

    def test_no_access_code_input_field(self):
        self.assertNotIn('name="access_code"', self._page())

    def test_has_forgot_password_link(self):
        content = self._page()
        self.assertIn("Forgot password", content)


class LoginAuthTests(TestCase):
    """Login must work with username or email, no access code required."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(username="logintest", email="logintest@example.com",
                              password="Secure!Pass99")

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_login_with_username_succeeds(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "logintest",
            "password": "Secure!Pass99",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_email_succeeds(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "logintest@example.com",
            "password": "Secure!Pass99",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_wrong_password_fails(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "logintest",
            "password": "WrongPassword!",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_unknown_email_fails(self):
        resp = self.client.post(LOGIN_URL, {
            "username": "nobody@example.com",
            "password": "Secure!Pass99",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
