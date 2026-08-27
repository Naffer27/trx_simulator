# simulator/tests/test_o3d3_treasury_stuck_execution_celery_monitor.py
"""
Bloque O.3d-3 — Treasury Stuck Execution Celery Monitor.

Covers ONLY simulator/tasks.py::observe_treasury_stuck_executions_task()
and its CELERY_BEAT_SCHEDULE entry ("observe-treasury-stuck-executions-15m",
trx_simulator/settings.py). The task calls
broker_audit.observe_stuck_treasury_executions() (O.3d-2, unmodified)
and nothing else — never inspect_stuck_treasury_execution() directly,
never mark_treasury_execution_failed(), never credit_wallet()/
debit_wallet(). Same shape as the pre-existing
observe_broker_risk_alerts_task (RISK-03): bind=True, max_retries=0,
acks_late=True, soft_time_limit=25, time_limit=29 — no task-level
retries, because every BrokerAuditEvent write the service call reaches
is already fail-open by construction (verified in O.3d-2's own test
suite).

No dashboard, no view, no template, no model and no migration are
introduced by this block.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from datetime import timedelta

from simulator import audit as _audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.tasks import observe_treasury_stuck_executions_task

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600  # settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS default


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3d-3 celery monitor test",
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


def _make_eligible_executing_request(**overrides):
    executor = make_user(username=f"o3d3_exec_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor, **overrides)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
    return tor


class TaskExistsAndRegisteredTests(SimpleTestCase):
    """1, 2 — the task exists and is registered by Celery."""

    def test_task_exists_and_is_callable(self):
        self.assertTrue(callable(observe_treasury_stuck_executions_task))

    def test_task_name_matches_the_approved_convention(self):
        self.assertEqual(
            observe_treasury_stuck_executions_task.name,
            "simulator.observe_treasury_stuck_executions",
        )

    def test_task_is_registered_in_the_celery_app(self):
        from trx_simulator.celery import app as celery_app
        self.assertIn(
            "simulator.observe_treasury_stuck_executions",
            celery_app.tasks.keys(),
        )

    def test_task_decorator_matches_observe_broker_risk_alerts_task_shape(self):
        """Same bind/retry/ack/time-limit profile as the direct RISK-03
        precedent this task deliberately mirrors — no task-level retry
        (max_retries=0), acks_late=True, tight soft/hard time limits."""
        registered = observe_treasury_stuck_executions_task
        self.assertEqual(registered.max_retries, 0)
        self.assertTrue(registered.acks_late)
        self.assertEqual(registered.soft_time_limit, 25)
        self.assertEqual(registered.time_limit, 29)


class CallsServiceExactlyOnceTests(TestCase):
    """3, 4, 5 — calls observe_stuck_treasury_executions() exactly
    once, never mark_treasury_execution_failed(), never
    credit_wallet()/debit_wallet()."""

    def test_calls_observe_stuck_treasury_executions_exactly_once(self):
        with patch(
            "simulator.broker_audit.observe_stuck_treasury_executions",
        ) as mock_observe:
            mock_observe.return_value = 0
            observe_treasury_stuck_executions_task.apply().get()

        mock_observe.assert_called_once_with()

    def test_never_calls_inspect_stuck_treasury_execution_directly(self):
        """The task must go through the two sanctioned service
        functions only — never reach past them into the read-only
        inspector itself.

        O.4e-4 legitimately changed the expected call count from 1 to
        2: observe_stuck_treasury_executions() calls the inspector once
        internally, and observe_treasury_stuck_execution_escalations()
        (also called by this same task, per O.4e-4's approved wiring)
        calls it again independently, by design — see that function's
        own docstring for why a fresh, independent read is required
        rather than reusing a candidate list computed elsewhere. Both
        call sites are legitimate, sanctioned service functions; this
        test confirms the TASK ITSELF never bypasses them to call the
        inspector directly."""
        with patch(
            "simulator.treasury_execution_recovery.inspect_stuck_treasury_execution",
        ) as mock_inspect:
            mock_inspect.return_value = []
            observe_treasury_stuck_executions_task.apply().get()

        self.assertEqual(mock_inspect.call_count, 2)

    def test_never_calls_mark_treasury_execution_failed(self):
        tor = _make_eligible_executing_request()
        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            observe_treasury_stuck_executions_task.apply().get()
        mock_mark_failed.assert_not_called()
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_never_calls_credit_or_debit_wallet(self):
        with patch("simulator.wallet_ledger.credit_wallet") as mock_credit, \
             patch("simulator.wallet_ledger.debit_wallet") as mock_debit:
            _make_eligible_executing_request()
            observe_treasury_stuck_executions_task.apply().get()
        mock_credit.assert_not_called()
        mock_debit.assert_not_called()


class NoSideEffectsTests(TestCase):
    """6, 7 — never modifies TreasuryOperationRequest, Wallet or
    WalletTransaction."""

    def setUp(self):
        self.wallet = make_wallet(initial_balance=Decimal("50.00"))
        self.tor = _make_eligible_executing_request(wallet=self.wallet)

    def test_never_modifies_the_treasury_operation_request(self):
        status_before = self.tor.status
        wallet_transaction_before = self.tor.wallet_transaction_id
        failure_reason_before = self.tor.failure_reason

        observe_treasury_stuck_executions_task.apply().get()

        self.tor.refresh_from_db()
        self.assertEqual(self.tor.status, status_before)
        self.assertEqual(self.tor.wallet_transaction_id, wallet_transaction_before)
        self.assertEqual(self.tor.failure_reason, failure_reason_before)

    def test_never_touches_wallet_balance(self):
        balance_before = self.wallet.available_balance
        observe_treasury_stuck_executions_task.apply().get()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_never_creates_wallet_transaction(self):
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        observe_treasury_stuck_executions_task.apply().get()
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(before, after)

    def test_never_creates_internal_transfer(self):
        observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_never_creates_auditlog(self):
        before = AuditLog.objects.count()
        observe_treasury_stuck_executions_task.apply().get()
        after = AuditLog.objects.count()
        self.assertEqual(before, after)


class ReturnContractTests(TestCase):
    """8 — returns a stable, JSON-serializable result."""

    def test_result_is_a_plain_dict_with_expected_keys(self):
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertIsInstance(result, dict)
        self.assertIn("written", result)
        self.assertIn("elapsed_ms", result)

    def test_result_is_json_serializable(self):
        import json
        result = observe_treasury_stuck_executions_task.apply().get()
        json.dumps(result)  # raises TypeError if not serializable

    def test_written_is_plain_int_and_elapsed_ms_is_plain_int(self):
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertIsInstance(result["written"], int)
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertGreaterEqual(result["elapsed_ms"], 0)


class ZeroAndObservedCandidatesTests(TestCase):
    """9, 10, 16 — zero candidates, observed/deduplicated candidates,
    and dedup respected across consecutive task executions."""

    def test_zero_candidates_returns_written_zero(self):
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result["written"], 0)

    def test_one_eligible_candidate_returns_written_one(self):
        _make_eligible_executing_request()
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result["written"], 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type="treasury.stuck_execution_observed",
            ).count(),
            1,
        )

    def test_consecutive_executions_respect_the_services_own_dedup(self):
        _make_eligible_executing_request()

        first = observe_treasury_stuck_executions_task.apply().get()
        second = observe_treasury_stuck_executions_task.apply().get()

        self.assertEqual(first["written"], 1)
        self.assertEqual(second["written"], 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type="treasury.stuck_execution_observed",
            ).count(),
            1,
        )


class ServiceExceptionHandlingTests(TestCase):
    """
    11 — documented fail-open contract: the WRITE side (every
    BrokerAuditEvent this call chain creates) is fail-open by
    construction (record_event(), verified in O.3d-2) and this task
    never wraps that further. The READ side is deliberately NOT
    swallowed here: if observe_stuck_treasury_executions() itself
    raises (e.g. a genuine failure reaching past its own internals,
    not a per-candidate write failure), this task does not catch it —
    same undocumented-but-real behavior observe_broker_risk_alerts_task
    already has for the equivalent RISK-03 read. Celery marks such a
    task failed and does not retry (max_retries=0); nothing here
    silently manufactures a fake "0 written" result for a genuine read
    failure.
    """

    def test_exception_from_the_service_propagates_out_of_apply(self):
        with patch(
            "simulator.broker_audit.observe_stuck_treasury_executions",
            side_effect=RuntimeError("boom"),
        ):
            result = observe_treasury_stuck_executions_task.apply()

        self.assertTrue(result.failed())
        with self.assertRaises(RuntimeError):
            result.get()

    def test_a_single_candidates_write_failure_does_not_fail_the_task(self):
        """
        The service's own per-candidate write is fail-open — a
        RuntimeError raised inside BrokerAuditEvent.objects.create()
        (O.3d-2's own documented contract) must not surface as a task
        failure; it is swallowed by record_event() long before it ever
        reaches this task.
        """
        _make_eligible_executing_request()
        with patch(
            "simulator.models.BrokerAuditEvent.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            result = observe_treasury_stuck_executions_task.apply()

        self.assertFalse(result.failed())
        self.assertEqual(result.get()["written"], 0)


class BeatScheduleTests(SimpleTestCase):
    """12, 13, 14 — the Beat entry exists, cadence is exactly 15
    minutes, and no other existing entry was altered."""

    def test_beat_schedule_registers_the_periodic_task(self):
        from django.conf import settings
        schedule = settings.CELERY_BEAT_SCHEDULE
        matching = [
            v for v in schedule.values()
            if v["task"] == "simulator.observe_treasury_stuck_executions"
        ]
        self.assertEqual(len(matching), 1)

    def test_cadence_is_exactly_every_15_minutes(self):
        from celery.schedules import crontab
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["observe-treasury-stuck-executions-15m"]
        self.assertEqual(entry["schedule"], crontab(minute="*/15"))

    def test_expires_option_is_just_under_the_tick_interval(self):
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["observe-treasury-stuck-executions-15m"]
        self.assertEqual(entry["options"]["expires"], 14 * 60)

    def test_pre_existing_beat_entries_are_unchanged(self):
        from celery.schedules import crontab
        from django.conf import settings
        schedule = settings.CELERY_BEAT_SCHEDULE

        self.assertEqual(schedule["reconcile-deposits-15m"]["task"], "simulator.reconcile_deposits")
        self.assertEqual(schedule["reconcile-deposits-15m"]["schedule"], crontab(minute="*/15"))
        self.assertEqual(schedule["reconcile-deposits-15m"]["args"], (24,))

        self.assertEqual(schedule["reconcile-withdrawals-15m"]["task"], "simulator.reconcile_withdrawals")
        self.assertEqual(schedule["reconcile-withdrawals-15m"]["args"], (48,))

        self.assertEqual(
            schedule["observe-broker-risk-alerts-5m"]["task"],
            "simulator.observe_broker_risk_alerts",
        )
        self.assertEqual(
            schedule["observe-broker-risk-alerts-5m"]["schedule"], crontab(minute="*/5"),
        )

        # 10 pre-existing + this block's 1 new entry + O.4e-2's
        # "record-celery-beat-heartbeat-5m" (added later, not a defect
        # here — see test_o4e2_..._celery_beat_heartbeat_foundation.py's
        # own BeatScheduleTests for that entry's dedicated coverage) +
        # FIX-02A.4's 2 new entries ("reconcile-unknown-payouts-15m",
        # "replay-payout-webhook-events-5m" — provider-agnostic UNKNOWN
        # reconciliation / durable webhook replay).
        self.assertEqual(len(schedule), 14)

    def test_no_other_task_names_were_renamed_or_removed(self):
        from django.conf import settings
        task_names = {v["task"] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        for expected in (
            "simulator.reconcile_deposits",
            "simulator.reconcile_withdrawals",
            "simulator.ping",
            "simulator.take_snapshots",
            "simulator.cleanup_audit_log",
            "simulator.cleanup_snapshots",
            "simulator.scan_positions",
            "simulator.take_revenue_snapshot",
            "simulator.evaluate_all_challenges",
            "simulator.observe_broker_risk_alerts",
            "simulator.observe_treasury_stuck_executions",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, task_names)


class ScopeAndSafetyTests(SimpleTestCase):
    """15 — AST confirms absence of financial functions in the task."""

    def test_ast_confirms_no_financial_functions_in_the_task(self):
        import ast
        import inspect

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed",
            "inspect_stuck_treasury_execution",
        }
        forbidden_imports = {"wallet_ledger"}

        source = inspect.getsource(observe_treasury_stuck_executions_task)
        tree = ast.parse(source)

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

    def test_only_calls_into_broker_audit_are_observation_and_escalation(self):
        import ast
        import inspect

        # O.4e-4 legitimately superseded this guard: the task now also
        # calls observe_treasury_stuck_execution_escalations(), in that
        # documented order, per O.4e-4 Fase 0's approved wiring decision
        # (reuse the existing task rather than a new Celery task/Beat
        # entry) — see test_o4e4_...py for the escalation-specific
        # coverage of both the function itself and its ordering.
        tree = ast.parse(inspect.getsource(observe_treasury_stuck_executions_task))
        broker_audit_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("broker_audit"):
                broker_audit_imports.extend(a.name for a in node.names)

        self.assertEqual(
            broker_audit_imports,
            ["observe_stuck_treasury_executions", "observe_treasury_stuck_execution_escalations"],
        )

    def test_no_save_call_in_the_task(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(observe_treasury_stuck_executions_task))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                self.assertNotEqual(name, "save", "task must not call .save()")
