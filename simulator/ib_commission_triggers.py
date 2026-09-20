"""
simulator/ib_commission_triggers.py
IB-COMMISSION-TRIGGERS-02A — additive reconciliation-sweep consumer layer.

Design Lock: IB-COMMISSION-TRIGGERS-02 — DESIGN LOCK V1 (revised, per
Owner engine-protection directive). Hard architectural rule this module
exists to satisfy: IB MAY CONSUME EXISTING DURABLE EVENTS. IB MUST NOT
CHANGE THE ENGINE THAT PRODUCES THEM.

This module contains ONLY read-then-generate sweep functions — it never
writes to LotExecutionEvent, Deposit, ChallengeEnrollment, or any
protected engine's own tables. It calls the existing, unmodified
generator functions in simulator/ib_commission.py, which are themselves
idempotent (DB-constraint-backed) — running any sweep here twice, or
concurrently, or over overlapping time windows, converges on the same
set of IBCommissionObligation rows, never duplicates.

Scope: PER_LOT, CHALLENGE_PERCENT, DEPOSIT_PERCENT only.
SPREAD_REVENUE_SHARE / TRADING_COMMISSION_REVENUE_SHARE / CPA_BONUS are
explicitly on HOLD / POLICY_PENDING — no sweep functions for them exist
here (see IB-COMMISSION-TRIGGERS-02 Design Lock V1, sections F/G/H/Q).

Every sweep function below returns a summary dict
{"scanned": int, "generated": int, "skipped": int} — "generated" counts
rows for which a NEW IBCommissionObligation was created this call;
"skipped" counts rows that already had one (idempotent no-op) or for
which the generator legitimately returned None (no attribution / no
rule / ambiguous rule / policy-excluded).
"""
import logging

from .ib_commission import (
    generate_challenge_percent_obligation, generate_deposit_percent_obligation,
    generate_per_lot_obligation,
)
from .models import ChallengeEnrollment, Deposit, IBCommissionObligation, LotExecutionEvent

logger = logging.getLogger("simulator.ib_commission_triggers")


def _already_obligated(source_event_type, source_event_id):
    """
    Cheap pre-check used only for this module's own "generated" vs.
    "skipped" bookkeeping below — NOT the idempotency guarantee itself
    (that remains entirely owned by IBCommissionObligation's own DB
    UniqueConstraint, enforced inside each generate_*_obligation() call
    regardless of this check). source_event_type/source_event_id alone
    are sufficient here: they're already known from the source row
    itself, before resolving which referral (if any) applies, and that
    tuple's uniqueness doesn't depend on the referral half of the real
    constraint — at most one obligation of a given rule_type can ever
    exist for a given source event, for any referral.
    """
    return IBCommissionObligation.objects.filter(
        source_event_type=source_event_type, source_event_id=source_event_id,
    ).exists()


def sweep_per_lot(cutoff, batch_size=500):
    """
    Scan LotExecutionEvent rows created at or after `cutoff` and generate
    any missing PER_LOT IBCommissionObligation for each, via the
    unmodified generate_per_lot_obligation(). Never touches
    LotExecutionEvent or the engine that writes it.
    """
    events = list(
        LotExecutionEvent.objects.filter(created_at__gte=cutoff)
        .order_by("id")[:batch_size]
    )
    generated = 0
    skipped = 0
    for event in events:
        if _already_obligated("per_lot_execution", event.pk):
            skipped += 1
            continue
        try:
            obligation = generate_per_lot_obligation(event)
        except Exception:
            logger.exception(
                "[ib_commission_triggers] sweep_per_lot failed for "
                "lot_execution_event=%d — skipping, next sweep will retry",
                event.pk,
            )
            skipped += 1
            continue
        if obligation is None:
            skipped += 1
        else:
            generated += 1
    result = {"scanned": len(events), "generated": generated, "skipped": skipped}
    logger.info("[ib_commission_triggers] sweep_per_lot %s", result)
    return result


def sweep_challenge_percent(cutoff, batch_size=500):
    """
    Scan ChallengeEnrollment rows created (enrolled_at) at or after
    `cutoff` and generate any missing CHALLENGE_PERCENT
    IBCommissionObligation for each, via the unmodified
    generate_challenge_percent_obligation() — which itself enforces the
    locked Owner policy that deposit=None (admin/manual) enrollments
    never generate a commission. Never touches ChallengeEnrollment or the
    payment engine that writes it.
    """
    enrollments = list(
        ChallengeEnrollment.objects.filter(enrolled_at__gte=cutoff)
        .select_related("product")
        .order_by("id")[:batch_size]
    )
    generated = 0
    skipped = 0
    for enrollment in enrollments:
        if _already_obligated("challenge_enrollment", enrollment.pk):
            skipped += 1
            continue
        try:
            obligation = generate_challenge_percent_obligation(enrollment)
        except Exception:
            logger.exception(
                "[ib_commission_triggers] sweep_challenge_percent failed for "
                "enrollment=%d — skipping, next sweep will retry",
                enrollment.pk,
            )
            skipped += 1
            continue
        if obligation is None:
            skipped += 1
        else:
            generated += 1
    result = {"scanned": len(enrollments), "generated": generated, "skipped": skipped}
    logger.info("[ib_commission_triggers] sweep_challenge_percent %s", result)
    return result


def sweep_deposit_percent(cutoff, batch_size=500):
    """
    Scan credited, non-challenge Deposit rows (credited_at at or after
    `cutoff`) and generate any missing DEPOSIT_PERCENT
    IBCommissionObligation for each, via the unmodified
    generate_deposit_percent_obligation(). Never touches Deposit or
    deposit_callback.
    """
    deposits = list(
        Deposit.objects.filter(
            credited=True, challenge_product__isnull=True, credited_at__gte=cutoff,
        ).order_by("id")[:batch_size]
    )
    generated = 0
    skipped = 0
    for deposit in deposits:
        if _already_obligated("deposit", deposit.pk):
            skipped += 1
            continue
        try:
            obligation = generate_deposit_percent_obligation(deposit)
        except Exception:
            logger.exception(
                "[ib_commission_triggers] sweep_deposit_percent failed for "
                "deposit=%d — skipping, next sweep will retry",
                deposit.pk,
            )
            skipped += 1
            continue
        if obligation is None:
            skipped += 1
        else:
            generated += 1
    result = {"scanned": len(deposits), "generated": generated, "skipped": skipped}
    logger.info("[ib_commission_triggers] sweep_deposit_percent %s", result)
    return result
