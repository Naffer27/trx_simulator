# simulator/tests/test_o3c5b_treasury_recovery_admin_ui.py
"""
Bloque O.3c-5b — Treasury Recovery Admin UI.

Covers the new admin view registered on TreasuryOperationRequestAdmin
(simulator/admin.py::treasury_request_recover_view), its template
(simulator/templates/admin/treasury_request_recover.html), the
EXECUTING recovery banner and its "Mark as FAILED" button injected on
the detail page (simulator/templates/admin/simulator/
treasuryoperationrequest/change_form.html).

This block reuses inspect_stuck_treasury_execution() and
mark_treasury_execution_failed() (O.3c-4b/O.3c-4c) exactly as built —
no classification, eligibility, or state-machine logic is
reimplemented here. Several tests below prove delegation explicitly
(mocking the service and asserting call args, plus AST checks that the
view never assigns .status/.failure_reason/.executed_at, never calls
.save(), and never imports/calls credit_wallet()/debit_wallet())
rather than just asserting the end state, because "the UI must delegate
completely, never duplicate business logic" is a correctness
requirement of this block, not just a style preference.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator import audit as _audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600  # settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS default


def _grant_recover_permission(user):
    perm = Permission.objects.get(codename="can_recover_treasury_execution")
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_recoverer(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant_recover_permission(user)


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3c-5b admin UI test",
        reference="REF-O3C5B",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by,
        wallet_transaction=wallet_transaction,
        requested_by=requested_by,
        approved_by=approved_by,
    )
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _started_audit_log(pk, age_seconds):
    created_at = timezone.now() - timedelta(seconds=age_seconds)
    return AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        action=f"Treasury request #{pk} execution started",
        detail={"treasury_request_id": pk},
        created_at=created_at,
    )


def _make_eligible_executing_request(executor, **overrides):
    """An EXECUTING request old enough (age > threshold) to be eligible=True."""
    tor = _make_executing_request(executed_by=executor, **overrides)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
    return tor


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _recover_url(pk):
    return reverse("admin:treasury_request_recover", args=[pk])


class BannerVisibilityTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c5b_banner_recoverer")
        self.executor = make_user(username="o3c5b_banner_executor", is_staff=True)
        self.requester = make_user(username="o3c5b_banner_requester")
        self.approver = make_user(username="o3c5b_banner_approver")
        self.client = Client()

    def test_banner_and_button_visible_for_eligible_candidate_with_permission(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertIn("CASE_A", content)
        self.assertIn("Mark as FAILED", content)

    def test_banner_visible_but_button_hidden_without_recover_permission(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        staff = make_user(username="o3c5b_banner_no_perm", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_treasuryoperationrequest"))
        staff.refresh_from_db()
        self.client.force_login(staff)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertNotIn("Mark as FAILED", content)

    def test_button_hidden_for_requester_viewing_own_request(self):
        requester_recoverer = _make_recoverer(username="o3c5b_banner_self_req")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=requester_recoverer, approved_by=self.approver,
        )
        self.client.force_login(requester_recoverer)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertNotIn("Mark as FAILED", content)

    def test_button_hidden_for_approver_viewing_own_approval(self):
        approver_recoverer = _make_recoverer(username="o3c5b_banner_self_appr")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=approver_recoverer,
        )
        self.client.force_login(approver_recoverer)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertNotIn("Mark as FAILED", content)

    def test_button_visible_when_executed_by_is_request_user(self):
        # mark_treasury_execution_failed() explicitly permits the
        # recovering user to BE the executed_by (only requested_by/
        # approved_by are blocked) — the UI must not add a stricter
        # rule than the service itself enforces.
        tor = _make_eligible_executing_request(
            self.recoverer, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Mark as FAILED")

    def test_button_hidden_when_candidate_not_eligible_too_young(self):
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=50)  # below the 600s threshold
        self.client.force_login(self.recoverer)
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertIn("✗ NO", content)
        self.assertNotIn("Mark as FAILED", content)
        self.assertIn("below the", content)

    def test_banner_absent_for_non_executing_statuses(self):
        self.client.force_login(self.recoverer)
        for status in (
            TreasuryOperationRequest.ST_PENDING,
            TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED,
            TreasuryOperationRequest.ST_EXECUTED,
            TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        ):
            with self.subTest(status=status):
                tor = _make_executing_request(
                    requested_by=self.requester, approved_by=self.approver, status=status,
                )
                resp = self.client.get(_change_url(tor.pk))
                self.assertNotContains(resp, "STUCK IN EXECUTING")


class RecoverViewAccessTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c5b_access_recoverer")
        self.executor = make_user(username="o3c5b_access_executor", is_staff=True)
        self.requester = make_user(username="o3c5b_access_requester")
        self.approver = make_user(username="o3c5b_access_approver")
        self.client = Client()

    def test_get_with_permission_shows_summary_and_diagnostics(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn(f"#{tor.pk}", content)
        self.assertIn("CASE_A", content)
        self.assertIn("will NOT reconcile", content)
        self.assertIn("Recovery Reason", content)

    def test_get_shows_all_required_fields(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(tor.pk))
        content = resp.content.decode()
        for label in (
            "Request ID", "Wallet", "Username", "Email", "Operation Type",
            "Currency", "Amount", "Reference", "Requested By", "Approved By",
            "Recovering As", "Status", "Case", "Age", "Age Confidence",
            "Executed By", "WalletTransaction", "Eligible",
        ):
            self.assertIn(label, content)

    def test_get_without_permission_403(self):
        staff = make_user(username="o3c5b_access_no_perm", is_staff=True)
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.force_login(staff)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_requester_403(self):
        requester_recoverer = _make_recoverer(username="o3c5b_access_self_req")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=requester_recoverer, approved_by=self.approver,
        )
        self.client.force_login(requester_recoverer)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_as_own_approver_403(self):
        approver_recoverer = _make_recoverer(username="o3c5b_access_self_appr")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=approver_recoverer,
        )
        self.client.force_login(approver_recoverer)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_get_unauthenticated_redirects_to_login(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_get_nonexistent_pk_404(self):
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_get_non_executing_redirects_with_warning(self):
        tor = _make_executing_request(
            requested_by=self.requester, approved_by=self.approver,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está en EXECUTING" in m for m in msgs))

    def test_get_not_eligible_redirects_with_block_reason(self):
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=50)  # below threshold -> not eligible
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("no es elegible" in m and "below the" in m for m in msgs))


class RecoverSubmissionTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c5b_submit_recoverer")
        self.executor = make_user(username="o3c5b_submit_executor", is_staff=True)
        self.requester = make_user(username="o3c5b_submit_requester")
        self.approver = make_user(username="o3c5b_submit_approver")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_post_valid_marks_failed_and_redirects(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "stuck 11+ minutes, confirmed dead worker"},
        )
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIn("[MANUAL RECOVERY]", tor.failure_reason)
        self.assertIn("stuck 11+ minutes, confirmed dead worker", tor.failure_reason)
        self.assertIsNone(tor.wallet_transaction)

    def test_post_success_message(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "confirmed dead worker"}, follow=True,
        )
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(f"#{tor.pk}" in m and "FAILED" in m for m in msgs))

    def test_post_missing_recovery_reason_rerenders_form_no_change(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.post(_recover_url(tor.pk), data={"recovery_reason": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "obligatorio")
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(
            AuditLog.objects.filter(
                event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
            ).count(),
            0,
        )

    def test_post_whitespace_only_recovery_reason_rerenders_form_no_change(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.post(_recover_url(tor.pk), data={"recovery_reason": "    "})
        self.assertEqual(resp.status_code, 200)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_post_without_permission_403_and_no_change(self):
        staff = make_user(username="o3c5b_submit_no_perm", is_staff=True)
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        client = Client()
        client.force_login(staff)
        resp = client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_post_self_recovery_as_requester_403_and_no_change(self):
        requester_recoverer = _make_recoverer(username="o3c5b_submit_self_req")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=requester_recoverer, approved_by=self.approver,
        )
        client = Client()
        client.force_login(requester_recoverer)
        resp = client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_post_self_recovery_as_approver_403_and_no_change(self):
        approver_recoverer = _make_recoverer(username="o3c5b_submit_self_appr")
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=approver_recoverer,
        )
        client = Client()
        client.force_login(approver_recoverer)
        resp = client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_post_by_executed_by_user_succeeds(self):
        tor = _make_eligible_executing_request(
            self.recoverer, requested_by=self.requester, approved_by=self.approver,
        )
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "self-recovering own stuck execution"},
        )
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)

    def test_post_on_already_recovered_request_warns_no_double_effect(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        other_recoverer = _make_recoverer(username="o3c5b_submit_other")
        other_client = Client()
        other_client.force_login(other_recoverer)
        other_client.post(_recover_url(tor.pk), data={"recovery_reason": "first recovery"})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        first_failure_reason = tor.failure_reason

        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "second attempt"}, follow=True,
        )
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está en EXECUTING" in m for m in msgs))

        tor.refresh_from_db()
        self.assertEqual(tor.failure_reason, first_failure_reason)  # unchanged
        self.assertEqual(
            AuditLog.objects.filter(
                event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            ).count(),
            1,
        )

    def test_post_not_eligible_too_young_redirects_with_warning_no_change(self):
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=50)
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "trying anyway"}, follow=True,
        )
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("no es elegible" in m for m in msgs))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)


class ServiceReuseTests(TestCase):
    """
    Proves the view delegates to mark_treasury_execution_failed()
    (O.3c-4c) and inspect_stuck_treasury_execution() (O.3c-4b) rather
    than reimplementing classification/eligibility/the transition —
    both by mocking the mutation service and asserting it was called
    with the right arguments, and structurally, by AST-checking the
    view method never assigns to .status/.failure_reason/.executed_at,
    never calls .save(), and never imports/calls
    credit_wallet()/debit_wallet() directly.
    """

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c5b_reuse_recoverer")
        self.executor = make_user(username="o3c5b_reuse_executor", is_staff=True)
        self.requester = make_user(username="o3c5b_reuse_requester")
        self.approver = make_user(username="o3c5b_reuse_approver")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_recover_view_calls_mark_treasury_execution_failed_with_correct_args(self):
        tor = _make_eligible_executing_request(
            self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_recover:
            mock_recover.return_value = tor
            self.client.post(_recover_url(tor.pk), data={"recovery_reason": "reason-x"})

        mock_recover.assert_called_once()
        args, kwargs = mock_recover.call_args
        self.assertEqual(args[0].pk, tor.pk)
        self.assertEqual(kwargs["request"].user, self.recoverer)
        self.assertEqual(kwargs["recovery_reason"], "reason-x")

    def test_view_method_never_assigns_recovery_fields_or_calls_save_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        fn = TreasuryOperationRequestAdmin.treasury_request_recover_view
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "status", "failure_reason", "executed_at", "wallet_transaction",
            ):
                self.assertNotIsInstance(
                    node.ctx, ast.Store,
                    f"treasury_request_recover_view must not assign .{node.attr} directly",
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                self.assertNotEqual(
                    name, "save", "treasury_request_recover_view must not call .save() directly",
                )

    def test_view_never_imports_or_calls_credit_or_debit_wallet_directly(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryOperationRequestAdmin

        fn = TreasuryOperationRequestAdmin.treasury_request_recover_view
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
                    "treasury_request_recover_view must never call credit_wallet()/debit_wallet() directly",
                )
        self.assertNotIn("credit_wallet", imported_names)
        self.assertNotIn("debit_wallet", imported_names)
        self.assertNotIn("wallet_ledger", imported_names)


class NoDuplicatedFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c5b_no_dup_recoverer")
        self.executor = make_user(username="o3c5b_no_dup_executor", is_staff=True)
        self.requester = make_user(username="o3c5b_no_dup_requester")
        self.approver = make_user(username="o3c5b_no_dup_approver")
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_get_confirmation_screen_creates_no_side_effects(self):
        tor = _make_eligible_executing_request(
            self.executor, wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.get(_recover_url(tor.pk))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_post_creates_exactly_one_auditlog_and_brokerauditevent_for_marked_failed(self):
        tor = _make_eligible_executing_request(
            self.executor, wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        self.client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(
            AuditLog.objects.filter(
                event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            ).count(),
            1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            ).count(),
            1,
        )

    def test_post_creates_no_wallet_transaction_or_internal_transfer(self):
        tor = _make_eligible_executing_request(
            self.executor, wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_post_does_not_change_wallet_balances(self):
        tor = _make_eligible_executing_request(
            self.executor, wallet=self.wallet, requested_by=self.requester, approved_by=self.approver,
        )
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        self.client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)
