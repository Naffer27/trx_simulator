"""
market_data/runtime_router/state.py — persistent circuit breaker state
(FOUNDATION-10).

Owns the single, process-wide ProviderRouter instance used to decide and
record real provider outcomes for allowlisted symbols. This replaces
FOUNDATION-09's documented limitation: select_runtime_provider() used to
create a fresh ProviderRouter() on every call, so it never carried real
connection success/failure from FeedManager's loops — every decision
reflected a healthy/CLOSED circuit breaker. Now there is one shared
instance per process, and market_data/feeds.py feeds it real outcomes via
record_provider_success()/record_provider_failure().

Known limitation (documented, not solved here — see
docs/MARKET_DATA_ARCHITECTURE.md §10): this state lives in one Python
process's memory. A multi-worker ASGI deployment (multiple Daphne
processes) would have one independent circuit breaker per worker, not a
shared one, per FOUNDATION-02 §3.4. Sharing this state (Redis, or a
dedicated market-data service) is future work.

Every public function here is safe to call from inside a live price feed
loop: none of them raise. A bug in feedback recording must never affect
the real feed it's observing.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.instruments.routing import build_route_plan
from market_data.router.breaker import CircuitBreakerState
from market_data.router.models import ProviderRoutePlan, ReasonCode
from market_data.router.router import ProviderRouter
from market_data.symbol_specs import get_spec

log = logging.getLogger("simulator.ws")

_router: Optional[ProviderRouter] = None
_last_selected_provider: dict[str, Optional[str]] = {}


def get_router() -> ProviderRouter:
    """The process-wide ProviderRouter singleton. Lazily created."""
    global _router
    if _router is None:
        _router = ProviderRouter()
    return _router


def reset_router_state() -> None:
    """Replace the singleton with a fresh instance and clear failover
    tracking. Test-only — a real running process never needs this; its
    circuit breaker state is meant to persist for the process lifetime."""
    global _router, _last_selected_provider
    _router = ProviderRouter()
    _last_selected_provider = {}


def get_circuit_breaker_state(symbol: str, provider_id: str) -> CircuitBreakerState:
    return get_router().get_breaker_state(provider_id, symbol)


def build_plan_for_symbol(symbol: str) -> ProviderRoutePlan:
    spec = get_spec(symbol)
    profile = profile_from_symbol_spec(spec)
    return build_route_plan(profile)


def record_provider_success(symbol: str, provider_id: str, *, now: Optional[int] = None) -> None:
    """Call once per feed session, on the first valid tick — never per tick,
    never just for a successful socket connect."""
    evaluated_at = now if now is not None else int(time.time())
    try:
        plan = build_plan_for_symbol(symbol)
        router = get_router()
        before = router.get_breaker_state(provider_id, symbol)
        after = router.provider_success(provider_id, plan, now=evaluated_at)
        _log_transition(symbol, provider_id, before, after, reason="tick_success")
    except Exception as exc:
        log.debug(
            "[router-state] record_provider_success failed for %s/%s (non-fatal): %r",
            symbol, provider_id, exc,
        )


def record_provider_failure(
    symbol: str, provider_id: str, *, error_code: Optional[str] = None, now: Optional[int] = None,
) -> None:
    """Call once when a loop terminates/raises for a real error — never for
    asyncio.CancelledError, never per internal reconnect attempt."""
    evaluated_at = now if now is not None else int(time.time())
    try:
        plan = build_plan_for_symbol(symbol)
        router = get_router()
        before = router.get_breaker_state(provider_id, symbol)
        after = router.provider_failure(provider_id, plan, now=evaluated_at, error_code=error_code)
        _log_transition(symbol, provider_id, before, after, reason=error_code or "terminal_failure")
    except Exception as exc:
        log.debug(
            "[router-state] record_provider_failure failed for %s/%s (non-fatal): %r",
            symbol, provider_id, exc,
        )


def evaluate_recovery(symbol: str, provider_id: str, *, now: Optional[int] = None) -> Optional[CircuitBreakerState]:
    """Exposed for direct testing/inspection. decide() (called by
    select_runtime_provider) already triggers this internally for every
    candidate it evaluates — feeds.py does not need to call this itself in
    the normal integration path."""
    evaluated_at = now if now is not None else int(time.time())
    try:
        plan = build_plan_for_symbol(symbol)
        router = get_router()
        before = router.get_breaker_state(provider_id, symbol)
        after = router.evaluate_recovery(provider_id, plan, now=evaluated_at)
        _log_transition(symbol, provider_id, before, after, reason="cooldown_elapsed")
        return after
    except Exception as exc:
        log.debug(
            "[router-state] evaluate_recovery failed for %s/%s (non-fatal): %r",
            symbol, provider_id, exc,
        )
        return None


def record_selection(symbol: str, provider_id: Optional[str], reason_code: Optional[ReasonCode]) -> None:
    """Logs event=market_data_router_failover when the selected provider for
    a symbol changes between two consecutive decisions. Never raises."""
    try:
        previous = _last_selected_provider.get(symbol)
        _last_selected_provider[symbol] = provider_id
        if previous is not None and previous != provider_id:
            log.info(
                "event=market_data_router_failover symbol=%s from_provider=%s to_provider=%s reason_code=%s",
                symbol, previous, provider_id, reason_code.value if reason_code else None,
            )
    except Exception as exc:
        log.debug("[router-state] record_selection failed for %s (non-fatal): %r", symbol, exc)


def _log_transition(
    symbol: str, provider_id: str, before: CircuitBreakerState, after: CircuitBreakerState, *, reason: str,
) -> None:
    if before.health == after.health:
        return
    log.info(
        "event=market_data_router_state_transition symbol=%s provider=%s from_state=%s to_state=%s "
        "reason=%s consecutive_failures=%s error_code=%s",
        symbol, provider_id, before.health.value, after.health.value, reason,
        after.consecutive_failures, after.last_error_code,
    )
