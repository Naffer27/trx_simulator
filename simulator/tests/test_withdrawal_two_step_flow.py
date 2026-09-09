# simulator/tests/test_withdrawal_two_step_flow.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — end-to-end two-step withdrawal flow.

POST /withdraw/ creates a WithdrawalEmailOTPChallenge (no WithdrawalRequest,
no debit yet). POST /withdraw/otp/ with the correct code creates the
WithdrawalRequest from the challenge's FROZEN payload and debits the wallet
— exactly once.

Covers:
  1.  Step 1 does not create a WithdrawalRequest and does not debit the wallet.
  2.  Step 1 creates exactly one PENDING WithdrawalEmailOTPChallenge.
  3.  Step 2 with the correct code creates exactly one WithdrawalRequest.
  4.  Step 2 debits the wallet exactly once, by the frozen amount.
  5.  The WithdrawalRequest fields match the challenge's frozen payload,
      not anything re-derivable from the second POST.
  6.  otp_challenge FK on the WithdrawalRequest points at the challenge.
  7.  The challenge ends in status=USED with used_at set.
  8.  A wrong code does not create a WithdrawalRequest and does not debit.
  9.  provider submission (submit_withdrawal_to_provider) is never called
      by either step — that's admin-only, later.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.models import (
    Wallet, WalletTransaction, WithdrawalEmailOTPChallenge, WithdrawalRequest,
)
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import (
    PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, full_withdraw_flow, latest_challenge,
)


@override_settings(MIN_WITHDRAWAL_USD=1000)
class WithdrawalTwoStepFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        from simulator.models import TOTPDevice
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_step1_does_not_create_withdrawal_request_or_debit(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, patch("simulator.withdrawal_otp.generate_code", return_value="123456"):
            self.client.post("/withdraw/", {
                "amount_usd": "1500", "crypto_currency": "usdttrc20",
                "wallet_address": str(self.vw.pk), "otp_code": "000000",
            })
        self.assertEqual(WithdrawalRequest.objects.count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_step1_creates_exactly_one_pending_challenge(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, patch("simulator.withdrawal_otp.generate_code", return_value="123456"):
            self.client.post("/withdraw/", {
                "amount_usd": "1500", "crypto_currency": "usdttrc20",
                "wallet_address": str(self.vw.pk), "otp_code": "000000",
            })
        challenges = WithdrawalEmailOTPChallenge.objects.filter(user=self.user)
        self.assertEqual(challenges.count(), 1)
        self.assertEqual(challenges.first().status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_step2_correct_code_creates_exactly_one_withdrawal_request(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500"),
            )
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)

    def test_step2_debits_wallet_exactly_once_by_frozen_amount(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("3500"))
        debit_txs = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW)
        self.assertEqual(debit_txs.count(), 1)
        self.assertEqual(debit_txs.first().amount, Decimal("-1500"))

    def test_withdrawal_request_matches_frozen_challenge_payload(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1500.00"))
        self.assertEqual(wr.crypto_currency, "usdttrc20")
        self.assertEqual(wr.wallet_address, self.vw.address)
        self.assertIsNotNone(wr.otp_challenge_id)

    def test_challenge_ends_used_after_successful_flow(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500"))
        challenge = latest_challenge(self.user)
        self.assertEqual(challenge.status, WithdrawalEmailOTPChallenge.STATUS_USED)
        self.assertIsNotNone(challenge.used_at)
        self.assertIsNotNone(challenge.verified_at)

    def test_wrong_code_does_not_create_request_or_debit(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, patch("simulator.withdrawal_otp.generate_code", return_value="123456"):
            self.client.post("/withdraw/", {
                "amount_usd": "1500", "crypto_currency": "usdttrc20",
                "wallet_address": str(self.vw.pk), "otp_code": "000000",
            })
            challenge = latest_challenge(self.user)
            self.client.post("/withdraw/otp/", {"challenge_id": challenge.id, "code": "000000"})
        self.assertEqual(WithdrawalRequest.objects.count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_provider_submission_never_called_by_either_step(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, \
             patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as mock_submit:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500"))
        mock_submit.assert_not_called()
