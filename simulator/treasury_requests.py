# simulator/treasury_requests.py
"""
Treasury Private Operations — O.3a-4 — submission service.

Connects, for the first time, the pieces built in O.3a-1 (permission),
O.3a-2 (event catalog) and O.3a-3 (form):

    TreasuryOperationRequestForm
            v
        validation
            v
    creation of TreasuryOperationRequest (status=PENDING)
            v
        AuditLog
            v
        BrokerAuditEvent
            v
        End

This module does not approve, reject, cancel or execute anything, and
never touches Wallet.available_balance / Wallet.pending_balance, never
creates a WalletTransaction or an InternalTransfer, and never calls
credit_wallet() / debit_wallet() / reconcile_wallet() / transfer_to_account()
/ transfer_to_wallet() (wallet_ledger.py, untouched). No financial
operation happens here — only the creation of a request row plus its two
audit writes.

Permission contract (two layers, per O.3a-4's security decision):
this service is not the only place the "simulator.can_submit_treasury_request"
permission must be checked — the future view (O.3a-5+) must check it too,
before it even builds the form. This service checks it independently
and does not trust that the caller already did.

Error contract — reuses Django's own standard exceptions, no custom
exception hierarchy:

  - request.user not authenticated       -> django.core.exceptions.PermissionDenied
  - request.user lacks the permission    -> django.core.exceptions.PermissionDenied
  - form was never validated             -> ValueError
        (form.is_valid()/form.errors was never accessed by the caller —
        detected via the absence of form.cleaned_data, which Django's
        BaseForm.full_clean() only ever sets on a bound form; this is a
        caller/programming error, not a business-rule failure)
  - form was validated and is invalid    -> django.core.exceptions.ValidationError
        (carries form.errors)

In every one of the four error cases above, execution stops before the
transaction.atomic() block below — nothing is created: no
TreasuryOperationRequest, no AuditLog, no BrokerAuditEvent.

Audit ordering: log_audit() and record_payment_event() are called AFTER
the transaction.atomic() block that creates the TreasuryOperationRequest
has already exited (i.e. after that row is committed) — same ordering
already used by views.py::deposit_callback(). Both helpers are fail-open
by their own contract (log_audit() and broker_audit.record_event() each
wrap their entire body in try/except and never raise — verified by
reading their source, O.3a-4 design study §1) — this service does not
add a redundant try/except around them, exactly like deposit_callback()
does not, trusting that same contract.
"""
import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import audit
from . import broker_audit
from .models import TreasuryOperationRequest, WalletTransaction

log = logging.getLogger("simulator.treasury_requests")

TREASURY_SUBMIT_PERMISSION  = "simulator.can_submit_treasury_request"
TREASURY_REVIEW_PERMISSION  = "simulator.can_review_treasury_request"
TREASURY_EXECUTE_PERMISSION = "simulator.can_execute_treasury_request"

# O.3c-2 — frozen mapping decision: reuse the existing WalletTransaction
# tx_type catalog exclusively, no new tx_type. "credit"/"debit" is a
# direction marker resolved against wallet_ledger.credit_wallet()/
# debit_wallet() inside execute_treasury_request() — the two functions
# themselves are imported locally there (never at module level here),
# same discipline already used everywhere else this project calls into
# wallet_ledger.py from outside it.
_EXECUTION_MAPPING = {
    TreasuryOperationRequest.OP_CREDIT_FUNDS:  ("credit", WalletTransaction.TX_CORRECTION),
    TreasuryOperationRequest.OP_DEBIT_FUNDS:   ("debit",  WalletTransaction.TX_CORRECTION),
    TreasuryOperationRequest.OP_REFUND:        ("credit", WalletTransaction.TX_CORRECTION),
    TreasuryOperationRequest.OP_BONUS_CREDIT:  ("credit", WalletTransaction.TX_BONUS),
    TreasuryOperationRequest.OP_IB_COMMISSION: ("credit", WalletTransaction.TX_REBATE),
    TreasuryOperationRequest.OP_MANUAL_CREDIT: ("credit", WalletTransaction.TX_CORRECTION),
    TreasuryOperationRequest.OP_MANUAL_DEBIT:  ("debit",  WalletTransaction.TX_CORRECTION),
}


class TreasuryRequestNotPending(Exception):
    """Raised when review or cancellation is attempted on a
    non-PENDING request."""


class TreasuryRequestSelfReviewDenied(Exception):
    """Raised when requested_by attempts to review their own request."""


class TreasuryRequestNotApproved(Exception):
    """Raised when execution is attempted on a non-APPROVED request."""


class TreasuryRequestSelfExecutionDenied(Exception):
    """Raised when requested_by or approved_by attempts to execute their own request."""


class TreasuryRequestExecutionInconsistent(Exception):
    """
    Raised when a Step B execution attempt finds the request's row no
    longer matches what it expects to own — either the Step B
    revalidation itself (status != EXECUTING, executed_by mismatch, or
    wallet_transaction already linked), or the failure-handling
    conditional update in the except block matching zero rows (the
    financial operation failed AND, by the time the code tried to mark
    the row FAILED, its state had already changed again). In the second
    case this means the row could NOT be confirmed as marked FAILED —
    callers must not assume FAILED was persisted and must treat this as
    requiring operational investigation.
    """


def submit_treasury_request(form, *, request):
    """
    Create a TreasuryOperationRequest from an already-validated
    TreasuryOperationRequestForm, then record it in both audit systems.

    Args:
        form:    a TreasuryOperationRequestForm on which .is_valid() has
                 already been called by the caller (this function checks
                 that it was, it does not call it itself).
        request: the current HttpRequest — request.user must be
                 authenticated and hold TREASURY_SUBMIT_PERMISSION;
                 request is also forwarded to audit.log_audit() for
                 ip/endpoint/method/request_id extraction.

    Returns:
        The created TreasuryOperationRequest instance (status=PENDING).

    Raises:
        PermissionDenied:  request.user is not authenticated, or lacks
                            TREASURY_SUBMIT_PERMISSION.
        ValueError:         form.cleaned_data does not exist yet — the
                            caller must call form.is_valid() first.
        ValidationError:    form.is_valid() is False.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to submit a treasury request.")

    if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

    if not hasattr(form, "cleaned_data"):
        raise ValueError(
            "submit_treasury_request() requires an already-validated form — "
            "call form.is_valid() before calling this function."
        )

    if not form.is_valid():
        raise ValidationError(form.errors)

    with transaction.atomic():
        instance = form.save(commit=False)
        instance.requested_by = request.user
        instance.status = TreasuryOperationRequest.ST_PENDING
        instance.currency = instance.wallet.currency
        instance.wallet_transaction = None
        instance.save()
    # ── transaction closed — the request row is committed from here on ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_SUBMITTED,
        f"Treasury request #{instance.pk} submitted ({instance.operation_type})",
        detail={
            "treasury_request_id": instance.pk,
            "operation_type": instance.operation_type,
            "wallet_id": instance.wallet_id,
            "wallet_user_id": instance.wallet.user_id,
            "amount": str(instance.amount),
            "currency": instance.currency,
            "category": instance.category,
            "reference": instance.reference,
            "has_evidence": bool(instance.evidence),
        },
    )

    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_SUBMITTED,
        severity=broker_audit.Severity.WARNING,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{instance.pk} submitted",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": instance.pk,
            "operation_type": instance.operation_type,
            "wallet_id": instance.wallet_id,
            "wallet_user_id": instance.wallet.user_id,
            "amount": str(instance.amount),
            "currency": instance.currency,
            "status": instance.status,
            "category": instance.category,
            "reference": instance.reference,
            "has_evidence": bool(instance.evidence),
        },
    )

    return instance


# ─────────────────────────────────────────────
# Treasury Request Review Workflow — O.3b-2
#
# approve_treasury_request() / reject_treasury_request() — the two
# terminal-for-this-phase transitions PENDING -> APPROVED and
# PENDING -> REJECTED. Neither moves money: no WalletTransaction or
# InternalTransfer is ever created, Wallet.available_balance /
# Wallet.pending_balance are never touched, and neither
# credit_wallet() / debit_wallet() / reconcile_wallet() /
# transfer_to_account() / transfer_to_wallet() is called
# (wallet_ledger.py, untouched) nor funded_payouts.py. Neither function
# touches executed_by / executed_at / wallet_transaction / cancelled_at,
# and neither transitions status to EXECUTING — that is a later block.
#
# Concurrency pattern mirrors funded_payouts.py::approve_sim_payout()
# (O.3b Fase 0 §Fase 1/3), not the bulk admin-action idiom used by
# approve_kyc()/approve_withdrawals(): this is a single-object service
# meant to be called once per button click, so a lost race raises
# TreasuryRequestNotPending rather than silently no-op'ing — the caller
# (a future view, O.3b-3) needs to know explicitly that nothing happened
# on this call, not assume success.
#
# Permission contract (two layers, same discipline as O.3a-4's
# submit_treasury_request() and the O.3b security decision that
# approved this design): the future UI will hide Approve/Reject for
# ineligible users, but this service re-checks permission AND
# self-review independently — it never trusts that the caller already
# did.
#
# review_notes is optional supplementary commentary from the reviewer.
# It is NOT a model field and NEVER written to
# TreasuryOperationRequest.metadata or any other column — it exists
# only in AuditLog.detail and BrokerAuditEvent.metadata, per the O.3b
# Fase 0 design adjustment approved before this block.
# ─────────────────────────────────────────────

def approve_treasury_request(instance, *, request, review_notes=""):
    """
    Transition a PENDING TreasuryOperationRequest to APPROVED.

    Args:
        instance:      a TreasuryOperationRequest — only its .pk is used;
                        it is re-read under lock, never trusted for
                        status or requested_by (both could be stale).
        request:       the current HttpRequest — request.user must be
                        authenticated and hold TREASURY_REVIEW_PERMISSION,
                        and must not be the request's own requested_by;
                        request is also forwarded to audit.log_audit().
        review_notes:  optional reviewer commentary, stripped; recorded
                        only in AuditLog/BrokerAuditEvent, never on the
                        model.

    Returns:
        The locked, updated TreasuryOperationRequest instance (APPROVED).

    Raises:
        PermissionDenied:              request.user not authenticated,
                                        or lacks TREASURY_REVIEW_PERMISSION.
        TreasuryRequestSelfReviewDenied: request.user is the request's
                                        own requested_by.
        TreasuryRequestNotPending:      the request's current status is
                                        not PENDING (re-checked under lock).
        TreasuryOperationRequest.DoesNotExist: instance.pk no longer
                                        exists — left to propagate; the
                                        future view (O.3b-3) translates
                                        this to a 404.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to review a treasury request.")

    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    review_notes = (review_notes or "").strip()

    with transaction.atomic():
        locked = TreasuryOperationRequest.objects.select_for_update().get(pk=instance.pk)

        if locked.status != TreasuryOperationRequest.ST_PENDING:
            raise TreasuryRequestNotPending(
                f"TreasuryOperationRequest #{locked.pk} is not pending (status={locked.status})."
            )
        if locked.requested_by_id == request.user.pk:
            raise TreasuryRequestSelfReviewDenied(
                f"User #{request.user.pk} cannot review their own treasury request #{locked.pk}."
            )

        previous_status = locked.status
        locked.status = TreasuryOperationRequest.ST_APPROVED
        locked.approved_by = request.user
        locked.approved_at = timezone.now()
        locked.save(update_fields=["status", "approved_by", "approved_at"])
    # ── transaction closed — the APPROVED transition is committed from here on ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_APPROVED,
        f"Treasury request #{locked.pk} approved",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "review_notes": review_notes,
            "previous_status": previous_status,
            "new_status": locked.status,
        },
    )

    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_APPROVED,
        severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} approved",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "review_notes": review_notes,
            "previous_status": previous_status,
            "status": locked.status,
        },
    )

    return locked


def reject_treasury_request(instance, rejection_reason, *, request, review_notes=""):
    """
    Transition a PENDING TreasuryOperationRequest to REJECTED.

    Args:
        instance:          a TreasuryOperationRequest — only its .pk is
                            used; re-read under lock, same discipline as
                            approve_treasury_request().
        rejection_reason:  required, stripped; ValueError if empty after
                            stripping — checked BEFORE any lock is
                            acquired or any row is touched.
        request:           same contract as approve_treasury_request().
        review_notes:      optional, same contract as
                            approve_treasury_request() — separate from
                            rejection_reason, never on the model.

    Returns:
        The locked, updated TreasuryOperationRequest instance (REJECTED).

    Raises:
        ValueError:                     rejection_reason is empty/blank.
        PermissionDenied:               same as approve_treasury_request().
        TreasuryRequestSelfReviewDenied: same as approve_treasury_request().
        TreasuryRequestNotPending:       same as approve_treasury_request().
        TreasuryOperationRequest.DoesNotExist: same as
                                        approve_treasury_request().
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to review a treasury request.")

    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

    rejection_reason = (rejection_reason or "").strip()
    if not rejection_reason:
        raise ValueError("rejection_reason is required to reject a treasury request.")

    review_notes = (review_notes or "").strip()

    with transaction.atomic():
        locked = TreasuryOperationRequest.objects.select_for_update().get(pk=instance.pk)

        if locked.status != TreasuryOperationRequest.ST_PENDING:
            raise TreasuryRequestNotPending(
                f"TreasuryOperationRequest #{locked.pk} is not pending (status={locked.status})."
            )
        if locked.requested_by_id == request.user.pk:
            raise TreasuryRequestSelfReviewDenied(
                f"User #{request.user.pk} cannot review their own treasury request #{locked.pk}."
            )

        previous_status = locked.status
        locked.status = TreasuryOperationRequest.ST_REJECTED
        locked.rejected_by = request.user
        locked.rejected_at = timezone.now()
        locked.rejection_reason = rejection_reason
        locked.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])
    # ── transaction closed — the REJECTED transition is committed from here on ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_REJECTED,
        f"Treasury request #{locked.pk} rejected",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "rejected_by_id": locked.rejected_by_id,
            "rejection_reason": locked.rejection_reason,
            "review_notes": review_notes,
            "previous_status": previous_status,
            "new_status": locked.status,
        },
    )

    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_REJECTED,
        severity=broker_audit.Severity.WARNING,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} rejected",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "rejected_by_id": locked.rejected_by_id,
            "rejection_reason": locked.rejection_reason,
            "review_notes": review_notes,
            "previous_status": previous_status,
            "status": locked.status,
        },
    )

    return locked


# ─────────────────────────────────────────────
# Treasury Cancel Workflow — O.3e-2
#
# cancel_treasury_request() — the only transition PENDING -> CANCELLED.
# Frozen Fase 0 decisions (O.3e-1 approved):
#   1/2. No cancellation_reason field exists on TreasuryOperationRequest
#        — any reason text is recorded exclusively in this event's own
#        AuditLog/BrokerAuditEvent detail/metadata, never on the model
#        (same discipline O.2g-1a's cancelled_at comment already
#        documents for "who": actor recorded via audit trail only).
#   3/4. Two, and only two, actors may produce this transition:
#        (a) requested_by, withdrawing their own still-PENDING request
#            (self-withdrawal — no self-conflict concept applies here,
#            unlike approve/reject: this IS the intended self-action);
#        (b) anyone else holding can_review_treasury_request,
#            administratively cancelling someone else's PENDING request.
#        Which permission is actually required depends on
#        locked.requested_by_id, itself a value of the row being
#        cancelled — so, unlike approve_treasury_request()/
#        reject_treasury_request() (whose single fixed permission is
#        checked before the lock is even acquired), the permission
#        dispatch here is necessarily resolved INSIDE the lock, right
#        after re-reading the row, never trusting instance.requested_by_id
#        from the possibly-stale caller-supplied instance. The
#        underlying discipline — never check anything security-relevant
#        against a value that could be stale — is identical to O.3a/
#        O.3b/O.3c; only the sequencing differs, and only because it
#        must.
#   5.   No new permission — reuses TREASURY_SUBMIT_PERMISSION (self-
#        withdrawal) and TREASURY_REVIEW_PERMISSION (administrative),
#        both already defined above.
#   6/7/8/9. Never imports wallet_ledger.py, never calls
#        execute_treasury_request() or mark_treasury_execution_failed(),
#        never touches treasury_engine — PENDING is the one status that
#        is guaranteed to have never had a wallet_transaction, so this
#        transition cannot move money by construction.
# ─────────────────────────────────────────────

def cancel_treasury_request(instance, *, request, cancellation_reason=""):
    """
    Transition a PENDING TreasuryOperationRequest to CANCELLED.

    Args:
        instance:             a TreasuryOperationRequest — only its .pk
                               is used; re-read under lock, never
                               trusted for status or requested_by (both
                               could be stale).
        request:               the current HttpRequest — request.user
                               must be authenticated, and must be EITHER
                               the request's own requested_by (holding
                               TREASURY_SUBMIT_PERMISSION) OR any other
                               user holding TREASURY_REVIEW_PERMISSION;
                               request is also forwarded to
                               audit.log_audit().
        cancellation_reason:  optional, stripped; recorded only in
                               AuditLog/BrokerAuditEvent (metadata key
                               "cancellation_reason") — never on the
                               model, per Fase 0 Decision 1/2. Never
                               required (unlike reject_treasury_
                               request()'s rejection_reason) — a future
                               UI block (O.3e-3) may still choose to
                               require it for the administrative path
                               only; this service does not enforce that.

    Returns:
        The locked, updated TreasuryOperationRequest instance (CANCELLED).

    Raises:
        PermissionDenied:               request.user not authenticated,
                                         or (depending on which actor
                                         this call resolves to) lacks
                                         TREASURY_SUBMIT_PERMISSION or
                                         TREASURY_REVIEW_PERMISSION.
        TreasuryRequestNotPending:       the request's current status is
                                         not PENDING (re-checked under
                                         lock).
        TreasuryOperationRequest.DoesNotExist: instance.pk no longer
                                         exists — left to propagate; the
                                         future view (O.3e-3) translates
                                         this to a 404.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to cancel a treasury request.")

    cancellation_reason = (cancellation_reason or "").strip()

    with transaction.atomic():
        locked = TreasuryOperationRequest.objects.select_for_update().get(pk=instance.pk)

        if locked.status != TreasuryOperationRequest.ST_PENDING:
            raise TreasuryRequestNotPending(
                f"TreasuryOperationRequest #{locked.pk} is not pending (status={locked.status})."
            )

        is_self_withdrawal = locked.requested_by_id == request.user.pk
        if is_self_withdrawal:
            if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
                raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")
            cancelled_by_role = "requester"
        else:
            if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
                raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")
            cancelled_by_role = "supervisor"

        previous_status = locked.status
        locked.status = TreasuryOperationRequest.ST_CANCELLED
        locked.cancelled_at = timezone.now()
        locked.save(update_fields=["status", "cancelled_at"])
    # ── transaction closed — the CANCELLED transition is committed from here on ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_CANCELLED,
        f"Treasury request #{locked.pk} cancelled ({cancelled_by_role})",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "cancelled_by_id": request.user.pk,
            "cancelled_by_role": cancelled_by_role,
            "cancellation_reason": cancellation_reason,
            "previous_status": previous_status,
            "new_status": locked.status,
        },
    )

    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_CANCELLED,
        severity=(
            broker_audit.Severity.INFO if cancelled_by_role == "requester"
            else broker_audit.Severity.WARNING
        ),
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} cancelled ({cancelled_by_role})",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "cancelled_by_id": request.user.pk,
            "cancelled_by_role": cancelled_by_role,
            "cancellation_reason": cancellation_reason,
            "previous_status": previous_status,
            "status": locked.status,
        },
    )

    return locked


# ─────────────────────────────────────────────
# Treasury Request Execution Engine — O.3c-3
#
# execute_treasury_request() — the only transition that moves real
# money: APPROVED -> EXECUTING -> EXECUTED, or APPROVED -> EXECUTING ->
# FAILED. Every credit_wallet()/debit_wallet() call and every tx_type
# choice follows the frozen O.3c-2 mapping (_EXECUTION_MAPPING above)
# exactly — no new tx_type, no direction field (O.3c-0), no
# WalletTransaction ever edited or deleted, no InternalTransfer ever
# created (this is a pure Wallet-internal correction/credit, never a
# Wallet<->TradingAccount transfer).
#
# Two separate transaction.atomic() blocks (Step A / Step B), mirroring
# wallet_ledger.py::transfer_to_account()'s own pattern — not a single
# atomic block — so that a durable EXECUTING marker survives a process
# crash between the two steps (O.3c Fase 0 §Fase 4/5): Step A commits
# APPROVED -> EXECUTING alone; Step B is the only place that ever calls
# credit_wallet()/debit_wallet() and the only place that ever sets
# wallet_transaction, always together with the EXECUTED transition, in
# the same atomic() — so wallet_transaction IS NULL is always a 100%
# reliable signal that no money moved for this request.
#
# Step B revalidation (O.3c-3 security amendment #1, required before
# this block was authorized): Step A's checks are NOT trusted blindly.
# Step B re-locks the row and independently reconfirms status ==
# EXECUTING, executed_by_id == request.user.pk, and wallet_transaction_id
# is None, BEFORE calling credit_wallet()/debit_wallet(). Any mismatch
# raises TreasuryRequestExecutionInconsistent and touches no money.
#
# Failure handling (O.3c-3 security amendment #2, required before this
# block was authorized): the except branch never does an unconditional
# UPDATE by pk. It uses a conditional UPDATE scoped to
# (pk, status=EXECUTING, wallet_transaction__isnull=True,
# executed_by=request.user) and inspects the number of rows actually
# updated. Only when exactly one row was updated does this function
# treat the request as durably marked FAILED and emit
# EXECUTION_FAILED — if zero rows matched, the row's state had already
# changed out from under this execution attempt; no FAILED transition
# can be claimed to have happened, no EXECUTION_FAILED event is emitted
# (that would be reporting a persisted fact that never actually
# persisted), and TreasuryRequestExecutionInconsistent is raised
# instead, chained via `from` onto the original financial exception so
# both are visible for operational investigation.
# ─────────────────────────────────────────────

def execute_treasury_request(instance, *, request, execution_notes=""):
    """
    Transition an APPROVED TreasuryOperationRequest to EXECUTED (moving
    real money via wallet_ledger.credit_wallet()/debit_wallet()) or, on
    financial failure, to FAILED.

    Args:
        instance:         a TreasuryOperationRequest — only its .pk is
                           used; re-read under lock in both steps, never
                           trusted for status/requested_by/approved_by/
                           executed_by/wallet_transaction (all could be
                           stale).
        request:          the current HttpRequest — request.user must be
                           authenticated and hold
                           TREASURY_EXECUTE_PERMISSION, and must be
                           neither the request's requested_by nor its
                           approved_by; request is also forwarded to
                           audit.log_audit().
        execution_notes:  optional executor commentary, stripped;
                           recorded only in AuditLog/BrokerAuditEvent,
                           never on the model and never merged into the
                           WalletTransaction.note text.

    Returns:
        The locked, updated TreasuryOperationRequest instance (EXECUTED).

    Raises:
        PermissionDenied:                request.user not authenticated,
                                          or lacks TREASURY_EXECUTE_PERMISSION.
        TreasuryRequestSelfExecutionDenied: request.user is the request's
                                          own requested_by or approved_by.
        TreasuryRequestNotApproved:       the request's status is not
                                          APPROVED (Step A recheck under
                                          lock).
        TreasuryRequestExecutionInconsistent: the Step B revalidation
                                          found status/executed_by/
                                          wallet_transaction no longer
                                          matching this execution
                                          attempt, or the failure-path
                                          conditional UPDATE matched
                                          zero rows.
        InsufficientFunds / ValueError:  propagated from debit_wallet()/
                                          credit_wallet() — the request
                                          is marked FAILED first (when
                                          the conditional UPDATE
                                          succeeds), then this is
                                          re-raised.
        TreasuryOperationRequest.DoesNotExist: instance.pk no longer
                                          exists — left to propagate;
                                          the future view (O.3c-5)
                                          translates this to a 404.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to execute a treasury request.")

    if not request.user.has_perm(TREASURY_EXECUTE_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_EXECUTE_PERMISSION}")

    execution_notes = (execution_notes or "").strip()

    # ── Step A — durable EXECUTING marker, own atomic block, commits alone ──
    with transaction.atomic():
        locked = TreasuryOperationRequest.objects.select_for_update().get(pk=instance.pk)

        if locked.status != TreasuryOperationRequest.ST_APPROVED:
            raise TreasuryRequestNotApproved(
                f"TreasuryOperationRequest #{locked.pk} is not approved (status={locked.status})."
            )
        if locked.requested_by_id == request.user.pk or locked.approved_by_id == request.user.pk:
            raise TreasuryRequestSelfExecutionDenied(
                f"User #{request.user.pk} cannot execute treasury request #{locked.pk} "
                "they themselves requested or approved."
            )

        locked.status = TreasuryOperationRequest.ST_EXECUTING
        locked.executed_by = request.user
        locked.wallet_transaction = None
        locked.save(update_fields=["status", "executed_by", "wallet_transaction"])
    # ── EXECUTING is committed and durable from here on ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        f"Treasury request #{locked.pk} execution started",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "previous_status": TreasuryOperationRequest.ST_APPROVED,
            "new_status": TreasuryOperationRequest.ST_EXECUTING,
        },
    )
    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        severity=broker_audit.Severity.INFO,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} execution started",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "status": TreasuryOperationRequest.ST_EXECUTING,
        },
    )

    # ── Step B — the only place that moves money, own atomic block ──
    from .wallet_ledger import credit_wallet, debit_wallet

    try:
        with transaction.atomic():
            locked = TreasuryOperationRequest.objects.select_for_update().get(pk=instance.pk)

            # Security amendment #1 — never trust Step A's checks alone.
            # Independently reconfirm this execution attempt still owns
            # this row before touching any money.
            if locked.status != TreasuryOperationRequest.ST_EXECUTING:
                raise TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: expected status "
                    f"EXECUTING at Step B, found {locked.status}."
                )
            if locked.executed_by_id != request.user.pk:
                raise TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: expected executed_by="
                    f"{request.user.pk} at Step B, found {locked.executed_by_id}."
                )
            if locked.wallet_transaction_id is not None:
                raise TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: wallet_transaction "
                    f"already linked (#{locked.wallet_transaction_id}) — refusing "
                    "to move money a second time."
                )

            direction, tx_type = _EXECUTION_MAPPING[locked.operation_type]
            op_function = credit_wallet if direction == "credit" else debit_wallet
            note = (
                f"Treasury Request #{locked.pk} — {locked.get_operation_type_display()} — "
                f"{locked.reference or locked.reason}"
            )

            wtx = op_function(
                locked.wallet_id, locked.amount, tx_type,
                note=note, initiated_by=request.user,
            )

            locked.wallet_transaction = wtx
            locked.status = TreasuryOperationRequest.ST_EXECUTED
            locked.executed_at = timezone.now()
            locked.save(update_fields=["wallet_transaction", "status", "executed_at"])
    except Exception as exc:
        # Security amendment #2 — never an unconditional update by pk.
        # Only claim FAILED was persisted if the conditional UPDATE
        # actually matched (and therefore updated) exactly this row,
        # still owned by this execution attempt, with no money moved.
        updated_rows = TreasuryOperationRequest.objects.filter(
            pk=instance.pk,
            status=TreasuryOperationRequest.ST_EXECUTING,
            wallet_transaction__isnull=True,
            executed_by=request.user,
        ).update(
            status=TreasuryOperationRequest.ST_FAILED,
            failure_reason=str(exc)[:256],
            executed_at=timezone.now(),
        )

        if updated_rows == 0:
            log.error(
                "[treasury_requests] execution failure for TreasuryOperationRequest "
                "#%s could NOT be persisted as FAILED — row no longer matched the "
                "expected state (status=EXECUTING, wallet_transaction=None, "
                "executed_by=%s); original error: %r. Requires operational "
                "investigation — do not assume FAILED was recorded.",
                instance.pk, request.user.pk, exc, exc_info=True,
            )
            raise TreasuryRequestExecutionInconsistent(
                f"TreasuryOperationRequest #{instance.pk}: execution failed "
                f"({exc!r}) but could not be marked FAILED — its state changed "
                "unexpectedly before the failure could be persisted. Requires "
                "operational investigation."
            ) from exc

        audit.log_audit(
            request, audit.EV_TREASURY_REQUEST_EXECUTION_FAILED,
            f"Treasury request #{instance.pk} execution failed",
            detail={
                "treasury_request_id": instance.pk,
                "operation_type": locked.operation_type,
                "wallet_id": locked.wallet_id,
                "wallet_user_id": locked.wallet.user_id,
                "amount": str(locked.amount),
                "requested_by_id": locked.requested_by_id,
                "approved_by_id": locked.approved_by_id,
                "executed_by_id": locked.executed_by_id,
                "previous_status": TreasuryOperationRequest.ST_EXECUTING,
                "new_status": TreasuryOperationRequest.ST_FAILED,
                "failure_reason": str(exc)[:256],
            },
        )
        broker_audit.record_payment_event(
            event_type=broker_audit.EV_TREASURY_REQUEST_EXECUTION_FAILED,
            severity=broker_audit.Severity.HIGH,
            actor_type=broker_audit.ActorType.STAFF,
            actor_id=request.user.pk,
            description=f"Treasury request #{instance.pk} execution failed",
            source_module="simulator.treasury_requests",
            metadata={
                "treasury_operation_request_id": instance.pk,
                "operation_type": locked.operation_type,
                "wallet_id": locked.wallet_id,
                "wallet_user_id": locked.wallet.user_id,
                "amount": str(locked.amount),
                "requested_by_id": locked.requested_by_id,
                "approved_by_id": locked.approved_by_id,
                "executed_by_id": locked.executed_by_id,
                "status": TreasuryOperationRequest.ST_FAILED,
                "failure_reason": str(exc)[:256],
            },
        )
        raise

    # ── transaction closed — EXECUTED + wallet_transaction are committed together ──

    audit.log_audit(
        request, audit.EV_TREASURY_REQUEST_EXECUTED,
        f"Treasury request #{locked.pk} executed",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "wallet_transaction_id": locked.wallet_transaction_id,
            "tx_type": locked.wallet_transaction.tx_type,
            "execution_notes": execution_notes,
            "previous_status": TreasuryOperationRequest.ST_EXECUTING,
            "new_status": TreasuryOperationRequest.ST_EXECUTED,
        },
    )
    broker_audit.record_payment_event(
        event_type=broker_audit.EV_TREASURY_REQUEST_EXECUTED,
        severity=broker_audit.Severity.WARNING,
        actor_type=broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} executed",
        source_module="simulator.treasury_requests",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "wallet_transaction_id": locked.wallet_transaction_id,
            "tx_type": locked.wallet_transaction.tx_type,
            "execution_notes": execution_notes,
            "status": TreasuryOperationRequest.ST_EXECUTED,
        },
    )

    return locked
