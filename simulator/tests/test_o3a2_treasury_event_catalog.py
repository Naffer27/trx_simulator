# simulator/tests/test_o3a2_treasury_event_catalog.py
"""
Bloque O.3a-2 — Treasury Request Submission Event Catalog.

Covers ONLY the single change this block makes: declaring
EV_TREASURY_REQUEST_SUBMITTED = "treasury.request_submitted" in both
simulator/audit.py and simulator/broker_audit.py, mirrored exactly —
same discipline already used by EV_DEPOSIT_CREDITED between the two
modules (audit.py = AuditLog, request-scoped; broker_audit.py =
BrokerAuditEvent, institutional cross-engine trail; no import coupling
between the two, each module owns its own constant).

No view, form, service, model, migration, URL or template exists yet
that emits this event — nothing calls log_audit(), record_event() or
record_payment_event() with it. These tests only prove the constant
exists, is correctly mirrored, and has no productive call site yet;
they assert nothing about how it will eventually be used.
"""
import inspect
import pathlib

from django.test import SimpleTestCase

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit


class EventConstantExistsTests(SimpleTestCase):

    def test_constant_exists_in_audit_module(self):
        self.assertTrue(hasattr(_audit, "EV_TREASURY_REQUEST_SUBMITTED"))

    def test_constant_exists_in_broker_audit_module(self):
        self.assertTrue(hasattr(_broker_audit, "EV_TREASURY_REQUEST_SUBMITTED"))

    def test_audit_constant_has_expected_value(self):
        self.assertEqual(_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")

    def test_broker_audit_constant_has_expected_value(self):
        self.assertEqual(_broker_audit.EV_TREASURY_REQUEST_SUBMITTED, "treasury.request_submitted")

    def test_both_constants_are_equal_to_each_other(self):
        self.assertEqual(
            _audit.EV_TREASURY_REQUEST_SUBMITTED,
            _broker_audit.EV_TREASURY_REQUEST_SUBMITTED,
        )

    def test_constant_is_a_plain_str_in_both_modules(self):
        self.assertIsInstance(_audit.EV_TREASURY_REQUEST_SUBMITTED, str)
        self.assertIsInstance(_broker_audit.EV_TREASURY_REQUEST_SUBMITTED, str)


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
            if "treasury.request_submitted" in text:
                hits.append(path.relative_to(self._SIMULATOR_DIR.parent))

        hit_names = sorted(str(p) for p in hits)
        self.assertEqual(
            hit_names,
            sorted([
                "simulator/audit.py",
                "simulator/broker_audit.py",
            ]),
        )

    def test_no_call_site_passes_the_constant_to_log_audit_or_record_event(self):
        for module in (_audit, _broker_audit):
            source = inspect.getsource(module)
            for call in ("log_audit(", "record_event(", "record_payment_event(",
                          "record_compliance_event(", "record_admin_event("):
                for line in source.splitlines():
                    if call in line:
                        self.assertNotIn(
                            "EV_TREASURY_REQUEST_SUBMITTED", line,
                            f"unexpected productive call site in {module.__name__}: {line!r}",
                        )


class ExistingCatalogUnchangedTests(SimpleTestCase):
    """
    Guards against accidentally editing a pre-existing constant while
    adding the new one — same spot-check discipline used elsewhere in
    this project's O.2 blocks (assert known values, not just presence).
    """

    def test_audit_py_existing_constants_unchanged(self):
        self.assertEqual(_audit.EV_DEPOSIT_CREATED, "deposit.created")
        self.assertEqual(_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_audit.EV_WITHDRAW_REQUEST, "withdrawal.requested")
        self.assertEqual(_audit.EV_ACCOUNT_FUNDED, "account.funded")
        self.assertEqual(_audit.EV_ADMIN_ACTION, "admin.action")

    def test_broker_audit_py_existing_constants_unchanged(self):
        self.assertEqual(_broker_audit.EV_DEPOSIT_CREDITED, "deposit.credited")
        self.assertEqual(_broker_audit.EV_KYC_APPROVED, "compliance.kyc_approved")
        self.assertEqual(_broker_audit.EV_KYC_REJECTED, "compliance.kyc_rejected")
        self.assertEqual(
            _broker_audit.EV_FUNDED_PAYOUT_SIM_APPROVED,
            "payment.funded_payout_sim_approved",
        )

    def test_log_audit_function_signature_unchanged(self):
        sig = inspect.signature(_audit.log_audit)
        self.assertEqual(
            list(sig.parameters),
            ["request", "event_type", "action", "account", "detail"],
        )
