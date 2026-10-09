# simulator/tests/test_mobile_sltp_timeframe_switch_window.py
"""
BLOQUE ACOTADO — Parte A: ventana de interacción SL/TP durante cambio/
carga de TEMPORALIDAD (timeframe), distinta de la ventana de cambio de
símbolo ya cubierta por test_mobile_sltp_symbol_scale_coherence.py.

Root cause (confirmed by the dedicated diagnostic in the previous
block, test_mobile_sltp_stale_history_ordering.py's Case B): clear()
only invalidated _historySymbol — it never itself re-rendered the
selected position's SL/TP section, and renderTfSelector()'s own click
handler (mobile.html) calls clear() + session.selectTimeframe() WITHOUT
ever calling refreshPositionLines() afterwards (unlike the watchlist's
symbol-switch handler, which does). So a timeframe-only switch left
the "+ TP"/"+ SL" button from BEFORE the switch sitting in the DOM,
still clickable, bound to the stale pos reference, until something
UNRELATED eventually re-rendered it.

Fix (mobile_chart.js only):
  - A pure client-side generation counter (_historyGeneration, bumped
    by clear()) paired with _readyGeneration (recorded by applyHistory()
    only when real data for the CURRENT generation lands). isSlTpEditableFor()
    now requires both the symbol string AND the generation to match —
    covers a timeframe-only switch (same symbol, generation moves on)
    without needing to separately track/compare timeframe strings at
    all.
  - clear() now calls _rerenderSelected() synchronously, so any stale
    control is replaced by the "Cargando gráfico…" pill IMMEDIATELY,
    not whenever the next unrelated render happens to occur.
  - The "+ Añadir" button's own onclick re-checks the gate directly
    (defense-in-depth) and refuses to start an edit if
    _defaultAddPrice() itself ever returned null (no pos.avg or any
    other substitute is used in that case).

No backend/WebSocket/trading_core/pricing/financial-calculation change.
Real-execution test, same established FakeWebSocket + real-source-dump
harness. No network, no real account, no server.
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

  const BTC = 'BTCUSD';

  function switchSymbol(sym){
    hooks.mobileChart.clear();
    hooks.session.selectSymbol(sym);
    hooks.refreshPositionLines();
  }
  function switchTimeframe(tf){
    // Exactly what renderTfSelector()'s own click handler does in the
    // real app — clear() then selectTimeframe(), deliberately WITHOUT
    // refreshPositionLines() (mobile.html does not call it there
    // either), so this faithfully reproduces the real code path, not
    // a more-defensive test-only sequence.
    hooks.mobileChart.clear();
    hooks.session.selectTimeframe(tf);
  }
  function scaleBars(base, n, tag){
    const bars = [];
    let seed = tag, price = base;
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
  function emitHistoryFor(sym, tf, bars, phase){
    ws().emit({ type: 'history', symbol: sym, timeframe: tf, phase: phase || 'complete', data: bars });
  }
  function emitPositions(items){ ws().emit({ type: 'positions', items: items }); }
  function sentUpdates(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:update'; }); }
  function addAffordance(field){ return document.querySelector('.mbgc-add-' + field); }
  function loadingPill(){
    const els = Array.from(document.querySelectorAll('.mbgc-pill.mbgc-add'));
    return els.find(function(el){ return el.textContent.indexOf('Cargando') !== -1; }) || null;
  }

  const B1 = { id: 'B1', symbol: BTC, side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  emitPositions([B1]);
  await sleep(20);

  // ───────────────────────────────────────────────────────────────
  // timeframe A listo -> cambio a B -> ANTES de recibir B, tocar el
  // control que pertenecía a A.
  // ───────────────────────────────────────────────────────────────
  switchSymbol(BTC);
  const tfA = hooks.session.currentTF; // '15m'
  emitHistoryFor(BTC, tfA, scaleBars(63000, 40, 1), 'complete');
  await sleep(60);
  assert(!!addAffordance('tp'), 'A listo: + TP visible para timeframe A (15m)');
  assert(hooks.mobileChart.isSlTpEditableFor(BTC), 'A listo: isSlTpEditableFor(BTCUSD) es verdadero');

  // Referencia al botón "+ TP" DE A, tomada ANTES del cambio — para
  // comprobar explícitamente que, aunque siguiera en el DOM, no
  // produce nada si se "toca" tras el cambio.
  const staleAddBtnFromA = addAffordance('tp');

  switchTimeframe('1h'); // cambio a B — exactamente como el selector real
  await sleep(20);

  const staleStillInDom = !!addAffordance('tp');
  const staleIsSameNode = staleStillInDom && addAffordance('tp') === staleAddBtnFromA;
  results.push({label:'tras cambiar a B: el control de A sigue en el DOM: ' + staleStillInDom + ' (mismo nodo: ' + staleIsSameNode + ')', pass:true});
  assert(!staleStillInDom, 'tras cambiar a B: el control "+ TP" de A ya NO está en el DOM (clear() re-renderizó de inmediato)');
  assert(!!loadingPill(), 'tras cambiar a B: se muestra "Cargando gráfico" de inmediato, sin esperar otro evento');
  assert(!hooks.mobileChart.isSlTpEditableFor(BTC), 'tras cambiar a B: isSlTpEditableFor ya es falso (generación invalidada)');

  const beforeSends = sentUpdates().length;
  const beforeEdit = hooks.mobileChart._edit;

  // "Tocar el control que pertenecía a A" — real hit-test en las
  // coordenadas donde ese control estaba (si algo sigue ahí, debe
  // fallar/no hacer nada; si ya no está, elementFromPoint resuelve a
  // cualquier otra cosa y el dispatch tampoco produce nada relevante).
  if (staleAddBtnFromA && document.body.contains(staleAddBtnFromA)) {
    realClick(staleAddBtnFromA);
  } else {
    const r = { x: 28, y: 40 }; // posición aproximada donde solían aparecer los add-affordances
    const target = document.elementFromPoint(r.x, r.y);
    if (target) target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:r.x, clientY:r.y}));
  }
  await sleep(20);

  assertEqual(hooks.mobileChart._edit, beforeEdit, 'tocar el control de A tras el cambio: NINGUNA previsualización/edición se inició (_edit sin cambios)');
  assertEqual(sentUpdates().length, beforeSends, 'tocar el control de A tras el cambio: 0 order:update enviados');
  assert(!document.querySelector('.mbgc-edit-confirm'), 'tocar el control de A tras el cambio: no aparece ningún toolbar de Confirmar');
  // Ningún precio derivado de pos.avg (63000) ni de la escala anterior
  // puede haberse calculado — no hay ningún input/preview visible con
  // valor alguno que inspeccionar, porque no se entró a modo edición.
  assert(!document.querySelector('.mbgc-edit-input'), 'tocar el control de A tras el cambio: no existe ningún input de precio provisional');

  // Intento directo de Confirmar durante la transición (API), para
  // cubrir también el camino de _confirmEdit() mismo, no solo la
  // ausencia de botón.
  hooks.mobileChart._confirmEdit();
  await sleep(10);
  assertEqual(sentUpdates().length, beforeSends, '_confirmEdit() directo durante la transición: tampoco envía nada (no hay _edit que confirmar)');

  // Llega correctamente B — la edición debe volver a habilitarse con
  // normalidad.
  emitHistoryFor(BTC, '1h', scaleBars(63000, 40, 2), 'complete');
  await sleep(60);
  assert(!!addAffordance('tp'), 'al llegar B: + TP vuelve a estar disponible');
  assert(hooks.mobileChart.isSlTpEditableFor(BTC), 'al llegar B: isSlTpEditableFor(BTCUSD) vuelve a ser verdadero');
  assert(!loadingPill(), 'al llegar B: "Cargando gráfico" ya no se muestra');

  realClick(addAffordance('tp'));
  await sleep(20);
  const confirmBtn = document.querySelector('.mbgc-edit-confirm');
  assert(!!confirmBtn, 'al llegar B: tocar + TP ahora sí abre el toolbar de edición');
  realClick(confirmBtn);
  await sleep(20);
  assertEqual(sentUpdates().length, beforeSends + 1, 'al llegar B: confirmar ahora sí envía exactamente un order:update');
  assertEqual(sentUpdates()[sentUpdates().length - 1].id, 'B1', 'al llegar B: el envío corresponde a B1');
"""


class TimeframeSwitchSlTpWindowTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_control_from_previous_timeframe_produces_nothing_during_transition(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "tf_window_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="tf_window_results",
        )
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(
            failures, [],
            f"{data['failCount']} of {len(data['results'])} timeframe-window assertions failed: {failures}",
        )
        self.assertGreaterEqual(data["passCount"], 15)
