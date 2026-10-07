# simulator/tests/test_pre_vps_polish_03c2_6_mobile_positions.py
"""
PRE-VPS-POLISH-03C.2.6 — Mobile positions / pending orders / closed trades.

Verifies that MobileTradingSession (mobile_session.js) now supports
closePosition()/cancelPendingOrder()/requestClosedTrades(), sending the
EXACT real backend contracts (action:'order:close'/action:'order:pending:
cancel'/action:'get_closed_trades') Desktop already uses — no price, no
symbol/side on close, no commission/fee/close_reason on closed trades
(none exist on that real contract). Positions continue reusing the real
canonical store (getCanonicalPositions/replaceCanonicalPositions,
trading_core.js) introduced by 03C.2.5; pending orders get a plain
forwarded array (no canonical store exists for them even on Desktop).
Backend-authoritative P&L only: pos.pnl displayed directly when finite,
"—" otherwise — never a local fallback computation.

mobile.html gains a minimal [Positions] [Pending] [Closed] tab
foundation with the real fields documented in the 03C.2.6 preflight.

Real-execution tests run the actual MobileTradingSession class
(composed with the real trading_core.js) via Node — skipped gracefully
when `node` is not on PATH.
"""
import json
import re
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

FAKE_WS_HARNESS = """
global.window = global;
global.localStorage = {};
global.document = { getElementById: () => null };
class FakeWS {
  constructor(url){ this.url = url; this.readyState = 1; this.sent = []; }
  send(payload){ this.sent.push(payload); }
  close(){}
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
global.WebSocket = FakeWS;
"""


# ─────────────────────────────────────────────────────────────────────────
# A. mobile_session.js — closePosition()/cancelPendingOrder()/
#    requestClosedTrades() structural contract
# ─────────────────────────────────────────────────────────────────────────
class ClosePositionSourceContractTests(SimpleTestCase):
    def _close_position_body(self):
        src = _session_source()
        start = src.index("closePosition(id, qty) {")
        end = src.index("\n  }\n\n  // PRE-VPS-POLISH-03C.2.6 — the one Mobile pending-order-cancel", start)
        return src[start:end]

    def test_exact_order_close_action(self):
        body = self._close_position_body()
        self.assertIn("action: 'order:close'", body)

    def test_id_stringified(self):
        body = self._close_position_body()
        self.assertIn("id: String(id)", body)

    def test_qty_only_added_when_provided(self):
        body = self._close_position_body()
        self.assertIn("if (qty != null) payload.qty = qty;", body)

    def test_no_symbol_side_price_entry_pnl_in_payload(self):
        body = self._close_position_body()
        for forbidden in ("symbol:", "side:", "price:", "entry:", "pnl:"):
            self.assertNotIn(forbidden, body)

    def test_mutual_exclusion_with_order_sending(self):
        body = self._close_position_body()
        self.assertIn("if (this._orderSending || this._closeSending) return;", body)


class CancelPendingOrderSourceContractTests(SimpleTestCase):
    def _cancel_body(self):
        src = _session_source()
        start = src.index("cancelPendingOrder(id) {")
        end = src.index("\n  }\n\n  // PRE-VPS-POLISH-03C.2.6 — closed trades are NOT auto-pushed", start)
        return src[start:end]

    def test_exact_pending_cancel_action(self):
        body = self._cancel_body()
        self.assertIn("action: 'order:pending:cancel'", body)
        self.assertIn("id: key", body)

    def test_single_slot_guard_present(self):
        body = self._cancel_body()
        self.assertIn("if (this._cancelSendingId != null) return;", body)


class RequestClosedTradesSourceContractTests(SimpleTestCase):
    def test_exact_get_closed_trades_payload(self):
        src = _session_source()
        self.assertIn(
            "this.ws.send(JSON.stringify({ action: 'get_closed_trades' }))",
            src,
        )

    def test_not_sent_automatically_in_connect_or_constructor(self):
        # requestClosedTrades() must only ever be invoked by the
        # orchestrator (mobile.html) — never from connect()/onopen/the
        # constructor itself.
        src = _session_source()
        connect_start = src.index("connect() {")
        connect_end = src.index("\n  disconnect()", connect_start)
        connect_body = src[connect_start:connect_end]
        self.assertNotIn("get_closed_trades", connect_body)


class HandleMsgPositionsBranchesSourceContractTests(SimpleTestCase):
    def _handle_msg_body(self):
        src = _session_source()
        start = src.index("_handleMsg(msg) {")
        end = src.index("\n  }\n}", start)
        return src[start:end]

    def test_pending_orders_forwarded_without_canonical_store(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'pending_orders'")
        snippet = body[idx:idx + 200]
        self.assertIn("this.onPendingOrders(msg.items)", snippet)
        self.assertNotIn("replaceCanonicalPending", snippet)
        self.assertNotIn("replaceCanonicalPositions", snippet)

    def test_order_close_clears_close_sending_and_forwards(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'order_close'")
        snippet = body[idx:idx + 150]
        self.assertIn("this._closeSending = false;", snippet)
        self.assertIn("this.onOrderClose(msg)", snippet)
        self.assertNotIn("replaceCanonicalPositions", snippet)

    def test_order_pending_cancel_clears_guard_and_forwards(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'order_pending_cancel'")
        snippet = body[idx:idx + 150]
        self.assertIn("this._cancelSendingId = null;", snippet)
        self.assertIn("this.onPendingOrderCancelled(msg)", snippet)

    def test_closed_trades_snapshot_forwarded(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'closed_trades_snapshot'")
        snippet = body[idx:idx + 150]
        self.assertIn("this.onClosedTrades(", snippet)

    def test_warn_distinguishes_close_vs_cancel_by_message_prefix(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'warn'")
        snippet = body[idx:idx + 600]
        self.assertIn("order_close_not_found", snippet)
        self.assertIn("order_pending_cancel_", snippet)
        self.assertIn("this.onCloseError(msg)", snippet)
        self.assertIn("this.onPendingCancelError(msg)", snippet)

    def test_error_checks_close_sending_before_order_sending(self):
        body = self._handle_msg_body()
        error_idx = body.index("msg.type === 'error'")
        warn_idx = body.index("msg.type === 'warn'")
        error_block = body[error_idx:warn_idx]
        close_idx = error_block.index("this._closeSending")
        order_idx = error_block.index("this._orderSending")
        self.assertLess(close_idx, order_idx)


# ─────────────────────────────────────────────────────────────────────────
# B. No financial-engine / IB duplication anywhere in Mobile's files
# ─────────────────────────────────────────────────────────────────────────
class NoFinancialEngineDuplicationTests(SimpleTestCase):
    def test_mobile_session_has_no_financial_engine_or_ib_functions(self):
        src = _session_source()
        for forbidden in (
            "commission_for", "BrokerLedger", "ib_commission", "IBCommission",
            "revenue_share", "spread_revenue", "computeRawPnL",
            "computePositionPnLSafe", "getContractSize", "CONTRACT_SIZE",
            "_raw_exec_price", "calculate_required_margin", "margin_guard",
            "LedgerEntry", "routing_decision",
        ):
            self.assertNotIn(forbidden, src)

    def test_mobile_chart_untouched_by_this_block(self):
        src = _chart_source()
        for forbidden in (
            "order:close", "order:pending:cancel", "get_closed_trades",
            "pending_orders", "closed_trades_snapshot", "computePositionPnLSafe",
        ):
            self.assertNotIn(forbidden, src)

    def test_mobile_html_has_no_financial_engine_or_ib_code(self):
        html = _html_source()
        for forbidden in (
            "commission_for", "BrokerLedger", "ib_commission", "computeRawPnL",
            "computePositionPnLSafe", "getContractSize", "CONTRACT_SIZE",
            "calculate_required_margin", "LedgerEntry",
        ):
            self.assertNotIn(forbidden, html)

    def test_no_raw_pnl_formula_in_html(self):
        # The literal (price-entry)*qty-style arithmetic must never exist
        # anywhere positions/closed rows are rendered.
        html = _html_source()
        for forbidden in ("-entry)*", "-pos.avg)*", "- entry) *"):
            self.assertNotIn(forbidden, html)


# ─────────────────────────────────────────────────────────────────────────
# C. No mobile-specific economic action names (certification §19)
# ─────────────────────────────────────────────────────────────────────────
class NoMobileSpecificEconomicRouteTests(SimpleTestCase):
    def test_only_real_backend_action_names_used(self):
        src = _session_source()
        action_values = set(re.findall(r"action:\s*'([a-zA-Z0-9_:]+)'", src))
        for forbidden_prefix in ("mobile:", "mob:", "app:"):
            for a in action_values:
                self.assertFalse(
                    a.startswith(forbidden_prefix),
                    f"found a mobile-specific action name: {a!r}",
                )

    def test_order_new_and_order_close_are_the_real_shared_actions(self):
        src = _session_source()
        self.assertIn("action: 'order:new'", src)
        self.assertIn("action: 'order:close'", src)
        self.assertNotIn("mobile:order:new", src)
        self.assertNotIn("mobile:order:close", src)


# ─────────────────────────────────────────────────────────────────────────
# D. mobile.html — tabs/fields/empty-state wiring
# ─────────────────────────────────────────────────────────────────────────
class MobilePositionsHtmlWiringTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_three_tabs_present(self):
        html = self._html()
        self.assertIn('data-pane="positions"', html)
        self.assertIn('data-pane="pending"', html)
        self.assertIn('data-pane="closed"', html)

    def test_three_panes_present(self):
        html = self._html()
        for el_id in ("mobPositionsPane", "mobPendingPane", "mobClosedPane"):
            self.assertIn(f'id="{el_id}"', html)

    def test_empty_states_present(self):
        html = self._html()
        self.assertIn("No open positions", html)
        self.assertIn("No pending orders", html)
        self.assertIn("No closed trades", html)

    def test_pnl_display_rule_is_finite_check_not_fallback(self):
        html = self._html()
        self.assertIn("pos.pnl!=null&&isFinite(Number(pos.pnl))", html)
        self.assertIn("t.pnl!=null&&isFinite(Number(t.pnl))", html)

    def test_closed_trades_requested_on_tab_activation(self):
        html = self._html()
        self.assertIn("session.requestClosedTrades()", html)

    def test_close_and_cancel_wired_to_session(self):
        html = self._html()
        self.assertIn("session.closePosition(btn.getAttribute('data-id'))", html)
        self.assertIn("session.cancelPendingOrder(btn.getAttribute('data-id'))", html)

    def test_renders_initial_canonical_positions_on_load(self):
        self.assertIn("renderPositions(getCanonicalPositions())", self._html())


# ─────────────────────────────────────────────────────────────────────────
# E. Real execution — closePosition()
# ─────────────────────────────────────────────────────────────────────────
class ClosePositionRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        session_src = _session_source()
        driver = f"""
        {FAKE_WS_HARNESS}
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

    def test_full_close_default_no_qty(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.closePosition(42);
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [{"action": "order:close", "id": "42"}])

    def test_optional_qty_included_when_provided(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.closePosition(42, 0.5);
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [{"action": "order:close", "id": "42", "qty": 0.5}])

    def test_double_close_blocked_while_in_flight(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.closePosition(42);
          s.closePosition(42);
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 1)

    def test_close_blocked_while_order_new_in_flight(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s.ws.sent = [];
          s.closePosition(42);
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 0)

    def test_order_new_blocked_while_close_in_flight(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.closePosition(42);
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 1 });
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 0)

    def test_order_close_response_clears_state_and_callback(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, (m) => { received = m; });
          s.ws = new WebSocket('x');
          s.closePosition(42);
          s._handleMsg({ type: 'order_close', id: 42, partial: false, realized_pnl: 12.5, symbol: 'EUR/USD', side: 'buy' });
          console.log(JSON.stringify({ received, closeSending: s._closeSending }));
        """)
        self.assertEqual(out["received"]["realized_pnl"], 12.5)
        self.assertFalse(out["closeSending"])

    def test_close_error_routed_to_close_callback_not_trading_error(self):
        out = self._run_node("""
          let closeErr = null, tradingErr = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, (m) => { tradingErr = m; }, null, null, null, (m) => { closeErr = m; });
          s.ws = new WebSocket('x');
          s.closePosition(42);
          s._handleMsg({ type: 'error', code: 'price_unavailable', message: 'no disponible' });
          console.log(JSON.stringify({ closeErr, tradingErr, closeSending: s._closeSending }));
        """)
        self.assertIsNotNone(out["closeErr"])
        self.assertIsNone(out["tradingErr"])
        self.assertFalse(out["closeSending"])

    def test_order_new_error_still_routed_to_trading_error(self):
        out = self._run_node("""
          let closeErr = null, tradingErr = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, null, (m) => { tradingErr = m; }, null, null, null, (m) => { closeErr = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'error', code: 'insufficient_margin', message: 'margen' });
          console.log(JSON.stringify({ closeErr, tradingErr }));
        """)
        self.assertIsNone(out["closeErr"])
        self.assertIsNotNone(out["tradingErr"])

    def test_close_warn_not_found_routed_and_clears_state(self):
        out = self._run_node("""
          let closeErr = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, (m) => { closeErr = m; });
          s.ws = new WebSocket('x');
          s.closePosition(42);
          s._handleMsg({ type: 'warn', message: 'order_close_not_found' });
          console.log(JSON.stringify({ closeErr, closeSending: s._closeSending }));
        """)
        self.assertIsNotNone(out["closeErr"])
        self.assertFalse(out["closeSending"])

    def test_canonical_positions_not_fabricated_after_close(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          replaceCanonicalPositions([{ id: 1, symbol: 'EUR/USD', side: 'buy', qty: 0.1, avg: 1.1, pnl: 5 }]);
          s.closePosition(1);
          s._handleMsg({ type: 'order_close', id: 1, partial: false, realized_pnl: 5 });
          console.log(JSON.stringify({ stillThere: getCanonicalPositions().length }));
        """)
        # order_close alone must NOT remove/mutate the canonical store --
        # only a real 'positions' snapshot does (verified separately).
        self.assertEqual(out["stillThere"], 1)

    def test_positions_snapshot_after_close_updates_canonical_store(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          replaceCanonicalPositions([{ id: 1, symbol: 'EUR/USD', side: 'buy', qty: 0.1, avg: 1.1, pnl: 5 }]);
          s.closePosition(1);
          s._handleMsg({ type: 'order_close', id: 1, partial: false, realized_pnl: 5 });
          s._handleMsg({ type: 'positions', items: [] });
          console.log(JSON.stringify({ remaining: getCanonicalPositions().length }));
        """)
        self.assertEqual(out["remaining"], 0)


# ─────────────────────────────────────────────────────────────────────────
# F. Real execution — cancelPendingOrder()
# ─────────────────────────────────────────────────────────────────────────
class CancelPendingOrderRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        session_src = _session_source()
        driver = f"""
        {FAKE_WS_HARNESS}
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

    def test_exact_cancel_payload(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.cancelPendingOrder(7);
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [{"action": "order:pending:cancel", "id": "7"}])

    def test_double_cancel_blocked_while_in_flight(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.cancelPendingOrder(7);
          s.cancelPendingOrder(7);
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 1)

    def test_cancel_response_clears_guard_and_callback(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, null, null, (m) => { received = m; });
          s.ws = new WebSocket('x');
          s.cancelPendingOrder(7);
          s._handleMsg({ type: 'order_pending_cancel', id: 7 });
          console.log(JSON.stringify({ received, cancelSendingId: s._cancelSendingId }));
        """)
        self.assertEqual(out["received"]["id"], 7)
        self.assertIsNone(out["cancelSendingId"])

    def test_cancel_warn_not_found_routed_and_clears_guard(self):
        out = self._run_node("""
          let cancelErr = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, (m) => { cancelErr = m; });
          s.ws = new WebSocket('x');
          s.cancelPendingOrder(7);
          s._handleMsg({ type: 'warn', message: 'order_pending_cancel_not_found' });
          console.log(JSON.stringify({ cancelErr, cancelSendingId: s._cancelSendingId }));
        """)
        self.assertIsNotNone(out["cancelErr"])
        self.assertIsNone(out["cancelSendingId"])

    def test_pending_orders_passthrough_no_canonical_store(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, (items) => { received = items; });
          s.ws = new WebSocket('x');
          const items = [{ id: 9, symbol: 'EUR/USD', side: 'buy', order_type: 'limit', qty: 0.1, trigger_price: 1.05, sl: null, tp: null, status: 'PENDING', created_ts: 1700000000 }];
          s._handleMsg({ type: 'pending_orders', items });
          console.log(JSON.stringify({ received }));
        """)
        self.assertEqual(out["received"][0]["id"], 9)

    def test_no_pending_create_or_edit_actions_exist(self):
        src = _session_source()
        for forbidden in ("order:pending:new", "order:pending:update"):
            self.assertNotIn(forbidden, src)


# ─────────────────────────────────────────────────────────────────────────
# G. Real execution — requestClosedTrades()
# ─────────────────────────────────────────────────────────────────────────
class ClosedTradesRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        session_src = _session_source()
        driver = f"""
        {FAKE_WS_HARNESS}
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

    def test_exact_get_closed_trades_payload(self):
        out = self._run_node("""
          const s = new MobileTradingSession();
          s.ws = new WebSocket('x');
          s.requestClosedTrades();
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [{"action": "get_closed_trades"}])

    def test_closed_trades_snapshot_passthrough(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, null, (trades) => { received = trades; });
          s.ws = new WebSocket('x');
          const trades = [{ id: 5, symbol: 'EUR/USD', side: 'buy', qty: 0.1, entry: 1.1, close: 1.12, pnl: 2.0, ts: 1700000000000 }];
          s._handleMsg({ type: 'closed_trades_snapshot', trades });
          console.log(JSON.stringify({ received }));
        """)
        self.assertEqual(out["received"][0]["id"], 5)

    def test_closed_trades_fields_contract_no_fabrication(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, null, (trades) => { received = trades; });
          s.ws = new WebSocket('x');
          const trades = [{ id: 5, symbol: 'EUR/USD', side: 'buy', qty: 0.1, entry: 1.1, close: 1.12, pnl: 2.0, ts: 1700000000000 }];
          s._handleMsg({ type: 'closed_trades_snapshot', trades });
          console.log(JSON.stringify({ keys: Object.keys(received[0]).sort() }));
        """)
        self.assertEqual(out["keys"], ["close", "entry", "id", "pnl", "qty", "side", "symbol", "ts"])

    def test_missing_trades_array_defaults_to_empty(self):
        out = self._run_node("""
          let received = 'untouched';
          const s = new MobileTradingSession(null, null, null, [], null, null, null, null, null, null, null, null, null, null, null, null, null, (trades) => { received = trades; });
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'closed_trades_snapshot' });
          console.log(JSON.stringify({ received }));
        """)
        self.assertEqual(out["received"], [])


# ─────────────────────────────────────────────────────────────────────────
# H. Regression — account/order:new/risk-warning/symbol/timeframe/chart
# ─────────────────────────────────────────────────────────────────────────
class PositionsBlockRegressionRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script):
        core_src = _core_source()
        session_src = _session_source()
        driver = f"""
        {FAKE_WS_HARNESS}
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

    def test_account_update_still_works(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; });
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(out["balance"], 1000)

    def test_order_new_still_works_alongside_positions_block(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 1 });
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"][0]["action"], "order:new")

    def test_risk_warning_still_works(self):
        out = self._run_node("""
          let warned = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, (m) => { warned = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5 });
          s._handleMsg({ type: 'risk_warning', pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD' });
          console.log(JSON.stringify({ warned }));
        """)
        self.assertIsNotNone(out["warned"])

    def test_symbol_switch_still_works(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol }));
        """)
        self.assertEqual(out["currentSymbol"], "BTCUSD")

    def test_timeframe_and_chart_callbacks_still_work(self):
        out = self._run_node("""
          let candleCalled = false;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, (b) => { candleCalled = true; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('1h');
          s._handleMsg({ type: 'candle_new', symbol: 'EUR/USD', data: { time: 1, open: 1, high: 1, low: 1, close: 1 } });
          console.log(JSON.stringify({ currentTF: s.currentTF, candleCalled }));
        """)
        self.assertEqual(out["currentTF"], "1h")
        self.assertTrue(out["candleCalled"])

    def test_exactly_one_websocket_ever(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('EUR/USD');
          s.closePosition(1);
          s._handleMsg({ type: 'order_close', id: 1 });
          s.cancelPendingOrder(2);
          s._handleMsg({ type: 'order_pending_cancel', id: 2 });
          s.requestClosedTrades();
          clearInterval(s.hb);
          console.log(JSON.stringify({ ok: true }));
        """)
        self.assertTrue(out["ok"])
        self.assertEqual(_session_source().count("new WebSocket("), 1)


# ─────────────────────────────────────────────────────────────────────────
# I. Zero diff — Desktop/backend/financial engine/other Mobile files
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

    def test_mobile_chart_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_chart.js")

    def test_views_py_zero_diff(self):
        self._assert_zero_diff("simulator/views.py")

    def test_consumers_py_zero_diff(self):
        self._assert_zero_diff("simulator/consumers.py")

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
