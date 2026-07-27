# simulator/broker_risk_shadow.py
"""
BOOK-06d — Shadow Exposure Consumer.

calculate_shadow_broker_exposure() is a fully isolated, read-only,
observational calculator. Option B (approved 2026-07-27): it does NOT
touch broker_exposure.py or broker_risk.py in any way — the official
aggregation formula (broker_exposure.calculate_broker_exposure()) stays
completely frozen for the entire shadow phase. This module talks
directly to the same underlying primitives broker_exposure.py itself
uses (market_data.feeds/market_data.symbol_specs), never to
broker_exposure.py's own internals — zero coupling, zero import of
anything from that module.

Deliberately duplicates only the minimal slice of the official formula
needed here — gross notional (qty * price * contract_size, summed
across BUY/SELL) — never the full BrokerExposureBreakdown (margin, PnL,
concentration, per-symbol/per-account detail are not recomputed; this
comparison does not need them). Accepted duplication cost (approved
2026-07-27, in favor of leaving the official calculator completely
untouched during the shadow-only phase): if broker_exposure.py's own
formula ever changes, this module must be updated in lockstep or the
comparison will silently drift. Resolving that risk (e.g. by
consolidating to a single shared formula, Option A) is explicitly
deferred until BOOK-06e has demonstrated both calculators agree and
business has approved a real policy — never done silently before then.

Two queries only, no N+1:
  1. Position queryset (same select_related shape as broker_exposure.py)
  2. One batch DealingDeskDecision lookup by position_id__in

Never writes anything — DealingDeskDecision, Position, TradingAccount
are all read-only from this module's perspective. Never raises: the
entire calculation runs inside a single try/except; any failure returns
a comparison equal to "no shadow adjustment" (all-zero dataclass —
gross_exposure_actual == gross_exposure_shadow == 0), same fail-open
discipline as every other engine in this project. This function is
never called by broker_risk.py's own validate_new_order() or by any
order-accept/reject path — no such call site exists yet, and adding one
is explicitly out of scope for BOOK-06d.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

log = logging.getLogger("simulator.broker_risk_shadow")

_ZERO = Decimal("0")


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


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


def _price_lookup_for_symbols(symbols: set) -> dict:
    """Same discipline as broker_exposure.py's own helper (never
    duplicated by import — reimplemented against the same primitive,
    market_data.feeds, to keep this module fully isolated): a symbol
    absent from the returned dict means "no fresh price", never
    substituted with anything."""
    from market_data.feeds import get_feed_manager
    fm = get_feed_manager()
    prices = {}
    for sym in symbols:
        if fm.has_price(sym):
            prices[sym] = _d(fm.last_price(sym))
    return prices


def _spec_cache_for_symbols(symbols: set) -> dict:
    from market_data.symbol_specs import get_spec
    cache = {}
    for sym in symbols:
        try:
            cache[sym] = get_spec(sym)
        except KeyError:
            cache[sym] = None
    return cache


def _gross_notional(positions, prices: dict, specs: dict) -> Decimal:
    """qty * price * contract_size, summed across BUY/SELL — unsigned,
    same definition as BrokerExposureBreakdown.gross_notional. Positions
    on a symbol with no fresh price are skipped entirely (never
    estimated), same fail-closed-on-missing-price discipline already
    established in broker_exposure.py."""
    total = _ZERO
    for pos in positions:
        price = prices.get(pos.symbol)
        if price is None:
            continue
        spec = specs.get(pos.symbol)
        contract_size = _d(spec.contract_size) if spec is not None else Decimal("1")
        total += _d(pos.qty) * price * contract_size
    return total


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
    Pure, isolated, read-only. Same filter contract as
    broker_exposure.calculate_broker_exposure() (account_id/account_ids/
    symbol/account_type/status/trader_class), so a caller can compare
    at whole-book or scoped granularity without inventing a second
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
        from .models import DealingDeskDecision, Position

        qs = Position.objects.select_related("account").all()
        if account_id is not None:
            qs = qs.filter(account_id=account_id)
        if account_ids is not None:
            qs = qs.filter(account_id__in=account_ids)
        if symbol is not None:
            qs = qs.filter(symbol=symbol)
        if account_type is not None:
            qs = qs.filter(account__account_type=account_type)
        if status is not None:
            qs = qs.filter(account__status=status)
        if trader_class is not None:
            qs = qs.filter(account__trader_score__trader_class=trader_class)

        positions = list(qs)
        total_position_count = len(positions)

        symbols = {p.symbol for p in positions}
        prices = _price_lookup_for_symbols(symbols)
        specs = _spec_cache_for_symbols(symbols)

        position_ids = [p.id for p in positions]
        excluded_ids = set(
            DealingDeskDecision.objects
            .filter(position_id__in=position_ids, is_simulated_hedge=True)
            .values_list("position_id", flat=True)
        ) if position_ids else set()

        shadow_positions = [p for p in positions if p.id not in excluded_ids]

        gross_actual = _gross_notional(positions, prices, specs)
        gross_shadow = _gross_notional(shadow_positions, prices, specs)
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
            excluded_position_count=len(excluded_ids),
            total_position_count=total_position_count,
            generated_at=timezone.now(),
        )
    except Exception as exc:
        log.error(
            "[broker_risk_shadow] FAILED to compute shadow exposure: %r", exc, exc_info=True,
        )
        return ShadowExposureComparison()
