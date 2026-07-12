"""
market_data/tests/test_catalog.py — FOUNDATION-12.

get_runtime_instrument(), compare_runtime_instrument(), CatalogSource.
Pure unittest, no Django dependency — market_data/catalog/ has zero
runtime wiring itself (the DB-aware drift-check orchestration lives in
simulator/runtime_instrument_catalog.py, tested separately in
simulator/tests/test_runtime_instrument_catalog.py).
"""

import ast
import pathlib
import unittest

import market_data.catalog as catalog_pkg
from market_data.catalog import (
    ACTIVE_CATALOG_SOURCE,
    CatalogSource,
    compare_runtime_instrument,
    get_runtime_instrument,
)
from market_data.instruments.bridges import DriftReport, profile_from_symbol_spec
from market_data.instruments.profiles import InstrumentProfile
from market_data.symbol_specs import get_spec


class GetRuntimeInstrumentTests(unittest.TestCase):
    def test_returns_the_same_profile_the_existing_bridge_would(self):
        # get_runtime_instrument is meant to be a transparent facade — it
        # must not compute anything the existing FOUNDATION-06 bridge
        # doesn't already compute.
        via_catalog = get_runtime_instrument("BTCUSD")
        via_bridge_directly = profile_from_symbol_spec(get_spec("BTCUSD"))
        self.assertEqual(via_catalog, via_bridge_directly)

    def test_returns_an_instrument_profile(self):
        result = get_runtime_instrument("EUR/USD")
        self.assertIsInstance(result, InstrumentProfile)
        self.assertEqual(result.canonical_symbol, "EUR/USD")

    def test_unknown_symbol_raises_keyerror_same_as_get_spec(self):
        # Deliberately NOT swallowed — see module docstring in
        # market_data/catalog/service.py for why this differs from the
        # "never raises" boundary services (shadow, runtime_router).
        with self.assertRaises(KeyError):
            get_runtime_instrument("NOT_A_REAL_SYMBOL")

    def test_every_registered_symbol_resolves(self):
        from market_data.symbol_specs import get_all_specs
        for spec in get_all_specs():
            with self.subTest(symbol=spec.symbol):
                profile = get_runtime_instrument(spec.symbol)
                self.assertEqual(profile.canonical_symbol, spec.symbol)


class CatalogSourceTests(unittest.TestCase):
    def test_active_source_is_symbol_spec_today(self):
        # The explicit, discoverable statement this whole block exists to
        # make swappable later — see market_data/catalog/service.py.
        self.assertEqual(ACTIVE_CATALOG_SOURCE, CatalogSource.SYMBOL_SPEC)

    def test_instrument_profile_source_exists_but_is_not_active(self):
        self.assertIn(CatalogSource.INSTRUMENT_PROFILE, CatalogSource)
        self.assertNotEqual(ACTIVE_CATALOG_SOURCE, CatalogSource.INSTRUMENT_PROFILE)


class CompareRuntimeInstrumentTests(unittest.TestCase):
    def test_identical_alternate_profile_has_no_differences(self):
        alternate = profile_from_symbol_spec(get_spec("EUR/USD"))  # same source, same values
        report = compare_runtime_instrument("EUR/USD", alternate)
        self.assertIsInstance(report, DriftReport)
        self.assertEqual(report.differences, ())
        self.assertFalse(report.has_drift)

    def test_detects_a_critical_difference(self):
        import dataclasses
        alternate = dataclasses.replace(
            profile_from_symbol_spec(get_spec("EUR/USD")), contract_size=1.0,
        )
        report = compare_runtime_instrument("EUR/USD", alternate)
        fields = {d.field for d in report.critical_differences}
        self.assertIn("contract_size", fields)

    def test_detects_a_warning_only_difference(self):
        import dataclasses
        alternate = dataclasses.replace(
            profile_from_symbol_spec(get_spec("EUR/USD")), display_name="Euro/Dollar (legacy)",
        )
        report = compare_runtime_instrument("EUR/USD", alternate)
        self.assertEqual(report.critical_differences, ())
        fields = {d.field for d in report.warning_differences}
        self.assertIn("display_name", fields)

    def test_unknown_symbol_raises_keyerror(self):
        alternate = profile_from_symbol_spec(get_spec("EUR/USD"))
        with self.assertRaises(KeyError):
            compare_runtime_instrument("NOT_A_REAL_SYMBOL", alternate)

    def test_mismatched_symbol_alternate_raises_valueerror(self):
        alternate = profile_from_symbol_spec(get_spec("BTCUSD"))  # different symbol than requested below
        with self.assertRaises(ValueError):
            compare_runtime_instrument("EUR/USD", alternate)


class NoNetworkOrDbOrDjangoDependencyTests(unittest.TestCase):
    """Static guarantee for market_data/catalog/: no network, no DB, no
    Django, and — unlike every other market_data/* isolation test so far —
    no `simulator` either. The DB-aware half of this Foundation lives in
    simulator/runtime_instrument_catalog.py specifically so this package
    can stay a leaf market_data/* module."""

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os", "simulator",
    })

    def test_no_forbidden_imports_in_catalog_package(self):
        package_dir = pathlib.Path(catalog_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/catalog/*.py to exist")

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
