# simulator/ib_commission_reversal.py
"""
IB-REVERSALS-FRAUD-05B — reversal/adjustment accounting foundation.

Approved design: IB-REVERSALS-FRAUD-05A audit + design lock. Closes the
gap that audit found: once an IBCommissionObligation reaches CREDITED
(or its linked TreasuryOperationRequest reaches a terminal non-success
state), no mechanism previously existed to compensate/reverse it. This
module is that mechanism — nothing else.

NON-NEGOTIABLE PRINCIPLE (locked, per 05B's own request): a credited IB
commission is never deleted, edited retroactively, or silently changed.
IBCommissionObligation's own snapshot fields (basis_amount,
applied_fixed_rate, applied_percentage_rate, calculated_amount, status,
credited_at, treasury_operation) are NEVER written by any function in
this module — grep-verifiable. A reversal/adjustment is always a NEW,
separate IBCommissionAdjustment row; history is reconstructed by
reading the original obligation plus its related adjustments together,
never by mutating the original.

Target flow (mirrors IB-TREASURY-CREDIT-03's own four-service shape,
same permission split, same idempotity postures — no new architecture
invented):

    IBCommissionObligation.CREDITED
            v  submit_adjustment()         (staff action, can_submit_treasury_request)
    IBCommissionAdjustment.PENDING
            v  approve_adjustment()        (staff action, can_review_treasury_request)
    IBCommissionAdjustment.APPROVED
            v  link_adjustment_to_treasury() (staff action, can_submit_treasury_request)
    TreasuryOperationRequest(operation_type=OP_MANUAL_DEBIT, status=PENDING)
            v  existing Treasury approve/execute (unmodified, Treasury's own authority)
    TreasuryOperationRequest.EXECUTED (WalletTransaction TX_CORRECTION debit,
                                        Wallet.available_balance debited —
                                        both by the existing, unmodified
                                        Treasury execution engine)
            v  sync_adjustment_from_treasury()  (pure observation, no auth)
    IBCommissionAdjustment.EXECUTED

This module NEVER moves money directly: no WalletTransaction, no
LedgerEntry, no BrokerLedger, no direct Wallet.available_balance
mutation anywhere in this file (grep-verifiable). The only Treasury/
wallet-touching call in this entire module is submit_treasury_request()
(creates a PENDING TreasuryOperationRequest — itself never moves money
either). Actual debit remains entirely inside the existing, unmodified
execute_treasury_request() -> wallet_ledger.debit_wallet() path —
staff-triggered, same permission discipline as every other Treasury
operation type. No new Treasury operation type was created: OP_MANUAL_
DEBIT already existed and is already mapped to ("debit", TX_CORRECTION)
in treasury_requests.py's own _EXECUTION_MAPPING (unmodified).

OVER-REVERSAL PROTECTION (locked requirement): the sum of every
PENDING/APPROVED/EXECUTED IBCommissionAdjustment against one obligation
can never exceed that obligation's own calculated_amount. Enforced
under transaction.atomic() + select_for_update() on the obligation row
(submit_adjustment()) and re-validated again, independently, at
approval time (approve_adjustment()) — a race between two concurrent
submissions, or between submission and a separately-approved sibling
adjustment, cannot silently push the total over the limit. A REJECTED
adjustment releases its reserved amount back (excluded from the sum).

IDEMPOTENCY POSTURES (deliberate, mirroring IB-TREASURY-CREDIT-03
exactly):
  - submit_adjustment()          : creates one new PENDING row per call
    (not itself idempotent — each call is a distinct request; the
    OVER-REVERSAL check is what prevents this from ever creating more
    reversible capacity than exists, not a duplicate-suppression check).
  - approve_adjustment()         : one-shot staff action. A second call
    on an already-APPROVED (or any non-PENDING) adjustment RAISES
    AdjustmentNotPending — mirrors approve_treasury_request()'s/
    approve_obligation()'s own convention exactly.
  - link_adjustment_to_treasury(): idempotent. A second call on an
    already-linked adjustment returns the SAME existing
    TreasuryOperationRequest, never creates a second one — driven by
    the adjustment's own treasury_operation OneToOneField, a database-
    backed guarantee (IntegrityError-safe fallback included).
  - sync_adjustment_from_treasury(): idempotent. A second call after
    EXECUTED is a pure NO-OP, same result. Safe to call from a
    reconciliation sweep on every pass.

Never touches simulator/consumers.py, simulator/population_engine.py,
or simulator/payout_orchestrator.py, directly or indirectly. Never
recalculates trading P/L, execution prices, spread, commission
formulas, or lots — reads only the already-generated
IBCommissionObligation.calculated_amount as the ceiling for what can be
reversed, exactly as IB-TREASURY-CREDIT-03's own settlement layer reads
it as the (never-recomputed) payout amount.

Explicitly NOT built here (HOLD, per IB-REVERSALS-FRAUD-05B's own
scope): automated fraud detection, risk scoring, velocity rules,
self-referral/multi-account detection, CPA_BONUS, SPREAD_REVENUE_SHARE,
new admin UI/dashboards, negative-wallet/receivable/debt tracking (an
insufficient-balance debit simply fails via the existing, unmodified
InsufficientFunds path — see that exception's own docstring in
wallet_ledger.py). Those remain explicitly out of scope, deferred to
later blocks per the 05A audit's own sizing recommendation (05C/05D).
"""
import logging
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from . import audit, broker_audit
from .forms import TreasuryOperationRequestForm
from .models import IBCommissionAdjustment, IBCommissionObligation, TreasuryOperationRequest
from .treasury_requests import (
    TREASURY_REVIEW_PERMISSION, TREASURY_SUBMIT_PERMISSION, submit_treasury_request,
)
from .wallet_ledger import get_or_create_wallet

logger = logging.getLogger("simulator.ib_commission_reversal")

# Statuses whose amount counts as "reserved" against the obligation's
# calculated_amount ceiling — PENDING/APPROVED reserve capacity before
# money moves, EXECUTED reserves it permanently. REJECTED is excluded
# on purpose: a declined request releases its reserved amount.
_RESERVED_STATUSES = (
    IBCommissionAdjustment.ST_PENDING,
    IBCommissionAdjustment.ST_APPROVED,
    IBCommissionAdjustment.ST_EXECUTED,
)

_TREASURY_TERMINAL_NON_SUCCESS_STATUSES = (
    TreasuryOperationRequest.ST_REJECTED,
    TreasuryOperationRequest.ST_CANCELLED,
    TreasuryOperationRequest.ST_FAILED,
)


class AdjustmentNotEligible(Exception):
    """Raised when the target obligation is not CREDITED — only
    CREDITED obligations may be reversed/adjusted. PENDING/APPROVED
    (unlinked or Treasury-pending) obligations never had money move in
    the first place; reversing them is out of this module's scope by
    design (IB-REVERSALS-FRAUD-05A Part D, Cases A-C need no
    compensating record at all)."""


class AdjustmentExceedsRemaining(Exception):
    """Raised when a new/updated adjustment amount would push the total
    reserved+executed amount for this obligation beyond its own
    calculated_amount — the over-reversal guard."""


class AdjustmentNotPending(Exception):
    """Raised when approve/reject is attempted on a non-PENDING
    adjustment (re-checked under lock)."""


class AdjustmentNotApproved(Exception):
    """Raised by link_adjustment_to_treasury() when the adjustment is
    not linked AND its status is not APPROVED (re-checked under lock)."""


def _remaining_reversible(obligation, *, exclude_pk=None):
    """calculated_amount minus everything currently PENDING/APPROVED/
    EXECUTED against it (optionally excluding one adjustment's own
    reservation, for approve_adjustment()'s self-re-check). Callers
    MUST already hold a row lock (select_for_update()) on `obligation`
    before calling this — it does not lock anything itself, so it is
    safe to reuse inside an already-open transaction without acquiring
    a second, redundant lock."""
    qs = IBCommissionAdjustment.objects.filter(
        obligation=obligation, status__in=_RESERVED_STATUSES,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    reserved = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    return obligation.calculated_amount - reserved


def remaining_reversible(obligation):
    """Public, lock-free read — for DISPLAY purposes only (e.g. a
    future admin page showing "how much can still be reversed" for a
    given obligation). Never use this result to decide whether to
    CREATE an adjustment — submit_adjustment() re-derives it itself,
    under lock, precisely because a value read without a lock can be
    stale by the time a decision is made from it."""
    return _remaining_reversible(obligation)


def _record_event(request, event_type, description, *, adjustment, extra=None):
    metadata = {
        "adjustment_id": adjustment.pk,
        "obligation_id": adjustment.obligation_id,
        "referral_id": adjustment.referral_id,
        "adjustment_type": adjustment.adjustment_type,
        "amount": str(adjustment.amount),
        "status": adjustment.status,
        "treasury_operation_id": adjustment.treasury_operation_id,
    }
    if extra:
        metadata.update(extra)

    audit.log_audit(request, event_type, description, detail=metadata)
    broker_audit.record_payment_event(
        event_type=event_type,
        severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=description,
        source_module="simulator.ib_commission_reversal",
        metadata=metadata,
    )


# ─────────────────────────────────────────────
# Service 1 — submit_adjustment(): create a PENDING IBCommissionAdjustment
# ─────────────────────────────────────────────

def submit_adjustment(obligation, *, amount, reason,
                       adjustment_type=IBCommissionAdjustment.TYPE_REVERSAL, request):
    """
    Create a PENDING IBCommissionAdjustment against a CREDITED
    IBCommissionObligation. Moves no money. Requires
    TREASURY_SUBMIT_PERMISSION — the "request" stage of the pipeline,
    same two-permission split IB-TREASURY-CREDIT-03 already established
    (submit vs. review are distinct permissions/steps there too).

    Args:
        obligation:      an IBCommissionObligation — only its .pk is
                         used; re-read under select_for_update().
        amount:          the amount to reverse/adjust — must be
                         positive and must fit within the obligation's
                         remaining reversible capacity (checked under
                         lock).
        reason:          required, non-empty, stripped.
        adjustment_type: IBCommissionAdjustment.TYPE_REVERSAL (default)
                         or .TYPE_ADJUSTMENT.
        request:         current HttpRequest — request.user must be
                         authenticated and hold TREASURY_SUBMIT_PERMISSION.

    Returns:
        The created IBCommissionAdjustment (status=PENDING).

    Raises:
        PermissionDenied:          not authenticated, or lacks permission.
        ValueError:                amount <= 0, or reason is empty.
        AdjustmentNotEligible:     obligation.status != CREDITED.
        AdjustmentExceedsRemaining: amount exceeds remaining reversible capacity.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to submit an IB commission adjustment.")
    if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"Adjustment amount must be positive, got {amount}.")

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to submit an IB commission adjustment.")

    with transaction.atomic():
        locked_obligation = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if locked_obligation.status != IBCommissionObligation.ST_CREDITED:
            raise AdjustmentNotEligible(
                f"IBCommissionObligation #{locked_obligation.pk} is not CREDITED "
                f"(status={locked_obligation.status}) — only credited obligations "
                "can be reversed/adjusted."
            )

        remaining = _remaining_reversible(locked_obligation)
        if amount > remaining:
            raise AdjustmentExceedsRemaining(
                f"Requested amount {amount} exceeds remaining reversible amount "
                f"{remaining} for IBCommissionObligation #{locked_obligation.pk} "
                f"(calculated_amount={locked_obligation.calculated_amount})."
            )

        adjustment = IBCommissionAdjustment.objects.create(
            obligation=locked_obligation,
            referral=locked_obligation.referral,
            adjustment_type=adjustment_type,
            amount=amount,
            reason=reason,
            status=IBCommissionAdjustment.ST_PENDING,
            created_by=request.user,
        )
    # ── transaction closed — the PENDING adjustment is committed from here on ──

    _record_event(
        request, "ib_reversal.adjustment_submitted",
        f"IB commission adjustment #{adjustment.pk} submitted against "
        f"obligation #{locked_obligation.pk} ({adjustment_type}, ${amount})",
        adjustment=adjustment,
    )
    logger.info(
        "[ib_commission_reversal] adjustment=%d submitted for obligation=%d "
        "amount=%s type=%s by user=%d",
        adjustment.pk, locked_obligation.pk, amount, adjustment_type, request.user.pk,
    )
    return adjustment


# ─────────────────────────────────────────────
# Service 2 — approve_adjustment(): PENDING -> APPROVED
# ─────────────────────────────────────────────

def approve_adjustment(adjustment, *, request):
    """
    Transition a PENDING IBCommissionAdjustment to APPROVED. Moves no
    money. Re-validates the remaining-reversible amount under lock —
    defensive against a race between this adjustment's own submission
    and a SIBLING adjustment against the same obligation being
    approved/executed in between. Requires TREASURY_REVIEW_PERMISSION
    (mirrors approve_treasury_request()/approve_obligation()).

    Raises:
        PermissionDenied, AdjustmentNotPending, AdjustmentExceedsRemaining.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to approve an IB commission adjustment.")
    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    with transaction.atomic():
        locked = IBCommissionAdjustment.objects.select_for_update().get(pk=adjustment.pk)

        if locked.status != IBCommissionAdjustment.ST_PENDING:
            raise AdjustmentNotPending(
                f"IBCommissionAdjustment #{locked.pk} is not pending (status={locked.status})."
            )

        locked_obligation = IBCommissionObligation.objects.select_for_update().get(pk=locked.obligation_id)
        remaining_excluding_self = _remaining_reversible(locked_obligation, exclude_pk=locked.pk)
        if locked.amount > remaining_excluding_self:
            raise AdjustmentExceedsRemaining(
                f"IBCommissionAdjustment #{locked.pk} amount {locked.amount} no longer "
                f"fits remaining reversible {remaining_excluding_self} for obligation "
                f"#{locked_obligation.pk} — a sibling adjustment must have consumed "
                "capacity since this one was submitted."
            )

        locked.status = IBCommissionAdjustment.ST_APPROVED
        locked.approved_by = request.user
        locked.approved_at = timezone.now()
        locked.save(update_fields=["status", "approved_by", "approved_at"])
    # ── transaction closed ──

    _record_event(
        request, "ib_reversal.adjustment_approved",
        f"IB commission adjustment #{locked.pk} approved", adjustment=locked,
    )
    logger.info(
        "[ib_commission_reversal] adjustment=%d approved by user=%d amount=%s",
        locked.pk, request.user.pk, locked.amount,
    )
    return locked


def reject_adjustment(adjustment, rejection_reason, *, request):
    """
    Transition a PENDING IBCommissionAdjustment to REJECTED. Moves no
    money. Releases the adjustment's reserved amount back to the
    obligation's remaining-reversible capacity (REJECTED is excluded
    from _RESERVED_STATUSES). Requires TREASURY_REVIEW_PERMISSION.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to reject an IB commission adjustment.")
    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    rejection_reason = (rejection_reason or "").strip()
    if not rejection_reason:
        raise ValueError("rejection_reason is required to reject an IB commission adjustment.")

    with transaction.atomic():
        locked = IBCommissionAdjustment.objects.select_for_update().get(pk=adjustment.pk)

        if locked.status != IBCommissionAdjustment.ST_PENDING:
            raise AdjustmentNotPending(
                f"IBCommissionAdjustment #{locked.pk} is not pending (status={locked.status})."
            )

        locked.status = IBCommissionAdjustment.ST_REJECTED
        locked.rejected_by = request.user
        locked.rejected_at = timezone.now()
        locked.rejection_reason = rejection_reason
        locked.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])
    # ── transaction closed ──

    _record_event(
        request, "ib_reversal.adjustment_rejected",
        f"IB commission adjustment #{locked.pk} rejected", adjustment=locked,
        extra={"rejection_reason": rejection_reason},
    )
    return locked


# ─────────────────────────────────────────────
# Service 3 — link_adjustment_to_treasury(): APPROVED -> linked TreasuryOperationRequest
# ─────────────────────────────────────────────

def link_adjustment_to_treasury(adjustment, *, request):
    """
    For an APPROVED IBCommissionAdjustment, create and link exactly one
    TreasuryOperationRequest (operation_type=OP_MANUAL_DEBIT — the
    existing, already-tested debit-capable operation type; NO new
    operation type is created by this module). amount MUST equal
    exactly adjustment.amount.

    Idempotent: if the adjustment is already linked, returns the
    EXISTING linked request without creating a second one — same
    database-backed guarantee (OneToOneField + select_for_update())
    IB-TREASURY-CREDIT-03's own link_treasury_request() already uses.

    Reuses the real Treasury request creation architecture exactly as a
    staff submission would: builds a TreasuryOperationRequestForm,
    validates it, calls treasury_requests.submit_treasury_request() —
    never constructs a TreasuryOperationRequest via bare ORM .create().

    Raises:
        PermissionDenied, AdjustmentNotApproved, ValidationError (defensive).
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to link an IB commission adjustment to Treasury.")
    if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

    with transaction.atomic():
        locked = IBCommissionAdjustment.objects.select_for_update().get(pk=adjustment.pk)

        if locked.treasury_operation_id is not None:
            return TreasuryOperationRequest.objects.get(pk=locked.treasury_operation_id)

        if locked.status != IBCommissionAdjustment.ST_APPROVED:
            raise AdjustmentNotApproved(
                f"IBCommissionAdjustment #{locked.pk} is not approved (status={locked.status})."
            )

        wallet, _created = get_or_create_wallet(locked.referral.user)

        form = TreasuryOperationRequestForm(data={
            "wallet": wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_MANUAL_DEBIT,
            "amount": str(locked.amount),
            "reason": (
                f"IB commission {locked.adjustment_type.lower()} — "
                f"obligation #{locked.obligation_id}: {locked.reason}"
            ),
            "reference": f"IBCommissionAdjustment #{locked.pk}",
            "category": TreasuryOperationRequest.CAT_OTHER,
            "comment": locked.reason,
        })
        if not form.is_valid():
            raise ValidationError(form.errors)

        treasury_request = submit_treasury_request(form, request=request)

        try:
            locked.treasury_operation = treasury_request
            locked.save(update_fields=["treasury_operation"])
        except IntegrityError:
            # Defense-in-depth only — unreachable under this function's
            # own select_for_update() discipline; kept as a backstop
            # against any future caller reaching this function without
            # holding that lock.
            locked.refresh_from_db()
            return TreasuryOperationRequest.objects.get(pk=locked.treasury_operation_id)
    # ── transaction closed — the link + the new TreasuryOperationRequest are committed together ──

    _record_event(
        request, "ib_reversal.treasury_debit_linked",
        f"IB commission adjustment #{locked.pk} linked to Treasury debit request #{treasury_request.pk}",
        adjustment=locked,
        extra={"treasury_operation_id": treasury_request.pk, "treasury_status": treasury_request.status},
    )
    logger.info(
        "[ib_commission_reversal] adjustment=%d linked to treasury_request=%d amount=%s",
        locked.pk, treasury_request.pk, treasury_request.amount,
    )
    return treasury_request


# ─────────────────────────────────────────────
# Service 4 — sync_adjustment_from_treasury(): APPROVED -> EXECUTED
# ─────────────────────────────────────────────

def sync_adjustment_from_treasury(adjustment):
    """
    Idempotent, read-then-write reconciliation: observes the linked
    TreasuryOperationRequest's own already-authoritative status and, if
    and only if it has reached EXECUTED, mirrors that onto the
    adjustment (APPROVED -> EXECUTED, executed_at set from the Treasury
    request's own executed_at). Never calls Treasury approve/execute.
    No `request`/permission required — pure observation, safe from an
    unattended reconciliation sweep, exactly like IB-TREASURY-CREDIT-03's
    own sync_obligation_from_treasury().

    REJECTED/CANCELLED/FAILED linked requests leave the adjustment
    APPROVED and linked, unchanged — reported via the returned dict's
    "outcome" key, never acted on further here.

    Returns:
        dict with keys "adjustment", "outcome", "treasury_status".
    """
    with transaction.atomic():
        locked = IBCommissionAdjustment.objects.select_for_update().get(pk=adjustment.pk)

        if locked.status == IBCommissionAdjustment.ST_EXECUTED:
            return {"adjustment": locked, "outcome": "already_executed", "treasury_status": None}

        if locked.status != IBCommissionAdjustment.ST_APPROVED:
            return {"adjustment": locked, "outcome": "not_approved", "treasury_status": None}

        if locked.treasury_operation_id is None:
            return {"adjustment": locked, "outcome": "not_linked", "treasury_status": None}

        treasury_request = TreasuryOperationRequest.objects.select_for_update().get(
            pk=locked.treasury_operation_id,
        )

        if treasury_request.status in _TREASURY_TERMINAL_NON_SUCCESS_STATUSES:
            return {
                "adjustment": locked, "outcome": "treasury_terminal_non_success",
                "treasury_status": treasury_request.status,
            }

        if treasury_request.status != TreasuryOperationRequest.ST_EXECUTED:
            return {
                "adjustment": locked, "outcome": "treasury_pending_execution",
                "treasury_status": treasury_request.status,
            }

        locked.status = IBCommissionAdjustment.ST_EXECUTED
        locked.executed_at = treasury_request.executed_at or timezone.now()
        locked.save(update_fields=["status", "executed_at"])
    # ── transaction closed ──

    logger.info(
        "[ib_commission_reversal] adjustment=%d executed (treasury_request=%d amount=%s)",
        locked.pk, treasury_request.pk, treasury_request.amount,
    )
    return {
        "adjustment": locked, "outcome": "executed",
        "treasury_status": treasury_request.status,
    }


# ─────────────────────────────────────────────
# Reconciliation — reconcile_pending_adjustments()
# ─────────────────────────────────────────────

def reconcile_pending_adjustments(batch_size=500):
    """
    Scan APPROVED, Treasury-linked IBCommissionAdjustment rows and call
    sync_adjustment_from_treasury() on each. Never creates a
    TreasuryOperationRequest, never approves/executes/rejects one,
    never touches Wallet/WalletTransaction/LedgerEntry/BrokerLedger
    directly — pure observation, same shape as IB-TREASURY-CREDIT-03's
    own reconcile_approved_obligations(). Not wired to any Celery task
    in this block (05B keeps scope to the accounting foundation only;
    scheduling is a later concern) — callable directly or from a future
    task.
    """
    adjustments = list(
        IBCommissionAdjustment.objects.filter(
            status=IBCommissionAdjustment.ST_APPROVED, treasury_operation__isnull=False,
        ).order_by("id")[:batch_size]
    )
    scanned = len(adjustments)
    executed = 0
    skipped = 0
    failed = 0
    for adj in adjustments:
        try:
            result = sync_adjustment_from_treasury(adj)
        except Exception:
            logger.exception(
                "[ib_commission_reversal] reconcile_pending_adjustments failed "
                "for adjustment=%d — skipping, next run will retry", adj.pk,
            )
            skipped += 1
            continue
        outcome = result["outcome"]
        if outcome == "executed":
            executed += 1
        elif outcome == "treasury_terminal_non_success":
            failed += 1
        else:
            skipped += 1
    result = {"scanned": scanned, "executed": executed, "skipped": skipped, "failed": failed}
    logger.info("[ib_commission_reversal] reconcile_pending_adjustments %s", result)
    return result
