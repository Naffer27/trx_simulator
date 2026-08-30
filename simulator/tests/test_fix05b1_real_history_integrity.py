# simulator/tests/test_fix05b1_real_history_integrity.py
"""
FIX-05B.1 — Real History Integrity / Closed Candles / No Synthetic History.

Design lock: FIX-05B.1 (this block). Contract:
  - generate_history() returns list[dict] (real, closed candles only) or
    None (no real history available) — NEVER a synthetic/random-walk series.
  - market_data.feeds._is_closed()/_closed_only() are the single authority
    for excluding a still-forming bar from any provider's response.
  - _send_bridge_candle() no longer exists — no flat synthetic candle is
    ever fabricated to bridge history and live.
  - history_unavailable is the explicit failure signal (reason=
    "no_real_history" for symbols with no real provider configured today,
    "provider_unavailable" for a kline symbol whose provider chain failed).
  - None of this touches FIX-05C's liveMid contract, live ticks, trading,
    pricing, or the WebSocket connection lifecycle.

All tests are self-contained (no network) — provider responses are mocked
at the urllib.request.urlopen boundary (fallback-chain tests) or by
mocking self._feed.fetch_kline_history directly (generate_history tests).
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase

from simulator.consumers import TradingConsumer, tf_seconds
from market_data.feeds import FeedManager, _is_closed, _closed_only
from market_data.symbol_specs import kline_symbols


def _run(coro):
    return asyncio.run(coro)


def _bare_history_consumer(symbol: str = "BTCUSD", timeframe: str = "1m") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._feed = MagicMock()
    c._feed.fetch_kline_history = AsyncMock(return_value=[])
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c.account = {}
    c.send_json = AsyncMock()
    return c


def _bar(t: int, o: float = 100.0, h: float = 101.0, l: float = 99.0,
         c: float = 100.5, v: float = 1.0) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _kraken_row(open_time_s: int, o="100", h="101", l="99", c="100.5", vwap="100.4", v="1.0") -> list:
    return [open_time_s, o, h, l, c, vwap, v, 1]


def _mock_response(body: bytes):
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = body
    return m


def _sent_types(mock: AsyncMock) -> list[str]:
    return [call.args[0]["type"] for call in mock.await_args_list]


def _sent(mock: AsyncMock, msg_type: str) -> "dict | None":
    for call in mock.await_args_list:
        if call.args[0]["type"] == msg_type:
            return call.args[0]
    return None


# ─────────────────────────────────────────────────────────────────────────
# 1/2. BTC/ETH real history accepted
# ─────────────────────────────────────────────────────────────────────────
class RealHistoryAcceptedTests(TestCase):
    def test_btc_real_history_accepted(self):
        now = int(time.time())
        tf_sec = tf_seconds("1m")
        closed_bar = _bar((now // tf_sec) * tf_sec - tf_sec, c=50000.0)
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        c._feed.fetch_kline_history.return_value = [closed_bar]
        result = _run(c.generate_history("BTCUSD", "1m", bars=240))
        self.assertEqual(result, [closed_bar])
        self.assertEqual(c._price_state["BTCUSD"], 50000.0)

    def test_eth_real_history_accepted(self):
        now = int(time.time())
        tf_sec = tf_seconds("1m")
        closed_bar = _bar((now // tf_sec) * tf_sec - tf_sec, c=3000.0)
        c = _bare_history_consumer(symbol="ETHUSD", timeframe="1m")
        c._feed.fetch_kline_history.return_value = [closed_bar]
        result = _run(c.generate_history("ETHUSD", "1m", bars=240))
        self.assertEqual(result, [closed_bar])
        self.assertEqual(c._price_state["ETHUSD"], 3000.0)


# ─────────────────────────────────────────────────────────────────────────
# 3. Binance -> Kraken fallback preserved (real fetch_kline_history, no
#    network — urllib.request.urlopen mocked so the actual 3-tier chain
#    inside feeds.py, untouched by this block, is exercised end-to-end).
# ─────────────────────────────────────────────────────────────────────────
class ProviderFallbackPreservedTests(TestCase):
    def test_binance_failure_falls_through_to_kraken(self):
        fm = FeedManager()
        now = int(time.time())
        kraken_body = json.dumps({
            "result": {"XXBTZUSD": [_kraken_row(now - 120), _kraken_row(now - 60)]},
            "error": [],
        }).encode()

        call_count = {"n": 0}

        def _urlopen(req, timeout=10):
            call_count["n"] += 1
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "kraken.com" in url:
                return _mock_response(kraken_body)
            raise OSError("simulated network failure")

        with patch("market_data.feeds.urllib.request.urlopen", side_effect=_urlopen):
            bars = _run(fm.fetch_kline_history("BTCUSD", interval="1m", limit=10))
        self.assertTrue(call_count["n"] >= 3)  # Binance US, Binance com, Kraken all attempted
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["close"], 100.5)

    def test_all_providers_fail_returns_empty_list(self):
        fm = FeedManager()
        with patch("market_data.feeds.urllib.request.urlopen", side_effect=OSError("down")):
            bars = _run(fm.fetch_kline_history("BTCUSD", interval="1m", limit=10))
        self.assertEqual(bars, [])


# ─────────────────────────────────────────────────────────────────────────
# 4/14. All provider failures (any timeframe, including 1s) -> history_unavailable
# ─────────────────────────────────────────────────────────────────────────
class AllProviderFailureTests(TestCase):
    def test_all_provider_failures_returns_none(self):
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        c._feed.fetch_kline_history.return_value = []
        result = _run(c.generate_history("BTCUSD", "1m", bars=240))
        self.assertIsNone(result)

    def test_1s_all_provider_failures_returns_none_no_special_case(self):
        # No `if timeframe == "1s"` branch exists anywhere in generate_history
        # (confirmed structurally in NoSyntheticCodeTests below) — this proves
        # the SAME generic path handles 1s correctly without one.
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1s")
        c._feed.fetch_kline_history.return_value = []
        result = _run(c.generate_history("BTCUSD", "1s", bars=240))
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────
# 5/6/7/8. No random.Random, no synthetic OHLC, no bridge candle, no call-sites
# ─────────────────────────────────────────────────────────────────────────
class NoSyntheticCodeTests(SimpleTestCase):
    def test_no_random_random_in_generate_history(self):
        import inspect
        src = inspect.getsource(TradingConsumer.generate_history)
        self.assertNotIn("random.Random(", src)
        self.assertNotIn("rnd.random()", src)
        self.assertNotIn('if timeframe == "1s"', src)
        self.assertNotIn("if timeframe=='1s'", src)

    def test_send_bridge_candle_does_not_exist(self):
        self.assertFalse(hasattr(TradingConsumer, "_send_bridge_candle"))

    def test_no_bridge_candle_call_sites_in_consumers(self):
        import inspect
        from simulator import consumers as consumers_module
        src = inspect.getsource(consumers_module)
        self.assertNotIn("_send_bridge_candle", src)

    def test_history_call_sites_dispatch_via_single_helper(self):
        import inspect
        from simulator import consumers as consumers_module
        full_src = inspect.getsource(consumers_module)
        # def _send_history_or_unavailable( + 3 call-sites (change_symbol,
        # change_timeframe, load_history) = 4 occurrences of the call form.
        self.assertEqual(full_src.count("_send_history_or_unavailable("), 4)
        self.assertEqual(full_src.count("async def _send_history_or_unavailable"), 1)


# ─────────────────────────────────────────────────────────────────────────
# 9/10/11/12/13. Closed-candle filter — pure function, both provider shapes
# ─────────────────────────────────────────────────────────────────────────
class ClosedCandleFilterTests(SimpleTestCase):
    def test_binance_style_open_candle_excluded(self):
        now = 1_000_000
        tf_sec = 60
        bars = [_bar(now - 120), _bar(now - 60), _bar(now)]  # last one still open
        result = _closed_only(bars, tf_sec, now_sec=now)
        self.assertEqual(len(result), 2)
        self.assertNotIn(_bar(now), result)

    def test_kraken_style_open_candle_excluded(self):
        now = 2_000_000
        tf_sec = 300
        bars = [_bar(now - 600), _bar(now - 300), _bar(now - 100)]  # last still forming
        result = _closed_only(bars, tf_sec, now_sec=now)
        self.assertEqual([b["time"] for b in result], [now - 600, now - 300])

    def test_closed_candles_retained(self):
        now = 1_000_000
        tf_sec = 60
        bars = [_bar(now - 180), _bar(now - 120), _bar(now - 60)]
        result = _closed_only(bars, tf_sec, now_sec=now)
        self.assertEqual(len(result), 3)

    def test_exact_close_boundary_is_closed_inclusive(self):
        # open_time + tf_seconds == now -> closed (per design lock §3/§15).
        self.assertTrue(_is_closed(candle_open_time_sec=940, tf_seconds_value=60, now_sec=1000))

    def test_never_pads_result_back_up_to_requested_count(self):
        now = 1_000_000
        tf_sec = 60
        bars = [_bar(now - 120), _bar(now)]  # 1 closed, 1 open, out of 2 "requested"
        result = _closed_only(bars, tf_sec, now_sec=now)
        self.assertEqual(len(result), 1)  # never padded to 2


# ─────────────────────────────────────────────────────────────────────────
# 15. Forex/no-provider symbols -> no_real_history, zero fetch attempted
# ─────────────────────────────────────────────────────────────────────────
class ForexNoRealHistoryTests(TestCase):
    def test_symbol_choices_match_registry_kline_membership(self):
        # Sanity-checks the premise of every test in this class/file against
        # the real, current registry — not a hardcoded assumption.
        self.assertIn("BTCUSD", kline_symbols())
        self.assertIn("ETHUSD", kline_symbols())
        self.assertNotIn("EUR/USD", kline_symbols())

    def test_forex_symbol_returns_none_without_calling_feed(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        result = _run(c.generate_history("EUR/USD", "1m", bars=240))
        self.assertIsNone(result)
        c._feed.fetch_kline_history.assert_not_called()

    def test_forex_dispatches_no_real_history_reason(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        _run(c._send_history_or_unavailable("EUR/USD", "1m", None))
        msg = _sent(c.send_json, "history_unavailable")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["reason"], "no_real_history")
        self.assertEqual(msg["symbol"], "EUR/USD")
        self.assertEqual(msg["timeframe"], "1m")

    def test_kline_symbol_failure_dispatches_provider_unavailable_reason(self):
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        _run(c._send_history_or_unavailable("BTCUSD", "1m", None))
        msg = _sent(c.send_json, "history_unavailable")
        self.assertEqual(msg["reason"], "provider_unavailable")

    def test_history_present_sends_history_not_unavailable(self):
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        _run(c._send_history_or_unavailable("BTCUSD", "1m", [_bar(1)]))
        self.assertIsNone(_sent(c.send_json, "history_unavailable"))
        msg = _sent(c.send_json, "history")
        self.assertEqual(msg["data"], [_bar(1)])


# ─────────────────────────────────────────────────────────────────────────
# 16/17. history_unavailable does not close the WS / does not touch trading
# ─────────────────────────────────────────────────────────────────────────
class HistoryUnavailableSafetyTests(TestCase):
    def test_does_not_call_close(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        # No `close` attribute defined on the bare consumer at all — if
        # _send_history_or_unavailable ever tried to call self.close(), this
        # would raise AttributeError, failing the test loudly.
        _run(c._send_history_or_unavailable("EUR/USD", "1m", None))
        self.assertEqual(c.send_json.await_count, 1)

    def test_only_sends_one_message_no_side_channel(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        _run(c._send_history_or_unavailable("EUR/USD", "1m", None))
        self.assertEqual(_sent_types(c.send_json), ["history_unavailable"])


# ─────────────────────────────────────────────────────────────────────────
# 18/19/21. Live bucket construction untouched — candle_kline() regression
# ─────────────────────────────────────────────────────────────────────────
def _kline_event(symbol: str, minute_t: int, o: float, h: float, l: float, c: float, v: float = 1.0) -> dict:
    return {"symbol": symbol, "data": {"time": minute_t, "open": o, "high": h, "low": l, "close": c, "volume": v}}


class LiveBucketRegressionTests(TestCase):
    def _bare_live_consumer(self, symbol="BTCUSD", timeframe="1m"):
        c = TradingConsumer.__new__(TradingConsumer)
        c.symbol = symbol
        c.timeframe = timeframe
        c._agg = {}
        c._last_bar_time = {}
        c.send_json = AsyncMock()
        return c

    def test_first_live_tick_creates_current_bucket(self):
        c = self._bare_live_consumer(timeframe="1m")
        _run(c.candle_kline(_kline_event("BTCUSD", 120, 100, 101, 99, 100.5)))
        msgs = [call.args[0] for call in c.send_json.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertEqual(msgs[0]["data"]["time"], 120)

    def test_historical_and_live_buckets_do_not_collide(self):
        # Historical: last CLOSED candle 12:00:00-12:01:00 (bucket=0, tf=60).
        # First live tick at 12:01:03 -> new bucket 60, must not touch bucket 0.
        c = self._bare_live_consumer(timeframe="1m")
        _run(c.candle_kline(_kline_event("BTCUSD", 63, 100, 101, 99, 100.5)))
        msgs = [call.args[0] for call in c.send_json.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]
        self.assertEqual(msgs[0]["data"]["time"], 60)  # (63 // 60) * 60 = 60, not 0

    def test_boundary_tick_exactly_at_next_bucket_start(self):
        c = self._bare_live_consumer(timeframe="1m")
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 100, 101, 99, 100.5)))
        msgs = [call.args[0] for call in c.send_json.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]
        self.assertEqual(msgs[0]["data"]["time"], 60)


# ─────────────────────────────────────────────────────────────────────────
# 20. FIX-05C liveMid contract regression (textual — dashboard.html untouched)
# ─────────────────────────────────────────────────────────────────────────
class Fix05cContractRegressionTests(SimpleTestCase):
    def _template_source(self):
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_live_mid_state_still_only_source_of_truth(self):
        src = self._template_source()
        self.assertIn(
            "this.liveMid=null; this.liveSource=null; this.prevLiveMid=null;",
            src,
        )
        self.assertIn("this.liveMid=(_a+_b)/2", src)

    def test_history_handler_still_does_not_write_live_mid(self):
        src = self._template_source()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        j = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){", i)
        self.assertNotIn("liveMid", src[i:j])
