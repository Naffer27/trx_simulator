"""
market_data/providers/finnhub.py — Finnhub provider adapter (FOUNDATION-04).

Payload shape mirrors the real trade message consumed today by
market_data/feeds.py::_finnhub_loop: {"type": "trade", "data": [{"p", "s",
"t", "v"}, ...]}. Finnhub WS is trade-only in this codebase (no bid/ask —
see market_data/feeds.py::_fetch_rest_price, which also only reads "c",
last price, from the REST quote endpoint). This module performs zero
network I/O and never reads FINNHUB_API_KEY or any other env var.
"""

from __future__ import annotations

from typing import Any

from market_data.contracts import ProviderCapability

from .base import MarketDataProviderAdapter
from .mappings import ProviderSymbolMapping
from .registry import ProviderCapabilityProfile, register_profile


class FinnhubAdapter(MarketDataProviderAdapter):
    """Finnhub trade prints — last price only, no continuous bid/ask."""

    provider_id = "finnhub"
    capabilities = frozenset({
        ProviderCapability.REALTIME_TICKS,
        ProviderCapability.LAST_PRICE,
        ProviderCapability.WEBSOCKET,
        ProviderCapability.REST,
    })

    def build_subscription_request(self, mapping: ProviderSymbolMapping) -> dict[str, Any]:
        self.validate_symbol_mapping(mapping)
        return {
            "transport": "WEBSOCKET",
            "type": "subscribe",
            "symbol": mapping.provider_symbol,
        }

    def parse_message(self, raw_message: Any) -> dict[str, Any]:
        if not isinstance(raw_message, dict):
            raise ValueError("Finnhub message must be a JSON object")

        if raw_message.get("type") != "trade":
            raise ValueError(f"Finnhub message is not a trade update (type={raw_message.get('type')!r})")

        trades = raw_message.get("data")
        if not isinstance(trades, list) or not trades:
            raise ValueError("Finnhub trade message missing non-empty 'data' list")

        trade = trades[0]
        try:
            last = float(trade["p"])
        except KeyError as exc:
            raise ValueError(f"Finnhub trade missing field {exc}") from None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Finnhub trade has non-numeric price: {exc}") from None

        parsed: dict[str, Any] = {"last": last}

        if "t" in trade:
            try:
                parsed["timestamp_provider"] = int(trade["t"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Finnhub trade has invalid timestamp 't': {exc}") from None

        return parsed


register_profile(ProviderCapabilityProfile(
    provider_id=FinnhubAdapter.provider_id,
    capabilities=FinnhubAdapter.capabilities,
    asset_classes=frozenset({"forex"}),
    transports=frozenset({"WEBSOCKET", "REST"}),
))
