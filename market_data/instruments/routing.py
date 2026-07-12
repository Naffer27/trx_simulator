"""
market_data/instruments/routing.py — InstrumentProfile -> ProviderRoutePlan
builder (FOUNDATION-07).

Pure. No network, no DB, no clock reads, no Django dependency. Reuses the
FOUNDATION-04/05/06 contracts as-is — nothing here redefines them.

Not wired into the runtime: nothing in feeds.py, consumers.py, risk_engine.py,
spread_engine.py, exposure_engine.py, or tasks.py imports this module.
market_data/symbol_specs.py remains the runtime source of truth.

Build-time validation is deliberately stricter than ProviderRouter.decide():
decide() must always return *some* decision (it runs continuously against
live state, degrading gracefully). build_route_plan() runs once, ahead of
time, against configuration — an unknown provider or an unsupported
capability there is a configuration bug, not a live degradation, so it
raises instead of silently producing a smaller plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_data.providers.registry import get_profile
from market_data.router.models import ProviderRouteEntry, ProviderRoutePlan

from .profiles import InstrumentProfile


@dataclass(frozen=True, kw_only=True)
class RouterPolicyConfig:
    """Circuit breaker thresholds for one ProviderRoutePlan. See
    market_data/router/models.py::ProviderRoutePlan for what each field governs."""

    max_failures: int
    open_cooldown_seconds: int
    half_open_successes_required: int

    def __post_init__(self) -> None:
        if self.max_failures < 1:
            raise ValueError(f"max_failures must be >= 1, got {self.max_failures!r}")
        if self.open_cooldown_seconds <= 0:
            raise ValueError(f"open_cooldown_seconds must be > 0, got {self.open_cooldown_seconds!r}")
        if self.half_open_successes_required < 1:
            raise ValueError(
                f"half_open_successes_required must be >= 1, got {self.half_open_successes_required!r}"
            )


# ─── Defaults by asset_class — single declarative table, no scattered hardcodes ──
#
# Rationale for the cooldowns: crypto feeds (Binance/Kraken WS) reconnect
# fast and cheaply, so a shorter recovery probe interval is reasonable;
# forex/metal lean on Finnhub (rate-limited, slower to trust again); energy/
# index have no real provider integrated yet (MD-6 roadmap) so the most
# conservative cooldown applies. half_open_successes_required is uniform
# (2) — nothing in this block's scope calls for varying it by asset class.

_DEFAULT_POLICY_BY_ASSET_CLASS: dict[str, RouterPolicyConfig] = {
    "crypto": RouterPolicyConfig(max_failures=3, open_cooldown_seconds=10, half_open_successes_required=2),
    "forex": RouterPolicyConfig(max_failures=3, open_cooldown_seconds=15, half_open_successes_required=2),
    "metal": RouterPolicyConfig(max_failures=3, open_cooldown_seconds=15, half_open_successes_required=2),
    "energy": RouterPolicyConfig(max_failures=3, open_cooldown_seconds=20, half_open_successes_required=2),
    "index": RouterPolicyConfig(max_failures=3, open_cooldown_seconds=20, half_open_successes_required=2),
}

_FALLBACK_POLICY = RouterPolicyConfig(max_failures=3, open_cooldown_seconds=20, half_open_successes_required=2)


def default_policy_for_asset_class(asset_class: str) -> RouterPolicyConfig:
    """The declared default for a known asset_class, or the conservative
    fallback for anything not yet in the table."""
    return _DEFAULT_POLICY_BY_ASSET_CLASS.get(asset_class, _FALLBACK_POLICY)


# ─── Builder ────────────────────────────────────────────────────────────────


def build_route_plan(profile: InstrumentProfile, *, policy: RouterPolicyConfig | None = None) -> ProviderRoutePlan:
    """
    Convert an InstrumentProfile's provider_mappings into a ProviderRoutePlan
    ProviderRouter.decide() can consume.

    - Disabled mappings are dropped, not carried over as disabled entries —
      a profile-level "this data source doesn't apply" is a different
      concept from the router's own per-entry enabled flag.
    - Priority is re-densified (0, 1, 2, ...) among the kept mappings, in
      their original relative order, so filtering never leaves a gap that
      would violate ProviderRoutePlan's "priorities unique" rule.
    - Every kept mapping's provider_id must exist in the Provider Capability
      Registry and support everything the mapping requires, or this raises —
      see module docstring for why that's stricter than decide().
    """
    resolved_policy = policy if policy is not None else default_policy_for_asset_class(profile.asset_class)

    enabled_mappings = sorted(
        (m for m in profile.provider_mappings if m.enabled),
        key=lambda m: m.priority,
    )

    entries: list[ProviderRouteEntry] = []
    for dense_priority, mapping in enumerate(enabled_mappings):
        try:
            capability_profile = get_profile(mapping.provider_id)
        except KeyError:
            raise ValueError(
                f"{profile.canonical_symbol}: provider_id={mapping.provider_id!r} is not "
                f"registered in the Provider Capability Registry"
            ) from None

        missing = mapping.required_capabilities - capability_profile.capabilities
        if missing:
            raise ValueError(
                f"{profile.canonical_symbol}: provider_id={mapping.provider_id!r} does not "
                f"support required capabilities {sorted(c.value for c in missing)}"
            )

        entries.append(ProviderRouteEntry(
            provider_id=mapping.provider_id,
            canonical_symbol=profile.canonical_symbol,
            provider_symbol=mapping.provider_symbol,
            priority=dense_priority,
            required_capabilities=mapping.required_capabilities,
            optional_capabilities=mapping.optional_capabilities,
            enabled=True,
        ))

    # No explicit "entries empty + simulation_allowed=False -> raise" check
    # here: ProviderRoutePlan's own constructor already enforces exactly
    # that rule (FOUNDATION-05) — re-checking it here would just duplicate
    # the single source of truth for that invariant.
    return ProviderRoutePlan(
        canonical_symbol=profile.canonical_symbol,
        entries=tuple(entries),
        simulation_allowed=profile.simulation_allowed,
        default_order_policy_on_degradation=profile.default_order_policy_on_degradation,
        max_failures=resolved_policy.max_failures,
        open_cooldown_seconds=resolved_policy.open_cooldown_seconds,
        half_open_successes_required=resolved_policy.half_open_successes_required,
    )
