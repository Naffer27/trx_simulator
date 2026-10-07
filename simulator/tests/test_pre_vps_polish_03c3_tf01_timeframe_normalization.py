# simulator/tests/test_pre_vps_polish_03c3_tf01_timeframe_normalization.py
"""
PRE-VPS-POLISH-03C.3.TF-01 — Timeframe Contract Audit + Normalization.

Two independent things are verified here:

1. FAIL-CLOSED BACKEND CONTRACT (simulator/consumers.py): tf_seconds()/
   normalize_tf() must never silently remap an unrecognized timeframe
   to any other bucket — the historical bug this block fixes is that
   any unknown value (a typo, or a not-yet-supported one like "4h")
   used to resolve to tf_seconds()==1 ("1s"), i.e. silently became
   1-second candles with no visible error. Both functions now return
   None for anything unrecognized; change_timeframe/load_history
   reject explicitly (mirroring the existing change_symbol/
   invalid_symbol pattern) instead of mutating self.timeframe with an
   invalid value. "1s" itself remains a fully valid INTERNAL value —
   only retired from the public UI.

2. PUBLIC CATALOG NORMALIZATION (Mobile + Desktop): the provisional
   public catalog is exactly 1m/5m/15m/1h/1d (in that order, default
   15m) — "1s" is retired from both UIs. Mobile additionally gets an
   explicit DISPLAY LABEL vs INTERNAL VALUE separation (1h displays as
   "1H", 1d as "1D", while the WS payload still carries the lowercase
   internal string consumers.py already expects).

Explicitly NOT covered/changed by this block: 4h/1w/1mo support,
SL/TP drag, chart overlays, any Markets/Positions/History/Account/
Desktop visual redesign, any financial calculation.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from django.test import SimpleTestCase, TestCase

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
    """Minimal bare TradingConsumer exercising only the change_timeframe/
    load_history branches of receive() — same __new__ + manual-attrs
    pattern as test_fix_history_auto_close_sync01_reconciliation.py's
    _consumer()/test_o6c1i_candle_timeframe_fix.py's _bare_consumer()."""
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


# ─────────────────────────────────────────────────────────────────────────
# A. tf_seconds()/normalize_tf() — fail-closed contract (pure functions)
# ─────────────────────────────────────────────────────────────────────────
class TfHelpersFailClosedTests(SimpleTestCase):
    def test_known_canonical_seconds_unchanged(self):
        self.assertEqual(tf_seconds("1m"), 60)
        self.assertEqual(tf_seconds("5m"), 300)
        self.assertEqual(tf_seconds("15m"), 900)
        self.assertEqual(tf_seconds("1h"), 3600)
        self.assertEqual(tf_seconds("1d"), 86400)

    def test_internal_1s_capability_preserved(self):
        # "1s" is retired from the PUBLIC UI only — the backend
        # capability itself must remain fully intact.
        self.assertEqual(tf_seconds("1s"), 1)
        self.assertEqual(normalize_tf("1s"), "1s")

    def test_normalize_known_aliases_unchanged(self):
        self.assertEqual(normalize_tf("m1"), "1m")
        self.assertEqual(normalize_tf("H1"), "1h")
        self.assertEqual(normalize_tf("3600"), "1h")
        self.assertEqual(normalize_tf("D1"), "1d")
        self.assertEqual(normalize_tf("86400"), "1d")

    def test_unknown_timeframe_never_falls_back_to_1s(self):
        # THE regression this block fixes: an unrecognized timeframe
        # used to resolve to tf_seconds()==1 / normalize_tf()=="1s".
        # Note: "1M" is deliberately NOT in this list — both helpers
        # are case-insensitive, so "1M".lower()=="1m" is a legitimate,
        # pre-existing alias for 1-minute, not a bogus value. This is
        # exactly the real ambiguity the TF-AUDIT flagged for the
        # future monthly timeframe's internal identifier (recommended
        # "1mo", never "1M") — confirmed here, not a defect in this fix.
        for bogus in ("4h", "1w", "1mo", "not_a_timeframe", "", "4H", "7d"):
            self.assertIsNone(tf_seconds(bogus), bogus)
            self.assertIsNone(normalize_tf(bogus), bogus)

    def test_not_yet_supported_future_timeframes_not_accidentally_enabled(self):
        for future_tf in ("4h", "1w", "1mo"):
            self.assertIsNone(normalize_tf(future_tf), future_tf)


# ─────────────────────────────────────────────────────────────────────────
# B. change_timeframe — explicit rejection, never silent remap
# ─────────────────────────────────────────────────────────────────────────
class ChangeTimeframeFailClosedTests(TestCase):
    def test_valid_timeframe_still_accepted(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "1h"})))
        self.assertEqual(c.timeframe, "1h")
        acks = [m for m in c.sent if m.get("type") == "ack"]
        self.assertEqual(acks[-1]["timeframe"], "1h")
        self.assertEqual(acks[-1]["tf_sec"], 3600)

    def test_invalid_timeframe_rejected_explicitly(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "4h"})))
        # Mirrors the existing change_symbol/invalid_symbol pattern.
        self.assertEqual(c.sent, [{"type": "error", "code": "invalid_timeframe", "message": "timeframe_no_permitido"}])

    def test_invalid_timeframe_never_mutates_self_timeframe(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "bogus"})))
        self.assertEqual(c.timeframe, "15m")

    def test_invalid_timeframe_never_fetches_history(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "1w"})))
        c.generate_history_first_page.assert_not_called()
        c._send_history_or_unavailable.assert_not_called()

    def test_typo_rejected_same_as_unsupported_future_tf(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "change_timeframe", "timeframe": "15n"})))
        self.assertEqual(c.timeframe, "15m")
        self.assertEqual(c.sent[-1]["code"], "invalid_timeframe")


# ─────────────────────────────────────────────────────────────────────────
# C. load_history — same fail-closed rejection
# ─────────────────────────────────────────────────────────────────────────
class LoadHistoryFailClosedTests(TestCase):
    def test_valid_timeframe_still_fetches_history(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "load_history", "symbol": "EUR/USD", "timeframe": "1d"})))
        c.generate_history_first_page.assert_awaited_once()
        args = c.generate_history_first_page.await_args.args
        self.assertEqual(args[1], "1d")

    def test_invalid_timeframe_rejected_explicitly(self):
        c = _bare_tf_consumer(timeframe="15m")
        _run(c.receive(json.dumps({"action": "load_history", "symbol": "EUR/USD", "timeframe": "4h"})))
        self.assertEqual(c.sent, [{"type": "error", "code": "invalid_timeframe", "message": "timeframe_no_permitido"}])
        c.generate_history_first_page.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# D. connect() bootstrap source contract — invalid ?tf= never becomes "1s"
# ─────────────────────────────────────────────────────────────────────────
class ConnectBootstrapSourceContractTests(SimpleTestCase):
    def test_bootstrap_line_falls_back_to_1m_not_normalize_tf_default(self):
        import inspect

        src = inspect.getsource(TradingConsumer.connect)
        self.assertIn('self.timeframe = (q_tf_raw and normalize_tf(q_tf_raw)) or "1m"', src)


# ─────────────────────────────────────────────────────────────────────────
# E. Mobile public catalog — exactly 1m/5m/15m/1H/1D, default 15m
# ─────────────────────────────────────────────────────────────────────────
class MobilePublicCatalogTests(SimpleTestCase):
    def setUp(self):
        self.html = _read(MOBILE_HTML_PATH)
        self.session_src = _read(MOBILE_SESSION_PATH)

    def test_1s_not_in_mobile_html_timeframe_catalog(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m','5m','15m','1h','1d'];", self.html)
        self.assertNotIn("['1s','1m','5m','15m','1h','1d']", self.html)

    def test_1s_not_in_mobile_session_validation_catalog(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '1d'];", self.session_src)
        self.assertNotIn("['1s', '1m', '5m', '15m', '1h', '1d']", self.session_src)

    def test_catalog_order_exact(self):
        start = self.html.index("const MOBILE_TIMEFRAMES = [") + len("const MOBILE_TIMEFRAMES = [")
        end = self.html.index("]", start)
        items = [x.strip().strip("'") for x in self.html[start:end].split(",")]
        self.assertEqual(items, ["1m", "5m", "15m", "1h", "1d"])

    def test_default_still_15m(self):
        self.assertIn("this.currentTF = '15m';", self.session_src)

    def test_display_labels_1H_1D_internal_values_unchanged(self):
        self.assertIn("const MOBILE_TF_LABELS = {'1m':'1m','5m':'5m','15m':'15m','1h':'1H','1d':'1D'};", self.html)
        # The internal value sent to selectTimeframe()/the backend is
        # the lowercase key — never the uppercase display label.
        self.assertIn("session.selectTimeframe(tf)", self.html)
        self.assertNotIn("selectTimeframe('1H')", self.html)
        self.assertNotIn("selectTimeframe('1D')", self.html)

    def test_future_timeframes_not_accidentally_enabled_in_mobile(self):
        for forbidden in ("'4h'", '"4h"', "'1w'", '"1w"', "'1mo'", '"1mo"'):
            self.assertNotIn(forbidden, self.html)
            self.assertNotIn(forbidden, self.session_src)


# ─────────────────────────────────────────────────────────────────────────
# F. Desktop public catalog — 1s removed, nothing else touched
# ─────────────────────────────────────────────────────────────────────────
class DesktopPublicCatalogTests(SimpleTestCase):
    def setUp(self):
        self.html = _read(DESKTOP_HTML_PATH)

    def test_1s_option_removed_from_both_select_elements(self):
        self.assertNotIn('<option value="1s">1s</option>', self.html)

    def test_1s_button_removed_from_popover(self):
        self.assertNotIn('data-tf="1s"', self.html)

    def test_remaining_timeframes_still_present(self):
        for tf in ('value="1m"', 'value="5m"', 'value="15m"', 'value="1h"', 'value="1d"'):
            self.assertIn(tf, self.html)
        for tf in ('data-tf="1m"', 'data-tf="5m"', 'data-tf="15m"', 'data-tf="1h"', 'data-tf="1d"'):
            self.assertIn(tf, self.html)

    def test_default_15m_unaffected(self):
        self.assertIn("currentTF='15m'", self.html)
        self.assertIn('value="15m" selected', self.html)


# ─────────────────────────────────────────────────────────────────────────
# G. Mobile/Desktop internal-value compatibility — one shared contract
# ─────────────────────────────────────────────────────────────────────────
class MobileDesktopCompatibilityTests(SimpleTestCase):
    def test_shared_internal_values_identical_across_both_frontends(self):
        mobile_html = _read(MOBILE_HTML_PATH)
        desktop_html = _read(DESKTOP_HTML_PATH)
        for internal in ("1m", "5m", "15m", "1h", "1d"):
            self.assertIn(internal, mobile_html)
            self.assertIn(f'"{internal}"', desktop_html)


# ─────────────────────────────────────────────────────────────────────────
# H. History/live aggregation continue respecting valid timeframes
# ─────────────────────────────────────────────────────────────────────────
class HistoryAndLiveAggregationUnaffectedTests(TestCase):
    def test_reset_agg_still_uses_real_tf_seconds(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c.timeframe = "1h"
        c._agg = {}
        c._reset_agg("EUR/USD")
        self.assertEqual(c._agg["EUR/USD"]["tf_sec"], 3600)

    def test_reset_trade_agg_still_uses_real_tf_seconds(self):
        c = TradingConsumer.__new__(TradingConsumer)
        c.timeframe = "1d"
        c._trade_agg = {}
        c._reset_trade_agg("BTCUSD")
        self.assertEqual(c._trade_agg["BTCUSD"]["tf_sec"], 86400)


# ─────────────────────────────────────────────────────────────────────────
# I. Real execution (Node) — mobile_session.js client-side guard unaffected
# ─────────────────────────────────────────────────────────────────────────
class SessionRealExecutionTests(SimpleTestCase):
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

    def test_1s_no_longer_accepted_by_mobile_session_guard(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.selectTimeframe('1s');
          console.log(JSON.stringify({ tf: s.currentTF, sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["tf"], "15m")
        self.assertEqual(out["sentCount"], 0)

    def test_1h_still_accepted_sends_lowercase_internal_value(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.selectTimeframe('1h');
          console.log(JSON.stringify({ tf: s.currentTF, sent: s.ws.sent }));
        """)
        self.assertEqual(out["tf"], "1h")
        self.assertEqual(json.loads(out["sent"][0]), {"action": "change_timeframe", "timeframe": "1h"})

    def test_default_still_15m(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          console.log(JSON.stringify({ tf: s.currentTF }));
        """)
        self.assertEqual(out["tf"], "15m")


# ─────────────────────────────────────────────────────────────────────────
# J. No financial calculation changed anywhere in this block
# ─────────────────────────────────────────────────────────────────────────
class NoFinancialCalculationChangedTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    def test_mobile_chart_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_chart.js")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

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

    def test_consumers_py_change_scoped_to_tf_helpers_only(self):
        # consumers.py DOES change in this block (that is the point) —
        # this proves the diff never touches order/position/pnl/margin/
        # commission/ledger code, only the timeframe helpers + the two
        # WS action branches already covered above.
        diff = subprocess.run(
            ["git", "diff", "--", "simulator/consumers.py"],
            capture_output=True, text=True,
        ).stdout
        for forbidden in (
            "commission_for", "calculate_spread_revenue", "broker_price(",
            "BrokerLedger", "LedgerEntry", "pnl_engine", "margin_used",
            "_check_tp_sl", "_check_pending_triggers", "_order_new", "_order_close",
        ):
            self.assertNotIn(forbidden, diff, forbidden)
