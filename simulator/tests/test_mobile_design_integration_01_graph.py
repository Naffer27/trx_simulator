# simulator/tests/test_mobile_design_integration_01_graph.py
"""
MOBILE-DESIGN-INTEGRATION-01 — GRAPH.

Verifies that the Mobile Trade/"Gráfico" view's visual overhaul (Visual
Contract V1 — money-broker-movil/grafico.html + tema.css) was integrated
as a presentation layer only: the approved geometry/typography/colors
live in simulator/templates/simulator/trade/mobile.html, scoped
exclusively to [data-mobile-view="trade"] (never touching the global
--mb-* tokens or any other view's CSS), and the one authorized additive
extension (Entry/SL/TP price lines) lives in
simulator/static/simulator/trade/mobile_chart.js using Lightweight
Charts' own createPriceLine() with zero new financial computation.

Also verifies: no prototype demo code (DATA/demoCandles/preset lot
array/fake BUY-SELL) was carried over, every existing engine authority
(onQuote/session.selectSymbol/submitTicket/getLotStep family/risk
preview-warning/getCanonicalPositions/onAccount) is unchanged, the
public timeframe catalog is untouched, no fabricated data (%change,
high/low, 24h volume, favorites) was introduced, the day/night token
scaffold never touches <html>'s own data-theme (base.html, shared with
Desktop/Dashboard), and the scoped diff touches only the two authorized
files.
"""
import re
import subprocess

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

MOBILE_HTML_PATH = "simulator/templates/simulator/trade/mobile.html"
MOBILE_CHART_JS_PATH = "simulator/static/simulator/trade/mobile_chart.js"
# SL/TP-on-open-position editing (audited-then-authorized sub-block) —
# mobile_session.js joins the authorized-to-change set for this round
# ONLY (previously fully protected/zero-diff) — see
# MobileSessionScopedDiffTests below for the precise scope check that
# replaces the old blanket zero-diff assertion.
MOBILE_SESSION_JS_PATH = "simulator/static/simulator/trade/mobile_session.js"

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def _mobile_html_source():
    with open(MOBILE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


def _mobile_chart_js_source():
    with open(MOBILE_CHART_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _style_block(source):
    return source[source.index("<style>") : source.index("</style>")]


def _trade_view_block(source):
    start = source.index('<div class="mob-view" data-mobile-view="trade">')
    end = source.index('<div class="mob-view" data-mobile-view="markets"')
    return source[start:end]


def _main_script_block(source):
    start = source.index("<script>\n(function(){")
    end = source.index("\n})();\n</script>", start) + len("\n})();\n</script>")
    return source[start:end]


def _render():
    user = make_user()
    account = make_account(user, account_type="STANDARD")
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client.get(
        reverse("simulator:dashboard_account", args=[account.pk]),
        HTTP_USER_AGENT=IPHONE_UA,
    )


# ─────────────────────────────────────────────────────────────────────────
# A. Visual Contract V1 geometry/tokens, scoped to the trade view only
# ─────────────────────────────────────────────────────────────────────────
class ScopedVisualContractTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.style = _style_block(self.html)

    # MOBILE-DESIGN-INTEGRATION-01 — GRAPH (presentation isolation pass).
    # OLD: tokens were declared on .mob-view[data-mobile-view="trade"]
    # only. NEW: declared on .mob-foundation instead (and never on bare
    # :root — still never touching the global --mb-* system). WHY: the
    # bottom nav (.mob-bottomnav) is a SIBLING of .mob-view, not a
    # descendant, and the Owner authorized restyling it to match the
    # Visual Contract V1 too ("la navegación inferior debe verse como
    # la referencia aprobada") — it needs the same --brand/--muted/
    # --nav-bg/--nav-active tokens, which only reach it via a shared
    # ancestor. Still fully scoped off :root/--mb-*, still inert for
    # Markets/Positions/History/Account (none of them reference --bg/
    # --brand/--buy/--sell/etc.).
    def test_contract_tokens_scoped_to_foundation_never_global_root(self):
        self.assertIn('.mob-foundation{', self.style)
        self.assertIn("--brand:#F5C518", self.style)
        self.assertIn("--buy:#2EBD85", self.style)
        self.assertIn("--sell:#D92D4B", self.style)
        root_block = self.html[self.html.index(":root{"): self.html.index("}", self.html.index(":root{")) + 1]
        self.assertNotIn("--brand:#F5C518", root_block)

    def test_global_mb_tokens_untouched(self):
        root_block = self.html[self.html.index(":root{"): self.html.index("}", self.html.index(":root{")) + 1]
        # The whole, original 03C.3.1 token block (sample of distinctive
        # literal values) must still be present verbatim.
        self.assertIn("--mb-brand:#c7a64a;", root_block)
        self.assertIn("--mb-buy:#1f9d6c;", root_block)
        self.assertIn("--mb-sell:#d1524f;", root_block)
        self.assertIn("--mb-bg:#0a0c12;", root_block)

    # OLD: expected .mob-foundation[data-mb-theme="dia"] .mob-view[...]
    # (nested under .mob-view). NEW: .mob-foundation[data-mb-theme="dia"]
    # directly (matching the token relocation above). WHY: same reason —
    # the day variant must also reach the shared bottom nav.
    def test_day_variant_scoped_and_never_touches_html_data_theme(self):
        self.assertIn('.mob-foundation[data-mb-theme="dia"]{', self.style)
        # Never reuses <html data-theme=...> — that belongs to base.html's
        # shared UI-THEME-01 mechanism (Desktop/Dashboard).
        self.assertNotIn('<html', self.html[: self.html.index("{% block")] if "{% block" in self.html else self.html)
        self.assertNotIn("document.documentElement.setAttribute('data-theme'", self.html)
        self.assertNotIn('document.documentElement.setAttribute("data-theme"', self.html)

    # OLD: expected a fixed height:50vh/min/max-height floor+ceiling.
    # NEW: expects a true flex:1 (matching grafico.html's own
    # .chart{flex:1}) inside a trade view now given height:100% (see
    # .mob-view[data-mobile-view="trade"] below). WHY (2nd correction
    # round): the 50vh guess under-used available space on taller
    # viewports and over-used it on shorter ones — flex:1 genuinely
    # consumes exactly the leftover room after the fixed-height
    # siblings, like the reference.
    def test_chart_takes_generous_vertical_room_not_compressed(self):
        body = self.style[self.style.index('.mbg-chart{'): self.style.index("}", self.style.index('.mbg-chart{')) + 1]
        self.assertIn("flex:1", body)
        self.assertIn("min-height:0", body)
        trade_view_rule = self.style[self.style.index('.mob-view[data-mobile-view="trade"]{'): self.style.index("}", self.style.index('.mob-view[data-mobile-view="trade"]{')) + 1]
        self.assertIn("height:100%", trade_view_rule)

    def test_candle_colors_match_contract(self):
        js = _mobile_chart_js_source()
        self.assertIn("upColor: '#2EBD85'", js)
        self.assertIn("downColor: '#F6465D'", js)


# ─────────────────────────────────────────────────────────────────────────
# B. No prototype demo code carried over
# ─────────────────────────────────────────────────────────────────────────
class NoDemoCodeTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_no_demo_data_structures(self):
        for forbidden in (
            "var DATA", "demoCandles", "DATA.lots", "DATA.contractSize",
            "DATA.position", "state.lotIdx", "state.pos",
        ):
            self.assertNotIn(forbidden, self.html, forbidden)

    def test_no_fake_position_creation_on_buy_sell(self):
        # The prototype's demo directly assigned a fake position object
        # on click — must never appear; real flow is submitTicket()->
        # session.submitOrder() only.
        self.assertNotIn("side: 'buy', entry: DATA.ask", self.html)
        self.assertNotIn("side: 'sell', entry: DATA.bid", self.html)

    def test_no_preset_lot_array(self):
        self.assertNotIn("[0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.00]", self.html)
        self.assertNotIn("[0.01,0.02,0.05,0.10,0.20,0.50,1.00]", self.html)


# ─────────────────────────────────────────────────────────────────────────
# C. Existing engine authorities unchanged
# ─────────────────────────────────────────────────────────────────────────
class EngineAuthoritiesUnchangedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.script = _main_script_block(self.html)

    def test_onquote_still_sole_price_writer_no_new_authority(self):
        self.assertIn("function onQuote(state){", self.script)
        self.assertIn("applyPriceTickState()", self.html)

    def test_select_symbol_single_call_site_preserved(self):
        self.assertEqual(self.html.count("session.selectSymbol(item.symbol)"), 1)

    def test_submit_ticket_unchanged_contract(self):
        self.assertIn("session.submitOrder({side,qty,sl,tp});", self.script)

    def test_lot_helpers_are_the_real_shared_ones(self):
        self.assertIn("getLotStep(sym)", self.script)
        self.assertIn("getLotMin(sym)", self.script)
        self.assertIn("getLotDecimals(sym)", self.script)
        # Never a second/local definition of these helpers in mobile.html.
        self.assertNotIn("function getLotStep(", self.script)
        self.assertNotIn("function getLotMin(", self.script)
        self.assertNotIn("function getLotDecimals(", self.script)

    def test_risk_preview_warning_contract_unchanged(self):
        self.assertIn("function onRiskPreview(msg){", self.script)
        self.assertIn("function onRiskWarning(msg){", self.script)
        self.assertIn("session.requestRiskPreview(", self.script)
        self.assertIn("session.confirmRiskWarning();", self.script)
        self.assertIn("session.cancelRiskWarning();", self.script)

    def test_canonical_positions_store_reused_not_duplicated(self):
        self.assertIn("getCanonicalPositions()", self.script)
        self.assertNotIn("function getCanonicalPositions(", self.script)
        self.assertNotIn("_mobileCanonicalPositions", self.html)

    def test_on_account_untouched(self):
        self.assertIn("function onAccount(msg){", self.script)


# ─────────────────────────────────────────────────────────────────────────
# D. Entry/SL/TP price lines — additive only, zero financial calc
# ─────────────────────────────────────────────────────────────────────────
class PositionLinesTests(SimpleTestCase):
    def setUp(self):
        self.js = _mobile_chart_js_source()
        self.html = _mobile_html_source()
        self.script = _main_script_block(self.html)

    # OLD: self.assertIn("setPositionLines(pos) {", self.js) / "clearPositionLines() {"
    # NEW: setPositions(positions, selectedId) / clearPositions().
    # WHY: Multi-position correction round — the public entry point now
    # takes the FULL array of canonical positions for the current
    # symbol plus which one is selected (never a single position, never
    # netted/merged/synthesized) — see setPositions()'s own class-level
    # comment in mobile_chart.js.
    def test_set_and_clear_position_lines_present(self):
        self.assertIn("setPositions(positions, selectedId) {", self.js)
        self.assertIn("clearPositions() {", self.js)

    def test_uses_native_create_price_line_api(self):
        self.assertIn("this.candleSeries.createPriceLine({", self.js)
        self.assertIn("this.candleSeries.removePriceLine(", self.js)

    # OLD: this test read pos.avg/pos.sl/pos.tp straight out of
    # setPositionLines(pos)'s own body (the single-position method).
    # NEW: setPositions() now draws every position's Entry via
    # _renderAllEntries() (pos.avg) and the SELECTED position's SL/TP
    # via _renderSelectedSlTp() (pos.sl/pos.tp) — two methods instead of
    # one, same real canonical fields, still zero computation.
    # WHY: Multi-position correction round.
    def test_reads_real_position_fields_verbatim_no_computation(self):
        entries_start = self.js.index("_renderAllEntries(list) {")
        entries_end = self.js.index("\n  }", entries_start)
        entries_body = self.js[entries_start:entries_end]
        self.assertIn("pos.avg", entries_body)

        sel_start = self.js.index("_renderSelectedSlTp(pos) {")
        sel_end = self.js.index("\n  }", sel_start)
        sel_body = self.js[sel_start:sel_end]
        self.assertIn("pos.sl", sel_body)
        self.assertIn("pos.tp", sel_body)
        for forbidden in (
            "computeRawPnL", "computePositionPnL", "margin_used", "commission_for",
            "getContractSize(", "* pos.qty", "pos.qty *", "spread_revenue",
        ):
            self.assertNotIn(forbidden, entries_body, forbidden)
            self.assertNotIn(forbidden, sel_body, forbidden)

    # OLD: self.script.index("function currentSymbolPosition(){") — a
    # for-loop returning the FIRST match only.
    # NEW: currentSymbolPositions() — filters ALL matches (never just
    # the first), since multiple open positions on the same symbol must
    # all be shown.
    # WHY: Multi-position correction round — currentSymbolPosition()
    # "devuelve la primera coincidencia y deja al gráfico representando
    # una sola posición aunque existan varias operaciones abiertas del
    # mismo símbolo" was the exact root cause this round fixes.
    def test_refresh_position_lines_filters_real_store_by_current_symbol(self):
        body_start = self.script.index("function currentSymbolPositions(){")
        body_end = self.script.index("\n  }", body_start)
        body = self.script[body_start:body_end]
        self.assertIn("getCanonicalPositions()", body)
        self.assertIn("p.symbol===session.currentSymbol", body)

    def test_wired_into_onpositions_init_and_symbol_change(self):
        # OLD: self.assertIn("function onPositions(items){ renderPositions(items); refreshPositionLines(); }", self.script)
        # NEW: onPositions() is no longer a one-liner — it also compares
        # each snapshot against a pending chart-initiated SL/TP edit
        # (condition 4, SL/TP-on-open-position editing sub-block) before
        # deciding whether to resolve it — but renderPositions() and
        # refreshPositionLines() are still both called from inside it,
        # unconditionally, every time.
        # WHY: SL/TP-on-open-position editing (audited-then-authorized
        # sub-block) — see onPositions()'s own updated comment in
        # mobile.html for the full match-then-resolve contract.
        body_start = self.script.index("function onPositions(items){")
        body_end = self.script.index("\n  }", body_start)
        body = self.script[body_start:body_end]
        self.assertIn("renderPositions(items);", body)
        self.assertIn("refreshPositionLines();", body)
        self.assertIn("resolveSlTpEdit(resolvedField)", body)
        # at least 3 call sites: onPositions, init, watchlist tap
        self.assertGreaterEqual(self.script.count("refreshPositionLines()"), 3)


# ─────────────────────────────────────────────────────────────────────────
# E. Timeframe catalog untouched
# ─────────────────────────────────────────────────────────────────────────
class TimeframeCatalogUnchangedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_catalog_exactly_six_no_1s(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m','5m','15m','1h','4h','1d'];", self.html)
        self.assertNotIn("'1s'", self.html)

    def test_no_1w_1mo_added(self):
        for forbidden in ("'1w'", "'1mo'", "'1M'"):
            self.assertNotIn(forbidden, self.html)

    def test_tf_selector_uses_new_class_but_same_catalog_variable(self):
        self.assertIn("item.className = 'mbg-tf'", self.html)
        self.assertIn("MOBILE_TIMEFRAMES.forEach", self.html)


# ─────────────────────────────────────────────────────────────────────────
# F. No fabricated data
# ─────────────────────────────────────────────────────────────────────────
class NoFabricatedDataTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_no_prohibited_fields(self):
        scanned = re.sub(r"<!--.*?-->", "", self.html, flags=re.DOTALL)
        scanned = re.sub(r"(?<!:)//[^\n]*", "", scanned)
        for forbidden in (
            ">Máx<", ">Mín<", ">Vol<", "change_pct", "pctChange", "dailyChange",
            "high_low", "highLow", "volume_24h", "favorite", "isFavorite",
            "market_status", "marketStatus",
        ):
            self.assertNotIn(forbidden, scanned, forbidden)

    def test_spread_is_derived_from_real_bid_ask_only(self):
        self.assertIn("state.ask-state.bid", self.html.replace(" ", ""))


# ─────────────────────────────────────────────────────────────────────────
# G. IDs preserved / new IDs present exactly once
# ─────────────────────────────────────────────────────────────────────────
class IdsPreservedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    # OLD: mobQuoteSource was expected among the preserved ids. NEW:
    # removed from this list entirely. WHY: this round's own explicit
    # instruction prohibits showing feed/provider infrastructure detail
    # (source="massive" etc.) in a user-facing slot — the element (and
    # its onQuote() write) was deliberately deleted, not silently lost;
    # mobConnStatus/mobConnStatusLabel (still present, still real) is
    # the correct place for connection info, not this quote row.
    # OLD: mobQuoteBid/mobQuoteAsk were expected among the preserved
    # ids. NEW: removed from this list. WHY (2nd correction round): the
    # reference's actual quote-stats slot is "Máx/Mín/Vol" (now shown
    # honestly, see PresentationIsolationTests below) — Bid/Ask are
    # still fully real and displayed, exclusively via mobTradeBidPrice/
    # mobTradeAskPrice on the trade-bar (the reference's own real slot
    # for per-side price) — never silently dropped, just consolidated
    # onto the ids that match the actual composition.
    # OLD: mobSlInput/mobTpInput were expected among the preserved ids.
    # NEW: removed from this list entirely.
    # WHY: Gráfico scope-removal round — the new-order SL/TP panel (and
    # its two inputs) is removed from this screen per explicit Owner
    # decision; that configuration moves to a future, separately-
    # designed order-creation screen (not built yet). submitOrder()'s
    # own sl/tp parameters are unchanged; only this screen's current UI
    # source for them is gone — readSlTp() now always returns
    # {sl:null,tp:null} instead of reading a removed DOM element.
    def test_existing_ids_present_exactly_once(self):
        for el_id in (
            "mobQuoteSymbol", "mobQuoteMid",
            "mobTfSelector", "mobChartContainer",
            "mobTicket", "mobQtyInput",
            "mobSellBtn", "mobBuyBtn", "mobTicketStatus",
            "mobRiskPanel", "mobRiskLevel", "mobRiskMargin", "mobRiskExposure",
            "mobRiskConfirm", "mobRiskConfirmText", "mobRiskCancelBtn", "mobRiskConfirmBtn",
            "mobWatchlist", "mobPositionsList", "mobBottomNav",
        ):
            self.assertEqual(self.html.count(f'id="{el_id}"'), 1, el_id)

    def test_new_graph_ids_present_exactly_once(self):
        for el_id in (
            "mobLiveBadge", "mobQuoteSpread", "mobTradeBidPrice", "mobTradeAskPrice",
            "mobLotDownBtn", "mobLotUpBtn",
        ):
            self.assertEqual(self.html.count(f'id="{el_id}"'), 1, el_id)

    def test_qty_input_is_readonly(self):
        self.assertIn('id="mobQtyInput" step="0.01" min="0" value="0.01" readonly', self.html)


# ─────────────────────────────────────────────────────────────────────────
# H. App shell + other views untouched
# ─────────────────────────────────────────────────────────────────────────
class AppShellAndOtherViewsUntouchedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_five_views_trade_default(self):
        views = re.findall(r'<div class="mob-view" data-mobile-view="(\w+)"( hidden)?>', self.html)
        self.assertEqual(len(views), 5)
        visible = {name for name, hidden_attr in views if not hidden_attr}
        hidden = {name for name, hidden_attr in views if hidden_attr}
        self.assertEqual(visible, {"trade"})
        self.assertEqual(hidden, {"markets", "positions", "history", "account"})

    def test_markets_view_block_byte_identical_to_before_this_block(self):
        # Scoped diff proof at the test level: the Markets view's own
        # markup/CSS region (03C.3.4/03C.3.4V.1) must be untouched by
        # this GRAPH-only block.
        self.assertIn('id="mobMarketSearch"', self.html)
        self.assertIn('id="mobMarketChips"', self.html)
        self.assertIn(".mob-watchlist{width:100%;background:var(--mb-surface-1);overflow:hidden;}", self.html)

    def test_topbar_bottomnav_untouched(self):
        self.assertIn('<div class="mob-topbar">', self.html)
        self.assertIn('<div class="mob-bottomnav" id="mobBottomNav">', self.html)
        self.assertEqual(self.html.count("function showView(name){"), 1)


# ─────────────────────────────────────────────────────────────────────────
# I. Scoped diff — only the 2 authorized files touched
# ─────────────────────────────────────────────────────────────────────────
class ScopedDiffTests(SimpleTestCase):
    # OLD: self.assertEqual(changed, sorted([MOBILE_HTML_PATH, MOBILE_CHART_JS_PATH]))
    # NEW: mobile_session.js joins the set.
    # WHY: SL/TP-on-open-position editing (audited-then-authorized
    # sub-block) — explicit Owner authorization: "Autorizado modificar
    # mobile_session.js exclusivamente para conectar el contrato
    # existente order:update y enrutar las respuestas necesarias para
    # esta operación." See MobileSessionScopedDiffTests below for the
    # check that its diff stays within that exact scope.
    # OLD: self.assertEqual(changed, sorted([MOBILE_HTML_PATH, MOBILE_CHART_JS_PATH]))
    # NEW: production scope is the 3 named paths; any OTHER changed file
    # must live under simulator/tests/ (test files for the interaction
    # itself, or pre-existing test files corrected for an EXPECTED
    # supersession this sub-block's own authorization produced — never
    # production code outside the 3 named paths).
    # WHY: SL/TP-on-open-position editing (audited-then-authorized
    # sub-block) — mobile_session.js joins production scope (explicit
    # Owner authorization); fixing now-superseded assertions in
    # pre-existing test files ("tests específicos de esta interacción",
    # per the same authorization) is expected, not a scope violation.
    def test_only_authorized_files_modified(self):
        result = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True)
        changed = sorted(l for l in result.stdout.splitlines() if l.strip())
        production_scope = {MOBILE_HTML_PATH, MOBILE_CHART_JS_PATH, MOBILE_SESSION_JS_PATH}
        for path in changed:
            self.assertTrue(
                path in production_scope or path.startswith("simulator/tests/"),
                f"{path} is outside the authorized production scope and not a test file",
            )

    # OLD: "simulator/static/simulator/trade/mobile_session.js" was in
    # this blanket zero-diff list.
    # NEW: removed — see MobileSessionScopedDiffTests below for its own
    # dedicated, narrowly-scoped check.
    # WHY: SL/TP-on-open-position editing (audited-then-authorized
    # sub-block) — explicitly authorized, narrow exception; backend/
    # Desktop/trading_core.js/models/routing remain fully protected and
    # unweakened here.
    def test_protected_files_zero_diff(self):
        for path in (
            "simulator/templates/simulator/trade/desktop.html",
            "simulator/consumers.py",
            "market_data/feeds.py",
            "simulator/static/simulator/trade/trading_core.js",
            "simulator/models.py",
            "simulator/routing.py",
        ):
            result = subprocess.run(["git", "diff", "--quiet", "--", path])
            self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    def test_no_migrations_added(self):
        result = subprocess.run(["git", "status", "--porcelain", "simulator/migrations/"], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "")

    def test_mobile_chart_js_diff_contains_no_financial_keywords(self):
        result = subprocess.run(["git", "diff", "--", MOBILE_CHART_JS_PATH], capture_output=True, text=True)
        diff = result.stdout
        for forbidden in (
            "commission_for", "BrokerLedger", "margin_used", "spread_revenue",
            "new WebSocket(", "fetch(", "computeRawPnL(", "getContractSize(",
        ):
            self.assertNotIn(forbidden, diff, forbidden)


# ─────────────────────────────────────────────────────────────────────────
# I.1 SL/TP-on-open-position editing — mobile_session.js's own diff stays
#     exactly within its narrow authorization (connect the EXISTING real
#     order:update contract + route its responses) and never touches any
#     other flow.
# ─────────────────────────────────────────────────────────────────────────
class MobileSessionScopedDiffTests(SimpleTestCase):
    def _diff(self):
        result = subprocess.run(["git", "diff", "--", MOBILE_SESSION_JS_PATH], capture_output=True, text=True)
        return result.stdout

    def test_real_diff_exists(self):
        self.assertTrue(self._diff().strip(), "expected a real mobile_session.js diff")

    def test_no_second_websocket_or_financial_computation(self):
        diff = self._diff()
        for forbidden in (
            "new WebSocket(", "fetch(", "XMLHttpRequest",
            "commission_for", "BrokerLedger", "margin_used", "spread_revenue",
            "computeRawPnL(", "getContractSize(", "contractSize",
        ):
            self.assertNotIn(forbidden, diff, forbidden)

    def test_no_other_flow_structurally_altered(self):
        # The diff may legitimately mention these method names in ADDED
        # explanatory comments (describing the mutual-exclusion
        # reasoning against the new _slTpSendingId guard), but must never
        # REMOVE a line that was any of these methods' own send/guard
        # logic — i.e. no '-' (removed) line may contain their call
        # signatures.
        diff = self._diff()
        removed_lines = [
            l for l in diff.splitlines()
            if l.startswith("-") and not l.startswith("---")
        ]
        for line in removed_lines:
            for untouchable in (
                "submitOrder(", "closePosition(", "cancelPendingOrder(",
                "requestRiskPreview(", "confirmRiskWarning(", "selectSymbol(",
                "selectTimeframe(",
            ):
                self.assertNotIn(untouchable + " {", line, line)

    def test_session_source_contains_the_new_minimal_method_only(self):
        with open(MOBILE_SESSION_JS_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("updateSlTp({", src)
        self.assertIn("action: 'order:update'", src)
        self.assertIn("_slTpSendingId", src)
        self.assertIn("onSlTpUpdateRejected", src)
        # Still forbidden — this sub-block never touches pending-order
        # creation/edit or any direct 'positions'-branch special-casing
        # (the match/no-match comparison is explicitly the caller's
        # job — see this file's own class-level comment).
        for forbidden in ("order:pending:new", "order:pending:update"):
            self.assertNotIn(forbidden, src)


# ─────────────────────────────────────────────────────────────────────────
# J. End-to-end render smoke test
# ─────────────────────────────────────────────────────────────────────────
class GraphRenderTests(TestCase):
    def test_graph_view_renders_with_new_structure(self):
        r = _render()
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('<div class="mbg">', body)
        self.assertIn('id="mobChartContainer"', body)
        self.assertIn('id="mobLotDownBtn"', body)
        self.assertIn('id="mobLotUpBtn"', body)
        self.assertIn('id="mobTradeBidPrice"', body)
        self.assertIn('id="mobTradeAskPrice"', body)
        self.assertIn('id="mobQuoteSpread"', body)


# ─────────────────────────────────────────────────────────────────────────
# K. Presentation isolation pass (2nd round) — single header, Bootstrap/
#    base.html bleed fixed, shared nav restyled, no fabricated controls
# ─────────────────────────────────────────────────────────────────────────
class PresentationIsolationTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.style = _style_block(self.html)

    def test_base_html_important_background_color_counter_fixed(self):
        # base.html (untouched, confirmed by ScopedDiffTests above)
        # declares body{background:#05050a !important;color:#fff
        # !important;} — this file must declare a matching-specificity
        # !important override so the real --mb-bg/--mb-text-primary
        # tokens actually win, instead of silently losing to a nearly-
        # identical dark color that only looked fine by coincidence.
        self.assertIn("background:var(--mb-bg) !important", self.style)
        self.assertIn("color:var(--mb-text-primary) !important", self.style)

    def test_foundation_background_uses_theme_aware_token(self):
        # OLD (self-inflicted bug, caught by screenshot verification):
        # .mob-foundation{background:var(--mb-bg);} — --mb-bg is the
        # OLD global token, which never changes with data-mb-theme, so
        # the whole page silently stayed dark even in "día". Fixed to
        # var(--bg), the token that actually flips.
        body = self.style[self.style.index(".mob-foundation{"): self.style.index("}", self.style.index(".mob-foundation{")) + 1]
        self.assertIn("background:var(--bg)", body)
        self.assertNotIn("background:var(--mb-bg)", body)

    def test_generic_topbar_hidden_only_while_graph_active(self):
        self.assertIn(".mob-foundation.mbg-active .mob-topbar{display:none;}", self.style)
        self.assertIn("foundationEl.classList.toggle('mbg-active', name === 'trade');", self.html)

    def test_bottomnav_restyled_with_contract_icons_same_wiring(self):
        # Same 5 buttons, same data-target values — only icon/visual
        # treatment added. Wiring (showView/click listeners) untouched
        # below.
        #
        # OLD: for label in ("MARKETS", "TRADE", "POSITIONS", "HISTORY", "ACCOUNT"):
        #          self.assertIn(label, self.html)
        # NEW: each label checked INSIDE its own <button>...</button>
        # body (not the whole file), against the reference's own exact
        # literal text (Watch/Gráfico/Posiciones/Historial/Cuenta).
        # WHY: 6-item correction round, item 4 — Owner-authorized text
        # change reproducing grafico.html's own <nav class="tabbar">
        # wording. The OLD assertion was already a bare, unscoped
        # substring check against the ENTIRE file — it kept "passing"
        # after this change only because "MARKETS"/"TRADE"/etc. still
        # happen to appear in this file's own explanatory comments and
        # in window.__TRADE_CONFIG__, never in the actual button text
        # it claimed to verify (same bare-word-collision class as other
        # scoped-diff fixes in this project). Scoping to each button's
        # own body makes this assert what it actually claims again.
        for target, icon_fragment, label in (
            ("markets", 'cx="12" cy="12" r="3"', "Watch"),
            ("trade", 'x="5" y="7" width="4" height="8"', "Gráfico"),
            ("positions", 'x="3" y="7" width="18" height="13"', "Posiciones"),
            ("history", 'cx="12" cy="12" r="9"', "Historial"),
            ("account", 'cx="12" cy="8" r="4"', "Cuenta"),
        ):
            btn_start = self.html.index(f'data-target="{target}"')
            btn_end = self.html.index("</button>", btn_start)
            btn_body = self.html[btn_start:btn_end]
            self.assertIn(icon_fragment, btn_body, target)
            self.assertIn(label, btn_body, target)

    def test_chart_background_transparent_blends_with_page(self):
        body = self.style[self.style.index(".mob-view[data-mobile-view=\"trade\"] .mbg-chart{"): self.style.index("}", self.style.index(".mob-view[data-mobile-view=\"trade\"] .mbg-chart{")) + 1]
        self.assertNotIn("background:", body)
        self.assertNotIn("border-radius:", body)

    def test_symbol_button_reuses_markets_navigation_no_new_picker(self):
        start = self.html.index("const elSymbolBtn=document.getElementById('mobSymbolBtn');")
        end = self.html.index("\n  }", start)
        body = self.html[start:end]
        self.assertIn('.mob-navitem[data-target="markets"]', body)
        self.assertIn("marketsNavBtn.click();", body)
        # No new symbol list/picker implemented locally.
        self.assertNotIn("new Set(MOBILE_SYMBOLS", body)

    # OLD: star/Trade were expected ABSENT (silently omitted). NEW
    # (2nd correction round): both are PRESENT, in the same slot as the
    # reference, but natively [disabled] + aria-disabled + a title=
    # tooltip explaining why — never wired to fabricated behavior
    # (no favorites persistence exists; "Trade" has no distinct real
    # destination). WHY: "no des por aprobadas las omisiones... conserva
    # la composición... presenta explícitamente los controles... sin
    # conexión disponible" — a visibly-disabled real control is more
    # honest than a silent gap.
    def test_star_and_trade_present_but_explicitly_disabled(self):
        # Star: unchanged — still no real backend, still honestly
        # disabled rather than hidden or wired to fabricated behavior.
        trade_block = _trade_view_block(self.html)
        self.assertIn('class="mbg-star-btn" disabled aria-disabled="true"', trade_block)
        self.assertNotIn("mbg-star-btn').addEventListener", self.html)

    # OLD: test_star_and_trade_present_but_explicitly_disabled also
    # asserted 'class="mbg-trade-link-btn" disabled aria-disabled="true"'
    # and assertNotIn("mbg-trade-link-btn').addEventListener", ...).
    # NEW: Trade is no longer disabled — it is wired to a real click
    # listener that opens #mobOrderPanel (the relocated new-order SL/TP
    # controls).
    # WHY: SL/TP-on-open-position editing (audited-then-authorized
    # sub-block) explicitly directed: "Reubica esos controles de orden
    # nueva dentro de un panel accesible desde Trade" — this reuses the
    # EXISTING dormant button for that real purpose rather than adding a
    # new one. Not a fabricated action: #mobOrderPanel contains the
    # SAME real mobSlInput/mobTpInput controls submitTicket() already
    # reads — only their visibility toggles.
    # OLD: test_trade_button_now_opens_real_order_panel asserted
    # id="mobTradePanelToggle"/an elTradePanelToggle click listener/
    # id="mobOrderPanel" hidden/mobSlInput/mobTpInput were all present.
    # NEW: all of that is GONE from this screen — Trade reverts to the
    # same honestly-disabled treatment as the star button.
    # WHY: Gráfico scope-removal round — explicit Owner decision: the
    # new-order SL/TP panel moves to a future, separately-designed
    # order-creation screen (not built yet); "no elimines la capacidad
    # funcional subyacente" — session.submitOrder() itself still fully
    # accepts sl/tp, only this screen's current UI source for them is
    # removed (readSlTp() always returns {sl:null,tp:null} now).
    def test_order_panel_removed_trade_button_disabled_again(self):
        trade_block = _trade_view_block(self.html)
        self.assertIn('class="mbg-trade-link-btn" disabled aria-disabled="true"', trade_block)
        self.assertNotIn('id="mobTradePanelToggle"', self.html)
        self.assertNotIn('id="mobOrderPanel"', self.html)
        self.assertNotIn('id="mobSlInput"', self.html)
        self.assertNotIn('id="mobTpInput"', self.html)
        self.assertNotIn('class="mbg-protect-row"', self.html)
        self.assertNotIn("setOrderPanelOpen", self.html)
        # The underlying capability is NOT removed — submitOrder() still
        # accepts sl/tp; only the current UI source for them is gone.
        self.assertIn("session.submitOrder({side,qty,sl,tp})", self.html)
        self.assertIn("function readSlTp(){", self.html)
        self.assertIn("return { sl: null, tp: null };", self.html)

    def test_live_badge_hidden_until_really_connected(self):
        # OLD: self.assertIn("mobLiveBadge\" hidden>LIVE<", self.html)
        # NEW: "EN VIVO" (not "LIVE").
        # WHY: 6-item correction round, item 4 — same principle as the
        # VENDER/COMPRAR/Lote/nav-label changes (equivalent real
        # functionality already exists — this badge is driven by the
        # real connection status, onStatusChange() below, unchanged) —
        # reproduced with the reference's own exact literal text
        # (money-broker-movil/grafico.html: <span class="live">EN
        # VIVO</span>).
        self.assertIn("mobLiveBadge\" hidden>EN VIVO<", self.html)
        self.assertIn("elLiveBadge.hidden=(String(state).toLowerCase()!=='connected');", self.html)


# ─────────────────────────────────────────────────────────────────────────
# L. 2nd correction round — overflow root cause, Máx/Mín/Vol honesty,
#    reactive theme-aware chart, Entry/TP/SL overlay pills
# ─────────────────────────────────────────────────────────────────────────
class SecondRoundCorrectionTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.style = _style_block(self.html)
        self.js = _mobile_chart_js_source()

    def _rule(self, selector):
        start = self.style.index(selector)
        end = self.style.index("}", start) + 1
        return self.style[start:end]

    def test_protect_row_overflow_fix_min_width_zero(self):
        # Root cause (confirmed with real scrollWidth/clientWidth
        # measurements at a genuine 390px viewport — default min-width:
        # auto on a flex item refuses to shrink below its own content's
        # intrinsic width, here driven by <input type="number">'s
        # browser-default minimum). Fix: min-width:0 on the flex item.
        body = self._rule('.mob-view[data-mobile-view="trade"] .mbg-protect-row label{')
        self.assertIn("min-width:0", body)

    def test_other_flex_rows_defensively_patched_too(self):
        for selector in (
            '.mob-view[data-mobile-view="trade"] .mbg-trade-bar .mbg-sell,',
            '.mob-view[data-mobile-view="trade"] .mbg-risk-confirm-buttons button{',
        ):
            self.assertIn(selector, self.style, selector)
        self.assertIn("min-width:0", self.style[self.style.index('.mob-navitem{'): self.style.index('}', self.style.index('.mob-navitem{'))])

    def test_max_min_vol_shown_explicitly_unavailable_not_substituted(self):
        trade_block = _trade_view_block(self.html)
        self.assertIn("M&aacute;x <b>&mdash;</b>", trade_block)
        self.assertIn("M&iacute;n <b>&mdash;</b>", trade_block)
        self.assertIn("Vol <b>&mdash;</b>", trade_block)

    def test_spread_lives_in_lot_box_real_reference_slot(self):
        # OLD: self.assertIn('Lot &middot; Spr <b id="mobQuoteSpread">', trade_block)
        # NEW: 'Lote' (not 'Lot').
        # WHY: 6-item correction round, item 4 — Owner-authorized text
        # change reproducing the reference's own exact literal label
        # ("Lote · Spr 23.0", money-broker-movil/grafico.html line 61)
        # where equivalent functionality already exists. The id
        # (mobQuoteSpread) and the real spread-derivation logic feeding
        # it are unchanged — only the visible label text.
        trade_block = _trade_view_block(self.html)
        self.assertIn('Lote &middot; Spr <b id="mobQuoteSpread">', trade_block)

    def test_apply_theme_colors_reads_real_css_vars_only(self):
        self.assertIn("applyThemeColors()", self.js)
        body_start = self.js.index("applyThemeColors() {")
        body_end = self.js.index("\n  }", body_start)
        body = self.js[body_start:body_end]
        for tok in ("--bg", "--muted", "--line", "--candle-up", "--candle-down", "getComputedStyle"):
            self.assertIn(tok, body, tok)
        for forbidden in ("margin_used", "commission_for", "computeRawPnL(", "* pos.qty", "pos.qty *"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_theme_observer_wired_without_touching_html_data_theme(self):
        self.assertIn("MutationObserver", self.html)
        self.assertIn("attributeFilter:['data-mb-theme']", self.html)
        self.assertNotIn("attributeFilter:['data-theme']", self.html)

    # OLD: this test read pos.avg/pos.pnl/pos.qty and the
    # _renderSlTpField('tp'/'sl', pos) calls straight out of
    # _renderOverlay(pos)'s single-position body.
    # NEW: multi-position correction round split this into
    # _renderEntryCapsule(pos) (pos.avg/pos.pnl/pos.qty — ONE call per
    # position) and _renderSelectedSlTp(pos) (the
    # _renderSlTpField('tp'/'sl', pos) calls — ONLY for the selected
    # position). pos.tp/pos.sl/_fmtPx() still live inside
    # _renderSlTpField()'s own body, unchanged.
    # WHY: Multi-position correction round — see setPositions()'s own
    # class-level comment in mobile_chart.js.
    def test_overlay_pills_use_real_position_fields_and_coordinate_api(self):
        capsule_start = self.js.index("_renderEntryCapsule(pos) {")
        capsule_end = self.js.index("\n  _renderSelectedSlTp", capsule_start)
        capsule_body = self.js[capsule_start:capsule_end]
        self.assertIn("pos.avg", capsule_body)
        self.assertIn("pos.pnl", capsule_body)
        self.assertIn("pos.qty", capsule_body)

        sel_start = self.js.index("_renderSelectedSlTp(pos) {")
        sel_end = self.js.index("\n  }", sel_start)
        sel_body = self.js[sel_start:sel_end]
        self.assertIn("this._renderSlTpField('tp', pos)", sel_body)
        self.assertIn("this._renderSlTpField('sl', pos)", sel_body)

        field_start = self.js.index("_renderSlTpField(field, pos) {")
        field_end = self.js.index("\n  _removePill", field_start)
        field_body = self.js[field_start:field_end]
        self.assertIn("pos[field]", field_body)
        self.assertIn("this._fmtPx(priceToShow)", field_body)
        # No dollar reward/risk figure computed for TP/SL (would require
        # a new financial calculation this file is not authorized for).
        for forbidden in ("pos.tp - pos.avg", "pos.avg - pos.sl", "* contractSize", "getContractSize("):
            self.assertNotIn(forbidden, field_body, forbidden)

    def test_close_button_reuses_real_session_close_flow(self):
        self.assertIn("mobileChart.onCloseClick = function(id){", self.html)
        body_start = self.html.index("mobileChart.onCloseClick = function(id){")
        body_end = self.html.index("};", body_start)
        body = self.html[body_start:body_end]
        self.assertIn("session.closePosition(id)", body)
        self.assertIn("closeInFlight", body)

    def test_reposition_called_on_every_real_candle_update(self):
        for anchor in ("appendCandle(rawBar) {", "updateLastCandle(rawBar) {"):
            start = self.js.index(anchor)
            end = self.js.index("\n  }", start)
            body = self.js[start:end]
            self.assertIn("this._repositionOverlay();", body, anchor)
