# simulator/tests/test_challenge_purchase.py
"""
Phase 4D — Purchase-to-Challenge-Activation tests.

Covers:
  - challenge_catalog_view GET
  - challenge_purchase_view GET / POST
  - deposit_callback bifurcation: challenge vs wallet
  - ChallengeEnrollment creation + Phase 1 activation
  - Idempotency: duplicate webhook does not create second enrollment
  - Partial payment does NOT activate challenge
  - Normal wallet deposits still work when challenge_product is None
  - Rollback: activation failure leaves deposit.credited=False
"""
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.urls import reverse

from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    Deposit,
    Wallet,
    WalletTransaction,
)
from simulator.tests.factories import (
    make_challenge_product,
    make_user,
    make_wallet,
    make_deposit,
)

CALLBACK_URL = "/deposit/callback/"

# Patch rate_check for all callback tests — Redis counter accumulates across the
# test session; by the time this module runs the limit may already be reached.
_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


# ── helpers ───────────────────────────────────────────────────────────────────

def _ipn(payment_id, payment_status, order_id="", amount="100.00"):
    return json.dumps({
        "payment_id":           payment_id,
        "payment_status":       payment_status,
        "order_id":             str(order_id),
        "actually_paid_amount": amount,
        "pay_currency":         "btc",
        "price_currency":       "usd",
        "price_amount":         float(amount),
    })


def _make_challenge_deposit(user, product, payment_id="cp_pay_001", credited=False):
    return Deposit.objects.create(
        user=user,
        amount_usd=product.price_usd,
        crypto_currency="btc",
        nowpayments_payment_id=payment_id,
        status="pending",
        credited=credited,
        challenge_product=product,
    )


# ── Challenge catalog view ────────────────────────────────────────────────────

class ChallengeCatalogViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_catalog_get_200(self):
        make_challenge_product(name="Starter 10K", tier="10K")
        url = reverse("simulator:challenge_catalog")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_catalog_lists_active_products(self):
        p1 = make_challenge_product(name="Active 10K", tier="10K", is_active=True)
        p2 = make_challenge_product(name="Inactive 10K", tier="10K", is_active=False)
        url = reverse("simulator:challenge_catalog")
        response = self.client.get(url)
        products = list(response.context["products"])
        self.assertIn(p1, products)
        self.assertNotIn(p2, products)

    def test_catalog_requires_login(self):
        self.client.logout()
        url = reverse("simulator:challenge_catalog")
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 200)


# ── Challenge purchase view ───────────────────────────────────────────────────

class ChallengePurchaseViewGetTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.product = make_challenge_product(name="Pro 10K", tier="10K")

    def test_get_returns_200(self):
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_passes_product_to_context(self):
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["product"], self.product)

    def test_inactive_product_redirects_to_catalog(self):
        self.product.is_active = False
        self.product.save()
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("simulator:challenge_catalog"))

    def test_nonexistent_product_redirects_to_catalog(self):
        url = reverse("simulator:challenge_purchase", args=[99999])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("simulator:challenge_catalog"))


class ChallengePurchaseViewPostTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.product = make_challenge_product(name="Pro 10K", tier="10K")

    @patch("simulator.nowpayments.create_payment")
    def test_post_creates_deposit_with_challenge_product(self, mock_create):
        mock_create.return_value = {
            "payment_id": "np_123",
            "invoice_url": "",
            "pay_address": "addr123",
            "pay_amount": 0.001,
            "payment_status": "waiting",
            "expiration_estimate_date": None,
        }
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        self.client.post(url, {"crypto_currency": "btc"})

        deposit = Deposit.objects.filter(user=self.user, challenge_product=self.product).first()
        self.assertIsNotNone(deposit)
        self.assertEqual(deposit.challenge_product_id, self.product.pk)
        self.assertEqual(deposit.amount_usd, self.product.price_usd)

    @patch("simulator.nowpayments.create_payment")
    def test_post_redirects_to_status_page(self, mock_create):
        mock_create.return_value = {
            "payment_id": "np_456",
            "invoice_url": "",
            "pay_address": "addr456",
            "pay_amount": 0.001,
            "payment_status": "waiting",
            "expiration_estimate_date": None,
        }
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        response = self.client.post(url, {"crypto_currency": "btc"})
        deposit = Deposit.objects.filter(user=self.user, challenge_product=self.product).first()
        self.assertRedirects(
            response,
            reverse("simulator:deposit_status", args=[deposit.pk]),
            fetch_redirect_response=False,
        )

    @patch("simulator.nowpayments.create_payment", side_effect=Exception("NP down"))
    def test_post_np_failure_marks_deposit_failed(self, _mock):
        url = reverse("simulator:challenge_purchase", args=[self.product.pk])
        self.client.post(url, {"crypto_currency": "btc"})
        deposit = Deposit.objects.filter(user=self.user, challenge_product=self.product).first()
        self.assertIsNotNone(deposit)
        self.assertEqual(deposit.status, "failed")


# ── deposit_callback: challenge bifurcation ───────────────────────────────────

class CallbackChallengeActivationTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user    = make_user()
        self.product = make_challenge_product()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_creates_enrollment(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cp_001")
        body = _ipn("cp_001", "finished", deposit.pk, str(self.product.price_usd))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_confirmed_creates_enrollment(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cp_002")
        body = _ipn("cp_002", "confirmed", deposit.pk, str(self.product.price_usd))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_creates_phase1_account(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cp_003")
        body = _ipn("cp_003", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        enrollment = ChallengeEnrollment.objects.get(deposit=deposit)
        self.assertIsNotNone(enrollment.phase1_account_id)
        self.assertEqual(enrollment.phase1_account.account_type, "CHALLENGE")
        self.assertEqual(enrollment.phase1_account.phase, "Fase 1")

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_sets_deposit_credited(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cp_004")
        body = _ipn("cp_004", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertIsNotNone(deposit.credited_at)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_enrollment_points_to_correct_product(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cp_005")
        body = _ipn("cp_005", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        enrollment = ChallengeEnrollment.objects.get(deposit=deposit)
        self.assertEqual(enrollment.product_id, self.product.pk)
        self.assertEqual(enrollment.user_id, self.user.pk)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_does_not_credit_wallet_for_challenge(self, _sig):
        """Challenge purchase must NOT top up the wallet."""
        wallet = make_wallet(user=self.user)
        deposit = _make_challenge_deposit(self.user, self.product, "cp_006")
        body = _ipn("cp_006", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT
            ).count(),
            0,
        )


# ── Idempotency ───────────────────────────────────────────────────────────────

class CallbackChallengeIdempotencyTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user    = make_user()
        self.product = make_challenge_product()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_webhook_does_not_create_second_enrollment(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "idem_001")
        body = _ipn("idem_001", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(
            ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_second_webhook_returns_200_idempotent(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "idem_002")
        body = _ipn("idem_002", "finished", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        r2 = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r2.status_code, 200)
        data = json.loads(r2.content)
        self.assertTrue(data.get("idempotent"))


# ── Partial payment does NOT activate ─────────────────────────────────────────

class CallbackPartialPaymentTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user    = make_user()
        self.product = make_challenge_product()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partially_paid_does_not_create_enrollment(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "partial_001")
        body = _ipn("partial_001", "partially_paid", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(
            ChallengeEnrollment.objects.filter(deposit=deposit).count(), 0
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partially_paid_leaves_deposit_uncredited(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "partial_002")
        body = _ipn("partial_002", "partially_paid", deposit.pk, str(self.product.price_usd))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)


# ── Normal wallet deposits unaffected ─────────────────────────────────────────

class CallbackWalletDepositUnchangedTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_regular_deposit_still_credits_wallet(self, _sig):
        """Deposits without challenge_product continue to credit the wallet normally."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("150.00"), payment_id="reg_001")
        # Ensure challenge_product is None
        self.assertIsNone(deposit.challenge_product_id)

        body = _ipn("reg_001", "finished", deposit.pk, "150.00")
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(response.status_code, 200)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("150.00"))

        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_regular_deposit_creates_no_enrollment(self, _sig):
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("50.00"), payment_id="reg_002")
        body = _ipn("reg_002", "finished", deposit.pk, "50.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 0)


# ── Rollback on activation failure ────────────────────────────────────────────

class CallbackActivationFailureRollbackTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user    = make_user()
        self.product = make_challenge_product()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    @patch("simulator.views._ce_activate", side_effect=RuntimeError("DB error"))
    def test_activation_failure_leaves_deposit_uncredited(self, _activate, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "fail_001")
        body = _ipn("fail_001", "finished", deposit.pk, str(self.product.price_usd))
        # Exception propagates through atomic() and out of the view — Django test
        # client re-raises it; we catch it so the assertion can run.
        try:
            self.client.post(CALLBACK_URL, body, content_type="application/json")
        except Exception:
            pass
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    @patch("simulator.views._ce_activate", side_effect=RuntimeError("DB error"))
    def test_activation_failure_creates_no_enrollment(self, _activate, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "fail_002")
        body = _ipn("fail_002", "finished", deposit.pk, str(self.product.price_usd))
        try:
            self.client.post(CALLBACK_URL, body, content_type="application/json")
        except Exception:
            pass
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 0)


# ── Confirmed-amount correctness (Hotfix: deposit.amount_usd → credit_amount) ─

def _ipn_no_confirmed(payment_id, payment_status, order_id="", amount="100.00"):
    """IPN payload without actually_paid_amount — simulates providers that omit it."""
    return json.dumps({
        "payment_id":    payment_id,
        "payment_status": payment_status,
        "order_id":      str(order_id),
        "pay_currency":  "btc",
        "price_currency": "usd",
        "price_amount":  float(amount),
    })


class CallbackWalletConfirmedAmountTests(TestCase):
    """Wallet top-up credits confirmed amount, not requested amount."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_credits_confirmed_amount_not_requested(self, _sig):
        """finished IPN with actually_paid_amount=$95 on $100 deposit → wallet gets $95."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="amt_001")
        body = _ipn("amt_001", "finished", deposit.pk, "95.00")
        r = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("95.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_credits_full_confirmed_when_amounts_match(self, _sig):
        """Standard full payment: confirmed == requested → wallet gets full amount."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("200.00"), payment_id="amt_002")
        body = _ipn("amt_002", "finished", deposit.pk, "200.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("200.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_falls_back_to_amount_usd_when_no_confirmed_field(self, _sig):
        """Provider omits actually_paid_amount → fall back to deposit.amount_usd."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("50.00"), payment_id="amt_003")
        body = _ipn_no_confirmed("amt_003", "finished", deposit.pk, "50.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_deposit_credited_true_after_underpayment(self, _sig):
        """Even if confirmed < requested, regular wallet deposit is still marked credited."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="amt_004")
        body = _ipn("amt_004", "finished", deposit.pk, "80.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_wallet_webhook_no_double_credit(self, _sig):
        """Second identical webhook must not credit the wallet a second time."""
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="amt_005")
        body = _ipn("amt_005", "finished", deposit.pk, "100.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))


class CallbackPartialPaymentStorageTests(TestCase):
    """partially_paid status stores confirmed_amount_usd and stays uncredited."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partial_payment_stores_confirmed_amount_usd(self, _sig):
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="pp_001")
        body = _ipn("pp_001", "partially_paid", deposit.pk, "60.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertEqual(deposit.confirmed_amount_usd, Decimal("60.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partial_payment_wallet_not_credited(self, _sig):
        wallet  = make_wallet(user=self.user)
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="pp_002")
        body = _ipn("pp_002", "partially_paid", deposit.pk, "60.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partial_payment_deposit_uncredited(self, _sig):
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="pp_003")
        body = _ipn("pp_003", "partially_paid", deposit.pk, "60.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partial_payment_status_updated(self, _sig):
        deposit = make_deposit(self.user, amount_usd=Decimal("100.00"), payment_id="pp_004")
        body = _ipn("pp_004", "partially_paid", deposit.pk, "60.00")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, "partially_paid")


class CallbackChallengeUnderpaidTests(TestCase):
    """Challenge purchase with confirmed amount below product price must NOT activate."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user    = make_user()
        self.product = make_challenge_product()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_underpaid_does_not_create_enrollment(self, _sig):
        """finished IPN but confirmed amount < price → no enrollment."""
        deposit = _make_challenge_deposit(self.user, self.product, "cup_001")
        underpaid = str(deposit.challenge_product.price_usd - Decimal("1.00"))
        body = _ipn("cup_001", "finished", deposit.pk, underpaid)
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_underpaid_deposit_not_credited(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cup_002")
        underpaid = str(deposit.challenge_product.price_usd - Decimal("1.00"))
        body = _ipn("cup_002", "finished", deposit.pk, underpaid)
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_underpaid_stores_confirmed_amount(self, _sig):
        deposit = _make_challenge_deposit(self.user, self.product, "cup_003")
        underpaid = deposit.challenge_product.price_usd - Decimal("5.00")
        body = _ipn("cup_003", "finished", deposit.pk, str(underpaid))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        deposit.refresh_from_db()
        self.assertEqual(deposit.confirmed_amount_usd, underpaid)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_exact_price_activates_enrollment(self, _sig):
        """Edge: confirmed == price exactly → should activate."""
        deposit = _make_challenge_deposit(self.user, self.product, "cup_004")
        exact = str(deposit.challenge_product.price_usd)
        body = _ipn("cup_004", "finished", deposit.pk, exact)
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(ChallengeEnrollment.objects.filter(deposit=deposit).count(), 1)
