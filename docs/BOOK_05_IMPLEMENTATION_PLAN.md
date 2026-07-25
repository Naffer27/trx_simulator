# BOOK-05 — Liquidity Engine: Plan de Implementación

| Campo | Valor |
|---|---|
| Rol del documento | Plan técnico de construcción por bloques — sin implementación |
| Fecha | 2026-07-25 |
| Fases previas aprobadas | FASE 0 (arquitectura y especificación), FASE 2 (resolución de arquitectura — modelos independientes) |
| Metodología | Idéntica a la de Audit Trail Engine y BOOK-04: bloques pequeños, cada uno desarrollable, probable, commiteable, etiquetable y estabilizable de forma independiente |
| Alcance de este documento | Solo la secuencia de bloques y su contrato de entrada/salida. Ninguna regla de negocio real de hedge, ningún código. |
| Predecesor cerrado | BOOK-04 (Routing Engine) — `book-04a` … `book-04f`, todos commiteados y tagueados localmente |

---

## Principio rector del plan

Igual que BOOK-04, este es un simulador de bróker (`trx_sim`) — no existe, en ningún punto del código actual, ninguna conectividad de ejecución externa real. BOOK-05 no cambia esa naturaleza: construye el **modelo de datos y el motor de cálculo** de lo que costaría/valdría cubrir exposición externamente con un proveedor de liquidez (LP), de forma simulada y observacional — nunca ejecuta una orden externa, nunca conecta a un LP real.

**Regla arquitectónica no negociable (aprobada en FASE 2):**

> **Nunca modificar una `RoutingDecision` existente. Toda simulación de liquidez vive exclusivamente en `LiquidityDecision` mediante una FK hacia la `RoutingDecision` correspondiente.**

Esta regla gobierna todo el diseño de este documento y todo bloque que lo implemente. `RoutingDecision` es la fuente institucional de verdad para dónde se enrutó realmente una orden (BOOK-04a-04f, ya cerrado) — BOOK-05 nunca escribe en esa tabla, nunca añade un valor a su vocabulario `Book`, nunca reutiliza `RoutingDecision.parent_decision`. Cualquier dato de simulación de liquidez vive exclusivamente en modelos propios de BOOK-05, enlazados por FK de solo lectura hacia la `RoutingDecision` real que los originó.

---

## 1. Objetivo del Liquidity Engine

Construir la capa que modela, de forma **simulada y observacional**, cómo se vería la cobertura externa (hedge) de la exposición del broker si existiera un proveedor de liquidez (LP) real — sin conectar nunca a un LP real, sin ejecutar nunca una orden externa, y sin cambiar en ningún momento el hecho actual de que el 100% del libro es B-Book.

## 2. Problema que resuelve

Hoy el proyecto ya tiene, dispersas y sin conectar entre sí, tres piezas que apuntan exactamente a este problema sin resolverlo:

1. **`TraderScore.routing_profile`** (`simulator/models.py:1231-1247`, poblado por `intelligence_engine.py` después de cada cierre) — clasifica cada cuenta en `INTERNAL`, `REVIEW`, `HEDGE_CANDIDATE` o `ELITE` según comportamiento (win rate, martingale, toxicidad) — pero ningún código real actúa sobre `HEDGE_CANDIDATE`. Es un dato calculado y mostrado en el admin, nunca consumido operacionalmente.
2. **`BrokerSnapshot.hedge_candidate_usd`** (`simulator/exposure_engine.py`, RISK-01) — agrega, en dólares, cuánta exposición pertenece a cuentas `HEDGE_CANDIDATE` — un número de dashboard, sin ningún mecanismo debajo que diga "¿y si cubriéramos esto, cuánto costaría/cuál sería el resultado?".
3. **`RoutingDecision.book`** (BOOK-04) — el campo que registra dónde se enrutó una orden, hoy con un único valor posible (`Book.INTERNAL`), y que **permanece así después de BOOK-05** (ver Principio rector).

El problema que resuelve BOOK-05: **conectar estas tres piezas** con un modelo real de "qué proveedor de liquidez simulado cubriría esta exposición, a qué costo simulado, y qué resultado (P&L) tendría esa cobertura" — sin activar todavía ninguna decisión real de negocio, y sin tocar jamás la `RoutingDecision` real de ninguna posición. Es la pieza de datos/infraestructura que el roadmap oficial coloca justo antes del **Dealing Desk híbrido A-book/B-book** (bloque 4), que sí tomará esas decisiones en tiempo real, consumiendo lo que BOOK-05 construye aquí.

## 3. Cómo se integra con el Routing Engine

**Resolución de FASE 2**: `LiquidityDecision` es un modelo independiente con una FK propia hacia `RoutingDecision` — **no** una fila de `RoutingDecision` con un nuevo valor de `book`, y **no** reutiliza `RoutingDecision.parent_decision`.

- **`routing_engine.py` no se modifica.** `Book.ALL` permanece `(Book.INTERNAL,)`. No se introduce ningún `Book.EXTERNAL_SIMULATED` ni ningún valor nuevo — la sola existencia de una fila en `LiquidityDecision` enlazada a una `RoutingDecision` real ya comunica el hecho de que esa posición tiene una simulación de hedge asociada; no se necesita un valor de `book` nuevo para eso.
- **`broker_ledger.py` no se modifica.** `_book_mode_for_trade()` permanece exactamente `{Book.INTERNAL: BOOK_MODE_B_BOOK}` — su función siempre fue resolver ambigüedad sobre datos **reales**; `LiquidityLedger` es, por definición, siempre simulado, así que no hay ambigüedad que ese traductor deba resolver.
- **`LiquidityDecision.routing_decision`** (FK a `RoutingDecision`, `related_name="liquidity_decisions"`) es el único punto de contacto entre BOOK-05 y BOOK-04 — de solo lectura, unidireccional: `routing_engine.py` no sabe que `LiquidityDecision` existe.
- **No se modifica `_should_activate_routing_decision()` ni el gate de BOOK-04f** — la decisión trivial (`Book.INTERNAL`, siempre) sigue siendo la única que se persiste como decisión real de apertura.

## 4. Arquitectura general

```
┌─────────────────────────────────────────────────────────────────┐
│  TradingConsumer._db_open_position_atomic() / close paths       │
│  (sin cambios de comportamiento — BOOK-04 sigue intacto)        │
└───────────────────────────┬───────────────────────────────────┘
                             │ RoutingDecision (Book.INTERNAL, real,
                             │ NUNCA modificada por BOOK-05)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  liquidity_engine.py (nuevo módulo — mismo nivel que             │
│  routing_engine.py)                                              │
│                                                                    │
│   evaluate_simulated_hedge(position, account, routing_decision)  │
│     lee TraderScore.routing_profile (ya calculado)                │
│     lee LiquidityProvider (registro simulado, nuevo modelo)      │
│     crea una fila en LiquidityDecision (tabla propia) con FK a   │
│       la RoutingDecision real — nunca escribe en RoutingDecision  │
│     — pura función + un writer fail-open, mismo contrato que     │
│       record_routing_decision()                                  │
└───────────────────────────┬───────────────────────────────────┘
                             │ (Shadow Mode, propio flag, gated aparte)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Al cierre: LiquidityLedger entry (modelo propio, mismo patrón   │
│  append-only + idempotente que BrokerLedger.REV_COUNTERPARTY_PNL,│
│  pero en su PROPIA tabla — nunca una fila de BrokerLedger)       │
│  — "si esto se hubiera cubierto con el LP simulado, este habría  │
│  sido el resultado" — nunca sustituye BrokerLedger real           │
└───────────────────────────┬───────────────────────────────────┘
                             │
                             ▼
              BrokerAuditEvent (Category.LIQUIDITY, nuevo,
              mismo patrón que Category.ROUTING de BOOK-04d)
                             │
                             ▼
              Admin de staff (solo lectura para LiquidityDecision/
              LiquidityLedger; CRUD normal para LiquidityProvider,
              porque un LP simulado es CONFIGURACIÓN de staff, no
              un hecho auditado — mismo criterio que
              BrokerSpreadConfig hoy)
```

Ningún componente nuevo se sienta en el camino crítico de `_db_open_position_atomic()` bajo lock — todo el cálculo de hedge simulado ocurre **después** de que la posición/decisión reales ya existen, en su propio bloque `try/except` con su propio nested `transaction.atomic()`, exactamente la disciplina de aislamiento que BOOK-04b ya probó y que este documento no reinventa.

## 5. Modelos nuevos necesarios

**Resolución de FASE 2 — ambas preguntas de arquitectura quedan cerradas: los tres modelos siguientes son independientes, definitivos, ninguno reutiliza ni modifica `RoutingDecision` o `BrokerLedger`.**

- **`LiquidityProvider`** — registro simulado de un LP. Campos: `name`, `symbols_covered` (JSON o M2M — a decidir en FASE 1 de BOOK-05a, detalle de implementación, no de arquitectura), `simulated_spread_markup_pips`, `max_capacity_usd`, `enabled`, `created_at`/`updated_at`. CRUD normal en admin (staff configura LPs simulados, igual que ya configura `BrokerSpreadConfig`).
- **`LiquidityDecision`** — modelo independiente, mismo molde que `RoutingDecision` (dual-versioning `engine_version`/`schema_version`, `decision_id` UUID, `inputs_snapshot` JSON). Campos propios: `routing_decision` (FK a `RoutingDecision`, **de solo lectura, nunca escrita por BOOK-04**, `related_name="liquidity_decisions"`, `on_delete=models.SET_NULL` — mismo criterio de "evidencia sobrevive al padre" ya usado en todo BOOK-04), `provider` (FK a `LiquidityProvider`, nullable), `position` (FK a `Position`, mismo criterio `SET_NULL` que `RoutingDecision.position`), `simulated_spread`, `simulated_cost`. **Nunca modifica la fila de `RoutingDecision` a la que apunta** — cumple la regla arquitectónica del Principio rector por construcción.
- **`LiquidityLedger`** — modelo independiente, mismo patrón append-only e idempotente que `BrokerLedger.REV_COUNTERPARTY_PNL`. Campos: FK a `Trade`, FK a `LiquidityDecision`, `simulated_pnl`, `meta` (JSON), `created_at`. **Nunca es una fila de `BrokerLedger`** — separación estructural, no solo convencional, precisamente para que ningún `Sum('amount')` real futuro pueda absorber dinero simulado por accidente.

**No hace falta ningún modelo nuevo para exposición agregada** — `BrokerSnapshot`/`SymbolExposure`/`TraderClassExposure` ya existen y ya tienen `hedge_candidate_usd`; BOOK-05 los lee, no los duplica.

## 6. Servicios

- **`simulator/liquidity_engine.py`** (nuevo módulo, mismo nivel que `routing_engine.py`):
  - `evaluate_simulated_hedge(...)` — función pura, sin DB, construye el contrato de la simulación (símbolo, LP elegido, spread simulado, costo simulado) — mismo rol que `build_shadow_mode_decision_contract()`.
  - `record_liquidity_decision(...)` — writer fail-open, mismo contrato exacto que `record_routing_decision()` (nested `transaction.atomic()`, nunca lanza, devuelve `None` en fallo, **solo escribe en `LiquidityDecision`, nunca en `RoutingDecision`**).
  - `select_simulated_provider(symbol, exposure_usd)` — pura función de selección entre `LiquidityProvider` activos que cubren ese símbolo y tienen capacidad — determinista, sin llamada de red (no hay red a la que llamar).
- **`simulator/liquidity_ledger.py`** (módulo propio, definitivo — no una función añadida a `broker_ledger.py`) — el writer de cierre, mismo molde que `create_broker_counterparty_entry()`: idempotente (`get_or_create` sobre `(source_trade, ...)`), un evento de auditoría por fila nueva, nunca por un replay.
- **Ninguna modificación a `routing_engine.py` ni a `broker_ledger.py`** — confirmado en la sección 3. `record_routing_decision()`, `build_shadow_mode_decision_contract()`, `_book_mode_for_trade()`, `create_broker_counterparty_entry()` y todos sus tests quedan completamente intactos.

## 7. Flujo completo desde una orden hasta un proveedor de liquidez (simulado)

1. Trader envía orden → `_order_new()` → `_db_open_position_atomic()` (sin cambios).
2. BOOK-04's Shadow Mode registra la `RoutingDecision` real (`Book.INTERNAL`, siempre) — sin cambios, sigue siendo la única decisión que afecta cualquier comportamiento.
3. **Después** de que la transacción de apertura ya confirmó (fuera del lock, mismo criterio que BOOK-04d usó para mover la auditoría a `_order_new()`): si `LIQUIDITY_ENGINE_ENABLED` está activo (flag propio, ver sección 10), se evalúa `TraderScore.routing_profile` de la cuenta (ya calculado, sin query adicional más allá de la relación `OneToOneField` ya existente).
4. Si el perfil es `HEDGE_CANDIDATE` (o el flag granular lo permite para cualquier perfil, mismo patrón de allowlist que BOOK-04f), `liquidity_engine.py` selecciona un `LiquidityProvider` simulado activo para ese símbolo y calcula el contrato de simulación.
5. Se persiste una `LiquidityDecision`, con `routing_decision` apuntando a la `RoutingDecision` real de esa posición — **sin escribir ni modificar esa `RoutingDecision`**. Nada de esto afecta el precio de ejecución, el spread cobrado al trader, el margen, ni el balance — es un registro paralelo, puramente observacional, en su propia tabla.
6. Al cierre de la posición (mismos tres escritores reales que BOOK-04c ya identificó: `_db_close_position_atomic`, `tasks._close_position_sync`, `admin.py::force_close`), se calcula el resultado simulado de esa cobertura y se persiste una fila de `LiquidityLedger` — el equivalente, para el LP simulado, de lo que `create_broker_counterparty_entry()` ya hace para el broker real, pero en su propia tabla.
7. Se emite un `BrokerAuditEvent` bajo `Category.LIQUIDITY` documentando el ciclo completo.
8. En ningún punto de este flujo se envía una orden a ningún sistema externo — no existe tal sistema en este proyecto, y este documento no propone construir uno.

## 8. Cómo convivirá con...

- **Audit Trail** — nueva `Category.LIQUIDITY`, mismo molde exacto que `Category.ROUTING` (BOOK-04d): distinta de `MONITORING`, reutiliza `events_for_account()`/`events_for_trade()` sin ningún cambio a `broker_audit.py` más allá de la nueva constante de categoría y sus `EV_*`.
- **Broker Ledger** — `BrokerLedger` **no se modifica en ningún sentido**: ni nuevo `revenue_type`, ni `AlterField`. `LiquidityLedger` es un modelo separado y definitivo (sección 5). `_book_mode_for_trade()` no se toca (sección 3).
- **`RoutingDecision`** — **de solo lectura, siempre.** BOOK-05 nunca escribe, nunca actualiza, nunca añade un valor a `book` en esta tabla — la regla del Principio rector es absoluta. `LiquidityDecision.routing_decision` es la única forma en que BOOK-05 se relaciona con ella, vía FK propia.
- **`TradingAccount`** — solo lectura. BOOK-05 no añade ningún campo a `TradingAccount` ni a `TraderScore` — consume `TraderScore.routing_profile` tal como `intelligence_engine.py` ya lo calcula, sin duplicar esa lógica de clasificación.

## 9. Estados

- `LiquidityProvider`: `enabled` (booleano simple, configurado por staff) — sin máquina de estados compleja; no hay "conexión" real que pueda caerse.
- `LiquidityDecision`: sin estado propio más allá de existir o no (mismo criterio append-only que `RoutingDecision` — una decisión no "cambia de estado"; si algo cambia, se crea una `LiquidityDecision` nueva, la `RoutingDecision` real jamás se toca).
- No hay estado de "orden enviada al LP / confirmada / rechazada" — deliberadamente, porque no hay ninguna orden real que enviar. Cualquier estado de ese tipo sería fabricar un flujo de ejecución que no existe, violando el principio de "nunca fabricar hechos que no ocurrieron" que gobierna todo este proyecto.

## 10. Feature flags

Mismo patrón exacto que `ROUTING_ENGINE_ENABLED`/`ROUTING_ENGINE_SYMBOLS`/`ROUTING_ENGINE_ACCOUNT_TYPES` (BOOK-04b/04f) y `MARKET_DATA_ROUTER_ENABLED`/`SYMBOLS` (Foundation-09):

- `LIQUIDITY_ENGINE_ENABLED` (booleano, default `False`) — interruptor maestro.
- `LIQUIDITY_ENGINE_SYMBOLS` (frozenset, default vacío = sin restricción, mismo criterio de compatibilidad ya justificado en BOOK-04f).
- `LIQUIDITY_ENGINE_ROUTING_PROFILES` (frozenset sobre los valores de `TraderScore.ROUTING_CHOICES` — p. ej. `{"HEDGE_CANDIDATE"}` para el canario inicial más conservador).

Todos apagados por defecto — cero cambio de comportamiento hasta activación explícita, en cualquier entorno.

## 11. Seguridad

- Ningún dato de PII nuevo — `LiquidityProvider`/`LiquidityDecision` no tocan nombre, email, ni ningún dato personal del trader (mismo criterio que `RoutingDecision`).
- Ninguna credencial de API externa — no hay LP real, no hay secretos que gestionar. Si en un futuro bloque (fuera de BOOK-05) se conectara un LP real, ese sería un cambio de superficie de seguridad completamente distinto (credenciales, mTLS, rate limits) — explícitamente fuera de este documento.
- `LiquidityProvider` es CRUD de staff en admin — mismo nivel de permiso (`is_staff`/`is_superuser`) que `BrokerSpreadConfig` hoy, sin superficie nueva de autenticación.

## 12. Auditoría

- `Category.LIQUIDITY` nueva, misma disciplina que `Category.ROUTING`.
- Cada `LiquidityDecision` creada, cada `LiquidityLedger` escrito genera exactamente un evento — mismo criterio "un hecho real, un evento" que `create_broker_counterparty_entry()` ya aplica (idempotencia vía `get_or_create`, evento solo en `_created=True`).
- Nada de esto es opcional para cumplimiento — es informativo/analítico, pero append-only igual que el resto del Audit Trail Engine.

## 13. Fail-safe

Misma disciplina exacta que BOOK-04 estableció y que este documento no relaja en ningún punto:
- `record_liquidity_decision()` nunca lanza — nested `transaction.atomic()`, captura todo, devuelve `None` en fallo.
- Un fallo en cualquier punto del flujo de BOOK-05 **nunca** afecta si la posición abre/cierra, el balance, el margen, la comisión, el spread cobrado al trader, ni el Audit Trail de BOOK-04.
- El gate de activación (`LIQUIDITY_ENGINE_ENABLED` + allowlists) sigue el mismo contrato que `_should_activate_routing_decision()`: nunca consulta la DB, nunca lanza, cualquier excepción se trata como "no simular".
- **Un fallo en cualquier punto de BOOK-05 nunca puede resultar en una escritura a `RoutingDecision`** — estructuralmente imposible, porque ningún código de BOOK-05 tiene un writer que apunte a esa tabla.

## 14. Compatibilidad con BOOK-04

- **Cero cambios a `routing_engine.py` y cero cambios a `broker_ledger.py`** (resolución de FASE 2, sección 3).
- Cero cambios a `_db_open_position_atomic()` en su lógica de decisión real (BOOK-04b) ni en su gate (BOOK-04f) — BOOK-05 se engancha **después**, en su propio bloque protegido.
- Cero cambios al call site de BOOK-04d en `_order_new()`.
- `RoutingDecision.parent_decision` permanece exactamente como está — sin usar, sin modificar, mismo estado que tiene desde BOOK-04a.
- Ningún `AlterField` sobre `RoutingDecision` ni sobre `BrokerLedger` en ninguna migración de BOOK-05.

## 15. Roadmap interno dividido en bloques pequeños

```
BOOK-05a  Liquidity Provider Foundation (modelos LiquidityProvider + LiquidityDecision, admin CRUD/solo-lectura, cero integración con el flujo real)
    │
    ▼
BOOK-05b  Simulated Hedge Pricing (motor puro de cálculo, sin escritura de decisión todavía)
    │
    ▼
BOOK-05c  Shadow Mode Integration (LiquidityDecision persistida vía FK a RoutingDecision — único punto que toca el flujo real, después del lock, RoutingDecision nunca escrita)
    │
    ▼
BOOK-05d  Cierre simétrico (LiquidityLedger al cierre, mismos tres escritores que BOOK-04c ya mapeó)
    │
    ▼
BOOK-05e  Integración con Audit Trail (Category.LIQUIDITY)
    │
    ▼
BOOK-05f  Visibilidad para Staff (admin solo lectura de LiquidityDecision/LiquidityLedger, ya con LiquidityProvider CRUD desde 05a)
    │
    ▼
BOOK-05g  Mecanismo de Activación Controlada (flags granulares — mismo patrón que BOOK-04f)
```

Cada bloque sigue la misma disciplina de FASE 1 (descubrimiento) → aprobación → implementación → auditoría final → commit + tags, exactamente como los seis bloques de BOOK-04. La arquitectura de datos ya quedó resuelta en esta FASE 2 — BOOK-05a no necesita una FASE 2 propia para eso; su FASE 1 se limita a detalles de implementación (p. ej. forma exacta de `symbols_covered` en `LiquidityProvider`).

## 16. Riesgos

- **Atomicidad/concurrencia**: nula si se respeta el diseño de la sección 4 — todo el cálculo de hedge ocurre fuera del lock global (`BrokerRiskLock → TradingAccount → Position`), en su propio nested savepoint. Riesgo real solo si un bloque futuro decide que BOOK-05 debe influir en la apertura en tiempo real (eso ya no sería Shadow Mode, sería el propio Dealing Desk híbrido, bloque 4 del roadmap, explícitamente fuera de este documento).
- **Rendimiento**: despreciable en Shadow Mode — un `OneToOneField` ya cacheable (`TraderScore`), sin llamadas de red reales.
- **Compatibilidad**: sin riesgo mientras los flags permanezcan apagados por defecto (mismo criterio que todo BOOK-04).
- **Riesgo de alcance**: el más importante a vigilar activamente durante la implementación — es muy fácil que "simular qué pasaría con un LP" se deslice hacia "decidir de verdad qué pasa con un LP", que es el bloque 4 del roadmap (Dealing Desk híbrido), no este. Cada sub-bloque debe declarar explícitamente, como BOOK-04f lo hizo, que resiste esa tentación.
- **Riesgo de violar la regla arquitectónica del Principio rector**: mitigado por diseño, no solo por disciplina — `LiquidityDecision`/`LiquidityLedger` son modelos separados sin ningún writer que apunte a `RoutingDecision`/`BrokerLedger`; una violación requeriría escribir código nuevo que contradiga explícitamente la arquitectura aprobada aquí, no un simple descuido de filtrado.

## 17. Estrategia de pruebas

- Mismo patrón de infraestructura ya establecido: `_FakeConsumer`/`_db_open_sync` para los puntos que tocan `_db_open_position_atomic()`, `CaptureQueriesContext` para probar cero queries nuevas donde se prometa cero queries, `transaction.atomic()` + excepción deliberada para probar fail-open, tags/canary de settings vía `override_settings`.
- Cobertura mínima por sub-bloque: modelo (`LiquidityProvider` CRUD, constraints), writer fail-open, gate granular (mismo matiz de casos que BOOK-04f: flag apagado, allowlist ausente/presente, configuración inválida, excepción inesperada), integración de cierre (idempotencia, exactamente un evento por cierre real), admin (solo lectura donde corresponda, CRUD donde corresponda).
- **Prueba estructural obligatoria, en cada sub-bloque que toque el flujo real (BOOK-05c en adelante): confirmar que ninguna `RoutingDecision` existente cambia — ni su `book`, ni ningún otro campo — antes y después de que se ejecute el flujo de BOOK-05.** Mismo tipo de prueba que ya usa BOOK-04b para demostrar que un fallo en Shadow Mode nunca afecta la posición.
- Ningún test de "el LP real respondió tal precio" — no existe tal LP; todo test es sobre el modelo de simulación, nunca sobre I/O externo real.

## 18. Definition of Done

BOOK-05 queda cerrado cuando: existe un registro configurable de LPs simulados; existe un motor que, para exposición clasificada como candidata a hedge, calcula y persiste una simulación de cobertura enlazada — vía FK, nunca por escritura — a la `RoutingDecision` real; existe un ledger simulado del resultado de esa cobertura al cierre, en su propia tabla; todo está auditado bajo `Category.LIQUIDITY`; todo está detrás de flags apagados por defecto; nada de esto afecta ejecución, precio, margen, comisión, ni el comportamiento ya cerrado de BOOK-04; **ninguna `RoutingDecision` fue jamás modificada por este bloque**; y ninguna decisión real de A-book/B-book se toma en ningún punto. El bloque 4 del roadmap oficial (Dealing Desk híbrido) queda como el consumidor futuro de esta base.
