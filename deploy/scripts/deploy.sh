#!/usr/bin/env bash
# ============================================================
# deploy.sh — Incremental deploy for trx_sim
#
# Run as: sudo -u trx_sim bash deploy/scripts/deploy.sh
# Assumes: virtualenv at /opt/trx_sim/venv, .env at /opt/trx_sim/.env
#
# Steps:
#   1. Pull latest code
#   2. Install/upgrade Python dependencies
#   3. Run database migrations
#   4. Collect static files
#   5. Restart services in safe order (Beat → Worker → Daphne)
#   6. Health check
# ============================================================
set -euo pipefail

APP_DIR=/opt/trx_sim
VENV=$APP_DIR/venv
PYTHON=$VENV/bin/python
MANAGE="$PYTHON $APP_DIR/manage.py"
LOG=/var/log/trx_sim/deploy.log

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

cd "$APP_DIR" || die "Cannot cd to $APP_DIR"

log "=== Deploy started ==="
log "Git: $(git rev-parse --short HEAD) on $(git branch --show-current)"

# ── 1. Pull code ─────────────────────────────────────────────
log "Pulling latest code..."
git pull --ff-only || die "git pull failed — resolve conflicts manually"

# ── 2. Dependencies ──────────────────────────────────────────
log "Installing Python dependencies..."
$VENV/bin/pip install -q -r requirements.txt || die "pip install failed"

# ── 3. Migrations ────────────────────────────────────────────
log "Running database migrations..."
$MANAGE migrate --noinput || die "migrate failed — check DB connection and migration state"

# ── 4. Static files ──────────────────────────────────────────
log "Collecting static files..."
$MANAGE collectstatic --noinput --clear -v 0 || die "collectstatic failed"

# ── 5. Restart services ───────────────────────────────────────
log "Restarting Beat scheduler..."
sudo systemctl restart celery-beat || die "celery-beat restart failed"
sleep 5   # allow redBeat lock to release before workers restart

log "Restarting Celery workers..."
sudo systemctl restart celery-worker || die "celery-worker restart failed"
sleep 3

log "Restarting Daphne (ASGI server)..."
sudo systemctl restart daphne || die "daphne restart failed"
sleep 3

# ── 6. Health check ──────────────────────────────────────────
log "Running health check..."
bash "$APP_DIR/deploy/scripts/healthcheck.sh" || die "health check FAILED after deploy"

log "=== Deploy completed successfully ==="
log "Version: $(git describe --tags --always)"
