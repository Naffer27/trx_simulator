# simulator/tests/test_o5d3_django_migrate_check.py
"""
Microbloque O.5d-3 — Django Schema Compatibility Check + Final Restore E2E.

Covers the Level 2 verification step added to deploy/scripts/
restore_drill.sh: after Level 1 (pure SQL) checks pass, the script now
runs

    DB_NAME=<drill_db> DB_USER=<drill_role> DB_PASSWORD=<drill_password> \\
        <python> <manage.py> migrate --check --plan --no-color

against the freshly-restored TEMPORARY database — never the real
DB_NAME — and ONLY appends "django_migrate_check" to checks_passed
(and therefore only writes restore_drill_success.json) if that check
exits 0. `migrate --check --plan` is Django's own read-only mechanism:
it reports whether any migration is unapplied and exits non-zero if
so, WITHOUT ever applying one. Plain `manage.py migrate` (no --check)
is never invoked anywhere in this script — see
ScriptStructuralGuardTests below and its twin in
test_o5d1_restore_drill_foundation.py.

Applies identically to --source local and --source offsite (this file
covers both with the same fake-tool technique already established in
test_o5d1_restore_drill_foundation.py / test_o5d2_offsite_restore_drill.py
— createdb/dropdb/pg_restore/psql/rclone/python are ALWAYS fake
executables here; this suite never touches a real PostgreSQL install
or real Django settings).

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, docs/BOOK06_RC1_AUDIT.md, O.4a-O.4e, health/
monitoring, scheduler/systemd, or models/migrations.
"""
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase

_SCRIPT_PATH = str(
    Path(__file__).resolve().parent.parent.parent
    / "deploy" / "scripts" / "restore_drill.sh"
)

_VALID_DUMP_BYTES = b"fake dump bytes for O.5d-3 tests"


def _make_fake_python(bin_dir, call_log, *, fail=False, missing=False):
    """Fake python for the Django schema compatibility check. Logs the
    DB_NAME/DB_USER it was invoked with (never the real DB_NAME) so
    tests can assert against it directly."""
    if missing:
        return  # deliberately do not create the binary
    python_path = Path(bin_dir) / "python"
    fail_line = "echo 'fake: pending migrations detected' >&2; exit 1" if fail else (
        "echo 'Planned operations:'; echo '  No planned migration operations.'; exit 0"
    )
    python_path.write_text(f"""#!/usr/bin/env bash
echo "python $* DB_NAME=$DB_NAME DB_USER=$DB_USER" >> {call_log}
{fail_line}
""")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_fake_pg_tools(bin_dir, *, pg_restore_exec_fail=False):
    call_log = Path(bin_dir) / "calls.log"

    createdb_path = Path(bin_dir) / "createdb"
    createdb_path.write_text(f"#!/usr/bin/env bash\necho \"createdb $*\" >> {call_log}\nexit 0\n")

    dropdb_path = Path(bin_dir) / "dropdb"
    dropdb_path.write_text(f"#!/usr/bin/env bash\necho \"dropdb $*\" >> {call_log}\nexit 0\n")

    pg_restore_path = Path(bin_dir) / "pg_restore"
    exec_fail_line = "echo 'fake restore exec failure' >&2; exit 1" if pg_restore_exec_fail else "exit 0"
    pg_restore_path.write_text(f"""#!/usr/bin/env bash
if [[ "$*" == *"--list"* ]]; then
    exit 0
else
    echo "pg_restore $*" >> {call_log}
    {exec_fail_line}
fi
""")

    psql_path = Path(bin_dir) / "psql"
    psql_path.write_text(f"""#!/usr/bin/env bash
echo "psql $*" >> {call_log}
QUERY=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-tAc" ]; then QUERY="$arg"; fi
    prev="$arg"
done
case "$QUERY" in
    "SELECT 1;") echo "1" ;;
    "SELECT COUNT(*) FROM django_migrations;") echo "5" ;;
    *"table_name="*) echo "t" ;;
    *) echo "unexpected_query" ;;
esac
""")

    for p in (createdb_path, dropdb_path, pg_restore_path, psql_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return call_log


def _make_fake_rclone(bin_dir, call_log, served_bytes=_VALID_DUMP_BYTES):
    served_path = Path(bin_dir) / ".served_bytes"
    served_path.write_bytes(served_bytes)
    rclone_path = Path(bin_dir) / "rclone"
    rclone_path.write_text(f"""#!/usr/bin/env bash
echo "rclone $*" >> {call_log}
dst="${{@: -1}}"
cp "{served_path}" "$dst"
exit 0
""")
    rclone_path.chmod(rclone_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_local_manifest(meta_dir, backup_dir, *, filename="trx_sim_test_local.dump", dump_bytes=_VALID_DUMP_BYTES):
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / filename).write_bytes(dump_bytes)
    manifest = {
        "schema_version": 1, "timestamp_utc": "2026-01-01T03:00:00Z",
        "database_name": "trx_sim_test", "filename": filename,
        "size_bytes": len(dump_bytes), "integrity_verified": True, "hostname": "test-host",
    }
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "backup_success.json").write_text(json.dumps(manifest))
    return manifest


def _write_offsite_manifest(meta_dir, *, filename="trx_sim_test_offsite.dump", dump_bytes=_VALID_DUMP_BYTES, remote_target="testremote:path/trx_sim_test_offsite.dump"):
    import hashlib
    manifest = {
        "schema_version": 1, "timestamp_utc": "2026-01-01T03:30:00Z",
        "database_name": "trx_sim_test", "filename": filename,
        "size_bytes": len(dump_bytes), "sha256": hashlib.sha256(dump_bytes).hexdigest(),
        "restorability_verified": True, "remote_target": remote_target, "hostname": "test-host",
    }
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "offsite_success.json").write_text(json.dumps(manifest))
    return manifest


def _run_drill_script(*, bin_dir, meta_dir, backup_dir=None, source="local",
                       db_name="trx_sim_staging_test", password="drill-test-password",
                       python_path=None, manage_py_path=None, path_override=None, extra_env=None):
    env = dict(os.environ)
    env["PATH"] = path_override if path_override is not None else f"{bin_dir}:{env.get('PATH', '')}"
    if backup_dir is not None:
        env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["DB_NAME"] = db_name
    env["RESTORE_DRILL_DB_PASSWORD"] = password
    env["RESTORE_DRILL_PYTHON"] = python_path if python_path is not None else str(Path(bin_dir) / "python")
    env["RESTORE_DRILL_MANAGE_PY"] = manage_py_path if manage_py_path is not None else str(Path(bin_dir) / "manage.py")
    if source == "offsite":
        rclone_conf = Path(bin_dir) / "rclone.conf"
        rclone_conf.write_text("[testremote]\ntype = local\n")
        env["RCLONE_CONFIG"] = str(rclone_conf)
    if extra_env:
        env.update(extra_env)
    args = ["bash", _SCRIPT_PATH, "--source", source]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)


def _setup(work, source, *, django_fail=False, django_missing=False, pg_restore_exec_fail=False):
    bin_dir = Path(work) / "bin"; bin_dir.mkdir()
    meta_dir = Path(work) / "meta"
    call_log = _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=pg_restore_exec_fail)
    _make_fake_python(bin_dir, call_log, fail=django_fail, missing=django_missing)
    (Path(bin_dir) / "manage.py").write_text("# fake\n")
    if source == "local":
        backup_dir = Path(work) / "backups"
        _write_local_manifest(meta_dir, backup_dir)
        return bin_dir, meta_dir, call_log, backup_dir
    else:
        _make_fake_rclone(bin_dir, call_log)
        _write_offsite_manifest(meta_dir)
        return bin_dir, meta_dir, call_log, None


class DjangoMigrateCheckSuccessTests(TestCase):

    def test_django_check_success_local(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertIn("django_migrate_check", data["checks_passed"])

    def test_django_check_success_offsite(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, _ = _setup(work, "offsite")
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, source="offsite")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertIn("django_migrate_check", data["checks_passed"])

    def test_django_check_runs_after_level1_checks_in_order(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text().splitlines()
            tools_in_order = [c.split()[0] for c in calls]
            # psql (Level 1) strictly before python (Level 2); python
            # strictly before dropdb (cleanup).
            self.assertLess(tools_in_order.index("psql"), tools_in_order.index("python"))
            self.assertLess(tools_in_order.index("python"), tools_in_order.index("dropdb"))


class DjangoMigrateCheckDbNameTests(TestCase):
    """Requirement #7: the command must target the DRILL database,
    never the real DB_NAME."""

    def test_check_targets_drill_db_not_real_db_name_local(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local",
                db_name="trx_sim_REAL_PRODUCTION_DB",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text().splitlines()
            python_calls = [c for c in calls if c.startswith("python")]
            self.assertEqual(len(python_calls), 1)
            self.assertNotIn("DB_NAME=trx_sim_REAL_PRODUCTION_DB", python_calls[0])
            self.assertRegex(python_calls[0], r"DB_NAME=trx_restore_drill_\d{14}_[0-9a-f]{8}")
            self.assertIn("DB_USER=trx_sim_drill", python_calls[0])

    def test_check_targets_drill_db_not_real_db_name_offsite(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, _ = _setup(work, "offsite")
            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, source="offsite",
                db_name="trx_sim_REAL_PRODUCTION_DB",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text().splitlines()
            python_calls = [c for c in calls if c.startswith("python")]
            self.assertEqual(len(python_calls), 1)
            self.assertNotIn("DB_NAME=trx_sim_REAL_PRODUCTION_DB", python_calls[0])
            self.assertRegex(python_calls[0], r"DB_NAME=trx_restore_drill_\d{14}_[0-9a-f]{8}")


class DjangoMigrateCheckFailureTests(TestCase):

    def test_django_check_failure_local_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local", django_fail=True)
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("schema compatibility", failure["reason"].lower())

    def test_django_check_failure_offsite_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, _ = _setup(work, "offsite", django_fail=True)
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, source="offsite")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())

    def test_django_check_failure_still_drops_drill_db(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local", django_fail=True)
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertNotEqual(result.returncode, 0)
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("createdb") for c in calls))
            self.assertTrue(any(c.startswith("dropdb") for c in calls))
            # dropdb called exactly once (no duplicate cleanup — O.5d-3 fix).
            self.assertEqual(sum(1 for c in calls if c.startswith("dropdb")), 1)

    def test_django_check_failure_never_overwrites_prior_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            ok = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "restore_drill_success.json").read_text()

            _make_fake_python(bin_dir, call_log, fail=True)
            failed = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual((meta_dir / "restore_drill_success.json").read_text(), prior_success)

    def test_python_binary_missing_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local", django_missing=True)
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("restore_drill_python", failure["reason"].lower())
            # Failed before any destructive operation — no createdb call.
            self.assertFalse(call_log.exists())

    def test_manage_py_missing_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local",
                manage_py_path=str(Path(work) / "does_not_exist.py"),
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("restore_drill_manage_py", failure["reason"].lower())
            self.assertFalse(call_log.exists())


class NoPiiInOutputTests(TestCase):

    def test_success_metadata_contains_no_secrets_or_row_data(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local",
                password="SuperSecretDrillPassword123",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "restore_drill_success.json").read_text()
            self.assertNotIn("SuperSecretDrillPassword123", body)
            self.assertNotIn("password", body.lower())
            self.assertNotIn("dsn", body.lower())

    def test_stdout_stderr_contain_no_password(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir, meta_dir, call_log, backup_dir = _setup(work, "local")
            result = _run_drill_script(
                bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local",
                password="SuperSecretDrillPassword123",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("SuperSecretDrillPassword123", result.stdout)
            self.assertNotIn("SuperSecretDrillPassword123", result.stderr)


class ScriptStructuralGuardTests(TestCase):
    """Mirrors the twin guard in test_o5d1_restore_drill_foundation.py
    — kept here too since O.5d-3 is what actually introduced the
    manage.py invocation this guards against misuse of."""

    def test_migrate_never_invoked_without_check_flag(self):
        source = Path(_SCRIPT_PATH).read_text()
        code = "\n".join(
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        migrate_lines = [
            line for line in code.splitlines()
            if "RESTORE_DRILL_MANAGE_PY" in line and "migrate" in line
        ]
        self.assertTrue(migrate_lines)
        for line in migrate_lines:
            self.assertIn("--check", line)

    def test_no_plain_migrate_apply_anywhere(self):
        source = Path(_SCRIPT_PATH).read_text()
        self.assertNotIn('"migrate"\n', source)  # no bare migrate subcommand string alone


class OrderAndConvergenceTests(TestCase):
    """Requirement #9 building block: confirms both --source modes
    converge on the identical Level 1 -> Level 2 -> success sequence."""

    def test_both_sources_produce_identical_checks_passed_suffix(self):
        with tempfile.TemporaryDirectory() as work_local:
            bin_dir, meta_dir, _, backup_dir = _setup(work_local, "local")
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, backup_dir=backup_dir, source="local")
            self.assertEqual(result.returncode, 0, result.stderr)
            local_checks = json.loads((meta_dir / "restore_drill_success.json").read_text())["checks_passed"]

        with tempfile.TemporaryDirectory() as work_offsite:
            bin_dir, meta_dir, _, _ = _setup(work_offsite, "offsite")
            result = _run_drill_script(bin_dir=bin_dir, meta_dir=meta_dir, source="offsite")
            self.assertEqual(result.returncode, 0, result.stderr)
            offsite_checks = json.loads((meta_dir / "restore_drill_success.json").read_text())["checks_passed"]

        # Both share the identical Level 1 + Level 2 tail.
        shared_tail = [
            "dump_integrity", "createdb", "pg_restore_exec", "connectivity",
            "migrations_table_populated", "critical_tables_exist", "django_migrate_check",
        ]
        self.assertEqual(local_checks[-len(shared_tail):], shared_tail)
        self.assertEqual(offsite_checks[-len(shared_tail):], shared_tail)
