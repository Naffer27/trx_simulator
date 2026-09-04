# simulator/tests/test_golden_marketdata_crypto_01_massive_crypto_live.py
"""
GOLDEN-MARKETDATA-CRYPTO-01 — Massive Crypto (BTCUSD/ETHUSD): historical
REST, live shared WebSocket, REST last-trade resync, dispatch priority,
and real-time candle aggregation in consumers.py.

Design lock: GOLDEN_MARKETDATA_CRYPTO_01_DESIGN_LOCK.md, Option A
(approved) — a deliberate structural duplicate of the certified
FIX-05B.2/FIX-05B.3-B2-A Forex-Massive pattern (see
test_fix05b3_massive_forex_live.py), never a shared/refactored code path
with it, to keep Forex regression risk at zero.

Protocol confirmed live (GOLDEN-MARKETDATA-CRYPTO-01 provider-access
audit):

  wss://socket.massive.com/crypto   (DIFFERENT cluster from .../forex)
  auth:      {"action":"auth","params":MASSIVE_API_KEY}
  subscribe: {"action":"subscribe","params":"XQ.BTC-USD,XQ.ETH-USD"}
             (channel prefix "XQ.", pair spelled WITH a hyphen — never
             the canonical no-separator internal symbol)
  quote:     [{"ev":"XQ","pair":"BTC-USD","bp":bid,"ap":ask,"t":ms,...}]
  REST hist: /v2/aggs/ticker/X:BTCUSD/range/... (same shape as Forex)
  REST last-trade (NO Forex equivalent): /v2/last/trade/X:BTCUSD ->
             {"results":{"p":price,...},"status":"OK"}

All WS tests mock market_data.feeds.websockets.connect — no real network
in manage.py test. REST tests mock urllib.request.urlopen. Same
_settle()/_ScriptedMassiveWS/_make_connect_mock idioms as the Forex
suite — duplicated here rather than imported, matching this block's
"duplicate, don't share" design decision.
"""
import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

_REAL_ASYNCIO_SLEEP = asyncio.sleep

from django.test import SimpleTestCase

from market_data.feeds import (
    DATA_STALE_TIMEOUT,
    FeedManager,
    MASSIVE_API_KEY,
    _MASSIVE_CRYPTO_ENABLED_SYMBOLS,
    _MASSIVE_CRYPTO_MAX_PAGES,
    _MASSIVE_CRYPTO_WS_PAIR,
    _MASSIVE_FOREX_MAX_PAGES,
    _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
    _massive_crypto_sym,
    _massive_crypto_symbol_from_pair,
    _massive_crypto_ws_param,
    _massive_crypto_ws_params_batch,
    get_feed_manager,
)

import simulator.consumers as consumers_module
from simulator.consumers import TradingConsumer, _KLINE_SYMBOLS

_FAKE_MASSIVE_KEY = "FAKEtestONLYnotREALkey1234567xx"

MASSIVE_AUTH_SUCCESS = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])
MASSIVE_AUTH_ERROR = json.dumps([{"ev": "status", "status": "error", "message": "invalid api key"}])
MASSIVE_CRYPTO_QUOTE_BTC = json.dumps([{
    "ev": "XQ", "pair": "BTC-USD", "bp": 65123.45, "ap": 65130.10,
    "bs": 0.5, "as": 0.4, "t": 1788217269000, "x": 1, "r": 1788217269000,
}])
MASSIVE_CRYPTO_QUOTE_ETH = json.dumps([{
    "ev": "XQ", "pair": "ETH-USD", "bp": 3210.55, "ap": 3211.20,
    "bs": 1.2, "as": 0.9, "t": 1788217269000, "x": 1, "r": 1788217269000,
}])
MASSIVE_CRYPTO_QUOTE_INVALID = json.dumps([{
    "ev": "XQ", "pair": "BTC-USD", "bp": 70000.0, "ap": 1.0, "t": 1788217269000,
}])
MASSIVE_EVENT_OTHER = json.dumps([{"ev": "unknown", "pair": "BTC-USD"}])


def _run(coro):
    return asyncio.run(coro)


async def _instant_real_yield(*args, **kwargs):
    await _REAL_ASYNCIO_SLEEP(0)


async def _settle(n=20):
    for _ in range(n):
        await _REAL_ASYNCIO_SLEEP(0)


class _ScriptedMassiveWS:
    """Same fake connection as test_fix05b3_massive_forex_live.py's own —
    duplicated per this block's "duplicate, don't share" decision."""

    def __init__(self, script, close_raises=True):
        self._script = list(script)
        self.sent = []
        self.closed = False
        self._close_raises = close_raises
        self._close_event = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
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
            raise ConnectionError("closed by watchdog (stale data)")
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self._close_event.set()


def _make_connect_mock(attempts, close_raises=True):
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
    def __init__(self, monotonic_fn):
        self.monotonic = monotonic_fn

    def __getattr__(self, name):
        import time as _real_time
        return getattr(_real_time, name)


class _ManualClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self, *args, **kwargs):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _MassiveCryptoLiveTestsBase(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        self._write_cache_patch = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        self._write_cache_mock = self._write_cache_patch.start()
        self.addCleanup(self._write_cache_patch.stop)
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


# ── A/E. Shared connection: one connection, one auth, batch subscribe ──
class SharedConnectionTests(_MassiveCryptoLiveTestsBase):
    def test_one_connection_for_two_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                self.assertEqual(connect_mock.call_count, 1)
        _run(_scenario())

    def test_one_auth_message_for_two_symbols(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                auth_msgs = [
                    json.loads(s) for s in connect_mock.instances[0].sent
                    if json.loads(s).get("action") == "auth"
                ]
                self.assertEqual(len(auth_msgs), 1)
                self.assertEqual(auth_msgs[0]["params"], _FAKE_MASSIVE_KEY)
        _run(_scenario())

    def test_batch_subscribe_uses_xq_prefix_and_hyphen_pairs(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                sub_msgs = [
                    json.loads(s) for s in connect_mock.instances[0].sent
                    if json.loads(s).get("action") == "subscribe"
                ]
                self.assertEqual(len(sub_msgs), 1)
                self.assertEqual(
                    sub_msgs[0]["params"],
                    _massive_crypto_ws_params_batch({"BTCUSD", "ETHUSD"}),
                )
                # MASSIVE-CRYPTO-TRADE-CANDLES-01 — every crypto subscribe
                # now covers both the quote channel (XQ — execution
                # authority, unchanged) and the trade channel (XT — chart/
                # candle/volume only) in the same call, same connection.
                self.assertEqual(sub_msgs[0]["params"], "XQ.BTC-USD,XT.BTC-USD,XQ.ETH-USD,XT.ETH-USD")
        _run(_scenario())

    def test_crypto_connection_never_shares_state_with_forex(self):
        # Distinct connections/locks/active-symbol sets by construction —
        # Option A never merges them.
        self.assertIsNot(self.fm._massive_crypto_active_symbols, self.fm._massive_active_symbols)
        self.assertIsNot(self.fm._massive_crypto_connect_lock, self.fm._massive_connect_lock)


# ── C/D/F. Per-symbol parsing + pair<->symbol normalization ──
class SymbolNormalizationAndParsingTests(_MassiveCryptoLiveTestsBase):
    def test_ws_pair_mapping_explicit_no_heuristic(self):
        self.assertEqual(_MASSIVE_CRYPTO_WS_PAIR, {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"})
        self.assertEqual(_massive_crypto_symbol_from_pair("BTC-USD"), "BTCUSD")
        self.assertEqual(_massive_crypto_symbol_from_pair("ETH-USD"), "ETHUSD")
        self.assertIsNone(_massive_crypto_symbol_from_pair("XRP-USD"))

    def test_ws_param_and_rest_ticker_mapping(self):
        # MASSIVE-CRYPTO-TRADE-CANDLES-01 — quote channel (execution)
        # AND trade channel (chart) subscribed together, same call.
        self.assertEqual(_massive_crypto_ws_param("BTCUSD"), "XQ.BTC-USD,XT.BTC-USD")
        self.assertEqual(_massive_crypto_sym("BTCUSD"), "X:BTCUSD")
        self.assertEqual(_massive_crypto_sym("ETHUSD"), "X:ETHUSD")
        self.assertIsNone(_massive_crypto_sym("XRPUSD"))

    def _assert_symbol_broadcasts(self, symbol, quote_json, expected_bid, expected_ask):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, quote_json]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_crypto_register_symbol(symbol, self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
                args = mock_broadcast.await_args
                self.assertEqual(args.args[0], symbol)
                self.assertAlmostEqual(args.args[2], expected_bid)
                self.assertAlmostEqual(args.args[3], expected_ask)
                self.assertEqual(args.kwargs["source"], "massive")
        _run(_scenario())

    def test_parse_btcusd(self):
        self._assert_symbol_broadcasts("BTCUSD", MASSIVE_CRYPTO_QUOTE_BTC, 65123.45, 65130.10)

    def test_parse_ethusd(self):
        self._assert_symbol_broadcasts("ETHUSD", MASSIVE_CRYPTO_QUOTE_ETH, 3210.55, 3211.20)

    def test_unknown_pair_dropped_never_routed(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS,
            json.dumps([{"ev": "XQ", "pair": "SOL-USD", "bp": 1.0, "ap": 1.1, "t": 1788217269000}]),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_not_awaited()
        _run(_scenario())


# ── P. No cross-symbol contamination ──
class SymbolIsolationTests(_MassiveCryptoLiveTestsBase):
    def test_only_the_quoted_symbol_broadcasts(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_CRYPTO_QUOTE_BTC]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                self.assertEqual(mock_broadcast.await_count, 1)
                self.assertEqual(mock_broadcast.await_args.args[0], "BTCUSD")
        _run(_scenario())


# ── G. Redis write + source="massive" ──
class RedisKeyTests(_MassiveCryptoLiveTestsBase):
    def test_write_price_cache_called_with_source_massive(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_CRYPTO_QUOTE_BTC]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self._write_cache_mock.assert_awaited()
                args = self._write_cache_mock.await_args
                self.assertEqual(args.args[0], "BTCUSD")
                self.assertEqual(args.args[3], "massive")
        _run(_scenario())


# ── I/J. Watchdog: per-symbol resubscribe (never escalates), all-stale close ──
class SymbolStaleRecoveryTests(_MassiveCryptoLiveTestsBase):
    def test_first_stale_triggers_resubscribe_of_that_symbol(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                clock.advance(1.0)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                clock.advance(19.5)
                await _settle()
                self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts["BTCUSD"], 1)
                self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts["ETHUSD"], 0)
                self.assertFalse(connect_mock.instances[0].closed)
                sent = [json.loads(s) for s in connect_mock.instances[0].sent]
                resub = [m for m in sent if m.get("params") == "XQ.BTC-USD,XT.BTC-USD" and m["action"] in ("subscribe", "unsubscribe")]
                self.assertEqual([m["action"] for m in resub], ["subscribe", "unsubscribe", "subscribe"])
        _run(_scenario())

    def test_one_symbol_stale_does_not_close_socket(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                self.assertFalse(connect_mock.instances[0].closed)
        _run(_scenario())

    def test_valid_quote_resets_attempts_to_zero(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_CRYPTO_QUOTE_BTC]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                self.fm._massive_crypto_symbol_stale_attempts["BTCUSD"] = 1
                await _settle()
                self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts["BTCUSD"], 0)
        _run(_scenario())


class EscalationTests(_MassiveCryptoLiveTestsBase):
    def test_persistent_symbol_staleness_never_escalates_to_second_provider(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        self.assertEqual(_MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD, 2)
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                for expected_attempt in (1, 2, 3):
                    clock.advance(DATA_STALE_TIMEOUT + 1)
                    self.fm._massive_crypto_last_quote_at["ETHUSD"] = clock.now
                    await _settle()
                    self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts["BTCUSD"], expected_attempt)
                    self.assertIn("BTCUSD", self.fm._massive_crypto_active_symbols)
        _run(_scenario())


class ConnectionStaleTests(_MassiveCryptoLiveTestsBase):
    def test_all_symbol_stale_closes_ws(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS],
            [asyncio.CancelledError()],
        ], close_raises=False)
        clock = self._patch_manual_clock(start=0.0)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                clock.advance(DATA_STALE_TIMEOUT + 1)
                await _settle()
                self.assertTrue(connect_mock.instances[0].closed)
        _run(_scenario())


class ZeroActiveShutdownTests(_MassiveCryptoLiveTestsBase):
    def test_zero_active_symbols_shuts_service_down(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                await self.fm._massive_crypto_unregister_symbol("BTCUSD")
                self.assertIsNone(self.fm._massive_crypto_shared_task)
                self.assertIsNone(self.fm._massive_crypto_ws)
                self.assertFalse(self.fm._massive_crypto_authed)
        _run(_scenario())


class RejectionTests(_MassiveCryptoLiveTestsBase):
    def test_invalid_quote_rejected_never_broadcast(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, MASSIVE_CRYPTO_QUOTE_INVALID]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_not_awaited()
        _run(_scenario())

    def test_malformed_event_does_not_crash_reader(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, "not valid json{{", MASSIVE_CRYPTO_QUOTE_BTC]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
        _run(_scenario())


# ── M/N. Dispatch priority — Massive checked first, Binance/Kraken unreachable ──
class DispatchPriorityTests(_MassiveCryptoLiveTestsBase):
    def test_btcusd_ethusd_use_massive_exclusively(self):
        for sym in ("BTCUSD", "ETHUSD"):
            self.assertIn(sym, _MASSIVE_CRYPTO_ENABLED_SYMBOLS)

    def test_massive_failure_never_falls_through_to_binance_or_kraken(self):
        async def _scenario():
            with patch.object(self.fm, "_massive_crypto_loop", new=AsyncMock(side_effect=RuntimeError("boom"))), \
                 patch.object(self.fm, "_binance_loop", new=AsyncMock()) as mock_binance, \
                 patch.object(self.fm, "_kraken_loop", new=AsyncMock()) as mock_kraken:
                result = await self.fm._try_live_legacy("BTCUSD", self.channel_layer)
                self.assertFalse(result)
                mock_binance.assert_not_called()
                mock_kraken.assert_not_called()
        _run(_scenario())

    def test_massive_success_returns_true_without_touching_binance(self):
        async def _scenario():
            with patch.object(self.fm, "_massive_crypto_loop", new=AsyncMock(return_value=None)), \
                 patch.object(self.fm, "_binance_loop", new=AsyncMock()) as mock_binance:
                result = await self.fm._try_live_legacy("BTCUSD", self.channel_layer)
                self.assertTrue(result)
                mock_binance.assert_not_called()
        _run(_scenario())

    def test_no_massive_key_falls_through_to_binance_unchanged(self):
        # Without a configured key, the new crypto branch must be a no-op
        # (not a crash, not a silent True) — the pre-existing Binance path
        # for any OTHER symbol stays reachable exactly as before. Binance
        # AND Kraken must both be mocked here: _try_live_legacy's original
        # chain falls from Binance to Kraken on failure, and BTCUSD has a
        # real, unmocked _kraken_sym() mapping — leaving _kraken_loop
        # unmocked previously let this test fall through to a REAL
        # wss://ws.kraken.com connection (confirmed: process hung with an
        # ESTABLISHED TCP socket, near-zero CPU, until killed). Isolation
        # gap in this test only — feeds.py's dispatch chain itself was
        # never at fault.
        async def _scenario():
            with patch("market_data.feeds.MASSIVE_API_KEY", ""), \
                 patch.object(self.fm, "_binance_loop", new=AsyncMock(side_effect=RuntimeError("no net in test"))), \
                 patch.object(self.fm, "_kraken_loop", new=AsyncMock(side_effect=RuntimeError("no net in test"))):
                result = await self.fm._try_live_legacy("BTCUSD", self.channel_layer)
                self.assertFalse(result)
        _run(_scenario())


# ── L. Fail-closed: REST last-trade resync ──
class RestResyncFailClosedTests(_MassiveCryptoLiveTestsBase):
    def _mock_urlopen(self, body: bytes, status_ok=True):
        cm = MagicMock()
        cm.__enter__.return_value = io.BytesIO(body)
        cm.__exit__.return_value = False
        return cm

    def test_successful_last_trade_returns_price(self):
        body = json.dumps({"status": "OK", "results": {"p": 65123.45}}).encode()
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)), \
                 patch.object(self.fm, "_binance_loop", new=AsyncMock()):
                px = await self.fm._fetch_rest_price("BTCUSD")
                self.assertAlmostEqual(px, 65123.45)
        _run(_scenario())

    def test_failure_returns_none_never_falls_through_to_binance_or_coingecko(self):
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", side_effect=TimeoutError("no net")), \
                 patch("market_data.feeds._binance_sym", return_value="BTCUSDT"):
                px = await self.fm._fetch_rest_price("BTCUSD")
                self.assertIsNone(px)
        _run(_scenario())

    def test_non_ok_status_returns_none_fails_closed(self):
        body = json.dumps({"status": "NOT_AUTHORIZED"}).encode()
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)):
                px = await self.fm._fetch_rest_price("BTCUSD")
                self.assertIsNone(px)
        _run(_scenario())

    def test_no_massive_key_falls_through_to_binance_rest(self):
        body = json.dumps({"price": "65000.00"}).encode()
        async def _scenario():
            with patch("market_data.feeds.MASSIVE_API_KEY", ""), \
                 patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)):
                px = await self.fm._fetch_rest_price("BTCUSD")
                self.assertAlmostEqual(px, 65000.00)
        _run(_scenario())


# ── K. Historical bars ──
class HistoricalFetchTests(_MassiveCryptoLiveTestsBase):
    def _mock_urlopen(self, body: bytes):
        cm = MagicMock()
        cm.__enter__.return_value = io.BytesIO(body)
        cm.__exit__.return_value = False
        return cm

    def test_btcusd_history_shape(self):
        body = json.dumps({
            "status": "OK",
            "results": [
                {"t": 1788200000000, "o": 65000.0, "h": 65200.0, "l": 64900.0, "c": 65100.0, "v": 12.5},
            ],
        }).encode()
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)):
                bars = await self.fm.fetch_massive_crypto_history("BTCUSD", interval="1m", limit=10)
                self.assertEqual(len(bars), 1)
                self.assertEqual(set(bars[0].keys()), {"time", "open", "high", "low", "close", "volume"})
                self.assertAlmostEqual(bars[0]["close"], 65100.0)
        _run(_scenario())

    def test_ethusd_history_shape(self):
        body = json.dumps({
            "status": "OK",
            "results": [
                {"t": 1788200000000, "o": 3200.0, "h": 3220.0, "l": 3190.0, "c": 3210.0, "v": 200.0},
            ],
        }).encode()
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)):
                bars = await self.fm.fetch_massive_crypto_history("ETHUSD", interval="1m", limit=10)
                self.assertEqual(len(bars), 1)
                self.assertAlmostEqual(bars[0]["close"], 3210.0)
        _run(_scenario())

    def test_unsupported_symbol_returns_empty_without_network(self):
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen") as mock_urlopen:
                bars = await self.fm.fetch_massive_crypto_history("XRPUSD", interval="1m", limit=10)
                self.assertEqual(bars, [])
                mock_urlopen.assert_not_called()
        _run(_scenario())

    def test_empty_results_is_not_an_error(self):
        body = json.dumps({"status": "OK", "results": []}).encode()
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", return_value=self._mock_urlopen(body)):
                bars = await self.fm.fetch_massive_crypto_history("BTCUSD", interval="1h", limit=10)
                self.assertEqual(bars, [])
        _run(_scenario())

    def test_forex_history_fetcher_untouched_by_crypto_addition(self):
        fm = FeedManager()
        self.assertTrue(hasattr(fm, "fetch_massive_history"))
        self.assertTrue(hasattr(fm, "fetch_massive_crypto_history"))
        self.assertIsNot(fm.fetch_massive_history, fm.fetch_massive_crypto_history)


# ── ACCEPTANCE FIX — DESC pagination (GOLDEN-MARKETDATA-CRYPTO-01) ──
# Confirmed live against the real Massive API: the crypto aggs endpoint
# returns only ~13 results/page regardless of `limit`. At the time this
# was written, the old ASC + 3-page cap (then named _MASSIVE_MAX_PAGES,
# assumed Forex-safe since Forex 1m fit in a single page) silently
# truncated crypto history ~4 days short of "now" for BOTH BTCUSD and
# ETHUSD. Fix: request sort=desc, paginate up to _MASSIVE_CRYPTO_MAX_PAGES,
# trim to the most recent `limit` bars, then sort ascending — same final
# contract as before.
#
# CHART-GLOBAL-REGRESSION-01 (later) confirmed live that Forex has the
# IDENTICAL small-page-size defect for every pair/timeframe except 1m —
# fetch_massive_history() now uses the same sort=desc + trim + ascending-
# sort mechanism, bounded by its own _MASSIVE_FOREX_MAX_PAGES (=20, same
# evidence-backed value, separate constant — see
# test_fix05b2_massive_history.py::DescPaginationForexTests for Forex's
# own full coverage of this same mechanism).
def _crypto_row(t_ms, o=77000.0, h=77100.0, l=76900.0, c=77050.0, v=5.0):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v}


def _crypto_payload(rows, next_url=None, status="OK"):
    d = {"status": status, "resultsCount": len(rows), "queryCount": len(rows)}
    if rows:
        d["results"] = rows
    if next_url:
        d["next_url"] = next_url
    return d


def _mock_response(body: bytes):
    m = MagicMock()
    m.__enter__.return_value = io.BytesIO(body)
    m.__exit__.return_value = False
    return m


class DescPaginationTests(_MassiveCryptoLiveTestsBase):
    def _run_fetch(self, symbol="BTCUSD", interval="1m", limit=200):
        return _run(self.fm.fetch_massive_crypto_history(symbol, interval=interval, limit=limit))

    def test_initial_request_url_includes_sort_desc(self):
        body = json.dumps(_crypto_payload([_crypto_row(9_000_000)])).encode()
        captured = []

        def _capture(req, timeout=None):
            captured.append(req.full_url if hasattr(req, "full_url") else str(req))
            return _mock_response(body)

        with patch("market_data.feeds.urllib.request.urlopen", side_effect=_capture):
            self._run_fetch()
        self.assertEqual(len(captured), 1)
        self.assertIn("sort=desc", captured[0])

    def test_stops_when_requested_limit_reached_on_first_page(self):
        # A single page already returns >= limit -> no second request.
        rows = [_crypto_row(9_000_000 + i * 60_000) for i in range(5)]
        body = json.dumps(_crypto_payload(rows, next_url="https://api.massive.com/next?cursor=x")).encode()
        with patch("market_data.feeds.urllib.request.urlopen", return_value=_mock_response(body)) as mock_urlopen:
            bars = self._run_fetch(limit=5)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(len(bars), 5)

    def test_stops_at_crypto_safety_cap_never_more(self):
        # Every page: 1 row + a next_url -> would paginate forever without
        # the cap. limit=1000 so len(all_bars) never reaches `limit` on its
        # own, forcing _MASSIVE_CRYPTO_MAX_PAGES to be what stops it.
        responses = [
            _mock_response(json.dumps(_crypto_payload(
                [_crypto_row(9_000_000 + i * 60_000)],
                next_url=f"https://api.massive.com/next?page={i}",
            )).encode())
            for i in range(_MASSIVE_CRYPTO_MAX_PAGES + 5)  # far more pages available than the cap allows
        ]
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=responses) as mock_urlopen:
            bars = self._run_fetch(limit=1000)
        self.assertEqual(mock_urlopen.call_count, _MASSIVE_CRYPTO_MAX_PAGES)
        self.assertEqual(len(bars), _MASSIVE_CRYPTO_MAX_PAGES)

    def test_stops_cleanly_when_no_next_url(self):
        rows = [_crypto_row(9_000_000 + i * 60_000) for i in range(3)]
        body = json.dumps(_crypto_payload(rows)).encode()  # no next_url
        with patch("market_data.feeds.urllib.request.urlopen", return_value=_mock_response(body)) as mock_urlopen:
            bars = self._run_fetch(limit=1000)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(len(bars), 3)

    def test_final_output_sorted_ascending_despite_desc_arrival(self):
        # Page 1 (newest-first): t=300,200 ; page 2: t=100
        page1 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(300_000), _crypto_row(200_000)],
            next_url="https://api.massive.com/next?cursor=a",
        )).encode())
        page2 = _mock_response(json.dumps(_crypto_payload([_crypto_row(100_000)])).encode())
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=[page1, page2]):
            bars = self._run_fetch(limit=1000)
        times = [b["time"] for b in bars]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times, [100, 200, 300])

    def test_keeps_the_most_recent_bars_when_more_than_limit_accumulates(self):
        # 3 pages of 2 rows each (desc), limit=3 -> must keep the 3 NEWEST
        # (t=600,500,400), never the 3 oldest.
        page1 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(600_000), _crypto_row(500_000)],
            next_url="https://api.massive.com/next?cursor=a",
        )).encode())
        page2 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(400_000), _crypto_row(300_000)],
            next_url="https://api.massive.com/next?cursor=b",
        )).encode())
        page3 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(200_000), _crypto_row(100_000)],
        )).encode())
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=[page1, page2, page3]):
            bars = self._run_fetch(limit=3)
        self.assertEqual([b["time"] for b in bars], [400, 500, 600])

    def test_no_duplicate_timestamps_across_page_boundary(self):
        page1 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(200_000, c=77777.0)],
            next_url="https://api.massive.com/next?cursor=a",
        )).encode())
        # Same ts repeated with a DIFFERENT close — first (newest-arrival)
        # occurrence must win.
        page2 = _mock_response(json.dumps(_crypto_payload(
            [_crypto_row(200_000, c=1.0), _crypto_row(100_000)],
        )).encode())
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=[page1, page2]):
            bars = self._run_fetch(limit=1000)
        times = [b["time"] for b in bars]
        self.assertEqual(len(times), len(set(times)))
        self.assertAlmostEqual(next(b["close"] for b in bars if b["time"] == 200), 77777.0)

    def test_sparse_timeframe_prefers_freshness_over_depth(self):
        # Every page returns only 1 row (mirrors Massive's real ~13/page
        # crypto behavior at a smaller scale) — even hitting the safety
        # cap without reaching `limit`, the newest bar (page 1) must
        # survive in the final result (degradation rule: less depth,
        # never stale freshness).
        responses = [
            _mock_response(json.dumps(_crypto_payload(
                [_crypto_row(9_000_000 - i * 60_000)],
                next_url=f"https://api.massive.com/next?page={i}",
            )).encode())
            for i in range(_MASSIVE_CRYPTO_MAX_PAGES + 3)
        ]
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=responses):
            bars = self._run_fetch(limit=1000)  # unreachable within the cap
        self.assertLess(len(bars), 1000)
        self.assertEqual(len(bars), _MASSIVE_CRYPTO_MAX_PAGES)
        self.assertEqual(bars[-1]["time"], 9000)  # the freshest bar (page 1) is present

    def test_forex_history_url_now_also_includes_sort_desc(self):
        # CHART-GLOBAL-REGRESSION-01 — this used to assert the OPPOSITE
        # ("Forex must never gain sort=desc"), back when Forex's own
        # 15m/1h staleness defect hadn't been discovered yet. Forex now
        # deliberately shares the same sort=desc mechanism as crypto (two
        # separate call sites/constants, not shared code — see
        # test_fix05b2_massive_history.py::DescPaginationForexTests for
        # Forex's own dedicated coverage of this).
        from market_data.feeds import FeedManager as _FM
        body = json.dumps({"status": "OK", "results": [{"t": 1788200000000, "o": 1.1, "h": 1.1, "l": 1.1, "c": 1.1, "v": 1.0}]}).encode()
        captured = []

        def _capture(req, timeout=None):
            captured.append(req.full_url if hasattr(req, "full_url") else str(req))
            return _mock_response(body)

        fm = _FM()
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=_capture):
            _run(fm.fetch_massive_history("EUR/USD", interval="1m", limit=200))
        self.assertEqual(len(captured), 1)
        self.assertIn("sort=desc", captured[0])

    def test_forex_and_crypto_have_separate_max_pages_constants(self):
        # Same evidence-backed value (20) — both pairs/timeframes show an
        # identical small-page-size profile — but two independently
        # defined constants, not one shared/reused value.
        self.assertEqual(_MASSIVE_FOREX_MAX_PAGES, 20)
        self.assertEqual(_MASSIVE_CRYPTO_MAX_PAGES, 20)
        import inspect
        from market_data import feeds as feeds_module
        src = inspect.getsource(feeds_module)
        self.assertIn("_MASSIVE_FOREX_MAX_PAGES = 20", src)
        self.assertIn("_MASSIVE_CRYPTO_MAX_PAGES = 20", src)


# ── generate_history() dispatch order in consumers.py ──
class GenerateHistoryDispatchTests(SimpleTestCase):
    def _make_consumer(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c._feed = get_feed_manager()
        c._price_state, c._bid_state, c._ask_state = {}, {}, {}
        c.account = {"spread_pips": 0.0}
        return c

    def test_btcusd_history_calls_massive_crypto_not_kline(self):
        c = self._make_consumer()
        async def _scenario():
            with patch.object(c._feed, "fetch_massive_crypto_history", new=AsyncMock(return_value=[
                {"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            ])) as mock_massive, \
                 patch.object(c._feed, "fetch_kline_history", new=AsyncMock()) as mock_kline:
                await c.generate_history("BTCUSD", "1m", bars=10)
                mock_massive.assert_awaited_once()
                mock_kline.assert_not_called()
        _run(_scenario())

    def test_ethusd_history_calls_massive_crypto_not_kline(self):
        c = self._make_consumer()
        async def _scenario():
            with patch.object(c._feed, "fetch_massive_crypto_history", new=AsyncMock(return_value=[
                {"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            ])) as mock_massive, \
                 patch.object(c._feed, "fetch_kline_history", new=AsyncMock()) as mock_kline:
                await c.generate_history("ETHUSD", "1m", bars=10)
                mock_massive.assert_awaited_once()
                mock_kline.assert_not_called()
        _run(_scenario())

    def test_forex_history_dispatch_unaffected(self):
        c = self._make_consumer()
        async def _scenario():
            with patch.object(c._feed, "fetch_massive_history", new=AsyncMock(return_value=[
                {"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            ])) as mock_massive_fx, \
                 patch.object(c._feed, "fetch_massive_crypto_history", new=AsyncMock()) as mock_massive_crypto:
                await c.generate_history("EUR/USD", "1m", bars=10)
                mock_massive_fx.assert_awaited_once()
                mock_massive_crypto.assert_not_called()
        _run(_scenario())


# ── consumers.py _on_tick — real-time candle aggregation from Massive ticks ──
class OnTickCandleAggregationTests(SimpleTestCase):
    def _make_consumer(self, timeframe="1m"):
        c = TradingConsumer.__new__(TradingConsumer)
        c._agg = {}
        c._last_bar_time = {}
        c.timeframe = timeframe
        c.send_json = AsyncMock()
        return c

    def test_btcusd_massive_tick_feeds_candle_aggregation(self):
        c = self._make_consumer()
        async def _scenario():
            await c._on_tick("BTCUSD", 65100.0, volume=0.0, ts=1788200000)
            self.assertIn("BTCUSD", c._agg)
            types = [call.args[0]["type"] for call in c.send_json.await_args_list]
            self.assertIn("candle_new", types)
        _run(_scenario())

    def test_ethusd_massive_tick_feeds_candle_aggregation(self):
        c = self._make_consumer()
        async def _scenario():
            await c._on_tick("ETHUSD", 3210.0, volume=0.0, ts=1788200000)
            self.assertIn("ETHUSD", c._agg)
            types = [call.args[0]["type"] for call in c.send_json.await_args_list]
            self.assertIn("candle_new", types)
        _run(_scenario())

    def test_no_duplicate_candle_path_single_new_then_update(self):
        # First tick in a bucket -> candle_new; a second tick in the SAME
        # bucket -> candle_update, never a second candle_new (no double
        # aggregation from the same tick stream).
        c = self._make_consumer()
        async def _scenario():
            await c._on_tick("BTCUSD", 65100.0, volume=0.0, ts=1788200000)
            await c._on_tick("BTCUSD", 65105.0, volume=0.0, ts=1788200010)
            types = [call.args[0]["type"] for call in c.send_json.await_args_list if call.args[0]["type"].startswith("candle")]
            self.assertEqual(types.count("candle_new"), 1)
            self.assertEqual(types.count("candle_update"), 1)
        _run(_scenario())

    def test_forex_symbol_unaffected_never_in_kline_symbols(self):
        c = self._make_consumer()
        self.assertNotIn("EUR/USD", _KLINE_SYMBOLS)
        async def _scenario():
            await c._on_tick("EUR/USD", 1.1000, volume=0.0, ts=1788200000)
            self.assertIn("EUR/USD", c._agg)
        _run(_scenario())

    def test_other_kline_symbols_still_skip_aggregation(self):
        # Defensive: a hypothetical _KLINE_SYMBOLS member OUTSIDE
        # _MASSIVE_CRYPTO_ENABLED_SYMBOLS must keep the ORIGINAL
        # skip-and-wait-for-candle_kline() behavior, unchanged.
        c = self._make_consumer()
        fake_symbols = frozenset({"BTCUSD", "ETHUSD", "FAKEKLINE"})
        async def _scenario():
            with patch.object(consumers_module, "_KLINE_SYMBOLS", fake_symbols):
                await c._on_tick("FAKEKLINE", 1.0, volume=0.0, ts=1788200000)
                self.assertNotIn("FAKEKLINE", c._agg)
                c.send_json.assert_not_awaited()
        _run(_scenario())

    def test_kline_symbols_today_exactly_matches_massive_crypto_symbols(self):
        # Confirms the premise this whole test class depends on: today,
        # every _KLINE_SYMBOLS member IS a Massive-crypto-live symbol —
        # exchange_symbol/kraken_symbol (symbol_specs.py) are set
        # exclusively on BTCUSD/ETHUSD (kept dormant, unchanged).
        self.assertEqual(set(_KLINE_SYMBOLS), set(_MASSIVE_CRYPTO_ENABLED_SYMBOLS))


class NoBinanceKrakenCoinGeckoRuntimeTests(_MassiveCryptoLiveTestsBase):
    """Functional zero-Binance/Kraken/CoinGecko-at-runtime outcome for
    BTCUSD/ETHUSD, without deleting any of that code (GOLDEN-MARKETDATA-
    CRYPTO-01 §I — KEEP AS DORMANT)."""

    def test_live_never_reaches_binance_or_kraken(self):
        async def _scenario():
            with patch.object(self.fm, "_massive_crypto_loop", new=AsyncMock(return_value=None)), \
                 patch.object(self.fm, "_binance_loop", new=AsyncMock()) as mock_binance, \
                 patch.object(self.fm, "_kraken_loop", new=AsyncMock()) as mock_kraken:
                for sym in ("BTCUSD", "ETHUSD"):
                    await self.fm._try_live_legacy(sym, self.channel_layer)
                mock_binance.assert_not_called()
                mock_kraken.assert_not_called()
        _run(_scenario())

    def test_rest_resync_never_reaches_coingecko_or_kraken(self):
        async def _scenario():
            with patch("market_data.feeds.urllib.request.urlopen", side_effect=TimeoutError("no net")):
                for sym in ("BTCUSD", "ETHUSD"):
                    px = await self.fm._fetch_rest_price(sym)
                    self.assertIsNone(px)
        _run(_scenario())

    def test_binance_kraken_code_bodies_still_exist_dormant(self):
        # "No borrar código ciegamente" — REMOVE-FROM-ACTIVE-RUNTIME only,
        # never blind deletion.
        self.assertTrue(hasattr(self.fm, "_binance_loop"))
        self.assertTrue(hasattr(self.fm, "_kraken_loop"))
        self.assertTrue(hasattr(self.fm, "fetch_kline_history"))


# ── ACCEPTANCE FIX — frontend volume contract (dashboard.html) ──
# Source-inspection tests, same pattern as test_fix05b1_real_history_
# integrity.py::Fix05cContractRegressionTests — no JS runtime in this
# Django test suite, so the template's raw text is the contract.
class FrontendVolumeContractTests(SimpleTestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_history_bars_mapping_includes_real_volume(self):
        src = self._template_source()
        self.assertIn(
            "const bars=msg.data.map(c=>({time:normTime(c.time),open:n(c.open),high:n(c.high),"
            "low:n(c.low),close:n(c.close),volume:n(c.volume)}))",
            src,
        )

    def test_vol_point_for_bar_prefers_real_volume(self):
        src = self._template_source()
        i = src.index("const volPointForBar=")
        snippet = src[i:i + 600]
        self.assertIn("b.volume!=null&&Number.isFinite(b.volume)&&b.volume>0", snippet)
        self.assertIn("val=b.volume;", snippet)

    def test_span_times_1e6_kept_only_as_fallback(self):
        # The old proxy formula must still exist (live-tick path has no
        # volume field to use instead) but only reachable in the `else`
        # branch, never as the primary/unconditional calculation.
        src = self._template_source()
        i = src.index("const volPointForBar=")
        snippet = src[i:i + 600]
        self.assertIn("span*1e6", snippet)
        self.assertIn("else{", snippet)
        # Primary path must come first and short-circuit the fallback.
        self.assertLess(snippet.index("val=b.volume;"), snippet.index("span*1e6"))

    def test_candle_ohlc_construction_unchanged(self):
        src = self._template_source()
        self.assertIn(
            "open:n(c.open),high:n(c.high),low:n(c.low),close:n(c.close)",
            src,
        )

    def test_candle_price_scale_config_unchanged(self):
        src = self._template_source()
        self.assertIn(
            "this.candleSeries=this.chart.addCandlestickSeries({upColor:'#26a69a',"
            "downColor:'#ef5350',wickUpColor:'#26a69a',wickDownColor:'#ef5350',"
            "borderUpColor:'#26a69a',borderDownColor:'#ef5350',"
            "priceFormat:priceFormatFor(this.currentSymbol),priceLineVisible:false});",
            src,
        )

    def test_volume_series_keeps_its_own_isolated_price_scale(self):
        src = self._template_source()
        self.assertIn("const VS=`vol-${this.id}`;", src)
        self.assertIn(
            "this.volumeSeries=this.chart.addHistogramSeries({priceScaleId:VS,",
            src,
        )
        self.assertIn(
            "this.chart.priceScale(VS).applyOptions({scaleMargins:{top:0.82,bottom:0},"
            "visible:false,borderVisible:false});",
            src,
        )

    def test_price_format_for_still_branches_by_symbol(self):
        # Symbol-switch scale correctness (BTC->EUR->BTC) relies entirely
        # on this pre-existing, untouched branching — never on anything
        # this volume fix changed.
        src = self._template_source()
        self.assertIn(
            "const priceFormatFor=sym=>(sym.includes('BTC')||sym.includes('ETH'))"
            "?{precision:2,minMove:0.01}:sym.endsWith('/JPY')?{precision:3,minMove:0.001}"
            ":{precision:5,minMove:0.00001};",
            src,
        )
        self.assertIn(
            "this.candleSeries?.applyOptions({priceFormat:priceFormatFor(this.currentSymbol)});",
            src,
        )

    def test_live_tick_candle_update_still_has_no_volume_field(self):
        # Confirms the fallback branch in volPointForBar is genuinely
        # reachable: candle_new/candle_update payloads never carry a
        # volume field (Massive quotes have no per-tick trade volume) —
        # unchanged by this fix.
        src = self._template_source()
        self.assertIn(
            "const b={time:normTime(msg.data.time),open:n(msg.data.open),high:n(msg.data.high),"
            "low:n(msg.data.low),close:n(msg.data.close)};",
            src,
        )


# ── ACCEPTANCE FIX — symbol/timeframe visual reset (GOLDEN-MARKETDATA-CRYPTO-01) ──
# Root cause confirmed by audit: switching symbol/timeframe reset internal
# STATE (this._bars, liveMid, bid/ask) but never cleared what was VISIBLE
# (candleSeries/volumeSeries data, the price badge text) — the chart kept
# rendering the PREVIOUS symbol's candles/scale, and the price badge kept
# showing the previous symbol's last price, until the new history/tick
# arrived. Not a network reordering race (Channels serializes all
# per-connection message processing) — a pure "forgot to clear the
# render" gap. Fixed in dashboard.html; these are source-inspection tests
# (same pattern as FrontendVolumeContractTests — no JS runtime in this
# Django test suite) plus one real backend behavioral test for the new
# `timeframe` field on the `history` message.
class HistoryTimeframeContractTests(SimpleTestCase):
    """Backend: {"type":"history",...} now always includes `timeframe`,
    for both Forex and Crypto symbols — the field the frontend needs to
    detect and ignore a stale-timeframe (same-symbol) response."""

    def _make_consumer(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c.send_json = AsyncMock()
        return c

    def test_history_message_includes_timeframe_crypto(self):
        c = self._make_consumer()
        hist = [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}]
        _run(c._send_history_or_unavailable("BTCUSD", "15m", hist))
        c.send_json.assert_awaited_once()
        msg = c.send_json.await_args.args[0]
        self.assertEqual(msg["type"], "history")
        self.assertEqual(msg["symbol"], "BTCUSD")
        self.assertEqual(msg["timeframe"], "15m")

    def test_history_message_includes_timeframe_forex(self):
        c = self._make_consumer()
        hist = [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}]
        _run(c._send_history_or_unavailable("EUR/USD", "1h", hist))
        msg = c.send_json.await_args.args[0]
        self.assertEqual(msg["timeframe"], "1h")

    def test_history_unavailable_still_includes_timeframe_unchanged(self):
        # Pre-existing contract (history_unavailable already had this
        # field) — confirms the fix didn't disturb it.
        c = self._make_consumer()
        _run(c._send_history_or_unavailable("ETHUSD", "1m", None))
        msg = c.send_json.await_args.args[0]
        self.assertEqual(msg["type"], "history_unavailable")
        self.assertEqual(msg["timeframe"], "1m")


class SymbolTimeframeVisualResetTests(SimpleTestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _method_body(self, src, method_signature, max_len=None):
        # Bounded by the NEXT top-level (2-space-indented) method
        # signature in the Panel class — never a fixed length guess,
        # which broke twice already as these methods grew comments (a
        # too-short window silently truncated real code out of the
        # snippet; a too-long window bled into unrelated later methods).
        import re
        i = src.index(method_signature)
        start = i + len(method_signature)
        m = re.search(r"\n  [A-Za-z_][A-Za-z0-9_]*\s*\(", src[start:])
        end = start + m.start() if m else (start + (max_len or 3500))
        return src[i:end]

    # -- _onSymChange(): candles/scale must never show the previous symbol --
    def test_on_sym_change_clears_candle_and_volume_series_immediately(self):
        src = self._template_source()
        body = self._method_body(src, "_onSymChange(){")
        self.assertIn("this.candleSeries?.setData([]); this.volumeSeries?.setData([]);", body)
        # Must happen BEFORE the WS change_symbol send (immediate, not
        # waiting for any response).
        self.assertLess(
            body.index("this.candleSeries?.setData([])"),
            body.index("action:'change_symbol'"),
        )

    def test_on_sym_change_blanks_price_badge_text(self):
        src = self._template_source()
        body = self._method_body(src, "_onSymChange(){")
        self.assertIn("if(this.pxTag)this.pxTag.textContent='—';", body)
        self.assertIn("this._updateBidAsk();", body)
        # Must come after liveMid is nulled, before the WS send.
        i_null = body.index("this.liveMid=null")
        i_blank = body.index("this.pxTag.textContent=")
        i_send = body.index("action:'change_symbol'")
        self.assertLess(i_null, i_blank)
        self.assertLess(i_blank, i_send)

    def test_on_sym_change_preserves_existing_cleanup(self):
        # Price lines / indicators / accumulator reset — must not have
        # been removed by this fix.
        src = self._template_source()
        body = self._method_body(src, "_onSymChange(){")
        self.assertIn("this._clearLines();", body)
        self.assertIn("this._clearOscillatorData();", body)
        self.assertIn("this._resetAgg();", body)

    # -- _onTFChange(): same principle, opposite direction too (15m<->1h) --
    def test_on_tf_change_clears_candle_and_volume_series_immediately(self):
        src = self._template_source()
        body = self._method_body(src, "_onTFChange(){")
        self.assertIn("this.candleSeries?.setData([]); this.volumeSeries?.setData([]);", body)
        self.assertLess(
            body.index("this.candleSeries?.setData([])"),
            body.index("action:'change_timeframe'"),
        )

    def test_on_tf_change_resets_candle_derived_close_state(self):
        src = self._template_source()
        body = self._method_body(src, "_onTFChange(){")
        self.assertIn("this.lastClose=null; this.prevClose=null; this.prevCandleClose=null;", body)

    def test_on_tf_change_does_not_touch_live_tick_state(self):
        # Deliberate: liveMid/bid/ask reflect the CURRENT symbol's live
        # quote, which stays valid across a timeframe-only switch — only
        # _onSymChange() (a real symbol change) should ever null these.
        src = self._template_source()
        body = self._method_body(src, "_onTFChange(){")
        self.assertNotIn("this.liveMid=null", body)
        self.assertNotIn("this.bid=this.ask=null", body)

    # -- history message: symbol guard (unchanged) + new timeframe guard --
    def test_history_handler_keeps_symbol_guard(self):
        src = self._template_source()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        body = src[i:i + 2200]
        self.assertIn("if(msg.symbol&&msg.symbol!==this.currentSymbol)return;", body)

    def test_history_handler_adds_timeframe_guard_after_symbol_guard(self):
        src = self._template_source()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        # CHART-HISTORY-INSTANT-LOAD-01 grew this handler with the phase-
        # aware merge branch — window widened from 1400 to keep
        # setData( within it (was truncating it out, see that fix's
        # test_chart_history_instant_load_01.py for the merge logic's
        # own dedicated tests).
        body = src[i:i + 2200]
        self.assertIn("if(msg.timeframe&&msg.timeframe!==this.currentTF)return;", body)
        self.assertLess(
            body.index("msg.symbol!==this.currentSymbol"),
            body.index("msg.timeframe!==this.currentTF"),
        )
        # Both guards must run before setData() is ever called.
        self.assertLess(
            body.index("msg.timeframe!==this.currentTF"),
            body.index(".setData("),
        )

    # -- structural: fix lives on the per-panel instance, not globally --
    def test_reset_fix_is_scoped_to_panel_instance_not_global(self):
        # Every line touched by this fix must reference `this.` (the
        # Panel instance) — confirms multi-panel isolation is preserved:
        # nothing here writes to a page-global variable that could leak
        # between panels.
        src = self._template_source()
        sym_body = self._method_body(src, "_onSymChange(){")
        tf_body = self._method_body(src, "_onTFChange(){")
        for marker in (
            "this.candleSeries?.setData([])",
            "this.volumeSeries?.setData([])",
            "this.pxTag.textContent=",
        ):
            self.assertIn(marker, sym_body)
        for marker in ("this.candleSeries?.setData([])", "this.volumeSeries?.setData([])"):
            self.assertIn(marker, tf_body)

    def test_reset_logic_is_symbol_and_provider_agnostic(self):
        # The fix must be one unconditional code path — no BTC/ETH/Forex
        # branching — so it applies identically whether the switch is
        # crypto->crypto, forex->forex, or crypto->forex.
        src = self._template_source()
        sym_body = self._method_body(src, "_onSymChange(){")
        self.assertNotIn("includes('BTC')", sym_body)
        self.assertNotIn("includes('ETH')", sym_body)


# ── ACCEPTANCE FIX — priceLine lifecycle (CHART-STATE-REGRESSION-AUDIT) ──
# Root cause confirmed by forensic audit: this.priceLine (createPriceLine,
# attached to candleSeries — same 'right' price scale) was created ONCE in
# initChart() and only ever re-priced by a real tick
# (_updateLiveQuoteDisplay) — _onSymChange() never touched it. It kept
# showing the PREVIOUS symbol's price/title, and its stale value was
# included in the 'right' scale's autoscale computation, distorting the
# visible range for the new symbol's real candles until the first new
# tick landed. Fixed: _onSymChange() removes it immediately (never just
# re-priced to 0/NaN); _updateLiveQuoteDisplay() lazily recreates it,
# exactly once, on the first real tick for the new symbol. _onTFChange()
# deliberately untouched — same symbol, the live quote stays valid.
#
# Behavior verified directly (Node.js, outside this Django test — no JS
# runtime exists in this test suite) before writing these source-
# inspection tests: switch removes the line and clears the reference;
# first tick recreates it with the correct price/title; a second tick
# updates the SAME object (line count stays at 1, never grows).
class PriceLineLifecycleTests(SimpleTestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _method_body(self, src, method_signature, max_len=None):
        # Bounded by the NEXT top-level (2-space-indented) method
        # signature in the Panel class — never a fixed length guess,
        # which broke twice already as these methods grew comments (a
        # too-short window silently truncated real code out of the
        # snippet; a too-long window bled into unrelated later methods).
        import re
        i = src.index(method_signature)
        start = i + len(method_signature)
        m = re.search(r"\n  [A-Za-z_][A-Za-z0-9_]*\s*\(", src[start:])
        end = start + m.start() if m else (start + (max_len or 3500))
        return src[i:end]

    # 1/2 — any symbol switch removes the previous priceLine immediately
    # (the fix is one unconditional code path — BTC->GBP and AUD->ETH are
    # both covered by the same, non-symbol-specific removal below).
    def test_on_sym_change_removes_stale_price_line_immediately(self):
        src = self._template_source()
        body = self._method_body(src, "_onSymChange(){")
        self.assertIn(
            "if(this.priceLine){this.candleSeries?.removePriceLine(this.priceLine);this.priceLine=null;}",
            body,
        )
        # Must run before the WS change_symbol send (immediate, not
        # waiting for any response) — same discipline as the candle/
        # volume series reset already covered by SymbolTimeframeVisualResetTests.
        self.assertLess(
            body.index("this.candleSeries?.removePriceLine(this.priceLine)"),
            body.index("action:'change_symbol'"),
        )

    def test_on_sym_change_never_leaves_price_line_with_stale_or_zero_price(self):
        # Must null the reference, never just re-price it to 0/NaN — a
        # visible line at an invalid price is still a line participating
        # in autoscale.
        src = self._template_source()
        body = self._method_body(src, "_onSymChange(){")
        self.assertIn("this.priceLine=null", body)
        self.assertNotIn("this.priceLine.applyOptions({price:0", body)
        self.assertNotIn("this.priceLine.applyOptions({price:NaN", body)

    # 3/4/5 — first real tick for the new symbol recreates it correctly
    def test_update_live_quote_display_recreates_price_line_lazily(self):
        src = self._template_source()
        body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        self.assertIn("if(!this.priceLine){", body)
        self.assertIn(
            "this.priceLine=this.candleSeries.createPriceLine({price:px,"
            "color:'rgba(255,255,255,.4)',lineWidth:1,lineStyle:2,axisLabelVisible:true,"
            "title:this.currentSymbol.replace('/','')});",
            body,
        )
        # price=px (liveMid) and title=currentSymbol — both live, not
        # hardcoded 0 or a stale symbol.
        self.assertIn("price:px", body)
        self.assertIn("title:this.currentSymbol.replace('/','')", body)

    def test_update_live_quote_display_never_creates_before_a_valid_tick(self):
        # Guard order: liveMid==null must return BEFORE any create/update
        # branch runs — never fabricates a line without a real quote.
        src = self._template_source()
        body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        self.assertIn("if(this.liveMid==null)return;", body)
        i_guard = body.index("if(this.liveMid==null)return;")
        i_create = body.index("if(!this.priceLine){")
        self.assertLess(i_guard, i_create)

    # 6 — second/third tick updates the SAME line, never creates a new one
    def test_subsequent_ticks_update_same_line_via_apply_options(self):
        src = self._template_source()
        body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        self.assertIn("}else{", body)
        self.assertIn(
            "this.priceLine.applyOptions({price:px,title:this.currentSymbol.replace('/','')});",
            body,
        )
        # createPriceLine must appear exactly once in this method — only
        # in the `if(!this.priceLine)` branch, never in the `else`.
        self.assertEqual(body.count("createPriceLine("), 1)

    def test_candle_series_guard_before_creating(self):
        # "candleSeries existe" guard, per the authorization's explicit
        # requirement — never calls createPriceLine on a missing series.
        src = self._template_source()
        body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        self.assertIn("if(!this.candleSeries)return;", body)
        self.assertLess(
            body.index("if(!this.candleSeries)return;"),
            body.index("createPriceLine("),
        )

    # 7 — timeframe change must NOT touch priceLine at all
    def test_on_tf_change_does_not_remove_price_line(self):
        src = self._template_source()
        body = self._method_body(src, "_onTFChange(){")
        self.assertNotIn("priceLine", body)

    # 8 — multi-panel: priceLine is instance-scoped, never global
    def test_price_line_is_scoped_to_panel_instance(self):
        src = self._template_source()
        sym_body = self._method_body(src, "_onSymChange(){")
        update_body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        for marker in ("this.priceLine", "this.candleSeries"):
            self.assertIn(marker, sym_body)
            self.assertIn(marker, update_body)
        # Never a page-global variable name for this state.
        self.assertNotIn("window.priceLine", sym_body + update_body)

    # 9 — Forex/Crypto agnostic: no symbol-specific branching anywhere in
    # the priceLine lifecycle code (same code path for every symbol).
    def test_price_line_lifecycle_is_symbol_agnostic(self):
        src = self._template_source()
        sym_body = self._method_body(src, "_onSymChange(){")
        update_body = self._method_body(src, "_updateLiveQuoteDisplay(){", max_len=900)
        combined = sym_body + update_body
        self.assertNotIn("includes('BTC')", combined)
        self.assertNotIn("includes('ETH')", combined)
