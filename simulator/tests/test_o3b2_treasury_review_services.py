# simulator/tests/test_o3b2_treasury_review_services.py
"""
Bloque O.3b-2 — Treasury Request Review Services.

Covers ONLY simulator/treasury_requests.py::approve_treasury_request()
and reject_treasury_request() (plus the two new exceptions,
TreasuryRequestNotPending / TreasuryRequestSelfReviewDenied) — the two
PENDING -> APPROVED / PENDING -> REJECTED transitions.

No view, URL, template, button or confirmation screen exists yet — every
test here calls the services directly. Neither service moves money:
no WalletTransaction/InternalTransfer is ever created, no Wallet balance
field ever changes, wallet_ledger.py and funded_payouts.py are never
called, and executed_by/executed_at/wallet_transaction/cancelled_at are
never touched. Nothing here transitions to EXECUTING — that is a later
block.
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
    TreasuryRequestNotPending,
    TreasuryRequestSelfReviewDenied,
    approve_treasury_request,
    reject_treasury_request,
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
        "amount": Decimal("50.00"),
        "reason": "O.3b-2 service test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _request_for(user):
    request = RequestFactory().post("/admin/treasury-request/review/")
    request.user = user
    return request


class SuccessfulApprovalTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_reviewer_ok")
        self.requester = make_user(username="o3b2_requester_ok")
        self.tor = _make_pending_request(requested_by=self.requester)

    def test_approval_succeeds(self):
        result = approve_treasury_request(self.tor, request=_request_for(self.reviewer))
        self.assertEqual(result.status, TreasuryOperationRequest.ST_APPROVED)

    def test_approved_by_and_approved_at_set(self):
        result = approve_treasury_request(self.tor, request=_request_for(self.reviewer))
        self.assertEqual(result.approved_by_id, self.reviewer.pk)
        self.assertIsNotNone(result.approved_at)

    def test_review_notes_stripped_and_not_on_model(self):
        approve_treasury_request(
            self.tor, request=_request_for(self.reviewer), review_notes="  looks fine  ",
        )
        row = AuditLog.objects.get(event_type="treasury.request_approved")
        self.assertEqual(row.detail["review_notes"], "looks fine")

        self.tor.refresh_from_db()
        self.assertEqual(self.tor.metadata, {})
        self.assertFalse(hasattr(self.tor, "review_notes"))

    def test_review_notes_empty_allowed(self):
        result = approve_treasury_request(self.tor, request=_request_for(self.reviewer))
        self.assertEqual(result.status, TreasuryOperationRequest.ST_APPROVED)
        row = AuditLog.objects.get(event_type="treasury.request_approved")
        self.assertEqual(row.detail["review_notes"], "")

    def test_returns_locked_updated_instance(self):
        result = approve_treasury_request(self.tor, request=_request_for(self.reviewer))
        self.assertEqual(result.pk, self.tor.pk)
        self.assertEqual(result.status, TreasuryOperationRequest.ST_APPROVED)

    def test_rejection_fields_remain_untouched_after_approval(self):
        # O.3b pre-commit checkpoint §2 — a successful approve() must never
        # populate any rejected_*/rejection_reason field.
        result = approve_treasury_request(self.tor, request=_request_for(self.reviewer))
        self.assertIsNone(result.rejected_by)
        self.assertIsNone(result.rejected_at)
        self.assertEqual(result.rejection_reason, "")


class SuccessfulRejectionTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_reviewer_rej")
        self.requester = make_user(username="o3b2_requester_rej")
        self.tor = _make_pending_request(requested_by=self.requester)

    def test_rejection_succeeds(self):
        result = reject_treasury_request(
            self.tor, "insufficient documentation", request=_request_for(self.reviewer),
        )
        self.assertEqual(result.status, TreasuryOperationRequest.ST_REJECTED)

    def test_rejected_by_at_and_reason_set(self):
        result = reject_treasury_request(
            self.tor, "insufficient documentation", request=_request_for(self.reviewer),
        )
        self.assertEqual(result.rejected_by_id, self.reviewer.pk)
        self.assertIsNotNone(result.rejected_at)
        self.assertEqual(result.rejection_reason, "insufficient documentation")

    def test_review_notes_stripped_and_not_on_model(self):
        reject_treasury_request(
            self.tor, "bad reference", request=_request_for(self.reviewer),
            review_notes="  flagged for follow-up  ",
        )
        row = AuditLog.objects.get(event_type="treasury.request_rejected")
        self.assertEqual(row.detail["review_notes"], "flagged for follow-up")

        self.tor.refresh_from_db()
        self.assertEqual(self.tor.metadata, {})

    def test_review_notes_empty_allowed(self):
        result = reject_treasury_request(
            self.tor, "bad reference", request=_request_for(self.reviewer),
        )
        self.assertEqual(result.status, TreasuryOperationRequest.ST_REJECTED)
        row = AuditLog.objects.get(event_type="treasury.request_rejected")
        self.assertEqual(row.detail["review_notes"], "")

    def test_rejection_reason_is_stripped(self):
        result = reject_treasury_request(
            self.tor, "  padded reason  ", request=_request_for(self.reviewer),
        )
        self.assertEqual(result.rejection_reason, "padded reason")

    def test_approval_fields_remain_untouched_after_rejection(self):
        # O.3b pre-commit checkpoint §2 — a successful reject() must never
        # populate approved_by/approved_at.
        result = reject_treasury_request(
            self.tor, "insufficient documentation", request=_request_for(self.reviewer),
        )
        self.assertIsNone(result.approved_by)
        self.assertIsNone(result.approved_at)


class PermissionContractTests(TestCase):

    def setUp(self):
        self.requester = make_user(username="o3b2_requester_perm")

    def test_approve_without_permission_raises_and_no_change(self):
        staff = make_user(username="o3b2_no_perm_approve", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)

        with self.assertRaises(PermissionDenied):
            approve_treasury_request(tor, request=_request_for(staff))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_reject_without_permission_raises_and_no_change(self):
        staff = make_user(username="o3b2_no_perm_reject", is_staff=True)
        tor = _make_pending_request(requested_by=self.requester)

        with self.assertRaises(PermissionDenied):
            reject_treasury_request(tor, "reason", request=_request_for(staff))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_approve_unauthenticated_raises(self):
        from django.contrib.auth.models import AnonymousUser

        tor = _make_pending_request(requested_by=self.requester)
        request = RequestFactory().post("/x/")
        request.user = AnonymousUser()

        with self.assertRaises(PermissionDenied):
            approve_treasury_request(tor, request=request)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_reject_unauthenticated_raises(self):
        from django.contrib.auth.models import AnonymousUser

        tor = _make_pending_request(requested_by=self.requester)
        request = RequestFactory().post("/x/")
        request.user = AnonymousUser()

        with self.assertRaises(PermissionDenied):
            reject_treasury_request(tor, "reason", request=request)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)


class SelfReviewTests(TestCase):

    def test_requester_cannot_approve_own_request(self):
        requester = _make_reviewer(username="o3b2_self_approve")
        tor = _make_pending_request(requested_by=requester)

        with self.assertRaises(TreasuryRequestSelfReviewDenied):
            approve_treasury_request(tor, request=_request_for(requester))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_requester_cannot_reject_own_request(self):
        requester = _make_reviewer(username="o3b2_self_reject")
        tor = _make_pending_request(requested_by=requester)

        with self.assertRaises(TreasuryRequestSelfReviewDenied):
            reject_treasury_request(tor, "reason", request=_request_for(requester))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)


class StatusTransitionTests(TestCase):
    """
    11-15 — solicitud no PENDING, doble aprobación/rechazo, aprobar tras
    rechazo y viceversa. Todas comparten la misma garantía: la segunda
    operación sobre una solicitud ya no-PENDING falla explícitamente
    (TreasuryRequestNotPending), nunca un no-op silencioso.
    """

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_status_reviewer")
        self.requester = make_user(username="o3b2_status_requester")

    def test_approve_non_pending_request_raises(self):
        tor = _make_pending_request(
            requested_by=self.requester, status=TreasuryOperationRequest.ST_APPROVED,
        )
        with self.assertRaises(TreasuryRequestNotPending):
            approve_treasury_request(tor, request=_request_for(self.reviewer))

    def test_reject_non_pending_request_raises(self):
        tor = _make_pending_request(
            requested_by=self.requester, status=TreasuryOperationRequest.ST_REJECTED,
        )
        with self.assertRaises(TreasuryRequestNotPending):
            reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))

    def test_double_approval_second_call_raises(self):
        tor = _make_pending_request(requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer))

        with self.assertRaises(TreasuryRequestNotPending):
            approve_treasury_request(tor, request=_request_for(self.reviewer))

        # Exactly one successful transition was audited — the second
        # (failed) call never reached the audit calls.
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_approved").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_approved").count(), 1,
        )

    def test_double_rejection_second_call_raises(self):
        tor = _make_pending_request(requested_by=self.requester)
        reject_treasury_request(tor, "first reason", request=_request_for(self.reviewer))

        with self.assertRaises(TreasuryRequestNotPending):
            reject_treasury_request(tor, "second reason", request=_request_for(self.reviewer))

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_rejected").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_rejected").count(), 1,
        )
        tor.refresh_from_db()
        self.assertEqual(tor.rejection_reason, "first reason")

    def test_approve_after_reject_raises(self):
        tor = _make_pending_request(requested_by=self.requester)
        reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))

        with self.assertRaises(TreasuryRequestNotPending):
            approve_treasury_request(tor, request=_request_for(self.reviewer))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_REJECTED)
        self.assertIsNone(tor.approved_by)

    def test_reject_after_approve_raises(self):
        tor = _make_pending_request(requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer))

        with self.assertRaises(TreasuryRequestNotPending):
            reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertIsNone(tor.rejected_by)
        self.assertEqual(tor.rejection_reason, "")


class RejectionReasonValidationTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_reason_validation")
        self.requester = make_user(username="o3b2_reason_requester")

    def test_empty_rejection_reason_raises_value_error(self):
        tor = _make_pending_request(requested_by=self.requester)
        with self.assertRaises(ValueError):
            reject_treasury_request(tor, "", request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_whitespace_only_rejection_reason_raises_value_error(self):
        tor = _make_pending_request(requested_by=self.requester)
        with self.assertRaises(ValueError):
            reject_treasury_request(tor, "     ", request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_none_rejection_reason_raises_value_error(self):
        tor = _make_pending_request(requested_by=self.requester)
        with self.assertRaises(ValueError):
            reject_treasury_request(tor, None, request=_request_for(self.reviewer))


class AuditContentTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_audit_reviewer")
        self.requester = make_user(username="o3b2_audit_requester")

    def test_approve_auditlog_and_brokerauditevent_content(self):
        tor = _make_pending_request(requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer), review_notes="ok")

        audit_row = AuditLog.objects.get(event_type="treasury.request_approved")
        self.assertEqual(audit_row.detail["treasury_request_id"], tor.pk)
        self.assertEqual(audit_row.detail["operation_type"], tor.operation_type)
        self.assertEqual(audit_row.detail["wallet_id"], tor.wallet_id)
        self.assertEqual(audit_row.detail["wallet_user_id"], tor.wallet.user_id)
        self.assertEqual(audit_row.detail["amount"], "50.00")
        self.assertEqual(audit_row.detail["requested_by_id"], self.requester.pk)
        self.assertEqual(audit_row.detail["approved_by_id"], self.reviewer.pk)
        self.assertEqual(audit_row.detail["review_notes"], "ok")
        self.assertEqual(audit_row.detail["previous_status"], "PENDING")
        self.assertEqual(audit_row.detail["new_status"], "APPROVED")

        broker_row = BrokerAuditEvent.objects.get(event_type="treasury.request_approved")
        self.assertEqual(broker_row.metadata["treasury_operation_request_id"], tor.pk)
        self.assertEqual(broker_row.metadata["operation_type"], tor.operation_type)
        self.assertEqual(broker_row.metadata["wallet_id"], tor.wallet_id)
        self.assertEqual(broker_row.metadata["wallet_user_id"], tor.wallet.user_id)
        self.assertEqual(broker_row.metadata["amount"], "50.00")
        self.assertEqual(broker_row.metadata["requested_by_id"], self.requester.pk)
        self.assertEqual(broker_row.metadata["approved_by_id"], self.reviewer.pk)
        self.assertEqual(broker_row.metadata["review_notes"], "ok")
        self.assertEqual(broker_row.metadata["previous_status"], "PENDING")
        self.assertEqual(broker_row.metadata["status"], "APPROVED")
        self.assertEqual(broker_row.severity, "INFO")
        self.assertEqual(broker_row.actor_type, "STAFF")
        self.assertEqual(broker_row.actor_id, self.reviewer.pk)
        self.assertEqual(broker_row.source_module, "simulator.treasury_requests")
        self.assertEqual(broker_row.category, "PAYMENTS")

    def test_reject_auditlog_and_brokerauditevent_content(self):
        tor = _make_pending_request(requested_by=self.requester)
        reject_treasury_request(
            tor, "duplicate ticket", request=_request_for(self.reviewer),
            review_notes="see ticket 42",
        )

        audit_row = AuditLog.objects.get(event_type="treasury.request_rejected")
        self.assertEqual(audit_row.detail["treasury_request_id"], tor.pk)
        self.assertEqual(audit_row.detail["operation_type"], tor.operation_type)
        self.assertEqual(audit_row.detail["wallet_id"], tor.wallet_id)
        self.assertEqual(audit_row.detail["wallet_user_id"], tor.wallet.user_id)
        self.assertEqual(audit_row.detail["amount"], "50.00")
        self.assertEqual(audit_row.detail["requested_by_id"], self.requester.pk)
        self.assertEqual(audit_row.detail["rejected_by_id"], self.reviewer.pk)
        self.assertEqual(audit_row.detail["rejection_reason"], "duplicate ticket")
        self.assertEqual(audit_row.detail["review_notes"], "see ticket 42")
        self.assertEqual(audit_row.detail["previous_status"], "PENDING")
        self.assertEqual(audit_row.detail["new_status"], "REJECTED")

        broker_row = BrokerAuditEvent.objects.get(event_type="treasury.request_rejected")
        self.assertEqual(broker_row.metadata["rejection_reason"], "duplicate ticket")
        self.assertEqual(broker_row.metadata["review_notes"], "see ticket 42")
        self.assertEqual(broker_row.metadata["status"], "REJECTED")
        self.assertEqual(broker_row.severity, "WARNING")
        self.assertEqual(broker_row.actor_type, "STAFF")
        self.assertEqual(broker_row.actor_id, self.reviewer.pk)
        self.assertEqual(broker_row.source_module, "simulator.treasury_requests")

    def test_exactly_one_auditlog_and_brokerauditevent_per_approval(self):
        tor = _make_pending_request(requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer))
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.count(), 1)

    def test_exactly_one_auditlog_and_brokerauditevent_per_rejection(self):
        tor = _make_pending_request(requested_by=self.requester)
        reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.count(), 1)


class AuditFailureIsolationTests(TestCase):
    """
    19-20 — un fallo simulado en la escritura de auditoría no revierte
    la transición ya commiteada. Mismo razonamiento que O.3a-4: en
    producción log_audit()/record_event() son fail-open por su propio
    contrato (nunca lanzan); este test fuerza artificialmente una
    excepción vía mock para demostrar la propiedad transaccional en sí
    — la transición ya está commiteada antes de que se invoque
    cualquiera de los dos helpers de auditoría.
    """

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_audit_fail_reviewer")
        self.requester = make_user(username="o3b2_audit_fail_requester")

    def test_simulated_auditlog_failure_does_not_revert_approval(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch("simulator.treasury_requests.audit.log_audit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                approve_treasury_request(tor, request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_simulated_brokerauditevent_failure_does_not_revert_rejection(self):
        tor = _make_pending_request(requested_by=self.requester)
        with patch(
            "simulator.treasury_requests.broker_audit.record_payment_event",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_REJECTED)
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_rejected").count(), 1,
        )


class NoFinancialSideEffectsTests(TestCase):

    def setUp(self):
        self.reviewer = _make_reviewer(username="o3b2_no_money_reviewer")
        self.requester = make_user(username="o3b2_no_money_requester")
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))

    def test_no_wallet_transaction_created_by_approval(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        approve_treasury_request(tor, request=_request_for(self.reviewer))
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(before, after)

    def test_no_wallet_transaction_created_by_rejection(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(before, after)

    def test_no_internal_transfer_created(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer))
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_balances_unchanged_after_approval(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        approve_treasury_request(tor, request=_request_for(self.reviewer))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_balances_unchanged_after_rejection(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        avail_before, pending_before = self.wallet.available_balance, self.wallet.pending_balance
        reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_execution_and_cancellation_fields_untouched_by_approval(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        approve_treasury_request(tor, request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertIsNone(tor.executed_by)
        self.assertIsNone(tor.executed_at)
        self.assertIsNone(tor.wallet_transaction)
        self.assertIsNone(tor.cancelled_at)
        self.assertEqual(tor.failure_reason, "")

    def test_execution_and_cancellation_fields_untouched_by_rejection(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        reject_treasury_request(tor, "reason", request=_request_for(self.reviewer))
        tor.refresh_from_db()
        self.assertIsNone(tor.executed_by)
        self.assertIsNone(tor.executed_at)
        self.assertIsNone(tor.wallet_transaction)
        self.assertIsNone(tor.cancelled_at)

    def test_neither_function_ever_reaches_executing_status(self):
        tor1 = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        result1 = approve_treasury_request(tor1, request=_request_for(self.reviewer))
        self.assertNotEqual(result1.status, TreasuryOperationRequest.ST_EXECUTING)

        tor2 = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        result2 = reject_treasury_request(tor2, "reason", request=_request_for(self.reviewer))
        self.assertNotEqual(result2.status, TreasuryOperationRequest.ST_EXECUTING)


class NoWalletLedgerOrFundedPayoutsUsageTests(TestCase):
    """
    24 — ninguno de los dos servicios llama wallet_ledger.py ni
    funded_payouts.py. AST-based y acotado exactamente a
    approve_treasury_request/reject_treasury_request — el módulo
    treasury_requests.py como archivo completo menciona ambos nombres
    en prosa dentro de su propio docstring (documentando lo que NO
    hace), así que una búsqueda de substring sobre el archivo entero
    daría un falso positivo.
    """

    def test_neither_function_imports_wallet_ledger_or_funded_payouts(self):
        import ast
        import inspect
        import textwrap

        from simulator.treasury_requests import approve_treasury_request, reject_treasury_request

        for fn in (approve_treasury_request, reject_treasury_request):
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

    def test_neither_function_calls_wallet_movement_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.treasury_requests import approve_treasury_request, reject_treasury_request

        forbidden = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
        }
        for fn in (approve_treasury_request, reject_treasury_request):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if name:
                        called.add(name)
            self.assertEqual(called & forbidden, set(), fn.__name__)


class ConcurrencyEquivalentTests(TestCase):
    """
    26 — simulación de concurrencia compatible con SQLite: SQLite no
    soporta bloqueo real a nivel de fila (select_for_update() no
    bloquea entre hilos como lo haría Postgres), y TestCase envuelve
    cada test en una transacción externa que impediría que un segundo
    hilo/conexión viera commits intermedios de todas formas — un test
    con threading real sería, en el mejor caso, no determinista sobre
    este backend.

    En su lugar, se prueba la garantía que realmente importa (idéntica
    en efecto observable a lo que produciría el lock bajo concurrencia
    real): una segunda llamada sobre la misma solicitud, después de que
    la primera ya transicionó su estado, siempre encuentra el status ya
    cambiado (el recheck posterior al lock) y siempre falla de forma
    explícita — nunca un no-op silencioso, nunca una doble auditoría
    exitosa. StatusTransitionTests ya cubre exactamente este contrato
    (doble aprobación, doble rechazo, aprobar-tras-rechazar,
    rechazar-tras-aprobar); esta clase lo nombra explícitamente como
    equivalente de concurrencia para que quede trazable en la suite.
    """

    def setUp(self):
        self.reviewer_a = _make_reviewer(username="o3b2_concurrent_a")
        self.reviewer_b = _make_reviewer(username="o3b2_concurrent_b")
        self.requester = make_user(username="o3b2_concurrent_requester")

    def test_two_reviewers_racing_to_approve_only_one_wins(self):
        tor = _make_pending_request(requested_by=self.requester)

        first = approve_treasury_request(tor, request=_request_for(self.reviewer_a))
        self.assertEqual(first.approved_by_id, self.reviewer_a.pk)

        with self.assertRaises(TreasuryRequestNotPending):
            approve_treasury_request(tor, request=_request_for(self.reviewer_b))

        tor.refresh_from_db()
        self.assertEqual(tor.approved_by_id, self.reviewer_a.pk)

    def test_two_reviewers_racing_approve_vs_reject_only_one_wins(self):
        tor = _make_pending_request(requested_by=self.requester)

        approve_treasury_request(tor, request=_request_for(self.reviewer_a))

        with self.assertRaises(TreasuryRequestNotPending):
            reject_treasury_request(tor, "too late", request=_request_for(self.reviewer_b))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(tor.rejection_reason, "")

    def test_does_not_exist_propagates_for_deleted_request(self):
        tor = _make_pending_request(requested_by=self.requester)
        pk = tor.pk
        TreasuryOperationRequest.objects.filter(pk=pk).delete()

        with self.assertRaises(TreasuryOperationRequest.DoesNotExist):
            approve_treasury_request(tor, request=_request_for(self.reviewer_a))
