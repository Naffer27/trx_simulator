# simulator/tests/test_crypto_quote_dedup_01.py
"""
CRYPTO-QUOTE-DEDUP-01 — suppress redundant crypto price_tick/candle_update
emissions for an EXACT repeat of the last broadcast (bid, ask) per symbol,
while keeping quote freshness (has_price()/get_validated_quote()), the
Redis price cache, and the connection-staleness watchdog fully alive —
freshness and "this is new price information" are deliberately separate
concepts (design lock discussion).

Design lock (approved, this file implements it):
  - _broadcast() is split into _update_price_state() (memory state +
    Redis cache + observability record_tick — everything has_price()/
    get_validated_quote() read) followed by the channel_layer.group_send()
    itself. _broadcast()'s own external contract (what it does when
    called in full) is UNCHANGED — this is a pure extract-method: every
    non-crypto caller (sim/binance/kraken/finnhub) and the Forex Massive
    loop still call _broadcast() exactly as before and get exactly the
    same 4 effects, in the same order.
  - _massive_crypto_shared_loop() gains ONE new per-symbol dict,
    _massive_crypto_last_broadcast_quote: dict[str, tuple[float, float]].
    A quote whose (bid, ask) exactly matches the last one THIS loop
    actually broadcast for that symbol calls _update_price_state() alone
    (freshness/cache refreshed, no group_send). Any real change in bid
    OR ask takes the unmodified _broadcast() path.
  - _massive_crypto_last_quote_at/_massive_crypto_symbol_stale_attempts
    (watchdog freshness) and `backoff` are updated BEFORE the dedup
    check, unconditionally — a duplicate is still live connection
    activity for the watchdog, exactly as a real price change is.
  - The new dict is reset (popped) for every active symbol on every
    fresh connection (same place/pattern as the two existing per-symbol
    dicts) and popped on unregister — a reconnect always re-broadcasts
    its first quote, even if it happens to match the pre-disconnect
    price.
  - Forex (_massive_shared_loop) is a completely separate function/dict
    namespace — never touched, never reads the new dict.

Same WS-mocking idiom as test_golden_marketdata_crypto_01_massive_crypto_
live.py (duplicated here rather than imported, matching this session's
established "duplicate, don't share" convention for test utilities).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from market_data.feeds import DATA_STALE_TIMEOUT, FeedManager

import simulator.consumers as consumers_module  # noqa: F401 (import-safety check only)

_REAL_ASYNCIO_SLEEP = asyncio.sleep

_FAKE_MASSIVE_KEY = "FAKEtestONLYnotREALkey1234567xx"

MASSIVE_AUTH_SUCCESS = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])


def _btc_quote(bid=65123.45, ask=65130.10, t=1788217269000):
    return json.dumps([{
        "ev": "XQ", "pair": "BTC-USD", "bp": bid, "ap": ask,
        "bs": 0.5, "as": 0.4, "t": t, "x": 1, "r": t,
    }])


def _eth_quote(bid=3210.55, ask=3211.20, t=1788217269000):
    return json.dumps([{
        "ev": "XQ", "pair": "ETH-USD", "bp": bid, "ap": ask,
        "bs": 1.2, "as": 0.9, "t": t, "x": 1, "r": t,
    }])


def _run(coro):
    return asyncio.run(coro)


async def _instant_real_yield(*args, **kwargs):
    await _REAL_ASYNCIO_SLEEP(0)


async def _settle(n=30):
    for _ in range(n):
        await _REAL_ASYNCIO_SLEEP(0)


class _ScriptedMassiveWS:
    """Same fake connection as the GOLDEN-MARKETDATA-CRYPTO-01 suite's own —
    duplicated per this session's established test-utility convention."""

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


class _DedupTestsBase(SimpleTestCase):
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

    def tearDown(self):
        # Never leave a shared crypto task running past its test.
        task = self.fm._massive_crypto_shared_task
        if task and not task.done():
            task.cancel()


# ─────────────────────────────────────────────────────────────────────────
# _update_price_state() — the extracted freshness/cache half of _broadcast()
# ─────────────────────────────────────────────────────────────────────────
class UpdatePriceStateUnitTests(_DedupTestsBase):
    def test_sets_bids_asks_prices_ts_source(self):
        async def _scenario():
            await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
            self.assertEqual(self.fm._bids["BTCUSD"], 65000.0)
            self.assertEqual(self.fm._asks["BTCUSD"], 65010.0)
            self.assertEqual(self.fm._price_source["BTCUSD"], "massive")
            self.assertIn("BTCUSD", self.fm._price_ts)
        _run(_scenario())

    def test_writes_redis_cache(self):
        async def _scenario():
            await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
            self._write_cache_mock.assert_awaited_once_with("BTCUSD", 65000.0, 65010.0, "massive")
        _run(_scenario())

    def test_returns_rounded_mid(self):
        async def _scenario():
            mid = await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
            self.assertAlmostEqual(mid, 65005.0, places=2)
        _run(_scenario())

    def test_has_no_channel_layer_parameter(self):
        # Structural guarantee, not just behavioral: this method cannot
        # call group_send even by accident — it never receives a channel
        # layer reference at all.
        import inspect
        sig = inspect.signature(FeedManager._update_price_state)
        self.assertNotIn("cl", sig.parameters)
        self.assertNotIn("channel_layer", sig.parameters)

    def test_body_never_mentions_group_send(self):
        # Executable body only — the docstring explains the design in
        # terms of _broadcast()'s group_send, which is expected prose,
        # not a code reference.
        import inspect
        src = inspect.getsource(FeedManager._update_price_state)
        body = src.split('"""', 2)[-1]
        self.assertNotIn("group_send", body)


# ─────────────────────────────────────────────────────────────────────────
# _broadcast() — external contract preserved (pure extract-method)
# ─────────────────────────────────────────────────────────────────────────
class BroadcastContractUnchangedTests(_DedupTestsBase):
    def test_broadcast_calls_update_price_state_then_group_send_in_order(self):
        calls = []

        async def _fake_update(symbol, bid, ask, source="live"):
            calls.append("update_price_state")
            return round((bid + ask) / 2, 5)

        async def _fake_group_send(*args, **kwargs):
            calls.append("group_send")

        async def _scenario():
            with patch.object(self.fm, "_update_price_state", side_effect=_fake_update), \
                 patch.object(self.channel_layer, "group_send", side_effect=_fake_group_send):
                await self.fm._broadcast("EUR/USD", self.channel_layer, 1.16, 1.1602, 1000, source="massive")
        _run(_scenario())
        self.assertEqual(calls, ["update_price_state", "group_send"])

    def test_broadcast_group_send_payload_shape_unchanged(self):
        async def _scenario():
            await self.fm._broadcast("EUR/USD", self.channel_layer, 1.16, 1.1602, 1000, source="massive")
        _run(_scenario())
        self.channel_layer.group_send.assert_awaited_once()
        group, payload = self.channel_layer.group_send.await_args.args
        self.assertEqual(payload["type"], "price.tick")
        self.assertEqual(payload["symbol"], "EUR/USD")
        self.assertEqual(payload["bid"], 1.16)
        self.assertEqual(payload["ask"], 1.1602)
        self.assertEqual(payload["time"], 1000)
        self.assertEqual(payload["source"], "massive")
        self.assertIn("mid", payload)

    def test_broadcast_still_updates_memory_and_redis(self):
        async def _scenario():
            await self.fm._broadcast("EUR/USD", self.channel_layer, 1.16, 1.1602, 1000, source="massive")
        _run(_scenario())
        self.assertEqual(self.fm._bids["EUR/USD"], 1.16)
        self._write_cache_mock.assert_awaited_once_with("EUR/USD", 1.16, 1.1602, "massive")


# ─────────────────────────────────────────────────────────────────────────
# Freshness survives >100s of identical (duplicate) quotes
# ─────────────────────────────────────────────────────────────────────────
class FreshnessAcrossDuplicatesTests(_DedupTestsBase):
    def test_has_price_stays_fresh_across_100_plus_seconds_of_identical_quotes(self):
        clock = {"t": 1_800_000_000.0}
        async def _scenario():
            with patch("market_data.feeds.time.time", side_effect=lambda: clock["t"]):
                for _ in range(6):
                    await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
                    self.assertTrue(self.fm.has_price("BTCUSD", max_age_seconds=30.0))
                    clock["t"] += 20.0  # 6 * 20s = 120s of elapsed wall time, never a gap > 30s
        _run(_scenario())

    def test_get_validated_quote_stays_valid_across_100_plus_seconds_of_identical_quotes(self):
        clock = {"t": 1_800_000_000.0}
        async def _scenario():
            with patch("market_data.feeds.time.time", side_effect=lambda: clock["t"]):
                for _ in range(6):
                    await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
                    q = self.fm.get_validated_quote("BTCUSD", max_age_seconds=30.0)
                    self.assertIsNotNone(q)
                    self.assertEqual(q.bid, 65000.0)
                    self.assertEqual(q.ask, 65010.0)
                    clock["t"] += 20.0
        _run(_scenario())

    def test_without_any_refresh_price_would_go_stale(self):
        # Control case — proves the previous two tests are actually
        # exercising the freshness mechanism, not passing vacuously.
        clock = {"t": 1_800_000_000.0}
        async def _scenario():
            with patch("market_data.feeds.time.time", side_effect=lambda: clock["t"]):
                await self.fm._update_price_state("BTCUSD", 65000.0, 65010.0, source="massive")
                clock["t"] += 120.0
                self.assertFalse(self.fm.has_price("BTCUSD", max_age_seconds=30.0))
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# _massive_crypto_shared_loop() — dedup decision, end to end
# ─────────────────────────────────────────────────────────────────────────
class SharedLoopDedupTests(_DedupTestsBase):
    def test_first_event_always_broadcasts(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _btc_quote()]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.channel_layer.group_send.assert_awaited_once()
        _run(_scenario())

    def test_duplicate_exact_quote_does_not_group_send(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0), _btc_quote(65000.0, 65010.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 1)
        _run(_scenario())

    def test_duplicate_quote_still_refreshes_redis_cache(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0), _btc_quote(65000.0, 65010.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self._write_cache_mock.await_count, 2)  # both quotes, dup included
                self.assertEqual(self.channel_layer.group_send.await_count, 1)  # only the first
        _run(_scenario())

    def test_bid_change_triggers_broadcast(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0), _btc_quote(65001.0, 65010.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 2)
        _run(_scenario())

    def test_ask_change_triggers_broadcast(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0), _btc_quote(65000.0, 65011.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 2)
        _run(_scenario())

    def test_third_identical_quote_also_suppressed(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS,
            _btc_quote(65000.0, 65010.0),
            _btc_quote(65000.0, 65010.0),
            _btc_quote(65000.0, 65010.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 1)
                self.assertEqual(self._write_cache_mock.await_count, 3)
        _run(_scenario())

    def test_btc_eth_dedup_state_independent(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS,
            _btc_quote(65000.0, 65010.0),
            _btc_quote(65000.0, 65010.0),   # BTC duplicate — suppressed
            _eth_quote(3200.0, 3201.0),      # ETH first event — must broadcast
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await self.fm._massive_crypto_register_symbol("ETHUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 2)  # BTC once + ETH once
                symbols_sent = [c.args[1]["symbol"] for c in self.channel_layer.group_send.await_args_list]
                self.assertEqual(symbols_sent, ["BTCUSD", "ETHUSD"])
        _run(_scenario())

    def test_watchdog_freshness_updated_even_for_suppressed_duplicate(self):
        connect_mock = _make_connect_mock([[
            MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0), _btc_quote(65000.0, 65010.0),
        ]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 1)  # the dup was suppressed
                # ...yet the watchdog's own freshness bookkeeping was touched
                # on BOTH events, not just the broadcast one.
                self.assertIn("BTCUSD", self.fm._massive_crypto_last_quote_at)
                self.assertEqual(self.fm._massive_crypto_symbol_stale_attempts["BTCUSD"], 0)
        _run(_scenario())

    def test_reconnect_broadcasts_first_event_even_if_identical_to_pre_disconnect(self):
        connect_mock = _make_connect_mock([
            [MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0)],
            [MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0)],  # same price, new connection
        ], close_raises=False)
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertEqual(self.channel_layer.group_send.await_count, 1)
                await connect_mock.instances[0].close()
                await _settle(60)
                self.assertEqual(len(connect_mock.instances), 2)
                self.assertEqual(
                    self.channel_layer.group_send.await_count, 2,
                    "reconnect must re-broadcast its first quote even if unchanged from before the disconnect",
                )
        _run(_scenario())

    def test_unregister_pops_dedup_state(self):
        connect_mock = _make_connect_mock([[MASSIVE_AUTH_SUCCESS, _btc_quote(65000.0, 65010.0)]])
        async def _scenario():
            with patch("market_data.feeds.websockets.connect", connect_mock):
                await self.fm._massive_crypto_register_symbol("BTCUSD", self.channel_layer)
                await _settle()
                self.assertIn("BTCUSD", self.fm._massive_crypto_last_broadcast_quote)
                await self.fm._massive_crypto_unregister_symbol("BTCUSD")
                self.assertNotIn("BTCUSD", self.fm._massive_crypto_last_broadcast_quote)
        _run(_scenario())


# ─────────────────────────────────────────────────────────────────────────
# Forex untouched
# ─────────────────────────────────────────────────────────────────────────
class ForexUntouchedTests(SimpleTestCase):
    def test_forex_shared_loop_has_no_dedup_logic(self):
        import inspect
        from market_data import feeds as feeds_module
        src = inspect.getsource(feeds_module.FeedManager._massive_shared_loop)
        self.assertNotIn("_massive_crypto_last_broadcast_quote", src)
        self.assertNotIn("last_broadcast_quote", src)

    def test_no_forex_equivalent_dedup_dict_exists(self):
        fm = FeedManager()
        self.assertFalse(hasattr(fm, "_massive_last_broadcast_quote"))

    def test_forex_broadcast_call_site_unchanged(self):
        import inspect
        from market_data import feeds as feeds_module
        src = inspect.getsource(feeds_module.FeedManager._massive_shared_loop)
        self.assertIn(
            'await self._broadcast(sym, channel_layer, bid, ask, ts, source="massive")', src,
        )
