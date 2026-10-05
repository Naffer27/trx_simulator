# simulator/tests/test_pre_vps_polish_03b2c2a_price_tick_shared_state.py
"""
PRE-VPS-POLISH-03B.2C.2A — Price/tick shared quote-state seam.

Verifies that `applyPriceTickState()` (simulator/static/simulator/trade/
trading_core.js) is a pure, side-effect-free mirror of the FIX-05C
fail-closed gate that previously lived inline in TradingPanel._handleMsg()'s
price/tick branch (desktop.html), and that the branch itself still lives
in its original position in desktop.html, delegating only the
authoritative bid/ask/liveMid/liveSource/prevLiveMid calculation to the
shared function — zero behavior change, zero duplication.

Structural assertions use the same source-inspection convention already
established by test_fix05c_frontend_price_pnl_contract.py. Equivalence
assertions execute the real function via Node (same hybrid convention as
test_live_chart_smooth_interpolation_01.py / test_live_chart_magnitude_
filter_01.py) — skipped gracefully when `node` is not on PATH.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import get_template
from django.test import SimpleTestCase


def _template_source() -> str:
    path = get_template("simulator/trade/desktop.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _core_source() -> str:
    with open(
        "simulator/static/simulator/trade/trading_core.js", encoding="utf-8"
    ) as f:
        return f.read()


def _slice(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i + len(start_marker))
    return src[i:j]


def _tick_block(src):
    return _slice(
        src,
        "if(msg.type==='price'||msg.type==='tick'){",
        "if(msg.type==='history'&&Array.isArray(msg.data)){",
    )


NODE_AVAILABLE = shutil.which("node") is not None


# ─────────────────────────────────────────────────────────────────────────
# Structural — branch stays in place, delegates, no duplication
# ─────────────────────────────────────────────────────────────────────────
class BranchStaysInPlaceTests(SimpleTestCase):
    def test_price_tick_branch_still_adjacent_to_history_branch(self):
        # Same adjacency the pre-existing FIX-05C tests rely on — proves
        # the branch was NOT relocated, only its internals changed.
        src = _template_source()
        block = _tick_block(src)
        self.assertIn("msg.symbol&&msg.symbol!==this.currentSymbol", block)

    def test_branch_delegates_to_shared_function(self):
        block = _tick_block(_template_source())
        self.assertIn(
            "applyPriceTickState(this.liveMid,_b,_a,_src)", block
        )

    def test_branch_still_assigns_all_five_fields_from_result(self):
        block = _tick_block(_template_source())
        self.assertIn("this.prevLiveMid=_priceState.prevLiveMid", block)
        self.assertIn(
            "this.bid=_priceState.bid;this.ask=_priceState.ask;"
            "this.liveMid=_priceState.liveMid;"
            "this.liveSource=_priceState.liveSource;",
            block,
        )

    def test_branch_side_effects_remain_inline_unchanged(self):
        block = _tick_block(_template_source())
        self.assertIn("this._scheduleVisualRender('quote')", block)
        self.assertIn("quotesLivePx[_qs]={price:this.liveMid,", block)
        self.assertIn("renderQuotes(quotesSearch?.value||'')", block)

    def test_gate_logic_no_longer_duplicated_inline_in_desktop_html(self):
        # The raw gate expression (old variable names) must not remain
        # inline in desktop.html — it now lives once, in trading_core.js.
        block = _tick_block(_template_source())
        self.assertNotIn(
            "_src!=null&&_src!=='sim'&&_b!=null&&_a!=null&&_a>_b", block
        )

    def test_shared_function_defined_exactly_once_in_core(self):
        core = _core_source()
        self.assertEqual(
            core.count("function applyPriceTickState("), 1
        )


class SharedFunctionPurityTests(SimpleTestCase):
    """The shared function body itself must stay pure — no DOM, no chart
    API, no mutable globals, no WebSocket, no localStorage, no `this.`."""

    def _function_body(self):
        src = _core_source()
        start = src.index("function applyPriceTickState(")
        end = src.index("\n}", start)
        return src[start:end]

    def test_no_dom_access(self):
        body = self._function_body()
        self.assertNotIn("document.", body)
        self.assertNotIn("window.", body)

    def test_no_this_reference(self):
        self.assertNotIn("this.", self._function_body())

    def test_no_quotes_live_px_or_render_quotes(self):
        body = self._function_body()
        self.assertNotIn("quotesLivePx", body)
        self.assertNotIn("renderQuotes", body)

    def test_no_websocket_or_localstorage(self):
        body = self._function_body()
        self.assertNotIn("WebSocket", body)
        self.assertNotIn("localStorage", body)


# ─────────────────────────────────────────────────────────────────────────
# Equivalence — real execution via Node
# ─────────────────────────────────────────────────────────────────────────
class PriceTickEquivalenceTests(SimpleTestCase):
    def _run_node(self, script):
        core = _core_source()
        driver = f"{core}\n{script}\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run(
                ["node", str(path)], capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                self.fail(
                    f"node harness failed:\nSTDOUT:\n{result.stdout}\n"
                    f"STDERR:\n{result.stderr}"
                )
            return json.loads(result.stdout.strip().splitlines()[-1])

    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def test_valid_real_quote_massive(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 1.1000, 1.1002, 'massive')));"
        )
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["bid"], 1.1000, places=6)
        self.assertAlmostEqual(out["ask"], 1.1002, places=6)
        self.assertAlmostEqual(out["liveMid"], 1.1001, places=6)
        self.assertEqual(out["liveSource"], "massive")
        self.assertIsNone(out["prevLiveMid"])

    def test_source_sim_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 100, 102, 'sim')));"
        )
        self.assertIsNone(out)

    def test_missing_source_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 100, 102, null)));"
        )
        self.assertIsNone(out)

    def test_missing_bid_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, null, 102, 'massive')));"
        )
        self.assertIsNone(out)

    def test_missing_ask_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 100, null, 'massive')));"
        )
        self.assertIsNone(out)

    def test_ask_equal_bid_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 100, 100, 'massive')));"
        )
        self.assertIsNone(out)

    def test_ask_less_than_bid_fails_closed(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(null, 102, 100, 'massive')));"
        )
        self.assertIsNone(out)

    def test_valid_tick_carries_forward_previous_live_mid(self):
        out = self._run_node(
            "console.log(JSON.stringify("
            "applyPriceTickState(1.0999, 100, 102, 'massive')));"
        )
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["prevLiveMid"], 1.0999, places=6)
        self.assertAlmostEqual(out["liveMid"], 101, places=6)

    def test_provider_agnostic_real_sources_all_accepted(self):
        # FIX-05C's own comment: "no rigid provider whitelist — any
        # non-null, non-'sim' source qualifies." Confirmed real sources
        # from market_data/feeds.py's _DURABLE_PRICE_VALID_SOURCES plus
        # one arbitrary non-whitelisted-but-real-looking source, proving
        # the gate stays provider-agnostic exactly as before.
        for source in ("massive", "finnhub", "binance", "kraken", "unknown"):
            out = self._run_node(
                "console.log(JSON.stringify("
                f"applyPriceTickState(null, 100, 102, '{source}')));"
            )
            self.assertIsNotNone(out, f"source={source!r} should pass the gate")
            self.assertEqual(out["liveSource"], source)
