# simulator/tests/test_support_emails.py
"""
Support ticket email notifications.

Covers:
  1.  Valid POST to /support/ queues a user confirmation email.
  2.  Email subject contains "Money Brokers".
  3.  Email body contains ticket id.
  4.  Email body contains the ticket subject.
  5.  Email body contains the ticket category display label.
  6.  If user email fails, the ticket is still created.
  7.  Admin notification is sent when SUPPORT_EMAIL is set.
  8.  Admin notification is NOT sent when SUPPORT_EMAIL is empty.
  9.  Admin email subject contains "Money Brokers".
  10. Admin email body contains the user's username.
  11. Admin email body contains the ticket category and priority.
  12. If admin email fails, the ticket is still created (user email also sent).
  13. Helper send_support_ticket_created_email queues to user.email.
  14. Helper send_support_ticket_admin_email queues to SUPPORT_EMAIL when set.
  15. Helper send_support_ticket_admin_email is silent when SUPPORT_EMAIL unset.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from simulator.models import SupportTicket
from simulator.support_emails import (
    send_support_ticket_created_email,
    send_support_ticket_admin_email,
)
from simulator.tests.factories import make_user

SUPPORT_URL  = "/support/"
_PATCH_EMAIL = patch("simulator.tasks.send_email_async.delay")

_VALID_POST = {
    "category": "deposit_issue",
    "subject":  "My deposit is missing",
    "message":  "I paid but balance is still 0.",
}


def _make_ticket(user, subject="Test ticket") -> SupportTicket:
    return SupportTicket.objects.create(
        user=user,
        category=SupportTicket.CATEGORY_DEPOSIT,
        subject=subject,
        message="Need help with my deposit.",
        status=SupportTicket.STATUS_OPEN,
        priority=SupportTicket.PRIORITY_NORMAL,
    )


# ── Integration: POST /support/ ───────────────────────────────────────────────

class SupportTicketCreatedEmailTests(TestCase):
    def setUp(self):
        self.user = make_user(email="ticket_user@test.com")
        self.client.force_login(self.user)

    @_PATCH_EMAIL
    def test_valid_post_queues_user_email(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(user_calls), 1)

    @_PATCH_EMAIL
    def test_user_email_subject_contains_brand(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertIn("Money Brokers", user_calls[0].kwargs["subject"])

    @_PATCH_EMAIL
    def test_user_email_body_contains_ticket_id(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        ticket = SupportTicket.objects.get(user=self.user)
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        body = user_calls[0].kwargs["message"]
        self.assertIn(str(ticket.id), body)

    @_PATCH_EMAIL
    def test_user_email_body_contains_subject(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        body = user_calls[0].kwargs["message"]
        self.assertIn("My deposit is missing", body)

    @_PATCH_EMAIL
    def test_user_email_body_contains_category(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        body = user_calls[0].kwargs["message"]
        # "deposit_issue" renders as "Problema con depósito"
        self.assertIn("depósito", body.lower())

    def test_email_failure_does_not_prevent_ticket_creation(self):
        with patch("simulator.tasks.send_email_async.delay",
                   side_effect=Exception("Celery down")):
            self.client.post(SUPPORT_URL, _VALID_POST)
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 1)


class SupportTicketAdminEmailTests(TestCase):
    def setUp(self):
        self.user = make_user(email="admin_notif@test.com")
        self.client.force_login(self.user)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="support@company.com")
    def test_admin_email_sent_when_support_email_set(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        admin_calls = [
            c for c in mock_email.call_args_list
            if "support@company.com" in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(admin_calls), 1)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="")
    def test_admin_email_not_sent_when_support_email_empty(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        admin_calls = [
            c for c in mock_email.call_args_list
            if "support@company.com" in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(admin_calls), 0)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="support@company.com")
    def test_admin_email_subject_contains_brand(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        admin_calls = [
            c for c in mock_email.call_args_list
            if "support@company.com" in c.kwargs.get("recipient_list", [])
        ]
        self.assertIn("Money Brokers", admin_calls[0].kwargs["subject"])

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="support@company.com")
    def test_admin_email_body_contains_username(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        admin_calls = [
            c for c in mock_email.call_args_list
            if "support@company.com" in c.kwargs.get("recipient_list", [])
        ]
        body = admin_calls[0].kwargs["message"]
        self.assertIn(self.user.username, body)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="support@company.com")
    def test_admin_email_body_contains_category_and_priority(self, mock_email):
        self.client.post(SUPPORT_URL, _VALID_POST)
        admin_calls = [
            c for c in mock_email.call_args_list
            if "support@company.com" in c.kwargs.get("recipient_list", [])
        ]
        body = admin_calls[0].kwargs["message"]
        self.assertIn("depósito", body.lower())   # category label
        self.assertIn("Normal", body)              # priority label

    def test_admin_email_failure_does_not_prevent_ticket_creation(self):
        with patch("simulator.tasks.send_email_async.delay",
                   side_effect=Exception("Celery down")):
            with override_settings(SUPPORT_EMAIL="support@company.com"):
                self.client.post(SUPPORT_URL, _VALID_POST)
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 1)


# ── Helper unit tests ─────────────────────────────────────────────────────────

class SupportEmailHelperTests(TestCase):
    def setUp(self):
        self.user   = make_user(email="helper_support@test.com")
        self.ticket = _make_ticket(self.user, "Cannot withdraw funds")

    @_PATCH_EMAIL
    def test_created_helper_queues_to_user_email(self, mock_email):
        send_support_ticket_created_email(self.ticket)
        self.assertEqual(mock_email.call_count, 1)
        self.assertIn(self.user.email, mock_email.call_args.kwargs["recipient_list"])

    @_PATCH_EMAIL
    def test_created_body_contains_ticket_id(self, mock_email):
        send_support_ticket_created_email(self.ticket)
        body = mock_email.call_args.kwargs["message"]
        self.assertIn(str(self.ticket.id), body)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="ops@company.com")
    def test_admin_helper_queues_to_support_email(self, mock_email):
        send_support_ticket_admin_email(self.ticket)
        self.assertEqual(mock_email.call_count, 1)
        self.assertIn("ops@company.com", mock_email.call_args.kwargs["recipient_list"])

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="")
    def test_admin_helper_silent_when_no_support_email(self, mock_email):
        send_support_ticket_admin_email(self.ticket)
        self.assertEqual(mock_email.call_count, 0)

    @_PATCH_EMAIL
    @override_settings(SUPPORT_EMAIL="ops@company.com")
    def test_admin_body_contains_user_email(self, mock_email):
        send_support_ticket_admin_email(self.ticket)
        body = mock_email.call_args.kwargs["message"]
        self.assertIn(self.user.email, body)
