"""
simulator/tests/test_dashboard_funded_section.py — Phase 4C.2

Verifies that trading_dashboard view injects funded-section context keys
and that payout calculations are correct for funded accounts.

All tests use context-dict checks; HTML spot-checks are in the last class.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.models import FundedConfig, LedgerEntry, Trade, TradingAccount
from simulator.tests.factories import (
    make_account,
    make_challenge_enrollment,
    make_challenge_product,
    make_user,
)

_PENNY = Decimal("0.01")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_product(**kwargs):
    defaults = dict(
        tier="10K",
        account_size=Decimal("10000.00"),
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=5,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=3,
        p2_max_duration_days=60,
        profit_split_pct=Decimal("80.00"),
    )
    defaults.update(kwargs)
    return make_challenge_product(**defaults)


def _make_funded_account(user, profit_split_pct="80.00", min_payout_usd="50.00",
                          min_trading_days=5, payout_cycle_days=14):
    """Activate → Phase2 → Funded, then override FundedConfig fields."""
    product = _make_product(profit_split_pct=Decimal(profit_split_pct))
    enrollment = make_challenge_enrollment(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    funded_account = advance_to_funded(enrollment)
    enrollment.refresh_from_db()

    # Patch FundedConfig with test-specific values
    FundedConfig.objects.filter(enrollment=enrollment).update(
        min_payout_usd=Decimal(str(min_payout_usd)),
        min_trading_days=min_trading_days,
        payout_cycle_days=payout_cycle_days,
    )
    funded_account.refresh_from_db()
    enrollment.refresh_from_db()
    return funded_account, enrollment


def _add_closed_trade(account, pnl: Decimal, days_ago: int = 0):
    """Add a closed Trade + LedgerEntry and update account balance."""
    closed = timezone.now() - timezone.timedelta(days=days_ago)
    Trade.objects.create(
        account=account, symbol="EUR/USD", trade_type="BUY",
        lot_size=Decimal("0.1"), entry_price=Decimal("1.10000"),
        exit_price=Decimal("1.11000"), profit_loss=pnl, closed_at=closed,
    )
    new_bal = Decimal(str(account.balance)) + pnl
    LedgerEntry.objects.create(
        account=account, event_type=LedgerEntry.EV_REALIZED,
        amount=pnl, balance_after=new_bal,
    )
    TradingAccount.objects.filter(pk=account.pk).update(balance=new_bal)
    account.refresh_from_db()


# ─────────────────────────────────────────────────────────────────────────────
# 1. All funded-section context keys present
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedSectionContextKeys(TestCase):

    FUNDED_KEYS = [
        "funded_section",
        "funded_cycle_profit",
        "funded_trader_cut",
        "funded_broker_cut",
        "funded_payout_eligible",
        "funded_payout_label",
        "funded_trading_days",
        "funded_min_trading_days",
        "funded_next_payout_date",
        "funded_days_until_payout",
    ]

    def _ctx(self, account, user):
        self.client.force_login(user)
        url = reverse("simulator:dashboard_account", args=[account.pk])
        return self.client.get(url).context

    def test_all_keys_present_for_funded_account(self):
        user = make_user()
        funded_account, _ = _make_funded_account(user)
        ctx = self._ctx(funded_account, user)
        for key in self.FUNDED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")

    def test_all_keys_present_for_challenge_account(self):
        user = make_user()
        account = make_account(user=user, account_type="CHALLENGE", tier="10K")
        ctx = self._ctx(account, user)
        for key in self.FUNDED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")

    def test_all_keys_present_for_retail_account(self):
        user = make_user()
        account = make_account(user=user, account_type="RETAIL", tier=None)
        ctx = self._ctx(account, user)
        for key in self.FUNDED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Non-funded accounts get neutral values
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedSectionNoFundedAccount(TestCase):

    def setUp(self):
        self.user = make_user()
        self.account = make_account(user=self.user, account_type="CHALLENGE", tier="10K")
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_funded_section_is_false(self):
        self.assertFalse(self._ctx()["funded_section"])

    def test_funded_cycle_profit_is_none(self):
        self.assertIsNone(self._ctx()["funded_cycle_profit"])

    def test_funded_trader_cut_is_none(self):
        self.assertIsNone(self._ctx()["funded_trader_cut"])

    def test_funded_broker_cut_is_none(self):
        self.assertIsNone(self._ctx()["funded_broker_cut"])

    def test_funded_payout_eligible_is_none(self):
        self.assertIsNone(self._ctx()["funded_payout_eligible"])

    def test_funded_payout_label_is_none(self):
        self.assertIsNone(self._ctx()["funded_payout_label"])

    def test_funded_trading_days_is_none(self):
        self.assertIsNone(self._ctx()["funded_trading_days"])

    def test_funded_next_payout_date_is_none(self):
        self.assertIsNone(self._ctx()["funded_next_payout_date"])

    def test_retail_account_funded_section_false(self):
        retail = make_account(user=self.user, account_type="RETAIL", tier=None)
        self.client.force_login(self.user)
        url = reverse("simulator:dashboard_account", args=[retail.pk])
        self.assertFalse(self.client.get(url).context["funded_section"])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Payout calculations
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedSectionCalculations(TestCase):

    def setUp(self):
        self.user = make_user()
        self.funded_account, self.enrollment = _make_funded_account(
            self.user, profit_split_pct="80.00", min_payout_usd="50.00"
        )
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.funded_account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    # ── funded_section ────────────────────────────────────────────────────

    def test_funded_section_true_for_funded_account(self):
        self.assertTrue(self._ctx()["funded_section"])

    # ── cycle profit ──────────────────────────────────────────────────────

    def test_cycle_profit_zero_with_no_trades(self):
        self.assertEqual(self._ctx()["funded_cycle_profit"], Decimal("0"))

    def test_cycle_profit_reflects_realized_gain(self):
        _add_closed_trade(self.funded_account, Decimal("200"))
        self.funded_account.refresh_from_db()
        self.assertEqual(self._ctx()["funded_cycle_profit"], Decimal("200.00"))

    def test_cycle_profit_floored_at_zero_when_in_loss(self):
        _add_closed_trade(self.funded_account, Decimal("-300"))
        self.funded_account.refresh_from_db()
        self.assertEqual(self._ctx()["funded_cycle_profit"], Decimal("0"))

    def test_cycle_profit_cumulative_across_trades(self):
        _add_closed_trade(self.funded_account, Decimal("100"))
        self.funded_account.refresh_from_db()
        _add_closed_trade(self.funded_account, Decimal("150"))
        self.funded_account.refresh_from_db()
        self.assertEqual(self._ctx()["funded_cycle_profit"], Decimal("250.00"))

    # ── trader cut ────────────────────────────────────────────────────────

    def test_trader_cut_80pct_of_200_profit(self):
        _add_closed_trade(self.funded_account, Decimal("200"))
        self.funded_account.refresh_from_db()
        # 80% of 200 = 160.00
        self.assertEqual(self._ctx()["funded_trader_cut"], Decimal("160.00"))

    def test_trader_cut_zero_when_no_profit(self):
        self.assertEqual(self._ctx()["funded_trader_cut"], Decimal("0.00"))

    def test_broker_cut_is_remainder(self):
        _add_closed_trade(self.funded_account, Decimal("200"))
        self.funded_account.refresh_from_db()
        ctx = self._ctx()
        # trader 160 + broker 40 = 200
        self.assertEqual(
            ctx["funded_trader_cut"] + ctx["funded_broker_cut"],
            ctx["funded_cycle_profit"],
        )

    def test_broker_cut_20pct_of_200_profit(self):
        _add_closed_trade(self.funded_account, Decimal("200"))
        self.funded_account.refresh_from_db()
        # 20% of 200 = 40.00
        self.assertEqual(self._ctx()["funded_broker_cut"], Decimal("40.00"))

    def test_split_70_30_config(self):
        user = make_user()
        funded_account, _ = _make_funded_account(user, profit_split_pct="70.00")
        _add_closed_trade(funded_account, Decimal("100"))
        funded_account.refresh_from_db()
        self.client.force_login(user)
        url = reverse("simulator:dashboard_account", args=[funded_account.pk])
        ctx = self.client.get(url).context
        self.assertEqual(ctx["funded_trader_cut"], Decimal("70.00"))
        self.assertEqual(ctx["funded_broker_cut"], Decimal("30.00"))

    # ── trading days ──────────────────────────────────────────────────────

    def test_trading_days_zero_with_no_trades(self):
        self.assertEqual(self._ctx()["funded_trading_days"], 0)

    def test_trading_days_increments_per_day(self):
        # Backdate funded_at so trades at days_ago=1,2 pass the filter
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        _add_closed_trade(self.funded_account, Decimal("50"), days_ago=0)
        _add_closed_trade(self.funded_account, Decimal("50"), days_ago=1)
        _add_closed_trade(self.funded_account, Decimal("50"), days_ago=2)
        self.funded_account.refresh_from_db()
        self.assertEqual(self._ctx()["funded_trading_days"], 3)

    def test_multiple_trades_same_day_count_as_one(self):
        _add_closed_trade(self.funded_account, Decimal("50"), days_ago=0)
        _add_closed_trade(self.funded_account, Decimal("50"), days_ago=0)
        self.funded_account.refresh_from_db()
        self.assertEqual(self._ctx()["funded_trading_days"], 1)

    def test_min_trading_days_from_funded_config(self):
        self.assertEqual(self._ctx()["funded_min_trading_days"], 5)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Payout eligibility
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedPayoutEligibility(TestCase):

    def setUp(self):
        self.user = make_user()
        self.funded_account, self.enrollment = _make_funded_account(
            self.user,
            profit_split_pct="80.00",
            min_payout_usd="50.00",
            min_trading_days=3,
        )
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.funded_account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_not_eligible_with_no_trades(self):
        self.assertFalse(self._ctx()["funded_payout_eligible"])

    def test_eligible_when_profit_and_days_met(self):
        # funded_at is set to now() by advance_to_funded; backdate it so that
        # trades at days_ago=1,2 are not filtered out by closed_at__gte=funded_at
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(3):
            _add_closed_trade(self.funded_account, Decimal("30"), days_ago=i)
        self.funded_account.refresh_from_db()
        self.assertTrue(self._ctx()["funded_payout_eligible"])

    def test_not_eligible_when_profit_below_min(self):
        # $40 profit < $50 min, but 3 days reached
        for i in range(3):
            _add_closed_trade(self.funded_account, Decimal("14"), days_ago=i)
        self.funded_account.refresh_from_db()
        ctx = self._ctx()
        self.assertFalse(ctx["funded_payout_eligible"])
        self.assertIn("50", ctx["funded_payout_label"])  # min payout mentioned

    def test_not_eligible_when_trading_days_below_min(self):
        # $100 profit (above min) but only 1 trading day
        _add_closed_trade(self.funded_account, Decimal("100"))
        self.funded_account.refresh_from_db()
        ctx = self._ctx()
        self.assertFalse(ctx["funded_payout_eligible"])
        self.assertIn("trading days", ctx["funded_payout_label"])

    def test_label_eligible(self):
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(3):
            _add_closed_trade(self.funded_account, Decimal("30"), days_ago=i)
        self.funded_account.refresh_from_db()
        self.assertIn("Eligible", self._ctx()["funded_payout_label"])

    def test_not_eligible_when_account_suspended(self):
        # Profit and days ok, but account suspended
        for i in range(3):
            _add_closed_trade(self.funded_account, Decimal("30"), days_ago=i)
        TradingAccount.objects.filter(pk=self.funded_account.pk).update(
            status=TradingAccount.STATUS_SUSPENDED
        )
        self.funded_account.refresh_from_db()
        ctx = self._ctx()
        self.assertFalse(ctx["funded_payout_eligible"])
        self.assertIn("suspended", ctx["funded_payout_label"])

    def test_label_suspended_when_account_suspended(self):
        TradingAccount.objects.filter(pk=self.funded_account.pk).update(
            status=TradingAccount.STATUS_SUSPENDED
        )
        self.funded_account.refresh_from_db()
        self.assertIn("suspended", self._ctx()["funded_payout_label"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Next payout date calculation
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedPayoutDate(TestCase):

    def setUp(self):
        self.user = make_user()
        self.funded_account, self.enrollment = _make_funded_account(
            self.user, payout_cycle_days=14
        )
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.funded_account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_next_payout_date_is_not_none(self):
        self.assertIsNotNone(self._ctx()["funded_next_payout_date"])

    def test_next_payout_date_is_14_days_after_funding_for_fresh_account(self):
        ctx = self._ctx()
        funded_at = self.enrollment.funded_at.date()
        expected = funded_at + timezone.timedelta(days=14)
        self.assertEqual(ctx["funded_next_payout_date"], expected)

    def test_days_until_payout_is_14_for_fresh_account(self):
        ctx = self._ctx()
        days = ctx["funded_days_until_payout"]
        # Fresh account: funded today, so 14 days until first window
        self.assertGreaterEqual(days, 13)
        self.assertLessEqual(days, 14)

    def test_next_payout_advances_after_first_cycle(self):
        # Simulate account funded 15 days ago (past first 14-day window)
        past_funded = timezone.now() - timezone.timedelta(days=15)
        self.enrollment.funded_at = past_funded
        self.enrollment.save(update_fields=["funded_at"])
        ctx = self._ctx()
        funded_at_date = past_funded.date()
        today = timezone.now().date()
        # After 15 days with 14-day cycle: 1 cycle completed, next = funded_at + 28 days
        expected = funded_at_date + timezone.timedelta(days=28)
        self.assertEqual(ctx["funded_next_payout_date"], expected)

    def test_days_until_payout_is_zero_or_positive(self):
        ctx = self._ctx()
        self.assertGreaterEqual(ctx["funded_days_until_payout"], 0)

    def test_next_payout_none_when_funded_at_not_set(self):
        # Manually clear funded_at to test fallback
        self.enrollment.funded_at = None
        self.enrollment.save(update_fields=["funded_at"])
        ctx = self._ctx()
        self.assertIsNone(ctx["funded_next_payout_date"])
        self.assertIsNone(ctx["funded_days_until_payout"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Template rendering spot-checks
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedSectionTemplate(TestCase):

    def setUp(self):
        self.user = make_user()
        self.funded_account, self.enrollment = _make_funded_account(
            self.user, profit_split_pct="80.00", min_payout_usd="50.00"
        )
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.funded_account.pk])

    def _html(self):
        return self.client.get(self.url).content.decode()

    def test_funded_card_present_in_html(self):
        self.assertIn("fundedCard", self._html())

    def test_profit_split_shown(self):
        self.assertIn("80", self._html())

    def test_min_payout_shown(self):
        self.assertIn("50", self._html())

    def test_cycle_profit_shown(self):
        # No profit yet — should show 0.00
        self.assertIn("0.00", self._html())

    def test_payout_label_shown(self):
        html = self._html()
        # Should show "Minimum" or "Eligible" or "suspended" label
        self.assertTrue(
            "Minimum" in html or "Eligible" in html or "suspended" in html,
            "Payout label not found in HTML",
        )

    def test_trading_days_row_present(self):
        self.assertIn("Trading Days", self._html())

    def test_next_window_row_present_when_funded_at_set(self):
        self.assertIn("Next Window", self._html())

    def test_no_funded_card_for_challenge_account(self):
        challenge_account = make_account(
            user=self.user, account_type="CHALLENGE", tier="10K"
        )
        url = reverse("simulator:dashboard_account", args=[challenge_account.pk])
        html = self.client.get(url).content.decode()
        self.assertNotIn("fundedCard", html)

    def test_eligible_badge_shown_when_all_conditions_met(self):
        # Backdate funded_at so trades at days_ago > 0 pass the closed_at__gte filter
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(5):
            _add_closed_trade(self.funded_account, Decimal("20"), days_ago=i)
        self.funded_account.refresh_from_db()
        self.assertIn("Eligible", self._html())

    # ── J.2: Funded payout request button UI ─────────────────────────────────

    def test_payout_form_absent_when_not_eligible(self):
        # No trades, no profit — not eligible — form must not appear
        html = self._html()
        self.assertNotIn("fdPayoutForm", html)

    def test_payout_form_present_when_eligible(self):
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(5):
            _add_closed_trade(self.funded_account, Decimal("20"), days_ago=i)
        self.funded_account.refresh_from_db()
        html = self._html()
        self.assertIn("fdPayoutForm", html)
        self.assertIn("fdPayoutBtn", html)
        self.assertIn("fdOtpInput", html)

    def test_payout_form_points_to_correct_url(self):
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(5):
            _add_closed_trade(self.funded_account, Decimal("20"), days_ago=i)
        self.funded_account.refresh_from_db()
        html = self._html()
        self.assertIn("/funded/payout/request/", html)

    def test_payout_button_label(self):
        self.enrollment.funded_at = timezone.now() - timezone.timedelta(days=10)
        self.enrollment.save(update_fields=["funded_at"])
        for i in range(5):
            _add_closed_trade(self.funded_account, Decimal("20"), days_ago=i)
        self.funded_account.refresh_from_db()
        self.assertIn("Request Payout", self._html())
