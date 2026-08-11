# Treasury Incident Runbook

**Alcance:** procedimientos operativos para incidentes que afectan al flujo Treasury (`TreasuryOperationRequest`), al audit trail (`BrokerAuditEvent` / `AuditLog`) y a la infraestructura de la que ambos dependen (Celery worker, Celery Beat, Redis, PostgreSQL). Cierra la parte documental de HIGH-5 (O.4e-1).

**Actualizado en O.4e-5:** la detección automática de staleness del audit trail (heartbeat de Celery Beat, O.4e-2) y su integración en `/api/health/detail/` (O.4e-3), así como el escalamiento observacional de un `EXECUTING` persistentemente atascado (O.4e-4), **ya existen y están implementados** — las Secciones 2 y 8 fueron actualizadas para reflejarlo. Ninguno de los dos automatiza recuperación: ambos son puramente observacionales (log/evento/Sentry), la única acción con efecto sobre el estado de negocio sigue siendo la Sección 1. Todo lo que sigue describe herramientas que existen realmente en este repositorio — ningún comando, servicio o procedimiento de este documento es hipotético.

**No modifica:** código, modelos, settings, Celery, endpoints de health, `AuditLog`, `BrokerAuditEvent`, workflows Treasury, Wallet/Ledger, O.4a-O.4d, ni `treasury_engine` (proyecto separado).

---

## 0. Mecanismos existentes que este runbook referencia

| Mecanismo | Dónde vive | Qué hace |
|---|---|---|
| `mark_treasury_execution_failed()` | `simulator/treasury_execution_recovery.py` | Única mutación autorizada para recuperar un `EXECUTING` atascado: transición a `FAILED`, nunca revive dinero, exige `recovery_reason`, prohíbe auto-recuperación (`requested_by`/`approved_by` no pueden ejecutarla) |
| Vista de recuperación admin | `/admin/simulator/treasuryoperationrequest/<pk>/recover/` (`admin:treasury_request_recover`) | Único punto de entrada HTTP autorizado a `mark_treasury_execution_failed()` — gateado por permiso `simulator.can_recover_treasury_execution` + TOTP (O.4a/O.4d) |
| Dashboard operacional Treasury | `/admin/simulator/treasuryoperationrequest/operational-dashboard/` (`admin:treasury_operational_dashboard`) | Vista read-only de todo `EXECUTING` actual clasificado por caso (CASE_A...F), edad, elegibilidad, y el historial de `EV_TREASURY_STUCK_EXECUTION_OBSERVED` ya persistido |
| `observe_stuck_treasury_executions()` | `simulator/broker_audit.py` | Tarea periódica Beat (`observe-treasury-stuck-executions-15m`, cada 15 min) que escribe `EV_TREASURY_STUCK_EXECUTION_OBSERVED` en `BrokerAuditEvent` (dedup 900s) |
| `/api/health/` | público | Liveness trivial: `{"status": "ok"}` |
| `/api/health/detail/` | staff + TOTP | Ping real a DB y Redis; **además (O.4e-3)** sección `celery_beat: {status: fresh\|stale\|missing, age_seconds, threshold_seconds: 900}` — `stale` degrada a HTTP 503 igual que un fallo de DB/Redis, `missing` se reporta pero no degrada (evita falso positivo justo tras un deploy) |
| `/api/metrics/` | staff + TOTP | Contadores DB, stats Redis, `task_failures` (últimos 100 fallos de tareas Celery), `celery.workers_online` (ping a workers — **no verifica Beat**; para eso usar `/api/health/detail/`'s `celery_beat`) |
| `record_celery_beat_heartbeat()` / `inspect_celery_beat_heartbeat_staleness()` | `simulator/broker_audit.py` (O.4e-2) | Tarea periódica Beat (`record-celery-beat-heartbeat-5m`, cada 5 min) que persiste `EV_CELERY_BEAT_HEARTBEAT`; función de lectura pura que calcula fresh/stale/missing contra el umbral de 900s, leyendo solo PostgreSQL (independiente de Redis) |
| `observe_treasury_stuck_execution_escalations()` | `simulator/broker_audit.py` (O.4e-4) | Llamada por el mismo task que la observación base (`observe_treasury_stuck_executions_task`, en ese orden); escribe `EV_TREASURY_STUCK_EXECUTION_ESCALATED` (severidad CRITICAL, vía `logger.error()`) cuando un candidato lleva ≥2700s persistentemente observado, con dedup de 3600s |
| `deploy/scripts/healthcheck.sh` | script | `curl /api/health/` + `systemctl is-active` de `daphne`/`celery-worker`/`celery-beat` + `redis-cli ping` + `psql SELECT 1` |
| `deploy/scripts/backup_postgres.sh` / `restore_postgres.sh` | scripts | Backup/restore `pg_dump`/`pg_restore` ya existentes. `backup_postgres.sh` (O.5a/O.5b) escribe `backup_success.json`/`backup_failure.json` bajo `BACKUP_METADATA_PATH` — reflejado en `/api/health/detail/`'s sección `backup` — y usa `flock` no bloqueante para rechazar limpiamente una segunda ejecución concurrente (manual o del timer) sin tocar la metadata existente |
| `backup-postgres.timer` (O.5b) | systemd, `deploy/systemd/backup-postgres.{service,timer}` | Dispara `backup_postgres.sh` diariamente a las 03:00 UTC — independiente de Redis/Celery/Daphne por diseño, corre aunque esos servicios estén caídos |
| Servicios systemd | `deploy/systemd/{daphne,celery-worker,celery-beat,backup-postgres}.service` | Nombres reales de servicio: `daphne`, `celery-worker`, `celery-beat`, `backup-postgres` |

---

## 1. TreasuryOperationRequest EXECUTING atascado

**Síntoma:** una solicitud queda en estado `EXECUTING` más tiempo del esperado (el sistema ya considera "candidato" a partir de `TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS=600s`, es decir, 10 minutos).

**Diagnóstico:**
1. Ir a `/admin/simulator/treasuryoperationrequest/operational-dashboard/`.
2. Cada fila muestra `case` (CASE_A a CASE_F), `age_seconds`/`age_display`, `eligible`, `block_reason`. Significado de cada caso:
   - **CASE_A** — limpio, superó el umbral de edad, candidato elegible.
   - **CASE_B** — ⚠ ya tiene un `WalletTransaction` vinculado — anomalía estructural, **nunca elegible** (investigar antes de tocar nada).
   - **CASE_C** — todavía por debajo del umbral de edad, posiblemente en curso legítimo (no se muestra como observación en el historial).
   - **CASE_D** — edad desconocida (no se encontró evento `EXECUTION_STARTED`).
   - **CASE_E** — ⚠ ya existe un evento `EXECUTED`/`FAILED` — inconsistencia de auditoría.
   - **CASE_F** — `executed_by` ausente o inactivo (informativo, no bloquea por sí solo la elegibilidad).
3. El historial de la misma pantalla muestra las observaciones `EV_TREASURY_STUCK_EXECUTION_OBSERVED` ya escritas por la tarea periódica (cada 15 min, dedup 900s) — confirma desde cuándo el sistema mismo detectó el problema.

**Acción autorizada (única):**
1. Confirmar que el caso es CASE_A (o CASE_D/CASE_F si el análisis manual confirma que es seguro) y que `eligible=True`.
2. Usar el botón/flujo de "Recover" en el detalle de la solicitud, que llama a `/admin/simulator/treasuryoperationrequest/<pk>/recover/` → `mark_treasury_execution_failed()`.
3. Proporcionar un `recovery_reason` real y específico (queda auditado verbatim).
4. Requiere: usuario autenticado, permiso `simulator.can_recover_treasury_execution`, TOTP verificado, y **no puede ser** el mismo usuario que solicitó (`requested_by`) o aprobó (`approved_by`) la operación.

**Resultado garantizado por el propio mecanismo:** la solicitud pasa a `FAILED`. Nunca revive dinero, nunca crea o toca un `WalletTransaction`, nunca vuelve a `APPROVED`.

**Prohibido:**
- Editar `status` directamente en el admin (campo de solo lectura por diseño) o vía shell/SQL.
- Reintentar la ejecución original.
- Intentar recuperar un CASE_B o CASE_E sin antes investigar la inconsistencia subyacente — recuperar sobre una anomalía estructural puede ocultar un problema más grave.

---

## 2. Audit trail stale

**Diagnóstico primario (O.4e-2/3, automatizado):**
```
GET /api/health/detail/    (staff + TOTP)
```
Revisar el campo `celery_beat`:
- `"status": "fresh"` — Beat está latiendo con normalidad (último heartbeat hace ≤900s).
- `"status": "stale"` — Beat dejó de latir hace más de 900s (`age_seconds` muestra hace cuánto). El endpoint entero responde `"status": "degraded"` + HTTP 503, igual que un fallo de DB/Redis — tratar como incidente activo, seguir a la Sección 4 (Beat caído).
- `"status": "missing"` — nunca se registró un heartbeat (típico en los primeros ~5 minutos tras un deploy nuevo; si persiste más allá de eso, tratar como `stale`). **No degrada** el status global por diseño (evita falso positivo justo después de desplegar).

Este campo se calcula desde el heartbeat persistente en `BrokerAuditEvent` (PostgreSQL) — sigue siendo legible aunque Redis esté caído (ver Sección 5).

**Diagnóstico secundario / manual (sigue siendo válido):**
1. `GET /api/metrics/` (staff + TOTP) — revisar `celery.workers_online` (¿hay al menos un worker respondiendo al ping?) y `task_failures.last_10` (¿hay fallos recientes de tareas relacionadas con auditoría?).
2. Verificar en shell (`python manage.py shell`) el timestamp del `BrokerAuditEvent` más reciente de un tipo que se escribe con cadencia fija (no orgánica):
   ```python
   from simulator.models import BrokerAuditEvent
   from simulator import broker_audit as _audit
   BrokerAuditEvent.objects.filter(
       event_type=_audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED
   ).order_by("-timestamp").first()
   ```
   Si la tarea `observe-treasury-stuck-executions-15m` está viva, deberías ver observaciones recientes **siempre que haya al menos un candidato EXECUTING que las genere** — su ausencia no es concluyente por sí sola si no hay candidatos.
3. Cruzar con `deploy/scripts/healthcheck.sh` (ver Secciones 3/4) para confirmar si Beat/worker realmente están activos a nivel de proceso.

**Importante — no confundir dos preguntas distintas** (precisión ya aprobada en Fase 0):
- ¿Celery Beat está vivo? → Sección 4.
- ¿Los writers (`record_event()`/`log_audit()`) están escribiendo correctamente? → revisar logs de `simulator.security`/`simulator.broker_audit` en busca de líneas `[ratelimit] Redis unavailable...` o excepciones atrapadas silenciosamente (ambas funciones son fail-open por contrato — nunca lanzan, solo loguean).

**Acción autorizada:** ninguna mutación — esta sección es puramente diagnóstica. Si el diagnóstico apunta a Beat/worker/Redis/Postgres caídos, seguir la sección correspondiente.

---

## 3. Celery worker caído

**Diagnóstico:**
```bash
systemctl status celery-worker
journalctl -u celery-worker -n 100 --no-pager
```
Confirmar también vía `/api/metrics/` → `celery.workers_online` (0 = ningún worker responde) y el ring buffer `task_failures`.

**Acción autorizada:**
```bash
sudo systemctl restart celery-worker
```
Verificar recuperación con `deploy/scripts/healthcheck.sh` o repitiendo el chequeo de `/api/metrics/`.

**Prohibido:** matar procesos Celery manualmente con `kill -9` en lugar de `systemctl` (rompe el `KillMode=mixed`/`SIGTERM` graceful shutdown ya configurado en `celery-worker.service`, pudiendo dejar tareas a medias).

---

## 4. Celery Beat caído

**Diagnóstico:**
```bash
systemctl status celery-beat
journalctl -u celery-beat -n 100 --no-pager
```

**Acción autorizada:**
```bash
sudo systemctl restart celery-beat
```

**⚠ Advertencia crítica (ya documentada en `deploy/systemd/celery-beat.service`):** debe existir **exactamente una** instancia de Beat activa. El scheduler usa `redbeat.RedBeatScheduler` con estado en Redis (prefijo `trx:beat:`) y un lock distribuido (`REDBEAT_LOCK_TIMEOUT=300s`). Dos Beat corriendo simultáneamente no rompen el lock, pero sí producen *scheduling jitter* (tareas duplicadas o desalineadas). Por esto mismo, el propio `.service` fija `RestartSec=30s` — **no reducir este valor** ni lanzar un segundo proceso Beat manualmente "para probar" mientras el primero podría seguir vivo.

Antes de reiniciar, confirmar que no hay ya un proceso Beat corriendo en otra instancia/host:
```bash
ps aux | grep "celery.*beat" | grep -v grep
```

---

## 5. Redis caído

Redis en este proyecto cumple **cuatro roles simultáneos** (`REDIS_URL` en `trx_simulator/settings.py`): broker y result backend de Celery (`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`), estado del scheduler Beat (`REDBEAT_REDIS_URL`), channel layer de Django Channels/WebSockets (`channels_redis.core.RedisChannelLayer`), y contadores de rate limiting/observabilidad (`simulator/ratelimit.py`, `simulator/observability.py`).

**Qué se degrada (según el código actual, no supuestos):**
- **Celery worker y Beat quedan efectivamente inoperantes** — Redis es su broker; no pueden despachar ni programar tareas mientras esté caído (`CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP=True` evita que el proceso *crashee* al arrancar, pero no repara la conectividad).
- **WebSockets/precios en tiempo real dejan de funcionar** — el channel layer es Redis-backed en este despliegue (cae a `InMemoryChannelLayer` solo si `REDIS_URL` está vacío, es decir, solo en desarrollo sin Redis configurado).
- **Rate limiting (O.4d) falla abierto** — `rate_check()`/`rate_peek()` devuelven valores que nunca bloquean login/TOTP (ver `simulator/ratelimit.py` — comportamiento verificado explícitamente en O.4d-3).
- **`task_failures` ring buffer y contador de WebSockets** dejan de actualizarse (viven en Redis).
- **`/api/health/detail/`** reporta `redis: {"status": "error"}` → HTTP 503.

**Qué sigue funcionando:**
- **Autenticación y TOTP** — el requisito de 2FA (`totp_session_verified()`) es 100% basado en sesión de Django (backend de sesión en DB por defecto), sin dependencia de Redis — confirmado explícitamente en O.4d-3.
- **Lectura/escritura de `BrokerAuditEvent`/`AuditLog`** — viven en PostgreSQL, no en Redis.
- **El sitio HTTP normal (Daphne)** sigue respondiendo para todo lo que no dependa de Redis.

**Acción autorizada:** verificar el proceso Redis (`redis-cli -h <host> -p <port> ping`, o `systemctl status redis` si corre como servicio del sistema) y reiniciarlo según el procedimiento estándar de la infraestructura. Tras recuperar Redis, reiniciar `celery-beat` y `celery-worker` (Sección 3/4) para asegurar que retoman el schedule limpiamente en vez de quedar en un estado de reconexión ambiguo.

---

## 6. PostgreSQL degradado/caído

**Diagnóstico:**
```bash
systemctl status postgresql
psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1"
```
(mismo chequeo que hace `deploy/scripts/healthcheck.sh`).

**Acción autorizada — degradación (lentitud, no caída total):** investigar a nivel de infraestructura (conexiones, locks, espacio en disco) antes de cualquier acción destructiva. `record_event()`/`log_audit()` son fail-open — una DB lenta puede manifestarse como audit trail incompleto sin que el resto de la aplicación falle visiblemente (ver Sección 2).

**Acción autorizada — caída total / corrupción, restauración necesaria:**
Usar exclusivamente `deploy/scripts/restore_postgres.sh <dump_file>`, que ya:
1. Verifica la integridad del dump (`pg_restore --list`) antes de tocar la base.
2. Exige confirmación explícita (`Type YES to continue`).
3. Detiene `daphne`, `celery-worker`, `celery-beat` antes de restaurar (evita escrituras a mitad de restauración).
4. Ejecuta `pg_restore --clean --if-exists`.
5. Reinicia los tres servicios en orden (`celery-beat` → `celery-worker` → `daphne`).
6. Corre `deploy/scripts/healthcheck.sh` al final automáticamente.

**Antes de restaurar:** correr `deploy/scripts/backup_postgres.sh` sobre el estado actual (aunque esté degradado) — el propio script de restore no lo hace por ti, y perder el estado pre-incidente sin intentarlo primero elimina evidencia forense.

**Prohibido:** improvisar un procedimiento de restauración manual con `pg_restore`/`psql` fuera de `restore_postgres.sh`, o restaurar sin haber verificado primero la integridad del dump.

---

## 7. Recuperación manual autorizada (resumen)

La **única** acción de recuperación con efecto sobre el estado de negocio Treasury que este runbook autoriza es la ya descrita en la Sección 1: `mark_treasury_execution_failed()` vía `/admin/simulator/treasuryoperationrequest/<pk>/recover/`. No existe ningún otro mecanismo autorizado (ni CLI, ni shell, ni SQL directo) para mutar el estado de un `TreasuryOperationRequest` durante un incidente.

Para incidentes de infraestructura, la recuperación autorizada es siempre a través de los scripts/servicios ya inventariados en la Sección 0 — nunca comandos ad-hoc inventados en el momento.

---

## 8. Escalamiento del incidente

**Escalamiento automático (O.4e-4):** si un `TreasuryOperationRequest` permanece `EXECUTING` y observado como atascado (CASE_A/B/D/E/F) durante ≥2700s (45 min) de forma persistente, el sistema escribe `EV_TREASURY_STUCK_EXECUTION_ESCALATED` (severidad CRITICAL) y emite `logger.error(...)` — lo que llega a Sentry automáticamente si `SENTRY_DSN` está configurado, vía la `LoggingIntegration(event_level=ERROR)` ya existente (sin llamada directa a `sentry_sdk.capture_message()`). Con dedup de 3600s: no vuelve a escalar antes de una hora mientras el mismo request siga atascado, y sí vuelve a hacerlo si sigue sin resolverse pasada esa hora. **Esto es puramente observacional — nunca ejecuta, reintenta ni recupera nada automáticamente.** La única acción con efecto real sigue siendo la Sección 1, y sigue siendo enteramente humana.

CASE_C (aún dentro del umbral de 600s) nunca escala, por diseño — nunca llega a observarse en primer lugar.

**Al escalar, incluir siempre:**
- El(los) `treasury_operation_request_id` afectado(s) y su `case`/`age_seconds` (desde el dashboard operacional).
- El `event_id` (UUID) de cualquier `BrokerAuditEvent` relevante (`EV_TREASURY_STUCK_EXECUTION_OBSERVED`, `EV_TREASURY_STUCK_EXECUTION_ESCALATED`, `EV_TREASURY_REQUEST_EXECUTION_FAILED`, `EV_TREASURY_REQUEST_EXECUTION_RECOVERY_BLOCKED`, etc.).
- Resultado de `deploy/scripts/healthcheck.sh` y de `/api/metrics/` al momento del incidente.
- Cualquier línea de log de `journalctl -u celery-worker`/`celery-beat` relevante.

**Quién puede actuar:** únicamente un usuario que ya posee `simulator.can_recover_treasury_execution` y TOTP verificado — no crear ni elevar permisos "para resolver más rápido" durante un incidente; eso es exactamente el tipo de atajo que O.4b/O.4d existen para impedir.

---

## 9. Evidencia que debe preservarse

Dos sistemas de auditoría con **retención distinta** — no son intercambiables como fuente de evidencia:

| Sistema | Retención | Uso como evidencia |
|---|---|---|
| **`BrokerAuditEvent`** | **Sin poda automática** — no hay ninguna tarea de limpieza programada para este modelo | Fuente de evidencia durable por defecto para cualquier incidente Treasury/audit-trail |
| **`AuditLog`** | **30 días** (`cleanup_audit_log_task`, diario 02:00 UTC, `retention_days=30`) | Útil para el contexto HTTP-scoped del incidente (login, IP, endpoint) pero **se borra a los 30 días** |

**Acción obligatoria si la investigación puede extenderse más allá de 30 días:** exportar (`AuditLog.objects.filter(...).values(...)` a un archivo, o un dump de la tabla) las filas de `AuditLog` relevantes al incidente **antes** de que `cleanup_audit_log_task` las alcance. `BrokerAuditEvent` no requiere esta acción — permanece indefinidamente.

Conservar también: el dump de `backup_postgres.sh` tomado antes de cualquier restauración (Sección 6), y los logs de `journalctl` citados en el escalamiento (Sección 8) — estos sí rotan según la configuración de journald del sistema.

---

## 10. Acciones expresamente prohibidas

- Editar `TreasuryOperationRequest.status` (o cualquier campo relacionado con el estado de ejecución) directamente en el admin, shell o SQL.
- Ejecutar o reintentar manualmente la lógica de ejecución de Treasury fuera de `mark_treasury_execution_failed()`.
- Restaurar PostgreSQL sin usar `restore_postgres.sh`, o sin haber corrido `backup_postgres.sh` sobre el estado actual primero.
- Ejecutar una segunda instancia de `celery-beat` mientras la primera podría seguir viva.
- Matar procesos Celery con `kill -9` en lugar de `systemctl stop`/`restart`.
- Borrar filas de `BrokerAuditEvent` bajo cualquier circunstancia.
- Otorgar o elevar permisos Treasury/superuser durante el incidente para "agilizar" la recuperación.
- Deshabilitar o bypassear el gate TOTP (O.4a/O.4d) para acelerar el acceso administrativo.
- Actuar sobre un candidato CASE_B o CASE_E (anomalía estructural / inconsistencia de auditoría) sin investigar la causa raíz primero.

---

## 11. Criterios de cierre del incidente

Un incidente Treasury/audit-trail se considera cerrado cuando **todos** los siguientes se cumplen:

1. **Estado de negocio resuelto**: cualquier `TreasuryOperationRequest` involucrado alcanzó un estado terminal (`FAILED` vía recuperación autorizada, o se confirmó que el `EXECUTING` era legítimo y en curso — CASE_C).
2. **Infraestructura restaurada**: `deploy/scripts/healthcheck.sh` pasa completo (daphne, celery-worker, celery-beat activos; Redis responde PONG; PostgreSQL acepta conexiones).
3. **Beat confirmado operativo**: `GET /api/health/detail/` muestra `celery_beat.status: "fresh"` (O.4e-2/3, ya implementado) tras la recuperación — no solo se infiere de logs.
4. **Evidencia preservada**: conforme a la Sección 9, incluyendo el `recovery_reason` registrado en cualquier `mark_treasury_execution_failed()` ejecutado.
5. **Nota de cierre registrada**: un resumen humano del incidente (causa, acción tomada, `event_id`s relevantes) documentado fuera de este runbook (ej. ticket/registro operativo del equipo) — este runbook no prescribe una herramienta específica de tracking porque ninguna existe todavía en este repositorio.
