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

  // ── 1. BTCUSD: 40 velas de 15m, TODAS dentro de las últimas 24h ────
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  assertEqual(statHigh(), '—', '1: Máx vuelve a "—" inmediatamente al limpiar (clear()), antes de que llegue nada nuevo');
  const btc40 = """ + _bars_js_window("63000", 40, 900, 1) + """; // 40*900s = 10h -> todas dentro de 24h
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: btc40 });
  await sleep(40);
  const expHi = Math.max.apply(null, btc40.map(function(b){return b.high;}));
  const expLo = Math.min.apply(null, btc40.map(function(b){return b.low;}));
  const expVol = btc40.reduce(function(s,b){return s+b.volume;}, 0);
  assert(Math.abs(Number(statHigh()) - expHi) < 1, '1: Máx numéricamente correcto (±1) frente al máximo real de las 40 velas (todas < 24h)');
  assert(Math.abs(Number(statLow()) - expLo) < 1, '1: Mín numéricamente correcto (±1) frente al mínimo real');
  assert(statVol().indexOf('$') === -1, '1: el volumen NUNCA lleva "$" (no es un campo monetario)');
  assert(statVol().indexOf('BTC') !== -1, '1: BTCUSD es crypto -> el volumen SÍ se muestra, con el activo base real (BTC)');
  const volNum = parseFloat(statVol().replace(/,/g, ''));
  assert(Math.abs(volNum - expVol) < 0.01, '1: volumen mostrado es la suma real de las velas de la ventana de 24h (' + volNum + ' vs ' + expVol + ')');

  // ── 2. Cambio a EUR/USD: no deben verse las estadísticas de BTC ────
  // Clic real sobre la fila del watchlist (no hooks.mobileChart.clear()
  // suelto) — así se ejercita el MISMO flujo real que ya llama a
  // resetDailyStats()/renderFavoriteStar() justo al limpiar.
  function clickWatchlistRow(sym){
    const marketsNavBtn = document.querySelector('.mob-navitem[data-target="markets"]');
    if (marketsNavBtn) marketsNavBtn.click();
    const row = document.querySelector('.mob-watchlist-item[data-symbol="' + sym + '"]');
    if (!row) throw new Error('watchlist row not found for ' + sym);
    realClick(row);
  }
  clickWatchlistRow('EUR/USD');
  assertEqual(statHigh(), '—', '2: Máx vuelve a "—" en cuanto empieza el cambio de símbolo (antes de llegar nada de EUR/USD)');
  assertEqual(statVol(), '—', '2: Vol vuelve a "—" en cuanto empieza el cambio de símbolo');
  await sleep(10);
  const eurBars = """ + _bars_js_window("1.1200", 30, 900, 2) + """;
  ws().emit({ type:'history', symbol:'EUR/USD', timeframe: hooks.session.currentTF, phase:'complete', data: eurBars });
  await sleep(40);
  const expHiEur = Math.max.apply(null, eurBars.map(function(b){return b.high;}));
  assert(Math.abs(Number(statHigh()) - expHiEur) < 0.01, '2: Máx ahora refleja EUR/USD, nunca el BTCUSD anterior');
  assert(statHigh() !== '—', '2: Máx tiene un valor real para EUR/USD');
  assertEqual(statVol(), '—', '2: EUR/USD no es crypto -> volumen queda "—" (unidad del proveedor no documentada), nunca un número inventado');

  // ── 3. Ventana de 24h real: velas viejas (>24h) quedan EXCLUIDAS ───
  hooks.mobileChart.clear();
  hooks.session.selectSymbol('BTCUSD');
  await sleep(10);
  // 10 velas de 1h "antiguas" (centradas ~30h atrás) + 5 velas de 1h
  // "recientes" (última hora) — solo las 5 recientes deben contar.
  const oldBars = """ + _bars_js_window("60000", 10, 3600, 3, 30 * 3600) + """;
  const recentBars = """ + _bars_js_window("63000", 5, 3600, 4, 0) + """;
  const mixed = oldBars.concat(recentBars);
  ws().emit({ type:'history', symbol:'BTCUSD', timeframe: hooks.session.currentTF, phase:'complete', data: mixed });
  await sleep(40);
  const expHiRecentOnly = Math.max.apply(null, recentBars.map(function(b){return b.high;}));
  const expVolRecentOnly = recentBars.reduce(function(s,b){return s+b.volume;}, 0);
  assert(Math.abs(Number(statHigh()) - expHiRecentOnly) < 1, '3: Máx excluye velas de más de 24h (usa solo la ventana real reciente)');
  assert(Math.abs(parseFloat(statVol()) - expVolRecentOnly) < 0.01, '3: Vol excluye velas de más de 24h en la suma');
  assert(Math.abs(Number(statHigh()) - Math.max.apply(null, oldBars.map(function(b){return b.high;}))) > 100, '3: Máx NO es el máximo de las velas viejas (prueba de que sí se filtró por tiempo)');

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
