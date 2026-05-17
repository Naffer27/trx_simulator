#!/usr/bin/env bash
# ============================================================
# restore_postgres.sh — Restore a pg_dump custom-format backup
#
# DESTRUCTIVE: drops and recreates all objects in the target database.
# Always run a fresh backup of the CURRENT state before restoring.
#
# Usage:
#   bash deploy/scripts/restore_postgres.sh /var/backups/trx_sim/trx_sim_staging_20260517_030000.dump
#
# Environment:
#   DB_NAME  — target database  (default: trx_sim_staging)
#   DB_USER  — database user    (default: trx_sim)
#   DB_HOST  — DB host          (default: 127.0.0.1)
#   DB_PORT  — DB port          (default: 5432)
# ============================================================
set -euo pipefail

DUMP_FILE="${1:-}"

DB_NAME="${DB_NAME:-trx_sim_staging}"
DB_USER="${DB_USER:-trx_sim}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; exit 1; }
warn() { log "WARNING: $*"; }

# ── Pre-flight checks ─────────────────────────────────────────
if [ -z "$DUMP_FILE" ]; then
    die "Usage: $0 <path-to-dump-file>"
fi

if [ ! -f "$DUMP_FILE" ]; then
    die "Dump file not found: $DUMP_FILE"
fi

log "=== PostgreSQL restore starting ==="
log "Source:   $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"
log "Target:   $DB_NAME @ $DB_HOST:$DB_PORT as $DB_USER"

# ── Verify dump before touching the DB ───────────────────────
log "Verifying dump integrity before restore..."
pg_restore --list "$DUMP_FILE" > /dev/null || die "Dump file appears corrupt — aborting"
log "Dump integrity: PASSED"

# ── Confirm ──────────────────────────────────────────────────
warn "This will DROP and RECREATE all objects in database '$DB_NAME'."
warn "Existing data will be PERMANENTLY LOST."
read -r -p "Type YES to continue: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
    log "Restore cancelled by user."
    exit 0
fi

# ── Stop app services to prevent writes during restore ────────
log "Stopping application services..."
if systemctl is-active --quiet daphne 2>/dev/null; then
    sudo systemctl stop daphne
    log "  Stopped: daphne"
fi
if systemctl is-active --quiet celery-worker 2>/dev/null; then
    sudo systemctl stop celery-worker
    log "  Stopped: celery-worker"
fi
if systemctl is-active --quiet celery-beat 2>/dev/null; then
    sudo systemctl stop celery-beat
    log "  Stopped: celery-beat"
fi

# ── Restore ──────────────────────────────────────────────────
log "Restoring database..."
pg_restore \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    --format=custom \
    --clean \
    --if-exists \
    --no-password \
    --verbose \
    -d "$DB_NAME" \
    "$DUMP_FILE" || die "pg_restore failed — database may be in a partially restored state"

log "Restore complete."

# ── Restart app services ──────────────────────────────────────
log "Restarting application services..."
sudo systemctl start celery-beat  && log "  Started: celery-beat"
sleep 3
sudo systemctl start celery-worker && log "  Started: celery-worker"
sleep 3
sudo systemctl start daphne        && log "  Started: daphne"
sleep 3

# ── Health check ──────────────────────────────────────────────
log "Running health check..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$SCRIPT_DIR/healthcheck.sh" || warn "Health check failed — verify manually"

log "=== Restore complete from: $DUMP_FILE ==="
