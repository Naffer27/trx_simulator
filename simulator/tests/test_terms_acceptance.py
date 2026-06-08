# simulator/tests/test_terms_acceptance.py
"""
Terms and Risk Disclaimer acceptance gate tests.

Covers:
  1.  User without terms cannot deposit
  2.  User without terms cannot create a REAL account
  3.  User without terms cannot withdraw
  4.  User without terms cannot buy a challenge
  5.  User without terms CAN create a DEMO account
  6.  User with current terms can access money actions
  7.  Old (stale) terms version is rejected by the gate
  8.  accept_terms_view creates TermsAcceptance record
  9.  accept_terms_view records ip_address and user_agent
  10. accept_terms_view redirects to ?next= if provided
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

from simulator.models import (
    AccountProduct, TermsAcceptance, TERMS_VERSION, RISK_DISCLOSURE_VERSION,
    WithdrawalRequest,
)
from simulator.tests.factories import (
    make_user, make_wallet, make_account_product, make_challenge_product,
)

User = get_user_model()

DEPOSIT_URL  = "/deposit/"
WITHDRAW_URL = "/withdraw/"
CREATE_URL   = "/accounts/create/"
ACCEPT_URL   = "/legal/accept/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")

# Payload for all 4 checkboxes checked
_ALL_CHECKS = {
    "accept_terms":            "on",
    "accept_risk":             "on",
    "accept_withdrawal_policy": "on",
    "understand_risk":         "on",
}


def _make_no_terms_user(email="noterms@test.com"):
    """Verified email but NO TermsAcceptance."""
    return make_user(email=email, email_verified=True, terms_accepted=False)


# ── 1-5. Money-action gates ───────────────────────────────────────────────────

class DepositTermsGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_no_terms_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_user_without_terms_cannot_deposit(self):
        """POST /deposit/ without terms → blocked with error, no Deposit row."""
        from simulator.models import Deposit
        r = self.client.post(DEPOSIT_URL, {
            "amount_usd": "100.00",
            "crypto_currency": "btc",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "términos")
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)


class WithdrawTermsGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_no_terms_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_user_without_terms_cannot_withdraw(self):
        """POST /withdraw/ without terms → blocked, no WR created, wallet untouched."""
        r = self.client.post(WITHDRAW_URL, {
            "amount_usd": "50.00",
            "crypto_currency": "btc",
            "wallet_address": "bc1qtest000000000000000000000000000000000",
            "otp_code": "000000",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "términos")
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500"))


class CreateAccountTermsGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_no_terms_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_user_without_terms_cannot_create_real_account(self):
        """POST /accounts/create/ for REAL product without terms → blocked."""
        from simulator.models import TradingAccount
        product = make_account_product(
            product_type=AccountProduct.TYPE_RETAIL,
            min_deposit=Decimal("100.00"),
        )
        r = self.client.post(CREATE_URL, {
            "product_id": product.pk,
            "amount": "100.00",
        }, follow=True)
        self.assertContains(r, "términos")
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)

    def test_user_without_terms_can_create_demo_account(self):
        """POST /accounts/create/ for DEMO product is allowed without terms."""
        from simulator.models import TradingAccount
        product = make_account_product(
            product_type=AccountProduct.TYPE_RETAIL,
            min_deposit=Decimal("0.00"),
        )
        product.family = AccountProduct.FAMILY_DEMO
        product.save()

        self.client.post(CREATE_URL, {"product_id": product.pk}, follow=True)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)


class ChallengePurchaseTermsGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_no_terms_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_user_without_terms_cannot_buy_challenge(self):
        """POST to challenge purchase without terms → blocked, no Deposit created."""
        from simulator.models import Deposit
        product = make_challenge_product(price_usd=Decimal("99.00"))
        url = reverse("simulator:challenge_purchase", args=[product.pk])
        r = self.client.post(url, {"crypto_currency": "btc"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "términos")
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)


# ── 6. Verified user with terms passes all gates ──────────────────────────────

class VerifiedUserWithTermsTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(email_verified=True, terms_accepted=True)
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_user_with_terms_passes_deposit_gate(self):
        """User with accepted terms passes the gate (NP call may fail, gate is open)."""
        from simulator.models import Deposit
        with patch("simulator.nowpayments.create_payment", side_effect=Exception("NP down")):
            self.client.post(DEPOSIT_URL, {
                "amount_usd": "100.00",
                "crypto_currency": "btc",
            })
        # Deposit row created means compliance gates passed
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 1)


# ── 7. Stale terms version rejected ──────────────────────────────────────────

class StaleTermsVersionTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = make_user(email_verified=True, terms_accepted=False)
        # Create acceptance for an OLD version
        TermsAcceptance.objects.create(
            user=self.user,
            terms_version="2024-01-v0",          # stale
            risk_disclaimer_version="2024-01-v0",
            ip_address=None,
            user_agent="",
        )
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_old_terms_version_is_rejected(self):
        """Acceptance of an old version does NOT pass the current gate."""
        from simulator.models import Deposit
        r = self.client.post(DEPOSIT_URL, {
            "amount_usd": "100.00",
            "crypto_currency": "btc",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "términos")
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)


# ── 8-10. accept_terms_view behaviour ────────────────────────────────────────

class AcceptTermsViewTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user = _make_no_terms_user()
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_get_renders_accept_form(self):
        r = self.client.get(ACCEPT_URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "accept_terms")

    def test_partial_checkboxes_shows_error(self):
        """Submitting without all checkboxes shows an error and creates no record."""
        r = self.client.post(ACCEPT_URL, {"accept_terms": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "casillas")
        self.assertFalse(
            TermsAcceptance.objects.filter(
                user=self.user,
                terms_version=TERMS_VERSION,
                risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
            ).exists()
        )

    def test_accept_view_creates_terms_acceptance(self):
        """POST with all checkboxes creates a TermsAcceptance record."""
        self.client.post(ACCEPT_URL, _ALL_CHECKS)
        self.assertTrue(
            TermsAcceptance.objects.filter(
                user=self.user,
                terms_version=TERMS_VERSION,
                risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
            ).exists()
        )

    def test_accept_view_records_user_agent(self):
        """TermsAcceptance row stores the user_agent from the request."""
        self.client.post(ACCEPT_URL, _ALL_CHECKS,
                         HTTP_USER_AGENT="TestBrowser/1.0")
        ta = TermsAcceptance.objects.get(
            user=self.user,
            terms_version=TERMS_VERSION,
            risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
        )
        self.assertIn("TestBrowser", ta.user_agent)

    def test_accept_view_redirects_to_home_by_default(self):
        r = self.client.post(ACCEPT_URL, _ALL_CHECKS)
        self.assertRedirects(r, reverse("simulator:home"),
                             fetch_redirect_response=False)

    def test_accept_view_redirects_to_next(self):
        """?next= parameter routes user back to original destination."""
        r = self.client.post(f"{ACCEPT_URL}?next=/deposit/", _ALL_CHECKS)
        self.assertRedirects(r, "/deposit/", fetch_redirect_response=False)

    def test_accept_view_ignores_offsite_next(self):
        """Absolute external URLs in ?next= are silently ignored."""
        r = self.client.post(f"{ACCEPT_URL}?next=https://evil.com/", _ALL_CHECKS)
        self.assertRedirects(r, reverse("simulator:home"),
                             fetch_redirect_response=False)

    def test_already_accepted_redirects_immediately(self):
        """User who already accepted current terms is not shown the form again."""
        TermsAcceptance.objects.create(
            user=self.user,
            terms_version=TERMS_VERSION,
            risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
            ip_address=None,
            user_agent="",
        )
        r = self.client.get(ACCEPT_URL)
        self.assertRedirects(r, reverse("simulator:home"),
                             fetch_redirect_response=False)
