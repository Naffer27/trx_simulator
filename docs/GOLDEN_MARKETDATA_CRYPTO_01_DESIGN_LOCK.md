# GOLDEN-MARKETDATA-CRYPTO-01 — Design Lock

**Fecha:** 2026-09-02 · **Tipo:** Diseño únicamente. Cero modificaciones productivas. Cero tests modificados. Cero git.
**Basado en:** GOLDEN-MARKETDATA-CRYPTO-01 — Provider Access Audit (mismo día) — Massive Crypto clasificado **READY** con evidencia real (REST BTC/ETH HTTP 200 en 3 endpoints cada uno; WS BTC 539 quotes/15s, WS ETH 488 quotes/15s, multi-symbol 1,063 quotes con cero contaminación cruzada; misma `MASSIVE_API_KEY` ya usada para Forex, sin error de plan/auth).

**Objetivo:** diseñar formalmente la migración del runtime Crypto (Binance/Kraken/CoinGecko) hacia Massive-only para BTCUSD/ETHUSD, sin implementar.

---

## Regla arquitectónica principal — verificada, no asumida

El objetivo del usuario es que el trading core (margin, PnL, execution, stop-out, ledger, UI, `TradingAccount`, `Trade`) **nunca** dependa estructuralmente de Massive. Se auditó el código real para confirmar si esto ya es así:

**Confirmado por grep exhaustivo:** ningún archivo de `simulator/` (`consumers.py`, `pnl_engine.py`, `risk_engine.py`, `models.py`, `broker_ledger.py`, templates) referencia `Massive`, `Binance`, `Kraken`, `CoinGecko` ni ningún nombre de proveedor. El trading core interactúa exclusivamente con:
- `FeedManager.get_validated_quote(symbol)` — el único gate de validez de precio, ya agnóstico de proveedor (rechaza staleness/`source=="sim"` sin importar qué proveedor lo generó).
- El cache de precios en memoria/Redis (`self._prices`/`self._bids`/`self._asks`, `_write_price_cache`), namespaced por `symbol` (`"BTCUSD"`, no por proveedor).

**Conclusión:** la regla arquitectónica principal **ya está satisfecha hoy, a nivel de trading core**, para Forex y para Crypto, independientemente de qué proveedor esté detrás. El acoplamiento a un proveedor específico existe únicamente **dentro de `market_data/feeds.py`**, en las funciones de dispatch (`_try_live_legacy`) — que es exactamente donde debe vivir, no en el core. Este Design Lock no necesita inventar una capa de abstracción nueva para proteger al core (ya está protegido); solo necesita decidir CÓMO se estructura el dispatch interno de `feeds.py` (ver §H).

---

## A. Current architecture (auditado, sin modificar)

**LIVE:**
- Binance WS primario: `_binance_loop()` — un stream combinado `{mapped}@bookTicker/{mapped}@kline_1m` (`wss://stream.binance.com:9443/stream`), entrega tick (bid/ask) y vela 1m en la misma conexión.
- Kraken WS fallback: `_kraken_loop()` — ticker + `ohlc-1` vía `wss://ws.kraken.com`.
- Dispatch: `_try_live_legacy()` intenta Binance → Kraken → (Massive solo si el símbolo está en `_MASSIVE_WS_ENABLED_SYMBOLS`, hoy forex-only, así que Crypto nunca llega a Massive) → Finnhub (nunca aplica a crypto, sin `finnhub_symbol` en ningún símbolo crypto).

**HISTORICAL:**
- `fetch_kline_history()` — Binance REST klines primero, Kraken REST OHLC como fallback (`_kraken_rest_pair()`).

**REST RESYNC (`_fetch_rest_price`):**
- Binance REST ticker → CoinGecko (`_CG_IDS = {"BTCUSD":"bitcoin","ETHUSD":"ethereum"}`) → Kraken REST (`_KR_PAIRS = {"BTCUSD":"XBTUSD","ETHUSD":"XETHZUSD"}`).

**REDIS:**
- `_write_price_cache(symbol, bid, ask, source)` → `SETEX` de 3 claves: `{PREFIX}:bid:{symbol}`, `:ask:{symbol}`, `:source:{symbol}`, TTL=`_PRICE_CACHE_TTL` (60s, `PRICE_CACHE_TTL` env). Namespaced 100% por `symbol` — cero riesgo de colisión entre BTCUSD/ETHUSD/EUR-USD, ya confirmado agnóstico de proveedor.

**SYMBOL NORMALIZATION (estado actual):**

| Canónico | Binance | Kraken WS | Kraken REST | CoinGecko | Massive REST (hoy, forex-only) | Massive WS (hoy, forex-only) |
|---|---|---|---|---|---|---|
| BTCUSD | BTCUSDT | XBT/USD | XBTUSD | bitcoin | *(no wired)* | *(no wired)* |
| ETHUSD | ETHUSDT | ETH/USD | XETHZUSD | ethereum | *(no wired)* | *(no wired)* |

`exchange_symbol`/`kraken_symbol` en `symbol_specs.py` están seteados **exclusivamente** en BTCUSD y ETHUSD — confirmado por grep, ningún otro símbolo (Forex incluido) los usa.

---

## B. Target architecture

**CRYPTO PRIMARY/SOLE PROVIDER: Massive.**

| Canónico | Massive REST ticker | Massive WS pair |
|---|---|---|
| BTCUSD | `X:BTCUSD` | `BTC-USD` |
| ETHUSD | `X:ETHUSD` | `ETH-USD` |

Los símbolos canónicos internos de Money Broker (`BTCUSD`, `ETHUSD`) **no cambian** — la normalización a formato Massive ocurre exclusivamente dentro de `market_data/feeds.py`, igual que hoy ocurre para Binance/Kraken/CoinGecko. Ningún otro archivo del repo necesita conocer el formato `X:BTCUSD`/`BTC-USD`.

---

## C. Shared WebSocket design

Réplica del principio B2 Forex ya probado y verificado (commit `c84876e`, tag `b2-massive-only-forex-runtime-v1`), con una diferencia estructural obligatoria: **`wss://socket.massive.com/crypto` es un cluster distinto de `wss://socket.massive.com/forex`** — no pueden compartir la misma conexión física. Diseño:

- Estado paralelo, independiente del de Forex: `_massive_crypto_ws`, `_massive_crypto_active_symbols`, `_massive_crypto_shared_task`, `_massive_crypto_connection_watchdog_task`, `_massive_crypto_symbol_stale_attempts`, `_massive_crypto_authed`, `_massive_crypto_subscribed`, `_massive_crypto_connect_lock`, `_massive_crypto_last_quote_at` — mismos nombres de campo que Forex con sufijo `_crypto_`, misma semántica.
- `_massive_crypto_register_symbol(symbol, channel_layer)` / `_massive_crypto_unregister_symbol(symbol)` — idénticos en estructura a `_massive_register_symbol`/`_massive_unregister_symbol` (idempotente, lock durante toda la duración del teardown, nunca una zombie).
- `_massive_crypto_shared_loop(channel_layer)` — UN reader para BTCUSD+ETHUSD, auth única (`{"action":"auth","params":MASSIVE_API_KEY}` — misma key, confirmado funcional para el cluster crypto), batch subscribe tras `auth_success` (`XQ.BTC-USD,XQ.ETH-USD`), routea cada evento `"ev":"XQ"` por `ev["pair"]` al símbolo correcto.
- **Decisión de diseño explícita:** NO refactorizar el código Forex existente para "compartir" una clase genérica en este bloque — duplicar el patrón (nuevo bloque de métodos `_massive_crypto_*`, estructuralmente idéntico) es más seguro y de menor riesgo que tocar código Forex ya certificado. Extraer un helper genérico común queda anotado como oportunidad de limpieza **futura**, fuera de este migration (ver §M, riesgo de regresión Forex).

---

## D. Historical design

`fetch_massive_history()` ya existe, es genérico en su forma (multiplier/timespan/paginación/dedupe), y **ya fue probado funcionando contra `X:BTCUSD`/`X:ETHUSD`** en el audit previo (6 barras `1/day` reales, OHLC coherente). Solo necesita:
- Un helper de símbolo nuevo, análogo a `_massive_sym()` pero para crypto: `_massive_crypto_sym(symbol) -> "X:BTCUSD"` — allowlist explícito (`_MASSIVE_CRYPTO_ENABLED_SYMBOLS = frozenset({"BTCUSD","ETHUSD"})`), nunca una heurística de "reemplazar substring", exactamente el mismo principio ya documentado en `_massive_sym()`'s docstring.
- `fetch_massive_history()` internamente llama a `_massive_sym()` (forex-only hoy) — parametrizar para aceptar el ticker ya resuelto, o añadir un segundo parámetro de asset-class, en vez de mezclar lógica forex/crypto en una sola función de mapeo. Preferencia: **NO modificar `_massive_sym()` existente** (riesgo Forex) — crear el helper crypto como función hermana separada.
- `_MASSIVE_TF` (1s/1m/5m/15m/1h/1d) es genérico, ya reusable sin cambios — el frontend usa el mismo set de timeframes para cualquier asset class (confirmado: no hay lógica de timeframe condicionada por asset class en el código de historial).

---

## E. REST resync / seed design

Massive REST (`/v2/last/trade/{ticker}`, confirmado HTTP 200 con precio real en el audit) pasa a ser la autoridad de seed/resync para BTCUSD/ETHUSD, reemplazando la cascada Binance REST→CoinGecko→Kraken REST **solo para estos 2 símbolos**. Evaluar retirar del runtime (NO borrar todavía, per regla explícita del usuario): la rama Binance REST, la rama CoinGecko completa (`_CG_IDS`), y la rama Kraken REST (`_KR_PAIRS`) dentro de `_fetch_rest_price()` — condicionadas a que BTCUSD/ETHUSD sean los únicos símbolos que hoy las alcanzan (confirmado: ningún otro símbolo usa `_CG_IDS`/`_KR_PAIRS`).

---

## F. Stale watchdog design

Réplica exacta de la política **ya corregida** en B2-FOREX-PROVIDER-CLEANUP-01 (no la política original de B2-A, que escalaba a Finnhub) — Crypto adopta desde el día uno el diseño correcto, sin repetir el error que Forex tuvo que corregir después:

- **Primer incidente por símbolo** (>`DATA_STALE_TIMEOUT` sin quote válida): resubscribe de ESE símbolo únicamente (`unsubscribe`+`subscribe`), reset de la ventana de gracia. `DATA_STALE_TIMEOUT=20s` — **sin cambiar**, per instrucción explícita.
- **Segundo incidente y sucesivos:** **NO fallback a Binance/Kraken** (el punto central de este diseño) — se sigue resubscribiendo exactamente igual que el primer incidente; el único cambio es severidad de log (WARNING→ERROR) para observabilidad. `_MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD=2` se mantiene como umbral de log, no de acción — **sin cambiar el valor**, per instrucción explícita.
- **ALL-SYMBOL STALE** (ambos símbolos crypto stale simultáneamente): cerrar el shared socket de crypto, reconnect con el mismo backoff ya probado en Forex (2/4/8/16/30s capado), reconectar, auth, resubscribe batch de los símbolos activos en ese momento.
- Sin ningún fallback externo inventado — consistente con "target es Massive-only", tal como pide el usuario explícitamente.

---

## G. Fail-closed contract

**Confirmado, no se necesita código nuevo.** `get_validated_quote()` es genérico por diseño (ya opera sobre cualquier `symbol`, rechaza staleness y `source=="sim"` sin importar el asset class). El fail-safe de stop-out (`[stopout] skipped this tick ... unpriced ... fail-safe: equity is incomplete, not evaluating`) opera por posición individual, ya ejercitado con éxito para Forex en 2 sesiones manuales reales (16h y 4h11m, cero liquidaciones falsas). **Declaración explícita del Design Lock:** ninguna ruta crypto podrá alimentar margin/PnL/execution/stop-out con un precio `source=="sim"` o stale — el contrato es el MISMO ya vigente y probado, no uno nuevo. Si Massive Crypto está genuinamente caído, el símbolo queda "unpriced" y el fail-safe existente se activa sin cambios.

---

## H. Provider abstraction

**Confirmación explícita (ver "Regla arquitectónica principal" arriba):** el trading core ya está desacoplado de cualquier proveedor — el gate es `get_validated_quote()` + cache por `symbol`, ambos ya agnósticos. La pregunta real de este apartado es: ¿el DISPATCH interno de `market_data/feeds.py` necesita una capa de adapter formal, o basta con el patrón if/elif ya usado (y ya probado en producción) para Forex?

**Dos opciones evaluadas:**

**Opción A — Minimal (recomendada):** extender `_try_live_legacy()` con una rama Massive-crypto análoga a la de Massive-forex (`if MASSIVE_API_KEY and symbol in _MASSIVE_WS_CRYPTO_ENABLED_SYMBOLS: await self._massive_crypto_forex_loop(...)`), reemplazando (o precediendo, durante una fase de transición) las ramas Binance/Kraken para esos 2 símbolos. Mismo patrón exacto que ya funciona en producción para Forex. Riesgo: bajo (código nuevo, aislado, no toca Forex).

**Opción B — Formal provider adapter:** activar la capa dormida ya existente en el repo (`market_data/router/`, `market_data/instruments/` — `ProviderRouter`, `ProviderCapability`, `InstrumentProfile`), que fue diseñada exactamente para este propósito pero nunca se conectó al runtime (`MARKET_DATA_ROUTER_ENABLED=False` por defecto, y su dispatch dict no tiene entrada `"massive"` hoy — hallazgo ya documentado en B2-FOREX-PROVIDER-CLEANUP-01). Requeriría: añadir un adapter `"massive"` a ese dispatch, añadir mapeo Massive a `market_data/instruments/bridges.py`, y activar el flag — scope considerablemente mayor, primera vez que ese subsistema se ejercitaría en producción para CUALQUIER símbolo.

**Recomendación:** Opción A para esta migración. Opción B queda documentada como el camino correcto para una futura consolidación multi-proveedor (cuando exista un segundo proveedor real compitiendo con Massive), pero activarla ahora expande el scope de este bloque más allá de "migrar Crypto a Massive" y introduce riesgo de primera-vez-en-producción de un subsistema no ejercitado. Confirmar esta preferencia con el usuario antes de implementar.

**Contrato interno mínimo (independiente de A o B):**
- Live tick: `{symbol, bid, ask, timestamp, source, status}` — ya es exactamente lo que `_broadcast()` construye hoy.
- Histórico: `{symbol, timeframe, timestamp, open, high, low, close, volume}` — ya es exactamente lo que `fetch_massive_history()`/`fetch_kline_history()` devuelven hoy (incluida la deduplicación por timestamp).

---

## I. Cleanup classification

| Pieza | Clasificación | Nota |
|---|---|---|
| `_binance_loop` | **A. REMOVE AFTER MIGRATION** | Solo usado por BTCUSD/ETHUSD |
| `_kraken_loop` | **A. REMOVE AFTER MIGRATION** | ídem |
| `_binance_sym` | **A. REMOVE AFTER MIGRATION** | ídem |
| `_kraken_sym` | **A. REMOVE AFTER MIGRATION** | ídem |
| `_kraken_rest_pair` | **A. REMOVE AFTER MIGRATION** | ídem |
| `fetch_kline_history` | **A. REMOVE AFTER MIGRATION** | Reemplazado por `fetch_massive_history` + helper crypto |
| CoinGecko branch (`_fetch_rest_price`) | **A. REMOVE AFTER MIGRATION** | Solo usado por BTCUSD/ETHUSD |
| Kraken REST branch (`_fetch_rest_price`) | **A. REMOVE AFTER MIGRATION** | ídem |
| Binance REST branch (`_fetch_rest_price`) | **A. REMOVE AFTER MIGRATION** | ídem |
| `symbol_specs.py::exchange_symbol/kraken_symbol` (BTCUSD/ETHUSD) | **B. KEEP AS DORMANT** | Metadata descriptiva, no consumida en runtime tras la migración — sin riesgo si queda; remover sería un cleanup cosmético separado |
| `"binance"`/`"kraken"` entries en `_try_live_via_new_router` dispatch dict | **B. KEEP AS DORMANT** | Ruta ya dormida (`MARKET_DATA_ROUTER_ENABLED=False`); no ejecuta hoy. Riesgo latente ya documentado en B2-FOREX-PROVIDER-CLEANUP-01 (regresión si el flag se activa sin actualizar el dispatch) — mismo patrón de guard usado en Forex podría replicarse aquí |
| `fetch_massive_history` | **C. REFACTOR/REUSE** | Generalizar para aceptar ticker resuelto en vez de resolverlo internamente vía `_massive_sym` (forex-only) |
| `_write_price_cache`, `get_validated_quote`, `_broadcast`, Redis TTL/key format | **D. PROTECTED / DO NOT TOUCH** | Ya genéricos, ya probados, sin necesidad de cambio |
| `_massive_forex_loop`, `_massive_register_symbol`, `_massive_shared_loop`, `_massive_connection_staleness_watchdog` (Forex) | **D. PROTECTED / DO NOT TOUCH** | Certificados esta sesión (commit `c84876e`); crypto usa una copia paralela (§C), nunca modifica estos |
| `DATA_STALE_TIMEOUT`, `_MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD` | **D. PROTECTED / DO NOT TOUCH** | Explícitamente prohibido cambiar el valor, per instrucción del usuario |
| `simulator/tests/test_book06j1_population_engine_close_race.py`, `db.sqlite3.backup_before_0069_0072` | **D. PROTECTED / DO NOT TOUCH** | Archivos protegidos permanentes del proyecto |

---

## J. Test matrix

| # | Área | Qué prueba |
|---|---|---|
| A | BTC REST | `fetch_massive_history`/helper crypto contra `X:BTCUSD`, barras reales, timeframes soportados |
| B | ETH REST | ídem `X:ETHUSD` |
| C | BTC WS | `_massive_crypto_register_symbol("BTCUSD", ...)`, auth, subscribe, quote real recibido y broadcast |
| D | ETH WS | ídem ETHUSD |
| E | Shared multi-symbol socket | BTCUSD+ETHUSD en una sola conexión, ambos reciben quotes, un solo reader task |
| F | Symbol normalization | `_massive_crypto_sym`/pair-WS mapping — BTCUSD↔X:BTCUSD↔BTC-USD, ETHUSD↔X:ETHUSD↔ETH-USD, sin heurística |
| G | Redis write/source | `source="massive"` persistido correctamente, TTL correcto, sin colisión de key con Forex |
| H | Reconnect | backoff 2/4/8/16/30s, resubscribe de símbolos activos tras reconexión |
| I | Per-symbol stale | 1er incidente → resubscribe; 2do+ → sigue resubscribiendo, NUNCA escala a otro proveedor (mirror de la cobertura ya existente en `test_fix05b3_massive_forex_live.py::EscalationTests`, post-cleanup) |
| J | All-symbol stale | cierre+reconexión completa cuando BTCUSD y ETHUSD están stale simultáneamente |
| K | Historical bars | conteo, deduplicación, timestamps correctos para varios timeframes |
| L | Fail-closed | `get_validated_quote()` rechaza stale/sim para BTCUSD/ETHUSD igual que para Forex — reusar el mismo patrón de test que Forex ya tiene |
| M | No Binance runtime | tras la migración, `_binance_loop` nunca se invoca para BTCUSD/ETHUSD |
| N | No Kraken runtime | ídem Kraken |
| O | No CoinGecko runtime | ídem CoinGecko |
| P | No cross-symbol contamination | mirror exacto de la prueba multi-símbolo ya hecha manualmente en el audit (539+488 quotes aislados, 1,063 en batch sin mezcla) |
| Q | Margin/PnL consumers unchanged | `pnl_engine.py`/`risk_engine.py` no se tocan; tests existentes de margen/PnL para BTCUSD siguen pasando sin modificación |
| R | Protected Forex behavior unchanged | suite completa de `test_fix05b3_massive_forex_live.py` (post B2-cleanup) sigue en verde — cero regresión Forex |

---

## K. Manual acceptance

Sesión posterior a la implementación, **no antes**:
- Chart BTCUSD abierto, chart ETHUSD abierto, luego ambos simultáneos.
- `source=massive` confirmado en Redis/UI para ambos símbolos.
- Posiciones 0.01 lote en BTCUSD y ETHUSD, P/L actualizando en vivo, margen calculado correctamente.
- Sin estado "unpriced" persistente después del cold-start (un evento inicial de "unpriced" transitorio es esperado y correcto, igual que se observó y documentó para Forex).
- Cero `Redis TimeoutError`.
- Cero actividad de Binance/Kraken/CoinGecko en el log durante la sesión (grep de confirmación, igual que se hizo para Forex).
- Comportamiento de stale/reconnect de Massive observado y correcto si ocurre naturalmente.
- **Duración mínima sugerida: 15–30 minutos**, per instrucción del usuario — nota: la sesión de aceptación Forex real duró 4h11m y resultó mucho más informativa (capturó un evento de staleness real); se recomienda considerar una ventana más larga si es viable, pero 15-30 min es el mínimo aceptable para certificar.

---

## L. Expected files

**Productivos (ninguno modificado en este bloque):**
- `market_data/feeds.py` — único archivo con cambios productivos esperados: nuevas constantes (`_MASSIVE_WS_CRYPTO_URL`, `_MASSIVE_CRYPTO_ENABLED_SYMBOLS`, `_MASSIVE_HISTORICAL_CRYPTO_ENABLED_SYMBOLS`), nuevo helper de símbolo, nuevo bloque `_massive_crypto_*` (mirror de `_massive_*` forex), rama nueva en `_try_live_legacy`, ajuste en `_fetch_rest_price` (retirar Binance/CoinGecko/Kraken para BTCUSD/ETHUSD), generalización mínima de `fetch_massive_history`.

**Tests (ninguno modificado en este bloque):**
- Nuevo archivo, análogo a `test_fix05b3_massive_forex_live.py` — ej. `test_fix0Xb3_massive_crypto_live.py` (nombre exacto a definir en el bloque de implementación).
- `test_o6c1ae_forex_provider_symbol_and_failover.py::CryptoRoutingUnaffectedTests` — probablemente requiere actualización (hoy afirma "BTCUSD still routes to Binance first"; post-migración afirmaría lo contrario) — mismo patrón de "test que necesita reescribirse porque la premisa cambió" ya visto en B2-FOREX-PROVIDER-CLEANUP-01.
- Cualquier test que mockee `MASSIVE_API_KEY=""` específicamente para mantener inertes las rutas Binance/Kraken en escenarios crypto necesitaría revisión (mismo patrón que las compatibility patches vistas en Forex).

**Docs (créditos, no código):**
- Este mismo Design Lock, y un futuro `GOLDEN_MARKETDATA_CRYPTO_01_ACCEPTANCE.md` o equivalente tras la sesión manual.

---

## M. Risks

1. **Doble feed / duplicate WS tasks:** mitigado por el mismo patrón lock-held-for-teardown ya probado en Forex (`_massive_crypto_register_symbol`/`_massive_crypto_unregister_symbol`), reusado sin modificación estructural.
2. **Cache stale heredada de Binance/Kraken durante la transición:** si se hace un corte limpio (Massive reemplaza Binance/Kraken el mismo día, no una transición gradual con ambos activos), este riesgo desaparece — recomendado.
3. **Mixed provider source:** con un corte limpio (no gradual), `source` en Redis/UI para BTCUSD/ETHUSD será siempre `"massive"` tras el deploy — sin estado mixto.
4. **Reconexión accidental a Binance/Kraken:** eliminado por diseño si el código de esas rutas se retira del dispatch (no solo se deja de llamar) — a decidir en implementación si se retira código o solo se gatea.
5. **Regresión Forex:** mitigado por diseño (código crypto 100% paralelo, nunca toca `_massive_forex_loop`/`_massive_shared_loop`/etc.) — la suite completa de Forex (post B2-cleanup) debe correr en verde antes y después.
6. **Colisión de clave Redis:** descartado — namespacing por `symbol` ya es único (`BTCUSD` ≠ `EUR/USD` ≠ `ETHUSD`).
7. **Colisión de symbol mapping:** descartado — helper crypto separado del helper forex, cada uno con su propio allowlist explícito, sin heurística compartida.
8. **Router dormido (`_try_live_via_new_router`):** riesgo latente ya documentado — si `MARKET_DATA_ROUTER_ENABLED` se activa sin actualizar su dispatch, podría intentar Binance/Kraken para crypto silenciosamente. Mismo patrón de guard usado para Forex (B2-FOREX-PROVIDER-CLEANUP-01 §7) debería replicarse para crypto en el bloque de implementación.
9. **Massive genuinamente caído para crypto (riesgo operativo, no de diseño):** cubierto por el fail-closed ya existente (§G) — el peor caso es "unpriced", nunca precio simulado ni fallback silencioso a otro proveedor.

---

## N. Rollback

Rollback técnico simple, sin dejar runtime híbrido inconsistente:
- El cambio productivo completo vive en un solo archivo (`market_data/feeds.py`) más un archivo de test nuevo — un `git revert` del commit de implementación (cuando exista) es suficiente y atómico, sin necesidad de migraciones de DB (no hay cambios de modelo) ni de estado persistente irreversible (Redis solo cachea con TTL de 60s, se autolimpia).
- Si el rollback ocurre DURANTE la implementación (antes de cualquier commit), no hay nada que revertir — basta con no continuar y dejar el working tree como estaba.
- Precondición para cualquier rollback: confirmar que Binance/Kraken/CoinGecko no fueron borrados (clasificación §I dice "REMOVE AFTER MIGRATION", es decir, después de la aceptación manual exitosa, nunca antes) — mientras el código viejo siga presente aunque no se use, el rollback es trivial (revertir el dispatch de vuelta).

---

## O. READY / NOT READY FOR IMPLEMENTATION

**READY FOR IMPLEMENTATION**, condicionado a una única decisión abierta del usuario antes de autorizar el bloque de implementación:

- **§H:** ¿Opción A (minimal, mirror del patrón Forex, recomendada) u Opción B (activar el provider-adapter formal dormido)?

Ningún otro punto del diseño (arquitectura, WS compartido, watchdog, fail-closed, histórico, REST resync, cleanup, test matrix, aceptación manual, archivos esperados, riesgos, rollback) tiene ambigüedad pendiente — todos están respaldados por evidencia real de esta sesión (el audit de acceso, la arquitectura B2 Forex ya certificada, y el código actual de crypto ya leído en detalle).

---

*(Documento generado en modo solo lectura/diseño. Cero modificaciones productivas. Cero tests modificados. Cero git add/commit/tag/push.)*
