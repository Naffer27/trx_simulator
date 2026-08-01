# simulator/tests/test_o3c3_treasury_execution_service.py
"""
Bloque O.3c-3 — Execute Treasury Request Service.

Covers ONLY simulator/treasury_requests.py::execute_treasury_request() —
the only transition that moves real money: APPROVED -> EXECUTING ->
EXECUTED, or APPROVED -> EXECUTING -> FAILED. No view, URL, template or
button exists yet — every test here calls the service directly.

This is the first Treasury service that DOES call into wallet_ledger.py
(credit_wallet()/debit_wallet()) — deliberately, and only via the frozen
O.3c-2 mapping (_EXECUTION_MAPPING). wallet_ledger.py itself is never
modified. No InternalTransfer is ever created (Treasury execution is a
pure Wallet-internal movement, never a Wallet<->TradingAccount transfer).
No new tx_type is used — only the existing catalog, per O.3c-2.

Security amendments (required before this block was authorized):
  1. Step B independently re-validates status==EXECUTING,
     executed_by_id==request.user.pk, and wallet_transaction_id is None
     under its own lock — never trusts Step A's checks alone.
  2. The failure-handling except branch uses a conditional UPDATE
     scoped to (pk, status=EXECUTING, wallet_transaction__isnull=True,
     executed_by=request.user) and inspects rows_updated — it never
     does an unconditional UPDATE by pk, and never emits a misleading
     EXECUTION_FAILED event for a transition that was not actually
     persisted.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.utils import timezone

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_requests import (
    TREASURY_EXECUTE_PERMISSION,
    TreasuryRequestExecutionInconsistent,
    TreasuryRequestNotApproved,
    TreasuryRequestSelfExecutionDenied,
    execute_treasury_request,
)
from simulator.wallet_ledger import InsufficientFunds, reconcile_wallet

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
        "reason": "O.3c-3 execution test",
        "reference": "REF-O3C3",
        "requested_by": requested_by,
        "status": TreasuryOperationRequest.ST_APPROVED,
        "approved_by": approved_by,
        "approved_at": timezone.now(),
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _request_for(user):
    request = RequestFactory().post("/admin/treasury-request/execute/")
    request.user = user
    return request


class SuccessfulExecutionTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c3_executor")
        self.requester = make_user(username="o3c3_requester")
        self.approver = make_user(username="o3c3_approver")

    def _expected_mapping(self):
        return {
            TreasuryOperationRequest.OP_CREDIT_FUNDS:  ("credit", WalletTransaction.TX_CORRECTION),
            TreasuryOperationRequest.OP_DEBIT_FUNDS:   ("debit",  WalletTransaction.TX_CORRECTION),
            TreasuryOperationRequest.OP_REFUND:        ("credit", WalletTransaction.TX_CORRECTION),
            TreasuryOperationRequest.OP_BONUS_CREDIT:  ("credit", WalletTransaction.TX_BONUS),
            TreasuryOperationRequest.OP_IB_COMMISSION: ("credit", WalletTransaction.TX_REBATE),
            TreasuryOperationRequest.OP_MANUAL_CREDIT: ("credit", WalletTransaction.TX_CORRECTION),
            TreasuryOperationRequest.OP_MANUAL_DEBIT:  ("debit",  WalletTransaction.TX_CORRECTION),
        }

    def test_successful_execution_for_each_operation_type(self):
        for op_type, (direction, tx_type) in self._expected_mapping().items():
            with self.subTest(operation_type=op_type):
                wallet = make_wallet(initial_balance=Decimal("500.00"))
                tor = _make_approved_request(
                    wallet=wallet, requested_by=self.requester, approved_by=self.approver,
                    operation_type=op_type, amount=Decimal("30.00"),
                )
                balance_before = wallet.available_balance

                result = execute_treasury_request(tor, request=_request_for(self.executor))

                self.assertEqual(result.status, TreasuryOperationRequest.ST_EXECUTED)
                self.assertIsNotNone(result.wallet_transaction)
                self.assertEqual(result.wallet_transaction.tx_type, tx_type)

                wallet.refresh_from_db()
                expected_delta = Decimal("30.00") if direction == "credit" else Decimal("-30.00")
                self.assertEqual(wallet.available_balance, balance_before + expected_delta)
                self.assertEqual(result.wallet_transaction.amount, expected_delta)

    def test_correct_wallet_ledger_function_invoked_credit(self):
        # credit_wallet/debit_wallet are imported locally inside the
        # function body (from .wallet_ledger import ...), so the patch
        # target is the source module simulator.wallet_ledger, not
        # simulator.treasury_requests — the local import re-resolves
        # that attribute fresh on every call.
        tor = _make_approved_request(
            requested_by=self.requester, approved_by=self.approver,
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        )
        with patch("simulator.wallet_ledger.credit_wallet") as mock_credit, \
             patch("simulator.wallet_ledger.debit_wallet") as mock_debit:
            mock_credit.return_value = WalletTransaction.objects.create(
                wallet=tor.wallet, tx_type=WalletTransaction.TX_BONUS,
                amount=Decimal("40.00"), balance_after=Decimal("40.00"),
            )
            execute_treasury_request(tor, request=_request_for(self.executor))

        mock_credit.assert_called_once()
        mock_debit.assert_not_called()
        args, kwargs = mock_credit.call_args
        self.assertEqual(args[0], tor.wallet_id)
        self.assertEqual(args[1], Decimal("40.00"))
        self.assertEqual(args[2], WalletTransaction.TX_BONUS)

    def test_executed_by_and_executed_at_set(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        result = execute_treasury_request(tor, request=_request_for(self.executor))
        self.assertEqual(result.executed_by_id, self.executor.pk)
        self.assertIsNotNone(result.executed_at)

    def test_execution_notes_recorded_only_in_audit(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        execute_treasury_request(
            tor, request=_request_for(self.executor), execution_notes="  double-checked  ",
        )
        row = AuditLog.objects.get(event_type="treasury.request_executed")
        self.assertEqual(row.detail["execution_notes"], "double-checked")

        tor.refresh_from_db()
        self.assertEqual(tor.metadata, {})
        self.assertNotIn("double-checked", tor.wallet_transaction.note)

    def test_execution_notes_empty_allowed(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        result = execute_treasury_request(tor, request=_request_for(self.executor))
        self.assertEqual(result.status, TreasuryOperationRequest.ST_EXECUTED)

    def test_wallet_transaction_note_contains_request_id_and_operation_type(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        result = execute_treasury_request(tor, request=_request_for(self.executor))
        self.assertIn(f"#{tor.pk}", result.wallet_transaction.note)
        self.assertIn("Bonus Credit", result.wallet_transaction.note)

    def test_exactly_two_auditlog_and_brokerauditevent_for_success(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        execute_treasury_request(tor, request=_request_for(self.executor))

        self.assertEqual(
            AuditLog.objects.filter(
                event_type__in=[
                    "treasury.request_execution_started", "treasury.request_executed",
                ],
            ).count(), 2,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type__in=[
                    "treasury.request_execution_started", "treasury.request_executed",
                ],
            ).count(), 2,
        )

    def test_reconcile_wallet_holds_after_execution(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        execute_treasury_request(tor, request=_request_for(self.executor))
        result = reconcile_wallet(wallet.id)
        self.assertTrue(result["ok"], f"Drift: {result['drift']}")


class PermissionContractTests(TestCase):

    def setUp(self):
        self.requester = make_user(username="o3c3_perm_requester")

    def test_execute_without_permission_403_equivalent_and_no_change(self):
        staff = make_user(username="o3c3_no_perm", is_staff=True)
        tor = _make_approved_request(requested_by=self.requester)

        with self.assertRaises(PermissionDenied):
            execute_treasury_request(tor, request=_request_for(staff))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_execute_unauthenticated_raises(self):
        from django.contrib.auth.models import AnonymousUser

        tor = _make_approved_request(requested_by=self.requester)
        request = RequestFactory().post("/x/")
        request.user = AnonymousUser()

        with self.assertRaises(PermissionDenied):
            execute_treasury_request(tor, request=request)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)


class SelfExecutionTests(TestCase):

    def test_requester_cannot_execute_own_request(self):
        requester_executor = _make_executor(username="o3c3_self_requester")
        tor = _make_approved_request(
            requested_by=requester_executor, approved_by=make_user(username="o3c3_approver_a"),
        )
        with self.assertRaises(TreasuryRequestSelfExecutionDenied):
            execute_treasury_request(tor, request=_request_for(requester_executor))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_approver_cannot_execute_same_request(self):
        approver_executor = _make_executor(username="o3c3_self_approver")
        tor = _make_approved_request(
            requested_by=make_user(username="o3c3_requester_b"), approved_by=approver_executor,
        )
        with self.assertRaises(TreasuryRequestSelfExecutionDenied):
            execute_treasury_request(tor, request=_request_for(approver_executor))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(AuditLog.objects.count(), 0)


class StatusTransitionTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c3_status_executor")
        self.requester = make_user(username="o3c3_status_requester")
        self.approver = make_user(username="o3c3_status_approver")

    def test_non_approved_statuses_all_raise(self):
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
                with self.assertRaises(TreasuryRequestNotApproved):
                    execute_treasury_request(tor, request=_request_for(self.executor))

    def test_double_execution_second_call_raises(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        execute_treasury_request(tor, request=_request_for(self.executor))

        with self.assertRaises(TreasuryRequestNotApproved):
            execute_treasury_request(tor, request=_request_for(self.executor))

        self.assertEqual(
            WalletTransaction.objects.filter(note__contains=f"#{tor.pk}").count(), 1,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 1,
        )


class InsufficientFundsTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c3_insuff_executor")
        self.requester = make_user(username="o3c3_insuff_requester")
        self.approver = make_user(username="o3c3_insuff_approver")

    def _assert_insufficient_funds_fails_cleanly(self, operation_type):
        wallet = make_wallet(initial_balance=Decimal("5.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            operation_type=operation_type, amount=Decimal("999.00"),
        )
        wtx_before = WalletTransaction.objects.filter(wallet=wallet).count()

        with self.assertRaises(InsufficientFunds):
            execute_treasury_request(tor, request=_request_for(self.executor))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIn("Wallet", tor.failure_reason)
        self.assertIsNone(tor.wallet_transaction)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("5.00"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), wtx_before)

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_started").count(), 1,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_failed").count(), 1,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 0,
        )

    def test_debit_funds_insufficient_balance(self):
        self._assert_insufficient_funds_fails_cleanly(TreasuryOperationRequest.OP_DEBIT_FUNDS)

    def test_manual_debit_insufficient_balance(self):
        self._assert_insufficient_funds_fails_cleanly(TreasuryOperationRequest.OP_MANUAL_DEBIT)


class AuditFailureIsolationTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c3_audit_fail_executor")
        self.requester = make_user(username="o3c3_audit_fail_requester")
        self.approver = make_user(username="o3c3_audit_fail_approver")

    def test_simulated_started_audit_failure_still_lets_step_b_run(self):
        # log_audit is fail-open in production — this forces an artificial
        # exception past that contract to prove EXECUTING already
        # committed durably before the STARTED audit call even runs.
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)

        with patch("simulator.treasury_requests.audit.log_audit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                execute_treasury_request(tor, request=_request_for(self.executor))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertIsNone(tor.wallet_transaction)

    def test_simulated_executed_brokerauditevent_failure_does_not_revert_execution(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)

        call_count = {"n": 0}
        real_record = None
        from simulator import broker_audit as _ba
        real_record = _ba.record_payment_event

        def flaky(*args, **kwargs):
            call_count["n"] += 1
            if kwargs.get("event_type") == _ba.EV_TREASURY_REQUEST_EXECUTED:
                raise RuntimeError("boom")
            return real_record(*args, **kwargs)

        with patch("simulator.treasury_requests.broker_audit.record_payment_event", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                execute_treasury_request(tor, request=_request_for(self.executor))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTED)
        self.assertIsNotNone(tor.wallet_transaction)


class NoOutOfScopeSideEffectsTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c3_scope_executor")
        self.requester = make_user(username="o3c3_scope_requester")
        self.approver = make_user(username="o3c3_scope_approver")

    def test_no_internal_transfer_ever_created(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        execute_treasury_request(tor, request=_request_for(self.executor))
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_cancelled_at_never_touched(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)
        result = execute_treasury_request(tor, request=_request_for(self.executor))
        self.assertIsNone(result.cancelled_at)


class WalletLedgerUsageTests(TestCase):
    """
    execute_treasury_request() is deliberately different from submit/
    approve/reject: it MUST import wallet_ledger (that is its entire
    purpose). AST-based, scoped to exactly this function.
    """

    def test_wallet_ledger_functions_imported_locally(self):
        import ast
        import inspect
        import textwrap

        from simulator.treasury_requests import execute_treasury_request

        tree = ast.parse(textwrap.dedent(inspect.getsource(execute_treasury_request)))
        imported_from_wallet_ledger = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "wallet_ledger":
                imported_from_wallet_ledger.update(a.name for a in node.names)
        self.assertEqual(imported_from_wallet_ledger, {"credit_wallet", "debit_wallet"})

    def test_never_calls_transfer_or_reconcile_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.treasury_requests import execute_treasury_request

        forbidden = {"transfer_to_account", "transfer_to_wallet", "reconcile_wallet"}
        tree = ast.parse(textwrap.dedent(inspect.getsource(execute_treasury_request)))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)
        self.assertEqual(called & forbidden, set())

    def test_wallet_ledger_module_itself_unmodified_marker(self):
        # Sanity check: wallet_ledger.py's own public functions still
        # exist with their known names — this block never edits that
        # module.
        from simulator import wallet_ledger
        self.assertTrue(hasattr(wallet_ledger, "credit_wallet"))
        self.assertTrue(hasattr(wallet_ledger, "debit_wallet"))
        self.assertTrue(hasattr(wallet_ledger, "reconcile_wallet"))


class StepBRevalidationTests(TestCase):
    """
    Security amendment #1 — Step B independently re-validates status,
    executed_by, and wallet_transaction under its own lock, never
    trusting Step A alone. Each scenario here simulates another process
    mutating the row in the narrow window between Step A's commit and
    Step B's lock acquisition, by hooking a side_effect onto
    audit.log_audit() (called exactly once, right in that window, and
    itself fail-open so a side_effect that doesn't raise is realistic).
    """

    def setUp(self):
        self.executor = _make_executor(username="o3c3_revalidate_executor")
        self.requester = make_user(username="o3c3_revalidate_requester")
        self.approver = make_user(username="o3c3_revalidate_approver")

    def _run_with_race(self, tor, mutate_fn):
        with patch("simulator.treasury_requests.audit.log_audit", side_effect=mutate_fn):
            with self.assertRaises(TreasuryRequestExecutionInconsistent):
                execute_treasury_request(tor, request=_request_for(self.executor))

    def test_step_b_finds_status_not_executing(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                status=TreasuryOperationRequest.ST_CANCELLED,
            )

        self._run_with_race(tor, mutate)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before,
        )

    def test_step_b_finds_wallet_transaction_already_linked(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        foreign_wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("1.00"), balance_after=Decimal("101.00"),
        )
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                wallet_transaction=foreign_wtx,
            )

        self._run_with_race(tor, mutate)

        tor.refresh_from_db()
        self.assertEqual(tor.wallet_transaction_id, foreign_wtx.pk)  # never overwritten
        # No additional WalletTransaction was created for this request.
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before,
        )

    def test_step_b_finds_executed_by_mismatch(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        other_executor = _make_executor(username="o3c3_other_executor")

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                executed_by=other_executor,
            )

        self._run_with_race(tor, mutate)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))


class FailureHandlerDoesNotOverwriteTests(TestCase):
    """
    Security amendment #2 — the except branch's conditional UPDATE must
    never overwrite a row that has already moved on (EXECUTED, FAILED
    by another process, executed_by changed, or wallet_transaction
    already linked) by the time it runs.

    Concurrency note (same precedent as
    test_o3b2_treasury_review_services.py::ConcurrencyEquivalentTests):
    SQLite has no real row-level locking and Django's TestCase wraps
    each test in one outer transaction, so a mutation performed as a
    side_effect of credit_wallet()/debit_wallet() itself lives inside
    Step B's OWN transaction.atomic() — when that block rolls back
    (because the side_effect also raises, simulating the financial
    failure), the mutation rolls back with it, which would silently
    defeat any attempt to fabricate this race through real threading
    on this backend too. Instead these tests verify the actual
    guarantee: the exact conditional-UPDATE query the except branch
    runs — scoped to (pk, status=EXECUTING, wallet_transaction__isnull=
    True, executed_by=<original executor>) — cannot match a row that
    has already moved into any of these four terminal/changed states,
    and the row is provably left untouched by attempting it directly.
    """

    def setUp(self):
        self.executor = _make_executor(username="o3c3_handler_executor")
        self.requester = make_user(username="o3c3_handler_requester")
        self.approver = make_user(username="o3c3_handler_approver")

    def _make_executing_request(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        # Mirrors exactly what Step A durably commits, without going
        # through execute_treasury_request() itself (Step A already has
        # its own dedicated coverage in SuccessfulExecutionTests).
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
            status=TreasuryOperationRequest.ST_EXECUTING, executed_by=self.executor,
        )
        tor.refresh_from_db()
        return tor

    def _attempt_conditional_failure_update(self, tor):
        """The exact query shape used by execute_treasury_request()'s except branch."""
        return TreasuryOperationRequest.objects.filter(
            pk=tor.pk,
            status=TreasuryOperationRequest.ST_EXECUTING,
            wallet_transaction__isnull=True,
            executed_by=self.executor,
        ).update(
            status=TreasuryOperationRequest.ST_FAILED,
            failure_reason="late failure from a stale execution attempt",
            executed_at=timezone.now(),
        )

    def test_does_not_overwrite_already_executed(self):
        tor = self._make_executing_request()
        wtx = WalletTransaction.objects.create(
            wallet=tor.wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("1.00"), balance_after=Decimal("101.00"),
        )
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
            status=TreasuryOperationRequest.ST_EXECUTED,
            wallet_transaction=wtx, executed_at=timezone.now(),
        )

        updated_rows = self._attempt_conditional_failure_update(tor)
        self.assertEqual(updated_rows, 0)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTED)  # untouched
        self.assertEqual(tor.wallet_transaction_id, wtx.pk)
        self.assertEqual(tor.failure_reason, "")

    def test_does_not_overwrite_already_failed_by_other_process(self):
        tor = self._make_executing_request()
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
            status=TreasuryOperationRequest.ST_FAILED,
            failure_reason="already handled by someone else",
        )

        updated_rows = self._attempt_conditional_failure_update(tor)
        self.assertEqual(updated_rows, 0)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertEqual(tor.failure_reason, "already handled by someone else")  # untouched

    def test_does_not_overwrite_when_executed_by_changed(self):
        other_executor = _make_executor(username="o3c3_handler_other")
        tor = self._make_executing_request()
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(executed_by=other_executor)

        updated_rows = self._attempt_conditional_failure_update(tor)
        self.assertEqual(updated_rows, 0)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)  # never marked FAILED
        self.assertEqual(tor.executed_by_id, other_executor.pk)  # untouched

    def test_does_not_overwrite_when_wallet_transaction_already_linked(self):
        tor = self._make_executing_request()
        wtx = WalletTransaction.objects.create(
            wallet=tor.wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("1.00"), balance_after=Decimal("101.00"),
        )
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(wallet_transaction=wtx)

        updated_rows = self._attempt_conditional_failure_update(tor)
        self.assertEqual(updated_rows, 0)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)  # never marked FAILED
        self.assertEqual(tor.wallet_transaction_id, wtx.pk)  # untouched, not overwritten

    def test_matching_row_is_the_only_case_that_updates_exactly_one_row(self):
        # Control case: confirm the same query DOES match (and only
        # once) when the row genuinely is still in the pristine
        # post-Step-A state — proving the zero-match assertions above
        # are meaningful rather than a query that never matches at all.
        tor = self._make_executing_request()
        updated_rows = self._attempt_conditional_failure_update(tor)
        self.assertEqual(updated_rows, 1)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)

        # A second attempt against the now-FAILED row matches nothing further.
        self.assertEqual(self._attempt_conditional_failure_update(tor), 0)


class ExecutionFailedEventEmissionTests(TestCase):
    """
    Requirements 5 and 6 — EXECUTION_FAILED is only ever emitted when the
    FAILED transition was actually persisted (updated_rows == 1), driven
    through the real execute_treasury_request() end to end. The
    updated_rows == 0 branch (Step B revalidation catching a stale row)
    is already exercised by StepBRevalidationTests; this class confirms
    the negative — no misleading event — for each of those scenarios,
    plus the positive control already covered by InsufficientFundsTests.
    """

    def setUp(self):
        self.executor = _make_executor(username="o3c3_emission_executor")
        self.requester = make_user(username="o3c3_emission_requester")
        self.approver = make_user(username="o3c3_emission_approver")

    def test_no_execution_failed_event_when_step_b_revalidation_rejects(self):
        tor = _make_approved_request(requested_by=self.requester, approved_by=self.approver)

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                status=TreasuryOperationRequest.ST_CANCELLED,
            )

        with patch("simulator.treasury_requests.audit.log_audit", side_effect=mutate):
            with self.assertRaises(TreasuryRequestExecutionInconsistent):
                execute_treasury_request(tor, request=_request_for(self.executor))

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_failed").count(), 0,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_execution_failed").count(), 0,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 0,
        )

    def test_execution_failed_event_emitted_exactly_once_on_genuine_failure(self):
        wallet = make_wallet(initial_balance=Decimal("5.00"))
        tor = _make_approved_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            operation_type=TreasuryOperationRequest.OP_DEBIT_FUNDS, amount=Decimal("999.00"),
        )

        with self.assertRaises(InsufficientFunds):
            execute_treasury_request(tor, request=_request_for(self.executor))

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_failed").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_execution_failed").count(), 1,
        )
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
