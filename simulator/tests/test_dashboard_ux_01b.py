# simulator/tests/test_dashboard_ux_01b.py
"""
DASHBOARD-UX-01B — Customer/Trader Main Dashboard (home_view) regression
coverage. Visual Design Lock implementation: real-data KPI cards, period
selector, Performance Overview (equity curve), Challenge/Account Progress,
financial cards (Deposits/Withdrawals/Available funds), performance
metrics (win rate), and Top Instruments — all backed by real queries on
the authenticated user's own data, reusing certified sources
(pnl_engine.calculate_required_margin() for margin, AccountEquitySnapshot.
equity — never .margin_used/.free_margin, per the DASHBOARD-UX-01A.1
deferred finding on snapshots.py — for the Performance Overview series).

No new model, no migration, no protected file touched. Zero fabricated
numbers: every widget either shows a real aggregate or an explicit empty
state.
"""
from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from simulator import pnl_engine
from simulator.models import (
    AccountEquitySnapshot, Deposit, Trade, WithdrawalRequest,
)
from simulator.tests.factories import make_account, make_position, make_user


class HomeViewAuthAndOwnershipTests(TestCase):
    """Items 1-5, 14-15, 25: login-required, ownership, account selection."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.other_user = make_user()
        self.other_account = make_account(self.other_user, account_type="RETAIL", balance=Decimal("5000"))

    def test_login_required(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertNotEqual(r.status_code, 200)

    def test_authenticated_user_gets_200(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)

    def test_correct_account_ownership(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["account"].pk, self.account.pk)
        self.assertEqual(r.context["account"].user_id, self.user.id)

    def test_cross_user_account_id_in_session_ignored_safely(self):
        """A session account_id pointing at another user's account must
        never be honored — _resolve_account() re-filters by user=request.user."""
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.other_account.id
        session.save()
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.context["account"].pk, self.other_account.pk)
        self.assertEqual(r.context["account"].user_id, self.user.id)

    def test_switch_account_denies_other_users_account(self):
        self.client.force_login(self.user)
        r = self.client.get(
            reverse("simulator:switch_account", args=[self.other_account.id]) + "?next=/home/"
        )
        session = self.client.session
        self.assertNotEqual(session.get("account_id"), self.other_account.id)

    def test_no_account_state_redirects(self):
        user_no_account = make_user()
        self.client.force_login(user_no_account)
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("simulator:accounts"), r.url)

    def test_multiple_accounts_all_listed(self):
        second = make_account(self.user, account_type="DEMO", balance=Decimal("1000"))
        self.client.force_login(self.user)
        r = self.client.get(reverse("simulator:home"))
        ids = {a.id for a in r.context["all_accounts"]}
        self.assertEqual(ids, {self.account.id, second.id})

    def test_selected_account_respected_over_most_recent(self):
        second = make_account(self.user, account_type="DEMO", balance=Decimal("1000"))
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id  # not the most-recently-created
        session.save()
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["account"].pk, self.account.pk)
        self.assertNotEqual(r.context["account"].pk, second.pk)

    def test_dashboard_remains_login_protected(self):
        self.client.logout()
        r = self.client.get(reverse("simulator:home"))
        self.assertNotEqual(r.status_code, 200)


class HomeViewZeroDataStateTests(TestCase):
    """Item 6, 16: a brand-new account shows real zeros / explicit empty
    states — never fabricated positive numbers."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()

    def test_zero_positions_zero_margin(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["open_positions_count"], 0)
        self.assertEqual(r.context["margin_used"], 0.0)

    def test_zero_deposits_zero_withdrawals(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["total_deposited"], Decimal("0.00"))
        self.assertEqual(r.context["total_withdrawn"], Decimal("0.00"))

    def test_zero_performance_metrics_no_fabricated_win_rate(self):
        r = self.client.get(reverse("simulator:home"))
        pm = r.context["performance_metrics"]
        self.assertEqual(pm["total"], 0)
        self.assertIsNone(pm["win_rate"])
        self.assertIsNone(pm["avg_win"])
        self.assertIsNone(pm["avg_loss"])

    def test_zero_top_instruments(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(list(r.context["top_instruments"]), [])

    def test_empty_equity_curve(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["equity_curve"], [])
        self.assertContains(r, "No data yet for this period")

    def test_no_challenge_progress_for_retail_account(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertIsNone(r.context["challenge_progress"])
        self.assertContains(r, "Trading Activity")
        self.assertNotContains(r, "Challenge Progress")


class HomeViewRealDataTests(TestCase):
    """Items 7-13, 17-18: balance/equity/margin/positions/deposits/
    withdrawals/period all reflect real, correctly-scoped data."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.account.leverage = 100
        self.account.currency = "USD"
        self.account.equity = Decimal("10050.00")
        self.account.save(update_fields=["leverage", "currency", "equity"])
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()

    def test_balance_correct(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["account"].balance, Decimal("10000.00"))

    def test_equity_correct(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["account"].equity, Decimal("10050.00"))

    def test_margin_matches_certified_ssot(self):
        """Reuses the DASHBOARD-UX-01A.2-certified SSOT — no duplicated
        formula in this test's expected value."""
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        r = self.client.get(reverse("simulator:home"))
        expected, err = pnl_engine.calculate_required_margin(
            "EUR/USD", Decimal("1.17000"), Decimal("1.0"), 100, "USD",
        )
        self.assertIsNone(err)
        self.assertAlmostEqual(r.context["margin_used"], round(expected, 2), places=2)

    def test_open_positions_correct(self):
        make_position(self.account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.17000"))
        make_position(self.account, symbol="BTCUSD", side="SELL",
                      qty=Decimal("0.1"), avg_price=Decimal("82000.0"))
        r = self.client.get(reverse("simulator:home"))
        self.assertEqual(r.context["open_positions_count"], 2)

    def test_deposits_correct(self):
        Deposit.objects.create(
            user=self.user, amount_usd=Decimal("500.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )
        # Not credited — must NOT be counted.
        Deposit.objects.create(
            user=self.user, amount_usd=Decimal("999.00"), crypto_currency="BTC",
            status=Deposit.STATUS_PENDING, credited=False,
        )
        r = self.client.get(reverse("simulator:home") + "?period=all")
        self.assertEqual(r.context["total_deposited"], Decimal("500.00"))

    def test_withdrawals_correct(self):
        WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("200.00"), crypto_currency="BTC",
            wallet_address="addr1", status=WithdrawalRequest.STATUS_COMPLETED,
        )
        # Still pending — must NOT be counted as withdrawn.
        WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("77.00"), crypto_currency="BTC",
            wallet_address="addr2", status=WithdrawalRequest.STATUS_PENDING,
        )
        r = self.client.get(reverse("simulator:home") + "?period=all")
        self.assertEqual(r.context["total_withdrawn"], Decimal("200.00"))

    def test_period_validation_invalid_value_falls_back_safely(self):
        r = self.client.get(reverse("simulator:home") + "?period=DROP TABLE simulator_trade")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["period"], "30d")

    def test_period_validation_valid_values_accepted(self):
        for p in ("today", "7d", "30d", "month", "3m", "6m", "1y", "all"):
            r = self.client.get(reverse("simulator:home") + f"?period={p}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.context["period"], p)

    def test_cross_user_trade_data_not_included(self):
        other_user = make_user()
        other_account = make_account(other_user, account_type="RETAIL")
        Trade.objects.create(
            account=other_account, symbol="GBP/USD", trade_type="BUY",
            lot_size=Decimal("1.0"), entry_price=Decimal("1.3"), exit_price=Decimal("1.31"),
            profit_loss=Decimal("100.00"), opened_at=timezone.now(), closed_at=timezone.now(),
        )
        r = self.client.get(reverse("simulator:home") + "?period=all")
        self.assertEqual(r.context["performance_metrics"]["total"], 0)
        self.assertEqual(list(r.context["top_instruments"]), [])

    def test_top_instruments_isolated_to_this_account_only(self):
        Trade.objects.create(
            account=self.account, symbol="EUR/USD", trade_type="BUY",
            lot_size=Decimal("0.1"), entry_price=Decimal("1.1"), exit_price=Decimal("1.11"),
            profit_loss=Decimal("10.00"), opened_at=timezone.now(), closed_at=timezone.now(),
        )
        other_user = make_user()
        other_account = make_account(other_user, account_type="RETAIL")
        Trade.objects.create(
            account=other_account, symbol="GBP/USD", trade_type="BUY",
            lot_size=Decimal("1.0"), entry_price=Decimal("1.3"), exit_price=Decimal("1.31"),
            profit_loss=Decimal("100.00"), opened_at=timezone.now(), closed_at=timezone.now(),
        )
        r = self.client.get(reverse("simulator:home") + "?period=all")
        symbols = {row["symbol"] for row in r.context["top_instruments"]}
        self.assertEqual(symbols, {"EUR/USD"})


class HomeViewPerformanceMetricsTests(TestCase):
    """Win rate / wins / losses / avg win / avg loss over real closed
    trades — mirrors history_view()'s own win/loss split, no duplicated
    formula invented here."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()
        now = timezone.now()
        Trade.objects.create(
            account=self.account, symbol="EUR/USD", trade_type="BUY",
            lot_size=Decimal("0.1"), entry_price=Decimal("1.1"), exit_price=Decimal("1.12"),
            profit_loss=Decimal("20.00"), opened_at=now, closed_at=now,
        )
        Trade.objects.create(
            account=self.account, symbol="EUR/USD", trade_type="SELL",
            lot_size=Decimal("0.1"), entry_price=Decimal("1.1"), exit_price=Decimal("1.09"),
            profit_loss=Decimal("-10.00"), opened_at=now, closed_at=now,
        )
        Trade.objects.create(
            account=self.account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("0.01"), entry_price=Decimal("82000"), exit_price=Decimal("83000"),
            profit_loss=Decimal("10.00"), opened_at=now, closed_at=now,
        )

    def test_win_rate_matches_real_split(self):
        r = self.client.get(reverse("simulator:home") + "?period=all")
        pm = r.context["performance_metrics"]
        self.assertEqual(pm["total"], 3)
        self.assertEqual(pm["wins"], 2)
        self.assertEqual(pm["losses"], 1)
        self.assertAlmostEqual(pm["win_rate"], round(2 / 3 * 100, 1), places=1)

    def test_avg_win_avg_loss_correct(self):
        r = self.client.get(reverse("simulator:home") + "?period=all")
        pm = r.context["performance_metrics"]
        self.assertAlmostEqual(float(pm["avg_win"]), 15.0, places=2)  # (20+10)/2
        self.assertAlmostEqual(float(pm["avg_loss"]), -10.0, places=2)

    def test_top_instruments_ranked_by_trade_count(self):
        r = self.client.get(reverse("simulator:home") + "?period=all")
        top = list(r.context["top_instruments"])
        self.assertEqual(top[0]["symbol"], "EUR/USD")
        self.assertEqual(top[0]["trade_count"], 2)


class HomeViewChallengeProgressTests(TestCase):
    """Items 19-20: Challenge Progress renders only for CHALLENGE/FUNDED
    accounts with a real, persisted profit_target — a normal account never
    shows fabricated challenge data."""

    def test_challenge_account_with_target_shows_challenge_progress(self):
        user = make_user()
        account = make_account(user, account_type="CHALLENGE", tier="10K", balance=Decimal("10500"))
        account.profit_target = Decimal("1000")
        account.equity = Decimal("10500")
        account.drawdown = Decimal("100")
        account.max_drawdown = Decimal("1200")
        account.save()
        self.client.force_login(user)
        session = self.client.session
        session["account_id"] = account.id
        session.save()

        r = self.client.get(reverse("simulator:home"))
        self.assertIsNotNone(r.context["challenge_progress"])
        self.assertEqual(r.context["challenge_progress"]["profit_target"], 1000.0)
        self.assertContains(r, "Challenge Progress")
        self.assertContains(r, "Challenge Goal")

    def test_challenge_account_without_target_shows_no_challenge_progress(self):
        user = make_user()
        account = make_account(user, account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        account.profit_target = None
        account.save()
        self.client.force_login(user)
        session = self.client.session
        session["account_id"] = account.id
        session.save()

        r = self.client.get(reverse("simulator:home"))
        self.assertIsNone(r.context["challenge_progress"])

    def test_progress_pct_clamped_and_uses_real_fields_only(self):
        user = make_user()
        account = make_account(user, account_type="FUNDED", tier="10K", balance=Decimal("12000"))
        account.profit_target = Decimal("1000")
        account.equity = Decimal("12000")  # profit = 2000, way over target
        account.save()
        self.client.force_login(user)
        session = self.client.session
        session["account_id"] = account.id
        session.save()

        r = self.client.get(reverse("simulator:home"))
        self.assertLessEqual(r.context["challenge_progress"]["progress_pct"], 100.0)


class HomeViewNavigationAndChromeTests(TestCase):
    """Items 21, 23-24: sidebar exposes only real, existing routes; the
    chart container renders safely in both data and empty-data states."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.id
        session.save()

    def test_required_navigation_routes_present(self):
        r = self.client.get(reverse("simulator:home"))
        html = r.content.decode()
        for route_name in (
            "simulator:home", "simulator:dashboard", "simulator:deposit",
            "simulator:withdraw", "simulator:associates", "simulator:support",
            "simulator:profile",
        ):
            self.assertIn(reverse(route_name), html)

    def test_associates_link_present_for_normal_user(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertContains(r, reverse("simulator:associates"))

    def test_chart_container_renders_with_data(self):
        now = timezone.now()
        for i in range(3):
            AccountEquitySnapshot.objects.create(
                account=self.account, taken_at=now - timezone.timedelta(minutes=i),
                balance=Decimal("10000"), equity=Decimal(str(10000 + i)),
                floating_pnl=Decimal("0"), margin_used=Decimal("0"),
                free_margin=Decimal("10000"), drawdown=Decimal("0"), open_positions=0,
            )
        r = self.client.get(reverse("simulator:home") + "?period=all")
        self.assertContains(r, 'id="perfOverviewChart"')
        self.assertNotContains(r, "No data yet for this period")

    def test_chart_empty_state_renders_safely_with_no_snapshots(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertContains(r, "No data yet for this period")
        self.assertNotContains(r, 'id="perfOverviewChart"')

    def test_no_staff_only_routes_exposed_to_normal_user(self):
        r = self.client.get(reverse("simulator:home"))
        self.assertNotContains(r, reverse("simulator:ops_panel"))


class HomeViewQueryBudgetTests(TestCase):
    """Item 22: soft query-count ceiling — catches a flagrant N+1
    regression without pinning to a brittle exact count."""

    def test_query_count_stays_within_reasonable_budget(self):
        user = make_user()
        account = make_account(user, account_type="RETAIL", balance=Decimal("10000"))
        for i in range(5):
            make_position(account, symbol="EUR/USD", side="BUY",
                          qty=Decimal("0.1"), avg_price=Decimal("1.1"))
        now = timezone.now()
        for i in range(10):
            Trade.objects.create(
                account=account, symbol="EUR/USD" if i % 2 else "BTCUSD", trade_type="BUY",
                lot_size=Decimal("0.1"), entry_price=Decimal("1.1"), exit_price=Decimal("1.11"),
                profit_loss=Decimal("5.00"), opened_at=now, closed_at=now,
            )
        self.client.force_login(user)
        session = self.client.session
        session["account_id"] = account.id
        session.save()

        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(reverse("simulator:home") + "?period=all")
        self.assertEqual(r.status_code, 200)
        self.assertLess(
            len(ctx.captured_queries), 40,
            f"home_view issued {len(ctx.captured_queries)} queries — investigate for N+1",
        )
