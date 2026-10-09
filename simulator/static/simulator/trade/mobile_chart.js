/* PRE-VPS-POLISH-03C.2.4 — Mobile Trading Chart foundation.
   Mobile-only presentation layer: chart + candle/volume bars. Shares
   nothing with Desktop's trading panel class (no inheritance, no
   reused chart code, no event bus) — loads the exact same Lightweight
   Charts 4.1.1 build Desktop uses (same CDN + jsdelivr-fallback
   pattern), and reuses trading_core.js's existing pure helpers
   (normTime/n/volPointForBar) for candle normalization — never a
   second implementation of them.

   Owns ONLY: chart, candleSeries, volumeSeries, bars. Explicitly NO:
   WebSocket, account state, orders, open trades, P&L, risk, financial
   calculations. MobileTradingSession (mobile_session.js) owns the
   connection/symbol/timeframe/message parsing and forwards already-
   guarded, already-symbol/timeframe-validated data here via plain
   callbacks wired in mobile.html — this class never reads msg.* or
   touches window.__TRADE_CONFIG__ or the shared quote-tick gate at all. */

class MobileTradingChart {
  constructor(containerEl) {
    this.containerEl = containerEl;
    this.chart = null;
    this.candleSeries = null;
    this.volumeSeries = null;
    this.bars = [];
    this._resizeObs = null;
    // ISOLATED FIX — symbol/scale coherence for SL/TP editing. The
    // symbol whose candle data is CURRENTLY ACCEPTED into candleSeries
    // — null whenever that is unknown/invalidated (right after clear(),
    // i.e. the instant a symbol/timeframe switch starts, and before the
    // first real history for the NEW symbol has actually been applied).
    // Confirmed root cause (POST-MOBILE-CERT audit): session.
    // selectSymbol() flips session.currentSymbol SYNCHRONOUSLY, before
    // any network round-trip, so currentSymbolPositions() already
    // returns the NEW symbol's positions while candleSeries can still
    // hold the OLD symbol's (wildly different-scale) data — tapping
    // "+ TP"/"+ SL" during that window computed a price via
    // priceToCoordinate()/coordinateToPrice() against the WRONG
    // instrument's scale (reproduced in isolation: BTCUSD-scale candles
    // loaded, EUR/USD position's default TP computed via the stale
    // scale came back wrong by >20x — same mechanism plausibly explains
    // Position 397's real-world 153745.18427 TP). isSlTpEditableFor()
    // below is the single gate every SL/TP add/drag/confirm path must
    // pass; mobile_session.js already drops any 'history' message whose
    // own symbol no longer matches the LIVE session.currentSymbol at
    // arrival time (see its _handleMsg, unchanged here) — out-of-order
    // responses for an abandoned symbol are already filtered out
    // BEFORE they would ever reach applyHistory(), so this field is
    // never set from a stale, wrong-symbol message.
    this._historySymbol = null;
    // ISOLATED FIX (Parte A — ventana de temporalidad) — a pure
    // client-side generation counter, SEPARATE from the symbol string
    // above: bumped by clear() every time EITHER a symbol OR a
    // timeframe switch starts (renderTfSelector's own click handler
    // calls clear() exactly like the watchlist's does — confirmed by
    // reading mobile.html — but, unlike the watchlist, never follows
    // it with refreshPositionLines(), so nothing else was re-
    // evaluating readiness for a timeframe-only switch at all before
    // this fix). _readyGeneration records which generation the LAST
    // successful applyHistory() actually satisfied. isSlTpEditableFor()
    // below requires them to match — so a stale _historySymbol value
    // that still happens to equal the right string (same instrument,
    // different timeframe) can never alone look "ready" again until a
    // real apply happens AFTER the most recent clear().
    this._historyGeneration = 0;
    this._readyGeneration = -1;
    // Multi-position correction round — see setPositions() below. One
    // native price line PER OPEN POSITION for the current symbol
    // (keyed by canonical id) — every position gets its own real Entry
    // line at its own real pos.avg, never averaged/merged/synthesized.
    // SL/TP native lines stay single (_slLine/_tpLine) because only the
    // SELECTED position's SL/TP are ever drawn.
    this._entryLines = {};
    this._slLine = null;
    this._tpLine = null;
    // 2nd correction round — HTML overlay pills (entry/TP/SL), positioned
    // via priceToCoordinate() (pure coordinate conversion, not a
    // financial calculation) and onCloseClick, a plain callback the page
    // wires to the REAL session.closePosition() — this class still never
    // touches session/WS/account state directly.
    this._posOverlayEls = null;
    // Multi-position correction round — ALL canonical positions for the
    // current symbol (the caller, mobile.html, already filters by
    // symbol — this file never re-filters/re-queries the store itself)
    // and which one's full capsule + SL/TP controls are shown.
    // selectedId persists across setPositions() calls as long as that
    // id is still present in the fresh list — see setPositions() for
    // the exact reselection rule.
    this._lastPositions = [];
    this._selectedId = null;
    this.onCloseClick = null;
    // Multi-position correction round — plain callback, same shape/
    // discipline as onCloseClick: fired with a canonical position id
    // ONLY when the user taps a position's capsule/compact chip (full
    // or compact) body — never its × button. mobile.html owns deciding
    // whether/how selectedPositionId actually changes (e.g. never
    // automatically while a different position has a pending SL/TP
    // edit) — this class only ever reports the tap.
    this.onSelectPosition = null;
    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP-on-open-position
    // editing (audited-then-authorized sub-block). Plain callback, same
    // shape/discipline as onCloseClick above: this class only ever
    // DRAWS and reports a user gesture — it never touches session/WS
    // itself and never sends anything. Called with (posId, field, price)
    // ONLY when the user explicitly taps Confirm (drag-release-confirm
    // or the exact-price input's own Confirm) — never during a live
    // drag/typing, and never automatically. The caller (mobile.html) is
    // responsible for building the real order-update payload (which
    // means preserving the OTHER field's current confirmed value,
    // never defaulting it to null) and for calling
    // session.updateSlTp().
    this.onSlTpConfirm = null;
    // Single in-progress SL/TP edit, max one at a time (one of the two
    // fields, never both): { field:'sl'|'tp', pos, mode, previewPrice,
    // pointerId, startClientY, startPrice, moved, isNew }. mode is one
    // of 'drag' (pointer actively down, moving) | 'edit' (not currently
    // being dragged — draggable again AND an editable exact-price input
    // + Confirmar/Cancelar are shown; entered either by releasing a
    // drag, by tapping an existing pill without moving it, or directly
    // by tapping "+ Añadir SL/TP") | 'sending' (Confirm already tapped,
    // order-update already sent, awaiting the real backend outcome —
    // locked, no further interaction). isNew is true only for a level
    // that did not exist on the canonical position before this edit
    // started (the "+ Añadir" path) — used purely to pick the
    // provisional-vs-editing visual treatment, never read for anything
    // sent to the backend. null when idle.
    this._edit = null;
    // Tracks whether this.chart's own pan/zoom handlers were disabled
    // for an active drag, so they can always be restored exactly once
    // — belt-and-suspenders alongside the pill's own touch-action:none
    // + stopPropagation()/preventDefault(), guaranteeing the SAME
    // gesture that drags a label never also pans/zooms the chart.
    this._gesturesDisabled = false;
    // Item 3 (6-item correction round) — per-instrument display precision.
    // Set by setSymbolFormat() from the REAL, already-existing
    // priceFormatFor(sym) (trading_core.js, the exact same helper
    // Desktop's own chart/badges already use — desktop.html calls
    // candleSeries.applyOptions({priceFormat:priceFormatFor(symbol)})
    // and formats badge text via .toFixed(priceFormatFor(symbol)
    // .precision) in multiple places). Pure display formatting — never
    // read by, or fed back into, any price/engine calculation.
    this._priceFormat = null;
  }

  // Identical two-tier load to Desktop's initChart(): a <script> tag in
  // the page already attempts the unpkg build; if window.LightweightCharts
  // still isn't defined by the time this runs, fall back to the same
  // jsdelivr URL Desktop's own fallback uses. Desktop's own loader is
  // never touched or reused directly — this is Mobile's own copy of the
  // same two-CDN pattern, not shared code.
  async initialize() {
    if (this.chart) return;
    if (!window.LightweightCharts) {
      await new Promise((res) => {
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js';
        s.onload = res;
        s.onerror = res;
        document.head.appendChild(s);
      });
    }
    if (!window.LightweightCharts || !this.containerEl) return;
    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH — background matches the
    // Visual Contract V1's own page background (--bg, "noche"/default)
    // exactly, so the chart blends into the page instead of reading as
    // a boxed card — the reference's own .chart has no fill of its own.
    this.chart = LightweightCharts.createChart(this.containerEl, {
      layout: { background: { type: 'solid', color: '#0B0E13' }, textColor: '#8A94A6' },
      grid: { vertLines: { color: 'rgba(255,255,255,.03)' }, horzLines: { color: 'rgba(255,255,255,.03)' } },
      timeScale: { borderColor: 'rgba(255,255,255,.06)' },
      rightPriceScale: { borderColor: 'rgba(255,255,255,.06)', scaleMargins: { top: 0.08, bottom: 0.02 } },
    });
    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH — candle colors match the
    // approved Visual Contract V1 (money-broker-movil/tema.css's
    // --candle-up/--candle-down, "noche" values) exactly — presentation
    // only, same two hardcoded hex pairs as before, no data change.
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: '#2EBD85', downColor: '#F6465D',
      wickUpColor: '#2EBD85', wickDownColor: '#F6465D',
      borderUpColor: '#2EBD85', borderDownColor: '#F6465D',
      priceLineVisible: false,
    });
    this.volumeSeries = this.chart.addHistogramSeries({
      priceScaleId: 'mob-vol', priceFormat: { type: 'volume' },
      priceLineVisible: false, lastValueVisible: false,
      color: 'rgba(255,255,255,0.07)',
    });
    this.chart.priceScale('mob-vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false, borderVisible: false });
    this.chart.timeScale().fitContent();
    if (typeof ResizeObserver !== 'undefined') {
      this._resizeObs = new ResizeObserver(() => this.resize());
      this._resizeObs.observe(this.containerEl);
    }
    // Multi-position correction round, item 2 — "reposicionarse... se
    // desplaza el gráfico" (pan/zoom). Lightweight Charts never fires
    // resize()/appendCandle()/etc. for a manual pan or pinch-zoom (only
    // the time-scale's own visible range actually changes), so without
    // this subscription Entry/SL/TP/add-affordance overlays would stay
    // frozen mid-pan. subscribeVisibleLogicalRangeChange is pure
    // coordinate-mapping plumbing — no financial logic. typeof-guarded:
    // the Node real-execution harness's FakeChart mock has no
    // timeScale().subscribe* method.
    if (this.chart && typeof this.chart.timeScale === 'function') {
      const ts = this.chart.timeScale();
      if (ts && typeof ts.subscribeVisibleLogicalRangeChange === 'function') {
        ts.subscribeVisibleLogicalRangeChange(() => this._repositionOverlay());
      }
    }
    // typeof-guarded: getComputedStyle is a browser-only global, absent
    // in the Node-based real-execution test harness (and any other
    // non-browser host) — this.containerEl there is a plain {} fixture,
    // never a real DOM node, so skipping here is correct, not a hack.
    if (typeof getComputedStyle === 'function' && getComputedStyle(this.containerEl).position === 'static') {
      this.containerEl.style.position = 'relative';
    }
    this.applyThemeColors();
  }

  // 2nd correction round — reads the REAL --bg/--muted/--line/--candle-up/
  // --candle-down custom properties (declared on .mob-foundation,
  // mobile.html) via getComputedStyle — the same technique
  // money-broker-movil/grafico.html's own app.js/render() already uses
  // for its demo candles — and hands them to Lightweight Charts'
  // applyOptions(). Pure color/presentation; no data, no financial
  // computation. Callable any time (initialize(), and again whenever
  // mobile.html detects data-mb-theme changed) so both "noche" and "día"
  // render the chart with the correct palette without touching the
  // engine or requiring Account to exist yet.
  applyThemeColors() {
    if (!this.chart || !this.containerEl || typeof getComputedStyle !== 'function') return;
    const cs = getComputedStyle(this.containerEl);
    const v = (name, fallback) => { const x = (cs.getPropertyValue(name) || '').trim(); return x || fallback; };
    const bg = v('--bg', '#0B0E13');
    const text = v('--muted', '#8A94A6');
    const line = v('--line', 'rgba(255,255,255,.06)');
    const up = v('--candle-up', '#2EBD85');
    const down = v('--candle-down', '#F6465D');
    this.chart.applyOptions({
      layout: { background: { type: 'solid', color: bg }, textColor: text },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      timeScale: { borderColor: line },
      rightPriceScale: { borderColor: line },
    });
    this.candleSeries?.applyOptions({
      upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderUpColor: up, borderDownColor: down,
    });
  }

  resize() {
    if (!this.chart || !this.containerEl) return;
    this.chart.applyOptions({ width: this.containerEl.clientWidth, height: this.containerEl.clientHeight });
    this._repositionOverlay();
  }

  // Item 3 (6-item correction round) — "Ajusta la precisión del eje y
  // etiquetas del chart a la especificación existente del instrumento:
  // EUR/USD no puede mostrarse simplemente como 1.12 cuando el precio
  // es 1.12098." Reads the REAL, already-existing per-symbol spec
  // (priceFormatFor(sym), trading_core.js — precision 2 for BTC/ETH, 3
  // for .../JPY, 5 otherwise) and applies it to the candle series'
  // OWN priceFormat option, exactly the same call Desktop's chart
  // already makes (desktop.html: candleSeries.applyOptions({priceFormat:
  // priceFormatFor(this.currentSymbol)})). This is chart DISPLAY
  // configuration, not a new calculation — Lightweight Charts itself
  // then formats the price axis and every native createPriceLine()
  // axis label at this precision automatically; no engine price or
  // computation changes. Called from mobile.html's onQuote() on every
  // tick (idempotent/cheap) so the format is always correct for
  // whichever symbol is currently live, without a dedicated symbol-
  // change hook.
  setSymbolFormat(sym) {
    if (!this.candleSeries || !sym) return;
    const pf = priceFormatFor(sym);
    if (this._priceFormat && this._priceFormat.precision === pf.precision && this._priceFormat.minMove === pf.minMove) return;
    this._priceFormat = pf;
    this.candleSeries.applyOptions({ priceFormat: { type: 'price', precision: pf.precision, minMove: pf.minMove } });
  }

  // Same precision used by the price axis/price-lines above, applied to
  // the entry/TP/SL overlay pill text (_renderEntryCapsule()/
  // _renderSelectedSlTp() below) so both read consistently for the
  // current instrument — never the generic
  // fmt() 5-decimal/trailing-zero-strip default, which is what produced
  // the under-precision the Owner flagged.
  _fmtPx(v) {
    const p = this._priceFormat ? this._priceFormat.precision : 5;
    return Number(v).toFixed(p);
  }

  // Symbol switch / timeframe switch: blank both series immediately so
  // the chart never shows a foreign symbol's/timeframe's data while new
  // history is in flight — same root-cause fix as Desktop's
  // GOLDEN-MARKETDATA-CRYPTO-01 (visual reset must happen independently
  // of the bars array reset).
  clear() {
    this.bars = [];
    this.candleSeries?.setData([]);
    this.volumeSeries?.setData([]);
    // ISOLATED FIX — the instant ANY switch starts (symbol OR
    // timeframe — renderTfSelector's own click handler calls clear()
    // exactly like the watchlist's does), SL/TP editing must stop
    // being available until the new one's real history actually
    // lands. Bumping the generation (rather than only clearing
    // _historySymbol) is what also covers a TIMEFRAME-only switch:
    // _historySymbol can legitimately still equal the right symbol
    // string (the instrument didn't change), but _readyGeneration now
    // lags behind _historyGeneration, so isSlTpEditableFor() still
    // correctly reports "not ready" until a fresh apply happens.
    this._historySymbol = null;
    this._historyGeneration++;
    // ISOLATED FIX — re-render the selected position's SL/TP section
    // RIGHT NOW, synchronously, so any pill/add-affordance left over
    // from before this clear() is immediately replaced by "Cargando
    // gráfico…" — previously nothing re-rendered until the NEXT
    // setPositions()/applyHistory() call, which for a pure timeframe
    // switch (renderTfSelector never calls refreshPositionLines())
    // could be arbitrarily later, leaving a stale, still-clickable
    // control bound to the position that was selected before the
    // switch.
    this._rerenderSelected();
  }

  // ISOLATED FIX (Mobile desconexión/reconexión) — dedicated
  // invalidation for a connection-state change, deliberately NOT just
  // "call clear() blindly": confirmed by reading clear() above, it
  // already only ever touches bars/candleSeries/volumeSeries data,
  // _historySymbol, and the generation counter — it never touches
  // canonical positions, selectedPositionId, or pendingSlTp (all
  // orchestrator-level, owned by mobile.html/trading_core.js, never by
  // this class), so reusing it here is safe on that front. What it
  // does NOT do on its own — because a symbol/timeframe switch always
  // has a synchronous setPositions() call right after it that already
  // abandons a non-'sending' edit via its own stillHasSelected/
  // editStillSelected logic — is cancel an in-progress, NOT-YET-SENT
  // preview. A disconnect has no such follow-up, so that cancellation
  // has to happen explicitly, here. An edit already in 'sending' mode
  // (confirmed, already sent, awaiting the real backend's resolution)
  // is deliberately left untouched: mobile.html's own pendingSlTp is
  // the real tracking-by-id for that request, entirely independent of
  // this field, and must keep resolving via the normal match/reject/
  // timeout path — reconnecting must never be treated as success, and
  // must never trigger a resend.
  invalidateForConnectionChange() {
    this.clear();
    if (this._edit && this._edit.mode !== 'sending') {
      this._abandonEdit();
      this._rerenderSelected();
    }
  }

  // ISOLATED FIX — the one gate every SL/TP add/drag/confirm path must
  // pass before touching priceToCoordinate()/coordinateToPrice() for a
  // position. True only once candleSeries genuinely holds THIS exact
  // symbol's own data (set by applyHistory() below), that data is
  // non-empty (never "ready" during the empty/just-cleared transition
  // either), AND no clear() has run since that data was accepted (the
  // generation check — covers a timeframe-only switch, where the
  // symbol string alone would otherwise still match). Never consults
  // pos.avg, a live price, or any other fallback — a false result
  // means every caller below must refuse, not substitute.
  isSlTpEditableFor(symbol) {
    return !!symbol && this._historySymbol === symbol
      && this._readyGeneration === this._historyGeneration
      && Array.isArray(this.bars) && this.bars.length > 0;
  }

  // Multi-position correction round — the ONE entry point for "fresh
  // canonical truth arrived" (symbol switch, a position opening/
  // closing, or any real 'positions' snapshot). `positions` is the
  // FULL array of already-symbol-filtered canonical positions the
  // caller (mobile.html) obtained from getCanonicalPositions() — this
  // class never re-queries/re-filters that store itself, never nets/
  // averages/merges them, and never invents a synthetic position.
  // `selectedId` is whichever canonical id the caller has decided is
  // "selected" (that decision — including the reselect-on-disappear
  // rule — lives entirely in mobile.html; this class only draws
  // whatever it is given).
  //
  // Draws ONE native Entry price line per position, each at its OWN
  // real pos.avg (never aligned/averaged against current price or
  // against each other) plus a capsule per position (full, interactive
  // controls for the selected one; a small compact chip, tap-to-select,
  // for every other one) — see _renderAllEntries()/_renderEntryCapsule()
  // below. SL/TP lines/zones/add-affordances/edit controls are drawn
  // ONLY for the selected position (see _renderSelectedSlTp() below) —
  // non-selected positions' SL/TP are deliberately not drawn as lines,
  // per this round's own authorization.
  setPositions(positions, selectedId) {
    if (!this.candleSeries) return;
    const list = Array.isArray(positions) ? positions : [];
    const selIdStr = selectedId != null ? String(selectedId) : null;
    const stillHasSelected = selIdStr != null && list.some((p) => String(p.id) === selIdStr);
    // Same "keep a 'sending' edit alive through an unrelated refresh"
    // rule as before, now ALSO requiring the edited position to still
    // BE the one selected (not merely present somewhere in the fresh
    // list) — otherwise a manual tap to select a DIFFERENT position
    // while an old one's order-update is in flight would leave an
    // orphaned, invisible _edit permanently blocking any new edit
    // attempt on the newly-selected position (its own mode/field could
    // collide with the stale 'sending' guard in _onPillPointerDown).
    // Abandoning it here only clears THIS class's own drawing state —
    // it never touches mobile.html's independent pendingSlTp
    // bookkeeping, so the real in-flight request keeps resolving
    // normally (via the next matching 'positions' snapshot, a
    // rejection, or the timeout) regardless of what is visually
    // selected.
    const editStillSelected = this._edit && stillHasSelected && String(this._edit.pos.id) === selIdStr;
    const keepSending = this._edit && this._edit.mode === 'sending' && editStillSelected;
    if (this._edit && !keepSending) this._abandonEdit();
    this._clearAllLines();
    this._clearOverlay();
    this._lastPositions = list;
    this._selectedId = stillHasSelected ? selIdStr : null;
    if (!list.length) return;
    this._renderAllEntries(list);
    const sel = this._selectedPos();
    if (sel) this._renderSelectedSlTp(sel);
    this._repositionOverlay();
  }

  // Convenience for "nothing to show" (symbol switch, no open position
  // for this symbol) — identical to setPositions([], null).
  clearPositions() {
    this.setPositions([], null);
  }

  _selectedPos() {
    if (this._selectedId == null) return null;
    return this._lastPositions.find((p) => String(p.id) === String(this._selectedId)) || null;
  }

  _entryLineColor() {
    // Item 2 (6-item correction round) — the entry line reuses the SAME
    // dedicated, deliberately-non-theme-reactive --entry-line token the
    // entry pill/capsule use (tema.css: noche #E8ECF2, día #0A0A0A —
    // NOT --text/--surface-2, which DO flip with theme). '#E8ECF2'
    // stays as the fallback for the non-browser test harness (no
    // getComputedStyle there).
    let lineColor = '#E8ECF2';
    if (typeof getComputedStyle === 'function' && this.containerEl) {
      const v = (getComputedStyle(this.containerEl).getPropertyValue('--entry-line') || '').trim();
      if (v) lineColor = v;
    }
    return lineColor;
  }

  _clearAllLines() {
    if (!this.candleSeries) { this._entryLines = {}; this._slLine = null; this._tpLine = null; return; }
    Object.keys(this._entryLines).forEach((id) => { this.candleSeries.removePriceLine(this._entryLines[id]); });
    this._entryLines = {};
    if (this._slLine) { this.candleSeries.removePriceLine(this._slLine); this._slLine = null; }
    if (this._tpLine) { this.candleSeries.removePriceLine(this._tpLine); this._tpLine = null; }
  }

  // Multi-position correction round — the ONE method the caller
  // (mobile.html) uses to tell this class "the edit for this field is
  // over, trust whatever canonical state is already loaded" — covers
  // EVERY terminal outcome uniformly:
  //  - declined to even send (missing id/symbol, or another trading
  //    action already owns the connection) — this._lastPositions is
  //    still the pre-edit list, so this simply reverts the frozen
  //    preview;
  //  - backend rejected (price_unavailable/invalid_*) — same revert,
  //    canonical state never changed server-side;
  //  - UI-facing timeout (no response within the caller's own, shorter
  //    window) — same revert; "cambio no confirmado" is the caller's
  //    own status text, this file draws no such message itself;
  //  - confirmed success — the caller must call refreshPositionLines()
  //    (updating this._lastPositions to the NEW canonical sl/tp) BEFORE
  //    calling this, so the very same revert-to-canonical logic then
  //    shows the newly-confirmed value instead of the old one.
  // No-ops if there is no matching in-progress edit (defensive only;
  // also naturally covers "the position already disappeared", since
  // setPositions() already cleared _edit on its own by then).
  resolveSlTpEdit(field) {
    if (!this._edit || this._edit.field !== field) return;
    this._abandonEdit();
    this._rerenderSelected();
  }

  // Internal — drops whatever local drag/tap/confirm/sending UI is in
  // progress and restores normal chart interaction (handleScroll/
  // handleScale), WITHOUT touching this._lastPositions or the canonical
  // store. Called from setPositions() (fresh truth arrived) and from
  // the Cancel button's own click handler (user-initiated, sends
  // nothing).
  _abandonEdit() {
    if (!this._edit) return;
    this._restoreChartGestures();
    this._edit = null;
  }

  _disableChartGestures() {
    if (!this._gesturesDisabled && this.chart) {
      this.chart.applyOptions({ handleScroll: false, handleScale: false });
      this._gesturesDisabled = true;
    }
  }

  _restoreChartGestures() {
    if (this._gesturesDisabled && this.chart) {
      this.chart.applyOptions({ handleScroll: true, handleScale: true });
    }
    this._gesturesDisabled = false;
  }

  // Multi-position correction round — re-draws ONLY the selected
  // position's SL/TP overlay (clears it, redraws from current
  // _selectedPos(), repositions) — used by every SL/TP-editing
  // transition (add-affordance tap, drag-release, Confirm, Cancel).
  // Deliberately never touches any Entry line/capsule — those are
  // untouched by any of these actions, so there is nothing to redraw
  // there even for a full setPositions() cycle.
  _rerenderSelected() {
    const sel = this._selectedPos();
    this._clearSelectedSlTpOnly();
    if (sel) this._renderSelectedSlTp(sel);
    this._repositionOverlay();
  }

  // 2nd correction round — Visual Contract V1's entry/TP/SL "pill"
  // capsules (tema.css's .entry/.pill), reproduced as absolutely-
  // positioned HTML overlays on top of the chart canvas (Lightweight
  // Charts draws to <canvas>; it cannot host HTML badges itself —
  // Desktop's own equivalent badges use the exact same "HTML overlay
  // positioned via priceToCoordinate()" technique). Deliberately NO
  // dollar reward/risk figure on the TP/SL pills — that would require
  // a NEW computation (price-delta × qty × contract size) this file is
  // not authorized to add (PROHIBIDO realizar cálculos financieros
  // nuevos). The entry pill's P&L uses pos.pnl verbatim — already a
  // real field on the canonical position object, never derived here.
  _ensureOverlayEl(key, className) {
    // typeof-guarded (see applyThemeColors() above): this.containerEl
    // is a real DOM node in the browser, but a plain {} fixture with no
    // .appendChild in the Node-based real-execution test harness.
    if (typeof this.containerEl.appendChild !== 'function') return null;
    if (!this._posOverlayEls) this._posOverlayEls = {};
    if (!this._posOverlayEls[key]) {
      const el = document.createElement('div');
      el.className = className;
      el.style.position = 'absolute';
      // Item 1 (6-item correction round) — the shaded Entry↔TP/Entry↔SL
      // zones are full-width bands (left:0;right:0, set in CSS), unlike
      // the left-aligned pills — skip the pill-only inline left:8px for
      // them. SL/TP editing — the confirm/cancel toolbar and the exact-
      // price input are positioned explicitly by their own render code
      // (next to whichever pill is being edited), also skipped here.
      if (className.indexOf('mbgc-zone') === -1 && className.indexOf('mbgc-edit') === -1) el.style.left = '8px';
      this.containerEl.appendChild(el);
      this._posOverlayEls[key] = el;
    }
    return this._posOverlayEls[key];
  }

  _clearOverlay() {
    if (!this._posOverlayEls) return;
    Object.keys(this._posOverlayEls).forEach((k) => { this._posOverlayEls[k].remove(); });
    this._posOverlayEls = null;
  }

  // Multi-position correction round — removes ONLY the selected
  // position's SL/TP-related overlay elements (pills/zones/add-
  // affordances/edit toolbar) without touching any position's Entry
  // line/capsule — used by resolveSlTpEdit() so resolving an edit never
  // has to tear down and rebuild every other position's Entry too.
  _clearSelectedSlTpOnly() {
    if (!this.candleSeries) { this._slLine = null; this._tpLine = null; }
    else {
      if (this._slLine) { this.candleSeries.removePriceLine(this._slLine); this._slLine = null; }
      if (this._tpLine) { this.candleSeries.removePriceLine(this._tpLine); this._tpLine = null; }
    }
    if (!this._posOverlayEls) return;
    ['tp', 'sl', 'zoneTp', 'zoneSl', 'add_tp', 'add_sl', 'editActions'].forEach((k) => {
      if (this._posOverlayEls[k]) { this._posOverlayEls[k].remove(); delete this._posOverlayEls[k]; }
    });
  }

  // Multi-position correction round — one native Entry price line AND
  // one capsule PER POSITION in the list (never averaged/merged/
  // synthesized — each position's own real pos.avg, verbatim). Zones/
  // TP/SL pills/add-affordances/edit toolbar are NOT drawn here at
  // all — those belong exclusively to the selected position, drawn
  // separately by _renderSelectedSlTp() below, exactly once.
  _renderAllEntries(list) {
    const lineColor = this._entryLineColor();
    list.forEach((pos) => {
      if (pos.avg == null) return;
      this._entryLines[pos.id] = this.candleSeries.createPriceLine({
        price: Number(pos.avg), color: lineColor, lineWidth: 1, lineStyle: 0,
        axisLabelVisible: true, title: String(pos.side || '').toUpperCase(),
      });
      this._renderEntryCapsule(pos);
    });
  }

  // Multi-position correction round — the FULL capsule (COMPRA/VENTA ·
  // qty · P&L · ×, draggable-nothing, Entry itself is NEVER draggable)
  // for the selected position; a small compact chip (side + its own
  // real entry price, tap-to-select by canonical id) for every other
  // one — "para no saturar el gráfico" per this round's own wording.
  // The × button is wired separately from the capsule BODY's own click
  // (which only ever selects) so it stays exclusively a close action,
  // never a selection one.
  _renderEntryCapsule(pos) {
    const key = 'entry_' + pos.id;
    const isSelected = this._selectedId != null && String(pos.id) === String(this._selectedId);
    const side = String(pos.side || '').toLowerCase();
    const sideClass = side === 'buy' ? 'mbgc-side-buy' : side === 'sell' ? 'mbgc-side-sell' : '';
    const baseClass = isSelected ? 'mbgc-pill mbgc-pill-entry' : 'mbgc-pill mbgc-pill-entry-compact';
    const el = this._ensureOverlayEl(key, baseClass);
    if (!el) return;
    el.className = baseClass + (sideClass ? ' ' + sideClass : '');
    if (isSelected) {
      const pnlOk = pos.pnl != null && isFinite(Number(pos.pnl));
      const pnlNum = pnlOk ? Number(pos.pnl) : null;
      const pnlTxt = pnlOk ? (pnlNum >= 0 ? '+' : '-') + '$' + Math.abs(pnlNum).toFixed(2) : '—';
      const qtyTxt = pos.qty != null ? Number(pos.qty).toFixed(2) : '—';
      // Item 4 (6-item correction round) — the entry pill's side badge
      // is equivalent functionality to the reference's own chip
      // (money-broker-movil/grafico.html: '(buy ? "COMPRA" : "VENTA")',
      // app.js line 167) — reproduced with its exact literal text.
      const sideTxt = side === 'buy' ? 'COMPRA' : side === 'sell' ? 'VENTA' : '—';
      el.innerHTML = '<span class="mbgc-pill-side">' + sideTxt
        + '</span><span class="mbgc-pill-info">' + qtyTxt + ' &middot; ' + pnlTxt + '</span>'
        + (pos.id != null ? '<button type="button" class="mbgc-pill-close" aria-label="Close position">&times;</button>' : '');
      const closeBtn = el.querySelector('.mbgc-pill-close');
      if (closeBtn) {
        // stopPropagation so the × never also reaches the capsule
        // body's own select handler below — exclusively a close action.
        closeBtn.onclick = (e) => { e.stopPropagation(); if (this.onCloseClick) this.onCloseClick(pos.id); };
      }
    } else {
      const sideTxt = side === 'buy' ? 'C' : side === 'sell' ? 'V' : '—';
      el.textContent = sideTxt + ' ' + this._fmtPx(pos.avg);
    }
    el.onclick = (e) => {
      if (e.target && e.target.closest && e.target.closest('.mbgc-pill-close')) return;
      if (this.onSelectPosition) this.onSelectPosition(pos.id);
    };
  }

  // Multi-position correction round — SL/TP lines/zones/add-
  // affordances/edit controls for the SELECTED position ONLY. Called
  // once per setPositions()/resolveSlTpEdit() — never for a non-
  // selected position (those only ever get an Entry, via
  // _renderEntryCapsule() above).
  _renderSelectedSlTp(pos) {
    // ISOLATED FIX — symbol/scale coherence gate. While candleSeries
    // doesn't yet hold THIS position's own symbol's real data (mid
    // symbol/timeframe switch, or genuinely empty), every SL/TP native
    // line/zone/pill/add-affordance/edit-toolbar is skipped entirely —
    // never drawn against a foreign instrument's scale — and a single
    // compact "Cargando gráfico" pill (reusing the existing "+ Añadir"
    // pill's own class, zero new CSS) takes their place at a fixed,
    // scale-independent position. _clearSelectedSlTpOnly() tears down
    // anything left over from the previously-selected/previous-symbol
    // position so nothing stale lingers on screen.
    if (!this.isSlTpEditableFor(pos.symbol)) {
      this._clearSelectedSlTpOnly();
      const el = this._ensureOverlayEl('slTpLoading', 'mbgc-pill mbgc-add');
      if (el) {
        el.textContent = 'Cargando gráfico…';
        el.onclick = null;
        el.style.cursor = 'default';
        const containerH = this.containerEl.clientHeight || 300;
        el.style.top = Math.round(containerH / 2 - 12) + 'px';
      }
      return;
    }
    if (this._posOverlayEls && this._posOverlayEls.slTpLoading) {
      this._posOverlayEls.slTpLoading.remove();
      delete this._posOverlayEls.slTpLoading;
    }
    // Item 1 (6-item correction round) — shaded zones between Entry and
    // TP/SL, tema.css's own .zone-tp/.zone-sl treatment (--buy-zone/
    // --sell-zone, translucent fills). Created here (coordinates filled
    // in by _repositionOverlay() below, same as every other overlay
    // element) — this is coordinate conversion for drawing a shaded
    // band between two already-real prices, never a financial
    // calculation (no reward/risk number is derived or displayed).
    if (pos.avg != null && pos.tp != null) this._ensureOverlayEl('zoneTp', 'mbgc-zone mbgc-zone-tp');
    if (pos.avg != null && pos.sl != null) this._ensureOverlayEl('zoneSl', 'mbgc-zone mbgc-zone-sl');
    if (pos.sl != null) {
      this._slLine = this.candleSeries.createPriceLine({
        price: Number(pos.sl), color: '#F6465D', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: 'SL',
      });
    }
    if (pos.tp != null) {
      this._tpLine = this.candleSeries.createPriceLine({
        price: Number(pos.tp), color: '#2EBD85', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: 'TP',
      });
    }
    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — the TP/SL
    // pills are now interactive (drag/tap) when a real level exists,
    // and an "+ Añadir TP/SL" affordance when it doesn't (never an
    // auto-assigned level). See _renderSlTpField() below for the full
    // state machine (idle/drag/edit/sending).
    this._renderSlTpField('tp', pos);
    this._renderSlTpField('sl', pos);
    this._renderEditActions();
  }

  // SL/TP presentation correction round — renders ONE of:
  //  - the normal confirmed pill (idle): solid fill, full opacity,
  //    wired for drag (pointerdown) and tap-to-edit;
  //  - the SAME pill showing a live/frozen preview while this._edit is
  //    active for this field — 'edit'/'drag' modes draw a ring cue
  //    (solid for an existing level, dashed for a brand-new one via
  //    "+ Añadir" — editing.isNew) but NEVER touch opacity; 'sending'
  //    is the only mode that dims the pill;
  //  - a "+ Añadir SL/TP" affordance when the position has no level for
  //    this field and nothing is being edited — tapping it enters edit
  //    mode directly (see _renderAddAffordance() below), never assigns
  //    any level automatically.
  // Pure presentation + gesture capture — no network call happens
  // anywhere in this method; onSlTpConfirm() (the one and only path
  // that can ever lead to a real send) fires exclusively from the
  // Confirm button in _renderEditActions() below.
  _renderSlTpField(field, pos) {
    const editing = this._edit && this._edit.field === field && this._edit.pos && String(this._edit.pos.id) === String(pos.id) ? this._edit : null;
    const hasLevel = pos[field] != null;
    if (!hasLevel && !editing) {
      this._removePill(field);
      this._renderAddAffordance(field, pos);
      return;
    }
    this._removeAddAffordance(field);
    const baseClass = field === 'tp' ? 'mbgc-pill mbgc-pill-tp' : 'mbgc-pill mbgc-pill-sl';
    const el = this._ensureOverlayEl(field, baseClass);
    if (!el) return;
    let modifier = '';
    let priceToShow = pos[field];
    if (editing) {
      priceToShow = editing.previewPrice;
      if (editing.mode === 'sending') modifier = ' mbgc-pill-sending';
      else modifier = editing.isNew ? ' mbgc-pill-provisional' : ' mbgc-pill-editing';
    }
    el.className = baseClass + modifier;
    el.textContent = field.toUpperCase() + ' ' + (priceToShow != null ? this._fmtPx(priceToShow) : '—');
    el.style.touchAction = 'none';
    if (editing && editing.mode === 'sending') {
      // Locked — no interaction while a real order-update is in flight.
      el.onpointerdown = null;
      el.style.cursor = 'default';
      return;
    }
    el.style.cursor = 'ns-resize';
    el.onpointerdown = (e) => this._onPillPointerDown(e, field, pos);
  }

  _removePill(field) {
    if (this._posOverlayEls && this._posOverlayEls[field]) {
      this._posOverlayEls[field].remove();
      delete this._posOverlayEls[field];
    }
  }

  // "+ Añadir SL/TP" — only shown when the position genuinely has no
  // level for this field and nothing is being edited. Stacked near the
  // entry pill's own column (same left:8px) since there is no real
  // price yet to position it at. Tapping it enters edit mode DIRECTLY
  // (a draggable provisional pill + the Confirmar/Cancelar form, both
  // pre-filled from a coordinate-only default — see _defaultAddPrice()
  // — never a financial calculation and never sent anywhere on its
  // own).
  _renderAddAffordance(field, pos) {
    if (!this._posOverlayEls) this._posOverlayEls = {};
    const key = 'add_' + field;
    if (typeof this.containerEl.appendChild !== 'function') return;
    let el = this._posOverlayEls[key];
    if (!el) {
      el = document.createElement('button');
      el.type = 'button';
      el.style.position = 'absolute';
      el.style.left = '8px';
      this.containerEl.appendChild(el);
      this._posOverlayEls[key] = el;
    }
    el.className = 'mbgc-add' + (field === 'tp' ? ' mbgc-add-tp' : ' mbgc-add-sl');
    el.textContent = '+ ' + field.toUpperCase();
    // SL/TP presentation correction round, item 2 — NO fixed top here
    // anymore (the old 36px/72px literally floated above the chart,
    // unrelated to the entry it belonged to). Positioning is entirely
    // _repositionOverlay()'s job now, anchored to the selected
    // position's own real Entry coordinate — see its own comment below.
    el.onclick = () => {
      if (this._edit) return; // at most one modification in progress
      // ISOLATED FIX (Parte A) — defense-in-depth: _renderSlTpField()
      // (the only place that wires this onclick) already refuses to
      // run at all while _renderSelectedSlTp() finds the chart not
      // ready for this position's own symbol/generation, so this
      // should be unreachable via a real tap — kept anyway in case a
      // stale affordance reference ever outlives a clear().
      if (!this.isSlTpEditableFor(pos.symbol)) return;
      const previewPrice = this._defaultAddPrice(field, pos);
      if (previewPrice == null) return;
      this._edit = {
        field, pos, mode: 'edit',
        previewPrice, startPrice: previewPrice,
        pointerId: null, startClientY: null, moved: false, isNew: true,
      };
      this._rerenderSelected();
    };
  }

  _removeAddAffordance(field) {
    const key = 'add_' + field;
    if (this._posOverlayEls && this._posOverlayEls[key]) {
      this._posOverlayEls[key].remove();
      delete this._posOverlayEls[key];
    }
  }

  // Pure coordinate placement for a brand-new level's STARTING preview
  // only — a fixed pixel offset from the entry price, converted back to
  // a price via coordinateToPrice() (the exact inverse of
  // priceToCoordinate(), both pure pixel<->price conversions, never a
  // financial rule). This is NEVER sent anywhere on its own — it only
  // exists so a provisional line has somewhere visible to start before
  // the user drags it or types an exact price; "no asignes precios
  // automáticamente" governs the CONFIRMED/sent value, which only ever
  // comes from the user's own drag/typed input at Confirm time.
  _defaultAddPrice(field, pos) {
    if (this.candleSeries && pos.avg != null) {
      const avgY = this.candleSeries.priceToCoordinate(Number(pos.avg));
      if (avgY != null) {
        const offsetPx = 60;
        const y = field === 'tp' ? avgY - offsetPx : avgY + offsetPx;
        const price = this.candleSeries.coordinateToPrice(y);
        if (price != null) return price;
      }
    }
    return pos.avg != null ? Number(pos.avg) : 0;
  }

  // ── Drag gesture (SL/TP editing) ──────────────────────────────────
  // Pointer Events (unified mouse + touch). touch-action:none (set in
  // _renderSlTpField()) plus preventDefault()/stopPropagation() here
  // plus temporarily disabling the chart's own handleScroll/handleScale
  // (_disableChartGestures()) together guarantee the SAME gesture that
  // drags a label never also pans/zooms the chart — three independent
  // layers because touch/mouse/pointer event semantics differ enough
  // across browsers that relying on just one would be fragile.
  // setPointerCapture() ensures pointermove/up keep arriving at this
  // exact element even if the finger/cursor leaves its bounds mid-drag.
  // Allowed to start from idle (a confirmed pill) OR from this SAME
  // field's own already-in-progress 'edit' state (re-dragging a pill
  // that was already released once, confirmed-existing or brand-new) —
  // refused only when a DIFFERENT field already owns the single edit
  // slot, or while 'sending' has it locked.
  _onPillPointerDown(e, field, pos) {
    // ISOLATED FIX — defense-in-depth: _renderSlTpField() (the only
    // place that wires this handler) already refuses to run at all
    // while _renderSelectedSlTp() finds the chart not yet ready for
    // this position's own symbol, so this should be unreachable via a
    // real tap — kept anyway in case a stale pill reference ever
    // outlives a symbol switch.
    if (!this.isSlTpEditableFor(pos.symbol)) return;
    const current = this._edit;
    if (current && (current.field !== field || current.mode === 'sending')) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    if (typeof e.stopPropagation === 'function') e.stopPropagation();
    const startPrice = current ? current.previewPrice : pos[field];
    const isNew = current ? current.isNew : pos[field] == null;
    this._edit = {
      field, pos, mode: 'drag',
      previewPrice: startPrice, startPrice,
      pointerId: e.pointerId, startClientY: e.clientY,
      moved: false, isNew,
    };
    // The Confirmar/Cancelar toolbar only shows once settled (see
    // _onPillPointerUp()) — hide it for the duration of this (possibly
    // SECOND) drag so it never sits stale over a moving pill.
    this._removeEditActions();
    this._disableChartGestures();
    const el = this._posOverlayEls && this._posOverlayEls[field];
    if (el) {
      if (typeof el.setPointerCapture === 'function') {
        try { el.setPointerCapture(e.pointerId); } catch (_) {}
      }
      el.onpointermove = (ev) => this._onPillPointerMove(ev);
      el.onpointerup = (ev) => this._onPillPointerUp(ev);
      el.onpointercancel = (ev) => this._onPillPointerUp(ev);
    }
  }

  _onPillPointerMove(e) {
    const ed = this._edit;
    if (!ed || ed.mode !== 'drag' || !this.candleSeries || e.pointerId !== ed.pointerId) return;
    // ISOLATED FIX — a symbol/timeframe switch mid-drag already calls
    // _abandonEdit() via the very next setPositions() (the edited
    // position is filtered out of the new symbol's list) — this is a
    // second, explicit guard against continuing to compute a preview
    // price from a scale that is no longer this position's own.
    if (!this.isSlTpEditableFor(ed.pos.symbol)) { this._abandonEdit(); return; }
    if (typeof e.preventDefault === 'function') e.preventDefault();
    const dy = e.clientY - ed.startClientY;
    if (Math.abs(dy) > 4) ed.moved = true;
    const startY = this.candleSeries.priceToCoordinate(Number(ed.startPrice));
    if (startY == null) return;
    const containerH = this.containerEl.clientHeight || 1;
    const newY = Math.max(4, Math.min(containerH - 4, startY + dy));
    const newPrice = this.candleSeries.coordinateToPrice(newY);
    if (newPrice == null) return;
    ed.previewPrice = newPrice;
    this._updateDragVisual();
  }

  // Cheap, targeted DOM update (never a full _rerenderSelected() rebuild)
  // so dragging stays smooth — only the dragged pill's position/text
  // and its own shaded zone band move during the gesture itself (the
  // edit-actions toolbar is hidden for the duration of any drag — see
  // _onPillPointerDown() — so there is nothing of its to sync here).
  _updateDragVisual() {
    const ed = this._edit;
    if (!ed || !this._posOverlayEls || !this.candleSeries) return;
    const el = this._posOverlayEls[ed.field];
    if (el) {
      const y = this.candleSeries.priceToCoordinate(Number(ed.previewPrice));
      if (y != null) el.style.top = Math.round(y - el.offsetHeight / 2) + 'px';
      el.textContent = ed.field.toUpperCase() + ' ' + this._fmtPx(ed.previewPrice);
    }
    const zoneKey = ed.field === 'tp' ? 'zoneTp' : 'zoneSl';
    const zoneEl = this._posOverlayEls[zoneKey];
    if (zoneEl && ed.pos.avg != null) {
      const yA = this.candleSeries.priceToCoordinate(Number(ed.pos.avg));
      const yB = this.candleSeries.priceToCoordinate(Number(ed.previewPrice));
      if (yA != null && yB != null) {
        zoneEl.style.display = '';
        zoneEl.style.top = Math.round(Math.min(yA, yB)) + 'px';
        zoneEl.style.height = Math.round(Math.abs(yB - yA)) + 'px';
      }
    }
  }

  // Release — whether the pointer actually moved or this was just a
  // tap, BOTH land in the same 'edit' state: the pill stays draggable
  // (pointerdown is re-attached by the next _renderSlTpField() call)
  // AND the Confirmar/Cancelar form (with its own editable exact-price
  // input) appears — "entra directamente en modo edición" with every
  // capability (drag further, type exact, confirm, cancel) available
  // at once, never a separate tap-only vs drag-only path.
  _onPillPointerUp(e) {
    const ed = this._edit;
    if (!ed || ed.mode !== 'drag' || e.pointerId !== ed.pointerId) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    const el = this._posOverlayEls && this._posOverlayEls[ed.field];
    if (el) {
      el.onpointermove = null;
      el.onpointerup = null;
      el.onpointercancel = null;
      if (typeof el.releasePointerCapture === 'function') {
        try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      }
    }
    this._restoreChartGestures();
    ed.mode = 'edit';
    this._rerenderSelected();
  }

  // ── Confirm/Cancel toolbar — always carries an editable exact-price
  // input, for BOTH re-dragging/editing an existing level and adding a
  // brand-new one. The ONLY UI surface that can ever lead to a real
  // send (via Confirm) or guarantee zero send (via Cancel). Typing in
  // the input live-updates the pill/zone the same way dragging does
  // (same _updateDragVisual()); the input itself is only ever
  // repainted from this._edit when it does NOT currently have focus,
  // so it never fights the user's own typing.
  _renderEditActions() {
    const ed = this._edit;
    const needed = ed && ed.mode === 'edit';
    if (!needed) { this._removeEditActions(); return; }
    if (typeof this.containerEl.appendChild !== 'function') return;
    if (!this._posOverlayEls) this._posOverlayEls = {};
    let box = this._posOverlayEls.editActions;
    let fresh = false;
    if (!box) {
      box = document.createElement('div');
      box.className = 'mbgc-edit-box';
      box.style.position = 'absolute';
      box.style.touchAction = 'none';
      this.containerEl.appendChild(box);
      this._posOverlayEls.editActions = box;
      fresh = true;
    }
    if (fresh || box.dataset.field !== ed.field) {
      box.dataset.field = ed.field;
      box.innerHTML =
        '<span class="mbgc-edit-label">' + ed.field.toUpperCase() + '</span>'
        + '<input type="number" step="any" class="mbgc-edit-input">'
        + '<button type="button" class="mbgc-edit-cancel">Cancelar</button>'
        + '<button type="button" class="mbgc-edit-confirm">Confirmar</button>'
        + '<div class="mbgc-edit-err" hidden>Precio inv&aacute;lido</div>';
      const input = box.querySelector('.mbgc-edit-input');
      const err = box.querySelector('.mbgc-edit-err');
      input.oninput = () => {
        const v = parseFloat(input.value);
        if (isFinite(v) && v > 0 && this._edit) {
          this._edit.previewPrice = v;
          err.hidden = true;
          this._updateDragVisual();
        }
      };
      box.querySelector('.mbgc-edit-cancel').onclick = () => this._cancelEdit();
      box.querySelector('.mbgc-edit-confirm').onclick = () => {
        const v = parseFloat(input.value);
        if (!isFinite(v) || v <= 0) { err.hidden = false; return; }
        if (this._edit) this._edit.previewPrice = v;
        this._confirmEdit();
      };
    }
    const input = box.querySelector('.mbgc-edit-input');
    if (input && document.activeElement !== input) {
      input.value = ed.previewPrice != null ? this._fmtPx(ed.previewPrice) : '';
    }
    this._positionEditBox();
  }

  // Extracted so _repositionOverlay() (resize/pan/zoom/candle update)
  // can keep the toolbar correctly anchored WITHOUT rebuilding its
  // innerHTML (which would steal focus away from the input mid-type).
  _positionEditBox() {
    const ed = this._edit;
    const box = this._posOverlayEls && this._posOverlayEls.editActions;
    if (!ed || !box) return;
    const refPrice = ed.previewPrice != null ? ed.previewPrice : ed.pos.avg;
    const containerH = this.containerEl.clientHeight || 300;
    let top = Math.round(containerH / 2);
    if (refPrice != null && this.candleSeries) {
      const y = this.candleSeries.priceToCoordinate(Number(refPrice));
      if (y != null) top = Math.round(y);
    }
    box.style.top = Math.max(4, Math.min(containerH - 44, top + 14)) + 'px';
    box.style.left = '8px';
  }

  _removeEditActions() {
    if (this._posOverlayEls && this._posOverlayEls.editActions) {
      this._posOverlayEls.editActions.remove();
      delete this._posOverlayEls.editActions;
    }
  }

  // Confirm — the ONE path that can ever lead to a real send. Validates
  // the price is a sane finite positive number (format sanity only,
  // never a financial/direction rule — the backend's own
  // _validate_sl_tp remains the real authority), locks the field into
  // 'sending' (no further drag/tap accepted) and removes the toolbar
  // BEFORE invoking onSlTpConfirm(), so a double-click can never fire
  // the callback twice. This class never sends anything itself — it
  // only reports the user's confirmed intent; the caller (mobile.html)
  // owns building the real order-update payload (preserving the OTHER
  // field's current confirmed value) and calling session.updateSlTp().
  _confirmEdit() {
    const ed = this._edit;
    if (!ed) return;
    const posId = ed.pos.id;
    const field = ed.field;
    const price = ed.previewPrice;
    if (posId == null || price == null || !isFinite(price) || price <= 0) return;
    // ISOLATED FIX — re-validate at the exact moment of Confirm, the
    // one path that can ever lead to a real send: (1) the edited
    // position's own id, (2) its own canonical symbol, (3) the symbol
    // the chart's candleSeries currently actually holds data for, and
    // (4) that this id is STILL the real, currently-selected position
    // (never a stale _edit outliving a selection change this class
    // somehow failed to abandon). Any mismatch aborts without sending
    // and without reverting the user's typed/dragged preview — the
    // same toolbar stays open so Cancel remains available; nothing is
    // silently "fixed" by substituting pos.avg or any other fallback.
    const sel = this._selectedPos();
    if (!sel || String(sel.id) !== String(posId)) return;
    if (!ed.pos.symbol || ed.pos.symbol !== sel.symbol) return;
    if (!this.isSlTpEditableFor(sel.symbol)) return;
    ed.mode = 'sending';
    this._removeEditActions();
    this._rerenderSelected();
    if (this.onSlTpConfirm) this.onSlTpConfirm(posId, field, price);
  }

  // Cancel — user-initiated, NEVER sends anything. Reverts immediately
  // to the last confirmed canonical level (or, for a brand-new "+
  // Añadir" level that was never confirmed, back to the "+ Añadir"
  // affordance itself) by dropping all local edit state and re-
  // rendering straight from the real canonical selected position this
  // file already holds (_selectedPos()), never the preview.
  _cancelEdit() {
    this._abandonEdit();
    this._rerenderSelected();
  }

  // Coordinate conversion only (candleSeries.priceToCoordinate()) — the
  // same native Lightweight Charts API Desktop already uses for its own
  // position badges. Re-run after every candle_new/candle_update and
  // resize() (see appendCandle()/updateLastCandle()/resize() below)
  // because the chart's own price<->pixel mapping shifts as new bars
  // arrive or the container resizes, even though the position's own
  // price levels never change.
  // Multi-position correction round — repositions EVERYTHING: every
  // position's own Entry capsule (collision-avoided, see
  // _layoutEntryCapsules() below — the capsule's Y may be nudged to
  // stay legible, the NATIVE price line never is, and never is the
  // underlying price) plus, for the selected position only, its SL/TP
  // pills/zones/add-affordances/edit toolbar. Called on resize(),
  // pan/zoom (subscribeVisibleLogicalRangeChange, see initialize()),
  // every candle_new/candle_update/history apply, and every
  // setPositions()/_rerenderSelected() — so this is the ONE place all
  // of those hooks ultimately converge on.
  _repositionOverlay() {
    if (!this._posOverlayEls || !this.candleSeries) return;
    this._layoutEntryCapsules();
    const sel = this._selectedPos();
    if (!sel) return;
    // MOBILE-DESIGN-INTEGRATION-01 — GRAPH, SL/TP editing — if a
    // drag/edit/sending edit is active for a field, its own (already-
    // real, already-on-screen) preview price is the correct level to
    // reposition at here too — otherwise a resize or an incoming candle
    // update mid-edit would snap the pill/zone back to the stale
    // confirmed value for one frame. Still pure coordinate math;
    // previewPrice itself was only ever set by the user's own drag/
    // typed input, never computed here.
    const ed = this._edit;
    const effective = (field, fallback) => (ed && ed.field === field) ? ed.previewPrice : fallback;
    const slPrice = effective('sl', sel.sl);
    const tpPrice = effective('tp', sel.tp);
    const containerH = this.containerEl.clientHeight || 300;
    // SL/TP presentation correction round — a level outside the
    // visible candle range is clamped to the nearest edge (4px from
    // top/bottom) rather than hidden outright, with a CSS cue
    // (mbgc-pill-offscreen) marking it as clamped — "ofrece una forma
    // clara de localizar/acceder... sin representarlo en un precio
    // falso": the label text itself is still the REAL price (_fmtPx of
    // the real value), only its drawn Y is clamped; the canonical data
    // is never touched.
    const place = (el, price) => {
      if (!el) return;
      const y = price != null ? this.candleSeries.priceToCoordinate(Number(price)) : null;
      if (y == null) { el.style.display = 'none'; return; }
      el.style.display = '';
      if (y < 4) {
        el.style.top = '4px';
        el.classList.add('mbgc-pill-offscreen');
      } else if (y > containerH - 24) {
        el.style.top = Math.round(containerH - 24) + 'px';
        el.classList.add('mbgc-pill-offscreen');
      } else {
        el.style.top = Math.round(y - el.offsetHeight / 2) + 'px';
        el.classList.remove('mbgc-pill-offscreen');
      }
    };
    // Item 1 (6-item correction round) — the shaded band spans exactly
    // the two boundary prices' own pixel coordinates (priceToCoordinate
    // for both, never a financial computation), re-run on every resize/
    // candle update/position change the same as the pills/lines above —
    // so it correctly tracks zoom, pan, resize, and position changes.
    // Clamped to the visible container bounds (never hidden outright)
    // when it partially/fully extends past the current candle range.
    const placeZone = (el, priceA, priceB) => {
      if (!el) return;
      const yA = priceA != null ? this.candleSeries.priceToCoordinate(Number(priceA)) : null;
      const yB = priceB != null ? this.candleSeries.priceToCoordinate(Number(priceB)) : null;
      if (yA == null || yB == null) { el.style.display = 'none'; return; }
      let top = Math.max(0, Math.min(yA, yB));
      let bottom = Math.min(containerH, Math.max(yA, yB));
      if (bottom <= top) { el.style.display = 'none'; return; }
      el.style.display = '';
      el.style.top = Math.round(top) + 'px';
      el.style.height = Math.round(bottom - top) + 'px';
    };
    if (this._posOverlayEls.zoneTp) placeZone(this._posOverlayEls.zoneTp, sel.avg, tpPrice);
    if (this._posOverlayEls.zoneSl) placeZone(this._posOverlayEls.zoneSl, sel.avg, slPrice);
    if (this._posOverlayEls.tp) place(this._posOverlayEls.tp, tpPrice);
    if (this._posOverlayEls.sl) place(this._posOverlayEls.sl, slPrice);
    // SL/TP presentation correction round, item 2 — "+ Añadir" controls
    // are anchored to the selected position's OWN real Entry coordinate
    // (never a fixed top) — a small fixed pixel offset from it (pure
    // coordinate math, same category as the drag/add-default math
    // elsewhere in this file), so they visibly belong to that Entry
    // instead of floating at a constant height regardless of scroll/
    // zoom/pan.
    const avgY = sel.avg != null ? this.candleSeries.priceToCoordinate(Number(sel.avg)) : null;
    const placeAdd = (key, offsetPx) => {
      const el = this._posOverlayEls && this._posOverlayEls[key];
      if (!el) return;
      if (avgY == null) { el.style.display = 'none'; return; }
      el.style.display = '';
      el.style.top = Math.round(Math.max(4, Math.min(containerH - 28, avgY + offsetPx))) + 'px';
    };
    placeAdd('add_tp', -34);
    placeAdd('add_sl', 34);
    this._positionEditBox();
  }

  // Multi-position correction round — positions every position's own
  // Entry capsule at its real priceToCoordinate(avg), WITH vertical
  // collision avoidance between capsules that would otherwise overlap
  // (sorted by Y, each subsequent one nudged down just enough to clear
  // a minimum gap from the previous). This ONLY ever moves the CAPSULE
  // (an HTML overlay) — the native Entry price line for every position
  // (createPriceLine(), drawn once in _renderAllEntries()) stays
  // exactly at its own real price, never nudged, and the underlying
  // canonical pos.avg is never touched either way — "nunca desplaces
  // el precio para resolver una colisión visual".
  // CLOSE-BUTTON REACHABILITY FIX (isolated-close-bug block) — ROOT
  // CAUSE, confirmed by reproduction: the chart's own rightPriceScale
  // uses a very tight bottom margin (scaleMargins.bottom: 0.02, see
  // initialize() above), so a position whose price sits near the LOW
  // end of the currently visible candle range computes a Y close to
  // (or past) the bottom edge of .mbg-chart's own clipping box. The
  // capsule — and specifically its × close button, since the capsule
  // is vertically centered on that Y — then renders overlapping
  // #mobTicket's trade-bar (VENDER/COMPRAR), which sits immediately
  // below in normal document flow and intercepts the tap before it
  // ever reaches the × (confirmed via elementFromPoint() at the real
  // 390×844 viewport: it resolved to .mbg-sell, not .mbgc-pill-close).
  // onCloseClick itself was never broken — selection, closeInFlight,
  // session.closePosition(), and the WS send all already fired
  // correctly once the tap actually reached the button.
  //
  // Fix: clamp each capsule's drawn Y so its own box never extends
  // past a small safe margin from the chart's top/bottom edges — the
  // SAME "pure coordinate clamp, never the underlying price" category
  // already used for off-screen SL/TP pills in _repositionOverlay()
  // below. This restores reachability without moving the native Entry
  // price line (still exactly at the real canonical pos.avg) or
  // changing the capsule's own visual content/style in the normal
  // (non-edge) case — the design itself is unchanged.
  //
  // RE-VERIFICATION FIX (real-Lightweight-Charts re-check) — a SECOND,
  // compounding bug confirmed by isolated reproduction using the REAL
  // chart engine (not the simplified FakeLWC stand-in) with several
  // positions clustered close together near the bottom edge: the
  // previous version clamped each item's Y INDEPENDENTLY, after the
  // minGap nudge — so when two or more items' nudged Y all exceeded
  // the safe bottom boundary, EVERY one of them got clamped to the
  // SAME ceiling value, collapsing the deliberate minGap separation
  // right back onto a single Y (confirmed: two positions' compact
  // chips ended up with byte-identical getBoundingClientRect() and
  // `top`, so a real tap meant for one resolved to whichever was
  // painted later — closing the wrong id). A flat minGap also never
  // accounted for the selected (taller, 24px) capsule sharing the
  // stack with shorter (20px) compact chips.
  //
  // Fix: gap-aware forward nudge (each item's own half-height, not a
  // flat constant, decides how far it must sit from the PREVIOUS
  // item's real edge) followed by an edge-aware backward clamp that
  // CASCADES any bottom-edge correction upward through every earlier
  // item — so clamping the last item to stay on-screen can never again
  // collapse it onto the one above. Still pure coordinate placement:
  // no price, no content, no style is touched; the normal (uncrowded,
  // not-near-an-edge) case lays out identically to before.
  _layoutEntryCapsules() {
    const containerH = this.containerEl.clientHeight || 300;
    const edgeMargin = 4;
    const gap = 4;
    const items = this._lastPositions
      .filter((p) => p.avg != null)
      .map((p) => {
        const el = this._posOverlayEls && this._posOverlayEls['entry_' + p.id];
        const y = el ? this.candleSeries.priceToCoordinate(Number(p.avg)) : null;
        return el && y != null ? { el, y, halfH: (el.offsetHeight || 24) / 2 } : null;
      })
      .filter(Boolean)
      .sort((a, b) => a.y - b.y);
    if (!items.length) return;

    for (let i = 1; i < items.length; i++) {
      const min = items[i - 1].y + items[i - 1].halfH + items[i].halfH + gap;
      if (items[i].y < min) items[i].y = min;
    }

    const last = items[items.length - 1];
    const bottomLimit = containerH - edgeMargin;
    if (last.y + last.halfH > bottomLimit) last.y = bottomLimit - last.halfH;
    for (let i = items.length - 2; i >= 0; i--) {
      const max = items[i + 1].y - items[i + 1].halfH - items[i].halfH - gap;
      if (items[i].y > max) items[i].y = max;
    }

    const topLimit = edgeMargin;
    if (items[0].y - items[0].halfH < topLimit) items[0].y = topLimit + items[0].halfH;
    for (let i = 1; i < items.length; i++) {
      const min = items[i - 1].y + items[i - 1].halfH + items[i].halfH + gap;
      if (items[i].y < min) items[i].y = min;
    }

    items.forEach((it) => {
      it.el.style.display = '';
      it.el.style.top = Math.round(it.y - it.halfH) + 'px';
    });
  }

  // History — same real phase contract already proven on Desktop: a
  // "complete" (depth-fetch) response arriving after bars already exist
  // (from the "initial" fast-paint) must never replace them — only
  // PREPEND the strictly-older portion. Otherwise, full replace. Backend
  // guarantees ascending order; never re-sorted here (matching Desktop,
  // which doesn't either).
  // ISOLATED FIX — `symbol` (new parameter): the symbol THIS data
  // actually belongs to, per the caller (mobile.html's onHistory()),
  // which reads session.currentSymbol fresh at the moment the message
  // arrives — the same live value mobile_session.js's own guard
  // already validated msg.symbol against before calling onHistory() at
  // all, so a stale/out-of-order response for an abandoned symbol never
  // reaches here. Optional (older/internal callers that don't pass it
  // leave _historySymbol untouched — bars/series still apply exactly as
  // before; those paths simply don't unlock SL/TP editing on their own).
  applyHistory(rawData, phase, symbol) {
    if (!Array.isArray(rawData)) return;
    const bars = rawData
      .map((c) => ({
        time: normTime(c.time), open: n(c.open), high: n(c.high), low: n(c.low), close: n(c.close), volume: n(c.volume),
      }))
      .filter((b) => b.time && b.open != null && b.high != null && b.low != null && b.close != null);

    if ((phase || 'complete') === 'complete' && this.bars.length) {
      const earliest = this.bars[0].time;
      const older = bars.filter((b) => b.time < earliest);
      if (older.length) {
        this.bars = older.concat(this.bars);
        this.candleSeries?.setData(this.bars);
        this.volumeSeries?.setData(this.bars.map(volPointForBar));
        this._repositionOverlay();
      }
      return;
    }
    this.bars = bars;
    this.candleSeries?.setData(bars);
    this.volumeSeries?.setData(bars.map(volPointForBar));
    this.chart?.timeScale().fitContent();
    if (symbol) {
      this._historySymbol = symbol;
      // ISOLATED FIX (Parte A) — records that THIS generation (the one
      // current as of the most recent clear()) is now genuinely
      // satisfied. Pairs with the generation bump in clear() — see
      // isSlTpEditableFor()'s own comment for why the symbol string
      // alone isn't enough to cover a timeframe-only switch.
      this._readyGeneration = this._historyGeneration;
    }
    // ISOLATED FIX — this is the one moment isSlTpEditableFor() can
    // flip from false to true (a genuine symbol's own history just
    // landed). _repositionOverlay() alone only repositions whatever
    // overlay elements already exist — it never rebuilds them — so
    // without this, the selected position's SL/TP section would stay
    // showing "Cargando gráfico…" forever once ready, instead of
    // rendering the real controls. Cheap and correct here specifically
    // because applyHistory() only ever fires on an initial/complete
    // history load, never per-tick (appendCandle()/updateLastCandle()
    // below correctly keep using the cheaper _repositionOverlay()-only
    // path, since readiness can never change on an ordinary candle
    // update).
    this._rerenderSelected();
  }

  // candle_new — append. Defensive-only guard (not a new price/
  // aggregation rule): if a timestamp arrives that isn't strictly after
  // the current last bar, patch it in place instead of pushing a
  // duplicate — the backend's own contract already guarantees candle_new
  // only fires for a genuinely later timestamp, this only protects the
  // bars array's ordering invariant against anything unexpected.
  appendCandle(rawBar) {
    const b = this._normalizeCandle(rawBar);
    if (!b) return;
    if (this.bars.length && b.time <= this.bars[this.bars.length - 1].time) {
      this.bars[this.bars.length - 1] = b;
    } else {
      this.bars.push(b);
    }
    this.candleSeries?.update(b);
    this._repositionOverlay();
  }

  // candle_update — patch the current last bar in place (no resize).
  updateLastCandle(rawBar) {
    const b = this._normalizeCandle(rawBar);
    if (!b) return;
    if (this.bars.length) this.bars[this.bars.length - 1] = b;
    else this.bars.push(b);
    this.candleSeries?.update(b);
    this._repositionOverlay();
  }

  // volume_update — backend already supplies value+color; displayed
  // exactly as sent, no computation here.
  updateVolume(rawPoint) {
    if (!rawPoint) return;
    const time = normTime(rawPoint.time);
    const value = n(rawPoint.value);
    if (!time || value == null) return;
    this.volumeSeries?.update({ time, value, color: rawPoint.color });
  }

  _normalizeCandle(rawBar) {
    if (!rawBar) return null;
    const b = {
      time: normTime(rawBar.time), open: n(rawBar.open), high: n(rawBar.high), low: n(rawBar.low), close: n(rawBar.close),
    };
    if (!(b.time && b.open != null && b.high != null && b.low != null && b.close != null)) return null;
    return b;
  }
}
