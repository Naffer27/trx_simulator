# simulator/tests/test_pre_vps_polish_03c3_tf02_4h_timeframe.py
"""
PRE-VPS-POLISH-03C.3.TF-02 — Real 4H Timeframe Support.

Verifies that "4h" is now a REAL, end-to-end-supported internal
timeframe value (14400 seconds), reusing the exact same generic
mechanisms 1h/1d already used — no second aggregation engine, no
client-side candle construction, no new WebSocket, no financial
calculation:

- consumers.py's _TF_ALIASES/_TF_SECONDS recognize "4h" (14400s) —
  fail-closed contract (TF-01) unchanged for anything still unknown
  (1w/1mo/typos).
- market_data/feeds.py's _MASSIVE_TF maps "4h" to Massive's own native
  (4, "hour") aggs bars — history is fetched FROM THE PROVIDER, never
  aggregated client-side from 1h bars.
- The SAME generic live-candle bucket formula (bucket = (ts // tf_sec)
  * tf_sec) already used by _on_tick()/candle_kline()/price_trade()
  for every other timeframe now produces correct 4h buckets with zero
  code changes to those methods — only the catalog grew.
- Mobile (mobile.html + mobile_session.js) and Desktop (desktop.html,
  3 selector locations) both expose "4h" in their public catalog, in
  the same position (between 1h and 1d), with the same internal value.
- "1s" stays retired from both public UIs (TF-01); "1w"/"1mo" stay
  unsupported (still None via normalize_tf/tf_seconds) — this block
  does not implement them.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from django.test import SimpleTestCase, TestCase

from market_data.feeds import _MASSIVE_TF
from simulator.consumers import TradingConsumer, normalize_tf, tf_seconds

NODE_AVAILABLE = shutil.which("node") is not None

MOBILE_HTML_PATH = "simulator/templates/simulator/trade/mobile.html"
MOBILE_SESSION_PATH = "simulator/static/simulator/trade/mobile_session.js"
DESKTOP_HTML_PATH = "simulator/templates/simulator/trade/desktop.html"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _run(coro):
    return asyncio.run(coro)


def _bare_tf_consumer(symbol="EUR/USD", timeframe="15m"):
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._agg = {}
    c._trade_agg = {}
    c._last_bar_time = {}
    c._history_generation = 0
    c._last_msg_ts = 0.0
    c.sent = []

    async def _send_json(payload):
        c.sent.append(payload)

    c.send_json = _send_json
    c.generate_history_first_page = AsyncMock(return_value=(None, None))
    c._send_history_or_unavailable = AsyncMock()
    c._start_history_depth = lambda *a, **k: None
    return c


def _bare_candle_consumer(symbol="EUR/USD", timeframe="4h"):
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._agg = {}
    c._trade_agg = {}
    c._last_bar_time = {}
    c.send_json = AsyncMock()
    return c


def _candle_msgs(mock):
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]


def _volume_msgs(mock):
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] == "volume_update"]


def _kline_event(symbol, minute_t, o, h, l, c, v=1.0):
    return {"symbol": symbol, "data": {"time": minute_t, "open": o, "high": h, "low": l, "close": c, "volume": v}}


# ─────────────────────────────────────────────────────────────────────────
# A. normalize_tf("4h") / tf_seconds("4h") — the real backend contract
# ─────────────────────────────────────────────────────────────────────────
class TfHelpers4hTests(SimpleTestCase):
    def test_normalize_tf_4h(self):
        self.assertEqual(normalize_tf("4h"), "4h")
        self.assertEqual(normalize_tf("H4"), "4h")
        self.assertEqual(normalize_tf("14400"), "4h")

    def test_tf_seconds_4h_is_14400(self):
        self.assertEqual(tf_seconds("4h"), 14400)

    def test_4h_not_converted_to_1h(self):
        self.assertNotEqual(tf_seconds("4h"), tf_seconds("1h"))

    def test_4h_not_converted_to_1s(self):
        self.assertNotEqual(tf_seconds("4h"), 1)
        self.assertNotEqual(tf_seconds("4h"), tf_seconds("1s"))

    def test_1w_1mo_still_unsupported(self):
        for still_unsupported in ("1w", "1mo", "1M", "1month", "mn1"):
            # "1M" is intentionally excluded from the strict-None check:
            # both helpers are case-insensitive, so "1M".lower()=="1m"
            # is the pre-existing 1-minute alias, not a bogus value —
            # confirmed finding from TF-01's own audit.
            if still_unsupported == "1M":
                continue
            self.assertIsNone(normalize_tf(still_unsupported), still_unsupported)
            self.assertIsNone(tf_seconds(still_unsupported), still_unsupported)

    def test_typo_still_fails_closed(self):
        self.assertIsNone(normalize_tf("4hh"))
        self.assertIsNone(normalize_tf("4"))


# ─────────────────────────────────────────────────────────────────────────
# B. Massive provider — real native 4h aggs, not client aggregation
# ─────────────────────────────────────────────────────────────────────────
class MassiveProvider4hTests(SimpleTestCase):
    def test_massive_tf_maps_4h_to_native_4_hour_multiplier(self):
        self.assertEqual(_MASSIVE_TF["4h"], (4, "hour"))

    def test_4h_seconds_matches_multiplier_times_hour(self):
        multiplier, timespan = _MASSIVE_TF["4h"]
        from market_data.feeds import _MASSIVE_SECONDS_PER_UNIT
        self.assertEqual(multiplier * _MASSIVE_SECONDS_PER_UNIT[timespan], tf_seconds("4h"))


# ─────────────────────────────────────────────────────────────────────────
# C. Mobile public catalog contains 4h, correct order, correct label
# ─────────────────────────────────────────────────────────────────────────
class Mobile4hCatalogTests(SimpleTestCase):
    def setUp(self):
        self.html = _read(MOBILE_HTML_PATH)
        self.session_src = _read(MOBILE_SESSION_PATH)

    def test_mobile_html_catalog_contains_4h_in_order(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m','5m','15m','1h','4h','1d'];", self.html)

    def test_mobile_session_catalog_contains_4h_in_order(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'];", self.session_src)

    def test_mobile_display_label_4H(self):
        self.assertIn("'4h':'4H'", self.html)

    def test_1s_still_absent_from_mobile_public_catalog(self):
        self.assertNotIn("['1s'", self.html)
        self.assertNotIn("['1s'", self.session_src)

    def test_1w_1mo_still_absent_from_mobile(self):
        for forbidden in ("'1w'", '"1w"', "'1mo'", '"1mo"'):
            self.assertNotIn(forbidden, self.html)
            self.assertNotIn(forbidden, self.session_src)


# ─────────────────────────────────────────────────────────────────────────
# D. Desktop public catalog contains 4h in all 3 selector locations
# ─────────────────────────────────────────────────────────────────────────
class Desktop4hCatalogTests(SimpleTestCase):
    def setUp(self):
        self.html = _read(DESKTOP_HTML_PATH)

    def test_popover_has_4h_between_1h_and_1d(self):
        self.assertIn(
            '<button class="mfp-tf-pop-item" data-tf="1h">1h</button>\n'
            '      <button class="mfp-tf-pop-item" data-tf="4h">4h</button>\n'
            '      <button class="mfp-tf-pop-item" data-tf="1d">1d</button>',
            self.html,
        )

    def test_both_select_elements_have_4h_option(self):
        self.assertEqual(
            self.html.count('<option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>'),
            2,
        )

    def test_1s_still_absent_from_desktop(self):
        self.assertNotIn('value="1s"', self.html)
        self.assertNotIn('data-tf="1s"', self.html)

    def test_1w_1mo_still_absent_from_desktop(self):
        self.assertNotIn('value="1w"', self.html)
        self.assertNotIn('value="1mo"', self.html)
        self.assertNotIn('data-tf="1w"', self.html)


# ─────────────────────────────────────────────────────────────────────────
# D2. PRE-VPS-POLISH-03C.3.TF-02B — shared trading_core.js helper
#     completeness. tfToSec() is the ONE lookup Desktop's _resetAgg()
#     (dead/write-only state — this.agg is never read anywhere in
#     desktop.html) and _updateRangeBar() (purely cosmetic range-button
#     visibility filter) use for "4h" — before TF-02B it silently fell
#     back to 60 (1 minute) for "4h", same class of bug TF-01 fixed on
#     the backend. Micro-fix: one new key in the existing dict, same
#     fallback, same signature, no second helper, no catalog
#     duplication, zero OHLCV/history/candle/order/P&L/spread/margin/
#     risk/economics impact (confirmed: this file is never imported by
#     consumers.py/feeds.py, and Desktop's two call sites are both
#     presentation-only).
# ─────────────────────────────────────────────────────────────────────────
class TradingCoreSharedHelper4hTests(SimpleTestCase):
    def setUp(self):
        self.core_src = _read("simulator/static/simulator/trade/trading_core.js")

    def test_exactly_one_tfToSec_definition(self):
        self.assertEqual(self.core_src.count("const tfToSec="), 1)

    def test_tfToSec_4h_is_14400_source_contract(self):
        self.assertIn("'4h':14400", self.core_src)

    def test_tfToSec_existing_values_unchanged(self):
        self.assertIn(
            "const tfToSec=tf=>({'1s':1,'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400}[String(tf)]??60);",
            self.core_src,
        )

    def test_fallback_unchanged(self):
        self.assertIn("??60);", self.core_src)

    def test_no_second_timeframe_helper_introduced(self):
        self.assertEqual(self.core_src.count("const tfToSec="), 1)
        self.assertNotIn("tfToSeconds", self.core_src)
        self.assertNotIn("timeframeToSeconds", self.core_src)

    def test_trading_core_js_diff_scoped_to_tfToSec_only(self):
        result = subprocess.run(["git", "diff", "--", "simulator/static/simulator/trade/trading_core.js"], capture_output=True, text=True)
        diff = result.stdout
        self.assertIn("const tfToSec=", diff)
        for forbidden in (
            "computeRawPnL(", "computePositionPnL(", "getContractSize(", "LOT_SPECS=",
            "applyPriceTickState(", "getCanonicalPositions(", "replaceCanonicalPositions(",
            "commission_for", "margin_used", "spread_revenue", "BrokerLedger",
        ):
            self.assertNotIn(forbidden, diff, forbidden)

    def test_real_execution_tfToSec_4h_equals_14400(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(
                self.core_src + "\nconsole.log(JSON.stringify({"
                "fourH: tfToSec('4h'), oneM: tfToSec('1m'), fiveM: tfToSec('5m'), "
                "fifteenM: tfToSec('15m'), oneH: tfToSec('1h'), oneD: tfToSec('1d'), "
                "oneS: tfToSec('1s'), unknown: tfToSec('1w')"
                "}));",
                encoding="utf-8",
            )
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(out["fourH"], 14400)
        self.assertEqual(out["oneM"], 60)
        self.assertEqual(out["fiveM"], 300)
        self.assertEqual(out["fifteenM"], 900)
        self.assertEqual(out["oneH"], 3600)
        self.assertEqual(out["oneD"], 86400)
        self.assertEqual(out["oneS"], 1)
        self.assertEqual(out["unknown"], 60)  # "1w" still falls to the unchanged ??60 fallback


# ─────────────────────────────────────────────────────────────────────────
# E. change_timeframe / load_history accept 4h for real
# ─────────────────────────────────────────────────────────────────────────
class WsActionAccepts4hTests(TestCase):
    def test_change_timeframe_accepts_4h(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "4h"})))
        self.assertEqual(c.timeframe, "4h")
        acks = [m for m in c.sent if m.get("type") == "ack"]
        self.assertEqual(acks[-1]["timeframe"], "4h")
        self.assertEqual(acks[-1]["tf_sec"], 14400)

    def test_load_history_accepts_4h_and_requests_it_from_provider(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "load_history", "symbol": "EUR/USD", "timeframe": "4h"})))
        c.generate_history_first_page.assert_awaited_once()
        args = c.generate_history_first_page.await_args.args
        self.assertEqual(args[1], "4h")

    def test_change_timeframe_4h_resets_both_aggregators(self):
        c = _bare_tf_consumer(timeframe="15m")
        c._agg = {"EUR/USD": {"tf_sec": 900}}
        c._trade_agg = {"EUR/USD": {"tf_sec": 900}}
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "4h"})))
        self.assertEqual(c._agg["EUR/USD"]["tf_sec"], 14400)
        self.assertEqual(c._trade_agg["EUR/USD"]["tf_sec"], 14400)


# ─────────────────────────────────────────────────────────────────────────
# F. History 4H uses real canonical candles — no fallback, no fabrication
# ─────────────────────────────────────────────────────────────────────────
class History4hUsesCanonicalPathTests(TestCase):
    def test_generate_history_dispatches_4h_to_massive_forex(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c._feed = AsyncMock()
        c._feed.fetch_massive_history.return_value = []
        c.account = {}
        _run(c.generate_history("EUR/USD", "4h", bars=50))
        c._feed.fetch_massive_history.assert_awaited_once_with("EUR/USD", interval="4h", limit=50)

    def test_generate_history_dispatches_4h_to_massive_crypto(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c._feed = AsyncMock()
        c._feed.fetch_massive_crypto_history.return_value = []
        c.account = {}
        _run(c.generate_history("BTCUSD", "4h", bars=50))
        c._feed.fetch_massive_crypto_history.assert_awaited_once_with("BTCUSD", interval="4h", limit=50)

    def test_no_special_case_4h_branch_in_generate_history(self):
        import inspect
        src = inspect.getsource(TradingConsumer.generate_history)
        self.assertNotIn('if timeframe == "4h"', src)
        self.assertNotIn("if timeframe=='4h'", src)


# ─────────────────────────────────────────────────────────────────────────
# G. Live 4H candles — bucket/alignment, same-bucket update, new-bucket
#    creation, OHLC correctness, volume semantics, no duplicates
# ─────────────────────────────────────────────────────────────────────────
class Live4hBucketingTests(TestCase):
    def test_bucket_boundaries_align_to_utc_0_4_8_12_16_20(self):
        tf_sec = tf_seconds("4h")
        self.assertEqual(86400 % tf_sec, 0)  # exactly 6 buckets per UTC day, zero drift
        for hour, expected_bucket_hour in (
            (0, 0), (3, 0), (4, 4), (7, 4), (8, 8), (11, 8),
            (12, 12), (15, 12), (16, 16), (19, 16), (20, 20), (23, 20),
        ):
            ts = hour * 3600 + 1000
            bucket = (ts // tf_sec) * tf_sec
            self.assertEqual(bucket, expected_bucket_hour * 3600, f"hour={hour}")

    def test_on_tick_update_within_same_4h_bucket(self):
        c = _bare_candle_consumer(symbol="EUR/USD", timeframe="4h")
        _run(c._on_tick("EUR/USD", 1.1000, volume=1.0, ts=0))
        _run(c._on_tick("EUR/USD", 1.1050, volume=2.0, ts=10000))  # still < 14400
        msgs = _candle_msgs(c.send_json)
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertEqual(msgs[-1]["type"], "candle_update")
        self.assertEqual(len(msgs), 2)  # exactly one new + one update, no extra candle

    def test_on_tick_new_candle_when_crossing_4h_boundary(self):
        c = _bare_candle_consumer(symbol="EUR/USD", timeframe="4h")
        _run(c._on_tick("EUR/USD", 1.1000, volume=1.0, ts=0))
        c.send_json.reset_mock()
        _run(c._on_tick("EUR/USD", 1.1100, volume=1.0, ts=14400))  # exactly the next bucket
        msgs = _candle_msgs(c.send_json)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertEqual(msgs[0]["data"]["time"], 14400)
        self.assertEqual(msgs[0]["data"]["open"], 1.1100)

    def test_tick_exactly_at_next_bucket_boundary_opens_new_bucket(self):
        # Boundary semantics: a tick at ts==14400 belongs to the SECOND
        # bucket (14400-28799), not the first (0-14399) — integer
        # division floor behavior, same convention already used by
        # every other timeframe.
        tf_sec = tf_seconds("4h")
        self.assertEqual((14400 // tf_sec) * tf_sec, 14400)
        self.assertEqual((14399 // tf_sec) * tf_sec, 0)

    def test_ohlc_semantics_open_high_low_close(self):
        c = _bare_candle_consumer(symbol="EUR/USD", timeframe="4h")
        _run(c._on_tick("EUR/USD", 100.0, volume=1.0, ts=0))       # open
        _run(c._on_tick("EUR/USD", 105.0, volume=1.0, ts=1000))    # new high
        _run(c._on_tick("EUR/USD", 95.0, volume=1.0, ts=2000))     # new low
        _run(c._on_tick("EUR/USD", 102.0, volume=1.0, ts=3000))    # close
        last = _candle_msgs(c.send_json)[-1]["data"]
        self.assertEqual(last["open"], 100.0)
        self.assertEqual(last["high"], 105.0)
        self.assertEqual(last["low"], 95.0)
        self.assertEqual(last["close"], 102.0)

    def test_volume_sums_within_bucket_tick_path(self):
        c = _bare_candle_consumer(symbol="EUR/USD", timeframe="4h")
        _run(c._on_tick("EUR/USD", 100.0, volume=1.5, ts=0))
        _run(c._on_tick("EUR/USD", 101.0, volume=2.5, ts=1000))
        vmsgs = _volume_msgs(c.send_json)
        self.assertEqual(vmsgs[-1]["value"], 4.0)

    def test_candle_kline_path_distinct_minute_volume_4h(self):
        # candle_kline() (exchange-kline path) sums DISTINCT minute
        # volumes, never double-counting repeat updates for the same
        # minute — same semantics already proven for 15m/1h, now
        # verified for 4h.
        c = _bare_candle_consumer(symbol="BTCUSD", timeframe="4h")
        _run(c.candle_kline(_kline_event("BTCUSD", 0, 100, 101, 99, 100, v=2.0)))
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 100, 103, 98, 101, v=3.0)))
        _run(c.candle_kline(_kline_event("BTCUSD", 60, 100, 103, 98, 101.5, v=3.5)))  # repeat update, same minute
        last = _candle_msgs(c.send_json)[-1]["data"]
        self.assertEqual(last["open"], 100)
        self.assertEqual(last["high"], 103)
        self.assertEqual(last["low"], 98)
        self.assertEqual(last["close"], 101.5)
        vmsgs = _volume_msgs(c.send_json)
        self.assertEqual(vmsgs[-1]["value"], 5.5)  # 2.0 + 3.5 (latest for minute 60), never 2.0+3.0+3.5

    def test_no_duplicate_candle_at_4h_boundary_seam(self):
        tf_sec = tf_seconds("4h")
        last_hist_bucket = 0
        c = _bare_candle_consumer(symbol="BTCUSD", timeframe="4h")
        # last minute belonging to the first historical 4h bucket (03:59:00)
        _run(c.candle_kline(_kline_event("BTCUSD", 14340, 100, 101, 99, 100.5)))
        msg = _candle_msgs(c.send_json)[0]
        self.assertEqual(msg["data"]["time"], last_hist_bucket)
        self.assertEqual(msg["data"]["time"] % tf_sec, 0)

    def test_live_bucket_formula_matches_history_formula_for_4h(self):
        tf_sec = tf_seconds("4h")
        historical_last_bucket = (12 * 3600 // tf_sec) * tf_sec
        first_live_minute_t = historical_last_bucket + tf_sec
        c = _bare_candle_consumer(symbol="BTCUSD", timeframe="4h")
        _run(c.candle_kline(_kline_event("BTCUSD", first_live_minute_t, 100, 101, 99, 100.5)))
        msg = _candle_msgs(c.send_json)[0]
        self.assertEqual(msg["type"], "candle_new")
        self.assertEqual(msg["data"]["time"], historical_last_bucket + tf_sec)
        self.assertEqual(msg["data"]["time"] % tf_sec, 0)

    def test_price_trade_path_4h_bucketing(self):
        # Massive-crypto trade-based accumulator (price_trade/
        # _emit_trade_bar) — the third independent accumulator, same
        # generic formula.
        c = _bare_candle_consumer(symbol="BTCUSD", timeframe="4h")
        _run(c.price_trade({"symbol": "BTCUSD", "price": 50000.0, "size": 0.1, "time": 0}))
        _run(c.price_trade({"symbol": "BTCUSD", "price": 50100.0, "size": 0.2, "time": 14400}))
        msgs = [call.args[0] for call in c.send_json.await_args_list if call.args[0]["type"] in ("candle_new", "candle_update")]
        self.assertEqual(msgs[0]["type"], "candle_new")
        self.assertEqual(msgs[0]["data"]["time"], 0)
        self.assertEqual(msgs[1]["type"], "candle_new")
        self.assertEqual(msgs[1]["data"]["time"], 14400)


# ─────────────────────────────────────────────────────────────────────────
# H. Reconnect preserves/restores 4h (real execution, Node)
# ─────────────────────────────────────────────────────────────────────────
class Reconnect4hRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _read("simulator/static/simulator/trade/trading_core.js")
        session_src = _read(MOBILE_SESSION_PATH)
        driver = f"""
        global.window = global;
        global.localStorage = {{}};
        global.document = {{ getElementById: () => null }};
        global.window.__TRADE_CONFIG__ = {{ accountId: 1 }};
        global.window.location = {{ href: 'http://example.com/dashboard/1/' }};
        class FakeWS {{
          constructor(url){{ this.url = url; this.readyState = 1; this.sent = []; }}
          send(payload){{ this.sent.push(payload); }}
          close(){{}}
        }}
        FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
        global.WebSocket = FakeWS;
        {core_src}
        {session_src}
        {script}
        """
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_4h_accepted_by_client_guard(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.selectTimeframe('4h');
          console.log(JSON.stringify({ tf: s.currentTF, sent: s.ws.sent }));
        """)
        self.assertEqual(out["tf"], "4h")
        self.assertEqual(json.loads(out["sent"][0]), {"action": "change_timeframe", "timeframe": "4h"})

    def test_reconnect_resends_4h_not_default(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('4h');
          // Simulate a dropped connection: clear the socket and let
          // connect() build its own fresh one (exactly what the real
          // reconnect timer does), then fire that instance's own
          // onopen — the exact resend path connect() wires.
          s.ws = null;
          s.connecting = false;
          s.connect();
          s.ws.readyState = 1;
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ tf: s.currentTF, sent: s.ws.sent }));
        """)
        self.assertEqual(out["tf"], "4h")
        sent_actions = [json.loads(p) for p in out["sent"]]
        tf_resends = [p for p in sent_actions if p.get("action") == "change_timeframe"]
        self.assertTrue(tf_resends, "expected a change_timeframe resend on reconnect")
        self.assertEqual(tf_resends[-1]["timeframe"], "4h")
        for p in sent_actions:
            if p.get("action") == "change_timeframe":
                self.assertNotEqual(p["timeframe"], "15m")
                self.assertNotEqual(p["timeframe"], "1h")
                self.assertNotEqual(p["timeframe"], "1s")


# ─────────────────────────────────────────────────────────────────────────
# I. Mobile/Desktop share the same backend authority; no second engine
# ─────────────────────────────────────────────────────────────────────────
class SharedAuthorityNoSecondEngineTests(SimpleTestCase):
    def test_mobile_and_desktop_use_identical_internal_4h_value(self):
        mobile_html = _read(MOBILE_HTML_PATH)
        desktop_html = _read(DESKTOP_HTML_PATH)
        self.assertIn("'4h'", mobile_html)
        self.assertIn('"4h"', desktop_html)

    def test_no_new_websocket_construction_site_in_mobile(self):
        self.assertNotIn("new WebSocket(", _read(MOBILE_HTML_PATH))
        self.assertEqual(_read(MOBILE_SESSION_PATH).count("new WebSocket("), 1)

    def test_no_local_financial_aggregation_in_mobile_chart_js(self):
        chart_src = _read("simulator/static/simulator/trade/mobile_chart.js")
        for forbidden in ("tf_seconds(", "_TF_ALIASES", "'4h'", '"4h"'):
            self.assertNotIn(forbidden, chart_src)

    def test_mobile_chart_js_zero_diff(self):
        result = subprocess.run(["git", "diff", "--quiet", "--", "simulator/static/simulator/trade/mobile_chart.js"])
        self.assertEqual(result.returncode, 0)


# ─────────────────────────────────────────────────────────────────────────
# J. Zero change to order execution / economics / IB / ledger
# ─────────────────────────────────────────────────────────────────────────
class NoEconomicsChangedTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    def test_views_py_zero_diff(self):
        self._assert_zero_diff("simulator/views.py")

    def test_routing_py_zero_diff(self):
        self._assert_zero_diff("simulator/routing.py")

    def test_asgi_py_zero_diff(self):
        self._assert_zero_diff("trx_simulator/asgi.py")

    def test_models_py_zero_diff(self):
        self._assert_zero_diff("simulator/models.py")

    def test_migrations_zero_diff(self):
        self._assert_zero_diff("simulator/migrations")

    def test_symbol_specs_py_zero_diff(self):
        self._assert_zero_diff("market_data/symbol_specs.py")

    def test_spread_engine_py_zero_diff(self):
        self._assert_zero_diff("simulator/spread_engine.py")

    def test_broker_ledger_py_zero_diff(self):
        self._assert_zero_diff("simulator/broker_ledger.py")

    def test_pnl_engine_py_zero_diff(self):
        self._assert_zero_diff("simulator/pnl_engine.py")

    def test_dynamic_spread_py_zero_diff(self):
        self._assert_zero_diff("simulator/dynamic_spread.py")

    def test_pricing_context_py_zero_diff(self):
        self._assert_zero_diff("simulator/pricing_context.py")

    def test_consumers_py_diff_scoped_to_timeframe_catalog_only(self):
        with open("simulator/consumers.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"4h": "4h"', src)
        self.assertIn('"4h": 14400', src)
        result = subprocess.run(["git", "diff", "--", "simulator/consumers.py"], capture_output=True, text=True)
        diff = result.stdout
        for forbidden in (
            "commission_for", "calculate_spread_revenue", "broker_price(",
            "BrokerLedger", "LedgerEntry", "pnl_engine", "margin_used",
            "_check_tp_sl", "_check_pending_triggers", "_order_new(", "_order_close(",
            "_db_open_position", "_db_close_position",
        ):
            self.assertNotIn(forbidden, diff, forbidden)

    def test_feeds_py_diff_scoped_to_4h_massive_entry_only(self):
        result = subprocess.run(["git", "diff", "--", "market_data/feeds.py"], capture_output=True, text=True)
        diff = result.stdout
        self.assertIn('"4h":  (4, "hour")', diff)
        for forbidden in ("commission", "BrokerLedger", "margin", "spread_revenue"):
            self.assertNotIn(forbidden, diff, forbidden)
