"""
market_data/tests/test_providers_adapters.py — FOUNDATION-04.

Normalization tests for BinanceAdapter, KrakenAdapter, FinnhubAdapter:
mock payloads in, NormalizedTick (or a clear ValueError) out. No network,
no DB — every payload here is a plain literal dict/list, never fetched.

Pure unittest, no Django dependency.
"""

import unittest

from market_data.contracts import NormalizedTick, ProviderCapability, SourceState
from market_data.providers.binance import BinanceAdapter
from market_data.providers.finnhub import FinnhubAdapter
from market_data.providers.kraken import KrakenAdapter
from market_data.providers.mappings import ProviderSymbolMapping


class BinanceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BinanceAdapter()
        self.mapping = ProviderSymbolMapping(
            canonical_symbol="BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
        )

    def test_provider_id(self):
        self.assertEqual(self.adapter.provider_id, "binance")

    def test_build_subscription_request(self):
        request = self.adapter.build_subscription_request(self.mapping)
        self.assertEqual(request, {"transport": "WEBSOCKET", "stream": "btcusdt@bookTicker"})

    def test_build_subscription_request_rejects_wrong_provider_mapping(self):
        wrong_mapping = ProviderSymbolMapping(canonical_symbol="BTCUSD", provider_id="kraken", provider_symbol="XBT/USD")
        with self.assertRaises(ValueError):
            self.adapter.build_subscription_request(wrong_mapping)

    def test_normalize_tick_happy_path(self):
        raw = {"stream": "btcusdt@bookTicker", "data": {"u": 400900217, "s": "BTCUSDT", "b": "82000.10", "B": "1", "a": "82000.50", "A": "1"}}
        tick = self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1_700_000_000)
        self.assertIsInstance(tick, NormalizedTick)
        self.assertEqual(tick.symbol, "BTCUSD")
        self.assertEqual(tick.provider_id, "binance")
        self.assertEqual(tick.source_state, SourceState.LIVE)
        self.assertFalse(tick.is_synthetic)
        self.assertFalse(tick.is_stale)
        self.assertEqual(tick.bid, 82000.10)
        self.assertEqual(tick.ask, 82000.50)
        self.assertIsNone(tick.last)
        self.assertEqual(tick.sequence, 400900217)
        self.assertIsNone(tick.timestamp_provider)
        self.assertEqual(tick.timestamp_received, 1_700_000_000)

    def test_missing_data_object_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick({"stream": "btcusdt@bookTicker"}, self.mapping, timestamp_received=1)

    def test_missing_bid_field_rejected(self):
        raw = {"data": {"a": "82000.50"}}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_non_numeric_price_rejected(self):
        raw = {"data": {"b": "not-a-number", "a": "82000.50"}}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_invalid_sequence_rejected(self):
        raw = {"data": {"b": "1.0", "a": "1.1", "u": "not-an-int"}}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_negative_price_rejected_by_tick_contract(self):
        raw = {"data": {"b": "-1.0", "a": "1.1"}}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_bid_greater_than_ask_rejected_by_tick_contract(self):
        raw = {"data": {"b": "2.0", "a": "1.0"}}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)


class KrakenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = KrakenAdapter()
        self.mapping = ProviderSymbolMapping(
            canonical_symbol="BTCUSD", provider_id="kraken", provider_symbol="XBT/USD",
        )

    def test_provider_id(self):
        self.assertEqual(self.adapter.provider_id, "kraken")

    def test_build_subscription_request(self):
        request = self.adapter.build_subscription_request(self.mapping)
        self.assertEqual(request["pair"], ["XBT/USD"])
        self.assertEqual(request["subscription"], {"name": "ticker"})

    def test_normalize_tick_happy_path(self):
        raw = [340, {"a": ["82000.5", "1", "1.000"], "b": ["82000.1", "1", "1.000"]}, "ticker", "XBT/USD"]
        tick = self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1_700_000_000)
        self.assertEqual(tick.symbol, "BTCUSD")
        self.assertEqual(tick.provider_id, "kraken")
        self.assertEqual(tick.source_state, SourceState.LIVE)
        self.assertFalse(tick.is_synthetic)
        self.assertEqual(tick.bid, 82000.1)
        self.assertEqual(tick.ask, 82000.5)
        self.assertIsNone(tick.sequence)

    def test_short_message_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick([1, {}], self.mapping, timestamp_received=1)

    def test_non_ticker_channel_rejected(self):
        raw = [340, {"c": ["1", "1"]}, "ohlc-1", "XBT/USD"]
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_missing_price_field_rejected(self):
        raw = [340, {"a": ["82000.5", "1", "1.000"]}, "ticker", "XBT/USD"]
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_non_numeric_price_rejected(self):
        raw = [340, {"a": ["not-a-number", "1", "1.000"], "b": ["82000.1", "1", "1.000"]}, "ticker", "XBT/USD"]
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)


class FinnhubAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FinnhubAdapter()
        self.mapping = ProviderSymbolMapping(
            canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD",
        )

    def test_provider_id(self):
        self.assertEqual(self.adapter.provider_id, "finnhub")

    def test_declares_no_bid_ask_capability(self):
        self.assertNotIn(ProviderCapability.BID_ASK, self.adapter.capabilities)
        self.assertIn(ProviderCapability.LAST_PRICE, self.adapter.capabilities)

    def test_build_subscription_request(self):
        request = self.adapter.build_subscription_request(self.mapping)
        self.assertEqual(request, {"transport": "WEBSOCKET", "type": "subscribe", "symbol": "FX:EURUSD"})

    def test_normalize_tick_happy_path(self):
        raw = {"type": "trade", "data": [{"p": 1.10005, "s": "FX:EURUSD", "t": 1_700_000_000_000, "v": 0}]}
        tick = self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1_700_000_001)
        self.assertEqual(tick.symbol, "EUR/USD")
        self.assertEqual(tick.provider_id, "finnhub")
        self.assertEqual(tick.source_state, SourceState.LIVE)
        self.assertFalse(tick.is_synthetic)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)
        self.assertEqual(tick.last, 1.10005)
        self.assertEqual(tick.timestamp_provider, 1_700_000_000_000)
        self.assertEqual(tick.timestamp_received, 1_700_000_001)

    def test_non_trade_message_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick({"type": "ping"}, self.mapping, timestamp_received=1)

    def test_empty_data_list_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick({"type": "trade", "data": []}, self.mapping, timestamp_received=1)

    def test_missing_price_field_rejected(self):
        raw = {"type": "trade", "data": [{"s": "FX:EURUSD", "t": 1}]}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_non_numeric_price_rejected(self):
        raw = {"type": "trade", "data": [{"p": "not-a-number", "t": 1}]}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_invalid_timestamp_rejected(self):
        raw = {"type": "trade", "data": [{"p": 1.1, "t": "not-a-number"}]}
        with self.assertRaises(ValueError):
            self.adapter.normalize_tick(raw, self.mapping, timestamp_received=1)

    def test_missing_timestamp_field_is_allowed(self):
        # Not every trade print is guaranteed to carry "t" — timestamp_provider is optional.
        raw = {"type": "trade", "data": [{"p": 1.1}]}
        tick = self.adapter.normalize_tick(raw, self.mapping, timestamp_received=42)
        self.assertIsNone(tick.timestamp_provider)


if __name__ == "__main__":
    unittest.main()
