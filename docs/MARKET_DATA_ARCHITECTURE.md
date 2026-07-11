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
