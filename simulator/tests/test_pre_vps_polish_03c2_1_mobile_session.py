# simulator/tests/test_pre_vps_polish_03c2_1_mobile_session.py
"""
PRE-VPS-POLISH-03C.2.1 — Mobile session + account identity + connection
status.

Verifies that mobile.html now loads trading_core.js, receives a
server-rendered window.__TRADE_CONFIG__.accountId (same pattern as
03B.2B), and wires up a new, minimal MobileTradingSession (simulator/
static/simulator/trade/mobile_session.js) that connects to the EXACT
same /ws/trading/<accountId>/ endpoint, same provider/heartbeat/
reconnect protocol, and the same account:update/account:snapshot
handling as Desktop's TradingPanel — with zero quotes/chart/history/
order-flow code anywhere in this sub-block.

Structural assertions use the same source-inspection convention already
established throughout this series. Real-execution assertions run the
actual MobileTradingSession class via Node (same hybrid convention as
test_pre_vps_polish_03b2c2a_price_tick_shared_state.py) — skipped
gracefully when `node` is not on PATH.
"""
import inspect
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.consumers import TradingConsumer
from simulator.tests.factories import make_account, make_user

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


def _mobile_template_source() -> str:
    path = get_template("simulator/trade/mobile.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _session_source() -> str:
    with open(
        "simulator/static/simulator/trade/mobile_session.js", encoding="utf-8"
    ) as f:
        return f.read()


def _core_source() -> str:
    # PRE-VPS-POLISH-03C.2.2A — mobile_session.js now has a real,
    # approved dependency on trading_core.js's applyPriceTickState()
    # (03C.2.2); the Node harness below must load the real core source
    # too, so _handleMsg() runs against the actual shared function —
    # never a mock/stub/reimplementation of the FIX-05C gate.
    with open(
        "simulator/static/simulator/trade/trading_core.js", encoding="utf-8"
    ) as f:
        return f.read()


NODE_AVAILABLE = shutil.which("node") is not None


# ─────────────────────────────────────────────────────────────────────────
# Structural — mobile.html wiring
# ─────────────────────────────────────────────────────────────────────────
class MobileTemplateWiringTests(TestCase):
    def _html_for(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return account, r.content.decode()

    # 1. Mobile loads trading_core.js
    # Note: WhiteNoise's ManifestStaticFilesStorage rewrites
    # {% static %} to a content-hashed filename (e.g.
    # trading_core.<hash>.js), so the rendered HTML never contains the
    # literal unhashed path — assert on the stable basename prefix,
    # the same way the static URL always resolves.
    def test_mobile_loads_trading_core(self):
        _, html = self._html_for("STANDARD")
        self.assertIn("simulator/trade/trading_core", html)

    def test_mobile_loads_session_script(self):
        _, html = self._html_for("STANDARD")
        self.assertIn("simulator/trade/mobile_session", html)

    # 2-6. __TRADE_CONFIG__.accountId matches the real account, per type
    def _assert_account_id_matches(self, account_type):
        account, html = self._html_for(account_type)
        self.assertIn(f"accountId: {account.id}", html)

    def test_account_id_matches_standard(self):
        self._assert_account_id_matches("STANDARD")

    def test_account_id_matches_demo(self):
        self._assert_account_id_matches("DEMO")

    def test_account_id_matches_challenge(self):
        self._assert_account_id_matches("CHALLENGE")

    def test_account_id_matches_funded(self):
        self._assert_account_id_matches("FUNDED")

    # 22-25. No order/trading/chart/history functionality exists yet
    def test_no_order_actions_in_mobile_html(self):
        _, html = self._html_for("STANDARD")
        self.assertNotIn("order:new", html)
        self.assertNotIn("order:close", html)
        self.assertNotIn("order:update", html)
        self.assertNotIn("risk_preview", html)

    def test_no_chart_or_history_in_mobile_html(self):
        _, html = self._html_for("STANDARD")
        self.assertNotIn("LightweightCharts", html)
        self.assertNotIn("load_history", html)
        self.assertNotIn("candle_update", html)
        self.assertNotIn("candle_new", html)
        self.assertNotIn("change_symbol", html)
        self.assertNotIn("change_timeframe", html)


class MobileSessionSourceContractTests(SimpleTestCase):
    # 7-10. wsUrl() construction mirrors Desktop's exactly
    def _wsurl_block(self):
        src = _session_source()
        start = src.index("wsUrl() {")
        end = src.index("\n  }", start)
        return src[start:end]

    def test_wsurl_uses_trade_config_account_id(self):
        block = self._wsurl_block()
        self.assertIn("window.__TRADE_CONFIG__.accountId", block)
        self.assertIn("'/ws/trading/' + accountId + '/'", block)

    def test_wsurl_protocol_flip_preserved(self):
        block = self._wsurl_block()
        self.assertIn("(u.protocol === 'https:') ? 'wss:' : 'ws:'", block)

    def test_wsurl_provider_param_preserved(self):
        block = self._wsurl_block()
        self.assertIn("u.searchParams.set('provider', mobileGlobalProvider)", block)

    def test_wsurl_finnhub_token_behavior_preserved(self):
        block = self._wsurl_block()
        self.assertIn("mobileGlobalProvider === 'finnhub'", block)
        self.assertIn("localStorage.finnhubToken", block)

    def test_provider_defaults_like_desktop(self):
        src = _session_source()
        self.assertIn("localStorage.provider || 'sim'", src)

    # 11. heartbeat = 15000ms
    def test_heartbeat_interval_15000ms(self):
        src = _session_source()
        self.assertIn('}, 15000);', src)

    # 12-14. reconnect constants
    def test_reconnect_initial_800ms(self):
        src = _session_source()
        self.assertIn("this.reconnDelay = 800;", src)

    def test_reconnect_cap_5000ms(self):
        src = _session_source()
        self.assertIn("Math.min(5000, this.reconnDelay)", src)

    def test_reconnect_growth_factor_1_7(self):
        src = _session_source()
        self.assertIn("this.reconnDelay * 1.7", src)

    # 15-16. account:update / account:snapshot both handled
    def test_handles_account_update_and_snapshot(self):
        src = _session_source()
        self.assertIn("msg.type === 'account:update'", src)
        self.assertIn("msg.type === 'account:snapshot'", src)

    # 22-24. no order/BUY/SELL/risk-preview action anywhere in the session
    def test_session_sends_no_order_or_risk_actions(self):
        # PRE-VPS-POLISH-03C.2.2A — check the real action PAYLOAD pattern
        # (action:'...'), not a bare word: 03C.2.2 added a documentation
        # comment that mentions "change_symbol" by name precisely to
        # describe its absence, which a bare substring match would
        # misread as the action itself. The contract under test is
        # unchanged and, if anything, pinned more precisely: Mobile must
        # never construct any of these as an outbound action.
        src = _session_source()
        for forbidden in (
            "action:'order:new'", "action: 'order:new'",
            "action:'order:close'", "action: 'order:close'",
            "action:'order:update'", "action: 'order:update'",
            "action:'risk_preview'", "action: 'risk_preview'",
            "'BUY'", "'SELL'",
            "action:'get_positions'", "action: 'get_positions'",
            "action:'get_closed_trades'", "action: 'get_closed_trades'",
            "action:'change_symbol'", "action: 'change_symbol'",
            "action:'change_timeframe'", "action: 'change_timeframe'",
        ):
            self.assertNotIn(forbidden, src)

    # 25. no chart/history/candle handling in the session
    def test_session_has_no_chart_or_history_handling(self):
        src = _session_source()
        for forbidden in (
            "LightweightCharts", "candleSeries", "load_history",
            "candle_update", "candle_new",
        ):
            self.assertNotIn(forbidden, src)


class MobileAccountDisplaySourceContractTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        return r.content.decode()

    # 17-21. Account fields come straight from the payload — same
    # fallback semantics as desktop.html's renderAccount(), preserved
    # verbatim, nothing additionally recalculated.
    def test_free_margin_fallback_preserved(self):
        html = self._html()
        self.assertIn(
            "msg.free_margin!=null?Number(msg.free_margin):equity-margin", html
        )

    def test_upnl_fallback_preserved(self):
        html = self._html()
        self.assertIn("Number(msg.upnl??msg.pnl_unreal??0)", html)

    def test_balance_equity_margin_from_payload(self):
        html = self._html()
        self.assertIn("usd(msg.balance)", html)
        self.assertIn("usd(equity)", html)
        self.assertIn("usd(margin)", html)
        self.assertIn("usd(free)", html)

    def test_leverage_from_payload(self):
        html = self._html()
        self.assertIn(
            "msg.leverage!=null?String(msg.leverage)+'x':'—'", html
        )


# ─────────────────────────────────────────────────────────────────────────
# 28. Ownership/security boundary unchanged (source-contract on consumers.py)
# ─────────────────────────────────────────────────────────────────────────
class OwnershipBoundaryUnchangedTests(SimpleTestCase):
    def test_db_get_account_for_user_still_filters_by_user_id(self):
        src = inspect.getsource(TradingConsumer._db_get_account_for_user)
        self.assertIn("id=acc_id, user_id=user_id", src)


# ─────────────────────────────────────────────────────────────────────────
# Real execution — the actual MobileTradingSession class
# ─────────────────────────────────────────────────────────────────────────
class MobileSessionRealExecutionTests(SimpleTestCase):
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
          constructor(url){{ this.url = url; this.readyState = 0; FakeWS.instances.push(this); }}
          send(){{}}
          close(){{}}
        }}
        FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
        FakeWS.instances = [];
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

    # 7/8. URL contains accountId, http -> ws
    def test_ws_url_contains_account_id_and_ws_protocol(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 42 };
          global.window.location = { href: 'http://example.com/dashboard/42/' };
          const s = new MobileTradingSession();
          console.log(JSON.stringify({ url: s.wsUrl() }));
        """)
        self.assertIn("/ws/trading/42/", out["url"])
        self.assertTrue(out["url"].startswith("ws://"))

    # 9. https -> wss
    def test_ws_url_https_becomes_wss(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 7 };
          global.window.location = { href: 'https://example.com/dashboard/7/' };
          const s = new MobileTradingSession();
          console.log(JSON.stringify({ url: s.wsUrl() }));
        """)
        self.assertTrue(out["url"].startswith("wss://"))

    # 10. provider param preserved
    def test_ws_url_has_provider_param(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 7 };
          global.window.location = { href: 'http://example.com/dashboard/7/' };
          global.localStorage.provider = 'massive';
          mobileGlobalProvider = global.localStorage.provider || 'sim';
          const s = new MobileTradingSession();
          console.log(JSON.stringify({ url: s.wsUrl() }));
        """)
        self.assertIn("provider=massive", out["url"])

    # 15/16/17-21. account:update handled, fields passed through untouched
    def test_handle_msg_passes_account_update_through_unmodified(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; });
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(
            out,
            {"type": "account:update", "balance": 1000, "equity": 950, "margin_used": 100, "upnl": -50, "leverage": 50},
        )

    def test_handle_msg_passes_account_snapshot_through(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; });
          s._handleMsg({ type: 'account:snapshot', balance: 500 });
          console.log(JSON.stringify({ gotIt: received !== null }));
        """)
        self.assertTrue(out["gotIt"])

    def test_handle_msg_ignores_other_message_types(self):
        # PRE-VPS-POLISH-03C.2.2A — 'price' is no longer an unsupported
        # type (03C.2.2 made it, alongside 'tick', a real, handled quote
        # message — see test_pre_vps_polish_03c2_2_mobile_live_quotes.py,
        # now the contractual authority for price/tick). Replaced with a
        # genuinely unsupported synthetic type so this test keeps
        # demonstrating its real intent: unsupported messages are still
        # ignored. 'history' is unchanged — still genuinely unhandled.
        out = self._run_node("""
          let called = false;
          const s = new MobileTradingSession(null, () => { called = true; });
          s._handleMsg({ type: '__unsupported_test_message_type__' });
          s._handleMsg({ type: 'history', data: [] });
          console.log(JSON.stringify({ called }));
        """)
        self.assertFalse(out["called"])

    def test_status_callback_reaches_connecting_then_connected(self):
        out = self._run_node("""
          const states = [];
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession((st) => states.push(st), null);
          s.connect();
          s.ws.onopen();
          clearInterval(s.hb);
          console.log(JSON.stringify({ states }));
        """)
        self.assertEqual(out["states"], ["CONNECTING", "CONNECTED"])

    def test_reconnect_delay_grows_after_close(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null);
          s.connect();
          const before = s.reconnDelay;
          s.ws.onclose();
          console.log(JSON.stringify({ before, after: s.reconnDelay }));
        """)
        self.assertEqual(out["before"], 800)
        self.assertAlmostEqual(out["after"], 800 * 1.7, places=3)
