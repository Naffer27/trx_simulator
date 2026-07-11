"""
market_data/providers/registry.py — Provider Capability Registry (FOUNDATION-04).

Static, declarative catalog of what each provider adapter can objectively
do (FOUNDATION-02 §4). Populated by each adapter module registering its own
profile at import time (mirrors the _REGISTRY pattern already used in
market_data/symbol_specs.py).

Not wired to a ProviderRouter — that component is designed in FOUNDATION-02
but does not exist as code yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_data.contracts import ProviderCapability

_VALID_TRANSPORTS = frozenset({"REST", "WEBSOCKET"})


@dataclass(frozen=True)
class ProviderCapabilityProfile:
    """What one provider can objectively do, as data — not as a claim in code comments."""

    provider_id: str
    capabilities: frozenset[ProviderCapability]
    asset_classes: frozenset[str]
    transports: frozenset[str]

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")
        if not self.capabilities:
            raise ValueError(f"{self.provider_id}: must declare at least one capability")
        if not self.asset_classes:
            raise ValueError(f"{self.provider_id}: must declare at least one asset class")
        if not self.transports:
            raise ValueError(f"{self.provider_id}: must declare at least one transport")
        unknown = self.transports - _VALID_TRANSPORTS
        if unknown:
            raise ValueError(f"{self.provider_id}: unknown transports {sorted(unknown)}")


_REGISTRY: dict[str, ProviderCapabilityProfile] = {}


def register_profile(profile: ProviderCapabilityProfile) -> None:
    _REGISTRY[profile.provider_id] = profile


def get_profile(provider_id: str) -> ProviderCapabilityProfile:
    try:
        return _REGISTRY[provider_id]
    except KeyError:
        raise KeyError(f"No capability profile registered for provider_id={provider_id!r}") from None


def supports(provider_id: str, capability: ProviderCapability) -> bool:
    return capability in get_profile(provider_id).capabilities


def all_profiles() -> tuple[ProviderCapabilityProfile, ...]:
    return tuple(_REGISTRY.values())
