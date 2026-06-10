# simulator/tests/test_kyc_account_gate.py
"""
KYC gate on real account opening.

Rules enforced in create_account_view:
  - Demo accounts open freely — no KYC check.
  - Real accounts require KYCProfile.status == approved.
    Any other status (no profile / not_started / pending / rejected) → blocked.

Covers:
  1.  Demo account opens without any KYC profile.
  2.  Demo account opens with pending KYC.
  3.  Real account blocked when no KYC profile.
  4.  Real account blocked when KYC not_started.
  5.  Real account blocked when KYC pending.
  6.  Real account blocked when KYC rejected.
  7.  Blocked real account: no TradingAccount created.
  8.  Blocked real account: wallet balance unchanged.
  9.  Blocked real account: redirect to accounts page (302).
  10. Blocked real account: acct_error contains "KYC".
  11. KYC approved: real account created.
  12. KYC approved: wallet debited.
  13. Banner shown on /accounts/open/ when KYC not approved (real products exist).
  14. Banner contains /kyc/ link.
  15. Banner NOT shown when KYC approved.
  16. Banner NOT shown when only demo products exist (no real products).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from simulator.models import AccountProduct, KYCProfile, TradingAccount
from simulator.tests.factories import make_user, make_wallet

User = get_user_model()

ACCOUNTS_URL = "/accounts/"
OPEN_URL     = "/accounts/open/"
CREATE_URL   = "/accounts/create/"


def _make_product(code="kyc-real", product_type=AccountProduct.TYPE_STANDARD,
                  family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("10.00")):
    return AccountProduct.objects.create(
        code=code, name="KYC Real Test", product_type=product_type,
        family=family, min_deposit=min_deposit, default_balance=Decimal("0"),
        max_leverage=100, typical_spread_pips=Decimal("1.0"),
        commission_per_lot=Decimal("0"), sort_order=10, is_active=True,
    )


def _make_demo_product(code="kyc-demo"):
    return AccountProduct.objects.create(
        code=code, name="KYC Demo Test", product_type=AccountProduct.TYPE_DEMO,
        family=AccountProduct.FAMILY_DEMO, min_deposit=Decimal("0"),
        default_balance=Decimal("10000"), max_leverage=100,
        typical_spread_pips=Decimal("1.0"), commission_per_lot=Decimal("0"),
        sort_order=5, is_active=True,
    )


def _set_kyc(user, status):
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status = status
    kyc.legal_name = "Test User"
    kyc.country = "Venezuela"
    kyc.document_type = "national_id"
    kyc.save()
    return kyc


class KYCDemoNoGateTests(TestCase):
    """Demo accounts must never require KYC."""

    def setUp(self):
        self.user    = make_user()
        self.wallet  = make_wallet(self.user, initial_balance=Decimal("200"))
        self.product = _make_demo_product()
        self.client.force_login(self.user)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_without_kyc_profile(self, _email):
        r = self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_with_pending_kyc(self, _email):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_with_rejected_kyc(self, _email):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)


class KYCRealGateBlockedTests(TestCase):
    """All non-approved KYC states block real account creation."""

    def setUp(self):
        self.user    = make_user()
        self.wallet  = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = _make_product()
        self.client.force_login(self.user)

    def _post(self, amount="50"):
        return self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": amount})

    def test_no_kyc_blocks_real_account(self):
        r = self._post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_kyc_not_started_blocks_real_account(self):
        _set_kyc(self.user, KYCProfile.STATUS_NOT_STARTED)
        r = self._post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_kyc_pending_blocks_real_account(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self._post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_kyc_rejected_blocks_real_account(self):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        r = self._post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_kyc_blocked_wallet_balance_unchanged(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self._post()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500"))

    def test_no_kyc_wallet_balance_unchanged(self):
        self._post()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500"))

    def test_kyc_blocked_sets_acct_error(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self._post()
        self.assertIn("acct_error", r.wsgi_request.session)

    def test_kyc_blocked_error_contains_kyc(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self._post()
        self.assertIn("KYC", r.wsgi_request.session.get("acct_error", ""))

    def test_kyc_blocked_redirects_to_accounts(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self._post()
        self.assertRedirects(r, ACCOUNTS_URL, fetch_redirect_response=False)


class KYCRealGateApprovedTests(TestCase):
    """KYC-approved users can create real accounts."""

    def setUp(self):
        self.user    = make_user()
        self.wallet  = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = _make_product(code="kyc-approved-real")
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        self.client.force_login(self.user)

    @patch("simulator.tasks.send_email_async")
    def test_approved_kyc_creates_real_account(self, _email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_approved_kyc_debits_wallet(self, _email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("450"))


class KYCAccountBannerTests(TestCase):
    """Banner on /accounts/open/ informs users about KYC requirement."""

    def setUp(self):
        self.user    = make_user()
        make_wallet(self.user)
        self.client.force_login(self.user)

    def test_banner_shown_when_no_kyc_and_real_products_exist(self):
        _make_product(code="banner-real")
        resp = self.client.get(OPEN_URL)
        self.assertContains(resp, "Verificación KYC requerida para cuentas reales")

    def test_banner_contains_kyc_link(self):
        _make_product(code="banner-link-real")
        resp = self.client.get(OPEN_URL)
        self.assertContains(resp, "/kyc/")

    def test_banner_shown_for_pending_kyc(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        _make_product(code="banner-pending-real")
        resp = self.client.get(OPEN_URL)
        self.assertContains(resp, "Verificación KYC requerida para cuentas reales")

    def test_banner_not_shown_when_kyc_approved(self):
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        _make_product(code="banner-approved-real")
        resp = self.client.get(OPEN_URL)
        self.assertNotIn("Verificación KYC requerida para cuentas reales", resp.content.decode())

    def test_banner_not_shown_when_no_real_products(self):
        _make_demo_product(code="banner-demo-only")
        resp = self.client.get(OPEN_URL)
        self.assertNotIn("Verificación KYC requerida para cuentas reales", resp.content.decode())
