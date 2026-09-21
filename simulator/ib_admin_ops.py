# simulator/ib_admin_ops.py
"""
IB-ADMIN-OPS-04B — IB operations / control-plane admin.

Approved design: IB-ADMIN-OPS-04A audit + design lock. Implements the
Django-admin-extension architecture identified there as the only
proven pattern for "staff views and operates a financial state
machine" in this codebase — this module is structurally the same shape
as TreasuryOperationRequestAdmin (simulator/admin.py): ModelAdmin
subclasses whose get_urls() add custom staff views, each rendering a
template under simulator/templates/admin/ and calling an authoritative
service function. No business logic is reimplemented here.

CORE PRINCIPLE (locked, IB-ADMIN-OPS-04 request): this module is an
OPERATIONS / CONTROL PLANE. It observes and operates existing
authoritative systems — it is not a second economic engine. It never
independently calculates trading P/L, execution prices, spread, trader
commission, lots (always summed from the already-durable
LotExecutionEvent.qty, never re-derived from price/qty/contract-size),
IB payout (always the already-generated IBCommissionObligation.
calculated_amount, never recomputed from IBCommissionRule), wallet
balances, or Treasury accounting.

Source-of-truth map (locked, IB-ADMIN-OPS-04A section R) — every
metric this module displays has exactly one authoritative source, read
here, never recomputed:
    IB identity        -> Referral
    Attribution         -> ReferralAttribution
    Lots                -> LotExecutionEvent.qty (summed)
    Commission rate     -> IBCommissionRule, via the unmodified
                           ib_commission.resolve_applicable_rule()
    Commission owed      -> IBCommissionObligation.calculated_amount
    Treasury state       -> TreasuryOperationRequest.status
    Wallet balance        -> Wallet.available_balance
    Wallet credit history -> WalletTransaction

Money/state-mutating actions in this module call ONLY the four already
-shipped, unmodified IB-TREASURY-CREDIT-03 services (simulator/
ib_treasury_settlement.py): approve_obligation(), link_treasury_
request(), sync_obligation_from_treasury(), reconcile_approved_
obligations(). This module never writes to IBCommissionObligation,
TreasuryOperationRequest, Wallet, or WalletTransaction directly — every
state transition is delegated.

Treasury boundary (locked): IB Ops may VIEW/LINK/NAVIGATE TO a
TreasuryOperationRequest. It never approves, executes, rejects, or
cancels one, and never grants (or checks for) can_execute_treasury_
request anywhere in this module — that authority remains exclusively
Treasury's own admin (simulator/admin.py::TreasuryOperationRequestAdmin).

Wallet boundary (locked): read-only. No balance editing, no manual
credit/debit, no WalletTransaction creation from this module — the
only wallet-touching call anywhere in this file is a plain read
(Wallet.objects.filter(...)) or, inside link_treasury_request() itself
(unmodified, called not reimplemented), the already-existing
get_or_create_wallet() auto-creation.

Auditability (IB-ADMIN-OPS-04A finding, closed here): approve_
obligation()/link_treasury_request()/sync_obligation_from_treasury()
only log via Python's logging module — none of them write to the
project's queryable audit systems (AuditLog / BrokerAuditEvent). That
gap is closed here, at this orchestration layer, using the EXISTING
audit.log_audit() / broker_audit.record_payment_event() convention —
no protected settlement/Treasury file is modified, no new audit-log
model is created (no second economic ledger), and no monetary
calculation is touched. Every admin view that transitions obligation
state calls both, mirroring exactly the dual-write convention every
treasury_requests.py service already uses for its own transitions.

Bulk approval is explicitly NOT implemented in V1 (Owner decision 5) —
approve_obligation()'s raise-on-repeat semantics make a naive bulk
action unsafe without per-row exception handling this block does not
build; only a bulk RECONCILIATION action exists (reconcile_approved_
obligations() is already idempotent by design, safe to run bulk/repeatedly).

Reversals/fraud boundary (locked, deferred to IB-REVERSALS-FRAUD-05):
this module never deletes an obligation, edits calculated_amount,
auto-reverses anything, debits a wallet, or replaces a Treasury
request. A stuck APPROVED obligation whose linked Treasury request
reached a terminal non-success state (REJECTED/CANCELLED/FAILED) is
only ever DISPLAYED here (a "needs attention" indicator), never acted
on beyond that display.

CPA_BONUS and SPREAD_REVENUE_SHARE remain HOLD — neither is
implemented, referenced, or branched on anywhere in this module.
"""
import logging
from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    Count, DecimalField, IntegerField, OuterRef, Q, Subquery, Sum,
)
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone

from . import audit, broker_audit
from .ib_commission import AmbiguousCommissionRuleError, resolve_applicable_rule
from .ib_risk_holds import (
    ObligationAlreadyHeld, ObligationNotHeld, ReferralAlreadyFrozen, ReferralNotFrozen,
    freeze_referral, hold_obligation, release_obligation, unfreeze_referral,
)
from .ib_treasury_settlement import (
    ObligationHeld, ObligationInvalidAmount, ObligationNotApproved, ObligationNotPending,
    ReferralNotActive, approve_obligation, link_treasury_request,
    reconcile_approved_obligations, sync_obligation_from_treasury,
)
from .models import (
    IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule, IBRiskEvent,
    LotExecutionEvent, Referral, ReferralAttribution, TradingAccount,
    TreasuryOperationRequest, Wallet, WalletTransaction,
)
from .treasury_requests import TREASURY_REVIEW_PERMISSION, TREASURY_SUBMIT_PERMISSION

logger = logging.getLogger("simulator.ib_admin_ops")

IB_DIRECTORY_PAGE_SIZE = 25
_RECENT_LIMIT = 10

_MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
_LOT_FIELD = DecimalField(max_digits=18, decimal_places=6)


# ─────────────────────────────────────────────
# Query / aggregation helpers — pure reads, batched, never per-row loops.
# ─────────────────────────────────────────────

def _period_starts(at_time=None):
    at_time = at_time or timezone.now()
    today_start = at_time.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timezone.timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    return today_start, week_start, month_start


def _obligation_sum_subquery(status, referral_field="referral"):
    """Correlated subquery: SUM(calculated_amount) for one status, scoped
    to the outer Referral row — avoids the classic Django multi-annotate
    JOIN fan-out bug (combining several Sum()/Count() over different
    to-many relations in a single annotate() call silently multiplies
    rows). One subquery per metric keeps every aggregate independently
    correct while still costing a single overall query, not one per IB
    row — this is the batching IB-ADMIN-OPS-04A's own N+1 finding
    requires."""
    qs = (
        IBCommissionObligation.objects.filter(
            **{referral_field: OuterRef("pk")}, status=status,
        )
        .order_by()
        .values(referral_field)
        .annotate(total=Sum("calculated_amount"))
        .values("total")
    )
    return Coalesce(Subquery(qs, output_field=_MONEY_FIELD), Decimal("0.00"))


def _obligation_count_subquery(status, referral_field="referral"):
    qs = (
        IBCommissionObligation.objects.filter(
            **{referral_field: OuterRef("pk")}, status=status,
        )
        .order_by()
        .values(referral_field)
        .annotate(total=Count("id"))
        .values("total")
    )
    return Coalesce(Subquery(qs, output_field=IntegerField()), 0)


def _lot_sum_subquery(since=None):
    filters = Q(
        account__user__referral_attribution__referral=OuterRef("pk"),
    )
    if since is not None:
        filters &= Q(created_at__gte=since)
    qs = (
        LotExecutionEvent.objects.filter(filters)
        .order_by()
        .values("account__user__referral_attribution__referral")
        .annotate(total=Sum("qty"))
        .values("total")
    )
    return Coalesce(Subquery(qs, output_field=_LOT_FIELD), Decimal("0"))


def ib_directory_queryset():
    """
    One Referral queryset, annotated with every directory-list metric
    via correlated subqueries — the batched, non-N+1 design
    IB-ADMIN-OPS-04A's own performance finding requires. Effective
    PER_LOT rate is deliberately NOT annotated here (see
    ib_directory_view() — it is resolved per visible row, on the
    already-paginated page, via the real, unmodified
    resolve_applicable_rule(), so it can never silently diverge from
    the authoritative resolver; the cost is bounded by page size, not
    total IB count).
    """
    client_count_sq = (
        ReferralAttribution.objects.filter(referral=OuterRef("pk"))
        .order_by().values("referral").annotate(total=Count("id")).values("total")
    )
    wallet_balance_sq = Wallet.objects.filter(user=OuterRef("user_id")).values("available_balance")[:1]

    _, _, month_start = _period_starts()

    return Referral.objects.select_related("user").annotate(
        client_count=Coalesce(Subquery(client_count_sq, output_field=IntegerField()), 0),
        month_lots=_lot_sum_subquery(since=month_start),
        pending_amount=_obligation_sum_subquery(IBCommissionObligation.ST_PENDING),
        approved_amount=_obligation_sum_subquery(IBCommissionObligation.ST_APPROVED),
        credited_amount=_obligation_sum_subquery(IBCommissionObligation.ST_CREDITED),
        wallet_balance=Coalesce(
            Subquery(wallet_balance_sq, output_field=_MONEY_FIELD), Decimal("0.00"),
        ),
    ).order_by("-created_at")


def ib_lot_totals(referral, at_time=None):
    """TODAY/WEEK/MONTH/LIFETIME lot volume for one IB — pure SUM over
    the already-durable LotExecutionEvent.qty, never re-derived from
    price/qty/contract-size/Trade/Position. Single-IB detail-page scope
    (not the directory list), so four small aggregate queries here is
    not an N+1 concern."""
    today_start, week_start, month_start = _period_starts(at_time)
    base = LotExecutionEvent.objects.filter(
        account__user__referral_attribution__referral=referral,
    )
    return {
        "today": base.filter(created_at__gte=today_start).aggregate(
            total=Coalesce(Sum("qty"), Decimal("0"), output_field=_LOT_FIELD),
        )["total"],
        "week": base.filter(created_at__gte=week_start).aggregate(
            total=Coalesce(Sum("qty"), Decimal("0"), output_field=_LOT_FIELD),
        )["total"],
        "month": base.filter(created_at__gte=month_start).aggregate(
            total=Coalesce(Sum("qty"), Decimal("0"), output_field=_LOT_FIELD),
        )["total"],
        "lifetime": base.aggregate(
            total=Coalesce(Sum("qty"), Decimal("0"), output_field=_LOT_FIELD),
        )["total"],
    }


def ib_active_trading_client_count(referral, since):
    """Distinct referred users with at least one LotExecutionEvent since
    `since` — pure aggregation over stored rows."""
    return (
        LotExecutionEvent.objects.filter(
            account__user__referral_attribution__referral=referral,
            created_at__gte=since,
        )
        .values("account__user").distinct().count()
    )


def ib_obligation_totals(referral):
    qs = IBCommissionObligation.objects.filter(referral=referral)
    totals = {}
    for status in (
        IBCommissionObligation.ST_PENDING, IBCommissionObligation.ST_APPROVED,
        IBCommissionObligation.ST_CREDITED,
    ):
        agg = qs.filter(status=status).aggregate(
            amount=Coalesce(Sum("calculated_amount"), Decimal("0.00"), output_field=_MONEY_FIELD),
            count=Count("id"),
        )
        totals[status] = agg
    return totals


def ib_effective_rate(referral, rule_type, at_time=None):
    """The single authoritative rate lookup — calls the real, unmodified
    resolve_applicable_rule() directly, never a re-implementation of its
    3-tier precedence. Returns (rule_or_None, source_label)."""
    try:
        rule = resolve_applicable_rule(referral, rule_type, at_time=at_time)
    except AmbiguousCommissionRuleError:
        return None, "ambiguous"
    if rule is None:
        return None, "none"
    return rule, ("per-IB" if rule.referral_id == referral.pk else "global")


# ─────────────────────────────────────────────
# IB-PORTAL-08B — single shared commission-summary aggregate, reused by
# BOTH the IB-facing portal (/associates/) and the Risk Desk (ib_detail_
# view) — one query shape, never duplicated. All four IBCommissionObl-
# igation figures computed in ONE aggregate() call via filtered Sum()s
# (Django conditional aggregates), so a 100k+-obligation IB never loads
# more than one DB-side aggregated row into Python — no per-obligation
# Python loop anywhere in this function.
#
# Semantics (locked, IB-PORTAL-08A section H / this block's section 5):
#   generated = SUM(calculated_amount) over ALL obligations for this
#               referral (CANCELLED/REVERSED are never written by any
#               code path today — see IBCommissionObligation.STATUS_
#               CHOICES' own docstring history — so this is currently
#               exactly PENDING+APPROVED+CREDITED, i.e.
#               generated == pending + credited, no double counting).
#   pending   = SUM(calculated_amount) where status IN (PENDING, APPROVED)
#   held      = SUM(calculated_amount) where is_held=True — a SUBSET of
#               pending (is_held is orthogonal to status, per IB-RISK-
#               HOLDS-07A section G), an informational OVERLAY/breakdown
#               of pending, never summed alongside it as a fifth bucket.
#   credited  = SUM(calculated_amount) where status=CREDITED
#   reversed  = SUM(amount) over EXECUTED IBCommissionAdjustment rows —
#               the only adjustment status where money has actually
#               moved (see ib_commission_reversal.py's own status
#               docstring) — displayed SEPARATELY, never netted against
#               credited (a CREDITED obligation's own calculated_amount/
#               credited_at never mutate, per IB-TREASURY-CREDIT-03's
#               locked immutability guarantee).
# ─────────────────────────────────────────────

def ib_commission_summary(referral):
    obligation_totals = IBCommissionObligation.objects.filter(referral=referral).aggregate(
        generated=Coalesce(Sum("calculated_amount"), Decimal("0.00"), output_field=_MONEY_FIELD),
        pending=Coalesce(
            Sum(
                "calculated_amount",
                filter=Q(status__in=[IBCommissionObligation.ST_PENDING, IBCommissionObligation.ST_APPROVED]),
            ),
            Decimal("0.00"), output_field=_MONEY_FIELD,
        ),
        held=Coalesce(
            Sum("calculated_amount", filter=Q(is_held=True)),
            Decimal("0.00"), output_field=_MONEY_FIELD,
        ),
        credited=Coalesce(
            Sum("calculated_amount", filter=Q(status=IBCommissionObligation.ST_CREDITED)),
            Decimal("0.00"), output_field=_MONEY_FIELD,
        ),
    )
    reversed_total = IBCommissionAdjustment.objects.filter(
        referral=referral, status=IBCommissionAdjustment.ST_EXECUTED,
    ).aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"), output_field=_MONEY_FIELD),
    )["total"]
    obligation_totals["reversed"] = reversed_total
    return obligation_totals


# IB-PORTAL-08B section 8 — safe, non-misleading status wording. Never
# says "Pagada"/"Paid" for anything short of CREDITED — that is the
# only status where IB-TREASURY-CREDIT-03's own pipeline has already
# observed a real, completed wallet credit (sync_obligation_from_
# treasury() only ever sets CREDITED after Treasury's own execution
# reached EXECUTED). A Treasury terminal failure while APPROVED is
# reported as "En revisión" — the existing ib_needs_attention() flag
# already surfaces this internally for staff; the IB never sees a raw
# Treasury status string.
def ib_obligation_status_label(obligation):
    if obligation.status == IBCommissionObligation.ST_CREDITED:
        return "Acreditada"
    if obligation.status == IBCommissionObligation.ST_PENDING:
        return "Pendiente de revisión"
    if obligation.status == IBCommissionObligation.ST_APPROVED:
        if obligation.treasury_operation_id is None:
            return "Aprobada"
        if ib_needs_attention(obligation):
            return "En revisión"
        return "En proceso"
    # CANCELLED / REVERSED — never written by any code path today, kept
    # only for STATUS_CHOICES completeness; labeled honestly if it ever
    # occurs rather than silently falling through.
    return obligation.get_status_display()


# ─────────────────────────────────────────────
# IB-PORTAL-08B section 11/12/13 — Change Commission Rate.
#
# Reuses IBCommissionRule's OWN existing invariants (IB-COMMISSION-
# RULES-DESIGN-01, unmodified) rather than inventing a new scheme:
#   - a rate CHANGE is a NEW row (effective_from=now), never an edit of
#     an existing row's economic fields (IBCommissionRuleAdmin already
#     enforces this at the UI layer via get_readonly_fields(); this
#     service enforces it structurally — it only ever .create()s a new
#     row and .save(update_fields=["effective_until"])s an old one,
#     never touches fixed_amount/percentage/rule_type/referral on an
#     existing row).
#   - the DB UniqueConstraint "ibrule_one_open_ended_per_type_scope"
#     (at most one open-ended rule per (rule_type, referral)) is
#     satisfied by always closing the currently-open row (setting its
#     effective_until=now) in the SAME transaction, BEFORE creating the
#     new open-ended row — never the other way around, so the
#     constraint is never even transiently violated.
#   - "ibrule_effective_until_after_from" is satisfied because the
#     closed row's own effective_from was necessarily set earlier than
#     `now` (it already existed).
#
# Section 12 (CRITICAL): only ever queries/closes a rule scoped to
# `referral=referral` (never referral=None). If the IB was previously
# falling back to the GLOBAL rule (no per-IB override existed yet),
# `open_rule` below is simply None — the global row is never touched,
# and a brand-new per-IB override row is created. The global rule (and
# every other IB relying on it) is structurally unreachable from this
# function.
#
# Section 13 (concurrency): serializes concurrent rate-change calls for
# the SAME referral by locking the Referral row first (select_for_
# update()) — the same lock-then-act discipline IB-RISK-HOLDS-07B
# already established in ib_treasury_settlement.py's approve_
# obligation()/link_treasury_request() (lock order: Referral, then any
# IBCommissionRule row for that referral). Two different referrals never
# block each other. A second POST for the same referral blocks until
# the first transaction commits (its close+create pair), then sees the
# already-closed row and no open one to re-close — never creates two
# ambiguous open rows.
# ─────────────────────────────────────────────

class InvalidCommissionRate(Exception):
    """Raised by change_commission_rate() when the requested new value is
    out of range for its rule_type's field (fixed_amount must be > 0;
    percentage must be within [0, 100]), or rule_type is not one of
    IBCommissionRule.FIXED_AMOUNT_RULE_TYPES/PERCENTAGE_RULE_TYPES."""


class CommissionRateUnchanged(Exception):
    """Raised by change_commission_rate() when the requested new value
    equals the currently open rate for this referral — a no-op guard
    against pointless rate-history churn from a duplicate submit."""


def change_commission_rate(referral, new_value, *, request,
                            rule_type=IBCommissionRule.RULE_PER_LOT):
    """
    Close the IB-specific open-ended IBCommissionRule for `rule_type`
    (if any) and open a new one at the new value, effective now. Never
    touches the global (referral=None) rule. Never touches any
    IBCommissionObligation — historical calculated_amount/
    applied_fixed_rate/applied_percentage_rate remain exactly as
    generated, permanently (IB-TREASURY-CREDIT-03's snapshot guarantee,
    reused unmodified, not re-implemented here).

    IB-COMMISSION-PARITY-09B.1 — generalized (same function, no second
    service) to dispatch on IBCommissionRule's OWN existing
    FIXED_AMOUNT_RULE_TYPES / PERCENTAGE_RULE_TYPES classification
    (reused, not reinvented) so this now correctly supports every
    already-defined rule_type, not just RULE_PER_LOT:
      - FIXED_AMOUNT_RULE_TYPES (PER_LOT, CPA_BONUS): new_value
        populates fixed_amount (percentage=None); must be > 0.
      - PERCENTAGE_RULE_TYPES (CHALLENGE_PERCENT, DEPOSIT_PERCENT,
        SPREAD_REVENUE_SHARE, TRADING_COMMISSION_REVENUE_SHARE):
        new_value populates percentage (fixed_amount=None); must be in
        [0, 100] inclusive — 0 is a valid, real contract (zero payout,
        distinct from NULL/"no rule"); >100 is structurally impossible
        (mirrors the DB-level "ibrule_percentage_lte_100" CheckConstraint,
        IB-COMMISSION-PARITY-09B.1 — this is defense-in-depth on top of
        that constraint, not a substitute for it).
    CPA_BONUS/SPREAD_REVENUE_SHARE remain HOLD unchanged by this
    generalization — no caller in this codebase passes either rule_type
    here today; this function's genericity mirrors resolve_applicable_
    rule()'s own pre-existing genericity, not a new activation.

    Args:
        referral:  a Referral — only its .pk is used; re-read under
                   select_for_update().
        new_value: the new fixed_amount or percentage value, depending
                   on rule_type — a positive Decimal (fixed_amount) or a
                   Decimal in [0, 100] (percentage), or anything
                   Decimal()-constructible.
        request:   the current HttpRequest — request.user must be
                   authenticated and hold TREASURY_REVIEW_PERMISSION
                   (same permission freeze_referral()/hold_obligation()
                   already gate on — no new permission invented).
        rule_type: defaults to RULE_PER_LOT.

    Returns:
        The newly created, open-ended IBCommissionRule.

    Raises:
        PermissionDenied:        request.user not authenticated, or
                                  lacks TREASURY_REVIEW_PERMISSION.
        InvalidCommissionRate:   new_value is out of range for its
                                  rule_type's field, or rule_type is
                                  unsupported.
        CommissionRateUnchanged: new_value equals the currently open
                                  per-IB rate.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to change an IB commission rate.")
    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    if rule_type in IBCommissionRule.FIXED_AMOUNT_RULE_TYPES:
        field_name = "fixed_amount"
    elif rule_type in IBCommissionRule.PERCENTAGE_RULE_TYPES:
        field_name = "percentage"
    else:
        raise InvalidCommissionRate(f"Unsupported rule_type for rate change: {rule_type!r}")

    try:
        new_value = Decimal(new_value)
    except Exception as exc:
        raise InvalidCommissionRate(f"'{new_value}' is not a valid decimal amount.") from exc

    if field_name == "fixed_amount":
        if new_value <= 0:
            raise InvalidCommissionRate("The new rate must be greater than 0.")
    else:
        if new_value < 0 or new_value > 100:
            raise InvalidCommissionRate("The new percentage must be between 0 and 100.")

    with transaction.atomic():
        referral_locked = Referral.objects.select_for_update().get(pk=referral.pk)

        open_rule = (
            IBCommissionRule.objects.select_for_update()
            .filter(rule_type=rule_type, referral=referral_locked, effective_until__isnull=True)
            .first()
        )

        if open_rule is not None and getattr(open_rule, field_name) == new_value:
            raise CommissionRateUnchanged(
                f"Referral #{referral_locked.pk} already has an open {rule_type} rule "
                f"at {new_value} — nothing to change."
            )

        now = timezone.now()

        if open_rule is not None:
            open_rule.effective_until = now
            open_rule.save(update_fields=["effective_until"])

        create_kwargs = dict(
            rule_type=rule_type, referral=referral_locked, enabled=True,
            effective_from=now, effective_until=None, created_by=request.user,
        )
        create_kwargs["fixed_amount"] = new_value if field_name == "fixed_amount" else None
        create_kwargs["percentage"] = new_value if field_name == "percentage" else None

        new_rule = IBCommissionRule.objects.create(**create_kwargs)
    # ── transaction closed — old row closed + new row opened, committed together ──

    logger.info(
        "[ib_admin_ops] referral=%d %s %s changed to %s by user=%d (previous open rule=%s)",
        referral_locked.pk, rule_type, field_name, new_value, request.user.pk,
        open_rule.pk if open_rule is not None else None,
    )
    return new_rule


def ib_needs_attention(obligation):
    """True when an obligation is stuck APPROVED with a linked Treasury
    request that reached a terminal non-success state — display only,
    per the locked reversals/fraud boundary (no action offered here,
    deferred to IB-REVERSALS-FRAUD-05)."""
    if obligation.status != IBCommissionObligation.ST_APPROVED:
        return False
    if obligation.treasury_operation_id is None:
        return False
    return obligation.treasury_operation.status in (
        TreasuryOperationRequest.ST_REJECTED,
        TreasuryOperationRequest.ST_CANCELLED,
        TreasuryOperationRequest.ST_FAILED,
    )


# ─────────────────────────────────────────────
# Audit helper — closes the IB-ADMIN-OPS-04A auditability gap at this
# orchestration layer only, reusing the existing dual-write convention.
# Never called from inside a protected settlement/Treasury service —
# only from the admin views below, after those services have already
# completed their own transition.
# ─────────────────────────────────────────────

def _record_ib_admin_event(request, event_type, description, *, obligation, extra=None):
    metadata = {
        "obligation_id": obligation.pk,
        "referral_id": obligation.referral_id,
        "rule_type": obligation.rule_type,
        "status": obligation.status,
        "calculated_amount": str(obligation.calculated_amount),
        "treasury_operation_id": obligation.treasury_operation_id,
    }
    if extra:
        metadata.update(extra)

    audit.log_audit(
        request, event_type, description,
        detail=metadata,
    )
    broker_audit.record_payment_event(
        event_type=event_type,
        severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=description,
        source_module="simulator.ib_admin_ops",
        metadata=metadata,
    )


def _record_ib_risk_event(request, event_type, description, *, referral, obligation=None, reason, extra=None):
    """
    IB-RISK-HOLDS-07B — same dual-write convention as
    _record_ib_admin_event() above (closes the same auditability gap,
    at the same orchestration layer, for freeze/unfreeze/hold/release).
    Called AFTER simulator/ib_risk_holds.py's own service function has
    already committed the transition (and its own IBRiskEvent row) —
    this only adds the two general-purpose, queryable audit trails
    (AuditLog / BrokerAuditEvent) on top, exactly like every other
    admin-triggered IB transition in this module already does.
    """
    metadata = {
        "referral_id": referral.pk,
        "risk_status": referral.risk_status,
        "reason": reason,
    }
    if obligation is not None:
        metadata["obligation_id"] = obligation.pk
        metadata["is_held"] = obligation.is_held
    if extra:
        metadata.update(extra)

    audit.log_audit(
        request, event_type, description,
        detail=metadata,
    )
    broker_audit.record_payment_event(
        event_type=event_type,
        severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=description,
        source_module="simulator.ib_admin_ops",
        metadata=metadata,
    )


# ─────────────────────────────────────────────
# IBCommissionObligationAdmin — hub of IB Ops: directory, detail,
# approve, link-to-Treasury, sync, reconcile.
# ─────────────────────────────────────────────

@admin.register(IBCommissionObligation)
class IBCommissionObligationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "referral", "rule_type", "calculated_amount", "status", "is_held",
        "source_event_type", "source_event_id", "created_at", "approved_by",
    )
    list_filter = ("rule_type", "status", "is_held", "created_at")
    search_fields = ("referral__code", "referral__user__username", "source_reference")
    readonly_fields = [f.name for f in IBCommissionObligation._meta.fields]
    ordering = ("-created_at", "-id")

    # Historical economic facts — never manually add/change/delete via
    # admin. Every state transition flows exclusively through the
    # authoritative services, called from the custom views below.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if request.user.has_perm("simulator.view_ibcommissionobligation"):
            return True
        if request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            return True
        if request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            return True
        return super().has_view_permission(request, obj)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        if self.has_view_permission(request):
            extra_context["ib_directory_url"] = reverse("admin:ib_directory")
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """
        Injects Approve / Link-to-Treasury buttons on an obligation's own
        detail page — same discipline as TreasuryOperationRequestAdmin.
        change_view(): read-only visibility lookup only, no state change
        happens in this method; the transitions themselves live entirely
        in ib_obligation_approve_view()/ib_obligation_link_treasury_view()
        below, which call approve_obligation()/link_treasury_request()
        (IB-TREASURY-CREDIT-03) unmodified.
        """
        extra_context = extra_context or {}
        instance = IBCommissionObligation.objects.filter(pk=object_id).first()
        if instance is not None and request.user.is_authenticated:
            if (
                instance.status == IBCommissionObligation.ST_PENDING
                and request.user.has_perm(TREASURY_REVIEW_PERMISSION)
            ):
                extra_context["show_ib_approve_button"] = True
                extra_context["ib_approve_url"] = reverse(
                    "admin:ib_obligation_approve", args=[instance.pk],
                )
            if (
                instance.status == IBCommissionObligation.ST_APPROVED
                and instance.treasury_operation_id is None
                and request.user.has_perm(TREASURY_SUBMIT_PERMISSION)
            ):
                extra_context["show_ib_link_button"] = True
                extra_context["ib_link_url"] = reverse(
                    "admin:ib_obligation_link_treasury", args=[instance.pk],
                )
            if (
                instance.status == IBCommissionObligation.ST_APPROVED
                and instance.treasury_operation_id is not None
            ):
                extra_context["show_ib_sync_button"] = True
                extra_context["ib_sync_url"] = reverse(
                    "admin:ib_obligation_sync", args=[instance.pk],
                )
            extra_context["ib_needs_attention"] = ib_needs_attention(instance)

            # IB-RISK-HOLDS-07B — Hold / Release buttons. Orthogonal to
            # status (a hold can be placed/lifted regardless of the
            # obligation's lifecycle stage — see ib_risk_holds.py's own
            # docstring), so shown unconditionally rather than gated on
            # instance.status like the buttons above.
            if request.user.has_perm(TREASURY_REVIEW_PERMISSION):
                if not instance.is_held:
                    extra_context["show_ib_hold_button"] = True
                    extra_context["ib_hold_url"] = reverse(
                        "admin:ib_obligation_hold", args=[instance.pk],
                    )
                else:
                    extra_context["show_ib_release_button"] = True
                    extra_context["ib_release_url"] = reverse(
                        "admin:ib_obligation_release", args=[instance.pk],
                    )

            # IB-REVERSALS-FRAUD-05C — Submit Adjustment button, only for
            # already-CREDITED obligations (the only status
            # submit_adjustment() itself accepts — see
            # ib_commission_reversal.py::AdjustmentNotEligible). Read-only
            # remaining_reversible() display always shown for a CREDITED
            # obligation; the button itself only when capacity remains.
            if instance.status == IBCommissionObligation.ST_CREDITED:
                from .ib_commission_reversal import remaining_reversible as _remaining_reversible_display
                remaining = _remaining_reversible_display(instance)
                extra_context["ib_remaining_reversible"] = remaining
                if remaining > 0 and request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
                    extra_context["show_ib_adjustment_submit_button"] = True
                    extra_context["ib_adjustment_submit_url"] = reverse(
                        "admin:ib_adjustment_submit", args=[instance.pk],
                    )
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("ib-directory/", self.admin_site.admin_view(self.ib_directory_view), name="ib_directory"),
            path("ib-directory/<int:referral_id>/", self.admin_site.admin_view(self.ib_detail_view), name="ib_detail"),
            path("<int:pk>/ib-approve/", self.admin_site.admin_view(self.ib_obligation_approve_view), name="ib_obligation_approve"),
            path("<int:pk>/ib-link-treasury/", self.admin_site.admin_view(self.ib_obligation_link_treasury_view), name="ib_obligation_link_treasury"),
            path("<int:pk>/ib-sync/", self.admin_site.admin_view(self.ib_obligation_sync_view), name="ib_obligation_sync"),
            path("ib-reconcile/", self.admin_site.admin_view(self.ib_reconcile_view), name="ib_reconcile"),
            path("<int:pk>/ib-hold/", self.admin_site.admin_view(self.ib_obligation_hold_view), name="ib_obligation_hold"),
            path("<int:pk>/ib-release/", self.admin_site.admin_view(self.ib_obligation_release_view), name="ib_obligation_release"),
            path("ib-directory/<int:referral_id>/ib-freeze/", self.admin_site.admin_view(self.ib_referral_freeze_view), name="ib_referral_freeze"),
            path("ib-directory/<int:referral_id>/ib-unfreeze/", self.admin_site.admin_view(self.ib_referral_unfreeze_view), name="ib_referral_unfreeze"),
            path("ib-directory/<int:referral_id>/ib-change-rate/", self.admin_site.admin_view(self.ib_referral_change_rate_view), name="ib_referral_change_rate"),
        ]
        return custom + urls

    # ── Directory (list) ──────────────────────────────────────────

    def ib_directory_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied("Missing permission to view IB Ops data.")

        qs = ib_directory_queryset()
        paginator = Paginator(qs, IB_DIRECTORY_PAGE_SIZE)
        page_number = request.GET.get("page", 1)
        page = paginator.get_page(page_number)

        # PER_LOT effective rate resolved per VISIBLE row only — bounded
        # by page size (IB_DIRECTORY_PAGE_SIZE), never by total IB count.
        # Calls the real resolve_applicable_rule() so this can never
        # silently diverge from the authoritative resolver — see
        # ib_directory_queryset()'s own docstring for why this is not
        # batched like the other columns.
        rows = []
        for referral in page.object_list:
            rule, source = ib_effective_rate(referral, IBCommissionRule.RULE_PER_LOT)
            rows.append({
                "referral": referral,
                "client_count": referral.client_count,
                "month_lots": referral.month_lots,
                "pending_amount": referral.pending_amount,
                "approved_amount": referral.approved_amount,
                "credited_amount": referral.credited_amount,
                "wallet_balance": referral.wallet_balance,
                "per_lot_rate": rule.fixed_amount if rule else None,
                "per_lot_source": source,
            })

        context = dict(
            self.admin_site.each_context(request),
            title="IB Directory",
            rows=rows,
            page=page,
            can_reconcile=request.user.has_perm(TREASURY_REVIEW_PERMISSION),
            reconcile_url=reverse("admin:ib_reconcile"),
        )
        return render(request, "admin/ib_directory.html", context)

    # ── Detail ─────────────────────────────────────────────────────

    def ib_detail_view(self, request, referral_id):
        if not self.has_view_permission(request):
            raise PermissionDenied("Missing permission to view IB Ops data.")

        referral = Referral.objects.select_related("user").filter(pk=referral_id).first()
        if referral is None:
            raise Http404("Referral (IB) not found.")

        from .wallet_ledger import get_or_create_wallet
        wallet, _created = get_or_create_wallet(referral.user)

        lot_totals = ib_lot_totals(referral)
        _, week_start, month_start = _period_starts()
        obligation_totals = ib_obligation_totals(referral)

        rate_rows = []
        for rule_type, label in IBCommissionRule.RULE_TYPE_CHOICES:
            if rule_type in (IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, IBCommissionRule.RULE_CPA_BONUS):
                continue  # HOLD — never resolved/displayed as active here
            rule, source = ib_effective_rate(referral, rule_type)
            rate_rows.append({
                "rule_type": rule_type, "label": label, "rule": rule, "source": source,
            })

        recent_clients = list(
            ReferralAttribution.objects.filter(referral=referral)
            .select_related("referred_user").order_by("-attributed_at")[:_RECENT_LIMIT]
        )
        recent_lots = list(
            LotExecutionEvent.objects.filter(
                account__user__referral_attribution__referral=referral,
            ).select_related("account").order_by("-created_at")[:_RECENT_LIMIT]
        )
        recent_obligations = list(
            IBCommissionObligation.objects.filter(referral=referral)
            .select_related("treasury_operation").order_by("-created_at")[:_RECENT_LIMIT]
        )
        recent_wallet_tx = list(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_REBATE)
            .order_by("-created_at")[:_RECENT_LIMIT]
        )

        # IB-REVERSALS-FRAUD-05C — Recent Adjustments + remaining_reversible
        # per CREDITED obligation. Read-only display; computed via the
        # unmodified ib_commission_reversal.remaining_reversible(), never
        # a re-derivation of the over-reversal math.
        from .ib_commission_reversal import remaining_reversible as _remaining_reversible_display
        from .models import IBCommissionAdjustment
        recent_adjustments = list(
            IBCommissionAdjustment.objects.filter(referral=referral)
            .select_related("obligation", "treasury_operation").order_by("-created_at")[:_RECENT_LIMIT]
        )

        # IB-RISK-HOLDS-07B — Freeze / Unfreeze button + recent risk
        # event history. Same permission as every other IB Ops
        # transition (TREASURY_REVIEW_PERMISSION); reused, not new.
        can_review = request.user.has_perm(TREASURY_REVIEW_PERMISSION)
        is_frozen = referral.risk_status == Referral.RISK_FROZEN
        recent_risk_events = list(
            IBRiskEvent.objects.filter(referral=referral)
            .select_related("obligation", "actor").order_by("-created_at")[:_RECENT_LIMIT]
        )

        # IB-PORTAL-08B — CONTRATO ACTUAL card (dedicated PER_LOT lookup,
        # not relied-upon-ordering from rate_rows above) + shared
        # commission summary (generated/pending/held/credited/reversed,
        # section 5's semantics — held is a breakdown of pending, never
        # additive) + Change Commission Rate control.
        per_lot_rule, per_lot_rate_source = ib_effective_rate(referral, IBCommissionRule.RULE_PER_LOT)
        commission_summary = ib_commission_summary(referral)

        context = dict(
            self.admin_site.each_context(request),
            title=f"IB Detail — {referral.code}",
            referral=referral,
            is_frozen=is_frozen,
            show_ib_freeze_button=(can_review and not is_frozen),
            show_ib_unfreeze_button=(can_review and is_frozen),
            ib_freeze_url=reverse("admin:ib_referral_freeze", args=[referral.pk]),
            ib_unfreeze_url=reverse("admin:ib_referral_unfreeze", args=[referral.pk]),
            recent_risk_events=recent_risk_events,
            per_lot_rule=per_lot_rule,
            per_lot_rate_source=per_lot_rate_source,
            commission_summary=commission_summary,
            show_ib_change_rate_button=can_review,
            ib_change_rate_url=reverse("admin:ib_referral_change_rate", args=[referral.pk]),
            wallet=wallet,
            lot_totals=lot_totals,
            active_clients_month=ib_active_trading_client_count(referral, month_start),
            client_count=referral.attributions.count(),
            obligation_totals=obligation_totals,
            rate_rows=rate_rows,
            recent_clients=recent_clients,
            recent_lots=recent_lots,
            recent_obligations=[
                {
                    "obligation": ob, "needs_attention": ib_needs_attention(ob),
                    "remaining_reversible": (
                        _remaining_reversible_display(ob)
                        if ob.status == IBCommissionObligation.ST_CREDITED else None
                    ),
                }
                for ob in recent_obligations
            ],
            recent_wallet_tx=recent_wallet_tx,
            recent_adjustments=recent_adjustments,
        )
        return render(request, "admin/ib_detail.html", context)

    # ── Approve (Service 1) ───────────────────────────────────────

    def ib_obligation_approve_view(self, request, pk):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = IBCommissionObligation.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission obligation not found.")

        detail_url = reverse("admin:simulator_ibcommissionobligation_change", args=[instance.pk])

        if instance.status != IBCommissionObligation.ST_PENDING:
            messages.warning(
                request,
                f"This obligation is no longer PENDING (current status: {instance.status}) — nothing to approve.",
            )
            return redirect(detail_url)

        if request.method == "POST":
            try:
                approved = approve_obligation(instance, request=request)
            except ObligationNotPending:
                messages.warning(
                    request,
                    f"This obligation is no longer PENDING (current status: {instance.status}) — nothing to approve.",
                )
                return redirect(detail_url)
            except ObligationInvalidAmount as exc:
                messages.error(request, f"Cannot approve — invalid amount: {exc}")
                return redirect(detail_url)
            except PermissionDenied:
                raise
            except Exception:
                messages.error(request, "Unexpected error while approving this obligation. No changes were made.")
                return redirect(detail_url)

            _record_ib_admin_event(
                request, "ib_admin.obligation_approved",
                f"IB commission obligation #{approved.pk} approved via IB Ops",
                obligation=approved,
                extra={"previous_status": IBCommissionObligation.ST_PENDING},
            )
            messages.success(request, f"✓ Obligation #{approved.pk} approved.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Approve Obligation #{instance.pk}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_obligation_approve.html", context)

    # ── Link to Treasury (Service 2) ───────────────────────────────

    def ib_obligation_link_treasury_view(self, request, pk):
        if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

        instance = IBCommissionObligation.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission obligation not found.")

        detail_url = reverse("admin:simulator_ibcommissionobligation_change", args=[instance.pk])

        if instance.treasury_operation_id is None and instance.status != IBCommissionObligation.ST_APPROVED:
            messages.warning(
                request,
                f"This obligation is not APPROVED (current status: {instance.status}) — cannot link to Treasury.",
            )
            return redirect(detail_url)

        if request.method == "POST":
            try:
                treasury_request = link_treasury_request(instance, request=request)
            except ObligationNotApproved:
                messages.warning(
                    request,
                    f"This obligation is not APPROVED (current status: {instance.status}) — cannot link to Treasury.",
                )
                return redirect(detail_url)
            except ObligationInvalidAmount as exc:
                messages.error(request, f"Cannot link — invalid amount: {exc}")
                return redirect(detail_url)
            except PermissionDenied:
                raise
            except Exception:
                messages.error(request, "Unexpected error while linking this obligation to Treasury. No changes were made.")
                return redirect(detail_url)

            instance.refresh_from_db()
            _record_ib_admin_event(
                request, "ib_admin.treasury_request_linked",
                f"IB commission obligation #{instance.pk} linked to Treasury request #{treasury_request.pk}",
                obligation=instance,
                extra={"treasury_operation_id": treasury_request.pk, "treasury_status": treasury_request.status},
            )
            messages.success(
                request,
                f"✓ Obligation #{instance.pk} linked to Treasury Request #{treasury_request.pk} "
                f"(status: {treasury_request.status}). Treasury review/execution happens in Treasury's own admin.",
            )
            return redirect(reverse("admin:simulator_treasuryoperationrequest_change", args=[treasury_request.pk]))

        context = dict(
            self.admin_site.each_context(request),
            title=f"Link Obligation #{instance.pk} to Treasury",
            instance=instance,
            already_linked=instance.treasury_operation_id is not None,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_obligation_link_treasury.html", context)

    # ── Sync (Service 3) ───────────────────────────────────────────

    def ib_obligation_sync_view(self, request, pk):
        """
        POST-only — sync_obligation_from_treasury() is a pure observation
        (no permission of its own, no money movement), so this view
        requires only that the operator can already view IB Ops data,
        not a Treasury-specific permission. Never calls Treasury
        approve/execute/reject — only observes already-authoritative
        Treasury state.
        """
        if not self.has_view_permission(request):
            raise PermissionDenied("Missing permission to view IB Ops data.")
        if request.method != "POST":
            raise Http404("This action requires POST.")

        instance = IBCommissionObligation.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission obligation not found.")

        detail_url = reverse("admin:simulator_ibcommissionobligation_change", args=[instance.pk])

        result = sync_obligation_from_treasury(instance)
        outcome = result["outcome"]
        synced = result["obligation"]

        if outcome == "credited":
            _record_ib_admin_event(
                request, "ib_admin.obligation_synced_credited",
                f"IB commission obligation #{synced.pk} synced to CREDITED from Treasury execution",
                obligation=synced,
                extra={"treasury_status": result["treasury_status"]},
            )
            messages.success(request, f"✓ Obligation #{synced.pk} is now CREDITED.")
        elif outcome == "treasury_terminal_non_success":
            messages.warning(
                request,
                f"⚠ Linked Treasury request reached a terminal non-success state "
                f"({result['treasury_status']}) — obligation remains APPROVED, no money moved. "
                "This needs staff attention (see IB-REVERSALS-FRAUD-05 for future resolution policy).",
            )
        elif outcome == "already_credited":
            messages.info(request, "This obligation is already CREDITED — nothing to sync.")
        else:
            messages.info(request, f"No change — {outcome.replace('_', ' ')}.")

        return redirect(detail_url)

    # ── Reconciliation (bulk, safe, idempotent) ─────────────────────

    def ib_reconcile_view(self, request):
        """
        Bulk action, but NOT bulk approval (Owner decision 5 explicitly
        forbids that). This only calls reconcile_approved_obligations()
        — itself already idempotent and safe to run repeatedly/bulk,
        since it never approves or executes anything, only observes
        already-authoritative Treasury state per obligation, exactly
        like ib_obligation_sync_view() above but for every eligible
        obligation at once.
        """
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")
        if request.method != "POST":
            raise Http404("This action requires POST.")

        result = reconcile_approved_obligations()
        messages.success(
            request,
            f"Reconciliation complete — scanned {result['scanned']}, "
            f"credited {result['credited']}, skipped {result['skipped']}, "
            f"failed {result['failed']}.",
        )
        return redirect(reverse("admin:ib_directory"))

    # ── IB-RISK-HOLDS-07B — Hold / Release (obligation-level) ───────

    def ib_obligation_hold_view(self, request, pk):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = IBCommissionObligation.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission obligation not found.")

        detail_url = reverse("admin:simulator_ibcommissionobligation_change", args=[instance.pk])

        if instance.is_held:
            messages.info(request, f"Obligation #{instance.pk} is already held.")
            return redirect(detail_url)

        if request.method == "POST":
            reason = request.POST.get("reason", "")
            try:
                held = hold_obligation(instance, reason, request=request)
            except ObligationAlreadyHeld:
                messages.info(request, f"Obligation #{instance.pk} is already held.")
                return redirect(detail_url)
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_obligation_hold.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Hold Obligation #{instance.pk}", instance=instance, cancel_url=detail_url,
                ))
            except PermissionDenied:
                raise

            _record_ib_risk_event(
                request, "ib_admin.obligation_held",
                f"IB commission obligation #{held.pk} held via IB Ops",
                referral=held.referral, obligation=held, reason=reason,
            )
            messages.success(request, f"✓ Obligation #{held.pk} is now held.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Hold Obligation #{instance.pk}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_obligation_hold.html", context)

    def ib_obligation_release_view(self, request, pk):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = IBCommissionObligation.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission obligation not found.")

        detail_url = reverse("admin:simulator_ibcommissionobligation_change", args=[instance.pk])

        if not instance.is_held:
            messages.info(request, f"Obligation #{instance.pk} is not held.")
            return redirect(detail_url)

        if request.method == "POST":
            reason = request.POST.get("reason", "")
            try:
                released = release_obligation(instance, reason, request=request)
            except ObligationNotHeld:
                messages.info(request, f"Obligation #{instance.pk} is not held.")
                return redirect(detail_url)
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_obligation_release.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Release Obligation #{instance.pk}", instance=instance, cancel_url=detail_url,
                ))
            except PermissionDenied:
                raise

            _record_ib_risk_event(
                request, "ib_admin.obligation_released",
                f"IB commission obligation #{released.pk} released via IB Ops",
                referral=released.referral, obligation=released, reason=reason,
            )
            messages.success(request, f"✓ Obligation #{released.pk} released — no longer held.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Release Obligation #{instance.pk}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_obligation_release.html", context)

    # ── IB-RISK-HOLDS-07B — Freeze / Unfreeze (IB-level) ────────────

    def ib_referral_freeze_view(self, request, referral_id):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = Referral.objects.filter(pk=referral_id).first()
        if instance is None:
            raise Http404("Referral (IB) not found.")

        detail_url = reverse("admin:ib_detail", args=[instance.pk])

        if instance.risk_status == Referral.RISK_FROZEN:
            messages.info(request, f"IB #{instance.pk} is already frozen.")
            return redirect(detail_url)

        if request.method == "POST":
            reason = request.POST.get("reason", "")
            try:
                frozen = freeze_referral(instance, reason, request=request)
            except ReferralAlreadyFrozen:
                messages.info(request, f"IB #{instance.pk} is already frozen.")
                return redirect(detail_url)
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_referral_freeze.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Freeze IB — {instance.code}", instance=instance, cancel_url=detail_url,
                ))
            except PermissionDenied:
                raise

            _record_ib_risk_event(
                request, "ib_admin.referral_frozen",
                f"IB {frozen.code} (#{frozen.pk}) frozen via IB Ops",
                referral=frozen, reason=reason,
            )
            messages.success(request, f"✓ IB {frozen.code} is now frozen.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Freeze IB — {instance.code}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_referral_freeze.html", context)

    def ib_referral_unfreeze_view(self, request, referral_id):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = Referral.objects.filter(pk=referral_id).first()
        if instance is None:
            raise Http404("Referral (IB) not found.")

        detail_url = reverse("admin:ib_detail", args=[instance.pk])

        if instance.risk_status != Referral.RISK_FROZEN:
            messages.info(request, f"IB #{instance.pk} is not frozen.")
            return redirect(detail_url)

        if request.method == "POST":
            reason = request.POST.get("reason", "")
            try:
                unfrozen = unfreeze_referral(instance, reason, request=request)
            except ReferralNotFrozen:
                messages.info(request, f"IB #{instance.pk} is not frozen.")
                return redirect(detail_url)
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_referral_unfreeze.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Unfreeze IB — {instance.code}", instance=instance, cancel_url=detail_url,
                ))
            except PermissionDenied:
                raise

            _record_ib_risk_event(
                request, "ib_admin.referral_unfrozen",
                f"IB {unfrozen.code} (#{unfrozen.pk}) unfrozen via IB Ops",
                referral=unfrozen, reason=reason,
            )
            messages.success(request, f"✓ IB {unfrozen.code} is now active.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Unfreeze IB — {instance.code}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_referral_unfreeze.html", context)

    # ── IB-PORTAL-08B — Change Commission Rate ──────────────────────

    def ib_referral_change_rate_view(self, request, referral_id):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = Referral.objects.filter(pk=referral_id).first()
        if instance is None:
            raise Http404("Referral (IB) not found.")

        detail_url = reverse("admin:ib_detail", args=[instance.pk])
        current_rule, current_source = ib_effective_rate(instance, IBCommissionRule.RULE_PER_LOT)

        if request.method == "POST":
            new_rate = request.POST.get("new_fixed_amount", "")
            try:
                new_rule = change_commission_rate(instance, new_rate, request=request)
            except InvalidCommissionRate as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_referral_change_rate.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Change Commission Rate — {instance.code}",
                    instance=instance, current_rule=current_rule, current_source=current_source,
                    cancel_url=detail_url,
                ))
            except CommissionRateUnchanged as exc:
                messages.info(request, f"{exc}")
                return redirect(detail_url)
            except PermissionDenied:
                raise

            _record_ib_risk_event(
                request, "ib_admin.rate_changed",
                f"IB {instance.code} (#{instance.pk}) PER_LOT rate changed to ${new_rule.fixed_amount}/lot "
                f"(new IBCommissionRule #{new_rule.pk}, effective_from={new_rule.effective_from.isoformat()})",
                referral=instance,
                reason=(
                    f"Rate changed to ${new_rule.fixed_amount}/lot. "
                    f"Previous: {'$' + str(current_rule.fixed_amount) + '/lot (' + current_source + ')' if current_rule else 'no configurada'}."
                ),
                extra={"new_rule_id": new_rule.pk, "new_fixed_amount": str(new_rule.fixed_amount)},
            )
            messages.success(request, f"✓ New rate ${new_rule.fixed_amount}/lot is now effective for {instance.code}.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Change Commission Rate — {instance.code}",
            instance=instance,
            current_rule=current_rule,
            current_source=current_source,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_referral_change_rate.html", context)


# ─────────────────────────────────────────────
# IBCommissionRuleAdmin — GLOBAL rules + per-IB overrides, configurable
# PER_LOT/percentage rates. Never hardcodes a rate. Historical-rate
# policy (locked): once a rule row exists, its economic fields
# (rule_type, referral, fixed_amount, percentage, cpa_trigger_event)
# become read-only — a rate CHANGE must create a NEW row (closing the
# old one via effective_until), never rewrite a live row's economic
# terms. Only effective_until/enabled/notes stay editable on an
# existing row. This mirrors the model's own documented convention
# (IBCommissionObligation snapshots applied_fixed_rate/
# applied_percentage_rate once, at generation time, and never re-reads
# the rule afterward — already proven, unmodified, by
# IB-TREASURY-CREDIT-03's own shipped tests) — this admin only adds UI
# discipline on top of a guarantee the model/service layers already
# enforce independently.
# ─────────────────────────────────────────────

_RULE_ECONOMIC_FIELDS = ("rule_type", "referral", "fixed_amount", "percentage", "cpa_trigger_event")


@admin.register(IBCommissionRule)
class IBCommissionRuleAdmin(admin.ModelAdmin):
    list_display = (
        "rule_type", "referral", "enabled", "fixed_amount", "percentage",
        "effective_from", "effective_until", "created_by", "created_at",
    )
    list_filter = ("rule_type", "enabled")
    search_fields = ("referral__code", "referral__user__username", "notes")
    ordering = ("-created_at",)

    def get_readonly_fields(self, request, obj=None):
        base = ("created_at", "updated_at")
        if obj is None:
            return base
        return base + _RULE_ECONOMIC_FIELDS

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        _record_ib_admin_event_rule(request, obj, created=not change)


def _record_ib_admin_event_rule(request, rule, *, created):
    event_type = "ib_admin.rule_created" if created else "ib_admin.rule_updated"
    description = (
        f"IBCommissionRule #{rule.pk} ({rule.rule_type}, "
        f"{'per-IB referral=' + str(rule.referral_id) if rule.referral_id else 'GLOBAL'}) "
        f"{'created' if created else 'updated (non-economic fields only)'}"
    )
    metadata = {
        "rule_id": rule.pk, "rule_type": rule.rule_type, "referral_id": rule.referral_id,
        "fixed_amount": str(rule.fixed_amount) if rule.fixed_amount is not None else None,
        "percentage": str(rule.percentage) if rule.percentage is not None else None,
        "enabled": rule.enabled, "effective_until": rule.effective_until.isoformat() if rule.effective_until else None,
    }
    audit.log_audit(request, event_type, description, detail=metadata)
    broker_audit.record_payment_event(
        event_type=event_type, severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF, actor_id=request.user.pk,
        description=description, source_module="simulator.ib_admin_ops", metadata=metadata,
    )


# ─────────────────────────────────────────────
# LotExecutionEventAdmin — fully read-only raw-execution visibility,
# same discipline as WalletTransactionAdmin. Not the primary IB Ops
# surface (that's the directory/detail pages above, which already
# aggregate this data) — provided for direct drill-down/search only.
# ─────────────────────────────────────────────

@admin.register(LotExecutionEvent)
class LotExecutionEventAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "symbol", "side", "qty", "execution_price", "entry_path", "created_at")
    list_filter = ("symbol", "side", "entry_path", "created_at")
    search_fields = ("account__id", "account__user__username", "symbol")
    readonly_fields = [f.name for f in LotExecutionEvent._meta.fields]
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────
# IBRiskEventAdmin — fully read-only, append-only history browser.
# IB-RISK-HOLDS-07B. Same discipline as AuditLogAdmin/
# BrokerAuditEventAdmin: rows are written exclusively by
# simulator/ib_risk_holds.py; nothing may add, change, or delete a row
# through this admin. Not the primary surface for this data either
# (that's the "Recent Risk Events" table on the IB detail page) —
# provided for direct drill-down/search across all IBs.
# ─────────────────────────────────────────────

@admin.register(IBRiskEvent)
class IBRiskEventAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "event_type", "referral", "obligation", "actor")
    list_filter = ("event_type", "created_at")
    search_fields = ("referral__code", "referral__user__username", "reason")
    readonly_fields = [f.name for f in IBRiskEvent._meta.fields]
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # superuser can purge stale rows if needed
