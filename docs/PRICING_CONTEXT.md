# Pricing Context — SPREAD-02

Persistencia del contexto de pricing de cada ejecución real (open/close), para auditabilidad — no cambia ninguna fórmula, ningún precio, ningún spread, ninguna comisión, ningún PnL. Ver `docs/MONEY_BROKER_CURRENT_STATE.md` / auditoría SPREAD-01 para el mapa completo del pricing actual.

## Contrato (`simulator/pricing_context.py`)

`schema_version=1`. Shape actual:

| Campo | Origen | Reconstruible sin este campo |
|---|---|---|
| `raw_bid` / `raw_ask` | Tick crudo de `FeedManager` (`event["bid"]/["ask"]` en `price_tick()`, o `prices[symbol]` del daemon) | No — el tick crudo no se persiste en ningún otro lugar |
| `executable_bid` / `executable_ask` | Precio ya post-markup (`self._bid_state`/`self._ask_state`, o igual a raw en el daemon — ver abajo) | No |
| `base_spread_pips` | `BrokerSpreadConfig.spread_pips` vigente en el momento de la captura | No — puede cambiar después vía admin |
| `account_markup_pips` | `account.spread_pips_snapshot` (o `0.0`) | Técnicamente sí (congelado en la cuenta), se guarda por conveniencia de reporting |
| `effective_spread_pips` | `base_spread_pips + account_markup_pips`, calculado al construir el contexto | Sí, pero se guarda para no repetir la suma en cada lectura |
| `provider_id` / `source_state` / `router_provider` | Lectura best-effort de `market_data.observability` (F13) | No — `null` para todo símbolo fuera del router o con el flag apagado; esto es esperado, no un fallo |
| `pricing_timestamp` | Timestamp del tick (`event["time"]`) en WS; `time.time()` de la lectura en el daemon (Redis no guarda timestamp) | No |
| `pricing_profile` | Qué disparador de ejecución produjo este contexto (ver tabla abajo) | No |

Campos deliberadamente **no** persistidos por ser triviales de derivar de los de arriba: `market_spread` (`raw_ask − raw_bid`), `broker_markup` en unidades de precio, `effective_spread` en unidades de precio (`executable_ask − executable_bid`), y la comisión (ya tiene su propia ruta contable en `LedgerEntry`/`BrokerLedger`, no se duplica aquí).

## Dónde vive

- `Position.pricing_context` — contexto de apertura. Se captura una sola vez; en un merge de netting, la posición original **no se sobreescribe** (un fill promediado no tiene un único precio "real").
- `Trade.pricing_context_open` — copiado **verbatim** de `Position.pricing_context` en el momento del cierre. Nunca recalculado — un cambio posterior en `BrokerSpreadConfig` no puede alterarlo retroactivamente.
- `Trade.pricing_context_close` — capturado fresco con el tick usado para `exit_price`.

Los tres campos son `JSONField(null=True, blank=True)`, sin `default` — `null` significa explícitamente "no capturado" (fila histórica, o ruta no cubierta), nunca dato fabricado.

## Perfiles de ejecución (`pricing_profile`)

| Perfil | Ruta | Método |
|---|---|---|
| `ws_manual_open` | Apertura manual WS | `consumers.py::_order_new` |
| `ws_manual_close` | Cierre manual WS | `consumers.py::_order_close` |
| `ws_tp` / `ws_sl` | Take-profit / stop-loss mientras el usuario está conectado | `consumers.py::_check_tp_sl` |
| `ws_stopout` | Stop-out (Challenge/Funded) mientras conectado | `consumers.py::_do_stopout` |
| `ws_margin_call` | Liquidación por margen (Retail/ECN/Standard/Crypto) mientras conectado | `consumers.py::_do_retail_liquidation` |
| `daemon_tp` / `daemon_sl` | TP/SL evaluado offline (usuario desconectado) | `tasks.py::scan_positions_task` |
| `daemon_stopout` / `daemon_margin_call` | Stop-out/liquidación offline | `tasks.py::_daemon_close_all` |
| `capture_failed` | La construcción del contexto falló — la operación se completó igual | cualquiera |

Los 5 disparadores WS comparten **un único** punto de ensamblado: `TradingConsumer._capture_pricing_context()`. Los 4 disparadores del daemon comparten `tasks.py::_daemon_pricing_context()`. Ninguno duplica la lógica del otro.

## Asimetría conocida — el daemon no aplica markup

`scan_positions_task` lee bid/ask directo del cache Redis (`_read_cached_price`, el mismo raw que escribe `FeedManager`) y los usa tal cual como `close_px` — **nunca llama `broker_price()`**. Esto ya era así antes de SPREAD-02 y este bloque no lo cambia (regla explícita: no tocar ejecución). Por lo tanto, en todo contexto con perfil `daemon_*`: `executable_bid == raw_bid` y `executable_ask == raw_ask` — no es un bug de este bloque, es un reflejo honesto de lo que realmente se ejecutó. `base_spread_pips`/`account_markup_pips` sí se capturan (informativos: qué política existía), pero no se aplicaron al precio.

## Rutas excluidas (documentado, no un olvido)

| Ruta | Por qué queda fuera |
|---|---|
| `views.py::trading_dashboard` (POST) / `views.py::api_orden` | Pricing legacy, paralelo y con errores conocidos (ver auditoría SPREAD-01) — se planea eliminar/unificar en SPREAD-03. No conectado al frontend actual. |
| `simulator/admin.py::force_close` | Acción de superusuario con precio opcional tecleado a mano (o `avg_price` como fallback) — no hay tick de mercado real que capturar. |
| `simulator/population_engine.py` | Herramienta de stress-test (`populate_broker`), precios sintéticos propios, no pasa por `FeedManager`. Soporte/testing, no producción. |

En los tres casos, `Trade.pricing_context_open`/`pricing_context_close` quedan `null` automáticamente (el modelo no tiene `default`) — ningún archivo de estas rutas necesitó tocarse para lograr ese comportamiento.

## Rollback

Revertir este bloque = quitar los 3 campos (`migrations.RemoveField` inverso de `0044_pricing_context`) y los kwargs `pricing_context*` en `consumers.py`/`tasks.py` (todos tienen default `None`, así que el código sigue funcionando si se dejan de pasar). Ningún otro sistema lee estos campos hoy (no hay reporting ni UI que dependa de ellos), así que el rollback no tiene efectos en cascada.
