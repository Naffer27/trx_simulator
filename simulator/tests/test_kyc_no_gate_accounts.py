# simulator/tests/test_kyc_no_gate_accounts.py
"""
Business rule: KYC only blocks money-out flows (withdrawals, funded payouts).
Account opening — demo or real — must NOT require KYC.

Covers:
  1.  Demo account opens without any KYC profile.
  2.  Demo account opens with KYC pending.
  3.  Demo account opens with KYC rejected.
  4.  Real account opens without any KYC profile.
  5.  Real account opens with KYC not_started.
  6.  Real account opens with KYC pending.
  7.  Real account opens with KYC rejected.
  8.  Real account opens with KYC approved (sanity check).
  9.  No KYC banner on /accounts/open/.
  10. Withdrawal with no KYC is still blocked (regression guard).
  11. Withdrawal with pending KYC is still blocked (regression guard).
  12. Withdrawal with approved KYC succeeds (regression guard).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from simulator.models import AccountProduct, KYCProfile, TradingAccount, TOTPDevice, WithdrawalRequest
from simulator.tests.factories import make_user, make_wallet

User = get_user_model()

OPEN_URL    = "/accounts/open/"
CREATE_URL  = "/accounts/create/"
WITHDRAW_URL = "/withdraw/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")


def _make_real_product(code="no-kyc-real"):
    return AccountProduct.objects.create(
        code=code, name="No-KYC Real", product_type=AccountProduct.TYPE_STANDARD,
        family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("10.00"),
        default_balance=Decimal("0"), max_leverage=100,
        typical_spread_pips=Decimal("1.0"), commission_per_lot=Decimal("0"),
        sort_order=10, is_active=True,
    )


def _make_demo_product(code="no-kyc-demo"):
    return AccountProduct.objects.create(
        code=code, name="No-KYC Demo", product_type=AccountProduct.TYPE_DEMO,
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


def _make_device(user):
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
        confirmed=True,
    )


def _wr_payload(amount="50.00"):
    return {
        "amount_usd": amount, "crypto_currency": "btc",
        "wallet_address": "bc1qtest000000000000000000000000000000000",
        "otp_code": "000000",
    }


class DemoAccountNoKYCTests(TestCase):
    """Demo accounts open regardless of KYC status."""

    def setUp(self):
        self.user    = make_user()
        make_wallet(self.user)
        self.product = _make_demo_product()
        self.client.force_login(self.user)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_with_no_kyc_profile(self, _e):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_with_kyc_pending(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_demo_opens_with_kyc_rejected(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)


class RealAccountNoKYCTests(TestCase):
    """Real accounts open regardless of KYC status (only email+terms+balance required)."""

    def setUp(self):
        self.user    = make_user()
        self.wallet  = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = _make_real_product()
        self.client.force_login(self.user)

    @patch("simulator.tasks.send_email_async")
    def test_real_opens_without_kyc_profile(self, _e):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_opens_with_kyc_not_started(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_NOT_STARTED)
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_opens_with_kyc_pending(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_opens_with_kyc_rejected(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_opens_with_kyc_approved(self, _e):
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_debits_wallet_without_kyc(self, _e):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("450"))


class AccountOpenNoBannerTests(TestCase):
    """No KYC banner must appear on the account catalog page."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        _make_real_product(code="banner-check-real")
        self.client.force_login(self.user)

    def test_no_kyc_banner_on_open_page(self):
        resp = self.client.get(OPEN_URL)
        self.assertNotIn("Verificación KYC requerida", resp.content.decode())

    def test_kyc_link_not_forced_on_open_page(self):
        resp = self.client.get(OPEN_URL)
        # /kyc/ may still appear in sidebar nav — check the banner-specific text is absent
        self.assertNotIn("Verificación KYC requerida para cuentas reales", resp.content.decode())


class WithdrawalKYCGateRegressionTests(TestCase):
    """KYC gate MUST still block withdrawals — regression guard after account-gate revert."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        _make_device(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def test_withdrawal_blocked_without_kyc(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_blocked_with_kyc_pending(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self.client.post(WITHDRAW_URL, _wr_payload())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_blocked_wallet_unchanged(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.client.post(WITHDRAW_URL, _wr_payload())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200"))

    def test_withdrawal_succeeds_with_kyc_approved(self):
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        self.client.post(WITHDRAW_URL, _wr_payload())
        wr = WithdrawalRequest.objects.filter(user=self.user).first()
        self.assertIsNotNone(wr)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_kyc_banner_still_on_withdraw_page(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "Verificación KYC requerida")
