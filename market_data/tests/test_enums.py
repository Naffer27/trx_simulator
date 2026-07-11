"""
market_data/tests/test_enums.py — FOUNDATION-03.

Pure unittest, no Django dependency: these enums have zero runtime wiring.
"""

import unittest

from market_data.contracts.enums import (
    OrderPolicy,
    ProviderCapability,
    ProviderHealthState,
    SourceState,
)


class SourceStateTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(
            {m.value for m in SourceState},
            {"LIVE", "SECONDARY", "SIMULATION", "RECOVERY", "STALE", "MARKET_CLOSED"},
        )

    def test_is_str_subclass(self):
        self.assertEqual(SourceState.LIVE, "LIVE")
        self.assertIsInstance(SourceState.LIVE, str)


class OrderPolicyTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(
            {m.value for m in OrderPolicy},
            {"OPEN_NORMAL", "CLOSE_ONLY", "HALT_NEW_ORDERS", "MARKET_CLOSED"},
        )


class ProviderHealthStateTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(
            {m.value for m in ProviderHealthState},
            {"CLOSED", "OPEN", "HALF_OPEN"},
        )


class ProviderCapabilityTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(
            {m.value for m in ProviderCapability},
            {
                "REALTIME_TICKS", "BID_ASK", "LAST_PRICE", "OHLC", "HISTORY",
                "MARKET_DEPTH", "VOLUME", "OPEN_INTEREST", "WEBSOCKET",
                "REST", "MARKET_STATUS",
            },
        )


if __name__ == "__main__":
    unittest.main()
