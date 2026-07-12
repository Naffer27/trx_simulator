"""
market_data/tests/test_runtime_router.py — FOUNDATION-09 (updated FOUNDATION-10).

select_runtime_provider() and RuntimeSelectionResult. Pure unittest, no
Django dependency — this service has zero runtime wiring itself (that
wiring lives in market_data/feeds.py, tested separately in
simulator/tests/test_feeds_router_integration.py and
simulator/tests/test_router_failure_feedback.py).

Persistent circuit breaker state (market_data/runtime_router/state.py) is
covered in market_data/tests/test_runtime_router_state.py.
"""

import ast
import pathlib
import unittest
from unittest.mock import patch

import market_data.providers  # noqa: F401 — triggers binance/kraken/finnhub registration
import market_data.runtime_router as runtime_router_pkg
from market_data.contracts import SourceState
from market_data.router.models import ReasonCode
from market_data.runtime_router.models import RuntimeSelectionResult
from market_data.runtime_router.service import select_runtime_provider
from market_data.runtime_router.state import reset_router_state


class SelectRuntimeProviderTests(unittest.TestCase):
    def setUp(self):
        # FOUNDATION-10: select_runtime_provider() now reads a process-wide
        # router singleton — reset it so tests never see state left over
        # from another test (in this file or elsewhere in the same run).
        reset_router_state()

    def test_btcusd_selects_binance(self):
        result = select_runtime_provider("BTCUSD", now=0)
        self.assertIsInstance(result, RuntimeSelectionResult)
        self.assertEqual(result.symbol, "BTCUSD")
        self.assertEqual(result.selected_provider_id, "binance")
        self.assertEqual(result.selected_provider_symbol, "BTCUSDT")
        self.assertEqual(result.source_state, SourceState.LIVE)
        self.assertEqual(result.reason_code, ReasonCode.PRIMARY_SELECTED)
        self.assertTrue(result.used_new_router)
        self.assertFalse(result.fallback_to_legacy)
        self.assertIsNone(result.error_code)

    def test_eur_usd_selects_finnhub(self):
        result = select_runtime_provider("EUR/USD", now=0)
        self.assertEqual(result.selected_provider_id, "finnhub")

    def test_simulation_only_symbol_selects_no_provider(self):
        result = select_runtime_provider("SOLUSD", now=0)  # disabled, no provider fields
        self.assertIsNone(result.selected_provider_id)
        self.assertTrue(result.used_new_router)   # router ran fine, this is a valid outcome
        self.assertFalse(result.fallback_to_legacy)
        self.assertEqual(result.source_state, SourceState.SIMULATION)

    def test_unknown_symbol_falls_back_to_legacy(self):
        result = select_runtime_provider("NOT_A_REAL_SYMBOL", now=0)
        self.assertTrue(result.fallback_to_legacy)
        self.assertFalse(result.used_new_router)
        self.assertIsNotNone(result.error_code)
        self.assertIn("unknown_symbol", result.error_code)
        self.assertIsNone(result.selected_provider_id)

    def test_build_failure_falls_back_to_legacy_not_raised(self):
        with patch(
            "market_data.runtime_router.service.build_plan_for_symbol",
            side_effect=RuntimeError("forced failure for this test"),
        ):
            result = select_runtime_provider("BTCUSD", now=0)  # must not raise
        self.assertTrue(result.fallback_to_legacy)
        self.assertIsNotNone(result.error_code)
        self.assertIn("route_plan_build_failed", result.error_code)

    def test_totally_unexpected_failure_still_never_raises(self):
        with patch(
            "market_data.runtime_router.service.get_spec",
            side_effect=RuntimeError("something completely unexpected"),
        ):
            result = select_runtime_provider("BTCUSD", now=0)  # must not raise
        self.assertTrue(result.fallback_to_legacy)
        self.assertIn("unexpected_error", result.error_code)

    def test_works_without_an_explicit_now(self):
        # now defaults to the real clock internally (not exposed on the
        # result — RuntimeSelectionResult has no evaluated_at field) — this
        # just proves the call succeeds and still produces a real decision.
        result = select_runtime_provider("BTCUSD")
        self.assertEqual(result.selected_provider_id, "binance")


class RuntimeSelectionResultTests(unittest.TestCase):
    def test_empty_symbol_rejected(self):
        with self.assertRaises(ValueError):
            RuntimeSelectionResult(
                symbol="", selected_provider_id=None, selected_provider_symbol=None,
                source_state=None, reason_code=None, used_new_router=False,
                fallback_to_legacy=True,
            )


class NoNetworkOrDbOrDjangoDependencyTests(unittest.TestCase):
    """Static guarantee for market_data/runtime_router/: no network, no DB,
    no Django. Mirrors market_data/shadow/'s isolation test."""

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os",
    })

    def test_no_forbidden_imports_in_runtime_router_package(self):
        package_dir = pathlib.Path(runtime_router_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/runtime_router/*.py to exist")

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
        package_dir = pathlib.Path(runtime_router_pkg.__file__).parent
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
