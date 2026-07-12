# Market Data Layer — Runtime Architecture Map (MD-1)

**Bloque:** MD-1 — Dead Code Verification + Architecture Truth
**Fecha:** 2026-07-11
**Alcance:** Solo documentación. No se eliminó, movió ni modificó ningún archivo de `market_data/` ni de ningún otro módulo del runtime.
**Guía técnica previa:** "MONEY BROKER — MARKET DATA LAYER: Arquitectura definitiva para Forex, Gold, Silver, Oil, Indices y Crypto" (aprobada).

Este documento confirma con evidencia de imports/uso reales (no solo lectura de código) qué partes de `market_data/` y módulos relacionados están vivas, muertas, duplicadas o experimentales.

---

## 1. Flujo real (arquitectura viva)

```
SymbolSpec (market_data/symbol_specs.py)
        │  get_spec(), allowed_symbols(), normalize_symbol()
        ▼
FeedManager (market_data/feeds.py)
        │  _feed_loop → _try_live (Binance → Kraken → Finnhub) → _sim_loop (bounded, con resync REST)
        ▼
Channels group "feed_{symbol}"  +  Redis cache "trx:price:{bid,ask}:{symbol}"
        │
        ├──▶ TradingConsumer (simulator/consumers.py)
        │        │  broker_price() ← spread_engine.py (BrokerSpreadConfig, markup en pips)
        │        │  evaluate_position_risk() / validate_order_risk() / check_and_enforce_risk()
        │        │        ← risk_engine.py
        │        ▼
        │    Dashboard (WebSocket, ws/trading/<account_id>/)
        │
        └──▶ Celery (simulator/tasks.py) — lee el mismo cache Redis vía _read_cached_price()
                 │  scan_positions_task → risk_engine.check_equity_stopout / evaluate_position_risk
                 ▼
             Stopouts, snapshots, exposure (exposure_engine.py)
```

`trx_simulator/asgi.py` es el único punto de entrada ASGI real: monta `simulator.routing.websocket_urlpatterns` (`TradingConsumer`) sobre Django Channels. No monta ni referencia `websocket_server.py` (FastAPI) en ningún punto.

---

## 2. Triple fuente de configuración por instrumento

| Fuente | Tipo | ¿La lee el runtime de precios/riesgo? | Uso real confirmado |
|---|---|---|---|
| `SymbolSpec` (`market_data/symbol_specs.py`) | dataclass estático en código | **Sí — es la fuente que todo el runtime importa** | `feeds.py`, `consumers.py`, `views.py`, `risk_engine.py`, `exposure_engine.py`, `spread_engine.py` (normalize), `tasks.py`, `population_engine.py`, `probe_market_symbol.py` |
| `Instrument` (`simulator/models.py`) | modelo DB | **No** — cero lecturas fuera de `admin.py`, `seed_instruments.py` y sus propios tests | `InstrumentAdmin` (UI de solo catálogo), `seed_instruments.py` (siembra unidireccional desde `symbol_specs.py`), `test_instrument_catalog.py`, `test_probe_market_symbol.py` (solo verifica que el probe NO lo toca) |
| `BrokerSpreadConfig` (`simulator/models.py`) | modelo DB | **Sí, parcialmente** — solo aporta el markup en pips sobre el spread crudo del feed | `spread_engine.py::broker_price()`, cacheado 30s en memoria de proceso |

**Confirmado:** no existe ningún signal, servicio ni tarea que lea `Instrument.objects` para pricing, routing de proveedor, margen o riesgo. Es un catálogo aislado, exactamente como su propio docstring en `seed_instruments.py` declara: *"This does NOT change symbol_specs.py or wire Instrument into trading; it only populates the catalog table for future admin management."*

### Riesgo de configuración divergente

Editar `Instrument` vía Django admin (spread, leverage, provider, `trading_enabled`) **no tiene ningún efecto en el runtime de trading**. Un operador que asuma lo contrario puede creer que ajustó el spread o activó un instrumento cuando en realidad no cambió nada — con dinero real, esto es un vector directo de error operativo silencioso.

---

## 3. Componentes — tabla completa de evidencia

| Componente | Quién lo importa (evidencia) | Runtime real | Solo tests/manual | Huérfano | Duplica lógica de `feeds.py` | Riesgo de tocar | Acción |
|---|---|---|---|---|---|---|---|
| `market_data/symbol_specs.py` | `feeds.py`, `consumers.py`, `views.py`, `risk_engine.py`, `exposure_engine.py`, `spread_engine.py`, `tasks.py`, `population_engine.py`, `models.py` (`BrokerSpreadConfig.save`), `probe_market_symbol.py`, 6+ archivos de test | Sí — fuente de verdad activa | No | No | No | Alto (todo depende de esto) | **KEEP** |
| `market_data/feeds.py` (`FeedManager`) | `consumers.py`, `exposure_engine.py`, `population_engine.py` | Sí — motor de precios en vivo | No | No | — (es el original) | Alto | **KEEP** (candidato a refactor futuro fuera de este bloque) |
| `market_data/provider_registry.py` | *(ningún import en todo el repo — `rg` confirma cero resultados)* | No | No | **Sí** | No aplica | Ninguno — no lo usa nada | **DELETE_LATER** |
| `market_data/adapters/binance.py` | Solo `interfaces.py` (self-import) | No — nunca instanciado | No | **Sí** | Sí — stub que reimplementa la idea de `_binance_loop` sin lógica | Ninguno | **DELETE_LATER** (o semilla real de la Provider Adapter layer propuesta, fuera de este bloque) |
| `market_data/adapters/finnhub.py` | Solo `interfaces.py` (self-import) | No — nunca instanciado | No | **Sí** | Sí — stub análogo a `_finnhub_loop` | Ninguno | **DELETE_LATER** |
| `market_data/interfaces.py` (`IMarketDataProvider`) | Solo los adapters muertos de arriba | No | No | **Sí** (soporte de código muerto) | No | Ninguno | **DELETE_LATER** (o reusar como base real de la Provider Adapter layer) |
| `market_data/dto.py` (`CandleDTO`) | Solo `interfaces.py`, `adapters/*` | No | No | **Sí** | No | Ninguno | **DELETE_LATER** |
| `market_data/normalizer.py` | *(cero imports — `rg` confirma cero resultados)* | No | No | **Sí** | No | Ninguno | **DELETE_LATER** |
| `market_data/config.py` | *(cero imports — `rg` confirma cero resultados)* | No | No | **Sí** — placeholders de API key vacíos, superados por `settings.py`/env vars reales | No | Bajo, pero riesgo de confusión sobre dónde van las keys | **DELETE_LATER** |
| `market_data/hub.py` (`publish`) | Solo `websocket_server.py` (import perezoso, `try/except`) | No — nada en el stack Django/Channels real lo llama | No | **Sí** (su único consumidor está huérfano) | No | Ninguno | **DELETE_LATER** |
| `websocket_server.py` (FastAPI) | `run_all.sh` (único punto que lo invoca) | **No** — no está montado en `trx_simulator/asgi.py`; el puerto 8001 real de producción lo ocupa **Daphne** (confirmado en `DEPLOY.md`, `docs/INFRA_PLAN_L1.md`, `docs/STAGING_READINESS_K4.md`) | No | **Sí** | Sí — reimplementa un sim loop de velas paralelo al de `feeds.py` | Ninguno para el runtime actual | **MOVE_TO_EXPERIMENTAL** (o `DELETE_LATER` si se confirma que no hay intención de retomarlo) |
| `run_all.sh` | Nadie — script de conveniencia local | No | Manual (dev local) | **Sí** — apunta a `$HOME/Desktop/trx_simulator`, directorio que **ya no existe** (el proyecto se renombró a `trx_sim`); es del *commit inicial* (`69e8b4f`, 2026-01-04), nunca actualizado | — | Ninguno | **DELETE_LATER** (o actualizar el path si se decide resucitar el flujo FastAPI) |
| `manual_finnhub_ws_check.py` | Nadie — ya renombrado en `4f0a8cd` ("chore: exclude manual finnhub websocket check from test discovery") específicamente para sacarlo del test discovery de Django (antes `test_finnhub_ws.py`) | No | Sí — diagnóstico manual deliberado | No (uso intencional, ya aislado) | No | Ninguno | **MOVE_TO_SCRIPTS_MANUAL** (ya cumple su función; solo falta ubicarlo fuera de la raíz) |
| `test_ws_finnhub.js` | Nadie — cero referencias en todo el repo; `package.json` no define ningún script que lo ejecute | No | No — ni siquiera es un test automatizado real | **Sí** | No | **Ver nota de seguridad abajo** | **DELETE_LATER** — requiere acción de seguridad primero (ver §4) |
| `trx_simulator/asgi.py` | Punto de entrada ASGI real (Daphne) | Sí | No | No | No | Alto (es el entrypoint) | **KEEP** |
| `simulator/consumers.py` (`TradingConsumer`) | `simulator/routing.py` → `asgi.py` | Sí | No | No | No | Alto | **KEEP** |
| `simulator/tasks.py` | Celery (`@shared_task`), `scan_positions_task` activo | Sí | No | No | No | Alto | **KEEP** |
| `simulator/risk_engine.py` | `consumers.py`, `tasks.py` | Sí | No | No | No | Alto | **KEEP** |
| `simulator/exposure_engine.py` | Importa `feeds.py`/`symbol_specs.py` en runtime | Sí | No | No | No | Medio | **KEEP** |
| `simulator/spread_engine.py` | `consumers.py::broker_price` | Sí | No | No | No | Alto | **KEEP** |
| `simulator/population_engine.py` | Importa `market_data.feeds.FeedManager` | Sí (uso puntual) | No | No | No | Medio | **KEEP** — **NEEDS_MORE_EVIDENCE** sobre alcance exacto de su dependencia de `FeedManager` (no se auditó línea por línea en este bloque) |
| `simulator/views.py` | Importa `symbol_specs` para el dashboard/allowed symbols | Sí | No | No | No | Medio | **KEEP** |
| `Instrument` (modelo) | `admin.py`, `seed_instruments.py`, tests propios | **No** (ver §2) | Parcial — vive en tests y en el admin, no en runtime de trading | No (foundation deliberada, no código muerto) | No aplica | Bajo mientras siga desconectado | **KEEP** — pendiente de decisión arquitectónica (conectar o documentar como WIP explícito), fuera de alcance de MD-1 |
| `probe_market_symbol.py` | Management command, usa `symbol_specs.get_spec` | Sí — diagnóstico read-only sancionado | Es en sí mismo una herramienta de diagnóstico | No | No | Ninguno — confirmado sin escritura a DB (`test_command_never_writes_to_db`) | **KEEP** |

---

## 4. ⚠ Hallazgo de seguridad — remediado parcialmente en MD-1b

`test_ws_finnhub.js` contenía un **API token de Finnhub hardcodeado en texto plano**, presente desde el *commit inicial* del repositorio (`69e8b4f`, 2026-01-04). Este documento nunca reprodujo el valor (regla explícita del bloque: no copiar secretos).

**Estado tras MD-1b (Secret & Security Cleanup):**

- El archivo se movió a `scripts/manual/test_ws_finnhub.js` y se reescribió para leer `FINNHUB_API_KEY` desde el entorno (`process.env.FINNHUB_API_KEY`), fallando de forma explícita si no está definida. El literal ya no existe en el working tree.
- El literal **sigue presente en el historial de git** (commit `69e8b4f` en adelante) — mover/reescribir el archivo actual no purga el historial. Ver plan de remediación Nivel B en el reporte de MD-1b.
- Acción pendiente del usuario, fuera del alcance de este repo: **rotar/revocar la key en el dashboard de Finnhub**. Sin eso, el token histórico sigue siendo válido independientemente de qué se haga con el código.

---

## 5. Arquitectura objetivo (referencia — sin implementar en este bloque)

Definida en el documento de arquitectura aprobado:

- **Provider Adapter** — un adapter dumb por vendor (habla el protocolo, no conoce instrumentos).
- **Symbol Mapping** — ya existe y funciona (`exchange_symbol`/`kraken_symbol`/`finnhub_symbol` en `SymbolSpec`).
- **Provider Router** — nuevo componente: decide qué adapter está activo por instrumento, con cadena de failover declarada como dato.
- **Normalized Bus** — ya existe y no requiere cambios (grupo Channels + Redis cache).
- **Circuit Breaker** — estado explícito por par (proveedor, símbolo): `CLOSED` → `OPEN` → `HALF_OPEN`, reemplazando el contador global `MAX_FAILURES=3` actual.
- **Market Data Quality Monitor** — SLA de staleness y validación cruzada de dos fuentes para instrumentos de alto valor, antes de permitir apertura de posición si la divergencia supera un umbral.

---

## 6. Roadmap MD-1 → MD-7

| Bloque | Objetivo | Cambia comportamiento del runtime |
|---|---|---|
| **MD-1** (este bloque) | Verificación de código muerto + documentación de arquitectura viva | No |
| **MD-2** | Routing explícito por `asset_class` en vez de `"/" in symbol` | Sí — requiere aprobación y tests dedicados |
| **MD-3** | Extraer esqueleto WS/REST compartido (`_binance_loop`/`_kraken_loop`/`_finnhub_loop` → helper común) | No debería (refactor interno con mismo comportamiento externo), pero requiere suite de regresión |
| **MD-4** | Provider Router con circuit breaker explícito por (proveedor, símbolo) | Sí |
| **MD-5** | Fuente de verdad única del instrumento — decisión DB-first vs code-first, conectar `Instrument` o deprecar `SymbolSpec` | Sí — bloque mayor, requiere plan de migración propio |
| **MD-6** | Integración de proveedor real para Forex/Metals/Oil/Indices (OANDA / Twelve Data / Polygon, según el documento de arquitectura) | Sí — solo entonces se evalúa activar `XAU/USD`, `XAG/USD`, oil, índices |
| **MD-7** | Conciencia de sesión de mercado + SLA de calidad de dato (Market Data Quality Monitor) | Sí |

**Regla explícita para todos los bloques futuros:** no activar `XAU/USD`, `XAG/USD`, Oil ni ningún índice hasta validar un proveedor real end-to-end (probe exitoso + coherencia de datos), independientemente de en qué bloque del roadmap se esté.

---

## 7. Confirmaciones de este bloque (MD-1)

- No se eliminó, movió ni renombró ningún archivo.
- No se cambió comportamiento del runtime (ningún archivo de `market_data/`, `simulator/` fuera de este documento fue modificado).
- No se activaron instrumentos ni se cambiaron proveedores.
- No se tocó `.env` ni ningún valor de configuración real.
- No se instalaron paquetes ni se corrieron migraciones.
- `/Users/naffermoreno/Desktop/treasury_engine` no fue referenciado ni tocado.

---

## 8. Provider Router Shadow Mode (FOUNDATION-08)

Corre la cadena nueva — `SymbolSpec → InstrumentProfile → ProviderRoutePlan → ProviderRouter.decide()` (`market_data/shadow/`) — en paralelo al runtime real, solo para observar. **Es puramente observacional: no controla suscripciones, precios, failover ni operaciones.** `FeedManager._try_live()` sigue siendo la única autoridad; el shadow mode nunca decide nada por sí mismo.

**Cómo activarlo:** variable de entorno `MARKET_DATA_SHADOW_MODE=True` (default `False` en local/staging/producción — no se debe activar sin aprobación explícita posterior a este bloque). Con la flag en `False`, `market_data/shadow/` ni siquiera se importa desde `feeds.py`.

**Punto de integración:** `FeedManager._ensure_running()`, justo antes de crear la tarea real del feed — se dispara una sola vez por arranque en frío de un símbolo (cuando no hay tarea corriendo o terminó), nunca por tick. Envuelto en `try/except` total: cualquier fallo del shadow mode se traga y se loguea en `DEBUG`; la tarea real del feed se crea siempre, sin condicionarse al resultado.

**Cómo interpretar agreement/disagreement:** cada evaluación compara `legacy_expected_provider` (una réplica declarativa del orden real de `_try_live()`: Binance si hay `exchange_symbol` → Kraken si hay `kraken_symbol` → Finnhub si hay `FINNHUB_API_KEY` y el símbolo tiene `"/"` → ninguno) contra `shadow_selected_provider` (lo que decide `ProviderRouter.decide()` sobre el `ProviderRoutePlan` construido desde el mismo `SymbolSpec`). `agrees_with_legacy=True` cuando coinciden. Una discrepancia **no implica un bug** — por ejemplo, un símbolo con `enabled=False` pero `finnhub_symbol` configurado (USD/CAD, USD/CHF, NZD/USD) mostrará `legacy_expected_provider="finnhub"` pero `shadow_selected_provider=None` (simulation-only), porque `legacy_expected_provider()` replica solo las tres condiciones declarativas pedidas, sin considerar el gate de `enabled`/`allowed_symbols()` que en la práctica nunca deja que `_try_live()` se ejecute para un símbolo deshabilitado. Es señal real y esperada, no ruido.

**Limitación actual, documentada deliberadamente:** el `ProviderRouter` que usa el shadow mode se instancia nuevo en cada evaluación — no recibe los fallos reales de `FeedManager` (los reintentos WS, los `consecutive_failures` de `_binance_loop`/`_kraken_loop`/`_finnhub_loop`). Por lo tanto, toda decisión de shadow parte de un circuit breaker sano/`CLOSED`. Este bloque prueba que la tubería completa encadena correctamente — no reproduce el comportamiento real del circuit breaker bajo fallos en vivo. Sincronizar el estado del breaker de shadow con los fallos reales del `FeedManager` queda fuera de alcance de FOUNDATION-08.

---

## 9. Controlled Provider Router Integration (FOUNDATION-09)

Primera vez que el `ProviderRouter` nuevo **controla de verdad** una decisión de proveedor en el runtime — pero solo la selección **inicial**, y solo para símbolos explícitamente autorizados. Todo lo demás sigue exactamente igual que antes de este bloque.

**Flags:** `MARKET_DATA_ROUTER_ENABLED` (bool, default `False`) + `MARKET_DATA_ROUTER_SYMBOLS` (lista separada por comas, default vacía). Ambas condiciones deben cumplirse para que un símbolo use el router nuevo:
- `ENABLED=False` → 100% legacy, siempre, para todos los símbolos.
- `ENABLED=True` pero símbolo fuera de la lista → legacy, sin excepción.
- `ENABLED=True` + símbolo en la lista → el router decide la selección inicial.

**Primer canary autorizado: `BTCUSD` solamente.** Cualquier ampliación de la allowlist es una decisión posterior explícita, no algo que este bloque habilite por sí mismo — el default en `.env.example`/`deploy/.env.staging.template` queda vacío, no con `BTCUSD` precargado.

**Punto de integración:** `FeedManager._try_live()` — se dividió en tres métodos, ninguno de los tres reescribe `_binance_loop`/`_kraken_loop`/`_finnhub_loop`:
- `_try_live()`: despachador delgado — decide si usa el router nuevo (`_should_use_new_router`) o corre legacy directo.
- `_try_live_via_new_router()`: construye la decisión vía `market_data.runtime_router.select_runtime_provider()` y despacha explícitamente al loop existente correspondiente (`binance→_binance_loop`, `kraken→_kraken_loop`, `finnhub→_finnhub_loop`). Un `provider_id` no reconocido, o cualquier excepción en el camino (incluida una falla real de conexión del loop despachado), se propaga hacia arriba deliberadamente.
- `_try_live_legacy()`: copia exacta del `_try_live` original — Binance → Kraken → Finnhub hardcodeado. Es lo que corre siempre que el flag está apagado, el símbolo no está en la lista, o el camino nuevo falla por cualquier motivo.

**Fallback a legacy — regla dura, sin excepciones:** cualquier error en el camino nuevo (fallo al construir el plan, proveedor no reconocido, o el loop despachado lanzando una excepción real de conexión) se loguea (`event=market_data_router_selection_error`) y ejecuta `_try_live_legacy()` completo, desde cero, para ese símbolo. Una decisión válida de "sin proveedor en vivo" (`selected_provider_id=None`, típicamente `SIMULATION_FALLBACK`) **no** es un error — deja que el fallback sintético existente de `_feed_loop`/`_sim_loop` actúe exactamente igual que hoy, sin ejecutar legacy de nuevo.

**Estado del router — misma limitación que shadow mode (F08), resuelta en F10:** en este bloque (F09), la selección inicial usaba un `ProviderRouter` sano/nuevo en cada llamada — F09 controlaba solo la selección inicial. **FOUNDATION-10 (§10 abajo) conecta el éxito/fallo real de los loops al circuit breaker**, reemplazando este límite.

**Cómo revertir:** `MARKET_DATA_ROUTER_ENABLED=False` (o vaciar `MARKET_DATA_ROUTER_SYMBOLS`) — vuelve al 100% legacy sin tocar código, sin reiniciar en frío ninguna otra parte del sistema.

---

## 10. Real Failure Feedback & Canary Failover (FOUNDATION-10)

Cierra el límite documentado en F09: ahora el `ProviderRouter` que controla la selección para símbolos de la allowlist **recibe resultados reales** de los loops (`_binance_loop`/`_kraken_loop`/`_finnhub_loop`), no solo un circuit breaker sano/nuevo en cada llamada.

**Diseño de estado:** `market_data/runtime_router/state.py` — un `ProviderRouter` **singleton por proceso** (`get_router()`, patrón idéntico a `get_feed_manager()`), que sobrevive entre decisiones. Expone `record_provider_success(symbol, provider_id, now)`, `record_provider_failure(symbol, provider_id, error_code, now)`, `evaluate_recovery(symbol, provider_id, now)` y `get_circuit_breaker_state(symbol, provider_id)` — ninguno lanza excepción (mismo patrón defensivo que el resto del paquete). `select_runtime_provider()` (F09) ahora consulta este mismo singleton en vez de crear un `ProviderRouter()` nuevo por llamada.

**Punto exacto de feedback — dos hooks opcionales, cero reescritura de loops:** `_binance_loop`/`_kraken_loop`/`_finnhub_loop` ganaron dos parámetros keyword-only, `on_first_tick` y `on_terminal_failure`, con default `None` en **todo** call site existente (legacy incluido) — comportamiento idéntico si no se pasan. `_try_live_via_new_router()` es el único lugar que los construye y los pasa al despachar.

**Política success/failure/cancel:**
- **SUCCESS** se registra exactamente una vez por sesión de conexión, en el primer tick válido transmitido (`_broadcast()` ya ejecutado) — nunca por abrir el socket, nunca por cada tick subsecuente (un flag local `tick_reported` lo garantiza).
- **FAILURE** se registra exactamente una vez, solo cuando el loop agota sus propios reintentos internos (`MAX_FAILURES=3`, lógica de backoff sin tocar) y está a punto de relanzar la excepción hacia arriba — no por cada intento de reconexión interno, no por cada mensaje malformado (que ya caían dentro del mismo contador interno existente, sin cambios).
- **CancelledError nunca cuenta como fallo** — se captura y relanza en su propio `except` antes de llegar al bloque que invoca `on_terminal_failure`, exactamente igual que hacía el código legacy.
- Ambos callbacks están envueltos en su propio `try/except` dentro del loop — un bug en el feedback jamás puede tumbar el feed real.

**Orquestación de failover:** no hizo falta código nuevo de reintento. `_feed_loop`'s ciclo externo ya vuelve a invocar `_try_live()` tras cada intento fallido; como el estado del router ahora persiste, cada nueva llamada a `select_runtime_provider()` ve el circuit breaker real y decide en consecuencia — Binance abierto → Kraken elegido, sin duplicar ningún loop ni backoff.

**Recovery:** cuando el cooldown vence, `decide()` (ya existente desde F05) transiciona OPEN→HALF_OPEN internamente y selecciona el proveedor recuperándose como *probe* (`reason_code=HALF_OPEN_PROBE`). El test de integración confirma explícitamente que **una sola sesión exitosa no cierra el breaker** cuando la política exige más de una (`half_open_successes_required=2` para crypto) — no se falsificó con ticks ilimitados de una sola sesión.

**Logging:** `event=market_data_router_state_transition` (cambio real de `CircuitBreakerState.health`, con `from_state`/`to_state`/`consecutive_failures`/`error_code`) y `event=market_data_router_failover` (cuando el proveedor *seleccionado* cambia entre dos decisiones consecutivas — no se emite en la primera selección de un símbolo). Ninguno incluye secretos ni payloads completos.

**Limitación multi-worker — documentada, no resuelta aquí:** el estado vive en la memoria de **un proceso Python**. Un deploy ASGI multi-worker (varios procesos Daphne) tendría un circuit breaker independiente por worker, no uno compartido — la siguiente fase es estado compartido (Redis o un servicio de market-data dedicado), no activación global inmediata. Ver FOUNDATION-02 §3.4.

**Canary:** `MARKET_DATA_ROUTER_SYMBOLS` sigue conteniendo solo `BTCUSD` como ejemplo documentado — ninguna ampliación de la allowlist ocurre en este bloque. **Rollback instantáneo:** `MARKET_DATA_ROUTER_ENABLED=False`.

---

## 11. Market Sessions & Market Status (FOUNDATION-11)

Resuelve una confusión estructural que F10 no distinguía: **proveedor caído** vs **mercado cerrado**. Antes de este bloque, si Binance rechazaba una suscripción de un instrumento de índice fuera de horario, el circuit breaker lo habría contado como un fallo real. Ahora, para símbolos de la allowlist, `FeedManager` primero pregunta "¿está abierto el mercado?" — y si no, **ni siquiera intenta** un proveedor.

**Provider failure vs market closed — la distinción exacta:** un *provider failure* es "el proveedor no entregó datos cuando debería haberlo hecho" (cuenta para el circuit breaker). Un *market closed* es "nadie está entregando datos porque el mercado no está operando" (nunca debe contar). `market_data/sessions/` responde la segunda pregunta de forma completamente independiente del estado de `ProviderRouter` — no comparten ningún dato.

**Calendarios iniciales (`market_data/sessions/calendars.py`), todos aproximaciones documentadas, sin proveedor de holidays externo:**
- `CRYPTO_24_7` — siempre `OPEN`.
- `FOREX_24_5` — `OPEN` domingo 22:00 UTC → viernes 22:00 UTC; `WEEKEND` el resto. Ventana fija, sin ajuste por DST de Nueva York (documentado como aproximación).
- `METALS_23_5` — misma ventana semanal que forex, más un hueco diario de `MAINTENANCE` 21:00–22:00 UTC (aprox. del cierre de settlement diario real).
- `US_INDICES_CFD` — horario real en `America/New_York` (`zoneinfo`, consciente de DST): `PRE_MARKET` 04:00–09:30, `OPEN` 09:30–16:00, `AFTER_HOURS` 16:00–20:00, `CLOSED` el resto, `WEEKEND` sáb/dom. `HOLIDAY` existe como valor de estado pero **este calendario nunca lo produce** — sin vendor de feriados, un feriado real de NYSE se vería incorrectamente como `OPEN`/`CLOSED` según el reloj.
- `ALWAYS_CLOSED` — siempre `CLOSED` (utilidad, no asignado a ningún asset_class hoy).
- `UNKNOWN` — siempre `UNKNOWN` + `HALT_NEW_ORDERS`.

**Mapeo `asset_class → calendar_id`** (`market_data/sessions/models.py::DEFAULT_CALENDAR_BY_ASSET_CLASS`, única tabla declarativa — cero hardcodes por símbolo): `crypto→CRYPTO_24_7`, `forex→FOREX_24_5`, `metal→METALS_23_5`, `index→US_INDICES_CFD`, `energy→UNKNOWN` (sin instrumento de energía registrado todavía). **Nota de diseño deliberada:** este mapeo se deriva de `InstrumentProfile.asset_class`, **no** del campo `trading_calendar_id` que ya existía desde F06 (ese campo hoy solo contiene el string libre `"24/7"`/`"24/5"`, metadata descriptiva de F06, no un identificador de calendario real). Reconciliar ambos campos queda fuera de alcance — no se tocó el bridge de F06 para no romper su contrato ya probado.

**order_policy por estado:** `OPEN→OPEN_NORMAL`. `PRE_MARKET`/`AFTER_HOURS→CLOSE_ONLY` (horario extendido real existe pero sin datos reales de esa franja todavía — se permite gestionar posiciones existentes, no abrir nuevas). `WEEKEND`/`HOLIDAY`/`MAINTENANCE`/`CLOSED→MARKET_CLOSED` (mercado genuinamente no operando, esperado — no una degradación). `UNKNOWN→HALT_NEW_ORDERS` (no estamos seguros de que el mercado esté cerrado, así que restringimos por incertidumbre, no por afirmación).

**Evaluador puro:** `evaluate_market_session(profile, *, now)` — `now` es un `datetime` timezone-aware **obligatorio**, sin default a reloj real (igual que `ProviderRouter.decide()`). Nunca lanza — cualquier fallo interno (calendario no reconocido, `now` naive) degrada a `UNKNOWN + HALT_NEW_ORDERS + EVALUATION_ERROR`. `evaluate_market_session_for_symbol(symbol, *, now=None)` es el wrapper de borde (symbol→spec→profile→evaluate) con reloj real por defecto, mismo patrón que `evaluate_shadow_route`/`select_runtime_provider`.

**Integración con FeedManager — punto exacto:** dentro de `_try_live_via_new_router()`, **antes** de llamar a `select_runtime_provider()`. Si `state != OPEN`, retorna `False` de inmediato — exactamente el mismo resultado que "sin proveedor disponible" — sin construir ningún `ProviderRoutePlan`, sin llamar `decide()`, sin tocar ningún circuit breaker. El fallback sintético existente de `_feed_loop`/`_sim_loop` sigue dando continuidad sin cambios. Para `BTCUSD` (el único canary activo hoy), el calendario es `CRYPTO_24_7` — siempre `OPEN` — así que esta verificación es un no-op transparente y no cambia ningún comportamiento observado en F09/F10.

**Logging:** `event=market_data_market_session` con `symbol`/`calendar_id`/`state`/`order_policy`/`reason_code`/`next_open_at`/`next_close_at`, emitido en cada llamada a `_try_live_via_new_router()` para un símbolo de la allowlist — antes de cualquier log de selección de proveedor. Sin secretos.

**Alcance allowlist:** ningún símbolo fuera de `MARKET_DATA_ROUTER_SYMBOLS`, y nada si `MARKET_DATA_ROUTER_ENABLED=False`, evalúa sesión — verificado explícitamente por test (`evaluate_market_session_for_symbol` ni siquiera se invoca). **Rollback:** `MARKET_DATA_ROUTER_ENABLED=False`, sin cambios.

**Limitaciones:**
- Sin proveedor de feriados externo — `HOLIDAY` es un valor de estado alcanzable por diseño, pero ningún calendario de este bloque lo produce todavía.
- `FOREX_24_5`/`METALS_23_5` usan una ventana UTC fija, sin el ajuste real que el mercado forex tiene por DST de Nueva York/Londres (aproximación documentada, no exacta).
- El mapeo `asset_class→calendar_id` y el campo `trading_calendar_id` de F06 conviven sin reconciliarse — ver nota de diseño arriba.
- `energy` no tiene calendario real — cualquier instrumento futuro de esa clase queda en `UNKNOWN` (`HALT_NEW_ORDERS`) hasta que se modele explícitamente.

---

## 12. Runtime Instrument Catalog (FOUNDATION-12)

No conecta nada al runtime — es la capa de indirección que hace posible una futura migración gradual sin tocar `FeedManager` cuando llegue el momento. `SymbolSpec` sigue siendo la única autoridad; `feeds.py` no se tocó en este bloque (primera vez desde F08 que ese archivo queda completamente intacto).

**El problema que resuelve:** hoy, todo el runtime (`feeds.py` y lo que dependa de él) importa `market_data.symbol_specs.get_spec()` directamente. Si algún día `SymbolSpec` deja de ser la fuente (decisión MD-5 pendiente desde F06), habría que tocar cada call site. `get_runtime_instrument(symbol)` es el único punto que un futuro call site debería usar en su lugar — cambiar de dónde saca los datos internamente, en una Foundation futura, no requeriría tocar ningún llamador.

**Diseño elegido:** dos paquetes con responsabilidades separadas, seguidas del mismo patrón de aislamiento establecido en F03–F11:
- `market_data/catalog/` (`service.py` + `__init__.py`) — puro, sin Django, sin DB, sin `simulator` (verificado por test de aislamiento estático, igual que shadow/runtime_router/sessions). `get_runtime_instrument(symbol) -> InstrumentProfile` siempre construye desde `SymbolSpec` vía el bridge ya existente de F06 (`profile_from_symbol_spec`) — cero lógica nueva de conversión. `compare_runtime_instrument(symbol, alternate_profile)` reutiliza `compare_profiles`/`DriftReport` de F06 tal cual, sin reinventar clasificación de drift.
- `simulator/runtime_instrument_catalog.py` — el único lugar autorizado a unir "el nuevo facade" con "la DB", porque `market_data/catalog/` no puede importar `simulator.models.Instrument` sin romper la dirección de dependencia establecida en todo el proyecto (`simulator` → `market_data`, nunca al revés).

**Decisión deliberada — `get_runtime_instrument()` SÍ puede lanzar:** a diferencia de `evaluate_shadow_route`/`select_runtime_provider`/`evaluate_market_session_for_symbol` (que nunca lanzan, por ser límites de un feed en vivo), `get_runtime_instrument()` propaga `KeyError` para un símbolo desconocido — exactamente lo que `get_spec()` ya hace hoy. Tragar ese error habría sido un cambio de comportamiento real frente a los call sites actuales, justo lo que este bloque promete no hacer.

**Comparación y detección de drift:** `simulator/runtime_instrument_catalog.py::check_runtime_catalog_drift(symbol)` — busca la fila `Instrument` correspondiente (normalizando símbolo compacto↔canónico), construye su perfil vía `profile_from_instrument`/`provider_mappings_for_instrument` (F06), y compara contra `get_runtime_instrument(symbol)`. Probado contra los datos reales sembrados: `EUR/USD` sin drift, `BTCUSD` con el mismo warning de `display_name` ya conocido desde F06b (`BTCUSD` vs `BTC/USD`) — confirma consistencia total con hallazgos previos usando el nuevo facade.

**Feature flag:** `MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED` (default `False`). A diferencia de `MARKET_DATA_SHADOW_MODE`/`MARKET_DATA_ROUTER_ENABLED` (que su propio bloque conectó a `feeds.py` de inmediato), este flag **no tiene consumidor en el runtime todavía** — por instrucción explícita de este bloque de no tocar `FeedManager`. Existe, está probado (`assertNumQueries(0)` cuando está apagado — cero acceso a DB), y queda listo para que una Foundation futura lo conecte en caliente sin inventar plumbing nuevo.

**Logging:** `event=market_data_runtime_catalog_drift` con `symbol`/`critical`/`warning`/`fields` — `WARNING` si hay drift crítico, `INFO` si solo hay warnings, silencio total si no hay drift. Sin secretos.

**Confirmación — cero cambio de comportamiento:** `test_returns_the_same_profile_the_existing_bridge_would` prueba que `get_runtime_instrument()` devuelve exactamente lo que `profile_from_symbol_spec(get_spec(...))` ya devolvía — no es una reimplementación, es un alias con intención arquitectónica. `git status --porcelain` confirma `feeds.py`, `consumers.py`, `risk_engine.py`, `spread_engine.py`, `exposure_engine.py`, `tasks.py`, `dashboard.html`, `symbol_specs.py` sin tocar; sin migraciones; sin símbolos nuevos activados.

**Próximo paso (no en este bloque):** cuando una Foundation futura decida que `FeedManager` debe llamar `get_runtime_instrument(symbol)` en vez de `get_spec(symbol)` directamente, ese cambio queda aislado a los call sites de `feeds.py` — el facade y la infraestructura de comparación ya existen y están probados.

## 13. Market Data Observability (FOUNDATION-13)

Capa de solo-lectura sobre el Market Data Engine ya construido (F03–F12): qué proveedor está activo por símbolo, estado del circuit breaker, failovers, sesión de mercado, frescura del último tick, modo live/simulación, drift de catálogo y errores recientes. No es otra reescritura arquitectónica — no selecciona proveedor, no evalúa riesgo, no cambia precio/spread/margen/PnL, y no persiste ningún payload de tick.

**Qué observa F13:**
- **Proveedor activo y su estado** (`active_provider_id`, `source_state`, `order_policy`, `degraded`) — leído de la misma decisión (`RouteDecision`) que F09/F10 ya calculan; F13 solo expone dos campos (`order_policy`, `degraded`) que `RuntimeSelectionResult` ya tenía disponibles internamente pero no exponía (cambio aditivo de 2 campos con default, cero impacto en consumidores existentes).
- **Circuit breaker por proveedor** — lectura directa del singleton `market_data.runtime_router.state` (F10), vía el mismo accessor de solo-lectura (`get_breaker_state`) que F10 ya usa para loguear un "antes" — nunca llama `provider_success`/`provider_failure`/`evaluate_recovery`.
- **Sesión de mercado** — recalculada en vivo con `evaluate_market_session_for_symbol()` (F11) en cada snapshot, nunca leída de un store — es la única forma de obtener una respuesta correcta sin importar el proceso que pregunte (ver limitación del comando más abajo).
- **Frescura del tick** (`last_tick_at`, `tick_age_seconds`, `stale`) — timestamp actualizado en `FeedManager._broadcast()` (el único punto compartido por Binance/Kraken/Finnhub/simulación), sin guardar bid/ask/mid.
- **Failovers** (`failover_count`, `last_failover_at`) — contador propio de F13, detecta cambio de proveedor comparando contra el último valor que el propio store ya necesitaba guardar (`active_provider_id`) — no duplica la detección de failover que F10 ya hace para su propio logging (`event=market_data_router_failover`); son dos consumidores independientes de la misma clase de evento, cada uno con su propio estado mínimo.
- **Drift de catálogo** (`catalog_drift_level`) — reutiliza `check_runtime_catalog_drift()` de F12 tal cual, sin reimplementar comparación; gateado por el flag existente `MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED`.

**Qué NO controla F13:** ningún campo de `MarketDataHealthSnapshot` retroalimenta la selección de proveedor, la política de órdenes, el precio, el spread o el flujo de control del feed loop. Es puramente un espejo de lectura; apagar `MARKET_DATA_OBSERVABILITY_ENABLED` no cambia el comportamiento del feed en absoluto, solo deja de alimentarse el store.

**Política de staleness — reutilizada, no inventada:** `_PRICE_CACHE_TTL` (default 60 s, env `PRICE_CACHE_TTL`), ya existente en `market_data/feeds.py` como TTL de las claves Redis que el daemon de Celery (`simulator/tasks.py::_read_cached_price`) ya usa para decidir si un precio cacheado es utilizable. F13 no crea una segunda política: `tick_age_seconds = evaluated_at - last_tick_at`, `stale = tick_age_seconds > _PRICE_CACHE_TTL`, con el mismo umbral que el resto del sistema.

**Diseño elegido:** tres piezas, siguiendo el patrón de aislamiento establecido en F03–F12:
- `market_data/observability/` (`models.py` + `store.py` + `service.py`) — puro, sin Django, sin DB, sin `simulator` (test de aislamiento estático igual que shadow/runtime_router/sessions/catalog). `store.py` es un singleton per-proceso keyed por símbolo, mismo patrón que `runtime_router/state.py` (F10): accessor perezoso, `reset_observability_state()` para tests, ningún `record_*()` lanza jamás. `service.py::build_snapshot()` nunca lanza — mismo patrón boundary que `select_runtime_provider`/`evaluate_market_session_for_symbol`/`check_runtime_catalog_drift`.
- `simulator/market_data_observability.py` — el único lugar autorizado a unir el snapshot puro con Django (`settings.MARKET_DATA_ROUTER_ENABLED/SYMBOLS`) y con el drift check de F12 (DB-aware) — misma razón de existir que `simulator/runtime_instrument_catalog.py` en F12.
- `simulator/management/commands/market_data_status.py` — comando de solo lectura, `--symbol`/`--all`/`--json`, sin red, sin escritura a DB.

**Puntos de integración en `feeds.py`** (todos gateados por `MARKET_DATA_OBSERVABILITY_ENABLED`, default `False`, todos en `try/except` propio): selección de proveedor y sesión (`_try_live_via_new_router`), primer tick válido y fallo terminal (`on_first_tick`/`on_terminal_failure`, ya existentes desde F10), y frescura de tick en `_broadcast()` — este último es el único hook que corre en cada tick, pero solo escribe un `float` a un dict, sin loguear ni tocar Redis ni el payload.

**Circuit breaker — garantía de no-mutación:** probado explícitamente (`test_reading_circuit_states_does_not_mutate_the_breaker`) — construir un snapshot dos veces seguidas produce el mismo `CircuitBreakerState` antes y después, byte a byte.

**Limitación — store per-proceso (igual que F10 §3.4 y F02 §3.4):** `market_data/observability/store.py` vive en la memoria de un solo proceso Python. Un despliegue ASGI multi-worker tiene un store independiente por worker, no uno compartido. `python manage.py market_data_status` corre en su **propio proceso**, separado del servidor Daphne/ASGI — por lo tanto `last_tick_at`, `active_provider_id` y `failover_count` **siempre** aparecerán vacíos al ejecutar el comando, incluso con el servidor activamente recibiendo ticks. Lo que el comando sí puede mostrar siempre, sin importar el proceso: `router_enabled`/`router_allowlisted` (settings), sesión de mercado (cálculo puro), circuit breaker (mismo store per-proceso de F10, misma limitación documentada ahí), y drift de catálogo (DB, independiente de proceso). El comando imprime esta nota explícitamente en cada corrida.

**Feature flag:** `MARKET_DATA_OBSERVABILITY_ENABLED` (default `False`). Gatea únicamente los hooks de grabación en `feeds.py` — con el flag apagado, `feeds.py` no ejecuta código nuevo alguno (ni siquiera una escritura a dict). Leer un snapshot (comando / futuro endpoint interno) siempre funciona independientemente del flag; solo mostrará estado vacío si nunca se grabó nada.

**Logging:** `event=market_data_observability_failover`, `event=market_data_observability_first_tick`, `event=market_data_observability_terminal_failure`, `event=market_data_observability_session_transition` (solo en transición, no por cada evaluación). Sin secretos, sin precios en los logs de failover/terminal_failure más allá del `error_code` ya sanitizado que F10 produce.

**Endpoint de salud interno (ítem 8) — diferido a F13b:** existe un patrón staff-only ya usado (`MoneyBrokerAdminSite`/`ModelAdmin.get_urls()` + `admin_site.admin_view()` + `JsonResponse`, ver `simulator/admin.py::broker_control_data`), pero exponerlo para observabilidad requeriría una ruta y una vista nuevas en un archivo ya extenso y cercano a zonas financieras auditadas — fuera del alcance mínimo de este bloque, tal como el spec permite explícitamente diferir. El comando de management cubre la necesidad operativa inmediata.

**Rollback:** apagar `MARKET_DATA_OBSERVABILITY_ENABLED` (ya es el default) detiene toda grabación nueva sin ningún otro cambio — el feed, el router, las sesiones y el catálogo siguen exactamente igual. Si hiciera falta revertir el código: `market_data/observability/` es un paquete nuevo y autocontenido (borrable sin dejar referencias colgantes salvo los hooks en `feeds.py`, que fallan de forma segura — `ImportError` dentro de un hook cae en el mismo `try/except` que cualquier otro error del monitor); los dos campos nuevos en `RuntimeSelectionResult` (`order_policy`, `degraded`) tienen default y no rompen construcciones existentes si se revierten.

**Futuro (no en este bloque):** compartir el store entre workers (Redis, mismo patrón pendiente desde F02 §3.4 y F10), métricas Prometheus, endpoint de salud staff-only (F13b), y un watchdog periódico que detecte el momento exacto en que un símbolo *entra* en staleness (hoy solo se puede leer staleness bajo demanda o detectar la recuperación en el siguiente tick — no hay una tarea periódica que empuje un evento "recién se puso stale").
