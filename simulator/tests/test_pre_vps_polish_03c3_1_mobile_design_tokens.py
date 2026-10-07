# simulator/tests/test_pre_vps_polish_03c3_1_mobile_design_tokens.py
"""
PRE-VPS-POLISH-03C.3.1 — Mobile Design Tokens / Typography / Color System
Foundation.

Verifies the CSS custom-property token system introduced in
simulator/templates/simulator/trade/mobile.html's own <style> block:
one single visual authority (surfaces/text/brand/buy-sell/P&L/status/
focus/typography/spacing/radius/elevation/touch-target/safe-area
tokens), every component rule consuming it (no literal color/size
left duplicated outside :root), and — critically — zero change to any
id, class name, data-attribute, callback, WS contract, or backend/
financial file. This block is CSS-only; it does not touch
mobile_session.js, mobile_chart.js, trading_core.js, desktop.html,
shell.html, or any backend file.
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


# ─────────────────────────────────────────────────────────────────────────
# A. Tokens present
# ─────────────────────────────────────────────────────────────────────────
class TokensPresentTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_root_block_exists(self):
        self.assertIn(":root{", self.style)

    def test_surface_and_border_tokens_present(self):
        for tok in ["--mb-bg", "--mb-surface-1", "--mb-surface-2", "--mb-border", "--mb-border-strong"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_text_tokens_present(self):
        for tok in ["--mb-text-primary", "--mb-text-secondary", "--mb-text-muted"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_brand_tokens_present(self):
        for tok in ["--mb-brand", "--mb-brand-strong", "--mb-brand-dim", "--mb-brand-border"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_buy_sell_tokens_present(self):
        for tok in ["--mb-buy", "--mb-buy-dim", "--mb-buy-border", "--mb-sell", "--mb-sell-dim", "--mb-sell-border"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_positive_negative_tokens_present(self):
        self.assertIn("--mb-positive:", self.style)
        self.assertIn("--mb-negative:", self.style)

    def test_status_tokens_present(self):
        for tok in ["--mb-status-neutral", "--mb-status-pending", "--mb-status-live", "--mb-status-down"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_focus_token_present(self):
        self.assertIn("--mb-focus:", self.style)

    def test_typography_tokens_present(self):
        for tok in [
            "--mb-font-sans", "--mb-text-2xs", "--mb-text-xs", "--mb-text-sm",
            "--mb-text-base", "--mb-text-md", "--mb-text-lg", "--mb-tracking-label",
        ]:
            self.assertIn(tok + ":", self.style, tok)

    def test_spacing_tokens_present(self):
        for i in range(1, 7):
            self.assertIn("--mb-space-%d:" % i, self.style)

    def test_radius_tokens_present(self):
        for tok in ["--mb-radius-sm", "--mb-radius-md", "--mb-radius-lg"]:
            self.assertIn(tok + ":", self.style, tok)

    def test_elevation_tokens_present(self):
        self.assertIn("--mb-shadow-1:", self.style)
        self.assertIn("--mb-shadow-2:", self.style)

    def test_touch_target_tokens_present(self):
        self.assertIn("--mb-touch-min:44px", self.style)
        self.assertIn("--mb-touch-sm:36px", self.style)

    def test_safe_area_tokens_present(self):
        self.assertIn("--mb-safe-top:env(safe-area-inset-top,0px)", self.style)
        self.assertIn("--mb-safe-bottom:env(safe-area-inset-bottom,0px)", self.style)


# ─────────────────────────────────────────────────────────────────────────
# B. Single accent system — brand gold, not dominant, no competing palette
# ─────────────────────────────────────────────────────────────────────────
class SingleAccentSystemTests(SimpleTestCase):
    def setUp(self):
        self.source = _mobile_html_source()
        self.style = _style_block(self.source)

    def test_old_ad_hoc_cyan_fully_retired(self):
        # 03C.2.1-03C.2.6 used an arbitrary #00b4d8 cyan accent with its
        # own explicit "NOT the final visual design" comments. This
        # block is that final design — the old hue must not survive
        # anywhere (it would be a second, competing accent system).
        self.assertNotIn("#00b4d8", self.source)

    def test_no_color_literal_outside_root_block(self):
        # Every hex/rgba literal must live exactly once, inside :root;
        # every component rule must consume it via var(--mb-*) — this
        # is what "one single visual authority" means operationally.
        root_start = self.style.index(":root{")
        root_end = self.style.index("}", root_start) + 1
        # :root{...} may itself contain nested closing braces only from
        # var()/rgba() calls, never a second top-level "}" before the
        # block's own properties end — walk brace depth to find the
        # true end of the :root rule.
        depth = 0
        i = root_start + len(":root{") - 1
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
        root_block = self.style[root_start:end]
        rest = self.style[:root_start] + self.style[end:]
        hex_literals = re.findall(r"#[0-9a-fA-F]{3,6}", rest)
        rgba_literals = re.findall(r"rgba\([^)]*\)", rest)
        self.assertEqual(hex_literals, [])
        self.assertEqual(rgba_literals, [])
        # Sanity: the literals DO exist inside :root (proves the test
        # isn't vacuously passing because nothing defines colors at all).
        self.assertTrue(re.search(r"#[0-9a-fA-F]{3,6}", root_block))

    def test_brand_accent_used_for_title_not_body_background(self):
        self.assertIn(".mob-foundation h1{font-size:var(--mb-text-lg)", self.style)
        self.assertIn("color:var(--mb-brand);margin:0;}", self.style)
        # the page background itself must never be the brand color —
        # brand stays an accent, not a dominant surface.
        self.assertIn("body{background:var(--mb-bg)", self.style)

    def test_brand_used_only_as_sparse_selection_tint_not_solid_fill(self):
        # Selected states use the dim/translucent variant (a tint), not
        # the raw --mb-brand as a solid background — "accent, not
        # dominant" implemented literally: no selector sets
        # background:var(--mb-brand) (solid brand fill).
        self.assertNotIn("background:var(--mb-brand);", self.style)
        self.assertIn("background:var(--mb-brand-dim)", self.style)


# ─────────────────────────────────────────────────────────────────────────
# C. BUY/SELL semantic colors
# ─────────────────────────────────────────────────────────────────────────
class BuySellSemanticsTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_buy_button_uses_buy_token(self):
        self.assertIn(
            "#mobBuyBtn{background:var(--mb-buy-dim);border-color:var(--mb-buy-border);color:var(--mb-buy);}",
            self.style,
        )

    def test_sell_button_uses_sell_token(self):
        self.assertIn(
            "#mobSellBtn{background:var(--mb-sell-dim);border-color:var(--mb-sell-border);color:var(--mb-sell);}",
            self.style,
        )

    def test_position_side_badges_use_buy_sell_tokens(self):
        self.assertIn(".mob-row-card .rc-side-buy{color:var(--mb-buy);}", self.style)
        self.assertIn(".mob-row-card .rc-side-sell{color:var(--mb-sell);}", self.style)

    def test_connection_status_live_down_reuse_buy_sell_tokens(self):
        self.assertIn("--mb-status-live:var(--mb-buy);", self.style)
        self.assertIn("--mb-status-down:var(--mb-sell);", self.style)


# ─────────────────────────────────────────────────────────────────────────
# D. P&L semantics — distinct token identity from BUY/SELL
# ─────────────────────────────────────────────────────────────────────────
class PnLSemanticsTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_positive_negative_tokens_defined_distinctly_from_buy_sell(self):
        self.assertIn("--mb-positive:var(--mb-buy);", self.style)
        self.assertIn("--mb-negative:var(--mb-sell);", self.style)

    def test_summary_pnl_classes_use_positive_negative_tokens(self):
        self.assertIn(".mob-summary .cell .value.pos{color:var(--mb-positive);}", self.style)
        self.assertIn(".mob-summary .cell .value.neg{color:var(--mb-negative);}", self.style)

    def test_row_card_pnl_classes_use_positive_negative_tokens(self):
        self.assertIn(".mob-row-card .rc-pnl.pos{color:var(--mb-positive);}", self.style)
        self.assertIn(".mob-row-card .rc-pnl.neg{color:var(--mb-negative);}", self.style)


# ─────────────────────────────────────────────────────────────────────────
# E. Typography foundation
# ─────────────────────────────────────────────────────────────────────────
class TypographyFoundationTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_body_uses_font_token(self):
        self.assertIn("font-family:var(--mb-font-sans);", self.style)

    def test_tabular_nums_applied_to_foundation(self):
        self.assertIn("font-variant-numeric:tabular-nums;", self.style)

    def test_no_raw_rem_font_size_literals_remain(self):
        # The old scale used 10 distinct ad-hoc rem sizes; this block
        # consolidates every font-size onto the token scale.
        self.assertNotIn("rem;", self.style)
        self.assertGreater(self.style.count("font-size:var(--mb-text-"), 10)


# ─────────────────────────────────────────────────────────────────────────
# F. Spacing / radius / elevation system
# ─────────────────────────────────────────────────────────────────────────
class SpacingRadiusElevationTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_foundation_padding_and_gap_use_spacing_tokens(self):
        self.assertIn(
            "padding:calc(var(--mb-space-4) + var(--mb-safe-top)) var(--mb-space-5) calc(var(--mb-space-4) + var(--mb-safe-bottom));",
            self.style,
        )
        self.assertIn("gap:var(--mb-space-5);", self.style)

    def test_cards_use_radius_and_elevation_tokens(self):
        self.assertIn("border-radius:var(--mb-radius-md)", self.style)
        self.assertIn("box-shadow:var(--mb-shadow-1)", self.style)

    def test_no_raw_px_radius_literals_remain(self):
        self.assertNotIn("border-radius:8px", self.style)
        self.assertNotIn("border-radius:6px", self.style)


# ─────────────────────────────────────────────────────────────────────────
# G. Touch-target foundation
# ─────────────────────────────────────────────────────────────────────────
class TouchTargetFoundationTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_primary_controls_use_touch_min(self):
        self.assertIn(".mob-status{display:flex;align-items:center;gap:var(--mb-space-2);font-size:var(--mb-text-sm);min-height:var(--mb-touch-min);}", self.style)
        self.assertIn("min-height:var(--mb-touch-min);box-sizing:border-box;box-shadow:var(--mb-shadow-1);}", self.style)  # .mob-summary .cell
        self.assertIn(".mob-ticket-buttons button{flex:1;min-height:var(--mb-touch-min);", self.style)

    def test_secondary_dense_controls_use_touch_sm(self):
        self.assertIn(".mob-tf-item{flex:1;min-height:var(--mb-touch-sm);", self.style)
        self.assertIn(".mob-pane-tab{flex:1;min-height:var(--mb-touch-sm);", self.style)
        self.assertIn(".mob-row-card button{min-height:var(--mb-touch-sm);", self.style)


# ─────────────────────────────────────────────────────────────────────────
# H. Focus-visible foundation
# ─────────────────────────────────────────────────────────────────────────
class FocusVisibleFoundationTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_focus_visible_rule_scoped_to_native_interactive_elements(self):
        self.assertIn(".mob-foundation button:focus-visible,", self.style)
        self.assertIn(".mob-foundation input:focus-visible{", self.style)
        self.assertIn("outline:2px solid var(--mb-focus);", self.style)
        self.assertIn("outline-offset:2px;", self.style)

    def test_focus_token_distinct_from_brand_buy_sell(self):
        # Accessible focus ring must not be visually confusable with
        # the brand accent or the buy/sell semantic colors.
        root = self.style[self.style.index(":root{") : self.style.index(":root{") + 2000]
        brand_val = re.search(r"--mb-brand:(#[0-9a-fA-F]{3,6});", root).group(1)
        buy_val = re.search(r"--mb-buy:(#[0-9a-fA-F]{3,6});", root).group(1)
        sell_val = re.search(r"--mb-sell:(#[0-9a-fA-F]{3,6});", root).group(1)
        focus_val = re.search(r"--mb-focus:(#[0-9a-fA-F]{3,6});", root).group(1)
        self.assertNotIn(focus_val, {brand_val, buy_val, sell_val})


# ─────────────────────────────────────────────────────────────────────────
# I. Safe-area preservation
# ─────────────────────────────────────────────────────────────────────────
class SafeAreaPreservationTests(SimpleTestCase):
    def setUp(self):
        self.style = _style_block(_mobile_html_source())

    def test_safe_area_env_calls_preserved_verbatim(self):
        # Same env() calls/defaults as every prior 03C.2.x sub-block —
        # only now named via tokens instead of inlined raw in
        # .mob-foundation's own padding rule.
        self.assertIn("env(safe-area-inset-top,0px)", self.style)
        self.assertIn("env(safe-area-inset-bottom,0px)", self.style)

    def test_foundation_padding_references_safe_area_tokens(self):
        self.assertIn("var(--mb-safe-top)", self.style)
        self.assertIn("var(--mb-safe-bottom)", self.style)


# ─────────────────────────────────────────────────────────────────────────
# J. Zero functional/WS/backend change — structural diff containment
# ─────────────────────────────────────────────────────────────────────────
class NoFunctionalChangeTests(SimpleTestCase):
    def setUp(self):
        self.old = _head_source()
        self.new = _mobile_html_source()

    def test_content_block_byte_identical_to_head(self):
        marker_start = "{% block content %}"
        marker_end = "{% endblock %}\n\n{% block extra_scripts %}"
        old_content = self.old[self.old.index(marker_start) : self.old.index(marker_end)]
        new_content = self.new[self.new.index(marker_start) : self.new.index(marker_end)]
        self.assertEqual(old_content, new_content)

    def test_extra_scripts_block_byte_identical_to_head(self):
        marker = "{% block extra_scripts %}"
        old_scripts = self.old[self.old.index(marker):]
        new_scripts = self.new[self.new.index(marker):]
        self.assertEqual(old_scripts, new_scripts)

    def test_only_style_block_differs(self):
        self.assertNotEqual(
            _style_block(self.old),
            _style_block(self.new),
            "03C.3.1 should have actually changed the style block",
        )

    def test_no_new_websocket_or_action_strings_introduced(self):
        for forbidden in [
            "new WebSocket(", "action:'order:", "action: 'order:",
            "order:pending:new", "order:pending:update", "order:update",
        ]:
            self.assertNotIn(forbidden, _style_block(self.new))


# ─────────────────────────────────────────────────────────────────────────
# K. End-to-end render smoke test (page still renders correctly)
# ─────────────────────────────────────────────────────────────────────────
class MobileRenderSmokeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="CHALLENGE")
        self.client.force_login(self.user)

    def test_mobile_page_still_renders_with_tokens(self):
        r = self.client.get(
            reverse("simulator:dashboard_account", args=[self.account.pk]),
            HTTP_USER_AGENT=IPHONE_UA,
        )
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("simulator/trade/mobile.html", {t.name for t in r.templates if t.name})
        self.assertIn("--mb-brand:", body)
        self.assertIn('id="mobConnStatus"', body)
        self.assertIn('id="mobBuyBtn"', body)
        self.assertIn('id="mobSellBtn"', body)
        self.assertIn('id="mobPositionsList"', body)


# ─────────────────────────────────────────────────────────────────────────
# L. Zero diff — Desktop/backend/financial engine/other Mobile files
# ─────────────────────────────────────────────────────────────────────────
class ProtectedFilesZeroDiffTests(SimpleTestCase):
    def _assert_zero_diff(self, path):
        result = subprocess.run(["git", "diff", "--quiet", "--", path])
        self.assertEqual(result.returncode, 0, f"{path} has a diff against HEAD, expected none")

    def test_mobile_session_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_session.js")

    def test_mobile_chart_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/mobile_chart.js")

    def test_trading_core_js_zero_diff(self):
        self._assert_zero_diff("simulator/static/simulator/trade/trading_core.js")

    def test_desktop_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/desktop.html")

    def test_shell_html_zero_diff(self):
        self._assert_zero_diff("simulator/templates/simulator/trade/shell.html")

    def test_views_py_zero_diff(self):
        self._assert_zero_diff("simulator/views.py")

    def test_consumers_py_zero_diff(self):
        self._assert_zero_diff("simulator/consumers.py")

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
