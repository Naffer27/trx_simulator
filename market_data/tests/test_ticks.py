"""
market_data/tests/test_ticks.py — FOUNDATION-03.

Pure unittest, no Django dependency: NormalizedTick and should_accept_tick
have zero runtime wiring (no DB, no Channels, no Redis).
"""

import dataclasses
import unittest

from market_data.contracts.enums import SourceState
from market_data.contracts.ticks import (
    CURRENT_SCHEMA_VERSION,
    NormalizedTick,
    is_schema_version_supported,
    parse_schema_version,
    should_accept_tick,
)


def make_tick(**overrides):
    defaults = dict(
        schema_version=CURRENT_SCHEMA_VERSION,
        symbol="EUR/USD",
        provider_id="test-provider",
        source_state=SourceState.LIVE,
        is_synthetic=False,
        is_stale=False,
        timestamp_received=1_000,
        bid=1.10000,
        ask=1.10015,
    )
    defaults.update(overrides)
    return NormalizedTick(**defaults)


class ConstructionTests(unittest.TestCase):
    def test_valid_construction(self):
        tick = make_tick()
        self.assertEqual(tick.symbol, "EUR/USD")
        self.assertEqual(tick.provider_id, "test-provider")
        self.assertEqual(tick.source_state, SourceState.LIVE)
        self.assertFalse(tick.is_synthetic)
        self.assertFalse(tick.is_stale)
        self.assertIsNone(tick.sequence)
        self.assertIsNone(tick.volume)
        self.assertIsNone(tick.open_interest)
        self.assertIsNone(tick.settlement_price)

    def test_optional_fields_can_be_populated(self):
        tick = make_tick(
            last=1.10007,
            timestamp_provider=999,
            sequence=42,
            volume=12.5,
            open_interest=100.0,
            settlement_price=1.10010,
        )
        self.assertEqual(tick.last, 1.10007)
        self.assertEqual(tick.timestamp_provider, 999)
        self.assertEqual(tick.sequence, 42)
        self.assertEqual(tick.volume, 12.5)
        self.assertEqual(tick.open_interest, 100.0)
        self.assertEqual(tick.settlement_price, 1.10010)

    def test_trade_only_tick_without_bid_ask(self):
        # e.g. a deep OTM option — only a last trade print, no continuous quote.
        tick = make_tick(bid=None, ask=None, last=4.20)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)
        self.assertEqual(tick.last, 4.20)


class ImmutabilityTests(unittest.TestCase):
    def test_frozen_rejects_attribute_assignment(self):
        tick = make_tick()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tick.bid = 1.5

    def test_frozen_rejects_new_attribute(self):
        tick = make_tick()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tick.extra_field = "nope"


class SymbolAndProviderValidationTests(unittest.TestCase):
    def test_empty_symbol_rejected(self):
        with self.assertRaises(ValueError):
            make_tick(symbol="")

    def test_empty_provider_id_rejected(self):
        with self.assertRaises(ValueError):
            make_tick(provider_id="")


class PriceValidationTests(unittest.TestCase):
    def test_bid_greater_than_ask_rejected(self):
        with self.assertRaises(ValueError):
            make_tick(bid=1.2000, ask=1.1000)

    def test_bid_equal_ask_is_allowed(self):
        tick = make_tick(bid=1.1000, ask=1.1000)
        self.assertEqual(tick.bid, tick.ask)

    def test_zero_and_negative_prices_rejected(self):
        for field_name, value in [
            ("bid", 0), ("bid", -1.0),
            ("ask", 0), ("ask", -1.0),
            ("last", 0), ("last", -1.0),
        ]:
            with self.subTest(field=field_name, value=value):
                kwargs = {field_name: value}
                # Avoid tripping the bid<=ask check for unrelated fields.
                if field_name == "bid":
                    kwargs["ask"] = None
                with self.assertRaises(ValueError):
                    make_tick(**kwargs)

    def test_none_prices_are_allowed(self):
        tick = make_tick(bid=None, ask=None, last=None)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)
        self.assertIsNone(tick.last)


class TimestampValidationTests(unittest.TestCase):
    def test_timestamp_received_required(self):
        with self.assertRaises(TypeError):
            # kw_only dataclass with no default -> omitting it is a TypeError,
            # which is the strongest possible "required" guarantee.
            NormalizedTick(
                schema_version=CURRENT_SCHEMA_VERSION,
                symbol="EUR/USD",
                provider_id="test-provider",
                source_state=SourceState.LIVE,
                is_synthetic=False,
                is_stale=False,
            )

    def test_timestamp_received_none_rejected(self):
        with self.assertRaises(ValueError):
            make_tick(timestamp_received=None)


class SourceStateValidationTests(unittest.TestCase):
    def test_invalid_source_state_rejected(self):
        with self.assertRaises(ValueError):
            make_tick(source_state="NOT_A_REAL_STATE")

    def test_raw_string_source_state_is_coerced(self):
        tick = make_tick(source_state="LIVE")
        self.assertEqual(tick.source_state, SourceState.LIVE)
        self.assertIsInstance(tick.source_state, SourceState)


class SyntheticCoherenceTests(unittest.TestCase):
    def test_simulation_requires_synthetic_true(self):
        tick = make_tick(source_state=SourceState.SIMULATION, is_synthetic=True)
        self.assertTrue(tick.is_synthetic)
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.SIMULATION, is_synthetic=False)

    def test_recovery_requires_synthetic_true(self):
        tick = make_tick(source_state=SourceState.RECOVERY, is_synthetic=True)
        self.assertTrue(tick.is_synthetic)
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.RECOVERY, is_synthetic=False)

    def test_live_requires_synthetic_false(self):
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.LIVE, is_synthetic=True)

    def test_secondary_requires_synthetic_false(self):
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.SECONDARY, is_synthetic=True)

    def test_stale_requires_synthetic_false(self):
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.STALE, is_synthetic=True)

    def test_market_closed_requires_synthetic_false(self):
        with self.assertRaises(ValueError):
            make_tick(source_state=SourceState.MARKET_CLOSED, is_synthetic=True)


class SchemaVersionTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(parse_schema_version("1.0"), (1, 0))
        self.assertEqual(parse_schema_version("1.7"), (1, 7))

    def test_parse_malformed_raises(self):
        for bad in ("abc", "1", "1.x", ""):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    parse_schema_version(bad)

    def test_current_major_supported(self):
        self.assertTrue(is_schema_version_supported(CURRENT_SCHEMA_VERSION))

    def test_same_major_additive_minor_supported(self):
        # Additive/minor bumps within the same major must stay compatible.
        self.assertTrue(is_schema_version_supported("1.99"))

    def test_different_major_unsupported(self):
        self.assertFalse(is_schema_version_supported("2.0"))

    def test_malformed_version_unsupported(self):
        self.assertFalse(is_schema_version_supported("not-a-version"))

    def test_tick_construction_rejects_unsupported_version(self):
        with self.assertRaises(ValueError):
            make_tick(schema_version="2.0")

    def test_tick_construction_rejects_malformed_version(self):
        with self.assertRaises(ValueError):
            make_tick(schema_version="garbage")


class ShouldAcceptTickTests(unittest.TestCase):
    def test_first_tick_always_accepted(self):
        candidate = make_tick(sequence=1, timestamp_received=1_000)
        self.assertTrue(should_accept_tick(None, candidate))

    def test_higher_sequence_accepted(self):
        previous = make_tick(sequence=5, timestamp_received=1_000)
        candidate = make_tick(sequence=6, timestamp_received=1_001)
        self.assertTrue(should_accept_tick(previous, candidate))

    def test_equal_sequence_rejected_as_duplicate(self):
        previous = make_tick(sequence=5, timestamp_received=1_000)
        candidate = make_tick(sequence=5, timestamp_received=1_050)
        self.assertFalse(should_accept_tick(previous, candidate))

    def test_lower_sequence_rejected_as_out_of_order(self):
        previous = make_tick(sequence=10, timestamp_received=1_000)
        candidate = make_tick(sequence=9, timestamp_received=1_050)
        self.assertFalse(should_accept_tick(previous, candidate))

    def test_exact_duplicate_rejected(self):
        previous = make_tick(sequence=5, timestamp_received=1_000)
        candidate = make_tick(sequence=5, timestamp_received=1_000)
        self.assertFalse(should_accept_tick(previous, candidate))

    def test_falls_back_to_timestamp_provider_when_no_sequence(self):
        previous = make_tick(sequence=None, timestamp_provider=500, timestamp_received=1_000)
        newer = make_tick(sequence=None, timestamp_provider=600, timestamp_received=1_001)
        older = make_tick(sequence=None, timestamp_provider=400, timestamp_received=1_002)
        self.assertTrue(should_accept_tick(previous, newer))
        self.assertFalse(should_accept_tick(previous, older))

    def test_timestamp_provider_tie_breaks_on_timestamp_received(self):
        previous = make_tick(sequence=None, timestamp_provider=500, timestamp_received=1_000)
        retransmit_newer = make_tick(sequence=None, timestamp_provider=500, timestamp_received=1_050)
        retransmit_older_or_equal = make_tick(sequence=None, timestamp_provider=500, timestamp_received=1_000)
        self.assertTrue(should_accept_tick(previous, retransmit_newer))
        self.assertFalse(should_accept_tick(previous, retransmit_older_or_equal))

    def test_falls_back_to_timestamp_received_when_nothing_else_comparable(self):
        previous = make_tick(sequence=None, timestamp_provider=None, timestamp_received=1_000)
        newer = make_tick(sequence=None, timestamp_provider=None, timestamp_received=1_001)
        older = make_tick(sequence=None, timestamp_provider=None, timestamp_received=999)
        self.assertTrue(should_accept_tick(previous, newer))
        self.assertFalse(should_accept_tick(previous, older))

    def test_different_provider_is_not_compared_as_same_stream(self):
        previous = make_tick(provider_id="binance", sequence=100, timestamp_received=1_000)
        candidate = make_tick(provider_id="kraken", sequence=1, timestamp_received=1)
        # sequence=1 would be "out of order" against sequence=100 if compared —
        # but it's a different provider, so it must be accepted unconditionally.
        self.assertTrue(should_accept_tick(previous, candidate))

    def test_different_symbol_is_not_compared_as_same_stream(self):
        previous = make_tick(symbol="EUR/USD", sequence=100, timestamp_received=1_000)
        candidate = make_tick(symbol="GBP/USD", sequence=1, timestamp_received=1)
        self.assertTrue(should_accept_tick(previous, candidate))


if __name__ == "__main__":
    unittest.main()
