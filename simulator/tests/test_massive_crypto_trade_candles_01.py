# simulator/tests/test_massive_crypto_trade_candles_01.py
"""
MASSIVE-CRYPTO-TRADE-CANDLES-01 — crypto candles are built from Massive
TRADES (XT), never from Massive QUOTES (XQ) anymore. Execution/spread/
PnL/margin/risk/SL-TP/Redis price cache stay on quotes, unchanged.

Verified live before implementing (MASSIVE-CRYPTO-TRADES-LIVE-PROBE-01):
XT.BTC-USD/XT.ETH-USD are permitted by the plan, event shape
{"ev":"XT","pair":...,"p":price,"s":size,"t":ts_ms,...}, and REST
aggregates bucket boundaries (BTCUSD 1m/15m, ETHUSD 1m) align exactly
with (ts // tf_sec) * tf_sec before this block was implemented.

Design lock, confirmed contract:
  XQ (quote) -> _broadcast() -> price.tick -> execution/spread/PnL/
                margin/risk/SL-TP/Redis price cache/quote dedup —
                UNCHANGED. Never builds a crypto candle anymore.
  XT (trade) -> _broadcast_trade() -> price.trade -> price_trade()
                (consumers.py) -> candle_new/candle_update/volume_update
                — NEVER touches bid/ask/execution state, NEVER calls
                _broadcast()/_update_price_state(), NEVER writes Redis.
  Bucket with zero trades -> no candle_new/candle_update at all (no
                synthetic bar, ever).
  Forex -> completely untouched (still quote/mid-driven via _on_tick(),
                exactly as before this block).

Same WS-mocking idiom as the other Massive-crypto test files (duplicated
per this session's established "duplicate, don't share" test-utility
convention) for feeds.py; same TradingConsumer.__new__ bare-instance
convention already used across this project's consumer-level tests for
consumers.py.
"""
import asyncio
import json
import time as _time_module
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from market_data.feeds import FeedManager, _MASSIVE_CRYPTO_ENABLED_SYMBOLS
import simulator.consumers as consumers_module
from simulator.consumers import TradingConsumer, tf_seconds

_FAKE_MASSIVE_KEY = "FAKEtestONLYnotREALkey1234567xx"
_REAL_ASYNCIO_SLEEP = asyncio.sleep

MASSIVE_AUTH_SUCCESS = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])


def _xt_event(pair="BTC-USD", price=80000.0, size=0.001, t_ms=1788484440000):
    return json.dumps([{"ev": "XT", "pair": pair, "p": price, "s": size, "t": t_ms,
                         "c": [2], "i": "test-trade-id", "x": 1, "r": t_ms}])


def _run(coro):
    return asyncio.run(coro)


async def _instant_real_yield(*args, **kwargs):
    await _REAL_ASYNCIO_SLEEP(0)


async def _settle(n=30):
    for _ in range(n):
        await _REAL_ASYNCIO_SLEEP(0)


class _ScriptedMassiveWS:
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


class _FeedTestsBase(SimpleTestCase):
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
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", _FAKE_MASSIVE_KEY)
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)

    def tearDown(self):
        task = self.fm._massive_crypto_shared_task
        if task and not task.done():
            task.cancel()


# ─────────────────────────────────────────────────────────────────────────
# feeds.py — _broadcast_trade() never touches execution state
# ─────────────────────────────────────────────────────────────────────────
class BroadcastTradeUnitTests(_FeedTestsBase):
    def test_broadcast_trade_only_group_sends_price_trade(self):
        async def _scenario():
            await self.fm._broadcast_trade("BTCUSD", self.channel_layer, 80000.0, 0.001, 1000)
        _run(_scenario())
        self.channel_layer.group_send.assert_awaited_once()
        group, payload = self.channel_layer.group_send.await_args.args
        self.assertEqual(payload["type"], "price.trade")
        self.assertEqual(payload["symbol"], "BTCUSD")
        self.assertEqual(payload["price"], 80000.0)
        self.assertEqual(payload["size"], 0.001)
        self.assertEqual(payload["time"], 1000)

    def test_broadcast_trade_never_writes_redis_or_state(self):
        async def _scenario():
            await self.fm._broadcast_trade("BTCUSD", self.channel_layer, 80000.0, 0.001, 1000)
        _run(_scenario())
        self._write_cache_mock.assert_not_awaited()
        self.assertNotIn("BTCUSD", self.fm._bids)
        self.assertNotIn("BTCUSD", self.fm._asks)
        self.assertNotIn("BTCUSD", self.fm._price_ts)

    def test_broadcast_trade_never_calls_broadcast_or_update_price_state(self):
        async def _scenario():
            with patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast, \
                 patch.object(self.fm, "_update_price_state", new=AsyncMock()) as mock_ups:
                await self.fm._broadcast_trade("BTCUSD", self.channel_layer, 80000.0, 0.001, 1000)
                mock_broadcast.assert_not_awaited()
                mock_ups.assert_not_awaited()
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# feeds.py — WS subscribe now covers both channels
# ─────────────────────────────────────────────────────────────────────────
class SubscribeBothChannelsTests(_FeedTestsBase):
    def test_ws_param_covers_both_xq_and_xt(self):
        from market_data.feeds import _massive_crypto_ws_param
        self.assertEqual(_massive_crypto_ws_param("BTCUSD"), "XQ.BTC-USD,XT.BTC-USD")

    def test_register_symbol_subscribes_both_channels(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                sub_msgs = [json.loads(s) for s in connect_mock.instances[0].sent if json.loads(s).get("action") == "subscribe"]
                self.assertEqual(len(sub_msgs), 1)
                self.assertEqual(sub_msgs[0]["params"], "XQ.BTC-USD,XT.BTC-USD")
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# feeds.py — _massive_crypto_shared_loop() XT dispatch
# ─────────────────────────────────────────────────────────────────────────
class SharedLoopXtDispatchTests(_FeedTestsBase):
    def test_xt_event_dispatches_to_broadcast_trade(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _xt_event("BTC-USD", 80000.0, 0.001, 1788484440000)]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast_trade", new=AsyncMock()) as mock_bt:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_bt.assert_awaited_once()
                args = mock_bt.await_args.args
                self.assertEqual(args[0], "BTCUSD")
                self.assertAlmostEqual(args[2], 80000.0)
                self.assertAlmostEqual(args[3], 0.001)
                self.assertEqual(args[4], 1788484440)  # ms -> s
        _run(_scenario())

    def test_xt_event_never_calls_broadcast_or_update_price_state(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _xt_event("BTC-USD", 80000.0, 0.001)]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast, \
                 patch.object(self.fm, "_update_price_state", new=AsyncMock()) as mock_ups:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_not_awaited()
                mock_ups.assert_not_awaited()
        _run(_scenario())

    def test_xq_event_still_dispatches_to_broadcast_unaffected(self):
        xq_event = json.dumps([{"ev": "XQ", "pair": "BTC-USD", "bp": 80000.0, "ap": 80001.0, "t": 1788484440000}])
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, xq_event]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast", new=AsyncMock()) as mock_broadcast, \
                 patch.object(self.fm, "_broadcast_trade", new=AsyncMock()) as mock_bt:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_broadcast.assert_awaited_once()
                mock_bt.assert_not_awaited()
        _run(_scenario())

    def test_malformed_xt_dropped_never_broadcasts(self):
        bad_event = json.dumps([{"ev": "XT", "pair": "BTC-USD", "t": 1788484440000}])  # missing "p"
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, bad_event]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast_trade", new=AsyncMock()) as mock_bt:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_bt.assert_not_awaited()
        _run(_scenario())

    def test_implausible_xt_price_rejected(self):
        # Capa A magnitude plausibility, reused via _validate_quote_values(sym, price, price).
        bad_event = _xt_event("BTC-USD", price=1.0, size=0.001)  # far outside BTCUSD's band
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, bad_event]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast_trade", new=AsyncMock()) as mock_bt:
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                mock_bt.assert_not_awaited()
        _run(_scenario())

    def test_xt_event_does_not_touch_xq_freshness_state(self):
        # A trade must never mask genuine quote staleness.
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _xt_event("BTC-USD", 80000.0, 0.001)]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts.get("BTCUSD"), 0)
        _run(_scenario())

    def test_eth_xt_event_dispatches_correctly(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _xt_event("ETH-USD", 2500.0, 0.02, 1788484440000)]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock), \
                 patch.object(self.fm, "_broadcast_trade", new=AsyncMock()) as mock_bt:
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                mock_bt.assert_awaited_once()
                self.assertEqual(mock_bt.await_args.args[0], "ETHUSD")
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — price_tick() no longer drives crypto candles
# ─────────────────────────────────────────────────────────────────────────
def _tick_consumer(symbol="EUR/USD"):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = symbol
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {"balance": 10000.0, "spread_pips": 0.0, "commercial_pricing_fields": {}}
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


def _tick_event(symbol, bid, ask, ts=1_700_000_000, source="massive"):
    return {"symbol": symbol, "bid": bid, "ask": ask,
            "mid": round((bid + ask) / 2, 5), "time": ts, "source": source}


class PriceTickCryptoGuardTests(SimpleTestCase):
    def test_crypto_symbol_never_calls_on_tick(self):
        c = _tick_consumer(symbol="BTCUSD")
        _run(c.price_tick(_tick_event("BTCUSD", 80000.0, 80001.0)))
        c._on_tick.assert_not_awaited()

    def test_eth_symbol_never_calls_on_tick(self):
        c = _tick_consumer(symbol="ETHUSD")
        _run(c.price_tick(_tick_event("ETHUSD", 2500.0, 2500.5)))
        c._on_tick.assert_not_awaited()

    def test_forex_symbol_still_calls_on_tick(self):
        c = _tick_consumer(symbol="EUR/USD")
        _run(c.price_tick(_tick_event("EUR/USD", 1.1000, 1.1002)))
        c._on_tick.assert_awaited_once()
        args = c._on_tick.await_args
        self.assertEqual(args.args[0], "EUR/USD")

    def test_source_inspection_guard_present(self):
        import inspect
        src = inspect.getsource(TradingConsumer.price_tick)
        self.assertIn("if symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:", src)
        self.assertIn("await self._on_tick(symbol, mid, volume=0.0, ts=ts)", src)


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — price_trade() aggregator
# ─────────────────────────────────────────────────────────────────────────
def _trade_consumer(symbol="BTCUSD", timeframe="1m"):
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._trade_agg = {}
    c._last_bar_time = {}
    c.bid = None
    c.ask = None
    c._bid_state = {}
    c._ask_state = {}
    c._price_state = {}
    c.send_json = AsyncMock()
    return c


def _all_sent(mock, msg_type):
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] == msg_type]


class PriceTradeAggregatorTests(SimpleTestCase):
    def test_wrong_symbol_ignored(self):
        c = _trade_consumer(symbol="BTCUSD")
        _run(c.price_trade({"symbol": "ETHUSD", "price": 2500.0, "size": 0.01, "time": 1000}))
        c.send_json.assert_not_awaited()

    def test_first_trade_is_open_high_low_close(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 5}))
        candle = _all_sent(c.send_json, "candle_new")[0]
        self.assertEqual(candle["data"]["open"], 80000.0)
        self.assertEqual(candle["data"]["high"], 80000.0)
        self.assertEqual(candle["data"]["low"], 80000.0)
        self.assertEqual(candle["data"]["close"], 80000.0)
        self.assertEqual(candle["data"]["time"], bucket)

    def test_high_low_correct_across_multiple_trades(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 1}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80050.0, "size": 0.001, "time": bucket + 2}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 79950.0, "size": 0.001, "time": bucket + 3}))
        last = _all_sent(c.send_json, "candle_update")[-1]
        self.assertEqual(last["data"]["high"], 80050.0)
        self.assertEqual(last["data"]["low"], 79950.0)

    def test_close_is_last_trade(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 1}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80025.0, "size": 0.001, "time": bucket + 2}))
        last = _all_sent(c.send_json, "candle_update")[-1]
        self.assertEqual(last["data"]["close"], 80025.0)

    def test_volume_is_sum_of_sizes(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 1}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80001.0, "size": 0.002, "time": bucket + 2}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80002.0, "size": 0.0005, "time": bucket + 3}))
        vol_msgs = _all_sent(c.send_json, "volume_update")
        self.assertAlmostEqual(vol_msgs[-1]["value"], 0.0035, places=6)

    def test_bucket_rollover_sends_candle_new_not_update(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket1 = (1_700_000_000 // 60) * 60
        bucket2 = bucket1 + 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket1 + 1}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80005.0, "size": 0.001, "time": bucket1 + 2}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80100.0, "size": 0.001, "time": bucket2 + 1}))
        new_events = _all_sent(c.send_json, "candle_new")
        self.assertEqual(len(new_events), 2)
        self.assertEqual(new_events[0]["data"]["time"], bucket1)
        self.assertEqual(new_events[1]["data"]["time"], bucket2)
        self.assertEqual(new_events[1]["data"]["open"], 80100.0)  # fresh OHLC, not carried over

    def test_no_synthetic_candle_for_empty_bucket(self):
        # A gap between two trades spanning an entire empty bucket must
        # never fabricate a bar for the skipped bucket.
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket1 = (1_700_000_000 // 60) * 60
        bucket3 = bucket1 + 120  # bucket2 (bucket1+60) never gets a trade
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket1 + 1}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80200.0, "size": 0.001, "time": bucket3 + 1}))
        new_events = _all_sent(c.send_json, "candle_new")
        self.assertEqual(len(new_events), 2)  # only 2 real bars — bucket1 and bucket3, never a bucket2
        self.assertEqual([e["data"]["time"] for e in new_events], [bucket1, bucket3])

    def test_timestamp_used_as_is_already_seconds(self):
        # feeds.py already converts ms->s before this ever runs — this
        # method must never divide by 1000 again.
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 5}))
        candle = _all_sent(c.send_json, "candle_new")[0]
        self.assertEqual(candle["data"]["time"], bucket)

    def test_never_touches_bid_ask_or_execution_state(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        bucket = (1_700_000_000 // 60) * 60
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": bucket + 1}))
        self.assertIsNone(c.bid)
        self.assertIsNone(c.ask)
        self.assertEqual(c._bid_state, {})
        self.assertEqual(c._ask_state, {})
        self.assertEqual(c._price_state, {})
        self.assertEqual(_all_sent(c.send_json, "tick"), [])

    def test_separate_accumulator_from_quote_agg(self):
        import inspect
        src = inspect.getsource(TradingConsumer.price_trade)
        self.assertIn("self._trade_agg", src)
        self.assertNotIn("self._agg[", src)
        self.assertNotIn("self._agg.get", src)

    def test_bucket_size_matches_current_timeframe(self):
        c = _trade_consumer(symbol="BTCUSD", timeframe="1m")
        self.assertEqual(tf_seconds("1m"), 60)
        _run(c.price_trade({"symbol": "BTCUSD", "price": 80000.0, "size": 0.001, "time": 1_700_000_030}))
        self.assertEqual(c._trade_agg["BTCUSD"]["tf_sec"], 60)


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — lifecycle wiring (symbol/timeframe switch reset)
# ─────────────────────────────────────────────────────────────────────────
class TradeAggResetWiringTests(SimpleTestCase):
    def test_change_symbol_resets_trade_agg(self):
        import inspect
        src = inspect.getsource(TradingConsumer.receive)
        i = src.index('if act == "change_symbol":')
        j = src.index('elif act == "change_timeframe":')
        block = src[i:j]
        self.assertIn("self._reset_trade_agg(new_sym)", block)

    def test_change_timeframe_resets_trade_agg(self):
        import inspect
        src = inspect.getsource(TradingConsumer.receive)
        i = src.index('elif act == "change_timeframe":')
        j = src.index('elif act == "load_history":')
        block = src[i:j]
        self.assertIn("self._reset_trade_agg(self.symbol)", block)

    def test_connect_initializes_trade_agg(self):
        import inspect
        src = inspect.getsource(TradingConsumer.connect)
        self.assertIn("self._trade_agg = {}", src)


# ─────────────────────────────────────────────────────────────────────────
# Forex untouched
# ─────────────────────────────────────────────────────────────────────────
class ForexUntouchedTests(SimpleTestCase):
    def test_on_tick_body_unchanged(self):
        import inspect
        src = inspect.getsource(TradingConsumer._on_tick)
        # Same historic guard/logic this method has always had — never
        # touched by this block.
        self.assertIn('if symbol in _KLINE_SYMBOLS and symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:', src)
        self.assertNotIn("_trade_agg", src)
        self.assertNotIn("price_trade", src)

    def test_massive_forex_shared_loop_has_no_xt_handling(self):
        import inspect
        from market_data import feeds as feeds_module
        src = inspect.getsource(feeds_module.FeedManager._massive_shared_loop)
        self.assertNotIn('"XT"', src)
        self.assertNotIn("_broadcast_trade", src)

    def test_massive_sym_forex_ws_param_unaffected(self):
        from market_data.feeds import _massive_ws_param
        self.assertEqual(_massive_ws_param("EUR/USD"), "C.EUR/USD")
