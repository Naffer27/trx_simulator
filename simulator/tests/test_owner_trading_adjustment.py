# simulator/tests/test_owner_trading_adjustment.py
"""
MONEY-INTEGRITY-FIX-02 — owner_trading_account_adjustment().

Covers: Owner can credit/debit a TradingAccount; non-Owner (including
OPS) is denied; wrong TOTP denied; zero amount denied; reason required;
duplicate idempotency_key is a no-op (no second write of any kind); open
positions deny with zero partial writes; before/after and equity are
exact; AuditLog + BrokerAuditEvent + unique reference are written.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from simulator.models import (
    AuditLog, LedgerEntry, ManualBalanceAdjustment, OpsAdminProfile, OwnerRoot,
)
from simulator.owner_actions import (
    InvalidAdjustment, OpenPositionsExist, owner_trading_account_adjustment,
)
from .factories import make_account, make_position, make_user

_PATCH_TOTP = patch("simulator.owner_actions.verify_totp", return_value=True)
_PATCH_TOTP_FAIL = patch("simulator.owner_actions.verify_totp", return_value=False)


def _make_owner():
    owner_user = make_user(is_superuser=True, is_staff=True)
    OwnerRoot.objects.create(user=owner_user, established_by="test")
    return owner_user


def _key():
    return uuid.uuid4().hex


class OwnerTradingAdjustmentAuthorizationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.account = make_account(account_type="RETAIL", balance=Decimal("500.00"))

    def test_non_owner_denied(self):
        plain_user = make_user()
        with self.assertRaises(PermissionDenied):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=plain_user, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("500.00"))

    def test_ops_denied(self):
        ernesto = make_user()
        OpsAdminProfile.objects.create(user=ernesto, assigned_by=self.owner)
        with self.assertRaises(PermissionDenied):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=ernesto, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )

    @_PATCH_TOTP_FAIL
    def test_wrong_totp_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=self.owner, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("500.00"))

    @_PATCH_TOTP
    def test_zero_amount_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_trading_account_adjustment(
                self.account.id, Decimal("0"), actor=self.owner, reason="test",
                totp_code="000000", idempotency_key=_key(),
            )

    @_PATCH_TOTP
    def test_reason_required(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=self.owner, reason="   ",
                totp_code="000000", idempotency_key=_key(),
            )


class OwnerTradingAdjustmentEffectTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.account = make_account(account_type="RETAIL", balance=Decimal("500.00"))

    @_PATCH_TOTP
    def test_owner_can_credit(self, _mock):
        owner_trading_account_adjustment(
            self.account.id, Decimal("100"), actor=self.owner, reason="bonus correction",
            totp_code="123456", idempotency_key=_key(),
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("600.00"))
        self.assertEqual(self.account.equity, Decimal("600.00"))

    @_PATCH_TOTP
    def test_owner_can_debit(self, _mock):
        owner_trading_account_adjustment(
            self.account.id, Decimal("-100"), actor=self.owner, reason="fee correction",
            totp_code="123456", idempotency_key=_key(),
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("400.00"))
        self.assertEqual(self.account.equity, Decimal("400.00"))

    @_PATCH_TOTP
    def test_before_after_exact(self, _mock):
        adj = owner_trading_account_adjustment(
            self.account.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(adj.balance_before, Decimal("500.00"))
        self.assertEqual(adj.balance_after, Decimal("550.00"))
        self.assertEqual(adj.equity_before, Decimal("500.00"))
        self.assertEqual(adj.equity_after, Decimal("550.00"))

    @_PATCH_TOTP
    def test_open_positions_denied(self, _mock):
        make_position(self.account)
        before_counts = (
            ManualBalanceAdjustment.objects.count(),
            LedgerEntry.objects.filter(account=self.account).count(),
        )
        with self.assertRaises(OpenPositionsExist):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=self.owner, reason="test",
                totp_code="123456", idempotency_key=_key(),
            )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("500.00"))
        self.assertEqual(
            (ManualBalanceAdjustment.objects.count(), LedgerEntry.objects.filter(account=self.account).count()),
            before_counts,
        )

    @_PATCH_TOTP
    def test_negative_resulting_balance_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_trading_account_adjustment(
                self.account.id, Decimal("-9999"), actor=self.owner, reason="test",
                totp_code="123456", idempotency_key=_key(),
            )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("500.00"))

    @_PATCH_TOTP
    def test_duplicate_idempotency_no_op(self, _mock):
        key = _key()
        adj1 = owner_trading_account_adjustment(
            self.account.id, Decimal("100"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=key,
        )
        adj2 = owner_trading_account_adjustment(
            self.account.id, Decimal("100"), actor=self.owner, reason="test (retry)",
            totp_code="123456", idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("600.00"))  # only ONE +100 applied
        self.assertEqual(ManualBalanceAdjustment.objects.count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_OWNER_CORRECTION).count(),
            1,
        )

    @_PATCH_TOTP
    def test_no_duplicate_ledger_entry(self, _mock):
        owner_trading_account_adjustment(
            self.account.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_OWNER_CORRECTION).count(),
            1,
        )

    @_PATCH_TOTP
    def test_unique_reference(self, _mock):
        adj1 = owner_trading_account_adjustment(
            self.account.id, Decimal("10"), actor=self.owner, reason="a",
            totp_code="123456", idempotency_key=_key(),
        )
        adj2 = owner_trading_account_adjustment(
            self.account.id, Decimal("10"), actor=self.owner, reason="b",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertNotEqual(adj1.reference, adj2.reference)

    @_PATCH_TOTP
    def test_auditlog_written(self, _mock):
        owner_trading_account_adjustment(
            self.account.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="owner.trading_adjustment").count(), 1,
        )

    @_PATCH_TOTP
    def test_brokerauditevent_written(self, _mock):
        from simulator.models import BrokerAuditEvent
        owner_trading_account_adjustment(
            self.account.id, Decimal("50"), actor=self.owner, reason="test",
            totp_code="123456", idempotency_key=_key(),
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="owner.trading_adjustment").count(), 1,
        )
