# simulator/tests/test_owner_broker_economic_adjustment.py
"""
BROKER-ECONOMICS-02C — owner_broker_economic_adjustment() (owner_actions.py).

Structurally the ONLY sanctioned way a user-facing surface may originate
a BrokerEconomicAdjustment / BrokerLedger.REV_ADJUSTMENT correction. Calls
simulator.broker_economic_adjustment.create_broker_economic_adjustment()
(BROKER-ECONOMICS-02B) exclusively — never writes either model itself.

Covers: Owner-only gate (never is_superuser/staff/TREASURY_REVIEW_
PERMISSION); TOTP required; reason required; idempotency (same key = no
double effect, exactly one AuditLog/BrokerAuditEvent, on genuine
creation only — never on an idempotent-retry return); reversal amount
ALWAYS server-derived, a caller-supplied amount for a reversal is never
even read; invalid/nonexistent references rejected with zero writes;
full economic isolation from Wallet/Treasury/IB/TradingAccount.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from simulator.audit import EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT
from simulator.broker_pnl import calculate_broker_pnl
from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerEconomicAdjustment, BrokerLedger,
    IBCommissionObligation, OpsAdminProfile, OwnerRoot, TreasuryOperationRequest,
    WalletTransaction,
)
from simulator.owner_actions import InvalidAdjustment, owner_broker_economic_adjustment
from simulator.tests.factories import make_account, make_broker_ledger, make_user
from simulator.wallet_ledger import get_or_create_wallet

_PATCH_TOTP = patch("simulator.owner_actions.verify_totp", return_value=True)
_PATCH_TOTP_FAIL = patch("simulator.owner_actions.verify_totp", return_value=False)


def _make_owner():
    owner_user = make_user(is_superuser=True, is_staff=True)
    OwnerRoot.objects.create(user=owner_user, established_by="test")
    return owner_user


def _key():
    return uuid.uuid4().hex


class AuthorizationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()

    @_PATCH_TOTP
    def test_non_owner_denied(self, _mock):
        plain_user = make_user()
        with self.assertRaises(PermissionDenied):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=plain_user,
                totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 0)

    @_PATCH_TOTP
    def test_ops_admin_denied(self, _mock):
        """OpsAdminProfile is NOT Owner Root — must be denied identically
        to a plain user. Confirms the gate is never a weaker permission."""
        ops = make_user()
        OpsAdminProfile.objects.create(user=ops, assigned_by=self.owner)
        with self.assertRaises(PermissionDenied):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=ops,
                totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_superuser_without_owner_root_denied(self):
        """is_superuser alone must never substitute for is_owner_root."""
        superuser_only = make_user(is_superuser=True, is_staff=True)
        with self.assertRaises(PermissionDenied):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=superuser_only,
                totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP_FAIL
    def test_wrong_totp_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.owner,
                totp_code="999999", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP_FAIL
    def test_owner_without_confirmed_totp_device_denied(self, _mock):
        """verify_totp() itself returns False when there's no confirmed
        device — same code path as a wrong code, exercised explicitly
        per the Owner's required scenario list."""
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.owner,
                totp_code="", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)


class InputValidationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()

    @_PATCH_TOTP
    def test_blank_reason_denied_before_totp_matters(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="   ", actor=self.owner,
                totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_zero_amount_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("0.00"), reason="x", actor=self.owner,
                totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_missing_amount_for_new_adjustment_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                reason="x", actor=self.owner, totp_code="000000", idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_missing_idempotency_key_denied(self):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.owner,
                totp_code="000000", idempotency_key="",
            )

    @_PATCH_TOTP
    def test_nonexistent_source_ledger_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.owner,
                totp_code="000000", idempotency_key=_key(), source_ledger_id=999999,
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_nonexistent_source_account_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.owner,
                totp_code="000000", idempotency_key=_key(), source_account_id=999999,
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_nonexistent_reverses_target_denied(self, _mock):
        with self.assertRaises(InvalidAdjustment):
            owner_broker_economic_adjustment(
                reason="x", actor=self.owner, totp_code="000000",
                idempotency_key=_key(), reverses_id=999999,
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)


class SuccessfulCreationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()

    @_PATCH_TOTP
    def test_valid_new_adjustment_creates_exactly_one_of_each(self, _mock):
        original = make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("100.00"))
        adjustment = owner_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="correction", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_ledger_id=original.pk,
        )
        self.assertEqual(adjustment.amount, Decimal("-20.00"))
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)
        self.assertEqual(adjustment.actor_id, self.owner.pk)

    @_PATCH_TOTP
    def test_exactly_one_auditlog_and_brokerauditevent(self, _mock):
        before_al = AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count()
        before_be = BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count()
        owner_broker_economic_adjustment(
            amount=Decimal("15.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), before_al + 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), before_be + 1,
        )

    @_PATCH_TOTP
    def test_audit_detail_contains_actor_reference_amount(self, _mock):
        adjustment = owner_broker_economic_adjustment(
            amount=Decimal("15.00"), reason="traceable reason", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        entry = AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).latest("id")
        self.assertEqual(entry.detail["actor_id"], self.owner.pk)
        self.assertEqual(entry.detail["reference"], adjustment.reference)
        self.assertEqual(entry.detail["amount"], "15.00")
        self.assertEqual(entry.detail["reason"], "traceable reason")


class IdempotencyTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()

    @_PATCH_TOTP
    def test_same_idempotency_key_no_double_effect(self, _mock):
        key = _key()
        adj1 = owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="first", actor=self.owner,
            totp_code="000000", idempotency_key=key,
        )
        adj2 = owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="retry", actor=self.owner,
            totp_code="000000", idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)

    @_PATCH_TOTP
    def test_retry_does_not_write_a_second_audit_pair(self, _mock):
        key = _key()
        owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="first", actor=self.owner,
            totp_code="000000", idempotency_key=key,
        )
        owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="retry", actor=self.owner,
            totp_code="000000", idempotency_key=key,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), 1,
        )

    @_PATCH_TOTP
    def test_different_key_creates_independent_operation(self, _mock):
        owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="first", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        owner_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="second, different key", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 2)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 2)


class ReversalServerDerivationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()

    @_PATCH_TOTP
    def test_reversal_amount_always_server_derived(self, _mock):
        original = owner_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        reversal = owner_broker_economic_adjustment(
            reason="reverse it", actor=self.owner, totp_code="000000",
            idempotency_key=_key(), reverses_id=original.pk,
        )
        self.assertEqual(reversal.amount, Decimal("20.00"))
        self.assertEqual(reversal.reverses_id, original.pk)

    @_PATCH_TOTP
    def test_client_supplied_reversal_amount_is_ignored_entirely(self, _mock):
        """The single most important adversarial test in this file: pass
        a deliberately WRONG amount alongside reverses_id and prove the
        wrapper silently discards it and derives the correct value
        itself — the browser's amount for a reversal is NEVER read."""
        original = owner_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(),
        )
        reversal = owner_broker_economic_adjustment(
            amount=Decimal("999999.00"),  # attacker/bug-supplied, must be ignored
            reason="reverse it", actor=self.owner, totp_code="000000",
            idempotency_key=_key(), reverses_id=original.pk,
        )
        self.assertEqual(reversal.amount, Decimal("20.00"))
        self.assertNotEqual(reversal.amount, Decimal("999999.00"))

    @_PATCH_TOTP
    def test_reversal_nets_to_zero_through_calculate_broker_pnl(self, _mock):
        account = make_account()
        original = owner_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_account_id=account.id,
        )
        before_reversal = calculate_broker_pnl(account_id=account.id)
        owner_broker_economic_adjustment(
            reason="reverse it", actor=self.owner, totp_code="000000",
            idempotency_key=_key(), reverses_id=original.pk, source_account_id=account.id,
        )
        after_reversal = calculate_broker_pnl(account_id=account.id)
        self.assertEqual(after_reversal.adjustments, before_reversal.adjustments + Decimal("20.00"))


class EconomicIsolationTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.trader = make_user()
        self.account = make_account(user=self.trader, balance=Decimal("10000"))
        self.wallet, _ = get_or_create_wallet(self.trader)

    @_PATCH_TOTP
    def test_no_trading_account_change(self, _mock):
        balance_before, equity_before = self.account.balance, self.account.equity
        owner_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_account_id=self.account.id,
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before)
        self.assertEqual(self.account.equity, equity_before)

    @_PATCH_TOTP
    def test_no_wallet_change(self, _mock):
        balance_before = self.wallet.available_balance
        owner_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_account_id=self.account.id,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), 0)

    @_PATCH_TOTP
    def test_no_treasury_operation_request(self, _mock):
        before = TreasuryOperationRequest.objects.count()
        owner_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_account_id=self.account.id,
        )
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)

    @_PATCH_TOTP
    def test_no_ib_commission_obligation_generated(self, _mock):
        owner_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.owner,
            totp_code="000000", idempotency_key=_key(), source_account_id=self.account.id,
        )
        self.assertEqual(IBCommissionObligation.objects.count(), 0)
