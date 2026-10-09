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
   onQuote), never touching chart/bars/series itself.

   03C.2.5 adds the trading-actions foundation: submitOrder()/
   requestRiskPreview()/confirmRiskWarning()/cancelRiskWarning(), plus
   parsing of order_ack/order_rejected/risk_warning/risk_preview/error/
   positions. This file sends the EXACT same {action:'order:new',...}/
   {action:'order:risk_preview',...} contract Desktop's TradingPanel
   already uses — no price, no accountId, no margin/commission/spread/
   contract-size/risk-engine logic duplicated here. Mobile sends
   intention; the backend remains the sole financial authority, exactly
   as every prior quote/symbol/chart message in this file already
   defers to it.

   03C.2.6 adds closePosition()/cancelPendingOrder()/
   requestClosedTrades(), plus parsing of pending_orders/order_close/
   closed_trades_snapshot/warn. Same contract discipline: EXACT real
   backend actions (order:close/order:pending:cancel/get_closed_trades),
   no second canonical store (positions keep reusing trading_core.js's
   getCanonicalPositions/replaceCanonicalPositions; pending orders get a
   plain forwarded array, same as Desktop's own pendingOrdersCache —
   Desktop has no shared canonical store for pending either), no local
   P&L/commission/margin/spread/IB computation anywhere. Still no
   pending-order creation or edit (the backend's own pending-order
   create/update actions stay out of scope — Mobile is still market-only
   for creation), no partial-close UI (closePosition() accepts an
   optional qty the backend already supports, but the ticket's own
   Close control never passes one). Those remain later, separately
   authorized sub-blocks.

   MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP-on-open-position editing
   (audited-then-authorized sub-block) — adds updateSlTp(), the ONLY
   Mobile authority for modifying SL/TP on an ALREADY-OPEN position
   (distinct from submitOrder()'s sl/tp, which only ever applies at
   NEW-order creation time). Sends the EXACT real backend contract
   Desktop's chart drag-lines/sheet editor already use unmodified
   (consumers.py::TradingConsumer._order_update, confirmed by a
   dedicated read-only audit of consumers.py + desktop.html before this
   method was authorized): {action:'order:update', id, symbol, sl, tp}.
   No new WebSocket, no second position model, no parallel P&L/SL/TP/
   price computation — sl/tp are passed through to the wire EXACTLY as
   the caller (mobile.html) provides them; this method never defaults
   either to null itself (null here only ever means an explicit
   removal the caller already decided on).

   The real backend response carries NO request/correlation id —
   success is just the generic {"type":"positions",...} snapshot (same
   message every other flow can also trigger), and 'error'/'warn' share
   codes with order:new's own validation path. This file resolves that
   the same way it already resolves order:new vs order:close ambiguity
   (_orderSending/_closeSending, see closePosition()'s own comment):
   one more mutually-exclusive single-slot guard, _slTpSendingId.
   Matching an incoming 'positions' snapshot against what was actually
   requested (same id + same confirmed sl/tp, instrument precision)
   is the caller's (mobile.html's) job, via the EXISTING, UNCHANGED
   onPositions callback — this file adds no special-casing to the
   'positions' branch below for this. A generous (20s) internal-only
   safety-valve timeout exists solely so a lost/dropped response can
   never deadlock this slot forever; it is NOT a success/failure
   declaration (the caller's own, separate, shorter UI-facing timeout
   owns that user-facing distinction) and is cleared early by
   clearSlTpPending() the moment the caller itself determines the real
   outcome (so a legitimate quick success never has to wait it out). */

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
//
// PRE-VPS-POLISH-03C.3.TF-01 — narrowed to the current PUBLIC catalog
// (1s retired from the UI; mobile.html's own MOBILE_TIMEFRAMES was
// narrowed to this exact same set — one contractual authority, not two
// incompatible catalogs). This is a client-side input-validation list
// only, not a capability flag: the backend's own tf_seconds()/
// normalize_tf() (consumers.py) still fully support "1s" internally
// and remain untouched as a capability — nothing here ever sends "1s"
// anymore simply because nothing in the UI offers it.
// PRE-VPS-POLISH-03C.3.TF-02 — real 4h added to this client-side
// validation list, matching mobile.html's own public catalog exactly
// (one contractual authority) — the backend's _TF_ALIASES/_TF_SECONDS
// (consumers.py) now recognize "4h" as a real 14400-second bucket.
const MOBILE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'];

class MobileTradingSession {
  constructor(onStatusChange, onAccount, onQuote, allowedSymbols, onHistory, onCandleNew, onCandleUpdate, onVolumeUpdate, onRiskPreview, onRiskWarning, onOrderAck, onOrderRejected, onTradingError, onPositions, onPendingOrders, onOrderClose, onCloseError, onClosedTrades, onPendingOrderCancelled, onPendingCancelError, onSlTpUpdateRejected) {
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

    // PRE-VPS-POLISH-03C.2.5 — same plain-callback pattern, one per real
    // backend message this trading-actions foundation now parses. Each
    // receives the raw server message unmodified (or, for onPositions,
    // the real canonical store) — never a client-computed financial
    // value.
    this.onRiskPreview = onRiskPreview || (() => {});
    this.onRiskWarning = onRiskWarning || (() => {});
    this.onOrderAck = onOrderAck || (() => {});
    this.onOrderRejected = onOrderRejected || (() => {});
    this.onTradingError = onTradingError || (() => {});
    this.onPositions = onPositions || (() => {});

    // PRE-VPS-POLISH-03C.2.6 — same plain-callback pattern, one per real
    // backend message this positions/pending/closed foundation now
    // parses. onPendingOrders receives the raw forwarded array (see
    // _handleMsg's 'pending_orders' branch — no canonical store, same
    // as Desktop's own pendingOrdersCache); the rest receive the raw
    // server message unmodified.
    this.onPendingOrders = onPendingOrders || (() => {});
    this.onOrderClose = onOrderClose || (() => {});
    this.onCloseError = onCloseError || (() => {});
    this.onClosedTrades = onClosedTrades || (() => {});
    this.onPendingOrderCancelled = onPendingOrderCancelled || (() => {});
    this.onPendingCancelError = onPendingCancelError || (() => {});

    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — same plain-
    // callback pattern as every other response above; fires ONLY when
    // an 'error'/'warn' is unambiguously attributable to the single
    // in-flight order:update this file itself sent (see updateSlTp()
    // and _slTpSendingId below). Receives the raw server message
    // unmodified, same as onTradingError/onCloseError.
    this.onSlTpUpdateRejected = onSlTpUpdateRejected || (() => {});

    // PRE-VPS-POLISH-03C.2.6 — close-in-flight guard, deliberately
    // SEPARATE from _orderSending (see submitOrder()'s own note below):
    // order:new and order:close share the generic 'error' message type
    // with overlapping codes (e.g. both can legitimately send
    // price_unavailable), so there is no way to tell from the message's
    // own content alone which action it answers. Mutual exclusion at
    // submission time (submitOrder()/closePosition() each refuse to
    // start while the OTHER is in flight) is what actually guarantees an
    // 'error' is never attributed to the wrong flow — not clever
    // inspection of the error payload itself.
    this._closeSending = false;
    // ISOLATED BUG FIX — token identifying which closePosition() call
    // owns the pending _closeSending slot, so that call's own 20s
    // safety-valve (see closePosition()) can never release a LATER,
    // different close's slot — same `=== key` discipline _slTpSendingId
    // already uses, adapted for _closeSending's plain boolean shape.
    this._closeToken = 0;
    // Single-slot cancel-in-flight guard (one pending-order cancel at a
    // time, not a per-id Set): the backend's failure response
    // ({"type":"warn","message":"order_pending_cancel_<code>"}) carries
    // no id to correlate against, so per-id tracking could not safely
    // release the right slot on failure anyway — a single scalar is the
    // honest minimal guard the real contract actually supports. Success
    // (order_pending_cancel) DOES carry an id, but since only one cancel
    // can ever be in flight here there is nothing to disambiguate.
    this._cancelSendingId = null;

    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — single-slot
    // guard, same shape as _cancelSendingId above (holds the position
    // id as a string while a modification is in flight, null
    // otherwise). updateSlTp() refuses to send while _orderSending/
    // _closeSending/this flag are already set, so at most ONE of the
    // three can ever be true when an 'error'/'warn' response arrives —
    // that mutual exclusion, not inspection of the error payload
    // itself, is what keeps attribution unambiguous (identical
    // reasoning to _closeSending's own comment above).
    this._slTpSendingId = null;

    // PRE-VPS-POLISH-03C.2.5 — order-ticket transient state. Mirrors
    // Desktop's module-level orderTicketState/_ pendingRiskOrder, scoped
    // per-instance here since Mobile has exactly one session (no
    // multi-panel ownership check needed). _orderSending is the single
    // idempotency guard: true from the moment submitOrder() actually
    // sends order:new until order_ack/order_rejected/risk_warning/error
    // resolves it — never cleared by selectSymbol()/selectTimeframe(),
    // so switching symbol while an order is genuinely in flight cannot
    // mask a still-outstanding ack/rejection behind a fresh submit.
    this._orderSending = false;
    // The exact intent of the most recent submitOrder() call — captured
    // only so a risk_warning response can later be turned into an
    // identical risk_confirmed:true resend (see confirmRiskWarning()),
    // never read for any financial computation.
    this._lastOrderIntent = null;
    // Set only while a risk_warning is awaiting the user's explicit
    // CONFIRM/CANCEL — cleared by confirmRiskWarning()/cancelRiskWarning()
    // and never auto-resent, so a stray duplicate confirm click cannot
    // double-submit.
    this._pendingRiskOrder = null;

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

    // PRE-VPS-POLISH-03C.2.5 — same debounce helper, same 200ms constant
    // Desktop's own _requestRiskPreview=debounce(...,200) uses
    // (desktop.html:3428) — a UX-only advisory round-trip, never the
    // real safety gate (the backend re-evaluates risk independently
    // inside order:new regardless of whether this was ever called; see
    // 03C.2.5 preflight Section F).
    this.requestRiskPreview = debounce((symbol, qty) => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try { this.ws.send(JSON.stringify({ action: 'order:risk_preview', symbol, qty })); } catch (_) {}
      }
    }, 200);
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

  // PRE-VPS-POLISH-03C.2.5 — the one Mobile order-submission authority.
  // Sends the EXACT backend contract Desktop's TradingPanel.sendOrder()
  // uses (desktop.html:4443-4461): action:'order:new', the current
  // symbol, side/qty/sl/tp as given, type always 'market' (pending/
  // limit/stop are out of scope for this sub-block), and
  // risk_confirmed:true only when explicitly requested. Deliberately
  // does NOT read qty/sl/tp from any DOM element and does NOT clamp qty
  // to getLotMin() itself — mirroring Desktop's own architecture split
  // exactly: sendActiveOrder() (UI layer, desktop.html:5100-5112) reads
  // the inputs and does the getLotMin() clamp BEFORE calling
  // panel.sendOrder(); TradingPanel.sendOrder() itself trusts whatever
  // qty/sl/tp it is given, with no second clamp or validation inside
  // it. mobile.html's own ticket wiring is this file's equivalent of
  // sendActiveOrder() and performs that same clamp, reusing the real
  // getLotMin()/getLotDecimals() from trading_core.js — never a second
  // lot-size catalog. No price, no accountId, no margin/commission/
  // spread/contract_size/risk-percentage is ever computed or sent here
  // — the backend remains the sole financial authority (03C.2.5
  // preflight, Sections C/H/I/J).
  submitOrder({ side, qty, sl = null, tp = null, riskConfirmed = false } = {}) {
    // PRE-VPS-POLISH-03C.2.6 — also refuses while a close is in flight;
    // see _closeSending's own comment in the constructor for why this
    // mutual exclusion (rather than inspecting the error payload) is
    // what keeps 'error' attribution unambiguous.
    if (this._orderSending || this._closeSending) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (!this.currentSymbol) return;
    const normSide = String(side || '').toLowerCase();
    if (normSide !== 'buy' && normSide !== 'sell') return;
    const payload = {
      action: 'order:new',
      symbol: this.currentSymbol,
      side: normSide,
      type: 'market',
      qty,
      sl: sl ?? null,
      tp: tp ?? null,
    };
    if (riskConfirmed) payload.risk_confirmed = true;
    // Captured BEFORE the send so a risk_warning response can later
    // resend this exact same intent — never re-read from a DOM element
    // that may have changed in the meantime (see confirmRiskWarning()).
    this._lastOrderIntent = { symbol: this.currentSymbol, side: normSide, qty, sl: sl ?? null, tp: tp ?? null };
    this._orderSending = true;
    try {
      this.ws.send(JSON.stringify(payload));
    } catch (_) {
      this._orderSending = false;
    }
  }

  // PRE-VPS-POLISH-03C.2.5 — resends the EXACT original intent captured
  // at submitOrder() time, with risk_confirmed:true added — never a
  // value re-read from the UI at confirm time (which could have
  // silently drifted from what the user actually saw in the risk
  // warning). Deliberately stricter than Desktop's own
  // confirmRiskOrder() (desktop.html:5675-5685), which re-reads #sl/#tp
  // live and executes on whatever this.currentSymbol happens to be at
  // confirm time, not the symbol the warning was actually about — a
  // real, pre-existing Desktop quirk this file does not copy. Refuses
  // (and clears the pending state) if the current symbol has since
  // changed, so a stale confirmation can never execute against the
  // wrong instrument; the backend would reject on symbol/quote grounds
  // regardless, but this is a client-side safety gate, not a financial
  // calculation.
  confirmRiskWarning() {
    const intent = this._pendingRiskOrder;
    if (!intent) return;
    this._pendingRiskOrder = null;
    if (intent.symbol !== this.currentSymbol) return;
    this.submitOrder({ side: intent.side, qty: intent.qty, sl: intent.sl, tp: intent.tp, riskConfirmed: true });
  }

  // PRE-VPS-POLISH-03C.2.5 — discards a pending risk_warning without
  // resending anything. Safe to call even if nothing is pending.
  cancelRiskWarning() {
    this._pendingRiskOrder = null;
  }

  // PRE-VPS-POLISH-03C.2.6 — the one Mobile position-close authority.
  // Sends the EXACT backend contract (consumers.py::_order_close,
  // confirmed by the 03C.2.6 preflight Section F): action:'order:close',
  // id, and qty ONLY when the caller actually provides one — omitting
  // it entirely (not sending null/undefined) is what the backend reads
  // as a full close, exactly like Desktop's own close button (desktop.
  // html:4809-4810, `if(qtyVal!=null...)payload.qty=qtyVal`). No symbol,
  // no side, no price, no entry, no pnl — the preflight confirmed none
  // of those are read by _order_close(). qty stays an accepted optional
  // parameter (the backend already supports partial close) but the
  // Mobile ticket's own Close control never passes one — full close
  // only for this sub-block's UI, per design lock Section 7.
  // ISOLATED BUG FIX — close-position guard never releasing after a
  // blocked/never-sent attempt (confirmed by isolated reproduction: a
  // disconnected tap on × left Mobile's closeInFlight stuck true
  // forever, since the caller set it unconditionally and no backend
  // response was ever going to arrive to release it). Returns true
  // ONLY when a message was actually handed to the socket — exactly
  // the same true/false discipline updateSlTp() already uses below —
  // so the caller (mobile.html) can set its own closeInFlight ONLY
  // when a real send happened, never before confirming it.
  closePosition(id, qty) {
    // Mutual exclusion with submitOrder() — see _closeSending's comment.
    if (this._orderSending || this._closeSending) return false;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    if (id == null) return false;
    const payload = { action: 'order:close', id: String(id) };
    if (qty != null) payload.qty = qty;
    this._closeSending = true;
    const token = ++this._closeToken;
    try {
      this.ws.send(JSON.stringify(payload));
    } catch (_) {
      this._closeSending = false;
      return false;
    }
    // ISOLATED BUG FIX — same safety-valve discipline as updateSlTp()'s
    // own 20s timeout above (see its comment): order:close's real
    // response carries no correlation id either, so if the backend
    // never answers at all (not just a disconnect — a send that
    // genuinely gets no reply), _closeSending would otherwise stay
    // true forever and silently refuse every future close, even after
    // the caller's own shorter UI-facing timeout has already released
    // ITS guard and reported "no confirmado". NOT a success/failure
    // declaration — purely so a lost/dropped response can never
    // deadlock this slot forever. The token check never releases a
    // LATER close's own slot.
    const self = this;
    const safetyValve = setTimeout(() => { if (self._closeSending && self._closeToken === token) self._closeSending = false; }, 20000);
    // Node-harness compatibility only (this file also runs under plain
    // Node in this project's own test scripts, which have no event
    // loop activity besides this timer and don't call process.exit()):
    // a browser's setTimeout() return value has no .unref — guarded,
    // never called there, so production behavior is unchanged.
    if (typeof safetyValve.unref === 'function') safetyValve.unref();
    return true;
  }

  // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — the one
  // Mobile position-SL/TP-modify authority (distinct from submitOrder()
  // 's sl/tp, which only ever applies at new-order creation time).
  // Sends the EXACT real backend contract (consumers.py::_order_update,
  // confirmed against Desktop's own drag-lines/sheet editor, which use
  // this identically): {action:'order:update', id, symbol, sl, tp}.
  //
  // Deliberately requires the caller to pass id/symbol explicitly —
  // NEVER falls back to this.currentSymbol itself (an Owner-mandated
  // correction: the caller must always use the canonical position's
  // OWN id/symbol, since a position's symbol need not equal whatever
  // is currently selected on screen). Refuses and returns false,
  // sending nothing, if either is missing, so the caller can report
  // state instead of silently failing.
  //
  // sl/tp are passed through to the wire EXACTLY as given — this
  // method never substitutes null for an unspecified field itself;
  // the caller (mobile.html) must always pass the field being edited's
  // new value AND the other field's current confirmed value (or an
  // explicit null, only when the user explicitly requested removal).
  //
  // Mutually exclusive with submitOrder()/closePosition() and with
  // itself (_slTpSendingId, see its own comment in the constructor) —
  // refuses while any trading action already owns the connection, the
  // same unambiguous-attribution discipline every other send method
  // here already follows.
  updateSlTp({ id, symbol, sl, tp } = {}) {
    if (id == null || !symbol) return false;
    if (this._orderSending || this._closeSending || this._slTpSendingId != null) return false;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    const key = String(id);
    this._slTpSendingId = key;
    try {
      this.ws.send(JSON.stringify({ action: 'order:update', id: key, symbol, sl, tp }));
    } catch (_) {
      this._slTpSendingId = null;
      return false;
    }
    // Safety-valve only (see the class-level comment at the top of this
    // file) — order:update carries no correlation id, so a lost/
    // dropped response could otherwise block this slot forever. NOT a
    // success/failure declaration: the caller's own, separate, shorter
    // UI-facing timeout owns that distinction. Auto-releases only if
    // nothing has already resolved THIS exact request (the `=== key`
    // check never clears a newer request's slot).
    const self = this;
    setTimeout(() => { if (self._slTpSendingId === key) self._slTpSendingId = null; }, 20000);
    return true;
  }

  // Lets the caller release the single-slot guard as soon as IT has
  // independently determined the real outcome (a matching 'positions'
  // snapshot confirming success — see this file's class-level comment:
  // that match/no-match comparison is the caller's job, never this
  // file's, since the backend's 'positions' push carries no request
  // id to correlate against here). No-ops if the given id doesn't
  // match the currently-pending one, so it can never release a NEWER
  // request's slot by mistake.
  clearSlTpPending(id) {
    if (this._slTpSendingId != null && this._slTpSendingId === String(id)) this._slTpSendingId = null;
  }

  // PRE-VPS-POLISH-03C.2.6 — the one Mobile pending-order-cancel
  // authority. Sends the EXACT backend contract (consumers.py::
  // _order_pending_cancel): action:'order:pending:cancel', id. Single-
  // slot guard (_cancelSendingId) — see its own comment in the
  // constructor. No pending-order creation/edit here (the backend's own
  // pending-order create/update actions stay out of scope — design lock
  // Section 13).
  cancelPendingOrder(id) {
    if (this._cancelSendingId != null) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (id == null) return;
    const key = String(id);
    this._cancelSendingId = key;
    try {
      this.ws.send(JSON.stringify({ action: 'order:pending:cancel', id: key }));
    } catch (_) {
      this._cancelSendingId = null;
    }
  }

  // PRE-VPS-POLISH-03C.2.6 — closed trades are NOT auto-pushed on
  // connect (unlike positions/pending — confirmed by the 03C.2.6
  // preflight Section C/I: only send_positions_snapshot()/
  // _refresh_and_send_pending_orders()/_recalc_account_and_push() run
  // inside the backend's own connect()). Mobile's chosen strategy
  // (design lock Section 15): request explicitly, only when the UI
  // actually activates the Closed tab — never automatically on
  // connect/reconnect, so a user who never opens that tab never
  // generates this round-trip at all; re-opening the tab later simply
  // re-requests the latest snapshot (acceptable, user-driven, not
  // periodic/automatic spam).
  requestClosedTrades() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ action: 'get_closed_trades' })); } catch (_) {}
    }
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

  // ISOLATED FIX (Mobile desconexión/reconexión) — every one of this
  // socket's own four handlers below (onopen/onmessage/onerror/
  // onclose) now starts by checking `this.ws !== sock` — `sock` is the
  // EXACT object this specific connect() call created, captured in a
  // local const at the moment it's constructed, never re-read from
  // `this.ws` afterward (which is a mutable field connect() itself
  // reassigns on every call). The instant a LATER connect() call
  // replaces `this.ws` with a newer socket, every one of THIS old
  // socket's handlers becomes a no-op — it can never again touch
  // session state, call _handleMsg(), flip the status badge, or
  // schedule a reconnect that would race the new connection's own.
  // This is connection identity, not request identity — it says
  // nothing about (and does not attempt to solve) two requests sent
  // over the SAME still-current connection; that remains open, tracked
  // separately, deliberately not addressed here.
  connect() {
    if (this.connecting || (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING))) return;
    this.connecting = true;
    this.onStatusChange('CONNECTING');
    let sock;
    try {
      sock = new WebSocket(this.wsUrl());
    } catch (e) {
      this.connecting = false;
      this.onStatusChange('DISCONNECTED');
      return;
    }
    this.ws = sock;
    sock.onopen = () => {
      if (this.ws !== sock) return;
      this.onStatusChange('CONNECTED');
      this.reconnDelay = 800;
      this.connecting = false;
      clearInterval(this.hb);
      this.hb = setInterval(() => { try { sock.send('{"action":"ping"}'); } catch (_) {} }, 15000);
      // PRE-VPS-POLISH-03C.2.3 — reconnect symbol restoration, mirroring
      // Desktop's connect()/onopen exactly: every new connection
      // (including every reconnect) re-sends change_symbol for
      // whatever currentSymbol already is, so a user selection survives
      // a dropped socket instead of silently drifting back to the
      // backend's own per-connection default symbol.
      if (this.currentSymbol) {
        try { sock.send(JSON.stringify({ action: 'change_symbol', symbol: this.currentSymbol })); } catch (_) {}
      }
      // PRE-VPS-POLISH-03C.2.4 — reconnect timeframe restoration, same
      // rationale as the symbol resend above (Desktop's connect()/onopen
      // always resends both). currentTF always has a real value, so this
      // always fires. History is only (re-)requested once a symbol is
      // actually selected — load_history needs a real symbol, and until
      // one is chosen there is nothing to request yet.
      try { sock.send(JSON.stringify({ action: 'change_timeframe', timeframe: this.currentTF })); } catch (_) {}
      if (this.currentSymbol) this._requestHistory();
    };
    sock.onmessage = ev => {
      if (this.ws !== sock) return;
      try { this._handleMsg(JSON.parse(ev.data)); } catch (_) {}
    };
    sock.onerror = () => {
      if (this.ws !== sock) return;
      this.onStatusChange('DISCONNECTED');
    };
    sock.onclose = () => {
      if (this.ws !== sock) return;
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
  // history/candle_new/candle_update/volume_update (03C.2.4) +
  // positions/order_ack/order_rejected/risk_warning/risk_preview/error
  // (03C.2.5) + pending_orders/order_close/order_pending_cancel/
  // closed_trades_snapshot/warn (03C.2.6). Pending-order creation/edit
  // and position-SL/TP-edit feedback (order_pending_new/
  // order_pending_update/order_update) remain intentionally unhandled
  // here — out of scope until their own separately-authorized sub-block.
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

    // PRE-VPS-POLISH-03C.2.5 — positions: the backend pushes this
    // automatically after every successful order:new (consumers.py's
    // _order_new -> _refresh_and_send_positions(), confirmed by the
    // 03C.2.5 preflight, Section L) — Mobile never has to request it
    // itself after an order. Reuses the REAL canonical store
    // (getCanonicalPositions/replaceCanonicalPositions, trading_core.js)
    // Desktop's own multiple panels already share — no second store, no
    // P&L computation (pos.pnl, when present, is already backend-
    // authoritative; this file never reads or recomputes it).
    if (msg.type === 'positions' && Array.isArray(msg.items)) {
      replaceCanonicalPositions(msg.items);
      this.onPositions(getCanonicalPositions());
      return;
    }

    // order_ack/order_rejected/risk_warning/error below are only ever
    // sent by the backend in direct response to a order:new this file
    // itself sent (consumers.py::_order_new) — guarding on
    // this._orderSending mirrors Desktop's own "only the panel that
    // owns the in-flight ticket may resolve it" ownership check
    // (desktop.html:3439/3491/3496/3646), adapted to Mobile's single-
    // session model: an error/ack/warning that arrives with no order
    // actually in flight (e.g. a future, unrelated error type) is
    // never misreported as a trading outcome.
    if (msg.type === 'order_ack') {
      if (this._orderSending) {
        this._orderSending = false;
        this.onOrderAck(msg);
      }
      return;
    }
    if (msg.type === 'order_rejected') {
      if (this._orderSending) {
        this._orderSending = false;
        this.onOrderRejected(msg);
      }
      return;
    }
    if (msg.type === 'risk_warning') {
      if (this._orderSending) {
        this._orderSending = false;
        this._pendingRiskOrder = this._lastOrderIntent;
        this.onRiskWarning(msg);
      }
      return;
    }
    if (msg.type === 'error') {
      // PRE-VPS-POLISH-03C.2.6 — order:new and order:close share this
      // generic type with overlapping codes (both can send
      // price_unavailable, for example) — _closeSending/_orderSending
      // are mutually exclusive at submission time (see submitOrder()/
      // closePosition()), so checking _closeSending FIRST is always
      // unambiguous: only one of the two can ever be true here.
      if (this._closeSending) {
        this._closeSending = false;
        this.onCloseError(msg);
        return;
      }
      if (this._orderSending) {
        this._orderSending = false;
        this.onTradingError(msg);
        return;
      }
      // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — order:
      // update shares this same generic 'error' type and some codes
      // (price_unavailable, invalid_sl_value/invalid_tp_value/
      // invalid_sl_direction/invalid_tp_direction) with order:new's own
      // validation path. updateSlTp() refuses to send while
      // _orderSending/_closeSending are active, so by the time
      // execution reaches here at most one of the three guards is ever
      // true — never attributed by inspecting the error's own code.
      if (this._slTpSendingId != null) {
        this._slTpSendingId = null;
        this.onSlTpUpdateRejected(msg);
      }
      return;
    }
    // PRE-VPS-POLISH-03C.2.6 — 'warn' is the real backend type for a
    // close/cancel that found nothing to act on (order_close_not_found/
    // order_pending_cancel_not_found) — distinguished by the message's
    // own prefix (the two are literally different strings), not by
    // guessing from state, since in principle either guard could be
    // armed at once (closing one position while cancelling an unrelated
    // pending order is a legitimate, independent pair of in-flight
    // actions).
    if (msg.type === 'warn') {
      const warnMsg = String(msg.message || '');
      if (this._closeSending && warnMsg.indexOf('order_close_not_found') === 0) {
        this._closeSending = false;
        this.onCloseError(msg);
        return;
      }
      if (this._cancelSendingId != null && warnMsg.indexOf('order_pending_cancel_') === 0) {
        this._cancelSendingId = null;
        this.onPendingCancelError(msg);
        return;
      }
      // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — real
      // backend warn for an order:update whose id/symbol matched no
      // open position (consumers.py::_order_update, literal message
      // 'order_update_not_found').
      if (this._slTpSendingId != null && warnMsg.indexOf('order_update_not_found') === 0) {
        this._slTpSendingId = null;
        this.onSlTpUpdateRejected(msg);
        return;
      }
      return;
    }
    // risk_preview is purely advisory (see 03C.2.5 preflight, Section F)
    // and can legitimately arrive without an order in flight — no
    // _orderSending guard here, plain passthrough of the real backend
    // response; no local risk computation.
    if (msg.type === 'risk_preview') {
      this.onRiskPreview(msg);
      return;
    }

    // PRE-VPS-POLISH-03C.2.6 — pending_orders: forwarded as the raw
    // array Desktop's own pendingOrdersCache receives unconditionally
    // on every snapshot (desktop.html:3631-3633) — no canonical store
    // exists for this (confirmed by the 03C.2.6 preflight, Section H:
    // pendingOrdersCache is a plain Desktop-local variable, not a
    // trading_core.js shared store) — so this file does not invent one
    // either; the orchestrator (mobile.html) owns whatever array this
    // callback hands it.
    if (msg.type === 'pending_orders' && Array.isArray(msg.items)) {
      this.onPendingOrders(msg.items);
      return;
    }
    // order_close: real backend response to closePosition() — plain
    // passthrough, never recomputed. The backend already follows this
    // with its own _recalc_account_and_push()/_refresh_and_send_positions()
    // (confirmed by the 03C.2.6 preflight, Section F) — this file never
    // mutates the canonical positions store itself in response to this
    // message; it only waits for the real 'positions' snapshot above.
    if (msg.type === 'order_close') {
      this._closeSending = false;
      this.onOrderClose(msg);
      return;
    }
    // order_pending_cancel: real backend ack for cancelPendingOrder().
    if (msg.type === 'order_pending_cancel') {
      this._cancelSendingId = null;
      this.onPendingOrderCancelled(msg);
      return;
    }
    // closed_trades_snapshot: real backend response to
    // requestClosedTrades() — plain passthrough of msg.trades, never a
    // fabricated commission/fee/close_reason field (none exist on this
    // contract — confirmed by the 03C.2.6 preflight, Section I).
    if (msg.type === 'closed_trades_snapshot') {
      this.onClosedTrades(Array.isArray(msg.trades) ? msg.trades : []);
      return;
    }
  }
}
