# simulator/tests/test_o3e4_treasury_cancel_workflow_end_to_end.py
"""
Microbloque O.3e-4 — Treasury Cancel Workflow, end-to-end verification.

This file does NOT re-test what O.3e-1/O.3e-2/O.3e-3 already cover in
isolation (event catalog, individual service unit tests, individual
admin-view unit tests, AST-based delegation proofs). It instead drives
full realistic journeys through the actual admin URLs — real Django
test Client against the real URLconf, permissions, templates, and
messages framework — for the 7 mandatory scenarios of the O.3e
checkpoint, and re-affirms the required invariants explicitly, once
each, as a single point of reference for what this block was required
to confirm.

Every mutation here goes through the real admin views (never by calling
cancel_treasury_request() directly) except where a scenario explicitly
needs a pre-existing row in a given status/relationship shape that only
a direct .objects.create() can set up cheaply (mirroring the precedent
in test_o3c5c_treasury_admin_end_to_end.py).
"""
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)

from .factories import make_user, make_wallet


# ─────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────

def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _make_submitter(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_submit_treasury_request")


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _submit_url():
    return reverse("admin:treasury_request_new")


def _cancel_url(pk):
    return reverse("admin:treasury_request_cancel", args=[pk])


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _submit_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "50.00",
        "reason": "O.3e-4 end-to-end test",
    }
    data.update(overrides)
    return data


def _make_pending_request(wallet=None, requested_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet,
        "amount": Decimal("40.00"),
        "reason": "O.3e-4 end-to-end test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


# ─────────────────────────────────────────────
# Scenario 1 — requested_by cancels their own PENDING request (self-withdrawal)
# ─────────────────────────────────────────────

class SelfWithdrawalEndToEndTests(TestCase):

    def setUp(self):
        self.requester = _make_submitter(username="o3e4_s1_requester")
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))
        self.client = Client()

    def test_self_withdrawal_full_journey(self):
        self.client.force_login(self.requester)
        resp = self.client.post(_submit_url(), data=_submit_data(self.wallet, amount="30.00"))
        tor = TreasuryOperationRequest.objects.get()
        self.assertRedirects(resp, _change_url(tor.pk))
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

        # Cancel button visible on own PENDING request.
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Cancel Request →")

        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "self-withdrawal")

        resp = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(f"#{tor.pk}" in m and "cancelada" in m for m in msgs))

        tor.refresh_from_db()
        self.wallet.refresh_from_db()

        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(tor.cancelled_at)

        # No financial side effects whatsoever.
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before)
        self.assertEqual(InternalTransfer.objects.count(), 0)
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancelled_by_role"], "requester")
        bae = BrokerAuditEvent.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(bae.severity, "INFO")

        # Cancel button gone now that it's terminal.
        resp = self.client.get(_change_url(tor.pk))
        self.assertNotContains(resp, "Cancel Request →")


# ─────────────────────────────────────────────
# Scenario 2 — supervisor cancels administratively
# ─────────────────────────────────────────────

class AdministrativeCancellationEndToEndTests(TestCase):

    def setUp(self):
        self.requester = make_user(username="o3e4_s2_requester")
        self.reviewer = _make_reviewer(username="o3e4_s2_reviewer")
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))
        self.client = Client()

    def test_administrative_cancellation_full_journey(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)

        self.client.force_login(self.reviewer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Cancel Request →")

        resp = self.client.get(_cancel_url(tor.pk))
        self.assertContains(resp, "administrative")

        resp = self.client.post(
            _cancel_url(tor.pk), data={"cancellation_reason": "duplicate ticket"}, follow=True,
        )
        self.assertRedirects(resp, _change_url(tor.pk))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(tor.cancelled_at)

        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancelled_by_role"], "supervisor")
        self.assertEqual(row.detail["cancellation_reason"], "duplicate ticket")
        # Reason lives only in audit — never on the model.
        self.assertFalse(hasattr(tor, "cancellation_reason"))

        bae = BrokerAuditEvent.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(bae.severity, "WARNING")


# ─────────────────────────────────────────────
# Scenario 3 — user without permission
# ─────────────────────────────────────────────

class NoPermissionEndToEndTests(TestCase):

    def test_get_and_post_403_no_change(self):
        requester = make_user(username="o3e4_s3_requester")
        staff = make_user(username="o3e4_s3_staff", is_staff=True)
        tor = _make_pending_request(requested_by=requester)
        client = Client()
        client.force_login(staff)

        resp = client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

        resp = client.post(_cancel_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertIsNone(tor.cancelled_at)
        self.assertEqual(AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 0)


# ─────────────────────────────────────────────
# Scenario 4 — every non-PENDING status rejects cancellation
# ─────────────────────────────────────────────

class NonPendingStatusRejectionTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e4_s4_reviewer")
        self.other = make_user(username="o3e4_s4_other")
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_every_non_pending_status_rejects_cancellation(self):
        for status in (
            TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED,
            TreasuryOperationRequest.ST_EXECUTING,
            TreasuryOperationRequest.ST_EXECUTED,
            TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        ):
            with self.subTest(status=status):
                tor = _make_pending_request(requested_by=self.other, status=status)
                resp = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
                self.assertRedirects(resp, _change_url(tor.pk))
                msgs = [str(m) for m in resp.context["messages"]]
                self.assertTrue(any("ya no está pendiente" in m for m in msgs))
                tor.refresh_from_db()
                self.assertEqual(tor.status, status)  # unchanged
                self.assertIsNone(tor.cancelled_at)


# ─────────────────────────────────────────────
# Scenario 5 — double cancellation
# ─────────────────────────────────────────────

class DoubleCancellationTests(TestCase):

    def setUp(self):
        self.requester = _make_submitter(username="o3e4_s5_requester")
        self.reviewer = _make_reviewer(username="o3e4_s5_reviewer")
        self.wallet = make_wallet()
        self.client = Client()

    def test_second_cancellation_does_not_change_anything(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)

        self.client.force_login(self.requester)
        resp1 = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        cancelled_at_first = tor.cancelled_at
        msgs1 = [str(m) for m in resp1.context["messages"]]
        self.assertTrue(any("cancelada" in m for m in msgs1))

        # Second attempt, this time by a supervisor — request is already terminal.
        self.client.force_login(self.reviewer)
        resp2 = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
        self.assertRedirects(resp2, _change_url(tor.pk))
        msgs2 = [str(m) for m in resp2.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in msgs2))
        self.assertFalse(any("Traceback" in m or "Exception" in m for m in msgs2))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertEqual(tor.cancelled_at, cancelled_at_first)  # unchanged
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )


# ─────────────────────────────────────────────
# Scenario 6 — cancellation_reason handling
# ─────────────────────────────────────────────

class CancellationReasonHandlingTests(TestCase):

    def setUp(self):
        self.requester = _make_submitter(username="o3e4_s6_requester")
        self.client = Client()
        self.client.force_login(self.requester)

    def test_empty_reason_allowed(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_cancel_url(tor.pk), data={"cancellation_reason": ""})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancellation_reason"], "")

    def test_missing_reason_key_allowed(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_cancel_url(tor.pk), data={})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)

    def test_present_reason_lives_only_in_audit_never_on_model(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.post(
            _cancel_url(tor.pk), data={"cancellation_reason": "  changed my mind  "},
        )
        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancellation_reason"], "changed my mind")
        bae = BrokerAuditEvent.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(bae.metadata["cancellation_reason"], "changed my mind")

        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})
        self.assertFalse(hasattr(tor, "cancellation_reason"))
        field_names = {f.name for f in TreasuryOperationRequest._meta.fields}
        self.assertNotIn("cancellation_reason", field_names)


# ─────────────────────────────────────────────
# Scenario 7 — UI-level access control
# ─────────────────────────────────────────────

class UiAccessControlEndToEndTests(TestCase):

    def setUp(self):
        self.requester = _make_submitter(username="o3e4_s7_requester")
        self.reviewer = _make_reviewer(username="o3e4_s7_reviewer")
        self.no_perms = make_user(username="o3e4_s7_no_perms", is_staff=True)
        self.other = make_user(username="o3e4_s7_other")
        self.wallet = make_wallet()
        self.client = Client()

    def test_button_hidden_without_permission(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.other)
        _grant(self.no_perms, "view_treasuryoperationrequest")
        self.client.force_login(self.no_perms)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Cancel Request →")

    def test_button_visible_for_self_withdrawal_and_administrative(self):
        tor_self = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        self.client.force_login(self.requester)
        resp = self.client.get(_change_url(tor_self.pk))
        self.assertContains(resp, "Cancel Request →")

        tor_admin = _make_pending_request(wallet=self.wallet, requested_by=self.other)
        self.client.force_login(self.reviewer)
        resp = self.client.get(_change_url(tor_admin.pk))
        self.assertContains(resp, "Cancel Request →")

    def test_direct_url_without_permission_403(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.other)
        self.client.force_login(self.no_perms)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_object_404(self):
        self.client.force_login(self.reviewer)
        resp = self.client.get(_cancel_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_redirects_to_login(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.other)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_stale_state_warns_and_redirects(self):
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        self.client.force_login(self.requester)
        resp = self.client.get(_cancel_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in msgs))


# ─────────────────────────────────────────────
# Invariants — re-affirmed explicitly across both cancellation pipelines
# ─────────────────────────────────────────────

class InvariantsTests(TestCase):
    """
    These invariants are already exercised implicitly by the scenario
    classes above; this class asserts them explicitly, once each, as a
    single point of reference for what O.3e-4 was required to confirm.
    """

    def setUp(self):
        self.requester = _make_submitter(username="o3e4_inv_requester")
        self.reviewer = _make_reviewer(username="o3e4_inv_reviewer")
        self.wallet = make_wallet(initial_balance=Decimal("50.00"))
        self.client = Client()

    def test_wallet_and_wallet_transaction_untouched(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        self.client.force_login(self.requester)
        self.client.post(_cancel_url(tor.pk), data={})

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before)

    def test_no_internal_transfer_ever(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        self.client.force_login(self.requester)
        self.client.post(_cancel_url(tor.pk), data={})
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_admin_view_never_imports_wallet_ledger_or_execution_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(TreasuryOperationRequestAdmin.treasury_request_cancel_view)),
        )
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
                imported.update(a.name for a in node.names)
        self.assertNotIn("wallet_ledger", imported)
        self.assertNotIn("funded_payouts", imported)
        self.assertNotIn("execute_treasury_request", imported)
        self.assertNotIn("mark_treasury_execution_failed", imported)

    def test_service_never_imports_wallet_ledger_or_execution_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.treasury_requests import cancel_treasury_request

        tree = ast.parse(textwrap.dedent(inspect.getsource(cancel_treasury_request)))
        imported = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    calls.add(name)
        self.assertNotIn("wallet_ledger", imported)
        self.assertNotIn("credit_wallet", calls)
        self.assertNotIn("debit_wallet", calls)
        self.assertNotIn("execute_treasury_request", calls)
        self.assertNotIn("mark_treasury_execution_failed", calls)

    def test_approved_by_rejected_by_executed_by_never_touched_by_cancellation(self):
        approver = make_user(username="o3e4_inv_approver")
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        self.client.force_login(self.reviewer)
        self.client.post(_cancel_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertIsNone(tor.approved_by_id)
        self.assertIsNone(tor.rejected_by_id)
        self.assertIsNone(tor.executed_by_id)

    def test_cancellation_reason_field_does_not_exist_on_model(self):
        field_names = {f.name for f in TreasuryOperationRequest._meta.fields}
        self.assertNotIn("cancellation_reason", field_names)

    def test_cancelled_at_only_set_on_cancel_never_on_creation_or_other_transitions(self):
        pending = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        self.assertIsNone(pending.cancelled_at)

        approved = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
            approved_by=self.reviewer, approved_at=timezone.now(),
        )
        self.assertIsNone(approved.cancelled_at)

        self.client.force_login(self.requester)
        self.client.post(_cancel_url(pending.pk), data={})
        pending.refresh_from_db()
        self.assertIsNotNone(pending.cancelled_at)

        approved.refresh_from_db()
        self.assertIsNone(approved.cancelled_at)  # untouched by an unrelated cancellation
