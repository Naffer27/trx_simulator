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


# ─────────────────────────────────────────────────────────────────────────
# A. HISTORY DEDUP — structural/textual (no JS runtime in this repo)
# ─────────────────────────────────────────────────────────────────────────
class HistoryDedupGuardTests(SimpleTestCase):
    """1-6, 9, 10 — the guard itself, keyed on msg.id, array cap preserved,
    surrounding order_close effects untouched."""

    def test_dedupe_guard_present_keyed_on_msg_id(self):
        block = _order_close_block(_template_source())
        self.assertIn("const _dupId=String(msg.id);", block)
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
        block = _order_close_block(_template_source())
        self.assertIn(
            "closedTradesHistory.unshift({id:msg.id,symbol:msg.symbol||this.currentSymbol,"
            "side:msg.side||'?',qty:msg.qty??msg.quantity??null,"
            "entry:msg.avg_entry??msg.avg??msg.entry??null,"
            "close:msg.close_px??msg.close??null,pnl:pnlV,ts:Date.now()});",
            block,
        )

    def test_sync_historial_panel_called_unconditionally(self):
        # A.4/spec: _syncHistorialPanel() must still run even on a
        # duplicate (idempotent re-render), outside the dedupe `if`.
        block = _order_close_block(_template_source())
        i = block.index("if(msg.id!=null){")
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
        block = _order_close_block(_template_source())
        toast_idx = block.index("execToast('close','Posición cerrada',pnlStr);")
        guard_idx = block.index("const _dupId=String(msg.id);")
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

    def test_margin_formula_untouched(self):
        import inspect

        from simulator.consumers import _compute_pretrade_margin_guard

        src = inspect.getsource(_compute_pretrade_margin_guard)
        self.assertIn(
            "required_margin = abs(entry_px * qty * spec_contract_size) / effective_lev", src
        )
