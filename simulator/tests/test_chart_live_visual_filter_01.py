# simulator/tests/test_chart_live_visual_filter_01.py
"""
CHART-LIVE-VISUAL-FILTER-01.

CHART-LIVE-PRICE-OSCILLATION-01's audit found two related, but
DISTINCT, visual issues in dashboard.html (backend market data/pricing/
execution were all confirmed correct and untouched):

  1. The live candle's close was repainted on every real tick with NO
     magnitude/max-age filter (unlike the quote/priceLine/badge path,
     which already had one) — a tiny, real sub-pip move from the raw
     feed was enough to visibly "jump" the current bar many times per
     second. Fixed by reusing the EXACT SAME
     _visualMagnitudeThreshold()/_maxVisualAgeMs() functions the quote
     branch already used, against a NEW, candle-own anchor
     (_lastPaintedCandleClose/_lastPaintedCandleAt — never
     _lastPaintedMid/_lastPaintedAt, which would reintroduce coupling
     to the unrelated quote value). Full behavioral coverage of this
     part lives in test_live_chart_magnitude_filter_01.py (updated by
     this same block) — not duplicated here.

  2. Two numbers sat very close together on the right axis: the
     candlestick series' own NATIVE last-value label (tied to the raw
     candle close) and a second, manually-created price line
     (this.priceLine, tied to the quote's marked-up mid) — legitimately
     different numbers by design (MASSIVE-CRYPTO-TRADE-CANDLES-01), but
     a confusing double label. Fixed by keeping the native label as the
     ONE visible axis label and turning this.priceLine fully invisible
     (axisLabelVisible:false, lineVisible:false) — its lazy-creation/
     re-pricing/removal-on-symbol-switch bookkeeping is UNCHANGED, so
     pxTag/_drawGlow (the bid/ask ticket's own live price display,
     wired independently) keep working exactly as before.

Backend market data/pricing/execution, candle aggregation (bucket
logic — see test_o6c1i_candle_timeframe_fix.py, untouched by this
block), and historical candles are all untouched — this file only
covers what THIS block changed: the single-visible-label contract, plus
scope guards proving SL/TP/entry/pending lines, the BUY/SELL bid/ask
ticket, multipanel, and crypto symbols are all unaffected.

Same source-inspection convention as every other dashboard.html test in
this project (no JS test runner is configured) — structural claims
against the literal template source, never a hand-copied snippet.
"""
import re

from django.template.loader import get_template
from django.test import SimpleTestCase


def _template_source() -> str:
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _method_body(src: str, method_signature: str, max_len=None) -> str:
    i = src.index(method_signature)
    start = i + len(method_signature)
    m = re.search(r"\n  [A-Za-z_][A-Za-z0-9_]*\s*\(", src[start:])
    end = start + m.start() if m else (start + (max_len or 3500))
    return src[i:end]


# ─────────────────────────────────────────────────────────────────────
# 1/2 — single visible price label: the native series label stays,
# the manually-created priceLine is fully invisible (not removed —
# its object/lazy-creation/badge/glow wiring is unchanged, see 4/8).
# ─────────────────────────────────────────────────────────────────────
class SinglePriceLabelTests(SimpleTestCase):

    def test_price_line_axis_label_and_line_are_hidden_everywhere_created(self):
        src = _template_source()
        # All THREE createPriceLine() call sites for the quote's own
        # price line (title:this.currentSymbol...) — never the SL/TP/
        # entry/pending/RSI ones (different titles, asserted untouched
        # in NoTouchLinesTests below).
        occurrences = [
            m.start() for m in re.finditer(
                r"this\.priceLine=this\.candleSeries\.createPriceLine\(\{[^}]*\}\);",
                src,
            )
        ]
        self.assertEqual(len(occurrences), 3, "expected exactly 3 this.priceLine creation sites")
        for start in occurrences:
            end = src.index(");", start) + 2
            call = src[start:end]
            self.assertIn("axisLabelVisible:false", call)
            self.assertIn("lineVisible:false", call)
            self.assertIn("title:this.currentSymbol.replace('/','')", call)

    def test_native_candle_series_last_value_label_stays_visible(self):
        src = _template_source()
        marker = "this.candleSeries=this.chart.addCandlestickSeries({"
        i = src.index(marker)
        j = src.index("});", i) + 3  # bounded to THIS call only, never the sibling volumeSeries options
        call = src[i:j]
        # lastValueVisible is not set to false anywhere in the
        # candlestick series' own options — Lightweight Charts defaults
        # it to true, and this asserts that default is never overridden.
        self.assertNotIn("lastValueVisible:false", call)
        self.assertNotIn("lastValueVisible: false", call)

    def test_price_line_object_lifecycle_unchanged(self):
        # Lazy creation, re-pricing via applyOptions, and removal on
        # symbol switch are all still there — only the two visual
        # properties changed, never the bookkeeping.
        src = _template_source()
        self.assertIn(
            "if(this.priceLine){this.candleSeries?.removePriceLine(this.priceLine);this.priceLine=null;}",
            src,
        )
        body = _method_body(src, "_updateLiveQuoteDisplay(){")
        self.assertIn("if(!this.priceLine){", body)
        self.assertIn("this.priceLine.applyOptions({price:px", body)


# ─────────────────────────────────────────────────────────────────────
# 4 — live candle still updates (paint is gated, never disabled).
# Full magnitude-filter behavior lives in
# test_live_chart_magnitude_filter_01.py — this is a narrow smoke check
# that candleSeries.update() is still reachable from both the isNew and
# same-bucket paths.
# ─────────────────────────────────────────────────────────────────────
class LiveCandleStillUpdatesTests(SimpleTestCase):
    def test_candle_series_update_reachable_from_both_paths(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_candle_start = body.index("if(this._candleRenderPending){")
        i_quote_start = body.index("if(this._quoteRenderPending){")
        candle_branch = body[i_candle_start:i_quote_start]
        self.assertIn("this.candleSeries.update(b);", candle_branch)          # isNew path
        self.assertIn("this._retargetCandleAnimation(b);", candle_branch)     # gated same-bucket path
        # _retargetCandleAnimation ultimately calls candleSeries.update()
        # every animation frame (_stepVisualAnim) — untouched by this block.
        self.assertIn(
            "this.candleSeries.update({time:b.time,open:b.open,high:b.high,low:b.low,close});",
            src,
        )


# ─────────────────────────────────────────────────────────────────────
# 8/9 — BUY/SELL bid/ask ticket and SL/TP/entry/pending lines untouched.
# ─────────────────────────────────────────────────────────────────────
class NoTouchLinesTests(SimpleTestCase):
    def test_bid_ask_ticket_paths_unaffected(self):
        src = _template_source()
        # _updateBidAsk()/_paintBadgeAndGlow() — the bid/ask ticket and
        # badge/glow display — never reference priceLine's visibility,
        # never mention axisLabelVisible/lineVisible.
        for sig in ("_updateBidAsk(){", "_paintBadgeAndGlow(px){"):
            body = _method_body(src, sig, max_len=700)
            self.assertNotIn("axisLabelVisible", body)
            self.assertNotIn("lineVisible", body)

    def test_sl_tp_entry_pending_lines_still_axis_label_visible_true(self):
        src = _template_source()
        for title, count in (("'SL'", 4), ("'TP'", 4)):
            occurrences = src.count(f"axisLabelVisible:true,title:{title}}}")
            self.assertEqual(
                occurrences, count,
                f"expected {count} SL/TP price-line creations with axisLabelVisible:true, title:{title}",
            )
        # Entry line (BUY/SELL open price) — dynamic title variable, own
        # distinct creation call, still axisLabelVisible:true.
        self.assertIn(
            "rec.plEntry=this.candleSeries.createPriceLine({price:entry,color:side==='buy'?bull:bear,"
            "lineWidth:1,lineStyle:2,axisLabelVisible:true,title});",
            src,
        )


# ─────────────────────────────────────────────────────────────────────
# 10 — multipanel: the touched code lives entirely in the shared Panel
# class methods (one definition, instantiated once per panel p0-p3) —
# no panel-id-specific branching was introduced.
# ─────────────────────────────────────────────────────────────────────
class MultipanelScopeTests(SimpleTestCase):
    def test_no_panel_id_hardcoded_in_touched_methods(self):
        src = _template_source()
        for sig in (
            "_flushVisualRender(){",
            "_cancelPendingVisualRender(){",
            "_cancelPendingCandleRender(){",
            "_updateLiveQuoteDisplay(){",
        ):
            body = _method_body(src, sig)
            for bad in ("id==='p0'", "id==='p1'", "id==='p2'", "id==='p3'", "this.id==='p0'"):
                self.assertNotIn(bad, body)

    def test_touched_methods_are_single_definitions_on_the_shared_class(self):
        src = _template_source()
        for sig in (
            "_flushVisualRender(){",
            "_cancelPendingVisualRender(){",
            "_cancelPendingCandleRender(){",
        ):
            self.assertEqual(src.count(sig), 1)


# ─────────────────────────────────────────────────────────────────────
# 11 — crypto intact: the magnitude/max-age helpers still branch by
# symbol class exactly as before, and are the SAME helpers now reused
# by the candle path — no asset-class special case was added.
# ─────────────────────────────────────────────────────────────────────
class CryptoIntactTests(SimpleTestCase):
    def test_magnitude_and_max_age_helpers_unchanged_symbol_branching(self):
        src = _template_source()
        threshold = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        max_age = _method_body(src, "_maxVisualAgeMs(symbol){")
        self.assertIn("symbol.includes('BTC')||symbol.includes('ETH')", threshold)
        self.assertIn("symbol.includes('BTC')||symbol.includes('ETH')", max_age)

    def test_candle_branch_calls_same_helpers_with_current_symbol(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_candle_start = body.index("if(this._candleRenderPending){")
        i_quote_start = body.index("if(this._quoteRenderPending){")
        candle_branch = body[i_candle_start:i_quote_start]
        self.assertIn("this._visualMagnitudeThreshold(this.currentSymbol,", candle_branch)
        self.assertIn("this._maxVisualAgeMs(this.currentSymbol)", candle_branch)


# ─────────────────────────────────────────────────────────────────────
# 12 — historical candles untouched: the 'history' message handler
# never references the new candle paint anchor or the price-line
# visibility change.
# ─────────────────────────────────────────────────────────────────────
class HistoricalCandlesIntactTests(SimpleTestCase):
    def test_history_handler_does_not_reference_new_candle_anchor(self):
        src = _template_source()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        j = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){", i)
        history_block = src[i:j]
        self.assertNotIn("_lastPaintedCandleClose", history_block)
        self.assertNotIn("_lastPaintedCandleAt", history_block)
        self.assertNotIn("axisLabelVisible", history_block)
