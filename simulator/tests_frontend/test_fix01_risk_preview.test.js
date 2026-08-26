/*
 * simulator/tests_frontend/test_fix01_risk_preview.test.js
 *
 * FIX-01 — regression tests for the Risk Preview contract_size bug and its
 * fix (computeRiskLocal(qty) omitted contract_size; the panel now also
 * asks the backend's authoritative evaluate_position_risk() via the
 * debounced 'order:risk_preview' WS action, with a stale-response guard).
 *
 * Same convention as test_o6c1x_cross_panel_pnl.test.js: load the ACTUAL
 * source from dashboard.html verbatim (brace/statement-balanced
 * extraction, never reimplemented) into an isolated vm context, and
 * exercise real behavior — not string matches.
 *
 * Run with: node --test simulator/tests_frontend/
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_PATH = path.join(__dirname, '..', 'templates', 'simulator', 'dashboard.html');
const SRC = fs.readFileSync(DASHBOARD_PATH, 'utf8');

function extractBalancedBraces(content, startIdx) {
  const i = content.indexOf('{', startIdx);
  let depth = 0, j = i;
  while (j < content.length) {
    if (content[j] === '{') depth++;
    else if (content[j] === '}') {
      depth--;
      if (depth === 0) return content.slice(startIdx, j + 1);
    }
    j++;
  }
  throw new Error('unbalanced braces starting at ' + startIdx);
}

function extractFunction(content, marker) {
  const idx = content.indexOf(marker);
  if (idx === -1) throw new Error('marker not found: ' + marker);
  return content.slice(idx, idx + extractBalancedBraces(content, idx).length);
}

function extractStatement(content, marker) {
  return extractFunction(content, marker) + ';';
}

/* For one-liners with no `{}` (e.g. `new Set([...])`) — slice to the next `;`. */
function extractLine(content, marker) {
  const idx = content.indexOf(marker);
  if (idx === -1) throw new Error('marker not found: ' + marker);
  const end = content.indexOf(';', idx);
  if (end === -1) throw new Error('no terminating ";" for: ' + marker);
  return content.slice(idx, end + 1);
}

function coreRiskSource() {
  return [
    extractStatement(SRC, 'const CONTRACT_SIZE = {'),
    extractFunction(SRC, 'function getContractSize(sym)'),
    extractLine(SRC, "const _CRYPTO_SYMS_FE=new Set("),
    extractFunction(SRC, 'function computeRiskLocal(qty)'),
  ].join('\n');
}

function makeElement(id) {
  const listeners = {};
  return {
    id, value: '', style: {},
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type) { (listeners[type] || []).forEach((fn) => fn()); },
  };
}

/* Builds a fresh vm context with computeRiskLocal + friends, an
   `activePanel` the tests can mutate, a spy `updateRiskPanel`, and a
   minimal `document` exposing just #qty/#riskPanel (all this code path
   touches). */
function makeRiskSandbox({ activePanel = null, lastAccountMsg = null } = {}) {
  const updateRiskPanelCalls = [];
  const elements = { qty: makeElement('qty'), riskPanel: makeElement('riskPanel') };
  const sandbox = {
    window: { _lastAccountMsg: lastAccountMsg },
    activePanel,
    document: { getElementById: (id) => elements[id] || null },
    updateRiskPanel: (data) => updateRiskPanelCalls.push(data),
    WebSocket: { OPEN: 1 },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(coreRiskSource(), sandbox);
  return { sandbox, elements, updateRiskPanelCalls };
}

function panel(overrides) {
  return Object.assign({ currentSymbol: 'EUR/USD', lastClose: 1.17000 }, overrides);
}

function accountMsg(overrides) {
  return Object.assign({ equity: 10000, margin_used: 0, leverage: 50 }, overrides);
}

// ─────────────────────────────────────────────────────────────────────────
// 1. computeRiskLocal — contract_size fix
// ─────────────────────────────────────────────────────────────────────────

test('FIX-01 — computeRiskLocal applies contract_size for EUR/USD (was the bug: qty*price only)', () => {
  const p = panel({ currentSymbol: 'EUR/USD', lastClose: 1.17 });
  const { sandbox } = makeRiskSandbox({ activePanel: p, lastAccountMsg: accountMsg() });

  const result = sandbox.computeRiskLocal(0.01);
  assert.ok(result, 'must return a result for a valid qty');

  const contractSize = 100000; // mirrors CONTRACT_SIZE['EUR/USD']
  const expectedNotional = 0.01 * 1.17 * contractSize; // 1170
  const expectedMargin = expectedNotional / 50;          // 23.4
  const expectedExposurePct = (expectedNotional / 10000) * 100; // 11.7%

  assert.strictEqual(Number(result.margin_required), Number(expectedMargin.toFixed(2)));
  assert.strictEqual(Number(result.exposure_pct), Number(expectedExposurePct.toFixed(1)));

  // The bug this guards against: pre-fix notional was qty*price=0.0117,
  // i.e. margin_required would round to 0.00 instead of 23.40.
  assert.notStrictEqual(Number(result.margin_required), 0.00);
});

test('FIX-01 — computeRiskLocal leaves crypto (contract_size=1) unaffected', () => {
  const p = panel({ currentSymbol: 'BTCUSD', lastClose: 82000 });
  const { sandbox } = makeRiskSandbox({ activePanel: p, lastAccountMsg: accountMsg() });

  const result = sandbox.computeRiskLocal(0.01);
  const expectedNotional = 0.01 * 82000 * 1; // contract_size=1 → no-op multiplier
  const expectedMargin = expectedNotional / 50;

  assert.strictEqual(Number(result.margin_required), Number(expectedMargin.toFixed(2)));
});

// ─────────────────────────────────────────────────────────────────────────
// 2. _requestRiskPreview — debounced WS sender (class field on TradingPanel)
// ─────────────────────────────────────────────────────────────────────────

/* _requestRiskPreview is an arrow-function class field, so `this` is
   lexically bound to whatever created it. Wrapping the extracted
   right-hand side in a plain function and invoking it with .call(mockPanel)
   reproduces exactly how the constructor creates it per-instance, without
   reimplementing the debounce/send logic. */
function makeRequestRiskPreview(mockPanel) {
  const debounceSrc = extractStatement(SRC, 'const debounce=');
  const fieldMarker = '_requestRiskPreview=debounce(';
  const idx = SRC.indexOf(fieldMarker);
  if (idx === -1) throw new Error('marker not found: ' + fieldMarker);
  let depth = 0, i = SRC.indexOf('(', idx);
  const parenStart = i;
  for (; i < SRC.length; i++) {
    if (SRC[i] === '(') depth++;
    else if (SRC[i] === ')') { depth--; if (depth === 0) break; }
  }
  const rhs = SRC.slice(idx + '_requestRiskPreview='.length, i + 1);
  const source = `${debounceSrc}\nfunction __make(){ return ${rhs}; }\n__make;`;
  const sandbox = { WebSocket: { OPEN: 1 }, console, setTimeout, clearTimeout };
  vm.createContext(sandbox);
  const factory = vm.runInContext(source, sandbox);
  return factory.call(mockPanel);
}

function makeMockPanel(currentSymbol = 'EUR/USD') {
  return {
    currentSymbol,
    ws: { readyState: 1, sent: [], send(payload) { this.sent.push(JSON.parse(payload)); } },
  };
}

test('FIX-01 — _requestRiskPreview sends {action, symbol, qty} after the debounce window', async () => {
  const mockPanel = makeMockPanel('EUR/USD');
  const req = makeRequestRiskPreview(mockPanel);

  req(0.01);
  assert.strictEqual(mockPanel.ws.sent.length, 0, 'must not send synchronously — it is debounced');

  await new Promise((r) => setTimeout(r, 260));
  assert.strictEqual(mockPanel.ws.sent.length, 1);
  assert.deepStrictEqual(mockPanel.ws.sent[0], { action: 'order:risk_preview', symbol: 'EUR/USD', qty: 0.01 });
});

test('FIX-01 — _requestRiskPreview coalesces rapid calls into a single send for the latest qty', async () => {
  const mockPanel = makeMockPanel('EUR/USD');
  const req = makeRequestRiskPreview(mockPanel);

  req(0.01);
  req(0.05);
  req(1.00); // simulates fast typing: only this last one should reach the wire

  await new Promise((r) => setTimeout(r, 260));
  assert.strictEqual(mockPanel.ws.sent.length, 1, 'keystroke flood must not flood the socket');
  assert.strictEqual(mockPanel.ws.sent[0].qty, 1.00);
});

test('FIX-01 — _requestRiskPreview sends nothing when the socket is not OPEN', async () => {
  const mockPanel = makeMockPanel('EUR/USD');
  mockPanel.ws.readyState = 3; // CLOSED
  const req = makeRequestRiskPreview(mockPanel);

  req(0.01);
  await new Promise((r) => setTimeout(r, 260));
  assert.strictEqual(mockPanel.ws.sent.length, 0);
});

// ─────────────────────────────────────────────────────────────────────────
// 3. qty input listener — wires computeRiskLocal (instant) + _requestRiskPreview
// ─────────────────────────────────────────────────────────────────────────

function wireQtyListener(sandboxExtras) {
  const qtyListenerSrc = [
    extractLine(SRC, "const _qtyEl=document.getElementById('qty');"),
    extractFunction(SRC, 'if(_qtyEl){'),
  ].join('\n');

  const { sandbox, elements, updateRiskPanelCalls } = makeRiskSandbox(sandboxExtras);
  vm.runInContext(qtyListenerSrc, sandbox);
  return { sandbox, elements, updateRiskPanelCalls };
}

test('FIX-01 — qty input triggers both the instant local estimate and the authoritative request', () => {
  const p = panel({ currentSymbol: 'EUR/USD', lastClose: 1.17 });
  p._requestRiskPreview = (qty) => { p._requestedQty = qty; };
  const { elements, updateRiskPanelCalls } = wireQtyListener({ activePanel: p, lastAccountMsg: accountMsg() });

  elements.qty.value = '0.01';
  elements.qty.dispatch('input');

  assert.strictEqual(p._requestedQty, 0.01, 'activePanel._requestRiskPreview must be called with the typed qty');
  assert.strictEqual(updateRiskPanelCalls.length, 1, 'the corrected local estimate must render immediately');
  assert.strictEqual(Number(updateRiskPanelCalls[0].margin_required), 23.4);
});

test('FIX-01 — clearing qty hides the panel and requests nothing', () => {
  const p = panel({ currentSymbol: 'EUR/USD', lastClose: 1.17 });
  p._requestRiskPreview = () => { p._requestedQty = 'SHOULD_NOT_BE_CALLED'; };
  const { elements, updateRiskPanelCalls } = wireQtyListener({ activePanel: p, lastAccountMsg: accountMsg() });

  elements.qty.value = '0';
  elements.qty.dispatch('input');

  assert.strictEqual(elements.riskPanel.style.display, 'none');
  assert.strictEqual(updateRiskPanelCalls.length, 0);
  assert.strictEqual(p._requestedQty, undefined);
});

// ─────────────────────────────────────────────────────────────────────────
// 4. risk_preview response handler — stale-response guard
// ─────────────────────────────────────────────────────────────────────────

function makeHandlerSandbox({ activePanel, qtyElValue }) {
  const updateRiskPanelCalls = [];
  const elements = { qty: makeElement('qty') };
  elements.qty.value = qtyElValue;
  const sandbox = {
    activePanel,
    document: { getElementById: (id) => elements[id] || null },
    updateRiskPanel: (data) => updateRiskPanelCalls.push(data),
    console,
  };
  vm.createContext(sandbox);
  const source = 'function __handler(msg){\n' + extractFunction(SRC, "if(msg.type==='risk_preview'){") + '\n}\n__handler;';
  const handler = vm.runInContext(source, sandbox);
  return { handler, updateRiskPanelCalls };
}

test("FIX-01 — risk_preview updates the panel when active panel + symbol + qty all match", () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0.01' });

  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.01, margin_required: 23.4 });

  assert.strictEqual(updateRiskPanelCalls.length, 1);
});

test('FIX-01 — risk_preview is dropped when it arrives on a no-longer-active panel', () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  const otherActivePanel = panel({ currentSymbol: 'EUR/USD' });
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: otherActivePanel, qtyElValue: '0.01' });

  // response's own connection ('this') is p, but the user has since made a
  // different panel active — must be ignored.
  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.01 });

  assert.strictEqual(updateRiskPanelCalls.length, 0);
});

test('FIX-01 — risk_preview is dropped when the symbol changed since the request was sent', () => {
  const p = panel({ currentSymbol: 'GBP/USD' }); // user switched symbol while the EUR/USD request was in flight
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0.01' });

  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.01 });

  assert.strictEqual(updateRiskPanelCalls.length, 0);
});

test('FIX-01 — risk_preview is dropped when qty was cleared while the request was in flight', () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0' });

  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.01 });

  assert.strictEqual(updateRiskPanelCalls.length, 0);
});

// FIX-01 (correction pass) — the exact race flagged in review: a request
// goes out for qty=0.01, the user retypes qty=0.10 *before* that response
// arrives, and the stale qty=0.01 response must not paint the panel.
test('FIX-01 — a stale response for the previous qty is dropped even when panel+symbol still match', () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  // request sent for qty=0.01; input has since moved on to 0.10
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0.10' });

  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.01, margin_required: 23.4 });

  assert.strictEqual(updateRiskPanelCalls.length, 0, 'the qty=0.01 response no longer matches the qty=0.10 now in the input');
});

test('FIX-01 — the response for the CURRENT qty=0.10 is accepted once it lands, "0.10" input vs 0.1 msg.qty included', () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0.10' });

  // backend echoes back a plain float (0.1), not the string '0.10' — must
  // still match via numeric comparison, not a string diff.
  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD', qty: 0.1, margin_required: 234.0 });

  assert.strictEqual(updateRiskPanelCalls.length, 1);
  assert.strictEqual(updateRiskPanelCalls[0].margin_required, 234.0);
});

test('FIX-01 — risk_preview is dropped when msg.qty is missing/non-numeric (defensive)', () => {
  const p = panel({ currentSymbol: 'EUR/USD' });
  const { handler, updateRiskPanelCalls } = makeHandlerSandbox({ activePanel: p, qtyElValue: '0.10' });

  handler.call(p, { type: 'risk_preview', symbol: 'EUR/USD' }); // no qty field at all

  assert.strictEqual(updateRiskPanelCalls.length, 0);
});
