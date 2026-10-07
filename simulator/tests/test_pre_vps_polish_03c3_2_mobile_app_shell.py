# simulator/tests/test_pre_vps_polish_03c3_2_mobile_app_shell.py
"""
PRE-VPS-POLISH-03C.3.2 — Mobile App Shell + Primary Navigation.

Verifies that simulator/templates/simulator/trade/mobile.html's own
<style>/content/extra_scripts blocks now express a real app shell
(top app bar + scrollable view area holding 5 named views, one
visible at a time via the native [hidden] attribute + a persistent
bottom navigation bar) built ENTIRELY by reorganizing the existing
03C.2.1-03C.3.1 DOM — every id/class/data-attribute from those blocks
is preserved verbatim, the existing orchestrator <script> is
byte-identical (only a new, additive view-switcher <script> was
appended after it), and zero backend/WS/financial-engine file changed.
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


def _render():
    user = make_user()
    account = make_account(user, account_type="STANDARD")
    from django.test import Client

    client = Client()
    client.force_login(user)
    r = client.get(
        reverse("simulator:dashboard_account", args=[account.pk]),
        HTTP_USER_AGENT=IPHONE_UA,
    )
    return r


# ─────────────────────────────────────────────────────────────────────────
# A. App shell structure exists
# ─────────────────────────────────────────────────────────────────────────
class AppShellStructureTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.style = _style_block(self.html)

    def test_shell_frame_rule_present(self):
        self.assertIn(".mob-foundation{", self.style)
        self.assertIn("height:100dvh;", self.style)

    def test_topbar_rule_present(self):
        self.assertIn(".mob-topbar{", self.style)

    def test_view_area_and_view_rules_present(self):
        self.assertIn(".mob-view-area{", self.style)
        self.assertIn(".mob-view{", self.style)

    def test_bottomnav_rule_present(self):
        self.assertIn(".mob-bottomnav{", self.style)
        self.assertIn(".mob-navitem{", self.style)
        self.assertIn(".mob-navitem.is-active{", self.style)

    def test_topbar_markup_present(self):
        self.assertIn('<div class="mob-topbar">', self.html)

    def test_bottomnav_markup_present(self):
        self.assertIn('<div class="mob-bottomnav" id="mobBottomNav">', self.html)


# ─────────────────────────────────────────────────────────────────────────
# B. 5 primary destinations, Trade default, single active view
# ─────────────────────────────────────────────────────────────────────────
class NavigationDestinationsTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_exactly_five_nav_items(self):
        items = re.findall(r'class="mob-navitem[^"]*" data-target="(\w+)"', self.html)
        self.assertEqual(set(items), {"markets", "trade", "positions", "history", "account"})
        self.assertEqual(len(items), 5)

    def test_exactly_five_views(self):
        views = re.findall(r'data-mobile-view="(\w+)"', self.html)
        self.assertEqual(set(views), {"markets", "trade", "positions", "history", "account"})
        self.assertEqual(len(views), 5)

    def test_trade_is_default_active_nav_item(self):
        self.assertIn('class="mob-navitem is-active" data-target="trade"', self.html)
        for other in ("markets", "positions", "history", "account"):
            self.assertNotIn(f'class="mob-navitem is-active" data-target="{other}"', self.html)

    def test_only_trade_view_visible_by_default(self):
        pattern = re.compile(r'<div class="mob-view" data-mobile-view="(\w+)"( hidden)?>')
        matches = pattern.findall(self.html)
        self.assertEqual(len(matches), 5)
        visible = {name for name, hidden_attr in matches if not hidden_attr}
        hidden = {name for name, hidden_attr in matches if hidden_attr}
        self.assertEqual(visible, {"trade"})
        self.assertEqual(hidden, {"markets", "positions", "history", "account"})

    def test_view_switcher_script_is_additive_minimal_js(self):
        # No framework/router/event-bus/state-manager — just native
        # [hidden] + classList toggling, exactly as authorized.
        for forbidden in ["new Vue(", "new Router(", "React.", "EventTarget(", "addEventListener('popstate'"]:
            self.assertNotIn(forbidden, self.html)
        self.assertIn("v.hidden = (v.getAttribute('data-mobile-view') !== name);", self.html)
        self.assertIn("b.classList.toggle('is-active', b.getAttribute('data-target') === name);", self.html)
        self.assertIn("showView('trade');", self.html)


# ─────────────────────────────────────────────────────────────────────────
# C. Existing ids/wiring preserved verbatim
# ─────────────────────────────────────────────────────────────────────────
class ExistingWiringPreservedTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()
        self.head = _head_source()

    def test_every_existing_id_still_present(self):
        for el_id in EXISTING_IDS:
            self.assertIn(f'id="{el_id}"', self.html, el_id)

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: the entire
    # orchestrator <script> had to be byte-identical to HEAD. NEW
    # CONTRACT: TF-01 (a later, separately-authorized block)
    # legitimately rewrote exactly the MOBILE_TIMEFRAMES/
    # renderTfSelector section inside that same script — everything
    # BEFORE and AFTER that section must still be byte-identical to
    # HEAD. WHY preserved: still fails if the orchestrator is rewritten
    # anywhere outside the TF-01-authorized section.
    def test_orchestrator_script_preserved_outside_tf01_section(self):
        PREFIX_END = "function onVolumeUpdate(point){ mobileChart.updateVolume(point); }"
        SUFFIX_START = "// PRE-VPS-POLISH-03C.2.5 — order ticket foundation."

        old_main_script = _old_main_script(self.head)
        old_prefix = old_main_script[: old_main_script.index(PREFIX_END) + len(PREFIX_END)]
        old_suffix = old_main_script[old_main_script.index(SUFFIX_START):]

        new_main_script_start = self.html.index("<script>\n(function(){")
        new_main_script_end = self.html.index("\n})();\n</script>", new_main_script_start) + len("\n})();\n</script>")
        new_main_script = self.html[new_main_script_start:new_main_script_end]
        new_prefix = new_main_script[: new_main_script.index(PREFIX_END) + len(PREFIX_END)]
        new_suffix = new_main_script[new_main_script.index(SUFFIX_START):]

        self.assertEqual(old_prefix, new_prefix)
        self.assertEqual(old_suffix, new_suffix)

    def test_exactly_one_new_appended_script_after_orchestrator(self):
        new_main_script_start = self.html.index("<script>\n(function(){")
        new_main_script_end = self.html.index("\n})();\n</script>", new_main_script_start) + len("\n})();\n</script>")
        new_main_script = self.html[new_main_script_start:new_main_script_end]
        idx = new_main_script_end
        tail = self.html[idx:]
        self.assertEqual(tail.count("<script>"), 1)
        self.assertIn("PRE-VPS-POLISH-03C.3.2 — app-shell view switcher", tail)

    def test_positions_pending_closed_still_present(self):
        for marker in ("mobPositionsPane", "mobPendingPane", "mobClosedPane", 'id="mobPaneTabs"'):
            self.assertIn(marker, self.html)

    def test_watchlist_catalog_still_present(self):
        self.assertIn('id="mobWatchlist"', self.html)
        self.assertIn("const MOBILE_SYMBOLS = ", self.html)

    def test_chart_container_still_present(self):
        self.assertIn('id="mobChartContainer"', self.html)

    def test_order_ticket_still_present(self):
        for marker in ("mobTicket", "mobBuyBtn", "mobSellBtn", "mobQtyInput", "mobSlInput", "mobTpInput"):
            self.assertIn(marker, self.html)

    def test_no_duplicate_catalog_or_positions_store_introduced(self):
        self.assertEqual(self.html.count('id="mobWatchlist"'), 1)
        self.assertEqual(self.html.count('id="mobPositionsList"'), 1)
        self.assertEqual(self.html.count('id="mobClosedList"'), 1)
        self.assertNotIn("getCanonicalPositions2", self.html)
        self.assertNotIn("_mobilePositionsShell", self.html)


# ─────────────────────────────────────────────────────────────────────────
# D. Safe areas + design tokens reused, no new ad-hoc colors
# ─────────────────────────────────────────────────────────────────────────
class TokensAndSafeAreaReuseTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_topbar_and_bottomnav_use_safe_area_tokens(self):
        self.assertIn("var(--mb-safe-top)", self.style)
        self.assertIn("var(--mb-safe-bottom)", self.style)

    def test_new_shell_rules_use_only_existing_tokens(self):
        shell_start = self.style.index(".mob-foundation{")
        shell_end = self.style.index(".mob-navitem.is-active{color:var(--mb-brand);}") + len(
            ".mob-navitem.is-active{color:var(--mb-brand);}"
        )
        shell_css = self.style[shell_start:shell_end]
        hex_literals = re.findall(r"#[0-9a-fA-F]{3,6}", shell_css)
        rgba_literals = re.findall(r"rgba\([^)]*\)", shell_css)
        self.assertEqual(hex_literals, [])
        self.assertEqual(rgba_literals, [])

    def test_no_second_root_token_block_introduced(self):
        self.assertEqual(self.style.count(":root{"), 1)


# ─────────────────────────────────────────────────────────────────────────
# E. No WS/financial changes introduced anywhere in the file
# ─────────────────────────────────────────────────────────────────────────
class NoWsOrFinancialChangeTests(SimpleTestCase):
    def setUp(self):
        self.html = _mobile_html_source()

    def test_exactly_one_websocket_construction_site_in_file(self):
        # mobile.html itself never constructs a WebSocket (that is
        # mobile_session.js's exclusive job, confirmed zero-diff below)
        # — this guards against the shell's new script accidentally
        # introducing a second one.
        self.assertNotIn("new WebSocket(", self.html)

    def test_no_new_ws_action_strings_in_view_switcher_script(self):
        switcher_start = self.html.index("PRE-VPS-POLISH-03C.3.2 — app-shell view switcher")
        switcher_script = self.html[switcher_start:]
        for forbidden in ["action:", "action :", ".send(", "order:", "get_closed_trades"]:
            self.assertNotIn(forbidden, switcher_script)

    def test_no_financial_arithmetic_in_view_switcher_script(self):
        switcher_start = self.html.index("PRE-VPS-POLISH-03C.3.2 — app-shell view switcher")
        switcher_script = self.html[switcher_start:]
        for forbidden in ["*qty", "price", "margin", "commission", "pnl", "spread"]:
            self.assertNotIn(forbidden, switcher_script.lower())


# ─────────────────────────────────────────────────────────────────────────
# F. End-to-end render: shell renders, default view correct, nav present
# ─────────────────────────────────────────────────────────────────────────
class AppShellRenderTests(TestCase):
    def test_shell_renders_with_trade_default_and_full_nav(self):
        r = _render()
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("simulator/trade/mobile.html", {t.name for t in r.templates if t.name})
        self.assertIn('<div class="mob-topbar">', body)
        self.assertIn('id="mobBottomNav"', body)
        for label in ("MARKETS", "TRADE", "POSITIONS", "HISTORY", "ACCOUNT"):
            self.assertIn(f">{label}<", body)
        self.assertIn('data-mobile-view="trade">', body)
        self.assertIn('data-mobile-view="markets" hidden>', body)


# ─────────────────────────────────────────────────────────────────────────
# G. Zero diff — Desktop/backend/financial engine/other Mobile files
# ─────────────────────────────────────────────────────────────────────────
class ProtectedFilesZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: blanket zero-diff.
    # NEW CONTRACT: TF-01 (separately authorized) narrowed
    # MOBILE_TIMEFRAMES to the 5-entry public catalog. WHY preserved:
    # still fails if the diff ever touches order/position/risk/close
    # wiring, or if the catalog itself drifts from the authorized set.
    def test_mobile_session_js_diff_scoped_to_tf01_timeframe_catalog_only(self):
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
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '1d'];", src)

    def test_mobile_chart_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_chart.js")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: blanket zero-diff.
    # NEW CONTRACT: TF-01 removed "1s" from desktop.html's 3 selector
    # entries. WHY preserved: still fails if the diff touches anything
    # beyond those removals.
    def test_desktop_html_diff_scoped_to_tf01_1s_removal_only(self):
        result = subprocess.run(
            ["git", "diff", "--", "simulator/templates/simulator/trade/desktop.html"],
            capture_output=True, text=True,
        )
        diff = result.stdout
        removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        for line in removed:
            self.assertIn("1s", line, line)
        for forbidden in (
            "sendOrder", "closePosition", "commission", "margin_used", "BrokerLedger",
            "computeRawPnL", "risk_preview", "'order:new'", "'order:close'",
        ):
            self.assertNotIn(forbidden, diff, forbidden)
        with open("simulator/templates/simulator/trade/desktop.html", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('value="1s"', src)
        self.assertNotIn('data-tf="1s"', src)

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
