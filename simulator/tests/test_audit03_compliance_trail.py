"""
simulator/tests/test_audit03_compliance_trail.py — AUDIT-03

Audits the Compliance audit trail integration added on top of AUDIT-01/02
(simulator/broker_audit.py): compliance.kyc_approved, compliance.kyc_rejected,
compliance.kyc_resubmitted, and the concurrency fix in
admin.py::approve_kyc()/reject_kyc() and views.py::kyc_view().

Conventions reused from test_kyc_emails.py: RequestFactory + CookieStorage
for admin action requests, KYCProfileAdmin instantiated directly (no HTTP),
send_email_async.delay mocked.

Scope: event shape, privacy whitelist, fail-open, bulk actions, the
non-pending-profile silent skip, deterministic race simulations (recheck
inside the transaction), and a PostgreSQL-only real-concurrency test that
is skipped cleanly on SQLite.
"""
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import connection
from django.test import TestCase, TransactionTestCase, RequestFactory
from django.utils import timezone

from simulator.admin import KYCProfileAdmin
from simulator.broker_audit import (
    ActorType,
    Category,
    EV_KYC_APPROVED,
    EV_KYC_REJECTED,
    EV_KYC_RESUBMITTED,
    KYC_REJECTION_REASON_MAX_LENGTH,
    Severity,
    events_for_user,
)
from simulator.models import BrokerAuditEvent, KYCProfile
from simulator.tests.factories import make_user

_PATCH_EMAIL = patch("simulator.tasks.send_email_async.delay")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — same pattern as test_kyc_emails.py
# ─────────────────────────────────────────────────────────────────────────────

def _make_pending_kyc(user, rejection_reason="") -> KYCProfile:
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status           = KYCProfile.STATUS_PENDING
    kyc.legal_name       = "Test User"
    kyc.country          = "Venezuela"
    kyc.document_type    = "national_id"
    kyc.document_number  = "V-12345678"
    kyc.rejection_reason = rejection_reason
    kyc.submitted_at     = timezone.now()
    kyc.save()
    return kyc


def _admin_request(admin_user):
    req           = RequestFactory().post("/admin/")
    req.user      = admin_user
    req._messages = CookieStorage(req)
    return req


def _run_approve(kyc_qs, admin_user):
    ma = KYCProfileAdmin(KYCProfile, AdminSite())
    ma.approve_kyc(_admin_request(admin_user), kyc_qs)


def _run_reject(kyc_qs, admin_user):
    ma = KYCProfileAdmin(KYCProfile, AdminSite())
    ma.reject_kyc(_admin_request(admin_user), kyc_qs)


# ─────────────────────────────────────────────────────────────────────────────
# Event shape — approval
# ─────────────────────────────────────────────────────────────────────────────

class KYCApprovalAuditTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_user@test.com")
        self.kyc   = _make_pending_kyc(self.user)

    @_PATCH_EMAIL
    def test_creates_exactly_one_event(self, _mock_delay):
        _run_approve(KYCProfile.objects.filter(pk=self.kyc.pk), self.admin)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED, user=self.user).count(), 1,
        )

    @_PATCH_EMAIL
    def test_event_shape(self, _mock_delay):
        _run_approve(KYCProfile.objects.filter(pk=self.kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_APPROVED, user=self.user)
        self.assertEqual(event.category, Category.COMPLIANCE)
        self.assertEqual(event.severity, Severity.INFO)
        self.assertEqual(event.actor_type, ActorType.STAFF)
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertEqual(event.user_id, self.user.pk)
        self.assertEqual(event.event_version, 1)
        self.assertIsNone(event.correlation_id)

    @_PATCH_EMAIL
    def test_metadata_shape(self, _mock_delay):
        _run_approve(KYCProfile.objects.filter(pk=self.kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_APPROVED, user=self.user)
        self.assertEqual(event.metadata, {
            "kyc_profile_id": self.kyc.pk,
            "status_before": "pending",
            "status_after": "approved",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Event shape — rejection (+ rejection_reason truncation)
# ─────────────────────────────────────────────────────────────────────────────

class KYCRejectionAuditTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_admin2@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_user2@test.com")

    @_PATCH_EMAIL
    def test_creates_exactly_one_event(self, _mock_delay):
        kyc = _make_pending_kyc(self.user, rejection_reason="Documento ilegible")
        _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_REJECTED, user=self.user).count(), 1,
        )

    @_PATCH_EMAIL
    def test_event_shape(self, _mock_delay):
        kyc = _make_pending_kyc(self.user, rejection_reason="Foto borrosa")
        _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_REJECTED, user=self.user)
        self.assertEqual(event.category, Category.COMPLIANCE)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertEqual(event.metadata["rejection_reason"], "Foto borrosa")
        self.assertEqual(event.metadata["status_before"], "pending")
        self.assertEqual(event.metadata["status_after"], "rejected")

    @_PATCH_EMAIL
    def test_rejection_reason_truncated_to_500(self, _mock_delay):
        long_reason = "x" * 900
        kyc = _make_pending_kyc(self.user, rejection_reason=long_reason)
        _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_REJECTED, user=self.user)
        self.assertEqual(len(event.metadata["rejection_reason"]), KYC_REJECTION_REASON_MAX_LENGTH)
        self.assertEqual(KYC_REJECTION_REASON_MAX_LENGTH, 500)

    @_PATCH_EMAIL
    def test_blank_rejection_reason_is_empty_string_not_missing(self, _mock_delay):
        kyc = _make_pending_kyc(self.user, rejection_reason="")
        _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_REJECTED, user=self.user)
        self.assertEqual(event.metadata["rejection_reason"], "")


# ─────────────────────────────────────────────────────────────────────────────
# Resubmit — only on REJECTED -> PENDING
# ─────────────────────────────────────────────────────────────────────────────

KYC_URL = "/kyc/"


def _fake_image():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("front.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 20, content_type="image/jpeg")


def _valid_post():
    return {
        "legal_name": "Juan Pérez", "country": "Venezuela",
        "document_type": "national_id", "document_number": "",
        "document_front": _fake_image(),
    }


class KYCResubmitAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a03_resubmit_user")
        self.client.force_login(self.user)

    def _set_rejected(self, reason="Foto borrosa"):
        reviewer = make_user(username="a03_reviewer", is_staff=True)
        kyc, _ = KYCProfile.objects.get_or_create(user=self.user)
        kyc.status = KYCProfile.STATUS_REJECTED
        kyc.reviewed_at = timezone.now()
        kyc.reviewed_by = reviewer
        kyc.rejection_reason = reason
        kyc.save()
        return kyc, reviewer

    def test_resubmit_after_rejection_creates_one_event(self):
        self._set_rejected()
        self.client.post(KYC_URL, _valid_post())
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_RESUBMITTED, user=self.user).count(), 1,
        )

    def test_resubmit_event_shape(self):
        self._set_rejected()
        self.client.post(KYC_URL, _valid_post())
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_RESUBMITTED, user=self.user)
        self.assertEqual(event.category, Category.COMPLIANCE)
        self.assertEqual(event.actor_type, ActorType.TRADER)
        self.assertIsNone(event.actor_id)  # self-service — no staff actor
        self.assertEqual(event.metadata, {
            "kyc_profile_id": event.metadata["kyc_profile_id"],
            "status_before": "rejected",
            "status_after": "pending",
        })

    def test_first_ever_submission_creates_no_event(self):
        """NOT_STARTED -> PENDING is the first submission, nothing to preserve."""
        self.client.post(KYC_URL, _valid_post())
        kyc = KYCProfile.objects.get(user=self.user)
        self.assertEqual(kyc.status, KYCProfile.STATUS_PENDING)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(category=Category.COMPLIANCE, user=self.user).count(), 0,
        )

    def test_prior_rejection_event_survives_the_wipe(self):
        """
        The whole point of AUDIT-03: kyc.reviewed_at/reviewed_by/
        rejection_reason are wiped by the resubmit (pre-existing, still true
        after AUDIT-03 — see test_kyc_ui.py::test_rejected_post_clears_
        review_fields), but the EARLIER compliance.kyc_rejected event (not
        created by this test — simulating that it was created when the
        rejection actually happened) is never touched by the resubmit.
        """
        kyc, reviewer = self._set_rejected(reason="Documento vencido")
        from simulator import broker_audit as _audit
        _audit.record_compliance_event(
            event_type=_audit.EV_KYC_REJECTED, severity=_audit.Severity.WARNING,
            actor_id=reviewer.pk, user=self.user,
            description="prior rejection", metadata={
                "kyc_profile_id": kyc.pk, "status_before": "pending",
                "status_after": "rejected", "rejection_reason": "Documento vencido",
            },
        )
        self.client.post(KYC_URL, _valid_post())

        kyc.refresh_from_db()
        self.assertIsNone(kyc.reviewed_at)          # wiped from KYCProfile, as before
        self.assertIsNone(kyc.reviewed_by)
        self.assertEqual(kyc.rejection_reason, "")

        rejected_event = BrokerAuditEvent.objects.get(event_type=EV_KYC_REJECTED, user=self.user)
        self.assertEqual(rejected_event.metadata["rejection_reason"], "Documento vencido")
        self.assertEqual(rejected_event.actor_id, reviewer.pk)  # untouched, permanent


# ─────────────────────────────────────────────────────────────────────────────
# Bulk actions
# ─────────────────────────────────────────────────────────────────────────────

class KYCBulkActionAuditTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_bulk_admin@test.com", is_staff=True, is_superuser=True)

    @_PATCH_EMAIL
    def test_bulk_approve_creates_one_event_per_profile(self, _mock_delay):
        users = [make_user(email=f"a03_bulk_{i}@test.com") for i in range(3)]
        kycs  = [_make_pending_kyc(u) for u in users]
        qs    = KYCProfile.objects.filter(pk__in=[k.pk for k in kycs])
        _run_approve(qs, self.admin)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 3,
        )
        self.assertEqual(_mock_delay.call_count, 3)

    @_PATCH_EMAIL
    def test_non_pending_profile_in_selection_is_silently_skipped(self, _mock_delay):
        already_approved_user = make_user(email="a03_already@test.com")
        already = _make_pending_kyc(already_approved_user)
        already.status = KYCProfile.STATUS_APPROVED
        already.save()

        pending_user = make_user(email="a03_pending@test.com")
        pending = _make_pending_kyc(pending_user)

        qs = KYCProfile.objects.filter(pk__in=[already.pk, pending.pk])
        _run_approve(qs, self.admin)

        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED, user=pending_user).count(), 1,
        )
        self.assertEqual(_mock_delay.call_count, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic race simulations — no real threads, work identically on
# SQLite and PostgreSQL. These prove the RECHECK LOGIC is correct: the
# action re-reads status from the DB inside its own transaction/lock and
# skips if it no longer matches, regardless of what the initial admin
# selection saw. They do NOT prove that a real simultaneous request is
# physically blocked by the DB lock while waiting — that guarantee is
# PostgreSQL-specific and is verified separately in
# PostgresRealConcurrencyTests below, gated on connection.vendor.
# ─────────────────────────────────────────────────────────────────────────────

class KYCDeterministicRaceTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_race_admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_race_user@test.com")

    @_PATCH_EMAIL
    def test_approve_recheck_skips_when_already_approved(self, _mock_delay):
        """
        Simulates: admin A's browser selected this profile while PENDING,
        but by the time approve_kyc() actually runs, another process has
        already moved it to APPROVED (e.g. a concurrent reject_kyc/approve_kyc
        that committed first). approve_kyc()'s queryset is re-evaluated
        fresh inside the loop — not cached from before this "concurrent"
        change — so it must see the current status and skip.
        """
        kyc = _make_pending_kyc(self.user)
        qs = KYCProfile.objects.filter(pk=kyc.pk)  # built while PENDING
        KYCProfile.objects.filter(pk=kyc.pk).update(
            status=KYCProfile.STATUS_APPROVED, reviewed_by=self.admin, reviewed_at=timezone.now(),
        )  # "another process" already approved it, no event for this simulated step

        _run_approve(qs, self.admin)

        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 0)
        _mock_delay.assert_not_called()

    @_PATCH_EMAIL
    def test_reject_recheck_skips_when_already_rejected(self, _mock_delay):
        kyc = _make_pending_kyc(self.user)
        qs = KYCProfile.objects.filter(pk=kyc.pk)
        KYCProfile.objects.filter(pk=kyc.pk).update(
            status=KYCProfile.STATUS_REJECTED, reviewed_by=self.admin, reviewed_at=timezone.now(),
        )

        _run_reject(qs, self.admin)

        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_REJECTED).count(), 0)
        _mock_delay.assert_not_called()

    @_PATCH_EMAIL
    def test_approve_loses_to_a_concurrent_reject(self, _mock_delay):
        """approve vs. reject concurrently — whichever commits first wins;
        the other's recheck must see the non-PENDING status and skip."""
        kyc = _make_pending_kyc(self.user)
        qs = KYCProfile.objects.filter(pk=kyc.pk)  # same selection reused for both actions

        _run_reject(qs, self.admin)   # "wins" the race — completes first
        _run_approve(qs, self.admin)  # "loses" — sees REJECTED, skips

        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_REJECTED).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 0)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_REJECTED)
        self.assertEqual(_mock_delay.call_count, 1)  # only the reject email

    @_PATCH_EMAIL
    def test_reject_loses_to_a_concurrent_approve(self, _mock_delay):
        kyc = _make_pending_kyc(self.user)
        qs = KYCProfile.objects.filter(pk=kyc.pk)

        _run_approve(qs, self.admin)
        _run_reject(qs, self.admin)

        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_REJECTED).count(), 0)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_APPROVED)
        self.assertEqual(_mock_delay.call_count, 1)


class KYCResubmitRaceTests(TestCase):
    """
    Deterministic (sequential) proof that a second resubmit attempt on an
    already-transitioned profile creates no second event — exercising the
    view's outer editable gate. A separate, mock-based test below exercises
    the INNER recheck (select_for_update().get() re-reading a status that
    changed after the outer, unlocked read) specifically.
    """
    def setUp(self):
        self.user = make_user(username="a03_double_resubmit_user")
        self.client.force_login(self.user)
        kyc, _ = KYCProfile.objects.get_or_create(user=self.user)
        kyc.status = KYCProfile.STATUS_REJECTED
        kyc.reviewed_at = timezone.now()
        kyc.reviewed_by = make_user(username="a03_double_reviewer", is_staff=True)
        kyc.rejection_reason = "Foto borrosa"
        kyc.save()

    def test_second_sequential_post_creates_no_second_event(self):
        self.client.post(KYC_URL, _valid_post())  # first resubmit — succeeds
        self.client.post(KYC_URL, _valid_post())  # second — status is now PENDING, not editable
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_RESUBMITTED, user=self.user).count(), 1,
        )

    def test_inner_recheck_skips_when_status_changed_between_outer_read_and_lock(self):
        """
        Simulates the exact race the outer `editable` gate alone cannot
        catch: get_or_create() returns a REJECTED snapshot, but by the time
        the view acquires select_for_update(), the row has already moved to
        PENDING (a concurrent request/admin action committed first). The
        mock only affects the outer, unlocked get_or_create() call — the
        inner select_for_update().get() hits the real DB and must see the
        true, already-changed state.
        """
        stale_kyc = KYCProfile.objects.get(user=self.user)  # REJECTED snapshot
        KYCProfile.objects.filter(user=self.user).update(status=KYCProfile.STATUS_PENDING)

        with patch(
            "simulator.views.KYCProfile.objects.get_or_create",
            return_value=(stale_kyc, False),
        ):
            resp = self.client.post(KYC_URL, _valid_post())

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_KYC_RESUBMITTED, user=self.user).count(), 0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Privacy whitelist
# ─────────────────────────────────────────────────────────────────────────────

class KYCPrivacyWhitelistTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_priv_admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_priv_user@test.com")

    _FORBIDDEN_SUBSTRINGS = (
        "document_front", "document_back", "selfie", "kyc/documents/",
        "kyc/selfies/", "V-12345678", "Test User", "Venezuela",
        "national_id", "passport",
    )

    @_PATCH_EMAIL
    def test_approved_metadata_whitelist(self, _mock_delay):
        kyc = _make_pending_kyc(self.user)
        _run_approve(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_APPROVED, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"kyc_profile_id", "status_before", "status_after"})
        self._assert_no_forbidden_data(event)

    @_PATCH_EMAIL
    def test_rejected_metadata_whitelist(self, _mock_delay):
        kyc = _make_pending_kyc(self.user, rejection_reason="Foto borrosa")
        _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_REJECTED, user=self.user)
        self.assertEqual(
            set(event.metadata.keys()),
            {"kyc_profile_id", "status_before", "status_after", "rejection_reason"},
        )
        self._assert_no_forbidden_data(event)

    def test_resubmitted_metadata_whitelist(self):
        self.client_login_and_resubmit()
        event = BrokerAuditEvent.objects.get(event_type=EV_KYC_RESUBMITTED, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"kyc_profile_id", "status_before", "status_after"})
        self._assert_no_forbidden_data(event)

    def client_login_and_resubmit(self):
        from django.test import Client
        c = Client()
        c.force_login(self.user)
        kyc, _ = KYCProfile.objects.get_or_create(user=self.user)
        kyc.status = KYCProfile.STATUS_REJECTED
        kyc.reviewed_at = timezone.now()
        kyc.reviewed_by = self.admin
        kyc.rejection_reason = "Documento vencido"
        kyc.save()
        c.post(KYC_URL, _valid_post())

    def _assert_no_forbidden_data(self, event):
        blob = str(event.metadata) + event.description
        for forbidden in self._FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, blob)


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open
# ─────────────────────────────────────────────────────────────────────────────

class KYCFailOpenTests(TestCase):
    def setUp(self):
        self.admin = make_user(email="a03_fo_admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_fo_user@test.com")

    _BOOM = patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom"))

    @_PATCH_EMAIL
    def test_approve_completes_despite_audit_failure(self, _mock_delay):
        kyc = _make_pending_kyc(self.user)
        with self._BOOM:
            _run_approve(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_APPROVED)
        _mock_delay.assert_called_once()

    @_PATCH_EMAIL
    def test_reject_completes_despite_audit_failure(self, _mock_delay):
        kyc = _make_pending_kyc(self.user)
        with self._BOOM:
            _run_reject(KYCProfile.objects.filter(pk=kyc.pk), self.admin)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_REJECTED)
        _mock_delay.assert_called_once()

    def test_resubmit_completes_despite_audit_failure(self):
        user = make_user(username="a03_fo_resubmit_user")
        client_ = self.client
        client_.force_login(user)
        kyc, _ = KYCProfile.objects.get_or_create(user=user)
        kyc.status = KYCProfile.STATUS_REJECTED
        kyc.reviewed_at = timezone.now()
        kyc.reviewed_by = self.admin
        kyc.rejection_reason = "x"
        kyc.save()

        with self._BOOM:
            resp = client_.post(KYC_URL, _valid_post())

        self.assertEqual(resp.status_code, 302)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_PENDING)
        self.assertIsNone(kyc.reviewed_at)


# ─────────────────────────────────────────────────────────────────────────────
# events_for_user()
# ─────────────────────────────────────────────────────────────────────────────

class EventsForUserTests(TestCase):
    @_PATCH_EMAIL
    def test_returns_events_for_the_given_user_only(self, _mock_delay):
        admin = make_user(email="a03_efu_admin@test.com", is_staff=True, is_superuser=True)
        user_a = make_user(email="a03_efu_a@test.com")
        user_b = make_user(email="a03_efu_b@test.com")
        kyc_a = _make_pending_kyc(user_a)
        kyc_b = _make_pending_kyc(user_b)

        _run_approve(KYCProfile.objects.filter(pk=kyc_a.pk), admin)
        _run_reject(KYCProfile.objects.filter(pk=kyc_b.pk), admin)

        events_a = events_for_user(user_a.pk)
        self.assertEqual(len(events_a), 1)
        self.assertEqual(events_a[0].event_type, EV_KYC_APPROVED)

        events_b = events_for_user(user_b.pk)
        self.assertEqual(len(events_b), 1)
        self.assertEqual(events_b[0].event_type, EV_KYC_REJECTED)

    def test_empty_for_user_with_no_events(self):
        user = make_user(email="a03_efu_none@test.com")
        self.assertEqual(events_for_user(user.pk), [])


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL-only real concurrency test — skipped cleanly on SQLite.
#
# IMPORTANT — honesty about what SQLite does and does not prove:
# Django's SQLite backend accepts .select_for_update() as a no-op (it does
# not raise, but it does not take a real row lock either) — the tests above
# verify the RECHECK LOGIC is correct (the code re-reads and correctly
# skips a stale transition) deterministically on any backend, including
# SQLite. They do NOT prove that a real second transaction would have been
# forced to WAIT for the first one's lock — SQLite provides no such
# guarantee to observe. Only this test, gated on connection.vendor ==
# "postgresql", exercises the actual row lock.
# ─────────────────────────────────────────────────────────────────────────────

class PostgresRealConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest(
                "Real select_for_update() row-locking is only meaningfully "
                "verifiable on PostgreSQL — SQLite does not take a real row "
                "lock, so this test would not prove anything there."
            )
        self.admin = make_user(email="a03_pg_admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="a03_pg_user@test.com")

    @_PATCH_EMAIL
    def test_concurrent_approve_only_one_wins(self, _mock_delay):
        import threading

        kyc = _make_pending_kyc(self.user)
        qs = KYCProfile.objects.filter(pk=kyc.pk)
        barrier = threading.Barrier(2)
        errors = []

        def _worker():
            try:
                barrier.wait(timeout=5)
                _run_approve(qs, self.admin)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [])
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_KYC_APPROVED).count(), 1)
        self.assertEqual(_mock_delay.call_count, 1)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, KYCProfile.STATUS_APPROVED)
