# simulator/payout_orchestrator.py
"""
FIX-02A.2 — Payout Orchestrator: submission flow, refund algorithm,
webhook correlation, and webhook application.

Implements the Design Lock Final (+ both correction passes) on top of:
  - simulator/payout_state_machine.py (FIX-02A.1, unmodified — reused verbatim)
  - simulator/payout_providers.py (NowPaymentsAdapter, FIX-02A.2)
  - simulator/wallet_ledger.py (credit_wallet, unmodified)

Two entry points:
  - submit_withdrawal_to_provider() — called from admin.py::approve_withdrawals
  - apply_provider_webhook_event()  — called (per event) from
    views.py::withdraw_payout_callback

Never touches funded_payouts.py — FUNDED_INTERNAL WithdrawalRequests are
detected during correlation and delegated to the existing, completely
unmodified handle_internal_payout_webhook().
"""
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .audit import (
    EV_WITHDRAW_APPROVED, EV_WITHDRAW_COMPLETE, EV_WITHDRAW_FAILED, EV_WITHDRAW_REFUNDED,
    log_audit,
)
from .models import FundedPayoutRequest, PayoutAttempt, Wallet, WalletTransaction, WithdrawalRequest
from .payout_providers import ProviderAuthError, ProviderError, ProviderPayoutEvent
from .payout_state_machine import (
    ActivePayoutAttemptExists, create_and_submit_payout_attempt,
    sync_withdrawal_request_status, transition_payout_attempt,
)
from .wallet_ledger import credit_wallet

logger = logging.getLogger(__name__)

_EARLY_WEBHOOK_MAX_QUERIES = 3      # 3 intentos de lectura de correlación (incluye el primero)
_EARLY_WEBHOOK_SLEEP_S = 0.15       # pausa entre intentos consecutivos — 2 sleeps, ~0.30s máximo

_WR_TERMINAL_STATUSES = (
    WithdrawalRequest.STATUS_COMPLETED,
    WithdrawalRequest.STATUS_REJECTED,
    WithdrawalRequest.STATUS_FAILED,
)


class EstimateFailed(Exception):
    """adapter.estimate() failed. Nothing was created — safe to report
    and let the admin retry immediately."""


class WithdrawalAlreadyClaimed(Exception):
    """The WithdrawalRequest is no longer 'pending' — another process
    already claimed/processed it (concurrent admin, or a stale queryset)."""


# ─────────────────────────────────────────────
# Correlation result types
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class AttemptMatch:
    attempt: PayoutAttempt


@dataclass(frozen=True)
class FundedInternalMatch:
    withdrawal_request: WithdrawalRequest


@dataclass(frozen=True)
class LegacyWithdrawalMatch:
    withdrawal_request: WithdrawalRequest


@dataclass(frozen=True)
class Ambiguous:
    reason: str


@dataclass(frozen=True)
class DataIntegrityViolation:
    reason: str


@dataclass(frozen=True)
class Orphan:
    reason: str


# ─────────────────────────────────────────────
# 1. Submission flow — admin.py::approve_withdrawals calls this
# ─────────────────────────────────────────────

def submit_withdrawal_to_provider(withdrawal_request, *, adapter, actor, callback_url, request=None):
    """
    Design Lock sequence: optimistic read -> adapter.estimate() -> TXN1
    -> adapter.create_payout() -> TXN2.

    Returns {"outcome": "processing"|"unknown"|"failed", "attempt_id": int}.

    Raises EstimateFailed / WithdrawalAlreadyClaimed / ActivePayoutAttemptExists
    for anything that happens BEFORE a PayoutAttempt is durably created —
    all three are safe to surface to the admin as "nothing was sent,
    you may retry" (WithdrawalAlreadyClaimed/ActivePayoutAttemptExists
    specifically mean someone/something else already owns this request).
    """
    # Step 1 — optimistic, UNLOCKED read. Not authoritative — just avoids
    # wasting an estimate() call on an obviously-already-handled row.
    wr_preview = WithdrawalRequest.objects.get(pk=withdrawal_request.pk)
    if wr_preview.status != WithdrawalRequest.STATUS_PENDING:
        raise WithdrawalAlreadyClaimed(
            f"WithdrawalRequest #{wr_preview.pk} is not pending (status={wr_preview.status})"
        )

    # Step 2 — estimate(). HTTP, no atomic, no locks. Side-effect-free.
    try:
        provider_amount = adapter.estimate(wr_preview.amount_usd, wr_preview.crypto_currency)
    except ProviderError as exc:
        raise EstimateFailed(str(exc)) from exc

    # Step 3 — TXN1: the single serialization point. WithdrawalRequest lock ONLY.
    with transaction.atomic():
        wr = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal_request.pk)
        if wr.status != WithdrawalRequest.STATUS_PENDING:
            raise WithdrawalAlreadyClaimed(
                f"WithdrawalRequest #{wr.pk} is not pending (status={wr.status})"
            )
        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            reviewed_by=actor, reviewed_at=timezone.now(),
        )
        # Reused verbatim from FIX-02A.1 — re-entrant on the lock already
        # held above (see that function's own docstring + the FIX-02A.2
        # audit confirming this composition is safe).
        attempt = create_and_submit_payout_attempt(
            wr,
            provider=adapter.provider_name,
            requested_asset=wr.crypto_currency,
            destination_address=wr.wallet_address,
            requested_amount_usd=wr.amount_usd,
            actor=actor,
        )
        # provider_amount (the estimate) is not part of .1's frozen
        # signature — persist it here, same transaction, same row we
        # just inserted and still hold under lock.
        PayoutAttempt.objects.filter(pk=attempt.pk).update(provider_amount=provider_amount)
        attempt.provider_amount = provider_amount

    log_audit(
        request, EV_WITHDRAW_APPROVED,
        f"Withdrawal #{wr.id} approved by {getattr(actor, 'username', actor)} — ${wr.amount_usd}",
        detail={
            "withdrawal_id": wr.id, "amount_usd": str(wr.amount_usd),
            "currency": wr.crypto_currency, "payout_attempt_id": attempt.pk,
            "reviewed_by": getattr(actor, "username", str(actor)),
        },
    )
    _send_status_email_safe(wr, "approved")

    # Step 4 — create_payout(). HTTP, no atomic, no locks.
    try:
        result = adapter.create_payout(attempt, callback_url=callback_url)
    except ProviderAuthError as exc:
        _apply_confirmed_failure_with_refund(
            attempt.pk, reason=str(exc), pre_send_rejection=True, actor=actor, request=request,
        )
        return {"outcome": "failed", "attempt_id": attempt.pk}
    except ProviderError as exc:
        _apply_result_without_refund(
            attempt.pk, PayoutAttempt.STATUS_UNKNOWN, reason=str(exc), actor=actor,
        )
        return {"outcome": "unknown", "attempt_id": attempt.pk}

    # Step 5 — TXN2, success.
    _apply_submission_success(attempt.pk, result, actor=actor)
    return {"outcome": "processing", "attempt_id": attempt.pk}


# ─────────────────────────────────────────────
# 2. TXN2 appliers — non-refund and refund paths
# ─────────────────────────────────────────────

def _apply_result_without_refund(attempt_id, new_status, *, reason="", raw_provider_status="", actor=None):
    """Lock order: WithdrawalRequest -> PayoutAttempt. No Wallet."""
    with transaction.atomic():
        wr_id = PayoutAttempt.objects.values_list("withdrawal_request_id", flat=True).get(pk=attempt_id)
        wr = WithdrawalRequest.objects.select_for_update().get(pk=wr_id)
        attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status in PayoutAttempt.TERMINAL_STATUSES:
            return attempt  # idempotent no-op
        transition_payout_attempt(attempt, new_status, reason=reason, raw_provider_status=raw_provider_status)
        sync_withdrawal_request_status(wr, new_status, actor=actor)
        return attempt


def _apply_submission_success(attempt_id, result, *, actor=None):
    """Lock order: WithdrawalRequest -> PayoutAttempt. No Wallet.
    Persists provider refs + dual-writes the np_* legacy mirror in the
    SAME transaction (Design Lock Correction #6 — not best-effort)."""
    with transaction.atomic():
        wr_id = PayoutAttempt.objects.values_list("withdrawal_request_id", flat=True).get(pk=attempt_id)
        wr = WithdrawalRequest.objects.select_for_update().get(pk=wr_id)
        attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status in PayoutAttempt.TERMINAL_STATUSES:
            return attempt  # idempotent no-op — defense in depth

        attempt.provider_reference = result.provider_reference
        attempt.provider_batch_id = result.provider_batch_id
        attempt.provider_amount = result.provider_amount
        transition_payout_attempt(attempt, PayoutAttempt.STATUS_PROCESSING, raw_provider_status=result.raw_status)
        sync_withdrawal_request_status(wr, PayoutAttempt.STATUS_PROCESSING, actor=actor)

        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            np_batch_id=result.provider_batch_id,
            np_payout_id=result.provider_reference,
            np_payout_status=result.raw_status,
            crypto_amount=result.provider_amount,
        )
        return attempt


def _apply_confirmed_failure_with_refund(attempt_id, *, reason, pre_send_rejection,
                                          raw_provider_status="", actor=None, request=None):
    """
    Global lock order when Wallet participates: Wallet -> WithdrawalRequest
    -> PayoutAttempt (Design Lock Correction #2). What to lock is resolved
    with an UNLOCKED preview read first; every decision is re-derived
    AFTER all three locks are held — the preview is never trusted.
    """
    preview = PayoutAttempt.objects.select_related("withdrawal_request__user").get(pk=attempt_id)
    # Wallet.user is the forward O2O (Wallet declares it) — User has no
    # reverse `.wallet_id` shortcut, only `.wallet` (a query). Querying
    # Wallet directly by user_id avoids relying on that reverse traversal.
    wallet_id = Wallet.objects.values_list("id", flat=True).get(user_id=preview.withdrawal_request.user_id)
    wr_id = preview.withdrawal_request_id

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=wallet_id)
        wr = WithdrawalRequest.objects.select_for_update().get(pk=wr_id)
        attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt_id)

        if attempt.status in PayoutAttempt.TERMINAL_STATUSES:
            return attempt  # idempotent — already resolved, refund never repeated

        transition_payout_attempt(
            attempt, PayoutAttempt.STATUS_FAILED,
            reason=reason, raw_provider_status=raw_provider_status,
            pre_send_rejection=pre_send_rejection,
        )
        sync_withdrawal_request_status(wr, PayoutAttempt.STATUS_FAILED, actor=actor)
        credit_wallet(
            wallet.id, wr.amount_usd, WalletTransaction.TX_CORRECTION,
            note=f"Refund — payout attempt #{attempt.pk} failed ({reason[:80]})",
            initiated_by=actor,
        )
        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            np_payout_status=raw_provider_status or "FAILED",
        )

    log_audit(
        request, EV_WITHDRAW_FAILED,
        f"Withdrawal #{wr.id} FAILED — payout attempt #{attempt.pk} — {reason}",
        detail={"withdrawal_id": wr.id, "payout_attempt_id": attempt.pk, "reason": reason},
    )
    log_audit(
        request, EV_WITHDRAW_REFUNDED,
        f"Withdrawal #{wr.id} refunded ${wr.amount_usd} after payout failure",
        detail={"withdrawal_id": wr.id, "payout_attempt_id": attempt.pk, "amount_usd": str(wr.amount_usd)},
    )
    _send_status_email_safe(wr, "failed")
    return attempt


def _send_status_email_safe(wr, event_key):
    from .withdrawal_emails import (
        EVENT_APPROVED, EVENT_COMPLETED, EVENT_FAILED, send_withdrawal_status_email,
    )
    _events = {"approved": EVENT_APPROVED, "completed": EVENT_COMPLETED, "failed": EVENT_FAILED}
    try:
        send_withdrawal_status_email(wr, _events[event_key])
    except Exception as mail_exc:
        logger.warning("[payout_orchestrator] email queuing failed wr=%d event=%s: %s", wr.id, event_key, mail_exc)


# ─────────────────────────────────────────────
# 3. Webhook correlation — Design Lock §A/§9/§11 (precedence + micro-correction)
# ─────────────────────────────────────────────

def resolve_payout_target(event: ProviderPayoutEvent):
    """
    provider_reference has GLOBAL precedence over provider_batch_id.
    batch_id is used ONLY when the payload carries no individual
    reference at all — never as a fallback for one that failed to
    resolve. Any layer producing >1 candidates is Ambiguous immediately
    — never inferred as "the same obligation" via ID union (the exact
    bug fixed in the micro-correction pass). A mismatch between a
    resolved PayoutAttempt and its own np_* mirror is
    DataIntegrityViolation, not Ambiguous — distinct categories.
    """
    if not event.provider_reference and not event.provider_batch_id:
        return Orphan("evento sin ninguna referencia utilizable")

    if event.provider_reference:
        for query_n in range(_EARLY_WEBHOOK_MAX_QUERIES):
            attempt_matches = list(PayoutAttempt.objects.filter(
                provider=event.provider, provider_reference=event.provider_reference))
            wr_matches = list(WithdrawalRequest.objects.filter(
                np_payout_id=event.provider_reference))

            if len(attempt_matches) > 1:
                return Ambiguous("provider_reference duplicado en PayoutAttempt")
            if len(wr_matches) > 1:
                return Ambiguous("np_payout_id duplicado en WithdrawalRequest")

            if len(attempt_matches) == 1:
                attempt = attempt_matches[0]
                if len(wr_matches) == 1 and wr_matches[0].pk != attempt.withdrawal_request_id:
                    return DataIntegrityViolation(
                        f"PayoutAttempt#{attempt.pk} apunta a WR#{attempt.withdrawal_request_id} "
                        f"pero el espejo np_payout_id apunta a WR#{wr_matches[0].pk}"
                    )
                return AttemptMatch(attempt)

            if len(wr_matches) == 1:
                return _classify_legacy_wr(wr_matches[0])

            if query_n < _EARLY_WEBHOOK_MAX_QUERIES - 1:
                time.sleep(_EARLY_WEBHOOK_SLEEP_S)
                continue
            return Orphan("provider_reference presente, 0 matches tras reintentos acotados")

    if event.provider_batch_id:
        attempt_matches = list(PayoutAttempt.objects.filter(
            provider=event.provider, provider_batch_id=event.provider_batch_id))
        wr_matches = list(WithdrawalRequest.objects.filter(np_batch_id=event.provider_batch_id))

        if len(attempt_matches) > 1:
            return Ambiguous("provider_batch_id coincide con múltiples PayoutAttempt")
        if len(wr_matches) > 1:
            return Ambiguous("provider_batch_id (np_batch_id) coincide con múltiples WithdrawalRequest")

        if len(attempt_matches) == 1:
            attempt = attempt_matches[0]
            if len(wr_matches) == 1 and wr_matches[0].pk != attempt.withdrawal_request_id:
                return DataIntegrityViolation(
                    f"PayoutAttempt#{attempt.pk} (batch) apunta a WR#{attempt.withdrawal_request_id} "
                    f"pero el espejo np_batch_id apunta a WR#{wr_matches[0].pk}"
                )
            return AttemptMatch(attempt)

        if len(wr_matches) == 1:
            return _classify_legacy_wr(wr_matches[0])

    return Orphan("ninguna capa resolvió")


def _classify_legacy_wr(wr):
    try:
        wr.funded_payout_internal
    except FundedPayoutRequest.DoesNotExist:
        return LegacyWithdrawalMatch(wr)
    return FundedInternalMatch(wr)


# ─────────────────────────────────────────────
# 4. Webhook application
# ─────────────────────────────────────────────

def apply_provider_webhook_event(event: ProviderPayoutEvent, *, request=None):
    """
    Resolves the target via resolve_payout_target() and applies it.
    Ambiguous/DataIntegrityViolation/Orphan are terminal outcomes in
    themselves — logged, never mutate state, never raised as an
    exception (which would surface as a 500 to NowPayments and could
    trigger their own retry storm on top of an already-uncertain case).
    """
    target = resolve_payout_target(event)

    if isinstance(target, Ambiguous):
        logger.error("[payout_orchestrator] AMBIGUOUS correlation — %s | event=%r", target.reason, event)
        return target
    if isinstance(target, DataIntegrityViolation):
        logger.error("[payout_orchestrator] DATA INTEGRITY VIOLATION — %s | event=%r", target.reason, event)
        return target
    if isinstance(target, Orphan):
        logger.warning("[payout_orchestrator] orphan webhook — %s | event=%r", target.reason, event)
        return target
    if isinstance(target, FundedInternalMatch):
        _apply_funded_internal_webhook(target.withdrawal_request, event)
        return target
    if isinstance(target, LegacyWithdrawalMatch):
        _apply_legacy_withdrawal_webhook(target.withdrawal_request, event, request=request)
        return target
    if isinstance(target, AttemptMatch):
        _apply_attempt_webhook(target.attempt, event, request=request)
        return target
    return target  # pragma: no cover — exhaustive above


def _apply_attempt_webhook(attempt, event: ProviderPayoutEvent, *, request=None):
    if event.normalized_status == PayoutAttempt.STATUS_FAILED:
        result_attempt = _apply_confirmed_failure_with_refund(
            attempt.pk, reason=f"NowPayments callback: {event.raw_status}",
            pre_send_rejection=False, raw_provider_status=event.raw_status, request=request,
        )
    else:
        result_attempt = _apply_result_without_refund(
            attempt.pk, event.normalized_status, raw_provider_status=event.raw_status,
        )
        if event.normalized_status == PayoutAttempt.STATUS_COMPLETED and result_attempt.status == PayoutAttempt.STATUS_COMPLETED:
            wr = WithdrawalRequest.objects.get(pk=attempt.withdrawal_request_id)
            log_audit(
                request, EV_WITHDRAW_COMPLETE,
                f"Withdrawal #{wr.id} COMPLETED — payout attempt #{attempt.pk}",
                detail={"withdrawal_id": wr.id, "payout_attempt_id": attempt.pk, "amount_usd": str(wr.amount_usd)},
            )
            _send_status_email_safe(wr, "completed")
    return result_attempt


def _apply_funded_internal_webhook(wr, event: ProviderPayoutEvent):
    """Byte-for-byte the same sequence withdraw_payout_callback already
    runs today for FUNDED_INTERNAL — relocated, not altered. Never
    touches funded_payouts.py."""
    from .funded_payouts import handle_internal_payout_webhook

    with transaction.atomic():
        wr_locked = WithdrawalRequest.objects.select_for_update().get(pk=wr.pk)
        if wr_locked.status in _WR_TERMINAL_STATUSES:
            logger.info("[payout_orchestrator] FUNDED_INTERNAL already final wr_id=%d — skip", wr_locked.id)
            return
        fpr = wr_locked.funded_payout_internal
        handle_internal_payout_webhook(fpr, wr_locked, event.normalized_status, event.provider_reference)
        update = {"status": event.normalized_status, "np_payout_status": event.raw_status}
        if event.provider_reference and not wr_locked.np_payout_id:
            update["np_payout_id"] = event.provider_reference
        WithdrawalRequest.objects.filter(pk=wr_locked.pk).update(**update)


def _apply_legacy_withdrawal_webhook(wr, event: ProviderPayoutEvent, *, request=None):
    """Byte-for-byte the same sequence withdraw_payout_callback already
    runs today for a regular (non-FUNDED_INTERNAL) withdrawal — for
    WithdrawalRequest rows created before FIX-02A.2, which have no
    PayoutAttempt to route through."""
    from .withdrawal_emails import EVENT_COMPLETED, EVENT_FAILED, send_withdrawal_status_email

    with transaction.atomic():
        wr_locked = WithdrawalRequest.objects.select_for_update().get(pk=wr.pk)
        if wr_locked.status in _WR_TERMINAL_STATUSES:
            logger.info("[payout_orchestrator] legacy WR already final wr_id=%d — skip", wr_locked.id)
            return

        update = {"status": event.normalized_status, "np_payout_status": event.raw_status}
        if event.provider_reference and not wr_locked.np_payout_id:
            update["np_payout_id"] = event.provider_reference

        if event.normalized_status == WithdrawalRequest.STATUS_FAILED:
            wallet = Wallet.objects.select_for_update().get(user_id=wr_locked.user_id)
            credit_wallet(
                wallet.id, wr_locked.amount_usd, WalletTransaction.TX_CORRECTION,
                note=f"Refund — payout #{event.provider_reference} failed",
            )
            log_audit(
                request, EV_WITHDRAW_FAILED,
                f"Withdrawal #{wr_locked.id} FAILED by NowPayments — payout {event.provider_reference}",
                detail={"withdrawal_id": wr_locked.id, "payout_id": event.provider_reference,
                        "amount_usd": str(wr_locked.amount_usd)},
            )
            log_audit(
                request, EV_WITHDRAW_REFUNDED,
                f"Withdrawal #{wr_locked.id} refunded ${wr_locked.amount_usd} after payout failure",
                detail={"withdrawal_id": wr_locked.id, "payout_id": event.provider_reference,
                        "amount_usd": str(wr_locked.amount_usd), "wallet_id": wallet.id},
            )
            WithdrawalRequest.objects.filter(pk=wr_locked.pk).update(**update)
            try:
                send_withdrawal_status_email(wr_locked, EVENT_FAILED)
            except Exception as mail_exc:
                logger.warning("[payout_orchestrator] failed email queuing failed wr=%d: %s", wr_locked.id, mail_exc)

        elif event.normalized_status == WithdrawalRequest.STATUS_COMPLETED:
            log_audit(
                request, EV_WITHDRAW_COMPLETE,
                f"Withdrawal #{wr_locked.id} COMPLETED — ${wr_locked.amount_usd}",
                detail={"withdrawal_id": wr_locked.id, "payout_id": event.provider_reference,
                        "amount_usd": str(wr_locked.amount_usd), "crypto_amount": str(wr_locked.crypto_amount),
                        "currency": wr_locked.crypto_currency},
            )
            WithdrawalRequest.objects.filter(pk=wr_locked.pk).update(**update)
            try:
                send_withdrawal_status_email(wr_locked, EVENT_COMPLETED)
            except Exception as mail_exc:
                logger.warning("[payout_orchestrator] completed email queuing failed wr=%d: %s", wr_locked.id, mail_exc)

        else:
            WithdrawalRequest.objects.filter(pk=wr_locked.pk).update(**update)
