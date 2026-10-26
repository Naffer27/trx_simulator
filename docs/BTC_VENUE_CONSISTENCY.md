# BTC venue consistency — 2026-10-26

## Scope approved by owner

Keep Massive as provider; pin BTCUSD live quotes (XQ) and trades (XT) to Coinbase (Massive exchange ID 1). Other symbols remain unchanged. Retain existing validation, event contracts, caches, pricing/execution/P&L/margin/risk logic, consumers and webhooks. Reject unknown venues/metadata and events older than the last accepted timestamp, independently for quotes and trades, comparing milliseconds before conversion to seconds. Equal milliseconds remain valid. Watermarks persist across reconnects. No fabricated/smoothed prices or automatic venue fallback.

This changes the source selection of BTC execution inputs, not the execution formula. It is not merely a chart animation fix. If Coinbase quotes stop, existing staleness protections remain in force; other venues must not refresh the selected quote. The new order gate does not replace age validation. Initial REST resync and historical aggregate data remain unchanged and can still reflect multiple venues; this patch does not rewrite old candles or cure history-loading/axis jumps.

## Evidence

Massive historical trades, UTC 2026-10-24 01:22:45–01:23:25, covering the supplied video: 222 trades. Coinbase: 162, range 80731.98–80764.48. Bitfinex: 14, range 80859–80869. Maximum adjacent mixed-venue change: $124.31; Coinbase-only: $6.36. The fixture preserves price/size/exchange and converts nanoseconds to the WebSocket millisecond contract. These are trade observations, not a historical capture of XQ quotes.

Official schemas: https://massive.com/docs/websocket/crypto/quotes and https://massive.com/docs/rest/crypto/trades . Exchange mapping was verified via the provider's /v3/reference/exchanges endpoint. A current REST snapshot also reports a Coinbase trade. REST quote queries returned no rows. A parallel WebSocket observation was refused with max_connections; existing production connections were not stopped or replaced for this observation.

## Validation and activation gate

156 tests pass across test_btc_venue_ordering, test_crypto_quote_dedup_01, test_golden_marketdata_crypto_01_massive_crypto_live and test_massive_crypto_trade_candles_01. New tests include actual-video-window replay, rejection/order/malformed metadata, independent quote/trade watermarks, reconnect persistence, duplicates, existing value validation and ETH isolation. The only change to an existing test is the separately owner-approved addition of x:1 to one fictitious XQ event; assertions are unchanged.

The feed emits a rate-limited diagnostic at most every 10 seconds for validated selected BTC quotes (exchange identity and event timestamp only; no accounts, secrets, orders or balances). Activation must observe at least two fresh, progressing selected-quote events from the normal Daphne connection and a healthy public page; otherwise restore the prior commit. Leave a BTC page open so its ordinary subscription reconnects. No migration, credentials change, payment or order is part of deployment. Live quote continuity remains unconfirmed until that gate succeeds.
