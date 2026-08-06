# simulator/tests/test_o3d1_treasury_stuck_execution_observation_event_catalog.py
"""
Bloque O.3d-1 — Treasury Stuck Execution Observation Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_STUCK_EXECUTION_OBSERVED = "treasury.stuck_execution_observed"
in both simulator/audit.py and simulator/broker_audit.py, mirrored
exactly — same discipline already used by EV_TREASURY_REQUEST_SUBMITTED
(O.3a-2), EV_TREASURY_REQUEST_APPROVED/REJECTED (O.3b-1),
EV_TREASURY_REQUEST_EXECUTION_STARTED/EXECUTED/EXECUTION_FAILED
(O.3c-1), and EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED/
MARKED_FAILED/RECOVERY_BLOCKED (O.3c-4a).

No observation service, no Celery task, no Celery Beat schedule entry,
no dashboard, no view, no URL, no template, no model, no migration and
no settings value exists yet that emits or consumes this event —
nothing calls log_audit(), record_event() or record_payment_event()
with it, and nothing reads it either. inspect_stuck_treasury_execution()
(O.3c-4b) and mark_treasury_execution_failed() (O.3c-4c) are entirely
unaffected — this block does not call, wrap or modify either.
"""
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit

_NEW_CONSTANT_NAME  = "EV_TREASURY_STUCK_EXECUTION_OBSERVED"
_NEW_CONSTANT_VALUE = "treasury.stuck_execution_observed"


class EventConstantExistsTests(SimpleTestCase):

    def test_constant_exists_in_audit_module(self):
        self.assertTrue(hasattr(_audit, _NEW_CONSTANT_NAME))

    def test_constant_exists_in_broker_audit_module(self):
        self.assertTrue(hasattr(_broker_audit, _NEW_CONSTANT_NAME))

    def test_audit_module_value_matches_approved_literal(self):
        self.assertEqual(getattr(_audit, _NEW_CONSTANT_NAME), _NEW_CONSTANT_VALUE)

    def test_broker_audit_module_value_matches_approved_literal(self):
        self.assertEqual(getattr(_broker_audit, _NEW_CONSTANT_NAME), _NEW_CONSTANT_VALUE)

    def test_both_modules_have_exactly_the_same_value(self):
        self.assertEqual(
            getattr(_audit, _NEW_CONSTANT_NAME),
            getattr(_broker_audit, _NEW_CONSTANT_NAME),
        )

    def test_constant_is_plain_str(self):
        self.assertIsInstance(getattr(_audit, _NEW_CONSTANT_NAME), str)
        self.assertIsInstance(getattr(_broker_audit, _NEW_CONSTANT_NAME), str)

    def test_value_is_distinct_from_every_prior_treasury_event(self):
        prior_values = {
            _audit.EV_TREASURY_REQUEST_SUBMITTED,
            _audit.EV_TREASURY_REQUEST_APPROVED,
            _audit.EV_TREASURY_REQUEST_REJECTED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
            _audit.EV_TREASURY_REQUEST_EXECUTED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED,
        }
        self.assertNotIn(_NEW_CONSTANT_VALUE, prior_values)


class NoProductiveUseYetTests(SimpleTestCase):
    """
    Confirms the constant is not yet wired into any real call site —
    grep-based, over source files, not behavior-based, because no
    behavior exists yet to test.
    """

    _SIMULATOR_DIR = pathlib.Path(__file__).resolve().parent.parent

    def _iter_non_test_py_files(self):
        for path in self._SIMULATOR_DIR.rglob("*.py"):
            if "tests" in path.parts:
                continue
            if "migrations" in path.parts:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path

    def test_event_string_appears_only_in_its_two_definition_sites(self):
        hits = []
        for path in self._iter_non_test_py_files():
            text = path.read_text(encoding="utf-8")
            if _NEW_CONSTANT_VALUE in text:
                hits.append(str(path.relative_to(self._SIMULATOR_DIR.parent)))
        self.assertEqual(
            sorted(hits),
            sorted(["simulator/audit.py", "simulator/broker_audit.py"]),
        )

    def test_no_call_site_passes_the_new_constant_to_log_audit_or_record_event(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            for call in ("log_audit(", "record_event(", "record_payment_event(",
                         "record_compliance_event(", "record_admin_event(",
                         "record_risk_event("):
                for line in source.splitlines():
                    if call in line:
                        self.assertNotIn(
                            _NEW_CONSTANT_NAME, line,
                            f"unexpected productive call site in {module.__name__}: {line!r}",
                        )

    def test_no_monitor_task_or_dashboard_symbols_introduced_in_audit_modules(self):
        """
        O.3d-2 superseded this guard on purpose, legitimately adding
        observe_stuck_treasury_executions() and record_treasury_stuck_
        execution_observation() to broker_audit.py (never to audit.py —
        same "observation lives only in broker_audit.py" discipline
        RISK-03's observe_broker_alerts() already established). Same
        "guard correctly falsified by the next authorized block"
        pattern already used for O.3c-1 -> O.3c-3a and
        O.3c-4a -> O.3c-4b-1/4c-1. TreasuryObservationLock never
        exists — O.3d-2 reused the pre-existing BrokerAuditObservationLock
        singleton instead of introducing a new model.
        """
        self.assertFalse(hasattr(_audit, "observe_stuck_treasury_executions"))
        self.assertFalse(hasattr(_audit, "record_treasury_stuck_execution_observation"))

        self.assertTrue(hasattr(_broker_audit, "observe_stuck_treasury_executions"))
        self.assertTrue(callable(_broker_audit.observe_stuck_treasury_executions))
        self.assertTrue(hasattr(_broker_audit, "record_treasury_stuck_execution_observation"))
        self.assertTrue(callable(_broker_audit.record_treasury_stuck_execution_observation))

        for name in ("observe_treasury_stuck_executions", "record_treasury_stuck_observation",
                     "TreasuryObservationLock"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(_audit, name))
                self.assertFalse(hasattr(_broker_audit, name))

    def test_no_recovery_or_execution_service_symbols_leaked_into_audit_modules(self):
        self.assertFalse(hasattr(_audit, "inspect_stuck_treasury_execution"))
        self.assertFalse(hasattr(_audit, "mark_treasury_execution_failed"))
        self.assertFalse(hasattr(_audit, "execute_treasury_request"))
        self.assertFalse(hasattr(_broker_audit, "inspect_stuck_treasury_execution"))
        self.assertFalse(hasattr(_broker_audit, "mark_treasury_execution_failed"))
        self.assertFalse(hasattr(_broker_audit, "execute_treasury_request"))


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    Guards against accidentally editing a pre-existing constant while
    adding the new one — same spot-check discipline used in O.3a-2,
    O.3b-1, O.3c-1 and O.3c-4a. Explicitly re-checks every prior
    Treasury constant stays intact, plus a sample of older, unrelated
    events.
    """

    def test_o3a2_submission_constant_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")

    def test_o3b1_review_constants_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")
        self.assertEqual(_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")

    def test_o3c1_execution_constants_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED, "treasury.request_execution_started")
        self.assertEqual(_audit.EV_TREASURY_REQUEST_EXECUTED, "treasury.request_executed")
        self.assertEqual(_audit.EV_TREASURY_REQUEST_EXECUTION_FAILED, "treasury.request_execution_failed")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED, "treasury.request_execution_started")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_EXECUTED, "treasury.request_executed")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_EXECUTION_FAILED, "treasury.request_execution_failed")

    def test_o3c4a_recovery_constants_unchanged(self):
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
            "treasury.request_execution_recovery_started",
        )
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            "treasury.request_execution_marked_failed",
        )
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED,
            "treasury.request_execution_recovery_blocked",
        )
        self.assertEqual(
            _broker_audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
            "treasury.request_execution_recovery_started",
        )
        self.assertEqual(
            _broker_audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
            "treasury.request_execution_marked_failed",
        )
        self.assertEqual(
            _broker_audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED,
            "treasury.request_execution_recovery_blocked",
        )

    def test_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_audit.EV_WITHDRAW_REQUEST, "withdrawal.requested")
        self.assertEqual(_audit.EV_ACCOUNT_FUNDED, "account.funded")
        self.assertEqual(_audit.EV_ADMIN_ACTION, "admin.action")

    def test_broker_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_broker_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_broker_audit.EV_KYC_APPROVED, "compliance.kyc_approved")
        self.assertEqual(_broker_audit.EV_KYC_REJECTED, "compliance.kyc_rejected")
        self.assertEqual(_broker_audit.EV_RISK_ALERT_OBSERVED, "risk.alert_observed")


class NoFinancialOrOperationalLogicIntroducedTests(SimpleTestCase):
    """
    O.3d-1 itself introduces no monitor, no Celery task, no dashboard,
    no model, no migration and no settings value — only the one
    event-catalog constant above. Existing Treasury services remain
    exactly as O.3c-5 left them.
    """

    def test_treasury_requests_module_unaffected(self):
        from simulator.treasury_requests import (
            approve_treasury_request,
            execute_treasury_request,
            reject_treasury_request,
            submit_treasury_request,
        )
        self.assertTrue(callable(submit_treasury_request))
        self.assertTrue(callable(approve_treasury_request))
        self.assertTrue(callable(reject_treasury_request))
        self.assertTrue(callable(execute_treasury_request))

    def test_treasury_execution_recovery_module_unaffected(self):
        from simulator.treasury_execution_recovery import (
            inspect_stuck_treasury_execution,
            mark_treasury_execution_failed,
        )
        self.assertTrue(callable(inspect_stuck_treasury_execution))
        self.assertTrue(callable(mark_treasury_execution_failed))

    def test_no_new_celery_task_registered_for_this_event(self):
        """
        O.3d-3 superseded this guard on purpose, legitimately
        registering simulator.observe_treasury_stuck_executions. Same
        "guard correctly falsified by the next authorized block"
        pattern already used for O.3c-1 -> O.3c-3a, O.3c-4a -> O.3c-4b-1,
        and this file's own O.3d-2 supersession above.
        """
        from trx_simulator.celery import app as celery_app
        task_names = set(celery_app.tasks.keys())
        self.assertIn("simulator.observe_treasury_stuck_executions", task_names)

    def test_no_new_celery_beat_schedule_entry_introduced(self):
        """
        O.3d-3 superseded this guard on purpose, legitimately adding
        the "observe-treasury-stuck-executions-15m" CELERY_BEAT_SCHEDULE
        entry. Same supersession pattern as the task-registration guard
        above.
        """
        from django.conf import settings
        schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
        treasury_keys = [key for key in schedule if "treasury" in key.lower()]
        self.assertEqual(treasury_keys, ["observe-treasury-stuck-executions-15m"])

    def test_no_wallet_ledger_symbol_referenced_by_either_module(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("credit_wallet", source)
                self.assertNotIn("debit_wallet", source)
                self.assertNotIn("wallet_ledger", source)

    def test_no_new_settings_symbol_introduced(self):
        from django.conf import settings
        for name in (
            "TREASURY_STUCK_EXECUTION_OBSERVATION_DEDUP_WINDOW_SECONDS",
            "TREASURY_OBSERVATION_DEDUP_WINDOW_SECONDS",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(settings, name))

    def test_recovery_threshold_setting_unchanged(self):
        from django.conf import settings
        self.assertEqual(settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS, 600)
