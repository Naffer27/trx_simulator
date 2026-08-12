#!/usr/bin/env bash
# ============================================================
# backup_media_offsite.sh — verified offsite archive of MEDIA_ROOT (O.5e-2)
#
# Closes RC-01 for durability of persistent user files (KYC documents/
# selfies, Treasury evidence, Broker documents) — none of these currently
# have any offsite backup coverage. This script builds a tar archive of
# MEDIA_ROOT, uploads it, and ONLY declares the copy durable after
# proving — not assuming — that the remote bytes are byte-identical AND
# a structurally valid archive:
#
#   1. Pre-flight: MEDIA_ROOT exists; no symlink inside it resolves
#      outside it (refuses to back up rather than silently skip or
#      silently dereference).
#   2. Build a deterministic tar archive (sorted relative paths, no
#      verbose, no absolute paths, symlinks stored as symlinks — never
#      dereferenced, matching tar's own default create behavior).
#   3. SHA-256 of the local archive.
#   4. Upload (rclone copyto).
#   5. Confirm the remote object actually exists (rclone lsf).
#   6. Download it back to an INDEPENDENT scratch file (never reuses the
#      just-built archive's path) — proves the bytes are recoverable
#      from the remote, not just that a copy command returned exit 0.
#   7. SHA-256 of the recovered bytes — must match #3 EXACTLY.
#   8. `tar -tf` on the RECOVERED copy — proves the remote bytes are a
#      structurally valid, listable archive, not just a byte-identical
#      blob (a bit-perfect copy of a truncated/corrupt archive would
#      pass step 7 and still be useless for restore).
#
# Only after all eight steps pass is media_backup_success.json written.
# Any failure at any step writes media_backup_failure.json instead and
# NEVER touches media_backup_success.json, MEDIA_ROOT, PostgreSQL
# backups, or backup_success.json/backup_failure.json/offsite_success.json
# (O.5a/O.5c manifests) — this script only ever reads MEDIA_ROOT and
# writes its own two metadata files + its own lock file + ephemeral
# scratch files it creates and deletes itself.
#
# Deliberately a SIBLING of backup_offsite.sh (O.5c-1), not a shared
# library or a modification of it (O.5e-2 Fase 0 approved decision):
#   - Own flock domain (.media_backup.lock) — never contends with
#     backup_postgres.sh's .backup.lock or backup_offsite.sh's
#     .offsite.lock.
#   - Own copies of the metadata/JSON helpers below — a bug in this
#     script's writer can never affect backup_postgres.sh's or
#     backup_offsite.sh's manifests, and vice versa.
#   - Reuses ONLY the already-approved RCLONE_CONFIG/RCLONE_REMOTE
#     contract (O.5c-1) — same dedicated, gitignored, chmod 600 rclone
#     config, same "no default remote, refuse to guess a provider"
#     rule. Uses its own MEDIA_RCLONE_REMOTE_PATH (default
#     trx-sim-media-backups) so media archives never collide with
#     Postgres dumps under the same remote/bucket.
#
# No local retention/rotation for media archives (deliberate scoping
# decision, O.5e-2): unlike backup_postgres.sh's BACKUP_DIR with
# KEEP_BACKUPS, the tar archive built here is a SCRATCH artifact only —
# it exists in MEDIA_ARCHIVE_SCRATCH_DIR for the duration of this run
# and is deleted on every exit path (success or failure). The durability
# guarantee for media comes entirely from the verified remote copy, never
# from a locally-retained archive. Restore tooling (O.5e-4, not built
# yet) will download the remote object it needs, per the same
# independent-download discipline this script itself uses in step 6.
#
# Archive format: plain, uncompressed tar (no gzip) — keeps `tar -tf`
# validation and the byte-for-byte SHA-256 comparison simple and fast,
# and avoids gzip's own non-determinism footguns (default gzip embeds a
# timestamp unless -n is passed). Compression is a possible future
# optimization, out of scope for O.5e-2.
#
# Determinism: entries are added in a fixed sorted order (`find
# -print0 | sort -z`, fed to `tar --null -T -`) rather than relying on
# directory-read order — the same MEDIA_ROOT tree always produces the
# same archive, independent of filesystem iteration order.
#
# Tar flag portability (verified before freezing, per O.5e-2 approval
# requirement): `-c/-t/-f/-C/-T/--null/--no-recursion` were verified
# working identically against both bsdtar/libarchive (this repo's macOS
# dev environment) and are long-documented GNU tar options (present
# since GNU tar 1.13+) — no GNU-tar-only flag (e.g. --sort=name,
# --owner, --numeric-owner) is used anywhere in this script specifically
# to keep it portable. Still recommended: one smoke run against the
# actual Linux production target before the first real cron/timer
# firing, same as any new operational script.
#
# Symlink policy: tar's own default archive-creation behavior already
# does NOT dereference symlinks (it stores the link target string, never
# reads through it) — so the naive risk ("a symlink to /etc/passwd gets
# its contents embedded in the archive") does not exist by default even
# without this script's own check. This script still performs an
# explicit pre-flight scan and REFUSES the entire backup (die, not
# silently skip) if any symlink inside MEDIA_ROOT resolves outside it —
# defense in depth, and because a symlink escaping MEDIA_ROOT is itself
# an anomaly worth surfacing loudly rather than silently archiving
# as-is. A symlink whose target cannot be resolved at all (dangling) is
# treated the same as an escape — fail closed rather than guess.
#
# Never logs an individual filename from inside MEDIA_ROOT (KYC document
# names, in particular, must never reach journald/log files) — every
# `log()` call below prints only counts, sizes, hashes, the fixed
# MEDIA_ROOT path itself, the generated archive filename (media_<ts>.tar
# — never derived from any file inside MEDIA_ROOT), and the remote
# target (bucket/prefix, not object names beyond the archive's own
# generic name). `tar -tf` output is always piped into `grep -q`/counted,
# never echoed.
#
# rclone is used ONLY for copyto (upload), copyto (download-back), and
# lsf (existence check) against RCLONE_REMOTE — this script never calls
# `rclone sync`, `rclone delete`, `rclone purge`, or any other command
# capable of mutating or removing anything already on the remote.
#
# Usage:
#   bash deploy/scripts/backup_media_offsite.sh
#
# Environment:
#   MEDIA_ROOT                — directory to back up (default:
#                                /opt/trx_sim/media, matches Django's
#                                MEDIA_ROOT = BASE_DIR / "media"). Must
#                                already exist — this script never
#                                creates it.
#   BACKUP_METADATA_PATH       — O.5a/O.5c signal + lock directory
#                                (default: /var/log/trx_sim/backup/) —
#                                same directory those already use; this
#                                script only ever writes
#                                media_backup_success.json,
#                                media_backup_failure.json, and its own
#                                .media_backup.lock there. Never reads or
#                                writes backup_success.json,
#                                backup_failure.json, or
#                                offsite_success.json.
#   RCLONE_CONFIG              — path to the DEDICATED rclone config file
#                                (default: /etc/trx_sim/rclone.conf) —
#                                same file O.5c-1 already uses.
#   RCLONE_REMOTE               — name of the remote defined inside
#                                RCLONE_CONFIG (no default — must be set
#                                explicitly; refuses to guess a
#                                provider). Same variable O.5c-1 uses;
#                                same remote may be reused.
#   MEDIA_RCLONE_REMOTE_PATH    — path/prefix on that remote under which
#                                media archives are stored (default:
#                                trx-sim-media-backups — deliberately
#                                distinct from RCLONE_REMOTE_PATH's
#                                trx-sim-backups default, so media
#                                archives and Postgres dumps never
#                                collide under the same remote).
#   MEDIA_ARCHIVE_SCRATCH_DIR   — directory for the archive build AND the
#                                independent verification download
#                                (default: system tmp via mktemp -d;
#                                created with mode 700).
# ============================================================
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-/opt/trx_sim/media}"
MEDIA_ROOT="${MEDIA_ROOT%/}"

BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH:-/var/log/trx_sim/backup/}"
BACKUP_METADATA_PATH="${BACKUP_METADATA_PATH%/}"

RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/trx_sim/rclone.conf}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
MEDIA_RCLONE_REMOTE_PATH="${MEDIA_RCLONE_REMOTE_PATH:-trx-sim-media-backups}"
MEDIA_ARCHIVE_SCRATCH_DIR="${MEDIA_ARCHIVE_SCRATCH_DIR:-}"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── own, independent metadata/scratch helpers (deliberately NOT shared
# with backup_postgres.sh or backup_offsite.sh — see header) ──────────
_TMP_PATHS=()
_cleanup_tmp() {
    local p
    for p in "${_TMP_PATHS[@]:-}"; do
        [ -n "$p" ] && rm -rf "$p" 2>/dev/null
    done
    return 0
}
trap _cleanup_tmp EXIT

_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n'
}

_write_metadata_atomic() {
    local target="$1" content="$2" tmp
    tmp="$(mktemp "${BACKUP_METADATA_PATH}/.media_metadata.XXXXXX")" || return 1
    _TMP_PATHS+=("$tmp")
    chmod 640 "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    printf '%s\n' "$content" > "$tmp" || { rm -f "$tmp"; return 1; }
    sync
    mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
    return 0
}

_write_media_failure() {
    local reason="$1" ts hostn content
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    hostn=$(hostname 2>/dev/null || echo unknown)
    content=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$ts",
  "reason": "$(_json_escape "$reason")",
  "hostname": "$(_json_escape "$hostn")"
}
JSON
)
    # Best-effort only — must never mask the ORIGINAL error, and must
    # NEVER touch media_backup_success.json or any O.5a/O.5c manifest.
    _write_metadata_atomic "${BACKUP_METADATA_PATH}/media_backup_failure.json" "$content" 2>/dev/null || true
}

# Reason strings passed to die() are operational/structural only (e.g.
# "symlink escape detected", "SHA-256 mismatch") — never a filename from
# inside MEDIA_ROOT. Enforced by convention at every call site below.
die() { log "ERROR: $*"; _write_media_failure "$*"; exit 1; }

_exit_lock_contention() {
    log "Another backup_media_offsite.sh run is already in progress (lock: $LOCK_FILE) — exiting without touching any metadata."
    exit 0
}

log "=== Media offsite backup starting ==="

mkdir -p "$BACKUP_METADATA_PATH" || die "Cannot create backup metadata dir: $BACKUP_METADATA_PATH"

# Checked BEFORE the flock attempt, same fix O.5c-1 already applies: if
# flock itself is missing, `flock -n 201` fails with a bash "command not
# found" (exit 127), which `|| _exit_lock_contention` would otherwise
# misreport as harmless lock contention instead of a real missing
# dependency.
command -v flock >/dev/null 2>&1 || die "flock binary not found on PATH"

LOCK_FILE="${BACKUP_METADATA_PATH}/.media_backup.lock"
exec 201>"$LOCK_FILE"
flock -n 201 || _exit_lock_contention

# ── Preconditions ──────────────────────────────────────────────────
command -v rclone >/dev/null 2>&1 || die "rclone binary not found on PATH"
command -v tar >/dev/null 2>&1 || die "tar binary not found on PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum binary not found on PATH"
command -v realpath >/dev/null 2>&1 || die "realpath binary not found on PATH"

[ -n "$RCLONE_REMOTE" ] || die "RCLONE_REMOTE is not set — refusing to guess a provider/remote"
[ -f "$RCLONE_CONFIG" ] || die "RCLONE_CONFIG file not found: $RCLONE_CONFIG"

# MEDIA_ROOT must already exist — this script never creates it, and a
# missing MEDIA_ROOT is a clear, loud failure, never treated as "0 files
# to back up" (that case is reserved for an EXISTING-but-empty
# directory, see below).
[ -d "$MEDIA_ROOT" ] || die "MEDIA_ROOT directory not found: $MEDIA_ROOT"

MEDIA_ROOT_REAL=$(realpath "$MEDIA_ROOT") || die "Failed to resolve MEDIA_ROOT to a real path"

# ── Symlink escape pre-flight scan ──────────────────────────────────
# Refuses the ENTIRE backup (not a partial/silent skip) if any symlink
# inside MEDIA_ROOT resolves outside it, or cannot be resolved at all
# (dangling) — fail closed rather than guess. Uses process substitution
# (not a trailing pipe) so `die` inside the loop actually terminates
# this script, not just a subshell (a `... | while read` pipeline would
# run the loop body in a subshell that `exit` cannot escape).
while IFS= read -r -d '' _link; do
    _target_real=$(realpath "$_link" 2>/dev/null) || die "Symlink escape check failed: an entry inside MEDIA_ROOT could not be resolved (dangling or unreadable) — refusing to back up"
    case "$_target_real" in
        "$MEDIA_ROOT_REAL"/*|"$MEDIA_ROOT_REAL") ;;
        *) die "Symlink escape detected: a symlink inside MEDIA_ROOT resolves outside it — refusing to back up (path withheld from logs)" ;;
    esac
done < <(find "$MEDIA_ROOT" -type l -print0)

log "Symlink escape check passed"

# ── Build deterministic file list (relative paths, NUL-delimited) ──
FILELIST=$(mktemp "${BACKUP_METADATA_PATH}/.media_filelist.XXXXXX") || die "Failed to create scratch file list"
_TMP_PATHS+=("$FILELIST")
chmod 600 "$FILELIST" 2>/dev/null || die "Failed to set restrictive permissions on scratch file list"

(cd "$MEDIA_ROOT" && find . -mindepth 1 \( -type f -o -type d -o -type l \) -print0 | sort -z) > "$FILELIST" \
    || die "Failed to enumerate MEDIA_ROOT contents"

FILE_COUNT=$(find "$MEDIA_ROOT" -type f -print0 2>/dev/null | tr -cd '\0' | wc -c | tr -d ' ')

log "MEDIA_ROOT enumerated: file_count=$FILE_COUNT (directories/symlinks not counted in file_count)"

# ── Scratch dir for the archive build + independent verification ──
SCRATCH_MKTEMP_ARGS=(-d)
[ -n "$MEDIA_ARCHIVE_SCRATCH_DIR" ] && SCRATCH_MKTEMP_ARGS=(-d -p "$MEDIA_ARCHIVE_SCRATCH_DIR")
SCRATCH_DIR=$(mktemp "${SCRATCH_MKTEMP_ARGS[@]}") || die "Failed to create archive scratch directory"
_TMP_PATHS+=("$SCRATCH_DIR")
chmod 700 "$SCRATCH_DIR" || die "Failed to set restrictive permissions on scratch directory"

TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')
ARCHIVE_FILENAME="media_${TIMESTAMP}.tar"
ARCHIVE_PATH="${SCRATCH_DIR}/${ARCHIVE_FILENAME}"

# ── Step: build the archive ─────────────────────────────────────────
# No -v (no verbose — never prints member names). --null/-T read the
# NUL-delimited, pre-sorted relative path list built above.
# --no-recursion means tar adds exactly the listed entries (already a
# complete recursive enumeration from `find`), not a second recursive
# walk of its own — this is what makes ordering fully controlled by the
# sorted list rather than tar's own directory-read order.
log "Building archive: $ARCHIVE_FILENAME (file_count=$FILE_COUNT)"
tar --null -T "$FILELIST" --no-recursion -C "$MEDIA_ROOT" -cf "$ARCHIVE_PATH" \
    || die "tar archive creation failed"
chmod 600 "$ARCHIVE_PATH" || die "Failed to set restrictive permissions on archive"

# Defense in depth: confirm the archive is listable AND that no absolute
# path made it in. Captured into a variable first (not a bare pipe) so a
# `tar -tf` failure here is itself detected explicitly — piping straight
# into `grep -q` would let a tar failure that also produces empty output
# read as a false "no absolute path found" instead of "couldn't even
# list this archive". Never echoes the listing itself.
_LISTING=$(tar -tf "$ARCHIVE_PATH") || die "Archive freshly built by tar is not listable — tar create step produced an invalid archive"
if printf '%s\n' "$_LISTING" | grep -q '^/'; then
    die "Archive contains an absolute path entry — refusing to upload"
fi
unset _LISTING

# ── Step: SHA-256 of the local archive ──────────────────────────────
SHA256_LOCAL=$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')
[ -n "$SHA256_LOCAL" ] || die "Failed to compute local SHA-256 for archive"
SIZE_BYTES=$(wc -c < "$ARCHIVE_PATH" | tr -d ' ')
log "Local archive: size_bytes=$SIZE_BYTES sha256=$SHA256_LOCAL"

REMOTE_TARGET="${RCLONE_REMOTE}:${MEDIA_RCLONE_REMOTE_PATH}/${ARCHIVE_FILENAME}"

# ── Step: upload ─────────────────────────────────────────────────────
log "Uploading to $REMOTE_TARGET ..."
rclone --config "$RCLONE_CONFIG" copyto "$ARCHIVE_PATH" "$REMOTE_TARGET" \
    || die "rclone upload failed"

# ── Step: confirm the remote object actually exists ────────────────
log "Confirming remote object exists ..."
rclone --config "$RCLONE_CONFIG" lsf "${RCLONE_REMOTE}:${MEDIA_RCLONE_REMOTE_PATH}/" --files-only 2>/dev/null \
    | grep -qx "$ARCHIVE_FILENAME" \
    || die "Uploaded object not found on remote listing after upload"

# ── Step: download to an INDEPENDENT scratch file ───────────────────
# Never reuses $ARCHIVE_PATH — proves the bytes are recoverable FROM THE
# REMOTE, not merely that a copy command exited 0.
RECOVERED_FILE=$(mktemp -p "$SCRATCH_DIR" media_recovered.XXXXXX) || die "Failed to create independent scratch file for download verification"
_TMP_PATHS+=("$RECOVERED_FILE")

log "Downloading to independent scratch file for verification ..."
rclone --config "$RCLONE_CONFIG" copyto "$REMOTE_TARGET" "$RECOVERED_FILE" \
    || die "rclone download-for-verification failed"

# ── Step: SHA-256 of the recovered bytes — must match EXACTLY ──────
SHA256_RECOVERED=$(sha256sum "$RECOVERED_FILE" | awk '{print $1}')
log "Recovered SHA-256: $SHA256_RECOVERED"

if [ "$SHA256_RECOVERED" != "$SHA256_LOCAL" ]; then
    die "SHA-256 mismatch: recovered copy is NOT byte-identical to the local archive — refusing to mark offsite success"
fi
log "Checksum verification PASSED (local == recovered from remote)"

# ── Step: tar validation of the RECOVERED copy ──────────────────────
# Deliberately runs against $RECOVERED_FILE, not $ARCHIVE_PATH — a
# bit-perfect copy of an already-corrupt archive would pass the checksum
# step and still be useless. Output is discarded, never echoed.
log "Validating recovered archive with tar -tf ..."
tar -tf "$RECOVERED_FILE" > /dev/null \
    || die "tar validation failed on the RECOVERED remote copy — remote copy is not a valid archive despite matching checksum"
log "Archive validation PASSED"

# ── Write durable media offsite success signal ──────────────────────
TIMESTAMP_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
HOSTNAME_VAL=$(hostname 2>/dev/null || echo unknown)

SUCCESS_JSON=$(cat <<JSON
{
  "schema_version": 1,
  "timestamp_utc": "$TIMESTAMP_UTC",
  "archive_filename": "$(_json_escape "$ARCHIVE_FILENAME")",
  "size_bytes": $SIZE_BYTES,
  "sha256": "$(_json_escape "$SHA256_LOCAL")",
  "file_count": $FILE_COUNT,
  "remote_target": "$(_json_escape "$REMOTE_TARGET")",
  "hostname": "$(_json_escape "$HOSTNAME_VAL")",
  "integrity_verified": true
}
JSON
)

_write_metadata_atomic "${BACKUP_METADATA_PATH}/media_backup_success.json" "$SUCCESS_JSON" \
    || die "All verification steps passed but writing media_backup_success.json failed — treating this run as failed"

log "Media offsite durability signal written: ${BACKUP_METADATA_PATH}/media_backup_success.json"
log "=== Media offsite backup complete: file_count=$FILE_COUNT ==="
