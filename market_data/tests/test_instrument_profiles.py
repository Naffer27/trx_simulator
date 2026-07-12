"""
market_data/tests/test_instrument_profiles.py — FOUNDATION-06.

InstrumentProfile construction/validation. Pure unittest, no Django
dependency — this contract has zero runtime wiring.
"""

import dataclasses
import unittest

from market_data.contracts import OrderPolicy, ProviderCapability
from market_data.instruments.profiles import InstrumentProfile
from market_data.providers.mappings import ProviderSymbolMapping


def make_mapping(**overrides):
    defaults = dict(
        canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD",
        priority=0, enabled=True,
    )
    defaults.update(overrides)
    return ProviderSymbolMapping(**defaults)


def make_profile(**overrides):
    defaults = dict(
        canonical_symbol="EUR/USD", display_name="EUR/USD", asset_class="forex",
        base_currency="EUR", quote_currency="USD",
        pip_size=0.0001, tick_size=0.00001, price_decimals=5,
        lot_step=0.01, min_lot=0.01, max_lot=100.0, contract_size=100000.0, max_leverage=500,
        default_spread=1.5, spread_unit="pips", commission_per_lot=0.0, commission_pct=0.0,
        margin_mode="leverage", pnl_mode="STANDARD", trading_enabled=True, trading_calendar_id="24/5",
        provider_mappings=(make_mapping(),), required_capabilities=frozenset({ProviderCapability.BID_ASK}),
        simulation_allowed=True, default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
    )
    defaults.update(overrides)
    return InstrumentProfile(**defaults)


class ConstructionTests(unittest.TestCase):
    def test_valid_construction(self):
        profile = make_profile()
        self.assertEqual(profile.canonical_symbol, "EUR/USD")
        self.assertEqual(profile.profile_version, "1.0")

    def test_frozen(self):
        profile = make_profile()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.max_leverage = 10

    def test_empty_canonical_symbol_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(canonical_symbol="")


class PositiveFieldTests(unittest.TestCase):
    def test_non_positive_fields_rejected(self):
        for field_name in ("pip_size", "tick_size", "lot_step", "min_lot", "max_lot", "contract_size"):
            for bad in (0, -1):
                with self.subTest(field=field_name, value=bad):
                    with self.assertRaises(ValueError):
                        make_profile(**{field_name: bad})

    def test_negative_price_decimals_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(price_decimals=-1)

    def test_max_leverage_must_be_positive(self):
        with self.assertRaises(ValueError):
            make_profile(max_leverage=0)


class LotCoherenceTests(unittest.TestCase):
    def test_min_lot_greater_than_max_lot_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(min_lot=200.0, max_lot=100.0)

    def test_lot_step_wider_than_max_lot_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(lot_step=200.0, max_lot=100.0)

    def test_lot_step_within_range_accepted(self):
        profile = make_profile(lot_step=0.5, min_lot=0.5, max_lot=100.0)
        self.assertEqual(profile.lot_step, 0.5)


class EnumeratedFieldTests(unittest.TestCase):
    def test_invalid_margin_mode_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(margin_mode="bogus")

    def test_invalid_pnl_mode_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(pnl_mode="bogus")

    def test_invalid_spread_unit_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(spread_unit="bogus")

    def test_valid_values_accepted(self):
        profile = make_profile(margin_mode="percent", pnl_mode="INVERSE", spread_unit="points")
        self.assertEqual(profile.margin_mode, "percent")


class ProviderMappingValidationTests(unittest.TestCase):
    def test_mapping_symbol_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(provider_mappings=(make_mapping(canonical_symbol="GBP/USD"),))

    def test_duplicate_priorities_rejected(self):
        mappings = (
            make_mapping(provider_id="finnhub", priority=0),
            make_mapping(provider_id="binance", priority=0),
        )
        with self.assertRaises(ValueError):
            make_profile(provider_mappings=mappings)

    def test_enabled_required_when_no_simulation(self):
        with self.assertRaises(ValueError):
            make_profile(
                trading_enabled=True, simulation_allowed=False,
                provider_mappings=(make_mapping(enabled=False),),
            )

    def test_enabled_mapping_satisfies_no_simulation_rule(self):
        profile = make_profile(
            trading_enabled=True, simulation_allowed=False,
            provider_mappings=(make_mapping(enabled=True),),
        )
        self.assertFalse(profile.simulation_allowed)

    def test_simulation_allowed_bypasses_enabled_mapping_requirement(self):
        profile = make_profile(trading_enabled=True, simulation_allowed=True, provider_mappings=())
        self.assertEqual(profile.provider_mappings, ())

    def test_disabled_instrument_bypasses_enabled_mapping_requirement(self):
        profile = make_profile(
            trading_enabled=False, simulation_allowed=False, provider_mappings=(),
        )
        self.assertFalse(profile.trading_enabled)


class DegradationPolicyTests(unittest.TestCase):
    def test_open_normal_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(default_order_policy_on_degradation=OrderPolicy.OPEN_NORMAL)

    def test_market_closed_rejected(self):
        with self.assertRaises(ValueError):
            make_profile(default_order_policy_on_degradation=OrderPolicy.MARKET_CLOSED)

    def test_close_only_accepted(self):
        profile = make_profile(default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY)
        self.assertEqual(profile.default_order_policy_on_degradation, OrderPolicy.CLOSE_ONLY)

    def test_halt_new_orders_accepted(self):
        profile = make_profile(default_order_policy_on_degradation=OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(profile.default_order_policy_on_degradation, OrderPolicy.HALT_NEW_ORDERS)


if __name__ == "__main__":
    unittest.main()
