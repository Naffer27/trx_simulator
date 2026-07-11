"""
market_data/router — FOUNDATION-05: Provider Router Foundation.

Pure provider selection/failover logic built on top of the FOUNDATION-03
contracts and the FOUNDATION-04 Provider Capability Registry. No network,
no DB, no clock reads (every timestamp is an explicit `now`), no sleeps,
no async loops.

Not wired into the runtime: market_data/feeds.py::FeedManager still owns
the real "Binance -> Kraken -> Finnhub" failover, hardcoded in
_try_live(). Nothing in this codebase imports market_data.router yet.
"""

from .breaker import CircuitBreakerState, maybe_transition_to_half_open, record_failure, record_success
from .models import ProviderRouteEntry, ProviderRoutePlan, ReasonCode, RouteDecision
from .router import ProviderRouter

__all__ = [
    "CircuitBreakerState",
    "record_success",
    "record_failure",
    "maybe_transition_to_half_open",
    "ProviderRouteEntry",
    "ProviderRoutePlan",
    "RouteDecision",
    "ReasonCode",
    "ProviderRouter",
]
