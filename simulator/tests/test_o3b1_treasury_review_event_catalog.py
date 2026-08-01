# simulator/tests/test_o3b1_treasury_review_event_catalog.py
"""
Bloque O.3b-1 — Treasury Request Review Workflow Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_REQUEST_APPROVED = "treasury.request_approved" and
EV_TREASURY_REQUEST_REJECTED = "treasury.request_rejected" in both
simulator/audit.py and simulator/broker_audit.py, mirrored exactly —
same discipline already used by EV_TREASURY_REQUEST_SUBMITTED (O.3a-2)
between the two modules.

No approve/reject service, view, URL, template or button exists yet
that emits either event — nothing calls log_audit(), record_event() or
record_payment_event() with them. These tests only prove the constants
exist, are correctly mirrored, and have no productive call site yet;
they assert nothing about how they will eventually be used.
"""
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit


class EventConstantsExistTests(SimpleTestCase):

    def test_approved_constant_exists_in_audit_module(self):
        self.assertTrue(hasattr(_audit, "EV_TREASURY_REQUEST_APPROVED"))

    def test_rejected_constant_exists_in_audit_module(self):
        self.assertTrue(hasattr(_audit, "EV_TREASURY_REQUEST_REJECTED"))

    def test_approved_constant_exists_in_broker_audit_module(self):
        self.assertTrue(hasattr(_broker_audit, "EV_TREASURY_REQUEST_APPROVED"))

    def test_rejected_constant_exists_in_broker_audit_module(self):
        self.assertTrue(hasattr(_broker_audit, "EV_TREASURY_REQUEST_REJECTED"))

    def test_audit_approved_value(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")

    def test_audit_rejected_value(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")

    def test_broker_audit_approved_value(self):
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_APPROVED, "treasury.request_approved")

    def test_broker_audit_rejected_value(self):
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_REJECTED, "treasury.request_rejected")

    def test_approved_constants_mirrored_between_modules(self):
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_APPROVED,
            _broker_audit.EV_TREASURY_REQUEST_APPROVED,
        )

    def test_rejected_constants_mirrored_between_modules(self):
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_REJECTED,
            _broker_audit.EV_TREASURY_REQUEST_REJECTED,
        )

    def test_approved_and_rejected_are_distinct_values(self):
        self.assertNotEqual(
            _audit.EV_TREASURY_REQUEST_APPROVED, _audit.EV_TREASURY_REQUEST_REJECTED,
        )

    def test_constants_are_plain_str(self):
        for value in (
            _audit.EV_TREASURY_REQUEST_APPROVED, _audit.EV_TREASURY_REQUEST_REJECTED,
            _broker_audit.EV_TREASURY_REQUEST_APPROVED, _broker_audit.EV_TREASURY_REQUEST_REJECTED,
        ):
            self.assertIsInstance(value, str)


class NoProductiveUseYetTests(SimpleTestCase):
    """
    Confirms neither constant is yet wired into any real call site —
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

    def test_approved_string_appears_only_in_its_two_definition_sites(self):
        hits = []
        for path in self._iter_non_test_py_files():
            text = path.read_text(encoding="utf-8")
            if "treasury.request_approved" in text:
                hits.append(str(path.relative_to(self._SIMULATOR_DIR.parent)))
        self.assertEqual(
            sorted(hits),
            sorted(["simulator/audit.py", "simulator/broker_audit.py"]),
        )

    def test_rejected_string_appears_only_in_its_two_definition_sites(self):
        hits = []
        for path in self._iter_non_test_py_files():
            text = path.read_text(encoding="utf-8")
            if "treasury.request_rejected" in text:
                hits.append(str(path.relative_to(self._SIMULATOR_DIR.parent)))
        self.assertEqual(
            sorted(hits),
            sorted(["simulator/audit.py", "simulator/broker_audit.py"]),
        )

    def test_no_call_site_passes_either_constant_to_log_audit_or_record_event(self):
        import inspect

        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            for call in ("log_audit(", "record_event(", "record_payment_event(",
                          "record_compliance_event(", "record_admin_event("):
                for line in source.splitlines():
                    if call in line:
                        self.assertNotIn(
                            "EV_TREASURY_REQUEST_APPROVED", line,
                            f"unexpected productive call site in {module.__name__}: {line!r}",
                        )
                        self.assertNotIn(
                            "EV_TREASURY_REQUEST_REJECTED", line,
                            f"unexpected productive call site in {module.__name__}: {line!r}",
                        )


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    Guards against accidentally editing a pre-existing constant while
    adding the two new ones — same spot-check discipline used in O.3a-2.
    """

    def test_audit_py_existing_constants_unchanged(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")
        self.assertEqual(_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_audit.EV_WITHDRAW_REQUEST, "withdrawal.requested")
        self.assertEqual(_audit.EV_ACCOUNT_FUNDED, "account.funded")
        self.assertEqual(_audit.EV_ADMIN_ACTION, "admin.action")

    def test_broker_audit_py_existing_constants_unchanged(self):
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")
        self.assertEqual(_broker_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_broker_audit.EV_KYC_APPROVED, "compliance.kyc_approved")
        self.assertEqual(_broker_audit.EV_KYC_REJECTED, "compliance.kyc_rejected")
