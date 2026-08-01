# simulator/tests/test_o3b3_treasury_review_admin_ui.py
"""
Bloque O.3b-3 — Treasury Review Admin UI.

Covers the two new admin views registered on TreasuryOperationRequestAdmin
(simulator/admin.py::treasury_request_approve_view /
treasury_request_reject_view), their templates
(simulator/templates/admin/treasury_request_approve.html /
treasury_request_reject.html), and the Approve/Reject header buttons
injected on the detail page
(simulator/templates/admin/simulator/treasuryoperationrequest/
change_form.html).

This block reuses approve_treasury_request()/reject_treasury_request()
(O.3b-2) exactly as built — no review/approval/rejection logic is
reimplemented here. Several tests below prove delegation explicitly (by
mocking the service functions and asserting they were called with the
right arguments) rather than just asserting the end state, precisely
because "don't duplicate logic" is a correctness requirement of this
block, not just a style preference.
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


def _grant_review_permission(user):
    perm = Permission.objects.get(codename="can_review_treasury_request")
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_reviewer(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant_review_permission(user)


def _make_pending_request(wallet=None, requested_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet,
        "amount": Decimal("40.00"),
        "reason": "O.3b-3 admin UI test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _approve_url(pk):
    return reverse("admin:treasury_request_approve", args=[pk])


def _reject_url(pk):
    return reverse("admin:treasury_request_reject", args=[pk])


class ButtonVisibilityTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_btn_reviewer")
        self.requester = make_user(username="o3b3_btn_requester")
        self.client = Client()

    def test_buttons_visible_for_pending_with_permission_not_requester(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.force_login(self.reviewer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Approve →")
        self.assertContains(resp, "Reject →")

    def test_buttons_hidden_without_review_permission(self):
        tor = _make_pending_request(requested_by=self.requester)
        staff = make_user(username="o3b3_btn_no_perm", is_staff=True)
        _grant = Permission.objects.get(codename="view_treasuryoperationrequest")
        staff.user_permissions.add(_grant)
        staff.refresh_from_db()
        self.client.force_login(staff)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Approve →")
        self.assertNotContains(resp, "Reject →")

    def test_buttons_hidden_for_requester_viewing_own_request(self):
        requester_reviewer = _make_reviewer(username="o3b3_btn_self")
        tor = _make_pending_request(requested_by=requester_reviewer)
        self.client.force_login(requester_reviewer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Approve →")
        self.assertNotContains(resp, "Reject →")

    def test_buttons_hidden_for_each_non_pending_status(self):
        self.client.force_login(self.reviewer)
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
                tor = _make_pending_request(requested_by=self.requester, status=status)
                resp = self.client.get(_change_url(tor.pk))
                self.assertNotContains(resp, "Approve →")
                self.assertNotContains(resp, "Reject →")


class ApproveViewAccessTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_approve_access_reviewer")
        self.requester = make_user(username="o3b3_approve_access_requester")
        self.client = Client()

    def test_get_with_permission_shows_summary(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.force_login(self.reviewer)
        resp = self.client.get(_approve_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{tor.pk}")
        self.assertContains(resp, tor.wallet.user.username)
        self.assertContains(resp, "DOES NOT move funds")

    def test_get_shows_all_required_fields(self):
        tor = _make_pending_request(requested_by=self.requester, reference="REF-1", category=TreasuryOperationRequest.CAT_OTHER)
        self.client.force_login(self.reviewer)
        resp = self.client.get(_approve_url(tor.pk))
        content = resp.content.decode()
        for label in (
            "Request ID", "Wallet", "Username", "Email", "Operation",
            "Currency", "Amount", "Category", "Reference",
            "Requested By", "Requested At", "Reviewing As", "Status",
        ):
            self.assertIn(label, content)

    def test_get_without_permission_403(self):
        staff = make_user(username="o3b3_approve_no_perm", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)
        self.client.force_login(staff)
        resp = self.client.get(_approve_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_requester_403(self):
        requester_reviewer = _make_reviewer(username="o3b3_approve_self")
        tor = _make_pending_request(requested_by=requester_reviewer)
        self.client.force_login(requester_reviewer)
        resp = self.client.get(_approve_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_unauthenticated_redirects_to_login(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.get(_approve_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_get_nonexistent_pk_404(self):
        self.client.force_login(self.reviewer)
        resp = self.client.get(_approve_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_get_non_pending_redirects_with_warning(self):
        tor = _make_pending_request(
            requested_by=self.requester, status=TreasuryOperationRequest.ST_APPROVED,
        )
        self.client.force_login(self.reviewer)
        resp = self.client.get(_approve_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in messages))


class ApproveSubmissionTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_approve_submit_reviewer")
        self.requester = make_user(username="o3b3_approve_submit_requester")
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_post_valid_approves_and_redirects(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_approve_url(tor.pk), data={"review_notes": ""})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(tor.approved_by_id, self.reviewer.pk)
        self.assertIsNotNone(tor.approved_at)

    def test_post_success_message(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_approve_url(tor.pk), data={}, follow=True)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(f"#{tor.pk}" in m and "aprobada" in m for m in messages))

    def test_post_review_notes_recorded_only_in_audit(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.post(_approve_url(tor.pk), data={"review_notes": "  looks good  "})
        row = AuditLog.objects.get(event_type="treasury.request_approved")
        self.assertEqual(row.detail["review_notes"], "looks good")
        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})

    def test_post_creates_exactly_one_auditlog_and_brokerauditevent(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.post(_approve_url(tor.pk), data={})
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_approved").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_approved").count(), 1,
        )

    def test_post_without_permission_403_and_no_change(self):
        staff = make_user(username="o3b3_approve_post_no_perm", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)
        client = Client()
        client.force_login(staff)
        resp = client.post(_approve_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_post_self_review_403_and_no_change(self):
        requester_reviewer = _make_reviewer(username="o3b3_approve_post_self")
        tor = _make_pending_request(requested_by=requester_reviewer)
        client = Client()
        client.force_login(requester_reviewer)
        resp = client.post(_approve_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_post_on_already_processed_request_warns_no_double_effect(self):
        tor = _make_pending_request(requested_by=self.requester)
        # First approval, via a second reviewer to avoid self-review noise.
        other_reviewer = _make_reviewer(username="o3b3_approve_other")
        other_client = Client()
        other_client.force_login(other_reviewer)
        other_client.post(_approve_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.approved_by_id, other_reviewer.pk)

        # Second reviewer tries again on the same (now-APPROVED) request.
        resp = self.client.post(_approve_url(tor.pk), data={}, follow=True)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está pendiente" in m for m in messages))

        tor.refresh_from_db()
        self.assertEqual(tor.approved_by_id, other_reviewer.pk)  # unchanged — first reviewer's approval stands
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_approved").count(), 1,
        )


class RejectViewAccessTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_reject_access_reviewer")
        self.requester = make_user(username="o3b3_reject_access_requester")
        self.client = Client()

    def test_get_with_permission_shows_summary_and_required_field(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.force_login(self.reviewer)
        resp = self.client.get(_reject_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{tor.pk}")
        self.assertContains(resp, "Rejection Reason")
        self.assertContains(resp, "DOES NOT move funds")

    def test_get_without_permission_403(self):
        staff = make_user(username="o3b3_reject_no_perm", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)
        self.client.force_login(staff)
        resp = self.client.get(_reject_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_requester_403(self):
        requester_reviewer = _make_reviewer(username="o3b3_reject_self")
        tor = _make_pending_request(requested_by=requester_reviewer)
        self.client.force_login(requester_reviewer)
        resp = self.client.get(_reject_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_non_pending_redirects_with_warning(self):
        tor = _make_pending_request(
            requested_by=self.requester, status=TreasuryOperationRequest.ST_REJECTED,
        )
        self.client.force_login(self.reviewer)
        resp = self.client.get(_reject_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))


class RejectSubmissionTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_reject_submit_reviewer")
        self.requester = make_user(username="o3b3_reject_submit_requester")
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_post_valid_rejects_and_redirects(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_reject_url(tor.pk), data={"rejection_reason": "bad ticket"})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_REJECTED)
        self.assertEqual(tor.rejected_by_id, self.reviewer.pk)
        self.assertEqual(tor.rejection_reason, "bad ticket")

    def test_post_success_message(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(
            _reject_url(tor.pk), data={"rejection_reason": "bad ticket"}, follow=True,
        )
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(f"#{tor.pk}" in m and "rechazada" in m for m in messages))

    def test_post_missing_rejection_reason_rerenders_form_no_change(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_reject_url(tor.pk), data={"rejection_reason": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "obligatorio")
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_post_whitespace_only_rejection_reason_rerenders_form_no_change(self):
        tor = _make_pending_request(requested_by=self.requester)
        resp = self.client.post(_reject_url(tor.pk), data={"rejection_reason": "    "})
        self.assertEqual(resp.status_code, 200)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_post_review_notes_recorded_only_in_audit(self):
        tor = _make_pending_request(requested_by=self.requester)
        self.client.post(
            _reject_url(tor.pk),
            data={"rejection_reason": "bad ticket", "review_notes": "flagged"},
        )
        row = AuditLog.objects.get(event_type="treasury.request_rejected")
        self.assertEqual(row.detail["review_notes"], "flagged")
        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})

    def test_post_without_permission_403_and_no_change(self):
        staff = make_user(username="o3b3_reject_post_no_perm", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)
        client = Client()
        client.force_login(staff)
        resp = client.post(_reject_url(tor.pk), data={"rejection_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_post_self_review_403_and_no_change(self):
        requester_reviewer = _make_reviewer(username="o3b3_reject_post_self")
        tor = _make_pending_request(requested_by=requester_reviewer)
        client = Client()
        client.force_login(requester_reviewer)
        resp = client.post(_reject_url(tor.pk), data={"rejection_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)


class ServiceReuseTests(TestCase):
    """
    Proves the views delegate to approve_treasury_request()/
    reject_treasury_request() (O.3b-2) rather than reimplementing the
    transition — both by mocking the service and asserting it was
    called with the right arguments, and structurally, by AST-checking
    the view methods never assign to `.status`/call `.save()` directly.
    """

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_reuse_reviewer")
        self.requester = make_user(username="o3b3_reuse_requester")
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_approve_view_calls_approve_treasury_request_with_correct_args(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch("simulator.treasury_requests.approve_treasury_request") as mock_approve:
            mock_approve.return_value = tor
            self.client.post(_approve_url(tor.pk), data={"review_notes": "note-x"})

        mock_approve.assert_called_once()
        args, kwargs = mock_approve.call_args
        self.assertEqual(args[0].pk, tor.pk)
        self.assertEqual(kwargs["request"].user, self.reviewer)
        self.assertEqual(kwargs["review_notes"], "note-x")

    def test_reject_view_calls_reject_treasury_request_with_correct_args(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch("simulator.treasury_requests.reject_treasury_request") as mock_reject:
            mock_reject.return_value = tor
            self.client.post(
                _reject_url(tor.pk),
                data={"rejection_reason": "reason-x", "review_notes": "note-y"},
            )

        mock_reject.assert_called_once()
        args, kwargs = mock_reject.call_args
        self.assertEqual(args[0].pk, tor.pk)
        self.assertEqual(args[1], "reason-x")
        self.assertEqual(kwargs["request"].user, self.reviewer)
        self.assertEqual(kwargs["review_notes"], "note-y")

    def test_view_methods_never_assign_status_or_call_save_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        for fn in (
            TreasuryOperationRequestAdmin.treasury_request_approve_view,
            TreasuryOperationRequestAdmin.treasury_request_reject_view,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "status":
                    # Reading instance.status for display/comparison is fine;
                    # only an assignment target would mean the view is
                    # mutating status itself instead of delegating.
                    self.assertNotIsInstance(
                        node.ctx, ast.Store,
                        f"{fn.__name__} must not assign .status directly",
                    )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    self.assertNotEqual(
                        name, "save", f"{fn.__name__} must not call .save() directly",
                    )


class NoFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b3_no_money_reviewer")
        self.requester = make_user(username="o3b3_no_money_requester")
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_approve_creates_no_wallet_transaction_or_internal_transfer(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.post(_approve_url(tor.pk), data={})
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_reject_creates_no_wallet_transaction_or_internal_transfer(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.post(_reject_url(tor.pk), data={"rejection_reason": "x"})
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_approve_does_not_change_wallet_balances(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        self.client.post(_approve_url(tor.pk), data={})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_reject_does_not_change_wallet_balances(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        self.client.post(_reject_url(tor.pk), data={"rejection_reason": "x"})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_admin_module_never_imports_wallet_ledger_or_funded_payouts_in_review_views(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        for fn in (
            TreasuryOperationRequestAdmin.treasury_request_approve_view,
            TreasuryOperationRequestAdmin.treasury_request_reject_view,
            TreasuryOperationRequestAdmin.change_view,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module)
                    imported.update(a.name for a in node.names)
            self.assertNotIn("wallet_ledger", imported, fn.__name__)
            self.assertNotIn("funded_payouts", imported, fn.__name__)
