"""
simulator/pricing_context.py — SPREAD-02, extended SPREAD-04.

Versioned, framework-light contract for the pricing context captured
alongside each real execution (open/close). Used identically by the async
WS consumer (simulator/consumers.py) and the sync Celery daemon
(simulator/tasks.py) — one shape, one assembly function, so neither path
can drift from the other.

This module does not decide any price, spread, or commission — it only
reads and packages values that are already computed elsewhere
(FeedManager ticks, BrokerSpreadConfig via spread_engine, the resolved
commercial pricing profile via commercial_pricing.py, F13's observability
store). No formula here duplicates broker_price(), commission_for(), or
commercial_pricing's resolver.

schema_version exists so a future block can change this shape without a
reader silently misinterpreting an old row — always branch on it before
trusting field names/semantics. Bumped to 2 in SPREAD-04 (added profile_id,
min_spread_pips, max_spread_pips, effective_spread_pips_pre_clamp). Bumped
to 3 in the pre-commit floor/ceiling opt-in correction (added
spread_bound_applied — explicit "none applied / within bounds / floor /
ceiling" instead of requiring a reader to infer it from comparing
pre-clamp and post-clamp values).

Convention: effective_spread_pips is the total round-trip markup ACTUALLY
applied by spread_engine.broker_price() — i.e. AFTER SPREAD-04's floor/
ceiling clamp — split in half between bid and ask (broker_price()'s own
convention, not "per side"). effective_spread_pips_pre_clamp is
base_spread_pips + account_markup_pips BEFORE that clamp; the two are
equal whenever no floor/ceiling applied.

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

SCHEMA_VERSION = 3

# spread_bound_applied values.
BOUND_NONE_CONFIGURED = None       # no min/max at all — bounds inactive or unset
BOUND_WITHIN_BOUNDS    = "within_bounds"  # min/max configured but pre-clamp already inside range
BOUND_FLOOR            = "floor"
BOUND_CEILING          = "ceiling"

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
    effective_spread_pips_pre_clamp: Optional[float]
    min_spread_pips: Optional[float]
    max_spread_pips: Optional[float]
    spread_bound_applied: Optional[str]
    profile_id: Optional[str]
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
    min_spread_pips=None,
    max_spread_pips=None,
    profile_id: Optional[str] = None,
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

    effective_spread_pips_pre_clamp = base_spread_pips + account_markup_pips.
    effective_spread_pips = that value clamped to [min_spread_pips,
    max_spread_pips] via spread_engine.compute_effective_spread_pips() —
    the SAME function broker_price() itself calls, so the value recorded
    here can never disagree with the spread actually applied to the fill.

    Never raises: any failure degrades to a minimal, clearly-marked
    dict rather than propagating into an open/close/daemon cycle.
    """
    try:
        base = _safe_float(base_spread_pips)
        markup = _safe_float(account_markup_pips)
        min_pips = _safe_float(min_spread_pips)
        max_pips = _safe_float(max_spread_pips)

        pre_clamp: Optional[float] = None
        post_clamp: Optional[float] = None
        if base is not None or markup is not None:
            from .spread_engine import compute_effective_spread_pips
            pre_clamp, post_clamp = compute_effective_spread_pips(
                base or 0.0, markup or 0.0, min_pips, max_pips,
            )

        bound_applied: Optional[str] = None
        if min_pips is not None or max_pips is not None:
            if pre_clamp is not None and post_clamp is not None and pre_clamp != post_clamp:
                bound_applied = BOUND_FLOOR if (min_pips is not None and post_clamp == min_pips) else BOUND_CEILING
            else:
                bound_applied = BOUND_WITHIN_BOUNDS

        ctx = PricingContext(
            schema_version=SCHEMA_VERSION,
            raw_bid=_safe_float(raw_bid),
            raw_ask=_safe_float(raw_ask),
            executable_bid=_safe_float(executable_bid),
            executable_ask=_safe_float(executable_ask),
            base_spread_pips=base,
            account_markup_pips=markup,
            effective_spread_pips=post_clamp,
            effective_spread_pips_pre_clamp=pre_clamp,
            min_spread_pips=min_pips,
            max_spread_pips=max_pips,
            spread_bound_applied=bound_applied,
            profile_id=profile_id,
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
    already use for BrokerLedger.REV_SPREAD — same async-safe cache, no new
    DB load) and pairs it with the given markup. Does not apply or change
    either value — read-only capture.

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


def tick_pricing_snapshot(symbol: str, profile) -> dict:
    """
    SPREAD-02b/SPREAD-04 — the ONE place allowed to read BrokerSpreadConfig/
    F13 observability for pricing-context purposes. Must be called
    immediately adjacent to broker_price() inside price_tick(), never
    later at order time: BrokerSpreadConfig and the observability store
    can both change between a tick and the order that eventually uses it,
    and only the values active AT THE TICK actually produced that tick's
    executable_bid/executable_ask. Re-reading at order time would silently
    mislabel an already-executed price with data that never produced it.

    profile is the ALREADY-RESOLVED commercial_pricing.CommercialPricingProfile
    for (account, symbol) — resolved once by the caller (price_tick() needs
    it anyway, to pass markup/min/max into broker_price()'s clamp), never
    re-resolved here. This function only adds base_spread_pips (symbol-level,
    not part of the commercial profile — read straight from
    BrokerSpreadConfig) and F13 observability, both DB-free reads.

    Returns {"base_spread_pips", "account_markup_pips", "profile_id",
    "min_spread_pips", "max_spread_pips", "provider_id", "source_state"} —
    the caller stores this dict verbatim per symbol and
    _capture_pricing_context() reads it back later, never re-querying.
    Never raises.
    """
    try:
        base, _ = spread_pips_for(symbol, None)
        provider_id, source_state = provider_state_for(symbol)
        return {
            "base_spread_pips": base,
            "account_markup_pips": profile.spread_markup_pips,
            "profile_id": profile.profile_id,
            "min_spread_pips": profile.min_spread_pips,
            "max_spread_pips": profile.max_spread_pips,
            "provider_id": provider_id,
            "source_state": source_state,
        }
    except Exception as exc:
        logger.debug("[pricing_context] tick_pricing_snapshot failed for %s (non-fatal): %r", symbol, exc)
        return {}


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
