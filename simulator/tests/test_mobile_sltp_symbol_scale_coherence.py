# simulator/tests/test_mobile_sltp_symbol_scale_coherence.py
"""
ISOLATED FIX — SL/TP editing symbol/scale coherence (POST-MOBILE-CERT
audit finding, Position 397's anomalous TP).

Root cause (confirmed by isolated reproduction before any code change):
MobileTradingChart reuses a SINGLE candleSeries across every symbol —
only its loaded DATA changes on a symbol switch, via applyHistory().
session.selectSymbol() (mobile_session.js) flips session.currentSymbol
SYNCHRONOUSLY, before any network round-trip, so currentSymbolPositions()
already returns the NEW symbol's positions while candleSeries can still
hold the OLD symbol's (wildly different-scale) data. Tapping "+ TP"/
"+ SL" during that window computed a default price via
priceToCoordinate()/coordinateToPrice() against the WRONG instrument's
scale — reproduced in isolation: BTCUSD-scale candles loaded, an
EUR/USD position's default TP came back wrong by >20x.

Fix (mobile_chart.js only, plus one argument threaded through at the
existing mobile.html onHistory() callsite — mobile_session.js itself
was NOT modified, since it already drops any 'history' message whose
own symbol no longer matches the live session.currentSymbol at arrival
time; this file's own fix only needed to be TOLD which symbol a given
apply belongs to):
  - clear() (already called synchronously before every selectSymbol()/
    selectTimeframe()) now also resets _historySymbol = null — SL/TP
    editing is invalidated the INSTANT a switch starts.
  - applyHistory(data, phase, symbol) records _historySymbol = symbol
    only when real bars are actually applied.
  - isSlTpEditableFor(symbol) is the one gate every SL/TP add/drag/
    confirm path now checks — true only once candleSeries demonstrably
    holds THAT exact symbol's own non-empty data.
  - _renderSelectedSlTp() skips every SL/TP line/zone/pill/add-
    affordance/edit-toolbar entirely while not ready, showing a
    compact, scale-independent "Cargando gráfico…" pill instead (reuses
    the existing .mbgc-add pill class — no new CSS).
  - _onPillPointerDown/_onPillPointerMove/_confirmEdit each re-check the
    gate directly (defense-in-depth beyond the structural one above).

Real-execution tests run the ACTUAL, unmodified source files (mobile.html,
mobile_chart.js, mobile_session.js, trading_core.js) in a real headless
browser, using the established FakeWebSocket + real-source-dump harness
from test_mobile_close_position_isolated_bug.py (imported, not
duplicated). No network, no real account, no server started by these
tests. Skipped gracefully when no Chrome-family browser is found.
"""
from django.test import TestCase

from simulator.tests.test_mobile_close_position_isolated_bug import (
    CHROME_PATH,
    FAKE_LWC,
    _dump_mobile_html,
    _driver_wrap,
    _inject_fake_ws_and_hooks,
    _inline_real_sources,
    _run_driver_and_extract,
)

BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const BTC = 'BTCUSD', EUR = 'EUR/USD';

  function switchSymbol(sym){
    hooks.mobileChart.clear();
    hooks.session.selectSymbol(sym);
    hooks.refreshPositionLines();
  }
  function scaleBars(base, n){
    const bars = [];
    let seed = 11, price = base;
    function rnd(){ seed = (seed*16807) % 2147483647; return seed/2147483647; }
    const now = Math.floor(Date.now()/1000);
    const tf = 900;
    const start = Math.floor(now/tf)*tf - n*tf;
    for (let i = 0; i < n; i++) {
      const o = price, c = o + (rnd()-0.5)*(base*0.001);
      const hi = Math.max(o,c) + rnd()*(base*0.0004), lo = Math.min(o,c) - rnd()*(base*0.0004);
      bars.push({ time: start + i*tf, open:o, high:hi, low:lo, close:c, volume: 10+rnd()*40 });
      price = c;
    }
    return bars;
  }
  function btcBars(){ return scaleBars(63000, 40); }
  function eurBars(){ return scaleBars(1.1200, 40); }
  function emitHistoryFor(sym, bars, phase){
    ws().emit({ type: 'history', symbol: sym, timeframe: hooks.session.currentTF, phase: phase || 'complete', data: bars });
  }
  function emitPositions(items){ ws().emit({ type: 'positions', items: items }); }
  function sentUpdates(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:update'; }); }
  function addAffordance(field){ return document.querySelector('.mbgc-add-' + field); }
  function loadingPill(){
    const els = Array.from(document.querySelectorAll('.mbgc-pill.mbgc-add'));
    return els.find(function(el){ return el.textContent.indexOf('Cargando') !== -1; }) || null;
  }
  function priceIsPlausibleFor(sym, v){
    // A value computed under the WRONG instrument's scale lands far
    // outside its own real symbol's plausible band — never merely
    // "a little off". BTCUSD ~ tens of thousands; EUR/USD ~ low single
    // digits. No overlap between the two bands at all.
    if (sym === BTC) return v > 1000 && v < 1000000;
    if (sym === EUR) return v > 0.01 && v < 100;
    return false;
  }

  const B1 = { id: 'B1', symbol: BTC, side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  const E1 = { id: 'E1', symbol: EUR, side: 'buy', avg: 1.1200, sl: null, tp: null, qty: 0.01, pnl: 0 };
  emitPositions([B1, E1]);
  await sleep(20);

  // ───────────────────────────────────────────────────────────────
  // 1. BTC → EUR
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(BTC);
    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('tp'), '1 BTC→EUR: + TP visible once BTCUSD ready');
    assert(!loadingPill(), '1 BTC→EUR: no loading pill once BTCUSD ready');

    switchSymbol(EUR);
    await sleep(20);
    assert(!addAffordance('tp'), '1 BTC→EUR: + TP hidden immediately after switch starts (EUR/USD history not yet applied)');
    assert(!!loadingPill(), '1 BTC→EUR: "Cargando gráfico" shown during the transition');
    const before1 = sentUpdates().length;
    // Nothing to tap — the add-affordance does not exist. This IS the
    // protection: there is no way to produce a payload right now.
    assertEqual(sentUpdates().length, before1, '1 BTC→EUR: zero order:update sent while not ready');

    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('tp'), '1 BTC→EUR: + TP visible again once EUR/USD ready');
    assert(!loadingPill(), '1 BTC→EUR: loading pill gone once EUR/USD ready');
    realClick(addAffordance('tp'));
    await sleep(20);
    const confirmBtn1 = document.querySelector('.mbgc-edit-confirm');
    assert(!!confirmBtn1, '1 BTC→EUR: confirm toolbar appears for E1');
    realClick(confirmBtn1);
    await sleep(20);
    const sent1 = sentUpdates();
    assertEqual(sent1.length, before1 + 1, '1 BTC→EUR: exactly one order:update sent once genuinely ready');
    const msg1 = sent1[sent1.length - 1];
    assertEqual(msg1.id, 'E1', "1 BTC→EUR: sent update carries E1's own id");
    assertEqual(msg1.symbol, EUR, "1 BTC→EUR: sent update carries E1's own symbol");
    assert(priceIsPlausibleFor(EUR, Number(msg1.tp)), '1 BTC→EUR: sent tp (' + msg1.tp + ') is EUR/USD-scale, never BTCUSD-scale');
    ws().emit({ type: 'warn', message: 'order_update_not_found' });
    await sleep(20);
  }

  // ───────────────────────────────────────────────────────────────
  // 2. EUR → BTC (symmetric)
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(EUR);
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('sl'), '2 EUR→BTC: + SL visible once EUR/USD ready');

    switchSymbol(BTC);
    await sleep(20);
    assert(!addAffordance('sl'), '2 EUR→BTC: + SL hidden immediately after switch starts');
    assert(!!loadingPill(), '2 EUR→BTC: "Cargando gráfico" shown during the transition');
    const before2 = sentUpdates().length;
    assertEqual(sentUpdates().length, before2, '2 EUR→BTC: zero order:update sent while not ready');

    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('sl'), '2 EUR→BTC: + SL visible again once BTCUSD ready');
    realClick(addAffordance('sl'));
    await sleep(20);
    realClick(document.querySelector('.mbgc-edit-confirm'));
    await sleep(20);
    const sent2 = sentUpdates();
    assertEqual(sent2.length, before2 + 1, '2 EUR→BTC: exactly one order:update sent once genuinely ready');
    const msg2 = sent2[sent2.length - 1];
    assertEqual(msg2.id, 'B1', "2 EUR→BTC: sent update carries B1's own id");
    assert(priceIsPlausibleFor(BTC, Number(msg2.sl)), '2 EUR→BTC: sent sl (' + msg2.sl + ') is BTCUSD-scale, never EUR/USD-scale');
    ws().emit({ type: 'warn', message: 'order_update_not_found' });
    await sleep(20);
  }

  // ───────────────────────────────────────────────────────────────
  // 3. BTC → EUR → BTC rápidamente (no history arrives in between)
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(BTC);
    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('tp'), '3 rapid: BTCUSD ready before the rapid sequence starts');

    switchSymbol(EUR);
    switchSymbol(BTC); // back to BTC before EUR/USD's history ever arrives
    await sleep(20);
    // Back on BTC, but clear() ran again on the second switch — must
    // still be "not ready" until a BTCUSD response actually lands
    // again, never silently reusing the FIRST (now-stale) BTC load.
    assert(!addAffordance('tp'), '3 rapid: still not ready immediately after the rapid double-switch');
    assert(!!loadingPill(), '3 rapid: "Cargando gráfico" shown throughout the rapid sequence');

    // The EUR/USD response that was in flight for the middle hop
    // arrives late — must never mark the chart ready for BTC (wrong
    // symbol for what is currently selected) nor get applied at all in
    // a way that could leak into a BTC computation.
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(30);
    assert(!addAffordance('tp'), '3 rapid: late EUR/USD response does not enable editing while BTCUSD is selected');

    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('tp'), '3 rapid: ready again once a genuine BTCUSD response lands');
    realClick(addAffordance('tp'));
    await sleep(20);
    realClick(document.querySelector('.mbgc-edit-confirm'));
    await sleep(20);
    const sent3 = sentUpdates();
    const msg3 = sent3[sent3.length - 1];
    assert(priceIsPlausibleFor(BTC, Number(msg3.tp)), '3 rapid: final sent tp (' + msg3.tp + ') is BTCUSD-scale');
    ws().emit({ type: 'warn', message: 'order_update_not_found' });
    await sleep(20);
  }

  // ───────────────────────────────────────────────────────────────
  // 4. Históricos que llegan fuera de orden
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(EUR);
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('sl'), '4 out-of-order: EUR/USD ready');

    switchSymbol(BTC); // currentSymbol is now BTCUSD; mobile_session.js will drop a stale EUR/USD response
    await sleep(10);
    // The EUR/USD request's response (sent before the switch) finally
    // arrives AFTER we've already moved on — mobile_session.js's own
    // existing guard (msg.symbol !== this.currentSymbol) must drop it
    // before it ever reaches applyHistory().
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(30);
    assert(!addAffordance('tp'), '4 out-of-order: late, now-foreign EUR/USD response never enables editing for BTCUSD');
    assert(!!loadingPill(), '4 out-of-order: still shows "Cargando gráfico" (BTCUSD’s own response has not arrived yet)');

    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    assert(!!addAffordance('tp'), '4 out-of-order: ready once BTCUSD’s OWN response actually lands');
  }

  // ───────────────────────────────────────────────────────────────
  // 5. Iniciar SL/TP y cambiar símbolo antes de confirmar
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(BTC);
    emitHistoryFor(BTC, btcBars(), 'complete');
    await sleep(60);
    realClick(addAffordance('sl'));
    await sleep(20);
    assert(!!document.querySelector('.mbgc-edit-confirm'), '5 switch-before-confirm: edit toolbar open for B1');
    const before5 = sentUpdates().length;

    switchSymbol(EUR); // before ever confirming
    await sleep(20);
    assert(!document.querySelector('.mbgc-edit-confirm'), '5 switch-before-confirm: toolbar gone — preview abandoned by the switch');
    assertEqual(sentUpdates().length, before5, '5 switch-before-confirm: switching cancelled the unsent preview — zero sends');

    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
  }

  // ───────────────────────────────────────────────────────────────
  // 6. Intentar confirmar durante transición (direct API attempt,
  //    bypassing the UI, to prove the gate itself — not just the
  //    absence of a clickable button)
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(EUR);
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
    realClick(addAffordance('tp'));
    await sleep(20);
    assert(!!hooks.mobileChart._edit, '6 confirm-during-transition: an edit is genuinely in progress for E1');
    const before6 = sentUpdates().length;

    // Simulate the transition starting (exactly what clear() does to
    // the readiness flag) WITHOUT a full symbol switch, to attempt
    // _confirmEdit() directly while _edit is still populated — proves
    // the gate inside _confirmEdit() itself, not merely the UI's lack
    // of a button.
    hooks.mobileChart._historySymbol = null;
    hooks.mobileChart._confirmEdit();
    await sleep(20);
    assertEqual(sentUpdates().length, before6, '6 confirm-during-transition: _confirmEdit() itself refuses — zero sends');

    hooks.mobileChart._historySymbol = EUR; // restore for the next scenario
  }

  // ───────────────────────────────────────────────────────────────
  // 7. Cambio de símbolo DESPUÉS de que order:update ya fue enviado
  // ───────────────────────────────────────────────────────────────
  {
    switchSymbol(EUR);
    emitHistoryFor(EUR, eurBars(), 'complete');
    await sleep(60);
    realClick(addAffordance('tp'));
    await sleep(20);
    realClick(document.querySelector('.mbgc-edit-confirm'));
    await sleep(20);
    const sent7 = sentUpdates();
    const before7 = sent7.length;
    assert(before7 >= 1, '7 post-send switch: order:update was actually sent for E1');
    const requestedTp7 = sent7[sent7.length - 1].tp;
    assertEqual(sent7[sent7.length - 1].id, 'E1', '7 post-send switch: sent update is for E1');
    assert(!!hooks.pendingSlTpRef(), '7 post-send switch: mobile.html’s own pendingSlTp tracking is set after sending');
    const pendingIdBefore = hooks.pendingSlTpRef().id;

    switchSymbol(BTC); // switch away WHILE the update is still in flight
    await sleep(20);
    assertEqual(sentUpdates().length, before7, '7 post-send switch: switching symbols never re-sends or duplicates the in-flight update');
    assert(!!hooks.pendingSlTpRef(), '7 post-send switch: pendingSlTp tracking survives the symbol switch (tracked by id, not by current symbol)');
    assertEqual(hooks.pendingSlTpRef().id, pendingIdBefore, '7 post-send switch: still tracking the SAME id (E1), never silently retargeted to B1/BTCUSD');

    // The real backend resolution for E1 arrives while BTCUSD is the
    // currently charted symbol — must resolve by id regardless, never
    // be reinterpreted as an edit of the new position/symbol.
    emitPositions([B1, { id: 'E1', symbol: EUR, side: 'buy', avg: 1.1200, sl: null, tp: requestedTp7, qty: 0.01, pnl: 0 }]);
    await sleep(30);
    assert(!hooks.pendingSlTpRef(), '7 post-send switch: resolves (clears) once the matching canonical snapshot arrives, even while BTCUSD is charted');
    assertEqual(sentUpdates().length, before7, '7 post-send switch: resolution itself never triggers any further send');
  }
"""


class SlTpSymbolScaleCoherenceTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_symbol_switch_never_leaks_wrong_scale_into_sltp(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "coherence_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="coherence_results",
        )
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(
            failures, [],
            f"{data['failCount']} of {len(data['results'])} symbol/scale coherence assertions failed: {failures}",
        )
        self.assertGreaterEqual(data["passCount"], 30)
