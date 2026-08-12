# simulator/tests/test_o5d1_restore_drill_foundation.py
"""
Microbloque O.5d-1 — Restore Drill Foundation.

Covers deploy/scripts/restore_drill.sh exclusively via real subprocess
runs — never imports/execs it as Python. Two stubbing strategies:

  - createdb/dropdb/pg_restore/psql are ALWAYS fake executables on a
    temporary PATH (mirrors O.5a/O.5b/O.5c-1's own pattern) — this
    suite never requires or touches a real PostgreSQL install by
    default.
  - date/od are ALSO stubbed in a small number of tests, deliberately
    and only to make the internally-generated drill database name
    DETERMINISTIC so the DB_NAME/DB_TEST_NAME collision guards can be
    exercised directly — this is a TEST-ONLY technique, never a
    backdoor in the script itself. restore_drill.sh has no
    environment variable or flag that can override the generated
    name; forcing a deterministic name for testing purposes requires
    controlling the OS-level date/random primitives it calls, not
    anything added to the script.

An OPTIONAL class requires a real local PostgreSQL server with the
dedicated trx_sim_drill role already provisioned — gated behind the
RESTORE_DRILL_TEST_REAL_PG=1 environment variable (opt-in, never
auto-detected by mere binary presence) — skipped by default.

Frozen decisions under test (O.5d Fase 0 + O.5d-1 approval):
  - The drill database name is generated INTERNALLY
    (trx_restore_drill_<UTC timestamp>_<random hex>) — there is no
    --database/--dbname/--target-db flag or equivalent env var.
  - Guards run BEFORE any destructive operation: regex pattern, !=
    DB_NAME, != effective DB_TEST_NAME, no staging/production/prod
    substring.
  - Cleanup trap is installed BEFORE createdb is ever attempted, and
    re-validates the drill name via the SAME guard function before
    ever calling dropdb.
  - Never --clean, never systemctl, never touches BACKUP_DIR, never
    reads/writes backup_success.json or backup_failure.json (O.5a).
  - --source offsite was a stub refusal at O.5d-1 authorship time
    ("not implemented yet"); it is now fully implemented (O.5d-2) —
    see test_o5d2_offsite_restore_drill.py for that path's exhaustive
    coverage. This file's own scope stays --source local only.

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, docs/BOOK06_RC1_AUDIT.md, O.4a-O.4e, or any
O.5a/O.5b/O.5c code.
"""
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

_REAL_PG_OPT_IN = os.environ.get("RESTORE_DRILL_TEST_REAL_PG") == "1"


def _make_fake_python(bin_dir, *, fail=False):
    """Fake python for the O.5d-3 Django schema compatibility check
    (`<manage.py> migrate --check --plan --no-color`). Logs the DB_NAME
    it was invoked with, so tests can assert it always equals the
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
    manage_py_path = Path(bin_dir) / "manage.py"
    manage_py_path.write_text("# fake manage.py placeholder for tests\n")
    return python_path, manage_py_path


def _make_fake_pg_tools(
    bin_dir, *,
    createdb_fail=False, dropdb_fail=False,
    pg_restore_list_fail=False, pg_restore_exec_fail=False,
    pg_restore_exec_sleep=0,
    migrations_count=5, connectivity_ok=True,
    missing_tables=(),
    django_migrate_check_fail=False,
):
    _make_fake_python(bin_dir, fail=django_migrate_check_fail)
    call_log = Path(bin_dir) / "calls.log"

    createdb_path = Path(bin_dir) / "createdb"
    if createdb_fail:
        createdb_path.write_text("#!/usr/bin/env bash\necho 'fake createdb failure' >&2\nexit 1\n")
    else:
        createdb_path.write_text(
            f"#!/usr/bin/env bash\necho \"createdb $*\" >> {call_log}\nexit 0\n"
        )

    dropdb_path = Path(bin_dir) / "dropdb"
    if dropdb_fail:
        dropdb_path.write_text("#!/usr/bin/env bash\necho 'fake dropdb failure' >&2\nexit 1\n")
    else:
        dropdb_path.write_text(
            f"#!/usr/bin/env bash\necho \"dropdb $*\" >> {call_log}\nexit 0\n"
        )

    pg_restore_path = Path(bin_dir) / "pg_restore"
    list_fail_line = (
        "echo 'fake corrupt dump' >&2; exit 1" if pg_restore_list_fail else "exit 0"
    )
    exec_fail_line = (
        "echo 'fake restore exec failure' >&2; exit 1" if pg_restore_exec_fail else
        (f"sleep {pg_restore_exec_sleep}; exit 0" if pg_restore_exec_sleep else "exit 0")
    )
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
    if [ "$prev" = "-tAc" ]; then
        QUERY="$arg"
    fi
    prev="$arg"
done
case "$QUERY" in
    "SELECT 1;")
        echo "{connectivity_result}"
        ;;
    "SELECT COUNT(*) FROM django_migrations;")
        echo "{migrations_count}"
        ;;
    *"table_name='django_migrations'"*)
        {"echo f" if "django_migrations" in missing else "echo t"}
        ;;
    *"table_name='django_content_type'"*)
        {"echo f" if "django_content_type" in missing else "echo t"}
        ;;
    *"table_name='auth_user'"*)
        {"echo f" if "auth_user" in missing else "echo t"}
        ;;
    *"table_name='django_session'"*)
        {"echo f" if "django_session" in missing else "echo t"}
        ;;
    *)
        echo "unexpected_query"
        ;;
esac
""")

    for p in (createdb_path, dropdb_path, pg_restore_path, psql_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return call_log


def _write_success_manifest(
    meta_dir, backup_dir, *,
    filename="trx_sim_test_20260101_030000.dump",
    content=b"fake dump bytes for O.5d-1 tests",
    size_override=None,
    integrity_verified=True,
):
    backup_dir.mkdir(parents=True, exist_ok=True)
    dump_path = backup_dir / filename
    dump_path.write_bytes(content)
    size_bytes = size_override if size_override is not None else len(content)

    manifest = {
        "schema_version": 1,
        "timestamp_utc": "2026-01-01T03:00:00Z",
        "database_name": "trx_sim_test",
        "filename": filename,
        "size_bytes": size_bytes,
        "integrity_verified": integrity_verified,
        "hostname": "test-host",
    }
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "backup_success.json").write_text(json.dumps(manifest))
    return dump_path, manifest


def _run_drill_script(
    *, bin_dir, backup_dir, meta_dir, source="local",
    db_name="trx_sim_staging_test", db_test_name=None,
    password="drill-test-password", path_override=None, timeout_seconds=None,
    extra_env=None,
):
    env = dict(os.environ)
    env["PATH"] = path_override if path_override is not None else f"{bin_dir}:{env.get('PATH', '')}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["DB_NAME"] = db_name
    if db_test_name is not None:
        env["DB_TEST_NAME"] = db_test_name
    env["RESTORE_DRILL_DB_PASSWORD"] = password
    if timeout_seconds is not None:
        env["RESTORE_DRILL_TIMEOUT_SECONDS"] = str(timeout_seconds)
    # O.5d-3 — Django schema compatibility check preconditions, always
    # required regardless of --source.
    env.setdefault("RESTORE_DRILL_PYTHON", str(Path(bin_dir) / "python"))
    env.setdefault("RESTORE_DRILL_MANAGE_PY", str(Path(bin_dir) / "manage.py"))
    if extra_env:
        env.update(extra_env)
    args = ["bash", _SCRIPT_PATH]
    if source is not None:
        args += ["--source", source]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)


def _make_deterministic_date_od(bin_dir, *, timestamp="20260101120000", suffix="deadbeef"):
    """Stubs date/od ONLY for the exact invocations restore_drill.sh uses
    to generate the drill name, deterministically — everything else
    (log() timestamps, etc.) passes through to the REAL binaries. This
    is a test-only technique to exercise the collision guards; it does
    NOT add any override capability to the script itself."""
    real_date = shutil.which("date")
    real_od = shutil.which("od")
    date_path = Path(bin_dir) / "date"
    date_path.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "-u" ] && [ "$2" = "+%Y%m%d%H%M%S" ]; then
    echo "{timestamp}"
else
    exec "{real_date}" "$@"
fi
""")
    od_path = Path(bin_dir) / "od"
    od_path.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "-An" ] && [ "$2" = "-tx1" ] && [ "$3" = "-N4" ]; then
    echo " {suffix}"
else
    exec "{real_od}" "$@"
fi
""")
    for p in (date_path, od_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f"trx_restore_drill_{timestamp}_{suffix}"


# ─────────────────────────────────────────────
# CLI contract / source argument
# ─────────────────────────────────────────────
class SourceArgumentTests(TestCase):

    def test_missing_source_argument_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, source=None,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_source_offsite_is_accepted_as_a_valid_value(self):
        # --source offsite was a stub refusal in O.5d-1 ("not
        # implemented yet") and is now fully implemented in O.5d-2 —
        # see test_o5d2_offsite_restore_drill.py for exhaustive
        # coverage of that path. This O.5d-1 test only confirms
        # "offsite" is not rejected as an invalid --source VALUE
        # (unlike test_source_invalid_value_rejected below) — with
        # this minimal local-mode fixture (no offsite_success.json,
        # no rclone) it fails cleanly on an offsite precondition
        # instead, never crashing or producing a malformed result.
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, source="offsite",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Invalid --source", result.stderr + result.stdout)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertEqual(failure["source"], "offsite")
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())

    def test_source_invalid_value_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, source="nonsense",
            )
            self.assertNotEqual(result.returncode, 0)


# ─────────────────────────────────────────────
# Precondition / input-validation failures
# ─────────────────────────────────────────────
class PreconditionTests(TestCase):

    def test_password_unset_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, password="",
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("RESTORE_DRILL_DB_PASSWORD", failure["reason"])

    def test_missing_backup_success_manifest_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"; backup_dir.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir()
            _make_fake_pg_tools(bin_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIsNone(failure["dump_filename"])

    def test_integrity_not_verified_true_refuses(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir, integrity_verified=False)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)

    def test_dump_file_missing_on_disk_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            dump_path, _ = _write_success_manifest(meta_dir, backup_dir)
            dump_path.unlink()

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)

    def test_size_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            dump_path, _ = _write_success_manifest(meta_dir, backup_dir)
            dump_path.write_bytes(b"different length content now")

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)

    def test_pg_restore_list_failure_never_creates_a_database(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_list_fail=True)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists(), "createdb must never be called when integrity check fails")


# ─────────────────────────────────────────────
# Successful end-to-end run
# ─────────────────────────────────────────────
class SuccessTests(TestCase):

    def _run_happy_path(self, work, **kwargs):
        bin_dir = Path(work) / "bin"; bin_dir.mkdir()
        backup_dir = Path(work) / "backups"
        meta_dir = Path(work) / "meta"
        call_log = _make_fake_pg_tools(bin_dir)
        dump_path, manifest = _write_success_manifest(meta_dir, backup_dir)
        result = _run_drill_script(
            bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, **kwargs,
        )
        return result, bin_dir, backup_dir, meta_dir, call_log, dump_path, manifest

    def test_success_writes_valid_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["source"], "local")
            self.assertEqual(data["dump_filename"], manifest["filename"])
            self.assertIn("connectivity", data["checks_passed"])
            self.assertIn("migrations_table_populated", data["checks_passed"])
            self.assertIn("critical_tables_exist", data["checks_passed"])
            self.assertRegex(data["drill_database_name"], r"^trx_restore_drill_\d{14}_[0-9a-f]{8}$")
            self.assertGreaterEqual(data["duration_seconds"], 0)

    def test_success_calls_createdb_pg_restore_and_dropdb_exactly_once_each(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text().splitlines()
            createdb_calls = [c for c in calls if c.startswith("createdb")]
            dropdb_calls = [c for c in calls if c.startswith("dropdb")]
            pg_restore_calls = [c for c in calls if c.startswith("pg_restore")]
            self.assertEqual(len(createdb_calls), 1)
            self.assertEqual(len(dropdb_calls), 1)
            self.assertEqual(len(pg_restore_calls), 1)
            # createdb and dropdb must target the SAME generated name.
            self.assertIn(
                createdb_calls[0].split()[-1], dropdb_calls[0],
            )

    def test_success_never_uses_clean_flag(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text()
            self.assertNotIn("--clean", calls)

    def test_success_never_touches_backup_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            before = (meta_dir / "backup_success.json").read_text()

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            after = (meta_dir / "backup_success.json").read_text()
            self.assertEqual(before, after)
            self.assertFalse((meta_dir / "backup_failure.json").exists())

    def test_success_leaves_dump_file_untouched_under_backup_dir(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dump_path.exists())
            self.assertEqual(dump_path.read_bytes(), b"fake dump bytes for O.5d-1 tests")

    def test_success_permissions_are_640(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            mode = (meta_dir / "restore_drill_success.json").stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o640)

    def test_no_leftover_temp_files(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(meta_dir.glob(".metadata.*")), [])

    def test_no_secrets_in_success_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_, meta_dir, call_log, dump_path, manifest = self._run_happy_path(
                work, password="SuperSecretPassword123",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "restore_drill_success.json").read_text()
            self.assertNotIn("SuperSecretPassword123", body)
            self.assertNotIn("password", body.lower())


# ─────────────────────────────────────────────
# Destructive operation failures — createdb/pg_restore-exec/Level-1 checks
# ─────────────────────────────────────────────
class FailureAndCleanupTests(TestCase):

    def test_createdb_failure_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, createdb_fail=True)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("createdb", failure["reason"].lower())

    def test_pg_restore_exec_failure_still_drops_the_created_database(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("createdb") for c in calls))
            self.assertTrue(any(c.startswith("dropdb") for c in calls),
                             "cleanup trap must drop the database even after a restore failure")

    def test_connectivity_check_failure_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, connectivity_ok=False)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("dropdb") for c in calls))

    def test_empty_migrations_table_fails(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, migrations_count=0)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("django_migrations", failure["reason"])
            calls = call_log.read_text().splitlines()
            self.assertTrue(any(c.startswith("dropdb") for c in calls))

    def test_missing_critical_table_fails(self):
        for missing_table in ("django_migrations", "django_content_type", "auth_user", "django_session"):
            with tempfile.TemporaryDirectory() as work:
                bin_dir = Path(work) / "bin"; bin_dir.mkdir()
                backup_dir = Path(work) / "backups"
                meta_dir = Path(work) / "meta"
                _make_fake_pg_tools(bin_dir, missing_tables=(missing_table,))
                _write_success_manifest(meta_dir, backup_dir)

                result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
                self.assertNotEqual(result.returncode, 0, f"missing table {missing_table!r} did not fail the drill")

    def test_dropdb_failure_does_not_mask_the_original_error(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True, dropdb_fail=True)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("pg_restore", failure["reason"].lower())

    def test_failure_never_overwrites_a_prior_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            ok = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "restore_drill_success.json").read_text()

            _make_fake_pg_tools(bin_dir, pg_restore_exec_fail=True)
            failed = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertNotEqual(failed.returncode, 0)

            self.assertEqual((meta_dir / "restore_drill_success.json").read_text(), prior_success)


# ─────────────────────────────────────────────
# Structural guards — script source, never re-tests O.5a/O.5c logic
# ─────────────────────────────────────────────
def _directives_only(content: str) -> str:
    """Strip comment lines and blank lines, leaving only what bash
    itself would actually execute. The script's own header
    extensively documents what it deliberately does NOT do (--clean,
    systemctl, the real trx_sim_staging name in the role-provisioning
    SQL example) — a raw substring scan would false-positive against
    that documentation, same lesson already learned in O.5c-2/O.5c-3's
    own test suites."""
    return "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


class ScriptStructuralGuardTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = Path(_SCRIPT_PATH).read_text()
        cls.code = _directives_only(cls.source)

    def test_no_clean_flag_anywhere_in_script(self):
        self.assertNotIn("--clean", self.code)

    def test_no_systemctl_reference_anywhere(self):
        self.assertNotIn("systemctl", self.code)

    def test_migrate_is_never_invoked_without_check_flag(self):
        # O.5d-3: the script DOES invoke manage.py now (the Django
        # schema compatibility check), but every "migrate" invocation
        # must be paired with --check — plain `manage.py migrate`
        # (which would APPLY migrations) must never appear anywhere.
        # Deliberately matches actual manage.py invocations only (not
        # unrelated prose like "...this does not look like a real,
        # migrated trx_sim database").
        migrate_lines = [
            line for line in self.code.splitlines()
            if "RESTORE_DRILL_MANAGE_PY" in line and "migrate" in line
        ]
        self.assertTrue(migrate_lines, "expected at least one migrate invocation (the O.5d-3 check)")
        for line in migrate_lines:
            self.assertIn("--check", line, f"migrate invoked without --check: {line!r}")

    def test_no_database_or_dbname_or_target_db_flag_accepted(self):
        for forbidden in ("--database", "--dbname", "--target-db", "DRILL_DB_TARGET", "RESTORE_DRILL_TARGET"):
            self.assertNotIn(forbidden, self.code)

    def test_no_hardcoded_real_database_name(self):
        self.assertNotIn("trx_sim_staging", self.code)

    def test_cleanup_trap_revalidates_name_before_dropdb(self):
        # The SAME validation function must be called again inside the
        # cleanup path — structural proof that dropdb is never reached
        # on an unvalidated name, even at cleanup time.
        final_cleanup_start = self.source.index("_final_cleanup()")
        final_cleanup_body = self.source[final_cleanup_start:final_cleanup_start + 1200]
        self.assertIn("_validate_drill_name", final_cleanup_body)
        self.assertIn("dropdb", final_cleanup_body)
        # The validate call must textually precede the dropdb call.
        self.assertLess(
            final_cleanup_body.index("_validate_drill_name"),
            final_cleanup_body.index("dropdb"),
        )

    def test_trap_installed_before_createdb(self):
        trap_index = self.source.index("trap _final_cleanup EXIT")
        createdb_index = self.source.index("createdb -h")
        self.assertLess(trap_index, createdb_index)

    def test_no_default_password(self):
        # The ONLY acceptable default for the password variable is an
        # empty string (forcing the explicit precondition check to
        # fail) — never a literal credential.
        self.assertIn('RESTORE_DRILL_DB_PASSWORD="${RESTORE_DRILL_DB_PASSWORD:-}"', self.source)


# ─────────────────────────────────────────────
# Name-collision guards — deterministic via stubbed date/od (test-only
# technique, never a capability added to the script).
# ─────────────────────────────────────────────
class NameCollisionGuardTests(TestCase):

    def test_generated_name_colliding_with_db_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            forced_name = _make_deterministic_date_od(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                db_name=forced_name,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists(), "createdb must never be called when the generated name collides with DB_NAME")
            failure = json.loads((meta_dir / "restore_drill_failure.json").read_text())
            self.assertIn("safety validation", failure["reason"])

    def test_generated_name_colliding_with_test_db_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir)
            forced_name = _make_deterministic_date_od(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                db_name="trx_sim_staging_unrelated", db_test_name=forced_name,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(call_log.exists())

    def test_normal_run_with_no_collision_still_succeeds(self):
        # Sanity check that the deterministic date/od stubbing itself
        # doesn't break the happy path when there is genuinely no
        # collision — isolates the guard tests above from a false
        # positive caused by the stubbing technique itself.
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _make_deterministic_date_od(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                db_name="trx_sim_staging_completely_different",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "restore_drill_success.json").read_text())
            self.assertEqual(data["drill_database_name"], "trx_restore_drill_20260101120000_deadbeef")


# ─────────────────────────────────────────────
# Concurrency — own flock domain
# ─────────────────────────────────────────────
@unittest.skipUnless(shutil.which("flock"), "flock binary not installed on this machine")
class LockContentionTests(TestCase):

    def test_lock_contention_exits_cleanly_without_touching_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            call_log = _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            lock_path = meta_dir / ".restore_drill.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 205>"{lock_path}"; flock -n 205 && sleep 5'],
            )
            try:
                time.sleep(1)
                result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((meta_dir / "restore_drill_success.json").exists())
                self.assertFalse((meta_dir / "restore_drill_failure.json").exists())
                self.assertFalse(call_log.exists(), "createdb must never be called during lock contention")
            finally:
                holder.wait(timeout=10)

    def test_lock_file_created_under_metadata_path(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((meta_dir / ".restore_drill.lock").exists())


# ─────────────────────────────────────────────
# Timeout
# ─────────────────────────────────────────────
class TimeoutTests(TestCase):

    def test_pg_restore_exceeding_timeout_is_killed_and_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            call_log = _make_fake_pg_tools(bin_dir, pg_restore_exec_sleep=10)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_drill_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir, timeout_seconds=2,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "restore_drill_success.json").exists())
            calls = call_log.read_text().splitlines() if call_log.exists() else []
            self.assertTrue(any(c.startswith("dropdb") for c in calls),
                             "cleanup must still drop the database after a timeout kill")


# ─────────────────────────────────────────────
# OPTIONAL — real local PostgreSQL, opt-in only
# ─────────────────────────────────────────────
@unittest.skipUnless(_REAL_PG_OPT_IN, "set RESTORE_DRILL_TEST_REAL_PG=1 to run against a real local Postgres")
class RealPostgresIntegrationTests(TestCase):
    """Requires a real local PostgreSQL server reachable via DB_HOST/
    DB_PORT with the dedicated trx_sim_drill role already provisioned
    (RESTORE_DRILL_DB_USER/RESTORE_DRILL_DB_PASSWORD env), and a real
    dump file producible via pg_dump of some source database. Never
    auto-provisions or auto-detects — must be explicitly opted into,
    since standing up real Postgres infrastructure is out of scope for
    the default `manage.py test` run."""

    def test_full_real_drill_against_real_postgres(self):
        self.skipTest(
            "Real-Postgres end-to-end wiring is an operator-provisioned "
            "manual exercise (see O.5d-1 closing report) — this class "
            "exists as the designated opt-in slot, not auto-implemented."
        )
