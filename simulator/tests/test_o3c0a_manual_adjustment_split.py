# simulator/tests/test_o3c0a_manual_adjustment_split.py
"""
Bloque O.3c-0a — Manual Adjustment Type Split.

Covers ONLY the schema-level facts of the frozen O.3c-0 architecture
decision: OP_MANUAL_ADJUSTMENT was retired and replaced by
OP_MANUAL_CREDIT / OP_MANUAL_DEBIT — no `direction` field was added,
no inference from `category` was implemented. The type itself carries
direction, mirroring WalletTransaction's own convention (tx_type +
signed amount, never a separate direction flag).

No execution engine exists yet — nothing here calls credit_wallet(),
debit_wallet(), or any wallet_ledger.py function. No WalletTransaction
is ever created and no Wallet balance ever changes.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from simulator.models import TreasuryOperationRequest, Wallet, WalletTransaction

from .factories import make_wallet


class OperationTypeChoicesTests(TestCase):

    def test_manual_adjustment_no_longer_exists(self):
        values = {c[0] for c in TreasuryOperationRequest.OPERATION_TYPE_CHOICES}
        self.assertNotIn("MANUAL_ADJUSTMENT", values)
        self.assertFalse(hasattr(TreasuryOperationRequest, "OP_MANUAL_ADJUSTMENT"))

    def test_manual_credit_exists(self):
        values = {c[0] for c in TreasuryOperationRequest.OPERATION_TYPE_CHOICES}
        self.assertIn("MANUAL_CREDIT", values)
        self.assertEqual(TreasuryOperationRequest.OP_MANUAL_CREDIT, "MANUAL_CREDIT")

    def test_manual_debit_exists(self):
        values = {c[0] for c in TreasuryOperationRequest.OPERATION_TYPE_CHOICES}
        self.assertIn("MANUAL_DEBIT", values)
        self.assertEqual(TreasuryOperationRequest.OP_MANUAL_DEBIT, "MANUAL_DEBIT")

    def test_operation_type_choices_has_exactly_seven_entries(self):
        self.assertEqual(len(TreasuryOperationRequest.OPERATION_TYPE_CHOICES), 7)

    def test_operation_type_field_max_length_unchanged(self):
        field = TreasuryOperationRequest._meta.get_field("operation_type")
        self.assertEqual(field.max_length, 20)

    def test_both_new_values_fit_within_max_length(self):
        field = TreasuryOperationRequest._meta.get_field("operation_type")
        self.assertLessEqual(len(TreasuryOperationRequest.OP_MANUAL_CREDIT), field.max_length)
        self.assertLessEqual(len(TreasuryOperationRequest.OP_MANUAL_DEBIT), field.max_length)


class NoDirectionFieldTests(TestCase):
    """Frozen decision: no `direction` field was added to the model."""

    def test_model_has_no_direction_field(self):
        field_names = {f.name for f in TreasuryOperationRequest._meta.fields}
        self.assertNotIn("direction", field_names)

    def test_instance_has_no_direction_attribute(self):
        wallet = make_wallet()
        instance = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_CREDIT,
            wallet=wallet, amount=Decimal("10.00"),
        )
        self.assertFalse(hasattr(instance, "direction"))


class AmountStillPositiveTests(TestCase):
    """amount stays positive for both new types — CheckConstraint unchanged."""

    def test_manual_credit_amount_zero_violates_constraint(self):
        wallet = make_wallet()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreasuryOperationRequest.objects.create(
                    operation_type=TreasuryOperationRequest.OP_MANUAL_CREDIT,
                    wallet=wallet, amount=Decimal("0.00"),
                )

    def test_manual_debit_amount_negative_violates_constraint(self):
        wallet = make_wallet()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreasuryOperationRequest.objects.create(
                    operation_type=TreasuryOperationRequest.OP_MANUAL_DEBIT,
                    wallet=wallet, amount=Decimal("-5.00"),
                )

    def test_manual_credit_positive_amount_accepted(self):
        wallet = make_wallet()
        instance = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_CREDIT,
            wallet=wallet, amount=Decimal("25.00"),
        )
        self.assertEqual(instance.amount, Decimal("25.00"))

    def test_manual_debit_positive_amount_accepted(self):
        # amount is always positive regardless of direction — the
        # execution engine (not built yet) is what will interpret this
        # positive amount as a debit for OP_MANUAL_DEBIT.
        wallet = make_wallet()
        instance = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_DEBIT,
            wallet=wallet, amount=Decimal("25.00"),
        )
        self.assertEqual(instance.amount, Decimal("25.00"))


class NoFinancialLogicImplementedTests(TestCase):
    """
    This microblock only touches schema/choices/form validation — no
    execution engine exists yet. Confirms creating either new type
    never touches Wallet or creates a WalletTransaction.
    """

    def test_creating_manual_credit_request_does_not_move_money(self):
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        balance_before = wallet.available_balance
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_CREDIT,
            wallet=wallet, amount=Decimal("15.00"),
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before,
        )

    def test_creating_manual_debit_request_does_not_move_money(self):
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        balance_before = wallet.available_balance
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_DEBIT,
            wallet=wallet, amount=Decimal("15.00"),
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before,
        )

    def test_treasury_requests_module_unaffected(self):
        # treasury_requests.py was explicitly not modified in this
        # microblock — sanity check that it still imports cleanly and
        # its public surface is unchanged.
        from simulator.treasury_requests import (
            approve_treasury_request,
            reject_treasury_request,
            submit_treasury_request,
        )
        self.assertTrue(callable(submit_treasury_request))
        self.assertTrue(callable(approve_treasury_request))
        self.assertTrue(callable(reject_treasury_request))
