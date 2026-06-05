# simulator/tests/test_account_products_flow.py
"""
Phase 6A — AccountProduct catalog + create_account_view tests.

Covers:
  - AccountProduct new fields persist correctly
  - code uniqueness
  - Demo account creation (no wallet debit, uses default_balance)
  - Real account creation (wallet transfer, min_deposit validation)
  - Inactive product rejected
  - CHALLENGE/FUNDED blocked
  - Catalog view sends demo_products / real_products to template
  - Email confirmation triggers on success, not on failure
"""
from decimal import Decimal
from unittest.mock import patch, call

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from simulator.models import AccountProduct, TradingAccount
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import credit_wallet, get_or_create_wallet

User = get_user_model()

ACCOUNTS_URL = "/accounts/"
CREATE_URL   = "/accounts/create/"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_product(
    code="test-standard",
    name="Test Standard",
    product_type=AccountProduct.TYPE_STANDARD,
    family=AccountProduct.FAMILY_REAL,
    min_deposit=Decimal("10.00"),
    default_balance=Decimal("0.00"),
    max_leverage=100,
    typical_spread_pips=Decimal("1.20"),
    commission_per_lot=Decimal("0.00"),
    is_popular=False,
    sort_order=10,
    is_active=True,
):
    return AccountProduct.objects.create(
        code=code, name=name, product_type=product_type, family=family,
        min_deposit=min_deposit, default_balance=default_balance,
        max_leverage=max_leverage, typical_spread_pips=typical_spread_pips,
        commission_per_lot=commission_per_lot, is_popular=is_popular,
        sort_order=sort_order, is_active=is_active,
    )


def _make_demo_product(code="test-demo", **kwargs):
    return _make_product(
        code=code, name="Test Demo", product_type=AccountProduct.TYPE_DEMO,
        family=AccountProduct.FAMILY_DEMO, min_deposit=Decimal("0"),
        default_balance=Decimal("10000"), **kwargs,
    )


def _login(client, user, password="testpass123"):
    client.login(username=user.username, password=password)


# ── Model field tests ─────────────────────────────────────────────────────────

class AccountProductFieldTests(TestCase):
    def test_new_fields_persist(self):
        p = _make_product(
            typical_spread_pips=Decimal("0.50"),
            commission_per_lot=Decimal("7.00"),
            is_popular=True,
            sort_order=5,
            default_balance=Decimal("5000"),
        )
        p.refresh_from_db()
        self.assertEqual(p.typical_spread_pips, Decimal("0.50"))
        self.assertEqual(p.commission_per_lot, Decimal("7.00"))
        self.assertTrue(p.is_popular)
        self.assertEqual(p.sort_order, 5)
        self.assertEqual(p.default_balance, Decimal("5000"))

    def test_code_unique(self):
        _make_product(code="unique-code")
        with self.assertRaises(IntegrityError):
            _make_product(code="unique-code")

    def test_family_choices(self):
        demo = _make_demo_product()
        real = _make_product()
        self.assertEqual(demo.family, AccountProduct.FAMILY_DEMO)
        self.assertEqual(real.family, AccountProduct.FAMILY_REAL)

    def test_platform_label_default(self):
        p = _make_product()
        self.assertEqual(p.platform_label, "Money Broker")

    def test_ordering_by_family_then_sort_order(self):
        _make_demo_product(code="d1", sort_order=20)
        _make_demo_product(code="d2", sort_order=10)
        _make_product(code="r1", sort_order=20)
        _make_product(code="r2", sort_order=10)
        codes = list(AccountProduct.objects.values_list("code", flat=True))
        self.assertEqual(codes[:2], ["d2", "d1"])   # DEMO first, sorted by sort_order
        self.assertEqual(codes[2:], ["r2", "r1"])   # REAL second


# ── Demo account creation ─────────────────────────────────────────────────────

class DemoAccountCreationTests(TestCase):
    def setUp(self):
        self.user    = make_user(email="demo_user@test.com")
        self.wallet, _= get_or_create_wallet(self.user)
        self.product = _make_demo_product()
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_demo_creates_account(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.assertEqual(TradingAccount.objects.filter(user=self.user, account_type="DEMO").count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_demo_uses_default_balance(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        account = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(account.balance, Decimal("10000"))

    @patch("simulator.tasks.send_email_async")
    def test_demo_does_not_debit_wallet(self, _mock_email):
        before = self.wallet.available_balance
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)

    @patch("simulator.tasks.send_email_async")
    def test_demo_account_type_is_demo(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        account = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(account.account_type, "DEMO")

    @patch("simulator.tasks.send_email_async")
    def test_demo_account_status_is_active(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        account = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(account.status, TradingAccount.STATUS_ACTIVE)

    @patch("simulator.tasks.send_email_async")
    def test_demo_leverage_from_product(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        account = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(account.leverage, self.product.max_leverage)

    @patch("simulator.tasks.send_email_async")
    def test_demo_triggers_email(self, mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        mock_email.delay.assert_called_once()
        kwargs = mock_email.delay.call_args[1]
        self.assertIn("Demo", kwargs["subject"])
        self.assertIn("Money Broker", kwargs["subject"])

    @patch("simulator.tasks.send_email_async")
    def test_demo_email_includes_account_id_and_product_name(self, mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        account = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        message = mock_email.delay.call_args[1]["message"]
        self.assertIn(str(account.id), message)
        self.assertIn(self.product.name, message)


# ── Real account creation ─────────────────────────────────────────────────────

class RealAccountCreationTests(TestCase):
    def setUp(self):
        self.user    = make_user(email="real_user@test.com")
        self.wallet  = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = _make_product(min_deposit=Decimal("10.00"))
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_real_creates_account(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async")
    def test_real_transfers_from_wallet(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "100"})
        self.wallet.refresh_from_db()
        account = TradingAccount.objects.get(user=self.user)
        self.assertAlmostEqual(float(self.wallet.available_balance), 400.0, places=2)
        self.assertAlmostEqual(float(account.balance), 100.0, places=2)

    @patch("simulator.tasks.send_email_async")
    def test_real_account_type_from_product(self, _mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        account = TradingAccount.objects.get(user=self.user)
        self.assertEqual(account.account_type, self.product.product_type)

    def test_real_below_min_deposit_redirects_with_error(self):
        # Read session from the request object before accounts_view pops it
        r = self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "5"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)
        self.assertIn("acct_error", r.wsgi_request.session)
        self.assertIn("mínimo", r.wsgi_request.session["acct_error"])

    def test_real_insufficient_wallet_redirects_with_error(self):
        r = self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "600"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)
        self.assertIn("acct_error", r.wsgi_request.session)

    @patch("simulator.tasks.send_email_async")
    def test_real_triggers_email(self, mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        mock_email.delay.assert_called_once()
        kwargs = mock_email.delay.call_args[1]
        self.assertIn("Real", kwargs["subject"])
        self.assertIn("Money Broker", kwargs["subject"])

    @patch("simulator.tasks.send_email_async")
    def test_real_email_includes_account_id_and_product_name(self, mock_email):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "50"})
        account = TradingAccount.objects.get(user=self.user)
        message = mock_email.delay.call_args[1]["message"]
        self.assertIn(str(account.id), message)
        self.assertIn(self.product.name, message)

    def test_failed_creation_does_not_send_email(self):
        with patch("simulator.tasks.send_email_async") as mock_email:
            self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "1"})
            mock_email.delay.assert_not_called()


# ── Guard rails ───────────────────────────────────────────────────────────────

class GuardTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("500"))
        _login(self.client, self.user)

    def test_inactive_product_returns_error(self):
        product = _make_product(code="inactive-p", is_active=False)
        r = self.client.post(CREATE_URL, {"product_id": product.pk, "amount": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)
        self.assertIn("acct_error", r.wsgi_request.session)

    def test_invalid_product_id_returns_error(self):
        r = self.client.post(CREATE_URL, {"product_id": "99999", "amount": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("acct_error", r.wsgi_request.session)

    def test_challenge_product_type_blocked(self):
        product = _make_product(code="challenge-p", product_type="CHALLENGE")
        r = self.client.post(CREATE_URL, {"product_id": product.pk, "amount": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)
        self.assertIn("acct_error", r.wsgi_request.session)

    def test_funded_product_type_blocked(self):
        product = _make_product(code="funded-p", product_type="FUNDED")
        r = self.client.post(CREATE_URL, {"product_id": product.pk, "amount": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TradingAccount.objects.filter(user=self.user).count(), 0)
        self.assertIn("acct_error", r.wsgi_request.session)

    def test_get_redirects_to_accounts(self):
        r = self.client.get(CREATE_URL)
        self.assertRedirects(r, ACCOUNTS_URL)

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        r = self.client.post(CREATE_URL, {"product_id": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


# ── Catalog view ──────────────────────────────────────────────────────────────

class CatalogViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        _login(self.client, self.user)

    def test_catalog_shows_active_products(self):
        p1 = _make_product(code="active-1")
        p2 = _make_demo_product(code="active-2")
        r = self.client.get(ACCOUNTS_URL)
        self.assertEqual(r.status_code, 200)
        real_ids  = [p.pk for p in r.context["real_products"]]
        demo_ids  = [p.pk for p in r.context["demo_products"]]
        self.assertIn(p1.pk, real_ids)
        self.assertIn(p2.pk, demo_ids)

    def test_catalog_excludes_inactive(self):
        inactive = _make_product(code="inactive-cat", is_active=False)
        r = self.client.get(ACCOUNTS_URL)
        real_ids = [p.pk for p in r.context["real_products"]]
        demo_ids = [p.pk for p in r.context["demo_products"]]
        self.assertNotIn(inactive.pk, real_ids + demo_ids)

    def test_catalog_groups_demo_and_real(self):
        demo = _make_demo_product(code="grp-demo")
        real = _make_product(code="grp-real")
        r = self.client.get(ACCOUNTS_URL)
        demo_codes = [p.code for p in r.context["demo_products"]]
        real_codes = [p.code for p in r.context["real_products"]]
        self.assertIn("grp-demo", demo_codes)
        self.assertIn("grp-real", real_codes)
        self.assertNotIn("grp-real", demo_codes)
        self.assertNotIn("grp-demo", real_codes)

    def test_catalog_response_200_for_authenticated(self):
        r = self.client.get(ACCOUNTS_URL)
        self.assertEqual(r.status_code, 200)

    def test_catalog_redirects_unauthenticated(self):
        self.client.logout()
        r = self.client.get(ACCOUNTS_URL)
        self.assertEqual(r.status_code, 302)
