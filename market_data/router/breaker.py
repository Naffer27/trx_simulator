"""
market_data/router/breaker.py — circuit breaker state and transitions (FOUNDATION-05).

State is immutable; every transition function takes a state and returns a
new one. Nothing here reads a clock or touches the network — `now` is
always an explicit argument, so every transition is deterministic and
testable without sleeping.

Scope is one (provider_id, canonical_symbol) pair per CircuitBreakerState —
isolation across pairs is the caller's responsibility (ProviderRouter keys
its store by that pair; see router.py).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

from market_data.contracts import ProviderHealthState

from .models import ProviderRoutePlan


@dataclass(frozen=True, kw_only=True)
class CircuitBreakerState:
    provider_id: str
    canonical_symbol: str
    health: ProviderHealthState = ProviderHealthState.CLOSED
    consecutive_failures: int = 0
    opened_at: Optional[int] = None
    last_failure_at: Optional[int] = None
    half_open_successes: int = 0
    last_error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be >= 0")
        if self.half_open_successes < 0:
            raise ValueError("half_open_successes must be >= 0")


def record_success(state: CircuitBreakerState, *, plan: ProviderRoutePlan, now: int) -> CircuitBreakerState:
    """CLOSED success resets the failure count. HALF_OPEN success counts toward
    closing; enough of them (half_open_successes_required) closes the breaker.
    A success reported while OPEN is a caller anomaly (the router never routes
    traffic to an OPEN provider) — left as a no-op rather than silently closing."""
    if state.health == ProviderHealthState.HALF_OPEN:
        successes = state.half_open_successes + 1
        if successes >= plan.half_open_successes_required:
            return dataclasses.replace(
                state, health=ProviderHealthState.CLOSED, consecutive_failures=0,
                opened_at=None, half_open_successes=0, last_error_code=None,
            )
        return dataclasses.replace(state, half_open_successes=successes)

    if state.health == ProviderHealthState.OPEN:
        return state

    return dataclasses.replace(state, consecutive_failures=0, last_error_code=None)


def record_failure(
    state: CircuitBreakerState, *, plan: ProviderRoutePlan, now: int, error_code: Optional[str] = None,
) -> CircuitBreakerState:
    """CLOSED failures accumulate until max_failures trips the breaker open.
    Any failure during HALF_OPEN immediately reopens it — a single bad trial
    is enough, per FOUNDATION-02 §3.2 (don't trust a flaky recovery)."""
    if state.health == ProviderHealthState.HALF_OPEN:
        return dataclasses.replace(
            state, health=ProviderHealthState.OPEN, opened_at=now, last_failure_at=now,
            half_open_successes=0, consecutive_failures=state.consecutive_failures + 1,
            last_error_code=error_code,
        )

    failures = state.consecutive_failures + 1

    if state.health == ProviderHealthState.OPEN:
        return dataclasses.replace(state, consecutive_failures=failures, last_failure_at=now, last_error_code=error_code)

    if failures >= plan.max_failures:
        return dataclasses.replace(
            state, health=ProviderHealthState.OPEN, consecutive_failures=failures,
            opened_at=now, last_failure_at=now, last_error_code=error_code,
        )

    return dataclasses.replace(state, consecutive_failures=failures, last_failure_at=now, last_error_code=error_code)


def maybe_transition_to_half_open(
    state: CircuitBreakerState, *, plan: ProviderRoutePlan, now: int,
) -> CircuitBreakerState:
    """OPEN -> HALF_OPEN once open_cooldown_seconds has elapsed since opened_at."""
    if (
        state.health == ProviderHealthState.OPEN
        and state.opened_at is not None
        and (now - state.opened_at) >= plan.open_cooldown_seconds
    ):
        return dataclasses.replace(state, health=ProviderHealthState.HALF_OPEN, half_open_successes=0)
    return state
