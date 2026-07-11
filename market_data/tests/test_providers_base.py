"""
market_data/tests/test_providers_base.py — FOUNDATION-04.

Covers: the MarketDataProviderAdapter contract, ProviderSymbolMapping,
the Provider Capability Registry, and the "no network / no DB / no
Django" isolation guarantee — via static import analysis, not by mocking
network calls (there should be nothing to mock).

Pure unittest, no Django dependency.
"""

import ast
import dataclasses
import pathlib
import unittest

import market_data.providers as providers
from market_data.contracts import ProviderCapability
from market_data.providers.base import MarketDataProviderAdapter
from market_data.providers.mappings import ProviderSymbolMapping
from market_data.providers.registry import ProviderCapabilityProfile, get_profile, supports


class ContractTests(unittest.TestCase):
    def test_base_adapter_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            MarketDataProviderAdapter()

    def test_concrete_adapters_are_instances_of_the_contract(self):
        for adapter_cls in (providers.BinanceAdapter, providers.KrakenAdapter, providers.FinnhubAdapter):
            with self.subTest(adapter=adapter_cls.__name__):
                adapter = adapter_cls()
                self.assertIsInstance(adapter, MarketDataProviderAdapter)

    def test_each_adapter_declares_provider_id_and_capabilities(self):
        for adapter_cls in (providers.BinanceAdapter, providers.KrakenAdapter, providers.FinnhubAdapter):
            with self.subTest(adapter=adapter_cls.__name__):
                adapter = adapter_cls()
                self.assertIsInstance(adapter.provider_id, str)
                self.assertTrue(adapter.provider_id)
                self.assertIsInstance(adapter.capabilities, frozenset)
                self.assertTrue(adapter.capabilities)
                for cap in adapter.capabilities:
                    self.assertIsInstance(cap, ProviderCapability)

    def test_probe_returns_true_without_network(self):
        for adapter_cls in (providers.BinanceAdapter, providers.KrakenAdapter, providers.FinnhubAdapter):
            with self.subTest(adapter=adapter_cls.__name__):
                self.assertTrue(adapter_cls().probe())


class ValidateSymbolMappingTests(unittest.TestCase):
    def test_matching_mapping_accepted(self):
        adapter = providers.BinanceAdapter()
        mapping = ProviderSymbolMapping(
            canonical_symbol="BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            required_capabilities=frozenset({ProviderCapability.BID_ASK}),
        )
        adapter.validate_symbol_mapping(mapping)  # must not raise

    def test_mismatched_provider_id_rejected(self):
        adapter = providers.BinanceAdapter()
        mapping = ProviderSymbolMapping(canonical_symbol="BTCUSD", provider_id="kraken", provider_symbol="XBT/USD")
        with self.assertRaises(ValueError):
            adapter.validate_symbol_mapping(mapping)

    def test_missing_required_capability_rejected(self):
        adapter = providers.FinnhubAdapter()  # no BID_ASK
        mapping = ProviderSymbolMapping(
            canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD",
            required_capabilities=frozenset({ProviderCapability.BID_ASK}),
        )
        with self.assertRaises(ValueError):
            adapter.validate_symbol_mapping(mapping)


class ProviderSymbolMappingTests(unittest.TestCase):
    def test_valid_construction(self):
        mapping = ProviderSymbolMapping(
            canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD",
        )
        self.assertEqual(mapping.canonical_symbol, "EUR/USD")
        self.assertTrue(mapping.enabled)
        self.assertEqual(mapping.priority, 0)
        self.assertEqual(mapping.required_capabilities, frozenset())

    def test_frozen(self):
        mapping = ProviderSymbolMapping(canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            mapping.enabled = False

    def test_empty_canonical_symbol_rejected(self):
        with self.assertRaises(ValueError):
            ProviderSymbolMapping(canonical_symbol="", provider_id="finnhub", provider_symbol="FX:EURUSD")

    def test_empty_provider_id_rejected(self):
        with self.assertRaises(ValueError):
            ProviderSymbolMapping(canonical_symbol="EUR/USD", provider_id="", provider_symbol="FX:EURUSD")

    def test_empty_provider_symbol_rejected(self):
        with self.assertRaises(ValueError):
            ProviderSymbolMapping(canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="")


class CapabilityRegistryTests(unittest.TestCase):
    def test_binance_profile(self):
        profile = get_profile("binance")
        self.assertIsInstance(profile, ProviderCapabilityProfile)
        self.assertEqual(profile.asset_classes, frozenset({"crypto"}))
        self.assertIn(ProviderCapability.BID_ASK, profile.capabilities)

    def test_kraken_profile(self):
        profile = get_profile("kraken")
        self.assertEqual(profile.asset_classes, frozenset({"crypto"}))
        self.assertIn(ProviderCapability.BID_ASK, profile.capabilities)

    def test_finnhub_profile(self):
        profile = get_profile("finnhub")
        self.assertEqual(profile.asset_classes, frozenset({"forex"}))
        self.assertIn(ProviderCapability.LAST_PRICE, profile.capabilities)
        self.assertNotIn(ProviderCapability.BID_ASK, profile.capabilities)

    def test_supports_helper(self):
        self.assertTrue(supports("binance", ProviderCapability.BID_ASK))
        self.assertFalse(supports("finnhub", ProviderCapability.BID_ASK))

    def test_unknown_provider_raises(self):
        with self.assertRaises(KeyError):
            get_profile("oanda")

    def test_profile_rejects_empty_capabilities(self):
        with self.assertRaises(ValueError):
            ProviderCapabilityProfile(
                provider_id="x", capabilities=frozenset(), asset_classes=frozenset({"crypto"}),
                transports=frozenset({"WEBSOCKET"}),
            )

    def test_profile_rejects_unknown_transport(self):
        with self.assertRaises(ValueError):
            ProviderCapabilityProfile(
                provider_id="x", capabilities=frozenset({ProviderCapability.LAST_PRICE}),
                asset_classes=frozenset({"crypto"}), transports=frozenset({"CARRIER_PIGEON"}),
            )

    def test_all_profiles_contains_the_three_known_providers(self):
        ids = {p.provider_id for p in providers.all_profiles()}
        self.assertEqual({"binance", "kraken", "finnhub"}, ids & {"binance", "kraken", "finnhub"})


class NoNetworkOrDjangoDependencyTests(unittest.TestCase):
    """
    Static guarantee, not a mock-based one: parse the source of every
    module in market_data/providers/ and assert it never imports anything
    that could touch the network, a database, Django settings, or the
    filesystem/env. If this test passes, there is nothing to accidentally
    call at runtime — because the capability to call it isn't imported.
    """

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os",
    })

    def test_no_forbidden_imports_in_providers_package(self):
        package_dir = pathlib.Path(providers.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/providers/*.py to exist")

        for path in source_files:
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(), filename=str(path))
                imported_roots = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_roots.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and node.level == 0:  # absolute import only; relative (.base etc) is fine
                            imported_roots.add(node.module.split(".")[0])
                forbidden_hits = imported_roots & self._FORBIDDEN_MODULES
                self.assertFalse(
                    forbidden_hits,
                    f"{path.name} imports forbidden module(s): {sorted(forbidden_hits)}",
                )


if __name__ == "__main__":
    unittest.main()
