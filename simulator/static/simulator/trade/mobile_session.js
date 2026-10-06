/* PRE-VPS-POLISH-03C.2.1/03C.2.2 — Mobile Trading Session foundation.
   Shares the exact same backend/WS protocol and account-ownership
   boundary as Desktop's TradingPanel (simulator/templates/simulator/
   trade/desktop.html) — same /ws/trading/<accountId>/ endpoint, same
   window.__TRADE_CONFIG__.accountId, same provider/token query params,
   same heartbeat/reconnect constants. No financial logic lives here;
   this is transport + presentation-status only.

   03C.2.2 adds live price/tick quote handling, reusing trading_core.js's
   applyPriceTickState() directly — the FIX-05C fail-closed gate is NOT
   duplicated here; this file only parses/forwards fields and applies
   whatever that shared function returns.

   03C.2.3 adds symbol selection (selectSymbol()) — the ONLY outbound
   action Mobile sends is the existing, unmodified backend contract
   {action:'change_symbol',symbol}, from selectSymbol() itself or from
   onopen's reconnect-restoration resend. No other new WS action exists
   here — no timeframe switching, no history loading, no order:*, no
   risk:*.

   Deliberately still minimal: NO chart, history, timeframe selection,
   positions, pending orders, or order placement — those are later,
   separately authorized sub-blocks (03C.2.4+/03C.3). */

// Same client-side provider preference Desktop reads — not a second
// source of truth, just the identical localStorage key read again in
// this template's own script scope (desktop.html's `globalProvider` is
// not reachable here; this file is not loaded by desktop.html).
let mobileGlobalProvider = localStorage.provider || 'sim';

class MobileTradingSession {
  constructor(onStatusChange, onAccount, onQuote, allowedSymbols) {
    this.ws = null;
    this.hb = null;
    this.reconnTimer = null;
    this.reconnDelay = 800;
    this.connecting = false;
    this.onStatusChange = onStatusChange || (() => {});
    this.onAccount = onAccount || (() => {});
    this.onQuote = onQuote || (() => {});

    // PRE-VPS-POLISH-03C.2.3 — server-provided symbol catalog (the same
    // authority the backend's own change_symbol handler enforces via
    // _ALLOWED_SYMBOLS), used only for client-side UX validation in
    // selectSymbol() — never a second source of truth; the backend
    // still independently validates and rejects on its own.
    this.allowedSymbols = Array.isArray(allowedSymbols) ? allowedSymbols : [];

    // PRE-VPS-POLISH-03C.2.2 — quote state belongs to this connection/
    // symbol (the backend subscribes one symbol per connection), same
    // per-instance pattern Desktop's TradingPanel already uses for its
    // own bid/ask/liveMid/liveSource/prevLiveMid — never a global.
    this.currentSymbol = null;
    this.bid = null;
    this.ask = null;
    this.liveMid = null;
    this.liveSource = null;
    this.prevLiveMid = null;
  }

  // PRE-VPS-POLISH-03C.2.3 — the one Mobile symbol authority. Mirrors
  // Desktop's _onSymChange() ordering exactly: state updated (and quote
  // fields reset) synchronously BEFORE the WS send, so the existing
  // _handleMsg symbol-mismatch guard immediately rejects any old-symbol
  // tick already in flight — no separate desiredSymbol/currentSymbol
  // split needed (see 03C.2.3 preflight, Section H).
  selectSymbol(symbol) {
    if (this.allowedSymbols.length && !this.allowedSymbols.includes(symbol)) return;
    this.currentSymbol = symbol;
    // FIX-05C-pattern reset (mirrors desktop.html's _onSymChange()): the
    // previous symbol's live quote must never leak into the newly
    // selected instrument.
    this.bid = null;
    this.ask = null;
    this.liveMid = null;
    this.liveSource = null;
    this.prevLiveMid = null;
    this.onQuote({
      symbol: this.currentSymbol,
      bid: this.bid,
      ask: this.ask,
      liveMid: this.liveMid,
      liveSource: this.liveSource,
    });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ action: 'change_symbol', symbol: this.currentSymbol })); } catch (_) {}
    }
    // If the socket isn't OPEN: do nothing further. currentSymbol is
    // already set, so onopen's reconnect-restoration resend (below)
    // picks it up once a connection is actually established.
  }

  // Identical construction to TradingPanel.wsUrl() — same accountId
  // source, same ws/wss derivation, same provider/token params.
  wsUrl() {
    const accountId = window.__TRADE_CONFIG__.accountId;
    const u = accountId
      ? new URL('/ws/trading/' + accountId + '/', window.location.href)
      : new URL('/ws/trading/', window.location.href);
    u.protocol = (u.protocol === 'https:') ? 'wss:' : 'ws:';
    u.searchParams.set('provider', mobileGlobalProvider);
    if (mobileGlobalProvider === 'finnhub') {
      const t = (document.getElementById('fhToken')?.value || localStorage.finnhubToken || '').trim();
      if (t) u.searchParams.set('token', t);
    }
    return u.toString();
  }

  connect() {
    if (this.connecting || (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING))) return;
    this.connecting = true;
    this.onStatusChange('CONNECTING');
    try {
      this.ws = new WebSocket(this.wsUrl());
    } catch (e) {
      this.connecting = false;
      this.onStatusChange('DISCONNECTED');
      return;
    }
    this.ws.onopen = () => {
      this.onStatusChange('CONNECTED');
      this.reconnDelay = 800;
      this.connecting = false;
      clearInterval(this.hb);
      this.hb = setInterval(() => { try { this.ws.send('{"action":"ping"}'); } catch (_) {} }, 15000);
      // PRE-VPS-POLISH-03C.2.3 — reconnect symbol restoration, mirroring
      // Desktop's connect()/onopen exactly: every new connection
      // (including every reconnect) re-sends change_symbol for
      // whatever currentSymbol already is, so a user selection survives
      // a dropped socket instead of silently drifting back to the
      // backend's own per-connection default symbol.
      if (this.currentSymbol) {
        try { this.ws.send(JSON.stringify({ action: 'change_symbol', symbol: this.currentSymbol })); } catch (_) {}
      }
    };
    this.ws.onmessage = ev => { try { this._handleMsg(JSON.parse(ev.data)); } catch (_) {} };
    this.ws.onerror = () => { this.onStatusChange('DISCONNECTED'); };
    this.ws.onclose = () => {
      this.connecting = false;
      clearInterval(this.hb);
      this.onStatusChange('RECONNECTING');
      clearTimeout(this.reconnTimer);
      this.reconnTimer = setTimeout(() => this.connect(), Math.min(5000, this.reconnDelay));
      this.reconnDelay = Math.min(5000, this.reconnDelay * 1.7);
    };
  }

  disconnect() {
    try { this.ws && this.ws.close(1000); } catch (_) {}
  }

  // account:update/account:snapshot (03C.2.1) + price/tick (03C.2.2).
  // Every other message type (history/candles/positions/pending/order
  // feedback) is intentionally ignored here — out of scope until its
  // own separately-authorized sub-block.
  _handleMsg(msg) {
    if (msg.type === 'account:update' || msg.type === 'account:snapshot') {
      this.onAccount(msg);
      return;
    }
    if (msg.type === 'price' || msg.type === 'tick') {
      // Defense-in-depth only — the backend already subscribes one
      // symbol per connection (see 03C.2.2 preflight), same as
      // Desktop's own panel-filter guard.
      if (this.currentSymbol && msg.symbol && msg.symbol !== this.currentSymbol) return;
      const bid = n(msg.bid ?? msg.best_bid ?? null);
      const ask = n(msg.ask ?? msg.best_ask ?? null);
      const source = msg.source;
      // The ONE shared authority — trading_core.js's applyPriceTickState().
      // The FIX-05C fail-closed gate lives there, once, and is never
      // duplicated here.
      const state = applyPriceTickState(this.liveMid, bid, ask, source);
      if (!state) return;
      this.currentSymbol = msg.symbol || this.currentSymbol;
      this.prevLiveMid = state.prevLiveMid;
      this.bid = state.bid;
      this.ask = state.ask;
      this.liveMid = state.liveMid;
      this.liveSource = state.liveSource;
      this.onQuote({
        symbol: this.currentSymbol,
        bid: this.bid,
        ask: this.ask,
        liveMid: this.liveMid,
        liveSource: this.liveSource,
      });
      return;
    }
  }
}
