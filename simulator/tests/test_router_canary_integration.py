"""
simulator/tests/test_router_canary_integration.py — FOUNDATION-10.

End-to-end simulated canary: Binance fails repeatedly -> circuit breaker
opens -> router selects Kraken -> Kraken yields a valid tick -> cooldown
elapses -> Binance HALF_OPEN probe -> probe succeeds enough times ->
CLOSED -> Binance is primary again. Drives the real production code path
(FeedManager._try_live_via_new_router -> market_data.runtime_router ->
market_data.router) — no real network (websockets.connect is a scripted
fake) and a fully controlled clock (time.time() is patched, never real).

Note on why each Binance "failure" below needs 3 scripted connection
errors, not 1: _binance_loop has its own internal MAX_FAILURES=3 reconnect
loop, and consecutive_failures resets to 0 the moment a connection opens
successfully. A connect()-level failure (never opens) is what actually
accumulates that internal counter — see
simulator/tests/test_router_failure_feedback.py::make_connect_mock's
docstring for the same distinction. So one _try_live_via_new_router() call
= one internal give-up cycle = exactly one router-level provider_failure().
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, override_settings

from market_data.contracts import ProviderHealthState
from market_data.feeds import FeedManager
from market_data.runtime_router.state import get_circuit_breaker_state, reset_router_state

from .test_router_failure_feedback import make_connect_mock

BINANCE_TICK = json.dumps({"stream": "btcusdt@bookTicker", "data": {"b": "82000.10", "a": "82000.50"}})
KRAKEN_TICK = json.dumps([340, {"b": ["82000.1", "1", "1.0"], "a": ["82000.5", "1", "1.0"]}, "ticker", "XBT/USD"])


def _run(coro):
    return asyncio.run(coro)


@override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
class CanaryFailoverRecoveryIntegrationTests(SimpleTestCase):
    def setUp(self):
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

    def _binance_fails_once_at_router_level(self, now: int) -> None:
        """One _try_live_via_new_router() call where Binance's own internal
        3-attempt reconnect loop exhausts and gives up — exactly one
        router-level provider_failure()."""
        self._clock = now
        boom = RuntimeError(f"binance down at t={now}")
        connect_mock = make_connect_mock([boom, boom, boom])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

    def test_full_canary_binance_fails_kraken_takes_over_binance_recovers(self):
        # ── Step 1: Binance fails 3 times at the router level -> circuit opens ──
        self._binance_fails_once_at_router_level(now=0)
        self.assertEqual(
            get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.CLOSED,
        )
        self._binance_fails_once_at_router_level(now=1)
        self.assertEqual(
            get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.CLOSED,
        )
        self._binance_fails_once_at_router_level(now=2)
        binance_state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(binance_state.health, ProviderHealthState.OPEN)
        self.assertEqual(binance_state.consecutive_failures, 3)
        opened_at = binance_state.opened_at
        self.assertEqual(opened_at, 2)

        # ── Step 2: next selection must pick Kraken (Binance OPEN, within cooldown) ──
        self._clock = 3
        connect_mock = make_connect_mock([[KRAKEN_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        self.assertEqual(get_circuit_breaker_state("BTCUSD", "kraken").health, ProviderHealthState.CLOSED)
        # Binance must still be OPEN — a successful Kraken session must never
        # touch Binance's own breaker state.
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)

        # ── Step 3: cooldown elapses (crypto default = 10s from opened_at=2) ──
        # First HALF_OPEN probe succeeds once — crypto requires 2 successes to close.
        self._clock = 13  # 13 - 2 = 11 >= 10
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        probe_state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(probe_state.health, ProviderHealthState.HALF_OPEN)
        self.assertEqual(probe_state.half_open_successes, 1)

        # ── Step 4: second successful probe closes the breaker ──
        self._clock = 14
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        recovered_state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(recovered_state.health, ProviderHealthState.CLOSED)
        self.assertEqual(recovered_state.consecutive_failures, 0)

        # ── Step 5: Binance is primary again — a plain healthy selection, not a probe ──
        self._clock = 15
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))
        connect_mock.assert_called_once()  # single clean connection — no failover, no probing needed

    def test_single_half_open_success_is_not_enough_to_close(self):
        # Explicit regression guard for the task's own caution: don't
        # falsify "recovered" from one lucky tick when the policy requires two.
        for t in (0, 1, 2):
            self._binance_fails_once_at_router_level(now=t)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)

        self._clock = 13
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))

        state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state.health, ProviderHealthState.HALF_OPEN)
        self.assertNotEqual(state.health, ProviderHealthState.CLOSED)  # one success != recovered
