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


def _old_main_script(head_source):
    # The one pre-03C.3.2 orchestrator <script> (account/quote/chart/
    # ticket/positions/pending/closed wiring) — unique anchor, since
    # the only other <script> blocks in extra_scripts are single-line
    # src="..."/config tags, never starting with "(function(){".
    start = head_source.index("<script>\n(function(){")
    end = head_source.index("\n})();\n</script>", start) + len("\n})();\n</script>")
    return head_source[start:end]


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

    # PRE-VPS-POLISH-03C.3.2A — OLD CONTRACT: 03C.3.1's `.mob-foundation`
    # owned BOTH top and bottom safe-area padding directly (it was the
    # single padded content column). NEW CONTRACT (03C.3.2, authorized):
    # `.mob-foundation` became the app-shell FRAME (100dvh, overflow
    # hidden, flex column, no padding/gap of its own at all) and safe-
    # area ownership moved to the two real edge regions of that frame —
    # `.mob-topbar` (top) and `.mob-bottomnav` (bottom) — while
    # `.mob-view-area` (the scrollable middle region) deliberately does
    # NOT consume `--mb-safe-bottom` itself, since the bottom nav below
    # it already reserves that space. WHY preserved: still fails if any
    # of the 5 shell regions stops using the 03C.3.1 token system, or if
    # the scrollable area starts double-reserving the bottom safe area.
    def _rule_body(self, selector):
        start = self.style.index(selector)
        end = self.style.index("}", start) + 1
        return self.style[start:end]

    def test_foundation_is_a_bare_frame_with_no_own_padding_or_safe_area(self):
        body = self._rule_body(".mob-foundation{")
        self.assertIn("height:100dvh;", body)
        self.assertIn("overflow:hidden;", body)
        self.assertIn("display:flex;flex-direction:column;", body)
        self.assertNotIn("padding", body)
        self.assertNotIn("--mb-safe-", body)

    def test_topbar_owns_safe_area_top_via_tokens(self):
        body = self._rule_body(".mob-topbar{")
        self.assertIn("var(--mb-safe-top)", body)
        self.assertIn("var(--mb-space-", body)

    def test_view_area_scrolls_and_does_not_absorb_safe_bottom(self):
        body = self._rule_body(".mob-view-area{")
        self.assertIn("overflow-y:auto;", body)
        self.assertIn("var(--mb-space-", body)
        self.assertNotIn("--mb-safe-bottom", body)

    def test_view_preserves_gap_via_spacing_token(self):
        body = self._rule_body(".mob-view{")
        self.assertIn("gap:var(--mb-space-5);", body)

    def test_bottomnav_owns_safe_area_bottom_via_tokens(self):
        body = self._rule_body(".mob-bottomnav{")
        self.assertIn("var(--mb-safe-bottom)", body)
        navitem_body = self._rule_body(".mob-navitem{")
        self.assertIn("var(--mb-touch-min)", navitem_body)

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

    # PRE-VPS-POLISH-03C.3.2A — OLD CONTRACT: the `content` block had
    # to be byte-identical to HEAD, because 03C.3.1 was CSS-only. NEW
    # CONTRACT (03C.3.2, authorized): `content` was reorganized into a
    # real app shell — same existing ids/classes/data-attributes, now
    # nested under 5 named `.mob-view` containers plus a topbar/
    # bottomnav. WHY preserved: the test's real intent — "the redesign
    # did not destroy the functional structure" — survives as a
    # semantic, structural check instead of a literal diff: the shell
    # regions exist, exactly 5 primary views exist with the right
    # names, exactly one (trade) starts visible, the other 4 start
    # hidden, every pre-existing id important to 03C.2.1-03C.2.6 is
    # still present exactly once (no silent removal, no accidental
    # duplication).
    def test_content_block_preserves_shell_structure_and_existing_ids(self):
        html = self.new
        self.assertIn('<div class="mob-foundation">', html)
        self.assertIn('<div class="mob-topbar">', html)
        self.assertIn('<div class="mob-view-area">', html)
        self.assertIn('<div class="mob-bottomnav" id="mobBottomNav">', html)

        views = re.findall(r'<div class="mob-view" data-mobile-view="(\w+)"( hidden)?>', html)
        self.assertEqual(len(views), 5)
        self.assertEqual({name for name, _ in views}, {"trade", "markets", "positions", "history", "account"})
        visible = {name for name, hidden_attr in views if not hidden_attr}
        hidden = {name for name, hidden_attr in views if hidden_attr}
        self.assertEqual(visible, {"trade"})
        self.assertEqual(hidden, {"markets", "positions", "history", "account"})

        important_ids = [
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
        for el_id in important_ids:
            self.assertEqual(html.count(f'id="{el_id}"'), 1, el_id)

    # PRE-VPS-POLISH-03C.3.2A — OLD CONTRACT: `extra_scripts` had to be
    # byte-identical to HEAD. NEW CONTRACT (03C.3.2, authorized): the
    # pre-existing orchestrator <script> (account/quote/chart/ticket/
    # positions/pending/closed wiring) is preserved byte-for-byte as a
    # contiguous substring (proven below, not merely "present"), and
    # exactly one new, additive <script> was appended after it — the
    # 03C.3.2 navigation controller. WHY preserved: still fails if the
    # orchestrator script is rewritten at all, if more than one script
    # is appended, or if the new script does anything beyond toggling
    # [hidden]/`.is-active` — proving it is presentation-only, never a
    # second transport/financial layer.
    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: the entire
    # orchestrator <script> had to be byte-identical to HEAD (03C.3.2A's
    # own contract). NEW CONTRACT: TF-01 (a later, separately-
    # authorized block) legitimately rewrote exactly the
    # MOBILE_TIMEFRAMES/renderTfSelector section inside that same
    # script (public catalog narrowed to 5 entries + display-label
    # split) — everything BEFORE and AFTER that section must still be
    # byte-identical to HEAD, and the 03C.3.2 navigation controller
    # must still be the one and only script appended after the
    # orchestrator, still presentation-only. WHY preserved: still
    # fails if the orchestrator is rewritten anywhere outside the
    # TF-01-authorized section, if a second script is appended, or if
    # the navigation controller stops being presentation-only.
    def test_extra_scripts_orchestrator_preserved_outside_tf01_section_new_code_is_navigation_only(self):
        PREFIX_END = "function onVolumeUpdate(point){ mobileChart.updateVolume(point); }"
        SUFFIX_START = "// PRE-VPS-POLISH-03C.2.5 — order ticket foundation."

        old_main_script = _old_main_script(self.old)
        old_prefix = old_main_script[: old_main_script.index(PREFIX_END) + len(PREFIX_END)]
        old_suffix = old_main_script[old_main_script.index(SUFFIX_START):]

        new_main_script_start = self.new.index("<script>\n(function(){")
        new_main_script_end = self.new.index("\n})();\n</script>", new_main_script_start) + len("\n})();\n</script>")
        new_main_script = self.new[new_main_script_start:new_main_script_end]
        new_prefix_end = new_main_script.index(PREFIX_END) + len(PREFIX_END)
        new_suffix_start = new_main_script.index(SUFFIX_START)
        new_prefix = new_main_script[:new_prefix_end]
        new_suffix = new_main_script[new_suffix_start:]

        self.assertEqual(old_prefix, new_prefix)
        self.assertEqual(old_suffix, new_suffix)

        # PRE-VPS-POLISH-03C.3.TF-02A — OLD CONTRACT: the "middle"
        # (TF-01-authorized) section had to contain the literal
        # 5-entry catalog. NEW CONTRACT: TF-02 (separately authorized)
        # grew that SAME section to the real 6-entry catalog (added
        # "4h") — still inside the same prefix/suffix boundaries, so
        # the byte-identical-outside-this-section guarantee above is
        # unaffected. WHY preserved: still fails if "1s" ever
        # reappears, or if the real 6-entry catalog/labels drift.
        middle = new_main_script[new_prefix_end:new_suffix_start]
        self.assertIn("const MOBILE_TIMEFRAMES = ['1m','5m','15m','1h','4h','1d'];", middle)
        self.assertIn("MOBILE_TF_LABELS", middle)
        self.assertIn("'4h':'4H'", middle)
        self.assertNotIn("'1s'", middle)

        tail = self.new[self.new.index(new_main_script) + len(new_main_script):]
        self.assertEqual(tail.count("<script>"), 1)
        self.assertIn("PRE-VPS-POLISH-03C.3.2", tail)
        self.assertIn("querySelectorAll('.mob-view')", tail)
        self.assertIn("querySelectorAll('.mob-navitem')", tail)
        self.assertIn("v.hidden = (v.getAttribute('data-mobile-view') !== name);", tail)
        self.assertIn("b.classList.toggle('is-active', b.getAttribute('data-target') === name);", tail)
        self.assertIn("showView('trade');", tail)
        for forbidden in [
            "new WebSocket(", ".send(", "order:new", "order:close",
            "order:risk_preview", "order:pending", "load_history",
            "change_symbol", "change_timeframe", "get_closed_trades",
            "margin", "commission", "pnl", "spread", "price",
            "account:update", "account:snapshot", "onAccount(",
        ]:
            self.assertNotIn(forbidden, tail)

    # PRE-VPS-POLISH-03C.3.TF-01A — OLD CONTRACT: HEAD's style block
    # had to differ from the working tree's (true only in 03C.3.1's
    # own commit window, before its style changes were committed to
    # HEAD). NEW CONTRACT: HEAD now already includes every style
    # change through 03C.3.3, and TF-01 is JS/HTML-catalog-only — the
    # correct, stronger assertion today is that the style block is
    # BYTE-IDENTICAL to HEAD (proving TF-01 really never touched CSS).
    def test_style_block_untouched_by_tf01(self):
        self.assertEqual(_style_block(self.old), _style_block(self.new))

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
