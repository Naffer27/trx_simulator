# simulator/tests/test_o3e1_treasury_cancel_workflow_event_catalog.py
"""
Bloque O.3e-1 — Treasury Cancel Workflow Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_REQUEST_CANCELLED = "treasury.request_cancelled" in both
simulator/audit.py and simulator/broker_audit.py, mirrored exactly —
same discipline already used by every prior Treasury constant
(EV_TREASURY_REQUEST_SUBMITTED/APPROVED/REJECTED/EXECUTION_STARTED/
EXECUTED/EXECUTION_FAILED/EXECUTION_RECOVERY_STARTED/MARKED_FAILED/
RECOVERY_BLOCKED/STUCK_EXECUTION_OBSERVED).

No cancellation service, no view, no URL, no template, no button and
no migration exists yet that emits or consumes this event — nothing
calls log_audit(), record_event() or record_payment_event() with it,
and nothing reads it either. TreasuryOperationRequest.cancel_
treasury_request() is entirely unaffected because it does not exist
yet. Frozen Fase 0 decisions this block respects structurally:

    1. No cancellation_reason field is added to TreasuryOperationRequest.
    2. Any cancellation reason text will live exclusively in this
       event's own AuditLog/BrokerAuditEvent detail/metadata.
    5. No new permission is introduced — cancellation reuses
       can_submit_treasury_request (implicit, via requested_by ==
       request.user) and can_review_treasury_request, both of which
       already exist.
"""
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit

_NEW_CONSTANT_NAME  = "EV_TREASURY_REQUEST_CANCELLED"
_NEW_CONSTANT_VALUE = "treasury.request_cancelled"


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
            _audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED,
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

    def test_no_cancellation_service_or_view_symbols_introduced_in_audit_modules(self):
        for name in (
            "cancel_treasury_request",
            "treasury_request_cancel_view",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(_audit, name))
                self.assertFalse(hasattr(_broker_audit, name))

    def test_no_treasury_request_service_symbols_leaked_into_audit_modules(self):
        """
        submit_/approve_/reject_/execute_treasury_request() live only
        in treasury_requests.py, and inspect_stuck_treasury_execution()/
        mark_treasury_execution_failed() live only in treasury_
        execution_recovery.py — none of the four should ever leak into
        audit.py or broker_audit.py. Deliberately does NOT include
        observe_stuck_treasury_executions() here: that one legitimately
        lives IN broker_audit.py itself since O.3d-2 (a different,
        already-approved placement, not a "not yet" case) — its own
        O.3d-1 test file already covers that placement correctly.
        """
        for name in (
            "submit_treasury_request", "approve_treasury_request",
            "reject_treasury_request", "execute_treasury_request",
            "inspect_stuck_treasury_execution", "mark_treasury_execution_failed",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(_audit, name))
                self.assertFalse(hasattr(_broker_audit, name))


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    Guards against accidentally editing a pre-existing constant while
    adding the new one — same spot-check discipline used by every
    prior Treasury event-catalog block. Explicitly re-checks every
    prior Treasury constant stays intact, plus a sample of older,
    unrelated events.
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

    def test_o3d1_observation_constant_unchanged(self):
        self.assertEqual(
            _audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED, "treasury.stuck_execution_observed",
        )
        self.assertEqual(
            _broker_audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED, "treasury.stuck_execution_observed",
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


class FrozenFase0DecisionsTests(SimpleTestCase):
    """
    Structural guards for the 7 Fase 0 decisions approved for O.3e-1,
    re-verified here so a later microbloque cannot silently drift from
    them without this test failing first.
    """

    def test_no_cancellation_reason_field_on_the_model(self):
        """Decision 1 — no new model field."""
        field_names = {f.name for f in TreasuryOperationRequestFields()}
        self.assertNotIn("cancellation_reason", field_names)
        self.assertNotIn("cancel_reason", field_names)

    def test_cancelled_at_and_st_cancelled_already_exist_unchanged(self):
        """The O.2g-1a schema this block builds on top of is untouched."""
        from simulator.models import TreasuryOperationRequest

        field_names = {f.name for f in TreasuryOperationRequest._meta.fields}
        self.assertIn("cancelled_at", field_names)
        self.assertEqual(TreasuryOperationRequest.ST_CANCELLED, "CANCELLED")

    def test_no_new_permission_introduced(self):
        """Decision 5 — no new permission. Exactly the same four
        Treasury permissions from O.3a-1/O.3c-4c must still be the
        complete set."""
        from simulator.models import TreasuryOperationRequest

        codenames = {codename for codename, _label in TreasuryOperationRequest._meta.permissions}
        self.assertEqual(
            codenames,
            {
                "can_submit_treasury_request",
                "can_review_treasury_request",
                "can_execute_treasury_request",
                "can_recover_treasury_execution",
            },
        )

    def test_treasury_requests_module_unaffected(self):
        """
        Decision 6 — no financial logic touched; existing services
        remain exactly as O.3c-5 left them.

        O.3e-2 superseded this guard on purpose, legitimately adding
        cancel_treasury_request() to treasury_requests.py. Same "guard
        correctly falsified by the next authorized block" pattern
        already used for O.3c-1 -> O.3c-3a, O.3c-4a -> O.3c-4b-1, and
        O.3d-1 -> O.3d-2/O.3d-3.
        """
        from simulator.treasury_requests import (
            approve_treasury_request,
            cancel_treasury_request,
            execute_treasury_request,
            reject_treasury_request,
            submit_treasury_request,
        )
        self.assertTrue(callable(submit_treasury_request))
        self.assertTrue(callable(approve_treasury_request))
        self.assertTrue(callable(reject_treasury_request))
        self.assertTrue(callable(execute_treasury_request))
        self.assertTrue(callable(cancel_treasury_request))
        self.assertEqual(cancel_treasury_request.__module__, "simulator.treasury_requests")

    def test_wallet_ledger_and_recovery_service_unaffected(self):
        """Decision 6 — wallet_ledger.py, execute_treasury_request(),
        mark_treasury_execution_failed() and the Treasury execution
        recovery service are all untouched by this block."""
        from simulator.treasury_execution_recovery import (
            inspect_stuck_treasury_execution,
            mark_treasury_execution_failed,
        )
        from simulator.wallet_ledger import credit_wallet, debit_wallet

        self.assertTrue(callable(credit_wallet))
        self.assertTrue(callable(debit_wallet))
        self.assertTrue(callable(inspect_stuck_treasury_execution))
        self.assertTrue(callable(mark_treasury_execution_failed))

    def test_no_new_settings_symbol_introduced(self):
        from django.conf import settings
        self.assertFalse(hasattr(settings, "TREASURY_CANCEL_REASON_MAX_LENGTH"))


def TreasuryOperationRequestFields():
    from simulator.models import TreasuryOperationRequest
    return TreasuryOperationRequest._meta.fields
