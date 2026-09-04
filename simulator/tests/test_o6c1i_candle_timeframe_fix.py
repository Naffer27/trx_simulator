# simulator/tests/test_o6c1i_candle_timeframe_fix.py
"""
O.6c-1i — Candle timeframe fix (root cause confirmed in O.6c-1h).

candle_kline() forwarded every Binance @kline_1m update as its own
candle_new/candle_update, ignoring self.timeframe entirely — so at 15m the
chart created a "new" candle roughly every 60s instead of every 900s.
Historical REST candles were always correct; the bug was exclusive to the
live kline path.

Fix: candle_kline() now buckets incoming 1-minute sub-bars using the same
formula as the tick aggregator (_on_tick/_emit_bar) —
bucket = (t // tf_sec) * tf_sec — accumulating open-of-first /
running-high / running-low / close-of-last / summed-volume per bucket, and
reuses _emit_bar() (unmodified) to signal candle_new only on bucket change
and candle_update otherwise. For timeframe=1m this reduces to exactly the
prior behavior since each 1-minute sub-bar is its own bucket.

Nothing in this file touches order execution, margin, risk, P&L, routing,
or ledger code — the change is scoped entirely to consumers.py::candle_kline().
"""
import asyncio
from unittest.mock import AsyncMock

from django.test import TestCase

from simulator.consumers import TradingConsumer, tf_seconds
from market_data.symbol_specs import kline_symbols


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer(symbol: str = "BTCUSD", timeframe: str = "15m") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._agg = {}
    c._last_bar_time = {}
    c.send_json = AsyncMock()
    return c


def _kline_event(symbol: str, minute_t: int, o: float, h: float, l: float, c: float, v: float = 1.0) -> dict:
    return {
        "symbol": symbol,
        "data": {"time": minute_t, "open": o, "high": h, "low": l, "close": c, "volume": v, "is_closed": False},
    }


def _candle_msgs(mock: AsyncMock) -> list[dict]:
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]


def _volume_msgs(mock: AsyncMock) -> list[dict]:
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] == "volume_update"]


# ─────────────────────────────────────────────────────────────────────────
# 1. 1m preserves current correct behavior — every minute is its own bucket
# ─────────────────────────────────────────────────────────────────────────
class OneMinutePreservedTests(TestCase):
    def test_each_minute_creates_a_new_candle(self):
        c = _bare_consumer(timeframe="1m")
        for i, minute_t in enumerate((0, 60, 120)):
            c.send_json.reset_mock()
            _run(c.candle_kline(_kline_event("BTCUSD", minute_t, 100 + i, 101 + i, 99 + i, 100.5 + i)))
            msgs = _candle_msgs(c.send_json)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["type"], "candle_new")
            self.assertEqual(msgs[0]["data"]["time"], minute_t)

    def test_repeat_update_within_same_minute_is_candle_update(self):
        c = _bare_consumer(timeframe="1m")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100.2)))
        c.send_json.reset_mock()
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 102, 99, 100.8)))
        msgs = _candle_msgs(c.send_json)
        self.assertEqual(msgs[0]["type"], "candle_update")
        self.assertEqual(msgs[0]["data"]["close"], 100.8)


# ─────────────────────────────────────────────────────────────────────────
# 2/3/4/9/10. Multi-minute bucketing at 5m / 15m / 1h
# ─────────────────────────────────────────────────────────────────────────
class MultiMinuteBucketingTests(TestCase):
    def _run_bucket(self, timeframe: str, n_minutes: int):
        c = _bare_consumer(timeframe=timeframe)
        all_msgs = []
        for i in range(n_minutes):
            minute_t = i * 60
            c.send_json.reset_mock()
            _run(c.candle_kline(_kline_event("BTCUSD", minute_t, 100, 100 + i, 100 - i, 100 + (i % 3), v=1.0)))
            all_msgs.extend(_candle_msgs(c.send_json))
        return c, all_msgs

    def test_5m_five_one_minute_klines_same_bucket(self):
        c, msgs = self._run_bucket("5m", 5)
        self.assertEqual(msgs[0]["type"], "candle_new")
        for m in msgs[1:]:
            self.assertEqual(m["type"], "candle_update")
        self.assertTrue(all(m["data"]["time"] == 0 for m in msgs))

    def test_15m_fifteen_one_minute_klines_same_bucket(self):
        c, msgs = self._run_bucket("15m", 15)
        self.assertEqual(msgs[0]["type"], "candle_new")
        for m in msgs[1:]:
            self.assertEqual(m["type"], "candle_update")
        self.assertTrue(all(m["data"]["time"] == 0 for m in msgs))

    def test_1h_sixty_one_minute_klines_same_bucket(self):
        c, msgs = self._run_bucket("1h", 60)
        self.assertEqual(msgs[0]["type"], "candle_new")
        for m in msgs[1:]:
            self.assertEqual(m["type"], "candle_update")
        self.assertTrue(all(m["data"]["time"] == 0 for m in msgs))

    def test_15m_new_bucket_only_when_15_minutes_elapse(self):
        """Exact example from the O.6c-1h/O.6c-1i spec: klines between
        12:00:00 and 12:14:59 (epoch 0..840, one per minute) belong to a
        single candle at bucket 12:00 (epoch 0); the first kline at 12:15:00
        (epoch 900) opens a new candle at bucket 12:15 (epoch 900)."""
        c = _bare_consumer(timeframe="15m")
        msgs = []
        for i in range(15):  # minutes 12:00..12:14
            c.send_json.reset_mock()
            _run(c.candle_kline(_kline_event("BTCUSD", i * 60, 100, 101, 99, 100.5)))
            msgs.extend(_candle_msgs(c.send_json))
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertTrue(all(m["data"]["time"] == 0 for m in msgs))
        self.assertTrue(all(m["type"] == "candle_update" for m in msgs[1:]))

        c.send_json.reset_mock()
        _run(c.candle_kline(_kline_event("BTCUSD", 900, 105, 106, 104, 105.5)))  # 12:15:00
        new_msgs = _candle_msgs(c.send_json)
        self.assertEqual(len(new_msgs), 1)
        self.assertEqual(new_msgs[0]["type"], "candle_new")
        self.assertEqual(new_msgs[0]["data"]["time"], 900)
        self.assertEqual(new_msgs[0]["data"]["open"], 105)


# ─────────────────────────────────────────────────────────────────────────
# 5/6/7/8. OHLCV correctness within a bucket
# ─────────────────────────────────────────────────────────────────────────
class OhlcvAggregationTests(TestCase):
    def test_open_stays_the_first_minute_open(self):
        c = _bare_consumer(timeframe="5m")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100.5)))
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 105, 106, 104, 105.5)))
        last = _candle_msgs(c.send_json)[-1]
        self.assertEqual(last["data"]["open"], 100)

    def test_close_updates_to_the_latest_minute_close(self):
        c = _bare_consumer(timeframe="5m")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100.5)))
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 105, 106, 104, 107.25)))
        last = _candle_msgs(c.send_json)[-1]
        self.assertEqual(last["data"]["close"], 107.25)

    def test_high_low_are_the_running_extremes(self):
        c = _bare_consumer(timeframe="5m")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 103, 98, 101)))
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 101, 109, 97, 102)))
        _run(c.candle_kline(_kline_event("BTCUSD", 120, 102, 104, 100, 103)))
        last = _candle_msgs(c.send_json)[-1]
        self.assertEqual(last["data"]["high"], 109)
        self.assertEqual(last["data"]["low"], 97)

    def test_volume_sums_distinct_minutes_not_repeat_updates(self):
        """Repeat updates for the SAME still-forming minute must overwrite,
        not add, or volume would be wildly over-counted (Binance reports
        cumulative volume-for-the-minute on every update)."""
        c = _bare_consumer(timeframe="5m")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100.2, v=1.0)))
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100.6, v=2.5)))  # same minute, cumulative v
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 100.6, 101, 100, 100.9, v=1.5)))  # new minute
        vol_msgs = _volume_msgs(c.send_json)
        self.assertEqual(vol_msgs[-1]["value"], 2.5 + 1.5)  # latest minute-0 volume + minute-60 volume


# ─────────────────────────────────────────────────────────────────────────
# 11/12. No regression for BTCUSD / ETHUSD specifically
# ─────────────────────────────────────────────────────────────────────────
class KlineSymbolNoRegressionTests(TestCase):
    def test_btcusd_and_ethusd_are_kline_symbols(self):
        self.assertIn("BTCUSD", kline_symbols())
        self.assertIn("ETHUSD", kline_symbols())

    def test_ethusd_15m_bucketing_matches_btcusd(self):
        c = _bare_consumer(symbol="ETHUSD", timeframe="15m")
        msgs = []
        for i in range(15):
            c.send_json.reset_mock()
            _run(c.candle_kline(_kline_event("ETHUSD", i * 60, 3000, 3010, 2990, 3005)))
            msgs.extend(_candle_msgs(c.send_json))
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertTrue(all(m["type"] == "candle_update" for m in msgs[1:]))

    def test_event_for_other_symbol_ignored(self):
        c = _bare_consumer(symbol="BTCUSD", timeframe="15m")
        _run(c.candle_kline(_kline_event("ETHUSD", 0, 3000, 3010, 2990, 3005)))
        c.send_json.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 13. Forex / non-kline path (_on_tick/_emit_bar) untouched
# ─────────────────────────────────────────────────────────────────────────
class NonKlinePathIntactTests(TestCase):
    def test_btcusd_massive_tick_now_feeds_candle_aggregation(self):
        # GOLDEN-MARKETDATA-CRYPTO-01 — BTCUSD is still a _KLINE_SYMBOLS
        # member (symbol_specs.py's exchange_symbol/kraken_symbol stay
        # dormant, unchanged) but is now served LIVE by Massive
        # (tick-only quotes, candle_kline() never fires for it anymore —
        # Binance/Kraken are functionally unreachable, _try_live_legacy).
        # _on_tick() was deliberately updated to stop early-returning for
        # Massive-crypto-live symbols so real-time candle aggregation from
        # ticks works again — the opposite of what this test used to
        # assert (see simulator/tests/test_golden_marketdata_crypto_01_
        # massive_crypto_live.py::OnTickCandleAggregationTests for the
        # full, dedicated coverage of this behavior).
        c = _bare_consumer(symbol="BTCUSD", timeframe="15m")
        c._emit_bar = AsyncMock()
        _run(c._on_tick("BTCUSD", 100.0, volume=1.0, ts=0))
        c._emit_bar.assert_awaited_once()

    def test_on_tick_aggregation_unchanged_for_forex_symbol(self):
        c = _bare_consumer(symbol="EUR/USD", timeframe="5m")
        _run(c._on_tick("EUR/USD", 1.1000, volume=0.0, ts=0))
        _run(c._on_tick("EUR/USD", 1.1010, volume=0.0, ts=100))  # still bucket 0 (< 300)
        msgs = _candle_msgs(c.send_json)
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertEqual(msgs[-1]["type"], "candle_update")
        self.assertEqual(msgs[-1]["data"]["close"], 1.1010)
        self.assertEqual(msgs[-1]["data"]["open"], 1.1000)


# ─────────────────────────────────────────────────────────────────────────
# 14. Historical/live bucket compatibility (same UTC-epoch bucket formula)
# ─────────────────────────────────────────────────────────────────────────
class HistoricalLiveBucketCompatibilityTests(TestCase):
    def test_live_bucket_formula_matches_generate_history_formula(self):
        """generate_history()'s synthetic-history bucketing (and, by the
        same formula, Binance's own natively-aligned REST kline buckets)
        use bucket = (t // tf_sec) * tf_sec — identical to candle_kline()'s
        new bucketing, so there is no seam/gap/duplicate at the
        historical -> live handoff."""
        tf_sec = tf_seconds("15m")
        historical_last_bucket = (12 * 3600 // tf_sec) * tf_sec  # e.g. some 15m-aligned hour boundary
        first_live_minute_t = historical_last_bucket + tf_sec  # next 1m kline right at the next 15m boundary

        c = _bare_consumer(timeframe="15m")
        _run(c.candle_kline(_kline_event("BTCUSD", first_live_minute_t, 100, 101, 99, 100.5)))
        msg = _candle_msgs(c.send_json)[0]
        self.assertEqual(msg["type"], "candle_new")
        self.assertEqual(msg["data"]["time"], historical_last_bucket + tf_sec)
        self.assertEqual(msg["data"]["time"] % tf_sec, 0)  # aligned to the same UTC-epoch grid

    def test_no_duplicate_bucket_at_seam(self):
        """A live minute that falls WITHIN the same 15m window as the last
        historical bucket must not re-open a duplicate candle at that
        already-closed historical bucket."""
        tf_sec = tf_seconds("15m")
        last_hist_bucket = 0
        # last minute belonging to that historical bucket (12:14:00, 0-indexed minute 14)
        c = _bare_consumer(timeframe="15m")
        _run(c.candle_kline(_kline_event("BTCUSD", 840, 100, 101, 99, 100.5)))  # 12:14:00 -> bucket 0
        msg = _candle_msgs(c.send_json)[0]
        self.assertEqual(msg["data"]["time"], last_hist_bucket)
