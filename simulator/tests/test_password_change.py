# simulator/tests/test_password_change.py
"""
Change Password flow — regression tests.

Covers:
  1.  Anonymous user is redirected to login
  2.  Authenticated user can see the change-password form
  3.  Wrong current password is rejected
  4.  Correct current password changes the password
  5.  Login works with the new password
  6.  Login fails with the old password after the change
  7.  Done page loads for authenticated users
  8.  Sidebar contains a "Cambiar Contraseña" link in authenticated pages
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.tests.factories import make_user

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))

User = get_user_model()

CHANGE_URL = "/password-change/"
DONE_URL   = "/password-change/done/"
LOGIN_URL  = "/login/"


class PasswordChangeAnonTests(TestCase):
    """Unauthenticated requests must be redirected to login."""

    def test_anon_get_redirects_to_login(self):
        resp = self.client.get(CHANGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_anon_post_redirects_to_login(self):
        resp = self.client.post(CHANGE_URL, {
            "old_password": "whatever",
            "new_password1": "NewPass!99",
            "new_password2": "NewPass!99",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


class PasswordChangeFormTests(TestCase):
    """Authenticated users must see and use the form."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(username="chgpass_user", password="OldPass!123")
        self.client.login(username="chgpass_user", password="OldPass!123")

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_form_page_loads(self):
        resp = self.client.get(CHANGE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cambiar Contraseña")

    def test_wrong_old_password_rejected(self):
        resp = self.client.post(CHANGE_URL, {
            "old_password": "WrongPassword!",
            "new_password1": "NewPass!99",
            "new_password2": "NewPass!99",
        })
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("NewPass!99"))

    def test_correct_password_changes_password(self):
        resp = self.client.post(CHANGE_URL, {
            "old_password": "OldPass!123",
            "new_password1": "NewPass!99x",
            "new_password2": "NewPass!99x",
        })
        self.assertRedirects(resp, DONE_URL, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass!99x"))

    def test_login_works_with_new_password(self):
        self.client.post(CHANGE_URL, {
            "old_password": "OldPass!123",
            "new_password1": "BrandNew!77",
            "new_password2": "BrandNew!77",
        })
        self.client.logout()
        resp = self.client.post(LOGIN_URL, {
            "username": "chgpass_user",
            "password": "BrandNew!77",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_fails_with_old_password(self):
        self.client.post(CHANGE_URL, {
            "old_password": "OldPass!123",
            "new_password1": "BrandNew!77",
            "new_password2": "BrandNew!77",
        })
        self.client.logout()
        resp = self.client.post(LOGIN_URL, {
            "username": "chgpass_user",
            "password": "OldPass!123",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_done_page_loads(self):
        resp = self.client.get(DONE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Contraseña Actualizada")


class PasswordChangeSidebarTests(TestCase):
    """Sidebar must expose the Change Password link to authenticated users."""

    def setUp(self):
        self.user = make_user(username="sidebar_user", password="SidePass!1")
        self.client.login(username="sidebar_user", password="SidePass!1")

    def test_sidebar_has_change_password_link(self):
        resp = self.client.get("/accounts/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/password-change/")
        self.assertContains(resp, "Cambiar Contraseña")
