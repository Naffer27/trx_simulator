# simulator/tests/test_fix05b3_massive_forex_live.py
"""
FIX-05B.3-B1 — Massive Forex LIVE WebSocket (EUR/USD only).

Design lock: FIX-05B.3-A. B1 scope: exactly one symbol, EUR/USD
(_MASSIVE_WS_ENABLED_SYMBOLS). GBP/USD, USD/JPY, AUD/USD stay on Finnhub
until B2. Protocol confirmed live (GOLDEN-LIVE-PROVIDER-01/FIX-05B.3-A):

  wss://socket.massive.com/forex
  auth:      {"action":"auth","params":MASSIVE_API_KEY}
             -> [{"ev":"status","status":"auth_success",...}]  (wait for
             this BEFORE subscribing — never sent blind)
  subscribe: {"action":"subscribe","params":"C.EUR/USD"}
  quote:     [{"ev":"C","p":"EUR/USD","b":bid,"a":ask,"t":ms}]

Every quote still passes through _validate_quote_values() (Capa A) before
reaching _broadcast() — the SAME single choke point Binance/Kraken/Finnhub
already use: no new state, no new Redis write path, no new frontend
contract. source="massive" needs no allowlist change anywhere (confirmed
FIX-05B.3-A §G — _read_cached_price()/the frontend tick handler only ever
reject source is None or source=="sim").

All tests mock market_data.feeds.websockets.connect — no real network in
manage.py test. Mocking shape mirrors the established pattern already used
for _finnhub_loop/_binance_loop/_kraken_loop tests (ScriptedWebSocket +
make_connect_mock in test_router_failure_feedback.py) — reimplemented
locally here (with send-call recording, which the shared helper doesn't
need for its own tests) rather than modifying that shared file.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

# Captured at import time, before any test patches asyncio.sleep — that
# patch mutates the actual global asyncio module's attribute (feeds.py
# does `import asyncio`, so `market_data.feeds.asyncio` IS this same
# object), so a reference grabbed later would just be the mock itself.
_REAL_ASYNCIO_SLEEP = asyncio.sleep

from django.test import SimpleTestCase

from market_data.feeds import (
    DATA_STALE_TIMEOUT,
    FeedManager,
    MASSIVE_API_KEY,
    _MASSIVE_ENABLED_SYMBOLS,
    _MASSIVE_WATCHDOG_POLL_SECONDS,
    _MASSIVE_WS_ENABLED_SYMBOLS,
    _PRICE_CACHE_TTL,
    _massive_ws_param,
    _parse_massive_events,
)

_FAKE_MASSIVE_KEY = "FAKEtestONLYnotREALkey1234567xx"

MASSIVE_AUTH_SUCCESS = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])
MASSIVE_AUTH_ERROR = json.dumps([{"ev": "status", "status": "error", "message": "invalid api key"}])
MASSIVE_QUOTE_EURUSD = json.dumps([{"ev": "C", "p": "EUR/USD", "b": 1.16139, "a": 1.16146, "t": 1788217269000}])
MASSIVE_QUOTE_INVALID = json.dumps([{"ev": "C", "p": "EUR/USD", "b": 2.0, "a": 1.0, "t": 1788217269000}])
MASSIVE_EVENT_OTHER = json.dumps([{"ev": "unknown", "p": "EUR/USD"}])


def _run(coro):
    return asyncio.run(coro)


class _WatchdogClosedTestError(Exception):
    """Stands in for websockets' real ConnectionClosed — the production
    code only ever does `except Exception`, so a plain Exception subclass
    is sufficient and keeps this file dependency-free of the real
    exception's constructor shape."""


class _ScriptedMassiveWS:
    """Fake async context manager + async iterator standing in for a real
    Massive WS connection, with send-call recording (the shared
    ScriptedWebSocket in test_router_failure_feedback.py doesn't record
    sends — not needed for its own tests). `script` is a list of raw
    text messages yielded in order by `async for raw in ws`; an item
    that's an exception instance is raised instead of yielded.

    FIX-05B.3-B1.1 — once `script` is exhausted, __anext__ no longer
    raises StopAsyncIteration immediately: it BLOCKS (awaiting an
    internal Event) exactly like a real socket with nothing new to
    deliver, until close() is called. `close_raises` controls which of
    the two real-world outcomes Design Lock §6 requires covering:
    True  -> the pending __anext__ wakes and raises (mirrors
             ConnectionClosed propagating out of `async for`).
    False -> the pending __anext__ wakes and raises StopAsyncIteration
             (mirrors the iterator ending cleanly, no exception).
    Every existing (pre-B1.1) script ends with an explicit exception
    item, so this change is inert for all of them — none ever reach
    exhaustion."""

    def __init__(self, script, close_raises=True):
        self._script = list(script)
        self.sent = []
        self.closed = False
        self._close_raises = close_raises
        self._close_event = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await self._close_event.wait()
        if self._close_raises:
            raise _WatchdogClosedTestError("closed by watchdog (stale data)")
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self._close_event.set()


def _make_connect_mock(attempts, close_raises=True):
    """attempts: list of scripts, one per websockets.connect() call.
    connect_mock.instances[i] is the _ScriptedMassiveWS for attempt i."""
    iterator = iter(attempts)
    instances = []

    def _connect(*args, **kwargs):
        script = next(iterator)
        ws = _ScriptedMassiveWS(script, close_raises=close_raises)
        instances.append(ws)
        return ws

    mock = MagicMock(side_effect=_connect)
    mock.instances = instances
    return mock


class _MassiveForexLiveTestsBase(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        self._write_cache_patch = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        self._write_cache_patch.start()
        self.addCleanup(self._write_cache_patch.stop)
        self._sleep_patch = patch("market_data.feeds.asyncio.sleep", new=AsyncMock())
        self._sleep_mock = self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", _FAKE_MASSIVE_KEY)
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)


# ─────────────────────────────────────────────────────────────────────────
# 1-3. auth_success / auth failure / subscribe EUR/USD
# ─────────────────────────────────────────────────────────────────────────
class AuthAndSubscribeTests(_MassiveForexLiveTestsBase):
    def test_auth_success_then_subscribe_sent_in_order(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        sent = [json.loads(s) for s in connect_mock.instances[0].sent]
        self.assertEqual(sent[0], {"action": "auth", "params": _FAKE_MASSIVE_KEY})
        self.assertEqual(sent[1], {"action": "subscribe", "params": "C.EUR/USD"})

    def test_auth_failure_raises_and_never_subscribes(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_ERROR],
            [asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        first_sent = [json.loads(s) for s in connect_mock.instances[0].sent]
        self.assertEqual(first_sent, [{"action": "auth", "params": _FAKE_MASSIVE_KEY}])
        self.assertEqual(connect_mock.call_count, 2)

    def test_subscribe_param_is_c_dot_eurusd(self):
        self.assertEqual(_massive_ws_param("EUR/USD"), "C.EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# 4-7. parse EUR/USD / bid-ask / timestamp / source
# ─────────────────────────────────────────────────────────────────────────
class QuoteParsingTests(_MassiveForexLiveTestsBase):
    def test_parse_eurusd_quote_broadcasts_normalized_values(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        mock_broadcast.assert_awaited_once_with(
            "EUR/USD", self.channel_layer, 1.16139, 1.16146, 1788217269, source="massive",
        )
        args, kwargs = mock_broadcast.await_args
        self.assertIsInstance(args[2], float)
        self.assertIsInstance(args[3], float)
        self.assertEqual(kwargs["source"], "massive")

    def test_timestamp_ms_to_seconds(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        ts = mock_broadcast.await_args.args[4]
        self.assertEqual(ts, 1788217269)


# ─────────────────────────────────────────────────────────────────────────
# 8-10. invalid quote / malformed event / non-C event
# ─────────────────────────────────────────────────────────────────────────
class RejectionTests(_MassiveForexLiveTestsBase):
    def test_invalid_quote_rejected_ask_below_bid(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_INVALID, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        mock_broadcast.assert_not_awaited()

    def test_malformed_event_ignored_without_crashing_reader(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, "not-json{{{", asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        mock_broadcast.assert_not_awaited()

    def test_non_c_event_ignored(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_EVENT_OTHER, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        mock_broadcast.assert_not_awaited()

    def test_parse_massive_events_helper_never_raises(self):
        self.assertEqual(_parse_massive_events("not-json{{{"), [])
        self.assertEqual(_parse_massive_events(json.dumps({"ev": "C"})), [{"ev": "C"}])
        self.assertEqual(_parse_massive_events(json.dumps([1, 2, "x"])), [])


# ─────────────────────────────────────────────────────────────────────────
# 11. _broadcast called exactly once
# ─────────────────────────────────────────────────────────────────────────
class BroadcastCountTests(_MassiveForexLiveTestsBase):
    def test_broadcast_called_exactly_once_for_one_valid_quote(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, MASSIVE_EVENT_OTHER, "not-json{{{",
            MASSIVE_QUOTE_EURUSD, asyncio.CancelledError(),
        ]])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(mock_broadcast.await_count, 1)


# ─────────────────────────────────────────────────────────────────────────
# 12-13. reconnect/backoff, resubscribe after reconnect
# ─────────────────────────────────────────────────────────────────────────
class ReconnectTests(_MassiveForexLiveTestsBase):
    def test_reconnect_backoff_starts_at_2_seconds(self):
        connect_mock = _make_connect_mock([
            [Exception("connection dropped")],
            [asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self._sleep_mock.assert_awaited_once_with(2.0)

    def test_backoff_doubles_and_caps_at_30(self):
        connect_mock = _make_connect_mock([
            [Exception("1")], [Exception("2")], [Exception("3")],
            [Exception("4")], [Exception("5")], [asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        waits = [c.args[0] for c in self._sleep_mock.await_args_list]
        self.assertEqual(waits, [2.0, 4.0, 8.0, 16.0, 30.0])

    def test_resubscribe_eurusd_after_reconnect(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, Exception("dropped")],
            [MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        # both attempts must independently auth + subscribe — resubscribe
        # is automatic, not a separate/forgotten code path.
        for ws in connect_mock.instances:
            sent = [json.loads(s) for s in ws.sent]
            self.assertEqual(sent[0], {"action": "auth", "params": _FAKE_MASSIVE_KEY})
        self.assertEqual(
            json.loads(connect_mock.instances[0].sent[1]),
            {"action": "subscribe", "params": "C.EUR/USD"},
        )
        self.assertEqual(
            json.loads(connect_mock.instances[1].sent[1]),
            {"action": "subscribe", "params": "C.EUR/USD"},
        )

    def test_never_gives_up_permanently_indefinite_retries(self):
        """Design Lock §I — unlike _finnhub_loop's fixed 3-attempts, this
        must still be retrying (not raised/returned) after 5 consecutive
        failures."""
        connect_mock = _make_connect_mock([
            [Exception(f"{i}")] for i in range(5)
        ] + [[asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(connect_mock.call_count, 6)


# ─────────────────────────────────────────────────────────────────────────
# 14. secret never logged
# ─────────────────────────────────────────────────────────────────────────
class SecretNotLoggedTests(_MassiveForexLiveTestsBase):
    def test_key_not_logged_on_auth_failure(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_ERROR],
            [asyncio.CancelledError()],
        ])
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        for record in cm.records:
            self.assertNotIn(_FAKE_MASSIVE_KEY, record.getMessage())

    def test_key_not_logged_on_connection_error(self):
        connect_mock = _make_connect_mock([
            [Exception("connection dropped")],
            [asyncio.CancelledError()],
        ])
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        for record in cm.records:
            self.assertNotIn(_FAKE_MASSIVE_KEY, record.getMessage())


# ─────────────────────────────────────────────────────────────────────────
# 15. Finnhub no longer primary for EUR/USD (GBP/USD unaffected)
# ─────────────────────────────────────────────────────────────────────────
class DispatchPriorityTests(_MassiveForexLiveTestsBase):
    def test_eurusd_tries_massive_not_finnhub(self):
        with patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(return_value=None)) as mock_massive, \
             patch.object(self.fm, "_finnhub_loop", new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = _run(self.fm._try_live_legacy("EUR/USD", self.channel_layer))
        self.assertTrue(result)
        mock_massive.assert_awaited_once_with("EUR/USD", self.channel_layer)

    def test_gbpusd_still_uses_finnhub_unaffected(self):
        """GBP/USD is not in _MASSIVE_WS_ENABLED_SYMBOLS (B1) — must skip
        the Massive branch entirely and go straight to Finnhub, exactly
        as before this block."""
        with patch("market_data.feeds.FINNHUB_API_KEY", "fake-finnhub-key"), \
             patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(side_effect=AssertionError("must not be called"))), \
             patch.object(self.fm, "_finnhub_loop", new=AsyncMock(return_value=None)) as mock_finnhub:
            result = _run(self.fm._try_live_legacy("GBP/USD", self.channel_layer))
        self.assertTrue(result)
        mock_finnhub.assert_awaited_once_with("GBP/USD", self.channel_layer)

    def test_massive_failure_falls_through_to_finnhub_as_secondary(self):
        """A genuinely unhandled Massive error (not its own internal
        reconnect cycle) must still let Finnhub run as a real fallback
        for EUR/USD — Finnhub is not silently made primary again, but it
        is not deleted/unreachable either (Design Lock §L, option C)."""
        with patch("market_data.feeds.FINNHUB_API_KEY", "fake-finnhub-key"), \
             patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(self.fm, "_finnhub_loop", new=AsyncMock(return_value=None)) as mock_finnhub:
            result = _run(self.fm._try_live_legacy("EUR/USD", self.channel_layer))
        self.assertTrue(result)
        mock_finnhub.assert_awaited_once_with("EUR/USD", self.channel_layer)

    def test_massive_forex_loop_returns_immediately_for_out_of_scope_symbol(self):
        connect_mock = MagicMock(side_effect=AssertionError("must not attempt a connection"))
        with patch("market_data.feeds.websockets.connect", connect_mock):
            _run(self.fm._massive_forex_loop("GBP/USD", self.channel_layer))
        connect_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 16. crypto unaffected
# ─────────────────────────────────────────────────────────────────────────
class CryptoUnaffectedTests(_MassiveForexLiveTestsBase):
    def test_btcusd_never_reaches_massive_branch(self):
        self.assertNotIn("BTCUSD", _MASSIVE_WS_ENABLED_SYMBOLS)
        with patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(side_effect=AssertionError("must not be called"))), \
             patch.object(self.fm, "_binance_loop", new=AsyncMock(return_value=None)) as mock_binance:
            result = _run(self.fm._try_live_legacy("BTCUSD", self.channel_layer))
        self.assertTrue(result)
        mock_binance.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────
# 17. Massive historical unaffected
# ─────────────────────────────────────────────────────────────────────────
class HistoricalUnaffectedTests(SimpleTestCase):
    def test_historical_and_live_symbol_allowlists_are_distinct_constants(self):
        # FIX-05B.2's 4-symbol historical allowlist is untouched by B1's
        # 1-symbol live allowlist — never merged into one constant.
        self.assertEqual(_MASSIVE_ENABLED_SYMBOLS, frozenset({"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"}))
        self.assertEqual(_MASSIVE_WS_ENABLED_SYMBOLS, frozenset({"EUR/USD"}))
        self.assertIsNot(_MASSIVE_ENABLED_SYMBOLS, _MASSIVE_WS_ENABLED_SYMBOLS)

    def test_fetch_massive_history_and_massive_forex_loop_are_separate_methods(self):
        fm = FeedManager()
        self.assertTrue(hasattr(fm, "fetch_massive_history"))
        self.assertTrue(hasattr(fm, "_massive_forex_loop"))
        self.assertIsNot(fm.fetch_massive_history, fm._massive_forex_loop)


# ═══════════════════════════════════════════════════════════════════════
# FIX-05B.3-B1.1 — Data staleness watchdog
# ═══════════════════════════════════════════════════════════════════════
#
# GOLDEN-MASSIVE-PRICECACHE-01 found the WS connection can stay
# technically alive (ping/pong healthy, no exception) while silently
# stopping delivery of "ev":"C" quote events for minutes.
# self._massive_last_quote_at[symbol] (time.monotonic()) is updated ONLY
# by a genuine, validated "ev":"C" event; a watchdog task polls it every
# ~_MASSIVE_WATCHDOG_POLL_SECONDS and, once DATA_STALE_TIMEOUT is
# exceeded, closes the connection — funneling into the SAME reconnect/
# backoff path a real connection error already uses (never a second,
# parallel reconnect mechanism).
#
# time.monotonic() is patched with a stateful fake (_fake_monotonic) that
# returns each value in the given sequence in order, then repeats the
# LAST one forever — safe against the watchdog polling far more times
# than the test explicitly scripted for (asyncio.sleep is mocked to
# return instantly in _MassiveForexLiveTestsBase, so the watchdog's own
# `while True: sleep; check` spins fast — every extra spin just re-reads
# the same "still not stale" or "still stale" value).
#
# CRITICAL: this patches market_data.feeds.time (the NAME binding used
# inside feeds.py), never time.monotonic on the real, shared `time`
# module. asyncio's own event-loop internals (BaseEventLoop.time(),
# _run_once()'s per-iteration timeout math) call the REAL time.monotonic()
# on every loop iteration — mutating that global would silently exhaust a
# short, test-scripted value sequence via asyncio's own bookkeeping calls
# (not this code's), starving the sequence before the watchdog ever reads
# its intended values and livelocking the whole event loop at 100% CPU
# (reproduced and confirmed while writing these tests). A fresh namespace
# object that proxies every other attribute (time.time() etc, still used
# elsewhere in feeds.py) to the real module keeps asyncio's own clock
# completely untouched.
import time as _real_time_module


def _fake_monotonic(values):
    it = iter(values)
    state = {"last": values[0] if values else 0.0}

    def _fn(*args, **kwargs):
        try:
            state["last"] = next(it)
        except StopIteration:
            pass
        return state["last"]

    return _fn


class _FakeTimeNamespace:
    def __init__(self, monotonic_fn):
        self.monotonic = monotonic_fn

    def __getattr__(self, name):
        return getattr(_real_time_module, name)


class _WatchdogTestsBase(_MassiveForexLiveTestsBase):
    def setUp(self):
        super().setUp()
        self._monotonic_patch = None

    def _patch_monotonic(self, values):
        self._monotonic_patch = patch(
            "market_data.feeds.time", _FakeTimeNamespace(_fake_monotonic(values))
        )
        self._monotonic_patch.start()
        self.addCleanup(self._monotonic_patch.stop)


# ─────────────────────────────────────────────────────────────────────────
# 1-3. Freshness detection — quote in time / silence past timeout / socket
#      alive with no data
# ─────────────────────────────────────────────────────────────────────────
class FreshnessDetectionTests(_WatchdogTestsBase):
    def test_quote_before_timeout_does_not_close_connection(self):
        # grace(0.0) -> quote resets to 5.0 -> watchdog checks repeatedly
        # see 10.0 -> elapsed=5.0 < DATA_STALE_TIMEOUT(20) -> never stale.
        self._patch_monotonic([0.0, 5.0, 10.0])
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertFalse(connect_mock.instances[0].closed)

    def test_silence_past_timeout_triggers_ws_close(self):
        # grace(0.0) -> no quote ever arrives -> every watchdog check sees
        # 25.0 -> elapsed=25.0 > DATA_STALE_TIMEOUT(20) -> close() called.
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],             # script exhausts -> __anext__ blocks
            [asyncio.CancelledError()],          # reconnect attempt — ends the test
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertTrue(connect_mock.instances[0].closed)

    def test_socket_alive_no_data_detected_via_log_not_ping(self):
        """(3) The staleness log line itself is the proof the watchdog
        —not a ping/pong timeout— is what detected this: no exception
        was ever raised by the connection itself before close()."""
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [asyncio.CancelledError()],
        ])
        with self.assertLogs("simulator.ws", level="WARNING") as cm:
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        joined = "\n".join(cm.output)
        self.assertIn("stale data", joined)
        self.assertIn("no valid quote for", joined)


# ─────────────────────────────────────────────────────────────────────────
# 4-6. Freshness timer resets only on a genuine valid "C" event
# ─────────────────────────────────────────────────────────────────────────
class FreshnessTimerResetTests(_WatchdogTestsBase):
    def test_auth_and_status_events_do_not_reset_freshness(self):
        fm = FeedManager()
        fm._massive_last_quote_at["EUR/USD"] = 5.0
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()]])
        self._patch_monotonic([5.0, 5.0, 5.0, 5.0, 5.0])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(fm._massive_forex_loop("EUR/USD", self.channel_layer))
        # auth_success re-seeds the grace window at connect time (5.0,
        # same as the patched clock) — it must not be some OTHER value
        # derived from the status message itself.
        self.assertEqual(fm._massive_last_quote_at["EUR/USD"], 5.0)

    def test_malformed_event_does_not_reset_freshness(self):
        fm = FeedManager()
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, "not-json{{{", asyncio.CancelledError()]])
        self._patch_monotonic([1.0, 1.0, 1.0, 1.0, 1.0])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(fm._massive_last_quote_at["EUR/USD"], 1.0)

    def test_valid_c_event_resets_freshness(self):
        fm = FeedManager()
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, asyncio.CancelledError()]])
        self._patch_monotonic([1.0, 42.0, 42.0, 42.0, 42.0])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(fm._massive_last_quote_at["EUR/USD"], 42.0)


# ─────────────────────────────────────────────────────────────────────────
# 7-10. Stale -> reconnect -> re-auth -> resubscribe -> source preserved
# ─────────────────────────────────────────────────────────────────────────
class StaleReconnectFlowTests(_WatchdogTestsBase):
    def test_stale_triggers_reconnect_second_connect_call(self):
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(connect_mock.call_count, 2)

    def test_reconnect_after_stale_sends_auth_again(self):
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        second_sent = [json.loads(s) for s in connect_mock.instances[1].sent]
        self.assertEqual(second_sent[0], {"action": "auth", "params": _FAKE_MASSIVE_KEY})

    def test_reconnect_after_stale_resubscribes_eurusd(self):
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        second_sent = [json.loads(s) for s in connect_mock.instances[1].sent]
        self.assertEqual(second_sent[1], {"action": "subscribe", "params": "C.EUR/USD"})

    def test_source_massive_preserved_after_stale_reconnect(self):
        self._patch_monotonic([0.0, 25.0, 25.0, 26.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, asyncio.CancelledError()],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        mock_broadcast.assert_awaited_once()
        self.assertEqual(mock_broadcast.await_args.kwargs["source"], "massive")


# ─────────────────────────────────────────────────────────────────────────
# 11-13. _broadcast is still the only authority / TTL untouched / no real network
# ─────────────────────────────────────────────────────────────────────────
class NoSideChannelTests(_WatchdogTestsBase):
    def test_watchdog_source_never_touches_redis_broadcast_or_db(self):
        import inspect
        import re
        from market_data.feeds import FeedManager as _FM
        src = inspect.getsource(_FM._massive_staleness_watchdog)
        # Strip the docstring first — it explicitly documents (in prose)
        # what the watchdog must NOT do, which would otherwise trip these
        # same substring checks as a false positive.
        code_only = re.sub(r'"""[\s\S]*?"""', "", src, count=1)
        for forbidden in ("_write_price_cache", "_broadcast(", "redis", "Redis", ".objects.", "save(", "commit("):
            self.assertNotIn(forbidden, code_only, f"unexpected {forbidden!r} in watchdog source")

    def test_price_cache_ttl_still_60_and_greater_than_stale_timeout(self):
        self.assertEqual(_PRICE_CACHE_TTL, 60)
        self.assertLess(DATA_STALE_TIMEOUT, _PRICE_CACHE_TTL)

    def test_no_real_network_socket_lib_used(self):
        # Structural: websockets.connect is always the patch target in
        # every test above — this asserts that symbol still exists and
        # is what _massive_forex_loop actually calls (no alternate/real
        # network path hiding elsewhere in the method).
        import inspect
        from market_data.feeds import FeedManager as _FM
        src = inspect.getsource(_FM._massive_forex_loop)
        self.assertIn("websockets.connect(", src)
        self.assertNotIn("requests.", src)
        self.assertNotIn("urllib.request.urlopen(", src)


# ─────────────────────────────────────────────────────────────────────────
# 14. Secret never logged, even during a stale-triggered reconnect
# ─────────────────────────────────────────────────────────────────────────
class StaleSecretNotLoggedTests(_WatchdogTestsBase):
    def test_key_not_logged_during_stale_reconnect(self):
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [asyncio.CancelledError()],
        ])
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("market_data.feeds.websockets.connect", connect_mock):
                with self.assertRaises(asyncio.CancelledError):
                    _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        for record in cm.records:
            self.assertNotIn(_FAKE_MASSIVE_KEY, record.getMessage())


# ─────────────────────────────────────────────────────────────────────────
# 15. CancelledError propagates cleanly, watchdog included
# ─────────────────────────────────────────────────────────────────────────
class CancellationTests(_WatchdogTestsBase):
    def test_cancelling_the_loop_cleanly_cancels_the_watchdog_too(self):
        async def _scenario():
            connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
            created = []
            real_create_task = asyncio.create_task

            def _tracking_create_task(coro, *a, **kw):
                t = real_create_task(coro, *a, **kw)
                created.append(t)
                return t

            # This test needs the watchdog task to be genuinely suspended
            # (not spinning) when the outer loop is cancelled, so its own
            # asyncio.sleep must be a REAL, tiny sleep here — the base
            # class's global instant-AsyncMock sleep (needed everywhere
            # else so reconnect backoff doesn't cost real seconds) never
            # actually yields to the event loop, which would either starve
            # cancellation delivery to the watchdog or, since real
            # time.monotonic() is left unpatched in this test, spin for a
            # genuine ~DATA_STALE_TIMEOUT seconds before self-resolving —
            # neither is what "cancel mid-poll" is meant to exercise.
            # _MASSIVE_WATCHDOG_POLL_SECONDS is also shrunk so that real
            # sleep costs ~10ms, not 5s.
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch("market_data.feeds.asyncio.create_task", side_effect=_tracking_create_task), \
                 patch("market_data.feeds.asyncio.sleep", side_effect=_REAL_ASYNCIO_SLEEP), \
                 patch("market_data.feeds._MASSIVE_WATCHDOG_POLL_SECONDS", 0.01):
                task = asyncio.ensure_future(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
                # Real sleep, longer than the watchdog's 0.01s poll, so it
                # has genuinely reached its own suspended await by the
                # time we cancel the outer task.
                await _REAL_ASYNCIO_SLEEP(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(len(created), 1)
                self.assertTrue(created[0].done())
                self.assertTrue(created[0].cancelled())

        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 16-18. crypto / Massive historical / Finnhub unaffected (regression,
#         already covered by CryptoUnaffectedTests/HistoricalUnaffectedTests/
#         DispatchPriorityTests above — this class adds the one watchdog-
#         specific angle: no watchdog is ever spawned for a non-Massive symbol).
# ─────────────────────────────────────────────────────────────────────────
class WatchdogScopeTests(_WatchdogTestsBase):
    def test_no_watchdog_spawned_for_out_of_scope_symbol(self):
        connect_mock = MagicMock(side_effect=AssertionError("must not attempt a connection"))
        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch("market_data.feeds.asyncio.create_task") as mock_create_task:
            _run(self.fm._massive_forex_loop("GBP/USD", self.channel_layer))
        mock_create_task.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 19-20. Exactly one watchdog per connection, none leaked across reconnects
# ─────────────────────────────────────────────────────────────────────────
class WatchdogTaskLifecycleTests(_WatchdogTestsBase):
    def test_exactly_one_watchdog_task_per_connection_no_duplicates(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()]])
        created = []
        real_create_task = asyncio.create_task

        def _tracking_create_task(coro, *a, **kw):
            t = real_create_task(coro, *a, **kw)
            created.append(t)
            return t

        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch("market_data.feeds.asyncio.create_task", side_effect=_tracking_create_task):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(len(created), 1)

    def test_no_watchdog_leaked_across_reconnect(self):
        self._patch_monotonic([0.0, 25.0, 25.0])
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],                              # attempt 1 -> goes stale
            [MASSIVE_AUTH_SUCCESS, asyncio.CancelledError()],     # attempt 2 -> ends the test
        ])
        created = []
        real_create_task = asyncio.create_task

        def _tracking_create_task(coro, *a, **kw):
            t = real_create_task(coro, *a, **kw)
            created.append(t)
            return t

        with patch("market_data.feeds.websockets.connect", connect_mock), \
             patch("market_data.feeds.asyncio.create_task", side_effect=_tracking_create_task):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        # one watchdog per connection attempt (2 attempts here) — both
        # must be finished (cancelled in the `finally` of their own
        # connection block), never left running in the background.
        self.assertEqual(len(created), 2)
        for t in created:
            self.assertTrue(t.done())


# ─────────────────────────────────────────────────────────────────────────
# 21-22. Design Lock §6 correction — BOTH ways a watchdog-forced close can
#         end `async for raw in ws` must reconnect, never stall/return.
# ─────────────────────────────────────────────────────────────────────────
class WatchdogCloseBothOutcomesTests(_WatchdogTestsBase):
    def test_close_then_iterator_raises_exception_still_reconnects(self):
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock(
            [[MASSIVE_AUTH_SUCCESS], [asyncio.CancelledError()]],
            close_raises=True,
        )
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(connect_mock.call_count, 2)
        self.assertTrue(connect_mock.instances[0].closed)

    def test_close_then_iterator_ends_normally_still_reconnects(self):
        """The Design Lock's own explicit correction: a clean end of the
        async iterator (no exception at all) must NOT let
        _massive_forex_loop return/stall — it must reconnect exactly
        like the exception case above."""
        self._patch_monotonic([0.0, 25.0])
        connect_mock = _make_connect_mock(
            [[MASSIVE_AUTH_SUCCESS], [asyncio.CancelledError()]],
            close_raises=False,
        )
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
        self.assertEqual(connect_mock.call_count, 2)
        self.assertTrue(connect_mock.instances[0].closed)
