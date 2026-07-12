"""
simulator/tests/test_feeds_observability_integration.py — FOUNDATION-13.

Drives the real integration points added to market_data/feeds.py::
FeedManager._broadcast() / _try_live_via_new_router() and verifies:
  - the MARKET_DATA_OBSERVABILITY_ENABLED flag gate (off = zero recording)
  - tick freshness, selection, first-tick, terminal-failure, and failover
    recording at those exact points
  - a broken observability call never affects the feed it's observing
  - symbols outside the router allowlist stay on the legacy path (no
    selection recorded) while still getting tick freshness from the same
    shared _broadcast() hook every loop already goes through
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, override_settings

from market_data.contracts import SourceState
from market_data.feeds import FeedManager
from market_data.observability import get_symbol_state, reset_observability_state
from market_data.runtime_router.state import reset_router_state
from market_data.sessions import MarketSessionState

from .test_router_failure_feedback import BINANCE_TICK, KRAKEN_TICK, make_connect_mock


def _run(coro):
    return asyncio.run(coro)


class _ObservabilityIntegrationTestCase(SimpleTestCase):
    def setUp(self):
        reset_observability_state()
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()

        patcher = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

        sleep_patcher = patch("market_data.feeds.asyncio.sleep", new=AsyncMock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        self._clock = 0
        for target in ("market_data.runtime_router.service.time.time", "market_data.runtime_router.state.time.time"):
            p = patch(target, side_effect=lambda: self._clock)
            p.start()
            self.addCleanup(p.stop)


class FlagGateTests(_ObservabilityIntegrationTestCase):
    @override_settings(MARKET_DATA_OBSERVABILITY_ENABLED=False)
    def test_flag_off_broadcast_records_nothing(self):
        _run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0))
        self.assertIsNone(get_symbol_state("EUR/USD").last_tick_at)

    @override_settings(MARKET_DATA_OBSERVABILITY_ENABLED=True)
    def test_flag_on_broadcast_records_a_tick(self):
        _run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0))
        self.assertIsNotNone(get_symbol_state("EUR/USD").last_tick_at)

    def test_settings_default_to_observability_disabled(self):
        _run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0))
        self.assertIsNone(get_symbol_state("EUR/USD").last_tick_at)


@override_settings(
    MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}),
    MARKET_DATA_OBSERVABILITY_ENABLED=True,
)
class SelectionRecordingTests(_ObservabilityIntegrationTestCase):
    def test_successful_selection_is_recorded(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        state = get_symbol_state("BTCUSD")
        self.assertEqual(state.active_provider_id, "binance")
        self.assertEqual(state.source_state, SourceState.LIVE)
        self.assertFalse(state.degraded)

    def test_first_tick_and_broadcast_tick_are_both_recorded(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        self.assertIsNotNone(get_symbol_state("BTCUSD").last_tick_at)

    def test_session_state_is_recorded(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        self.assertEqual(get_symbol_state("BTCUSD").last_session_state, MarketSessionState.OPEN)

    def test_failover_across_two_selections_increments_the_counter(self):
        # Selection 1: Binance connects fine.
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))
        self.assertEqual(get_symbol_state("BTCUSD").failover_count, 0)

        # Force Binance's circuit open (3 connect-level failures), then the
        # next selection must land on Kraken — a real failover.
        for now in (1, 2, 3):
            self._clock = now
            boom = RuntimeError(f"binance down at t={now}")
            failing_connect = make_connect_mock([boom, boom, boom])
            with patch("market_data.feeds.websockets.connect", failing_connect):
                with self.assertRaises(RuntimeError):
                    _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        self._clock = 4
        kraken_connect = make_connect_mock([[KRAKEN_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", kraken_connect):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        state = get_symbol_state("BTCUSD")
        self.assertEqual(state.active_provider_id, "kraken")
        self.assertEqual(state.failover_count, 1)
        self.assertEqual(state.last_failover_at, 4)

    def test_terminal_failure_records_error_code(self):
        boom = RuntimeError("binance down")
        failing_connect = make_connect_mock([boom, boom, boom])
        with patch("market_data.feeds.websockets.connect", failing_connect):
            with self.assertRaises(RuntimeError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        error_code = get_symbol_state("BTCUSD").last_error_code
        self.assertIsNotNone(error_code)
        self.assertIn("binance down", error_code)


@override_settings(
    MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}),
    MARKET_DATA_OBSERVABILITY_ENABLED=True,
)
class MonitorFailureIsolationTests(_ObservabilityIntegrationTestCase):
    """Ante error del monitor, el runtime debe continuar — REGLA CRÍTICA."""

    def test_broken_record_tick_does_not_break_broadcast(self):
        with patch("market_data.observability.record_tick", side_effect=RuntimeError("monitor boom")):
            _run(self.fm._broadcast("BTCUSD", self.channel_layer, 82000.0, 82000.5, 0))  # must not raise
        self.channel_layer.group_send.assert_called_once()

    def test_broken_record_selection_does_not_break_router_dispatch(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.observability.record_selection", side_effect=RuntimeError("monitor boom")):
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))
        # The real router-state selection still happened despite the monitor blowing up.
        from market_data.runtime_router.state import get_circuit_breaker_state
        self.assertIsNotNone(get_circuit_breaker_state("BTCUSD", "binance"))

    def test_broken_record_first_tick_does_not_break_the_loop(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.observability.record_first_tick", side_effect=RuntimeError("monitor boom")):
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))
        self.channel_layer.group_send.assert_called()


@override_settings(
    MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}),
    MARKET_DATA_OBSERVABILITY_ENABLED=True,
)
class SymbolsOutsideAllowlistStayLegacyTests(_ObservabilityIntegrationTestCase):
    def test_non_allowlisted_symbol_never_gets_a_recorded_selection(self):
        # EUR/USD is not on MARKET_DATA_ROUTER_SYMBOLS — _try_live_via_new_router
        # must never be invoked for it (covered by FOUNDATION-09's own tests);
        # here we only assert the observability consequence: no selection state.
        _run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0))
        state = get_symbol_state("EUR/USD")
        self.assertIsNone(state.active_provider_id)
        # But tick freshness still flows through the same shared _broadcast() hook.
        self.assertIsNotNone(state.last_tick_at)

    def test_btcusd_and_ethusd_stores_stay_isolated_through_real_dispatch(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        self.assertIsNotNone(get_symbol_state("BTCUSD").active_provider_id)
        self.assertIsNone(get_symbol_state("ETHUSD").active_provider_id)
