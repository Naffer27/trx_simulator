# simulator/tests/test_fix05c_frontend_price_pnl_contract.py
"""
FIX-05C — Frontend Price / PnL / Reconnect Cleanup.

Textual/structural contract tests, same pattern already established by
FrontendStopoutWordingContractTests (test_stopout_liquidation_outcome_
integrity.py): no JS test runner exists in this repo, so these assert on
the literal template/consumer source rather than executing the JS. Each
assertion targets a specific, real, unique string in the shipped code —
not a loose regex — so a future edit that quietly reintroduces the audited
bug (history/candle as live authority, fabricated bid/ask, raw PnL
duplication, dead order price) fails loudly here.

Design lock: FIX-05C audit (this block). Contract:
  lastClose  = candle/history state only
  liveMid/bid/ask = real live quote state (tick-only, fail-closed on
                    source missing/"sim")
  PnL        = backend authoritative (computePositionPnLSafe everywhere)
"""
import inspect

from django.template.loader import get_template
from django.test import SimpleTestCase

from simulator.consumers import TradingConsumer


def _template_source():
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _slice(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i + len(start_marker))
    return src[i:j]


class LiveQuoteStateTests(SimpleTestCase):
    """1. liveMid state exists, distinct from lastClose."""

    def test_live_mid_state_exists_on_construction(self):
        src = _template_source()
        self.assertIn(
            "this.liveMid=null; this.liveSource=null; this.prevLiveMid=null;",
            src,
        )


class HistoryLiveAuthorityTests(SimpleTestCase):
    """2. history does not write liveMid; 17. cannot move the live price line."""

    def test_history_does_not_write_live_mid(self):
        src = _template_source()
        history_block = _slice(
            src,
            "if(msg.type==='history'&&Array.isArray(msg.data)){",
            "if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){",
        )
        self.assertNotIn("liveMid", history_block)

    def test_set_price_no_longer_touches_price_line(self):
        # setPrice() is what history/candle_update call with lastClose —
        # it must not be the writer of the LIVE current price line anymore.
        src = _template_source()
        set_price_block = _slice(
            src,
            "setPrice(px,prev){",
            "_updateLiveQuoteDisplay(){",
        )
        self.assertNotIn("this.priceLine", set_price_block)

    def test_live_quote_display_is_the_sole_price_line_writer(self):
        src = _template_source()
        self.assertIn("_updateLiveQuoteDisplay(){", src)
        live_block = _slice(
            src, "_updateLiveQuoteDisplay(){", "_hasLiveFinancialQuote(){"
        )
        self.assertIn("this.priceLine.applyOptions({price:px", live_block)
        self.assertIn("this.liveMid", live_block)


class CandleLiveAuthorityTests(SimpleTestCase):
    """3. candle_update/candle_new does not write liveMid or bid/ask."""

    def test_candle_update_does_not_write_live_mid(self):
        src = _template_source()
        candle_block = _slice(
            src,
            "if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){",
            "if(msg.type==='positions'){",
        )
        self.assertNotIn("liveMid", candle_block)
        # candle_update only READS bid/ask (to decide whether to refresh
        # the "—" display) — it must never WRITE (assign) them.
        self.assertNotIn("this.bid=_", candle_block)
        self.assertNotIn("this.bid=n(", candle_block)
        self.assertNotIn("this.ask=_", candle_block)
        self.assertNotIn("this.ask=n(", candle_block)
        # LIVE-CHART-RENDER-STABILITY-01 — the candle path's paint calls
        # are coalesced through _scheduleVisualRender('candle', ...) /
        # _flushVisualRender() rather than inlined here directly.
        self.assertIn("_scheduleVisualRender('candle', msg.type==='candle_new')", candle_block)
        # MASSIVE-CRYPTO-TRADE-CANDLES-01 — _updateBidAsk() now lives
        # exclusively in the QUOTE branch of _flushVisualRender() (gated
        # by the magnitude/max-age shouldPaint check), never in the
        # candle branch at all — candle_update/candle_new still never
        # writes or repaints bid/ask, the invariant asserted above holds
        # unchanged; the write-site just moved out of this handler
        # entirely (LIVE-CHART-RENDER-STABILITY-01), then out of the
        # candle branch specifically (this block).
        src = _template_source()
        flush_block = _slice(src, "_flushVisualRender(){", "_cancelPendingVisualRender(){")
        i_quote_branch = flush_block.index("if(this._quoteRenderPending){")
        candle_branch = flush_block[:i_quote_branch]
        quote_branch = flush_block[i_quote_branch:]
        self.assertNotIn("this._updateBidAsk();", candle_branch)
        self.assertIn("this._updateBidAsk();", quote_branch)
        self.assertIn("if(shouldPaint){", quote_branch)


class RealTickLiveAuthorityTests(SimpleTestCase):
    """4. a real tick sets liveMid/bid/ask; 6/7. sim/missing-source fail closed."""

    def _tick_block(self):
        src = _template_source()
        return _slice(
            src,
            "if(msg.type==='price'||msg.type==='tick'){",
            "if(msg.type==='history'&&Array.isArray(msg.data)){",
        )

    def test_real_tick_sets_live_mid_bid_ask(self):
        block = self._tick_block()
        self.assertIn("this.liveMid=(_a+_b)/2", block)
        self.assertIn("this.bid=_b;this.ask=_a;", block)
        # LIVE-CHART-RENDER-STABILITY-01 — the tick handler now schedules
        # the live-quote paint (coalesced to ~100ms) instead of calling it
        # inline. LIVE-CHART-SMOOTH-INTERPOLATION-01 — the actual paint,
        # one level down in _flushVisualRender()'s quote branch, is now
        # split into an instant badge/glow write and an animated
        # priceLine retarget (never a single _updateLiveQuoteDisplay()
        # call there anymore — that method is still used, unchanged, by
        # setActive()'s one-time immediate re-paint only).
        self.assertIn("_scheduleVisualRender('quote')", block)
        src = _template_source()
        flush_block = _slice(src, "_flushVisualRender(){", "_cancelPendingVisualRender(){")
        self.assertIn("this._paintBadgeAndGlow(current);", flush_block)
        self.assertIn("this._retargetPriceLineAnimation(current);", flush_block)

    def test_sim_tick_does_not_establish_live_quote(self):
        block = self._tick_block()
        self.assertIn("_src!=='sim'", block)

    def test_missing_source_fails_closed(self):
        block = self._tick_block()
        self.assertIn("_src!=null", block)

    def test_quotes_live_px_fed_from_live_mid_not_last_close(self):
        block = self._tick_block()
        self.assertIn("quotesLivePx[_qs]={price:this.liveMid,", block)


class TickSourcePropagationTests(SimpleTestCase):
    """5. backend price_tick() propagates source to the client payload."""

    def test_backend_sends_source_in_tick_payload(self):
        src = inspect.getsource(TradingConsumer.price_tick)
        self.assertIn(
            '"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, '
            '"time": ts, "source": source}',
            src,
        )
        self.assertIn('source  = event.get("source")', src)

    def test_gate_and_check_tp_sl_untouched(self):
        # FIX-05C must not alter the pre-existing financial gate.
        src = inspect.getsource(TradingConsumer.price_tick)
        self.assertIn('_price_source = event.get("source")', src)
        self.assertIn(
            'if _price_source is not None and _price_source != "sim":', src
        )


class ReconnectResetTests(SimpleTestCase):
    """8. onclose invalidates bid/ask/liveMid; onopen never restores them."""

    def test_onclose_resets_live_quote_state(self):
        src = _template_source()
        onclose_block = _slice(
            src, "this.ws.onclose=()=>{", "disconnect(){"
        )
        self.assertIn(
            "this.bid=null; this.ask=null; this.liveMid=null; "
            "this.liveSource=null; this.prevLiveMid=null;",
            onclose_block,
        )
        self.assertIn("this._updateBidAsk();", onclose_block)

    def test_onopen_does_not_restore_stale_quote(self):
        src = _template_source()
        onopen_block = _slice(
            src, "this.ws.onopen=()=>{", "this.ws.onmessage="
        )
        self.assertNotIn("liveMid", onopen_block)
        self.assertNotIn("this.bid=", onopen_block)


class SymbolTimeframeChangeTests(SimpleTestCase):
    """9. symbol change resets liveMid; 10. timeframe change does not."""

    def test_symbol_change_resets_live_mid(self):
        src = _template_source()
        sym_block = _slice(src, "_onSymChange(){", "_onTFChange(){")
        self.assertIn(
            "this.liveMid=null; this.liveSource=null; this.prevLiveMid=null;",
            sym_block,
        )

    def test_timeframe_change_does_not_reset_live_mid(self):
        src = _template_source()
        tf_block = _slice(src, "_onTFChange(){", "setStatus(t){")
        self.assertNotIn("liveMid", tf_block)
        self.assertNotIn("this.bid", tf_block)
        self.assertNotIn("this.ask", tf_block)


class NoFabricationTests(SimpleTestCase):
    """11. _updateBidAsk no longer fabricates bid/ask; 12. renders '—'."""

    def _bid_ask_block(self):
        src = _template_source()
        return _slice(src, "_updateBidAsk(){", "wsUrl(){")

    def test_no_synthetic_mid_spread_fabrication(self):
        block = self._bid_ask_block()
        self.assertNotIn("mid-s/2", block)
        self.assertNotIn("mid+s/2", block)
        self.assertNotIn("this.lastClose??", block)

    def test_no_tick_renders_unavailable_dash(self):
        block = self._bid_ask_block()
        self.assertIn("b.toFixed(pf):'—'", block)
        self.assertIn("a.toFixed(pf):'—'", block)
        # hasTick is exactly "real bid/ask present" now (no fallback path).
        self.assertIn(
            "const hasTick=this.bid!=null&&this.ask!=null&&this.ask>this.bid;",
            block,
        )
        self.assertIn("const b=hasTick?this.bid:null;", block)
        self.assertIn("const a=hasTick?this.ask:null;", block)


class SendOrderContractTests(SimpleTestCase):
    """13. order requires a live quote; 14. dead client price removed."""

    def _send_order_block(self):
        src = _template_source()
        return _slice(src, "sendOrder(side,qty,slV,tpV", "\n  }\n}")

    def test_send_order_requires_live_financial_quote(self):
        block = self._send_order_block()
        self.assertIn("!this._hasLiveFinancialQuote()", block)

    def test_send_order_payload_has_no_client_price(self):
        block = self._send_order_block()
        self.assertNotIn("price:this.lastClose", block)
        self.assertIn(
            "const payload={action:'order:new',symbol:this.currentSymbol,"
            "side,type,qty,sl:slV,tp:tpV};",
            block,
        )

    def test_has_live_financial_quote_helper_fail_closed(self):
        src = _template_source()
        helper_block = _slice(
            src, "_hasLiveFinancialQuote(){", "\n  }"
        )
        self.assertIn("this.liveSource!=null", helper_block)
        self.assertIn("this.liveSource!=='sim'", helper_block)
        self.assertIn("Number.isFinite(this.liveMid)", helper_block)


class PnLTitleContractTests(SimpleTestCase):
    """15/16. _updatePnLTitles uses computePositionPnLSafe, never raw lastClose."""

    def _pnl_titles_block(self):
        src = _template_source()
        return _slice(
            src, "_updatePnLTitles(){", "\n  }\n\n"
        )

    def test_update_pnl_titles_uses_backend_safe_pnl(self):
        block = self._pnl_titles_block()
        self.assertIn("computePositionPnLSafe(pos)", block)
        self.assertIn("this.positionsCache.find(", block)

    def test_update_pnl_titles_no_raw_compute_or_last_close(self):
        block = self._pnl_titles_block()
        self.assertNotIn("computeRawPnL(", block)
        self.assertNotIn("lastClose", block)


class EntrySlTpUnchangedTests(SimpleTestCase):
    """18. entry/SL/TP price lines still come from backend values, untouched."""

    def test_draw_lines_entry_source_unchanged(self):
        src = _template_source()
        self.assertIn(
            "const entry=Number(pos.avg??pos.entry??pos.price??this.lastClose);",
            src,
        )

    def test_sl_tp_from_backend_unchanged(self):
        src = _template_source()
        self.assertIn("if(pos.sl!=null){rec.sl=Number(pos.sl);", src)
        self.assertIn("if(pos.tp!=null){rec.tp=Number(pos.tp);", src)


class RiskPreviewUnchangedTests(SimpleTestCase):
    """19. FIX-01 risk preview payload not reopened/altered."""

    def test_risk_preview_payload_unchanged(self):
        src = _template_source()
        self.assertIn(
            "if(this.ws?.readyState===WebSocket.OPEN)this.ws.send(JSON."
            "stringify({action:'order:risk_preview',symbol:this.currentSymbol,qty}));",
            src,
        )


class StopoutWordingRegressionTests(SimpleTestCase):
    """20. pre-existing FIX-03/STOPOUT frontend wording untouched by FIX-05C."""

    def test_partial_wording_still_present(self):
        src = _template_source()
        self.assertIn("msg.partial", src)
        self.assertIn("STOP-OUT PARCIAL", src)

    def test_full_wording_still_unchanged(self):
        src = _template_source()
        self.assertIn("STOP-OUT — Posiciones liquidadas", src)
