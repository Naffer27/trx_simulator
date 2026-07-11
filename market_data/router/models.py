"""
market_data/router/models.py — route contracts and reason codes (FOUNDATION-05).

Pure data. Defined in docs/FOUNDATION_02_MARKET_DATA_CORE.md §3-4.
No network, no DB, no clock reads — every timestamp used anywhere in this
package is an explicit `now` argument supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from market_data.contracts import OrderPolicy, ProviderCapability, SourceState

# Lower number = higher precedence. priority=0 is "primary" for a symbol.
_MIN_PRIORITY = 0


class ReasonCode(str, Enum):
    """Why the router reached the decision it did."""

    PRIMARY_SELECTED = "PRIMARY_SELECTED"
    SECONDARY_SELECTED = "SECONDARY_SELECTED"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    HALF_OPEN_PROBE = "HALF_OPEN_PROBE"
    SIMULATION_FALLBACK = "SIMULATION_FALLBACK"
    NO_PROVIDER_AVAILABLE = "NO_PROVIDER_AVAILABLE"
    # Reachable only once InstrumentProfile.trading_calendar_id (FOUNDATION-02 §2.1.F)
    # exists and is fed into a plan — not wired to decide() in this block.
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass(frozen=True, kw_only=True)
class ProviderRouteEntry:
    """One candidate provider for one canonical symbol, at a declared priority."""

    provider_id: str
    canonical_symbol: str
    provider_symbol: str
    priority: int
    required_capabilities: frozenset[ProviderCapability] = frozenset()
    optional_capabilities: frozenset[ProviderCapability] = frozenset()
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
        if not self.provider_symbol:
            raise ValueError("provider_symbol must not be empty")
        if self.priority < _MIN_PRIORITY:
            raise ValueError(f"priority must be >= {_MIN_PRIORITY}, got {self.priority!r}")
        for cap in self.required_capabilities | self.optional_capabilities:
            if not isinstance(cap, ProviderCapability):
                raise ValueError(f"invalid capability: {cap!r}")


@dataclass(frozen=True, kw_only=True)
class ProviderRoutePlan:
    """
    Failover plan for one canonical symbol: candidate entries, whether
    simulation is an acceptable last resort, and the circuit breaker
    thresholds that govern this symbol's providers.
    """

    canonical_symbol: str
    entries: tuple[ProviderRouteEntry, ...] = ()
    simulation_allowed: bool
    default_order_policy_on_degradation: OrderPolicy
    max_failures: int = 3
    open_cooldown_seconds: int = 60
    half_open_successes_required: int = 2

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")

        for entry in self.entries:
            if entry.canonical_symbol != self.canonical_symbol:
                raise ValueError(
                    f"entry for provider_id={entry.provider_id!r} has canonical_symbol="
                    f"{entry.canonical_symbol!r}, expected {self.canonical_symbol!r}"
                )

        priorities = [entry.priority for entry in self.entries]
        if len(set(priorities)) != len(priorities):
            raise ValueError(f"entry priorities must be unique, got {priorities}")

        if not self.simulation_allowed and not any(entry.enabled for entry in self.entries):
            raise ValueError(
                "simulation_allowed=False requires at least one enabled entry"
            )

        if self.max_failures < 1:
            raise ValueError(f"max_failures must be >= 1, got {self.max_failures!r}")
        if self.open_cooldown_seconds <= 0:
            raise ValueError(f"open_cooldown_seconds must be > 0, got {self.open_cooldown_seconds!r}")
        if self.half_open_successes_required < 1:
            raise ValueError(
                f"half_open_successes_required must be >= 1, got {self.half_open_successes_required!r}"
            )

        # FOUNDATION-02 §3.5: real-money default must never be OPEN_NORMAL when
        # the only thing left to serve is simulation.
        if self.default_order_policy_on_degradation not in (OrderPolicy.CLOSE_ONLY, OrderPolicy.HALT_NEW_ORDERS):
            raise ValueError(
                "default_order_policy_on_degradation must be CLOSE_ONLY or HALT_NEW_ORDERS, "
                f"got {self.default_order_policy_on_degradation!r}"
            )


@dataclass(frozen=True, kw_only=True)
class RouteDecision:
    """The router's explicit output for one symbol at one point in time."""

    canonical_symbol: str
    selected_provider_id: Optional[str]
    selected_provider_symbol: Optional[str]
    source_state: SourceState
    order_policy: OrderPolicy
    degraded: bool
    reason_code: ReasonCode
    tried_providers: tuple[str, ...]
