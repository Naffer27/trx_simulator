# AUDIT-03 — Documento de Transición y Preparación (Compliance / KYC)

| Campo | Valor |
|---|---|
| Rol del documento | Preparación para el siguiente bloque del Audit Trail Engine — sin implementación |
| Fecha | 2026-07-21 |
| Bloque anterior | **AUDIT-02 — Payments & Payout Audit Trail** — ✅ cerrado, commit `f8fdb8f`, tags `audit-02` / `audit-02-payments-trail-v1` |
| Bloque de este documento | **AUDIT-03 — Compliance Audit Trail (KYC)** |
| Estado | Preparación completa. **No implementado.** |
| Referencia de diseño | `docs/AUDIT_TRAIL_ENGINE_PLAN.md` §8.2, §12 (AUDIT-03), Anexo C |

---

## 1. Qué quedó terminado en AUDIT-02

- **`BrokerAuditEvent`** extendido de forma aditiva con `funded_payout_request`, `deposit`, `correlation_id`, `event_version` (migración `0053`, solo `AddField`/`AddIndex`, reversibilidad verificada en vivo).
- **`record_payment_event()`** — wrapper `Category.PAYMENTS`, mismo patrón exacto que los wrappers de AUDIT-01 (`record_trade_event`, `record_risk_event`, `record_admin_event`).
- **8 puntos de integración reales** cubiertos en `funded_payouts.py` (6) y `views.py` (2): aprobación SIM, aprobación/envío/reversa INTERNAL, webhook completado/fallido, ciclo de vida de depósito (`created`/`callback` en `AuditLog`, `credited` espejado en `BrokerAuditEvent`).
- **`correlation_id`** — UUID institucional generado una sola vez en la entidad raíz (`FundedPayoutRequest`, `Deposit`), sobrevive el salto async a un webhook, sin depender del id de ningún modelo individual. Patrón ya validado end-to-end con test dedicado.
- **`event_version`** — versiona la forma de `metadata` por `event_type`, mismo precedente que `pricing_context.py`/`broker_ledger.py`.
- **Fail-open puro** fijado como política del engine completo (no solo de AUDIT-02), documentada explícitamente en `broker_audit.py` y en el Anexo C del plan.
- **27 tests nuevos** (2892 → 2919), incluyendo idempotencia, doble-aprobación, correlation_id end-to-end, fail-open forzado, y whitelist de privacidad.
- **Decisión de convergencia AuditLog ↔ BrokerAuditEvent** fijada como regla institucional: todo hecho financiero/institucional debe existir en `BrokerAuditEvent`; lo puramente procedimental puede quedarse solo en `AuditLog`. Retiros quedan explícitamente diferidos a **AUDIT-09**.
- Commit `f8fdb8f` — working tree limpio, sin push.

## 2. Qué dependencias ya están listas para AUDIT-03

| Dependencia | Estado | Por qué AUDIT-03 la necesita |
|---|---|---|
| `record_event()` con `user_id`/`user` | **Falta — es el único campo de schema pendiente antes de codear AUDIT-03** | KYC se ancla a `User`, no a `TradingAccount`. Un usuario puede tener `KYCProfile` sin tener aún ninguna cuenta de trading. `BrokerAuditEvent` hoy no tiene FK a `User`. |
| Patrón de wrapper delgado (`record_*_event()`) | ✅ Listo | `record_compliance_event()` sigue exactamente el mismo molde que `record_payment_event()` — cero diseño nuevo, solo repetir el patrón. |
| `Category.COMPLIANCE` | ✅ Ya definida en `broker_audit.py` desde AUDIT-01, sin ningún call site — AUDIT-03 es quien la activa por primera vez | Confirma el hallazgo central del plan: AUDIT-01 fue diseñado con espacio para este trabajo. |
| Fail-open como regla del engine | ✅ Ya fijada (Anexo C, §5 de este documento) | AUDIT-03 hereda la regla sin tener que re-decidirla — cualquier excepción fail-closed necesitaría justificación propia, y este documento no propone ninguna. |
| Whitelist de privacidad por `metadata` | ✅ Patrón y tests ya establecidos en AUDIT-02 | Crítico en KYC — ver §5 de este documento. |
| Admin `BrokerAuditEventAdmin` extensible | ✅ Ya extendido dos veces (AUDIT-01→02) sin tocar el append-only | Un tercer `search_fields`/`readonly_fields` más es trivial. |
| `reviewed_by`/`reviewed_at` en `KYCProfile` | ✅ Ya existen en el modelo | Dan el actor/timestamp de la ÚLTIMA revisión — AUDIT-03 no los reemplaza, los complementa con historial append-only. |
| `kyc_emails.py` (envío de notificación KYC) | ✅ Ya funciona, no se toca | AUDIT-03 es aditivo puro, igual que AUDIT-02 no tocó `deposit_emails.py`/`withdrawal_emails.py`. |

## 3. Qué problemas resuelve AUDIT-03

Del inventario de gaps de `docs/AUDIT_TRAIL_ENGINE_PLAN.md` §3-4:

1. **`admin.py::approve_kyc()`/`reject_kyc()` no tienen ningún rastro cross-cutting** — solo el campo `reviewed_by`/`reviewed_at` del propio modelo (última revisión, sin historial; una re-revisión sobreescribe la anterior sin dejar huella de que existió).
2. **La decisión de KYC tiene consecuencia financiera directa y hoy invisible en el timeline institucional:** `readiness.py::get_user_readiness()` calcula `can_withdraw = email_verified and terms_accepted and kyc_approved and totp_enabled` — una aprobación o rechazo de KYC **desbloquea o bloquea retiros reales**, y sin embargo no genera ningún evento en `BrokerAuditEvent`, el timeline que sí registra los payouts que esa misma aprobación termina habilitando.
3. **Sin trazabilidad unificada usuario↔cuenta:** hoy no hay forma de preguntar "¿qué pasó con este usuario, en compliance y en dinero, en los últimos 30 días?" desde un único punto — KYC vive aparte de `BrokerAuditEvent`.

## 4. Alcance de AUDIT-03 (Compliance / KYC)

**Dentro de alcance:**
- `admin.py::approve_kyc()` — un evento por perfil aprobado, con `before={"status": "pending"}`/`after={"status": "approved"}`.
- `admin.py::reject_kyc()` — un evento por perfil rechazado, incluyendo `rejection_reason` (texto ya redactado por el propio staff, visible en el admin — no es un dato nuevo expuesto).
- Extensión de `BrokerAuditEvent` con el campo `user` (FK nullable, `SET_NULL`) — únicamente esto de schema nuevo.
- `record_compliance_event()` — wrapper nuevo, `Category.COMPLIANCE`.
- Extensión de `BrokerAuditEventAdmin.search_fields`/`readonly_fields` con `user__username`.
- `events_for_user()` — helper de consulta nuevo en `broker_audit.py`.

**Explícitamente fuera de alcance (diferido a bloques futuros, con nombre):**
- El envío/reenvío de un `KYCProfile` por el propio usuario (`kyc_view`, self-service, sin staff involucrado) — no es una decisión de compliance, es una acción del propio usuario sobre su propio perfil; auditarla no cierra ningún gap de riesgo real. Si se decide cubrirla, sería un evento de severidad INFO, actor TRADER, sin urgencia.
- Cualquier verificación automatizada de documentos (OCR, verificación de identidad de terceros) — no existe hoy en el sistema, es trabajo de un futuro proveedor de KYC, no de este bloque.
- Reportes regulatorios agregados (SAR, límites por jurisdicción) — mencionados en `docs/AUDIT_TRAIL_ENGINE_PLAN.md` §14 como función futura de Compliance que construye *sobre* `Category.COMPLIANCE` ya wireado, no parte de este bloque.

## 5. Riesgos

| # | Riesgo | Severidad | Mitigación |
|---|---|---|---|
| 1 | **Fuga de datos sensibles de KYC en `metadata`** — nombre legal, país, tipo/número de documento son PII real, a diferencia de los montos/IDs de AUDIT-02 | **Alta si no se aplica disciplina** | Whitelist estricta: `metadata` solo lleva `{"status_before", "status_after", "kyc_profile_id"}`. **Nunca** `document_front`/`document_back`/`selfie` (rutas de archivo o contenido), **nunca** `document_number` completo (si se necesita referenciarlo, solo los últimos 4 caracteres, mismo criterio que `_mask_wallet()`). Test de lista blanca obligatorio antes de cerrar el bloque, igual que en AUDIT-02. |
| 2 | **Confusión de alcance financiero vs. administrativo** — una aprobación de KYC no mueve dinero directamente, pero *habilita* que se mueva después (retiros) | Media | El evento se categoriza `COMPLIANCE`, no `PAYMENTS` — la relación causal con retiros futuros se reconstruye por `user_id` + orden temporal, no fusionando categorías. |
| 3 | **Campo `user` nuevo en `BrokerAuditEvent` podría tentar a usarlo como sustituto de `account`** | Baja | Documentar explícitamente: `account` sigue siendo la FK correcta para todo evento con `TradingAccount` real; `user` es *adicional*, para los dominios (KYC, auth) donde no hay cuenta involucrada. No reemplaza ni deprecia `account`. |
| 4 | **Volumen de reintentos de envío de KYC** (si se decide auditar también el self-service) podría inflar la tabla sin valor de reconstrucción | Baja (mitigado por estar fuera de alcance, ver §4) | No auditar el self-service en este bloque. |
| 5 | **`kyc_emails.py` podría fallar y dejar al staff sin saber si la notificación llegó** | Preexistente, no introducido por AUDIT-03 | Ya manejado con `try/except` + `_wlog.warning()` en el código actual — AUDIT-03 no lo toca, solo agrega el evento de auditoría al lado. |

Ningún riesgo de esta lista es de severidad suficiente para bloquear el inicio del bloque — todos tienen mitigación conocida y ya aplicada exitosamente en AUDIT-02 (fail-open, whitelist, categorías separadas).

## 6. Estrategia de implementación

Mismo ciclo de 4 fases que AUDIT-02, sin desviaciones:

1. **FASE 1 — Auditoría de lectura** (antes de escribir código): releer `admin.py::approve_kyc/reject_kyc`, `KYCProfileAdmin`, `kyc_emails.py`, y los tests existentes de KYC (`grep -rn "KYCProfile" simulator/tests/`) para confirmar que no hay una cuarta función de mutación de `KYCProfile` que se me esté escapando.
2. **FASE 2 — Diseño técnico detallado** (mismo formato que se usó para AUDIT-02 antes de autorizar): archivos exactos, explicación de cada cambio, impacto en tests existentes, tests nuevos propuestos, plan de rollback. Presentado para aprobación **antes** de tocar código.
3. **FASE 3 — Implementación** — migración aditiva (`0054`, un solo campo `user` + su índice), wrapper `record_compliance_event()`, 2 puntos de integración (`approve_kyc`, `reject_kyc`), extensión de admin.
4. **FASE 4 — Autoauditoría técnica final** (mismo checklist de 9 puntos usado para AUDIT-02: idempotencia, fail-open, transacciones/race conditions, privacidad, calidad de código, tests, documentación) antes de recomendar el commit.

**Complejidad estimada: Baja** — comparado con AUDIT-02 (8 puntos de integración, 2 sistemas, correlation_id nuevo), AUDIT-03 es 2 puntos de integración, 1 sistema, sin necesidad de correlation_id nuevo (KYC es una decisión atómica, no una operación multi-request como un payout — `event_version=1` y el `request_id` estándar ya resuelto por `RequestIDMiddleware` son suficientes).

## 7. Lista preliminar de eventos

| `event_type` propuesto | Categoría | Severidad | Actor | Disparador |
|---|---|---|---|---|
| `compliance.kyc_approved` | `COMPLIANCE` | INFO | STAFF | `admin.py::approve_kyc()` |
| `compliance.kyc_rejected` | `COMPLIANCE` | WARNING | STAFF | `admin.py::reject_kyc()` |

Deliberadamente **solo 2** — no se incluye `compliance.kyc_submitted` (self-service, fuera de alcance, ver §4) ni eventos de verificación automatizada (no existen en el sistema hoy). Nomenclatura consistente con el resto de `Category.PAYMENTS`/`RISK` ya existentes (`dominio.accion_pasado`).

## 8. Estimación de bloques internos

No se propone dividir AUDIT-03 en sub-bloques — a diferencia de AUDIT-02 (que tuvo 8 puntos de integración reales frente a los ~4 estimados), el alcance de AUDIT-03 es intrínsecamente pequeño y ya está acotado con precisión en §4/§7. Un solo bloque, una sola migración, una sola ronda de implementación:

| Paso interno | Contenido | Estimación |
|---|---|---|
| 3.1 | Migración `0054`: campo `user` en `BrokerAuditEvent` + índice | Trivial |
| 3.2 | `record_compliance_event()` en `broker_audit.py` + constantes `EV_KYC_APPROVED`/`EV_KYC_REJECTED` | Trivial |
| 3.3 | Integración en `approve_kyc()`/`reject_kyc()` | Baja |
| 3.4 | Extensión de `BrokerAuditEventAdmin` (`user__username` en `search_fields`) | Trivial |
| 3.5 | Tests: shape, actor/severidad, whitelist de privacidad, fail-open, no-duplicación en doble-click de la acción admin | Baja-Media (estimado 8-12 tests nuevos, comparado con los 27 de AUDIT-02) |

## 9. Checklist de entrada para comenzar AUDIT-03

- [x] AUDIT-02 commiteado (`f8fdb8f`) y tageado (`audit-02`, `audit-02-payments-trail-v1`).
- [x] Suite completa en verde al momento del cierre (2919/2919, 3 skipped).
- [x] `docs/AUDIT_TRAIL_ENGINE_PLAN.md` actualizado con el estado as-built de AUDIT-02 (Anexo C).
- [x] Este documento (`docs/AUDIT_03_PREPARATION.md`) revisado y disponible como punto de partida.
- [ ] **Confirmación explícita del usuario** para iniciar la FASE 1 (auditoría de lectura) de AUDIT-03 — no se ha dado todavía; este documento es preparación, no autorización de inicio.
- [ ] Decisión del usuario sobre si el self-service de envío de KYC (`kyc_view`) queda fuera de alcance permanentemente o se agenda para un bloque propio (recomendación de este documento: dejarlo fuera, sin urgencia — ver §4).
- [ ] Confirmar que no hay trabajo en curso de otro bloque (BOOK-04, Liquidity, Dealing Desk híbrido) que toque `admin.py::KYCProfileAdmin` en paralelo, para evitar conflictos de merge.

---

*Documento de preparación — no autoriza ni inicia la implementación de AUDIT-03. Generado inmediatamente después del cierre oficial de AUDIT-02 (commit `f8fdb8f`, 2026-07-21).*
