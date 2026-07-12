"""
market_data/tests/test_instrument_routing.py — FOUNDATION-07.

build_route_plan(), RouterPolicyConfig, and default_policy_for_asset_class().
Pure unittest, no Django dependency — this builder has zero runtime wiring.
"""

import dataclasses
import unittest

import market_data.providers  # noqa: F401 — triggers binance/kraken/finnhub registration
from market_data.contracts import ProviderCapability
from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.instruments.routing import (
    RouterPolicyConfig,
    build_route_plan,
    default_policy_for_asset_class,
)
from market_data.router.models import ProviderRoutePlan
from market_data.router.router import ProviderRouter
from market_data.symbol_specs import get_spec

from .test_instrument_profiles import make_mapping, make_profile


class BuildRoutePlanFromRealSpecsTests(unittest.TestCase):
    def test_btcusd_binance_primary_kraken_secondary(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("BTCUSD")))
        self.assertIsInstance(plan, ProviderRoutePlan)
        self.assertEqual(len(plan.entries), 2)
        by_provider = {e.provider_id: e for e in plan.entries}
        self.assertEqual(set(by_provider), {"binance", "kraken"})
        self.assertEqual(by_provider["binance"].priority, 0)
        self.assertEqual(by_provider["kraken"].priority, 1)
        self.assertEqual(by_provider["binance"].provider_symbol, "BTCUSDT")
        self.assertEqual(by_provider["kraken"].provider_symbol, "XBT/USD")

    def test_ethusd_binance_primary_kraken_secondary(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("ETHUSD")))
        by_provider = {e.provider_id: e for e in plan.entries}
        self.assertEqual(set(by_provider), {"binance", "kraken"})
        self.assertEqual(by_provider["binance"].priority, 0)
        self.assertEqual(by_provider["kraken"].priority, 1)

    def test_eur_usd_finnhub_only(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("EUR/USD")))
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(plan.entries[0].provider_id, "finnhub")
        self.assertEqual(plan.entries[0].priority, 0)

    def test_default_order_policy_preserved(self):
        profile = profile_from_symbol_spec(get_spec("EUR/USD"))
        plan = build_route_plan(profile)
        self.assertEqual(plan.default_order_policy_on_degradation, profile.default_order_policy_on_degradation)

    def test_simulation_allowed_preserved(self):
        profile = profile_from_symbol_spec(get_spec("EUR/USD"))
        plan = build_route_plan(profile)
        self.assertEqual(plan.simulation_allowed, profile.simulation_allowed)

    def test_disabled_symbol_becomes_simulation_only_plan(self):
        # USD/CAD is disabled in symbol_specs.py -> its mapping is disabled too.
        plan = build_route_plan(profile_from_symbol_spec(get_spec("USD/CAD")))
        self.assertEqual(plan.entries, ())
        self.assertTrue(plan.simulation_allowed)

    def test_every_registered_symbol_builds_a_valid_plan(self):
        from market_data.symbol_specs import get_all_specs
        for spec in get_all_specs():
            with self.subTest(symbol=spec.symbol):
                plan = build_route_plan(profile_from_symbol_spec(spec))
                self.assertEqual(plan.canonical_symbol, spec.symbol)


class DisabledMappingIgnoredTests(unittest.TestCase):
    def test_disabled_mapping_excluded_enabled_mapping_kept(self):
        profile = make_profile(
            provider_mappings=(
                make_mapping(provider_id="finnhub", priority=0, enabled=False),
                make_mapping(provider_id="binance", priority=1, enabled=True, provider_symbol="EURUSDT"),
            ),
        )
        plan = build_route_plan(profile)
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(plan.entries[0].provider_id, "binance")
        self.assertEqual(plan.entries[0].priority, 0)  # re-densified, not the original 1

    def test_all_mappings_disabled_yields_empty_entries(self):
        profile = make_profile(
            trading_enabled=False,  # required to construct with no enabled mapping + no simulation guard tripping
            provider_mappings=(make_mapping(enabled=False),),
        )
        plan = build_route_plan(profile)
        self.assertEqual(plan.entries, ())


class CapabilityValidationTests(unittest.TestCase):
    def test_unknown_provider_rejected(self):
        profile = make_profile(
            provider_mappings=(make_mapping(provider_id="oanda", provider_symbol="EUR_USD"),),
        )
        with self.assertRaises(ValueError) as ctx:
            build_route_plan(profile)
        self.assertIn("oanda", str(ctx.exception))

    def test_capability_mismatch_rejected(self):
        # finnhub does not support BID_ASK (FOUNDATION-04 registry) — a
        # mapping that requires it must fail at build time, not be silently
        # dropped or degraded.
        profile = make_profile(
            provider_mappings=(
                make_mapping(
                    provider_id="finnhub", required_capabilities=frozenset({ProviderCapability.BID_ASK}),
                ),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            build_route_plan(profile)
        self.assertIn("finnhub", str(ctx.exception))
        self.assertIn("BID_ASK", str(ctx.exception))

    def test_supported_capability_accepted(self):
        profile = make_profile(
            provider_mappings=(
                make_mapping(
                    provider_id="finnhub", required_capabilities=frozenset({ProviderCapability.LAST_PRICE}),
                ),
            ),
        )
        plan = build_route_plan(profile)
        self.assertEqual(len(plan.entries), 1)

    def test_disabled_mapping_with_bad_capability_is_not_validated(self):
        # A disabled mapping is dropped before validation — an unknown
        # provider or unsupported capability on a disabled mapping must not
        # block building the plan.
        profile = make_profile(
            trading_enabled=False,
            provider_mappings=(make_mapping(provider_id="totally_unknown_provider", enabled=False),),
        )
        plan = build_route_plan(profile)
        self.assertEqual(plan.entries, ())


class SimulationOnlyTests(unittest.TestCase):
    def test_empty_mappings_simulation_allowed_builds_valid_plan(self):
        profile = make_profile(provider_mappings=(), simulation_allowed=True)
        plan = build_route_plan(profile)
        self.assertEqual(plan.entries, ())
        self.assertTrue(plan.simulation_allowed)

    def test_empty_mappings_no_simulation_rejected(self):
        profile = make_profile(
            trading_enabled=False, provider_mappings=(), simulation_allowed=False,
        )
        with self.assertRaises(ValueError):
            build_route_plan(profile)


class RouterPolicyConfigTests(unittest.TestCase):
    def test_valid_construction(self):
        policy = RouterPolicyConfig(max_failures=3, open_cooldown_seconds=10, half_open_successes_required=2)
        self.assertEqual(policy.max_failures, 3)

    def test_frozen(self):
        policy = RouterPolicyConfig(max_failures=3, open_cooldown_seconds=10, half_open_successes_required=2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.max_failures = 5

    def test_non_positive_fields_rejected(self):
        with self.assertRaises(ValueError):
            RouterPolicyConfig(max_failures=0, open_cooldown_seconds=10, half_open_successes_required=2)
        with self.assertRaises(ValueError):
            RouterPolicyConfig(max_failures=3, open_cooldown_seconds=0, half_open_successes_required=2)
        with self.assertRaises(ValueError):
            RouterPolicyConfig(max_failures=3, open_cooldown_seconds=10, half_open_successes_required=0)

    def test_defaults_by_asset_class(self):
        self.assertEqual(default_policy_for_asset_class("crypto").open_cooldown_seconds, 10)
        self.assertEqual(default_policy_for_asset_class("forex").open_cooldown_seconds, 15)
        self.assertEqual(default_policy_for_asset_class("metal").open_cooldown_seconds, 15)
        self.assertEqual(default_policy_for_asset_class("energy").open_cooldown_seconds, 20)
        self.assertEqual(default_policy_for_asset_class("index").open_cooldown_seconds, 20)
        for asset_class in ("crypto", "forex", "metal", "energy", "index"):
            with self.subTest(asset_class=asset_class):
                policy = default_policy_for_asset_class(asset_class)
                self.assertEqual(policy.max_failures, 3)
                self.assertEqual(policy.half_open_successes_required, 2)

    def test_unknown_asset_class_gets_conservative_fallback(self):
        policy = default_policy_for_asset_class("option")
        self.assertEqual(policy.open_cooldown_seconds, 20)

    def test_build_route_plan_uses_asset_class_default(self):
        profile = profile_from_symbol_spec(get_spec("BTCUSD"))  # asset_class="crypto"
        plan = build_route_plan(profile)
        self.assertEqual(plan.open_cooldown_seconds, 10)

    def test_build_route_plan_accepts_explicit_policy_override(self):
        profile = profile_from_symbol_spec(get_spec("BTCUSD"))
        custom = RouterPolicyConfig(max_failures=5, open_cooldown_seconds=99, half_open_successes_required=3)
        plan = build_route_plan(profile, policy=custom)
        self.assertEqual(plan.open_cooldown_seconds, 99)
        self.assertEqual(plan.max_failures, 5)
        self.assertEqual(plan.half_open_successes_required, 3)


class RouterIntegrationTests(unittest.TestCase):
    """Proves the plan build_route_plan() produces is directly consumable by
    ProviderRouter.decide() — no adapter/glue code needed between them."""

    def test_btcusd_plan_selects_binance_primary(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("BTCUSD")))
        decision = ProviderRouter().decide(plan, now=0)
        self.assertEqual(decision.selected_provider_id, "binance")

    def test_eur_usd_plan_selects_finnhub(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("EUR/USD")))
        decision = ProviderRouter().decide(plan, now=0)
        self.assertEqual(decision.selected_provider_id, "finnhub")

    def test_disabled_symbol_plan_falls_back_to_simulation(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("USD/CAD")))
        decision = ProviderRouter().decide(plan, now=0)
        self.assertIsNone(decision.selected_provider_id)

    def test_router_failover_still_works_on_a_built_plan(self):
        plan = build_route_plan(profile_from_symbol_spec(get_spec("BTCUSD")))
        router = ProviderRouter()
        for t in range(plan.max_failures):
            router.provider_failure("binance", plan, now=t)
        decision = router.decide(plan, now=plan.max_failures + 1)
        self.assertEqual(decision.selected_provider_id, "kraken")


if __name__ == "__main__":
    unittest.main()
