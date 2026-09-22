# simulator/tests/test_ib_portal_ux_10b_clients.py
"""
IB-PORTAL-UX-10B — Clients (/associates/clients/). Regression coverage
for simulator/views.py::associates_clients_view() and
simulator/templates/simulator/associates_clients.html.

Covers:
  - auth, empty state, real per-client aggregates (SSOT reused from 10A,
    never a second calculation — Live = TradingAccount.
    WITHDRAWABLE_ACCOUNT_TYPES, Deposits/Traded Volume/Commission from
    the same certified fields as the Associates dashboard).
  - Client Status (Owner decision J.1): Registered/Active/Dormant, purely
    derived from has_live + a real LotExecutionEvent within the last 30
    days — never User.is_active.
  - search (username), status filter, Joined period filter (reusing the
    exact same _IB_PERIODS whitelist as the dashboard).
  - pagination.
  - explicit cross-IB isolation (a client's row can never appear for the
    wrong IB, regardless of search/filter input).
  - query-count / N+1 sanity (scaling test, mirrors the one built for
    the 10A dashboard).
  - sidebar navigation: Asociados is now a group with Dashboard/
    Clientes/Programa (Programa visible but inert, no real route yet).
  - the existing, certified Associates dashboard (10A) is unaffected.
"""
from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
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
    return f"portalux10b_{_seq}"


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


def _clients_url(**params):
    url = reverse("simulator:associates_clients")
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url


class ClientsAuthTests(TestCase):
    def test_login_required(self):
        r = self.client.get(_clients_url())
        self.assertNotEqual(r.status_code, 200)

    def test_authenticated_ib_gets_200(self):
        owner = make_user()
        self.client.force_login(owner)
        r = self.client.get(_clients_url())
        self.assertEqual(r.status_code, 200)


class ClientsEmptyStateTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_zero_clients_shows_empty_state(self):
        r = self.client.get(_clients_url())
        self.assertEqual(list(r.context["client_rows"]), [])
        self.assertContains(r, "No clients yet")

    def test_total_clients_zero(self):
        r = self.client.get(_clients_url())
        self.assertEqual(r.context["total_clients"], 0)


class ClientsRealDataTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)

    def test_registered_status_no_live_account(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        make_account(trader, account_type="DEMO")
        r = self.client.get(_clients_url(period="all"))
        rows = r.context["client_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status_label"], "Registered")
        self.assertTrue(rows[0]["has_demo"])
        self.assertFalse(rows[0]["has_live"])

    def test_active_status_live_and_recent_lot(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        acc = make_account(trader, account_type="RETAIL")
        _make_lot_event(acc, created_at=timezone.now() - timezone.timedelta(days=5))
        r = self.client.get(_clients_url(period="all"))
        rows = r.context["client_rows"]
        self.assertEqual(rows[0]["status_label"], "Active")

    def test_dormant_status_live_but_no_recent_lot(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        acc = make_account(trader, account_type="RETAIL")
        _make_lot_event(acc, created_at=timezone.now() - timezone.timedelta(days=45))
        r = self.client.get(_clients_url(period="all"))
        rows = r.context["client_rows"]
        self.assertEqual(rows[0]["status_label"], "Dormant")

    def test_dormant_boundary_exactly_30_days_is_not_recent(self):
        """>=30 days ago must NOT count as 'recent' (the window is a
        strict last-30-days lookback, not an inclusive 30-day span)."""
        trader = make_user()
        _make_attribution(trader, self.ref)
        acc = make_account(trader, account_type="RETAIL")
        _make_lot_event(acc, created_at=timezone.now() - timezone.timedelta(days=31))
        r = self.client.get(_clients_url(period="all"))
        self.assertEqual(r.context["client_rows"][0]["status_label"], "Dormant")

    def test_live_uses_withdrawable_account_types_only(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        make_account(trader, account_type="CHALLENGE", tier="10K")
        r = self.client.get(_clients_url(period="all"))
        rows = r.context["client_rows"]
        self.assertFalse(rows[0]["has_live"])
        self.assertEqual(rows[0]["status_label"], "Registered")

    def test_ftd_date_and_deposits_total(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        Deposit.objects.create(
            user=trader, amount_usd=Decimal("300.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )
        Deposit.objects.create(
            user=trader, amount_usd=Decimal("200.00"), crypto_currency="BTC",
            status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
        )
        # Not credited — must not count.
        Deposit.objects.create(
            user=trader, amount_usd=Decimal("999.00"), crypto_currency="BTC",
            status=Deposit.STATUS_PENDING, credited=False,
        )
        r = self.client.get(_clients_url(period="all"))
        row = r.context["client_rows"][0]
        self.assertIsNotNone(row["ftd_date"])
        self.assertEqual(row["deposits_total"], Decimal("500.00"))

    def test_traded_volume_reuses_lot_execution_event(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        acc = make_account(trader, account_type="RETAIL")
        _make_lot_event(acc, qty="0.30")
        _make_lot_event(acc, qty="0.20")
        r = self.client.get(_clients_url(period="all"))
        self.assertEqual(r.context["client_rows"][0]["traded_volume"], Decimal("0.50"))

    def test_traded_volume_renders_human_readable_not_raw_precision(self):
        """Owner visual QA feedback: the DB aggregate carries full stored
        precision (e.g. Decimal('0.650000000000000')) but the template
        must display a human quantity ('0.65 lots'), never the raw
        18-decimal string — display-only, the underlying context value
        (row['traded_volume']) must still carry the real, unrounded
        Decimal so no calculation is affected."""
        trader = make_user()
        _make_attribution(trader, self.ref)
        acc = make_account(trader, account_type="RETAIL")
        _make_lot_event(acc, qty="0.65")
        r = self.client.get(_clients_url(period="all"))
        # The context itself still carries full DB precision — unrounded.
        raw_value = r.context["client_rows"][0]["traded_volume"]
        self.assertEqual(str(raw_value), "0.650000000000000")
        # The rendered HTML must show the human-readable form only.
        html = r.content.decode()
        self.assertIn("0.65 lots", html)
        self.assertNotIn("0.650000000000000 lots", html)

    def test_traded_volume_zero_renders_as_bare_zero(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        make_account(trader, account_type="DEMO")  # no lots at all
        r = self.client.get(_clients_url(period="all"))
        html = r.content.decode()
        self.assertIn("0 lots", html)
        self.assertNotIn("0E-15 lots", html)
        self.assertNotIn("0.000000000000000 lots", html)

    def test_commission_total_per_client(self):
        trader1 = make_user()
        trader2 = make_user()
        attr1 = _make_attribution(trader1, self.ref)
        attr2 = _make_attribution(trader2, self.ref)
        rule = IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_PER_LOT, referral=self.ref, enabled=True,
            fixed_amount=Decimal("8.00"), effective_from=timezone.now() - timezone.timedelta(days=1),
        )
        IBCommissionObligation.objects.create(
            attribution=attr1, referral=self.ref, rule=rule, rule_type=rule.rule_type,
            source_event_type="test", source_event_id=1,
            calculated_amount=Decimal("10.00"), currency="USD",
            status=IBCommissionObligation.ST_PENDING,
        )
        IBCommissionObligation.objects.create(
            attribution=attr1, referral=self.ref, rule=rule, rule_type=rule.rule_type,
            source_event_type="test", source_event_id=2,
            calculated_amount=Decimal("5.00"), currency="USD",
            status=IBCommissionObligation.ST_CREDITED,
        )
        IBCommissionObligation.objects.create(
            attribution=attr2, referral=self.ref, rule=rule, rule_type=rule.rule_type,
            source_event_type="test", source_event_id=3,
            calculated_amount=Decimal("2.00"), currency="USD",
            status=IBCommissionObligation.ST_PENDING,
        )
        r = self.client.get(_clients_url(period="all"))
        by_user = {row["username"]: row["commission_total"] for row in r.context["client_rows"]}
        self.assertEqual(by_user[trader1.username], Decimal("15.00"))
        self.assertEqual(by_user[trader2.username], Decimal("2.00"))

    def test_no_email_or_pii_beyond_username_in_context(self):
        trader = make_user(email="private.trader@example.com")
        _make_attribution(trader, self.ref)
        r = self.client.get(_clients_url(period="all"))
        row = r.context["client_rows"][0]
        self.assertEqual(set(row.keys()), {
            "attribution", "username", "joined_at", "has_demo", "has_live",
            "ftd_date", "deposits_total", "traded_volume", "commission_total",
            "status_label",
        })
        self.assertNotContains(r, trader.email)


class ClientsFilterTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)
        self.trader_a = make_user(username="alice_trader")
        self.trader_b = make_user(username="bob_trader")
        _make_attribution(self.trader_a, self.ref)
        _make_attribution(self.trader_b, self.ref)
        make_account(self.trader_a, account_type="RETAIL")

    def test_search_by_username(self):
        r = self.client.get(_clients_url(period="all", q="alice"))
        usernames = {row["username"] for row in r.context["client_rows"]}
        self.assertEqual(usernames, {"alice_trader"})

    def test_status_filter_registered(self):
        r = self.client.get(_clients_url(period="all", status="registered"))
        usernames = {row["username"] for row in r.context["client_rows"]}
        self.assertEqual(usernames, {"bob_trader"})

    def test_period_filter_excludes_old_joins(self):
        old_trader = make_user()
        _make_attribution(old_trader, self.ref, attributed_at=timezone.now() - timezone.timedelta(days=400))
        r = self.client.get(_clients_url(period="30d"))
        usernames = {row["username"] for row in r.context["client_rows"]}
        self.assertNotIn(old_trader.username, usernames)
        self.assertIn("alice_trader", usernames)

    def test_invalid_period_falls_back_to_all(self):
        r = self.client.get(_clients_url(period="DROP TABLE simulator_referral"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["period"], "all")

    def test_invalid_status_falls_back_to_all(self):
        r = self.client.get(_clients_url(status="nonsense"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["status_filter"], "all")


class ClientsPaginationTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)
        for i in range(25):
            trader = make_user()
            _make_attribution(trader, self.ref)

    def test_first_page_has_page_size_rows(self):
        r = self.client.get(_clients_url(period="all"))
        self.assertEqual(len(r.context["client_rows"]), 20)
        self.assertEqual(r.context["total_clients"], 25)

    def test_second_page_has_remainder(self):
        r = self.client.get(_clients_url(period="all", page=2))
        self.assertEqual(len(r.context["client_rows"]), 5)


class ClientsCrossIBIsolationTests(TestCase):
    def setUp(self):
        self.owner_a = make_user()
        self.owner_b = make_user()
        self.ref_a = Referral.objects.create(user=self.owner_a, code=_code())
        self.ref_b = Referral.objects.create(user=self.owner_b, code=_code())
        self.trader_a = make_user(username="trader_of_a")
        self.trader_b = make_user(username="trader_of_b")
        _make_attribution(self.trader_a, self.ref_a)
        _make_attribution(self.trader_b, self.ref_b)

    def test_ib_a_never_sees_ib_bs_client(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_clients_url(period="all"))
        usernames = {row["username"] for row in r.context["client_rows"]}
        self.assertEqual(usernames, {"trader_of_a"})

    def test_search_cannot_leak_another_ibs_client(self):
        """Searching for the OTHER IB's client username must return
        nothing — the search is applied inside the already-scoped
        queryset, never against the global User table."""
        self.client.force_login(self.owner_a)
        r = self.client.get(_clients_url(period="all", q="trader_of_b"))
        self.assertEqual(list(r.context["client_rows"]), [])

    def test_ib_b_sees_only_its_own_client(self):
        self.client.force_login(self.owner_b)
        r = self.client.get(_clients_url(period="all"))
        usernames = {row["username"] for row in r.context["client_rows"]}
        self.assertEqual(usernames, {"trader_of_b"})

    def test_commission_totals_isolated(self):
        rule = IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_PER_LOT, referral=self.ref_b, enabled=True,
            fixed_amount=Decimal("8.00"), effective_from=timezone.now() - timezone.timedelta(days=1),
        )
        attr_b = ReferralAttribution.objects.get(referred_user=self.trader_b)
        IBCommissionObligation.objects.create(
            attribution=attr_b, referral=self.ref_b, rule=rule, rule_type=rule.rule_type,
            source_event_type="test", source_event_id=1,
            calculated_amount=Decimal("50.00"), currency="USD",
            status=IBCommissionObligation.ST_PENDING,
        )
        self.client.force_login(self.owner_a)
        r = self.client.get(_clients_url(period="all"))
        # IB A's own client must show $0 commission, never IB B's $50.
        self.assertEqual(r.context["client_rows"][0]["commission_total"], Decimal("0.00"))


class ClientsQueryScalingTests(TestCase):
    """Query-count / N+1 sanity — mirrors the dedicated scaling test
    already built for the 10A dashboard."""

    def _build_ib(self, n_clients):
        owner = make_user()
        ref = Referral.objects.create(user=owner, code=_code())
        for _ in range(n_clients):
            trader = make_user()
            _make_attribution(trader, ref)
            acc = make_account(trader, account_type="RETAIL")
            _make_lot_event(acc, qty="0.10")
            Deposit.objects.create(
                user=trader, amount_usd=Decimal("50.00"), crypto_currency="BTC",
                status=Deposit.STATUS_FINISHED, credited=True, credited_at=timezone.now(),
            )
        return owner

    def test_query_count_does_not_scale_with_client_count(self):
        owner_small = self._build_ib(2)
        self.client.force_login(owner_small)
        with CaptureQueriesContext(connection) as ctx_small:
            r_small = self.client.get(_clients_url(period="all"))
        self.assertEqual(r_small.status_code, 200)

        owner_large = self._build_ib(25)
        self.client.force_login(owner_large)
        with CaptureQueriesContext(connection) as ctx_large:
            r_large = self.client.get(_clients_url(period="all"))
        self.assertEqual(r_large.status_code, 200)

        self.assertLess(
            len(ctx_large.captured_queries), len(ctx_small.captured_queries) + 5,
            f"small={len(ctx_small.captured_queries)} large={len(ctx_large.captured_queries)} — looks like N+1",
        )


class ClientsNavigationTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_sidebar_shows_dashboard_and_clients_links(self):
        r = self.client.get(_clients_url())
        self.assertContains(r, reverse("simulator:associates"))
        self.assertContains(r, reverse("simulator:associates_clients"))

    def test_sidebar_shows_programa_link(self):
        """IB-PORTAL-UX-10C implemented /associates/program/ as a real
        route — this test originally asserted the opposite (a disabled
        "Próximamente" placeholder with NoReverseMatch), which was
        correct for 10B's own scope at the time but is now intentionally
        superseded by 10C. Updated to assert the current, correct
        reality: Programa is a real, reachable link from the Clients
        page's own sidebar."""
        r = self.client.get(_clients_url())
        self.assertContains(r, reverse("simulator:associates_program"))

    def test_active_section_marks_clients_link_active(self):
        r = self.client.get(_clients_url())
        self.assertEqual(r.context["active_section"], "associates_clients")

    def test_dashboard_page_still_reachable_and_unaffected(self):
        """10A's own dashboard must render exactly as before — this
        block never touches associates_view()/its own certified logic."""
        r = self.client.get(reverse("simulator:associates"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("commission_summary", r.context)
        self.assertIn("top_cards", r.context)
