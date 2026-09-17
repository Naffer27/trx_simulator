# simulator/tests/test_customer_support_01d_knowledge_base.py
"""
CUSTOMER-SUPPORT-01D — Knowledge Base + Instant Answers Foundation.

Deterministic, code-backed catalogue only — NOT generative AI. No LLM
call, no embedding lookup, no vector database, no free-form intent
classifier is exercised or even importable from this surface.

Source of truth: docs/CUSTOMER_SUPPORT_KNOWLEDGE_BASE_SPEC_V1.md.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.models import SupportMessage, SupportTicket
from simulator.support_knowledge.catalogue import CATALOGUE, KnowledgeAction, KnowledgeCategory, RiskLevel
from simulator.support_knowledge import service as kb
from .factories import make_user


def _make_ticket(client_user=None, **kwargs):
    client_user = client_user or make_user()
    defaults = dict(category=SupportTicket.CATEGORY_OTHER, subject="Help needed", message="Original message body")
    defaults.update(kwargs)
    return SupportTicket.objects.create(user=client_user, **defaults)


WIDGET_URL = "/support/widget/"


def _questions_url(category):
    return f"/support/widget/knowledge/{category}/"


def _answer_url(intent):
    return f"/support/widget/knowledge/answer/{intent}/"


# ── Catalogue integrity ──────────────────────────────────────────────────

class CatalogueIntegrityTests(TestCase):
    def test_unique_intent_ids(self):
        intents = [item.intent for item in CATALOGUE]
        self.assertEqual(len(intents), len(set(intents)))

    def test_all_categories_valid(self):
        for item in CATALOGUE:
            self.assertIsInstance(item.category, KnowledgeCategory)

    def test_all_risk_levels_valid(self):
        for item in CATALOGUE:
            self.assertIn(item.risk_level, (RiskLevel.GREEN, RiskLevel.YELLOW, RiskLevel.RED))

    def test_all_actions_valid(self):
        for item in CATALOGUE:
            self.assertIn(item.action, (KnowledgeAction.ANSWER, KnowledgeAction.SUPPORT, KnowledgeAction.OPS))

    def test_enabled_green_items_have_nonempty_answer(self):
        for item in CATALOGUE:
            if item.enabled and item.risk_level == RiskLevel.GREEN:
                self.assertTrue(item.answer.strip(), f"{item.intent} is enabled GREEN with an empty answer")

    def test_green_items_use_answer_action(self):
        for item in CATALOGUE:
            if item.risk_level == RiskLevel.GREEN:
                self.assertEqual(item.action, KnowledgeAction.ANSWER)

    def test_non_green_items_never_use_answer_action(self):
        for item in CATALOGUE:
            if item.risk_level != RiskLevel.GREEN:
                self.assertNotEqual(item.action, KnowledgeAction.ANSWER)

    def test_disabled_items_are_never_returned_by_get_item(self):
        for item in CATALOGUE:
            if not item.enabled:
                self.assertIsNone(kb.get_item(item.intent))

    def test_disabled_items_are_never_returned_by_list_questions(self):
        for category in KnowledgeCategory:
            questions = kb.list_questions(category.value)
            returned_intents = {q.intent for q in questions}
            disabled_in_category = {
                i.intent for i in CATALOGUE if i.category == category and not i.enabled
            }
            self.assertEqual(returned_intents & disabled_in_category, set())

    def test_withdrawal_minimum_not_exposed(self):
        """The explicit WITHDRAWAL-POLICY-CORRECTION-02 safeguard."""
        self.assertIsNone(kb.get_item("withdrawal_minimum"))
        withdrawal_questions = kb.list_questions("withdrawals")
        self.assertNotIn("withdrawal_minimum", [q.intent for q in withdrawal_questions])

    def test_all_security_intents_are_red(self):
        for item in CATALOGUE:
            if item.category == KnowledgeCategory.SECURITY:
                self.assertEqual(item.risk_level, RiskLevel.RED)

    def test_list_categories_only_includes_categories_with_enabled_items(self):
        categories = kb.list_categories()
        slugs = {slug for slug, _ in categories}
        for category in KnowledgeCategory:
            has_enabled = any(i.category == category and i.enabled for i in CATALOGUE)
            self.assertEqual(category.value in slugs, has_enabled)


# ── Widget: catalogue access ─────────────────────────────────────────────

class WidgetKnowledgeAccessTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_category_list_visible_at_entry_point(self):
        resp = self.client.get(WIDGET_URL)
        self.assertEqual(resp.status_code, 200)
        for label in ("Retiros", "Depósitos", "KYC", "Trading", "Cuenta", "Seguridad", "Soporte técnico"):
            self.assertContains(resp, label)

    def test_category_returns_correct_questions(self):
        resp = self.client.get(_questions_url("withdrawals"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "¿Cómo retiro fondos?")
        self.assertContains(resp, "¿Necesito KYC para retirar?")

    def test_category_does_not_return_other_categories_questions(self):
        resp = self.client.get(_questions_url("withdrawals"))
        self.assertNotContains(resp, "¿Qué es el equity?")  # trading-only

    def test_unknown_category_returns_empty_list_not_error(self):
        resp = self.client.get(_questions_url("not_a_real_category"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No hay preguntas disponibles")

    def test_green_question_returns_exact_approved_answer(self):
        item = kb.get_item("withdrawal_how_to")
        resp = self.client.get(_answer_url("withdrawal_how_to"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.answer)

    def test_answer_rendered_as_support_assistant_bubble(self):
        resp = self.client.get(_answer_url("withdrawal_how_to"))
        self.assertContains(resp, "Money Broker Support")

    def test_human_support_option_present_on_answer(self):
        resp = self.client.get(_answer_url("withdrawal_how_to"))
        self.assertContains(resp, "Hablar con soporte")

    def test_disabled_intent_404s(self):
        resp = self.client.get(_answer_url("withdrawal_minimum"))
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_intent_404s(self):
        resp = self.client.get(_answer_url("this_intent_does_not_exist"))
        self.assertEqual(resp.status_code, 404)

    def test_no_identity_claims_of_human_or_ai(self):
        resp = self.client.get(_answer_url("withdrawal_how_to"))
        content = resp.content.decode()
        for forbidden in ("agente humano", "agente en línea", "IA", "inteligencia artificial", "está escribiendo"):
            self.assertNotIn(forbidden, content)


# ── YELLOW behavior ──────────────────────────────────────────────────────

class YellowBehaviorTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_does_not_fabricate_account_status(self):
        resp = self.client.get(_answer_url("withdrawal_not_received"))
        content = resp.content.decode()
        # Never states a concrete status/amount/date as if it were real.
        self.assertNotIn("tu retiro fue procesado", content.lower())
        self.assertNotIn("tu retiro está pendiente", content.lower())
        self.assertContains(resp, "información específica de tu cuenta")

    def test_offers_human_support(self):
        resp = self.client.get(_answer_url("withdrawal_not_received"))
        self.assertContains(resp, "Hablar con soporte")

    def test_handoff_link_preselects_correct_ticket_category(self):
        resp = self.client.get(_answer_url("withdrawal_not_received"))
        self.assertContains(resp, "category=withdrawal_issue")


# ── RED behavior ─────────────────────────────────────────────────────────

class RedBehaviorTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_not_presented_as_ordinary_faq(self):
        resp = self.client.get(_answer_url("unauthorized_withdrawal"))
        self.assertContains(resp, "sw-msg-security")
        self.assertContains(resp, "caso de seguridad")

    def test_routes_to_human_support(self):
        resp = self.client.get(_answer_url("unauthorized_withdrawal"))
        self.assertContains(resp, "Hablar con soporte")

    def test_does_not_auto_escalate_or_grant_new_permission(self):
        """01D never calls into owner_actions/support_status escalation
        directly from a widget click — the handoff only opens the
        existing new-ticket form; actual Ops escalation remains a human
        action via the existing 01B escalate control."""
        before_count = SupportTicket.objects.filter(user=self.user).count()
        self.client.get(_answer_url("unauthorized_withdrawal"))
        after_count = SupportTicket.objects.filter(user=self.user).count()
        self.assertEqual(before_count, after_count)

    def test_all_security_category_questions_are_red_styled(self):
        resp = self.client.get(_questions_url("security"))
        self.assertEqual(resp.status_code, 200)
        for item in kb.list_questions("security"):
            answer_resp = self.client.get(_answer_url(item.intent))
            self.assertContains(answer_resp, "sw-msg-security")


# ── Human handoff (existing ticket flow reused) ──────────────────────────

class HumanHandoffTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_handoff_opens_existing_new_ticket_form(self):
        resp = self.client.get(_answer_url("withdrawal_not_received"))
        # extract the handoff URL and follow it
        content = resp.content.decode()
        start = content.index('data-sw-fetch="/support/widget/new/')
        end = content.index('"', start + len('data-sw-fetch="'))
        handoff_url = content[start + len('data-sw-fetch="'):end].replace("&amp;", "&")
        follow = self.client.get(handoff_url)
        self.assertEqual(follow.status_code, 200)
        self.assertContains(follow, "sw-new-form")

    def test_handoff_prefills_topic_as_subject(self):
        resp = self.client.get(_answer_url("withdrawal_not_received"))
        content = resp.content.decode()
        start = content.index('data-sw-fetch="/support/widget/new/')
        end = content.index('"', start + len('data-sw-fetch="'))
        handoff_url = content[start + len('data-sw-fetch="'):end].replace("&amp;", "&")
        follow = self.client.get(handoff_url)
        self.assertContains(follow, "No he recibido mi retiro")

    def test_handoff_creation_uses_existing_ticket_creation_path(self):
        """The actual ticket creation, once the customer submits the
        prefilled form, is 100% the existing _create_support_ticket()
        logic — no parallel creation path for KB-originated tickets."""
        resp = self.client.post("/support/widget/new/", {
            "category": "withdrawal_issue", "subject": "No he recibido mi retiro", "message": "detalle",
        })
        self.assertEqual(resp.status_code, 200)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.category, "withdrawal_issue")
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)


# ── Security / IDOR / leak prevention ─────────────────────────────────────

class KnowledgeSecurityTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_anonymous_category_denied(self):
        resp = self.client.get(_questions_url("withdrawals"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_anonymous_answer_denied(self):
        resp = self.client.get(_answer_url("withdrawal_how_to"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_no_foreign_ticket_access_via_knowledge_routes(self):
        """Knowledge routes touch no per-user data at all — verifying
        a second, unrelated user's own tickets remain fully invisible
        through this surface."""
        other = make_user()
        other_ticket = _make_ticket(client_user=other)
        self.client.force_login(self.user)
        resp = self.client.get(WIDGET_URL)
        self.assertNotContains(resp, f"Ticket #{other_ticket.id}")

    def test_internal_never_exposed_via_knowledge_answer(self):
        self.client.force_login(self.user)
        for item in CATALOGUE:
            if not item.enabled:
                continue
            resp = self.client.get(_answer_url(item.intent))
            self.assertNotContains(resp, "INTERNAL")

    def test_no_developer_metadata_leak_in_any_fragment(self):
        self.client.force_login(self.user)
        leaks = ("CUSTOMER-SUPPORT-01D", "catalogue.py", "service.py", "_enabled_items",
                 "KnowledgeAction", "KnowledgeCategory", "support_knowledge")
        targets = [WIDGET_URL, _questions_url("withdrawals"), _answer_url("withdrawal_how_to")]
        for url in targets:
            content = self.client.get(url).content.decode()
            for leak in leaks:
                self.assertNotIn(leak, content, f"leaked {leak!r} at {url}")

    def test_no_multiline_template_comment_leak(self):
        self.client.force_login(self.user)
        for url in [WIDGET_URL, _questions_url("withdrawals"), _answer_url("withdrawal_how_to"),
                    "/support/widget/new/"]:
            content = self.client.get(url).content.decode()
            self.assertNotIn("{#", content)
            self.assertNotIn("NOT a full page", content)

    def test_escalation_reason_never_exposed(self):
        self.client.force_login(self.user)
        for item in CATALOGUE:
            if not item.enabled:
                continue
            resp = self.client.get(_answer_url(item.intent))
            self.assertNotContains(resp, "escalation_reason")
            self.assertNotContains(resp, "assigned_to")


# ── Regression: 01A/01B/01C/01C.1 still green (spot check here; full
# suites run separately by the harness/report) ───────────────────────────

class RegressionSpotCheckTests(TestCase):
    def test_support_list_page_still_works(self):
        user = make_user()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("simulator:support")).status_code, 200)

    def test_widget_root_unaffected_by_knowledge_routes_existing(self):
        # MANUAL-CERTIFICATION-FIX-03: the widget root no longer
        # auto-opens a ticket at all (see ActiveTicketOverridesKbHomeDocumentedTests
        # for the full coverage of the product decision) — this spot
        # check just confirms an existing ticket's presence doesn't
        # break the KB-home route itself.
        user = make_user()
        _make_ticket(client_user=user, status=SupportTicket.STATUS_OPEN)
        self.client.force_login(user)
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")


# ── MANUAL-CERTIFICATION-FIX — widget entry point + no stale cache ───────
#
# Root cause: none of the /support/widget/* fragment views sent a
# Cache-Control header, so a browser's fetch() (default cache mode)
# could silently replay an earlier-cached response body — e.g. the
# pre-01D form-only fragment — instead of re-hitting the server, even
# though the server-side routing logic was already correct. Fixed with
# @never_cache on every widget fragment view. These tests both prove
# the entry-point routing behavior itself and prove the cache-control
# headers that make it reliable in a real browser.

class WidgetEntryPointNavigationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_initial_widget_get_renders_kb_entry_point(self):
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")
        self.assertContains(resp, "Retiros")
        self.assertContains(resp, "Hablar con soporte")

    def test_initial_widget_get_does_not_render_new_ticket_form(self):
        resp = self.client.get(WIDGET_URL)
        self.assertNotContains(resp, "sw-new-form")
        self.assertNotContains(resp, "Iniciar conversación")

    def test_hablar_con_soporte_loads_new_ticket_form(self):
        resp = self.client.get("/support/widget/new/")
        self.assertContains(resp, "sw-new-form")
        self.assertContains(resp, "Iniciar conversación")
        self.assertNotContains(resp, "Hola, ¿en qué podemos ayudarte?")

    def test_back_from_new_ticket_form_returns_to_kb_entry(self):
        resp = self.client.get("/support/widget/new/")
        content = resp.content.decode()
        self.assertIn(f'data-sw-fetch="{WIDGET_URL}"', content)
        back_resp = self.client.get(WIDGET_URL)
        self.assertContains(back_resp, "Hola, ¿en qué podemos ayudarte?")

    def test_back_from_knowledge_questions_returns_to_kb_entry(self):
        resp = self.client.get(_questions_url("withdrawals"))
        content = resp.content.decode()
        self.assertIn(f'data-sw-fetch="{WIDGET_URL}"', content)


class WidgetFragmentNoStaleCacheTests(TestCase):
    """Proves the actual root-cause fix: every widget fragment response
    is marked non-cacheable, so a browser can never silently replay a
    stale fragment body from an earlier session on a later open."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _assert_never_cached(self, resp):
        cache_control = resp.get("Cache-Control", "")
        self.assertIn("no-store", cache_control)
        self.assertIn("no-cache", cache_control)

    def test_widget_entry_point_not_cacheable(self):
        self._assert_never_cached(self.client.get(WIDGET_URL))

    def test_new_ticket_form_not_cacheable(self):
        self._assert_never_cached(self.client.get("/support/widget/new/"))

    def test_knowledge_questions_not_cacheable(self):
        self._assert_never_cached(self.client.get(_questions_url("withdrawals")))

    def test_knowledge_answer_not_cacheable(self):
        item = kb.list_questions("withdrawals")[0]
        self._assert_never_cached(self.client.get(_answer_url(item.intent)))

    def test_active_thread_not_cacheable(self):
        ticket = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        self._assert_never_cached(self.client.get(WIDGET_URL))
        self._assert_never_cached(self.client.get(f"/support/widget/ticket/{ticket.pk}/"))


# ── MANUAL-CERTIFICATION-FIX-02 — actual navigation semantics trace ──────
#
# Full call-chain trace performed against both static source and a live
# HTTP round trip through the actual running Daphne process confirmed:
# the floating trigger's JS calls loadState() -> fetchFragment on FIRST
# open, and loadState() has exactly one hardcoded literal target,
# '/support/widget/'. There is no other file anywhere under
# simulator/templates/ that references sw-toggle/sw-panel/sw-body/
# fetchFragment/support widget routes, no duplicate IIFE, no stale
# data-sw-new-trigger / data-sw-quick-category selector, and no second
# click handler bound to the same elements. These tests pin that source
# shape down so a future edit can't silently reintroduce a dual path.

import re as _re
from pathlib import Path as _Path

_BASE_APP_TEMPLATE = (
    _Path(__file__).resolve().parent.parent
    / "templates" / "simulator" / "base_app.html"
)


class FirstOpenSourceTraceTests(TestCase):
    """Static trace of the widget's client-side entry point — proves
    there is exactly one code path from the floating button to the
    first fragment request, and that it targets /support/widget/."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = _BASE_APP_TEMPLATE.read_text(encoding="utf-8")

    def test_loadstate_targets_widget_root_exactly_once(self):
        # loadState() is the ONLY function that runs on first open
        # (via openPanel(), bound to the single #sw-toggle click
        # listener) — it must call fetchFragment('/support/widget/')
        # exactly once, with no other literal target.
        matches = _re.findall(
            r"function loadState\s*\(\)\s*\{\s*fetchFragment\('([^']+)'\)",
            self.source,
        )
        self.assertEqual(matches, ["/support/widget/"])

    def test_loadstate_never_targets_new_ticket_route(self):
        loadstate_body = _re.search(
            r"function loadState\s*\([^)]*\)\s*\{(.*?)\n  \}",
            self.source, _re.DOTALL,
        )
        self.assertIsNotNone(loadstate_body)
        self.assertNotIn("/support/widget/new/", loadstate_body.group(1))

    def test_single_toggle_click_listener_bound_to_toggle(self):
        # Exactly one addEventListener call on the `toggle` variable —
        # no second/duplicate handler that could independently trigger
        # a fragment request on the same button.
        self.assertEqual(self.source.count("toggle.addEventListener("), 1)

    def test_single_openpanel_definition_calling_loadstate_once(self):
        self.assertEqual(self.source.count("function openPanel()"), 1)
        openpanel_body = _re.search(
            r"function openPanel\(\)\s*\{(.*?)\n  \}", self.source, _re.DOTALL,
        ).group(1)
        # loadState() must be reachable but not literally duplicated
        # into two independent calls that could race two fragment
        # fetches against each other.
        self.assertEqual(openpanel_body.count("loadState()"), 2)  # if/else branches, same call

    def test_no_stale_pre_01d_selectors_remain(self):
        # These attribute names were replaced by the unified
        # data-sw-fetch pattern in 01D — their reintroduction would
        # mean a second, competing handler path exists again.
        self.assertNotIn("data-sw-new-trigger", self.source)
        self.assertNotIn("data-sw-quick-category", self.source)

    def test_no_duplicate_widget_script_block(self):
        # Only one IIFE owns #sw-toggle/#sw-panel/#sw-body — a second
        # copy (e.g. from a botched merge) would silently double-bind
        # listeners and could race two fragment requests per click.
        self.assertEqual(self.source.count("getElementById('sw-toggle')"), 1)
        self.assertEqual(self.source.count("getElementById('sw-panel')"), 1)
        self.assertEqual(self.source.count("getElementById('sw-body')"), 1)

    def test_widget_wiring_lives_only_in_base_app_template(self):
        # No other template in the project defines a competing trigger
        # that could be what the browser is actually hitting.
        templates_dir = _BASE_APP_TEMPLATE.parent.parent
        offenders = []
        for path in templates_dir.rglob("*.html"):
            if path == _BASE_APP_TEMPLATE:
                continue
            text = path.read_text(encoding="utf-8")
            if "sw-toggle" in text or "fetchFragment(" in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [])


class HablarConSoporteTargetTests(TestCase):
    """Pins the exact URL the human-handoff button targets, and that
    the new-ticket form's back link returns to the KB entry point —
    the two ends of the ONLY sanctioned path to /support/widget/new/."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_hablar_con_soporte_button_targets_new_ticket_route(self):
        resp = self.client.get(WIDGET_URL)
        content = resp.content.decode()
        self.assertIn(
            'data-sw-fetch="/support/widget/new/">\n    Hablar con soporte'.replace("\n    ", ""),
            content.replace("\n", "").replace("  ", ""),
        )

    def test_new_ticket_form_back_link_targets_widget_root(self):
        resp = self.client.get("/support/widget/new/")
        content = resp.content.decode()
        self.assertIn(f'data-sw-fetch="{WIDGET_URL}"', content)

    def test_live_request_to_widget_root_never_returns_new_ticket_form(self):
        # Direct proof for a fresh, ticket-less account: GET
        # /support/widget/ never renders the new-ticket form markup.
        resp = self.client.get(WIDGET_URL)
        self.assertNotContains(resp, "sw-new-form")
        self.assertNotContains(resp, 'name="category"')
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")


class WidgetRootAlwaysKbHomeTests(TestCase):
    """MANUAL-CERTIFICATION-FIX-03 — supersedes the now-obsolete
    "active ticket overrides KB home" behavior asserted by the previous
    fix round (formerly ActiveTicketOverridesKbHomeDocumentedTests).
    Product decision: the widget ALWAYS opens on the Knowledge Base
    home, regardless of ticket status — _active_widget_ticket() and its
    auto-select rule were removed from support_widget_view entirely
    (it had no other caller). Existing conversations are reachable only
    through the explicit "Mis conversaciones" flow, never automatically."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_open_ticket_does_not_override_kb_home(self):
        _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")
        self.assertNotContains(resp, "sw-status-row")

    def test_pending_customer_ticket_does_not_override_kb_home(self):
        _make_ticket(client_user=self.user, status=SupportTicket.STATUS_PENDING_CUSTOMER)
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")

    def test_escalated_ticket_does_not_override_kb_home(self):
        _make_ticket(client_user=self.user, status=SupportTicket.STATUS_ESCALATED)
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")

    def test_most_recently_updated_ticket_still_does_not_override_kb_home(self):
        older = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        newer = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_PENDING_CUSTOMER)
        SupportTicket.objects.filter(pk=older.pk).update(updated_at=newer.updated_at - __import__("datetime").timedelta(days=1))
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")
        self.assertNotContains(resp, f"Ticket #{newer.id}")

    def test_closed_ticket_still_does_not_override_kb_home(self):
        _make_ticket(client_user=self.user, status=SupportTicket.STATUS_CLOSED)
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")

    def test_kb_home_shows_mis_conversaciones_and_hablar_con_soporte(self):
        resp = self.client.get(WIDGET_URL)
        self.assertContains(resp, "Mis conversaciones")
        self.assertContains(resp, "Hablar con soporte")


# ── MANUAL-CERTIFICATION-FIX-03 — "Mis conversaciones" ───────────────────

CONVERSATIONS_URL = "/support/widget/conversations/"


class MisConversacionesTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user()
        self.client.force_login(self.user)

    def test_lists_own_tickets_only(self):
        mine = _make_ticket(client_user=self.user, subject="Mi ticket", status=SupportTicket.STATUS_OPEN)
        _make_ticket(client_user=self.other, subject="Ticket ajeno", status=SupportTicket.STATUS_OPEN)
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertContains(resp, f"#{mine.pk}")
        self.assertNotContains(resp, "Ticket ajeno")
        self.assertEqual(resp.content.decode().count("sw-conv-item"), 1)

    def test_lists_open_pending_and_closed_tickets(self):
        open_t = _make_ticket(client_user=self.user, subject="Abierto", status=SupportTicket.STATUS_OPEN)
        pending_t = _make_ticket(client_user=self.user, subject="Pendiente", status=SupportTicket.STATUS_PENDING_CUSTOMER)
        closed_t = _make_ticket(client_user=self.user, subject="Cerrado", status=SupportTicket.STATUS_CLOSED)
        resp = self.client.get(CONVERSATIONS_URL)
        content = resp.content.decode()
        for t in (open_t, pending_t, closed_t):
            self.assertIn(f"#{t.pk}", content)

    def test_empty_state_when_no_tickets(self):
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertNotContains(resp, "sw-conv-item")
        self.assertContains(resp, "conversaciones")

    def test_shows_friendly_status_label_not_raw_code(self):
        _make_ticket(client_user=self.user, status=SupportTicket.STATUS_PENDING_CUSTOMER)
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertContains(resp, "Esperando tu respuesta")
        self.assertNotContains(resp, "pending_customer")

    def test_shows_category_label_not_raw_code(self):
        _make_ticket(client_user=self.user, category=SupportTicket.CATEGORY_WITHDRAWAL)
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertContains(resp, "Problema con retiro")
        self.assertNotContains(resp, "withdrawal_issue")

    def test_does_not_expose_internal_metadata(self):
        ticket = _make_ticket(client_user=self.user)
        SupportMessage.objects.create(
            ticket=ticket, author=self.user, author_role=SupportMessage.ROLE_SUPPORT,
            body="nota interna de staff", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertNotContains(resp, "nota interna de staff")
        self.assertNotContains(resp, "assigned_to")
        self.assertNotContains(resp, "escalation_reason")

    def test_clicking_own_ticket_opens_thread(self):
        ticket = _make_ticket(client_user=self.user)
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertIn(f'data-sw-fetch="/support/widget/ticket/{ticket.pk}/"', resp.content.decode())
        thread_resp = self.client.get(f"/support/widget/ticket/{ticket.pk}/")
        self.assertContains(thread_resp, f"Ticket #{ticket.id}")

    def test_foreign_ticket_404s(self):
        theirs = _make_ticket(client_user=self.other)
        resp = self.client.get(f"/support/widget/ticket/{theirs.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_denied(self):
        self.client.logout()
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertEqual(resp.status_code, 302)

    def test_not_cacheable(self):
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertIn("no-store", resp.get("Cache-Control", ""))

    def test_back_link_returns_to_kb_home(self):
        resp = self.client.get(CONVERSATIONS_URL)
        self.assertIn('data-sw-fetch="/support/widget/"', resp.content.decode())

    def test_thread_back_links_to_conversations_and_home(self):
        ticket = _make_ticket(client_user=self.user)
        resp = self.client.get(f"/support/widget/ticket/{ticket.pk}/")
        content = resp.content.decode()
        self.assertIn(f'data-sw-fetch="{CONVERSATIONS_URL}"', content)
        self.assertIn('data-sw-fetch="/support/widget/"', content)

    def test_new_ticket_appears_under_conversations_after_creation(self):
        resp = self.client.post("/support/widget/new/", {
            "category": SupportTicket.CATEGORY_OTHER,
            "subject": "Nuevo caso",
            "message": "Detalle del caso",
        })
        self.assertContains(resp, "Nuevo caso")
        list_resp = self.client.get(CONVERSATIONS_URL)
        self.assertContains(list_resp, "Nuevo caso")
