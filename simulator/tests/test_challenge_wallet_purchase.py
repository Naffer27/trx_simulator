# simulator/tests/test_challenge_wallet_purchase.py
"""
WALLET_CHALLENGES.1 — Buy Challenges With Internal Wallet Balance.

Tests for challenge_wallet_purchase_view (POST /challenges/<id>/wallet-buy/).

NOTE on tx_type "CHALLENGE_FEE":
  WalletTransaction.TX_CHOICES does not include "CHALLENGE_FEE" yet; the field
  is a plain CharField with no DB CheckConstraint, so the value is stored and
  queried correctly. A future cleanup migration should add it to TX_CHOICES
  formally (no schema change needed, just choices update).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from simulator.models import (
    ChallengeEnrollment,
    RiskRule,
    TradingAccount,
    Wallet,
    WalletTransaction,
)
from simulator.tests.factories import (
    make_challenge_product,
    make_challenge_enrollment,
    make_user,
    make_wallet,
)
from simulator.wallet_ledger import reconcile_wallet

WALLET_BUY_URL = "/challenges/{product_id}/wallet-buy/"


def _url(product_id):
    return reverse("simulator:challenge_wallet_purchase", kwargs={"product_id": product_id})


def _login(client, user):
    client.force_login(user)


class WalletPurchaseSuccessTests(TestCase):
    """User has sufficient balance — happy path."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("150.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_sufficient_balance_redirects_to_accounts(self):
        r = self.client.post(_url(self.product.pk))
        self.assertRedirects(r, reverse("simulator:accounts"), fetch_redirect_response=False)

    def test_wallet_debited_by_exact_price(self):
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("350.00"))

    def test_wallet_transaction_uses_challenge_fee_tx_type(self):
        self.client.post(_url(self.product.pk))
        tx = WalletTransaction.objects.filter(wallet=self.wallet, tx_type="CHALLENGE_FEE").first()
        self.assertIsNotNone(tx, "Expected a WalletTransaction with tx_type='CHALLENGE_FEE'")
        self.assertEqual(tx.amount, Decimal("-150.00"))

    def test_wallet_transaction_amount_is_negative(self):
        self.client.post(_url(self.product.pk))
        tx = WalletTransaction.objects.get(wallet=self.wallet, tx_type="CHALLENGE_FEE")
        self.assertTrue(tx.amount < 0)

    def test_wallet_ledger_reconciles_after_purchase(self):
        self.client.post(_url(self.product.pk))
        result = reconcile_wallet(self.wallet.pk)
        self.assertTrue(result["ok"], f"Wallet reconciliation failed: drift={result['drift']}")

    def test_challenge_enrollment_created(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.filter(
            user=self.user, product=self.product
        ).first()
        self.assertIsNotNone(enrollment)

    def test_enrollment_status_is_phase_1(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_PHASE_1)

    def test_enrollment_deposit_is_none(self):
        """Wallet purchases always have deposit=None."""
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        self.assertIsNone(enrollment.deposit)

    def test_trading_account_created(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        self.assertIsNotNone(enrollment.phase1_account_id)

    def test_trading_account_type_is_challenge(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        account = enrollment.phase1_account
        self.assertEqual(account.account_type, "CHALLENGE")

    def test_trading_account_balance_equals_product_account_size(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        self.assertEqual(enrollment.phase1_account.initial_balance, self.product.account_size)

    def test_risk_rule_created_for_phase1_account(self):
        self.client.post(_url(self.product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=self.user, product=self.product)
        exists = RiskRule.objects.filter(account=enrollment.phase1_account).exists()
        self.assertTrue(exists, "RiskRule must exist for the Phase 1 account")

    def test_session_success_key_set(self):
        r = self.client.post(_url(self.product.pk))
        self.assertIn("challenge_success", self.client.session)


class WalletPurchaseInsufficientFundsTests(TestCase):
    """User does not have enough balance."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("200.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("50.00"))
        _login(self.client, self.user)

    def test_insufficient_balance_redirects_to_challenge_purchase(self):
        r = self.client.post(_url(self.product.pk))
        self.assertRedirects(
            r,
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk}),
            fetch_redirect_response=False,
        )

    def test_insufficient_balance_wallet_unchanged(self):
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("50.00"))

    def test_insufficient_balance_no_enrollment_created(self):
        self.client.post(_url(self.product.pk))
        count = ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count()
        self.assertEqual(count, 0)

    def test_insufficient_balance_no_trading_account_created(self):
        self.client.post(_url(self.product.pk))
        count = TradingAccount.objects.filter(user=self.user, account_type="CHALLENGE").count()
        self.assertEqual(count, 0)

    def test_insufficient_balance_sets_session_error(self):
        self.client.post(_url(self.product.pk))
        self.assertIn("challenge_error", self.client.session)

    def test_wallet_ledger_reconciles_after_failed_attempt(self):
        self.client.post(_url(self.product.pk))
        result = reconcile_wallet(self.wallet.pk)
        self.assertTrue(result["ok"])


class WalletPurchaseIdempotencyTests(TestCase):
    """Duplicate purchase attempts are blocked."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000.00"))
        _login(self.client, self.user)

    def test_double_click_creates_only_one_enrollment(self):
        self.client.post(_url(self.product.pk))
        self.client.post(_url(self.product.pk))
        count = ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count()
        self.assertEqual(count, 1)

    def test_double_click_debits_wallet_only_once(self):
        self.client.post(_url(self.product.pk))
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("900.00"))

    def test_existing_phase1_enrollment_blocks_purchase(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("1000.00"),
                         "Wallet must not be debited when an active enrollment exists")

    def test_existing_phase2_enrollment_blocks_purchase(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_PHASE_2,
        )
        r = self.client.post(_url(self.product.pk))
        self.assertRedirects(
            r,
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk}),
            fetch_redirect_response=False,
        )

    def test_existing_funded_enrollment_blocks_purchase(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_FUNDED,
        )
        count_before = ChallengeEnrollment.objects.filter(user=self.user).count()
        self.client.post(_url(self.product.pk))
        count_after = ChallengeEnrollment.objects.filter(user=self.user).count()
        self.assertEqual(count_before, count_after)

    def test_failed_enrollment_does_not_block_new_purchase(self):
        """A failed enrollment should not prevent a fresh purchase."""
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_FAILED,
        )
        self.client.post(_url(self.product.pk))
        new_count = ChallengeEnrollment.objects.filter(
            user=self.user, product=self.product, status=ChallengeEnrollment.ST_PHASE_1
        ).count()
        self.assertEqual(new_count, 1)


class WalletPurchaseAtomicRollbackTests(TestCase):
    """Activation failure rolls back the wallet debit."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_activation_failure_rolls_back_wallet_debit(self):
        with patch("simulator.views._ce_activate", side_effect=RuntimeError("activation boom")):
            self.client.post(_url(self.product.pk))

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.available_balance, Decimal("500.00"),
            "Wallet debit must roll back when activation raises an exception",
        )

    def test_activation_failure_creates_no_enrollment(self):
        with patch("simulator.views._ce_activate", side_effect=RuntimeError("boom")):
            self.client.post(_url(self.product.pk))

        count = ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count()
        self.assertEqual(count, 0)

    def test_activation_failure_no_challenge_fee_tx(self):
        with patch("simulator.views._ce_activate", side_effect=RuntimeError("boom")):
            self.client.post(_url(self.product.pk))

        tx_count = WalletTransaction.objects.filter(
            wallet=self.wallet, tx_type="CHALLENGE_FEE"
        ).count()
        self.assertEqual(tx_count, 0)

    def test_activation_failure_sets_session_error(self):
        with patch("simulator.views._ce_activate", side_effect=RuntimeError("boom")):
            self.client.post(_url(self.product.pk))
        self.assertIn("challenge_error", self.client.session)

    def test_wallet_reconciles_after_rollback(self):
        with patch("simulator.views._ce_activate", side_effect=RuntimeError("boom")):
            self.client.post(_url(self.product.pk))
        result = reconcile_wallet(self.wallet.pk)
        self.assertTrue(result["ok"])


class WalletPurchaseComplianceGateTests(TestCase):
    """Email and terms gates block purchase before touching the wallet."""

    def setUp(self):
        self.product = make_challenge_product(price_usd=Decimal("100.00"))

    def test_unverified_email_blocks_purchase(self):
        user = make_user(email_verified=False, terms_accepted=True)
        make_wallet(user, initial_balance=Decimal("500.00"))
        self.client.force_login(user)
        self.client.post(_url(self.product.pk))
        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.available_balance, Decimal("500.00"))

    def test_terms_not_accepted_blocks_purchase(self):
        user = make_user(email_verified=True, terms_accepted=False)
        make_wallet(user, initial_balance=Decimal("500.00"))
        self.client.force_login(user)
        self.client.post(_url(self.product.pk))
        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.available_balance, Decimal("500.00"))

    def test_unauthenticated_redirects_to_login(self):
        r = self.client.post(_url(self.product.pk))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


class WalletPurchaseGetMethodTests(TestCase):
    """GET requests and inactive products redirect correctly."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_get_redirects_to_challenge_purchase_page(self):
        product = make_challenge_product()
        r = self.client.get(_url(product.pk))
        self.assertRedirects(
            r,
            reverse("simulator:challenge_purchase", kwargs={"product_id": product.pk}),
            fetch_redirect_response=False,
        )

    def test_inactive_product_redirects_to_catalog(self):
        product = make_challenge_product(is_active=False)
        r = self.client.post(_url(product.pk))
        self.assertRedirects(r, reverse("simulator:challenge_catalog"), fetch_redirect_response=False)


class ExternalNowPaymentsFlowUnaffectedTests(TestCase):
    """
    The existing NOWPayments checkout flow (POST /challenges/<id>/buy/) must
    continue to work independently of the wallet purchase flow.
    """

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("0.00"))
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        _login(self.client, self.user)

    def test_external_checkout_url_still_returns_200_on_get(self):
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertEqual(r.status_code, 200)

    def test_external_checkout_context_has_crypto_choices(self):
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertIn("crypto_choices", r.context)
        self.assertTrue(len(r.context["crypto_choices"]) > 0)

    def test_external_checkout_context_now_includes_wallet(self):
        """challenge_purchase_view GET must expose wallet context for the new wallet UI."""
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertIn("wallet_balance", r.context)
        self.assertIn("can_pay_with_wallet", r.context)
        self.assertIn("wallet_shortfall", r.context)

    def test_can_pay_with_wallet_false_when_balance_zero(self):
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertFalse(r.context["can_pay_with_wallet"])

    def test_wallet_shortfall_correct_when_insufficient(self):
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertEqual(r.context["wallet_shortfall"], self.product.price_usd)

    def test_can_pay_with_wallet_true_when_balance_sufficient(self):
        from simulator.wallet_ledger import credit_wallet
        from simulator.models import WalletTransaction
        wallet = Wallet.objects.get(user=self.user)
        credit_wallet(wallet.id, Decimal("200.00"), WalletTransaction.TX_DEPOSIT)
        r = self.client.get(
            reverse("simulator:challenge_purchase", kwargs={"product_id": self.product.pk})
        )
        self.assertTrue(r.context["can_pay_with_wallet"])

    def test_wallet_purchase_url_is_separate_from_nowpayments_url(self):
        wallet_url = reverse(
            "simulator:challenge_wallet_purchase", kwargs={"product_id": self.product.pk}
        )
        np_url = reverse(
            "simulator:challenge_purchase", kwargs={"product_id": self.product.pk}
        )
        self.assertNotEqual(wallet_url, np_url)
