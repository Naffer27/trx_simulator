"""
market_data/contracts — FOUNDATION-03: Market Data Contracts Foundation.

Pure data contracts and pure validation/ordering functions defined by
docs/FOUNDATION_02_MARKET_DATA_CORE.md. Nothing in this package is wired
into the runtime (feeds.py, consumers.py, symbol_specs.py, risk_engine.py,
spread_engine.py, exposure_engine.py, tasks.py) yet — that is a separate,
later block.
"""

from .enums import (
    OrderPolicy,
    ProviderCapability,
    ProviderHealthState,
    SourceState,
)
from .ticks import (
    CURRENT_SCHEMA_VERSION,
    NormalizedTick,
    is_schema_version_supported,
    parse_schema_version,
    should_accept_tick,
)

__all__ = [
    "OrderPolicy",
    "ProviderCapability",
    "ProviderHealthState",
    "SourceState",
    "CURRENT_SCHEMA_VERSION",
    "NormalizedTick",
    "is_schema_version_supported",
    "parse_schema_version",
    "should_accept_tick",
]
