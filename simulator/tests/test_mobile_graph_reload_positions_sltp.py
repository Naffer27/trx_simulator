# simulator/tests/test_mobile_graph_reload_positions_sltp.py
"""
MOBILE GRÁFICO — FIX DIRIGIDO POST-COMMIT (checkpoint de10f822...).

Corrige dos defectos de coordinación/render, exclusivamente en Mobile,
reproducidos primero en aislamiento antes de cualquier cambio de
producción (ver el informe entregado junto a este archivo):

  1. RELOAD / POSICIONES — causa demostrada: la ÚNICA vía que existía
     para fijar session.currentSymbol era un tap del watchlist
     (selectSymbol()). Tras una recarga completa eso nunca ocurre por
     sí solo, así que currentSymbolPositions() (mobile.html) filtraba
     siempre contra null — ninguna posición abierta podía dibujarse,
     sin importar en qué orden llegaran 'positions'/'history'. "Volvía
     a aparecer al abrir otra operación" solo porque hacerlo exige
     pasar primero por el watchlist (selectSymbol()), nunca por el
     order:new en sí. Fix (mobile.html::onPositions()): si todavía no
     hay símbolo elegido y el snapshot canónico REAL ya trae alguna
     posición abierta, se autoselecciona el símbolo de la primera —
     mismo selectSymbol()/change_symbol/load_history que el watchlist
     ya usa, nunca un mensaje nuevo, nunca una posición inventada.

  2. DISPONIBILIDAD SL/TP — causa demostrada (distinta de un fallo en
     el propio gate isSlTpEditableFor(), que ya funcionaba
     correctamente en el caso limpio — ver
     test_new_confirmed_position_already_ready_no_delay más abajo):
     setPositions()/applyHistory() podían recibir verdad canónica real
     MIENTRAS MobileTradingChart.initialize() seguía resolviendo de
     forma asíncrona (candleSeries/chart aún null — un hueco real en
     conexiones/dispositivos lentos, no hipotético: initialize() hace
     await de un <script> CDN si window.LightweightCharts no está
     listo). setPositions() descartaba esa verdad en silencio (ni
     siquiera la recordaba); nada volvía a consumirla una vez el chart
     quedaba listo. Fix: setPositions() ahora SIEMPRE conserva
     _lastPositions/_selectedId (solo omite el dibujo si candleSeries
     no existe aún, igual que applyHistory() ya hacía con bars/
     _historySymbol); initialize() reproduce esa verdad ya retenida
     (bars + _lastPositions) exactamente una vez, en cuanto
     candleSeries/volumeSeries pasan a ser reales — sin ningún evento
     nuevo, sin ninguna llamada de red adicional.

Ambos fixes son exclusivamente de Mobile (mobile_chart.js/mobile.html).
Ninguna superficie protegida (consumers.py, Desktop, trading_core.js,
modelos, migraciones, contratos WebSocket, motor financiero) fue
tocada. Real-execution tests, mismo harness establecido (FakeWebSocket
+ dump real de las fuentes en Chrome headless, FAKE_LWC determinista)
de test_mobile_close_position_isolated_bug.py.
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


def _bars_js(base="63000", n=30, seed=1):
    return (
        "(function(){ const bars=[]; const now=Math.floor(Date.now()/1000); const tf=900;"
        f" let price={base};"
        " for(let i=0;i<" + str(n) + ";i++){ bars.push({time: now-(" + str(n) + "-i)*tf,"
        " open:price, high:price+10, low:price-10, close:price, volume:10}); }"
        " return bars; })()"
    )


# 1 — posición preexistente → reload completo → aparece automáticamente
# sin nueva operación. Nunca se llama a selectSymbol() manualmente.
TEST1_RELOAD = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  assertEqual(hooks.session.currentSymbol, null, '1: precondición — ningún símbolo elegido tras connect(), sin ningún tap previo');

  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: 62000, tp: 64000, qty: 0.01, pnl: 1.23 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(30);

  assertEqual(hooks.session.currentSymbol, 'BTCUSD', '1: el símbolo se autoselecciona desde el snapshot canónico real, sin ningún tap manual');
  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '1: B1 se dibuja automáticamente tras el reload, sin BUY/SELL ni evento artificial');
  assertEqual(hooks.mobileChart._selectedId, 'B1', '1: B1 queda seleccionado automáticamente');

  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'initial', data: """ + _bars_js() + """ });
  await sleep(30);
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '1: SL/TP se habilita con normalidad en cuanto llega history real (el autoselect no inventó una escala)');
  assert(!!document.querySelector('.mbgc-pill-sl'), '1: el pill real de SL (62000) de B1 queda visible');
"""

# 2 — positions llega antes de history → al quedar chart válido
# (history aplicado) aparecen overlays automáticamente.
TEST2_POSITIONS_BEFORE_HISTORY = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(20);

  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '2: Entry de B1 ya dibujada (no depende de history)');
  assert(!hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '2: SL/TP aún bloqueado — history todavía no llegó');
  assert(!!document.querySelector('.mbgc-pill.mbgc-add') && document.querySelector('.mbgc-pill.mbgc-add').textContent.indexOf('Cargando')!==-1, '2: "Cargando gráfico" visible mientras se espera history');

  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'initial', data: """ + _bars_js() + """ });
  await sleep(30);

  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '2: al llegar history, SL/TP se habilita automáticamente');
  assert(!!document.querySelector('.mbgc-add-tp'), '2: "+ TP" disponible automáticamente, sin ningún evento adicional');
"""

# 3 — history llega antes de positions (símbolo ya vigente, p.ej. tras
# una reconexión) → al llegar positions aparecen overlays
# automáticamente.
TEST3_HISTORY_BEFORE_POSITIONS = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);

  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'initial', data: """ + _bars_js() + """ });
  await sleep(20);
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '3: chart listo ANTES de que exista ninguna posición');
  assertEqual(Object.keys(hooks.mobileChart._entryLines||{}).length, 0, '3: ninguna Entry dibujada todavía (no hay posiciones)');

  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(20);

  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '3: al llegar positions, B1 se dibuja automáticamente');
  assert(!!document.querySelector('.mbgc-add-tp'), '3: "+ TP" disponible de inmediato (el chart ya estaba listo)');
"""

# 4 — inicialización lenta del chart (candleSeries/chart aún no
# existen cuando llega la verdad canónica) → el estado converge y
# aparecen posiciones + SL/TP real, sin ningún evento nuevo, en cuanto
# el chart termina de inicializarse.
TEST4_SLOW_CHART_INIT = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  // Reproduce fielmente el hueco async real de initialize() (CDN
  // lento): candleSeries/chart aún no existen cuando llega la verdad
  // canónica.
  hooks.mobileChart.chart = null;
  hooks.mobileChart.candleSeries = null;
  hooks.mobileChart.volumeSeries = null;

  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: 62000, tp: 64000, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(10);
  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'complete', data: """ + _bars_js() + """ });
  await sleep(10);

  assertEqual(Object.keys(hooks.mobileChart._entryLines||{}).length, 0, '4: nada dibujado todavía (candleSeries no existe) — pero los datos no deben perderse');
  assertEqual(hooks.mobileChart._lastPositions.length, 1, '4: _lastPositions retiene B1 aunque el chart no esté listo (fix de setPositions())');
  assertEqual(hooks.mobileChart.bars.length, 30, '4: bars retiene el history aunque el chart no esté listo (ya existente en applyHistory())');

  // El chart "termina de inicializarse" — SIN ningún evento nuevo.
  await hooks.mobileChart.initialize();
  await sleep(20);

  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '4: B1 converge automáticamente en cuanto el chart queda listo');
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '4: SL/TP disponible automáticamente tras la convergencia');
  assert(!!document.querySelector('.mbgc-pill-sl'), '4: pill real de SL visible tras la convergencia, sin ningún evento nuevo');
"""

# 5 — gráfico ya listo → apertura CONFIRMADA simulada/aislada (solo el
# snapshot 'positions' que el backend real empuja tras un order:new ya
# resuelto — esta prueba no envía ninguna orden) → posición y SL/TP
# disponibles sin esperar ningún evento innecesario (ni history
# 'complete', ni otro snapshot, ni otro tick).
TEST5_NEW_CONFIRMED_POSITION_NO_DELAY = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  // Solo phase 'initial' — nunca 'complete' — para demostrar que no se
  // espera innecesariamente a 'complete'.
  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'initial', data: """ + _bars_js() + """ });
  await sleep(20);
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '5: precondición — chart ya listo SOLO con phase initial (demuestra que initial basta)');

  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(10);

  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '5: posición recién confirmada dibujada de inmediato');
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '5: SL/TP disponible de inmediato, sin esperar ningún evento adicional');
  assert(!!document.querySelector('.mbgc-add-tp'), '5: "+ TP" visible de inmediato');
"""

# 6 — reconexión con posiciones existentes → mientras no hay escala
# válida, SL/TP bloqueado; al llegar histórico válido actual,
# posiciones y controles se reconstruyen automáticamente.
TEST6_RECONNECT_WITH_EXISTING_POSITIONS = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'initial', data: """ + _bars_js(seed=1) + """ });
  await sleep(20);
  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: 62000, tp: 64000, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(20);
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '6: precondición — listo y editable antes de la desconexión');
  assert(!!document.querySelector('.mbgc-pill-sl'), '6: precondición — pill real de SL visible antes de la desconexión');

  // Desconexión real (socket viejo cerrado por identidad, igual que el
  // fix de reconexión ya auditado y autorizado) + nueva conexión.
  const oldSock = ws();
  oldSock.readyState = 3;
  if (oldSock.onclose) oldSock.onclose({});
  await sleep(10);

  assert(!hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '6: tras la desconexión, SL/TP queda bloqueado (sin escala válida vigente)');
  assert(!document.querySelector('.mbgc-pill-sl'), '6: el pill real de SL ya no se muestra tras la desconexión');
  assert(!!document.querySelector('.mbgc-pill.mbgc-add') && document.querySelector('.mbgc-pill.mbgc-add').textContent.indexOf('Cargando')!==-1, '6: "Cargando gráfico" visible tras la desconexión');

  hooks.session.connect();
  await sleep(10);
  const newSock = ws();
  assert(newSock !== oldSock, '6: connect() crea un socket NUEVO, distinto del anterior');

  // El backend real reenvía positions/history tras la reconexión
  // (mismo flujo ya existente en onopen/_requestHistory) — simulado
  // aquí con la MISMA posición/símbolo (sin ninguna operación nueva).
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(10);
  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'complete', data: """ + _bars_js(seed=2) + """ });
  await sleep(30);

  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '6: tras la reconexión y el histórico vigente, SL/TP se reconstruye automáticamente');
  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '6: B1 se redibuja automáticamente tras la reconexión');
  assert(!!document.querySelector('.mbgc-pill-sl'), '6: el pill real de SL vuelve a mostrarse tras la reconexión, sin ninguna operación nueva');
"""

# 7 — cambio de símbolo/timeframe → sigue siendo imposible editar
# utilizando la escala anterior (verificación de no regresión de las
# protecciones ya auditadas/autorizadas en bloques previos).
TEST7_NO_STALE_SCALE_REGRESSION = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type: 'history', symbol: 'BTCUSD', timeframe: hooks.session.currentTF, phase: 'complete', data: """ + _bars_js(base="63000") + """ });
  await sleep(20);
  const B1 = { id: 'B1', symbol: 'BTCUSD', side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  ws().emit({ type: 'positions', items: [B1] });
  await sleep(20);
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '7: precondición — BTCUSD listo y editable');
  const addTpBefore = document.querySelector('.mbgc-add-tp');
  assert(!!addTpBefore, '7: precondición — "+ TP" visible para BTCUSD');

  // Cambio de símbolo — exactamente la secuencia real del watchlist.
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('EUR/USD');
  hooks.refreshPositionLines();
  await sleep(10);

  assert(!hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '7: tras el cambio, la escala de BTCUSD ya no se considera válida');
  function sentUpdates(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:update'; }); }
  assertEqual(sentUpdates().length, 0, '7: ningún order:update enviado solo por el cambio de símbolo');
"""


def _run(test_case, body, result_id):
    html = _dump_mobile_html(test_case)
    html = _inline_real_sources(html, FAKE_LWC)
    html = _inject_fake_ws_and_hooks(html, test_case)
    driver = _driver_wrap(body, result_id)
    return _run_driver_and_extract(
        test_case, html, driver, window_size="390,844", virtual_time_budget=15000, result_id=result_id,
    )


class MobileGraphReloadPositionsSlTpTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _assert_all_pass(self, data):
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")

    def test_1_preexisting_position_survives_full_reload(self):
        self._assert_all_pass(_run(self, TEST1_RELOAD, "t1_results"))

    def test_2_positions_before_history(self):
        self._assert_all_pass(_run(self, TEST2_POSITIONS_BEFORE_HISTORY, "t2_results"))

    def test_3_history_before_positions(self):
        self._assert_all_pass(_run(self, TEST3_HISTORY_BEFORE_POSITIONS, "t3_results"))

    def test_4_slow_chart_initialization_converges(self):
        self._assert_all_pass(_run(self, TEST4_SLOW_CHART_INIT, "t4_results"))

    def test_5_new_confirmed_position_already_ready_no_delay(self):
        self._assert_all_pass(_run(self, TEST5_NEW_CONFIRMED_POSITION_NO_DELAY, "t5_results"))

    def test_6_reconnect_with_existing_positions_reconstructs(self):
        self._assert_all_pass(_run(self, TEST6_RECONNECT_WITH_EXISTING_POSITIONS, "t6_results"))

    def test_7_symbol_switch_still_blocks_stale_scale(self):
        self._assert_all_pass(_run(self, TEST7_NO_STALE_SCALE_REGRESSION, "t7_results"))
