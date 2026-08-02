# simulator/treasury_execution_recovery.py
"""
Treasury Private Operations — O.3c-4b — recovery inspection service.

inspect_stuck_treasury_execution() is a strictly read-only diagnostic:
it detects TreasuryOperationRequest rows stuck in EXECUTING (candidates
for a future recovery action) and classifies each one. It NEVER writes:
no .save(), no .update(), no .delete(), no AuditLog row, no
BrokerAuditEvent row, no status/failure_reason/executed_at change, and
it never touches Wallet or WalletTransaction. It does not import
credit_wallet, debit_wallet, transfer_to_account, transfer_to_wallet or
reconcile_wallet (wallet_ledger.py is not imported at all here) — this
module cannot move money even by accident, structurally, because it has
no code path that calls into wallet_ledger.py.

mark_treasury_execution_failed() (O.3c-4c) is this module's only
mutable action — the sole way a stuck EXECUTING request is ever
resolved. It ONLY ever transitions EXECUTING -> FAILED. "Reset to
APPROVED" and "Reconcile from wallet_transaction" were both evaluated
and explicitly rejected in the O.3c-4 Fase 0 design (approved) and do
not exist anywhere in this module. Like inspect_stuck_treasury_
execution(), it never imports or calls credit_wallet(), debit_wallet(),
transfer_to_account(), transfer_to_wallet() or reconcile_wallet(), and
it never creates a WalletTransaction or InternalTransfer — recovery is
incident response for a row that never moved money, never a financial
operation itself.

── Why not updated_at / executed_at ────────────────────────────────
Confirmed empirically during Fase 0: TreasuryOperationRequest.updated_at
(auto_now=True) is never included in any update_fields=[...] list used
by treasury_requests.py, so Django never writes it to the DB on any of
those saves — it stays frozen from row creation for the entire life of
the request and cannot signal "how long has this been EXECUTING".
executed_at is only ever set by execute_treasury_request()'s Step B
(on success) or by its except-block (on failure) — a row genuinely
stuck in EXECUTING has executed_at IS NULL by construction. The only
trustworthy signal for "when did this specific EXECUTING attempt
begin" is the EXECUTION_STARTED audit event (audit.py's
EV_TREASURY_REQUEST_EXECUTION_STARTED), written immediately after Step
A's own transaction.atomic() commits.

── Query strategy (no N+1) ─────────────────────────────────────────
Exactly 3 queries total, regardless of how many EXECUTING rows exist:
  1. TreasuryOperationRequest.objects.filter(status=EXECUTING)
     .select_related("executed_by") — avoids one query per row for the
     executed_by.is_active check (Case F).
  2. One AuditLog query covering EXECUTION_STARTED/EXECUTED/
     EXECUTION_FAILED for every candidate pk at once
     (detail__treasury_request_id__in=[...]), grouped into a dict in
     Python.
  3. The same shape query against BrokerAuditEvent
     (metadata__treasury_operation_request_id__in=[...] — note the
     different key name; AuditLog.detail and BrokerAuditEvent.metadata
     have never used the same key for this field, confirmed across
     every call site in treasury_requests.py).
"""
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from . import audit as _audit
from . import broker_audit as _broker_audit
from .models import AuditLog, BrokerAuditEvent, TreasuryOperationRequest
from .treasury_requests import TreasuryRequestExecutionInconsistent

TREASURY_RECOVER_PERMISSION = "simulator.can_recover_treasury_execution"

CASE_A = "CASE_A"  # EXECUTING, wallet_transaction NULL, age confirmed >= threshold, clean
CASE_B = "CASE_B"  # EXECUTING, wallet_transaction exists — structurally anomalous, never eligible
CASE_C = "CASE_C"  # EXECUTING, age confirmed but below threshold — possibly still in flight
CASE_D = "CASE_D"  # EXECUTING, age UNKNOWN (no STARTED event found anywhere), otherwise clean
CASE_E = "CASE_E"  # EXECUTING, but an EXECUTED/FAILED event already exists — audit inconsistency
CASE_F = "CASE_F"  # EXECUTING, executed_by missing or inactive (does not by itself block eligibility)

AGE_CONFIDENCE_AUDIT_LOG          = "AUDIT_LOG"
AGE_CONFIDENCE_BROKER_AUDIT_EVENT = "BROKER_AUDIT_EVENT"
AGE_CONFIDENCE_UNKNOWN            = "UNKNOWN"


@dataclass(frozen=True)
class StuckExecutionCandidate:
    """
    One EXECUTING TreasuryOperationRequest, classified. Immutable —
    this is a read-only report row, never a handle for mutation.
    """
    instance: TreasuryOperationRequest
    case: str
    age_seconds: Optional[float]
    age_confidence: str
    eligible: bool
    block_reason: Optional[str]
    has_wallet_transaction: bool
    executed_by_is_active: Optional[bool]  # None when executed_by is missing entirely
    has_started_event: bool
    has_executed_event: bool
    has_failed_event: bool


class TreasuryRequestSelfRecoveryDenied(Exception):
    """Raised when requested_by or approved_by attempts to recover their own request."""


def _validate_min_age_seconds(min_age_seconds):
    if min_age_seconds is None:
        min_age_seconds = settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS

    if isinstance(min_age_seconds, bool) or not isinstance(min_age_seconds, int):
        raise ValueError(
            f"min_age_seconds must be an int, got {min_age_seconds!r} "
            f"({type(min_age_seconds).__name__})"
        )
    if min_age_seconds <= 0:
        raise ValueError(f"min_age_seconds must be greater than zero, got {min_age_seconds!r}")

    return min_age_seconds


def _collect_signals(model, pk_lookup, timestamp_field, pks):
    """
    One query against `model`, filtered to the three execution event
    types and the given set of TreasuryOperationRequest pks, grouped in
    Python into {pk: {"started": datetime|None, "executed": bool, "failed": bool}}.
    """
    rows = model.objects.filter(
        event_type__in=(
            _audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
            _audit.EV_TREASURY_REQUEST_EXECUTED,
            _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED,
        ),
        **{f"{pk_lookup}__in": pks},
    ).values("event_type", pk_lookup.split("__")[0], timestamp_field)

    signals = {pk: {"started": None, "executed": False, "failed": False} for pk in pks}
    detail_field = pk_lookup.split("__")[0]
    detail_key = pk_lookup.split("__", 1)[1]

    for row in rows:
        pk = row[detail_field].get(detail_key)
        if pk not in signals:
            continue
        event_type = row["event_type"]
        ts = row[timestamp_field]
        if event_type == _audit.EV_TREASURY_REQUEST_EXECUTION_STARTED:
            if signals[pk]["started"] is None or ts < signals[pk]["started"]:
                signals[pk]["started"] = ts
        elif event_type == _audit.EV_TREASURY_REQUEST_EXECUTED:
            signals[pk]["executed"] = True
        elif event_type == _audit.EV_TREASURY_REQUEST_EXECUTION_FAILED:
            signals[pk]["failed"] = True

    return signals


def _classify(instance, audit_signal, broker_signal, min_age_seconds, now):
    has_wallet_transaction = instance.wallet_transaction_id is not None

    executed_by = instance.executed_by
    executed_by_is_active = executed_by.is_active if executed_by is not None else None
    executed_by_missing_or_inactive = executed_by is None or executed_by_is_active is False

    has_executed_event = audit_signal["executed"] or broker_signal["executed"]
    has_failed_event    = audit_signal["failed"]   or broker_signal["failed"]

    # AuditLog is the more authoritative source (Fase 1 finding) — only
    # fall back to BrokerAuditEvent's own independent STARTED write
    # when AuditLog's own fail-open write never persisted.
    if audit_signal["started"] is not None:
        age_confidence = AGE_CONFIDENCE_AUDIT_LOG
        started_ts = audit_signal["started"]
    elif broker_signal["started"] is not None:
        age_confidence = AGE_CONFIDENCE_BROKER_AUDIT_EVENT
        started_ts = broker_signal["started"]
    else:
        age_confidence = AGE_CONFIDENCE_UNKNOWN
        started_ts = None

    has_started_event = started_ts is not None
    age_seconds = (now - started_ts).total_seconds() if started_ts is not None else None

    # ── case label — precedence order, first match wins, so a row
    # never falls into two cases at once. Most structurally severe /
    # trust-breaking conditions are checked first; the age split is
    # only reached once wallet_transaction/audit-consistency/executor
    # concerns are ruled out. CASE_F is informational (Fase 0: it does
    # not by itself change financial safety) and therefore only takes
    # the label after B/E, but ahead of the age-based buckets.
    if has_wallet_transaction:
        case = CASE_B
    elif has_executed_event or has_failed_event:
        case = CASE_E
    elif executed_by_missing_or_inactive:
        case = CASE_F
    elif age_confidence == AGE_CONFIDENCE_UNKNOWN:
        case = CASE_D
    elif age_seconds < min_age_seconds:
        case = CASE_C
    else:
        case = CASE_A

    # ── eligibility — computed independently from the case label,
    # strictly from the 7-point checklist O.3c-4 Fase 0 approved.
    # executed_by status is deliberately NOT one of the conditions
    # (Case F does not by itself change financial safety), so a
    # CASE_F row CAN still be eligible=True.
    block_reasons = []
    if has_wallet_transaction:
        block_reasons.append(
            "wallet_transaction is already linked — this should be structurally "
            "impossible; requires manual investigation, never automatic recovery"
        )
    if has_executed_event:
        block_reasons.append(
            "an EXECUTED audit event already exists for this request while its "
            "status is still EXECUTING — audit trail inconsistency"
        )
    if has_failed_event:
        block_reasons.append(
            "a FAILED audit event already exists for this request while its "
            "status is still EXECUTING — audit trail inconsistency"
        )
    if not block_reasons:
        if age_confidence == AGE_CONFIDENCE_UNKNOWN:
            block_reasons.append(
                f"no {_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED} audit event "
                "found in AuditLog or BrokerAuditEvent — age cannot be confirmed; "
                "requires manual review, recovery is never auto-authorized with "
                "unknown age"
            )
        elif age_seconds < min_age_seconds:
            block_reasons.append(
                f"only {age_seconds:.0f}s elapsed since EXECUTION_STARTED — below "
                f"the {min_age_seconds}s minimum age threshold"
            )

    eligible = not block_reasons
    block_reason = "; ".join(block_reasons) if block_reasons else None

    return StuckExecutionCandidate(
        instance=instance,
        case=case,
        age_seconds=age_seconds,
        age_confidence=age_confidence,
        eligible=eligible,
        block_reason=block_reason,
        has_wallet_transaction=has_wallet_transaction,
        executed_by_is_active=executed_by_is_active,
        has_started_event=has_started_event,
        has_executed_event=has_executed_event,
        has_failed_event=has_failed_event,
    )


def inspect_stuck_treasury_execution(*, min_age_seconds=None):
    """
    Read-only. Returns a list[StuckExecutionCandidate], one per
    TreasuryOperationRequest currently in status=EXECUTING, ordered
    deterministically by pk ascending.

    Args:
        min_age_seconds: minimum age (seconds) a request must have
            spent in EXECUTING (measured from its EXECUTION_STARTED
            audit event) before it is even considered old enough to
            recover. Defaults to
            settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS. Must
            be a plain int > 0.

    Raises:
        ValueError: min_age_seconds is not an int, or is <= 0.

    Never writes to the database. Never emits an AuditLog or
    BrokerAuditEvent row. Never imports or calls credit_wallet(),
    debit_wallet(), transfer_to_account(), transfer_to_wallet() or
    reconcile_wallet().
    """
    min_age_seconds = _validate_min_age_seconds(min_age_seconds)

    candidates = list(
        TreasuryOperationRequest.objects
        .filter(status=TreasuryOperationRequest.ST_EXECUTING)
        .select_related("executed_by")
        .order_by("pk")
    )
    if not candidates:
        return []

    pks = [c.pk for c in candidates]

    audit_signals = _collect_signals(AuditLog, "detail__treasury_request_id", "created_at", pks)
    broker_signals = _collect_signals(
        BrokerAuditEvent, "metadata__treasury_operation_request_id", "timestamp", pks,
    )

    now = timezone.now()
    return [
        _classify(instance, audit_signals[instance.pk], broker_signals[instance.pk], min_age_seconds, now)
        for instance in candidates
    ]


# ─────────────────────────────────────────────
# Treasury Execution Recovery — O.3c-4c
#
# mark_treasury_execution_failed() — the only mutable recovery action.
# It transitions a stuck EXECUTING request straight to FAILED. It never
# resets to APPROVED, never touches wallet_transaction, never calls
# credit_wallet()/debit_wallet(). Reuses inspect_stuck_treasury_
# execution() twice: once before any lock is taken (a cheap, fail-fast
# gate — reject immediately if the row was never a legitimate
# candidate, no audit event emitted), and once again AFTER the row has
# been select_for_update()'d, to reconfirm full eligibility (age/
# EXECUTED/FAILED events) against data nobody else could be
# concurrently mutating anymore. On top of that reused classification,
# three explicit checks (status/wallet_transaction/executed_by) mirror
# execute_treasury_request()'s own Step B revalidation discipline
# (O.3c-3, Ajuste 1), and the actual mutation is always a conditional
# UPDATE with a row-count check — never an unconditional .save() — same
# belt-and-suspenders discipline as O.3c-3's Ajuste 2, even though the
# preceding rechecks already run under the same lock.
#
# RECOVERY_STARTED is only ever emitted after the pre-lock gate has
# already confirmed EXECUTING + eligible=True — never for a row that
# was never a legitimate candidate. From that point on, the code
# guarantees exactly one of RECOVERY_BLOCKED or EXECUTION_MARKED_FAILED
# follows — there is no path where STARTED fires and neither terminal
# event does.
# ─────────────────────────────────────────────

def mark_treasury_execution_failed(instance, *, request, recovery_reason):
    """
    Transition a stuck EXECUTING TreasuryOperationRequest to FAILED.
    The only mutable Treasury Execution Recovery action (O.3c-4c) —
    never resets to APPROVED, never reconciles a WalletTransaction,
    never moves money.

    Args:
        instance:         a TreasuryOperationRequest — only its .pk is
                           used; re-inspected and re-locked, never
                           trusted for status/wallet_transaction/
                           executed_by (all could be stale).
        request:          the current HttpRequest — request.user must
                           be authenticated and hold
                           TREASURY_RECOVER_PERMISSION, and must be
                           neither the request's requested_by nor its
                           approved_by (may be its own executed_by).
        recovery_reason:  required, stripped; ValueError if empty.
                           Persisted (prefixed) into failure_reason and
                           recorded verbatim in AuditLog/BrokerAuditEvent.

    Returns:
        The locked, updated TreasuryOperationRequest instance (FAILED).

    Raises:
        PermissionDenied:                  request.user not authenticated,
                                            or lacks TREASURY_RECOVER_PERMISSION.
        ValueError:                        recovery_reason is empty/blank.
        TreasuryRequestSelfRecoveryDenied: request.user is the request's
                                            own requested_by or approved_by.
        TreasuryRequestExecutionInconsistent: the request is not currently
                                            EXECUTING, is not eligible per
                                            inspect_stuck_treasury_execution()
                                            (pre-lock or post-lock), or the
                                            conditional UPDATE failed to
                                            match exactly one row.
        TreasuryOperationRequest.DoesNotExist: instance.pk no longer exists.
        django.db.Error:                   the row is locked by another
                                            in-flight process
                                            (select_for_update(nowait=True)
                                            contention) — propagated
                                            unwrapped; callers should treat
                                            this as "still actively
                                            processing, try again shortly".

    Never calls credit_wallet(), debit_wallet(), transfer_to_account(),
    transfer_to_wallet() or reconcile_wallet(). Never creates or touches
    a WalletTransaction or InternalTransfer. Never resets status to
    APPROVED.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required to recover a stuck treasury execution.")

    if not request.user.has_perm(TREASURY_RECOVER_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_RECOVER_PERMISSION}")

    recovery_reason = (recovery_reason or "").strip()
    if not recovery_reason:
        raise ValueError("recovery_reason is required to recover a stuck treasury execution.")

    # ── Pre-lock gate — reuses inspect_stuck_treasury_execution() as-is,
    # never reimplements its classification logic. Fails fast, no audit
    # event, if the row was never a legitimate recovery candidate.
    pre_lock_candidate = next(
        (r for r in inspect_stuck_treasury_execution() if r.instance.pk == instance.pk), None,
    )
    if pre_lock_candidate is None:
        raise TreasuryRequestExecutionInconsistent(
            f"TreasuryOperationRequest #{instance.pk} is not currently EXECUTING — nothing to recover."
        )

    target = pre_lock_candidate.instance
    if target.requested_by_id == request.user.pk or target.approved_by_id == request.user.pk:
        raise TreasuryRequestSelfRecoveryDenied(
            f"User #{request.user.pk} cannot recover treasury request #{target.pk} "
            "they themselves requested or approved."
        )

    if not pre_lock_candidate.eligible:
        raise TreasuryRequestExecutionInconsistent(
            f"TreasuryOperationRequest #{target.pk} is not eligible for recovery: "
            f"{pre_lock_candidate.block_reason}"
        )

    captured_executed_by_id = target.executed_by_id

    _audit.log_audit(
        request, _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
        f"Treasury request #{target.pk} execution recovery started",
        detail={
            "treasury_request_id": target.pk,
            "operation_type": target.operation_type,
            "wallet_id": target.wallet_id,
            "wallet_user_id": target.wallet.user_id,
            "amount": str(target.amount),
            "requested_by_id": target.requested_by_id,
            "approved_by_id": target.approved_by_id,
            "executed_by_id": captured_executed_by_id,
            "recovery_actor_id": request.user.pk,
            "recovery_reason": recovery_reason,
            "age_seconds": pre_lock_candidate.age_seconds,
            "age_confidence": pre_lock_candidate.age_confidence,
            "case": pre_lock_candidate.case,
        },
    )
    _broker_audit.record_payment_event(
        event_type=_broker_audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_STARTED,
        severity=_broker_audit.Severity.WARNING,
        actor_type=_broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{target.pk} execution recovery started",
        source_module="simulator.treasury_execution_recovery",
        metadata={
            "treasury_operation_request_id": target.pk,
            "operation_type": target.operation_type,
            "wallet_id": target.wallet_id,
            "wallet_user_id": target.wallet.user_id,
            "amount": str(target.amount),
            "requested_by_id": target.requested_by_id,
            "approved_by_id": target.approved_by_id,
            "executed_by_id": captured_executed_by_id,
            "recovery_actor_id": request.user.pk,
            "recovery_reason": recovery_reason,
            "age_seconds": pre_lock_candidate.age_seconds,
            "age_confidence": pre_lock_candidate.age_confidence,
            "case": pre_lock_candidate.case,
        },
    )

    locked = None
    try:
        with transaction.atomic():
            try:
                locked = (
                    TreasuryOperationRequest.objects
                    .select_for_update(nowait=True)
                    .get(pk=instance.pk)
                )
            except TreasuryOperationRequest.DoesNotExist as exc:
                exc.block_type = "STATE_CHANGED"
                raise

            # Never trust the pre-lock gate alone — explicit recheck
            # under lock, mirroring execute_treasury_request()'s own
            # Step B revalidation discipline (O.3c-3, Ajuste 1).
            if locked.status != TreasuryOperationRequest.ST_EXECUTING:
                exc = TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: expected status "
                    f"EXECUTING at recovery time, found {locked.status}."
                )
                exc.block_type = "STATE_CHANGED"
                raise exc
            if locked.wallet_transaction_id is not None:
                exc = TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: wallet_transaction "
                    f"already linked (#{locked.wallet_transaction_id}) — refusing "
                    "to mark FAILED, money may already have moved."
                )
                exc.block_type = "STATE_CHANGED"
                raise exc
            if locked.executed_by_id != captured_executed_by_id:
                exc = TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: expected executed_by="
                    f"{captured_executed_by_id} at recovery time, found "
                    f"{locked.executed_by_id}."
                )
                exc.block_type = "STATE_CHANGED"
                raise exc

            # Reconfirm full eligibility (age/EXECUTED/FAILED events) by
            # reusing inspect_stuck_treasury_execution() again — safe to
            # read plainly now, since nobody else can be writing to this
            # row while we hold its lock.
            post_lock_candidate = next(
                (r for r in inspect_stuck_treasury_execution() if r.instance.pk == locked.pk), None,
            )
            if post_lock_candidate is None or not post_lock_candidate.eligible:
                reason = post_lock_candidate.block_reason if post_lock_candidate else "no longer EXECUTING"
                exc = TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk} is no longer eligible "
                    f"for recovery: {reason}"
                )
                exc.block_type = "STATE_CHANGED"
                raise exc

            failure_reason = f"[MANUAL RECOVERY] {recovery_reason}"[:256]

            updated_rows = TreasuryOperationRequest.objects.filter(
                pk=locked.pk,
                status=TreasuryOperationRequest.ST_EXECUTING,
                wallet_transaction__isnull=True,
                executed_by_id=captured_executed_by_id,
            ).update(
                status=TreasuryOperationRequest.ST_FAILED,
                failure_reason=failure_reason,
                executed_at=timezone.now(),
            )

            if updated_rows != 1:
                # Should be structurally impossible while holding the row
                # lock through every check above — kept as a final,
                # non-negotiable verification gate anyway, same
                # belt-and-suspenders discipline as O.3c-3's Ajuste 2.
                exc = TreasuryRequestExecutionInconsistent(
                    f"TreasuryOperationRequest #{locked.pk}: conditional UPDATE "
                    f"matched {updated_rows} rows (expected 1) despite passing "
                    "every recheck under lock — internal inconsistency, requires "
                    "investigation."
                )
                exc.block_type = "UPDATE_MATCHED_ZERO_ROWS"
                raise exc
    except Exception as exc:
        block_type = getattr(exc, "block_type", "LOCK_BUSY")
        _audit.log_audit(
            request, _audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED,
            f"Treasury request #{instance.pk} execution recovery blocked",
            detail={
                "treasury_request_id": instance.pk,
                "attempted_by": request.user.pk,
                "recovery_reason": recovery_reason,
                "found_status": locked.status if locked is not None else None,
                "found_wallet_transaction_id": locked.wallet_transaction_id if locked is not None else None,
                "block_type": block_type,
                "reason": str(exc),
            },
        )
        _broker_audit.record_payment_event(
            event_type=_broker_audit.EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED,
            severity=_broker_audit.Severity.HIGH,
            actor_type=_broker_audit.ActorType.STAFF,
            actor_id=request.user.pk,
            description=f"Treasury request #{instance.pk} execution recovery blocked",
            source_module="simulator.treasury_execution_recovery",
            metadata={
                "treasury_operation_request_id": instance.pk,
                "attempted_by": request.user.pk,
                "recovery_reason": recovery_reason,
                "found_status": locked.status if locked is not None else None,
                "found_wallet_transaction_id": locked.wallet_transaction_id if locked is not None else None,
                "block_type": block_type,
                "reason": str(exc),
            },
        )
        raise

    locked.refresh_from_db()

    _audit.log_audit(
        request, _audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
        f"Treasury request #{locked.pk} execution marked failed by recovery",
        detail={
            "treasury_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "recovery_actor_id": request.user.pk,
            "recovery_reason": recovery_reason,
            "failure_reason": locked.failure_reason,
            "previous_status": TreasuryOperationRequest.ST_EXECUTING,
            "new_status": TreasuryOperationRequest.ST_FAILED,
        },
    )
    _broker_audit.record_payment_event(
        event_type=_broker_audit.EV_TREASURY_REQUEST_EXECUTION_MARKED_FAILED,
        severity=_broker_audit.Severity.HIGH,
        actor_type=_broker_audit.ActorType.STAFF,
        actor_id=request.user.pk,
        description=f"Treasury request #{locked.pk} execution marked failed by recovery",
        source_module="simulator.treasury_execution_recovery",
        metadata={
            "treasury_operation_request_id": locked.pk,
            "operation_type": locked.operation_type,
            "wallet_id": locked.wallet_id,
            "wallet_user_id": locked.wallet.user_id,
            "amount": str(locked.amount),
            "requested_by_id": locked.requested_by_id,
            "approved_by_id": locked.approved_by_id,
            "executed_by_id": locked.executed_by_id,
            "recovery_actor_id": request.user.pk,
            "recovery_reason": recovery_reason,
            "failure_reason": locked.failure_reason,
            "previous_status": TreasuryOperationRequest.ST_EXECUTING,
            "new_status": TreasuryOperationRequest.ST_FAILED,
        },
    )

    return locked
