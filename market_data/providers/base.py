"""
market_data/providers/base.py — MarketDataProviderAdapter contract (FOUNDATION-04).

Dumb by design (FOUNDATION-02 principle §0.1): an adapter speaks one
provider's wire format and knows nothing about failover, retries, or
circuit breakers — that is ProviderRouter's job, not built in this block.

No network access anywhere in this class: every method here operates on
data already in hand (a raw payload dict the caller passed in) — nothing
is fetched, subscribed, opened, or awaited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from market_data.contracts import (
    CURRENT_SCHEMA_VERSION,
    NormalizedTick,
    ProviderCapability,
    SourceState,
)

from .mappings import ProviderSymbolMapping


class MarketDataProviderAdapter(ABC):
    """
    Structural contract every provider adapter must satisfy.

    provider_id / capabilities are declared by each concrete subclass as
    class attributes (not abstract properties — kept simple, per
    FOUNDATION-04 scope: three known, hand-written adapters, no dynamic
    plugin loading to guard against here).
    """

    provider_id: str
    capabilities: frozenset[ProviderCapability]

    # ── shared across every adapter — not overridden ──

    def validate_symbol_mapping(self, mapping: ProviderSymbolMapping) -> None:
        """Is this mapping usable by THIS adapter (not: is the mapping well-formed —
        ProviderSymbolMapping already guarantees that on construction)."""
        if mapping.provider_id != self.provider_id:
            raise ValueError(
                f"mapping.provider_id={mapping.provider_id!r} does not match "
                f"adapter provider_id={self.provider_id!r}"
            )
        missing = mapping.required_capabilities - self.capabilities
        if missing:
            raise ValueError(
                f"{self.provider_id} adapter lacks required capabilities: "
                f"{sorted(c.value for c in missing)}"
            )

    def normalize_tick(
        self,
        raw_message: Any,
        mapping: ProviderSymbolMapping,
        *,
        timestamp_received: int,
    ) -> NormalizedTick:
        """
        Parse + validate + build the final NormalizedTick.

        Shared assembly logic; parse_message() is where each adapter's
        provider-specific wire shape is handled. timestamp_received is
        supplied by the caller (not read from a clock here) so this stays
        a pure function of its inputs.
        """
        self.validate_symbol_mapping(mapping)
        parsed = self.parse_message(raw_message)
        return NormalizedTick(
            schema_version=CURRENT_SCHEMA_VERSION,
            symbol=mapping.canonical_symbol,
            provider_id=self.provider_id,
            source_state=SourceState.LIVE,
            is_synthetic=False,
            is_stale=False,
            timestamp_received=timestamp_received,
            bid=parsed.get("bid"),
            ask=parsed.get("ask"),
            last=parsed.get("last"),
            timestamp_provider=parsed.get("timestamp_provider"),
            sequence=parsed.get("sequence"),
        )

    def probe(self) -> bool:
        """
        Conceptual health signal only. No network access in this block —
        always True. Real probing is later work, once an adapter actually
        opens a connection (ProviderRouter / MD-4 territory).
        """
        return True

    # ── provider-specific — implemented by each concrete adapter ──

    @abstractmethod
    def build_subscription_request(self, mapping: ProviderSymbolMapping) -> dict[str, Any]:
        """Conceptual only — describes what a subscribe call would send. Sends nothing."""
        ...

    @abstractmethod
    def parse_message(self, raw_message: Any) -> dict[str, Any]:
        """
        Extract this provider's wire shape into a small intermediate dict
        with whichever of these keys the message actually carries:
        "bid", "ask", "last", "timestamp_provider", "sequence".

        Raises ValueError with a clear message on a malformed or
        incomplete payload — never returns a partially-parsed result.
        """
        ...
