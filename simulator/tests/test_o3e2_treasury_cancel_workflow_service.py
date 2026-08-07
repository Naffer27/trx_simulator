# simulator/tests/test_o3e2_treasury_cancel_workflow_service.py
"""
Bloque O.3e-2 — Treasury Cancel Workflow Service.

Covers ONLY simulator/treasury_requests.py::cancel_treasury_request()
— the single PENDING -> CANCELLED transition, reusing
TreasuryRequestNotPending (already used by approve_treasury_request()/
reject_treasury_request()) rather than introducing a new exception.

No view, URL, template, button or confirmation screen exists yet —
every test here calls the service directly. This service never moves
money: no WalletTransaction/InternalTransfer is ever created, no
Wallet balance field ever changes, wallet_ledger.py is never imported,
execute_treasury_request()/mark_treasury_execution_failed() are never
called, and executed_by/executed_at/wallet_transaction/approved_by/
rejected_by are never touched. No cancellation_reason field exists on
the model (Fase 0 Decision 1/2) — any reason text lives exclusively in
AuditLog/BrokerAuditEvent.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_requests import (
    TREASURY_REVIEW_PERMISSION,
    TREASURY_SUBMIT_PERMISSION,
    TreasuryRequestNotPending,
    cancel_treasury_request,
)

from .factories import make_user, make_wallet


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _make_submitter(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_submit_treasury_request")


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _make_pending_request(wallet=None, requested_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet,
        "amount": Decimal("50.00"),
        "reason": "O.3e-2 cancel service test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _request_for(user):
    request = RequestFactory().post("/admin/treasury-request/cancel/")
    request.user = user
    return request


class SuccessfulSelfWithdrawalTests(TestCase):

    def setUp(self):
        self.requester = _make_submitter(username="o3e2_self_requester")

    def test_requester_cancels_own_pending_request(self):
        tor = _make_pending_request(requested_by=self.requester)
        result = cancel_treasury_request(tor, request=_request_for(self.requester))

        self.assertEqual(result.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(result.cancelled_at)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(tor.cancelled_at)

    def test_self_withdrawal_does_not_require_review_permission(self):
        """A plain submitter with no can_review_treasury_request can
        still withdraw their own request — the two paths are gated by
        different, independent permissions."""
        tor = _make_pending_request(requested_by=self.requester)
        self.assertFalse(self.requester.has_perm(TREASURY_REVIEW_PERMISSION))
        result = cancel_treasury_request(tor, request=_request_for(self.requester))
        self.assertEqual(result.status, TreasuryOperationRequest.ST_CANCELLED)


class SuccessfulAdministrativeCancellationTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e2_admin_reviewer")
        self.requester = make_user(username="o3e2_admin_requester")

    def test_reviewer_cancels_someone_elses_pending_request(self):
        tor = _make_pending_request(requested_by=self.requester)
        result = cancel_treasury_request(tor, request=_request_for(self.reviewer))

        self.assertEqual(result.status, TreasuryOperationRequest.ST_CANCELLED)
        self.assertIsNotNone(result.cancelled_at)

    def test_administrative_path_does_not_require_submit_permission(self):
        self.assertFalse(self.reviewer.has_perm(TREASURY_SUBMIT_PERMISSION))
        tor = _make_pending_request(requested_by=self.requester)
        result = cancel_treasury_request(tor, request=_request_for(self.reviewer))
        self.assertEqual(result.status, TreasuryOperationRequest.ST_CANCELLED)


class PermissionContractTests(TestCase):

    def setUp(self):
        self.requester = make_user(username="o3e2_perm_requester")

    def test_unauthenticated_raises_permission_denied(self):
        tor = _make_pending_request(requested_by=self.requester)
        anon = RequestFactory().post("/x/")
        from django.contrib.auth.models import AnonymousUser
        anon.user = AnonymousUser()
        with self.assertRaises(PermissionDenied):
            cancel_treasury_request(tor, request=anon)

    def test_self_withdrawal_without_submit_permission_denied(self):
        """The requester lost/never had can_submit_treasury_request by
        the time they try to withdraw — identity alone is not enough."""
        requester_no_perm = make_user(username="o3e2_perm_no_submit", is_staff=True)
        tor = _make_pending_request(requested_by=requester_no_perm)
        with self.assertRaises(PermissionDenied):
            cancel_treasury_request(tor, request=_request_for(requester_no_perm))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_third_party_without_review_permission_denied(self):
        stranger = make_user(username="o3e2_perm_stranger", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)
        with self.assertRaises(PermissionDenied):
            cancel_treasury_request(tor, request=_request_for(stranger))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_submitter_without_review_permission_cannot_cancel_others_request(self):
        other_submitter = _make_submitter(username="o3e2_perm_other_submitter")
        tor = _make_pending_request(requested_by=self.requester)
        with self.assertRaises(PermissionDenied):
            cancel_treasury_request(tor, request=_request_for(other_submitter))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)


class StatusTransitionTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e2_status_reviewer")
        self.requester = _make_submitter(username="o3e2_status_requester")

    def test_non_pending_statuses_all_raise(self):
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
                with self.assertRaises(TreasuryRequestNotPending):
                    cancel_treasury_request(tor, request=_request_for(self.reviewer))
                tor.refresh_from_db()
                self.assertEqual(tor.status, status)  # unchanged

    def test_double_cancel_second_call_raises(self):
        tor = _make_pending_request(requested_by=self.requester)
        cancel_treasury_request(tor, request=_request_for(self.reviewer))
        with self.assertRaises(TreasuryRequestNotPending):
            cancel_treasury_request(tor, request=_request_for(self.reviewer))

    def test_cannot_cancel_after_approval(self):
        tor = _make_pending_request(
            requested_by=self.requester, status=TreasuryOperationRequest.ST_APPROVED,
        )
        with self.assertRaises(TreasuryRequestNotPending):
            cancel_treasury_request(tor, request=_request_for(self.requester))


class AuditContentTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e2_audit_reviewer")
        self.requester = _make_submitter(username="o3e2_audit_requester")
        self.wallet = make_wallet()

    def test_self_withdrawal_audit_detail_and_metadata(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(
            tor, request=_request_for(self.requester), cancellation_reason="changed my mind",
        )

        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["treasury_request_id"], tor.pk)
        self.assertEqual(row.detail["cancelled_by_id"], self.requester.pk)
        self.assertEqual(row.detail["cancelled_by_role"], "requester")
        self.assertEqual(row.detail["cancellation_reason"], "changed my mind")
        self.assertEqual(row.detail["previous_status"], TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(row.detail["new_status"], TreasuryOperationRequest.ST_CANCELLED)

        event = BrokerAuditEvent.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(event.severity, "INFO")
        self.assertEqual(event.metadata["cancelled_by_role"], "requester")
        self.assertEqual(event.metadata["cancellation_reason"], "changed my mind")

    def test_administrative_cancellation_audit_severity_is_warning(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(
            tor, request=_request_for(self.reviewer), cancellation_reason="duplicate ticket",
        )

        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancelled_by_id"], self.reviewer.pk)
        self.assertEqual(row.detail["cancelled_by_role"], "supervisor")

        event = BrokerAuditEvent.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(event.severity, "WARNING")
        self.assertEqual(event.metadata["cancelled_by_role"], "supervisor")

    def test_cancellation_reason_defaults_to_empty_string(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(tor, request=_request_for(self.requester))

        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancellation_reason"], "")

    def test_cancellation_reason_is_stripped(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(
            tor, request=_request_for(self.requester), cancellation_reason="   padded   ",
        )
        row = AuditLog.objects.get(event_type="treasury.request_cancelled")
        self.assertEqual(row.detail["cancellation_reason"], "padded")

    def test_creates_exactly_one_auditlog_and_brokerauditevent(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(tor, request=_request_for(self.requester))
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )


class NoFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e2_nomoney_reviewer")
        self.requester = _make_submitter(username="o3e2_nomoney_requester")
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))

    def test_no_wallet_transaction_or_internal_transfer_created(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        cancel_treasury_request(tor, request=_request_for(self.requester))

        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_wallet_balance_unchanged(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance

        cancel_treasury_request(tor, request=_request_for(self.requester))

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_only_status_and_cancelled_at_fields_change(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        # str() rather than raw value — FieldFile (evidence) doesn't
        # reliably compare equal to itself across two Python instances
        # representing the same "no file" state.
        before = {
            f.name: str(getattr(tor, f.name))
            for f in TreasuryOperationRequest._meta.fields
            if f.name not in ("status", "cancelled_at", "updated_at")
        }

        cancel_treasury_request(tor, request=_request_for(self.requester))
        tor.refresh_from_db()

        for name, value in before.items():
            with self.subTest(field=name):
                self.assertEqual(str(getattr(tor, name)), value)

    def test_executed_by_and_wallet_transaction_never_touched(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        cancel_treasury_request(tor, request=_request_for(self.requester))
        tor.refresh_from_db()
        self.assertIsNone(tor.executed_by_id)
        self.assertIsNone(tor.executed_at)
        self.assertIsNone(tor.wallet_transaction_id)
        self.assertIsNone(tor.approved_by_id)
        self.assertIsNone(tor.rejected_by_id)


class NoOtherServiceCalledTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3e2_noservice_reviewer")
        self.requester = _make_submitter(username="o3e2_noservice_requester")

    def test_never_calls_execute_treasury_request(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch("simulator.treasury_requests.execute_treasury_request") as mock_execute:
            cancel_treasury_request(tor, request=_request_for(self.requester))
        mock_execute.assert_not_called()

    def test_never_calls_mark_treasury_execution_failed(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            cancel_treasury_request(tor, request=_request_for(self.requester))
        mock_mark_failed.assert_not_called()

    def test_never_calls_credit_or_debit_wallet(self):
        with patch("simulator.wallet_ledger.credit_wallet") as mock_credit, \
             patch("simulator.wallet_ledger.debit_wallet") as mock_debit:
            tor = _make_pending_request(requested_by=self.requester)
            cancel_treasury_request(tor, request=_request_for(self.requester))
        mock_credit.assert_not_called()
        mock_debit.assert_not_called()


class ConcurrencyEquivalentTests(TestCase):
    """
    Same discipline as O.3b-2's own ConcurrencyEquivalentTests: SQLite
    does not support real cross-thread row locking the way Postgres
    does, and TestCase's own wrapping transaction would prevent a
    second thread from observing an intermediate commit anyway — a
    real-threading test would be non-deterministic on this backend for
    no added confidence. Instead this proves the actually-relevant
    guarantee: a second call on the same request, after the first
    already transitioned it, always finds the status already changed
    (the post-lock recheck) and always fails explicitly — never a
    silent no-op, never a second successful audit trail.
    """

    def setUp(self):
        self.reviewer_a = _make_reviewer(username="o3e2_concurrent_a")
        self.reviewer_b = _make_reviewer(username="o3e2_concurrent_b")
        self.requester = make_user(username="o3e2_concurrent_requester")

    def test_two_actors_racing_to_cancel_only_one_wins(self):
        tor = _make_pending_request(requested_by=self.requester)

        first = cancel_treasury_request(tor, request=_request_for(self.reviewer_a))
        self.assertEqual(first.status, TreasuryOperationRequest.ST_CANCELLED)

        with self.assertRaises(TreasuryRequestNotPending):
            cancel_treasury_request(tor, request=_request_for(self.reviewer_b))

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_cancelled").count(), 1,
        )

    def test_cancel_after_approve_race_only_approval_wins(self):
        from simulator.treasury_requests import approve_treasury_request

        tor = _make_pending_request(requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer_a))

        with self.assertRaises(TreasuryRequestNotPending):
            cancel_treasury_request(tor, request=_request_for(self.reviewer_b))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_does_not_exist_propagates_for_deleted_request(self):
        tor = _make_pending_request(requested_by=self.requester)
        pk = tor.pk
        TreasuryOperationRequest.objects.filter(pk=pk).delete()

        with self.assertRaises(TreasuryOperationRequest.DoesNotExist):
            cancel_treasury_request(tor, request=_request_for(self.reviewer_a))


class ScopeAndSafetyTests(TestCase):

    def test_ast_confirms_no_financial_functions_in_cancel_treasury_request(self):
        import ast
        import inspect
        import textwrap

        from simulator import treasury_requests as module

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "execute_treasury_request", "mark_treasury_execution_failed",
        }
        forbidden_imports = {"wallet_ledger"}

        tree = ast.parse(textwrap.dedent(inspect.getsource(module.cancel_treasury_request)))
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.rsplit(".", 1)[-1])
                imported.update(a.name for a in node.names)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)

        self.assertFalse(forbidden_calls & called, f"found: {forbidden_calls & called}")
        self.assertFalse(forbidden_imports & imported, f"found: {forbidden_imports & imported}")

    def test_no_save_call_other_than_the_documented_one(self):
        import ast
        import inspect
        import textwrap

        from simulator import treasury_requests as module

        tree = ast.parse(textwrap.dedent(inspect.getsource(module.cancel_treasury_request)))
        save_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
        ]
        self.assertEqual(len(save_calls), 1)

    def test_uses_select_for_update(self):
        from django.db.models import QuerySet

        reviewer = _make_reviewer(username="o3e2_lock_spy_reviewer")
        requester = make_user(username="o3e2_lock_spy_requester")
        tor = _make_pending_request(requested_by=requester)

        original = QuerySet.select_for_update
        calls = []

        def spy(self, *args, **kwargs):
            calls.append((args, kwargs))
            return original(self, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            cancel_treasury_request(tor, request=_request_for(reviewer))

        self.assertTrue(len(calls) >= 1)

    def test_no_new_permission_used_beyond_submit_and_review(self):
        import ast
        import inspect
        import textwrap

        from simulator import treasury_requests as module

        tree = ast.parse(textwrap.dedent(inspect.getsource(module.cancel_treasury_request)))
        source = inspect.getsource(module.cancel_treasury_request)
        self.assertIn("TREASURY_SUBMIT_PERMISSION", source)
        self.assertIn("TREASURY_REVIEW_PERMISSION", source)
        self.assertNotIn("can_recover_treasury_execution", source)
        self.assertNotIn("can_execute_treasury_request", source)
