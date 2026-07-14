"""
simulator/spread_engine.py — Phase 3A, made async-safe in SPREAD-03 FASE A.
Broker spread engine: applies per-symbol price markup to raw market bid/ask.

Architecture note (SPREAD-03): _get_config() is now a PURE, DB-free read
from simulator.spread_config_cache — the process-wide, async-safe cache.
It performs zero ORM access itself, so broker_price() is safe to call
directly from TradingConsumer.price_tick() (an `async def` method) — the
old per-call lazy DB read raised SynchronousOnlyOperation from that
context and silently returned None every time (see
simulator/spread_config_cache.py's module docstring for the full
diagnosis, and simulator/tests/test_spread_config_cache.py for the tests
proving both the old bug and the fix).
"""
import logging
from typing import Optional

from market_data.symbol_specs import normalize_symbol

logger = logging.getLogger("simulator.spread")


def compute_effective_spread_pips(
    base_pips: float,
    markup_pips: float,
    min_spread_pips: Optional[float] = None,
    max_spread_pips: Optional[float] = None,
    *,
    is_dynamic: bool = False,
    dynamic_inputs: Optional["DynamicSpreadInputs"] = None,
) -> tuple[float, float]:
    """
    SPREAD-04, extended SPREAD-05. Pure. Returns (effective_before_bounds,
    effective_after_bounds).

    Static path (is_dynamic=False, or dynamic_inputs not supplied — the
    default for every existing caller): unchanged from SPREAD-04.
    effective_before_bounds = base_pips + markup_pips, clamped to
    [min_spread_pips, max_spread_pips] — either bound being None means no
    floor / no ceiling on that side.

    Dynamic path (is_dynamic=True AND dynamic_inputs given): delegates to
    simulator.dynamic_spread.evaluate_dynamic_spread(dynamic_inputs) — a
    pure function of the already-frozen inputs object, so base_pips/
    markup_pips/min_spread_pips/max_spread_pips (the positional args) are
    ignored in this branch; dynamic_inputs is self-sufficient (it was
    built from the same BrokerSpreadConfig/profile values). Returns
    (effective_before_bounds, effective_after_bounds) from the resulting
    DynamicSpreadDecision — "before_bounds" here means after the
    session/source/stale/volatility/liquidity/manual multiplier chain but
    before the floor/ceiling clamp, generalizing "before_clamp"'s meaning
    for the static path (where the two coincide, since is_dynamic=False
    multiplies by 1.0 six times — an exact no-op).

    The single function both broker_price() and
    pricing_context.tick_pricing_snapshot() call, so the spread actually
    applied to a fill and the audit record captured for it can never
    disagree.
    """
    if is_dynamic and dynamic_inputs is not None:
        from .dynamic_spread import evaluate_dynamic_spread
        decision = evaluate_dynamic_spread(dynamic_inputs)
        return decision.effective_before_bounds, decision.effective_after_bounds

    before = base_pips + markup_pips
    after = before
    if min_spread_pips is not None and after < min_spread_pips:
        after = min_spread_pips
    if max_spread_pips is not None and after > max_spread_pips:
        after = max_spread_pips
    return before, after


def _get_config(symbol: str):
    """
    Return the cached BrokerSpreadConfig snapshot for symbol (enabled=True)
    or None. Normalizes symbol to canonical form so 'EURUSD' and 'EUR/USD'
    resolve to the same cache entry. Zero DB access — see
    simulator/spread_config_cache.py for how and when the cache is
    populated (never here, never per-call). Never raises.
    """
    from .spread_config_cache import get_cached_config
    return get_cached_config(normalize_symbol(symbol))


def broker_price(symbol: str, bid: float, ask: float,
                 markup_pips: float = 0.0,
                 min_spread_override: Optional[float] = None,
                 max_spread_override: Optional[float] = None,
                 dynamic_inputs: Optional["DynamicSpreadInputs"] = None) -> tuple[float, float]:
    """
    Apply broker spread to raw market bid/ask.
    Returns (client_bid, client_ask).

    effective_pips = clamp(BrokerSpreadConfig.spread_pips + markup_pips,
                            min_spread_pips, max_spread_pips)   (SPREAD-04)
    Widens the spread symmetrically:
      client_bid = bid − (effective_pips × pip_size / 2)
      client_ask = ask + (effective_pips × pip_size / 2)

    markup_pips is the per-account additive spread — SPREAD-04's
    commercial pricing resolver (simulator/commercial_pricing.py) is the
    canonical source; callers with no resolved profile pass 0.0 and see
    pure BrokerSpreadConfig behaviour, unchanged from before SPREAD-04.

    min_spread_override/max_spread_override let a caller supply an
    already-resolved floor/ceiling (e.g. an account/product-level override
    combined with the symbol's own BrokerSpreadConfig — see
    commercial_pricing.build_commercial_pricing_profile()) so the value
    actually clamped here is identical to what gets recorded in the
    pricing-context audit trail, never a second, independently-timed read.
    When omitted (None), falls back to BrokerSpreadConfig's own
    min_spread/max_spread for *symbol* — either still being unset/None
    means no floor/no ceiling, same as before this block.

    dynamic_inputs (SPREAD-05) — an already-built
    simulator.dynamic_spread.DynamicSpreadInputs for this exact tick (see
    dynamic_spread.build_dynamic_inputs(), called once by
    consumers.py::price_tick()). Only takes effect when the symbol's
    BrokerSpreadConfig.is_dynamic is True; omitted or ignored otherwise —
    every caller that predates SPREAD-05 (and every test that doesn't pass
    it) keeps the exact SPREAD-04 static formula, unchanged.

    Falls through unchanged if effective_pips <= 0 or on any error.
    """
    try:
        symbol = normalize_symbol(symbol)
        cfg = _get_config(symbol)
        base_pips = float(cfg.spread_pips) if cfg is not None else 0.0
        min_pips = min_spread_override if min_spread_override is not None else (
            cfg.min_spread if cfg is not None else None
        )
        max_pips = max_spread_override if max_spread_override is not None else (
            cfg.max_spread if cfg is not None else None
        )
        is_dynamic = bool(cfg.is_dynamic) if cfg is not None else False
        _, effective_pips = compute_effective_spread_pips(
            base_pips, markup_pips, min_pips, max_pips,
            is_dynamic=is_dynamic, dynamic_inputs=dynamic_inputs,
        )
        if effective_pips <= 0.0:
            return bid, ask
        from market_data.symbol_specs import get_spec
        spec  = get_spec(symbol)
        extra = effective_pips * spec.pip_size / 2
        return (
            round(bid - extra, spec.price_decimals),
            round(ask + extra, spec.price_decimals),
        )
    except Exception as exc:
        logger.warning("[spread] broker_price failed for %s: %s", symbol, exc)
        return bid, ask


def calculate_spread_revenue(symbol: str, qty: float, spread_pips: float) -> float:
    """
    Revenue earned by the broker on one side of the spread per execution.
    = (spread_pips × pip_size / 2) × qty × contract_size

    Hooked on position OPEN only — the open-side half-spread is the broker's capture point.
    Returns 0.0 on any error (non-fatal).
    """
    try:
        from market_data.symbol_specs import get_spec
        spec        = get_spec(normalize_symbol(symbol))
        half_spread = float(spread_pips) * spec.pip_size / 2
        return round(half_spread * qty * spec.contract_size, 8)
    except Exception as exc:
        logger.warning("[spread] calculate_spread_revenue failed for %s: %s", symbol, exc)
        return 0.0
