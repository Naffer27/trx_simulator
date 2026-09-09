# simulator/tests/test_withdrawal_minimum.py
"""
Minimum withdrawal amount — MIN_WITHDRAWAL_USD (default $1,000 as of
WITHDRAWAL-SECURITY-EXTENSION-01; still fully configurable via settings/env).

WITHDRAWAL-SECURITY-EXTENSION-01 note: the withdrawal flow is now two
steps. The minimum-amount check itself is UNCHANGED in mechanism — still
enforced at step 1, against settings.MIN_WITHDRAWAL_USD — so "blocked"
means "no WithdrawalEmailOTPChallenge created" and "succeeds" means step 1
creates a challenge (completing the email-OTP step then creates the actual
WithdrawalRequest).

Covers:
  1.  POST with amount below minimum is blocked.
  2.  POST with amount exactly equal to minimum succeeds (challenge created).
  3.  POST with amount above minimum succeeds.
  4.  Blocked request does not debit wallet.
  5.  Blocked request does not create a WithdrawalEmailOTPChallenge.
  6.  Error message mentions the minimum amount.
  7.  MIN_WITHDRAWAL_USD is respected when overridden via settings.
  8.  GET /withdraw/ shows the minimum withdrawal amount in the page.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.models import WithdrawalEmailOTPChallenge, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet

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


def _wr_payload(amount, wallet_pk):
    return {
        "amount_usd":      str(amount),
        "crypto_currency": "usdttrc20",
        "wallet_address":  str(wallet_pk),
        "otp_code":        "000000",
    }


@override_settings(MIN_WITHDRAWAL_USD=25)
class MinimumWithdrawalTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="minwd@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def _post(self, amount):
        return self.client.post(WITHDRAW_URL, _wr_payload(amount, self.vw.pk))

    def test_amount_below_minimum_is_blocked(self):
        r = self._post("20.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_amount_equal_to_minimum_succeeds(self):
        r = self._post("25.00")
        self.assertEqual(r.status_code, 302)  # redirects to /withdraw/otp/
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)

    def test_amount_above_minimum_succeeds(self):
        r = self._post("100.00")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)

    def test_blocked_does_not_debit_wallet(self):
        self._post("10.00")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_blocked_does_not_create_challenge(self):
        self._post("10.00")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_error_message_mentions_minimum(self):
        r = self._post("10.00")
        self.assertContains(r, "25")

    @override_settings(MIN_WITHDRAWAL_USD=50)
    def test_custom_minimum_blocks_below_new_threshold(self):
        r = self._post("30.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    @override_settings(MIN_WITHDRAWAL_USD=50)
    def test_custom_minimum_allows_at_new_threshold(self):
        r = self._post("50.00")
        self.assertEqual(r.status_code, 302)

    def test_get_shows_minimum_withdrawal_amount(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "25")
        self.assertEqual(resp.context["min_withdrawal"], Decimal("25"))


class MinimumWithdrawalEndToEndTest(TestCase):
    """Confirms the full two-step flow at the DEFAULT $1,000 minimum still
    produces a real PENDING WithdrawalRequest — not just a challenge."""

    @override_settings(MIN_WITHDRAWAL_USD=1000)
    def test_1000_end_to_end_creates_pending_wr(self):
        from simulator.tests.withdrawal_flow_helpers import PATCH_EMAIL as _OTP_EMAIL, full_withdraw_flow

        user = make_user()
        wallet = make_wallet(user, initial_balance=Decimal("5000"))
        _make_device(user)
        make_kyc_approved(user)
        vw = make_verified_withdrawal_wallet(user)
        self.client.force_login(user)

        with _PATCH_TOTP, _PATCH_RATELIMIT, _OTP_EMAIL:
            r1, r2, challenge = full_withdraw_flow(self.client, user, verified_wallet=vw, amount_usd=Decimal("1000"))
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=user)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(wr.amount_usd, Decimal("1000.00"))
