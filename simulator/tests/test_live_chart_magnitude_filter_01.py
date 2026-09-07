# simulator/tests/test_live_chart_magnitude_filter_01.py
"""
LIVE-CHART-MAGNITUDE-FILTER-01 — magnitude+max-age visual filter for the
quote/priceLine/badge/bid-ask paint path (dashboard.html only), layered
inside the existing LIVE-CHART-RENDER-STABILITY-01 100ms throttle.

MASSIVE-CRYPTO-TRADE-CANDLES-01 update: this file originally asserted a
SINGLE shared paint decision for candle AND quote together. That design
was deliberately reversed once crypto candles moved to Massive TRADES
(XT — see consumers.py::price_trade()) while priceLine/badge/bid-ask
stayed on Massive QUOTES (XQ — price_tick()): the two are no longer the
same number, and gating a real, distinct trade by an unrelated quote's
lack of movement would silently hide real chart data.

CHART-LIVE-VISUAL-FILTER-01 update: the claim "candle has NO magnitude
gate at all" is no longer true — it never had to be true for the reason
above to hold. What had to stay true (and still does) is narrower: the
candle must never be gated by the UNRELATED quote's lack of movement.
A same-bucket candle_update CAN now be gated by its OWN close's lack of
movement, via the exact same _visualMagnitudeThreshold()/
_maxVisualAgeMs() functions the quote branch already used — reusing the
functions, never sharing the anchor. Current contract:
  - CANDLE, candle_new (isNew=true, new bucket): unconditional, no gate
    — a brand new bar is always painted directly, exactly as before.
  - CANDLE, candle_update (isNew=false, same bucket):
    100ms throttle, THEN a magnitude+max-age gate against its OWN
    anchor (_lastPaintedCandleClose/_lastPaintedCandleAt) — never
    _lastPaintedMid/_lastPaintedAt (that would reintroduce the old
    coupling this block's audit confirmed was never the point).
  - QUOTE (priceLine/badge/bid-ask via _updateBidAsk()/
    _updateLiveQuoteDisplay()): unchanged magnitude+max-age filter,
    gated against the last PAINTED liveMid.
Both still update financial/derived state (_bars/bid/ask/liveMid/
setPrice) unconditionally on every message — this file only asserts
things about the PAINT decision, never state. candleSeries.update()'s
underlying `b` (open/high/low/close, from _bars) is never altered by
the gate — only whether/when the animation retargets toward it.

Design-lock-mandated pip correction (still in force, unaffected by the
split above): the pip threshold must NEVER be derived from
priceFormatFor().minMove — that's the order-pricing tick/pipette
granularity (0.00001 for EUR/USD-class pairs, 0.001 for USD/JPY — a
TENTH of a real pip), not "1 pip". pipSizeFor() is the explicit, real-
pip-convention helper (0.0001 non-JPY, 0.01 JPY).

Same source-inspection convention as every other dashboard.html test in
this project (no JS test runner is configured) for structural claims.
For the genuinely dynamic behavioral claims this file additionally
extracts the REAL current method bodies from the template at test-run
time (never a hand-copied/stale snippet) into a small Node.js harness
and executes them for real, with real timers. Those tests skip
gracefully (not fail) if `node` isn't on PATH, so they never break a CI
environment without Node — every claim they'd otherwise check is still
covered by a source-level test alongside them.
"""
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from django.test import SimpleTestCase


def _template_source() -> str:
    from django.template.loader import get_template
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _method_body(src: str, method_signature: str, max_len=None) -> str:
    i = src.index(method_signature)
    start = i + len(method_signature)
    m = re.search(r"\n  [A-Za-z_][A-Za-z0-9_]*\s*\(", src[start:])
    end = start + m.start() if m else (start + (max_len or 3500))
    return src[i:end]


NODE_AVAILABLE = shutil.which("node") is not None


class PipSizeSourceTests(SimpleTestCase):
    """Point 1 — the real-pip-vs-pipette correction itself, at the source
    level: the exact, unique definition must exist, and the threshold
    helper must never fall back to priceFormatFor().minMove."""

    def test_pip_size_for_defined_with_real_pip_convention(self):
        src = _template_source()
        self.assertIn(
            "const pipSizeFor=sym=>sym.endsWith('/JPY')?0.01:0.0001;", src,
        )

    def test_threshold_helper_never_references_min_move(self):
        src = _template_source()
        body = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        self.assertNotIn("minMove", body)
        self.assertNotIn("priceFormatFor", body)
        self.assertIn("pipSizeFor(symbol)*0.5", body)

    def test_threshold_helper_uses_1_5_bps_for_crypto(self):
        src = _template_source()
        body = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        self.assertIn("1.5/10000*anchorPrice", body)


class MaxVisualAgeSourceTests(SimpleTestCase):
    def test_forex_max_age_1000ms_crypto_500ms(self):
        src = _template_source()
        body = _method_body(src, "_maxVisualAgeMs(symbol){")
        self.assertIn("?500:1000", body)
        self.assertIn("BTC", body)
        self.assertIn("ETH", body)


class SplitDecisionSourceTests(SimpleTestCase):
    """MASSIVE-CRYPTO-TRADE-CANDLES-01 — candle and quote are INDEPENDENT
    branches: each has its OWN magnitude+max-age gate now
    (CHART-LIVE-VISUAL-FILTER-01), against its OWN anchor — never a
    shared decision, never a shared anchor. This replaces the prior
    "candle has no gate at all" contract this file used to assert
    (deliberately reversed — see module docstring)."""

    def test_candle_branch_gates_only_the_same_bucket_update_path(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_candle_start = body.index("if(this._candleRenderPending){")
        i_quote_start = body.index("if(this._quoteRenderPending){")
        candle_branch = body[i_candle_start:i_quote_start]
        # The gate exists, uses the shared threshold functions, and its
        # own dedicated anchor — but NEVER the quote's anchor.
        self.assertIn("shouldPaintCandle", candle_branch)
        self.assertIn("this._visualMagnitudeThreshold(", candle_branch)
        self.assertIn("this._maxVisualAgeMs(", candle_branch)
        self.assertIn("_lastPaintedCandleClose", candle_branch)
        self.assertIn("_lastPaintedCandleAt", candle_branch)
        self.assertNotIn("_lastPaintedMid", candle_branch)
        self.assertNotIn("_lastPaintedAt=", candle_branch)  # the quote anchor's own setter, never here
        # volume/scrollToRealTime/indicators stay unconditional — only
        # candleSeries.update()'s retarget is gated.
        self.assertIn("this.volumeSeries.update(", candle_branch)
        self.assertIn("this._updateIndicatorsLastBar(", candle_branch)

    def test_candle_new_bucket_path_is_unconditional(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_candle_start = body.index("if(this._candleRenderPending){")
        i_quote_start = body.index("if(this._quoteRenderPending){")
        candle_branch = body[i_candle_start:i_quote_start]
        i_isnew = candle_branch.index("if(isNew){")
        i_else = candle_branch.index("}else{", i_isnew)
        new_bucket_path = candle_branch[i_isnew:i_else]
        self.assertIn("this.candleSeries.update(b)", new_bucket_path)
        self.assertNotIn("shouldPaintCandle", new_bucket_path)
        self.assertNotIn("_visualMagnitudeThreshold", new_bucket_path)
        self.assertNotIn("_maxVisualAgeMs", new_bucket_path)

    def test_quote_branch_still_has_should_paint_gate(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_quote_start = body.index("if(this._quoteRenderPending){")
        quote_branch = body[i_quote_start:]
        self.assertIn("shouldPaint", quote_branch)
        self.assertIn("this._visualMagnitudeThreshold(", quote_branch)
        self.assertIn("this._maxVisualAgeMs(", quote_branch)
        self.assertIn("if(shouldPaint){", quote_branch)
        self.assertIn("this._updateBidAsk();", quote_branch)
        # LIVE-CHART-SMOOTH-INTERPOLATION-01 — the actual quote paint is
        # now split into instant badge/glow + animated priceLine, both
        # still fully gated by the SAME shouldPaint check asserted above.
        self.assertIn("this._paintBadgeAndGlow(current);", quote_branch)
        self.assertIn("this._retargetPriceLineAnimation(current);", quote_branch)

    def test_candle_and_quote_are_independent_top_level_branches(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        self.assertIn("if(this._candleRenderPending){", body)
        self.assertIn("if(this._quoteRenderPending){", body)


class LifecycleResetSourceTests(SimpleTestCase):
    """Point 5 — symbol switch: full reset of BOTH anchors (quote —
    unchanged — and, CHART-LIVE-VISUAL-FILTER-01, candle). Timeframe
    switch: never touches the quote anchor (the candle branch doesn't
    read it, and the quote/priceLine contract is explicitly unaffected
    by a timeframe-only change) but DOES reset the candle anchor — the
    old timeframe's close must never gate the new timeframe's first
    bar."""

    def test_symbol_switch_resets_both_paint_anchors(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingVisualRender(){")
        self.assertIn("this._lastPaintedMid=null", body)
        self.assertIn("this._lastPaintedAt=null", body)
        self.assertIn("this._lastPaintedCandleClose=null", body)
        self.assertIn("this._lastPaintedCandleAt=null", body)

    def test_timeframe_switch_resets_candle_anchor_only(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingCandleRender(){")
        self.assertNotIn("_lastPaintedMid", body)
        self.assertNotIn("_lastPaintedAt=null", body)  # the quote anchor's setter, never here
        self.assertIn("this._lastPaintedCandleClose=null", body)
        self.assertIn("this._lastPaintedCandleAt=null", body)


class ThrottleUnchangedSourceTests(SimpleTestCase):
    """Point 6 — no new timer; the existing 100ms throttle contract is
    untouched (same guard already verified in LIVE-CHART-RENDER-
    STABILITY-01's own test file — re-confirmed here as a regression
    guard specific to this block's changes)."""

    def test_schedule_visual_render_unchanged(self):
        src = _template_source()
        body = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        self.assertIn("if(this._visualRenderTimer!=null)return;", body)
        self.assertEqual(body.count("setTimeout("), 1)
        self.assertIn(",100)", body)

    def test_no_second_timer_introduced_anywhere_in_flush_or_threshold_helpers(self):
        src = _template_source()
        for sig in (
            "_flushVisualRender(){",
            "_visualMagnitudeThreshold(symbol, anchorPrice){",
            "_maxVisualAgeMs(symbol){",
        ):
            body = _method_body(src, sig)
            self.assertNotIn("setTimeout(", body)
            self.assertNotIn("setInterval(", body)
            self.assertNotIn("requestAnimationFrame(", body)


class NoTouchScopeTests(SimpleTestCase):
    def test_new_helpers_never_touch_websocket_or_financial_paths(self):
        src = _template_source()
        for sig in (
            "_flushVisualRender(){",
            "_visualMagnitudeThreshold(symbol, anchorPrice){",
            "_maxVisualAgeMs(symbol){",
        ):
            body = _method_body(src, sig)
            self.assertNotIn("this.ws.send", body)
            self.assertNotIn("_resetAgg", body)
            self.assertNotIn("_recomputeSpread", body)
            self.assertNotIn("setPrice(", body)


# ─────────────────────────────────────────────────────────────────────────
# Behavioral verification via a real Node.js execution of the CURRENT
# (freshly re-extracted from the template every run, never hand-copied)
# method bodies. Skips gracefully if node is unavailable.
# ─────────────────────────────────────────────────────────────────────────
class MagnitudeFilterBehaviorTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH — behavioral checks skipped, "
                           "structural coverage above still applies")

    def _harness_source(self) -> str:
        src = _template_source()
        m = re.search(r"const pipSizeFor=.*?;", src)
        pip_size_for = m.group(0)
        vol_point_for_bar = re.search(r"const volPointForBar=.*?;\n", src).group(0)
        schedule = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        threshold = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        max_age = _method_body(src, "_maxVisualAgeMs(symbol){")
        # Renamed (name only, body untouched) so a thin counting wrapper
        # can observe real invocation counts without altering the
        # verbatim throttle/magnitude logic itself.
        flush = _method_body(src, "_flushVisualRender(){").replace(
            "_flushVisualRender(){", "_flushVisualRenderReal(){", 1,
        )
        cancel_full = _method_body(src, "_cancelPendingVisualRender(){")
        cancel_candle = _method_body(src, "_cancelPendingCandleRender(){")

        return textwrap.dedent(f"""
        'use strict';
        {pip_size_for}
        {vol_point_for_bar}

        class FakePanel {{
          constructor(symbol){{
            this.currentSymbol=symbol;
            this._visualRenderTimer=null;
            this._quoteRenderPending=false;
            this._candleRenderPending=false;
            this._candleRenderIsNew=false;
            this._lastPaintedMid=null;
            this._lastPaintedAt=null;
            this._lastPaintedCandleClose=null;
            this._lastPaintedCandleAt=null;
            this._bars=[];
            this.bid=1; this.ask=1.1; this.stayLive=false; this.liveMid=null;
            this.candleSeries={{ update: () => {{ this.counters.candleUpdate++; }} }};
            this.volumeSeries={{ update: () => {{ this.counters.volumeUpdate++; }} }};
            this.chart={{ timeScale: () => ({{ scrollToRealTime: () => {{}} }}) }};
            this.counters={{ candleUpdate:0, volumeUpdate:0, quoteDisplay:0, bidAsk:0, indicators:0, flushCalls:0 }};
          }}
          _updateBidAsk(){{ this.counters.bidAsk++; }}
          _updateIndicatorsLastBar(b, isNew){{ this.counters.indicators++; }}
          // LIVE-CHART-SMOOTH-INTERPOLATION-01 stand-ins — this file's
          // own concern is the magnitude/max-age GATE (whether a paint
          // happens at all), not the animation mechanism (its own
          // dedicated test file covers that in full) — so these are
          // simple synchronous counters, not real interpolation.
          _paintBadgeAndGlow(px){{ this.counters.quoteDisplay++; }}
          _retargetPriceLineAnimation(px){{ /* no-op stub — animation itself is out of scope here */ }}
          _retargetCandleAnimation(bar){{ this.candleSeries.update(bar); }}
          _cancelVisualAnimation(){{ /* no-op stub — animation itself is out of scope here */ }}

          {threshold}

          {max_age}

          {schedule}

          {flush}

          _flushVisualRender(){{ this.counters.flushCalls=(this.counters.flushCalls||0)+1; this._flushVisualRenderReal(); }}

          {cancel_full}

          {cancel_candle}

          // MASSIVE-CRYPTO-TRADE-CANDLES-01 — quote and candle are now
          // independent real-world triggers (price_tick() vs
          // price_trade(), separate WS messages); exposed here as
          // separate methods so tests can drive/observe them in
          // isolation, plus tick() for the common case where one real
          // broadcast happens to produce both (a trade AND a quote
          // update at the same moment).
          tickQuote(mid){{
            this.liveMid=mid;
            this._scheduleVisualRender('quote');
          }}
          tickCandle(closePrice, isNew=false){{
            this._bars.push({{time: Math.floor(Date.now()/1000), open:closePrice, high:closePrice, low:closePrice, close:closePrice}});
            this._scheduleVisualRender('candle', isNew);
          }}
          tick(mid){{
            this.tickQuote(mid);
            this.tickCandle(mid);
          }}
        }}

        module.exports = {{ FakePanel }};
        """)

    def _run_scenario(self, scenario_body: str) -> dict:
        harness = self._harness_source()
        driver = f"""
        {harness}
        (async () => {{
          {scenario_body}
        }})();
        """
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run(
                ["node", str(path)], capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            return json.loads(result.stdout.strip().splitlines()[-1])

    # ---- candle: CHART-LIVE-VISUAL-FILTER-01 — same-bucket updates now
    # gated by their OWN magnitude+max-age filter; a brand new bucket
    # always paints regardless ----

    def test_candle_same_bucket_sub_threshold_move_suppressed(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.tickCandle(80000);
          await new Promise(r => setTimeout(r, 150));
          p.tickCandle(80000.001); // trivially tiny move — far below any bps threshold
          await new Promise(r => setTimeout(r, 150));
          p.tickCandle(80000.002);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate}));
        """)
        self.assertEqual(out["candleUpdate"], 1)  # only the first (anchor-establishing) paint

    def test_candle_same_bucket_move_crossing_threshold_paints(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.tickCandle(80000);
          await new Promise(r => setTimeout(r, 150));
          const afterFirst = p.counters.candleUpdate;
          p.tickCandle(80013); // +$13 from the anchor — above the 1.5bps/$12 threshold
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterFirst, afterCross: p.counters.candleUpdate}));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterCross"], 2)

    def test_candle_new_bucket_always_paints_even_for_trivial_move(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.tickCandle(80000, true);  // candle_new
          await new Promise(r => setTimeout(r, 150));
          p.tickCandle(80000.001, true); // another candle_new, trivial move — must still paint
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate}));
        """)
        self.assertEqual(out["candleUpdate"], 2)  # isNew bypasses the gate entirely

    def test_candle_max_age_forces_render_without_threshold_cross(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.tickCandle(80000);
          await new Promise(r => setTimeout(r, 150));
          const afterFirst = p.counters.candleUpdate;
          p.tickCandle(80000.001);
          await new Promise(r => setTimeout(r, 150));
          const afterSubThreshold = p.counters.candleUpdate;
          await new Promise(r => setTimeout(r, 450));
          p.tickCandle(80000.001);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterFirst, afterSubThreshold, afterMaxAge: p.counters.candleUpdate}));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterSubThreshold"], 1)
        self.assertEqual(out["afterMaxAge"], 2)  # BTC max-age is 500ms — forced even without crossing

    def test_candle_paints_immediately_regardless_of_prior_quote_state(self):
        # No dependency on this.liveMid/_lastPaintedMid whatsoever.
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          // liveMid/_lastPaintedMid stay null throughout — candle must
          // still paint on its own schedule.
          p.tickCandle(80000);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate, liveMidStillNull: p.liveMid===null}));
        """)
        self.assertEqual(out["candleUpdate"], 1)
        self.assertTrue(out["liveMidStillNull"])

    # ---- quote: magnitude+max-age gate unchanged ----

    def test_quote_sub_threshold_move_does_not_render(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          const afterFirst = p.counters.quoteDisplay;
          p.tickQuote(1.16002); // +0.2 pip — sub-threshold
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterFirst, afterSecond: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterSecond"], 1)

    def test_quote_accumulated_moves_cross_threshold_and_render(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p.tickQuote(1.16002);
          await new Promise(r => setTimeout(r, 150));
          p.tickQuote(1.16004);
          await new Promise(r => setTimeout(r, 150));
          p.tickQuote(1.16006); // cumulative +0.6 pip from 1.16000 — should cross
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({quoteDisplay: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["quoteDisplay"], 2)

    def test_quote_max_age_forces_render_without_threshold_cross(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          const afterFirst = p.counters.quoteDisplay;
          p.tickQuote(1.16001);
          await new Promise(r => setTimeout(r, 150));
          const afterSubThreshold = p.counters.quoteDisplay;
          await new Promise(r => setTimeout(r, 950));
          p.tickQuote(1.16001);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterFirst, afterSubThreshold, afterMaxAge: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterSubThreshold"], 1)
        self.assertEqual(out["afterMaxAge"], 2)

    def test_quote_btc_bps_threshold_correct(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.tickQuote(80000);
          await new Promise(r => setTimeout(r, 150));
          const afterFirst = p.counters.quoteDisplay;
          p.tickQuote(80010); // +$10, 1.25bps — below the 1.5bps/$12 threshold
          await new Promise(r => setTimeout(r, 150));
          const afterSmall = p.counters.quoteDisplay;
          p.tickQuote(80013); // now +$13 from the anchor — above $12
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterFirst, afterSmall, afterCross: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["afterFirst"], 1)
        self.assertEqual(out["afterSmall"], 1)
        self.assertEqual(out["afterCross"], 2)

    # ---- candle and quote can now legitimately diverge ----

    def test_candle_and_quote_gate_independently_on_their_own_anchors(self):
        # CHART-LIVE-VISUAL-FILTER-01 — both branches are now filtered,
        # but against SEPARATE anchors: the same real-world moment can
        # cross the candle's own threshold while staying sub-threshold
        # for the quote (or vice versa) — this is what "independent"
        # means now, not "candle is unfiltered".
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tick(1.16000); // both fire once, both anchors establish at the same value
          await new Promise(r => setTimeout(r, 150));
          p.tickCandle(1.16006); // +6 pips — crosses the candle's OWN threshold
          p.tickQuote(1.16002);  // +0.2 pip — sub-threshold for the quote's OWN anchor
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate, quoteDisplay: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["candleUpdate"], 2)   # crossed its own threshold — painted
        self.assertEqual(out["quoteDisplay"], 1)   # stayed sub-threshold — suppressed

    def test_candle_trivial_move_does_not_leak_into_quote_anchor_or_vice_versa(self):
        # The two anchors are genuinely separate state — a move that
        # only affects one must never influence the other's decision.
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tick(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p.tickCandle(1.16000001); // trivial candle move — suppressed
          await new Promise(r => setTimeout(r, 150));
          const midAnchor = p._lastPaintedMid;
          const candleAnchor = p._lastPaintedCandleClose;
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate, midAnchor, candleAnchor}));
        """)
        self.assertEqual(out["candleUpdate"], 1)  # only the first, anchor-establishing paint
        self.assertEqual(out["midAnchor"], 1.16)
        self.assertEqual(out["candleAnchor"], 1.16)

    # ---- lifecycle ----

    def test_symbol_switch_forces_immediate_next_quote_paint(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p._cancelPendingVisualRender();
          p.currentSymbol = 'GBP/USD';
          p.tickQuote(1.35000);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({quoteDisplay: p.counters.quoteDisplay, anchorAfterSwitch: p._lastPaintedMid}));
        """)
        self.assertEqual(out["quoteDisplay"], 2)
        self.assertEqual(out["anchorAfterSwitch"], 1.35)

    def test_timeframe_switch_does_not_disturb_quote_anchor(self):
        # _cancelPendingCandleRender() (timeframe switch) must NOT reset
        # the quote anchor — a sub-threshold quote move right after a TF
        # switch stays suppressed, proving the anchor survived untouched.
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p._cancelPendingCandleRender();
          p.tickQuote(1.16002); // still sub-threshold relative to the SAME anchor
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({quoteDisplay: p.counters.quoteDisplay}));
        """)
        self.assertEqual(out["quoteDisplay"], 1)

    def test_timeframe_switch_resets_candle_anchor_so_next_bar_paints_then_gates_again(self):
        # CHART-LIVE-VISUAL-FILTER-01 — _cancelPendingCandleRender() now
        # resets the candle anchor (the OLD timeframe's close must never
        # gate the NEW timeframe's first bar): the immediate next candle
        # after a TF switch paints unconditionally (anchor==null), same
        # observable outcome as before this block — but a SECOND trivial
        # move right after that must now be suppressed, proving the gate
        # is genuinely active again (not "candle never checks one").
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickCandle(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p._cancelPendingCandleRender();
          p.tickCandle(1.16000001); // trivial move, but anchor was just reset — paints
          await new Promise(r => setTimeout(r, 150));
          const afterReset = p.counters.candleUpdate;
          p.tickCandle(1.16000002); // trivial move again, anchor now set — suppressed
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({afterReset, afterSecondTrivialMove: p.counters.candleUpdate}));
        """)
        self.assertEqual(out["afterReset"], 2)
        self.assertEqual(out["afterSecondTrivialMove"], 2)

    def test_throttle_still_one_timer_per_burst(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          for(let i=0;i<40;i++) p.tick(80000+i);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({flushCalls: p.counters.flushCalls}));
        """)
        # 40 ticks fired synchronously inside one 100ms window must still
        # produce exactly one flush (one setTimeout, not 40) — this is
        # LIVE-CHART-RENDER-STABILITY-01's own guarantee, unaffected by
        # this block; the candle branch just always paints once per flush.
        self.assertEqual(out["flushCalls"], 1)
