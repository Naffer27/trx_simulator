# simulator/tests/test_o3c1_treasury_execution_event_catalog.py
"""
Bloque O.3c-1 — Treasury Execution Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_REQUEST_EXECUTION_STARTED = "treasury.request_execution_started",
EV_TREASURY_REQUEST_EXECUTED = "treasury.request_executed", and
EV_TREASURY_REQUEST_EXECUTION_FAILED = "treasury.request_execution_failed"
in both simulator/audit.py and simulator/broker_audit.py, mirrored
exactly — same discipline already used by EV_TREASURY_REQUEST_SUBMITTED
(O.3a-2) and EV_TREASURY_REQUEST_APPROVED/REJECTED (O.3b-1) between the
two modules.

No execution service, view, URL, template or button exists yet that
emits any of these three events — nothing calls log_audit(),
record_event() or record_payment_event() with them. No WalletTransaction
or InternalTransfer is ever created, and no EXECUTING/EXECUTED/FAILED
transition is implemented anywhere. These tests only prove the constants
exist, are correctly mirrored, and have no productive call site yet.
"""
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit

_NEW_CONSTANTS = (
    ("EV_TREASURY_REQUEST_EXECUTION_STARTED", "treasury.request_execution_started"),
    ("EV_TREASURY_REQUEST_EXECUTED", "treasury.request_executed"),
    ("EV_TREASURY_REQUEST_EXECUTION_FAILED", "treasury.request_execution_failed"),
)


class EventConstantsExistTests(SimpleTestCase):

    def test_all_three_constants_exist_in_audit_module(self):
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(_audit, name))

    def test_all_three_constants_exist_in_broker_audit_module(self):
        for name, _value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(_broker_audit, name))

    def test_audit_module_values_match_approved_literals(self):
        for name, expected_value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(_audit, name), expected_value)

    def test_broker_audit_module_values_match_approved_literals(self):
        for name, expected_value in _NEW_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(_broker_audit, name), expected_value)

    def test_each_pair_has_exactly_the_same_value(self):
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


class NoProductiveUseYetTests(SimpleTestCase):
    """
    Confirms none of the three constants is yet wired into any real
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

    def test_no_wallet_transaction_or_execution_service_symbols_introduced(self):
        # Sanity check on scope: this block must not have created any
        # execution-related service/model symbol as a side effect.
        self.assertFalse(hasattr(_audit, "execute_treasury_request"))
        self.assertFalse(hasattr(_broker_audit, "execute_treasury_request"))


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    Guards against accidentally editing a pre-existing constant while
    adding the three new ones — same spot-check discipline used in
    O.3a-2 and O.3b-1. Explicitly re-checks the O.3a/O.3b Treasury
    constants stay intact, plus a sample of older, unrelated events.
    """

    def test_o3a2_submission_constants_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")

    def test_o3b1_review_constants_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")
        self.assertEqual(_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")

    def test_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_audit.EV_WITHDRAW_REQUEST, "withdrawal.requested")
        self.assertEqual(_audit.EV_ACCOUNT_FUNDED, "account.funded")
        self.assertEqual(_audit.EV_ADMIN_ACTION, "admin.action")

    def test_broker_audit_py_unrelated_constants_unchanged(self):
        self.assertEqual(_broker_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_broker_audit.EV_KYC_APPROVED, "compliance.kyc_approved")
        self.assertEqual(_broker_audit.EV_KYC_REJECTED, "compliance.kyc_rejected")


class NoFinancialExecutionIntroducedTests(SimpleTestCase):
    """
    8 — O.3c-1 itself introduced no financial execution logic, only the
    three event-catalog constants above.

    Updated in O.3c-3a: execute_treasury_request() now legitimately
    exists (implemented and authorized in O.3c-3), so this class no
    longer asserts its absence. Instead it asserts the architecture that
    replaced that guard: the implementation lives exclusively in
    treasury_requests.py, and no other module defines its own financial
    execution entry point under this name.
    """

    def test_execute_treasury_request_exists_and_lives_only_in_treasury_requests(self):
        from simulator import treasury_requests
        self.assertTrue(hasattr(treasury_requests, "execute_treasury_request"))
        self.assertTrue(callable(treasury_requests.execute_treasury_request))
        self.assertEqual(
            treasury_requests.execute_treasury_request.__module__,
            "simulator.treasury_requests",
        )

    def test_no_other_module_implements_financial_execution(self):
        import simulator.admin as admin_module
        import simulator.views as views_module
        import simulator.wallet_ledger as wallet_ledger_module

        for module in (admin_module, views_module, wallet_ledger_module):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "execute_treasury_request"))

    def test_treasury_requests_module_unaffected(self):
        from simulator.treasury_requests import (
            approve_treasury_request,
            reject_treasury_request,
            submit_treasury_request,
        )
        self.assertTrue(callable(submit_treasury_request))
        self.assertTrue(callable(approve_treasury_request))
        self.assertTrue(callable(reject_treasury_request))
