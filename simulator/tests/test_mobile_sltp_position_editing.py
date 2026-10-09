# simulator/tests/test_mobile_sltp_position_editing.py
"""
MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP-on-open-position editing
(audited-then-authorized sub-block).

Verifies the drag/tap/confirm/cancel/resolve interaction added to
simulator/static/simulator/trade/mobile_chart.js (pure presentation —
no network) and the orchestration added to
simulator/templates/simulator/trade/mobile.html + the minimal
updateSlTp()/clearSlTpPending() addition to
simulator/static/simulator/trade/mobile_session.js (the real
order:update contract — consumers.py::_order_update, the exact same
one Desktop's own chart drag-lines/sheet editor already use).

Real-execution tests run the ACTUAL, unmodified source files in a real
headless browser (Chrome), using a deterministic FakeLWC in place of
the real Lightweight Charts library — the same "mock the external
rendering library, never project logic" discipline the Node-based
real-execution tests elsewhere in this project already use (see
test_pre_vps_polish_03c2_4_mobile_chart.py's own FAKE_LWC_HARNESS) —
and a FakeWebSocket (same pattern this project already uses in its
Node suites) in place of a live connection. No network, no real
account, no server started by these tests. Skipped gracefully when no
Chrome-family browser is found on this machine.

Covers, at minimum (per the Owner's explicit test list for this
sub-block): modify SL preserving TP; modify TP preserving SL; Cancel ->
zero sends; Confirm -> exactly one send; backend rejection -> visual
rollback; a snapshot for a different position never confirms; a
snapshot for the same position with different values never confirms;
a matching snapshot confirms; disconnection before send; timeout after
send; the position disappearing mid-edit; a touch/pointer drag that
never also pans/zooms the chart; switching the selected position never
leaks a provisional edit from one position to another.

Explicit removal of an existing SL/TP via null is NOT covered here:
this sub-block's UI never offers an explicit "remove" affordance (only
"+ Añadir SL/TP" for a level that doesn't exist yet) — per its own
authorization, "NO asignar automáticamente ningún nivel" and no
invented behavior beyond what was actually built.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

MOBILE_CHART_JS_PATH = "simulator/static/simulator/trade/mobile_chart.js"
MOBILE_SESSION_JS_PATH = "simulator/static/simulator/trade/mobile_session.js"
MOBILE_HTML_PATH = "simulator/templates/simulator/trade/mobile.html"


def _chart_source() -> str:
    with open(MOBILE_CHART_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _session_source() -> str:
    with open(MOBILE_SESSION_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _CHROME_CANDIDATES():
    return [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
    ]


def _find_chrome():
    for c in _CHROME_CANDIDATES():
        if c and Path(c).exists():
            return c
    return None


CHROME_PATH = _find_chrome()

# Deterministic, dependency-free stand-in for the real Lightweight
# Charts library — same mocking discipline as this project's own
# FAKE_LWC_HARNESS (Node tests), extended with linear
# priceToCoordinate()/coordinateToPrice() (real inverse functions,
# simple deterministic math) and createPriceLine()/removePriceLine()
# so the REAL mobile_chart.js drag/tap code (which calls these exact
# APIs) can be exercised meaningfully without a 150KB+ third-party
# bundle or any network access during tests.
FAKE_LWC = """
class FakeSeries {
  constructor(){ this.data=[]; this.lines=[]; this._priceFormat=null; this._range=[0,2]; this._h=300; }
  setData(d){ this.data=Array.isArray(d)?d:[]; this._recalcRange(); }
  update(b){ this.data.push(b); this._recalcRange(); }
  applyOptions(o){ if(o && o.priceFormat) this._priceFormat=o.priceFormat; }
  _recalcRange(){
    let lo=Infinity, hi=-Infinity;
    this.data.forEach(function(b){
      if(b.low!=null)lo=Math.min(lo,b.low);
      if(b.high!=null)hi=Math.max(hi,b.high);
      if(b.value!=null){lo=Math.min(lo,b.value);hi=Math.max(hi,b.value);}
    });
    if(!isFinite(lo)||!isFinite(hi)||lo===hi){ lo=0; hi=2; }
    this._range=[lo,hi];
  }
  priceToCoordinate(price){
    if(price==null||!isFinite(price))return null;
    const lo=this._range[0], hi=this._range[1];
    return Math.round(this._h - ((price-lo)/(hi-lo))*this._h);
  }
  coordinateToPrice(y){
    const lo=this._range[0], hi=this._range[1];
    return lo + ((this._h-y)/this._h)*(hi-lo);
  }
  createPriceLine(opts){ const line=Object.assign({},opts); this.lines.push(line); return line; }
  removePriceLine(line){ this.lines=this.lines.filter(function(l){return l!==line;}); }
}
class FakeChart {
  constructor(){
    this.candle=new FakeSeries(); this.volume=new FakeSeries(); this._scales={};
    this._opts=null; this.handleScroll=true; this.handleScale=true;
  }
  addCandlestickSeries(){ return this.candle; }
  addHistogramSeries(){ return this.volume; }
  priceScale(id){ if(!this._scales[id]) this._scales[id]=new FakeSeries(); return this._scales[id]; }
  timeScale(){ return { fitContent(){} }; }
  applyOptions(o){
    this._opts=o;
    if(o){ if('handleScroll' in o) this.handleScroll=o.handleScroll; if('handleScale' in o) this.handleScale=o.handleScale; }
  }
}
window.LightweightCharts = { createChart: function(){ return new FakeChart(); } };
"""


def _run_chrome_dump(html_path, virtual_time_budget=6000, timeout=30):
    result = subprocess.run(
        [
            CHROME_PATH, "--headless", "--disable-gpu", "--hide-scrollbars",
            "--virtual-time-budget=" + str(virtual_time_budget),
            "--dump-dom", "file://" + str(html_path),
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout


def _extract_json(dom_html, element_id):
    m = re.search(r'<(?:pre|div) id="' + re.escape(element_id) + r'"[^>]*>(.*?)</(?:pre|div)>', dom_html, re.S)
    if not m:
        return None
    text = m.group(1)
    # Chrome's DOM dump HTML-escapes the pre's text content.
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    try:
        return json.loads(text)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────
# A. Chart-level interaction — pure presentation, no network. Exercises
#    the REAL mobile_chart.js drag/tap/confirm/cancel/resolve state
#    machine via real browser PointerEvent dispatch against a real
#    (fake-LWC-backed) chart instance and synthetic fixture positions.
# ─────────────────────────────────────────────────────────────────────────
class ChartInteractionRealExecutionTests(SimpleTestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _run(self):
        chart_src = _chart_source()
        core_src = _session_source()  # not used directly; trading_core loaded separately below
        with open("simulator/static/simulator/trade/trading_core.js", encoding="utf-8") as f:
            trading_core_src = f.read()
        driver = r"""
(async function(){
  const results = [];
  function assert(cond, label){ results.push({label, pass: !!cond}); }
  function assertEqual(a, b, label){ results.push({label, pass: a === b, got: a, want: b}); }

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

  const container = document.getElementById('mobChartContainer');
  const chart = new MobileTradingChart(container);
  await chart.initialize();
  chart.setSymbolFormat('EUR/USD');
  const tfSec = 3600;
  const now = Math.floor(Date.now()/1000);
  const startTime = Math.floor(now/tfSec)*tfSec - 60*tfSec;
  const bars = fixtureBars(60, startTime, tfSec);
  chart.applyHistory(bars, 'complete', 'EUR/USD');
  const lastClose = bars[bars.length-1].close;

  function makePos(overrides){
    return Object.assign({
      id: 'P1', symbol: 'EUR/USD', side: 'buy',
      avg: lastClose - 0.0015, sl: lastClose - 0.0045, tp: lastClose + 0.0025,
      qty: 0.02, pnl: 1.23,
    }, overrides || {});
  }

  function firePointer(el, type, opts){
    const ev = new PointerEvent(type, Object.assign({ bubbles: true, cancelable: true, pointerId: 1, clientY: 100 }, opts));
    el.dispatchEvent(ev);
  }

  // S1 — drag SL -> preview -> Cancel -> zero sends, revert.
  {
    let confirmCalls = 0;
    chart.onSlTpConfirm = () => { confirmCalls++; };
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    assert(!!slEl, 'S1: sl pill rendered');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointermove', { clientY: 250 });
    firePointer(slEl, 'pointerup', { clientY: 250 });
    const cancelBtn = container.querySelector('.mbgc-edit-cancel');
    assert(!!cancelBtn, 'S1: cancel button rendered after drag release');
    if (cancelBtn) cancelBtn.click();
    assertEqual(confirmCalls, 0, 'S1: zero onSlTpConfirm calls after Cancel');
    const slElAfter = container.querySelector('.mbgc-pill-sl');
    assert(slElAfter && slElAfter.textContent.indexOf(chart._fmtPx(pos.sl)) !== -1, 'S1: pill reverted to confirmed sl value');
  }

  // S2 — drag TP -> preview -> Confirm -> exactly one send.
  {
    const calls = [];
    chart.onSlTpConfirm = (id, field, price) => { calls.push({ id, field, price }); };
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const tpEl = container.querySelector('.mbgc-pill-tp');
    firePointer(tpEl, 'pointerdown', { clientY: 200 });
    firePointer(tpEl, 'pointermove', { clientY: 150 });
    firePointer(tpEl, 'pointerup', { clientY: 150 });
    const confirmBtn = container.querySelector('.mbgc-edit-confirm');
    if (confirmBtn) confirmBtn.click();
    assertEqual(calls.length, 1, 'S2: exactly one onSlTpConfirm call');
    assertEqual(calls[0] && calls[0].field, 'tp', 'S2: confirmed field is tp');
    assert(!container.querySelector('.mbgc-edit-box'), 'S2: edit toolbar removed after Confirm (locked)');
    chart.resolveSlTpEdit('tp');
  }

  // S3 — tap (no movement) opens the exact-price form pre-filled.
  {
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointerup', { clientY: 300 });
    const input = container.querySelector('.mbgc-edit-input');
    assert(!!input, 'S3: exact-price input opened on tap');
    assertEqual(input && input.value, chart._fmtPx(pos.sl), 'S3: input pre-filled with confirmed sl');
    const cancelBtn = container.querySelector('.mbgc-edit-cancel');
    if (cancelBtn) cancelBtn.click();
  }

  // S4 — tap-form Confirm with a typed exact price sends once.
  {
    const calls = [];
    chart.onSlTpConfirm = (id, field, price) => { calls.push({ id, field, price }); };
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointerup', { clientY: 300 });
    const input = container.querySelector('.mbgc-edit-input');
    const typed = pos.sl - 0.0005;
    if (input) input.value = String(typed);
    const confirmBtn = container.querySelector('.mbgc-edit-confirm');
    if (confirmBtn) confirmBtn.click();
    assertEqual(calls.length, 1, 'S4: exactly one send after typed Confirm');
    assert(calls[0] && Math.abs(calls[0].price - typed) < 1e-9, 'S4: sent price matches typed value exactly');
    chart.resolveSlTpEdit('sl');
  }

  // S5 — position without SL shows "+ SL"; tapping it enters edit mode
  // DIRECTLY: a draggable, visually-distinct PROVISIONAL pill (never
  // drawn as if it were an already-saved level) + an editable price
  // input pre-filled with a pure coordinate-derived starting point
  // (never a financial calculation, and never sent anywhere on its
  // own — only Confirm can ever lead to a send). Cancel removes it
  // entirely, reverting to "+ SL" (never a phantom saved level).
  {
    let confirmCalls = 0;
    chart.onSlTpConfirm = () => { confirmCalls++; };
    const pos = makePos({ sl: null });
    chart.setPositions([pos], pos.id);
    const addBtn = container.querySelector('.mbgc-add-sl');
    assert(!!addBtn, 'S5: + SL affordance rendered when sl is null');
    if (addBtn) addBtn.click();
    const provisionalPill = container.querySelector('.mbgc-pill-sl');
    assert(!!provisionalPill, 'S5: a draggable provisional SL pill appears immediately on "+ Añadir"');
    assert(provisionalPill && provisionalPill.className.indexOf('mbgc-pill-provisional') !== -1, 'S5: provisional pill is visually distinct from a confirmed level');
    assert(provisionalPill && provisionalPill.className.indexOf('mbgc-pill-editing') === -1, 'S5: provisional (new) is never styled as "editing an existing" level');
    const input = container.querySelector('.mbgc-edit-input');
    assert(!!input, 'S5: exact-price input shown immediately alongside the provisional pill');
    assert(input && input.value !== '', 'S5: input starts pre-filled with a sensible coordinate-derived default (never empty-forcing-guesswork)');
    assertEqual(confirmCalls, 0, 'S5: showing the provisional affordance + default never sends anything by itself');
    // Re-drag the provisional pill — must still work (not just the
    // initial placement).
    firePointer(provisionalPill, 'pointerdown', { clientY: 300 });
    firePointer(provisionalPill, 'pointermove', { clientY: 260 });
    firePointer(provisionalPill, 'pointerup', { clientY: 260 });
    assertEqual(confirmCalls, 0, 'S5: re-dragging the still-unconfirmed provisional pill sends nothing');
    const cancelBtn = container.querySelector('.mbgc-edit-cancel');
    if (cancelBtn) cancelBtn.click();
    assert(!container.querySelector('.mbgc-pill-sl'), 'S5: Cancel removes the provisional pill entirely (no phantom saved level)');
    assert(!!container.querySelector('.mbgc-add-sl'), 'S5: Cancel reverts to "+ Añadir SL"');
  }

  // S6 — same for TP, plus: typing in the input live-updates the pill.
  {
    const pos = makePos({ tp: null });
    chart.setPositions([pos], pos.id);
    const addBtn = container.querySelector('.mbgc-add-tp');
    assert(!!addBtn, 'S6: + TP affordance rendered when tp is null');
    if (addBtn) addBtn.click();
    const input = container.querySelector('.mbgc-edit-input');
    const before = container.querySelector('.mbgc-pill-tp').textContent;
    if (input) {
      input.value = String(pos.avg + 0.01);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    const after = container.querySelector('.mbgc-pill-tp').textContent;
    assert(after !== before, 'S6: typing an exact price live-updates the provisional pill (bidirectional sync with drag)');
    const cancelBtn = container.querySelector('.mbgc-edit-cancel');
    if (cancelBtn) cancelBtn.click();
  }

  // S7 — a CONFIRMED existing level can be re-dragged after its first
  // release (not just once) — drag -> release(edit) -> drag again ->
  // Confirm sends exactly once with the FINAL price.
  {
    const calls = [];
    chart.onSlTpConfirm = (id, field, price) => { calls.push({ id, field, price }); };
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    let slEl = container.querySelector('.mbgc-pill-sl');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointermove', { clientY: 260 });
    firePointer(slEl, 'pointerup', { clientY: 260 });
    assert(container.querySelector('.mbgc-pill-sl').className.indexOf('mbgc-pill-editing') !== -1, 'S7: first release shows the EXISTING-level editing ring (not provisional)');
    slEl = container.querySelector('.mbgc-pill-sl'); // re-query: _renderOverlay() may have recreated it
    firePointer(slEl, 'pointerdown', { clientY: 260 });
    firePointer(slEl, 'pointermove', { clientY: 220 });
    firePointer(slEl, 'pointerup', { clientY: 220 });
    const confirmBtn = container.querySelector('.mbgc-edit-confirm');
    if (confirmBtn) confirmBtn.click();
    assertEqual(calls.length, 1, 'S7: re-dragging after the first release still sends exactly once on Confirm');
    chart.resolveSlTpEdit('sl');
  }

  // S8 — drag disables chart pan/zoom; release restores it (this is
  // the mechanism guaranteeing the SAME gesture never also pans the
  // chart — "sin desplazar simultáneamente el chart").
  {
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    assertEqual(chart._gesturesDisabled, false, 'S8: gestures enabled before drag');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    assertEqual(chart._gesturesDisabled, true, 'S8: gestures disabled immediately on pointerdown');
    assertEqual(chart.chart.handleScroll, false, 'S8: chart.applyOptions({handleScroll:false}) actually reached the chart instance');
    firePointer(slEl, 'pointerup', { clientY: 300 });
    assertEqual(chart._gesturesDisabled, false, 'S8: gestures restored after release');
    const cancelBtn = container.querySelector('.mbgc-edit-cancel');
    if (cancelBtn) cancelBtn.click();
  }

  // S9 — switching the selected position mid-edit drops the
  // provisional state instead of leaking it onto the new position.
  {
    let calls = 0;
    chart.onSlTpConfirm = () => { calls++; };
    const posA = makePos({ id: 'A' });
    chart.setPositions([posA], posA.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointermove', { clientY: 250 });
    firePointer(slEl, 'pointerup', { clientY: 250 });
    assert(!!container.querySelector('.mbgc-edit-box'), 'S9: edit toolbar open before switching position');
    const posB = makePos({ id: 'B', sl: lastClose - 0.0055 });
    chart.setPositions([posB], posB.id);
    assert(!container.querySelector('.mbgc-edit-box'), 'S9: edit toolbar gone after switching position');
    assertEqual(calls, 0, 'S9: no send happened from the abandoned edit');
    const slElAfter = container.querySelector('.mbgc-pill-sl');
    assert(slElAfter && slElAfter.textContent.indexOf(chart._fmtPx(posB.sl)) !== -1, 'S9: new position shows its OWN confirmed sl');
  }

  // S10 — position disappears mid-edit: edit auto-cancels, gestures
  // restored, nothing left dangling.
  {
    const pos = makePos();
    chart.setPositions([pos], pos.id);
    const slEl = container.querySelector('.mbgc-pill-sl');
    firePointer(slEl, 'pointerdown', { clientY: 300 });
    firePointer(slEl, 'pointermove', { clientY: 250 });
    firePointer(slEl, 'pointerup', { clientY: 250 });
    chart.clearPositions();
    assert(!container.querySelector('.mbgc-edit-box'), 'S10: edit UI gone after position disappears');
    assertEqual(chart._gesturesDisabled, false, 'S10: chart gestures restored after position disappears mid-drag');
  }

  const passCount = results.filter(r => r.pass).length;
  const failCount = results.length - passCount;
  const out = document.createElement('pre');
  out.id = 'results';
  out.textContent = JSON.stringify({ passCount, failCount, results }, null, 2);
  document.body.appendChild(out);
})();
"""
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><title>t</title>"
            "<style>#wrap{width:390px;height:700px;position:relative;}"
            "#mobChartContainer{width:100%;height:100%;}"
            ".mbgc-pill{position:absolute;left:8px;}"
            ".mbgc-add{position:absolute;left:8px;}"
            ".mbgc-edit-box{position:absolute;left:8px;}</style>"
            "<script>" + FAKE_LWC + "</script>"
            "<script>" + trading_core_src + "</script>"
            "</head><body><div id='wrap'><div id='mobChartContainer'></div></div>"
            "<script>" + chart_src + "</script>"
            "<script>" + driver + "</script>"
            "</body></html>"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.html"
            path.write_text(html, encoding="utf-8")
            dom = _run_chrome_dump(path)
            data = _extract_json(dom, "results")
            if data is None:
                self.fail("could not extract results from the browser harness DOM dump")
            return data

    def test_drag_tap_confirm_cancel_resolve_state_machine(self):
        data = self._run()
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} chart-interaction assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


# ─────────────────────────────────────────────────────────────────────────
# B. Orchestration-level — the real order:update contract. Exercises the
#    REAL mobile.html onSlTpConfirm handler + onPositions match logic +
#    the REAL mobile_session.js updateSlTp()/clearSlTpPending(), via a
#    FakeWebSocket (same pattern this project's own Node suites use).
# ─────────────────────────────────────────────────────────────────────────
class OrchestrationRealExecutionTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _dumped_html(self):
        user = make_user()
        account = make_account(user, account_type="STANDARD")
        self.client.force_login(user)
        url = reverse("simulator:dashboard_account", args=[account.pk])
        r = self.client.get(url, HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _run(self):
        html = self._dumped_html()
        html = html.replace(
            '<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>',
            "<script>" + FAKE_LWC + "</script>",
        )
        # Inline the real sources directly (never a file:// relative
        # src="...") — the harness is written to a fresh temp directory
        # with nothing else in it, so a relative script src would 404
        # silently and the whole page would run with none of the real
        # classes defined.
        with open("simulator/static/simulator/trade/trading_core.js", encoding="utf-8") as f:
            trading_core_src = f.read()
        chart_src = _chart_source()
        session_src = _session_source()
        # lambda replacements (never a raw replacement string) — the
        # real JS sources legitimately contain backslash sequences
        # (e.g. regex literals) that re.sub would otherwise misparse as
        # backreference escapes. The hash segment is OPTIONAL: whether
        # {% static %} resolves to a ManifestStaticFilesStorage-hashed
        # filename depends on this process's static-file configuration
        # (collectstatic/manifest state), not something this test
        # should assume either way.
        html = re.sub(
            r'<script src="/static/simulator/trade/trading_core(?:\.[0-9a-f]+)?\.js"></script>',
            lambda m: "<script>" + trading_core_src + "</script>", html,
        )
        html = re.sub(
            r'<script src="/static/simulator/trade/mobile_chart(?:\.[0-9a-f]+)?\.js"></script>',
            lambda m: "<script>" + chart_src + "</script>", html,
        )
        html = re.sub(
            r'<script src="/static/simulator/trade/mobile_session(?:\.[0-9a-f]+)?\.js"></script>',
            lambda m: "<script>" + session_src + "</script>", html,
        )

        fake_ws = """
<script>
class FakeWebSocket {
  static get CONNECTING(){ return 0; }
  static get OPEN(){ return 1; }
  static get CLOSING(){ return 2; }
  static get CLOSED(){ return 3; }
  constructor(url){
    this.url = url; this.readyState = FakeWebSocket.CONNECTING; this.sent = [];
    window.__LAST_FAKE_WS__ = this;
    const self = this;
    setTimeout(function(){ self.readyState = FakeWebSocket.OPEN; if (self.onopen) self.onopen({}); }, 0);
  }
  send(data){ this.sent.push(data); }
  close(){ this.readyState = FakeWebSocket.CLOSED; if (this.onclose) this.onclose({}); }
  emit(msg){ if (this.onmessage) this.onmessage({ data: JSON.stringify(msg) }); }
}
window.WebSocket = FakeWebSocket;
</script>
"""
        # Injected at the very top of <head> — unconditional, independent
        # of matching any specific script tag — so window.WebSocket is
        # monkeypatched before ANY script (trading_core/mobile_chart/
        # mobile_session/the orchestrator's own session.connect()) runs,
        # regardless of how those were just inlined above.
        html = html.replace("<head>", "<head>" + fake_ws, 1)
        html = html.replace(
            "  session.connect();\n})();",
            "  session.connect();\n"
            "  window.__TEST_HOOKS__ = { mobileChart: mobileChart, session: session, pendingSlTpRef: function(){ return pendingSlTp; } };\n"
            "})();",
        )
        if "__TEST_HOOKS__" not in html:
            self.fail("could not locate the orchestrator IIFE's session.connect() call to attach test hooks")

        driver = r"""
<script>
(async function(){
  const results = [];
  function assert(cond, label){ results.push({label, pass: !!cond}); }
  function assertEqual(a, b, label){ results.push({label, pass: a === b, got: a, want: b}); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function waitFor(cond, timeoutMs){
    return new Promise(function(resolve){
      const start = Date.now();
      (function poll(){
        if (cond()) return resolve(true);
        if (Date.now() - start > timeoutMs) return resolve(false);
        setTimeout(poll, 10);
      })();
    });
  }

  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  const chart = hooks.mobileChart;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const SYM = 'EUR/USD';
  function emitQuote(){ ws().emit({ type: 'tick', symbol: SYM, bid: 1.12090, ask: 1.12106, source: 'test' }); }
  function emitPositions(items){ ws().emit({ type: 'positions', items: items }); }
  function lastSentUpdate(){
    const sent = ws().sent;
    for (let i = sent.length - 1; i >= 0; i--) {
      const msg = JSON.parse(sent[i]);
      if (msg.action === 'order:update') return msg;
    }
    return null;
  }
  function sentCount(){ return ws().sent.filter(function(s){ return JSON.parse(s).action === 'order:update'; }).length; }

  emitQuote();
  await sleep(20);

  // A — modify SL, preserve TP.
  {
    const before = sentCount();
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1180, tp: 1.1230, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.1175);
    await sleep(20);
    const msg = lastSentUpdate();
    assertEqual(sentCount(), before + 1, 'A: exactly one order:update sent');
    assert(msg && msg.id === 'POS1', 'A: sent id matches canonical position');
    assert(msg && msg.symbol === SYM, 'A: sent symbol matches canonical position (never a silent currentSymbol substitute)');
    assertEqual(msg && msg.sl, 1.1175, 'A: sent sl is the new requested value');
    assertEqual(msg && msg.tp, 1.123, 'A: sent tp preserves the existing CONFIRMED value (never null for the untouched field)');
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1230, qty: 0.02, pnl: 1.0 }]);
    await sleep(30);
    assert(!hooks.pendingSlTpRef(), 'A: pending cleared after matching snapshot');
  }

  // B — modify TP, preserve SL (also proves clearSlTpPending() frees
  // mobile_session.js's own single-slot guard promptly after A, rather
  // than leaving it blocked for the internal 20s safety-valve).
  {
    const before = sentCount();
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1230, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'tp', 1.1245);
    await sleep(20);
    const msg = lastSentUpdate();
    assertEqual(sentCount(), before + 1, 'B: exactly one order:update sent');
    assertEqual(msg && msg.tp, 1.1245, 'B: sent tp is the new requested value');
    assertEqual(msg && msg.sl, 1.1175, 'B: sent sl preserves the existing CONFIRMED value');
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(30);
    assert(!hooks.pendingSlTpRef(), 'B: pending cleared after matching snapshot');
  }

  // C — backend rejects -> rollback.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.1160);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'C: pending set right after confirm');
    ws().emit({ type: 'error', code: 'invalid_sl_direction', message: 'sl invalido' });
    await sleep(20);
    assert(!hooks.pendingSlTpRef(), 'C: pending cleared after backend rejection');
    const statusEl = document.getElementById('mobSlTpStatus');
    assert(statusEl && !statusEl.hidden && statusEl.classList.contains('is-error'), 'C: status shows the real rejection, marked as error');
    assert(!chart._edit, 'C: chart edit state cleared after rejection (visual reverted)');
  }

  // D — multi-position correction round: a snapshot that includes an
  // UNRELATED second position for the same symbol, while the EDITED
  // position is still present with its old (not-yet-matching) values,
  // never confirms — the unrelated position must never be mistaken for
  // a match.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.1170);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'D: pending set after confirm');
    emitPositions([
      { id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 },
      { id: 'OTHER', symbol: SYM, side: 'sell', avg: 1.10, sl: null, tp: null, qty: 0.01, pnl: 0 },
    ]);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'D: still pending — an unrelated second position in the snapshot is never mistaken for a match');
    ws().emit({ type: 'warn', message: 'order_update_not_found' });
    await sleep(20);
  }

  // D2 — multi-position correction round: the EDITED position
  // genuinely disappears from the snapshot entirely (closed) — this
  // round's own explicit rule: cancel the pending edit via the safe
  // behavior already implemented (never silently keep "confirming"
  // against a position that no longer exists).
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'tp', 1.126);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'D2: pending set after confirm');
    emitPositions([{ id: 'OTHER', symbol: SYM, side: 'sell', avg: 1.10, sl: null, tp: null, qty: 0.01, pnl: 0 }]);
    await sleep(20);
    assert(!hooks.pendingSlTpRef(), 'D2: pending cancelled once the edited position itself is gone from the snapshot');
  }

  // E — a snapshot for the SAME position but DIFFERENT values never
  // confirms either; only an exact match (at real precision) does.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.1165);
    await sleep(20);
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1172, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'E: still pending — snapshot values differ from what was requested');
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1165, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    assert(!hooks.pendingSlTpRef(), 'E: resolves once the matching snapshot actually arrives');
    const statusEl = document.getElementById('mobSlTpStatus');
    assert(statusEl && statusEl.textContent.indexOf('confirmado') !== -1, 'E: status shows confirmation text once matched');
  }

  // F — disconnected before send: refuses synchronously, reports
  // state, never queues a phantom pending request.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1165, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    const before = sentCount();
    const savedState = ws().readyState;
    ws().readyState = 3;
    chart.onSlTpConfirm('POS1', 'sl', 1.116);
    await sleep(20);
    assertEqual(sentCount(), before, 'F: zero new order:update sends while disconnected');
    assert(!hooks.pendingSlTpRef(), 'F: no pending request created when the send itself was refused');
    const statusEl = document.getElementById('mobSlTpStatus');
    assert(statusEl && !statusEl.hidden, 'F: status reports the failed-to-send state');
    ws().readyState = savedState;
  }

  // G — missing/mismatched id: refuses, never sends.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1165, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    const before = sentCount();
    chart.onSlTpConfirm('DOES-NOT-MATCH', 'sl', 1.1);
    await sleep(20);
    assertEqual(sentCount(), before, 'G: zero sends when the confirmed id does not match the canonical position');
    assert(!hooks.pendingSlTpRef(), 'G: no pending request created');
  }

  // H — timeout after send: no response within the UI-facing window ->
  // "cambio no confirmado", pending cleared, never a fabricated success.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1165, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.115);
    await sleep(20);
    assert(!!hooks.pendingSlTpRef(), 'H: pending set right after confirm');
    await sleep(8200);
    assert(!hooks.pendingSlTpRef(), 'H: pending cleared after the UI-facing timeout with no response');
    const statusEl = document.getElementById('mobSlTpStatus');
    assert(statusEl && statusEl.textContent.indexOf('no confirmado') !== -1, 'H: status says "cambio no confirmado", never a fabricated success');
  }

  // I — position disappears entirely while an edit is pending: the
  // chart drops its own edit UI the moment it has no position left.
  {
    emitPositions([{ id: 'POS1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1165, tp: 1.1245, qty: 0.02, pnl: 1.0 }]);
    await sleep(20);
    chart.onSlTpConfirm('POS1', 'sl', 1.114);
    await sleep(20);
    emitPositions([]);
    await sleep(20);
    assert(!chart._edit, 'I: chart drops its own edit UI once the position is gone from the snapshot');
    ws().emit({ type: 'warn', message: 'order_update_not_found' });
    await sleep(20);
  }

  // J — multi-position correction round: several open positions for
  // the SAME symbol are ALL drawn simultaneously (never netted/merged/
  // only-the-first), each at its own real entry; one is selected by
  // default; SL/TP editing touches ONLY the selected id's canonical
  // data, never copies between positions; selecting a different
  // position's capsule switches which one shows full controls.
  {
    const container = document.getElementById('mobChartContainer');
    emitPositions([
      { id: 'J1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.1175, tp: 1.1245, qty: 0.02, pnl: 1.0 },
      { id: 'J2', symbol: SYM, side: 'buy', avg: 1.1180, sl: null, tp: null, qty: 0.01, pnl: -0.5 },
      { id: 'J3', symbol: SYM, side: 'sell', avg: 1.1230, sl: 1.1260, tp: 1.1190, qty: 0.03, pnl: 2.1 },
      { id: 'J4-other-symbol', symbol: 'BTC/USD', side: 'buy', avg: 50000, sl: null, tp: null, qty: 0.01, pnl: 0 },
    ]);
    await sleep(20);
    assertEqual(container.querySelectorAll('.mbgc-pill-entry, .mbgc-pill-entry-compact').length, 3, 'J: all 3 open positions for THIS symbol get their own Entry capsule simultaneously — the other-symbol position (J4) never appears on the EUR/USD chart');
    assertEqual(container.querySelectorAll('.mbgc-pill-entry').length, 1, 'J: exactly one is the FULL selected capsule');
    assertEqual(container.querySelectorAll('.mbgc-pill-entry-compact').length, 2, 'J: the other two are compact, tap-to-select chips');
    // J1 is first in canonical order -> deterministic default selection.
    assert(!!hooks.pendingSlTpRef, 'J: hooks available'); // sanity
    const before = sentCount();
    chart.onSlTpConfirm('J1', 'sl', 1.117);
    await sleep(20);
    const msg = lastSentUpdate();
    assertEqual(sentCount(), before + 1, 'J: edit on the selected (J1) position sends');
    assertEqual(msg && msg.id, 'J1', 'J: sent id is exactly the selected position (never J2/J3)');
    assertEqual(msg && msg.tp, 1.1245, 'J: preserves J1\'s OWN tp — never copied from J2 or J3');
    emitPositions([
      { id: 'J1', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.117, tp: 1.1245, qty: 0.02, pnl: 1.0 },
      { id: 'J2', symbol: SYM, side: 'buy', avg: 1.1180, sl: null, tp: null, qty: 0.01, pnl: -0.5 },
      { id: 'J3', symbol: SYM, side: 'sell', avg: 1.1230, sl: 1.1260, tp: 1.1190, qty: 0.03, pnl: 2.1 },
    ]);
    await sleep(20);
    assert(!hooks.pendingSlTpRef(), 'J: J1 edit resolves normally while J2/J3 are also present');
    // Select J3's compact capsule instead.
    const compactChips = Array.from(container.querySelectorAll('.mbgc-pill-entry-compact'));
    const j3Chip = compactChips.find(function(el){ return true; }); // either compact chip works for this check
    assert(!!j3Chip, 'J: a compact chip exists to tap');
    if (j3Chip) j3Chip.click();
    await sleep(20);
    assertEqual(container.querySelectorAll('.mbgc-pill-entry').length, 1, 'J: after selecting a different capsule, still exactly one FULL capsule');
  }

  const passCount = results.filter(function(r){ return r.pass; }).length;
  const failCount = results.length - passCount;
  const out = document.createElement('pre');
  out.id = 'orch_results';
  out.textContent = JSON.stringify({ passCount: passCount, failCount: failCount, results: results }, null, 2);
  document.body.appendChild(out);
})();
</script>
"""
        html = html.replace("</body>", driver + "\n</body>")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness.html"
            path.write_text(html, encoding="utf-8")
            # Generous headroom over the ~8.8s of scripted sleeps/awaits
            # the driver actually needs (dominated by scenario H's own
            # 8.2s UI-facing-timeout wait) — under --virtual-time-budget,
            # any timer scheduled to fire AFTER the budget expires never
            # fires at all, silently truncating the script mid-run.
            dom = _run_chrome_dump(path, virtual_time_budget=20000, timeout=50)
            data = _extract_json(dom, "orch_results")
            if data is None:
                self.fail("could not extract results from the orchestration harness DOM dump")
            return data

    def test_order_update_contract_end_to_end(self):
        data = self._run()
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} orchestration assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


# ─────────────────────────────────────────────────────────────────────────
# C. Static contract checks (no browser needed) — the minimal addition
#    to mobile_session.js stays exactly minimal, and mobile.html never
#    bypasses it.
# ─────────────────────────────────────────────────────────────────────────
class ContractSurfaceTests(SimpleTestCase):
    def test_session_exposes_exactly_the_minimal_new_surface(self):
        src = _session_source()
        self.assertIn("updateSlTp({", src)
        self.assertIn("clearSlTpPending(id)", src)
        self.assertIn("this.onSlTpUpdateRejected", src)
        self.assertIn("_slTpSendingId", src)

    def test_mobile_html_never_constructs_the_action_string_itself(self):
        with open(MOBILE_HTML_PATH, encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("order:update", html)
        self.assertIn("session.updateSlTp(", html)
        self.assertIn("session.clearSlTpPending(", html)
