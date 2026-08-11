# simulator/backup_monitoring.py
"""
O.5a — Backup Durability Signal (read-only inspector).

Answers a single question: "how long has it been since the last
verified-successful PostgreSQL backup?" — nothing else. Deliberately
filesystem-based (O.5a Fase 0 §2/§8/§13): reads only
backup_success.json under settings.BACKUP_METADATA_PATH, written by
deploy/scripts/backup_postgres.sh. No import of the Django ORM, redis,
or Celery anywhere in this module — the signal must stay readable even
if PostgreSQL or Redis is down, since that is exactly the scenario in
which an operator most needs to know whether a usable backup exists.

backup_failure.json (also written by the script) is never read here —
by design (O.5a Fase 0 §2/§5), the only question this module answers
is about the last SUCCESS; a failure must never influence that answer.
"""
import json
from collections import namedtuple
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

BackupStaleness = namedtuple(
    "BackupStaleness",
    ["status", "last_success_at", "age_seconds", "stale_after_seconds"],
)

_SUCCESS_FILENAME = "backup_success.json"

_REQUIRED_FIELDS = {
    "schema_version", "timestamp_utc", "database_name", "filename",
    "size_bytes", "integrity_verified", "hostname",
}


def inspect_backup_staleness(*, stale_after_seconds: int = None) -> BackupStaleness:
    """
    Read-only inspection of backup_success.json's freshness.

    Returns a BackupStaleness namedtuple:
        status:               one of "ok" / "stale" / "missing" / "error".
                               "missing" — the file has never been written
                               (no successful backup has ever completed, or
                               O.5b's scheduler does not exist yet — see
                               O.5a Fase 0 §7, an expected state until O.5b).
                               "error" — the file exists but is corrupt,
                               unreadable, or internally inconsistent
                               (invalid JSON, missing required fields,
                               unparseable/future timestamp, invalid
                               size_bytes, integrity_verified != True, or a
                               read/permission error) — deliberately
                               distinct from "missing": there IS evidence,
                               but it cannot be trusted.
        last_success_at:      aware datetime of the last verified success,
                               or None if not "ok"/"stale".
        age_seconds:          seconds since last_success_at, or None if not
                               "ok"/"stale".
        stale_after_seconds:  the threshold actually used (echoes back the
                               argument or the settings-derived default).

    Never raises for any malformed input — every failure mode is caught
    and reported as status="error", exactly like health_detail_view's own
    per-subsystem try/except discipline for db/redis/celery_beat.
    """
    threshold = (
        settings.BACKUP_STALE_SECONDS if stale_after_seconds is None
        else stale_after_seconds
    )
    path = Path(settings.BACKUP_METADATA_PATH) / _SUCCESS_FILENAME

    if not path.exists():
        return BackupStaleness(
            status="missing", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        raw = path.read_text()
    except OSError:
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if not isinstance(data, dict) or not _REQUIRED_FIELDS.issubset(data.keys()):
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    if data.get("integrity_verified") is not True:
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    size_bytes = data.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    timestamp_raw = data.get("timestamp_utc")
    ts = parse_datetime(timestamp_raw) if isinstance(timestamp_raw, str) else None
    if ts is None:
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.utc)

    now = timezone.now()
    if ts > now:
        return BackupStaleness(
            status="error", last_success_at=None,
            age_seconds=None, stale_after_seconds=threshold,
        )

    age_seconds = (now - ts).total_seconds()
    status = "stale" if age_seconds > threshold else "ok"
    return BackupStaleness(
        status=status, last_success_at=ts,
        age_seconds=age_seconds, stale_after_seconds=threshold,
    )
