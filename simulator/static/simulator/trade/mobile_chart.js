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
    this.chart = LightweightCharts.createChart(this.containerEl, {
      layout: { background: { type: 'solid', color: '#020209' }, textColor: '#9098a8' },
      grid: { vertLines: { color: 'rgba(255,255,255,.03)' }, horzLines: { color: 'rgba(255,255,255,.03)' } },
      timeScale: { borderColor: 'rgba(255,255,255,.06)' },
      rightPriceScale: { borderColor: 'rgba(255,255,255,.06)', scaleMargins: { top: 0.08, bottom: 0.02 } },
    });
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
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
  }

  resize() {
    if (!this.chart || !this.containerEl) return;
    this.chart.applyOptions({ width: this.containerEl.clientWidth, height: this.containerEl.clientHeight });
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
  }

  // History — same real phase contract already proven on Desktop: a
  // "complete" (depth-fetch) response arriving after bars already exist
  // (from the "initial" fast-paint) must never replace them — only
  // PREPEND the strictly-older portion. Otherwise, full replace. Backend
  // guarantees ascending order; never re-sorted here (matching Desktop,
  // which doesn't either).
  applyHistory(rawData, phase) {
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
      }
      return;
    }
    this.bars = bars;
    this.candleSeries?.setData(bars);
    this.volumeSeries?.setData(bars.map(volPointForBar));
    this.chart?.timeScale().fitContent();
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
  }

  // candle_update — patch the current last bar in place (no resize).
  updateLastCandle(rawBar) {
    const b = this._normalizeCandle(rawBar);
    if (!b) return;
    if (this.bars.length) this.bars[this.bars.length - 1] = b;
    else this.bars.push(b);
    this.candleSeries?.update(b);
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
