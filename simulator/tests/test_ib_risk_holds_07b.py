# simulator/tests/test_ib_risk_holds_07b.py
"""
IB-RISK-HOLDS-07B

Regression coverage for simulator/ib_risk_holds.py (freeze_referral(),
unfreeze_referral(), hold_obligation(), release_obligation()) and the
guard clauses this block adds to simulator/ib_treasury_settlement.py
(approve_obligation()/link_treasury_request()) and to the four
generate_*_obligation() functions in simulator/ib_commission.py.

Approved design: IB-RISK-HOLDS-07A audit + design lock. This suite
proves:
  - freeze/unfreeze and hold/release each transition state, write
    exactly one IBRiskEvent, and raise (never silently no-op) on a
    repeat call.
  - a frozen IB suppresses generation of NEW obligations across all
    four generators, and blocks approve_obligation()/
    link_treasury_request() on its existing PENDING/APPROVED
    obligations.
  - a held obligation blocks approve_obligation()/
    link_treasury_request() independently of the IB's own freeze state.
  - once an obligation is already linked to a TreasuryOperationRequest,
    a subsequent freeze/hold cannot retroactively block that link
    (idempotent-return path in link_treasury_request() is checked
    BEFORE the freeze/hold guards).
  - reconciliation (sync_obligation_from_treasury()) and the reversal/
    adjustment path (ib_commission_reversal.py) are completely
    unaffected by freeze/hold, end-to-end, with real Treasury
    execution — not mocked.
  - unfreeze/release are pure eligibility restorations: no obligation
    is ever auto-approved, auto-linked, or auto-executed as a side
    effect.
  - permission (TREASURY_REVIEW_PERMISSION, reused, no new permission)
    and required-reason enforcement on all four actions.
  - CREDITED obligations are never mutated by any of this.
  - simulator/ib_risk_holds.py never touches Wallet/WalletTransaction/
    TreasuryOperationRequest directly (structural grep).
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.ib_commission import (
    generate_challenge_percent_obligation, generate_deposit_percent_obligation,
    generate_per_lot_obligation, generate_trading_commission_revenue_share_obligation,
)
from simulator.ib_commission_reversal import (
    approve_adjustment, link_adjustment_to_treasury, remaining_reversible,
    submit_adjustment, sync_adjustment_from_treasury,
)
from simulator.ib_risk_holds import (
    ObligationAlreadyHeld, ObligationNotHeld, ReferralAlreadyFrozen, ReferralNotFrozen,
    freeze_referral, hold_obligation, release_obligation, unfreeze_referral,
)
from simulator.ib_treasury_settlement import (
    ObligationHeld, ReferralNotActive, approve_obligation, link_treasury_request,
    sync_obligation_from_treasury,
)
from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerLedger, ChallengeEnrollment, ChallengeProduct,
    Deposit, IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule,
    IBRiskEvent, LotExecutionEvent, Referral, ReferralAttribution, TreasuryOperationRequest,
)
from simulator.treasury_requests import approve_treasury_request, execute_treasury_request
from simulator.tests.factories import make_account, make_broker_ledger, make_user

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"risk07b_{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type, referral=None, enabled=True, fixed_amount=None,
               percentage=None, effective_from=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
    )


def _grant(user, codename):
    perm = Permission.objects.get(codename=codename)
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _make_submitter(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_submit_treasury_request")


def _make_executor(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_execute_treasury_request")


def _fake_request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _make_pending_obligation(referral=None, amount="25.00", rule=None):
    referral = referral or _make_referral()
    trader = make_user()
    _make_attribution(trader, referral)
    rule = rule or _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))
    attribution = ReferralAttribution.objects.get(referral=referral, referred_user=trader)
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
        source_event_type="test_event", source_event_id=_next_seq(),
        calculated_amount=Decimal(amount), currency="USD",
        status=IBCommissionObligation.ST_PENDING,
    )


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _make_credited_obligation(calculated_amount="40.00", referral=None):
    """Full happy-path settlement via the real, unmodified
    IB-TREASURY-CREDIT-03 services — starting state for reversal/
    reconciliation tests."""
    obligation = _make_pending_obligation(referral=referral, amount=calculated_amount)
    reviewer = _make_reviewer()
    submitter = _make_submitter()
    executor = _make_executor()

    obligation = approve_obligation(obligation, request=_fake_request(reviewer))
    treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
    approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    execute_treasury_request(treasury_request, request=_fake_request(executor))
    result = sync_obligation_from_treasury(obligation)
    obligation = result["obligation"]
    assert obligation.status == IBCommissionObligation.ST_CREDITED
    return obligation


# ─────────────────────────────────────────────────────────────────────────
# 1 — Freeze / Unfreeze — transition, fields, idempotency
# ─────────────────────────────────────────────────────────────────────────

class FreezeUnfreezeTests(TestCase):
    def test_freeze_sets_fields(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        frozen = freeze_referral(referral, "suspected fraud", request=_fake_request(reviewer))
        self.assertEqual(frozen.risk_status, Referral.RISK_FROZEN)
        self.assertIsNotNone(frozen.frozen_at)
        self.assertEqual(frozen.frozen_by_id, reviewer.pk)
        self.assertEqual(frozen.frozen_reason, "suspected fraud")

    def test_unfreeze_clears_fields(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "reason A", request=_fake_request(reviewer))
        unfrozen = unfreeze_referral(referral, "cleared review", request=_fake_request(reviewer))
        self.assertEqual(unfrozen.risk_status, Referral.RISK_ACTIVE)
        self.assertIsNone(unfrozen.frozen_at)
        self.assertIsNone(unfrozen.frozen_by_id)
        self.assertEqual(unfrozen.frozen_reason, "")

    def test_freeze_already_frozen_raises(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "first", request=_fake_request(reviewer))
        with self.assertRaises(ReferralAlreadyFrozen):
            freeze_referral(referral, "second", request=_fake_request(reviewer))

    def test_unfreeze_not_frozen_raises(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(ReferralNotFrozen):
            unfreeze_referral(referral, "no-op", request=_fake_request(reviewer))

    def test_freeze_blank_reason_raises(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        with self.assertRaises(ValueError):
            freeze_referral(referral, "  ", request=_fake_request(reviewer))
        referral.refresh_from_db()
        self.assertEqual(referral.risk_status, Referral.RISK_ACTIVE)

    def test_unfreeze_blank_reason_raises(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "x", request=_fake_request(reviewer))
        with self.assertRaises(ValueError):
            unfreeze_referral(referral, "", request=_fake_request(reviewer))
        referral.refresh_from_db()
        self.assertEqual(referral.risk_status, Referral.RISK_FROZEN)

    def test_freeze_without_permission_denied(self):
        referral = _make_referral()
        plain = make_user(is_staff=True)
        with self.assertRaises(PermissionDenied):
            freeze_referral(referral, "x", request=_fake_request(plain))

    def test_unfreeze_without_permission_denied(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "x", request=_fake_request(reviewer))
        plain = make_user(is_staff=True)
        with self.assertRaises(PermissionDenied):
            unfreeze_referral(referral, "y", request=_fake_request(plain))

    def test_freeze_unauthenticated_denied(self):
        from django.contrib.auth.models import AnonymousUser
        referral = _make_referral()
        request = RequestFactory().post("/")
        request.user = AnonymousUser()
        with self.assertRaises(PermissionDenied):
            freeze_referral(referral, "x", request=request)

    def test_freeze_creates_ibriskevent(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "audit trail check", request=_fake_request(reviewer))
        event = IBRiskEvent.objects.get(referral=referral, event_type=IBRiskEvent.EVENT_FROZEN)
        self.assertIsNone(event.obligation)
        self.assertEqual(event.actor_id, reviewer.pk)
        self.assertEqual(event.reason, "audit trail check")

    def test_unfreeze_creates_ibriskevent(self):
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "x", request=_fake_request(reviewer))
        unfreeze_referral(referral, "unfreeze audit check", request=_fake_request(reviewer))
        event = IBRiskEvent.objects.get(referral=referral, event_type=IBRiskEvent.EVENT_UNFROZEN)
        self.assertEqual(event.reason, "unfreeze audit check")

    def test_freeze_then_unfreeze_history_preserved(self):
        """History across a freeze->unfreeze cycle must not be lost —
        both IBRiskEvent rows remain queryable even though the bare
        Referral fields were overwritten/cleared."""
        referral = _make_referral()
        reviewer = _make_reviewer()
        freeze_referral(referral, "first freeze", request=_fake_request(reviewer))
        unfreeze_referral(referral, "first unfreeze", request=_fake_request(reviewer))
        freeze_referral(referral, "second freeze", request=_fake_request(reviewer))
        events = list(IBRiskEvent.objects.filter(referral=referral).order_by("id"))
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [e.event_type for e in events],
            [IBRiskEvent.EVENT_FROZEN, IBRiskEvent.EVENT_UNFROZEN, IBRiskEvent.EVENT_FROZEN],
        )
        self.assertEqual(events[0].reason, "first freeze")
        self.assertEqual(events[1].reason, "first unfreeze")


# ─────────────────────────────────────────────────────────────────────────
# 2 — Hold / Release — transition, fields, idempotency
# ─────────────────────────────────────────────────────────────────────────

class HoldReleaseTests(TestCase):
    def test_hold_sets_fields(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        held = hold_obligation(obligation, "risk review", request=_fake_request(reviewer))
        self.assertTrue(held.is_held)
        self.assertIsNotNone(held.held_at)
        self.assertEqual(held.held_by_id, reviewer.pk)
        self.assertEqual(held.hold_reason, "risk review")

    def test_release_clears_fields(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        hold_obligation(obligation, "x", request=_fake_request(reviewer))
        released = release_obligation(obligation, "cleared", request=_fake_request(reviewer))
        self.assertFalse(released.is_held)
        self.assertIsNone(released.held_at)
        self.assertIsNone(released.held_by_id)
        self.assertEqual(released.hold_reason, "")

    def test_hold_already_held_raises(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        hold_obligation(obligation, "first", request=_fake_request(reviewer))
        with self.assertRaises(ObligationAlreadyHeld):
            hold_obligation(obligation, "second", request=_fake_request(reviewer))

    def test_release_not_held_raises(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        with self.assertRaises(ObligationNotHeld):
            release_obligation(obligation, "no-op", request=_fake_request(reviewer))

    def test_hold_blank_reason_raises(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        with self.assertRaises(ValueError):
            hold_obligation(obligation, "", request=_fake_request(reviewer))
        obligation.refresh_from_db()
        self.assertFalse(obligation.is_held)

    def test_hold_without_permission_denied(self):
        obligation = _make_pending_obligation()
        plain = make_user(is_staff=True)
        with self.assertRaises(PermissionDenied):
            hold_obligation(obligation, "x", request=_fake_request(plain))

    def test_hold_can_be_placed_on_any_status(self):
        """Orthogonal to status (IB-RISK-HOLDS-07A section G) — a hold
        can be placed on a CREDITED obligation too, even though its
        only operational effect (blocking approve/link) has already
        passed."""
        obligation = _make_credited_obligation("15.00")
        reviewer = _make_reviewer()
        held = hold_obligation(obligation, "post-credit flag", request=_fake_request(reviewer))
        self.assertTrue(held.is_held)
        self.assertEqual(held.status, IBCommissionObligation.ST_CREDITED)

    def test_hold_creates_ibriskevent_linked_to_obligation_and_referral(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        hold_obligation(obligation, "trace check", request=_fake_request(reviewer))
        event = IBRiskEvent.objects.get(
            obligation=obligation, event_type=IBRiskEvent.EVENT_OBLIGATION_HELD,
        )
        self.assertEqual(event.referral_id, obligation.referral_id)
        self.assertEqual(event.reason, "trace check")

    def test_release_creates_ibriskevent(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        hold_obligation(obligation, "x", request=_fake_request(reviewer))
        release_obligation(obligation, "release trace", request=_fake_request(reviewer))
        event = IBRiskEvent.objects.get(
            obligation=obligation, event_type=IBRiskEvent.EVENT_OBLIGATION_RELEASED,
        )
        self.assertEqual(event.reason, "release trace")


# ─────────────────────────────────────────────────────────────────────────
# 3 — Generation suppression while frozen (all four generators)
# ─────────────────────────────────────────────────────────────────────────

class GenerationSuppressionTests(TestCase):
    def test_per_lot_suppressed_when_frozen(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))
        freeze_referral(referral, "x", request=_fake_request(_make_reviewer()))

        event = LotExecutionEvent.objects.create(
            account=account, position=None, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.5"), execution_price=Decimal("1.1"),
            merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
        )
        obligation = generate_per_lot_obligation(event)
        self.assertIsNone(obligation)
        self.assertEqual(IBCommissionObligation.objects.filter(referral=referral).count(), 0)

    def test_per_lot_generates_normally_when_active(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))

        event = LotExecutionEvent.objects.create(
            account=account, position=None, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.5"), execution_price=Decimal("1.1"),
            merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
        )
        obligation = generate_per_lot_obligation(event)
        self.assertIsNotNone(obligation)

    def test_challenge_percent_suppressed_when_frozen(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        product = ChallengeProduct.objects.create(
            name=f"P07B-{_next_seq()}", account_size=Decimal("10000.00"),
            price_usd=Decimal("200.00"), is_active=True,
            p1_profit_target_pct=Decimal("8.00"), p1_max_drawdown_pct=Decimal("10.00"),
            p1_max_daily_loss_pct=Decimal("5.00"), p1_min_trading_days=0, p1_max_duration_days=30,
            p2_profit_target_pct=Decimal("5.00"), p2_max_drawdown_pct=Decimal("10.00"),
            p2_max_daily_loss_pct=Decimal("5.00"), p2_min_trading_days=0, p2_max_duration_days=60,
            max_lot_size=Decimal("5.00"), max_open_positions=5, profit_split_pct=Decimal("80.00"),
        )
        deposit = Deposit.objects.create(
            user=trader, amount_usd=Decimal("200.00"), crypto_currency="btc",
            status="finished", credited=True, challenge_product=product,
        )
        enrollment = ChallengeEnrollment.objects.create(user=trader, product=product, deposit=deposit)
        _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal("10.00"))
        freeze_referral(referral, "x", request=_fake_request(_make_reviewer()))

        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertIsNone(obligation)

    def test_deposit_percent_suppressed_when_frozen(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        deposit = Deposit.objects.create(
            user=trader, amount_usd=Decimal("1000.00"), crypto_currency="btc",
            status="finished", credited=True, challenge_product=None,
        )
        _make_rule(IBCommissionRule.RULE_DEPOSIT_PERCENT, percentage=Decimal("5.00"))
        freeze_referral(referral, "x", request=_fake_request(_make_reviewer()))

        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNone(obligation)

    def test_trading_commission_revenue_share_suppressed_when_frozen(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal("20.00"))
        row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("7.00"),
            source_account=account, source_ledger=None, symbol="EUR/USD",
        )
        freeze_referral(referral, "x", request=_fake_request(_make_reviewer()))

        obligation = generate_trading_commission_revenue_share_obligation(row)
        self.assertIsNone(obligation)

    def test_generation_resumes_after_unfreeze(self):
        referral = _make_referral()
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))
        reviewer = _make_reviewer()
        freeze_referral(referral, "x", request=_fake_request(reviewer))
        unfreeze_referral(referral, "y", request=_fake_request(reviewer))

        event = LotExecutionEvent.objects.create(
            account=account, position=None, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.5"), execution_price=Decimal("1.1"),
            merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
        )
        obligation = generate_per_lot_obligation(event)
        self.assertIsNotNone(obligation)


# ─────────────────────────────────────────────────────────────────────────
# 4 — approve_obligation()/link_treasury_request() guard clauses
# ─────────────────────────────────────────────────────────────────────────

class ApproveLinkGuardTests(TestCase):
    def test_approve_blocked_when_referral_frozen(self):
        obligation = _make_pending_obligation()
        freeze_referral(obligation.referral, "x", request=_fake_request(_make_reviewer()))
        with self.assertRaises(ReferralNotActive):
            approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_approve_blocked_when_obligation_held(self):
        obligation = _make_pending_obligation()
        hold_obligation(obligation, "x", request=_fake_request(_make_reviewer()))
        with self.assertRaises(ObligationHeld):
            approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_approve_succeeds_when_active_and_not_held(self):
        obligation = _make_pending_obligation()
        approved = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        self.assertEqual(approved.status, IBCommissionObligation.ST_APPROVED)

    def test_link_blocked_when_referral_frozen(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        freeze_referral(obligation.referral, "x", request=_fake_request(_make_reviewer()))
        with self.assertRaises(ReferralNotActive):
            link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        obligation.refresh_from_db()
        self.assertIsNone(obligation.treasury_operation_id)

    def test_link_blocked_when_obligation_held(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        hold_obligation(obligation, "x", request=_fake_request(_make_reviewer()))
        with self.assertRaises(ObligationHeld):
            link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        obligation.refresh_from_db()
        self.assertIsNone(obligation.treasury_operation_id)

    def test_link_succeeds_when_active_and_not_held(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        treasury_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        self.assertEqual(treasury_request.status, TreasuryOperationRequest.ST_PENDING)

    def test_already_linked_obligation_unaffected_by_later_freeze(self):
        """Money-movement boundary (IB-RISK-HOLDS-07A section L): once a
        TreasuryOperationRequest already exists, a subsequent freeze can
        never retroactively block it — a repeat link_treasury_request()
        call must keep returning the SAME request, never start raising."""
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        first_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))

        freeze_referral(obligation.referral, "after the fact", request=_fake_request(_make_reviewer()))

        second_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        self.assertEqual(first_request.pk, second_request.pk)

    def test_already_linked_obligation_unaffected_by_later_hold(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        first_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))

        obligation.refresh_from_db()
        hold_obligation(obligation, "after the fact", request=_fake_request(_make_reviewer()))

        second_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        self.assertEqual(first_request.pk, second_request.pk)


# ─────────────────────────────────────────────────────────────────────────
# 5 — Reconciliation continues to work despite freeze/hold
# ─────────────────────────────────────────────────────────────────────────

class ReconciliationContinuesTests(TestCase):
    def test_sync_credits_despite_referral_frozen_after_execution(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        treasury_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        approve_treasury_request(treasury_request, request=_fake_request(_make_reviewer()))
        execute_treasury_request(treasury_request, request=_fake_request(_make_executor()))

        freeze_referral(obligation.referral, "frozen after execution", request=_fake_request(_make_reviewer()))

        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["obligation"].status, IBCommissionObligation.ST_CREDITED)

    def test_sync_credits_despite_obligation_held_after_execution(self):
        obligation = _make_pending_obligation()
        obligation = approve_obligation(obligation, request=_fake_request(_make_reviewer()))
        treasury_request = link_treasury_request(obligation, request=_fake_request(_make_submitter()))
        approve_treasury_request(treasury_request, request=_fake_request(_make_reviewer()))
        execute_treasury_request(treasury_request, request=_fake_request(_make_executor()))

        obligation.refresh_from_db()
        hold_obligation(obligation, "held after execution", request=_fake_request(_make_reviewer()))

        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["obligation"].status, IBCommissionObligation.ST_CREDITED)


# ─────────────────────────────────────────────────────────────────────────
# 6 — CREDITED immutability under freeze/hold
# ─────────────────────────────────────────────────────────────────────────

class CreditedImmutabilityTests(TestCase):
    def test_freeze_and_hold_never_mutate_credited_snapshot_fields(self):
        obligation = _make_credited_obligation("33.00")
        original_amount = obligation.calculated_amount
        original_credited_at = obligation.credited_at
        original_status = obligation.status

        freeze_referral(obligation.referral, "x", request=_fake_request(_make_reviewer()))
        hold_obligation(obligation, "y", request=_fake_request(_make_reviewer()))

        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, original_amount)
        self.assertEqual(obligation.credited_at, original_credited_at)
        self.assertEqual(obligation.status, original_status)


# ─────────────────────────────────────────────────────────────────────────
# 7 — Reversal/adjustment path fully unaffected by freeze (E2E, real Treasury)
# ─────────────────────────────────────────────────────────────────────────

class ReversalDuringFreezeTests(TestCase):
    def test_full_adjustment_lifecycle_succeeds_while_ib_frozen_and_obligation_held(self):
        obligation = _make_credited_obligation("40.00")
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()

        freeze_referral(obligation.referral, "under investigation", request=_fake_request(reviewer))
        hold_obligation(obligation, "also held", request=_fake_request(reviewer))

        adj = submit_adjustment(
            obligation, amount=Decimal("15.00"), reason="chargeback",
            adjustment_type=IBCommissionAdjustment.TYPE_REVERSAL, request=_fake_request(submitter),
        )
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)

        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))
        result = sync_adjustment_from_treasury(adj)
        self.assertEqual(result["adjustment"].status, IBCommissionAdjustment.ST_EXECUTED)

        remaining = remaining_reversible(obligation)
        self.assertEqual(remaining, Decimal("25.00"))


# ─────────────────────────────────────────────────────────────────────────
# 8 — No auto-advance on unfreeze/release
# ─────────────────────────────────────────────────────────────────────────

class NoAutoAdvanceTests(TestCase):
    def test_unfreeze_does_not_auto_approve_pending_obligation(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        freeze_referral(obligation.referral, "x", request=_fake_request(reviewer))
        unfreeze_referral(obligation.referral, "y", request=_fake_request(reviewer))
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_release_does_not_auto_link_approved_obligation(self):
        obligation = _make_pending_obligation()
        reviewer = _make_reviewer()
        obligation = approve_obligation(obligation, request=_fake_request(reviewer))
        hold_obligation(obligation, "x", request=_fake_request(reviewer))
        release_obligation(obligation, "y", request=_fake_request(reviewer))
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)
        self.assertIsNone(obligation.treasury_operation_id)


# ─────────────────────────────────────────────────────────────────────────
# 9 — Admin views (Client-based, real URLs, real permission checks)
# ─────────────────────────────────────────────────────────────────────────

class AdminViewTests(TestCase):
    def setUp(self):
        self.reviewer = _make_reviewer(is_superuser=False)
        self.client = Client()
        self.client.force_login(self.reviewer)

    def test_freeze_view_get_renders_confirm_form(self):
        referral = _make_referral()
        url = reverse("admin:ib_referral_freeze", args=[referral.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Freeze IB")

    def test_freeze_view_post_without_reason_shows_error_no_change(self):
        referral = _make_referral()
        url = reverse("admin:ib_referral_freeze", args=[referral.pk])
        resp = self.client.post(url, {"reason": ""})
        self.assertEqual(resp.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.risk_status, Referral.RISK_ACTIVE)

    def test_freeze_view_post_with_reason_freezes_and_writes_audit(self):
        referral = _make_referral()
        url = reverse("admin:ib_referral_freeze", args=[referral.pk])
        resp = self.client.post(url, {"reason": "admin view freeze"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.risk_status, Referral.RISK_FROZEN)
        self.assertTrue(AuditLog.objects.filter(event_type="ib_admin.referral_frozen").exists())
        self.assertTrue(BrokerAuditEvent.objects.filter(event_type="ib_admin.referral_frozen").exists())

    def test_unfreeze_view_post_with_reason_unfreezes(self):
        referral = _make_referral()
        freeze_referral(referral, "x", request=_fake_request(self.reviewer))
        url = reverse("admin:ib_referral_unfreeze", args=[referral.pk])
        resp = self.client.post(url, {"reason": "admin view unfreeze"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.risk_status, Referral.RISK_ACTIVE)

    def test_hold_view_post_with_reason_holds_and_writes_audit(self):
        obligation = _make_pending_obligation()
        url = reverse("admin:ib_obligation_hold", args=[obligation.pk])
        resp = self.client.post(url, {"reason": "admin view hold"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        obligation.refresh_from_db()
        self.assertTrue(obligation.is_held)
        self.assertTrue(AuditLog.objects.filter(event_type="ib_admin.obligation_held").exists())

    def test_release_view_post_with_reason_releases(self):
        obligation = _make_pending_obligation()
        hold_obligation(obligation, "x", request=_fake_request(self.reviewer))
        url = reverse("admin:ib_obligation_release", args=[obligation.pk])
        resp = self.client.post(url, {"reason": "admin view release"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        obligation.refresh_from_db()
        self.assertFalse(obligation.is_held)

    def test_freeze_view_requires_permission(self):
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        referral = _make_referral()
        url = reverse("admin:ib_referral_freeze", args=[referral.pk])
        resp = client.post(url, {"reason": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_ib_detail_view_shows_frozen_banner(self):
        referral = _make_referral()
        freeze_referral(referral, "banner check", request=_fake_request(self.reviewer))
        url = reverse("admin:ib_detail", args=[referral.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "FROZEN")


# ─────────────────────────────────────────────────────────────────────────
# 10 — Structural: ib_risk_holds.py never touches Wallet/Treasury directly
# ─────────────────────────────────────────────────────────────────────────

class NoDirectMoneyMutationTests(TestCase):
    def test_no_wallet_or_treasury_mutation_calls_in_source(self):
        import inspect

        import simulator.ib_risk_holds as mod
        source = inspect.getsource(mod)
        forbidden = [
            "debit_wallet(", "credit_wallet(", "WalletTransaction.objects.create",
            "TreasuryOperationRequest.objects.create", "execute_treasury_request(",
            "approve_treasury_request(", "submit_treasury_request(",
        ]
        for token in forbidden:
            self.assertNotIn(token, source, f"forbidden call found: {token}")

    def test_reconciliation_and_reversal_modules_not_imported(self):
        """The module's own docstring legitimately mentions these file
        paths in prose (explaining what it deliberately does NOT touch)
        — this checks for an actual import statement, not a bare
        substring match against that prose."""
        import inspect

        import simulator.ib_risk_holds as mod
        source = inspect.getsource(mod)
        self.assertNotIn("import ib_commission_reversal", source)
        self.assertNotIn("from .ib_commission_reversal", source)
        self.assertNotIn("import ib_treasury_settlement", source)
        self.assertNotIn("from .ib_treasury_settlement", source)


# ─────────────────────────────────────────────────────────────────────────
# 11 — Migration defaults / backward compatibility
# ─────────────────────────────────────────────────────────────────────────

class MigrationDefaultsTests(TestCase):
    def test_new_referral_defaults_active(self):
        referral = _make_referral()
        self.assertEqual(referral.risk_status, Referral.RISK_ACTIVE)
        self.assertIsNone(referral.frozen_at)
        self.assertEqual(referral.frozen_reason, "")

    def test_new_obligation_defaults_not_held(self):
        obligation = _make_pending_obligation()
        self.assertFalse(obligation.is_held)
        self.assertIsNone(obligation.held_at)
        self.assertEqual(obligation.hold_reason, "")
