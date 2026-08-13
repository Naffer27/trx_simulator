# trx_sim Deployment Runbook

Complete guide for deploying and operating trx_sim on a Linux (Ubuntu/Debian) server.

---

## Architecture

```
Internet → Nginx (80/443) → Daphne (127.0.0.1:8001, ASGI)
                              ↓
                         Django 5.1.6
                              ↓
                    Redis (channel layer + queues + metrics)
                    PostgreSQL (primary data store)
                    Celery Worker (async tasks)
                    Celery Beat w/ RedBeat (scheduled tasks)
```

---

## Prerequisites

- Ubuntu 22.04 or Debian 12
- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Nginx
- A system user `trx_sim` (no login shell)
- Virtualenv at `/opt/trx_sim/venv`
- App directory at `/opt/trx_sim/`
- `.env` file at `/opt/trx_sim/.env`
- Log directory at `/var/log/trx_sim/`

### Install system packages

```bash
apt-get update && apt-get install -y \
  python3.12 python3.12-venv python3-pip \
  postgresql-15 redis-server nginx \
  git curl build-essential
```

---

## Initial Setup

### 1. Create system user

```bash
useradd --system --shell /usr/sbin/nologin --home /opt/trx_sim trx_sim
```

### 2. Clone repository

```bash
mkdir -p /opt/trx_sim
git clone <your-repo-url> /opt/trx_sim
chown -R trx_sim:trx_sim /opt/trx_sim
```

### 3. Create virtualenv and install dependencies

```bash
sudo -u trx_sim python3.12 -m venv /opt/trx_sim/venv
sudo -u trx_sim /opt/trx_sim/venv/bin/pip install -r /opt/trx_sim/requirements.txt
```

### 4. Configure environment

```bash
cp /opt/trx_sim/deploy/.env.staging.template /opt/trx_sim/.env
# Edit .env — set SECRET_KEY, DB_*, REDIS_URL, ALLOWED_HOSTS, etc.
chmod 600 /opt/trx_sim/.env
```

### 5. Configure PostgreSQL

```bash
sudo -u postgres psql <<SQL
CREATE USER trx_sim WITH PASSWORD 'your_db_password';
CREATE DATABASE trx_sim_staging OWNER trx_sim;
GRANT ALL PRIVILEGES ON DATABASE trx_sim_staging TO trx_sim;
SQL
```

### 6. Run migrations and collect static files

```bash
sudo -u trx_sim bash -c "
  cd /opt/trx_sim
  source venv/bin/activate
  python manage.py migrate
  python manage.py collectstatic --noinput
"
```

### 7. Create superuser

```bash
sudo -u trx_sim /opt/trx_sim/venv/bin/python /opt/trx_sim/manage.py createsuperuser
```

### 8. Configure Redis persistence

```bash
# Add persistence settings to Redis config
cat /opt/trx_sim/deploy/redis_persistence.conf >> /etc/redis/redis.conf
systemctl restart redis-server
```

### 9. Install systemd services

```bash
cp /opt/trx_sim/deploy/systemd/daphne.service       /etc/systemd/system/
cp /opt/trx_sim/deploy/systemd/celery-worker.service /etc/systemd/system/
cp /opt/trx_sim/deploy/systemd/celery-beat.service  /etc/systemd/system/

systemctl daemon-reload
systemctl enable daphne celery-worker celery-beat
```

### 10. Install the automated backup scheduler (O.5b)

Deliberately independent of Redis/Celery/Daphne — see
`deploy/systemd/backup-postgres.service` for the full rationale. Requires
`/var/backups/trx_sim` to exist and be owned by `trx_sim` (unlike
`/var/log/trx_sim`, this directory is NOT created by step 15 below —
`backup_postgres.sh` cannot create it itself under an unprivileged user
if `/var/backups/` itself is root-owned, which it is on most distros).

```bash
mkdir -p /var/backups/trx_sim
chown trx_sim:trx_sim /var/backups/trx_sim
chmod 750 /var/backups/trx_sim

cp /opt/trx_sim/deploy/systemd/backup-postgres.service /etc/systemd/system/
cp /opt/trx_sim/deploy/systemd/backup-postgres.timer   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now backup-postgres.timer
```

Verify:

```bash
systemctl status backup-postgres.timer
systemctl list-timers backup-postgres.timer

# Optional: trigger an immediate run instead of waiting for 03:00 UTC
systemctl start backup-postgres.service
journalctl -u backup-postgres.service -n 50
```

### 11. Install the offsite backup verification scheduler (O.5c-2)

Independent of `backup-postgres.timer` by design — see
`deploy/systemd/backup-offsite.service` for the full rationale (no
`Requires=`/`After=` ordering between the two, so an offsite hiccup can
never interfere with the local backup unit or vice versa). Requires the
DEDICATED `rclone.conf` to be provisioned FIRST — this file is never
part of this repository and must never contain real credentials
anywhere that could reach Git:

```bash
mkdir -p /etc/trx_sim
cp /opt/trx_sim/deploy/rclone.conf.example /etc/trx_sim/rclone.conf
chown trx_sim:trx_sim /etc/trx_sim/rclone.conf
chmod 600 /etc/trx_sim/rclone.conf

# Edit as the trx_sim user (or chown back afterwards) and fill in the
# real remote credentials — see deploy/rclone.conf.example for the
# expected shape. Initial operational target is Cloudflare R2 (O.5c
# Fase 0 approval, decision 1).
sudo -u trx_sim vi /etc/trx_sim/rclone.conf
```

Then add to `/opt/trx_sim/.env` (see `.env.example`, O.5c-1) — the
remote NAME must match whatever section header you used in
`rclone.conf` above:

```
RCLONE_CONFIG=/etc/trx_sim/rclone.conf
RCLONE_REMOTE=<remote name from rclone.conf>
RCLONE_REMOTE_PATH=trx-sim-backups
```

Install the units:

```bash
cp /opt/trx_sim/deploy/systemd/backup-offsite.service /etc/systemd/system/
cp /opt/trx_sim/deploy/systemd/backup-offsite.timer   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now backup-offsite.timer
```

Verify:

```bash
systemctl status backup-offsite.timer
systemctl list-timers backup-offsite.timer

# Optional: trigger an immediate run instead of waiting for ~03:30 UTC
systemctl start backup-offsite.service
journalctl -u backup-offsite.service -n 50
```

### 11a. Install the media offsite backup scheduler (O.5e-3)

Reuses the SAME `RCLONE_CONFIG`/`RCLONE_REMOTE` provisioned in step 11
above — no separate rclone config or remote is needed for media. Only a
distinct path prefix on that remote (`MEDIA_RCLONE_REMOTE_PATH`, default
`trx-sim-media-backups`) so media archives never collide with Postgres
dumps in the same bucket. `MEDIA_ROOT` itself needs no provisioning —
Django's `FileSystemStorage` already creates it on first upload.

Add to `/opt/trx_sim/.env` (see `.env.example`, O.5e-3) — optional, safe
defaults shown below apply if omitted:

```
# MEDIA_RCLONE_REMOTE_PATH=trx-sim-media-backups
# MEDIA_BACKUP_EXPECTED_INTERVAL_SECONDS=86400
# MEDIA_BACKUP_STALE_SECONDS=129600
```

Install the units:

```bash
cp /opt/trx_sim/deploy/systemd/backup-media-offsite.service /etc/systemd/system/
cp /opt/trx_sim/deploy/systemd/backup-media-offsite.timer   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now backup-media-offsite.timer
```

Verify:

```bash
systemctl status backup-media-offsite.timer
systemctl list-timers backup-media-offsite.timer

# Optional: trigger an immediate run instead of waiting for ~04:00 UTC
systemctl start backup-media-offsite.service
journalctl -u backup-media-offsite.service -n 50
```

Fires daily at ~04:00 UTC — 30 minutes after `backup-offsite.timer`'s own
~03:30 UTC fire (independent timer, no `Requires=`/`After=` dependency on
either Postgres backup unit, same "never entangled" design as step 11).
`MEDIA_ROOT` stays private: this unit only ever READS it
(`ReadOnlyPaths=/opt/trx_sim/media` — enforced by the kernel, not just by
`backup_media_offsite.sh`'s own logic) and writes only to
`/var/log/trx_sim`. This does **not** change how media files are served —
KYC documents, Treasury evidence, and Broker documents remain reachable
exclusively through the authenticated/authorized Django routes under
`/secure-media/...` (O.5e-1); no `location /media/` exists in Nginx and
none is added by this step.

### 12. Provision the restore drill role (O.5d-1)

`deploy/scripts/restore_drill.sh` proves a backup is actually
restorable by restoring it into a throwaway temporary database, then
destroying it — it deliberately runs as a **separate, minimally
privileged** role, never as `trx_sim`. `trx_sim` receives NO new
privileges for this — it stays exactly as provisioned in step 5.

```bash
sudo -u postgres psql <<SQL
CREATE ROLE trx_sim_drill WITH
    LOGIN
    PASSWORD 'your_drill_role_password'
    CREATEDB
    NOSUPERUSER
    NOCREATEROLE
    NOREPLICATION
    NOINHERIT
    CONNECTION LIMIT 3;

-- CRITICAL: revoke from PUBLIC, not merely from the named role.
-- Postgres grants CONNECT to PUBLIC on every database by default;
-- REVOKE CONNECT ... FROM trx_sim_drill alone is a silent NO-OP as
-- long as PUBLIC still has it — empirically confirmed during O.5d-1
-- Fase 0 (revoking only from the named role did NOT prevent it from
-- connecting; only revoking from PUBLIC did).
REVOKE CONNECT ON DATABASE trx_sim_staging FROM PUBLIC;

-- Documentation only, not strictly required: trx_sim already retains
-- CONNECT on its own database via ownership, independent of the
-- PUBLIC revoke above. Stated explicitly so nobody has to wonder
-- whether the app broke.
GRANT CONNECT ON DATABASE trx_sim_staging TO trx_sim;
SQL
```

This role can never read, write, own, or drop `trx_sim_staging` — it
is not the owner (`DROP DATABASE` requires being the owner or
superuser) and has no `CONNECT` privilege on it at all. It can only
create and drop databases it itself creates. `restore_drill.sh` adds
its own additional guards on top (a fixed, internally-generated
`trx_restore_drill_*` naming scheme, with no override of any kind) —
see that script's own header comment for the full rationale.

Add to `/opt/trx_sim/.env` (used only when an operator runs the drill
manually — this is NOT wired into any systemd timer):

```
RESTORE_DRILL_DB_USER=trx_sim_drill
RESTORE_DRILL_DB_PASSWORD=your_drill_role_password
```

Run manually (never scheduled — see O.5d Fase 0 approval, decision 3):

```bash
sudo -u trx_sim bash -c 'set -a; source /opt/trx_sim/.env; set +a; \
  bash /opt/trx_sim/deploy/scripts/restore_drill.sh --source local'
```

### 13. Configure Nginx

```bash
cp /opt/trx_sim/deploy/nginx/trx_sim.conf /etc/nginx/sites-available/trx_sim
ln -s /etc/nginx/sites-available/trx_sim /etc/nginx/sites-enabled/trx_sim
rm -f /etc/nginx/sites-enabled/default

# Configure SSL (obtain certificate first):
certbot --nginx -d yourdomain.com --non-interactive --agree-tos -m admin@yourdomain.com

nginx -t && systemctl restart nginx
```

### 14. Configure logrotate

```bash
cp /opt/trx_sim/deploy/logrotate/trx_sim /etc/logrotate.d/trx_sim
```

### 15. Create log directory

```bash
mkdir -p /var/log/trx_sim
chown trx_sim:trx_sim /var/log/trx_sim
```

### 16. Start all services

```bash
systemctl start celery-beat
sleep 5
systemctl start celery-worker
sleep 3
systemctl start daphne
```

### 17. Verify health

```bash
bash /opt/trx_sim/deploy/scripts/healthcheck.sh
```

---

## Subsequent Deploys

Use the automated deploy script:

```bash
sudo -u trx_sim bash /opt/trx_sim/deploy/scripts/deploy.sh
```

This handles: git pull → pip install → migrate → collectstatic → restart Beat → restart Worker → restart Daphne → healthcheck.

### Manual deploy steps (if script unavailable)

```bash
cd /opt/trx_sim
git pull --ff-only
source venv/bin/activate
pip install -q -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear -v 0
sudo systemctl restart celery-beat
sleep 5
sudo systemctl restart celery-worker
sleep 3
sudo systemctl restart daphne
bash deploy/scripts/healthcheck.sh
```

---

## Health Verification

### Check all services

```bash
systemctl status daphne celery-worker celery-beat nginx redis-server postgresql
```

### Check API health endpoint

```bash
curl -s http://127.0.0.1:8001/api/health/ | python3 -m json.tool
```

### Check WebSocket (requires wscat or websocat)

```bash
# Install: npm install -g wscat
wscat -c ws://127.0.0.1:8001/ws/trading/?symbol=EUR%2FUSD \
  --header "Cookie: sessionid=<your-session-id>"
```

### Check Redis

```bash
redis-cli ping       # should return PONG
redis-cli info memory | grep used_memory_human
redis-cli get trx:metrics:ws_connections
```

### Check Celery workers

```bash
/opt/trx_sim/venv/bin/celery -A trx_simulator inspect ping
/opt/trx_sim/venv/bin/celery -A trx_simulator inspect active
```

### View logs

```bash
journalctl -u daphne -n 100 --no-pager
journalctl -u celery-worker -n 100 --no-pager
journalctl -u celery-beat -n 100 --no-pager
tail -f /var/log/trx_sim/app.log
```

### Ops panel (browser)

Open: `https://yourdomain.com/staff/ops/` (staff login required)

---

## Rollback

### Code rollback

```bash
cd /opt/trx_sim
git log --oneline -10        # find the target commit
git checkout <commit-hash>   # or: git reset --hard <commit>
# Then re-run deploy steps: migrate, collectstatic, restart services
```

### Database rollback

```bash
# List available backups
ls -lt /var/backups/trx_sim/

# Restore (DESTRUCTIVE — reads current data are LOST)
bash /opt/trx_sim/deploy/scripts/restore_postgres.sh \
  /var/backups/trx_sim/trx_sim_staging_<timestamp>.dump
```

### Django migration rollback

```bash
# List migrations
python manage.py showmigrations simulator

# Roll back to a specific migration
python manage.py migrate simulator <migration-name>
```

---

## Emergency Procedures

### Service is down

```bash
# Check what failed
journalctl -u daphne --since "5 minutes ago"
journalctl -u celery-worker --since "5 minutes ago"

# Restart individual service
sudo systemctl restart daphne
sudo systemctl restart celery-worker
sudo systemctl restart celery-beat
```

### Redis is unreachable

```bash
systemctl status redis-server
systemctl restart redis-server
# Check /var/log/redis/redis-server.log
```

Redis down impact:
- WebSocket channel layer fails → new WS connections rejected
- Rate limiting fails open (requests still served)
- Celery task queuing fails

### PostgreSQL is unreachable

```bash
systemctl status postgresql
systemctl restart postgresql
sudo -u postgres psql -c "SELECT 1"
```

### Celery Beat running twice (lock conflict)

```bash
# Only one Beat instance must run at a time
systemctl status celery-beat
# If two instances running, kill all and restart once:
pkill -f "celery beat"
sleep 35   # wait for RedBeat lock to expire (REDBEAT_LOCK_TIMEOUT=300s)
systemctl start celery-beat
```

### Disable 2FA for a locked-out user (emergency)

```bash
sudo -u trx_sim /opt/trx_sim/venv/bin/python /opt/trx_sim/manage.py \
  disable_2fa <username> --confirm
```

This writes an AuditLog event and is irreversible. The user must re-enroll 2FA.

### Reset stress peak metrics after load test

```bash
sudo -u trx_sim /opt/trx_sim/venv/bin/python /opt/trx_sim/manage.py \
  reset_peaks --confirm
```

---

## Backup

### PostgreSQL (manual)

```bash
bash /opt/trx_sim/deploy/scripts/backup_postgres.sh
```

A concurrent manual run while the timer-triggered instance is still
running is safely rejected by the script's own `flock` (O.5b) — it
exits cleanly without touching any metadata, it does not queue up
behind the running dump.

### PostgreSQL (automated — systemd timer, O.5b)

Daily at 03:00 UTC, keep 14 copies. **Not** cron, **not** Celery Beat —
deliberately independent of Redis/Celery so the backup can run even if
the application layer is down. See step 10 in Initial Setup above for
installation, and `deploy/systemd/backup-postgres.service` /
`backup-postgres.timer` for the full unit definitions and rationale.

```bash
systemctl status backup-postgres.timer
systemctl list-timers backup-postgres.timer
journalctl -u backup-postgres.service -n 50
```

Backup freshness is surfaced at `GET /api/health/detail/` → `"backup"`
(O.5a) — `stale`/`error` degrade the endpoint to 503.

### PostgreSQL offsite verification (manual)

```bash
bash /opt/trx_sim/deploy/scripts/backup_offsite.sh
```

Reads the current `backup_success.json` (O.5a) and, ONLY after proving
the remote copy is byte-identical (SHA-256 match against an
independently downloaded copy) AND restorable (`pg_restore --list` on
the recovered copy), writes `offsite_success.json` (O.5c-1). Never
modifies `backup_success.json`/`backup_failure.json`, and never writes
to or deletes anything under `/var/backups/trx_sim` — the local dump is
read-only input to this script.

### PostgreSQL offsite verification (automated — systemd timer, O.5c-2)

Daily at ~03:30 UTC (30 minutes after `backup-postgres.timer`'s 03:00
UTC fire) — deliberately its own independent timer, with no
`Requires=`/`After=` dependency on `backup-postgres.timer`/`.service`,
so an offsite hiccup can never interfere with the local backup. See
step 11 in Initial Setup above for installation (including the required
`rclone.conf` provisioning), and `deploy/systemd/backup-offsite.service`
/ `backup-offsite.timer` for the full unit definitions and rationale.

```bash
systemctl status backup-offsite.timer
systemctl list-timers backup-offsite.timer
journalctl -u backup-offsite.service -n 50
```

Integration with `GET /api/health/detail/` (an `"offsite"` block
alongside `"backup"`) is O.5c-3 — not yet implemented as of this
scheduler existing.

### Media (MEDIA_ROOT) offsite verification (manual)

```bash
bash /opt/trx_sim/deploy/scripts/backup_media_offsite.sh
```

Builds a deterministic tar archive of `MEDIA_ROOT` and, ONLY after
proving the remote copy is byte-identical (SHA-256 match against an
independently downloaded copy) AND a structurally valid archive
(`tar -tf` on the recovered copy), writes `media_backup_success.json`
(O.5e-2). Never modifies or deletes anything under `MEDIA_ROOT` — the
archive is a scratch artifact only, deleted on every exit path. No
individual filename (KYC documents included) is ever logged or written
into the manifest.

### Media offsite verification (automated — systemd timer, O.5e-3)

Daily at ~04:00 UTC (30 minutes after `backup-offsite.timer`'s ~03:30
UTC fire) — deliberately its own independent timer, with no
`Requires=`/`After=` dependency on any Postgres backup unit. See step
11a in Initial Setup above for installation, and
`deploy/systemd/backup-media-offsite.service` /
`backup-media-offsite.timer` for the full unit definitions and rationale.

```bash
systemctl status backup-media-offsite.timer
systemctl list-timers backup-media-offsite.timer
journalctl -u backup-media-offsite.service -n 50
```

Media backup freshness is surfaced at `GET /api/health/detail/` →
`"media_backup"` (O.5e-3) — `stale`/`invalid` always degrade the
endpoint to 503; `missing`/`not_configured` degrade it only when
`APP_ENV=production` (same policy as `"offsite_backup"`). Only
status/age/threshold/last-success-timestamp are exposed — never a
filename, remote target, or sha256.

**`MEDIA_ROOT` remains private regardless of this scheduler.** Backing
it up offsite does not change how it is served: KYC documents, Treasury
evidence, and Broker documents are reachable exclusively through the
authenticated/authorized Django routes under `/secure-media/...`
(O.5e-1) — no `location /media/` exists in Nginx, and none should ever
be added.

### PostgreSQL restore drill (manual, O.5d-1/O.5d-2/O.5d-3)

```bash
# From the local dump (O.5a) — useful development/staging diagnostic:
sudo -u trx_sim bash -c 'set -a; source /opt/trx_sim/.env; set +a; \
  bash /opt/trx_sim/deploy/scripts/restore_drill.sh --source local'

# From the verified OFFSITE copy (O.5c-1) — the ONLY evidence that
# counts as Production Restore READY:
sudo -u trx_sim bash -c 'set -a; source /opt/trx_sim/.env; set +a; \
  bash /opt/trx_sim/deploy/scripts/restore_drill.sh --source offsite'
```

Proves a backup is actually restorable — not just parseable — by
restoring it into a throwaway, uniquely-named temporary database
(`trx_restore_drill_<timestamp>_<random>`, generated internally, never
overridable) on the same PostgreSQL server, running read-only
structural checks against it (connectivity, critical tables,
`django_migrations` populated, AND `manage.py migrate --check --plan`
— O.5d-3 — confirming the schema is fully compatible with this
codebase's current migration graph, never applying anything), then
destroying it. Runs as the dedicated `trx_sim_drill` role (step 12 in
Initial Setup) — never as `trx_sim`, and structurally unable to read,
write, own, or drop the real database regardless of any bug in the
script's own name guards.

`--source offsite` uses EXCLUSIVELY the durable evidence already
produced by O.5c-1 (`offsite_success.json`) to determine what to
fetch — there is no way to supply a remote filename, path, or expected
checksum manually. It downloads a **fresh, independent copy** from the
offsite remote (never substituting a local dump even if one with the
same name still exists), verifies its size and SHA-256 against
`offsite_success.json` exactly, and only then restores and checks it —
proving recovery is possible using *only* what survives outside the
VPS. A `--source local` success is a useful diagnostic; only a
`--source offsite` success demonstrates the VPS could actually be
rebuilt from nothing.

Deliberately **manual only** — no systemd timer, no cron, no Celery
schedule (O.5d Fase 0 approval, decision 3): this repo documents the
restore drill as a gate before real production, not as a recurring
cadence, and the operation is heavier and touches more sensitive data
than the backup jobs above.

```bash
cat /var/log/trx_sim/backup/restore_drill_success.json   # after a run
# "source": "offsite" plus the full offsite_* entries in
# "checks_passed" is what "Restore READY" means — a "local" success
# alone does not.
```

Not yet integrated into `GET /api/health/detail/` — no cadence is
documented to derive a non-arbitrary staleness threshold from (O.5d
Fase 0 §10).

### Media offsite restore drill (manual, O.5e-4)

```bash
sudo -u trx_sim bash -c 'set -a; source /opt/trx_sim/.env; set +a; \
  bash /opt/trx_sim/deploy/scripts/media_restore_drill.sh'
```

Proves the offsite media archive (O.5e-2) is actually recoverable and
extractable — not just uploaded — by downloading it fresh into a
throwaway, uniquely-named scratch directory
(`trx_media_restore_drill_<timestamp>_<random>`, generated internally,
never overridable), verifying its size and SHA-256 against
`media_backup_success.json` exactly, validating the archive structurally
(`tar -tf`), extracting it, and verifying the extracted `file_count`
matches the manifest and that every extracted entry (including symlinks)
stays contained inside the scratch directory. **Never restores onto,
writes into, or reads from the real `MEDIA_ROOT`** — it is consulted only
to confirm the scratch directory is not it and not inside it, before
that directory is ever created. There is no "local" mode: media backups
keep no local copy to drill against (O.5e-2), so the offsite manifest is
the only, and therefore mandatory, source.

```bash
cat /var/log/trx_sim/backup/media_restore_drill_success.json   # after a run
# "checks_passed" containing "download", "sha256_match", "tar_valid",
# "extracted", "no_symlink_escape", and "file_count_match" together is
# what a genuine restore-verified media backup means. No filename, path,
# or checksum value is ever written into this file — see the script's
# own header for the exact field list.
```

Deliberately **manual only** — no systemd timer, no cron (O.5e-4
approval, same rationale as the Postgres restore drill): this is a
production-readiness gate, not a recurring cadence. **RC requires at
least one real, successful offsite media restore drill before
launch** — a `media_backup_success.json` alone proves the archive was
uploaded and verified in place; only a successful
`media_restore_drill.sh` run proves it can actually be recovered and
extracted from nothing.

Not integrated into `GET /api/health/detail/`, for the same reason as
the Postgres restore drill (O.5d Fase 0 §10): no cadence to derive a
non-arbitrary staleness threshold from.

### Redis

Redis persistence is configured via `deploy/redis_persistence.conf`:
- **AOF** (`appendonly yes`, `appendfsync everysec`): ~1s durability
- **RDB**: snapshot every 15 min (if ≥1 write)

Backup Redis data directory:

```bash
# Stop redis briefly for a consistent copy, or use BGSAVE:
redis-cli BGSAVE
cp /var/lib/redis/trx_sim_dump.rdb /var/backups/trx_sim/redis_$(date +%Y%m%d_%H%M%S).rdb
```

---

## Load Testing

### Prerequisites

```bash
pip install -r load_tests/requirements-load-test.txt

# Create test users (run once)
python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
for i in range(1, 21):
    u, _ = U.objects.get_or_create(username=f'loadtest_{i}')
    u.set_password('LoadTest123!')
    u.save()
staff, _ = U.objects.get_or_create(username='loadtest_staff')
staff.set_password('LoadTest123!')
staff.is_staff = True
staff.save()
print('Done')
"

# Add to .env before running load tests:
# LOAD_TEST_MODE=True
# Restart Daphne after adding it.
```

### Run load tests

```bash
# HTTP load test (headless, 5 min):
bash load_tests/run_load_test.sh --type http --headless --users 50 --spawn-rate 5 --run-time 5m

# WebSocket — connect scenario:
bash load_tests/run_load_test.sh --type ws --scenario connect --users 30 --ticks 30

# WebSocket — sustained (2 min per user):
bash load_tests/run_load_test.sh --type ws --scenario sustained --users 20 --duration 120

# WebSocket — reconnect storm:
bash load_tests/run_load_test.sh --type ws --scenario reconnect --users 50 --reconnects 10
```

### After load tests

```bash
# Disable LOAD_TEST_MODE in .env and restart Daphne
# Reset peak metrics:
python manage.py reset_peaks --confirm
```

---

## Monitoring

| What | Where |
|------|-------|
| Ops panel | `https://yourdomain.com/staff/ops/` |
| Metrics JSON | `GET /api/metrics/` (staff only) |
| Broker monitoring | `GET /api/broker/monitoring/` (staff only) |
| App logs | `/var/log/trx_sim/app.log` |
| Deploy log | `/var/log/trx_sim/deploy.log` |
| Backup log | `/var/log/trx_sim/backup.log` |
| systemd logs | `journalctl -u daphne / celery-worker / celery-beat` |
| Redis slow log | `redis-cli SLOWLOG GET 10` |
| PG slow queries | `pg_stat_statements` (enable extension) |

---

## Security Notes

- Never commit `.env` to version control
- `TOTP_ENCRYPTION_KEY` must be a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `SECRET_KEY` must be 50+ chars, random: `python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `LOAD_TEST_MODE=True` must NEVER be set in production — it disables all rate limiting
- Daphne binds to `127.0.0.1` only — Nginx is the only public-facing server
- All HTTP→HTTPS redirect is handled by Nginx (`return 301 https://...`)
- AuditLog rows are append-only (no update/delete) and retained 30 days
