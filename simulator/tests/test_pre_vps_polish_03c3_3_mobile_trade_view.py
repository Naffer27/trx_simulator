# simulator/tests/test_pre_vps_polish_03c3_3_mobile_trade_view.py
"""
PRE-VPS-POLISH-03C.3.3 — Mobile Trade View + Chart Visual Polish.

Verifies that the visual redesign of the Trade view inside
simulator/templates/simulator/trade/mobile.html (symbol/price header
hierarchy, chart made the protagonist, compact ticket grid) is purely
presentational: every existing id/class/data-attribute the certified
Mobile engine depends on (03C.2.1-03C.2.6) is preserved exactly once,
the app shell from 03C.3.2 is untouched, the pre-existing orchestrator
<script> is byte-identical, no new WebSocket/financial computation/SL-
TP-drag/order:update exists anywhere, and zero backend/financial-engine
file changed.
"""
import re
import subprocess

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

MOBILE_HTML_PATH = "simulator/templates/simulator/trade/mobile.html"

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

EXISTING_IDS = [
    "mobConnStatus", "mobConnStatusLabel",
    "mobBalance", "mobEquity", "mobMargin", "mobFree", "mobUpnl", "mobLeverage",
    "mobQuoteSymbol", "mobQuoteBid", "mobQuoteAsk", "mobQuoteMid", "mobQuoteSource",
    "mobWatchlist", "mobTfSelector", "mobChartContainer",
    "mobTicket", "mobQtyInput", "mobSlInput", "mobTpInput",
    "mobSellBtn", "mobBuyBtn", "mobTicketStatus",
    "mobRiskPanel", "mobRiskLevel", "mobRiskMargin", "mobRiskExposure",
    "mobRiskConfirm", "mobRiskConfirmText", "mobRiskCancelBtn", "mobRiskConfirmBtn",
    "mobPaneTabs", "mobPositionsPane", "mobPositionsList", "mobPositionsEmpty",
    "mobPendingPane", "mobPendingList", "mobPendingEmpty",
    "mobClosedPane", "mobClosedList", "mobClosedEmpty",
    "mobBottomNav",
]


def _mobile_html_source():
    with open(MOBILE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


def _style_block(source):
    return source[source.index("<style>") : source.index("</style>")]


def _head_source():
    result = subprocess.run(
        ["git", "show", "HEAD:" + MOBILE_HTML_PATH],
        capture_output=True,
        text=True,
    )
    return result.stdout


def _old_main_script(head_source):
    start = head_source.index("<script>\n(function(){")
    end = head_source.index("\n})();\n</script>", start) + len("\n})();\n</script>")
    return head_source[start:end]


def _old_switcher_script(head_source):
    start = head_source.index("PRE-VPS-POLISH-03C.3.2 — app-shell view switcher")
    return head_source[start:]


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
# A. Trade remains default; app shell + bottom nav preserved
# ─────────────────────────────────────────────────────────────────────────
class ShellAndDefaultViewPreservedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_trade_still_default_visible_view(self):
        views = re.findall(r'<div class="mob-view" data-mobile-view="(\w+)"( hidden)?>', self.html)
        self.assertEqual(len(views), 5)
        visible = {name for name, hidden_attr in views if not hidden_attr}
        hidden = {name for name, hidden_attr in views if hidden_attr}
        self.assertEqual(visible, {"trade"})
        self.assertEqual(hidden, {"markets", "positions", "history", "account"})

    def test_trade_still_default_active_nav_item(self):
        self.assertIn('class="mob-navitem is-active" data-target="trade"', self.html)

    def test_app_shell_regions_untouched(self):
        self.assertIn('<div class="mob-topbar">', self.html)
        self.assertIn('<div class="mob-view-area">', self.html)
        self.assertIn('<div class="mob-bottomnav" id="mobBottomNav">', self.html)
        for label in ("MARKETS", "TRADE", "POSITIONS", "HISTORY", "ACCOUNT"):
            self.assertIn(f">{label}<", self.html)

    def test_shell_css_rules_byte_identical_to_head(self):
        head_style = _style_block(_head_source())
        new_style = _style_block(self.html)
        for selector in (".mob-foundation{", ".mob-topbar{", ".mob-view-area{", ".mob-bottomnav{", ".mob-navitem{", ".mob-navitem.is-active{"):
            old_body = head_style[head_style.index(selector): head_style.index("}", head_style.index(selector)) + 1]
            new_body = new_style[new_style.index(selector): new_style.index("}", new_style.index(selector)) + 1]
            self.assertEqual(old_body, new_body, selector)


# ─────────────────────────────────────────────────────────────────────────
# B. Every existing functional id present exactly once
# ─────────────────────────────────────────────────────────────────────────
class ExistingIdsPreservedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_every_id_present_exactly_once(self):
        for el_id in EXISTING_IDS:
            self.assertEqual(self.html.count(f'id="{el_id}"'), 1, el_id)

    def test_no_duplicate_catalog_or_positions_store(self):
        self.assertEqual(self.html.count('id="mobWatchlist"'), 1)
        self.assertEqual(self.html.count('id="mobPositionsList"'), 1)
        self.assertNotIn("getCanonicalPositions2", self.html)
        self.assertNotIn("_mobilePositionsShell", self.html)


# ─────────────────────────────────────────────────────────────────────────
# C. Chart, timeframe catalog, BUY/SELL, Volume/SL/TP, risk UI intact
# ─────────────────────────────────────────────────────────────────────────
class TradeComponentsIntactTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_chart_container_present(self):
        self.assertIn('<div id="mobChartContainer"></div>', self.html)

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: the public catalog
    # was exactly 6 entries including "1s". NEW CONTRACT (TF-01,
    # separately authorized): "1s" is retired from the public UI — the
    # catalog is now exactly the 5 entries 1m/5m/15m/1h/1d. WHY
    # preserved: still fails if 4h is ever accidentally enabled, and
    # now ALSO fails if "1s" is ever reintroduced.
    # PRE-VPS-POLISH-03C.3.TF-02A — OLD CONTRACT CHAIN: 03C.3.3's
    # original catalog was 6 entries including "1s"; TF-01 narrowed it
    # to 5 (1m/5m/15m/1h/1d). NEW CONTRACT: TF-02 (separately
    # authorized) added real end-to-end "4h" support — the catalog is
    # now exactly the 6 entries 1m/5m/15m/1h/4h/1d. WHY preserved:
    # still fails if "1s" is ever reintroduced, and fails if the real
    # 6-entry order ever drifts.
    def test_timeframe_catalog_exactly_six_no_1s(self):
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m','5m','15m','1h','4h','1d'];", self.html)
        self.assertNotIn("['1s','1m','5m','15m','1h','1d']", self.html)
        self.assertNotIn("['1m','5m','15m','1h','1d']", self.html)

    def test_buy_sell_controls_present_and_market_only(self):
        self.assertIn('<button id="mobSellBtn" type="button">SELL</button>', self.html)
        self.assertIn('<button id="mobBuyBtn" type="button">BUY</button>', self.html)
        for forbidden in ("Limit", "Stop", "order:pending:new", "trigPrice", "ordType"):
            self.assertNotIn(forbidden, self.html)

    def test_volume_sl_tp_inputs_present(self):
        self.assertIn('<input type="number" id="mobQtyInput" step="0.01" min="0" value="0.01">', self.html)
        self.assertIn('<input type="number" id="mobSlInput" placeholder="—">', self.html)
        self.assertIn('<input type="number" id="mobTpInput" placeholder="—">', self.html)

    def test_risk_ui_present(self):
        for marker in ("mobRiskPanel", "mobRiskLevel", "mobRiskMargin", "mobRiskExposure",
                       "mobRiskConfirm", "mobRiskConfirmText", "mobRiskCancelBtn", "mobRiskConfirmBtn"):
            self.assertIn(marker, self.html)

    def test_no_spread_or_daily_change_fabricated(self):
        # Design lock Section 6: no real backend field exists for
        # either on this contract — must stay omitted, never fabricated.
        # Checked against the rendered markup with HTML comments
        # stripped, since this file's own explanatory comments
        # legitimately discuss (in English prose) why those fields are
        # absent — that prose is not a rendered label.
        trade_start = self.html.index('data-mobile-view="trade"')
        trade_end = self.html.index('data-mobile-view="markets"')
        trade_html = re.sub(r"<!--.*?-->", "", self.html[trade_start:trade_end], flags=re.DOTALL)
        for forbidden in (">Spread<", "spread_pct", "dailyChange", "daily_change", "pctChange"):
            self.assertNotIn(forbidden, trade_html)


# ─────────────────────────────────────────────────────────────────────────
# D. No SL/TP drag, no order:update, no new WS, no local financial calc
# ─────────────────────────────────────────────────────────────────────────
class NoFutureFunctionalityImplementedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_no_order_update_action(self):
        self.assertNotIn("order:update", self.html)

    def test_no_sltp_drag_implemented(self):
        for forbidden in [
            "pointerdown", "pointermove", "pointerup", "touchstart", "touchmove", "touchend",
            "mousedown", "mousemove", "dragSL", "dragTP", "createPriceLine", "setPriceLine",
        ]:
            self.assertNotIn(forbidden, self.html)

    def test_no_new_websocket_construction_site(self):
        self.assertNotIn("new WebSocket(", self.html)

    def test_no_local_financial_computation_introduced(self):
        # The new CSS/HTML this block adds must never compute margin/
        # commission/P&L/spread locally. Scoped to the style block +
        # the new wrapper markup only (the pre-existing orchestrator
        # script legitimately contains "margin"/"pnl" as id/string
        # literals from earlier certified blocks — not what this check
        # is for).
        style = _style_block(self.html)
        for forbidden in ["*qty", "qty*", "price-entry", "(price-", "commission_for", "spread_engine"]:
            self.assertNotIn(forbidden, style)


# ─────────────────────────────────────────────────────────────────────────
# E. Design tokens reused, no new ad-hoc colors outside :root
# ─────────────────────────────────────────────────────────────────────────
class DesignTokenReuseTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_no_second_root_block(self):
        self.assertEqual(self.style.count(":root{"), 1)

    def test_no_color_literal_outside_root_block(self):
        root_start = self.style.index(":root{")
        depth = 0
        end = None
        for idx in range(root_start + len(":root{") - 1, len(self.style)):
            ch = self.style[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        rest = self.style[:root_start] + self.style[end:]
        hex_literals = re.findall(r"#[0-9a-fA-F]{3,6}", rest)
        rgba_literals = re.findall(r"rgba\([^)]*\)", rest)
        self.assertEqual(hex_literals, [])
        self.assertEqual(rgba_literals, [])

    def test_new_text_xl_token_declared_in_root_only(self):
        self.assertEqual(self.style.count("--mb-text-xl:"), 1)
        root_start = self.style.index(":root{")
        root_end = self.style.index("}", root_start) + 1
        self.assertIn("--mb-text-xl:", self.style[root_start:root_end])

    def test_trade_view_new_rules_consume_only_existing_or_new_root_tokens(self):
        for selector in (
            ".mob-quote .mq-price{", ".mob-quote .sym{", ".mob-chart-wrap{",
            ".mob-ticket-fields{", ".mob-ticket-row input{",
        ):
            body = self.style[self.style.index(selector): self.style.index("}", self.style.index(selector)) + 1]
            self.assertNotRegex(body, r"#[0-9a-fA-F]{3,6}")
            self.assertNotRegex(body, r"rgba\(")


# ─────────────────────────────────────────────────────────────────────────
# F. Responsive structure compatible (no fixed-width traps)
# ─────────────────────────────────────────────────────────────────────────
class ResponsiveStructureTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_trade_only_cards_no_longer_capped_at_360px(self):
        for selector in (".mob-quote{", ".mob-tf{", ".mob-chart-wrap{", ".mob-ticket{"):
            body = self.style[self.style.index(selector): self.style.index("}", self.style.index(selector)) + 1]
            self.assertIn("max-width:none", body, selector)

    def test_chart_height_is_viewport_relative_with_floor_and_ceiling(self):
        body = self.style[self.style.index(".mob-chart-wrap{"): self.style.index("}", self.style.index(".mob-chart-wrap{")) + 1]
        self.assertIn("height:44vh", body)
        self.assertIn("min-height:240px", body)
        self.assertIn("max-height:420px", body)

    def test_ticket_fields_grid_uses_minmax_zero_to_avoid_overflow(self):
        body = self.style[self.style.index(".mob-ticket-fields{"): self.style.index("}", self.style.index(".mob-ticket-fields{")) + 1]
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", body)

    def test_other_views_retain_their_360px_cap_unchanged(self):
        # Markets/Positions card widths are explicitly out of scope for
        # this block — confirms they were not touched.
        for selector in (".mob-watchlist{", ".mob-pane-tabs{", ".mob-pane{"):
            body = self.style[self.style.index(selector): self.style.index("}", self.style.index(selector)) + 1]
            self.assertIn("max-width:360px", body, selector)


# ─────────────────────────────────────────────────────────────────────────
# G. Existing orchestrator + view switcher scripts byte-identical
# ─────────────────────────────────────────────────────────────────────────
class OrchestratorPreservedTests(SimpleTestCase):
    def setUp(self):
        self.old = _head_source()
        self.new = _mobile_html_source()

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: the entire
    # orchestrator <script> had to be byte-identical to HEAD. NEW
    # CONTRACT: TF-01 (a later, separately-authorized block)
    # legitimately rewrote exactly the MOBILE_TIMEFRAMES/
    # renderTfSelector section inside that same script — everything
    # BEFORE and AFTER that section must still be byte-identical to
    # HEAD. WHY preserved: still fails if the orchestrator is rewritten
    # anywhere outside the TF-01-authorized section.
    def test_main_orchestrator_script_preserved_outside_tf01_section(self):
        PREFIX_END = "function onVolumeUpdate(point){ mobileChart.updateVolume(point); }"
        SUFFIX_START = "// PRE-VPS-POLISH-03C.2.5 — order ticket foundation."

        old_main_script = _old_main_script(self.old)
        old_prefix = old_main_script[: old_main_script.index(PREFIX_END) + len(PREFIX_END)]
        old_suffix = old_main_script[old_main_script.index(SUFFIX_START):]

        new_main_script_start = self.new.index("<script>\n(function(){")
        new_main_script_end = self.new.index("\n})();\n</script>", new_main_script_start) + len("\n})();\n</script>")
        new_main_script = self.new[new_main_script_start:new_main_script_end]
        new_prefix = new_main_script[: new_main_script.index(PREFIX_END) + len(PREFIX_END)]
        new_suffix = new_main_script[new_main_script.index(SUFFIX_START):]

        self.assertEqual(old_prefix, new_prefix)
        self.assertEqual(old_suffix, new_suffix)

    def test_view_switcher_script_byte_identical(self):
        self.assertIn(_old_switcher_script(self.old), self.new)

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: assumed HEAD's
    # `content` block would always differ from the working tree's
    # (true only in 03C.3.3's own commit window, before its Trade view
    # markup was committed to HEAD). NEW CONTRACT: HEAD now already
    # includes 03C.3.3's own Trade view markup, and TF-01 never
    # touches `content` (HTML markup) at all — only the orchestrator
    # `<script>`'s timeframe catalog section. The correct, stronger
    # assertion today is that `content` is BYTE-IDENTICAL to HEAD.
    def test_content_block_untouched_by_tf01(self):
        marker_start = "{% block content %}"
        marker_end = "{% endblock %}\n\n{% block extra_scripts %}"
        old_content = self.old[self.old.index(marker_start): self.old.index(marker_end)]
        new_content = self.new[self.new.index(marker_start): self.new.index(marker_end)]
        self.assertEqual(old_content, new_content)


# ─────────────────────────────────────────────────────────────────────────
# H. End-to-end render smoke test
# ─────────────────────────────────────────────────────────────────────────
class TradeViewRenderTests(TestCase):
    def test_trade_view_renders_with_new_hierarchy(self):
        r = _render()
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('<span class="sym" id="mobQuoteSymbol">—</span>', body)
        self.assertIn('<span class="mq-price" id="mobQuoteMid">—</span>', body)
        self.assertIn('<div class="mob-chart-block">', body)
        self.assertIn('<div class="mob-ticket-fields">', body)
        self.assertIn('id="mobChartContainer"', body)
        self.assertIn('id="mobBuyBtn"', body)
        self.assertIn('id="mobSellBtn"', body)


# ─────────────────────────────────────────────────────────────────────────
# I. Zero diff — Desktop/backend/financial engine/other Mobile files
# ─────────────────────────────────────────────────────────────────────────
class ProtectedFilesZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    # PRE-VPS-POLISH-03C.3.TF-02A — OLD CONTRACT CHAIN: TF-01 narrowed
    # MOBILE_TIMEFRAMES to the 5-entry public catalog (blanket zero-
    # diff superseded then). NEW CONTRACT: TF-02 (separately
    # authorized) grew it to the real 6-entry catalog (added "4h").
    # WHY preserved: still fails if the diff ever touches order/
    # position/risk/close wiring, or if the catalog itself drifts from
    # the authorized set.
    def test_mobile_session_js_diff_scoped_to_timeframe_catalog_only(self):
        result = subprocess.run(
            ["git", "diff", "--", "simulator/static/simulator/trade/mobile_session.js"],
            capture_output=True, text=True,
        )
        diff = result.stdout
        self.assertIn("MOBILE_TIMEFRAMES", diff)
        for forbidden in (
            "submitOrder(", "closePosition(", "cancelPendingOrder(", "confirmRiskWarning(",
            "requestRiskPreview", "new WebSocket(", "_handleMsg(msg)",
        ):
            self.assertNotIn(forbidden, diff, forbidden)
        with open("simulator/static/simulator/trade/mobile_session.js", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'];", src)

    def test_mobile_chart_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_chart.js")

    # PRE-VPS-POLISH-03C.3.TF-02A — OLD CONTRACT: blanket zero-diff
    # (trading_core.js had never been touched by any Mobile sub-block).
    # NEW CONTRACT: TF-02B added exactly one key ('4h':14400) to the
    # existing tfToSec() lookup (Desktop's shared, non-financial
    # timeframe-seconds helper), closing the same silent-fallback gap
    # class TF-01 fixed on the backend. WHY preserved: still fails if
    # trading_core.js's diff ever touches any protected financial/
    # transport surface (P&L, contract size, canonical positions,
    # price authority, order execution, margin, spread, commissions,
    # Broker Ledger, risk, WebSocket/transport, candle construction) —
    # only the tfToSec() dict is allowed to change, and every removed
    # line must be that one definition.
    def test_trading_core_js_diff_scoped_to_4h_timeframe_only(self):
        result = subprocess.run(
            ["git", "diff", "--", "simulator/static/simulator/trade/trading_core.js"],
            capture_output=True, text=True,
        )
        diff = result.stdout
        self.assertIn("const tfToSec=", diff)
        self.assertIn("'4h':14400", diff)
        for forbidden in (
            "computeRawPnL(", "computePositionPnL(", "getContractSize(", "LOT_SPECS=",
            "applyPriceTickState(", "getCanonicalPositions(", "replaceCanonicalPositions(",
            "commission_for", "margin_used", "spread_revenue", "BrokerLedger",
            "new WebSocket(", "_calcSMA", "_calcEMA", "_calcRSI",
        ):
            self.assertNotIn(forbidden, diff, forbidden)
        removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        for line in removed:
            self.assertIn("tfToSec", line, line)

    # PRE-VPS-POLISH-03C.3.TF-02A — OLD CONTRACT (TF-01A): the diff had
    # to be exactly the "1s" removal. NEW CONTRACT: TF-02 ALSO inserted
    # "4h" between "1h" and "1d" in the same 3 selector locations. WHY
    # preserved: still fails if the diff touches anything beyond those
    # legitimate timeframe-catalog changes.
    def test_desktop_html_diff_scoped_to_timeframe_catalog_only(self):
        result = subprocess.run(
            ["git", "diff", "--", "simulator/templates/simulator/trade/desktop.html"],
            capture_output=True, text=True,
        )
        diff = result.stdout
        changed = [
            l for l in diff.splitlines()
            if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))
        ]
        self.assertTrue(changed, "expected a real timeframe-catalog diff")
        for line in changed:
            self.assertTrue(("1s" in line) or ("4h" in line) or ("1h" in line and "1d" in line), line)
        for forbidden in (
            "sendOrder", "closePosition", "commission", "margin_used", "BrokerLedger",
            "computeRawPnL", "risk_preview", "'order:new'", "'order:close'",
        ):
            self.assertNotIn(forbidden, diff, forbidden)
        with open("simulator/templates/simulator/trade/desktop.html", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('value="1s"', src)
        self.assertNotIn('data-tf="1s"', src)
        self.assertIn('value="4h"', src)
        self.assertIn('data-tf="4h"', src)

    def test_shell_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/shell.html")

    def test_views_py_zero_diff(self):
        self._assert_zero_diff("simulator/views.py")

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: blanket zero-diff.
    # NEW CONTRACT: TF-01 rewrote tf_seconds()/normalize_tf() to be
    # fail-closed and added explicit invalid_timeframe rejection. WHY
    # preserved: still fails if that diff ever touches order/position/
    # P&L/margin/commission/ledger code.
    def test_consumers_py_diff_scoped_to_tf01_timeframe_helpers_only(self):
        with open("simulator/consumers.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_TF_ALIASES", src)
        self.assertIn("_TF_SECONDS", src)
        self.assertIn('"code": "invalid_timeframe"', src)
        result = subprocess.run(["git", "diff", "--", "simulator/consumers.py"], capture_output=True, text=True)
        diff = result.stdout
        for forbidden in (
            "commission_for", "calculate_spread_revenue", "broker_price(",
            "BrokerLedger", "LedgerEntry", "pnl_engine", "margin_used",
            "_check_tp_sl", "_check_pending_triggers", "_order_new(", "_order_close(",
            "_db_open_position", "_db_close_position",
        ):
            self.assertNotIn(forbidden, diff, forbidden)

    def test_routing_py_zero_diff(self):
        self._assert_zero_diff("simulator/routing.py")

    def test_asgi_py_zero_diff(self):
        self._assert_zero_diff("trx_simulator/asgi.py")

    def test_models_py_zero_diff(self):
        self._assert_zero_diff("simulator/models.py")

    def test_migrations_zero_diff(self):
        self._assert_zero_diff("simulator/migrations")

    def test_symbol_specs_py_zero_diff(self):
        self._assert_zero_diff("market_data/symbol_specs.py")

    def test_spread_engine_py_zero_diff(self):
        self._assert_zero_diff("simulator/spread_engine.py")
