# simulator/tests/test_customer_support_01a_data_foundation.py
"""
CUSTOMER-SUPPORT-01A — data foundation only.

No view/admin/UI behavior is exercised here (none exists yet for the
new models) — these are pure model-level tests proving the additive
schema (8 new SupportTicket fields, SupportMessage, SupportAttachment,
legacy-PENDING status compatibility, and the attachment message/ticket
invariant from Design Lock Correction 1) behaves exactly as designed,
with zero visible change to any existing behavior.
"""
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import TestCase

from simulator.models import SupportAttachment, SupportMessage, SupportTicket
from simulator.models import _support_attachment_upload_path
from .factories import make_user


def _make_ticket(user=None, **kwargs):
    user = user or make_user()
    defaults = dict(
        category=SupportTicket.CATEGORY_OTHER,
        subject="Test ticket",
        message="Test message body",
    )
    defaults.update(kwargs)
    return SupportTicket.objects.create(user=user, **defaults)


def _make_message(ticket, author=None, **kwargs):
    author = author or ticket.user
    defaults = dict(
        author_role=SupportMessage.ROLE_CLIENT,
        body="hello",
        visibility=SupportMessage.VISIBILITY_CUSTOMER,
    )
    defaults.update(kwargs)
    return SupportMessage.objects.create(ticket=ticket, author=author, **defaults)


class SupportTicketExistingFieldsPreservedTests(TestCase):
    def test_defaults_intact(self):
        ticket = _make_ticket()
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        self.assertEqual(ticket.priority, SupportTicket.PRIORITY_NORMAL)
        self.assertEqual(ticket.admin_note, "")
        self.assertIsNone(ticket.resolved_at)
        self.assertIsNotNone(ticket.created_at)
        self.assertIsNotNone(ticket.updated_at)

    def test_existing_fields_preserved(self):
        user = make_user()
        ticket = _make_ticket(
            user=user, category=SupportTicket.CATEGORY_WITHDRAWAL,
            subject="Missing withdrawal", message="Where is my money",
            priority=SupportTicket.PRIORITY_URGENT,
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.user_id, user.pk)
        self.assertEqual(ticket.category, SupportTicket.CATEGORY_WITHDRAWAL)
        self.assertEqual(ticket.subject, "Missing withdrawal")
        self.assertEqual(ticket.message, "Where is my money")
        self.assertEqual(ticket.priority, SupportTicket.PRIORITY_URGENT)


class SupportTicketNewFieldsTests(TestCase):
    def test_new_fields_default_null_or_false(self):
        ticket = _make_ticket()
        self.assertIsNone(ticket.assigned_to)
        self.assertIsNone(ticket.assigned_at)
        self.assertFalse(ticket.escalated_to_ops)
        self.assertIsNone(ticket.escalated_at)
        self.assertIsNone(ticket.escalated_by)
        self.assertEqual(ticket.escalation_reason, "")
        self.assertIsNone(ticket.first_response_at)
        self.assertIsNone(ticket.closed_at)

    def test_new_fields_nullable_and_settable(self):
        from django.utils import timezone
        owner = make_user()
        ticket = _make_ticket()
        now = timezone.now()
        ticket.assigned_to = owner
        ticket.assigned_at = now
        ticket.escalated_to_ops = True
        ticket.escalated_at = now
        ticket.escalated_by = owner
        ticket.escalation_reason = "suspected fraud"
        ticket.first_response_at = now
        ticket.closed_at = now
        ticket.full_clean()
        ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, owner.pk)
        self.assertEqual(ticket.escalated_by_id, owner.pk)
        self.assertTrue(ticket.escalated_to_ops)
        self.assertEqual(ticket.escalation_reason, "suspected fraud")

    def test_assigned_to_set_null_on_user_delete(self):
        agent = make_user()
        ticket = _make_ticket()
        ticket.assigned_to = agent
        ticket.save(update_fields=["assigned_to"])
        agent.delete()
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to)

    def test_escalated_by_set_null_on_user_delete(self):
        actor = make_user()
        ticket = _make_ticket()
        ticket.escalated_by = actor
        ticket.save(update_fields=["escalated_by"])
        actor.delete()
        ticket.refresh_from_db()
        self.assertIsNone(ticket.escalated_by)


class SupportTicketStatusCompatibilityTests(TestCase):
    def test_status_choices_include_legacy_pending_and_new_values(self):
        keys = {k for k, _ in SupportTicket.STATUS_CHOICES}
        self.assertEqual(
            keys,
            {
                SupportTicket.STATUS_OPEN,
                SupportTicket.STATUS_PENDING,
                SupportTicket.STATUS_PENDING_CUSTOMER,
                SupportTicket.STATUS_PENDING_SUPPORT,
                SupportTicket.STATUS_ESCALATED,
                SupportTicket.STATUS_RESOLVED,
                SupportTicket.STATUS_CLOSED,
            },
        )

    def test_legacy_pending_remains_a_valid_status(self):
        ticket = _make_ticket(status=SupportTicket.STATUS_PENDING)
        ticket.full_clean()
        ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.STATUS_PENDING)

    def test_default_new_ticket_status_is_open_not_pending(self):
        ticket = _make_ticket()
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        self.assertNotEqual(ticket.status, SupportTicket.STATUS_PENDING)

    def test_pending_equivalent_statuses_helper(self):
        self.assertEqual(
            SupportTicket.PENDING_EQUIVALENT_STATUSES,
            frozenset({SupportTicket.STATUS_PENDING, SupportTicket.STATUS_PENDING_SUPPORT}),
        )


class SupportViewNeverCreatesPendingTests(TestCase):
    def test_support_view_post_creates_open_never_pending(self):
        user = make_user()
        self.client.force_login(user)
        self.client.post("/support/", {
            "category": SupportTicket.CATEGORY_OTHER,
            "subject": "Test",
            "message": "Test message",
        })
        ticket = SupportTicket.objects.filter(user=user).latest("created_at")
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        self.assertNotEqual(ticket.status, SupportTicket.STATUS_PENDING)


class SupportMessageTests(TestCase):
    def setUp(self):
        self.ticket = _make_ticket()

    def test_create_customer_visible(self):
        msg = _make_message(self.ticket, visibility=SupportMessage.VISIBILITY_CUSTOMER)
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_CUSTOMER)

    def test_create_internal(self):
        agent = make_user()
        msg = _make_message(
            self.ticket, author=agent, author_role=SupportMessage.ROLE_SUPPORT,
            visibility=SupportMessage.VISIBILITY_INTERNAL, body="internal only",
        )
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_INTERNAL)

    def test_author_role_preserved_as_snapshot_string(self):
        agent = make_user()
        msg = _make_message(self.ticket, author=agent, author_role=SupportMessage.ROLE_OPS)
        msg.refresh_from_db()
        self.assertEqual(msg.author_role, "OPS")
        # Snapshot, not derived: it stays "OPS" even though nothing in
        # this codebase's permission_levels.py has been consulted to
        # produce or re-validate it after the fact.

    def test_author_protect_blocks_delete(self):
        agent = make_user()
        _make_message(self.ticket, author=agent)
        with self.assertRaises(ProtectedError):
            agent.delete()

    def test_cascade_delete_when_ticket_deleted(self):
        msg = _make_message(self.ticket)
        ticket_id = self.ticket.id
        self.ticket.delete()
        self.assertFalse(SupportMessage.objects.filter(pk=msg.pk).exists())
        self.assertFalse(SupportTicket.objects.filter(pk=ticket_id).exists())


class SupportAttachmentInvariantTests(TestCase):
    """Design Lock Correction 1 §2 — the message/ticket invariant."""

    def setUp(self):
        self.ticket_a = _make_ticket()
        self.ticket_b = _make_ticket()
        self.uploader = make_user()

    def _attachment(self, ticket, message=None):
        return SupportAttachment(
            ticket=ticket, message=message, uploaded_by=self.uploader,
            file=SimpleUploadedFile("evidence.png", b"fake-bytes"),
            filename="evidence.png", content_type="image/png", size_bytes=10,
        )

    def test_same_ticket_message_accepted(self):
        msg = _make_message(self.ticket_a)
        att = self._attachment(self.ticket_a, message=msg)
        att.full_clean()  # must not raise
        att.save()
        self.assertEqual(att.message_id, msg.pk)

    def test_cross_ticket_message_rejected(self):
        msg_on_a = _make_message(self.ticket_a)
        att_on_b = self._attachment(self.ticket_b, message=msg_on_a)
        with self.assertRaises(ValidationError):
            att_on_b.full_clean()

    def test_message_none_accepted(self):
        att = self._attachment(self.ticket_a, message=None)
        att.full_clean()  # must not raise
        att.save()
        self.assertIsNone(att.message_id)


class SupportAttachmentFKBehaviorTests(TestCase):
    def setUp(self):
        self.ticket = _make_ticket()
        self.uploader = make_user()

    def _save_attachment(self, message=None):
        att = SupportAttachment(
            ticket=self.ticket, message=message, uploaded_by=self.uploader,
            file=SimpleUploadedFile("doc.pdf", b"%PDF-fake"),
            filename="doc.pdf", content_type="application/pdf", size_bytes=9,
        )
        att.full_clean()
        att.save()
        return att

    def test_uploaded_by_protect_blocks_delete(self):
        self._save_attachment()
        with self.assertRaises(ProtectedError):
            self.uploader.delete()

    def test_message_set_null_on_message_delete(self):
        msg = _make_message(self.ticket)
        att = self._save_attachment(message=msg)
        msg.delete()
        att.refresh_from_db()
        self.assertIsNone(att.message_id)
        self.assertTrue(SupportAttachment.objects.filter(pk=att.pk).exists())

    def test_ticket_cascade_deletes_attachment(self):
        att = self._save_attachment()
        att_pk = att.pk
        self.ticket.delete()
        self.assertFalse(SupportAttachment.objects.filter(pk=att_pk).exists())

    def test_filename_metadata_independent_from_storage_filename(self):
        att = self._save_attachment()
        self.assertEqual(att.filename, "doc.pdf")
        # The actual stored file name is a generated uuid, never the
        # original client-supplied name (path traversal / collision
        # avoidance) — the two must NOT match.
        self.assertNotIn("doc.pdf", att.file.name)


class SupportAttachmentUploadPathHelperTests(TestCase):
    def test_path_is_scoped_to_ticket_and_deterministic_shape(self):
        ticket = _make_ticket()
        fake_instance = SupportAttachment(ticket=ticket)
        path = _support_attachment_upload_path(fake_instance, "report.pdf")
        self.assertTrue(path.startswith(f"support_attachments/{ticket.id}/"))
        self.assertTrue(path.endswith(".pdf"))

    def test_path_is_unique_per_call(self):
        ticket = _make_ticket()
        fake_instance = SupportAttachment(ticket=ticket)
        p1 = _support_attachment_upload_path(fake_instance, "same_name.png")
        p2 = _support_attachment_upload_path(fake_instance, "same_name.png")
        self.assertNotEqual(p1, p2)

    def test_path_never_embeds_original_filename(self):
        ticket = _make_ticket()
        fake_instance = SupportAttachment(ticket=ticket)
        path = _support_attachment_upload_path(fake_instance, "sensitive customer name.jpg")
        self.assertNotIn("sensitive", path)
        self.assertNotIn("customer", path)
