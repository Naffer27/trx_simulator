"""
market_data/tests/test_observability.py — FOUNDATION-13.

Pure unittest, no Django dependency. Covers market_data/observability/'s
contracts (models.py), per-process store (store.py), and snapshot
assembly (service.py) — circuit breaker reads never mutate, staleness
uses a controlled clock, symbol isolation, and the never-raises boundary.
"""

import ast
import pathlib
import unittest

import market_data.observability as observability_pkg
from market_data.contracts import OrderPolicy, ProviderHealthState, SourceState
from market_data.observability import CatalogDriftLevel, CircuitBreakerView, MarketDataHealthSnapshot
from market_data.observability.service import build_snapshot
from market_data.observability.store import (
    get_symbol_state,
    record_first_tick,
    record_selection,
    record_session_state,
    record_terminal_failure,
    record_tick,
    reset_observability_state,
)
from market_data.router.models import ReasonCode
from market_data.runtime_router.state import reset_router_state
from market_data.sessions import MarketSessionState


class _ObservabilityIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        reset_observability_state()
        reset_router_state()


class ModelValidationTests(unittest.TestCase):
    def test_snapshot_requires_symbol(self):
        with self.assertRaises(ValueError):
            MarketDataHealthSnapshot(canonical_symbol="")

    def test_snapshot_rejects_negative_failover_count(self):
        with self.assertRaises(ValueError):
            MarketDataHealthSnapshot(canonical_symbol="BTCUSD", failover_count=-1)

    def test_circuit_breaker_view_requires_provider_id(self):
        with self.assertRaises(ValueError):
            CircuitBreakerView(
                provider_id="", health=ProviderHealthState.CLOSED, consecutive_failures=0,
            )

    def test_catalog_drift_level_has_expected_members(self):
        for member in ("NOT_CHECKED", "NO_DATA", "MATCH", "WARNING", "CRITICAL", "UNAVAILABLE"):
            self.assertIn(member, CatalogDriftLevel.__members__)


class StoreTickTests(_ObservabilityIsolatedTestCase):
    def test_no_data_symbol_has_no_last_tick(self):
        self.assertIsNone(get_symbol_state("BTCUSD").last_tick_at)

    def test_record_tick_sets_last_tick_at(self):
        record_tick("BTCUSD", now=100.0)
        self.assertEqual(get_symbol_state("BTCUSD").last_tick_at, 100.0)

    def test_record_tick_updates_on_each_call(self):
        record_tick("BTCUSD", now=100.0)
        record_tick("BTCUSD", now=105.0)
        self.assertEqual(get_symbol_state("BTCUSD").last_tick_at, 105.0)

    def test_symbols_are_isolated(self):
        record_tick("BTCUSD", now=100.0)
        self.assertIsNone(get_symbol_state("ETHUSD").last_tick_at)

    def test_record_tick_never_raises_on_internal_error(self):
        from unittest.mock import patch
        with patch("market_data.observability.store.time.time", side_effect=RuntimeError("boom")):
            record_tick("BTCUSD")  # must not raise
        # now= was not supplied and time.time() blew up — no crash, no write.


class StoreSelectionTests(_ObservabilityIsolatedTestCase):
    def test_first_selection_is_not_a_failover(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
        )
        state = get_symbol_state("BTCUSD")
        self.assertEqual(state.active_provider_id, "binance")
        self.assertEqual(state.failover_count, 0)
        self.assertIsNone(state.last_failover_at)

    def test_provider_change_counts_as_failover(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
        )
        record_selection(
            "BTCUSD", provider_id="kraken", provider_symbol="XBT/USD",
            source_state=SourceState.SECONDARY, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=True, reason_code=ReasonCode.SECONDARY_SELECTED, now=2.0,
        )
        state = get_symbol_state("BTCUSD")
        self.assertEqual(state.failover_count, 1)
        self.assertEqual(state.last_failover_at, 2.0)
        self.assertEqual(state.active_provider_id, "kraken")

    def test_repeated_same_provider_is_not_a_failover(self):
        for _ in range(3):
            record_selection(
                "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
                source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
                degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
            )
        self.assertEqual(get_symbol_state("BTCUSD").failover_count, 0)

    def test_simulation_source_state_marks_degraded(self):
        record_selection(
            "BTCUSD", provider_id=None, provider_symbol=None,
            source_state=SourceState.SIMULATION, order_policy=OrderPolicy.CLOSE_ONLY,
            degraded=True, reason_code=ReasonCode.SIMULATION_FALLBACK, now=1.0,
        )
        self.assertTrue(get_symbol_state("BTCUSD").degraded)

    def test_btcusd_and_ethusd_selection_are_isolated(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
        )
        self.assertIsNone(get_symbol_state("ETHUSD").active_provider_id)


class StoreFailureTests(_ObservabilityIsolatedTestCase):
    def test_terminal_failure_sets_error_code(self):
        record_terminal_failure("BTCUSD", "binance", error_code="RuntimeError('boom')")
        self.assertEqual(get_symbol_state("BTCUSD").last_error_code, "RuntimeError('boom')")

    def test_first_tick_never_raises(self):
        record_first_tick("BTCUSD", "binance")  # just must not raise


class StoreSessionTests(_ObservabilityIsolatedTestCase):
    def test_first_session_record_is_not_a_transition(self):
        with self.assertNoLogs("simulator.ws", level="INFO"):
            record_session_state("BTCUSD", MarketSessionState.OPEN)

    def test_session_change_logs_a_transition(self):
        record_session_state("BTCUSD", MarketSessionState.OPEN)
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_session_state("BTCUSD", MarketSessionState.WEEKEND)
        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_observability_session_transition", joined)
        self.assertIn("from_state=OPEN", joined)
        self.assertIn("to_state=WEEKEND", joined)


class BuildSnapshotTests(_ObservabilityIsolatedTestCase):
    def test_initial_snapshot_has_no_data(self):
        snap = build_snapshot("BTCUSD", now=100.0)
        self.assertIsNone(snap.active_provider_id)
        self.assertIsNone(snap.last_tick_at)
        self.assertIsNone(snap.tick_age_seconds)
        self.assertFalse(snap.stale)
        self.assertEqual(snap.failover_count, 0)
        self.assertEqual(snap.evaluated_at, 100.0)

    def test_snapshot_reflects_recorded_selection(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
        )
        snap = build_snapshot("BTCUSD", now=2.0)
        self.assertEqual(snap.active_provider_id, "binance")
        self.assertEqual(snap.source_state, SourceState.LIVE)
        self.assertEqual(snap.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertFalse(snap.degraded)

    def test_stale_after_threshold_exceeded(self):
        record_tick("BTCUSD", now=0.0)
        snap = build_snapshot("BTCUSD", stale_after_seconds=60.0, now=61.0)
        self.assertTrue(snap.stale)
        self.assertEqual(snap.tick_age_seconds, 61.0)

    def test_not_stale_within_threshold(self):
        record_tick("BTCUSD", now=0.0)
        snap = build_snapshot("BTCUSD", stale_after_seconds=60.0, now=30.0)
        self.assertFalse(snap.stale)

    def test_recovers_after_a_fresh_tick(self):
        record_tick("BTCUSD", now=0.0)
        self.assertTrue(build_snapshot("BTCUSD", stale_after_seconds=60.0, now=200.0).stale)
        record_tick("BTCUSD", now=205.0)
        self.assertFalse(build_snapshot("BTCUSD", stale_after_seconds=60.0, now=206.0).stale)

    def test_synthetic_true_only_for_simulation_source_state(self):
        record_selection(
            "BTCUSD", provider_id=None, provider_symbol=None,
            source_state=SourceState.SIMULATION, order_policy=OrderPolicy.CLOSE_ONLY,
            degraded=True, reason_code=ReasonCode.SIMULATION_FALLBACK, now=1.0,
        )
        self.assertTrue(build_snapshot("BTCUSD", now=2.0).synthetic)

    def test_live_source_state_is_not_synthetic(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED, now=1.0,
        )
        self.assertFalse(build_snapshot("BTCUSD", now=2.0).synthetic)

    def test_catalog_drift_level_is_passed_through(self):
        snap = build_snapshot("BTCUSD", catalog_drift_level=CatalogDriftLevel.CRITICAL, now=1.0)
        self.assertEqual(snap.catalog_drift_level, CatalogDriftLevel.CRITICAL)

    def test_router_flags_are_passed_through(self):
        snap = build_snapshot("BTCUSD", router_enabled=True, router_allowlisted=True, now=1.0)
        self.assertTrue(snap.router_enabled)
        self.assertTrue(snap.router_allowlisted)

    def test_market_session_is_always_freshly_computed(self):
        # BTCUSD is CRYPTO_24_7 — always OPEN regardless of when this runs.
        snap = build_snapshot("BTCUSD", now=1.0)
        self.assertEqual(snap.market_session_state, MarketSessionState.OPEN)

    def test_circuit_states_visible_for_btcusd(self):
        snap = build_snapshot("BTCUSD", now=1.0)
        provider_ids = {c.provider_id for c in snap.circuit_states}
        self.assertIn("binance", provider_ids)
        self.assertIn("kraken", provider_ids)
        for view in snap.circuit_states:
            self.assertEqual(view.health, ProviderHealthState.CLOSED)

    def test_reading_circuit_states_does_not_mutate_the_breaker(self):
        from market_data.runtime_router.state import get_circuit_breaker_state
        before = get_circuit_breaker_state("BTCUSD", "binance")
        build_snapshot("BTCUSD", now=1.0)
        build_snapshot("BTCUSD", now=2.0)
        after = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(before, after)

    def test_error_code_from_recorded_terminal_failure(self):
        record_terminal_failure("BTCUSD", "binance", error_code="RuntimeError('boom')")
        snap = build_snapshot("BTCUSD", now=1.0)
        self.assertEqual(snap.error_code, "RuntimeError('boom')")

    def test_never_raises_on_internal_error(self):
        from unittest.mock import patch
        with patch("market_data.observability.service.get_symbol_state", side_effect=RuntimeError("boom")):
            snap = build_snapshot("BTCUSD", now=1.0)  # must not raise
        self.assertIsNotNone(snap.error_code)
        self.assertIn("snapshot_build_failed", snap.error_code)

    def test_unknown_symbol_degrades_instead_of_raising(self):
        snap = build_snapshot("NOT_A_REAL_SYMBOL", now=1.0)  # must not raise
        self.assertEqual(snap.canonical_symbol, "NOT_A_REAL_SYMBOL")
        self.assertEqual(snap.circuit_states, ())


class NoNetworkOrDbOrDjangoDependencyTests(unittest.TestCase):
    """Static guarantee for market_data/observability/: no network, no DB,
    no Django, no simulator — mirrors every other market_data/* isolation
    test (pattern set by FOUNDATION-12's market_data/catalog/)."""

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os", "simulator",
    })

    def test_no_forbidden_imports_in_observability_package(self):
        package_dir = pathlib.Path(observability_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/observability/*.py to exist")

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
