# simulator/tests/test_pre_vps_polish_03c2_3_mobile_symbol_selector.py
"""
PRE-VPS-POLISH-03C.2.3 — Mobile symbol selector / watchlist.

Verifies that MobileTradingSession (simulator/static/simulator/trade/
mobile_session.js) now supports selectSymbol(symbol) — the ONE Mobile
symbol authority, sending the backend's existing, unmodified
{action:'change_symbol',symbol} contract on the SAME WebSocket, with
zero new WS action types, zero duplicated FIX-05C gate, and zero second
connection. mobile.html gains a minimal watchlist sourced exclusively
from the backend's own allowed-symbol authority (views.py's
mobile_allowed_symbols_json, derived from market_data/symbol_specs.py —
the exact same source consumers.py's change_symbol handler enforces).

Structural assertions use the same source-inspection convention already
established throughout this series. Real-execution assertions run the
actual MobileTradingSession class (composed with the real
trading_core.js) via Node — skipped gracefully when `node` is not on
PATH.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from market_data.symbol_specs import allowed_symbols, get_all_specs
from simulator.tests.factories import make_account, make_user

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


def _session_source() -> str:
    with open(
        "simulator/static/simulator/trade/mobile_session.js", encoding="utf-8"
    ) as f:
        return f.read()


def _core_source() -> str:
    with open(
        "simulator/static/simulator/trade/trading_core.js", encoding="utf-8"
    ) as f:
        return f.read()


NODE_AVAILABLE = shutil.which("node") is not None


# ─────────────────────────────────────────────────────────────────────────
# 1/2. Catalog integrity — real server source, no invented/disabled symbols
# ─────────────────────────────────────────────────────────────────────────
class SymbolCatalogSsotTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _rendered_catalog(self):
        html = self._html()
        start = html.index("const MOBILE_SYMBOLS = ")
        end = html.index(";\n", start)
        return json.loads(html[start + len("const MOBILE_SYMBOLS = "):end])

    def test_catalog_matches_real_allowed_symbols(self):
        catalog = self._rendered_catalog()
        rendered_symbols = {item["symbol"] for item in catalog}
        self.assertEqual(rendered_symbols, set(allowed_symbols()))

    def test_no_disabled_symbol_exposed(self):
        catalog = self._rendered_catalog()
        rendered_symbols = {item["symbol"] for item in catalog}
        disabled = {sp.symbol for sp in get_all_specs() if not sp.enabled}
        self.assertTrue(disabled, "expected at least one disabled symbol in the real registry")
        self.assertEqual(rendered_symbols & disabled, set())

    def test_no_invented_symbol(self):
        catalog = self._rendered_catalog()
        real_symbols = {sp.symbol for sp in get_all_specs()}
        for item in catalog:
            self.assertIn(item["symbol"], real_symbols)

    def test_asset_class_matches_real_spec(self):
        catalog = self._rendered_catalog()
        real_by_symbol = {sp.symbol: sp.asset_class for sp in get_all_specs()}
        for item in catalog:
            self.assertEqual(item["asset_class"], real_by_symbol[item["symbol"]])

    def test_no_fabricated_fields(self):
        catalog = self._rendered_catalog()
        for item in catalog:
            self.assertEqual(set(item.keys()), {"symbol", "asset_class"})

    # Account types 31-34
    def _assert_catalog_for_account_type(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        start = html.index("const MOBILE_SYMBOLS = ")
        end = html.index(";\n", start)
        catalog = json.loads(html[start + len("const MOBILE_SYMBOLS = "):end])
        self.assertEqual({i["symbol"] for i in catalog}, set(allowed_symbols()))

    def test_standard_account_catalog(self):
        self._assert_catalog_for_account_type("STANDARD")

    def test_demo_account_catalog(self):
        self._assert_catalog_for_account_type("DEMO")

    def test_challenge_account_catalog(self):
        self._assert_catalog_for_account_type("CHALLENGE")

    def test_funded_account_catalog(self):
        self._assert_catalog_for_account_type("FUNDED")


# ─────────────────────────────────────────────────────────────────────────
# Mobile.html watchlist wiring
# ─────────────────────────────────────────────────────────────────────────
class MobileWatchlistWiringTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        return r.content.decode()

    def test_watchlist_container_present(self):
        self.assertIn('id="mobWatchlist"', self._html())

    def test_click_calls_select_symbol(self):
        html = self._html()
        self.assertIn("session.selectSymbol(item.symbol)", html)

    def test_allowed_symbols_passed_to_session_constructor(self):
        # PRE-VPS-POLISH-03C.2.4A — the old assertion was an exact,
        # whole-signature literal; 03C.2.4 legitimately extended the
        # constructor with 4 more trailing callback args (onHistory/
        # onCandleNew/onCandleUpdate/onVolumeUpdate), which made that
        # literal obsolete. This now proves the real functional
        # contract instead: the allowedSymbols expression must appear
        # as a genuine argument INSIDE the actual `new
        # MobileTradingSession(...)` call span (bounded below), not
        # merely as text anywhere on the page — and it must be
        # terminated by a comma or the call's closing paren, proving
        # it is a real positional argument rather than embedded inside
        # some other expression.
        html = self._html()
        start = html.index("new MobileTradingSession(")
        end = html.index(");", start) + 1
        call_text = html[start:end]
        self.assertRegex(
            call_text,
            r"MOBILE_SYMBOLS\.map\(function\(item\)\{\s*return item\.symbol;\s*\}\)\s*[,)]",
        )

    def test_no_second_onsymbolchange_callback_invented(self):
        html = self._html()
        self.assertNotIn("onSymbolChange", html)
        self.assertNotIn("onSymbolPending", html)


# ─────────────────────────────────────────────────────────────────────────
# Structural — mobile_session.js selectSymbol()/onopen contract
# ─────────────────────────────────────────────────────────────────────────
class SelectSymbolSourceContractTests(SimpleTestCase):
    def _select_symbol_body(self):
        src = _session_source()
        start = src.index("selectSymbol(symbol) {")
        end = src.index("\n  }\n\n", start)
        return src[start:end]

    def test_exact_change_symbol_payload(self):
        body = self._select_symbol_body()
        self.assertIn(
            "this.ws.send(JSON.stringify({ action: 'change_symbol', symbol: this.currentSymbol }))",
            body,
        )

    def test_quote_fields_reset_on_switch(self):
        body = self._select_symbol_body()
        for field in ("this.bid = null", "this.ask = null", "this.liveMid = null", "this.liveSource = null", "this.prevLiveMid = null"):
            self.assertIn(field, body)

    def test_on_quote_called_with_nulled_fields(self):
        body = self._select_symbol_body()
        self.assertIn("this.onQuote({", body)

    def test_validates_against_allowed_symbols(self):
        body = self._select_symbol_body()
        self.assertIn("this.allowedSymbols.includes(symbol)", body)

    def test_only_sends_when_ws_open(self):
        body = self._select_symbol_body()
        self.assertIn("this.ws.readyState === WebSocket.OPEN", body)

    def test_no_second_websocket_instantiated(self):
        # Exactly one `new WebSocket(` call anywhere in the file (inside
        # connect()) — selectSymbol() reuses the existing connection.
        src = _session_source()
        self.assertEqual(src.count("new WebSocket("), 1)

    def test_no_gate_duplication_still_holds(self):
        src = _session_source()
        self.assertNotIn("source!=='sim'", src)
        self.assertNotIn("source !== 'sim'", src)
        self.assertNotIn("ask>bid", src)
        self.assertNotIn("ask > bid", src)
        self.assertIn("applyPriceTickState(this.liveMid, bid, ask, source)", src)

    def test_reconnect_resends_current_symbol(self):
        src = _session_source()
        onopen_start = src.index("this.ws.onopen = () => {")
        onopen_end = src.index("\n    };", onopen_start)
        onopen_body = src[onopen_start:onopen_end]
        self.assertIn("if (this.currentSymbol) {", onopen_body)
        self.assertIn(
            "this.ws.send(JSON.stringify({ action: 'change_symbol', symbol: this.currentSymbol }))",
            onopen_body,
        )

    def test_tick_race_guard_unchanged(self):
        src = _session_source()
        self.assertIn(
            "if (this.currentSymbol && msg.symbol && msg.symbol !== this.currentSymbol) return;",
            src,
        )

    def test_accountid_not_touched_by_selection(self):
        body = self._select_symbol_body()
        self.assertNotIn("__TRADE_CONFIG__", body)
        self.assertNotIn("accountId", body)


# ─────────────────────────────────────────────────────────────────────────
# No new WS actions beyond the currently-authorized set
# ─────────────────────────────────────────────────────────────────────────
class NoOtherNewWsActionsTests(SimpleTestCase):
    def test_only_authorized_ws_actions_are_ever_sent(self):
        # PRE-VPS-POLISH-03C.2.4A — superseded the old "exactly 3 call
        # sites" structural count. PRE-VPS-POLISH-03C.2.5A then proved
        # the contract via static text instead — but 03C.2.6 added a
        # SECOND method (closePosition()) that also names its payload
        # variable `payload`, making its call site textually identical
        # to submitOrder()'s own ("JSON.stringify(payload)") and
        # impossible to disambiguate by a single "look up the payload
        # definition" text search.
        # PRE-VPS-POLISH-03C.2.6A — replaced with real execution: every
        # send-capable method is actually invoked, the real
        # this.ws.sent payloads are parsed, and the resulting set of
        # real `action` values is compared against the FULL currently-
        # authorized set — proving exactly what is sent, not guessing
        # from source text.
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")
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
        const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
        s.ws = new WebSocket('x');
        s.selectSymbol('EUR/USD');
        s.selectTimeframe('1h');
        s.submitOrder({{ side: 'buy', qty: 1 }});
        s._handleMsg({{ type: 'order_ack', order_id: 1, symbol: 'EUR/USD', side: 'buy', qty: 1 }});
        s.closePosition(42);
        s._handleMsg({{ type: 'order_close', id: 42 }});
        s.cancelPendingOrder(7);
        s._handleMsg({{ type: 'order_pending_cancel', id: 7 }});
        s.requestClosedTrades();
        s.requestRiskPreview('EUR/USD', 1);
        setTimeout(() => {{
          const actions = new Set(
            s.ws.sent
              .map((x) => {{ try {{ return JSON.parse(x).action; }} catch (e) {{ return null; }} }})
              .filter((a) => a)
          );
          console.log(JSON.stringify({{ actions: Array.from(actions).sort() }}));
        }}, 300);
        """
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            out["actions"],
            sorted([
                "change_symbol", "change_timeframe", "load_history",
                "order:new", "order:close", "order:pending:cancel",
                "get_closed_trades", "order:risk_preview",
            ]),
        )

    def test_no_other_action_payloads(self):
        # PRE-VPS-POLISH-03C.2.4A — change_timeframe (selectTimeframe()/
        # onopen reconnect resend) and load_history (the debounced
        # _requestHistory() helper) are now authorized real Mobile
        # actions — removed from this forbidden list for that reason
        # alone.
        # PRE-VPS-POLISH-03C.2.5A — order:new/order:risk_preview
        # (submitOrder()/requestRiskPreview()) are now, likewise,
        # authorized real Mobile actions — removed from this forbidden
        # list for that reason alone.
        # PRE-VPS-POLISH-03C.2.6A — order:close (closePosition()),
        # order:pending:cancel (cancelPendingOrder()), and
        # get_closed_trades (requestClosedTrades()) are now, likewise,
        # authorized real Mobile actions — removed from this forbidden
        # list for that reason alone. Every other financial/order/risk
        # action remains fully forbidden, unweakened.
        src = _session_source()
        for forbidden in (
            "action:'get_positions'", "action: 'get_positions'",
            "action:'order:update'", "action: 'order:update'",
            "action:'order:pending:new'", "action: 'order:pending:new'",
            "action:'order:pending:update'", "action: 'order:pending:update'",
        ):
            self.assertNotIn(forbidden, src)
        # The 5 actions currently authorized must each be real and
        # present — not simply absent-of-prohibition.
        for authorized in (
            "action: 'order:new'", "action: 'order:risk_preview'",
            "action: 'order:close'", "action: 'order:pending:cancel'",
            "action: 'get_closed_trades'",
        ):
            self.assertIn(authorized, src)

    def test_no_chart_or_history_handling(self):
        # PRE-VPS-POLISH-03C.2.4A — history/candle_new/candle_update/
        # volume_update are now real, authorized Mobile message types,
        # parsed and guarded in _handleMsg and forwarded to
        # MobileTradingChart via plain callbacks — removed from the
        # forbidden list for that reason alone. LightweightCharts/
        # candleSeries remain forbidden: this file (the transport
        # layer) must never itself touch the rendering library or own
        # a chart series — that stays exclusively mobile_chart.js's job.
        src = _session_source()
        for forbidden in ("LightweightCharts", "candleSeries"):
            self.assertNotIn(forbidden, src)
        for authorized in ("'history'", "'candle_new'", "'candle_update'", "'volume_update'"):
            self.assertIn(authorized, src)


# ─────────────────────────────────────────────────────────────────────────
# WS lifecycle unchanged (heartbeat/reconnect/wsUrl/provider/token)
# ─────────────────────────────────────────────────────────────────────────
class WsLifecycleStillUnchangedTests(SimpleTestCase):
    def test_heartbeat_interval_unchanged(self):
        self.assertIn('}, 15000);', _session_source())

    def test_reconnect_constants_unchanged(self):
        src = _session_source()
        self.assertIn("this.reconnDelay = 800;", src)
        self.assertIn("Math.min(5000, this.reconnDelay)", src)
        self.assertIn("this.reconnDelay * 1.7", src)

    def test_wsurl_construction_unchanged(self):
        src = _session_source()
        self.assertIn("window.__TRADE_CONFIG__.accountId", src)
        self.assertIn("'/ws/trading/' + accountId + '/'", src)
        self.assertIn("(u.protocol === 'https:') ? 'wss:' : 'ws:'", src)

    def test_provider_and_finnhub_unchanged(self):
        src = _session_source()
        self.assertIn("u.searchParams.set('provider', mobileGlobalProvider)", src)
        self.assertIn("mobileGlobalProvider === 'finnhub'", src)
        self.assertIn("localStorage.finnhubToken", src)


# ─────────────────────────────────────────────────────────────────────────
# Real execution — the actual selectSymbol() + real shared core
# ─────────────────────────────────────────────────────────────────────────
class MobileSymbolRealExecutionTests(SimpleTestCase):
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
            result = subprocess.run(
                ["node", str(path)], capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                self.fail(
                    f"node harness failed:\nSTDOUT:\n{result.stdout}\n"
                    f"STDERR:\n{result.stderr}"
                )
            return json.loads(result.stdout.strip().splitlines()[-1])

    # 3-10. selection mechanics
    def test_valid_selection_sets_current_symbol_and_sends_payload(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol, sent: s.ws.sent }));
        """)
        self.assertEqual(out["currentSymbol"], "BTCUSD")
        self.assertEqual(
            json.loads(out["sent"][0]), {"action": "change_symbol", "symbol": "BTCUSD"}
        )

    def test_invalid_symbol_rejected_client_side(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('NOT_A_REAL_SYMBOL');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol, sentCount: s.ws.sent.length }));
        """)
        self.assertIsNone(out["currentSymbol"])
        self.assertEqual(out["sentCount"], 0)

    def test_empty_catalog_does_not_block_selection(self):
        # No catalog provided (undefined) -> client-side validation is a
        # UX nicety only; the backend remains the real authority (see
        # 03C.2.3 preflight, Section U).
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol }));
        """)
        self.assertEqual(out["currentSymbol"], "EUR/USD")

    def test_quote_state_cleared_and_on_quote_receives_nulls(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, (q) => { received = q; }, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          s.selectSymbol('BTCUSD');
          console.log(JSON.stringify({ received, bid: s.bid, ask: s.ask, liveMid: s.liveMid, prevLiveMid: s.prevLiveMid }));
        """)
        self.assertEqual(out["received"], {"symbol": "BTCUSD", "bid": None, "ask": None, "liveMid": None, "liveSource": None})
        self.assertIsNone(out["bid"])
        self.assertIsNone(out["ask"])
        self.assertIsNone(out["liveMid"])
        self.assertIsNone(out["prevLiveMid"])

    # 11-13. race conditions — the exact scenario specified
    def test_stale_old_symbol_tick_rejected_after_switch(self):
        out = self._run_node("""
          const quotes = [];
          const s = new MobileTradingSession(null, null, (q) => { quotes.push(q); }, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          s.selectSymbol('BTCUSD');
          const beforeStale = quotes.length;
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1001, ask: 1.1003, source: 'massive' });
          const afterStale = quotes.length;
          s._handleMsg({ type: 'tick', symbol: 'BTCUSD', bid: 60000, ask: 60010, source: 'massive' });
          const afterCorrect = quotes.length;
          console.log(JSON.stringify({ beforeStale, afterStale, afterCorrect, finalSymbol: s.currentSymbol, finalBid: s.bid }));
        """)
        self.assertEqual(out["afterStale"], out["beforeStale"])
        self.assertEqual(out["afterCorrect"], out["beforeStale"] + 1)
        self.assertEqual(out["finalSymbol"], "BTCUSD")
        self.assertAlmostEqual(out["finalBid"], 60000, places=2)

    def test_prev_live_mid_does_not_leak_across_switch(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          s.selectSymbol('BTCUSD');
          s._handleMsg({ type: 'tick', symbol: 'BTCUSD', bid: 60000, ask: 60010, source: 'massive' });
          console.log(JSON.stringify({ prevLiveMid: s.prevLiveMid, liveMid: s.liveMid }));
        """)
        self.assertIsNone(out["prevLiveMid"])
        self.assertAlmostEqual(out["liveMid"], 60005, places=2)

    # 14-18. edge cases
    def test_disconnected_selection_retained_no_send(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          // s.ws stays null -- never connected
          s.selectSymbol('EUR/USD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol, wsIsNull: s.ws === null }));
        """)
        self.assertEqual(out["currentSymbol"], "EUR/USD")
        self.assertTrue(out["wsIsNull"])

    def test_reconnect_resends_current_symbol_via_onopen(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('BTCUSD');
          const sentBeforeReconnect = s.ws.sent.length;
          // simulate reconnect: a brand new onopen on a fresh socket
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ sentAfterReopen: s.ws.sent.slice(sentBeforeReconnect) }));
        """)
        self.assertEqual(
            json.loads(out["sentAfterReopen"][0]),
            {"action": "change_symbol", "symbol": "BTCUSD"},
        )

    def test_no_symbol_or_history_resend_but_timeframe_resend_without_prior_selection(self):
        # PRE-VPS-POLISH-03C.2.4A — the old contract ("nothing at all is
        # resent on first connect without a prior symbol selection") is
        # no longer correct: 03C.2.4 legitimately gave currentTF a real,
        # always-present default ('15m', unlike currentSymbol which
        # starts null), and onopen now always resends change_timeframe
        # — mirroring Desktop, which always has a timeframe. The real
        # contract is: no symbol selected -> no change_symbol AND no
        # load_history (both need a real symbol); but change_timeframe
        # for the default IS sent, every connect, regardless.
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ sent: s.ws.sent }));
        """)
        payloads = [json.loads(x) for x in out["sent"]]
        self.assertFalse(any(p.get("action") == "change_symbol" for p in payloads))
        self.assertFalse(any(p.get("action") == "load_history" for p in payloads))
        self.assertIn({"action": "change_timeframe", "timeframe": "15m"}, payloads)

    def test_repeated_same_symbol_selection(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectSymbol('EUR/USD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol, sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["currentSymbol"], "EUR/USD")
        self.assertEqual(out["sentCount"], 2)

    def test_rapid_a_b_c_selection_final_is_authoritative(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'GBP/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectSymbol('GBP/USD');
          s.selectSymbol('BTCUSD');
          // stale ticks for the two abandoned symbols must still be rejected
          const quotes = [];
          s.onQuote = (q) => quotes.push(q);
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1, ask: 1.2, source: 'massive' });
          s._handleMsg({ type: 'tick', symbol: 'GBP/USD', bid: 1.3, ask: 1.4, source: 'massive' });
          s._handleMsg({ type: 'tick', symbol: 'BTCUSD', bid: 60000, ask: 60010, source: 'massive' });
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol, quoteCount: quotes.length, lastSymbol: quotes[0] && quotes[0].symbol }));
        """)
        self.assertEqual(out["currentSymbol"], "BTCUSD")
        self.assertEqual(out["quoteCount"], 1)
        self.assertEqual(out["lastSymbol"], "BTCUSD")

    # 19-20. invariants: applyPriceTickState remains sole authority
    def test_apply_price_tick_state_still_sole_authority(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ liveMid: s.liveMid }));
        """)
        self.assertAlmostEqual(out["liveMid"], 1.1001, places=6)

    # 21-23. fail-closed invariants still hold through a selection
    def test_sim_source_still_fails_closed_after_selection(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'sim' });
          console.log(JSON.stringify({ bid: s.bid }));
        """)
        self.assertIsNone(out["bid"])

    def test_missing_source_still_fails_closed_after_selection(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: null });
          console.log(JSON.stringify({ bid: s.bid }));
        """)
        self.assertIsNone(out["bid"])

    def test_ask_less_or_equal_bid_still_fails_closed_after_selection(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1002, ask: 1.1000, source: 'massive' });
          console.log(JSON.stringify({ bid: s.bid }));
        """)
        self.assertIsNone(out["bid"])

    # 24/25. account:update / account:snapshot unaffected
    def test_account_update_unaffected_by_symbol_selection(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; }, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(out["balance"], 1000)

    def test_account_snapshot_unaffected_by_symbol_selection(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; }, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'account:snapshot', balance: 500 });
          console.log(JSON.stringify({ gotIt: received !== null }));
        """)
        self.assertTrue(out["gotIt"])

    # 35. security/accountId unchanged by selection
    def test_accountid_config_untouched_by_selection(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 77 };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          console.log(JSON.stringify({ accountId: global.window.__TRADE_CONFIG__.accountId }));
        """)
        self.assertEqual(out["accountId"], 77)


# ─────────────────────────────────────────────────────────────────────────
# 28-30. Desktop / backend / trading_core.js / financial engine zero diff
# ─────────────────────────────────────────────────────────────────────────
class DesktopAndBackendZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(
            result.returncode, 0, f"{path} has a diff against HEAD, expected none"
        )

    def test_desktop_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/desktop.html")

    def test_shell_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/shell.html")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

    def test_consumers_py_zero_diff(self):
        self._assert_zero_diff("simulator/consumers.py")

    def test_routing_py_zero_diff(self):
        self._assert_zero_diff("simulator/routing.py")

    def test_models_py_zero_diff(self):
        self._assert_zero_diff("simulator/models.py")

    def test_symbol_specs_py_zero_diff(self):
        self._assert_zero_diff("market_data/symbol_specs.py")

    def test_spread_engine_py_zero_diff(self):
        self._assert_zero_diff("simulator/spread_engine.py")
