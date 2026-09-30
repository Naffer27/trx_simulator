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
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .audit import (
    EV_WITHDRAW_AGED_SUBMITTED_TO_UNKNOWN, EV_WITHDRAW_APPROVED, EV_WITHDRAW_COMPLETE,
    EV_WITHDRAW_FAILED, EV_WITHDRAW_PAYOUT_VERIFICATION_FAILED, EV_WITHDRAW_PAYOUT_VERIFIED,
    EV_WITHDRAW_RECONCILIATION_RESOLVED, EV_WITHDRAW_REFUNDED,
    EV_WITHDRAW_WEBHOOK_MANUAL_REVIEW, log_audit,
)
from .models import (
    FundedPayoutRequest, PayoutAttempt, PayoutWebhookEvent, Wallet, WalletTransaction,
    WithdrawalRequest,
)
from .payout_providers import (
    PayoutLookupOutcome, ProviderAuthError, ProviderError, ProviderPayoutEvent,
    compute_webhook_event_fingerprint, get_adapter_for_provider,
)
from .payout_state_machine import (
    ActivePayoutAttemptExists, create_and_submit_payout_attempt, is_submitted_aged,
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
    # WITHDRAWAL-ECONOMICS-01 — the client receives NET (gross minus the
    # snapshotted commercial fee), never the full gross amount_usd. A
    # legacy WithdrawalRequest (net_amount is None — created before this
    # block existed, no fee was ever evaluated for it) falls back to
    # amount_usd unchanged — its historical economics are never
    # reinterpreted as if a fee had applied.
    estimate_basis = wr_preview.net_amount if wr_preview.net_amount is not None else wr_preview.amount_usd
    try:
        provider_amount = adapter.estimate(estimate_basis, wr_preview.crypto_currency)
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
            # WITHDRAWAL-ECONOMICS-01 — same estimate_basis computed above
            # (net_amount, or amount_usd for a legacy request with no fee
            # snapshot): this is the actual USD figure requested of the
            # provider, kept consistent with the adapter.estimate() call.
            requested_amount_usd=estimate_basis,
            actor=actor,
        )
        # provider_amount (the estimate) is not part of .1's frozen
        # signature — persist it here, same transaction, same row we
        # just inserted and still hold under lock.
        PayoutAttempt.objects.filter(pk=attempt.pk).update(provider_amount=provider_amount)
        attempt.provider_amount = provider_amount

    # WITHDRAWAL-POLICY-CORRECTION-01 — actor is None for auto-authorized
    # (<=$1,000, no staff review) and operational-retry submissions; only a
    # real admin User has actor.username. The fallback label makes that
    # explicit here without attributing approval to nobody/"None".
    _actor_label = getattr(actor, "username", None) or "system (auto-authorized)"
    log_audit(
        request, EV_WITHDRAW_APPROVED,
        f"Withdrawal #{wr.id} approved by {_actor_label} — ${wr.amount_usd}",
        detail={
            "withdrawal_id": wr.id, "amount_usd": str(wr.amount_usd),
            "currency": wr.crypto_currency, "payout_attempt_id": attempt.pk,
            "reviewed_by": _actor_label,
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


class PayoutVerificationError(Exception):
    """WITHDRAWAL-E2E-02C — raised by submit_payout_verification() for a
    LOCAL precondition failure (bad code format, terminal attempt,
    already verified, missing batch id) — before the provider is ever
    called. Distinct from ProviderError subclasses, which represent a
    failure that DID reach (or ambiguously may have reached) NowPayments."""


def submit_payout_verification(attempt_id, code, *, actor=None, request=None):
    """
    WITHDRAWAL-E2E-02C — confirms NowPayments' own provider-side 2FA
    code (POST /v1/payout/{batch_id}/verify) for a PayoutAttempt that
    create_payout() already submitted successfully.

    Design Lock (WITHDRAWAL-E2E-02C report, §H): this function NEVER
    touches Wallet, NEVER calls create_payout() (structurally impossible
    — it only ever calls adapter.verify_payout()), and NEVER refunds. A
    wrong/expired code, or any provider/network error on this call, just
    means "nothing changed — try again, or wait for the provider's own
    terminal webhook/reconciliation signal" (WITHDRAWAL-E2E-02B's
    already-existing, unmodified refund engine remains the EXCLUSIVE
    path that ever credits money back, driven only by a confirmed
    terminal FAILED signal — never by a verification attempt failing
    here).

    Lock order: PayoutAttempt only — no Wallet, no WithdrawalRequest
    lock, because this function writes neither of those models. The
    provider HTTP call is deliberately made WHILE HOLDING the
    PayoutAttempt row lock (a deliberate, narrow exception to this
    codebase's usual "never hold atomic() open around HTTP" discipline
    — see submit_withdrawal_to_provider() above): unlike that function,
    there is no Wallet/WithdrawalRequest lock chain here to worry about
    holding open, this is a single fast POST, and holding the row lock
    for its duration is exactly what makes a concurrent double-click
    "Verify" safe — the second caller blocks on the same row, then
    re-reads under lock and finds verified_at already set, never
    reaching the provider a second time.

    CRITICAL: `code` is a local variable only — it is validated, passed
    straight through to adapter.verify_payout(), and never assigned to
    any model field, never included in any log_audit() detail, and goes
    out of scope the moment this function returns (success or failure).

    Raises PayoutVerificationError for any local precondition failure.
    Raises a normalized ProviderError subclass (see payout_providers.py)
    if the provider call itself fails — callers should catch broadly
    (`except (PayoutVerificationError, ProviderError)`) and show the
    admin a single generic message, never provider-specific detail that
    could hint at what the correct code might be.
    """
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise PayoutVerificationError("El código de verificación debe tener exactamente 6 dígitos.")

    # WITHDRAWAL-E2E-02G FASE B — the ProviderError audit write used to
    # live inside this same transaction.atomic() block, right before the
    # `raise` that necessarily propagates out of it. An exception leaving
    # atomic() rolls back the ENTIRE transaction, including that audit
    # INSERT — so a real, confirmed provider verification failure left
    # zero AuditLog trace, even though the logger.info() line (a plain
    # Python log call, not a DB write) made it look like it had been
    # recorded (WITHDRAWAL-E2E-02F, observed on WR18). The audit write
    # now happens in this outer except, strictly after the atomic block
    # has already rolled back — nothing here touches Wallet, sets no
    # PayoutAttempt field, and never turns this into a terminal payout
    # failure (that remains exclusively the refund engine's call, driven
    # only by a confirmed terminal FAILED signal from webhook/reconciliation).
    try:
        with transaction.atomic():
            attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt_id)

            if attempt.status in PayoutAttempt.TERMINAL_STATUSES:
                raise PayoutVerificationError(
                    f"PayoutAttempt #{attempt.pk} ya está en estado terminal ({attempt.status}) — nada que verificar."
                )
            if attempt.verified_at is not None:
                raise PayoutVerificationError(
                    f"PayoutAttempt #{attempt.pk} ya fue verificado el {attempt.verified_at.isoformat()}."
                )
            if not attempt.provider_batch_id:
                raise PayoutVerificationError(
                    f"PayoutAttempt #{attempt.pk} no tiene provider_batch_id todavía — nada que verificar."
                )

            adapter = get_adapter_for_provider(attempt.provider)
            adapter.verify_payout(attempt, code)  # raises ProviderError on failure

            now = timezone.now()
            PayoutAttempt.objects.filter(pk=attempt.pk).update(verified_at=now)
            attempt.verified_at = now
    except ProviderError as exc:
        # Local precondition checks above already ran; a provider-side
        # failure here leaves the attempt completely untouched (nothing
        # was written — the transaction rolled back with nothing to
        # lose). Audited without the code, durably, outside the rolled-
        # back transaction — then re-raised so the caller (the admin
        # view) can show a generic message.
        log_audit(
            request, EV_WITHDRAW_PAYOUT_VERIFICATION_FAILED,
            f"PayoutAttempt #{attempt_id} provider verification failed",
            detail={
                "payout_attempt_id": attempt_id,
                "withdrawal_id": getattr(attempt, "withdrawal_request_id", None),
                "provider_batch_id": getattr(attempt, "provider_batch_id", ""),
                "error_type": type(exc).__name__,
            },
        )
        raise

    log_audit(
        request, EV_WITHDRAW_PAYOUT_VERIFIED,
        f"PayoutAttempt #{attempt.pk} provider-side verification confirmed",
        detail={
            "payout_attempt_id": attempt.pk,
            "withdrawal_id": attempt.withdrawal_request_id,
            "provider_batch_id": attempt.provider_batch_id,
            "verified_by": getattr(actor, "username", None) or "unknown",
        },
    )
    return attempt


# ─────────────────────────────────────────────
# 2. TXN2 appliers — non-refund and refund paths
# ─────────────────────────────────────────────

def _apply_result_without_refund(attempt_id, new_status, *, reason="", raw_provider_status="",
                                  confirmed_amount=None, tx_hash="", actor=None, request=None):
    """
    Lock order: WithdrawalRequest -> PayoutAttempt. No Wallet.

    FIX-02A.4 — this is now the SINGLE place that applies a non-refund
    transition, reused identically by the live webhook path, the
    durable webhook replay path, and the active UNKNOWN reconciliation
    service — never duplicated at any of those call sites. The
    COMPLETED audit+email side effect used to live only in the webhook
    caller (_apply_attempt_webhook); it's centralized here so every
    caller gets it automatically, exactly once, guarded by the same
    idempotent TERMINAL_STATUSES check below (an early return here
    skips the post-transaction block entirely — no email/audit on a
    no-op). reconciled_at is set only when the attempt is actually
    leaving UNKNOWN by real evidence — never touched otherwise.

    WITHDRAWAL-E2E-02G FASE B — confirmed_amount/tx_hash are optional
    and only ever meaningful when new_status == STATUS_COMPLETED; both
    callers reaching COMPLETED (webhook FINISHED, reconciliation GET
    FINISHED) pass them straight from that same terminal evidence's own
    payload (never from provider_amount, the pre-send estimate, and
    never from a create_payout()/verify_payout() return value — neither
    of those calls this function with STATUS_COMPLETED at all). Ignored
    for every other new_status.
    """
    with transaction.atomic():
        wr_id = PayoutAttempt.objects.values_list("withdrawal_request_id", flat=True).get(pk=attempt_id)
        wr = WithdrawalRequest.objects.select_for_update().get(pk=wr_id)
        attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status in PayoutAttempt.TERMINAL_STATUSES:
            return attempt  # idempotent no-op — no audit/email below, function returns here
        was_unknown = attempt.status == PayoutAttempt.STATUS_UNKNOWN
        transition_payout_attempt(attempt, new_status, reason=reason, raw_provider_status=raw_provider_status)
        sync_withdrawal_request_status(wr, new_status, actor=actor)
        if was_unknown and new_status in (PayoutAttempt.STATUS_PROCESSING, PayoutAttempt.STATUS_COMPLETED):
            now = timezone.now()
            PayoutAttempt.objects.filter(pk=attempt.pk).update(reconciled_at=now)
            attempt.reconciled_at = now
        if new_status == PayoutAttempt.STATUS_COMPLETED and (confirmed_amount is not None or tx_hash):
            PayoutAttempt.objects.filter(pk=attempt.pk).update(
                confirmed_amount=confirmed_amount, tx_hash=tx_hash,
            )
            attempt.confirmed_amount = confirmed_amount
            attempt.tx_hash = tx_hash

        # WITHDRAWAL-ECONOMICS-01 — the single certified call site: fires
        # exactly when a withdrawal genuinely reaches COMPLETED, inside
        # this same locked transaction (the TERMINAL_STATUSES guard above
        # already makes this unreachable a second time for the same
        # attempt; the writer's own DB constraint is an independent,
        # second layer of protection — see withdrawal_economics.py).
        if new_status == PayoutAttempt.STATUS_COMPLETED:
            from .withdrawal_economics import (
                DuplicateWithdrawalFeeRevenue, record_withdrawal_fee_revenue,
            )
            try:
                record_withdrawal_fee_revenue(wr)
            except DuplicateWithdrawalFeeRevenue as exc:
                logger.warning("[payout_orchestrator] %s", exc)

    if new_status == PayoutAttempt.STATUS_COMPLETED:
        log_audit(
            request, EV_WITHDRAW_COMPLETE,
            f"Withdrawal #{wr.id} COMPLETED — payout attempt #{attempt.pk}",
            detail={
                "withdrawal_id": wr.id, "payout_attempt_id": attempt.pk, "amount_usd": str(wr.amount_usd),
                "confirmed_amount": str(confirmed_amount) if confirmed_amount is not None else None,
                "tx_hash": tx_hash or None,
            },
        )
        _send_status_email_safe(
            wr, "completed",
            confirmed_amount=attempt.confirmed_amount, tx_hash=attempt.tx_hash,
        )
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

        was_unknown = attempt.status == PayoutAttempt.STATUS_UNKNOWN
        transition_payout_attempt(
            attempt, PayoutAttempt.STATUS_FAILED,
            reason=reason, raw_provider_status=raw_provider_status,
            pre_send_rejection=pre_send_rejection,
        )
        sync_withdrawal_request_status(wr, PayoutAttempt.STATUS_FAILED, actor=actor)
        if was_unknown:
            now = timezone.now()
            PayoutAttempt.objects.filter(pk=attempt.pk).update(reconciled_at=now)
            attempt.reconciled_at = now
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


def _send_status_email_safe(wr, event_key, *, confirmed_amount=None, tx_hash=""):
    from .withdrawal_emails import (
        EVENT_APPROVED, EVENT_COMPLETED, EVENT_FAILED, send_withdrawal_status_email,
    )
    _events = {"approved": EVENT_APPROVED, "completed": EVENT_COMPLETED, "failed": EVENT_FAILED}
    try:
        send_withdrawal_status_email(
            wr, _events[event_key], confirmed_amount=confirmed_amount, tx_hash=tx_hash,
        )
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
    if event.normalized_status is None:
        # WITHDRAWAL-E2E-02B — signature-valid, but raw_status isn't in
        # this adapter's recognized vocabulary. Never guess a financial
        # transition from an unknown status — this direct-path applier
        # has no durable PayoutWebhookEvent row to route to MANUAL_REVIEW
        # (see process_webhook_event() for that), so it simply never
        # touches PayoutAttempt/WithdrawalRequest/Wallet and logs for
        # visibility.
        logger.warning(
            "[payout_orchestrator] unrecognized raw_status=%r — no transition attempted | event=%r",
            event.raw_status, event,
        )
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
    """
    FIX-02A.4 — the COMPLETED audit+email side effect used to live here
    as a post-hoc check; it's now centralized inside
    _apply_result_without_refund() itself (reused identically by
    replay/reconciliation) — this function no longer duplicates it.
    """
    if event.normalized_status == PayoutAttempt.STATUS_FAILED:
        result_attempt = _apply_confirmed_failure_with_refund(
            attempt.pk, reason=f"provider callback: {event.raw_status}",
            pre_send_rejection=False, raw_provider_status=event.raw_status, request=request,
        )
    elif event.normalized_status == PayoutAttempt.STATUS_COMPLETED:
        # WITHDRAWAL-E2E-02G FASE B — a FINISHED webhook (batch or flat
        # shape, see parse_webhook()) carries the provider's own
        # confirmed amount/hash in this event's own raw sub-payload.
        # Extraction is best-effort and never raises: a missing/odd
        # field just leaves confirmed_amount/tx_hash unset, it never
        # blocks the actual COMPLETED transition.
        confirmed_amount, tx_hash = _extract_confirmed_amount_and_hash(event.raw_event_payload)
        result_attempt = _apply_result_without_refund(
            attempt.pk, event.normalized_status, raw_provider_status=event.raw_status,
            confirmed_amount=confirmed_amount, tx_hash=tx_hash, request=request,
        )
    else:
        result_attempt = _apply_result_without_refund(
            attempt.pk, event.normalized_status, raw_provider_status=event.raw_status, request=request,
        )
    return result_attempt


def _extract_confirmed_amount_and_hash(raw_payload: dict):
    """
    WITHDRAWAL-E2E-02G FASE B — best-effort, never-raising extraction of
    the provider's own confirmed amount/hash from a raw payout payload
    (a webhook's raw_event_payload or a lookup_payout()'s raw_metadata —
    both mirror the same NowPayments payout object shape, confirmed
    against the official SDK's normalizePayout(): "amount", "hash" at
    the payload's own top level). Returns (None, "") on anything
    missing or unparseable — never fabricates a value.
    """
    raw_payload = raw_payload or {}
    amount = None
    raw_amount = raw_payload.get("amount")
    if raw_amount not in (None, ""):
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, ValueError, TypeError):
            amount = None
    tx_hash = str(raw_payload.get("hash") or "")
    return amount, tx_hash


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


# ─────────────────────────────────────────────
# 5. Durable webhook inbox — FIX-02A.4
# ─────────────────────────────────────────────

def get_or_create_webhook_event(event: ProviderPayoutEvent):
    """
    Race-safe insert into the durable, provider-agnostic webhook inbox.
    Django's documented pattern for this exact race: create() inside a
    savepoint, IntegrityError -> fetch the winner — NOT
    `if not exists(): create()`, which has a real race window on
    concurrent duplicate delivery. Works correctly on PostgreSQL (a
    concurrent INSERT on the same unique event_fingerprint either waits
    for the other transaction to resolve or fails immediately with
    IntegrityError) and on SQLite.

    Returns (event, created) — created=False means an identical event
    (same provider/refs/status/raw sub-payload) was already durably
    recorded, by this call or an earlier one.
    """
    fingerprint = compute_webhook_event_fingerprint(event)
    try:
        with transaction.atomic():
            row = PayoutWebhookEvent.objects.create(
                provider=event.provider,
                event_fingerprint=fingerprint,
                provider_reference=event.provider_reference,
                provider_batch_id=event.provider_batch_id,
                raw_status=event.raw_status,
                # WITHDRAWAL-E2E-02B — the model field is non-nullable
                # (blank=True, default=""); event.normalized_status is
                # None for an unrecognized raw_status (see
                # payout_providers.py::parse_webhook()) — persisted as
                # "" here, same "blank == not set" convention every
                # other field on this model already uses.
                normalized_status=event.normalized_status or "",
                raw_payload=event.raw_event_payload,
            )
            return row, True
    except IntegrityError:
        return PayoutWebhookEvent.objects.get(event_fingerprint=fingerprint), False


def _to_provider_event(webhook_event: PayoutWebhookEvent) -> ProviderPayoutEvent:
    """Reconstructs the dataclass resolve_payout_target()/the appliers
    expect from a durably-persisted row — used by both the live path
    (right after get_or_create_webhook_event) and replay."""
    return ProviderPayoutEvent(
        provider=webhook_event.provider,
        provider_reference=webhook_event.provider_reference,
        provider_batch_id=webhook_event.provider_batch_id,
        # WITHDRAWAL-E2E-02B — inverse of get_or_create_webhook_event()'s
        # `or ""` on write: "" on the model means "was None" (unrecognized
        # at persistence time), reconstructed as None here so replay sees
        # the exact same honest signal the live path saw.
        normalized_status=webhook_event.normalized_status or None,
        raw_status=webhook_event.raw_status,
        provider_amount=None,
        occurred_at=webhook_event.received_at,
        raw_event_payload=webhook_event.raw_payload,
    )


def _mark_webhook_event_unresolved(event_id, *, last_error, bump_retry):
    """
    Records a failed correlation/application attempt without ever
    marking RESOLVED. bump_retry=True applies the configured backoff
    and, past PAYOUT_WEBHOOK_MAX_AUTO_RETRIES, moves PENDING ->
    MANUAL_REVIEW — which means ONLY "automatic replay paused", never
    resolved/failed/purgeable (Design Lock). Ambiguous/DataIntegrityViolation
    outcomes are never auto-retried (retrying the same query cannot
    change a genuine data-ambiguity) — callers pass bump_retry=False for
    those and this function moves them to MANUAL_REVIEW immediately.
    """
    from django.conf import settings as _settings

    with transaction.atomic():
        webhook_event = PayoutWebhookEvent.objects.select_for_update().get(pk=event_id)
        if webhook_event.correlation_status == PayoutWebhookEvent.STATUS_RESOLVED:
            return  # resolved by someone else meanwhile — never downgrade a resolved event

        update = {"last_error": last_error[:2000]}
        went_manual_review = False
        if bump_retry:
            retry_count = webhook_event.retry_count + 1
            base = _settings.PAYOUT_WEBHOOK_REPLAY_BASE_SECONDS
            cap = _settings.PAYOUT_WEBHOOK_REPLAY_MAX_SECONDS
            delay_seconds = min(base * (2 ** (retry_count - 1)), cap)
            update["retry_count"] = retry_count
            update["next_retry_at"] = timezone.now() + timedelta(seconds=delay_seconds)
            if retry_count >= _settings.PAYOUT_WEBHOOK_MAX_AUTO_RETRIES:
                update["correlation_status"] = PayoutWebhookEvent.STATUS_MANUAL_REVIEW
                went_manual_review = True
        else:
            update["correlation_status"] = PayoutWebhookEvent.STATUS_MANUAL_REVIEW
            went_manual_review = True

        PayoutWebhookEvent.objects.filter(pk=event_id).update(**update)

    if went_manual_review:
        log_audit(
            None, EV_WITHDRAW_WEBHOOK_MANUAL_REVIEW,
            f"PayoutWebhookEvent #{event_id} moved to MANUAL_REVIEW — automatic replay paused",
            detail={"webhook_event_id": event_id, "last_error": last_error[:500]},
        )


def process_webhook_event(event_id, *, request=None):
    """
    FIX-02A.4 — the single pipeline for EVERY durable webhook event,
    reached identically from the live callback path (common case:
    resolves within the same request) and from the periodic replay
    task (orphan / late correlation / crash recovery). Three phases,
    deliberately NOT one giant transaction:

      TXN0 — lock the event row, read-only, resolve correlation target.
             Released immediately after (short-lived lock).
      TXN1 — the SAME authoritative appliers already used everywhere
             else (_apply_result_without_refund /
             _apply_confirmed_failure_with_refund /
             _apply_funded_internal_webhook /
             _apply_legacy_withdrawal_webhook) — own lock order, own
             idempotent TERMINAL_STATUSES guard. NEVER duplicated here.
      TXN2 — mark RESOLVED, a guarded UPDATE, only after TXN1 returns
             successfully.

    If the process crashes between TXN1 and TXN2, the event simply
    stays PENDING — a later call (replay) re-enters at TXN0, re-derives
    the same target, calls TXN1 again. The applier's own
    TERMINAL_STATUSES guard makes that second call a pure no-op — no
    second refund, no second completion, no second email, no second
    audit entry — and THIS time TXN2 runs and marks RESOLVED.

    Concurrent duplicate callers (two live deliveries, two replay
    workers, live+replay) all funnel through the same TXN0 lock on this
    row; the loser blocks, then re-reads fresh state. Redundant TXN1
    calls across such races are safe (not wasted-but-unsafe) purely
    because the deeper PayoutAttempt-level lock/guard already
    guarantees idempotency — this function does not itself claim
    "exactly one worker ever calls TXN1".
    """
    with transaction.atomic():
        webhook_event = PayoutWebhookEvent.objects.select_for_update().get(pk=event_id)
        if webhook_event.correlation_status == PayoutWebhookEvent.STATUS_RESOLVED:
            return webhook_event, None  # Caso B — already resolved, nothing to do
        target = resolve_payout_target(_to_provider_event(webhook_event))
    # TXN0 committed (read-only); lock released here.

    if isinstance(target, Ambiguous):
        logger.error("[payout_orchestrator] webhook event #%d AMBIGUOUS — %s", event_id, target.reason)
        _mark_webhook_event_unresolved(event_id, last_error=f"Ambiguous: {target.reason}", bump_retry=False)
        return webhook_event, target
    if isinstance(target, DataIntegrityViolation):
        logger.error("[payout_orchestrator] webhook event #%d DATA INTEGRITY VIOLATION — %s", event_id, target.reason)
        _mark_webhook_event_unresolved(event_id, last_error=f"DataIntegrityViolation: {target.reason}", bump_retry=False)
        return webhook_event, target
    if isinstance(target, Orphan):
        logger.info("[payout_orchestrator] webhook event #%d still orphan — %s", event_id, target.reason)
        _mark_webhook_event_unresolved(event_id, last_error=target.reason, bump_retry=True)
        return webhook_event, target

    event = _to_provider_event(webhook_event)
    if event.normalized_status is None:
        # WITHDRAWAL-E2E-02B — signature-valid webhook, correlated to a
        # real target, but raw_status isn't in our recognized
        # vocabulary (e.g. a future NowPayments status we've never seen
        # — this exact gap silently dropped WR17's two real REJECTED
        # webhooks before this block). Never guess a financial
        # transition: route straight to MANUAL_REVIEW — same
        # non-retryable path already used for Ambiguous/
        # DataIntegrityViolation — and skip TXN2 entirely, so the event
        # never gets marked RESOLVED while genuinely unactioned.
        logger.warning(
            "[payout_orchestrator] webhook event #%d has unrecognized raw_status=%r — "
            "MANUAL_REVIEW, no transition attempted", event_id, webhook_event.raw_status,
        )
        _mark_webhook_event_unresolved(
            event_id, last_error=f"unrecognized provider status: {webhook_event.raw_status!r}",
            bump_retry=False,
        )
        return webhook_event, target

    attempt_for_fk = None
    if isinstance(target, FundedInternalMatch):
        _apply_funded_internal_webhook(target.withdrawal_request, event)
    elif isinstance(target, LegacyWithdrawalMatch):
        _apply_legacy_withdrawal_webhook(target.withdrawal_request, event, request=request)
    elif isinstance(target, AttemptMatch):
        attempt_for_fk = _apply_attempt_webhook(target.attempt, event, request=request)

    # TXN2 — mark RESOLVED. A guarded UPDATE (WHERE on current status)
    # is itself atomic/check-and-set — no separate select_for_update
    # needed for this step, and safe if two callers reach it concurrently.
    PayoutWebhookEvent.objects.filter(
        pk=event_id,
        correlation_status__in=[PayoutWebhookEvent.STATUS_PENDING, PayoutWebhookEvent.STATUS_MANUAL_REVIEW],
    ).update(
        correlation_status=PayoutWebhookEvent.STATUS_RESOLVED,
        resolved_at=timezone.now(),
        correlated_attempt=attempt_for_fk if isinstance(attempt_for_fk, PayoutAttempt) else None,
    )
    return webhook_event, target


# ─────────────────────────────────────────────
# 6. UNKNOWN reconciliation — FIX-02A.4, provider-agnostic
# ─────────────────────────────────────────────

def reconcile_unknown_payout_attempts(*, batch_size=100, actor=None, request=None):
    """
    Two independent steps, both provider-agnostic — this function never
    imports NowPaymentsAdapter or references any provider-specific
    status string:

      1. Aged SUBMITTED -> UNKNOWN. Purely structural (submitted_at vs.
         settings.PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS) — no
         provider call.
      2. Active lookup_payout() for UNKNOWN attempts whose adapter
         declares at least one lookup capability. Capability-gated: if
         none of supports_lookup_by_provider_reference/
         _provider_request_id/_batch is True, lookup_payout() is never
         even called (NowPayments today: none are True — this whole
         branch is a documented, verified no-op for it, not a design
         gap — see payout_providers.py::NowPaymentsAdapter).

    NEVER creates a PayoutAttempt. NEVER calls adapter.create_payout().
    NEVER refunds except through _apply_confirmed_failure_with_refund's
    own confirmed-FAILED path (FOUND_FAILED only) — NOT_FOUND/
    UNAVAILABLE/AMBIGUOUS/UNSUPPORTED all leave the attempt in UNKNOWN,
    unmutated, exactly per Design Lock point 8/9.
    """
    now = timezone.now()
    from django.conf import settings as _settings

    result = {"aged_to_unknown": 0, "checked": 0, "resolved": 0, "still_unknown": 0}

    # Step 1 — aged SUBMITTED -> UNKNOWN.
    threshold = _settings.PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS
    aged_ids = list(
        PayoutAttempt.objects.filter(
            status=PayoutAttempt.STATUS_SUBMITTED,
            submitted_at__lte=now - timedelta(seconds=threshold),
        ).values_list("pk", flat=True)[:batch_size]
    )
    for attempt_id in aged_ids:
        attempt = _apply_result_without_refund(
            attempt_id, PayoutAttempt.STATUS_UNKNOWN,
            reason=f"aged SUBMITTED — no response observed within {threshold}s",
            actor=actor, request=request,
        )
        if attempt.status == PayoutAttempt.STATUS_UNKNOWN:
            result["aged_to_unknown"] += 1
            log_audit(
                request, EV_WITHDRAW_AGED_SUBMITTED_TO_UNKNOWN,
                f"PayoutAttempt #{attempt_id} aged SUBMITTED -> UNKNOWN after {threshold}s",
                detail={"payout_attempt_id": attempt_id, "threshold_seconds": threshold},
            )

    # Step 2 — active lookup for UNKNOWN attempts. Never-checked
    # (reconciliation_checked_at IS NULL) attempts are prioritized.
    unknown_ids = list(
        PayoutAttempt.objects.filter(status=PayoutAttempt.STATUS_UNKNOWN)
        .order_by(F("reconciliation_checked_at").asc(nulls_first=True))
        .values_list("pk", flat=True)[:batch_size]
    )
    for attempt_id in unknown_ids:
        attempt = PayoutAttempt.objects.get(pk=attempt_id)
        adapter = get_adapter_for_provider(attempt.provider)
        capable = any(
            adapter.capabilities.get(k, False)
            for k in (
                "supports_lookup_by_provider_reference",
                "supports_lookup_by_provider_request_id",
                "supports_lookup_by_batch",
            )
        )
        result["checked"] += 1
        if not capable:
            PayoutAttempt.objects.filter(pk=attempt_id).update(reconciliation_checked_at=timezone.now())
            result["still_unknown"] += 1
            logger.debug(
                "[payout_orchestrator] UNKNOWN attempt #%d provider=%s has no lookup "
                "capability — staying UNKNOWN, no HTTP attempted", attempt_id, attempt.provider,
            )
            continue

        lookup_result = adapter.lookup_payout(attempt)
        PayoutAttempt.objects.filter(pk=attempt_id).update(reconciliation_checked_at=timezone.now())

        if _apply_lookup_outcome(attempt_id, lookup_result, from_label="UNKNOWN", actor=actor, request=request):
            result["resolved"] += 1
        else:
            result["still_unknown"] += 1

    return result


def _apply_lookup_outcome(attempt_id, lookup_result, *, from_label: str, actor=None, request=None) -> bool:
    """
    WITHDRAWAL-E2E-02G FASE B — the outcome-dispatch logic shared by
    both active-reconciliation callers (UNKNOWN's existing step 2, and
    PROCESSING's new step 3 below): given a lookup_payout() result,
    apply the matching PayoutAttempt transition through the same
    authoritative appliers used everywhere else, or leave the attempt
    untouched. Returns True if the attempt was resolved (moved out of
    its prior status), False otherwise (caller decides what counter
    that maps to). from_label only affects audit message text.
    """
    outcome = lookup_result.outcome

    if outcome == PayoutLookupOutcome.FOUND_PROCESSING:
        # WITHDRAWAL-E2E-02G FASE B — ALLOWED_TRANSITIONS[PROCESSING] has
        # no PROCESSING -> PROCESSING self-loop (deliberate — see
        # WITHDRAWAL-E2E-02C's design report). UNKNOWN -> PROCESSING is a
        # real, allowed transition (this branch's original purpose); but
        # this same branch is now also reached from
        # reconcile_processing_payout_attempts(), where "provider still
        # says processing" for an attempt ALREADY processing is not a
        # transition at all — it's confirmation that nothing changed.
        # Skip the transition call entirely in that case rather than
        # crash on an invalid same-status transition.
        current_status = PayoutAttempt.objects.filter(pk=attempt_id).values_list("status", flat=True).first()
        if current_status == PayoutAttempt.STATUS_PROCESSING:
            logger.info(
                "[payout_orchestrator] %s attempt #%d still PROCESSING per provider — no change",
                from_label, attempt_id,
            )
            return False
        _apply_result_without_refund(
            attempt_id, PayoutAttempt.STATUS_PROCESSING,
            raw_provider_status=lookup_result.raw_provider_status, actor=actor, request=request,
        )
        log_audit(
            request, EV_WITHDRAW_RECONCILIATION_RESOLVED,
            f"PayoutAttempt #{attempt_id} reconciled {from_label} -> PROCESSING",
            detail={"payout_attempt_id": attempt_id, "outcome": outcome.value},
        )
        return True

    if outcome == PayoutLookupOutcome.FOUND_COMPLETED:
        confirmed_amount, tx_hash = _extract_confirmed_amount_and_hash(lookup_result.raw_metadata)
        _apply_result_without_refund(
            attempt_id, PayoutAttempt.STATUS_COMPLETED,
            raw_provider_status=lookup_result.raw_provider_status,
            confirmed_amount=confirmed_amount, tx_hash=tx_hash, actor=actor, request=request,
        )
        log_audit(
            request, EV_WITHDRAW_RECONCILIATION_RESOLVED,
            f"PayoutAttempt #{attempt_id} reconciled {from_label} -> COMPLETED",
            detail={"payout_attempt_id": attempt_id, "outcome": outcome.value},
        )
        return True

    if outcome == PayoutLookupOutcome.FOUND_FAILED:
        _apply_confirmed_failure_with_refund(
            attempt_id, reason="reconciliation: provider confirms FAILED",
            pre_send_rejection=False, raw_provider_status=lookup_result.raw_provider_status,
            actor=actor, request=request,
        )
        log_audit(
            request, EV_WITHDRAW_RECONCILIATION_RESOLVED,
            f"PayoutAttempt #{attempt_id} reconciled {from_label} -> FAILED (refunded exactly once)",
            detail={"payout_attempt_id": attempt_id, "outcome": outcome.value},
        )
        return True

    # NOT_FOUND / UNAVAILABLE / AMBIGUOUS / UNSUPPORTED — stays as-is,
    # zero mutation. Deliberately not logged via log_audit (would spam
    # AuditLog every cycle for an unresolved attempt) — logger only.
    logger.info(
        "[payout_orchestrator] %s attempt #%d reconciliation outcome=%s — no change",
        from_label, attempt_id, outcome.value,
    )
    return False


def reconcile_processing_payout_attempts(*, batch_size=100, actor=None, request=None):
    """
    WITHDRAWAL-E2E-02G FASE B — active GET reconciliation for
    PayoutAttempts already in PROCESSING (item 3 of the FASE A design).
    Distinct from reconcile_unknown_payout_attempts(): that function
    only ever rescues STATUS_UNKNOWN, which left a genuinely-in-flight,
    genuinely-successful attempt like WR18's (webhook never correlated,
    reconciliation never eligible) with no automatic path back to
    COMPLETED at all.

    Eligibility, all three required: status == PROCESSING,
    provider_reference is set (nothing to look up otherwise), and
    updated_at is older than PAYOUT_PROCESSING_AGED_RECONCILE_SECONDS —
    the same "never race a payout that might still be legitimately in
    flight" discipline as the SUBMITTED->UNKNOWN aging step, sized well
    above WR18's real end-to-end cycle (~3 minutes).

    NEVER creates a PayoutAttempt. NEVER calls adapter.create_payout().
    NEVER refunds except through _apply_confirmed_failure_with_refund's
    own confirmed-FAILED path. Reuses the exact same lookup_payout() /
    outcome dispatch as UNKNOWN reconciliation (_apply_lookup_outcome) —
    same idempotent TERMINAL_STATUSES guard, so a webhook and this
    reconciliation racing on the same attempt can never double-apply.
    """
    from django.conf import settings as _settings

    now = timezone.now()
    threshold = _settings.PAYOUT_PROCESSING_AGED_RECONCILE_SECONDS
    result = {"checked": 0, "resolved": 0, "still_processing": 0}

    processing_ids = list(
        PayoutAttempt.objects.filter(
            status=PayoutAttempt.STATUS_PROCESSING,
        ).exclude(provider_reference="").filter(
            updated_at__lte=now - timedelta(seconds=threshold),
        ).order_by(F("reconciliation_checked_at").asc(nulls_first=True))
        .values_list("pk", flat=True)[:batch_size]
    )
    for attempt_id in processing_ids:
        attempt = PayoutAttempt.objects.get(pk=attempt_id)
        adapter = get_adapter_for_provider(attempt.provider)
        capable = any(
            adapter.capabilities.get(k, False)
            for k in (
                "supports_lookup_by_provider_reference",
                "supports_lookup_by_provider_request_id",
                "supports_lookup_by_batch",
            )
        )
        result["checked"] += 1
        if not capable:
            PayoutAttempt.objects.filter(pk=attempt_id).update(reconciliation_checked_at=timezone.now())
            result["still_processing"] += 1
            continue

        lookup_result = adapter.lookup_payout(attempt)
        PayoutAttempt.objects.filter(pk=attempt_id).update(reconciliation_checked_at=timezone.now())

        if _apply_lookup_outcome(attempt_id, lookup_result, from_label="PROCESSING", actor=actor, request=request):
            result["resolved"] += 1
        else:
            result["still_processing"] += 1

    return result
