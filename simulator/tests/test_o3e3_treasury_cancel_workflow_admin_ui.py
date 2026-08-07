# simulator/tests/test_o3e3_treasury_cancel_workflow_admin_ui.py
"""
Bloque O.3e-3 — Treasury Cancel Workflow Admin UI.

Covers the new admin view registered on TreasuryOperationRequestAdmin
(simulator/admin.py::treasury_request_cancel_view), its template
(simulator/templates/admin/treasury_request_cancel.html), and the
"Cancel Request" header button injected on the detail page
(simulator/templates/admin/simulator/treasuryoperationrequest/
change_form.html).

This block reuses cancel_treasury_request() (O.3e-2) exactly as built —
no cancellation/state-machine logic is reimplemented here. Unlike the
O.3b-3 review views (two separate views, each with one fixed required
permission), there is a SINGLE cancel view here because the required
permission depends on row data (is the caller the request's own
requested_by?) — the same two-branch dispatch frozen inside
cancel_treasury_request() itself. Several tests below prove delegation
explicitly (by mocking the service function and asserting it was called
with the right arguments) rather than just asserting the end state.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)

from .factories import make_user, make_wallet


def _grant(user, codename):
    perm = Permission.objects.get(codename=codename)
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_submitter(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant(user, "can_submit_treasury_request")


def _make_reviewer(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant(user, "can_review_treasury_request")


def _make_pending_request(wallet=None, requested_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet,
        "amount": Decimal("40.00"),
        "reason": "O.3e-3 admin UI test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _cancel_url(pk):
    return reverse("admin:treasury_request_cancel", args=[pk])


class ButtonVisibilityTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_button_visible_for_self_withdrawal(self):
        requester = _make_submitter(username="o3e3_btn_self")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Cancel Request →")

    def test_button_visible_for_administrative_cancellation(self):
        requester = make_user(username="o3e3_btn_admin_requester")
        reviewer = _make_reviewer(username="o3e3_btn_admin_reviewer")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(reviewer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Cancel Request →")

    def test_button_hidden_without_any_relevant_permission(self):
        requester = make_user(username="o3e3_btn_none_requester")
        staff = make_user(username="o3e3_btn_none_staff", is_staff=True)
        _grant(staff, "view_treasuryoperationrequest")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(staff)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Cancel Request →")

    def test_button_hidden_for_requester_lacking_submit_permission(self):
        # Edge case: requested_by no longer holds can_submit_treasury_request
        # (e.g. permission revoked after submission). The requester is
        # still requested_by, so dispatch resolves to self-withdrawal —
        # which requires can_submit_treasury_request specifically, not
        # can_review_treasury_request.
        requester = make_user(username="o3e3_btn_no_submit_perm", is_staff=True)
        _grant(requester, "can_review_treasury_request")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Cancel Request →")

    def test_button_hidden_for_each_non_pending_status(self):
        requester = _make_submitter(username="o3e3_btn_nonpending_requester")
        self.client.force_login(requester)
        non_pending = [
            TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED,
            TreasuryOperationRequest.ST_EXECUTING,
            TreasuryOperationRequest.ST_EXECUTED,
            TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        ]
        for status in non_pending:
            with self.subTest(status=status):
                tor = _make_pending_request(requested_by=requester, status=status)
                resp = self.client.get(_change_url(tor.pk))
                self.assertNotContains(resp, "Cancel Request →")


class CancelViewAccessTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_get_self_withdrawal_shows_summary(self):
        requester = _make_submitter(username="o3e3_access_self")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{tor.pk}")
        self.assertContains(resp, "self-withdrawal")
        self.assertContains(resp, "DOES NOT move funds")

    def test_get_administrative_shows_summary(self):
        requester = make_user(username="o3e3_access_admin_requester")
        reviewer = _make_reviewer(username="o3e3_access_admin_reviewer")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(reviewer)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "administrative")

    def test_get_shows_all_required_fields(self):
        requester = _make_submitter(username="o3e3_access_fields")
        tor = _make_pending_request(
            requested_by=requester, reference="REF-1", category=TreasuryOperationRequest.CAT_OTHER,
        )
        self.client.force_login(requester)
        resp = self.client.get(_cancel_url(tor.pk))
        content = resp.content.decode()
        for label in (
            "Request ID", "Wallet", "Username", "Email", "Operation",
            "Currency", "Amount", "Category", "Reference",
            "Requested By", "Requested At", "Cancelling As", "Status",
        ):
            self.assertIn(label, content)

    def test_get_without_any_permission_403(self):
        requester = make_user(username="o3e3_access_noperm_requester")
        staff = make_user(username="o3e3_access_noperm_staff", is_staff=True)
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(staff)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_requester_without_submit_permission_403(self):
        requester = make_user(username="o3e3_access_norsubmit", is_staff=True)
        _grant(requester, "can_review_treasury_request")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_unauthenticated_redirects_to_login(self):
        requester = _make_submitter(username="o3e3_access_anon_requester")
        tor = _make_pending_request(requested_by=requester)
        resp = self.client.get(_cancel_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_get_nonexistent_pk_404(self):
        reviewer = _make_reviewer(username="o3e3_access_404")
        self.client.force_login(reviewer)
        resp = self.client.get(_cancel_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_get_non_pending_redirects_with_warning(self):
        requester = _make_submitter(username="o3e3_access_nonpending_requester")
        tor = _make_pending_request(
            requested_by=requester, status=TreasuryOperationRequest.ST_APPROVED,
        )
        self.client.force_login(requester)
        resp = self.client.get(_cancel_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in msgs))


class CancelSubmissionTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_post_valid_self_withdrawal_cancels_and_redirects(self):
        requester = _make_submitter(username="o3e3_submit_self")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.post(_cancel_url(tor.pk), data={})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(tor.cancelled_at)

    def test_post_valid_administrative_cancels_and_redirects(self):
        requester = make_user(username="o3e3_submit_admin_requester")
        reviewer = _make_reviewer(username="o3e3_submit_admin_reviewer")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(reviewer)
        resp = self.client.post(_cancel_url(tor.pk), data={})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)

    def test_post_success_message(self):
        requester = _make_submitter(username="o3e3_submit_msg")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(f"#{tor.pk}" in m and "cancelada" in m for m in msgs))

    def test_post_without_reason_still_succeeds(self):
        requester = _make_submitter(username="o3e3_submit_noreason")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        resp = self.client.post(_cancel_url(tor.pk), data={})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)

    def test_post_cancellation_reason_recorded_only_in_audit(self):
        requester = _make_submitter(username="o3e3_submit_reason_audit")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        self.client.post(_cancel_url(tor.pk), data={"cancellation_reason": "  changed my mind  "})
        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancellation_reason"], "changed my mind")
        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})
        self.assertFalse(hasattr(tor, "cancellation_reason"))

    def test_post_creates_exactly_one_auditlog_and_brokerauditevent(self):
        requester = _make_submitter(username="o3e3_submit_audit_count")
        tor = _make_pending_request(requested_by=requester)
        self.client.force_login(requester)
        self.client.post(_cancel_url(tor.pk), data={})
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )

    def test_post_without_permission_403_and_no_change(self):
        requester = make_user(username="o3e3_submit_noperm_requester")
        staff = make_user(username="o3e3_submit_noperm_staff", is_staff=True)
        tor = _make_pending_request(requested_by=requester)
        client = Client()
        client.force_login(staff)
        resp = client.post(_cancel_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_post_on_already_processed_request_warns_no_double_effect(self):
        requester = _make_submitter(username="o3e3_submit_double_requester")
        tor = _make_pending_request(requested_by=requester)
        reviewer = _make_reviewer(username="o3e3_submit_double_reviewer")
        other_client = Client()
        other_client.force_login(reviewer)
        other_client.post(_cancel_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)

        self.client.force_login(requester)
        resp = self.client.post(_cancel_url(tor.pk), data={}, follow=True)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in msgs))
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )


class ServiceReuseTests(TestCase):
    """
    Proves the view delegates to cancel_treasury_request() (O.3e-2)
    rather than reimplementing the transition — both by mocking the
    service and asserting it was called with the right arguments, and
    structurally, by AST-checking the view method never assigns to
    `.status`/calls `.save()` directly.
    """

    def test_cancel_view_calls_cancel_treasury_request_with_correct_args(self):
        requester = _make_submitter(username="o3e3_reuse_self")
        tor = _make_pending_request(requested_by=requester)
        client = Client()
        client.force_login(requester)
        with patch("simulator.treasury_requests.cancel_treasury_request") as mock_cancel:
            mock_cancel.return_value = tor
            client.post(_cancel_url(tor.pk), data={"cancellation_reason": "note-x"})

        mock_cancel.assert_called_once()
        args, kwargs = mock_cancel.call_args
        self.assertEqual(args[0].pk, tor.pk)
        self.assertEqual(kwargs["request"].user, requester)
        self.assertEqual(kwargs["cancellation_reason"], "note-x")

    def test_view_method_never_assigns_status_or_calls_save_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(TreasuryOperationRequestAdmin.treasury_request_cancel_view)),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "status":
                self.assertNotIsInstance(
                    node.ctx, ast.Store,
                    "treasury_request_cancel_view must not assign .status directly",
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                self.assertNotEqual(
                    name, "save", "treasury_request_cancel_view must not call .save() directly",
                )


class NoFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.requester = _make_submitter(username="o3e3_no_money_requester")
        self.client = Client()
        self.client.force_login(self.requester)

    def test_cancel_creates_no_wallet_transaction_or_internal_transfer(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.post(_cancel_url(tor.pk), data={})
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_cancel_does_not_change_wallet_balances(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        self.client.post(_cancel_url(tor.pk), data={})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_admin_module_never_imports_wallet_ledger_or_funded_payouts_in_cancel_view(self):
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
