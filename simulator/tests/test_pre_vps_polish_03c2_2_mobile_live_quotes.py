# simulator/tests/test_pre_vps_polish_03c2_2_mobile_live_quotes.py
"""
PRE-VPS-POLISH-03C.2.2 — Mobile live quotes / bid-ask.

Verifies that MobileTradingSession (simulator/static/simulator/trade/
mobile_session.js) now handles real WS price/tick messages by calling
trading_core.js's REAL applyPriceTickState() — the FIX-05C fail-closed
gate is never duplicated here — and forwards the result via a new
onQuote callback, following the same pattern as the existing
onStatusChange/onAccount callbacks (03C.2.1). mobile.html gains a
minimal SYMBOL/BID/ASK/MID/SOURCE display, sourced exclusively from
that shared state (no second mid calculation anywhere in the template).

No new WS message is sent by this sub-block itself (no order:*/risk:*/
get_positions/get_closed_trades) — Mobile only consumes ticks the
backend already streams to every connection by default. (change_symbol,
change_timeframe, and load_history were separately authorized by later
sub-blocks — 03C.2.3 and 03C.2.4 respectively — and are covered by
their own test files; see NoNewWsMessagesTests below for the current,
updated allowlist.)

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
    with open(
        "simulator/static/simulator/trade/trading_core.js", encoding="utf-8"
    ) as f:
        return f.read()


NODE_AVAILABLE = shutil.which("node") is not None


# ─────────────────────────────────────────────────────────────────────────
# Structural — mobile_session.js: no gate duplication, real reuse
# ─────────────────────────────────────────────────────────────────────────
class NoGateDuplicationTests(SimpleTestCase):
    # 39/40. Mobile uses the real shared function; never re-expresses
    # the FIX-05C condition itself.
    def test_session_calls_real_shared_function(self):
        src = _session_source()
        self.assertIn(
            "applyPriceTickState(this.liveMid, bid, ask, source)", src
        )

    def test_session_does_not_duplicate_fix05c_condition(self):
        src = _session_source()
        self.assertNotIn("source!=='sim'", src)
        self.assertNotIn("source !== 'sim'", src)
        self.assertNotIn("ask>bid", src)
        self.assertNotIn("ask > bid", src)

    def test_session_has_no_second_mid_formula(self):
        src = _session_source()
        self.assertNotIn("(ask+bid)/2", src)
        self.assertNotIn("(ask + bid) / 2", src)
        self.assertNotIn("(bid+ask)/2", src)
        self.assertNotIn("(bid + ask) / 2", src)

    def test_session_does_not_redefine_apply_price_tick_state(self):
        src = _session_source()
        self.assertNotIn("function applyPriceTickState", src)
        self.assertNotIn("applyMobilePriceTickState", src)
        self.assertNotIn("mobilePriceGate", src)
        self.assertNotIn("validateMobileQuote", src)


# ─────────────────────────────────────────────────────────────────────────
# Structural — price/tick branch shape, quote state init
# ─────────────────────────────────────────────────────────────────────────
class PriceTickBranchSourceContractTests(SimpleTestCase):
    def _tick_branch(self):
        src = _session_source()
        start = src.index("if (msg.type === 'price' || msg.type === 'tick')")
        end = src.index("\n  }\n}", start)
        return src[start:end]

    # 3/4. price and tick both recognized
    def test_recognizes_both_price_and_tick_types(self):
        branch = self._tick_branch()
        self.assertIn("msg.type === 'price'", branch)
        self.assertIn("msg.type === 'tick'", branch)

    # 9. symbol comes from msg.symbol
    def test_symbol_comes_from_message(self):
        branch = self._tick_branch()
        self.assertIn("this.currentSymbol = msg.symbol || this.currentSymbol", branch)

    # 18/19. best_bid/best_ask fallback, same chain as Desktop
    def test_best_bid_best_ask_fallback_present(self):
        branch = self._tick_branch()
        self.assertIn("msg.bid ?? msg.best_bid ?? null", branch)
        self.assertIn("msg.ask ?? msg.best_ask ?? null", branch)

    def test_quote_state_initialized_to_null(self):
        # PRE-VPS-POLISH-03C.2.3A — locate the constructor by its stable
        # "constructor(" boundary rather than the exact, now-obsolete
        # 3-argument literal (03C.2.3 legitimately added a 4th
        # allowedSymbols parameter) — stays stable if the parameter list
        # changes again. The quote-state contract itself is unchanged.
        src = _session_source()
        ctor_start = src.index("constructor(")
        ctor_end = src.index("\n  }", ctor_start)
        block = src[ctor_start:ctor_end]
        for field in (
            "this.currentSymbol = null",
            "this.bid = null",
            "this.ask = null",
            "this.liveMid = null",
            "this.liveSource = null",
            "this.prevLiveMid = null",
        ):
            self.assertIn(field, block)

    def test_on_quote_callback_has_safe_default(self):
        src = _session_source()
        self.assertIn("this.onQuote = onQuote || (() => {})", src)


# ─────────────────────────────────────────────────────────────────────────
# 23-27. No new WS messages anywhere in the session
# ─────────────────────────────────────────────────────────────────────────
class NoNewWsMessagesTests(SimpleTestCase):
    def test_no_subscription_or_order_messages(self):
        # Check for the real outbound-message pattern (action:'...'), not
        # the bare word — this file's own documentation comments mention
        # "change_symbol" by name precisely to document its absence.
        #
        # PRE-VPS-POLISH-03C.2.3 — change_symbol is now an authorized
        # Mobile action (symbol selector/watchlist), sent ONLY from
        # selectSymbol() and from onopen's reconnect-restoration resend
        # — removed from this forbidden list for that reason alone.
        # PRE-VPS-POLISH-03C.2.4A — change_timeframe (selectTimeframe()/
        # onopen reconnect resend) and load_history (the debounced
        # _requestHistory() helper) are likewise now authorized real
        # Mobile actions — removed from this forbidden list for that
        # reason alone. get_positions/get_closed_trades/order:*/risk:*
        # remain fully forbidden, unweakened.
        src = _session_source()
        for forbidden in (
            "action:'get_positions'", "action: 'get_positions'",
            "action:'get_closed_trades'", "action: 'get_closed_trades'",
            "action:'order:", "action: 'order:",
            "action:'risk:", "action: 'risk:",
            "risk_preview",
        ):
            self.assertNotIn(forbidden, src)

    def test_only_authorized_ws_actions_are_ever_sent(self):
        # PRE-VPS-POLISH-03C.2.4A — superseded the old "exactly 3 call
        # sites" structural count (03C.2.4 legitimately added 3 more
        # real send sites for change_timeframe/load_history). Rather than
        # replace one brittle magic number with another ("exactly 6"),
        # this proves the actual contract semantically: every single
        # this.ws.send(...) call site in the file is either the literal
        # ping payload, or a JSON.stringify({action:'...'}) call whose
        # action is one of the 3 authorized WS actions — so no other
        # action (financial, order, risk, or otherwise) can ever be
        # constructed here, regardless of how many call sites exist.
        src = _session_source()
        action_values = set(re.findall(r"action:\s*'([a-zA-Z_]+)'", src))
        self.assertEqual(action_values, {"change_symbol", "change_timeframe", "load_history"})

        send_sites = re.findall(r"this\.ws\.send\(([^;]*)\);", src)
        self.assertTrue(send_sites, "expected at least one this.ws.send( call site")
        allowed_actions = ("change_symbol", "change_timeframe", "load_history")
        for site in send_sites:
            is_ping = site == '\'{"action":"ping"}\''
            is_authorized_json = any(f"action: '{a}'" in site for a in allowed_actions)
            self.assertTrue(
                is_ping or is_authorized_json,
                f"unexpected this.ws.send(...) payload shape, not ping nor an authorized action: {site}",
            )


# ─────────────────────────────────────────────────────────────────────────
# 28-32. WS lifecycle (heartbeat/reconnect/wsUrl/provider/token) intact
# ─────────────────────────────────────────────────────────────────────────
class WsLifecycleUnchangedTests(SimpleTestCase):
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

    def test_provider_param_unchanged(self):
        src = _session_source()
        self.assertIn("u.searchParams.set('provider', mobileGlobalProvider)", src)
        self.assertIn("localStorage.provider || 'sim'", src)

    def test_finnhub_token_behavior_unchanged(self):
        src = _session_source()
        self.assertIn("mobileGlobalProvider === 'finnhub'", src)
        self.assertIn("localStorage.finnhubToken", src)


# ─────────────────────────────────────────────────────────────────────────
# Mobile.html wiring + UI foundation
# ─────────────────────────────────────────────────────────────────────────
class MobileQuoteUiTests(TestCase):
    def _html_for(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # 33-37. Stable IDs present
    def test_symbol_bid_ask_mid_source_ids_present(self):
        html = self._html_for("STANDARD")
        for stable_id in (
            'id="mobQuoteSymbol"', 'id="mobQuoteBid"', 'id="mobQuoteAsk"',
            'id="mobQuoteMid"', 'id="mobQuoteSource"',
        ):
            self.assertIn(stable_id, html)

    # 38. mobile.html does not compute its own mid
    def test_mobile_html_does_not_compute_mid(self):
        html = self._html_for("STANDARD")
        self.assertNotIn("(bid+ask)/2", html)
        self.assertNotIn("(bid + ask) / 2", html)
        self.assertNotIn("(state.bid+state.ask)/2", html)

    def test_mid_displayed_from_shared_state_only(self):
        html = self._html_for("STANDARD")
        self.assertIn("fmt(state.liveMid)", html)

    def test_source_displayed_from_shared_state(self):
        html = self._html_for("STANDARD")
        self.assertIn("state.liveSource", html)

    def test_on_quote_wired_into_session_constructor(self):
        # PRE-VPS-POLISH-03C.2.3A — the old single-line literal is
        # obsolete: 03C.2.3 legitimately added a 4th argument (the
        # server-derived allowed-symbol catalog), making the real call
        # multiline. Verify the actual wiring — all 4 real arguments —
        # rather than either the stale literal or a weak substring check.
        html = self._html_for("STANDARD")
        start = html.index("new MobileTradingSession(")
        end = html.index(");", start)
        block = html[start:end]
        self.assertIn("onStatusChange", block)
        self.assertIn("onAccount", block)
        self.assertIn("onQuote", block)
        self.assertIn(
            "MOBILE_SYMBOLS.map(function(item){ return item.symbol; })", block
        )

    # No chart/order/position/SL-TP/bottom-nav-final UI introduced.
    # PRE-VPS-POLISH-03C.2.3A — "watchlist" removed from this forbidden
    # list ONLY: 03C.2.3 explicitly, authorizedly introduces the Mobile
    # watchlist/symbol selector (its own existence/correctness is proven
    # by test_pre_vps_polish_03c2_3_mobile_symbol_selector.py). Every
    # other prohibition here is unchanged and still fully enforced.
    def test_no_chart_or_order_ui_introduced(self):
        html = self._html_for("STANDARD")
        for forbidden in (
            "LightweightCharts", "BUY", "SELL", "order:new",
            "position",
        ):
            self.assertNotIn(forbidden, html)

    # 43-46. All 4 account types still render Mobile correctly
    def test_standard_renders(self):
        html = self._html_for("STANDARD")
        self.assertIn('id="mobQuoteSymbol"', html)

    def test_demo_renders(self):
        html = self._html_for("DEMO")
        self.assertIn('id="mobQuoteSymbol"', html)

    def test_challenge_renders(self):
        html = self._html_for("CHALLENGE")
        self.assertIn('id="mobQuoteSymbol"', html)

    def test_funded_renders(self):
        html = self._html_for("FUNDED")
        self.assertIn('id="mobQuoteSymbol"', html)


# ─────────────────────────────────────────────────────────────────────────
# Real execution — the actual MobileTradingSession + real shared core
# ─────────────────────────────────────────────────────────────────────────
class MobileQuoteRealExecutionTests(SimpleTestCase):
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
          constructor(url){{ this.url = url; this.readyState = 0; }}
          send(){{}}
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

    # 1. MobileTradingSession accepts onQuote
    def test_accepts_on_quote_callback(self):
        out = self._run_node("""
          let called = false;
          const s = new MobileTradingSession(null, null, () => { called = true; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ called }));
        """)
        self.assertTrue(out["called"])

    # 2. Initial quote state is null
    def test_initial_quote_state_is_null(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          console.log(JSON.stringify({
            currentSymbol: s.currentSymbol, bid: s.bid, ask: s.ask,
            liveMid: s.liveMid, liveSource: s.liveSource, prevLiveMid: s.prevLiveMid,
          }));
        """)
        self.assertEqual(
            out,
            {"currentSymbol": None, "bid": None, "ask": None, "liveMid": None, "liveSource": None, "prevLiveMid": None},
        )

    # 3/4. price and tick both reach the callback
    def test_price_type_recognized(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'price', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ got }));
        """)
        self.assertIsNotNone(out["got"])

    def test_tick_type_recognized(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ got }));
        """)
        self.assertIsNotNone(out["got"])

    # 5/6/7/8/9. valid bid/ask/liveMid/liveSource/symbol reach the callback
    def test_valid_tick_fields_reach_callback(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify(got));
        """)
        self.assertEqual(out["symbol"], "EUR/USD")
        self.assertAlmostEqual(out["bid"], 1.1000, places=6)
        self.assertAlmostEqual(out["ask"], 1.1002, places=6)
        self.assertAlmostEqual(out["liveMid"], 1.1001, places=6)
        self.assertEqual(out["liveSource"], "massive")

    # 10. prevLiveMid correct across sequential ticks — the exact
    # Owner-specified sequence, executed against the REAL shared function.
    def test_prev_live_mid_across_sequential_ticks(self):
        out = self._run_node("""
          const quotes = [];
          const s = new MobileTradingSession(null, null, (q) => { quotes.push(q); });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1002, ask: 1.1004, source: 'massive' });
          console.log(JSON.stringify({
            tick1LiveMid: quotes[0].liveMid,
            tick2PrevLiveMid: s.prevLiveMid,
          }));
        """)
        self.assertAlmostEqual(out["tick1LiveMid"], out["tick2PrevLiveMid"], places=6)

    # 11/12/13/14/15/16. fail-closed scenarios — zero state/callback update
    def _assert_fails_closed(self, msg_overrides):
        out = self._run_node(f"""
          let called = false;
          const s = new MobileTradingSession(null, null, () => {{ called = true; }});
          const before = {{ bid: s.bid, ask: s.ask, liveMid: s.liveMid }};
          s._handleMsg({json.dumps({"type": "tick", "symbol": "EUR/USD", **msg_overrides})});
          console.log(JSON.stringify({{ called, bidAfter: s.bid, askAfter: s.ask, liveMidAfter: s.liveMid }}));
        """)
        self.assertFalse(out["called"])
        self.assertIsNone(out["bidAfter"])
        self.assertIsNone(out["askAfter"])
        self.assertIsNone(out["liveMidAfter"])

    def test_source_sim_fails_closed(self):
        self._assert_fails_closed({"bid": 1.1000, "ask": 1.1002, "source": "sim"})

    def test_source_missing_fails_closed(self):
        self._assert_fails_closed({"bid": 1.1000, "ask": 1.1002, "source": None})

    def test_bid_missing_fails_closed(self):
        self._assert_fails_closed({"bid": None, "ask": 1.1002, "source": "massive"})

    def test_ask_missing_fails_closed(self):
        self._assert_fails_closed({"bid": 1.1000, "ask": None, "source": "massive"})

    def test_ask_equal_bid_fails_closed(self):
        self._assert_fails_closed({"bid": 1.1000, "ask": 1.1000, "source": "massive"})

    def test_ask_less_than_bid_fails_closed(self):
        self._assert_fails_closed({"bid": 1.1002, "ask": 1.1000, "source": "massive"})

    # 17. unknown-but-real provider still permitted if gate passes
    def test_unknown_real_source_still_permitted(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'unknown' });
          console.log(JSON.stringify(got));
        """)
        self.assertEqual(out["liveSource"], "unknown")

    # 18/19. best_bid/best_ask fallback actually works end-to-end
    def test_best_bid_fallback_works(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', best_bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify(got));
        """)
        self.assertAlmostEqual(out["bid"], 1.1000, places=6)

    def test_best_ask_fallback_works(self):
        out = self._run_node("""
          let got = null;
          const s = new MobileTradingSession(null, null, (q) => { got = q; });
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, best_ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify(got));
        """)
        self.assertAlmostEqual(out["ask"], 1.1002, places=6)

    # 20/21. account:update / account:snapshot regression (unchanged)
    def test_account_update_still_works(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; }, null);
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(out["balance"], 1000)

    def test_account_snapshot_still_works(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; }, null);
          s._handleMsg({ type: 'account:snapshot', balance: 500 });
          console.log(JSON.stringify({ gotIt: received !== null }));
        """)
        self.assertTrue(out["gotIt"])

    # 22. unsupported message types still ignored (no callback firing)
    def test_unknown_message_type_ignored(self):
        out = self._run_node("""
          let accountCalled = false, quoteCalled = false;
          const s = new MobileTradingSession(null, () => { accountCalled = true; }, () => { quoteCalled = true; });
          s._handleMsg({ type: 'positions', items: [] });
          s._handleMsg({ type: 'history', data: [] });
          console.log(JSON.stringify({ accountCalled, quoteCalled }));
        """)
        self.assertFalse(out["accountCalled"])
        self.assertFalse(out["quoteCalled"])


# ─────────────────────────────────────────────────────────────────────────
# 41/42. Desktop and backend/engine files remain byte-for-byte unchanged
# ─────────────────────────────────────────────────────────────────────────
class DesktopAndBackendZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(
            ["git", "diff", "--quiet", "--", path],
        )
        self.assertEqual(
            result.returncode, 0, f"{path} has a diff against HEAD, expected none"
        )

    def test_desktop_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/desktop.html")

    def test_shell_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/shell.html")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

    # PRE-VPS-POLISH-03C.2.3A — the blanket views.py zero-diff invariant
    # was explicitly superseded by 03C.2.3's own authorization: views.py
    # may now expose the existing allowed-symbol catalog to Mobile.
    # Replaced with the narrow real contract: the new exposure exists,
    # and the account-resolution/ownership/redirect logic it must NOT
    # touch is still present verbatim. Full behavioral routing/security
    # coverage (foreign-account redirect, same account_id across
    # presentations, ownership boundary) already exists and stays green
    # in test_pre_vps_polish_03c1_presentation_routing.py — not
    # re-proven here via brittle source matching.
    def test_views_py_symbol_exposure_added_without_touching_account_logic(self):
        with open("simulator/views.py", encoding="utf-8") as f:
            src = f.read()
        # The new, narrowly-authorized exposure exists.
        self.assertIn("mobile_allowed_symbols_json", src)
        self.assertIn("_allowed_symbols()", src)
        # Account resolution/ownership/redirect logic — unauthorized to
        # change in this block — is untouched, verbatim.
        self.assertIn(
            'account = TradingAccount.objects.filter(pk=account_id, user=request.user).first()',
            src,
        )
        self.assertIn('return redirect("simulator:accounts")', src)

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
