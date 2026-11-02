/* Watchlist display only. Never sends orders or updates account/position state. */
window.MobileWatchQuotes = function (options) {
  'use strict';
  const readers = new Map(), quotes = new Map();
  let active = options.active, paused = false;
  const symbols = Array.from(new Set(options.symbols));
  function invalidate(symbol) { quotes.delete(symbol); options.changed(symbol); }
  function stop(symbol) {
    const r = readers.get(symbol); if (!r) return;
    readers.delete(symbol); clearTimeout(r.retry); clearInterval(r.heartbeat);
    r.socket.close(); invalidate(symbol);
  }
  function open(symbol) {
    if (paused || symbol === active || readers.has(symbol)) return;
    const url = new URL(options.url()); url.searchParams.set('tf', '1m');
    const r = {socket: new WebSocket(url.toString()), retry: null, heartbeat: null};
    readers.set(symbol, r);
    r.socket.onopen = function () {
      if (readers.get(symbol) !== r) return;
      r.socket.send(JSON.stringify({action:'change_symbol',symbol}));
      r.heartbeat = setInterval(function () {
        if (r.socket.readyState === WebSocket.OPEN) r.socket.send(JSON.stringify({action:'ping'}));
      },15000);
    };
    r.socket.onmessage = function (event) {
      if (readers.get(symbol) !== r) return;
      let m; try { m=JSON.parse(event.data); } catch (_) { return; }
      if (!['tick','price'].includes(m.type) || m.symbol !== symbol) return;
      const bid=m.bid ?? m.best_bid, ask=m.ask ?? m.best_ask;
      if (bid == null || ask == null || !Number.isFinite(Number(bid)) || !Number.isFinite(Number(ask))) return;
      const q=applyPriceTickState(null,Number(bid),Number(ask),m.source); if (!q) return;
      const raw=Number(m.time), at=raw>0 ? (raw<1e12 ? raw*1000 : raw) : Date.now();
      if (Date.now()-at>60000 || at>Date.now()+5000) { invalidate(symbol); return; }
      quotes.set(symbol,{bid:q.bid,ask:q.ask,at});options.changed(symbol);
    };
    r.socket.onclose = function () {
      if (readers.get(symbol) !== r) return;
      clearInterval(r.heartbeat); invalidate(symbol);
      r.retry=setTimeout(function () { if (readers.get(symbol)!==r) return; readers.delete(symbol);open(symbol); },3000);
    };
  }
  function sync() { symbols.forEach(s=>{if(s===active)stop(s);else open(s);}); }
  const expiry=setInterval(function () { quotes.forEach((q,s)=>{if(Date.now()-q.at>60000)invalidate(s);}); },1000);
  sync();
  return {
    get(symbol) { const q=quotes.get(symbol); return q && Date.now()-q.at<=60000 ? q : null; },
    setActive(symbol) { if(symbol===active || !symbols.includes(symbol))return; active=symbol;sync(); },
    pause() { paused=true;Array.from(readers.keys()).forEach(stop); },
    resume() { paused=false;sync(); },
    destroy() { this.pause();clearInterval(expiry); }
  };
};
