const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('simulator/static/simulator/desktop_v2/desktop-v2.js', 'utf8');
function fixture() {
  let select, writes = 0;
  const el = { dataset: {}, set innerHTML(html) { writes++; select = { dataset: {}, value: '', innerHTML: '' }; } };
  const calls = [];
  const ctx = { S: { sym: 'BTCUSD' }, D: { q: { BTCUSD: {}, EURUSD: {}, ETHUSD: {} }, quotes: ['BTCUSD', 'EURUSD', 'ETHUSD'].map(sym => ({sym})) },
    $: s => s === '#v2-symbol' ? el : select,
    esc: s => s, renderWatch: () => calls.push('watch'), renderToolbar: () => calls.push('toolbar'), buildCharts: () => calls.push('charts'), renderTicket: () => calls.push('ticket') };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('  function renderSymbol()'), source.indexOf('  function renderSeg(')), ctx);
  vm.runInContext(source.slice(source.indexOf('  function selectInstrument('), source.indexOf("  document.addEventListener('click'")), Object.assign(ctx, { document: { addEventListener: (type, fn) => { ctx.change = fn; } } }));
  return { ctx, calls, select: () => select, writes: () => writes };
}
test('selector lists available instruments and survives quote refreshes', () => {
  const f = fixture(); f.ctx.renderSymbol(); const first = f.select();
  for (const sym of ['BTCUSD', 'EURUSD', 'ETHUSD']) assert.ok(first.innerHTML.includes('value="' + sym + '"'));
  f.ctx.renderSymbol(); assert.equal(f.select(), first); assert.equal(f.writes(), 1);
});
test('changing selector updates chart and ticket without placing an order', () => {
  const f = fixture(); f.ctx.change({target: {id: 'v2-symbol-select', value: 'EURUSD'}});
  assert.equal(f.ctx.S.sym, 'EURUSD'); assert.deepEqual(f.calls, ['watch','toolbar','charts','ticket']);
  f.ctx.selectInstrument('INVALID'); assert.equal(f.ctx.S.sym, 'EURUSD'); assert.equal(f.calls.length, 4);
});
