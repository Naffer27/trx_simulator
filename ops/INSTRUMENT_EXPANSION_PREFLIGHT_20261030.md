# Requested instruments — preflight, no activation

Scope confirmed by owner: US30, WTI (not Brent), gold XAU/USD, Solana and Kaspa.

| Instrument | Existing executable registry state | Missing before a demo pilot |
| --- | --- | --- |
| US30 | Defined, disabled; contract 1, lot min/step 0.1, leverage cap 20 | No provider mapping in its SymbolSpec; verify a suitable executable quote source and sessions |
| XAU/USD | Defined, disabled; contract 100 oz, min/step 0.01, leverage cap 100 | Finnhub mapping exists but live source coverage, quote freshness and historical chart consistency must be certified |
| SOLUSD | Defined, disabled; contract 1 coin, min/step 0.1, leverage cap 10 | No provider mapping in its SymbolSpec; configure and verify quotes and history |
| WTI | No executable SymbolSpec | Define the offered contract (cash CFD vs futures), quote source, size, precision, lot steps, margin, fees and calendar/rollover policy |
| Kaspa | No executable SymbolSpec | Verify provider coverage and quote currency, define contract size, precision, steps, risk cap and fees |

Evidence: market_data/symbol_specs.py, market_data/feeds.py,
market_data/sessions/models.py, simulator/management/commands/seed_instruments.py.
The energy calendar currently resolves to UNKNOWN. Registry values above are
existing code values, not approved new commercial terms or verified vendor specs.

Order of implementation: obtain verified provider symbols and permissions;
define contract and economic terms; add disabled specs/adapters and tests;
validate quote freshness, spread units, P&L and margin per symbol; validate
sessions and market closure; demo end-to-end; then controlled production enable.

The Instrument admin table remains reference-only. Editing it does not by
itself change the live registry. A per-account/per-symbol tariff matrix is not
implemented by the pending spread patch. Do not apply the same dollar-per-lot
charge blindly to contracts with different economic sizes.

No new instrument, provider subscription or production trading was enabled.

## Follow-up: provider verified, 2026-10-30

See `MASSIVE_INSTRUMENT_RULES_20261030.md` and the sanitized JSON evidence.
Massive reference matches exist for all five targets. Gold bid/ask and history,
SOL/KAS trade snapshots and history returned HTTP 200. DJI and CL snapshot
requests returned HTTP 403. New demo rule previews are local and disabled;
no executable mapping or account commission policy has been switched on.
