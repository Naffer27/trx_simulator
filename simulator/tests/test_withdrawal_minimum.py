# simulator/tests/test_withdrawal_minimum.py
"""
WITHDRAWAL-POLICY-CORRECTION-02 — Owner-locked minimum withdrawal amount.

Confirmed business policy: minimum withdrawal = USD 20.00. USD 20.00
through USD 1,000.00 inclusive uses the normal automated security flow
(required_approvals=1, auto-authorized). Above USD 1,000.00 requires
additional internal review (required_approvals=2). No daily cap, no
maximum cap.

WithdrawForm.amount_usd's min_value=Decimal("20") (simulator/forms.py) is
a UX-layer convenience for manual entry — it is NOT the security
boundary. The authoritative, unbypassable gate — covering BOTH manual
entry and Withdraw All — is the final_amount < Decimal("20") check
inside withdraw_otp_verify_view's _finalize() (simulator/views.py),
which runs under the same select_for_update() lock as the balance read.
See test_withdrawal_minimum_and_withdraw_all.py for the Withdraw All
and race-condition coverage of that authoritative gate.

Covers:
  1.  $0 / negative amounts are rejected by the form.
  2.  $0.01 is rejected by the form (below the $20 floor).
  3.  $19.99 is rejected by the form (below the $20 floor).
  4.  $20.00 is accepted by the form (exact boundary).
  5.  $20.00 succeeds end-to-end, auto-authorized, debits exactly once.
  6.  $1,000.00 keeps the existing normal-approval behavior (regression
      guard — this correction does not touch the >$1,000 threshold).
  7.  $1,000.01 keeps the existing additional-approval behavior
      (regression guard).
  8.  GET /withdraw/ no longer shows any legacy "minimum withdrawal"
      figure key in context.
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


MIN_AMOUNT_MSG = "El monto mínimo de retiro es $20.00 USD."
AMOUNT_REQUIRED_MSG = "Monto requerido (o marca 'Retirar todo')."


class FormLevelFloorTests(TestCase):
    """WithdrawForm.amount_usd min_value=Decimal("20") — UX-layer floor.

    MANUAL-CERTIFICATION-FIX-01: below-minimum amounts must show ONLY the
    approved MIN_AMOUNT_MSG — never Django's default min_value text, and
    never the redundant AMOUNT_REQUIRED_MSG stacked on top of it (that
    message is reserved for the genuine "nothing submitted" case)."""

    def setUp(self):
        self.user = make_user(email="floortest@test.com")
        make_kyc_approved(self.user)
        _make_device(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)

    def _form(self, amount_str):
        return WithdrawForm(
            {"amount_usd": amount_str, "crypto_currency": "usdttrc20",
             "wallet_address": str(self.vw.pk)},
            user=self.user,
        )

    def test_zero_amount_rejected_by_form(self):
        form = self._form("0.00")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["amount_usd"], [MIN_AMOUNT_MSG])

    def test_negative_amount_rejected_by_form(self):
        form = self._form("-5.00")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["amount_usd"], [MIN_AMOUNT_MSG])

    def test_0_01_rejected_by_form_exact_message_no_duplicate(self):
        form = self._form("0.01")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["amount_usd"], [MIN_AMOUNT_MSG])

    def test_19_99_rejected_by_form_exact_message_no_duplicate(self):
        form = self._form("19.99")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["amount_usd"], [MIN_AMOUNT_MSG])

    def test_20_00_accepted_by_form(self):
        form = self._form("20.00")
        self.assertTrue(form.is_valid(), form.errors)

    def test_blank_amount_without_withdraw_all_shows_required_message(self):
        """The genuine "nothing submitted" case must still be caught —
        this is NOT the case the fix suppresses."""
        form = WithdrawForm(
            {"amount_usd": "", "crypto_currency": "usdttrc20",
             "wallet_address": str(self.vw.pk)},
            user=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["amount_usd"], [AMOUNT_REQUIRED_MSG])


class NoFixedMinimumEndToEndTests(TestCase):
    """$20 — the confirmed floor — succeeds end-to-end; the >$1,000
    approval threshold is unaffected by this correction (regression
    guard only, logic itself untouched)."""

    def setUp(self):
        self.user   = make_user(email="nomin@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_20_dollars_succeeds_end_to_end(self):
        """$20 — the exact policy floor — creates a real, auto-authorized WithdrawalRequest."""
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

    def test_1000_00_manual_keeps_normal_approval(self):
        """Regression guard: WITHDRAWAL-POLICY-CORRECTION-02 must not touch
        the existing >$1,000 approval threshold."""
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1000.00"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1000.00"))
        self.assertEqual(wr.required_approvals, 1)

    def test_1000_01_manual_keeps_additional_approval(self):
        """Regression guard: >$1,000 still requires the existing dual approval."""
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1000.01"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.amount_usd, Decimal("1000.01"))
        self.assertEqual(wr.required_approvals, 2)

    def test_get_does_not_show_minimum_withdrawal_figure(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("min_withdrawal", resp.context)
