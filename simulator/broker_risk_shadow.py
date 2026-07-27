# simulator/broker_risk_shadow.py
"""
BOOK-06d — Shadow Exposure Consumer.
BOOK-06g — duplication retired (2026-07-27).

calculate_shadow_broker_exposure() is a read-only, observational
calculator. It delegates entirely to broker_exposure.calculate_broker_
exposure() — the single official aggregation formula (RISK-01) — for
BOTH the actual and the shadow number. It never re-derives gross
notional itself.

History: BOOK-06d (approved 2026-07-27) originally chose Option B — a
fully isolated module duplicating just the gross-notional slice of the
official formula, precisely so the shadow-only observation phase could
proceed without touching broker_exposure.py or broker_risk.py at all.
That duplication was an explicitly accepted, temporary cost: "if
broker_exposure.py's own formula ever changes, this module must be
updated in lockstep or the comparison will silently drift" (BOOK-06d's
own module docstring, now retired). BOOK-06g (approved 2026-07-27) adds
calculate_broker_exposure()'s own exclude_position_ids parameter
(Option A) precisely to close that risk — this module now consumes it
instead of carrying a second formula.

Never writes anything — DealingDeskDecision, Position, TradingAccount
are all read-only from this module's perspective. Never raises: the
entire calculation runs inside a single try/except; any failure returns
a comparison equal to "no shadow adjustment" (all-zero dataclass —
gross_exposure_actual == gross_exposure_shadow == 0), same fail-open
discipline as every other engine in this project. This function is
never called by broker_risk.py's own validate_new_order() or by any
order-accept/reject path — no such call site exists yet (that is a
separate, still-unauthorized future block).
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

log = logging.getLogger("simulator.broker_risk_shadow")

_ZERO = Decimal("0")


@dataclass
class ShadowExposureComparison:
    gross_exposure_actual: Decimal = _ZERO
    gross_exposure_shadow: Decimal = _ZERO
    simulated_hedge_excluded_exposure: Decimal = _ZERO
    absolute_difference: Decimal = _ZERO
    percentage_difference: Decimal = _ZERO
    excluded_position_count: int = 0
    total_position_count: int = 0
    generated_at: "datetime | None" = None


def calculate_shadow_broker_exposure(
    *,
    account_id=None,
    account_ids=None,
    symbol=None,
    account_type=None,
    status=None,
    trader_class=None,
) -> ShadowExposureComparison:
    """
    Pure, read-only. Same filter contract as broker_exposure.
    calculate_broker_exposure() (account_id/account_ids/symbol/
    account_type/status/trader_class), so a caller can compare at
    whole-book or scoped granularity without inventing a second
    filtering scheme.

    Positions whose DealingDeskDecision (if any) has is_simulated_hedge
    True are excluded from gross_exposure_shadow only — never from
    gross_exposure_actual, which always reflects every open position,
    identical to today's real broker_exposure_snapshot(). A position
    with no DealingDeskDecision at all counts exactly like one with
    is_simulated_hedge=False (BOOK-06d design, approved 2026-07-27) —
    missing data never removes a position from the shadow count either.

    Returns a ShadowExposureComparison — never a model instance, never
    persisted. On any failure, returns the all-zero default (shadow ==
    actual == 0), logged and swallowed.
    """
    try:
        from . import broker_exposure as _exposure
        from .models import DealingDeskDecision

        excluded_ids = frozenset(
            DealingDeskDecision.objects
            .filter(is_simulated_hedge=True)
            .values_list("position_id", flat=True)
        )

        filters = dict(
            account_id=account_id, account_ids=account_ids, symbol=symbol,
            account_type=account_type, status=status, trader_class=trader_class,
        )

        # BOOK-06g (approved 2026-07-27) — deliberate design decision:
        # calculate_broker_exposure() is called TWICE here — once with
        # no exclusion (the official number, gross_exposure_actual) and
        # once with exclude_position_ids (the shadow number,
        # gross_exposure_shadow) — precisely so both numbers are
        # produced by the exact same formula, the same query shape, the
        # same pricing/spec lookups, the same rounding. This is what
        # closes the drift risk BOOK-06d's own Option B accepted
        # temporarily: two independently-written aggregations can only
        # diverge by definition; one formula called twice cannot.
        #
        # This costs one extra top-level query relative to BOOK-06d's
        # original two-query design (now three: one DealingDeskDecision
        # batch lookup + two Position aggregations) — still zero N+1,
        # no query per position in either call. ANY future optimization
        # that reduces this back to a single aggregation call (e.g. by
        # computing the delta from a single position list instead of
        # two full calculate_broker_exposure() calls) MUST preserve the
        # guarantee that gross_exposure_actual and gross_exposure_shadow
        # are always derived from the identical formula before removing
        # either of these two calls — never reintroduce a second,
        # independently-maintained aggregation, even a partial one.
        actual = _exposure.calculate_broker_exposure(**filters)
        shadow = _exposure.calculate_broker_exposure(
            **filters, exclude_position_ids=excluded_ids,
        )

        gross_actual = actual.gross_notional
        gross_shadow = shadow.gross_notional
        excluded_exposure = gross_actual - gross_shadow
        absolute_difference = abs(excluded_exposure)
        percentage_difference = (
            (absolute_difference / gross_actual * Decimal("100")).quantize(Decimal("0.01"))
            if gross_actual != 0 else Decimal("0.00")
        )

        return ShadowExposureComparison(
            gross_exposure_actual=gross_actual,
            gross_exposure_shadow=gross_shadow,
            simulated_hedge_excluded_exposure=excluded_exposure,
            absolute_difference=absolute_difference,
            percentage_difference=percentage_difference,
            # Derived from the two aggregation results themselves, not
            # from len(excluded_ids) — excluded_ids is computed globally
            # (never scoped to `filters`), so counting it directly would
            # over-count whenever a symbol/account/etc. filter is
            # applied. The difference between the two open_position_count
            # values is always correct within whatever scope `filters`
            # already applied to both calls identically.
            excluded_position_count=actual.open_position_count - shadow.open_position_count,
            total_position_count=actual.open_position_count,
            generated_at=timezone.now(),
        )
    except Exception as exc:
        log.error(
            "[broker_risk_shadow] FAILED to compute shadow exposure: %r", exc, exc_info=True,
        )
        return ShadowExposureComparison()
