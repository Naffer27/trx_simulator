# simulator/tests/test_withdraw_2fa.py
"""
2FA validation for withdrawal requests.

WITHDRAWAL-SECURITY-EXTENSION-01 note: the withdrawal flow is now two
steps (POST /withdraw/ creates a WithdrawalEmailOTPChallenge; POST
/withdraw/otp/ with the email code creates the actual WithdrawalRequest +
debits the wallet). The 2FA (TOTP) gate itself is UNCHANGED — still
checked at step 1, in the same order, before anything else — so "blocked"
here means "no challenge created" and "allowed" means "challenge created,
and completing the email-OTP step then produces the WithdrawalRequest".

Covers:
  1. User without a confirmed TOTPDevice cannot withdraw (no challenge created).
  2. User with a confirmed TOTPDevice and a currently valid TOTP code can
     proceed to step 1 (challenge created), and completing step 2 creates
     the WithdrawalRequest and debits the wallet.
  3. Invalid (wrong) TOTP code rejects the withdrawal at step 1.
  4. b64:-prefixed secret is decoded correctly by verify_totp_code.
"""
import base64
from decimal import Decimal
from unittest.mock import patch

import pyotp

from django.test import TestCase
from django.utils import timezone

from simulator.models import TOTPDevice, WithdrawalEmailOTPChallenge, WithdrawalRequest
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_EMAIL as _PATCH_OTP_EMAIL, full_withdraw_flow
from simulator.two_factor import verify_totp, verify_totp_code

WITHDRAW_URL = "/withdraw/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")

# A known base32 secret used for deterministic tests.
_RAW_SECRET = pyotp.random_base32()
_B64_SECRET = "b64:" + base64.b64encode(_RAW_SECRET.encode()).decode()


def _make_confirmed_device(user, secret: str = _B64_SECRET) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        secret=secret,
        confirmed=True,
        confirmed_at=timezone.now(),
    )


def _valid_code(raw_secret: str = _RAW_SECRET) -> str:
    """Return the current TOTP code for *raw_secret*."""
    return pyotp.TOTP(raw_secret).now()


def _wr_payload(otp_code: str = "000000", amount: str = "1500.00", wallet_pk=None) -> dict:
    return {
        "amount_usd":      amount,
        "crypto_currency": "usdttrc20",
        "wallet_address":  str(wallet_pk) if wallet_pk is not None else "",
        "otp_code":        otp_code,
    }


# ── 1. No device → withdrawal blocked ────────────────────────────────────────

class NoDeviceGateTest(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL.start()
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()

    def test_no_confirmed_device_blocks_withdrawal(self):
        """No TOTPDevice confirmed=True → 200 with 2FA error, no challenge created."""
        r = self.client.post(WITHDRAW_URL, _wr_payload(wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "2FA")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_unconfirmed_device_also_blocks_withdrawal(self):
        """TOTPDevice with confirmed=False counts as no device."""
        TOTPDevice.objects.create(
            user=self.user,
            secret=_B64_SECRET,
            confirmed=False,
        )
        r = self.client.post(WITHDRAW_URL, _wr_payload(wallet_pk=self.vw.pk))
        self.assertContains(r, "2FA")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)


# ── 2. Valid TOTP code → challenge created, and completing it withdraws ──────

class ValidCodeTest(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL.start()
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        _make_confirmed_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()

    def test_valid_current_code_creates_challenge_not_wr_yet(self):
        """Real current TOTP code → step 1 creates a challenge, no WR/debit yet."""
        code = _valid_code()
        r = self.client.post(WITHDRAW_URL, _wr_payload(otp_code=code, wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 302)  # redirects to /withdraw/otp/
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_valid_code_then_email_otp_creates_pending_wr_and_debits(self):
        """Real current TOTP code, then the email OTP step → WR PENDING, wallet debited."""
        code = _valid_code()
        with _PATCH_OTP_EMAIL:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500.00"),
                totp_code=code,
            )
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.filter(user=self.user).first()
        self.assertIsNotNone(wr)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("3500.00"))


# ── 3. Invalid code → withdrawal rejected ────────────────────────────────────

class InvalidCodeTest(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL.start()
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        _make_confirmed_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()

    def test_wrong_code_rejects_withdrawal(self):
        """Incorrect 6-digit code → 200 with error, no challenge created."""
        r = self.client.post(WITHDRAW_URL, _wr_payload(otp_code="000000", wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "2FA")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_wrong_code_does_not_debit_wallet(self):
        """Incorrect code → wallet balance unchanged."""
        self.client.post(WITHDRAW_URL, _wr_payload(otp_code="000000", wallet_pk=self.vw.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_empty_code_rejects_withdrawal(self):
        """Empty otp_code field → rejected, no challenge created."""
        payload = _wr_payload(otp_code="", wallet_pk=self.vw.pk)
        r = self.client.post(WITHDRAW_URL, payload)
        self.assertContains(r, "2FA")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)


# ── 4. b64: secret decodes correctly ─────────────────────────────────────────

class B64SecretTest(TestCase):
    """Unit tests for the b64: prefix handling in verify_totp_code and verify_totp."""

    def test_b64_prefix_decoded_correctly(self):
        """verify_totp_code handles b64:-prefixed secrets correctly."""
        raw    = pyotp.random_base32()
        stored = "b64:" + base64.b64encode(raw.encode()).decode()
        code   = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp_code(stored, code))

    def test_b64_wrong_code_returns_false(self):
        """Wrong code against a b64: secret returns False."""
        raw    = pyotp.random_base32()
        stored = "b64:" + base64.b64encode(raw.encode()).decode()
        self.assertFalse(verify_totp_code(stored, "000000"))

    def test_verify_totp_uses_b64_device(self):
        """verify_totp(user, code) resolves a b64: device and returns True for valid code."""
        user   = make_user()
        raw    = pyotp.random_base32()
        stored = "b64:" + base64.b64encode(raw.encode()).decode()
        TOTPDevice.objects.create(
            user=user, secret=stored, confirmed=True, confirmed_at=timezone.now()
        )
        code = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp(user, code))

    def test_verify_totp_returns_false_for_no_device(self):
        """verify_totp(user, code) returns False when no confirmed device exists."""
        user = make_user()
        self.assertFalse(verify_totp(user, "123456"))

    def test_raw_base32_secret_also_works(self):
        """Legacy raw base32 secret (no prefix) still verifies correctly."""
        raw  = pyotp.random_base32()
        code = pyotp.TOTP(raw).now()
        self.assertTrue(verify_totp_code(raw, code))
