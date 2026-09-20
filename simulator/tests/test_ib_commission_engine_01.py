# simulator/tests/test_ib_commission_engine_01.py
"""
IB-COMMISSION-ENGINE-01

Regression coverage for simulator/ib_commission.py::resolve_applicable_rule()
and generate_per_lot_obligation(), and the IBCommissionRule/
IBCommissionObligation models themselves.

This block calculates PENDING IB commission liabilities only — it never
moves money (no credit_wallet(), no WalletTransaction, no LedgerEntry, no
TreasuryOperationRequest) and is NOT wired into any live execution path
yet (consumers.py is untouched by this block) — every LotExecutionEvent
used here is created directly, not via a real order-open call.
"""
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from simulator.ib_commission import (
    AmbiguousCommissionRuleError, generate_per_lot_obligation, resolve_applicable_rule,
)
from simulator.models import (
    IBCommissionObligation, IBCommissionRule, LedgerEntry, LotExecutionEvent, Referral,
    ReferralAttribution, TreasuryOperationRequest, WalletTransaction,
)
from simulator.tests.factories import make_account, make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"ibtest{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral, source=ReferralAttribution.SOURCE_SESSION):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral, source=source,
    )


def _make_rule(rule_type=IBCommissionRule.RULE_PER_LOT, referral=None, enabled=True,
               fixed_amount=Decimal("10.00"), percentage=None,
               effective_from=None, effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or timezone.now(),
        effective_until=effective_until,
    )


def _make_lot_event(account, qty="0.50", execution_price="1.10000",
                     symbol="EUR/USD", side="BUY", merged=False,
                     entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
                     created_at=None):
    ev = LotExecutionEvent.objects.create(
        account=account, position=None, symbol=symbol, side=side,
        qty=Decimal(qty), execution_price=Decimal(execution_price),
        merged=merged, entry_path=entry_path,
    )
    if created_at is not None:
        LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
        ev.refresh_from_db()
    return ev


def _referred_setup(fixed_amount="10.00", qty="0.50"):
    """A fully wired referred trader: IB owner -> Referral -> attributed
    user -> TradingAccount -> LotExecutionEvent, plus a global PER_LOT
    rule at the given rate. Returns (referral, event).

    The rule is created FIRST, with effective_from safely in the past,
    so the event (created after, with auto_now_add's "now") always
    falls within the rule's active window — resolve_applicable_rule()
    requires effective_from <= event.created_at."""
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    account = make_account(user=trader, balance=Decimal("10000"))
    _make_rule(
        fixed_amount=Decimal(fixed_amount),
        effective_from=timezone.now() - timezone.timedelta(minutes=5),
    )
    event = _make_lot_event(account, qty=qty)
    return referral, event


# ─────────────────────────────────────────────────────────────────────────
# 1-5 — rule resolution precedence
# ─────────────────────────────────────────────────────────────────────────

class RuleResolutionTests(TestCase):
    def setUp(self):
        self.referral = _make_referral()

    def test_1_global_rule_resolves(self):
        rule = _make_rule(fixed_amount=Decimal("2.00"))
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(resolved.pk, rule.pk)

    def test_2_per_ib_override_wins_over_global(self):
        _make_rule(fixed_amount=Decimal("2.00"))  # global
        override = _make_rule(referral=self.referral, fixed_amount=Decimal("10.00"))
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(resolved.pk, override.pk)
        self.assertEqual(resolved.fixed_amount, Decimal("10.00"))

    def test_2b_other_ib_without_override_still_gets_global(self):
        global_rule = _make_rule(fixed_amount=Decimal("2.00"))
        _make_rule(referral=self.referral, fixed_amount=Decimal("10.00"))  # IB A's override
        other_referral = _make_referral()  # IB B — no override
        resolved = resolve_applicable_rule(other_referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(resolved.pk, global_rule.pk)

    def test_3_disabled_override_falls_through_to_global(self):
        global_rule = _make_rule(fixed_amount=Decimal("2.00"))
        _make_rule(referral=self.referral, enabled=False, fixed_amount=Decimal("10.00"))
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(resolved.pk, global_rule.pk)

    def test_4_expired_override_falls_through_to_global(self):
        global_rule = _make_rule(fixed_amount=Decimal("2.00"))
        now = timezone.now()
        _make_rule(
            referral=self.referral, fixed_amount=Decimal("10.00"),
            effective_from=now - timezone.timedelta(days=10),
            effective_until=now - timezone.timedelta(days=1),
        )
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT, at_time=now)
        self.assertEqual(resolved.pk, global_rule.pk)

    def test_5_future_dated_rule_is_ignored(self):
        now = timezone.now()
        _make_rule(fixed_amount=Decimal("2.00"), effective_from=now + timezone.timedelta(days=1))
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT, at_time=now)
        self.assertIsNone(resolved)

    def test_6_no_applicable_rule_returns_none(self):
        resolved = resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT)
        self.assertIsNone(resolved)

    def test_13_ambiguous_global_tier_raises(self):
        now = timezone.now()
        _make_rule(fixed_amount=Decimal("2.00"), effective_from=now)
        # Force a second simultaneously-active global rule, bypassing the
        # app-level partial-unique-index guard the normal admin workflow
        # would respect — this simulates the data-integrity edge case the
        # resolver must fail closed against, not silently pick one.
        IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_PER_LOT, referral__isnull=True).update(
            effective_until=now + timezone.timedelta(days=1),
        )
        _make_rule(fixed_amount=Decimal("5.00"), effective_from=now)
        with self.assertRaises(AmbiguousCommissionRuleError):
            resolve_applicable_rule(self.referral, IBCommissionRule.RULE_PER_LOT, at_time=now)


# ─────────────────────────────────────────────────────────────────────────
# 7 — no attribution -> no obligation
# ─────────────────────────────────────────────────────────────────────────

class NoAttributionTests(TestCase):
    def test_7_user_without_attribution_produces_no_obligation(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        event = _make_lot_event(account)
        _make_rule(fixed_amount=Decimal("10.00"))

        result = generate_per_lot_obligation(event)

        self.assertIsNone(result)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_account_with_no_user_produces_no_obligation(self):
        account = make_account(balance=Decimal("10000"))
        account.user = None
        account.save(update_fields=["user"])
        event = _make_lot_event(account)
        _make_rule(fixed_amount=Decimal("10.00"))

        result = generate_per_lot_obligation(event)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────
# 8/9/10/17 — PER_LOT calculation
# ─────────────────────────────────────────────────────────────────────────

class PerLotCalculationTests(TestCase):
    def test_8_half_lot_at_10_dollars(self):
        referral, event = _referred_setup(fixed_amount="10.00", qty="0.50")
        obligation = generate_per_lot_obligation(event)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.calculated_amount, Decimal("5.00"))
        self.assertEqual(obligation.applied_fixed_rate, Decimal("10.00"))
        self.assertEqual(obligation.basis_quantity, Decimal("0.50"))

    def test_9_high_value_rate_no_arbitrary_cap(self):
        referral, event = _referred_setup(fixed_amount="30.00", qty="1.00")
        obligation = generate_per_lot_obligation(event)
        self.assertEqual(obligation.calculated_amount, Decimal("30.00"))
        self.assertEqual(obligation.applied_fixed_rate, Decimal("30.00"))

    def test_9b_very_high_value_rate_still_uncapped(self):
        """No code anywhere enforces a ceiling — prove an even more
        unusual owner-configured rate still calculates correctly."""
        referral, event = _referred_setup(fixed_amount="500.00", qty="2.00")
        obligation = generate_per_lot_obligation(event)
        self.assertEqual(obligation.calculated_amount, Decimal("1000.00"))

    def test_10_zero_rate_creates_a_zero_dollar_obligation(self):
        """$0 is a valid economic value per the approved design — the
        engine has no special-casing to skip it: a $0 rule still
        generates a real, permanent PENDING obligation for $0.00."""
        referral, event = _referred_setup(fixed_amount="0.00", qty="1.00")
        obligation = generate_per_lot_obligation(event)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.calculated_amount, Decimal("0.00"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_17_decimal_calculation_no_float_artifacts(self):
        """0.1 lot at a rate classically prone to float rounding error
        (0.1 * 3 == 0.30000000000000004 in binary float) must be exact."""
        referral, event = _referred_setup(fixed_amount="3.00", qty="0.10")
        obligation = generate_per_lot_obligation(event)
        self.assertEqual(obligation.calculated_amount, Decimal("0.30"))
        self.assertIsInstance(obligation.calculated_amount, Decimal)
        self.assertNotIsInstance(obligation.calculated_amount, float)


# ─────────────────────────────────────────────────────────────────────────
# 11/15 — idempotency
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def test_11_same_event_processed_twice_yields_one_obligation(self):
        referral, event = _referred_setup(fixed_amount="10.00", qty="0.50")

        first = generate_per_lot_obligation(event)
        second = generate_per_lot_obligation(event)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_15_db_unique_constraint_prevents_duplicate_row(self):
        """Bypasses the engine's own get_or_create() to prove the
        constraint itself — not just application logic — blocks a
        duplicate row for the same (referral, rule_type, source_event_type,
        source_event_id) tuple."""
        referral, event = _referred_setup(fixed_amount="10.00", qty="0.50")
        obligation = generate_per_lot_obligation(event)

        with self.assertRaises(IntegrityError):
            IBCommissionObligation.objects.create(
                attribution=obligation.attribution,
                referral=referral,
                rule=obligation.rule,
                rule_type=IBCommissionRule.RULE_PER_LOT,
                source_event_type="per_lot_execution",
                source_event_id=event.pk,
                basis_quantity=Decimal("0.50"),
                applied_fixed_rate=Decimal("10.00"),
                calculated_amount=Decimal("5.00"),
            )


# ─────────────────────────────────────────────────────────────────────────
# 12 — historical snapshot survives a later rate change
# ─────────────────────────────────────────────────────────────────────────

class HistoricalSnapshotTests(TestCase):
    def test_12_old_obligation_keeps_original_rate_after_rule_changes(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))

        t0 = timezone.now() - timezone.timedelta(days=2)
        old_rule = _make_rule(fixed_amount=Decimal("2.00"), effective_from=t0)

        old_event = _make_lot_event(account, qty="3.00", created_at=t0)
        old_obligation = generate_per_lot_obligation(old_event)
        self.assertEqual(old_obligation.calculated_amount, Decimal("6.00"))
        self.assertEqual(old_obligation.applied_fixed_rate, Decimal("2.00"))

        # Rate change: close the old rule, open a new one — never mutate
        # the old row's economic fields.
        t1 = timezone.now() - timezone.timedelta(days=1)
        IBCommissionRule.objects.filter(pk=old_rule.pk).update(effective_until=t1)
        _make_rule(fixed_amount=Decimal("20.00"), effective_from=t1)

        new_event = _make_lot_event(account, qty="3.00", created_at=timezone.now())
        new_obligation = generate_per_lot_obligation(new_event)
        self.assertEqual(new_obligation.calculated_amount, Decimal("60.00"))
        self.assertEqual(new_obligation.applied_fixed_rate, Decimal("20.00"))

        # The OLD obligation is untouched, forever.
        old_obligation.refresh_from_db()
        self.assertEqual(old_obligation.calculated_amount, Decimal("6.00"))
        self.assertEqual(old_obligation.applied_fixed_rate, Decimal("2.00"))


# ─────────────────────────────────────────────────────────────────────────
# 14 — obligation creation never moves money
# ─────────────────────────────────────────────────────────────────────────

class NoMoneyMovementTests(TestCase):
    def test_14_no_wallet_ledger_or_treasury_side_effects(self):
        ib_owner = make_user()
        wallet = get_or_create_wallet(ib_owner)[0]
        balance_before = wallet.available_balance
        wtx_before = WalletTransaction.objects.filter(wallet=wallet).count()
        ledger_before = LedgerEntry.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()

        referral, event = _referred_setup(fixed_amount="10.00", qty="1.00")
        # _referred_setup creates its own IB owner distinct from the one
        # above; re-derive the real IB owner's wallet for the assertion.
        real_ib_owner = referral.user
        real_wallet = get_or_create_wallet(real_ib_owner)[0]

        obligation = generate_per_lot_obligation(event)

        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)
        real_wallet.refresh_from_db()
        self.assertEqual(real_wallet.available_balance, Decimal("0"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=real_wallet).count(), 0)
        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)
        self.assertIsNone(obligation.treasury_operation)
        # Untouched control wallet, sanity check.
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), wtx_before)


# ─────────────────────────────────────────────────────────────────────────
# 16 — correct links
# ─────────────────────────────────────────────────────────────────────────

class LinkageTests(TestCase):
    def test_16_obligation_links_are_correct(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        attribution = _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        rule = _make_rule(
            fixed_amount=Decimal("10.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        event = _make_lot_event(account, qty="0.50")

        obligation = generate_per_lot_obligation(event)

        self.assertEqual(obligation.attribution_id, attribution.pk)
        self.assertEqual(obligation.referral_id, referral.pk)
        self.assertEqual(obligation.rule_id, rule.pk)
        self.assertEqual(obligation.rule_type, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(obligation.source_event_type, "per_lot_execution")
        self.assertEqual(obligation.source_event_id, event.pk)
        self.assertIn(str(event.pk), obligation.source_reference)
