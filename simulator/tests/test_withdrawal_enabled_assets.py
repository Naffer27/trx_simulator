# simulator/tests/test_withdrawal_enabled_assets.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — settings.WALLET_WITHDRAWAL_ENABLED_ASSETS
is a NEW, Wallet-withdrawal-only allowlist, deliberately separate from
currencies.py's WITHDRAWAL_CURRENCY_MAP (which the FUNDED_INTERNAL account
flow also consumes, at a completely different call site) — see Design Lock
section N.

Covers:
  1.  Default config: only usdttrc20 offered in the Wallet withdraw form; BTC absent.
  2.  BTC is rejected as a Wallet withdrawal crypto_currency by default (form invalid).
  3.  Overriding WALLET_WITHDRAWAL_ENABLED_ASSETS to include "btc" makes it
      selectable, and a real BTC withdrawal (with a verified BTC wallet) succeeds.
  4.  currencies.py's WITHDRAWAL_CURRENCY_MAP / WITHDRAWAL_CHOICES are
      COMPLETELY UNCHANGED regardless of WALLET_WITHDRAWAL_ENABLED_ASSETS —
      still exposes btc/eth/usdttrc20/sol/bnbbsc.
  5.  The unrelated FUNDED_INTERNAL flow (funded_payout_request_view) still
      accepts "eth" as crypto_currency even though "eth" is NOT in
      WALLET_WITHDRAWAL_ENABLED_ASSETS — proving the two allowlists are
      fully isolated, no regression from this block.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from simulator.challenge_engine import activate_challenge_enrollment, advance_to_funded, advance_to_phase2
from simulator.currencies import WITHDRAWAL_CURRENCY_MAP, WITHDRAWAL_CHOICES
from simulator.forms import WithdrawForm
from simulator.models import (
    ChallengeEnrollment, ChallengeProduct, FundedConfig, TOTPDevice, WithdrawalRequest,
)
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, full_withdraw_flow


class WalletWithdrawFormAssetTests(TestCase):
    def setUp(self):
        self.user = make_user()

    @override_settings(WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20"])
    def test_default_only_usdttrc20_offered(self):
        form = WithdrawForm(user=self.user)
        keys = [c[0] for c in form.fields["crypto_currency"].choices]
        self.assertEqual(keys, ["usdttrc20"])

    @override_settings(WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20"])
    def test_btc_not_accepted_by_default(self):
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        vw = make_verified_withdrawal_wallet(self.user, asset="BTC", network="BTC_MAINNET")
        make_wallet(self.user, initial_balance=Decimal("5000"))
        form = WithdrawForm(
            {"amount_usd": "1500", "crypto_currency": "btc", "wallet_address": str(vw.pk)},
            user=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("crypto_currency", form.errors)

    @override_settings(WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20", "btc"])
    def test_btc_enabled_via_settings_can_withdraw(self):
        self.client.force_login(self.user)
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        make_wallet(self.user, initial_balance=Decimal("5000"))
        vw = make_verified_withdrawal_wallet(self.user, asset="BTC", network="BTC_MAINNET")
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            r1, r2, challenge = full_withdraw_flow(
                self.client, self.user, verified_wallet=vw, amount_usd=Decimal("1500"),
                crypto_currency="btc",
            )
        self.assertIsNotNone(challenge)
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.crypto_currency, "btc")


@override_settings(WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20"])
class CurrenciesModuleIsolationTests(TestCase):
    """currencies.py itself is never touched by this block — confirm its
    contents are exactly as they were before, regardless of the new setting."""

    def test_withdrawal_currency_map_unchanged(self):
        self.assertIn("btc", WITHDRAWAL_CURRENCY_MAP)
        self.assertIn("eth", WITHDRAWAL_CURRENCY_MAP)
        self.assertIn("usdttrc20", WITHDRAWAL_CURRENCY_MAP)
        self.assertIn("sol", WITHDRAWAL_CURRENCY_MAP)
        self.assertIn("bnbbsc", WITHDRAWAL_CURRENCY_MAP)

    def test_withdrawal_choices_unchanged(self):
        keys = {c[0] for c in WITHDRAWAL_CHOICES}
        self.assertEqual(keys, {"btc", "eth", "usdttrc20", "sol", "bnbbsc"})


def _make_product():
    return ChallengeProduct.objects.create(
        name="H1-isolation-test", account_size=Decimal("10000.00"), price_usd=Decimal("99.00"),
        is_active=True,
        p1_profit_target_pct=Decimal("8.00"), p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"), p1_min_trading_days=0, p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"), p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"), p2_min_trading_days=0, p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"), max_open_positions=5, profit_split_pct=Decimal("80.00"),
    )


@override_settings(WALLET_WITHDRAWAL_ENABLED_ASSETS=["usdttrc20"], LOAD_TEST_MODE=True)
class FundedInternalRegressionTests(TestCase):
    """
    FUNDED_INTERNAL (funded_payout_request_view, views.py) validates
    crypto_currency against currencies.py's WITHDRAWAL_CHOICES directly —
    a completely different allowlist than WALLET_WITHDRAWAL_ENABLED_ASSETS.
    "eth" is deliberately NOT in WALLET_WITHDRAWAL_ENABLED_ASSETS above, to
    prove this endpoint doesn't care about that setting at all.
    """

    def setUp(self):
        self.user = make_user()
        from simulator.models import EmailVerification, TermsAcceptance, KYCProfile, TERMS_VERSION, RISK_DISCLOSURE_VERSION
        EmailVerification.objects.filter(user=self.user).update(verified=True)
        TermsAcceptance.objects.get_or_create(
            user=self.user, terms_version=TERMS_VERSION, risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
        )
        KYCProfile.objects.update_or_create(user=self.user, defaults={"status": KYCProfile.STATUS_APPROVED})
        TOTPDevice.objects.create(user=self.user, secret="FAKESECRETFORTEST", confirmed=True)

        product = _make_product()
        self.enrollment = ChallengeEnrollment.objects.create(user=self.user, product=product)
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()

        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fc.funded_type = FundedConfig.FUNDED_INTERNAL
        self.fc.min_payout_usd = Decimal("50.00")
        self.fc.min_trading_days = 0
        self.fc.save()

        initial = Decimal(str(self.account.initial_balance or self.account.balance))
        self.account.balance = initial + Decimal("200.00")
        self.account.save()

        self.client.login(username=self.user.username, password="testpass123")
        self.url = reverse("simulator:funded_payout_request")

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_eth_accepted_for_funded_internal_despite_wallet_allowlist(self, _mock):
        resp = self.client.post(self.url, {
            "enrollment_id": self.enrollment.pk,
            "otp_code": "123456",
            "crypto_currency": "eth",
            "wallet_address": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        })
        self.assertEqual(resp.status_code, 201, resp.content)
