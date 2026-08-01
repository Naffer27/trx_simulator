# simulator/tests/test_o3a4_treasury_submission_service.py
"""
Bloque O.3a-4 — Treasury Request Submission Service.

Covers ONLY simulator/treasury_requests.py::submit_treasury_request() —
the first real connection between TreasuryOperationRequestForm (O.3a-3),
the "can_submit_treasury_request" permission (O.3a-1) and the
treasury.request_submitted event catalog (O.3a-2).

No view, URL, template or button exists as a result of this block — every
test here calls submit_treasury_request(form, request=request) directly.
No approval, rejection, cancellation or execution logic is exercised
(none exists). No WalletTransaction/InternalTransfer is ever created and
no Wallet balance field ever changes — asserted explicitly in every test
class below.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from simulator.forms import TreasuryOperationRequestForm
from simulator.models import (
    AuditLog, InternalTransfer, TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_requests import (
    TREASURY_SUBMIT_PERMISSION, submit_treasury_request,
)

from .factories import make_user, make_wallet


def _grant_submit_permission(user):
    perm = Permission.objects.get(codename="can_submit_treasury_request")
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_operator(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant_submit_permission(user)


def _valid_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "42.50",
        "reason": "Welcome bonus campaign",
    }
    data.update(overrides)
    return data


def _request_for(user):
    request = RequestFactory().post("/admin/treasury-request/new/")
    request.user = user
    return request


class SuccessfulCreationTests(TestCase):
    """1-9 — creación exitosa por cada operation_type + campos + auditoría."""

    def setUp(self):
        self.operator = _make_operator(username="o3a4_operator")
        self.wallet = make_wallet()

    def _valid_data_for(self, operation_type):
        base = {
            "wallet": self.wallet.pk,
            "operation_type": operation_type,
            "amount": "100.00",
            "reason": "O.3a-4 service test",
        }
        if operation_type in (
            TreasuryOperationRequest.OP_CREDIT_FUNDS,
            TreasuryOperationRequest.OP_DEBIT_FUNDS,
            TreasuryOperationRequest.OP_MANUAL_CREDIT,
            TreasuryOperationRequest.OP_MANUAL_DEBIT,
        ):
            base["category"] = TreasuryOperationRequest.CAT_OTHER
        if operation_type in (
            TreasuryOperationRequest.OP_REFUND,
            TreasuryOperationRequest.OP_IB_COMMISSION,
            TreasuryOperationRequest.OP_MANUAL_CREDIT,
            TreasuryOperationRequest.OP_MANUAL_DEBIT,
        ):
            base["reference"] = "REF-O3A4"
        if operation_type in (
            TreasuryOperationRequest.OP_MANUAL_CREDIT,
            TreasuryOperationRequest.OP_MANUAL_DEBIT,
        ):
            base["comment"] = "Documented manually for this test."
        return base

    def test_successful_creation_for_each_operation_type(self):
        for op_type, _ in TreasuryOperationRequest.OPERATION_TYPE_CHOICES:
            with self.subTest(operation_type=op_type):
                wallet = make_wallet()
                data = self._valid_data_for(op_type)
                data["wallet"] = wallet.pk
                form = TreasuryOperationRequestForm(data=data)
                self.assertTrue(form.is_valid(), form.errors)

                request = _request_for(self.operator)
                instance = submit_treasury_request(form, request=request)

                self.assertIsNotNone(instance.pk)
                self.assertEqual(instance.operation_type, op_type)
                self.assertEqual(instance.status, TreasuryOperationRequest.ST_PENDING)

    def test_instance_status_is_pending(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(instance.status, TreasuryOperationRequest.ST_PENDING)

    def test_requested_by_is_assigned(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(instance.requested_by_id, self.operator.pk)

    def test_currency_copied_from_wallet(self):
        wallet = make_wallet()
        wallet.currency = "EUR"
        wallet.save(update_fields=["currency"])
        form = TreasuryOperationRequestForm(data=_valid_data(wallet))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(instance.currency, "EUR")

    def test_wallet_transaction_is_none(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))
        self.assertIsNone(instance.wallet_transaction)

    def test_exactly_one_treasury_operation_request_created(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(TreasuryOperationRequest.objects.count(), 1)

    def test_exactly_one_auditlog_created(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_submitted").count(), 1,
        )

    def test_exactly_one_brokerauditevent_created(self):
        from simulator.models import BrokerAuditEvent
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_submitted").count(), 1,
        )

    def test_auditlog_detail_content(self):
        form = TreasuryOperationRequestForm(data=_valid_data(
            self.wallet, reference="TCK-9", category="",
        ))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))

        row = AuditLog.objects.get(event_type="treasury.request_submitted")
        self.assertEqual(row.detail["treasury_request_id"], instance.pk)
        self.assertEqual(row.detail["operation_type"], instance.operation_type)
        self.assertEqual(row.detail["wallet_id"], instance.wallet_id)
        self.assertEqual(row.detail["wallet_user_id"], instance.wallet.user_id)
        self.assertEqual(row.detail["amount"], "42.50")
        self.assertEqual(row.detail["currency"], instance.currency)
        self.assertEqual(row.detail["category"], instance.category)
        self.assertEqual(row.detail["reference"], "TCK-9")
        self.assertEqual(row.detail["has_evidence"], False)

    def test_brokerauditevent_metadata_content(self):
        from simulator.models import BrokerAuditEvent
        from simulator import broker_audit as _broker_audit

        form = TreasuryOperationRequestForm(data=_valid_data(
            self.wallet, reference="TCK-9", category="",
        ))
        self.assertTrue(form.is_valid(), form.errors)
        instance = submit_treasury_request(form, request=_request_for(self.operator))

        row = BrokerAuditEvent.objects.get(event_type="treasury.request_submitted")
        self.assertEqual(row.metadata["treasury_operation_request_id"], instance.pk)
        self.assertEqual(row.metadata["operation_type"], instance.operation_type)
        self.assertEqual(row.metadata["wallet_id"], instance.wallet_id)
        self.assertEqual(row.metadata["wallet_user_id"], instance.wallet.user_id)
        self.assertEqual(row.metadata["amount"], "42.50")
        self.assertEqual(row.metadata["currency"], instance.currency)
        self.assertEqual(row.metadata["status"], TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(row.metadata["category"], instance.category)
        self.assertEqual(row.metadata["reference"], "TCK-9")
        self.assertEqual(row.metadata["has_evidence"], False)
        self.assertEqual(row.severity, _broker_audit.Severity.WARNING)
        self.assertEqual(row.actor_type, _broker_audit.ActorType.STAFF)
        self.assertEqual(row.actor_id, self.operator.pk)
        self.assertEqual(row.source_module, "simulator.treasury_requests")
        self.assertEqual(row.category, _broker_audit.Category.PAYMENTS)

    def test_evidence_file_never_stored_in_json_only_boolean_flag(self):
        from simulator.models import BrokerAuditEvent

        f = SimpleUploadedFile("proof.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))

        audit_row = AuditLog.objects.get(event_type="treasury.request_submitted")
        broker_row = BrokerAuditEvent.objects.get(event_type="treasury.request_submitted")

        self.assertEqual(audit_row.detail["has_evidence"], True)
        self.assertEqual(broker_row.metadata["has_evidence"], True)

        for blob in (audit_row.detail, broker_row.metadata):
            for value in blob.values():
                self.assertNotIn("proof.pdf", str(value))
                self.assertNotIn("PDF-1.4", str(value))


class PermissionAndAuthContractTests(TestCase):
    """11-12 — usuario no autenticado / sin permiso: no crea nada."""

    def setUp(self):
        self.wallet = make_wallet()

    def test_unauthenticated_user_raises_permission_denied_and_creates_nothing(self):
        request = RequestFactory().post("/admin/treasury-request/new/")
        request.user = AnonymousUser()

        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)

        with self.assertRaises(PermissionDenied):
            submit_treasury_request(form, request=request)

        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)
        from simulator.models import BrokerAuditEvent
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)

    def test_staff_without_permission_raises_permission_denied_and_creates_nothing(self):
        staff = make_user(username="o3a4_no_perm", is_staff=True)
        self.assertFalse(staff.has_perm(TREASURY_SUBMIT_PERMISSION))

        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)

        with self.assertRaises(PermissionDenied):
            submit_treasury_request(form, request=_request_for(staff))

        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)


class FormValidationContractTests(TestCase):
    """13-14 — formulario no validado / inválido: no crea nada."""

    def setUp(self):
        self.operator = _make_operator(username="o3a4_form_contract")
        self.wallet = make_wallet()

    def test_never_validated_form_raises_value_error_and_creates_nothing(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        # Deliberately NOT calling form.is_valid()/form.errors first.
        self.assertFalse(hasattr(form, "cleaned_data"))

        with self.assertRaises(ValueError):
            submit_treasury_request(form, request=_request_for(self.operator))

        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_invalid_form_raises_validation_error_and_creates_nothing(self):
        data = _valid_data(self.wallet)
        del data["amount"]  # amount is required — makes the form invalid
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())

        with self.assertRaises(ValidationError):
            submit_treasury_request(form, request=_request_for(self.operator))

        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)


class NoFinancialSideEffectsTests(TestCase):
    """15-17 — sin WalletTransaction, sin cambio de balance, sin InternalTransfer."""

    def setUp(self):
        self.operator = _make_operator(username="o3a4_no_money")
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))

    def test_no_wallet_transaction_created(self):
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(before, after)

    def test_wallet_balance_unchanged(self):
        balance_before = self.wallet.available_balance
        pending_before = self.wallet.pending_balance
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(self.wallet.pending_balance, pending_before)

    def test_no_internal_transfer_created(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)
        submit_treasury_request(form, request=_request_for(self.operator))
        self.assertEqual(InternalTransfer.objects.count(), 0)


class AuditFailureIsolationTests(TestCase):
    """
    18-19 — un fallo simulado en la escritura de auditoría no revierte
    la solicitud ya creada. En producción log_audit()/record_event() son
    fail-open por su propio contrato (nunca lanzan) — este test fuerza
    artificialmente una excepción vía mock para demostrar la propiedad
    transaccional en sí: la creación ya está commiteada antes de que se
    invoque cualquiera de los dos helpers de auditoría.
    """

    def setUp(self):
        self.operator = _make_operator(username="o3a4_audit_fail")
        self.wallet = make_wallet()

    def test_simulated_auditlog_failure_does_not_revert_the_request(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)

        with patch("simulator.treasury_requests.audit.log_audit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                submit_treasury_request(form, request=_request_for(self.operator))

        self.assertEqual(TreasuryOperationRequest.objects.count(), 1)

    def test_simulated_brokerauditevent_failure_does_not_revert_the_request(self):
        form = TreasuryOperationRequestForm(data=_valid_data(self.wallet))
        self.assertTrue(form.is_valid(), form.errors)

        with patch(
            "simulator.treasury_requests.broker_audit.record_payment_event",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                submit_treasury_request(form, request=_request_for(self.operator))

        self.assertEqual(TreasuryOperationRequest.objects.count(), 1)
        # AuditLog is written before record_payment_event, so it still exists.
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_submitted").count(), 1,
        )


class NoWalletLedgerOrFundedPayoutsUsageTests(TestCase):
    """
    20 — submit_treasury_request() no llama wallet_ledger.py ni
    funded_payouts.py.

    Scoped to submit_treasury_request() specifically (via
    inspect.getsource(module.submit_treasury_request), not the whole
    module) since O.3c-3 legitimately added execute_treasury_request()
    to this same module, which DOES import and call
    credit_wallet()/debit_wallet() — that is its entire purpose, proven
    separately by simulator.tests.test_o3c3_treasury_execution_service.
    py::WalletLedgerUsageTests. A whole-module scan would now
    false-positive on that unrelated, authorized function.

    AST-based (not a plain substring search): the module's own docstring
    legitimately documents, in prose, that submit_treasury_request()
    does NOT call credit_wallet() / debit_wallet() / etc. — a substring
    search would false-positive on that very documentation. Walking the
    parsed AST for real Import/ImportFrom nodes and real Call nodes
    ignores docstrings entirely.
    """

    def test_submit_treasury_request_never_imports_wallet_ledger_or_funded_payouts(self):
        import ast
        import inspect
        import textwrap
        from simulator.treasury_requests import submit_treasury_request

        tree = ast.parse(textwrap.dedent(inspect.getsource(submit_treasury_request)))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
                imported_modules.update(alias.name for alias in node.names)

        self.assertNotIn("wallet_ledger", imported_modules)
        self.assertNotIn("funded_payouts", imported_modules)

    def test_submit_treasury_request_never_calls_wallet_movement_functions(self):
        import ast
        import inspect
        import textwrap
        from simulator.treasury_requests import submit_treasury_request

        tree = ast.parse(textwrap.dedent(inspect.getsource(submit_treasury_request)))
        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called_names.add(name)

        forbidden = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
        }
        self.assertEqual(called_names & forbidden, set())
