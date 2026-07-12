"""
market_data/observability/models.py — FOUNDATION-13: Market Data
Observability contracts.

Pure data. MarketDataHealthSnapshot never carries a price, a spread, a
secret, or a raw tick payload — only state, timestamps, and counters.
Every field is either read from an existing component (circuit breaker,
market session evaluator, router selection outcome) or derived from a
timestamp recorded at an existing low-frequency integration point — this
package is not a second source of truth for anything market_data/* or
simulator/* already knows how to compute. See service.py and store.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from market_data.contracts import OrderPolicy, ProviderHealthState, SourceState
from market_data.sessions import CalendarId, MarketSessionState


class CatalogDriftLevel(str, Enum):
    """Mirrors the possible outcomes of simulator.runtime_instrument_catalog
    .check_runtime_catalog_drift() without importing it — that function is
    DB/Django-aware (FOUNDATION-12) and lives outside this package on
    purpose, same as every other market_data/* leaf. The caller maps its
    result into one of these before calling build_snapshot()."""

    NOT_CHECKED = "NOT_CHECKED"    # MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED is False
    NO_DATA = "NO_DATA"            # flag on, no matching DB row to compare against
    MATCH = "MATCH"                # flag on, compared, zero differences
    WARNING = "WARNING"            # flag on, only non-critical differences
    CRITICAL = "CRITICAL"          # flag on, at least one critical difference
    UNAVAILABLE = "UNAVAILABLE"    # flag on, but the check itself failed


@dataclass(frozen=True, kw_only=True)
class CircuitBreakerView:
    """Read-only projection of market_data.router.breaker.CircuitBreakerState
    for one provider. Built by reading the existing runtime_router
    singleton — never mutates it (no provider_success/provider_failure/
    evaluate_recovery call happens while building this)."""

    provider_id: str
    health: ProviderHealthState
    consecutive_failures: int
    opened_at: Optional[int] = None
    last_failure_at: Optional[int] = None
    last_error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")


@dataclass(frozen=True, kw_only=True)
class MarketDataHealthSnapshot:
    """One observability read of one canonical symbol's market-data state,
    at one point in time (evaluated_at). Immutable — a fresh snapshot must
    be built for each read, never mutated in place."""

    canonical_symbol: str

    active_provider_id: Optional[str] = None
    active_provider_symbol: Optional[str] = None

    router_enabled: bool = False
    router_allowlisted: bool = False

    source_state: Optional[SourceState] = None
    order_policy: Optional[OrderPolicy] = None

    market_session_state: Optional[MarketSessionState] = None
    market_calendar_id: Optional[CalendarId] = None

    circuit_states: tuple[CircuitBreakerView, ...] = ()

    last_tick_at: Optional[float] = None
    tick_age_seconds: Optional[float] = None
    stale: bool = False
    synthetic: bool = False
    degraded: bool = False

    last_failover_at: Optional[float] = None
    failover_count: int = 0

    catalog_drift_level: CatalogDriftLevel = CatalogDriftLevel.NOT_CHECKED

    error_code: Optional[str] = None
    evaluated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
        if self.failover_count < 0:
            raise ValueError("failover_count must be >= 0")
