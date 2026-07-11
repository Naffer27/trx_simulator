"""
market_data/providers/mappings.py — ProviderSymbolMapping (FOUNDATION-04).

Pure data contract: which canonical symbol maps to which raw symbol on a
given provider, and what that provider must support to serve it.

No DB, no migrations. Deliberately not connected to simulator.models.Instrument
or market_data/symbol_specs.py in this block.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_data.contracts import ProviderCapability


@dataclass(frozen=True)
class ProviderSymbolMapping:
    """One (canonical_symbol, provider) routing entry."""

    canonical_symbol: str
    provider_id: str
    provider_symbol: str
    enabled: bool = True
    priority: int = 0
    required_capabilities: frozenset[ProviderCapability] = frozenset()
    optional_capabilities: frozenset[ProviderCapability] = frozenset()

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")
        if not self.provider_symbol:
            raise ValueError("provider_symbol must not be empty")
