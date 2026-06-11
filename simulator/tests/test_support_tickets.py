# simulator/tests/test_support_tickets.py
"""
Support Tickets MVP — test suite.

Covers:
  1. Anonymous user is redirected to login.
  2. Authenticated user can GET /support/.
  3. Valid POST creates a SupportTicket.
  4. Created ticket belongs to the authenticated user.
  5. User sees only their own tickets.
  6. Another user's tickets are not visible.
  7. SupportTicket is registered in admin.
  8. Admin actions change status correctly.
  9. Sidebar contains "Soporte" link.
 10. Invalid POST (missing subject) shows error, no ticket created.
 11. Invalid POST (bad category) shows error, no ticket created.
 12. POST creates ticket with status=open and priority=normal.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from simulator.models import SupportTicket
from simulator.tests.factories import make_user

User = get_user_model()


class SupportAnonRedirectTests(TestCase):
    def test_anonymous_redirects_to_login(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertRedirects(r, f"/login/?next=/support/", fetch_redirect_response=False)


class SupportGetTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.login(username=self.user.username, password="testpass123")

    def test_get_returns_200(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertEqual(r.status_code, 200)

    def test_get_uses_support_template(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertTemplateUsed(r, "simulator/support.html")

    def test_get_context_has_category_choices(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertIn("category_choices", r.context)
        self.assertTrue(len(r.context["category_choices"]) > 0)

    def test_get_context_has_tickets(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertIn("tickets", r.context)


class SupportPostTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.login(username=self.user.username, password="testpass123")
        self.url = reverse("simulator:support")
        self.valid_data = {
            "category": "deposit_issue",
            "subject":  "My deposit is missing",
            "message":  "I deposited yesterday but balance is still 0.",
        }

    def test_valid_post_creates_ticket(self):
        self.client.post(self.url, self.valid_data)
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 1)

    def test_ticket_belongs_to_authenticated_user(self):
        self.client.post(self.url, self.valid_data)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.user, self.user)

    def test_ticket_has_correct_fields(self):
        self.client.post(self.url, self.valid_data)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.category, "deposit_issue")
        self.assertEqual(ticket.subject, "My deposit is missing")
        self.assertIn("balance is still 0", ticket.message)

    def test_ticket_default_status_is_open(self):
        self.client.post(self.url, self.valid_data)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)

    def test_ticket_default_priority_is_normal(self):
        self.client.post(self.url, self.valid_data)
        ticket = SupportTicket.objects.get(user=self.user)
        self.assertEqual(ticket.priority, SupportTicket.PRIORITY_NORMAL)

    def test_valid_post_returns_200_with_success(self):
        r = self.client.post(self.url, self.valid_data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["success"])

    def test_missing_subject_shows_error(self):
        data = dict(self.valid_data, subject="")
        r = self.client.post(self.url, data)
        self.assertFalse(r.context["success"])
        self.assertIsNotNone(r.context["error"])
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 0)

    def test_missing_message_shows_error(self):
        data = dict(self.valid_data, message="")
        r = self.client.post(self.url, data)
        self.assertFalse(r.context["success"])
        self.assertIsNotNone(r.context["error"])
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 0)

    def test_invalid_category_shows_error(self):
        data = dict(self.valid_data, category="not_a_real_category")
        r = self.client.post(self.url, data)
        self.assertFalse(r.context["success"])
        self.assertIsNotNone(r.context["error"])
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 0)

    def test_missing_category_shows_error(self):
        data = dict(self.valid_data, category="")
        r = self.client.post(self.url, data)
        self.assertFalse(r.context["success"])
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 0)


class SupportTicketIsolationTests(TestCase):
    """User sees only their own tickets."""

    def setUp(self):
        self.user_a = make_user()
        self.user_b = make_user()

    def _make_ticket(self, user, subject="Test"):
        return SupportTicket.objects.create(
            user=user,
            category=SupportTicket.CATEGORY_OTHER,
            subject=subject,
            message="detail",
        )

    def test_user_sees_own_tickets(self):
        self._make_ticket(self.user_a, "Ticket A")
        self.client.login(username=self.user_a.username, password="testpass123")
        r = self.client.get(reverse("simulator:support"))
        tickets = list(r.context["tickets"])
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].user, self.user_a)

    def test_user_does_not_see_other_users_tickets(self):
        self._make_ticket(self.user_b, "Ticket B")
        self.client.login(username=self.user_a.username, password="testpass123")
        r = self.client.get(reverse("simulator:support"))
        tickets = list(r.context["tickets"])
        self.assertEqual(len(tickets), 0)

    def test_tickets_limited_to_10(self):
        for i in range(15):
            self._make_ticket(self.user_a, f"Ticket {i}")
        self.client.login(username=self.user_a.username, password="testpass123")
        r = self.client.get(reverse("simulator:support"))
        self.assertLessEqual(len(list(r.context["tickets"])), 10)


class SupportAdminTests(TestCase):
    """Admin registration and actions."""

    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="admin_su", password="adminpass", email="admin@test.com"
        )
        self.regular_user = make_user()
        self.client.login(username="admin_su", password="adminpass")

    def _make_ticket(self, status=SupportTicket.STATUS_OPEN):
        return SupportTicket.objects.create(
            user=self.regular_user,
            category=SupportTicket.CATEGORY_OTHER,
            subject="Test ticket",
            message="Need help.",
            status=status,
        )

    def test_support_ticket_in_admin_changelist(self):
        r = self.client.get("/admin/simulator/supportticket/")
        self.assertEqual(r.status_code, 200)

    def test_action_mark_pending(self):
        t = self._make_ticket(SupportTicket.STATUS_OPEN)
        self.client.post(
            "/admin/simulator/supportticket/",
            {"action": "mark_pending", "_selected_action": [t.pk]},
        )
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING)

    def test_action_mark_resolved(self):
        t = self._make_ticket(SupportTicket.STATUS_OPEN)
        self.client.post(
            "/admin/simulator/supportticket/",
            {"action": "mark_resolved", "_selected_action": [t.pk]},
        )
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_RESOLVED)
        self.assertIsNotNone(t.resolved_at)

    def test_action_mark_closed(self):
        t = self._make_ticket(SupportTicket.STATUS_OPEN)
        self.client.post(
            "/admin/simulator/supportticket/",
            {"action": "mark_closed", "_selected_action": [t.pk]},
        )
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_CLOSED)


class SupportSidebarTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.login(username=self.user.username, password="testpass123")

    def test_sidebar_contains_soporte_link(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertContains(r, "Soporte")

    def test_sidebar_soporte_link_url(self):
        r = self.client.get(reverse("simulator:support"))
        self.assertContains(r, reverse("simulator:support"))
