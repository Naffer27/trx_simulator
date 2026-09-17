# MONEY BROKER — PLAN MAESTRO UNIFICADO V3

## Fuente única de verdad operativa del proyecto — 2026

> **V3 SUPERSEDE A V2 COMO DOCUMENTO OPERATIVO DE DIRECCIÓN.** V2 queda intacto como evidencia histórica — no fue modificado ni borrado. Regla documental heredada de V2, sin cambios: **no se crean nuevas listas maestras paralelas.**

**Fecha:** 2026-09-17 · **Tipo:** Documentación únicamente. Cero código. Cero tests nuevos. Cero modelos. Cero migraciones. Cero git.

**Repository checkpoint verificado en vivo esta sesión:**

| Campo | Valor | Verificado |
|---|---|---|
| PROJECT | Money Broker | — |
| PATH | `/Users/naffermoreno/Desktop/trx_sim` | — |
| REPO | `trx_simulator` (`github.com/Naffer27/trx_simulator`) | `git remote -v` |
| HEAD | `31708583363208463eef82640a734929c75bb221` | `git rev-parse HEAD` |
| origin/main | `31708583363208463eef82640a734929c75bb221` | `git rev-parse origin/main` — **MATCH** |
| Latest tag | `customer-support-01d-knowledge-base-v1` → `3170858...` | `git describe --tags` + `git rev-parse <tag>` — **MATCH con HEAD** |
| Working tree | Limpio salvo docs/archivos protegidos no relacionados | `git status --porcelain` |

**Discrepancia:** ninguna. Los valores esperados del prompt coinciden exactamente con la evidencia en vivo de Git.

---

## DASHBOARD DE ESTADO ACTUAL

| Área | Estado actual (V3) | Cambio vs V2 |
|---|---|---|
| Forex market data | CERRADO — Massive-only, publicado | Sin cambio |
| USD/JPY margin | CERRADO — base/quote-aware, publicado | Sin cambio |
| **Crypto market data** | **CERRADO a nivel VERIFIED LOCAL** — Massive-exclusive, 141 tests dedicados | **Cambió: de PENDIENTE a cerrado localmente** |
| **Order Management V2** | **CERRADO** — Pending orders + Partial close, reales y wired | **Nuevo bloque cerrado, no existía en V2** |
| Full suite | 7065 tests (creció desde los 6415 de V2) — **3 FAIL + 3 ERROR, diagnóstico CERRADO, ver §5.1** | Cambió: causa raíz confirmada (fuga de estado de rate-limit de Redis entre tests) — sin evidencia de regresión financiera; fix de aislamiento técnico queda pendiente, no bloqueante |
| Golden Scenarios | EN CURSO — sin cambio | Sin cambio |
| Payment Engine propio | PENDIENTE DE INTEGRACIÓN — arquitectónicamente confirmado como la causa raíz de por qué WithdrawalRequest #4 no puede autoreconciliarse | Sin cambio, con evidencia nueva de por qué importa |
| VPS privado | PENDIENTE — cero cambio de infraestructura desde V2 | Sin cambio |
| Compliance Config | PENDIENTE — sin cambio | Sin cambio |
| Trader UI V2 | PENDIENTE — sin cambio | Sin cambio |
| Copy Trading | PENDIENTE — Build vs Buy sin resolver | Sin cambio |
| **Customer Support (01A-01D)** | **CERRADO y certificado manualmente** — no existía como bloque en V2 | **Nuevo dominio completo, no anticipado por V2** |
| **WITHDRAWAL-POLICY-CORRECTION-02** | **OPEN BLOCKER — dinero real** — el piso de retiro en código es $0.01, la política confirmada es $20 | **Nuevo hallazgo crítico, no existía en V2** |

**Regla documental (heredada de V2, reafirmada):** desde esta versión, V3 reemplaza como fuente operativa a V2. A partir de ahora se actualiza este único archivo hasta crear el documento final de proyecto. Este documento tiene un propósito distinto y complementario a:

- **Broker Capability & Readiness Audit V1.4** (`docs/MONEY_BROKER_BROKER_CAPABILITY_READINESS_AUDIT_V1_4.md`) — mide madurez/evidencia objetivamente (40 capacidades, 0-5).
- **Money Broker Protocolo Maestro de Bloques y Fixes** (`Money_Broker_Protocolo_Maestro_Bloques_y_Fixes.pdf`) — define cómo se ejecuta y cierra cada bloque/fix.

Los tres documentos son complementarios. Este (V3) define **dirección, arquitectura, fases, prioridades y orden de ejecución** — no remide madurez (eso vive en el Audit) ni redefine metodología de cierre (eso vive en el Protocolo).

---

# 1. PROPÓSITO Y GOBIERNO DEL DOCUMENTO

Sin cambios respecto a V2 — estos principios se mantienen íntegros porque ninguna evidencia del repositorio ni decisión de negocio los contradice:

- El dueño del proyecto define negocio, producto, riesgo, libro, payouts y prioridades.
- La coordinación técnica define arquitectura, secuencia, design locks, invariantes y cierre de releases.
- Claude implementa únicamente el alcance aprobado y no decide producto, negocio ni Git.
- No se declara una capacidad terminada solo porque los tests pasan; debe cumplir aceptación funcional y arquitectura.
- Todo cambio financiero, de routing, concurrencia o ejecución externa requiere audit/design lock antes de implementación.

**Confirmación de disciplina, esta ventana:** los 22 bloques cerrados entre V2 (2026-09-02) y hoy (2026-09-17) siguieron todos el mismo patrón — audit → design lock → implementación acotada → tests dedicados → regresión → certificación manual cuando aplica → auditoría pre-Git → cierre de Git por el Owner. Sin excepciones detectadas.

---

# 2. MISIÓN DEL PROYECTO

Sin cambios — Money Broker sigue sin ser un simulador genérico:

- Liquid Brokers se usa como referencia funcional, no como tecnología a copiar.
- Trader Intelligence recomienda; no controla por sí sola el book final.
- Book Assignment debe poder ser modificado por staff autorizado y por Broker Risk, con auditoría.
- **A-Book solo existe cuando riesgo real sale a un LP real y existe confirmación/reconciliación.** Reafirmado explícitamente en el Audit V1.4: toda la infraestructura de simulación LP (BOOK-05/06, 35 archivos de test) se autodescribe en sus propios docstrings como NO A-Book.
- **Trading Equity, saldo retirable, payout aprobado, liquidez de tesorería y cash settled son conceptos distintos.** Reafirmado con evidencia nueva: `money-integrity-fix-01-synthetic-funds-gate-v1` cerró un gap real donde esta distinción no estaba enforced en código (fondos sintéticos DEMO/CHALLENGE/FUNDED podían cruzar a wallet real sin gate).
- Toda regla económica crítica debe ser configurable, versionada o congelada por snapshot de producto/cuenta.

---

# 3. ESTADO ACTUAL CONFIRMADO (reconstruido desde repo/git/tests/docs)

## 3.1 Trading Core

| Área | V2 (2026-09-02) | V3 (2026-09-17) |
|---|---|---|
| Order management | Solo market orders | **Market + Pending (Limit/Stop) + Partial Close** — `order-management-v2a/v2b`, 59 tests dedicados combinados |
| Margin/Leverage | Base/quote-aware, USD/JPY validado | Sin cambio, sin regresión |
| PnL/Equity | `pnl_engine.py` motor único | Sin cambio |
| Stop-out | Rechaza precios incompletos | **Reforzado**: `golden-weekend-risk01` cerró un gap real (exposición sin fallback cuando el mercado cierra) — `LastKnownMarketPrice` nuevo |
| Ledger | BrokerLedger con idempotencia | **Reforzado**: `ledger-rounding-reconciliation-01` cerró un drift real de redondeo |
| B-Book | Contabilidad de contraparte real | Sin cambio de madurez; reforzado indirectamente |
| Broker Risk / Exposure | `broker_exposure.py`, bug conocido en `snapshots.py` | **Bug cerrado**: `fix-snapshots-contract-size-01` — confirmado con docstring explícito en el código actual |
| Routing / Liquidity | Foundation/shadow | Sin cambio — cero conectividad real, sin tag lo tocó |
| Trader Intelligence | Foundation, alimenta como input | Sin cambio, sin re-investigación esta ventana |

## 3.2 Market Data

| Área | V2 | V3 |
|---|---|---|
| Forex | Massive-only, EUR/USD, GBP/USD, USD/JPY, AUD/USD | Sin cambio de certificación |
| Crypto | PENDIENTE — Binance 451, proveedor sin decidir | **CERRADO a nivel VERIFIED LOCAL** — Massive-exclusive (`_MASSIVE_CRYPTO_ENABLED_SYMBOLS`, `feeds.py:2024-2043`), 141 tests dedicados. Falta solo una sesión de aceptación manual de larga duración con la misma paridad que Forex (evidence debt, no bloqueante) |

## 3.3 Payments

| Área | V2 | V3 |
|---|---|---|
| Deposits | NowPayments, solo cripto, madura | Sin cambio |
| Withdrawals | Flujo con gates reales | Reforzado (email-OTP de 2 pasos, `VerifiedWithdrawalWallet`) — **pero con un OPEN BLOCKER nuevo, ver §4** |
| NowPayments | Cliente embebido de terceros | Sin cambio arquitectónico |
| Payment Engine integration | PENDIENTE DE INTEGRACIÓN | Sin cambio — **arquitectónicamente confirmado** que `payout_providers.py::NowPaymentsAdapter` no puede siquiera consultar estado (`status_query=False, cancel=False`), lo cual es la causa raíz directa de por qué WithdrawalRequest #4 sigue atascado |

## 3.4 Security / Compliance

| Área | V2 | V3 |
|---|---|---|
| KYC | Real, ciclo completo | Sin cambio |
| 2FA | Real, TOTP auditado | Sin cambio |
| Emails | Reales para lifecycle de depósito/retiro | Sin cambio |
| Owner/Ops | Existía sin formalizar completamente | **Formalizado**: `money-integrity-fix-02-owner-ops-financial-control-v1` construyó `OwnerRoot`/`OpsAdminProfile`/`permission_levels.py`/`owner_actions.py` — la jerarquía real que todo el trabajo posterior (incluyendo Customer Support) usa |
| Money Integrity | Sin gaps nombrados en V2 | **Dos gaps reales encontrados y cerrados** esta ventana: fondos sintéticos→wallet (fix-01), redondeo de ledger |

## 3.5 Customer Support — dominio completo, no existía en V2

V2 no mencionaba Customer Support como dominio propio. Hoy es un sistema real de 5 bloques (ver §6 completo abajo).

## 3.6 Infraestructura

| Área | V2 | V3 |
|---|---|---|
| Redis | Sin TimeoutError, verificado | Sin cambio |
| Celery | — | Sin cambio, Beat+worker verificados localmente |
| Daphne | — | Sin cambio |
| PostgreSQL | — | Sin cambio — sigue sin usarse como motor real, solo SQLite en dev/test |
| Nginx/TLS | — | Sin cambio — config existe, ningún certificado jamás emitido |
| systemd | — | Sin cambio — `deploy/systemd/*.service` presentes, nunca ejercitados en un host real |
| Backups | — | Sin cambio — 7 scripts, **cero artefacto de ejecución exitosa confirmado** |
| Restore drill | — | Sin cambio — nunca ejecutado con éxito documentado |
| Monitoring | — | Precisión nueva: `init_sentry()` es código real gateado por `SENTRY_DSN`, nunca ejercitado con un DSN real |
| VPS | PENDIENTE | **Confirmado explícitamente de nuevo esta ventana**: cero commits tocaron `deploy/`/`INFRA_PLAN_L1.md`/`DEPLOY.md`/`SECURITY_CHECKLIST.md` desde V2. Cero evidencia de un VPS jamás provisionado |

---

# 4. WITHDRAWAL BUSINESS POLICY — VERIFICADO, GAP CONFIRMADO

**Política de negocio confirmada:**
- Mínimo: **USD 20**
- USD 20 a USD 1,000: flujo automatizado normal de seguridad
- Sobre USD 1,000: revisión/aprobación interna adicional

**Discrepancia código-política, verificada directamente en el código actual esta sesión:**

```python
# simulator/forms.py:127-130
amount_usd = forms.DecimalField(
    ...
    min_value=Decimal("0.01"),
    ...
)
```

Este es el único piso de monto en todo el flujo de retiro. `simulator/tests/test_withdrawal_minimum.py` documenta esto como una decisión **deliberada** de `withdrawal-policy-correction-01-v1` (2026-09-10) — el commit que eliminó el viejo piso de $1,000 y no dejó ningún reemplazo.

> **MARCADO: WITHDRAWAL-POLICY-CORRECTION-02 — OPEN / HIGH PRIORITY.**
> No se corrige en este bloque de documentación, per instrucción explícita. Es el ítem #1 del Plan de Ataque actualizado (§7).

---

# 5. REAL MONEY CONTINUITY

## Deposit #45 — CONFIRMADO ACTUAL

$20, USDT/TRC20, `status=finished`, `credited=True`. Verificado con query de solo lectura a la DB de dev esta sesión. **Clasificación: CONFIRMED CURRENT.**

## Withdrawal #4 — CONFIRMADO ACTUAL (sin resolver)

$20.01 (nótese la discrepancia $20 vs $20.01, relacionada con §4), `status=processing`, un `PayoutAttempt` con `status=unknown`, sometido 2026-09-12, NOWPayments retornó HTTP 403 en su momento, estado del proveedor sigue **UNKNOWN**. `PayoutAttempt.UNKNOWN` está documentado en el código como no-terminal — nunca tratado como FAILED, nunca reconciliado automáticamente. Sin retry ciego, sin refund, exactamente como exige la regla de seguridad establecida. **Clasificación: CONFIRMED CURRENT — no histórico, sigue abierto hoy.**

## 5.1 Full test suite finding — ROOT CAUSE CONFIRMED

Una corrida completa de la suite (`manage.py test simulator`, 7065 tests) reportó **3 FAIL + 3 ERROR** — no la 0 FAIL/0 ERROR que tanto V2 como el Audit V1.4 citan como baseline histórico de las suites dedicadas por bloque. Una segunda corrida de diagnóstico identificó exactamente los 6 tests y su causa raíz:

- Los 6 fallos trazan al endpoint de webhook de depósito, protegido por `@rate_limit("deposit_callback", limit=30, window=60)` (`simulator/views.py:1700`).
- La suite completa agota acumulativamente el presupuesto compartido de rate-limit (respaldado por Redis, que **no** se resetea entre tests como sí la DB) antes de que `test_deposit_emails.py::DepositConfirmedEmailTests` y el test relacionado de invariante de ledger (`test_ledger_invariant_audit.py`) alcancen ese endpoint.
- El endpoint entonces retorna legítimamente HTTP 429 en vez de HTTP 200.
- Como el callback nunca se procesa: las aserciones de email de confirmación de depósito fallan (3 ERROR — `mock_email.call_args` es `None` porque el email nunca se encoló) y las aserciones relacionadas con `pending_balance` fallan (2 FAIL adicionales + 1 en el test de invariante) — todas son fallas en cascada de la misma condición de rate-limit, no fallas independientes.

**Verificación:** los 2 archivos de test directamente afectados se corrieron aislados: **32/32 PASS.** Esto confirma que el comportamiento de la aplicación es correcto — el rate limiter está haciendo exactamente lo que debe hacer; el problema es que el estado de Redis se comparte entre tests sin limpieza dedicada.

**Clasificación:**
- **TEST-ISOLATION GAP.**
- **NO es una regresión financiera confirmada.**
- **NO es un defecto de runtime de la aplicación.**
- **CAUSA RAÍZ CONFIRMADA.**

**Distinción importante:**
- **DIAGNÓSTICO = CERRADO.**
- **FIX TÉCNICO DE AISLAMIENTO = AÚN NO IMPLEMENTADO.**

Bloque futuro potencial: **FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01** — resetear/limpiar la clave de rate-limit de Redis relevante en `setUp`/`tearDown` de los tests afectados, siguiendo el mismo patrón ya establecido para AUDIT04B-ORDER-FLAKE-01. **No implementado en este bloque de documentación.** Es un ítem de higiene de infraestructura de tests, **NO BLOQUEANTE** para el trabajo de corrección de dinero real — ver el Plan de Ataque actualizado en §7, donde ya no ocupa un lugar en la secuencia de prioridad de negocio.

---

# 6. CUSTOMER SUPPORT — ESTADO ACTUAL COMPLETO

Verificado, cada hash contra Git en vivo esta sesión:

| Bloque | Commit | Tag | Verificado |
|---|---|---|---|
| CUSTOMER-SUPPORT-01A | `f61f8feae4965c3324de8414bcebdcc68d9adf30` | `customer-support-01a-data-foundation-v1` | ✅ |
| CUSTOMER-SUPPORT-01B | `de5b51bef2952b70ad7836ff3dfafcea2c36e61b` | `customer-support-01b-dedicated-support-panel-v1` | ✅ |
| CUSTOMER-SUPPORT-01C | `30349feb79de79a4c2f0eee238dde9c694dd4042` | `customer-support-01c-customer-thread-v1` | ✅ |
| CUSTOMER-SUPPORT-01C.1 | `4706485e3f9f92eb2e0d0d088ecd4d5ed73987b9` | `customer-support-01c1-chat-widget-v1` | ✅ |
| Knowledge Base Spec | `6ca042f38a290501c4f1bf982dd94474cb9390dd` | `customer-support-knowledge-base-spec-v1` | ✅ |
| CUSTOMER-SUPPORT-01D | `31708583363208463eef82640a734929c75bb221` | `customer-support-01d-knowledge-base-v1` | ✅ = HEAD actual |

**Real e implementado**, verificado contra código actual (no solo mensajes de commit):

- **`/staff/support/` dedicado** — `simulator/support_panel_views.py`, decorador propio `support_panel_required`, **no** `admin.site`, **no** `@staff_member_required`.
- **Jerarquía CLIENT → SUPPORT → OPS → OWNER** — real, gateada por `permission_levels.py`.
- **Customer threads** — `_build_customer_thread()` excluye `INTERNAL` a nivel de query, no de template.
- **INTERNAL notes** — aisladas, nunca expuestas al cliente (verificado con tests dedicados en las 5 suites).
- **Assignments/escalation** — `apply_transition()`, `STATUS_ESCALATED` real.
- **Floating widget** — arquitectura de fragment-swap HTML, reutiliza `SupportTicket`/`SupportMessage` sin duplicar lógica.
- **Deterministic KB** — 82 items estáticos, explícitamente NO IA generativa. 75 habilitados, 7 POLICY_PENDING.
- **GREEN/YELLOW/RED** — respuesta instantánea aprobada / mensaje seguro sin inventar estado / mensaje de seguridad con handoff, respectivamente.
- **Mis conversaciones** — el widget ahora SIEMPRE abre en KB home; conversaciones existentes solo alcanzables explícitamente.
- **Human handoff** — reutiliza `_create_support_ticket()` real, sin lógica paralela.
- **Certificación manual del browser** — confirmada por el Owner, 10/10 puntos para 01D, tras dos rondas de manual-certification-fix (cache stale, luego el conflicto de auto-selección de ticket activo bloqueando KB home).

**256 tests dedicados** across 5 archivos (26+54+38+58+80).

**Restante (no completo, no sobreclamado):** sin emails de ciclo de vida, sin flujo operativo de adjuntos (`SupportAttachment` es solo foundation), sin SLA, sin notificación de escalación a Ops, sin certificación operacional de un agente Support real separado del Owner, sin política de conversaciones duplicadas, sin audit trail dedicado al estilo `broker_audit.py`, 7 items POLICY_PENDING, sin widget público anónimo (diferido intencionalmente), sin prueba a escala.

**Secuencia planeada (inferida de los propios docstrings del código, no de un roadmap escrito aparte):**
```
01D (HECHO) → 01E (adjuntos + emails de ciclo de vida) → 01F (SLA + notificación de escalación + audit trail) → Support AI (futuro, capa generativa sobre la base determinística de 01D)
```

---

# 7. PLAN MAESTRO DE ATAQUE — V3 (reconstruido, no copiado de V2)

**Metodología:** orden por 1) seguridad/riesgo de dinero real, 2) corrección, 3) dependencia técnica, 4) valor de producto — nunca por atractivo visual. Contrastado contra la secuencia candidata provista y ajustado donde la evidencia actual del repo lo exige (marcado explícitamente cada ajuste).

**Actualización post-diagnóstico (§5.1):** el "Full suite triage" que ocupaba el puesto #3 en la versión anterior de esta tabla **ya no es un prerequisito bloqueante** — su diagnóstico está CERRADO (fuga de estado de rate-limit de Redis entre tests, sin evidencia de regresión financiera). El fix técnico de aislamiento restante es un ítem de higiene de infraestructura de tests, documentado por separado al final de esta sección como **NO BLOQUEANTE**, y ya no ocupa un lugar en la secuencia de prioridad de negocio. La tabla fue renumerada en consecuencia — **las prioridades de negocio no cambiaron**, solo la numeración.

| # | Bloque | Resultado esperado | Estado | Ajuste vs secuencia candidata |
|---|---|---|---|---|
| 0 | Baseline / alineación documental | Confirmar HEAD, tags, y que V1.4 + V3 + Protocolo están sincronizados | **HECHO** (este mismo documento) | — |
| 1 | **WITHDRAWAL-POLICY-CORRECTION-02** | Enforce USD 20 mínimo en código | **OPEN, máxima prioridad** | Sin ajuste — dinero real, confirmado |
| 2 | Withdrawal #4 reconciliation | Resolver el payout atascado sin retry ciego | **OPEN** | Secuenciado después de #1 (no reconciliar un monto cuya política de mínimo sigue en disputa) |
| 3 | Real withdrawal E2E certification | Sesión manual completa, $20-$1,000 y >$1,000 | **PENDIENTE, depende de #1-#2** | — |
| 4 | Internal Broker Trading Certification (Golden Scenarios restantes) | Spread/lot/margin/commission/PnL/equity/close/ledger/admin/chart Bid-Ask sync | **PENDIENTE — técnicamente desbloqueado** (Order Mgmt V2 y Crypto ya certificados) pero sesiones manuales nunca registradas por separado | — |
| 5 | Payment E2E certification | Depósito+retiro end-to-end | **PENDIENTE, depende de #1-#2** | — |
| 6 | Money Integrity remaining gaps | Re-auditar idempotencia genérica de corrección + invariante TradingAccount balance-vs-ledger | **PENDIENTE, confirmatorio** | — |
| 7 | CUSTOMER-SUPPORT-01E | Adjuntos + emails de ciclo de vida | **PENDIENTE** | — |
| 8 | CUSTOMER-SUPPORT-01F | SLA + notificación de escalación + audit trail | **PENDIENTE, depende de 01E** | — |
| 9 | Crypto market-data — sesión de aceptación manual | Cerrar el evidence-debt de paridad con Forex | **PENDIENTE, NO BLOQUEANTE** | **Ajustado**: la secuencia candidata decía "si sigue abierto" — **ya no está abierto a nivel de certificación técnica** (VERIFIED LOCAL confirmado, Audit V1.4 #6), solo falta la sesión manual larga como paridad cosmética con Forex |
| 10 | Payment Engine Integration Audit | Design lock de la integración, sin tocar `treasury_engine` | **PENDIENTE** | — |
| 11 | OWASP / security review | Debida diligencia previa a cualquier intento de staging | **PENDIENTE** | — |
| 12 | Dev/test superuser cleanup | Higiene de cuentas antes de cualquier staging (p.ej. "Admin2") | **PENDIENTE** | — |
| 13 | PostgreSQL + restore drill | Provisionar y ejercitar de verdad | **PENDIENTE** | — |
| 14 | Private VPS Staging | Deploy real, primera capacidad posible en maturity 4 | **PENDIENTE, depende de #1-#2 y #13** | — |
| 15 | Broker Economics (Swap/Overnight) | Política + implementación mínima | **PENDIENTE** | — |
| 16 | Compliance Config / AML | Jurisdicción/provider policy configurable, screening AML | **PENDIENTE** | — |
| 17 | Trader UI V2 | Temas, colores, selector, responsive | **PENDIENTE** | — |
| 18 | Routing V2 Runtime / Book Authority | Activar política real, override auditable | **PENDIENTE** | — |
| 19 | A-Book — solo si/cuando el negocio lo requiera | LP real, lifecycle, reconciliation | **PENDIENTE, explícitamente condicionado a decisión de negocio, no técnica** | — |
| 20 | Private Beta | Escalamiento 10→25→50→100 usuarios | **PENDIENTE, depende de todo lo anterior** | — |

**Diferencias explícitas vs la secuencia candidata provista:** se corrigió el ítem de certificación crypto (ahora #9) para reflejar que ya está cerrado a nivel VERIFIED LOCAL, no "si sigue abierto" — el evidence-debt restante es cosmético, no bloqueante. Ningún otro reordenamiento de las prioridades de negocio fue necesario — la secuencia candidata del Owner ya reflejaba correctamente las dependencias reales encontradas en el repositorio.

### 7.1 Ítem de higiene de infraestructura de tests — NO BLOQUEANTE, fuera de la secuencia de prioridad de negocio

| Bloque | Propósito | Estado | Bloquea la secuencia de arriba? |
|---|---|---|---|
| **FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01** | Resetear/limpiar la clave de rate-limit de Redis del endpoint `deposit_callback` en `setUp`/`tearDown` de los tests afectados, siguiendo el patrón ya establecido para AUDIT04B-ORDER-FLAKE-01, para lograr aislamiento determinístico de la suite completa | **PENDIENTE** | **NO** — explícitamente no debe adelantarse a la corrección de dinero real (#1-#2 arriba). Puede ejecutarse en paralelo o después, a discreción del Owner |

---

# 8. ARQUITECTURA OBJETIVO DEL BROKER

Sin cambios respecto a V2 — la cadena y separación de responsabilidades siguen siendo el objetivo correcto:

```
Client UI → Pricing/Spread/Commission → Execution/Margin/PnL → Client & Broker Accounting
→ Trader Intelligence → Broker Risk/Exposure → Routing Authority
→ B-Book/Partial Hedge/A-Book → Treasury/Payout/Reserves → Broker Net Economics
```

**Mantenido explícitamente: A-Book != shadow LP simulation.** Toda la infraestructura BOOK-05/06 (LiquidityProvider, LiquidityDecision, LiquidityLedger, DealingDeskDecision) se autodescribe en código como simulación, sin conectividad real — confirmado de nuevo esta sesión, sin cambio desde V2.

- **Trading & Accounting:** ejecución, balance, equity, margin, PnL, spread, comisión y ledgers.
- **Trader Intelligence:** comportamiento, consistencia, rentabilidad, toxicidad, estabilidad y recomendaciones — sigue siendo solo INPUT, no controla el book.
- **Broker Risk & Exposure:** gross/net exposure, concentración, límites.
- **Routing & Liquidity:** internalización, hedge recommendation, partial hedge, futura conectividad LP.
- **Broker Decision Authority:** decisión final de book, overrides y audit trail — sigue sin existir el override manual real.
- **Treasury / Payout Risk:** obligaciones retirables, reservas, liquidez y settlement.
- **Broker Economics:** spread + commission + counterparty PnL - payment costs - payouts - hedge/LP costs.

---

# 9. GOBERNANZA OPERATIVA ACTUAL

## Jerarquía verificada contra código actual

| Rol | Persona | Modelo | Restricción |
|---|---|---|---|
| **OWNER ROOT** | Naffer | `OwnerRoot` | Singleton a nivel de DB (`CheckConstraint` + `UniqueConstraint`), nunca creable/editable/borrable vía Django Admin, ni siquiera por un superuser |
| **OPS ADMIN** | Ernesto | `OpsAdminProfile` | Mismo patrón singleton, pero reemplazable en el tiempo |
| **SUPPORT** | autoridad dedicada | permiso `is_customer_support` | Solo tickets de soporte, sin mutación financiera arbitraria — confirmado por grep, ningún código de `support_panel_views.py` toca `Wallet`/`TradingAccount`/`WithdrawalRequest` |
| **CLIENT** | cliente | `User` estándar | Acceso solo a lo propio, enforced por filtros de ownership en cada ruta |

No se incluyen secretos ni credenciales privadas en este documento.

---

# 10. ARCHIVOS PROTEGIDOS / LÍMITES DEL PROYECTO

**Nunca tocar:** `/Users/naffermoreno/Desktop/treasury_engine` — proyecto completamente separado. Confirmado esta sesión: la ruta no existe actualmente en esta máquina; no fue inspeccionada ni modificada, tal como exige la instrucción, independientemente de lo que cualquier trabajo de Money Broker pudiera sugerir.

**Protegidos, verificados presentes y sin modificar esta sesión:**
- `simulator/tests/test_book06j1_population_engine_close_race.py`
- `db.sqlite3.backup_before_0069_0072`

Ninguno de los dos debe ser modificado, borrado, movido, staged o commiteado.

---

# 11. GOBERNANZA DE DOCUMENTOS

| Documento | Propósito |
|---|---|
| **Plan Maestro Unificado V3** (este documento) | Dirección, arquitectura, fases, prioridades y orden de ejecución |
| **Broker Capability & Readiness Audit V1.4** | Medición objetiva de madurez/evidencia (matriz de 40 capacidades, 0-5) |
| **Protocolo Maestro de Bloques y Fixes** | Metodología de ejecución y cierre de cada bloque/fix |

Los tres son complementarios. **No se crean roadmaps maestros paralelos.** Cuando este documento y el Audit V1.4 parezcan decir cosas distintas sobre un mismo ítem, el Audit es la fuente de verdad para MADUREZ (¿cuánto está hecho, con qué evidencia?) y este Plan Maestro es la fuente de verdad para DIRECCIÓN (¿qué sigue, en qué orden, por qué?).

---

# 12. GATES DE RELEASE (sin cambios respecto a V2, reafirmados)

- No implementar cambios financieros, de concurrencia, routing o ejecución externa sin audit/design lock.
- Claude no hace git add/commit/tag/push; Git closure permanece separado y controlado. **Confirmado sin excepción en los 22 bloques cerrados desde V2.**
- No afirmar producción solo por tests.
- No afirmar A-Book sin LP real y reconciliation.
- No cambiar regla de producto sin aprobar antes la regla objetivo.
- No ir a VPS staging hasta cerrar MUST-TEST local aplicable.
- No lanzamiento público hasta cerrar blockers de producción, operaciones, legal/compliance y dependencias externas.
- **Nuevo, explícito tras el hallazgo de §4:** ningún gap de política de dinero real (como WITHDRAWAL-POLICY-CORRECTION-02) se corrige silenciosamente dentro de un bloque de documentación — requiere su propio bloque autorizado explícitamente.

---

# 13. PRÓXIMAS ACCIONES INMEDIATAS

- Abrir **WITHDRAWAL-POLICY-CORRECTION-02** — decisión del Owner sobre el diseño exacto de enforcement del mínimo de $20. Ya no bloqueado por el hallazgo de suite completa (§5.1 — diagnóstico cerrado, sin evidencia de regresión financiera).
- Diseñar la reconciliación de WithdrawalRequest #4 — sin retry ciego, sin refund automático.
- Continuar Golden Broker Scenarios restantes (ahora técnicamente desbloqueados por Order Mgmt V2 + Crypto).
- Abrir CUSTOMER-SUPPORT-01E cuando el Owner lo priorice.
- Mantener Broker Economics, Compliance Config, UI V2 y Copy Trading en ese orden salvo decisión explícita del dueño — sin cambio respecto a V2.
- FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01 (§7.1) — ítem de higiene, no bloqueante, ejecutable en paralelo o después a discreción del Owner.

---

# 14. INSTRUCCIONES PARA RETOMAR MONEY BROKER

```
PROJECT:             Money Broker
PATH:                /Users/naffermoreno/Desktop/trx_sim
REPO:                trx_simulator (github.com/Naffer27/trx_simulator)
VERIFIED HEAD:       31708583363208463eef82640a734929c75bb221
VERIFIED origin/main: 31708583363208463eef82640a734929c75bb221
LATEST TAG:          customer-support-01d-knowledge-base-v1
LAST CLOSED BLOCK:   CUSTOMER-SUPPORT-01D
OPEN BLOCK:          NINGUNO cerrado formalmente — WITHDRAWAL-POLICY-CORRECTION-02 es el
                     hallazgo activo sin bloque de implementación abierto todavía. El
                     hallazgo de suite completa (§5.1) ya tiene diagnóstico CERRADO (fuga
                     de rate-limit de Redis entre tests, sin evidencia de regresión
                     financiera) — su fix técnico (FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-
                     ISOLATION-01, §7.1) es NO BLOQUEANTE
NEXT RECOMMENDED:    WITHDRAWAL-POLICY-CORRECTION-02 — ya no bloqueado por el hallazgo de
                     suite completa
DO NOT TOUCH:        /Users/naffermoreno/Desktop/treasury_engine (proyecto separado)
                     simulator/tests/test_book06j1_population_engine_close_race.py
                     db.sqlite3.backup_before_0069_0072
GIT:                 Claude nunca hace git add/commit/tag/push sin autorización explícita del Owner
```

### 18 hechos que la próxima conversación debe saber antes de dar cualquier comando

1. **HEAD y el último tag son idénticos** (`3170858...`) — verificado en vivo contra el remoto, sin drift.
2. **Este es un broker local únicamente.** Cero ejecución en VPS/staging/producción ha ocurrido jamás — no asumir ningún entorno desplegado.
3. **Hay un blocker de dinero real abierto ahora mismo**: `WithdrawForm.amount_usd` enforce $0.01, la política confirmada es $20. No corregirlo silenciosamente — necesita autorización explícita del Owner sobre el diseño exacto.
4. **WithdrawalRequest #4 es un payout real, atascado, sin resolver** ($20.01, NOWPayments HTTP 403, estado UNKNOWN). Nunca hacer retry ciego ni refund sin un diseño explícito de reconciliación.
5. **Deposit #45 está bien** — $20, finished, credited, reconciliado. No es un problema abierto.
6. **Una corrida completa de la suite (7065 tests) mostró 3 FAIL + 3 ERROR — causa raíz CONFIRMADA**: fuga de estado de rate-limit de Redis del endpoint `deposit_callback` entre tests (no un bug de la aplicación). Los 2 archivos afectados pasan 32/32 aislados. Sin evidencia de regresión financiera. El fix técnico de aislamiento (FIX-DEPOSIT-CALLBACK-RATELIMIT-TEST-ISOLATION-01) queda pendiente pero es NO BLOQUEANTE — no lo dejes adelantarse al trabajo de corrección de dinero real.
7. **Customer Support (01A-01D) es un sistema real, completo, verificado localmente y certificado manualmente** — no reconstruir desde cero. 256 tests dedicados.
8. **Support todavía carece de**: emails de ciclo de vida, flujo de adjuntos, SLA, notificaciones de escalación, y tiene 7 items de Knowledge Base deliberadamente deshabilitados (POLICY_PENDING).
9. **Pending Orders y Partial Close son reales y publicados** (Order Management V2A/V2B) — no asumir que siguen faltando.
10. **Crypto market data (BTCUSD/ETHUSD) está certificado** vía Massive, mismo patrón que Forex — solo falta una sesión de aceptación manual larga como paridad cosmética, no bloqueante.
11. **La integración con un Payment Engine propio dentro de trx_sim sigue en cero** — `nowpayments.py`/`payout_providers.py` son el único código de pagos; el lado de payout ni siquiera puede consultar estado del proveedor.
12. **`treasury_engine` es un proyecto completamente separado** y actualmente no existe en `/Users/naffermoreno/Desktop/treasury_engine` en esta máquina — nunca inspeccionarlo ni modificarlo sin autorización explícita del Owner.
13. **Nunca hacer git add/commit/tag/push sin autorización explícita del Owner** — el flujo establecido siempre termina en cierre de Git manual por el Owner.
14. **Los archivos protegidos nunca deben modificarse/borrarse/moverse/stagearse**: `test_book06j1_population_engine_close_race.py`, `db.sqlite3.backup_before_0069_0072`.
15. **B-Book Local MVP está en 91.0%** según el Audit V1.4 — un baseline local fuerte; los gaps restantes son Instrument catalog, Swap/Overnight (no existe), personalización de Trader UI y las últimas certificaciones de Golden Scenarios.
16. **Private Staging Readiness sigue en 66.7%, sin cambio** — todo el progreso de ingeniería de esta ventana fue local, no tocó infraestructura.
17. **Este documento (V3) y el Audit V1.4 son complementarios**: V3 = dirección, Audit = madurez medida. No crear un tercer documento paralelo — actualizar estos, siguiendo la regla de V2 heredada.
18. **La disciplina de ingeniería de este proyecto es inusualmente estricta**: audit → design-lock → implementación acotada → tests dedicados → regresión → certificación manual → auditoría pre-Git → cierre de Git solo por el Owner, en cada bloque. Seguirla; no atajarla.

---

# 15. REGLA DE ACTUALIZACIÓN DE ESTE DOCUMENTO

Sin cambios respecto a V2: este Plan Maestro Unificado V3 será el único documento vivo de dirección del proyecto. Después de cada bloque importante, se actualizarán: estado, evidencia, fase, decisiones y próximos pasos. No se crearán nuevas listas maestras paralelas. Al finalizar el proyecto, esta línea de versiones se convertirá en un documento final que describa la arquitectura, historia de decisiones, capacidades terminadas, operación y readiness de Money Broker.

---

*Fin de Money Broker — Plan Maestro Unificado V3. Documento operativo — complementario al Broker Capability & Readiness Audit V1.4 y al Protocolo Maestro de Bloques y Fixes, nunca un reemplazo de ninguno de los dos.*
