"""
simulator/tests/test_router_failure_feedback.py — FOUNDATION-10.

Covers the on_first_tick/on_terminal_failure feedback hooks added to
FeedManager._binance_loop/_kraken_loop/_finnhub_loop: fires exactly once
per session, never for asyncio.CancelledError, and a bug in the callback
itself never crashes the real feed. No real network — websockets.connect
is replaced with a small scripted fake, and asyncio.sleep is mocked so
these tests don't actually wait through the real 3s reconnect backoff.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from market_data.feeds import FeedManager


class ScriptedWebSocket:
    """Fake async context manager + async iterator standing in for a real
    websockets connection. `script` is a list of items yielded in order;
    an item that's an exception instance is raised instead of yielded."""

    def __init__(self, script):
        self._script = list(script)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._script:
            raise StopAsyncIteration
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, data):
        pass


def make_connect_mock(attempts):
    """
    attempts: list with one entry per reconnect attempt (one per call to
    websockets.connect()). An entry that is a bare BaseException instance
    simulates the connection itself failing (raised directly from
    connect(), before any message is ever read — this is what actually
    makes _binance_loop's own consecutive_failures counter accumulate
    across attempts, since that counter resets to 0 the moment `async
    with websockets.connect(...) as ws:` succeeds). A list entry is a
    successful connect whose messages/trailing-exception are yielded from
    async iteration instead.
    """
    iterator = iter(attempts)

    def _connect(*args, **kwargs):
        attempt = next(iterator)
        if isinstance(attempt, BaseException):
            raise attempt
        return ScriptedWebSocket(attempt)

    return MagicMock(side_effect=_connect)


BINANCE_TICK = json.dumps({"stream": "btcusdt@bookTicker", "data": {"b": "82000.10", "a": "82000.50"}})
KRAKEN_TICK = json.dumps([340, {"b": ["82000.1", "1", "1.0"], "a": ["82000.5", "1", "1.0"]}, "ticker", "XBT/USD"])
FINNHUB_TICK = json.dumps({"type": "trade", "data": [{"p": 82000.3, "t": 1700000000000, "v": 0.01}]})


def _run(coro):
    return asyncio.run(coro)


class FeedbackHookTestsBase(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        self._write_cache_patch = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        self._write_cache_patch.start()
        self.addCleanup(self._write_cache_patch.stop)
        self._sleep_patch = patch("market_data.feeds.asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)


class BinanceFeedbackTests(FeedbackHookTestsBase):
    def test_first_tick_triggers_on_first_tick_once_not_per_tick(self):
        on_first_tick = MagicMock()
        on_terminal_failure = MagicMock()
        connect_mock = make_connect_mock([
            [BINANCE_TICK, BINANCE_TICK, BINANCE_TICK, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._binance_loop(
                    "BTCUSD", "BTCUSDT", self.channel_layer,
                    on_first_tick=on_first_tick, on_terminal_failure=on_terminal_failure,
                ))

        self.assertEqual(on_first_tick.call_count, 1)
        on_terminal_failure.assert_not_called()

    def test_terminal_failure_after_max_reconnect_attempts(self):
        on_first_tick = MagicMock()
        on_terminal_failure = MagicMock()
        boom = RuntimeError("connection refused")
        connect_mock = make_connect_mock([boom, boom, boom])  # MAX_FAILURES=3
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError):
                _run(self.fm._binance_loop(
                    "BTCUSD", "BTCUSDT", self.channel_layer,
                    on_first_tick=on_first_tick, on_terminal_failure=on_terminal_failure,
                ))

        on_first_tick.assert_not_called()
        on_terminal_failure.assert_called_once()
        self.assertIs(on_terminal_failure.call_args.args[0], boom)

    def test_cancelled_error_on_first_attempt_never_reports_failure(self):
        on_first_tick = MagicMock()
        on_terminal_failure = MagicMock()
        connect_mock = make_connect_mock([[asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._binance_loop(
                    "BTCUSD", "BTCUSDT", self.channel_layer,
                    on_first_tick=on_first_tick, on_terminal_failure=on_terminal_failure,
                ))

        on_first_tick.assert_not_called()
        on_terminal_failure.assert_not_called()

    def test_callback_exception_does_not_crash_the_feed(self):
        on_first_tick = MagicMock(side_effect=RuntimeError("bug in feedback recording"))
        connect_mock = make_connect_mock([
            [BINANCE_TICK, BINANCE_TICK, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):  # not the callback's RuntimeError
                _run(self.fm._binance_loop(
                    "BTCUSD", "BTCUSDT", self.channel_layer, on_first_tick=on_first_tick,
                ))

        self.assertEqual(on_first_tick.call_count, 1)  # still only called once, despite raising
        self.assertEqual(self.channel_layer.group_send.await_count, 2)  # both ticks still broadcast

    def test_no_hooks_behaves_exactly_as_before(self):
        connect_mock = make_connect_mock([[BINANCE_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._binance_loop("BTCUSD", "BTCUSDT", self.channel_layer))
        self.assertEqual(self.channel_layer.group_send.await_count, 1)


class KrakenFeedbackTests(FeedbackHookTestsBase):
    def test_first_tick_triggers_on_first_tick_once(self):
        on_first_tick = MagicMock()
        connect_mock = make_connect_mock([[KRAKEN_TICK, KRAKEN_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._kraken_loop(
                    "BTCUSD", "XBT/USD", self.channel_layer, on_first_tick=on_first_tick,
                ))
        self.assertEqual(on_first_tick.call_count, 1)

    def test_terminal_failure_after_max_reconnect_attempts(self):
        on_terminal_failure = MagicMock()
        boom = RuntimeError("kraken down")
        connect_mock = make_connect_mock([boom, boom, boom])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError):
                _run(self.fm._kraken_loop(
                    "BTCUSD", "XBT/USD", self.channel_layer, on_terminal_failure=on_terminal_failure,
                ))
        on_terminal_failure.assert_called_once()


class FinnhubFeedbackTests(FeedbackHookTestsBase):
    def test_first_tick_triggers_on_first_tick_once(self):
        on_first_tick = MagicMock()
        connect_mock = make_connect_mock([[FINNHUB_TICK, FINNHUB_TICK, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch("market_data.feeds.FINNHUB_API_KEY", "fake-key-for-test"):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._finnhub_loop(
                    "EUR/USD", self.channel_layer, on_first_tick=on_first_tick,
                ))
        self.assertEqual(on_first_tick.call_count, 1)

    def test_terminal_failure_after_max_reconnect_attempts(self):
        on_terminal_failure = MagicMock()
        boom = RuntimeError("finnhub down")
        connect_mock = make_connect_mock([boom, boom, boom])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch("market_data.feeds.FINNHUB_API_KEY", "fake-key-for-test"):
            with self.assertRaises(RuntimeError):
                _run(self.fm._finnhub_loop(
                    "EUR/USD", self.channel_layer, on_terminal_failure=on_terminal_failure,
                ))
        on_terminal_failure.assert_called_once()
