# simulator/tests/test_withdrawal_wallet_registration.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — wallet registration/change flow through
the real HTTP endpoints (/withdraw/wallets/register/, /withdraw/wallets/otp/),
complementing test_verified_withdrawal_wallet.py's function-level unit tests.

Covers:
  1.  TOTP + email OTP + local address validation all required to register.
  2.  Wrong TOTP blocks registration (no challenge created).
  3.  Invalid address format blocks registration (form-level, before any OTP).
  4.  Correct flow creates a PENDING_COOLDOWN VerifiedWithdrawalWallet.
  5.  New wallet is NOT usable (not offered in WithdrawForm) while cooling down.
  6.  Old wallet remains usable while the new one cools down.
  7.  After cooldown elapses, the new wallet becomes usable and the old one
      is deactivated (lazy activation, triggered by visiting /withdraw/).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from simulator.models import TOTPDevice, VerifiedWithdrawalWallet
from simulator.tests.factories import (
    make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet,
    VALID_TRC20_ADDRESS, VALID_TRC20_ADDRESS_ALT,
)
from simulator.tests.withdrawal_flow_helpers import PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code
from simulator.models import WithdrawalEmailOTPChallenge


@override_settings(WALLET_ADDRESS_CHANGE_COOLDOWN_HOURS=24, WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20"])
class WalletRegistrationFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.client.force_login(self.user)

    def _register(self, address=VALID_TRC20_ADDRESS, asset="usdttrc20", otp_code="000000"):
        return self.client.post("/withdraw/wallets/register/", {
            "asset": asset, "address": address, "otp_code": otp_code,
        })

    def test_wrong_totp_blocks_registration(self):
        with PATCH_RATELIMIT, PATCH_EMAIL, patch("simulator.two_factor.verify_totp_code", return_value=False):
            self._register()
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.count(), 0)
        self.assertEqual(VerifiedWithdrawalWallet.objects.count(), 0)

    def test_invalid_address_blocks_registration(self):
        with PATCH_TOTP, PATCH_RATELIMIT, PATCH_EMAIL:
            resp = self._register(address="not-a-real-address")
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.count(), 0)
        self.assertContains(resp, "inv", status_code=200)  # form error re-rendered

    def test_full_flow_creates_pending_cooldown_wallet(self):
        with PATCH_TOTP, PATCH_RATELIMIT, PATCH_EMAIL, fixed_otp_code("123456") as code:
            self._register()
            challenge = WithdrawalEmailOTPChallenge.objects.filter(
                user=self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            ).first()
            self.assertIsNotNone(challenge)
            resp = self.client.post("/withdraw/wallets/otp/", {"challenge_id": challenge.id, "code": code})
        self.assertRedirects(resp, "/withdraw/wallets/", fetch_redirect_response=False)
        wallet = VerifiedWithdrawalWallet.objects.get(user=self.user)
        self.assertEqual(wallet.status, VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN)
        self.assertEqual(wallet.address, VALID_TRC20_ADDRESS)
        self.assertIsNotNone(wallet.cooldown_until)

    def test_new_wallet_not_usable_during_cooldown(self):
        with PATCH_TOTP, PATCH_RATELIMIT, PATCH_EMAIL, fixed_otp_code("123456") as code:
            self._register()
            challenge = WithdrawalEmailOTPChallenge.objects.filter(
                user=self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            ).first()
            self.client.post("/withdraw/wallets/otp/", {"challenge_id": challenge.id, "code": code})

        from simulator.forms import WithdrawForm
        form = WithdrawForm(user=self.user)
        self.assertEqual(list(form.fields["wallet_address"].choices), [])

    def test_old_wallet_stays_usable_during_new_cooldown(self):
        old = make_verified_withdrawal_wallet(
            self.user, asset="USDT", network="TRC20", address=VALID_TRC20_ADDRESS_ALT,
        )
        with PATCH_TOTP, PATCH_RATELIMIT, PATCH_EMAIL, fixed_otp_code("123456") as code:
            self._register(address=VALID_TRC20_ADDRESS)
            challenge = WithdrawalEmailOTPChallenge.objects.filter(
                user=self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            ).first()
            self.client.post("/withdraw/wallets/otp/", {"challenge_id": challenge.id, "code": code})

        # New registration is still PENDING_COOLDOWN — old wallet must
        # remain the one offered for withdrawal.
        from simulator.forms import WithdrawForm
        form = WithdrawForm(user=self.user)
        keys = [c[0] for c in form.fields["wallet_address"].choices]
        self.assertIn(str(old.pk), keys)
        self.assertEqual(len(keys), 1)

    def test_lazy_activation_on_visiting_withdraw_deactivates_old(self):
        old = make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20", address=VALID_TRC20_ADDRESS_ALT)
        with PATCH_TOTP, PATCH_RATELIMIT, PATCH_EMAIL, fixed_otp_code("123456") as code:
            self._register()
            challenge = WithdrawalEmailOTPChallenge.objects.filter(
                user=self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            ).first()
            self.client.post("/withdraw/wallets/otp/", {"challenge_id": challenge.id, "code": code})

        new_wallet = VerifiedWithdrawalWallet.objects.exclude(pk=old.pk).get(user=self.user)
        VerifiedWithdrawalWallet.objects.filter(pk=new_wallet.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1),
        )

        with PATCH_RATELIMIT:
            self.client.get("/withdraw/")  # triggers lazy activation via get_active_wallet()

        old.refresh_from_db()
        new_wallet.refresh_from_db()
        self.assertEqual(old.status, VerifiedWithdrawalWallet.STATUS_DEACTIVATED)
        self.assertEqual(new_wallet.status, VerifiedWithdrawalWallet.STATUS_ACTIVE)
