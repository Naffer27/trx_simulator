# simulator/tests/test_ib_commission_parity_09b1.py
"""
IB-COMMISSION-PARITY-09B.1 — Percentage configuration safety.

Regression coverage for the two fixes this block makes:
  - simulator/ib_admin_ops.py::change_commission_rate() generalized to
    dispatch on IBCommissionRule.FIXED_AMOUNT_RULE_TYPES/
    PERCENTAGE_RULE_TYPES (the model's own existing classification,
    reused unmodified) instead of unconditionally writing
    fixed_amount/percentage=None.
  - simulator/models.py::IBCommissionRule — new "ibrule_percentage_
    lte_100" DB CheckConstraint + a matching Model.clean() check,
    migration 0087 (purely additive, verified zero pre-existing rows
    violated it before the constraint was added).

Same service, not a second one — no new resolver, no new commission
engine. RULE_PER_LOT's own fixed_amount semantics are completely
unchanged (still requires > 0). CPA_BONUS/SPREAD_REVENUE_SHARE remain
untouched/unreferenced by every test in this file.

Money-safety / no-second-engine discipline, same as every prior IB
block: no WalletTransaction, no TreasuryOperationRequest, no wallet
credit/debit anywhere in this file.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from simulator.ib_admin_ops import (
    CommissionRateUnchanged, InvalidCommissionRate, change_commission_rate,
    ib_effective_rate,
)
from simulator.models import (
    IBCommissionObligation, IBCommissionRule, Referral, ReferralAttribution,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.tests.factories import make_user

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"parity09b1_{_seq}"


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _make_referral():
    return Referral.objects.create(user=make_user(), code=_code())


def _make_reviewer():
    from django.contrib.auth.models import Permission
    reviewer = make_user(is_staff=True)
    reviewer.user_permissions.add(Permission.objects.get(codename="can_review_treasury_request"))
    return type(reviewer).objects.get(pk=reviewer.pk)


def _fake_request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _make_obligation(referral, rule, calculated_amount, applied_percentage_rate=None, applied_fixed_rate=None):
    trader = make_user()
    attribution = ReferralAttribution.objects.create(
        referred_user=trader, referral=referral, source=ReferralAttribution.SOURCE_SESSION,
    )
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
        source_event_type="test_event", source_event_id=_next_seq(),
        applied_percentage_rate=applied_percentage_rate, applied_fixed_rate=applied_fixed_rate,
        calculated_amount=Decimal(calculated_amount), currency="USD",
        status=IBCommissionObligation.ST_PENDING,
    )


# ─────────────────────────────────────────────────────────────────────────
# PER_LOT unaffected — fixed_amount semantics completely unchanged
# ─────────────────────────────────────────────────────────────────────────

class PerLotUnaffectedTests(TestCase):
    def test_per_lot_still_works(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        new_rule = change_commission_rate(ref, "8.00", request=_fake_request(reviewer))
        self.assertEqual(new_rule.rule_type, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(new_rule.fixed_amount, Decimal("8.00"))
        self.assertIsNone(new_rule.percentage)

    def test_per_lot_zero_still_rejected(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(ref, "0", request=_fake_request(reviewer))

    def test_per_lot_negative_still_rejected(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(ref, "-5.00", request=_fake_request(reviewer))


# ─────────────────────────────────────────────────────────────────────────
# Commission revenue share 20% -> 25%, versioning, historical snapshot
# ─────────────────────────────────────────────────────────────────────────

class PercentageRateChangeTests(TestCase):
    def setUp(self):
        self.ref = _make_referral()
        self.reviewer = _make_reviewer()
        self.rule_v1 = IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=self.ref, enabled=True, percentage=Decimal("20.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )

    def test_20_to_25_creates_new_rule(self):
        new_rule = change_commission_rate(
            self.ref, "25", request=_fake_request(self.reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.assertEqual(new_rule.percentage, Decimal("25"))
        self.assertIsNone(new_rule.fixed_amount)
        self.assertNotEqual(new_rule.pk, self.rule_v1.pk)

    def test_old_rule_versioned_closed(self):
        change_commission_rate(
            self.ref, "25", request=_fake_request(self.reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.rule_v1.refresh_from_db()
        self.assertIsNotNone(self.rule_v1.effective_until)
        self.assertEqual(self.rule_v1.percentage, Decimal("20.00"), "old row's economic field never mutated")

    def test_historical_obligation_keeps_20_percent(self):
        obligation = _make_obligation(self.ref, self.rule_v1, "1.60", applied_percentage_rate=Decimal("20.00"))
        change_commission_rate(
            self.ref, "25", request=_fake_request(self.reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        obligation.refresh_from_db()
        self.assertEqual(obligation.applied_percentage_rate, Decimal("20.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("1.60"))

    def test_new_obligation_after_change_uses_25_percent(self):
        change_commission_rate(
            self.ref, "25", request=_fake_request(self.reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        rule, source = ib_effective_rate(self.ref, IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)
        self.assertEqual(rule.percentage, Decimal("25"))
        self.assertEqual(source, "per-IB")


# ─────────────────────────────────────────────────────────────────────────
# Boundary tests — 0%, 100%, negative, 100.001%, 150%
# ─────────────────────────────────────────────────────────────────────────

class PercentageBoundaryServiceTests(TestCase):
    """Protection at the service level (change_commission_rate())."""

    def test_zero_percent_accepted(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        new_rule = change_commission_rate(
            ref, "0", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.assertEqual(new_rule.percentage, Decimal("0"))

    def test_100_percent_accepted(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        new_rule = change_commission_rate(
            ref, "100", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.assertEqual(new_rule.percentage, Decimal("100"))

    def test_negative_percent_rejected(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(
                ref, "-1", request=_fake_request(reviewer),
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            )

    def test_100_001_percent_rejected(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(
                ref, "100.001", request=_fake_request(reviewer),
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            )

    def test_150_percent_rejected(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(
                ref, "150", request=_fake_request(reviewer),
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            )


class PercentageBoundaryFullCleanTests(TestCase):
    """Protection at the Model.clean()/full_clean() level."""

    def _rule(self, percentage):
        return IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=percentage,
            effective_from=timezone.now(),
        )

    def test_zero_percent_passes_full_clean(self):
        self._rule(Decimal("0")).full_clean(exclude=["id"])

    def test_100_percent_passes_full_clean(self):
        self._rule(Decimal("100")).full_clean(exclude=["id"])

    def test_negative_percent_fails_full_clean(self):
        with self.assertRaises(ValidationError):
            self._rule(Decimal("-1")).full_clean(exclude=["id"])

    def test_100_001_percent_fails_full_clean(self):
        with self.assertRaises(ValidationError):
            self._rule(Decimal("100.001")).full_clean(exclude=["id"])

    def test_150_percent_fails_full_clean(self):
        with self.assertRaises(ValidationError):
            self._rule(Decimal("150")).full_clean(exclude=["id"])


class PercentageBoundaryDbConstraintTests(TestCase):
    """Protection at the raw DB CheckConstraint level — the last line of
    defense, proven independently of clean()/full_clean() by bypassing
    them entirely (direct .save() on an unvalidated instance)."""

    def test_zero_percent_saves_directly(self):
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=Decimal("0"),
            effective_from=timezone.now(),
        )
        rule.save()

    def test_100_percent_saves_directly(self):
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=Decimal("100"),
            effective_from=timezone.now(),
        )
        rule.save()

    def test_100_001_percent_rejected_by_db(self):
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=Decimal("100.001"),
            effective_from=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                rule.save()

    def test_150_percent_rejected_by_db(self):
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=Decimal("150"),
            effective_from=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                rule.save()

    def test_negative_still_rejected_by_db(self):
        """Non-regression: the pre-existing gte_0 constraint (untouched
        by this block) still works alongside the new upper-bound one."""
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=_make_referral(), enabled=True, percentage=Decimal("-1"),
            effective_from=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                rule.save()


# ─────────────────────────────────────────────────────────────────────────
# Global / per-IB override protection (percentage types)
# ─────────────────────────────────────────────────────────────────────────

class GlobalAndPerIbProtectionTests(TestCase):
    def test_global_percentage_rule_not_modified_when_creating_ib_override(self):
        global_rule = IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=None, enabled=True, percentage=Decimal("10.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        ref = _make_referral()
        reviewer = _make_reviewer()

        change_commission_rate(
            ref, "30", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )

        global_rule.refresh_from_db()
        self.assertIsNone(global_rule.effective_until)
        self.assertEqual(global_rule.percentage, Decimal("10.00"))

    def test_per_ib_override_isolated_from_other_ib(self):
        IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=None, enabled=True, percentage=Decimal("10.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        ref_a = _make_referral()
        ref_b = _make_referral()
        rule_b = IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref_b, enabled=True, percentage=Decimal("15.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        reviewer = _make_reviewer()

        change_commission_rate(
            ref_a, "40", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )

        rule_b.refresh_from_db()
        self.assertIsNone(rule_b.effective_until)
        self.assertEqual(rule_b.percentage, Decimal("15.00"))

        rule_a, source_a = ib_effective_rate(ref_a, IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)
        self.assertEqual(rule_a.percentage, Decimal("40"))
        self.assertEqual(source_a, "per-IB")


# ─────────────────────────────────────────────────────────────────────────
# No-op protection / no ambiguous duplicate open rules
# ─────────────────────────────────────────────────────────────────────────

class NoOpAndNoDuplicateOpenRulesTests(TestCase):
    def test_same_percentage_rejected_as_noop(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, enabled=True, percentage=Decimal("20.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        with self.assertRaises(CommissionRateUnchanged):
            change_commission_rate(
                ref, "20.00", request=_fake_request(reviewer),
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            )

    def test_zero_to_zero_rejected_as_noop(self):
        """0% is itself a real, meaningful contract — a repeat 0% submit
        must be caught as a no-op exactly like any other value."""
        ref = _make_referral()
        reviewer = _make_reviewer()
        IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, enabled=True, percentage=Decimal("0.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        with self.assertRaises(CommissionRateUnchanged):
            change_commission_rate(
                ref, "0", request=_fake_request(reviewer),
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            )

    def test_sequential_percentage_changes_never_leave_two_open_rules(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, enabled=True, percentage=Decimal("5.00"),
            effective_from=timezone.now() - timezone.timedelta(minutes=5),
        )
        change_commission_rate(ref, "10", request=_fake_request(reviewer), rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)
        change_commission_rate(ref, "20", request=_fake_request(reviewer), rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)
        change_commission_rate(ref, "30", request=_fake_request(reviewer), rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)

        open_rules = IBCommissionRule.objects.filter(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, effective_until__isnull=True,
        )
        self.assertEqual(open_rules.count(), 1)
        self.assertEqual(open_rules.first().percentage, Decimal("30"))
        self.assertEqual(
            IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=ref).count(),
            4,
        )

    def test_first_time_percentage_override_no_prior_rule(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        new_rule = change_commission_rate(
            ref, "33", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.assertEqual(new_rule.percentage, Decimal("33"))
        self.assertEqual(new_rule.referral_id, ref.pk)


# ─────────────────────────────────────────────────────────────────────────
# Structural — no second engine, no second resolver, NULL still valid
# for fixed-amount types
# ─────────────────────────────────────────────────────────────────────────

class StructuralTests(TestCase):
    def test_null_percentage_still_valid_for_fixed_amount_types(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        new_rule = change_commission_rate(ref, "8.00", request=_fake_request(reviewer))
        self.assertIsNone(new_rule.percentage)
        self.assertEqual(new_rule.fixed_amount, Decimal("8.00"))

    def test_unsupported_rule_type_raises(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(ref, "10", request=_fake_request(reviewer), rule_type="NOT_A_REAL_TYPE")

    def test_no_wallet_or_treasury_writes(self):
        self.assertEqual(WalletTransaction.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_no_cpa_bonus_or_spread_revenue_share_activated(self):
        self.assertEqual(IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_CPA_BONUS).count(), 0)
        self.assertEqual(IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE).count(), 0)
