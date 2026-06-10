# simulator/tests/test_withdrawal_minimum.py
"""
Minimum withdrawal amount — MIN_WITHDRAWAL_USD (default $25).

Covers:
  1.  POST with amount below minimum is blocked.
  2.  POST with amount exactly equal to minimum succeeds.
  3.  POST with amount above minimum succeeds.
  4.  Blocked request does not debit wallet.
  5.  Blocked request does not create a WithdrawalRequest.
  6.  Error message mentions the minimum amount.
  7.  MIN_WITHDRAWAL_USD is respected when overridden via settings.
  8.  GET /withdraw/ shows the minimum withdrawal amount in the page.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.models import Wallet, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved

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


def _wr_payload(amount):
    return {
        "amount_usd":      str(amount),
        "crypto_currency": "btc",
        "wallet_address":  "bc1qtest000000000000000000000000000000000",
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
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def test_amount_below_minimum_is_blocked(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("20.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_amount_equal_to_minimum_succeeds(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("25.00"))
        self.assertRedirects(r, "/withdraw/history/", fetch_redirect_response=False)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)

    def test_amount_above_minimum_succeeds(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("100.00"))
        self.assertRedirects(r, "/withdraw/history/", fetch_redirect_response=False)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)

    def test_blocked_does_not_debit_wallet(self):
        self.client.post(WITHDRAW_URL, _wr_payload("10.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_blocked_does_not_create_wr(self):
        self.client.post(WITHDRAW_URL, _wr_payload("10.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_error_message_mentions_minimum(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("10.00"))
        self.assertContains(r, "25")

    @override_settings(MIN_WITHDRAWAL_USD=50)
    def test_custom_minimum_blocks_below_new_threshold(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("30.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    @override_settings(MIN_WITHDRAWAL_USD=50)
    def test_custom_minimum_allows_at_new_threshold(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertRedirects(r, "/withdraw/history/", fetch_redirect_response=False)

    def test_get_shows_minimum_withdrawal_amount(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "25")
        self.assertEqual(resp.context["min_withdrawal"], Decimal("25"))
