#!/usr/bin/env bash
# ============================================================
# backup_postgres.sh — pg_dump custom-format backup for trx_sim
#
# Creates a timestamped .dump file, verifies it with pg_restore --list,
# and prunes old backups (keeps KEEP_BACKUPS most recent).
#
# O.5a: after a verified-successful dump, writes a durable JSON success
# signal (backup_success.json) under BACKUP_METADATA_PATH — read by
# simulator/backup_monitoring.py and surfaced at GET /api/health/detail/.
# Any failure writes a separate backup_failure.json — it NEVER touches
# or replaces backup_success.json (a failed run must never erase the
# evidence of the last real success).
#
# Usage:
#   bash deploy/scripts/backup_postgres.sh
#   BACKUP_DIR=/mnt/backups bash deploy/scripts/backup_postgres.sh
#
# Cron example (daily at 03:00 UTC):
#   0 3 * * * trx_sim bash /opt/trx_sim/deploy/scripts/backup_postgres.sh >> /var/log/trx_sim/backup.log 2>&1
#
# Environment (read from .env or shell):
#   DB_NAME              — database name        (default: trx_sim_staging)
#   DB_USER              — database user        (default: trx_sim)
#   DB_HOST              — DB host              (default: 127.0.0.1)
#   DB_PORT              — DB port              (default: 5432)
#   BACKUP_DIR           — dump output dir      (default: /var/backups/trx_sim)
#   KEEP_BACKUPS          — files to keep         (default: 14)
#   BACKUP_METADATA_PATH — O.5a signal dir       (default: /var/log/trx_sim/backup/)
# ============================================================
set -euo pipefail

DB_NAME="${DB_NAME:-trx_sim_staging}"
DB_USER="${DB_USER:-trx_sim}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/trx_sim}"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH:-/var/log/trx_sim/backup/}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH%/}"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
FILENAME="${DB_NAME}_${TIMESTAMP}.dump"
FILEPATH="$BACKUP_DIR/$FILENAME"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── O.5a — durable metadata helpers ────────────────────────────
# Temp files are tracked here and cleaned up on any exit path (normal
# or error) — a successfully mv'd temp file no longer exists at its
# tracked path by the time this trap runs, so rm -f on it is a no-op.
_TMP_FILES=()
_cleanup_tmp() {
    local f
    for f in "${_TMP_FILES[@]:-}"; do
        [ -n "$f" ] && rm -f "$f" 2>/dev/null
    done
    return 0
}
trap _cleanup_tmp EXIT

_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n'
}

# Atomic write: temp file in the SAME directory as the target (same
# filesystem → mv is a real rename(2), not a copy), permissions set
# BEFORE the rename (never a window where the final file has the wrong
# mode), sync before the rename for durability against a crash mid-write.
_write_metadata_atomic() {
    local target="$1" content="$2" tmp
    mkdir -p "$BACKUP_METADATA_PATH" 2>/dev/null || return 1
    tmp="$(mktemp "${BACKUP_METADATA_PATH}/.metadata.XXXXXX")" || return 1
    _TMP_FILES+=("$tmp")
    chmod 640 "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    printf '%s\n' "$content" > "$tmp" || { rm -f "$tmp"; return 1; }
    sync
    mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
    return 0
}

_write_failure_metadata() {
    local reason="$1" ts hostn content
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    hostn=$(hostname 2>/dev/null || echo unknown)
    content=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$ts",
  "database_name": "$(_json_escape "$DB_NAME")",
  "reason": "$(_json_escape "$reason")",
  "hostname": "$(_json_escape "$hostn")"
}
JSON
)
    # Best-effort only — a failure here must never mask the ORIGINAL
    # error that triggered die() in the first place, and must never
    # touch backup_success.json.
    _write_metadata_atomic "${BACKUP_METADATA_PATH}/backup_failure.json" "$content" 2>/dev/null || true
}

die()  { log "ERROR: $*"; _write_failure_metadata "$*"; exit 1; }

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

# ── O.5a — write durable success metadata ──────────────────────
# Only reached after both pg_dump AND pg_restore --list have succeeded
# (O.5a Fase 0 §3/§5 — approved condition). A failure to write this
# signal fails the whole backup run (die()) — a backup that cannot
# prove itself durably is, from a monitoring standpoint, indistinguishable
# from a backup that never happened.
log "Writing backup durability signal (O.5a)..."
SIZE_BYTES=$(wc -c < "$FILEPATH" | tr -d ' ')
TIMESTAMP_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
HOSTNAME_VAL=$(hostname 2>/dev/null || echo unknown)

SUCCESS_JSON=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$TIMESTAMP_UTC",
  "database_name": "$(_json_escape "$DB_NAME")",
  "filename": "$(_json_escape "$FILENAME")",
  "size_bytes": $SIZE_BYTES,
  "integrity_verified": true,
  "hostname": "$(_json_escape "$HOSTNAME_VAL")"
}
JSON
)

_write_metadata_atomic "${BACKUP_METADATA_PATH}/backup_success.json" "$SUCCESS_JSON" \
    || die "Backup dump succeeded but writing the durability signal to $BACKUP_METADATA_PATH failed — treating this run as failed"
log "Durability signal written: $BACKUP_METADATA_PATH/backup_success.json"

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
