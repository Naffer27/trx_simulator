"""
market_data/providers — FOUNDATION-04: Provider Adapter Foundation.

Pure, network-free provider adapters that turn a provider's raw payload
(already in hand — nothing here opens a connection) into a
market_data.contracts.NormalizedTick (FOUNDATION-03).

Not wired into the runtime: market_data/feeds.py::FeedManager still owns
every real WebSocket/REST connection and does not import this package.
No caller in the codebase imports market_data.providers yet.

Relationship to market_data/adapters/ (legacy, untouched by this block):
that package is confirmed dead code in docs/MARKET_DATA_ARCHITECTURE.md
(MD-1) — empty TODO stubs (`pass`, `return []`) around a candle-only
Protocol (IMarketDataProvider / CandleDTO), never instantiated anywhere.
It is left exactly as-is, pending the MD-2 dead-code cleanup called out in
that document. This package is the new, tick-oriented replacement design
built on the FOUNDATION-02/03 contracts — deliberately not merged with the
legacy stubs, to avoid two importable things with overlapping names and no
clear owner.
"""

from .base import MarketDataProviderAdapter
from .mappings import ProviderSymbolMapping
from .registry import (
    ProviderCapabilityProfile,
    all_profiles,
    get_profile,
    register_profile,
    supports,
)

# Importing these triggers each adapter's self-registration into the
# capability registry (see registry.register_profile() calls at the
# bottom of each module) — mirrors the _REGISTRY pattern in symbol_specs.py.
from .binance import BinanceAdapter
from .finnhub import FinnhubAdapter
from .kraken import KrakenAdapter

__all__ = [
    "MarketDataProviderAdapter",
    "ProviderSymbolMapping",
    "ProviderCapabilityProfile",
    "register_profile",
    "get_profile",
    "supports",
    "all_profiles",
    "BinanceAdapter",
    "KrakenAdapter",
    "FinnhubAdapter",
]
