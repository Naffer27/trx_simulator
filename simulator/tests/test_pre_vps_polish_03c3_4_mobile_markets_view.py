# simulator/tests/test_pre_vps_polish_03c3_4_mobile_markets_view.py
"""
MOBILE-WATCH-INTEGRATION-01 — Mobile Watch/Markets view, visual contract
port of money-broker-movil 2/watch.html + tema.css.

Supersedes the PRE-VPS-POLISH-03C.3.4 generic (English-labeled) Markets
view this file used to test: search box, asset_class category chips,
"Favoritos" chip, instrument rows (badge/name/descriptive-name/Bid-Ask/
24H) and the header-dedup/no-second-connection guarantees are now
checked against the current visual-contract markup instead.

Architecture carried over unchanged and still verified here: ONE real
catalog (MOBILE_SYMBOLS, server-rendered from market_data/
symbol_specs.py via views.py's mobile_allowed_symbols_json — never a
second instrument list), ONE WebSocket/price authority (onQuote()/
applyPriceTickState(), never a per-row fetch/poll/selectSymbol() loop),
and the EXACT SAME favorites localStorage mechanism Gráfico's star
button already writes to (favoriteStorageKey()/isFavoriteSymbol()) —
never a second favorites store.

Real-execution tests reuse the established FakeWebSocket + real-source-
dump harness (test_mobile_close_position_isolated_bug.py). No network,
no real account activity, no server started by these tests.
"""
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user
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


def _markets_view_block(source):
    start = source.index('data-mobile-view="markets"')
    end = source.index('data-mobile-view="positions"')
    return source[start:end]


# ─────────────────────────────────────────────────────────────────────────
# A. Static structure — single catalog, no fabrication, no polling,
#    favorites reuse, header dedup. No Chrome needed.
# ─────────────────────────────────────────────────────────────────────────
class WatchStaticStructureTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.markets_block = _markets_view_block(self.html)

    def test_mobile_symbols_defined_exactly_once(self):
        self.assertEqual(self.html.count("const MOBILE_SYMBOLS = "), 1)
        self.assertIn("{{ mobile_allowed_symbols_json|safe }}", self.html)

    def test_no_second_symbol_catalog_literal(self):
        for forbidden in ("MARKETS_SYMBOLS", "MOBILE_INSTRUMENTS", "MARKET_CATALOG", "var SYMBOLS"):
            self.assertNotIn(forbidden, self.html, forbidden)

    def test_categories_derived_from_mobile_symbols_not_hardcoded(self):
        self.assertIn("function getMarketCategories", self.html)
        self.assertIn("MOBILE_SYMBOLS.forEach", self.html)
        # The reference mockup's "Índices"/"Materias primas" chip labels
        # must exist only as a *translation table* (MOBILE_CATEGORY_
        # LABELS, used only if that asset_class is ever actually
        # present) — never hardcoded into the chip-rendering loop itself.
        self.assertIn("MOBILE_CATEGORY_LABELS", self.html)
        render_start = self.html.index("function renderMarketChips(){")
        render_end = self.html.index("\n  }", render_start)
        render_body = self.html[render_start:render_end]
        self.assertNotIn("Índices", render_body)
        self.assertNotIn("Materias primas", render_body)

    def test_no_fabricated_demo_values(self):
        for forbidden in ("84,568.91", "2,314.50", "38,420.0", "chg.toFixed", "'+' + s.chg"):
            self.assertNotIn(forbidden, self.markets_block, forbidden)

    def test_no_app_js_or_demo_data_copied(self):
        self.assertNotIn("app.js", self.html)
        self.assertNotIn("MB.group(", self.html)

    def test_24h_column_always_dash_never_computed(self):
        # The literal 24H cell must be a static '—' assignment, never a
        # percentage derived from mobileChart.bars or any candle field.
        self.assertIn("chgEl.textContent = '—';", self.html)
        self.assertNotIn("chgEl.textContent = (up", self.html)
        render_start = self.html.index("function renderWatchlist(){")
        render_end = self.html.index("\n  }\n  // MOBILE-WATCH-INTEGRATION-01", render_start)
        render_body = self.html[render_start:render_end]
        self.assertNotIn(".bars", render_body)
        self.assertNotIn("toFixed(2) + '%'", render_body)

    def _js_function_body(self, fn_name):
        start = self.html.index(f"function {fn_name}(")
        start = self.html.index("{", start)
        depth = 0
        i = start
        while i < len(self.html):
            if self.html[i] == "{":
                depth += 1
            elif self.html[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.html[start:i + 1]
            i += 1
        self.fail(f"unbalanced braces while scanning function {fn_name}()")

    def test_no_second_favorites_store(self):
        # Exactly the existing favoriteStorageKey()/isFavoriteSymbol()
        # mechanism — filteredMarketSymbols() must call it, never define
        # a second localStorage key prefix.
        self.assertIn("isFavoriteSymbol(item.symbol)", self._js_function_body("filteredMarketSymbols"))
        self.assertEqual(self.html.count("function favoriteStorageKey("), 1)
        self.assertEqual(self.html.count("function isFavoriteSymbol("), 1)

    def test_no_polling_or_second_network_mechanism_in_markets_block(self):
        for forbidden in ("fetch(", "XMLHttpRequest", "setInterval(", "new WebSocket("):
            self.assertNotIn(forbidden, self.markets_block, forbidden)

    def test_never_iterates_symbols_calling_select_symbol(self):
        # The only selectSymbol() call in renderWatchlist() must be the
        # single, explicit one inside the row click handler — never a
        # loop that cycles through MOBILE_SYMBOLS to harvest prices.
        render_watchlist_body = self._js_function_body("renderWatchlist")
        self.assertEqual(render_watchlist_body.count("session.selectSymbol("), 1)

    def test_row_click_navigates_never_submits_order(self):
        render_watchlist_body = self._js_function_body("renderWatchlist")
        self.assertNotIn("session.submitOrder(", render_watchlist_body)
        self.assertNotIn("confirmRiskWarning(", render_watchlist_body)

    def test_header_dedup_css_rule_present(self):
        self.assertIn(".mob-foundation.mbw-active .mob-topbar{display:none;}", self.html)

    def test_other_views_topbar_rule_untouched(self):
        # The pre-existing Gráfico/Trade-form header-dedup rule must
        # still be present, verbatim — this round only adds a sibling
        # rule for Watch, never replaces the existing one.
        self.assertIn(".mob-foundation.mbg-active .mob-topbar{display:none;}", self.html)

    def test_watchlist_refresh_bridge_exposed_for_view_switch(self):
        self.assertIn("window.__mobRefreshWatchlist = renderWatchlist;", self.html)
        self.assertIn("window.__mobRefreshWatchlist", self.html)


# ─────────────────────────────────────────────────────────────────────────
# B. Real-execution behavior: search, categories, favorites, navigation,
#    per-symbol quote isolation, header dedup, no duplicate connection.
# ─────────────────────────────────────────────────────────────────────────
BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function gotoMarkets(){ document.querySelector('.mob-navitem[data-target="markets"]').click(); }
  function gotoTrade(){ document.querySelector('.mob-navitem[data-target="trade"]').click(); }
  function rows(){ return document.querySelectorAll('#mobWatchlist .mob-watchlist-item'); }
  function rowFor(sym){ return document.querySelector('#mobWatchlist .mob-watchlist-item[data-symbol="' + sym + '"]'); }
  function chip(cat){ return document.querySelector('.mbw-chip[data-category="' + cat + '"]'); }
  function topbarVisible(){ return getComputedStyle(document.querySelector('.mob-topbar')).display !== 'none'; }

  ws().emit({ type: 'positions', items: [] });
  await sleep(30);
  gotoMarkets();
  await sleep(20);

  // ── Header dedup: no double Money Broker/Connected bar over Watch ──
  assert(!topbarVisible(), 'header: generic topbar hidden while Watch is active');

  // ── Real catalog, all 6 currently-enabled symbols reachable ────────
  assertEqual(rows().length, 6, 'catálogo: las 6 instancias reales habilitadas aparecen con el filtro "Todos"');
  assert(!!rowFor('BTCUSD'), 'BTCUSD presente');
  assert(!!rowFor('EUR/USD'), 'EUR/USD presente');

  // ── Búsqueda por símbolo ────────────────────────────────────────────
  const search = document.getElementById('mobMarketSearch');
  search.value = 'btc'; search.dispatchEvent(new Event('input'));
  await sleep(10);
  assertEqual(rows().length, 1, 'búsqueda "btc" por símbolo -> solo BTCUSD');
  assertEqual(rows()[0].getAttribute('data-symbol'), 'BTCUSD', 'búsqueda por símbolo encuentra el símbolo correcto');

  // ── Búsqueda por nombre descriptivo ─────────────────────────────────
  search.value = 'euro'; search.dispatchEvent(new Event('input'));
  await sleep(10);
  assertEqual(rows().length, 1, 'búsqueda "euro" por nombre descriptivo -> EUR/USD');
  assertEqual(rows()[0].getAttribute('data-symbol'), 'EUR/USD', 'búsqueda por nombre encuentra el símbolo correcto');

  // ── Sin resultados -> estado vacío claro ────────────────────────────
  search.value = 'zzzznoexiste'; search.dispatchEvent(new Event('input'));
  await sleep(10);
  assertEqual(rows().length, 0, 'búsqueda sin coincidencias no deja filas');
  const emptyEl = document.getElementById('mobMarketEmpty');
  assert(!emptyEl.hidden && emptyEl.textContent.trim().length > 0, 'estado vacío visible y con texto cuando la búsqueda no encuentra nada');

  // ── Icono de filtros: acción real (restablecer) ─────────────────────
  document.getElementById('mobMarketResetBtn').click();
  await sleep(10);
  assertEqual(search.value, '', 'el icono de filtros limpia la búsqueda (acción real, no decorativa)');
  assertEqual(rows().length, 6, 'tras restablecer, vuelven las 6 instancias');

  // ── Categorías: Forex / Cripto, derivadas del catálogo real ─────────
  realClick(chip('forex'));
  await sleep(10);
  assertEqual(rows().length, 4, 'chip Forex muestra solo los 4 pares forex reales habilitados');
  Array.prototype.forEach.call(rows(), function(r){ assert(['EUR/USD','GBP/USD','USD/JPY','AUD/USD'].indexOf(r.getAttribute('data-symbol')) !== -1, 'fila forex real: ' + r.getAttribute('data-symbol')); });

  realClick(chip('crypto'));
  await sleep(10);
  assertEqual(rows().length, 2, 'chip Cripto muestra solo los 2 símbolos crypto reales habilitados');

  realClick(chip('ALL'));
  await sleep(10);
  assertEqual(rows().length, 6, 'chip Todos vuelve a mostrar el catálogo completo');

  // ── Favoritos: vacío -> estado vacío específico ─────────────────────
  realClick(chip('FAV'));
  await sleep(10);
  assertEqual(rows().length, 0, 'sin favoritos marcados, el chip Favoritos no muestra filas');
  assert(!document.getElementById('mobMarketEmpty').hidden, 'estado vacío visible para Favoritos sin elementos');
  assert(document.getElementById('mobMarketEmpty').textContent.indexOf('favorito') !== -1, 'el estado vacío de Favoritos menciona favoritos, no un mensaje genérico');

  // ── Favoritos: EXACTAMENTE el almacenamiento real de Gráfico ───────
  const acc = (window.__TRADE_CONFIG__ && window.__TRADE_CONFIG__.accountId != null) ? window.__TRADE_CONFIG__.accountId : 'anon';
  localStorage.setItem('mobFavorite:' + acc + ':BTCUSD', '1');
  realClick(chip('ALL'));
  realClick(chip('FAV'));
  await sleep(10);
  assertEqual(rows().length, 1, 'tras marcar BTCUSD como favorito (misma clave real), el chip Favoritos lo muestra');
  assertEqual(rows()[0].getAttribute('data-symbol'), 'BTCUSD', 'la fila mostrada en Favoritos es la correcta');

  realClick(chip('ALL'));
  await sleep(10);

  // ── Cotizaciones: solo la fila seleccionada recibe precio real ─────
  realClick(rowFor('EUR/USD'));
  await sleep(20);
  assertEqual(hooks.session.currentSymbol, 'EUR/USD', 'tocar la fila selecciona el símbolo correcto en la sesión real');
  // Navega a Gráfico mediante el flujo existente — nunca envía una orden.
  assert(document.querySelector('.mob-view[data-mobile-view="trade"]').hidden === false, 'tocar una fila abre Gráfico mediante el flujo existente');
  assertEqual(JSON.parse(ws().sent[ws().sent.length-1] || '{}').action, 'change_symbol', 'el único efecto de red de tocar la fila es el change_symbol real de selectSymbol(), nunca una orden');

  gotoMarkets();
  await sleep(10);
  ws().emit({ type: 'price', symbol: 'EUR/USD', bid: 1.10025, ask: 1.10041, source: 'test' });
  await sleep(20);
  const eurRow = rowFor('EUR/USD');
  assert(eurRow.querySelector('.wl-price').textContent !== '—', 'EUR/USD (seleccionado) muestra un bid real tras el tick');
  assertEqual(eurRow.querySelector('.wl-price').textContent, '1.10025', 'precisión real de 5 decimales conservada para EUR/USD');
  const btcRow = rowFor('BTCUSD');
  assertEqual(btcRow.querySelector('.wl-price').textContent, '—', 'BTCUSD (no seleccionado) nunca muestra el precio de EUR/USD ni ningún otro número');
  assertEqual(btcRow.querySelector('.wl-bidask').textContent, '—', 'BTCUSD (no seleccionado) muestra "—" en ask — limitación real: solo se recibe precio del símbolo seleccionado');

  // 24H siempre "—", nunca un color/porcentaje de demo.
  assertEqual(eurRow.querySelector('.wl-chg').textContent, '—', '24H siempre "—" — no existe variación real con periodo definido');
  assertEqual(eurRow.querySelector('.wl-chg').className.indexOf('up'), -1, '24H nunca lleva una clase de color "up"/"down" fabricada');

  // ── Volver de Gráfico a Watch: favorito cambiado se refleja, sin
  //    segunda conexión ───────────────────────────────────────────────
  localStorage.setItem('mobFavorite:' + acc + ':EUR/USD', '1');
  gotoTrade();
  await sleep(10);
  gotoMarkets();
  await sleep(10);
  assertEqual(ws(), firstWs, 'volver a Watch nunca crea una segunda conexión WebSocket (misma instancia real)');
  realClick(chip('FAV'));
  await sleep(10);
  assertEqual(rows().length, 2, 'el favorito marcado mientras se estaba en Gráfico ya aparece al volver a Watch (misma clave real, sin snapshot obsoleto)');
"""


class WatchRealExecutionTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_search_categories_favorites_navigation_and_quote_isolation(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "watch_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="watch_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


# ─────────────────────────────────────────────────────────────────────────
# C. Favorites are scoped per real account — two different accounts
#    (two different real accountId values, same mechanism) never see
#    each other's favorites.
# ─────────────────────────────────────────────────────────────────────────
ACCOUNT_SEPARATION_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  ws().emit({ type: 'positions', items: [] });
  await sleep(20);
  document.querySelector('.mob-navitem[data-target="markets"]').click();
  await sleep(10);

  const acc = window.__TRADE_CONFIG__.accountId;
  assertEqual(typeof acc, 'number', 'precondición: accountId real (no "anon"/null) para esta cuenta');

  // Favorito de OTRA cuenta real (clave con el MISMO formato real,
  // accountId distinto) nunca debe aparecer para esta sesión.
  localStorage.setItem('mobFavorite:' + (acc + 999) + ':BTCUSD', '1');
  document.querySelector('.mbw-chip[data-category="FAV"]').click();
  await sleep(10);
  assertEqual(document.querySelectorAll('#mobWatchlist .mob-watchlist-item').length, 0, 'el favorito guardado bajo OTRO accountId no se filtra para esta cuenta');

  // El favorito de ESTA cuenta sí aparece.
  localStorage.setItem('mobFavorite:' + acc + ':BTCUSD', '1');
  document.querySelector('.mbw-chip[data-category="ALL"]').click();
  document.querySelector('.mbw-chip[data-category="FAV"]').click();
  await sleep(10);
  assertEqual(document.querySelectorAll('#mobWatchlist .mob-watchlist-item').length, 1, 'el favorito guardado bajo el accountId real de ESTA cuenta sí se refleja');
"""


class WatchFavoritesAccountSeparationTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_favorites_scoped_to_real_account_id(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(ACCOUNT_SEPARATION_BODY, "acc_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=10000, result_id="acc_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
