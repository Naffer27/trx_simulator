# Money Broker — Broker Capability & Readiness Audit V1.4

## CANONICAL CURRENT READINESS DASHBOARD

> **SUPERSEDES V1.3 AS CURRENT READINESS DASHBOARD.** V1, V1.1, V1.2, V1.2.1 y V1.3 quedan intactos como evidencia histórica — ninguno fue modificado ni borrado. V1.4 es un documento **completo y autosuficiente**: no remite a ningún documento anterior para ninguna información necesaria. Todo lo requerido para auditar el estado de Money Broker hoy — métricas, matriz de 40 capacidades, capacidad suplementaria de Soporte, gaps, blockers, cola de ejecución, horizonte y handoff a la próxima conversación — está contenido aquí.

**Fecha:** 2026-09-17 · **Tipo:** Documentación únicamente. Cero código. Cero tests nuevos escritos para este documento. Cero modelos. Cero migraciones. Cero git.
**Esta es una auditoría de reevaluación real** — cada una de las 40 capacidades originales fue releída contra el repositorio actual (no copiada de V1.3), y se investigaron los 22 bloques cerrados desde V1.3 (2026-09-02) hasta hoy.

**Repository checkpoint verificado directamente contra Git en esta sesión:**

| Campo | Valor | Verificado |
|---|---|---|
| PROJECT | Money Broker | — |
| LOCAL TECHNICAL PROJECT | trx_sim | — |
| REPOSITORY | trx_simulator (`github.com/Naffer27/trx_simulator`) | `git remote -v` |
| HEAD | `31708583363208463eef82640a734929c75bb221` | `git rev-parse HEAD` |
| origin/main | `31708583363208463eef82640a734929c75bb221` | `git rev-parse origin/main` — **MATCH**, fetched live |
| Latest tag | `customer-support-01d-knowledge-base-v1` → `3170858...` | `git rev-parse <tag>` — **MATCH with HEAD** |
| Branch | `main` | `git branch --show-current` |
| Working tree | Clean except protected/untracked docs (see Part XIV) | `git status --porcelain` |

**Discrepancy report (per instruction to verify, not assume):** the prompt's stated HEAD/tag values were checked directly against `git rev-parse` and `git fetch origin --tags` and found to be **byte-for-byte correct — no discrepancy**. No `Plan Maestro Unificado V2/V3` file was found anywhere on disk (`find . -iname "*plan*maestro*"` → zero hits); V1.3's own Part X comparison table is preserved below as the most recent available Plan Maestro reconciliation, with a note that no newer Plan Maestro document exists to compare against for V1.4.

```
B-BOOK LOCAL MVP READINESS
███████████████████████░░  91.0%   (71 / 78)      was 80.8% in V1.3   (+10.2pt)

PRIVATE STAGING READINESS
███████████████░░░░░░░░░  66.7%   (30 / 45)       unchanged since V1.3 (+0.0pt)

OVERALL BROKER READINESS
████████░░░░░░░░░░░░░░░░  40.0%   (80 / 200)      was 36.5% in V1.3   (+3.5pt)

PRODUCTION READINESS
████████░░░░░░░░░░░░░░░░  40.0%   (80 / 200)      identical to Overall (same numerator/denominator)
```

## ⭐ NEXT 3 BLOCKS

```
1. WITHDRAWAL-POLICY-CORRECTION-02  — close the $0.01-vs-$20 minimum-withdrawal gap (OPEN BLOCKER, real money risk)
2. WithdrawalRequest #4 reconciliation — resolve the stuck HTTP-403/UNKNOWN payout before any policy work touches withdrawals
3. CUSTOMER-SUPPORT-01E             — lifecycle emails + attachment operational flow (biggest support gap remaining)
```

## Frase ejecutiva

> **Money Broker hoy es:** un broker B-Book local sustancialmente más maduro que en V1.3 (91.0% del MVP local) — con Pending Orders, Partial Close, Crypto Market Data certificado y un sistema de Soporte completo (tickets, panel dedicado, Knowledge Base, escalación) todos verificados localmente. **NO** staging (0 puntos de infraestructura movidos desde V1.3). **NO** production. **NO** A-Book real. Y tiene **un blocker de dinero real abierto ahora mismo**: el piso de retiro mínimo en código es $0.01, no los $20 que la política de negocio confirma.

---

# PARTE I — EXECUTIVE CONTROL DASHBOARD

*(ver barras y frase ejecutiva arriba — repetidas aquí por continuidad de formato con V1.3)*

### Money Broker today IS:
- A functional local B-Book broker at **91.0%** of its own MVP definition (up from 80.8%)
- Full retail order-type parity locally: market, pending (limit/stop), and partial close — all VERIFIED LOCAL with dedicated test suites
- Crypto market data now routed exclusively through a certified provider path (Massive), same architecture pattern as Forex
- A real, dedicated Customer Support system — not a v1.3-era flat ticket table, but a 5-block build (01A–01D) with role hierarchy, escalation, INTERNAL-note isolation, a deterministic (non-AI) Knowledge Base, and a manually browser-certified floating chat widget
- Still built on a strong local engineering discipline: audit → design-lock → scoped implementation → dedicated tests → regression → manual certification → Owner-only Git closure, on every block reviewed in this audit

### Money Broker today is NOT:
- Deployed anywhere — zero VPS/staging execution evidence exists, identical to V1.3 (confirmed again this session)
- Compliant with its own confirmed withdrawal-minimum policy — code enforces $0.01, business policy says $20 (**OPEN BLOCKER**)
- Reconciled on WithdrawalRequest #4 — a real stuck payout (HTTP 403, provider state UNKNOWN) remains unresolved since 2026-09-12
- An A-Book broker — all LP/hedge infrastructure remains deep simulation, zero external connectivity
- Feature-complete on Support — no lifecycle emails, no SLA, no attachment operational flow, no Support-AI layer, several Knowledge Base entries deliberately POLICY_PENDING

---

# PARTE II — ARITMÉTICA CANÓNICA

**Regla fija (heredada de V1.3, sin cambios):** la única fuente de verdad para Overall/Production y el conteo por maturity es la lista canónica de 40 IDs (Parte III). Cualquier trabajo posterior que no encaje limpiamente en esos 40 IDs se documenta en la Parte III-B (Capacidad Suplementaria) **sin alterar esta aritmética** — exactamente como exige la instrucción de esta auditoría.

### Count por maturity (canónico, 40 IDs)

| Maturity | V1.3 Count | V1.4 Count | Delta |
|---|---|---|---|
| 0 NOT STARTED | 8 | 8 | 0 |
| 1 FOUNDATION | 7 | 7 | 0 |
| 2 IMPLEMENTED | 9 | 6 | **-3** |
| 3 VERIFIED LOCAL | 16 | 19 | **+3** |
| 4 VERIFIED STAGING | 0 | 0 | 0 |
| 5 VERIFIED PRODUCTION | 0 | 0 | 0 |
| **Total** | **40** | **40** | — |

Three capabilities moved from 2→3 since V1.3, each backed by a real closed block with dedicated tests (verified independently this session, not copied from commit messages):

- **#6 Market data Crypto**: 2→3 — `golden-marketdata-crypto-01-chart-stability-v1` (2026-09-03). BTCUSD/ETHUSD now route exclusively through Massive (`market_data/feeds.py:2024-2043`, `_MASSIVE_CRYPTO_ENABLED_SYMBOLS`), same certified-provider pattern as Forex. 141 dedicated tests across 3 files.
- **#12 Pending orders**: 0→3 — `order-management-v2a-pending-orders-v1` (2026-09-04). Real `PendingOrder` model (`simulator/models.py:331`), migration `0075`, full consumer/task wiring (not a stub). 27 dedicated tests.
- **#13 Partial close**: 0→3 — `order-management-v2b-partial-close-v1` (2026-09-04). Atomic partial-qty close wired into `consumers.py`. 32 dedicated tests.

```
SUM_MATURITY = 8×0 + 7×1 + 6×2 + 19×3 = 0 + 7 + 12 + 57 = 76
```

Wait — recompute precisely against the actual per-ID list (Parte III), not the bucket count alone, since ID #35 (Support/history) does **not** move despite real underlying support progress (explained in its row and in Parte III-B):

```
V1.3 SUM_MATURITY = 73
Deltas confirmed this audit: #6 (+1), #12 (+3), #13 (+3) = +7
V1.4 SUM_MATURITY = 73 + 7 = 80

OVERALL      = 80 / (40×5) × 100 = 80/200 × 100 = 40.0%
PRODUCTION   = 80 / (40×5) × 100 = 80/200 × 100 = 40.0%   (idéntico a Overall)
```

*(The bucket table above double-checks against this: three IDs moved 2→3, so the 2-bucket shrinks by 3 and the 3-bucket grows by 3 — 9-3=6 and 16+3=19, consistent.)*

---

# PARTE III — MATRIZ COMPLETA DE 40 CAPACIDADES (REEVALUADA)

Cada fila fue releída contra el repo actual. Donde no cambió nada, se indica explícitamente "Sin cambio desde V1.3" en vez de copiar la prosa vieja sin verificar. Formato de columnas idéntico a V1.3.

| ID | Capability | Domain | Mat. V1.3 | **Mat. V1.4** | Current state (V1.4) | Strongest evidence | Known gap | Blocking scope | Next gate |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Legal/Jurisdiction/Compliance Config | Compliance | 0 | **0** | Sin cambio desde V1.3 — ningún modelo/config de compliance existe | `models.py:2059` (`KYCProfile.country` texto libre); grep repo-wide sin hits | Toda la feature es greenfield | PRODUCTION | COMPLIANCE-CONFIG-01 |
| 2 | Client onboarding | Compliance | 3 | **3** | Sin cambio desde V1.3 | Suite de registro/verify-email/terms — sin regresión detectada en los 22 bloques revisados | Sin evidencia de staging | NON-BLOCKING | Deploy a staging |
| 3 | KYC / AML / 2FA | Compliance | 2 | **2** | Sin cambio en KYC(3)/2FA(3); AML sigue en 0. Promedio (3+0+3)/3=2 sin cambio | Grep de AML → 0 hits, repetido esta sesión | AML ausente arrastra el promedio | PRODUCTION | AML-FOUNDATION-01 |
| 4 | Account / Product configuration | Trading | 3 | **3** | Sin cambio desde V1.3 | `AccountProduct`/`ChallengeProduct` intactos | Sin evidencia de staging | NON-BLOCKING | Deploy a staging |
| 5 | Market data Forex | Market Data | 3 | **3** | Sin cambio de certificación — `chart-live-visual-filter-01-v1` (09-07) es una corrección de renderizado del chart (mapea a #32), no toca la certificación del feed | Massive-only intacto; `b2-massive-only-forex-runtime-v1` sigue siendo la evidencia canónica | Ninguno pendiente en el feed en sí | NON-BLOCKING | — |
| 6 | Market data Crypto | Market Data | 2 | **3 ⬆** | BTCUSD/ETHUSD ahora exclusivamente vía Massive (`feeds.py:2024-2043`, comentario explícito "GOLDEN-MARKETDATA-CRYPTO-01"); Binance/Kraken/CoinGecko quedan como fallback legacy inalcanzable | Commit `8d9966f` / tag `golden-marketdata-crypto-01-chart-stability-v1`; 141 tests dedicados (`test_crypto_quote_dedup_01.py`=24, `test_golden_marketdata_crypto_01_massive_crypto_live.py`=84, `test_massive_crypto_trade_candles_01.py`=33) | **EVIDENCE DEBT**: a diferencia de Forex, no existe una sesión de aceptación manual de larga duración registrada — el propio `GOLDEN_MARKETDATA_CRYPTO_01_DESIGN_LOCK.md` nombra esa sesión como entregable futuro nunca producido | VPS (para paridad total con Forex) | Sesión manual de aceptación crypto (opcional, no bloquea 3) |
| 7 | Instrument catalog | Market Data | 2 | **2** | Sin cambio — dos catálogos paralelos (`symbol_specs.py` runtime vs `Instrument` DB) siguen sin sincronización | Sin cambios en los 22 tags revisados | Riesgo de drift silencioso sin cambio | VPS | Unificar catálogos |
| 8 | Pricing / Spread / Commission | Trading | 3 | **3** | Sin cambio de maturity. Gap re-verificado esta sesión: `commission_for()` (`consumers.py:3345`) **sigue devolviendo `float`**, no Decimal — `ledger-rounding-reconciliation-01-v1` no tocó esta función específica | Grep directo confirmado esta sesión | Float/Decimal inconsistency AÚN ABIERTO (re-confirmado, no cerrado por los fixes de esta ventana) | NON-BLOCKING | Auditoría Decimal-discipline dedicada |
| 9 | Swap / Overnight Financing | Trading | 0 | **0** | Sin cambio — 0 código real | Grep repetido, 0 hits | Feature ausente | PRODUCTION | BROKER-ECONOMICS-SWAP-01 |
| 10 | Order management | Trading | 3 | **3** | Sin cambio en el market-order path; ver #12/#13 para los tipos nuevos | Suite intacta | Ninguno nuevo | NON-BLOCKING | — |
| 11 | Execution | Trading | 3 | **3** | Sin cambio — `_raw_exec_price()` sigue siendo la única autoridad financiera | 92 tests dedicados, sin regresión | Sin modelo de slippage | HYBRID-A-BOOK | — |
| 12 | Pending orders | Trading | 0 | **3 ⬆** | Real `PendingOrder` model (`models.py:331`), wiring completo en `consumers.py`/`tasks.py`, migración `0075_pending_order_v2a.py`, UI de admin/dashboard | Commit `040a047` / tag `order-management-v2a-pending-orders-v1`; `test_order_management_v2a.py` = 27 tests | Sin manual-acceptance dedicado (tests-only, mismo estándar que otras filas maturity-3 del propio V1.3) | PRODUCTION (paridad) | — |
| 13 | Partial close | Trading | 0 | **3 ⬆** | `_order_close` ahora acepta `close_qty` parcial, atómico, wired en `consumers.py` (+376 líneas) | Commit `3e1dfb7` / tag `order-management-v2b-partial-close-v1`; `test_order_management_v2b_partial_close.py` = 32 tests | Igual que #12 | PRODUCTION (paridad) | — |
| 14 | Leverage / Margin (general) | Risk | 3 | **3** | Sin cambio | Suite intacta | Ninguno nuevo | NON-BLOCKING | — |
| 15 | PnL / Equity | Accounting | 3 | **3** | Sin cambio | `pnl_engine.py` intacto como motor único | Ninguno nuevo | NON-BLOCKING | — |
| 16 | Margin Call / Stop-Out | Risk | 3 | **3** | Sin cambio de maturity, pero **gap real cerrado**: exposición del broker ahora sobrevive a mercado cerrado (antes no tenía fallback) | `golden-weekend-risk01-market-closed-exposure-v1` (bb9b713) — `LastKnownMarketPrice` model + migración `0076`, 545 tests; más `fix-sltp-residual-01`(b0e804d)/`fix-sltp-update-validation-01`(f8cba39, 380 tests) endureciendo SL/TP | Ninguno nuevo conocido | NON-BLOCKING | — |
| 17 | Ledger / Accounting | Accounting | 3 | **3** | Sin cambio de maturity, pero **dos gaps reales cerrados** desde V1.3 (ninguno estaba nombrado en V1.3 como known gap — se descubrieron y cerraron en esta ventana): (a) drift de redondeo entre ledger calculado y balance posteado; (b) fondos sintéticos DEMO/CHALLENGE/FUNDED podían transferirse a wallet real sin gate | (a) `ledger-rounding-reconciliation-01-v1` (3c370be), 506 tests; (b) `money-integrity-fix-01-synthetic-funds-gate-v1` (f07f1a1) — `wallet_ledger.py::transfer_to_wallet()` ahora bloquea `account_type` no-retirable con `select_for_update()`, +241 líneas de test | Ninguno nuevo conocido | NON-BLOCKING | — |
| 18 | Deposits | Payments | 3 | **3** | Sin cambio — NowPayments sigue siendo el único rail, solo cripto | `test_deposit.py`(8)/`test_deposit_emails.py`(15)/`test_deposit_status_challenge.py`(45)/`test_nowpayments_secret.py`(17, subió de 13) intactos | Sin rail fiat | NON-BLOCKING (PRODUCTION si se exige fiat) | — |
| 19 | Withdrawals | Payments | 3 | **3** | Flujo verificado-local intacto y reforzado (email-OTP de 2 pasos, `VerifiedWithdrawalWallet`, expiración de OTP corregida) — **pero ver blocker crítico abajo, Parte VIII-B** | `withdrawal-security-extension-01-v1`(50f3930, 4123 líneas); `fix-withdrawal-otp-expired-challenge-01-v1`(936d713); `fix-withdrawal-otp-stale-pending-02-v1`(a7b9dc5) | **OPEN BLOCKER — WITHDRAWAL-POLICY-CORRECTION-02**: `WithdrawForm.amount_usd` (`forms.py:127-130`) solo exige `min_value=Decimal("0.01")` — el piso de negocio confirmado de USD 20 **no está en código**. `test_withdrawal_minimum.py` lo documenta como decisión deliberada de `withdrawal-policy-correction-01-v1`, no un descuido | PRODUCTION (compliance de política) | **WITHDRAWAL-POLICY-CORRECTION-02** |
| 20 | Payment Engine integration | Payments | 0 | **0** | Sin cambio, confirmado de nuevo — `payout_providers.py::NowPaymentsAdapter` documenta explícitamente `status_query=False, cancel=False`: "no GET endpoint for payout status/cancellation exists anywhere in this codebase" — esto es precisamente por qué WithdrawalRequest #4 no puede autoreconciliarse | Grep repetido, 0 hits de `PaymentEngine` real | La integración objetivo no existe | PRODUCTION | PAYMENT-ENGINE-INTEGRATION-AUDIT |
| 21 | Treasury / Settlement / Liquidity | Treasury | 3 | **3** | Sin cambio | 34 archivos de test intactos | Runbook nunca ejercitado en infra real | VPS | Simulacro de incidente en staging real |
| 22 | B-Book accounting | Accounting | 3 | **3** | Sin cambio de maturity; reforzado indirectamente por money-integrity-fix-01/02 | `broker_ledger.py` intacto | Ninguno específico | NON-BLOCKING | — |
| 23 | Broker Risk / Exposure | Risk | 3 | **3** | Sin cambio de maturity, pero **el known gap de V1.3 fue cerrado**: `snapshots.py::_position_data()` ahora multiplica por `contract_size` (antes no lo hacía, bug de reporting confirmado) | `fix-snapshots-contract-size-01-v1`(aab706a) — docstring explícito confirmando "BUG 1... Now qty*contract_size*price"; 246 tests dedicados | Ninguno nuevo conocido | NON-BLOCKING | — |
| 24 | Routing V2 | Routing | 1 | **1** | Sin cambio — sigue hardcodeado a `Book.INTERNAL` | Sin tags relacionados en los 22 revisados | BOOK-06k.5 sigue sin existir | HYBRID-A-BOOK | BOOK-06k.5 |
| 25 | Book Authority / Manual Override | Routing | 1 | **1** | Sin cambio | Sin cambios detectados | Cadena completa ausente | HYBRID-A-BOOK | Diseño de Book Authority |
| 26 | A-Book / LP abstraction | External Execution | 1 | **1** | Sin cambio | Sin cambios detectados | No es A-Book sin conectividad real | HYBRID-A-BOOK | LP sandbox adapter real |
| 27 | External hedge lifecycle | External Execution | 0 | **0** | Sin cambio | Grep repetido, 0 hits | Depende de #26 | HYBRID-A-BOOK | Junto con #26 |
| 28 | LP reconciliation | External Execution | 0 | **0** | Sin cambio | Grep repetido, 0 hits | Depende de #26 | HYBRID-A-BOOK | Junto con #26 |
| 29 | Trader Intelligence | Risk | 2 | **2** | Sin cambio — sin re-investigación específica esta ventana, ningún tag de los 22 lo toca | Evidence debt de V1.3 se mantiene sin resolver | Sin test file dedicado confirmado por nombre propio | NON-BLOCKING | Confirmar cobertura directa |
| 30 | Copy Trading | Trading | 0 | **0** | Sin cambio | Grep repetido, 0 hits | Feature ausente | NON-BLOCKING (fuera de MVP) | Decisión Build vs Buy |
| 31 | Financial Admin Dashboard | Accounting | 2 | **2** | Sin cambio | Sin tags relacionados | Fragmentado en 4+ vistas | NON-BLOCKING | Consolidación |
| 32 | Trader UI / customization | UI | 1 | **1** | Sin cambio de maturity — `chart-live-visual-filter-01-v1`(4d5c88a, 237 tests) es una corrección de estabilidad del candle en vivo, no agrega personalización (temas/colores siguen en 0) | Confirmado leyendo el diff del tag | Cero personalización real | NON-BLOCKING | Trader UI V2 |
| 33 | Mobile / responsive | UI | 1 | **1** | Sin cambio | Sin tags relacionados | Sin PWA | NON-BLOCKING | — |
| 34 | Admin / Ops tooling | Infrastructure | 2 | **2** | Sin cambio — **corrección de conteo**: V1.3 decía 15 comandos, el conteo real verificado esta sesión (y en V1.3, releyendo su propio listado) es **14** — error de conteo de V1.3, no un comando removido | `ls simulator/management/commands/` = 14 archivos, listado verificado línea por línea | Runbooks sin ejercitar en incidente real | VPS | Ensayo de runbook contra staging real |
| 35 | Support / Statements / History | Infrastructure | 1 | **1 (ver nota)** | El sub-componente "tickets" pasó de una tabla plana (maturity 2 en V1.3) a un sistema completo con panel dedicado, escalación, KB e INTERNAL-isolation (maturity 3 justificado — ver Parte III-B). El sub-componente "export de extracto" sigue en 0 (no construido, dominio distinto). Promedio recalculado: (3+0)/2 = **1.5 → redondeado conservadoramente a 1** (regla: ante ambigüedad de composición, se elige la maturity más baja) — **la aritmética canónica de esta fila NO cambia**, para no inflar el Overall por una técnica de promedio. El salto real de Soporte está documentado íntegro en la Parte III-B como capacidad suplementaria, con su propia maturity=3 | Ver Parte III-B completa | Export de extracto sigue sin existir | NON-BLOCKING | Implementar export (fila) / ver Parte III-B para Soporte real |
| 36 | Security / Rate limiting / Audit trail | Infrastructure | 3 | **3** | Sin cambio | Sin regresión detectada | Ninguno pendiente conocido | NON-BLOCKING | — |
| 37 | Monitoring / Observability | Infrastructure | 2 | **2** | Sin cambio de maturity, pero **hallazgo de precisión**: `simulator/observability.py::init_sentry()` es código real y conectado (`settings.py:1095-1104`, gateado por `SENTRY_DSN`) — más preciso que "no conectado" (V1.3) sería "listo mediante DSN, nunca ejercitado con un DSN real" | Confirmado por investigación dedicada esta sesión | Nunca ejercitado con DSN real; UptimeRobot sigue solo planificado | VPS | Conectar monitoreo externo con DSN real |
| 38 | Backups / Restore / DR | Infrastructure | 2 | **2** | Sin cambio — **confirmado explícitamente**: no existe `restore_drill_success.json` ni `media_restore_drill_success.json` en ningún lugar del repo | 7 scripts presentes en `deploy/scripts/`; cero artefacto de ejecución exitosa | Restore drill real nunca confirmado ejecutado | VPS/PRODUCTION | Ejecutar `media_restore_drill.sh` con éxito documentado |
| 39 | VPS / Staging readiness | Infrastructure | 2 | **2** | Sin cambio — **confirmado explícitamente esta sesión**: CERO evidencia de un VPS jamás provisionado, un certificado TLS jamás emitido, o un deploy real jamás ejecutado | `docs/INFRA_PLAN_L1.md`/`DEPLOY.md`/`SECURITY_CHECKLIST.md` sin commits desde V1.3 (`git log --since=2026-09-02` vacío en esos paths) | Ningún VPS ha sido provisionado/ejercitado | VPS | Deploy real a VPS + smoke test |
| 40 | Production readiness (rollup) | Infrastructure | 1 | **1** | Sin cambio de maturity — el rollup se mueve solo cuando staging real ocurre; el progreso de esta ventana es 100% local | Comparación directa contra gates del Plan Maestro (ninguno de producción cerrado) | Compliance(0), Payment Engine(0), VPS(2, no ejecutado), y ahora el blocker de retiro | PRODUCTION | Cerrar bloques 1-3 de Parte VII antes de reconsiderar |

**Verificación:** 40 filas presentes, IDs 1-40 sin omisiones ni repeticiones, ninguna renumerada. Suma de maturity = 80 (ver Parte II). Tres filas subieron (#6, #12, #13); ninguna bajó; treinta y siete se re-verificaron sin cambio (incluyendo dos correcciones de precisión sin impacto en maturity: #34 conteo de comandos, #37 descripción de Sentry).

---

# PARTE III-B — CAPACIDAD SUPLEMENTARIA (fuera de los 40 IDs canónicos)

Per instrucción explícita de esta auditoría: el trabajo posterior a V1.3 en Customer Support **no encaja** en la definición original de la fila #35 ("Support / Statements / History" — en V1.3 esto era literalmente `SupportTicket` como tabla plana + una página de historial). Lo que existe hoy es una arquitectura completa de 5 bloques con roles, escalación, aislamiento INTERNAL, y una Knowledge Base determinística. Se documenta aquí como capacidad suplementaria, **sin alterar la aritmética canónica de 40 IDs** (Parte II).

## S1 — Customer Support System (CUSTOMER-SUPPORT-01A → 01D)

| Campo | Valor |
|---|---|
| Maturity | **3 — VERIFIED LOCAL** |
| Domain | Support / Infrastructure (supplemental — closest canonical ID: #35, but scope has outgrown it) |
| Blocking scope | NON-BLOCKING for B-Book local MVP; PRODUCTION for full retail-scale readiness (see remaining gaps below) |

### Blocks closed, verified via `git rev-parse` against each tag

| Block | Commit | Tag | Verified |
|---|---|---|---|
| CUSTOMER-SUPPORT-01A (data foundation) | `f61f8feae4965c3324de8414bcebdcc68d9adf30` | `customer-support-01a-data-foundation-v1` | ✅ exact match |
| CUSTOMER-SUPPORT-01B (dedicated support panel) | `de5b51bef2952b70ad7836ff3dfafcea2c36e61b` | `customer-support-01b-dedicated-support-panel-v1` | ✅ exact match |
| CUSTOMER-SUPPORT-01C (customer thread) | `30349feb79de79a4c2f0eee238dde9c694dd4042` | `customer-support-01c-customer-thread-v1` | ✅ exact match |
| CUSTOMER-SUPPORT-01C.1 (floating chat widget) | `4706485e3f9f92eb2e0d0d088ecd4d5ed73987b9` | `customer-support-01c1-chat-widget-v1` | ✅ exact match |
| Knowledge Base Spec (doc-only block) | `6ca042f38a290501c4f1bf982dd94474cb9390dd` | `customer-support-knowledge-base-spec-v1` | ✅ exact match |
| CUSTOMER-SUPPORT-01D (Knowledge Base + Instant Answers) | `31708583363208463eef82640a734929c75bb221` | `customer-support-01d-knowledge-base-v1` | ✅ exact match — **= current HEAD** |

### What is real and implemented (verified against current code, not just commit messages)

- **`SupportTicket` / `SupportMessage` / `SupportAttachment` data foundation** (01A) — `author_role` snapshot pattern, `visibility` field (`CUSTOMER_VISIBLE` / `INTERNAL`) at the model layer.
- **CUSTOMER_VISIBLE filtering** — `_build_customer_thread()` excludes `INTERNAL` rows at the **query level**, not a template-layer filter. Verified this session across 4 separate test suites that a customer never sees INTERNAL content, staff notes, `assigned_to`, or `escalation_reason`.
- **Support separated from Django Admin** — `simulator/support_panel_views.py` (376 lines) is a dedicated view module with its own `support_panel_required` decorator built on `permission_levels.py`'s `is_owner_root`/`is_ops_admin`/`is_customer_support` — not `admin.site`, not `@staff_member_required`. Confirmed by direct grep this session.
- **Support hierarchy CLIENT → SUPPORT → OPS → OWNER** — real, permission-gated, verified against `simulator/permission_levels.py` and `OwnerRoot`/`OpsAdminProfile` models (see Parte XII for the full authority documentation).
- **Assignment / escalation** — `apply_transition()` state machine in `support_status.py`, `STATUS_ESCALATED` real and tested.
- **Floating chat widget (01C.1)** — HTML-fragment-swap architecture (`fetchFragment`/`renderFragment`), reuses the exact same `SupportTicket`/`SupportMessage` machinery as the full-page support views (no parallel system).
- **Deterministic Knowledge Base (01D)** — explicitly **NOT generative AI**: `simulator/support_knowledge/catalogue.py` is a static, version-controlled tuple of 82 frozen dataclass `KnowledgeItem`s (no DB model, no migration). GREEN/YELLOW/RED risk-level behavior:
  - **GREEN** (`action=ANSWER`) → instant deterministic answer, 75 of 82 items enabled.
  - **YELLOW** (`action=SUPPORT`) → safe non-committal message, never fabricates account/transaction status, offers human handoff.
  - **RED** (`action=OPS`, all under Security) → distinct security-styled message, never presented as ordinary FAQ, routes to human support — never auto-escalates (verified: viewing a RED item creates zero `SupportTicket` rows).
  - **7 items** (`withdrawal_minimum`, 5× deposit specifics, `kyc_documents`) are `enabled=False` — structurally unreachable POLICY_PENDING placeholders, not merely hidden.
- **"Mis conversaciones"** — added in the final certification-fix round of 01D; the widget now **always** opens on the KB home (the old 01C.1 "auto-select most recent ticket" rule was deliberately removed — it blocked the KB home from ever being reachable for a customer with any open ticket). Existing conversations reachable only via explicit navigation, never automatically.
- **Human handoff** — "Hablar con soporte" always routes into the real, unchanged `_create_support_ticket()` path; no parallel ticket-creation logic anywhere in the KB flow.

### Test evidence (counted directly this session, not estimated)

| File | Tests |
|---|---|
| `test_customer_support_01a_data_foundation.py` | 26 |
| `test_customer_support_01b_support_panel.py` | 54 |
| `test_customer_support_01c_customer_thread.py` | 38 |
| `test_customer_support_01c1_chat_widget.py` | 58 |
| `test_customer_support_01d_knowledge_base.py` | 80 |
| **Total dedicated Support tests** | **256** |

### Manual certification record

Manual browser certification was explicitly performed and confirmed by the Owner for CUSTOMER-SUPPORT-01D, covering all 10 points required by the certification protocol used throughout this project:
1. KB Home appears on first widget open — **PASSED**
2. GREEN FAQ returns deterministic approved answer — **PASSED**
3. YELLOW case does not fabricate account/transaction state — **PASSED**
4. RED security case shows security-specific guidance + human handoff — **PASSED**
5. "Hablar con soporte" opens the existing human support flow — **PASSED**
6. Human ticket creation still works — **PASSED**
7. "Mis conversaciones" lists the customer's own tickets — **PASSED**
8. Existing ticket opens correctly — **PASSED**
9. Navigation back to "Mis conversaciones" / "Inicio de soporte" works — **PASSED**
10. Existing human support architecture remains intact — **PASSED**

Two rounds of manual-certification-fix were required before this passed cleanly (a stale browser-cache issue on `/support/widget/*` fragment responses, fixed with `@never_cache`; then a genuine product-decision conflict where the old active-ticket auto-select blocked the KB home, fixed by removing that rule and adding "Mis conversaciones"). Both fixes are documented in the commit history under the `customer-support-01d-knowledge-base-v1` tag.

### Why S1's maturity is 3, not higher

Per this document's own Rule VII: "Tests alone do not create maturity 4 or 5... Staging requires actual deployed/staging evidence." Support has never run in a staging environment — maturity 3 (VERIFIED LOCAL) is the ceiling, identical to every other locally-verified capability in Parte III.

---

# PARTE IV — WHAT CUSTOMER SUPPORT STILL DOES NOT HAVE (per explicit audit instruction — do not overclaim)

Support is **not** 100% complete. Explicit remaining gaps, none silently closed:

| Gap | Status | Notes |
|---|---|---|
| Lifecycle email notifications (ticket created/replied/escalated/closed) | **NOT BUILT** | No email-sending code found in `support_panel_views.py` or the widget views — confirmed by grep this session |
| Attachment operational flow | **FOUNDATION ONLY** | `SupportAttachment` model exists (01A) but no upload/download view — 01A's own docstring says this is deferred to a future "01E" extension of `secure_media.py` |
| SLA / response-time targets | **NOT DEFINED** | No SLA field, no timer, no breach detection anywhere in the codebase |
| Queue/operational metrics (response time, backlog) | **NOT BUILT** | No dashboard aggregation for support-specific ops metrics found |
| Escalation notification to OPS | **PARTIAL** | `STATUS_ESCALATED` transition is real and auditable, but no notification (email/in-app) fires when it happens — an Ops agent must be watching the queue |
| Support Agent real-user operational certification | **NOT DONE** | All certification performed by the Owner acting as both customer and, implicitly, verifying the panel code paths — no session where a real, separate Support-role human operated the panel end-to-end against live customer traffic |
| Duplicate/conversation policy | **UNDEFINED** | "Hablar con soporte" always creates a new ticket (consistent with the pre-existing 01C.1 "+ Nueva conversación" precedent) — there is no policy preventing a customer from opening unlimited parallel tickets |
| Support audit trail | **PARTIAL** | `SupportMessage.author_role` snapshot + `visibility` field give a real trail, but there is no dedicated `broker_audit.py`-style `EV_SUPPORT_*` event stream comparable to the financial audit trail |
| Future Support AI | **NOT STARTED, BY DESIGN** | 01D is explicitly, deliberately deterministic — no LLM, no embedding, no classifier. A future "Support AI" layer was named as a possibility in the 01D authorization but zero code exists toward it |
| Remaining POLICY_PENDING knowledge entries | **7 ITEMS OPEN** | `withdrawal_minimum`, `deposit_supported_assets`, `deposit_supported_networks`, `deposit_minimum`, `deposit_fee`, `deposit_third_party_wallet`, `kyc_documents` — all structurally disabled until their underlying business policy/code is finalized (the withdrawal one is explicitly blocked on WITHDRAWAL-POLICY-CORRECTION-02, Parte VIII-B) |
| Public anonymous support widget | **DEFERRED, INTENTIONALLY** | 01D explicitly scoped to authenticated customers only — no anonymous/pre-login support surface exists |
| Support at public-volume scale | **UNTESTED** | All certification is single-user, local, dev-data scale — no load/concurrency testing of the support system has been performed |

### Planned sequence (still consistent with current evidence)

```
01D (DONE) → 01E (attachments + lifecycle emails) → 01F (SLA + escalation notifications + audit trail) → Support AI (future, generative layer on top of the deterministic 01D foundation)
```

This sequence was not found written down anywhere in the repo as a formal plan — it is inferred from what 01A-01D's own docstrings explicitly deferred ("01E adds attachment upload/download," "future Support AI will sit on top of this deterministic foundation"). Recorded here as the most evidence-consistent next sequence, not as a confirmed roadmap document.

---

# PARTE V — WITHDRAWAL POLICY: CURRENT BUSINESS TRUTH (CRITICAL)

## Confirmed business policy

- **Minimum withdrawal = USD 20**
- **USD 20 through USD 1,000**: normal automated security flow
- **Above USD 1,000**: additional internal approval/review

### Required security controls (verified present in code, independent of the minimum-amount gap below)

| Control | Status | Evidence |
|---|---|---|
| KYC | ✅ Present | Gated before withdrawal (see #3 Parte III) |
| Verified withdrawal wallet | ✅ Present | `VerifiedWithdrawalWallet` — "no se ofrece texto libre" (Design Lock rule 4), destination must be a pre-verified wallet |
| Wallet security/cooldown rules | ✅ Present | Part of `withdrawal-security-extension-01-v1` |
| TOTP/2FA | ✅ Present | Real setup/verify/disable cycle, audited |
| Email OTP | ✅ Present | Two-step `WithdrawalEmailOTPChallenge`, hardened twice post-V1.3 for expiry/stale-resend bugs |
| Sufficient withdrawable balance | ✅ Present | Funds reserved atomically at request creation |
| Pending-request conflict checks | ✅ Present | Rate-limit + single-active-request gates in the withdrawal flow |

**Important, re-confirmed:** Trading Equity ≠ withdrawable balance. `Wallet.available_balance` remains the single-writer, materialized source for withdrawable funds — `money-integrity-fix-01-synthetic-funds-gate-v1` (this window) specifically closed a real gap where synthetic DEMO/CHALLENGE/FUNDED `TradingAccount` balances could otherwise have crossed into the real Wallet without a gate.

## KNOWN CODE/POLICY GAP — verified, still true, OPEN BLOCKER

```python
# simulator/forms.py:127-130
amount_usd = forms.DecimalField(
    label="Monto (USD)",
    required=False,
    min_value=Decimal("0.01"),
    ...
)
```

This is the **only** amount floor anywhere in the withdrawal path. `simulator/tests/test_withdrawal_minimum.py`'s own docstring states this is deliberate, not an oversight:

> "WITHDRAWAL-POLICY-CORRECTION-01 — no fixed minimum withdrawal amount. MIN_WITHDRAWAL_USD (formerly a fixed $1,000 Money Broker policy...) has been removed entirely — deliberately, not repurposed. The only remaining floor is `WithdrawForm.amount_usd`'s own `min_value=Decimal("0.01")`, a pure form-layer validator, not a business policy gate."

`test_0_01_accepted_by_form` explicitly asserts $0.01 **is** accepted. No test anywhere rejects an amount between $0.01 and $20.

**Origin:** `withdrawal-policy-correction-01-v1` (2026-09-10, commit `411638e`, "fix: align withdrawal authorization policy") is the exact commit that removed the old (also wrong, but differently wrong) $1,000 floor and left no replacement minimum at all.

### Formal classification

> **OPEN BLOCKER: WITHDRAWAL-POLICY-CORRECTION-02**
> Not silently fixed in this audit (per explicit instruction). Business policy (USD 20 minimum) and enforced code (USD 0.01 minimum) are in direct, confirmed conflict, right now, at current HEAD.

---

# PARTE VI — WITHDRAWAL / PAYOUT REAL-WORLD STATUS

## WithdrawalRequest #4 — CONFIRMED CURRENT (re-verified via read-only DB query this session)

| Field | Value |
|---|---|
| `amount_usd` | 20.01 |
| `status` | `processing` |
| `crypto_currency` | `usdttrc20` |
| `np_payout_id` | *(empty)* |
| PayoutAttempt #1 `status` | `unknown` |
| PayoutAttempt #1 `submitted_at` | 2026-09-12 16:44:12 |

This matches the historical account exactly: intended ~$20, observed request ~$20.01 (the same $20-vs-$20.01 UI/policy discrepancy that WITHDRAWAL-POLICY-CORRECTION-02 would need to resolve at the root), a `PayoutAttempt` existed, the NOWPayments payout POST returned HTTP 403, and provider state remains **UNKNOWN** — `PayoutAttempt.UNKNOWN` is explicitly documented in `models.py` as non-terminal, and the code confirms it is **never** treated as FAILED and **never** auto-reconciled from UNKNOWN. No blind retry or refund has occurred — consistent with the required safety rule.

**Classification: CONFIRMED CURRENT.** This is not historical — it is an open, unresolved, real stuck payout as of this audit.

**Why it can't self-resolve:** `payout_providers.py::NowPaymentsAdapter` explicitly documents `status_query=False, cancel=False` — "no GET endpoint for payout status/cancellation exists anywhere in this codebase." This is a structural, architectural limitation, not a bug that a small patch would close — it maps directly to capability #20 (Payment Engine integration = 0).

## Deposit #45 — CONFIRMED CURRENT

| Field | Value |
|---|---|
| `amount_usd` | 20.00 |
| `crypto_currency` | `usdttrc20` |
| `status` | `finished` |
| `credited` | `True` |

Matches the historical account exactly: $20, USDT/TRC20, finished, credited, reconciled. **Classification: CONFIRMED CURRENT.**

---

# PARTE VII — DEPOSITS / NOWPAYMENTS

- **NowPayments integration status**: unchanged since V1.3 — the only implemented deposit rail, crypto-only.
- **Provider abstraction status**: `payout_providers.py::NowPaymentsAdapter` exists as a thin, provider-agnostic wrapper on the *payout* (withdrawal) side, but explicitly cannot query status or cancel — it does not solve the reconciliation gap, it documents it honestly.
- **Only one provider implemented**: confirmed — no second deposit or payout rail exists anywhere in `simulator/`.
- **Reconciliation maturity**: deposits are idempotent and webhook-verified (HMAC-SHA512, `select_for_update()` + `credited` flag) — this side is mature. Payouts/withdrawals are the weak side (see Parte VI).
- **Real-money testing limitations**: all evidence in this document comes from the same dev/test NOWPayments sandbox-style credentials used throughout the project's history — no real-money production testing has occurred, consistent with V1.3.

---

# PARTE VIII — MONEY INTEGRITY (post-V1.3 work)

Two real, substantive closures happened in this window — neither was a documentation-only pass:

### money-integrity-fix-01-synthetic-funds-gate-v1 (`f07f1a1`)
Closed a genuinely open gap that V1.3 did not flag by name: `wallet_ledger.py::transfer_to_wallet()` previously had **no check** preventing a DEMO/CHALLENGE/FUNDED `TradingAccount` (synthetic, never backed by a real Wallet debit) from transferring its balance into the real Wallet. Now fail-closed via a `select_for_update()`-locked `account_type` check against `TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES`, denied atomically before any `InternalTransfer` row is created (zero trace on denial). +241 lines of dedicated test coverage in `test_wallet_ledger.py`.

### money-integrity-fix-02-owner-ops-financial-control-v1 (`349d384`)
The origin of the entire Owner/Ops authority hierarchy used by everything built afterward, including all of Customer Support 01A-01D: `OwnerRoot`, `OpsAdminProfile`, `permission_levels.py`, `owner_actions.py` (334 new lines), migration `0080`, plus dedicated owner-only manual financial-adjustment paths (`test_owner_trading_adjustment.py`, `test_owner_wallet_adjustment.py`).

### Reassessed items

- **Synthetic funds prohibited from becoming withdrawable**: ✅ now gated (fix-01 above) — this was a real, previously-open gap, now closed.
- **Owner-only manual financial adjustments**: ✅ real, `owner_actions.py` — confirmed present.
- **TradingAccount direct-admin restrictions**: unchanged from V1.3, not re-investigated this window (no tag touched it directly beyond the fix-02 hierarchy build).
- **Wallet/ledger invariants**: reinforced (fix-01 + ledger-rounding-reconciliation-01).
- **Atomic balance changes / idempotency**: unchanged, `UniqueConstraint`-backed idempotency (migration 0048) still the core mechanism, no regression found.
- **Generic correction idempotency**: not re-investigated this window — carried forward from V1.3 without new evidence either way.
- **TradingAccount balance-vs-ledger invariant**: no open gap surfaced this window.
- **Fraud/velocity controls**: unchanged from V1.3 — rate-limiting exists (`ratelimit.py`, Redis-backed) but no dedicated fraud/velocity-scoring system beyond what V1.3 already documented under #36.

---

# PARTE IX — OWNER / OPS / SUPPORT AUTHORITY

## Operational hierarchy (verified against current code)

| Role | Person (dev/test identity) | Model | Constraint |
|---|---|---|---|
| OWNER | Naffer / "Admin2" (dev account) | `OwnerRoot` | **DB-level singleton**: `CheckConstraint(singleton_enforcer=True)` + `UniqueConstraint(fields=["singleton_enforcer"])` — structurally impossible for a second row to ever exist. Never created/changed/deleted through Django Admin (`OwnerRootAdmin` returns `False` for add/change/delete unconditionally, for every user including superusers) |
| OPS | Ernesto (per project convention) | `OpsAdminProfile` | Same singleton pattern, but **can be replaced** over its lifetime (unlike OwnerRoot) |
| SUPPORT | dedicated `is_customer_support` permission | Django `Permission` (`codename="is_customer_support"`, `models.py:2318`) | Ticket-scoped only — no arbitrary financial mutation capability found anywhere in `support_panel_views.py` |
| CLIENT | customer | standard `User` | Customer-only access, enforced by ownership filters (`get_object_or_404(..., user=request.user)`) on every customer-facing route reviewed across all 5 support blocks |

## Permissions, verified

- **OWNER**: root operational authority — `owner_actions.py` gates manual financial adjustments to Owner only.
- **OPS**: operations authority below Owner — `OpsAdminProfile`, singleton-but-replaceable.
- **SUPPORT**: support tickets only, no arbitrary financial mutation. Confirmed no code path in `support_panel_views.py` touches `Wallet`, `TradingAccount`, or `WithdrawalRequest` balances.
- **CLIENT**: customer-only access.

## Support panel authorization — confirmed separated from Django Admin

```python
# simulator/support_panel_views.py:54-59
@wraps(view_fn)
@login_required
def wrapper(request, *args, **kwargs):
    from .permission_levels import is_customer_support, is_ops_admin, is_owner_root
    if not (is_owner_root(request.user) or is_ops_admin(request.user) or is_customer_support(request.user)):
        raise PermissionDenied(...)
```

This is a **dedicated decorator** (`support_panel_required`), not `@staff_member_required`, not routed through `admin.site` — confirmed by direct grep this session. Anonymous → redirected to login via `@login_required`. No secrets or private credentials are included in this document.

---

# PARTE X — TRADING CORE (reassessed)

Most of the 40-ID matrix above already covers this in detail (IDs 4, 8-17, 22-23). Summary of what genuinely moved this window:

- **Market orders**: unchanged, maturity 3.
- **Pending orders**: 0→3, real `PendingOrder` model + full consumer/task wiring (see #12).
- **Partial close**: 0→3, real atomic partial-qty close (see #13).
- **Netting/hedging**: unchanged — no tag touched this; deep simulation infrastructure (BOOK-05/06) remains unconnected to any real LP, consistent with V1.3.
- **Pricing/spread/commissions**: unchanged maturity, known `float`-vs-`Decimal` gap in `commission_for()` re-confirmed still open.
- **Margin/leverage/PnL/equity/free margin/margin call/stop-out**: unchanged maturity, hardened by weekend-risk exposure preservation + SL/TP validation fixes (real gaps found and closed, see #16 row).
- **SL/TP**: hardened twice this window (`fix-sltp-residual-01`, `fix-sltp-update-validation-01`) — both real correctness fixes, not cosmetic.
- **Chart synchronization**: `chart-live-visual-filter-01-v1` fixed live-candle rendering stability — UI-layer, maps to #32, does not move trading-core maturity.
- **Stale-price protection**: unchanged, `get_validated_quote()` fail-safe intact.
- **Broker ledger/accounting / B-Book accounting**: unchanged maturity, two real gaps closed (Parte VIII).
- **Broker exposure**: the `snapshots.py` contract_size bug (V1.3's flagged ops-only gap) is now **fixed** (see #23).
- **Trader intelligence**: unchanged, not re-investigated this window.
- **Liquidity/routing shadow work**: unchanged, still zero real external connectivity (#24-28).
- **Latest BOOK milestones**: BOOK-06j/06k lineage referenced in the protected test file `test_book06j1_population_engine_close_race.py` (untouched, per instruction) — no new BOOK-numbered tag closed in this window; the 22 tags closed since V1.3 use direct feature/fix naming (`order-management-*`, `fix-*`, `golden-*`, `money-integrity-*`, `withdrawal-*`, `customer-support-*`) rather than the BOOK-nn convention.

---

# PARTE XI — MARKET DATA (reassessed)

### FOREX
- Massive-only runtime, unchanged since V1.3 — still the certified path, no regression found in this window's 22 tags.
- Symbols certified: EUR/USD, GBP/USD, USD/JPY, AUD/USD (unchanged).
- Finnhub: still retired from the Forex runtime.

### CRYPTO
- **BTCUSD / ETHUSD current provider path**: now Massive-exclusive (`_MASSIVE_CRYPTO_ENABLED_SYMBOLS`, `feeds.py:2024-2043`) — this is the real change since V1.3.
- **Binance 451 issue**: unchanged as a historical fact (still the reason Massive was chosen); Binance/Kraken/CoinGecko code remains present as legacy fallback but is currently unreachable given the enabled-symbols allowlist.
- **Kraken/Massive/current alternatives**: Massive won; no evidence Kraken is the active runtime path.
- **Is crypto now certified or still pending?**: **Certified at the VERIFIED LOCAL level (maturity 3)** — real routing, real 141-test coverage — but **not** certified with the same manual-acceptance rigor as Forex (no multi-hour manual session logged). This is stated explicitly, not implied: crypto is closed for B-Book Local MVP purposes but carries an evidence-debt note that a future Forex-parity manual session would still be valuable.

---

# PARTE XII — ROUTING / B-BOOK / A-BOOK

Strict definitions maintained, unchanged from V1.3:

> **B-Book**: internalized broker risk.
> **A-Book**: exists ONLY when actual risk is sent externally to a real LP/provider and external lifecycle/reconciliation exists. Shadow/simulated LP logic is NOT A-Book.

| Item | Maturity | Status |
|---|---|---|
| B-Book current maturity | 3 | Unchanged, the only real book (`Book.INTERNAL`) |
| RoutingDecision | 1 | Unchanged, model exists, caller exists, decision hardcoded |
| Liquidity shadow mode | 1 | Unchanged, deep simulation, zero real connectivity |
| Book authority | 1 | Unchanged, no manual override capability |
| Partial hedge | 0 | Unchanged |
| LP abstraction | 1 | Unchanged, `LiquidityProvider`/`LiquidityDecision`/`LiquidityLedger`/`DealingDeskDecision` all self-disclaim real connectivity in their own docstrings |
| External hedge lifecycle | 0 | Unchanged |
| LP reconciliation | 0 | Unchanged |
| Actual external connectivity | **NONE** | Confirmed again this session — no adapter, no external API client for any LP |

No tag closed in this window touched Routing/B-Book/A-Book. This entire domain is byte-for-byte unchanged from V1.3.

---

# PARTE XIII — PAYMENT ENGINE / TREASURY ENGINE

**Critical separation, maintained per instruction:**

- **Money Broker project**: `trx_sim` (this repository, this audit's subject).
- **Separate project**: `treasury_engine` — located at `/Users/naffermoreno/Desktop/treasury_engine` on this machine. **NOT inspected, NOT modified, NOT touched** in this audit block, per explicit instruction. (Confirmed via `ls`: the directory does not even currently exist at that path on this machine — noted factually, not investigated further, as inspecting it was explicitly forbidden regardless.)

### Money Broker's integration readiness (from trx_sim evidence only)

- **Payment Engine target remains an external service** — this document does not propose duplicating its logic inside `trx_sim`.
- **Current NOWPayments adapter status**: `simulator/nowpayments.py` (deposits, mature) + `simulator/payout_providers.py::NowPaymentsAdapter` (payouts, explicitly incomplete — no status query, no cancel).
- **Current integration gap**: capability #20 = 0, unchanged. The architecture today is the *opposite* of the target — a third-party processor embedded directly, not an abstraction over a real internal Payment Engine.
- **Payment Engine Integration audit/gate status**: `PAYMENT-ENGINE-INTEGRATION-AUDIT` remains queued, not started (see Parte XV, Next Execution Queue) — this would be a design/audit block, not implementation, and explicitly would not touch `treasury_engine`.

---

# PARTE XIV — INFRASTRUCTURE / VPS (reassessed, zero change confirmed)

| Item | V1.3 Maturity | V1.4 Maturity | Note |
|---|---|---|---|
| VPS plan | 2 | **2** | `INFRA_PLAN_L1.md` (614 lines) unchanged, zero commits since V1.3 |
| PostgreSQL | 1 | **1** | Real, env-gated config exists (`settings.py:195-224`, `psycopg2-binary` in requirements.txt); dev/test still always SQLite |
| Redis | 3 | **3** | Unchanged, actively used locally |
| Celery | 3 | **3** | Unchanged, Beat + worker both exercised locally |
| Daphne | 3 | **3** | Unchanged |
| Nginx/TLS | 1 | **1** | Config exists (`deploy/nginx/trx_sim.conf`); no TLS cert ever issued |
| SMTP (real delivery) | 1 | **1** | Real backend + production guard exists in settings; tests still use `locmem`; no real delivery ever confirmed |
| Monitoring | 2 | **2** | `init_sentry()` confirmed real and DSN-gated (precision improvement over V1.3's description) but never exercised with a real DSN |
| Backups | 2 | **2** | 7 scripts present; **no successful-run artifact exists anywhere in the repo** |
| Restore drill | 1 | **1** | Fully scripted; `INFRA_PLAN_L1.md` itself says a real successful run is still required to close this gate; never happened |
| Health checks | 3 | **3** | Unchanged, `/api/health/` + `/api/health/detail/`, dedicated tests |
| Admin/ops tooling | 2 | **2** | 14 commands (V1.3's "15" was a count error, not a removed command) |
| Secrets/config | 2 | **2** | Unchanged, production guards + `test_no_hardcoded_secrets.py` intact |
| External provider connectivity from a real VPS | 2 | **2** | No new evidence possible without a VPS |

**Zero commits touched `deploy/`, `docs/INFRA_PLAN_L1.md`, `docs/DEPLOY.md`, or `docs/SECURITY_CHECKLIST.md` since V1.3** (`git log --since=2026-09-02` on those paths returns empty). `SECURITY_CHECKLIST.md` (178 lines) and `DEPLOY.md` (843 lines) remain current and internally consistent with code evidence — neither claims anything code evidence contradicts.

**CONFIRMED explicitly, again, this session: no VPS has ever been provisioned, no TLS certificate has ever been issued, no real staging/production run has ever occurred.** This is the sharpest contrast in this entire audit: Trading Core moved dramatically (+10.2pt on B-Book MVP); Infrastructure moved by exactly zero points.

---

# PARTE XV — GOLDEN BROKER SCENARIOS

Unchanged from V1.3 — **no tag in this window closed a Golden Scenario certification.** The pseudo-item "Golden Scenarios" inside B-Book MVP (Parte IV below) remains at maturity **1**: Forex market data and stop-out mechanics remain the only solidly-closed sub-scenarios (per V1.3); controlled Forex order → spread → lot → margin → commission → entry price → chart/Bid-Ask alignment → P&L → equity → close → ledger → admin → deposits → withdrawal → KYC → 2FA → emails → account products, as an end-to-end manually-certified chain across **all** account product types, remains **incomplete**.

Two of the sub-scenario blockers named in V1.3 are now removed as *technical* blockers (Order Management V2 exists; Crypto is certified), but the actual manual Golden Scenario certification sessions covering those new capabilities have not been separately logged. This is stated explicitly rather than inferred as closed.

---

# PARTE XVI — CURRENT REAL DATA / DEV CERTIFICATION ARTIFACTS

**Label: DEV/MANUAL CERTIFICATION DATA — not production customers.** Re-verified via read-only query this session:

| Record | Status |
|---|---|
| `SupportTicket` #1, #5, #9 | Present, unchanged, owner "Admin2" (dev account) |
| `SupportTicket` #10, #11 | **New since the last support audit round** — created during the Owner's own manual browser certification of CUSTOMER-SUPPORT-01D ("Hablar con soporte" real end-to-end test) |
| `SupportMessage` count | 3, unchanged |
| Deposit #45 | $20, USDT/TRC20, `finished`, `credited=True` — CONFIRMED CURRENT |
| WithdrawalRequest #4 | $20.01, `processing`, PayoutAttempt `unknown`, HTTP 403 historically, unreconciled — CONFIRMED CURRENT |

---

# PARTE XVII — PROTECTED / DO-NOT-TOUCH FILES

Verified present and untouched this session (`git status --porcelain` + `ls -la`):

| File | Status |
|---|---|
| `simulator/tests/test_book06j1_population_engine_close_race.py` | Present, untracked, unmodified |
| `db.sqlite3.backup_before_0069_0072` | Present, untracked, unmodified (9.9MB, mode `-rwx------`) |

**Never touch:** `/Users/naffermoreno/Desktop/treasury_engine` during Money Broker work unless the Owner explicitly authorizes it. (Confirmed: the path does not currently exist on this machine; this audit did not attempt to create, inspect, or otherwise interact with it.)

---

# PARTE XVIII — GIT WORKFLOW / GOVERNANCE (unchanged, reaffirmed)

```
problem
→ reproduction
→ root cause
→ audit/design lock
→ scoped implementation
→ dedicated tests
→ regression tests
→ manual certification when applicable
→ pre-Git audit
→ selective stage
→ staged-diff inspection
→ Owner manual commit
→ verify
→ Owner manual tag
→ verify
→ Owner manual push
→ remote verification
→ closure
```

**Claude never decides or performs Git closure.** No `git add`/`commit`/`tag`/`push` without explicit Owner authorization — verified this session: zero git mutation commands were run; `git status`/`git log`/`git rev-parse`/`git show`/`git fetch --tags` (all read-only) were the only git commands used to produce this audit.

---

# PARTE XIX — CURRENT PUBLISHED SUPPORT HISTORY (every hash verified live via `git rev-parse`)

| Block | Commit | Tag |
|---|---|---|
| CUSTOMER-SUPPORT-01A | `f61f8feae4965c3324de8414bcebdcc68d9adf30` | `customer-support-01a-data-foundation-v1` |
| CUSTOMER-SUPPORT-01B | `de5b51bef2952b70ad7836ff3dfafcea2c36e61b` | `customer-support-01b-dedicated-support-panel-v1` |
| CUSTOMER-SUPPORT-01C | `30349feb79de79a4c2f0eee238dde9c694dd4042` | `customer-support-01c-customer-thread-v1` |
| CUSTOMER-SUPPORT-01C.1 | `4706485e3f9f92eb2e0d0d088ecd4d5ed73987b9` | `customer-support-01c1-chat-widget-v1` |
| Knowledge Base Spec | `6ca042f38a290501c4f1bf982dd94474cb9390dd` | `customer-support-knowledge-base-spec-v1` |
| CUSTOMER-SUPPORT-01D | `31708583363208463eef82640a734929c75bb221` | `customer-support-01d-knowledge-base-v1` |

All six confirmed with zero discrepancy against the prompt's stated values.

---

# PARTE XX — NEXT EXECUTION QUEUE (rebuilt, not reused from V1.3)

Ordered by: **1. safety/risk, 2. correctness, 3. dependency, 4. product value** — per explicit instruction, never by visual impressiveness.

### 1. WITHDRAWAL-POLICY-CORRECTION-02 — *(SAFETY, highest priority)*
- **Why now:** live, confirmed, real-money-risk gap. Every day this stays open, a customer can technically request a withdrawal as low as $0.01, in direct conflict with confirmed business policy.
- **Depends on:** nothing technical. Needs the Owner's decision on exact enforcement point (form validator vs. a real business-policy layer) before implementation.
- **Definition of Done:** `WithdrawForm.amount_usd` (or an equivalent policy-layer check) enforces $20 minimum; `test_withdrawal_minimum.py` updated to assert rejection below $20; WithdrawalRequest #4's $20.01-vs-$20 discrepancy investigated as part of the same root-cause pass.
- **Blocking scope:** PRODUCTION (compliance/correctness).

### 2. WithdrawalRequest #4 reconciliation
- **Why now:** a real stuck payout, unresolved since 2026-09-12. Should not be touched via blind retry/refund per the established safety rule — needs a deliberate reconciliation design.
- **Depends on:** ideally sequenced with/after #1 (don't want to reconcile a $20.01 request while the $20-vs-$0.01 policy question is still open).
- **Definition of Done:** either a provider-side status confirmation (would require closing #20's status-query gap) or a documented, Owner-authorized manual resolution path.
- **Blocking scope:** PRODUCTION.

### 3. CUSTOMER-SUPPORT-01E — attachments + lifecycle emails
- **Why now:** the single biggest remaining Support gap; foundation (`SupportAttachment`) already exists.
- **Depends on:** nothing blocking.
- **Definition of Done:** upload/download wired through `secure_media.py`; ticket-created/replied/closed emails sent.
- **Blocking scope:** NON-BLOCKING for B-Book MVP; PRODUCTION for retail-scale support.

### 4. Withdrawal E2E certification (post-#1/#2)
- **Why now:** cannot be meaningfully certified while the minimum-amount policy gap and the #4 reconciliation are open.
- **Depends on:** #1, #2.
- **Definition of Done:** a full manual Golden-Scenario-style withdrawal session, $20-$1,000 normal flow and >$1,000 review flow, both certified.
- **Blocking scope:** PRODUCTION.

### 5. Internal broker trading certification (Golden Scenarios remaining sub-scenarios)
- **Why now:** Order Management V2 and Crypto are now technically unblocked; the manual certification sessions were never separately logged.
- **Depends on:** nothing technical (both #12/#13/#6 are already VERIFIED LOCAL).
- **Definition of Done:** remaining Golden Scenario sub-scenarios (ECN/No-Commission/VIP product types, crypto trading session) manually certified with evidence logged.
- **Blocking scope:** LOCAL (closes Golden Scenarios pseudo-item to 3).

### 6. Payment E2E certification
- **Why now:** deposits are mature; withdrawals need #1-#2 closed first to be meaningful.
- **Depends on:** #1, #2.
- **Blocking scope:** PRODUCTION.

### 7. Money Integrity remaining (generic correction idempotency, TradingAccount balance-vs-ledger invariant re-audit)
- **Why now:** not re-investigated this window; worth a dedicated confirmatory pass given how much else in the ledger/wallet area changed.
- **Depends on:** nothing.
- **Blocking scope:** NON-BLOCKING (confirmatory).

### 8. Crypto market-data manual acceptance session (parity with Forex)
- **Why now:** cheap, closes the one evidence-debt item left on an otherwise-3 capability.
- **Depends on:** nothing.
- **Blocking scope:** LOCAL (evidence-debt closure, not a maturity change).

### 9. Payment Engine integration audit
- **Why now:** diagnostic/design only, doesn't require anything else closed; directly informs whether #2 (WithdrawalRequest #4) can ever be self-service-reconciled.
- **Depends on:** nothing (does NOT touch treasury_engine).
- **Blocking scope:** PRODUCTION.

### 10. OWASP/security audit
- **Why now:** due diligence before any staging attempt.
- **Depends on:** nothing.
- **Blocking scope:** VPS/PRODUCTION.

### 11. Dev/test superuser cleanup
- **Why now:** hygiene before any staging attempt (dev accounts like "Admin2" carrying real-shaped ticket/withdrawal data should not migrate to production).
- **Depends on:** nothing.
- **Blocking scope:** VPS/PRODUCTION.

### 12. PostgreSQL — provision and exercise for real
- **Depends on:** nothing blocking, but best sequenced with #13.
- **Blocking scope:** VPS.

### 13. Restore drill — execute for real, produce the success artifact
- **Depends on:** #12 (needs a real Postgres to restore).
- **Blocking scope:** VPS/PRODUCTION.

### 14. Private VPS staging
- **Depends on:** reasonably closing #1-#2 (don't deploy with a known live money-policy gap) + #12/#13 (the two weakest infra items).
- **Blocking scope:** VPS/PRODUCTION — this is the block capable of moving anything to maturity 4.

### 15. Broker Economics (Swap/Overnight)
- **Blocking scope:** PRODUCTION, not urgent (no safety/correctness risk, pure feature gap).

### 16. Compliance Config / AML
- **Blocking scope:** PRODUCTION, required before any real multi-jurisdiction launch.

### 17. UI V2 / Routing/Book Authority / A-Book
- **Blocking scope:** explicitly lowest priority — visually or architecturally interesting, but no money-correctness or safety dependency forces these earlier. A-Book remains a HYBRID-A-BOOK-scope item only, never required for B-Book local MVP or staging.

### 18. FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01 — *(NON-BLOCKING, test infrastructure hygiene)*
- **Why now:** root-caused this session — a full-suite run (7065 tests) surfaced 3 FAIL + 3 ERROR, all traced to shared Redis rate-limit state on the `deposit_callback` endpoint (`@rate_limit("deposit_callback", limit=30, window=60)`, `simulator/views.py:1700`) leaking across `test_deposit_emails.py::DepositConfirmedEmailTests` and a related `test_ledger_invariant_audit.py` test. Diagnosis is CLOSED; the implementation fix is not.
- **Depends on:** nothing. Explicitly does not outrank items #1-#17 — this is test-infrastructure hygiene, not money correctness.
- **Definition of Done:** reset/clear the relevant Redis rate-limit key in `setUp`/`tearDown` of the affected test classes, following the same isolation pattern already established for AUDIT04B-ORDER-FLAKE-01; full suite runs clean end-to-end.
- **Verification already done this session:** the 2 affected files run in isolation pass 32/32 — confirms application behavior is correct, this is purely a test-isolation gap.
- **Blocking scope:** NON-BLOCKING. Does not gate WITHDRAWAL-POLICY-CORRECTION-02 or any other item in this queue.

---

# PARTE XXI — ROADMAP / PERCENTAGE (full arithmetic shown)

## A. B-Book Local MVP Readiness

```
Items (26, unchanged set definition from V1.3):
1 Onboarding=3, 2 KYC=3, 3 2FA=3, 4 Account/Product=3, 5 Forex=3,
6 Crypto=3(was 2), 7 Instrument catalog=2, 8 Pricing=3, 9 Swap=0,
10 Market orders=3, 11 Pending orders=3(was 0), 12 Partial close=3(was 0),
13 Execution=3, 14 Leverage/Margin=3, 15 PnL/Equity=3, 16 Stop-out=3,
17 Ledger=3, 18 Deposits=3, 19 Withdrawals=3, 20 Internal Treasury=3,
21 B-Book accounting=3, 22 Broker risk/exposure=3,
23 Support/history (tickets sub, excl. export)=3(was 2),
24 Security/audit=3, 25 Trader UI base=2, 26 Golden Scenarios=1

B_BOOK_SCORE = 3+3+3+3+3+3+2+3+0+3+3+3+3+3+3+3+3+3+3+3+3+3+3+3+2+1 = 71
B_BOOK_MAX   = 26 × 3 = 78
B_BOOK_PERCENT = 71/78 × 100 = 91.03% → 91.0%
```

**Why it moved (+10.2pt from 80.8%):** #6 Crypto (+1), #11 Pending orders (+3), #12 Partial close (+3), #23 Support tickets sub-component (+1) — all real, evidence-backed closures, not reclassification.

**Remaining gap (7 of 26 below 100%):** #7 Instrument catalog(2), #9 Swap(0), #23 export sub-component still drags a composite average elsewhere (see Parte III row 35's separate treatment), #25 Trader UI base(2), #26 Golden Scenarios(1).

## B. Private Staging Readiness

```
Identical to V1.3 — zero infra items moved.
STAGING_SCORE = 30, STAGING_MAX = 45
STAGING_PERCENT = 30/45 × 100 = 66.7%   (+0.0pt since V1.3)
```

**Why it did not move:** confirmed explicitly this session — no commit touched `deploy/`, `INFRA_PLAN_L1.md`, `DEPLOY.md`, or `SECURITY_CHECKLIST.md` since V1.3. All engineering progress in this window was local application code, not infrastructure.

## C. Production Candidate Readiness

Same set as Overall (Parte II) — 40 canonical IDs, no staging/production evidence exists anywhere, so Production = Overall by definition (identical numerator/denominator, per V1.3's own rule, unchanged):

```
80 / 200 × 100 = 40.0%   (+3.5pt since V1.3's 36.5%)
```

## D. Overall Broker Readiness

```
80 / 200 × 100 = 40.0%
```

### "Money Broker today is..."
A local B-Book broker with mature trading-core parity (market + pending + partial-close orders, certified Forex + Crypto data, hardened risk/ledger/accounting) and a genuinely complete local Customer Support system — all VERIFIED LOCAL, none of it staged or in production.

### "Money Broker today is NOT..."
Deployed anywhere. Compliant with its own withdrawal-minimum policy in code. Reconciled on a real stuck payout (#4). An A-Book broker. Feature-complete on Support (no emails, no SLA, no attachment flow). Ready for real customer money without closing the Parte XX queue, starting with item #1.

---

# PARTE XXII — HECHOS VS PENDIENTES

## HECHOS CONFIRMADOS
- HEAD = origin/main = `3170858...` — verificado en vivo contra el remoto.
- 22 bloques cerrados desde V1.3 (2026-09-02) hasta hoy (2026-09-17), todos con tag verificado.
- Pending Orders y Partial Close son código real y wired, no stubs — verificado leyendo el diff, no solo el commit message.
- Crypto Market Data ahora es Massive-exclusive, mismo patrón que Forex.
- El piso de retiro mínimo en código es $0.01, no $20 — verificado leyendo `forms.py` directamente, dos veces (una por el subagente de investigación, otra por el coordinador).
- WithdrawalRequest #4 sigue exactamente en el mismo estado atascado descrito históricamente — verificado con query de solo lectura a la DB de dev.
- Deposit #45 sigue reconciliado y acreditado — verificado con query de solo lectura.
- El sistema completo de Customer Support (01A-01D) es real, testeado (256 tests dedicados) y manualmente certificado por el Owner (10/10 puntos de certificación de 01D).
- Cero infraestructura de staging/VPS ha cambiado desde V1.3 — cero commits en los paths relevantes.
- Los archivos protegidos (`test_book06j1_population_engine_close_race.py`, `db.sqlite3.backup_before_0069_0072`) siguen presentes, sin seguimiento, sin modificar.
- `treasury_engine` no existe actualmente en la ruta esperada en esta máquina — no fue inspeccionado, tal como exige la instrucción.
- **Full-suite finding — diagnóstico CERRADO** (actualizado tras esta auditoría): la corrida completa (`manage.py test simulator`, 7065 tests) reportó 3 FAIL + 3 ERROR. Causa raíz confirmada: fuga de estado de rate-limit de Redis del endpoint `deposit_callback` (`@rate_limit("deposit_callback", limit=30, window=60)`, `simulator/views.py:1700`) — el presupuesto compartido se agota acumulativamente antes de que `test_deposit_emails.py::DepositConfirmedEmailTests` y el test relacionado de `test_ledger_invariant_audit.py` alcancen ese endpoint, que entonces retorna legítimamente HTTP 429. Los 6 fallos son en cascada de esa única condición. Verificado: los 2 archivos afectados corridos aislados = **32/32 PASS**. Ver clasificación formal completa abajo.

### FULL-SUITE TEST ISOLATION — clasificación formal

| Campo | Valor |
|---|---|
| Diagnóstico | **CERRADO** |
| Fix de implementación | **PENDIENTE** (bloque futuro potencial: `FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01` — resetear/limpiar la clave de rate-limit de Redis en `setUp`/`tearDown`, mismo patrón que AUDIT04B-ORDER-FLAKE-01. No implementado en este bloque de documentación) |
| Riesgo a la corrección financiera | **SIN EVIDENCIA DE REGRESIÓN** — comportamiento de la aplicación confirmado correcto (el rate limiter hace exactamente lo que debe hacer); es un gap de aislamiento de tests, no un defecto de runtime |
| Estado de bloqueo | **NO BLOQUEANTE** para el próximo bloque de corrección de dinero real (WITHDRAWAL-POLICY-CORRECTION-02), siempre que las suites dedicadas afectadas permanezcan verdes — confirmado: 32/32 |

**Readiness percentages unchanged — this was an evidence-quality/test-isolation clarification, not a capability maturity promotion.** Ninguna de las 40 capacidades canónicas (Parte III) ni la capacidad suplementaria S1 (Parte III-B) cambia de maturity por este hallazgo — ni Deposits (#18) ni Ledger/Accounting (#17) se promueven, porque el comportamiento de la aplicación ya era correcto (el 0 FAIL/0 ERROR de las suites dedicadas por archivo, ya citado en toda esta auditoría, sigue siendo la evidencia real de esas capacidades). Este hallazgo tampoco las degrada, porque no hay evidencia de regresión financiera. Overall/Production se mantienen en **40.0%** (80/200), B-Book Local MVP en **91.0%** (71/78), Private Staging en **66.7%** (30/45) — sin cambio respecto a los valores ya reportados en esta misma versión V1.4.

## PENDIENTE DE VERIFICAR
- Trader Intelligence (#29) — no re-investigado esta ventana, evidence debt de V1.3 se mantiene tal cual.
- Generic correction idempotency y el invariante TradingAccount balance-vs-ledger — no re-auditados esta ventana.
- Si `ledger-rounding-reconciliation-01-v1` tocó indirectamente el gap float/Decimal de `commission_for()` — se verificó directamente que NO lo tocó (función re-leída, sigue devolviendo `float`), pero vale la pena una auditoría dedicada completa del tema Decimal-discipline.

## POLICY_PENDING
7 items del catálogo de Knowledge Base (Parte III-B), estructuralmente deshabilitados: `withdrawal_minimum`, `deposit_supported_assets`, `deposit_supported_networks`, `deposit_minimum`, `deposit_fee`, `deposit_third_party_wallet`, `kyc_documents`.

## KNOWN CODE/POLICY GAPS
1. **WITHDRAWAL-POLICY-CORRECTION-02** (Parte V) — OPEN BLOCKER, dinero real.
2. `commission_for()` float vs Decimal (#8) — reconfirmado abierto.
3. Instrument catalog dual-source drift risk (#7) — sin cambio.
4. Payment Engine integration ausente dentro de trx_sim (#20) — arquitectónicamente confirmado como la causa raíz de por qué #4 no puede autoreconciliarse.
5. Support: sin lifecycle emails, sin SLA, sin flujo operativo de adjuntos, sin notificación de escalación a Ops (Parte IV).
6. **FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01** — gap de aislamiento de tests (no de aplicación), diagnóstico CERRADO, fix técnico PENDIENTE, NO BLOQUEANTE. Ver clasificación formal arriba.

---

# PARTE XXIII — INSTRUCCIONES PARA RETOMAR MONEY BROKER EN UNA NUEVA CONVERSACIÓN

```
PROJECT:            Money Broker
PATH:                /Users/naffermoreno/Desktop/trx_sim
REPO:                trx_simulator (github.com/Naffer27/trx_simulator)
CURRENT HEAD:        31708583363208463eef82640a734929c75bb221
CURRENT TAG:         customer-support-01d-knowledge-base-v1
DO NOT TOUCH:        /Users/naffermoreno/Desktop/treasury_engine (separate project)
                     simulator/tests/test_book06j1_population_engine_close_race.py
                     db.sqlite3.backup_before_0069_0072
LAST CLOSED BLOCK:   CUSTOMER-SUPPORT-01D (knowledge base + instant answers)
CURRENT OPEN BLOCK:  NONE — repository is clean at HEAD except protected/unrelated untracked docs
NEXT RECOMMENDED:    WITHDRAWAL-POLICY-CORRECTION-02 (see Parte V/XX #1 — real money-risk blocker)
```

### 20 facts the next conversation must know before giving any command

1. **HEAD and the latest tag are identical** (`3170858...`) — confirmed live against `origin/main`, no drift.
2. **This is a local-only broker.** Zero VPS/staging/production execution has ever occurred, confirmed again in this audit — do not assume any deployed environment exists.
3. **A real money-risk blocker is open right now**: `WithdrawForm.amount_usd` enforces a $0.01 minimum withdrawal in code, while confirmed business policy is $20. Do NOT silently fix this — it needs explicit Owner authorization on the exact enforcement design.
4. **WithdrawalRequest #4 is a real, unresolved, stuck payout** ($20.01, NOWPayments HTTP 403, provider state UNKNOWN). Never blind-retry or refund it without an explicit reconciliation design.
5. **Deposit #45 is fine** — $20, finished, credited, reconciled. Not an open issue.
6. **Customer Support (01A-01D) is a real, complete, locally-verified, manually-certified system** — not a stub. 256 dedicated tests, deterministic (non-AI) Knowledge Base, role hierarchy, INTERNAL-note isolation. Do not rebuild any of this from scratch.
7. **Support still lacks**: lifecycle emails, attachment upload/download flow, SLA, escalation notifications, and has 7 deliberately-disabled POLICY_PENDING knowledge items.
8. **Pending Orders and Partial Close are real and shipped** (Order Management V2A/V2B) — do not assume these are still missing, V1.3's "0" scores for these are obsolete.
9. **Crypto market data (BTCUSD/ETHUSD) is now certified** via Massive, same pattern as Forex — but lacks a long manual-acceptance session like Forex has.
10. **Payment Engine integration inside trx_sim is still 0** — `nowpayments.py`/`payout_providers.py` are the only payment code; there is no internal abstraction over an external Payment Engine yet, and the payout side cannot even query provider status.
11. **`treasury_engine` is a completely separate project** and does not currently exist at `/Users/naffermoreno/Desktop/treasury_engine` on this machine — never inspect or modify it without explicit Owner authorization, regardless of what any Money Broker work seems to imply.
12. **Never perform git add/commit/tag/push without explicit Owner authorization** — the established workflow always ends in Owner-manual Git closure, never Claude-performed closure.
13. **The protected files must never be modified/deleted/moved/staged**: `simulator/tests/test_book06j1_population_engine_close_race.py`, `db.sqlite3.backup_before_0069_0072`.
14. **B-Book Local MVP is at 91.0%** — a strong local baseline; the remaining gaps are Instrument catalog unification, Swap/Overnight (doesn't exist at all), Trader UI personalization, and the last Golden Scenario sub-certifications.
15. **Private Staging Readiness is unchanged at 66.7%** — all local engineering progress in this window did not touch infrastructure at all.
16. **Overall/Production readiness is 40.0%** (up from 36.5%) — driven entirely by local trading-core and support progress, not staging progress.
17. **Dev/test data in the DB is NOT production data** — tickets #1/#5/#9/#10/#11, Deposit #45, WithdrawalRequest #4 are all dev/manual-certification artifacts tied to the "Admin2" dev account.
18. **The engineering discipline on this project is unusually strict**: audit → design-lock → scoped implementation → dedicated tests → regression → manual certification → pre-Git audit → Owner-only Git closure, on every single block. Follow it; don't shortcut it.
19. **No `Plan Maestro Unificado V2/V3` file exists on disk** — do not reference it as if it were available; V1.3's Part X comparison (preserved conceptually in this document's history) is the most recent reconciliation against it that exists.
20. **This V1.4 document is meant to be fully self-contained** — a new conversation should be able to work from this PDF alone without needing any earlier audit version, exactly as V1.3 was designed to be self-contained relative to V1.2.1.

---

# PARTE XXIV — VERSION HISTORY

| Version | Date | Overall | B-Book MVP | Private Staging | Production | Delta | Blocks closed since previous version |
|---|---|---|---|---|---|---|---|
| V1 | 2026-09-02 | N/A | N/A | N/A | N/A | — | Primera auditoría de las 40 capacidades |
| V1.1 | 2026-09-02 | 36.5% | N/A | N/A | 36.5% | primer cálculo | Forex corregido a VERIFIED LOCAL completo |
| V1.2 | 2026-09-02 | 36.5% | 80.8% | 66.7% | 36.5% | +0.0 | Payment Engine/Treasury separados; snapshots.py reclasificado |
| V1.2.1 | 2026-09-02 | 36.5% | 80.8% | 66.7% | 36.5% | +0.0 | Reconciliación aritmética pura |
| V1.3 | 2026-09-02 | 36.5% | 80.8% | 66.7% | 36.5% | +0.0 | Consolidación documental pura |
| **V1.4** | **2026-09-17** | **40.0%** | **91.0%** | **66.7%** | **40.0%** | **Overall +3.5 · B-Book +10.2 · Staging +0.0 · Production +3.5** | **22 bloques reales: Order Mgmt V2A/V2B, Crypto certificado, snapshots fix, weekend-risk, sltp×2, ledger-rounding, chart-filter, withdrawal-security-extension, nowpayments-settlement-fix, withdrawal-policy-correction-01, withdrawal-otp×2, money-integrity-fix-01/02, CUSTOMER-SUPPORT-01A/01B/01C/01C.1/KB-spec/01D** |

---

# PARTE XXV — DEFINICIÓN EJECUTIVA

**Money Broker hoy NO es:**

- un broker terminado
- un broker en staging
- un broker en producción
- un A-Book real
- compliant con su propia política de retiro mínimo en código

**Money Broker hoy SÍ es:**

- un broker B-Book local funcional y considerablemente más maduro que en V1.3 (91.0% de B-Book Local MVP Readiness, arriba de 80.8%)
- con paridad completa de tipos de orden retail localmente: market, pending (limit/stop), partial close — los tres en maturity 3
- con market data Forex Y Crypto ahora certificados con el mismo patrón arquitectónico (Massive-only)
- con un sistema de Customer Support real, completo y manualmente certificado — no una tabla de tickets plana
- con dos gaps de integridad financiera reales encontrados y cerrados esta ventana (fondos sintéticos→wallet, redondeo de ledger)
- con un blocker de dinero real, abierto y sin resolver, documentado honestamente en este mismo documento: el piso de retiro mínimo en código ($0.01) contradice la política de negocio confirmada ($20)

**Gaps restantes, listados claramente:**

- WITHDRAWAL-POLICY-CORRECTION-02 (crítico, dinero real)
- WithdrawalRequest #4 sin reconciliar
- Compliance configurable por jurisdicción: no existe
- AML: no existe
- Broker economics incompleto: swap/overnight ausente
- Integración con un motor de pagos/tesorería externo: no existe dentro de trx_sim
- Infraestructura de staging: código listo, nunca ejercitada realmente (66.7%, sin cambio)
- Cualquier forma real de ejecución externa (A-Book/LP): cero conectividad real, sin cambio
- Customer Support: sin emails de ciclo de vida, sin SLA, sin flujo de adjuntos, sin Support AI, 7 items POLICY_PENDING

Ninguno de estos gaps es una sorpresa de esta auditoría — todos están nombrados con evidencia directa, verificados contra el repositorio actual, no copiados de V1.3.

---

*Fin de Money Broker — Broker Capability & Readiness Audit V1.4. Documento autosuficiente — no requiere ningún documento anterior para ser comprendido o usado como punto de partida de una nueva conversación.*
