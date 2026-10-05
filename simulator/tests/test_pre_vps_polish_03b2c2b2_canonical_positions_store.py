# simulator/tests/test_pre_vps_polish_03b2c2b2_canonical_positions_store.py
"""
PRE-VPS-POLISH-03B.2C.2B.2 — Canonical positions store.

Verifies that `positionsCache` is now a thin, getter-only compatibility
view (TradingPanel.prototype) over a single canonical positions array
(simulator/static/simulator/trade/trading_core.js's getCanonicalPositions
/replaceCanonicalPositions), replacing the old per-panel copy + manual
"cache broadcast" to allPanels — while the CHART render broadcast
(_renderLines/_removeLines across allPanels) stays exactly as before,
since each panel's chart lines remain independent presentation state.

Structural assertions use the same source-inspection convention already
established by test_fix05c_frontend_price_pnl_contract.py /
test_pre_vps_polish_03b2c2a_price_tick_shared_state.py. Real-execution
assertions run the actual shared-store code (trading_core.js) plus the
actual Object.defineProperty block sliced verbatim out of desktop.html
via Node — no reimplementation, no mock of the mechanism under test.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


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


NODE_AVAILABLE = shutil.which("node") is not None


# ─────────────────────────────────────────────────────────────────────────
# 6/7 — constructor no longer copies; positions branch writes through
# ─────────────────────────────────────────────────────────────────────────
class ConstructorAndPositionsBranchTests(SimpleTestCase):
    def test_constructor_no_longer_initializes_positions_cache(self):
        src = _template_source()
        self.assertNotIn(
            "this.positionsCache=[]; this.linesById=new Map();", src
        )
        self.assertIn(
            "this.linesById=new Map(); this.selectedPosIds=new Set();", src
        )

    def _positions_branch(self):
        src = _template_source()
        return _slice(
            src,
            "if(msg.type==='positions'){",
            "if(msg.type==='pending_orders'){",
        )

    def test_positions_branch_uses_replace_canonical_positions(self):
        block = self._positions_branch()
        self.assertIn("replaceCanonicalPositions(_freshItems);", block)

    def test_positions_branch_guard_preserved(self):
        block = self._positions_branch()
        self.assertIn("if(Array.isArray(_freshItems)){", block)

    def test_positions_branch_cache_broadcast_removed(self):
        block = self._positions_branch()
        self.assertNotIn("p.positionsCache=_freshItems", block)

    def test_positions_branch_chart_render_broadcast_preserved(self):
        block = self._positions_branch()
        self.assertIn(
            "for(const p of allPanels){if(p===this)continue;"
            "p._renderLines(p.positionsCache);}",
            block,
        )


# ─────────────────────────────────────────────────────────────────────────
# 10/11 — order_close compatibility branch + chart-removal behavior
# ─────────────────────────────────────────────────────────────────────────
class OrderCloseBranchTests(SimpleTestCase):
    def _order_close_branch(self):
        src = _template_source()
        return _slice(
            src,
            "const cid=(msg.id!=null&&!msg.partial)?String(msg.id):null;",
            "_applyIndicators(){",
        )

    def test_dead_items_branch_preserved(self):
        block = self._order_close_branch()
        self.assertIn("if(msg.items){", block)

    def test_order_close_uses_replace_canonical_positions(self):
        block = self._order_close_branch()
        self.assertIn("replaceCanonicalPositions(msg.items);", block)

    def test_order_close_cache_broadcast_removed(self):
        block = self._order_close_branch()
        self.assertNotIn("p.positionsCache=msg.items", block)

    def test_order_close_chart_render_broadcast_preserved(self):
        block = self._order_close_branch()
        self.assertIn(
            "for(const p of allPanels){if(p===this)continue;"
            "p._renderLines(msg.items);}",
            block,
        )

    def test_remove_lines_broadcast_preserved(self):
        # Full-close chart-line/selection cleanup broadcast — untouched by
        # this block, independently protected by
        # test_golden_fix01_history_dedup_trade_precision.py.
        block = self._order_close_branch()
        self.assertIn(
            "for(const p of allPanels){if(p===this)continue;"
            "p._removeLines(cid);p.selectedPosIds.delete(cid);"
            "if(p.lastSelectedId===cid)p.lastSelectedId=null;}",
            block,
        )


# ─────────────────────────────────────────────────────────────────────────
# Getter definition
# ─────────────────────────────────────────────────────────────────────────
class CompatibilityGetterTests(SimpleTestCase):
    def _getter_block(self):
        src = _template_source()
        return _slice(src, "Object.defineProperty(", "  }\n);")

    def test_getter_targets_trading_panel_prototype(self):
        block = self._getter_block()
        self.assertIn("TradingPanel.prototype", block)
        self.assertIn("'positionsCache'", block)

    def test_getter_reads_canonical_store(self):
        block = self._getter_block()
        self.assertIn("return getCanonicalPositions();", block)

    def test_no_setter_defined(self):
        block = self._getter_block()
        self.assertNotIn("set(", block)

    def test_configurable_true(self):
        block = self._getter_block()
        self.assertIn("configurable:true", block)


# ─────────────────────────────────────────────────────────────────────────
# Canonical store purity (same discipline as applyPriceTickState)
# ─────────────────────────────────────────────────────────────────────────
class CanonicalStorePurityTests(SimpleTestCase):
    def _store_block(self):
        core = _core_source()
        return _slice(
            core,
            "let _canonicalPositions",
            "function replaceCanonicalPositions(items){ _canonicalPositions = Array.isArray(items) ? items : []; }",
        )

    def test_store_defined_in_core(self):
        core = _core_source()
        self.assertIn("function getCanonicalPositions(){", core)
        self.assertIn("function replaceCanonicalPositions(items){", core)

    def test_store_has_no_pnl_logic(self):
        block = self._store_block()
        self.assertNotIn("computePositionPnLSafe", block)
        self.assertNotIn("computeRawPnL", block)
        self.assertNotIn("pnl", block)

    def test_store_has_no_chart_or_dom_or_ws(self):
        block = self._store_block()
        self.assertNotIn("document.", block)
        self.assertNotIn("WebSocket", block)
        self.assertNotIn("candleSeries", block)


# ─────────────────────────────────────────────────────────────────────────
# 12/13/14/15 — lifecycle methods never touch the canonical store
# ─────────────────────────────────────────────────────────────────────────
class LifecycleUntouchedTests(SimpleTestCase):
    def test_sym_change_does_not_touch_positions(self):
        src = _template_source()
        body = _slice(src, "_onSymChange(){", "_onTFChange(){")
        self.assertNotIn("replaceCanonicalPositions", body)
        self.assertNotIn("positionsCache", body)

    def test_tf_change_does_not_touch_positions(self):
        src = _template_source()
        body = _slice(src, "_onTFChange(){", "setStatus(t){")
        self.assertNotIn("replaceCanonicalPositions", body)
        self.assertNotIn("positionsCache", body)

    def test_onclose_does_not_touch_positions(self):
        src = _template_source()
        body = _slice(src, "this.ws.onclose=()=>{", "disconnect(){")
        self.assertNotIn("replaceCanonicalPositions", body)
        self.assertNotIn("positionsCache", body)

    def test_sltp_drag_does_not_write_canonical_positions(self):
        src = _template_source()
        body = _slice(
            src,
            "this.chartEl.addEventListener('pointermove',e=>{",
            "this.chartEl.addEventListener('pointerup',endDrag);",
        )
        self.assertNotIn("replaceCanonicalPositions", body)
        self.assertNotIn("positionsCache=", body)
        # The real, confined optimistic mutation: chart-local linesById only.
        self.assertIn("rec.sl=price", body)
        self.assertIn("rec.tp=price", body)


# ─────────────────────────────────────────────────────────────────────────
# Real execution — the actual store + the actual getter mechanism
# ─────────────────────────────────────────────────────────────────────────
class CanonicalStoreRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not NODE_AVAILABLE:
            self.skipTest("node not available on PATH")

    def _getter_block(self):
        return _slice(
            _template_source(), "Object.defineProperty(", "  }\n);"
        ) + "  }\n);"

    def _run_node(self, script):
        core = _core_source()
        getter = self._getter_block()
        driver = (
            "class TradingPanel { constructor(){} }\n"
            f"{core}\n{getter}\n{script}\n"
        )
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

    def test_canonical_state_starts_empty(self):
        out = self._run_node(
            "console.log(JSON.stringify(getCanonicalPositions()));"
        )
        self.assertEqual(out, [])

    def test_replace_sets_canonical_state(self):
        out = self._run_node(
            "replaceCanonicalPositions([{id:1},{id:2}]);"
            "console.log(JSON.stringify(getCanonicalPositions()));"
        )
        self.assertEqual(out, [{"id": 1}, {"id": 2}])

    def test_replace_invalid_input_yields_empty_array(self):
        out = self._run_node(
            "replaceCanonicalPositions(undefined);"
            "console.log(JSON.stringify(getCanonicalPositions()));"
        )
        self.assertEqual(out, [])

    def test_two_panels_read_same_canonical_reference(self):
        out = self._run_node(
            "const p1=new TradingPanel();const p2=new TradingPanel();"
            "replaceCanonicalPositions([{id:1}]);"
            "console.log(JSON.stringify({same:p1.positionsCache===p2.positionsCache}));"
        )
        self.assertTrue(out["same"])

    def test_no_own_property_positions_cache_per_instance(self):
        out = self._run_node(
            "const p1=new TradingPanel();"
            "console.log(JSON.stringify({ownProps:Object.getOwnPropertyNames(p1)}));"
        )
        self.assertNotIn("positionsCache", out["ownProps"])

    def test_direct_assignment_throws_under_strict_mode(self):
        out = self._run_node(
            "class Caller { attempt(p){ 'use strict'; p.positionsCache=[{id:99}]; } }\n"
            "const p1=new TradingPanel();let threw=false;let message='';\n"
            "try{ new Caller().attempt(p1); }catch(e){ threw=true; message=e.constructor.name; }\n"
            "console.log(JSON.stringify({threw,message}));"
        )
        self.assertTrue(out["threw"])
        self.assertEqual(out["message"], "TypeError")

    def test_new_panel_immediately_sees_existing_canonical_positions(self):
        out = self._run_node(
            "replaceCanonicalPositions([{id:7}]);"
            "const freshPanel=new TradingPanel();"
            "console.log(JSON.stringify(freshPanel.positionsCache));"
        )
        self.assertEqual(out, [{"id": 7}])


# ─────────────────────────────────────────────────────────────────────────
# 18 — account-type equivalence (same wiring regardless of account_type)
# ─────────────────────────────────────────────────────────────────────────
class AccountTypeWiringTests(TestCase):
    def _html_for(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(
            reverse("simulator:dashboard_account", args=[account.pk])
        )
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _assert_getter_present(self, account_type):
        html = self._html_for(account_type)
        self.assertIn("Object.defineProperty(", html)
        self.assertIn("TradingPanel.prototype", html)
        self.assertNotIn("this.positionsCache=[]; this.linesById=new Map();", html)

    def test_standard_account_wiring(self):
        self._assert_getter_present("STANDARD")

    def test_demo_account_wiring(self):
        self._assert_getter_present("DEMO")

    def test_challenge_account_wiring(self):
        self._assert_getter_present("CHALLENGE")

    def test_funded_account_wiring(self):
        self._assert_getter_present("FUNDED")
