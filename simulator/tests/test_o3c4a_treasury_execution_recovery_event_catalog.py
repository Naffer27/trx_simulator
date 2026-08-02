# simulator/tests/test_o3c4a_treasury_execution_recovery_event_catalog.py
"""
Bloque O.3c-4a — Treasury Execution Recovery Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED = "treasury.request_execution_recovery_started",
EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED = "treasury.request_execution_marked_failed", and
EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED = "treasury.request_execution_recovery_blocked"
in both simulator/audit.py and simulator/broker_audit.py, mirrored
exactly — same discipline already used by EV_TREASURY_REQUEST_SUBMITTED
(O.3a-2), EV_TREASURY_REQUEST_APPROVED/REJECTED (O.3b-1), and
EV_TREASURY_REQUEST_EXECUTION_STARTED/EXECUTED/EXECUTION_FAILED (O.3c-1).

No inspection service, no recovery service, no view, URL, template,
button, permission or settings value exists yet that emits or consumes
any of these three events — nothing calls log_audit(), record_event()
or record_payment_event() with them, and nothing reads them either.
No TreasuryOperationRequest transition, no credit_wallet()/
debit_wallet() call, no WalletTransaction is created or touched by this
block. There is no EV_TREASURY_REQUEST_EXECUTION_RESET_TO_APPROVED
constant — that recovery action was evaluated and explicitly rejected
in the O.3c-4 Fase 0 design (approved): only "Mark FAILED" and "Leave
unchanged / Manual investigation" are permitted recovery outcomes.
"""
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit

_NEW_CONSTANTS = (
    ("EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED", "treasury.request_execution_recovery_started"),
    ("EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED",    "treasury.request_execution_marked_failed"),
    ("EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED", "treasury.request_execution_recovery_blocked"),
)

_REJECTED_CONSTANT_NAMES = (
    "EV_TREASURY_REQUEST_EXECUTION_RESET_TO_APPROVED",
    "EV_TREASURY_REQUEST_EXECUTION_RESET",
    "EV_TREASURY_REQUEST_RESET_TO_APPROVED",
)


class EventConstantsExistTests(SimpleTestCase):
    """1, 2 — the three constants exist in both modules."""

    def test_all_three_constants_exist_in_audit_module(self):
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(_audit, name))

    def test_all_three_constants_exist_in_broker_audit_module(self):
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(_broker_audit, name))

    def test_audit_module_values_match_approved_literals(self):
        """4 — literal values match exactly what O.3c-4 Fase 0 approved."""
        for name, expected_value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(_audit, name), expected_value)

    def test_broker_audit_module_values_match_approved_literals(self):
        """4 — literal values match exactly what O.3c-4 Fase 0 approved."""
        for name, expected_value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(_broker_audit, name), expected_value)

    def test_each_pair_has_exactly_the_same_value(self):
        """3 — audit.py and broker_audit.py are mirrored exactly."""
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(_audit, name), getattr(_broker_audit, name))

    def test_all_three_values_are_distinct_from_each_other(self):
        audit_values = {getattr(_audit, name) for name, _ in _NEW_CONSTANTS}
        self.assertEqual(len(audit_values), 3)

    def test_constants_are_plain_str(self):
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertIsInstance(getattr(_audit, name), str)
                self.assertIsInstance(getattr(_broker_audit, name), str)


class ResetToApprovedEventDoesNotExistTests(SimpleTestCase):
    """6 — no reset_to_approved event exists anywhere; that action was rejected."""

    def test_no_reset_to_approved_constant_in_audit_module(self):
        for name in _REJECTED_CONSTANT_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(_audit, name))

    def test_no_reset_to_approved_constant_in_broker_audit_module(self):
        for name in _REJECTED_CONSTANT_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(_broker_audit, name))

    def test_no_reset_to_approved_string_literal_anywhere_in_either_module(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("reset_to_approved", source)
                self.assertNotIn("RESET_TO_APPROVED", source)


class NoProductiveUseYetTests(SimpleTestCase):
    """
    5 — confirms none of the three constants is yet wired into any real
    call site — grep-based, over source files, not behavior-based,
    because no behavior exists yet to test.
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

    def test_each_event_string_appears_only_in_its_two_definition_sites(self):
        for _name, value in _NEW_CONSTANTS:
            with self.subTest(value=value):
                hits = []
                for path in self._iter_non_test_py_files():
                    text = path.read_text(encoding="utf-8")
                    if value in text:
                        hits.append(str(path.relative_to(self._SIMULATOR_DIR.parent)))
                self.assertEqual(
                    sorted(hits),
                    sorted(["simulator/audit.py", "simulator/broker_audit.py"]),
                )

    def test_no_call_site_passes_any_new_constant_to_log_audit_or_record_event(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            for call in ("log_audit(", "record_event(", "record_payment_event(",
                          "record_compliance_event(", "record_admin_event("):
                for line in source.splitlines():
                    if call in line:
                        for name, _value in _NEW_CONSTANTS:
                            self.assertNotIn(
                                name, line,
                                f"unexpected productive call site in {module.__name__}: {line!r}",
                            )

    def test_no_recovery_service_symbols_leaked_into_audit_modules(self):
        """8 — audit.py/broker_audit.py themselves still carry no recovery logic."""
        self.assertFalse(hasattr(_audit, "inspect_stuck_treasury_execution"))
        self.assertFalse(hasattr(_audit, "mark_treasury_execution_failed"))
        self.assertFalse(hasattr(_broker_audit, "inspect_stuck_treasury_execution"))
        self.assertFalse(hasattr(_broker_audit, "mark_treasury_execution_failed"))

    def test_treasury_execution_recovery_module_exists(self):
        """
        1 — O.3c-4b-1: simulator.treasury_execution_recovery now exists.
        O.3c-4a's own guard here (asserting ModuleNotFoundError) was a
        forward-looking placeholder for a module that O.3c-4b — reviewed,
        approved, and closed — legitimately created. Same "guard
        correctly falsified by the next authorized block" pattern
        already handled once for O.3c-1 -> O.3c-3 via O.3c-3a.
        """
        import importlib
        module = importlib.import_module("simulator.treasury_execution_recovery")
        self.assertIsNotNone(module)

    def test_inspect_stuck_treasury_execution_exists(self):
        """2 — inspect_stuck_treasury_execution() exists and is callable."""
        from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution
        self.assertTrue(callable(inspect_stuck_treasury_execution))

    def test_mark_treasury_execution_failed_exists(self):
        """
        3, 4 — O.3c-4c-1: mark_treasury_execution_failed() now exists —
        the exact candidate name from the O.3c-4 Fase 0 design,
        implemented and authorized in O.3c-4c. O.3c-4b's own guard here
        (asserting its absence) predates that authorized addition. Same
        "guard correctly falsified by the next authorized block"
        pattern already handled for O.3c-1 -> O.3c-3 (via O.3c-3a) and
        O.3c-4a -> O.3c-4b (via O.3c-4b-1).
        """
        from simulator.treasury_execution_recovery import mark_treasury_execution_failed
        self.assertTrue(callable(mark_treasury_execution_failed))
        self.assertEqual(
            mark_treasury_execution_failed.__module__, "simulator.treasury_execution_recovery",
        )

    def test_module_implements_only_the_one_approved_mutable_action(self):
        """
        5, 6 — the module's only mutable recovery action is
        mark_treasury_execution_failed() (EXECUTING -> FAILED). No other
        plausible recovery function exists under any name — in
        particular "Reset to APPROVED" and "Reconcile from
        wallet_transaction" were both evaluated and explicitly rejected
        in the O.3c-4 Fase 0 design (approved) and must never appear
        here.
        """
        import simulator.treasury_execution_recovery as module

        for name in (
            "mark_failed_stuck_execution",
            "recover_stuck_execution",
            "reset_to_approved",
            "reset_treasury_execution_to_approved",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name))

    def test_no_new_permission_or_settings_symbol_introduced(self):
        """8 — no permission and no settings constant leaked into these modules."""
        self.assertFalse(hasattr(_audit, "TREASURY_RECOVER_PERMISSION"))
        self.assertFalse(hasattr(_broker_audit, "TREASURY_RECOVER_PERMISSION"))
        self.assertFalse(hasattr(_audit, "TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS"))
        self.assertFalse(hasattr(_broker_audit, "TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS"))


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    7 — guards against accidentally editing a pre-existing constant while
    adding the three new ones — same spot-check discipline used in
    O.3a-2, O.3b-1 and O.3c-1. Explicitly re-checks the O.3a/O.3b/O.3c
    Treasury constants stay intact, plus a sample of older, unrelated
    events.
    """

    def test_o3a2_submission_constants_unchanged(self):
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

    def test_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_audit.EV_WITHDRAW_REQUEST, "withdrawal.requested")
        self.assertEqual(_audit.EV_ACCOUNT_FUNDED, "account.funded")
        self.assertEqual(_audit.EV_ADMIN_ACTION, "admin.action")

    def test_broker_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_broker_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_broker_audit.EV_KYC_APPROVED, "compliance.kyc_approved")
        self.assertEqual(_broker_audit.EV_KYC_REJECTED, "compliance.kyc_rejected")


class NoRecoveryOrFinancialLogicIntroducedTests(SimpleTestCase):
    """8 — no recovery logic and no financial movement was introduced by this block."""

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

    def test_no_wallet_ledger_symbol_referenced_by_either_module(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("credit_wallet", source)
                self.assertNotIn("debit_wallet", source)
                self.assertNotIn("wallet_ledger", source)

    def test_recovery_threshold_setting_exists(self):
        """
        7 — O.3c-4b-1: TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS now
        exists in settings, added by O.3c-4b (reviewed, approved,
        closed). O.3c-4a's own guard here (asserting hasattr() was
        False) predates that authorized addition.
        """
        from django.conf import settings
        self.assertTrue(hasattr(settings, "TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS"))

    def test_recovery_threshold_default_value_is_600(self):
        """8 — its default value is 600 seconds, per O.3c-4 Fase 0's approved threshold."""
        from django.conf import settings
        self.assertEqual(settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS, 600)
