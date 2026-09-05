# simulator/tests/test_golden_fix01_history_dedup_trade_precision.py
"""
GOLDEN-SCENARIOS-FIX-01 — History dedup + Trade.lot_size precision.

Design lock: GOLDEN-SCENARIOS-FIX-01 (this block). Two independent fixes:

  A. History dedup — dashboard.html's order_close handler unshifts into the
     page-global closedTradesHistory array once per WS connection; with N
     panels open (each its own connection, each receiving the SAME
     position_changed()-driven order_close broadcast), the same real Trade
     produced N visual rows. DB/ledger were always correct (Golden
     Scenarios History Duplication Audit) — this was purely a rendering
     bug. Fixed with a dedupe guard keyed on msg.id (position_id — stable,
     never reused, a Position closes exactly once in full).

     ORDER-MANAGEMENT-V2B UPDATE — "a Position closes exactly once in
     full" stopped being true once partial close shipped: the SAME
     position_id now repeats across multiple partial closes of a still-
     open Position. The dedupe key was re-derived from msg.trade_id
     (unique per realization event, full or partial, never reused) —
     see that block's design lock §7/§8. The tests below were updated to
     match the new key; every OTHER invariant they protect (single-field
     dedupe, toast/cleanup ordering, unconditional _syncHistorialPanel,
     the 100-item cap) is unchanged and still enforced.

     No JS test runner exists in this repo (same situation as FIX-05C) —
     these are textual/structural assertions on the shipped template
     source, not simulated runtime execution. They are not a substitute
     for a real JS test and are not presented as one.

  B. Trade.lot_size — DecimalField(decimal_places=2) truncated BTCUSD's
     minimum lot (0.001, lot_step) to 0.00 when archiving a closed trade.
     Position.qty (decimal_places=6) already had the correct precision
     throughout the position's life — only the post-close audit record
     lost it. Fixed by widening Trade.lot_size to decimal_places=6,
     max_digits=10 (unchanged — still comfortably covers the largest
     max_lot in symbol_specs.py, 100.0). Purely additive: every existing
     2-decimal value remains exactly representable, nothing is recomputed
     or retroactively reconstructed.

  FIX-01B (follow-up) — A/B above closed the DB/serializer side, but a
  third, purely cosmetic layer still truncated qty for BTCUSD: the
  bottom-panel history renderer (_renderBtmHistory()) hardcoded
  Number(t.qty).toFixed(2) instead of reusing getLotDecimals(t.symbol),
  the same helper _syncHistorialPanel() already used correctly. Fixed by
  making that one call-site symbol-aware too — see
  BtmHistoryQtySymbolAwareRenderTests below.
"""
from decimal import Decimal

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase

from simulator.models import Position, Trade, TradingAccount


def _template_source():
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _order_close_block(src):
    i = src.index("if(msg.type==='order_close'){")
    j = src.index("/* ── Indicator rendering ── */", i)
    return src[i:j]


def _btm_history_block(src):
    i = src.index("function _renderBtmHistory(){")
    j = src.index("/* ── Bottom panel resize", i)
    return src[i:j]


def _sync_historial_panel_block(src):
    i = src.index("function _syncHistorialPanel(){")
    j = src.index("function _renderHstSummary(", i)
    return src[i:j]


# ─────────────────────────────────────────────────────────────────────────
# A. HISTORY DEDUP — structural/textual (no JS runtime in this repo)
# ─────────────────────────────────────────────────────────────────────────
class HistoryDedupGuardTests(SimpleTestCase):
    """1-6, 9, 10 — the guard itself, keyed on msg.id, array cap preserved,
    surrounding order_close effects untouched."""

    def test_dedupe_guard_present_keyed_on_msg_id(self):
        # ORDER-MANAGEMENT-V2B — re-keyed from msg.id (position_id) to
        # msg.trade_id (see this file's module docstring update).
        block = _order_close_block(_template_source())
        self.assertIn("const _dupId=String(msg.trade_id);", block)
        self.assertIn(
            "if(!closedTradesHistory.some(t=>String(t.id)===_dupId)){", block
        )

    def test_dedupe_does_not_key_on_symbol_side_pnl_timestamp(self):
        # The guard's condition must reference only id — not compose a key
        # from symbol/side/pnl/ts (design lock A.1: "ÚNICAMENTE por id").
        block = _order_close_block(_template_source())
        i = block.index("closedTradesHistory.some(")
        j = block.index(")", i)
        guard_expr = block[i:j]
        self.assertIn("t.id", guard_expr)
        self.assertNotIn("t.symbol", guard_expr)
        self.assertNotIn("t.side", guard_expr)
        self.assertNotIn("t.pnl", guard_expr)
        self.assertNotIn("t.ts", guard_expr)

    def test_unshift_still_present_inside_guard(self):
        block = _order_close_block(_template_source())
        self.assertIn("closedTradesHistory.unshift(", block)

    def test_array_cap_at_100_preserved(self):
        block = _order_close_block(_template_source())
        self.assertIn("if(closedTradesHistory.length>100)closedTradesHistory.pop();", block)

    def test_object_fields_unchanged(self):
        # Design lock: "Preservar exactamente los campos actuales del
        # objeto. NO reescribir estructura innecesariamente."
        # ORDER-MANAGEMENT-V2B — id now sources from msg.trade_id (the
        # new dedupe key) with position_id added alongside it so nothing
        # that needed the Position's own id lost access to it; every
        # pre-existing field (symbol/side/qty/entry/close/pnl/ts) is
        # untouched, exactly as this test's own intent requires.
        block = _order_close_block(_template_source())
        self.assertIn(
            "closedTradesHistory.unshift({id:msg.trade_id,position_id:msg.id,"
            "symbol:msg.symbol||this.currentSymbol,"
            "side:msg.side||'?',qty:msg.qty??msg.quantity??null,"
            "entry:msg.avg_entry??msg.avg??msg.entry??null,"
            "close:msg.close_px??msg.close??null,pnl:pnlV,ts:Date.now()});",
            block,
        )

    def test_sync_historial_panel_called_unconditionally(self):
        # A.4/spec: _syncHistorialPanel() must still run even on a
        # duplicate (idempotent re-render), outside the dedupe `if`.
        # ORDER-MANAGEMENT-V2B — outer guard re-keyed to msg.trade_id.
        block = _order_close_block(_template_source())
        i = block.index("if(msg.trade_id!=null){")
        # last occurrence of _syncHistorialPanel() within the msg.id block,
        # must be OUTSIDE (after) the inner dedupe-guard's closing brace.
        inner_if = block.index(
            "if(!closedTradesHistory.some(t=>String(t.id)===_dupId)){", i
        )
        inner_close = block.index("}\n        _syncHistorialPanel();", inner_if)
        self.assertIn("_syncHistorialPanel();", block[inner_close:inner_close + 60])


class HistoryDedupSurroundingEffectsTests(SimpleTestCase):
    """4, 10 — toast / line cleanup / positions refresh untouched by the fix."""

    def test_toast_still_fires_outside_dedupe_guard(self):
        # ORDER-MANAGEMENT-V2B — the toast text itself now branches on
        # msg.partial (full vs partial close wording); this test's own
        # concern (toast fires before/outside the dedupe guard) is
        # otherwise unchanged, guard re-keyed to msg.trade_id.
        block = _order_close_block(_template_source())
        toast_idx = block.index("execToast('close',msg.partial?'Cierre parcial':'Posición cerrada',pnlStr);")
        guard_idx = block.index("const _dupId=String(msg.trade_id);")
        self.assertLess(toast_idx, guard_idx)  # toast fires before/outside the guard

    def test_line_cleanup_and_positions_refresh_untouched(self):
        block = _order_close_block(_template_source())
        self.assertIn("this._removeLines(cid);", block)
        self.assertIn("for(const p of allPanels){if(p===this)continue;p._removeLines(cid);", block)


class HistoryDedupInvariantDocumentationTests(SimpleTestCase):
    """1/2/3 (1/2/4-panel invariant) and 5 (initial-history protection) —
    documented as structural facts about the guard's own logic, since
    simulating N independent WebSocket connections is outside what a
    Django test can execute for a template-only frontend."""

    def test_guard_scans_whole_array_not_just_ws_appended_entries(self):
        # A.2: the guard must protect ids already present from the initial
        # closed_trades_json seed too — achieved by scanning the FULL
        # closedTradesHistory array (not a separate "seen via WS" set),
        # so an id seeded at page load is already covered.
        block = _order_close_block(_template_source())
        self.assertIn("closedTradesHistory.some(t=>String(t.id)===_dupId)", block)
        self.assertNotIn("_wsSeenIds", block)  # no separate WS-only tracking set introduced


# ─────────────────────────────────────────────────────────────────────────
# A.1 (FIX-01B) — HISTORY QTY SYMBOL-AWARE RENDER
#
# _renderBtmHistory() (the bottom-panel "Journal" history view — distinct
# from _syncHistorialPanel()'s side-tab list, identifiable by its PnL
# string carrying a literal "$" prefix, which the side-tab never does)
# hardcoded Number(t.qty).toFixed(2) for the qty span. For BTCUSD
# (lot_step=0.001, LOT_SPECS.dec=3) this rendered "0.001" as "0.00" even
# though the DB (Trade.lot_size, FIX-01) and the closed_trades_json
# serializer (views.py, unchanged) were already correct — the value was
# lost only in this last, pure-presentation step. Fixed by reusing
# getLotDecimals(t.symbol||'') — the exact same helper
# _syncHistorialPanel() already used correctly one function over — no new
# formatter, no change to LOT_SPECS/getLotDecimals themselves.
# ─────────────────────────────────────────────────────────────────────────
class BtmHistoryQtySymbolAwareRenderTests(SimpleTestCase):
    """No JS runtime in this repo (same situation as the order_close dedupe
    tests above) — textual/structural assertions on the shipped template
    source, not simulated runtime execution."""

    def test_btm_history_qty_uses_get_lot_decimals(self):
        block = _btm_history_block(_template_source())
        self.assertIn(
            "Number(t.qty).toFixed(getLotDecimals(t.symbol||''))", block
        )

    def test_btm_history_qty_no_longer_hardcoded_2dp(self):
        block = _btm_history_block(_template_source())
        self.assertNotIn("Number(t.qty).toFixed(2)", block)

    def test_btc_qty_contract_renders_with_three_decimals(self):
        # Contract check against the real LOT_SPECS table, not a hardcoded
        # assumption — BTCUSD's own dec:3 entry is what makes 0.001 render
        # correctly, so assert that entry exists rather than just trusting
        # the call-site wiring in isolation.
        src = _template_source()
        self.assertIn('"BTCUSD":  {step:0.001, min:0.001, dec:3}', src)

    def test_eth_qty_contract_keeps_two_decimals(self):
        src = _template_source()
        self.assertIn('"ETHUSD":  {step:0.01,  min:0.01,  dec:2}', src)

    def test_pnl_formatting_in_btm_history_unchanged(self):
        # Must still be 2dp, with the "$" prefix that identifies this
        # panel — untouched by the qty fix.
        block = _btm_history_block(_template_source())
        self.assertIn("Math.abs(pnlV).toFixed(2)", block)

    def test_sync_historial_panel_untouched(self):
        block = _sync_historial_panel_block(_template_source())
        self.assertIn(
            "Number(t.qty).toFixed(getLotDecimals(t.symbol||''))", block
        )


# ─────────────────────────────────────────────────────────────────────────
# B. TRADE.LOT_SIZE PRECISION
# ─────────────────────────────────────────────────────────────────────────
class TradeLotSizePrecisionTests(TestCase):
    def setUp(self):
        self.account = TradingAccount.objects.create(balance=Decimal("1000.00"))

    def _make_trade(self, lot_size: Decimal) -> Trade:
        return Trade.objects.create(
            account=self.account,
            symbol="BTCUSD",
            trade_type=Trade.BUY,
            lot_size=lot_size,
            entry_price=Decimal("80000.000000"),
            exit_price=Decimal("80100.000000"),
            profit_loss=Decimal("0.10"),
        )

    def test_btc_min_lot_0_001_persists_exactly(self):
        t = self._make_trade(Decimal("0.001"))
        t.refresh_from_db()
        self.assertEqual(t.lot_size, Decimal("0.001000"))

    def test_btc_lot_0_002_persists_exactly(self):
        t = self._make_trade(Decimal("0.002"))
        t.refresh_from_db()
        self.assertEqual(t.lot_size, Decimal("0.002000"))

    def test_legacy_2dp_value_0_01_still_valid(self):
        t = self._make_trade(Decimal("0.01"))
        t.refresh_from_db()
        self.assertEqual(t.lot_size, Decimal("0.010000"))

    def test_large_lot_100_within_range(self):
        t = self._make_trade(Decimal("100.0"))
        t.refresh_from_db()
        self.assertEqual(t.lot_size, Decimal("100.000000"))

    def test_field_metadata_decimal_places_and_max_digits(self):
        field = Trade._meta.get_field("lot_size")
        self.assertEqual(field.decimal_places, 6)
        self.assertEqual(field.max_digits, 10)

    def test_position_qty_field_unchanged(self):
        field = Position._meta.get_field("qty")
        self.assertEqual(field.decimal_places, 6)
        self.assertEqual(field.max_digits, 18)

    def test_existing_pre_migration_zero_value_not_retroactively_reconstructed(self):
        # B.3 — a pre-existing 0.00 (the truncated-to-zero case this fix
        # closes going forward) must NOT be magically turned into 0.001.
        # Simulates the exact legacy shape: a trade archived under the old
        # 2dp field, now read back under the new 6dp field.
        t = self._make_trade(Decimal("0.00"))
        t.refresh_from_db()
        self.assertEqual(t.lot_size, Decimal("0.000000"))


class NoFinancialFormulaChangedTests(SimpleTestCase):
    """17 — this fix touches archival precision only, never a financial
    calculation. Textual regression: the real formulas remain exactly as
    audited in the Golden Scenarios Phase 1 report."""

    def test_pnl_engine_formula_untouched(self):
        import inspect

        from simulator import pnl_engine

        src = inspect.getsource(pnl_engine.calculate_quote_pnl)
        self.assertIn('diff = (close - entry) if is_buy else (entry - close)', src)
        self.assertIn("return diff * qty * cs", src)

    def test_margin_formula_delegates_to_centralized_conversion(self):
        """FIX-USDJPY-MARGIN-01-B — this GOLDEN-FIX01 lock originally
        pinned the OLD inline formula (`entry_px * qty *
        spec_contract_size / effective_lev`), which was base/quote-blind
        and overstated USD/JPY margin ~160x (GOLDEN-USDJPY-MARGIN-01).
        That formula was deliberately and correctly replaced. The lock's
        intent survives unchanged — margin math must not silently
        re-diverge into an inline, uncentralized formula again — now
        verified as "delegates to the shared pnl_engine helper" rather
        than pinned to one exact old expression."""
        import inspect

        from simulator.consumers import _compute_pretrade_margin_guard

        src = inspect.getsource(_compute_pretrade_margin_guard)
        self.assertIn("pnl_engine.calculate_required_margin", src)
        self.assertNotIn(
            "required_margin = abs(entry_px * qty * spec_contract_size) / effective_lev", src
        )
