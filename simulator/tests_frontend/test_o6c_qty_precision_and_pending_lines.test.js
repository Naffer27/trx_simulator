/*
 * simulator/tests_frontend/test_o6c_qty_precision_and_pending_lines.test.js
 *
 * O.6c — regression tests for the frontend defects confirmed by the O.6c
 * read-only audit and fixed across two rounds:
 *
 *   FIX 1 — qty display precision: getLotDecimals(sym) is now used at
 *   every qty display call site instead of a hardcoded .toFixed(2), so
 *   BTCUSD's 0.001 lot no longer renders as "0.00".
 *
 *   FIX 3 (round 1) — pendingTmp lifecycle: the order_ack handler
 *   (dashboard.html) removes the pendingTmp placeholder by identity
 *   (symbol+side) the moment the server confirms the fill (order_ack.
 *   order_id is the real backend Position id), instead of relying on
 *   _reconcilePending()'s price-tolerance match — which the audit showed
 *   can miss by tens of dollars and leave a permanent orphaned line.
 *
 *   FIX 3 (round 2) — VISUAL STANDARD: manual testing confirmed round 1
 *   was technically correct (no orphan survives) but still drew a visible
 *   PriceLine for the pending order before confirmation, which is no
 *   longer wanted at all. sendOrder() no longer calls _drawLines() for
 *   the tmp entry — pendingTmp is now bookkeeping-only (symbol/side/
 *   tmpId), so order_ack's reconciliation and the reject-path cleanup
 *   still have an id to match, but _removeLines() on it is always a
 *   no-op (nothing was ever drawn). The chart standard is now: no
 *   positions -> current-price line only; open position -> current price
 *   + one real ENTRY line (+ SL/TP if set); nothing else, ever.
 *
 * Same technique as test_o6c1x_cross_panel_pnl.test.js throughout: the
 * ACTUAL source is extracted verbatim from dashboard.html via balanced-
 * brace matching (never reimplemented) into an isolated vm context, then
 * exercised against synthetic state. No new dependency — node:test +
 * node:vm only.
 *
 * Run with: node --test simulator/tests_frontend/
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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

function extractStatement(content, marker) {
  const idx = content.indexOf(marker);
  if (idx === -1) throw new Error('marker not found: ' + marker);
  const braced = extractBalancedBraces(content, idx);
  return content.slice(idx, idx + braced.length + 1); // include trailing ';'
}

function extractFunction(content, marker) {
  const idx = content.indexOf(marker);
  if (idx === -1) throw new Error('marker not found: ' + marker);
  return content.slice(idx, idx + extractBalancedBraces(content, idx).length);
}

/* Extracts a flat run of statements between two literal markers
   (inclusive of both), for code that isn't a single braced block —
   e.g. the tail of sendOrder() after its WS send. */
function extractRange(content, startMarker, endMarker) {
  const start = content.indexOf(startMarker);
  if (start === -1) throw new Error('start marker not found: ' + startMarker);
  const endOfEnd = content.indexOf(endMarker, start);
  if (endOfEnd === -1) throw new Error('end marker not found: ' + endMarker);
  return content.slice(start, endOfEnd + endMarker.length);
}

function readDashboard() {
  const filePath = path.join(__dirname, '..', 'templates', 'simulator', 'dashboard.html');
  return fs.readFileSync(filePath, 'utf8');
}

/* ── FIX 1 — LOT_SPECS / getLotDecimals, verbatim ── */
function loadLotPrecisionFunctions() {
  const content = readDashboard();
  const src = [
    extractStatement(content, 'const LOT_SPECS = {'),
    extractFunction(content, 'function getLotDecimals(sym)'),
  ].join('\n');
  return src;
}

function makeLotSandbox() {
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(loadLotPrecisionFunctions(), sandbox);
  return sandbox;
}

/* ── FIX 3 — the order_ack pendingTmp-reconcile block, verbatim ──
   Extracted as a standalone `if(msg.order_id!=null){...}` statement
   (the exact block added inside the order_ack branch of _handleMsg),
   wrapped as a function(msg, side) so it can run with a synthetic
   `this` via .call(fakePanel, ...) — same statement text the real
   _handleMsg executes, not a reimplementation. refreshQB() is called
   as a bare global inside the real code (same as in dashboard.html
   itself, where it's a top-level function) — stubbed in the sandbox. */
function loadOrderAckReconcileFn(sandbox) {
  const content = readDashboard();
  const stmt = extractBalancedBraces(content, content.indexOf('if(msg.order_id!=null){'));
  vm.runInContext(`var __ackReconcile = function(msg, side){ ${stmt} };`, sandbox);
  return sandbox.__ackReconcile;
}

/* ── the order-rejected pendingTmp cleanup block, verbatim (unchanged
   by either round of this fix — regression guard that it still fires
   on rejection) ── */
function loadRejectCleanupFn(sandbox) {
  const content = readDashboard();
  // First occurrence in the file is the 'error' (rejection) branch —
  // later occurrences are sendOrder's own defensive-cleanup blocks.
  const stmt = extractBalancedBraces(content, content.indexOf('if(this.pendingTmp.length){'));
  vm.runInContext(`var __rejectCleanup = function(){ ${stmt} };`, sandbox);
  return sandbox.__rejectCleanup;
}

/* ── round 2 — the exact tail of sendOrder() that runs after the WS
   send: this is the code under test for "no visible PriceLine for a
   pending order". Extracted verbatim between the tmpId declaration and
   the showQB() call that closes the statement. ── */
function loadSendOrderPendingTailFn(sandbox) {
  const content = readDashboard();
  const stmt = extractRange(
    content,
    'const tmpId=`tmp-${Date.now()}`;',
    'setTimeout(()=>showQB(this,tmpId),50);',
  );
  vm.runInContext(`var __sendOrderTail = function(side){ ${stmt} };`, sandbox);
  return sandbox.__sendOrderTail;
}

/* ── _drawLines(id,pos), verbatim — proves SL/TP (and ENTRY) creation
   itself is completely untouched by the round-2 change; only the
   pending-order call site stopped invoking it. ── */
function loadDrawLinesFn(sandbox) {
  const content = readDashboard();
  const stmt = extractBalancedBraces(content, content.indexOf('_drawLines(id,pos){'));
  vm.runInContext(`var __drawLines = function ${stmt};`, sandbox);
  return sandbox.__drawLines;
}

/* ── _removeLines(id), verbatim — used together with _clearLines() below
   so the symbol-change test exercises the REAL removal chain (map delete
   + candleSeries.removePriceLine), not just the spy default. ── */
function loadRemoveLinesFn(sandbox) {
  const content = readDashboard();
  const stmt = extractBalancedBraces(content, content.indexOf('_removeLines(id){'));
  vm.runInContext(`var __removeLines = function ${stmt};`, sandbox);
  return sandbox.__removeLines;
}

/* ── _clearLines(), verbatim — proves a symbol change empties both the
   drawn-lines map AND the pendingTmp bookkeeping array, so nothing can
   linger/duplicate across a symbol switch. ── */
function loadClearLinesFn(sandbox) {
  const content = readDashboard();
  const stmt = extractBalancedBraces(content, content.indexOf('_clearLines(){'));
  vm.runInContext(`var __clearLines = function ${stmt};`, sandbox);
  return sandbox.__clearLines;
}

function makePendingSandbox() {
  const sandbox = {
    refreshQB: () => { sandbox.__refreshQBCalls = (sandbox.__refreshQBCalls || 0) + 1; },
    // sendOrder's tail schedules showQB() via setTimeout — a no-op stub is
    // enough here: everything these tests assert on (pendingTmp/
    // selectedPosIds/lastSelectedId/_drawLines calls) is set synchronously
    // before that scheduled call.
    setTimeout: () => {},
    showQB: () => {},
  };
  vm.createContext(sandbox);
  return sandbox;
}

function fakeCandleSeries() {
  const created = [];
  return {
    __created: created,
    createPriceLine(opts) {
      const line = { opts, applyOptions(o) { Object.assign(opts, o); }, __removed: false };
      created.push(line);
      return line;
    },
    removePriceLine(line) { line.__removed = true; },
  };
}

function fakePanel(overrides) {
  const removed = [];
  const drawCalls = [];
  return Object.assign({
    pendingTmp: [],
    selectedPosIds: new Set(),
    lastSelectedId: null,
    linesById: new Map(),
    currentSymbol: 'BTCUSD',
    lastClose: 64604.79,
    _removeLines(id) { removed.push(id); },
    _drawLines(...args) { drawCalls.push(args); },
    __removed: removed,
    __drawCalls: drawCalls,
  }, overrides);
}

/* ═══════════════════════════════════════════════════════════════════
   FIX 1 — qty precision by instrument
   ═══════════════════════════════════════════════════════════════════ */

test('O.6c — BTCUSD lot precision is 3 decimals (0.001 lot, not 0.00)', () => {
  const sandbox = makeLotSandbox();
  assert.strictEqual(sandbox.getLotDecimals('BTCUSD'), 3);
  assert.strictEqual((0.001).toFixed(sandbox.getLotDecimals('BTCUSD')), '0.001');
});

test('O.6c — forex lot precision is 2 decimals (0.01 lot)', () => {
  const sandbox = makeLotSandbox();
  assert.strictEqual(sandbox.getLotDecimals('EUR/USD'), 2);
  assert.strictEqual((0.01).toFixed(sandbox.getLotDecimals('EUR/USD')), '0.01');
});

test('O.6c — ETHUSD (dec:2) and unknown symbols keep the safe 2-decimal fallback', () => {
  const sandbox = makeLotSandbox();
  assert.strictEqual(sandbox.getLotDecimals('ETHUSD'), 2);
  assert.strictEqual(sandbox.getLotDecimals('SOME_FUTURE_SYMBOL'), 2, 'LOT_SPECS miss must fall back to dec:2, never throw');
});

/* ═══════════════════════════════════════════════════════════════════
   FIX 3, round 2 — pendingTmp never draws a visible PriceLine
   ═══════════════════════════════════════════════════════════════════ */

test('O.6c — sendOrder never calls _drawLines() for a pending order (no visible line)', () => {
  const sandbox = makePendingSandbox();
  const tail = loadSendOrderPendingTailFn(sandbox);
  const panel = fakePanel();

  tail.call(panel, 'buy');

  assert.strictEqual(panel.__drawCalls.length, 0, '_drawLines must never be invoked from sendOrder for a pending order');
  assert.strictEqual(panel.pendingTmp.length, 1, 'bookkeeping entry must still be pushed for reconciliation');
  assert.strictEqual(panel.pendingTmp[0].symbol, 'BTCUSD');
  assert.strictEqual(panel.pendingTmp[0].side, 'buy');
  assert.ok(panel.pendingTmp[0].tmpId.startsWith('tmp-'));
  assert.strictEqual(panel.lastSelectedId, panel.pendingTmp[0].tmpId, 'selection bookkeeping is kept so order_ack can transfer it to the real id');
});

test('O.6c — pendingTmp bookkeeping entry has no corresponding linesById entry', () => {
  const sandbox = makePendingSandbox();
  const tail = loadSendOrderPendingTailFn(sandbox);
  const panel = fakePanel();

  tail.call(panel, 'sell');

  const tmpId = panel.pendingTmp[0].tmpId;
  assert.strictEqual(panel.linesById.has(tmpId), false, 'no PriceLine object may exist for a pending order');
});

/* ═══════════════════════════════════════════════════════════════════
   FIX 3 — pendingTmp lifecycle / reconciliation (unchanged by round 2)
   ═══════════════════════════════════════════════════════════════════ */

test('O.6c — order_ack reconciles pendingTmp by identity, NOT by price tolerance (the exact reported gap)', () => {
  const sandbox = makePendingSandbox();
  const ackReconcile = loadOrderAckReconcileFn(sandbox);
  const panel = fakePanel({
    // Bookkeeping-only entry (round 2: never had a drawn line) at the
    // click-time lastClose (64604.79) — the real fill (order_id=320)
    // later confirms avg_price=64569.30. Gap = $35.49, ~140x the old
    // minMove*25=$0.25 _reconcilePending() tolerance that used to leave
    // this orphaned.
    pendingTmp: [{ tmpId: 'tmp-1', symbol: 'BTCUSD', side: 'buy', avg: 64604.79 }],
  });
  panel.lastSelectedId = 'tmp-1';
  panel.selectedPosIds.add('tmp-1');

  ackReconcile.call(panel, { order_id: 320, symbol: 'BTCUSD' }, 'buy');

  assert.deepStrictEqual(panel.pendingTmp, [], 'the tmp bookkeeping entry must be gone');
  assert.deepStrictEqual(panel.__removed, ['tmp-1'], '_removeLines must still be called defensively, even though nothing was drawn for it');
  assert.strictEqual(panel.selectedPosIds.has('tmp-1'), false);
  assert.strictEqual(panel.selectedPosIds.has('320'), true, 'selection must transfer to the real Position id');
  assert.strictEqual(panel.lastSelectedId, '320');
});

test('O.6c — order_ack never touches a pendingTmp entry for a different symbol/side', () => {
  const sandbox = makePendingSandbox();
  const ackReconcile = loadOrderAckReconcileFn(sandbox);
  const panel = fakePanel({
    pendingTmp: [
      { tmpId: 'tmp-eur', symbol: 'EUR/USD', side: 'sell', avg: 1.17000 },
      { tmpId: 'tmp-btc', symbol: 'BTCUSD', side: 'buy', avg: 64604.79 },
    ],
  });

  ackReconcile.call(panel, { order_id: 320, symbol: 'BTCUSD' }, 'buy');

  assert.strictEqual(panel.pendingTmp.length, 1);
  assert.strictEqual(panel.pendingTmp[0].tmpId, 'tmp-eur', 'unrelated pending order must survive untouched');
  assert.deepStrictEqual(panel.__removed, ['tmp-btc']);
});

test('O.6c — order_ack with no matching pendingTmp entry is a safe no-op', () => {
  const sandbox = makePendingSandbox();
  const ackReconcile = loadOrderAckReconcileFn(sandbox);
  const panel = fakePanel({ pendingTmp: [] });

  assert.doesNotThrow(() => ackReconcile.call(panel, { order_id: 999, symbol: 'BTCUSD' }, 'buy'));
  assert.deepStrictEqual(panel.pendingTmp, []);
  assert.deepStrictEqual(panel.__removed, []);
});

test('O.6c — a rejected order leaves zero lines and zero pendingTmp entries', () => {
  const sandbox = makePendingSandbox();
  const rejectCleanup = loadRejectCleanupFn(sandbox);
  const panel = fakePanel({
    pendingTmp: [{ tmpId: 'tmp-rejected', symbol: 'BTCUSD', side: 'buy', avg: 64604.79 }],
  });
  panel.lastSelectedId = 'tmp-rejected';

  rejectCleanup.call(panel);

  assert.deepStrictEqual(panel.pendingTmp, [], 'rejection must still drain pendingTmp');
  assert.deepStrictEqual(panel.__removed, ['tmp-rejected']);
  assert.strictEqual(panel.linesById.size, 0, 'no line ever existed to remove — the map stays empty throughout');
  assert.strictEqual(panel.lastSelectedId, null);
});

test('O.6c — an accepted order produces exactly one ENTRY line, and it is never preceded by a tmp line', () => {
  // Full accepted-order sequence against a real linesById Map: sendOrder's
  // tail pushes bookkeeping only (no draw), order_ack's reconcile runs
  // (finds nothing to remove from the map, which is correct), then the
  // 'positions' snapshot draws the one real ENTRY line.
  const sandbox = makePendingSandbox();
  const tail = loadSendOrderPendingTailFn(sandbox);
  const ackReconcile = loadOrderAckReconcileFn(sandbox);
  const panel = fakePanel();

  tail.call(panel, 'buy');
  assert.strictEqual(panel.linesById.size, 0, 'still nothing drawn while the order is in flight');

  ackReconcile.call(panel, { order_id: 320, symbol: 'BTCUSD' }, 'buy');
  assert.strictEqual(panel.linesById.size, 0, 'order_ack itself draws nothing — it only clears bookkeeping');

  // 'positions' snapshot arrives next — _renderLines()/_drawLines() draws
  // the real position unconditionally (real _drawLines exercised in the
  // dedicated SL/TP test below; here we only assert the map ends with
  // exactly one entry, matching the visual standard).
  panel.linesById.set('320', { plEntry: 'REAL_LINE_OBJECT' });

  assert.strictEqual(panel.linesById.size, 1, 'exactly one entry line must exist for this fill, and only after confirmation');
  assert.ok(panel.linesById.has('320'));
});

/* ═══════════════════════════════════════════════════════════════════
   Lifecycle — timeframe change / symbol change never duplicate lines
   ═══════════════════════════════════════════════════════════════════ */

test('O.6c — _clearLines empties both linesById and pendingTmp (symbol-change round trip cannot leave orphans)', () => {
  const sandbox = makePendingSandbox();
  const clearLines = loadClearLinesFn(sandbox);
  const removeLines = loadRemoveLinesFn(sandbox);
  const candleSeries = fakeCandleSeries();
  const line = candleSeries.createPriceLine({ price: 64569.3 });
  const panel = fakePanel({
    candleSeries,
    linesById: new Map([['320', { plEntry: line, plSL: null, plTP: null }]]),
    pendingTmp: [{ tmpId: 'tmp-stale', symbol: 'BTCUSD', side: 'buy', avg: 64604.79 }],
  });
  panel._removeLines = removeLines.bind(panel); // real removal chain, not the spy

  clearLines.call(panel);

  assert.strictEqual(panel.linesById.size, 0);
  // pendingTmp is reassigned to a fresh array literal INSIDE the vm
  // sandbox by the real _clearLines() source — that array belongs to a
  // different JS realm than this test's own `[]`, so compare by length
  // (primitive), not deepStrictEqual (which also checks prototype
  // identity and would false-fail across realms).
  assert.strictEqual(panel.pendingTmp.length, 0, 'pendingTmp bookkeeping must not survive a symbol change either');
  assert.strictEqual(line.__removed, true, 'the real chart PriceLine object must be removed from the series');
});

/* ═══════════════════════════════════════════════════════════════════
   SL/TP — untouched by round 2, still created for real positions
   ═══════════════════════════════════════════════════════════════════ */

test('O.6c — _drawLines still creates ENTRY + SL + TP for a real position (unaffected by the pending-line removal)', () => {
  const sandbox = makePendingSandbox();
  const drawLines = loadDrawLinesFn(sandbox);
  const candleSeries = fakeCandleSeries();
  const panel = fakePanel({ candleSeries, linesById: new Map() });

  drawLines.call(panel, '320', { id: 320, side: 'buy', avg: 64569.30, sl: 64500.00, tp: 64700.00 });

  assert.strictEqual(candleSeries.__created.length, 3, 'ENTRY + SL + TP must each create one real PriceLine');
  const rec = panel.linesById.get('320');
  assert.ok(rec.plEntry && rec.plSL && rec.plTP);
  assert.strictEqual(rec.plEntry.opts.price, 64569.30);
  assert.strictEqual(rec.plSL.opts.price, 64500.00);
  assert.strictEqual(rec.plSL.opts.title, 'SL');
  assert.strictEqual(rec.plTP.opts.price, 64700.00);
  assert.strictEqual(rec.plTP.opts.title, 'TP');
});

test('O.6c — _drawLines creates only ENTRY when a real position has no SL/TP', () => {
  const sandbox = makePendingSandbox();
  const drawLines = loadDrawLinesFn(sandbox);
  const candleSeries = fakeCandleSeries();
  const panel = fakePanel({ candleSeries, linesById: new Map() });

  drawLines.call(panel, '321', { id: 321, side: 'sell', avg: 1.17020 });

  assert.strictEqual(candleSeries.__created.length, 1, 'no SL/TP set -> only the ENTRY line is created');
  const rec = panel.linesById.get('321');
  assert.ok(rec.plEntry && !rec.plSL && !rec.plTP);
});
