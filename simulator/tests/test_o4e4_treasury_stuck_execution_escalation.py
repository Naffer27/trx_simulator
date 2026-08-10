# simulator/tests/test_o4e4_treasury_stuck_execution_escalation.py
"""
Microbloque O.4e-4 — Treasury Persistent Stuck Execution Escalation.

100% observational: covers observe_treasury_stuck_execution_escalations()
(broker_audit.py) and its wiring into the existing
observe_treasury_stuck_executions_task (tasks.py, O.3d-3, unmodified in
its own dedicated file — see the updated guard tests in
test_o3d3_treasury_stuck_execution_celery_monitor.py for the two
call-count assertions O.4e-4 legitimately superseded).

Persistence is measured from the EARLIEST EV_TREASURY_STUCK_EXECUTION_
OBSERVED row already durable for a request (Min(timestamp)) — never
from StuckExecutionCandidate.age_seconds, which is None for CASE_D.
Frozen decisions (approved before implementation):
  threshold = 2700s (45 min), dedup window = 3600s (1h),
  event = EV_TREASURY_STUCK_EXECUTION_ESCALATED, severity = CRITICAL,
  logger.error() only (no sentry_sdk.capture_message()).
"""
import ast
import inspect
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.broker_audit import (
    EV_TREASURY_STUCK_EXECUTION_ESCALATED,
    EV_TREASURY_STUCK_EXECUTION_OBSERVED,
    TREASURY_STUCK_EXECUTION_ESCALATION_DEDUP_WINDOW_SECONDS,
    TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS,
    observe_treasury_stuck_execution_escalations,
)
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest,
    Wallet, WalletTransaction,
)
from simulator.tasks import observe_treasury_stuck_executions_task

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet, amount=Decimal("10.00"), reason="O.4e-4 escalation test",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by, wallet_transaction=wallet_transaction,
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


def _make_observed_event(tor, seconds_ago, case="CASE_A"):
    event = _broker_audit.record_payment_event(
        event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED,
        severity=_broker_audit.Severity.WARNING,
        actor_type=_broker_audit.ActorType.SYSTEM,
        description="O.4e-4 test observation",
        metadata={"treasury_operation_request_id": tor.pk, "case": case},
    )
    BrokerAuditEvent.objects.filter(pk=event.pk).update(
        timestamp=timezone.now() - timedelta(seconds=seconds_ago)
    )
    return event


def _case_a_request(observed_seconds_ago=2701):
    executor = make_user(username=f"o4e4_a_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    _make_observed_event(tor, observed_seconds_ago, case="CASE_A")
    return tor


def _case_b_request(observed_seconds_ago=2701):
    executor = make_user(username=f"o4e4_b_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    wallet = make_wallet(initial_balance=Decimal("50.00"))
    wtx = WalletTransaction.objects.filter(wallet=wallet).first()
    tor = _make_executing_request(wallet=wallet, executed_by=executor, wallet_transaction=wtx)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    _make_observed_event(tor, observed_seconds_ago, case="CASE_B")
    return tor


def _case_d_request(observed_seconds_ago=2701):
    # No EXECUTION_STARTED event anywhere — age unknown.
    executor = make_user(username=f"o4e4_d_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _make_observed_event(tor, observed_seconds_ago, case="CASE_D")
    return tor


def _case_e_request(observed_seconds_ago=2701):
    executor = make_user(username=f"o4e4_e_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTED,
        action=f"Treasury request #{tor.pk} executed",
        detail={"treasury_request_id": tor.pk},
    )
    _make_observed_event(tor, observed_seconds_ago, case="CASE_E")
    return tor


def _case_f_request(observed_seconds_ago=2701):
    tor = _make_executing_request(executed_by=None)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    _make_observed_event(tor, observed_seconds_ago, case="CASE_F")
    return tor


def _case_c_request():
    # Confirmed age, but below the 600s stuck threshold — never even
    # observed in the first place (CASE_C is filtered out before the
    # severity map), so it can never accumulate a first_observed_at.
    executor = make_user(username=f"o4e4_c_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=100)  # well under 600s
    return tor


def _all_financial_counts():
    return (
        TreasuryOperationRequest.objects.count(),
        Wallet.objects.count(),
        WalletTransaction.objects.count(),
        InternalTransfer.objects.count(),
    )


# ─────────────────────────────────────────────
# Escalation by case
# ─────────────────────────────────────────────

class EscalationByCaseTests(TestCase):

    def test_case_a_persistently_observed_escalates(self):
        _case_a_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 1,
        )

    def test_case_b_persistently_observed_escalates(self):
        _case_b_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_case_d_persistently_observed_escalates(self):
        _case_d_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_case_e_persistently_observed_escalates(self):
        _case_e_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_case_f_persistently_observed_escalates(self):
        _case_f_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_case_c_never_escalates(self):
        _case_c_request()
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 0,
        )


# ─────────────────────────────────────────────
# Threshold — before / at boundary / after
# ─────────────────────────────────────────────

class ThresholdTests(TestCase):

    def test_before_threshold_does_not_escalate(self):
        _case_a_request(observed_seconds_ago=TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS - 1)
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 0)

    def test_exactly_at_threshold_escalates(self):
        tor = _case_a_request(observed_seconds_ago=0)
        first_observed = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        frozen_now = first_observed.timestamp + timedelta(seconds=TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_well_after_threshold_escalates(self):
        _case_a_request(observed_seconds_ago=TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 500)
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)

    def test_never_observed_yet_does_not_escalate(self):
        # A fresh candidate this very tick, before any observation row
        # exists at all — must not crash, must not escalate.
        executor = make_user(username="o4e4_fresh", is_staff=True)
        _make_executing_request(executed_by=executor)
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 0)


# ─────────────────────────────────────────────
# Persistence signal — first_observed_at, not age_seconds
# ─────────────────────────────────────────────

class PersistenceSignalTests(TestCase):

    def test_uses_first_observed_at_not_created_at_or_age_seconds(self):
        tor = _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_execution_escalations()
        event = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        first_observed = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        self.assertEqual(event.metadata["first_observed_at"], first_observed.timestamp.isoformat())
        self.assertGreaterEqual(event.metadata["persisted_seconds"], 2701)

    def test_case_d_escalates_without_relying_on_age_seconds(self):
        # CASE_D has NO EXECUTION_STARTED event anywhere — age_seconds is
        # None in inspect_stuck_treasury_execution()'s own output. This
        # confirms escalation never touches that field.
        from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution
        tor = _case_d_request(observed_seconds_ago=2701)
        candidates = {c.instance.pk: c for c in inspect_stuck_treasury_execution()}
        self.assertIsNone(candidates[tor.pk].age_seconds)  # sanity: genuinely unknown

        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)
        event = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        self.assertNotIn("age_seconds", event.metadata)

    def test_uses_earliest_observation_when_multiple_exist(self):
        tor = _case_a_request(observed_seconds_ago=2701)
        # A second, more recent observation for the same request must
        # not reset the persistence clock.
        _make_observed_event(tor, seconds_ago=100, case="CASE_A")
        event_before = observe_treasury_stuck_execution_escalations()
        self.assertEqual(event_before, 1)
        escalation = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        self.assertGreaterEqual(escalation.metadata["persisted_seconds"], 2701)


# ─────────────────────────────────────────────
# Dedup — no flooding, re-alert after the window
# ─────────────────────────────────────────────

class DedupTests(TestCase):

    def test_dedup_prevents_flooding_within_window(self):
        _case_a_request(observed_seconds_ago=2701)
        for _ in range(5):
            observe_treasury_stuck_execution_escalations()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 1,
        )

    def test_re_alerts_after_dedup_window_elapses(self):
        _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_execution_escalations()
        escalation = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        BrokerAuditEvent.objects.filter(pk=escalation.pk).update(
            timestamp=timezone.now() - timedelta(seconds=TREASURY_STUCK_EXECUTION_ESCALATION_DEDUP_WINDOW_SECONDS + 1)
        )
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 2,
        )

    def test_custom_dedup_window_override(self):
        _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_execution_escalations(dedup_window_seconds=60)
        escalation = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        BrokerAuditEvent.objects.filter(pk=escalation.pk).update(
            timestamp=timezone.now() - timedelta(seconds=61)
        )
        written = observe_treasury_stuck_execution_escalations(dedup_window_seconds=60)
        self.assertEqual(written, 1)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

class LoggingTests(TestCase):

    def test_logger_error_fires_only_when_escalation_is_actually_written(self):
        _case_a_request(observed_seconds_ago=2701)
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_execution_escalations()
        mock_error.assert_called_once()

    def test_logger_error_does_not_fire_below_threshold(self):
        _case_a_request(observed_seconds_ago=100)
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_execution_escalations()
        mock_error.assert_not_called()

    def test_logger_error_does_not_fire_on_dedup_hit(self):
        _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_execution_escalations()
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_execution_escalations()
        mock_error.assert_not_called()

    def test_missing_sentry_dsn_does_not_break_escalation(self):
        # Default test settings have no SENTRY_DSN configured — this
        # confirms record_payment_event()/logger.error() work fine
        # without it (no sentry_sdk import failure, no exception).
        _case_a_request(observed_seconds_ago=2701)
        written = observe_treasury_stuck_execution_escalations()
        self.assertEqual(written, 1)


# ─────────────────────────────────────────────
# Event shape
# ─────────────────────────────────────────────

class EventShapeTests(TestCase):

    def test_event_shape_and_metadata(self):
        tor = _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_execution_escalations()
        event = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        self.assertEqual(event.severity, _broker_audit.Severity.CRITICAL)
        self.assertEqual(event.actor_type, _broker_audit.ActorType.SYSTEM)
        self.assertEqual(event.category, _broker_audit.Category.PAYMENTS)
        self.assertEqual(event.metadata["treasury_operation_request_id"], tor.pk)
        self.assertEqual(event.metadata["case"], "CASE_A")
        self.assertEqual(
            event.metadata["escalation_threshold_seconds"], TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS,
        )
        self.assertIn("dedup_window_seconds", event.metadata)
        self.assertIn("wallet_id", event.metadata)
        self.assertIn("amount", event.metadata)


# ─────────────────────────────────────────────
# Financial invariants
# ─────────────────────────────────────────────

class NoFinancialMutationTests(TestCase):

    def test_no_financial_mutation_across_all_cases(self):
        _case_a_request()
        _case_b_request()
        _case_d_request()
        _case_e_request()
        _case_f_request()
        _case_c_request()
        before = _all_financial_counts()
        for _ in range(3):
            observe_treasury_stuck_execution_escalations()
        after = _all_financial_counts()
        self.assertEqual(before, after)

    def test_status_of_escalated_requests_unchanged(self):
        tor = _case_a_request()
        observe_treasury_stuck_execution_escalations()
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)


# ─────────────────────────────────────────────
# AST — structural invariants
# ─────────────────────────────────────────────

class ScopeAndSafetyTests(SimpleTestCase):

    _FORBIDDEN_CALLS = {
        "credit_wallet", "debit_wallet", "reconcile_wallet",
        "transfer_to_account", "transfer_to_wallet",
        "execute_treasury_request", "mark_treasury_execution_failed",
    }
    _FORBIDDEN_IMPORTS = {"wallet_ledger"}

    def _walk(self, fn):
        tree = ast.parse(inspect.getsource(fn))
        imported, called = set(), set()
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
        return imported, called

    def test_escalation_function_has_no_financial_imports_or_calls(self):
        imported, called = self._walk(observe_treasury_stuck_execution_escalations)
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")

    def test_record_escalation_has_no_financial_imports_or_calls(self):
        imported, called = self._walk(_broker_audit._record_treasury_stuck_execution_escalation)
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")


# ─────────────────────────────────────────────
# Task wiring — order, no race
# ─────────────────────────────────────────────

class TaskWiringTests(TestCase):

    def test_task_calls_observation_then_escalation_in_order(self):
        call_order = []
        with patch(
            "simulator.broker_audit.observe_stuck_treasury_executions",
            side_effect=lambda: call_order.append("observed") or 0,
        ), patch(
            "simulator.broker_audit.observe_treasury_stuck_execution_escalations",
            side_effect=lambda: call_order.append("escalated") or 0,
        ):
            observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(call_order, ["observed", "escalated"])

    def test_task_result_includes_both_counts(self):
        _case_a_request(observed_seconds_ago=2701)
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertIn("written", result)
        self.assertIn("escalated", result)
        self.assertEqual(result["escalated"], 1)

    def test_task_end_to_end_writes_both_event_types(self):
        _case_a_request(observed_seconds_ago=2701)
        observe_treasury_stuck_executions_task.apply().get()
        self.assertTrue(BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED).exists())
        self.assertTrue(BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).exists())


# ─────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────

class EventCatalogTests(SimpleTestCase):

    def test_escalated_constant_value(self):
        self.assertEqual(EV_TREASURY_STUCK_EXECUTION_ESCALATED, "treasury.stuck_execution_escalated")

    def test_mirrored_in_audit_module(self):
        self.assertEqual(
            _audit.EV_TREASURY_STUCK_EXECUTION_ESCALATED, EV_TREASURY_STUCK_EXECUTION_ESCALATED,
        )

    def test_default_threshold_and_dedup_window(self):
        self.assertEqual(TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS, 2700)
        self.assertEqual(TREASURY_STUCK_EXECUTION_ESCALATION_DEDUP_WINDOW_SECONDS, 3600)
