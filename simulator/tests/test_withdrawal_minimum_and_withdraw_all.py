# simulator/tests/test_withdrawal_minimum_and_withdraw_all.py
"""
WITHDRAWAL-POLICY-CORRECTION-02 — $20 minimum + Withdraw All, driven
through the real two-step HTTP flow.

Confirmed Owner-locked policy: minimum withdrawal = USD 20.00, no daily
cap, no maximum cap, no dust exception — Withdraw All does NOT bypass
the $20 minimum. required_approvals threshold (>$1,000 needs 2 approvals)
is untouched by this correction — covered here only as a regression
guard alongside the new $20 floor tests.

Covers:
  1.  Manual partial $999 -> accepted, auto-authorized (below $1,000).
  2.  Manual partial $1000 -> accepted, auto-authorized (boundary, regression guard).
  3.  Manual partial $1000.01 -> accepted, needs two approvals (regression guard).
  4.  Withdraw All with balance $19.99 -> REJECTED, no WithdrawalRequest
      created, wallet unchanged, no OTP challenge/email sent (early
      advisory check in withdraw_view fires before step 1 completes).
  5.  Withdraw All with balance $20.00 -> accepted, exact boundary.
  6.  Withdraw All with balance > $20 -> accepted normally, debits
      EXACTLY available_balance (unaffected by this correction).
  7.  Withdraw All ignores any manually-typed amount_usd -> always uses
      the fresh Wallet.available_balance read at verify time.
  8.  Withdraw All debits use TradingAccount.balance NEVER -> only
      Wallet.available_balance.
  9.  Balance INCREASES between challenge creation and OTP verification
      (a deposit lands in between) -> the debited amount reflects the
      FRESH balance at verify time, not the stale snapshot.
  10. Balance DECREASES below $20 between challenge creation and OTP
      verification -> the authoritative _finalize() gate rejects, even
      though the early advisory check passed at creation time (proves
      the backend gate, not the form/early check, is the real security
      boundary — see WITHDRAWAL-POLICY-CORRECTION-02 design lock).
"""
from decimal import Decimal

from django.test import TestCase

from simulator.models import TOTPDevice, WalletTransaction, WithdrawalRequest
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import (
    PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT,
    full_withdraw_flow, fixed_otp_code, submit_withdraw_request, submit_withdraw_otp,
)
from simulator.wallet_ledger import credit_wallet, debit_wallet


class ManualAmountApprovalThresholdTests(TestCase):
    """Regression guards only — WITHDRAWAL-POLICY-CORRECTION-02 does not
    touch the >$1,000 required_approvals threshold."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_manual_999_accepted_auto_authorized(self):
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


class WithdrawAllMinimumTests(TestCase):
    """Withdraw All does NOT bypass the $20 minimum — Owner rule 6/7
    (locked): no dust exception."""

    def setUp(self):
        self.user = make_user()
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_withdraw_all_19_99_rejected_no_request_no_debit_no_email(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("19.99"))
        with PATCH_TOTP, PATCH_EMAIL as mock_email, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        # Early advisory check in withdraw_view fires before any challenge
        # is created — no OTP step, no email, no WithdrawalRequest.
        self.assertIsNone(challenge)
        self.assertContains(r1, "El monto mínimo de retiro es $20.00 USD.")
        self.assertFalse(WithdrawalRequest.objects.filter(user=self.user).exists())
        mock_email.assert_not_called()
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("19.99"))

    def test_withdraw_all_20_00_accepted_exact_boundary(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("20.00"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        self.assertIsNotNone(challenge)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("20.00"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_withdraw_all_balance_drops_below_20_between_challenge_and_verify_rejected(self):
        """The authoritative gate is _finalize(), not the early advisory
        check — proven by passing the early check at creation time, then
        draining the wallet below $20 before OTP verification."""
        wallet = make_wallet(self.user, initial_balance=Decimal("50.00"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code("123456") as code:
            r1, challenge = submit_withdraw_request(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
            self.assertIsNotNone(challenge)  # early check passed at $50
            # Balance drops below $20 AFTER the challenge exists, BEFORE verify.
            debit_wallet(
                wallet.id, Decimal("35.00"), WalletTransaction.TX_CORRECTION,
                note="simulated concurrent withdrawal draining balance below $20",
            )
            r2 = submit_withdraw_otp(self.client, challenge, code)
        self.assertContains(r2, "El monto mínimo de retiro es $20.00 USD.")
        self.assertFalse(WithdrawalRequest.objects.filter(user=self.user).exists())
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("15.00"))  # only the $35 correction applied


class WithdrawAllTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_withdraw_all_above_20_allowed_exact_balance(self):
        wallet = make_wallet(self.user, initial_balance=Decimal("400"))
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, withdraw_all=True)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("400.00"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_withdraw_all_large_balance_also_allowed(self):
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
