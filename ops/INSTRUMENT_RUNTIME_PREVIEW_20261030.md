# Instrument runtime comparison — local, not deployed

Adds a read-only panel to the existing Instrument admin detail. It reads
SymbolSpec and compares equivalent contract, lot, precision, margin mode,
quote currency and enabled fields with the saved reference row. Unknown
instruments remain unknown; nothing allocates or enables a new instrument.

No changes to consumers, price feeds, webhooks, order execution, account
snapshots, migrations or production data. The reference table remains editable
and non-authoritative. This change does not make admin edits control trading.

Commission and spread are intentionally excluded from the comparison: their
reference fields are not equivalent to the account-specific commercial rules.
The panel explicitly says that it is not a price or execution quote.

Validation: 77 tests passed in an isolated archive without .env or production
DB. Suite: simulator.tests.test_instrument_runtime_preview,
simulator.tests.test_instrument_catalog,
simulator.tests.test_dashboard_allowed_symbols,
market_data.tests.test_instrument_bridges,
market_data.tests.test_instrument_profiles.
Log: /tmp/money-instrument-preview.zoTQb1/tests.log.
The isolated test settings used Django StaticFilesStorage so dashboard tests
could render without a collected deployment manifest. No production settings
were edited. New SimpleTestCase tests prohibit database access and verify
reference edits cannot mutate runtime specs, disabled targets remain disabled,
unknown targets get no fabricated defaults and HTML values are escaped.

Corrected audit finding: BTCUSD lot_step is 0.001 in the current registry.
Do not reuse the earlier conversational claim of 0.01 as an execution rule.

Remaining expansion work: certified live quotes and historical data for gold,
US30 and SOL; contract/source/session definition for WTI; source and commercial
specification for Kaspa; then end-to-end demo validation. No new asset is ready
for real-client activation on the strength of this admin-only change.
