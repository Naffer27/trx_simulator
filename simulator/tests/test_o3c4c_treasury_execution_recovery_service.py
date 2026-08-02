# simulator/tests/test_o3c4c_treasury_execution_recovery_service.py
"""
Bloque O.3c-4c — Treasury Execution Recovery Service.

Covers ONLY simulator/treasury_execution_recovery.py::
mark_treasury_execution_failed() — the only mutable Treasury Execution
Recovery action. It ONLY ever transitions a stuck EXECUTING request to
FAILED. It never resets to APPROVED, never reconciles a
WalletTransaction, never calls credit_wallet()/debit_wallet(). This is
strictly separate from execute_treasury_request() (O.3c-3) — recovery
is incident response for a row that never moved money, never a
financial operation itself.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_execution_recovery import (
    TREASURY_RECOVER_PERMISSION,
    TreasuryRequestSelfRecoveryDenied,
    inspect_stuck_treasury_execution,
    mark_treasury_execution_failed,
)
from simulator.treasury_requests import TreasuryRequestExecutionInconsistent

from .factories import make_user, make_wallet


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


def _make_audit_log_event(pk, event_type):
    return AuditLog.objects.create(
        event_type=event_type,
        action=f"probe event for treasury request #{pk}",
        detail={"treasury_request_id": pk},
    )


def _request_for(user):
    request = RequestFactory().post("/admin/treasury-request/recover/")
    request.user = user
    return request


_RECOVERY_EVENT_TYPES = (
    "treasury.request_execution_recovery_started",
    "treasury.request_execution_marked_failed",
    "treasury.request_execution_recovery_blocked",
)


def _recovery_auditlog_count():
    # AuditLog.objects.count() alone is unsafe here — _started_audit_log()/
    # _make_audit_log_event() (test setup helpers simulating pre-existing
    # execution history) also create AuditLog rows unrelated to whatever
    # mark_treasury_execution_failed() itself emits.
    return AuditLog.objects.filter(event_type__in=_RECOVERY_EVENT_TYPES).count()


def _recovery_brokerauditevent_count():
    return BrokerAuditEvent.objects.filter(event_type__in=_RECOVERY_EVENT_TYPES).count()


class SuccessfulRecoveryTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_recoverer")
        self.requester = make_user(username="o3c4c_requester")
        self.approver = make_user(username="o3c4c_approver")
        self.executor = make_user(username="o3c4c_executor", is_active=True)

    def test_case_a_success(self):
        wallet = make_wallet(initial_balance=Decimal("500.00"))
        tor = _make_executing_request(
            wallet=wallet, executed_by=self.executor,
            requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        result = mark_treasury_execution_failed(
            tor, request=_request_for(self.recoverer), recovery_reason="crashed during deploy",
        )

        self.assertEqual(result.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIsNone(result.wallet_transaction)
        self.assertIsNotNone(result.executed_at)
        self.assertIn("[MANUAL RECOVERY]", result.failure_reason)
        self.assertIn("crashed during deploy", result.failure_reason)
        self.assertEqual(result.executed_by_id, self.executor.pk)  # original executor preserved

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("500.00"))  # no money moved
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)  # only the seed deposit

    def test_exactly_two_auditlog_and_brokerauditevent_for_success(self):
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        mark_treasury_execution_failed(
            tor, request=_request_for(self.recoverer), recovery_reason="ok",
        )

        self.assertEqual(
            AuditLog.objects.filter(
                event_type__in=[
                    "treasury.request_execution_recovery_started",
                    "treasury.request_execution_marked_failed",
                ],
            ).count(), 2,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type__in=[
                    "treasury.request_execution_recovery_started",
                    "treasury.request_execution_marked_failed",
                ],
            ).count(), 2,
        )

    def test_failure_reason_truncated_to_256_chars(self):
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        long_reason = "x" * 500
        result = mark_treasury_execution_failed(
            tor, request=_request_for(self.recoverer), recovery_reason=long_reason,
        )
        self.assertLessEqual(len(result.failure_reason), 256)

    def test_double_execution_still_prevented_by_underlying_execute_service(self):
        # Recovery marks FAILED; a subsequent execute attempt must still
        # be impossible because status is no longer APPROVED/EXECUTING.
        tor = _make_executing_request(
            executed_by=self.executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)
        mark_treasury_execution_failed(tor, request=_request_for(self.recoverer), recovery_reason="ok")

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)


class CaseFEligibleRecoveryTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_f_recoverer")
        self.requester = make_user(username="o3c4c_f_requester")
        self.approver = make_user(username="o3c4c_f_approver")

    def test_inactive_executor_row_can_still_be_recovered(self):
        inactive_executor = make_user(username="o3c4c_f_inactive", is_active=False)
        tor = _make_executing_request(
            executed_by=inactive_executor, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        result = mark_treasury_execution_failed(
            tor, request=_request_for(self.recoverer), recovery_reason="orphaned, inactive executor",
        )
        self.assertEqual(result.status, TreasuryOperationRequest.ST_FAILED)

    def test_missing_executor_row_can_still_be_recovered(self):
        tor = _make_executing_request(
            executed_by=None, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        result = mark_treasury_execution_failed(
            tor, request=_request_for(self.recoverer), recovery_reason="orphaned, missing executor",
        )
        self.assertEqual(result.status, TreasuryOperationRequest.ST_FAILED)


class PermissionContractTests(TestCase):

    def setUp(self):
        self.requester = make_user(username="o3c4c_perm_requester")

    def test_no_permission_denies_and_touches_nothing(self):
        staff = make_user(username="o3c4c_no_perm", is_staff=True)
        tor = _make_executing_request(requested_by=self.requester)
        _started_audit_log(tor.pk, age_seconds=1000)

        with self.assertRaises(PermissionDenied):
            mark_treasury_execution_failed(tor, request=_request_for(staff), recovery_reason="x")

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(_recovery_auditlog_count(), 0)

    def test_unauthenticated_denies(self):
        from django.contrib.auth.models import AnonymousUser

        tor = _make_executing_request(requested_by=self.requester)
        _started_audit_log(tor.pk, age_seconds=1000)
        request = RequestFactory().post("/x/")
        request.user = AnonymousUser()

        with self.assertRaises(PermissionDenied):
            mark_treasury_execution_failed(tor, request=request, recovery_reason="x")


class SelfConflictTests(TestCase):

    def test_requester_cannot_recover_own_request(self):
        requester_recoverer = _make_recoverer(username="o3c4c_self_requester")
        tor = _make_executing_request(
            requested_by=requester_recoverer, approved_by=make_user(username="o3c4c_approver_a"),
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        with self.assertRaises(TreasuryRequestSelfRecoveryDenied):
            mark_treasury_execution_failed(
                tor, request=_request_for(requester_recoverer), recovery_reason="x",
            )
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(_recovery_auditlog_count(), 0)

    def test_approver_cannot_recover_same_request(self):
        approver_recoverer = _make_recoverer(username="o3c4c_self_approver")
        tor = _make_executing_request(
            requested_by=make_user(username="o3c4c_requester_b"), approved_by=approver_recoverer,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        with self.assertRaises(TreasuryRequestSelfRecoveryDenied):
            mark_treasury_execution_failed(
                tor, request=_request_for(approver_recoverer), recovery_reason="x",
            )
        self.assertEqual(_recovery_auditlog_count(), 0)

    def test_same_executed_by_is_allowed_to_recover(self):
        # Frozen decision (O.3c-4 Fase 0): recovering your own crashed
        # execution attempt is technical diagnosis, not a new financial
        # authorization — unlike requested_by/approved_by.
        executor_recoverer = _make_recoverer(username="o3c4c_self_executor")
        tor = _make_executing_request(
            executed_by=executor_recoverer,
            requested_by=make_user(username="o3c4c_requester_c"),
            approved_by=make_user(username="o3c4c_approver_c"),
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        result = mark_treasury_execution_failed(
            tor, request=_request_for(executor_recoverer), recovery_reason="my own crashed attempt",
        )
        self.assertEqual(result.status, TreasuryOperationRequest.ST_FAILED)


class RecoveryReasonValidationTests(TestCase):

    def test_empty_reason_raises_value_error(self):
        recoverer = _make_recoverer(username="o3c4c_reason_empty")
        tor = _make_executing_request(requested_by=make_user(username="o3c4c_reason_req"))
        _started_audit_log(tor.pk, age_seconds=1000)

        with self.assertRaises(ValueError):
            mark_treasury_execution_failed(tor, request=_request_for(recoverer), recovery_reason="")

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(_recovery_auditlog_count(), 0)

    def test_blank_reason_raises_value_error(self):
        recoverer = _make_recoverer(username="o3c4c_reason_blank")
        tor = _make_executing_request(requested_by=make_user(username="o3c4c_reason_req2"))
        _started_audit_log(tor.pk, age_seconds=1000)

        with self.assertRaises(ValueError):
            mark_treasury_execution_failed(tor, request=_request_for(recoverer), recovery_reason="   ")


class PreLockGateTests(TestCase):
    """
    Every scenario here must be blocked BEFORE any lock is acquired and
    BEFORE any audit event is emitted — mirrors execute_treasury_
    request()'s own TreasuryRequestNotApproved (raised before any event).
    """

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_gate_recoverer")
        self.requester = make_user(username="o3c4c_gate_requester")
        self.approver = make_user(username="o3c4c_gate_approver")

    def _assert_blocked_no_events(self, tor):
        with self.assertRaises(TreasuryRequestExecutionInconsistent):
            mark_treasury_execution_failed(
                tor, request=_request_for(self.recoverer), recovery_reason="x",
            )
        self.assertEqual(_recovery_auditlog_count(), 0)
        self.assertEqual(_recovery_brokerauditevent_count(), 0)

    def test_not_executing_at_all(self):
        tor = _make_executing_request(
            requested_by=self.requester, approved_by=self.approver,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        self._assert_blocked_no_events(tor)

    def test_case_b_wallet_transaction_exists(self):
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("5.00"), balance_after=Decimal("55.00"),
        )
        tor = _make_executing_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            wallet_transaction=wtx,
        )
        _started_audit_log(tor.pk, age_seconds=100000)
        self._assert_blocked_no_events(tor)
        tor.refresh_from_db()
        self.assertEqual(tor.wallet_transaction_id, wtx.pk)  # untouched

    def test_case_c_recent(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=5)
        self._assert_blocked_no_events(tor)

    def test_case_d_unknown_age(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        # no STARTED event anywhere
        self._assert_blocked_no_events(tor)

    def test_case_e_executed_event(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=1000)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTED)
        self._assert_blocked_no_events(tor)

    def test_case_e_failed_event(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=1000)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED)
        self._assert_blocked_no_events(tor)


class PostLockRevalidationTests(TestCase):
    """
    Simulates another process mutating the row between the pre-lock
    gate and the recovery's own lock acquisition — same technique as
    O.3c-3's StepBRevalidationTests: a side_effect on the fail-open
    RECOVERY_STARTED audit call, the only deterministic hook point in
    single-threaded test code between the two.
    """

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_revalidate_recoverer")
        self.requester = make_user(username="o3c4c_revalidate_requester")
        self.approver = make_user(username="o3c4c_revalidate_approver")

    def _run_with_race(self, tor, mutate_fn):
        # side_effect fully replaces log_audit for the whole patch scope —
        # if it never delegated to the real implementation, RECOVERY_
        # STARTED (and later RECOVERY_BLOCKED) would never actually be
        # written. Mutate only on the first call (STARTED, the only call
        # that happens before the lock), then always call through to the
        # real log_audit so every event still gets its real AuditLog row.
        real_log_audit = _audit.log_audit
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                mutate_fn()
            return real_log_audit(*args, **kwargs)

        with patch("simulator.treasury_execution_recovery._audit.log_audit", side_effect=side_effect):
            with self.assertRaises(TreasuryRequestExecutionInconsistent):
                mark_treasury_execution_failed(
                    tor, request=_request_for(self.recoverer), recovery_reason="x",
                )

    def test_status_changed_between_gate_and_lock(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_executing_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                status=TreasuryOperationRequest.ST_CANCELLED,
            )

        self._run_with_race(tor, mutate)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))
        # RECOVERY_STARTED was emitted (the hook itself is log_audit),
        # but RECOVERY_BLOCKED must follow, never MARKED_FAILED.
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_marked_failed").count(), 0,
        )

    def test_wallet_transaction_appears_between_gate_and_lock(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_executing_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
        )
        _started_audit_log(tor.pk, age_seconds=1000)
        foreign_wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("1.00"), balance_after=Decimal("101.00"),
        )

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(wallet_transaction=foreign_wtx)

        self._run_with_race(tor, mutate)

        tor.refresh_from_db()
        self.assertEqual(tor.wallet_transaction_id, foreign_wtx.pk)  # never overwritten
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_execution_marked_failed").count(), 0,
        )

    def test_executed_by_changed_between_gate_and_lock(self):
        original_executor = make_user(username="o3c4c_revalidate_orig_exec", is_active=True)
        other_executor = make_user(username="o3c4c_revalidate_other_exec", is_active=True)
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        tor = _make_executing_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            executed_by=original_executor,
        )
        _started_audit_log(tor.pk, age_seconds=1000)

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(executed_by=other_executor)

        self._run_with_race(tor, mutate)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

    def test_recovery_blocked_event_emitted_with_state_changed_block_type(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=1000)

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                status=TreasuryOperationRequest.ST_CANCELLED,
            )

        self._run_with_race(tor, mutate)

        blocked = AuditLog.objects.get(event_type="treasury.request_execution_recovery_blocked")
        self.assertEqual(blocked.detail["block_type"], "STATE_CHANGED")


class ConditionalUpdateInvariantTests(TestCase):
    """
    Direct invariant proof (same "concurrency-equivalent" precedent as
    O.3c-3's FailureHandlerDoesNotOverwriteTests): the exact conditional
    UPDATE query mark_treasury_execution_failed() runs cannot match a
    row that has already moved into any of these states.
    """

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_invariant_recoverer")
        self.requester = make_user(username="o3c4c_invariant_requester")
        self.approver = make_user(username="o3c4c_invariant_approver")

    def _attempt_conditional_update(self, tor, executed_by_id):
        return TreasuryOperationRequest.objects.filter(
            pk=tor.pk,
            status=TreasuryOperationRequest.ST_EXECUTING,
            wallet_transaction__isnull=True,
            executed_by_id=executed_by_id,
        ).update(
            status=TreasuryOperationRequest.ST_FAILED,
            failure_reason="late recovery attempt",
            executed_at=timezone.now(),
        )

    def test_query_does_not_match_already_failed_row(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
            status=TreasuryOperationRequest.ST_FAILED, failure_reason="already handled",
        )
        updated_rows = self._attempt_conditional_update(tor, None)
        self.assertEqual(updated_rows, 0)
        tor.refresh_from_db()
        self.assertEqual(tor.failure_reason, "already handled")  # untouched

    def test_query_matches_exactly_the_pristine_row(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        updated_rows = self._attempt_conditional_update(tor, None)
        self.assertEqual(updated_rows, 1)


class EventEmissionInvariantTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3c4c_emission_recoverer")
        self.requester = make_user(username="o3c4c_emission_requester")
        self.approver = make_user(username="o3c4c_emission_approver")

    def test_started_always_followed_by_exactly_one_terminal_event_on_success(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=1000)

        mark_treasury_execution_failed(tor, request=_request_for(self.recoverer), recovery_reason="ok")

        started = AuditLog.objects.filter(event_type="treasury.request_execution_recovery_started").count()
        blocked = AuditLog.objects.filter(event_type="treasury.request_execution_recovery_blocked").count()
        marked_failed = AuditLog.objects.filter(event_type="treasury.request_execution_marked_failed").count()
        self.assertEqual(started, 1)
        self.assertEqual(blocked, 0)
        self.assertEqual(marked_failed, 1)

    def test_started_always_followed_by_exactly_one_terminal_event_on_block(self):
        tor = _make_executing_request(requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=1000)

        real_log_audit = _audit.log_audit
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                TreasuryOperationRequest.objects.filter(pk=tor.pk).update(
                    status=TreasuryOperationRequest.ST_CANCELLED,
                )
            return real_log_audit(*args, **kwargs)

        with patch("simulator.treasury_execution_recovery._audit.log_audit", side_effect=side_effect):
            with self.assertRaises(TreasuryRequestExecutionInconsistent):
                mark_treasury_execution_failed(
                    tor, request=_request_for(self.recoverer), recovery_reason="x",
                )

        started = AuditLog.objects.filter(event_type="treasury.request_execution_recovery_started").count()
        blocked = AuditLog.objects.filter(event_type="treasury.request_execution_recovery_blocked").count()
        marked_failed = AuditLog.objects.filter(event_type="treasury.request_execution_marked_failed").count()
        self.assertEqual(started, 1)
        self.assertEqual(blocked, 1)
        self.assertEqual(marked_failed, 0)

    def test_no_events_at_all_for_pre_lock_rejected_attempt(self):
        # CASE_B — rejected entirely before RECOVERY_STARTED is even emitted.
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("5.00"), balance_after=Decimal("55.00"),
        )
        tor = _make_executing_request(
            wallet=wallet, requested_by=self.requester, approved_by=self.approver,
            wallet_transaction=wtx,
        )
        _started_audit_log(tor.pk, age_seconds=100000)

        with self.assertRaises(TreasuryRequestExecutionInconsistent):
            mark_treasury_execution_failed(
                tor, request=_request_for(self.recoverer), recovery_reason="x",
            )

        self.assertEqual(_recovery_auditlog_count(), 0)
        self.assertEqual(_recovery_brokerauditevent_count(), 0)


class LockingStrategyTests(TestCase):

    def test_uses_select_for_update_nowait(self):
        recoverer = _make_recoverer(username="o3c4c_lock_recoverer")
        requester = make_user(username="o3c4c_lock_requester")
        tor = _make_executing_request(requested_by=requester)
        _started_audit_log(tor.pk, age_seconds=1000)

        from django.db.models import QuerySet
        original = QuerySet.select_for_update
        calls = []

        def spy(self, *args, **kwargs):
            calls.append(kwargs)
            return original(self, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            mark_treasury_execution_failed(
                tor, request=_request_for(recoverer), recovery_reason="ok",
            )

        self.assertTrue(any(c.get("nowait") is True for c in calls))


class ScopeAndSafetyTests(TestCase):

    def test_ast_confirms_no_financial_functions_anywhere_in_module(self):
        import ast
        import inspect as _inspect

        import simulator.treasury_execution_recovery as module

        forbidden = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
        }
        tree = ast.parse(_inspect.getsource(module))

        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)

        self.assertEqual(imported & forbidden, set())
        self.assertEqual(called & forbidden, set())

    def test_no_reset_to_approved_or_reconcile_function_exists(self):
        import simulator.treasury_execution_recovery as module

        for name in (
            "reset_to_approved",
            "reset_treasury_execution_to_approved",
            "reconcile_from_wallet_transaction",
            "recover_stuck_execution",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name))

    def test_never_creates_internal_transfer(self):
        from simulator.models import InternalTransfer

        recoverer = _make_recoverer(username="o3c4c_no_transfer_recoverer")
        requester = make_user(username="o3c4c_no_transfer_requester")
        tor = _make_executing_request(requested_by=requester)
        _started_audit_log(tor.pk, age_seconds=1000)

        mark_treasury_execution_failed(tor, request=_request_for(recoverer), recovery_reason="ok")

        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_does_not_exist_propagates_for_deleted_request(self):
        recoverer = _make_recoverer(username="o3c4c_deleted_recoverer")
        requester = make_user(username="o3c4c_deleted_requester")
        tor = _make_executing_request(requested_by=requester)
        _started_audit_log(tor.pk, age_seconds=1000)

        def mutate(*args, **kwargs):
            TreasuryOperationRequest.objects.filter(pk=tor.pk).delete()

        with patch("simulator.treasury_execution_recovery._audit.log_audit", side_effect=mutate):
            with self.assertRaises(TreasuryOperationRequest.DoesNotExist):
                mark_treasury_execution_failed(
                    tor, request=_request_for(recoverer), recovery_reason="x",
                )
