"""
market_data/providers/kraken.py — Kraken provider adapter (FOUNDATION-04).

Payload shape mirrors the real Kraken WS v1 ticker message consumed today
by market_data/feeds.py::_kraken_loop — a 4+ element list
[channelID, data, channelName, pair]. This module performs zero network
I/O: it only transforms a payload already in hand.
"""

from __future__ import annotations

from typing import Any

from market_data.contracts import ProviderCapability

from .base import MarketDataProviderAdapter
from .mappings import ProviderSymbolMapping
from .registry import ProviderCapabilityProfile, register_profile


class KrakenAdapter(MarketDataProviderAdapter):
    """Kraken ticker (best bid/ask), real-time over WebSocket. Fallback provider for crypto."""

    provider_id = "kraken"
    capabilities = frozenset({
        ProviderCapability.REALTIME_TICKS,
        ProviderCapability.BID_ASK,
        ProviderCapability.OHLC,
        ProviderCapability.HISTORY,
        ProviderCapability.WEBSOCKET,
        ProviderCapability.REST,
    })

    def build_subscription_request(self, mapping: ProviderSymbolMapping) -> dict[str, Any]:
        self.validate_symbol_mapping(mapping)
        return {
            "transport": "WEBSOCKET",
            "event": "subscribe",
            "pair": [mapping.provider_symbol],
            "subscription": {"name": "ticker"},
        }

    def parse_message(self, raw_message: Any) -> dict[str, Any]:
        if not isinstance(raw_message, list) or len(raw_message) < 4:
            raise ValueError(
                "Kraken message must be a list [channelID, data, channelName, pair] "
                "with at least 4 elements"
            )

        channel_name = raw_message[-2]
        if channel_name != "ticker":
            raise ValueError(f"Kraken message is not a ticker update (channel={channel_name!r})")

        data = raw_message[1]
        if not isinstance(data, dict):
            raise ValueError("Kraken ticker message missing data object")

        try:
            bid = float(data["b"][0])
            ask = float(data["a"][0])
        except (KeyError, IndexError) as exc:
            raise ValueError(f"Kraken ticker message missing field {exc}") from None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Kraken ticker message has non-numeric price: {exc}") from None

        return {"bid": bid, "ask": ask}


register_profile(ProviderCapabilityProfile(
    provider_id=KrakenAdapter.provider_id,
    capabilities=KrakenAdapter.capabilities,
    asset_classes=frozenset({"crypto"}),
    transports=frozenset({"WEBSOCKET", "REST"}),
))
