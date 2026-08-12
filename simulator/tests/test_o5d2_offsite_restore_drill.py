# simulator/tests/test_o5d2_offsite_restore_drill.py
"""
Microbloque O.5d-2 — Verified Offsite Restore Drill.

Covers deploy/scripts/restore_drill.sh's `--source offsite` path
exclusively via real subprocess runs. Two stubbing strategies:

  - createdb/dropdb/pg_restore/psql are ALWAYS fake executables on a
    temporary PATH (mirrors O.5d-1's own pattern) — never requires or
    touches a real PostgreSQL install by default.
  - rclone is ALWAYS a fake executable too for the primary suite below
    (full control over exactly what bytes get "downloaded", to
    exercise size/checksum match AND mismatch deterministically). A
    SEPARATE, real-rclone-if-available class re-runs the core
    download+verify path against rclone's own `local` backend — no
    network, no real credentials — for a stronger, non-simulated proof
    that the actual rclone command sequence works (mirrors O.5c-1's
    own technique).

Frozen decisions under test (O.5d-2 Fase 0 + approval):
  - --source offsite reads offsite_success.json (O.5c-1) EXCLUSIVELY —
    never backup_success.json, never BACKUP_DIR, even if a same-named
    file still exists there.
  - No flag/env var lets the operator supply a remote filename, remote
    path, target database, or expected checksum — all of it comes
    from offsite_success.json's own fields (in particular
    "remote_target", already a complete, ready-to-use rclone source
    string, exactly as O.5c-1 wrote it).
  - Order is: read+validate offsite manifest -> download to
    independent scratch -> size match -> SHA-256 match -> pg_restore
    --list -> createdb -> pg_restore exec -> Level 1 SQL checks ->
    success -> cleanup (drop DB + delete scratch). createdb NEVER runs
    if any earlier offsite validation step fails.
  - A success with source="offsite" is the only evidence that counts
    as Production Restore READY — verified by inspecting
    checks_passed's exact contents in the success tests below.
  - Cleanup (DB drop AND scratch deletion) happens on every exit path:
    success, checksum mismatch, pg_restore failure, SQL check failure,
    timeout. The remote object itself is NEVER deleted or modified —
    only rclone `copyto` (read) is ever invoked, never `delete`/`sync`
    with a remote destination.

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, docs/BOOK06_RC1_AUDIT.md, O.4a-O.4e, health/
monitoring, scheduler/systemd, or models/migrations.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from django.test import TestCase

_SCRIPT_PATH = str(
    Path(__file__).resolve().parent.parent.parent
    / "deploy" / "scripts" / "restore_drill.sh"
)

_RCLONE_AVAILABLE = shutil.which("rclone") is not None
_REAL_PG_OPT_IN = os.environ.get("RESTORE_DRILL_TEST_REAL_PG") == "1"

_VALID_DUMP_BYTES = b"fake dump bytes for O.5d-2 offsite tests"


def _make_fake_python(bin_dir, *, fail=False):
    """Fake python for the O.5d-3 Django schema compatibility check.
    Logs the DB_NAME it received so tests can assert it is always the
    drill database, never the real DB_NAME."""
    call_log = Path(bin_dir) / "calls.log"
    python_path = Path(bin_dir) / "python"
    fail_line = "echo 'fake: pending migrations detected' >&2; exit 1" if fail else (
        "echo 'Planned operations:'; echo '  No planned migration operations.'; exit 0"
    )
    python_path.write_text(f"""#!/usr/bin/env bash
echo "python $* DB_NAME=$DB_NAME" >> {call_log}
{fail_line}
""")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    (Path(bin_dir) / "manage.py").write_text("# fake manage.py placeholder for tests\n")


def _make_fake_pg_tools(
    bin_dir, *,
    createdb_fail=False, dropdb_fail=False,
    pg_restore_list_fail=False, pg_restore_exec_fail=False,
    migrations_count=5, connectivity_ok=True, missing_tables=(),
    django_migrate_check_fail=False,
):
    _make_fake_python(bin_dir, fail=django_migrate_check_fail)
    call_log = Path(bin_dir) / "calls.log"

    createdb_path = Path(bin_dir) / "createdb"
    if createdb_fail:
        createdb_path.write_text("#!/usr/bin/env bash\necho 'fake createdb failure' >&2\nexit 1\n")
    else:
        createdb_path.write_text(f"#!/usr/bin/env bash\necho \"createdb $*\" >> {call_log}\nexit 0\n")

    dropdb_path = Path(bin_dir) / "dropdb"
    if dropdb_fail:
        dropdb_path.write_text("#!/usr/bin/env bash\necho 'fake dropdb failure' >&2\nexit 1\n")
    else:
        dropdb_path.write_text(f"#!/usr/bin/env bash\necho \"dropdb $*\" >> {call_log}\nexit 0\n")

    pg_restore_path = Path(bin_dir) / "pg_restore"
    list_fail_line = "echo 'fake corrupt dump' >&2; exit 1" if pg_restore_list_fail else "exit 0"
    exec_fail_line = "echo 'fake restore exec failure' >&2; exit 1" if pg_restore_exec_fail else "exit 0"
    pg_restore_path.write_text(f"""#!/usr/bin/env bash
if [[ "$*" == *"--list"* ]]; then
    {list_fail_line}
else
    echo "pg_restore $*" >> {call_log}
    {exec_fail_line}
fi
""")

    missing = set(missing_tables)
    psql_path = Path(bin_dir) / "psql"
    connectivity_result = "1" if connectivity_ok else "0"
    psql_path.write_text(f"""#!/usr/bin/env bash
QUERY=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-tAc" ]; then QUERY="$arg"; fi
    prev="$arg"
done
case "$QUERY" in
    "SELECT 1;") echo "{connectivity_result}" ;;
    "SELECT COUNT(*) FROM django_migrations;") echo "{migrations_count}" ;;
    *"table_name='django_migrations'"*) {"echo f" if "django_migrations" in missing else "echo t"} ;;
    *"table_name='django_content_type'"*) {"echo f" if "django_content_type" in missing else "echo t"} ;;
    *"table_name='auth_user'"*) {"echo f" if "auth_user" in missing else "echo t"} ;;
    *"table_name='django_session'"*) {"echo f" if "django_session" in missing else "echo t"} ;;
    *) echo "unexpected_query" ;;
esac
""")

    for p in (createdb_path, dropdb_path, pg_restore_path, psql_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return call_log


def _make_fake_rclone(bin_dir, call_log, *, fail=False, served_bytes=_VALID_DUMP_BYTES, sleep_seconds=0):
    """A fake rclone: `--config <cfg> copyto <src> <dst>` writes
    `served_bytes` to <dst>, letting tests deterministically control
    what the "download" produces (to exercise size/checksum match AND
    mismatch)."""
    served_path = Path(bin_dir) / ".served_bytes"
    served_path.write_bytes(served_bytes)
    rclone_path = Path(bin_dir) / "rclone"
    fail_line = "echo 'fake rclone failure' >&2; exit 1" if fail else ""
    sleep_line = f"sleep {sleep_seconds}" if sleep_seconds else ""
    rclone_path.write_text(f"""#!/usr/bin/env bash
{fail_line}
echo "rclone $*" >> {call_log}
{sleep_line}
dst="${{@: -1}}"
cp "{served_path}" "$dst"
exit 0
""")
    rclone_path.chmod(rclone_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_offsite_manifest(
    meta_dir, *,
    filename="trx_sim_test_20260101_030000.dump",
    dump_bytes=_VALID_DUMP_BYTES,
    remote_target="testremote:trx-sim-backups/trx_sim_test_20260101_030000.dump",
    restorability_verified=True,
    omit_field=None,
):
    manifest = {
        "schema_version": 1,
        "timestamp_utc": "2026-01-01T03:30:00Z",
        "database_name": "trx_sim_test",
        "filename": filename,
        "size_bytes": len(dump_bytes),
        "sha256": hashlib.sha256(dump_bytes).hexdigest(),
        "restorability_verified": restorability_verified,
        "remote_target": remote_target,
        "hostname": "test-host",
    }
    if omit_field:
        del manifest[omit_field]
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "offsite_success.json").write_text(json.dumps(manifest))
    return manifest


def _run_drill_script(
    *, bin_dir, meta_dir, backup_dir=None, source="offsite",
    db_name="trx_sim_staging_test", password="drill-test-password",
    rclone_config="dummy-rclone-config-present",
    path_override=None, timeout_seconds=None, extra_env=None,
    write_rclone_config=True,
):
    env = dict(os.environ)
    env["PATH"] = path_override if path_override is not None else f"{bin_dir}:{env.get('PATH', '')}"
    if backup_dir is not None:
        env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["DB_NAME"] = db_name
    env["RESTORE_DRILL_DB_PASSWORD"] = password
    if timeout_seconds is not None:
        env["RESTORE_DRILL_TIMEOUT_SECONDS"] = str(timeout_seconds)
    # O.5d-3 — Django schema compatibility check preconditions, always
    # required regardless of --source.
    env.setdefault("RESTORE_DRILL_PYTHON", str(Path(bin_dir) / "python"))
    env.setdefault("RESTORE_DRILL_MANAGE_PY", str(Path(bin_dir) / "manage.py"))

    if write_rclone_config:
        rclone_conf_path = Path(bin_dir) / "rclone.conf"
        rclone_conf_path.write_text("[testremote]\ntype = local\n")
        env["RCLONE_CONFIG"] = str(rclone_conf_path)
    else:
        env["RCLONE_CONFIG"] = rclone_config

    if extra_env:
        env.update(extra_env)
    args = ["bash", _SCRIPT_PATH, "--source", source]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)


# ─────────────────────────────────────────────
# Metadata validation — createdb must NEVER run on any of these
# ─────────────────────────────────────────────
class OffsiteMetadataValidationTests(TestCase):

    def test_missing_offsite_manifest_fails_without_touching_db(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir()
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists(), "no tool should be invoked when the manifest is missing")
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertEqual(failure["source"], "offsite")

    def test_malformed_manifest_missing_required_field_fails(self):
        for field in (
            "schema_version", "timestamp_utc", "database_name", "filename",
            "size_bytes", "sha256", "restorability_verified", "remote_target", "hostname",
        ):
            with tempfile.TemporaryDirectory() as work:
                bin_dir = Path(work) / "bin"; bin_dir.mkdir()
                meta_dir = Path(work) / "meta"
                call_log = _make_fake_pg_tools(bin_dir)
                _make_fake_rclone(bin_dir, call_log)
                _write_offsite_manifest(meta_dir, omit_field=field)

                result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
                self.assertNotEqual(result.returncode, 0, f"missing field {field!r} did not fail")
                self.assertFalse(call_log.exists(), f"missing field {field!r}: no tool should run")

    def test_restorability_not_verified_true_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir, restorability_verified=False)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists())

    def test_malformed_sha256_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            manifest = _write_offsite_manifest(meta_dir)
            data = json.loads((meta_dir / "offsite_success.json").read_text())
            data["sha256"] = "not-a-valid-hash"
            (meta_dir / "offsite_success.json").write_text(json.dumps(data))

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists())

    def test_rclone_config_missing_fails_before_any_download(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, write_rclone_config=False,
                rclone_config=str(Path(work) / "does_not_exist.conf"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists())

    def test_rclone_binary_missing_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)  # no rclone placed
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("rclone", failure["reason"].lower())

    def test_never_reads_backup_dir_or_backup_success_json(self):
        # Even if a same-named local dump AND a valid backup_success.json
        # exist, offsite mode must never touch either.
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            backup_dir = Path(work) / "backups"; backup_dir.mkdir()
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            manifest = _write_offsite_manifest(meta_dir)

            # A same-named local file with DIFFERENT (wrong) bytes — if
            # this were ever read, checksum would fail; success below
            # proves it never was.
            (backup_dir / manifest["filename"]).write_bytes(b"WRONG BYTES - must never be used")
            (meta_dir / "backup_success.json").write_text(json.dumps({
                "schema_version": 1, "timestamp_utc": "2026-01-01T03:00:00Z",
                "database_name": "trx_sim_test", "filename": manifest["filename"],
                "size_bytes": 30, "integrity_verified": True, "hostname": "h",
            }))
            backup_success_before = (meta_dir / "backup_success.json").read_text()

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((meta_dir / "backup_success.json").read_text(), backup_success_before)
            self.assertFalse((meta_dir / "backup_failure.json").exists())

    def test_works_even_when_backup_dir_does_not_exist_at_all(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir,
                backup_dir=Path(work) / "does" / "not" / "exist",
            )
            self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Downloaded-bytes verification — size and SHA-256, independently
# ─────────────────────────────────────────────
class DownloadVerificationTests(TestCase):

    def test_rclone_failure_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log, fail=True)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("rclone", failure["reason"].lower())
            calls = call_log.read_text().splitlines() if call_log.exists() else []
            self.assertFalse(any(c.startswith("createdb") for c in calls))

    def test_size_mismatch_fails_before_createdb(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            # rclone "downloads" different-length bytes than the manifest expects.
            _make_fake_rclone(bin_dir, call_log, served_bytes=b"short")
            _write_offsite_manifest(meta_dir, dump_bytes=_VALID_DUMP_BYTES)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("size", failure["reason"].lower())
            calls = call_log.read_text().splitlines()
            self.assertFalse(any(c.startswith("createdb") for c in calls))
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())

    def test_sha256_mismatch_same_size_fails_before_createdb(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            tampered = bytearray(_VALID_DUMP_BYTES)
            tampered[0] ^= 0xFF  # same length, different content/hash
            _make_fake_rclone(bin_dir, call_log, served_bytes=bytes(tampered))
            _write_offsite_manifest(meta_dir, dump_bytes=_VALID_DUMP_BYTES)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("sha-256", failure["reason"].lower())
            calls = call_log.read_text().splitlines()
            self.assertFalse(any(c.startswith("createdb") for c in calls))

    def test_pg_restore_list_failure_on_recovered_bytes_fails_before_createdb(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_list_fail=True)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            calls = call_log.read_text().splitlines()
            self.assertFalse(any(c.startswith("createdb") for c in calls))


# ─────────────────────────────────────────────
# Order of operations
# ─────────────────────────────────────────────
class OrderOfOperationsTests(TestCase):

    def test_success_path_order_download_then_createdb_then_restore_then_checks(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            calls = call_log.read_text().splitlines()
            tools_in_order = [c.split()[0] for c in calls]
            # rclone (download) strictly before createdb; createdb
            # before pg_restore (exec); pg_restore before dropdb (cleanup).
            self.assertLess(tools_in_order.index("rclone"), tools_in_order.index("createdb"))
            self.assertLess(tools_in_order.index("createdb"), tools_in_order.index("pg_restore"))
            self.assertLess(tools_in_order.index("pg_restore"), tools_in_order.index("dropdb"))

    def test_checks_passed_records_full_offsite_chain(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertEqual(data["source"], "offsite")
            for expected in (
                "offsite_metadata_valid", "offsite_download", "offsite_size_match",
                "offsite_sha256_match", "dump_integrity", "createdb", "pg_restore_exec",
                "connectivity", "migrations_table_populated", "critical_tables_exist",
            ):
                self.assertIn(expected, data["checks_passed"], f"missing check: {expected}")


# ─────────────────────────────────────────────
# Post-restore checks run against the DRILL DB, never DB_NAME
# ─────────────────────────────────────────────
class PostRestoreFailureTests(TestCase):

    def test_createdb_failure_writes_failure_and_never_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, createdb_fail=True)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())

    def test_pg_restore_exec_failure_still_drops_created_db(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("createdb") for c in calls))
            self.assertTrue(any(c.startswith("dropdb") for c in calls))

    def test_sql_check_failure_still_drops_created_db(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, missing_tables=("auth_user",))
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("dropdb") for c in calls))
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())

    def test_failure_never_overwrites_a_prior_offsite_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            ok = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "restore_drill_success.json").read_text()

            _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True)
            failed = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual((meta_dir / "restore_drill_success.json").read_text(), prior_success)


# ─────────────────────────────────────────────
# Cleanup and the remote object itself
# ─────────────────────────────────────────────
class CleanupTests(TestCase):

    def test_scratch_file_removed_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            leftovers = list(Path(tempfile.gettempdir()).glob("restore_drill_offsite.*"))
            self.assertEqual(leftovers, [], f"scratch file(s) left behind: {leftovers}")

    def test_scratch_file_removed_after_checksum_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log, served_bytes=b"tampered content here")
            _write_offsite_manifest(meta_dir, dump_bytes=_VALID_DUMP_BYTES)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            leftovers = list(Path(tempfile.gettempdir()).glob("restore_drill_offsite.*"))
            self.assertEqual(leftovers, [])

    def test_rclone_never_invoked_with_delete_or_sync_subcommand(self):
        # Structural: the remote object must never be written to or
        # deleted — only `copyto` (a read) may ever appear.
        source = Path(_SCRIPT_PATH).read_text()
        for forbidden in ("rclone delete", "rclone sync", "rclone purge", "rclone rmdir"):
            self.assertNotIn(forbidden, source)

    def test_dropdb_failure_after_offsite_success_checks_does_not_mask_reason(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True, dropdb_fail=True)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("pg_restore", failure["reason"].lower())


# ─────────────────────────────────────────────
# Concurrency and timeout
# ─────────────────────────────────────────────
@unittest.skipUnless(shutil.which("flock"), "flock binary not installed on this machine")
class LockContentionTests(TestCase):

    def test_lock_contention_never_touches_offsite_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log)
            _write_offsite_manifest(meta_dir)

            lock_path = meta_dir / ".restore_drill.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 205>"{lock_path}"; flock -n 205 && sleep 5'],
            )
            try:
                time.sleep(1)
                result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((meta_dir / "restore_drill_success.json").exists())
                self.assertFalse((meta_dir / "restore_drill_failure.json").exists())
                self.assertFalse(call_log.exists())
            finally:
                holder.wait(timeout=10)


class TimeoutTests(TestCase):

    def test_download_exceeding_timeout_is_killed_and_reported(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            _make_fake_rclone(bin_dir, call_log, sleep_seconds=10)
            _write_offsite_manifest(meta_dir)

            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, timeout_seconds=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())
            calls = call_log.read_text().splitlines() if call_log.exists() else []
            self.assertFalse(any(c.startswith("createdb") for c in calls))


# ─────────────────────────────────────────────
# OPTIONAL — real rclone, `local` backend, no network/credentials.
# Postgres tools remain stubbed (real-Postgres coverage stays in the
# separate RESTORE_DRILL_TEST_REAL_PG-gated class, unchanged from O.5d-1).
# ─────────────────────────────────────────────
@unittest.skipUnless(_RCLONE_AVAILABLE, "rclone binary not installed on this machine")
class RealRcloneLocalBackendIntegrationTests(TestCase):

    def test_real_rclone_download_and_checksum_verification_succeeds(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            call_log = _make_fake_pg_tools(bin_dir)  # Postgres tools stay stubbed

            dump_bytes = _VALID_DUMP_BYTES
            filename = "trx_sim_test_20260101_030000.dump"
            (remote_dir / filename).write_bytes(dump_bytes)

            rclone_conf = Path(work) / "rclone.conf"
            rclone_conf.write_text("[realtestremote]\ntype = local\n")

            manifest = _write_offsite_manifest(
                meta_dir, filename=filename, dump_bytes=dump_bytes,
                remote_target=f"realtestremote:{remote_dir}/{filename}",
            )

            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir,
                write_rclone_config=False, rclone_config=str(rclone_conf),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertEqual(data["source"], "offsite")
            self.assertIn("offsite_sha256_match", data["checks_passed"])

    def test_real_rclone_detects_tampered_remote_object(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_tools(bin_dir)

            dump_bytes = _VALID_DUMP_BYTES
            filename = "trx_sim_test_20260101_030000.dump"
            # The manifest records a checksum for dump_bytes, but the
            # REAL remote object (what rclone will actually fetch) is
            # tampered — proves the real-rclone path catches this too.
            (remote_dir / filename).write_bytes(b"TAMPERED ON DISK REMOTE")

            rclone_conf = Path(work) / "rclone.conf"
            rclone_conf.write_text("[realtestremote]\ntype = local\n")

            _write_offsite_manifest(
                meta_dir, filename=filename, dump_bytes=dump_bytes,
                remote_target=f"realtestremote:{remote_dir}/{filename}",
            )

            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir,
                write_rclone_config=False, rclone_config=str(rclone_conf),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())


# ─────────────────────────────────────────────
# OPTIONAL — real local PostgreSQL, opt-in only (mirrors O.5d-1's own
# placeholder — not filled in here either, same rationale).
# ─────────────────────────────────────────────
@unittest.skipUnless(_REAL_PG_OPT_IN, "set RESTORE_DRILL_TEST_REAL_PG=1 to run against a real local Postgres")
class RealPostgresOffsiteIntegrationTests(TestCase):

    def test_full_real_offsite_drill_against_real_postgres(self):
        self.skipTest(
            "Real-Postgres end-to-end offsite wiring is an operator-provisioned "
            "manual exercise (see O.5d-2 closing report for the full manual "
            "smoke-test transcript) — this class exists as the designated "
            "opt-in slot, not auto-implemented."
        )
