# simulator/offsite_monitoring.py
"""
O.5c-3 — Offsite Backup Durability Signal (read-only inspector).

Answers a single question: "does a verified, sufficiently-recent
offsite copy of the last successful PostgreSQL backup exist?" —
nothing else. Mirrors simulator/backup_monitoring.py (O.5a) exactly in
discipline: filesystem-based, reads only offsite_success.json under
settings.BACKUP_METADATA_PATH, written by deploy/scripts/
backup_offsite.sh (O.5c-1). No import of the Django ORM, redis,
Celery, or subprocess anywhere in this module — and, critically, this
module NEVER invokes rclone or pg_restore and NEVER makes a network
call. The strong cryptographic/remote verification (SHA-256 round-trip
+ pg_restore --list against the recovered copy) already happened once,
durably, inside backup_offsite.sh — this module never repeats it, it
only reads the evidence that verification left behind.

offsite_failure.json (also written by backup_offsite.sh) is never read
here — by design, mirroring O.5a's own choice for backup_failure.json:
the only question this module answers is about the last verified
SUCCESS; a failure must never influence that answer.

If settings.OFFSITE_CONFIGURED is False, this module short-circuits to
status="not_configured" without touching the filesystem at all — see
that setting's own comment in trx_simulator/settings.py for why it
reuses O.5c-1's existing RCLONE_REMOTE contract instead of introducing
a second, parallel enablement flag (O.5c-3 Fase 0 approval).
"""
import json
from collections import namedtuple
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

OffsiteBackupStaleness = namedtuple(
    "OffsiteBackupStaleness",
    ["status", "last_verified_at", "age_seconds", "stale_after_seconds"],
)

_SUCCESS_FILENAME = "offsite_success.json"

_REQUIRED_FIELDS = {
    "schema_version", "timestamp_utc", "database_name", "filename",
    "size_bytes", "sha256", "restorability_verified", "remote_target",
    "hostname",
}

_REQUIRED_NONEMPTY_STRING_FIELDS = (
    "database_name", "filename", "remote_target", "hostname",
)


def inspect_offsite_backup_staleness(*, stale_after_seconds: int = None) -> OffsiteBackupStaleness:
    """
    Read-only inspection of offsite_success.json's freshness.

    Returns an OffsiteBackupStaleness namedtuple:
        status:               one of "fresh" / "stale" / "missing" /
                               "invalid" / "not_configured".
                               "not_configured" — settings.OFFSITE_CONFIGURED
                               is False; offsite verification is
                               deliberately not in use on this
                               deployment.
                               "missing" — offsite IS configured, but the
                               file has never been written (no verified
                               offsite copy has ever completed).
                               "invalid" — the file exists but is
                               corrupt, unreadable, or internally
                               inconsistent (invalid JSON, missing
                               required fields, restorability_verified
                               != True, malformed sha256, invalid
                               size_bytes, unparseable/future timestamp,
                               empty required string field, or a
                               read/permission error) — deliberately
                               distinct from "missing": there IS
                               evidence, but it cannot be trusted. A
                               corrupt/incomplete manifest can NEVER
                               produce "fresh".
        last_verified_at:      aware datetime of the last verified
                               offsite success, or None if not
                               "fresh"/"stale".
        age_seconds:          seconds since last_verified_at, or None
                               if not "fresh"/"stale".
        stale_after_seconds:  the threshold actually used (echoes back
                               the argument or the settings-derived
                               default), or None for "not_configured".

    Never raises for any malformed input — every failure mode is
    caught and reported as status="invalid", exactly like
    backup_monitoring.inspect_backup_staleness's own discipline. Never
    executes rclone, pg_restore, or any subprocess — never makes a
    network call — never writes anything.
    """
    if not getattr(settings, "OFFSITE_CONFIGURED", False):
        return OffsiteBackupStaleness(
            status="not_configured", last_verified_at=None,
            age_seconds=None, stale_after_seconds=None,
        )

    threshold = (
        settings.OFFSITE_STALE_SECONDS if stale_after_seconds is None
        else stale_after_seconds
    )
    path = Path(settings.BACKUP_METADATA_PATH) / _SUCCESS_FILENAME

    if not path.exists():
        return OffsiteBackupStaleness(
            status="missing", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        raw = path.read_text()
    except OSError:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if not isinstance(data, dict) or not _REQUIRED_FIELDS.issubset(data.keys()):
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if data.get("restorability_verified") is not True:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    sha256_val = data.get("sha256")
    if not isinstance(sha256_val, str) or len(sha256_val) != 64:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )
    try:
        int(sha256_val, 16)
    except ValueError:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    size_bytes = data.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    for field_name in _REQUIRED_NONEMPTY_STRING_FIELDS:
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            return OffsiteBackupStaleness(
                status="invalid", last_verified_at=None,
                age_seconds=None, stale_after_seconds=threshold,
            )

    timestamp_raw = data.get("timestamp_utc")
    ts = parse_datetime(timestamp_raw) if isinstance(timestamp_raw, str) else None
    if ts is None:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.utc)

    now = timezone.now()
    if ts > now:
        return OffsiteBackupStaleness(
            status="invalid", last_verified_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    age_seconds = (now - ts).total_seconds()
    status = "stale" if age_seconds > threshold else "fresh"
    return OffsiteBackupStaleness(
        status=status, last_verified_at=ts,
        age_seconds=age_seconds, stale_after_seconds=threshold,
    )
