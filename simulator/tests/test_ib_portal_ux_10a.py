# simulator/tests/test_ib_portal_ux_10a.py
"""
IB-PORTAL-UX-10A — Associates Performance Dashboard (visual parity + real
IB data). Regression coverage for the new context added to
simulator/views.py::associates_view() and rendered in
simulator/templates/simulator/associates.html:

  - Top 4 cards (Registered/Demo/Live/First-Time Deposit) — cumulative
    totals + period-over-period growth, zero-denominator-guarded.
  - Acquisition funnel (all-time, Clicks-based, zero-denominator-guarded).
  - Conversion Rates donuts (same all-time basis as the funnel).
  - Performance Overview day-bucketed series (Registered/Demo/Live/
    Deposits) — NOT AccountEquitySnapshot (wrong domain, see
    DASHBOARD-UX-01A.1's own deferred finding on that model).
  - Financial/Activity cards (Deposits/Withdrawals/Traded Volume),
    period-scoped, attributed-traders-only.
  - Commission Overview (Generated/Pending/On Hold/Credited/Current
    Rate) — a visual re-presentation of the EXISTING, unmodified
    ib_admin_ops.ib_commission_summary()/ib_effective_rate() SSOT, not a
    new calculation.

Every new aggregate is scoped via the ReferralAttribution.referred_user
OneToOneField join (user__referral_attribution__referral=ref /
account__user__referral_attribution__referral=ref) — structurally
impossible for one IB's query to return another IB's traders. This
suite includes explicit cross-IB isolation tests proving that.

Pre-existing IB-PORTAL-08B functionality (referral link/code/copy
button, commission summary cards' underlying data, obligation history
table) must remain intact — covered here at the context-contract level;
the full commission-engine regression (rate changes, holds, freeze,
IDOR on the admin side) already lives in test_ib_portal_08b.py and is
run alongside this suite, not duplicated here.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import (
    Deposit, IBCommissionObligation, IBCommissionRule, LotExecutionEvent,
    Referral, ReferralAttribution, TradingAccount, WithdrawalRequest,
)
from simulator.tests.factories import make_account, make_user

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"portalux10a_{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral, attributed_at=None):
    attr = ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )
    if attributed_at is not None:
        ReferralAttribution.objects.filter(pk=attr.pk).update(attributed_at=attributed_at)
        attr.refresh_from_db()
    return attr


def _make_lot_event(account, qty="0.50", created_at=None):
    ev = LotExecutionEvent.objects.create(
        account=account, position=None, symbol="EUR/USD", side="BUY",
        qty=Decimal(qty), execution_price=Decimal("1.10000"),
        merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
    )
    if created_at is not None:
        LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
        ev.refresh_from_db()
    return ev


def _associates_url(**params):
    url = reverse("simulator:associates")
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url


class PortalUX10AAuthTests(TestCase):
    def test_login_required(self):
        r = self.client.get(_associates_url())
        self.assertNotEqual(r.status_code, 200)

    def test_authenticated_ib_gets_200(self):
        owner = make_user()
        self.client.force_login(owner)
        r = self.client.get(_associates_url())
        self.assertEqual(r.status_code, 200)


class PortalUX10AZeroReferralsTests(TestCase):
    """IB with zero referrals — every widget shows a real zero / empty
    state, never a fabricated number."""

    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_zero_top_cards(self):
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["top_cards"]["registered"]["value"], 0)
        self.assertEqual(r.context["top_cards"]["demo"]["value"], 0)
        self.assertEqual(r.context["top_cards"]["live"]["value"], 0)
        self.assertEqual(r.context["top_cards"]["ftd"]["value"], 0)

    def test_zero_denominator_deltas_and_conversions_are_none(self):
        r = self.client.get(_associates_url())
        for key in ("registered", "demo", "live", "ftd"):
            self.assertIsNone(r.context["top_cards"][key]["delta"])
        cr = r.context["conversion_rates"]
        self.assertIsNone(cr["registration"]["pct"])  # 0 clicks -> guarded
        self.assertIsNone(cr["live"]["pct"])
        self.assertIsNone(cr["ftd"]["pct"])

    def test_zero_financial_cards(self):
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["ib_deposits_total"], Decimal("0.00"))
        self.assertEqual(r.context["ib_withdrawals_total"], Decimal("0.00"))
        self.assertEqual(r.context["ib_traded_volume"], Decimal("0"))

    def test_no_performance_data_renders_empty_state(self):
        r = self.client.get(_associates_url())
        self.assertFalse(r.context["has_performance_data"])
        self.assertContains(r, "No data yet for this period")

    def test_monthly_goal_and_channels_empty_states(self):
        r = self.client.get(_associates_url())
        self.assertContains(r, "No goal configured")
        self.assertContains(r, "No channel attribution data available")

    def test_referral_link_still_present(self):
        r = self.client.get(_associates_url())
        self.assertContains(r, r.context["referral"].code)
        self.assertContains(r, "id=\"refUrl\"")


class PortalUX10ARealDataTests(TestCase):
    """IB with real attributed traders — registered/demo/live/FTD counts,
    funnel, conversions, financial cards all reflect real DB state."""

    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code(), clicks=10)
        self.client.force_login(self.owner)

        self.trader1 = make_user()
        self.trader2 = make_user()
        self.trader3 = make_user()
        _make_attribution(self.trader1, self.ref)
        _make_attribution(self.trader2, self.ref)
        _make_attribution(self.trader3, self.ref)

        # trader1: demo account only
        make_account(self.trader1, account_type="DEMO")
        # trader2: live (RETAIL) account + a credited deposit (FTD)
        self.acc2 = make_account(self.trader2, account_type="RETAIL")
        Deposit.objects.create(
            user=self.trader2, amount_usd=Decimal("500.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )
        # trader3: CHALLENGE account (not "live" per WITHDRAWABLE_ACCOUNT_TYPES)
        make_account(self.trader3, account_type="CHALLENGE", tier="10K")

    def test_registered_count(self):
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["top_cards"]["registered"]["value"], 3)

    def test_demo_count(self):
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["top_cards"]["demo"]["value"], 1)

    def test_live_count_uses_withdrawable_account_types(self):
        """CHALLENGE is not 'live' per the Owner's authorized decision
        (Live = TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES)."""
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["top_cards"]["live"]["value"], 1)
        self.assertIn(self.acc2.account_type, TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES)

    def test_ftd_count(self):
        r = self.client.get(_associates_url())
        self.assertEqual(r.context["top_cards"]["ftd"]["value"], 1)

    def test_funnel_stage_values_and_percentages(self):
        r = self.client.get(_associates_url())
        stages = {s["label"]: s for s in r.context["funnel_stages"]}
        self.assertEqual(stages["Clicks"]["value"], 10)
        self.assertEqual(stages["Registered"]["value"], 3)
        self.assertEqual(stages["Demo Accounts"]["value"], 1)
        self.assertEqual(stages["Live Accounts"]["value"], 1)
        self.assertEqual(stages["First-Time Deposit"]["value"], 1)
        self.assertAlmostEqual(stages["Registered"]["pct"], 30.0, places=1)  # 3/10

    def test_conversion_rates_real_ratios(self):
        r = self.client.get(_associates_url())
        cr = r.context["conversion_rates"]
        self.assertAlmostEqual(cr["registration"]["pct"], 30.0, places=1)  # 3/10 clicks
        self.assertAlmostEqual(cr["live"]["pct"], round(1 / 3 * 100, 1), places=1)  # 1/3 registered
        self.assertAlmostEqual(cr["ftd"]["pct"], 100.0, places=1)  # 1/1 live

    def test_deposit_aggregation(self):
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["ib_deposits_total"], Decimal("500.00"))

    def test_withdrawal_aggregation(self):
        WithdrawalRequest.objects.create(
            user=self.trader2, amount_usd=Decimal("120.00"), crypto_currency="BTC",
            wallet_address="addr1", status=WithdrawalRequest.STATUS_COMPLETED,
        )
        WithdrawalRequest.objects.create(
            user=self.trader2, amount_usd=Decimal("50.00"), crypto_currency="BTC",
            wallet_address="addr2", status=WithdrawalRequest.STATUS_PENDING,
        )
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["ib_withdrawals_total"], Decimal("120.00"))

    def test_traded_volume_aggregation_reuses_lot_execution_event(self):
        _make_lot_event(self.acc2, qty="0.30")
        _make_lot_event(self.acc2, qty="0.20")
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["ib_traded_volume"], Decimal("0.50"))
        # Also reflected in the existing, unmodified lifetime lot_totals SSOT.
        self.assertEqual(r.context["lot_totals"]["lifetime"], Decimal("0.50"))

    def test_period_filtering_excludes_old_activity_from_daily_series(self):
        """top_cards[...].value is always the cumulative total (by
        design — matches the reference's running-total card semantic);
        period filtering is proven instead on the Performance Overview
        daily series, which IS period-scoped (_ib_daily_series filters
        on date_field__gte=period_start)."""
        import json as _json
        old_trader = make_user()
        _make_attribution(old_trader, self.ref, attributed_at=timezone.now() - timezone.timedelta(days=400))

        r_30d = self.client.get(_associates_url(period="30d"))
        payload_30d = _json.loads(r_30d.context["performance_series_json"])
        total_30d = sum(p["value"] for p in payload_30d["registered"])
        self.assertEqual(total_30d, 3)  # excludes the 400-day-old row

        r_all = self.client.get(_associates_url(period="all"))
        payload_all = _json.loads(r_all.context["performance_series_json"])
        total_all = sum(p["value"] for p in payload_all["registered"])
        self.assertEqual(total_all, 4)  # includes it

        # The cumulative top-card value is unaffected by the period filter.
        self.assertEqual(r_30d.context["top_cards"]["registered"]["value"], 4)

    def test_previous_period_comparison_present_when_baseline_exists(self):
        older_trader = make_user()
        _make_attribution(older_trader, self.ref, attributed_at=timezone.now() - timezone.timedelta(days=40))
        r = self.client.get(_associates_url(period="30d"))
        # 4 total registered; 1 existed before the 30d window (the baseline).
        self.assertEqual(r.context["top_cards"]["registered"]["value"], 4)
        self.assertIsNotNone(r.context["top_cards"]["registered"]["delta"])

    def test_period_validation_invalid_value_falls_back_safely(self):
        r = self.client.get(_associates_url(period="DROP TABLE simulator_referral"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["period"], "month")

    def test_period_validation_valid_values_accepted(self):
        for p in ("7d", "30d", "month", "3m", "6m", "1y", "all"):
            r = self.client.get(_associates_url(period=p))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.context["period"], p)

    def test_commission_overview_still_uses_existing_summary(self):
        r = self.client.get(_associates_url())
        self.assertIn("commission_summary", r.context)
        self.assertContains(r, "Commission Overview")
        self.assertContains(r, "On Hold")

    def test_existing_history_table_functionality_preserved(self):
        r = self.client.get(_associates_url())
        self.assertIn("history_page", r.context)
        self.assertIn("history_rows", r.context)


class PortalUX10ACrossIBIsolationTests(TestCase):
    """The named, explicit requirement: IB A must never see IB B's
    traders, deposits, withdrawals, lots, or funnel/conversion figures."""

    def setUp(self):
        self.owner_a = make_user()
        self.owner_b = make_user()
        self.ref_a = Referral.objects.create(user=self.owner_a, code=_code(), clicks=5)
        self.ref_b = Referral.objects.create(user=self.owner_b, code=_code(), clicks=50)

        self.trader_a = make_user()
        self.trader_b = make_user()
        _make_attribution(self.trader_a, self.ref_a)
        _make_attribution(self.trader_b, self.ref_b)

        self.acc_a = make_account(self.trader_a, account_type="RETAIL")
        self.acc_b = make_account(self.trader_b, account_type="RETAIL")
        _make_lot_event(self.acc_a, qty="1.00")
        _make_lot_event(self.acc_b, qty="9.00")

        Deposit.objects.create(
            user=self.trader_a, amount_usd=Decimal("100.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )
        Deposit.objects.create(
            user=self.trader_b, amount_usd=Decimal("9000.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )

    def test_registered_count_isolated(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["top_cards"]["registered"]["value"], 1)

    def test_live_count_isolated(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["top_cards"]["live"]["value"], 1)

    def test_traded_volume_isolated(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["ib_traded_volume"], Decimal("1.00"))
        self.assertNotEqual(r.context["ib_traded_volume"], Decimal("9.00"))

    def test_deposits_isolated(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["ib_deposits_total"], Decimal("100.00"))
        self.assertNotEqual(r.context["ib_deposits_total"], Decimal("9000.00"))

    def test_funnel_clicks_isolated(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url())
        stages = {s["label"]: s for s in r.context["funnel_stages"]}
        self.assertEqual(stages["Clicks"]["value"], 5)

    def test_ib_b_sees_its_own_larger_numbers_not_ib_as(self):
        self.client.force_login(self.owner_b)
        r = self.client.get(_associates_url(period="all"))
        self.assertEqual(r.context["top_cards"]["registered"]["value"], 1)
        self.assertEqual(r.context["ib_traded_volume"], Decimal("9.00"))
        self.assertEqual(r.context["ib_deposits_total"], Decimal("9000.00"))

    def test_no_unrelated_users_data_leaks_via_performance_series(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_associates_url(period="all"))
        # IB A's deposit series total must equal only trader_a's deposit,
        # never trader_b's. performance_series isn't directly in context
        # (only its JSON is) — re-derive from the payload actually
        # rendered to the page.
        import json as _json
        payload = _json.loads(r.context["performance_series_json"])
        total = sum(p["value"] for p in payload["deposits"])
        self.assertEqual(total, 100.0)


class PortalUX10AQueryScalingTests(TestCase):
    """Item 22 — query-count sanity. Proves the new IB-PORTAL-UX-10A
    aggregates (top-4 cards, funnel, conversions, performance series,
    financial cards) are O(1) per request — the query count must NOT
    grow with the number of attributed traders/lot events, unlike a
    per-row Python loop would."""

    def _build_ib(self, n_traders, n_lots_per_trader=2):
        owner = make_user()
        ref = Referral.objects.create(user=owner, code=_code(), clicks=n_traders * 3)
        for _ in range(n_traders):
            trader = make_user()
            _make_attribution(trader, ref)
            acc = make_account(trader, account_type="RETAIL")
            for _ in range(n_lots_per_trader):
                _make_lot_event(acc, qty="0.10")
            Deposit.objects.create(
                user=trader, amount_usd=Decimal("50.00"), crypto_currency="BTC",
                status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
            )
        return owner

    def test_query_count_does_not_scale_with_attributed_trader_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        owner_small = self._build_ib(n_traders=2)
        self.client.force_login(owner_small)
        with CaptureQueriesContext(connection) as ctx_small:
            r_small = self.client.get(_associates_url(period="all"))
        self.assertEqual(r_small.status_code, 200)

        owner_large = self._build_ib(n_traders=25)
        self.client.force_login(owner_large)
        with CaptureQueriesContext(connection) as ctx_large:
            r_large = self.client.get(_associates_url(period="all"))
        self.assertEqual(r_large.status_code, 200)

        # A 12.5x increase in traders/lots/deposits must not translate
        # into materially more queries — a handful of extra queries is
        # tolerable (pagination/incidental), a linear scale-up is not.
        self.assertLess(
            len(ctx_large.captured_queries), len(ctx_small.captured_queries) + 5,
            f"small-IB={len(ctx_small.captured_queries)} queries, "
            f"large-IB={len(ctx_large.captured_queries)} queries — looks like N+1",
        )
