# simulator/tests/test_fix_sltp_residual01_order_ticket_reset.py
"""
FIX-SLTP-RESIDUAL-01 — order ticket #sl/#tp reset.

Design lock: FIX-SLTP-RESIDUAL-01. The order ticket's #sl/#tp inputs
(dashboard.html) are a single global form shared by every panel/symbol
AND by both Market and Pending orders (sendActiveOrder() reads them once
before branching). Nothing cleared them after a successful order — only
on symbol change or panel switch — so a second order on the SAME symbol
in the SAME panel could silently resend a residual SL/TP the user no
longer intended.

Fix: a single helper, clearOrderProtectionInputs(), called on every
DEFINITIVE ACCEPT (order_ack for Market, order_pending_new for Pending)
in addition to the two pre-existing call sites (changeSymbol, setActive)
— never on a reject or the intermediate risk_warning pause, so the user
can correct one field and resend without retyping SL/TP.

No JS test runner exists in this repo (same situation as FIX-01/FIX-05C)
— these are textual/structural assertions on the shipped template
source, not simulated runtime execution. Not a substitute for a real JS
test, not presented as one.
"""
from django.template.loader import get_template
from django.test import SimpleTestCase


def _template_source():
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _slice(src, start_marker, end_marker, start_from=0):
    i = src.index(start_marker, start_from)
    j = src.index(end_marker, i)
    return src[i:j]


def _helper_block(src):
    return _slice(src, "function clearOrderProtectionInputs(){", "/* ── Panel management ── */")


def _change_symbol_block(src):
    return _slice(
        src,
        "// PANEL-04 — SL/TP are price levels for the *previous* symbol's",
        "// FIX-01 — symbol change alters notional/margin: refresh the risk preview",
    )


def _set_active_block(src):
    return _slice(
        src,
        "// PANEL-04 — the order ticket is a single global form; switching which",
        "const mhdrSymSel=document.getElementById('mhdrSymSel');",
    )


def _order_ack_block(src):
    return _slice(src, "if(msg.type==='order_ack'){", "if(msg.type==='order_close'){")


def _order_pending_new_line(src):
    i = src.index("if(msg.type==='order_pending_new'){")
    j = src.index("\n", i)
    return src[i:j]


def _error_block(src):
    return _slice(src, "if(msg.type==='error'){", "if(msg.type==='account:update'")


def _risk_warning_block(src):
    return _slice(
        src, "if(msg.type==='risk_warning'){",
        "if(msg.type==='order_rejected'&&msg.code==='extreme_risk'){",
    )


def _order_rejected_extreme_risk_block(src):
    return _slice(
        src, "if(msg.type==='order_rejected'&&msg.code==='extreme_risk'){",
        "if(msg.type==='risk_preview'){",
    )


def _error_pending_block(src):
    return _slice(src, "if(msg.type==='error_pending'){", "if(msg.type==='order_pending_new'){")


def _confirm_risk_order_block(src):
    return _slice(src, "function confirmRiskOrder(){", "function cancelRiskConfirm(){")


def _reconnect_onclose_block(src):
    return _slice(src, "this._cancelPendingVisualRender();", "disconnect(){")


# ─────────────────────────────────────────────────────────────────────────
# 1-3. Helper exists and clears exactly #sl/#tp
# ─────────────────────────────────────────────────────────────────────────
class HelperTests(SimpleTestCase):
    def test_helper_exists(self):
        src = _template_source()
        self.assertIn("function clearOrderProtectionInputs(){", src)

    def test_helper_clears_sl_and_tp(self):
        block = _helper_block(_template_source())
        self.assertIn("document.getElementById('sl')", block)
        self.assertIn("document.getElementById('tp')", block)
        self.assertIn("slEl.value=''", block)
        self.assertIn("tpEl.value=''", block)

    def test_helper_never_touches_sheet_sl_tp(self):
        # FIX-SLTP-RESIDUAL-01 §5 — modify-position editor isolation.
        block = _helper_block(_template_source())
        self.assertNotIn("sheetSL", block)
        self.assertNotIn("sheetTP", block)


# ─────────────────────────────────────────────────────────────────────────
# 4-7. The 4 call sites
# ─────────────────────────────────────────────────────────────────────────
class CallSiteTests(SimpleTestCase):
    def test_change_symbol_uses_helper(self):
        block = _change_symbol_block(_template_source())
        self.assertIn("clearOrderProtectionInputs();", block)
        # No leftover duplicated inline reset alongside the helper call.
        self.assertNotIn("slEl.value=''", block)

    def test_set_active_uses_helper(self):
        block = _set_active_block(_template_source())
        self.assertIn("clearOrderProtectionInputs();", block)
        self.assertNotIn("slEl.value=''", block)

    def test_order_ack_uses_helper(self):
        block = _order_ack_block(_template_source())
        self.assertIn("clearOrderProtectionInputs();", block)

    def test_order_pending_new_uses_helper(self):
        line = _order_pending_new_line(_template_source())
        self.assertIn("clearOrderProtectionInputs();", line)


# ─────────────────────────────────────────────────────────────────────────
# 8-11. Reject / intermediate paths NEVER clear
# ─────────────────────────────────────────────────────────────────────────
class RejectSemanticsTests(SimpleTestCase):
    def test_error_does_not_clear(self):
        block = _error_block(_template_source())
        self.assertNotIn("clearOrderProtectionInputs", block)

    def test_order_rejected_extreme_risk_does_not_clear(self):
        block = _order_rejected_extreme_risk_block(_template_source())
        self.assertNotIn("clearOrderProtectionInputs", block)

    def test_error_pending_does_not_clear(self):
        block = _error_pending_block(_template_source())
        self.assertNotIn("clearOrderProtectionInputs", block)

    def test_risk_warning_does_not_clear(self):
        block = _risk_warning_block(_template_source())
        self.assertNotIn("clearOrderProtectionInputs", block)


# ─────────────────────────────────────────────────────────────────────────
# 12. Risk-confirm keeps reading the DOM fresh, and does not clear either
# ─────────────────────────────────────────────────────────────────────────
class RiskConfirmTests(SimpleTestCase):
    def test_confirm_risk_order_reads_dom_fresh(self):
        block = _confirm_risk_order_block(_template_source())
        self.assertIn("document.getElementById('sl')?.value", block)
        self.assertIn("document.getElementById('tp')?.value", block)

    def test_confirm_risk_order_does_not_clear(self):
        # The clear happens once, on the eventual order_ack — never here.
        block = _confirm_risk_order_block(_template_source())
        self.assertNotIn("clearOrderProtectionInputs", block)


# ─────────────────────────────────────────────────────────────────────────
# 13 (helper isolation) already covered above. 14. Reconnect never repopulates.
# ─────────────────────────────────────────────────────────────────────────
class ReconnectTests(SimpleTestCase):
    def test_reconnect_onclose_never_writes_sl_tp(self):
        block = _reconnect_onclose_block(_template_source())
        self.assertNotIn("getElementById('sl')", block)
        self.assertNotIn("getElementById('tp')", block)


# ─────────────────────────────────────────────────────────────────────────
# 15. BUY/SELL share the same safe flow (single generic function)
# ─────────────────────────────────────────────────────────────────────────
class BuySellSharedFlowTests(SimpleTestCase):
    def test_buy_and_sell_buttons_call_the_same_generic_function(self):
        src = _template_source()
        self.assertIn("function sendActiveOrder(side){", src)
        self.assertIn("document.getElementById('btnBuy').addEventListener('click',()=>sendActiveOrder('buy'));", src)
        self.assertIn("document.getElementById('btnSell').addEventListener('click',()=>sendActiveOrder('sell'));", src)
