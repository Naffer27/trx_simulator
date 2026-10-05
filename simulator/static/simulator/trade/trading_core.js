/* PRE-VPS-POLISH-03B.2A — Shared Trading Core — pure helpers/constants.
   Extracted verbatim from simulator/templates/simulator/trade/desktop.html.
   Zero behavior change: same names, same signatures, same bodies.
   Loaded before desktop.html's own inline <script> blocks. */

const fmt=(v,p=5)=>Number(v).toFixed(p).replace(/0+$/,'').replace(/\.$/,'');
const RGX_HEX=/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i;
const hexToRgba=(hex,a=1)=>{const m=RGX_HEX.exec(hex);if(!m)return hex;return `rgba(${parseInt(m[1],16)},${parseInt(m[2],16)},${parseInt(m[3],16)},${a})`;};

/* ── Shared utils ── */
const priceFormatFor=sym=>(sym.includes('BTC')||sym.includes('ETH'))?{precision:2,minMove:0.01}:sym.endsWith('/JPY')?{precision:3,minMove:0.001}:{precision:5,minMove:0.00001};
// LIVE-CHART-MAGNITUDE-FILTER-01 — the REAL pip size, hand-verified,
// deliberately NEVER derived from priceFormatFor().minMove: minMove is
// the order-pricing tick/pipette granularity (0.00001 for most forex
// pairs, 0.001 for JPY — a tenth of a real pip), a different concept
// from "1 pip". Real convention: 0.0001 for non-JPY forex, 0.01 for
// JPY pairs.
const pipSizeFor=sym=>sym.endsWith('/JPY')?0.01:0.0001;
const tfToSec=tf=>({'1s':1,'1m':60,'5m':300,'15m':900,'1h':3600,'1d':86400}[String(tf)]??60);
const roundToTick=(price,sym)=>{const {minMove,precision}=priceFormatFor(sym);return Number((Math.round(price/minMove)*minMove).toFixed(precision));};
const debounce=(fn,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};};
const normTime=secOrMs=>{const t=Number(secOrMs||0);if(!t)return null;return(t>1e12)?Math.floor(t/1000):t;};
// GOLDEN-MARKETDATA-CRYPTO-01 — ACCEPTANCE FIX (volume). Real volume (b.volume,
// present on history bars for every symbol — Forex and crypto both confirmed
// live to carry valid non-zero values from Massive) is used when available.
// The span*1e6 proxy — calibrated for Forex's pip-scale price movements,
// confirmed to explode to 15,000x-450,000x its intended range for BTC/ETH's
// dollar-scale movements — is now ONLY a fallback for the live tick path
// (candle_new/candle_update), whose bars carry no volume field at all
// (Massive quotes have no per-tick trade volume, same as before this fix).
// UI-THEME-01 FASE D4.10 — opacity raised .45→.62 (same bull/bear hex,
// same value/color computation, zero data change) so volume bars are
// clearly visible against the chart background instead of washing out;
// the histogram's own pane allocation (scaleMargins top:0.82, ~18% of
// pane height — see initChart()) was already within the requested
// 12-18% target and is untouched.
const volPointForBar=b=>{const up=(b.close??0)>=(b.open??0);let val;if(b.volume!=null&&Number.isFinite(b.volume)&&b.volume>0){val=b.volume;}else{const span=Math.max((b.high??b.close)-(b.low??b.close),Math.abs((b.close??0)-(b.open??0)));val=Math.max(1,Math.floor(span*1e6));}return{time:b.time,value:val,color:up?'rgba(38,166,154,.62)':'rgba(239,83,80,.62)'};};

// UI-THEME-01 FASE D4.10 — RANGE (history visible) definitions, distinct
// from TIMEFRAME (candle size, tfToSec above). Each entry's `days` is
// calendar time, applied via setVisibleRange() over whatever is already
// loaded in a panel's own this._bars — never a new fetch/infra. See
// TradingPanel._updateRangeBar()/_applyRange().
const RANGE_DEFS=[
  {key:'1D',label:'1D',days:1},
  {key:'5D',label:'5D',days:5},
  {key:'1M',label:'1M',days:30},
  {key:'3M',label:'3M',days:90},
  {key:'6M',label:'6M',days:182},
  {key:'1Y',label:'1Y',days:365},
];
const usd=v=>v==null?'—':('$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2}));

/* ── Lot specs per symbol (mirrors SymbolSpec.lot_step / min_lot) ── */
const LOT_SPECS = {
  "AUD/USD": {step:0.01,  min:0.01,  dec:2},
  "BTCUSD":  {step:0.001, min:0.001, dec:3},
  "ETHUSD":  {step:0.01,  min:0.01,  dec:2},
  "EUR/USD": {step:0.01,  min:0.01,  dec:2},
  "GBP/USD": {step:0.01,  min:0.01,  dec:2},
  "NAS100":  {step:0.1,   min:0.1,   dec:1},
  "NZD/USD": {step:0.01,  min:0.01,  dec:2},
  "SOLUSD":  {step:0.1,   min:0.1,   dec:1},
  "US30":    {step:0.1,   min:0.1,   dec:1},
  "US500":   {step:0.1,   min:0.1,   dec:1},
  "USD/CAD": {step:0.01,  min:0.01,  dec:2},
  "USD/CHF": {step:0.01,  min:0.01,  dec:2},
  "USD/JPY": {step:0.01,  min:0.01,  dec:2},
  "XAG/USD": {step:0.01,  min:0.01,  dec:2},
  "XAU/USD": {step:0.01,  min:0.01,  dec:2},
};
function getLotStep(sym)    { return (LOT_SPECS[sym]||{step:0.01}).step; }
function getLotMin(sym)     { return (LOT_SPECS[sym]||{min:0.01}).min; }
function getLotDecimals(sym){ return (LOT_SPECS[sym]||{dec:2}).dec; }
/* PANEL-04 — per-symbol qty label text only; does not touch backend units/formulas */
function getQtyLabel(sym){
  if(sym==='BTCUSD') return 'Volume (BTC)';
  if(sym==='ETHUSD') return 'Volume (ETH)';
  return 'Volume (Lots)';
}

/* getContractSize() itself is pure (no Django tags) — but it reads the
   global CONTRACT_SIZE constant, which IS Django-templated and therefore
   stays defined in desktop.html (cannot physically live in a static .js
   file). Safe via ordinary JS late binding: this function is only ever
   CALLED at runtime, long after desktop.html's own <script> has already
   defined CONTRACT_SIZE — see PRE-VPS-POLISH-03B.2A report, Section B. */
function getContractSize(sym){ return CONTRACT_SIZE[sym] ?? 1; }

/* ── MARGIN-02 — per-position PnL ──────────────────────────────────
   Backend is authoritative: the "positions" WS message now includes a
   pre-computed pos.pnl (account currency, via simulator/pnl_engine.py —
   the same engine used for equity/close/Trade.profit_loss). This is the
   ONLY thing that must be correct for USD/JPY, whose quote currency
   (JPY) differs from the account currency (USD, the only account
   currency this system has today).
   The client-side fallback below only runs if pos.pnl is absent (e.g. a
   stale cache from before this block) — it mirrors pnl_engine's two
   supported conversion modes (no-conversion, base==account inverse) and
   never invents a rate for anything else. */
const QUOTE_CURRENCY = {
  "EUR/USD": "USD", "GBP/USD": "USD", "USD/JPY": "JPY", "AUD/USD": "USD",
  "BTCUSD": "USD", "ETHUSD": "USD", "XAU/USD": "USD",
};
const ACCOUNT_CURRENCY_FALLBACK = "USD"; // every TradingAccount in this system is USD today
/* Conversion-aware raw calc — used only when no backend pos.pnl is
   available (e.g. chart-line labels, which aren't backed by a positions
   snapshot entry). Mirrors pnl_engine.py's two supported modes. */
function computeRawPnL(symbol, side, entry, qty, px){
  const cs=getContractSize(symbol||'');
  const raw=String(side||'buy').toLowerCase()==='buy'?(px-entry)*qty*cs:(entry-px)*qty*cs;
  const quoteCcy=QUOTE_CURRENCY[symbol||'']||'USD';
  if(quoteCcy===ACCOUNT_CURRENCY_FALLBACK) return raw;
  // base_account_inverse: the instrument's own price IS the conversion rate.
  return px>0 ? raw/px : raw;
}
function computePositionPnL(pos, px){
  if(pos.pnl!=null && isFinite(Number(pos.pnl))) return Number(pos.pnl);
  return computeRawPnL(pos.symbol, pos.side, Number(pos.avg??0), Number(pos.qty??1), px);
}

function normalizeLot(sym, v){
  const s=LOT_SPECS[sym]||{step:0.01,min:0.01,dec:2};
  const val=Math.max(s.min, Math.min(100, parseFloat(v)||s.min));
  return +val.toFixed(s.dec);
}

/* ── Math — pure functions, no side effects ── */
function _calcSMA(bars, period){
  const out=[];
  for(let i=period-1;i<bars.length;i++){
    let s=0;for(let j=0;j<period;j++)s+=bars[i-j].close;
    out.push({time:bars[i].time,value:s/period});
  }
  return out;
}
function _calcEMA(bars, period){
  if(bars.length<period)return[];
  const k=2/(period+1);const out=[];
  let ema=0;for(let i=0;i<period;i++)ema+=bars[i].close;ema/=period;
  out.push({time:bars[period-1].time,value:ema});
  for(let i=period;i<bars.length;i++){
    ema=bars[i].close*k+ema*(1-k);
    out.push({time:bars[i].time,value:ema});
  }
  return out;
}
function _calcRSI(bars, period){
  if(bars.length<period+1)return[];
  const out=[];
  let avgGain=0,avgLoss=0;
  for(let i=1;i<=period;i++){
    const d=bars[i].close-bars[i-1].close;
    if(d>0)avgGain+=d;else avgLoss+=Math.abs(d);
  }
  avgGain/=period;avgLoss/=period;
  const rs0=avgLoss===0?Infinity:avgGain/avgLoss;
  out.push({time:bars[period].time,value:avgLoss===0?100:100-100/(1+rs0)});
  for(let i=period+1;i<bars.length;i++){
    const d=bars[i].close-bars[i-1].close;
    avgGain=(avgGain*(period-1)+(d>0?d:0))/period;
    avgLoss=(avgLoss*(period-1)+(d<0?Math.abs(d):0))/period;
    const rs=avgLoss===0?Infinity:avgGain/avgLoss;
    out.push({time:bars[i].time,value:avgLoss===0?100:100-100/(1+rs)});
  }
  return out;
}

const n=v=>v===null||v===undefined||v===''?null:Number(v);

/* ── PRE-VPS-POLISH-03B.2C.2A — shared price/tick quote-state seam ──
   Pure, authoritative-state-only mirror of the FIX-05C fail-closed gate
   inside TradingPanel._handleMsg()'s price/tick branch (desktop.html).
   Takes explicit scalar inputs only — no DOM, no chart API, no mutable
   globals, no quotesLivePx, no renderQuotes, no TradingPanel lookup, no
   WebSocket, no localStorage. The price/tick branch itself stays in
   place in desktop.html (10+ source-contract tests depend on its exact
   position/adjacency, per the 03B.2C.2 preflight) and calls this
   function for the authoritative calculation only; its own side effects
   (_scheduleVisualRender, quotesLivePx, renderQuotes) remain inline,
   unchanged.
   FIX-05C invariant, preserved verbatim: a tick only becomes REAL LIVE
   QUOTE authority (bid/ask/liveMid) when its source is explicit and
   trusted — source==null (missing) or source==="sim" must NEVER
   establish a tradeable quote. Returns null when the gate rejects the
   tick (caller must apply no state change); otherwise returns the exact
   {bid,ask,liveMid,liveSource,prevLiveMid} the caller assigns verbatim. */
function applyPriceTickState(currentLiveMid, bid, ask, source){
  if(source!=null&&source!=='sim'&&bid!=null&&ask!=null&&ask>bid){
    return {bid:bid,ask:ask,liveMid:(ask+bid)/2,liveSource:source,prevLiveMid:currentLiveMid};
  }
  return null;
}
