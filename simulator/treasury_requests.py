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

from . import audit
from . import broker_audit
from .models import TreasuryOperationRequest

TREASURY_SUBMIT_PERMISSION = "simulator.can_submit_treasury_request"


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
