"""
market_data/observability — FOUNDATION-13: Market Data Observability.

Read-only operational layer over the existing Market Data Engine: which
provider is active per symbol, circuit breaker state, failovers, market
session, last-tick freshness, live/simulation mode, catalog drift, and
recent errors. This package never selects a provider, never evaluates
risk, never changes a price/spread/margin/PnL rule, and never writes a
tick payload — it only reads existing state and records small,
low-frequency timestamps/counters of its own. See service.py and
store.py module docstrings for the full design rationale.

Not wired into any live decision path — market_data/feeds.py calls into
this package's store at existing integration points purely to record
state for later reading; nothing here feeds back into provider selection,
order policy, or the feed loop's control flow.
"""

from .models import CatalogDriftLevel, CircuitBreakerView, MarketDataHealthSnapshot
from .service import build_snapshot
from .store import (
    get_symbol_state,
    record_first_tick,
    record_selection,
    record_session_state,
    record_terminal_failure,
    record_tick,
    reset_observability_state,
)

__all__ = [
    "MarketDataHealthSnapshot",
    "CircuitBreakerView",
    "CatalogDriftLevel",
    "build_snapshot",
    "record_tick",
    "record_selection",
    "record_first_tick",
    "record_terminal_failure",
    "record_session_state",
    "get_symbol_state",
    "reset_observability_state",
]
