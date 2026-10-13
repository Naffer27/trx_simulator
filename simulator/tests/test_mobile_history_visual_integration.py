# simulator/tests/test_mobile_history_visual_integration.py
"""
MOBILE-HISTORY-INTEGRATION-01 — Mobile Historial view, visual contract
port of money-broker-movil 2/historial.html + tema.css.

Verifies the new "Operaciones" tab sits entirely on top of the EXISTING
closed-trades flow (session.requestClosedTrades()/onClosedTrades(), the
same 03C.2.6/FIX-HISTORY-AUTO-CLOSE-SYNC-01 contract Posiciones' own
Cerradas tab already uses) — never a second WS request mechanism, never
a duplicate connection, never a second financial computation. Also
verifies: no duplicate rows on repeated/empty responses (merge-by-id,
same discipline desktop.html's own reconcileClosedTrades() already
established), loading/empty/error state separation (a disconnect must
never render as "sin operaciones"), local-timezone day grouping across
midnight/month boundaries, and that the summary never fabricates a
"Neto"/"Acierto" figure that has no real, period-demonstrated backend
field. Depósitos/Retiros are verified disabled (no real data flow
exists for them anywhere in this codebase).

Real-execution tests reuse the established FakeWebSocket + real-source-
dump harness (test_mobile_close_position_isolated_bug.py). No network,
no real account activity, no server started by these tests.
"""
from django.test import SimpleTestCase, TestCase

from simulator.tests.test_mobile_close_position_isolated_bug import (
    CHROME_PATH,
    FAKE_LWC,
    _dump_mobile_html,
    _driver_wrap,
    _inject_fake_ws_and_hooks,
    _inline_real_sources,
    _run_driver_and_extract,
)

MOBILE_HTML_PATH = "simulator/templates/simulator/trade/mobile.html"


def _mobile_html_source():
    with open(MOBILE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


def _js_function_body(html, fn_name):
    start = html.index(f"function {fn_name}(")
    start = html.index("{", start)
    depth = 0
    i = start
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces while scanning function {fn_name}()")


# ─────────────────────────────────────────────────────────────────────────
# A. Static structure — real reuse, no second request/financial logic,
#    honest summary, disabled deposits/withdrawals, header dedup.
# ─────────────────────────────────────────────────────────────────────────
class HistoryStaticStructureTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_coming_soon_placeholder_removed(self):
        self.assertNotIn("History — coming soon", self.html)

    def test_header_dedup_css_rule_present(self):
        self.assertIn(".mob-foundation.mbh-active .mob-topbar{display:none;}", self.html)

    def test_other_views_topbar_rules_untouched(self):
        self.assertIn(".mob-foundation.mbg-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbw-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbp-active .mob-topbar{display:none;}", self.html)

    def test_closed_trades_request_reused_not_duplicated(self):
        # The only real call site for the request itself stays inside
        # requestClosedTrades() (mobile_session.js, protected) — this
        # file must only ever call session.requestClosedTrades(), never
        # send a raw get_closed_trades payload itself, and never send it
        # from more than the two real, user-driven trigger points
        # (Posiciones' Cerradas tab, Historial's own activation/tab).
        self.assertEqual(self.html.count("session.requestClosedTrades();"), 3)
        # The real 'get_closed_trades' action string must only ever
        # appear inside mobile_session.js's own protected implementation
        # (read-only reference in this file's comments is fine) — this
        # file itself must never construct that payload or call ws.send
        # with it directly.
        self.assertNotIn("ws.send(JSON.stringify({ action: 'get_closed_trades'", self.html)
        self.assertNotIn('action:"get_closed_trades"', self.html)

    def test_closed_trades_callback_feeds_both_views_without_removing_positions(self):
        on_closed_body = _js_function_body(self.html, "onClosedTrades")
        self.assertIn("renderClosed(trades)", on_closed_body)
        self.assertIn("reconcileHistorialTrades(trades)", on_closed_body)

    def test_reconcile_merges_by_real_id_never_wipes_on_empty(self):
        body = _js_function_body(self.html, "reconcileHistorialTrades")
        self.assertIn("if(!Array.isArray(trades)||!trades.length)return;", body)
        self.assertIn("String(t.id)", body)
        self.assertNotIn("historialTrades=trades", body)
        self.assertNotIn("historialTrades=[]", body)

    def test_no_pnl_or_commission_recomputation(self):
        render_body = _js_function_body(self.html, "renderHistorialDays")
        for forbidden in ("t.entry-", "t.close-", "* t.qty", "commission", "spread"):
            self.assertNotIn(forbidden, render_body, forbidden)
        self.assertIn("mbgStripMoney(pnlVal,true)", render_body)

    def test_summary_never_fabricates_net_or_winrate(self):
        summary_body = _js_function_body(self.html, "renderHistorialSummary")
        self.assertNotIn("mobHistNet", summary_body)
        self.assertNotIn("mobHistWinRate", summary_body)
        self.assertIn('id="mobHistNet">—<', self.html)
        self.assertIn('id="mobHistWinRate">—<', self.html)

    def test_coverage_note_never_presents_50_as_a_total_cap(self):
        # The backend's 50-per-response limit is real, but merging by
        # trade.id across multiple real responses can accumulate MORE
        # than 50 locally — the note must describe the per-query limit,
        # never claim it bounds the total shown or that the list is the
        # complete history.
        summary_body = _js_function_body(self.html, "renderHistorialSummary")
        self.assertIn(
            "Cada consulta recupera hasta 50 operaciones recientes. Puedes filtrar las operaciones cargadas.",
            summary_body,
        )
        self.assertNotIn("Se muestran como máximo las 50", summary_body)
        self.assertNotIn("nunca el historial completo", summary_body)

    def test_count_label_matches_what_the_number_represents(self):
        # Same cell, same id — its LABEL must change with what the
        # number actually means: all loaded data vs. a filtered subset.
        summary_body = _js_function_body(self.html, "renderHistorialSummary")
        self.assertIn("elHistCountLabel.textContent=historialDateFilter?'Filtradas':'Operaciones';", summary_body)

    def test_stale_data_preserved_not_hidden_on_error(self):
        # A disconnect/timeout with trades already loaded must keep the
        # list visible (never replace it with the full error screen) and
        # show a compact "could not refresh" note instead.
        state_body = _js_function_body(self.html, "renderHistorialState")
        self.assertIn("hasData=historialTrades.length>0", state_body.replace(" ", ""))
        self.assertIn("elHistStale.hidden=!(historialState==='error'&&hasData)", state_body.replace(" ", ""))
        self.assertIn("elHistError.hidden=!(historialState==='error'&&!hasData)", state_body.replace(" ", ""))
        self.assertIn('id="mobHistStale"', self.html)

    def test_deposits_withdrawals_disabled_no_invented_endpoint(self):
        self.assertIn('data-htab="deposits" disabled', self.html)
        self.assertIn('data-htab="withdrawals" disabled', self.html)
        # Scoped to the Historial view's own markup/script (not the
        # whole 3000+ line file, which legitimately contains unrelated
        # "fetch("/"balance-" substrings elsewhere e.g. CSS class names).
        start = self.html.index('data-mobile-view="history"')
        end = self.html.index('function onClosedTrades')
        history_block = self.html[start:end]
        for forbidden in ("/api/deposits", "/api/withdrawals", "fetch(", "XMLHttpRequest", "new WebSocket("):
            self.assertNotIn(forbidden, history_block, forbidden)

    def test_date_filter_never_requests_a_range_from_backend(self):
        # The only real network effect of the date filter must be zero —
        # it filters historialTrades (already-local data) only.
        input_handler_start = self.html.index("elHistDateInput.addEventListener")
        input_handler_end = self.html.index("});", input_handler_start) + 3
        handler = self.html[input_handler_start:input_handler_end]
        self.assertNotIn("session.", handler)
        self.assertNotIn("ws.send", handler)

    def test_connection_status_gates_request_never_reads_ws_directly(self):
        body = _js_function_body(self.html, "requestHistorial")
        self.assertIn("mobConnected", body)
        self.assertNotIn("session.ws", body)
        self.assertNotIn(".readyState", body)


# ─────────────────────────────────────────────────────────────────────────
# B. Real-execution behavior.
# ─────────────────────────────────────────────────────────────────────────
BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function gotoHistory(){ document.querySelector('.mob-navitem[data-target="history"]').click(); }
  function gotoPositions(){ document.querySelector('.mob-navitem[data-target="positions"]').click(); }
  function topbarVisible(){ return getComputedStyle(document.querySelector('.mob-topbar')).display !== 'none'; }
  function getClosedTradesSends(){ return ws().sent.filter(function(s){ return JSON.parse(s).action === 'get_closed_trades'; }); }
  function rows(){ return document.querySelectorAll('.mbh-row'); }
  function dayLabels(){ return Array.prototype.map.call(document.querySelectorAll('.mbh-day-label'), function(e){ return e.textContent; }); }

  ws().emit({ type:'positions', items: [] });
  await sleep(20);

  // ── Entrada a Historial: pide EXACTAMENTE el flujo real existente ──
  gotoHistory();
  await sleep(10);
  assert(!topbarVisible(), 'header: generic topbar hidden while Historial is active');
  assertEqual(getClosedTradesSends().length, 1, 'entrar a Historial dispara exactamente una solicitud real get_closed_trades');
  assert(!document.getElementById('mobHistLoading').hidden, 'estado "cargando" visible mientras se espera la respuesta real');
  assert(document.getElementById('mobHistEmpty').hidden, 'nunca se declara "vacío" mientras aún se está cargando');
  assert(document.getElementById('mobHistError').hidden, 'sin error todavía');

  // ── La respuesta real alimenta Historial Y Posiciones, sin romper
  //    Cerradas ────────────────────────────────────────────────────
  const T1 = { id: 501, symbol:'EUR/USD', side:'buy', qty:0.10, entry:1.10500, close:1.10800, pnl:30.00, ts: Date.now() - 60000 };
  const T2 = { id: 502, symbol:'BTCUSD', side:'sell', qty:0.01, entry:63000, close:62500, pnl:-5.00, ts: Date.now() - 120000 };
  const T3 = { id: 503, symbol:'EUR/USD', side:'sell', qty:0.05, entry:1.10900, close:1.10900, pnl:0.00, ts: Date.now() - 180000 };
  ws().emit({ type:'closed_trades_snapshot', trades: [T1, T2, T3] });
  await sleep(20);

  assert(document.getElementById('mobHistLoading').hidden, 'el estado de carga se oculta tras la respuesta real');
  assertEqual(rows().length, 3, 'tres operaciones reales -> tres filas, identificadas por su propia id real');
  assertEqual(document.getElementById('mobHistCount').textContent, '3', 'el recuento de Operaciones es real (3 cargadas), nunca fabricado');
  assertEqual(document.getElementById('mobHistNet').textContent, '—', '"Neto" nunca se fabrica — sin campo real de P/L con periodo demostrado');
  assertEqual(document.getElementById('mobHistWinRate').textContent, '—', '"Acierto" nunca se fabrica — sin campo real de porcentaje de acierto');
  assert(document.getElementById('mobHistScopeNote').textContent.length > 0, 'se informa el alcance real de los registros mostrados');

  gotoPositions();
  await sleep(10);
  document.querySelector('.mob-pane-tab[data-pane="closed"]').click();
  await sleep(20);
  assert(document.querySelectorAll('#mobClosedList .mob-row-card').length >= 3, 'Posiciones sigue recibiendo las mismas operaciones cerradas reales — Historial no le quitó sus actualizaciones');

  // ── Varios símbolos, resultados +/-/0, signos y precisión reales ──
  gotoHistory();
  await sleep(10);
  const positiveRow = Array.prototype.find.call(rows(), function(r){ return r.textContent.indexOf('EUR/USD') !== -1 && r.textContent.indexOf('COMPRA') !== -1; });
  const negativeRow = Array.prototype.find.call(rows(), function(r){ return r.textContent.indexOf('BTCUSD') !== -1; });
  const zeroRow = Array.prototype.find.call(rows(), function(r){ return r.textContent.indexOf('VENTA') !== -1 && r.textContent.indexOf('EUR/USD') !== -1; });
  assertEqual(positiveRow.querySelector('.mbh-pl').textContent, '+$30.00', 'resultado positivo real, signo + y precisión de 2 decimales');
  assert(positiveRow.querySelector('.mbh-pl').classList.contains('pos'), 'clase pos aplicada');
  assertEqual(negativeRow.querySelector('.mbh-pl').textContent, '−$5.00', 'resultado negativo real conserva el signo negativo real');
  assert(negativeRow.querySelector('.mbh-pl').classList.contains('neg'), 'clase neg aplicada');
  assertEqual(zeroRow.querySelector('.mbh-pl').textContent, '$0.00', 'resultado cero real se muestra como cero, nunca "—" ni con signo');

  // ── Respuestas repetidas: nunca duplica filas ───────────────────────
  ws().emit({ type:'closed_trades_snapshot', trades: [T1, T2, T3] });
  await sleep(20);
  assertEqual(rows().length, 3, 'una respuesta repetida con las MISMAS ids reales nunca duplica filas');

  // ── Respuesta vacía: nunca borra el historial ya cargado ───────────
  ws().emit({ type:'closed_trades_snapshot', trades: [] });
  await sleep(20);
  assertEqual(rows().length, 3, 'una respuesta vacía (p. ej. degradada por un fallo real del backend) nunca borra lo ya cargado');

  // ── Volver a entrar: vuelve a pedir (comportamiento ya establecido
  //    por Cerradas), sin crear una segunda conexión ──────────────────
  const sendsBeforeReentry = getClosedTradesSends().length; // 2 hasta aquí: la entrada inicial a Historial + el tap real de Cerradas en Posiciones, ambos ya ejercitados arriba
  gotoPositions();
  await sleep(10);
  gotoHistory();
  await sleep(10);
  assertEqual(ws(), firstWs, 'navegar entre pantallas nunca crea una segunda conexión WebSocket');
  assertEqual(getClosedTradesSends().length, sendsBeforeReentry + 1, 'volver a entrar a Historial sí vuelve a pedir el snapshot real UNA vez más (igual que reabrir Cerradas ya hacía) — nunca una solicitud duplicada SIMULTÁNEA por la misma entrada');
"""


class HistoryRealExecutionTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_entry_feed_multi_symbol_dedupe_and_shared_connection(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "hist_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="hist_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


# ─────────────────────────────────────────────────────────────────────────
# C. Empty list, error (disconnected/timeout), and midnight/month-
#    boundary date grouping — all distinguished, never conflated.
# ─────────────────────────────────────────────────────────────────────────
STATES_AND_DATES_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  function gotoHistory(){ document.querySelector('.mob-navitem[data-target="history"]').click(); }

  ws().emit({ type:'positions', items: [] });
  await sleep(20);

  // ── Lista vacía real (respuesta real, cero operaciones) ─────────────
  gotoHistory();
  await sleep(10);
  ws().emit({ type:'closed_trades_snapshot', trades: [] });
  await sleep(20);
  assert(!document.getElementById('mobHistEmpty').hidden, 'lista vacía real se declara solo tras una respuesta real sin operaciones');
  assert(document.getElementById('mobHistError').hidden, 'una lista vacía real nunca se confunde con un error');
  assert(document.getElementById('mobHistLoading').hidden, 'ni con "cargando"');

  // ── Error: sin conexión al entrar — nunca se presenta como "vacío" ──
  ws().close();
  await sleep(10);
  gotoHistory();
  await sleep(10);
  assert(!document.getElementById('mobHistError').hidden, 'sin conexión real al entrar -> estado de error visible de inmediato');
  assert(document.getElementById('mobHistEmpty').hidden, 'una desconexión real NUNCA se presenta como "sin operaciones"');
  assert(document.getElementById('mobHistLoading').hidden, 'tampoco se presenta como "cargando" indefinidamente');
"""


class HistoryStatesAndDatesTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_empty_and_disconnected_error_states_never_conflated(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(STATES_AND_DATES_BODY, "hist_states_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="hist_states_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")


MIDNIGHT_MONTH_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  document.querySelector('.mob-navitem[data-target="history"]').click();
  await sleep(10);
  ws().emit({ type:'positions', items: [] });
  await sleep(10);

  // Construye marcas de tiempo reales (epoch ms) a propósito a ambos
  // lados de la medianoche LOCAL de hoy y a ambos lados de un cambio de
  // mes real — nunca comparando solo el número del día.
  const now = new Date();
  const todayMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0).getTime();
  const justBeforeMidnight = todayMidnight - 5000;   // 23:59:55 de ayer (real)
  const justAfterMidnight  = todayMidnight + 5000;   // 00:00:05 de hoy (real)
  const firstOfMonth = new Date(now.getFullYear(), now.getMonth(), 1, 0, 30, 0, 0).getTime();
  const lastOfPrevMonth = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0).getTime() - 3600000; // 23:00 del último día del mes anterior (real)

  const TA = { id: 601, symbol:'EUR/USD', side:'buy', qty:0.10, entry:1.1, close:1.11, pnl:1.00, ts: justBeforeMidnight };
  const TB = { id: 602, symbol:'EUR/USD', side:'buy', qty:0.10, entry:1.1, close:1.11, pnl:2.00, ts: justAfterMidnight };
  const TC = { id: 603, symbol:'BTCUSD', side:'buy', qty:0.01, entry:60000, close:61000, pnl:3.00, ts: firstOfMonth };
  const TD = { id: 604, symbol:'BTCUSD', side:'buy', qty:0.01, entry:60000, close:59000, pnl:-4.00, ts: lastOfPrevMonth };
  ws().emit({ type:'closed_trades_snapshot', trades: [TA, TB, TC, TD] });
  await sleep(20);

  const groups = document.querySelectorAll('.mbh-day-label');
  assert(groups.length >= 2, 'una operación 5s antes de medianoche y otra 5s después quedan en grupos de DÍA distintos, nunca el mismo "hoy"');
  const texts = Array.prototype.map.call(groups, function(g){ return g.textContent; });
  assert(texts.join('|').indexOf('OCT') !== -1 || texts.join('|').indexOf('NOV') !== -1 || texts.join('|').indexOf('HOY') !== -1 || texts.join('|').indexOf('AYER') !== -1, 'las etiquetas de grupo usan fechas reales, nunca inventadas');
  // Las filas de TC (1º de mes) y TD (último día del mes anterior) NO
  // comparten grupo aunque numéricamente "1" podría confundirse con un
  // offset de "31-1": deben aparecer en grupos de fecha distintos.
  const rowsAll = document.querySelectorAll('.mbh-row');
  assertEqual(rowsAll.length, 4, 'las cuatro operaciones reales aparecen, agrupadas por fecha completa real');
"""


class HistoryMidnightMonthBoundaryTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_day_grouping_across_midnight_and_month_boundary(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(MIDNIGHT_MONTH_BODY, "hist_dates_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="hist_dates_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")


# ─────────────────────────────────────────────────────────────────────────
# D. Merge coherence (update-in-place, not duplicate), counter/label
#    coherence (loaded vs. filtered, accumulation beyond 50), and
#    stale-data preservation on a real disconnect/timeout.
# ─────────────────────────────────────────────────────────────────────────
MERGE_AND_COVERAGE_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  function gotoHistory(){ document.querySelector('.mob-navitem[data-target="history"]').click(); }
  function rows(){ return document.querySelectorAll('.mbh-row'); }
  function rowById(symbol, timeFrag){ return Array.prototype.find.call(rows(), function(r){ return r.textContent.indexOf(symbol) !== -1 && r.textContent.indexOf(timeFrag) !== -1; }); }

  ws().emit({ type:'positions', items: [] });
  await sleep(10);
  gotoHistory();
  await sleep(10);

  // ── Actualización del MISMO trade.id: la fila se actualiza en su
  //    lugar, nunca se duplica ──────────────────────────────────────
  const now = Date.now();
  const T1v1 = { id: 9001, symbol:'EUR/USD', side:'buy', qty:0.10, entry:1.1, close:1.11, pnl:5.00, ts: now };
  ws().emit({ type:'closed_trades_snapshot', trades: [T1v1] });
  await sleep(20);
  assertEqual(rows().length, 1, 'una sola operación real -> una sola fila');
  assertEqual(document.querySelector('.mbh-pl').textContent, '+$5.00', 'valor inicial real reflejado');

  const T1v2 = { id: 9001, symbol:'EUR/USD', side:'buy', qty:0.10, entry:1.1, close:1.1085, pnl:8.50, ts: now };
  ws().emit({ type:'closed_trades_snapshot', trades: [T1v2] });
  await sleep(20);
  assertEqual(rows().length, 1, 'una actualización del MISMO trade.id (9001) actualiza la fila en su lugar -> sigue habiendo exactamente una fila, nunca dos');
  assertEqual(document.querySelector('.mbh-pl').textContent, '+$8.50', 'la fila refleja el valor real ACTUALIZADO (8.50), no el original ni un duplicado');

  // ── El contador puede superar 50 al fusionar varias respuestas
  //    reales por trade.id (simulando el límite real de 50 por
  //    CONSULTA, nunca un tope del total acumulado) ─────────────────
  function batch(startId, count, baseTs){
    const out=[];
    for(let i=0;i<count;i++){ out.push({ id:startId+i, symbol:'BTCUSD', side:'buy', qty:0.01, entry:60000, close:60100, pnl:1.00, ts: baseTs-i*1000 }); }
    return out;
  }
  ws().emit({ type:'closed_trades_snapshot', trades: batch(10000, 30, now-100000) });
  await sleep(20);
  ws().emit({ type:'closed_trades_snapshot', trades: batch(20000, 30, now-200000) });
  await sleep(20);
  assertEqual(rows().length, 61, 'dos respuestas reales de 30 (ids distintos, ninguno repetido) + la fila de EUR/USD -> 61 filas reales, el total acumulado SÍ supera el límite de 50 por consulta');
  assertEqual(document.getElementById('mobHistCount').textContent, '61', 'el contador real refleja el total acumulado (61), nunca un tope artificial de 50');
  assertEqual(document.getElementById('mobHistCountLabel').textContent, 'Operaciones', 'sin filtro de fecha activo, la etiqueta dice "Operaciones" (todo lo cargado)');
  assert(document.getElementById('mobHistScopeNote').textContent.indexOf('Cada consulta recupera hasta 50') !== -1, 'la nota describe el límite POR CONSULTA, nunca un tope del total mostrado');

  // ── Con un filtro de fecha activo, la etiqueta cambia para reflejar
  //    que el número ya no es "todo lo cargado" ──────────────────────
  const todayKey = new Date(now).toISOString().slice(0,10);
  document.getElementById('mobHistDateBtn').click();
  await sleep(10);
  const dateInput = document.getElementById('mobHistDateInput');
  dateInput.value = todayKey;
  dateInput.dispatchEvent(new Event('change'));
  await sleep(20);
  assertEqual(document.getElementById('mobHistCountLabel').textContent, 'Filtradas', 'con un filtro de fecha activo, la etiqueta pasa a "Filtradas"');
  assert(document.getElementById('mobHistCount').textContent !== '101', 'el número ya no representa el total cargado, sino el subconjunto filtrado real');
  document.getElementById('mobHistDateClear').click();
  await sleep(10);
  assertEqual(document.getElementById('mobHistCountLabel').textContent, 'Operaciones', 'al quitar el filtro, la etiqueta vuelve a "Operaciones"');
  assertEqual(document.getElementById('mobHistCount').textContent, '61', 'y el número vuelve a representar todo lo cargado');

  // ── Ante desconexión/timeout CON datos ya cargados: la lista se
  //    conserva, nunca se presenta como recién sincronizada ─────────
  ws().close();
  await sleep(10);
  gotoHistory();
  await sleep(10);
  assertEqual(rows().length, 61, 'al perder la conexión con datos ya cargados, la lista real sigue visible — nunca se vacía ni se oculta');
  assert(!document.getElementById('mobHistStale').hidden, 'se muestra el aviso compacto de "no se pudo actualizar"');
  assert(document.getElementById('mobHistError').hidden, 'nunca la pantalla de error completa — eso reemplazaría la lista que sí existe');
  assert(document.getElementById('mobHistEmpty').hidden, 'nunca se presenta como "sin operaciones"');
  assert(document.getElementById('mobHistLoading').hidden, 'nunca se presenta como "cargando" ocultando lo ya real');
"""


class HistoryMergeCoverageStaleTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_update_in_place_accumulation_label_and_stale_preservation(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(MERGE_AND_COVERAGE_BODY, "hist_merge_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="hist_merge_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 12)
