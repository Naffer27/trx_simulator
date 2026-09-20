# simulator/ib_treasury_settlement.py
"""
IB-TREASURY-CREDIT-03 — settlement service.

Approved design: IB-TREASURY-CREDIT-03A audit + design lock. Implements
the narrow linking/reverse-sync layer identified there as the only
genuinely new work required — every primitive this module calls into
(TreasuryOperationRequest's OP_IB_COMMISSION operation type, its
approve/execute state machine, wallet_ledger.credit_wallet(), the
TX_REBATE mapping) already existed, already tested, unmodified here.

Target flow:

    IBCommissionObligation.PENDING
            v  approve_obligation()            (staff action)
    IBCommissionObligation.APPROVED
            v  link_treasury_request()          (staff action)
    TreasuryOperationRequest(operation_type=OP_IB_COMMISSION, status=PENDING)
            v  existing approve_treasury_request() / execute_treasury_request()
    TreasuryOperationRequest.EXECUTED  (WalletTransaction TX_REBATE created,
                                         Wallet.available_balance credited —
                                         both by the existing, unmodified
                                         Treasury execution engine)
            v  sync_obligation_from_treasury()  (pure observation, no auth)
    IBCommissionObligation.CREDITED

LOCKED ECONOMIC RULE: the authoritative payout amount is always and only
IBCommissionObligation.calculated_amount, snapshotted at obligation-
generation time by simulator/ib_commission.py. Nothing in this module
ever reads IBCommissionRule, recomputes qty/price/contract_size,
re-reads BrokerLedger, or re-derives an amount from any trading fact.
Settlement is rule_type-agnostic: it never branches on PER_LOT vs
CHALLENGE_PERCENT vs DEPOSIT_PERCENT vs TRADING_COMMISSION_REVENUE_SHARE
— it only ever reads calculated_amount. CPA_BONUS and SPREAD_REVENUE_
SHARE remain on HOLD (IB-COMMISSION-TRIGGERS-02D / -02) and are
untouched by this module — it has no rule_type-specific logic to touch.

Three services, three different idempotency postures (deliberate, per
the approved design):
  - approve_obligation()          : one-shot staff action. A second call
    on an already-APPROVED obligation RAISES ObligationNotPending —
    mirrors treasury_requests.py::approve_treasury_request()'s own
    convention exactly (the caller must know explicitly that nothing
    happened on a repeat call, not assume success).
  - link_treasury_request()       : idempotent. A second call on an
    already-linked obligation returns the SAME existing
    TreasuryOperationRequest, never creates a second one. Driven by the
    obligation's own treasury_operation OneToOneField — a database-
    backed guarantee, not merely an in-process check (a race that got
    past the row lock would still hit IntegrityError on the second FK
    assignment, caught below and treated as "already linked").
  - sync_obligation_from_treasury(): idempotent. A second call after
    CREDITED is a pure NO-OP, same result. Safe to call from a
    reconciliation sweep on every pass.

Never moves money directly: no WalletTransaction, no LedgerEntry, no
BrokerLedger, no direct Wallet.available_balance mutation anywhere in
this file. The only Treasury/wallet-touching call in this entire module
is submit_treasury_request() (creates a PENDING TreasuryOperationRequest
— itself never moves money either, per its own docstring). Actual money
movement remains entirely inside the existing, unmodified
execute_treasury_request() -> wallet_ledger.credit_wallet() path —
staff-triggered, same permission discipline as every other Treasury
operation type.

REJECTED / CANCELLED / FAILED Treasury requests: per the locked 03
policy, sync_obligation_from_treasury() leaves the obligation APPROVED
and linked (never auto-cancels, never auto-reverses, never spins up a
replacement TreasuryOperationRequest) — it only reports the terminal
non-success state via its return value's "outcome" key. Replacement/
reversal policy is explicitly deferred to IB-REVERSALS-FRAUD-05.

Never touches simulator/consumers.py or simulator/population_engine.py,
directly or indirectly — this module operates entirely downstream of
trading economics, reading only the already-generated
IBCommissionObligation row.
"""
import logging

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .forms import TreasuryOperationRequestForm
from .models import IBCommissionObligation, TreasuryOperationRequest
from .treasury_requests import (
    TREASURY_REVIEW_PERMISSION, TREASURY_SUBMIT_PERMISSION, submit_treasury_request,
)
from .wallet_ledger import get_or_create_wallet

logger = logging.getLogger("simulator.ib_treasury_settlement")

# Reconciliation outcome labels — returned by sync_obligation_from_treasury()
# and used by reconcile_approved_obligations() to bucket its counters.
OUTCOME_CREDITED                    = "credited"
OUTCOME_ALREADY_CREDITED            = "already_credited"
OUTCOME_NOT_APPROVED                = "not_approved"
OUTCOME_NOT_LINKED                  = "not_linked"
OUTCOME_TREASURY_PENDING_EXECUTION  = "treasury_pending_execution"
OUTCOME_TREASURY_TERMINAL_NON_SUCCESS = "treasury_terminal_non_success"

_TREASURY_TERMINAL_NON_SUCCESS_STATUSES = {
    TreasuryOperationRequest.ST_REJECTED,
    TreasuryOperationRequest.ST_CANCELLED,
    TreasuryOperationRequest.ST_FAILED,
}


class ObligationNotPending(Exception):
    """Raised by approve_obligation() when the obligation's current
    status is not PENDING (re-checked under lock)."""


class ObligationNotApproved(Exception):
    """Raised by link_treasury_request() when the obligation's current
    status is not APPROVED (re-checked under lock) and it is not already
    linked (the already-linked case is idempotent, not an error)."""


class ObligationInvalidAmount(Exception):
    """Raised when calculated_amount is not a positive value — a
    defensive guard; IBCommissionObligation.calculated_amount is NOT
    NULL at the schema level but carries no CheckConstraint enforcing
    positivity, so this module does not trust that alone."""


def _validate_amount(obligation):
    if obligation.calculated_amount is None or obligation.calculated_amount <= 0:
        raise ObligationInvalidAmount(
            f"IBCommissionObligation #{obligation.pk} has a non-positive "
            f"calculated_amount ({obligation.calculated_amount}) — refusing to proceed."
        )


# ─────────────────────────────────────────────
# Service 1 — approve_obligation(): PENDING -> APPROVED
# ─────────────────────────────────────────────

def approve_obligation(obligation, *, request):
    """
    Transition a PENDING IBCommissionObligation to APPROVED.

    Never touches Treasury or Wallet. Reuses TREASURY_REVIEW_PERMISSION
    ("simulator.can_review_treasury_request") — the closest existing
    Treasury convention for a staff review/approval gate; no new
    permission is invented for this block.

    Args:
        obligation: an IBCommissionObligation — only its .pk is used; it
                    is re-read under select_for_update(), never trusted
                    for status (could be stale).
        request:    the current HttpRequest — request.user must be
                    authenticated and hold TREASURY_REVIEW_PERMISSION.

    Returns:
        The locked, updated IBCommissionObligation instance (APPROVED).

    Raises:
        PermissionDenied:        request.user not authenticated, or
                                  lacks TREASURY_REVIEW_PERMISSION.
        ObligationNotPending:    current status is not PENDING.
        ObligationInvalidAmount: calculated_amount is not positive.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to approve an IB commission obligation.")
    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    with transaction.atomic():
        locked = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if locked.status != IBCommissionObligation.ST_PENDING:
            raise ObligationNotPending(
                f"IBCommissionObligation #{locked.pk} is not pending (status={locked.status})."
            )

        _validate_amount(locked)

        locked.status = IBCommissionObligation.ST_APPROVED
        locked.approved_by = request.user
        locked.approved_at = timezone.now()
        locked.save(update_fields=["status", "approved_by", "approved_at"])
    # ── transaction closed — the APPROVED transition is committed from here on ──

    logger.info(
        "[ib_treasury_settlement] obligation=%d approved by user=%d amount=%s rule_type=%s",
        locked.pk, request.user.pk, locked.calculated_amount, locked.rule_type,
    )
    return locked


# ─────────────────────────────────────────────
# Service 2 — link_treasury_request(): APPROVED -> linked TreasuryOperationRequest
# ─────────────────────────────────────────────

def link_treasury_request(obligation, *, request):
    """
    For an APPROVED IBCommissionObligation, create and link exactly one
    TreasuryOperationRequest (operation_type=OP_IB_COMMISSION,
    amount=obligation.calculated_amount exactly, status=PENDING —
    created, never approved or executed, by this function).

    Idempotent: if the obligation is already linked
    (treasury_operation_id is not None), returns the EXISTING linked
    request without creating a second one or re-validating status —
    safe to call repeatedly, including from a reconciliation retry.

    Reuses the existing Treasury request creation architecture exactly
    as a real staff submission would: builds a TreasuryOperationRequestForm,
    validates it, and calls treasury_requests.submit_treasury_request()
    — never constructs a TreasuryOperationRequest via bare ORM .create(),
    so every existing per-operation-type form validation (amount > 0,
    reason required, reference required for OP_IB_COMMISSION) and every
    existing audit-log write (AuditLog + BrokerAuditEvent) fire exactly
    as they do for any other Treasury request.

    The wallet resolved is the IB owner's wallet
    (obligation.referral.user), via the existing get_or_create_wallet()
    — never a bare Wallet.objects.get(), so a referral owner who has
    never had a wallet created for any other reason still settles
    correctly on first payout.

    Args:
        obligation: an IBCommissionObligation — only its .pk is used;
                    re-read under select_for_update().
        request:    the current HttpRequest — request.user must be
                    authenticated and hold TREASURY_SUBMIT_PERMISSION
                    (submit_treasury_request() re-checks this
                    independently regardless).

    Returns:
        The linked TreasuryOperationRequest (newly created, or the
        pre-existing one on a repeat call).

    Raises:
        PermissionDenied:        request.user not authenticated, or
                                  lacks TREASURY_SUBMIT_PERMISSION.
        ObligationNotApproved:   obligation is not linked AND its status
                                  is not APPROVED.
        ObligationInvalidAmount: calculated_amount is not positive.
        ValidationError:         the constructed form is somehow invalid
                                  (defensive — should not occur given the
                                  fields this function itself supplies).
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to link an IB commission obligation to Treasury.")
    if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

    with transaction.atomic():
        locked = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if locked.treasury_operation_id is not None:
            # Idempotent no-op — already linked. Re-fetch to hand back a
            # fully-loaded instance rather than a lazy FK.
            return TreasuryOperationRequest.objects.get(pk=locked.treasury_operation_id)

        if locked.status != IBCommissionObligation.ST_APPROVED:
            raise ObligationNotApproved(
                f"IBCommissionObligation #{locked.pk} is not approved (status={locked.status})."
            )

        _validate_amount(locked)

        wallet, _created = get_or_create_wallet(locked.referral.user)

        form = TreasuryOperationRequestForm(data={
            "wallet": wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_IB_COMMISSION,
            "amount": str(locked.calculated_amount),
            "reason": f"IB commission settlement — obligation #{locked.pk} ({locked.rule_type})",
            "reference": f"IBCommissionObligation #{locked.pk}",
        })
        if not form.is_valid():
            from django.core.exceptions import ValidationError
            raise ValidationError(form.errors)

        treasury_request = submit_treasury_request(form, request=request)

        try:
            locked.treasury_operation = treasury_request
            locked.save(update_fields=["treasury_operation"])
        except IntegrityError:
            # Defense-in-depth only — under this function's own
            # select_for_update() discipline, a genuine concurrent
            # second linker blocks on the row lock and observes
            # treasury_operation_id already set above, never reaching
            # this assignment. Kept as a backstop against any future
            # caller that reaches this function without holding that
            # lock (e.g. a lock-order bug elsewhere).
            locked.refresh_from_db()
            return TreasuryOperationRequest.objects.get(pk=locked.treasury_operation_id)
    # ── transaction closed — the link + the new TreasuryOperationRequest are committed together ──

    logger.info(
        "[ib_treasury_settlement] obligation=%d linked to treasury_request=%d amount=%s",
        locked.pk, treasury_request.pk, treasury_request.amount,
    )
    return treasury_request


# ─────────────────────────────────────────────
# Service 3 — sync_obligation_from_treasury(): APPROVED -> CREDITED
# ─────────────────────────────────────────────

def sync_obligation_from_treasury(obligation):
    """
    Idempotent, read-then-write reconciliation: observes the linked
    TreasuryOperationRequest's own already-authoritative status and, if
    and only if it has reached EXECUTED, mirrors that onto the
    obligation (APPROVED -> CREDITED, credited_at set from the
    Treasury request's own executed_at).

    Never calls approve_treasury_request() or execute_treasury_request()
    — never approves or executes anything. Never creates a
    WalletTransaction/LedgerEntry/BrokerLedger, never calls
    credit_wallet()/debit_wallet(). CREDITED here means Treasury's own,
    already-completed execution already moved the money; this function
    only records that fact on the obligation.

    No `request`/permission required — this is a pure observation step,
    safe to call from an unattended reconciliation task exactly like
    the existing sweep_ib_commission_triggers_task pattern.

    REJECTED / CANCELLED / FAILED linked requests: per the locked 03
    policy, the obligation is left APPROVED and linked, unchanged — no
    wallet credit, no CREDITED transition, no automatic replacement
    request, no automatic obligation cancellation/reversal. The
    "outcome" key on the returned dict reports this terminal state so a
    caller (e.g. the reconciliation task, or a future admin view) can
    surface it for staff attention. Deferred to IB-REVERSALS-FRAUD-05.

    Args:
        obligation: an IBCommissionObligation — only its .pk is used;
                    re-read under select_for_update().

    Returns:
        dict with keys:
          "obligation":      the locked, current IBCommissionObligation
                              instance (unchanged if no transition
                              happened this call).
          "outcome":          one of the OUTCOME_* constants above.
          "treasury_status":  the linked TreasuryOperationRequest's
                              current status, or None if not linked.
    """
    with transaction.atomic():
        locked = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if locked.status == IBCommissionObligation.ST_CREDITED:
            return {
                "obligation": locked, "outcome": OUTCOME_ALREADY_CREDITED,
                "treasury_status": None,
            }

        if locked.status != IBCommissionObligation.ST_APPROVED:
            # PENDING / CANCELLED / REVERSED — nothing for this service
            # to do; not an error, just out of scope for a sync call.
            return {
                "obligation": locked, "outcome": OUTCOME_NOT_APPROVED,
                "treasury_status": None,
            }

        if locked.treasury_operation_id is None:
            return {
                "obligation": locked, "outcome": OUTCOME_NOT_LINKED,
                "treasury_status": None,
            }

        treasury_request = TreasuryOperationRequest.objects.select_for_update().get(
            pk=locked.treasury_operation_id,
        )

        if treasury_request.status in _TREASURY_TERMINAL_NON_SUCCESS_STATUSES:
            return {
                "obligation": locked, "outcome": OUTCOME_TREASURY_TERMINAL_NON_SUCCESS,
                "treasury_status": treasury_request.status,
            }

        if treasury_request.status != TreasuryOperationRequest.ST_EXECUTED:
            # PENDING / APPROVED / EXECUTING — still in progress.
            return {
                "obligation": locked, "outcome": OUTCOME_TREASURY_PENDING_EXECUTION,
                "treasury_status": treasury_request.status,
            }

        locked.status = IBCommissionObligation.ST_CREDITED
        locked.credited_at = treasury_request.executed_at or timezone.now()
        locked.save(update_fields=["status", "credited_at"])
    # ── transaction closed — the CREDITED transition is committed from here on ──

    logger.info(
        "[ib_treasury_settlement] obligation=%d credited (treasury_request=%d amount=%s)",
        locked.pk, treasury_request.pk, treasury_request.amount,
    )
    return {
        "obligation": locked, "outcome": OUTCOME_CREDITED,
        "treasury_status": treasury_request.status,
    }


# ─────────────────────────────────────────────
# Reconciliation — reconcile_approved_obligations()
# ─────────────────────────────────────────────

def reconcile_approved_obligations(batch_size=500):
    """
    Scan APPROVED, Treasury-linked IBCommissionObligation rows and call
    sync_obligation_from_treasury() on each. Never creates a
    TreasuryOperationRequest, never approves or executes one, never
    touches Wallet/WalletTransaction/LedgerEntry/BrokerLedger directly —
    it only observes already-authoritative Treasury state (via
    sync_obligation_from_treasury()) and mirrors it onto the obligation.

    Unbounded lookback (no time cutoff), deliberately — per
    IB-TREASURY-CREDIT-03A §M: this set is naturally small and self-
    limiting (only APPROVED-and-linked rows are ever scanned, not the
    full obligation table), so a stuck obligation must never silently
    age out of visibility the way a fast-growing generation sweep's time
    window would allow.

    Per-row exception isolation — a failure syncing one obligation never
    aborts the batch; it is logged and counted as "skipped", next run
    retries it.
    """
    obligations = list(
        IBCommissionObligation.objects.filter(
            status=IBCommissionObligation.ST_APPROVED,
            treasury_operation__isnull=False,
        ).order_by("id")[:batch_size]
    )
    scanned = len(obligations)
    credited = 0
    skipped = 0
    failed = 0
    for obligation in obligations:
        try:
            result = sync_obligation_from_treasury(obligation)
        except Exception:
            logger.exception(
                "[ib_treasury_settlement] reconcile_approved_obligations failed "
                "for obligation=%d — skipping, next run will retry", obligation.pk,
            )
            skipped += 1
            continue
        outcome = result["outcome"]
        if outcome == OUTCOME_CREDITED:
            credited += 1
        elif outcome == OUTCOME_TREASURY_TERMINAL_NON_SUCCESS:
            failed += 1
        else:
            skipped += 1
    result = {"scanned": scanned, "credited": credited, "skipped": skipped, "failed": failed}
    logger.info("[ib_treasury_settlement] reconcile_approved_obligations %s", result)
    return result
