# simulator/payout_state_machine.py
"""
FIX-02A.1 — Core Safety / PayoutAttempt / State Machine / Internal
Idempotency Foundation.

Pure, provider-agnostic foundation. No HTTP, no provider integration
(that's FIX-02A.2), no admin/views/callback rewiring (FIX-02A.3), no
reconciliation service (FIX-02A.4). See "FIX-02A — Design Lock
Correction Final" for the full design this module implements.

Two service surfaces live here:
  1. transition_payout_attempt() — the PayoutAttempt state machine,
     validated transitions only.
  2. create_and_submit_payout_attempt() — the sole authorized way to
     create a PayoutAttempt, and sync_withdrawal_request_status() — the
     sole authorized way, for the PayoutAttempt-driven subset of
     transitions, to write WithdrawalRequest.status.

Never touches Wallet — no import of wallet_ledger.py anywhere in this
module. Never makes an HTTP call — no import of nowpayments.py or
`requests` anywhere in this module either.
"""
from django.db import transaction
from django.utils import timezone

from .models import PayoutAttempt, WithdrawalRequest


class InvalidPayoutAttemptTransition(Exception):
    """Raised when a PayoutAttempt status transition is not allowed."""


class ActivePayoutAttemptExists(Exception):
    """Raised when creating a PayoutAttempt while a non-terminal one
    already exists for the same WithdrawalRequest (Design Lock
    invariant 11 — at most one active attempt at a time)."""


# ─────────────────────────────────────────────
# 1. State machine — PayoutAttempt.status
# ─────────────────────────────────────────────

_S = PayoutAttempt

ALLOWED_TRANSITIONS = {
    _S.STATUS_CREATED:    {_S.STATUS_SUBMITTED},
    _S.STATUS_SUBMITTED:  {_S.STATUS_PROCESSING, _S.STATUS_UNKNOWN, _S.STATUS_FAILED},
    _S.STATUS_PROCESSING: {_S.STATUS_COMPLETED, _S.STATUS_FAILED},
    _S.STATUS_UNKNOWN:    {_S.STATUS_COMPLETED, _S.STATUS_FAILED},
    _S.STATUS_COMPLETED:  set(),
    _S.STATUS_FAILED:     set(),
}


def transition_payout_attempt(attempt, new_status, *, reason="", raw_provider_status="",
                               pre_send_rejection=False):
    """
    Validate and apply a PayoutAttempt status transition.

    Caller is responsible for holding whatever lock is appropriate for
    the calling context (see the Design Lock's lock-order table, §3) —
    this function does not itself call select_for_update(); it only
    validates and writes the in-memory instance passed in.

    SUBMITTED -> FAILED is only allowed when pre_send_rejection=True.
    Per the Design Lock, that transition represents an unambiguous
    provider rejection BEFORE any money could have moved (e.g. a 4xx
    validation error) — never a timeout/network-ambiguous outcome. A
    bare SUBMITTED -> FAILED without that flag is refused, forcing
    ambiguous outcomes through UNKNOWN instead.

    Raises InvalidPayoutAttemptTransition for any transition not in
    ALLOWED_TRANSITIONS, or for SUBMITTED -> FAILED without
    pre_send_rejection=True.
    """
    current = attempt.status
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise InvalidPayoutAttemptTransition(
            f"PayoutAttempt #{attempt.pk}: {current} -> {new_status} is not an allowed transition"
        )
    if current == _S.STATUS_SUBMITTED and new_status == _S.STATUS_FAILED and not pre_send_rejection:
        raise InvalidPayoutAttemptTransition(
            f"PayoutAttempt #{attempt.pk}: SUBMITTED -> FAILED requires an unambiguous "
            "pre-send rejection (pre_send_rejection=True) — a timeout/network-ambiguous "
            "outcome must go through UNKNOWN instead, never directly to FAILED."
        )

    now = timezone.now()
    attempt.status = new_status
    if reason:
        attempt.last_error = reason
    if raw_provider_status:
        attempt.raw_provider_status = raw_provider_status
    if new_status == _S.STATUS_PROCESSING:
        attempt.acknowledged_at = now
    elif new_status == _S.STATUS_COMPLETED:
        attempt.completed_at = now
    elif new_status == _S.STATUS_FAILED:
        attempt.failed_at = now
    attempt.save()
    return attempt


# ─────────────────────────────────────────────
# 2. WithdrawalRequest.status — PayoutAttempt-driven subset
# ─────────────────────────────────────────────
#
# WithdrawalRequest.status stays a materialized DB column (Design Lock
# Correction #4 — not a computed property), because admin list_filter,
# _get_daily_withdrawal_used(), and reconcile_withdrawals_task all
# filter on it as a real column today, in production code this block
# does not touch. This module owns only the slice of writes driven by
# PayoutAttempt transitions; the pending/approved/rejected writes still
# done ad-hoc in views.py/admin.py are NOT rewired to go through here
# yet — that's FIX-02A.3.

_PAYOUT_ATTEMPT_STATUS_TO_WITHDRAWAL_STATUS = {
    PayoutAttempt.STATUS_SUBMITTED:  WithdrawalRequest.STATUS_PROCESSING,
    PayoutAttempt.STATUS_PROCESSING: WithdrawalRequest.STATUS_PROCESSING,
    PayoutAttempt.STATUS_UNKNOWN:    WithdrawalRequest.STATUS_PROCESSING,
    PayoutAttempt.STATUS_COMPLETED:  WithdrawalRequest.STATUS_COMPLETED,
    PayoutAttempt.STATUS_FAILED:     WithdrawalRequest.STATUS_FAILED,
}


def derive_withdrawal_status(payout_attempt_status):
    """
    Pure mapping, no DB. The approved mapping (Design Lock §7):
      SUBMITTED / PROCESSING / UNKNOWN -> processing
      COMPLETED                        -> completed
      FAILED (confirmed)               -> failed
    Never 'pending' for any input — that regression (the original
    FIX-02 bug) is structurally impossible here, not just guarded.

    CREATED has no approved mapping: it is never expected to be
    durably observable on its own (see PayoutAttempt.status
    docstring), so deriving from it is a programming error.
    """
    try:
        return _PAYOUT_ATTEMPT_STATUS_TO_WITHDRAWAL_STATUS[payout_attempt_status]
    except KeyError:
        raise ValueError(
            f"No approved WithdrawalRequest.status mapping for PayoutAttempt "
            f"status {payout_attempt_status!r}"
        )


def sync_withdrawal_request_status(withdrawal_request, payout_attempt_status, *, actor=None):
    """
    The sole authorized way (for this PayoutAttempt-driven subset) to
    write WithdrawalRequest.status. Locks the WithdrawalRequest row,
    derives the target status via derive_withdrawal_status(), and
    writes it.

    Safe to call both standalone and from inside an outer atomic()
    block that already holds this same row's lock — re-entrant
    transaction.atomic() adds a savepoint, not a new transaction, and
    re-acquiring select_for_update() on a row this same transaction
    already locked is a no-op (same convention wallet_ledger.py
    documents for credit_wallet()/debit_wallet()).
    """
    new_status = derive_withdrawal_status(payout_attempt_status)
    with transaction.atomic():
        wr = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal_request.pk)
        if wr.status != new_status:
            WithdrawalRequest.objects.filter(pk=wr.pk).update(status=new_status)
        wr.status = new_status
    return wr


# ─────────────────────────────────────────────
# 3. create_and_submit_payout_attempt — the sole authorized creation path
# ─────────────────────────────────────────────

def _next_attempt_number(withdrawal_request_id):
    last = (
        PayoutAttempt.objects
        .filter(withdrawal_request_id=withdrawal_request_id)
        .order_by("-attempt_number")
        .values_list("attempt_number", flat=True)
        .first()
    )
    return (last or 0) + 1


def _make_idempotency_key(withdrawal_request_id, attempt_number):
    """
    Deterministic, derived from the two fields already uniquely
    constrained together (withdrawal_request, attempt_number) — no
    randomness, human-debuggable in logs, no dependency on any
    provider's key format since no provider is integrated yet
    (FIX-02A.2). Not a claim of external/provider-side idempotency —
    see the module-level note in create_and_submit_payout_attempt().
    """
    return f"moneybroker-wr{withdrawal_request_id}-attempt{attempt_number}"


def create_and_submit_payout_attempt(
    withdrawal_request, *,
    provider, requested_asset, destination_address, requested_amount_usd,
    requested_network="", actor=None,
):
    """
    The sole authorized way to create a PayoutAttempt.

    Lock order: WithdrawalRequest ONLY — no Wallet lock (Design Lock
    Correction #1: the wallet was already debited once, at
    WithdrawalRequest creation; creating/submitting an attempt never
    moves money, so it never needs the wallet — confirmed against
    admin.py::approve_withdrawals, which today already does not lock
    Wallet either).

    Inserts the row already in SUBMITTED (submitted_at=now()) in a
    single write — CREATED never becomes a separately durable state on
    this path (see PayoutAttempt.status docstring). FIX-02A.1 makes NO
    network call at all, so this whole operation is one local
    transaction, never open around HTTP (Design Lock Correction #2 —
    that discipline is what FIX-02A.2's orchestrator must inherit when
    it adds the real provider call AFTER this transaction commits).

    idempotency_key IS unique in our own DB and DOES guarantee this
    system never creates two concurrent/duplicate attempts for the
    same intent (internal idempotency). It is NOT sent to, and is NOT
    known to be honored by, any provider — nowpayments.py::
    create_payout() sends no order_id/reference field today. This key
    provides zero protection against a provider processing a raw HTTP
    retry twice; that protection comes only from the "never retry from
    UNKNOWN/SUBMITTED, only from confirmed FAILED" policy (Design Lock
    Correction #3), enforced by ActivePayoutAttemptExists below, not by
    this key.

    Raises ActivePayoutAttemptExists if a non-terminal PayoutAttempt
    already exists for this WithdrawalRequest. Enforced here under the
    WithdrawalRequest lock (the actual race-preventing mechanism for
    two callers targeting the same WithdrawalRequest — the lock fully
    serializes them) AND, as defense in depth against any future code
    path that bypasses this function, by PayoutAttempt's own partial
    unique DB constraint (pa_one_active_attempt_per_wr).
    """
    with transaction.atomic():
        wr = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal_request.pk)

        if PayoutAttempt.objects.filter(
            withdrawal_request=wr,
            status__in=PayoutAttempt.NON_TERMINAL_STATUSES,
        ).exists():
            raise ActivePayoutAttemptExists(
                f"WithdrawalRequest #{wr.pk} already has an unresolved PayoutAttempt — "
                "reconcile it before creating a new one."
            )

        attempt_number = _next_attempt_number(wr.pk)
        attempt = PayoutAttempt.objects.create(
            withdrawal_request=wr,
            provider=provider,
            attempt_number=attempt_number,
            idempotency_key=_make_idempotency_key(wr.pk, attempt_number),
            requested_amount_usd=requested_amount_usd,
            requested_asset=requested_asset,
            requested_network=requested_network,
            destination_address=destination_address,
            status=PayoutAttempt.STATUS_SUBMITTED,
            submitted_at=timezone.now(),
            created_by=actor,
            submitted_by=actor,
        )

        sync_withdrawal_request_status(wr, PayoutAttempt.STATUS_SUBMITTED, actor=actor)

    return attempt
