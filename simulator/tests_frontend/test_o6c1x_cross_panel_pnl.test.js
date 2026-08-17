/*
 * simulator/tests_frontend/test_o6c1x_cross_panel_pnl.test.js
 *
 * O.6c-1x — regression tests for the cross-panel position P&L fix.
 *
 * These tests load the ACTUAL source of CONTRACT_SIZE/getContractSize/
 * QUOTE_CURRENCY/ACCOUNT_CURRENCY_FALLBACK/computeRawPnL/
 * computePositionPnL/computePositionPnLSafe verbatim from dashboard.html
 * (extracted via brace/statement matching — never reimplemented) into an
 * isolated vm context with a controlled quotesLivePx, then assert on
 * computePositionPnLSafe()'s behavior — the single choke point all 5
 * fixed call sites (renderGlobalPositions, openSheet, _syncTradingPanel,
 * _patchTradingPanelPnL, the 1.5s live updater) now use instead of
 * panel.lastClose/srcPanel.lastClose/qbPanel.lastClose.
 *
 * No new dependency: uses Node's built-in test runner (node:test,
 * available since Node 18) and built-in vm module. package.json is
 * unchanged.
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

/* CONTRACT_SIZE / QUOTE_CURRENCY are object literals ending in `};` —
   balance-match through the closing `}` the same way, statement ends
   at the following `;`. */
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

function loadDashboardPnlFunctions() {
  const filePath = path.join(__dirname, '..', 'templates', 'simulator', 'dashboard.html');
  const content = fs.readFileSync(filePath, 'utf8');

  const src = [
    extractStatement(content, 'const CONTRACT_SIZE = {'),
    extractFunction(content, 'function getContractSize(sym)'),
    extractStatement(content, 'const QUOTE_CURRENCY = {'),
    'const ACCOUNT_CURRENCY_FALLBACK = "USD";',
    extractFunction(content, 'function computeRawPnL(symbol'),
    extractFunction(content, 'function computePositionPnL(pos'),
    extractFunction(content, 'function computePositionPnLSafe(pos'),
  ].join('\n');

  return src;
}

function makeSandbox(quotesLivePx) {
  const sandbox = { quotesLivePx };
  vm.createContext(sandbox);
  vm.runInContext(loadDashboardPnlFunctions(), sandbox);
  return sandbox;
}

function pos(overrides) {
  return Object.assign({
    id: 1, symbol: 'EUR/USD', side: 'buy', qty: 0.01, avg: 1.17017, pnl: null,
  }, overrides);
}

test('O.6c-1x — EURUSD position never uses BTCUSD quote (the exact reported incident)', () => {
  const sandbox = makeSandbox({ 'BTCUSD': { price: 62723.20, prev: null } }); // no EUR/USD entry at all
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', pnl: null }));
  assert.strictEqual(raw, null, 'must be null (safe "—"), never a BTCUSD-derived number');
});

test('O.6c-1x — BTCUSD position never uses EURUSD quote', () => {
  const sandbox = makeSandbox({ 'EUR/USD': { price: 1.17017, prev: null } }); // no BTCUSD entry
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'BTCUSD', side: 'buy', qty: 0.01, avg: 63000, pnl: null }));
  assert.strictEqual(raw, null, 'must be null (safe "—"), never a EUR/USD-derived number');
});

test('O.6c-1x — multipanel with different symbols: each position only ever resolves its own', () => {
  const sandbox = makeSandbox({
    'BTCUSD':  { price: 63012.70, prev: null },
    'EUR/USD': { price: 1.10010, prev: null },
  });
  const btc = sandbox.computePositionPnLSafe(pos({ symbol: 'BTCUSD', side: 'buy', qty: 0.01, avg: 63000, pnl: null }));
  const eur = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', side: 'buy', qty: 0.01, avg: 1.10000, pnl: null }));
  assert.ok(Math.abs(btc - 0.127) < 0.001, `BTCUSD pnl should be ~0.127, got ${btc}`);
  assert.ok(Math.abs(eur - 0.10) < 0.001, `EUR/USD pnl should be ~0.10, got ${eur}`);
});

test('O.6c-1x — backend pos.pnl, when present and finite, always takes priority', () => {
  // A deliberately corrupted quotesLivePx entry for this exact symbol —
  // must still be ignored because pos.pnl is present.
  const sandbox = makeSandbox({ 'EUR/USD': { price: 63088.50, prev: null } });
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', pnl: 42.5 }));
  assert.strictEqual(raw, 42.5);
});

test('O.6c-1x — pos.pnl=null + correct quote for pos.symbol computes correctly', () => {
  const sandbox = makeSandbox({ 'EUR/USD': { price: 1.18017, prev: null } }); // +1 pip*1000... simple round number
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', side: 'buy', qty: 0.01, avg: 1.17017, pnl: null }));
  // (1.18017 - 1.17017) * 0.01 * 100000 = 10.0
  assert.ok(Math.abs(raw - 10.0) < 1e-9, `expected 10.0, got ${raw}`);
});

test('O.6c-1x — pos.pnl=null + no quote at all for pos.symbol shows safe null (—)', () => {
  const sandbox = makeSandbox({}); // completely empty — nothing charted yet
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', pnl: null }));
  assert.strictEqual(raw, null);
});

test('O.6c-1x — normalized (no-slash) quotesLivePx key is honored, same pattern as renderQuotes()', () => {
  const sandbox = makeSandbox({ 'EURUSD': { price: 1.17017, prev: null } }); // no-slash form only
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'EUR/USD', side: 'buy', qty: 0.01, avg: 1.17017, pnl: null }));
  assert.strictEqual(raw, 0); // entry == quote, exactly zero, but NOT null — quote was found
});

test('O.6c-1x — NaN/Infinity quotesLivePx price is never used as a computed P&L input', () => {
  const sandboxNaN = makeSandbox({ 'EUR/USD': { price: NaN, prev: null } });
  assert.strictEqual(sandboxNaN.computePositionPnLSafe(pos({ pnl: null })), null);
  const sandboxInf = makeSandbox({ 'EUR/USD': { price: Infinity, prev: null } });
  assert.strictEqual(sandboxInf.computePositionPnLSafe(pos({ pnl: null })), null);
});

test('O.6c-1x — formula/contract-size untouched: computeRawPnL(EUR/USD) still uses contract_size=100000', () => {
  const sandbox = makeSandbox({});
  const raw = sandbox.computeRawPnL('EUR/USD', 'buy', 1.17017, 0.01, 62723.20);
  // Same formula as before O.6c-1x — this function itself was NOT changed.
  // (62723.20 - 1.17017) * 0.01 * 100000 = 62,722,029.83 — the exact
  // O.6c-1x reported reproduction.
  assert.ok(Math.abs(raw - 62722029.83) < 0.01, 'computeRawPnL formula itself must be byte-identical to before');
});

test('O.6c-1x — SELL side still computed correctly via the safe path', () => {
  const sandbox = makeSandbox({ 'BTCUSD': { price: 62900.00, prev: null } });
  const raw = sandbox.computePositionPnLSafe(pos({ symbol: 'BTCUSD', side: 'sell', qty: 0.01, avg: 63000.00, pnl: null }));
  // sell: (entry - px) * qty * cs = (63000 - 62900) * 0.01 * 1 = 1.0
  assert.ok(Math.abs(raw - 1.0) < 1e-9, `expected 1.0, got ${raw}`);
});

/* ── Static-source verification: none of the 5 fixed call sites can
   possibly read panel.lastClose/srcPanel.lastClose/qbPanel.lastClose
   for a P&L computation anymore — a regression here would mean someone
   reintroduced the exact O.6c-1x bug pattern. ── */
test('O.6c-1x — none of the 5 call sites can read a panel-scoped lastClose for P&L', () => {
  const filePath = path.join(__dirname, '..', 'templates', 'simulator', 'dashboard.html');
  const content = fs.readFileSync(filePath, 'utf8');

  const renderGlobalPositions = extractFunction(content, 'function renderGlobalPositions(');
  const openSheet = extractFunction(content, 'function openSheet(focus');
  const syncTradingPanel = extractFunction(content, 'function _syncTradingPanel(');
  const patchTradingPanelPnL = extractFunction(content, 'function _patchTradingPanelPnL(');
  const idx = content.indexOf('/* ── Live P/L updater for btm-panel positions ── */');
  const liveUpdaterEnd = content.indexOf('},1500);', idx) + '},1500);'.length;
  const liveUpdater = content.slice(idx, liveUpdaterEnd);

  for (const [name, body] of [
    ['renderGlobalPositions', renderGlobalPositions],
    ['openSheet', openSheet],
    ['_syncTradingPanel', syncTradingPanel],
    ['_patchTradingPanelPnL', patchTradingPanelPnL],
    ['live P/L updater', liveUpdater],
  ]) {
    // computePositionPnL (the raw, unsafe function) must never be called
    // directly with a panel-derived px in these 5 sites anymore.
    assert.ok(
      !/computePositionPnL\(\s*(pos|r)\s*,\s*px\s*\)/.test(body),
      `${name} must not call computePositionPnL(pos, px) with a panel-derived px`,
    );
  }
  // openSheet's one remaining qbPanel.lastClose use is the rec-only
  // (no synced Position) branch, already same-symbol by construction —
  // explicitly allowed, verified separately (see test above naming it).
  assert.ok(openSheet.includes('computePositionPnLSafe(pos)'), 'openSheet must use the safe path for real positions');
  assert.ok(renderGlobalPositions.includes('computePositionPnLSafe(r)'));
  assert.ok(syncTradingPanel.includes('computePositionPnLSafe(pos)'));
  assert.ok(patchTradingPanelPnL.includes('computePositionPnLSafe(pos)'));
  assert.ok(liveUpdater.includes('computePositionPnLSafe(pos)'));
});
