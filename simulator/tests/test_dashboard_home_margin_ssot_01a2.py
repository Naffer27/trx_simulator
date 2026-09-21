# simulator/tests/test_dashboard_home_margin_ssot_01a2.py
"""
DASHBOARD-UX-01A.2 — regression coverage for the home_view margin bug
audited in DASHBOARD-UX-01A.1: home_view computed
`sum(avg_price * qty / leverage)`, omitting contract_size, the
per-instrument leverage cap, and FX conversion entirely — understating
required margin by 80%-99.999% depending on instrument (confirmed
numerically for EUR/USD, USD/JPY, BTCUSD in the audit). home_view now
calls the shared simulator.views._home_total_margin() helper, which
mirrors simulator.admin._retail_total_margin() and reuses
pnl_engine.calculate_required_margin() per position — the same formula
the live order-open/pretrade-guard/Trading Panel/admin-display paths
already use.

Covers:
  1.  _home_total_margin() matches pnl_engine.calculate_required_margin()
      for EUR/USD (forex major, contract_size=100,000).
  2.  ... for USD/JPY (cross-currency: base=USD, quote=JPY).
  3.  ... for BTCUSD (crypto, contract_size=1, lower max_leverage cap).
  4.  Sums correctly across multiple positions/instruments.
  5.  Returns 0.0 for an account with no open positions.
  6.  Respects the per-instrument leverage cap (BTCUSD max_leverage=20
      even when account.leverage is higher).
  7.  The old buggy formula is proven wrong by contrast, for each of the
      3 instruments (documents the bug this block fixes).
  8.  End-to-end HTTP test: GET /home/ renders the SSOT-derived
      margin_used, not the old buggy value.
  9.  free_margin/margin_level are confirmed absent from home_view's
      context both before and after this fix (no new metrics
      introduced, per DASHBOARD-UX-01A.1 Section D / this block's
      Section 4 scope restriction).
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator import pnl_engine
from simulator.views import _home_total_margin
from simulator.tests.factories import make_account, make_position, make_user


class HomeTotalMarginHelperTests(TestCase):
    """Direct unit coverage of simulator.views._home_total_margin()."""

    def setUp(self):
        self.account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        self.account.leverage = 100
        self.account.currency = "USD"
        self.account.save(update_fields=["leverage", "currency"])

    # ── EUR/USD ──────────────────────────────────────────────────────────

    def test_eurusd_matches_engine_ssot(self):
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        positions = list(self.account.positions.all())
        home_total = _home_total_margin(self.account, positions)

        expected, err = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.17000"), Decimal("1.0"), 100, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(home_total, expected, places=6)
        self.assertAlmostEqual(home_total, 1170.0, places=2)

    def test_eurusd_old_buggy_formula_was_wrong_by_contrast(self):
        price, qty, lev = 1.17000, 1.0, 100
        old_buggy_value = (qty * price) / lev
        self.assertAlmostEqual(old_buggy_value, 0.0117, places=4)
        # ~99.999% understated vs the real $1,170.00 required margin.
        self.assertLess(old_buggy_value, 1.0)

    # ── USD/JPY (cross-currency: base=USD, quote=JPY) ──────────────────

    def test_usdjpy_matches_engine_ssot(self):
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        positions = list(self.account.positions.all())
        home_total = _home_total_margin(self.account, positions)

        expected, err = pnl_engine.calculate_required_margin(
            "USD/JPY", Decimal("155.000"), Decimal("1.0"), 100, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(home_total, expected, places=6)
        self.assertAlmostEqual(home_total, 1000.0, places=2)

    def test_usdjpy_old_buggy_formula_was_wrong_by_contrast(self):
        price, qty, lev = 155.000, 1.0, 100
        old_buggy_value = (qty * price) / lev
        self.assertAlmostEqual(old_buggy_value, 1.55, places=2)
        # ~99.845% understated vs the real $1,000.00 required margin.
        self.assertLess(old_buggy_value, 2.0)

    # ── BTCUSD (crypto, contract_size=1, max_leverage=20) ──────────────

    def test_btcusd_matches_engine_ssot_and_leverage_cap(self):
        make_position(self.account, symbol="BTCUSD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("82000.0"))
        positions = list(self.account.positions.all())
        home_total = _home_total_margin(self.account, positions)

        # Account leverage=100, but BTCUSD's own spec caps max_leverage=20,
        # so effective_leverage must be 20, not 100.
        expected, err = pnl_engine.calculate_required_margin(
            "BTCUSD", Decimal("82000.0"), Decimal("1.0"), 20, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(home_total, expected, places=6)
        self.assertAlmostEqual(home_total, 4100.0, places=2)

    def test_btcusd_old_buggy_formula_ignored_leverage_cap_by_contrast(self):
        price, qty, acct_lev = 82000.0, 1.0, 100
        old_buggy_value = (qty * price) / acct_lev  # ignores the 20x cap
        self.assertAlmostEqual(old_buggy_value, 820.0, places=2)
        # ~80% understated vs the real $4,100.00 required margin.
        self.assertLess(old_buggy_value, 4100.0)

    # ── Aggregation / edge cases ─────────────────────────────────────────

    def test_sums_across_multiple_positions_and_instruments(self):
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        make_position(self.account, symbol="BTCUSD", side="SELL",
                      qty=Decimal("0.5"), avg_price=Decimal("82000.0"))
        positions = list(self.account.positions.all())
        home_total = _home_total_margin(self.account, positions)

        m1, _ = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.17000"), Decimal("1.0"), 100, "USD")
        m2, _ = pnl_engine.calculate_required_margin(
            "BTCUSD", Decimal("82000.0"), Decimal("0.5"), 20, "USD")
        self.assertAlmostEqual(home_total, m1 + m2, places=6)

    def test_no_open_positions_returns_zero(self):
        self.assertEqual(_home_total_margin(self.account, []), 0.0)


class HomeViewMarginHttpTests(TestCase):
    """End-to-end: GET /home/ must render the SSOT-derived margin_used,
    matching pnl_engine.calculate_required_margin(), not the old formula."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL",
                                    balance=Decimal("10000"))
        self.account.leverage = 100
        self.account.currency = "USD"
        self.account.save(update_fields=["leverage", "currency"])
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()

    def test_home_view_requires_login(self):
        self.client.logout()
        r = self.client.get(reverse("simulator:home"))
        self.assertNotEqual(r.status_code, 200)

    def test_home_view_no_positions_zero_margin(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["margin_used"], 0.0)

    def test_home_view_eurusd_position_matches_engine_ssot(self):
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)

        expected, err = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.17000"), Decimal("1.0"), 100, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(r.context["margin_used"], round(expected, 2), places=2)
        # Proves the old buggy ~$0.01 figure is no longer rendered.
        self.assertGreater(r.context["margin_used"], 100.0)

    def test_home_view_usdjpy_position_matches_engine_ssot(self):
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)

        expected, err = pnl_engine.calculate_required_margin(
            "USD/JPY", Decimal("155.000"), Decimal("1.0"), 100, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(r.context["margin_used"], round(expected, 2), places=2)

    def test_home_view_btcusd_position_matches_engine_ssot(self):
        make_position(self.account, symbol="BTCUSD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("82000.0"))
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)

        expected, err = pnl_engine.calculate_required_margin(
            "BTCUSD", Decimal("82000.0"), Decimal("1.0"), 20, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(r.context["margin_used"], round(expected, 2), places=2)

    def test_home_view_contract_preserves_no_free_margin_or_margin_level(self):
        """DASHBOARD-UX-01A.1 confirmed home_view never computed
        free_margin/margin_level. This block fixes margin_used only and
        introduces no new metrics — confirm that contract is unchanged."""
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("free_margin", r.context)
        self.assertNotIn("margin_level", r.context)

    def test_home_view_other_context_keys_unchanged(self):
        """The fix must not touch balance/equity/positions-count/other
        pre-existing context keys."""
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["account"].pk, self.account.pk)
        self.assertEqual(r.context["open_positions_count"], 1)
        for key in (
            "all_accounts", "pnl_today", "daily_dd_pct", "total_trades",
            "recent_moves", "recent_trades", "upcoming_events",
            "active_bonuses_ct", "referral", "recent_docs", "ea_count",
            "active_section", "readiness",
        ):
            self.assertIn(key, r.context)
