# simulator/tests/test_kyc_emails.py
"""
KYC approval/rejection email notifications.

Covers:
  1.  Approving a pending KYC queues one email to the profile owner.
  2.  Rejecting a pending KYC queues one email to the profile owner.
  3.  Approval email subject contains "Money Brokers".
  4.  Approval email body mentions "aprobado" and does not include document paths.
  5.  Rejection email subject contains "Money Brokers".
  6.  Rejection email body includes rejection_reason when set.
  7.  Rejection email body omits reason line when rejection_reason is blank.
  8.  If email raises, admin approve action still completes (status updated).
  9.  If email raises, admin reject action still completes (status updated).
  10. Approval email body does not expose document file paths.
  11. Rejection email body does not expose document file paths.
  12. helper send_kyc_approved_email queues to user.email.
  13. helper send_kyc_rejected_email queues to user.email.
"""
from unittest.mock import patch, call

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import TestCase, RequestFactory
from django.utils import timezone

from simulator.models import KYCProfile
from simulator.tests.factories import make_user
from simulator.kyc_emails import send_kyc_approved_email, send_kyc_rejected_email

_PATCH_EMAIL = patch("simulator.tasks.send_email_async.delay")


def _make_pending_kyc(user, rejection_reason="") -> KYCProfile:
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status           = KYCProfile.STATUS_PENDING
    kyc.legal_name       = "Test User"
    kyc.country          = "Venezuela"
    kyc.document_type    = "national_id"
    kyc.rejection_reason = rejection_reason
    kyc.submitted_at     = timezone.now()
    kyc.save()
    return kyc


def _admin_request(admin_user):
    req          = RequestFactory().post("/admin/")
    req.user     = admin_user
    req._messages = CookieStorage(req)
    return req


# ── Admin action tests ────────────────────────────────────────────────────────

class KYCApproveEmailTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="kyc_user@test.com")
        self.kyc   = _make_pending_kyc(self.user)

    def _run_approve(self):
        from simulator.admin import KYCProfileAdmin
        ma = KYCProfileAdmin(KYCProfile, AdminSite())
        req = _admin_request(self.admin)
        qs = KYCProfile.objects.filter(pk=self.kyc.pk)
        ma.approve_kyc(req, qs)

    @_PATCH_EMAIL
    def test_approve_queues_email(self, mock_delay):
        self._run_approve()
        calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(calls), 1)

    @_PATCH_EMAIL
    def test_approve_updates_status(self, mock_delay):
        self._run_approve()
        self.kyc.refresh_from_db()
        self.assertEqual(self.kyc.status, KYCProfile.STATUS_APPROVED)

    @_PATCH_EMAIL
    def test_approve_email_subject_contains_brand(self, mock_delay):
        self._run_approve()
        subj = mock_delay.call_args.kwargs["subject"]
        self.assertIn("Money Brokers", subj)

    @_PATCH_EMAIL
    def test_approve_email_body_mentions_approved(self, mock_delay):
        self._run_approve()
        body = mock_delay.call_args.kwargs["message"]
        self.assertIn("aprobada", body)

    @_PATCH_EMAIL
    def test_approve_email_body_has_no_document_paths(self, mock_delay):
        self._run_approve()
        body = mock_delay.call_args.kwargs["message"]
        # upload_to paths per the model — these must never appear in emails
        self.assertNotIn("kyc/documents/", body)
        self.assertNotIn("kyc/selfies/", body)
        self.assertNotIn("document_front", body)
        self.assertNotIn("document_back", body)

    def test_approve_action_survives_email_failure(self):
        with patch("simulator.tasks.send_email_async.delay", side_effect=Exception("Celery down")):
            self._run_approve()
        self.kyc.refresh_from_db()
        self.assertEqual(self.kyc.status, KYCProfile.STATUS_APPROVED)


class KYCRejectEmailTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="admin2@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="kyc_reject@test.com")

    def _make_kyc(self, rejection_reason=""):
        return _make_pending_kyc(self.user, rejection_reason=rejection_reason)

    def _run_reject(self, kyc):
        from simulator.admin import KYCProfileAdmin
        ma = KYCProfileAdmin(KYCProfile, AdminSite())
        req = _admin_request(self.admin)
        qs = KYCProfile.objects.filter(pk=kyc.pk)
        ma.reject_kyc(req, qs)

    @_PATCH_EMAIL
    def test_reject_queues_email(self, mock_delay):
        kyc = self._make_kyc()
        self._run_reject(kyc)
        calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(calls), 1)

    @_PATCH_EMAIL
    def test_reject_updates_status(self, mock_delay):
        kyc = self._make_kyc()
        self._run_reject(kyc)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_REJECTED)

    @_PATCH_EMAIL
    def test_reject_email_subject_contains_brand(self, mock_delay):
        kyc = self._make_kyc()
        self._run_reject(kyc)
        subj = mock_delay.call_args.kwargs["subject"]
        self.assertIn("Money Brokers", subj)

    @_PATCH_EMAIL
    def test_reject_email_includes_rejection_reason(self, mock_delay):
        kyc = self._make_kyc(rejection_reason="Documento ilegible")
        self._run_reject(kyc)
        body = mock_delay.call_args.kwargs["message"]
        self.assertIn("Documento ilegible", body)

    @_PATCH_EMAIL
    def test_reject_email_omits_reason_line_when_blank(self, mock_delay):
        kyc = self._make_kyc(rejection_reason="")
        self._run_reject(kyc)
        body = mock_delay.call_args.kwargs["message"]
        self.assertNotIn("Motivo:", body)

    @_PATCH_EMAIL
    def test_reject_email_body_has_no_document_paths(self, mock_delay):
        kyc = self._make_kyc()
        self._run_reject(kyc)
        body = mock_delay.call_args.kwargs["message"]
        # upload_to paths per the model — these must never appear in emails
        self.assertNotIn("kyc/documents/", body)
        self.assertNotIn("kyc/selfies/", body)
        self.assertNotIn("document_front", body)
        self.assertNotIn("document_back", body)

    def test_reject_action_survives_email_failure(self):
        kyc = self._make_kyc()
        with patch("simulator.tasks.send_email_async.delay", side_effect=Exception("Celery down")):
            self._run_reject(kyc)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_REJECTED)


# ── Helper unit tests (call helper directly) ─────────────────────────────────

class KYCEmailHelperTests(TestCase):
    def setUp(self):
        self.user = make_user(email="helper@test.com")
        self.kyc  = _make_pending_kyc(self.user)

    @_PATCH_EMAIL
    def test_approved_helper_queues_to_user_email(self, mock_delay):
        send_kyc_approved_email(self.kyc)
        self.assertEqual(mock_delay.call_count, 1)
        self.assertIn(self.user.email, mock_delay.call_args.kwargs["recipient_list"])

    @_PATCH_EMAIL
    def test_rejected_helper_queues_to_user_email(self, mock_delay):
        send_kyc_rejected_email(self.kyc)
        self.assertEqual(mock_delay.call_count, 1)
        self.assertIn(self.user.email, mock_delay.call_args.kwargs["recipient_list"])

    @_PATCH_EMAIL
    def test_approved_subject_contains_brand(self, mock_delay):
        send_kyc_approved_email(self.kyc)
        self.assertIn("Money Brokers", mock_delay.call_args.kwargs["subject"])

    @_PATCH_EMAIL
    def test_rejected_subject_contains_brand(self, mock_delay):
        send_kyc_rejected_email(self.kyc)
        self.assertIn("Money Brokers", mock_delay.call_args.kwargs["subject"])

    @_PATCH_EMAIL
    def test_rejected_with_reason_includes_reason_in_body(self, mock_delay):
        self.kyc.rejection_reason = "Selfie borrosa"
        send_kyc_rejected_email(self.kyc)
        body = mock_delay.call_args.kwargs["message"]
        self.assertIn("Selfie borrosa", body)

    @_PATCH_EMAIL
    def test_rejected_without_reason_omits_motivo_line(self, mock_delay):
        self.kyc.rejection_reason = ""
        send_kyc_rejected_email(self.kyc)
        body = mock_delay.call_args.kwargs["message"]
        self.assertNotIn("Motivo:", body)
