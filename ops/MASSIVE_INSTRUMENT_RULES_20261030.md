# Massive verification and proposed demo rules — 2026-10-30

Status: local, not deployed, no runtime activation. This completes a reproducible
rules preview, not a live integration. Existing account rules remain unchanged.

## Actual provider checks

Authenticated, bounded GET queries using the locally configured Massive key.
No key is included in evidence. No production database access, subscription
changes or live WebSocket connections were made. Opening another socket could
compete with the existing connection quota; quote streaming remains unverified.
Evidence: `ops/evidence/massive_expansion_20261030.json`.

| Requested asset | Reference found | Actual data access | Remaining restriction |
| --- | --- | --- | --- |
| Gold | C:XAUUSD, active, unit one troy ounce | Latest bid/ask and three recent minute bars HTTP 200 | Streaming and chart/execution consistency not certified |
| Solana | X:SOLUSD, active, USD quote | Snapshot/trade and minute bars HTTP 200 | Snapshot is not a bid/ask quote; XQ streaming unverified |
| Kaspa | X:KASUSD, active, USD quote | Snapshot/trade and minute bars HTTP 200 | XQ streaming, precision and liquidity not certified |
| US30 | I:DJI, active Dow Jones index | Snapshot HTTP 403 | Resolve permission; index value is not a CFD bid/ask quote |
| WTI | CL, single NYMEX Light Sweet Crude Oil Futures | Product and dated contracts HTTP 200; snapshot HTTP 403 | Resolve permission and implement contract expiry/session lifecycle |

403 means the particular request was forbidden; it does not prove which paid
subscription is missing. No upgrade was purchased. Technical API access does
not establish commercial redistribution rights.

Futures discovery must specify date and type=single. The first unconstrained
query returned historical combinations, and product_code=CL alone returned
an intercommodity combo. A request using last_trade_date.asc sorting returned
400; the successful retry used an explicit last-trade-date filter. Do not use
any of those initial responses as the chosen front contract. Successful dated
results include CLZ6, expiring 2026-11-20, and CLF7, expiring 2026-12-21. No
contract was selected automatically; volume and quote freshness were not proven.

## Demo rules

The user approved 0.01% notional at opening for SOL/KAS in demo, and delegated
WTI selection to what Massive offers. WTI therefore uses a dated-futures price
reference, not a fabricated continuous cash series. This is a broker demo:
it does not route orders to NYMEX or promise physical delivery.

| Symbol | Units / lot | Min / step / max lots | Price increment | Leverage cap | Opening fee |
| --- | --- | --- | --- | --- | --- |
| XAU/USD | 100 troy oz | .01 / .01 / 50 | .01 USD/oz | 100 | ECN 7 USD/lot; VIP 3.50 USD/lot |
| SOLUSD | 1 SOL | .1 / .1 / 1000 | .01 USD | 10 | 0.01% of notional |
| KASUSD | 1 KAS | 1 / 1 / 100000 | .000001 USD | 2 | 0.01% of notional |
| US30 | 1 USD per point per lot | .1 / .1 / 100 | 1 index point | 20 | ECN 7 USD/lot; VIP 3.50 USD/lot |
| WTI | 1000 barrels | 1 / 1 / 10 | .01 USD/barrel | 10 | ECN 7 USD/lot; VIP 3.50 USD/lot |

Gold/SOL/US30 size, increment and risk limits come from existing disabled
SymbolSpecs, not a claim of venue requirements. Kaspa sizes/precision/leverage
and WTI demo leverage/max quantity are proposed conservative broker demo
parameters. WTI 1000 barrel size and .01 tick are verified provider contract
facts. The quoted leverage is not exchange initial margin. Noncrypto fees reuse
the approved ECN/VIP example rates; no account snapshot has been changed.

Effective leverage = min(account leverage, instrument cap).
Units = lots × units per lot.
Notional USD = units × quoted USD price.
Margin preview = notional / effective leverage.
Tick value USD = units × price increment.
SOL/KAS commission = notional × .0001, once at opening.
Other commission = lots × the tier rate.
USD long P&L before costs = units × (exit price − entry price); reverse for short.
Actual execution must use the proper bid/ask side and existing ledger posting.

Spread is VARIABLE and not filled in with an invented constant. The preview
returns null for spread and overnight financing, not zero. Existing account
markup/spread charging is not changed. Sessions: SOL/KAS 24/7; gold uses the
existing METALS_23_5 definition, subject to holiday/source checks; US30 keeps its
existing session candidate but it cannot be treated as confirmed for I:DJI;
WTI requires dated Massive schedules including breaks/holidays and expiry.
No automatic rollover, financing policy or forced expiry close has been enabled.

Sub-cent fees require attention: 1000 KAS at USD .03 means USD 30 notional and
USD .003 commission, which can round to zero in a cent-based ledger. No minimum
fee or hidden surcharge was added. The preview retains the exact amount; actual
posting must be validated with the existing rounding allocation before demo use.

## Local implementation and limits

`market_data/instruments/pilot_rules.py`: immutable proposed definitions and
Decimal-based preview. Not imported by consumers/feeds; never mutates registry.
`preview_instrument_pilot`: read-only command, explicit price/quantity inputs,
returns disabled status and blockers. No database queries or live prices.
`market_data/tests/test_instrument_pilot_rules.py`: quantity, fee, leverage,
precision math, invalid numbers and no-activation regression checks.

Run example in an isolated test environment:
`python manage.py preview_instrument_pilot --symbol SOLUSD --lots 10 --price 100`
This example uses an illustrative price, not a current quote.

84 tests passed, no skips, in an archive without .env or production databases:
/tmp/money-instrument-preview.C2WGJT/tests.log. The suite covers proposed rules,
existing admin comparison, catalogue, allowed-symbol dashboards and profile
bridges. No engine execution, webhook or live-feed code changed.

## Primary sources

- https://massive.com/knowledge-base/article/does-massive-support-commodities-data
- https://massive.com/docs/rest/forex/quotes/last-quote
- https://massive.com/docs/rest/crypto/snapshots/single-ticker-snapshot
- https://massive.com/docs/websocket/crypto/quotes
- https://massive.com/docs/websocket/indices/value
- https://massive.com/docs/rest/futures/products
- https://massive.com/docs/rest/futures/contracts
- https://massive.com/docs/rest/futures/snapshots/contracts-snapshot

Next activation gates: resolve 403s; capture bid/ask streams without competing
with production sockets; integrate account+instrument demo fee selection;
implement WTI dated lifecycle and session policy; validate end-to-end demo
execution before any real-account enablement. A green preview is not a release.
