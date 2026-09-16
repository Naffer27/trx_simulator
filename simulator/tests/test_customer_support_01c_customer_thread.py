# simulator/tests/test_customer_support_01c_customer_thread.py
"""
CUSTOMER-SUPPORT-01C — customer-side ticket detail, reply, close,
reopen, IDOR enforcement, INTERNAL-message leak prevention, and
interoperability with the 01B Support Panel.

No new lifecycle emails, no attachments — out of scope for this block
(deferred to 01E), not tested here for that reason.
"""
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import OpsAdminProfile, OwnerRoot, SupportMessage, SupportTicket
from simulator.support_status import apply_transition
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


def _detail_url(t):
    return f"/support/tickets/{t.pk}/"


def _reply_url(t):
    return f"/support/tickets/{t.pk}/reply/"


def _close_url(t):
    return f"/support/tickets/{t.pk}/close/"


def _reopen_url(t):
    return f"/support/tickets/{t.pk}/reopen/"


# ── Access / IDOR ────────────────────────────────────────────────────────

class AccessTests(TestCase):
    def setUp(self):
        self.owner_user = make_user()
        self.other_user = make_user()
        self.ticket = _make_ticket(client_user=self.owner_user)

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_own_ticket_returns_200(self):
        self.client.force_login(self.owner_user)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 200)

    def test_other_users_ticket_returns_404(self):
        self.client.force_login(self.other_user)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 404)

    def test_guessed_nonexistent_id_returns_404(self):
        self.client.force_login(self.owner_user)
        resp = self.client.get("/support/tickets/999999/")
        self.assertEqual(resp.status_code, 404)


class IDORTests(TestCase):
    """detail/reply/close/reopen all 404 on a foreign ticket — never 403."""

    def setUp(self):
        self.owner_user = make_user()
        self.attacker = make_user()
        self.ticket = _make_ticket(client_user=self.owner_user)
        self.client.force_login(self.attacker)

    def test_detail_404(self):
        self.assertEqual(self.client.get(_detail_url(self.ticket)).status_code, 404)

    def test_reply_404(self):
        resp = self.client.post(_reply_url(self.ticket), {"body": "attack"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(SupportMessage.objects.filter(ticket=self.ticket).exists())

    def test_close_404(self):
        resp = self.client.post(_close_url(self.ticket))
        self.assertEqual(resp.status_code, 404)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_OPEN)

    def test_reopen_404(self):
        self.ticket.status = SupportTicket.STATUS_CLOSED
        self.ticket.closed_at = timezone.now()
        self.ticket.save()
        resp = self.client.post(_reopen_url(self.ticket))
        self.assertEqual(resp.status_code, 404)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_CLOSED)


# ── Thread visibility ────────────────────────────────────────────────────

class ThreadVisibilityTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.support = _make_support()
        self.ticket = _make_ticket(client_user=self.client_user, message="original body text")
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="staff customer-visible reply", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="TOP SECRET INTERNAL NOTE", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.client_user, author_role=SupportMessage.ROLE_CLIENT,
            body="customer own reply text", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        self.client.force_login(self.client_user)

    def test_original_message_visible(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "original body text")

    def test_customer_visible_staff_message_visible(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "staff customer-visible reply")

    def test_customer_reply_visible(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "customer own reply text")

    def test_internal_message_not_in_response(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertNotContains(resp, "TOP SECRET INTERNAL NOTE")

    def test_internal_body_not_in_template_context(self):
        resp = self.client.get(_detail_url(self.ticket))
        thread = resp.context["thread"]
        bodies = [entry["body"] for entry in thread]
        self.assertNotIn("TOP SECRET INTERNAL NOTE", bodies)
        # Query-level exclusion: exactly 3 entries (original + 2
        # CUSTOMER_VISIBLE), the INTERNAL row was never fetched at all.
        self.assertEqual(len(thread), 3)


# ── Reply ────────────────────────────────────────────────────────────────

class ReplyTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.client.force_login(self.client_user)

    def test_reply_creates_client_customer_visible_message(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)
        self.client.post(_reply_url(t), {"body": "hello"})
        msg = SupportMessage.objects.get(ticket=t)
        self.assertEqual(msg.author_id, self.client_user.pk)
        self.assertEqual(msg.author_role, SupportMessage.ROLE_CLIENT)
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_CUSTOMER)

    def test_empty_body_rejected(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_reply_url(t), {"body": "   "})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SupportMessage.objects.filter(ticket=t).exists())

    def test_pending_customer_to_pending_support(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_PENDING_CUSTOMER)
        self.client.post(_reply_url(t), {"body": "reply"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_SUPPORT)

    def test_open_reply_moves_to_pending_support(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)
        self.client.post(_reply_url(t), {"body": "reply"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_SUPPORT)

    def test_legacy_pending_reply_moves_to_pending_support(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_PENDING)
        self.client.post(_reply_url(t), {"body": "reply"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_SUPPORT)

    def test_pending_support_reply_stays_pending_support(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_PENDING_SUPPORT)
        self.client.post(_reply_url(t), {"body": "reply"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_SUPPORT)

    def test_closed_reply_reopens(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_CLOSED, closed_at=timezone.now())
        self.client.post(_reply_url(t), {"body": "reopening this"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)
        self.assertIsNone(t.closed_at)
        self.assertTrue(SupportMessage.objects.filter(ticket=t, body="reopening this").exists())

    def test_resolved_reply_reopens(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_RESOLVED, resolved_at=timezone.now())
        self.client.post(_reply_url(t), {"body": "still broken"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)
        self.assertTrue(SupportMessage.objects.filter(ticket=t, body="still broken").exists())

    def test_escalated_reply_keeps_escalated_status(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_ESCALATED, escalated_to_ops=True)
        self.client.post(_reply_url(t), {"body": "more info for ops"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_ESCALATED)
        self.assertTrue(SupportMessage.objects.filter(ticket=t, body="more info for ops").exists())

    def test_reply_never_creates_internal_message(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)
        self.client.post(_reply_url(t), {"body": "hello"})
        self.assertFalse(
            SupportMessage.objects.filter(ticket=t, visibility=SupportMessage.VISIBILITY_INTERNAL).exists()
        )


# ── Close ────────────────────────────────────────────────────────────────

class CloseTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.other_user = make_user()
        self.client.force_login(self.client_user)

    def test_own_eligible_ticket_closes(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_close_url(t))
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_CLOSED)

    def test_closed_at_set(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_RESOLVED, resolved_at=timezone.now())
        self.client.post(_close_url(t))
        t.refresh_from_db()
        self.assertIsNotNone(t.closed_at)

    def test_escalated_cannot_be_closed_by_client(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_ESCALATED, escalated_to_ops=True)
        resp = self.client.post(_close_url(t))
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_ESCALATED)

    def test_another_users_ticket_cannot_be_closed(self):
        t = _make_ticket(client_user=self.other_user, status=SupportTicket.STATUS_OPEN)
        resp = self.client.post(_close_url(t))
        self.assertEqual(resp.status_code, 404)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)


# ── Reopen ───────────────────────────────────────────────────────────────

class ReopenTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.other_user = make_user()
        self.client.force_login(self.client_user)

    def test_closed_to_open(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_CLOSED, closed_at=timezone.now())
        resp = self.client.post(_reopen_url(t))
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)

    def test_closed_at_cleared(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_CLOSED, closed_at=timezone.now())
        self.client.post(_reopen_url(t))
        t.refresh_from_db()
        self.assertIsNone(t.closed_at)

    def test_resolved_at_preserved_through_close_then_reopen(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_RESOLVED)
        apply_transition(t, SupportTicket.STATUS_CLOSED, actor=self.client_user)
        old_resolved_at = t.resolved_at
        self.client.post(_reopen_url(t))
        t.refresh_from_db()
        self.assertEqual(t.resolved_at, old_resolved_at)

    def test_resolved_to_open_directly(self):
        t = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_RESOLVED, resolved_at=timezone.now())
        resp = self.client.post(_reopen_url(t))
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_OPEN)
        self.assertIsNotNone(t.resolved_at)  # preserved historically

    def test_another_users_ticket_cannot_be_reopened(self):
        t = _make_ticket(client_user=self.other_user, status=SupportTicket.STATUS_CLOSED, closed_at=timezone.now())
        resp = self.client.post(_reopen_url(t))
        self.assertEqual(resp.status_code, 404)
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_CLOSED)


# ── Interoperability with 01B ────────────────────────────────────────────

class InteropTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.support = _make_support()
        self.ticket = _make_ticket(client_user=self.client_user, status=SupportTicket.STATUS_OPEN)

    def test_support_customer_visible_reply_appears_customer_side(self):
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="staff says hi", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        self.client.force_login(self.client_user)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "staff says hi")

    def test_customer_reply_appears_support_panel_side(self):
        self.client.force_login(self.client_user)
        self.client.post(_reply_url(self.ticket), {"body": "client says hi"})

        self.client.logout()
        self.client.force_login(self.support)
        resp = self.client.get(f"/staff/support/tickets/{self.ticket.pk}/")
        self.assertContains(resp, "client says hi")

    def test_internal_note_never_leaks_customer_side(self):
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="INTERNAL ONLY SECRET", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )
        self.client.force_login(self.client_user)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertNotContains(resp, "INTERNAL ONLY SECRET")


# ── Global support access (base_app.html) ───────────────────────────────

class GlobalSupportAccessTests(TestCase):
    def test_authenticated_layout_contains_support_link(self):
        user = make_user()
        self.client.force_login(user)
        resp = self.client.get("/support/")
        self.assertContains(resp, 'href="/support/"')

    def test_unauthenticated_redirects_consistently(self):
        resp = self.client.get("/support/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])


class SupportListPageWithTicketRegressionTests(TestCase):
    """
    MANUAL-CERTIFICATION-FIX-01 — reproduces exactly the manually-reported
    browser failure: a logged-in customer with at least one SupportTicket
    hitting GET /support/. support.html's ticket-card loop calls
    {% url 'simulator:support_ticket_detail' ticket.pk %}, which Django
    evaluates (and can raise NoReverseMatch from) at render time — this
    is the one thing SupportListPage rendering can fail on that an empty
    ticket list would never exercise. Uses the real Client + reverse()
    path throughout (never a hardcoded URL string) so a real URL-name
    regression would be caught here the same way Django itself catches
    it when rendering the template.
    """

    def test_support_list_renders_200_with_working_detail_link(self):
        user = make_user()
        ticket = _make_ticket(client_user=user, subject="Regression ticket")
        self.client.force_login(user)

        resp = self.client.get(reverse("simulator:support"))

        self.assertEqual(resp.status_code, 200)
        expected_href = reverse("simulator:support_ticket_detail", args=[ticket.pk])
        self.assertContains(resp, f'href="{expected_href}"')

        # The link must actually work, not just be present in the markup.
        detail_resp = self.client.get(expected_href)
        self.assertEqual(detail_resp.status_code, 200)
