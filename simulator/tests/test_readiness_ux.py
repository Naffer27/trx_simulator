# simulator/tests/test_readiness_ux.py
"""
Onboarding / Withdrawal Readiness UX tests.

Scenarios:
  1. Fully-ready user: can_withdraw=True, no missing_requirements.
  2. Email not verified: email_verified=False, 'email' in missing keys.
  3. Terms not accepted: terms_accepted=False, 'terms' in missing keys.
  4. KYC status 'none': kyc_approved=False, 'kyc' in missing keys.
  5. KYC status 'pending': kyc_status='pending', can_withdraw=False.
  6. 2FA not enabled: totp_enabled=False, 'totp' in missing keys.
  7. Home dashboard renders checklist only when requirements are missing.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.models import KYCProfile, TOTPDevice
from simulator.readiness import get_user_readiness
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_totp(user) -> TOTPDevice:
    import pyotp
    return TOTPDevice.objects.create(
        user=user,
        secret=pyotp.random_base32(),
        confirmed=True,
        confirmed_at=timezone.now(),
    )


def _kyc_pending(user) -> KYCProfile:
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status = KYCProfile.STATUS_PENDING
    kyc.save()
    return kyc


def _fully_ready_user():
    """Return a user that passes all four gates."""
    user = make_user(email_verified=True, terms_accepted=True)
    make_wallet(user)
    make_kyc_approved(user)
    _make_totp(user)
    return user


# ── 1. Fully ready ─────────────────────────────────────────────────────────────

class FullyReadyTest(TestCase):
    def test_can_withdraw_true(self):
        user = _fully_ready_user()
        r = get_user_readiness(user)
        self.assertTrue(r["can_withdraw"])
        self.assertEqual(r["missing_requirements"], [])

    def test_all_flags_true(self):
        user = _fully_ready_user()
        r = get_user_readiness(user)
        self.assertTrue(r["email_verified"])
        self.assertTrue(r["terms_accepted"])
        self.assertTrue(r["kyc_approved"])
        self.assertEqual(r["kyc_status"], KYCProfile.STATUS_APPROVED)
        self.assertTrue(r["totp_enabled"])


# ── 2. Email not verified ──────────────────────────────────────────────────────

class EmailNotVerifiedTest(TestCase):
    def setUp(self):
        self.user = make_user(email_verified=False, terms_accepted=True)
        make_wallet(self.user)
        make_kyc_approved(self.user)
        _make_totp(self.user)

    def test_email_verified_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["email_verified"])

    def test_email_in_missing(self):
        r = get_user_readiness(self.user)
        keys = [m["key"] for m in r["missing_requirements"]]
        self.assertIn("email", keys)

    def test_can_withdraw_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["can_withdraw"])


# ── 3. Terms not accepted ──────────────────────────────────────────────────────

class TermsNotAcceptedTest(TestCase):
    def setUp(self):
        self.user = make_user(email_verified=True, terms_accepted=False)
        make_wallet(self.user)
        make_kyc_approved(self.user)
        _make_totp(self.user)

    def test_terms_accepted_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["terms_accepted"])

    def test_terms_in_missing(self):
        r = get_user_readiness(self.user)
        keys = [m["key"] for m in r["missing_requirements"]]
        self.assertIn("terms", keys)

    def test_can_withdraw_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["can_withdraw"])


# ── 4. KYC status 'none' ──────────────────────────────────────────────────────

class KycNoneTest(TestCase):
    def setUp(self):
        # No KYCProfile created for this user
        self.user = make_user(email_verified=True, terms_accepted=True)
        make_wallet(self.user)
        _make_totp(self.user)

    def test_kyc_status_none(self):
        r = get_user_readiness(self.user)
        self.assertEqual(r["kyc_status"], "none")
        self.assertFalse(r["kyc_approved"])

    def test_kyc_in_missing(self):
        r = get_user_readiness(self.user)
        keys = [m["key"] for m in r["missing_requirements"]]
        self.assertIn("kyc", keys)

    def test_can_withdraw_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["can_withdraw"])


# ── 5. KYC pending ────────────────────────────────────────────────────────────

class KycPendingTest(TestCase):
    def setUp(self):
        self.user = make_user(email_verified=True, terms_accepted=True)
        make_wallet(self.user)
        _kyc_pending(self.user)
        _make_totp(self.user)

    def test_kyc_status_pending(self):
        r = get_user_readiness(self.user)
        self.assertEqual(r["kyc_status"], KYCProfile.STATUS_PENDING)

    def test_kyc_approved_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["kyc_approved"])

    def test_can_withdraw_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["can_withdraw"])

    def test_missing_label_mentions_pending(self):
        r = get_user_readiness(self.user)
        kyc_items = [m for m in r["missing_requirements"] if m["key"] == "kyc"]
        self.assertTrue(kyc_items)
        self.assertIn("revisión", kyc_items[0]["label"].lower())


# ── 6. 2FA not enabled ────────────────────────────────────────────────────────

class TotpNotEnabledTest(TestCase):
    def setUp(self):
        self.user = make_user(email_verified=True, terms_accepted=True)
        make_wallet(self.user)
        make_kyc_approved(self.user)
        # No TOTPDevice created

    def test_totp_enabled_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["totp_enabled"])

    def test_totp_in_missing(self):
        r = get_user_readiness(self.user)
        keys = [m["key"] for m in r["missing_requirements"]]
        self.assertIn("totp", keys)

    def test_can_withdraw_false(self):
        r = get_user_readiness(self.user)
        self.assertFalse(r["can_withdraw"])


# ── 7. Home dashboard checklist rendering ─────────────────────────────────────

HOME_URL = "/home/"


class HomeDashboardChecklistTest(TestCase):
    def _get_home(self, user):
        from simulator.tests.factories import make_account
        make_account(user=user)
        self.client.force_login(user)
        return self.client.get(HOME_URL)

    def test_checklist_hidden_for_fully_ready_user(self):
        user = _fully_ready_user()
        r = self._get_home(user)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Completa tu cuenta")

    def test_checklist_shown_for_incomplete_user(self):
        user = make_user(email_verified=False, terms_accepted=False)
        make_wallet(user)
        r = self._get_home(user)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Completa tu cuenta")

    def test_checklist_shows_email_cta(self):
        user = make_user(email_verified=False, terms_accepted=True)
        make_wallet(user)
        r = self._get_home(user)
        self.assertContains(r, "Verificar email")

    def test_checklist_shows_kyc_cta(self):
        user = make_user(email_verified=True, terms_accepted=True)
        make_wallet(user)
        # No KYCProfile → kyc_status='none'
        r = self._get_home(user)
        self.assertContains(r, "KYC")

    def test_checklist_shows_totp_cta(self):
        user = make_user(email_verified=True, terms_accepted=True)
        make_wallet(user)
        make_kyc_approved(user)
        # No TOTP device
        r = self._get_home(user)
        self.assertContains(r, "2FA")
