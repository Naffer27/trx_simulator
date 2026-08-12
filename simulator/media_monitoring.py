# simulator/media_monitoring.py
"""
O.5e-3 — Media Offsite Backup Durability Signal (read-only inspector).

Answers a single question: "does a verified, sufficiently-recent offsite
archive of MEDIA_ROOT exist?" — nothing else. Mirrors
simulator/offsite_monitoring.py (O.5c-3) exactly in discipline:
filesystem-based, reads only media_backup_success.json under
settings.BACKUP_METADATA_PATH, written by
deploy/scripts/backup_media_offsite.sh (O.5e-2). No import of the Django
ORM, redis, Celery, or subprocess anywhere in this module — and,
critically, this module NEVER invokes rclone or tar and NEVER makes a
network call. The strong verification (SHA-256 round-trip + `tar -tf` on
the recovered copy) already happened once, durably, inside
backup_media_offsite.sh — this module never repeats it, it only reads
the evidence that verification left behind.

media_backup_failure.json (also written by backup_media_offsite.sh) is
never read here — by design, mirroring O.5a/O.5c-3's own choice: the
only question this module answers is about the last verified SUCCESS; a
failure must never influence that answer.

If settings.OFFSITE_CONFIGURED is False, this module short-circuits to
status="not_configured" without touching the filesystem at all — media
backup reuses the EXISTING RCLONE_REMOTE-derived contract from O.5c-1/
O.5c-3 rather than introducing a second, parallel enablement flag
(O.5e-3 approved decision — backup_media_offsite.sh itself refuses to
run without RCLONE_REMOTE set, exactly like backup_offsite.sh does, so
there is no scenario where media backup could be "configured" while
Postgres offsite is not, or vice versa).
"""
import json
from collections import namedtuple
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

MediaBackupStaleness = namedtuple(
    "MediaBackupStaleness",
    ["status", "last_success_at", "age_seconds", "stale_after_seconds"],
)

_SUCCESS_FILENAME = "media_backup_success.json"

_REQUIRED_FIELDS = {
    "schema_version", "timestamp_utc", "archive_filename", "size_bytes",
    "sha256", "file_count", "remote_target", "hostname", "integrity_verified",
}

_REQUIRED_NONEMPTY_STRING_FIELDS = (
    "archive_filename", "remote_target", "hostname",
)


def inspect_media_backup_staleness(*, stale_after_seconds: int = None) -> MediaBackupStaleness:
    """
    Read-only inspection of media_backup_success.json's freshness.

    Returns a MediaBackupStaleness namedtuple:
        status:               one of "fresh" / "stale" / "missing" /
                               "invalid" / "not_configured".
                               "not_configured" — settings.OFFSITE_CONFIGURED
                               is False; offsite verification (Postgres
                               AND media both) is deliberately not in use
                               on this deployment.
                               "missing" — offsite IS configured, but the
                               file has never been written (no verified
                               media offsite backup has ever completed).
                               "invalid" — the file exists but is
                               corrupt, unreadable, or internally
                               inconsistent (invalid JSON, missing
                               required fields, integrity_verified !=
                               True, malformed sha256, invalid
                               size_bytes/file_count, empty required
                               string field, unparseable/future
                               timestamp, or a read/permission error) —
                               deliberately distinct from "missing":
                               there IS evidence, but it cannot be
                               trusted. A corrupt/incomplete manifest can
                               NEVER produce "fresh".
        last_success_at:      aware datetime of the last verified
                               success, or None if not "fresh"/"stale".
        age_seconds:          seconds since last_success_at, or None if
                               not "fresh"/"stale".
        stale_after_seconds:  the threshold actually used (echoes back
                               the argument or the settings-derived
                               default), or None for "not_configured".

    Never raises for any malformed input — every failure mode is caught
    and reported as status="invalid", exactly like
    offsite_monitoring.inspect_offsite_backup_staleness's own
    discipline. Never executes rclone, tar, or any subprocess — never
    makes a network call — never writes anything.
    """
    if not getattr(settings, "OFFSITE_CONFIGURED", False):
        return MediaBackupStaleness(
            status="not_configured", last_success_at=None,
            age_seconds=None, stale_after_seconds=None,
        )

    threshold = (
        settings.MEDIA_BACKUP_STALE_SECONDS if stale_after_seconds is None
        else stale_after_seconds
    )
    path = Path(settings.BACKUP_METADATA_PATH) / _SUCCESS_FILENAME

    if not path.exists():
        return MediaBackupStaleness(
            status="missing", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        raw = path.read_text()
    except OSError:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if not isinstance(data, dict) or not _REQUIRED_FIELDS.issubset(data.keys()):
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if data.get("integrity_verified") is not True:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    sha256_val = data.get("sha256")
    if not isinstance(sha256_val, str) or len(sha256_val) != 64:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )
    try:
        int(sha256_val, 16)
    except ValueError:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    size_bytes = data.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    # file_count MAY legitimately be 0 (an existing-but-empty MEDIA_ROOT
    # is a valid backup, O.5e-2 approved decision) — only negative or
    # non-integer values are invalid, unlike size_bytes above.
    file_count = data.get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    for field_name in _REQUIRED_NONEMPTY_STRING_FIELDS:
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            return MediaBackupStaleness(
                status="invalid", last_success_at=None,
                age_seconds=None, stale_after_seconds=threshold,
            )

    timestamp_raw = data.get("timestamp_utc")
    ts = parse_datetime(timestamp_raw) if isinstance(timestamp_raw, str) else None
    if ts is None:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.utc)

    now = timezone.now()
    if ts > now:
        return MediaBackupStaleness(
            status="invalid", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    age_seconds = (now - ts).total_seconds()
    status = "stale" if age_seconds > threshold else "fresh"
    return MediaBackupStaleness(
        status=status, last_success_at=ts,
        age_seconds=age_seconds, stale_after_seconds=threshold,
    )
