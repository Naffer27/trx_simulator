"""
market_data/contracts/enums.py — typed values for the market data contracts.

Pure data. No provider names, no runtime wiring, no Django dependency.
Defined in FOUNDATION-02 (docs/FOUNDATION_02_MARKET_DATA_CORE.md).
"""

from enum import Enum


class SourceState(str, Enum):
    """Where a NormalizedTick's price came from, as seen by the ProviderRouter."""

    LIVE = "LIVE"                  # primary real provider, within SLA
    SECONDARY = "SECONDARY"        # fallback real provider, within SLA
    SIMULATION = "SIMULATION"      # no real provider available — synthetic price
    RECOVERY = "RECOVERY"          # probing a real provider while still serving simulation
    STALE = "STALE"                # last real price held past its freshness window
    MARKET_CLOSED = "MARKET_CLOSED"  # instrument's trading_calendar says market is shut


class OrderPolicy(str, Enum):
    """
    What trading actions are allowed for a symbol right now.

    Deliberately NOT a field of NormalizedTick (see FOUNDATION-02 §3.5) — it is a
    business decision derived from SourceState + InstrumentProfile, not a market fact.
    """

    OPEN_NORMAL = "OPEN_NORMAL"
    CLOSE_ONLY = "CLOSE_ONLY"
    HALT_NEW_ORDERS = "HALT_NEW_ORDERS"
    MARKET_CLOSED = "MARKET_CLOSED"


class ProviderHealthState(str, Enum):
    """Circuit-breaker state for one (provider, symbol) pair."""

    CLOSED = "CLOSED"        # healthy — requests flow normally
    OPEN = "OPEN"             # tripped — requests short-circuited
    HALF_OPEN = "HALF_OPEN"   # probing recovery


class ProviderCapability(str, Enum):
    """One objectively-checkable thing a provider can do (Provider Capability Registry, FOUNDATION-02 §4)."""

    REALTIME_TICKS = "REALTIME_TICKS"
    BID_ASK = "BID_ASK"
    LAST_PRICE = "LAST_PRICE"
    OHLC = "OHLC"
    HISTORY = "HISTORY"
    MARKET_DEPTH = "MARKET_DEPTH"
    VOLUME = "VOLUME"
    OPEN_INTEREST = "OPEN_INTEREST"
    WEBSOCKET = "WEBSOCKET"
    REST = "REST"
    MARKET_STATUS = "MARKET_STATUS"
