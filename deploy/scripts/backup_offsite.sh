#!/usr/bin/env bash
# ============================================================
# backup_offsite.sh — verified offsite copy of the latest local
# PostgreSQL backup (O.5c-1)
#
# Reads backup_success.json (O.5a, written by backup_postgres.sh),
# uploads the dump it references to a remote configured in a dedicated
# rclone.conf, and ONLY declares the copy durable after proving —
# not assuming — that the remote bytes are byte-identical AND
# restorable:
#
#   1. SHA-256 of the local dump.
#   2. Upload (rclone copyto).
#   3. Confirm the remote object actually exists (rclone lsf).
#   4. Download it back to an INDEPENDENT scratch file (never reuses
#      the local dump path) — this is what actually proves the bytes
#      are recoverable from the remote, not just that a copy command
#      returned exit 0.
#   5. SHA-256 of the recovered bytes — must match #1 EXACTLY.
#   6. pg_restore --list on the RECOVERED scratch copy — proves the
#      remote bytes are a restorable dump, not just a byte-identical
#      blob (a bit-perfect copy of a corrupt file would pass step 5
#      and still be useless).
#
# Only after all six steps pass is offsite_success.json written.
# Any failure at any step writes offsite_failure.json instead and
# NEVER touches offsite_success.json.
#
# Deliberately independent of backup_postgres.sh (O.5c Fase 0 §C /
# approved decision 4):
#   - Never invoked BY backup_postgres.sh and never invokes it.
#   - Never writes to, reads for writing, or deletes backup_success.json
#     / backup_failure.json (O.5a) — this script's failures can never
#     turn a valid local backup into a reported failure.
#   - Never deletes or modifies anything under BACKUP_DIR — the local
#     dump is read-only input here. The ONLY file this script ever
#     deletes is its own scratch download (and its own metadata temp
#     files), tracked explicitly and cleaned up on every exit path.
#   - Own flock domain (.offsite.lock) — never contends with
#     backup_postgres.sh's own .backup.lock.
#
# Provider-agnostic by construction: no S3/R2/B2-specific code or
# hardcoded bucket/endpoint anywhere in this script. RCLONE_REMOTE is
# just the name of a remote already defined in RCLONE_CONFIG — the
# actual provider lives entirely in that (gitignored, chmod 600,
# never-committed) config file. Initial operational target is
# Cloudflare R2 (O.5c Fase 0 approval, decision 1), but that choice is
# expressed only in rclone.conf, never in this script.
#
# No custom bash-level retry loop across a whole run and no bash-level
# `timeout` wrapper (GNU coreutils `timeout` is not guaranteed present
# on every host and is not used elsewhere in deploy/scripts/) — same
# precedent already established by backup_postgres.sh not wrapping
# pg_dump: the outer time bound is TimeoutStartSec on
# backup-offsite.service (O.5c-2), and the outer retry cadence is
# tomorrow's timer fire, not an immediate re-attempt against a
# possibly still-broken remote. rclone's own default internal retry
# (a handful of attempts per operation) already absorbs brief
# transient network blips within a single step.
#
# JSON field extraction from backup_success.json uses grep/sed, not a
# JSON library — deliberately, to keep this script free of any
# Python/venv dependency (same rationale backup-postgres.service
# already documents for itself: an offsite copy must be able to run
# even if the application layer/venv is broken). This is safe here
# specifically because backup_success.json is not arbitrary input — it
# is generated exclusively by backup_postgres.sh's own fixed
# one-key-per-line heredoc, never hand-edited or produced by any other
# process.
#
# Usage:
#   bash deploy/scripts/backup_offsite.sh
#
# Environment:
#   BACKUP_DIR             — where the local dump referenced by
#                             backup_success.json lives (default:
#                             /var/backups/trx_sim)
#   BACKUP_METADATA_PATH   — O.5a/O.5c signal + lock directory (default:
#                             /var/log/trx_sim/backup/) — same directory
#                             O.5a already uses; no new filesystem
#                             permission needed.
#   RCLONE_CONFIG          — path to the DEDICATED rclone config file
#                             (default: /etc/trx_sim/rclone.conf).
#                             Never inside this repo, never inside
#                             /opt/trx_sim, chmod 600, provisioned
#                             manually — see deploy/rclone.conf.example.
#   RCLONE_REMOTE           — name of the remote defined inside
#                             RCLONE_CONFIG (no default — must be set
#                             explicitly; refuses to guess a provider).
#   RCLONE_REMOTE_PATH      — path/prefix on that remote under which
#                             dumps are stored (default:
#                             trx-sim-backups).
#   OFFSITE_SCRATCH_DIR     — directory for the independent verification
#                             download (default: system tmp via mktemp;
#                             under systemd this is already private via
#                             PrivateTmp=true on backup-offsite.service,
#                             O.5c-2).
# ============================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/trx_sim}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH:-/var/log/trx_sim/backup/}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH%/}"

RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/trx_sim/rclone.conf}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
RCLONE_REMOTE_PATH="${RCLONE_REMOTE_PATH:-trx-sim-backups}"
OFFSITE_SCRATCH_DIR="${OFFSITE_SCRATCH_DIR:-}"

SUCCESS_MANIFEST="${BACKUP_METADATA_PATH}/backup_success.json"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── durable metadata helpers (own copy — deliberately NOT shared with
# backup_postgres.sh, see header rationale: keeping the two scripts
# fully independent means a bug in one's metadata writer can never
# affect the other's) ────────────────────────────────────────────────
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

# Extracts a top-level value for "key" from a JSON file generated by
# OUR OWN fixed one-key-per-line heredoc format (backup_postgres.sh /
# this script itself) — NOT a general-purpose JSON parser, must never
# be pointed at arbitrary/untrusted JSON.
_json_field() {
    local file="$1" key="$2"
    grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"\|\"${key}\"[[:space:]]*:[[:space:]]*[^,}[:space:]]*" "$file" 2>/dev/null \
        | head -1 \
        | sed -E "s/\"${key}\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"\$//"
}

_write_metadata_atomic() {
    local target="$1" content="$2" tmp
    tmp="$(mktemp "${BACKUP_METADATA_PATH}/.metadata.XXXXXX")" || return 1
    _TMP_FILES+=("$tmp")
    chmod 640 "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    printf '%s\n' "$content" > "$tmp" || { rm -f "$tmp"; return 1; }
    sync
    mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
    return 0
}

_write_offsite_failure() {
    local reason="$1" ts hostn content
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    hostn=$(hostname 2>/dev/null || echo unknown)
    content=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$ts",
  "database_name": "$(_json_escape "${DATABASE_NAME:-unknown}")",
  "filename": $( [ -n "${DUMP_FILENAME:-}" ] && printf '"%s"' "$(_json_escape "$DUMP_FILENAME")" || printf 'null' ),
  "reason": "$(_json_escape "$reason")",
  "hostname": "$(_json_escape "$hostn")"
}
JSON
)
    # Best-effort only — must never mask the ORIGINAL error, and must
    # NEVER touch offsite_success.json, backup_success.json, or
    # backup_failure.json.
    _write_metadata_atomic "${BACKUP_METADATA_PATH}/offsite_failure.json" "$content" 2>/dev/null || true
}

die() { log "ERROR: $*"; _write_offsite_failure "$*"; exit 1; }

# Own flock domain — never contends with backup_postgres.sh's
# .backup.lock. Lock contention is not a failure (another offsite run
# is already legitimately in progress) — exit 0, touch nothing.
_exit_lock_contention() {
    log "Another backup_offsite.sh run is already in progress (lock: $LOCK_FILE) — exiting without touching any metadata."
    exit 0
}

log "=== PostgreSQL offsite verification starting ==="

mkdir -p "$BACKUP_METADATA_PATH" || die "Cannot create backup metadata dir: $BACKUP_METADATA_PATH"

# Checked BEFORE the flock attempt below on purpose: if flock itself is
# missing, `flock -n 201` fails with a bash "command not found" (exit
# 127), which `|| _exit_lock_contention` would otherwise misreport as
# "another run is already in progress" (a clean exit 0, no failure
# recorded) — silently hiding a real missing-dependency failure behind
# a code path that is deliberately not supposed to be a failure at all.
command -v flock >/dev/null 2>&1 || die "flock binary not found on PATH"

LOCK_FILE="${BACKUP_METADATA_PATH}/.offsite.lock"
exec 201>"$LOCK_FILE"
flock -n 201 || _exit_lock_contention

# ── Preconditions ──────────────────────────────────────────────────
command -v rclone >/dev/null 2>&1 || die "rclone binary not found on PATH"
command -v pg_restore >/dev/null 2>&1 || die "pg_restore binary not found on PATH"

[ -n "$RCLONE_REMOTE" ] || die "RCLONE_REMOTE is not set — refusing to guess a provider/remote"
[ -f "$RCLONE_CONFIG" ] || die "RCLONE_CONFIG file not found: $RCLONE_CONFIG"

# ── Read and validate the local success manifest (O.5a) ────────────
# Read-only with respect to O.5a — this script never writes to
# backup_success.json/backup_failure.json under any circumstance.
[ -f "$SUCCESS_MANIFEST" ] || die "No backup_success.json at $SUCCESS_MANIFEST — nothing verified-local to offsite yet"

INTEGRITY_VERIFIED=$(_json_field "$SUCCESS_MANIFEST" "integrity_verified")
[ "$INTEGRITY_VERIFIED" = "true" ] || die "backup_success.json does not report integrity_verified=true — refusing to offsite an unverified dump"

DUMP_FILENAME=$(_json_field "$SUCCESS_MANIFEST" "filename")
[ -n "$DUMP_FILENAME" ] || die "backup_success.json has no usable 'filename' field"

DATABASE_NAME=$(_json_field "$SUCCESS_MANIFEST" "database_name")
RECORDED_SIZE_BYTES=$(_json_field "$SUCCESS_MANIFEST" "size_bytes")

DUMP_FILE="${BACKUP_DIR%/}/${DUMP_FILENAME}"
[ -f "$DUMP_FILE" ] || die "Dump file referenced by backup_success.json not found on disk: $DUMP_FILE (pruned by retention before offsite ran?)"

CURRENT_SIZE_BYTES=$(wc -c < "$DUMP_FILE" | tr -d ' ')
if [ -n "$RECORDED_SIZE_BYTES" ] && [ "$CURRENT_SIZE_BYTES" != "$RECORDED_SIZE_BYTES" ]; then
    die "Local dump size ($CURRENT_SIZE_BYTES bytes) no longer matches backup_success.json's recorded size_bytes ($RECORDED_SIZE_BYTES) — possible tampering or truncation, refusing to offsite"
fi

log "Offsiting: $DUMP_FILE (database=$DATABASE_NAME, $CURRENT_SIZE_BYTES bytes)"

# ── Step 1: SHA-256 of the local dump ───────────────────────────────
SHA256_LOCAL=$(sha256sum "$DUMP_FILE" | awk '{print $1}')
[ -n "$SHA256_LOCAL" ] || die "Failed to compute local SHA-256 for $DUMP_FILE"
log "Local SHA-256: $SHA256_LOCAL"

REMOTE_TARGET="${RCLONE_REMOTE}:${RCLONE_REMOTE_PATH}/${DUMP_FILENAME}"

# ── Step 2: upload ──────────────────────────────────────────────────
log "Uploading to $REMOTE_TARGET ..."
rclone --config "$RCLONE_CONFIG" copyto "$DUMP_FILE" "$REMOTE_TARGET" \
    || die "rclone upload failed: $DUMP_FILE -> $REMOTE_TARGET"

# ── Step 3: confirm the remote object actually exists ──────────────
log "Confirming remote object exists ..."
rclone --config "$RCLONE_CONFIG" lsf "${RCLONE_REMOTE}:${RCLONE_REMOTE_PATH}/" --files-only 2>/dev/null \
    | grep -qx "$DUMP_FILENAME" \
    || die "Uploaded object not found on remote listing after upload: $REMOTE_TARGET"

# ── Step 4: download to an INDEPENDENT scratch file ─────────────────
# Never reuses $DUMP_FILE's path — this is what actually proves the
# bytes are recoverable FROM THE REMOTE, not merely that a copy
# command exited 0.
SCRATCH_MKTEMP_ARGS=(offsite_verify.XXXXXX)
[ -n "$OFFSITE_SCRATCH_DIR" ] && SCRATCH_MKTEMP_ARGS=(-p "$OFFSITE_SCRATCH_DIR" "${SCRATCH_MKTEMP_ARGS[@]}")
SCRATCH_FILE=$(mktemp "${SCRATCH_MKTEMP_ARGS[@]}") || die "Failed to create independent scratch file for download verification"
_TMP_FILES+=("$SCRATCH_FILE")

log "Downloading to independent scratch file for verification: $SCRATCH_FILE ..."
rclone --config "$RCLONE_CONFIG" copyto "$REMOTE_TARGET" "$SCRATCH_FILE" \
    || die "rclone download-for-verification failed: $REMOTE_TARGET -> $SCRATCH_FILE"

# ── Step 5: SHA-256 of the recovered bytes — must match EXACTLY ────
SHA256_RECOVERED=$(sha256sum "$SCRATCH_FILE" | awk '{print $1}')
log "Recovered SHA-256: $SHA256_RECOVERED"

if [ "$SHA256_RECOVERED" != "$SHA256_LOCAL" ]; then
    die "SHA-256 mismatch: local=$SHA256_LOCAL recovered=$SHA256_RECOVERED — remote copy is NOT byte-identical, refusing to mark offsite success"
fi
log "Checksum verification PASSED (local == recovered from remote)"

# ── Step 6: restorability of the RECOVERED copy ─────────────────────
# Deliberately runs against $SCRATCH_FILE (the bytes that actually
# came back from the remote), not $DUMP_FILE — a bit-perfect copy of
# an already-corrupt file would pass step 5 and still be useless.
log "Verifying restorability of the recovered copy (pg_restore --list) ..."
pg_restore --list "$SCRATCH_FILE" > /dev/null \
    || die "pg_restore --list failed on the RECOVERED remote copy — remote copy is not restorable despite matching checksum"
log "Restorability verification PASSED"

# ── Write durable offsite success signal ─────────────────────────────
# Only reached after upload + existence + checksum match + restorability
# all passed. Deliberately NOT sourced from backup_success.json's own
# recorded values for sha256/restorability — these are freshly proven
# by THIS run against THIS remote copy.
TIMESTAMP_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
HOSTNAME_VAL=$(hostname 2>/dev/null || echo unknown)

SUCCESS_JSON=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$TIMESTAMP_UTC",
  "database_name": "$(_json_escape "$DATABASE_NAME")",
  "filename": "$(_json_escape "$DUMP_FILENAME")",
  "size_bytes": $CURRENT_SIZE_BYTES,
  "sha256": "$(_json_escape "$SHA256_LOCAL")",
  "restorability_verified": true,
  "remote_target": "$(_json_escape "${RCLONE_REMOTE}:${RCLONE_REMOTE_PATH}/${DUMP_FILENAME}")",
  "hostname": "$(_json_escape "$HOSTNAME_VAL")"
}
JSON
)

_write_metadata_atomic "${BACKUP_METADATA_PATH}/offsite_success.json" "$SUCCESS_JSON" \
    || die "All verification steps passed but writing offsite_success.json failed — treating this run as failed"

log "Offsite durability signal written: ${BACKUP_METADATA_PATH}/offsite_success.json"

# ── Cleanup: only the scratch download, nothing under BACKUP_DIR ────
rm -f "$SCRATCH_FILE"
log "=== Offsite verification complete: $DUMP_FILENAME ==="
