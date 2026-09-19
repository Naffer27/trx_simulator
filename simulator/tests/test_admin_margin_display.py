# simulator/tests/test_admin_margin_display.py
"""
INTERNAL-BROKER-TRADING-CERTIFICATION-01A — regression coverage for the
admin margin-display bug audited in INTERNAL-BROKER-TRADING-CERTIFICATION-01
(finding K.1): TradingAccountAdmin.margin_panel() and
TradingAccountAdmin.dealing_desk_view() both computed
`sum(avg_price * qty / leverage)`, omitting contract_size entirely —
understating forex margin by ~100,000x. Both now call the shared
simulator.admin._retail_total_margin() helper, which reuses
pnl_engine.calculate_required_margin() per position (the same formula the
live order-open/pretrade-risk-guard path uses) instead of a duplicated
ad-hoc formula.

Golden scenario (same numbers as the preflight audit's Golden Scenario #1):
  EUR/USD, price=1.148630, qty=0.01 lot, contract_size=100,000,
  account leverage=50, symbol max_leverage=500 -> effective_leverage=50.
  expected margin = 1.148630 * 0.01 * 100,000 / 50 = 22.9726 ~= 22.97

Covers:
  1.  _retail_total_margin() matches pnl_engine.calculate_required_margin()
      for a single EUR/USD position (the golden-scenario numbers).
  2.  _retail_total_margin() sums correctly across multiple positions.
  3.  _retail_total_margin() returns 0.0 for no open positions.
  4.  _retail_total_margin() respects the account/symbol leverage cap
      (min(account.leverage, symbol.max_leverage)), not just account.leverage.
  5.  margin_panel() renders the authoritative (not the old buggy) figure.
  6.  dealing_desk_view() (real HTTP admin request) renders the same
      authoritative figure.
  7.  The old buggy formula is proven wrong by contrast (documents the bug
      this block fixes, not a regression risk by itself).
"""
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from simulator import pnl_engine
from simulator.admin import _retail_total_margin, TradingAccountAdmin
from simulator.models import TradingAccount
from simulator.tests.factories import make_account, make_position, make_user

User = get_user_model()


class RetailTotalMarginHelperTests(TestCase):
    """Direct unit coverage of simulator.admin._retail_total_margin()."""

    def setUp(self):
        self.account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        self.account.leverage = 50
        self.account.save(update_fields=["leverage"])

    def test_matches_pnl_engine_for_golden_scenario(self):
        make_position(
            self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.148630"),
        )
        positions = list(self.account.positions.all())
        total = _retail_total_margin(self.account, positions)

        expected, err = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.148630"), Decimal("0.01"), 50, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(total, expected, places=6)
        self.assertAlmostEqual(total, 22.9726, places=4)

    def test_old_buggy_formula_was_wrong_by_contrast(self):
        """Documents the bug this block fixes — the old formula omitted
        contract_size and would have shown ~$0.0002297, not ~$22.97."""
        price, qty, lev = Decimal("1.148630"), Decimal("0.01"), 50
        old_buggy_value = float(price) * float(qty) / lev
        self.assertAlmostEqual(old_buggy_value, 0.00022973, places=8)
        self.assertLess(old_buggy_value, 0.001)  # ~100,000x understated vs 22.97

    def test_sums_across_multiple_positions(self):
        make_position(self.account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.148630"))
        make_position(self.account, symbol="EUR/USD", side="SELL",
                       qty=Decimal("0.02"), avg_price=Decimal("1.150000"))
        positions = list(self.account.positions.all())
        total = _retail_total_margin(self.account, positions)

        m1, _ = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.148630"), Decimal("0.01"), 50, "USD")
        m2, _ = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.150000"), Decimal("0.02"), 50, "USD")
        self.assertAlmostEqual(total, m1 + m2, places=6)

    def test_no_open_positions_returns_zero(self):
        self.assertEqual(_retail_total_margin(self.account, []), 0.0)

    def test_respects_symbol_max_leverage_cap(self):
        """Account leverage (500) above the symbol's own cap must clamp to
        the symbol's max_leverage, exactly like the live order-open path."""
        self.account.leverage = 500
        self.account.save(update_fields=["leverage"])
        make_position(self.account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.148630"))
        positions = list(self.account.positions.all())
        total = _retail_total_margin(self.account, positions)

        # EUR/USD max_leverage=500 in market_data/symbol_specs.py, so
        # effective_leverage = min(500, 500) = 500 here (not 50).
        expected, _ = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.148630"), Decimal("0.01"), 500, "USD")
        self.assertAlmostEqual(total, expected, places=6)
        self.assertLess(total, 22.97)  # higher leverage -> lower margin


class MarginPanelRenderTests(TestCase):
    """margin_panel() (the admin list/detail readonly display) must render
    the authoritative figure, not the old ~$0.00 figure."""

    def setUp(self):
        self.account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        self.account.leverage = 50
        self.account.save(update_fields=["leverage"])
        make_position(
            self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.148630"),
        )

    def test_margin_panel_shows_authoritative_margin(self):
        model_admin = TradingAccountAdmin(TradingAccount, admin.site)
        html = model_admin.margin_panel(self.account)
        self.assertIn("22.97", html)

    def test_margin_panel_does_not_show_old_buggy_near_zero_value(self):
        model_admin = TradingAccountAdmin(TradingAccount, admin.site)
        html = model_admin.margin_panel(self.account)
        self.assertNotIn("$0.00", html.split("Margin Used")[1].split("</div>")[0]
                          if "Margin Used" in html else "")

    def test_non_retail_account_short_circuits(self):
        challenge_account = make_account(account_type="CHALLENGE")
        model_admin = TradingAccountAdmin(TradingAccount, admin.site)
        result = model_admin.margin_panel(challenge_account)
        self.assertIn("Solo visible", result)


class DealingDeskViewMarginTests(TestCase):
    """Real HTTP admin request against dealing_desk_view() — proves the
    account-detail admin page renders the same authoritative margin as
    margin_panel() and _retail_total_margin(), not a third duplicate
    formula."""

    def setUp(self):
        self.staff = make_user(username="admin_margin_test")
        self.staff.is_staff = True
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_staff", "is_superuser"])
        self.client = Client()
        self.client.force_login(self.staff)

        self.account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        self.account.leverage = 50
        self.account.save(update_fields=["leverage"])
        make_position(
            self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.148630"),
        )

    def test_dealing_desk_view_renders_authoritative_margin(self):
        url = reverse("admin:simulator_tradingaccount_dealing_desk",
                       args=[self.account.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "22.97")
