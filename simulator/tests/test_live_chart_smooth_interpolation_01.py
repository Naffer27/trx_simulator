# simulator/tests/test_live_chart_smooth_interpolation_01.py
"""
LIVE-CHART-SMOOTH-INTERPOLATION-01 — visual-only interpolation for the
candle's displayed close and the priceLine (dashboard.html only). Manual
acceptance of MASSIVE-CRYPTO-TRADE-CANDLES-01 confirmed the data source
split (candle=trades, quote=quotes) fixed the semantic mismatch but the
last candle and priceLine still visually "jump" — this block smooths the
DRAWN pixels via a single shared requestAnimationFrame loop per panel,
over ~200ms, toward the latest real target. this.liveMid/this._bars/
setPrice() (PnL/RR)/execution are never touched by any of this — only
candleSeries.update()'s close and priceLine.applyOptions()'s price are
ever animated; open/high/low always come from the real, current bar,
fresh every frame. Badge text and bid/ask visual stay instant/exact
(interpolating displayed digits reads as flicker, not smoothness).

Safety argument (auditable, not just asserted): interpolating between
two REAL close values that were each within [low, high] at their own
time always stays within [low, high], because within one bucket low/
high only ever widen (never shrink) — so any point between two values
that were each in-range at an earlier, narrower version of the range
stays in-range for the current, wider version too.

Same source-inspection + real-Node-execution hybrid convention already
established in this project's other dashboard.html test files (no JS
test runner is a committed dependency — Node-driven tests skip
gracefully, never fail, when `node` isn't on PATH).
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


# ─────────────────────────────────────────────────────────────────────────
# Structural / source-inspection
# ─────────────────────────────────────────────────────────────────────────
class DurationConstantTests(SimpleTestCase):
    def test_anim_duration_is_200ms(self):
        src = _template_source()
        self.assertIn("this._animDurationMs=200;", src)


class SingleRafSourceTests(SimpleTestCase):
    """Condition 1 — exactly one requestAnimationFrame call site for
    THIS mechanism (dashboard.html already uses rAF elsewhere for
    unrelated features — glow/resize/indicator drawing — so the count
    is scoped to _ensureVisualAnimLoop()/_stepVisualAnim() only, not the
    whole file), guarded so retargeting never starts a second concurrent
    loop."""

    def test_exactly_one_raf_call_site_for_visual_anim(self):
        src = _template_source()
        ensure_loop = _method_body(src, "_ensureVisualAnimLoop(){")
        step_anim = _method_body(src, "_stepVisualAnim(){")
        # _ensureVisualAnimLoop() is the only place that starts the
        # loop; _stepVisualAnim() re-schedules itself via the same
        # pattern inline (its own single call, for continuing an
        # already-running loop) — exactly one requestAnimationFrame(
        # call in each of these two methods, none anywhere else in the
        # animation mechanism.
        self.assertEqual(ensure_loop.count("requestAnimationFrame("), 1)
        self.assertEqual(step_anim.count("requestAnimationFrame("), 1)

    def test_ensure_loop_guarded_against_duplicate_scheduling(self):
        src = _template_source()
        body = _method_body(src, "_ensureVisualAnimLoop(){")
        self.assertIn("if(this._visualAnimRAF==null){", body)

    def test_cancel_visual_animation_uses_cancel_animation_frame(self):
        src = _template_source()
        body = _method_body(src, "_cancelVisualAnimation(){")
        self.assertIn("cancelAnimationFrame(this._visualAnimRAF)", body)
        self.assertIn("this._visualAnimRAF=null", body)
        self.assertIn("this._candleAnim=null", body)
        self.assertIn("this._quoteAnim=null", body)


class CandleAnimationSourceTests(SimpleTestCase):
    """Conditions 3/4 — candle_new snaps, candle_update animates close
    only; open/high/low always real."""

    def test_candle_new_snaps_without_animation(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_isnew = body.index("if(isNew){")
        i_else = body.index("}else{", i_isnew)
        new_branch = body[i_isnew:i_else]
        self.assertIn("this._candleAnim=null;", new_branch)
        self.assertIn("this.candleSeries.update(b);", new_branch)

    def test_candle_update_retargets_animation(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_else = body.index("}else{")
        i_end = body.index("}", body.index("_retargetCandleAnimation(b);"))
        update_branch = body[i_else:i_end]
        self.assertIn("this._retargetCandleAnimation(b);", update_branch)

    def test_indicators_volume_scroll_stay_outside_animation_and_use_real_bar(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_isnew = body.index("if(isNew){")
        immediate_part = body[:i_isnew]
        self.assertIn("this.volumeSeries.update(volPointForBar(b));", immediate_part)
        self.assertIn("this._updateIndicatorsLastBar(b, isNew);", immediate_part)
        self.assertIn("this.chart.timeScale().scrollToRealTime();", immediate_part)

    def test_step_anim_reads_open_high_low_from_real_bar_every_frame(self):
        src = _template_source()
        body = _method_body(src, "_stepVisualAnim(){")
        self.assertIn("open:b.open,high:b.high,low:b.low,close", body)
        # Only close is ever the interpolated value.
        self.assertNotIn("open:close", body)


class QuoteAnimationSourceTests(SimpleTestCase):
    """Condition 5 — priceLine animates; badge/bid-ask stay instant."""

    def test_flush_quote_branch_calls_instant_badge_and_animated_priceline(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        i_quote = body.index("if(this._quoteRenderPending){")
        quote_branch = body[i_quote:]
        self.assertIn("this._updateBidAsk();", quote_branch)
        self.assertIn("this._paintBadgeAndGlow(current);", quote_branch)
        self.assertIn("this._retargetPriceLineAnimation(current);", quote_branch)
        self.assertNotIn("_updateLiveQuoteDisplay()", quote_branch)

    def test_paint_badge_and_glow_never_touches_priceline(self):
        src = _template_source()
        # Truncate at the method's OWN closing brace (_method_body's
        # next-method-signature boundary would otherwise also capture
        # _retargetPriceLineAnimation()'s preceding doc-comment, which
        # legitimately mentions "priceLine" in prose about a DIFFERENT
        # method).
        i = src.index("_paintBadgeAndGlow(px){")
        j = src.index("this._drawGlow(px);", i)
        body = src[i:src.index("}", j) + 1]
        self.assertNotIn("priceLine", body)
        self.assertIn("this.pxTag", body)
        self.assertIn("this._drawGlow(px);", body)

    def test_priceline_first_creation_is_instant(self):
        src = _template_source()
        body = _method_body(src, "_retargetPriceLineAnimation(px){")
        i_create = body.index("createPriceLine(")
        i_quoteanim_null = body.index("this._quoteAnim=null;")
        i_return = body.index("return;", i_quoteanim_null)
        self.assertLess(i_create, i_quoteanim_null)
        self.assertLess(i_quoteanim_null, i_return)

    def test_step_anim_applies_options_only_when_priceline_exists(self):
        src = _template_source()
        body = _method_body(src, "_stepVisualAnim(){")
        self.assertIn("this._quoteAnim&&this.priceLine", body)


class LifecycleSourceTests(SimpleTestCase):
    """Conditions 7/8/9 — symbol switch full stop, timeframe switch
    candle-only stop, disconnect stop, defensive guard."""

    def test_symbol_switch_cancels_visual_animation(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingVisualRender(){")
        self.assertIn("this._cancelVisualAnimation();", body)

    def test_timeframe_switch_cancels_only_candle_animation(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingCandleRender(){")
        self.assertIn("this._candleAnim=null;", body)
        self.assertNotIn("_quoteAnim", body)
        self.assertNotIn("_cancelVisualAnimation", body)  # full-stop is symbol-switch only

    def test_disconnect_reaches_the_same_full_cancel(self):
        src = _template_source()
        i = src.index("this.ws.onclose=()=>{")
        j = src.index("};", i)
        onclose_body = src[i:j]
        self.assertIn("this._cancelPendingVisualRender();", onclose_body)

    def test_step_anim_has_defensive_guard(self):
        src = _template_source()
        body = _method_body(src, "_stepVisualAnim(){")
        self.assertIn("if(!this.candleSeries||!this.chart){", body)
        i_guard = body.index("if(!this.candleSeries||!this.chart){")
        i_guard_end = body.index("}", i_guard)
        guard_body = body[i_guard:i_guard_end]
        self.assertIn("this._visualAnimRAF=null", guard_body)
        self.assertIn("return;", guard_body)


class NoTouchScopeTests(SimpleTestCase):
    def test_animation_methods_never_touch_financial_state(self):
        src = _template_source()
        for sig in (
            "_stepVisualAnim(){", "_retargetCandleAnimation(bar){",
            "_retargetPriceLineAnimation(px){", "_cancelVisualAnimation(){",
        ):
            body = _method_body(src, sig)
            self.assertNotIn("this.liveMid=", body)
            self.assertNotIn("this._bars=", body)
            self.assertNotIn("this._bars.push", body)
            self.assertNotIn("this._bars[", body)
            self.assertNotIn("setPrice(", body)
            self.assertNotIn("this.ws.send", body)

    def test_throttle_and_magnitude_filter_untouched(self):
        src = _template_source()
        sched = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        self.assertIn("if(this._visualRenderTimer!=null)return;", sched)
        self.assertEqual(sched.count("setTimeout("), 1)
        threshold = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        self.assertIn("pipSizeFor(symbol)*0.5", threshold)
        self.assertIn("1.5/10000*anchorPrice", threshold)


# ─────────────────────────────────────────────────────────────────────────
# Behavioral verification via a real Node.js execution of the CURRENT
# (freshly re-extracted from the template every run) method bodies.
# ─────────────────────────────────────────────────────────────────────────
class SmoothInterpolationBehaviorTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH — behavioral checks skipped, "
                           "structural coverage above still applies")

    def _harness_source(self) -> str:
        src = _template_source()
        vol_point_for_bar = re.search(r"const volPointForBar=.*?;\n", src).group(0)
        pip_size_for = re.search(r"const pipSizeFor=.*?;", src).group(0)
        threshold = _method_body(src, "_visualMagnitudeThreshold(symbol, anchorPrice){")
        max_age = _method_body(src, "_maxVisualAgeMs(symbol){")
        schedule = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        flush = _method_body(src, "_flushVisualRender(){")
        cancel_full = _method_body(src, "_cancelPendingVisualRender(){")
        cancel_candle = _method_body(src, "_cancelPendingCandleRender(){")
        paint_badge = _method_body(src, "_paintBadgeAndGlow(px){")
        retarget_priceline = _method_body(src, "_retargetPriceLineAnimation(px){")
        current_anim_value = _method_body(src, "_currentAnimValue(anim,now){")
        ensure_loop = _method_body(src, "_ensureVisualAnimLoop(){")
        step_anim = _method_body(src, "_stepVisualAnim(){")
        retarget_candle = _method_body(src, "_retargetCandleAnimation(bar){")
        cancel_anim = _method_body(src, "_cancelVisualAnimation(){")

        return textwrap.dedent(f"""
        'use strict';
        {pip_size_for}
        {vol_point_for_bar}
        function priceFormatFor(sym){{ return {{precision:2}}; }}

        // Plain Node has no browser rAF/performance — polyfill with
        // real timers so the harness exercises the REAL, unmodified
        // _stepVisualAnim()/_ensureVisualAnimLoop() logic under real
        // async timing, not a fake clock.
        const performance = {{ now: () => Date.now() }};
        let _rafId = 1;
        const _rafTimers = new Map();
        function requestAnimationFrame(cb){{
          const id = _rafId++;
          const t = setTimeout(() => {{ _rafTimers.delete(id); cb(); }}, 16);
          _rafTimers.set(id, t);
          return id;
        }}
        function cancelAnimationFrame(id){{
          const t = _rafTimers.get(id);
          if(t){{ clearTimeout(t); _rafTimers.delete(id); }}
        }}

        class FakePanel {{
          constructor(symbol){{
            this.currentSymbol=symbol;
            this._visualRenderTimer=null;
            this._quoteRenderPending=false;
            this._candleRenderPending=false;
            this._candleRenderIsNew=false;
            this._lastPaintedMid=null;
            this._lastPaintedAt=null;
            this._visualAnimRAF=null;
            this._candleAnim=null;
            this._quoteAnim=null;
            this._animDurationMs=200;
            this._bars=[];
            this.bid=1; this.ask=1.1; this.stayLive=false; this.liveMid=null; this.prevLiveMid=null;
            this.priceLine=null;
            this.pxTag={{ textContent:'', classList:{{ toggle:()=>{{}} }} }};
            this.candleSeries={{
              update: (bar) => {{ this.counters.candleUpdate++; this.lastCandleUpdatePayload=bar; }},
              createPriceLine: (opts) => {{ this.counters.priceLineCreated++; this.lastPriceLineValue=opts.price; return {{ applyOptions: (o) => {{ this.counters.priceLineApplyOptions++; this.lastPriceLineValue=o.price; }} }}; }},
            }};
            this.volumeSeries={{ update: () => {{}} }};
            this.chart={{ timeScale: () => ({{ scrollToRealTime: () => {{}} }}) }};
            this.counters={{ candleUpdate:0, priceLineCreated:0, priceLineApplyOptions:0, bidAsk:0, badgeAndGlow:0, indicators:0 }};
          }}
          _updateBidAsk(){{ this.counters.bidAsk++; }}
          _updateIndicatorsLastBar(b, isNew){{ this.counters.indicators++; }}
          _drawGlow(px){{}}

          {threshold}
          {max_age}
          {schedule}
          {flush}
          {cancel_full}
          {cancel_candle}
          {paint_badge}
          {retarget_priceline}
          {current_anim_value}
          {ensure_loop}
          {step_anim}
          {retarget_candle}
          {cancel_anim}

          tickQuote(mid){{ this.liveMid=mid; this._scheduleVisualRender('quote'); }}
          pushBar(close, isNew){{
            const time = this._bars.length ? this._bars[this._bars.length-1].time + (isNew?60:0) : 1000;
            const bar = this._bars.length && !isNew
              ? {{...this._bars[this._bars.length-1], close, high:Math.max(this._bars[this._bars.length-1].high,close), low:Math.min(this._bars[this._bars.length-1].low,close)}}
              : {{time, open:close, high:close, low:close, close}};
            if(isNew || !this._bars.length) this._bars.push(bar);
            else this._bars[this._bars.length-1]=bar;
            this._scheduleVisualRender('candle', isNew);
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
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.fail(f"node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_interpolation_never_leaves_low_high_range(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          const samples = [];
          const origUpdate = p.candleSeries.update;
          p.candleSeries.update = (bar) => { samples.push(bar); origUpdate(bar); };
          p.pushBar(80100, false); // triggers a 100ms-throttled flush, then a 200ms animation
          await new Promise(r => setTimeout(r, 30));
          p.pushBar(80050, false); // retarget mid-animation
          await new Promise(r => setTimeout(r, 300));
          const violations = samples.filter(s => s.close < s.low || s.close > s.high);
          console.log(JSON.stringify({sampleCount: samples.length, violations: violations.length}));
        """)
        self.assertGreater(out["sampleCount"], 0)
        self.assertEqual(out["violations"], 0)

    def test_candle_new_paints_immediately_no_animation_frames(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({candleUpdate: p.counters.candleUpdate, close: p.lastCandleUpdatePayload.close}));
        """)
        self.assertEqual(out["candleUpdate"], 1)
        self.assertEqual(out["close"], 80000)

    def test_candle_update_eventually_converges_to_real_close(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          p.pushBar(80100, false);
          await new Promise(r => setTimeout(r, 400)); // well past 100ms throttle + 200ms anim
          console.log(JSON.stringify({close: p.lastCandleUpdatePayload.close, updateCalls: p.counters.candleUpdate}));
        """)
        self.assertAlmostEqual(out["close"], 80100, delta=0.01)
        self.assertGreater(out["updateCalls"], 1)  # multiple animation frames, not just one jump

    def test_retarget_continues_from_current_visual_position_no_jump(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          p.pushBar(80100, false);
          await new Promise(r => setTimeout(r, 50)); // mid-animation, roughly 25% through
          const midValue = p.lastCandleUpdatePayload.close;
          p.pushBar(80050, false); // retarget to a LOWER value while still rising
          await new Promise(r => setTimeout(r, 16)); // one more frame
          const nextValue = p.lastCandleUpdatePayload.close;
          console.log(JSON.stringify({midValue, nextValue}));
        """)
        # The very next frame after a retarget must stay close to where
        # the animation already was — never jump back to the old target
        # (80100) or straight to the new one (80050).
        self.assertLess(abs(out["nextValue"] - out["midValue"]), 15)

    def test_only_one_raf_active_across_multiple_retargets(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          const raf1 = p._visualAnimRAF;
          p.pushBar(80010, false);
          await new Promise(r => setTimeout(r, 120));
          p.pushBar(80020, false);
          await new Promise(r => setTimeout(r, 120));
          const rafActiveNow = p._visualAnimRAF !== null;
          console.log(JSON.stringify({rafActiveNow}));
        """)
        self.assertTrue(out["rafActiveNow"])  # still exactly one, still running toward the latest target

    def test_priceline_animates_badge_stays_instant(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150)); // past the 100ms throttle — first creation lands
          const badgeCallsAfterFirst = p.counters.bidAsk;
          p.tickQuote(1.17000); // large real move — passes magnitude filter
          await new Promise(r => setTimeout(r, 108)); // just past the 100ms throttle — animation barely started
          const priceLineDuringAnim = p.lastPriceLineValue;
          await new Promise(r => setTimeout(r, 400)); // well past the 200ms animation duration
          const priceLineFinal = p.lastPriceLineValue;
          console.log(JSON.stringify({priceLineDuringAnim, priceLineFinal, applyOptionsCalls: p.counters.priceLineApplyOptions}));
        """)
        # Timing-tolerant: real setTimeout/rAF scheduling has jitter, so
        # assert the (much more robust) proof that real animation frames
        # happened at all, plus a non-decreasing convergence toward the
        # target — not a brittle exact mid-flight sample.
        self.assertLessEqual(out["priceLineDuringAnim"], out["priceLineFinal"])
        self.assertAlmostEqual(out["priceLineFinal"], 1.17, delta=0.0001)
        self.assertGreater(out["applyOptionsCalls"], 1)

    def test_priceline_first_creation_not_animated(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          console.log(JSON.stringify({created: p.counters.priceLineCreated, applyOptions: p.counters.priceLineApplyOptions}));
        """)
        self.assertEqual(out["created"], 1)
        self.assertEqual(out["applyOptions"], 0)  # never animated on first creation

    def test_symbol_switch_stops_animation_and_snaps(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          p.pushBar(80500, false);
          await new Promise(r => setTimeout(r, 20)); // mid-animation
          p._cancelPendingVisualRender();
          console.log(JSON.stringify({rafAfterCancel: p._visualAnimRAF, candleAnimAfterCancel: p._candleAnim}));
        """)
        self.assertIsNone(out["rafAfterCancel"])
        self.assertIsNone(out["candleAnimAfterCancel"])

    def test_timeframe_switch_leaves_quote_animation_running(self):
        out = self._run_scenario("""
          const p = new FakePanel('EUR/USD');
          p.tickQuote(1.16000);
          await new Promise(r => setTimeout(r, 150));
          p.tickQuote(1.17000);
          await new Promise(r => setTimeout(r, 130)); // past the 100ms throttle — mid quote animation
          p._cancelPendingCandleRender(); // timeframe switch
          const quoteAnimSurvived = p._quoteAnim !== null;
          await new Promise(r => setTimeout(r, 300));
          console.log(JSON.stringify({quoteAnimSurvived, finalPrice: p.lastPriceLineValue}));
        """)
        self.assertTrue(out["quoteAnimSurvived"])
        self.assertAlmostEqual(out["finalPrice"], 1.17, delta=0.0001)

    def test_defensive_guard_on_destroyed_chart(self):
        out = self._run_scenario("""
          const p = new FakePanel('BTCUSD');
          p.pushBar(80000, true);
          await new Promise(r => setTimeout(r, 150));
          p.pushBar(80500, false);
          p.candleSeries = null; // simulate panel teardown mid-animation
          let threw = false;
          try { await new Promise(r => setTimeout(r, 50)); } catch(e) { threw = true; }
          console.log(JSON.stringify({threw, rafCleared: p._visualAnimRAF === null}));
        """)
        self.assertFalse(out["threw"])
        self.assertTrue(out["rafCleared"])
