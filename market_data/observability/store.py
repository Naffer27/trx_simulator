"""
market_data/observability/store.py — FOUNDATION-13 per-process event store.

Mirrors market_data.runtime_router.state's established pattern: a
process-wide, symbol-keyed dict of mutable state, a lazy accessor, and a
reset_*() for test isolation. Records only low-frequency events (provider
selection, first tick, terminal failure, failover, tick timestamps) — no
prices, no bids/asks, no raw tick payloads, no user data. A tick updates
only a float timestamp here, never the tick's own values.

Known limitation (same as FOUNDATION-10's circuit breaker state and
FOUNDATION-02 §3.4): this lives in one Python process's memory. A
multi-worker ASGI deployment has one independent store per worker, not a
shared one. management commands run in their own separate process and
never see a running ASGI worker's live store — see
simulator/management/commands/market_data_status.py for how that's
surfaced to the operator instead of silently showing empty state.

Every public function here is safe to call from inside a live feed loop:
none of them raise. A bug in observability recording must never affect
the feed it's observing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from market_data.contracts import OrderPolicy, SourceState
from market_data.router.models import ReasonCode
from market_data.sessions import MarketSessionState

log = logging.getLogger("simulator.ws")

_DEGRADED_SOURCE_STATES = frozenset({
    SourceState.SECONDARY, SourceState.RECOVERY, SourceState.SIMULATION, SourceState.STALE,
})


@dataclass
class _SymbolObservabilityState:
    active_provider_id: Optional[str] = None
    active_provider_symbol: Optional[str] = None
    source_state: Optional[SourceState] = None
    order_policy: Optional[OrderPolicy] = None
    degraded: bool = False
    reason_code: Optional[ReasonCode] = None

    last_tick_at: Optional[float] = None

    failover_count: int = 0
    last_failover_at: Optional[float] = None

    last_error_code: Optional[str] = None

    last_session_state: Optional[MarketSessionState] = None


_symbol_states: dict[str, _SymbolObservabilityState] = {}


def get_symbol_state(symbol: str) -> _SymbolObservabilityState:
    """Read-only-by-convention accessor for service.py — callers must not
    mutate the returned object; use the record_*() functions below."""
    return _symbol_states.setdefault(symbol, _SymbolObservabilityState())


def reset_observability_state() -> None:
    """Test-only — a real running process never needs this."""
    global _symbol_states
    _symbol_states = {}


def record_tick(symbol: str, *, now: Optional[float] = None) -> None:
    """Called from FeedManager._broadcast() on every tick — real or
    simulated. Updates only a timestamp, never the tick's bid/ask/mid.
    Never raises."""
    try:
        evaluated_at = now if now is not None else time.time()
        _symbol_states.setdefault(symbol, _SymbolObservabilityState()).last_tick_at = evaluated_at
    except Exception as exc:
        log.debug("[observability] record_tick failed for %s (non-fatal): %r", symbol, exc)


def record_selection(
    symbol: str,
    *,
    provider_id: Optional[str],
    provider_symbol: Optional[str],
    source_state: Optional[SourceState],
    order_policy: Optional[OrderPolicy],
    degraded: bool,
    reason_code: Optional[ReasonCode],
    now: Optional[float] = None,
) -> None:
    """Called once per router decision (market_data.feeds._try_live_via_new_router,
    after select_runtime_provider()) — covers a real provider being picked,
    no provider being available, and simulation fallback uniformly (all are
    valid RuntimeSelectionResult outcomes). Detects a provider change since
    the last recorded selection for this symbol and counts it as a
    failover. Never raises."""
    try:
        evaluated_at = now if now is not None else time.time()
        state = _symbol_states.setdefault(symbol, _SymbolObservabilityState())

        previous_provider = state.active_provider_id
        if previous_provider is not None and previous_provider != provider_id:
            state.failover_count += 1
            state.last_failover_at = evaluated_at
            log.info(
                "event=market_data_observability_failover symbol=%s from_provider=%s "
                "to_provider=%s failover_count=%d",
                symbol, previous_provider, provider_id, state.failover_count,
            )

        state.active_provider_id = provider_id
        state.active_provider_symbol = provider_symbol
        state.source_state = source_state
        state.order_policy = order_policy
        state.degraded = degraded or (source_state in _DEGRADED_SOURCE_STATES)
        state.reason_code = reason_code
    except Exception as exc:
        log.debug("[observability] record_selection failed for %s (non-fatal): %r", symbol, exc)


def record_first_tick(symbol: str, provider_id: str, *, now: Optional[float] = None) -> None:
    """Called once per feed session from on_first_tick — logs the event;
    tick freshness itself is tracked by record_tick() via _broadcast().
    Never raises."""
    try:
        log.info("event=market_data_observability_first_tick symbol=%s provider=%s", symbol, provider_id)
    except Exception as exc:
        log.debug("[observability] record_first_tick failed for %s (non-fatal): %r", symbol, exc)


def record_terminal_failure(
    symbol: str, provider_id: str, *, error_code: Optional[str] = None, now: Optional[float] = None,
) -> None:
    """Called once when a dispatched loop gives up for a real error (never
    for CancelledError) from on_terminal_failure. Never raises."""
    try:
        _symbol_states.setdefault(symbol, _SymbolObservabilityState()).last_error_code = error_code
        log.info(
            "event=market_data_observability_terminal_failure symbol=%s provider=%s error_code=%s",
            symbol, provider_id, error_code,
        )
    except Exception as exc:
        log.debug("[observability] record_terminal_failure failed for %s (non-fatal): %r", symbol, exc)


def record_session_state(symbol: str, state_value: MarketSessionState, *, now: Optional[float] = None) -> None:
    """Called from the existing session-evaluation hook in
    market_data.feeds._try_live_via_new_router(). Logs a transition only
    (open<->closed etc) — does not recompute or store calendar rules; the
    canonical, always-fresh session read for a snapshot is
    evaluate_market_session_for_symbol(), called directly by service.py.
    Never raises."""
    try:
        state = _symbol_states.setdefault(symbol, _SymbolObservabilityState())
        previous = state.last_session_state
        state.last_session_state = state_value
        if previous is not None and previous != state_value:
            log.info(
                "event=market_data_observability_session_transition symbol=%s from_state=%s to_state=%s",
                symbol, previous.value, state_value.value,
            )
    except Exception as exc:
        log.debug("[observability] record_session_state failed for %s (non-fatal): %r", symbol, exc)
