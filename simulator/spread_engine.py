"""
simulator/spread_engine.py — Phase 3A
Broker spread engine: applies per-symbol price markup to raw market bid/ask.

Architecture note:
  _get_config() caches DB lookups (TTL=30s) so hot-path tick processing
  (potentially 1 call per second per connected consumer) is not DB-bound.
  A sub-millisecond sync DB call occurs at most once per 30 s per symbol.
"""
import time as _t
import logging

from market_data.symbol_specs import normalize_symbol

logger = logging.getLogger("simulator.spread")

# Module-level TTL cache: canonical_symbol -> (BrokerSpreadConfig | None, monotonic_timestamp)
_cache: dict = {}
_CACHE_TTL = 30.0  # seconds


def _get_config(symbol: str):
    """
    Return BrokerSpreadConfig for symbol (enabled=True) or None.
    Normalizes symbol to canonical form before lookup so 'EURUSD' and
    'EUR/USD' resolve to the same cache entry and DB row.
    Cached for _CACHE_TTL seconds. Never raises.
    """
    symbol = normalize_symbol(symbol)
    now = _t.monotonic()
    entry = _cache.get(symbol)
    if entry and (now - entry[1]) < _CACHE_TTL:
        return entry[0]
    try:
        from .models import BrokerSpreadConfig
        cfg = BrokerSpreadConfig.objects.filter(symbol=symbol, enabled=True).first()
    except Exception as exc:
        logger.debug("[spread] config lookup failed for %s: %s", symbol, exc)
        cfg = None
    _cache[symbol] = (cfg, now)
    return cfg


def broker_price(symbol: str, bid: float, ask: float) -> tuple[float, float]:
    """
    Apply broker spread markup to raw market bid/ask.
    Returns (client_bid, client_ask).

    Widens the spread symmetrically:
      client_bid = bid − (spread_pips × pip_size / 2)
      client_ask = ask + (spread_pips × pip_size / 2)

    Falls through unchanged if no config exists or on any error.
    Normalizes symbol so 'EURUSD' and 'EUR/USD' are treated identically.
    """
    try:
        symbol = normalize_symbol(symbol)
        cfg = _get_config(symbol)
        if cfg is None:
            return bid, ask
        from market_data.symbol_specs import get_spec
        spec  = get_spec(symbol)
        extra = float(cfg.spread_pips) * spec.pip_size / 2
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
