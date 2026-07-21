# Money Broker (trx_sim) — Project Status & Architecture Audit 2026

| Campo | Valor |
|---|---|
| Rol del documento | Auditoría técnica integral — Lead Software Architect / CTO |
| Fecha | 2026-07-21 |
| Rama | `main` (limpia, sincronizada con `origin/main`) |
| HEAD | `776d48a` — feat: broker-event-audit-foundation-audit01 |
| Commits totales | 193 (primer commit 2026-01-04, último 2026-07-17) |
| Contexto | Retomado tras migración del proyecto a un Mac nuevo |
| Alcance de este documento | Solo lectura y análisis. Ningún archivo de código fue modificado. |
| Verificación en vivo | `git status`, `manage.py check`, `manage.py collectstatic`, suite de tests completa ejecutados durante esta auditoría |

---

## 0. Veredicto ejecutivo en una frase

**Money Broker es un motor de bróker de trading funcionalmente maduro, con disciplina de ingeniería inusualmente alta para su etapa (auditorías internas antes de cada bloque, límites de responsabilidad explícitos, cero deuda de `TODO`/`FIXME` acumulada, 100% de tests verdes), pero que todavía corre sobre un feed de mercado mayormente simulado, una arquitectura de módulo único ("god files") en las piezas más críticas, y documentación de estado que quedó desactualizada respecto al HEAD real.** No es un prototipo — es un sistema que ya podría operar en staging controlado, pero **no está listo para dinero real de terceros** sin cerrar los puntos de la Sección 6 (Riesgos).

---

## 1. Resumen ejecutivo

Money Broker (`trx_sim`) es una plataforma de bróker de trading simulado/real construida en **Django 5.1.6 + Channels + Daphne + Celery + Redis + PostgreSQL**, con:

- **42 modelos de datos**, 54 migraciones (todas aplicadas, sin pendientes).
- **~31.750 líneas de código Python** en `simulator/` + `market_data/` + `trx_simulator/` (excluyendo migraciones, tests y caches).
- **2.892 tests** (127 archivos en `simulator/tests`, 16 en `market_data/tests`) — **100% en verde** tras esta auditoría (ver §8).
- Un motor de precios en tiempo real (WebSocket) con failover Binance → Kraken → Finnhub → simulación acotada.
- Un motor de trading (apertura/cierre, margen, PnL, netting) servido tanto por WebSocket (`consumers.py`) como por un daemon Celery de respaldo (`tasks.py`) para posiciones offline.
- Una capa de negocio de bróker construida en bloques incrementales y auto-auditados: `broker_ledger` (BOOK-02) → `broker_pnl` (BOOK-03) → `broker_exposure` (RISK-01) → `broker_risk` (RISK-02) → `broker_alerts` (RISK-03) → `broker_audit` (AUDIT-01). Cada módulo documenta en su propio docstring qué inconsistencia previa vino a resolver — un patrón de calidad que rara vez se ve en proyectos de este tamaño.
- Programas de fondeo (challenges Phase 1/2/Funded), wallet interna, depósitos/retiros cripto vía NOWPayments, KYC, 2FA (TOTP + Fernet), verificación de email, soporte, referidos, calendario económico.
- Un admin de Django reorganizado (`MoneyBrokerAdminSite`) en 7 secciones operativas.
- Un paquete `market_data/` nuevo (routers, circuit breaker, catálogo de instrumentos, sesiones de mercado, shadow mode, observabilidad) construido en paralelo al motor legacy, **completamente apagado por flags** hasta ser aprobado explícitamente — patrón de "dark launch" correctamente ejecutado.

El proyecto lleva **~6.5 meses** de desarrollo (193 commits) con una metodología consistente de "bloques con nombre" (`RISK-02`, `BOOK-03`, `SPREAD-05`, `AUDIT-01`, etc.), cada uno versionado con tag de git y frecuentemente con documento propio en `docs/`.

**El mayor riesgo no es de código — es de estado documental y de alcance real del feed de precios**, detallado en las secciones 5 y 6.

---

## 2. Arquitectura general

```
                                   Internet
                                      │
                         Nginx (TLS, WS upgrade, static)
                                      │
                         Daphne :8001 (ASGI) ── único entrypoint real
                                      │
                    ┌─────────────────┴─────────────────┐
                    │                                     │
              Django (sync)                        Channels (async)
                    │                                     │
        views.py (109 vistas) ── urls.py         consumers.py
        admin.py (MoneyBrokerAdminSite)          TradingConsumer (única clase, 157 KB)
                    │                                     │
                    └───────────────┬─────────────────────┘
                                     │
                     ┌───────────────┼───────────────────────┐
                     │               │                       │
              PostgreSQL/SQLite   Redis                 Celery Worker + Beat (RedBeat)
              (42 modelos)     (channel layer,           9 tareas programadas:
                                cache de precios,         reconcile_deposits/withdrawals,
                                rate limiting)            take_snapshots, scan_positions,
                                                           cleanup_*, evaluate_challenges,
                                                           observe_broker_risk_alerts

market_data/ (paquete independiente)
  symbol_specs.py  ── fuente de verdad ÚNICA de instrumentos en runtime
  feeds.py (FeedManager) ── Binance → Kraken → Finnhub → simulación
  router/, runtime_router/, shadow/, catalog/, instruments/, contracts/,
  observability/, sessions/, providers/ ── arquitectura nueva, apagada por flags
```

**Puntos clave verificados con evidencia (no solo lectura de código):**

- `trx_simulator/asgi.py` es el **único** entrypoint ASGI real. No monta `websocket_server.py` (FastAPI) ni `simulator/main.py` (FastAPI) — ambos son remanentes del prototipo original (el primer commit del repo, `a5553e9`, se llama literalmente *"Initial commit: simulator (FastAPI WS + Django UI)"*). Confirmado también en `docs/MARKET_DATA_ARCHITECTURE.md` (bloque MD-1, auditoría de código muerto con evidencia de imports).
- `market_data.symbol_specs.SymbolSpec` es la única fuente de verdad para pricing/riesgo/routing en runtime. El modelo `Instrument` (DB) existe en paralelo pero **no tiene ningún efecto en el trading real** — es un catálogo administrativo desconectado (riesgo documentado, ver §6).
- Todos los flags de arquitectura nueva de `market_data/` (`MARKET_DATA_SHADOW_MODE`, `MARKET_DATA_ROUTER_ENABLED`, `MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED`, `MARKET_DATA_OBSERVABILITY_ENABLED`) están en `False` por defecto y así deben permanecer sin aprobación explícita.
- `CHANNEL_LAYERS` usa Redis en producción (`REDIS_URL`) y memoria en dev — correcto y documentado.
- Celery usa RedBeat (scheduler distribuido, lock en Redis) — correcto para más de un worker.

---

## 3. Estado por módulo

### 3.1 Apps Django
- Una sola app real: **`simulator`** (monolito — 44 archivos Python de primer nivel, sin sub-apps). `market_data` es un paquete Python normal, no una app Django registrada (no tiene `apps.py`, no tiene modelos propios con migraciones — sus modelos residen físicamente en `simulator/migrations`, ver nota en §7).
- **Estado: funcional y estable.** El costo de esta estructura (un solo app monolítico) se paga en mantenibilidad, no en corrección — ver §7 (deuda técnica).

### 3.2 Trading Engine
- `order_engine.py`, `orders.py`, `pnl_engine.py`, `spread_engine.py`, `dynamic_spread.py`, `pricing_context.py`, `commercial_pricing.py`, `spread_config_cache.py`.
- Apertura/cierre real ocurre en dos caminos que deben mantenerse en paridad: **WebSocket** (`consumers.py`, síncrono via `database_sync_to_async`) y **daemon Celery offline** (`tasks.py::scan_positions_task` → SL/TP/stopout/margin-call cuando el usuario no tiene el dashboard abierto). Los commits recientes (`panel02`–`panel04`, `margin02`) muestran trabajo activo y deliberado para blindar esta paridad (guardas atómicas de margen, concurrencia en el cierre).
- Spread dinámico (`SPREAD-05`) es determinístico por diseño (mismo input → mismo output, sin `random`/`uuid4`), con `is_dynamic=False` por defecto → comportamiento bit-a-bit idéntico al motor anterior salvo opt-in explícito.
- **Estado: maduro.** Es la parte del sistema con más iteraciones de auditoría propia (SPREAD-01 a SPREAD-05, panel01 a panel04, margin01-02).

### 3.3 Broker Engine (negocio del bróker, no del trader)
- Cadena BOOK-02 → BOOK-03 → RISK-01 → RISK-02 → RISK-03 → AUDIT-01, todos construidos secuencialmente sobre hallazgos de un "FASE 1 audit" documentado en cada módulo.
- `broker_pnl.py` (BOOK-03) es hoy la única fuente correcta de PnL del bróker — antes había implementaciones parciales e inconsistentes en `admin.py`, `exposure_engine.py` y `broker_monitoring.py` (una de ellas omitía silenciosamente el resultado de contraparte realizado). Ya corregido y unificado.
- `broker_exposure.py` (RISK-01) documenta explícitamente que antes de su creación el cálculo de notional/exposición vivía en **cuatro lugares distintos**, dos con bug real (`contract_size` faltante, subestimando notional en órdenes de magnitud para FX). Ya corregido.
- **Estado: maduro y con excelente trazabilidad de por qué existe cada pieza.** Es el módulo mejor documentado internamente de todo el proyecto.

### 3.4 Ledger / Wallet
- `wallet_ledger.py`: punto de entrada único para `Wallet.available_balance` (nunca se muta directo — regla de código enforced por convención + revisada). Toda mutación pasa por `credit_wallet()`/`debit_wallet()` dentro de `transaction.atomic()` + `select_for_update()`, con invariante de reconciliación explícito (`SUM(WalletTransaction) == Wallet.available_balance`).
- `broker_ledger.py` (BOOK-02): un registro por Trade cerrado (`broker_counterparty_pnl = -trader_pnl`), ya que hoy **todo trade se ejecuta B-Book** (no existe A-Book/hedge/LP).
- **Estado: sólido.** Diseño textbook de doble entrada con guardas de concurrencia correctas.

### 3.5 Risk Engine
- Dos capas claramente separadas y sin solaparse:
  - `risk_engine.py` — compliance **por cuenta** (drawdown, daily loss, clasificación de trader, `TraderScore`).
  - `broker_risk.py` (RISK-02) — límites **agregados del libro completo del bróker** (¿puede el bróker aceptar esta nueva exposición?), independiente de A-Book/B-Book.
- `broker_alerts.py` (RISK-03) es puramente observacional — nunca bloquea una orden ni muta estado financiero, solo lee `broker_exposure`/`broker_risk`/`BrokerRiskLock` y clasifica severidad.
- **Estado: maduro**, con separación de responsabilidades ejemplar (compliance vs. agregado vs. alertas vs. auditoría son 4 módulos distintos, cada uno con un solo trabajo).

### 3.6 Challenge Engine (Funding Programs)
- `challenge_engine.py`: activación, evaluación de fase, avance a Phase 2/Funded — todo con `transaction.atomic()` + `select_for_update()`, sin tocar `consumers.py`/`tasks.py`/`risk_engine.py` directamente (contrato documentado en su propio docstring).
- Compra vía wallet interna o vía NOWPayments (crypto) — ambos caminos conviven, con idempotencia (una compra activa bloquea recompra) y rollback total si la activación falla a mitad de camino.
- **Estado: funcional**, con 42 tests dedicados solo a la compra vía wallet.

### 3.7 Market Data
- `feeds.py::FeedManager` es el motor real de precios en vivo: Binance → Kraken → Finnhub → simulación acotada con resync REST.
- **Solo 3 símbolos tienen feed en vivo real confirmado (BTCUSD, EUR/USD y similares vía las 3 fuentes)** — el resto de instrumentos corre en modo simulado. Metales, petróleo e índices están **explícitamente bloqueados** por regla del propio roadmap del proyecto hasta validar un proveedor real (documentado en `MARKET_DATA_ARCHITECTURE.md` §6).
- Arquitectura nueva (`router/`, `runtime_router/`, `shadow/`, `catalog/`, `instruments/`, `contracts/`, `observability/`, `sessions/`, `providers/`) — bien testeada (16 archivos de test) pero **100% apagada por flags**, sin ningún efecto en producción todavía. Es infraestructura construida por adelantado (FOUNDATION-08 a FOUNDATION-13), no una funcionalidad entregada.
- **Código muerto confirmado** (con evidencia de cero imports, documentado por el propio equipo en MD-1): `market_data/adapters/{binance,finnhub}.py`, `interfaces.py`, `dto.py`, `normalizer.py`, `config.py`, `provider_registry.py`, `hub.py`, `websocket_server.py`, `run_all.sh`.
- **Estado: motor legacy maduro y en producción; arquitectura sucesora construida pero no activada.**

### 3.8 Celery
- 9 tareas programadas vía RedBeat: reconciliación de depósitos/retiros (15 min), heartbeat (5 min), snapshots de equity (1 min), limpieza de audit log (diaria 2 AM) y snapshots (diaria 3 AM), daemon de posiciones offline (30 s), snapshot de revenue (5 min), evaluación de challenges (horaria), observación de alertas de riesgo (5 min).
- Límites configurados correctamente: `TASK_TIME_LIMIT=30min`, `SOFT_TIME_LIMIT=5min`, reinicio de worker cada 500 tareas (memory safety), `expires` en cada beat entry para evitar acumulación si el worker se atrasa.
- **Estado: bien configurado para producción**, sin señales de deuda.

### 3.9 Redis
- Triple uso: channel layer (Channels), broker/result backend de Celery + lock de RedBeat, y rate limiting HTTP (`ratelimit.py`). Todo con fallback documentado: si Redis cae, `rate_check()` es **fail-open** (nunca bloquea tráfico legítimo por infraestructura caída) y lo loguea como evento de seguridad.
- **Estado: correcto**, con `deploy/redis_persistence.conf` (AOF+RDB) listo para producción.

### 3.10 Channels / Daphne / WebSockets
- Único consumer real: `TradingConsumer` (`consumers.py`, **157 KB en una sola clase**). Maneja ticks de precio, apertura/cierre, netting, margen, pricing context — todo en un archivo.
- **Estado: funcional pero es el punto de mayor concentración de complejidad del proyecto** (ver §7, deuda técnica).

### 3.11 Dashboard
- `trading_dashboard` (views.py) + `dashboard.html` + assets propios. Sidebar consciente del tipo de cuenta (Demo/Real, bloque K.5).
- **Estado: funcional**, sin issues detectados más allá del hallazgo de infraestructura de §8 (manifest de estáticos).

### 3.12 Admin
- `admin.py`, 146 KB, 37 `ModelAdmin` + `MoneyBrokerAdminSite` (7 secciones: CORE OPERATIONS, TRADING ENGINE, FUNDING PROGRAMS, PAYMENTS & LEDGER, BROKER BUSINESS, GROWTH, TOOLS).
- Acciones sensibles protegidas con `@superuser_required_action` (permissions.py).
- **Estado: funcional y bien organizado**, pero es el segundo archivo más grande del proyecto — mismo patrón de "god file" que `consumers.py`/`views.py`.

### 3.13 APIs
- REST-ish, no DRF — vistas Django clásicas devolviendo `JsonResponse`. Incluye API de estado de challenge (bearer token), webhook de activación externa (firma HMAC), diagnóstico NOWPayments, health/metrics, exposición de broker.
- **Estado: funcional**, coherente con el resto del proyecto (sin framework de API separado, deuda menor de consistencia si se planea abrir integraciones a terceros a futuro).

### 3.14 Seguridad
- `manage.py check` → **sin issues**. `manage.py check --deploy` no se ejecutó en esta auditoría (requiere variables de producción que no deben forzarse en este entorno) — pendiente antes de cualquier despliegue real, ya cubierto como paso explícito en `SECURITY_CHECKLIST.md`.
- Firma HMAC-SHA512 con `hmac.compare_digest` (constant-time) para IPN de NOWPayments — correcto.
- Guardas `ImproperlyConfigured` en arranque si faltan en producción: `DJANGO_SECRET_KEY`, `EMAIL_HOST` (con backend SMTP), `NOWPAYMENTS_IPN_SECRET`.
- 2FA: TOTP con secretos cifrados Fernet (fallback base64 solo en dev, con warning explícito en logs).
- Rate limiting fail-open + logging de seguridad dedicado (`simulator.security` logger).
- `.env`, `db.sqlite3`, `media/` correctamente excluidos de git (`.gitignore` verificado, `git ls-files` confirma que ninguno está trackeado).
- **Hallazgo de seguridad histórico ya remediado**: un API token de Finnhub hardcodeado en `test_ws_finnhub.js` (presente desde el commit inicial) fue movido a `scripts/manual/` y reescrito para leer de entorno. **El valor sigue vivo en el historial de git** — la rotación de esa key en el dashboard de Finnhub es una acción pendiente fuera del alcance de este repo (documentado en `MARKET_DATA_ARCHITECTURE.md` §4).
- **Estado: sólido para etapa staging; requiere el checklist de `SECURITY_CHECKLIST.md` completo antes de producción real.**

### 3.15 Permisos
- `@superuser_required_action` para acciones de admin, `require_2fa`/`staff_require_2fa` para vistas, gates de propiedad de recurso (un usuario no puede ver el depósito de otro — verificado en smoke test H-3/H-4).
- **Estado: funcional.**

### 3.16 NOWPayments
- Módulo aislado (`nowpayments.py`): creación de pago, verificación IPN, payouts vía JWT, estimación de precio, currencies soportadas.
- **Estado: funcional**, con fallback documentado como "legacy temporal" pendiente de reemplazo por un Treasury Engine separado (ver `docs/MONEY_BROKER_CURRENT_STATE.md` — pendiente aún vigente).

### 3.17 Emails
- Módulos separados por dominio: `deposit_emails.py`, `withdrawal_emails.py`, `kyc_emails.py`, `support_emails.py`, `email_verification.py`. Backend file-based en dev (`dev_emails/`), SMTP en prod (con guarda de arranque si falta `EMAIL_HOST`).
- **Gap detectado (heredado, ya documentado en `STAGING_READINESS_K4.md`):** `.env.example` sugiere `anymail` (SendGrid/Mailgun) como backend de producción, pero `anymail` **no está en `requirements.txt`** — un `pip install` fallaría si se intenta usar ese backend tal cual está documentado.
- **Estado: funcional en dev; gap de dependencia pendiente antes de usar un proveedor transaccional real.**

### 3.18 Scheduler
- Ver §3.8 (Celery/RedBeat). **Estado: correcto.**

### 3.19 Tests
- **2.892 tests, 100% pasando, 3 skipped** (verificado en vivo en esta auditoría — ver §8 para el detalle del falso positivo inicial).
- Cobertura por bloque es consistente con la metodología del proyecto (cada bloque nuevo trae su propio archivo de test).
- `tests_frontend/` tiene un único archivo (`panel04_order_ticket.test.mjs`) — cobertura de frontend mínima comparada con la de backend.
- `load_tests/` (Locust + script de WS) existe pero no se ejecutó en esta auditoría (requiere entorno corriendo).
- **Estado: excelente para backend; frontend/carga es la superficie de test más delgada.**

### 3.20 Documentación
- `docs/` tiene 9 archivos, todos de altísima calidad técnica y con evidencia (no aspiracionales) — pero **desactualizados respecto al HEAD actual**:
  - `MONEY_BROKER_CURRENT_STATE.md` — fechado 2026-06-26, checkpoint `pre-treasury-v1`. El HEAD actual está **~15+ commits y 5 bloques completos por delante** (BOOK-02/03, RISK-01/02/03, AUDIT-01 no aparecen en este documento).
  - `RESUME_TOMORROW.md` (raíz) — referencia un HEAD (`8390bd3`) y un tag (`money-flow-audit-v1`) que ya no reflejan el estado actual del repo; quedó huérfano de un ciclo de trabajo anterior.
  - Ambos deberían archivarse o reemplazarse por este documento como nuevo punto de entrada.
- No existe `README.md` en la raíz — no hay una puerta de entrada única para alguien (o el propio usuario, tras una migración de máquina) que llegue al repo por primera vez. `DEPLOY.md` y `SECURITY_CHECKLIST.md` cubren partes del rol de un README pero no reemplazan uno.
- Dos imágenes de arquitectura en la raíz (`Mapa de Conexión del Broker & Simulador.png`, `arquitectura_trading_multi_tenant.png`) no están enlazadas desde ningún documento en `docs/`.
- **Estado: contenido excelente, organización y vigencia desactualizadas.**

---

## 4. Qué está terminado

- Autenticación, registro, verificación de email, aceptación de términos, 2FA TOTP.
- Wallet interna con ledger de doble entrada auditable.
- Depósitos y retiros cripto vía NOWPayments (firma IPN verificada, idempotencia, guardas de estado terminal).
- Apertura/cierre de posiciones (WebSocket + daemon offline en paridad), netting, margen, stopout, margin call.
- Motor de PnL del bróker unificado y económicamente correcto (BOOK-02/03).
- Motor de exposición y límites de riesgo agregados del bróker (RISK-01/02).
- Alertas de riesgo operacional en tiempo real (RISK-03) y auditoría de eventos cross-engine (AUDIT-01).
- Programas de fondeo (challenges Phase 1/2/Funded) con compra por wallet o cripto.
- Snapshots de equity (cuenta y bróker) y de revenue, con retención configurable y limpieza automática.
- Panel operacional de staff, admin reorganizado por secciones de negocio.
- Motor de spread dinámico determinístico (opt-in, retrocompatible).
- KYC, soporte, referidos, calendario, documentos, expertos (módulos de ecosistema).
- Suite de tests de 2.892 casos, 100% verde.
- Runbooks de despliegue (`DEPLOY.md`), infraestructura recomendada (`docs/INFRA_PLAN_L1.md`) y checklist de seguridad pre-lanzamiento (`SECURITY_CHECKLIST.md`).

## 5. Qué falta

- **Treasury Engine** separado (mencionado como pendiente desde el checkpoint `pre-treasury-v1` y aún no iniciado según el estado del repo) — la wallet actual queda como "legacy temporal" hasta que exista.
- **Proveedor de datos de mercado real para Forex/Metals/Oil/Indices** — hoy solo BTCUSD/EUR-USD-clase tienen feed en vivo real; el resto es simulado. Bloques MD-2 a MD-7 (routing explícito por clase de activo, provider router con circuit breaker real, fuente única de instrumento, integración de proveedor real, SLA de calidad de dato) están planeados pero no ejecutados.
- **Activación de la arquitectura nueva de `market_data/`** (router/runtime_router/shadow/catalog) — construida y testeada, pero 100% detrás de flags apagados.
- **`Instrument` (DB) vs `SymbolSpec` (código)** — decisión arquitectónica pendiente (¿cuál es la fuente de verdad final?), documentada como bloque MD-5 futuro.
- **`anymail` en `requirements.txt`** — gap de dependencia para poder usar el backend de email de producción documentado.
- **`manage.py check --deploy`** con variables de producción reales — no ejecutado en este entorno de desarrollo (correcto no hacerlo aquí), pendiente como parte del checklist antes de cualquier despliegue.
- **`TOTP_STAFF_REQUIRED=True`** y demás variables críticas de `STAGING_READINESS_K4.md` — pendientes de fijar en el `.env` real de cada entorno (no es un gap de código, es un gap de configuración de despliegue).
- **Cobertura de test de frontend** — un solo archivo (`tests_frontend/`) frente a 2.892 tests de backend.
- **Documento de estado unificado** — este mismo documento reemplaza la necesidad, pero `MONEY_BROKER_CURRENT_STATE.md` y `RESUME_TOMORROW.md` deben marcarse como históricos.
- **`README.md`** en la raíz del repo.

## 6. Riesgos

| # | Riesgo | Severidad | Detalle |
|---|--------|-----------|---------|
| 1 | **Feed de precios mayormente simulado** | **Alto** | Solo un puñado de símbolos tiene precio real de mercado. Operar cualquier instrumento fuera de esos con dinero real expondría al bróker (o al cliente) a precios que no reflejan el mercado. El propio roadmap ya prohíbe activar XAU/XAG/Oil/índices sin proveedor validado — regla correcta, debe seguir aplicándose sin excepción. |
| 2 | **`Instrument` (DB) desconectado del runtime** | **Medio-Alto** | Un operador de staff puede editar spread/leverage/`trading_enabled` de un instrumento vía admin creyendo que tiene efecto real — no lo tiene. Vector de error operativo silencioso con dinero real. Mitigación mínima: advertencia visible en el admin de `Instrument` hasta resolver MD-5. |
| 3 | **Concentración de complejidad en archivos únicos** | **Medio** | `consumers.py` (157 KB, una sola clase), `views.py` (157 KB, 109 funciones), `admin.py` (146 KB), `models.py` (103 KB, 42 modelos). Cualquier cambio en el motor de trading toca un archivo que ya es difícil de revisar en un PR. Riesgo de regresión silenciosa aumenta con el tamaño del archivo, no con la calidad del código en sí (que es buena). |
| 4 | **Documentación de estado desactualizada** | **Medio** | `MONEY_BROKER_CURRENT_STATE.md` y `RESUME_TOMORROW.md` no reflejan el HEAD real. Si alguien retoma el proyecto leyendo solo esos documentos (como casi ocurre en esta sesión), parte de una foto vieja del sistema. |
| 5 | **Dependencias huérfanas de FastAPI (`fastapi`, `uvicorn`, `starlette`, `pydantic*`, `fastapi-cli`, `fastapi-cloud-cli`, `rich-toolkit`, `typer`, etc.)** | **Bajo-Medio** | Superficie de ataque e instalación innecesaria — nada del runtime real las usa (confirmado: el único importador, `simulator/main.py`, es código muerto). Cada dependencia extra es una dependencia a la que hay que darle mantenimiento/parchear por CVEs sin beneficio funcional. |
| 6 | **Historial de git con secreto histórico** | **Bajo** (si ya se rotó) / **Alto** (si no) | El token de Finnhub hardcodeado en el commit inicial sigue en el historial de git independientemente de que el archivo actual ya no lo tenga. **Verificar que la key fue rotada en el dashboard de Finnhub** — si no se ha hecho, es la acción de mayor prioridad de esta lista pese a ser "solo" un token de datos de mercado. |
| 7 | **`anymail` faltante en requirements** | **Bajo** | Solo se manifiesta al intentar usar el backend de email de producción documentado — bloquearía el primer despliegue a staging si no se detecta antes. |
| 8 | **Cobertura de test de frontend mínima** | **Bajo-Medio** | El motor de trading vive en gran parte en JS/WS del lado cliente (dashboard). Un solo archivo de test frontend frente a 2.892 de backend deja regresiones de UI/UX fuera del radar de la suite automatizada. |
| 9 | **`manage.py check --deploy` no verificado con config real de producción** | **Medio** | No se puede descartar que existan warnings/errors de despliegue no vistos hasta que se ejecute con las variables reales — acción explícita ya listada en `SECURITY_CHECKLIST.md` paso 10, aún pendiente de ejecución real. |

## 7. Deuda técnica

- **Archivos monolíticos ("god files"):** `consumers.py` (157 KB / 1 clase), `views.py` (157 KB / 109 funciones), `admin.py` (146 KB), `models.py` (103 KB / 42 modelos), `tasks.py` (54 KB). Ninguno tiene bugs conocidos, pero todos superan el punto en que un archivo es revisable cómodamente en un solo PR. Refactor recomendado: dividir por dominio (auth, wallet, trading, challenges, admin sections) — sin cambiar comportamiento, solo estructura.
- **Único app Django (`simulator`)** para todo excepto `market_data`. Aceptable para el tamaño actual, pero cualquier crecimiento futuro (ej. separar KYC, soporte, o el ecosistema de referidos/bonos) se beneficiaría de sub-apps con sus propios modelos/migraciones.
- **`market_data`'s modelos viven físicamente en `simulator/migrations`** (confirmado: `market_data/catalog/migrations` existe pero con 0 archivos — las migraciones reales de `market_data` están en las migraciones de `simulator`). Acoplamiento de infraestructura que contradice la separación de paquetes que el resto de `market_data/` sí respeta.
- **Código muerto confirmado y ya inventariado por el propio equipo** (MD-1): adapters stub de Binance/Finnhub, `interfaces.py`, `dto.py`, `normalizer.py`, `config.py`, `provider_registry.py`, `hub.py`, `websocket_server.py`, `simulator/main.py`, `run_all.sh` (apunta a un path que ya no existe tras el rename `trx_simulator` → `trx_sim`). Ninguno afecta runtime, pero infla el repo y confunde a quien lo explora por primera vez.
- **Dependencias huérfanas de FastAPI** en `requirements.txt` (ver §6, riesgo 5).
- **Sin bare `except:`** en todo el código de producción (0 ocurrencias) — mencionado aquí como *ausencia* de deuda, señal positiva rara de destacar explícitamente.
- **Casi cero `TODO`/`FIXME`** en código real (4 ocurrencias totales, 2 de ellas en comentarios que documentan los propios adapters muertos, y 2 en símbolos de test intencionalmente falsos `"XXXXXX"`). Para un proyecto de ~32K LOC esto es un nivel de disciplina inusual — normalmente señal de que el equipo cierra bloques en vez de dejarlos a medias con marcadores.
- **Sin API framework** (DRF/ninja) — las 109 vistas son Django clásico devolviendo `JsonResponse` a mano donde se necesita JSON. No es un problema hoy; sería fricción si se planea abrir una API pública o mobile a terceros.

## 8. Calidad del código

**Evidencia recolectada en vivo durante esta auditoría** (no solo inspección estática):

```
$ git status                    → limpio, main al día con origin
$ python manage.py check        → System check identified no issues (0 silenced)
$ python manage.py showmigrations simulator → 54/54 aplicadas, 0 pendientes
$ python manage.py test simulator.tests market_data.tests -v 1
    Primera corrida (sin collectstatic previo, entorno recién migrado):
       Ran 2892 tests in 166.582s — FAILED (errors=258, skipped=3)
       Causa raíz única: ValueError: Missing staticfiles manifest entry
       for 'css/dashboard.css' — WhiteNoise's CompressedManifestStaticFilesStorage
       requiere que collectstatic se haya ejecutado al menos una vez;
       tras la migración a este Mac, nunca se corrió.
    Tras `python manage.py collectstatic --noinput`
    (128 archivos copiados, 387 post-procesados):
       Segunda corrida completa:
       Ran 2892 tests in 168.128s — OK (skipped=3)
```

**Conclusión de esta verificación: no había ninguna regresión de código.** Los 258 "fallos" iniciales eran 100% un artefacto de entorno post-migración (falta un `collectstatic`), no un bug del sistema. **Esto debe agregarse como paso explícito al protocolo de "retomar tras migrar de máquina"**: correr `collectstatic` antes de correr la suite de tests en cualquier Mac/entorno nuevo.

Otros indicadores de calidad:
- 0 `except:` desnudos en todo el código de producción.
- Prácticamente 0 deuda de `TODO`/`FIXME` sin resolver.
- Convención consistente de Decimal para dinero (nunca float en cálculos financieros — confirmado en docstrings de `wallet_ledger.py`, `challenge_engine.py`, `broker_ledger.py`).
- Uso sistemático de `transaction.atomic()` + `select_for_update()` en toda mutación financiera.
- Cada módulo crítico documenta en su cabecera: qué hace, qué NO hace, y qué inconsistencia previa vino a resolver — patrón de "auditoría antes de construir" replicado en más de 8 módulos distintos (`SPREAD-*`, `BOOK-*`, `RISK-*`, `AUDIT-01`, `MD-1`).
- Logging estructurado opcional (JSON) con loggers especializados por subsistema (`simulator.ws`, `simulator.risk`, `simulator.security`, `simulator.exposure`, etc.), cada uno con su propio nivel configurable por variable de entorno.

**Veredicto de calidad: notablemente por encima del promedio para la etapa del proyecto.** El principal costo no es de corrección sino de tamaño de archivo (§7).

## 9. Prioridad de los siguientes bloques

1. **Higiene de entorno post-migración** (inmediato, sin código): documentar `collectstatic` como paso obligatorio al levantar el proyecto en una máquina nueva; confirmar rotación de la key histórica de Finnhub si aún no se hizo.
2. **Actualizar/archivar documentación de estado**: reemplazar `MONEY_BROKER_CURRENT_STATE.md` y `RESUME_TOMORROW.md` por una referencia a este documento; agregar `README.md` mínimo.
3. **Limpieza de código muerto ya inventariado** (MD-1 `DELETE_LATER` list) y de dependencias FastAPI huérfanas en `requirements.txt` — bajo riesgo, alto valor de claridad, cero impacto funcional.
4. **`anymail` en requirements** antes de intentar el primer despliegue a staging con email de producción.
5. **Ejecutar `manage.py check --deploy`** con variables reales de staging y cerrar cualquier warning crítico, siguiendo `SECURITY_CHECKLIST.md` en orden.
6. **Decisión arquitectónica MD-5** (¿`SymbolSpec` o `Instrument` como fuente de verdad?) antes de seguir invirtiendo en el catálogo DB o en más UI de admin sobre `Instrument`.
7. **Refactor incremental de los "god files"** (`consumers.py`, `views.py`, `admin.py`) — dividir por dominio, sin cambiar comportamiento, con la misma disciplina de "auditar antes de tocar" ya demostrada en bloques anteriores.
8. **Ampliar cobertura de test de frontend** en proporción al peso del dashboard en la experiencia de trading real.
9. **Iniciar el Treasury Engine** (o al menos su diseño) como reemplazo planeado de la wallet interna actual, sin apagar NOWPayments/wallet hasta que esté probado.
10. **Continuar el roadmap MD-2 → MD-7** de `market_data/` solo cuando haya un proveedor real validado — no antes.

## 10. Roadmap recomendado hacia Production Ready

```
Fase 0 — Higiene inmediata (días)
  ├─ collectstatic documentado en el protocolo de arranque
  ├─ Confirmar rotación de key histórica de Finnhub
  ├─ README.md + archivar docs de estado obsoletos
  └─ Limpieza de código muerto MD-1 + deps FastAPI huérfanas

Fase 1 — Cierre de checklist de seguridad (antes de cualquier staging real)
  ├─ SECURITY_CHECKLIST.md completo, en orden, sin saltos
  ├─ manage.py check --deploy limpio con env de staging real
  ├─ anymail en requirements + smoke test de email real
  └─ Variables críticas de STAGING_READINESS_K4.md fijadas (TOTP_STAFF_REQUIRED=True, etc.)

Fase 2 — Staging real (VPS, según docs/INFRA_PLAN_L1.md)
  ├─ Deploy con Postgres + Redis + Daphne + Celery/Beat + Nginx
  ├─ Backups offsite de Postgres + media probados con restore drill
  └─ Smoke test K.3/K.4 repetido contra el entorno real (no local)

Fase 3 — Decisiones arquitectónicas pendientes
  ├─ MD-5: fuente de verdad única de instrumento
  ├─ Treasury Engine: diseño + integración sin apagar wallet actual
  └─ Refactor de god files (consumers/views/admin) por dominio

Fase 4 — Datos de mercado reales
  ├─ Proveedor validado para Forex/Metals/Oil/Indices (MD-6)
  ├─ Activación gradual de router/shadow/catalog ya construidos (MD-2 a MD-4, MD-7)
  └─ Solo entonces: evaluar activar XAU/XAG/Oil/índices

Fase 5 — Producción con dinero real de terceros
  ├─ Todo lo anterior cerrado y verificado en staging por al menos un ciclo completo
  ├─ Auditoría externa de seguridad (recomendado, fuera del alcance de este equipo)
  └─ Plan de rollback y monitoreo (Sentry) verificado end-to-end
```

---

## Anexo A — Comandos ejecutados durante esta auditoría (evidencia)

```bash
git status                                   # limpio
git branch --show-current                    # main
git log --oneline -20                        # HEAD = 776d48a
python manage.py check                       # sin issues
python manage.py showmigrations simulator    # 54/54 aplicadas
python manage.py collectstatic --noinput     # 128 archivos, 387 post-procesados
python manage.py test simulator.tests market_data.tests -v 1
                                              # 2892 tests, OK (skipped=3), tras collectstatic
```

## Anexo B — Inventario de código muerto (con evidencia de cero imports)

| Archivo | Evidencia |
|---|---|
| `market_data/adapters/binance.py`, `finnhub.py` | Solo auto-importados por `interfaces.py`; nunca instanciados |
| `market_data/interfaces.py`, `dto.py`, `normalizer.py`, `config.py` | Cero imports en todo el repo (confirmado por grep + MD-1) |
| `market_data/provider_registry.py`, `hub.py` | Cero imports / único consumidor también huérfano |
| `websocket_server.py` (FastAPI) | No montado en `asgi.py`; solo referenciado por `run_all.sh` |
| `simulator/main.py` (FastAPI) | No importado por ningún entrypoint real (`asgi.py`/`wsgi.py`) |
| `run_all.sh` | Apunta a `$HOME/Desktop/trx_simulator`, directorio inexistente tras el rename a `trx_sim` |

## Anexo C — `TODO`/`FIXME` encontrados (búsqueda exhaustiva, 4 resultados totales)

```
market_data/providers/__init__.py:14   → comentario que documenta el código muerto de arriba
market_data/adapters/binance.py:6      → stub muerto (ver Anexo B)
market_data/adapters/finnhub.py:6,11   → stub muerto (ver Anexo B)
simulator/tests/test_spread_engine.py  → símbolo de test intencional "XXXXXX", no es deuda real
```

No se encontraron bare `except:`, ni funciones marcadas `NotImplementedError` fuera de las ya inventariadas como código muerto, ni bloques `HACK`/`XXX`/`WIP` en código de producción.

---

*Documento generado como parte de la auditoría CTO/Lead Architect solicitada el 2026-07-21. No se modificó ningún archivo del proyecto salvo la creación de este mismo documento. `staticfiles/` fue regenerado localmente (`collectstatic`) durante la verificación de tests — directorio ignorado por git, no afecta el árbol de trabajo (`git status` permanece limpio).*
