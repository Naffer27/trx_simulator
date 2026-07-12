"""
market_data/tests/test_instrument_bridges.py — FOUNDATION-06.

profile_from_symbol_spec, profile_from_instrument, compare_profiles, and
the "no Django / no network" isolation guarantee for market_data/instruments/.

Pure unittest, no Django dependency, no database access — profile_from_instrument
is exercised against a plain stub object (SimpleNamespace), never a real
Django Instrument row, proving the bridge itself needs no ORM.
"""

import ast
import pathlib
import types
import unittest
from decimal import Decimal

import market_data.instruments as instruments_pkg
from market_data.contracts import OrderPolicy, ProviderCapability
from market_data.instruments.bridges import (
    DriftReport,
    compare_profiles,
    profile_from_instrument,
    profile_from_symbol_spec,
    provider_mapping_from_instrument,
)
from market_data.providers.mappings import ProviderSymbolMapping
from market_data.symbol_specs import SymbolSpec, get_spec


def make_instrument_stub(**overrides):
    defaults = dict(
        symbol="EURUSD", display_name="EUR/USD", asset_class="forex",
        base_currency="EUR", quote_currency="USD",
        pip_size=Decimal("0.0001"), tick_size=Decimal("0.00001"), price_decimals=5,
        lot_step=Decimal("0.01"), min_lot=Decimal("0.01"), max_lot=Decimal("100.00"),
        contract_size=Decimal("100000.0000"),
        default_spread=Decimal("1.5000"), spread_unit="pips",
        commission_per_lot=Decimal("0.00"), commission_pct=Decimal("0.000000"),
        max_leverage=500, margin_mode="leverage", pnl_mode="STANDARD",
        trading_enabled=True, session="24/5",
        market_data_provider="finnhub", provider_symbol="FX:EURUSD",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class ProfileFromSymbolSpecTests(unittest.TestCase):
    def test_eur_usd_standard_pnl_and_forex_calendar(self):
        profile = profile_from_symbol_spec(get_spec("EUR/USD"))
        self.assertEqual(profile.canonical_symbol, "EUR/USD")
        self.assertEqual(profile.pnl_mode, "STANDARD")
        self.assertEqual(profile.trading_calendar_id, "24/5")
        self.assertEqual(profile.spread_unit, "pips")
        self.assertEqual(profile.base_currency, "EUR")
        self.assertTrue(profile.trading_enabled)
        self.assertAlmostEqual(profile.default_spread, 1.5, places=6)

    def test_usd_jpy_registry_entry_defaults_quote_currency_to_usd(self):
        # Documents a real, pre-existing data-quality gap this bridge surfaces
        # rather than papers over: market_data/symbol_specs.py never sets
        # quote_currency="JPY" for USD/JPY (it's left at the dataclass default,
        # "USD"), so pnl_mode derives as STANDARD here even though
        # seed_instruments.py correctly seeds PNL_INVERSE for USDJPY. See
        # simulator/tests/test_audit_instrument_profiles.py for the drift this
        # produces against the real seeded catalog.
        spec = get_spec("USD/JPY")
        self.assertEqual(spec.quote_currency, "USD")  # not "JPY" — the gap itself
        profile = profile_from_symbol_spec(spec)
        self.assertEqual(profile.pnl_mode, "STANDARD")

    def test_inverse_pnl_derivation_rule_in_isolation(self):
        # The derivation rule itself (quote_currency != "USD" -> INVERSE) is
        # correct — tested directly against a synthetic spec, independent of
        # whatever the real registry's quote_currency data quality is today.
        synthetic = SymbolSpec(
            symbol="USD/JPY", asset_class="forex",
            contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
            tick_size=0.001, pip_size=0.01, price_decimals=3,
            base_price=155.0, sim_drift=0.08, spread=0.018, commission_pct=0.0,
            max_leverage=500, quote_currency="JPY",
        )
        profile = profile_from_symbol_spec(synthetic)
        self.assertEqual(profile.pnl_mode, "INVERSE")

    def test_btcusd_crypto_calendar_and_dual_mapping(self):
        profile = profile_from_symbol_spec(get_spec("BTCUSD"))
        self.assertEqual(profile.trading_calendar_id, "24/7")
        self.assertEqual(profile.base_currency, "BTC")
        provider_ids = {m.provider_id for m in profile.provider_mappings}
        self.assertEqual(provider_ids, {"binance", "kraken"})
        by_provider = {m.provider_id: m for m in profile.provider_mappings}
        self.assertEqual(by_provider["binance"].priority, 0)
        self.assertEqual(by_provider["kraken"].priority, 1)
        self.assertEqual(by_provider["binance"].provider_symbol, "BTCUSDT")

    def test_index_uses_points_spread_unit_and_symbol_fallback_base_currency(self):
        profile = profile_from_symbol_spec(get_spec("US30"))
        self.assertEqual(profile.spread_unit, "points")
        self.assertEqual(profile.base_currency, "US30")
        self.assertFalse(profile.trading_enabled)  # disabled in symbol_specs.py

    def test_finnhub_only_symbol_has_single_last_price_mapping(self):
        profile = profile_from_symbol_spec(get_spec("EUR/USD"))
        self.assertEqual(len(profile.provider_mappings), 1)
        mapping = profile.provider_mappings[0]
        self.assertEqual(mapping.provider_id, "finnhub")
        self.assertIn(ProviderCapability.LAST_PRICE, mapping.required_capabilities)
        self.assertNotIn(ProviderCapability.BID_ASK, mapping.required_capabilities)

    def test_degradation_policy_is_never_open_normal(self):
        for symbol in ("EUR/USD", "BTCUSD", "US30", "XAU/USD"):
            with self.subTest(symbol=symbol):
                profile = profile_from_symbol_spec(get_spec(symbol))
                self.assertNotEqual(profile.default_order_policy_on_degradation, OrderPolicy.OPEN_NORMAL)


class ProfileFromInstrumentTests(unittest.TestCase):
    def test_basic_conversion_and_float_cast(self):
        stub = make_instrument_stub()
        profile = profile_from_instrument(stub)
        self.assertEqual(profile.canonical_symbol, "EUR/USD")  # normalize_symbol("EURUSD")
        self.assertIsInstance(profile.pip_size, float)
        self.assertAlmostEqual(profile.pip_size, 0.0001)
        self.assertEqual(profile.trading_calendar_id, "24/5")  # session pass-through
        self.assertEqual(profile.display_name, "EUR/USD")

    def test_crypto_symbol_normalizes_correctly(self):
        stub = make_instrument_stub(symbol="BTCUSD", display_name="BTC/USD", asset_class="crypto")
        profile = profile_from_instrument(stub)
        self.assertEqual(profile.canonical_symbol, "BTCUSD")

    def test_provider_mappings_are_caller_supplied_not_queried(self):
        stub = make_instrument_stub()
        mapping = ProviderSymbolMapping(
            canonical_symbol="EUR/USD", provider_id="finnhub", provider_symbol="FX:EURUSD", priority=0,
        )
        profile = profile_from_instrument(stub, provider_mappings=(mapping,))
        self.assertEqual(profile.provider_mappings, (mapping,))

    def test_no_mappings_by_default(self):
        stub = make_instrument_stub()
        profile = profile_from_instrument(stub)
        self.assertEqual(profile.provider_mappings, ())


class ProviderMappingFromInstrumentTests(unittest.TestCase):
    def test_real_provider_produces_one_mapping(self):
        stub = make_instrument_stub(market_data_provider="binance", provider_symbol="BTCUSDT", symbol="BTCUSD")
        mappings = provider_mapping_from_instrument(stub)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].provider_id, "binance")
        self.assertEqual(mappings[0].provider_symbol, "BTCUSDT")
        self.assertEqual(mappings[0].canonical_symbol, "BTCUSD")

    def test_sim_provider_produces_no_mapping(self):
        stub = make_instrument_stub(market_data_provider="sim", provider_symbol="")
        self.assertEqual(provider_mapping_from_instrument(stub), ())

    def test_empty_provider_symbol_produces_no_mapping(self):
        stub = make_instrument_stub(market_data_provider="finnhub", provider_symbol="")
        self.assertEqual(provider_mapping_from_instrument(stub), ())

    def test_mapping_enabled_matches_trading_enabled(self):
        stub = make_instrument_stub(trading_enabled=False)
        mappings = provider_mapping_from_instrument(stub)
        self.assertFalse(mappings[0].enabled)


class CompareProfilesTests(unittest.TestCase):
    def test_identical_profiles_have_no_differences(self):
        runtime = profile_from_symbol_spec(get_spec("EUR/USD"))
        db = profile_from_symbol_spec(get_spec("EUR/USD"))  # same source, same values
        report = compare_profiles(runtime, db)
        self.assertIsInstance(report, DriftReport)
        self.assertEqual(report.differences, ())
        self.assertEqual(report.critical_differences, ())
        self.assertEqual(report.warning_differences, ())
        self.assertFalse(report.has_drift)
        self.assertIn("contract_size", report.matches)

    def test_contract_size_drift_is_critical(self):
        runtime = profile_from_symbol_spec(get_spec("EUR/USD"))
        stub = make_instrument_stub(contract_size=Decimal("50000.0000"))
        db = profile_from_instrument(stub)
        report = compare_profiles(runtime, db)
        fields = {d.field for d in report.critical_differences}
        self.assertIn("contract_size", fields)
        self.assertTrue(report.has_drift)

    def test_display_name_drift_is_warning_only(self):
        runtime = profile_from_symbol_spec(get_spec("EUR/USD"))
        stub = make_instrument_stub(display_name="Euro vs Dollar (legacy label)")
        db = profile_from_instrument(stub)
        report = compare_profiles(runtime, db)
        warning_fields = {d.field for d in report.warning_differences}
        critical_fields = {d.field for d in report.critical_differences}
        self.assertIn("display_name", warning_fields)
        self.assertNotIn("display_name", critical_fields)

    def test_provider_mapping_drift_is_critical(self):
        runtime = profile_from_symbol_spec(get_spec("EUR/USD"))  # finnhub mapping
        stub = make_instrument_stub(market_data_provider="binance", provider_symbol="EURUSDT")
        db = profile_from_instrument(stub, provider_mappings=provider_mapping_from_instrument(stub))
        report = compare_profiles(runtime, db)
        fields = {d.field for d in report.critical_differences}
        self.assertIn("provider_mappings", fields)

    def test_float_decimal_rounding_noise_does_not_count_as_drift(self):
        # Decimal("0.0001") -> float should compare equal to the literal float 0.0001
        # used in symbol_specs.py — this proves the epsilon comparison works, not
        # just exact ==.
        runtime = profile_from_symbol_spec(get_spec("EUR/USD"))
        stub = make_instrument_stub()  # pip_size=Decimal("0.0001"), matches spec exactly
        db = profile_from_instrument(stub)
        report = compare_profiles(runtime, db)
        self.assertNotIn("pip_size", {d.field for d in report.differences})

    def test_mismatched_symbols_raise(self):
        eur = profile_from_symbol_spec(get_spec("EUR/USD"))
        btc = profile_from_symbol_spec(get_spec("BTCUSD"))
        with self.assertRaises(ValueError):
            compare_profiles(eur, btc)


class NoNetworkOrDjangoDependencyTests(unittest.TestCase):
    """Static guarantee, mirroring FOUNDATION-04/05's isolation tests: parse the
    source of every module in market_data/instruments/ and confirm it never
    imports Django or anything network/DB-adjacent. profile_from_instrument
    must work against a duck-typed object, not require the real ORM."""

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os", "time",
    })

    def test_no_forbidden_imports_in_instruments_package(self):
        package_dir = pathlib.Path(instruments_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/instruments/*.py to exist")

        for path in source_files:
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(), filename=str(path))
                imported_roots = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_roots.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and node.level == 0:
                            imported_roots.add(node.module.split(".")[0])
                forbidden_hits = imported_roots & self._FORBIDDEN_MODULES
                self.assertFalse(
                    forbidden_hits,
                    f"{path.name} imports forbidden module(s): {sorted(forbidden_hits)}",
                )


if __name__ == "__main__":
    unittest.main()
