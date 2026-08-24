# BOOK-06 — Release Candidate Audit RC-1

**Fecha:** 2026-07-28
**Alcance:** BOOK-06a → BOOK-06h.2 (Dealing Desk Foundation → Real Activation Integration)
**Commit auditado:** `fd50c2f03d41198b30bb654f6be388e625c057fb` (rama `main`, árbol limpio, sin push)
**Autor:** Auditoría técnica realizada por Claude Code, a petición del propietario del proyecto.
**Regla de la fase:** solo auditoría y documentación. Ningún hallazgo fue corregido durante esta fase. Ningún flag fue activado. No hubo commit ni push.

---

## 1. Executive Summary

BOOK-06 introduce un motor de clasificación de riesgo interno ("Dealing Desk") que marca posiciones como `is_simulated_hedge` y permite, de forma opcional y controlada por un canario explícito (`DEALING_DESK_EXPOSURE_ENABLED` + `DEALING_DESK_EXPOSURE_ACCOUNT_IDS`), que `validate_new_order()` evalúe los límites de riesgo (RISK-02) contra una exposición ajustada (excluyendo esas posiciones) en lugar de la exposición oficial (RISK-01).

La arquitectura es sólida: separación estricta de responsabilidades entre clasificación (`dealing_desk.py`), cálculo (`broker_exposure.py`/`broker_risk_shadow.py`) y activación (`broker_risk.py`), principio rector respetado (ninguna `RoutingDecision`/`LiquidityDecision` se modifica jamás), fail-safe verificado en cada capa, y 102 tests dedicados a BOOK-06 (todos en verde, 3428/3428 en la suite completa).

Esta auditoría identificó **0 BLOCKER**, **1 HIGH**, **4 MEDIUM**, **5 LOW** y **6 INFO**. El hallazgo HIGH no es un defecto de código sino una propiedad matemática real y verificada empíricamente: excluir una posición marcada como cobertura simulada de una cuenta canario puede, en libros direccionalmente sesgados, convertir un `PASS` de `MAX_NET_NOTIONAL` en `REJECT` — el mecanismo no es uniformemente "más permisivo", como podría asumirse intuitivamente. Este hallazgo requiere reconocimiento explícito de negocio/riesgo antes de activar el canario, no una corrección de código.

Un hallazgo previo de mi propio RFC de BOOK-06h (que `pricing_coverage_pct` también podía flipear PASS→REJECT vía exclusión) fue **re-derivado matemáticamente y verificado empíricamente como FALSO** en esta auditoría — se documenta la corrección en la Sección 6.

**Veredicto: READY WITH CONDITIONS.**

---

## 2. Scope

Incluye: `simulator/dealing_desk.py`, `simulator/broker_exposure.py`, `simulator/broker_risk_shadow.py`, `simulator/broker_risk.py`, `simulator/models.py` (`DealingDeskDecision`, `BrokerRiskLock`), `simulator/consumers.py` (bloque de integración BOOK-06c), `simulator/admin.py` (BOOK-06e), `trx_simulator/settings.py` (flags BOOK-06g), y los 8 archivos de tests BOOK-06a→06h.2 (102 tests).

Excluye explícitamente (por regla de esta fase): cualquier cambio de código, migración, activación de flag, commit o push. No se revisó `treasury_engine` ni `mi_broker` (fuera del alcance autorizado).

---

## 3. Current Baseline

- Commit: `fd50c2f03d41198b30bb654f6be388e625c057fb`, rama `main`, árbol limpio, 40 commits por delante de `origin/main`, sin push en ningún momento del proyecto.
- Suite: 3428 tests, `OK (skipped=4)`.
- Flags: `DEALING_DESK_EXPOSURE_ENABLED=False` (default), `DEALING_DESK_EXPOSURE_ACCOUNT_IDS` vacío (default).
- Historial completo de commits de BOOK-06 (6 commits, todos ya cerrados y etiquetados):

| Commit | Tag(s) | Contenido |
|---|---|---|
| `baa1161` | `book-06a` | Modelo `DealingDeskDecision` (foundation) |
| `d86e235` | `book-06b` | `evaluate_dealing_desk_decision()` (motor puro) |
| `ba06751` | `book-06c` | `record_dealing_desk_decision()` + integración en `consumers.py` |
| `d80af49` | `book-06d` | `calculate_shadow_broker_exposure()` (Opción B, aislada) |
| `35c86b1` | `book-06e` | Vista de observabilidad (superusuario) |
| `ae9962c` | `book-06g` | `exclude_position_ids` en RISK-01 + resolver dormido + Opción A |
| `83b84bc` | `book-06h.1` | Corrección de alcance del resolver (scoping al canario) |
| `fd50c2f` | `book-06h.2` | Activación real: una línea en `validate_new_order()` |

Diff acumulado de todo BOOK-06 (`baa1161^..HEAD`): **18 archivos, +2786/-2 líneas.** Ningún archivo fuera de esta lista fue tocado — en particular, **ningún archivo de `pnl_engine.py`, `broker_pnl.py`, `broker_ledger.py`, `liquidity_ledger.py`, `risk_engine.py` o los campos `balance`/`equity`/`margin` de `TradingAccount`** aparece en este diff.

---

## 4. Architecture Review

Flujo exacto, extremo a extremo, para una orden nueva contra una cuenta dentro del canario:

```
1. TradingConsumer._order_new() adquiere BrokerRiskLock (select_for_update,
   PRIMERO en el orden global: BrokerRiskLock → TradingAccount → Position)
   dentro de _db_open_position_atomic()'s transaction.atomic().
        │
2. validate_new_order(account_id, symbol, side, qty, price, contract_size)
   se ejecuta DENTRO de esa misma transacción/lock (broker_risk.py:492).
        │
3. book = _resolve_broker_exposure_for_validation(account_id)  [broker_risk.py:521]
        │
        ├─ _should_use_dealing_desk_adjusted_exposure(account_id) [broker_risk.py:411]
        │      flag OFF, o account_id no está en el allowlist  → False
        │      flag ON  y account_id SÍ está en el allowlist   → True
        │
        ├─ False → book = broker_exposure.broker_exposure_snapshot()   [oficial, RISK-01 sin cambios]
        │
        └─ True  → intenta:
               excluded_ids = DealingDeskDecision.objects.filter(
                   is_simulated_hedge=True,
                   routing_decision__account_id__in=DEALING_DESK_EXPOSURE_ACCOUNT_IDS,
               ).exclude(position_id__isnull=True).values_list("position_id", flat=True)
               book = broker_exposure.calculate_broker_exposure(exclude_position_ids=excluded_ids)
               │
               └─ cualquier excepción → log.error(...) → fallback a broker_exposure_snapshot()
        │
4. validate_symbol_limit() / validate_account_limit()  — NUNCA leen `book`,
   siempre oficiales (broker_exposure_for_symbol/_for_account directos),
   sin importar el canario.
        │
5. validate_total_limit(book) / validate_position_limit(book)  — SÍ leen
   `book` — aquí es donde el ajuste del canario realmente cambia el resultado.
        │
6. RiskLimitDecision (allowed, reason_code, risk_checks, exposure_after,
   margin_after) es devuelto a consumers.py, que solo lee .allowed/
   .reason_code/.reason_message.
        │
7. Si allowed → Position.objects.create() → transacción commitea → BrokerRiskLock liberado.
        │
8. DESPUÉS del commit (fuera del lock, paso separado): 
   TradingConsumer._db_record_dealing_desk_decision() resuelve routing_profile
   (TraderScore, fallback "INTERNAL") → evaluate_dealing_desk_decision() (puro)
   → record_dealing_desk_decision() → DealingDeskDecision.objects.create()
   (nested transaction.atomic(), fail-open, nunca interrumpe el order_ack).
```

**Puntos arquitectónicos verificados por lectura directa del código:**

- `dealing_desk.py::evaluate_dealing_desk_decision()` es puro (sin DB, sin imports de `simulator.models`) — confirmado por `ZeroQueriesTests.test_zero_database_queries` (BOOK-06b).
- `broker_exposure.calculate_broker_exposure()` sigue siendo la ÚNICA fórmula de agregación — `broker_risk_shadow.py` (BOOK-06d/06g) y el resolver de `broker_risk.py` (BOOK-06h) delegan ambos en ella, nunca la reimplementan. La duplicación temporal de BOOK-06d (Opción B) fue retirada en BOOK-06g (Opción A) exactamente como estaba planeado.
- `validate_new_order()` tiene **un único cambio funcional en toda su historia BOOK-06**: la línea `book = _resolve_broker_exposure_for_validation(account_id)` (antes `book = _exposure.broker_exposure_snapshot()`), confirmado línea por línea en el diff de BOOK-06h.2 y por el test estructural `test_only_functional_change_is_the_book_resolution_line`.

---

## 5. Functional Correctness

Cada afirmación se confirmó leyendo el código y ejecutando (o citando) el test correspondiente:

| # | Afirmación | Verificado por | Resultado |
|---|---|---|---|
| 1 | Flag OFF conserva comportamiento oficial | `test_flag_off_identical_result` (06h.2) | ✅ Confirmado — `exposure_after` idéntico al oficial |
| 2 | Flag ON + allowlist vacío conserva comportamiento oficial | `test_flag_on_empty_allowlist_identical_result` (06h.2) | ✅ Confirmado |
| 3 | Cuenta fuera del allowlist conserva comportamiento oficial | `test_account_outside_canary_identical_result` (06h.2) | ✅ Confirmado |
| 4 | Cuenta dentro del allowlist usa exposición ajustada | `test_inside_canary_with_excludable_positions_uses_adjusted_book` (06h.2) | ✅ Confirmado numéricamente (excluye exactamente la cantidad marcada) |
| 5 | Fallback ante excepción usa cálculo oficial | `test_resolver_exception_falls_back_to_official_calculation` (06h.2), `test_resolver_failure_falls_back_to_official_never_raises` (06g) | ✅ Confirmado, nunca lanza |
| 6 | Posiciones no elegibles siguen incluidas | `test_inside_canary_no_excludable_positions_identical_result` (06h.2) | ✅ Confirmado |
| 7 | Posiciones elegibles se excluyen únicamente para cuentas canario | `test_exclusion_scoped_to_allowlisted_accounts_only`, `test_non_canary_hedge_position_never_excluded_even_with_gate_true_elsewhere` (06h.1) | ✅ Confirmado — corrección de BOOK-06h.1 sobre el defecto de BOOK-06g |
| 8 | No existe doble exclusión | Construcción: `frozenset` + membership test (`p.id not in exclude_position_ids`), no un contador. `test_duplicate_decisions_for_same_position_deduplicated` (06h.1) | ✅ Estructuralmente imposible + verificado |
| 9 | No existe exclusión global accidental | `test_exclusion_scoped_to_allowlisted_accounts_only` (06h.1) — corrige exactamente el defecto que SÍ existía en BOOK-06g antes de 06h.1 | ✅ Corregido y verificado |
| 10 | No existe contaminación entre cuentas | Ídem punto 7/9 | ✅ Confirmado |

**Nota importante:** `validate_symbol_limit()` y `validate_account_limit()` (`broker_risk.py:179-213`) **nunca leen `book`** — llaman directamente a `broker_exposure_for_symbol()`/`broker_exposure_for_account()`, sin pasar por el resolver. Estos dos límites son, por construcción, **inmunes al canario en todo momento**, con flag ON o OFF.

---

## 6. Mathematical Risk Review

### 6.1 Campos no afectados por la exclusión (monotónicamente no-crecientes)

`MAX_TOTAL_BROKER_EXPOSURE`, `MAX_LONG_EXPOSURE`, `MAX_SHORT_EXPOSURE`, `MAX_GROSS_NOTIONAL`, `MAX_OPEN_POSITIONS_BROKER_WIDE`, `MAX_SYMBOL_EXPOSURE`, `MAX_ACCOUNT_EXPOSURE`, `MAX_POSITION_SIZE` — excluir posiciones solo puede mover estos de REJECT→PASS, nunca al revés (los dos últimos ni siquiera son afectados, ver Sección 5).

### 6.2 HIGH — `MAX_NET_NOTIONAL` es NO monotónico (verificado empíricamente)

Se construyó y ejecutó un escenario real (test desechable, eliminado tras la verificación, no forma parte de la suite permanente):

- Libro oficial: 600 lotes BUY @ 100 (long_notional=60,000) + 250 lotes SELL @ 100 de una cuenta no-canario (short_notional=25,000) + **100 lotes SELL @ 100 de la cuenta canario, marcados `is_simulated_hedge=True`** (short_notional=10,000).
- **Oficial:** `net_notional = 60,000 − 35,000 = 25,000` → con `MAX_NET_NOTIONAL=30,000` → **PASS**.
- **Ajustado (canario activo, excluye los 100 lotes SELL marcados):** `net_notional = 60,000 − 25,000 = 35,000` → **FAIL** (supera el límite).
- Resultado confirmado por ejecución real: `official.allowed == True`, `adjusted.allowed == False`.

**Mecanismo:** excluir una posición SHORT de un libro sesgado a LONG reduce el lado corto, lo que *aumenta* `abs(net_notional)`, no lo reduce. El canario puede, en escenarios direccionales específicos, **rechazar órdenes que el cálculo oficial habría aceptado** — el efecto no es uniformemente "más permisivo". Esto es una propiedad matemática inherente al *netting*, no un defecto de implementación.

**Impacto:** cambia el comportamiento trader-facing específicamente para cuentas canario bajo condiciones direccionales concretas. **Requiere reconocimiento explícito de negocio/riesgo antes de activar el canario** (ver Sección 16).

### 6.3 CORRECCIÓN — `pricing_coverage_pct` NO puede flipear PASS→REJECT vía exclusión (el RFC de BOOK-06h estaba equivocado en este punto)

El RFC previo (BOOK-06h, aprobado por el usuario) afirmó que excluir una posición con precio fresco podía reducir `pricing_coverage_pct` por debajo de 100% y disparar el gate fail-closed. **Esta auditoría re-derivó la matemática y la verificó empíricamente: la afirmación es falsa.**

Sea `P` = posiciones con precio, `T` = posiciones totales, `k` posiciones excluidas de las cuales `p_ex` tenían precio. La razón después de excluir es `(P−p_ex)/(T−k)`. Álgebra: esta razón es **menor** que `P/T` si y solo si `p_ex/k > P/T`. Como `p_ex ≤ k` siempre, `p_ex/k ≤ 1`. Si el libro oficial ya tiene `P/T = 1` (100% — la única situación en la que el check de precio pasaría), entonces `p_ex/k ≤ 1 = P/T` **siempre**, nunca estrictamente mayor → la razón **no puede** bajar de 100%. Y si `P/T < 1` (cobertura oficial ya incompleta), la orden **ya está rechazada** por esa razón, antes y con independencia de cualquier exclusión — no hay "flip" que atribuir a BOOK-06.

Verificado empíricamente: cuenta canario con 2 posiciones, ambas con precio fresco → oficial `pricing_coverage_pct=100.00`, ajustado (excluyendo 1) → `pricing_coverage_pct=100.00` (se mantiene). Confirmado con `assertEqual`.

**Esta corrección debe reemplazar la afirmación equivalente del RFC de BOOK-06h en cualquier referencia futura.**

### 6.4 `exposure_after` no es invariante bajo el canario (MEDIUM, ver Sección 15, Hallazgo F-02)

`exposure_after` (`broker_risk.py:546`) se calcula a partir de `total_check.current_value`, que a su vez proviene de `book.gross_quantity` — el mismo `book` que el resolver puede haber ajustado. Es decir: cuando el canario está activo, `exposure_after` refleja la exposición AJUSTADA, no la oficial. Ver Hallazgo F-02.

### 6.5 Posiciones sin precio, NULL, duplicadas

- Posiciones sin precio: excluidas de `gross_notional`/`net_notional`/PnL pero SIEMPRE contadas en `open_position_count` (broker_exposure.py:346-350) — sin cambios de BOOK-06.
- `position_id NULL`: excluido explícitamente vía `.exclude(position_id__isnull=True)` — verificado por `test_decision_with_null_position_is_safely_ignored` (06h.1).
- `routing_decision NULL`: nunca puede hacer match con `routing_decision__account_id__in=...` — verificado por `test_decision_with_null_routing_decision_never_matches_allowlist` (06h.1) — la posición permanece incluida (comportamiento conservador correcto).
- Decisiones duplicadas para la misma posición: colapsan vía `frozenset()` — verificado por `test_duplicate_decisions_for_same_position_deduplicated` (06h.1).

---

## 7. Concurrency and Consistency

- `BrokerRiskLock` se adquiere PRIMERO (orden global `BrokerRiskLock → TradingAccount → Position`), sin cambios — el resolver corre DENTRO de la misma transacción/lock que ya protegía `validate_new_order()` antes de BOOK-06. No se introdujo ningún lock nuevo, ningún cambio de orden.
- La query del resolver (`DealingDeskDecision.objects.filter(...)`) es una lectura simple sin `select_for_update()` — correcto: es una lectura consistente dentro de la misma transacción que ya lee `Position` sin lock adicional (mismo nivel de aislamiento que el resto de `calculate_broker_exposure()`).

**MEDIUM — Ventana de consistencia eventual entre apertura de posición y escritura de `DealingDeskDecision` (Hallazgo F-03):**

Confirmado por lectura de `consumers.py:1372-1416` y el propio docstring de `dealing_desk.py`: `DealingDeskDecision` se escribe **DESPUÉS** de que `_db_open_position_atomic()` ya comiteó y liberó `BrokerRiskLock` — es un paso separado, fuera de la transacción y del lock originales. Esto crea una ventana real (típicamente milisegundos) en la que:

- Una posición recién abierta de una cuenta canario ya existe en la tabla `Position` (y por tanto ya cuenta en la exposición bruta oficial).
- Su `DealingDeskDecision` correspondiente aún no existe.
- Una orden CONCURRENTE que dispare `validate_new_order()` durante esa ventana **no verá esa posición como excluible todavía**.

**Dirección del efecto:** conservadora, nunca insegura — la posición temporalmente "no excluida" se cuenta como si fuera oficial, lo cual es el estado MÁS estricto, no menos. Se autocorrige en la siguiente llamada una vez que el `DealingDeskDecision` se persiste. Mismo principio ya usado en el proyecto para justificar por qué los cierres no serializan contra `BrokerRiskLock` ("solo reduce exposición, nunca la aumenta").

**Recomendación:** documentar esta ventana explícitamente en el runbook del canario como comportamiento esperado, no como un defecto si se observa durante el ensayo.

- Idempotencia: no existe restricción de unicidad en `DealingDeskDecision` (Opción A, mismo precedente que BOOK-05d) — dos escrituras para la misma `RoutingDecision` producirían dos filas, pero el resolver las deduplica vía `frozenset` al leer, así que esto no afecta la corrección de la exclusión, solo el volumen de filas históricas.
- Consistencia decisión/posición/routing_decision: `DealingDeskDecision.position`/`routing_decision` usan `on_delete=SET_NULL` (nunca `CASCADE`) — la fila de decisión sobrevive intacta al cierre de la posición o (en teoría) al borrado de la `RoutingDecision`, preservando el historial de auditoría. Confirmado por `DeleteSemanticsTests` (BOOK-06a, 3 tests).
- **¿Puede el resolver leer un estado parcial o inconsistente?** No de forma insegura: en el peor caso (la ventana descrita arriba) lee un estado *más conservador* que el final, nunca uno que subestime el riesgo real.

---

## 8. Performance Review

Medido empíricamente (no estimado) con `CaptureQueriesContext` y `bulk_create`, usando un test desechable eliminado tras la medición (no forma parte de la suite permanente; `git status` limpio confirmado después de eliminarlo):

| Posiciones abiertas (broker-wide) | Queries, flag OFF | Queries, flag ON (cuenta canario) | Tiempo, flag OFF | Tiempo, flag ON |
|---:|---:|---:|---:|---:|
| 100 | 5 | 6 | 0.020 s | 0.008 s |
| 1,000 | 5 | 6 | 0.100 s | 0.058 s |
| 10,000 | 5 | 6 | 1.090 s | 0.624 s |
| 100,000 | 5 | 6 | 12.149 s | 6.907 s |

**Hallazgos:**

- El **conteo de queries es constante** (5 sin canario, 6 con canario activo) en las 4 escalas medidas — el overhead de BOOK-06 es exactamente **+1 query fija** (el batch de `DealingDeskDecision`), nunca por posición. Coincide exactamente con la estimación del RFC de BOOK-06h.
- El **tiempo de ejecución escala linealmente con el número de posiciones abiertas del broker completo** (10-12 segundos por validación a 100,000 posiciones) — esto es una característica **preexistente de RISK-01** (`calculate_broker_exposure()` carga y itera en Python cada posición abierta del broker en cada llamada; ver `broker_exposure.py:282`), **no introducida por BOOK-06**. BOOK-06 solo añade una query fija y adicional sobre ese costo ya existente — a 100,000 posiciones, la query extra es <1% del tiempo total.
- **Índices:** `is_simulated_hedge` no tiene índice dedicado, pero la query del resolver está acotada por `routing_decision__account_id__in=<allowlist>`, que SÍ usa el índice automático de Django sobre la FK `RoutingDecision.account` — el costo real de la query escala con el volumen de decisiones del canario (1-2 cuentas), no con el tamaño total de la tabla `DealingDeskDecision`.
- **Tamaño del `frozenset`:** acotado por el número de posiciones marcadas `is_simulated_hedge=True` pertenecientes exclusivamente a las cuentas del allowlist — para un canario de 1-2 cuentas, del orden de decenas a cientos de elementos, trivial en memoria.
- No se realizó ninguna optimización durante esta fase (regla explícita de la auditoría).

**Conclusión de rendimiento:** el techo de escalabilidad a 100,000 posiciones es un riesgo arquitectónico preexistente de RISK-01/RISK-02, no de BOOK-06, y está muy lejos del volumen realista de un canario interno de 1-2 cuentas. No bloquea el canario propuesto en esta auditoría, pero se documenta como nota arquitectónica para revisión futura fuera de BOOK-06.

---

## 9. Fail-safe and Rollback

- Confirmado: cualquier excepción dentro de `_resolve_broker_exposure_for_validation()` (incluida una excepción simulada en la query de `DealingDeskDecision.objects.filter`) cae al `broker_exposure_snapshot()` oficial — `test_resolver_exception_falls_back_to_official_calculation` (06h.2), `test_resolver_failure_falls_back_to_official_never_raises` (06g). El error se registra vía `log.error(..., exc_info=True)` y **nunca interrumpe la orden**.
- El flag permite desactivar el sistema **sin deploy** (cambio de variable de entorno) — pero **SÍ requiere reinicio de proceso**: `DEALING_DESK_EXPOSURE_ENABLED`/`DEALING_DESK_EXPOSURE_ACCOUNT_IDS` se leen vía `os.getenv()` una sola vez, en el momento en que `trx_simulator/settings.py` se importa (arranque del proceso Daphne/gunicorn) — no hay recarga en caliente. **Este es el mismo comportamiento que ya rige `LIQUIDITY_ENGINE_ENABLED`/`ROUTING_ENGINE_ENABLED`**, no una particularidad nueva de BOOK-06.
- Vaciar el allowlist neutraliza el canario de inmediato (tras el reinicio) — el gate (`_should_use_dealing_desk_adjusted_exposure`) trata un allowlist vacío como "ninguna cuenta califica", incluso con el flag maestro en `True` (semántica deliberadamente invertida respecto a BOOK-04f, verificada por `test_empty_allowlist_with_flag_on_is_still_false`, 06g).
- No existen capas de caché (Redis/memcache/etc.) en la ruta del resolver ni en `calculate_broker_exposure()` — cada llamada es una lectura fresca de la base de datos. No hay riesgo de "caché retrasando el rollback".
- El rollback (desactivar el flag) **no altera datos históricos**: las filas de `DealingDeskDecision` ya escritas permanecen exactamente como están — desactivar el flag solo detiene su USO futuro en la resolución de exposición, nunca borra ni modifica lo ya persistido (principio rector del proyecto, respetado en su totalidad).

---

## 10. Observability

**Lo que existe hoy:**

- `log.error("[broker_risk] dealing desk exposure resolution failed for account=%s: %r", ...)` (`broker_risk.py:482`) — se emite únicamente en la ruta de FALLO/fallback.
- `log.info("[dealing_desk] decision=%s symbol=%s is_simulated_hedge=%s routing_decision_id=%s liquidity_decision_id=%s ...")` (`dealing_desk.py:215`) — se emite en cada escritura de `DealingDeskDecision` (éxito o marcado False), pero no indica si esa decisión terminó siendo usada para excluir exposición en una validación posterior.
- Vista de observabilidad BOOK-06e (`admin/shadow-exposure/`, superusuario únicamente) — pero esta calcula el **shadow GLOBAL** (`calculate_shadow_broker_exposure()`, sin scope de cuenta), una lente distinta de la resolución REAL y acotada al canario que usa `broker_risk.py` (ver Hallazgo F-04).

**MEDIUM — Falta un log en la ruta de ÉXITO del canario (Hallazgo F-05):**

No existe ninguna línea de log que indique "cuenta=X evaluada con el libro AJUSTADO, N posiciones excluidas, notional excluido=Y, decisión final=PASS/REJECT". Hoy, la única señal observable en logs es el fallo/fallback — **no hay forma de confirmar, solo mirando logs, que el canario efectivamente se usó durante un trial real**, ni con qué frecuencia. Esto es un vacío de observabilidad real que debe cerrarse (o aceptarse explícitamente para un primer trial muy corto y muy vigilado) antes de un canario real — ver Sección 16.

**Tiempo de ejecución:** no se registra específicamente el tiempo del resolver (aunque la Sección 8 demuestra que es despreciable frente al costo total de `calculate_broker_exposure()`).

**Razón del rechazo:** ya se registra correctamente vía `_risk02.reason_code`/`reason_message` en el evento de auditoría `EV_RISK_ORDER_REJECTED` (`consumers.py:3087-3098`) — sin cambios de BOOK-06, cubre tanto el camino oficial como el ajustado indistintamente (correcto, ya que la decisión PASS/REJECT es lo que realmente importa auditar, sin importar qué libro la produjo).

---

## 11. Security

- Ningún dato sensible en exceso: los logs de BOOK-06 solo incluyen `account_id` (entero interno), `symbol` (string), `decision_id` (UUID), conteos y montos agregados — nunca nombres, emails, direcciones IP, ni tokens.
- `account_id` se maneja como identificador interno numérico en todo momento, igual que en el resto del proyecto (RISK-01/RISK-02/BOOK-04/05).
- `settings.py` no expone secretos — solo dos valores booleanos/numéricos derivados de variables de entorno de servidor.
- El allowlist **no puede ser manipulado por usuarios normales**: se lee exclusivamente de `os.getenv()` en el arranque del proceso — no existe ningún endpoint, vista, serializer o formulario de admin que escriba estos dos settings (confirmado por búsqueda exhaustiva en todo el árbol de `simulator/`). La única superficie de admin relacionada (BOOK-06e) es de **solo lectura** y restringida a `is_superuser`.
- **LOW — `.env.example` no documenta las dos variables** (Hallazgo F-06) — mismo gap preexistente que ya afecta a `LIQUIDITY_ENGINE_ENABLED`/`ROUTING_ENGINE_ENABLED` (no es una regresión introducida por BOOK-06, pero vale la pena cerrarla antes del canario para claridad operativa).

---

## 12. Test Coverage Matrix

102 tests dedicados a BOOK-06 en 8 archivos, todos en verde. Matriz de cobertura funcional:

| Componente | Comportamiento | Test(s) | Cobertura |
|---|---|---|---|
| `DealingDeskDecision` (modelo) | Creación, campos, índices, ordering, sin `inputs_snapshot`, sin unique constraint | `test_book06a_dealing_desk_foundation.py` (9 tests, `DealingDeskDecisionModelTests`) | Suficiente |
| `DealingDeskDecision` (borrado) | `SET_NULL` en cascada desde `RoutingDecision`/`Position`/`LiquidityDecision` | `DeleteSemanticsTests` (3 tests) | Suficiente |
| Principio rector | Crear una `DealingDeskDecision` nunca modifica `RoutingDecision`/`LiquidityDecision`/`Book.ALL` | `UpstreamDecisionsNeverModifiedTests` (3 tests) | Suficiente |
| Admin (BOOK-06a) | Registro, solo lectura, sin add/change/delete | `DealingDeskDecisionAdminTests` (7 tests) | Suficiente |
| `evaluate_dealing_desk_decision()` | Las 4 ramas de la regla, tipos inválidos, excepciones, determinismo, `qualifying_profiles` custom | `test_book06b_dealing_desk_decision_engine.py` (14 tests) | Suficiente |
| `evaluate_dealing_desk_decision()` | Cero queries (función pura) | `ZeroQueriesTests` | Suficiente |
| `record_dealing_desk_decision()` + integración `_order_new()` | Escritura, fail-open ante fallo del motor/writer, sin duplicados por defecto, independencia de `LiquidityDecision`, fallback a "INTERNAL", cliente recibe ack/fill siempre | `test_book06c_dealing_desk_integration_open.py` (11 tests) | Suficiente |
| `calculate_shadow_broker_exposure()` | Exclusión global, sin decisión = cuenta como actual, fail-open, filtros, query count fijo | `test_book06d_shadow_exposure_consumer.py` (12 tests) | Suficiente |
| Vista de observabilidad BOOK-06e | Permisos (`is_superuser`/403/redirect), métricas, filtros, fail-open→200, cero escrituras | `test_book06e_shadow_exposure_observability.py` (10 tests) | Suficiente |
| `exclude_position_ids` (RISK-01) | Default None, set vacío, exclusión específica, id inexistente, paridad con shadow | `CalculateBrokerExposureExclusionTests` (5 tests) | Suficiente |
| `broker_risk_shadow` → Opción A | Misma fórmula subyacente, `excluded_position_count` respeta filtros | `ShadowCalculatorDelegatesToOfficialFormulaTests` (2 tests) | Suficiente |
| Gate `_should_use_dealing_desk_adjusted_exposure` | Flag off, dentro/fuera de allowlist, allowlist vacío+flag on, configuración inválida | `DealingDeskExposureGateTests` (5 tests) | Suficiente |
| Resolver (BOOK-06g, dormido) | Gate false→oficial sin query, gate true→exclusión, fallo→oficial | `DealingDeskExposureResolverTests` (3 tests) | Suficiente |
| `validate_new_order()` estructural | Resolver invocado exactamente una vez (post-06h.2), sin nuevos imports de pnl/ledger | `ValidateNewOrderUnaffectedTests` (2 tests, actualizado en 06h.2) | Suficiente |
| Alcance del canario (BOOK-06h.1) | Exclusión escopeada al allowlist, no-canario nunca excluido, `position_id`/`routing_decision` NULL, decisiones duplicadas | `test_book06h1_resolver_canary_scope.py` (5 tests) | Suficiente |
| Activación real (BOOK-06h.2) | Los 6 escenarios de paridad + uso del libro ajustado + fallback + las 9 reglas presentes | `test_book06h2_real_activation_integration.py` (8 tests) | Suficiente |
| **`MAX_NET_NOTIONAL` no-monotónico** | — | **Ningún test permanente lo cubre hoy** | **Insuficiente — recomendado** |
| **Ventana de consistencia eventual (Sección 7)** | — | **Ningún test lo cubre** (es un fenómeno de timing entre `awaits`, difícil de testear determinísticamente sin inyectar retrasos) | **Insuficiente — aceptable documentar en vez de testear** |
| **Log de éxito del canario (Sección 10)** | — | N/A — la funcionalidad ni siquiera existe todavía | **No aplica hasta implementarse** |

**Tests faltantes recomendados antes de canario real:**
1. Un test permanente que reproduzca el escenario de la Sección 6.2 (`MAX_NET_NOTIONAL` PASS→REJECT vía exclusión) — documenta la propiedad como comportamiento intencional/conocido, no como regresión futura.
2. Un test de concurrencia con dos hilos/transacciones que dispare literalmente la ventana de la Sección 7 (abrir posición canario + validar segunda orden antes de que la `DealingDeskDecision` exista) — opcional, de alto esfuerzo relativo al riesgo (ya es conservador por diseño).

---

## 13. RISK-01 / RISK-02 Compatibility

Confirmado por diff acumulado (`baa1161^..HEAD`, Sección 3) que BOOK-06 **no modifica**:

- **Locks:** `BrokerRiskLock` sin cambios estructurales; mismo orden global; el resolver no adquiere ningún lock nuevo.
- **Validadores:** `validate_symbol_limit`/`validate_account_limit` sin ningún cambio de línea; `validate_total_limit`/`validate_position_limit` sin cambios de firma ni de lógica interna — solo reciben un `book` potencialmente distinto desde su único llamador.
- **Balances/equity:** ningún archivo relacionado con `TradingAccount.balance`/`.equity` tocado.
- **Margin:** `margin_after` (broker_risk.py:548-562) usa `broker_exposure_for_account()` directamente — **nunca** el `book` resuelto por el canario. Confirmado por lectura de código: es matemáticamente imposible que el canario afecte `margin_after`.
- **P&L:** `pnl_engine.py` no aparece en el diff de BOOK-06.
- **Ledger:** `broker_ledger.py`/`liquidity_ledger.py` no aparecen en el diff.
- **Ejecución de órdenes / cierres / netting / drawdown:** `consumers.py` solo ganó el bloque aditivo de escritura de `DealingDeskDecision` (después del commit, sin tocar la lógica de apertura/cierre/merge/netting existente).

---

## 14. Canary Readiness

### 14.1 Propuesta concreta de primer canario interno

- **Alcance:** exactamente **una** cuenta de prueba interna (nunca un cliente real), `DEALING_DESK_EXPOSURE_ACCOUNT_IDS={<esa cuenta>}`.
- **Requisitos mínimos de la cuenta:** cuenta de staff/QA ya existente o creada para este propósito, con `TraderScore.routing_profile` capaz de calificar como `HEDGE_CANDIDATE` (o el perfil que se decida usar como calificante), balance suficiente para abrir posiciones de prueba representativas.
- **Datos necesarios antes de iniciar:** al menos una `DealingDeskDecision(is_simulated_hedge=True)` real generada por tráfico de la propia cuenta (no fabricada), para confirmar que el flujo completo (orden → decisión → resolución) ocurre de punta a punta antes de considerar el trial "activo".
- **Duración recomendada:** 2-4 semanas de observación activa (mismo criterio que BOOK-06f), con revisión semanal de logs y del estado de la cuenta.
- **Escenarios de prueba:** (a) orden dentro del canario sin posiciones excluibles → debe ser idéntico al oficial; (b) orden dentro del canario con posiciones excluibles → debe usar el libro ajustado; (c) orden fuera del canario → debe ser idéntico al oficial en todo momento; (d) al menos un escenario direccional deliberado que reproduzca la Sección 6.2 en un entorno controlado, para confirmar que el equipo entiende y acepta el comportamiento antes de que ocurra orgánicamente.
- **Métricas a observar:** conteo de veces que se usó el libro ajustado vs. el oficial (requiere cerrar el Hallazgo F-05 primero, o revisar manualmente vía shell/DB durante el trial), cualquier log de fallback/error, cualquier cambio inesperado en balance/margin/P&L de la cuenta canario o de cualquier otra cuenta.
- **Criterios de abortar:** cualquier excepción real del resolver en producción; cualquier discrepancia entre el comportamiento observado y lo documentado en esta auditoría; cualquier cambio inesperado en balance/equity/margin/P&L/ledger de cualquier cuenta (no solo la canario).
- **Criterios de aprobar:** el trial completo transcurre sin disparar ningún criterio de aborto, y el equipo de negocio/riesgo confirma explícitamente que el comportamiento de la Sección 6.2 fue observado (o deliberadamente no se dio) y es aceptable.
- **Procedimiento de rollback:** `DEALING_DESK_EXPOSURE_ENABLED=False` (o vaciar `DEALING_DESK_EXPOSURE_ACCOUNT_IDS`) + reinicio del proceso Daphne/gunicorn. No requiere migración, no requiere limpieza de datos — las filas de `DealingDeskDecision` ya escritas permanecen intactas y no afectan nada mientras el flag esté OFF.

---

## 15. Findings

| ID | Severidad | Resumen | Archivo:línea | Bloquea canario |
|---|---|---|---|---|
| F-01 | **HIGH** | `MAX_NET_NOTIONAL` no es monotónico bajo exclusión — puede convertir PASS oficial en REJECT ajustado en libros direccionalmente sesgados (verificado empíricamente) | `simulator/broker_risk.py:337-352` | No bloquea, pero **requiere sign-off explícito de negocio/riesgo** antes de activar |
| F-02 | MEDIUM | `exposure_after` en `RiskLimitDecision` no es invariante bajo el canario — refleja el libro ajustado, no siempre el oficial. Sin impacto actual (ningún caller lo lee hoy), pero es una trampa latente para un futuro consumidor (dashboard/auditoría) | `simulator/broker_risk.py:546` | No bloquea |
| F-03 | MEDIUM | Ventana de consistencia eventual: `DealingDeskDecision` se escribe después de que la transacción de apertura ya comiteó y liberó `BrokerRiskLock` — una posición recién abierta puede no ser excluible aún para una validación concurrente inmediatamente posterior. Dirección conservadora (nunca insegura), autocorregible | `simulator/consumers.py:1372-1416` | No bloquea |
| F-04 | LOW | `broker_risk_shadow.py` (BOOK-06e) calcula una exclusión GLOBAL, sin scope de cuenta — arquitectónicamente distinta del resolver real (BOOK-06h, escopeado al canario). Riesgo de confusión operativa si se comparan ambos números esperando que coincidan | `simulator/broker_risk_shadow.py:89-93` | No bloquea, recomendado aclarar en UI |
| F-05 | MEDIUM | No existe log en la ruta de ÉXITO del canario (cuenta evaluada, N excluidas, notional excluido, PASS/REJECT) — solo se loguea el fallo/fallback. Imposible confirmar desde logs con qué frecuencia se usó el canario durante un trial real | `simulator/broker_risk.py:464-486` | **Recomendado cerrar antes de canario**, o aceptar explícitamente para un primer trial muy corto |
| F-06 | LOW | `.env.example` no documenta `DEALING_DESK_EXPOSURE_ENABLED`/`DEALING_DESK_EXPOSURE_ACCOUNT_IDS` (gap preexistente, comparte patrón con `LIQUIDITY_ENGINE_ENABLED`) | `.env.example` | No bloquea |
| F-07 | LOW | Comentario desactualizado en `settings.py` — sigue diciendo "validate_new_order() no las usa, todavía llama a broker_exposure_snapshot() directamente" cuando BOOK-06h.2 ya cambió eso | `trx_simulator/settings.py:476-484` | No bloquea, cosmético |
| F-08 | LOW | No hay indicador operativo visible de "canario activo" fuera de leer el propio valor del flag/allowlist — la vista BOOK-06e no refleja el estado real de activación (ver F-04) | N/A (ausencia de funcionalidad) | No bloquea |
| F-09 | INFO | **Corrección de un hallazgo previo:** el RFC de BOOK-06h afirmó que `pricing_coverage_pct` también podía flipear PASS→REJECT vía exclusión. Re-derivado matemáticamente y verificado empíricamente como **falso** en esta auditoría (Sección 6.3) | `simulator/broker_exposure.py:396-401` | N/A — corrección, no hallazgo nuevo |
| F-10 | INFO | Conteo de queries verificado constante (5/6) en 100 a 100,000 posiciones; el crecimiento lineal de latencia a gran escala es preexistente de RISK-01, no introducido por BOOK-06 | `simulator/broker_exposure.py:282` | No bloquea |
| F-11 | INFO | No existe restricción de unicidad en `DealingDeskDecision` — decisiones duplicadas son posibles pero inofensivas (deduplicadas por `frozenset` al leer) | `simulator/models.py:2592-2669` | No bloquea |
| F-12 | INFO | Todos los umbrales de RISK-02 (`MAX_NET_NOTIONAL`, etc.) y los dos flags de BOOK-06 se congelan en tiempo de importación del proceso — ningún cambio de configuración surte efecto sin reinicio, comportamiento uniforme en todo el proyecto | `simulator/broker_risk.py:102-110`, `trx_simulator/settings.py:495-501` | No bloquea, documentar en runbook |
| F-13 | INFO | Ninguna PII ni secreto se registra en los logs de BOOK-06; el allowlist solo puede configurarse desde el servidor (variable de entorno), sin superficie de escritura en la aplicación | `simulator/dealing_desk.py`, `simulator/broker_risk.py` | No bloquea |
| F-14 | INFO | 102 tests dedicados a BOOK-06, todos en verde; 2 tests recomendados adicionales identificados (Sección 12) | — | No bloquea |

**Recuento:** 0 BLOCKER · 1 HIGH · 4 MEDIUM · 5 LOW · 6 INFO — **15 hallazgos totales** (incluyendo F-09, que es una corrección, no un hallazgo nuevo de riesgo).

---

## 16. Required Actions Before Canary

1. **[F-01, HIGH]** Presentar la Sección 6.2 de esta auditoría (con el ejemplo numérico) al responsable de negocio/riesgo y obtener reconocimiento explícito por escrito de que el comportamiento no-monotónico de `MAX_NET_NOTIONAL` es entendido y aceptado antes de activar cualquier flag.
2. **[F-05, MEDIUM]** Decidir explícitamente: o bien implementar un log de éxito mínimo (cuenta, excluidas, notional excluido, PASS/REJECT) en una subfase futura antes del canario, o aceptar por escrito operar el primer trial sin esa señal, compensando con revisión manual directa de la tabla `DealingDeskDecision` durante el período de observación.
3. **[F-07, LOW]** Actualizar el comentario obsoleto en `trx_simulator/settings.py:476-484` (cosmético, no funcional) — no se hizo en esta auditoría por regla explícita de "no corregir hallazgos durante esta fase".
4. Confirmar el procedimiento operativo de reinicio de proceso (Daphne/gunicorn) en el entorno donde se activará el canario — el runbook de la Sección 14 asume que existe un mecanismo de reinicio conocido y de bajo riesgo.
5. Preparar/identificar la cuenta canario específica y confirmar que puede generar al menos una `DealingDeskDecision(is_simulated_hedge=True)` real antes de considerar el trial iniciado.

## 17. Recommended Actions After Canary

1. Cerrar F-04: aclarar en la UI de BOOK-06e que su cálculo es global y no representa el alcance real del canario activo.
2. Cerrar F-06: documentar las dos variables en `.env.example`.
3. Evaluar si `exposure_after` (F-02) necesita un campo adicional que distinga explícitamente el valor oficial del ajustado, en caso de que un futuro consumidor lo necesite.
4. Añadir el test permanente recomendado en la Sección 12 (punto 1) que fija el comportamiento de la Sección 6.2 como contrato documentado.
5. Si el canario se expande más allá de 1-2 cuentas, revisar si el techo de rendimiento de la Sección 8 (preexistente de RISK-01) necesita atención antes de escalar a volúmenes de posiciones significativamente mayores.
6. Considerar formalizar `ACCOUNT_TYPES`/`TRADER_CLASSES`/`ROLLOUT_PCT` (diferidos desde BOOK-06f) solo después de que el canario de una cuenta haya cerrado con éxito.

## 18. Final Verdict

**READY WITH CONDITIONS**

BOOK-06 está arquitectónicamente sólido, exhaustivamente testeado (102/102 tests propios, 3428/3428 en la suite completa), con fail-safe verificado en cada capa y sin ningún hallazgo BLOCKER. Las condiciones obligatorias antes de activar cualquier flag en un entorno real son las listadas en la Sección 16, en particular el reconocimiento explícito de negocio/riesgo sobre el hallazgo F-01 (`MAX_NET_NOTIONAL` no-monotónico) y una decisión consciente sobre el vacío de observabilidad F-05.

---

*Fin del documento. Ningún archivo de producción fue modificado durante esta auditoría. Ningún flag fue activado. No se realizó commit ni push.*
