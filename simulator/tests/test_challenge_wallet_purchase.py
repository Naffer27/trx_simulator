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
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from simulator.models import (
    BrokerLedger,
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
        # BBOOK-CLOSE-01 FASE B.1 — enrollment_source=SRC_WALLET made
        # explicit: this test's intent is "an existing WALLET purchase
        # blocks a new WALLET purchase attempt" (still fully protected).
        # The factory's own default (ADMIN_GRANT) would no longer block
        # this by deliberate design — that is a different, separately
        # covered scenario (ProvenanceCrossSourceExclusivityTests).
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_PHASE_1,
            enrollment_source=ChallengeEnrollment.SRC_WALLET,
        )
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("1000.00"),
                         "Wallet must not be debited when an active enrollment exists")

    def test_existing_phase2_enrollment_blocks_purchase(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            status=ChallengeEnrollment.ST_PHASE_2,
            enrollment_source=ChallengeEnrollment.SRC_WALLET,
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
            enrollment_source=ChallengeEnrollment.SRC_WALLET,
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


# ── BBOOK-CLOSE-01 — Path B concurrency race fix ────────────────────────────
#
# The tests below were added when challenge_wallet_purchase_view was changed
# to create the ChallengeEnrollment FIRST (inside its own savepoint, relying
# on models.py's uniq_active_enrollment_per_user_product UniqueConstraint as
# the authoritative guard) and only THEN debit the wallet — replacing the
# previous select_for_update().exists() pre-check, which the FASE A audit
# proved locks nothing when no matching row exists yet.

class WalletPurchaseExactBalanceTests(TestCase):
    """Item 2 — wallet balance exactly equals the price."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("199.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("199.00"))
        _login(self.client, self.user)

    def test_exact_balance_succeeds(self):
        r = self.client.post(_url(self.product.pk))
        self.assertRedirects(r, reverse("simulator:accounts"), fetch_redirect_response=False)

    def test_exact_balance_leaves_wallet_at_zero(self):
        self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("0.00"))


class WalletPurchaseCrossScopeTests(TestCase):
    """Items 7/8 — the new constraint is scoped to (user, product); it must
    never block unrelated purchases."""

    def test_same_user_different_products_both_allowed(self):
        user = make_user()
        product_a = make_challenge_product(price_usd=Decimal("100.00"))
        product_b = make_challenge_product(price_usd=Decimal("100.00"))
        make_wallet(user, initial_balance=Decimal("500.00"))
        _login(self.client, user)

        self.client.post(_url(product_a.pk))
        self.client.post(_url(product_b.pk))

        count = ChallengeEnrollment.objects.filter(user=user).count()
        self.assertEqual(count, 2, "Different products for the same user must not collide")

    def test_different_users_same_product_both_allowed(self):
        product = make_challenge_product(price_usd=Decimal("100.00"))
        user_a = make_user()
        user_b = make_user()
        make_wallet(user_a, initial_balance=Decimal("500.00"))
        make_wallet(user_b, initial_balance=Decimal("500.00"))

        c1 = Client()
        c1.force_login(user_a)
        c1.post(_url(product.pk))

        c2 = Client()
        c2.force_login(user_b)
        c2.post(_url(product.pk))

        count = ChallengeEnrollment.objects.filter(product=product).count()
        self.assertEqual(count, 2, "Different users buying the same product must not collide")


class WalletPurchaseDebitFailureRollbackTests(TestCase):
    """Item 9 — if debit_wallet() itself fails AFTER the enrollment was
    created (inside the outer atomic block), the whole transaction —
    including the enrollment — must roll back. No orphaned enrollment
    without a corresponding debit."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_debit_failure_rolls_back_enrollment(self):
        with patch("simulator.views.debit_wallet", side_effect=RuntimeError("debit boom")):
            self.client.post(_url(self.product.pk))

        count = ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count()
        self.assertEqual(count, 0, "A failed debit must roll back the enrollment created just before it")

    def test_debit_failure_leaves_wallet_unchanged(self):
        with patch("simulator.views.debit_wallet", side_effect=RuntimeError("debit boom")):
            self.client.post(_url(self.product.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500.00"))

    def test_debit_failure_creates_no_trading_account(self):
        with patch("simulator.views.debit_wallet", side_effect=RuntimeError("debit boom")):
            self.client.post(_url(self.product.pk))
        count = TradingAccount.objects.filter(user=self.user, account_type="CHALLENGE").count()
        self.assertEqual(count, 0)


class WalletPurchaseRevenueBookingFailureRollbackTests(TestCase):
    """Item 10 — an unexpected (non-Duplicate) exception from
    record_challenge_fee_revenue() must roll back the whole transaction,
    including the already-created enrollment and the already-debited
    wallet. record_challenge_fee_revenue/DuplicateChallengeRevenue are
    imported LOCALLY inside the view at call time, so the patch target is
    their real source module, not simulator.views."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_revenue_booking_failure_rolls_back_everything(self):
        with patch(
            "simulator.challenge_revenue.record_challenge_fee_revenue",
            side_effect=RuntimeError("revenue booking boom"),
        ):
            self.client.post(_url(self.product.pk))

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500.00"))
        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count(), 0,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type="CHALLENGE_FEE").count(), 0,
        )


class WalletPurchaseExactlyOnceInvariantTests(TestCase):
    """Items 12-18 — a genuine duplicate purchase attempt (sequential,
    same user+product) must leave EVERY economic artifact at exactly one,
    and the losing/second request must produce ZERO economic side effects
    of its own — not just a blocked enrollment."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("199.00"))
        self.wallet = make_wallet(self.user, initial_balance=Decimal("999.00"))
        _login(self.client, self.user)

    def _buy_twice(self):
        self.client.post(_url(self.product.pk))
        self.client.post(_url(self.product.pk))

    def test_exactly_one_enrollment(self):
        self._buy_twice()
        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count(), 1,
        )

    def test_exactly_one_wallet_transaction(self):
        self._buy_twice()
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type="CHALLENGE_FEE").count(), 1,
        )

    def test_exactly_one_rev_challenge_fee_row(self):
        self._buy_twice()
        rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("199.00"))

    def test_exactly_one_trading_account_and_risk_rule(self):
        self._buy_twice()
        accounts = TradingAccount.objects.filter(user=self.user, account_type="CHALLENGE")
        self.assertEqual(accounts.count(), 1)
        self.assertEqual(RiskRule.objects.filter(account__in=accounts).count(), 1)

    def test_correct_final_wallet_balance(self):
        self._buy_twice()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("800.00"))

    def test_wallet_never_negative_even_when_balance_only_covers_one(self):
        """Wallet funded for exactly one purchase — the second (losing)
        attempt must never push balance below zero."""
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("199.00"))
        wallet = make_wallet(user, initial_balance=Decimal("199.00"))
        client = Client()
        client.force_login(user)
        client.post(_url(product.pk))
        client.post(_url(product.pk))
        wallet.refresh_from_db()
        self.assertGreaterEqual(wallet.available_balance, Decimal("0.00"))
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_losing_request_zero_economic_side_effects(self):
        """The precise BBOOK-CLOSE-01 proof: after two sequential attempts,
        the SECOND (losing) request's own contribution to every economic
        table is exactly zero — not "eventually consistent", never
        created at all."""
        self.client.post(_url(self.product.pk))
        enrollment_count_after_first = ChallengeEnrollment.objects.filter(
            user=self.user, product=self.product,
        ).count()
        wallet_tx_after_first = WalletTransaction.objects.filter(
            wallet=self.wallet, tx_type="CHALLENGE_FEE",
        ).count()
        ledger_after_first = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_CHALLENGE_FEE,
        ).count()

        self.client.post(_url(self.product.pk))  # the losing request

        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count(),
            enrollment_count_after_first,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type="CHALLENGE_FEE").count(),
            wallet_tx_after_first,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE).count(),
            ledger_after_first,
        )


class WalletPurchaseNoDuplicateEmailTests(TestCase):
    """Item 19 — a duplicate purchase attempt must not send a second
    'challenge activated' email. send_email_async is imported LOCALLY
    inside the view at call time; patch its real source module."""

    def setUp(self):
        # make_user() leaves User.email blank by default — the view's own
        # `if email: send_email_async.delay(...)` guard would make this
        # test vacuously pass either way, so an explicit email is required
        # to actually exercise the code path being tested.
        self.user = make_user(email="bbook-close01@example.com")
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)

    def test_duplicate_attempt_sends_exactly_one_email(self):
        with patch("simulator.tasks.send_email_async") as mock_task:
            self.client.post(_url(self.product.pk))
            self.client.post(_url(self.product.pk))
        self.assertEqual(mock_task.delay.call_count, 1)


def _run_locked_retry(fn, barrier, results, index, max_retries=60):
    """Shared concurrency-test helper — mirrors the pattern already
    certified in test_payment_webhook_inbox.py / test_provider_cost_normalization.py.
    Retries on SQLite's own coarse 'database is locked' errors so a losing
    thread gets a real chance to run its transaction, rather than being
    counted as a false negative."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                return fn()
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("operational_error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


class WalletPurchaseConcurrencyTests(TransactionTestCase):
    """
    Real multi-thread concurrency tests.

    WHAT SQLITE CAN CERTIFY HERE: that the DB-level
    uniq_active_enrollment_per_user_product constraint exists, is real, and
    rejects a genuine duplicate INSERT with IntegrityError — SQLite enforces
    partial/conditional UniqueConstraints identically to PostgreSQL. That part
    of the fix is fully certified by these tests on any engine.

    WHAT SQLITE CANNOT CERTIFY: true row-level lock SERIALIZATION timing.
    SQLite silently no-ops select_for_update() (has_select_for_update=False,
    verified against this repo's own connection features in the FASE A
    audit) and instead falls back to a coarse, TABLE-level write lock that
    frequently makes the losing thread's write fail outright with
    "database is locked" rather than block-and-retry the way PostgreSQL's
    real row lock does. These tests do NOT — and cannot — prove that the two
    requests would serialize cleanly under PostgreSQL's finer-grained
    locking; only a real PostgreSQL run can certify that timing behavior
    (see POSTGRES CONCURRENCY CERTIFICATION = PENDING in the delivery
    report). What these tests DO prove, on any engine: however the two
    requests interleave, the FINAL state never contains more than one
    active enrollment, one debit, one revenue row — the constraint holds
    under real concurrent pressure, not just sequential replay.
    """

    def test_two_concurrent_requests_same_user_same_product(self):
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("199.00"))
        wallet = make_wallet(user, initial_balance=Decimal("398.00"))

        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            client = Client()
            client.force_login(user)
            return client.post(_url(product.pk)).status_code

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_attempt, barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self._assert_final_state_correct(user, product, wallet, max_active=1)

    def test_five_concurrent_requests_same_user_same_product(self):
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("100.00"))
        # Funded well above 5x price — if the constraint failed to hold,
        # every one of the 5 requests would have enough balance to succeed.
        wallet = make_wallet(user, initial_balance=Decimal("1000.00"))

        n = 5
        barrier = threading.Barrier(n)
        results = [None] * n

        def _attempt():
            client = Client()
            client.force_login(user)
            return client.post(_url(product.pk)).status_code

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_attempt, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self._assert_final_state_correct(user, product, wallet, max_active=1)

    def _assert_final_state_correct(self, user, product, wallet, *, max_active):
        wallet.refresh_from_db()
        enrollments = ChallengeEnrollment.objects.filter(user=user, product=product)
        self.assertLessEqual(
            enrollments.count(), max_active,
            "The DB constraint must prevent more than one active enrollment "
            "regardless of how many concurrent requests raced for it",
        )
        wallet_txns = WalletTransaction.objects.filter(wallet=wallet, tx_type="CHALLENGE_FEE").count()
        ledger_rows = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_CHALLENGE_FEE, source_challenge_enrollment__in=enrollments,
        ).count()
        self.assertEqual(wallet_txns, enrollments.count())
        self.assertEqual(ledger_rows, enrollments.count())
        self.assertGreaterEqual(wallet.available_balance, Decimal("0.00"))


# ── BBOOK-CLOSE-01 FASE B.1 — enrollment_source provenance ─────────────────
#
# These tests cover the explicit provenance distinction authorized in
# FASE B.1-A/B: ChallengeEnrollment.enrollment_source (DEPOSIT/WALLET/
# EXTERNAL/ADMIN_GRANT), the revised uniq_active_enrollment_per_user_product
# constraint (excludes ADMIN_GRANT), and the anti-bypass admin exposure.

import hashlib
import hmac
import json as _json

from django.test import override_settings

from simulator.models import Deposit

_EXT_ENDPOINT = "/api/internal/challenge/activate/"
_EXT_TEST_SECRET = "test-webhook-secret-32-bytes-long!!"


def _sign_ext(payload: dict, secret: str = _EXT_TEST_SECRET) -> str:
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _post_ext(client, payload: dict, secret: str = _EXT_TEST_SECRET):
    body = _json.dumps(payload)
    sig = _sign_ext(payload, secret)
    return client.post(
        _EXT_ENDPOINT, body, content_type="application/json",
        HTTP_X_MONEYBROKER_SIGNATURE=sig,
    )


def _deposit_ipn(payment_id, payment_status, order_id, amount):
    return _json.dumps({
        "payment_id": payment_id, "payment_status": payment_status, "order_id": str(order_id),
        "actually_paid": float(amount), "pay_currency": "btc",
        "price_currency": "usd", "price_amount": float(amount),
    })


class ProvenanceCrossSourceExclusivityTests(TestCase):
    """Items F.1-F.5 — every real purchase source (DEPOSIT/WALLET/EXTERNAL)
    is mutually exclusive with every other real purchase source for the
    same (user, product); ADMIN_GRANT is exempt from all of them."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("199.00"))

    def test_wallet_then_wallet_second_rejected(self):
        make_wallet(self.user, initial_balance=Decimal("398.00"))
        _login(self.client, self.user)
        self.client.post(_url(self.product.pk))
        self.client.post(_url(self.product.pk))
        enrollments = ChallengeEnrollment.objects.filter(user=self.user, product=self.product)
        self.assertEqual(enrollments.count(), 1)
        self.assertEqual(enrollments.first().enrollment_source, ChallengeEnrollment.SRC_WALLET)

    def test_deposit_then_wallet_rejected(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_DEPOSIT,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)
        self.client.post(_url(self.product.pk))
        enrollments = ChallengeEnrollment.objects.filter(user=self.user, product=self.product)
        self.assertEqual(enrollments.count(), 1)
        self.assertEqual(enrollments.first().enrollment_source, ChallengeEnrollment.SRC_DEPOSIT)

    def test_external_then_wallet_rejected(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_EXTERNAL,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)
        self.client.post(_url(self.product.pk))
        enrollments = ChallengeEnrollment.objects.filter(user=self.user, product=self.product)
        self.assertEqual(enrollments.count(), 1)
        self.assertEqual(enrollments.first().enrollment_source, ChallengeEnrollment.SRC_EXTERNAL)

    def test_admin_grant_then_wallet_both_coexist(self):
        make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_ADMIN_GRANT,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        make_wallet(self.user, initial_balance=Decimal("500.00"))
        _login(self.client, self.user)
        r = self.client.post(_url(self.product.pk))
        self.assertRedirects(r, reverse("simulator:accounts"), fetch_redirect_response=False)

        enrollments = ChallengeEnrollment.objects.filter(user=self.user, product=self.product)
        self.assertEqual(enrollments.count(), 2, "ADMIN_GRANT + WALLET must coexist")
        sources = sorted(enrollments.values_list("enrollment_source", flat=True))
        self.assertEqual(
            sources,
            sorted([ChallengeEnrollment.SRC_ADMIN_GRANT, ChallengeEnrollment.SRC_WALLET]),
        )

    def test_admin_grant_then_admin_grant_multiple_allowed(self):
        e1 = make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_ADMIN_GRANT,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        e2 = make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_ADMIN_GRANT,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count(), 2,
        )


class ProvenanceScopeTests(TestCase):
    """Items F.6-F.8 — the constraint stays scoped correctly: different
    users, different products, and terminal (FAILED/WITHDRAWN) statuses
    never collide with it."""

    def test_different_users_same_product_wallet_purchase_both_allowed(self):
        product = make_challenge_product(price_usd=Decimal("100.00"))
        user_a, user_b = make_user(), make_user()
        make_wallet(user_a, initial_balance=Decimal("500.00"))
        make_wallet(user_b, initial_balance=Decimal("500.00"))
        Client_a, Client_b = Client(), Client()
        Client_a.force_login(user_a)
        Client_b.force_login(user_b)
        Client_a.post(_url(product.pk))
        Client_b.post(_url(product.pk))
        self.assertEqual(ChallengeEnrollment.objects.filter(product=product).count(), 2)

    def test_same_user_different_products_wallet_purchase_both_allowed(self):
        user = make_user()
        product_a = make_challenge_product(price_usd=Decimal("100.00"))
        product_b = make_challenge_product(price_usd=Decimal("100.00"))
        make_wallet(user, initial_balance=Decimal("500.00"))
        _login(self.client, user)
        self.client.post(_url(product_a.pk))
        self.client.post(_url(product_b.pk))
        self.assertEqual(ChallengeEnrollment.objects.filter(user=user).count(), 2)

    def test_withdrawn_previous_allows_repurchase(self):
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("100.00"))
        make_challenge_enrollment(
            user=user, product=product,
            enrollment_source=ChallengeEnrollment.SRC_WALLET,
            status=ChallengeEnrollment.ST_WITHDRAWN,
        )
        make_wallet(user, initial_balance=Decimal("500.00"))
        _login(self.client, user)
        self.client.post(_url(product.pk))
        new_count = ChallengeEnrollment.objects.filter(
            user=user, product=product, status=ChallengeEnrollment.ST_PHASE_1,
        ).count()
        self.assertEqual(new_count, 1)


class ProvenanceCorrectPerCreatorTests(TestCase):
    """Items F.9-F.12 — each real creator sets enrollment_source correctly,
    driven through its real, unmodified production code path."""

    def test_path_b_wallet_purchase_sets_wallet_source(self):
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("100.00"))
        make_wallet(user, initial_balance=Decimal("500.00"))
        _login(self.client, user)
        self.client.post(_url(product.pk))
        enrollment = ChallengeEnrollment.objects.get(user=user, product=product)
        self.assertEqual(enrollment.enrollment_source, ChallengeEnrollment.SRC_WALLET)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_path_a_deposit_purchase_sets_deposit_source(self, _sig):
        user = make_user()
        product = make_challenge_product(price_usd=Decimal("150.00"))
        deposit = Deposit.objects.create(
            user=user, amount_usd=product.price_usd, crypto_currency="btc",
            nowpayments_payment_id="prov_test_dep_1", status="pending", credited=False,
            challenge_product=product, pay_amount=product.price_usd,
        )
        body = _deposit_ipn("prov_test_dep_1", "finished", deposit.pk, product.price_usd)
        with patch("simulator.ratelimit.rate_check", return_value=(True, 0)):
            self.client.post("/deposit/callback/", body, content_type="application/json")
        enrollment = ChallengeEnrollment.objects.get(deposit=deposit)
        self.assertEqual(enrollment.enrollment_source, ChallengeEnrollment.SRC_DEPOSIT)

    @override_settings(CHALLENGE_WEBHOOK_SECRET=_EXT_TEST_SECRET)
    def test_external_webhook_sets_external_source(self):
        product = make_challenge_product(external_code="prov_test_code")
        payload = {
            "event_id": "prov_test_evt_1", "email": "prov_test@external.com",
            "full_name": "Provenance Test", "challenge_product_code": "prov_test_code",
            "payment_id": "prov_test_pay_1", "amount_paid": float(product.price_usd),
            "currency": "USD", "paid_at": "2026-06-04T10:00:00Z",
        }
        r = _post_ext(self.client, payload)
        self.assertEqual(r.status_code, 200)
        enrollment = ChallengeEnrollment.objects.get(external_event_id="prov_test_evt_1")
        self.assertEqual(enrollment.enrollment_source, ChallengeEnrollment.SRC_EXTERNAL)

    def test_admin_grant_default_source(self):
        """A bare creation with no enrollment_source specified — exactly
        what the plain Django admin Add form produces — must default to
        ADMIN_GRANT from the model field itself."""
        user = make_user()
        product = make_challenge_product()
        enrollment = ChallengeEnrollment.objects.create(
            user=user, product=product, deposit=None,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        self.assertEqual(enrollment.enrollment_source, ChallengeEnrollment.SRC_ADMIN_GRANT)


class ProvenanceDoesNotAlterEconomicsTests(TestCase):
    """Item F.13 — adding enrollment_source changes nothing about the
    economic side effects of a wallet purchase: WalletTransaction,
    REV_CHALLENGE_FEE, TradingAccount, RiskRule, balance, and (by
    construction, since Path B is always deposit=None) IB attribution
    all behave exactly as certified in the rest of FASE B."""

    def test_full_invariant_set_unaffected_by_provenance_field(self):
        user = make_user(email="prov-invariant@example.com")
        product = make_challenge_product(price_usd=Decimal("199.00"))
        wallet = make_wallet(user, initial_balance=Decimal("199.00"))
        _login(self.client, user)

        with patch("simulator.tasks.send_email_async") as mock_task:
            self.client.post(_url(product.pk))

        wallet.refresh_from_db()
        enrollment = ChallengeEnrollment.objects.get(user=user, product=product)

        self.assertEqual(enrollment.enrollment_source, ChallengeEnrollment.SRC_WALLET)
        self.assertEqual(wallet.available_balance, Decimal("0.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type="CHALLENGE_FEE").count(), 1,
        )
        ledger_rows = BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)
        self.assertEqual(ledger_rows.count(), 1)
        self.assertEqual(ledger_rows.first().amount, Decimal("199.00"))
        self.assertIsNotNone(enrollment.phase1_account_id)
        self.assertTrue(RiskRule.objects.filter(account=enrollment.phase1_account).exists())
        self.assertEqual(mock_task.delay.call_count, 1)
        # Path B enrollments are always deposit=None; IB commission requires
        # a real Deposit (locked Owner policy, ib_commission.py) — zero IB
        # obligations must exist for this purchase, unaffected by provenance.
        from simulator.models import IBCommissionObligation
        self.assertEqual(IBCommissionObligation.objects.count(), 0)


class ProvenanceAdminNoBypassTests(TestCase):
    """Item F.14 — an operator cannot relabel an existing WALLET/DEPOSIT/
    EXTERNAL enrollment as ADMIN_GRANT via the admin Change form to evade
    uniq_active_enrollment_per_user_product. enrollment_source is in
    ChallengeEnrollmentAdmin.readonly_fields — never bound to the
    ModelForm, so no POST payload can alter it, valid staff session or
    not."""

    def setUp(self):
        self.staff = make_user(username="prov_staff_admin")
        self.staff.is_staff = True
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.staff)

        self.user = make_user()
        self.product = make_challenge_product(price_usd=Decimal("100.00"))
        self.enrollment = make_challenge_enrollment(
            user=self.user, product=self.product,
            enrollment_source=ChallengeEnrollment.SRC_WALLET,
            status=ChallengeEnrollment.ST_PHASE_1,
        )

    def test_change_form_cannot_alter_enrollment_source(self):
        change_url = reverse(
            "admin:simulator_challengeenrollment_change", args=[self.enrollment.pk],
        )
        r = self.client.post(change_url, {
            "user": self.user.pk,
            "product": self.product.pk,
            "deposit": "",
            "enrollment_source": ChallengeEnrollment.SRC_ADMIN_GRANT,  # attempted bypass
            "_save": "Save",
        })
        self.enrollment.refresh_from_db()
        self.assertEqual(
            self.enrollment.enrollment_source, ChallengeEnrollment.SRC_WALLET,
            "enrollment_source must remain WALLET — the admin form must never accept this field",
        )

    def test_bypass_then_second_wallet_purchase_still_rejected(self):
        """End-to-end proof: even after attempting the bypass POST above,
        a second real wallet purchase for the same user+product is still
        correctly rejected — the constraint was never actually evaded."""
        change_url = reverse(
            "admin:simulator_challengeenrollment_change", args=[self.enrollment.pk],
        )
        self.client.post(change_url, {
            "user": self.user.pk, "product": self.product.pk, "deposit": "",
            "enrollment_source": ChallengeEnrollment.SRC_ADMIN_GRANT,
            "_save": "Save",
        })

        make_wallet(self.user, initial_balance=Decimal("500.00"))
        buyer_client = Client()
        buyer_client.force_login(self.user)
        buyer_client.post(_url(self.product.pk))

        self.assertEqual(
            ChallengeEnrollment.objects.filter(user=self.user, product=self.product).count(), 1,
            "The attempted admin-UI bypass must not have opened the door to a second purchase",
        )
