# Infra Plan Real — L.1: Money Brokers en Staging

| Campo       | Valor                                              |
|-------------|----------------------------------------------------|
| Fecha       | 2026-06-25                                         |
| Commit base | `247f1dd` bloque-K.4.1-save-staging-readiness-report |
| Marca       | Money Brokers / `moneybrokers.app`                 |
| Basado en   | K.4 Staging Readiness (docs/STAGING_READINESS_K4.md) |

---

## Notas críticas antes de comprar

> **Preferir x86/AMD64 sobre ARM para el primer deploy.**
> Los proveedores ofrecen instancias ARM más baratas (Hetzner CAX, AWS Graviton, etc.)
> pero algunas dependencias Python (numba, llvmlite, pandas-ta) tienen wheels precompilados
> solo para x86_64. En ARM, pip puede necesitar compilar desde fuente, lo que falla si
> faltan headers del sistema. Para el primer deploy usar siempre CPX/CX (Intel/AMD) o
> equivalente x86_64 en el proveedor elegido.

> **Verificar precios y specs actuales en el checkout del proveedor antes de comprar.**
> Los precios de VPS cambian frecuentemente. Las cifras en este documento son orientativas
> (junio 2026). Confirmar en el panel del proveedor antes de aprovisionar.

> **Producción real requiere backups offsite obligatorios.**
> No lanzar a producción real sin tener backups de PostgreSQL + media files en un destino
> offsite (S3, Hetzner Object Storage, Cloudflare R2) probados con un restore drill exitoso.
> Si la VPS muere sin offsite backup, los datos de usuarios se pierden permanentemente.

---

## A. Comparativa de Proveedores VPS

| Criterio | Hetzner | DigitalOcean | Vultr | AWS Lightsail |
|---|---|---|---|---|
| **2vCPU x86 / 4GB / 40-80GB** | ~€5/mo (CPX11) | ~$24/mo | ~$24/mo | ~$20/mo |
| **4vCPU x86 / 8GB / 80GB** | ~€10/mo (CPX21) | ~$48/mo | ~$48/mo | ~$40/mo |
| **8vCPU x86 / 16GB / 160GB** | ~€20/mo (CPX31) | ~$96/mo | ~$96/mo | ~$80/mo |
| **Arquitectura x86** | CPX series (AMD EPYC) ✅ | Intel/AMD ✅ | Intel/AMD ✅ | Intel/AMD ✅ |
| **Arquitectura ARM** | CAX series (evitar en 1er deploy) | — | — | Graviton (evitar) |
| **Ubicación** | DE / FI / US-Ashburn | NY / SF / Amsterdam | NY / Chicago / Amsterdam | Múltiples AWS |
| **Snapshots** | €0.011/GB/mes | 20% del droplet/mes | $0.05/GB/mes | Incluido con límites |
| **Object Storage** | Hetzner Object Storage €0.025/GB/mes | Spaces $5/mo (250GB) | Object Storage $5/mo | S3 |
| **Managed DB** | No (self-managed) | PostgreSQL $15/mo+ | No | RDS (costoso) |
| **Firewall** | Cloud Firewall gratis | Cloud Firewall gratis | Cloud Firewall gratis | Grupos de seguridad |
| **Transferencia** | 20TB incluidos | 4TB incluidos | 4TB incluidos | 1TB incluido |
| **DX / Panel** | Simple, limpio | Excelente | Bueno | AWS Console (complejo) |
| **Soporte** | Comunidad + tickets | 24/7 (planes) | Soporte básico | AWS Support |

### Recomendación final — Staging

**Hetzner CPX11** (x86/AMD, ~€5/mes verificar precio actual)
- 2 vCPU AMD, 4 GB RAM, 40 GB NVMe, 20 TB tráfico
- Serie **CPX** (AMD EPYC x86_64) — no CAX (ARM)
- Ubicación: **Ashburn, Virginia (US)** — mejor latencia para usuarios latinoamericanos
- Object Storage de Hetzner para backups off-site: €0.025/GB/mes

### Recomendación final — Producción Inicial

**Hetzner CPX21 o CPX31** (x86/AMD, verificar specs y precio actual en hetzner.com).
No depender de VPS mínima para producción real. Elegir el tier que deje margen de CPU/RAM
para picos de tráfico. Escalar verticalmente (resize) es posible sin reinstalar en Hetzner.

- Beta 100 usuarios: CPX21 (~€10/mo)
- Producción real (migración desde broker): CPX31 (~€20/mo) o superior según carga real

---

## B. Specs por Fase

| Fase | Modelo sugerido | CPU | RAM | Disco | Notas |
|---|---|---|---|---|---|
| Staging | Hetzner CPX11 (x86) | 2 vCPU AMD | 4 GB | 40 GB NVMe | Verificar precio en checkout |
| Beta 10 usuarios | Hetzner CPX11 (x86) | 2 vCPU AMD | 4 GB | 40 GB NVMe | Mismo VPS con BROKER_ACCESS_CODE |
| Beta 100 usuarios | Hetzner CPX21 (x86) | 4 vCPU AMD | 8 GB | 80 GB NVMe | Resize sin reinstalar |
| Producción inicial | Hetzner CPX31+ (x86) | 8+ vCPU AMD | 16+ GB | 160+ GB NVMe | Specs según carga real medida en beta |
| Producción escalada | CPX41/CCX33 o superior | 16+ vCPU | 32+ GB | 240+ GB | Load testing previo obligatorio |

**Límites esperados con CPX11 (staging):**
- Conexiones WebSocket simultáneas: ~200–500 (Daphne/Twisted)
- Requests HTTP/seg: ~100–200 (Django + PostgreSQL)
- Celery tasks: 4 workers, ~50 tareas/min cómodamente

---

## C. Dominio / DNS / Cloudflare

### Estructura de dominios sugerida

El código usa `moneybrokers.app` como fallback en `DEFAULT_FROM_EMAIL` y `TOTP_ISSUER_NAME`.
Confirmar disponibilidad del dominio antes de L.2.

```
moneybrokers.app           → producción (futuro)
staging.moneybrokers.app   → VPS staging (L.2/L.3)
www.moneybrokers.app       → alias de producción
```

Alternativas si `moneybrokers.app` no está disponible:
`moneybrokers.io`, `moneybroker.app`, `moneybrokers.co`

### Cloudflare: SÍ — obligatorio

**Por qué Cloudflare:**
- DNS gratis con propagación <1 min
- Proxy naranja: oculta IP del VPS, previene ataques directos
- DDoS protection L3/L4 automática (gratis)
- SSL/TLS gestionado (coexiste con Certbot en el servidor)
- Firewall rules gratuitas (bloquear IPs, bots, países)
- Caché de estáticos (reduce carga en WhiteNoise)

**Registrador recomendado:** Cloudflare Registrar (precio de costo, sin markup) o Namecheap.
No usar GoDaddy.

**Modo SSL en Cloudflare:** `Full (strict)` — Cloudflare valida el certificado Certbot en el VPS.

### Registros DNS en Cloudflare

| Tipo | Nombre | Valor | Proxy | TTL |
|---|---|---|---|---|
| A | `@` | `<IP_VPS>` | Sí (naranja) | Auto |
| A | `www` | `<IP_VPS>` | Sí (naranja) | Auto |
| A | `staging` | `<IP_VPS>` | Sí (naranja) | Auto |
| MX | `@` | `<MX SendGrid>` | No | Auto |
| TXT | `@` | SPF record SendGrid | No | Auto |
| CNAME | `s1._domainkey` | DKIM SendGrid | No | Auto |
| CNAME | `s2._domainkey` | DKIM SendGrid | No | Auto |
| TXT | `_dmarc` | DMARC policy | No | Auto |

---

## D. Seguridad del Servidor

### Hardening inicial (ejecutar en orden en L.2)

```bash
# 1. Crear usuario admin no-root
adduser deploy
usermod -aG sudo deploy

# 2. Copiar SSH key al usuario deploy
mkdir -p /home/deploy/.ssh
cat ~/.ssh/id_ed25519.pub >> /home/deploy/.ssh/authorized_keys
chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys

# 3. Deshabilitar root SSH y password auth
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd

# 4. Usuario sistema para la app (sin shell de login)
useradd --system --shell /usr/sbin/nologin --home /opt/trx_sim trx_sim
```

### UFW Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP → Nginx (redirige a HTTPS)
ufw allow 443/tcp   # HTTPS → Nginx
ufw enable
```

**Puertos internos — NUNCA exponer en UFW:**

| Puerto | Servicio | Binding obligatorio |
|---|---|---|
| 8001 | Daphne (ASGI) | `127.0.0.1` solamente |
| 5432 | PostgreSQL | `127.0.0.1` solamente |
| 6379 | Redis | `127.0.0.1` solamente |

### fail2ban

```bash
apt install fail2ban
# Jails mínimas: sshd (5 intentos/10min → ban 1h), nginx-http-auth
```

### Permisos de archivos críticos

```bash
chmod 600 /opt/trx_sim/.env
chmod 700 /opt/trx_sim/
chown -R trx_sim:trx_sim /opt/trx_sim/
chmod 750 /var/log/trx_sim/
```

---

## E. Servicios — Archivos listos en el repo

| Servicio | Archivo en repo | Acción en L.2 |
|---|---|---|
| Nginx | `deploy/nginx/trx_sim.conf` | `cp` + reemplazar `YOUR_DOMAIN` + añadir location `/media/` + `nginx -t && reload` |
| Daphne | `deploy/systemd/daphne.service` | `cp /etc/systemd/system/` + `enable` |
| Celery Worker | `deploy/systemd/celery-worker.service` | Ídem |
| Celery Beat | `deploy/systemd/celery-beat.service` | Ídem |
| Redis persistence | `deploy/redis_persistence.conf` | Append a `/etc/redis/redis.conf` |
| logrotate | `deploy/logrotate/trx_sim` | `cp /etc/logrotate.d/` |
| deploy.sh | `deploy/scripts/deploy.sh` | Usar para cada deploy posterior |
| healthcheck.sh | `deploy/scripts/healthcheck.sh` | Verificar tras cada deploy |

### Nginx — location block para media (pendiente de añadir en L.2)

Añadir dentro del bloque HTTPS en `trx_sim.conf` antes de la regla de logs:

```nginx
# KYC documents and user uploads
location /media/ {
    alias /opt/trx_sim/media/;
    expires 1h;
    add_header Cache-Control "private";
    access_log off;
}
```

### Orden de arranque inicial (crítico)

```
1. PostgreSQL     → servicio del sistema, ya activo
2. Redis          → servicio del sistema, ya activo
3. Certbot        → obtener certificado SSL antes de habilitar Nginx HTTPS
4. Nginx          → start (necesita cert para SSL)
5. celery-beat    → start (necesita Redis)
6. sleep 5        → tiempo para lock de redBeat
7. celery-worker  → start
8. sleep 3
9. daphne         → start
10. healthcheck.sh → verificación completa
```

---

## F. Backups y Restore

### Estrategia completa

| Qué | Local | Offsite | Frecuencia | Retención | Herramienta |
|---|---|---|---|---|---|
| PostgreSQL dump | `/var/backups/trx_sim/` | Hetzner Object Storage / S3 | Diario 03:00 UTC | 14 local / 30 offsite | `backup_postgres.sh` + `rclone` |
| Media files (KYC) | — | Offsite junto con PG | Diario | 30 días | `rclone` |
| `.env` | No guardar en VPS extra | Gestor de secretos (1Password/Bitwarden) | Ante cada cambio | Permanente | Manual |
| Redis | `/var/lib/redis/` (AOF+RDB) | No necesario (data efímera) | Automático | 7 días RDB | `redis_persistence.conf` |
| VPS Snapshot | — | Hetzner Snapshots | Semanal | 4 snapshots | Panel Hetzner |

### rclone para offsite

```bash
# Instalar
curl https://rclone.org/install.sh | sudo bash
rclone config   # configurar Hetzner Object Storage (S3-compatible)

# Cron diario a las 03:30 UTC (30min después del pg_dump)
30 3 * * * rclone copy /var/backups/trx_sim/ hetzner-s3:trx-sim-backups/ \
  --include "*.dump" --min-age 1m >> /var/log/trx_sim/backup-offsite.log 2>&1

# Media files
35 3 * * * rclone sync /opt/trx_sim/media/ hetzner-s3:trx-sim-media/ \
  >> /var/log/trx_sim/backup-media.log 2>&1
```

### Certbot auto-renewal

```bash
# Verificar que el timer de renovación está activo:
systemctl status certbot.timer

# Si no está activo:
systemctl enable certbot.timer && systemctl start certbot.timer

# Verificar con dry-run:
certbot renew --dry-run

# Añadir verificación mensual adicional en crontab:
0 4 1 * * certbot renew --quiet >> /var/log/trx_sim/certbot.log 2>&1
```

### Restore drill (obligatorio antes de producción real)

```bash
# 1. VPS temporal (destruir tras la prueba)
# 2. Descargar último dump offsite
rclone copy hetzner-s3:trx-sim-backups/ /tmp/restore/ \
  --include "*.dump" --max-age 48h
# 3. Ejecutar restore
bash restore_postgres.sh /tmp/restore/<latest>.dump
# 4. Verificar
bash healthcheck.sh http://127.0.0.1:8001
# 5. Destruir VPS temporal
```

---

## G. Monitoreo

### Stack mínimo (todo gratuito para staging)

| Herramienta | Para qué | Costo | Setup |
|---|---|---|---|
| `/api/health/` | Liveness probe público | Gratis (propio) | Ya existe |
| **UptimeRobot** | Ping externo cada 5 min, alerta email si cae | Gratis (50 monitores) | Apuntar a `https://staging.moneybrokers.app/api/health/` |
| **Sentry** | Errores Python/Django en tiempo real | Gratis (5K eventos/mes) | Añadir `SENTRY_DSN` al `.env` |
| **journald** | Logs de todos los servicios systemd | Gratis | `journalctl -u daphne -f` |
| Celery heartbeat | Confirma Beat + Worker vivos | Gratis (propio) | Task `beat-heartbeat-5m` ya existe |
| `/api/health/detail/` | DB + Redis latency, staff-only | Gratis (propio) | Ya existe |

### Sentry — setup en 5 min

```bash
# sentry-sdk ya está en requirements.txt
# 1. Crear cuenta en sentry.io (gratis)
# 2. New Project → Python → Django
# 3. Copiar DSN → añadir al .env:
SENTRY_DSN=https://xxx@o123.ingest.sentry.io/456
SENTRY_ENVIRONMENT=staging
```

### UptimeRobot — monitores mínimos

```
Monitor 1: https://staging.moneybrokers.app/api/health/
  Intervalo: 5 min
  Alerta: email si != 200 o timeout > 30s

Monitor 2 (producción, cuando exista):
  Alerta: email + SMS
```

---

## H. Variables .env Staging — Lista Completa

### Críticas — valores reales obligatorios antes de L.3

| Variable | Cómo generar / obtener |
|---|---|
| `DJANGO_SECRET_KEY` | `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `DEBUG` | `False` |
| `DOMAIN` | `staging.moneybrokers.app` |
| `SITE_URL` | `https://staging.moneybrokers.app` |
| `ALLOWED_HOSTS_EXTRA` | `staging.moneybrokers.app` |
| `CSRF_TRUSTED_ORIGINS_EXTRA` | `https://staging.moneybrokers.app` |
| `ADMIN_URL` | `python -c "import secrets; print(secrets.token_urlsafe(12))"` + `/` |
| `DB_NAME` | `trx_sim_staging` |
| `DB_USER` | `trx_sim` |
| `DB_PASSWORD` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DB_HOST` | `127.0.0.1` |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` |
| `TOTP_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TOTP_STAFF_REQUIRED` | `True` |
| `BROKER_ACCESS_CODE` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `NOWPAYMENTS_API_KEY` | Sandbox API key (sandbox.nowpayments.io) |
| `NOWPAYMENTS_IPN_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `NOWPAYMENTS_WEBHOOK_URL` | `https://staging.moneybrokers.app/deposit/callback/` |
| `NOWPAYMENTS_EMAIL` | Email de la cuenta NowPayments |
| `NOWPAYMENTS_PASSWORD` | Password de la cuenta NowPayments |
| `CHALLENGE_WEBHOOK_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `CHALLENGE_STATUS_API_TOKEN` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
| `EMAIL_HOST` | `smtp.sendgrid.net` |
| `EMAIL_PORT` | `587` |
| `EMAIL_HOST_USER` | `apikey` (literal — no tu email) |
| `EMAIL_HOST_PASSWORD` | API key de SendGrid (`SG.xxx...`) |
| `DEFAULT_FROM_EMAIL` | `noreply@staging.moneybrokers.app` |
| `ADMIN_EMAIL` | `nafferphotographer@gmail.com` |
| `SUPPORT_EMAIL` | `support@staging.moneybrokers.app` |
| `SENTRY_DSN` | DSN del proyecto en sentry.io |
| `SENTRY_ENVIRONMENT` | `staging` |
| `FINNHUB_API_KEY` | finnhub.io free tier |
| `LOG_JSON` | `true` |
| `SECURE_SSL_REDIRECT` | `True` (activar tras verificar SSL) |

### Opcionales con defaults aceptables para staging

| Variable | Default | Ajuste recomendado |
|---|---|---|
| `SECURE_HSTS_SECONDS` | `0` | Mantener 0 en staging |
| `MAX_WITHDRAWAL_DAILY_USD` | `1500` | Reducir a `100` para staging |
| `MIN_WITHDRAWAL_USD` | `25` | OK |
| `SNAPSHOT_RETENTION_DAYS` | `7` | OK |
| `REVENUE_SNAPSHOT_RETENTION_DAYS` | `90` | OK |
| `LOAD_TEST_MODE` | `False` | NUNCA `True` en staging real |
| `TOTP_ISSUER_NAME` | `Money Brokers` | OK |
| `LOG_JSON` | `false` | Cambiar a `true` en staging |
| `SIM_LOG_LEVEL` | `INFO` | OK |

---

## I. NOWPayments — Staging sin Dinero Real

### Sandbox de NowPayments

NowPayments tiene ambiente sandbox completo en `https://sandbox.nowpayments.io/`:
- API key de sandbox es diferente del production API key
- Pagos con crypto de testnet — no mueve dinero real
- El webhook callback funciona igual que producción

### Configuración para staging

```bash
NOWPAYMENTS_API_KEY=<sandbox_api_key>         # sandbox.nowpayments.io
NOWPAYMENTS_IPN_SECRET=<hex_generado>
NOWPAYMENTS_WEBHOOK_URL=https://staging.moneybrokers.app/deposit/callback/
NOWPAYMENTS_EMAIL=<email_cuenta_nowpayments>
NOWPAYMENTS_PASSWORD=<password_cuenta>
```

### Flujo de prueba en sandbox

```
1. Crear depósito en staging → genera pay_address de testnet
2. En sandbox.nowpayments.io → Dashboard → Payment Simulator
   → Enviar IPN manual con el payment_id
3. Webhook llega a staging → verifica HMAC-SHA512 → credita wallet
```

El `ChallengeProduct.price_usd = $1.00` del producto interno de prueba permite testear el
flujo completo sin mover cantidades grandes en sandbox.

---

## J. Email — Proveedor para Staging

### Comparativa

| Proveedor | Free tier | Costo paid | Deliverability | Integración | Uso |
|---|---|---|---|---|---|
| **SendGrid** | 100/día forever | $19.95/mo (50K) | Alta | SMTP directo o anymail | ✅ Staging |
| **Mailgun** | 100/día (3 meses) | $15/mo (10K) | Alta | SMTP directo o anymail | OK |
| **Postmark** | No free | $15/mo (10K) | Muy alta (transaccional) | anymail | ✅ Producción |
| **Brevo** | 300/día forever | $25/mo (20K) | Media | SMTP o anymail | Alternativa |
| Gmail SMTP | Límites bajos | N/A | Baja (spam) | — | ❌ Nunca en prod |

### Recomendación

- **Staging:** SendGrid free (100 emails/día — suficiente para pruebas)
- **Producción:** Postmark — mejor deliverability para emails transaccionales

### Configuración SendGrid con SMTP directo (sin instalar anymail)

No se necesita `anymail` para SendGrid por SMTP. `anymail` solo es necesario si se quieren
features avanzados (webhooks de entrega, templates, estadísticas de apertura).

```bash
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=apikey        # literal, no el email de la cuenta
EMAIL_HOST_PASSWORD=SG.xxx    # API key de SendGrid con permisos Mail Send
DEFAULT_FROM_EMAIL=noreply@staging.moneybrokers.app
```

**SPF + DKIM obligatorios** — sin verificar el dominio en SendGrid, los emails van a spam.
SendGrid tiene guía paso a paso de verificación de dominio con registros CNAME en Cloudflare.

---

## Checklists

### Checklist — Compra y Configuración Inicial

```
[ ] Confirmar disponibilidad dominio moneybrokers.app (o alternativa)
[ ] Registrar dominio en Cloudflare Registrar o Namecheap
[ ] Agregar dominio a Cloudflare (cambiar nameservers en registrador)
[ ] Crear cuenta en Hetzner (hetzner.com)
[ ] Verificar precio/specs actuales de CPX11 (x86) en el checkout
[ ] Provisionar VPS CPX11 x86 en Ashburn (US) — Ubuntu 22.04 LTS
[ ] Anotar IP pública del VPS
[ ] Generar SSH key: ssh-keygen -t ed25519 -C "trx_sim_staging"
[ ] Agregar SSH public key al VPS en panel Hetzner antes de crear
[ ] Crear cuenta en SendGrid (sendgrid.com) — free tier
[ ] Verificar dominio en SendGrid (DNS CNAME + SPF TXT en Cloudflare)
[ ] Crear cuenta en Sentry (sentry.io) — free tier, proyecto Django
[ ] Crear proyecto en sandbox.nowpayments.io — obtener sandbox API key
[ ] Crear cuenta en UptimeRobot (uptimerobot.com) — free
[ ] Crear bucket en Hetzner Object Storage (o Cloudflare R2) para backups
[ ] Generar todos los secrets (sección H) y guardar en 1Password/Bitwarden
[ ] Preparar archivo .env staging completo (NO subir al repo)
```

### Checklist — DNS en Cloudflare

```
[ ] Registro A  → @              → <IP_VPS>         → Proxy ON
[ ] Registro A  → www            → <IP_VPS>         → Proxy ON
[ ] Registro A  → staging        → <IP_VPS>         → Proxy ON
[ ] Registro MX → @              → SendGrid MX       → Proxy OFF
[ ] Registro TXT → @             → SPF SendGrid      → Proxy OFF
[ ] Registro CNAME → s1._domainkey → SendGrid DKIM  → Proxy OFF
[ ] Registro CNAME → s2._domainkey → SendGrid DKIM  → Proxy OFF
[ ] Registro TXT → _dmarc        → política DMARC   → Proxy OFF
[ ] Verificar propagación: dig staging.moneybrokers.app
[ ] Configurar SSL/TLS en Cloudflare → Full (strict)
[ ] Verificar que staging.moneybrokers.app resuelve a IP del VPS
```

### Checklist — .env Staging Completo

```
[ ] DJANGO_SECRET_KEY          — 50+ chars aleatorios
[ ] DEBUG=False
[ ] DOMAIN=staging.moneybrokers.app
[ ] SITE_URL=https://staging.moneybrokers.app
[ ] ALLOWED_HOSTS_EXTRA=staging.moneybrokers.app
[ ] CSRF_TRUSTED_ORIGINS_EXTRA=https://staging.moneybrokers.app
[ ] ADMIN_URL=<random_urlsafe_12>/
[ ] DB_NAME=trx_sim_staging
[ ] DB_USER=trx_sim
[ ] DB_PASSWORD=<32 chars aleatorios>
[ ] DB_HOST=127.0.0.1
[ ] DB_PORT=5432
[ ] REDIS_URL=redis://127.0.0.1:6379/0
[ ] TOTP_ENCRYPTION_KEY=<Fernet key 44 chars>
[ ] TOTP_STAFF_REQUIRED=True
[ ] BROKER_ACCESS_CODE=<32 chars urlsafe>
[ ] NOWPAYMENTS_API_KEY=<sandbox key>
[ ] NOWPAYMENTS_IPN_SECRET=<hex 64 chars>
[ ] NOWPAYMENTS_WEBHOOK_URL=https://staging.moneybrokers.app/deposit/callback/
[ ] NOWPAYMENTS_EMAIL=<email cuenta NowPayments>
[ ] NOWPAYMENTS_PASSWORD=<password cuenta>
[ ] CHALLENGE_WEBHOOK_SECRET=<hex 64 chars>
[ ] CHALLENGE_STATUS_API_TOKEN=<hex 64 chars>
[ ] EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
[ ] EMAIL_HOST=smtp.sendgrid.net
[ ] EMAIL_PORT=587
[ ] EMAIL_USE_TLS=True
[ ] EMAIL_HOST_USER=apikey
[ ] EMAIL_HOST_PASSWORD=<SendGrid API key SG.xxx>
[ ] DEFAULT_FROM_EMAIL=noreply@staging.moneybrokers.app
[ ] ADMIN_EMAIL=nafferphotographer@gmail.com
[ ] SUPPORT_EMAIL=support@staging.moneybrokers.app
[ ] SENTRY_DSN=<DSN de sentry.io>
[ ] SENTRY_ENVIRONMENT=staging
[ ] FINNHUB_API_KEY=<finnhub.io free tier>
[ ] LOG_JSON=true
[ ] SECURE_SSL_REDIRECT=True
[ ] SECURE_HSTS_SECONDS=0
[ ] LOAD_TEST_MODE=False
[ ] MAX_WITHDRAWAL_DAILY_USD=100
[ ] MIN_WITHDRAWAL_USD=25
[ ] SNAPSHOT_RETENTION_DAYS=7
[ ] REVENUE_SNAPSHOT_RETENTION_DAYS=90
[ ] APP_NAME=trx_sim
[ ] TOTP_ISSUER_NAME=Money Brokers
```

---

## Próximo Bloque — L.2: VPS Provisioning Paso a Paso

L.2 cubrirá en orden exacto de ejecución:

1. Primera conexión SSH como root → crear usuario `deploy` con sudo
2. Hardening SSH — key only, deshabilitar root, cambiar puerto opcional
3. UFW + fail2ban
4. Instalar dependencias del sistema — Python 3.12+, PostgreSQL 15, Redis 7, Nginx, Certbot, git, build-essential
5. Crear usuario sistema `trx_sim` (sin shell de login)
6. Clonar repositorio en `/opt/trx_sim/`
7. Crear virtualenv e instalar `requirements.txt` (verificar que todas las wheels instalan en x86)
8. Configurar PostgreSQL — crear user + DB, ajustar `pg_hba.conf`
9. Configurar Redis — persistence config, bind 127.0.0.1
10. Crear y configurar `.env` en `/opt/trx_sim/.env` con chmod 600
11. `python manage.py migrate --noinput`
12. `python manage.py collectstatic --noinput`
13. `python manage.py createsuperuser` (usuario staff inicial)
14. Instalar servicios systemd — daphne, celery-worker, celery-beat
15. Configurar Nginx — reemplazar `YOUR_DOMAIN`, añadir location `/media/`, verificar con `nginx -t`
16. Certbot SSL — obtener certificado, verificar `certbot.timer` activo
17. Arrancar servicios en orden correcto (Beat → Worker → Daphne)
18. `bash deploy/scripts/healthcheck.sh`
19. Configurar cron de backup — pg_dump + rclone offsite
20. Configurar logrotate
21. Crear monitor en UptimeRobot
22. Smoke test mínimo contra staging real (registro, login, depósito sandbox, challenge sandbox)
