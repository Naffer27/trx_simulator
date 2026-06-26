# Staging Readiness & Security/Incident Inventory — K.4

| Campo        | Valor                                            |
|--------------|--------------------------------------------------|
| Fecha        | 2026-06-25                                       |
| Commit base  | `d56988f` money-broker-ready-for-staging-v1      |
| Basado en    | Smoke test K.3.2 (24 PASS, 1 COND, 1 SKIP)      |
| Entorno base | Local SQLite + InMemoryChannelLayer              |

---

## Arquitectura Staging Recomendada

```
Internet
  │
  ├─ 443/80 → Nginx (TLS, static files, WS upgrade)
  │              │
  │              └─ 127.0.0.1:8001 → Daphne (ASGI, trx_sim user)
  │
  ├─ Redis 127.0.0.1:6379 (channels + celery + redBeat + ratelimit)
  │   └─ AOF + RDB persistence
  │
  ├─ PostgreSQL 127.0.0.1:5432 (trx_sim_staging DB)
  │   └─ pg_dump diario → /var/backups/trx_sim/ + offsite S3/R2
  │
  ├─ Celery Worker (systemd, 4 concurrentes)
  └─ Celery Beat  (systemd, redBeat scheduler)

Todo en una VPS 2CPU/4GB — costo ~$20–25/mes (Hetzner CX22 o DO Basic)
```

Archivos de deploy listos: `deploy/nginx/trx_sim.conf`, `deploy/systemd/daphne.service`,
`deploy/systemd/celery-worker.service`, `deploy/systemd/celery-beat.service`,
`deploy/scripts/deploy.sh`, `deploy/scripts/healthcheck.sh`, `deploy/logrotate/trx_sim`.

---

## Arquitectura Producción Inicial Recomendada

```
Internet
  │
CDN (Cloudflare free) — static assets, DDoS básico
  │
  ├─ 443/80 → Nginx → Daphne (4CPU/16GB VPS)
  │
  ├─ Managed PostgreSQL (Supabase / Neon / DO Managed PG)
  │   └─ Backups automáticos offsite incluidos
  │
  ├─ Redis (VPS local o Upstash serverless)
  │   └─ Redis AUTH obligatorio
  │
  ├─ Celery Worker + Beat (misma VPS)
  │
  └─ Email: SendGrid / Mailgun via anymail — no Gmail SMTP
```

---

## A. Variables .env para Staging

### Presentes en código, ausentes o incorrectas en `deploy/.env.staging.template`

| Variable | Severidad | Observación |
|----------|-----------|-------------|
| `DOMAIN` | **Critical** | Settings usa `DOMAIN` para `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS`. El template usa `ALLOWED_HOSTS=staging...` directamente — inconsistente con el código |
| `SITE_URL` | **Critical** | Links de email (verificación, activación challenge) usan `SITE_URL`. Sin esto apuntan a `http://127.0.0.1:8000` |
| `TOTP_STAFF_REQUIRED` | **Critical** | Template tiene `False`. Debe ser `True` antes de staging |
| `TOTP_ENCRYPTION_KEY` | **Critical** | Template tiene `CHANGE_ME_FERNET_KEY`. Sin generar = TOTP secrets en base64 plano |
| `CHALLENGE_WEBHOOK_SECRET` | **Critical** | Sin esto todos los webhooks externos son rechazados |
| `NOWPAYMENTS_EMAIL` | **High** | Necesario para JWT de payouts. Sin esto las retiradas fallan silenciosamente |
| `NOWPAYMENTS_PASSWORD` | **High** | Ídem |
| `CHALLENGE_STATUS_API_TOKEN` | **High** | Sin esto todas las requests de estado de challenge retornan 401 |
| `ADMIN_URL` | **High** | Template no tiene esta variable → defaultea a `admin/` — debe randomizarse |
| `SECURE_SSL_REDIRECT` | **High** | Debe ser `True` en staging tras verificar SSL |
| `DEFAULT_FROM_EMAIL` | **Medium** | No en template → defaultea a `noreply@moneybrokers.app` |
| `SUPPORT_EMAIL` | **Medium** | No en template |
| `FINNHUB_API_KEY` | **Medium** | En template pero vacío — sin esto el price feed cae a simulado |
| `REVENUE_SNAPSHOT_RETENTION_DAYS` | **Low** | Defaults a 90 días — aceptable |
| `MAX_WITHDRAWAL_DAILY_USD` | **Low** | Defaults a $1500 — confirmar antes de beta |
| `SECURE_HSTS_SECONDS` | **Low** | No en staging template — Nginx ya añade HSTS pero Django no |

### Variables OK — presentes y correctamente documentadas

`DJANGO_SECRET_KEY`, `DEBUG`, `DB_NAME/USER/PASSWORD/HOST/PORT`, `REDIS_URL`, `LOG_JSON`,
`BROKER_ACCESS_CODE`, `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`,
`NOWPAYMENTS_WEBHOOK_URL`, `EMAIL_HOST/*`, `SENTRY_DSN`, `SNAPSHOT_RETENTION_DAYS`,
`LOAD_TEST_MODE`.

### Gap crítico — `anymail` no está en `requirements.txt`

`.env.example` documenta usar `anymail.backends.sendgrid.EmailBackend` como opción de
producción, pero `anymail` no aparece en `requirements.txt`. Si staging usa
SendGrid/Mailgun/Postmark, el `pip install` fallará.

---

## B. Servicios Requeridos

| Servicio | Estado | Archivo | Observación |
|----------|--------|---------|-------------|
| PostgreSQL | **OK** | `settings.py:109`, `DEPLOY.md` | Config completa, CONN_MAX_AGE, health checks |
| Redis | **OK** | `settings.py:188`, `deploy/redis_persistence.conf` | AOF + RDB configurados |
| Daphne/ASGI | **OK** | `deploy/systemd/daphne.service` | Bind 127.0.0.1:8001, systemd hardened |
| Celery worker | **OK** | `deploy/systemd/celery-worker.service` | concurrency=4, restart on failure |
| Celery beat | **OK** | `deploy/systemd/celery-beat.service` | redBeat, lock timeout correcto |
| Nginx | **OK** | `deploy/nginx/trx_sim.conf` | HTTP→HTTPS, WS upgrade, static files |
| SSL/Certbot | **OK** | `DEPLOY.md:131` | Documentado para instalación inicial |
| **Certbot auto-renewal** | **Gap** | `DEPLOY.md` | Renovación automática no verificada en runbook. Certbot instala `certbot.timer` systemd por defecto — debe verificarse con `systemctl status certbot.timer` |
| collectstatic | **OK** | `deploy/scripts/deploy.sh:46` | Incluido en deploy.sh |
| **Media files (Nginx)** | **Gap** | `deploy/nginx/trx_sim.conf` | `/media/` no está en el config de Nginx. KYC docs (`kyc/documents/`, `kyc/selfies/`) no se sirven |
| logrotate | **OK** | `deploy/logrotate/trx_sim` | 14 días, daphne + nginx |
| systemd (todos) | **OK** | `deploy/systemd/` | 3 servicios, NoNewPrivileges, PrivateTmp |

---

## C. Seguridad Staging

| Item | Estado | Severidad | Config | Acción |
|------|--------|-----------|--------|--------|
| `DEBUG=False` | **OK** | Critical | `settings.py:32` | Configurar en .env |
| `SESSION_COOKIE_SECURE` | **OK** | Critical | `settings.py:452` | Auto: `not DEBUG` |
| `CSRF_COOKIE_SECURE` | **OK** | Critical | `settings.py:453` | Auto: `not DEBUG` |
| `CSRF_COOKIE_SAMESITE=Lax` | **OK** | High | `settings.py:454` | Hardcoded |
| `SECURE_PROXY_SSL_HEADER` | **OK** | High | `settings.py:448` | Hardcoded para Nginx |
| `X_FRAME_OPTIONS=DENY` | **OK** | High | middleware + Nginx | Doble protección |
| `SECURE_CONTENT_TYPE_NOSNIFF` | **OK** | High | Django default | True |
| Webhook HMAC-SHA512 | **OK** | Critical | `views.py:1657` | Verificado antes de cualquier DB read |
| `NOWPAYMENTS_IPN_SECRET` guard | **OK** | Critical | `settings.py:490` | ImproperlyConfigured si vacío en prod |
| `EMAIL_HOST` guard | **OK** | High | `settings.py:413` | ImproperlyConfigured si smtp+vacío |
| Rate limiting HTTP | **OK** | High | `simulator/ratelimit.py` | Login: 8/5min, Redis-based, fail-open |
| Rate limiting WS | **OK** | High | `consumers.py:717` | Órdenes rate limitadas |
| Secrets fuera del repo | **OK** | Critical | `.gitignore` | .env excluido |
| .env chmod 600 | **OK** | High | `DEPLOY.md:75` | Documentado |
| Daphne bind 127.0.0.1 | **OK** | Critical | `daphne.service:27` | No expuesto públicamente |
| Redis bind 127.0.0.1 | **OK** | High | `.env.staging.template:29` | No expuesto |
| Postgres bind 127.0.0.1 | **OK** | High | Template | No expuesto |
| `TOTP_STAFF_REQUIRED=True` | **Gap** | **Critical** | Template tiene `False` | Cambiar a `True` en staging .env |
| `TOTP_ENCRYPTION_KEY` set | **Gap** | **Critical** | Template tiene `CHANGE_ME` | Generar Fernet key |
| `ADMIN_URL` randomizado | **Gap** | **High** | Default `admin/` | Configurar URL no predecible |
| `SECURE_SSL_REDIRECT=True` | **Gap** | **High** | `False` por defecto | Añadir a staging .env tras SSL verificado |
| HSTS consistente | **Gap** | **High** | Nginx: 15768000s / Django: 0 | Elegir un solo actor para HSTS. Hoy Nginx lo emite pero Django no. Hay que alinear |
| Redis AUTH password | **Gap** | **Medium** | No en template | Añadir si otros procesos en VPS pueden conectar a 127.0.0.1:6379 |
| Media files en Nginx | **Gap** | **High** | Nginx no sirve `/media/` | Añadir location block para KYC |
| `anymail` en requirements | **Gap** | **High** | No está | Añadir o documentar proveedor SMTP definitivo |
| CSP (Content-Security-Policy) | **Gap** | **Medium** | No configurado | Añadir en Bloque L o posterior |
| Bloqueo de cuenta tras X fallos | **Gap** | **Medium** | No implementado | Considerar para producción real |
| Notificación de login IP nueva | **Gap** | **Low** | No implementado | Futuro — opcional para beta |

---

## D. Backups / Recuperación

| Qué | Estado | Frecuencia recomendada | Observación |
|-----|--------|------------------------|-------------|
| PostgreSQL dump | **OK** | Diario 03:00 UTC, 14 copias | `deploy/scripts/backup_postgres.sh` + cron documentado en `DEPLOY.md` |
| Media files (KYC) | **Gap** | Diario | KYC docs no incluidos en backup. Pérdida irreversible si VPS muere |
| `.env` | **Gap** | Ante cada cambio | No documentado. Guardar en gestor de secretos (1Password, Vault, Bitwarden) |
| Redis (AOF+RDB) | **OK** | Automático ~1s durabilidad | `deploy/redis_persistence.conf` configurado |
| Redis backup offsite | **Gap** | Diario | Solo local. Redis data es mayormente efímero (Celery queues, métricas) — aceptable perder |
| Logs | **OK** | logrotate 14 días | `/var/log/trx_sim/` + `/var/log/nginx/` |
| **Off-site backup** | **Gap Critical** | Diario | Backups de PG están en `/var/backups/trx_sim/` — misma VPS. Si el disco muere o el proveedor elimina la VPS, todo se pierde. Necesita S3 / Hetzner Object Storage / Cloudflare R2 |
| Snapshot VPS | **Gap** | Semanal | No documentado. Proveedor VPS ofrece snapshots automáticos (Hetzner: €0.011/GB/mes, DO: 20% del costo del droplet) |
| Restore drill | **OK** | Antes de producción | `deploy/scripts/restore_postgres.sh` existe con pre-flight integrity check |

### Escenario: el VPS muere completamente

| Componente | Sin off-site backup | Con off-site backup |
|------------|--------------------|--------------------|
| Código | Recuperable (GitHub) | Recuperable (GitHub) |
| PostgreSQL | **Perdido** | Recuperable (último dump) |
| Media/KYC | **Perdido** | Recuperable si incluido en backup |
| .env secrets | **Perdido** — rotar todo | Recuperable si en gestor de secretos |
| Redis data | Efímero — aceptable perder | Efímero — aceptable perder |

---

## E. Monitoreo / Logs

| Qué | Estado | Endpoint / Ubicación |
|-----|--------|----------------------|
| Health check público | **OK** | `GET /api/health/` → `{"status":"ok"}` |
| Health detail staff | **OK** | `GET /api/health/detail/` — DB + Redis latency, staff-only |
| Métricas operacionales | **OK** | `GET /api/metrics/` (staff) |
| Broker monitoring | **OK** | `GET /api/broker/monitoring/` (staff) |
| Ops panel web | **OK** | `https://domain/staff/ops/` |
| Sentry errores | **Needs Config** | `SENTRY_DSN` vacío en template — integración lista, solo falta el DSN |
| Celery heartbeat | **OK** | Task `beat-heartbeat-5m` cada 5 min — confirma beat + worker vivos |
| Django logs JSON | **OK** | `LOG_JSON=true` → stdout → journald |
| Logs Celery | **OK** | journald + `/var/log/trx_sim/celery-worker.log`, `celery-beat.log` |
| Logs Nginx | **OK** | `/var/log/nginx/trx_sim.access.log` + `trx_sim.error.log` |
| Logs webhook NowPayments | **OK** | `simulator.security` logger — sig verification logueada |
| Logs wallet credit | **OK** | `wallet_ledger.py` — cada credit logueado |
| Logs challenge activation | **OK** | Enrollment flow logueado |
| Logs withdrawals/payouts | **OK** | `payout_cb` logger en `views.py` |
| **Uptime monitor externo** | **Gap** | No configurado. UptimeRobot / Betterstack / Freshping — gratis para 1 URL |
| **Alerta Celery muerto** | **Gap** | Heartbeat existe pero nadie lo monitorea externamente. SL/TP, stopouts y reconciliación se pausan silenciosamente si Celery cae |
| **`pg_stat_statements`** | **Gap** | Mencionado en `DEPLOY.md` pero sin instrucción de instalación del extension |
| **Alerta errores NowPayments** | **Gap** | Sin Sentry DSN configurado, errores de pago no generan notificación |

---

## F. Escenarios de Incidente / Hackeo

| Escenario | Defensa actual | Gap / Acción requerida |
|-----------|---------------|------------------------|
| **F-1 Usuario hackeado** | Rate limit login (8/5min), `security_log auth.login_failed`, TOTP disponible (opt-in) | Sin bloqueo automático de cuenta; sin alerta de login desde nueva IP; 2FA no obligatorio para usuarios regulares |
| **F-2 Staff hackeado** | TOTP_STAFF_REQUIRED (cuando True), AuditLog en operaciones críticas, `disable_2fa` management command | `TOTP_STAFF_REQUIRED=False` en template de staging — **activar antes de subir** |
| **F-3 .env filtrado** | .env en .gitignore, chmod 600 documentado, secrets aleatorios con instrucciones de generación | Sin rotation procedure documentada. Si se filtra `SECRET_KEY`: invalidar sesiones activas, rotar todos los secrets, regenerar Fernet key |
| **F-4 VPS comprometido** | System user sin login shell, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` en systemd | Sin containment playbook (quién notificar, pasos de isolation, forensics mínimos) |
| **F-5 Webhook falso (NowPayments)** | HMAC-SHA512 verificado antes de cualquier DB read; `ImproperlyConfigured` si `IPN_SECRET` vacío en prod | **OK** — bien protegido |
| **F-6 Redis expuesto** | 127.0.0.1 binding en template | Sin AUTH password — si otros procesos en la VPS conectan a 127.0.0.1:6379 pueden leer queues y métricas |
| **F-7 PostgreSQL expuesto** | 127.0.0.1 binding | `pg_hba.conf` no documentado explícitamente — verificar que rechaza conexiones externas |
| **F-8 Base de datos borrada** | `backup_postgres.sh` + `restore_postgres.sh` | Backups locales en misma VPS — si la VPS muere, se pierden. Off-site backup es bloqueante |
| **F-9 NowPayments falla** | `reconcile-deposits-15m` + `reconcile-withdrawals-15m` en Celery beat | Sin alerta cuando NowPayments retorna errores repetidos — necesita Sentry DSN configurado |
| **F-10 Celery detenido** | systemd `Restart=on-failure`, heartbeat task cada 5 min | Sin alerta externa si beat-heartbeat-5m deja de ejecutarse. SL/TP, stopouts y reconciliación se pausan silenciosamente |
| **F-11 Daphne caído** | systemd `Restart=on-failure`, `StartLimitBurst=3` | Nginx sirve 502 Bad Gateway sin página de error amigable. Sin alerta externa |
| **F-12 Nginx caído** | systemd gestionado | Sin sorry page; sin alerta externa; HTTP y HTTPS dejan de responder |
| **F-13 SSL vencido** | Certbot instalado + Nginx config listo | Certbot instala `certbot.timer` systemd por defecto — verificar con `systemctl status certbot.timer`. No está en DEPLOY.md |

---

## G. Capacidad VPS Recomendada

| Entorno | CPU | RAM | Disco | Coste estimado | Notas |
|---------|-----|-----|-------|---------------|-------|
| **Staging** | 2 vCPU | 4 GB | 40 GB SSD | ~$20–25/mes | Hetzner CX22 / DO Basic. Todos los servicios en una VPS |
| **Beta 10 usuarios** | 2 vCPU | 4 GB | 40 GB SSD | ~$20–25/mes | Puede compartir VPS con staging usando `BROKER_ACCESS_CODE` |
| **Beta 100 usuarios** | 4 vCPU | 8 GB | 80 GB SSD | ~$40–60/mes | Considerar separar Postgres en managed DB ($15–25/mes adicional) |
| **Producción inicial** | 4–8 vCPU | 16 GB | 160 GB SSD | ~$80–120/mes | Managed Postgres + Redis recomendados; CDN para estáticos |

**Bottleneck esperado en producción inicial:** WebSocket connections — una por usuario activo en
trading. Daphne/Twisted maneja miles; el límite real es RAM de Django + Redis capacity.

---

## Bloqueantes — Antes de montar VPS (staging)

Estos 9 items deben estar resueltos antes de hacer `deploy.sh` en el VPS:

1. **`TOTP_STAFF_REQUIRED=True`** en `.env` de staging (identificado en K.3.2 F-3)
2. **`TOTP_ENCRYPTION_KEY`** generado y configurado — sin esto los TOTP secrets quedan en base64 plano
3. **`DOMAIN` + `SITE_URL`** en staging `.env` — sin `SITE_URL` los links de email apuntan a localhost
4. **`CHALLENGE_WEBHOOK_SECRET` + `CHALLENGE_STATUS_API_TOKEN`** en staging `.env`
5. **`NOWPAYMENTS_EMAIL` + `NOWPAYMENTS_PASSWORD`** en staging `.env` — necesarios para payouts JWT
6. **Nginx location block para `/media/`** — KYC documents no se sirven sin este bloque
7. **`anymail` en `requirements.txt`** O decisión y documentación de proveedor SMTP definitivo
8. **Certbot auto-renewal verificado** — `systemctl status certbot.timer` y añadir a `DEPLOY.md`
9. **`ADMIN_URL`** randomizado en staging `.env` — no usar el default `admin/`

---

## Bloqueantes — Antes de producción real

10. **Off-site backup configurado y restore drill completado** — S3 / Hetzner Object Storage / R2
11. **Media files (KYC) incluidas en backup**
12. **`.env` backup procedure** — en gestor de secretos (1Password / Vault)
13. **`SECURE_SSL_REDIRECT=True`** + HSTS alineado entre Django y Nginx
14. **Sentry DSN** configurado — sin esto los errores de producción son silenciosos
15. **Uptime monitor externo** — UptimeRobot / Betterstack (gratuito para 1 URL)
16. **Redis AUTH password** configurado en VPS
17. **Incident response básico documentado** — rotation de secrets, containment de VPS comprometida
18. **Certbot renewal verificado** (`systemctl status certbot.timer`)
19. **`pg_stat_statements` extension** instalada para diagnóstico de queries lentas

---

## Cosas que pueden esperar

- CSP headers (Content-Security-Policy) — no bloquea nada hoy en producción
- CI/CD automatizado (GitHub Actions → deploy.sh) — deploy.sh manual es suficiente para beta
- CDN para estáticos — WhiteNoise sirve bien hasta ~500 RPM
- Managed PostgreSQL/Redis — útil pero costoso; VPS single-node es fine hasta beta 100
- Prometheus/Grafana — ops panel propio + Sentry cubren la beta
- Load balancer / múltiples Daphne workers — no necesario hasta producción real con >200 simultáneos
- Login notification por nueva IP — nice-to-have post-beta
- 2FA obligatorio para usuarios regulares — post-beta; hoy solo staff

---

## Próximos Bloques

| Bloque | Descripción | Prioridad |
|--------|-------------|-----------|
| **L.1** | Infra Plan — VPS provider, dominio, IPs, DNS, SSH keys, firewall rules | Inmediato |
| **L.2** | VPS Provisioning — sistema, usuarios, dependencias, PostgreSQL, Redis, Certbot | Antes de deploy |
| **L.3** | Deploy Staging — clonar repo, .env completado, migrations, collectstatic, servicios activos, healthcheck | Core |
| **L.4** | Backup & Monitoring — cron PG backup + offsite S3/R2, Sentry DSN, UptimeRobot, certbot.timer, media en Nginx | Post-deploy |
| **L.5** | Beta 10 usuarios — BROKER_ACCESS_CODE activo, onboarding 10 usuarios reales, monitoreo first week | Post-staging estable |
