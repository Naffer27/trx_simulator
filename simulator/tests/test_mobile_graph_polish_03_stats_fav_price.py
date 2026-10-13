# simulator/tests/test_mobile_graph_polish_03_stats_fav_price.py
"""
MOBILE-GRAPH-POLISH-03 — Máx/Mín/Vol, favoritos locales, precio actual
amarillo. Directed, real-execution tests.

Reuses the established FakeWebSocket + real-source-dump harness
(test_mobile_close_position_isolated_bug.py). No network, no real
account, no server.
"""
from django.test import TestCase

from simulator.tests.test_mobile_close_position_isolated_bug import (
    CHROME_PATH,
    REAL_LWC_SOURCE,
    _dump_mobile_html,
    _driver_wrap,
    _inject_fake_ws_and_hooks,
    _inline_real_sources,
    _run_driver_and_extract,
)


def _bars_js_window(base, n, step_seconds, tag, newest_age_seconds=0):
    """n bars ending `newest_age_seconds` ago, spaced step_seconds apart,
    each carrying a real, deterministic volume so sums are checkable."""
    return (
        "(function(){ const bars=[]; const now=Math.floor(Date.now()/1000);"
        f" const end=now-{newest_age_seconds}; let seed={tag}; let price={base};"
        " function rnd(){ seed=(seed*16807)%2147483647; return seed/2147483647; }"
        f" for(let i=0;i<{n};i++){{"
        f"   const t=end-({n}-1-i)*{step_seconds};"
        "   const o=price, c=o+(rnd()-0.5)*(price*0.001);"
        "   const hi=Math.max(o,c)+rnd()*(price*0.0005), lo=Math.min(o,c)-rnd()*(price*0.0005);"
        "   bars.push({time:t, open:o, high:hi, low:lo, close:c, volume: 10+i});"
        "   price=c;"
        " }"
        " return bars; })()"
    )


BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  function statHigh(){ return document.getElementById('mobStatHigh').textContent; }
  function statLow(){ return document.getElementById('mobStatLow').textContent; }
  function statVol(){ return document.getElementById('mobStatVol').textContent; }
  function star(){ return document.getElementById('mobStarBtn'); }
  function starFill(){ const p = star().querySelector('svg path'); return p ? p.getAttribute('fill') : null; }

  // ── 1. Máx/Mín/Vol: cálculo de 24h DESACTIVADO en este bloque —
  //      quedan "—" siempre: al limpiar, con poca historia, con mucha
  //      historia (48h) y tras cambiar de símbolo. Se conectarán en un
  //      bloque separado con una fuente de datos adecuada para esa
  //      ventana; esta prueba solo verifica el apagado, no investiga
  //      cobertura/continuidad.
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  assertEqual(statHigh(), '—', '1: Máx queda "—" al limpiar, antes de que llegue nada nuevo');
  assertEqual(statLow(), '—', '1: Mín queda "—" al limpiar');
  assertEqual(statVol(), '—', '1: Vol queda "—" al limpiar');
  const btc40 = """ + _bars_js_window("63000", 40, 900, 1) + """;
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc40 });
  await sleep(40);
  assertEqual(statHigh(), '—', '1: Máx sigue "—" con historia real ya cargada (cálculo desactivado en este bloque)');
  assertEqual(statLow(), '—', '1: Mín sigue "—" por el mismo motivo');
  assertEqual(statVol(), '—', '1: Vol sigue "—" por el mismo motivo');

  const btc48h = """ + _bars_js_window("63000", 48, 3600, 6) + """;
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc48h });
  await sleep(40);
  assertEqual(statHigh(), '—', '1: Máx sigue "—" incluso con 48h de historia cargada — el cálculo está desactivado, no es un problema de cobertura');
  assertEqual(statLow(), '—', '1: Mín sigue "—" por el mismo motivo');

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('EUR/USD');
  await sleep(10);
  assertEqual(statHigh(), '—', '1: Máx queda "—" tras cambiar de símbolo');
  const eurBars = """ + _bars_js_window("1.1200", 30, 3600, 2) + """;
  ws().emit({ type:'history', symbol:'EUR/USD', timeframe: hooks.session.currentTF, phase:'complete', data: eurBars });
  await sleep(40);
  assertEqual(statHigh(), '—', '1: Máx sigue "—" para EUR/USD con historia real cargada');
  assertEqual(statVol(), '—', '1: Vol sigue "—" para EUR/USD');

  // Vuelve a BTCUSD con btc40 cargado — precondición para los bloques
  // 4 (favoritos) y 5 (precio amarillo) que siguen a continuación.
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc40 });
  await sleep(40);

  // ── 4. Favoritos: alternar, independencia por símbolo, persistencia
  //      del mecanismo subyacente (localStorage) ────────────────────
  assertEqual(star().getAttribute('aria-pressed'), 'false', '4: BTCUSD no es favorito al principio');
  assertEqual(starFill(), 'none', '4: estrella sin relleno cuando no es favorito');
  realClick(star());
  await sleep(10);
  assertEqual(star().getAttribute('aria-pressed'), 'true', '4: BTCUSD queda marcado como favorito tras el click');
  assertEqual(starFill(), '#F5C518', '4: estrella amarilla rellena cuando SÍ es favorito');

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('ETHUSD');
  await sleep(10);
  ws().emit({ type:'history', symbol:'ETHUSD', timeframe: hooks.session.currentTF, phase:'complete', data: """ + _bars_js_window("2480", 10, 900, 5) + """ });
  await sleep(20);
  assertEqual(star().getAttribute('aria-pressed'), 'false', '4: ETHUSD es un símbolo independiente — no hereda el favorito de BTCUSD');

  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc40 });
  await sleep(20);
  assertEqual(star().getAttribute('aria-pressed'), 'true', '4: BTCUSD sigue marcado como favorito al volver a seleccionarlo (mecanismo de persistencia real — localStorage)');

  // Verificación directa del mecanismo de persistencia subyacente
  // (equivalente a lo que una recarga real comprobaría: localStorage
  // sobrevive, está scopeado por cuenta+símbolo).
  const favKeys = Object.keys(localStorage).filter(function(k){ return k.indexOf('mobFavorite:') === 0; });
  assert(favKeys.length >= 1, '4: la preferencia se guarda realmente en localStorage (nunca solo en memoria)');
  assert(favKeys.some(function(k){ return k.indexOf(':BTCUSD') !== -1; }), '4: la clave de localStorage está scopeada por símbolo (BTCUSD)');

  realClick(star());
  await sleep(10);
  assertEqual(star().getAttribute('aria-pressed'), 'false', '4: un segundo click quita el favorito');
  assertEqual(starFill(), 'none', '4: estrella vuelve a contorno al quitar el favorito');

  // ── 5. Precio actual amarillo: fuente, actualización, sin duplicar ─
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc40 });
  await sleep(40);
  const B1 = { id:'B1', symbol:'BTCUSD', side:'buy', avg:63000, sl:62000, tp:64000, qty:0.01, pnl:0 };
  ws().emit({ type:'positions', items:[B1] });
  await sleep(20);

  const lastTag = document.querySelector('.mbgc-last-tag');
  const lastLine = document.querySelector('.mbgc-last-line');
  assert(!!lastTag, '5: la etiqueta amarilla del precio actual existe');
  assert(!!lastLine, '5: la línea amarilla del precio actual existe');
  const realClose = hooks.mobileChart.bars[hooks.mobileChart.bars.length - 1].close;
  assertEqual(parseFloat(lastTag.textContent), Number(realClose.toFixed(2)), '5: el texto de la etiqueta es EXACTAMENTE el close real de la última vela (nunca bid/ask/avg/entry)');
  assert(document.querySelectorAll('.mbgc-last-tag').length === 1, '5: nunca hay dos etiquetas de precio actual (sin duplicado)');

  // Nunca confundir con la Entry (B1.avg=63000, distinto de la última vela)
  const entryLine = hooks.mobileChart._entryLines && hooks.mobileChart._entryLines['B1'];
  assert(!!entryLine, '5: precondición — la línea de Entry de B1 existe por separado');
  // API real de Lightweight Charts: un IPriceLine expone su precio vía
  // .options().price, no como propiedad plana (a diferencia del
  // FakeSeries simplificado de otros tests de este suite).
  assertEqual(entryLine.options().price, 63000, '5: la línea de Entry sigue en avg (63000), la amarilla no la sobreescribió');

  // Se actualiza con una vela nueva (candle_new), sin crear un segundo
  // elemento ni tocar las líneas SL/TP de la posición.
  const newBar = { time: btc40[btc40.length-1].time + 900, open: 63500, high: 63600, low: 63400, close: 63550, volume: 5 };
  hooks.mobileChart.appendCandle(newBar);
  await sleep(20);
  assertEqual(parseFloat(document.querySelector('.mbgc-last-tag').textContent), 63550, '5: la etiqueta amarilla se actualiza con la vela nueva real (candle_new)');
  assert(document.querySelectorAll('.mbgc-last-tag').length === 1, '5: sigue habiendo exactamente una etiqueta tras la actualización');
  const slPill = document.querySelector('.mbgc-pill-sl');
  const tpPill = document.querySelector('.mbgc-pill-tp');
  assert(!!slPill && slPill.textContent.indexOf('62000') !== -1, '5: el pill de SL de la posición no cambió (sigue en 62000)');
  assert(!!tpPill && tpPill.textContent.indexOf('64000') !== -1, '5: el pill de TP de la posición no cambió (sigue en 64000)');

  // Sin posición seleccionada en absoluto, la etiqueta amarilla debe
  // seguir existiendo (es del chart, no de la posición).
  hooks.mobileChart.setPositions([], null);
  await sleep(10);
  assert(!!document.querySelector('.mbgc-last-tag'), '5: la etiqueta amarilla sigue presente aunque no haya ninguna posición seleccionada');
"""


# ── Brecha 3 — la estrella de favoritos no debe depender de onQuote() ──
# Este body nunca emite un mensaje 'price'/'tick' (la única vía real de
# onQuote() fuera de la llamada síncrona que selectSymbol() ya dispara
# por diseño — mobile_session.js, protegido, sin cambios). Reproduce
# exactamente la ruta de auto-selección tras un reload real:
# onPositions() recibe un snapshot con una posición abierta ANTES de que
# el usuario haya elegido ningún símbolo (session.currentSymbol es
# null), y debe reflejar el favorito correcto para ese símbolo auto-
# seleccionado de inmediato — a través de la llamada directa añadida en
# el propio sitio de onPositions(), nunca solo porque onQuote() llegó.
FAVORITE_ONPOSITIONS_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  function star(){ return document.getElementById('mobStarBtn'); }

  assertEqual(hooks.session.currentSymbol, null, 'precondición: ningún símbolo elegido todavía (página recién cargada)');

  // Favorito pre-existente para BTCUSD, guardado como si una sesión
  // anterior ya lo hubiera marcado (mismo formato real de
  // favoriteStorageKey(): 'mobFavorite:<accountId>:<symbol>').
  const acc = (window.__TRADE_CONFIG__ && window.__TRADE_CONFIG__.accountId != null) ? window.__TRADE_CONFIG__.accountId : 'anon';
  localStorage.setItem('mobFavorite:' + acc + ':BTCUSD', '1');

  // Snapshot de posiciones real con BTCUSD abierta — dispara la rama
  // de auto-select de onPositions() (currentSymbol era null). Ningún
  // 'price'/'tick' se emite nunca en este test.
  const B1 = { id:'B1', symbol:'BTCUSD', side:'buy', avg:63000, sl:null, tp:null, qty:0.01, pnl:0 };
  ws().emit({ type:'positions', items:[B1] });
  await sleep(15);

  assertEqual(hooks.session.currentSymbol, 'BTCUSD', 'onPositions() auto-seleccionó BTCUSD (la única posición abierta)');
  assertEqual(star().getAttribute('aria-pressed'), 'true', 'brecha 3: la estrella ya refleja el favorito guardado, inmediatamente tras el auto-select — nunca se emitió ningún tick/onQuote real en este test');
"""


class MobileGraphPolish03Tests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")
        if not REAL_LWC_SOURCE:
            self.skipTest("real Lightweight Charts fixture not found")

    def test_stats_favorites_and_last_price(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, REAL_LWC_SOURCE)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=20000, result_id="results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")

    def test_favorite_star_onpositions_autoselect_without_onquote(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, REAL_LWC_SOURCE)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(FAVORITE_ONPOSITIONS_BODY, "results2")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=10000, result_id="results2",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
