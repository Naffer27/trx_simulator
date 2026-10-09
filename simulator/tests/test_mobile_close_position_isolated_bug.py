# simulator/tests/test_mobile_close_position_isolated_bug.py
"""
ISOLATED BUG — Mobile position close (Gráfico × and Mobile->Posiciones
Cerrar).

Root cause (confirmed by isolated reproduction before any code change —
see the work report delivered alongside this file): with the real
chart's own tight rightPriceScale bottom margin (scaleMargins.bottom:
0.02, set in MobileTradingChart.initialize(), untouched by this fix), a
selected position whose canonical entry price sits near the LOW end of
the currently visible candle range computes an Entry-capsule Y close to
or past the bottom edge of .mbg-chart's own clipping box. The capsule —
and specifically its × close button — then visually/physically
overlapped #mobTicket's trade-bar (VENDER/COMPRAR), which sits directly
below in normal document flow and intercepted the tap via the browser's
real hit-testing before it ever reached the × (onCloseClick/
session.closePosition()/the WS send were never broken — confirmed
working correctly the moment a tap could actually reach the button).

A second, related interception was found while reproducing a realistic
multi-position scenario: a NON-selected position's compact select-chip
can land at the same screen coordinates as the SELECTED position's own
"+ Añadir SL/TP" buttons; at the tied z-index both used before this fix,
the add-affordances (appended to the DOM later) won real hit-testing,
so a tap meant to SELECT a different position before its × could even
be reached was instead intercepted by "+ SL"/"+ TP".

Both fixes are pure coordinate/stacking corrections in
mobile_chart.js's _layoutEntryCapsules() (a bottom/top edge clamp, same
category as the pre-existing off-screen SL/TP pill clamp) and a z-index
bump on .mbgc-pill-entry/.mbgc-pill-entry-compact in mobile.html's
<style> (mobile_chart.js only ever draws; mobile.html alone owns their
presentation). Neither changes the close contract itself — the SAME
session.closePosition()/order:close flow that was already correct is
reused verbatim; mobile_session.js is UNCHANGED.

Real-execution tests run the ACTUAL, unmodified source files in a real
headless browser (Chrome), using a deterministic FakeLWC (same
discipline as this project's own Node FAKE_LWC_HARNESS) and a
FakeWebSocket (same pattern already used in test_mobile_sltp_position_
editing.py). No network, no real account, no server started by these
tests. Skipped gracefully when no Chrome-family browser is found.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


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

# Same deterministic, dependency-free Lightweight Charts stand-in as
# test_mobile_sltp_position_editing.py's own FAKE_LWC (linear
# priceToCoordinate()/coordinateToPrice() — real inverse functions,
# real pixel values a real DOM layout + real elementFromPoint() hit-
# testing can meaningfully exercise).
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


def _run_chrome_dump(html_path, window_size="390,844", virtual_time_budget=8000, timeout=30):
    result = subprocess.run(
        [
            CHROME_PATH, "--headless", "--disable-gpu", "--hide-scrollbars",
            "--window-size=" + window_size,
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
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    try:
        return json.loads(text)
    except ValueError:
        return None


def _dump_mobile_html(test_case):
    user = make_user()
    account = make_account(user, account_type="STANDARD")
    test_case.client.force_login(user)
    url = reverse("simulator:dashboard_account", args=[account.pk])
    r = test_case.client.get(url, HTTP_USER_AGENT=IPHONE_UA)
    test_case.assertEqual(r.status_code, 200)
    return r.content.decode()


def _inline_real_sources(html, chart_script):
    """Replace every real <script src> this suite cares about with the
    ACTUAL, unmodified file contents inlined — chart_script is either
    FAKE_LWC or the real vendored lightweight-charts build, selected by
    the caller."""
    html = html.replace(
        '<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>',
        "<script>" + chart_script + "</script>",
    )
    with open("simulator/static/simulator/trade/trading_core.js", encoding="utf-8") as f:
        trading_core_src = f.read()
    with open("simulator/static/simulator/trade/mobile_chart.js", encoding="utf-8") as f:
        chart_src = f.read()
    with open("simulator/static/simulator/trade/mobile_session.js", encoding="utf-8") as f:
        session_src = f.read()
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
    return html


FAKE_WS_SCRIPT = """
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


def _inject_fake_ws_and_hooks(html, test_case):
    html = html.replace("<head>", "<head>" + FAKE_WS_SCRIPT, 1)
    html = html.replace(
        "  session.connect();\n})();",
        "  session.connect();\n"
        "  window.__TEST_HOOKS__ = { mobileChart: mobileChart, session: session, pendingSlTpRef: function(){ return pendingSlTp; }, refreshPositionLines: refreshPositionLines };\n"
        "})();",
    )
    if "__TEST_HOOKS__" not in html:
        test_case.fail("could not locate the orchestrator IIFE's session.connect() call to attach test hooks")
    return html


class ClosePositionIsolatedBugTests(TestCase):
    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def _dumped_html(self):
        return _dump_mobile_html(self)

    def _run(self):
        html = self._dumped_html()
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)

        driver = r"""
<script>
window.addEventListener('error', function(e){
  const out = document.createElement('pre');
  out.id = 'close_bug_results';
  out.textContent = JSON.stringify({ passCount: 0, failCount: 1, results: [{label: 'uncaught error: ' + e.message + ' @ ' + (e.filename||'') + ':' + (e.lineno||''), pass: false}] });
  if (!document.getElementById('close_bug_results')) document.body.appendChild(out);
});
(async function(){
  try {
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
  // Real hit-test dispatch — click() called directly on an element
  // bypasses the browser's own hit-testing and would mask exactly the
  // bug this suite guards against. This mirrors a real tap: find
  // whatever element the browser would ACTUALLY deliver the event to
  // at that screen point, then dispatch there.
  function realClick(el){
    const rect = el.getBoundingClientRect();
    const cx = rect.x + rect.width/2, cy = rect.y + rect.height/2;
    const target = document.elementFromPoint(cx, cy);
    if (target) target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));
    return target;
  }

  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  const chart = hooks.mobileChart;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const SYM = 'EUR/USD';
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
  function emitQuote(){ ws().emit({ type: 'tick', symbol: SYM, bid: 1.1209, ask: 1.1211, source: 'test' }); }
  function emitHistory(){
    const tfSec = 900, now = Math.floor(Date.now()/1000);
    const start = Math.floor(now/tfSec)*tfSec - 60*tfSec;
    ws().emit({ type: 'history', symbol: SYM, timeframe: '15m', phase: 'complete', data: fixtureBars(60, start, tfSec) });
  }
  function emitPositions(items){ ws().emit({ type: 'positions', items: items }); }
  function lastSentClose(){
    const sent = ws().sent;
    for (let i = sent.length - 1; i >= 0; i--) {
      const msg = JSON.parse(sent[i]);
      if (msg.action === 'order:close') return msg;
    }
    return null;
  }
  function sentCloseCount(){ return ws().sent.filter(function(s){ return JSON.parse(s).action === 'order:close'; }).length; }
  function selectByText(substr){
    const chips = Array.from(document.querySelectorAll('.mbgc-pill-entry-compact'));
    const chip = chips.find(function(c){ return c.textContent.indexOf(substr) !== -1; });
    if (chip) realClick(chip);
    return chip;
  }
  // Selection persists across snapshots for a position that is still
  // open (correct canonical-reconciliation behavior, not a bug) — so a
  // scenario that needs a SPECIFIC position selected regardless of
  // what an earlier scenario left selected must select explicitly by
  // that position's own unique qty/pnl text, never assume "default"
  // order. No-ops (returns true) if that position's full capsule is
  // already the selected one.
  function ensureSelected(priceText, qtyText, pnlText){
    const full = document.querySelector('.mbgc-pill-entry');
    if (full && full.textContent.indexOf(qtyText) !== -1 && full.textContent.indexOf(pnlText) !== -1) return true;
    // A compact (non-selected) chip only ever shows side-letter + price
    // (mobile_chart.js's _renderEntryCapsule, else-branch) — never
    // qty/pnl — so it must be found by its own price text, then
    // confirmed to have produced the RIGHT full capsule afterward.
    const chips = Array.from(document.querySelectorAll('.mbgc-pill-entry-compact'));
    const chip = chips.find(function(c){ return c.textContent.indexOf(priceText) !== -1; });
    if (chip) realClick(chip);
    return !!chip;
  }
  function fullCapsuleCloseBtn(){
    const cap = document.querySelector('.mbgc-pill-entry');
    return cap ? cap.querySelector('.mbgc-pill-close') : null;
  }
  // Every scenario below that provokes a real send must leave the
  // close guard (closeInFlight / mobile_session.js's _closeSending)
  // released before the next scenario runs, exactly like a real
  // backend eventually would (reject/timeout/success) — otherwise a
  // later scenario's own tap would be silently swallowed by the SAME
  // guard this suite is verifying, which would read as a false
  // regression in a LATER scenario instead of the true cause.
  function releaseCloseGuard(){ ws().emit({ type: 'warn', message: 'order_close_not_found' }); }

  emitQuote();
  await sleep(20);
  emitHistory();
  await sleep(20);

  const A = { id: 'A', symbol: SYM, side: 'buy', avg: 1.1200, sl: null, tp: null, qty: 0.02, pnl: 1.0 };
  const B = { id: 'B', symbol: SYM, side: 'buy', avg: 1.1190, sl: null, tp: null, qty: 0.01, pnl: 0.5 };
  const C = { id: 'C', symbol: SYM, side: 'sell', avg: 1.1220, sl: null, tp: null, qty: 0.03, pnl: -0.2 };

  // A+B — FACTUAL FINDING (reported in the work report, not assumed):
  // the current, unmodified close flow has NO Cancel/Confirm dialog —
  // mobileChart.onCloseClick fires session.closePosition(id) directly
  // once closeInFlight/pendingSlTp allow it (verified by reading
  // mobile.html's own onCloseClick wiring; unchanged by this fix, since
  // adding a dialog would be a flow/behavior change, not a bug fix).
  // So this scenario verifies what the task's own conditional text
  // reduces to when no such dialog exists: select B via its REAL
  // compact chip (not an unrelated add-affordance), tap its real ×,
  // and confirm EXACTLY ONE message is sent containing B's id — never
  // A's or C's — and that selecting/closing never perturbs chart
  // gestures or starts an SL/TP edit as a side effect.
  {
    emitPositions([A, B, C]);
    await sleep(30);
    const chip = selectByText('1.11900');
    assert(!!chip, "A+B: B's own compact chip is found and tappable");
    await sleep(20);
    const sel = document.querySelector('.mbgc-pill-entry');
    assert(!!sel && sel.textContent.indexOf('0.01') !== -1 && sel.textContent.indexOf('0.50') !== -1, "A+B: selecting B's chip actually selects B (its own qty/pnl), not A");
    assertEqual(chart._gesturesDisabled, false, 'A+B: selecting a position never leaves chart gestures disabled (no accidental drag)');
    assert(!chart._edit, 'A+B: selecting a position never starts an SL/TP edit');
    const closeBtn = fullCapsuleCloseBtn();
    assert(!!closeBtn, "A+B: B's close (×) button exists on the selected capsule");
    const before = sentCloseCount();
    if (closeBtn) realClick(closeBtn);
    await sleep(20);
    const msg = lastSentClose();
    assertEqual(sentCloseCount(), before + 1, 'A+B: exactly one order:close sent for a real tap on ×');
    assertEqual(msg && msg.id, 'B', 'A+B: sent id is exactly B — never A or C');
    releaseCloseGuard();
    await sleep(20);
  }

  // C — backend simulated success: snapshot without B. Only B
  // disappears; A and C remain; selection reconciles.
  {
    emitPositions([A, C]);
    await sleep(30);
    const ids = Array.from(document.querySelectorAll('.mbgc-pill-entry, .mbgc-pill-entry-compact')).length;
    assertEqual(ids, 2, "C: only A and C remain after B's close is confirmed by the canonical snapshot");
    assert(!document.querySelector('.mbgc-pill-entry-compact') || document.querySelectorAll('.mbgc-pill-entry, .mbgc-pill-entry-compact').length === 2, 'C: no leftover B overlay');
  }

  // D — rejection: B remains open, guard releases, error visible, can
  // retry.
  {
    emitPositions([A, B, C]);
    await sleep(30);
    selectByText('1.11900');
    await sleep(20);
    const closeBtn = fullCapsuleCloseBtn();
    const before = sentCloseCount();
    if (closeBtn) realClick(closeBtn);
    await sleep(20);
    assertEqual(sentCloseCount(), before + 1, 'D: close B sent');
    ws().emit({ type: 'warn', message: 'order_close_not_found' });
    await sleep(20);
    emitPositions([A, B, C]); // backend truth: nothing actually closed
    await sleep(20);
    const sel = document.querySelector('.mbgc-pill-entry');
    assert(!!sel, 'D: a selected capsule still exists after rejection (B was never removed)');
    // Retry must be possible — guard released.
    const closeBtn2 = fullCapsuleCloseBtn();
    const before2 = sentCloseCount();
    if (closeBtn2) realClick(closeBtn2);
    await sleep(20);
    assertEqual(sentCloseCount(), before2 + 1, 'D: after a rejection, the close guard is released and a retry can be sent');
    releaseCloseGuard();
    await sleep(20);
  }

  // E — double tap on × never produces two sends (closeInFlight guard).
  {
    emitPositions([A, B, C]);
    await sleep(30);
    selectByText('1.11900');
    await sleep(20);
    const before = sentCloseCount();
    const closeBtn = fullCapsuleCloseBtn();
    if (closeBtn) { realClick(closeBtn); realClick(fullCapsuleCloseBtn() || closeBtn); }
    await sleep(20);
    assertEqual(sentCloseCount(), before + 1, 'E: two rapid taps on × never produce more than one order:close send');
    releaseCloseGuard();
    await sleep(20);
  }

  // G — close after a resolved SL/TP edit: edit A's SL, confirm, let
  // the matching snapshot resolve it, THEN close A — must use id A and
  // must not be blocked by stale SL/TP state.
  {
    emitPositions([A, B, C]);
    await sleep(30);
    chart.onSlTpConfirm('A', 'sl', 1.118);
    await sleep(20);
    emitPositions([
      { id: 'A', symbol: SYM, side: 'buy', avg: 1.1200, sl: 1.118, tp: null, qty: 0.02, pnl: 1.0 }, B, C,
    ]);
    await sleep(20);
    assert(!hooks.pendingSlTpRef(), 'G: SL/TP edit on A is fully resolved before attempting to close it');
    const selected = ensureSelected('1.12000', '0.02', '1.00');
    assert(selected, 'G: A can be explicitly (re-)selected regardless of what an earlier scenario left selected');
    await sleep(20);
    const selFull = document.querySelector('.mbgc-pill-entry');
    assert(!!selFull && selFull.textContent.indexOf('0.02') !== -1 && selFull.textContent.indexOf('1.00') !== -1, "G: selecting by A's own price actually selects A (its own qty/pnl)");
    const closeBtn = fullCapsuleCloseBtn();
    const before = sentCloseCount();
    if (closeBtn) realClick(closeBtn);
    await sleep(20);
    const msg = lastSentClose();
    assertEqual(sentCloseCount(), before + 1, 'G: close after a resolved SL/TP edit sends normally');
    assertEqual(msg && msg.id, 'A', "G: close uses A's own id");
    releaseCloseGuard();
    await sleep(20);
  }

  // I — disconnected before send: zero messages, UI not stuck.
  {
    emitPositions([A, B, C]);
    await sleep(30);
    selectByText('1.11900');
    await sleep(20);
    const savedState = ws().readyState;
    ws().readyState = 3; // CLOSED
    const before = sentCloseCount();
    const closeBtn = fullCapsuleCloseBtn();
    if (closeBtn) realClick(closeBtn);
    await sleep(20);
    assertEqual(sentCloseCount(), before, 'I: disconnected — zero close messages sent');
    ws().readyState = savedState;
    // UI must not be permanently stuck: a close must still be possible
    // once reconnected/open again.
    const closeBtn2 = fullCapsuleCloseBtn();
    const before2 = sentCloseCount();
    if (closeBtn2) realClick(closeBtn2);
    await sleep(20);
    assertEqual(sentCloseCount(), before2 + 1, 'I: once the connection is open again, close is not permanently blocked');
    releaseCloseGuard();
    await sleep(20);
  }

  const passCount = results.filter(function(r){ return r.pass; }).length;
  const failCount = results.length - passCount;
  const out = document.createElement('pre');
  out.id = 'close_bug_results';
  out.textContent = JSON.stringify({ passCount: passCount, failCount: failCount, results: results }, null, 2);
  document.body.appendChild(out);
  } catch (e) {
    const out = document.createElement('pre');
    out.id = 'close_bug_results';
    out.textContent = JSON.stringify({ passCount: 0, failCount: 1, results: [{label: 'driver threw: ' + e.message + '\\n' + e.stack, pass: false}] });
    document.body.appendChild(out);
  }
})();
</script>
"""
        return _run_driver_and_extract(self, html, driver, window_size="390,844", virtual_time_budget=8000)

    def test_close_position_by_correct_id_real_hit_testing(self):
        data = self._run()
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} close-bug assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 15)


def _run_driver_and_extract(test_case, html, driver_script, window_size="390,844", virtual_time_budget=8000, result_id="close_bug_results"):
    html = html.replace("</body>", driver_script + "\n</body>")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "harness.html"
        path.write_text(html, encoding="utf-8")
        dom = _run_chrome_dump(path, window_size=window_size, virtual_time_budget=virtual_time_budget)
        data = _extract_json(dom, result_id)
        if data is None:
            test_case.fail(f"could not extract results from the harness DOM dump (id={result_id})")
        return data


REAL_LWC_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lightweight-charts.4.1.1.standalone.production.js"


def _load_real_lwc():
    if not REAL_LWC_FIXTURE.exists():
        return None
    return REAL_LWC_FIXTURE.read_text(encoding="utf-8")


REAL_LWC_SOURCE = _load_real_lwc()


# Shared driver scaffolding (error trap + try/catch + results dump) reused
# by both real-execution checks below — same discipline as the main
# close-bug driver above, trimmed to what each check actually needs.
def _driver_wrap(body_js, result_id="check_results"):
    return (
        "<script>\n"
        "window.addEventListener('error', function(e){\n"
        "  const out = document.createElement('pre');\n"
        f"  out.id = '{result_id}';\n"
        "  out.textContent = JSON.stringify({ passCount: 0, failCount: 1, results: [{label: 'uncaught error: ' + e.message + ' @ ' + (e.filename||'') + ':' + (e.lineno||''), pass: false}] });\n"
        f"  if (!document.getElementById('{result_id}')) document.body.appendChild(out);\n"
        "});\n"
        "(async function(){\n"
        "  const results = [];\n"
        "  function assert(cond, label){ results.push({label, pass: !!cond}); }\n"
        "  function assertEqual(a, b, label){ results.push({label, pass: a === b, got: a, want: b}); }\n"
        "  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }\n"
        "  function waitFor(cond, timeoutMs){\n"
        "    return new Promise(function(resolve){\n"
        "      const start = Date.now();\n"
        "      (function poll(){\n"
        "        if (cond()) return resolve(true);\n"
        "        if (Date.now() - start > timeoutMs) return resolve(false);\n"
        "        setTimeout(poll, 10);\n"
        "      })();\n"
        "    });\n"
        "  }\n"
        "  function realClick(el){\n"
        "    const rect = el.getBoundingClientRect();\n"
        "    const cx = rect.x + rect.width/2, cy = rect.y + rect.height/2;\n"
        "    const target = document.elementFromPoint(cx, cy);\n"
        "    if (target) target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));\n"
        "    return target;\n"
        "  }\n"
        "  try {\n"
        + body_js +
        "\n"
        "  const passCount = results.filter(function(r){ return r.pass; }).length;\n"
        "  const failCount = results.length - passCount;\n"
        "  const out = document.createElement('pre');\n"
        f"  out.id = '{result_id}';\n"
        "  out.textContent = JSON.stringify({ passCount: passCount, failCount: failCount, results: results }, null, 2);\n"
        "  document.body.appendChild(out);\n"
        "  } catch (e) {\n"
        "    const out = document.createElement('pre');\n"
        f"    out.id = '{result_id}';\n"
        "    const partial = (typeof results !== 'undefined' && Array.isArray(results)) ? results : [];\n"
        "    const crashed = partial.concat([{label: 'driver threw: ' + e.message + ' | ' + e.stack, pass: false}]);\n"
        "    const pc = crashed.filter(function(r){ return r.pass; }).length;\n"
        "    out.textContent = JSON.stringify({ passCount: pc, failCount: crashed.length - pc, results: crashed });\n"
        "    document.body.appendChild(out);\n"
        "  }\n"
        "})();\n"
        "</script>\n"
    )


class ClosePositionRealChartTests(TestCase):
    """Check 1 (user-requested re-verification): repeats the close-bug
    reproduction using the REAL, unmodified Lightweight Charts 4.1.1
    build (vendored at simulator/tests/fixtures/, pinned to the exact
    version mobile.html/desktop.html load from unpkg) instead of the
    FakeLWC stand-in — real priceToCoordinate(), real canvas-backed
    layout — with positions deliberately placed near the chart's own
    bottom edge and clustered close together. Still a FakeWebSocket;
    still no server, no network call, no real account."""

    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")
        if REAL_LWC_SOURCE is None:
            self.skipTest(f"real Lightweight Charts fixture missing at {REAL_LWC_FIXTURE}")

    def test_close_near_bottom_edge_with_real_chart_never_hits_buy_sell(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, REAL_LWC_SOURCE)
        html = _inject_fake_ws_and_hooks(html, self)

        body = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const SYM = 'EUR/USD';
  function fixtureBars(n, startTime, tf, base){
    const bars = [];
    let seed = 11, price = base;
    function rnd(){ seed = (seed*16807) % 2147483647; return seed/2147483647; }
    for(let i=0;i<n;i++){
      const o = price, c = o + (rnd()-0.5)*0.0010;
      const hi = Math.max(o,c) + rnd()*0.0004, lo = Math.min(o,c) - rnd()*0.0004;
      bars.push({ time: startTime + i*tf, open:o, high:hi, low:lo, close:c, volume: 10+rnd()*40 });
      price = c;
    }
    return bars;
  }
  function sentCloseMsgs(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:close'; }); }
  function sentNewOrderMsgs(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:new'; }); }
  function releaseCloseGuard(){ ws().emit({ type: 'warn', message: 'order_close_not_found' }); }
  function buySellEl(){ return document.getElementById('mobBuyBtn') || document.getElementById('mobSellBtn'); }
  function isBuyOrSell(el){
    return !!(el && el.closest && (el.closest('#mobBuyBtn') || el.closest('#mobSellBtn') || el.closest('.mbg-buy') || el.closest('.mbg-sell')));
  }

  ws().emit({ type: 'tick', symbol: SYM, bid: 1.08955, ask: 1.08975, source: 'test' });
  await sleep(30);
  const tfSec = 900, now = Math.floor(Date.now()/1000);
  const start = Math.floor(now/tfSec)*tfSec - 80*tfSec;
  // Real candle range deliberately spans roughly 1.0895–1.0990 — the
  // positions below sit AT/NEAR the low end of this real range, so the
  // REAL chart's own priceToCoordinate() (not a fake linear stand-in)
  // places their Entry capsule near the bottom edge exactly the way
  // the real app would for a position opened near a local low.
  ws().emit({ type: 'history', symbol: SYM, timeframe: '15m', phase: 'complete', data: fixtureBars(80, start, tfSec, 1.0940) });
  // Real LWC needs a real render pass (canvas/RAF-backed, unlike the
  // FakeLWC stand-in's synchronous math) before priceToCoordinate()
  // reflects the loaded range — give it real time to settle.
  await sleep(400);

  // Three positions, same symbol, clustered together AND near the low
  // edge of the visible range — stresses BOTH confirmed root causes
  // (edge-clamp geometry + z-index tie-break) at once, with the real
  // engine.
  const A = { id: 'A', symbol: SYM, side: 'buy', avg: 1.08960, sl: null, tp: null, qty: 0.02, pnl: 1.0 };
  const B = { id: 'B', symbol: SYM, side: 'buy', avg: 1.08965, sl: null, tp: null, qty: 0.01, pnl: 0.5 };
  const C = { id: 'C', symbol: SYM, side: 'sell', avg: 1.08970, sl: null, tp: null, qty: 0.03, pnl: -0.2 };
  ws().emit({ type: 'positions', items: [A, B, C] });
  await sleep(400);

  // Deterministic per-position check: each position's own overlay DOM
  // element is looked up via the chart's own internal keyed store
  // (_posOverlayEls['entry_'+id] — the SAME element _renderEntryCapsule
  // itself keys and reuses across renders, never recreated), so WHICH
  // element belongs to which id is never in question — but the actual
  // selection tap is still a REAL elementFromPoint()+dispatchEvent hit
  // test on that element's own real screen rect, exactly exercising the
  // z-index-tie-break surface bug 2 lived in (a different position's
  // own "+ SL/TP" add-affordance can only ever steal a tap at a given
  // SCREEN COORDINATE — looking up the right element by id first does
  // not change what real hit-testing resolves to at that coordinate).
  for (const pos of [A, B, C]) {
    const el = hooks.mobileChart._posOverlayEls && hooks.mobileChart._posOverlayEls['entry_' + pos.id];
    assert(!!el, 'real-chart: ' + pos.id + ' has its own entry overlay element');
    if (!el) continue;
    if (!el.classList.contains('mbgc-pill-entry')) {
      const selRect = el.getBoundingClientRect();
      const scx = selRect.x + selRect.width/2, scy = selRect.y + selRect.height/2;
      const selHit = document.elementFromPoint(scx, scy);
      assert(!isBuyOrSell(selHit), 'real-chart: ' + pos.id + "'s selection tap coordinate does not resolve to VENDER/COMPRAR");
      const selTarget = realClick(el);
      assert(!isBuyOrSell(selTarget), 'real-chart: ' + pos.id + "'s real-dispatched selection tap was never VENDER/COMPRAR");
      await sleep(250);
    }
    const full = document.querySelector('.mbgc-pill-entry');
    assert(!!full && full.textContent.indexOf(Number(pos.qty).toFixed(2)) !== -1, 'real-chart: selecting ' + pos.id + " via its own real element actually selects it (its own qty is shown)");
    const closeBtn = full ? full.querySelector('.mbgc-pill-close') : null;
    assert(!!closeBtn, 'real-chart: ' + pos.id + "'s × close button exists in the DOM");
    if (!closeBtn) continue;
    const rect = closeBtn.getBoundingClientRect();
    const cx = rect.x + rect.width/2, cy = rect.y + rect.height/2;
    const hitTarget = document.elementFromPoint(cx, cy);
    assert(!isBuyOrSell(hitTarget), 'real-chart: ' + pos.id + "'s × real tap coordinate does not resolve to VENDER/COMPRAR");
    const before = sentCloseMsgs().length;
    const target = realClick(closeBtn);
    await sleep(150);
    assert(!isBuyOrSell(target), 'real-chart: ' + pos.id + "'s real-dispatched tap target itself was never VENDER/COMPRAR");
    const closes = sentCloseMsgs();
    assertEqual(closes.length, before + 1, 'real-chart: ' + pos.id + ': exactly one order:close sent');
    const last = closes[closes.length - 1];
    assertEqual(last && last.id, pos.id, 'real-chart: ' + pos.id + ": order:close carries this position's OWN id");
    releaseCloseGuard();
    await sleep(150);
  }

  assertEqual(sentNewOrderMsgs().length, 0, 'real-chart: zero order:new (BUY/SELL) sent at any point during selection/close');
"""
        driver = _driver_wrap(body, "check_results")
        data = _run_driver_and_extract(self, html, driver, window_size="390,844", virtual_time_budget=12000, result_id="check_results")
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} real-chart assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 20)


class ClosePositionNeverRespondsTests(TestCase):
    """Check 2 (user-requested re-verification): order:close IS sent,
    then the connection drops or the backend simply never answers at
    all (client-side these are indistinguishable — once the message is
    sent, silence is silence either way). The UI must report "not
    confirmed", never delete the position or declare success, release
    its own guards so it is not stuck, never auto-resend, and still
    reconcile correctly with whatever canonical snapshot eventually
    does arrive. Uses FakeLWC (pure geometry is not what this check is
    about) + FakeWebSocket. No server, no network, no real account."""

    def setUp(self):
        if not CHROME_PATH:
            self.skipTest("no Chrome-family browser found")

    def test_close_sent_then_silence_reports_unconfirmed_and_recovers(self):
        html = _dump_mobile_html(self)
        html = _inline_real_sources(html, FAKE_LWC)
        html = _inject_fake_ws_and_hooks(html, self)

        body = r"""
  await waitFor(function(){ return !!window.__TEST_HOOKS__ && !!window.__LAST_FAKE_WS__; }, 3000);
  const hooks = window.__TEST_HOOKS__;
  function ws(){ return window.__LAST_FAKE_WS__; }
  await waitFor(function(){ return ws() && ws().readyState === 1; }, 3000);

  const SYM = 'EUR/USD';
  function fixtureBars(n, startTime, tf){
    const bars = [];
    let seed = 3, price = 1.1000;
    function rnd(){ seed = (seed*16807) % 2147483647; return seed/2147483647; }
    for(let i=0;i<n;i++){
      const o = price, c = o + (rnd()-0.5)*0.0010;
      const hi = Math.max(o,c) + rnd()*0.0004, lo = Math.min(o,c) - rnd()*0.0004;
      bars.push({ time: startTime + i*tf, open:o, high:hi, low:lo, close:c, volume: 10+rnd()*40 });
      price = c;
    }
    return bars;
  }
  function sentCloseMsgs(){ return ws().sent.map(function(s){ return JSON.parse(s); }).filter(function(m){ return m.action === 'order:close'; }); }
  const elTicketStatus = document.getElementById('mobTicketStatus');
  const elCloseBtn = function(){ return document.querySelector('.mob-close-btn'); };

  ws().emit({ type: 'tick', symbol: SYM, bid: 1.1009, ask: 1.1011, source: 'test' });
  await sleep(20);
  const tfSec = 900, now = Math.floor(Date.now()/1000);
  const start = Math.floor(now/tfSec)*tfSec - 40*tfSec;
  ws().emit({ type: 'history', symbol: SYM, timeframe: '15m', phase: 'complete', data: fixtureBars(40, start, tfSec) });
  await sleep(20);

  const A = { id: 'A', symbol: SYM, side: 'buy', avg: 1.1000, sl: null, tp: null, qty: 0.02, pnl: 1.0 };
  ws().emit({ type: 'positions', items: [A] });
  await sleep(30);

  // Send the close, then go silent — no order_close, no order_close_
  // not_found warn, no 'error', and (to also cover the disconnection
  // half of this check) drop the socket right after sending, exactly
  // as a real connection loss right after the send would look from
  // this client's own point of view.
  const full = document.querySelector('.mbgc-pill-entry');
  assert(!!full, 'never-responds: A has a rendered, selected full capsule');
  const closeBtn = full ? full.querySelector('.mbgc-pill-close') : null;
  assert(!!closeBtn, "never-responds: A's × close button exists");
  const target = realClick(closeBtn);
  await sleep(50);
  const closesAfterSend = sentCloseMsgs();
  assertEqual(closesAfterSend.length, 1, 'never-responds: exactly one order:close was actually sent');
  assertEqual(closesAfterSend[0] && closesAfterSend[0].id, 'A', "never-responds: it carries A's own id");
  ws().readyState = 3; // CLOSED — connection drops right after the send, no response will ever arrive

  // Immediately after sending (well before the UI's own no-response
  // timeout), the position must still be shown exactly as before —
  // "sent" is never conflated with "confirmed".
  assert(!!document.querySelector('.mbgc-pill-entry'), 'never-responds: immediately after sending, A is still shown as open (not optimistically removed)');
  assertEqual(elTicketStatus.textContent.indexOf('no confirmado'), -1, 'never-responds: no "not confirmed" message yet — the timeout has not fired');

  // Wait past the UI-facing no-response timeout (8s) with NOTHING ever
  // arriving — no order_close, no warn, no error, no later snapshot.
  await sleep(8500);

  assert(elTicketStatus.textContent.toLowerCase().indexOf('no confirmado') !== -1, 'never-responds: UI reports the close as not confirmed once the window elapses — got: "' + elTicketStatus.textContent + '"');
  assert(!!document.querySelector('.mbgc-pill-entry'), 'never-responds: the position was NOT deleted locally — still shown as open');
  assertEqual(sentCloseMsgs().length, 1, 'never-responds: still exactly one order:close ever sent — no automatic resend after the timeout');

  // Guards/controls must be released — Posiciones' own Close button,
  // disabled while closeInFlight was true, is usable again, and a
  // fresh close attempt is not silently swallowed (both mobile.html's
  // own closeInFlight AND mobile_session.js's internal _closeSending
  // safety-valve must have let go; the latter only matters for a
  // TRUE backend silence — covered structurally by its own 20s
  // internal timer, longer than this 8s UI window by design, so a
  // retry THIS soon after is exactly the scenario that would catch a
  // guard that failed to release).
  const posCloseBtn = elCloseBtn();
  assert(!!posCloseBtn, 'never-responds: Posiciones still renders a Close button for the still-open position');
  assertEqual(posCloseBtn ? posCloseBtn.disabled : true, false, 'never-responds: the Close control is re-enabled (closeInFlight released), not stuck');

  // A canonical snapshot that arrives LATER (after the timeout already
  // reported "not confirmed") must still reconcile normally — whatever
  // it says becomes the real truth, with no special-casing needed.
  ws().readyState = 1;
  ws().emit({ type: 'positions', items: [] });
  await sleep(50);
  assert(!document.querySelector('.mbgc-pill-entry') && !document.querySelector('.mbgc-pill-entry-compact'), 'never-responds: a later canonical snapshot (A now gone) is still correctly reconciled after the timeout');
  assertEqual(sentCloseMsgs().length, 1, 'never-responds: the late snapshot itself never triggers any additional send either');
"""
        driver = _driver_wrap(body, "check_results")
        data = _run_driver_and_extract(self, html, driver, window_size="390,844", virtual_time_budget=10000, result_id="check_results")
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertEqual(failures, [], f"{data['failCount']} of {len(data['results'])} never-responds assertions failed: {failures}")
        self.assertGreaterEqual(data["passCount"], 8)
