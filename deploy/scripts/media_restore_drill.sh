#!/usr/bin/env bash
# ============================================================
# media_restore_drill.sh — verified offsite media restore drill (O.5e-4)
#
# Proves the offsite media archive (O.5e-2) is ACTUALLY recoverable and
# extractable — not just uploaded — by downloading it fresh from the
# remote, verifying it independently, and extracting it into a
# throwaway, uniquely-named scratch directory that is NEVER MEDIA_ROOT
# and NEVER inside MEDIA_ROOT. Never restores onto, writes into, or
# reads FROM MEDIA_ROOT at any point — MEDIA_ROOT is consulted ONLY to
# resolve its own real path, purely so the scratch directory can be
# proven NOT to be it or inside it, before that scratch directory is
# ever created.
#
# ── Chain (matches O.5e-4 approval exactly) ─────────────────────────
#   OFFSITE MEDIA → DOWNLOAD → SIZE VERIFY → SHA-256 VERIFY →
#   TAR VALIDATION → EXTRACT TO TEMP DIRECTORY →
#   VERIFY FILE_COUNT / STRUCTURE → CLEANUP
#
# Only after every step passes is media_restore_drill_success.json
# written. Any failure at any step writes media_restore_drill_failure.
# json instead and NEVER touches media_restore_drill_success.json,
# MEDIA_ROOT, or any O.5a/O.5c/O.5e-2 manifest (backup_success.json,
# offsite_success.json, media_backup_success.json) — this script only
# ever READS media_backup_success.json and writes its own two metadata
# files + its own lock file + its own throwaway scratch directory.
#
# ── Source of truth — EXCLUSIVELY media_backup_success.json (O.5e-2) ─
# There is no flag or environment variable to supply a remote filename,
# remote_target, expected checksum, or destination path — all of that
# is derived from media_backup_success.json's own "remote_target"/
# "size_bytes"/"sha256"/"file_count" fields, exactly as
# backup_media_offsite.sh wrote them (same reuse discipline O.5d-2
# already established for the Postgres restore drill's --source
# offsite path against offsite_success.json). This script has no
# "local" mode at all — media_backup_offsite.sh keeps no local archive
# to drill against (O.5e-2 deliberate no-retention decision), so
# offsite is the only source that could ever exist, and is therefore
# the only one implemented — never a substitute path for Production
# Readiness, it IS the only path.
#
# ── Deliberately a SIBLING, not a modification of anything ──────────
#   - Independent of restore_drill.sh (PostgreSQL, O.5d) — no shared
#     code, no shared lock domain (.media_restore_drill.lock, never
#     .restore_drill.lock), never touches a database, never imports
#     createdb/dropdb/pg_restore/psql.
#   - Independent of backup_media_offsite.sh (O.5e-2) — never invoked
#     by it, never invokes it, own lock domain, own metadata files.
#     Both scripts happen to read/write the same BACKUP_METADATA_PATH
#     directory (matching O.5a/O.5c/O.5e-2's own precedent), but never
#     each other's files.
#
# ── The scratch directory name ───────────────────────────────────────
# Generated INTERNALLY as trx_media_restore_drill_<UTC timestamp>_
# <random hex> — there is NO --destination/--scratch-dir/--target flag
# or equivalent environment variable that lets a caller choose WHAT
# this directory is; MEDIA_RESTORE_DRILL_SCRATCH_DIR (see Environment
# below) only controls WHERE its PARENT lives, never its name or
# identity. Every name is validated by _validate_drill_scratch_name()
# against ALL of the following before ANY download/extract/rm -rf is
# allowed to run against it:
#   - matches ^trx_media_restore_drill_[0-9]{14}_[0-9a-f]{8}$ exactly
#   - its REAL (symlink-resolved) path is not equal to, and not
#     contained inside, MEDIA_ROOT's own real path
# The SAME validation is re-run, unconditionally, inside the cleanup
# trap before it ever calls `rm -rf` on it — a corrupted variable at
# cleanup time can never turn into deleting something unvalidated.
#
# ── Extraction safety (empirically verified, not assumed) ───────────
# Real tar (bsdtar/libarchive, this repo's macOS dev environment;
# modern GNU tar behaves the same way for this by long-standing
# default) already REFUSES to extract any entry whose name contains a
# ".." component (aborts with a nonzero exit — confirmed by
# deliberately crafting such an archive and observing the failure
# during O.5e-4 development) and strips a leading "/" from absolute
# entries rather than writing outside the extraction root. What real
# tar does NOT block: a SYMLINK entry can still be created inside the
# extraction root pointing OUTSIDE it (confirmed the same way — the
# symlink object itself lands safely inside, but its target string can
# be anything, e.g. /etc/passwd) — nothing reads through it during
# extraction, but a later process that did would be exposed to
# whatever that symlink points at. Because of this, extraction safety
# here is layered, not assumed from any single mechanism:
#   1. PRE-extract: `tar -tf`'s plain listing is checked for any
#      absolute path entry or any entry containing ".." — refuses
#      (dies) before ever calling `tar -x` if found, rather than
#      relying solely on tar's own behavior.
#   2. Extraction itself, using real tar's own built-in protections.
#   3. POST-extract: every symlink actually found on disk inside the
#      extraction root is realpath-resolved and its target's
#      containment inside the extraction root is verified explicitly —
#      this is the layer that catches the one gap real tar leaves open.
# Any failure at any of these three layers dies (refuses, writes
# failure evidence) rather than silently skipping the offending entry.
#
# Never logs an individual filename from inside the archive (KYC
# document names, in particular, must never reach journald/log files)
# — every `log()` call below prints only counts, sizes, hashes,
# durations, and fixed/generic labels. `tar -tf` output is always
# captured into a variable and grepped, never echoed. The extracted
# tree is never read for content, only walked structurally (find/wc/
# realpath) to count and verify containment.
#
# Usage:
#   bash deploy/scripts/media_restore_drill.sh
#
# Environment:
#   MEDIA_ROOT                      — read ONLY to resolve its real
#                                      path for the scratch-containment
#                                      safety check above; NEVER read
#                                      from or written to otherwise
#                                      (default: /opt/trx_sim/media).
#   BACKUP_METADATA_PATH            — O.5a/O.5c/O.5e-2/O.5e-4 signal +
#                                      lock directory (default:
#                                      /var/log/trx_sim/backup/) — same
#                                      directory those already use;
#                                      this script only ever reads
#                                      media_backup_success.json and
#                                      writes media_restore_drill_
#                                      success.json / _failure.json /
#                                      its own .media_restore_drill.lock
#                                      there.
#   RCLONE_CONFIG                    — same DEDICATED rclone config as
#                                      O.5c-1/O.5e-2 (default:
#                                      /etc/trx_sim/rclone.conf) — this
#                                      script only ever reads the
#                                      "remote_target" value ALREADY
#                                      computed and stored by
#                                      backup_media_offsite.sh; it never
#                                      needs RCLONE_REMOTE/
#                                      MEDIA_RCLONE_REMOTE_PATH
#                                      separately.
#   MEDIA_RESTORE_DRILL_SCRATCH_DIR  — PARENT directory under which the
#                                      internally-named scratch
#                                      directory is created (default:
#                                      system tmp). Never the scratch
#                                      directory's own name/identity —
#                                      see naming section above.
#   MEDIA_RESTORE_DRILL_TIMEOUT_SECONDS — max wall-clock time for the
#                                      download step (default: 3600,
#                                      same default as restore_drill.sh).
# ============================================================
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-/opt/trx_sim/media}"
MEDIA_ROOT="${MEDIA_ROOT%/}"

BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH:-/var/log/trx_sim/backup/}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH%/}"

RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/trx_sim/rclone.conf}"
MEDIA_RESTORE_DRILL_SCRATCH_DIR="${MEDIA_RESTORE_DRILL_SCRATCH_DIR:-${TMPDIR:-/tmp}}"
MEDIA_RESTORE_DRILL_TIMEOUT_SECONDS="${MEDIA_RESTORE_DRILL_TIMEOUT_SECONDS:-3600}"

MEDIA_MANIFEST="${BACKUP_METADATA_PATH}/media_backup_success.json"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

START_TIME=$(date +%s)

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

# ── own, independent metadata helpers (deliberately NOT shared with
# backup_media_offsite.sh or restore_drill.sh — see header) ───────────
_TMP_PATHS=()
_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n'
}

# Extracts a top-level value for "key" from OUR OWN fixed one-key-per-
# line heredoc JSON (backup_media_offsite.sh's format) — NOT a
# general-purpose parser, never point this at untrusted JSON.
_json_field() {
    local file="$1" key="$2"
    grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"\|\"${key}\"[[:space:]]*:[[:space:]]*[^,}[:space:]]*" "$file" 2>/dev/null \
        | head -1 \
        | sed -E "s/\"${key}\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"\$//"
}

_write_metadata_atomic() {
    local target="$1" content="$2" tmp
    tmp="$(mktemp "${BACKUP_METADATA_PATH}/.media_restore_drill_metadata.XXXXXX")" || return 1
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
  "source": "offsite",
  "reason": "$(_json_escape "$reason")"
}
JSON
)
    # Best-effort only — must never mask the ORIGINAL error, and must
    # NEVER touch media_restore_drill_success.json or any O.5a/O.5c/
    # O.5e-2 manifest.
    _write_metadata_atomic "${BACKUP_METADATA_PATH}/media_restore_drill_failure.json" "$content" 2>/dev/null || true
}

# Reason strings passed to die() are operational/structural only — never
# a filename from inside the archive. Enforced by convention at every
# call site below.
die() { log "ERROR: $*"; _write_drill_failure "$*"; exit 1; }

_exit_lock_contention() {
    log "Another media_restore_drill.sh run is already in progress (lock: $LOCK_FILE) — exiting without touching any metadata."
    exit 0
}

# ── scratch directory name validation — reused for BOTH the pre-create
# check and, unconditionally, inside the cleanup trap before rm -rf ───
_validate_drill_scratch_name() {
    local path="$1" base
    base=$(basename "$path")
    if ! [[ "$base" =~ ^trx_media_restore_drill_[0-9]{14}_[0-9a-f]{8}$ ]]; then
        log "REJECTED: '$base' does not match the required trx_media_restore_drill_ pattern"
        return 1
    fi
    local real_path
    real_path=$(realpath "$path" 2>/dev/null) || { log "REJECTED: could not resolve real path of scratch directory"; return 1; }
    case "$real_path" in
        "$MEDIA_ROOT_REAL"/*|"$MEDIA_ROOT_REAL")
            log "REJECTED: scratch directory resolves inside MEDIA_ROOT"
            return 1
            ;;
    esac
    return 0
}

# ── cleanup: installed BEFORE download/extract is ever attempted. Only
# removes the scratch dir if it both (a) was actually created by THIS
# run and (b) re-passes _validate_drill_scratch_name at cleanup time —
# never trusts a stale/corrupted variable. Releases the lock
# unconditionally. Cleanup failure is logged as a WARNING, never masks
# the real exit code or overwrites whatever success/failure JSON was
# already written. ──
SCRATCH_CREATED=0
SCRATCH_DIR=""
LOCK_FD=201

_final_cleanup() {
    local exit_code=$?
    if [ "$SCRATCH_CREATED" = "1" ] && [ -n "$SCRATCH_DIR" ]; then
        if _validate_drill_scratch_name "$SCRATCH_DIR"; then
            log "Cleanup: removing scratch directory"
            rm -rf "$SCRATCH_DIR" 2>/dev/null \
                || log "WARNING: failed to remove scratch directory — manual cleanup may be required"
        else
            log "WARNING: scratch directory failed re-validation at cleanup — refusing to remove it automatically (manual review required)"
        fi
    fi
    exec 201>&- 2>/dev/null || true
    return "$exit_code"
}
trap _final_cleanup EXIT

# ── run a command with an internal wall-clock timeout, without
# depending on the external `timeout` binary — own copy, same
# portability rationale already established in backup_offsite.sh/
# restore_drill.sh ──
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

log "=== Media offsite restore drill starting ==="

mkdir -p "$BACKUP_METADATA_PATH" || die "Cannot create backup metadata dir: $BACKUP_METADATA_PATH"

command -v flock >/dev/null 2>&1 || die "flock binary not found on PATH"

LOCK_FILE="${BACKUP_METADATA_PATH}/.media_restore_drill.lock"
exec 201>"$LOCK_FILE"
flock -n 201 || _exit_lock_contention

# ── Preconditions ──────────────────────────────────────────────────
command -v rclone >/dev/null 2>&1 || die "rclone binary not found on PATH"
command -v tar >/dev/null 2>&1 || die "tar binary not found on PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum binary not found on PATH"
command -v realpath >/dev/null 2>&1 || die "realpath binary not found on PATH"

[ -f "$RCLONE_CONFIG" ] || die "RCLONE_CONFIG file not found: $RCLONE_CONFIG"

# MEDIA_ROOT is read ONLY to resolve its real path for the
# scratch-containment safety check — it is fine (and common in a fresh
# checkout / CI) for it not to exist yet; that just means "nothing to be
# contained inside", not a precondition failure for a restore drill that
# never touches it.
if [ -d "$MEDIA_ROOT" ]; then
    MEDIA_ROOT_REAL=$(realpath "$MEDIA_ROOT") || die "Failed to resolve MEDIA_ROOT to a real path"
else
    MEDIA_ROOT_REAL="$MEDIA_ROOT"
fi

# ── Read and validate the offsite media manifest (O.5e-2) — EXCLUSIVE
# source of truth for what to download and verify. ─────────────────
[ -f "$MEDIA_MANIFEST" ] || die "No media_backup_success.json at $MEDIA_MANIFEST — nothing verified-offsite to drill against yet"

for _field in schema_version timestamp_utc archive_filename size_bytes sha256 file_count remote_target hostname integrity_verified; do
    _val=$(_json_field "$MEDIA_MANIFEST" "$_field")
    [ -n "$_val" ] || die "media_backup_success.json is missing or has an empty required field: $_field"
done

MANIFEST_INTEGRITY_VERIFIED=$(_json_field "$MEDIA_MANIFEST" "integrity_verified")
[ "$MANIFEST_INTEGRITY_VERIFIED" = "true" ] || die "media_backup_success.json does not report integrity_verified=true — refusing to drill an unverified archive"

EXPECTED_SIZE_BYTES=$(_json_field "$MEDIA_MANIFEST" "size_bytes")
EXPECTED_SHA256=$(_json_field "$MEDIA_MANIFEST" "sha256")
[[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "media_backup_success.json's 'sha256' field is not a valid 64-char hex string — refusing to drill"
EXPECTED_FILE_COUNT=$(_json_field "$MEDIA_MANIFEST" "file_count")
[[ "$EXPECTED_FILE_COUNT" =~ ^[0-9]+$ ]] || die "media_backup_success.json's 'file_count' field is not a non-negative integer — refusing to drill"
REMOTE_TARGET=$(_json_field "$MEDIA_MANIFEST" "remote_target")

CHECKS_PASSED+=("manifest_valid")
log "Drilling against offsite media archive (expected ${EXPECTED_SIZE_BYTES} bytes, expected file_count=${EXPECTED_FILE_COUNT})"

# ── Generate and validate the scratch directory name ─────────────────
TIMESTAMP_COMPACT=$(date -u '+%Y%m%d%H%M%S')
RANDOM_SUFFIX=$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')
SCRATCH_DIR="${MEDIA_RESTORE_DRILL_SCRATCH_DIR%/}/trx_media_restore_drill_${TIMESTAMP_COMPACT}_${RANDOM_SUFFIX}"

mkdir "$SCRATCH_DIR" || die "Failed to create scratch directory"
SCRATCH_CREATED=1
chmod 700 "$SCRATCH_DIR" || die "Failed to set restrictive permissions on scratch directory"

_validate_drill_scratch_name "$SCRATCH_DIR" \
    || die "Generated scratch directory failed its own safety validation — refusing to proceed"
log "Scratch directory ready"

ARCHIVE_FILE="${SCRATCH_DIR}/downloaded.tar"

# ── Step: download from the offsite remote ───────────────────────────
log "Downloading offsite media archive to scratch for verification..."
_run_with_timeout "$MEDIA_RESTORE_DRILL_TIMEOUT_SECONDS" \
    rclone --config "$RCLONE_CONFIG" copyto "$REMOTE_TARGET" "$ARCHIVE_FILE" \
    || die "rclone download failed or timed out"
chmod 600 "$ARCHIVE_FILE" 2>/dev/null || true
CHECKS_PASSED+=("download")

# ── Step: size verify ─────────────────────────────────────────────────
DOWNLOADED_SIZE_BYTES=$(wc -c < "$ARCHIVE_FILE" | tr -d ' ')
if [ "$DOWNLOADED_SIZE_BYTES" != "$EXPECTED_SIZE_BYTES" ]; then
    die "Downloaded archive size ($DOWNLOADED_SIZE_BYTES bytes) does not match media_backup_success.json's recorded size_bytes ($EXPECTED_SIZE_BYTES) — refusing to drill"
fi
CHECKS_PASSED+=("size_match")

# ── Step: SHA-256 verify ──────────────────────────────────────────────
log "Computing SHA-256 of the recovered bytes..."
DOWNLOADED_SHA256=$(sha256sum "$ARCHIVE_FILE" | awk '{print $1}')
if [ "$DOWNLOADED_SHA256" != "$EXPECTED_SHA256" ]; then
    die "SHA-256 mismatch: recovered bytes do not match media_backup_success.json's recorded sha256 — refusing to drill on unverified bytes"
fi
CHECKS_PASSED+=("sha256_match")
log "Checksum verification PASSED"

# ── Step: tar validation (structural) ─────────────────────────────────
log "Validating archive structure with tar -tf..."
LISTING=$(tar -tf "$ARCHIVE_FILE") || die "tar validation failed — recovered archive is not a valid tar file"
CHECKS_PASSED+=("tar_valid")

# ── Step: pre-extract safety checks on the LISTING (never echoed) ────
if printf '%s\n' "$LISTING" | grep -q '^/'; then
    die "Archive contains an absolute path entry — refusing to extract"
fi
CHECKS_PASSED+=("no_absolute_paths")

if printf '%s\n' "$LISTING" | grep -qE '(^|/)\.\.(/|$)'; then
    die "Archive contains a '..' traversal entry — refusing to extract"
fi
CHECKS_PASSED+=("no_traversal")
unset LISTING

# ── Step: extract into the scratch directory ONLY ────────────────────
EXTRACT_DIR="${SCRATCH_DIR}/extracted"
mkdir "$EXTRACT_DIR" || die "Failed to create extraction subdirectory"

log "Extracting archive into scratch (never MEDIA_ROOT)..."
tar -xf "$ARCHIVE_FILE" -C "$EXTRACT_DIR" \
    || die "tar extraction failed (real tar refuses unsafe entries by default — see script header)"
CHECKS_PASSED+=("extracted")

EXTRACT_DIR_REAL=$(realpath "$EXTRACT_DIR") || die "Failed to resolve extraction directory to a real path"

# ── Step: post-extract symlink containment (the one gap real tar
# leaves open — see script header) ────────────────────────────────────
while IFS= read -r -d '' _link; do
    _target_real=$(realpath "$_link" 2>/dev/null) || die "Post-extract symlink check failed: an extracted entry could not be resolved (dangling or unreadable) — refusing this drill result"
    case "$_target_real" in
        "$EXTRACT_DIR_REAL"/*|"$EXTRACT_DIR_REAL") ;;
        *) die "Symlink escape detected in extracted archive: a symlink resolves outside the scratch extraction directory — refusing this drill result (path withheld from logs)" ;;
    esac
done < <(find "$EXTRACT_DIR" -type l -print0)
CHECKS_PASSED+=("no_symlink_escape")

# ── Step: verify file_count matches the manifest ──────────────────────
EXTRACTED_FILE_COUNT=$(find "$EXTRACT_DIR" -type f -print0 2>/dev/null | tr -cd '\0' | wc -c | tr -d ' ')
if [ "$EXTRACTED_FILE_COUNT" != "$EXPECTED_FILE_COUNT" ]; then
    die "Extracted file_count ($EXTRACTED_FILE_COUNT) does not match media_backup_success.json's recorded file_count ($EXPECTED_FILE_COUNT) — refusing this drill result"
fi
CHECKS_PASSED+=("file_count_match")

# ── Step: verify every extracted entry stays inside the scratch dir ──
# (belt-and-suspenders beyond the symlink-specific check above — walks
# every file/dir too, catching anything the earlier layered checks
# might have missed.)
while IFS= read -r -d '' _entry; do
    _entry_real=$(realpath "$_entry" 2>/dev/null) || die "Post-extract containment check failed: an extracted entry could not be resolved — refusing this drill result"
    case "$_entry_real" in
        "$EXTRACT_DIR_REAL"/*|"$EXTRACT_DIR_REAL") ;;
        *) die "Extracted entry escaped the scratch extraction directory — refusing this drill result (path withheld from logs)" ;;
    esac
done < <(find "$EXTRACT_DIR" -mindepth 1 -print0)
CHECKS_PASSED+=("structure_contained")

log "Extraction verification PASSED: file_count=${EXTRACTED_FILE_COUNT}, all entries contained within scratch"

# ── Write durable success signal ──────────────────────────────────────
# "source": "offsite" is the only value this script ever produces (no
# local mode exists, see header) — present anyway for schema symmetry
# with restore_drill.sh's own success/failure manifests.
DURATION_SECONDS=$(( $(date +%s) - START_TIME ))
TIMESTAMP_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

SUCCESS_JSON=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$TIMESTAMP_UTC",
  "source": "offsite",
  "duration_seconds": $DURATION_SECONDS,
  "archive_size_bytes": $DOWNLOADED_SIZE_BYTES,
  "file_count": $EXTRACTED_FILE_COUNT,
  "checks_passed": $(_render_checks_json)
}
JSON
)

_write_metadata_atomic "${BACKUP_METADATA_PATH}/media_restore_drill_success.json" "$SUCCESS_JSON" \
    || die "All verification steps passed but writing media_restore_drill_success.json failed — treating this run as failed"

log "Media restore drill durability signal written: ${BACKUP_METADATA_PATH}/media_restore_drill_success.json"
log "=== Media offsite restore drill complete: file_count=${EXTRACTED_FILE_COUNT}, duration=${DURATION_SECONDS}s ==="
