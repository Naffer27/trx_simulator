"""
simulator/ib_commission.py
IB-COMMISSION-ENGINE-01 — rule resolution + PER_LOT commission calculation.

Approved design: IB-COMMISSION-RULES-DESIGN-01 /
IB-COMMISSION-RULES-DESIGN-01A / IB-PER-LOT-EXECUTION-EVENT-DESIGN-01.

Scope of this module:
  - resolve_applicable_rule() — the 3-tier precedence resolver
    (per-IB override -> global -> None), fail-closed on ambiguity.
  - generate_per_lot_obligation() — PER_LOT commission calculation from a
    single LotExecutionEvent (IB-COMMISSION-ENGINE-01).
  - generate_challenge_percent_obligation() — CHALLENGE_PERCENT
    commission from a single, deposit-backed ChallengeEnrollment
    (IB-COMMISSION-TRIGGERS-02A).
  - generate_deposit_percent_obligation() — DEPOSIT_PERCENT commission
    from a single credited, non-challenge Deposit
    (IB-COMMISSION-TRIGGERS-02A).
  - generate_trading_commission_revenue_share_obligation() —
    TRADING_COMMISSION_REVENUE_SHARE commission from a single
    BrokerLedger REV_COMMISSION row (IB-COMMISSION-TRIGGERS-02C).
    Consumes the already-calculated broker revenue amount directly —
    never recomputes qty/price/contract-size/commission-rate.
  - generate_spread_revenue_share_obligation() — SPREAD_REVENUE_SHARE
    commission from a single BrokerLedger REV_SPREAD row
    (IB-COMMISSION-PARITY-09C.1). Mirrors the TRADING_COMMISSION_
    REVENUE_SHARE generator's shape exactly — consumes
    broker_ledger.amount directly, never recomputes pips/bid/ask/
    contract-size. REV_SPREAD does not exist for every execution (see
    that function's own docstring) — no obligation is generated when it
    doesn't, never a fabricated/zero-basis one.

Every generate_*_obligation() function is idempotent (DB-constraint-
backed, never merely an in-process check) and creates an
IBCommissionObligation here NEVER moves money: no credit_wallet(), no
WalletTransaction, no LedgerEntry, no TreasuryOperationRequest. It is
purely a calculated PENDING liability record.

None of these functions is called automatically by any engine
(simulator/consumers.py, simulator/views.py, simulator/population_engine.py)
— they are invoked only by the additive sweep functions in
simulator/ib_commission_triggers.py, which read already-durable rows
those engines produce, unmodified. CPA_BONUS remains on HOLD /
POLICY_PENDING — no generator function exists yet for it.
"""
import logging
from decimal import ROUND_HALF_EVEN, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    BrokerLedger, ChallengeEnrollment, Deposit, IBCommissionObligation,
    IBCommissionRule, LotExecutionEvent, Referral, ReferralAttribution,
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


def _referral_is_active(referral: Referral) -> bool:
    """
    IB-RISK-HOLDS-07B — shared gate, called by all four generate_*_
    obligation() functions below (never from inside
    resolve_applicable_rule() itself — that function has a second,
    display-only caller, simulator/ib_admin_ops.py::ib_effective_rate(),
    that must keep showing an IB's configured rate even while frozen;
    injecting a freeze check there would silently break that display).

    Fail-closed per IB-RISK-HOLDS-07A section Q: only an EXACT match on
    Referral.RISK_ACTIVE passes. Any other value — including a future,
    not-yet-defined risk_status this function doesn't know about — is
    treated as inactive, never guessed past.
    """
    return referral.risk_status == Referral.RISK_ACTIVE


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
    if not _referral_is_active(referral):
        return None

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


def generate_challenge_percent_obligation(enrollment: ChallengeEnrollment):
    """
    IB-COMMISSION-TRIGGERS-02A. Calculate (never credit) the
    CHALLENGE_PERCENT IB commission owed for a single ChallengeEnrollment.
    Creates exactly one PENDING IBCommissionObligation, or returns None
    (no error) when there's nothing to generate:
      - the enrollment has no linked Deposit (deposit_id is None —
        OWNER POLICY locked for this block: admin/manual enrollments
        never generate a commission, only real deposit-backed purchases)
      - the enrolling user has no ReferralAttribution
      - no CHALLENGE_PERCENT rule resolves for this referral at this
        enrollment's time (enrolled_at)
      - the rule tier is ambiguous (fail-closed, logged)

    basis_amount is enrollment.product.price_usd (the catalog price of
    the challenge purchased). Idempotent — same DB-constraint-backed
    guarantee as generate_per_lot_obligation(). Never moves money.
    """
    if enrollment.deposit_id is None:
        return None

    user = enrollment.user
    if user is None:
        return None

    try:
        attribution = ReferralAttribution.objects.select_related("referral").get(
            referred_user=user,
        )
    except ReferralAttribution.DoesNotExist:
        return None

    referral = attribution.referral
    if not _referral_is_active(referral):
        return None

    try:
        rule = resolve_applicable_rule(
            referral, IBCommissionRule.RULE_CHALLENGE_PERCENT,
            at_time=enrollment.enrolled_at,
        )
    except AmbiguousCommissionRuleError as exc:
        logger.error(
            "[ib_commission] CHALLENGE_PERCENT rule resolution ambiguous for "
            "enrollment=%d referral=%d — refusing to generate an obligation: %s",
            enrollment.pk, referral.pk, exc,
        )
        return None

    if rule is None:
        return None

    basis_amount = enrollment.product.price_usd
    applied_percentage_rate = rule.percentage
    calculated_amount = _money_round(basis_amount * applied_percentage_rate / Decimal("100"))

    source_reference = (
        f"ChallengeEnrollment #{enrollment.pk} product={enrollment.product_id} "
        f"deposit={enrollment.deposit_id}"
    )

    try:
        with transaction.atomic():
            obligation, created = IBCommissionObligation.objects.get_or_create(
                referral=referral,
                rule_type=IBCommissionRule.RULE_CHALLENGE_PERCENT,
                source_event_type="challenge_enrollment",
                source_event_id=enrollment.pk,
                defaults={
                    "attribution": attribution,
                    "rule": rule,
                    "source_reference": source_reference,
                    "basis_amount": basis_amount,
                    "applied_percentage_rate": applied_percentage_rate,
                    "calculated_amount": calculated_amount,
                    "currency": "USD",
                    "status": IBCommissionObligation.ST_PENDING,
                },
            )
    except IntegrityError:
        obligation = IBCommissionObligation.objects.get(
            referral=referral,
            rule_type=IBCommissionRule.RULE_CHALLENGE_PERCENT,
            source_event_type="challenge_enrollment",
            source_event_id=enrollment.pk,
        )

    return obligation


def generate_deposit_percent_obligation(deposit: Deposit):
    """
    IB-COMMISSION-TRIGGERS-02A. Calculate (never credit) the
    DEPOSIT_PERCENT IB commission owed for a single credited, non-
    challenge Deposit. Creates exactly one PENDING IBCommissionObligation,
    or returns None (no error) when:
      - the deposit is not yet credited, or is a challenge-purchase
        deposit (challenge_product_id is not None — that's
        generate_challenge_percent_obligation()'s domain, not this one)
      - the depositing user has no ReferralAttribution
      - no DEPOSIT_PERCENT rule resolves for this referral at this
        deposit's credited time
      - the rule tier is ambiguous (fail-closed, logged)

    basis_amount is deposit.amount_usd — NEVER confirmed_amount_usd,
    which IB-COMMISSION-TRIGGERS-02 Phase A confirmed is an unused/dead
    field never written by the current deposit_callback implementation.

    Idempotent — same DB-constraint-backed guarantee as
    generate_per_lot_obligation(). Never moves money.
    """
    if not deposit.credited or deposit.challenge_product_id is not None:
        return None

    user = deposit.user
    if user is None:
        return None

    try:
        attribution = ReferralAttribution.objects.select_related("referral").get(
            referred_user=user,
        )
    except ReferralAttribution.DoesNotExist:
        return None

    referral = attribution.referral
    if not _referral_is_active(referral):
        return None
    at_time = deposit.credited_at or deposit.created_at

    try:
        rule = resolve_applicable_rule(
            referral, IBCommissionRule.RULE_DEPOSIT_PERCENT,
            at_time=at_time,
        )
    except AmbiguousCommissionRuleError as exc:
        logger.error(
            "[ib_commission] DEPOSIT_PERCENT rule resolution ambiguous for "
            "deposit=%d referral=%d — refusing to generate an obligation: %s",
            deposit.pk, referral.pk, exc,
        )
        return None

    if rule is None:
        return None

    basis_amount = deposit.amount_usd
    applied_percentage_rate = rule.percentage
    calculated_amount = _money_round(basis_amount * applied_percentage_rate / Decimal("100"))

    source_reference = f"Deposit #{deposit.pk} amount_usd={deposit.amount_usd}"

    try:
        with transaction.atomic():
            obligation, created = IBCommissionObligation.objects.get_or_create(
                referral=referral,
                rule_type=IBCommissionRule.RULE_DEPOSIT_PERCENT,
                source_event_type="deposit",
                source_event_id=deposit.pk,
                defaults={
                    "attribution": attribution,
                    "rule": rule,
                    "source_reference": source_reference,
                    "basis_amount": basis_amount,
                    "applied_percentage_rate": applied_percentage_rate,
                    "calculated_amount": calculated_amount,
                    "currency": "USD",
                    "status": IBCommissionObligation.ST_PENDING,
                },
            )
    except IntegrityError:
        obligation = IBCommissionObligation.objects.get(
            referral=referral,
            rule_type=IBCommissionRule.RULE_DEPOSIT_PERCENT,
            source_event_type="deposit",
            source_event_id=deposit.pk,
        )

    return obligation


def generate_trading_commission_revenue_share_obligation(broker_ledger: BrokerLedger):
    """
    IB-COMMISSION-TRIGGERS-02C. Calculate (never credit) the
    TRADING_COMMISSION_REVENUE_SHARE IB commission owed for a single
    BrokerLedger REV_COMMISSION row. Creates exactly one PENDING
    IBCommissionObligation, or returns None (no error) when:
      - the row's revenue_type is not REV_COMMISSION (e.g. REV_SPREAD —
        that is a separate, on-HOLD rule type, not this one)
      - amount is not positive (defensive — real REV_COMMISSION rows are
        always created with amount > 0 by consumers.py, but BrokerLedger
        itself has no DB-level CheckConstraint enforcing that, unlike
        IBCommissionRule; this generator does not trust the absence of a
        schema guarantee it does not itself own)
      - source_account_id is None (defensive — schema-legal via SET_NULL
        on TradingAccount delete, never hit by a live REV_COMMISSION row
        in practice)
      - the executing account has no user, or that user has no
        ReferralAttribution
      - no TRADING_COMMISSION_REVENUE_SHARE rule resolves for this
        referral at this row's created_at
      - the rule tier is ambiguous (fail-closed, logged)

    basis_amount is broker_ledger.amount — the broker's own,
    already-calculated commission revenue for this execution. This
    function NEVER recomputes qty/price/contract_size/commission-rate;
    IB-COMMISSION-TRIGGERS-02C's authorized architecture is to consume
    the durable broker revenue record, not re-derive it. Idempotent —
    same DB-constraint-backed guarantee as the other three generators in
    this module. Never moves money.
    """
    if broker_ledger.revenue_type != BrokerLedger.REV_COMMISSION:
        return None

    if broker_ledger.amount is None or broker_ledger.amount <= 0:
        return None

    if broker_ledger.source_account_id is None:
        return None

    user = broker_ledger.source_account.user
    if user is None:
        return None

    try:
        attribution = ReferralAttribution.objects.select_related("referral").get(
            referred_user=user,
        )
    except ReferralAttribution.DoesNotExist:
        return None

    referral = attribution.referral
    if not _referral_is_active(referral):
        return None

    try:
        rule = resolve_applicable_rule(
            referral, IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            at_time=broker_ledger.created_at,
        )
    except AmbiguousCommissionRuleError as exc:
        logger.error(
            "[ib_commission] TRADING_COMMISSION_REVENUE_SHARE rule resolution "
            "ambiguous for broker_ledger=%d referral=%d — refusing to generate "
            "an obligation: %s",
            broker_ledger.pk, referral.pk, exc,
        )
        return None

    if rule is None:
        return None

    basis_amount = broker_ledger.amount
    applied_percentage_rate = rule.percentage
    calculated_amount = _money_round(basis_amount * applied_percentage_rate / Decimal("100"))

    source_reference = (
        f"BrokerLedger #{broker_ledger.pk} REV_COMMISSION "
        f"account={broker_ledger.source_account_id} amount={broker_ledger.amount}"
    )

    try:
        with transaction.atomic():
            obligation, created = IBCommissionObligation.objects.get_or_create(
                referral=referral,
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
                source_event_type="broker_ledger_commission",
                source_event_id=broker_ledger.pk,
                defaults={
                    "attribution": attribution,
                    "rule": rule,
                    "source_reference": source_reference,
                    "basis_amount": basis_amount,
                    "applied_percentage_rate": applied_percentage_rate,
                    "calculated_amount": calculated_amount,
                    "currency": "USD",
                    "status": IBCommissionObligation.ST_PENDING,
                },
            )
    except IntegrityError:
        obligation = IBCommissionObligation.objects.get(
            referral=referral,
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            source_event_type="broker_ledger_commission",
            source_event_id=broker_ledger.pk,
        )

    return obligation


def generate_spread_revenue_share_obligation(broker_ledger: BrokerLedger):
    """
    IB-COMMISSION-PARITY-09C.1. Calculate (never credit) the
    SPREAD_REVENUE_SHARE IB commission owed for a single BrokerLedger
    REV_SPREAD row. Creates exactly one PENDING IBCommissionObligation,
    or returns None (no error) when:
      - the row's revenue_type is not REV_SPREAD (e.g. REV_COMMISSION —
        that is generate_trading_commission_revenue_share_obligation()'s
        domain, not this one)
      - amount is not positive (defensive — real REV_SPREAD rows are
        always created with amount > 0 by consumers.py, but BrokerLedger
        itself has no DB-level CheckConstraint enforcing that, unlike
        IBCommissionRule; this generator does not trust the absence of a
        schema guarantee it does not itself own)
      - source_account_id is None (defensive — schema-legal via SET_NULL
        on TradingAccount delete, never hit by a live REV_SPREAD row in
        practice)
      - the executing account has no user, or that user has no
        ReferralAttribution
      - no SPREAD_REVENUE_SHARE rule resolves for this referral at this
        row's created_at
      - the rule tier is ambiguous (fail-closed, logged)

    basis_amount is broker_ledger.amount — the broker's own,
    already-captured spread revenue for this execution (IB-COMMISSION-
    PARITY-09C's certified SSOT: REV_SPREAD.amount is written verbatim
    from the same Decimal the trader was charged via LedgerEntry(EV_FEE)
    in the same statement, in simulator/consumers.py — see that
    module's own O.6c-1aa comment). This function NEVER recomputes
    qty/price/contract_size/pips/bid/ask; it consumes the durable
    broker revenue record exactly as generate_trading_commission_
    revenue_share_obligation() already does for REV_COMMISSION —
    mirrors that function's shape line-for-line, not a second
    architecture. Idempotent — same DB-constraint-backed guarantee as
    the other generators in this module. Never moves money.

    IB-COMMISSION-PARITY-09C's certified, deliberate consequence: the
    pending/stop/limit-trigger execution path never writes REV_SPREAD
    at all (no live pricing data to compute a markup from at trigger
    time — see simulator/consumers.py::_trigger_pending_order_core's
    own docstring) — this generator is never even called for that path,
    since no REV_SPREAD row exists to sweep. This is correct, existing,
    unmodified engine behavior, not a gap this block closes.
    """
    if broker_ledger.revenue_type != BrokerLedger.REV_SPREAD:
        return None

    if broker_ledger.amount is None or broker_ledger.amount <= 0:
        return None

    if broker_ledger.source_account_id is None:
        return None

    user = broker_ledger.source_account.user
    if user is None:
        return None

    try:
        attribution = ReferralAttribution.objects.select_related("referral").get(
            referred_user=user,
        )
    except ReferralAttribution.DoesNotExist:
        return None

    referral = attribution.referral
    if not _referral_is_active(referral):
        return None

    try:
        rule = resolve_applicable_rule(
            referral, IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            at_time=broker_ledger.created_at,
        )
    except AmbiguousCommissionRuleError as exc:
        logger.error(
            "[ib_commission] SPREAD_REVENUE_SHARE rule resolution "
            "ambiguous for broker_ledger=%d referral=%d — refusing to generate "
            "an obligation: %s",
            broker_ledger.pk, referral.pk, exc,
        )
        return None

    if rule is None:
        return None

    basis_amount = broker_ledger.amount
    applied_percentage_rate = rule.percentage
    calculated_amount = _money_round(basis_amount * applied_percentage_rate / Decimal("100"))

    source_reference = (
        f"BrokerLedger #{broker_ledger.pk} REV_SPREAD "
        f"account={broker_ledger.source_account_id} amount={broker_ledger.amount}"
    )

    try:
        with transaction.atomic():
            obligation, created = IBCommissionObligation.objects.get_or_create(
                referral=referral,
                rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
                source_event_type="broker_ledger_spread",
                source_event_id=broker_ledger.pk,
                defaults={
                    "attribution": attribution,
                    "rule": rule,
                    "source_reference": source_reference,
                    "basis_amount": basis_amount,
                    "applied_percentage_rate": applied_percentage_rate,
                    "calculated_amount": calculated_amount,
                    "currency": "USD",
                    "status": IBCommissionObligation.ST_PENDING,
                },
            )
    except IntegrityError:
        obligation = IBCommissionObligation.objects.get(
            referral=referral,
            rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            source_event_type="broker_ledger_spread",
            source_event_id=broker_ledger.pk,
        )

    return obligation
