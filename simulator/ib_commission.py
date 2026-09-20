"""
simulator/ib_commission.py
IB-COMMISSION-ENGINE-01 — rule resolution + PER_LOT commission calculation.

Approved design: IB-COMMISSION-RULES-DESIGN-01 /
IB-COMMISSION-RULES-DESIGN-01A / IB-PER-LOT-EXECUTION-EVENT-DESIGN-01.

Scope of this module, deliberately narrow:
  - resolve_applicable_rule() — the 3-tier precedence resolver
    (per-IB override -> global -> None), fail-closed on ambiguity.
  - generate_per_lot_obligation() — PER_LOT commission calculation from a
    single LotExecutionEvent, idempotent, creating exactly one
    IBCommissionObligation in PENDING status.

Creating an IBCommissionObligation here NEVER moves money: no
credit_wallet(), no WalletTransaction, no LedgerEntry, no
TreasuryOperationRequest. It is purely a calculated PENDING liability
record. Wiring this into consumers.py's live execution paths, and the
other 5 rule types (CHALLENGE_PERCENT/DEPOSIT_PERCENT/
SPREAD_REVENUE_SHARE/TRADING_COMMISSION_REVENUE_SHARE/CPA_BONUS), belong
to future, separate blocks (IB-COMMISSION-TRIGGERS-02 and beyond).
"""
import logging
from decimal import ROUND_HALF_EVEN, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    IBCommissionObligation, IBCommissionRule, LotExecutionEvent, ReferralAttribution,
)

logger = logging.getLogger("simulator.ib_commission")


class AmbiguousCommissionRuleError(Exception):
    """Raised by resolve_applicable_rule() when more than one candidate
    rule is simultaneously active at the same precedence tier — a data-
    integrity situation the partial unique index on IBCommissionRule
    should normally prevent. Never guessed past: callers must treat this
    as "no rule resolved", not silently pick one."""


def _money_round(value: Decimal) -> Decimal:
    """Explicit, deterministic 2dp rounding for commission amounts —
    ROUND_HALF_EVEN, same convention consumers.py::_money_round()
    documents and pins for ledger-facing amounts (LEDGER-ROUNDING-
    RECONCILIATION-01) — made explicit here rather than left to the
    implicit default decimal context, so a future context change can
    never silently alter financial rounding. Not imported from
    consumers.py on purpose — this module has no WS dependency."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def resolve_applicable_rule(referral, rule_type, at_time=None):
    """
    3-tier precedence resolver (IB-COMMISSION-RULES-DESIGN-01 section C):
      1. active per-IB override (referral=<referral>)
      2. otherwise active global rule (referral=None)
      3. otherwise None

    "Active" = enabled=True, effective_from <= at_time, and
    (effective_until is NULL or effective_until > at_time).

    A disabled or expired per-IB override is treated as ABSENT — falls
    through to the global tier, never to "zero commission". A future-
    dated rule is inactive (falls through) regardless of tier.

    Raises AmbiguousCommissionRuleError if more than one candidate
    resolves at a single tier — never guesses. Callers must treat that
    as "no rule", not attempt to pick one.
    """
    at_time = at_time or timezone.now()

    def _active_candidates(referral_filter):
        return IBCommissionRule.objects.filter(
            rule_type=rule_type,
            referral=referral_filter,
            enabled=True,
            effective_from__lte=at_time,
        ).filter(
            Q(effective_until__isnull=True) | Q(effective_until__gt=at_time)
        )

    if referral is not None:
        per_ib = list(_active_candidates(referral))
        if len(per_ib) > 1:
            raise AmbiguousCommissionRuleError(
                f"Ambiguous per-IB {rule_type} rule for referral={referral.pk} "
                f"at {at_time.isoformat()} — {len(per_ib)} candidates: "
                f"{[r.pk for r in per_ib]}"
            )
        if len(per_ib) == 1:
            return per_ib[0]

    global_candidates = list(_active_candidates(None))
    if len(global_candidates) > 1:
        raise AmbiguousCommissionRuleError(
            f"Ambiguous global {rule_type} rule at {at_time.isoformat()} — "
            f"{len(global_candidates)} candidates: {[r.pk for r in global_candidates]}"
        )
    if len(global_candidates) == 1:
        return global_candidates[0]

    return None


def generate_per_lot_obligation(lot_execution_event: LotExecutionEvent):
    """
    Calculate (never credit) the PER_LOT IB commission owed for a single
    LotExecutionEvent. Creates exactly one PENDING IBCommissionObligation,
    or returns None (no error) when there's nothing to generate:
      - the executing account has no user, or that user has no
        ReferralAttribution (not a referred trader — the common case)
      - no PER_LOT rule resolves for this referral at this event's time
      - the rule tier is ambiguous (fail-closed, logged)

    Idempotent: a second call for the SAME LotExecutionEvent returns the
    existing obligation, never creates a duplicate — enforced by the DB
    UniqueConstraint on (referral, rule_type, source_event_type,
    source_event_id), not merely by an in-process check, so this is safe
    across retries, duplicate signals, and process restarts.

    Never moves money: no credit_wallet(), no WalletTransaction, no
    LedgerEntry, no TreasuryOperationRequest. Only ever writes a PENDING
    IBCommissionObligation row.
    """
    user = lot_execution_event.account.user
    if user is None:
        return None

    try:
        attribution = ReferralAttribution.objects.select_related("referral").get(
            referred_user=user,
        )
    except ReferralAttribution.DoesNotExist:
        return None

    referral = attribution.referral

    try:
        rule = resolve_applicable_rule(
            referral, IBCommissionRule.RULE_PER_LOT,
            at_time=lot_execution_event.created_at,
        )
    except AmbiguousCommissionRuleError as exc:
        logger.error(
            "[ib_commission] PER_LOT rule resolution ambiguous for "
            "lot_execution_event=%d referral=%d — refusing to generate "
            "an obligation: %s",
            lot_execution_event.pk, referral.pk, exc,
        )
        return None

    if rule is None:
        return None

    basis_quantity = lot_execution_event.qty
    applied_fixed_rate = rule.fixed_amount
    calculated_amount = _money_round(basis_quantity * applied_fixed_rate)

    source_reference = (
        f"LotExecutionEvent #{lot_execution_event.pk} "
        f"{lot_execution_event.symbol} {lot_execution_event.side} "
        f"qty={lot_execution_event.qty} ({lot_execution_event.entry_path})"
    )

    # Idempotency: the DB UniqueConstraint on IBCommissionObligation
    # (referral, rule_type, source_event_type, source_event_id) is the
    # real guarantee — get_or_create() is the fast path for the common
    # case; the IntegrityError fallback covers a genuine race (two
    # concurrent callers for the same event), mirroring the exact
    # pattern already established for ReferralAttribution/PayoutAttempt
    # elsewhere in this codebase.
    try:
        with transaction.atomic():
            obligation, created = IBCommissionObligation.objects.get_or_create(
                referral=referral,
                rule_type=IBCommissionRule.RULE_PER_LOT,
                source_event_type="per_lot_execution",
                source_event_id=lot_execution_event.pk,
                defaults={
                    "attribution": attribution,
                    "rule": rule,
                    "source_reference": source_reference,
                    "basis_quantity": basis_quantity,
                    "applied_fixed_rate": applied_fixed_rate,
                    "calculated_amount": calculated_amount,
                    "currency": "USD",
                    "status": IBCommissionObligation.ST_PENDING,
                },
            )
    except IntegrityError:
        obligation = IBCommissionObligation.objects.get(
            referral=referral,
            rule_type=IBCommissionRule.RULE_PER_LOT,
            source_event_type="per_lot_execution",
            source_event_id=lot_execution_event.pk,
        )

    return obligation
