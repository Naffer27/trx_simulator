# Pending-order spread parity — deployed, disabled by default

## Problem and scope

Manual market openings deduct a separate spread-markup fee. Pending-order
triggers previously deducted commission only. This opt-in change adds the same
static fee formula and monetary allocation to the shared trigger executor used
by WebSocket and Celery. Raw execution prices, contract sizes, leverage limits,
account catalogue, closing fees and IB rules are unchanged.

`PENDING_ORDER_SPREAD_FEE_ENABLED` defaults to false. No migrations or data
updates are required. Deployment was confirmed by the operator on 2026-10-30; the new fee remains disabled in production.

## Economics and accounting

For supported static configurations the opening spread fee is:

    effective spread pips × symbol pip size ÷ 2 × lots × contract size

Effective spread uses broker static spread plus the account's resolved markup,
with account bounds taking precedence over enabled instrument bounds. The
existing frozen commercial-profile resolver remains authoritative. Fresh broker
configuration is read from the database for pending triggers; this avoids a
Celery process depending on a WebSocket process's local cache.

Commission calculation remains unchanged. The shared allocation helper derives
posted charges from the persisted, two-decimal balance, preserving half-even
rounding and the manual path's residual allocation. Fee metadata captures the
pricing context and exact amount. Pending spread customer and broker entries
are mandatory in the same transaction. Failure rolls back the fill, charges and
pending status. The existing best-effort commission broker entry is unchanged.
The locked pending status prevents a second trigger from charging again.

## Limitations and activation conditions

- Dynamic spread configurations deliberately raise before execution under the
  new flag. The order remains pending. A quote-level pricing decision shared
  by WebSocket and daemon is required before supporting them; no silent static
  substitution is permitted. With the flag off legacy execution is preserved.
- This does not make the reference Instrument admin model authoritative, add
  per-account/per-symbol commission overrides, or implement an admin tariff matrix.
- Manual profiles may be cached; this is formula parity for equivalent static
  inputs, not a guarantee that concurrent configuration changes reach both
  workers at exactly the same instant.
- Review outstanding pending orders and client fee disclosures before activation:
  enabling the flag affects future triggers, including previously placed orders.
- Before rollout, verify production configuration is static, run PostgreSQL
  concurrency/rollback checks and a controlled demo fill through both trigger
  paths. SQLite unit tests do not certify concurrent production locking.
- Turning the flag off restores legacy future-trigger behavior. Do not remove
  or reverse already posted charges merely to roll back the feature.

## Local validation

Targeted Django suites: pending spread parity, ledger rounding reconciliation,
pending-order management, and unified raw execution/spread fee regression.
Tests cover EUR/USD and BTCUSD with ECN/VIP rates, legacy flag-off behavior,
static bounds, duplicate triggers, daemon execution, missing config, sub-cent
fees, dynamic refusal and rollback after either ledger insertion fails.

Validation result (2026-10-30): 64 tests run, 63 passed, 1 existing skip because
EUR/USD has zero legacy commission_pct. All 11 new parity tests passed.
Log: `/tmp/money-pending-parity-validated.log`.

Release validation reran all four suites with isolated local Redis: 99 tests
run, 98 passed, 1 existing skip. Log: `/tmp/money-parity-release-tests.log`.
This supersedes the earlier sandbox-blocked Redis integration result.

## Deployment evidence

Operator-provided output confirms successful installation on 2026-10-30,
Django system checks clean, helper import/calculation verified, feature flag
false, ES/EN homepage HTTP 200 and daphne/celery-worker/celery-beat active.
Backup: `/root/money-pending-spread-20261030T180422Z`.
No migrations or feature activation were performed. PostgreSQL concurrency
and controlled demo validation remain prerequisites for activation.

## Explicit DEMO validation (2026-10-30)

The full 11-case pending fee suite was also run with `account_type='DEMO'`,
including ECN/VIP commission profiles, manual/pending equivalence, daemon,
idempotency and transaction rollback. Combined regression result: 110 tests,
109 passed, 1 existing skip; isolated SQLite test database and local Redis.
Log: `/tmp/money-demo-parity-tests.log`.

This is automated DEMO-model validation, not a live browser or production
PostgreSQL certification. The production flag remains off. A live demo pilot
must be isolated from customer accounts: the current flag is global, so do not
switch it on globally merely to test one demo account. Use a staging service
with its own database and market-data connection, or implement and validate an
explicit demo-only rollout gate before any live pilot.

## PostgreSQL follow-up

The isolated PostgreSQL pilot found a backend-dependent half-cent rounding
mismatch; the allocation helper now explicitly quantizes the balance before
saving. Follow-up validation: 113 regression passes and 1 skip, plus a passing
concurrent-trigger test. See DEMO_REAL_RULES_VALIDATION_20261030.md for evidence
and limitations. This rounding correction is not yet deployed; the published
pending fee feature remains disabled. No historical entries were changed.
