# simulator/tests/test_o3c5a_treasury_execute_admin_ui.py
"""
Bloque O.3c-5a — Treasury Execute Admin UI.

Covers the new admin view registered on TreasuryOperationRequestAdmin
(simulator/admin.py::treasury_request_execute_view), its template
(simulator/templates/admin/treasury_request_execute.html), the Execute
header button and the EXECUTED/FAILED state panel injected on the
detail page (simulator/templates/admin/simulator/
treasuryoperationrequest/change_form.html).

This block reuses execute_treasury_request() (O.3c-3) exactly as
built — no financial logic, state-machine transition or wallet_ledger
call is reimplemented here. Several tests below prove delegation
explicitly (mocking the service and asserting the call args, plus an
AST check that the view never assigns .status/.wallet_transaction,
never calls .save(), and never imports credit_wallet/debit_wallet)
rather than just asserting the end state, because "the view must
delegate completely, never duplicate business validations" is a
correctness requirement of this block, not just a style preference.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.wallet_ledger import credit_wallet

from .factories import make_user, make_wallet


def _grant_execute_permission(user):
    perm = Permission.objects.get(codename="can_execute_treasury_request")
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_executor(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant_execute_permission(user)


def _make_approved_request(wallet=None, requested_by=None, approved_by=None,
                            operation_type=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    if operation_type is None:
        operation_type = TreasuryOperationRequest.OP_BONUS_CREDIT
    data = {
        "operation_type": operation_type,
        "wallet": wallet,
        "amount": Decimal("40.00"),
        "reason": "O.3c-5a admin UI test",
        "reference": "REF-O3C5A",
        "requested_by": requested_by,
        "status": TreasuryOperationRequest.ST_APPROVED,
        "approved_by": approved_by,
        "approved_at": timezone.now(),
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _execute_url(pk):
    return reverse("admin:treasury_request_execute", args=[pk])


class ButtonVisibilityTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_btn_executor")
        self.requester = make_user(username="o3c5a_btn_requester")
        self.approver = make_user(username="o3c5a_btn_approver")
        self.client = Client()

    def test_button_visible_for_approved_with_permission_not_requester_not_approver(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        self.client.force_login(self.executor)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Execute →")

    def test_button_hidden_without_execute_permission(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        staff = make_user(username="o3c5a_btn_no_perm", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_treasuryoperationrequest"))
        staff.refresh_from_db()
        self.client.force_login(staff)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Execute →")

    def test_button_hidden_for_requester_viewing_own_request(self):
        requester_executor = _make_executor(username="o3c5a_btn_self_req")
        tor = _make_approved_request(requested_by=requester_executor, approved_by=self.approver)
        self.client.force_login(requester_executor)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Execute →")

    def test_button_hidden_for_approver_viewing_own_approval(self):
        approver_executor = _make_executor(username="o3c5a_btn_self_appr")
        tor = _make_approved_request(requested_by=self.requester, approved_by=approver_executor)
        self.client.force_login(approver_executor)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Execute →")

    def test_button_hidden_when_wallet_transaction_already_linked(self):
        wallet = make_wallet()
        wtx = credit_wallet(wallet.id, Decimal("5.00"), WalletTransaction.TX_BONUS, note="anomaly setup")
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            wallet_transaction=wtx,
        )
        self.client.force_login(self.executor)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Execute →")

    def test_button_hidden_for_each_non_approved_status(self):
        self.client.force_login(self.executor)
        non_approved = [
            TreasuryOperationRequest.ST_PENDING,
            TreasuryOperationRequest.ST_REJECTED,
            TreasuryOperationRequest.ST_EXECUTING,
            TreasuryOperationRequest.ST_EXECUTED,
            TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        ]
        for status in non_approved:
            with self.subTest(status=status):
                tor = _make_approved_request(
                    requested_by=self.requester, approved_by=self.approver, status=status,
                )
                resp = self.client.get(_change_url(tor.pk))
                self.assertNotContains(resp, "Execute →")


class ExecuteViewAccessTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_access_executor")
        self.requester = make_user(username="o3c5a_access_requester")
        self.approver = make_user(username="o3c5a_access_approver")
        self.client = Client()

    def test_get_with_permission_shows_summary(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{tor.pk}")
        self.assertContains(resp, tor.wallet.user.username)
        self.assertContains(resp, "WILL move real funds")

    def test_get_shows_all_required_fields(self):
        tor = _make_approved_request(
            requested_by=self.requester, approved_by=self.approver,
            reference="REF-1", category=TreasuryOperationRequest.CAT_OTHER,
        )
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(tor.pk))
        content = resp.content.decode()
        for label in (
            "Request ID", "Wallet", "Username", "Email", "Operation Type",
            "Currency", "Amount", "Category", "Reference", "Reason",
            "Requested By", "Approved By", "Executing As", "Status",
            "Available Balance", "Pending Balance",
        ):
            self.assertIn(label, content)

    def test_get_without_permission_403(self):
        staff = make_user(username="o3c5a_access_no_perm", is_staff=True)
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        self.client.force_login(staff)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_requester_403(self):
        requester_executor = _make_executor(username="o3c5a_access_self_req")
        tor = _make_approved_request(requested_by=requester_executor, approved_by=self.approver)
        self.client.force_login(requester_executor)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_approver_403(self):
        approver_executor = _make_executor(username="o3c5a_access_self_appr")
        tor = _make_approved_request(requested_by=self.requester, approved_by=approver_executor)
        self.client.force_login(approver_executor)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_unauthenticated_redirects_to_login(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_get_nonexistent_pk_404(self):
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_get_non_approved_redirects_with_warning(self):
        tor = _make_approved_request(
            requested_by=self.requester, approved_by=self.approver,
            status=TreasuryOperationRequest.ST_PENDING,
        )
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está aprobada" in m for m in msgs))


class ExecuteSubmissionTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_submit_executor")
        self.requester = make_user(username="o3c5a_submit_requester")
        self.approver = make_user(username="o3c5a_submit_approver")
        self.client = Client()
        self.client.force_login(self.executor)

    def test_post_valid_executes_and_redirects(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        resp = self.client.post(_execute_url(tor.pk), data={"execution_notes": ""})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTED)
        self.assertEqual(tor.executed_by_id, self.executor.pk)
        self.assertIsNotNone(tor.executed_at)
        self.assertIsNotNone(tor.wallet_transaction_id)

    def test_post_success_message_includes_wallet_transaction_id(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        resp = self.client.post(_execute_url(tor.pk), data={}, follow=True)
        tor.refresh_from_db()
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(
            f"#{tor.pk}" in m and "ejecutada" in m and f"#{tor.wallet_transaction_id}" in m
            for m in msgs
        ))

    def test_post_execution_notes_recorded_only_in_audit(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        self.client.post(_execute_url(tor.pk), data={"execution_notes": "  looks fine  "})
        row = AuditLog.objects.get(event_type="treasury.request_executed")
        self.assertEqual(row.detail["execution_notes"], "looks fine")
        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})

    def test_post_creates_expected_wallet_transaction_and_updates_balance(self):
        wallet = make_wallet(initial_balance=Decimal("10.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, amount=Decimal("25.00"),
        )
        self.client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()
        wallet.refresh_from_db()
        wtx = tor.wallet_transaction
        self.assertEqual(wtx.tx_type, WalletTransaction.TX_BONUS)
        self.assertEqual(wtx.amount, Decimal("25.00"))
        self.assertEqual(wallet.available_balance, Decimal("35.00"))
        self.assertEqual(wtx.balance_after, Decimal("35.00"))

    def test_post_insufficient_funds_marks_failed_with_error_message(self):
        wallet = make_wallet(initial_balance=Decimal("5.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            operation_type=TreasuryOperationRequest.OP_MANUAL_DEBIT, amount=Decimal("999.00"),
        )
        resp = self.client.post(_execute_url(tor.pk), data={}, follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIsNone(tor.wallet_transaction)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("fondos insuficientes" in m for m in msgs))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("5.00"))

    def test_post_without_permission_403_and_no_change(self):
        staff = make_user(username="o3c5a_submit_no_perm", is_staff=True)
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        client = Client()
        client.force_login(staff)
        resp = client.post(_execute_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_post_self_execution_as_requester_403_and_no_change(self):
        requester_executor = _make_executor(username="o3c5a_submit_self_req")
        tor = _make_approved_request(requested_by=requester_executor, approved_by=self.approver)
        client = Client()
        client.force_login(requester_executor)
        resp = client.post(_execute_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_post_self_execution_as_approver_403_and_no_change(self):
        approver_executor = _make_executor(username="o3c5a_submit_self_appr")
        tor = _make_approved_request(requested_by=self.requester, approved_by=approver_executor)
        client = Client()
        client.force_login(approver_executor)
        resp = client.post(_execute_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_post_on_already_processed_request_warns_no_double_effect(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        # First execution, via a second executor to avoid self-execution noise.
        other_executor = _make_executor(username="o3c5a_submit_other")
        other_client = Client()
        other_client.force_login(other_executor)
        other_client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTED)
        first_wtx_id = tor.wallet_transaction_id

        # Second executor tries again on the same (now-EXECUTED) request.
        resp = self.client.post(_execute_url(tor.pk), data={}, follow=True)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está aprobada" in m for m in msgs))

        tor.refresh_from_db()
        self.assertEqual(tor.wallet_transaction_id, first_wtx_id)  # unchanged
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 1,
        )


class ExecutionStatePanelTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_panel_executor")
        self.requester = make_user(username="o3c5a_panel_requester")
        self.approver = make_user(username="o3c5a_panel_approver")
        self.client = Client()

    def test_executed_panel_shows_wallet_transaction_details(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        self.client.force_login(self.executor)
        self.client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()

        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("Execution", content)
        self.assertIn(f"#{tor.wallet_transaction_id}", content)
        self.assertIn(tor.wallet_transaction.get_tx_type_display(), content)
        self.assertIn(str(tor.wallet_transaction.balance_after), content)
        self.assertIn(self.executor.username, content)

    def test_failed_panel_distinguishes_financial_failure(self):
        wallet = make_wallet(initial_balance=Decimal("1.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            operation_type=TreasuryOperationRequest.OP_MANUAL_DEBIT, amount=Decimal("500.00"),
        )
        self.client.force_login(self.executor)
        self.client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)

        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("Execution Failure", content)
        self.assertIn("Financial engine failure", content)
        self.assertNotIn("Manual recovery (not a financial engine failure)", content)

    def test_failed_panel_distinguishes_manual_recovery(self):
        # No public recovery UI exists yet (that's O.3c-5b) — the
        # "[MANUAL RECOVERY]" prefix convention itself belongs to
        # mark_treasury_execution_failed() (O.3c-4c, unmodified here).
        # This test only proves the O.3c-5a panel reads that prefix
        # correctly, using the exact literal the recovery service writes.
        tor = _make_approved_request(
            requested_by=self.requester, approved_by=self.approver,
            status=TreasuryOperationRequest.ST_FAILED,
            failure_reason="[MANUAL RECOVERY] stuck 40+ minutes",
            executed_by=self.executor, executed_at=timezone.now(),
        )
        self.client.force_login(self.executor)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("Execution Failure", content)
        self.assertIn("Manual recovery (not a financial engine failure)", content)


class ServiceReuseTests(TestCase):
    """
    Proves the view delegates to execute_treasury_request() (O.3c-3)
    rather than reimplementing the transition — both by mocking the
    service and asserting it was called with the right arguments, and
    structurally, by AST-checking the view method never assigns to
    .status/.wallet_transaction/.executed_by, never calls .save(), and
    never imports/calls credit_wallet()/debit_wallet() directly.
    """

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_reuse_executor")
        self.requester = make_user(username="o3c5a_reuse_requester")
        self.approver = make_user(username="o3c5a_reuse_approver")
        self.client = Client()
        self.client.force_login(self.executor)

    def test_execute_view_calls_execute_treasury_request_with_correct_args(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        # execute_treasury_request() is mocked out entirely, so the view
        # never actually calls credit_wallet/debit_wallet — the returned
        # instance still needs an in-memory wallet_transaction for the
        # view's own success-message formatting to read.
        tor.wallet_transaction = credit_wallet(
            tor.wallet_id, Decimal("40.00"), WalletTransaction.TX_BONUS, note="mocked execution",
        )
        with patch("simulator.treasury_requests.execute_treasury_request") as mock_execute:
            mock_execute.return_value = tor
            self.client.post(_execute_url(tor.pk), data={"execution_notes": "note-x"})

        mock_execute.assert_called_once()
        args, kwargs = mock_execute.call_args
        self.assertEqual(args[0].pk, tor.pk)
        self.assertEqual(kwargs["request"].user, self.executor)
        self.assertEqual(kwargs["execution_notes"], "note-x")

    def test_view_method_never_assigns_financial_fields_or_calls_save_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        fn = TreasuryOperationRequestAdmin.treasury_request_execute_view
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "status", "wallet_transaction", "executed_by", "executed_at",
            ):
                self.assertNotIsInstance(
                    node.ctx, ast.Store,
                    f"treasury_request_execute_view must not assign .{node.attr} directly",
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                self.assertNotEqual(
                    name, "save", "treasury_request_execute_view must not call .save() directly",
                )

    def test_view_never_imports_or_calls_credit_or_debit_wallet_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        for fn in (
            TreasuryOperationRequestAdmin.treasury_request_execute_view,
            TreasuryOperationRequestAdmin.change_view,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            imported_names = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imported_names.update(a.name for a in node.names)
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    self.assertNotIn(
                        name, ("credit_wallet", "debit_wallet"),
                        f"{fn.__name__} must never call credit_wallet()/debit_wallet() directly",
                    )
            self.assertNotIn("credit_wallet", imported_names, fn.__name__)
            self.assertNotIn("debit_wallet", imported_names, fn.__name__)


class NoDuplicatedFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5a_no_dup_executor")
        self.requester = make_user(username="o3c5a_no_dup_requester")
        self.approver = make_user(username="o3c5a_no_dup_approver")
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.client = Client()
        self.client.force_login(self.executor)

    def test_get_confirmation_screen_creates_no_wallet_transaction(self):
        tor = _make_approved_request(
            wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.get(_execute_url(tor.pk))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_post_creates_exactly_one_auditlog_and_brokerauditevent_for_execution(self):
        tor = _make_approved_request(
            wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.post(_execute_url(tor.pk), data={})
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_executed").count(), 1,
        )

    def test_post_creates_no_internal_transfer(self):
        tor = _make_approved_request(
            wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.post(_execute_url(tor.pk), data={})
        self.assertEqual(InternalTransfer.objects.count(), 0)
