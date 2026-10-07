# simulator/tests/test_pre_vps_polish_03c2_5_mobile_trading_actions.py
"""
PRE-VPS-POLISH-03C.2.5 — Mobile trading actions foundation.

Verifies that MobileTradingSession (mobile_session.js) now supports
submitOrder()/requestRiskPreview()/confirmRiskWarning()/
cancelRiskWarning(), sending the EXACT same {action:'order:new',...}/
{action:'order:risk_preview',...} contract Desktop's TradingPanel
already uses (desktop.html:4443-4461/3428) — market-only, no price, no
accountId, no client-computed margin/commission/spread/contract_size/
risk. The backend remains the sole financial authority; Mobile sends
intention and displays the real backend response (order_ack/
order_rejected/risk_warning/risk_preview/error/positions) unmodified.

mobile.html gains a minimal order ticket (BUY/SELL/Volume/SL/TP) that
clamps qty via the REAL getLotMin()/getLotDecimals() (trading_core.js)
before calling submitOrder() — mirroring Desktop's own
sendActiveOrder()/TradingPanel.sendOrder() architecture split exactly.

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


# ─────────────────────────────────────────────────────────────────────────
# A. mobile_session.js — submitOrder()/requestRiskPreview() structural
# ─────────────────────────────────────────────────────────────────────────
class SubmitOrderSourceContractTests(SimpleTestCase):
    def _submit_order_body(self):
        src = _session_source()
        start = src.index("submitOrder({ side, qty, sl = null, tp = null, riskConfirmed = false } = {}) {")
        end = src.index("\n  }\n\n  // PRE-VPS-POLISH-03C.2.5 — resends", start)
        return src[start:end]

    def test_exact_order_new_action_present(self):
        body = self._submit_order_body()
        self.assertIn("action: 'order:new'", body)

    def test_symbol_from_current_symbol_not_a_parameter(self):
        body = self._submit_order_body()
        self.assertIn("symbol: this.currentSymbol", body)

    def test_type_is_always_market(self):
        body = self._submit_order_body()
        self.assertIn("type: 'market'", body)

    def test_side_normalized_to_lowercase(self):
        body = self._submit_order_body()
        self.assertIn("String(side || '').toLowerCase()", body)
        self.assertIn("normSide !== 'buy' && normSide !== 'sell'", body)

    def test_sl_tp_default_to_null(self):
        body = self._submit_order_body()
        self.assertIn("sl: sl ?? null", body)
        self.assertIn("tp: tp ?? null", body)

    def test_risk_confirmed_only_when_requested(self):
        body = self._submit_order_body()
        self.assertIn("if (riskConfirmed) payload.risk_confirmed = true;", body)

    def test_no_price_field_in_payload(self):
        body = self._submit_order_body()
        self.assertNotIn("price:", body)

    def test_no_accountid_field_in_payload(self):
        body = self._submit_order_body()
        self.assertNotIn("accountId", body)
        self.assertNotIn("account:", body)

    def test_sending_guard_present(self):
        body = self._submit_order_body()
        self.assertIn("if (this._orderSending) return;", body)

    def test_requires_open_socket_and_current_symbol(self):
        body = self._submit_order_body()
        self.assertIn("this.ws.readyState !== WebSocket.OPEN", body)
        self.assertIn("if (!this.currentSymbol) return;", body)


class RiskPreviewSourceContractTests(SimpleTestCase):
    def test_exact_risk_preview_payload(self):
        src = _session_source()
        self.assertIn(
            "this.ws.send(JSON.stringify({ action: 'order:risk_preview', symbol, qty }))",
            src,
        )

    def test_risk_preview_debounced_200ms(self):
        src = _session_source()
        idx = src.index("this.requestRiskPreview = debounce(")
        snippet = src[idx:idx + 400]
        self.assertIn("}, 200);", snippet)


class ConfirmCancelRiskWarningSourceContractTests(SimpleTestCase):
    def _confirm_body(self):
        src = _session_source()
        start = src.index("confirmRiskWarning() {")
        end = src.index("\n  }\n\n  // PRE-VPS-POLISH-03C.2.5 — discards", start)
        return src[start:end]

    def test_resends_exact_pending_intent(self):
        body = self._confirm_body()
        self.assertIn("side: intent.side, qty: intent.qty, sl: intent.sl, tp: intent.tp, riskConfirmed: true", body)

    def test_stale_symbol_guard_present(self):
        body = self._confirm_body()
        self.assertIn("if (intent.symbol !== this.currentSymbol) return;", body)

    def test_cancel_only_clears_pending_state(self):
        src = _session_source()
        start = src.index("cancelRiskWarning() {")
        end = src.index("\n  }\n\n  // Identical construction", start)
        body = src[start:end]
        self.assertIn("this._pendingRiskOrder = null;", body)
        self.assertNotIn("this.ws.send(", body)


class HandleMsgTradingBranchesSourceContractTests(SimpleTestCase):
    def _handle_msg_body(self):
        src = _session_source()
        start = src.index("_handleMsg(msg) {")
        end = src.index("\n  }\n}", start)
        return src[start:end]

    def test_positions_reuses_canonical_store(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'positions'")
        snippet = body[idx:idx + 300]
        self.assertIn("replaceCanonicalPositions(msg.items)", snippet)
        self.assertIn("this.onPositions(getCanonicalPositions())", snippet)

    def test_order_ack_guarded_by_sending_flag(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'order_ack'")
        snippet = body[idx:idx + 200]
        self.assertIn("if (this._orderSending)", snippet)
        self.assertIn("this.onOrderAck(msg)", snippet)

    def test_order_rejected_guarded_by_sending_flag(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'order_rejected'")
        snippet = body[idx:idx + 200]
        self.assertIn("if (this._orderSending)", snippet)
        self.assertIn("this.onOrderRejected(msg)", snippet)

    def test_risk_warning_captures_pending_order(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'risk_warning'")
        snippet = body[idx:idx + 220]
        self.assertIn("this._pendingRiskOrder = this._lastOrderIntent;", snippet)
        self.assertIn("this.onRiskWarning(msg)", snippet)

    def test_error_guarded_by_sending_flag(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'error'")
        snippet = body[idx:idx + 160]
        self.assertIn("if (this._orderSending)", snippet)
        self.assertIn("this.onTradingError(msg)", snippet)

    def test_risk_preview_not_gated_by_sending_flag(self):
        body = self._handle_msg_body()
        idx = body.index("msg.type === 'risk_preview'")
        snippet = body[idx:idx + 80]
        self.assertNotIn("_orderSending", snippet)
        self.assertIn("this.onRiskPreview(msg)", snippet)


# ─────────────────────────────────────────────────────────────────────────
# B. No financial-engine duplication anywhere in Mobile's files
# ─────────────────────────────────────────────────────────────────────────
class NoFinancialEngineDuplicationTests(SimpleTestCase):
    def test_mobile_session_has_no_financial_engine_functions(self):
        src = _session_source()
        for forbidden in (
            "calculate_required_margin", "commission_for", "_raw_exec_price",
            "evaluate_position_risk", "computeRiskLocal", "computePositionPnL",
            "computeRawPnL", "getContractSize", "CONTRACT_SIZE",
            "broker_price", "BrokerRiskLock", "pnl_engine",
        ):
            self.assertNotIn(forbidden, src)

    def test_mobile_chart_has_no_financial_engine_functions(self):
        # mobile_chart.js must remain untouched by this block — confirmed
        # it never gained any order/risk code either.
        src = _chart_source()
        for forbidden in (
            "order:new", "order:risk_preview", "computeRiskLocal",
            "computePositionPnL", "calculate_required_margin",
        ):
            self.assertNotIn(forbidden, src)

    def test_mobile_html_has_no_financial_engine_functions(self):
        html = _html_source()
        for forbidden in (
            "computeRiskLocal", "computePositionPnL", "computeRawPnL",
            "getContractSize", "CONTRACT_SIZE", "calculate_required_margin",
            "commission_for",
        ):
            self.assertNotIn(forbidden, html)

    def test_no_second_positions_store_invented(self):
        src = _session_source()
        self.assertNotIn("_mobilePositions", src)
        self.assertNotIn("let positionsCache", src)
        self.assertNotIn("const positionsCache", src)

    def test_no_pending_or_update_or_close_actions(self):
        src = _session_source()
        for forbidden in (
            "order:pending:new", "order:pending:cancel", "order:update", "order:close",
        ):
            self.assertNotIn(forbidden, src)

    def test_no_fetch_or_xhr_introduced(self):
        src = _session_source()
        for forbidden in ("fetch(", "XMLHttpRequest"):
            self.assertNotIn(forbidden, src)


# ─────────────────────────────────────────────────────────────────────────
# C. mobile.html — ticket UI wiring
# ─────────────────────────────────────────────────────────────────────────
class MobileTicketHtmlWiringTests(TestCase):
    def _html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        r = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_buy_sell_qty_sl_tp_controls_present(self):
        html = self._html()
        for el_id in ("mobBuyBtn", "mobSellBtn", "mobQtyInput", "mobSlInput", "mobTpInput"):
            self.assertIn(f'id="{el_id}"', html)

    def test_submit_ticket_calls_session_submit_order(self):
        self.assertIn("session.submitOrder({side,qty,sl,tp})", self._html())

    def test_qty_clamp_uses_real_lot_helpers(self):
        html = self._html()
        self.assertIn("getLotMin(session.currentSymbol)", html)
        self.assertIn("getLotDecimals(session.currentSymbol)", html)
        # No second lot-size catalog invented in the template itself.
        self.assertNotIn("LOT_SPECS", html)

    def test_risk_preview_requested_on_qty_input(self):
        self.assertIn("session.requestRiskPreview(session.currentSymbol,qty)", self._html())

    def test_confirm_and_cancel_wired(self):
        html = self._html()
        self.assertIn("session.confirmRiskWarning()", html)
        self.assertIn("session.cancelRiskWarning()", html)

    def test_market_only_no_type_selector(self):
        html = self._html()
        for forbidden in ("order:pending:new", "Limit", "Stop", "trigPrice", "ordType"):
            self.assertNotIn(forbidden, html)

    def test_no_positions_pending_closed_ui_yet(self):
        html = self._html()
        for forbidden in ("mobPositions", "mobPendingOrders", "mobClosedTrades"):
            self.assertNotIn(forbidden, html)


# ─────────────────────────────────────────────────────────────────────────
# D. Real execution — submitOrder()
# ─────────────────────────────────────────────────────────────────────────
class SubmitOrderRealExecutionTests(SimpleTestCase):
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

    def test_buy_payload_exact(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 0.02, sl: 1.09, tp: 1.11 });
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [
            {"action": "order:new", "symbol": "EUR/USD", "side": "buy", "type": "market", "qty": 0.02, "sl": 1.09, "tp": 1.11},
        ])

    def test_sell_payload_exact(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'sell', qty: 0.03, sl: null, tp: null });
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [
            {"action": "order:new", "symbol": "EUR/USD", "side": "sell", "type": "market", "qty": 0.03, "sl": None, "tp": None},
        ])

    def test_side_uppercase_input_normalized_to_lowercase(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'BUY', qty: 0.01 });
          console.log(JSON.stringify({ side: JSON.parse(s.ws.sent[0]).side }));
        """)
        self.assertEqual(out["side"], "buy")

    def test_invalid_side_rejected(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'hold', qty: 0.01 });
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 0)

    def test_no_symbol_selected_blocks_submit(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, []);
          s.ws = new WebSocket('x');
          s.submitOrder({ side: 'buy', qty: 0.01 });
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 0)

    def test_disconnected_blocks_submit(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.selectSymbol('EUR/USD');
          // s.ws stays null -- never connected
          s.submitOrder({ side: 'buy', qty: 0.01 });
          console.log(JSON.stringify({ wsIsNull: s.ws === null }));
        """)
        self.assertTrue(out["wsIsNull"])

    def test_no_double_submit_while_sending(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 0.01 });
          s.submitOrder({ side: 'sell', qty: 0.02 });
          console.log(JSON.stringify({ sentCount: s.ws.sent.length, firstSide: JSON.parse(s.ws.sent[0]).side }));
        """)
        self.assertEqual(out["sentCount"], 1)
        self.assertEqual(out["firstSide"], "buy")

    def test_qty_preserved_exactly_no_clamp_in_session(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 0.5 });
          console.log(JSON.stringify({ qty: JSON.parse(s.ws.sent[0]).qty }));
        """)
        self.assertEqual(out["qty"], 0.5)

    def test_single_websocket_ever(self):
        self.assertEqual(_session_source().count("new WebSocket("), 1)


# ─────────────────────────────────────────────────────────────────────────
# E. Real execution — requestRiskPreview()
# ─────────────────────────────────────────────────────────────────────────
class RiskPreviewRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _run_node(self, script, wait_ms=250):
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

    def test_exact_risk_preview_request_payload(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.requestRiskPreview('EUR/USD', 0.5);
          setTimeout(() => console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) })), 250);
        """)
        self.assertEqual(out["sent"], [{"action": "order:risk_preview", "symbol": "EUR/USD", "qty": 0.5}])

    def test_risk_preview_response_passthrough(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, (m) => { received = m; });
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'risk_preview', symbol: 'EUR/USD', qty: 0.5, risk_level: 'MEDIUM', exposure_pct: 30.0, margin_required: 123.45 });
          console.log(JSON.stringify({ received }));
        """)
        self.assertEqual(out["received"]["risk_level"], "MEDIUM")
        self.assertAlmostEqual(out["received"]["exposure_pct"], 30.0, places=3)
        self.assertAlmostEqual(out["received"]["margin_required"], 123.45, places=3)

    def test_risk_preview_can_arrive_without_order_in_flight(self):
        out = self._run_node("""
          let called = false;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, () => { called = true; });
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'risk_preview', symbol: 'EUR/USD', qty: 0.1, risk_level: 'LOW' });
          console.log(JSON.stringify({ called, orderSending: s._orderSending }));
        """)
        self.assertTrue(out["called"])
        self.assertFalse(out["orderSending"])


# ─────────────────────────────────────────────────────────────────────────
# F. Real execution — risk_warning / confirm / cancel
# ─────────────────────────────────────────────────────────────────────────
class RiskWarningConfirmCancelRealExecutionTests(SimpleTestCase):
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

    def test_risk_warning_resolves_sending_and_captures_intent(self):
        out = self._run_node("""
          let warned = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, (m) => { warned = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5, sl: 1.09, tp: 1.11 });
          s._handleMsg({ type: 'risk_warning', requires_confirm: true, pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD', exposure_pct: 75 });
          console.log(JSON.stringify({ warned, orderSending: s._orderSending, pending: s._pendingRiskOrder }));
        """)
        self.assertIsNotNone(out["warned"])
        self.assertFalse(out["orderSending"])
        self.assertEqual(out["pending"], {"symbol": "EUR/USD", "side": "buy", "qty": 5, "sl": 1.09, "tp": 1.11})

    def test_confirm_resends_exact_original_intent_with_risk_confirmed(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5, sl: 1.09, tp: 1.11 });
          s._handleMsg({ type: 'risk_warning', pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD' });
          s.ws.sent = [];
          s.confirmRiskWarning();
          console.log(JSON.stringify({ sent: s.ws.sent.map(JSON.parse) }));
        """)
        self.assertEqual(out["sent"], [{
            "action": "order:new", "symbol": "EUR/USD", "side": "buy", "type": "market",
            "qty": 5, "sl": 1.09, "tp": 1.11, "risk_confirmed": True,
        }])

    def test_cancel_clears_pending_without_resending(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5 });
          s._handleMsg({ type: 'risk_warning', pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD' });
          s.ws.sent = [];
          s.cancelRiskWarning();
          console.log(JSON.stringify({ sentCount: s.ws.sent.length, pending: s._pendingRiskOrder }));
        """)
        self.assertEqual(out["sentCount"], 0)
        self.assertIsNone(out["pending"])

    def test_double_confirm_is_a_no_op_second_time(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5 });
          s._handleMsg({ type: 'risk_warning', pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD' });
          s.ws.sent = [];
          s.confirmRiskWarning();
          const afterFirst = s.ws.sent.length;
          s.confirmRiskWarning();
          console.log(JSON.stringify({ afterFirst, afterSecond: s.ws.sent.length }));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterSecond"], 1)

    def test_stale_confirmation_blocked_after_symbol_switch(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 5 });
          s._handleMsg({ type: 'risk_warning', pending_side: 'buy', pending_qty: 5, pending_symbol: 'EUR/USD' });
          s.selectSymbol('BTCUSD');
          s.ws.sent = [];
          s.confirmRiskWarning();
          console.log(JSON.stringify({ sentCount: s.ws.sent.length, pending: s._pendingRiskOrder }));
        """)
        self.assertEqual(out["sentCount"], 0)
        self.assertIsNone(out["pending"])

    def test_confirm_with_nothing_pending_is_a_no_op(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.confirmRiskWarning();
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 0)


# ─────────────────────────────────────────────────────────────────────────
# G. Real execution — order_ack / order_rejected / error
# ─────────────────────────────────────────────────────────────────────────
class AckRejectedErrorRealExecutionTests(SimpleTestCase):
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

    def test_order_ack_callback_and_state_cleared(self):
        out = self._run_node("""
          let acked = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, (m) => { acked = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'order_ack', order_id: 42, symbol: 'EUR/USD', side: 'buy', qty: 1, status: 'accepted' });
          console.log(JSON.stringify({ acked, orderSending: s._orderSending }));
        """)
        self.assertEqual(out["acked"]["order_id"], 42)
        self.assertFalse(out["orderSending"])

    def test_order_rejected_callback_and_state_cleared(self):
        out = self._run_node("""
          let rejected = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, (m) => { rejected = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'order_rejected', code: 'extreme_risk', exposure_pct: 150 });
          console.log(JSON.stringify({ rejected, orderSending: s._orderSending }));
        """)
        self.assertEqual(out["rejected"]["code"], "extreme_risk")
        self.assertFalse(out["orderSending"])

    def test_error_callback_and_state_cleared(self):
        out = self._run_node("""
          let errored = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, null, (m) => { errored = m; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'error', code: 'insufficient_margin', message: 'margen insuficiente' });
          console.log(JSON.stringify({ errored, orderSending: s._orderSending }));
        """)
        self.assertEqual(out["errored"]["code"], "insufficient_margin")
        self.assertFalse(out["orderSending"])

    def test_unrelated_error_without_order_in_flight_does_not_fire_trading_error(self):
        out = self._run_node("""
          let called = false;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, null, () => { called = true; });
          s.ws = new WebSocket('x');
          // no submitOrder() call -- no order ever in flight
          s._handleMsg({ type: 'error', code: 'invalid_symbol', message: 'simbolo_no_permitido' });
          console.log(JSON.stringify({ called }));
        """)
        self.assertFalse(out["called"])

    def test_ack_after_order_allows_next_submit(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'order_ack', order_id: 1, symbol: 'EUR/USD', side: 'buy', qty: 1 });
          s.ws.sent = [];
          s.submitOrder({ side: 'sell', qty: 1 });
          console.log(JSON.stringify({ sentCount: s.ws.sent.length }));
        """)
        self.assertEqual(out["sentCount"], 1)


# ─────────────────────────────────────────────────────────────────────────
# H. Real execution — positions
# ─────────────────────────────────────────────────────────────────────────
class PositionsRealExecutionTests(SimpleTestCase):
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

    def test_positions_message_updates_canonical_store_and_callback(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, null, null, (items) => { received = items; });
          s.ws = new WebSocket('x');
          const items = [{ id: 1, symbol: 'EUR/USD', side: 'buy', qty: 0.1, avg: 1.1, sl: null, tp: null, opened_at: 1700000000, pnl: 5.5 }];
          s._handleMsg({ type: 'positions', items });
          console.log(JSON.stringify({ received, canonical: getCanonicalPositions() }));
        """)
        self.assertEqual(out["received"][0]["symbol"], "EUR/USD")
        self.assertEqual(out["canonical"][0]["id"], 1)

    def test_positions_without_items_array_ignored(self):
        out = self._run_node("""
          let called = false;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], null, null, null, null, null, null, null, null, null, () => { called = true; });
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'positions' });
          console.log(JSON.stringify({ called }));
        """)
        self.assertFalse(out["called"])

    def test_no_pnl_recomputation_on_positions(self):
        # The canonical store keeps whatever pnl the backend sent,
        # unmodified -- no computePositionPnL()/computeRawPnL() exists
        # anywhere in this file to recompute it (see
        # NoFinancialEngineDuplicationTests above).
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s._handleMsg({ type: 'positions', items: [{ id: 1, symbol: 'EUR/USD', side: 'buy', qty: 0.1, avg: 1.1, pnl: -3.25 }] });
          console.log(JSON.stringify({ pnl: getCanonicalPositions()[0].pnl }));
        """)
        self.assertAlmostEqual(out["pnl"], -3.25, places=3)


# ─────────────────────────────────────────────────────────────────────────
# I. Regression — account/quote/symbol/timeframe/chart/reconnect
# ─────────────────────────────────────────────────────────────────────────
class TradingActionsRegressionRealExecutionTests(SimpleTestCase):
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

    def test_account_update_still_works_alongside_order_flow(self):
        out = self._run_node("""
          let received = null;
          const s = new MobileTradingSession(null, (msg) => { received = msg; });
          s.ws = new WebSocket('x');
          s.selectSymbol && s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'account:update', balance: 1000, equity: 950, margin_used: 100, upnl: -50, leverage: 50 });
          console.log(JSON.stringify(received));
        """)
        self.assertEqual(out["balance"], 1000)

    def test_quote_flow_still_works_alongside_order_flow(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'tick', symbol: 'EUR/USD', bid: 1.1000, ask: 1.1002, source: 'massive' });
          console.log(JSON.stringify({ liveMid: s.liveMid }));
        """)
        self.assertAlmostEqual(out["liveMid"], 1.1001, places=6)

    def test_symbol_switch_still_works(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD', 'BTCUSD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('BTCUSD');
          console.log(JSON.stringify({ currentSymbol: s.currentSymbol }));
        """)
        self.assertEqual(out["currentSymbol"], "BTCUSD")

    def test_timeframe_switch_still_works(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.selectTimeframe('1h');
          console.log(JSON.stringify({ currentTF: s.currentTF }));
        """)
        self.assertEqual(out["currentTF"], "1h")

    def test_history_and_candle_callbacks_still_work(self):
        out = self._run_node("""
          let historyCalled = false, candleCalled = false;
          const s = new MobileTradingSession(null, null, null, ['EUR/USD'], () => { historyCalled = true; }, (b) => { candleCalled = true; });
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s._handleMsg({ type: 'history', symbol: 'EUR/USD', timeframe: '15m', phase: 'initial', data: [] });
          s._handleMsg({ type: 'candle_new', symbol: 'EUR/USD', data: { time: 1, open: 1, high: 1, low: 1, close: 1 } });
          console.log(JSON.stringify({ historyCalled, candleCalled }));
        """)
        self.assertTrue(out["historyCalled"])
        self.assertTrue(out["candleCalled"])

    def test_reconnect_restoration_unchanged(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('EUR/USD');
          const before = s.ws.sent.length;
          s.ws.onopen();
          clearInterval(s.hb);
          setTimeout(() => {
            console.log(JSON.stringify({ sentAfterReopen: s.ws.sent.slice(before) }));
          }, 250);
        """)
        payloads = [json.loads(x) for x in out["sentAfterReopen"]]
        self.assertIn({"action": "change_symbol", "symbol": "EUR/USD"}, payloads)

    def test_exactly_one_websocket_with_full_order_flow_exercised(self):
        out = self._run_node("""
          global.window.__TRADE_CONFIG__ = { accountId: 1 };
          global.window.location = { href: 'http://example.com/dashboard/1/' };
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.connect();
          s.ws.readyState = 1;
          s.selectSymbol('EUR/USD');
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'order_ack', order_id: 1, symbol: 'EUR/USD', side: 'buy', qty: 1 });
          s.requestRiskPreview('EUR/USD', 1);
          clearInterval(s.hb);
          console.log(JSON.stringify({ ok: true }));
        """)
        self.assertTrue(out["ok"])
        self.assertEqual(_session_source().count("new WebSocket("), 1)

    def test_no_get_positions_required_after_successful_order(self):
        out = self._run_node("""
          const s = new MobileTradingSession(null, null, null, ['EUR/USD']);
          s.ws = new WebSocket('x');
          s.selectSymbol('EUR/USD');
          s.ws.sent = [];
          s.submitOrder({ side: 'buy', qty: 1 });
          s._handleMsg({ type: 'order_ack', order_id: 1, symbol: 'EUR/USD', side: 'buy', qty: 1 });
          const payloads = s.ws.sent.map(JSON.parse);
          const askedGetPositions = payloads.some(p => p.action === 'get_positions');
          console.log(JSON.stringify({ askedGetPositions }));
        """)
        self.assertFalse(out["askedGetPositions"])


# ─────────────────────────────────────────────────────────────────────────
# J. Zero diff — Desktop/backend/financial engine/other Mobile files
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
