# BOOK-04 — Routing Engine: Plan de Implementación

| Campo | Valor |
|---|---|
| Rol del documento | Plan técnico de construcción por bloques — sin implementación |
| Fecha | 2026-07-22 |
| Fases previas aprobadas | FASE 1 (descubrimiento), FASE 2 (persistencia vs. recálculo → persistir), FASE 3 (Routing Decision Contract → mixto, doble versionado) |
| Metodología | Idéntica a la del Audit Trail Engine (AUDIT-01 → AUDIT-04b): bloques pequeños, cada uno desarrollable, probable, commiteable, etiquetable y estabilizable de forma independiente |
| Alcance de este documento | Solo la secuencia de bloques y su contrato de entrada/salida. Ninguna regla de negocio, ningún código. |

---

## Principio rector del plan

**El broker debe quedar completamente funcional después de cada bloque, sin excepción — igual que después de cada uno de los cinco bloques del Audit Trail Engine.** Para lograrlo, BOOK-04 reutiliza un patrón que ya existe y ya está probado en este mismo proyecto, no uno nuevo: el **Shadow Mode** de `market_data/shadow/` (`MARKET_DATA_SHADOW_MODE`, Foundation-08) y la **activación canaria** de `MARKET_DATA_ROUTER_ENABLED`/`MARKET_DATA_ROUTER_SYMBOLS` (Foundation-09) — evaluar en paralelo, apagado por defecto, nunca controlar nada hasta aprobación explícita, con fallback automático ante cualquier error. BOOK-04 es la aplicación de ese mismo patrón, ya validado en `market_data`, al Routing Engine.

**Ningún bloque de este plan diseña ni activa una regla de ruteo real.** El Routing Engine, al cerrarse BOOK-04, sabrá *registrar, persistir, auditar y mostrar* una decisión de ruteo — la decisión en sí seguirá siendo siempre la misma trivial ("todo es interno", el 100% real de hoy) hasta que un bloque futuro, fuera de este plan, decida qué reglas activar.

---

## Orden recomendado y dependencias

```
BOOK-04a  Foundation (modelo + writer)
    │
    ▼
BOOK-04b  Shadow Mode Integration (único touch real al Trading Engine)
    │
    ▼
BOOK-04c  Cierre Simétrico (Trade + BrokerLedger)
    │
    ▼
BOOK-04d  Integración con Audit Trail (Category.ROUTING)
    │
    ▼
BOOK-04e  Visibilidad para Staff (solo lectura)
    │
    ▼
BOOK-04f  Mecanismo de Activación Controlada (sin reglas)
```

Secuencial y sin atajos — cada bloque depende del anterior exactamente en ese orden, mismo criterio que AUDIT-02 no pudo empezar antes de que AUDIT-01 existiera.

---

## BOOK-04a — Routing Decision Foundation

### Objetivo
Crear el contrato de datos aprobado en FASE 3 como modelo real — mixto (columnas tipadas + JSON `inputs_snapshot`), doble versionado (`schema_version`/`engine_version`) — sin conectarlo a ningún flujo de negocio todavía.

### Alcance
- Modelo `RoutingDecision`: `decision_id` (UUID), `book` (string abierto, sin `choices=` a nivel de DB), `reason_code`, `reason_message`, `engine_version`, `schema_version`, `decided_at`, `inputs_snapshot` (JSON), y los campos opcionales de FASE 3 (`external_reference`, `parent_decision_id` FK a sí mismo, `override_by`/`override_reason`, `correlation_id`) — todos nullable.
- Writer `record_routing_decision(...)` en un módulo nuevo (`simulator/routing_engine.py`), fail-open desde el día uno, mismo contrato que `record_event()`.
- **Cero integración con `consumers.py`, `Position`, `Trade`, `_db_open_position_atomic`.** El broker sigue funcionando exactamente igual, sin ninguna diferencia observable.

### Archivos que probablemente participarán
```
simulator/models.py                                        — modelo RoutingDecision
simulator/migrations/00XX_book04a_routing_decision_foundation.py
simulator/routing_engine.py                                 — nuevo módulo
simulator/tests/test_book04a_routing_decision_foundation.py
```

### ¿Requiere migración?
Sí — tabla nueva (`CreateModel`), aditiva pura, no toca ninguna tabla existente.

### Riesgos
Mínimos. El único riesgo real es de diseño (mitigado por FASE 3 ya aprobada antes de escribir código).

### Dependencias
FASE 3 aprobada. Ninguna otra.

### Estrategia de pruebas
Creación del modelo con todos los campos; `schema_version`/`engine_version` verificados como ejes independientes; fail-open del writer forzando una excepción; ningún test de integración con trading porque no existe integración todavía.

### Definition of Done
Modelo migrado y probado de forma aislada. `python manage.py makemigrations --check` sin cambios pendientes tras aplicar. Suite completa del proyecto en verde, sin ninguna diferencia respecto al estado anterior — el bloque es indistinguible desde fuera del código.

---

## BOOK-04b — Shadow Mode Integration

### Objetivo
Conectar el Routing Engine al único punto real identificado en FASE 1 — dentro de `_db_open_position_atomic()`, entre RISK-02 (paso 8.5) y la creación de `Position` (paso 9) — en **modo sombra**: cada apertura real produce una decisión persistida, pero la decisión es siempre determinística y coincide exactamente con el comportamiento actual (`book="INTERNAL"` siempre, el 100% real de hoy). Nunca cambia qué se ejecuta — solo empieza a registrar el hecho.

### Alcance
- Flag `ROUTING_ENGINE_ENABLED` en settings, default `False` — mismo patrón que `MARKET_DATA_SHADOW_MODE`.
- Con el flag activo: una integración nueva dentro de la transacción ya abierta de `_db_open_position_atomic`, ubicada en la región entre RISK-02 (paso 8.5) y la creación/merge de `Position` (paso 9), con una secuencia interna de cuatro pasos — nunca adquiere un lock nuevo, nunca invierte el LOCK ORDER ya documentado (`BrokerRiskLock → TradingAccount → Position`):
  1. **Construir el contrato** — decidir `book`/`reason_code`/`reason_message`/`inputs_snapshot` (no depende de `position_id`, puede calcularse antes del branch `if existing:`).
  2. **Ejecutar create/merge** — el branch existente (`Position.objects.create()` o `existing.save(...)`) se ejecuta sin cambios; a partir de aquí `position_id`/`merged` ya existen.
  3. **Persistir `RoutingDecision`** — se llama al writer con `position_id` ya conocido, dentro de la misma transacción.
  4. **Enlazar `Position.routing_decision` cuando corresponda** — solo si la `Position` se creó por primera vez (`merged=False`); en un merge, `Position.routing_decision` no se toca.
- Campo nuevo en `Position`: `routing_decision` (FK nullable a `RoutingDecision`) — la **decisión principal**: la tomada cuando la `Position` se crea por primera vez. **Nunca se sobreescribe en un merge de netting.**
- Campo nuevo en `RoutingDecision`: `position` (FK nullable a `Position`, **`on_delete=models.SET_NULL`**) — permite que **cada incremento** (apertura nueva o merge) tenga su propia `RoutingDecision`, todas enlazadas a la misma `Position` sin que ninguna reemplace a la anterior. `on_delete=SET_NULL` no es una elección defensiva genérica: es obligatoria porque `Position` se **elimina físicamente** al cerrarse (`_db_close_position_atomic`, `consumers.py` — la fila se convierte en `Trade` y `Position.delete()` se ejecuta de verdad, no es un soft-delete). Con `CASCADE` cada cierre borraría silenciosamente las `RoutingDecision` de esa posición — el mismo patrón de destrucción de evidencia ya corregido dos veces en este proyecto (KYC en AUDIT-03, 2FA en AUDIT-04a). Con `SET_NULL`, las filas sobreviven intactas; solo su enlace a la `Position` ya cerrada se anula. Resolución arquitectónica de netting/merge (ver subsección dedicada abajo, incluida su interacción con el cierre).
- `_db_open_position_atomic()` añade `routing_decision_id` a su `result` dict de retorno (junto a `position_id`/`merged`/`new_balance` ya existentes) — es el `decision_id` de la decisión tomada **para esta llamada concreta**, sea apertura nueva o incremento por merge. Este dato es lo que permite a BOOK-04d auditar sin volver a tocar `_db_open_position_atomic()` (ver BOOK-04d).
- Envuelto en su propio `try/except`, fail-open explícito — y ese `try/except` protege **ambos** pasos 1 y 3 de la secuencia anterior, no solo la llamada al writer: tanto la construcción del contrato (`book`/`reason_code`/`inputs_snapshot`) como la llamada a `record_routing_decision()` deben quedar dentro del mismo bloque protegido, porque el fail-open ya probado de BOOK-04a solo cubre el cuerpo interno del writer — un fallo al **construir** el contrato (antes de siquiera llamar al writer) no está cubierto por esa garantía si no se envuelve explícitamente aquí también. En cualquiera de los dos casos: un fallo nunca impide que la posición se abra.

#### Resolución arquitectónica: netting / merge

Cuando una orden incremental se funde en una `Position` existente (`existing` en `_db_open_position_atomic`, `consumers.py`), no se crea una fila `Position` nueva — solo se actualizan `qty`/`avg_price`. Sin una relación dedicada, esto forzaría a elegir entre sobreescribir la decisión original (perdiendo la historia) o descartar la decisión del incremento (perdiendo la explicabilidad de esa orden concreta). Ninguna de las dos es aceptable.

Se resuelve con **ambas relaciones a la vez**, no una sola:
1. `Position.routing_decision` — la decisión **principal**, fijada una sola vez al crear la `Position`, estable durante toda su vida. Es la fuente de verdad que usa BOOK-04c para el cierre y para `BrokerLedger`.
2. `RoutingDecision.position` (relación inversa uno-a-muchos) — cada orden individual, incluida cada una que se funde por merge, produce su propia `RoutingDecision` enlazada a la misma `Position`. Nada se sobreescribe ni se pierde; cada incremento queda auditable por separado vía `RoutingDecision.objects.filter(position=...)` mientras la `Position` permanece abierta (ver abajo qué ocurre exactamente al cerrarla).

Con esto: la `Position` agregada mantiene una fuente de verdad estable y única (`routing_decision`, la principal) para todo lo que consume una sola decisión por posición (cierre, `BrokerLedger`); y cada incremento individual sigue siendo explicable por completo sin ambigüedad, sin que un merge posterior borre o reemplace la evidencia de los anteriores — mismo principio de no-destrucción de evidencia ya aplicado en AUDIT-03 (KYC) y AUDIT-04a (2FA).

**Qué ocurre al cerrar la `Position`:** `Position.delete()` se ejecuta de verdad en el cierre (ver `on_delete` arriba) — en ese momento, `RoutingDecision.position` pasa a `NULL` por `SET_NULL` en cada `RoutingDecision` que apuntaba a esa `Position`, incluidas la principal y todos sus incrementos. **Las decisiones no desaparecen** — cada fila sigue existiendo, íntegra, como evidencia histórica permanente — lo único que se pierde en ese instante es la posibilidad de encontrarlas por `RoutingDecision.objects.filter(position=<esa Position>)`, porque esa `Position` ya no existe como fila. La decisión principal no depende de ese enlace para sobrevivir al cierre: BOOK-04c la copia a `Trade.routing_decision` antes de que la `Position` se elimine, precisamente para que la posición cerrada conserve un puntero estable y permanente. Los incrementos no-principales no tienen ese espejo — siguen existiendo como filas de `RoutingDecision`, pero después del cierre solo son localizables por otros medios (rango de `decided_at`, o una dimensión de consulta adicional que un bloque futuro podría añadir), no por `position`.

### Archivos que probablemente participarán
```
simulator/consumers.py            — una llamada nueva en _db_open_position_atomic, dentro del flag; result dict +routing_decision_id
simulator/models.py               — Position: +routing_decision (FK nullable, decisión principal); RoutingDecision: +position (FK nullable, uno-a-muchos)
simulator/migrations/00XX_book04b_position_routing_decision.py
simulator/routing_engine.py       — función de decisión trivial/determinística
trx_simulator/settings.py         — ROUTING_ENGINE_ENABLED
simulator/tests/test_book04b_shadow_mode_integration.py
```

### ¿Requiere migración?
Sí — `AddField` en `Position` (FK nullable, `on_delete=SET_NULL`) y en `RoutingDecision` (FK nullable a `Position`, `on_delete=SET_NULL` — ver justificación en Alcance), aditiva pura.

### Riesgos
**El más alto de todo el plan — y el único bloque que introduce lógica de decisión real dentro de `_db_open_position_atomic()`** (BOOK-04d no la toca, ver su sección; BOOK-04e la toca más tarde, pero solo para pasar un `account_id` ya disponible a la llamada que este bloque ya introduce aquí — sin ninguna lógica de decisión adicional, ver la sección de BOOK-04e). Mitigado por: flag apagado por defecto (cero cambio de comportamiento hasta activación explícita); decisión siempre trivial en este bloque (nada que pueda fallar de forma sorpresiva); fail-open probado explícitamente; ningún lock nuevo, ninguna inversión del orden ya documentado; la resolución de netting/merge (arriba) evita que un incremento sobreescriba o pierda la decisión de otro.

### Dependencias
BOOK-04a.

### Estrategia de pruebas
Con el flag apagado: suite completa de trading idéntica a antes de este bloque (test de no-op explícito). Con el flag activo: cada apertura real produce exactamente una `RoutingDecision` enlazada a su `Position` vía `Position.routing_decision`; fallo forzado del router no impide la apertura; test determinista (sin hilos) de que no se introdujo ninguna carrera nueva ni se invirtió el lock order (mismo tipo de verificación ya usada en AUDIT-03/04a). **Test dedicado de netting/merge:** abrir una `Position`, luego fusionar un segundo incremento sobre la misma — verificar que `Position.routing_decision` sigue apuntando a la decisión original (no cambia), que existe una segunda `RoutingDecision` distinta enlazada vía `RoutingDecision.position`, y que ambas son consultables de forma independiente.

### Definition of Done
Flag apagado (default en cualquier entorno no explícitamente configurado): cero diferencia de comportamiento, suite completa en verde. Flag activo en entorno de prueba: cada posición nueva lleva su decisión principal enlazada, siempre trivial; un merge de netting no altera la decisión principal de la `Position` y produce su propia `RoutingDecision` enlazada por incremento. Ninguna regla de negocio real activada.

---

## BOOK-04c — Cierre Simétrico (Trade + BrokerLedger)

### Objetivo
Cerrar el hueco de simetría identificado en FASE 2: al cerrar una posición, el `Trade` hereda la decisión de ruteo de la `Position` que se cierra, y `broker_ledger.py::create_broker_counterparty_entry()` deriva su parámetro `book_mode` (ya existente, nunca usado con una fuente real) desde la decisión persistida.

### Alcance
- Campo espejo en `Trade`, copiado verbatim al cerrar desde `Position.routing_decision` (la decisión **principal**, nunca desde un incremento individual) — mismo patrón que `pricing_context_open`/`pricing_context_close`, nunca recalculado.
- **BOOK-04c debe mantener consistencia entre los tres escritores reales de cierre, no solo `consumers.py`** — verificados por código, los tres llaman a `create_broker_counterparty_entry()` y comparten el mismo flujo común (crear `Trade` → `LedgerEntry` → `BrokerLedger` → recién entonces `Position.delete()`):
  - `simulator/consumers.py::_db_close_position_atomic()` — cierre manual vía WebSocket.
  - `simulator/tasks.py::_close_position_sync()` — daemon Celery (TP/SL/stopout/margin-call).
  - `simulator/admin.py::force_close` (bloque de cierre de la acción de dealing desk) — cierre administrativo.

  Los tres reciben el mismo cambio mecánico y simétrico: copiar `pos.routing_decision` al `Trade.objects.create(...)` de cada uno, en el mismo punto donde ya copian `pricing_context`/`pnl_conversion` (dos de los tres lo hacen hoy) — siempre antes de `pos.delete()`. Ninguna lógica de decisión nueva en ninguno de los tres; solo lectura de un dato ya persistido.

  `simulator/consumers.py::_db_mirror_close_position` queda **explícitamente fuera de alcance**: es un path ya deprecado ("superseded by `_db_close_position_atomic`"), ya inconsistente hoy respecto a los tres anteriores (no copia `pricing_context`, no llama a `create_broker_counterparty_entry()`). BOOK-04c no lo corrige ni lo homologa — mantiene su estado actual, sin empeorarlo ni mejorarlo.

- **`RoutingDecision.book` y `BrokerLedger.book_mode` no representan el mismo dominio de valores.** Son dos vocabularios de strings distintos: `routing_engine.py::Book` describe *dónde* se enruta una orden (hoy solo `"INTERNAL"`); `broker_ledger.py::BOOK_MODE_B_BOOK = "B_BOOK"` describe el *tratamiento contable* de un `Trade` ya cerrado. Que hoy correspondan 1:1 (`INTERNAL` → siempre `B_BOOK`) es un mapeo de negocio vigente, no una identidad de strings, y no debe asumirse que seguirá siendo así el día que exista un routing real (A-book/LP/hedge). La traducción entre ambos espacios ocurre en **un único punto claramente identificado: dentro de `create_broker_counterparty_entry()` (`broker_ledger.py`)**, derivando `book_mode` internamente a partir de `trade.routing_decision.book` cuando el llamador no lo sobreescribe explícitamente (el parámetro `book_mode=` ya existente se conserva para overrides futuros) — nunca replicada en cada uno de los tres escritores por separado. No se introduce ninguna tabla nueva, ningún enum nuevo, ni ningún cambio de modelo para esto — es una función de traducción interna a `broker_ledger.py`, del mismo tamaño y naturaleza que el resto de ese módulo.
- Los incrementos individuales de una `Position` fusionada por netting (cada `RoutingDecision` enlazada vía `RoutingDecision.position`, ver BOOK-04b) **sobreviven como evidencia histórica tras el cierre — el cierre nunca los borra ni los reescribe** —, pero dejan de ser localizables por `RoutingDecision.position`: al eliminarse la `Position` en el cierre, ese campo pasa a `NULL` en cada una de ellas por `SET_NULL` (ver BOOK-04b). Solo la decisión principal conserva un puntero estable post-cierre, vía `Trade.routing_decision` (copiado antes de que la `Position` se elimine); los incrementos no-principales quedan como filas íntegras pero sin ese enlace directo.
- Sigue bajo el mismo flag `ROUTING_ENGINE_ENABLED` — apagado, comportamiento idéntico al actual.

### Archivos que probablemente participarán
```
simulator/models.py               — Trade: +routing_decision (FK nullable, espejo de Position)
simulator/migrations/00XX_book04c_trade_routing_decision.py
simulator/consumers.py            — _db_close_position_atomic() únicamente (NO _db_mirror_close_position, deprecado y fuera de alcance)
simulator/tasks.py                — _close_position_sync()
simulator/admin.py                — force_close (bloque de cierre)
simulator/broker_ledger.py        — create_broker_counterparty_entry() — único punto de traducción book → book_mode
simulator/tests/test_book04c_close_symmetry.py
```

### ¿Requiere migración?
Sí — `AddField` en `Trade`, aditiva pura.

### Riesgos
Bajo — toca el camino de cierre solo para *leer* un dato ya persistido en BOOK-04b, nunca para tomar una nueva decisión ahí. Flag apagado mantiene el comportamiento actual sin cambio. Riesgo de alcance (no de arquitectura): si el cambio se aplica solo en `consumers.py` y no en `tasks.py`/`admin.py`, la "simetría apertura↔cierre" quedaría probada solo parcialmente — mitigado exigiendo el mismo cambio, simétrico, en los tres escritores reales. Centralizar la traducción `book`→`book_mode` en `create_broker_counterparty_entry()` (en vez de repetirla en cada escritor) evita que los tres puedan divergir entre sí.

### Dependencias
BOOK-04b.

### Estrategia de pruebas
Con el flag activo: cerrar una posición copia correctamente la decisión principal al `Trade`; `BrokerLedger.meta["book_mode"]` refleja `"B_BOOK"` (nunca el string literal `"INTERNAL"`) — test explícito de la traducción de vocabularios. **La misma verificación (Trade.routing_decision correcto + book_mode correcto) se repite en los tres escritores reales** (`consumers.py`, `tasks.py`, `admin.py`) — no basta con probarlo en uno solo. **Test dedicado de cierre tras netting:** abrir una `Position`, fusionar dos o más incrementos (cada uno con su propia `RoutingDecision`), cerrarla — verificar que `Trade.routing_decision` es exactamente la decisión principal (la del primer incremento, sin cambios), que `BrokerLedger` es consistente con esa misma decisión, y que las `RoutingDecision` de los incrementos posteriores **siguen existiendo intactas como filas** tras el cierre, con su campo `position` en `NULL` (por `SET_NULL`, ver BOOK-04b) — es decir, ya no localizables vía `RoutingDecision.position`, pero nunca borradas ni reescritas. Con el flag apagado: comportamiento idéntico al preexistente (test de no-regresión explícito, mismo criterio que cada bloque anterior).

### Definition of Done
Simetría apertura↔cierre verificada por test **en los tres escritores reales de cierre** (`consumers.py`, `tasks.py`, `admin.py`) — `_db_mirror_close_position` queda fuera, sin cambios. `book_mode` real fluye hasta `BrokerLedger` con el flag activo, traducido desde `RoutingDecision.book` en el único punto centralizado (`create_broker_counterparty_entry()`). Suite completa en verde en ambos estados del flag.

---

## BOOK-04d — Integración con Audit Trail (Category.ROUTING)

### Objetivo
Registrar, como evento institucional, el hecho de que **"la decisión de routing asociada a una apertura aceptada fue registrada"** — no la finalización completa de la respuesta WebSocket ni del flujo de presentación al cliente. Sigue exactamente el patrón de los cinco bloques ya cerrados del Audit Trail Engine — sin diseñar aquí los `event_type` exactos más allá de dejar reservado que serán constantes `EV_*` coherentes con el resto de `broker_audit.py` (detalle de implementación del propio bloque, no de este plan). **Este bloque no modifica `_db_open_position_atomic()` — BOOK-04b introduce ahí la única lógica de decisión real; BOOK-04e la toca después, pero solo para pasar un dato ya disponible (`account_id`), sin ninguna lógica de decisión nueva (ver BOOK-04e).**

### Alcance

**1. Integración async correcta — obligatoria, no opcional.** `_order_new()` (`simulator/consumers.py`) es un método `async def`, **sin** el decorador `database_sync_to_async`. `record_routing_event()`/`record_event()` (`broker_audit.py`) usan el ORM de Django de forma **síncrona**. Llamar a `record_routing_event()` directamente desde el cuerpo de `_order_new()` produciría `django.core.exceptions.SynchronousOnlyOperation` en el primer pedido real procesado con el flag activo — **queda expresamente prohibido**. La implementación debe crear una función pequeña, sync, decorada `@database_sync_to_async` (mismo molde que las 14 apariciones ya existentes de ese decorador en `consumers.py`), invocada con `await` desde `_order_new()`.

**2. Punto exacto de emisión.** La llamada (`await self._db_record_routing_audit_event(...)` o el nombre que se le dé) se ubica:
- después de confirmar `result.get("ok", True)` (es decir, después del `if not result.get("ok", True): ... return` ya existente);
- **antes** de las mutaciones de memoria (`self.account["balance"] = ...`, `self._create_position()`/`self._open_or_update_position()`) y de los envíos WebSocket (`order_ack`/`order_fill`);
- fuera de `_db_open_position_atomic()`;
- fuera de todos los locks (`BrokerRiskLock`/`TradingAccount`/`Position`);
- fuera de la transacción de apertura (que ya hizo commit).

La escritura es fail-open y no puede impedir, retrasar de forma relevante, ni alterar que la orden continúe — para cuando se ejecuta, la `Position` ya existe (o ya se fusionó) y la transacción de BOOK-04b ya comprometió.

**3. Contrato de datos.** No releer `RoutingDecision` ni `Position` desde la base de datos bajo ninguna circunstancia. Usar **únicamente** los datos ya expuestos en `result`: `routing_decision_id`, `position_id`, `merged`. El `metadata` del evento es deliberadamente delgado — **no incluye** `inputs_snapshot`, `book`, `reason_code`, `engine_version`, ni ningún dato que exigiera una consulta adicional (esos campos no viajan en `result` hoy; incluirlos violaría la prohibición de releer la base de datos). **Si `routing_decision_id` es `None` (flag apagado, writer fallido, o enlace principal fallido), no se crea ningún evento `ROUTING`** — la ausencia de decisión no es, en sí misma, un hecho auditable por este bloque.

**4. Categoría institucional.** `Category.ROUTING` — categoría **nueva y distinta**, sin migración (mismo patrón ya confirmado tres veces: `PAYMENTS`/`COMPLIANCE`/`AUTHENTICATION`). **No se reutiliza `Category.MONITORING`** — `MONITORING` corresponde a infraestructura, proveedores, circuit breakers y health de `market_data/`; `ROUTING` corresponde a decisiones de negocio del motor de ruteo. Son dominios semánticamente distintos; conflacionarlos degradaría la utilidad de ambas categorías para cualquier consulta futura por categoría.

**5. Fail-open — protección adicional en el call site.** `record_event()` ya es fail-open por diseño (savepoint anidado, nunca relanza), pero esa garantía cubre únicamente su propio cuerpo. La implementación debe envolver, en el *call site* dentro de `_order_new()`, tanto la construcción de los argumentos como el `await` a la función `database_sync_to_async` en su propio `try/except` — no basta con confiar en el fail-open interno de `record_event()`. Cualquier fallo de: construcción del contrato de argumentos; *scheduling* del hilo (`database_sync_to_async` en sí); ejecución del wrapper; o escritura de auditoría — debe quedar absorbido ahí, sin propagarse, y sin afectar en ningún caso: `Position`; `RoutingDecision`; balance; margen; `order_ack`; `order_fill`; ni el flujo normal de apertura.

### Archivos que probablemente participarán
```
simulator/broker_audit.py         — Category.ROUTING, record_routing_event(), constantes EV_* (reservadas, sin diseñar aquí)
simulator/consumers.py            — función nueva @database_sync_to_async + una llamada await en _order_new(), después del chequeo result.get("ok") — NO en _db_open_position_atomic()
simulator/tests/test_book04d_routing_audit_trail.py
```
Ningún otro archivo. Sin migración. Este bloque no modifica `_db_open_position_atomic()` en ningún punto de su propia implementación (un bloque posterior, BOOK-04e, sí la toca — ver su sección — pero eso es ajeno al alcance de BOOK-04d).

### ¿Requiere migración?
No.

### Riesgos
Mínimos — mismo perfil que cualquier bloque de integración del Audit Trail ya completado. Fail-open heredado sin cambios de `record_event()`, más la protección adicional del call site (punto 5). Riesgo adicional ya mitigado por este ajuste: al vivir en `_order_new()` en vez de en `_db_open_position_atomic()`, este bloque ya no comparte superficie de riesgo con el camino crítico bajo lock — su única dependencia de datos es el `result` dict ya devuelto. Riesgo de integración async (llamada síncrona directa desde un método `async def`) identificado y resuelto explícitamente arriba — antes de este ajuste, una implementación literal del texto previo del plan habría fallado en tiempo de ejecución con `SynchronousOnlyOperation`.

### Dependencias
BOOK-04b (necesita `routing_decision_id` en el `result` dict). Audit Trail Engine ya cerrado (prerequisito ya cumplido).

### Estrategia de pruebas
Mínimo exigido:
- categoría `ROUTING` en el evento creado;
- `metadata` exacta y con whitelist explícita (`decision_id`/`position_id`/`merged` únicamente — ningún otro campo);
- una apertura nueva produce exactamente un evento;
- un merge produce exactamente un evento;
- flag apagado produce cero eventos;
- `routing_decision_id=None` produce cero eventos (independientemente del estado del flag);
- una orden rechazada (`result["ok"] is False`) produce cero eventos;
- un fallo forzado en la escritura de auditoría no afecta la apertura ya confirmada (`Position`, balance, margen, `order_ack`, `order_fill` sin cambios);
- **prueba dedicada del camino async real** (no solo la función `@database_sync_to_async` desnuda) que demuestre la ausencia de `SynchronousOnlyOperation` al invocar el flujo completo desde un contexto async genuino;
- cero consultas adicionales a la base de datos para enriquecer el `metadata` (verificable con `CaptureQueriesContext`, mismo patrón ya usado en BOOK-04b/04c);
- ningún `inputs_snapshot` duplicado dentro de `BrokerAuditEvent`.

### Definition of Done
Cada decisión de ruteo real (flag activo, `routing_decision_id` no `None`) genera exactamente un evento institucional bajo `Category.ROUTING`, correlacionable por `decision_id`/`position_id`/`account`. Flag apagado, o `routing_decision_id=None` por cualquier motivo: cero eventos nuevos. Orden rechazada: cero eventos. `_db_open_position_atomic()` permanece sin cambios respecto a BOOK-04b — confirmado por lectura de diff, no solo por descripción. La llamada se realiza vía `@database_sync_to_async` + `await`, nunca de forma síncrona directa desde `_order_new()`.

### Estado del bloque
**APROBADO PARA IMPLEMENTAR DESPUÉS DEL AJUSTE DOCUMENTAL** — la revisión técnica previa a este ajuste encontró un riesgo real y verificable (integración async faltante); queda resuelto explícitamente arriba. No quedan ajustes pendientes conocidos para iniciar la implementación de este bloque.

---

## BOOK-04e — Visibilidad para Staff (Dealing Desk, solo lectura)

### Objetivo
Staff debe poder consultar decisiones de routing **por cuenta de forma durable** — incluyendo posiciones abiertas y cerradas, **sin depender del ciclo de vida de `Position`** — además de la superficie de solo lectura sobre `RoutingDecision` en el admin. Preparación explícita para el bloque 4 del roadmap oficial (Dealing Desk híbrido A-book/B-book), sin implementar ninguna lógica híbrida todavía.

### Alcance

**1. Modelo — `RoutingDecision.account` (resolución de FASE 2, Opción B aprobada).**
Campo nuevo:
- `ForeignKey` a `TradingAccount`;
- `null=True`, `blank=True`;
- `on_delete=models.SET_NULL`;
- `related_name="routing_decisions"` (mismo criterio ya usado por `RoutingDecision.position` — sin colisión, cada `related_name` es único por modelo destino: `TradingAccount.routing_decisions` y `Position.routing_decisions` coexisten sin conflicto);
- fijado **una sola vez**, dentro de `record_routing_decision()`, en el momento de creación;
- **nunca recalculado, nunca actualizado después de creado, nunca derivado posteriormente desde `Position`, `Trade`, `BrokerAuditEvent` ni ningún otro modelo.**

**2. Compatibilidad histórica.**
Sin backfill. Toda `RoutingDecision` creada antes de este bloque conserva `account=None` — una señal honesta de "anterior a BOOK-04e", igual que el resto de campos nullable ya introducidos en BOOK-04a/04b/04c. No se fabrica ni se infiere ninguna relación histórica desde `Position`, `Trade`, `BrokerAuditEvent` ni ningún otro modelo.

**3. Firma del servicio — `record_routing_decision()`.**
El parámetro pasa a ser **`account_id`** (forma por id, no por objeto) y **obligatorio, sin default**. Justificación de la elección entre las dos formas ya establecidas en el resto de la API interna: el único call site real (`_db_open_position_atomic()`, dentro de `_order_new()`) ya tiene `self._db_account_id` disponible como atributo directo, y ya lo pasa en esa misma forma a otras llamadas dentro de la misma función (p. ej. `record_risk_event(..., account_id=self._db_account_id, ...)` en el rechazo de RISK-02). Es además el mismo criterio ya usado por este propio writer para `position_id` (se pasa `position_id=position_id`, no el objeto, porque el id ya es una variable local disponible en ese punto). `account_id` sin valor por defecto obliga a que cualquier caller — presente o futuro — decida explícitamente qué pasar, en vez de heredar un `None` silencioso por omisión.

**4. Call site.**
El único punto real que debe pasar `account_id` de forma explícita es la llamada a `record_routing_decision()` dentro de `_db_open_position_atomic()` (`consumers.py`), usando `self._db_account_id` — ya disponible en ese punto, sin ninguna consulta adicional. No se modifica el comportamiento de la decisión de ruteo, no se toca ningún lock, no se altera el orden ni el alcance de la transacción ya aprobados en BOOK-04b.

**5. Helpers de consulta.**
- `routing_decisions_for_account(account_id)` — consulta **directa** sobre `RoutingDecision.objects.filter(account_id=account_id)`, mismo molde exacto que `events_for_account()` en `broker_audit.py`. Funciona igual para decisiones de posiciones abiertas y cerradas, porque `account` nunca depende de `Position`.
- `routing_decisions_for_position(position_id)` — se mantiene sin cambios de diseño: sigue siendo útil **mientras la posición esté abierta** (`RoutingDecision.position` aún no se ha anulado por `SET_NULL`); una vez la posición se cierra, deja de devolver resultados por esa vía — la consulta durable por cuenta se resuelve exclusivamente mediante `RoutingDecision.account`, no mediante este helper.

**6. Admin — `RoutingDecisionAdmin` (solo lectura, sin cambios de permisos).**
- `list_display`: `decision_id, book, reason_code, account, position, decided_at` (mismo criterio de columnas relevantes ya usado en `BrokerAuditEventAdmin`).
- `list_filter`: `book`, `reason_code`.
- `search_fields`: `decision_id`, `account__user__username`, `position__id` — `account` ahora es un campo de búsqueda directo y funcional incluso para decisiones de posiciones ya cerradas (a diferencia de la única vía indirecta disponible antes de esta resolución).
- `readonly_fields`: todos los campos del modelo — mismo criterio de "ningún campo editable" ya aplicado en `BrokerAuditEventAdmin`.
- `has_add_permission`/`has_change_permission`/`has_delete_permission`: los tres `False` — mismo patrón exacto.
- `get_actions()`: `delete_selected` eliminado explícitamente del diccionario de acciones — mismo refuerzo "cinturón y tirantes" ya usado en `BrokerAuditEventAdmin`.

### Archivos que probablemente participarán
```
simulator/models.py                — RoutingDecision: +account (FK nullable, on_delete=SET_NULL)
simulator/routing_engine.py        — record_routing_decision(account_id obligatorio), helpers de consulta
simulator/consumers.py             — call site en _db_open_position_atomic() pasa account_id=self._db_account_id
simulator/admin.py                 — RoutingDecisionAdmin (solo lectura)
simulator/migrations/00XX_book04e_routing_decision_account.py
simulator/tests/test_book04e_routing_visibility.py
```

### ¿Requiere migración?
**Sí** — `AddField` en `RoutingDecision` (FK nullable), aditiva pura, sin `RunPython`, reversible, sin alteración de ninguna fila existente.

### Riesgos
- **Futuros callers que olviden pasar `account_id`**: mitigado haciendo el parámetro obligatorio (sin `default=None`) en `record_routing_decision()` — un caller nuevo que no lo pase falla de forma explícita e inmediata (`TypeError`), no de forma silenciosa.
- **`account=None` es válido únicamente** para decisiones históricas anteriores a BOOK-04e, o ante un fallo controlado y ya fail-open del propio writer (mismo criterio que el resto de campos opcionales del contrato) — nunca como resultado de omitir el argumento por descuido en un caller nuevo.
- Ninguna escritura adicional fuera de la creación de la `RoutingDecision` — el campo se fija una sola vez, en el mismo `INSERT`, sin ningún `UPDATE` posterior.
- Ninguna modificación del campo `account` después de creado, bajo ninguna circunstancia.
- Bajo — capa de lectura pura para los helpers/admin; el único cambio con escritura real es la línea nueva en el único call site ya existente.

### Dependencias
BOOK-04a (modelo), BOOK-04b (único call site real que puede poblar `account_id`). **No se declara dependencia funcional obligatoria de BOOK-04c ni de BOOK-04d** para resolver la consulta por cuenta — la resolución de FASE 2 (Opción B) elimina esa dependencia por diseño: `account` se fija en el momento del `open`, sin necesitar pasar por `Trade` ni por `BrokerAuditEvent`.

### Estrategia de pruebas
El admin carga sin error y es completamente solo lectura (mismo test estructural ya usado para `BrokerAuditEventAdmin`); `routing_decisions_for_account()` retorna resultados idénticos para una decisión con posición abierta y para la misma decisión tras el cierre de esa posición (prueba directa de que la consulta por cuenta no depende del ciclo de vida de `Position`); `routing_decisions_for_position()` retorna resultados mientras la posición está abierta y una lista vacía tras su cierre — comportamiento documentado, no un defecto; decisiones creadas antes de este bloque (`account=None`, simuladas directamente en el test) siguen siendo consultables por los helpers existentes sin fallar; `record_routing_decision()` sin `account_id` falla con `TypeError`, nunca crea una fila con `account` implícito.

### Definition of Done
- Toda `RoutingDecision` nueva guarda `account` en el momento de su creación.
- Las decisiones anteriores a BOOK-04e permanecen con `account=None`, sin backfill.
- La consulta por cuenta (`routing_decisions_for_account()`) funciona igual antes y después del cierre de la `Position` asociada.
- `RoutingDecisionAdmin` es completamente solo lectura — sin `add`/`change`/`delete`, sin `delete_selected`.
- No se fabrica ningún dato histórico ni se infiere ninguna relación retroactiva.
- El comportamiento del motor de ruteo (decisión trivial, Shadow Mode, netting, cierre) permanece exactamente igual al de BOOK-04a-04d — este bloque es una capa de lectura y un campo denormalizado, no un cambio de lógica.
- Migración aditiva, reversible, verificada (aplicar/revertir/reaplicar) sin pérdida de datos existentes.
- Suite completa del proyecto en verde, sin regresiones.

---

## BOOK-04f — Mecanismo de Activación Controlada (sin reglas de negocio)

### Objetivo
Preparar el mecanismo operativo para que un bloque futuro — **fuera de BOOK-04** — pueda activar reglas de ruteo reales de forma gradual y segura. Ninguna regla se diseña ni se implementa aquí.

### Alcance
- Flags de activación granular adicionales (mecánica de filtrado por símbolo o tipo de cuenta — no las reglas de *qué* activar), mismo patrón canario que `MARKET_DATA_ROUTER_ENABLED`/`MARKET_DATA_ROUTER_SYMBOLS` (Foundation-09).
- Ningún cambio a la decisión trivial introducida en BOOK-04b — este bloque solo prepara el interruptor, no lo acciona con lógica nueva.

### Diseño del mecanismo de gate (resolución de FASE 2)

**1. Dónde vive el gate: `simulator/consumers.py`, no `routing_engine.py`.**
Precedente directo ya probado en este mismo proyecto: `FeedManager._should_use_new_router(symbol)` (`market_data/feeds.py:449-459`) — la decisión de "¿debo delegar a la ruta nueva?" vive en el módulo **caller** (`feeds.py`), no en el módulo **callee** (`market_data/runtime_router/service.py`). BOOK-04f replica exactamente esa misma división de responsabilidades: `consumers.py` es el caller de `routing_engine.py`, así que el gate — "¿debo siquiera intentar Shadow Mode para esta orden?" — vive junto al call site que protege, como método privado de `TradingConsumer`, no dentro de `routing_engine.py`. Colocarlo en `routing_engine.py` acoplaría ese módulo a conocimiento de `TradingConsumer`/`self.account` que hoy no tiene ninguna razón para tener — `routing_engine.py` sigue sin saber nada de símbolos permitidos, tipos de cuenta, ni de ningún estado del consumer, exactamente como hoy.

**2. Firma propuesta**: `TradingConsumer._should_activate_routing_decision(self, symbol: str, account_type: str) -> bool`, definido en `simulator/consumers.py` junto a `_db_open_position_atomic()`. No es `@database_sync_to_async` — no hace ninguna consulta a la DB (ver Riesgos → Rendimiento).

**3. Único cambio al call site existente**: reemplaza exactamente la línea `if getattr(_settings, "ROUTING_ENGINE_ENABLED", False):` (dentro de `_db_open_position_atomic()`) por `if self._should_activate_routing_decision(symbol, self.account.get("account_type", "CHALLENGE")):` — mismo punto exacto de la secuencia ya aprobada en BOOK-04b, ningún otro cambio a esa función.

### ¿Modifica BOOK-04f `_db_open_position_atomic()`?
**Sí.** Un tercer bloque la modifica, de forma mínima: cambia únicamente la condición del `if` que ya gatea todo el bloque de Shadow Mode (introducido por BOOK-04b), de un booleano plano a una llamada a `_should_activate_routing_decision()`. No cambia el orden de ejecución, el lock, la atomicidad, el contenido de la decisión, el punto donde se invoca `record_routing_decision()`, ni ningún otro código dentro del bloque ya aprobado por BOOK-04b/04e.

### Archivos que probablemente participarán
```
trx_simulator/settings.py         — ROUTING_ENGINE_SYMBOLS, ROUTING_ENGINE_ACCOUNT_TYPES (flags de activación granular)
simulator/consumers.py            — nuevo método _should_activate_routing_decision(); cambia la condición del if ya existente en _db_open_position_atomic()
simulator/tests/test_book04f_activation_mechanism.py
```
(`simulator/routing_engine.py` retirado de esta lista — no participa; ver "Diseño del mecanismo de gate" arriba.)

### ¿Requiere migración?
No — los flags nuevos son configuración de entorno (mismo criterio que `MARKET_DATA_ROUTER_ENABLED`/`MARKET_DATA_ROUTER_SYMBOLS`), sin campo ni tabla nueva.

### Vocabulario de `account_type`
`ROUTING_ENGINE_ACCOUNT_TYPES`, cuando se define, contiene únicamente valores tomados de `TradingAccount.ACCOUNT_TYPES` (`RETAIL, ECN, STANDARD, DEMO, CRYPTO, CHALLENGE, FUNDED` — los mismos 7 ya definidos en `simulator/models.py`). Sin validación en tiempo de carga de `settings.py` contra el modelo (evita importar `simulator.models` desde `settings.py`, con el riesgo de `AppRegistryNotReady` que eso implicaría al cargarse antes de que el app registry esté listo) — un valor mal escrito simplemente nunca coincide con ningún `account_type` real en tiempo de ejecución (ver tabla de comportamiento abajo), nunca lanza una excepción ni bloquea el arranque.

### Comportamiento del gate — todos los casos

| Escenario | Comportamiento |
|---|---|
| `ROUTING_ENGINE_ENABLED=False` | Inactivo siempre — mismo interruptor maestro de BOOK-04b, sin cambios. Ninguna otra variable se evalúa. |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_SYMBOLS` ausente o vacío | **Sin restricción por símbolo** — activo para todos los símbolos, idéntico al comportamiento actual de BOOK-04b/04e. Ver justificación de compatibilidad abajo. |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_SYMBOLS` definido, símbolo de la orden SÍ está en la lista | Activo para esa orden (sujeto también al filtro de cuenta si está definido — ver siguiente fila). |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_SYMBOLS` definido, símbolo de la orden NO está en la lista | Inactivo para esa orden — no se evalúa ninguna otra condición. |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_ACCOUNT_TYPES` ausente o vacío | **Sin restricción por tipo de cuenta** — mismo criterio de compatibilidad que `ROUTING_ENGINE_SYMBOLS` vacío. |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_ACCOUNT_TYPES` definido, `account_type` de la cuenta SÍ está en la lista | Activo para esa cuenta (AND con el filtro de símbolo si ambos están definidos, nunca OR). |
| `ROUTING_ENGINE_ENABLED=True`, `ROUTING_ENGINE_ACCOUNT_TYPES` definido, `account_type` NO está en la lista (incluye un valor mal escrito que no coincide con ningún valor real de `TradingAccount.ACCOUNT_TYPES`) | Inactivo para esa cuenta. |
| Variable de entorno mal formada (p. ej. `ROUTING_ENGINE_SYMBOLS` con solo comas/espacios) | Se parsea al mismo `frozenset` vacío que "ausente" (mismo parseo defensivo ya usado por `MARKET_DATA_ROUTER_SYMBOLS`: `s.strip() for s in ... if s.strip()`) — nunca lanza excepción al cargar `settings.py`. |
| Cualquier excepción inesperada durante la evaluación del gate (p. ej. `self.account` ausente o con forma inesperada) | Fail-safe: se trata como inactivo — nunca como activo por defecto, y nunca propaga la excepción hacia `_db_open_position_atomic()`. Mismo criterio exacto que `_should_use_new_router()` en `market_data/feeds.py` (`try/except Exception: return False`). |

**Justificación de la asimetría con `MARKET_DATA_ROUTER_SYMBOLS`** (donde una lista vacía significa "ningún símbolo", no "todos"): `MARKET_DATA_ROUTER_ENABLED` y su allowlist se introdujeron juntos, sin ningún despliegue previo dependiendo del flag maestro por sí solo — vacío-significa-ninguno fue una elección deliberada para forzar una lista canaria explícita desde el día uno. `ROUTING_ENGINE_ENABLED`, en cambio, ya existe desde BOOK-04b y ya activa Shadow Mode para el 100% de símbolos/cuentas cuando está en `True`. Si BOOK-04f adoptara la semántica "vacío = ninguno" para los nuevos flags, cualquier entorno que ya tuviera `ROUTING_ENGINE_ENABLED=True` perdería toda actividad de Shadow Mode en cuanto BOOK-04f se desplegara — un cambio de comportamiento real causado por un bloque cuyo propio Objetivo declara explícitamente que no acciona nada. La semántica "ausente = sin restricción" es la única compatible con el principio rector del plan ("el broker debe quedar completamente funcional después de cada bloque, sin excepción") aplicado a los entornos que ya adoptaron BOOK-04b/04e.

### Riesgos
- **Alcance**: bajo, siempre que se resista la tentación de agregar reglas reales en este bloque — el límite debe quedar explícito en el propio commit y en su documentación.
- **Atomicidad**: nula — el gate se resuelve enteramente con datos ya en memoria antes de llegar a este punto (`symbol` como parámetro ya recibido, `self.account.get("account_type", ...)` ya cacheado en el consumer desde connect/hydrate, `settings.ROUTING_ENGINE_SYMBOLS`/`ROUTING_ENGINE_ACCOUNT_TYPES` ya cargados al arrancar el proceso). Ninguna query nueva se introduce dentro de `transaction.atomic()` ni bajo ningún lock.
- **Concurrencia**: nula — mismos frozensets inmutables que `MARKET_DATA_ROUTER_SYMBOLS`, fijados una sola vez al cargar `settings.py`; sin estado compartido mutable, sin necesidad de ningún lock nuevo.
- **Rendimiento**: despreciable — el gate es una comparación de membership en un `frozenset` (O(1) amortizado) más, en el peor caso, un dict lookup sobre `self.account` ya en memoria. Explícitamente prohibido: el gate nunca debe emitir una query a `TradingAccount` para resolver `account_type` — debe reutilizar `self.account["account_type"]`, ya poblado.
- **Compatibilidad**: sin riesgo si se respeta la semántica "ausente = sin restricción" definida arriba — comportamiento idéntico a hoy para cualquier entorno que aún no haya definido los flags granulares nuevos.

### Dependencias
BOOK-04a → BOOK-04e completos.

### Estrategia de pruebas
- Flag maestro apagado (ya cubierto, sin regresión) — ninguna decisión se registra, sin importar los flags granulares.
- Flag maestro encendido, sin flags granulares definidos → comportamiento idéntico al de BOOK-04b/04e (todos los símbolos/cuentas activan) — prueba de compatibilidad explícita, reutilizando el mismo patrón (`_db_open_sync` + `_FakeConsumer`) ya establecido en `test_book04b_shadow_mode_integration.py` y `test_book04e_routing_visibility.py`.
- Flag maestro encendido + `ROUTING_ENGINE_SYMBOLS` definido → activa solo para símbolos en la lista, inactiva para cualquier otro.
- Flag maestro encendido + `ROUTING_ENGINE_ACCOUNT_TYPES` definido → activa solo para los `account_type` reales de `TradingAccount.ACCOUNT_TYPES` incluidos en la lista, inactiva para cualquier otro (incluyendo un valor no perteneciente a esos 7).
- Ambos flags granulares definidos simultáneamente → AND, no OR (una orden debe cumplir símbolo Y tipo de cuenta para activar).
- Configuración inválida/mal formada (variable vacía, solo separadores) → se comporta igual que "ausente", nunca lanza excepción.
- Excepción inesperada durante la evaluación del gate → capturada, tratada como inactivo, nunca propaga hacia `_db_open_position_atomic()`.
- `_FakeConsumer` (stub reutilizado desde BOOK-04b) debe extenderse con `account_type` en su diccionario `self.account` para poder probar la dimensión de cuenta.
- Ningún test de "regla real" — ninguna existe todavía.

### Definition of Done
BOOK-04 queda cerrado con el motor completo, auditable, visible, y con el interruptor de activación gradual listo — sin una sola regla de negocio de ruteo activa. Un bloque futuro (fuera de este plan) diseña e implementa las reglas reales sobre esta base ya construida.

---

## Consideraciones transversales

### Compatibilidad SQLite (desarrollo/tests) y PostgreSQL (producción)
- `inputs_snapshot` y cualquier otro campo JSON de `RoutingDecision` usan `django.db.models.JSONField`, que Django ya abstrae de forma idéntica sobre SQLite y PostgreSQL — ningún bloque de BOOK-04 depende de un motor concreto para funcionar.
- Ningún bloque de este plan construye una consulta que dependa de operadores JSON exclusivos de PostgreSQL (`?`, `@>`, `jsonb_path_query`, índices `GIN`, etc.). `inputs_snapshot` se escribe y se lee como un bloque opaco — nunca se filtra, ordena ni indexa por una clave dentro de él.
- Todo dato que sí necesite filtrarse, ordenarse o indexarse (`book`, `reason_code`, `decided_at`, `engine_version`, `schema_version`, `decision_id`, `position`, `correlation_id`) vive en una columna tipada propia, no dentro del JSON — mismo precedente ya establecido por `pricing_context`.
- La suite de tests de cada bloque corre sobre SQLite (motor por defecto de desarrollo/test de este proyecto). Ningún test de BOOK-04 asumirá comportamiento exclusivo de PostgreSQL; si en el futuro se introdujera una consulta JSON avanzada, un test debe fallar primero en SQLite para que la diferencia se detecte antes de producción, nunca después.

### Backfill histórico
- Toda `Position` y todo `Trade` creados **antes** de que el bloque correspondiente (BOOK-04b para `Position`, BOOK-04c para `Trade`) exista en producción conservarán `routing_decision = NULL` de forma permanente.
- `NULL` en este campo es una señal explícita e inequívoca de "operación anterior al Routing Engine" — nunca un dato faltante por error, y nunca algo que un backfill posterior deba "corregir".
- **Nunca se fabricará retroactivamente una `RoutingDecision`** para una operación que ya ocurrió antes de que el motor existiera — hacerlo inventaría un hecho que nunca sucedió, exactamente el error que este plan evita desde FASE 2.
- Todas las migraciones de BOOK-04 son aditivas (`CreateModel` en BOOK-04a, `AddField` con `null=True` en BOOK-04b/04c) y no reescriben, recalculan ni tocan una sola fila existente.

### Rollback del flag `ROUTING_ENGINE_ENABLED`
Si el flag se desactiva después de haber estado activo (en cualquier entorno):
- Las `RoutingDecision` ya persistidas permanecen intactas — mismo régimen append-only que `BrokerAuditEvent`, nunca se borran ni se alteran.
- Los enlaces ya escritos (`Position.routing_decision`, `Trade.routing_decision`, `RoutingDecision.position`) no se borran ni se modifican.
- Las órdenes nuevas, a partir del momento de la desactivación, vuelven exactamente al comportamiento de flag-apagado: no se registra ninguna `RoutingDecision` nueva.
- Las posiciones que ya tenían una decisión persistida antes de la desactivación la conservan y la usan con normalidad al cerrarse — BOOK-04c sigue leyendo `Position.routing_decision` sin importar el estado actual del flag, porque lee un dato ya escrito, no decide nada en el momento del cierre.
- Apagar el flag es una operación de **solo configuración** — no dispara, ni requiere, ningún proceso de rollback destructivo de datos ni ninguna migración inversa.

---

## Resumen de impacto por bloque sobre el Trading Engine

| Bloque | Toca `consumers.py` | Función tocada | Migración | Cambia comportamiento con flag apagado | Cambia comportamiento con flag activo |
|---|---|---|---|---|---|
| BOOK-04a | No | — | Sí (tabla nueva) | No | N/A (no hay flag todavía) |
| BOOK-04b | Sí — único punto crítico | `_db_open_position_atomic()` (bajo lock) | Sí (`Position`, `RoutingDecision`) | No | Registra, nunca decide distinto |
| BOOK-04c | Sí — solo lectura al cerrar | escritor(es) de cierre real (fuera del lock de apertura) | Sí (`Trade`) | No | Registra, nunca decide distinto |
| BOOK-04d | Sí — una llamada más | `_order_new()`, después del retorno de `_db_open_position_atomic()` — **no** la función crítica | No | No | Registra evento institucional |
| BOOK-04e | Sí — un argumento más en una llamada existente | `_db_open_position_atomic()`, pasa `account_id` ya disponible a la llamada que BOOK-04b ya introduce — sin nueva lógica de decisión | Sí (`RoutingDecision`: +`account`) | No | No (solo lectura + denormalización) |
| BOOK-04f | Sí — cambia la condición del `if` ya existente | `_db_open_position_atomic()`, sustituye el booleano plano por `_should_activate_routing_decision(symbol, account_type)` — sin nueva lógica de decisión ni cambio al contenido de la decisión | No | No | No decide distinto — solo amplía o restringe CUÁNDO se registra la misma decisión trivial, nunca QUÉ decisión es |

**`_db_open_position_atomic()` es modificada por tres bloques de este plan: BOOK-04b (la única lógica de decisión real, bajo lock), BOOK-04e (un argumento más en esa misma llamada, sin ninguna lógica de decisión nueva) y BOOK-04f (sustituye la condición booleana plana del `if` que gatea todo el bloque por una llamada a `_should_activate_routing_decision()`, sin alterar nada dentro de ese bloque).** BOOK-04d, que en una versión anterior de este plan compartía esa misma función (junto a `EV_POSITION_OPENED`), fue reubicado a `_order_new()` precisamente para no depender de ella.

**En ningún punto de este plan el broker deja de ejecutar exactamente las mismas operaciones que ejecuta hoy.** El Routing Engine, al cierre de BOOK-04, sabe registrar y mostrar decisiones — no sabe, ni debe saber todavía, tomar una decisión distinta de la que el sistema ya toma implícitamente desde su primer commit.

---

## Convención de tags por bloque

Mismo patrón dual ya usado en cada cierre del Audit Trail Engine (tag corto + tag descriptivo versionado):

| Bloque | Tag corto | Tag descriptivo |
|---|---|---|
| BOOK-04a | `book-04a` | `book-04a-routing-foundation-v1` |
| BOOK-04b | `book-04b` | `book-04b-shadow-routing-integration-v1` |
| BOOK-04c | `book-04c` | `book-04c-routing-close-symmetry-v1` |
| BOOK-04d | `book-04d` | `book-04d-routing-audit-trail-v1` |
| BOOK-04e | `book-04e` | `book-04e-routing-staff-visibility-v1` |
| BOOK-04f | `book-04f` | `book-04f-routing-gradual-activation-v1` |

No implementé nada, no modifiqué archivos, no hice commit ni push.
