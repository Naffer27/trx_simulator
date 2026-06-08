# simulator/tests/test_email_verification.py
"""
Email verification gate tests.

Covers:
  1.  New registered user has email_verified=False
  2.  Registration queues a verification email
  3.  Valid signed token marks email as verified
  4.  Invalid/expired token is rejected
  5.  Verified user can deposit
  6.  Unverified user cannot deposit
  7.  Unverified user cannot create a REAL account
  8.  Unverified user CAN create a DEMO account
  9.  Unverified user cannot withdraw
  10. Unverified user cannot purchase a challenge
  11. Verified user can fund (wallet → trading account)
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

from simulator.email_verification import make_email_token
from simulator.models import (
    AccountProduct, ChallengeProduct, EmailVerification,
    TradingAccount, WithdrawalRequest,
)
from simulator.tests.factories import (
    make_user, make_wallet, make_account_product, make_challenge_product,
)

User = get_user_model()

DEPOSIT_URL   = "/deposit/"
WITHDRAW_URL  = "/withdraw/"
CREATE_URL    = "/accounts/create/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")
_PATCH_NP        = patch("simulator.nowpayments.create_payment", return_value={
    "payment_id": "np_test_001",
    "payment_status": "waiting",
    "invoice_url": "",
    "pay_address": "bc1qtest",
    "pay_amount": 0.001,
    "expiration_estimate_date": None,
})


def _make_unverified_user(email="unverified@test.com"):
    """Create a user whose email has NOT been verified."""
    return make_user(email=email, email_verified=False)


def _make_verified_user(email="verified@test.com"):
    """Create a user whose email HAS been verified (factory default)."""
    return make_user(email=email, email_verified=True)


# ── 1. Registration creates unverified record ────────────────────────────────

class RegistrationEmailVerificationTests(TestCase):
    """Registration must create an unverified EmailVerification record."""

    def setUp(self):
        _PATCH_EMAIL.start()
        _PATCH_RATELIMIT.start()

    def tearDown(self):
        _PATCH_EMAIL.stop()
        _PATCH_RATELIMIT.stop()

    def test_new_registered_user_has_email_verified_false(self):
        """After registration, EmailVerification.verified must be False."""
        self.client.post("/register/", {
            "username":  "newuser_vertest",
            "email":     "newuser@test.com",
            "password1": "StrongPass!99",
            "password2": "StrongPass!99",
            "tier":      "10K",
            "phase":     "Fase 1",  # must match form choices exactly
        })
        user = User.objects.filter(username="newuser_vertest").first()
        self.assertIsNotNone(user, "User should be created")
        ev = EmailVerification.objects.filter(user=user).first()
        self.assertIsNotNone(ev, "EmailVerification record should be created")
        self.assertFalse(ev.verified)
        self.assertIsNone(ev.verified_at)

    def test_registration_sends_verification_email(self):
        """Registration must queue exactly one verification email via send_email_async."""
        with patch("simulator.tasks.send_email_async.delay") as mock_delay:
            self.client.post("/register/", {
                "username":  "newuser_emailsend",
                "email":     "emailsend@test.com",
                "password1": "StrongPass!99",
                "password2": "StrongPass!99",
                "tier":      "10K",
                "phase":     "Fase 1",
            })
        verify_calls = [
            c for c in mock_delay.call_args_list
            if "verifica" in c.kwargs.get("subject", "").lower()
        ]
        self.assertTrue(len(verify_calls) >= 1,
                        "At least one verification email should be queued")


# ── 2. Token verification ─────────────────────────────────────────────────────

class EmailTokenVerificationTests(TestCase):
    """Tests for the /verify-email/<token>/ view."""

    def test_valid_token_verifies_email(self):
        """GET with a valid signed token marks EmailVerification.verified=True."""
        user = _make_unverified_user()
        token = make_email_token(user.pk)
        url = reverse("simulator:verify_email", args=[token])

        r = self.client.get(url)

        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        ev = EmailVerification.objects.get(user=user)
        self.assertTrue(ev.verified)
        self.assertIsNotNone(ev.verified_at)

    def test_valid_token_renders_success_page(self):
        """Successful verification renders a page with success indicator."""
        user = _make_unverified_user()
        token = make_email_token(user.pk)
        url = reverse("simulator:verify_email", args=[token])

        r = self.client.get(url)

        self.assertContains(r, "verificado")

    def test_invalid_token_rejected(self):
        """A garbage token must not verify any email and must return an error page."""
        r = self.client.get(reverse("simulator:verify_email", args=["not-a-real-token"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "inválido")

    def test_invalid_token_does_not_verify_user(self):
        """Invalid token must not flip verified flag on any user."""
        user = _make_unverified_user()
        self.client.get(reverse("simulator:verify_email", args=["badtoken"]))
        ev = EmailVerification.objects.get(user=user)
        self.assertFalse(ev.verified)

    def test_verified_user_token_is_idempotent(self):
        """Clicking a valid token a second time is harmless — stays verified."""
        user = _make_unverified_user()
        token = make_email_token(user.pk)
        url = reverse("simulator:verify_email", args=[token])
        self.client.get(url)
        self.client.get(url)
        ev = EmailVerification.objects.get(user=user)
        self.assertTrue(ev.verified)


# ── 3. Money-action gates for unverified users ────────────────────────────────

class UnverifiedUserDepositGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_unverified_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_unverified_user_cannot_deposit(self):
        """POST /deposit/ with unverified email → blocked with error, no Deposit created."""
        from simulator.models import Deposit
        r = self.client.post(DEPOSIT_URL, {
            "amount_usd": "100.00",
            "crypto_currency": "btc",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "verifica")
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)

    def test_verified_user_can_deposit(self):
        """POST /deposit/ with verified email → passes gate (NP call may fail but gate opens)."""
        from simulator.models import Deposit
        ev = self.user.email_verification
        ev.verified = True
        ev.save()

        with patch("simulator.nowpayments.create_payment", side_effect=Exception("NP down")):
            self.client.post(DEPOSIT_URL, {
                "amount_usd": "100.00",
                "crypto_currency": "btc",
            })
        # Gate passed — Deposit record created before NP call
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 1)


class UnverifiedUserWithdrawGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_unverified_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_unverified_user_cannot_withdraw(self):
        """POST /withdraw/ with unverified email → blocked, no WR created."""
        r = self.client.post(WITHDRAW_URL, {
            "amount_usd": "50.00",
            "crypto_currency": "btc",
            "wallet_address": "bc1qtest000000000000000000000000000000000",
            "otp_code": "000000",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "verifica")
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_unverified_withdraw_does_not_debit_wallet(self):
        """Email gate must prevent any wallet debit."""
        self.client.post(WITHDRAW_URL, {
            "amount_usd": "50.00",
            "crypto_currency": "btc",
            "wallet_address": "bc1qtest000000000000000000000000000000000",
            "otp_code": "000000",
        })
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500"))


class UnverifiedUserAccountGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_unverified_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_unverified_user_cannot_create_real_account(self):
        """POST /accounts/create/ for a REAL product → blocked for unverified user."""
        product = make_account_product(
            product_type=AccountProduct.TYPE_RETAIL,
            min_deposit=Decimal("100.00"),
        )
        r = self.client.post(CREATE_URL, {
            "product_id": product.pk,
            "amount": "100.00",
        }, follow=True)
        self.assertContains(r, "verifica")
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_unverified_user_can_create_demo_account(self):
        """POST /accounts/create/ for a DEMO product → allowed even without verification."""
        product = make_account_product(
            product_type=AccountProduct.TYPE_RETAIL,
            min_deposit=Decimal("0.00"),
        )
        # Force it to be DEMO family
        product.family = AccountProduct.FAMILY_DEMO
        product.save()

        r = self.client.post(CREATE_URL, {
            "product_id": product.pk,
        }, follow=True)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)


class UnverifiedUserChallengePurchaseGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_unverified_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_unverified_user_cannot_purchase_challenge(self):
        """POST to challenge purchase → blocked, no Deposit created."""
        from simulator.models import Deposit
        product = make_challenge_product(price_usd=Decimal("99.00"))
        url = reverse("simulator:challenge_purchase", args=[product.pk])
        r = self.client.post(url, {"crypto_currency": "btc"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "verifica")
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)


class VerifiedUserPassesAllGatesTests(TestCase):
    """Smoke tests: a verified user is not blocked by the email gate."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_verified_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_verified_user_deposit_gate_passes(self):
        """Verified user's POST to /deposit/ passes the email gate (NP call may fail)."""
        from simulator.models import Deposit
        with patch("simulator.nowpayments.create_payment", side_effect=Exception("NP down")):
            self.client.post(DEPOSIT_URL, {
                "amount_usd": "100.00",
                "crypto_currency": "btc",
            })
        # Deposit record created means gate was passed
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async.delay")
    def test_verified_user_fund_account_gate_passes(self, _email):
        """Verified user can transfer wallet → trading account."""
        from simulator.models import TradingAccount
        product = make_account_product(
            product_type=AccountProduct.TYPE_RETAIL,
            min_deposit=Decimal("100.00"),
        )
        account = TradingAccount.objects.create(
            user=self.user,
            account_type="RETAIL",
            tier="10K",
            balance=Decimal("0"),
            equity=Decimal("0"),
            peak_balance=Decimal("0"),
            initial_balance=Decimal("0"),
            status="Activo",
            leverage=50,
        )
        url = reverse("simulator:fund_account", args=[account.pk])
        self.client.post(url, {"amount": "100.00"})
        self.wallet.refresh_from_db()
        # If gate passed and transfer happened, balance dropped
        self.assertEqual(self.wallet.available_balance, Decimal("400.00"))
