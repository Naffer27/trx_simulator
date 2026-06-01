"""
simulator/tests/test_dashboard_intelligence.py — Phase 4A Pasos 1-3

Verifies that trading_dashboard view injects intelligence context keys
and that their values are correct for known account states.

Does NOT test template rendering — only context dict completeness and values.
"""
import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import AccountEquitySnapshot, LedgerEntry
from simulator.tests.factories import make_account, make_ledger_entry, make_trade, make_user


# ─────────────────────────────────────────────────────────────────────────────
# 1. All intelligence keys are present in the context
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardIntelligenceContextKeys(TestCase):
    """trading_dashboard must include all Phase 4A intelligence keys."""

    def setUp(self):
        self.user    = make_user()
        self.account = make_account(user=self.user, account_type="CHALLENGE",
                                    tier="10K", balance=Decimal("10000"))
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_realized_dd_pct_key_present(self):
        self.assertIn("realized_dd_pct", self._ctx())

    def test_daily_realized_dd_pct_key_present(self):
        self.assertIn("daily_realized_dd_pct", self._ctx())

    def test_today_realized_pnl_key_present(self):
        self.assertIn("today_realized_pnl", self._ctx())

    def test_profit_pct_key_present(self):
        self.assertIn("profit_pct", self._ctx())

    def test_has_profit_target_key_present(self):
        self.assertIn("has_profit_target", self._ctx())

    def test_intel_max_dd_pct_key_present(self):
        self.assertIn("intel_max_dd_pct", self._ctx())

    def test_intel_max_daily_pct_key_present(self):
        self.assertIn("intel_max_daily_pct", self._ctx())

    def test_equity_curve_key_present(self):
        self.assertIn("equity_curve", self._ctx())

    def test_trader_score_key_present(self):
        self.assertIn("trader_score", self._ctx())

    def test_recent_violations_key_present(self):
        self.assertIn("recent_violations", self._ctx())

    def test_win_rate_pct_key_present(self):
        self.assertIn("win_rate_pct", self._ctx())

    def test_total_realized_pnl_key_present(self):
        self.assertIn("total_realized_pnl", self._ctx())


# ─────────────────────────────────────────────────────────────────────────────
# 2. Correct values for known account states
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardIntelligenceValues(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _get(self, account):
        url = reverse("simulator:dashboard_account", args=[account.pk])
        return self.client.get(url)

    # ── Realized drawdown ─────────────────────────────────────────────────────

    def test_realized_dd_pct_zero_when_balance_equals_peak(self):
        """balance == peak_balance → realized_dd_pct = 0."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        self.assertAlmostEqual(self._get(account).context["realized_dd_pct"], 0.0, places=2)

    def test_realized_dd_pct_correct_when_balance_below_peak(self):
        """peak=10000, balance updated to 9000 via update() → realized_dd_pct=10%."""
        from simulator.models import TradingAccount as _TA
        account = make_account(user=self.user, balance=Decimal("10000"))
        _TA.objects.filter(pk=account.pk).update(balance=Decimal("9000"))
        account.refresh_from_db()
        self.assertAlmostEqual(self._get(account).context["realized_dd_pct"], 10.0, places=2)

    # ── Daily realized drawdown ───────────────────────────────────────────────

    def test_daily_realized_dd_pct_zero_with_no_ledger_today(self):
        account = make_account(user=self.user)
        self.assertAlmostEqual(
            self._get(account).context["daily_realized_dd_pct"], 0.0, places=2
        )

    def test_daily_realized_dd_pct_computed_from_today_loss(self):
        """Loss of -500 today on peak=10000 → daily_realized_dd_pct = 5%."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        make_ledger_entry(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=Decimal("-500.00"),
            balance_after=Decimal("9500.00"),
        )
        self.assertAlmostEqual(
            self._get(account).context["daily_realized_dd_pct"], 5.0, places=2
        )

    def test_daily_realized_dd_pct_zero_for_gains(self):
        """Positive today_pnl → daily_realized_dd_pct = 0 (no drawdown)."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        make_ledger_entry(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=Decimal("200.00"),
            balance_after=Decimal("10200.00"),
        )
        self.assertAlmostEqual(
            self._get(account).context["daily_realized_dd_pct"], 0.0, places=2
        )

    # ── Profit target ─────────────────────────────────────────────────────────

    def test_profit_pct_none_when_no_profit_target(self):
        """profit_target=NULL → profit_pct=None, has_profit_target=False."""
        account = make_account(user=self.user)
        ctx = self._get(account).context
        self.assertIsNone(ctx["profit_pct"])
        self.assertFalse(ctx["has_profit_target"])

    def test_profit_pct_computed_when_profit_target_set(self):
        """profit_target=1000, balance=10500, initial=10000 → profit_pct≈50%."""
        from simulator.models import TradingAccount as _TA
        account = make_account(user=self.user, balance=Decimal("10000"))
        _TA.objects.filter(pk=account.pk).update(
            balance=Decimal("10500"),
            profit_target=Decimal("1000"),
        )
        account.refresh_from_db()
        ctx = self._get(account).context
        self.assertTrue(ctx["has_profit_target"])
        self.assertAlmostEqual(ctx["profit_pct"], 50.0, places=1)

    # ── Tier limits ───────────────────────────────────────────────────────────

    def test_tier_limits_challenge_10k(self):
        """CHALLENGE 10K → intel_max_dd_pct=10.0, intel_max_daily_pct=5.0."""
        account = make_account(user=self.user, account_type="CHALLENGE", tier="10K")
        ctx = self._get(account).context
        self.assertAlmostEqual(ctx["intel_max_dd_pct"],    10.0, places=1)
        self.assertAlmostEqual(ctx["intel_max_daily_pct"],  5.0, places=1)

    def test_tier_limits_challenge_50k(self):
        """CHALLENGE 50K → intel_max_dd_pct=8.0, intel_max_daily_pct=4.0."""
        account = make_account(user=self.user, account_type="CHALLENGE",
                               tier="50K", balance=Decimal("50000"))
        ctx = self._get(account).context
        self.assertAlmostEqual(ctx["intel_max_dd_pct"],    8.0, places=1)
        self.assertAlmostEqual(ctx["intel_max_daily_pct"], 4.0, places=1)

    # ── Equity curve ──────────────────────────────────────────────────────────

    def test_equity_curve_empty_when_no_snapshots(self):
        account = make_account(user=self.user)
        self.assertEqual(self._get(account).context["equity_curve"], [])

    # ── Trader score ──────────────────────────────────────────────────────────

    def test_trader_score_none_when_not_created(self):
        account = make_account(user=self.user)
        self.assertIsNone(self._get(account).context["trader_score"])

    # ── Violations ────────────────────────────────────────────────────────────

    def test_recent_violations_empty_when_clean(self):
        account = make_account(user=self.user)
        self.assertEqual(list(self._get(account).context["recent_violations"]), [])

    # ── Trade performance ─────────────────────────────────────────────────────

    def test_win_rate_pct_none_when_no_closed_trades(self):
        account = make_account(user=self.user)
        self.assertIsNone(self._get(account).context["win_rate_pct"])

    def test_total_realized_pnl_zero_when_no_trades(self):
        account = make_account(user=self.user)
        self.assertAlmostEqual(
            self._get(account).context["total_realized_pnl"], 0.0, places=2
        )

    def test_win_rate_pct_correct_with_mixed_trades(self):
        """2 wins, 1 loss → win_rate_pct ≈ 66.67%."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        make_trade(account=account, profit_loss=Decimal("100.00"))
        make_trade(account=account, profit_loss=Decimal("50.00"))
        make_trade(account=account, profit_loss=Decimal("-30.00"))
        ctx = self._get(account).context
        self.assertAlmostEqual(ctx["win_rate_pct"], 200 / 3, places=1)

    def test_total_realized_pnl_sums_all_trades(self):
        """100 + 50 - 30 = 120 total realized pnl."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        make_trade(account=account, profit_loss=Decimal("100.00"))
        make_trade(account=account, profit_loss=Decimal("50.00"))
        make_trade(account=account, profit_loss=Decimal("-30.00"))
        ctx = self._get(account).context
        self.assertAlmostEqual(ctx["total_realized_pnl"], 120.0, places=2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Bar fill helpers (Paso 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardBarHelpers(TestCase):
    """
    Verifies the 4 bar-display variables injected by Paso 2:
    daily_dd_bar_pct, max_dd_bar_pct, daily_dd_safe, max_dd_safe.
    """

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _get(self, account):
        url = reverse("simulator:dashboard_account", args=[account.pk])
        return self.client.get(url)

    def test_bar_helper_keys_present(self):
        account = make_account(user=self.user)
        ctx = self._get(account).context
        for key in ("daily_dd_bar_pct", "max_dd_bar_pct", "daily_dd_safe", "max_dd_safe"):
            self.assertIn(key, ctx)

    def test_daily_dd_bar_pct_zero_when_no_loss(self):
        """No daily loss → daily_dd_bar_pct = 0."""
        account = make_account(user=self.user)
        self.assertAlmostEqual(self._get(account).context["daily_dd_bar_pct"], 0.0, places=2)

    def test_max_dd_bar_pct_zero_at_peak(self):
        """balance == peak → max_dd_bar_pct = 0."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        self.assertAlmostEqual(self._get(account).context["max_dd_bar_pct"], 0.0, places=2)

    def test_daily_dd_bar_pct_capped_at_100(self):
        """Daily loss exceeding the limit still caps bar at 100%."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        # -600 > 5% limit on 10K → bar should be 100, not 120
        make_ledger_entry(account=account, event_type=LedgerEntry.EV_REALIZED,
                          amount=Decimal("-600.00"), balance_after=Decimal("9400.00"))
        bar = self._get(account).context["daily_dd_bar_pct"]
        self.assertLessEqual(bar, 100.0)

    def test_max_dd_bar_pct_proportional(self):
        """peak=10000, balance=9500 → realized_dd=5%, limit=10% → bar=50%."""
        from simulator.models import TradingAccount as _TA
        account = make_account(user=self.user, balance=Decimal("10000"))
        _TA.objects.filter(pk=account.pk).update(balance=Decimal("9500"))
        account.refresh_from_db()
        self.assertAlmostEqual(self._get(account).context["max_dd_bar_pct"], 50.0, places=1)

    def test_daily_dd_safe_true_when_under_half_limit(self):
        """daily_realized_dd_pct = 0 < 2.5 (half of 5%) → daily_dd_safe=True."""
        account = make_account(user=self.user)
        self.assertTrue(self._get(account).context["daily_dd_safe"])

    def test_daily_dd_safe_false_when_over_half_limit(self):
        """Loss of -400 on 10K (4% > 2.5%) → daily_dd_safe=False."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        make_ledger_entry(account=account, event_type=LedgerEntry.EV_REALIZED,
                          amount=Decimal("-400.00"), balance_after=Decimal("9600.00"))
        self.assertFalse(self._get(account).context["daily_dd_safe"])

    def test_max_dd_safe_true_at_peak(self):
        """No drawdown → max_dd_safe=True."""
        account = make_account(user=self.user)
        self.assertTrue(self._get(account).context["max_dd_safe"])

    def test_max_dd_safe_false_when_over_half_limit(self):
        """realized_dd=6% > 5% (half of 10%) → max_dd_safe=False."""
        from simulator.models import TradingAccount as _TA
        account = make_account(user=self.user, balance=Decimal("10000"))
        _TA.objects.filter(pk=account.pk).update(balance=Decimal("9400"))
        account.refresh_from_db()
        self.assertFalse(self._get(account).context["max_dd_safe"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. equity_curve_json (Paso 3)
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardEquityCurveJson(TestCase):
    """
    equity_curve_json must be valid JSON, contain {e, b} dicts,
    and be empty list when no snapshots exist.
    """

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _get(self, account):
        url = reverse("simulator:dashboard_account", args=[account.pk])
        return self.client.get(url)

    def test_equity_curve_json_key_present(self):
        account = make_account(user=self.user)
        self.assertIn("equity_curve_json", self._get(account).context)

    def test_equity_curve_json_is_valid_json_string(self):
        account = make_account(user=self.user)
        raw = self._get(account).context["equity_curve_json"]
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, list)

    def test_equity_curve_json_empty_when_no_snapshots(self):
        account = make_account(user=self.user)
        raw = self._get(account).context["equity_curve_json"]
        self.assertEqual(json.loads(raw), [])

    def test_equity_curve_json_contains_e_and_b_keys(self):
        """Each snapshot serializes to {e: equity, b: balance}."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        AccountEquitySnapshot.objects.create(
            account=account,
            taken_at=timezone.now(),
            balance=Decimal("10000"),
            equity=Decimal("10050"),
        )
        raw = self._get(account).context["equity_curve_json"]
        pts = json.loads(raw)
        self.assertEqual(len(pts), 1)
        self.assertIn("e", pts[0])
        self.assertIn("b", pts[0])
        self.assertAlmostEqual(pts[0]["e"], 10050.0, places=2)
        self.assertAlmostEqual(pts[0]["b"], 10000.0, places=2)

    def test_equity_curve_json_ordered_oldest_first(self):
        """Oldest snapshot appears first in the JSON list."""
        account = make_account(user=self.user, balance=Decimal("10000"))
        now = timezone.now()
        AccountEquitySnapshot.objects.create(
            account=account, taken_at=now,
            balance=Decimal("10000"), equity=Decimal("10100"),
        )
        AccountEquitySnapshot.objects.create(
            account=account, taken_at=now + timezone.timedelta(hours=1),
            balance=Decimal("10000"), equity=Decimal("10200"),
        )
        raw = self._get(account).context["equity_curve_json"]
        pts = json.loads(raw)
        self.assertEqual(len(pts), 2)
        self.assertLessEqual(pts[0]["e"], pts[1]["e"])
