# simulator/tests/test_mobile_sltp_disconnect_reconnect.py
"""
MOBILE — FIX AISLADO DE DESCONEXIÓN/RECONEXIÓN.

Causa: ni clear() ni ningún otro mecanismo invalidaba la disponibilidad
de edición SL/TP cuando la conexión se perdía — y el reenvío de
reconexión (onopen) usaba action:'change_symbol'/'change_timeframe' por
ws.send() directo, nunca session.selectSymbol()/selectTimeframe(), así
que nunca pasaba por clear(). El chart permanecía "listo" usando velas
previas a la caída durante toda la ventana de caída+reconexión+reenvío.
Además, un socket reemplazado por una reconexión podía, en teoría,
seguir entregando eventos (onmessage/onopen/onclose/onerror) que
tocaran el estado de la sesión vigente, sin que nada comprobara si ese
socket seguía siendo el actual.

Fix (solo Mobile):
  - mobile_chart.js: invalidateForConnectionChange() — reutiliza clear()
    (confirmado que éste nunca toca posiciones canónicas/selección/
    pendingSlTp) y, ADEMÁS, cancela una previsualización NO enviada
    (_edit.mode !== 'sending') — algo que clear() por sí solo no hace,
    porque para cambios de símbolo/timeframe ya lo hace el siguiente
    setPositions(), pero una desconexión no tiene ese siguiente paso.
    Una edición YA enviada (mode:'sending') se deja intacta — su
    seguimiento real es pendingSlTp en mobile.html, independiente de
    este campo.
  - mobile.html: onStatusChange() llama a esa invalidación para
    cualquier estado que no sea 'CONNECTED'.
  - mobile_session.js: connect() captura el socket concreto en una
    constante local (`sock`) en el momento de crearlo; sus 4 handlers
    (onopen/onmessage/onerror/onclose) comprueban `this.ws !== sock`
    antes de hacer nada — un socket reemplazado nunca puede tocar el
    estado de la sesión vigente, entregar mensajes, cambiar el status,
    ni programar una reconexión que interfiera con la actual.

Explícitamente NO abordado aquí (dejado pendiente, por instrucción
directa): el caso BTC#1→EUR→BTC#2 con respuesta tardía de BTC#1 DENTRO
de la MISMA conexión — la identidad de socket no lo resuelve (distingue
conexiones, no solicitudes dentro de una misma conexión) y no se
implementa ninguna solución para ese caso en este bloque.

Real-execution tests, mismo arnés establecido (FakeWebSocket real +
fuentes reales sin modificar aparte de las 3 líneas/métodos de arriba).
Sin red real, sin cuenta real, sin servidor.
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
  function curSock(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return curSock() && curSock().readyState === 1; }, 3000);

  const BTC = 'BTCUSD';

  function switchSymbol(sym){
    hooks.mobileChart.clear();
    hooks.session.selectSymbol(sym);
    hooks.refreshPositionLines();
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
  function emitHistoryOn(sock, sym, tf, bars, phase){
    sock.emit({ type: 'history', symbol: sym, timeframe: tf, phase: phase || 'complete', data: bars });
  }
  function sentUpdatesOn(sock){ return sock.sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:update'; }); }
  function addAffordance(field){ return document.querySelector('.mbgc-add-' + field); }
  function loadingPill(){
    const els = Array.from(document.querySelectorAll('.mbgc-pill.mbgc-add'));
    return els.find(function(el){ return el.textContent.indexOf('Cargando') !== -1; }) || null;
  }
  const B1 = { id: 'B1', symbol: BTC, side: 'buy', avg: 63000, sl: null, tp: null, qty: 0.01, pnl: 0 };
  curSock().emit({ type: 'positions', items: [B1] });
  await sleep(20);

  switchSymbol(BTC);
  const tf = hooks.session.currentTF;
  const sockA = curSock();
  emitHistoryOn(sockA, BTC, tf, scaleBars(63000, 40, 1), 'complete');
  await sleep(60);
  assert(!!addAffordance('tp'), 'setup: BTCUSD listo sobre sockA antes de empezar');

  // ───────────────────────────────────────────────────────────────
  // 1. gráfico listo -> desconexión -> SL/TP bloqueado de inmediato
  // ───────────────────────────────────────────────────────────────
  {
    assert(hooks.mobileChart.isSlTpEditableFor(BTC), '1: listo antes de desconectar (sockA)');
    sockA.readyState = 3;
    sockA.onclose();
    await sleep(10);
    results.push({label:'1: socket que originó el evento = sockA (el vigente en ese momento)', pass:true});
    assert(!hooks.mobileChart.isSlTpEditableFor(BTC), '1: bloqueado inmediatamente tras onclose de sockA, sin esperar ningún reintento');
    assert(!addAffordance('tp'), '1: "+ TP" ya no está disponible');
    assert(!!loadingPill(), '1: se muestra "Cargando gráfico" de inmediato');
  }

  // Reconectar (sockB) para preparar el siguiente escenario.
  hooks.session.connect();
  await sleep(20);
  const sockB = curSock();
  results.push({label:'tras reconectar: nuevo socket (sockB) distinto de sockA: ' + (sockB !== sockA), pass:true});
  emitHistoryOn(sockB, BTC, tf, scaleBars(63000, 40, 2), 'complete');
  await sleep(60);
  assert(!!addAffordance('tp'), 'setup fase 2: listo de nuevo sobre sockB');

  // ───────────────────────────────────────────────────────────────
  // 2. previsualización NO enviada -> desconexión -> se cancela, 0 envíos
  // ───────────────────────────────────────────────────────────────
  {
    realClick(addAffordance('tp'));
    await sleep(20);
    assert(!!hooks.mobileChart._edit, '2: previsualización iniciada (sin enviar) sobre sockB');
    assertEqual(hooks.mobileChart._edit.mode, 'edit', '2: el modo es "edit" (no enviada, no "sending")');
    const beforeSends = sentUpdatesOn(sockB).length;

    sockB.readyState = 3;
    sockB.onclose();
    await sleep(10);
    results.push({label:'2: socket que originó el evento = sockB; envíos en sockB antes/después: ' + beforeSends + '/' + sentUpdatesOn(sockB).length, pass:true});
    assertEqual(hooks.mobileChart._edit, null, '2: la previsualización se cancela (_edit queda null) al desconectar');
    assertEqual(sentUpdatesOn(sockB).length, beforeSends, '2: 0 order:update producidos por la desconexión');
    assert(!document.querySelector('.mbgc-edit-confirm'), '2: no queda ningún toolbar de Confirmar visible');
  }

  // Reconectar (sockC) y dejar listo para el siguiente escenario.
  hooks.session.connect();
  await sleep(20);
  const sockC = curSock();
  emitHistoryOn(sockC, BTC, tf, scaleBars(63000, 40, 3), 'complete');
  await sleep(60);
  assert(!!addAffordance('tp'), 'setup fase 3: listo de nuevo sobre sockC');

  // ───────────────────────────────────────────────────────────────
  // 3. modificación YA enviada -> desconexión -> no se declara éxito,
  //    no se reenvía automáticamente
  // ───────────────────────────────────────────────────────────────
  let pendingIdAfterSend = null;
  {
    realClick(addAffordance('tp'));
    await sleep(20);
    realClick(document.querySelector('.mbgc-edit-confirm'));
    await sleep(20);
    const sentBefore = sentUpdatesOn(sockC);
    assertEqual(sentBefore.length, 1, '3: exactamente 1 order:update enviado sobre sockC antes de desconectar');
    assertEqual(sentBefore[0].id, 'B1', '3: el envío corresponde a B1');
    assert(!!hooks.pendingSlTpRef(), '3: pendingSlTp (mobile.html) queda registrado tras el envío');
    pendingIdAfterSend = hooks.pendingSlTpRef().id;
    const requestedTpAfterSend = sentBefore[0].tp;
    assertEqual(hooks.mobileChart._edit && hooks.mobileChart._edit.mode, 'sending', '3: _edit queda en modo "sending" tras confirmar');

    sockC.readyState = 3;
    sockC.onclose();
    await sleep(10);
    results.push({label:'3: socket que originó el evento = sockC; envíos en sockC antes/después de desconectar: ' + sentBefore.length + '/' + sentUpdatesOn(sockC).length, pass:true});
    assertEqual(sentUpdatesOn(sockC).length, 1, '3: sigue siendo exactamente 1 — la desconexión no reenvía ni duplica');
    assert(!!hooks.pendingSlTpRef(), '3: pendingSlTp NO se borra por desconectar — sigue rastreado por id');
    assertEqual(hooks.pendingSlTpRef().id, pendingIdAfterSend, '3: sigue siendo el MISMO id (B1), no se reinterpreta como otra edición');

    // Resolución canónica REAL del envío de la prueba 3 (nunca
    // fabricada, nunca por reconectar) — llega ahora, sobre lo que en
    // ese momento sea la conexión vigente, confirmando que
    // pendingSlTp se resuelve por id normalmente. Se resuelve aquí
    // (no al final del archivo) precisamente para dejar un estado
    // limpio antes de las pruebas 4-9, que verifican un ASPECTO
    // DISTINTO (la conexión/reconexión) y no deben confundirse con
    // "el campo TP de B1 sigue bloqueado porque sigue 'sending'" —
    // eso ya quedó demostrado arriba.
    curSock().emit({ type: 'positions', items: [{ id: 'B1', symbol: BTC, side: 'buy', avg: 63000, sl: null, tp: requestedTpAfterSend, qty: 0.01, pnl: 0 }] });
    await sleep(30);
    assertEqual(hooks.pendingSlTpRef(), null, '3: pendingSlTp se resuelve (limpia) al llegar el snapshot canónico real que confirma el mismo id/valor solicitado');
  }

  // ───────────────────────────────────────────────────────────────
  // 4. reconexión con mismo símbolo/timeframe -> permanece bloqueado
  //    hasta aplicar histórico ACEPTADO de la conexión NUEVA
  // ───────────────────────────────────────────────────────────────
  let sockD;
  {
    hooks.session.connect();
    await sleep(20);
    sockD = curSock();
    results.push({label:'4: nuevo socket (sockD) distinto de sockC: ' + (sockD !== sockC) + '; símbolo/timeframe vigentes siguen siendo los mismos: ' + (hooks.session.currentSymbol === BTC && hooks.session.currentTF === tf), pass:true});
    assert(!addAffordance('sl'), '4: aún bloqueado justo tras reconectar, aunque símbolo/timeframe coincidan con los de antes');
    assert(!!loadingPill(), '4: "Cargando gráfico" mientras sockD no tiene histórico propio todavía');
    assertEqual(sentUpdatesOn(sockD).length, 0, '4: 0 order:update producidos solo por reconectar');

    emitHistoryOn(sockD, BTC, tf, scaleBars(63000, 40, 4), 'complete');
    await sleep(60);
    assert(!!addAffordance('sl'), '4: listo de nuevo una vez aplicado el histórico ACEPTADO de sockD (la conexión nueva)');
  }

  // ───────────────────────────────────────────────────────────────
  // 5. onmessage tardío del socket anterior (sockC, ya reemplazado) ->
  //    ignorado
  // ───────────────────────────────────────────────────────────────
  {
    const barsBefore = JSON.stringify(hooks.mobileChart.bars.slice(-1)[0]);
    const foreignBars = scaleBars(99999, 40, 99);
    sockC.emit({ type: 'history', symbol: BTC, timeframe: tf, phase: 'complete', data: foreignBars });
    await sleep(30);
    const barsAfter = JSON.stringify(hooks.mobileChart.bars.slice(-1)[0]);
    results.push({label:'5: evento originado en sockC (ya reemplazado por sockD); bars sin cambio: ' + (barsBefore === barsAfter), pass:true});
    assertEqual(barsAfter, barsBefore, '5: el onmessage tardío de sockC no modifica las velas vigentes (las de sockD)');
    assert(!!addAffordance('sl'), '5: sigue listo con los datos de sockD, intactos');
    assertEqual(sentUpdatesOn(sockC).length, 1, '5: sockC sigue mostrando exactamente su único envío histórico (el de la prueba 3) — nada nuevo');
    assertEqual(sentUpdatesOn(sockD).length, 0, '5: sockD no produjo ningún order:update por esto');
  }

  // ───────────────────────────────────────────────────────────────
  // 6. onopen tardío del socket anterior (sockC) -> no altera la
  //    conexión vigente
  // ───────────────────────────────────────────────────────────────
  {
    const wsBefore = hooks.session.ws;
    const connectingBefore = hooks.session.connecting;
    sockC.onopen();
    await sleep(20);
    results.push({label:'6: evento originado en sockC; session.ws sigue siendo sockD: ' + (hooks.session.ws === sockD), pass:true});
    assertEqual(hooks.session.ws, wsBefore, '6: this.ws no cambia por el onopen tardío de sockC');
    assertEqual(hooks.session.connecting, connectingBefore, '6: el estado "connecting" no se altera');
    assertEqual(sentUpdatesOn(sockD).length, 0, '6: 0 order:update producidos');
  }

  // ───────────────────────────────────────────────────────────────
  // 7. onclose tardío del socket anterior (sockC) -> no marca caída la
  //    conexión nueva ni programa una reconexión incorrecta
  // ───────────────────────────────────────────────────────────────
  {
    const sockRefBefore = curSock();
    sockC.onclose();
    await sleep(30);
    const sockRefAfter = curSock();
    results.push({label:'7: evento originado en sockC; ningún socket nuevo fue creado como efecto secundario: ' + (sockRefBefore === sockRefAfter), pass:true});
    assertEqual(sockRefAfter, sockRefBefore, '7: el onclose tardío de sockC no dispara una reconexión que reemplace a sockD');
    assertEqual(hooks.session.ws, sockD, '7: this.ws sigue siendo sockD');
    assert(!!addAffordance('sl'), '7: sockD sigue listo — no se vio afectado');
  }

  // ───────────────────────────────────────────────────────────────
  // 8. onerror tardío del socket anterior (sockC) -> no altera el
  //    estado vigente
  // ───────────────────────────────────────────────────────────────
  {
    const wsBefore = hooks.session.ws;
    sockC.onerror();
    await sleep(20);
    results.push({label:'8: evento originado en sockC; session.ws sin cambio: ' + (hooks.session.ws === wsBefore), pass:true});
    assertEqual(hooks.session.ws, wsBefore, '8: this.ws no cambia por el onerror tardío de sockC');
    assert(!!addAffordance('sl'), '8: sockD sigue listo tras el onerror tardío de sockC');
    assertEqual(sentUpdatesOn(sockD).length, 0, '8: 0 order:update producidos');
  }

  // ───────────────────────────────────────────────────────────────
  // 9. histórico válido del socket NUEVO (sockD) -> habilita de nuevo
  //    SL/TP, pese a todo el ruido de los pasos 5-8
  // ───────────────────────────────────────────────────────────────
  {
    assert(hooks.mobileChart.isSlTpEditableFor(BTC), '9: isSlTpEditableFor(BTCUSD) verdadero — el ruido de sockC no lo afectó');
    realClick(addAffordance('sl'));
    await sleep(20);
    const confirmBtn = document.querySelector('.mbgc-edit-confirm');
    assert(!!confirmBtn, '9: tocar "+ SL" sobre sockD funciona con normalidad');
    realClick(confirmBtn);
    await sleep(20);
    const sentD = sentUpdatesOn(sockD);
    results.push({label:'9: socket que originó el envío = sockD; total de order:update en sockD: ' + sentD.length, pass:true});
    assertEqual(sentD.length, 1, '9: exactamente 1 order:update enviado sobre la conexión vigente (sockD)');
    assertEqual(sentD[0].id, 'B1', '9: corresponde a B1');
  }
"""


class MobileDisconnectReconnectSlTpTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_disconnect_reconnect_sltp_protection(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "disc_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="disc_results",
        )
        import os
        if os.environ.get("MB_DIAG"):
            for r in data["results"]:
                print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(
            failures, [],
            f"{data['failCount']} of {len(data['results'])} disconnect/reconnect assertions failed: {failures}",
        )
        self.assertGreaterEqual(data["passCount"], 25)
