"""
market_data/observability/service.py — FOUNDATION-13.

build_snapshot() assembles a MarketDataHealthSnapshot for one canonical
symbol, reusing exactly the sources of truth that already exist:

  - circuit breaker state: market_data.runtime_router.state, read-only
    (get_circuit_breaker_state / build_plan_for_symbol already exist —
    FOUNDATION-10). Nothing here calls provider_success/provider_failure/
    evaluate_recovery, so reading a snapshot never mutates a breaker.
  - market session: recomputed fresh via
    market_data.sessions.evaluate_market_session_for_symbol() — pure,
    deterministic, and the only way to get a correct answer regardless of
    which process (or which management-command invocation) asks. It is
    NOT read from this package's own store — see store.py's per-process
    limitation docstring for why a stored value would be misleading
    across processes.
  - tick freshness / active selection / failover count: this package's
    own per-process store (store.py), fed by market_data/feeds.py at
    existing low-frequency integration points. Never a new source of live
    prices — only timestamps and small counters.

Everything Django/DB-aware (settings flags, catalog drift) is computed by
the caller and passed in as plain values/enums — this module stays a pure
leaf, importing no django and no simulator, exactly like every other
market_data/* package.

build_snapshot() never raises — any internal failure degrades to a
minimal snapshot with error_code set, same boundary-service pattern as
select_runtime_provider() / evaluate_market_session_for_symbol() /
check_runtime_catalog_drift().
"""

from __future__ import annotations

import time
from typing import Optional

from market_data.contracts import SourceState
from market_data.runtime_router.state import build_plan_for_symbol, get_circuit_breaker_state
from market_data.sessions import evaluate_market_session_for_symbol

from .models import CatalogDriftLevel, CircuitBreakerView, MarketDataHealthSnapshot
from .store import get_symbol_state


def build_snapshot(
    symbol: str,
    *,
    router_enabled: bool = False,
    router_allowlisted: bool = False,
    catalog_drift_level: CatalogDriftLevel = CatalogDriftLevel.NOT_CHECKED,
    stale_after_seconds: float = 60.0,
    now: Optional[float] = None,
) -> MarketDataHealthSnapshot:
    evaluated_at = now if now is not None else time.time()
    try:
        return _build_snapshot_inner(
            symbol,
            router_enabled=router_enabled,
            router_allowlisted=router_allowlisted,
            catalog_drift_level=catalog_drift_level,
            stale_after_seconds=stale_after_seconds,
            evaluated_at=evaluated_at,
        )
    except Exception as exc:
        return MarketDataHealthSnapshot(
            canonical_symbol=symbol,
            router_enabled=router_enabled,
            router_allowlisted=router_allowlisted,
            catalog_drift_level=catalog_drift_level,
            error_code=f"snapshot_build_failed: {exc!r}",
            evaluated_at=evaluated_at,
        )


def _build_snapshot_inner(
    symbol: str,
    *,
    router_enabled: bool,
    router_allowlisted: bool,
    catalog_drift_level: CatalogDriftLevel,
    stale_after_seconds: float,
    evaluated_at: float,
) -> MarketDataHealthSnapshot:
    recorded = get_symbol_state(symbol)
    circuit_states = _read_circuit_states(symbol)
    session = evaluate_market_session_for_symbol(symbol)

    tick_age_seconds: Optional[float] = None
    stale = False
    if recorded.last_tick_at is not None:
        tick_age_seconds = max(0.0, evaluated_at - recorded.last_tick_at)
        stale = tick_age_seconds > stale_after_seconds

    synthetic = recorded.source_state == SourceState.SIMULATION

    return MarketDataHealthSnapshot(
        canonical_symbol=symbol,
        active_provider_id=recorded.active_provider_id,
        active_provider_symbol=recorded.active_provider_symbol,
        router_enabled=router_enabled,
        router_allowlisted=router_allowlisted,
        source_state=recorded.source_state,
        order_policy=recorded.order_policy,
        market_session_state=session.state,
        market_calendar_id=session.calendar_id,
        circuit_states=circuit_states,
        last_tick_at=recorded.last_tick_at,
        tick_age_seconds=tick_age_seconds,
        stale=stale,
        synthetic=synthetic,
        degraded=recorded.degraded,
        last_failover_at=recorded.last_failover_at,
        failover_count=recorded.failover_count,
        catalog_drift_level=catalog_drift_level,
        error_code=recorded.last_error_code,
        evaluated_at=evaluated_at,
    )


def _read_circuit_states(symbol: str) -> tuple[CircuitBreakerView, ...]:
    """Read-only: uses the same get_circuit_breaker_state() accessor
    record_provider_success/failure already use to capture a "before"
    snapshot for logging — it lazily creates a default CLOSED entry for an
    unseen (provider, symbol) pair but never transitions health."""
    try:
        plan = build_plan_for_symbol(symbol)
    except Exception:
        return ()

    views: list[CircuitBreakerView] = []
    for entry in sorted(plan.entries, key=lambda e: e.priority):
        try:
            breaker = get_circuit_breaker_state(symbol, entry.provider_id)
        except Exception:
            continue
        views.append(CircuitBreakerView(
            provider_id=entry.provider_id,
            health=breaker.health,
            consecutive_failures=breaker.consecutive_failures,
            opened_at=breaker.opened_at,
            last_failure_at=breaker.last_failure_at,
            last_error_code=breaker.last_error_code,
        ))
    return tuple(views)
