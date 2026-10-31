# Gold demo release — deployed 2026-10-31

MASSIVE_GOLD_DATA_ENABLED=1 opts XAU/USD into the existing shared Massive forex data connection. GOLD_DEMO_ACCOUNT_IDS=3,4,5,6 limits trading/catalog access to the existing ADMIN2 and blameus30fx USD demo accounts. Both checks fail closed. Global SymbolSpec activation and real accounts remain unchanged.

Gold authorization is checked at the WebSocket boundary and again on the locked database account before opening. Gold pending orders are disabled for this pilot; closing existing positions remains available independently of the opt-in. Gold display precision is 0.01. No balances, commission snapshots or instrument spreads are modified.

The accounts page displays the stored demo product name and per-lot opening commission: ECN USD 7, VIP USD 3.50. Both pilot profiles retain USD 100,000 practice balances. Amounts use grouped display formatting; stored values and form parsing are unchanged. Spanish and English presentation checked.

REST evidence: evidence/gold_readiness_20261031.json. Gold quote/history HTTP 200; latest quote Friday 2026-10-30 20:59:32 UTC, not a fresh Saturday executable price. US30 I:DJI remains HTTP 403 and disabled. No live upstream WebSocket was opened for this audit.

Validation: 143 isolated tests passed, no skips, /tmp/money-gold-demo.mH93m4/tests.log. Includes shared reader/history regressions, account isolation, atomic real-account rejection, demo gold opening/closing accounting, margin/P&L, spread-unit explanations and account card rendering. No real orders were submitted. Open-market live quote and demo exercise remain to be verified after publication.

Deployment package: /tmp/money-gold-demo-release, remote /home/naffer/money-gold-demo-release. Verifies source hashes and demo identities before installing; backs up all changed files and configuration; rolls back on failure. Restarts daphne, verifies public resources and service health. No migrations. Administrator sudo is required. Package preparation does not mean deployment succeeded.

Deployment completed per operator output: /root/money-gold-demo-20261031T141623Z. Initial HTTP 502 recovered on retry; public checks returned 200 and three services active. No migrations or balance changes. Live open-market demo exercise remains pending.
