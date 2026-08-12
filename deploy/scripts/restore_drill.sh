#!/usr/bin/env bash
# ============================================================
# restore_drill.sh — automated PostgreSQL restore drill (O.5d-1/O.5d-2)
#
# Proves a backup is ACTUALLY restorable — not just parseable — by
# restoring it into a throwaway, uniquely-named temporary database on
# the same PostgreSQL server, running read-only structural checks
# against it, then destroying it. NEVER touches the real database.
#
# ── --source local (O.5d-1) ─────────────────────────────────────────
# Reads backup_success.json (O.5a) and restores from the dump already
# on local disk under BACKUP_DIR.
#
# ── --source offsite (O.5d-2) ───────────────────────────────────────
# Proves recovery from the OFFSITE copy specifically — the drill that
# actually matters for Production Readiness (a local-only drill does
# not demonstrate the VPS could be rebuilt from nothing). Uses
# EXCLUSIVELY the durable evidence already produced by O.5c-1
# (offsite_success.json) as its source of truth for WHAT to fetch —
# there is no flag or env var to supply a remote filename, remote
# path, target database, or expected checksum; all of that comes from
# offsite_success.json's own "remote_target"/"filename"/"size_bytes"/
# "sha256" fields, exactly as O.5c-1 wrote them. This is a deliberate
# reuse of the EXISTING offsite contract (O.5d-2 Fase 0 finding:
# remote_target already IS a complete, ready-to-use rclone source
# path — "${RCLONE_REMOTE}:${RCLONE_REMOTE_PATH}/${DUMP_FILENAME}" —
# no second, parallel way to name the remote object was introduced).
#
# --source offsite downloads a FRESH, independent copy to scratch —
# it NEVER falls back to whatever local dump might still exist under
# BACKUP_DIR, even if the identical file happens to still be there.
# The offsite code path never reads BACKUP_DIR or backup_success.json
# at all. Before any database is touched, the freshly downloaded
# bytes must independently pass: size match against offsite_success.
# json's recorded size_bytes, SHA-256 match against its recorded
# sha256, and pg_restore --list on the RECOVERED scratch copy. Any
# mismatch writes restore_drill_failure.json and stops — no database
# is ever created on a checksum/size/list failure.
#
# A success with source="offsite" is the ONLY evidence that counts as
# "Production Restore READY" (O.5d Fase 0 approval, decision 4) — it
# demonstrates the bytes were recovered from remote storage AND those
# exact bytes were restored into a real database AND verified. A
# source="local" success is a useful development/staging diagnostic,
# never a substitute.
#
# ── Why this is a SEPARATE script from restore_postgres.sh ─────────
# restore_postgres.sh is the REAL incident-recovery tool
# (TREASURY_INCIDENT_RUNBOOK.md) — it targets the real $DB_NAME,
# requires interactive "Type YES" confirmation, and stops/restarts
# daphne/celery-worker/celery-beat. None of that is safe to automate
# unattended. restore_drill.sh shares none of that behavior: it never
# touches $DB_NAME, never prompts, never touches systemd services, and
# always operates on a brand-new database it creates and destroys
# itself.
#
# ── The temp database name ──────────────────────────────────────────
# Generated INTERNALLY as trx_restore_drill_<UTC timestamp>_<random
# hex> — there is NO --database/--dbname/--target-db flag or
# equivalent environment variable. This is a deliberate design
# decision (O.5d Fase 0 §4 approval): the only way to change what this
# script operates on is to change the code that generates the name,
# never a runtime argument.
#
# Every name is validated by _validate_drill_name() against ALL of the
# following before ANY destructive operation (createdb/dropdb) is
# allowed to run against it:
#   - matches ^trx_restore_drill_[0-9]{14}_[0-9a-f]{8}$ exactly
#   - != $DB_NAME
#   - != the effective Django TEST database name ($DB_TEST_NAME, or
#     "${DB_NAME}_test" if unset — mirrors settings.py's own default
#     exactly)
#   - does not contain "staging", "production", or "prod" (defense in
#     depth against a hypothetical future change to the generator —
#     the fixed prefix/format above can never actually produce these
#     substrings today, but the check costs nothing and never assumes
#     that stays true)
# The SAME validation function is called again, unconditionally,
# inside the cleanup trap before it ever calls dropdb — a corrupted
# variable at cleanup time can never turn into a drop of something
# unvalidated.
#
# ── PostgreSQL-level defense (independent of this script's logic) ──
# This script is designed to run as a DEDICATED, minimally-privileged
# role — see "Role provisioning" below. That role:
#   - is NOT superuser and NOT the owner of the real database, so it
#     is STRUCTURALLY unable to DROP it (Postgres requires being the
#     owner or superuser to drop a database — this is enforced by
#     Postgres itself, not by anything in this script).
#   - has NO connect privilege on the real database at all (see the
#     REVOKE below) — it cannot read a single row of real data, ever,
#     regardless of any bug in this script's own name-validation logic.
# PostgreSQL has no native way to restrict CREATEDB to a name pattern
# (O.5d Fase 0 finding, confirmed by empirical testing) — the
# trx_restore_drill_* naming rule above is enforced ENTIRELY by this
# script, not by Postgres. What Postgres DOES independently guarantee,
# regardless of any bug in the naming logic: this role can never read,
# write, or drop the real database. The worst case of a hypothetical
# guard bypass is creating unwanted databases of its own — never
# touching real data. A periodic administrative check for
# unexpectedly-named databases owned by this role is documented as a
# recommended FUTURE control, deliberately out of scope for O.5d-1.
#
# ── Role provisioning (NOT done by this script — run once manually) ─
#   -- On the Postgres server, as a superuser:
#   CREATE ROLE trx_sim_drill WITH
#       LOGIN
#       PASSWORD 'CHANGE_ME'
#       CREATEDB
#       NOSUPERUSER
#       NOCREATEROLE
#       NOREPLICATION
#       NOINHERIT
#       CONNECTION LIMIT 3;
#
#   -- CRITICAL: revoke from PUBLIC, not merely from the named role.
#   -- Postgres grants CONNECT to PUBLIC on every database by default;
#   -- REVOKE CONNECT ... FROM trx_sim_drill alone is a silent NO-OP
#   -- as long as PUBLIC still has it — empirically confirmed during
#   -- O.5d-1 Fase 0 (a REVOKE targeted only at the named role did NOT
#   -- prevent it from connecting; only revoking from PUBLIC did).
#   REVOKE CONNECT ON DATABASE trx_sim_staging FROM PUBLIC;
#
#   -- Documentation only, not strictly required: trx_sim already
#   -- retains CONNECT on its own database via ownership, independent
#   -- of the PUBLIC revoke above. Stated explicitly so a future
#   -- reader never has to wonder whether the app broke.
#   GRANT CONNECT ON DATABASE trx_sim_staging TO trx_sim;
#
# This role is NEVER superuser, NEVER owns the real database, NEVER
# granted anything on it, and NEVER made a member of trx_sim. See
# DEPLOY.md for the full provisioning step.
#
# ── Usage ────────────────────────────────────────────────────────────
#   bash deploy/scripts/restore_drill.sh --source local
#   bash deploy/scripts/restore_drill.sh --source offsite
#
# ── Environment ──────────────────────────────────────────────────────
#   BACKUP_DIR                  — where the local dump lives, --source
#                                  local only (default:
#                                  /var/backups/trx_sim)
#   BACKUP_METADATA_PATH        — O.5a/O.5c/O.5d signal + lock
#                                  directory, both sources (default:
#                                  /var/log/trx_sim/backup/)
#   DB_NAME                     — the REAL database name — read ONLY
#                                  for the safety comparison, NEVER
#                                  used as a restore target.
#   DB_TEST_NAME                — the REAL Django test database name
#                                  (optional) — same, read-only
#                                  comparison purpose.
#   DB_HOST / DB_PORT           — same Postgres SERVER as production
#                                  (default: 127.0.0.1 / 5432) — this
#                                  script deliberately has no separate
#                                  host override; reusing the real
#                                  connection target while never being
#                                  able to touch the real database
#                                  (via role privileges) is the design.
#   RESTORE_DRILL_DB_USER       — the dedicated role (default:
#                                  trx_sim_drill)
#   RESTORE_DRILL_DB_PASSWORD   — required, no default — refuses to
#                                  guess a credential.
#   RESTORE_DRILL_TIMEOUT_SECONDS — max wall-clock time for EACH of the
#                                  download (--source offsite only) and
#                                  pg_restore steps, independently
#                                  (default: 3600)
#   RCLONE_CONFIG                — --source offsite only. Same
#                                  DEDICATED, gitignored rclone config
#                                  as O.5c-1 (default:
#                                  /etc/trx_sim/rclone.conf) — this
#                                  script only ever reads the
#                                  "remote_target" value ALREADY
#                                  computed and stored by
#                                  backup_offsite.sh; it never needs
#                                  RCLONE_REMOTE/RCLONE_REMOTE_PATH
#                                  separately.
#   RESTORE_DRILL_APP_DIR        — both sources, O.5d-3. Mirrors
#                                  deploy/scripts/deploy.sh's own
#                                  APP_DIR convention (default:
#                                  /opt/trx_sim).
#   RESTORE_DRILL_PYTHON         — both sources, O.5d-3 (default:
#                                  $RESTORE_DRILL_APP_DIR/venv/bin/
#                                  python) — used ONLY to run
#                                  `manage.py migrate --check --plan`
#                                  against the DRILL database; never
#                                  `manage.py migrate` (no --check),
#                                  which would apply migrations.
#   RESTORE_DRILL_MANAGE_PY      — both sources, O.5d-3 (default:
#                                  $RESTORE_DRILL_APP_DIR/manage.py)
# ============================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/trx_sim}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH:-/var/log/trx_sim/backup/}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH%/}"

DB_NAME="${DB_NAME:-}"
DB_TEST_NAME="${DB_TEST_NAME:-}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"

RESTORE_DRILL_DB_USER="${RESTORE_DRILL_DB_USER:-trx_sim_drill}"
RESTORE_DRILL_DB_PASSWORD="${RESTORE_DRILL_DB_PASSWORD:-}"
RESTORE_DRILL_TIMEOUT_SECONDS="${RESTORE_DRILL_TIMEOUT_SECONDS:-3600}"

RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/trx_sim/rclone.conf}"

# ── Django schema compatibility check (O.5d-3) — mirrors
# deploy/scripts/deploy.sh's own APP_DIR/venv/manage.py convention.
# Applies to BOTH --source local and --source offsite. ──────────────
RESTORE_DRILL_APP_DIR="${RESTORE_DRILL_APP_DIR:-/opt/trx_sim}"
RESTORE_DRILL_PYTHON="${RESTORE_DRILL_PYTHON:-${RESTORE_DRILL_APP_DIR}/venv/bin/python}"
RESTORE_DRILL_MANAGE_PY="${RESTORE_DRILL_MANAGE_PY:-${RESTORE_DRILL_APP_DIR}/manage.py}"

SUCCESS_MANIFEST="${BACKUP_METADATA_PATH}/backup_success.json"
OFFSITE_MANIFEST="${BACKUP_METADATA_PATH}/offsite_success.json"

CHECKS_PASSED=()

_render_checks_json() {
    local first=1 item
    printf '['
    for item in "${CHECKS_PASSED[@]:-}"; do
        [ -z "$item" ] && continue
        [ "$first" = "1" ] || printf ', '
        printf '"%s"' "$(_json_escape "$item")"
        first=0
    done
    printf ']'
}

SOURCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --source)
            SOURCE="${2:-}"
            shift 2
            ;;
        --source=*)
            SOURCE="${1#--source=}"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

START_TIME=$(date +%s)

# ── durable metadata helpers (own copy — deliberately NOT shared with
# backup_postgres.sh/backup_offsite.sh, same independence rationale) ──
_TMP_FILES=()
_cleanup_tmp_files() {
    local f
    for f in "${_TMP_FILES[@]:-}"; do
        [ -n "$f" ] && rm -f "$f" 2>/dev/null
    done
    return 0
}

_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n'
}

# Extracts a top-level value for "key" from OUR OWN fixed
# one-key-per-line heredoc JSON (backup_postgres.sh's format) — NOT a
# general-purpose parser, never point this at untrusted JSON.
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

_write_drill_failure() {
    local reason="$1" ts content
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    content=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$ts",
  "source": "$(_json_escape "${SOURCE:-unknown}")",
  "dump_filename": $( [ -n "${DUMP_FILENAME:-}" ] && printf '"%s"' "$(_json_escape "$DUMP_FILENAME")" || printf 'null' ),
  "reason": "$(_json_escape "$reason")"
}
JSON
)
    _write_metadata_atomic "${BACKUP_METADATA_PATH}/restore_drill_failure.json" "$content" 2>/dev/null || true
}

# Does NOT call _final_cleanup directly — `exit 1` below already fires
# the `trap _final_cleanup EXIT` (installed further down) exactly
# once. An earlier version of this script called both, which made a
# real dropdb run TWICE — harmless in practice (the second attempt
# just fails with "database does not exist" and logs a WARNING) but
# sloppy and confusing in production logs — caught during a real
# end-to-end smoke test with a real dropdb (O.5d-3), never
# distinguished by the stubbed-dropdb subprocess tests since a
# duplicate call there has no observable side effect either way.
die() { log "ERROR: $*"; _write_drill_failure "$*"; exit 1; }

_exit_lock_contention() {
    log "Another restore_drill.sh run is already in progress (lock: $LOCK_FILE) — exiting without touching any metadata."
    exit 0
}

# ── drill database name validation — reused for BOTH the pre-create
# check and, unconditionally, inside the cleanup trap before dropdb ──
_validate_drill_name() {
    local name="$1"
    if ! [[ "$name" =~ ^trx_restore_drill_[0-9]{14}_[0-9a-f]{8}$ ]]; then
        log "REJECTED: '$name' does not match the required trx_restore_drill_ pattern"
        return 1
    fi
    if [ -n "$DB_NAME" ] && [ "$name" = "$DB_NAME" ]; then
        log "REJECTED: '$name' equals DB_NAME"
        return 1
    fi
    local effective_test_name="${DB_TEST_NAME:-${DB_NAME}_test}"
    if [ -n "$DB_NAME" ] && [ "$name" = "$effective_test_name" ]; then
        log "REJECTED: '$name' equals the Django TEST database name"
        return 1
    fi
    local lowered forbidden
    lowered=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')
    for forbidden in staging production prod; do
        if [[ "$lowered" == *"$forbidden"* ]]; then
            log "REJECTED: '$name' contains forbidden substring '$forbidden'"
            return 1
        fi
    done
    return 0
}

# ── cleanup: installed BEFORE createdb is ever attempted. Only drops
# the drill DB if it both (a) was actually created by THIS run and
# (b) re-passes _validate_drill_name at cleanup time — never trusts a
# stale/corrupted variable. Releases the lock and deletes any scratch
# file unconditionally. ──
DRILL_DB_CREATED=0
DRILL_DB_NAME=""
LOCK_FD=201

_final_cleanup() {
    local exit_code=$?
    if [ "$DRILL_DB_CREATED" = "1" ] && [ -n "$DRILL_DB_NAME" ]; then
        if _validate_drill_name "$DRILL_DB_NAME"; then
            log "Cleanup: dropping drill database $DRILL_DB_NAME"
            PGPASSWORD="$RESTORE_DRILL_DB_PASSWORD" dropdb \
                -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" \
                "$DRILL_DB_NAME" 2>&1 | while IFS= read -r line; do log "  $line"; done || \
                log "WARNING: dropdb failed for $DRILL_DB_NAME — manual cleanup may be required"
        else
            log "WARNING: drill database name failed re-validation at cleanup — refusing to drop it automatically (manual review required): $DRILL_DB_NAME"
        fi
    fi
    _cleanup_tmp_files
    exec 201>&- 2>/dev/null || true
    return "$exit_code"
}
trap _final_cleanup EXIT

# ── run a command with an internal wall-clock timeout, without
# depending on the external `timeout` binary (not guaranteed present —
# same portability rationale already established in backup_offsite.sh,
# O.5c-1) ──
_run_with_timeout() {
    local timeout_seconds="$1"; shift
    "$@" &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "$timeout_seconds" ]; then
            log "Timeout (${timeout_seconds}s) exceeded — terminating pid $pid"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 2
            kill -KILL "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            return 124
        fi
        sleep 1
        waited=$((waited + 1))
    done
    wait "$pid"
}

log "=== PostgreSQL restore drill starting ==="

# ── Preconditions ────────────────────────────────────────────────────
[ -n "$SOURCE" ] || die "Usage: restore_drill.sh --source local|offsite"
if [ "$SOURCE" != "local" ] && [ "$SOURCE" != "offsite" ]; then
    die "Invalid --source '$SOURCE' — must be 'local' or 'offsite'"
fi

mkdir -p "$BACKUP_METADATA_PATH" || die "Cannot create backup metadata dir: $BACKUP_METADATA_PATH"

command -v flock >/dev/null 2>&1 || die "flock binary not found on PATH"

LOCK_FILE="${BACKUP_METADATA_PATH}/.restore_drill.lock"
exec 201>"$LOCK_FILE"
flock -n 201 || _exit_lock_contention

command -v createdb >/dev/null 2>&1 || die "createdb binary not found on PATH"
command -v dropdb >/dev/null 2>&1 || die "dropdb binary not found on PATH"
command -v pg_restore >/dev/null 2>&1 || die "pg_restore binary not found on PATH"
command -v psql >/dev/null 2>&1 || die "psql binary not found on PATH"

[ -n "$RESTORE_DRILL_DB_PASSWORD" ] || die "RESTORE_DRILL_DB_PASSWORD is not set — refusing to guess a credential"

if [ "$SOURCE" = "offsite" ]; then
    command -v rclone >/dev/null 2>&1 || die "rclone binary not found on PATH"
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum binary not found on PATH"
    [ -f "$RCLONE_CONFIG" ] || die "RCLONE_CONFIG file not found: $RCLONE_CONFIG"
fi

# Django schema compatibility check precondition — applies to BOTH sources.
command -v "$RESTORE_DRILL_PYTHON" >/dev/null 2>&1 || die "RESTORE_DRILL_PYTHON not found or not executable: $RESTORE_DRILL_PYTHON"
[ -f "$RESTORE_DRILL_MANAGE_PY" ] || die "RESTORE_DRILL_MANAGE_PY not found: $RESTORE_DRILL_MANAGE_PY"

if [ "$SOURCE" = "local" ]; then
    # ── local: read O.5a's backup_success.json, restore straight from
    # BACKUP_DIR. The offsite branch below NEVER executes in this case
    # and never reads this manifest. ─────────────────────────────────
    [ -f "$SUCCESS_MANIFEST" ] || die "No backup_success.json at $SUCCESS_MANIFEST — nothing verified-local to drill against yet"

    INTEGRITY_VERIFIED=$(_json_field "$SUCCESS_MANIFEST" "integrity_verified")
    [ "$INTEGRITY_VERIFIED" = "true" ] || die "backup_success.json does not report integrity_verified=true — refusing to drill an unverified dump"

    DUMP_FILENAME=$(_json_field "$SUCCESS_MANIFEST" "filename")
    [ -n "$DUMP_FILENAME" ] || die "backup_success.json has no usable 'filename' field"

    RECORDED_SIZE_BYTES=$(_json_field "$SUCCESS_MANIFEST" "size_bytes")

    DUMP_FILE="${BACKUP_DIR%/}/${DUMP_FILENAME}"
    [ -f "$DUMP_FILE" ] || die "Dump file referenced by backup_success.json not found on disk: $DUMP_FILE"

    CURRENT_SIZE_BYTES=$(wc -c < "$DUMP_FILE" | tr -d ' ')
    if [ -n "$RECORDED_SIZE_BYTES" ] && [ "$CURRENT_SIZE_BYTES" != "$RECORDED_SIZE_BYTES" ]; then
        die "Local dump size ($CURRENT_SIZE_BYTES bytes) no longer matches backup_success.json's recorded size_bytes ($RECORDED_SIZE_BYTES) — refusing to drill"
    fi
    CHECKS_PASSED+=("size_match")

    log "Drilling against local dump: $DUMP_FILE ($CURRENT_SIZE_BYTES bytes, source=$SOURCE)"
    RESTORE_SOURCE_FILE="$DUMP_FILE"

else
    # ── offsite: read O.5c-1's offsite_success.json EXCLUSIVELY — never
    # touches BACKUP_DIR or backup_success.json at any point. Downloads
    # a FRESH, independent copy; never substitutes a local file even if
    # one with the same name still exists on disk. ────────────────────
    [ -f "$OFFSITE_MANIFEST" ] || die "No offsite_success.json at $OFFSITE_MANIFEST — nothing verified-offsite to drill against yet"

    for _field in schema_version timestamp_utc database_name filename size_bytes sha256 restorability_verified remote_target hostname; do
        _val=$(_json_field "$OFFSITE_MANIFEST" "$_field")
        [ -n "$_val" ] || die "offsite_success.json is missing or has an empty required field: $_field"
    done
    CHECKS_PASSED+=("offsite_metadata_valid")

    OFFSITE_RESTORABILITY_VERIFIED=$(_json_field "$OFFSITE_MANIFEST" "restorability_verified")
    [ "$OFFSITE_RESTORABILITY_VERIFIED" = "true" ] || die "offsite_success.json does not report restorability_verified=true — refusing to drill an unverified offsite copy"

    DUMP_FILENAME=$(_json_field "$OFFSITE_MANIFEST" "filename")
    EXPECTED_SIZE_BYTES=$(_json_field "$OFFSITE_MANIFEST" "size_bytes")
    EXPECTED_SHA256=$(_json_field "$OFFSITE_MANIFEST" "sha256")
    [[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "offsite_success.json's 'sha256' field is not a valid 64-char hex string — refusing to drill"
    REMOTE_TARGET=$(_json_field "$OFFSITE_MANIFEST" "remote_target")

    log "Drilling against offsite object: $REMOTE_TARGET (expected $EXPECTED_SIZE_BYTES bytes, source=$SOURCE)"

    # Independent scratch file — NEVER the local dump path, NEVER
    # BACKUP_DIR. Restrictive permissions set immediately (mktemp's
    # default is already 600 on the platforms this targets, chmod'd
    # explicitly regardless so this never silently depends on umask).
    SCRATCH_FILE=$(mktemp "${TMPDIR:-/tmp}/restore_drill_offsite.XXXXXX") || die "Failed to create independent scratch file for offsite download"
    chmod 600 "$SCRATCH_FILE" 2>/dev/null || true
    _TMP_FILES+=("$SCRATCH_FILE")

    log "Downloading offsite object to independent scratch file for verification..."
    _run_with_timeout "$RESTORE_DRILL_TIMEOUT_SECONDS" \
        rclone --config "$RCLONE_CONFIG" copyto "$REMOTE_TARGET" "$SCRATCH_FILE" \
        || die "rclone download failed or timed out: $REMOTE_TARGET"
    CHECKS_PASSED+=("offsite_download")

    DOWNLOADED_SIZE_BYTES=$(wc -c < "$SCRATCH_FILE" | tr -d ' ')
    if [ "$DOWNLOADED_SIZE_BYTES" != "$EXPECTED_SIZE_BYTES" ]; then
        die "Downloaded offsite object size ($DOWNLOADED_SIZE_BYTES bytes) does not match offsite_success.json's recorded size_bytes ($EXPECTED_SIZE_BYTES) — refusing to drill"
    fi
    CHECKS_PASSED+=("offsite_size_match")

    log "Computing SHA-256 of the recovered bytes..."
    DOWNLOADED_SHA256=$(sha256sum "$SCRATCH_FILE" | awk '{print $1}')
    if [ "$DOWNLOADED_SHA256" != "$EXPECTED_SHA256" ]; then
        die "SHA-256 mismatch: recovered=$DOWNLOADED_SHA256 expected=$EXPECTED_SHA256 (from offsite_success.json) — refusing to drill on unverified bytes"
    fi
    CHECKS_PASSED+=("offsite_sha256_match")
    log "Offsite checksum verification PASSED (recovered bytes match offsite_success.json exactly)"

    RESTORE_SOURCE_FILE="$SCRATCH_FILE"
fi

# ── Integrity pre-check (both sources converge here) ─────────────────
log "Verifying dump integrity (pg_restore --list) before touching any database..."
pg_restore --list "$RESTORE_SOURCE_FILE" > /dev/null || die "pg_restore --list failed — recovered dump appears corrupt, refusing to drill"
CHECKS_PASSED+=("dump_integrity")
log "Dump integrity: PASSED"

# ── Generate and validate the drill database name ───────────────────
TIMESTAMP_COMPACT=$(date -u '+%Y%m%d%H%M%S')
RANDOM_SUFFIX=$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')
DRILL_DB_NAME="trx_restore_drill_${TIMESTAMP_COMPACT}_${RANDOM_SUFFIX}"

_validate_drill_name "$DRILL_DB_NAME" || die "Generated drill database name failed its own safety validation — refusing to proceed: $DRILL_DB_NAME"
log "Drill database name: $DRILL_DB_NAME"

export PGPASSWORD="$RESTORE_DRILL_DB_PASSWORD"

command -v createdb >/dev/null 2>&1 || die "createdb binary not found on PATH"

# ── Create the drill database ────────────────────────────────────────
# Cleanup trap is already installed above, before this point — a
# failure anywhere from here on is guaranteed to attempt cleanup.
createdb -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" "$DRILL_DB_NAME" \
    || die "createdb failed for $DRILL_DB_NAME"
DRILL_DB_CREATED=1
CHECKS_PASSED+=("createdb")
log "Created drill database: $DRILL_DB_NAME"

# ── Restore into the drill database ONLY ─────────────────────────────
# No --clean (the database is brand new and empty — nothing to clean).
# --no-owner --no-privileges: the dump's ALTER TABLE ... OWNER TO
# trx_sim statements would otherwise fail/warn, since we are restoring
# as trx_sim_drill, not trx_sim — ownership of the drill copy's
# objects is irrelevant to what this drill proves.
log "Restoring dump into $DRILL_DB_NAME (timeout: ${RESTORE_DRILL_TIMEOUT_SECONDS}s)..."
_run_with_timeout "$RESTORE_DRILL_TIMEOUT_SECONDS" \
    pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" \
        --format=custom --no-owner --no-privileges --no-password \
        -d "$DRILL_DB_NAME" "$RESTORE_SOURCE_FILE" \
    || die "pg_restore into $DRILL_DB_NAME failed or timed out"
CHECKS_PASSED+=("pg_restore_exec")
log "Restore into $DRILL_DB_NAME complete"

# ── Level 1 verifications — pure SQL, read-only, no Django/venv. Run
# explicitly against $DRILL_DB_NAME (the just-restored temporary
# database) via -d "$DRILL_DB_NAME" on every call below — never
# against $DB_NAME. ──────────────────────────────────────────────────
log "Running Level 1 verifications..."

CONNECTIVITY=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" -d "$DRILL_DB_NAME" -tAc "SELECT 1;" 2>&1) \
    || die "Level 1 check failed: connectivity to restored database"
[ "$CONNECTIVITY" = "1" ] || die "Level 1 check failed: unexpected connectivity probe result: $CONNECTIVITY"
CHECKS_PASSED+=("connectivity")
log "  Level 1: connectivity — PASSED"

MIGRATIONS_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" -d "$DRILL_DB_NAME" -tAc \
    "SELECT COUNT(*) FROM django_migrations;" 2>&1) \
    || die "Level 1 check failed: could not query django_migrations"
if ! [[ "$MIGRATIONS_COUNT" =~ ^[0-9]+$ ]] || [ "$MIGRATIONS_COUNT" -le 0 ]; then
    die "Level 1 check failed: django_migrations table is empty or unreadable (count=$MIGRATIONS_COUNT) — this does not look like a real, migrated trx_sim database"
fi
CHECKS_PASSED+=("migrations_table_populated")
log "  Level 1: django_migrations populated ($MIGRATIONS_COUNT rows) — PASSED"

# Standard Django framework tables — deliberately NOT app-specific
# (simulator.*) table names, so this check stays stable even as the
# app's own schema evolves. Existence only — never row content.
CRITICAL_TABLES=(django_migrations django_content_type auth_user django_session)
for table in "${CRITICAL_TABLES[@]}"; do
    EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$RESTORE_DRILL_DB_USER" -d "$DRILL_DB_NAME" -tAc \
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='${table}');" 2>&1) \
        || die "Level 1 check failed: could not check existence of table $table"
    [ "$EXISTS" = "t" ] || die "Level 1 check failed: critical table '$table' does not exist in the restored database"
done
CHECKS_PASSED+=("critical_tables_exist")
log "  Level 1: critical tables present (${CRITICAL_TABLES[*]}) — PASSED"

# ── Level 2 — Django schema compatibility check (O.5d-3) ────────────
# DB_NAME/DB_USER/DB_PASSWORD are overridden ONLY for this one
# invocation, to the drill database and role — every other setting
# (DJANGO_SECRET_KEY, REDIS_URL, TOTP_ENCRYPTION_KEY, APP_ENV, etc.)
# is inherited unchanged from this script's own environment, so the
# check runs against the SAME settings/migration graph the real app
# uses, just pointed at $DRILL_DB_NAME instead of the real database.
# `migrate --check --plan` is READ-ONLY by Django's own design — it
# reports whether any migration is unapplied and exits non-zero if so,
# WITHOUT ever applying one. Plain `manage.py migrate` (which would
# apply migrations) is never invoked anywhere in this script.
log "Running Django schema compatibility check (migrate --check --plan) against $DRILL_DB_NAME..."
# set +e around this pipeline only: under `set -e -o pipefail`, a
# non-zero exit from the python side would otherwise terminate the
# script immediately (pipefail) before PIPESTATUS could ever be read
# below — die() is called explicitly instead, right after, once the
# real status is known.
set +e
DB_NAME="$DRILL_DB_NAME" DB_USER="$RESTORE_DRILL_DB_USER" DB_PASSWORD="$RESTORE_DRILL_DB_PASSWORD" \
DB_HOST="$DB_HOST" DB_PORT="$DB_PORT" \
    "$RESTORE_DRILL_PYTHON" "$RESTORE_DRILL_MANAGE_PY" migrate --check --plan --no-color \
        2>&1 | while IFS= read -r line; do log "  $line"; done
DJANGO_MIGRATE_CHECK_STATUS=${PIPESTATUS[0]}
set -e
[ "$DJANGO_MIGRATE_CHECK_STATUS" = "0" ] \
    || die "Django schema compatibility check failed (migrate --check --plan reported pending migrations, or the command itself failed) against $DRILL_DB_NAME"
CHECKS_PASSED+=("django_migrate_check")
log "  Level 2: Django schema compatibility (migrate --check --plan) — PASSED"

# ── Write durable success signal ─────────────────────────────────────
# "source": "offsite" is the ONLY evidence that counts as Production
# Restore READY — it means the bytes above were recovered from remote
# storage (offsite_download/offsite_size_match/offsite_sha256_match in
# checks_passed) AND those exact bytes were restored into a real
# database AND verified, both structurally (pg_restore_exec/
# connectivity/migrations_table_populated/critical_tables_exist) AND
# at the Django schema level (django_migrate_check, O.5d-3).
# "source": "local"
# never implies any of the offsite checks ran.
DURATION_SECONDS=$(( $(date +%s) - START_TIME ))
TIMESTAMP_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

SUCCESS_JSON=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$TIMESTAMP_UTC",
  "source": "$(_json_escape "$SOURCE")",
  "dump_filename": "$(_json_escape "$DUMP_FILENAME")",
  "duration_seconds": $DURATION_SECONDS,
  "checks_passed": $(_render_checks_json),
  "drill_database_name": "$(_json_escape "$DRILL_DB_NAME")"
}
JSON
)

_write_metadata_atomic "${BACKUP_METADATA_PATH}/restore_drill_success.json" "$SUCCESS_JSON" \
    || die "All verification steps passed but writing restore_drill_success.json failed — treating this run as failed"

log "Restore drill durability signal written: ${BACKUP_METADATA_PATH}/restore_drill_success.json"
log "=== Restore drill complete: $DUMP_FILENAME -> $DRILL_DB_NAME (${DURATION_SECONDS}s) ==="
