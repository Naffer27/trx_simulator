# Audit Trail Engine — Cierre Oficial

| Campo | Valor |
|---|---|
| Rol del documento | Cierre técnico del engine completo — evaluación final, sin implementación |
| Fecha | 2026-07-22 |
| Bloques cubiertos | AUDIT-01, AUDIT-02, AUDIT-03, AUDIT-04a, AUDIT-04b |
| Último commit | `2ff6d3e735ca935646b04c89582c47063d54963a` — `feat: complete AUDIT-04b password and admin access audit trail` |
| Último tag | `audit-04b` / `audit-04b-password-admin-trail-v1` |
| Estado del repo | `main`, working tree limpio, sin push pendiente de este documento |

---

## 1. Resumen ejecutivo

### Objetivo del Audit Trail

Dar a Money Broker una **única fuente de verdad institucional, cross-engine, append-only** para reconstruir — después del hecho, sin depender de logs de servidor rotables — qué pasó, quién lo hizo, cuándo, y con qué resultado, en cualquier decisión de trading, riesgo, dinero, identidad o compliance del sistema.

### Qué problemas resuelve

Antes de este engine, la trazabilidad de Money Broker estaba fragmentada en cinco formas incompletas: (1) `AuditLog`, útil pero acotado a un puñado de eventos HTTP; (2) logs de texto no persistentes (`security_log()`), perdidos en cada rotación; (3) el propio estado de los modelos de negocio (`reviewed_by`/`reviewed_at`), sobrescrito en cada nueva revisión sin dejar historial; (4) nada en absoluto para dominios enteros (payouts de fondeo, acceso al Django Admin, reset de contraseña); y (5), el patrón más grave encontrado dos veces de forma independiente (KYC y 2FA), acciones legítimas del propio sistema que **destruían la única evidencia de una decisión anterior** al ejecutarse (un resubmit de KYC borraba el rechazo previo; desactivar 2FA borraba el único registro de que había estado activo).

### Alcance final

Cinco bloques completados, seis categorías institucionales activas (`TRADING`, `RISK`, `ADMIN`, `PAYMENTS`, `COMPLIANCE`, `AUTHENTICATION`), **30 tipos de evento** distintos, **187 tests dedicados** exclusivamente al motor de auditoría, cero deuda de migraciones pendientes, y una disciplina de diseño (fail-open, append-only, locking correcto, privacidad por whitelist) verificada y probada de forma idéntica en los cinco bloques — no hay una versión "temprana, menos rigurosa" del engine; el estándar de AUDIT-04b es el mismo que el de AUDIT-01.

---

## 2. Bloques completados

### AUDIT-01 — Broker Event Audit Trail Foundation

- **Propósito:** cimentar el engine — modelo, disciplina de escritura, y los tres primeros dominios (Trading, Risk, Admin) que hasta entonces solo dejaban líneas de log.
- **Funcionalidades implementadas:** apertura/cierre de posición (los 4 caminos reales de cierre unificados en un solo escritor), rechazo de orden por límite de riesgo del bróker (RISK-02), observación periódica de alertas críticas/altas (RISK-03, deduplicada), y force-close administrativo con actor real capturado.
- **Commit:** `776d48a` — `feat: broker-event-audit-foundation-audit01`.
- **Tag:** ninguno — este bloque es anterior a la convención de tags de cierre establecida a partir de AUDIT-02; existe solo como commit.
- **Tests agregados:** 68 (`simulator/tests/test_broker_audit_trail.py`).
- **Decisiones de arquitectura principales:** el modelo `BrokerAuditEvent` se diseñó con seis categorías más de las que este bloque usaba (`PAYMENTS`, `COMPLIANCE`, `AUTHENTICATION`, `MONITORING`, `LEDGER`, `SYSTEM`) — espacio deliberado para el trabajo que vendría después, sin necesidad de tocar el schema en ningún bloque posterior salvo para FKs de dominio. `record_event()` como único escritor real; todo lo demás son wrappers delgados por categoría. Fail-open y append-only (incluido a nivel de Django Admin) fijados aquí como contrato no negociable del resto del engine.

### AUDIT-02 — Payments & Payout Audit Trail

- **Propósito:** cerrar el gap de mayor severidad real encontrado en la auditoría general del proyecto — payouts de fondeo sin ningún rastro, y ciclo de vida de depósito incompleto.
- **Funcionalidades implementadas:** aprobación SIM e INTERNAL de payouts de fondeo, envío/reversa/confirmación vía webhook de NowPayments, y el espejo institucional de `deposit.credited` (el hecho financiero real del depósito, complementando — no reemplazando — `AuditLog`).
- **Commit:** `f8fdb8f` — `feat: complete AUDIT-02 payments and payouts audit trail`.
- **Tags:** `audit-02`, `audit-02-payments-trail-v1`.
- **Tests agregados:** 27 (23 en `test_audit02_payments_trail.py` + 4 extendidos en archivos preexistentes de payouts/depósitos, sin tocar sus aserciones originales).
- **Decisiones de arquitectura principales:** `correlation_id` institucional (UUID generado una sola vez en la entidad raíz — `FundedPayoutRequest`/`Deposit` — vía `default=uuid.uuid4`, nunca regenerado) para seguir una operación a través de un salto async real (aprobación → webhook). Migración `0053`, puramente aditiva. Decisión explícita de mantener `AuditLog` para lo procedimental (depósito creado, callback recibido) y reservar `BrokerAuditEvent` para el hecho financiero — retiros quedaron deliberadamente diferidos (ver AUDIT-09 en `docs/AUDIT_TRAIL_ENGINE_PLAN.md`).

### AUDIT-03 — Compliance Audit Trail (KYC)

- **Propósito:** dar historial permanente a las decisiones de KYC, cuyo único rastro (`reviewed_by`/`reviewed_at`/`rejection_reason`) se perdía por completo en cada resubmit del usuario.
- **Funcionalidades implementadas:** aprobación/rechazo de KYC por staff, y el evento `kyc_resubmitted` capturado en el instante exacto en que `KYCProfile` pierde los datos de la revisión anterior — sin necesitar preservarlos ahí mismo, porque el evento `kyc_rejected` ya los guardó permanentemente antes.
- **Commits:** `409b911` (`docs: prepare AUDIT-03 planning`) + `0d28052` — `feat: complete AUDIT-03 compliance KYC audit trail`.
- **Tags:** `audit-03`, `audit-03-compliance-kyc-trail-v1`.
- **Tests agregados:** 28, todos en `test_audit03_compliance_trail.py`.
- **Decisiones de arquitectura principales:** primer bloque en **corregir una condición de carrera real** (no solo observarla) — `approve_kyc()`/`reject_kyc()`/el resubmit en `kyc_view()` no tenían `select_for_update()`; dos clics concurrentes podían duplicar el evento y sobrescribir silenciosamente `reviewed_by`. Migración `0054` (campo `user`, único nuevo desde entonces — todo dominio ulterior lo reutiliza sin migración propia). `correlation_id` explícitamente descartado aquí (`KYCProfile` es una fila reutilizada de por vida, no una operación nueva por intento) en favor de correlación por query (`events_for_user()` ordenado por tiempo).

### AUDIT-04a — 2FA Lifecycle Trail

- **Propósito:** cerrar el mismo patrón de destrucción de evidencia que KYC, esta vez en 2FA — `totp_disable_view` borra el único `TOTPDevice` que existía.
- **Funcionalidades implementadas:** activación (con snapshot de un re-enable silencioso), desactivación self-service y de emergencia (comando `disable_2fa`, con el cruce entre ambos flujos correctamente serializado), y verificación fallida con **umbral configurable** — nunca un evento por cada intento de fuerza bruta, solo al cruzar el umbral, usando el contador Redis ya existente de `ratelimit.py` en modo observación pura.
- **Commit:** `23f9d26` — `feat: complete AUDIT-04a 2FA lifecycle audit trail`.
- **Tags:** `audit-04a`, `audit-04a-2fa-lifecycle-trail-v1`.
- **Tests agregados:** 31, todos en `test_audit04a_2fa_trail.py`.
- **Decisiones de arquitectura principales:** **cero migración** (primer bloque en no necesitar ninguna — `user` ya alcanzaba). Locking añadido de verdad (no solo diseñado) en disable self-service y en el comando de emergencia, con test explícito del cruce entre ambos. Decisión deliberada de **no** auditar cada intento fallido de verificación — la primera vez que el engine antepone "evitar ruido" a "cubrir todo".

### AUDIT-04b — Password & Admin Access Trail

- **Propósito:** el dominio con menos código propio que envolver de todo el engine — password reset/change y login al Django Admin son 100% vistas stock de Django, sin ningún punto de integración preexistente.
- **Funcionalidades implementadas:** cambio de contraseña autenticado, solicitud y finalización de reset (con anti-enumeración preservada), y login exitoso/fallido al Django Admin real (distinto del login de usuario normal, nunca antes auditado).
- **Commit:** `2ff6d3e` — `feat: complete AUDIT-04b password and admin access audit trail`.
- **Tags:** `audit-04b`, `audit-04b-password-admin-trail-v1`.
- **Tests agregados:** 33, todos en `test_audit04b_password_admin_trail.py`.
- **Decisiones de arquitectura principales:** módulo nuevo dedicado (`simulator/auth_password_views.py`) en vez de seguir engordando `views.py`. `correlation_id` resuelto de forma elegante sin tocar el mecanismo de seguridad de Django — `sha256(token)` como clave de un `SET ... NX EX` atómico en Redis (get-or-create real, semánticamente correcto incluso cuando Django genera el mismo token para solicitudes cercanas). Override de `AdminSite.login()` como punto de extensión oficial de Django, verificado empíricamente (no solo por diseño) que no altera CSRF, `next`, ni el flujo GET.

---

## 3. Arquitectura final

- **`BrokerAuditEvent`** — única tabla institucional. 19 campos: `event_id` (UUID), `event_type`, `category`, `severity`, `timestamp` (auto), `actor_type`/`actor_id`, `account`/`trade`/`symbol` (dominio Trading), `funded_payout_request`/`deposit` (Payments), `user` (Compliance/Authentication), `correlation_id`, `event_version`, `description`, `metadata` (JSON), `source_module`, `request_id`. 9 índices, todos compuestos con `-timestamp` salvo el de `correlation_id` (búsqueda exacta).
- **`Category`** — 9 valores definidos (`TRADING`, `RISK`, `LEDGER`, `PAYMENTS`, `ADMIN`, `AUTHENTICATION`, `COMPLIANCE`, `MONITORING`, `SYSTEM`), string plano sin `choices=` a nivel de DB — agregar una categoría nueva nunca requiere migración, ya lo demostraron 3 bloques distintos.
- **`Severity`** — `INFO < WARNING < HIGH < CRITICAL`, orden explícito vía `Severity.rank()`.
- **`ActorType`** — `TRADER` (autoservicio), `STAFF` (staff con `actor_id` real), `SYSTEM` (automatizado, sin actor humano identificable — usado honestamente, nunca con un `actor_id` inventado).
- **`correlation_id`** — UUID opcional, usado en 2 de los 5 bloques (AUDIT-02, AUDIT-04b), cada uno con una estrategia de generación distinta y explícitamente justificada según si existe o no una entidad raíz persistida donde anclarlo. Nunca forzado donde no aporta (AUDIT-01, AUDIT-03, AUDIT-04a).
- **`event_version`** — versiona la forma de `metadata` por `event_type`, no la tabla ni el engine; todo evento hasta hoy en `1`.
- **`metadata`** — JSON libre, siempre bajo whitelist explícita por `event_type`, verificada por test en los 4 bloques que lo introdujeron.
- **Política append-only** — por convención de código (nunca `.update()`/`.delete()` sobre `BrokerAuditEvent`) y reforzada estructuralmente: `BrokerAuditEventAdmin.has_add/change/delete_permission()` en `False`, y `delete_selected` eliminado explícitamente de las acciones bulk.
- **Fail-open** — política no negociable del engine completo, no de un bloque: `record_event()` escribe dentro de un savepoint anidado (`transaction.atomic()`), captura cualquier excepción, loguea, y retorna `None` — nunca relanza. Verificado con al menos un test dedicado en cada uno de los cinco bloques.
- **Locking** — cuando una mutación real puede duplicarse bajo concurrencia (KYC approve/reject, 2FA disable self-service vs. emergencia), se cierra con `transaction.atomic()` + `select_for_update()` + recheck del estado **dentro** del lock. Cuando no hay ese riesgo (2FA enable, password change/reset), se documenta explícitamente por qué no hace falta, en vez de agregarlo por reflejo.
- **Privacidad** — regla única en los cinco bloques: nunca contraseñas, tokens, secretos TOTP, códigos de un solo uso, documentos o números de identidad, direcciones cripto sin enmascarar, ni payloads crudos de proveedor. Cada bloque con metadata propia tiene su propio test de lista blanca.

---

## 4. Cobertura actual

### Dominios auditados (6 de 9 categorías activas)

| Categoría | Dominio | Bloque |
|---|---|---|
| `TRADING` | Apertura/cierre de posición | AUDIT-01 |
| `RISK` | Rechazo por límite del bróker, alertas críticas/altas | AUDIT-01 |
| `ADMIN` | Force-close | AUDIT-01 |
| `PAYMENTS` | Payouts de fondeo, depósito acreditado | AUDIT-02 |
| `COMPLIANCE` | Aprobación/rechazo/resubmit de KYC | AUDIT-03 |
| `AUTHENTICATION` | 2FA (lifecycle completo), password reset/change, login al Admin | AUDIT-04a/04b |

### Los 30 event types por categoría

**TRADING (8):** `position.opened`, `position.closed`, `position.closed.manual`, `position.closed.stop_loss`, `position.closed.take_profit`, `position.closed.stopout`, `position.closed.margin_call`, `position.closed.admin_force_close`.

**RISK (2):** `risk.order_rejected`, `risk.alert_observed`.

**ADMIN (1):** `admin.position_force_close`.

**PAYMENTS (7):** `payment.funded_payout_sim_approved`, `payment.funded_payout_internal_approved`, `payment.funded_payout_internal_submitted`, `payment.funded_payout_internal_submit_failed`, `payment.funded_payout_internal_completed`, `payment.funded_payout_internal_failed`, `deposit.credited`.

**COMPLIANCE (3):** `compliance.kyc_approved`, `compliance.kyc_rejected`, `compliance.kyc_resubmitted`.

**AUTHENTICATION (9):** `auth.2fa_enabled`, `auth.2fa_disabled`, `auth.2fa_disabled_emergency`, `auth.2fa_verify_failed`, `auth.password_changed`, `auth.password_reset_requested`, `auth.password_reset_completed`, `auth.admin_site_login_success`, `auth.admin_site_login_failed`.

### Consulta

9 helpers de query en `broker_audit.py`: `events_for_account`, `events_for_trade`, `events_for_symbol`, `events_by_category`, `events_by_severity`, `events_for_funded_payout`, `events_for_deposit`, `events_for_user`, `events_by_correlation_id` — cubren las seis dimensiones de búsqueda con las que hoy se puede reconstruir cualquier operación auditada.

---

## 5. Dominios pendientes

- **`MONITORING`** — sin ningún event type. Candidato natural: observabilidad de market data (failover de proveedor, circuit breaker) ya construida en `market_data/observability/` pero nunca conectada a `BrokerAuditEvent`.
- **`LEDGER`** — sin ningún event type. `BrokerLedger` (BOOK-02) sigue siendo el sistema de verdad contable por diseño; lo que falta es un espejo institucional para entradas que no sean `COUNTERPARTY_PNL` (comisión, spread, fee de challenge) — hoy solo la entrada de cierre de posición tiene su evento gemelo.
- **`SYSTEM`** — `record_system_event()` existe, cero call sites reales.
- **Fuera de las 9 categorías, pero explícitamente pendiente:** retiros (`WithdrawalRequest`) — cobertura completa en `AuditLog`, nunca espejados a `BrokerAuditEvent`; diferido con nombre propio, **AUDIT-09**, en `docs/AUDIT_TRAIL_ENGINE_PLAN.md`, nunca agendado.
- Cambios de configuración administrativa (`BrokerSpreadConfig`, `RiskRule`, `FundedConfig` editados vía admin) — diseñado como **AUDIT-06** en el plan original, nunca implementado.
- Una superficie de consulta dedicada para staff más allá del changelist estándar de Django Admin — diseñada como **AUDIT-08**, nunca implementada.

---

## 6. Métricas

| Métrica | Valor |
|---|---|
| Tipos de evento (`EV_*`) definidos | **30** |
| Categorías activas / definidas | **6 / 9** |
| Tests dedicados al Audit Trail Engine | **187** (68 AUDIT-01 + 27 AUDIT-02 + 28 AUDIT-03 + 31 AUDIT-04a + 33 AUDIT-04b) |
| Tests totales del proyecto | **3011** (0 fallos, 4 skipped) |
| Migraciones del engine | **3** (`0051`, `0052` — AUDIT-01; `0053` — AUDIT-02; `0054` — AUDIT-03) — AUDIT-04a y AUDIT-04b no requirieron ninguna |
| Último commit | `2ff6d3e735ca935646b04c89582c47063d54963a` |
| Último tag | `audit-04b` / `audit-04b-password-admin-trail-v1` |

*(Número de filas reales en `BrokerAuditEvent` deliberadamente no reportado aquí — es una métrica de runtime que crece con el uso del sistema, no un dato de diseño; en este entorno de desarrollo no es representativa de producción.)*

---

## 7. Riesgos conocidos (reales, abiertos hoy)

1. **`disable_2fa` sigue sin capturar la identidad real del operador humano** — `actor_id=None`, solo `"performed_by": "management_command"`. Señalado en el diseño de AUDIT-04a, nunca cerrado — requeriría un argumento `--operator` nuevo en el comando, decisión de alcance no tomada.
2. **`totp_verify_view` sigue sin rate limit a nivel de aplicación** — AUDIT-04a solo agregó observabilidad (umbral configurable), nunca bloqueo. Es un gap de seguridad de la aplicación, no del audit trail, pero permanece abierto y ya fue señalado dos veces (FASE 1 y diseño de AUDIT-04a) sin resolverse.
3. **Retiros (`WithdrawalRequest`) no tienen espejo en `BrokerAuditEvent`** — diferido formalmente a AUDIT-09, sin fecha.
4. **`INTERNAL_RESET_SESSION_TOKEN`** — dependencia de una constante interna (no pública) de `django.contrib.auth.views`, estable históricamente pero sin garantía ante un upgrade mayor de Django. Documentado explícitamente en el diseño de AUDIT-04b, no mitigado más allá de eso.
5. **Cambios de configuración administrativa con impacto financiero/de riesgo** (`BrokerSpreadConfig.spread_pips`, `RiskRule.max_lot_size`, etc.) **no dejan rastro institucional** — diseñado como AUDIT-06, nunca implementado.
6. **Tres categorías completas sin ningún evento** (`MONITORING`, `LEDGER`, `SYSTEM`) — ver §5.
7. **Timing side-channel teórico en password reset** — escribir 1 fila solo cuando el email matchea es una diferencia de latencia medible en teoría; documentado como riesgo residual aceptado en el diseño de AUDIT-04b, no mitigable sin rediseñar el flujo de reset completo.

Ningún riesgo de esta lista es de severidad crítica **para el engine en sí** (todos son gaps de cobertura o de un dominio adyacente, no fallas del mecanismo ya construido) — se presentan como backlog conocido, no como bloqueantes.

---

## 8. Recomendaciones para el siguiente engine (Routing Engine / BOOK-04)

No se diseña aquí el Routing Engine — solo cómo puede apoyarse en lo ya construido:

- **`BrokerAuditEvent` sigue siendo la única tabla a escribir.** Una nueva categoría (`Category.ROUTING`, por ejemplo) se agrega con el mismo costo que las últimas tres — una constante de string, cero migración.
- **El patrón de wrapper delgado se replica sin inventar nada nuevo:** `record_routing_event()` sería, igual que los seis anteriores, una función de 10 líneas que fija `category=Category.ROUTING` y delega en `record_event()`.
- **`correlation_id` ya tiene dos estrategias probadas para elegir según el caso real de Routing:** si BOOK-04 introduce su propia entidad persistida por decisión de ruteo, el patrón de AUDIT-02 (UUID en la entidad raíz) es directamente reutilizable; si no hay una fila propia y el ciclo de vida es efímero, el patrón Redis get-or-create de AUDIT-04b (clave derivada de algo determinístico del propio flujo) también es reutilizable tal cual.
- **`events_for_account()`/`events_for_trade()` ya existen** y son exactamente lo que un post-mortem de ruteo necesita ("¿qué pasó con esta orden, en riesgo y en ejecución, antes de que se enrutara así?") — no hace falta un helper nuevo para esa dimensión, solo uno adicional si BOOK-04 tiene su propia entidad (ej. `events_for_routing_decision()`).
- **Locking, fail-open y privacidad** son las mismas tres disciplinas ya verificadas en 5 bloques — Routing debería empezar su propia FASE 1 preguntando exactamente lo mismo que cada bloque anterior: ¿hay una mutación real bajo concurrencia que proteger? ¿hay datos que nunca deben guardarse? La respuesta se documenta, no se asume.
- **`MONITORING`, hoy dormida, podría encajar mejor que `ROUTING`** para los eventos de circuit-breaker/failover de proveedor que BOOK-04 probablemente necesite observar — vale la pena decidirlo explícitamente en la FASE 1 de ese bloque, no dar por hecho una categoría nueva cuando una ya definida podría bastar.

---

## 9. Conclusión

El Audit Trail Engine, como **mecanismo**, está sólidamente terminado: cinco bloques con el mismo nivel de rigor de principio a fin (ninguno más liviano que el anterior — si acaso, cada uno mejoró sobre el previo: AUDIT-03 fue el primero en cerrar una carrera real, AUDIT-04a el primero en no necesitar migración, AUDIT-04b el primero en resolver `correlation_id` sin entidad raíz propia), 187 tests dedicados, cero regresiones acumuladas a lo largo de todo el proceso, fail-open y append-only verificados empíricamente en cada bloque — no solo diseñados. La disciplina de "una tabla, un escritor, wrappers delgados" se sostuvo sin excepción durante cinco iteraciones, incluyendo el caso más difícil (instrumentar vistas de Django que no son código propio, sin tocar su mecanismo de seguridad).

**Como cobertura, es deliberadamente parcial.** Cubre hoy los dominios de mayor severidad real identificados en la auditoría original del proyecto — dinero saliendo (payouts), identidad regulatoria (KYC), y los dos vectores de toma de cuenta más comunes (2FA, password/admin) — pero dos dominios financieros completos quedan fuera (retiros, cambios de configuración administrativa) y tres categorías enteras siguen sin un solo evento.

**¿Production Ready para un broker privado?** Con una condición explícita: **sí, para operar hoy**, porque el mecanismo en sí no puede romper nada (fail-open verificado en los cinco bloques significa que ni siquiera un fallo completo del audit trail bloquea una operación real), y porque cubre exactamente los dominios que un auditor o regulador pediría primero. **No, como "trazabilidad completa"** — un broker privado que necesite responder "muéstrame todo lo que pasó con esta cuenta" hoy tendría una respuesta completa para trading, riesgo, payouts, KYC y accesos de identidad, pero un hueco real en retiros y en cambios de configuración de riesgo/spread. La recomendación técnica es: **autorizar producción con el alcance actual, dejando AUDIT-09 (retiros) y AUDIT-06 (configuración administrativa) documentados como backlog explícito y comunicado**, no como trabajo oculto — exactamente el mismo criterio de honestidad que este engine aplicó a sí mismo en cada uno de sus cinco bloques.

---

*Documento de cierre — Audit Trail Engine. No implementado ni modificado ningún archivo del proyecto al generar este documento. Sin commit, sin push.*
