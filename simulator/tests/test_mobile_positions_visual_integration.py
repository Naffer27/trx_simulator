# simulator/tests/test_mobile_positions_visual_integration.py
"""
MOBILE-POSITIONS-INTEGRATION-01 — Mobile Positions view, visual contract
port of money-broker-movil 2/posiciones.html + tema.css.

Verifies the redesigned "Abiertas" (open positions) cards and the new
account summary card sit entirely on top of the existing architecture:
the real account:update fields (never recomputed), the real canonical
positions store (getCanonicalPositions(), id-keyed — never symbol/index-
keyed), the EXACT existing close flow (session.closePosition() +
closeInFlight + armPendingClose()/onOrderClose()/onCloseError(), never
duplicated), and the EXACT existing SL/TP editor (mobile_chart.js's own
drag/tap UI, reached here only via symbol-select + selectedPositionId,
never a second editor). Pending/Cerradas panes and their own cancel
flow are untouched by this round and not re-tested here beyond
confirming they still exist unchanged.

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
# A. Static structure — real reuse, no second editor/close flow, no
#    fabricated margin level, header dedup. No Chrome needed.
# ─────────────────────────────────────────────────────────────────────────
class PositionsStaticStructureTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_header_dedup_css_rule_present(self):
        self.assertIn(".mob-foundation.mbp-active .mob-topbar{display:none;}", self.html)

    def test_other_views_topbar_rules_untouched(self):
        self.assertIn(".mob-foundation.mbg-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbw-active .mob-topbar{display:none;}", self.html)

    def test_margin_level_never_computed(self):
        # "Nivel" must stay the static dash already in the markup — never
        # equity/margin*100 or any other formula computed in this file.
        self.assertIn('id="mobPosLevel">—<', self.html)
        self.assertNotIn("mobPosLevel.textContent", self.html)
        on_account_body = _js_function_body(self.html, "onAccount")
        self.assertNotIn("msg.margin_level", on_account_body)
        self.assertNotIn("elPosLevel.textContent", on_account_body)
        self.assertNotIn("elPosLevel", self.html)  # no such variable declared at all

    def test_close_flow_reused_not_duplicated(self):
        # The only two legitimate session.closePosition() call sites are
        # the chart overlay's own × (mobileChart.onCloseClick, pre-
        # existing) and the Positions list's delegated listener (pre-
        # existing) — this round must not add a THIRD. The new "Cerrar"
        # button reuses the exact same .mob-close-btn class/delegated
        # handler, never a new call site.
        self.assertEqual(self.html.count("const sent=session.closePosition("), 2)
        render_body = _js_function_body(self.html, "renderPositions")
        self.assertIn('class="mbp-btn-danger mob-close-btn"', render_body)

    def test_sltp_editor_never_duplicated(self):
        # The SL/TP edit button's OWN click handler (not the pre-existing
        # mobileChart.onSlTpConfirm callback elsewhere in the file) must
        # never submit an order or an SL/TP update itself — it only
        # navigates and selects a position id; the real edit still only
        # ever happens through mobile_chart.js's own onSlTpConfirm.
        handler_start = self.html.index("e.target.closest('.mob-sltp-edit-btn')")
        handler_end = self.html.index("function onOrderClose", handler_start)
        sltp_handler = self.html[handler_start:handler_end]
        self.assertNotIn("session.updateSlTp(", sltp_handler)
        self.assertNotIn("session.submitOrder(", sltp_handler)
        self.assertNotIn("session.closePosition(", sltp_handler)
        self.assertIn("selectedPositionId=pos.id", sltp_handler)
        self.assertIn("refreshPositionLines()", sltp_handler)

    def test_cards_identified_by_real_id_not_symbol_or_index(self):
        render_body = _js_function_body(self.html, "renderPositions")
        self.assertIn('data-id="\'+pos.id+\'"', render_body)
        self.assertNotIn("items[i]", render_body)

    def test_no_second_price_authority_for_current_price(self):
        current_price_body = _js_function_body(self.html, "positionCurrentPrice")
        self.assertIn("lastQuoteState", current_price_body)
        self.assertNotIn("selectSymbol(", current_price_body)
        self.assertNotIn("fetch(", current_price_body)

    def test_range_bar_neutral_without_real_inputs(self):
        frac_body = _js_function_body(self.html, "positionRangeFraction")
        self.assertIn("return null", frac_body)

    def test_refresh_icon_disabled_unless_closed_tab(self):
        # The button's own markup starts out `disabled` (Abiertas is the
        # default-selected pane) — updatePosRefreshBtn() then toggles it
        # live per the active tab.
        idx = self.html.index('id="mobPosRefreshBtn"')
        around = self.html[idx - 80:idx + 80]
        self.assertIn("disabled", around)
        self.assertIn("function updatePosRefreshBtn(key)", self.html)
        self.assertIn("elPosRefreshBtn.disabled=(key!=='closed');", self.html)

    def test_pending_closed_cancel_flow_untouched(self):
        self.assertEqual(self.html.count("session.cancelPendingOrder("), 1)
        self.assertEqual(self.html.count("function renderClosed("), 1)
        self.assertEqual(self.html.count("function renderPending("), 1)


# ─────────────────────────────────────────────────────────────────────────
# B. Real-execution behavior.
# ─────────────────────────────────────────────────────────────────────────
BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function gotoPositions(){ document.querySelector('.mob-navitem[data-target="positions"]').click(); }
  function topbarVisible(){ return getComputedStyle(document.querySelector('.mob-topbar')).display !== 'none'; }
  function card(id){ return document.querySelector('.mbp-card[data-id="' + id + '"]'); }

  // ── Resumen de cuenta: positivo / negativo / cero / ausente ────────
  gotoPositions();
  await sleep(10);
  assert(!topbarVisible(), 'header: generic topbar hidden while Posiciones is active');
  assertEqual(document.getElementById('mobPosEquity').textContent, '—', 'equity "—" antes del primer account:update real');
  assertEqual(document.getElementById('mobPosPl').textContent, '—', 'P&L flotante "—" antes del primer mensaje');

  ws().emit({ type:'account:update', balance: 10230, equity: 10240.89, margin_used: 418.13, free_margin: 9822.76, upnl: 10.89, pnl_unreal: 10.89, leverage: 100 });
  await sleep(20);
  assertEqual(document.getElementById('mobPosEquity').textContent, '$10,240.89', 'equity real reflejada tal cual, sin recálculo');
  assertEqual(document.getElementById('mobPosPl').textContent, '+$10.89', 'P&L flotante positivo real, con signo +');
  assert(document.getElementById('mobPosPl').classList.contains('pos'), 'clase pos aplicada para P&L positivo');
  assertEqual(document.getElementById('mobPosBalance').textContent, '$10,230', 'balance real');
  assertEqual(document.getElementById('mobPosMargin').textContent, '$418.13', 'margen real');
  assertEqual(document.getElementById('mobPosFree').textContent, '$9,822.76', 'libre real');
  assertEqual(document.getElementById('mobPosLevel').textContent, '—', 'Nivel siempre "—" — no existe margin_level en el contrato real');

  ws().emit({ type:'account:update', balance: 0, equity: 0, margin_used: 0, free_margin: 0, upnl: -45.20, pnl_unreal: -45.20, leverage: 50 });
  await sleep(20);
  assertEqual(document.getElementById('mobPosBalance').textContent, '$0', 'balance cero real se muestra como cero, nunca "—"');
  assertEqual(document.getElementById('mobPosPl').textContent, '−$45.20', 'P&L negativo conserva el signo negativo real');
  assert(document.getElementById('mobPosPl').classList.contains('neg'), 'clase neg aplicada para P&L negativo');

  // ── Varias posiciones del MISMO símbolo: tarjetas e IDs correctos ──
  const P1 = { id:'P1', symbol:'EUR/USD', side:'buy', qty:0.10, avg:1.10500, sl:1.10000, tp:1.11200, pnl:12.34 };
  const P2 = { id:'P2', symbol:'EUR/USD', side:'sell', qty:0.20, avg:1.10800, sl:1.11200, tp:1.10000, pnl:-5.00 };
  ws().emit({ type:'positions', items:[P1, P2] });
  await sleep(20);
  assertEqual(document.querySelectorAll('.mbp-card').length, 2, 'dos posiciones del mismo símbolo -> dos tarjetas separadas');
  assertEqual(document.getElementById('mobPosCount').textContent, '2', 'contador de Abiertas refleja el real');
  assert(!!card('P1') && !!card('P2'), 'ambas tarjetas identificables por su ID real');
  assertEqual(card('P1').querySelector('.mbp-tag').textContent, 'COMPRA', 'P1 lado real COMPRA');
  assertEqual(card('P2').querySelector('.mbp-tag').textContent, 'VENTA', 'P2 lado real VENTA');
  assertEqual(card('P1').querySelector('.mbp-pl-cell b').textContent, '+$12.34', 'P&L real de P1, nunca recalculado');
  assertEqual(card('P2').querySelector('.mbp-pl-cell b').textContent, '−$5.00', 'P&L real de P2, signo negativo conservado');

  // ── Cierre: el ID enviado es exactamente el de la tarjeta tocada ───
  realClick(card('P2').querySelector('.mob-close-btn'));
  await sleep(20);
  const lastSent = JSON.parse(ws().sent[ws().sent.length-1]);
  assertEqual(lastSent.action, 'order:close', 'se envía exactamente el flujo real de cierre (order:close)');
  assertEqual(lastSent.id, 'P2', 'el ID enviado es el de la tarjeta P2, nunca P1 ni un índice');

  // ── Doble pulsación: no se duplica el envío mientras está en vuelo ─
  const sentCountAfterFirst = ws().sent.length;
  realClick(card('P2').querySelector('.mob-close-btn'));
  await sleep(10);
  assertEqual(ws().sent.length, sentCountAfterFirst, 'una segunda pulsación mientras el cierre está en vuelo no reenvía nada');
  assert(card('P1').querySelector('.mob-close-btn').disabled, 'TODOS los botones Cerrar quedan deshabilitados mientras cualquier cierre está en vuelo (guardia real compartida)');

  // ── Rechazo de cierre: el guard se libera, la posición no se retira
  //    localmente hasta que el snapshot real lo confirme ─────────────
  ws().emit({ type:'error', message:'price_unavailable' });
  await sleep(20);
  assert(!card('P1').querySelector('.mob-close-btn').disabled, 'tras el rechazo, los botones Cerrar vuelven a estar habilitados');
  assertEqual(document.querySelectorAll('.mbp-card').length, 2, 'el rechazo nunca retira la posición localmente — sigue esperando al snapshot real');

  // ── Modificar SL/TP: navega y selecciona el ID real, sin enviar nada ─
  const sentCountBeforeEdit = ws().sent.length;
  realClick(card('P1').querySelector('.mob-sltp-edit-btn'));
  await sleep(30);
  assertEqual(hooks.session.currentSymbol, 'EUR/USD', 'Modificar SL/TP selecciona el símbolo real de la posición');
  assert(document.querySelector('.mob-view[data-mobile-view="trade"]').hidden === false, 'Modificar SL/TP abre el Gráfico mediante el flujo existente');
  assertEqual(String(hooks.mobileChart._selectedId), 'P1', 'el Gráfico queda con la posición P1 exacta seleccionada (no P2, no la primera por símbolo)');
  const sentAfterEdit = ws().sent.slice(sentCountBeforeEdit).map(function(s){ return JSON.parse(s).action; });
  assert(sentAfterEdit.indexOf('order:update') === -1, 'abrir el editor nunca envía una modificación por sí mismo');
  assert(sentAfterEdit.indexOf('order:close') === -1, 'abrir el editor nunca envía un cierre');

  // ── Volver a Posiciones: sin segunda conexión, snapshot sigue vivo ──
  gotoPositions();
  await sleep(10);
  assertEqual(ws(), firstWs, 'volver a Posiciones nunca crea una segunda conexión WebSocket');
  assertEqual(document.querySelectorAll('.mbp-card').length, 2, 'las tarjetas siguen reflejando el snapshot real tras volver');

  // ── Pestañas, contadores, estado vacío ──────────────────────────────
  ws().emit({ type:'pending_orders', items: [ { id:'PO1', symbol:'GBP/USD', side:'buy', order_type:'limit', qty:0.05, trigger_price:1.25000, sl:null, tp:null, status:'PENDING' } ] });
  await sleep(10);
  assertEqual(document.getElementById('mobPendingCount').textContent, '1', 'contador de Pendientes refleja el real');

  document.querySelector('.mob-pane-tab[data-pane="pending"]').click();
  await sleep(10);
  assert(document.getElementById('mobPosRefreshBtn').disabled, 'icono de actualización deshabilitado en Pendientes (sin mecanismo real de refresco)');

  document.querySelector('.mob-pane-tab[data-pane="closed"]').click();
  await sleep(10);
  assert(!document.getElementById('mobPosRefreshBtn').disabled, 'icono de actualización habilitado en Cerradas (reutiliza requestClosedTrades real)');
  const sentBeforeRefresh = ws().sent.length;
  realClick(document.getElementById('mobPosRefreshBtn'));
  await sleep(10);
  assertEqual(JSON.parse(ws().sent[ws().sent.length-1]).action, 'get_closed_trades', 'el refresco en Cerradas reutiliza exactamente requestClosedTrades()');
  assert(ws().sent.length > sentBeforeRefresh, 'el click sí generó el envío real esperado');

  document.querySelector('.mob-pane-tab[data-pane="positions"]').click();
  await sleep(10);
  ws().emit({ type:'positions', items: [] });
  await sleep(20);
  assertEqual(document.querySelectorAll('.mbp-card').length, 0, 'sin posiciones abiertas, no quedan tarjetas');
  assert(!document.getElementById('mobPositionsEmpty').hidden, 'estado vacío visible sin posiciones abiertas');
  assertEqual(document.getElementById('mobPosCount').textContent, '0', 'contador de Abiertas vuelve a 0');
"""


class PositionsRealExecutionTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_summary_cards_close_flow_sltp_edit_tabs_and_empty_state(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "pos_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="pos_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 25)


# ─────────────────────────────────────────────────────────────────────────
# C. Three+ positions across distinct symbols — ID/pnl/SL/TP independence,
#    price-availability isolation, no stale-looking-live price after a
#    real symbol switch, tick isolation. All with simulated transport.
# ─────────────────────────────────────────────────────────────────────────
MULTI_SYMBOL_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  function gotoPositions(){ document.querySelector('.mob-navitem[data-target="positions"]').click(); }
  function gotoMarkets(){ document.querySelector('.mob-navitem[data-target="markets"]').click(); }
  function card(id){ return document.querySelector('.mbp-card[data-id="' + id + '"]'); }
  function curPriceOf(id){ const c=card(id); return c ? c.querySelector('.mbp-sub').textContent.split('→')[1].trim() : null; }
  function rangeFillOf(id){ const c=card(id); const f=c && c.querySelector('.mbp-range-fill'); return f ? f.style.width : null; }

  ws().emit({ type:'positions', items: [] });
  await sleep(20);

  // Selecciona EUR/USD PRIMERO (flujo real de Watch) — así, cuando el
  // snapshot real de posiciones llegue más abajo, session.currentSymbol
  // ya no es null y la rama de auto-select de onPositions() (reload)
  // no se dispara, dejando el conteo de change_symbol determinista.
  gotoMarkets();
  await sleep(10);
  realClick(document.querySelector('.mob-watchlist-item[data-symbol="EUR/USD"]'));
  await sleep(20);
  gotoPositions();
  await sleep(10);

  // Tres posiciones reales: dos del MISMO símbolo (EUR/USD), una de otro
  // (BTCUSD) — IDs reales, nunca inventados ni basados en índice.
  const P1 = { id:'POS-A1', symbol:'EUR/USD', side:'buy',  qty:0.10, avg:1.10500, sl:1.10000, tp:1.11200, pnl:12.34 };
  const P2 = { id:'POS-A2', symbol:'EUR/USD', side:'sell', qty:0.20, avg:1.10800, sl:1.11200, tp:1.10000, pnl:-5.00 };
  const P3 = { id:'POS-B1', symbol:'BTCUSD',  side:'buy',  qty:0.01, avg:63000.00, sl:62000.00, tp:65000.00, pnl:3.50 };
  ws().emit({ type:'positions', items: [P1, P2, P3] });
  await sleep(20);

  assertEqual(document.querySelectorAll('.mbp-card').length, 3, 'tres posiciones reales -> tres tarjetas, nunca fusionadas por símbolo');
  assert(!!card('POS-A1') && !!card('POS-A2') && !!card('POS-B1'), 'cada tarjeta conserva exactamente su position ID real');

  // ── pnl/SL/TP independientes por tarjeta, nunca recalculados ───────
  assertEqual(card('POS-A1').querySelector('.mbp-pl-cell b').textContent, '+$12.34', 'POS-A1 conserva su propio pos.pnl real');
  assertEqual(card('POS-A2').querySelector('.mbp-pl-cell b').textContent, '−$5.00', 'POS-A2 conserva su propio pos.pnl real (negativo, independiente de POS-A1)');
  assertEqual(card('POS-B1').querySelector('.mbp-pl-cell b').textContent, '+$3.50', 'POS-B1 conserva su propio pos.pnl real, símbolo distinto');
  // fmt() (trading_core.js, protegido) recorta ceros finales -- 1.10000
  // real se MUESTRA como "1.1", 1.11200 como "1.112": no es una
  // pérdida de precisión, es el mismo formateador ya usado en Gráfico/
  // Watch para cualquier precio real de 5 decimales.
  const slTpA1 = card('POS-A1').querySelector('.mbp-range-labels').textContent;
  const slTpA2 = card('POS-A2').querySelector('.mbp-range-labels').textContent;
  assert(slTpA1.indexOf('1.1') !== -1 && slTpA1.indexOf('1.112') !== -1, 'POS-A1 muestra su propio SL/TP real (1.10000 -> "1.1", 1.11200 -> "1.112")');
  assert(slTpA2.indexOf('1.112') !== -1 && slTpA2.indexOf('1.1') !== -1, 'POS-A2 muestra su propio SL/TP real, invertido vs POS-A1 (SL 1.11200/TP 1.10000), nunca compartido');

  // ── Sin cotización válida de ningún símbolo: precio "—", barra neutra ─
  assertEqual(curPriceOf('POS-A1'), '—', 'sin cotización real de EUR/USD, POS-A1 muestra "—"');
  assertEqual(curPriceOf('POS-A2'), '—', 'sin cotización real de EUR/USD, POS-A2 muestra "—"');
  assertEqual(curPriceOf('POS-B1'), '—', 'sin cotización real de BTCUSD, POS-B1 muestra "—"');
  assertEqual(rangeFillOf('POS-A1'), null, 'sin precio real, la barra de POS-A1 queda neutra (sin relleno)');
  assertEqual(rangeFillOf('POS-B1'), null, 'sin precio real, la barra de POS-B1 queda neutra (sin relleno)');

  // ── Recibe un tick real de EUR/USD (símbolo ya seleccionado) ───────
  ws().emit({ type:'price', symbol:'EUR/USD', bid:1.10700, ask:1.10716, source:'test' });
  await sleep(20);

  // ── El precio solo aparece para el símbolo que realmente cotiza ────
  assertEqual(curPriceOf('POS-A1'), '1.10708', 'POS-A1 (EUR/USD) recibe el precio real tras el tick de su propio símbolo');
  assertEqual(curPriceOf('POS-A2'), '1.10708', 'POS-A2 (mismo símbolo EUR/USD) recibe el MISMO precio real — no es una posición distinta de otro símbolo');
  assertEqual(curPriceOf('POS-B1'), '—', 'POS-B1 (BTCUSD) NUNCA recibe el precio de EUR/USD — sigue "—"');
  assert(rangeFillOf('POS-A1') !== null, 'la barra de POS-A1 deja de ser neutra con precio real de su propio símbolo');
  assert(rangeFillOf('POS-B1') === null, 'la barra de POS-B1 sigue neutra — ningún precio de otro símbolo la activa');

  // ── El P&L sigue viniendo de pos.pnl — el tick nunca lo recalcula ──
  assertEqual(card('POS-A1').querySelector('.mbp-pl-cell b').textContent, '+$12.34', 'el tick real nunca recalcula el P&L — sigue siendo exactamente pos.pnl');
  assertEqual(card('POS-A2').querySelector('.mbp-pl-cell b').textContent, '−$5.00', 'mismo caso para POS-A2 — P&L intacto tras el tick');

  // ── Dos posiciones del mismo símbolo: niveles de la barra independientes
  const fillA1 = parseFloat(rangeFillOf('POS-A1'));
  const fillA2 = parseFloat(rangeFillOf('POS-A2'));
  assert(isFinite(fillA1) && isFinite(fillA2), 'ambas barras del mismo símbolo son numéricas');
  assert(Math.abs(fillA1 - fillA2) > 1, 'POS-A1 (compra, SL 1.10000/TP 1.11200) y POS-A2 (venta, SL 1.11200/TP 1.10000) son posiciones DISTINTAS con SL/TP invertidos -> su fracción en la barra difiere, nunca comparten el mismo nivel');

  // ── Cambio de símbolo REAL (Watch, EUR/USD -> BTCUSD) — el transporte
  //    real solo cotiza UN símbolo a la vez (mobile_session.js, confirmado
  //    protegido/sin tocar), así que "un tick de otro símbolo" solo puede
  //    probarse seleccionándolo primero, nunca con dos símbolos cotizando
  //    a la vez. Esto cubre a la vez: ninguna tarjeta conserva un precio
  //    anterior aparentando estar actualizada, y pos.pnl/SL/TP reales
  //    permanecen intactos (nunca borrados ni sustituidos) ────────────
  gotoMarkets();
  await sleep(10);
  realClick(document.querySelector('.mob-watchlist-item[data-symbol="BTCUSD"]'));
  await sleep(20);
  gotoPositions();
  await sleep(10);

  assertEqual(curPriceOf('POS-A1'), '—', 'tras cambiar a BTCUSD, POS-A1 (EUR/USD, ya no cotizado) vuelve a "—" — nunca queda con el precio viejo pareciendo vivo');
  assertEqual(curPriceOf('POS-A2'), '—', 'mismo caso para POS-A2');
  assertEqual(rangeFillOf('POS-A1'), null, 'la barra de POS-A1 vuelve a estado neutro tras el cambio de símbolo — ya no hay precio real de EUR/USD');
  assertEqual(curPriceOf('POS-B1'), '—', 'POS-B1 (BTCUSD) aún "—" — BTCUSD ya está seleccionado pero todavía no llegó ningún tick suyo');
  assertEqual(card('POS-A1').querySelector('.mbp-pl-cell b').textContent, '+$12.34', 'el cambio de símbolo NUNCA borra ni sustituye el pos.pnl real de POS-A1');
  assertEqual(card('POS-A2').querySelector('.mbp-pl-cell b').textContent, '−$5.00', 'ni el de POS-A2');
  const slTpA1After = card('POS-A1').querySelector('.mbp-range-labels').textContent;
  assertEqual(slTpA1After, slTpA1, 'el cambio de símbolo NUNCA borra ni sustituye el SL/TP real de POS-A1');
  assertEqual(card('POS-B1').querySelector('.mbp-pl-cell b').textContent, '+$3.50', 'POS-B1 conserva su pos.pnl real también, intacto durante todo el cambio');

  // ── Un tick ahora solo actualiza las tarjetas del símbolo seleccionado
  //    (BTCUSD) — EUR/USD, ya no cotizado, nunca se ve afectado ───────
  const subA1Before = card('POS-A1').querySelector('.mbp-sub').textContent;
  ws().emit({ type:'price', symbol:'BTCUSD', bid:63500.00, ask:63520.00, source:'test' });
  await sleep(20);
  assertEqual(card('POS-A1').querySelector('.mbp-sub').textContent, subA1Before, 'un tick de BTCUSD nunca modifica las tarjetas de EUR/USD');
  assertEqual(curPriceOf('POS-B1'), '63510', 'POS-B1 sí recibe su propio tick real de BTCUSD, una vez que es el símbolo seleccionado');
  assert(rangeFillOf('POS-B1') !== null, 'la barra de POS-B1 deja de ser neutra con precio real de su propio símbolo');

  // ── Nunca se recorrieron símbolos llamando selectSymbol() en bucle:
  //    exactamente dos cambios de símbolo reales (EUR/USD, BTCUSD),
  //    cada uno originado por un tap real de Watch ───────────────────
  const changeSymbolSends = ws().sent.filter(function(s){ return JSON.parse(s).action === 'change_symbol'; });
  assertEqual(changeSymbolSends.length, 2, 'exactamente dos change_symbol reales (uno por cada tap real de Watch) — nunca un bucle sobre el catálogo');
"""


class PositionsMultiSymbolTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_three_plus_positions_distinct_symbols(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(MULTI_SYMBOL_BODY, "multi_sym_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="multi_sym_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


# ─────────────────────────────────────────────────────────────────────────
# D. "Modificar SL/TP" with deliberately delayed/stale/wrong-instrument
#    history — verifies the REAL, unmodified mobile_chart.js gate
#    (isSlTpEditableFor()) via its own public method, never a second
#    implementation. mobile_chart.js/mobile_session.js are not touched.
# ─────────────────────────────────────────────────────────────────────────
DELAYED_HISTORY_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function gotoPositions(){ document.querySelector('.mob-navitem[data-target="positions"]').click(); }
  function gotoMarkets(){ document.querySelector('.mob-navitem[data-target="markets"]').click(); }

  ws().emit({ type:'positions', items: [] });
  await sleep(20);

  // Símbolo de partida: EUR/USD, ya seleccionado y con su propia
  // historia real cargada (editable).
  gotoMarkets();
  await sleep(10);
  realClick(document.querySelector('.mob-watchlist-item[data-symbol="EUR/USD"]'));
  await sleep(20);
  const eurBars = [{ time: Math.floor(Date.now()/1000)-900, open:1.108, high:1.109, low:1.107, close:1.1085, volume:10 }];
  ws().emit({ type:'history', symbol:'EUR/USD', timeframe: hooks.session.currentTF, phase:'complete', data: eurBars });
  await sleep(20);
  assert(hooks.mobileChart.isSlTpEditableFor('EUR/USD'), 'precondición: EUR/USD ya es editable con su propia historia real cargada');

  gotoPositions();
  await sleep(10);

  // Dos posiciones de BTCUSD (mismo símbolo, IDs distintos) + la de
  // EUR/USD ya seleccionada — para comprobar que no se selecciona "la
  // otra" posición del mismo símbolo por error.
  const E1 = { id:'EUR-1', symbol:'EUR/USD', side:'buy', qty:0.10, avg:1.10500, sl:1.10000, tp:1.11200, pnl:1.00 };
  const B1 = { id:'BTC-TARGET', symbol:'BTCUSD', side:'buy', qty:0.01, avg:63000, sl:62000, tp:65000, pnl:5.00 };
  const B2 = { id:'BTC-OTHER', symbol:'BTCUSD', side:'sell', qty:0.02, avg:63500, sl:64500, tp:62500, pnl:-2.00 };
  ws().emit({ type:'positions', items: [E1, B1, B2] });
  await sleep(20);

  const sentCountBeforeClick = ws().sent.length;

  // ── Pulsa "Modificar SL/TP" sobre BTC-TARGET (otro símbolo) ────────
  const btn = document.querySelector('.mbp-card[data-id="BTC-TARGET"] .mob-sltp-edit-btn');
  realClick(btn);
  await sleep(30);

  // ── Navega mediante el flujo existente, selecciona exactamente el ID
  //    solicitado, y abrir la pantalla no envía ninguna modificación ──
  assertEqual(hooks.session.currentSymbol, 'BTCUSD', 'Modificar SL/TP navega mediante el flujo real de cambio de símbolo');
  assertEqual(document.querySelector('.mob-view[data-mobile-view="trade"]').hidden, false, 'navega a Gráfico mediante el flujo existente (showView real)');
  assertEqual(String(hooks.mobileChart._selectedId), 'BTC-TARGET', 'queda seleccionada exactamente la posición solicitada por su ID real (no BTC-OTHER)');
  const sentSinceClick = ws().sent.slice(sentCountBeforeClick).map(function(s){ return JSON.parse(s).action; });
  assert(sentSinceClick.indexOf('order:update') === -1, 'abrir la pantalla nunca envía un update de SL/TP');
  assert(sentSinceClick.indexOf('order:close') === -1, 'abrir la pantalla nunca envía un cierre');

  // ── Mientras no haya historia/escala válida del símbolo nuevo, la
  //    edición permanece bloqueada (gate real de mobile_chart.js) ────
  assertEqual(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), false, 'BTCUSD aún no es editable — su historia real todavía no llegó (retraso deliberado)');

  // ── Una respuesta history ANTIGUA del símbolo anterior (EUR/USD) no
  //    habilita la edición — el contrato real (mobile_session.js,
  //    protegido/sin tocar) ya la descarta porque currentSymbol ahora
  //    es BTCUSD, no EUR/USD; se confirma aquí, no se reimplementa. ──
  const staleEurBars = [{ time: Math.floor(Date.now()/1000)-900, open:1.109, high:1.110, low:1.108, close:1.1095, volume:10 }];
  ws().emit({ type:'history', symbol:'EUR/USD', timeframe: hooks.session.currentTF, phase:'complete', data: staleEurBars });
  await sleep(20);
  assertEqual(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), false, 'una historia tardía del símbolo ANTERIOR (EUR/USD) nunca habilita la edición de BTCUSD');
  assertEqual(hooks.session.currentSymbol, 'BTCUSD', 'currentSymbol real no se contamina con el mensaje tardío de EUR/USD');

  // ── Una respuesta tardía de OTRO instrumento no relacionado tampoco
  //    la habilita ──────────────────────────────────────────────────
  const gbpBars = [{ time: Math.floor(Date.now()/1000)-900, open:1.250, high:1.251, low:1.249, close:1.2505, volume:10 }];
  ws().emit({ type:'history', symbol:'GBP/USD', timeframe: hooks.session.currentTF, phase:'complete', data: gbpBars });
  await sleep(20);
  assertEqual(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), false, 'una historia tardía de un instrumento NO relacionado (GBP/USD) tampoco habilita la edición de BTCUSD');

  // ── Llega el history correcto del símbolo correcto: se habilita para
  //    esa misma posición (BTC-TARGET), nunca la otra (BTC-OTHER) ────
  const btcBars = [{ time: Math.floor(Date.now()/1000)-900, open:63000, high:63100, low:62900, close:63050, volume:5 }];
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btcBars });
  await sleep(20);
  assertEqual(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), true, 'con el history real y correcto de BTCUSD ya cargado, la edición se habilita');
  assertEqual(String(hooks.mobileChart._selectedId), 'BTC-TARGET', 'sigue exactamente BTC-TARGET seleccionada — nunca cambió a BTC-OTHER pese a compartir símbolo');

  // ── Nunca se creó una segunda conexión/suscripción ──────────────────
  assertEqual(ws(), firstWs, 'todo el escenario ocurre sobre la MISMA conexión WebSocket real, nunca una segunda');
  const changeSymbolSends2 = ws().sent.filter(function(s){ return JSON.parse(s).action === 'change_symbol'; });
  assertEqual(changeSymbolSends2.length, 2, 'exactamente dos change_symbol reales en todo el escenario (EUR/USD inicial + BTCUSD del botón) — ninguna suscripción extra');
"""


class PositionsSlTpDelayedHistoryTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_sltp_edit_blocked_until_correct_history_arrives(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(DELAYED_HISTORY_BODY, "sltp_delay_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="sltp_delay_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 14)
