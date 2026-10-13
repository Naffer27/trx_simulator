# simulator/tests/test_mobile_trade_form_integration.py
"""
MOBILE-TRADE-FORM-01 — dedicated Trade screen, directed integration
tests.

Reproduces money-broker-movil 2/trade.html's composition (instrument /
Vender-Comprar / tipo de orden / volumen / SL-TP / info disponible /
CTA) inside mobile.html's existing view-switcher
(data-mobile-view="tradeform"), reached only via the Gráfico topbar's
Trade button (mobOpenTradeFormBtn) and closed via its own × (never a
new WebSocket, never a second financial computation — every send goes
through the EXISTING session.submitOrder()/requestRiskPreview()/
confirmRiskWarning()/cancelRiskWarning()).

Real-execution tests, same established FakeWebSocket + real-source-dump
harness as the rest of this suite (test_mobile_close_position_isolated_
bug.py). No network, no real account, no server.
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


def _bars_js(base=63000.0, n=20, seed=3):
    return (
        "(function(){ const bars=[]; const now=Math.floor(Date.now()/1000); const tf=900;"
        f" let price={base};"
        " for(let i=0;i<" + str(n) + ";i++){ bars.push({time: now-(" + str(n) + "-i)*tf,"
        " open:price, high:price+10, low:price-10, close:price, volume:10}); }"
        " return bars; })()"
    )


# 1 — abrir Trade y volver al Gráfico sin perder estado ni duplicar
# conexiones; símbolo/lado/volumen reflejados; abrir/cerrar no envía
# nada.
TEST1_OPEN_CLOSE_PRESERVES_STATE = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  function sentActions(){ return ws().sent.map(function(s){ return JSON.parse(s).action; }); }

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: """ + _bars_js() + """ });
  await sleep(20);
  const B1 = { id:'B1', symbol:'BTCUSD', side:'buy', avg:63000, sl:null, tp:null, qty:0.01, pnl:0 };
  ws().emit({ type:'positions', items:[B1] });
  await sleep(20);

  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '1: precondición — Gráfico listo antes de abrir Trade');
  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '1: precondición — B1 dibujada antes de abrir Trade');
  const barsBefore = hooks.mobileChart.bars.length;
  const socketBefore = ws();
  const sentBefore = sentActions().length;

  const openBtn = document.getElementById('mobOpenTradeFormBtn');
  assert(!!openBtn, '1: el botón Trade existe');
  realClick(openBtn);
  await sleep(20);

  const tradeView = document.querySelector('.mob-view[data-mobile-view="tradeform"]');
  const graphView = document.querySelector('.mob-view[data-mobile-view="trade"]');
  assert(!tradeView.hidden, '1: la pantalla Trade queda visible tras pulsar el botón');
  assert(graphView.hidden, '1: el Gráfico queda oculto (no destruido) mientras Trade está abierto');
  assertEqual(document.getElementById('mobTfSymbolName').textContent, 'BTCUSD', '1: símbolo real reflejado en Trade');
  assertEqual(document.getElementById('mobTfQtyOut').textContent, '0.01', '1: volumen por defecto reflejado');
  assertEqual(document.querySelector('#mobTfSide button[data-side="buy"]').getAttribute('aria-selected'), 'true', '1: lado por defecto = Comprar');
  assertEqual(ws(), socketBefore, '1: abrir Trade NO crea un WebSocket nuevo');

  const closeBtn = document.getElementById('mobTradeFormCloseBtn');
  realClick(closeBtn);
  await sleep(20);
  assert(tradeView.hidden, '1: × oculta la pantalla Trade');
  assert(!graphView.hidden, '1: × vuelve a mostrar el Gráfico');
  assertEqual(hooks.mobileChart.bars.length, barsBefore, '1: las velas del Gráfico se conservan tras volver');
  assert(hooks.mobileChart.isSlTpEditableFor('BTCUSD'), '1: el estado "listo" del Gráfico se conserva tras volver');
  assert(!!(hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1']), '1: B1 sigue dibujada tras volver');
  assertEqual(hooks.session.currentSymbol, 'BTCUSD', '1: el símbolo seleccionado se conserva tras volver');
  assertEqual(ws(), socketBefore, '1: cerrar Trade NO crea un WebSocket nuevo');
  assertEqual(sentActions().length, sentBefore, '1: abrir y cerrar el formulario no envía ningún mensaje nuevo');
"""

# 2 — símbolo, lado, volumen y SL/TP correctos en el envío real.
TEST2_SUBMIT_CORRECT_FIELDS = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  // El CTA/los botones de confirmación quedan, en una ventana real
  // corta, por debajo del pliegue de .mbgt-content (overflow-y:auto —
  // mismo comportamiento que la referencia) — scrollIntoView() antes
  // de tocarlos es exactamente lo que un usuario real haría.
  function tfClick(id){ const el=document.getElementById(id); el.scrollIntoView({block:'center'}); realClick(el); }
  function sentOrders(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action==='order:new'; }); }

  hooks.session.selectSymbol('ETHUSD');
  await sleep(10);
  realClick(document.getElementById('mobOpenTradeFormBtn'));
  await sleep(20);

  realClick(document.querySelector('#mobTfSide button[data-side="sell"]'));
  await sleep(10);
  assertEqual(document.querySelector('#mobTfSide button[data-side="sell"]').getAttribute('aria-selected'), 'true', '2: Vender queda seleccionado');

  realClick(document.getElementById('mobTfQtyUpBtn'));
  await sleep(10);
  const qtyShown = document.getElementById('mobTfQtyOut').textContent;

  document.getElementById('mobTfSlInput').value = '2500.50';
  document.getElementById('mobTfSlInput').dispatchEvent(new Event('input', {bubbles:true}));
  document.getElementById('mobTfTpInput').value = '2300.25';
  document.getElementById('mobTfTpInput').dispatchEvent(new Event('input', {bubbles:true}));
  await sleep(10);

  tfClick('mobTfCtaBtn');
  await sleep(20);

  const orders = sentOrders();
  assertEqual(orders.length, 1, '2: exactamente un order:new enviado');
  assertEqual(orders[0].symbol, 'ETHUSD', '2: símbolo correcto en el envío');
  assertEqual(orders[0].side, 'sell', '2: lado correcto en el envío');
  assertEqual(String(orders[0].qty), qtyShown, '2: volumen enviado coincide con el mostrado');
  assertEqual(orders[0].sl, 2500.5, '2: SL correcto en el envío');
  assertEqual(orders[0].tp, 2300.25, '2: TP correcto en el envío');
"""

# 3 — vista previa de riesgo, confirmación obligatoria, rechazo,
# prevención de doble envío.
TEST3_RISK_CONFIRM_REJECT_DOUBLE_SEND = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  // El CTA/los botones de confirmación quedan, en una ventana real
  // corta, por debajo del pliegue de .mbgt-content (overflow-y:auto —
  // mismo comportamiento que la referencia) — scrollIntoView() antes
  // de tocarlos es exactamente lo que un usuario real haría.
  function tfClick(id){ const el=document.getElementById(id); el.scrollIntoView({block:'center'}); realClick(el); }
  function sentOrders(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action==='order:new'; }); }
  function sentPreviews(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action==='order:risk_preview'; }); }

  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  realClick(document.getElementById('mobOpenTradeFormBtn'));
  // session.requestRiskPreview() está debounced 200ms (trading_core.js) —
  // esperar más que eso antes de comprobar el envío real.
  await sleep(260);
  assert(sentPreviews().length >= 1, '3: abrir Trade solicita una vista previa de riesgo real');

  ws().emit({ type:'risk_preview', symbol:'BTCUSD', qty:0.01, risk_level:'LOW', margin_required: 42.30, exposure_pct: 3.5, notional: 630.0 });
  await sleep(20);
  // mbgStripMoney() (ya existente, aprobado para la franja Balance/P&L)
  // reutilizado aquí — siempre dos decimales, igual que la referencia.
  assertEqual(document.getElementById('mobTfMargin').textContent, '$42.30', '3: margen requerido real reflejado (mbgStripMoney ya existente)');
  assert(document.getElementById('mobTfNominal').textContent.indexOf('630') !== -1, '3: nominal real reflejado (no qty*price calculado aquí)');

  // Doble click rápido — solo debe producir un envío real (order:new),
  // el segundo click debe ser absorbido por _orderSending/setTfSending.
  tfClick('mobTfCtaBtn');
  tfClick('mobTfCtaBtn');
  await sleep(10);
  assertEqual(sentOrders().length, 1, '3: doble click solo produce un order:new');
  assert(document.getElementById('mobTfCtaBtn').disabled, '3: CTA deshabilitado mientras el envío está en curso');

  // El backend exige confirmación de riesgo obligatoria.
  ws().emit({ type:'risk_warning', exposure_pct: 42 });
  await sleep(20);
  assert(!document.getElementById('mobTfRiskConfirm').hidden, '3: el panel de confirmación obligatoria se muestra');
  assert(!document.getElementById('mobTfCtaBtn').disabled, '3: CTA vuelve a estar habilitado mientras se espera confirmación');
  assertEqual(sentOrders().length, 1, '3: risk_warning no reenvía nada por sí solo');

  tfClick('mobTfRiskConfirmBtn');
  await sleep(20);
  assertEqual(sentOrders().length, 2, '3: confirmar reenvía exactamente un order:new más (risk_confirmed)');
  assertEqual(sentOrders()[1].risk_confirmed, true, '3: el reenvío confirma risk_confirmed:true');

  // Rechazo del backend — nunca se declara éxito.
  ws().emit({ type:'order_rejected', message:'price_unavailable' });
  await sleep(20);
  assert(document.getElementById('mobTfStatus').textContent.toLowerCase().indexOf('rechazada') !== -1, '3: el rechazo se muestra, nunca como éxito');
  assert(!document.getElementById('mobTfCtaBtn').disabled, '3: CTA se reactiva tras el rechazo');
"""

# 4 — cancelar la confirmación de riesgo no envía nada; cerrar con una
# confirmación pendiente la cancela explícitamente (nunca queda
# pendiente en silencio).
TEST4_CANCEL_NEVER_SENDS = """
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  // El CTA/los botones de confirmación quedan, en una ventana real
  // corta, por debajo del pliegue de .mbgt-content (overflow-y:auto —
  // mismo comportamiento que la referencia) — scrollIntoView() antes
  // de tocarlos es exactamente lo que un usuario real haría.
  function tfClick(id){ const el=document.getElementById(id); el.scrollIntoView({block:'center'}); realClick(el); }
  function sentOrders(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action==='order:new'; }); }

  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  realClick(document.getElementById('mobOpenTradeFormBtn'));
  await sleep(10);

  // El propio intento inicial YA envía un order:new real (eso es lo que
  // el backend responde con risk_warning) — lo que "cancelar" nunca debe
  // hacer es añadir un SEGUNDO envío (el reenvío confirmado).
  tfClick('mobTfCtaBtn');
  await sleep(10);
  assertEqual(sentOrders().length, 1, '4: precondición — el intento inicial ya envió un order:new real');
  ws().emit({ type:'risk_warning', exposure_pct: 55 });
  await sleep(20);
  assert(!document.getElementById('mobTfRiskConfirm').hidden, '4: confirmación visible tras risk_warning');

  tfClick('mobTfRiskCancelBtn');
  await sleep(20);
  assert(document.getElementById('mobTfRiskConfirm').hidden, '4: cancelar oculta la confirmación');
  assertEqual(sentOrders().length, 1, '4: cancelar NO añade un segundo order:new (el reenvío confirmado)');

  // Segundo intento: dejar una confirmación pendiente y cerrar con ×.
  tfClick('mobTfCtaBtn');
  await sleep(10);
  assertEqual(sentOrders().length, 2, '4: precondición — segundo intento inicial, segundo order:new real');
  ws().emit({ type:'risk_warning', exposure_pct: 60 });
  await sleep(20);
  assert(!document.getElementById('mobTfRiskConfirm').hidden, '4: segunda confirmación visible');
  realClick(document.getElementById('mobTradeFormCloseBtn'));
  await sleep(10);
  assertEqual(sentOrders().length, 2, '4: cerrar con una confirmación pendiente NO añade ningún envío nuevo');
  assert(document.getElementById('mobTfRiskConfirm').hidden, '4: cerrar oculta/limpia la confirmación pendiente');
"""


def _run(test_case, body, result_id):
    html = _dump_mobile_html(test_case)
    html = _inline_real_sources(html, FAKE_LWC)
    html = _inject_fake_ws_and_hooks(html, test_case)
    driver = _driver_wrap(body, result_id)
    return _run_driver_and_extract(
        test_case, html, driver, window_size="390,844", virtual_time_budget=15000, result_id=result_id,
    )


class MobileTradeFormIntegrationTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _assert_all_pass(self, data):
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")

    def test_1_open_close_preserves_state_no_duplicate_socket(self):
        self._assert_all_pass(_run(self, TEST1_OPEN_CLOSE_PRESERVES_STATE, "t1_results"))

    def test_2_submit_sends_correct_fields(self):
        self._assert_all_pass(_run(self, TEST2_SUBMIT_CORRECT_FIELDS, "t2_results"))

    def test_3_risk_preview_confirm_reject_double_send(self):
        self._assert_all_pass(_run(self, TEST3_RISK_CONFIRM_REJECT_DOUBLE_SEND, "t3_results"))

    def test_4_cancel_and_close_never_send(self):
        self._assert_all_pass(_run(self, TEST4_CANCEL_NEVER_SENDS, "t4_results"))
