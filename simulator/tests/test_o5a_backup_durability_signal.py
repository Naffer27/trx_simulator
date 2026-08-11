# simulator/tests/test_o5a_backup_durability_signal.py
"""
Microbloque O.5a — Backup Durability Signal + Health Foundation.

Covers simulator/backup_monitoring.py (a filesystem-only, read-only
inspector — no ORM/Redis/Celery import anywhere in that module) and its
integration into GET /api/health/detail/ as a new "backup" block.

Frozen decisions under test (O.5a Fase 0 + approval):
  - backup.status == "ok"      → does NOT degrade the endpoint.
  - backup.status == "missing" → does NOT degrade the endpoint (O.5b's
    scheduler does not exist yet — "missing" is the expected state
    until then, same grace period already established for celery_beat
    in O.4e-3).
  - backup.status == "stale"   → DOES degrade to 503.
  - backup.status == "error"   → DOES degrade to 503 (a backup signal
    that exists but is corrupt/inconsistent is a real loss of
    observability, per explicit instruction — not a softer condition
    than staleness).

Also covers deploy/scripts/backup_postgres.sh's O.5a metadata-writing
behavior via subprocess, with pg_dump/pg_restore stubbed by fake
executables on a temporary PATH — never runs a real pg_dump, never
touches /var/backups/trx_sim or any real PostgreSQL instance.

Does NOT touch GET /api/health/ (the public liveness probe — its own
test_health_check.py::PublicHealthCheckTests locks its contract; a
regression test here reconfirms it, but the endpoint itself is
untouched), Treasury, Wallet/Ledger, or any O.4a-O.4e code.
"""
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from simulator.backup_monitoring import inspect_backup_staleness
from simulator.tests.factories import make_user
from simulator.views import health_detail_view

HEALTH_DETAIL_URL = "/api/health/detail/"
HEALTH_PUBLIC_URL = "/api/health/"

_SCRIPT_PATH = str(
    Path(__file__).resolve().parents[2] / "deploy" / "scripts" / "backup_postgres.sh"
)

_STALE_SECONDS = 129600  # settings default, echoed explicitly in assertions below


def _valid_metadata(*, age_seconds=0, **overrides):
    ts = timezone.now() - timezone.timedelta(seconds=age_seconds)
    data = {
        "schema_version": 1,
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "database_name": "trx_sim_test",
        "filename": "trx_sim_test_20260811_030000.dump",
        "size_bytes": 12345,
        "integrity_verified": True,
        "hostname": "test-host",
    }
    data.update(overrides)
    return data


def _write_json(dir_path, filename, data_or_text):
    path = Path(dir_path) / filename
    if isinstance(data_or_text, str):
        path.write_text(data_or_text)
    else:
        path.write_text(json.dumps(data_or_text))
    return path


# ─────────────────────────────────────────────
# simulator/backup_monitoring.py — reader unit tests
# ─────────────────────────────────────────────

class MissingTests(TestCase):

    def test_no_file_reports_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.last_success_at)
        self.assertIsNone(result.age_seconds)
        self.assertEqual(result.stale_after_seconds, _STALE_SECONDS)

    def test_empty_directory_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "missing")

    def test_metadata_dir_itself_absent_reports_missing_not_error(self):
        with tempfile.TemporaryDirectory() as d:
            nonexistent = str(Path(d) / "does" / "not" / "exist")
            with override_settings(BACKUP_METADATA_PATH=nonexistent):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "missing")


class FreshAndStaleTests(TestCase):

    def test_fresh_backup_reports_ok(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.last_success_at)
        self.assertAlmostEqual(result.age_seconds, 60, delta=2)

    def test_old_backup_reports_stale(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=_STALE_SECONDS + 100))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "stale")

    def test_exact_threshold_boundary_is_still_ok(self):
        # Time is frozen relative to the EXACT timestamp string that ends
        # up in the JSON (whole-second precision — the "%Y-%m-%dT%H:%M:%SZ"
        # format truncates microseconds) rather than relying on real
        # wall-clock elapsed time — same technique already used in
        # O.4e-3's ThresholdAndAgeTests for celery_beat, adapted for the
        # second-level truncation this metadata format uses.
        with tempfile.TemporaryDirectory() as d:
            written_ts = timezone.now().replace(microsecond=0)
            _write_json(d, "backup_success.json", _valid_metadata(
                timestamp_utc=written_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
            frozen_now = written_ts + timezone.timedelta(seconds=_STALE_SECONDS)
            with override_settings(BACKUP_METADATA_PATH=d):
                with patch("simulator.backup_monitoring.timezone.now", return_value=frozen_now):
                    result = inspect_backup_staleness()
        self.assertEqual(result.status, "ok")

    def test_one_second_past_threshold_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            written_ts = timezone.now().replace(microsecond=0)
            _write_json(d, "backup_success.json", _valid_metadata(
                timestamp_utc=written_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
            frozen_now = written_ts + timezone.timedelta(seconds=_STALE_SECONDS + 1)
            with override_settings(BACKUP_METADATA_PATH=d):
                with patch("simulator.backup_monitoring.timezone.now", return_value=frozen_now):
                    result = inspect_backup_staleness()
        self.assertEqual(result.status, "stale")

    def test_custom_stale_after_seconds_argument_is_echoed(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=10))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness(stale_after_seconds=5)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.stale_after_seconds, 5)


class CorruptOrInconsistentTests(TestCase):

    def test_invalid_json_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", "{not valid json!!")
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.last_success_at)

    def test_non_dict_json_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", json.dumps([1, 2, 3]))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_missing_required_field_reports_error(self):
        for field in ("schema_version", "timestamp_utc", "database_name",
                      "filename", "size_bytes", "integrity_verified", "hostname"):
            data = _valid_metadata()
            del data[field]
            with tempfile.TemporaryDirectory() as d:
                _write_json(d, "backup_success.json", data)
                with override_settings(BACKUP_METADATA_PATH=d):
                    result = inspect_backup_staleness()
            self.assertEqual(result.status, "error", f"missing field {field!r} did not report error")

    def test_unparseable_timestamp_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(timestamp_utc="not-a-date"))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_non_string_timestamp_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(timestamp_utc=12345))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_future_timestamp_reports_error(self):
        future = timezone.now() + timezone.timedelta(days=1)
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(
                timestamp_utc=future.strftime("%Y-%m-%dT%H:%M:%SZ")
            ))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.age_seconds, "future timestamp must never produce a negative age")

    def test_zero_size_bytes_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(size_bytes=0))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_negative_size_bytes_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(size_bytes=-5))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_non_numeric_size_bytes_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(size_bytes="a lot"))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_boolean_size_bytes_reports_error(self):
        # bool is a subclass of int in Python — must be explicitly rejected.
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(size_bytes=True))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_integrity_verified_false_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(integrity_verified=False))
            with override_settings(BACKUP_METADATA_PATH=d):
                result = inspect_backup_staleness()
        self.assertEqual(result.status, "error")

    def test_permission_denied_reports_error_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_json(d, "backup_success.json", _valid_metadata())
            os.chmod(path, 0o000)
            try:
                with override_settings(BACKUP_METADATA_PATH=d):
                    result = inspect_backup_staleness()
            finally:
                os.chmod(path, 0o640)  # restore so tempdir cleanup can remove it
        self.assertEqual(result.status, "error")


# ─────────────────────────────────────────────
# GET /api/health/detail/ integration
# ─────────────────────────────────────────────

class _StaffClientMixin:
    def setUp(self):
        self.staff = make_user(username=f"o5a_staff_{id(self)}")
        self.staff.is_staff = True
        self.staff.save()
        self.client.force_login(self.staff)
        super().setUp()


class HealthContractTests(_StaffClientMixin, TestCase):

    def test_backup_block_present_with_expected_keys(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("backup", data)
        self.assertEqual(
            set(data["backup"].keys()),
            {"status", "age_seconds", "threshold_seconds", "last_success_at"},
        )

    def test_ok_status_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["backup"]["status"], "ok")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_missing_status_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["backup"]["status"], "missing")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_stale_status_degrades_to_503(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=_STALE_SECONDS + 1))
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["backup"]["status"], "stale")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_error_status_degrades_to_503(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", "{corrupt")
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["backup"]["status"], "error")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_last_success_at_reflects_metadata_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=0))
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIsNotNone(resp.json()["backup"]["last_success_at"])

    def test_threshold_seconds_echoes_settings_default(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.json()["backup"]["threshold_seconds"], _STALE_SECONDS)


class IndependenceFromOtherSubsystemsTests(_StaffClientMixin, TestCase):

    def test_db_down_backup_still_reports_independently(self):
        # RequestFactory + a directly-attached request.user bypasses
        # session-backed authentication entirely (AuthenticationMiddleware
        # would otherwise hit the SAME global DB connection for the
        # session lookup on every request, so patching
        # connection.ensure_connection at the django.db level would break
        # auth itself, not just the view's own "db" check). This isolates
        # the mock to exactly the view's own db block.
        factory = RequestFactory()
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            request = factory.get(HEALTH_DETAIL_URL)
            request.user = self.staff
            with override_settings(BACKUP_METADATA_PATH=d):
                with patch("django.db.connection.ensure_connection", side_effect=Exception("db down")):
                    resp = health_detail_view(request)
        data = json.loads(resp.content)
        self.assertEqual(data["db"]["status"], "error")
        self.assertEqual(data["backup"]["status"], "ok")
        # Overall still degraded — because of db, not because of backup.
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_redis_down_backup_still_reports_independently(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            with override_settings(BACKUP_METADATA_PATH=d):
                with patch("redis.from_url", side_effect=Exception("redis down")):
                    resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["redis"]["status"], "error")
        self.assertEqual(data["backup"]["status"], "ok")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)


class ReadOnlyAndRegressionTests(_StaffClientMixin, TestCase):

    def test_repeated_get_never_creates_metadata_file(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    self.client.get(HEALTH_DETAIL_URL)
            self.assertFalse((Path(d) / "backup_success.json").exists())
            self.assertFalse((Path(d) / "backup_failure.json").exists())

    def test_repeated_get_does_not_modify_existing_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            original_mtime = path.stat().st_mtime
            with override_settings(BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    self.client.get(HEALTH_DETAIL_URL)
            self.assertEqual(path.stat().st_mtime, original_mtime)

    def test_no_secret_looking_keys_in_backup_block(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=60))
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        body = resp.content.decode().lower()
        for forbidden in ("password", "dsn", "secret", "db_user", "db_host", "db_port"):
            self.assertNotIn(forbidden, body)

    def test_db_and_redis_and_celery_beat_keys_still_present(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("db", data)
        self.assertIn("redis", data)
        self.assertIn("celery_beat", data)


class PublicHealthEndpointUntouchedTests(TestCase):
    """Regression: /api/health/ contract is byte-for-byte unaffected by O.5a."""

    def test_public_health_unaffected(self):
        resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(set(data.keys()), {"status"})
        self.assertEqual(data["status"], "ok")

    def test_public_health_body_has_no_backup_string(self):
        resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertNotIn(b'"backup"', resp.content)

    def test_public_health_unaffected_even_when_backup_is_stale(self):
        # Confirms the new "backup" block is wired exclusively into
        # health_detail_view, never into the public health_check view,
        # regardless of backup state.
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", _valid_metadata(age_seconds=_STALE_SECONDS + 1))
            with override_settings(BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class AuthRegressionTests(TestCase):

    def test_anonymous_still_forbidden(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_still_forbidden(self):
        user = make_user(username="o5a_nonstaff")
        self.client.force_login(user)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────
# deploy/scripts/backup_postgres.sh — subprocess integration
# (pg_dump/pg_restore stubbed — never touches a real DB)
# ─────────────────────────────────────────────

def _make_fake_pg_tools(bin_dir, *, dump_ok=True, restore_ok=True):
    dump_path = Path(bin_dir) / "pg_dump"
    restore_path = Path(bin_dir) / "pg_restore"

    if dump_ok:
        dump_path.write_text("#!/usr/bin/env bash\necho 'FAKE DUMP CONTENT FOR O5A TESTS'\n")
    else:
        dump_path.write_text("#!/usr/bin/env bash\necho 'fake pg_dump failure' >&2\nexit 1\n")

    if restore_ok:
        restore_path.write_text("#!/usr/bin/env bash\nexit 0\n")
    else:
        restore_path.write_text("#!/usr/bin/env bash\necho 'fake pg_restore failure' >&2\nexit 1\n")

    for p in (dump_path, restore_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_backup_script(bin_dir, backup_dir, metadata_dir, db_name="trx_sim_o5a_test"):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(metadata_dir)
    env["DB_NAME"] = db_name
    return subprocess.run(
        ["bash", _SCRIPT_PATH], env=env, capture_output=True, text=True, timeout=30,
    )


class BackupScriptSuccessTests(TestCase):

    def test_success_writes_valid_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)

            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            success_file = meta_dir / "backup_success.json"
            self.assertTrue(success_file.exists())
            data = json.loads(success_file.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["database_name"], "trx_sim_o5a_test")
            self.assertTrue(data["integrity_verified"])
            self.assertGreater(data["size_bytes"], 0)
            self.assertIn("hostname", data)
            self.assertIn("timestamp_utc", data)
            self.assertIn("filename", data)

    def test_success_file_permissions_are_640(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            mode = (meta_dir / "backup_success.json").stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o640)

    def test_success_metadata_contains_no_secrets(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            body = (meta_dir / "backup_success.json").read_text().lower()
            for forbidden in ("password", "dsn", "db_user", "db_host", "db_port"):
                self.assertNotIn(forbidden, body)

    def test_no_leftover_temp_files_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            leftovers = list(meta_dir.glob(".metadata.*"))
            self.assertEqual(leftovers, [])

    def test_reader_reads_script_written_metadata_as_ok(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            with override_settings(BACKUP_METADATA_PATH=str(meta_dir)):
                result = inspect_backup_staleness()
            self.assertEqual(result.status, "ok")


class BackupScriptFailureTests(TestCase):

    def test_pg_dump_failure_writes_failure_metadata_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=False)

            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((meta_dir / "backup_failure.json").exists())
            self.assertFalse((meta_dir / "backup_success.json").exists())

    def test_pg_restore_verification_failure_writes_failure_metadata_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=False)

            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((meta_dir / "backup_failure.json").exists())
            self.assertFalse((meta_dir / "backup_success.json").exists())

    def test_failure_never_overwrites_a_prior_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"

            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)
            ok_result = _run_backup_script(bin_dir, backup_dir, meta_dir)
            self.assertEqual(ok_result.returncode, 0, ok_result.stderr)
            success_file = meta_dir / "backup_success.json"
            original_content = success_file.read_text()

            _make_fake_pg_tools(bin_dir, dump_ok=False)
            fail_result = _run_backup_script(bin_dir, backup_dir, meta_dir)
            self.assertNotEqual(fail_result.returncode, 0)

            self.assertEqual(success_file.read_text(), original_content)
            self.assertTrue((meta_dir / "backup_failure.json").exists())

    def test_no_leftover_temp_files_after_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=False)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            leftovers = list(meta_dir.glob(".metadata.*"))
            self.assertEqual(leftovers, [])

    def test_failure_metadata_contains_no_secrets(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=False)

            _run_backup_script(bin_dir, backup_dir, meta_dir)

            body = (meta_dir / "backup_failure.json").read_text().lower()
            for forbidden in ("password", "dsn", "db_user", "db_host", "db_port"):
                self.assertNotIn(forbidden, body)
