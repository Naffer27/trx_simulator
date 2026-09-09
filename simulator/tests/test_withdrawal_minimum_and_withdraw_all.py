# simulator/tests/test_withdrawal_minimum_and_withdraw_all.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — rules 1/2/7 (minimum + Withdraw All),
driven through the real two-step HTTP flow.

Covers:
  1.  Manual partial $999 -> rejected (no challenge, no WithdrawalRequest).
  2.  Manual partial $1000 -> accepted.
  3.  Manual partial $1000.01 -> accepted.
  4.  Withdraw All with balance < $1000 -> allowed, debits EXACTLY available_balance.
  5.  Withdraw All with balance >= $1000 -> ALSO allowed (Design Lock Correction 1
      — Withdraw All is independent of the $1000 floor either way).
  6.  Withdraw All ignores any manually-typed amount_usd — always uses the
      fresh Wallet.available_balance read at verify time, not a stale value.
  7.  Withdraw All debits use TradingAccount.balance NEVER — only Wallet.available_balance.
  8.  Balance moves between challenge creation and OTP verification (a
      deposit lands in between) — the debited amount reflects the FRESH
      balance at verify time, not the snapshot taken when the challenge
      was created.
"""
from decimal import Decimal

from django.test import TestCase, override_settings

from simulator.models import TOTPDevice, WalletTransaction, WithdrawalRequest
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, full_withdraw_flow
from simulator.wallet_ledger import credit_wallet


@override_settings(MIN_WITHDRAWAL_USD=1000)
class MinimumWithdrawalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_manual_999_rejected(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("999"))
        self.assertIsNone(challenge)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_manual_1000_accepted(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1000"))
        self.assertIsNotNone(challenge)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1000.00"))
        self.assertEqual(wr.required_approvals, 1)

    def test_manual_1000_01_accepted_needs_two_approvals(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1000.01"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 2)


@override_settings(MIN_WITHDRAWAL_USD=1000)
class WithdrawAllTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_withdraw_all_below_minimum_allowed_exact_balance(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("400"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("400.00"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_withdraw_all_at_or_above_minimum_also_allowed(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("5000.00"))
        self.assertEqual(wr.required_approvals, 2)

    def test_withdraw_all_ignores_manual_amount_field(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("777"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=self.vw, withdraw_all=True, amount_usd=Decimal("50"),
            )
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("777.00"))

    def test_withdraw_all_uses_fresh_balance_not_stale_snapshot(self):
        """A deposit lands AFTER the challenge is created but BEFORE the
        OTP is verified — the debit must reflect the balance at verify
        time, not whatever was in the wallet when the challenge was made."""
        from simulator.tests.withdrawal_flow_helpers import fixed_otp_code, submit_withdraw_request, submit_withdraw_otp

        wallet = make_wallet(self.user, initial_balance=Decimal("400"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code("123456") as code:
            r1, challenge = submit_withdraw_request(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
            self.assertIsNotNone(challenge)
            # Balance moves AFTER the challenge exists.
            credit_wallet(wallet.id, Decimal("600"), WalletTransaction.TX_DEPOSIT, note="late deposit")
            r2 = submit_withdraw_otp(self.client, challenge, code)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1000.00"))  # 400 + 600, not the stale 400
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_withdraw_all_never_touches_trading_account_balance(self):
        from simulator.models import TradingAccount
        wallet = make_wallet(self.user, initial_balance=Decimal("400"))
        account = TradingAccount.objects.create(
            user=self.user, account_type="RETAIL", tier="10K",
            balance=Decimal("9999"), equity=Decimal("9999"), peak_balance=Decimal("9999"),
            initial_balance=Decimal("9999"), status="Activo", leverage=50,
        )
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("9999"))  # untouched
