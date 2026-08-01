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
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import audit
from . import broker_audit
from .models import TreasuryOperationRequest

TREASURY_SUBMIT_PERMISSION = "simulator.can_submit_treasury_request"
TREASURY_REVIEW_PERMISSION = "simulator.can_review_treasury_request"


class TreasuryRequestNotPending(Exception):
    """Raised when review is attempted on a non-PENDING request."""


class TreasuryRequestSelfReviewDenied(Exception):
    """Raised when requested_by attempts to review their own request."""


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
