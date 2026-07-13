"""
simulator/pricing_context.py — SPREAD-02.

Versioned, framework-light contract for the pricing context captured
alongside each real execution (open/close). Used identically by the async
WS consumer (simulator/consumers.py) and the sync Celery daemon
(simulator/tasks.py) — one shape, one assembly function, so neither path
can drift from the other.

This module does not decide any price, spread, or commission — it only
reads and packages values that are already computed elsewhere
(FeedManager ticks, BrokerSpreadConfig, account snapshot, F13's
observability store). No formula here duplicates broker_price() or
commission_for(); spread_pips_for() reads the exact same
BrokerSpreadConfig row and account markup those already read, it does not
recompute anything new.

schema_version exists so a future block can change this shape without a
reader silently misinterpreting an old row — always branch on it before
trusting field names/semantics.

Convention (SPREAD-02): effective_spread_pips is the total round-trip
markup used by spread_engine.broker_price() (base_spread_pips +
account_markup_pips), split in half between bid and ask — the same
convention broker_price() itself uses. It is NOT "per side".

Never raises. Every public function here degrades to a safe, documented
default instead of propagating — pricing-context capture must never block
an open, a close, or a daemon cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger("simulator.pricing")

SCHEMA_VERSION = 1

# Execution-trigger labels — one per production close/open path this block
# covers. Kept as plain strings (not an enum) so daemon "reason" values
# (already used verbatim in tasks.py logging/WS payloads) can be reused
# without a translation layer.
PROFILE_WS_OPEN         = "ws_manual_open"
PROFILE_WS_CLOSE        = "ws_manual_close"
PROFILE_WS_TP           = "ws_tp"
PROFILE_WS_SL           = "ws_sl"
PROFILE_WS_STOPOUT      = "ws_stopout"
PROFILE_WS_MARGIN_CALL  = "ws_margin_call"
PROFILE_DAEMON_STOPOUT     = "daemon_stopout"
PROFILE_DAEMON_MARGIN_CALL = "daemon_margin_call"
PROFILE_DAEMON_TP          = "daemon_tp"
PROFILE_DAEMON_SL          = "daemon_sl"
PROFILE_CAPTURE_FAILED  = "capture_failed"


@dataclass(frozen=True)
class PricingContext:
    schema_version: int
    raw_bid: Optional[float]
    raw_ask: Optional[float]
    executable_bid: Optional[float]
    executable_ask: Optional[float]
    base_spread_pips: Optional[float]
    account_markup_pips: Optional[float]
    effective_spread_pips: Optional[float]
    provider_id: Optional[str]
    source_state: Optional[str]
    router_provider: Optional[str]
    pricing_timestamp: Optional[float]
    pricing_profile: str

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_pricing_context(
    *,
    raw_bid=None,
    raw_ask=None,
    executable_bid=None,
    executable_ask=None,
    base_spread_pips=None,
    account_markup_pips=None,
    provider_id: Optional[str] = None,
    source_state: Optional[str] = None,
    router_provider: Optional[str] = None,
    pricing_timestamp=None,
    pricing_profile: str,
) -> dict:
    """
    Assembles the versioned pricing-context dict. This is the single place
    that knows the shape — every caller uses this instead of hand-building
    the dict, so the schema can never drift between call sites.

    effective_spread_pips = base_spread_pips + account_markup_pips (the
    same total broker_price() applies) — computed here, not re-derived
    from prices, so it stays correct even when one of the two inputs is
    unavailable (None + 1.5 = 1.5, not None).

    Never raises: any failure degrades to a minimal, clearly-marked
    dict rather than propagating into an open/close/daemon cycle.
    """
    try:
        base = _safe_float(base_spread_pips)
        markup = _safe_float(account_markup_pips)
        effective: Optional[float] = None
        if base is not None or markup is not None:
            effective = (base or 0.0) + (markup or 0.0)

        ctx = PricingContext(
            schema_version=SCHEMA_VERSION,
            raw_bid=_safe_float(raw_bid),
            raw_ask=_safe_float(raw_ask),
            executable_bid=_safe_float(executable_bid),
            executable_ask=_safe_float(executable_ask),
            base_spread_pips=base,
            account_markup_pips=markup,
            effective_spread_pips=effective,
            provider_id=provider_id,
            source_state=source_state,
            router_provider=router_provider,
            pricing_timestamp=(
                _safe_float(pricing_timestamp) if pricing_timestamp is not None else time.time()
            ),
            pricing_profile=pricing_profile,
        )
        return ctx.to_dict()
    except Exception as exc:
        logger.debug("[pricing_context] build failed for profile=%s (non-fatal): %r", pricing_profile, exc)
        return {"schema_version": SCHEMA_VERSION, "pricing_profile": PROFILE_CAPTURE_FAILED}


def spread_pips_for(symbol: str, account_markup_pips) -> tuple[Optional[float], Optional[float]]:
    """
    Reads BrokerSpreadConfig.spread_pips for *symbol* (the exact same
    cached accessor spread_engine.broker_price()/_db_open_position_atomic
    already use for BrokerLedger.REV_SPREAD — same 30s TTL cache, no new
    DB load) and pairs it with the account's own markup. Does not apply
    or change either value — read-only capture.

    Returns (base_spread_pips, account_markup_pips) as floats/None.
    Never raises.
    """
    try:
        from .spread_engine import _get_config as _get_spread_config
        from market_data.symbol_specs import normalize_symbol

        cfg = _get_spread_config(normalize_symbol(symbol))
        base = float(cfg.spread_pips) if (cfg is not None and cfg.enabled) else None
        markup = _safe_float(account_markup_pips)
        return base, markup
    except Exception as exc:
        logger.debug("[pricing_context] spread_pips_for failed for %s (non-fatal): %r", symbol, exc)
        return None, _safe_float(account_markup_pips)


def tick_pricing_snapshot(symbol: str, account_markup_pips) -> dict:
    """
    SPREAD-02b — the ONE place allowed to read BrokerSpreadConfig/F13
    observability for pricing-context purposes. Must be called immediately
    adjacent to broker_price() inside price_tick(), never later at order
    time: both BrokerSpreadConfig and the observability store can change
    between a tick and the order that eventually uses it, and only the
    values active AT THE TICK actually produced that tick's
    executable_bid/executable_ask. Re-reading either at order time would
    silently mislabel an already-executed price with a config that never
    produced it.

    Returns {"base_spread_pips", "account_markup_pips", "provider_id",
    "source_state"} — the caller stores this dict verbatim per symbol and
    _capture_pricing_context() reads it back later, never re-querying.
    Composed entirely from spread_pips_for()/provider_state_for(), both
    already never-raising — this function cannot raise either.
    """
    base, markup = spread_pips_for(symbol, account_markup_pips)
    provider_id, source_state = provider_state_for(symbol)
    return {
        "base_spread_pips": base,
        "account_markup_pips": markup,
        "provider_id": provider_id,
        "source_state": source_state,
    }


def provider_state_for(symbol: str) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort read of market_data.observability's per-process store
    (FOUNDATION-13) — never mutates it, never triggers a new evaluation.

    Populated only when MARKET_DATA_OBSERVABILITY_ENABLED=True and the
    symbol is under the router (MARKET_DATA_ROUTER_ENABLED +
    MARKET_DATA_ROUTER_SYMBOLS); (None, None) otherwise — that is expected
    for legacy symbols, not a failure.

    Returns (provider_id, source_state_value). Never raises.
    """
    try:
        from market_data.observability import get_symbol_state

        state = get_symbol_state(symbol)
        provider_id = state.active_provider_id
        source_state = state.source_state.value if state.source_state is not None else None
        return provider_id, source_state
    except Exception as exc:
        logger.debug("[pricing_context] provider_state_for failed for %s (non-fatal): %r", symbol, exc)
        return None, None
