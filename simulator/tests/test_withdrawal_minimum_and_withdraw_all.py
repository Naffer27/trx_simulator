# simulator/tests/test_withdrawal_minimum_and_withdraw_all.py
"""
WITHDRAWAL-POLICY-CORRECTION-01 — no fixed minimum + Withdraw All, driven
through the real two-step HTTP flow.

Covers:
  1.  Manual partial $999 -> accepted, auto-authorized (no fixed minimum).
  2.  Manual partial $1000 -> accepted, auto-authorized.
  3.  Manual partial $1000.01 -> accepted, needs two approvals.
  4.  Withdraw All with balance < $1000 -> allowed, debits EXACTLY available_balance.
  5.  Withdraw All with balance >= $1000 -> ALSO allowed (Withdraw All is
      independent of any amount threshold — always the full balance).
  6.  Withdraw All ignores any manually-typed amount_usd — always uses the
      fresh Wallet.available_balance read at verify time, not a stale value.
  7.  Withdraw All debits use TradingAccount.balance NEVER — only Wallet.available_balance.
  8.  Balance moves between challenge creation and OTP verification (a
      deposit lands in between) — the debited amount reflects the FRESH
      balance at verify time, not the snapshot taken when the challenge
      was created.
"""
from decimal import Decimal

from django.test import TestCase

from simulator.models import TOTPDevice, WalletTransaction, WithdrawalRequest
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, full_withdraw_flow
from simulator.wallet_ledger import credit_wallet


class MinimumWithdrawalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_manual_999_accepted_auto_authorized(self):
        """WITHDRAWAL-POLICY-CORRECTION-01: no fixed minimum — $999 <= $1000 is auto-authorized."""
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("999"))
        self.assertIsNotNone(challenge)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("999.00"))
        self.assertEqual(wr.required_approvals, 1)
        self.assertIsNone(wr.reviewed_by)

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
