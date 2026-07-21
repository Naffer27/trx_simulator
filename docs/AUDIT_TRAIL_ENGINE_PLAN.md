# Audit Trail Engine — Análisis y Diseño (AUDIT-02 → AUDIT-0N)

| Campo | Valor |
|---|---|
| Rol del documento | Fase de análisis y diseño — sin implementación |
| Fecha | 2026-07-21 |
| Ruta permitida | `~/Desktop/trx_sim` (única ruta tocada) |
| Rama | `main`, limpia, sincronizada con `origin/main` |
| HEAD | `776d48a` — feat: broker-event-audit-foundation-audit01 |
| Roadmap oficial | Money Broker — Engineering Architecture & Development Roadmap 2026: (1) **Audit Trail Engine**, (2) BOOK-04 Routing, (3) Liquidity Engine, (4) Dealing Desk híbrido A-book/B-book |
| Alcance de este documento | Solo análisis y diseño. No se implementó código, no se modificó ningún archivo existente, no se creó ninguna migración. |

---

## 0. Corrección crítica de encuadre (léase primero)

**El Audit Trail Engine no empieza de cero.** El commit HEAD (`776d48a feat: broker-event-audit-foundation-audit01`) ya implementa **AUDIT-01 — Broker Event Audit Trail Foundation** (`simulator/broker_audit.py` + modelo `BrokerAuditEvent` + `BrokerAuditObservationLock`), con 68 tests dedicados (`simulator/tests/test_broker_audit_trail.py`), append-only reforzado también a nivel de Django Admin (`has_add_permission`/`has_change_permission`/`has_delete_permission` → `False`), y ya integrado en 4 puntos reales del código: apertura de posición, cierre de posición (los 4 escritores reales), rechazo RISK-02, y force-close de admin.

**Esto cambia el trabajo pedido de "diseñar el Audit Trail Engine" a "completar el Audit Trail Engine ya iniciado."** Todo este documento respeta esa realidad: los bloques propuestos se numeran **AUDIT-02 en adelante**, ninguno reemplaza o duplica `BrokerAuditEvent`, `AuditLog` o cualquier otro sistema existente — todos son **extensión aditiva** de lo que ya existe, exactamente como pide la regla de single source of truth.

**Hallazgo más importante de todo el análisis:** `simulator/broker_audit.py` ya define seis categorías (`Category.LEDGER`, `PAYMENTS`, `COMPLIANCE`, `AUTHENTICATION`, `MONITORING`, `SYSTEM`) que **hoy no tiene ningún punto de llamada real** — solo `TRADING`, `RISK` y `ADMIN` están conectados. AUDIT-01 fue diseñado *a propósito* con espacio para este trabajo futuro sin necesidad de tocar el schema. **AUDIT-02 en adelante es, en su mayor parte, un ejercicio de integración sobre una arquitectura que ya lo anticipó — no un rediseño.**

> **Nota de estado (2026-07-21):** AUDIT-02 ya está **implementado y auditado** (pendiente solo de commit). El diseño de AUDIT-02 en §12 queda como registro histórico; el estado real construido — con dos extensiones arquitectónicas que surgieron en la revisión (`correlation_id`, `event_version`) y la resolución final de fail-open — está en **Anexo C**, al final de este documento.

---

## 1. Resumen ejecutivo

- Hoy existen **dos sistemas de audit trail reales y complementarios**, ambos correctos en su alcance declarado: `AuditLog` (Fase B, HTTP-request-scoped: login, depósitos, retiros, 2FA, vistas admin) y `BrokerAuditEvent` (AUDIT-01, cross-engine institucional: trading, riesgo, admin). Ninguno debe fusionarse con el otro — cada uno ya documenta explícitamente por qué existe el otro y qué NO reemplaza.
- Existe además un **tercer mecanismo, no persistente**: `security_log()` (`observability.py`), que escribe únicamente a logs estructurados (JSON opcional) — nunca a base de datos. Varios eventos de seguridad relevantes (`ws.rejected_unauthenticated`, `ratelimit.hit`, `auth.2fa_verified`, `auth.2fa_failed`, `withdrawal.2fa_failed`) **solo existen ahí**: no son consultables, no sobreviven rotación de logs, no tienen índice ni `account`/`user` asociado de forma estructurada.
- **Gaps confirmados con evidencia de código (no suposición):**
  - **Funded payouts** (`funded_payouts.py`, `admin.py::admin_approve_sim_payout/admin_approve_internal_payout`) — **cero** llamadas de auditoría. Dinero real saliendo de la plataforma sin ningún evento cross-cutting, solo el campo `reviewed_by`/`reviewed_at` del propio modelo (última revisión, sin historial).
  - **KYC** (`admin.py::approve_kyc/reject_kyc`) — mismo patrón: cero auditoría cross-cutting, solo `reviewed_by`/`reviewed_at` en `KYCProfile`.
  - **Challenge engine** (`challenge_engine.py`) — **cero** llamadas de auditoría en activación, avance de fase o avance a fondeado.
  - **Rechazos de orden por cuenta individual** (`consumers.py:2616-2627`, guard de margen/balance/whitelist/lot-cap) — solo `log.info("[db_open] REJECTED ...")`, sin fila persistente. Solo el rechazo **RISK-02** (broker-wide) sí genera `BrokerAuditEvent` (línea 2657).
  - **`position.opened` no cubre el motor de población** (`population_engine.py:260`, `Position.objects.create()` directo) — bypassa por completo el único call site de apertura en `consumers.py`.
  - **Cambios de configuración administrativa** (`BrokerSpreadConfig`, `RiskRule`, `FundedConfig`, `ChallengeProduct` vía admin) — sin registro de before/after fuera de lo que Django Admin pudiera loguear automáticamente en el flujo estándar de "Save" (no aplica a las acciones custom por queryset, que no pasan por ese camino).
  - **`EV_DEPOSIT_CREATED` y `EV_DEPOSIT_CALLBACK`** están definidos en `audit.py` pero **nunca se invocan** — solo `EV_DEPOSIT_CREDITED` se llama en la práctica. El momento de creación del depósito y la recepción cruda del webhook no dejan rastro propio.
- **Diseño propuesto:** completar `BrokerAuditEvent` como la única tabla institucional de eventos cross-engine (ya lo es), extendiéndola con: (a) un campo `user` opcional para dominios sin `TradingAccount` (KYC, auth), (b) FKs opcionales adicionales para Deposit/WithdrawalRequest/FundedPayoutRequest/ChallengeEnrollment (búsqueda directa por esas entidades), y (c) wiring de las seis categorías ya definidas y no usadas. Ningún modelo nuevo. Ninguna lógica de trading/ledger/risk se reimplementa — el Audit Trail solo **observa y registra** lo que esos motores ya deciden.
- **Primer bloque recomendado:** **AUDIT-02 — Payments & Payout Audit Trail** (funded payouts + cierre del ciclo de vida de depósitos). Es el gap de mayor severidad real (dinero saliendo de la plataforma sin ningún rastro cross-cutting) y el de menor complejidad de integración (sigue el patrón ya probado de BOOK-02/RISK-02: un único punto de escritura por evento, sin tocar la lógica financiera existente).

---

## 2. Estado actual de trazabilidad

### 2.1 Sistemas existentes (evidencia, no inferencia)

| Sistema | Archivo / Modelo | Alcance declarado | Escritores reales (grep confirmado) |
|---|---|---|---|
| `AuditLog` | `simulator/audit.py` + `models.py:1512` | HTTP-request-scoped: auth, depósitos, retiros, 2FA, admin views | 14 call sites: login success/fail, `EV_ACCOUNT_FUNDED/WITHDRAWN`, `EV_DEPOSIT_CREDITED`, `EV_WITHDRAW_REQUEST/APPROVED/REJECTED/FAILED/REFUNDED/COMPLETE`, `EV_ADMIN_VIEW` (ops panel), `EV_ADMIN_ACTION` (2FA enable/disable) |
| `BrokerAuditEvent` (AUDIT-01) | `simulator/broker_audit.py` + `models.py:2095` | Cross-engine institucional: trading/riesgo/admin lifecycle | 5 rutas reales: `consumers.py:2781` (position opened, solo WS), `broker_ledger.py:95` (position closed — único escritor para los 4 caminos de cierre), `consumers.py:2657` (RISK-02 rechazo, solo WS), `admin.py:762` (force-close), `tasks.py` → `observe_broker_risk_alerts_task` → `observe_broker_alerts()` (RISK-03, solo CRITICAL/HIGH) |
| `TradingViolation` | `risk_engine.py` + `models.py:1133` | Per-account: breach de `RiskRule` (drawdown, daily loss, lot size, exposure, rate limit, martingale) | `risk_engine.py` (`evaluate_position_risk`, `check_equity_stopout`) |
| `BrokerLedger` (BOOK-02) | `broker_ledger.py` + `models.py:378` | Contabilidad del bróker (Decimal): COMMISSION, SPREAD, CHALLENGE_FEE, WITHDRAW_FEE, COUNTERPARTY_PNL | No es un log de eventos — es el ledger contable en sí |
| `LedgerEntry` | `models.py:290` | Ledger general del trader (por `TradingAccount`) | Todo el motor de trading |
| `WalletTransaction` | `wallet_ledger.py` + `models.py:1024` | Ledger de wallet, append-only por diseño propio (invariante `SUM == available_balance`) | `credit_wallet()`/`debit_wallet()` únicamente |
| `security_log()` | `observability.py` | **Solo logging estructurado — no persiste a DB** | `ratelimit.py`, `consumers.py:534`, `views.py` (login, 2FA, withdrawal 2FA) |
| Campos `reviewed_by`/`reviewed_at` | `WithdrawalRequest`, `KYCProfile`, `FundedPayoutRequest` | Último revisor/momento — **no histórico**, se sobreescribe en cada cambio de estado | Las propias vistas/acciones admin que mutan esos modelos |

### 2.2 Qué NO se toca (regla de single source of truth, ya respetada por AUDIT-01 y que este documento hereda sin excepción)

- `BrokerLedger` sigue siendo el único sistema de verdad para montos contables del bróker.
- `LedgerEntry`/`WalletTransaction` siguen siendo los únicos ledgers financieros del trader/wallet.
- `TradingViolation` sigue siendo el registro de compliance per-account de `RiskRule`.
- `AuditLog` sigue siendo el sistema de record para eventos HTTP-request-scoped ya cubiertos.
- Ningún bloque de este plan **recalcula** PnL, exposición, margen, spread o comisión — solo **observa** decisiones ya tomadas por los motores existentes (`risk_engine.py`, `broker_risk.py`, `challenge_engine.py`, `funded_payouts.py`, `nowpayments.py`) y las registra.

---

## 3. Inventario de eventos (por dominio pedido)

Leyenda: 🟢 = trazabilidad completa y consultable · 🟡 = solo log (no persistente/consultable) · 🔴 = sin rastro alguno (ni log distintivo) · ⚪ = parcial (campo de última revisión en el propio modelo, sin historial)

| Dominio | Evento | Estado | Evidencia |
|---|---|---|---|
| **Posiciones** | Apertura (WebSocket) | 🟢 | `consumers.py:2781` → `BrokerAuditEvent` |
| | Apertura (population engine / simulación) | 🔴 | `population_engine.py:260`, sin llamada a `record_trade_event` |
| | Cierre (WS, daemon, force-close, población) | 🟢 | `broker_ledger.py:95`, único escritor, cubre los 4 caminos |
| | Rechazo por guard per-account (margen/balance/whitelist/lote) | 🟡 | `consumers.py:2620` solo `log.info` |
| | Rechazo por RISK-02 (broker-wide) | 🟢 | `consumers.py:2657` |
| **Balance / Ledger** | `LedgerEntry` (apertura/cierre/ajustes) | 🟢 (como ledger, no como "evento" cross-cutting) | `models.py:290`, escrito en todo el motor |
| | `BrokerLedger` COUNTERPARTY_PNL | 🟢 | Genera además `BrokerAuditEvent` (única fila con doble registro) |
| | `BrokerLedger` COMMISSION/SPREAD/CHALLENGE_FEE/WITHDRAW_FEE | ⚪ | Se persisten como ledger, pero **no** generan `BrokerAuditEvent` propio — solo la fila COUNTERPARTY_PNL lo hace |
| **Depósitos** | Creación (`Deposit` PENDING) | 🔴 | `EV_DEPOSIT_CREATED` definido en `audit.py`, nunca invocado |
| | Callback IPN recibido (crudo) | 🔴 | `EV_DEPOSIT_CALLBACK` definido, nunca invocado |
| | Acreditado (`credit_wallet`) | 🟢 | `views.py:1800` → `AuditLog` |
| **Retiros** | Solicitud | 🟢 | `AuditLog` |
| | Aprobado / rechazado por staff | 🟢 | `AuditLog` (`admin.py:1501/1579`) — pero **sin antes/después** del resto del `WithdrawalRequest` |
| | Callback / completado / fallido / reembolsado | 🟢 | `AuditLog` |
| | 2FA fallido en el flujo de retiro | 🟡 | `security_log("withdrawal.2fa_failed")`, solo log |
| **Entradas BrokerLedger** | Ver fila "Balance/Ledger" arriba | ⚪ | Solo COUNTERPARTY_PNL genera evento cross-cutting |
| **Eventos de riesgo** | `TradingViolation` (per-account) | 🟢 (como su propio sistema) | No integrado al timeline unificado de `BrokerAuditEvent` — es una fuente separada, correctamente, por diseño de AUDIT-01 |
| | RISK-02 rechazo broker-wide | 🟢 | Ver arriba |
| | RISK-03 alertas CRITICAL/HIGH | 🟢 | `observe_broker_alerts()`, deduplicado 900 s |
| | RISK-03 alertas INFO/WARNING/MEDIUM/LOW | 🔴 (por diseño explícito, FASE 6 de AUDIT-01: "solo eventos críticos") | `broker_audit.py:199-205` |
| **Alertas RISK-03** | Ver arriba | 🟢/🔴 mixto por severidad | — |
| **Cambios administrativos** | Force-close de posición | 🟢 | `admin.py:762` |
| | Aprobación/rechazo KYC | 🔴 | Solo `reviewed_by`/`reviewed_at` en el modelo (⚪), sin evento |
| | Aprobación de payout FUNDED_SIM/FUNDED_INTERNAL | 🔴 | `funded_payouts.py`, cero llamadas de auditoría |
| | Edición de `BrokerSpreadConfig`/`RiskRule`/`FundedConfig`/`ChallengeProduct` | 🔴 (fuera del posible LogEntry automático de Django, que no aplica a acciones custom por queryset) | Sin wrapper de auditoría propio |
| | Cambios de soporte (mark_pending/resolved/closed) | 🔴 (severidad baja, no financiero) | `admin.py` |
| **Acciones de staff** | Ver "cambios administrativos" | mixto | — |
| **KYC** | Ver arriba | 🔴/⚪ | — |
| **2FA** | Enable/disable (self-service) | 🟢 | `AuditLog` vía `EV_ADMIN_ACTION` (nombre de evento cuestionable — no es una acción "admin", es self-service; ver Anexo de nomenclatura §12) |
| | Verify success/fail | 🟡 | `security_log()` solamente |
| | Disable de emergencia (management command) | 🟢 | `disable_2fa.py` escribe `AuditLog` directamente (único management command que sí audita) |
| **Cambios de configuración** | Ver "cambios administrativos" | 🔴 | — |
| **Force close** | Ver arriba | 🟢 | Doble evento correcto (financiero + administrativo), ya documentado como decisión deliberada en `broker_audit.py` |
| **Payouts** | Ver "Aprobación de payout" | 🔴 | — |
| **Eventos de challenge** | Activación, avance Fase 2, avance a Funded | 🔴 | `challenge_engine.py`, cero llamadas |
| **Errores/rechazos WS** | Rechazo no autenticado | 🟡 | `security_log("ws.rejected_unauthenticated")` |
| | Rechazo de orden per-account | 🟡 | Ver "Posiciones" arriba |
| | Rate limit hit | 🟡 | `security_log("ratelimit.hit")` |
| **Eventos de seguridad** | Login éxito/fallo | 🟢 (doble: `AuditLog` + `security_log`) | Redundante mas no dañino — ambos sistemas documentan su propio alcance |
| | 2FA verify éxito/fallo | 🟡 | Solo `security_log()` |

---

## 4. Gaps encontrados (síntesis priorizada)

1. **Payouts de fondeo sin ningún rastro cross-cutting** — dinero real saliendo de la plataforma. **Severidad: Alta.**
2. **KYC sin rastro cross-cutting** — decisión de compliance sin timeline consultable junto al resto de la actividad de la cuenta. **Severidad: Alta** (compliance).
3. **Rechazos de orden per-account (el 90%+ de los rechazos reales en volumen) solo en logs** — un disputa de cliente ("¿por qué se rechazó mi orden?") no puede resolverse desde la base de datos, solo desde logs de servidor que pueden rotar. **Severidad: Media-Alta.**
4. **Challenges sin ningún rastro** — activación/fase/fondeado son eventos de negocio significativos (dinero + creación de cuenta) sin timeline. **Severidad: Media-Alta.**
5. **Cambios de configuración administrativa sin before/after** — un cambio de `BrokerSpreadConfig.spread_pips` o `RiskRule.max_lot_size` altera economía/riesgo en vivo sin dejar quién/cuándo/qué-valor-antes fuera del propio log de servidor. **Severidad: Media.**
6. **`position.opened` no cubre `population_engine.py`** — inconsistencia menor (el motor de población es tráfico sintético de stress-test, no usuarios reales), pero rompe la garantía de "todo open real registrado" si alguna vez se usa población en un entorno con datos mixtos. **Severidad: Baja.**
7. **Ciclo de vida de depósito incompleto** (`EV_DEPOSIT_CREATED`/`EV_DEPOSIT_CALLBACK` muertos) — dificulta reconstruir una disputa de pago desde el momento de creación, no solo desde el acreditado. **Severidad: Media.**
8. **Eventos de autenticación/2FA de bajo nivel (verify success/fail, rate limit) solo en logs** — útil para forense de seguridad, hoy no sobrevive rotación de logs ni es consultable por cuenta/usuario. **Severidad: Media** (seguridad, no financiero).
9. **`BrokerAuditEvent` no tiene campo `user`** — todo lo anterior (KYC, auth, payouts a veces) se ancla a `User`, no siempre a `TradingAccount`. El modelo actual no puede representarlo sin forzar un FK que no aplica. **Gap de schema, no de comportamiento — bloqueante para AUDIT-02 en adelante.**
10. **Sin búsqueda directa por depósito/retiro/orden** — `BrokerAuditEvent` indexa `account`, `trade`, `symbol`, `category`, `severity`, pero no `Deposit`/`WithdrawalRequest`/`ChallengeEnrollment`/`FundedPayoutRequest`. Cumplir el requisito del usuario ("buscar por depósito y retiro") requiere extender el modelo o usar `metadata__contains` (funciona en Postgres, más lento, sin índice dedicado).

---

## 5. Arquitectura propuesta

```
                     ┌─────────────────────────────────────────────┐
                     │   Motores de negocio (sin cambios de lógica)  │
                     │   risk_engine · broker_risk · challenge_engine │
                     │   funded_payouts · nowpayments · admin actions │
                     │   consumers.py · tasks.py · population_engine  │
                     └───────────────────┬───────────────────────────┘
                                         │  llamadas aditivas, nunca condicionan
                                         │  el resultado de la operación de negocio
                                         ▼
                     ┌─────────────────────────────────────────────┐
                     │        simulator/broker_audit.py (AUDIT-01+)   │
                     │  record_event() — único escritor real          │
                     │  record_trade_event / record_risk_event /      │
                     │  record_admin_event / record_system_event /    │
                     │  record_payment_event (nuevo) /                │
                     │  record_compliance_event (nuevo) /              │
                     │  record_auth_event (nuevo)                      │
                     │  — todos wrappers delgados sobre record_event() │
                     └───────────────────┬───────────────────────────┘
                                         │  transaction.atomic() anidado
                                         │  (savepoint — nunca envenena la
                                         │   transacción externa)
                                         ▼
                     ┌─────────────────────────────────────────────┐
                     │           BrokerAuditEvent (única tabla)       │
                     │   + user (nuevo, opcional)                     │
                     │   + deposit / withdrawal_request /             │
                     │     funded_payout_request / challenge_enrollment│
                     │     (nuevos, opcionales, uno por bloque)        │
                     └───────────────────┬───────────────────────────┘
                                         │  solo lectura
                                         ▼
              ┌──────────────────────────┴───────────────────────────┐
              │                                                        │
   Django Admin (BrokerAuditEventAdmin,             Futuros: Dealing Desk híbrido,
   ya append-only — extender filtros/search)         Compliance, BOOK-04, Liquidity,
                                                       Treasury (todos CONSUMIDORES,
                                                       ninguno productor)
```

**Principio rector:** el Audit Trail Engine tiene **una tabla de escritura** (`BrokerAuditEvent`) con **un único writer de bajo nivel** (`record_event()`), y N wrappers delgados por categoría (patrón ya establecido en AUDIT-01: `record_trade_event`, `record_risk_event`, `record_admin_event`, `record_system_event`). AUDIT-02+ añade wrappers nuevos (`record_payment_event`, `record_compliance_event`, `record_auth_event`) **sin tocar `record_event()` en su forma actual**, salvo la extensión de firma para los nuevos campos opcionales (retrocompatible — todo parámetro nuevo con default `None`).

`AuditLog` permanece sin cambios — sigue siendo el sistema de record de su dominio HTTP-request-scoped ya cubierto. Este plan no le agrega eventos nuevos ni le quita los actuales.

---

## 6. Modelo de datos propuesto

**Ninguna tabla nueva.** Extensión incremental de `BrokerAuditEvent` vía migraciones aditivas pequeñas (una por bloque que la necesite, nunca una migración monolítica):

```python
class BrokerAuditEvent(models.Model):
    # ... campos existentes sin cambios ...

    # AUDIT-02+ — nuevo, nullable, no rompe ninguna fila existente
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )  # ancla eventos que no tienen (o no todavía) un TradingAccount: KYC, auth, payout

    # AUDIT-02 — búsqueda directa por depósito/retiro/payout
    deposit = models.ForeignKey(
        "Deposit", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    withdrawal_request = models.ForeignKey(
        "WithdrawalRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    funded_payout_request = models.ForeignKey(
        "FundedPayoutRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # AUDIT-07 (challenges) — si AUDIT-05 (rechazos) y AUDIT-03 (KYC) no
    # necesitan más FKs, este es el único pendiente adicional
    challenge_enrollment = models.ForeignKey(
        "ChallengeEnrollment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
```

**Por qué FKs explícitos y no un patrón genérico (`ContentType` + `object_id`):**
- Consistente con el estilo ya establecido del modelo (`account`, `trade` ya son FKs explícitos, no genéricos).
- Cada FK nuevo es **opcional y aditivo** — una migración pequeña por bloque, revisable en un PR de tamaño razonable (evita la migración monolítica que el propio `docs/PROJECT_STATUS_2026.md` ya señaló como patrón de riesgo — "god files").
- Permite índices dedicados por entidad (`models.Index(fields=["deposit", "-timestamp"])`, etc.), igual que ya existe para `account`/`trade`/`symbol`.
- Evita la complejidad y el costo de query de un `GenericForeignKey` real, que además complica los `has_delete_permission=False` y los `SET_NULL` que ya están cuidadosamente decididos en el modelo actual.

**Sin campos nuevos para AUDIT-05 (rechazos) ni AUDIT-06 (config admin)** — ambos reutilizan `account`/`metadata` (rechazos) o `metadata` con un `content_type`/`object_repr` ligero dentro del JSON (cambios de config, ver §8.6) — no requieren FK dedicado.

**Nada se vuelve obligatorio.** Todo campo nuevo es `null=True, blank=True` — ninguna fila histórica de AUDIT-01 se ve afectada, y cada wrapper nuevo simplemente no pasa los campos que no aplican.

---

## 7. Servicio de escritura

Extiende `record_event()` (broker_audit.py) con parámetros nuevos, todos opcionales, todos con el mismo contrato de **nunca lanzar excepción**:

```python
def record_event(
    *,
    event_type: str, category: str, severity: str, actor_type: str, description: str,
    actor_id: Optional[int] = None,
    account_id: Optional[int] = None, account=None,
    trade_id: Optional[int] = None, trade=None,
    user_id: Optional[int] = None, user=None,                    # NUEVO
    deposit_id: Optional[int] = None, deposit=None,               # NUEVO (AUDIT-02)
    withdrawal_request_id: Optional[int] = None, withdrawal_request=None,  # NUEVO (AUDIT-02)
    funded_payout_request_id: Optional[int] = None, funded_payout_request=None,  # NUEVO (AUDIT-02)
    challenge_enrollment_id: Optional[int] = None, challenge_enrollment=None,    # NUEVO (AUDIT-07)
    symbol: str = "", metadata: Optional[dict] = None,
    source_module: str = "", request=None, request_id: Optional[str] = None,
    before: Optional[dict] = None, after: Optional[dict] = None,   # NUEVO — ver §9
):
    ...
```

**Antes/después (`before`/`after`):** no son columnas nuevas — se guardan **dentro de `metadata`** como `{"before": {...}, "after": {...}}` cuando el llamador los provee. No se añade un campo dedicado porque su forma varía completamente por dominio (un cambio de `RiskRule` no tiene el mismo shape que un cambio de estado de `FundedPayoutRequest`) — `metadata` (JSONField) ya es exactamente el mecanismo que el modelo usa para esto, consistente con cómo `record_alert_event()` ya guarda `alert_id`/`metric`/`current_value`/`threshold` sin campos dedicados.

**Nuevos wrappers delgados** (mismo patrón que `record_trade_event`/`record_risk_event`):

```python
def record_payment_event(*, event_type, severity=Severity.INFO, description, ...):
    """Category.PAYMENTS — depósitos, retiros, payouts de fondeo."""

def record_compliance_event(*, event_type, severity=Severity.WARNING, description, ...):
    """Category.COMPLIANCE — KYC y futuras verificaciones regulatorias."""

def record_auth_event(*, event_type, severity=Severity.INFO, description, ...):
    """Category.AUTHENTICATION — 2FA, sesiones, eventos de seguridad seleccionados."""
```

Cada uno simplemente fija `category=Category.PAYMENTS/COMPLIANCE/AUTHENTICATION` y delega en `record_event()` — cero lógica nueva de escritura, exactamente el patrón ya validado por 68 tests en AUDIT-01.

**Contrato invariante que se hereda sin cambios:**
- Nunca lanza — cualquier excepción se loguea y se traga (`log.error(..., exc_info=True)`, `return None`).
- Escribe dentro de su propio `transaction.atomic()` anidado (savepoint) — nunca envenena la transacción externa del llamador.
- `request_id` se resuelve automáticamente desde el contexto de request/Celery si no se pasa explícito (ya implementado, reutilizado sin cambios).

---

## 8. Estrategia de integración por dominio

Principio para los ocho dominios: **el Audit Trail nunca decide, nunca valida, nunca bloquea — solo observa el resultado ya decidido por el motor de negocio y lo registra después (o alrededor) del hecho, dentro de la misma transacción cuando sea seguro.**

### 8.1 Payments (depósitos, retiros, payouts) — AUDIT-02
- `funded_payouts.py::approve_sim_payout()` / `approve_internal_payout()` — agregar `record_payment_event()` al final de cada función, **dentro** de la misma `transaction.atomic()` que ya envuelve la aprobación (igual que BOOK-02 hace con el cierre de Trade). Actor: `STAFF`, `actor_id=approved_by.pk` (el parámetro `request.user` que ya reciben ambas funciones).
- `views.py::deposit_view` / `deposit_callback` — invocar los `EV_DEPOSIT_CREATED`/`EV_DEPOSIT_CALLBACK` de `audit.py` que ya existen pero nunca se llaman (esto es trabajo de **AuditLog**, no de `BrokerAuditEvent` — mismo sistema, solo activar constantes ya definidas). Opcionalmente, espejar el evento crítico (`deposit.credited`) también en `BrokerAuditEvent` vía `record_payment_event()` para que aparezca en el timeline institucional unificado junto a trading/riesgo — **decisión a confirmar con el usuario antes de implementar**, ya que introduce una duplicación deliberada (como ya existe para force-close).

### 8.2 Compliance (KYC) — AUDIT-03
- `admin.py::approve_kyc()` / `reject_kyc()` — agregar `record_compliance_event()` con `before={"status": "PENDING"}`, `after={"status": "APPROVED"/"REJECTED"}`, `actor_id=request.user.pk`. Sin tocar el envío de email existente (`kyc_emails.py`) ni el guardado del modelo.

### 8.3 Authentication (2FA, sesiones) — AUDIT-04
- Persistir selectivamente los eventos que hoy solo pasan por `security_log()` y tienen valor operativo/forense duradero: `auth.2fa_verified` (fallos repetidos = señal de cuenta comprometida), `withdrawal.2fa_failed`. **No** persistir `ratelimit.hit` ni `ws.rejected_unauthenticated` en `BrokerAuditEvent` — son ruido de volumen alto (potencialmente cientos por minuto bajo un scan automatizado) sin valor de reconstrucción de una operación; permanecen correctamente como logs. Este es el ejemplo más claro de "no todo log-only es un gap" — el gap es solo donde la ausencia de persistencia impide reconstruir una operación real de negocio.

### 8.4 Trading rejections & population engine — AUDIT-05
- `consumers.py:2616-2627` (guard per-account) — agregar `record_risk_event()` (reutilizando la categoría RISK ya existente, no una nueva) justo antes del `return`, con `actor_type=ActorType.SYSTEM` (es el guard el que decide, no el trader) y `metadata={"error_code": guard["error_code"], ...}`. Mismo patrón exacto que el rechazo RISK-02 once líneas más abajo — este bloque literalmente completa una asimetría que ya existe en el mismo archivo.
- `population_engine.py:260` — agregar la misma llamada `record_trade_event(EV_POSITION_OPENED, actor_type=ActorType.SYSTEM, source_module="simulator.population_engine")` que `consumers.py` ya hace, para simetría real entre apertura y cierre (el cierre ya está unificado vía `broker_ledger.py`).

### 8.5 Challenge lifecycle — AUDIT-07 (orden tentativo, puede adelantarse si el usuario lo prioriza)
- `challenge_engine.py::activate_challenge_enrollment()` / `advance_to_phase2()` / `advance_to_funded()` — un `record_event()` por transición, categoría `TRADING` (es progresión de una cuenta de trading) o `PAYMENTS` cuando involucra el `challenge_enrollment_id` nuevo. A definir con el usuario cuál encaja mejor semánticamente antes de implementar — no es una decisión técnica, es de taxonomía del negocio.

### 8.6 Administrative configuration changes — AUDIT-06
- Wrapper genérico `record_admin_config_change(model_instance, changed_fields: dict, request)` que arma `before`/`after` a partir de los campos realmente modificados (no el objeto completo — evita guardar campos irrelevantes o sensibles) y llama `record_admin_event()`. Se integra en el `save_model()` de los `ModelAdmin` de `BrokerSpreadConfig`, `RiskRule`, `FundedConfig`, `ChallengeProduct` — el único punto de integración transversal a los cuatro (Django ya expone `save_model(self, request, obj, form, change)` con acceso a `form.changed_data`).

### 8.7 Qué partes del sistema **solo consumen** (nunca emiten)
- `admin.py::BrokerAuditEventAdmin` (ya read-only, se extiende con más `search_fields`/`list_filter`, nunca con capacidad de escritura).
- El futuro **Dealing Desk híbrido A/B-book** (lee `BrokerAuditEvent` para reconstruir por qué una orden se enrutó de cierta forma — no le corresponde escribir eventos de auditoría de dominios que no le pertenecen).
- **BOOK-04 Routing** y **Liquidity Engine** — consumidores puros del timeline para post-mortems de ruteo; sus propios eventos de negocio (decisión de ruteo, ejecución en LP) se auditarán en *sus propios* bloques BOOK-04-AUDIT-XX cuando existan, no en este documento.
- **Treasury** (futuro, hoy no existe) — consumidor natural del timeline de payments una vez exista.
- Cualquier vista de solo lectura (`broker_monitoring_view`, `snapshots_view`, dashboards de staff).

### 8.8 Qué partes **emiten** (productores)
`consumers.py`, `tasks.py`, `admin.py` (acciones custom), `funded_payouts.py`, `challenge_engine.py`, `population_engine.py`, y las vistas HTTP relevantes de `views.py` — exactamente los mismos módulos que ya son productores hoy, extendidos con las llamadas nuevas de §8.1-8.6.

### 8.9 Dependencias del Audit Trail Engine
- **Ninguna dependencia nueva de infraestructura.** Sigue usando PostgreSQL (o SQLite en dev) como hoy, sin Redis, sin Celery propio (el único uso de Celery — `observe_broker_risk_alerts_task` — ya existe y no cambia).
- Depende de que cada motor de negocio (`funded_payouts.py`, `challenge_engine.py`, etc.) exponga el objeto/actor necesario en el punto de integración — ninguno requiere cambio de firma pública, solo una llamada adicional al final de una función ya `transaction.atomic()`.
- Depende de `simulator.observability.get_request_id()` para correlación (ver §9) — ya existe, sin cambios.

---

## 9. Seguridad y privacidad

- **Correlation ID:** `request_id` ya existe en el modelo y se resuelve automáticamente vía `RequestIDMiddleware` (`X-Request-ID` de vuelta al cliente) + `simulator.observability.set_request_id()`/`get_request_id()` (thread-local). Para Celery, `tasks.py` ya opera sin `request` (pasa `request_id=""` implícitamente) — **recomendación para AUDIT-02+:** generar un `request_id` sintético al inicio de cada tarea Celery relevante (`f"celery:{task.request.id}"`) para que los eventos que un task produce (ej. `observe_broker_risk_alerts_task`, futuras tareas de payout) sean correlacionables entre sí sin depender de un request HTTP. Esto no es una `Signal` de Django ni un side effect oculto — es un valor explícito pasado como parámetro.
- **Trazabilidad end-to-end de una operación:** hoy ya es parcialmente posible vía `trade_id`/`account_id` (ej. `events_for_trade()`). Con los FKs nuevos de §6, se extiende a `deposit_id`/`withdrawal_request_id`/`funded_payout_request_id`/`challenge_enrollment_id` — cada uno con su propio `events_for_X()` en `broker_audit.py`, mismo patrón que los cinco ya existentes.
- **Qué NO debe guardarse jamás** (regla explícita para todo bloque futuro, no negociable):
  - Contraseñas, tokens TOTP (ni siquiera cifrados), claves de API, secretos de webhook, JWT de NOWPayments.
  - Direcciones de wallet completas sin enmascarar (reutilizar `_mask_wallet()` de `views.py:53`, ya existente, para cualquier `metadata` que incluya una dirección cripto).
  - Documentos de KYC en sí (imágenes/PDFs) — el evento de compliance registra que una revisión ocurrió, `status` antes/después y quién la hizo, **nunca** el contenido del documento ni una URL directa al archivo en `media/`.
  - Cualquier PII no ya expuesta en otros lugares consultables por el mismo staff (nombre completo, país y tipo de documento **si y solo si** ya son visibles para ese mismo staff en `KYCProfileAdmin` — no expandir la superficie de exposición más allá de lo que el admin ya muestra).
  - El cuerpo completo de un webhook (`deposit_callback`/`withdraw_payout_callback`) — solo campos ya validados y necesarios (IDs, montos, estado), nunca el payload crudo con posibles headers/firmas.
- **Fail-open vs fail-closed:**
  - **Regla por defecto, heredada sin excepción de AUDIT-01/BOOK-02/RISK-02:** el Audit Trail Engine es **fail-open para toda escritura de auditoría** — un fallo al escribir un `BrokerAuditEvent` nunca bloquea, revierte ni retrasa la operación financiera o administrativa que lo originó. Esto es coherente con la filosofía ya documentada en tres módulos distintos del proyecto y no debe romperse solo porque el dominio sea "compliance" o "payout".
  - **Única excepción a evaluar (no decidida en este documento, requiere decisión explícita del usuario antes de implementar):** para la aprobación de un **payout de fondeo** específicamente, podría justificarse un **soft fail-closed operativo** — no revertir la transacción de negocio (eso seguiría siendo fail-open a nivel de DB), sino **bloquear el siguiente paso externo** (la llamada real a NOWPayments en `approve_internal_payout`) si el registro de auditoría de la aprobación no pudo escribirse, con un mensaje claro al staff ("aprobado en DB pero no se pudo auditar — reintente antes de que el dinero salga"). Esto es una decisión de negocio/compliance, no una decisión técnica — se deja explícitamente pendiente de aprobación antes de tocar código.
  - Todo lo demás (trading, riesgo, KYC, auth) permanece **fail-open puro**, sin excepción — un evento de auditoría perdido en estos dominios es recuperable por reconciliación posterior; un payout real revertido o duplicado por una excepción de auditoría mal manejada sería mucho peor que el gap que se intenta cerrar.
- **No introducir señales Django (`post_save`, etc.):** todas las integraciones de §8 son **llamadas explícitas** al final de una función ya existente, nunca un signal receiver. Esto es deliberado — un signal engancharía la escritura de auditoría a *cualquier* `.save()` de los modelos afectados, incluyendo llamadas desde shell, scripts de management, o migraciones de datos, con efectos secundarios impredecibles exactamente del tipo que el usuario pidió evitar ("no introducir señales Django peligrosas... sin justificación").
- **Lock order:** `record_event()` **nunca adquiere un lock de negocio** (no toca `BrokerRiskLock`, no hace `select_for_update()` sobre `TradingAccount`/`Position`) — solo su propio savepoint interno más, cuando corresponde (dedup de alertas), `BrokerAuditObservationLock`, que ya está diseñado para adquirirse **fuera** de cualquier transacción de trading (solo desde el task periódico de Celery). Ningún bloque de AUDIT-02+ debe cambiar esto: toda integración nueva llama a `record_event()`/wrappers **después** de que el motor de negocio ya adquirió y liberó (o sigue sosteniendo, en el mismo savepoint anidado) sus propios locks en el orden ya documentado (`consumers.py`, sección "LOCK ORDER"; `broker_risk.py:54`; `tasks.py:415`). Esto ya es cierto para las 4 integraciones actuales de AUDIT-01 y se mantiene como regla dura para todas las nuevas.
- **PostgreSQL:** todo el diseño (JSONField, índices compuestos, `select_for_update()` singleton) ya es 100% compatible con PostgreSQL en producción — son los mismos mecanismos que AUDIT-01/RISK-02 ya usan ahí. `metadata__alert_id` (JSON key lookup, usado en el dedup) es una consulta válida en PostgreSQL; en SQLite (dev) funciona de forma más limitada pero suficiente para tests (ya probado por los 68 tests existentes).

---

## 10. Estrategia de consulta y admin

- **Extender `BrokerAuditEventAdmin`** (no crear un segundo admin): agregar a `search_fields` los nuevos FKs (`deposit__id`, `withdrawal_request__id`, `funded_payout_request__id`, `user__username`) y a `list_filter` un filtro por dominio si el volumen de categorías lo justifica.
- **Nuevas funciones de consulta en `broker_audit.py`** (mismo patrón que los 6 `events_for_X()`/`events_by_X()` ya existentes, sin abstraerlos prematuramente en un helper genérico — tres líneas repetidas por función son preferibles a una capa de indirección para seis casos):
  ```python
  def events_for_user(user_id, limit=50): ...
  def events_for_deposit(deposit_id, limit=50): ...
  def events_for_withdrawal(withdrawal_request_id, limit=50): ...
  def events_for_funded_payout(funded_payout_request_id, limit=50): ...
  ```
- **Compatibilidad con el futuro Dealing Desk híbrido:** el Dealing Desk (bloque 4 del roadmap oficial) necesitará, por su propia naturaleza (decisiones de ruteo A-book/B-book en tiempo real), consultar "¿qué pasó con esta cuenta/orden en los últimos N minutos?" — exactamente la forma de `events_for_account()`/`events_for_trade()` ya existente. No se requiere ningún cambio para soportarlo; es un consumidor más.
- **No se propone una API REST nueva para el audit trail en este documento** — el patrón actual (funciones Python + Django Admin) cubre el caso de uso de staff. Si Compliance o un futuro Dealing Desk necesitan acceso programático externo, eso es una decisión de alcance separada, a evaluar cuando ese consumidor exista realmente (evitar construir por adelantado sin necesidad confirmada, consistente con la regla general del proyecto de "no features especulativas").

---

## 11. Estrategia de pruebas

Seguir el mismo molde que `simulator/tests/test_broker_audit_trail.py` (68 tests) ya estableció para AUDIT-01, replicado por bloque:

1. **Tests unitarios del wrapper nuevo** (`record_payment_event`, `record_compliance_event`, `record_auth_event`): categoría correcta, severidad por defecto correcta, nunca lanza ante un `Exception` simulado dentro del bloque `try` (mockear `BrokerAuditEvent.objects.create` para forzar el fallo y verificar que la función llamante no revierte ni lanza).
2. **Tests de integración por punto de llamada** (uno por integración de §8): ej. "aprobar un FUNDED_SIM payout crea exactamente un `BrokerAuditEvent` con `category=PAYMENTS`, `actor_id=<staff>`, `funded_payout_request=<fpr>`" — reutilizando fixtures ya existentes de `test_funded_payout_*` si existen, o creando las mínimas necesarias.
3. **Tests de no-duplicación** (idempotencia): replicar el patrón ya usado para el dedup de alertas (`test_broker_audit_trail.py`) en cualquier integración que pueda re-ejecutarse (ej. un retry de `approve_internal_payout` no debe crear un segundo evento para la misma aprobación — usar `get_or_create` con una clave natural, igual que BOOK-02 hace con `(source_trade, revenue_type)`).
4. **Tests de fail-open**: forzar una excepción en la escritura del evento y confirmar que la operación de negocio (aprobar KYC, aprobar payout, abrir posición) se completa igual y su propio estado queda correcto — el test más importante de todos, ya que es la garantía central que el usuario pide explícitamente ("no bloquear silenciosamente operaciones financieras por fallos del audit").
5. **Tests de privacidad**: para cada integración nueva, un test que arma el `metadata` esperado y confirma **por lista blanca** (no por lista negra) qué claves están presentes — falla si aparece cualquier clave no explícitamente esperada, para atrapar una fuga accidental de un campo sensible (dirección de wallet sin enmascarar, token, etc.) antes de que llegue a producción.
6. **Tests de lock order**: para las integraciones que ocurren dentro de un bloque ya bajo `BrokerRiskLock`/`select_for_update()` (rechazo per-account, apertura de población), confirmar que la llamada de auditoría no introduce una adquisición de lock adicional (se puede verificar indirectamente corriendo el test bajo `TransactionTestCase` con dos hilos concurrentes, replicando el estilo que `test_broker_audit_trail.py` ya usa para el dedup de `BrokerAuditObservationLock`).
7. **No se requiere ningún test nuevo de infraestructura** (Postgres-specific, Celery-specific) — la infraestructura de test ya cubre SQLite/dev; el comportamiento JSON-específico de Postgres en producción ya está validado por los 68 tests de AUDIT-01 corriendo hoy contra el mismo esquema de modelo.

---

## 12. Bloques AUDIT-02 → AUDIT-08

> AUDIT-01 (`broker-event-audit-foundation-audit01`) ya está implementado y en producción de código — no se repite aquí. Los bloques siguientes son el trabajo de análisis/diseño pedido, listos para implementación futura bloque por bloque, en el orden recomendado del §15.

### AUDIT-02 — Payments & Payout Audit Trail — ✅ IMPLEMENTADO (2026-07-21, pendiente de commit)

> El diseño original de este bloque queda abajo sin editar, como registro histórico de la decisión. El **estado as-built exacto** — incluyendo dos extensiones que no estaban en este diseño original (`correlation_id`, `event_version`) y la resolución final de la decisión fail-open/fail-closed — está documentado íntegro en **Anexo C**, que tiene autoridad sobre cualquier discrepancia con el texto de abajo.

- **Objetivo:** cerrar el gap de mayor severidad real — payouts de fondeo sin ningún rastro, y ciclo de vida de depósito incompleto.
- **Alcance exacto:** agregar `record_payment_event()` (wrapper nuevo) + campos `user`, `deposit`, `withdrawal_request`, `funded_payout_request` al modelo. Integrar en `funded_payouts.py::approve_sim_payout/approve_internal_payout` y activar `EV_DEPOSIT_CREATED`/`EV_DEPOSIT_CALLBACK` ya definidos en `audit.py`.
- **Archivos existentes que tocaría:** `simulator/broker_audit.py`, `simulator/models.py` (migración aditiva), `simulator/funded_payouts.py`, `simulator/views.py` (activar constantes muertas), `simulator/admin.py` (extender `BrokerAuditEventAdmin.search_fields`).
- **Archivos nuevos:** ninguno — todo vive en los módulos existentes, siguiendo el patrón AUDIT-01.
- **Modelos/servicios necesarios:** extensión de `BrokerAuditEvent` (FKs nuevos), `record_payment_event()` en `broker_audit.py`.
- **Eventos cubiertos:** `payment.payout_approved_sim`, `payment.payout_approved_internal`, `deposit.created`, `deposit.callback_received` (mapeando 1:1 a las constantes ya definidas en `audit.py` para el sistema `AuditLog`, y nuevos equivalentes en `Category.PAYMENTS` para `BrokerAuditEvent` cuando el evento amerite timeline institucional).
- **Riesgos:** ninguno a la lógica de payout (aditivo puro); riesgo de diseño en la decisión fail-open/fail-closed del payout específicamente (§9), a resolver con el usuario antes de codear. **Resuelto — ver Anexo C: fail-open puro, sin excepción.**
- **Dependencias:** ninguna nueva; reutiliza `funded_payouts.py`, `nowpayments.py`, `views.py` sin modificarlos más allá de la llamada nueva.
- **Estrategia de tests:** ver §11 puntos 1-4, con foco en fail-open del payout.
- **Criterios de aceptación:** (a) toda aprobación de payout (SIM e INTERNAL) genera exactamente un `BrokerAuditEvent` con `category=PAYMENTS`, `actor_id` del staff real; (b) un fallo simulado en la escritura del evento no revierte la aprobación en DB (test explícito); (c) `EV_DEPOSIT_CREATED`/`EV_DEPOSIT_CALLBACK` se invocan y son consultables por `request_id`; (d) 0 regresiones en la suite completa (2892 tests existentes deben seguir en verde). **Cumplido — ver Anexo C.**
- **Complejidad estimada:** Media (una migración pequeña + ~4 puntos de integración + tests). **Real: 8 puntos de integración (más que los ~4 estimados — ver Anexo C §1), complejidad final Media-Alta.**
- **Orden recomendado:** **1º** (ver §15).

### AUDIT-03 — Compliance Audit Trail (KYC)
- **Objetivo:** dar a las decisiones de KYC un timeline consultable, cross-cutting, con antes/después.
- **Alcance exacto:** `record_compliance_event()` wrapper (`Category.COMPLIANCE`, ya definida y sin uso) integrado en `admin.py::approve_kyc/reject_kyc`.
- **Archivos existentes que tocaría:** `simulator/broker_audit.py` (nuevo wrapper), `simulator/admin.py` (dos funciones).
- **Archivos nuevos:** ninguno.
- **Modelos/servicios necesarios:** ninguno nuevo más allá del campo `user` ya propuesto en AUDIT-02 (si AUDIT-02 se implementa primero, AUDIT-03 lo reutiliza sin migración propia).
- **Eventos cubiertos:** `compliance.kyc_approved`, `compliance.kyc_rejected`.
- **Riesgos:** cuidado explícito de privacidad — `metadata` debe incluir `status` antes/después, `rejection_reason` (ya es texto que el propio staff redactó, visible en el admin), **nunca** contenido de `document_front`/`document_back`/`selfie`. Lista blanca de tests obligatoria (§11.5).
- **Dependencias:** campo `user` de AUDIT-02 (o implementarlo aquí si AUDIT-03 se adelanta).
- **Estrategia de tests:** ver §11, con énfasis en la lista blanca de privacidad.
- **Criterios de aceptación:** toda aprobación/rechazo de KYC genera un evento consultable por `user_id`; ningún campo de archivo aparece en `metadata` (test explícito que falla si se detecta `document_front`/`document_back`/`selfie` como substring de cualquier valor).
- **Complejidad estimada:** Baja.
- **Orden recomendado:** 2º.

### AUDIT-04 — Authentication & Session Security Trail
- **Objetivo:** persistir selectivamente los eventos de seguridad que hoy solo existen en `security_log()` y tienen valor de reconstrucción real (no todo — ver §8.3).
- **Alcance exacto:** `record_auth_event()` wrapper (`Category.AUTHENTICATION`) para `auth.2fa_verify_failed` (fallos repetidos) y `withdrawal.2fa_failed`. Explícitamente **fuera de alcance**: `ratelimit.hit`, `ws.rejected_unauthenticated` (permanecen log-only, ver justificación en §8.3).
- **Archivos existentes que tocaría:** `simulator/broker_audit.py`, `simulator/views.py` (los mismos puntos donde `security_log()` ya se llama, agregando la llamada adicional al lado, nunca reemplazándola).
- **Archivos nuevos:** ninguno.
- **Modelos/servicios necesarios:** ninguno nuevo (reutiliza `user`/`account` ya propuestos).
- **Eventos cubiertos:** `auth.2fa_verify_failed`, `withdrawal.2fa_failed`.
- **Riesgos:** volumen — si un atacante hace fuerza bruta de TOTP, cada intento fallido escribiría una fila. Mitigación: el propio `ratelimit.py` ya limita la tasa de intentos antes de que este código se alcance en volumen alto; no se requiere un dedup adicional.
- **Dependencias:** ninguna.
- **Estrategia de tests:** ver §11.
- **Criterios de aceptación:** fallos de 2FA (login y retiro) consultables por `user_id`/`account_id`; `ratelimit.hit`/`ws.rejected_unauthenticated` explícitamente NO migrados (test de no-regresión que confirma que siguen siendo log-only, para que una futura sesión no los agregue sin la misma discusión de volumen).
- **Complejidad estimada:** Baja.
- **Orden recomendado:** 4º.

### AUDIT-05 — Trading Rejection Trail (per-account) + Population Engine Parity
- **Objetivo:** cerrar la asimetría entre rechazo RISK-02 (auditado) y rechazo per-account (solo log), y la asimetría entre apertura WS (auditada) y apertura por población (no auditada).
- **Alcance exacto:** una llamada `record_risk_event()` en `consumers.py:2620` (justo antes del `return` del guard fallido) + una llamada `record_trade_event(EV_POSITION_OPENED, actor_type=SYSTEM)` en `population_engine.py:260`.
- **Archivos existentes que tocaría:** `simulator/consumers.py`, `simulator/population_engine.py`.
- **Archivos nuevos:** ninguno.
- **Modelos/servicios necesarios:** ninguno nuevo — reutiliza wrappers ya existentes de AUDIT-01 sin cambios de schema.
- **Eventos cubiertos:** `risk.order_rejected` (variante per-account, distinguible de la variante RISK-02 vía `metadata.reason_source`), `position.opened` (origen `population_engine`).
- **Riesgos:** volumen — los rechazos per-account pueden ser frecuentes bajo condiciones de mercado volátiles (muchos usuarios chocando el mismo guard de margen). Mitigación a decidir: severidad `INFO` (no `WARNING`/`HIGH`) para no inflar los filtros de "eventos críticos" del admin.
- **Dependencias:** ninguna.
- **Estrategia de tests:** ver §11, más un test específico de "no lock adicional" (§11.6), dado que este call site vive dentro del guard bajo lock ya existente.
- **Criterios de aceptación:** todo rechazo per-account genera un evento consultable por `account_id`; aperturas de `population_engine` aparecen en `events_for_account()` igual que las de WS; 0 regresión en performance del guard (el guard es de baja latencia por diseño — medir antes/después si el equipo lo considera necesario).
- **Complejidad estimada:** Baja-Media (el guard de `consumers.py` es código sensible de lock order — requiere revisión cuidadosa aunque el cambio en sí sea pequeño).
- **Orden recomendado:** 3º.

### AUDIT-06 — Administrative Configuration Change Trail
- **Objetivo:** before/after de ediciones de configuración con impacto económico/de riesgo (`BrokerSpreadConfig`, `RiskRule`, `FundedConfig`, `ChallengeProduct`).
- **Alcance exacto:** un helper `record_admin_config_change()` integrado en `save_model()` de los cuatro `ModelAdmin` correspondientes.
- **Archivos existentes que tocaría:** `simulator/admin.py` (cuatro clases `ModelAdmin`), `simulator/broker_audit.py` (helper nuevo).
- **Archivos nuevos:** ninguno.
- **Modelos/servicios necesarios:** ninguno nuevo — usa `metadata` (JSON) para `before`/`after`, sin FK dedicado (estos modelos no necesitan búsqueda directa por evento con la misma urgencia que depósitos/retiros).
- **Eventos cubiertos:** `admin.config_changed` (genérico, con `metadata.model` indicando cuál de los cuatro).
- **Riesgos:** `form.changed_data` de Django puede incluir campos que no deberían guardarse tal cual (ninguno de estos cuatro modelos tiene campos sensibles hoy, pero el helper debe usar lista blanca de campos por modelo, no volcar todo `changed_data` a ciegas — mismo principio de §9).
- **Dependencias:** ninguna.
- **Estrategia de tests:** ver §11, con foco en la lista blanca por modelo.
- **Criterios de aceptación:** toda edición vía admin de los cuatro modelos genera un evento con el diff exacto de campos cambiados; ediciones sin cambios reales (`form.changed_data` vacío) no generan evento (evitar ruido).
- **Complejidad estimada:** Media (cuatro puntos de integración, cada uno con su propia lista blanca de campos).
- **Orden recomendado:** 5º.

### AUDIT-07 — Challenge Lifecycle Trail
- **Objetivo:** timeline de activación/avance de challenges — hoy completamente silencioso.
- **Alcance exacto:** una llamada de auditoría en cada uno de los tres puntos de transición de `challenge_engine.py` (`activate_challenge_enrollment`, `advance_to_phase2`, `advance_to_funded`), dentro de sus `transaction.atomic()` ya existentes.
- **Archivos existentes que tocaría:** `simulator/challenge_engine.py`, `simulator/broker_audit.py` (posible campo `challenge_enrollment` si se decide FK dedicado).
- **Archivos nuevos:** ninguno.
- **Modelos/servicios necesarios:** FK opcional `challenge_enrollment` en `BrokerAuditEvent` (migración pequeña) — o, alternativamente, reutilizar `account` (la `TradingAccount` resultante) si el equipo decide que no amerita un FK dedicado. **Decisión a tomar con el usuario antes de implementar** (ver nota en §8.5 sobre taxonomía de categoría).
- **Eventos cubiertos:** `challenge.enrollment_activated`, `challenge.advanced_to_phase2`, `challenge.advanced_to_funded`.
- **Riesgos:** ninguno a la lógica de challenge (aditivo, dentro de una `transaction.atomic()` ya existente — incluso puede fallar de forma fail-open sin afectar la activación real, igual que todo lo demás).
- **Dependencias:** decisión de categoría/FK previa (única razón para no ser el primer bloque pese a ser conceptualmente simple).
- **Estrategia de tests:** ver §11.
- **Criterios de aceptación:** las tres transiciones de challenge dejan un evento consultable por cuenta/enrollment; 42 tests existentes de `test_challenge_wallet_purchase.py` siguen en verde sin modificación.
- **Complejidad estimada:** Baja-Media.
- **Orden recomendado:** 6º.

### AUDIT-08 — Cross-Entity Search & Admin Query Surface
- **Objetivo:** consolidar lo construido en AUDIT-02 a AUDIT-07 en una superficie de consulta real para staff — no un nuevo motor de escritura, un cierre de la capa de lectura.
- **Alcance exacto:** extender `BrokerAuditEventAdmin` (search_fields/list_filter) y agregar `events_for_user/deposit/withdrawal/funded_payout()` en `broker_audit.py`; opcionalmente una vista de staff simple ("timeline de esta cuenta/usuario") reutilizando `recent_events`-style queries, si el usuario confirma que el admin de Django no es suficiente para el flujo operativo diario.
- **Archivos existentes que tocaría:** `simulator/admin.py`, `simulator/broker_audit.py`, posiblemente `simulator/views.py` + un template nuevo si se decide una vista de staff dedicada.
- **Archivos nuevos:** posible template nuevo (`simulator/templates/simulator/audit_timeline.html`), solo si se confirma la necesidad.
- **Modelos/servicios necesarios:** ninguno nuevo.
- **Eventos cubiertos:** ninguno nuevo — es capa de lectura sobre todo lo anterior.
- **Riesgos:** ninguno funcional; riesgo de alcance difuso si se construye una UI antes de confirmar que el admin estándar no alcanza — evitar sobre-construir.
- **Dependencias:** AUDIT-02 a AUDIT-07 (o al menos los que se hayan implementado hasta ese punto).
- **Estrategia de tests:** tests de admin (`search_fields` funcionan), tests de las nuevas funciones `events_for_X()`.
- **Criterios de aceptación:** un staff puede encontrar, desde un único punto de entrada, todos los eventos relacionados con un depósito/retiro/usuario/cuenta/challenge específico sin tocar la shell de Django.
- **Complejidad estimada:** Baja (si solo se extiende el admin existente) a Media (si se construye una vista dedicada).
- **Orden recomendado:** 7º (último, por diseño — consolida en vez de abrir gaps nuevos).

---

## 13. Criterios de finalización del Audit Trail Engine completo

El engine se considera **completo** (no "terminado para siempre" — extensible por diseño) cuando:

1. Los 8 dominios pedidos por el usuario (posiciones, balance, depósitos, retiros, BrokerLedger, riesgo, alertas RISK-03 críticas/altas, administrativos, staff, KYC, 2FA, configuración, force close, payouts, challenges, errores WS, seguridad) tienen **al menos** una vía de trazabilidad consultable (🟢), o una decisión explícita y documentada de por qué permanece 🟡/🔴 (como ya ocurre, correctamente, con RISK-03 INFO/WARNING y con `ratelimit.hit`).
2. `BrokerAuditEvent` es indexable/buscable por las seis dimensiones pedidas: usuario, cuenta, posición (`trade`), orden (vía `account`+`metadata` o el nuevo esquema si se decide un FK de orden), depósito, retiro.
3. Todo escritor nuevo pasa por `record_event()` (o un wrapper delgado sobre él) — cero segundas tablas de eventos, cero lógica de escritura duplicada.
4. La suite de tests del proyecto (hoy 2892) crece proporcionalmente (se estima +150-250 tests nuevos a través de AUDIT-02 a AUDIT-08) y permanece en 100% verde en cada bloque.
5. Cada bloque implementado sigue exactamente el molde de FASE 1 (auditoría de lo existente antes de tocar nada) → FASE N (implementación aditiva) → tests → documento propio, igual que AUDIT-01 y toda la serie BOOK/RISK.
6. Ningún bloque introdujo un `Signal` de Django, un nuevo lock de negocio, o una segunda tabla de eventos — los tres invariantes arquitectónicos explícitos de este documento.
7. El fail-open/fail-closed de cada dominio quedó documentado y decidido explícitamente (no implícito) — incluyendo la decisión pendiente de §9 sobre payouts.

---

## 14. Dependencias futuras con BOOK-04, Liquidity, Treasury y Compliance

- **BOOK-04 (Routing Engine):** consumirá `BrokerAuditEvent` para reconstruir, post-mortem, por qué una orden se enrutó A-book vs B-book en un momento dado — requiere que AUDIT-02+ ya tenga el timeline de riesgo/trading completo (AUDIT-05 en particular, para que los rechazos per-account también sean parte de la reconstrucción). **BOOK-04 no debe escribir sus propios eventos de ruteo en `BrokerAuditEvent` sin su propio bloque de diseño** (fuera de alcance de este documento, ver regla explícita del usuario: "no implementar Routing... todavía").
- **Liquidity Engine:** cuando exista, sus decisiones de asignación de liquidez (qué LP, qué precio, qué fill) serán eventos de un dominio nuevo (`Category.LIQUIDITY`, a definir en su propio bloque) — el Audit Trail Engine ya tiene la extensibilidad de categoría necesaria (agregar una constante a la clase `Category` es trivial y no rompe nada existente).
- **Treasury (aún no existe como proyecto separado, según `docs/PROJECT_STATUS_2026.md` §5):** una vez exista, sus movimientos de fondos entre la wallet interna (legacy) y el Treasury real necesitarán el mismo timeline consultable que AUDIT-02 ya construye para payments — Treasury debería ser un **consumidor y productor** vía el mismo `record_payment_event()`, no un sistema de auditoría propio.
- **Compliance (como función, no como bloque numerado todavía):** AUDIT-03 (KYC) es su primer cimiento real. Futuras necesidades regulatorias (reportes de actividad sospechosa, límites de exposición por jurisdicción) construirán sobre `Category.COMPLIANCE` ya wireado, no sobre un sistema nuevo.
- **Dealing Desk híbrido A-book/B-book (bloque 4 del roadmap):** es, con los otros tres, el consumidor más intensivo del Audit Trail completo — necesitará reconstruir, para cualquier cuenta/orden, la secuencia completa: apertura → validación de riesgo → decisión de ruteo (futuro BOOK-04) → ejecución → cierre → resultado de contraparte. Cada eslabón de esa cadena que este plan deja en 🔴/🟡 hoy es un eslabón que el Dealing Desk híbrido no podrá reconstruir cuando llegue su turno — esta es la justificación de negocio más concreta para completar AUDIT-02 a AUDIT-08 **antes** de llegar al bloque 4 del roadmap oficial.

---

## 15. Recomendación concreta del primer bloque a implementar

### AUDIT-02 — Payments & Payout Audit Trail

**Por qué este y no otro:**
1. **Es el gap de mayor severidad real con evidencia de código, no de suposición** — `funded_payouts.py` tiene cero llamadas de auditoría pese a mover dinero real (crypto, vía NOWPayments, en el camino `FUNDED_INTERNAL`).
2. **Sigue exactamente el patrón ya validado tres veces** (BOOK-02, RISK-02, AUDIT-01 mismo) — motor de negocio existente + un único punto de integración aditivo + wrapper delgado sobre un writer ya probado. Es el bloque de **menor riesgo de romper algo**, no el más grande.
3. **Desbloquea trabajo futuro real:** tanto Treasury (futuro) como el propio roadmap de Compliance dependen de que el dominio de payments tenga trazabilidad antes de construir encima.
4. **Complejidad estimada más baja de los bloques de alta severidad** (comparado con AUDIT-06, que toca cuatro `ModelAdmin` distintos, o AUDIT-07, que requiere una decisión de taxonomía de negocio previa).
5. Es coherente con la secuencia cronológica del propio roadmap: BOOK-02/03 y RISK-01/02/03 ya se ocuparon de dinero (posiciones/ledger) y riesgo; el gap remanente de mayor severidad en "dinero moviéndose" es, precisamente, payouts.

**Siguiente paso concreto (fuera de alcance de esta sesión, solo para dejar registrado):** antes de escribir código de AUDIT-02, correr su propia "FASE 1" — una lectura exhaustiva de `funded_payouts.py`, `views.py::funded_payout_request_view`, y el webhook de NOWPayments relevante, replicando el mismo formato de auditoría de prerrequisitos que AUDIT-01 documentó en su propio módulo antes de escribir una sola línea de `record_event()`.

---

## Anexo A — Comandos y lecturas ejecutados durante este análisis

```bash
pwd                                    # confirmado: ~/Desktop/trx_sim
git status --short                     # ?? docs/PROJECT_STATUS_2026.md (sin cambios de esta sesión)
git branch --show-current              # main
git log -5 --oneline                   # HEAD 776d48a ... hasta 2dbd853
grep -rn "AUDIT-0" --include="*.py" simulator market_data
grep -rn "record_.*_event\|log_audit(\|security_log(" --include="*.py" simulator
grep -n "Category\.PAYMENTS\|COMPLIANCE\|AUTHENTICATION\|MONITORING\|LEDGER\|SYSTEM" simulator/broker_audit.py simulator/*.py
```

Lectura completa de: `docs/PROJECT_STATUS_2026.md`, `simulator/broker_audit.py`, `simulator/audit.py`, `simulator/models.py` (secciones `AuditLog`, `BrokerAuditEvent`, `BrokerAuditObservationLock`, `TradingViolation`, `FundedPayoutRequest`, `WithdrawalRequest`, `KYCProfile`), `simulator/broker_ledger.py`, `simulator/admin.py` (secciones KYC, funded payout actions, `BrokerAuditEventAdmin`), `simulator/consumers.py` (lock order + rutas de rechazo), `simulator/management/commands/disable_2fa.py`.

## Anexo B — Nomenclatura a revisar (menor, no bloqueante)

`views.py` usa `EV_ADMIN_ACTION` (de `audit.py`) para registrar que un usuario **normal** activó/desactivó su propio 2FA — semánticamente esto no es una "acción admin", es autogestión del propio usuario. No se propone corregirlo en este documento (fuera de alcance de "Audit Trail Engine", es un detalle de nomenclatura de AuditLog ya existente) pero se deja anotado para una futura sesión de limpieza, sin urgencia.

---

## Anexo C — AUDIT-02: estado de implementación final (as-built)

*Añadido el 2026-07-21 tras la implementación, la autoauditoría técnica y una ronda de 5 ajustes de diseño solicitados antes de autorizar la implementación. Tiene autoridad sobre cualquier texto de §12/§15 que quede desactualizado por estas decisiones.*

### C.1 — Los 8 puntos de integración reales (no ~4, como estimaba el diseño original)

La lectura completa de `funded_payouts.py` reveló 4 funciones, no 2, y `handle_internal_payout_webhook()` (el punto donde un payout `FUNDED_INTERNAL` realmente se completa o falla, vía webhook asíncrono de NowPayments) resultó ser el gap más severo: `views.py::withdraw_payout_callback` **salta por completo** sus propios `log_audit()` de retiro normal cuando el `WithdrawalRequest` tiene un `FundedPayoutRequest` vinculado (`views.py:2522-2525`, `continue` antes de llegar a esa lógica) — es decir, esas transiciones no dejaban *ningún* rastro, en ningún sistema, antes de AUDIT-02.

| # | Función / vista | Evento | Momento exacto de la llamada (verificado línea por línea) |
|---|---|---|---|
| 1 | `funded_payouts.approve_sim_payout()` | `payment.funded_payout_sim_approved` | Última instrucción dentro del único `transaction.atomic()`, después de todas las mutaciones (débito, ledger, wallet, FPR→COMPLETED) |
| 2 | `funded_payouts.approve_internal_payout()` — Fase 1 | `payment.funded_payout_internal_approved` | Última instrucción del `transaction.atomic()` de Fase 1, después de FPR→APPROVED y creación del `WithdrawalRequest` |
| 3 | `funded_payouts.approve_internal_payout()` — Fase 2 éxito | `payment.funded_payout_internal_submitted` | Después de que la llamada a NowPayments retorna éxito y de actualizar WR/FPR→PROCESSING (fuera de `atomic()`, como el resto de Fase 2 — es una llamada HTTP externa) |
| 4 | `funded_payouts.approve_internal_payout()` — excepción/reversa | `payment.funded_payout_internal_submit_failed` (HIGH, SYSTEM) | Última instrucción del `transaction.atomic()` de la reversa compensatoria, después de restaurar el saldo y marcar FPR/WR como FAILED |
| 5 | `funded_payouts.handle_internal_payout_webhook()` — COMPLETED | `payment.funded_payout_internal_completed` (SYSTEM) | Después de FPR→COMPLETED, dentro del mismo `transaction.atomic()` protegido por `select_for_update()` |
| 6 | `funded_payouts.handle_internal_payout_webhook()` — FAILED | `payment.funded_payout_internal_failed` (HIGH, SYSTEM) | Después de restaurar saldo + FPR→FAILED, mismo bloque atómico |
| 7 | `views.deposit_view()` | `deposit.created` (AuditLog, constante activada, ya existía) | Justo después de `Deposit.objects.create()` |
| 8 | `views.deposit_callback()` | `deposit.callback` (AuditLog, constante activada) **+** `deposit.credited` (espejo en `BrokerAuditEvent`, nuevo) | `deposit.callback`: antes del gate de idempotencia, deliberado (registra todo intento, incluidos duplicados). `deposit.credited`: dentro del bloque protegido por `select_for_update()`, justo después de `credit_wallet()` y del `log_audit()` ya existente |

Todos verificados con `select_for_update` sin cambios (mismos 8 locks preexistentes, ninguno nuevo) y con las llamadas de auditoría siempre **después** de la mutación financiera correspondiente dentro de su bloque protegido.

### C.2 — `correlation_id`: diseño final

No se deriva de ningún id de modelo individual. Es un `UUIDField`, generado **una sola vez por operación**, en su **entidad raíz**:

```python
# FundedPayoutRequest y Deposit
correlation_id = models.UUIDField(
    null=True, blank=True, default=uuid.uuid4, editable=False, db_index=True,
)
```

`default=uuid.uuid4` se evalúa una vez al **construir** la instancia en Python — no en cada `.save()` — por lo que ninguna de las llamadas posteriores del ciclo de vida (que solo hacen `.filter(pk=...).update(...)`, nunca `.objects.create()` de nuevo sobre la misma operación) puede regenerarlo. Cada función de `funded_payouts.py`/`views.py` simplemente **lee** `fpr_locked.correlation_id` / `deposit.correlation_id` desde la fila ya persistida y lo pasa a `record_payment_event(correlation_id=...)`. `record_event()` (broker_audit.py) nunca genera un `correlation_id` — solo genera `event_id` (fresco en cada evento, correcto, cada fila necesita un id único).

Verificado con el test `CorrelationAcrossLifecycleTests.test_correlation_id_unifies_approval_and_webhook`: aprobación (Fase 1 + Fase 2) → webhook asíncrono posterior (llamada separada, simulando el salto real de proceso) comparten el mismo `correlation_id`, leído de vuelta desde la fila `FundedPayoutRequest`, nunca de estado en memoria compartido entre llamadas.

Distinto de `request_id` (correlaciona eventos dentro de **una** request/task HTTP/Celery) — `correlation_id` correlaciona todos los eventos de **una operación de negocio completa**, sin importar cuántas requests/tasks/webhooks la tocaron. Regla para bloques futuros (Trading/Treasury/Compliance): cada dominio nuevo añade `correlation_id` a **su propia** entidad raíz siguiendo este mismo patrón.

### C.3 — `event_version`: diseño final

```python
# BrokerAuditEvent
event_version = models.PositiveSmallIntegerField(default=1)
```

Mismo patrón que `pricing_context.py::schema_version` y `broker_ledger.py::_SCHEMA_VERSION`, ya precedentes en este codebase. Versiona la forma de `metadata` **para ese `event_type` específico** — no la tabla completa ni el engine — así un futuro bloque puede cambiar la forma de un `event_type` puntual sin que un lector antiguo la malinterprete silenciosamente. Todo `event_type` introducido en AUDIT-02 arrancó en `event_version=1`; las filas de AUDIT-01 (previas a esta migración) también quedan en `1` por el `default` no-nulo de la migración — correcto, ya que su forma de `metadata` no cambió.

### C.4 — Decisión final AuditLog vs. `BrokerAuditEvent`

| Momento | Sistema | Razón |
|---|---|---|
| `deposit.created` (Deposit recién creado, sin dinero confirmado aún) | **Solo `AuditLog`** | Procedimental, pre-financiero — no hay hecho institucional que registrar todavía |
| `deposit.callback` (webhook de NowPayments recibido, cualquier status) | **Solo `AuditLog`** | Igual — registra el intento, no un hecho financiero confirmado. Se registra **incluso en duplicados**, deliberadamente (ver C.1 #8) |
| `deposit.credited` (dinero realmente acreditado en la wallet) | **`AuditLog` (sin cambio) + espejo en `BrokerAuditEvent`** (nuevo) | Es el hecho financiero real. Cierra la asimetría: los payouts (dinero saliendo) ya tenían cobertura institucional; los depósitos (dinero entrando) no la tenían hasta este espejo. Mismo precedente que force-close: "eventos financieros y administrativos son distintos, no duplicados" — aquí es "registro HTTP vs. registro institucional cross-engine son complementarios" |
| Retiros (`WithdrawalRequest`: request/approved/rejected/failed/refunded/completed) | **Solo `AuditLog` (sin cambio)** | Ya tiene cobertura rica. Espejarlos en `BrokerAuditEvent` queda **explícitamente diferido** a un bloque futuro nombrado: **AUDIT-09 — Withdrawal & Legacy AuditLog Convergence** |

Regla institucional fijada por esta decisión, para todo bloque futuro: *todo hecho con peso financiero o institucional debe existir en `BrokerAuditEvent`, exista o no también en `AuditLog`; lo puramente procedimental/HTTP-scoped puede quedarse solo en `AuditLog`.*

### C.5 — Fail-open: regla final del engine (no solo de AUDIT-02)

> **Fail-open es la política por defecto del Audit Trail Engine completo, no negociable.** Ninguna escritura de auditoría puede bloquear, revertir o retrasar una operación financiera o administrativa. Cualquier excepción fail-closed futura debe justificarse explícitamente en el documento de diseño de **su propio bloque**, con el escenario de negocio concreto que la motiva — nunca se introduce de forma implícita ni como default.

La sugerencia tentativa de un "soft fail-closed" en la aprobación de payout (mencionada en una versión anterior de este documento) **fue retirada explícitamente** — AUDIT-02 se implementó 100% fail-open en sus 8 puntos, sin excepción, verificado con 6 tests que fuerzan la excepción (`patch(..., side_effect=RuntimeError(...))` sobre `BrokerAuditEvent.objects.create`) y confirman que la operación financiera real (aprobación, reversa compensatoria, webhook) se completa exactamente igual.

Mecanismo (sin cambios respecto al de AUDIT-01, solo reafirmado): `record_event()` envuelve el `.create()` en `try/except Exception`, nunca relanza:
```python
except Exception as exc:
    log.error("[broker_audit] FAILED to record event=%s: %r", event_type, exc, exc_info=True)
    return None
```

### C.6 — Diagrama de flujo actualizado (AUDIT-01 + AUDIT-02)

```
                    Trading (AUDIT-01, sin cambios)
     consumers.py ──► position.opened / position.closed / risk.order_rejected
                            │
                            ▼
     ┌──────────────────────────────────────────────────────────────┐
     │              simulator/broker_audit.py                        │
     │  record_event()  ← único escritor real                        │
     │    ├─ record_trade_event()   (TRADING)                        │
     │    ├─ record_risk_event()    (RISK)                           │
     │    ├─ record_admin_event()   (ADMIN)                          │
     │    ├─ record_system_event()  (SYSTEM)                         │
     │    └─ record_payment_event() (PAYMENTS)  ← nuevo, AUDIT-02     │
     └───────────────────────┬────────────────────────────────────────┘
                             │ savepoint anidado — fail-open siempre
                             ▼
                 BrokerAuditEvent (única tabla institucional)
                   + funded_payout_request, deposit (FKs nuevos)
                   + correlation_id (UUID, leído de la entidad raíz)
                   + event_version (default=1, por event_type)

  Payments (AUDIT-02, nuevo) ──────────────────────────────────────────
  funded_payouts.py:
    approve_sim_payout() ──────────────► SIM_APPROVED
    approve_internal_payout() Fase 1 ──► INTERNAL_APPROVED ─┐
    approve_internal_payout() Fase 2 ──► INTERNAL_SUBMITTED ─┤ mismo
      (NP falla) ───────────────────────► INTERNAL_SUBMIT_FAILED (HIGH)
    handle_internal_payout_webhook()                         │ correlation_id
      COMPLETED ──────────────────────► INTERNAL_COMPLETED  ─┤ (persistido en
      FAILED ─────────────────────────► INTERNAL_FAILED (HIGH)┘ FundedPayoutRequest,
                                                                 leído de vuelta
                                                                 tras el salto
                                                                 async del webhook)

  views.py:
    deposit_view()      ──► AuditLog: deposit.created
    deposit_callback()  ──► AuditLog: deposit.callback (todo intento, incl. duplicados)
                        ──► AuditLog: deposit.credited (sin cambio)
                        ──► BrokerAuditEvent: deposit.credited (espejo nuevo,
                                                correlation_id = deposit.correlation_id)

  Diferido explícitamente:
    WithdrawalRequest (request/approved/rejected/failed/refunded/completed)
      → permanece solo en AuditLog. Convergencia futura: AUDIT-09.
```

### C.7 — Verificación final ejecutada

```
python manage.py check                                   → System check identified no issues (0 silenced)
python manage.py migrate simulator 0052 → 0053 → 0052    → reversibilidad confirmada en vivo, limpia
python manage.py test simulator.tests market_data.tests  → 2919 tests, OK (skipped=3)
git diff --check                                         → sin problemas
```

27 tests nuevos en total para AUDIT-02 (2892 base + 27 = 2919): 23 en `simulator/tests/test_audit02_payments_trail.py` (incluyendo las 2 pruebas de doble-aprobación agregadas en la ronda de cierre) + 1 en `test_funded_payout_sim_approval.py` + 1 en `test_funded_payout_internal_approval.py` + 2 en `test_deposit.py`. La cobertura de doble-aprobación (SIM e INTERNAL) confirma que un segundo intento no duplica el `BrokerAuditEvent` — protegido por el guard preexistente `FundedPayoutAlreadyProcessed`, no por un mecanismo de dedup nuevo.
