# simulator/ib_risk_holds.py
"""
IB-RISK-HOLDS-07B — freeze (IB-level) / hold (obligation-level) service.

Approved design: IB-RISK-HOLDS-07A audit + design lock. Implements
exactly the architecture locked there:
  - freeze_referral()   / unfreeze_referral()   — Referral.risk_status
  - hold_obligation()   / release_obligation()  — IBCommissionObligation.
    is_held
  - Every transition writes one IBRiskEvent row (append-only, permanent
    history — see that model's own docstring) in the SAME atomic
    transaction as the state change itself.

Idempotency posture (deliberate, matching this module's own precedent —
approve_obligation()/reject_treasury_request()'s "one-shot staff action"
convention): all four functions here RAISE on a repeat call (freezing an
already-frozen IB, unfreezing an already-active one, holding an
already-held obligation, releasing a not-held one) — the caller must
know explicitly that nothing happened, never assume success silently.
This is a deliberate contrast with the OTHER established posture in
this codebase (link_treasury_request()/sync_obligation_from_treasury()
are silently idempotent, safe for retries/reconciliation sweeps) — these
four functions are staff decisions, not retryable observations, so the
raise-on-repeat convention applies instead.

Permission: all four reuse TREASURY_REVIEW_PERMISSION
("simulator.can_review_treasury_request") — the same permission
approve_obligation()/reject_adjustment() already gate on. No new
permission is invented for this block (explicit Owner decision).

Reason: required (non-empty after strip) for all four actions — no
silent/blank freeze, unfreeze, hold, or release.

Unfreeze/release semantics (locked): PURE restoration of eligibility.
Neither function approves, links, executes, or otherwise advances any
obligation or Treasury request — they only clear the blocking flag so
that a SUBSEQUENT, separate staff action (approve_obligation()/
link_treasury_request(), called independently, exactly as it already
is today) becomes possible again. No auto-approve, no auto-link, no
auto-execute anywhere in this module.

Money-movement boundary (locked, IB-RISK-HOLDS-07A section L): this
module NEVER touches TreasuryOperationRequest, Wallet, or
WalletTransaction, directly or indirectly. It only ever writes to
Referral.risk_status/frozen_*, IBCommissionObligation.is_held/held_*,
and IBRiskEvent. The actual blocking effect of a freeze/hold is entirely
enforced elsewhere, in the two existing guard clauses this same block
adds to simulator/ib_treasury_settlement.py (approve_obligation()/
link_treasury_request()) — this module has no power to stop money that
has already reached a TreasuryOperationRequest, and does not pretend to.

Reconciliation (sync_obligation_from_treasury()/
reconcile_approved_obligations()) and the reversal/adjustment path
(simulator/ib_commission_reversal.py) are both DELIBERATELY untouched by
this entire block — neither file is imported or modified here, and
neither reads is_held/risk_status anywhere. Both continue to operate
identically regardless of freeze/hold state (IB-RISK-HOLDS-07A sections
M and N).

Never touches simulator/consumers.py, simulator/population_engine.py,
simulator/payout_orchestrator.py, simulator/treasury_requests.py,
simulator/wallet_ledger.py, simulator/ib_commission_reversal.py, or
simulator/ib_commission_triggers.py, directly or indirectly.
"""
import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import IBCommissionObligation, IBRiskEvent, Referral
from .treasury_requests import TREASURY_REVIEW_PERMISSION

logger = logging.getLogger("simulator.ib_risk_holds")


class ReferralAlreadyFrozen(Exception):
    """Raised by freeze_referral() when the referral's current
    risk_status is already FROZEN (re-checked under lock)."""


class ReferralNotFrozen(Exception):
    """Raised by unfreeze_referral() when the referral's current
    risk_status is not FROZEN (re-checked under lock)."""


class ObligationAlreadyHeld(Exception):
    """Raised by hold_obligation() when the obligation's current
    is_held is already True (re-checked under lock)."""


class ObligationNotHeld(Exception):
    """Raised by release_obligation() when the obligation's current
    is_held is already False (re-checked under lock)."""


def _require_review_permission(request):
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required for this IB risk action.")
    if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
        raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")


def _require_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required for this IB risk action.")
    return reason


# ─────────────────────────────────────────────
# IB-level freeze / unfreeze — Referral.risk_status
# ─────────────────────────────────────────────

def freeze_referral(referral, reason, *, request):
    """
    Transition a Referral (IB) from ACTIVE to FROZEN.

    Blocks (via the guard clauses in ib_treasury_settlement.py):
      - generation of any NEW obligation for this IB (checked in
        ib_commission.py's four generators, via _referral_is_active()).
      - approve_obligation() on any of this IB's PENDING obligations.
      - link_treasury_request() on any of this IB's APPROVED obligations.

    Does NOT touch (locked, out of scope):
      - obligations already linked to a TreasuryOperationRequest —
        Treasury's own state machine remains the sole authority from
        that point on, regardless of this IB's risk_status.
      - reconciliation — sync_obligation_from_treasury() still credits
        an obligation whose Treasury request executed before, during,
        or after this freeze.
      - IB-REVERSALS-FRAUD-05's adjustment/reversal path — fully
        unaffected, on purpose.

    Args:
        referral: a Referral — only its .pk is used; re-read under
                  select_for_update(), never trusted for risk_status
                  (could be stale).
        reason:   required, non-empty staff-supplied explanation.
        request:  the current HttpRequest — request.user must be
                  authenticated and hold TREASURY_REVIEW_PERMISSION.

    Returns:
        The locked, updated Referral instance (risk_status=FROZEN).

    Raises:
        PermissionDenied:      request.user not authenticated, or lacks
                                TREASURY_REVIEW_PERMISSION.
        ValueError:             reason is blank.
        ReferralAlreadyFrozen: current risk_status is already FROZEN.
    """
    _require_review_permission(request)
    reason = _require_reason(reason)

    with transaction.atomic():
        locked = Referral.objects.select_for_update().get(pk=referral.pk)

        if locked.risk_status == Referral.RISK_FROZEN:
            raise ReferralAlreadyFrozen(
                f"Referral #{locked.pk} is already FROZEN."
            )

        now = timezone.now()
        locked.risk_status = Referral.RISK_FROZEN
        locked.frozen_at = now
        locked.frozen_by = request.user
        locked.frozen_reason = reason
        locked.save(update_fields=["risk_status", "frozen_at", "frozen_by", "frozen_reason"])

        IBRiskEvent.objects.create(
            event_type=IBRiskEvent.EVENT_FROZEN,
            referral=locked,
            obligation=None,
            actor=request.user,
            reason=reason,
        )
    # ── transaction closed — the FROZEN transition is committed from here on ──

    logger.info(
        "[ib_risk_holds] referral=%d frozen by user=%d reason=%r",
        locked.pk, request.user.pk, reason,
    )
    return locked


def unfreeze_referral(referral, reason, *, request):
    """
    Transition a Referral (IB) from FROZEN back to ACTIVE.

    PURE restoration of eligibility — does not approve, link, or
    execute anything. Any obligation that was PENDING/APPROVED while
    frozen still requires its own, separate approve_obligation()/
    link_treasury_request() call afterward, exactly as before this
    block existed.

    Clears frozen_at/frozen_by/frozen_reason back to blank — those
    fields describe the CURRENT freeze episode only; permanent history
    of this freeze (and its reason) is preserved in the IBRiskEvent row
    already written by freeze_referral(), plus the UNFROZEN row written
    here.

    Args:
        referral: a Referral — only its .pk is used; re-read under
                  select_for_update().
        reason:   required, non-empty staff-supplied explanation.
        request:  the current HttpRequest — request.user must be
                  authenticated and hold TREASURY_REVIEW_PERMISSION.

    Returns:
        The locked, updated Referral instance (risk_status=ACTIVE).

    Raises:
        PermissionDenied:  request.user not authenticated, or lacks
                            TREASURY_REVIEW_PERMISSION.
        ValueError:         reason is blank.
        ReferralNotFrozen: current risk_status is not FROZEN.
    """
    _require_review_permission(request)
    reason = _require_reason(reason)

    with transaction.atomic():
        locked = Referral.objects.select_for_update().get(pk=referral.pk)

        if locked.risk_status != Referral.RISK_FROZEN:
            raise ReferralNotFrozen(
                f"Referral #{locked.pk} is not FROZEN (risk_status={locked.risk_status})."
            )

        locked.risk_status = Referral.RISK_ACTIVE
        locked.frozen_at = None
        locked.frozen_by = None
        locked.frozen_reason = ""
        locked.save(update_fields=["risk_status", "frozen_at", "frozen_by", "frozen_reason"])

        IBRiskEvent.objects.create(
            event_type=IBRiskEvent.EVENT_UNFROZEN,
            referral=locked,
            obligation=None,
            actor=request.user,
            reason=reason,
        )
    # ── transaction closed — the ACTIVE transition is committed from here on ──

    logger.info(
        "[ib_risk_holds] referral=%d unfrozen by user=%d reason=%r",
        locked.pk, request.user.pk, reason,
    )
    return locked


# ─────────────────────────────────────────────
# Obligation-level hold / release — IBCommissionObligation.is_held
# ─────────────────────────────────────────────

def hold_obligation(obligation, reason, *, request):
    """
    Set is_held=True on a single IBCommissionObligation.

    Orthogonal to `status` (IB-RISK-HOLDS-07A section G) — a hold can be
    placed on an obligation in any status. Its only operational effect
    is via the guard clauses in approve_obligation()/
    link_treasury_request() (ib_treasury_settlement.py), so holding an
    obligation already past those stages (CREDITED) has no further
    effect beyond the flag itself — this function does not restrict
    which status may be held, per the same "no unnecessary states"
    discipline this whole IB program already follows elsewhere.

    Args:
        obligation: an IBCommissionObligation — only its .pk is used;
                    re-read under select_for_update().
        reason:     required, non-empty staff-supplied explanation.
        request:    the current HttpRequest — request.user must be
                    authenticated and hold TREASURY_REVIEW_PERMISSION.

    Returns:
        The locked, updated IBCommissionObligation instance (is_held=True).

    Raises:
        PermissionDenied:       request.user not authenticated, or
                                 lacks TREASURY_REVIEW_PERMISSION.
        ValueError:              reason is blank.
        ObligationAlreadyHeld:  current is_held is already True.
    """
    _require_review_permission(request)
    reason = _require_reason(reason)

    with transaction.atomic():
        locked = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if locked.is_held:
            raise ObligationAlreadyHeld(
                f"IBCommissionObligation #{locked.pk} is already held."
            )

        now = timezone.now()
        locked.is_held = True
        locked.held_at = now
        locked.held_by = request.user
        locked.hold_reason = reason
        locked.save(update_fields=["is_held", "held_at", "held_by", "hold_reason"])

        IBRiskEvent.objects.create(
            event_type=IBRiskEvent.EVENT_OBLIGATION_HELD,
            referral_id=locked.referral_id,
            obligation=locked,
            actor=request.user,
            reason=reason,
        )
    # ── transaction closed — the hold is committed from here on ──

    logger.info(
        "[ib_risk_holds] obligation=%d held by user=%d reason=%r",
        locked.pk, request.user.pk, reason,
    )
    return locked


def release_obligation(obligation, reason, *, request):
    """
    Set is_held=False on a single IBCommissionObligation.

    PURE restoration of eligibility — does not approve or link anything.
    A held-then-released PENDING/APPROVED obligation still requires its
    own, separate approve_obligation()/link_treasury_request() call
    afterward.

    Clears held_at/held_by/hold_reason back to blank — same discipline
    as unfreeze_referral(); permanent history is preserved via the
    IBRiskEvent rows already written by hold_obligation() and this call.

    Args:
        obligation: an IBCommissionObligation — only its .pk is used;
                    re-read under select_for_update().
        reason:     required, non-empty staff-supplied explanation.
        request:    the current HttpRequest — request.user must be
                    authenticated and hold TREASURY_REVIEW_PERMISSION.

    Returns:
        The locked, updated IBCommissionObligation instance (is_held=False).

    Raises:
        PermissionDenied:  request.user not authenticated, or lacks
                            TREASURY_REVIEW_PERMISSION.
        ValueError:         reason is blank.
        ObligationNotHeld: current is_held is already False.
    """
    _require_review_permission(request)
    reason = _require_reason(reason)

    with transaction.atomic():
        locked = IBCommissionObligation.objects.select_for_update().get(pk=obligation.pk)

        if not locked.is_held:
            raise ObligationNotHeld(
                f"IBCommissionObligation #{locked.pk} is not held."
            )

        locked.is_held = False
        locked.held_at = None
        locked.held_by = None
        locked.hold_reason = ""
        locked.save(update_fields=["is_held", "held_at", "held_by", "hold_reason"])

        IBRiskEvent.objects.create(
            event_type=IBRiskEvent.EVENT_OBLIGATION_RELEASED,
            referral_id=locked.referral_id,
            obligation=locked,
            actor=request.user,
            reason=reason,
        )
    # ── transaction closed — the release is committed from here on ──

    logger.info(
        "[ib_risk_holds] obligation=%d released by user=%d reason=%r",
        locked.pk, request.user.pk, reason,
    )
    return locked
