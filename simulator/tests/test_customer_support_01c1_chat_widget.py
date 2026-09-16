# simulator/tests/test_customer_support_01c1_chat_widget.py
"""
CUSTOMER-SUPPORT-01C.1 — floating chat widget.

Architectural principle under test throughout: the widget is NOT a
second support system. Every endpoint reuses SupportTicket/
SupportMessage, the same ownership filter, the same CUSTOMER_VISIBLE-
only thread query, and the same reply/creation logic as the full
/support/ pages — these tests verify that reuse holds, not a parallel
implementation.

No attachments, no new lifecycle emails beyond the existing
ticket-created ones, no model/migration changes, no Support AI logic —
out of scope for this block.
"""
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from simulator.models import OpsAdminProfile, OwnerRoot, SupportMessage, SupportTicket
from .factories import make_user


def _support_permission():
    return Permission.objects.get(content_type__app_label="simulator", codename="is_customer_support")


def _make_owner(**kwargs):
    user = make_user(is_superuser=True, is_staff=True, **kwargs)
    OwnerRoot.objects.create(user=user, established_by="test")
    return user


def _make_ops(owner, **kwargs):
    user = make_user(is_staff=True, **kwargs)
    OpsAdminProfile.objects.create(user=user, assigned_by=owner)
    return user


def _make_support(**kwargs):
    user = make_user(is_staff=False, **kwargs)
    user.user_permissions.add(_support_permission())
    return user


def _make_ticket(client_user=None, **kwargs):
    client_user = client_user or make_user()
    defaults = dict(category=SupportTicket.CATEGORY_OTHER, subject="Help needed", message="Original message body")
    defaults.update(kwargs)
    return SupportTicket.objects.create(user=client_user, **defaults)


WIDGET_URL = "/support/widget/"
WIDGET_NEW_URL = "/support/widget/new/"


def _widget_ticket_url(t):
    return f"/support/widget/ticket/{t.pk}/"


def _widget_reply_url(t):
    return f"/support/widget/ticket/{t.pk}/reply/"


def _widget_close_url(t):
    return f"/support/widget/ticket/{t.pk}/close/"


# ── Global widget presence ──────────────────────────────────────────────

class GlobalWidgetTests(TestCase):
    def test_authenticated_page_contains_widget_trigger(self):
        user = make_user()
        self.client.force_login(user)
        resp = self.client.get(reverse("simulator:support"))
        self.assertContains(resp, 'id="sw-toggle"')
        self.assertContains(resp, 'id="sw-panel"')

    def test_no_duplicate_widget_markup(self):
        user = make_user()
        self.client.force_login(user)
        resp = self.client.get(reverse("simulator:support"))
        content = resp.content.decode()
        self.assertEqual(content.count('id="sw-root"'), 1)
        self.assertEqual(content.count('id="sw-toggle"'), 1)

    def test_anonymous_page_has_no_widget(self):
        resp = self.client.get(reverse("simulator:landing"))
        self.assertNotContains(resp, 'id="sw-root"')

    def test_widget_hidden_on_staff_support_panel(self):
        owner = _make_owner()
        self.client.force_login(owner)
        resp = self.client.get("/staff/support/")
        self.assertNotContains(resp, 'id="sw-root"')


# ── MANUAL-CERTIFICATION-FIX — technical comment leak (root cause:
# Django's {# ... #} comment regex is compiled without re.DOTALL, so it
# cannot match across newlines — a multi-line {# #} block falls through
# as literal text instead of being stripped. Fixed by switching both
# widget fragments to {% comment %}...{% endcomment %}, a real block
# tag whose content is skipped by the parser regardless of line count.
# ─────────────────────────────────────────────────────────────────────

class TemplateCommentLeakTests(TestCase):
    _LEAK_STRINGS = (
        "CUSTOMER-SUPPORT-01C.1",
        "widget thread fragment",
        "widget empty-state",
        "_build_customer_thread",
        "Injected directly",
        "NOT a full page",
    )

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_auto_select_widget_has_no_technical_comment(self):
        resp = self.client.get(WIDGET_URL)
        content = resp.content.decode()
        for leak in self._LEAK_STRINGS:
            self.assertNotIn(leak, content, f"leaked developer comment text: {leak!r}")

    def test_specific_ticket_fragment_has_no_technical_comment(self):
        t = _make_ticket(client_user=self.user)
        resp = self.client.get(_widget_ticket_url(t))
        content = resp.content.decode()
        for leak in self._LEAK_STRINGS:
            self.assertNotIn(leak, content, f"leaked developer comment text: {leak!r}")

    def test_empty_state_has_no_technical_comment(self):
        resp = self.client.get(WIDGET_NEW_URL)
        content = resp.content.decode()
        for leak in self._LEAK_STRINGS:
            self.assertNotIn(leak, content, f"leaked developer comment text: {leak!r}")

    def test_reply_fragment_has_no_technical_comment(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_widget_reply_url(t), {"body": "hi"})
        content = resp.content.decode()
        for leak in self._LEAK_STRINGS:
            self.assertNotIn(leak, content, f"leaked developer comment text: {leak!r}")

    def test_no_developer_helper_or_route_names_exposed(self):
        # Broader safety net: no internal helper/module name leaks
        # anywhere in the rendered empty-state or thread fragments.
        resp = self.client.get(WIDGET_URL)
        content = resp.content.decode()
        for forbidden in ("apply_transition", "support_status.py", "views.py", "_widget_ticket_context"):
            self.assertNotIn(forbidden, content)


# ── Empty-state UX (MANUAL-CERTIFICATION-FIX §6) ─────────────────────────

class EmptyStateQuickLinksTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_empty_state_shows_clean_header(self):
        resp = self.client.get(WIDGET_NEW_URL)
        self.assertContains(resp, "Hola, ¿en qué podemos ayudarte?")

    def test_quick_links_present_in_empty_state(self):
        resp = self.client.get(WIDGET_NEW_URL)
        for label in ("Retiros", "Depósitos", "Trading", "Verificación KYC", "Mi cuenta",
                      "Preguntas frecuentes", "Hablar con soporte"):
            self.assertContains(resp, label)

    def test_quick_links_use_existing_categories_only(self):
        resp = self.client.get(WIDGET_NEW_URL)
        content = resp.content.decode()
        import re
        used = set(re.findall(r'data-sw-quick-category="([^"]+)"', content))
        valid = {c for c, _ in SupportTicket.CATEGORY_CHOICES}
        self.assertTrue(used.issubset(valid))
        self.assertTrue(used)  # at least one quick link actually present

    def test_quick_links_absent_from_active_thread_state(self):
        # Quick links belong to the empty state ONLY — not shown
        # alongside an active ticket's thread.
        t = _make_ticket(client_user=self.user)
        resp = self.client.get(_widget_ticket_url(t))
        self.assertNotContains(resp, "sw-quick-link")

    def test_quick_links_do_not_invent_faq_content(self):
        # "Preguntas frecuentes" is a visual entry point only — no FAQ
        # body/answer text is rendered anywhere in this fragment.
        resp = self.client.get(WIDGET_NEW_URL)
        content = resp.content.decode()
        self.assertNotIn("Respuesta:", content)
        self.assertNotIn("FAQ-", content)


# ── Ownership / IDOR ─────────────────────────────────────────────────────

class WidgetOwnershipTests(TestCase):
    def setUp(self):
        self.owner_user = make_user()
        self.attacker = make_user()
        self.ticket = _make_ticket(client_user=self.owner_user)

    def test_own_ticket_fragment_visible(self):
        self.client.force_login(self.owner_user)
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertEqual(resp.status_code, 200)

    def test_foreign_ticket_fragment_404(self):
        self.client.force_login(self.attacker)
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertEqual(resp.status_code, 404)

    def test_foreign_reply_blocked_404(self):
        self.client.force_login(self.attacker)
        resp = self.client.post(_widget_reply_url(self.ticket), {"body": "attack"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(SupportMessage.objects.filter(ticket=self.ticket).exists())

    def test_foreign_close_blocked_404(self):
        self.client.force_login(self.attacker)
        resp = self.client.post(_widget_close_url(self.ticket))
        self.assertEqual(resp.status_code, 404)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_OPEN)

    def test_widget_auto_select_never_shows_foreign_ticket(self):
        # The attacker has no ticket of their own — auto-select must
        # fall back to the empty state, never leak the owner's ticket.
        self.client.force_login(self.attacker)
        resp = self.client.get(WIDGET_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f"Ticket #{self.ticket.id}")

    def test_no_enumeration_endpoint_exists(self):
        # There is no "list all tickets" widget endpoint at all — the
        # only ticket-specific route requires a pk resolved through the
        # ownership filter.
        self.assertEqual(self.client.get("/support/widget/tickets/").status_code, 404)


# ── Thread content ───────────────────────────────────────────────────────

class WidgetThreadContentTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.support = _make_support()
        self.ticket = _make_ticket(client_user=self.client_user, message="original widget body")
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="staff visible widget reply", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="WIDGET INTERNAL SECRET", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )
        self.client.force_login(self.client_user)

    def test_original_message_visible(self):
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertContains(resp, "original widget body")

    def test_customer_visible_staff_message_visible(self):
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertContains(resp, "staff visible widget reply")

    def test_internal_not_in_rendered_html(self):
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertNotContains(resp, "WIDGET INTERNAL SECRET")

    def test_internal_not_in_template_context(self):
        resp = self.client.get(_widget_ticket_url(self.ticket))
        bodies = [e["body"] for e in resp.context["thread"]]
        self.assertNotIn("WIDGET INTERNAL SECRET", bodies)
        self.assertEqual(len(bodies), 2)  # original + 1 CUSTOMER_VISIBLE reply only

    def test_internal_not_in_auto_select_fragment(self):
        resp = self.client.get(WIDGET_URL)
        self.assertNotContains(resp, "WIDGET INTERNAL SECRET")


# ── New ticket ───────────────────────────────────────────────────────────

class WidgetNewTicketTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_get_returns_empty_form_fragment(self):
        resp = self.client.get(WIDGET_NEW_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "sw-new-form")

    def test_valid_creation_works(self):
        resp = self.client.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_DEPOSIT, "subject": "Widget ticket", "message": "widget message body",
        })
        self.assertEqual(resp.status_code, 200)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.subject, "Widget ticket")
        self.assertContains(resp, f"Ticket #{ticket.id}")  # widget opens it immediately

    def test_invalid_category_rejected(self):
        resp = self.client.post(WIDGET_NEW_URL, {
            "category": "not_a_real_category", "subject": "x", "message": "y",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SupportTicket.objects.filter(user=self.user).exists())
        self.assertContains(resp, "categoría")

    def test_empty_subject_rejected(self):
        resp = self.client.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_OTHER, "subject": "", "message": "y",
        })
        self.assertFalse(SupportTicket.objects.filter(user=self.user).exists())

    def test_empty_message_rejected(self):
        resp = self.client.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_OTHER, "subject": "x", "message": "",
        })
        self.assertFalse(SupportTicket.objects.filter(user=self.user).exists())

    def test_created_ticket_belongs_to_request_user(self):
        self.client.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_OTHER, "subject": "mine", "message": "body",
        })
        ticket = SupportTicket.objects.get(subject="mine")
        self.assertEqual(ticket.user_id, self.user.pk)

    @patch("simulator.tasks.send_email_async.delay")
    def test_created_ticket_email_path_preserved(self, mock_email):
        self.client.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_OTHER, "subject": "email test", "message": "body",
        })
        self.assertTrue(mock_email.called)


# ── Reply ────────────────────────────────────────────────────────────────

class WidgetReplyTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_reply_appears_in_fragment(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_widget_reply_url(t), {"body": "widget reply text"})
        self.assertContains(resp, "widget reply text")

    def test_reply_correct_author_role_and_visibility(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        self.client.post(_widget_reply_url(t), {"body": "hello"})
        msg = SupportMessage.objects.get(ticket=t)
        self.assertEqual(msg.author_id, self.user.pk)
        self.assertEqual(msg.author_role, SupportMessage.ROLE_CLIENT)
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_CUSTOMER)

    def test_open_reply_moves_to_pending_support(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        self.client.post(_widget_reply_url(t), {"body": "hello"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_SUPPORT)

    def test_closed_reply_reopens(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_CLOSED)
        self.client.post(_widget_reply_url(t), {"body": "reopening via widget"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)

    def test_escalated_reply_keeps_status(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_ESCALATED, escalated_to_ops=True)
        self.client.post(_widget_reply_url(t), {"body": "more info"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_ESCALATED)

    def test_empty_body_rejected(self):
        t = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_widget_reply_url(t), {"body": "   "})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SupportMessage.objects.filter(ticket=t).exists())


# ── Status labels ────────────────────────────────────────────────────────

class WidgetStatusLabelTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _label_for(self, status, **extra):
        t = _make_ticket(client_user=self.user, status=status, **extra)
        resp = self.client.get(_widget_ticket_url(t))
        return resp

    def test_open_label(self):
        self.assertContains(self._label_for(SupportTicket.STATUS_OPEN), "Abierto")

    def test_pending_support_label(self):
        self.assertContains(self._label_for(SupportTicket.STATUS_PENDING_SUPPORT), "Esperando soporte")

    def test_legacy_pending_uses_same_label_as_pending_support(self):
        self.assertContains(self._label_for(SupportTicket.STATUS_PENDING), "Esperando soporte")

    def test_pending_customer_label(self):
        self.assertContains(self._label_for(SupportTicket.STATUS_PENDING_CUSTOMER), "Esperando tu respuesta")

    def test_escalated_label(self):
        self.assertContains(
            self._label_for(SupportTicket.STATUS_ESCALATED, escalated_to_ops=True), "Revisión especializada",
        )

    def test_resolved_label(self):
        self.assertContains(self._label_for(SupportTicket.STATUS_RESOLVED), "Resuelto")

    def test_closed_conversation_shows_closed_label_and_no_close_button(self):
        resp = self._label_for(SupportTicket.STATUS_CLOSED)
        self.assertContains(resp, "Cerrado")
        self.assertNotContains(resp, "data-sw-close-form")

    def test_no_new_status_values_introduced(self):
        # Every status the widget can display is one of the pre-existing
        # SupportTicket.STATUS_CHOICES values — no new value invented.
        from simulator.views import _WIDGET_STATUS_LABELS
        valid = {k for k, _ in SupportTicket.STATUS_CHOICES}
        self.assertTrue(set(_WIDGET_STATUS_LABELS.keys()).issubset(valid))


# ── Interoperability with 01B/01C ────────────────────────────────────────

class WidgetInteropTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.support = _make_support()
        self.ticket = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)

    def test_widget_message_appears_in_support_panel(self):
        self.client.force_login(self.client_user)
        self.client.post(_widget_reply_url(self.ticket), {"body": "from the widget"})

        self.client.logout()
        self.client.force_login(self.support)
        resp = self.client.get(f"/staff/support/tickets/{self.ticket.pk}/")
        self.assertContains(resp, "from the widget")

    def test_support_reply_appears_in_widget(self):
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="agent reply for widget", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        self.client.force_login(self.client_user)
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertContains(resp, "agent reply for widget")

    def test_widget_reply_also_appears_on_full_detail_page(self):
        self.client.force_login(self.client_user)
        self.client.post(_widget_reply_url(self.ticket), {"body": "cross-surface check"})
        resp = self.client.get(f"/support/tickets/{self.ticket.pk}/")
        self.assertContains(resp, "cross-surface check")

    def test_internal_note_absent_from_widget(self):
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="INTERNAL VIA PANEL", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )
        self.client.force_login(self.client_user)
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertNotContains(resp, "INTERNAL VIA PANEL")


# ── Security ─────────────────────────────────────────────────────────────

class WidgetSecurityTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.ticket = _make_ticket(client_user=self.user, status=SupportTicket.STATUS_OPEN)

    def test_anonymous_widget_redirects_to_login(self):
        resp = self.client.get(WIDGET_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_anonymous_widget_ticket_redirects_to_login(self):
        resp = self.client.get(_widget_ticket_url(self.ticket))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_csrf_required_on_reply(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        resp = strict.post(_widget_reply_url(self.ticket), {"body": "no csrf token"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(SupportMessage.objects.filter(ticket=self.ticket).exists())

    def test_csrf_required_on_close(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        resp = strict.post(_widget_close_url(self.ticket))
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_OPEN)

    def test_csrf_required_on_new_ticket(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        resp = strict.post(WIDGET_NEW_URL, {
            "category": SupportTicket.CATEGORY_OTHER, "subject": "x", "message": "y",
        })
        self.assertEqual(resp.status_code, 403)


# ── Regression: existing full-page routes still work ─────────────────────

class ExistingPagesStillWorkTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.ticket = _make_ticket(client_user=self.user)
        self.client.force_login(self.user)

    def test_support_list_page_still_200(self):
        self.assertEqual(self.client.get(reverse("simulator:support")).status_code, 200)

    def test_support_detail_page_still_200(self):
        resp = self.client.get(reverse("simulator:support_ticket_detail", args=[self.ticket.pk]))
        self.assertEqual(resp.status_code, 200)
