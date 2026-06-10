# simulator/tests/test_profile_view.py
"""
User profile page — /profile/

Covers:
  1.  Anonymous user redirected to login.
  2.  Authenticated user gets 200.
  3.  Page shows username.
  4.  Page shows email.
  5.  Page shows date_joined.
  6.  POST updates first_name.
  7.  POST updates last_name.
  8.  POST redirects (PRG pattern).
  9.  Invalid POST (first_name too long) re-renders with 200.
  10. Invalid POST does not save changes.
  11. KYC not_started shown as 'Sin verificar'.
  12. KYC pending shown as 'En revisión'.
  13. KYC approved shown as 'Aprobado'.
  14. KYC rejected shown as 'Rechazado'.
  15. Email verified status shown.
  16. Email unverified status shown.
  17. 2FA enabled status shown.
  18. 2FA disabled status shown.
  19. Sidebar 'Mi Perfil' link appears on /accounts/ page.
  20. active_section='profile' renders link as active on profile page.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from simulator.models import KYCProfile, TOTPDevice, EmailVerification, TermsAcceptance, TERMS_VERSION, RISK_DISCLOSURE_VERSION
from simulator.tests.factories import make_user, make_wallet

User = get_user_model()

PROFILE_URL  = "/profile/"
ACCOUNTS_URL = "/accounts/"


def _set_kyc(user, status):
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status = status
    kyc.legal_name = "Test"
    kyc.country = "VE"
    kyc.document_type = "national_id"
    kyc.save()
    return kyc


class ProfileAnonTests(TestCase):
    def test_anon_redirected_to_login(self):
        r = self.client.get(PROFILE_URL)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])

    def test_anon_post_redirected_to_login(self):
        r = self.client.post(PROFILE_URL, {"first_name": "Ana"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


class ProfileGetTests(TestCase):
    def setUp(self):
        self.user = make_user(username="testprofile", email="profile@test.com")
        self.client.force_login(self.user)

    def test_authenticated_gets_200(self):
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_shows_username(self):
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "testprofile")

    def test_shows_email(self):
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "profile@test.com")

    def test_shows_date_joined(self):
        year = str(self.user.date_joined.year)
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, year)


class ProfilePostTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_post_updates_first_name(self):
        self.client.post(PROFILE_URL, {"first_name": "Carlos", "last_name": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Carlos")

    def test_post_updates_last_name(self):
        self.client.post(PROFILE_URL, {"first_name": "", "last_name": "Rodríguez"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "Rodríguez")

    def test_post_redirects_after_save(self):
        r = self.client.post(PROFILE_URL, {"first_name": "Ana", "last_name": ""})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/profile/", r["Location"])

    def test_invalid_post_too_long_first_name_returns_200(self):
        r = self.client.post(PROFILE_URL, {"first_name": "A" * 200, "last_name": ""})
        self.assertEqual(r.status_code, 200)

    def test_invalid_post_does_not_save(self):
        original = self.user.first_name
        self.client.post(PROFILE_URL, {"first_name": "A" * 200, "last_name": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, original)


class ProfileKYCStatusTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_kyc_not_started_shows_sin_verificar(self):
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Sin verificar")

    def test_kyc_pending_shows_en_revision(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "En revisión")

    def test_kyc_approved_shows_aprobado(self):
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Aprobado")

    def test_kyc_rejected_shows_rechazado(self):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Rechazado")


class ProfileSecurityStatusTests(TestCase):
    def setUp(self):
        self.user = make_user(email_verified=True)
        self.client.force_login(self.user)

    def test_email_verified_status_shown(self):
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Verificado")

    def test_email_unverified_status_shown(self):
        user2 = make_user(email_verified=False)
        self.client.force_login(user2)
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Sin verificar")

    def test_2fa_enabled_shown(self):
        TOTPDevice.objects.create(
            user=self.user,
            secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
            confirmed=True,
        )
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Activado")

    def test_2fa_disabled_shown(self):
        resp = self.client.get(PROFILE_URL)
        self.assertContains(resp, "Desactivado")


class ProfileSidebarTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.client.force_login(self.user)

    def test_sidebar_link_mi_perfil_on_accounts(self):
        resp = self.client.get(ACCOUNTS_URL)
        self.assertContains(resp, "Mi Perfil")

    def test_sidebar_link_points_to_profile_url(self):
        resp = self.client.get(ACCOUNTS_URL)
        self.assertContains(resp, "/profile/")

    def test_profile_page_active_section(self):
        resp = self.client.get(PROFILE_URL)
        # The active class should be rendered on the Mi Perfil link
        content = resp.content.decode()
        self.assertIn('active_section', str(resp.context))
        self.assertEqual(resp.context["active_section"], "profile")
