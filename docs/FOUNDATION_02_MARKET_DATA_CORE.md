# FOUNDATION-02 — Núcleo Definitivo del Market Data Engine

**Bloque:** FOUNDATION-02 — Diseño arquitectónico puro
**Fecha:** 2026-07-11
**Alcance:** Solo arquitectura. No se creó ninguna clase, no se modificó ningún archivo de runtime, no hay commits asociados a este documento.
**Precondición leída:** `docs/MARKET_DATA_ARCHITECTURE.md` (MD-1, aprobado) — este documento profundiza su §5 ("Arquitectura objetivo") y no lo contradice.
**Horizonte de diseño:** debe seguir siendo válido soportando Forex, Crypto, Gold, Silver, Oil, Indices, Stocks, Futures y Options dentro de cinco años.

---

## 0. Principios de diseño

Estos principios son la vara con la que se valida cada decisión de abajo. Si un componente futuro los viola, el componente está mal diseñado, no el principio.

1. **El núcleo es agnóstico de proveedor.** Ningún componente central (routing, riesgo, margen, UI, ledger) puede conocer el nombre de un proveedor concreto ("Binance", "Finnhub", "OANDA"). Solo el Adapter de ese proveedor lo sabe.
2. **El núcleo es agnóstico de asset class, salvo donde el dato mismo lo exige.** Nada de `if asset_class == "forex"` disperso en motores de riesgo o exposición. La lógica de negocio lee campos de `InstrumentProfile`; solo `InstrumentProfile` mismo sabe que un future tiene `expiry_date` y un forex no.
3. **Lo que varía por proveedor vive en el Adapter y en el Capability Registry — nunca en el `NormalizedTick`, nunca en el motor de riesgo.**
4. **Fail-safe, no fail-fast, en dato de mercado.** El sistema nunca debe caerse ni operar con un precio silenciosamente incorrecto. Debe declarar explícitamente cuando un dato es *stale*, *synthetic* o *degraded* — la decisión de confiar o no en ese dato se delega al consumidor (riesgo, UI), no se oculta.
5. **Declarativo sobre imperativo.** Cadenas de failover, capacidades de proveedor y perfiles de instrumento son **datos** versionables y auditables — no ramas `if/else` enterradas en un feed loop. Esto es exactamente lo que hoy falla: `_try_live()` en `feeds.py` tiene "Binance → Kraken → Finnhub" cableado en código, no declarado como dato.
6. **Añadir un asset class completo (p.ej. Options) no debe tocar código de otro componente** — solo debe requerir añadir datos: un nuevo `InstrumentProfile`, una entrada nueva en el Capability Registry.
7. **Todo contrato de datos está versionado explícitamente.** `NormalizedTick` y `InstrumentProfile` no son "lo que el código produce hoy" — son contratos con número de versión, para que un consumidor pueda detectar y rechazar una forma que no entiende en vez de malinterpretarla en silencio.
8. **La degradación nunca es indistinguible de la operación normal, y en dinero real restringe por defecto.** Un precio sintético jamás se ve, desde ningún consumidor, como un precio real. Cuando no hay dato confiable, el sistema por defecto reduce lo que se permite hacer (nunca lo amplía) hasta que la confiabilidad se recupere.

---

## 1. NormalizedTick

Es el contrato universal entre "cualquier proveedor de datos" y "cualquier consumidor dentro del broker". Es el componente más importante de los cuatro porque es el que **nunca debe cambiar de forma** cuando se añade un proveedor o un asset class — si cambia, el aislamiento se rompió.

### 1.1 Qué debe contener (universal, todo asset class)

| Campo | Propósito |
|---|---|
| `schema_version` | Versión del contrato `NormalizedTick` (p.ej. `"1.0"`). Ver §8 — permite a un consumidor rechazar o degradar con seguridad una forma que no reconoce, en vez de leerla mal en silencio |
| `symbol` | Símbolo canónico del broker (el de `InstrumentProfile`), nunca el símbolo crudo del proveedor |
| `asset_class` | Para que el consumidor sepa qué campos opcionales esperar sin adivinar |
| `bid` / `ask` | Si el instrumento cotiza dos lados |
| `mid` | Derivado (`(bid+ask)/2`), nunca una fuente independiente |
| `timestamp_provider` | Hora que reportó el proveedor de origen |
| `timestamp_received` | Hora en que el broker lo recibió — la diferencia contra `timestamp_provider` es la métrica de latencia/staleness |
| `provider_id` | Trazabilidad de origen — **solo para auditoría/telemetría**, nunca para lógica de negocio aguas abajo |
| `sequence` | Monotonic, por proveedor — detecta ticks fuera de orden o duplicados |
| `source_state` | `PRIMARY` \| `SECONDARY` \| `SIMULATION` \| `RECOVERY` — heredado del estado del `ProviderRouter` en el momento de emisión |
| `is_synthetic` | `true` si el precio fue generado por el simulador interno, no observado en mercado real |
| `is_stale` | `true` si excede el `max_tick_gap` esperado para ese `asset_class` pero se sigue sirviendo por continuidad |

### 1.2 Qué debe contener (opcional, solo si el asset class lo requiere)

| Campo | Aplica a |
|---|---|
| `last_trade_price` | Instrumentos donde no hay bid/ask continuo (opciones ilíquidas — solo prints de trade) |
| `volume` | Crypto, stocks |
| `open_interest` | Futures, options |
| `settlement_price` | Futures, al cierre de sesión |

Regla dura: **un campo opcional ausente es `null`, nunca `0` ni un string vacío.** Un `open_interest: 0` es ambiguo (¿no hay contratos abiertos, o el proveedor no lo reportó?); `null` no lo es.

Fuera de alcance deliberado: **implied volatility, greeks, y cualquier dato derivado/calculado no pertenecen al `NormalizedTick`.** El tick es un hecho de mercado observado, no un producto de un motor de pricing. Si en el futuro se necesita IV para options, es un consumidor derivado que lee el tick, no un campo del tick.

### 1.3 Qué jamás debe contener

- **El símbolo crudo del proveedor** (`"FX:EURUSD"`, `"XBT/USD"`, `"BTCUSDT"`) — eso muere dentro del Adapter; la traducción a símbolo canónico ocurre antes de que el tick exista.
- **El markup del broker.** El spread que ve el usuario final incluye el markup de `BrokerSpreadConfig` (`spread_engine.py`) — eso se aplica **después**, nunca dentro del tick. El `NormalizedTick` es precio crudo de mercado.
- **Lógica de negocio**: margen, apalancamiento, tamaño de posición, PnL. El tick es un hecho, no una decisión de trading.
- **Estado de ninguna cuenta de usuario.** El tick es global por símbolo — nunca por cuenta.
- **Credenciales, tokens o API keys**, ni en el payload ni en logs adyacentes al tick.
- **El shape crudo del protocolo del proveedor** (campos como `"e"`, `"s"`, `"p"`, `"T"` de un mensaje de Binance). El Adapter traduce; si el shape crudo se filtra aguas abajo, el aislamiento de proveedor ya se rompió.

### 1.4 Cómo llega desde cualquier proveedor

```
Provider raw feed (WS o REST, shape propio de cada proveedor)
        │
        ▼
Provider Adapter  ── dumb, un adapter por proveedor
        │            habla el protocolo del proveedor, traduce
        │            symbol crudo → symbol canónico (vía provider_symbol_map
        │            de InstrumentProfile), produce NormalizedTick
        │            NO conoce reglas de negocio, NO decide failover
        ▼
ProviderRouter  ── decide si ESTE tick de ESTE proveedor es la fuente
        │           activa para este symbol ahora mismo; lo descarta
        │           silenciosamente si no lo es (o lo usa para probes
        │           de recuperación en estado RECOVERY)
        ▼
Normalized Bus  ── Channels group "feed_{symbol}" + Redis cache
        │           (ya existe, sin cambios)
        ▼
   Consumidores
```

### 1.5 Cómo lo consume el resto del broker

- **`TradingConsumer` / dashboard**: lee bid/ask del bus, aplica `spread_engine.broker_price()` **encima** del tick — el markup nunca contamina el tick en sí.
- **`risk_engine` / `exposure_engine` / tareas de Celery**: leen el último `NormalizedTick` cacheado por símbolo, ignoran los campos irrelevantes para su asset class, y respetan `is_stale`/`is_synthetic` para decidir si evaluar un stopout ahora o esperar a que el dato sea confiable.
- **Regla dura**: ningún consumidor importa un Adapter directamente ni decide lógica de negocio en función de qué proveedor originó el tick. `provider_id` es trazabilidad, no una condición de negocio.

### 1.6 Orden e idempotencia de ticks

El tick trae tres marcas temporales/ordinales con roles distintos, no intercambiables:

- **`timestamp_provider`** — hora que declara el proveedor de origen. Es la referencia de orden *dentro* de ese proveedor.
- **`timestamp_received`** — hora en que el Adapter del broker lo recibió. Nunca se usa para ordenar ticks entre sí; solo para medir latencia/staleness (§1.1).
- **`sequence`** — contador monotónico **cuando el proveedor lo ofrece** (no todos lo hacen). Cuando existe, es la señal de orden preferente sobre `timestamp_provider`, porque un proveedor puede reenviar dos ticks con el mismo timestamp truncado.

**Monotonicidad — regla por par `(provider_id, symbol)`, no global.** Cada Adapter mantiene el último `sequence` (o, en su ausencia, el último `timestamp_provider`) aceptado por cada `(provider_id, symbol)` que sirve. Un tick entrante con `sequence` (o `timestamp_provider` a falta de `sequence`) menor o igual al último aceptado para ese mismo par se considera **duplicado o fuera de orden** y se descarta en el borde del Adapter — nunca llega al `ProviderRouter` ni al bus. La monotonicidad **no** se exige entre proveedores distintos: `Primary` y `Secondary` pueden tener relojes/latencias distintas, y eso es exactamente lo que el estado (`source_state`) ya declara.

**Política en transición de estado (el caso que sí cruza proveedores).** En un cambio `RECOVERY → PRIMARY`, el `ProviderRouter` no debe dejar que el primer tick recuperado de Primary sobreescriba en el bus un tick más reciente ya servido por Secondary o Simulation. Regla: el router compara `timestamp_provider` del tick entrante contra el `timestamp_received` del último tick efectivamente publicado al bus (de cualquier fuente) para ese símbolo; si el candidato es más viejo, se usa solo para completar la validación de recuperación (§3.2), no se publica.

**Regla dura de idempotencia:** publicar el mismo tick dos veces (mismo `provider_id` + `symbol` + `sequence`/`timestamp_provider`) debe ser un no-op observable para cualquier consumidor — el bus y los consumidores nunca deben tratar una repetición como un nuevo evento de precio.

---

## 2. InstrumentProfile

Generaliza el `SymbolSpec` actual (`market_data/symbol_specs.py`), que hoy ya cubre bien forex/crypto/metals/index como CFD perpetuo. Falta lo que exige cinco años de roadmap: sesión de mercado, expiración, y N proveedores sin explotar el dataclass.

### 2.1 Campos por categoría

**A. Identidad**
| Campo | Nota |
|---|---|
| `symbol` | Canónico, igual que hoy |
| `asset_class` | `forex \| crypto \| metal \| energy \| index \| stock \| future \| option` |
| `underlying_symbol` | Para futures/options: a qué instrumento referencia. Para spot, `null` |
| `venue` | Dónde se negocia nominalmente (informativo — no necesariamente de dónde viene el feed) |

**B. Definición del contrato** — ya existen y generalizan bien: `contract_size`, `min_lot`, `max_lot`, `lot_step`.
Se añade: `contract_multiplier` (valor monetario por punto de movimiento en moneda de settlement — para futures/options puede diferir conceptualmente de `contract_size`).

**C. Precio** — ya existen y generalizan bien: `tick_size`, `pip_size`, `price_decimals`, `base_price`, `sim_drift`.

**D. Costos de ejecución** — ya existen: `spread`, `commission_pct`. Se añade `commission_fixed` (futures/options suelen cobrar por contrato, no por %).

**E. Margen** — ya existen: `max_leverage`, `margin_mode` (`leverage` \| `percent`). El campo ya está preparado para crecer sin romper nada; cuando aplique margen real tipo SPAN (futures/options), se añade como nuevo valor de `margin_mode`, no como columna nueva.

**F. Calendario de sesión — campo nuevo, crítico, no existe hoy**
| Campo | Nota |
|---|---|
| `trading_calendar_id` | Referencia a un calendario: 24/7 (crypto), 24/5 (forex/metals/oil), horario de bolsa con feriados (stocks/index/futures) |

**Por qué es crítico:** sin esto, un silencio de feed fuera de horario (NYSE cerrado) se interpreta indistinguiblemente de un proveedor caído en plena sesión — y el `ProviderRouter` dispara un failover innecesario. El router necesita poder preguntar "¿el mercado está cerrado ahora mismo?" antes de decidir que un proveedor falló.

**G. Settlement / expiración — solo `future` y `option`**
`contract_month`, `expiry_date`, `last_trading_day`, `delivery_type` (`cash` \| `physical`), `settlement_price_source`.

**H. Específico de `option`**
`strike_price`, `option_right` (`call` \| `put`), `exercise_style` (`american` \| `european`) — comparte `underlying_symbol` con futures.

**I. Ruteo de datos — reemplaza los campos sueltos actuales**
Hoy `SymbolSpec` tiene `finnhub_symbol`, `kraken_symbol`, `exchange_symbol` como campos individuales. Se reemplaza por:

| Campo | Nota |
|---|---|
| `required_capabilities` | Lista de capacidades que **cualquier** proveedor debe tener para servir este instrumento (p.ej. `["bid_ask_streaming", "ohlc_1m"]`) |
| `provider_symbol_map` | `{provider_id: símbolo crudo de ese proveedor}` — generaliza a N proveedores sin añadir un campo nuevo por cada proveedor futuro |

**J. Gate de backend** — ya existe: `enabled`.

**K. Versionado — campo nuevo, ver §8**
| Campo | Nota |
|---|---|
| `profile_version` | Incrementa en cada cambio de forma o semántica de este perfil (no en cada edición cosmética) — permite a un consumidor detectar que está leyendo una versión de contrato distinta a la que espera |
| `updated_at` | Timestamp de la última modificación del perfil — auditoría operativa, no sustituye a `profile_version` |

### 2.2 Tabla de aplicabilidad por asset class

| Categoría | forex | crypto | metal/energy | index (CFD) | index (future) | stock | future | option |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Contrato/precio/margen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Calendario de sesión | 24/5 | 24/7 | 24/5 | 24/5 aprox | bolsa | bolsa | bolsa | bolsa |
| Settlement/expiración | — | — | — | — | ✓ | — | ✓ | ✓ |
| Strike/right | — | — | — | — | — | — | — | ✓ |

Nota deliberada: **"index" hoy en `symbol_specs.py` es un CFD de índice perpetuo (sin expiry).** Un future de índice real sí tiene expiración. El `InstrumentProfile` debe distinguir explícitamente ambos casos por `asset_class` (`index` vs `future` con `underlying_symbol` apuntando al índice) — no asumir que todo lo que se llama "index" se comporta igual.

### 2.3 Fuente de verdad: code vs DB

Esta pregunta ya está identificada como decisión pendiente en el roadmap MD-1 (bloque MD-5) y en el propio código: `Instrument` (modelo DB) existe hoy desconectado del runtime, mientras `SymbolSpec` (código) es la fuente viva.

**Recomendación de este documento:** code-first solo para el set inicial pequeño (el actual: ~4 forex + 3 crypto + 2 metals + 3 index). **DB-backed antes de escalar a stocks u options** — un archivo Python escrito a mano no escala a miles de tickers ni a cadenas de opciones con decenas de strikes/expiries por subyacente, y ya existe el modelo `Instrument` esperando ese rol. Camino sugerido: el dataclass conceptual sigue siendo el *shape* validado (el contrato de campos); las instancias se pueblan desde DB con cache en memoria, no se reemplaza `symbol_specs.py` de golpe.

---

## 3. ProviderRouter

Decide, por símbolo, qué proveedor está activo ahora mismo — y lo hace como máquina de estados explícita, no como una cadena de `try/except` anidada como hoy en `_try_live()`.

### 3.1 Estados y ciclo

```
                  ┌─────────────┐
      ┌──────────▶│   PRIMARY   │◀───────────────┐
      │           └──────┬──────┘                 │
      │                  │ timeout / fallos        │ validación de
      │                  │ consecutivos / stale     │ recuperación
      │                  ▼                          │ exitosa
      │           ┌─────────────┐            ┌──────┴──────┐
      │           │  SECONDARY  │───────────▶│  RECOVERY   │
      │           └──────┬──────┘  probes en  └──────┬──────┘
      │                  │ también falla      background      │
      │                  ▼                                     │ validación
      │           ┌─────────────┐                               │ fallida
      │           │ SIMULATION  │◀──────────────────────────────┘
      │           └──────┬──────┘
      │                  │ resync REST + probes periódicos
      └──────────────────┘  logran contacto real
```

El ciclo declarado por el usuario (Primary → Secondary → Simulation → Recovery → Primary) es el camino principal; en la práctica existen atajos (p.ej. Secondary puede recuperar Primary directamente sin pasar por Simulation si la caída fue breve) — el diagrama de arriba refleja ambos.

### 3.2 Responsabilidades por estado

**PRIMARY** — El proveedor con mejor *capability match* y mejor latencia esperada para este símbolo (según el Capability Registry), entregando ticks dentro de SLA. Ningún fallback activo. Sale de este estado por: N fallos de conexión consecutivos, o silencio mayor a `max_tick_gap` — **excepto si `trading_calendar_id` indica mercado cerrado**, en cuyo caso el silencio es esperado y no dispara failover.

**SECONDARY** — Siguiente proveedor con capability match, según el orden que resulta de cruzar `InstrumentProfile.required_capabilities` contra el Capability Registry (nunca hardcoded). Cada tick emitido lleva `source_state=SECONDARY` explícito. En paralelo, el router puede sondear pasivamente si Primary volvió.

**SIMULATION** — Se activa solo cuando ningún proveedor real (ni Primary ni Secondary) responde dentro de SLA. Nunca inventa un precio desde cero: siempre intenta un resync REST antes del primer tick sintético (como ya hace `_resync_price` hoy) y reintenta periódicamente mientras dura. **Cada tick lleva `is_synthetic=true` sin excepción** — no negociable, porque el motor de riesgo y el dashboard deben poder decidir no abrir posiciones nuevas (o advertir al usuario) con precio sintético. Responsabilidad exclusiva: continuidad de servicio, no precisión de mercado.

**RECOVERY** — Estado de transición, no de servicio permanente. Mientras Simulation sigue sirviendo, el router lanza sondas periódicas a Primary (y opcionalmente Secondary) en background. Una sonda exitosa no basta: requiere un umbral de validación (N sondas consecutivas exitosas, o coherencia del precio recuperado contra el último precio sintético dentro de un rango razonable) antes de promover de vuelta — esto evita *flapping* (rebotar entre estados por un proveedor inestable). Si la validación falla repetidamente, se mantiene en Simulation; Recovery tiene su propio timeout, nunca queda atascado.

**Vuelta a PRIMARY** — Solo tras validación exitosa en Recovery. Nunca salta directo de Simulation a Primary sin pasar por validación.

### 3.3 Timeouts — deben ser configurables por `asset_class`, no un único valor global

Hoy `MAX_FAILURES=3` es global y está repetido en cada `_*_loop` (`_binance_loop`, `_kraken_loop`, `_finnhub_loop`), y se resetea al reconectar sin memoria de estado entre reintentos. Deben pasar a ser datos:

- `max_tick_gap` (dependiente de asset_class — crypto 24/7 tolera un gap corto; forex algo mayor; durante mercado cerrado no aplica)
- `consecutive_failures_threshold`
- `recovery_probe_interval` / `recovery_confirmations_required`
- `simulation_resync_interval` (ya existe como `SIM_RESYNC_INTERVAL`)

### 3.4 Ámbito del estado — nota de escala a 5 años

El estado del router debe rastrearse **por símbolo**, con salud interna **por par (proveedor, símbolo)** para saber cuál secondary elegir — esto ya lo señaló MD-1 como reemplazo del contador global actual.

Más importante: hoy `FeedManager` vive dentro del mismo proceso ASGI (Daphne) que sirve WebSockets a usuarios. Si el broker escala a múltiples workers/instancias, cada uno tendría su propio `ProviderRouter` en memoria, decidiendo estado de forma independiente — un worker podría estar en `PRIMARY` y otro en `SIMULATION` para el mismo símbolo al mismo tiempo. El diseño a 5 años debe asumir estado compartido (Redis, o un servicio de market-data dedicado con un único router por símbolo), no un router en memoria de proceso.

### 3.5 Degradación segura para real-money — política de order gating

**Principio base (ya declarado en §0.8 y §3.2):** un tick en `SIMULATION` nunca es indistinguible de un tick real. `is_synthetic` y `source_state` son obligatorios en todo tick — esto ya resuelve "que se pueda saber" que el dato es sintético. Lo que falta especificar es **qué se permite hacer** con ese conocimiento, y eso no es responsabilidad del tick — es responsabilidad de una señal separada.

**`order_policy` es una señal distinta de `NormalizedTick.source_state`, no un campo del tick.** Por principio §0.3, el tick nunca contiene lógica de negocio — y "qué órdenes se permiten" es una decisión de negocio, no un hecho de mercado. `order_policy` es publicado por el `ProviderRouter`, por símbolo, derivado de `source_state` + la configuración declarada en `InstrumentProfile`, y viaja en un canal propio (mismo bus, evento separado) para que exista incluso en ausencia total de ticks (p.ej. un `HALT_NEW_ORDERS` no depende de que siga llegando precio).

**Estados de `order_policy`:**

| Estado | Significado |
|---|---|
| `OPEN_NORMAL` | Precio confiable (`PRIMARY` o `SECONDARY`) — operación sin restricciones |
| `CLOSE_ONLY` | Solo se permite cerrar/reducir posiciones existentes; ninguna apertura nueva |
| `HALT_NEW_ORDERS` | Bloquea toda orden nueva (apertura); la política de cierres se decide por instrumento — no se asume idéntico a `CLOSE_ONLY` |
| `MARKET_CLOSED` | Fuera de horario según `trading_calendar_id` (§2.1.F) — **no es degradación**, es esperado; no debe confundirse con fallo de proveedor |

**Regla dura por defecto (no configurable a `OPEN_NORMAL` de forma implícita):** para instrumentos real-money, al entrar el símbolo en estado `SIMULATION`, `order_policy` por defecto es `CLOSE_ONLY` o `HALT_NEW_ORDERS` — **nunca `OPEN_NORMAL` con precio sintético.** Servir precio sintético para informar al usuario está bien; dejarlo abrir posición nueva contra ese precio no. Cambiar este default a algo menos restrictivo para un instrumento específico requiere una decisión explícita y documentada en su `InstrumentProfile`, nunca un flag global.

**Propagación obligatoria — defensa en profundidad, ningún punto único de control:**
- **UI**: bloquea visualmente el botón de apertura y muestra el estado (`CLOSE_ONLY`, etc.) — evita que el usuario intente una acción que de todos modos será rechazada.
- **Risk Engine**: no evalúa ni autoriza apertura de nuevas posiciones mientras `order_policy` no sea `OPEN_NORMAL`.
- **Execution Engine** (hoy: el punto de validación de orden en `consumers.py` + `risk_engine.validate_order_risk`) — **última línea de defensa.** Rechaza la orden en el punto de ejecución incluso si UI o Risk Engine fallaran en aplicarlo. Ninguno de los tres puede ser el único guardián.

---

## 4. Provider Capability Registry

Describe objetivamente qué puede hacer cada proveedor, como datos — para que `ProviderRouter` construya la cadena de failover cruzando esto contra `InstrumentProfile.required_capabilities`, en vez de tenerla cableada en código.

### 4.1 Dimensiones

| Dimensión | Ejemplo |
|---|---|
| `provider_id` | `"binance"`, `"kraken"`, `"finnhub"`, `"oanda"` (futuro) |
| `supported_asset_classes` | `["crypto"]`, `["forex", "metal"]` |
| `supported_symbols` | Lista explícita, o patrón si el proveedor cubre una clase entera |
| `quote_type` | `bid_ask` \| `trade_only` (algunos proveedores solo dan último trade, no ambos lados) |
| `ohlc_granularities` | `["1m", "5m", "1h", "1d"]` o `none` |
| `transport` | `websocket` \| `rest_poll` \| `both` |
| `market_depth` | `none` \| `L1` \| `L2` |
| `historical_depth` | Cuánto histórico permite consultar hacia atrás |
| `rate_limits` | Requests/seg, conexiones concurrentes, símbolos por conexión |
| `expected_latency_ms` | Banda esperada — desempata entre proveedores con igual capability |
| `requires_api_key` | bool |
| `cost_tier` | `free` \| `paid` \| `enterprise` |
| `geo_restrictions` | Notas (p.ej. Binance.com geo-bloqueado en ciertas regiones — hoy mitigado con Binance US) |
| `reliability_notes` | Conocimiento operativo acumulado en texto libre (p.ej. "Kraken WS v1 no señala cierre de vela explícitamente") |

### 4.2 Cómo se usa

Es dato estático, no lógica. Al inicializar el estado de un símbolo, `ProviderRouter` cruza `InstrumentProfile.required_capabilities` contra el Capability Registry y construye la cadena `Primary → Secondary` candidata, ordenada por (match de capacidades, `expected_latency_ms`, `cost_tier`). Esto reemplaza el cableado actual "Binance → Kraken → Finnhub" de `_try_live()` — cambiar el orden de failover pasa a ser un cambio de dato, no un deploy de código.

### 4.3 Ejemplo conceptual (estado actual + candidatos del roadmap MD-6)

| provider_id | asset_classes | quote_type | transport | requiere key | notas |
|---|---|---|---|---|---|
| binance | crypto | bid_ask + OHLC | WS + REST | No | Fallback US→com por geo-bloqueo |
| kraken | crypto | bid_ask + OHLC | WS + REST | No | Sin señal explícita de cierre de vela |
| finnhub | forex (hoy) | trade-only (WS) / bid_ask (REST quote) | WS + REST | Sí | Único feed real de forex hoy |
| oanda (candidato MD-6) | forex, metal | bid_ask | REST/stream | Sí | No integrado — nombrado en MD-1 §5 |
| twelve data / polygon (candidatos MD-6) | index, stock, energy | bid_ask/OHLC | REST/WS | Sí | No integrado |

---

## 5. Cómo interactúan los cuatro componentes

```
 InstrumentProfile.required_capabilities
              │
              ▼
 Provider Capability Registry ──▶ candidatos ordenados (Primary/Secondary)
              │
              ▼
 ProviderRouter (máquina de estados por símbolo)
      │                    │
      ▼                    ▼
 Provider Adapter    Provider Adapter   ... (uno por proveedor, dumb)
      │                    │
      └────────┬───────────┘
               ▼
        NormalizedTick
               │
               ▼
        Normalized Bus (Channels + Redis — sin cambios)
               │
     ┌─────────┼──────────────┐
     ▼         ▼              ▼
TradingConsumer  risk_engine  Celery tasks
(+spread markup) exposure_engine
```

---

## 6. Qué NO cambia de la arquitectura actual

- El **Normalized Bus** (Channels group + Redis cache) ya cumple el rol de bus — no hace falta reinventarlo.
- El patrón **un feed por símbolo, N consumidores** vía subscribe/unsubscribe sigue siendo válido.
- `spread_engine.py` aplicando el markup **encima** del tick normalizado es el patrón correcto — solo hay que garantizar que siga leyendo de un `NormalizedTick` real y nunca de campos crudos de proveedor.

---

## 7. Respuesta directa: ¿la diseñaría exactamente igual desde cero?

**Sí, en sus líneas generales.** La separación Adapter / Capability Registry / Router / InstrumentProfile / Bus es el patrón correcto, y es exactamente hacia donde ya apuntaba el roadmap MD-1→MD-7 aprobado. Este documento no lo contradice — lo especifica.

Tres cambios concretos que sí haría distinto desde el día uno, no como *afterthought*:

1. **`InstrumentProfile` con backing en DB desde el inicio para todo lo que no sea el set inicial de forex/crypto** — no esperar al bloque MD-5. Options y stocks son intrínsecamente datos de alto volumen (cadenas de opciones, miles de tickers); un archivo Python escrito a mano no escala ni es auditable a esa escala, y ya existe `Instrument` en DB (hoy desconectado) esperando exactamente este rol.

2. **Estado del `ProviderRouter` compartido, no en memoria de un solo proceso, desde el diseño inicial del componente** — no como corrección posterior. "Money Broker" implica eventualmente más de un worker ASGI; un router con estado local por proceso puede divergir entre workers y dar señales contradictorias sobre qué proveedor está activo para el mismo símbolo.

3. **`provider_symbol_map` genérico en vez de un campo nuevo por proveedor** (`finnhub_symbol`, `kraken_symbol`, `exchange_symbol`...). Cada proveedor nuevo hoy obliga a añadir un campo al dataclass y tocar cada call site que lo usa; con un mapa genérico, añadir un proveedor es solo agregar datos.

**Lo que no cambiaría:** el Normalized Bus (Channels+Redis) ni el patrón de feed compartido por símbolo — eso ya es sólido y no tiene relación con cuántos asset classes soporte el broker.

---

## 8. Versionado de contratos y compatibilidad hacia atrás

Ambos contratos (`NormalizedTick` §1, `InstrumentProfile` §2) llevan un número de versión explícito porque van a evolucionar durante los cinco años de horizonte de este documento — options y futures ya obligan a añadir campos que forex/crypto no necesitan (§2.1.G/H) — y un consumidor no puede asumir en silencio que la forma de hoy es la forma de siempre.

- **`NormalizedTick.schema_version`** (§1.1) — versión del contrato de tick. Cambia cuando cambia la forma o el significado de un campo, no cuando se añade un campo opcional nuevo que un consumidor viejo puede simplemente ignorar.
- **`InstrumentProfile.profile_version`** (§2.1.K) — versión del contrato de instrumento, independiente de `schema_version` del tick. `updated_at` acompaña como dato operativo (cuándo se tocó por última vez), no reemplaza a `profile_version` (qué tan compatible es la forma).

**Política de compatibilidad:**

1. **Aditivo no rompe versión mayor.** Añadir un campo opcional nuevo (p.ej. `open_interest` cuando se activó Futures) es un cambio menor — los consumidores existentes deben ignorar campos que no reconocen (compatibilidad hacia adelante), nunca fallar por su presencia.
2. **Cambiar el significado o el tipo de un campo existente, o quitar uno, es un cambio mayor.** Requiere: (a) incrementar la versión mayor, (b) un período de coexistencia donde el productor puede emitir ambas formas o el consumidor soporta ambas explícitamente, (c) que ningún consumidor mezcle silenciosamente campos de dos versiones mayores distintas.
3. **Un consumidor que recibe un `schema_version` mayor que no reconoce debe degradar de forma segura** (tratar el tick como no confiable / no consumirlo) **en vez de leer campos que pueden haber cambiado de significado.** Esto es una instancia directa del principio §0.8: ante duda sobre el contrato, restringir, no asumir.
4. **`profile_version` sigue la misma regla** para `InstrumentProfile`: un cambio mayor en la forma del perfil (p.ej. reestructurar `provider_symbol_map`) no debe desplegarse hasta que todo componente que lee `InstrumentProfile` (Router, Capability matching, motor de margen) soporte la versión nueva — no hay "cambiar el shape y esperar que nada se rompa en runtime" para un dato del que depende el cálculo de margen.

---

## 9. Relación con el roadmap existente (MD-1 §6)

Este documento es la base de diseño para los bloques ya esbozados en MD-1, sin comprometer fechas:

- **MD-2** (routing explícito por `asset_class`) — usa la tabla de aplicabilidad de §2.2.
- **MD-4** (Provider Router + circuit breaker por par proveedor/símbolo) — usa §3 completo.
- **MD-5** (fuente de verdad del instrumento) — usa la recomendación de §2.3.
- **MD-6** (proveedores reales para metals/oil/index) — usa el Capability Registry de §4.

No se activa ningún instrumento nuevo, no se toca ningún archivo de runtime, no hay cambio de comportamiento asociado a este documento.
