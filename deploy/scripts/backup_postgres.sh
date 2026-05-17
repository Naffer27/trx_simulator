#!/usr/bin/env bash
# ============================================================
# backup_postgres.sh — pg_dump custom-format backup for trx_sim
#
# Creates a timestamped .dump file, verifies it with pg_restore --list,
# and prunes old backups (keeps KEEP_BACKUPS most recent).
#
# Usage:
#   bash deploy/scripts/backup_postgres.sh
#   BACKUP_DIR=/mnt/backups bash deploy/scripts/backup_postgres.sh
#
# Cron example (daily at 03:00 UTC):
#   0 3 * * * trx_sim bash /opt/trx_sim/deploy/scripts/backup_postgres.sh >> /var/log/trx_sim/backup.log 2>&1
#
# Environment (read from .env or shell):
#   DB_NAME     — database name   (default: trx_sim_staging)
#   DB_USER     — database user   (default: trx_sim)
#   DB_HOST     — DB host         (default: 127.0.0.1)
#   DB_PORT     — DB port         (default: 5432)
#   BACKUP_DIR  — output dir      (default: /var/backups/trx_sim)
#   KEEP_BACKUPS — files to keep  (default: 14)
# ============================================================
set -euo pipefail

DB_NAME="${DB_NAME:-trx_sim_staging}"
DB_USER="${DB_USER:-trx_sim}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/trx_sim}"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
FILENAME="${DB_NAME}_${TIMESTAMP}.dump"
FILEPATH="$BACKUP_DIR/$FILENAME"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; exit 1; }

log "=== PostgreSQL backup starting ==="
log "Database: $DB_NAME @ $DB_HOST:$DB_PORT"
log "Output:   $FILEPATH"

# ── Create backup dir ─────────────────────────────────────────
mkdir -p "$BACKUP_DIR" || die "Cannot create backup dir: $BACKUP_DIR"

# ── Dump database ─────────────────────────────────────────────
pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    --format=custom \
    --no-password \
    --verbose \
    "$DB_NAME" \
    > "$FILEPATH" || die "pg_dump failed — check DB connection and credentials"

log "Dump complete: $(du -sh "$FILEPATH" | cut -f1) written to $FILEPATH"

# ── Verify dump integrity ─────────────────────────────────────
log "Verifying dump integrity..."
pg_restore --list "$FILEPATH" > /dev/null || die "Dump verification failed — file may be corrupt"
log "Integrity check PASSED"

# ── Prune old backups ─────────────────────────────────────────
EXISTING=$(ls -t "$BACKUP_DIR"/${DB_NAME}_*.dump 2>/dev/null | wc -l)
if [ "$EXISTING" -gt "$KEEP_BACKUPS" ]; then
    TO_DELETE=$(ls -t "$BACKUP_DIR"/${DB_NAME}_*.dump | tail -n +"$((KEEP_BACKUPS + 1))")
    echo "$TO_DELETE" | while read -r f; do
        rm -f "$f"
        log "Removed old backup: $f"
    done
fi

REMAINING=$(ls "$BACKUP_DIR"/${DB_NAME}_*.dump 2>/dev/null | wc -l)
log "Backup retention: $REMAINING / $KEEP_BACKUPS backups kept"
log "=== Backup complete: $FILENAME ==="
