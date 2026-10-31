# Mobile chart framing correction — deployed 2026-10-30

The production source contains the prior autoscale reset, but skips it when a live candle arrives before the first `complete` history: `bars.length` incorrectly classifies that history as a depth extension. This also skips marking the current history generation ready.

The mobile chart now tracks accepted history independently of live candles. The first nonempty history resets vertical autoscale with symmetric margins and shows a bounded recent window (approximately eight pixels per candle). Subsequent live candles and depth extensions preserve manual zoom. Initialization after history arrival restores instrument precision and the same viewport. Empty history cannot unlock editing.

Only mobile_chart.js presentation changes. No consumer, websocket, backend, quote, trading, accounting, instrument or database changes.

Validation:
- Syntax: node --check and git diff --check pass.
- Real vendored Lightweight Charts 4.1.1, actual trading_core.js and mobile_chart.js, deterministic OHLC fixture; no accounts or orders.
- Before fix: reproduction fails scale/readiness/visibility for live-before-history symbol changes and readable late-initialization framing.
- After fix: all 22 browser assertions pass (EUR/USD, BTCUSD, USD/JPY, ETHUSD; live-before-history, manual zoom, depth extension, empty history, timeframe reset, delayed chart initialization). Browser screenshot inspected.
- CLI Chrome headless exits 134 on this workstation, so the same regression BODY was rendered and executed in the in-app browser at localhost:8151/after.html. This is browser verification, not a successful CLI test run.
- Regression source: simulator/tests/test_mobile_chart_framing.py.

Deployment completed per operator output. Backup: /root/money-chart-framing-20261030T202144Z. Public asset content and HTTP 200 checks succeeded, services active. Further device feedback remains pending.
