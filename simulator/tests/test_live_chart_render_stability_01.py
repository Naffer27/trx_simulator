# simulator/tests/test_live_chart_render_stability_01.py
"""
LIVE-CHART-RENDER-STABILITY-01 — visual coalescing/throttle for the
tick and candle_update/candle_new message handlers in dashboard.html.

Manual acceptance of CRYPTO-QUOTE-DEDUP-01 confirmed the dedup itself
works (no more redundant broadcasts for an exact-repeat quote) but did
NOT fix the visual flicker: BTCUSD/ETHUSD still legitimately carry many
REAL, distinct price changes per second. This block addresses the
RENDER side only, entirely inside dashboard.html — a trailing-edge
THROTTLE (never a debounce) capping the expensive paint calls
(candleSeries.update()/volumeSeries.update()/priceLine/badge/bid-ask
DOM text) to roughly one flush per 100ms, while every tick/candle
message still updates financial/derived state (this.bid/ask/liveMid/
_bars/lastClose/prevClose, setPrice()'s PnL/RR titles) synchronously
and unconditionally, exactly as before.

No JS test runner exists in this project (package.json has no jest/
mocha/vitest — confirmed before writing this file) — same source-
inspection convention already established for every other dashboard.html
behavior test this session (SymbolTimeframeVisualResetTests,
PriceLineLifecycleTests, FrontendHistoryPhaseMergeTests, ...).
"""
import re

from django.test import SimpleTestCase


def _template_source() -> str:
    from django.template.loader import get_template
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _method_body(src: str, method_signature: str, max_len=None) -> str:
    """Bounded by the NEXT top-level (2-space-indented) method signature
    in the Panel class — never a fixed length guess (see this session's
    established convention: a fixed window silently truncates real code
    out as methods grow, or bleeds into unrelated later methods)."""
    i = src.index(method_signature)
    start = i + len(method_signature)
    m = re.search(r"\n  [A-Za-z_][A-Za-z0-9_]*\s*\(", src[start:])
    end = start + m.start() if m else (start + (max_len or 3500))
    return src[i:end]


class SchedulerStateAndThrottleTests(SimpleTestCase):
    """1. 100 ticks rápidos → renders fuertemente reducidos (throttle,
    not debounce): a timer already pending is never rescheduled."""

    def test_schedule_is_a_throttle_not_a_debounce(self):
        src = _template_source()
        body = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        # The early-return guard is what makes this a throttle: a second
        # (or 40th) call within the same window never resets/reschedules
        # the timer.
        self.assertIn("if(this._visualRenderTimer!=null)return;", body)
        self.assertIn("setTimeout(", body)
        self.assertIn(",100)", body)

    def test_schedule_marks_pending_flags_before_the_throttle_guard_check(self):
        src = _template_source()
        body = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        i_flag = body.index("Pending=true")
        i_guard = body.index("if(this._visualRenderTimer!=null)return;")
        self.assertLess(i_flag, i_guard)

    def test_tick_handler_calls_schedule_exactly_once_not_direct_paint(self):
        src = _template_source()
        i = src.index("if(msg.type==='price'||msg.type==='tick'){")
        j = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){", i)
        body = src[i:j]
        self.assertEqual(body.count("_scheduleVisualRender('quote')"), 1)
        # The old direct-paint calls must be gone from this handler —
        # replaced by the throttle, not called alongside it.
        self.assertNotIn("this._updateBidAsk();\n        this._updateLiveQuoteDisplay();", body)

    def test_candle_handler_calls_schedule_exactly_once_not_direct_paint(self):
        src = _template_source()
        i = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){")
        j = src.index("if(msg.type==='positions'){", i)
        body = src[i:j]
        self.assertEqual(body.count("_scheduleVisualRender('candle', msg.type==='candle_new')"), 1)
        self.assertNotIn("this.candleSeries.update(b);this.volumeSeries.update(volPointForBar(b));", body)


class AlwaysPaintsLatestStateTests(SimpleTestCase):
    """2. Siempre se pinta el último precio recibido — flush re-reads
    current state, never a snapshot captured at schedule time."""

    def test_flush_rereads_last_bar_from_bars_array(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        self.assertIn("this._bars[this._bars.length-1]", body)

    def test_flush_paints_current_live_mid_via_update_live_quote_display(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        # this.liveMid is read fresh, AT FLUSH TIME (never a captured
        # snapshot from schedule time), into `current` — the same
        # guarantee as before, just via a local var LIVE-CHART-SMOOTH-
        # INTERPOLATION-01 introduced so both the instant badge/glow
        # paint and the animated priceLine retarget agree on one value.
        self.assertIn("const current=this.liveMid;", body)
        self.assertIn("this._paintBadgeAndGlow(current);", body)
        self.assertIn("this._retargetPriceLineAnimation(current);", body)


class UnconditionalStateWriteTests(SimpleTestCase):
    """3. La última candle conserva el OHLC correcto — state (_bars,
    lastClose, setPrice()) is written unconditionally BEFORE the
    (possibly deferred) paint is scheduled, never inside the coalesced
    path itself. Also covers: financial state (PnL/RR via setPrice(),
    bid/ask/liveMid) is never gated by the throttle — explicit NO-TOCAR
    PnL/margin/execution scope."""

    def test_candle_state_written_before_schedule_call(self):
        src = _template_source()
        i = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){")
        j = src.index("if(msg.type==='positions'){", i)
        body = src[i:j]
        i_bars_write = body.index("this._bars.push(b)")
        i_setprice = body.index("this.setPrice(")
        i_schedule = body.index("_scheduleVisualRender(")
        self.assertLess(i_bars_write, i_schedule)
        self.assertLess(i_setprice, i_schedule)

    def test_setprice_not_inside_flush(self):
        # setPrice() (PnL/RR titles) must stay on the immediate/
        # unconditional path — explicitly out of this block's scope.
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        self.assertNotIn("setPrice(", body)

    def test_tick_state_written_before_schedule_call(self):
        src = _template_source()
        i = src.index("if(msg.type==='price'||msg.type==='tick'){")
        j = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){", i)
        body = src[i:j]
        i_state = body.index("this.liveMid=(_a+_b)/2")
        i_schedule = body.index("_scheduleVisualRender(")
        self.assertLess(i_state, i_schedule)


class SameCycleTests(SimpleTestCase):
    """4. priceLine + badge + candle se actualizan juntos — both paint
    branches run inside the SAME synchronous flush call, no nested
    async/setTimeout between them."""

    def test_flush_paints_candle_and_quote_in_one_synchronous_call(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        self.assertIn("this.candleSeries.update(", body)
        self.assertIn("this._paintBadgeAndGlow(current);", body)
        self.assertIn("this._retargetPriceLineAnimation(current);", body)
        self.assertNotIn("setTimeout(", body)
        self.assertNotIn("await ", body)


class SymbolSwitchCancelsPendingTests(SimpleTestCase):
    """5. symbol switch descarta pending del símbolo anterior."""

    def test_on_sym_change_cancels_pending_render(self):
        src = _template_source()
        body = _method_body(src, "_onSymChange(){")
        self.assertIn("this._cancelPendingVisualRender();", body)
        # Must happen before _bars is reset, i.e. before anything the
        # (now cancelled) pending render could have re-read.
        self.assertLess(
            body.index("this._cancelPendingVisualRender();"),
            body.index("this._bars=[];"),
        )

    def test_cancel_visual_render_clears_timer_and_both_flags(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingVisualRender(){")
        self.assertIn("clearTimeout(this._visualRenderTimer)", body)
        self.assertIn("this._visualRenderTimer=null", body)
        self.assertIn("this._quoteRenderPending=false", body)
        self.assertIn("this._candleRenderPending=false", body)


class TimeframeSwitchCancelsOnlyCandleTests(SimpleTestCase):
    """6. timeframe switch descarta pending candle viejo — but the live
    quote (priceLine/badge) stays valid across a TF-only change, matching
    the pre-existing liveMid/bid/ask contract for _onTFChange()."""

    def test_on_tf_change_cancels_only_candle_render(self):
        src = _template_source()
        body = _method_body(src, "_onTFChange(){")
        self.assertIn("this._cancelPendingCandleRender();", body)
        self.assertNotIn("_cancelPendingVisualRender()", body)

    def test_cancel_candle_render_does_not_touch_timer_or_quote_flag(self):
        src = _template_source()
        body = _method_body(src, "_cancelPendingCandleRender(){")
        self.assertNotIn("_visualRenderTimer", body)
        self.assertNotIn("_quoteRenderPending", body)
        self.assertIn("this._candleRenderPending=false", body)

    def test_on_tf_change_still_leaves_live_quote_state_untouched(self):
        # Pre-existing contract (GOLDEN-MARKETDATA-CRYPTO-01) — this
        # block must not have broken it.
        src = _template_source()
        body = _method_body(src, "_onTFChange(){")
        self.assertNotIn("this.liveMid=null", body)
        self.assertNotIn("this.bid=this.ask=null", body)


class ReconnectDoesNotDuplicateSchedulerTests(SimpleTestCase):
    """7. reconnect no duplica scheduler."""

    def test_onclose_cancels_pending_render(self):
        src = _template_source()
        i = src.index("this.ws.onclose=()=>{")
        j = src.index("};", i)
        body = src[i:j]
        self.assertIn("this._cancelPendingVisualRender();", body)
        # Must run before the immediate (unthrottled) disconnect repaint,
        # so a stray already-scheduled flush can never fire afterward and
        # overwrite it.
        self.assertLess(
            body.index("this._cancelPendingVisualRender();"),
            body.index("this._updateBidAsk();"),
        )

    def test_schedule_itself_is_idempotently_guarded(self):
        # Belt-and-suspenders: even without onclose's explicit cancel, a
        # stray call could never create a second concurrent timer.
        src = _template_source()
        body = _method_body(src, "_scheduleVisualRender(kind, isNew){")
        self.assertEqual(body.count("setTimeout("), 1)


class SymbolAgnosticMechanismTests(SimpleTestCase):
    """8/9. Forex y Crypto funcionan igual — the coalescing mechanism
    itself has zero symbol-specific branching; it operates purely on
    this.*/this._bars, the same for every symbol."""

    def test_no_symbol_literals_in_scheduler_methods(self):
        src = _template_source()
        for sig in (
            "_scheduleVisualRender(kind, isNew){",
            "_flushVisualRender(){",
            "_cancelPendingVisualRender(){",
            "_cancelPendingCandleRender(){",
        ):
            body = _method_body(src, sig)
            for literal in ("BTCUSD", "ETHUSD", "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"):
                self.assertNotIn(literal, body, f"{sig} must stay symbol-agnostic (found {literal!r})")


class HistoryInstantLoadIntactTests(SimpleTestCase):
    """10. History instant-load (CHART-HISTORY-INSTANT-LOAD-01) permanece
    intacto — byte-for-byte regression check that this block never
    touched that handler."""

    def test_phase_merge_block_unchanged(self):
        src = _template_source()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        j = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){", i)
        body = src[i:j]
        self.assertIn("(msg.phase||'complete')==='complete'", body)
        self.assertIn("this._bars[0].time", body)
        self.assertIn("b.time<earliest", body)
        self.assertIn("older.concat(this._bars)", body)
        self.assertIn("this.chart.timeScale().fitContent();", body)
        # This block must not have introduced any coalescing into history
        # loading — history is a one-off event, never tick-driven.
        self.assertNotIn("_scheduleVisualRender", body)
        self.assertNotIn("_flushVisualRender", body)


class NoTouchScopeTests(SimpleTestCase):
    """Explicit NO-TOCAR: nothing about candle bucketing/timeframe/spread/
    setPrice's PnL surface was pulled into the coalescing path."""

    def test_flush_never_touches_agg_or_spread(self):
        src = _template_source()
        body = _method_body(src, "_flushVisualRender(){")
        self.assertNotIn("_resetAgg", body)
        self.assertNotIn("_recomputeSpread", body)

    def test_schedule_and_flush_never_touch_the_websocket(self):
        src = _template_source()
        for sig in ("_scheduleVisualRender(kind, isNew){", "_flushVisualRender(){"):
            body = _method_body(src, sig)
            self.assertNotIn("this.ws.send", body)
