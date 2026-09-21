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

Scope: PER_LOT, CHALLENGE_PERCENT, DEPOSIT_PERCENT,
TRADING_COMMISSION_REVENUE_SHARE, SPREAD_REVENUE_SHARE
(IB-COMMISSION-PARITY-09C.1 — sweep_spread_revenue_share() below).
CPA_BONUS remains explicitly on HOLD / POLICY_PENDING — no sweep
function for it exists here (see IB-COMMISSION-TRIGGERS-02 Design Lock
V1, sections F/G/H/Q).

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
    generate_per_lot_obligation, generate_spread_revenue_share_obligation,
    generate_trading_commission_revenue_share_obligation,
)
from .models import (
    BrokerLedger, ChallengeEnrollment, Deposit, IBCommissionObligation,
    LotExecutionEvent,
)

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


def sweep_trading_commission_revenue_share(cutoff, batch_size=500):
    """
    IB-COMMISSION-TRIGGERS-02C. Scan BrokerLedger REV_COMMISSION rows
    created at or after `cutoff` and generate any missing
    TRADING_COMMISSION_REVENUE_SHARE IBCommissionObligation for each, via
    the unmodified generate_trading_commission_revenue_share_obligation().
    Reads BrokerLedger directly — the already-durable broker revenue
    record — and never scans/recomputes from Trade, Position,
    LotExecutionEvent, or LedgerEntry. Never touches BrokerLedger or the
    engine that writes it. Never consumes REV_SPREAD (excluded by the
    revenue_type filter below) — that is sweep_spread_revenue_share()'s
    own, separate domain (IB-COMMISSION-PARITY-09C.1).
    """
    rows = list(
        BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_COMMISSION, created_at__gte=cutoff,
        ).order_by("id")[:batch_size]
    )
    generated = 0
    skipped = 0
    for row in rows:
        if _already_obligated("broker_ledger_commission", row.pk):
            skipped += 1
            continue
        try:
            obligation = generate_trading_commission_revenue_share_obligation(row)
        except Exception:
            logger.exception(
                "[ib_commission_triggers] sweep_trading_commission_revenue_share "
                "failed for broker_ledger=%d — skipping, next sweep will retry",
                row.pk,
            )
            skipped += 1
            continue
        if obligation is None:
            skipped += 1
        else:
            generated += 1
    result = {"scanned": len(rows), "generated": generated, "skipped": skipped}
    logger.info("[ib_commission_triggers] sweep_trading_commission_revenue_share %s", result)
    return result


def sweep_spread_revenue_share(cutoff, batch_size=500):
    """
    IB-COMMISSION-PARITY-09C.1. Scan BrokerLedger REV_SPREAD rows
    created at or after `cutoff` and generate any missing
    SPREAD_REVENUE_SHARE IBCommissionObligation for each, via the
    unmodified generate_spread_revenue_share_obligation(). Reads
    BrokerLedger directly — the already-durable broker revenue record —
    and never scans/recomputes from Trade, Position, LotExecutionEvent,
    or LedgerEntry, never re-queries market data. Never touches
    BrokerLedger or the engine that writes it. Never consumes
    REV_COMMISSION (excluded by the revenue_type filter below) — that
    is sweep_trading_commission_revenue_share()'s own, separate domain.

    IB-COMMISSION-PARITY-09C's certified fact, unchanged by this
    function: REV_SPREAD rows simply do not exist for the pending/stop/
    limit-trigger execution path — this sweep only ever finds rows for
    executions where one was actually written, exactly mirroring the
    (already-shipped, already-certified) commission sweep's own
    "scan what's durable, never synthesize" discipline.
    """
    rows = list(
        BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_SPREAD, created_at__gte=cutoff,
        ).order_by("id")[:batch_size]
    )
    generated = 0
    skipped = 0
    for row in rows:
        if _already_obligated("broker_ledger_spread", row.pk):
            skipped += 1
            continue
        try:
            obligation = generate_spread_revenue_share_obligation(row)
        except Exception:
            logger.exception(
                "[ib_commission_triggers] sweep_spread_revenue_share "
                "failed for broker_ledger=%d — skipping, next sweep will retry",
                row.pk,
            )
            skipped += 1
            continue
        if obligation is None:
            skipped += 1
        else:
            generated += 1
    result = {"scanned": len(rows), "generated": generated, "skipped": skipped}
    logger.info("[ib_commission_triggers] sweep_spread_revenue_share %s", result)
    return result
