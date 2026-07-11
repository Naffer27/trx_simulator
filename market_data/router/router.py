"""
market_data/router/router.py — ProviderRouter (FOUNDATION-05).

Decides which provider (if any) serves a symbol right now, given a
ProviderRoutePlan and the current circuit breaker state. No network, no
DB, no clock reads: every call takes an explicit `now`, and provider
health comes from market_data.providers.registry (a static, in-memory
catalog — see FOUNDATION-04) plus this router's own in-memory circuit
breaker store.

The circuit breaker store (self._breaker_states) is the one piece of
mutable state in this package, scoped to one ProviderRouter instance —
per FOUNDATION-02 §3.4, sharing this state across multiple processes is a
later concern (Redis or a dedicated service), out of scope here.
"""

from __future__ import annotations

from market_data.contracts import OrderPolicy, ProviderHealthState, SourceState
from market_data.providers.registry import get_profile

from .breaker import CircuitBreakerState, maybe_transition_to_half_open, record_failure, record_success
from .models import ProviderRoutePlan, ReasonCode, RouteDecision


class ProviderRouter:
    def __init__(self) -> None:
        self._breaker_states: dict[tuple[str, str], CircuitBreakerState] = {}

    # ── circuit breaker state access ──

    def get_breaker_state(self, provider_id: str, canonical_symbol: str) -> CircuitBreakerState:
        key = (provider_id, canonical_symbol)
        if key not in self._breaker_states:
            self._breaker_states[key] = CircuitBreakerState(
                provider_id=provider_id, canonical_symbol=canonical_symbol,
            )
        return self._breaker_states[key]

    # ── recovery / outcome reporting (pure w.r.t. the outside world — no I/O) ──

    def provider_success(self, provider_id: str, plan: ProviderRoutePlan, *, now: int) -> CircuitBreakerState:
        state = self.get_breaker_state(provider_id, plan.canonical_symbol)
        new_state = record_success(state, plan=plan, now=now)
        self._breaker_states[(provider_id, plan.canonical_symbol)] = new_state
        return new_state

    def provider_failure(
        self, provider_id: str, plan: ProviderRoutePlan, *, now: int, error_code: str | None = None,
    ) -> CircuitBreakerState:
        state = self.get_breaker_state(provider_id, plan.canonical_symbol)
        new_state = record_failure(state, plan=plan, now=now, error_code=error_code)
        self._breaker_states[(provider_id, plan.canonical_symbol)] = new_state
        return new_state

    def evaluate_recovery(self, provider_id: str, plan: ProviderRoutePlan, *, now: int) -> CircuitBreakerState:
        state = self.get_breaker_state(provider_id, plan.canonical_symbol)
        new_state = maybe_transition_to_half_open(state, plan=plan, now=now)
        self._breaker_states[(provider_id, plan.canonical_symbol)] = new_state
        return new_state

    # ── decision ──

    def decide(self, plan: ProviderRoutePlan, *, now: int) -> RouteDecision:
        sorted_entries = sorted(plan.entries, key=lambda e: e.priority)
        tried: list[str] = []
        skip_reasons: list[ReasonCode] = []

        for index, entry in enumerate(sorted_entries):
            tried.append(entry.provider_id)

            if not entry.enabled:
                skip_reasons.append(ReasonCode.PROVIDER_DISABLED)
                continue

            try:
                profile = get_profile(entry.provider_id)
            except KeyError:
                # Unknown to the Capability Registry — cannot trust it, same
                # outcome as declaring capabilities it doesn't actually have.
                skip_reasons.append(ReasonCode.CAPABILITY_MISMATCH)
                continue

            if entry.required_capabilities - profile.capabilities:
                skip_reasons.append(ReasonCode.CAPABILITY_MISMATCH)
                continue

            self.evaluate_recovery(entry.provider_id, plan, now=now)
            state = self.get_breaker_state(entry.provider_id, plan.canonical_symbol)

            if state.health == ProviderHealthState.OPEN:
                skip_reasons.append(ReasonCode.CIRCUIT_OPEN)
                continue

            if state.health == ProviderHealthState.HALF_OPEN:
                # Real-money-safe by default (FOUNDATION-02 §0.8): a trial
                # provider is not yet trusted with new positions.
                return RouteDecision(
                    canonical_symbol=plan.canonical_symbol,
                    selected_provider_id=entry.provider_id,
                    selected_provider_symbol=entry.provider_symbol,
                    source_state=SourceState.RECOVERY,
                    order_policy=OrderPolicy.CLOSE_ONLY,
                    degraded=True,
                    reason_code=ReasonCode.HALF_OPEN_PROBE,
                    tried_providers=tuple(tried),
                )

            is_primary = index == 0
            return RouteDecision(
                canonical_symbol=plan.canonical_symbol,
                selected_provider_id=entry.provider_id,
                selected_provider_symbol=entry.provider_symbol,
                source_state=SourceState.LIVE if is_primary else SourceState.SECONDARY,
                order_policy=OrderPolicy.OPEN_NORMAL,
                degraded=not is_primary,
                reason_code=ReasonCode.PRIMARY_SELECTED if is_primary else ReasonCode.SECONDARY_SELECTED,
                tried_providers=tuple(tried),
            )

        # No entry was viable.
        uniform_reason = skip_reasons[0] if skip_reasons and len(set(skip_reasons)) == 1 else None

        if plan.simulation_allowed:
            return RouteDecision(
                canonical_symbol=plan.canonical_symbol,
                selected_provider_id=None,
                selected_provider_symbol=None,
                source_state=SourceState.SIMULATION,
                order_policy=plan.default_order_policy_on_degradation,
                degraded=True,
                reason_code=ReasonCode.SIMULATION_FALLBACK,
                tried_providers=tuple(tried),
            )

        return RouteDecision(
            canonical_symbol=plan.canonical_symbol,
            selected_provider_id=None,
            selected_provider_symbol=None,
            source_state=SourceState.STALE,
            order_policy=OrderPolicy.HALT_NEW_ORDERS,
            degraded=True,
            reason_code=uniform_reason or ReasonCode.NO_PROVIDER_AVAILABLE,
            tried_providers=tuple(tried),
        )
