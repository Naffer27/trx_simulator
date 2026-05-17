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

### 10. Configure Nginx

```bash
cp /opt/trx_sim/deploy/nginx/trx_sim.conf /etc/nginx/sites-available/trx_sim
ln -s /etc/nginx/sites-available/trx_sim /etc/nginx/sites-enabled/trx_sim
rm -f /etc/nginx/sites-enabled/default

# Configure SSL (obtain certificate first):
certbot --nginx -d yourdomain.com --non-interactive --agree-tos -m admin@yourdomain.com

nginx -t && systemctl restart nginx
```

### 11. Configure logrotate

```bash
cp /opt/trx_sim/deploy/logrotate/trx_sim /etc/logrotate.d/trx_sim
```

### 12. Create log directory

```bash
mkdir -p /var/log/trx_sim
chown trx_sim:trx_sim /var/log/trx_sim
```

### 13. Start all services

```bash
systemctl start celery-beat
sleep 5
systemctl start celery-worker
sleep 3
systemctl start daphne
```

### 14. Verify health

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

### PostgreSQL (automated cron — add to trx_sim user crontab)

```cron
# Daily backup at 03:00 UTC, keep 14 copies
0 3 * * * bash /opt/trx_sim/deploy/scripts/backup_postgres.sh >> /var/log/trx_sim/backup.log 2>&1
```

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
