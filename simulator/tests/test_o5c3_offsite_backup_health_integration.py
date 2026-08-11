# simulator/tests/test_o5c3_offsite_backup_health_integration.py
"""
Microbloque O.5c-3 — Offsite Backup Inspector + Health Integration.

Covers simulator/offsite_monitoring.py (a filesystem-only, read-only
inspector — no ORM/Redis/Celery/subprocess/network import anywhere in
that module) and its integration into GET /api/health/detail/ as a new
"offsite_backup" block.

Frozen decisions under test (O.5c-3 Fase 0 + approval):
  - offsite_backup.status == "fresh"          → never degrades.
  - offsite_backup.status == "stale"          → ALWAYS degrades (only
    reachable when settings.OFFSITE_CONFIGURED is True).
  - offsite_backup.status == "invalid"        → ALWAYS degrades (same
    reachability condition as "stale").
  - offsite_backup.status == "missing"        → degrades ONLY when
    settings.APP_ENV == "production"; visible but non-degrading in
    staging/development.
  - offsite_backup.status == "not_configured" → same environment-gated
    rule as "missing" (settings.OFFSITE_CONFIGURED is False).
  - settings.OFFSITE_CONFIGURED reuses O.5c-1's EXISTING RCLONE_REMOTE
    contract (bool(os.getenv("RCLONE_REMOTE", "").strip())) — no second,
    parallel enablement flag was introduced (O.5c-3 Fase 0 approval).

The source of truth is EXCLUSIVELY offsite_success.json (O.5c-1) — this
module never re-verifies the remote object, never executes rclone or
pg_restore, never makes a network call, never writes any manifest.

Does NOT touch GET /api/health/ (the public liveness probe — regression
reconfirmed here, endpoint itself untouched), Treasury, Wallet/Ledger,
treasury_engine, or any O.4a-O.4e/O.5a/O.5b/O.5c-1/O.5c-2 code.
"""
import ast
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from simulator import offsite_monitoring
from simulator.offsite_monitoring import inspect_offsite_backup_staleness
from simulator.tests.factories import make_user
from simulator.views import health_detail_view

HEALTH_DETAIL_URL = "/api/health/detail/"
HEALTH_PUBLIC_URL = "/api/health/"

_STALE_SECONDS = 129600  # settings default (OFFSITE_STALE_SECONDS), echoed explicitly below
_SENSITIVE_REMOTE_TARGET = "trx-r2:s3cretbucket123/trx_sim_test_20260811_030000.dump"


def _valid_offsite_metadata(*, age_seconds=0, **overrides):
    ts = timezone.now() - timezone.timedelta(seconds=age_seconds)
    data = {
        "schema_version": 1,
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "database_name": "trx_sim_test",
        "filename": "trx_sim_test_20260811_030000.dump",
        "size_bytes": 12345,
        "sha256": "a" * 64,
        "restorability_verified": True,
        "remote_target": _SENSITIVE_REMOTE_TARGET,
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
# simulator/offsite_monitoring.py — reader unit tests
# ─────────────────────────────────────────────

class NotConfiguredTests(TestCase):

    def test_offsite_configured_false_reports_not_configured(self):
        with override_settings(OFFSITE_CONFIGURED=False):
            result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "not_configured")
        self.assertIsNone(result.last_verified_at)
        self.assertIsNone(result.age_seconds)
        self.assertIsNone(result.stale_after_seconds)

    def test_not_configured_never_touches_filesystem(self):
        # If OFFSITE_CONFIGURED is False, the function must short-circuit
        # BEFORE any Path.exists()/read — proven by making any such call
        # explode.
        with patch("pathlib.Path.exists", side_effect=AssertionError("must not touch filesystem")):
            with override_settings(OFFSITE_CONFIGURED=False):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "not_configured")

    def test_offsite_configured_true_no_file_is_missing_not_not_configured(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "missing")


class MissingTests(TestCase):

    def test_no_file_reports_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.last_verified_at)
        self.assertIsNone(result.age_seconds)
        self.assertEqual(result.stale_after_seconds, _STALE_SECONDS)

    def test_metadata_dir_itself_absent_reports_missing_not_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            nonexistent = str(Path(d) / "does" / "not" / "exist")
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=nonexistent):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "missing")


class FreshAndStaleTests(TestCase):

    def test_fresh_offsite_backup_reports_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "fresh")
        self.assertIsNotNone(result.last_verified_at)
        self.assertAlmostEqual(result.age_seconds, 60, delta=2)

    def test_old_offsite_backup_reports_stale(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=_STALE_SECONDS + 100))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "stale")

    def test_exact_threshold_boundary_is_still_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            written_ts = timezone.now().replace(microsecond=0)
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(
                timestamp_utc=written_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
            frozen_now = written_ts + timezone.timedelta(seconds=_STALE_SECONDS)
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                with patch("simulator.offsite_monitoring.timezone.now", return_value=frozen_now):
                    result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "fresh")

    def test_one_second_past_threshold_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            written_ts = timezone.now().replace(microsecond=0)
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(
                timestamp_utc=written_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
            frozen_now = written_ts + timezone.timedelta(seconds=_STALE_SECONDS + 1)
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                with patch("simulator.offsite_monitoring.timezone.now", return_value=frozen_now):
                    result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "stale")

    def test_custom_stale_after_seconds_argument_is_echoed(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=10))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness(stale_after_seconds=5)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.stale_after_seconds, 5)


class InvalidTests(TestCase):

    def test_invalid_json_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", "{not valid json!!")
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")
        self.assertIsNone(result.last_verified_at)

    def test_non_dict_json_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", json.dumps([1, 2, 3]))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_missing_required_field_reports_invalid(self):
        for field in (
            "schema_version", "timestamp_utc", "database_name", "filename",
            "size_bytes", "sha256", "restorability_verified", "remote_target",
            "hostname",
        ):
            data = _valid_offsite_metadata()
            del data[field]
            with tempfile.TemporaryDirectory() as d:
                _write_json(d, "offsite_success.json", data)
                with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                    result = inspect_offsite_backup_staleness()
            self.assertEqual(result.status, "invalid", f"missing field {field!r} did not report invalid")

    def test_restorability_verified_false_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(restorability_verified=False))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_restorability_verified_missing_truthy_string_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(restorability_verified="true"))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_sha256_wrong_length_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(sha256="abc123"))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_sha256_non_hex_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(sha256="z" * 64))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_sha256_non_string_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(sha256=12345))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_zero_size_bytes_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(size_bytes=0))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_negative_size_bytes_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(size_bytes=-5))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_non_numeric_size_bytes_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(size_bytes="a lot"))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_boolean_size_bytes_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(size_bytes=True))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_empty_string_required_field_reports_invalid(self):
        for field in ("database_name", "filename", "remote_target", "hostname"):
            with tempfile.TemporaryDirectory() as d:
                _write_json(d, "offsite_success.json", _valid_offsite_metadata(**{field: ""}))
                with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                    result = inspect_offsite_backup_staleness()
            self.assertEqual(result.status, "invalid", f"empty {field!r} did not report invalid")

    def test_unparseable_timestamp_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(timestamp_utc="not-a-date"))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_non_string_timestamp_reports_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(timestamp_utc=12345))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")

    def test_future_timestamp_reports_invalid(self):
        future = timezone.now() + timezone.timedelta(days=1)
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(
                timestamp_utc=future.strftime("%Y-%m-%dT%H:%M:%SZ")
            ))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                result = inspect_offsite_backup_staleness()
        self.assertEqual(result.status, "invalid")
        self.assertIsNone(result.age_seconds, "future timestamp must never produce a negative age")

    def test_permission_denied_reports_invalid_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_json(d, "offsite_success.json", _valid_offsite_metadata())
            os.chmod(path, 0o000)
            try:
                with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                    result = inspect_offsite_backup_staleness()
            finally:
                os.chmod(path, 0o640)
        self.assertEqual(result.status, "invalid")


class InspectorReadOnlyTests(TestCase):

    def test_repeated_calls_never_modify_the_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            original_mtime = path.stat().st_mtime
            original_content = path.read_text()
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    inspect_offsite_backup_staleness()
            self.assertEqual(path.stat().st_mtime, original_mtime)
            self.assertEqual(path.read_text(), original_content)

    def test_repeated_calls_never_create_any_file(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    inspect_offsite_backup_staleness()
            self.assertEqual(list(Path(d).iterdir()), [])


class ModuleSourceIsSubprocessAndNetworkFreeTests(TestCase):
    """Structural guarantee: this module CANNOT execute rclone/pg_restore
    or make a network call, because it never IMPORTS anything capable of
    doing so — a stronger proof than mocking at runtime. Uses `ast` to
    inspect only actual import statements, deliberately ignoring the
    module's own docstring/comments (which legitimately discuss these
    same tokens to document what the module does NOT do — a raw
    substring scan would false-positive against that documentation, the
    same lesson already learned in O.5c-2's own test suite)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = Path(offsite_monitoring.__file__).read_text()
        tree = ast.parse(source)
        cls.imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    cls.imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                cls.imported_modules.add(node.module.split(".")[0])
        cls.tree = tree

    def test_no_subprocess_or_process_execution_imports(self):
        for forbidden in ("subprocess", "os"):
            self.assertNotIn(forbidden, self.imported_modules)

    def test_no_network_related_imports(self):
        for forbidden in ("socket", "requests", "urllib", "http"):
            self.assertNotIn(forbidden, self.imported_modules)

    def test_no_dynamic_code_execution_calls(self):
        # No eval()/exec()/__import__() anywhere — rules out indirectly
        # invoking subprocess/socket/rclone through obfuscated code paths.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("eval", "exec", "__import__"))

    def test_no_orm_or_financial_model_import(self):
        # Mirrors backup_monitoring.py's own guarantee — cannot mutate
        # Treasury/Wallet/Ledger/BrokerAuditEvent/AuditLog if it never
        # imports any of them.
        self.assertNotIn("models", self.imported_modules)


# ─────────────────────────────────────────────
# GET /api/health/detail/ integration
# ─────────────────────────────────────────────

class _StaffClientMixin:
    def setUp(self):
        self.staff = make_user(username=f"o5c3_staff_{id(self)}")
        self.staff.is_staff = True
        self.staff.save()
        self.client.force_login(self.staff)
        super().setUp()


class HealthContractTests(_StaffClientMixin, TestCase):

    def test_offsite_backup_block_present_with_expected_keys(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("offsite_backup", data)
        self.assertEqual(
            set(data["offsite_backup"].keys()),
            {"status", "age_seconds", "threshold_seconds", "last_verified_at"},
        )

    def test_fresh_status_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="production"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "fresh")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_missing_in_production_degrades_to_503(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="production"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "missing")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_missing_in_staging_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="staging"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "missing")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_missing_in_development_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="development"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "missing")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_not_configured_in_production_degrades_to_503(self):
        with override_settings(OFFSITE_CONFIGURED=False, APP_ENV="production"):
            resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "not_configured")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_not_configured_in_development_does_not_degrade(self):
        with override_settings(OFFSITE_CONFIGURED=False, APP_ENV="development"):
            resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "not_configured")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_not_configured_in_staging_does_not_degrade(self):
        with override_settings(OFFSITE_CONFIGURED=False, APP_ENV="staging"):
            resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "not_configured")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_stale_degrades_even_outside_production(self):
        # "stale" is gated only by OFFSITE_CONFIGURED, never by APP_ENV —
        # distinct from missing/not_configured's environment-gated rule.
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=_STALE_SECONDS + 1))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="staging"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "stale")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_invalid_degrades_even_outside_production(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", "{corrupt")
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d, APP_ENV="development"):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["offsite_backup"]["status"], "invalid")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_last_verified_at_reflects_metadata_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=0))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIsNotNone(resp.json()["offsite_backup"]["last_verified_at"])

    def test_threshold_seconds_echoes_settings_default(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.json()["offsite_backup"]["threshold_seconds"], _STALE_SECONDS)

    def test_not_configured_threshold_seconds_is_null(self):
        with override_settings(OFFSITE_CONFIGURED=False):
            resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIsNone(resp.json()["offsite_backup"]["threshold_seconds"])


class IndependenceFromOtherSubsystemsTests(_StaffClientMixin, TestCase):

    def test_db_down_offsite_backup_still_reports_independently(self):
        factory = RequestFactory()
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            request = factory.get(HEALTH_DETAIL_URL)
            request.user = self.staff
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                with patch("django.db.connection.ensure_connection", side_effect=Exception("db down")):
                    resp = health_detail_view(request)
        data = json.loads(resp.content)
        self.assertEqual(data["db"]["status"], "error")
        self.assertEqual(data["offsite_backup"]["status"], "fresh")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_redis_down_offsite_backup_still_reports_independently(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                with patch("redis.from_url", side_effect=Exception("redis down")):
                    resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["redis"]["status"], "error")
        self.assertEqual(data["offsite_backup"]["status"], "fresh")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_offsite_backup_stale_does_not_corrupt_backup_local_block(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "backup_success.json", {
                "schema_version": 1,
                "timestamp_utc": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "database_name": "trx_sim_test",
                "filename": "trx_sim_test_20260811_030000.dump",
                "size_bytes": 1,
                "integrity_verified": True,
                "hostname": "test-host",
            })
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=_STALE_SECONDS + 1))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["backup"]["status"], "ok")
        self.assertEqual(data["offsite_backup"]["status"], "stale")
        self.assertEqual(data["status"], "degraded")


class ReadOnlyAndRegressionTests(_StaffClientMixin, TestCase):

    def test_repeated_get_never_creates_offsite_metadata_file(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    self.client.get(HEALTH_DETAIL_URL)
            self.assertFalse((Path(d) / "offsite_success.json").exists())
            self.assertFalse((Path(d) / "offsite_failure.json").exists())

    def test_repeated_get_does_not_modify_existing_offsite_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            original_mtime = path.stat().st_mtime
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                for _ in range(5):
                    self.client.get(HEALTH_DETAIL_URL)
            self.assertEqual(path.stat().st_mtime, original_mtime)

    def test_never_executes_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                with patch("subprocess.run", side_effect=AssertionError("subprocess.run must never be called")), \
                     patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen must never be called")):
                    resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIn(resp.status_code, (200, 503))

    def test_no_secrets_or_remote_identifiers_in_response_body(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "offsite_success.json", _valid_offsite_metadata(age_seconds=60))
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        body = resp.content.decode()
        body_lower = body.lower()
        for forbidden in (
            "password", "dsn", "secret", "access_key", "rclone_config",
            "rclone.conf", "db_user", "db_host", "db_port", "endpoint",
        ):
            self.assertNotIn(forbidden, body_lower)
        # The manifest's own remote_target / sha256 / filename / database_name
        # must never surface in the health body — only the four approved keys.
        self.assertNotIn("s3cretbucket123", body)
        self.assertNotIn(_SENSITIVE_REMOTE_TARGET, body)
        self.assertNotIn("a" * 64, body)  # the sha256 value
        self.assertNotIn("trx_sim_test_20260811_030000.dump", body)

    def test_db_redis_celery_beat_backup_keys_still_present(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(OFFSITE_CONFIGURED=True, BACKUP_METADATA_PATH=d):
                resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("db", data)
        self.assertIn("redis", data)
        self.assertIn("celery_beat", data)
        self.assertIn("backup", data)


class PublicHealthEndpointUntouchedTests(TestCase):
    """Regression: /api/health/ contract is byte-for-byte unaffected by O.5c-3."""

    def test_public_health_unaffected(self):
        resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(set(data.keys()), {"status"})
        self.assertEqual(data["status"], "ok")

    def test_public_health_body_has_no_offsite_backup_string(self):
        resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertNotIn(b'"offsite_backup"', resp.content)

    def test_public_health_unaffected_even_when_offsite_backup_missing_in_production(self):
        with override_settings(OFFSITE_CONFIGURED=True, APP_ENV="production"):
            resp = self.client.get(HEALTH_PUBLIC_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class AuthRegressionTests(TestCase):

    def test_anonymous_still_forbidden(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_still_forbidden(self):
        user = make_user(username="o5c3_nonstaff")
        self.client.force_login(user)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_totp_gate_still_enforced_when_required(self):
        from simulator.models import TOTPDevice

        user = make_user(username="o5c3_totp_staff")
        user.is_staff = True
        user.save()
        # health_detail_view only checks .filter(confirmed=True).exists()
        # for this gate — it never decrypts .secret, so a raw placeholder
        # value is sufficient here.
        TOTPDevice.objects.create(user=user, confirmed=True, secret="placeholder")
        self.client.force_login(user)

        with override_settings(TOTP_STAFF_REQUIRED=True):
            resp = self.client.get(HEALTH_DETAIL_URL)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "2fa_required"})
