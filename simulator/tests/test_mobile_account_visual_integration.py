# simulator/tests/test_mobile_account_visual_integration.py
"""
MOBILE-ACCOUNT-INTEGRATION-01 — Mobile Cuenta view, visual contract port
of money-broker-movil 2/cuenta.html + tema.css, plus the Día/Noche
bridge to the REAL, shared, protected theme mechanism (base.html's own
anti-flash bootstrap + desktop.html's own toggle: localStorage
['mb_theme'], <html data-theme>, the 'mb:themechange' CustomEvent).

Verifies: real account_id/account_type/leverage (never "Cuenta 51"/
"STANDARD"/"50x" fixed values), the SAME six real account:update fields
(balance/equity/margin_used/free_margin/upnl/leverage) already used by
Gráfico's strip and Posiciones' summary (never recomputed a second
time), margin_level read correctly for "Nivel de margen" (confirmed
real and unconditional on every account:update — correcting an earlier
round's own wrong comment, left untouched elsewhere since Posiciones'
own composition/behavior is out of this round's scope), used_margin_pct
gated by a client-side mirror of the real MARGIN_ENGINE_TYPES set
(never a fabricated 0%/100% for an ungated account_type), real
Depositar/Retirar destinations (no account/wallet id confusion, no
simulated submission), and the Día/Noche bridge itself: no second
storage key/event, immediate propagation to every other Mobile view and
the chart's own canvas/overlays via the existing dormant
MutationObserver, no reconnect/reload/recreate, persistence of the
real preference across a fresh load.

Real-execution tests reuse the established FakeWebSocket + real-source-
dump harness (test_mobile_close_position_isolated_bug.py). No network,
no real account activity, no deposit/withdraw ever executed, no server
started by these tests.
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


def _account_view_block(html):
    start = html.index('<div class="mob-view" data-mobile-view="account"')
    # The account view is the last `.mob-view`; bound it by the next
    # top-level sibling that always follows it in this file (the bottom
    # tabbar's own opening comment/markup) rather than a brittle fixed
    # length.
    end = html.index("<!-- PRE-VPS-POLISH-03C.3.2 — app-shell view switcher", start)
    return html[start:end]


# ─────────────────────────────────────────────────────────────────────────
# A. Static structure — real reuse, no fixed account values, no second
#    theme system, correct margin_level/used_margin_pct gating. No
#    Chrome needed.
# ─────────────────────────────────────────────────────────────────────────
class AccountStaticStructureTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.account_block = _account_view_block(self.html)

    def test_header_dedup_css_rule_present(self):
        self.assertIn(".mob-foundation.mba-active .mob-topbar{display:none;}", self.html)

    def test_other_views_topbar_rules_untouched(self):
        self.assertIn(".mob-foundation.mbg-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbw-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbp-active .mob-topbar{display:none;}", self.html)
        self.assertIn(".mob-foundation.mbh-active .mob-topbar{display:none;}", self.html)

    def test_no_fixed_account_values_real_template_vars_used(self):
        self.assertIn("{{ account_id }}", self.account_block)
        self.assertIn("{{ account.account_type }}", self.account_block)
        self.assertIn("{{ account.leverage }}", self.account_block)
        # Never the reference's own literal demo values hardcoded here.
        self.assertNotIn(">51<", self.account_block)
        self.assertNotIn(">STANDARD<", self.account_block)
        self.assertNotIn(">50x<", self.account_block)

    def test_six_shared_metric_ids_reused_not_duplicated(self):
        for element_id in ("mobBalance", "mobEquity", "mobMargin", "mobFree", "mobUpnl", "mobLeverage"):
            self.assertEqual(
                self.html.count(f'id="{element_id}"'), 1,
                f"{element_id} must still be declared exactly once in the whole file (reused, never duplicated)",
            )

    def test_deposit_withdraw_use_real_named_urls_never_fragment_or_account_id(self):
        self.assertIn("{% url 'simulator:deposit' %}", self.account_block)
        self.assertIn("{% url 'simulator:withdraw' %}", self.account_block)
        self.assertNotIn('href="#depositar"', self.account_block)
        self.assertNotIn('href="#retirar"', self.account_block)
        # Never the POST-only account->wallet transfer endpoint (which
        # would require inventing a financial amount form) as an actual
        # URL target — this file's own explanatory comment is allowed
        # to name it in prose.
        self.assertNotIn("{% url 'simulator:withdraw_account'", self.account_block)

    def test_margin_level_not_read_directly_inside_on_account(self):
        # onAccount() itself must stay free of the literal field read —
        # Cuenta's own logic lives in mbaRenderAccountExtras(), called
        # from onAccount() by name only, so Posiciones' own already-
        # committed static test (asserting onAccount() never reads the
        # real margin-level field directly) keeps passing unmodified.
        on_account_body = _js_function_body(self.html, "onAccount")
        self.assertNotIn("msg.margin_level", on_account_body)
        self.assertIn("mbaRenderAccountExtras(msg, equity, margin)", on_account_body)

    def test_margin_level_read_verbatim_in_its_own_function_never_recomputed(self):
        extras_body = _js_function_body(self.html, "mbaRenderAccountExtras")
        self.assertIn("msg.margin_level", extras_body)
        # Never equity/margin recomputed into a level here.
        self.assertNotIn("equity/margin*100", extras_body.replace(" ", ""))
        self.assertNotIn("equity/margin_used*100", extras_body.replace(" ", ""))

    def test_used_margin_pct_gated_by_real_margin_engine_types_mirror(self):
        extras_body = _js_function_body(self.html, "mbaRenderAccountExtras")
        self.assertIn("mbaMarginEngineTypes", extras_body)
        for t in ("RETAIL", "ECN", "STANDARD", "DEMO", "CRYPTO"):
            self.assertIn(t, extras_body)
        self.assertIn("msg.used_margin_pct", extras_body)
        self.assertIn("is-neutral", extras_body)

    def test_theme_bridge_reuses_real_storage_key_and_event_no_second_system(self):
        self.assertIn("localStorage.setItem('mb_theme'", self.html)
        self.assertIn("mb:themechange", self.html)
        # Never a second/parallel storage key for this.
        self.assertNotIn("mb_mobile_theme", self.html)
        self.assertNotIn("mobile_theme", self.html.lower().replace("mobtheme", ""))

    def test_theme_bridge_never_touches_desktop_only_toggle_id(self):
        # Must never duplicate/redefine Desktop's own toggle DOM element
        # (this file's own explanatory comments are allowed to name it
        # in prose, e.g. when describing the sequence they mirror).
        self.assertNotIn('id="termThemeToggle"', self.html)
        self.assertNotIn("getElementById('termThemeToggle')", self.html)

    def test_theme_seg_buttons_present_with_real_values(self):
        self.assertIn('data-theme-value="noche"', self.account_block)
        self.assertIn('data-theme-value="dia"', self.account_block)
        self.assertIn('id="mobAcctThemeSeg"', self.account_block)

    def test_grid_money_cells_use_approved_formatter_not_shared_usd(self):
        # Owner-reported round 2 fix: mobBalance/mobEquity/mobMargin/
        # mobFree/mobUpnl (now Cuenta-exclusive, confirmed by the id-
        # uniqueness test above) must use this file's own approved
        # mbgStripMoney() — real minus, always 2 decimals, no "+" on
        # zero — never the shared usd() helper (trading_core.js,
        # protected, left untouched: no minimum fraction digits, bare
        # ASCII "-"). Scoped to onAccount()'s own body so a match
        # elsewhere (e.g. a comment) can't produce a false pass.
        on_account_body = _js_function_body(self.html, "onAccount")
        self.assertIn("mbgStripMoney(msg.balance,false)", on_account_body.replace(" ", ""))
        self.assertIn("mbgStripMoney(equity,false)", on_account_body.replace(" ", ""))
        self.assertIn("mbgStripMoney(margin,false)", on_account_body.replace(" ", ""))
        self.assertIn("mbgStripMoney(free,false)", on_account_body.replace(" ", ""))
        self.assertIn("mbgStripMoney(upnl,true)", on_account_body.replace(" ", ""))
        # Scoped to the specific elBalance/elEquity/elMargin/elFree/
        # elUpnl statements — NOT a bare substring check, since Posiciones'
        # own untouched elPosEquity/elPosMargin/elPosFree lines a few
        # lines below legitimately still call usd(equity)/usd(margin)/
        # usd(free) (same literal substrings, different, out-of-scope
        # elements) and would otherwise produce a false failure here.
        self.assertNotIn("elBalance.textContent=usd(msg.balance)", on_account_body.replace(" ", ""))
        self.assertNotIn("elEquity.textContent=usd(equity)", on_account_body.replace(" ", ""))
        self.assertNotIn("elMargin.textContent=usd(margin)", on_account_body.replace(" ", ""))
        self.assertNotIn("elFree.textContent=usd(free)", on_account_body.replace(" ", ""))
        compact = on_account_body.replace(" ", "")
        self.assertEqual(compact.count("elUpnl.textContent="), 1, "elUpnl.textContent must be assigned exactly once")
        self.assertIn("elUpnl.textContent=mbgStripMoney(upnl,true);", compact)

    def test_shared_usd_formatter_itself_untouched(self):
        # The protected, shared trading_core.js formatter is never
        # edited — only which formatter Cuenta's own cells call.
        with open("simulator/static/simulator/trade/trading_core.js", encoding="utf-8") as f:
            core_src = f.read()
        self.assertIn(
            "const usd=v=>v==null?'—':('$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2}));",
            core_src,
        )

    def test_deposit_withdraw_underline_removed_locally_link_semantics_kept(self):
        self.assertIn(".mba-funds a{", self.html)
        # The rule that drops the underline must live on the real <a>
        # selector itself (never e.g. swapped to a <button>/<span>) and
        # be scoped to .mba-funds — Depositar/Retirar are the ONLY <a>
        # elements anywhere in this file (confirmed separately), so
        # this can never affect another screen.
        self.assertEqual(self.html.count("<a class=\"mba-dep\""), 1)
        self.assertEqual(self.html.count("<a class=\"mba-ret\""), 1)
        self.assertIn("text-decoration:none", self.html)

    def test_deposit_withdraw_focus_visible_ring_added_not_removed(self):
        # A dedicated, Cuenta-local :focus-visible rule must exist (the
        # shared button/input:focus-visible rule does not cover <a>) —
        # keyboard focus must stay visible after removing the underline.
        self.assertIn(".mba-funds a:focus-visible{outline:2px solid var(--mb-focus);outline-offset:2px;}", self.html)
        # And the pre-existing shared rule covering the Día/Noche
        # buttons (real <button> elements) must remain exactly as is —
        # never touched by this round's link-focus addition.
        self.assertIn(
            ".mob-foundation button:focus-visible,\n  .mob-foundation input:focus-visible{\n    outline:2px solid var(--mb-focus);\n    outline-offset:2px;\n  }",
            self.html,
        )


# ─────────────────────────────────────────────────────────────────────────
# B. Real execution — real account:update fields (positive/negative/zero/
#    absent), connected/disconnected, nivel de margen, uso de margen
#    gating, Día/Noche propagation + persistence + chart retheme without
#    reconnect, cross-screen coherence, Depositar/Retirar hrefs (never
#    clicked/executed).
# ─────────────────────────────────────────────────────────────────────────
BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function gotoAccount(){ document.querySelector('.mob-navitem[data-target="account"]').click(); }
  function gotoWatch(){ document.querySelector('.mob-navitem[data-target="markets"]').click(); }
  function gotoPositions(){ document.querySelector('.mob-navitem[data-target="positions"]').click(); }
  function topbarVisible(){ return getComputedStyle(document.querySelector('.mob-topbar')).display !== 'none'; }

  // ── Resumen de cuenta: id/tipo/apalancamiento reales, nunca fijos ──
  gotoAccount();
  await sleep(10);
  assert(!topbarVisible(), 'header: generic topbar hidden while Cuenta is active');

  // ── Ausente, luego positivo real ────────────────────────────────────
  assertEqual(document.getElementById('mobAcctEquityBig').textContent, '—', 'equity grande "—" antes del primer account:update real');
  assertEqual(document.getElementById('mobAcctUplBig').textContent, '—', 'P&L grande "—" antes del primer mensaje');
  assertEqual(document.getElementById('mobAcctMarginLevel').textContent, '—', 'nivel de margen "—" antes del primer mensaje');
  assertEqual(document.getElementById('mobAcctUsedPct').textContent, '—', 'uso de margen "—" antes del primer mensaje');

  ws().emit({ type:'account:update', balance: 972.76, equity: 972.34, margin_used: 41.72, free_margin: 930.62, upnl: -0.42, pnl_unreal: -0.42, leverage: 50, account_type: 'STANDARD', margin_level: 2331.11, used_margin_pct: 4.3 });
  await sleep(20);
  assertEqual(document.getElementById('mobAcctEquityBig').textContent, '$972.34', 'equity grande real, sin recálculo');
  assertEqual(document.getElementById('mobAcctUplBig').textContent, '−$0.42', 'P&L negativo real conserva el signo real');
  assert(document.getElementById('mobAcctUplBig').classList.contains('neg'), 'clase neg aplicada');
  assertEqual(document.getElementById('mobBalance').textContent, '$972.76', 'Balance (grid compartido) real, dos decimales');
  assertEqual(document.getElementById('mobEquity').textContent, '$972.34', 'Equity (grid compartido) real, dos decimales');
  assertEqual(document.getElementById('mobMargin').textContent, '$41.72', 'Margen usado (grid compartido) real, dos decimales');
  assertEqual(document.getElementById('mobFree').textContent, '$930.62', 'Margen libre (grid compartido) real, dos decimales');
  assertEqual(document.getElementById('mobUpnl').textContent, '−$0.42', 'P&L de la cuadrícula usa el MISMO formateador aprobado que el P&L principal (signo real, dos decimales)');
  assertEqual(document.getElementById('mobUpnl').textContent, document.getElementById('mobAcctUplBig').textContent, 'ya NO hay diferencia de formato entre el P&L principal y el de la cuadrícula');
  assert(document.getElementById('mobUpnl').classList.contains('neg'), 'clase neg aplicada también en la celda de la cuadrícula');
  assertEqual(document.getElementById('mobLeverage').textContent, '50x', 'Apalancamiento (grid compartido) real');
  assertEqual(document.getElementById('mobAcctMarginLevel').textContent, '2,331%', 'nivel de margen real (msg.margin_level, nunca recalculado)');
  assertEqual(document.getElementById('mobAcctUsedPct').textContent, '4.3%', 'uso de margen real para account_type gateado (STANDARD)');
  assertEqual(document.getElementById('mobAcctFreePct').textContent, '95.7%', 'libre de margen real (100 - usado)');
  assert(!document.getElementById('mobAcctMarginBarWrap').classList.contains('is-neutral'), 'barra NO neutra con account_type real gateado');
  assertEqual(document.getElementById('mobAcctMarginBar').style.width, '4.3%', 'ancho real de la barra de uso de margen');

  // ── Cero real (nunca "—") y tipo de cuenta NO gateado ──────────────
  ws().emit({ type:'account:update', balance: 0, equity: 0, margin_used: 0, free_margin: 0, upnl: 0, pnl_unreal: 0, leverage: 50, account_type: 'CHALLENGE', margin_level: 0, used_margin_pct: 0 });
  await sleep(20);
  assertEqual(document.getElementById('mobAcctEquityBig').textContent, '$0.00', 'equity cero real se muestra como cero, nunca "—"');
  assertEqual(document.getElementById('mobAcctUplBig').textContent, '$0.00', 'P&L cero real, sin signo');
  assert(!document.getElementById('mobAcctUplBig').classList.contains('pos') && !document.getElementById('mobAcctUplBig').classList.contains('neg'), 'cero real no lleva clase pos ni neg');
  // ── Cero real -> $0.00 (dos decimales, SIN "+") en las 5 celdas
  //    monetarias de la cuadrícula, no solo en los números grandes ──
  assertEqual(document.getElementById('mobBalance').textContent, '$0.00', 'Balance cero real: $0.00, dos decimales, nunca "$0"');
  assertEqual(document.getElementById('mobEquity').textContent, '$0.00', 'Equity cero real: $0.00');
  assertEqual(document.getElementById('mobMargin').textContent, '$0.00', 'Margen usado cero real: $0.00');
  assertEqual(document.getElementById('mobFree').textContent, '$0.00', 'Margen libre cero real: $0.00');
  assertEqual(document.getElementById('mobUpnl').textContent, '$0.00', 'P&L de la cuadrícula cero real: $0.00, sin "+"');
  assert(!document.getElementById('mobUpnl').classList.contains('pos') && !document.getElementById('mobUpnl').classList.contains('neg'), 'P&L cero real en la cuadrícula no lleva clase pos ni neg');
  assertEqual(document.getElementById('mobAcctMarginLevel').textContent, '—', 'nivel de margen "—" sin margen en uso (margin_used<=0), nunca 0%');
  assertEqual(document.getElementById('mobAcctUsedPct').textContent, '—', 'uso de margen "—" para account_type NO gateado (CHALLENGE), nunca 0% inventado');
  assertEqual(document.getElementById('mobAcctFreePct').textContent, '—', 'libre de margen "—" para account_type NO gateado');
  assert(document.getElementById('mobAcctMarginBarWrap').classList.contains('is-neutral'), 'barra neutra para account_type NO gateado');

  // ── Valores sin dos decimales "de fábrica" -> el formateador SIEMPRE
  //    completa a dos decimales reales (nunca redondea ni inventa) ───
  ws().emit({ type:'account:update', balance: 1000, equity: 999.5, margin_used: 50, free_margin: 949.5, upnl: 25, pnl_unreal: 25, leverage: 10, account_type: 'STANDARD', margin_level: 1999, used_margin_pct: 5 });
  await sleep(20);
  assertEqual(document.getElementById('mobBalance').textContent, '$1,000.00', 'Balance entero real se completa a dos decimales reales, nunca "$1,000"');
  assertEqual(document.getElementById('mobEquity').textContent, '$999.50', 'Equity con un solo decimal real se completa a dos, nunca "$999.5"');
  assertEqual(document.getElementById('mobUpnl').textContent, '+$25.00', 'P&L positivo real: signo + y dos decimales reales, nunca "+$25"');
  assertEqual(document.getElementById('mobAcctUplBig').textContent, '+$25.00', 'el P&L principal coincide con el de la cuadrícula también en el caso positivo');

  // ── Conectado real ──────────────────────────────────────────────────
  // onStatusChange ya fue invocado con CONNECTED por el bootstrap real
  // de session.connect(); confirma que el badge ya refleja ese estado
  // real (no una simulación de este test).
  assertEqual(document.getElementById('mobAcctConn').textContent, 'CONECTADO', 'badge de conexión real ya refleja CONNECTED');
  assert(!document.getElementById('mobAcctConn').classList.contains('is-down'), 'sin clase is-down mientras está conectado');

  // ── Coherencia entre pantallas: mismos campos en Gráfico/Posiciones ─
  gotoPositions();
  await sleep(10);
  ws().emit({ type:'account:update', balance: 5000, equity: 5010.5, margin_used: 100, free_margin: 4910.5, upnl: 10.5, pnl_unreal: 10.5, leverage: 20, account_type: 'STANDARD', margin_level: 5010.5, used_margin_pct: 2.0 });
  await sleep(20);
  // Mismo valor real (equity), comparado NUMÉRICAMENTE — Posiciones
  // conserva su propio formateador preexistente (usd(), fuera de
  // alcance de este ajuste, sin mínimo de decimales) mientras que
  // Cuenta ahora usa el formateador aprobado (mbgStripMoney, siempre
  // dos decimales); los dígitos reales deben coincidir igual, solo la
  // presentación de ceros finales difiere entre pantallas.
  function toNum(txt){ return Number(txt.replace(/[^0-9.\-]/g, '')); }
  assertEqual(toNum(document.getElementById('mobPosEquity').textContent), toNum(document.getElementById('mobEquity').textContent), 'Equity de Posiciones coincide numéricamente con el mismo campo real mostrado en Cuenta');
  gotoAccount();
  await sleep(10);
  assertEqual(document.getElementById('mobAcctEquityBig').textContent, '$5,010.50', 'Equity grande de Cuenta sigue el mismo valor real tras volver');
  assertEqual(document.getElementById('mobGraphBalance').textContent, '$5,000.00', 'tira de Gráfico sigue el mismo balance real (coherencia entre vistas)');

  // ── Depositar/Retirar: destinos reales, nunca ejecutados ───────────
  assertEqual(document.querySelector('.mba-dep').getAttribute('href'), '/deposit/', 'Depositar enlaza al destino real de Wallet, sin id de cuenta');
  assertEqual(document.querySelector('.mba-ret').getAttribute('href'), '/withdraw/', 'Retirar enlaza al destino real de Wallet, sin id de cuenta');
  assertEqual(document.querySelector('.mba-dep').tagName, 'A', 'Depositar sigue siendo un <a> real (semántica de enlace intacta)');
  assertEqual(document.querySelector('.mba-ret').tagName, 'A', 'Retirar sigue siendo un <a> real (semántica de enlace intacta)');
  assertEqual(getComputedStyle(document.querySelector('.mba-dep')).textDecorationLine, 'none', 'Depositar sin subrayado');
  assertEqual(getComputedStyle(document.querySelector('.mba-ret')).textDecorationLine, 'none', 'Retirar sin subrayado');
  // El foco visible por teclado se conserva: :focus-visible produce un
  // contorno real (outline-style != "none"), aunque jsdom-less Chrome
  // headless no simula ":focus-visible" vía .focus() de forma fiable —
  // se verifica la REGLA CSS en sí (declarada, no solo "existe algún
  // estilo"), que es lo que realmente garantiza el contorno al navegar
  // con teclado.
  const depFocusRule = Array.from(document.styleSheets).some(function(sheet){
    try {
      return Array.from(sheet.cssRules).some(function(rule){
        return rule.selectorText === '.mba-funds a:focus-visible' && /outline/.test(rule.style.cssText);
      });
    } catch(e) { return false; }
  });
  assert(depFocusRule, 'existe una regla :focus-visible real con outline para Depositar/Retirar (el contorno de foco por teclado no se eliminó)');
  // El selector Día/Noche usa <button> real -> sigue cubierto por la
  // regla compartida .mob-foundation button:focus-visible (no tocada
  // esta ronda) — confirma explícitamente que NO se le quitó el
  // contorno de foco (no es un defecto, según indicó el dueño).
  const themeBtnFocusRule = Array.from(document.styleSheets).some(function(sheet){
    try {
      return Array.from(sheet.cssRules).some(function(rule){
        return rule.selectorText && rule.selectorText.indexOf('.mob-foundation button:focus-visible') !== -1 && /outline/.test(rule.style.cssText);
      });
    } catch(e) { return false; }
  });
  assert(themeBtnFocusRule, 'la regla compartida de foco para <button> (cubre el selector Día/Noche) sigue intacta');

  // ── Día/Noche: refleja la preferencia real ya persistida al cargar ──
  assertEqual(document.documentElement.getAttribute('data-theme'), 'light', 'la preferencia real persistida (localStorage mb_theme) ya fijó <html data-theme> ANTES de este script, vía el bootstrap protegido de base.html');
  assertEqual(document.querySelector('.mob-foundation').getAttribute('data-mb-theme'), 'dia', 'el puente de Cuenta ya sincronizó el atributo LOCAL data-mb-theme con la preferencia real al cargar');
  assertEqual(document.querySelector('.mba-theme-seg button[data-theme-value="dia"]').getAttribute('aria-selected'), 'true', 'el botón Día refleja el tema activo real al cargar');
  assertEqual(document.querySelector('.mba-theme-seg button[data-theme-value="noche"]').getAttribute('aria-selected'), 'false', 'el botón Noche NO queda marcado cuando el tema real activo es día');
  assertEqual(getComputedStyle(document.querySelector('.mba-metric')).backgroundColor, 'rgb(255, 255, 255)', 'la cuadrícula de métricas ya refleja el color real de día (#FFFFFF) vía la cascada CSS, no solo el atributo');

  // ── Cambiar a Noche desde el propio selector de Cuenta ─────────────
  let applyThemeCalls = 0;
  const originalApplyTheme = hooks.mobileChart.applyThemeColors.bind(hooks.mobileChart);
  hooks.mobileChart.applyThemeColors = function(){ applyThemeCalls++; return originalApplyTheme(); };
  const symbolBefore = hooks.session.currentSymbol;
  const tfBefore = hooks.session.currentTF;
  const sentCountBeforeTheme = ws().sent.length;
  // El selector Apariencia puede quedar fuera del área visible sin
  // desplazar la vista (igual que en un teléfono real) — se desplaza
  // explícitamente antes de tocarlo, igual que haría un usuario real,
  // para que elementFromPoint() (el mismo hit-testing real que
  // realClick() ya usa) no caiga fuera del viewport.
  document.querySelector('.mba-theme-seg button[data-theme-value="noche"]').scrollIntoView({block:'center'});
  await sleep(20);
  realClick(document.querySelector('.mba-theme-seg button[data-theme-value="noche"]'));
  await sleep(30);
  assertEqual(document.documentElement.getAttribute('data-theme'), 'dark', 'clic en Noche fija <html data-theme> real a dark (misma secuencia que el toggle real de desktop.html)');
  assertEqual(document.querySelector('.mob-foundation').getAttribute('data-mb-theme'), null, 'noche vuelve a ser el valor LOCAL por defecto (atributo ausente)');
  assertEqual(document.querySelector('.mba-theme-seg button[data-theme-value="noche"]').getAttribute('aria-selected'), 'true', 'el botón Noche queda marcado tras el cambio');
  assertEqual(document.querySelector('.mba-theme-seg button[data-theme-value="dia"]').getAttribute('aria-selected'), 'false', 'el botón Día se desmarca tras el cambio');
  assert(applyThemeCalls >= 1, 'el cambio de tema re-aplica los colores reales del gráfico (applyThemeColors) vía el MutationObserver existente, sin un mecanismo nuevo');
  assertEqual(ws(), firstWs, 'cambiar de tema nunca crea una segunda conexión WebSocket');
  assertEqual(ws().sent.length, sentCountBeforeTheme, 'cambiar de tema nunca envía ningún mensaje por WebSocket (ni órdenes, ni reconexión)');
  assertEqual(hooks.session.currentSymbol, symbolBefore, 'el símbolo seleccionado se conserva tras el cambio de tema');
  assertEqual(hooks.session.currentTF, tfBefore, 'la temporalidad seleccionada se conserva tras el cambio de tema');

  // ── El cambio de tema se refleja en otras vistas, sin recrear nada ──
  gotoWatch();
  await sleep(10);
  assertEqual(document.querySelector('.mob-foundation').getAttribute('data-mb-theme'), null, 'Watch también refleja el tema real activo (noche) tras navegar, mismo atributo compartido');
  gotoAccount();
  await sleep(10);
  assertEqual(document.querySelector('.mba-theme-seg button[data-theme-value="noche"]').getAttribute('aria-selected'), 'true', 'al volver a Cuenta, el selector sigue reflejando el tema real activo');
"""


class AccountAppearanceRealExecutionTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_account_data_connection_margin_theme_bridge_and_cross_screen_coherence(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        # Preset the REAL persisted preference (localStorage['mb_theme'])
        # before the real base.html bootstrap script (already present
        # further down <head> in the dumped HTML) ever runs — simulates
        # a fresh load/reload after a theme was chosen in an earlier
        # session, exactly like a real browser restarting with the same
        # localStorage. Inserted as the very first child of <head>, so
        # script execution order guarantees it runs first.
        html = html.replace(
            "<head>",
            "<head><script>try{localStorage.setItem('mb_theme','light');}catch(e){}</script>",
            1,
        )
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(BODY, "acct_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="acct_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 25)


# ─────────────────────────────────────────────────────────────────────────
# C. Viewport — 390x844 and 430x932, both themes: no horizontal overflow,
#    no control hidden behind the bottom nav.
# ─────────────────────────────────────────────────────────────────────────
VIEWPORT_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  document.querySelector('.mob-navitem[data-target="account"]').click();
  await sleep(20);
  ws().emit({ type:'account:update', balance: 972.76, equity: 972.34, margin_used: 41.72, free_margin: 930.62, upnl: -0.42, pnl_unreal: -0.42, leverage: 50, account_type: 'STANDARD', margin_level: 2331.11, used_margin_pct: 4.3 });
  await sleep(20);

  const root = document.querySelector('.mob-foundation');
  assertEqual(root.scrollWidth, root.clientWidth, 'sin desbordamiento horizontal interno en .mob-foundation');
  assertEqual(document.documentElement.scrollWidth, document.documentElement.clientWidth, 'sin desbordamiento horizontal a nivel de documento');

  const navEl = document.querySelector('.mob-bottomnav, nav.mob-bottomnav, .mob-tabbar');
  const navRect = navEl ? navEl.getBoundingClientRect() : null;
  // .mob-view-area scrolls independently (overflow-y:auto); the bottom
  // nav is a normal flex SIBLING below it, never an overlay — so a
  // control further down an unscrolled long view naturally reports a
  // getBoundingClientRect() past the nav's own position without that
  // meaning real occlusion. The real question is whether, AFTER
  // scrolling a control into view (exactly what a real user would do
  // to reach it), the nav still visually covers any part of it.
  document.querySelector('.mba-theme-seg').scrollIntoView({block:'end'});
  await sleep(10);
  const fundsRect = document.querySelector('.mba-funds').getBoundingClientRect();
  const themeRect = document.querySelector('.mba-theme-seg').getBoundingClientRect();
  assert(fundsRect.right <= window.innerWidth + 0.5, 'Depositar/Retirar no se desbordan a la derecha');
  assert(themeRect.right <= window.innerWidth + 0.5, 'selector Día/Noche no se desborda a la derecha');
  if (navRect) {
    assert(themeRect.bottom <= navRect.top + 0.5, 'tras desplazarse hasta el final, el selector Día/Noche queda visible por completo, sin quedar tapado por la navegación inferior');
  }
"""


class AccountViewportTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _run_at(self, window_size, theme):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = html.replace(
            "<head>",
            f"<head><script>try{{localStorage.setItem('mb_theme','{theme}');}}catch(e){{}}</script>",
            1,
        )
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(VIEWPORT_BODY, "acct_vp_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size=window_size, virtual_time_budget=12000, result_id="acct_vp_results",
        )
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{window_size}/{theme}: {failures}")
        self.assertGreater(len(data["results"]), 0)

    def test_390x844_noche(self):
        self._run_at("390,844", "dark")

    def test_390x844_dia(self):
        self._run_at("390,844", "light")

    def test_430x932_noche(self):
        self._run_at("430,932", "dark")

    def test_430x932_dia(self):
        self._run_at("430,932", "light")


# ─────────────────────────────────────────────────────────────────────────
# D. Pending checks from the previous round's brief, completed now:
#    Trade/Gráfico's own visual flip under the real theme, and SL/TP
#    edit-in-progress surviving a real theme change with no reload/
#    reconnect/recreate and no change to symbol/timeframe/selection.
#    (The exact source of margin_level was re-verified by direct
#    reading of simulator/consumers.py — see the round's own report,
#    no code change needed since mbaRenderAccountExtras() already read
#    it correctly; not re-tested here as a separate assertion because
#    test_margin_level_read_verbatim_in_its_own_function_never_recomputed
#    above already covers it statically.)
# ─────────────────────────────────────────────────────────────────────────
TRADE_THEME_BODY = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  const chart = hooks.mobileChart;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);
  const firstWs = ws();

  function fixtureBars(n, startTime, tf){
    const bars = [];
    let seed = 7, price = 1.1200;
    function rnd(){ seed = (seed*16807) % 2147483647; return seed/2147483647; }
    for(let i=0;i<n;i++){
      const o = price, c = o + (rnd()-0.5)*0.0012;
      const hi = Math.max(o,c) + rnd()*0.0006, lo = Math.min(o,c) - rnd()*0.0006;
      bars.push({ time: startTime + i*tf, open:o, high:hi, low:lo, close:c, volume: 10+rnd()*40 });
      price = c;
    }
    return bars;
  }
  const SYM = 'EUR/USD';
  function emitQuote(){ ws().emit({ type:'tick', symbol: SYM, bid: 1.1209, ask: 1.1211, source:'test' }); }
  function emitHistory(){
    const tfSec = 900, now = Math.floor(Date.now()/1000);
    const start = Math.floor(now/tfSec)*tfSec - 60*tfSec;
    ws().emit({ type:'history', symbol: SYM, timeframe:'15m', phase:'complete', data: fixtureBars(60, start, tfSec) });
  }

  // ── Gráfico (Trade) visible, con posición e historial reales ───────
  document.querySelector('.mob-navitem[data-target="trade"]').click();
  await sleep(10);
  emitQuote();
  await sleep(20);
  emitHistory();
  await sleep(20);
  const lastClose = 1.1200;
  const P1 = { id:'P1', symbol: SYM, side:'buy', avg: lastClose-0.0015, sl: lastClose-0.0045, tp: lastClose+0.0025, qty:0.02, pnl:1.23 };
  ws().emit({ type:'positions', items:[P1] });
  await sleep(30);
  assertEqual(hooks.session.currentSymbol, SYM, 'Gráfico auto-selecciona el símbolo real de la única posición abierta (flujo existente, sin WS nuevo)');
  assertEqual(String(chart._selectedId), 'P1', 'la posición real queda seleccionada en el gráfico (regla "primera del snapshot" ya existente)');

  // ── Flip visual real de Gráfico bajo el tema real ya persistido ────
  // (mb_theme='light' precargado por el test -> día activo al cargar;
  // .mob-foundation[data-mb-theme="dia"] .mbg-account-strip tiene su
  // propia regla real border-color:transparent).
  assertEqual(getComputedStyle(document.querySelector('.mbg-account-strip')).borderColor, 'rgba(0, 0, 0, 0)', 'Gráfico SÍ refleja el tema real activo (día) vía la cascada CSS antes del cambio');

  // ── Empieza una edición real de SL (arrastre en curso, SIN soltar) ──
  const slEl = document.querySelector('.mbgc-pill-sl');
  assert(!!slEl, 'el pill de SL real está renderizado para la posición seleccionada');
  function firePointer(el, type, opts){
    const ev = new PointerEvent(type, Object.assign({ bubbles:true, cancelable:true, pointerId:1, clientY:100 }, opts));
    el.dispatchEvent(ev);
  }
  // Arrastre completo hasta soltar — el overlay real Cancelar/Confirmar
  // (previsualización pendiente de confirmar, aún sin enviar nada al
  // backend) solo aparece TRAS soltar, igual que la propia suite de
  // edición SL/TP ya establecida (test_mobile_sltp_position_editing.py,
  // escenario S1: pointerdown -> pointermove -> pointerup -> overlay).
  // "Estado de edición" que debe sobrevivir al cambio de tema es
  // precisamente este: una previsualización real pendiente de
  // confirmar, no un simple dedo-aún-en-pantalla.
  firePointer(slEl, 'pointerdown', { clientY: 300 });
  firePointer(slEl, 'pointermove', { clientY: 260 });
  firePointer(slEl, 'pointerup', { clientY: 260 });
  await sleep(20);
  const cancelBtnBefore = document.querySelector('.mbgc-edit-cancel');
  assert(!!cancelBtnBefore, 'el overlay Cancelar/Confirmar real aparece tras soltar, con la previsualización pendiente');
  const gesturesDisabledBefore = chart._gesturesDisabled;
  const selectedBefore = String(chart._selectedId);
  const symbolBefore = hooks.session.currentSymbol;
  const tfBefore = hooks.session.currentTF;
  const sentBeforeTheme = ws().sent.length;

  // ── Cambia el tema real MIENTRAS la edición sigue en curso ─────────
  // Misma secuencia exacta que produce el propio selector de Cuenta/el
  // toggle real de desktop.html — aplicada aquí directamente (sin
  // navegar a Cuenta) porque el requisito es que CUALQUIER cambio real
  // de tema (desde Cuenta, desde Desktop en otra pestaña vía el evento
  // 'storage', etc.) nunca interrumpa una edición en curso en Gráfico.
  let applyThemeCalls = 0;
  const originalApplyTheme = chart.applyThemeColors.bind(chart);
  chart.applyThemeColors = function(){ applyThemeCalls++; return originalApplyTheme(); };
  document.documentElement.setAttribute('data-theme', 'dark');
  try{ localStorage.setItem('mb_theme', 'dark'); }catch(e){}
  document.dispatchEvent(new CustomEvent('mb:themechange', { detail:{ theme:'dark' } }));
  await sleep(30);

  assertEqual(getComputedStyle(document.querySelector('.mbg-account-strip')).borderColor, 'rgb(31, 38, 50)', 'Gráfico refleja el tema real nuevo (noche, --border:#1F2632) tras el cambio, vía la cascada CSS');
  assert(applyThemeCalls >= 1, 'el cambio de tema re-aplica los colores reales del gráfico también estando en Gráfico');
  assertEqual(ws(), firstWs, 'el cambio de tema durante una edición en curso nunca crea una segunda conexión WebSocket');
  assertEqual(ws().sent.length, sentBeforeTheme, 'el cambio de tema durante una edición en curso nunca envía ningún mensaje (ni la edición, ni una reconexión)');
  assertEqual(hooks.session.currentSymbol, symbolBefore, 'el símbolo seleccionado se conserva durante el cambio de tema en plena edición');
  assertEqual(hooks.session.currentTF, tfBefore, 'la temporalidad se conserva durante el cambio de tema en plena edición');
  assertEqual(String(chart._selectedId), selectedBefore, 'la posición seleccionada se conserva durante el cambio de tema en plena edición');
  assertEqual(chart._gesturesDisabled, gesturesDisabledBefore, 'el estado de gestos del gráfico no cambia por sí mismo a causa del cambio de tema');
  const cancelBtnAfter = document.querySelector('.mbgc-edit-cancel');
  assert(!!cancelBtnAfter, 'el overlay Cancelar/Confirmar real sigue presente tras el cambio de tema (estado de edición conservado)');

  // ── Limpieza: cancela la edición en curso (no deja estado colgado) ──
  cancelBtnAfter.click();
  await sleep(10);
"""


class TradeThemeAndSlTpEditPersistenceTests(TestCase):
    """Completes the pending items from the previous round's own
    VERIFICACIÓN DIRIGIDA list that this round's Cuenta-only testing had
    not yet covered: Gráfico's own visual theme flip, and an in-progress
    SL/TP drag edit surviving a real theme change untouched."""

    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_trade_view_theme_flip_and_sltp_edit_survives_theme_change(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = html.replace(
            "<head>",
            "<head><script>try{localStorage.setItem('mb_theme','light');}catch(e){}</script>",
            1,
        )
        html = _inject_fake_ws_and_hooks(html, self)
        driver = _driver_wrap(TRADE_THEME_BODY, "trade_theme_results")
        data = _run_driver_and_extract(
            self, html, driver, window_size="390,844", virtual_time_budget=15000, result_id="trade_theme_results",
        )
        for r in data["results"]:
            print("RESULT:", r)
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 10)
