"""
market_data/providers/binance.py — Binance provider adapter (FOUNDATION-04).

Payload shape mirrors the real combined-stream bookTicker message consumed
today by market_data/feeds.py::_binance_loop — grounded in the live wire
format, not invented. This module performs zero network I/O: it only
transforms a payload dict already in hand.
"""

from __future__ import annotations

from typing import Any

from market_data.contracts import ProviderCapability

from .base import MarketDataProviderAdapter
from .mappings import ProviderSymbolMapping
from .registry import ProviderCapabilityProfile, register_profile


class BinanceAdapter(MarketDataProviderAdapter):
    """Binance bookTicker (best bid/ask), real-time over WebSocket."""

    provider_id = "binance"
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
            "stream": f"{mapping.provider_symbol.lower()}@bookTicker",
        }

    def parse_message(self, raw_message: Any) -> dict[str, Any]:
        if not isinstance(raw_message, dict):
            raise ValueError("Binance message must be a JSON object")

        data = raw_message.get("data")
        if not isinstance(data, dict):
            raise ValueError("Binance bookTicker message missing 'data' object")

        try:
            bid = float(data["b"])
            ask = float(data["a"])
        except KeyError as exc:
            raise ValueError(f"Binance bookTicker message missing field {exc}") from None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Binance bookTicker message has non-numeric price: {exc}") from None

        parsed: dict[str, Any] = {"bid": bid, "ask": ask}

        if "u" in data:
            try:
                parsed["sequence"] = int(data["u"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Binance bookTicker message has invalid update id 'u': {exc}") from None

        return parsed


register_profile(ProviderCapabilityProfile(
    provider_id=BinanceAdapter.provider_id,
    capabilities=BinanceAdapter.capabilities,
    asset_classes=frozenset({"crypto"}),
    transports=frozenset({"WEBSOCKET", "REST"}),
))
