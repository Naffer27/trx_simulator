// =====================================================
// path: static/js/main.js
// =====================================================
import { buildWsUrl, connectWS } from './ws.js';
import { initChart } from './chart.js';
import { setupOrders } from './orders.js';

const $ = (s,sc=document)=>sc.querySelector(s);
const n = (v)=> (v===null||v===undefined||v==='')?null:Number(v);

const els = {
  chart: $('#chart'), board: $('#board'), posTbody: $('#posTable tbody'),
  hdrSymbol: $('#hdrSymbol'), hdrTF: $('#hdrTF'), symTag: $('#symTag'), pxTag: $('#pxTag'),
  providerSel: $('#provider'), tokenInput: $('#fhToken'),
  selSymbol: $('#symbol'), selTF: $('#tf'),
  pxBuy: $('#pxBuy'), pxSell: $('#pxSell'), status: $('#status')
};

const state = {
  provider: (window.APP_CONFIG?.providerDefault) || 'sim',
  symbol: els.selSymbol?.value || 'BTCUSD',
  tf: els.selTF?.value || '1m',
  bid:null, ask:null, lastClose:null, prevClose:null, spread:0
};

function setStatus(t){ els.status && (els.status.textContent=t); }
function updateHeaders(){
  els.hdrSymbol && (els.hdrSymbol.textContent=state.symbol.replace('/',''));
  els.hdrTF && (els.hdrTF.textContent=state.tf);
  els.symTag && (els.symTag.textContent=state.symbol.replace('/',''));
}
function recomputeFallbackSpread(){
  const isCripto = (state.symbol.includes('BTC')||state.symbol.includes('ETH'));
  const isJPY = state.symbol.endsWith('/JPY');
  state.spread = isCripto ? 0.3 : (isJPY ? 0.004 : 0.0002);
}
function updateBidAsk(){
  const p = (state.symbol.includes('BTC')||state.symbol.includes('ETH'))?2:(state.symbol.endsWith('/JPY')?3:5);
  const hasTick = state.bid!=null && state.ask!=null && state.ask>state.bid;
  const mid = state.lastClose ?? ((state.ask!=null && state.bid!=null)? (state.ask+state.bid)/2 : null);
  const s = hasTick ? (state.ask-state.bid) : (state.spread||0);
  const b = hasTick ? state.bid : (mid!=null? mid - s/2 : null);
  const a = hasTick ? state.ask : (mid!=null? mid + s/2 : null);
  els.pxSell && (els.pxSell.textContent = (b!=null)? b.toFixed(p) : '—');
  els.pxBuy  && (els.pxBuy.textContent  = (a!=null)? a.toFixed(p) : '—');
}

/* ====== Chart ====== */
const chart = initChart({
  containerEl: els.chart,
  symbol: state.symbol,
  onLineChange: (id, {sl,tp})=>{ ws.send({ action:"order:update", id, symbol:state.symbol, sl, tp }); }
});
function setPrice(px, prev){
  const p=(state.symbol.includes('BTC')||state.symbol.includes('ETH'))?2:(state.symbol.endsWith('/JPY')?3:5);
  chart.setPrice(px);
  els.pxTag && (els.pxTag.textContent=Number(px).toFixed(p));
  const up=(prev==null)||px>=prev;
  els.pxTag?.classList.toggle('green',up);
  els.pxTag?.classList.toggle('red',!up);
}

/* ====== Orders ====== */
const orders = setupOrders({
  onOrder: (side, qty) => {
    if (!state.symbol || state.lastClose==null) { setStatus('⚠️ Espera primer tick'); return; }
    ws.send({ action:"order:new", symbol:state.symbol, side,
      type: $('#ordType')?.value || 'market', qty, price:state.lastClose,
      sl: $('#sl')?.value ? Number($('#sl').value) : null,
      tp: $('#tp')?.value ? Number($('#tp').value) : null
    });
  },
  qtyInput: $('#qty'),
  lotValEl: $('#lotVal'),
  incBtn: $('#lotInc'),
  decBtn: $('#lotDec'),
  buyBtns: [$('#btnBuy'), $('#btnTopBuy')],
  sellBtns:[$('#btnSell'), $('#btnTopSell')],
});

/* ====== WebSocket ====== */
const ws = connectWS({
  buildUrl: ()=> buildWsUrl({ provider: state.provider, token: (state.provider==='finnhub') ? (els.tokenInput?.value||localStorage.finnhubToken||'') : null }),
  onOpen: () => {
    setStatus('✅ WS conectado');
    ws.send({ action:'change_symbol', symbol: state.symbol });
    ws.send({ action:'change_timeframe', timeframe: state.tf });
    ws.send({ action:'load_history', symbol: state.symbol, timeframe: state.tf });
  },
  onMsg: (msg) => {
    if (msg.type==='info'||msg.type==='warn'){ setStatus((msg.type==='info'?'ℹ️ ':'⚠️ ')+(msg.message||'')); return; }
    if (msg.type==='account:update'||msg.type==='account:snapshot'){ renderAccount(msg); return; }

    if (msg.type==='tick' && (('bid' in msg)||('ask' in msg)||('best_bid' in msg)||('best_ask' in msg))){
      state.bid = n(msg.bid ?? msg.best_bid ?? null);
      state.ask = n(msg.ask ?? msg.best_ask ?? null);
      if (state.bid!=null && state.ask!=null && state.ask>state.bid) {
        state.spread = state.ask - state.bid;
        state.prevClose = state.lastClose ?? state.bid;
        state.lastClose = (state.ask + state.bid) / 2;
        updateBidAsk(); setPrice(state.lastClose, state.prevClose);
      }
      return;
    }

    if (msg.type==='history' && Array.isArray(msg.data)){
      const bars = msg.data.map(c=>({time:n(c.time),open:n(c.open),high:n(c.high),low:n(c.low),close:n(c.close)}))
                           .filter(b=>b.time && b.open!=null && b.high!=null && b.low!=null && b.close!=null);
      chart.setHistory(bars);
      if (bars.length){ state.prevClose=bars.at(-2)?.close ?? bars.at(-1).open; state.lastClose=bars.at(-1).close; setPrice(state.lastClose,state.prevClose); }
      recomputeFallbackSpread(); updateBidAsk();
      return;
    }

    if ((msg.type==='candle_update'||msg.type==='candle_new') && msg.data){
      const b={ time:n(msg.data.time), open:n(msg.data.open), high:n(msg.data.high), low:n(msg.data.low), close:n(msg.data.close) };
      if (b.time && b.open!=null && b.high!=null && b.low!=null && b.close!=null){
        chart.updateCandle(b);
        state.prevClose = state.lastClose ?? b.open; state.lastClose = b.close; setPrice(state.lastClose, state.prevClose);
        if (state.bid==null||state.ask==null) updateBidAsk();
      }
      return;
    }

    if (msg.type==='positions'){ renderPositions(msg.items||[]); return; }
    if (msg.type==='order_close'){ renderPositions(msg.items||[]); return; }
  },
  onClose: ()=> setStatus('🔌 WS desconectado — reintentando…'),
  onError: ()=> setStatus('⚠️ WS error')
});

/* ====== Render helpers ====== */
function usd(v){ return v==null?'—':('$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2})); }
function renderAccount(msg){
  const r = {
    balance: $('#accBalance'), equity: $('#accEquity'), pnl: $('#accPnL'),
    margin: $('#accMargin'), free: $('#accFree'), lev: $('#accLev'),
  };
  if (!r.balance) return; // oculto en móvil
  const equity=Number(msg.equity??0), margin=Number(msg.margin_used??0);
  const free=(msg.free_margin!=null)?Number(msg.free_margin):(equity-margin);
  const upnl=Number((msg.upnl!=null)?msg.upnl:(msg.pnl_unreal!=null?msg.pnl_unreal:0));
  r.balance.textContent=usd(msg.balance); r.equity.textContent=usd(equity);
  r.margin.textContent=usd(margin); r.free.textContent=usd(free);
  r.lev.textContent=(msg.leverage!=null)?String(msg.leverage)+'x':'—';
  r.pnl.textContent=usd(upnl);
  r.pnl.classList.toggle('pos', upnl> 0.00001);
  r.pnl.classList.toggle('neg', upnl<-0.00001);
  if (Math.abs(upnl)<=0.00001){ r.pnl.classList.remove('pos','neg'); }
}
function renderPositions(rows){
  const p=(state.symbol.includes('BTC')||state.symbol.includes('ETH'))?2:(state.symbol.endsWith('/JPY')?3:5);
  els.posTbody.innerHTML=(rows||[]).map(r=>`
    <tr data-id="${r.id}" data-sym="${r.symbol}">
      <td>${r.id}</td><td>${(r.symbol||'').replace('/','')}</td><td>${String(r.side||'').toUpperCase()}</td>
      <td align="right">${Number(r.qty||0).toFixed(2)}</td>
      <td align="right">${Number(r.avg ?? r.entry ?? 0).toFixed(p)}</td>
      <td align="right">${r.sl!=null?Number(r.sl).toFixed(p):'-'}</td>
      <td align="right">${r.tp!=null?Number(r.tp).toFixed(p):'-'}</td>
      <td>${r.opened_at? new Date((r.opened_at||0)*1000).toLocaleTimeString(): '-'}</td>
    </tr>`).join('');
}

/* ====== UI bindings ====== */
els.providerSel?.addEventListener('change', ()=>{
  state.provider = els.providerSel.value; localStorage.provider = state.provider;
  els.tokenInput && (els.tokenInput.style.display = (state.provider==='finnhub')?'inline-block':'none');
  ws.close(1000); // reconecta solo
});
els.tokenInput?.addEventListener('change', ()=>{ localStorage.finnhubToken = els.tokenInput.value || ''; ws.close(1000); });

els.selSymbol?.addEventListener('change', ()=>{
  state.symbol = els.selSymbol.value;
  updateHeaders(); chart.setSymbol(state.symbol);
  ws.send({ action:'change_symbol', symbol: state.symbol });
  ws.send({ action:'load_history', symbol: state.symbol, timeframe: state.tf });
  recomputeFallbackSpread(); updateBidAsk();
});
els.selTF?.addEventListener('change', ()=>{
  state.tf = els.selTF.value; updateHeaders();
  ws.send({ action:'change_timeframe', timeframe: state.tf });
  ws.send({ action:'load_history', symbol: state.symbol, timeframe: state.tf });
});

document.querySelectorAll('.watchlist .wl')?.forEach(b=>{
  b.addEventListener('click', ()=>{ if (!els.selSymbol) return; els.selSymbol.value=b.dataset.sym; els.selSymbol.dispatchEvent(new Event('change')); });
});
document.querySelectorAll('.tabbtn')?.forEach(b=>{
  b.addEventListener('click', ()=>{
    document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('active')); b.classList.add('active');
    const t=b.dataset.tab;
    $('.rightcol')       && ($('.rightcol').style.display  = (t==='trade')?'flex':'none');
    $('.positions-card') && ($('.positions-card').style.display = (t==='history')?'block':'none');
    $('.board')         && ($('.board').style.display = (t==='chart')?'block':'none');
    chart.resize();
  });
});

/* ====== bootstrap ====== */
updateHeaders();
recomputeFallbackSpread();
updateBidAsk();