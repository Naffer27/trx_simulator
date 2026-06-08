# simulator/tests/test_register_login_fix.py
"""
Register / Login confusion fix — regression tests.

Covers:
  1.  Register creates user with usable password
  2.  After register, user is authenticated and redirected away from login
  3.  Welcome email to user does NOT include purchase.code
  4.  Admin email may include purchase.code
  5.  Login works with username + password
  6.  Login works with email + password
  7.  Login fails with wrong password
  8.  Access code placeholder no longer uses CODE-123-4567
"""
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()

_REG_URL   = "/register/"
_LOGIN_URL = "/login/"

_VALID_REGISTER = {
    "username":  "fixtest_user",
    "email":     "fixtest@example.com",
    "password1": "StrongPass!77",
    "password2": "StrongPass!77",
    "tier":      "10K",
    "phase":     "Fase 1",
}

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")
_PATCH_SEND_MAIL   = patch("simulator.views.send_mail")


class RegisterPasswordTests(TestCase):
    """Registration must create a user with a properly hashed password."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()
        _PATCH_SEND_MAIL.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()
        _PATCH_SEND_MAIL.stop()

    def test_register_creates_user_with_usable_password(self):
        self.client.post(_REG_URL, _VALID_REGISTER)
        user = User.objects.filter(username="fixtest_user").first()
        self.assertIsNotNone(user, "User must be created")
        self.assertTrue(user.has_usable_password(), "Password must be hashed and usable")
        self.assertTrue(
            user.check_password("StrongPass!77"),
            "check_password must succeed with the password used at registration",
        )

    def test_register_redirects_away_from_login(self):
        """After registration the user should NOT be sent to /login/."""
        resp = self.client.post(_REG_URL, _VALID_REGISTER)
        self.assertNotEqual(
            resp.get("Location", ""),
            _LOGIN_URL,
            "redirect after registration must not point to the login page",
        )
        # Must redirect somewhere (302)
        self.assertEqual(resp.status_code, 302)

    def test_register_authenticates_user_session(self):
        """After registration the user must be authenticated in the session."""
        self.client.post(_REG_URL, _VALID_REGISTER)
        # If the user is logged in, _auth_user_id will be in the session
        self.assertIn(
            "_auth_user_id",
            self.client.session,
            "User must be logged in immediately after registration",
        )


class WelcomeEmailContentTests(TestCase):
    """Welcome email to user must not expose purchase.code."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()

    def test_user_email_does_not_contain_purchase_code(self):
        with patch("simulator.views.send_mail") as mock_mail:
            self.client.post(_REG_URL, _VALID_REGISTER)

        user = User.objects.filter(username="fixtest_user").first()
        self.assertIsNotNone(user)

        # Collect all calls to send_mail
        calls_to_user = [
            c for c in mock_mail.call_args_list
            if user.email in (c.args[3] if c.args else c.kwargs.get("recipient_list", []))
        ]
        self.assertTrue(len(calls_to_user) >= 1, "At least one email must be sent to the user")

        for c in calls_to_user:
            plain_body = c.args[1] if len(c.args) > 1 else c.kwargs.get("message", "")
            html_body  = c.kwargs.get("html_message", "")
            full_text  = plain_body + html_body
            self.assertNotIn(
                "Código de acceso",
                full_text,
                "User email must not contain 'Código de acceso'",
            )
            # purchase.code format is CODE-<id>-<4 digits>
            import re
            self.assertFalse(
                re.search(r"CODE-\d+-\d+", full_text),
                "User email must not contain a CODE-<id>-<digits> purchase code",
            )

    def test_admin_email_contains_purchase_code(self):
        """Admin notification email is allowed to include purchase.code."""
        with patch("simulator.views.send_mail") as mock_mail:
            self.client.post(_REG_URL, _VALID_REGISTER)

        admin_email = "nafferphotographer@gmail.com"
        calls_to_admin = [
            c for c in mock_mail.call_args_list
            if admin_email in (c.args[3] if c.args else c.kwargs.get("recipient_list", []))
               and (c.args[3] if c.args else c.kwargs.get("recipient_list", [])) == [admin_email]
        ]
        self.assertTrue(len(calls_to_admin) >= 1, "Admin notification email must be sent")

        import re
        found_code = any(
            re.search(r"CODE-\d+-\d+", c.args[1] if len(c.args) > 1 else c.kwargs.get("message", ""))
            for c in calls_to_admin
        )
        self.assertTrue(found_code, "Admin email should include the purchase code")


class LoginUsernameEmailTests(TestCase):
    """Login must work with username OR email."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()
        _PATCH_SEND_MAIL.start()
        # Register a user so we have one to log in as
        self.client.post(_REG_URL, _VALID_REGISTER)
        self.client.logout()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL_ASYNC.stop()
        _PATCH_SEND_MAIL.stop()

    def test_login_with_username_succeeds(self):
        resp = self.client.post(_LOGIN_URL, {
            "username": "fixtest_user",
            "password": "StrongPass!77",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_email_succeeds(self):
        resp = self.client.post(_LOGIN_URL, {
            "username": "fixtest@example.com",  # email in the username field
            "password": "StrongPass!77",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_fails_with_wrong_password(self):
        resp = self.client.post(_LOGIN_URL, {
            "username": "fixtest_user",
            "password": "WrongPassword!",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_fails_with_email_and_wrong_password(self):
        resp = self.client.post(_LOGIN_URL, {
            "username": "fixtest@example.com",
            "password": "WrongPassword!",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class LoginTemplateTests(TestCase):
    """Login template must not expose CODE-* placeholder that looks like purchase.code."""

    def test_access_code_placeholder_does_not_use_code_format(self):
        resp = self.client.get(_LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertNotIn(
            "CODE-123-4567",
            content,
            "Login page must not use CODE-123-4567 as placeholder",
        )
