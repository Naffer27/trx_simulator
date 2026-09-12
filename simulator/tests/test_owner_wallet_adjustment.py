# simulator/tests/test_owner_wallet_adjustment.py
"""
MONEY-INTEGRITY-FIX-02 — owner_wallet_adjustment().

Structurally SEPARATE from TreasuryOperationRequest (never touched here).
Reuses credit_wallet()/debit_wallet() unmodified with TX_CORRECTION.

Covers: Owner credit/debit; OPS denied; non-Owner denied; wrong TOTP
denied; duplicate idempotency_key is a no-op for both credit and debit
(no double WalletTransaction, no double OwnerWalletAdjustment); exactly
one WalletTransaction/OwnerWalletAdjustment per real call; before/after
exact; AuditLog + BrokerAuditEvent written; insufficient-funds debit
denied with zero partial writes.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from simulator.models import AuditLog, OpsAdminProfile, OwnerRoot, OwnerWalletAdjustment, WalletTransaction
from simulator.owner_actions import InvalidAdjustment, owner_wallet_adjustment
from .factories import make_user, make_wallet

_PATCH_TOTP = patch("simulator.owner_actions.verify_totp", return_value=True)
_PATCH_TOTP_FAIL = patch("simulator.owner_actions.verify_totp", return_value=False)


def _make_owner():
    owner_user = make_user(is_superuser=True, is_staff=True)
    OwnerRoot.objects.create(user=owner_user, established_by="test")
    return owner_user


def _key():
    return uuid.uuid4().hex


class OwnerWalletAdjustmentAuthorizationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.wallet = make_wallet(initial_balance=Decimal("200.00"))

    def test_non_owner_denied(self):
        plain_user = make_user()
        with self.assertRaises(PermissionDenied):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("50"), actor=plain_user, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))

    def test_ops_denied(self):
        ernesto = make_user()
        OpsAdminProfile.objects.create(user=ernesto, assigned_by=self.owner)
        with self.assertRaises(PermissionDenied):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("50"), actor=ernesto, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )

    @_PATCH_TOTP_FAIL
    def test_wrong_totp_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))

    @_PATCH_TOTP
    def test_zero_amount_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("0"), actor=self.owner, reason="test",
                totp_code="123456", idempotency_key=_key(),
            )

    @_PATCH_TOTP
    def test_reason_required(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("50"), actor=self.owner, reason="   ",
                totp_code="123456", idempotency_key=_key(),
            )


class OwnerWalletAdjustmentEffectTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.wallet = make_wallet(initial_balance=Decimal("200.00"))

    @_PATCH_TOTP
    def test_owner_can_credit(self, _mock):
        owner_wallet_adjustment(
            self.wallet.id, Decimal("100"), actor=self.owner, reason="goodwill credit",
            totp_code="123456", idempotency_key=_key(),
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("300.00"))

    @_PATCH_TOTP
    def test_owner_can_debit(self, _mock):
        owner_wallet_adjustment(
            self.wallet.id, Decimal("-50"), actor=self.owner, reason="fee correction",
            totp_code="123456", idempotency_key=_key(),
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("150.00"))

    @_PATCH_TOTP
    def test_insufficient_funds_debit_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("-9999"), actor=self.owner, reason="test",
                totp_code="123456", idempotency_key=_key(),
            )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))
        self.assertEqual(OwnerWalletAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_before_after_exact(self, _mock):
        adj = owner_wallet_adjustment(
            self.wallet.id, Decimal("75"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(adj.balance_before, Decimal("200.00"))
        self.assertEqual(adj.balance_after, Decimal("275.00"))

    @_PATCH_TOTP
    def test_duplicate_idempotency_no_double_credit(self, _mock):
        key = _key()
        adj1 = owner_wallet_adjustment(
            self.wallet.id, Decimal("100"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=key,
        )
        adj2 = owner_wallet_adjustment(
            self.wallet.id, Decimal("100"), actor=self.owner, reason="retry",
            totp_code="123456", idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("300.00"))  # only ONE +100
        self.assertEqual(OwnerWalletAdjustment.objects.count(), 1)

    @_PATCH_TOTP
    def test_duplicate_idempotency_no_double_debit(self, _mock):
        key = _key()
        adj1 = owner_wallet_adjustment(
            self.wallet.id, Decimal("-50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=key,
        )
        adj2 = owner_wallet_adjustment(
            self.wallet.id, Decimal("-50"), actor=self.owner, reason="retry",
            totp_code="123456", idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("150.00"))  # only ONE -50
        self.assertEqual(OwnerWalletAdjustment.objects.count(), 1)

    @_PATCH_TOTP
    def test_exactly_one_wallettransaction(self, _mock):
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        owner_wallet_adjustment(
            self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(after - before, 1)

    @_PATCH_TOTP
    def test_exactly_one_ownerwalletadjustment(self, _mock):
        owner_wallet_adjustment(
            self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(OwnerWalletAdjustment.objects.count(), 1)

    @_PATCH_TOTP
    def test_wallettransaction_type_correction(self, _mock):
        adj = owner_wallet_adjustment(
            self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(adj.wallet_transaction.tx_type, WalletTransaction.TX_CORRECTION)

    @_PATCH_TOTP
    def test_auditlog_written(self, _mock):
        owner_wallet_adjustment(
            self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="owner.wallet_adjustment").count(), 1,
        )

    @_PATCH_TOTP
    def test_brokerauditevent_written(self, _mock):
        from simulator.models import BrokerAuditEvent
        owner_wallet_adjustment(
            self.wallet.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="owner.wallet_adjustment").count(), 1,
        )

    @_PATCH_TOTP
    def test_unique_reference(self, _mock):
        adj1 = owner_wallet_adjustment(
            self.wallet.id, Decimal("10"), actor=self.owner, reason="a",
            totp_code="123456", idempotency_key=_key(),
        )
        adj2 = owner_wallet_adjustment(
            self.wallet.id, Decimal("10"), actor=self.owner, reason="b",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertNotEqual(adj1.reference, adj2.reference)
