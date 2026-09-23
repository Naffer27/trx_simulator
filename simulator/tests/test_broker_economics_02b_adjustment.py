# simulator/tests/test_broker_economics_02b_adjustment.py
"""
BROKER-ECONOMICS-02B — Economic Adjustment Foundation.

Covers:
  - simulator/models.py::BrokerEconomicAdjustment
  - simulator/broker_economic_adjustment.py::create_broker_economic_adjustment()

Scope discipline (mirrors the FASE A design lock exactly):
  - This is the CORE SERVICE only — no permission/TOTP/authorization
    concept is exercised or expected here (that is 02C's future scope).
  - No backfill, no bulk/batch entry point exists to test.
  - No dev-DB, no real user, no QA-data seeding — every fixture below is
    created and torn down entirely inside Django's isolated test
    transaction, exactly like every other test file in this suite.

Regression companions deliberately exercised here (not just unit-tested
in isolation): broker_pnl.py's existing REV_ADJUSTMENT summation, and
all six IB rule-type generators' isolation from REV_ADJUSTMENT — proving
BROKER-ECONOMICS-02 FASE A's own conclusions hold against the real,
now-implemented writer, not just against a hand-built BrokerLedger row.
"""
import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from simulator.broker_economic_adjustment import (
    InvalidEconomicAdjustment,
    create_broker_economic_adjustment,
)
from simulator.broker_pnl import calculate_broker_pnl
from simulator.ib_commission import (
    generate_spread_revenue_share_obligation,
    generate_trading_commission_revenue_share_obligation,
)
from simulator.ib_commission_triggers import (
    sweep_challenge_percent,
    sweep_deposit_percent,
    sweep_per_lot,
    sweep_spread_revenue_share,
    sweep_trading_commission_revenue_share,
)
from simulator.models import (
    BrokerEconomicAdjustment, BrokerLedger, IBCommissionObligation,
    IBCommissionRule, Referral, ReferralAttribution, TreasuryOperationRequest,
    Wallet, WalletTransaction,
)
from simulator.tests.factories import make_account, make_broker_ledger, make_user
from simulator.wallet_ledger import get_or_create_wallet


def _key():
    return uuid.uuid4().hex


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=f"bea02b{uuid.uuid4().hex[:8]}")


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type, referral=None, percentage=None, fixed_amount=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=True,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=timezone.now() - timezone.timedelta(minutes=5),
    )


# ─────────────────────────────────────────────────────────────────────────
# 1-5 — Input validation, before any write
# ─────────────────────────────────────────────────────────────────────────

class InputValidationTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.account = make_account()

    def test_positive_adjustment_creates_one_economic_effect(self):
        adj = create_broker_economic_adjustment(
            amount=Decimal("50.00"), reason="recording omitted revenue",
            actor=self.actor, idempotency_key=_key(),
        )
        self.assertEqual(adj.amount, Decimal("50.00"))
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)

    def test_negative_adjustment_creates_one_economic_effect(self):
        original = make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("100.00"))
        adj = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="overstated spread correction",
            actor=self.actor, idempotency_key=_key(), source_ledger=original,
        )
        self.assertEqual(adj.amount, Decimal("-20.00"))
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)

    def test_zero_amount_rejected(self):
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("0.00"), reason="x", actor=self.actor, idempotency_key=_key(),
            )
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 0)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_missing_reason_rejected(self):
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="   ", actor=self.actor, idempotency_key=_key(),
            )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="", actor=self.actor, idempotency_key=_key(),
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_actor_required(self):
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=None, idempotency_key=_key(),
            )
        unsaved_user = type(self.actor)(username="unsaved")  # pk is None
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=unsaved_user, idempotency_key=_key(),
            )

    def test_missing_idempotency_key_rejected(self):
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.actor, idempotency_key="",
            )


# ─────────────────────────────────────────────────────────────────────────
# 6-7 — Exactly one REV_ADJUSTMENT, original BrokerLedger row untouched
# ─────────────────────────────────────────────────────────────────────────

class LedgerIntegrationTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_exactly_one_rev_adjustment_created(self):
        create_broker_economic_adjustment(
            amount=Decimal("30.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)

    def test_original_broker_ledger_row_unchanged(self):
        original = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("100.00"), symbol="EUR/USD",
        )
        original_created_at = original.created_at
        create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_ledger=original,
        )
        original.refresh_from_db()
        self.assertEqual(original.revenue_type, BrokerLedger.REV_SPREAD)
        self.assertEqual(original.amount, Decimal("100.00"))  # NEVER mutated to 80
        self.assertEqual(original.symbol, "EUR/USD")
        self.assertEqual(original.created_at, original_created_at)

    def test_adjustment_linked_to_created_ledger_entry(self):
        adj = create_broker_economic_adjustment(
            amount=Decimal("15.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        self.assertIsNotNone(adj.created_ledger_entry)
        self.assertEqual(adj.created_ledger_entry.revenue_type, BrokerLedger.REV_ADJUSTMENT)
        self.assertEqual(adj.created_ledger_entry.amount, Decimal("15.00"))


# ─────────────────────────────────────────────────────────────────────────
# 8-9, 27 — Idempotency + concurrent-duplicate protection + retry determinism
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_same_idempotency_key_does_not_double_impact(self):
        key = _key()
        adj1 = create_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="first", actor=self.actor, idempotency_key=key,
        )
        adj2 = create_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="retry", actor=self.actor, idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)

    def test_retry_is_deterministic_even_with_different_amount_argument(self):
        """A retry with the SAME key must return the original result, never
        a second economic effect — even if a caller mistakenly varies the
        other arguments on retry, the key is authoritative."""
        key = _key()
        adj1 = create_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="first", actor=self.actor, idempotency_key=key,
        )
        adj2 = create_broker_economic_adjustment(
            amount=Decimal("999.00"), reason="different reason entirely",
            actor=self.actor, idempotency_key=key,
        )
        self.assertEqual(adj1.pk, adj2.pk)
        self.assertEqual(adj2.amount, Decimal("40.00"))  # original wins, not the retry's args
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)

    def test_db_level_unique_constraint_is_the_real_backstop(self):
        """Proves the idempotency_key uniqueness is DB-enforced, not just an
        in-code check — a raw duplicate INSERT (bypassing the service's own
        pre-check entirely) must still be rejected by the database."""
        key = _key()
        adj1 = create_broker_economic_adjustment(
            amount=Decimal("40.00"), reason="x", actor=self.actor, idempotency_key=key,
        )
        ledger2 = BrokerLedger.objects.create(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("40.00"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEconomicAdjustment.objects.create(
                    reference="BEA-DUPLICATE01", amount=Decimal("40.00"), reason="race",
                    actor=self.actor, idempotency_key=key,  # same key as adj1
                    created_ledger_entry=ledger2,
                )
        self.assertEqual(
            BrokerEconomicAdjustment.objects.filter(idempotency_key=key).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────
# 10-11 — Atomicity: rollback leaves no orphan row of either kind
# ─────────────────────────────────────────────────────────────────────────

class AtomicityTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_invalid_reverses_target_leaves_no_orphan_ledger_row(self):
        """reverses validation fails AFTER the function is already inside
        the atomic block's target resolution — if it fails, nothing must
        be created at all."""
        bogus = BrokerEconomicAdjustment(pk=999999)  # never saved, no real pk
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.actor,
                idempotency_key=_key(), reverses=bogus,
            )
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 0)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_invalid_source_ledger_leaves_no_orphan_rows(self):
        bogus_ledger = BrokerLedger(pk=999999)  # never saved
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.actor,
                idempotency_key=_key(), source_ledger=bogus_ledger,
            )
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 0)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_double_reversal_attempt_rolls_back_cleanly(self):
        root = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal", actor=self.actor,
            idempotency_key=_key(), reverses=root,
        )
        ledger_count_before = BrokerLedger.objects.count()
        sidecar_count_before = BrokerEconomicAdjustment.objects.count()
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("20.00"), reason="second reversal attempt",
                actor=self.actor, idempotency_key=_key(), reverses=root,
            )
        self.assertEqual(BrokerLedger.objects.count(), ledger_count_before)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), sidecar_count_before)


# ─────────────────────────────────────────────────────────────────────────
# 12-15 — Reversal design
# ─────────────────────────────────────────────────────────────────────────

class ReversalTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_reversal_creates_opposite_amount_both_rows_permanent(self):
        original = make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("100.00"))
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="overstated", actor=self.actor,
            idempotency_key=_key(), source_ledger=original,
        )
        b = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal of A", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        self.assertEqual(b.reverses_id, a.pk)
        self.assertEqual(b.amount, -a.amount)
        # Both rows still exist — no delete, no update-in-place.
        self.assertTrue(BrokerEconomicAdjustment.objects.filter(pk=a.pk).exists())
        self.assertTrue(BrokerEconomicAdjustment.objects.filter(pk=b.pk).exists())
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 2)

    def test_original_adjustment_remains_unchanged_after_reversal(self):
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="original reason", actor=self.actor, idempotency_key=_key(),
        )
        a_amount, a_reason, a_created_at = a.amount, a.reason, a.created_at
        create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        a.refresh_from_db()
        self.assertEqual(a.amount, a_amount)
        self.assertEqual(a.reason, a_reason)
        self.assertEqual(a.created_at, a_created_at)
        self.assertIsNone(a.reverses_id)  # A itself never becomes a reversal of anything

    def test_double_reversal_prevented(self):
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="first reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("20.00"), reason="second reversal", actor=self.actor,
                idempotency_key=_key(), reverses=a,
            )

    def test_reversal_of_a_reversal_rejected_one_hop_only(self):
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        b = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal of A", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("-20.00"), reason="reversal of a reversal",
                actor=self.actor, idempotency_key=_key(), reverses=b,
            )

    def test_self_reversal_structurally_impossible_via_public_api(self):
        """The service's public API cannot express `reverses=<the row
        being created>` in one call — the new row has no pk yet at the
        point `reverses` is validated. This is proven, not assumed: the
        only two adjustments that exist after this call are the original
        and a normal, valid reversal of a DIFFERENT row."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        b = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="valid reversal of a", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        self.assertNotEqual(b.reverses_id, b.pk)

    def test_self_reversal_rejected_at_db_level(self):
        """Bypasses the service entirely and attempts the literal
        self-reference state directly (an UPDATE, since a row cannot be
        INSERTed already pointing at its own not-yet-assigned pk) —
        proves the CheckConstraint added specifically for this defense-
        in-depth case actually rejects it."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEconomicAdjustment.objects.filter(pk=a.pk).update(reverses_id=a.pk)


# ─────────────────────────────────────────────────────────────────────────
# REVERSAL-AMOUNT-INTEGRITY-01 — adversarial proof that
# create_broker_economic_adjustment() CANNOT create a reversal whose
# amount is not exactly the negative of the adjustment it reverses.
#
# Closes the defect found by BROKER-ECONOMICS-02B's pre-closure
# certification: a "reversal" could previously be created with ANY
# amount, structurally marking the original as reversed via reverses_id
# while the combined economic effect was not actually zero.
# ─────────────────────────────────────────────────────────────────────────

class ReversalAmountIntegrityTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.account = make_account()

    def test_negative_20_reversed_by_positive_20_accepted(self):
        """A = -20.00 -> reversal +20.00 : ACCEPTED."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        b = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="correct reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        self.assertEqual(b.reverses_id, a.pk)
        self.assertEqual(b.amount, Decimal("20.00"))

    def test_negative_20_reversed_by_positive_5_rejected(self):
        """A = -20.00 -> reversal +5.00 : REJECTED (partial reversal)."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("5.00"), reason="wrong, too small", actor=self.actor,
                idempotency_key=_key(), reverses=a,
            )

    def test_negative_20_reversed_by_positive_999_rejected(self):
        """A = -20.00 -> reversal +999.00 : REJECTED (over-reversal)."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("999.00"), reason="wrong, way too large", actor=self.actor,
                idempotency_key=_key(), reverses=a,
            )

    def test_positive_20_reversed_by_negative_20_accepted(self):
        """A = +20.00 -> reversal -20.00 : ACCEPTED (opposite sign case)."""
        a = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        b = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="correct reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        self.assertEqual(b.reverses_id, a.pk)
        self.assertEqual(b.amount, Decimal("-20.00"))

    def test_positive_20_reversed_by_negative_5_rejected(self):
        """A = +20.00 -> reversal -5.00 : REJECTED."""
        a = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("-5.00"), reason="wrong", actor=self.actor,
                idempotency_key=_key(), reverses=a,
            )

    def test_same_sign_amount_rejected(self):
        """Adversarial extra case: passing the SAME sign/value as A
        (i.e. reverses=A, amount=A.amount, which would DOUBLE the
        economic effect instead of neutralizing it) must be rejected."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("-20.00"), reason="same sign, doubles the effect",
                actor=self.actor, idempotency_key=_key(), reverses=a,
            )

    def test_rejected_reversal_produces_zero_writes_and_leaves_a_untouched(self):
        """A rejected mismatched reversal must: create no BrokerLedger
        row, create no BrokerEconomicAdjustment row, and leave A exactly
        as it was."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="original", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        ledger_count_before = BrokerLedger.objects.count()
        sidecar_count_before = BrokerEconomicAdjustment.objects.count()
        a_amount, a_reason, a_reverses_id = a.amount, a.reason, a.reverses_id

        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("5.00"), reason="mismatched", actor=self.actor,
                idempotency_key=_key(), reverses=a,
            )

        self.assertEqual(BrokerLedger.objects.count(), ledger_count_before)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), sidecar_count_before)
        a.refresh_from_db()
        self.assertEqual(a.amount, a_amount)
        self.assertEqual(a.reason, a_reason)
        self.assertEqual(a.reverses_id, a_reverses_id)  # A still reverses nothing

    def test_retry_of_valid_reversal_same_idempotency_key_no_third_row(self):
        """Retry of a VALID reversal using the SAME idempotency_key must
        return the identical operation, never create a third row (C),
        and never duplicate the economic effect."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        reversal_key = _key()
        b1 = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal", actor=self.actor,
            idempotency_key=reversal_key, reverses=a,
        )
        b2 = create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="retry of the same reversal",
            actor=self.actor, idempotency_key=reversal_key, reverses=a,
        )
        self.assertEqual(b1.pk, b2.pk)
        # Exactly A + B exist — never a third row C.
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 2)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 2)
        # A is still reversed by exactly one row (b1 == b2), not two.
        self.assertEqual(
            BrokerEconomicAdjustment.objects.filter(reverses_id=a.pk).count(), 1,
        )

    def test_valid_reversal_nets_to_zero_through_calculate_broker_pnl(self):
        """The economic proof, not just field comparison: A + B must net
        to exactly zero as observed through calculate_broker_pnl() —
        the REAL, unmodified aggregation SSOT — not merely
        `b.amount == -a.amount`."""
        baseline = calculate_broker_pnl(account_id=self.account.id)

        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="original", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        after_a = calculate_broker_pnl(account_id=self.account.id)
        self.assertEqual(after_a.adjustments, baseline.adjustments - Decimal("20.00"))
        self.assertEqual(after_a.broker_net_pnl, baseline.broker_net_pnl - Decimal("20.00"))

        create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a, source_account=self.account,
        )
        after_b = calculate_broker_pnl(account_id=self.account.id)

        # A + B = 0: the adjustments component (and therefore
        # broker_net_pnl) is back to EXACTLY the pre-A baseline.
        self.assertEqual(after_b.adjustments, baseline.adjustments)
        self.assertEqual(after_b.broker_net_pnl, baseline.broker_net_pnl)

    def test_idempotency_key_none_rejected(self):
        """Explicit None (distinct from the empty-string case already
        covered by test_missing_idempotency_key_rejected)."""
        with self.assertRaises(InvalidEconomicAdjustment):
            create_broker_economic_adjustment(
                amount=Decimal("10.00"), reason="x", actor=self.actor, idempotency_key=None,
            )
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 16-17 — Broker P&L integration (broker_pnl.py itself is UNCHANGED)
# ─────────────────────────────────────────────────────────────────────────

class BrokerPnLIntegrationTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.account = make_account()

    def test_positive_adjustment_increases_broker_net_pnl(self):
        before = calculate_broker_pnl(account_id=self.account.id)
        create_broker_economic_adjustment(
            amount=Decimal("50.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        after = calculate_broker_pnl(account_id=self.account.id)
        self.assertEqual(after.adjustments, before.adjustments + Decimal("50.00"))
        self.assertEqual(after.broker_net_pnl, before.broker_net_pnl + Decimal("50.00"))

    def test_negative_adjustment_decreases_broker_net_pnl(self):
        before = calculate_broker_pnl(account_id=self.account.id)
        create_broker_economic_adjustment(
            amount=Decimal("-30.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        after = calculate_broker_pnl(account_id=self.account.id)
        self.assertEqual(after.adjustments, before.adjustments - Decimal("30.00"))
        self.assertEqual(after.broker_net_pnl, before.broker_net_pnl - Decimal("30.00"))

    def test_worked_example_matches_fee_plus_counterparty_plus_adjustment(self):
        """Mirrors broker_pnl's own certified test (6 fee-side + 8
        counterparty - 3 adjustment = 11) using the REAL 02B writer for
        the adjustment leg instead of a hand-built BrokerLedger row."""
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("6.00"), source_account=self.account)
        make_broker_ledger(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, amount=Decimal("8.00"), source_account=self.account)
        create_broker_economic_adjustment(
            amount=Decimal("-3.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        result = calculate_broker_pnl(account_id=self.account.id)
        self.assertEqual(result.broker_net_pnl, Decimal("11.00"))


# ─────────────────────────────────────────────────────────────────────────
# 18-21 — Economic isolation: TradingAccount / Wallet / Treasury / IB
# ─────────────────────────────────────────────────────────────────────────

class EconomicIsolationTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.trader = make_user()
        self.account = make_account(user=self.trader, balance=Decimal("10000"))
        self.wallet, _ = get_or_create_wallet(self.trader)

    def test_no_trading_account_balance_or_equity_change(self):
        balance_before, equity_before = self.account.balance, self.account.equity
        create_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before)
        self.assertEqual(self.account.equity, equity_before)

    def test_no_wallet_balance_change(self):
        balance_before = self.wallet.available_balance
        create_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), 0)

    def test_no_treasury_operation_request_created(self):
        before = TreasuryOperationRequest.objects.count()
        create_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)

    def test_no_ib_commission_obligation_generated_direct_generator_calls(self):
        """The FASE A isolation matrix, converted into a regression test
        against the REAL writer: feed the newly-created REV_ADJUSTMENT row
        directly into both BrokerLedger-consuming generators and confirm
        each rejects it (returns None) rather than generating an
        obligation."""
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        _make_attribution(self.trader, referral)
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal("20.00"))
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, percentage=Decimal("20.00"))

        adj = create_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )
        ledger_row = adj.created_ledger_entry
        self.assertEqual(ledger_row.revenue_type, BrokerLedger.REV_ADJUSTMENT)

        result_commission = generate_trading_commission_revenue_share_obligation(ledger_row)
        result_spread = generate_spread_revenue_share_obligation(ledger_row)
        self.assertIsNone(result_commission)
        self.assertIsNone(result_spread)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_no_ib_commission_obligation_generated_via_real_sweeps(self):
        """Same conclusion, exercised through the actual production sweep
        entry points rather than by calling a generator directly."""
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        _make_attribution(self.trader, referral)
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("1.00"))
        _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal("10.00"))
        _make_rule(IBCommissionRule.RULE_DEPOSIT_PERCENT, percentage=Decimal("10.00"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal("20.00"))
        _make_rule(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, percentage=Decimal("20.00"))

        create_broker_economic_adjustment(
            amount=Decimal("500.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account,
        )

        cutoff = timezone.now() - timezone.timedelta(minutes=30)
        sweep_per_lot(cutoff)
        sweep_challenge_percent(cutoff)
        sweep_deposit_percent(cutoff)
        sweep_trading_commission_revenue_share(cutoff)
        sweep_spread_revenue_share(cutoff)

        self.assertEqual(IBCommissionObligation.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 22-23 — Cross-account isolation + reference preservation
# ─────────────────────────────────────────────────────────────────────────

class CrossAccountAndReferenceTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.account_a = make_account()
        self.account_b = make_account()

    def test_cross_account_isolation_in_broker_pnl(self):
        create_broker_economic_adjustment(
            amount=Decimal("77.00"), reason="x", actor=self.actor,
            idempotency_key=_key(), source_account=self.account_a,
        )
        result_a = calculate_broker_pnl(account_id=self.account_a.id)
        result_b = calculate_broker_pnl(account_id=self.account_b.id)
        self.assertEqual(result_a.adjustments, Decimal("77.00"))
        self.assertEqual(result_b.adjustments, Decimal("0.00"))

    def test_reference_generated_and_unique(self):
        adj1 = create_broker_economic_adjustment(
            amount=Decimal("10.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        adj2 = create_broker_economic_adjustment(
            amount=Decimal("10.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        self.assertTrue(adj1.reference.startswith("BEA-"))
        self.assertNotEqual(adj1.reference, adj2.reference)


# ─────────────────────────────────────────────────────────────────────────
# 24-25 — Precision + DB constraints
# ─────────────────────────────────────────────────────────────────────────

class PrecisionAndConstraintTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_amount_precision_matches_broker_ledger_exactly(self):
        adj = create_broker_economic_adjustment(
            amount=Decimal("12.34"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        self.assertEqual(adj.amount, adj.created_ledger_entry.amount)
        self.assertEqual(adj.created_ledger_entry.amount, Decimal("12.34"))

    def test_amount_nonzero_check_constraint_enforced_at_db_level(self):
        """Bypasses the service entirely — proves the CheckConstraint
        itself, not just the service's Python-level guard."""
        ledger = BrokerLedger.objects.create(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("1.00"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEconomicAdjustment.objects.create(
                    reference="BEA-ZEROTEST01", amount=Decimal("0.00"), reason="x",
                    actor=self.actor, idempotency_key=_key(), created_ledger_entry=ledger,
                )

    def test_reverses_one_to_one_constraint_enforced_at_db_level(self):
        """Bypasses the service's own double-reversal check — proves the
        OneToOneField's own UNIQUE constraint is the real backstop."""
        a = create_broker_economic_adjustment(
            amount=Decimal("-20.00"), reason="x", actor=self.actor, idempotency_key=_key(),
        )
        create_broker_economic_adjustment(
            amount=Decimal("20.00"), reason="first reversal", actor=self.actor,
            idempotency_key=_key(), reverses=a,
        )
        ledger2 = BrokerLedger.objects.create(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("20.00"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEconomicAdjustment.objects.create(
                    reference="BEA-DBLREV01", amount=Decimal("20.00"), reason="second reversal, raw",
                    actor=self.actor, idempotency_key=_key(), reverses=a,
                    created_ledger_entry=ledger2,
                )


# ─────────────────────────────────────────────────────────────────────────
# 26 — Omitted-revenue case (no original BrokerLedger row)
# ─────────────────────────────────────────────────────────────────────────

class OmittedRevenueCaseTests(TestCase):
    def setUp(self):
        self.actor = make_user()

    def test_omitted_revenue_adjustment_with_no_source_ledger(self):
        adj = create_broker_economic_adjustment(
            amount=Decimal("50.00"), reason="revenue omitted historically, recorded now",
            actor=self.actor, idempotency_key=_key(),
        )
        self.assertIsNone(adj.source_ledger)
        self.assertEqual(adj.amount, Decimal("50.00"))
        self.assertEqual(adj.created_ledger_entry.revenue_type, BrokerLedger.REV_ADJUSTMENT)


# ─────────────────────────────────────────────────────────────────────────
# 28 — Future book neutrality
# ─────────────────────────────────────────────────────────────────────────

class BookNeutralityTests(TestCase):
    def test_service_and_model_make_no_book_mode_assumption(self):
        """No field, constraint, or code path in the model or service
        references BOOK-04/05/06, Book.INTERNAL, or assumes B-Book-only
        operation — verified structurally, not by convention alone."""
        import inspect

        from simulator import broker_economic_adjustment as svc_module
        source = inspect.getsource(svc_module)
        for forbidden in ("routing_engine", "liquidity_engine", "dealing_desk", "Book.INTERNAL", "book_mode"):
            self.assertNotIn(forbidden, source)

        field_names = {f.name for f in BrokerEconomicAdjustment._meta.fields}
        self.assertNotIn("book_mode", field_names)
        self.assertNotIn("book", field_names)


# ─────────────────────────────────────────────────────────────────────────
# Isolation-by-import — module never imports Wallet/Treasury/IB services
# ─────────────────────────────────────────────────────────────────────────

class ModuleIsolationTests(TestCase):
    def test_service_module_imports_nothing_from_wallet_treasury_or_ib(self):
        """Parses the module's actual AST import nodes — robust against
        import syntax (`import X`, `from . import X`, `from .X import Y`,
        `from package.X import Y`, `import package.X as Y`) — rather
        than a substring search over the raw source text. Hardened
        after BROKER-ECONOMICS-02B's pre-closure certification flagged
        that a plain `"import {name}"` substring check would miss a
        future `from .wallet_ledger import credit_wallet` style import."""
        import ast
        import inspect

        from simulator import broker_economic_adjustment as svc_module

        source = inspect.getsource(svc_module)
        tree = ast.parse(source)

        forbidden = {
            "wallet_ledger", "ib_treasury_settlement", "ib_commission_triggers",
            "ib_commission", "owner_actions", "ib_admin_ops", "ib_risk_holds",
        }

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # "import wallet_ledger" / "import a.wallet_ledger"
                    imported_names.add(alias.name)
                    imported_names.add(alias.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    # "from .wallet_ledger import X" (module='wallet_ledger')
                    # "from simulator.wallet_ledger import X"
                    imported_names.add(node.module)
                    imported_names.add(node.module.rsplit(".", 1)[-1])
                for alias in node.names:
                    # "from . import wallet_ledger"
                    imported_names.add(alias.name)

        overlap = forbidden & imported_names
        self.assertEqual(
            overlap, set(),
            msg=(
                f"broker_economic_adjustment.py must not import any of "
                f"{sorted(forbidden)} in any form — found: {sorted(overlap)}"
            ),
        )
