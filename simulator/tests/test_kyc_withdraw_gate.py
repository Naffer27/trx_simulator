# simulator/tests/test_kyc_withdraw_gate.py
"""
KYC gate on withdrawals — ensures only KYC-approved users can initiate
withdrawals and that the banner is shown when KYC is not approved.

WITHDRAWAL-SECURITY-EXTENSION-01 note: the withdrawal flow is now two
steps. The KYC gate itself is UNCHANGED — still checked at step 1, before
anything else — so "blocked" means "no WithdrawalEmailOTPChallenge
created" and "approved" means step 1 creates a challenge (WithdrawalRequest
+ debit only happen after the email-OTP step).

Covers:
  1.  No KYC profile → blocked, no challenge, wallet unchanged.
  2.  KYC status not_started → blocked.
  3.  KYC status pending → blocked.
  4.  KYC status rejected → blocked.
  5.  Any non-approved status → wallet balance unchanged.
  6.  Any non-approved status → no WithdrawalEmailOTPChallenge created.
  7.  KYC approved → passes gate, challenge created; completing the email
      OTP step then creates the WithdrawalRequest as PENDING.
  8.  KYC approved (full flow) → wallet debited.
  9.  Banner shown on GET when KYC not approved (no profile).
  10. Banner shown on GET when KYC pending.
  11. Banner contains /kyc/ link.
  12. Banner NOT shown when KYC approved.
  13. POST with blocked KYC → returns 200 with KYC error message.
  14. POST with blocked KYC → returns 200, not redirect.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from simulator.models import KYCProfile, WithdrawalEmailOTPChallenge, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_EMAIL as _PATCH_OTP_EMAIL, full_withdraw_flow

User = get_user_model()

WITHDRAW_URL = "/withdraw/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")


def _make_device(user) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
        confirmed=True,
    )


def _wr_payload(amount="1500.00", wallet_pk=""):
    return {
        "amount_usd":      amount,
        "crypto_currency": "usdttrc20",
        "wallet_address":  str(wallet_pk),
        "otp_code":        "000000",
    }


def _set_kyc(user, status):
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status = status
    kyc.legal_name = "Test User"
    kyc.country = "Venezuela"
    kyc.document_type = "national_id"
    kyc.save()
    return kyc


class KYCGateBlockedTests(TestCase):
    """All non-approved KYC states block the withdrawal POST."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def _post(self):
        return self.client.post(WITHDRAW_URL, _wr_payload(wallet_pk=self.vw.pk))

    def test_no_kyc_profile_blocks_withdrawal(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_kyc_not_started_blocks_withdrawal(self):
        _set_kyc(self.user, KYCProfile.STATUS_NOT_STARTED)
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_kyc_pending_blocks_withdrawal(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_kyc_rejected_blocks_withdrawal(self):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_kyc_blocked_wallet_balance_unchanged(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self._post()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_no_kyc_wallet_balance_unchanged(self):
        self._post()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_kyc_rejected_no_wr_created(self):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        self._post()
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_kyc_blocked_returns_200_not_redirect(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        self.assertEqual(self._post().status_code, 200)

    def test_kyc_blocked_shows_kyc_in_error(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        r = self._post()
        self.assertContains(r, "KYC")


class KYCGateApprovedTests(TestCase):
    """KYC-approved users pass the gate and proceed to create WR."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def test_approved_kyc_creates_challenge(self):
        self.client.post(WITHDRAW_URL, _wr_payload(wallet_pk=self.vw.pk))
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_approved_kyc_full_flow_creates_pending_wr_and_debits(self):
        with _PATCH_OTP_EMAIL:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500.00"),
            )
        wr = WithdrawalRequest.objects.filter(user=self.user).first()
        self.assertIsNotNone(wr)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("3500.00"))


class KYCBannerTests(TestCase):
    """KYC banner is shown when KYC is not approved; hidden when approved."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("100"))
        self.client.force_login(self.user)

    def test_banner_shown_on_get_when_no_kyc(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Verificación KYC requerida")

    def test_banner_contains_kyc_link(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "/kyc/")

    def test_banner_shown_for_pending_kyc(self):
        _set_kyc(self.user, KYCProfile.STATUS_PENDING)
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "Verificación KYC requerida")

    def test_banner_shown_for_rejected_kyc(self):
        _set_kyc(self.user, KYCProfile.STATUS_REJECTED)
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "Verificación KYC requerida")

    def test_banner_not_shown_for_approved_kyc(self):
        _set_kyc(self.user, KYCProfile.STATUS_APPROVED)
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("Verificación KYC requerida", resp.content.decode())
