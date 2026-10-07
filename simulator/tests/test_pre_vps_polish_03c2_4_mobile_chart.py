# simulator/tests/test_pre_vps_polish_03c2_4_mobile_chart.py
"""
PRE-VPS-POLISH-03C.2.4 — Mobile chart / candles / history / timeframes.

Verifies:
  - MobileTradingSession (mobile_session.js) now supports
    selectTimeframe(tf) and parses history/candle_new/candle_update/
    volume_update, guarded exactly like Desktop guards them (history on
    BOTH symbol+timeframe; candle_*/volume_update on symbol only — a
    real, confirmed asymmetry mirrored from desktop.html, not invented).
  - MobileTradingChart (new file, mobile_chart.js) is a small, Mobile-
    only presentation class owning chart/candleSeries/volumeSeries/bars
    only — no WebSocket/account/order/position/P&L/risk code.
  - mobile.html gains a chart container + 6-value timeframe selector
    (no "4h"), the same two-CDN Lightweight Charts 4.1.1 loading pattern
    Desktop uses (own unpkg <script> tag + mobile_chart.js's own
    jsdelivr fallback), wired via plain callbacks — no event bus.
  - No client-side tick-to-candle aggregation anywhere; applyPriceTickState
    remains the sole shared quote authority; Desktop/backend/financial-
    engine files remain byte-for-byte unchanged.

Real-execution tests run the actual MobileTradingSession/MobileTradingChart
classes (composed with the real trading_core.js) via Node — skipped
gracefully when `node` is not on PATH. LightweightCharts itself (an
external rendering library, not project logic) is mocked with a minimal
FakeLWC, exactly as FakeWS mocks the browser WebSocket API — the shared
helpers (normTime/n/volPointForBar/tfToSec/debounce/applyPriceTickState)
are NEVER mocked; they are always loaded from the real trading_core.js.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


def _session_source() -> str:
    with open("simulator/static/simulator/trade/mobile_session.js", encoding="utf-8") as f:
        return f.read()


def _chart_source() -> str:
    with open("simulator/static/simulator/trade/mobile_chart.js", encoding="utf-8") as f:
        return f.read()


def _core_source() -> str:
    with open("simulator/static/simulator/trade/trading_core.js", encoding="utf-8") as f:
        return f.read()


def _html_source() -> str:
    with open("simulator/templates/simulator/trade/mobile.html", encoding="utf-8") as f:
        return f.read()


NODE_AVAILABLE = shutil.which("node") is not None

FAKE_LWC_HARNESS = """
class FakeSeries {
  constructor(){ this.data=null; this.updates=[]; this.options=null; }
  setData(d){ this.data = d; }
  update(b){ this.updates.push(b); }
  applyOptions(o){ this.options = o; }
}
class FakeChart {
  constructor(){
    this.candle = new FakeSeries();
    this.volume = new FakeSeries();
    this._scales = {};
    this._opts = null;
  }
  addCandlestickSeries(){ return this.candle; }
  addHistogramSeries(){ return this.volume; }
  priceScale(id){ if(!this._scales[id]) this._scales[id] = new FakeSeries(); return this._scales[id]; }
  timeScale(){ return { fitContent(){} }; }
  applyOptions(o){ this._opts = o; }
}
global.window = global.window || global;
global.window.LightweightCharts = { createChart: (el, opts) => new FakeChart() };
global.LightweightCharts = global.window.LightweightCharts;
"""


# ─────────────────────────────────────────────────────────────────────────
# A. mobile_chart.js — file shape / isolation
# ─────────────────────────────────────────────────────────────────────────
class MobileTradingChartFileShapeTests(SimpleTestCase):
    def test_file_exists_and_defines_class(self):
        src = _chart_source()
        self.assertIn("class MobileTradingChart", src)

    def test_no_websocket_code(self):
        src = _chart_source()
        for forbidden in ("new WebSocket(", "WebSocket.OPEN", "ws.send", "this.ws"):
            self.assertNotIn(forbidden, src)

    def test_no_account_or_order_or_position_code(self):
        # "margin" is deliberately excluded: Lightweight Charts' own
        # scaleMargins layout option legitimately contains it; that is
        # not financial account-margin logic.
        src = _chart_source().lower()
        for forbidden in (
            "account:update", "account:snapshot", "balance", "equity",
            "order:new", "order:close", "order:update", "risk_preview",
            "position", "pending", "upnl", "pnl",
        ):
            self.assertNotIn(forbidden, src)

    def test_no_tradingpanel_inheritance(self):
        src = _chart_source()
        self.assertNotIn("extends TradingPanel", src)
        self.assertNotIn("TradingPanel", src)

    def test_no_client_side_tick_to_candle_aggregation(self):
        src = _chart_source()
        # No field named 'tick' is ever turned into a candle here — the
        # backend remains sole candle authority. This file only ever
        # receives already-built bars (history/candle_new/candle_update).
        self.assertNotIn("msg.type", src)
        self.assertNotIn("'tick'", src)
        self.assertNotIn('"tick"', src)

    def test_reuses_real_shared_helpers_not_reimplemented(self):
        src = _chart_source()
        # normTime/n are invoked directly; volPointForBar is passed as a
        # bare callback reference (bars.map(volPointForBar)) so it never
        # appears with a trailing "(" in this file.
        for helper in ("normTime(", "n(", "volPointForBar"):
            self.assertIn(helper, src)
        # Never a second copy of these functions' real bodies.
        self.assertNotIn("const normTime=", src)
        self.assertNotIn("const normTime =", src)
        self.assertNotIn("const volPointForBar=", src)
        self.assertNotIn("const volPointForBar =", src)

    def test_no_fix05c_duplication(self):
        src = _chart_source()
        self.assertNotIn("applyPriceTickState", src)
        self.assertNotIn("liveMid", src)
        self.assertNotIn("liveSource", src)

    def test_two_tier_cdn_pattern_present(self):
        src = _chart_source()
        self.assertIn("window.LightweightCharts", src)
        self.assertIn("cdn.jsdelivr.net/npm/lightweight-charts@4.1.1", src)

    def test_public_methods_present(self):
        src = _chart_source()
        for method in (
            "initialize(", "resize(", "clear(", "applyHistory(",
            "appendCandle(", "updateLastCandle(", "updateVolume(",
        ):
            self.assertIn(method, src)


# ─────────────────────────────────────────────────────────────────────────
# B. mobile.html — chart/timeframe UI wiring
# ─────────────────────────────────────────────────────────────────────────
class MobileChartHtmlWiringTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_chart_container_present(self):
        self.assertIn('id="mobChartContainer"', self._html())

    def test_timeframe_selector_container_present(self):
        self.assertIn('id="mobTfSelector"', self._html())

    def test_unpkg_lightweight_charts_script_tag(self):
        html = self._html()
        self.assertIn("https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js", html)

    def test_mobile_chart_script_tag_present(self):
        # ManifestStaticFilesStorage hashes the filename (e.g.
        # mobile_chart.<hash>.js), so only the stem is checked here.
        self.assertIn("mobile_chart", self._html())

    def test_mobile_chart_loaded_before_session_instantiation(self):
        html = self._html()
        self.assertLess(html.index("mobile_chart"), html.index("new MobileTradingSession("))

    def test_timeframe_catalog_exactly_six_no_4h(self):
        html = self._html()
        self.assertIn("const MOBILE_TIMEFRAMES = ['1s','1m','5m','15m','1h','1d'];", html)
        self.assertNotIn("'4h'", html)
        self.assertNotIn('"4h"', html)

    def test_timeframe_click_calls_select_timeframe(self):
        self.assertIn("session.selectTimeframe(tf)", self._html())

    def test_watchlist_click_clears_chart_before_selecting_symbol(self):
        html = self._html()
        self.assertIn("mobileChart.clear(); session.selectSymbol(item.symbol);", html)

    def test_timeframe_click_clears_chart_before_selecting_timeframe(self):
        html = self._html()
        idx = html.index("item.addEventListener('click', function(){")
        snippet = html[idx: idx + 220]
        self.assertIn("mobileChart.clear();", snippet)
        self.assertIn("session.selectTimeframe(tf);", snippet)

    def test_no_order_ticket_or_positions_ui_yet(self):
        html = self._html()
        for forbidden in ("BUY", "SELL", "mobPositions", "mobPendingOrders", "mobClosedTrades", "SL/TP", "stopLoss", "takeProfit"):
            self.assertNotIn(forbidden, html)

    # Account types 31-34
    def _assert_chart_ui_for_account_type(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="mobChartContainer"', html)
        self.assertIn('id="mobTfSelector"', html)

    def test_standard_account_chart_ui(self):
        self._assert_chart_ui_for_account_type("STANDARD")

    def test_demo_account_chart_ui(self):
        self._assert_chart_ui_for_account_type("DEMO")

    def test_challenge_account_chart_ui(self):
        self._assert_chart_ui_for_account_type("CHALLENGE")

    def test_funded_account_chart_ui(self):
        self._assert_chart_ui_for_account_type("FUNDED")


# ─────────────────────────────────────────────────────────────────────────
# C. mobile_session.js — selectTimeframe() structural contract
# ─────────────────────────────────────────────────────────────────────────
class SelectTimeframeSourceContractTests(SimpleTestCase):
    def _body(self):
        src = _session_source()
        start = src.index("selectTimeframe(tf) {")
        end = src.index("\n  }\n\n", start)
        return src[start:end]

    def test_validates_against_real_catalog(self):
        body = self._body()
        self.assertIn("MOBILE_TIMEFRAMES.includes(tf)", body)

    def test_catalog_exactly_six_no_4h(self):
        src = _session_source()
        self.assertIn("const MOBILE_TIMEFRAMES = ['1s', '1m', '5m', '15m', '1h', '1d'];", src)
        self.assertNotIn("'4h'", src)
        self.assertNotIn('"4h"', src)

    def test_default_timeframe_is_15m(self):
        src = _session_source()
        self.assertIn("this.currentTF = '15m';", src)

    def test_exact_change_timeframe_payload(self):
        body = self._body()
        self.assertIn(
            "this.ws.send(JSON.stringify({ action: 'change_timeframe', timeframe: this.currentTF }))",
            body,
        )

    def test_does_not_touch_quote_fields(self):
        body = self._body()
        for forbidden in ("this.bid = null", "this.ask = null", "this.liveMid = null", "this.liveSource = null", "this.prevLiveMid = null", "this.onQuote("):
            self.assertNotIn(forbidden, body)

    def test_only_sends_when_ws_open(self):
        body = self._body()
        self.assertIn("this.ws.readyState === WebSocket.OPEN", body)

    def test_reuses_tf_to_sec_not_a_second_values_catalog(self):
        # tfToSec() remains the sole seconds-mapping source; this file
        # never re-types the {'1s':1,...} value mapping.
        src = _session_source()
        self.assertNotIn("'1s':1", src)
        self.assertNotIn("'1m':60", src)


# ─────────────────────────────────────────────────────────────────────────
# D. mobile_session.js — history/candle_*/volume_update guard contract
# ─────────────────────────────────────────────────────────────────────────
class HandleMsgNewBranchesSourceContractTests(SimpleTestCase):
    def _handle_msg_body(self):
        src = _session_source()
        start = src.index("_handleMsg(msg) {")
        end = src.index("\n  }\n}", start)
        return src[start:end]

    def test_history_guarded_on_symbol_and_timeframe(self):
        body = self._handle_msg_body()
        self.assertIn("if (msg.symbol && msg.symbol !== this.currentSymbol) return;\n      if (msg.timeframe && msg.timeframe !== this.currentTF) return;", body)

    def test_candle_messages_guarded_on_symbol_only_no_timeframe_field(self):
        body = self._handle_msg_body()
        idx = body.index("candle_new' || msg.type === 'candle_update')")
        snippet = body[idx: idx + 260]
        self.assertIn("msg.symbol !== this.currentSymbol", snippet)
        self.assertNotIn("this.currentTF", snippet)

    def test_volume_update_guarded_on_symbol_only(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'volume_update'")
        snippet = body[idx: idx + 220]
        self.assertIn("msg.symbol !== this.currentSymbol", snippet)
        self.assertNotIn("this.currentTF", snippet)

    def test_no_backend_history_contract_duplicated(self):
        # The handler forwards raw data/phase — it must never itself
        # decide replace-vs-prepend (that's MobileTradingChart's job).
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'history'")
        history_block = body[idx: idx + 400]
        self.assertNotIn("phase === 'complete'", history_block)
        self.assertNotIn("setData", history_block)


# ─────────────────────────────────────────────────────────────────────────
# E. Real execution — MobileTradingSession (FakeWS + real trading_core.js)
# ─────────────────────────────────────────────────────────────────────────
class SessionRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        session_src = _session_source()
        driver = f"""
        global.window = global;
        global.localStorage = {{}};
        global.document = {{ getElementById: () => null }};
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

    def test_default_timeframe_is_15m(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          console.log(JSON.stringify({ tf: s.currentTF }));
        """)
        self.assertEqual(out["tf"], "15m")

    def test_valid_timeframe_selection_sends_payload(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.selectTimeframe('1h');
          console.log(JSON.stringify({ tf: s.currentTF, sent: s.ws.sent }));
        """)
        self.assertEqual(out["tf"], "1h")
        self.assertEqual(json.loads(out["sent"][0]), {"action": "change_timeframe", "timeframe": "1h"})

    def test_invalid_timeframe_rejected(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.selectTimeframe('4h');
          console.log(JSON.stringify({ tf: s.currentTF, sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["tf"], "15m")
        self.assertEqual(out["sentCount"], 0)

    def test_timeframe_switch_preserves_live_quote_state(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          const before = { bid: s.bid, ask: s.ask, liveMid: s.liveMid, liveSource: s.liveSource };
          s.selectTimeframe('1h');
          const after = { bid: s.bid, ask: s.ask, liveMid: s.liveMid, liveSource: s.liveSource };
          console.log(JSON.stringify({ before, after }));
        """)
        self.assertEqual(out["before"], out["after"])
        self.assertIsNotNone(out["after"]["bid"])

    def test_history_delivered_when_symbol_and_timeframe_match(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], (data, phase) => { got = { data, phase }; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '15m', phase: 'initial', data: [{time:1,open:1,high:1,low:1,close:1}] });
          console.log(JSON.stringify({ got }));
        """)
        self.assertIsNotNone(out["got"])
        self.assertEqual(out["got"]["phase"], "initial")

    def test_history_rejected_on_symbol_mismatch(self):
        out = self._run_node("""
          let callCount = 0;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD'], () => { callCount++; });
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '15m', phase: 'initial', data: [] });
          console.log(JSON.stringify({ callCount }));
        """)
        self.assertEqual(out["callCount"], 0)

    def test_history_rejected_on_timeframe_mismatch(self):
        out = self._run_node("""
          let callCount = 0;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], () => { callCount++; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '1h', phase: 'initial', data: [] });
          console.log(JSON.stringify({ callCount }));
        """)
        self.assertEqual(out["callCount"], 0)

    def test_candle_new_delivered_ignoring_timeframe_field(self):
        # candle_new carries no real timeframe field; even if a caller
        # stuffs an unrelated one in, it must NOT be used to reject —
        # mirrors Desktop's real symbol-only guard exactly.
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, (bar) => { got = bar; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('1h');
          s._handleMsg({ type: 'candle_new', symbol: 'EUR/USD', timeframe: '5m', data: { time: 1, open: 1, high: 2, low: 0.5, close: 1.5 } });
          console.log(JSON.stringify({ got }));
        """)
        self.assertIsNotNone(out["got"])

    def test_candle_update_rejected_on_symbol_mismatch(self):
        out = self._run_node("""
          let callCount = 0;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD'], null, null, () => { callCount++; });
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          s._handleMsg({ type: 'candle_update', symbol: 'EUR/USD', data: { time: 1, open: 1, high: 2, low: 0.5, close: 1.5 } });
          console.log(JSON.stringify({ callCount }));
        """)
        self.assertEqual(out["callCount"], 0)

    def test_volume_update_delivered_on_symbol_match(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, (p) => { got = p; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'volume_update', symbol: 'EUR/USD', time: 1700000000, value: 42, color: '#26a69a' });
          console.log(JSON.stringify({ got }));
        """)
        self.assertEqual(out["got"], {"time": 1700000000, "value": 42, "color": "#26a69a"})

    def test_volume_update_rejected_on_symbol_mismatch(self):
        out = self._run_node("""
          let callCount = 0;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD'], null, null, null, () => { callCount++; });
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          s._handleMsg({ type: 'volume_update', symbol: 'EUR/USD', time: 1, value: 1, color: '#fff' });
          console.log(JSON.stringify({ callCount }));
        """)
        self.assertEqual(out["callCount"], 0)

    def test_exact_load_history_payload_on_symbol_select(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          setTimeout(() => {
            console.log(JSON.stringify({ sent: s.ws.sent }));
          }, 250);
        """)
        payloads = [json.loads(x) for x in out["sent"]]
        self.assertIn({"action": "load_history", "symbol": "EUR/USD", "timeframe": "15m"}, payloads)

    def test_exact_load_history_payload_on_timeframe_select(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.selectTimeframe('1h');
          setTimeout(() => {
            console.log(JSON.stringify({ sent: s.ws.sent }));
          }, 250);
        """)
        payloads = [json.loads(x) for x in out["sent"]]
        self.assertIn({"action": "load_history", "symbol": "EUR/USD", "timeframe": "1h"}, payloads)

    def test_reconnect_restores_symbol_timeframe_and_requests_history(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('1h');
          const before = s.ws.sent.length;
          s.ws.onopen();
          clearInterval(s.hb);
          setTimeout(() => {
            console.log(JSON.stringify({ sentAfterReopen: s.ws.sent.slice(before) }));
          }, 250);
        """)
        payloads = [json.loads(x) for x in out["sentAfterReopen"]]
        self.assertIn({"action": "change_symbol", "symbol": "EUR/USD"}, payloads)
        self.assertIn({"action": "change_timeframe", "timeframe": "1h"}, payloads)
        self.assertIn({"action": "load_history", "symbol": "EUR/USD", "timeframe": "1h"}, payloads)

    def test_reconnect_resends_timeframe_even_without_symbol(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, []);
          s.connect();
          s.ws.readyState = 1;
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ sent: s.ws.sent }));
        """)
        payloads = [json.loads(x) for x in out["sent"]]
        self.assertIn({"action": "change_timeframe", "timeframe": "15m"}, payloads)
        self.assertNotIn({"action": "change_symbol", "symbol": None}, payloads)

    def test_exactly_one_websocket_ever(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('1h');
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ ok: true }));
        """)
        self.assertTrue(out["ok"])
        self.assertEqual(_session_source().count("new WebSocket("), 1)

    def test_apply_price_tick_state_still_sole_quote_authority(self):
        src = _session_source()
        self.assertIn("applyPriceTickState(this.liveMid, bid, ask, source)", src)
        self.assertNotIn("source!=='sim'", src)
        self.assertNotIn("ask>bid", src)

    def test_account_update_still_works_alongside_chart_messages(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; }, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '15m', phase: 'initial', data: [] });
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(out["balance"], 1000)

    def test_live_quote_still_works_alongside_chart_messages(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '15m', phase: 'initial', data: [] });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ liveMid: s.liveMid }));
        """)
        self.assertAlmostEqual(out["liveMid"], 1.1001, places=6)


# ─────────────────────────────────────────────────────────────────────────
# F. Real execution — MobileTradingChart (FakeLWC + real trading_core.js)
# ─────────────────────────────────────────────────────────────────────────
class ChartRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        chart_src = _chart_source()
        driver = f"""
        {FAKE_LWC_HARNESS}
        global.document = {{ createElement: () => ({{}}), head: {{ appendChild: () => {{}} }} }};
        {core_src}
        {chart_src}
        {script}
        """
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_initialize_creates_candle_and_volume_series(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({ clientWidth: 300, clientHeight: 260 });
            await c.initialize();
            console.log(JSON.stringify({ hasChart: !!c.chart, hasCandle: !!c.candleSeries, hasVolume: !!c.volumeSeries }));
          })();
        """)
        self.assertTrue(out["hasChart"])
        self.assertTrue(out["hasCandle"])
        self.assertTrue(out["hasVolume"])

    def test_clear_resets_bars_and_series(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:1,open:1,high:2,low:0.5,close:1.5}], 'complete');
            c.clear();
            console.log(JSON.stringify({ bars: c.bars, candleData: c.candleSeries.data, volumeData: c.volumeSeries.data }));
          })();
        """)
        self.assertEqual(out["bars"], [])
        self.assertEqual(out["candleData"], [])
        self.assertEqual(out["volumeData"], [])

    def test_apply_history_full_replace_when_no_existing_bars(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([
              {time:100,open:1,high:2,low:0.5,close:1.5},
              {time:200,open:1.5,high:2.5,low:1,close:2}
            ], 'initial');
            console.log(JSON.stringify({ bars: c.bars, candleData: c.candleSeries.data }));
          })();
        """)
        self.assertEqual(len(out["bars"]), 2)
        self.assertEqual(out["bars"][0]["time"], 100)
        self.assertEqual(len(out["candleData"]), 2)

    def test_apply_history_complete_phase_prepends_older_bars_only(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:200,open:1,high:2,low:0.5,close:1.5}], 'initial');
            c.applyHistory([
              {time:100,open:0.9,high:1.1,low:0.8,close:1.0},
              {time:200,open:99,high:99,low:99,close:99}
            ], 'complete');
            console.log(JSON.stringify({ bars: c.bars }));
          })();
        """)
        self.assertEqual([b["time"] for b in out["bars"]], [100, 200])
        # the duplicate/newer timestamp from the "complete" response must
        # NOT have replaced the already-live bar at time=200
        self.assertEqual(out["bars"][1]["open"], 1)

    def test_apply_history_complete_with_no_older_bars_is_noop(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:200,open:1,high:2,low:0.5,close:1.5}], 'initial');
            c.applyHistory([{time:300,open:1,high:2,low:0.5,close:1.5}], 'complete');
            console.log(JSON.stringify({ bars: c.bars }));
          })();
        """)
        self.assertEqual([b["time"] for b in out["bars"]], [200])

    def test_apply_history_filters_invalid_bars(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([
              {time:100,open:1,high:2,low:0.5,close:1.5},
              {time:null,open:1,high:2,low:0.5,close:1.5},
              {time:200,open:null,high:2,low:0.5,close:1.5}
            ], 'initial');
            console.log(JSON.stringify({ count: c.bars.length }));
          })();
        """)
        self.assertEqual(out["count"], 1)

    def test_apply_history_chronological_order_preserved(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([
              {time:100,open:1,high:2,low:0.5,close:1.5},
              {time:200,open:1,high:2,low:0.5,close:1.5},
              {time:300,open:1,high:2,low:0.5,close:1.5}
            ], 'initial');
            console.log(JSON.stringify({ times: c.bars.map(b => b.time) }));
          })();
        """)
        self.assertEqual(out["times"], sorted(out["times"]))

    def test_append_candle_pushes_new_bar(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:100,open:1,high:2,low:0.5,close:1.5}], 'initial');
            c.appendCandle({time:200,open:2,high:3,low:1.5,close:2.5});
            console.log(JSON.stringify({ count: c.bars.length, last: c.bars[c.bars.length-1] }));
          })();
        """)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["last"]["time"], 200)

    def test_append_candle_duplicate_timestamp_patches_instead_of_duplicating(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:100,open:1,high:2,low:0.5,close:1.5}], 'initial');
            c.appendCandle({time:100,open:1,high:9,low:0.5,close:8});
            console.log(JSON.stringify({ count: c.bars.length, last: c.bars[c.bars.length-1] }));
          })();
        """)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["last"]["close"], 8)

    def test_update_last_candle_patches_in_place(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:100,open:1,high:2,low:0.5,close:1.5}], 'initial');
            c.updateLastCandle({time:100,open:1,high:5,low:0.5,close:4});
            console.log(JSON.stringify({ count: c.bars.length, last: c.bars[c.bars.length-1] }));
          })();
        """)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["last"]["close"], 4)

    def test_update_volume_uses_backend_value_directly_not_recomputed(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.updateVolume({ time: 100, value: 12345, color: '#26a69a' });
            console.log(JSON.stringify({ updates: c.volumeSeries.updates }));
          })();
        """)
        self.assertEqual(out["updates"], [{"time": 100, "value": 12345, "color": "#26a69a"}])

    def test_apply_history_volume_uses_vol_point_for_bar(self):
        out = self._run_node("""
          (async () => {
            const c = new MobileTradingChart({});
            await c.initialize();
            c.applyHistory([{time:100,open:1,high:2,low:0.5,close:1.5,volume:77}], 'initial');
            console.log(JSON.stringify({ volData: c.volumeSeries.data }));
          })();
        """)
        self.assertEqual(out["volData"][0]["value"], 77)

    def test_resize_applies_container_dimensions(self):
        out = self._run_node("""
          (async () => {
            const container = { clientWidth: 321, clientHeight: 123 };
            const c = new MobileTradingChart(container);
            await c.initialize();
            c.resize();
            console.log(JSON.stringify({ opts: c.chart._opts }));
          })();
        """)
        self.assertEqual(out["opts"], {"width": 321, "height": 123})


# ─────────────────────────────────────────────────────────────────────────
# G. No order/position/P&L UI anywhere in this block's files
# ─────────────────────────────────────────────────────────────────────────
class NoOutOfScopeUiTests(SimpleTestCase):
    def test_mobile_session_no_order_or_position_actions(self):
        src = _session_source()
        for forbidden in (
            "order:new", "order:close", "order:update", "risk_preview",
            "get_positions", "get_pending", "get_closed_trades",
        ):
            self.assertNotIn(forbidden, src)

    def test_mobile_chart_no_order_or_position_actions(self):
        src = _chart_source()
        for forbidden in ("order:new", "order:close", "order:update", "risk_preview"):
            self.assertNotIn(forbidden, src)

    def test_mobile_html_no_buy_sell_or_sl_tp_controls(self):
        html = _html_source()
        for forbidden in ("mobBuyBtn", "mobSellBtn", "mobSlInput", "mobTpInput"):
            self.assertNotIn(forbidden, html)


# ─────────────────────────────────────────────────────────────────────────
# H. Desktop / backend / financial-engine zero diff
# ─────────────────────────────────────────────────────────────────────────
class DesktopAndBackendZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    def test_desktop_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/desktop.html")

    def test_shell_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/shell.html")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

    def test_views_py_zero_diff(self):
        self._assert_zero_diff("simulator/views.py")

    def test_consumers_py_zero_diff(self):
        self._assert_zero_diff("simulator/consumers.py")

    def test_routing_py_zero_diff(self):
        self._assert_zero_diff("simulator/routing.py")

    def test_asgi_py_zero_diff(self):
        self._assert_zero_diff("simulator/asgi.py")

    def test_models_py_zero_diff(self):
        self._assert_zero_diff("simulator/models.py")

    def test_symbol_specs_py_zero_diff(self):
        self._assert_zero_diff("market_data/symbol_specs.py")

    def test_spread_engine_py_zero_diff(self):
        self._assert_zero_diff("simulator/spread_engine.py")
