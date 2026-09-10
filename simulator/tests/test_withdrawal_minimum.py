# simulator/tests/test_withdrawal_minimum.py
"""
WITHDRAWAL-POLICY-CORRECTION-01 — no fixed minimum withdrawal amount.

MIN_WITHDRAWAL_USD (formerly a fixed $1,000 Money Broker policy, set in
WITHDRAWAL-SECURITY-EXTENSION-01) has been removed entirely — deliberately,
not repurposed. The only remaining floor is WithdrawForm.amount_usd's own
min_value=Decimal("0.01"), a pure form-layer validator, not a business
policy gate.

Covers:
  1.  $0 / negative amounts are rejected by the form (min_value=0.01).
  2.  $0.01 is accepted (smallest possible positive amount).
  3.  Small amounts well under the old $1,000 threshold (e.g. $20) succeed
      end-to-end and are NOT blocked by any minimum.
  4.  GET /withdraw/ no longer shows any "minimum withdrawal" figure.
"""
from decimal import Decimal

from django.test import TestCase

from simulator.forms import WithdrawForm
from simulator.models import WithdrawalEmailOTPChallenge, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import (
    PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, full_withdraw_flow,
)

WITHDRAW_URL = "/withdraw/"


def _make_device(user) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
        confirmed=True,
    )


class FormLevelFloorTests(TestCase):
    """WithdrawForm.amount_usd min_value=Decimal("0.01") — the only remaining floor."""

    def setUp(self):
        self.user = make_user(email="floortest@test.com")
        make_kyc_approved(self.user)
        _make_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)

    def test_zero_amount_rejected_by_form(self):
        form = WithdrawForm(
            {"amount_usd": "0.00", "crypto_currency": "usdttrc20",
             "wallet_address": str(self.vw.pk)},
            user=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("amount_usd", form.errors)

    def test_negative_amount_rejected_by_form(self):
        form = WithdrawForm(
            {"amount_usd": "-5.00", "crypto_currency": "usdttrc20",
             "wallet_address": str(self.vw.pk)},
            user=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("amount_usd", form.errors)

    def test_0_01_accepted_by_form(self):
        form = WithdrawForm(
            {"amount_usd": "0.01", "crypto_currency": "usdttrc20",
             "wallet_address": str(self.vw.pk)},
            user=self.user,
        )
        self.assertTrue(form.is_valid(), form.errors)


class NoFixedMinimumEndToEndTests(TestCase):
    """Small amounts, well under the old $1,000 threshold, succeed end-to-end."""

    def setUp(self):
        self.user   = make_user(email="nomin@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_20_dollars_succeeds_end_to_end(self):
        """$20 — far below the old $1,000 minimum — creates a real, auto-authorized WithdrawalRequest."""
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("20.00"),
            )
        self.assertIsNotNone(challenge)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("20.00"))
        self.assertEqual(wr.required_approvals, 1)

    def test_20_dollars_debits_wallet_exactly_once(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("20.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("4980.00"))

    def test_get_does_not_show_minimum_withdrawal_figure(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("min_withdrawal", resp.context)
