"""
market_data/tests/test_shadow.py — FOUNDATION-08.

evaluate_shadow_route(), legacy_expected_provider(), and ShadowResult.
Pure unittest, no Django dependency — the shadow service itself has zero
runtime wiring (that wiring lives in market_data/feeds.py, tested
separately in simulator/tests/test_feeds_shadow_integration.py).
"""

import ast
import pathlib
import unittest
from unittest.mock import patch

import market_data.providers  # noqa: F401 — triggers binance/kraken/finnhub registration
import market_data.shadow as shadow_pkg
from market_data.contracts import OrderPolicy, SourceState
from market_data.router.models import ReasonCode
from market_data.shadow.models import ShadowResult
from market_data.shadow.service import evaluate_shadow_route, legacy_expected_provider
from market_data.symbol_specs import SymbolSpec, get_spec


class LegacyExpectedProviderTests(unittest.TestCase):
    def test_exchange_symbol_present_expects_binance(self):
        self.assertEqual(legacy_expected_provider(get_spec("BTCUSD")), "binance")

    def test_no_exchange_symbol_but_kraken_expects_kraken(self):
        synthetic = SymbolSpec(
            symbol="TEST", asset_class="crypto", contract_size=1.0, min_lot=0.01, max_lot=10.0,
            lot_step=0.01, tick_size=0.01, pip_size=1.0, price_decimals=2, base_price=1.0,
            sim_drift=0.1, spread=1.0, commission_pct=0.0, max_leverage=10,
            kraken_symbol="TEST/USD",
        )
        self.assertEqual(legacy_expected_provider(synthetic), "kraken")

    @patch("market_data.shadow.service._FINNHUB_API_KEY", "fake-key-for-test")
    def test_forex_with_finnhub_key_expects_finnhub(self):
        self.assertEqual(legacy_expected_provider(get_spec("EUR/USD")), "finnhub")

    @patch("market_data.shadow.service._FINNHUB_API_KEY", "")
    def test_forex_without_finnhub_key_expects_none(self):
        self.assertIsNone(legacy_expected_provider(get_spec("EUR/USD")))

    @patch("market_data.shadow.service._FINNHUB_API_KEY", "fake-key-for-test")
    def test_no_slash_symbol_does_not_qualify_for_finnhub(self):
        synthetic = SymbolSpec(
            symbol="NOSLASH", asset_class="index", contract_size=1.0, min_lot=0.1, max_lot=10.0,
            lot_step=0.1, tick_size=1.0, pip_size=1.0, price_decimals=0, base_price=100.0,
            sim_drift=1.0, spread=1.0, commission_pct=0.0, max_leverage=10,
            finnhub_symbol="SOME:SYMBOL",
        )
        self.assertIsNone(legacy_expected_provider(synthetic))

    def test_nothing_configured_expects_none(self):
        synthetic = SymbolSpec(
            symbol="NOTHING", asset_class="index", contract_size=1.0, min_lot=0.1, max_lot=10.0,
            lot_step=0.1, tick_size=1.0, pip_size=1.0, price_decimals=0, base_price=100.0,
            sim_drift=1.0, spread=1.0, commission_pct=0.0, max_leverage=10,
        )
        self.assertIsNone(legacy_expected_provider(synthetic))


class EvaluateShadowRouteTests(unittest.TestCase):
    def test_btcusd_legacy_and_shadow_agree_on_binance(self):
        result = evaluate_shadow_route("BTCUSD", now=1000)
        self.assertIsInstance(result, ShadowResult)
        self.assertEqual(result.canonical_symbol, "BTCUSD")
        self.assertEqual(result.legacy_expected_provider, "binance")
        self.assertEqual(result.shadow_selected_provider, "binance")
        self.assertTrue(result.agrees_with_legacy)
        self.assertEqual(result.shadow_source_state, SourceState.LIVE)
        self.assertEqual(result.shadow_order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertFalse(result.degraded)
        self.assertEqual(result.reason_code, ReasonCode.PRIMARY_SELECTED)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.evaluated_at, 1000)

    def test_btcusd_plan_carries_kraken_as_secondary_but_binance_selected(self):
        # Proves the plan built under the hood really has Kraken as a
        # secondary candidate, not just that Binance happens to win.
        from market_data.instruments.bridges import profile_from_symbol_spec
        from market_data.instruments.routing import build_route_plan

        plan = build_route_plan(profile_from_symbol_spec(get_spec("BTCUSD")))
        provider_ids = {e.provider_id for e in plan.entries}
        self.assertEqual(provider_ids, {"binance", "kraken"})

        result = evaluate_shadow_route("BTCUSD", now=0)
        self.assertEqual(result.shadow_selected_provider, "binance")  # primary wins when healthy

    @patch("market_data.shadow.service._FINNHUB_API_KEY", "fake-key-for-test")
    def test_eur_usd_legacy_and_shadow_agree_on_finnhub(self):
        result = evaluate_shadow_route("EUR/USD", now=0)
        self.assertEqual(result.legacy_expected_provider, "finnhub")
        self.assertEqual(result.shadow_selected_provider, "finnhub")
        self.assertTrue(result.agrees_with_legacy)

    def test_simulation_only_symbol_reports_no_selected_provider(self):
        # SOLUSD is disabled and has no provider fields at all in symbol_specs.py.
        result = evaluate_shadow_route("SOLUSD", now=0)
        self.assertIsNone(result.legacy_expected_provider)
        self.assertIsNone(result.shadow_selected_provider)
        self.assertEqual(result.shadow_source_state, SourceState.SIMULATION)
        self.assertEqual(result.reason_code, ReasonCode.SIMULATION_FALLBACK)
        self.assertTrue(result.agrees_with_legacy)  # both sides: no live provider
        self.assertIsNone(result.error_code)

    def test_unknown_symbol_is_encapsulated_as_error_not_raised(self):
        result = evaluate_shadow_route("NOT_A_REAL_SYMBOL", now=0)
        self.assertIsNotNone(result.error_code)
        self.assertIn("unknown_symbol", result.error_code)
        self.assertIsNone(result.shadow_selected_provider)
        self.assertIsNone(result.agrees_with_legacy)

    def test_build_failure_is_encapsulated_not_raised(self):
        with patch(
            "market_data.shadow.service.build_route_plan",
            side_effect=RuntimeError("forced failure for this test"),
        ):
            result = evaluate_shadow_route("BTCUSD", now=0)
        self.assertIsNotNone(result.error_code)
        self.assertIn("shadow_build_failed", result.error_code)
        self.assertIsNone(result.shadow_selected_provider)
        self.assertIsNone(result.agrees_with_legacy)
        # legacy_expected_provider was computed before the failure and is
        # still reported — the caller gets partial, honest information.
        self.assertEqual(result.legacy_expected_provider, "binance")

    def test_totally_unexpected_failure_still_never_raises(self):
        with patch(
            "market_data.shadow.service.get_spec",
            side_effect=RuntimeError("something completely unexpected"),
        ):
            result = evaluate_shadow_route("BTCUSD", now=0)  # must not raise
        self.assertIsNotNone(result.error_code)
        self.assertIn("unexpected_error", result.error_code)

    def test_default_now_uses_real_clock_when_not_supplied(self):
        import time
        before = int(time.time())
        result = evaluate_shadow_route("BTCUSD")
        after = int(time.time())
        self.assertTrue(before <= result.evaluated_at <= after)


class ShadowResultTests(unittest.TestCase):
    def test_empty_canonical_symbol_rejected(self):
        with self.assertRaises(ValueError):
            ShadowResult(
                canonical_symbol="", legacy_expected_provider=None, shadow_selected_provider=None,
                shadow_source_state=None, shadow_order_policy=None, degraded=None, reason_code=None,
                agrees_with_legacy=None, evaluated_at=0,
            )


class NoNetworkOrDbOrDjangoDependencyTests(unittest.TestCase):
    """
    Static guarantee for market_data/shadow/: no network, no DB, no Django.
    `os` and `time` are intentionally NOT forbidden here (unlike contracts/
    providers/router/instruments) — this package is the deliberate boundary
    that reads FINNHUB_API_KEY (env) and defaults to a real clock so it's
    usable as a real runtime service, not a pure computation library.
    """

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio",
    })

    def test_no_forbidden_imports_in_shadow_package(self):
        package_dir = pathlib.Path(shadow_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/shadow/*.py to exist")

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

    def test_does_not_import_market_data_feeds(self):
        # The dependency must point the other way (feeds.py -> shadow),
        # never shadow -> feeds — that would be a circular import. Checks
        # actual import statements (AST), not prose — the module docstring
        # and comments legitimately reference "market_data/feeds.py" and
        # "market_data.feeds" in explanatory text.
        package_dir = pathlib.Path(shadow_pkg.__file__).parent
        for path in sorted(package_dir.glob("*.py")):
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(), filename=str(path))
                imported_modules = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported_modules.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        imported_modules.add(node.module)
                self.assertFalse(
                    any(m == "market_data.feeds" or m.startswith("market_data.feeds.") for m in imported_modules),
                    f"{path.name} imports market_data.feeds — would create a circular import",
                )


if __name__ == "__main__":
    unittest.main()
