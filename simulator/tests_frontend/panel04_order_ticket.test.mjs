// PANEL-04 — Order Ticket Safety & WebSocket UX — frontend behavior tests.
//
// There is no JS test framework or DOM engine (jsdom) in this repo, and
// dashboard.html is a single monolithic template (~4000 lines) that runs
// top-level DOM wiring at parse time and depends on an externally-loaded
// chart library (LightweightCharts). Booting the *entire* file in Node
// would require faithfully shimming all of that, which is disproportionate
// and would produce false confidence (a shim bug reads as a passing test).
//
// Instead, this harness extracts the *literal* source of the specific
// functions/methods under test directly out of dashboard.html (brace-
// matched, not retyped by hand) and executes it in a node:vm sandbox
// against a minimal DOM/WebSocket shim. This verifies the actual shipped
// code for the units that are self-contained enough to run in isolation.
//
// What this DOES cover (behavioral, real extracted source):
//   - SL/TP clear on symbol change (_onSymChange)
//   - SL/TP clear on active-panel change (setActive)
//   - Double-click sends exactly one payload (sendOrder + state machine)
//   - Button real-disabled during 'sending' (not just a CSS class)
//   - WS-not-open shows a visible message, no silent no-op, no send
//   - Exception in ws.send() releases the ticket state
//   - Timeout releases buttons, triggers get_positions, never resends
//   - Reconnect (onclose while our order was in flight) allows retry
//   - Qty label per symbol + lot step/min/decimals per symbol
//   - Cross-panel isolation: a message received by a DIFFERENT panel
//     never resolves this panel's in-flight ticket (audit finding, see
//     the panel-04 post-implementation audit)
//
// What this does NOT cover here (out of scope for a Node harness):
//   - Visual rendering / CSS / real browser event dispatch
//   - order:new SL/TP direction validation — already covered by Django
//     backend tests (simulator/tests/test_order_ticket_sl_tp_validation.py,
//     25/25 passing) since PANEL-04 makes that rejection server-side.
//   - PANEL-02 / PANEL-03 regressions — covered by the full Python suite
//     (2649/2649 passing), not JS-specific.
//
// Run: node simulator/tests_frontend/panel04_order_ticket.test.mjs

import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HTML_PATH = path.join(__dirname, '..', 'templates', 'simulator', 'dashboard.html');
const src = readFileSync(HTML_PATH, 'utf8');

// ── Brace-matched extraction (handles strings/template literals/comments
//    well enough for this codebase's style — no regex-over-braces hacks) ──
function extractBalanced(text, openBraceIdx) {
  let i = openBraceIdx, depth = 0;
  let inSingle = false, inDouble = false, inTemplate = false;
  let inLineComment = false, inBlockComment = false;
  for (; i < text.length; i++) {
    const c = text[i], prev = text[i - 1];
    if (inLineComment) { if (c === '\n') inLineComment = false; continue; }
    if (inBlockComment) { if (c === '/' && prev === '*') inBlockComment = false; continue; }
    if (inSingle) { if (c === "'" && prev !== '\\') inSingle = false; continue; }
    if (inDouble) { if (c === '"' && prev !== '\\') inDouble = false; continue; }
    if (inTemplate) {
      if (c === '`' && prev !== '\\') { inTemplate = false; continue; }
      if (c === '$' && text[i + 1] === '{') {
        let exprDepth = 1; i += 2;
        while (i < text.length && exprDepth > 0) {
          if (text[i] === '{') exprDepth++;
          else if (text[i] === '}') exprDepth--;
          if (exprDepth > 0) i++;
        }
        continue;
      }
      continue;
    }
    if (c === '/' && text[i + 1] === '/') { inLineComment = true; i++; continue; }
    if (c === '/' && text[i + 1] === '*') { inBlockComment = true; i++; continue; }
    if (c === "'") { inSingle = true; continue; }
    if (c === '"') { inDouble = true; continue; }
    if (c === '`') { inTemplate = true; continue; }
    if (c === '{') { depth++; continue; }
    if (c === '}') { depth--; if (depth === 0) return text.slice(openBraceIdx, i + 1); continue; }
  }
  throw new Error('extractBalanced: unbalanced braces');
}

function extractBlockStartingAt(marker) {
  const idx = src.indexOf(marker);
  assert.ok(idx !== -1, `marker not found in dashboard.html: ${marker}`);
  const braceIdx = src.indexOf('{', idx + marker.length - 1);
  return extractBalanced(src, braceIdx); // starts with '{', ends with matching '}'
}

// ── Extract the literal source of the units under test ──
const onSymChangeBody = extractBlockStartingAt('_onSymChange(){');
const sendOrderBody = extractBlockStartingAt('sendOrder(side,qty,slV,tpV,riskConfirmed=false){');
const onCloseBody = extractBlockStartingAt('this.ws.onclose=()=>{');
const setActiveBody = extractBlockStartingAt('function setActive(panel){');
const getQtyLabelBody = extractBlockStartingAt('function getQtyLabel(sym){');
const handleMsgBody = extractBlockStartingAt('_handleMsg(msg){');

const orderTicketSrc = (() => {
  const start = src.indexOf("let orderTicketState='idle';");
  assert.ok(start !== -1, 'orderTicketState declaration not found');
  const fnStart = src.indexOf('function _onOrderTicketTimeout(){');
  const fnBody = extractBlockStartingAt('function _onOrderTicketTimeout(){');
  const fnEnd = src.indexOf(fnBody, fnStart) + fnBody.length;
  return src.slice(start, fnEnd);
})();

const lotSpecsSrc = (() => {
  const objStart = src.indexOf('const LOT_SPECS = {');
  assert.ok(objStart !== -1, 'LOT_SPECS not found');
  const objBody = extractBalanced(src, src.indexOf('{', objStart));
  const objDecl = `const LOT_SPECS = ${objBody};`;
  const stepBody = extractBlockStartingAt('function getLotStep(sym)    {');
  const minBody = extractBlockStartingAt('function getLotMin(sym)     {');
  const decBody = extractBlockStartingAt('function getLotDecimals(sym){');
  return [
    objDecl,
    `function getLotStep(sym)${stepBody}`,
    `function getLotMin(sym)${minBody}`,
    `function getLotDecimals(sym)${decBody}`,
  ].join('\n');
})();

// ── Minimal DOM shims ──────────────────────────────────────────────────
function makeButton(id) {
  return { id, disabled: false, classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } } };
}
function makeInput(id, value = '') { return { id, value }; }

function runInFreshSandbox(code, extraGlobals = {}) {
  const sandbox = {
    console,
    setTimeout, clearTimeout,
    ...extraGlobals,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return sandbox;
}

// ── TEST GROUP 1: order ticket state machine (module-level, pure DOM) ──
function runOrderTicketStateMachineTests() {
  const els = {
    btnSell: makeButton('btnSell'), btnBuy: makeButton('btnBuy'),
    btnTopSell: makeButton('btnTopSell'), btnTopBuy: makeButton('btnTopBuy'),
  };
  const toasts = [];
  const sandbox = runInFreshSandbox(`
    ${orderTicketSrc}
    globalThis.__api = {
      setSending: _setOrderTicketSending,
      resolve: _resolveOrderTicket,
      onTimeout: _onOrderTicketTimeout,
      state: () => orderTicketState,
      timer: () => orderTicketTimer,
    };
  `, {
    document: { getElementById: (id) => els[id] || null },
    execToast: (type, text, sub) => toasts.push({ type, text, sub }),
    WebSocket: { OPEN: 1 },
  });
  const api = sandbox.__api;

  // botón disabled durante sending (real attribute, not just CSS class)
  api.setSending({ ws: { readyState: 1 } });
  assert.equal(api.state(), 'sending');
  for (const b of Object.values(els)) {
    assert.equal(b.disabled, true, `${b.id} must be really disabled while sending`);
    assert.ok(b.classList.contains('btn-sending'));
  }

  // resolve -> idle re-enables everything
  api.resolve();
  assert.equal(api.state(), 'idle');
  for (const b of Object.values(els)) {
    assert.equal(b.disabled, false);
    assert.ok(!b.classList.contains('btn-sending'));
  }

  // timeout: releases buttons, refreshes positions, does not resend order
  const sentPayloads = [];
  const fakePanel = { ws: { readyState: 1, send: (p) => sentPayloads.push(JSON.parse(p)) } };
  api.setSending(fakePanel);
  assert.equal(api.state(), 'sending');
  clearTimeout(api.timer());
  api.onTimeout();
  assert.equal(api.state(), 'idle', 'timeout must release the ticket back to idle');
  assert.equal(els.btnBuy.disabled, false, 'timeout must re-enable buttons');
  assert.equal(sentPayloads.length, 1, 'timeout must trigger exactly one get_positions refresh');
  assert.equal(sentPayloads[0].action, 'get_positions');
  assert.ok(!sentPayloads.some(p => p.action === 'order:new'), 'timeout must never auto-resend the order');

  // a stale timer firing after the ticket is already idle must be a no-op
  api.onTimeout();
  assert.equal(api.state(), 'idle');

  console.log('OK  order ticket state machine (sending/disabled/timeout/idle)');
}

// ── TEST GROUP 2: sendOrder — double-click, WS-closed, send-exception, reconnect ──
function runSendOrderTests() {
  const els = {
    btnSell: makeButton('btnSell'), btnBuy: makeButton('btnBuy'),
    btnTopSell: makeButton('btnTopSell'), btnTopBuy: makeButton('btnTopBuy'),
    ordType: makeInput('ordType', 'market'),
  };
  const toasts = [];
  const sandbox = runInFreshSandbox(`
    ${orderTicketSrc}
    function sendOrder(side,qty,slV,tpV,riskConfirmed=false)${sendOrderBody}
    function onclose()${onCloseBody}
    globalThis.__api = {
      sendOrder, onclose,
      resolve: _resolveOrderTicket,
      state: () => orderTicketState,
    };
  `, {
    document: { getElementById: (id) => els[id] || null },
    execToast: (type, text, sub) => toasts.push({ type, text, sub }),
    navigator: { vibrate: () => {} },
    showQB: () => {},
    WebSocket: { OPEN: 1 },
    clearInterval,
  });
  const api = sandbox.__api;

  function makePanel({ wsOpen = true, throwOnSend = false } = {}) {
    return {
      ws: {
        readyState: wsOpen ? 1 : 3,
        send: (p) => { if (throwOnSend) throw new Error('boom'); sentPayloads.push(JSON.parse(p)); },
      },
      currentSymbol: 'EUR/USD', lastClose: 1.1,
      setStatus: () => {},
      _drawLines: () => {}, _applyLineStyles: () => {},
      selectedPosIds: { clear() {}, add() {} },
      pendingTmp: [],
      hb: null, reconnTimer: null, reconnDelay: 800,
      connect: () => {},
    };
  }

  // 1) Double-click: two rapid calls must send exactly one payload.
  let sentPayloads = [];
  toasts.length = 0;
  const panel1 = makePanel();
  api.sendOrder.call(panel1, 'buy', 0.01, null, null);
  api.sendOrder.call(panel1, 'buy', 0.01, null, null); // second click while 'sending'
  assert.equal(sentPayloads.length, 1, 'a second rapid click must not send a second payload');
  assert.equal(api.state(), 'sending');
  assert.equal(els.btnBuy.disabled, true);
  api.resolve();
  console.log('OK  double-click sends exactly one payload, button real-disabled while sending');

  // 2) WS not open: visible message, no silent no-op, ws.send never called.
  sentPayloads = [];
  toasts.length = 0;
  const panel2 = makePanel({ wsOpen: false });
  api.sendOrder.call(panel2, 'sell', 0.01, null, null);
  assert.equal(sentPayloads.length, 0, 'must not attempt to send when WS is not OPEN');
  assert.equal(api.state(), 'idle', 'ticket must stay idle — nothing was actually sent');
  assert.ok(toasts.length >= 1, 'closed socket must show a visible message, not a silent no-op');
  assert.notEqual(toasts[0].type, 'send', 'must not claim an order is being sent when it was not');
  console.log('OK  WS-not-open shows a visible error and never calls ws.send');

  // 3) Exception in ws.send(): must release the ticket state.
  sentPayloads = [];
  toasts.length = 0;
  const panel3 = makePanel({ throwOnSend: true });
  api.sendOrder.call(panel3, 'buy', 0.01, null, null);
  assert.equal(api.state(), 'idle', 'a thrown ws.send() must release the ticket back to idle');
  assert.equal(els.btnBuy.disabled, false, 'buttons must be re-enabled after a send exception');
  console.log('OK  exception in ws.send() releases the order-ticket state');

  // 4) Reconnect: onclose while our order was in flight resolves the ticket,
  //    so the very next click is allowed to send again.
  sentPayloads = [];
  toasts.length = 0;
  const panel4 = makePanel();
  api.sendOrder.call(panel4, 'buy', 0.01, null, null);
  assert.equal(api.state(), 'sending');
  api.onclose.call(panel4);
  assert.equal(api.state(), 'idle', 'a socket drop mid-flight must release the ticket');
  sentPayloads = [];
  api.sendOrder.call(panel4, 'buy', 0.01, null, null);
  assert.equal(sentPayloads.length, 1, 'after reconnect the ticket must accept a new order');
  console.log('OK  reconnect (onclose while sending) releases the ticket and allows retry');
}

// ── TEST GROUP 3: SL/TP never carries over across symbol/panel switches ──
function runSlTpClearTests() {
  // 3a — changing symbol on the active panel must clear #sl/#tp
  {
    const els = { qty: makeInput('qty', '0.01'), sl: makeInput('sl', '1.2345'), tp: makeInput('tp', '1.2999') };
    const fakePanel = {
      symSel: { value: 'EUR/USD' }, hdrSym: { textContent: '' }, symTag: { textContent: '' },
      candleSeries: null, currentSymbol: 'BTCUSD',
      _clearLines() {}, selectedPosIds: { clear() {} }, lastSelectedId: null, _bars: [],
      _clearOscillatorData() {}, lastClose: 1, prevClose: 1, prevCandleClose: 1, bid: 1, ask: 1,
      _resetAgg() {}, mobileLot: 0, ws: { readyState: 0, send: () => {} }, _loadHistory: () => {},
    };
    const sandbox = runInFreshSandbox(`
      function _onSymChange()${onSymChangeBody}
      globalThis.__api = { onSymChange: _onSymChange };
    `, {
      document: { getElementById: (id) => els[id] || null },
      isMobile: () => false,
      updatePanelTabs: () => {},
      hideQB: () => {},
      _updateQtyInputAttrs: () => {},
      priceFormatFor: () => ({}),
      getLotMin: () => 0.01,
      activePanel: fakePanel,
      WebSocket: { OPEN: 1 },
    });
    sandbox.__api.onSymChange.call(fakePanel);
    assert.equal(els.sl.value, '', 'SL must be cleared when the active panel changes symbol');
    assert.equal(els.tp.value, '', 'TP must be cleared when the active panel changes symbol');
    console.log('OK  changing symbol (e.g. BTCUSD -> EUR/USD) on the active panel clears SL/TP');
  }

  // 3b — switching which panel is active must clear #sl/#tp
  {
    const els = { sl: makeInput('sl', '1.2345'), tp: makeInput('tp', '1.2999'), mhdrSymSel: makeInput('mhdrSymSel', '') };
    const prevPanel = { wrapper: { classList: { remove() {} } } };
    const nextPanel = {
      wrapper: { classList: { add() {} } }, statusEl: { textContent: '✅ connected' },
      _updateBidAsk() {}, lastClose: null, currentSymbol: 'BTCUSD', currentTF: '15m', mobileLot: 0.01,
    };
    const sandbox = runInFreshSandbox(`
      let activePanel = __prevPanel;
      function setActive(panel)${setActiveBody}
      globalThis.__api = { setActive, getActivePanel: () => activePanel };
    `, {
      document: { getElementById: (id) => els[id] || null },
      __prevPanel: prevPanel,
      _syncMhdrTFBadge: () => {},
      _updateQtyInputAttrs: () => {},
      getLotDecimals: () => 2,
      getLotMin: () => 0.01,
      isMobile: () => false,
      updateGlobalWSStatus: () => {},
      statusEl: null,
    });
    sandbox.__api.setActive(nextPanel);
    assert.equal(els.sl.value, '', 'SL must be cleared when switching which panel the ticket applies to');
    assert.equal(els.tp.value, '', 'TP must be cleared when switching which panel the ticket applies to');
    assert.equal(sandbox.__api.getActivePanel(), nextPanel);
    console.log('OK  switching active panel clears SL/TP');
  }
}

// ── TEST GROUP 3b (audit finding): a message on a DIFFERENT panel must
//    never resolve another panel's in-flight order ticket ──────────────
function runCrossPanelIsolationTests() {
  const toasts = [];
  const removedLinesFor = { A: [], B: [] };
  function makePanel(label) {
    return {
      label,
      pendingTmp: [{ tmpId: `tmp-${label}` }],
      selectedPosIds: { delete() {}, add() {}, clear() {} },
      lastSelectedId: null,
      _removeLines: (id) => removedLinesFor[label].push(id),
      currentSymbol: 'EUR/USD', lastClose: 1.1,
    };
  }
  const panelA = makePanel('A');
  const panelB = makePanel('B');

  const sandbox = runInFreshSandbox(`
    ${orderTicketSrc}
    function _handleMsg(msg)${handleMsgBody}
    globalThis.__api = {
      handleMsg: _handleMsg,
      setSending: _setOrderTicketSending,
      state: () => orderTicketState,
      panel: () => orderTicketPanel,
    };
  `, {
    document: { getElementById: () => null },
    execToast: (type, text, sub) => toasts.push({ type, text, sub }),
    refreshQB: () => {},
    _showRiskConfirmModal: () => {},
    showRiskAlert: () => {},
    priceFormatFor: () => ({ precision: 2 }),
    WebSocket: { OPEN: 1 },
  });
  const api = sandbox.__api;

  // panelA owns the in-flight ticket.
  api.setSending(panelA);
  assert.equal(api.state(), 'sending');
  assert.equal(api.panel(), panelA);

  // A message arriving on panelB — unrelated to panelA's pending order —
  // must not resolve panelA's ticket, for each of the 4 resolving types.
  api.handleMsg.call(panelB, { type: 'error', code: 'invalid_symbol', message: 'x' });
  assert.equal(api.state(), 'sending', "panel B's error must not resolve panel A's ticket");
  assert.equal(api.panel(), panelA);
  assert.deepEqual(removedLinesFor.B, ['tmp-B'], "panel B's own pendingTmp cleanup must still run locally");

  api.handleMsg.call(panelB, { type: 'risk_warning', exposure_pct: 10, pending_side: 'buy', pending_qty: 0.01, pending_symbol: 'EUR/USD' });
  assert.equal(api.state(), 'sending', "panel B's risk_warning must not resolve panel A's ticket");

  api.handleMsg.call(panelB, { type: 'order_rejected', code: 'extreme_risk', exposure_pct: 50 });
  assert.equal(api.state(), 'sending', "panel B's order_rejected must not resolve panel A's ticket");

  api.handleMsg.call(panelB, { type: 'order_ack', side: 'buy' });
  assert.equal(api.state(), 'sending', "panel B's order_ack must not resolve panel A's ticket");

  // panelA's own order_ack DOES resolve its own ticket.
  api.handleMsg.call(panelA, { type: 'order_ack', side: 'buy' });
  assert.equal(api.state(), 'idle', "panel A's own order_ack must resolve its own ticket");

  console.log('OK  a message on another panel never resolves this panel\'s in-flight ticket (audit fix)');
}

// ── TEST GROUP 4: qty label + lot specs per symbol ─────────────────────
function runQtyLabelAndLotSpecTests() {
  const sandbox = runInFreshSandbox(`
    ${lotSpecsSrc}
    function getQtyLabel(sym)${getQtyLabelBody}
    globalThis.__api = { getLotStep, getLotMin, getLotDecimals, getQtyLabel };
  `);
  const api = sandbox.__api;

  assert.equal(api.getQtyLabel('BTCUSD'), 'Volume (BTC)');
  assert.equal(api.getQtyLabel('ETHUSD'), 'Volume (ETH)');
  assert.equal(api.getQtyLabel('EUR/USD'), 'Volume (Lots)');
  assert.equal(api.getQtyLabel('GBP/USD'), 'Volume (Lots)');

  assert.equal(api.getLotStep('BTCUSD'), 0.001);
  assert.equal(api.getLotMin('BTCUSD'), 0.001);
  assert.equal(api.getLotDecimals('BTCUSD'), 3);
  assert.equal(api.getLotStep('EUR/USD'), 0.01);
  assert.equal(api.getLotMin('EUR/USD'), 0.01);
  assert.equal(api.getLotDecimals('EUR/USD'), 2);
  assert.equal(api.getLotStep('US30'), 0.1);
  console.log('OK  qty label and lot step/min/decimals are correct per symbol');
}

// ── run ──
runOrderTicketStateMachineTests();
runSendOrderTests();
runSlTpClearTests();
runCrossPanelIsolationTests();
runQtyLabelAndLotSpecTests();
console.log('\nAll PANEL-04 frontend behavior tests passed.');
