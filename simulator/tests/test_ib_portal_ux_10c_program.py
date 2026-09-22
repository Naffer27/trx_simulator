# simulator/tests/test_ib_portal_ux_10c_program.py
"""
IB-PORTAL-UX-10C — Affiliate Program (/associates/program/), read-only.
Regression coverage for simulator/views.py::associates_program_view() and
simulator/templates/simulator/associates_program.html.

Covers:
  - auth, IB without a pre-existing Referral (get_or_create path).
  - PER_LOT / Trading Commission Revenue Share / Spread Revenue Share,
    each global and IB-specific-override, via the real, unmodified
    ib_admin_ops.ib_effective_rate() — never a second resolver.
  - multiple simultaneous components (never collapsed into one rate).
  - effective_from / effective_until: a future rule never appears yet,
    an expired rule never appears once effective_until has passed.
  - absence of a rule renders "Not configured" — never a fabricated
    $0/0% standing in for "no rule".
  - CPA_BONUS is never shown as an active component, even when a real
    CPA_BONUS IBCommissionRule row exists for the IB (the generator/
    sweep gap is a backend fact, not a UI filter of convenience — the
    view's own compensation-type list simply never includes it).
  - explicit cross-IB isolation.
  - navigation: Dashboard/Clientes/Programa links + active state.
  - zero mutation surface (GET-only page, no economic write anywhere).
  - query-count sanity (bounded, not proportional to rule history size).
"""
from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from simulator.models import IBCommissionRule, Referral
from simulator.tests.factories import make_user

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"portalux10c_{_seq}"


def _make_rule(rule_type, referral=None, fixed_amount=None, percentage=None,
                effective_from=None, effective_until=None, enabled=True):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(days=5)),
        effective_until=effective_until,
    )


def _program_url():
    return reverse("simulator:associates_program")


class ProgramAuthTests(TestCase):
    def test_login_required(self):
        r = self.client.get(_program_url())
        self.assertNotEqual(r.status_code, 200)

    def test_authenticated_ib_gets_200(self):
        owner = make_user()
        self.client.force_login(owner)
        r = self.client.get(_program_url())
        self.assertEqual(r.status_code, 200)

    def test_ib_without_prior_referral_gets_one_created(self):
        """get_or_create path — mirrors associates_view()'s own behavior,
        never a second Referral for the same user. The page never
        displays the referral code itself (Owner Visual Review FIX-01 —
        that tool lives exclusively on /associates/), so this only
        checks the Referral row itself, not page content for the code."""
        owner = make_user()
        self.assertFalse(Referral.objects.filter(user=owner).exists())
        self.client.force_login(owner)
        r = self.client.get(_program_url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Referral.objects.filter(user=owner).count(), 1)
        self.assertEqual(r.context["referral"], Referral.objects.get(user=owner))


class ProgramNoRuleTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_no_rule_anywhere_shows_not_configured_never_fake_rate(self):
        r = self.client.get(_program_url())
        self.assertFalse(r.context["has_active_component"])
        for c in r.context["components"]:
            self.assertFalse(c["active"])
        self.assertContains(r, "No compensation rule is currently configured")
        # Never a fabricated $0/0% anywhere in the rendered page.
        self.assertNotContains(r, "$0.00")
        self.assertNotContains(r, ">0%<")


class ProgramComponentsTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)

    def test_per_lot_global_rule(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=None, fixed_amount=Decimal("8.00"))
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertTrue(comps[IBCommissionRule.RULE_PER_LOT]["active"])
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["value"], Decimal("8.00"))
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["source"], "global")
        self.assertContains(r, "Global")

    def test_per_lot_ib_specific_override(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=None, fixed_amount=Decimal("8.00"))
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("12.00"))
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        # The override, not the global rate, must be the one shown.
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["value"], Decimal("12.00"))
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["source"], "per-IB")
        self.assertContains(r, "IB-Specific")
        self.assertNotContains(r, "$8.00")

    def test_trading_commission_revenue_share(self):
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("15.000"))
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        c = comps[IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE]
        self.assertTrue(c["active"])
        self.assertEqual(c["value"], Decimal("15.000"))
        self.assertEqual(c["unit"], "percentage")

    def test_spread_revenue_share(self):
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=self.ref, percentage=Decimal("5.000"))
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertTrue(comps[IBCommissionRule.RULE_SPREAD_REVENUE_SHARE]["active"])
        self.assertEqual(comps[IBCommissionRule.RULE_SPREAD_REVENUE_SHARE]["value"], Decimal("5.000"))

    def test_multiple_simultaneous_components_never_collapsed(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("10.00"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("20.000"))
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=self.ref, percentage=Decimal("10.000"))
        r = self.client.get(_program_url())
        active = [c for c in r.context["components"] if c["active"]]
        self.assertEqual(len(active), 3)
        self.assertTrue(r.context["has_active_component"])
        # All three real values rendered distinctly — never one flat rate.
        # (Owner Visual Review FIX-02: percentages render human-readable,
        # trailing zeros stripped — "20%"/"10%", not "20.000%"/"10.000%".)
        self.assertContains(r, "$10.00")
        self.assertContains(r, "20%")
        self.assertContains(r, "10%")

    def test_effective_from_future_rule_not_shown_yet(self):
        _make_rule(
            IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("99.00"),
            effective_from=timezone.now() + timezone.timedelta(days=10),
        )
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertFalse(comps[IBCommissionRule.RULE_PER_LOT]["active"])
        self.assertNotContains(r, "$99.00")

    def test_expired_rule_not_shown(self):
        _make_rule(
            IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("77.00"),
            effective_from=timezone.now() - timezone.timedelta(days=30),
            effective_until=timezone.now() - timezone.timedelta(days=1),
        )
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertFalse(comps[IBCommissionRule.RULE_PER_LOT]["active"])
        self.assertNotContains(r, "$77.00")

    def test_effective_until_shown_when_present(self):
        until = timezone.now() + timezone.timedelta(days=30)
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("6.00"), effective_until=until)
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["effective_until"], until)
        self.assertContains(r, until.strftime("%d"))

    def test_cpa_bonus_never_shown_even_if_rule_exists(self):
        """A real CPA_BONUS rule row existing must NOT make it appear as
        an active component — the audit confirmed no generator/sweep
        exists for it, so the view's compensation-type list simply never
        includes RULE_CPA_BONUS at all."""
        _make_rule(IBCommissionRule.RULE_CPA_BONUS, referral=self.ref, fixed_amount=Decimal("50.00"))
        r = self.client.get(_program_url())
        rule_types_shown = {c["rule_type"] for c in r.context["components"]}
        self.assertNotIn(IBCommissionRule.RULE_CPA_BONUS, rule_types_shown)
        self.assertNotContains(r, "CPA")
        self.assertNotContains(r, "$50.00")


class ProgramSubIBEmptyStateTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_subib_honest_empty_state_no_invented_levels(self):
        r = self.client.get(_program_url())
        self.assertContains(r, "Programa multinivel aún no configurado")
        # Never any fabricated level/percentage/downline content.
        for forbidden in ("Level 1", "Level 2", "Level 3", "Level 4", "Level 5", "Downline", "Sub-IB Earnings"):
            self.assertNotContains(r, forbidden)


class ProgramCrossIBIsolationTests(TestCase):
    def setUp(self):
        self.owner_a = make_user()
        self.owner_b = make_user()
        self.ref_a = Referral.objects.create(user=self.owner_a, code=_code())
        self.ref_b = Referral.objects.create(user=self.owner_b, code=_code())
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref_a, fixed_amount=Decimal("11.00"))
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref_b, fixed_amount=Decimal("22.00"))

    def test_ib_a_sees_only_its_own_rate(self):
        self.client.force_login(self.owner_a)
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["value"], Decimal("11.00"))
        self.assertNotContains(r, "$22.00")

    def test_ib_b_sees_only_its_own_rate(self):
        self.client.force_login(self.owner_b)
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        self.assertEqual(comps[IBCommissionRule.RULE_PER_LOT]["value"], Decimal("22.00"))
        self.assertNotContains(r, "$11.00")

    def test_referral_code_never_crosses(self):
        """Owner Visual Review FIX-01 removed the referral code display
        from this page entirely (it lives exclusively on /associates/
        now) — so neither IB's code should appear here at all. The
        actual per-IB code isolation is exercised on /associates/
        itself, unchanged and out of this block's scope."""
        self.client.force_login(self.owner_a)
        r = self.client.get(_program_url())
        self.assertNotContains(r, self.ref_a.code)
        self.assertNotContains(r, self.ref_b.code)


class ProgramNavigationTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_sidebar_shows_all_three_real_links(self):
        r = self.client.get(_program_url())
        self.assertContains(r, reverse("simulator:associates"))
        self.assertContains(r, reverse("simulator:associates_clients"))
        self.assertContains(r, reverse("simulator:associates_program"))

    def test_active_section_is_associates_program(self):
        r = self.client.get(_program_url())
        self.assertEqual(r.context["active_section"], "associates_program")

    def test_dashboard_and_clients_pages_still_reachable(self):
        """10A/10B must render exactly as before — this block never
        touches associates_view()/associates_clients_view()."""
        r1 = self.client.get(reverse("simulator:associates"))
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(reverse("simulator:associates_clients"))
        self.assertEqual(r2.status_code, 200)

    def test_programa_link_is_a_real_enabled_route_not_disabled(self):
        """The Programa sidebar link itself must be a real, clickable
        route to /associates/program/ — not the old sb-disabled/
        sb-badge-soon placeholder. Other legitimately-still-disabled
        items (e.g. PAMM) may still use that same CSS class elsewhere on
        the page, so this isolates the sidebar <nav> block first, then
        finds the specific sb-child anchor whose visible text is
        "Programa" within it."""
        r = self.client.get(_program_url())
        html = r.content.decode()
        nav_start = html.index('<nav class="sb-nav"')
        nav_end = html.index("</nav>", nav_start)
        sidebar_html = html[nav_start:nav_end]

        program_url = reverse("simulator:associates_program")
        href_idx = sidebar_html.index(f'href="{program_url}"')
        # The sb-child anchor containing this href — bounded by the
        # nearest <a and the nearest </a> around it.
        anchor_start = sidebar_html.rfind("<a ", 0, href_idx)
        anchor_end = sidebar_html.index("</a>", href_idx)
        anchor_tag = sidebar_html[anchor_start:anchor_end]

        self.assertIn("Programa", anchor_tag)
        self.assertIn("sb-child", anchor_tag)
        self.assertNotIn("sb-disabled", anchor_tag)


class ProgramNoMutationSurfaceTests(TestCase):
    """This page must never accept a write — no POST handler, no form
    that changes economic state."""

    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)

    def test_post_not_allowed(self):
        r = self.client.post(_program_url(), {"fixed_amount": "999.00"})
        self.assertIn(r.status_code, (405, 400))

    def test_no_rate_change_form_in_html(self):
        r = self.client.get(_program_url())
        self.assertNotContains(r, "<form")


class ProgramQueryCountTests(TestCase):
    def test_query_count_is_bounded_not_proportional_to_rule_history(self):
        owner = make_user()
        ref = Referral.objects.create(user=owner, code=_code())
        # A real rate-change history: several closed-out rules per type,
        # only the currently open-ended one should matter for resolution.
        now = timezone.now()
        for i in range(10):
            _make_rule(
                IBCommissionRule.RULE_PER_LOT, referral=ref, fixed_amount=Decimal("5.00"),
                effective_from=now - timezone.timedelta(days=100 - i * 5),
                effective_until=now - timezone.timedelta(days=95 - i * 5),
            )
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=ref, fixed_amount=Decimal("9.00"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=ref, percentage=Decimal("12.000"))
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=ref, percentage=Decimal("6.000"))

        self.client.force_login(owner)
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(_program_url())
        self.assertEqual(r.status_code, 200)
        self.assertLess(
            len(ctx.captured_queries), 30,
            f"{len(ctx.captured_queries)} queries for a 10-row rate history — investigate",
        )


class ProgramReferralCodeDeduplicationFix01Tests(TestCase):
    """Owner Visual Review FIX-01 — the referral code/link tool must live
    exclusively on /associates/ (the Dashboard). /associates/program/
    must never show a second "REFERRAL CODE" card or copy control for
    the same data. Every other section of the program page (Estado del
    Programa, Modalidades Activas, Compensation Components, Special
    Conditions, Sub-IB empty state) must remain exactly as before."""

    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)

    def test_a_program_page_has_no_referral_code_card(self):
        r = self.client.get(_program_url())
        html = r.content.decode()
        self.assertNotIn("Referral Code", html)
        self.assertNotIn("prg-code-row", html)
        self.assertNotIn(self.ref.code, html)

    def test_b_program_page_has_no_second_copy_control(self):
        r = self.client.get(_program_url())
        html = r.content.decode()
        self.assertNotIn("prg-copy-btn", html)
        self.assertNotIn("prgCopyCode", html)
        self.assertNotIn('id="prgRefCode"', html)

    def test_c_dashboard_still_has_its_referral_link(self):
        """/associates/ (10A, untouched by this fix) must still show
        exactly one working referral link/code/copy control."""
        r = self.client.get(reverse("simulator:associates"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Tu enlace de referido", html)
        self.assertIn('id="refUrl"', html)
        self.assertIn("copyRef()", html)
        self.assertIn(self.ref.code, html)

    def test_d_program_status_still_shown(self):
        r = self.client.get(_program_url())
        self.assertContains(r, "Estado del Programa")
        self.assertContains(r, "Activo")

    def test_e_active_modalities_still_shown(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("8.00"))
        r = self.client.get(_program_url())
        self.assertContains(r, "Modalidades Activas")
        self.assertContains(r, "1 configurada")

    def test_f_compensation_components_intact(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("8.00"))
        r = self.client.get(_program_url())
        self.assertContains(r, "Compensation Components")
        self.assertContains(r, "Per Lot")
        self.assertContains(r, "$8.00")

    def test_g_special_conditions_intact(self):
        r = self.client.get(_program_url())
        self.assertContains(r, "Special Conditions")
        self.assertContains(r, "Global")
        self.assertContains(r, "IB-Specific")

    def test_h_subib_empty_state_unchanged(self):
        r = self.client.get(_program_url())
        self.assertContains(r, "Programa Sub-IB")
        self.assertContains(r, "Programa multinivel aún no configurado")

    def test_i_post_still_rejected_with_405(self):
        r = self.client.post(_program_url(), {"fixed_amount": "999.00"})
        self.assertEqual(r.status_code, 405)

    def test_j_no_economic_logic_touched(self):
        """Sanity: creating a rule via the real, unmodified engine path
        and reading it back through the page still resolves to the
        exact same certified value — proves ib_effective_rate() itself
        was never touched by this visual-only fix."""
        rule = _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("18.500"))
        r = self.client.get(_program_url())
        comps = {c["rule_type"]: c for c in r.context["components"]}
        c = comps[IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE]
        self.assertEqual(c["value"], rule.percentage)
        self.assertEqual(c["value"], Decimal("18.500"))

    def test_page_title_updated(self):
        r = self.client.get(_program_url())
        self.assertContains(r, "Programa IB")
        self.assertContains(r, "Tu contrato económico vigente con Money Broker.")


class ProgramPercentageFormattingFix02Tests(TestCase):
    """Owner Visual Review FIX-02 — percentage components render human-
    readable (trailing zeros stripped, no precision ever silently
    dropped), the underlying stored Decimal is never touched, and the
    old duplicated bare "%" line beneath the value is gone. PER_LOT
    (fixed_amount, $/lot) is explicitly unaffected."""

    def setUp(self):
        self.owner = make_user()
        self.ref = Referral.objects.create(user=self.owner, code=_code())
        self.client.force_login(self.owner)

    def _pct_component(self, response, rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE):
        return {c["rule_type"]: c for c in response.context["components"]}[rule_type]

    def test_whole_percentage_strips_to_bare_integer(self):
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("20.000"))
        r = self.client.get(_program_url())
        c = self._pct_component(r)
        self.assertEqual(c["value_display"], "20")
        self.assertEqual(c["value"], Decimal("20.000"))  # raw Decimal untouched
        self.assertContains(r, "20%")
        self.assertNotContains(r, "20.000%")

    def test_second_whole_percentage_15(self):
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=self.ref, percentage=Decimal("15.000"))
        r = self.client.get(_program_url())
        c = self._pct_component(r, IBCommissionRule.RULE_SPREAD_REVENUE_SHARE)
        self.assertEqual(c["value_display"], "15")
        self.assertEqual(c["value"], Decimal("15.000"))
        self.assertContains(r, "15%")
        self.assertNotContains(r, "15.000%")

    def test_half_percentage_keeps_one_significant_decimal(self):
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("12.500"))
        r = self.client.get(_program_url())
        c = self._pct_component(r)
        self.assertEqual(c["value_display"], "12.5")
        self.assertEqual(c["value"], Decimal("12.500"))  # raw Decimal untouched
        self.assertContains(r, "12.5%")
        self.assertNotContains(r, "12.50%")
        self.assertNotContains(r, "12.500%")

    def test_two_significant_decimals_never_lost(self):
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=self.ref, percentage=Decimal("12.250"))
        r = self.client.get(_program_url())
        c = self._pct_component(r, IBCommissionRule.RULE_SPREAD_REVENUE_SHARE)
        self.assertEqual(c["value_display"], "12.25")
        self.assertEqual(c["value"], Decimal("12.250"))
        self.assertContains(r, "12.25%")

    def test_three_significant_decimals_never_lost(self):
        """The field supports 3 decimal places — a value using all three
        significant digits must never be silently rounded away."""
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("33.333"))
        r = self.client.get(_program_url())
        c = self._pct_component(r)
        self.assertEqual(c["value_display"], "33.333")
        self.assertContains(r, "33.333%")

    def test_no_duplicated_bare_percent_line(self):
        """The old design rendered a second, isolated '%' line beneath
        the value for percentage-type components — must be gone."""
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=self.ref, percentage=Decimal("20.000"))
        r = self.client.get(_program_url())
        html = r.content.decode()
        self.assertNotIn('class="prg-comp-unit">%<', html)

    def test_per_lot_unaffected_by_percentage_formatting(self):
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=self.ref, fixed_amount=Decimal("10.00"))
        r = self.client.get(_program_url())
        c = {c["rule_type"]: c for c in r.context["components"]}[IBCommissionRule.RULE_PER_LOT]
        self.assertIsNone(c["value_display"])  # only set for percentage components
        self.assertEqual(c["value"], Decimal("10.00"))
        self.assertContains(r, "$10.00")
        self.assertContains(r, "$ / lot")
