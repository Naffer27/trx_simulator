# MONEY BROKER — BOOK B RELEASE CANDIDATE CERTIFICATION — V1

**Date:** 2026-10-02
**Status:** CODE/RC READY — NOT YET VPS DEPLOYED — NOT YET PRODUCTION CERTIFIED
**Scope:** Certification document only. This freezes exactly what is being declared a Release Candidate for Book B (B-Book). It is not a production go-live declaration, and the RC described here has not been deployed anywhere outside the current local development environment.

---

## 1. Exact Git Baseline

| | |
|---|---|
| Branch | `main` |
| HEAD | `189e3c17959ffafd8f12f45eea40ba2124069313` |
| HEAD message | `feat: add broker ledger backfill safety guard` |
| origin/main | `fcb501302bdd9ca4a9ea2b6d034850bb1ed21691` |
| Relationship | HEAD is exactly 1 commit ahead of origin/main, 0 behind |
| Working tree | Clean except pre-existing protected untracked files (DB backups, `.env.backup_*`, `docs/*.html/.pdf` design-locks, 5 readiness-audit draft `.md` versions, `simulator/tests/test_book06j1_population_engine_close_race.py`) |

This document itself does not change HEAD. No commit, tag, or push has been authorized or performed as of this writing.

---

## 2. What Constitutes Book B in This RC

Book B (B-Book) as certified here is:

- A fully internal dealing model: every real trade executes against the house, with zero external routing, zero liquidity provider (LP) connectivity, and zero A-Book/hybrid execution.
- Real money movement on deposits (NOWPayments) and withdrawals/funded payouts (NOWPayments payout, KYC-gated).
- A complete, independently certified broker-economics ledger (`BrokerLedger`) covering commission, spread, counterparty P&L, challenge fees, withdrawal fees, funded profit share, provider costs, and manual economic adjustments.
- A complete, independently certified IB (Introducing Broker) commission system — attribution, per-lot/challenge/deposit/commission/spread revenue-share triggers, treasury settlement, reversals, risk holds, and a self-serve IB portal.
- A complete challenge/funded-account engine with real phase evaluation and real funded-payout wallet credit.
- Supporting infrastructure (PostgreSQL/Redis/Celery+RedBeat/Daphne, backups with verified restore drills, secret-key guards) that is code-ready for private VPS deployment.

Explicitly **not** part of Book B as certified here: BOOK-04 routing, BOOK-05 liquidity, and BOOK-06 dealing-desk/canary machinery all exist in the codebase but are flag-gated off by default and produce no behavioral effect on real trades — see Section 8 and Section 12.

---

## 3. Django / Migration Checks

| Check | Result |
|---|---|
| `manage.py check` | System check identified no issues (0 silenced) |
| `manage.py check --deploy` | 5 warnings, all classified **production-config expected**, zero code blockers: `security.W004` (SECURE_HSTS_SECONDS unset), `W008` (SECURE_SSL_REDIRECT not True), `W012` (SESSION_COOKIE_SECURE not True), `W016` (CSRF_COOKIE_SECURE not True), `W018` (DEBUG=True) |
| `makemigrations --check --dry-run` | No changes detected |

All five `--deploy` warnings are environment-variable-only gaps, not missing code — see Section 11.

---

## 4. Full Test Suite Result

| Metric | Value |
|---|---|
| Total tests | 8,361 |
| Failures | 12 |
| Skipped | 18 |
| Errors | 0 |
| Duration | 966.1s |

**This RC is not "all green."** The 12 failures below are known, individually classified, and accepted as documented debt for this RC — not hidden, not silently waived.

---

## 5. The 12 Known Failures — Individually Identified and Classified

| # | Test | File | Classification |
|---|---|---|---|
| 1 | `test_apply_line_styles_dim_opacity_reduced` | `test_dashboard_order_lines_polish.py` | UI contract drift (dashboard.html JS source-string assertion) |
| 2 | `test_drag_sl_emphasis_present` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 3 | `test_drag_tp_emphasis_present` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 4 | `test_ensure_sl_uses_linewidth_1` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 5 | `test_entry_dim_color_gray_blue` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 6 | `test_entry_line_created_with_linewidth_1` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 7 | `test_sl_tp_price_lines_still_rendered` | `test_dashboard_order_lines_polish.py` | UI contract drift |
| 8 | `test_challenge_shows_neither_demo_nor_real_panel_title` | `test_dashboard_panel_mode.py` | UI contract drift |
| 9 | `test_demo_does_not_show_real_account_title` | `test_dashboard_panel_mode.py` | UI contract drift |
| 10 | `test_sl_tp_entry_pending_lines_still_axis_label_visible_true` | `test_chart_live_visual_filter_01.py` | UI contract drift |
| 11 | `test_update_pnl_titles_uses_backend_safe_pnl` | `test_fix05c_frontend_price_pnl_contract.py` | UI contract drift |
| 12 | `test_month_lots_correct` | `test_ib_admin_ops_04b.py` | Backend IB admin-dashboard aggregation bug — isolated to a reporting rollup display, **not** `IBCommissionObligation` creation, `BrokerLedger` correctness, or any money-moving path |

All 11 UI-classified failures are source-string/DOM-literal assertions against `dashboard.html`'s shipped JavaScript (expected `createPriceLine` call signatures, CSS color literals, axis-label visibility flags, panel-title text) — they test whether specific strings/behaviors still exist verbatim in the committed UI, not backend trading correctness, order execution, margin, or P&L calculation. None of the 12 touches a money-moving or order-execution code path.

A previously-identified 13th item (`test_two_concurrent_rejects_same_wr_exactly_one_wins_one_refund`, a concurrency/timing test) is known-flaky — it did not reproduce in the RC certification run and is recorded as nondeterministic, not as a fixed or regressed defect.

**This debt is carried forward into the RC, not resolved by it.**

---

## 6. Money-Path Certification

The following areas are backed by passing, dedicated tests at this exact HEAD (223 broker-economics/IB-trigger tests, 21 BROKER-ECONOMICS-05B tests, 515 broader targeted tests — none overlapping with the 12 known failures above):

- Deposit accounting (NOWPayments IPN, duplicate-callback protection)
- Withdrawal accounting (fee snapshot, OTP gate, payout pipeline)
- `BrokerLedger` and its BROKER-ECONOMICS-05B `economic_date` backfill-safety guard
- Economic adjustments (02B/02C, Owner-only TOTP-gated)
- Spread and commission revenue (both IB sweep functions, double-filtered on `created_at` + `economic_date`)
- Counterparty P&L (BOOK-02)
- Challenge revenue, funded payout economics, provider costs (04-series)
- IB obligations (idempotency via DB `UniqueConstraint`, reversal admin, risk holds)
- Treasury credit integration (maker-checker)
- Wallet/account balance integrity (explicit zero-side-effect tests on the 05B guard)
- Idempotency/replay protection at every layer checked (repeat sweep, concurrent sweep, obligation `get_or_create`)

**No known regression blocks this RC on the money path.**

---

## 7. Trading-Path Certification

Backend trading correctness, explicitly distinguished from the UI drift in Section 5:

- Market BUY/SELL, pending orders, position close — no failing test in this area; live-execution log output during the RC run shows correct accept/reject behavior (margin rejections, max-positions rejections, merges)
- SL/TP — execution/trigger logic unaffected; the SL/TP-named failures in Section 5 are chart-rendering/axis-label string assertions, not SL/TP trigger defects
- Margin (`margin_per_trade_exceeded`, `total_margin_exceeded`, `max_positions`) — all guards exercised and correctly rejecting in the RC run
- P&L — the one P&L-named failure in Section 5 is a dashboard title-string assertion about which function name is called, not a P&L calculation defect
- Spread, commission — certified in Section 6
- Daily/max drawdown, stale-price protection, negative-balance guards, price integrity, exposure/risk guards — no failing test in any of these areas
- WebSocket/order lifecycle — account-blocked, margin-call, and stopout paths all exercised correctly in the RC run's live output

**No backend trading-engine defect is present in the 12 known failures. No trading-logic failure is being minimized, and no UI-only failure is being mischaracterized as an engine defect.**

---

## 8. B-Book Boundary

Confirmed directly against code at this HEAD:

- `ROUTING_ENGINE_ENABLED` (settings.py:860) — defaults `False`, not set in `.env`
- `LIQUIDITY_ENGINE_ENABLED` (settings.py:896) — defaults `False`, not set in `.env`
- `DEALING_DESK_EXPOSURE_ENABLED` (settings.py:924) — defaults `False`, not set in `.env`
- `DEALING_DESK_EXPOSURE_ACCOUNT_IDS` allowlist — empty
- No LP integration code exists anywhere in the codebase (no HTTP/network client code in any routing or liquidity module)
- BOOK-04 routing never produces a `book_mode` other than `B_BOOK`
- BOOK-05 liquidity remains simulation-only; `LiquidityDecision` never writes back to `RoutingDecision`
- `DealingDeskDecision.is_simulated_hedge` is a classification label only — no network call, no external connection, and no code path exists by which it could become a real hedge

**This RC is 100% B-Book with zero external execution capability, confirmed at the code level, not merely by configuration.**

---

## 9. Frozen Owner Decisions

| Decision | Status | Evidence |
|---|---|---|
| **KYC-A** | FROZEN | KYC blocks only funded payout (`views.py:2523`) and withdrawal (`views.py:2677`) — the only two KYC gates in the codebase. Does not block registration, demo, real account opening, deposits, BUY/SELL, pending orders, position close, or challenges. |
| **CPA-A** | FROZEN | `CPA_BONUS` (IB-COMMISSION-TRIGGERS-02D) remains fully inactive — no sweep function, no generator function, confirmed by exact enumeration against `ib_commission_triggers.py`/`ib_commission.py`. Schema exists but is unreachable by any code path. |
| **CANARY-A** | FROZEN | `DEALING_DESK_EXPOSURE_ENABLED=False`, allowlist empty, decision engine/audit/shadow logging continue running unmodified. |

These are Owner business decisions, not engineering gaps — all three require zero code changes to remain exactly as frozen.

---

## 10. Known Accepted Debt

**`simulator/tasks.py:404`** — the `BrokerRevenueSnapshot` incremental revenue snapshot task filters purely on `BrokerLedger.created_at`, with no `economic_date` awareness (the guard introduced by BROKER-ECONOMICS-05B does not extend to this reporting task). A future historical-import/backfill, even once safely excluded from generating IB obligations by the 05B guard, could still appear as a revenue spike on this dashboard snapshot.

This is registered as **future historical-import/backfill debt** — not corrected in this RC, and not a precondition for RC readiness, VPS deployment, or Book B operational status. It must be addressed as its own block **before** any future historical-import/backfill capability is ever built.

---

## 11. VPS Configuration Requirements

**CODE READY (no changes needed):** PostgreSQL configuration, Redis, Daphne/Channels, Celery + RedBeat, Nginx template, systemd units (daphne, celery-worker, celery-beat, 3× backup), backup + restore scripts (with verified restore drills), `DJANGO_SECRET_KEY`/`TOTP_ENCRYPTION_KEY` production guards, 99 migrations all applying cleanly.

**VPS CONFIG REQUIRED (environment only, no code changes):**
- `SECURE_HSTS_SECONDS` (real value)
- `SECURE_SSL_REDIRECT=True`
- `SESSION_COOKIE_SECURE=True`
- `CSRF_COOKIE_SECURE=True`
- `DEBUG=False`
- Real production `DJANGO_SECRET_KEY`
- Real production `TOTP_ENCRYPTION_KEY`
- Production database credentials
- `SENTRY_DSN`

**One minor code-adjacent gap carried from the Master Audit:** the `anymail` package is still absent from `requirements.txt` despite being documented in `.env.example` — relevant only if a documented SendGrid/Mailgun/Postmark/SES backend is actually used.

No secrets are recorded in this document.

---

## 12. Explicit Scope Exclusion — LP / External Execution / A-Book

**LP (liquidity provider) integration, external execution, and A-Book/hybrid routing are explicitly NOT part of this Release Candidate.** BOOK-04 (routing) and BOOK-05 (liquidity) exist in the codebase as flag-gated, simulation-only infrastructure with zero production effect (see Section 8). BOOK-06 (dealing desk) reached internal "ready for canary" engineering status in a prior audit but remains off by Owner decision (CANARY-A, Section 9). None of this machinery routes, hedges, or executes against any real external counterparty. This RC certifies a 100% internal B-Book dealing model only.

---

## 13. Deployment Status — Not Yet Deployed

**This Release Candidate has NOT been deployed to any Private VPS or any environment outside local development.** No VPS provisioning, no staging deployment, and no production deployment has occurred as of this document. HEAD `189e3c1` exists only in the local repository and has not been pushed to `origin` as of this writing (origin/main remains at `fcb5013`).

---

## 14. Criteria for Successful Private VPS Deployment

A Private VPS deployment of this RC will be considered successful when, at minimum:

1. The exact commit certified in this document (or an explicitly re-certified successor) is deployed, with `git rev-parse HEAD` on the VPS matching the pushed/tagged commit.
2. All VPS configuration items in Section 11 are set with real production values (verified via a fresh `manage.py check --deploy` run on the VPS returning the expected warnings resolved).
3. `manage.py check`, `manage.py migrate`, and `collectstatic` all complete without error against the production PostgreSQL database.
4. Daphne, Celery worker, and Celery Beat (RedBeat) all start and remain running under their systemd units.
5. A real backup is taken and a real restore drill is exercised at least once against the VPS's own Postgres instance (not just locally).
6. The application is reachable over HTTPS with HSTS/secure-cookie settings active, confirmed by a live request, not just configuration inspection.
7. No code changes are made during deployment beyond configuration — if a code change becomes necessary, this RC certification is void and must be re-run against the new HEAD.

---

## 15. Criteria for Declaring Book B Operational After VPS E2E Certification

Book B will be considered operational (able to safely handle real money from real clients) only when, in addition to the deployment criteria in Section 14:

1. A full end-to-end manual test cycle is performed directly on the VPS: real account registration, real deposit via NOWPayments, a real BUY and a real SELL trade with SL/TP, a real position close, a real challenge purchase and phase progression, and a real withdrawal request through to payout completion.
2. Celery Beat's scheduled tasks (including the IB commission sweep and the deposit/withdrawal reconciliation tasks) are confirmed actually firing on the VPS's real schedule, not just present in `CELERY_BEAT_SCHEDULE`.
3. The three frozen Owner decisions (Section 9) are re-confirmed as still correctly reflected in the deployed code — no accidental drift between this RC and the deployed artifact.
4. The known debt item (Section 10) and the 12 known test failures (Section 5) are re-reviewed by the Owner at that time and either re-accepted as still non-blocking, or explicitly scheduled for remediation — Book B is not to be silently declared "fully certified" with this debt forgotten.
5. A real monitoring/alerting channel (Sentry, or equivalent) is confirmed receiving events from the live VPS.
6. The Owner explicitly signs off, in writing, that Book B is open for real client funds — this document and the RC certification result alone do not constitute that sign-off.

**Until all of the above are met, Book B's status remains CODE/RC READY — NOT YET VPS DEPLOYED — NOT YET PRODUCTION CERTIFIED.**
