# BOOK-06 — Release Candidate Closeout

**Fecha:** 2026-07-28
**Commit de cierre:** `3dd271b91b3fa8c8e35434f182a4510fab2c2968` (`book-06h.3` / `book-06h3-canary-observability-v1`) + trabajo de validación de rollback de BOOK-06h.4 (no comiteado al momento de este documento)
**Este documento no modifica ni reemplaza `docs/BOOK06_RC1_AUDIT.md`** — lo cita y lo cierra operativamente.

---

## 1. Resumen de BOOK-06

BOOK-06 construyó, de punta a punta, un motor de clasificación de riesgo interno ("Dealing Desk") que marca posiciones como `is_simulated_hedge` (BOOK-06a-06c), un comparador de exposición observacional (BOOK-06d-06e), y un mecanismo de activación real, controlado por canario, que permite a `validate_new_order()` evaluar los límites de riesgo (RISK-02) contra una exposición ajustada en lugar de la oficial, exclusivamente para cuentas explícitamente autorizadas (BOOK-06g → BOOK-06h.4).

El principio rector del proyecto (nunca modificar una `RoutingDecision`/`LiquidityDecision` existente) se respetó en la totalidad del bloque. El motor de riesgo oficial (RISK-01/RISK-02) permanece sin alteraciones fuera de la única línea de activación autorizada en BOOK-06h.2.

## 2. Bloques implementados

| Bloque | Contenido |
|---|---|
| BOOK-06a | Modelo `DealingDeskDecision` (foundation, satélite, sin escritor) |
| BOOK-06b | `evaluate_dealing_desk_decision()` — motor de clasificación puro |
| BOOK-06c | `record_dealing_desk_decision()` + integración en `consumers.py` |
| BOOK-06d | `calculate_shadow_broker_exposure()` — comparador observacional (Opción B, aislada) |
| BOOK-06e | Vista de observabilidad admin (solo superusuario) |
| BOOK-06f | RFC de política de activación controlada (solo diseño, sin código) |
| BOOK-06g | `exclude_position_ids` en RISK-01 + resolver dormido + retiro de duplicación (Opción A) |
| BOOK-06h.1 | Corrección de alcance del resolver — exclusión escopeada al canario |
| BOOK-06h.2 | Activación real — una línea en `validate_new_order()` |
| BOOK-06h.3 | Observabilidad del canario (log estructurado + indicador admin) — cierra RC-1 F-05 |
| BOOK-06h.4 | Validación exhaustiva de rollback + este cierre documental |

## 3. Commits

| Commit | Tag(s) |
|---|---|
| `baa1161` | `book-06a`, `book-06a-dealing-desk-foundation-v1` |
| `d86e235` | `book-06b`, `book-06b-dealing-desk-decision-engine-v1` |
| `ba06751` | `book-06c`, `book-06c-dealing-desk-integration-writer-v1` |
| `d80af49` | `book-06d`, `book-06d-shadow-exposure-consumer-v1` |
| `35c86b1` | `book-06e`, `book-06e-shadow-exposure-observability-v1` |
| `ae9962c` | `book-06g`, `book-06g-controlled-activation-foundation-v1` |
| `83b84bc` | `book-06h.1`, `book-06h1-resolver-canary-scope-v1` |
| `fd50c2f` | `book-06h.2`, `book-06h2-real-activation-integration-v1` |
| `3dd271b` | `book-06h.3`, `book-06h3-canary-observability-v1` |
| *(pendiente)* | BOOK-06h.4 — tests de rollback + `docs/BOOK06_CANARY_ROLLBACK_RUNBOOK.md` + este documento, no comiteados aún |

BOOK-06f no generó commit (RFC de diseño únicamente, sin código).

## 4. Tags

Ver columna "Tag(s)" de la Sección 3. Todos verificados apuntando a su commit correspondiente al momento de su cierre respectivo.

## 5. Cobertura

**126 tests dedicados a BOOK-06**, distribuidos en 10 archivos:

| Archivo | Tests |
|---|---:|
| `test_book06a_dealing_desk_foundation.py` | 24 |
| `test_book06b_dealing_desk_decision_engine.py` | 14 |
| `test_book06c_dealing_desk_integration_open.py` | 11 |
| `test_book06d_shadow_exposure_consumer.py` | 12 |
| `test_book06e_shadow_exposure_observability.py` | 10 |
| `test_book06g_controlled_activation_foundation.py` | 18 |
| `test_book06h1_resolver_canary_scope.py` | 5 |
| `test_book06h2_real_activation_integration.py` | 8 |
| `test_book06h3_canary_observability.py` | 11 |
| `test_book06h4_rollback_closure.py` | 13 |
| **Total** | **126** |

**Suite completa del proyecto:** 3452 tests, `OK (skipped=4)` — sin ninguna regresión atribuible a BOOK-06 en ningún punto de su desarrollo.

BOOK-06h.4 añadió cobertura específica de rollback: OFF→ON→OFF idéntico, snapshot oficial idéntico antes/durante/después, ausencia de caché/estado global/atributos persistentes, secuencias de allowlist en distinto orden, alternancia OFF/ON ×5, fallback→rollback→operación normal, e indicador visual de BOOK-06e sin inconsistencias al alternar.

## 6. Hallazgos RC-1

Ver `docs/BOOK06_RC1_AUDIT.md` para el detalle completo. Resumen: **0 BLOCKER · 1 HIGH · 4 MEDIUM · 5 LOW · 6 INFO** (15 hallazgos, incluyendo una autocorrección matemática sobre un RFC previo).

| ID | Severidad | Estado tras BOOK-06h.3/h.4 |
|---|---|---|
| F-01 | HIGH | Sin cambios de código posibles — requiere reconocimiento explícito de negocio/riesgo (ver Sección 7) |
| F-02 | MEDIUM | Sin cambios — riesgo latente documentado, sin impacto actual (ningún caller lee `exposure_after` hoy) |
| F-03 | MEDIUM | Sin cambios — ventana de consistencia eventual, dirección conservadora, documentada en el runbook |
| F-04 | LOW | Sin cambios de código; el runbook (Sección 4) aclara explícitamente la diferencia de alcance entre el indicador de BOOK-06e y el shadow global |
| F-05 | MEDIUM | **Cerrado en BOOK-06h.3** — log estructurado + indicador admin, ambos verificados por 24 tests (06h.3 + 06h.4) |
| F-06 | LOW | Sin cambios — `.env.example` sigue sin documentar las dos variables |
| F-07 | LOW | Sin cambios — comentario de `settings.py` sigue desactualizado (no se tocó `settings.py` en ningún momento de h.1-h.4, por regla explícita de alcance) |
| F-08 | LOW | Parcialmente atendido — el indicador de BOOK-06e (F-05) también cierra la falta de indicador operativo visible |
| F-09 | INFO | Corrección ya incorporada al RC-1, sin cambios adicionales |
| F-10 a F-14 | INFO | Sin cambios — características ya documentadas, ninguna requiere acción |

## 7. Condiciones cumplidas

- **F-05 (MEDIUM) cerrado**: observabilidad del canario implementada y probada (BOOK-06h.3).
- **Reinicio de proceso documentado**: `docs/BOOK06_CANARY_ROLLBACK_RUNBOOK.md`, Sección 6, deja explícito que tanto la activación como el rollback requieren reinicio.
- **Rollback verificado exhaustivamente**: 13 tests nuevos (BOOK-06h.4) demuestran, con ejecución real, que OFF→ON→OFF restaura el comportamiento oficial exacto, que no queda estado global/caché/atributos persistentes, y que la alternancia repetida produce resultados consistentes en cada estado equivalente.
- **F-07 (LOW, cosmético)**: no se corrigió — fuera del alcance explícito de BOOK-06h.1 a h.4 (ninguna subfase autorizó tocar `trx_simulator/settings.py`). Queda como acción recomendada de bajo impacto, no bloqueante.

## 8. Condiciones pendientes

- **F-01 (HIGH) — reconocimiento explícito de negocio/riesgo**: el RC-1 exige que el comportamiento no-monotónico de `MAX_NET_NOTIONAL` (verificado empíricamente: un PASS oficial puede convertirse en REJECT ajustado para la cuenta canario en libros direccionalmente sesgados) sea reconocido y aceptado explícitamente antes de activar cualquier flag en un entorno real. **Esto no ha ocurrido de forma explícita en esta conversación** — la revisión y aprobación de BOOK-06h.1/h.2/h.3/h.4 cubrió la implementación técnica de cada subfase, no una declaración explícita de aceptación del hallazgo F-01 en sí.
- **Preparación operativa de la cuenta canario real**: el RC-1 exige identificar una cuenta interna específica y confirmar que genera al menos una `DealingDeskDecision(is_simulated_hedge=True)` real antes de considerar iniciado un trial. Esto es un paso operativo fuera del alcance de este repositorio de código y no se ha realizado.
- **F-06/F-07 (LOW)**: documentación cosmética pendiente (`.env.example`, comentario de `settings.py`) — no bloqueante, recomendado para higiene operativa.

## 9. Decisión final

**READY FOR INTERNAL CANARY**

La ingeniería de BOOK-06 está finalizada. Todos los bloques BOOK-06h.1 a BOOK-06h.4 fueron completados: corrección de alcance del resolver, activación real en `validate_new_order()`, observabilidad del canario (cierra F-05), y validación exhaustiva de rollback con evidencia ejecutable. La suite completa de pruebas está en verde: 126 tests propios de BOOK-06 y 3452 tests en la suite completa del proyecto, sin ninguna regresión.

Las condiciones pendientes listadas en la Sección 8 son **exclusivamente operativas** — reconocimiento de negocio/riesgo, configuración de cuenta, activación de flags, ejecución del runbook — y **no representan defectos de implementación**. Ninguna acción de ingeniería adicional es necesaria antes de iniciar el canario.

## Operational Prerequisites

1. Aprobación formal del hallazgo F-01 (`MAX_NET_NOTIONAL` no-monotónico) por el responsable de negocio/riesgo.
2. Configuración de la cuenta canario interna.
3. Activación manual de los feature flags correspondientes.
4. Ejecución completa de `docs/BOOK06_CANARY_ROLLBACK_RUNBOOK.md` antes de iniciar el canario.

---

*Este documento no reemplaza ni modifica `docs/BOOK06_RC1_AUDIT.md`. Ningún archivo de producción fue modificado durante BOOK-06h.4. Ningún flag fue activado de forma permanente. No se realizó commit ni push.*
