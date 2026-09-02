# simulator/tests/test_fix05b3_massive_forex_live.py
"""
FIX-05B.3-B1/B1.1/B2 — Massive Forex LIVE WebSocket, shared multi-symbol
connection.

Design lock: FIX-05B.3-A (B1), FIX-05B.3-B2-A (B2). B2 scope: EUR/USD,
GBP/USD, USD/JPY, AUD/USD (_MASSIVE_WS_ENABLED_SYMBOLS) all share ONE
Massive WebSocket connection, ONE auth, ONE reader task, ONE connection
watchdog — never one connection per symbol. XAU/USD stays out of scope.
Protocol confirmed live (GOLDEN-LIVE-PROVIDER-01/FIX-05B.3-A):

  wss://socket.massive.com/forex
  auth:      {"action":"auth","params":MASSIVE_API_KEY}
             -> [{"ev":"status","status":"auth_success",...}]  (wait for
             this BEFORE subscribing — never sent blind)
  subscribe: {"action":"subscribe","params":"C.EUR/USD,C.GBP/USD,..."}
             (comma-joined batch — confirmed live, 1-connection-4-symbols)
  quote:     [{"ev":"C","p":"EUR/USD","b":bid,"a":ask,"t":ms}]

Architecture (Design Lock §C/§E): FeedManager._massive_forex_loop(symbol,
cl) is a THIN PER-SYMBOL ADAPTER — it registers *symbol* with the shared
connection (_massive_register_symbol), blocks on an asyncio.Event for as
long as Massive serves that symbol, and unregisters in `finally`. The
REAL connection lives in _massive_shared_loop (the ONE reader) plus
_massive_connection_staleness_watchdog (the ONE watchdog, checking BOTH
connection-wide and per-symbol staleness — Design Lock §8/§9).

Every quote still passes through _validate_quote_values() (Capa A) before
reaching _broadcast() — the SAME single choke point Binance/Kraken/Finnhub
already use. source="massive" needs no allowlist change anywhere.

All tests mock market_data.feeds.websockets.connect — no real network in
manage.py test. Since the shared connection/watchdog run as background
asyncio.Tasks separate from whichever coroutine a test directly awaits,
tests use _settle() (real, unmocked single-turn yields — see the
_REAL_ASYNCIO_SLEEP note below) to let them make deterministic progress,
then usually drive the FeedManager's register/unregister methods
directly rather than always going through the full per-symbol adapter —
much more direct and controllable for shared-connection-level behavior.
"""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Captured at import time, before any test patches asyncio.sleep — that
# patch mutates the actual global asyncio module's attribute (feeds.py
# does `import asyncio`, so `market_data.feeds.asyncio` IS this same
# object), so a reference grabbed later would just be the mock itself.
# FIX-05B.3-B1.1 lesson (see _FakeTimeNamespace below): never mutate a
# shared global this way when you can rebind the NAME the target module
# uses instead — asyncio.sleep is the one deliberate exception, because
# _MassiveForexLiveTestsBase's whole test suite depends on EVERY
# asyncio.sleep call (reconnect backoff, watchdog poll) resolving
# instantly; _REAL_ASYNCIO_SLEEP exists so a handful of tests that need a
# GENUINE single-turn yield (not a mocked instant no-op) can still get one.
_REAL_ASYNCIO_SLEEP = asyncio.sleep

from django.test import SimpleTestCase, override_settings

from market_data.contracts import OrderPolicy
from market_data.contracts import SourceState
from market_data.router.models import ReasonCode
from market_data.runtime_router.models import RuntimeSelectionResult
from market_data.runtime_router.state import reset_router_state
from market_data.sessions.models import CalendarId, MarketSessionResult, MarketSessionState, SessionReasonCode

from market_data.feeds import (
    DATA_STALE_TIMEOUT,
    FeedManager,
    MASSIVE_API_KEY,
    _MASSIVE_ENABLED_SYMBOLS,
    _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
    _MASSIVE_WATCHDOG_POLL_SECONDS,
    _MASSIVE_WS_ENABLED_SYMBOLS,
    _PRICE_CACHE_TTL,
    _massive_ws_param,
    _massive_ws_params_batch,
    _parse_massive_events,
)

_FAKE_MASSIVE_KEY = "FAKEtestONLYnotREALkey1234567xx"

MASSIVE_AUTH_SUCCESS = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])
MASSIVE_AUTH_ERROR = json.dumps([{"ev": "status", "status": "error", "message": "invalid api key"}])
MASSIVE_QUOTE_EURUSD = json.dumps([{"ev": "C", "p": "EUR/USD", "b": 1.16139, "a": 1.16146, "t": 1788217269000}])
MASSIVE_QUOTE_GBPUSD = json.dumps([{"ev": "C", "p": "GBP/USD", "b": 1.34120, "a": 1.34135, "t": 1788217269000}])
MASSIVE_QUOTE_USDJPY = json.dumps([{"ev": "C", "p": "USD/JPY", "b": 149.850, "a": 149.865, "t": 1788217269000}])
MASSIVE_QUOTE_AUDUSD = json.dumps([{"ev": "C", "p": "AUD/USD", "b": 0.66120, "a": 0.66135, "t": 1788217269000}])
MASSIVE_QUOTE_INVALID = json.dumps([{"ev": "C", "p": "EUR/USD", "b": 2.0, "a": 1.0, "t": 1788217269000}])
MASSIVE_EVENT_OTHER = json.dumps([{"ev": "unknown", "p": "EUR/USD"}])


def _run(coro):
    return asyncio.run(coro)


async def _instant_real_yield(*args, **kwargs):
    """side_effect for the mocked asyncio.sleep — MUST be a genuine
    `async def` (not a plain function returning a coroutine object) so
    AsyncMock recognizes it (via asyncio.iscoroutinefunction) and
    actually awaits it, rather than treating an unawaited coroutine
    object as an inert return value (confirmed while writing these
    tests: a lambda returning _REAL_ASYNCIO_SLEEP(0) produced a
    "coroutine was never awaited" warning AND never actually yielded —
    same livelock as a bare AsyncMock, just with an extra warning)."""
    await _REAL_ASYNCIO_SLEEP(0)


async def _settle(n=20):
    """Give the event loop N genuine single-turn yields (the REAL,
    unmocked asyncio.sleep(0) — see _REAL_ASYNCIO_SLEEP above) so
    background tasks created via asyncio.create_task (the shared reader,
    the connection watchdog) make deterministic progress before a test
    inspects state. Cheap — each turn is a bare yield, no real delay."""
    for _ in range(n):
        await _REAL_ASYNCIO_SLEEP(0)


class _WatchdogClosedTestError(Exception):
    """Stands in for websockets' real ConnectionClosed — the production
    code only ever does `except Exception`, so a plain Exception subclass
    is sufficient and keeps this file dependency-free of the real
    exception's constructor shape."""


class _ScriptedMassiveWS:
    """Fake async context manager + async iterator standing in for a real
    Massive WS connection, with send-call recording. `script` is a list
    of raw text messages yielded in order by `async for raw in ws`; an
    item that's an exception instance is raised instead of yielded.

    Once `script` is exhausted, __anext__ BLOCKS (awaiting an internal
    Event) exactly like a real socket with nothing new to deliver, until
    close() is called. `close_raises` controls which of the two
    real-world outcomes Design Lock §6 (B1.1) requires covering:
    True  -> the pending __anext__ wakes and raises (mirrors
             ConnectionClosed propagating out of `async for`).
    False -> the pending __anext__ wakes and raises StopAsyncIteration
             (mirrors the iterator ending cleanly, no exception)."""

    def __init__(self, script, close_raises=True):
        self._script = list(script)
        self.sent = []
        self.closed = False
        self._close_raises = close_raises
        self._close_event = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        # Mirrors real websockets.connect()'s context manager: exiting
        # the `async with` block — for ANY reason, including
        # cancellation — closes the underlying connection. Without this,
        # a cancelled/errored shared_task would leave .closed False even
        # though the connection conceptually ended (confirmed while
        # writing these tests: this exact gap made
        # test_zero_active_symbols_shuts_service_down fail).
        self.closed = True
        self._close_event.set()
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


class _FakeTimeNamespace:
    """FIX-05B.3-B1.1 — proxies every attribute except `monotonic` to the
    REAL time module, so patching `market_data.feeds.time` (the NAME
    binding feeds.py uses) to an instance of this never touches the
    real, shared `time` module — critically, never breaks asyncio's own
    event-loop internals, which call the real time.monotonic() on every
    loop iteration. Mutating the real module's `.monotonic` attribute
    directly (patch("...time.monotonic", ...)) was confirmed, while
    writing B1.1's tests, to livelock the whole event loop at 100% CPU."""

    def __init__(self, monotonic_fn):
        self.monotonic = monotonic_fn

    def __getattr__(self, name):
        import time as _real_time
        return getattr(_real_time, name)


class _ManualClock:
    """A time.monotonic() stand-in advanced EXPLICITLY by the test
    (.advance(seconds)), never implicitly by call-count. B2's multi-step
    timing (register -> connect-time grace reseed -> watchdog poll ->
    resubscribe -> second poll -> escalate) touches time.monotonic() a
    different number of times depending on how many symbols are
    registered, whether a resubscribe fired, etc. — a fixed
    call-order-dependent sequence of values (B1's approach) is too
    fragile to hand-count correctly here and, if miscounted, silently
    produces a livelock (confirmed while writing these tests — see
    _FakeTimeNamespace's own note on why a broken fake clock hangs the
    whole event loop, not just fails an assertion). A clock that only
    moves when told to is call-count-independent by construction."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self, *args, **kwargs):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _MassiveForexLiveTestsBase(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        self._write_cache_patch = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        self._write_cache_mock = self._write_cache_patch.start()
        self.addCleanup(self._write_cache_patch.stop)
        # Every asyncio.sleep(x) call anywhere in feeds.py (reconnect
        # backoff, watchdog poll interval) resolves with NO real delay
        # (side_effect ignores the requested duration) but STILL does a
        # genuine single-turn yield (via the real, unmocked
        # asyncio.sleep(0) captured as _REAL_ASYNCIO_SLEEP above) —
        # never a fully zero-yield synchronous no-op. This matters for
        # B2: a bare AsyncMock() with no side_effect resolves via a
        # single coroutine send() with no internal await at all, which
        # never hands control back to the event loop — a
        # `while True: await asyncio.sleep(...); <check condition>`
        # loop (the connection watchdog) that keeps finding "not stale"
        # would then spin the CPU forever with the SAME clock value,
        # since nothing — including the test's own driver coroutine —
        # ever gets a turn to advance a manual clock or make further
        # assertions (confirmed while writing these tests: this is
        # exactly what a bare AsyncMock produced here — a real hang).
        # A real (if zero-delay) yield keeps the watchdog's own
        # unbounded polling genuinely cooperative, interleaving fairly
        # with _settle()'s yields instead of starving them.
        self._sleep_patch = patch(
            "market_data.feeds.asyncio.sleep",
            new=AsyncMock(side_effect=_instant_real_yield),
        )
        self._sleep_mock = self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", _FAKE_MASSIVE_KEY)
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)

    def _patch_manual_clock(self, start=0.0) -> _ManualClock:
        clock = _ManualClock(start)
        p = patch("market_data.feeds.time", _FakeTimeNamespace(clock))
        p.start()
        self.addCleanup(p.stop)
        return clock


# ─────────────────────────────────────────────────────────────────────────
# 1-3. Shared connection: one connection, one auth, batch subscribe
# ─────────────────────────────────────────────────────────────────────────
class SharedConnectionTests(_MassiveForexLiveTestsBase):
    def test_one_connection_for_two_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                self.assertEqual(connect_mock.call_count, 1)
        _run(_scenario())

    def test_one_auth_message_for_two_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                auth_msgs = [
                    json.loads(s) for s in connect_mock.instances[0].sent
                    if json.loads(s).get("action") == "auth"
                ]
                self.assertEqual(len(auth_msgs), 1)
                self.assertEqual(auth_msgs[0]["params"], _FAKE_MASSIVE_KEY)
        _run(_scenario())

    def test_batch_subscribe_covers_both_active_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                sub_msgs = [
                    json.loads(s) for s in connect_mock.instances[0].sent
                    if json.loads(s).get("action") == "subscribe"
                ]
                self.assertEqual(len(sub_msgs), 1)
                self.assertEqual(sub_msgs[0]["params"], _massive_ws_params_batch({"EUR/USD", "GBP/USD"}))
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 4-7. Multi-symbol parsing
# ─────────────────────────────────────────────────────────────────────────
class MultiSymbolParsingTests(_MassiveForexLiveTestsBase):
    def _assert_symbol_broadcasts(self, symbol, quote_json, expected_bid, expected_ask):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, quote_json]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol(symbol, self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
                args = mock_broadcast.await_args
                self.assertEqual(args.args[0], symbol)
                self.assertAlmostEqual(args.args[2], expected_bid)
                self.assertAlmostEqual(args.args[3], expected_ask)
                self.assertEqual(args.kwargs["source"], "massive")
        _run(_scenario())

    def test_parse_eurusd(self):
        self._assert_symbol_broadcasts("EUR/USD", MASSIVE_QUOTE_EURUSD, 1.16139, 1.16146)

    def test_parse_gbpusd(self):
        self._assert_symbol_broadcasts("GBP/USD", MASSIVE_QUOTE_GBPUSD, 1.34120, 1.34135)

    def test_parse_usdjpy(self):
        self._assert_symbol_broadcasts("USD/JPY", MASSIVE_QUOTE_USDJPY, 149.850, 149.865)

    def test_parse_audusd(self):
        self._assert_symbol_broadcasts("AUD/USD", MASSIVE_QUOTE_AUDUSD, 0.66120, 0.66135)


# ─────────────────────────────────────────────────────────────────────────
# 8-10. Symbol isolation / no cross-broadcast / source=massive
# ─────────────────────────────────────────────────────────────────────────
class SymbolIsolationTests(_MassiveForexLiveTestsBase):
    def test_only_the_quoted_symbol_broadcasts(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
                self.assertEqual(mock_broadcast.await_args.args[0], "EUR/USD")
        _run(_scenario())

    def test_no_cross_symbol_contamination_in_bid_ask(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, MASSIVE_QUOTE_GBPUSD]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                calls = {c.args[0]: (c.args[2], c.args[3]) for c in mock_broadcast.await_args_list}
                self.assertEqual(calls["EUR/USD"], (1.16139, 1.16146))
                self.assertEqual(calls["GBP/USD"], (1.34120, 1.34135))
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 11. Redis canonical keys ×4 (via _write_price_cache, called from
#     _broadcast — same single pipeline every provider already uses)
# ─────────────────────────────────────────────────────────────────────────
class RedisKeyTests(_MassiveForexLiveTestsBase):
    def test_write_price_cache_called_per_symbol_with_source_massive(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD, MASSIVE_QUOTE_GBPUSD,
            MASSIVE_QUOTE_USDJPY, MASSIVE_QUOTE_AUDUSD,
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                for sym in ("EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"):
                    await self.fm._massive_register_symbol(sym, self.channel_layer)
                await _settle()
                written = {c.args[0]: c.args[3] for c in self._write_cache_mock.await_args_list}
                for sym in ("EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"):
                    self.assertEqual(written.get(sym), "massive")
        _run(_scenario())

    def test_price_cache_ttl_still_60(self):
        self.assertEqual(_PRICE_CACHE_TTL, 60)
        self.assertLess(DATA_STALE_TIMEOUT, _PRICE_CACHE_TTL)


# ─────────────────────────────────────────────────────────────────────────
# 12-13. Per-symbol freshness — EUR tick never refreshes GBP
# ─────────────────────────────────────────────────────────────────────────
class PerSymbolFreshnessTests(_MassiveForexLiveTestsBase):
    def test_freshness_is_independent_per_symbol(self):
        # EUR/USD establishes the connection at t=0; GBP/USD joins the
        # ALREADY-authed connection later, at t=50 — an incremental join
        # (Design Lock §C), which seeds ONLY the new symbol's grace
        # window, proving the two dict entries are genuinely independent
        # (not the same shared "last quote" value under two keys).
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                eur_seed = self.fm._massive_last_quote_at["EUR/USD"]
                clock.advance(50.0)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                gbp_seed = self.fm._massive_last_quote_at["GBP/USD"]
                self.assertEqual(eur_seed, 0.0)
                self.assertEqual(gbp_seed, 50.0)
                self.assertNotEqual(eur_seed, gbp_seed)
        _run(_scenario())

    def test_eur_tick_does_not_refresh_gbp(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                broadcast_symbols = {c.args[0] for c in mock_broadcast.await_args_list}
                self.assertNotIn("GBP/USD", broadcast_symbols)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 14-17. Symbol stale recovery: first incident -> resubscribe, only that
#         symbol, socket stays open, a valid quote resets attempts to 0
# ─────────────────────────────────────────────────────────────────────────
class SymbolStaleRecoveryTests(_MassiveForexLiveTestsBase):
    def test_first_stale_triggers_resubscribe_of_that_symbol(self):
        # A single active symbol going stale IS connection-wide staleness
        # by definition (it's the only one there is) — matches B1's
        # original single-symbol behavior exactly (§9). To exercise the
        # SYMBOL-stale path (§8) specifically, at least one sibling must
        # stay fresh: EUR/USD connects at t=0 (goes stale after); GBP/USD
        # joins fresh at t=1 (still fresh when the watchdog checks at
        # t=20.5 — elapsed 19.5 <= 20).
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                clock.advance(1.0)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                clock.advance(19.5)
                await _settle()
                self.assertEqual(self.fm._massive_symbol_stale_attempts["EUR/USD"], 1)
                self.assertEqual(self.fm._massive_symbol_stale_attempts["GBP/USD"], 0)
                self.assertFalse(connect_mock.instances[0].closed)
                sent = [json.loads(s) for s in connect_mock.instances[0].sent]
                resub = [m for m in sent if m.get("params") == "C.EUR/USD" and m["action"] in ("subscribe", "unsubscribe")]
                self.assertEqual([m["action"] for m in resub], ["subscribe", "unsubscribe", "subscribe"])
        _run(_scenario())

    def test_resubscribe_affects_only_the_stale_symbol(self):
        # EUR/USD establishes the connection at t=0 (goes stale after);
        # GBP/USD joins fresh at t=25 — only EUR/USD is individually
        # stale by the time the watchdog polls again.
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                self.assertEqual(self.fm._massive_symbol_stale_attempts.get("EUR/USD"), 1)
                self.assertEqual(self.fm._massive_symbol_stale_attempts.get("GBP/USD"), 0)
                sent = [json.loads(s) for s in connect_mock.instances[0].sent]
                gbp_resends = [m for m in sent if m.get("params") == "C.GBP/USD" and m["action"] == "unsubscribe"]
                self.assertEqual(gbp_resends, [])
        _run(_scenario())

    def test_one_symbol_stale_does_not_close_socket(self):
        # Two active symbols; only EUR/USD is stale (GBP/USD joined
        # fresh) — the shared connection must stay open (Design Lock §8:
        # symbol-stale is never connection-stale unless EVERY active
        # symbol is stale simultaneously).
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                self.assertFalse(connect_mock.instances[0].closed)
        _run(_scenario())

    def test_valid_quote_resets_attempts_to_zero(self):
        # Script deliberately includes the quote in the SAME connection
        # attempt as auth — both process synchronously within the
        # shared_task's first real step (no yield between them), so
        # seeding attempts=1 right after register() (before that task
        # has been scheduled to run at all) and settling once exercises
        # the REAL reset-on-valid-quote code path in _massive_shared_loop.
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_EURUSD]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                self.fm._massive_symbol_stale_attempts["EUR/USD"] = 1
                await _settle()
                self.assertEqual(self.fm._massive_symbol_stale_attempts["EUR/USD"], 0)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 18-20. B2-FOREX-PROVIDER-CLEANUP-01 §3/§E — Second (and later) consecutive
#         stale incidents keep resubscribing Massive; no escalation, no
#         fallback, siblings unaffected.
# ─────────────────────────────────────────────────────────────────────────
class EscalationTests(_MassiveForexLiveTestsBase):
    def test_second_consecutive_stale_keeps_resubscribing_massive(self):
        # A lone active symbol going stale is CONNECTION staleness (§9),
        # which would close the socket before a second consecutive
        # incident could ever be observed. GBP/USD stays active and
        # (via a direct freshness bump between the two windows, standing
        # in for a real tick — GBP's OWN update path is covered
        # elsewhere) fresh throughout, so EUR/USD alone accumulates
        # consecutive symbol-stale incidents.
        #
        # Reaching (and passing) the threshold no longer changes the
        # ACTION taken — every incident triggers the same resubscribe,
        # uncapped. The symbol stays registered and active the entire
        # time; only the log severity changes (not asserted here).
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        self.assertEqual(_MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD, 2)
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()  # connection established at t=0
                clock.advance(DATA_STALE_TIMEOUT + 1)
                self.fm._massive_last_quote_at["GBP/USD"] = clock.now
                await _settle()  # attempt 1 for EUR -> resubscribe, grace reset
                self.assertEqual(self.fm._massive_symbol_stale_attempts["EUR/USD"], 1)
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                clock.advance(DATA_STALE_TIMEOUT + 1)
                self.fm._massive_last_quote_at["GBP/USD"] = clock.now
                await _settle()  # attempt 2 for EUR -> at threshold, STILL resubscribes
                self.assertEqual(self.fm._massive_symbol_stale_attempts["EUR/USD"], 2)
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                clock.advance(DATA_STALE_TIMEOUT + 1)
                self.fm._massive_last_quote_at["GBP/USD"] = clock.now
                await _settle()  # attempt 3 -> past threshold, still no escalation
                self.assertEqual(self.fm._massive_symbol_stale_attempts["EUR/USD"], 3)
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
        _run(_scenario())

    def test_persistent_symbol_staleness_leaves_sibling_unaffected(self):
        # EUR/USD connects at t=0; GBP/USD joins at t=1 (fresh). Advance
        # to t=20.5: EUR is stale (elapsed 20.5 > 20) while GBP is not
        # (elapsed 19.5 <= 20) — genuinely asymmetric staleness. EUR's
        # attempts is forced to threshold-1 so this ONE watchdog check
        # crosses what used to be the escalation point — confirms it no
        # longer matters: EUR keeps getting resubscribed, GBP untouched.
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                clock.advance(1.0)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                self.fm._massive_symbol_stale_attempts["EUR/USD"] = _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD - 1
                clock.advance(19.5)
                await _settle()
                self.assertEqual(
                    self.fm._massive_symbol_stale_attempts["EUR/USD"],
                    _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
                )
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                self.assertEqual(self.fm._massive_symbol_stale_attempts.get("GBP/USD", 0), 0)
                self.assertIn("GBP/USD", self.fm._massive_active_symbols)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 21-24. Connection stale (ALL active symbols stale) -> close, reconnect,
#         re-auth, resubscribe currently-active symbols
# ─────────────────────────────────────────────────────────────────────────
class ConnectionStaleTests(_MassiveForexLiveTestsBase):
    def test_all_symbol_stale_closes_ws(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [asyncio.CancelledError()],
        ])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await _settle()
                self.assertTrue(connect_mock.instances[0].closed)
                self.assertEqual(connect_mock.call_count, 2)
        _run(_scenario())

    def test_reconnect_reauths(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS],
        ])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await _settle()
                second_auth = [
                    json.loads(s) for s in connect_mock.instances[1].sent
                    if json.loads(s).get("action") == "auth"
                ]
                self.assertEqual(len(second_auth), 1)
        _run(_scenario())

    def test_reconnect_resubscribes_currently_active_symbols(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS],
        ])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await _settle()
                second_sub = [
                    json.loads(s) for s in connect_mock.instances[1].sent
                    if json.loads(s).get("action") == "subscribe"
                ]
                self.assertEqual(len(second_sub), 1)
                self.assertEqual(second_sub[0]["params"], _massive_ws_params_batch({"EUR/USD", "GBP/USD"}))
        _run(_scenario())

    def test_reconnect_only_resubscribes_symbols_still_present(self):
        """A symbol unregistered WHILE the connection is between attempts
        (no live ws) must not reappear in the next batch subscribe —
        Design Lock §9/§11: the reconnect reads the LIVE active set."""
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [MASSIVE_AUTH_SUCCESS],
        ])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                # Between the close and the (mocked/instant) reconnect,
                # unregister GBP/USD directly on the shared bookkeeping —
                # simulates a panel closing in that narrow window.
                self.fm._massive_active_symbols.discard("GBP/USD")
                await _settle()
                second_sub = [
                    json.loads(s) for s in connect_mock.instances[1].sent
                    if json.loads(s).get("action") == "subscribe"
                ]
                self.assertEqual(second_sub[0]["params"], "C.EUR/USD")
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 25-26. Singleton invariants — one reader task, one watchdog task
# ─────────────────────────────────────────────────────────────────────────
class SingletonInvariantTests(_MassiveForexLiveTestsBase):
    def test_one_reader_task_for_multiple_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                first_task = self.fm._massive_shared_task
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                self.assertIs(self.fm._massive_shared_task, first_task)
        _run(_scenario())

    def test_one_watchdog_task_at_a_time(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                self.assertIsNotNone(self.fm._massive_connection_watchdog_task)
                self.assertFalse(self.fm._massive_connection_watchdog_task.done())
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 27-29. Subscription lifecycle — simultaneous joins, duplicate
#         registration is idempotent-safe, unsubscribe one leaves others
# ─────────────────────────────────────────────────────────────────────────
class SubscriptionLifecycleTests(_MassiveForexLiveTestsBase):
    def test_simultaneous_joins_create_exactly_one_connection(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await asyncio.gather(
                    self.fm._massive_register_symbol("EUR/USD", self.channel_layer),
                    self.fm._massive_register_symbol("GBP/USD", self.channel_layer),
                    self.fm._massive_register_symbol("USD/JPY", self.channel_layer),
                )
                await _settle()
                self.assertEqual(connect_mock.call_count, 1)
        _run(_scenario())

    def test_duplicate_registration_of_same_symbol_is_idempotent(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                sent_before = len(connect_mock.instances[0].sent)
                # A second registration of the SAME already-subscribed
                # symbol must not send a redundant subscribe.
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                self.assertEqual(len(connect_mock.instances[0].sent), sent_before)
        _run(_scenario())

    def test_unregister_one_leaves_the_other_active(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                await self.fm._massive_unregister_symbol("GBP/USD")
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                self.assertIsNotNone(self.fm._massive_shared_task)
                self.assertFalse(connect_mock.instances[0].closed)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 30. No second business registry — reuses _counts/_position_symbols,
#     never duplicates them (Design Lock §D/§5)
# ─────────────────────────────────────────────────────────────────────────
class NoSecondRegistryTests(_MassiveForexLiveTestsBase):
    def test_register_unregister_never_touch_counts_or_position_symbols(self):
        import inspect
        from market_data.feeds import FeedManager as _FM
        src = (
            inspect.getsource(_FM._massive_register_symbol)
            + inspect.getsource(_FM._massive_unregister_symbol)
        )
        for forbidden in ("self._counts", "self._position_symbols", "mark_position_symbol", "has_position_ref"):
            self.assertNotIn(forbidden, src)


# ─────────────────────────────────────────────────────────────────────────
# 31-32. Zero-active shutdown / fresh service after shutdown
# ─────────────────────────────────────────────────────────────────────────
class ZeroActiveShutdownTests(_MassiveForexLiveTestsBase):
    def test_zero_active_symbols_shuts_service_down(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                self.assertIsNotNone(self.fm._massive_shared_task)
                self.assertTrue(connect_mock.instances[0].closed is False)

                await self.fm._massive_unregister_symbol("EUR/USD")

                self.assertTrue(connect_mock.instances[0].closed)
                self.assertIsNone(self.fm._massive_shared_task)
                self.assertIsNone(self.fm._massive_connection_watchdog_task)
                self.assertIsNone(self.fm._massive_ws)
                self.assertFalse(self.fm._massive_authed)
                self.assertEqual(self.fm._massive_subscribed, set())
                self.assertEqual(self.fm._massive_active_symbols, set())
        _run(_scenario())

    def test_new_symbol_after_shutdown_creates_exactly_one_new_service(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS], [MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                await self.fm._massive_unregister_symbol("EUR/USD")

                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()

                self.assertEqual(connect_mock.call_count, 2)
                self.assertIsNotNone(self.fm._massive_shared_task)
                self.assertFalse(self.fm._massive_shared_task.done())
                second_auth = [
                    json.loads(s) for s in connect_mock.instances[1].sent
                    if json.loads(s).get("action") == "auth"
                ]
                self.assertEqual(len(second_auth), 1)
                second_sub = [
                    json.loads(s) for s in connect_mock.instances[1].sent
                    if json.loads(s).get("action") == "subscribe"
                ]
                self.assertEqual(second_sub[0]["params"], "C.GBP/USD")
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 33-34. No task leaks / clean iterator end still reconnects
# ─────────────────────────────────────────────────────────────────────────
class TaskLifecycleTests(_MassiveForexLiveTestsBase):
    def test_no_watchdog_leaked_after_shutdown(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                watchdog = self.fm._massive_connection_watchdog_task
                await self.fm._massive_unregister_symbol("EUR/USD")
                self.assertTrue(watchdog.done())
        _run(_scenario())

    def test_clean_iterator_end_still_reconnects(self):
        connect_mock = _make_connect_mock(
            [[MASSIVE_AUTH_SUCCESS], [asyncio.CancelledError()]],
            close_raises=False,
        )
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await _settle()
                self.assertEqual(connect_mock.call_count, 2)
                self.assertTrue(connect_mock.instances[0].closed)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 35-36. Connection exception / CancelledError propagate cleanly
# ─────────────────────────────────────────────────────────────────────────
class ExceptionHandlingTests(_MassiveForexLiveTestsBase):
    def test_connection_exception_triggers_reconnect(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS, RuntimeError("boom")],
            [asyncio.CancelledError()],
        ])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                self.assertEqual(connect_mock.call_count, 2)
        _run(_scenario())

    def test_adapter_cancellation_unregisters_cleanly(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                task = asyncio.ensure_future(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
                await _settle()
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertNotIn("EUR/USD", self.fm._massive_active_symbols)
                # Last symbol gone -> shared service torn down too.
                self.assertIsNone(self.fm._massive_shared_task)
        _run(_scenario())

    def test_adapter_survives_persistent_staleness_never_raises(self):
        # B2-FOREX-PROVIDER-CLEANUP-01 §3/§4 — there is no event left to
        # set and no escalation path left to trigger: even many
        # consecutive stale incidents for ONE symbol must never make its
        # per-symbol adapter task complete or raise. It only ever ends
        # via outside cancellation (covered by
        # test_adapter_cancellation_unregisters_cleanly above).
        #
        # GBP/USD is registered alongside EUR/USD and kept fresh (direct
        # timestamp bump, standing in for a real tick) purely so EUR/USD
        # going stale is SYMBOL staleness, not CONNECTION staleness
        # (§9 — a lone active symbol going stale would close/reconnect
        # instead, resetting attempts to 0 each time).
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                task = asyncio.ensure_future(self.fm._massive_forex_loop("EUR/USD", self.channel_layer))
                await self.fm._massive_register_symbol("GBP/USD", self.channel_layer)
                await _settle()
                for _ in range(5):
                    clock.advance(DATA_STALE_TIMEOUT + 1)
                    self.fm._massive_last_quote_at["GBP/USD"] = clock.now
                    await _settle()
                self.assertGreaterEqual(
                    self.fm._massive_symbol_stale_attempts["EUR/USD"],
                    _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
                )
                self.assertFalse(task.done())
                self.assertIn("EUR/USD", self.fm._massive_active_symbols)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertNotIn("EUR/USD", self.fm._massive_active_symbols)
                self.assertIn("GBP/USD", self.fm._massive_active_symbols)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 37-38. Malformed events ignored / auth+status never count as freshness
# ─────────────────────────────────────────────────────────────────────────
class RejectionTests(_MassiveForexLiveTestsBase):
    def test_malformed_event_does_not_crash_reader(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, "not-json{{{", MASSIVE_QUOTE_EURUSD]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
        _run(_scenario())

    def test_invalid_quote_rejected(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_QUOTE_INVALID]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_not_awaited()
        _run(_scenario())

    def test_auth_and_status_do_not_advance_freshness_past_grace_seed(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        self._patch_manual_clock(start=5.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                await _settle()
                self.assertEqual(self.fm._massive_last_quote_at["EUR/USD"], 5.0)
        _run(_scenario())

    def test_parse_massive_events_helper_never_raises(self):
        self.assertEqual(_parse_massive_events("not-json{{{"), [])
        self.assertEqual(_parse_massive_events("null"), [])
        self.assertEqual(_parse_massive_events(MASSIVE_QUOTE_EURUSD), json.loads(MASSIVE_QUOTE_EURUSD))


# ─────────────────────────────────────────────────────────────────────────
# 39-41. Crypto / historical / Finnhub code unaffected
# ─────────────────────────────────────────────────────────────────────────
class CryptoUnaffectedTests(_MassiveForexLiveTestsBase):
    def test_btcusd_never_registers_with_massive(self):
        async def _scenario():
            await self.fm._massive_forex_loop("BTCUSD", self.channel_layer)
            self.assertNotIn("BTCUSD", self.fm._massive_active_symbols)
            self.assertIsNone(self.fm._massive_shared_task)
        _run(_scenario())


class HistoricalUnaffectedTests(_MassiveForexLiveTestsBase):
    def test_historical_and_live_symbol_allowlists_are_distinct_constants(self):
        self.assertIsNot(_MASSIVE_ENABLED_SYMBOLS, _MASSIVE_WS_ENABLED_SYMBOLS)

    def test_fetch_massive_history_and_massive_forex_loop_are_separate_methods(self):
        fm = FeedManager()
        self.assertTrue(hasattr(fm, "fetch_massive_history"))
        self.assertTrue(hasattr(fm, "_massive_forex_loop"))
        self.assertIsNot(fm.fetch_massive_history, fm._massive_forex_loop)


class DispatchPriorityTests(_MassiveForexLiveTestsBase):
    def test_all_4_pairs_use_massive_exclusively(self):
        # B2-FOREX-PROVIDER-CLEANUP-01 — Massive is the SOLE runtime
        # provider for these 4 pairs; there is no "before Finnhub"
        # ordering left to test, only "never Finnhub".
        for sym in ("EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"):
            self.assertIn(sym, _MASSIVE_WS_ENABLED_SYMBOLS)

    def test_massive_failure_never_falls_through_to_finnhub(self):
        async def _scenario():
            with patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(side_effect=RuntimeError("boom"))), \
                 patch.object(self.fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub:
                result = await self.fm._try_live_legacy("EUR/USD", self.channel_layer)
                self.assertFalse(result)
                mock_finnhub.assert_not_called()
        _run(_scenario())

    def test_finnhub_loop_body_untouched_but_unreachable_for_forex(self):
        # _finnhub_loop() itself is not modified by this block (still a
        # real, working function — used by non-Forex/dormant paths, see
        # the Design Lock's H) — it is simply never dispatched to for
        # any of the 4 active Forex pairs anymore (asserted directly in
        # test_massive_failure_never_falls_through_to_finnhub above).
        import inspect
        from market_data.feeds import FeedManager as _FM
        src = inspect.getsource(_FM._finnhub_loop)
        self.assertNotIn("_massive_", src)


def _make_open_forex_session(symbol="EUR/USD"):
    return MarketSessionResult(
        canonical_symbol=symbol, calendar_id=CalendarId.FOREX_24_5,
        state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
        evaluated_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        reason_code=SessionReasonCode.MARKET_OPEN, timezone="UTC",
    )


def _make_finnhub_decision(symbol="EUR/USD"):
    return RuntimeSelectionResult(
        symbol=symbol, selected_provider_id="finnhub", selected_provider_symbol=f"FX:{symbol.replace('/', '')}",
        source_state=SourceState.LIVE, reason_code=ReasonCode.PRIMARY_SELECTED,
        used_new_router=True, fallback_to_legacy=False, error_code=None,
    )


# ─────────────────────────────────────────────────────────────────────────
# 44-46. B2-FOREX-PROVIDER-CLEANUP-01 §7 — dormant new-router regression
#         guard: even if MARKET_DATA_ROUTER_ENABLED is ever turned on for
#         one of the 4 active Forex pairs, a "finnhub" selection must
#         never be dispatched — fail closed instead. Non-Forex symbols
#         (out of this block's scope) are unaffected.
# ─────────────────────────────────────────────────────────────────────────
class ForexRouterGuardTests(SimpleTestCase):
    def setUp(self):
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"EUR/USD"}))
    def test_finnhub_selection_for_massive_symbol_raises_never_dispatches(self):
        async def _scenario():
            with patch(
                "market_data.sessions.service.evaluate_market_session_for_symbol",
                return_value=_make_open_forex_session("EUR/USD"),
            ), patch(
                "market_data.runtime_router.service.select_runtime_provider",
                return_value=_make_finnhub_decision("EUR/USD"),
            ), patch.object(self.fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub, \
                 patch.object(self.fm, "_massive_forex_loop", new=AsyncMock()) as mock_massive:
                with self.assertRaises(RuntimeError):
                    await self.fm._try_live_via_new_router("EUR/USD", self.channel_layer)
                mock_finnhub.assert_not_called()
                mock_massive.assert_not_called()
        _run(_scenario())

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"EUR/USD"}))
    def test_finnhub_selection_for_massive_symbol_falls_back_to_legacy_never_finnhub(self):
        """End-to-end through _try_live(): the raise above is caught by
        _try_live's own except-and-fall-through to _try_live_legacy,
        which (per this same block) never runs Finnhub for this symbol
        either — the overall result is zero Finnhub calls regardless of
        which layer is exercised."""
        async def _scenario():
            with patch(
                "market_data.sessions.service.evaluate_market_session_for_symbol",
                return_value=_make_open_forex_session("EUR/USD"),
            ), patch(
                "market_data.runtime_router.service.select_runtime_provider",
                return_value=_make_finnhub_decision("EUR/USD"),
            ), patch.object(self.fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub, \
                 patch.object(self.fm, "_massive_forex_loop", new=AsyncMock(side_effect=RuntimeError("boom"))):
                result = await self.fm._try_live("EUR/USD", self.channel_layer)
                self.assertFalse(result)
                mock_finnhub.assert_not_called()
        _run(_scenario())

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_finnhub_selection_for_non_forex_symbol_unaffected(self):
        """The guard is scoped to _MASSIVE_WS_ENABLED_SYMBOLS only —
        crypto/other symbols are out of this block's scope and still
        dispatch to Finnhub via this dormant path exactly as before."""
        async def _scenario():
            with patch(
                "market_data.runtime_router.service.select_runtime_provider",
                return_value=_make_finnhub_decision("BTCUSD"),
            ), patch.object(self.fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub:
                result = await self.fm._try_live_via_new_router("BTCUSD", self.channel_layer)
                self.assertTrue(result)
                mock_finnhub.assert_called_once()
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# 42-43. No secrets logged / no real network
# ─────────────────────────────────────────────────────────────────────────
class SecretNotLoggedTests(_MassiveForexLiveTestsBase):
    def test_key_not_logged_on_auth_failure(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_ERROR], [asyncio.CancelledError()]])
        async def _scenario():
            with self.assertLogs("simulator.ws", level="DEBUG") as cm:
                with patch("market_data.feeds.websockets.connect", connect_mock):
                    await self.fm._massive_register_symbol("EUR/USD", self.channel_layer)
                    await _settle()
            for record in cm.records:
                self.assertNotIn(_FAKE_MASSIVE_KEY, record.getMessage())
        _run(_scenario())


class NoRealNetworkTests(_MassiveForexLiveTestsBase):
    def test_shared_loop_only_ever_calls_the_mocked_connect(self):
        import inspect
        from market_data.feeds import FeedManager as _FM
        src = inspect.getsource(_FM._massive_shared_loop)
        self.assertIn("websockets.connect(", src)
        self.assertNotIn("requests.", src)
        self.assertNotIn("urllib.request.urlopen(", src)
