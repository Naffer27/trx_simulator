# ECN/VIP catalogue alignment — 2026-10-30

Scope: align new real-account products with advertised minimums and leverage ceilings, preserve all existing accounts, and state actual charging semantics. This is not an instrument-pricing-engine redesign.

## Result

- `real-ecn`: minimum USD 10, maximum account leverage 500, existing USD 7/lot opening commission unchanged.
- `real-vip`: new product named Real VIP, ECN engine type, minimum USD 100000, USD 3.50/lot at opening, maximum account leverage 500.
- `real-standard`: inactive for new account creation; existing accounts and their frozen profiles remain untouched. Demo products unchanged.
- Both new real offers use 100/70 margin call/stop-out. Per-symbol leverage caps still apply, BTC at 20.
- Replace unsupported zero-spread claim with variable-spread wording. State separate spread charges and opening commission; do not change any spread settings or execution logic.
- Account catalogue/modal retain commission cents ($3.50) and identify account markup rather than calling it total spread.

## Apply

`align_real_account_catalog` previews by default. `--apply --backup /absolute/new/file.json` applies only after audited-value guards, saves the previous product rows in a new mode-0600 file, then commits the three product changes in one transaction. The command does not call account, wallet, transfer, or ledger mutation routines. Repeating the completed command makes no writes. The `real-vip` code deliberately uses ECN product_type; no new account type/migration is required.

Do not run the old `seed_account_products --force-update` after this release: its legacy defaults are not this commercial release. Normal seeding skips existing rows. Future installations must explicitly run this alignment after initial seeding.

## Validation

137 focused tests: 135 passed; two existing FundingProgramsCardTests text assertions failed. Both failures reproduced using account_open.html from HEAD (before this change). All 9 new release tests passed, including dry run, drift refusal, backup collision, idempotence, existing account/ledger preservation, VIP minimum enforcement and account creation, ECN USD 10 account creation, and VIP cents display.

Spanish and English landing templates rendered and retained two account cards and the three-column comparison table. No production tests or real trades were executed.

## Publication and rollback

Prepared package: `/home/naffer/money-catalog-release-20261030` on VPS 69.62.126.192; deployment requires owner sudo authentication. Script checks exact predecessor and package hashes, saves replaced files under a new private `/root/money-catalog-*` directory, applies the backed-up product command, restarts only daphne and checks public ES/EN HTTP responses. Failure handler restores the touched fields/files; a newly created VIP product is deactivated rather than deleted, preserving any account that may already reference it. Concurrent tariff changes stop automatic rollback for review.

## Remaining audit findings

Instrument admin remains reference-only. No account-by-symbol commission matrix was added. Manual and pending-order spread-markup fee paths remain different. Gold and US30 remain disabled. Existing account terms are not retroactively upgraded. Current public copy is explicit about variable spread instead of promising equivalence to Liquid's execution economics. Future admin edits require corresponding public-copy review; this change does not make the homepage dynamically generated from products.

## Publication confirmed

Owner execution completed successfully on 2026-10-30. Backup: `/root/money-catalog-20261030T171622Z`. Product alignment completed, a second invocation reported no writes, ES/EN public pages returned HTTP 200 with the expected content, and daphne/celery-worker/celery-beat were active. The deployment output reported existing accounts preserved. No further deployment is required for this release.
