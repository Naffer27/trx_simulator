# simulator/tests/test_o3c4b_treasury_execution_recovery_inspection.py
"""
Bloque O.3c-4b — Treasury Execution Recovery Inspection Service.

Covers ONLY simulator/treasury_execution_recovery.py::
inspect_stuck_treasury_execution() — a strictly read-only diagnostic.
No mutation, no AuditLog/BrokerAuditEvent write, no Wallet/
WalletTransaction touch, no wallet_ledger import anywhere in this
module. The mutable recovery action (mark_treasury_execution_failed())
is a later block, O.3c-4c, and does not exist yet.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_execution_recovery import (
    AGE_CONFIDENCE_AUDIT_LOG,
    AGE_CONFIDENCE_BROKER_AUDIT_EVENT,
    AGE_CONFIDENCE_UNKNOWN,
    CASE_A, CASE_B, CASE_C, CASE_D, CASE_E, CASE_F,
    inspect_stuck_treasury_execution,
)

from .factories import make_user, make_wallet


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by,
        wallet_transaction=wallet_transaction,
    )
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _make_audit_log_event(pk, event_type, age_seconds=None):
    created_at = timezone.now() - timedelta(seconds=age_seconds) if age_seconds is not None else timezone.now()
    return AuditLog.objects.create(
        event_type=event_type,
        action=f"probe event for treasury request #{pk}",
        detail={"treasury_request_id": pk},
        created_at=created_at,
    )


def _make_broker_audit_event(pk, event_type, age_seconds=None):
    obj = BrokerAuditEvent.objects.create(
        event_type=event_type,
        category=_broker_audit.Category.PAYMENTS,
        severity=_broker_audit.Severity.INFO,
        actor_type=_broker_audit.ActorType.STAFF,
        description=f"probe event for treasury request #{pk}",
        source_module="simulator.tests.test_o3c4b",
        metadata={"treasury_operation_request_id": pk},
    )
    if age_seconds is not None:
        backdated = timezone.now() - timedelta(seconds=age_seconds)
        BrokerAuditEvent.objects.filter(pk=obj.pk).update(timestamp=backdated)
        obj.refresh_from_db()
    return obj


def _started_audit_log(pk, age_seconds):
    return _make_audit_log_event(pk, _audit.EV_TREASURY_REQUEST_EXECUTION_STARTED, age_seconds)


def _started_broker_event(pk, age_seconds):
    return _make_broker_audit_event(pk, _broker_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED, age_seconds)


def _result_for(pk, results):
    for r in results:
        if r.instance.pk == pk:
            return r
    raise AssertionError(f"no result for pk={pk}")


class ThresholdValidationTests(TestCase):

    @override_settings(TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS=50)
    def test_uses_settings_value_when_min_age_seconds_is_none(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        _started_audit_log(tor.pk, age_seconds=100)  # older than the 50s setting
        [result] = inspect_stuck_treasury_execution()
        self.assertEqual(result.case, CASE_A)
        self.assertTrue(result.eligible)

    @override_settings(TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS=5000)
    def test_settings_value_below_age_yields_case_c(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        _started_audit_log(tor.pk, age_seconds=100)  # younger than the 5000s setting
        [result] = inspect_stuck_treasury_execution()
        self.assertEqual(result.case, CASE_C)
        self.assertFalse(result.eligible)

    def test_explicit_override_takes_precedence_over_settings(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        _started_audit_log(tor.pk, age_seconds=100)
        # Settings default (600s) would make this CASE_C; explicit override says otherwise.
        [result] = inspect_stuck_treasury_execution(min_age_seconds=10)
        self.assertEqual(result.case, CASE_A)
        self.assertTrue(result.eligible)

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds=0)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds=-1)

    def test_rejects_float(self):
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds=600.5)

    def test_rejects_string(self):
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds="600")

    def test_rejects_bool(self):
        # bool is a subclass of int in Python — True/False must not
        # silently become min_age_seconds=1/0.
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds=True)
        with self.assertRaises(ValueError):
            inspect_stuck_treasury_execution(min_age_seconds=False)


class ScopeFilteringTests(TestCase):

    def test_only_executing_status_is_inspected(self):
        wallet = make_wallet()
        executing = _make_executing_request(wallet=wallet)
        _started_audit_log(executing.pk, age_seconds=1000)

        for status in (
            TreasuryOperationRequest.ST_PENDING, TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED, TreasuryOperationRequest.ST_EXECUTED,
            TreasuryOperationRequest.ST_FAILED, TreasuryOperationRequest.ST_CANCELLED,
        ):
            TreasuryOperationRequest.objects.create(
                operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
                wallet=wallet, amount=Decimal("5.00"), status=status,
            )

        results = inspect_stuck_treasury_execution()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].instance.pk, executing.pk)


class AgeConfidenceSourceTests(TestCase):

    def test_auditlog_takes_priority_over_broker_audit_event(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        _started_broker_event(tor.pk, age_seconds=5000)  # different age — must be ignored

        [result] = inspect_stuck_treasury_execution()
        self.assertEqual(result.age_confidence, AGE_CONFIDENCE_AUDIT_LOG)
        self.assertAlmostEqual(result.age_seconds, 1000, delta=5)

    def test_falls_back_to_broker_audit_event_when_auditlog_missing(self):
        tor = _make_executing_request()
        _started_broker_event(tor.pk, age_seconds=2000)

        [result] = inspect_stuck_treasury_execution()
        self.assertEqual(result.age_confidence, AGE_CONFIDENCE_BROKER_AUDIT_EVENT)
        self.assertAlmostEqual(result.age_seconds, 2000, delta=5)

    def test_no_events_anywhere_yields_unknown(self):
        _make_executing_request()

        [result] = inspect_stuck_treasury_execution()
        self.assertEqual(result.age_confidence, AGE_CONFIDENCE_UNKNOWN)
        self.assertIsNone(result.age_seconds)

    def test_age_computed_correctly(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=12345)

        [result] = inspect_stuck_treasury_execution()
        self.assertAlmostEqual(result.age_seconds, 12345, delta=5)


class ClassificationTests(TestCase):

    def test_case_a_eligible(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        _started_audit_log(tor.pk, age_seconds=1000)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_A)
        self.assertTrue(result.eligible)
        self.assertIsNone(result.block_reason)
        self.assertFalse(result.has_wallet_transaction)
        self.assertTrue(result.has_started_event)

    def test_case_b_never_eligible_even_if_otherwise_perfect(self):
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("10.00"), balance_after=Decimal("60.00"),
        )
        tor = _make_executing_request(
            wallet=wallet, executed_by=make_user(is_active=True), wallet_transaction=wtx,
        )
        _started_audit_log(tor.pk, age_seconds=100000)  # very old, otherwise perfect

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_B)
        self.assertFalse(result.eligible)
        self.assertIn("wallet_transaction", result.block_reason)
        self.assertTrue(result.has_wallet_transaction)

    def test_case_c_recent_not_eligible(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        _started_audit_log(tor.pk, age_seconds=10)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_C)
        self.assertFalse(result.eligible)
        self.assertIn("minimum age threshold", result.block_reason)

    def test_case_d_unknown_age_otherwise_consistent(self):
        tor = _make_executing_request(executed_by=make_user(is_active=True))
        # No STARTED event in either system, no wallet_transaction,
        # no EXECUTED/FAILED events, active executor — otherwise clean.

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_D)
        self.assertFalse(result.eligible)
        self.assertEqual(result.age_confidence, AGE_CONFIDENCE_UNKNOWN)
        self.assertIn("cannot be confirmed", result.block_reason)

    def test_case_e_by_executed_event(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTED)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_E)
        self.assertFalse(result.eligible)
        self.assertTrue(result.has_executed_event)
        self.assertIn("EXECUTED audit event", result.block_reason)

    def test_case_e_by_failed_event(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_E)
        self.assertFalse(result.eligible)
        self.assertTrue(result.has_failed_event)
        self.assertIn("FAILED audit event", result.block_reason)

    def test_case_e_by_inconsistent_chain_both_executed_and_failed(self):
        # A doubly-contradictory chain: both EXECUTED and FAILED events
        # exist for the same still-EXECUTING request — must still
        # resolve cleanly to CASE_E, not confuse the classifier.
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTED)
        _make_audit_log_event(tor.pk, _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_E)
        self.assertFalse(result.eligible)
        self.assertTrue(result.has_executed_event)
        self.assertTrue(result.has_failed_event)
        self.assertIn("EXECUTED audit event", result.block_reason)
        self.assertIn("FAILED audit event", result.block_reason)

    def test_case_f_inactive_executor(self):
        inactive_user = make_user(is_active=False)
        tor = _make_executing_request(executed_by=inactive_user)
        _started_audit_log(tor.pk, age_seconds=1000)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_F)
        self.assertEqual(result.executed_by_is_active, False)
        # Case F does not by itself change financial safety (O.3c-4
        # Fase 0) — eligibility still follows the 7-point checklist,
        # which does not mention executed_by status at all.
        self.assertTrue(result.eligible)

    def test_case_f_missing_executor(self):
        tor = _make_executing_request(executed_by=None)
        _started_audit_log(tor.pk, age_seconds=1000)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.case, CASE_F)
        self.assertIsNone(result.executed_by_is_active)
        self.assertTrue(result.eligible)

    def test_wallet_transaction_present_blocks_regardless_of_other_conditions(self):
        wallet = make_wallet(initial_balance=Decimal("20.00"))
        wtx = WalletTransaction.objects.create(
            wallet=wallet, tx_type=WalletTransaction.TX_BONUS,
            amount=Decimal("5.00"), balance_after=Decimal("25.00"),
        )
        tor = _make_executing_request(
            wallet=wallet, executed_by=make_user(is_active=True), wallet_transaction=wtx,
        )
        _started_audit_log(tor.pk, age_seconds=999999)

        [result] = inspect_stuck_treasury_execution(min_age_seconds=1)
        self.assertFalse(result.eligible)

    def test_unknown_age_is_never_eligible(self):
        _make_executing_request(executed_by=make_user(is_active=True))
        [result] = inspect_stuck_treasury_execution(min_age_seconds=600)
        self.assertEqual(result.age_confidence, AGE_CONFIDENCE_UNKNOWN)
        self.assertFalse(result.eligible)


class ReadOnlyGuaranteeTests(TestCase):

    def test_performs_no_write_to_treasury_operation_request(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        before = TreasuryOperationRequest.objects.get(pk=tor.pk)

        inspect_stuck_treasury_execution()

        after = TreasuryOperationRequest.objects.get(pk=tor.pk)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.wallet_transaction_id, after.wallet_transaction_id)
        self.assertEqual(before.failure_reason, after.failure_reason)
        self.assertEqual(before.executed_at, after.executed_at)

    def test_creates_no_auditlog_or_brokerauditevent_rows(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=1000)
        auditlog_before = AuditLog.objects.count()
        broker_before = BrokerAuditEvent.objects.count()

        inspect_stuck_treasury_execution()

        self.assertEqual(AuditLog.objects.count(), auditlog_before)
        self.assertEqual(BrokerAuditEvent.objects.count(), broker_before)

    def test_does_not_touch_wallet_balance(self):
        wallet = make_wallet(initial_balance=Decimal("777.00"))
        tor = _make_executing_request(wallet=wallet)
        _started_audit_log(tor.pk, age_seconds=1000)
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        inspect_stuck_treasury_execution()

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("777.00"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before)

    def test_ast_confirms_no_financial_functions_referenced_anywhere_in_module(self):
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
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)

        self.assertEqual(imported & forbidden, set())
        self.assertEqual(called & forbidden, set())


class QueryEfficiencyTests(TestCase):

    def test_fixed_query_count_regardless_of_row_count(self):
        wallet = make_wallet()
        for _ in range(6):
            tor = _make_executing_request(wallet=wallet, executed_by=make_user(is_active=True))
            _started_audit_log(tor.pk, age_seconds=1000)

        with CaptureQueriesContext(connection) as ctx:
            results = inspect_stuck_treasury_execution(min_age_seconds=600)

        self.assertEqual(len(results), 6)
        # Exactly 3 queries by design: EXECUTING rows (+select_related
        # executed_by), one AuditLog batch query, one BrokerAuditEvent
        # batch query — regardless of how many EXECUTING rows exist.
        self.assertEqual(len(ctx.captured_queries), 3)

    def test_zero_queries_beyond_the_first_when_no_executing_rows_exist(self):
        with CaptureQueriesContext(connection) as ctx:
            results = inspect_stuck_treasury_execution()
        self.assertEqual(results, [])
        self.assertEqual(len(ctx.captured_queries), 1)


class DeterministicOrderingTests(TestCase):

    def test_results_ordered_by_pk_ascending_and_stable_across_calls(self):
        wallet = make_wallet()
        pks = []
        for _ in range(5):
            tor = _make_executing_request(wallet=wallet)
            _started_audit_log(tor.pk, age_seconds=1000)
            pks.append(tor.pk)

        first_call = [r.instance.pk for r in inspect_stuck_treasury_execution()]
        second_call = [r.instance.pk for r in inspect_stuck_treasury_execution()]

        self.assertEqual(first_call, sorted(pks))
        self.assertEqual(first_call, second_call)
