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
   onopen's reconnect-restoration resend.

   03C.2.4 adds timeframe selection (selectTimeframe()) plus parsing of
   history/candle_new/candle_update/volume_update. This file still owns
   ONLY transport + symbol/timeframe state + guarded message parsing —
   it forwards already-guarded raw data to MobileTradingChart
   (mobile_chart.js) via plain callbacks (same pattern as onAccount/
   onQuote), never touching chart/bars/series itself. No order:*, no
   risk:*, no open trades, no pending orders — those remain later,
   separately authorized sub-blocks (03C.3+). */

// Same client-side provider preference Desktop reads — not a second
// source of truth, just the identical localStorage key read again in
// this template's own script scope (desktop.html's `globalProvider` is
// not reachable here; this file is not loaded by desktop.html).
let mobileGlobalProvider = localStorage.provider || 'sim';

// PRE-VPS-POLISH-03C.2.4 — the real backend timeframe catalog (consumers.
// py's tf_seconds() keys — confirmed no four-hour entry anywhere
// server-side), used only to validate selectTimeframe()'s input. The
// SECONDS values
// themselves are never re-typed here — tfToSec() (trading_core.js)
// remains the single source for that mapping; this list exists only
// because tfToSec() has a silent ??60 fallback and can't itself reject
// an invalid timeframe.
const MOBILE_TIMEFRAMES = ['1s', '1m', '5m', '15m', '1h', '1d'];

class MobileTradingSession {
  constructor(onStatusChange, onAccount, onQuote, allowedSymbols, onHistory, onCandleNew, onCandleUpdate, onVolumeUpdate) {
    this.ws = null;
    this.hb = null;
    this.reconnTimer = null;
    this.reconnDelay = 800;
    this.connecting = false;
    this.onStatusChange = onStatusChange || (() => {});
    this.onAccount = onAccount || (() => {});
    this.onQuote = onQuote || (() => {});
    // PRE-VPS-POLISH-03C.2.4 — same plain-callback pattern as onAccount/
    // onQuote; each receives already symbol/timeframe-guarded raw data
    // (never a parsed/normalized bar — normalization is MobileTradingChart's
    // job, not this transport layer's).
    this.onHistory = onHistory || (() => {});
    this.onCandleNew = onCandleNew || (() => {});
    this.onCandleUpdate = onCandleUpdate || (() => {});
    this.onVolumeUpdate = onVolumeUpdate || (() => {});

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

    // PRE-VPS-POLISH-03C.2.4 — timeframe state. Unlike currentSymbol
    // (which starts null until the user picks one from the watchlist),
    // currentTF always has a real value — mirrors Desktop, which never
    // has a "no timeframe" state either.
    this.currentTF = '15m';

    // Same debounce helper Desktop's own _loadHistory=debounce(...,180)
    // already uses (trading_core.js) — reused, not reimplemented.
    this._requestHistory = debounce(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN && this.currentSymbol) {
        try { this.ws.send(JSON.stringify({ action: 'load_history', symbol: this.currentSymbol, timeframe: this.currentTF })); } catch (_) {}
      }
    }, 180);
  }

  // PRE-VPS-POLISH-03C.2.4 — the one Mobile timeframe authority. Mirrors
  // Desktop's _onTFChange(): validated against the real catalog, state
  // updated synchronously, send change_timeframe + request history —
  // and, deliberately, bid/ask/liveMid/liveSource/prevLiveMid are NEVER
  // touched here (FIX-05C quote state survives a timeframe-only change,
  // exactly like Desktop's _onTFChange() leaves it alone — the symbol
  // didn't change, so the live quote is still valid).
  selectTimeframe(tf) {
    if (!MOBILE_TIMEFRAMES.includes(tf)) return;
    this.currentTF = tf;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ action: 'change_timeframe', timeframe: this.currentTF })); } catch (_) {}
      this._requestHistory();
    }
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
      // PRE-VPS-POLISH-03C.2.4 — explicit history request, mirroring
      // Desktop's own _onSymChange() (which calls _loadHistory() right
      // after sending change_symbol too) — technically redundant since
      // change_symbol alone already triggers a full backend history
      // resend, but that double-send is a confirmed, harmless,
      // real architectural fact on Desktop, not a bug to "fix" here.
      this._requestHistory();
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
      // PRE-VPS-POLISH-03C.2.4 — reconnect timeframe restoration, same
      // rationale as the symbol resend above (Desktop's connect()/onopen
      // always resends both). currentTF always has a real value, so this
      // always fires. History is only (re-)requested once a symbol is
      // actually selected — load_history needs a real symbol, and until
      // one is chosen there is nothing to request yet.
      try { this.ws.send(JSON.stringify({ action: 'change_timeframe', timeframe: this.currentTF })); } catch (_) {}
      if (this.currentSymbol) this._requestHistory();
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

  // account:update/account:snapshot (03C.2.1) + price/tick (03C.2.2) +
  // history/candle_new/candle_update/volume_update (03C.2.4). Positions/
  // pending orders/order feedback remain intentionally ignored here —
  // out of scope until their own separately-authorized sub-block.
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
    // PRE-VPS-POLISH-03C.2.4 — history: guarded on BOTH symbol and
    // timeframe, exactly like Desktop's own history branch (desktop.html
    // ~3551/3561) — this is the one message type Desktop itself guards
    // on both fields, a real, confirmed asymmetry versus candle_*/
    // volume_update below (which carry no timeframe field at all and are
    // guarded on symbol only, matching Desktop exactly). Raw data/phase
    // forwarded as-is; normalization + replace-vs-prepend decision is
    // MobileTradingChart's job, not this transport layer's.
    if (msg.type === 'history' && Array.isArray(msg.data)) {
      if (msg.symbol && msg.symbol !== this.currentSymbol) return;
      if (msg.timeframe && msg.timeframe !== this.currentTF) return;
      this.onHistory(msg.data, msg.phase);
      return;
    }
    // candle_new/candle_update: symbol-only guard — mirrors Desktop's
    // real, unguarded-by-timeframe behavior (desktop.html:3593); these
    // backend messages carry no timeframe field to guard on at all.
    if ((msg.type === 'candle_new' || msg.type === 'candle_update') && msg.data) {
      if (msg.symbol && msg.symbol !== this.currentSymbol) return;
      if (msg.type === 'candle_new') this.onCandleNew(msg.data);
      else this.onCandleUpdate(msg.data);
      return;
    }
    // volume_update: real backend message Desktop itself never consumes
    // (Desktop derives live volume from the candle bar via
    // volPointForBar instead) — Mobile deliberately consumes it here,
    // an explicitly authorized divergence from Desktop's actual
    // behavior. Symbol-only guard, same as candle_*  above (no
    // timeframe field exists on this message either).
    if (msg.type === 'volume_update') {
      if (msg.symbol && msg.symbol !== this.currentSymbol) return;
      this.onVolumeUpdate({ time: msg.time, value: msg.value, color: msg.color });
      return;
    }
  }
}
