# Demo accounts with real ECN/VIP trading rules

Owner requirement: demo must follow real trading rules, using fictitious funds.

The isolated test creates the current published ECN/VIP products from the
existing release definitions. It derives demo products from their persisted
fields, changing only demo identity, family/type and fictitious starting balance.
Both accounts are created through the existing /accounts/create/ view. This
checks the actual snapshot creation path, not a separate mock account constructor.

Verified:
- Commission, spread policy, leverage, allowed symbols, lot ceiling, margin-call,
  stop-out and margin concentration snapshots match their real counterpart.
- Real and demo pending fills deduct equal commission and spread amounts.
- Demo creation does not debit wallet funds.
- Later product edits do not silently reprice an existing demo.
- The ECN account leverage of 500 remains subject to BTC's cap of 20;
  0.01 BTC at the test price 80,000 requires 40 of margin in both accounts.

These checks do not claim certified live execution quality, slippage, holidays,
all stop-out scenarios or production PostgreSQL concurrency.

## Isolated runner

`bash ops/run_isolated_demo_rules.sh sqlite`

Archives tracked code into a fresh temporary directory, excludes .env and
untracked backups, copies the new test, uses an empty process environment and
an ephemeral test database plus a dedicated local Redis on port 16391.
No VPS, payment keys, provider keys or production database are loaded. The
runner stops its own services on exit and preserves logs in the printed folder.
It does not start a public site or create demo accounts on production.

`bash ops/run_isolated_demo_rules.sh postgres`

Uses an ephemeral PostgreSQL cluster with a private Unix socket, no TCP listener,
and a separate test database. Requires local PostgreSQL tools; DEMO_PG_BIN can
select their directory. DEMO_REDIS_SERVER can select the Redis binary.

Validation on 2026-10-30: 26 tests passed in the isolated SQLite environment
(log /tmp/money-demo-pg.GtqkUW/tests.log). PostgreSQL bootstrap was blocked by
this execution sandbox's shared-memory restriction (shmget); no PostgreSQL
validation is claimed. Production fee activation remains disabled.

Before a customer-facing demo rollout, prepare isolated staging, verify real
quotes and PostgreSQL locking/rollback there, and then test the browser flow.
The current global fee flag must not be turned on just to test one demo account.

## PostgreSQL and browser evidence — follow-up 2026-10-30

Executed against a new private PostgreSQL 16 cluster under /tmp on the VPS,
using an archived code copy, empty environment, no production .env, no TCP
PostgreSQL listener and temporary Redis. Production services were not restarted.
Services belonging to the test were stopped on exit; test logs were retained.

The first PostgreSQL run found a real half-cent rounding mismatch for BTC:
PostgreSQL NUMERIC persisted a different rounded balance from the half-even
ledger allocation (e.g. a .075 fee posted .08 while balance decreased .07).
The fix quantizes the resulting balance explicitly before persistence and
returns that same value to the caller, so database backends cannot choose a
different rounding policy. No historical balances or entries are rewritten.

Regression results after correction:
- PostgreSQL: 114 tests, 113 passed, 1 existing skip;
  /tmp/money-demo-validation.B5p63u/tests.log on VPS.
- Additional PostgreSQL TransactionTestCase: two concurrent worker connections
  trigger the same DEMO pending order; exactly one fill and one commission/spread
  pair. Passed, /tmp/money-demo-validation.q6hXvr/tests.log on VPS.
- SQLite: 115 tests, 113 passed, 2 expected skips (PostgreSQL concurrency and
  zero legacy EUR/USD commission); /tmp/money-demo-pg.V1yKUL/tests.log locally.

The ledger test harness now invokes the real atomic synchronous body on its
TestCase connection. Previously the Channels connection-cleanup wrapper closed
PostgreSQL's test transaction; the correction is test-only. The raw-execution
regression now expects the response balance to equal the persisted two-decimal
balance, instead of the unrounded intermediate amount. Raw price and fee
formulas are unchanged.

Browser validation at http://127.0.0.1:8148:
- ECN and VIP demo accounts created successfully in an isolated SQLite database.
- Each has 100,000 fictitious balance, leverage snapshot 500, margin call 100,
  stop-out 70; commissions 7 and 3.50 per lot respectively.
- Wallet remains 0; no real funds or payment credentials are present.
- Verified account list and VIP confirmation flow. No live quote/trade browser
  certification: the temporary site has no market-data credentials.
- Before public rollout, replace the catalogue's hardcoded '$10,000 practice
  capital' heading with product-specific copy and remove real-deposit wording
  from demo descriptions. Existing production demo products were not edited.

This follow-up and rounding correction are local source changes, not a new
production deployment. The global production pending-fee flag remains off.
