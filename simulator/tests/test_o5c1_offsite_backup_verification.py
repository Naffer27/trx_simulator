# simulator/tests/test_o5c1_offsite_backup_verification.py
"""
Microbloque O.5c-1 — Offsite Backup Script + Strong Verification.

Covers deploy/scripts/backup_offsite.sh exclusively via real subprocess
runs — never imports/execs it as Python, never touches a real network
or a real cloud provider. Two different stubbing strategies are used,
matching the two things this script depends on:

  - pg_restore is ALWAYS a fake executable on a temporary PATH (mirrors
    O.5a/O.5b's own pattern) — this suite never requires a real
    PostgreSQL install.
  - rclone is used FOR REAL wherever possible, pointed at rclone's own
    "local" backend (a plain directory on this machine, configured via
    a throwaway rclone.conf) — never a network call, never a real cloud
    credential, but real rclone command semantics (copyto/lsf exit
    codes, output format) instead of a hand-written guess at them.
    Tests requiring rclone are skipped (not failed) if the `rclone`
    binary is not installed on the machine running the suite — rclone
    is a system binary (O.5c Fase 0 approval, decision 2), not a
    requirements.txt dependency, so it is not guaranteed present in
    every environment `manage.py test` might run in.

Frozen decisions under test (O.5c Fase 0 + O.5c-1 approval):
  - backup_offsite.sh NEVER reads-for-writing, modifies, or deletes
    backup_success.json / backup_failure.json (O.5a) — under success,
    under failure, and under lock contention.
  - backup_offsite.sh NEVER modifies or deletes anything under
    BACKUP_DIR — the local dump is read-only input.
  - offsite_success.json is written ONLY after: local SHA-256 computed,
    upload succeeded, remote existence confirmed, download to an
    INDEPENDENT scratch file succeeded, SHA-256 of the recovered bytes
    matches the local SHA-256 EXACTLY, and pg_restore --list succeeds
    against the RECOVERED scratch copy (not the local one).
  - Any failure at any step writes offsite_failure.json and NEVER
    touches offsite_success.json (an existing prior success must
    survive a later failed run untouched).
  - Own flock domain (.offsite.lock), independent of O.5b's
    .backup.lock — lock contention exits 0 without touching any
    metadata (not a failure, someone else is already offsiting).

Does NOT touch GET /api/health/detail/ (that integration is O.5c-3),
Treasury, Wallet/Ledger, or any O.4a-O.4e/O.5a/O.5b code — this suite
only exercises the standalone shell script.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import TestCase

_SCRIPT_PATH = str(
    Path(__file__).resolve().parent.parent.parent
    / "deploy" / "scripts" / "backup_offsite.sh"
)

_RCLONE_AVAILABLE = shutil.which("rclone") is not None
_skip_without_rclone = unittest.skipUnless(
    _RCLONE_AVAILABLE, "rclone binary not installed on this machine"
)


def _shadow_path_excluding(binary_name, shadow_dir):
    """Build a PATH (a single directory, `shadow_dir`, populated with
    symlinks) containing every executable reachable via the real PATH
    EXCEPT `binary_name` — used to genuinely exercise backup_offsite.sh's
    "binary not found" precondition checks without relying on the test
    machine happening to lack that binary, and WITHOUT accidentally
    removing sibling tools that happen to live in the same directory
    (e.g. flock and rclone are both under /opt/homebrew/bin on a
    Homebrew-managed Mac — excluding that whole directory would also
    hide flock, which is not what any single one of these tests wants
    to assert about)."""
    shadow_dir = Path(shadow_dir)
    shadow_dir.mkdir(parents=True, exist_ok=True)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        dpath = Path(d)
        if not dpath.is_dir():
            continue
        try:
            entries = list(dpath.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.name == binary_name:
                continue
            link = shadow_dir / entry.name
            if not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    return str(shadow_dir)


def _make_fake_pg_restore(bin_dir, *, ok=True):
    path = Path(bin_dir) / "pg_restore"
    if ok:
        path.write_text("#!/usr/bin/env bash\nexit 0\n")
    else:
        path.write_text("#!/usr/bin/env bash\necho 'fake pg_restore failure' >&2\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_corrupting_rclone_wrapper(bin_dir, counter_file):
    """A real-rclone passthrough wrapper whose SECOND `copyto` invocation
    (the verification download) writes deliberately corrupted bytes
    instead of what actually landed on the remote — simulates
    undetectable-by-existence-check remote bit-rot, to prove the
    checksum step actually catches it."""
    real_rclone = shutil.which("rclone")
    assert real_rclone, "rclone must be installed to build the corrupting wrapper"
    path = Path(bin_dir) / "rclone"
    path.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "--config" ] && [ "$3" = "copyto" ]; then
    COUNT=$( [ -f "{counter_file}" ] && cat "{counter_file}" || echo 0 )
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "{counter_file}"
    if [ "$COUNT" = "2" ]; then
        DEST="${{@: -1}}"
        echo -n "CORRUPTED BYTES — DOES NOT MATCH LOCAL SHA256" > "$DEST"
        exit 0
    fi
fi
exec "{real_rclone}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_local_rclone_conf(conf_path, remote_name="testremote"):
    conf_path.write_text(f"[{remote_name}]\ntype = local\n")


def _write_success_manifest(
    meta_dir, backup_dir, *,
    filename="trx_sim_test_20260101_030000.dump",
    database_name="trx_sim_test",
    content=b"fake dump bytes for O.5c-1 tests",
    size_override=None,
    integrity_verified=True,
    omit_integrity_verified=False,
):
    backup_dir.mkdir(parents=True, exist_ok=True)
    dump_path = backup_dir / filename
    dump_path.write_bytes(content)
    size_bytes = size_override if size_override is not None else len(content)

    manifest = {
        "schema_version": 1,
        "timestamp_utc": "2026-01-01T03:00:00Z",
        "database_name": database_name,
        "filename": filename,
        "size_bytes": size_bytes,
        "hostname": "test-host",
    }
    if not omit_integrity_verified:
        manifest["integrity_verified"] = integrity_verified

    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "backup_success.json").write_text(json.dumps(manifest))
    return dump_path, manifest


def _run_offsite_script(
    *, bin_dir, backup_dir, meta_dir, rclone_config, rclone_remote,
    rclone_remote_path, path_override=None, extra_env=None,
):
    env = dict(os.environ)
    env["PATH"] = path_override if path_override is not None else (
        f"{bin_dir}:{env.get('PATH', '')}"
    )
    env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["RCLONE_CONFIG"] = str(rclone_config)
    env["RCLONE_REMOTE"] = rclone_remote
    env["RCLONE_REMOTE_PATH"] = str(rclone_remote_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", _SCRIPT_PATH], env=env, capture_output=True, text=True, timeout=30,
    )


# ─────────────────────────────────────────────
# Precondition / input-validation failures — none of these require a
# real rclone transfer to occur, so they run unconditionally.
# ─────────────────────────────────────────────
class OffsiteScriptPreconditionTests(TestCase):

    def test_rclone_binary_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
                path_override=f"{bin_dir}:{_shadow_path_excluding('rclone', Path(work) / 'shadow')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("rclone", failure["reason"].lower())

    def test_pg_restore_binary_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()  # no pg_restore placed here
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
                path_override=f"{bin_dir}:{_shadow_path_excluding('pg_restore', Path(work) / 'shadow')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("pg_restore", failure["reason"].lower())

    def test_rclone_remote_env_unset_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("RCLONE_REMOTE", failure["reason"])

    def test_rclone_config_file_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=Path(work) / "does_not_exist.conf",
                rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("RCLONE_CONFIG", failure["reason"])

    def test_missing_backup_success_manifest_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"; backup_dir.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir()
            _make_fake_pg_restore(bin_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIsNone(failure["filename"])

    def test_integrity_not_verified_true_refuses_to_offsite(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir, integrity_verified=False)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("integrity_verified", failure["reason"])

    def test_local_dump_file_missing_writes_failure(self):
        """backup_success.json references a filename, but the dump was
        already pruned locally (KEEP_BACKUPS rotation) before offsite ran."""
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            dump_path, _ = _write_success_manifest(meta_dir, backup_dir)
            dump_path.unlink()  # simulate pruning
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("not found", failure["reason"].lower())

    def test_local_dump_size_mismatch_writes_failure(self):
        """Local dump's current size no longer matches backup_success.json's
        recorded size_bytes — treated as tampering/truncation, refused."""
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            dump_path, _ = _write_success_manifest(meta_dir, backup_dir)
            dump_path.write_bytes(b"this content has a different length than recorded")
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("size", failure["reason"].lower())

    def test_none_of_the_precondition_failures_touch_backup_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_restore(bin_dir)
            _, manifest = _write_success_manifest(meta_dir, backup_dir)
            before = (meta_dir / "backup_success.json").read_text()

            _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=Path(work) / "does_not_exist.conf",
                rclone_remote="testremote",
                rclone_remote_path=Path(work) / "remote",
            )

            after = (meta_dir / "backup_success.json").read_text()
            self.assertEqual(before, after)
            self.assertFalse((meta_dir / "backup_failure.json").exists())


# ─────────────────────────────────────────────
# Full success path — requires real rclone against its `local` backend.
# ─────────────────────────────────────────────
@_skip_without_rclone
class OffsiteScriptSuccessTests(TestCase):

    def _run_happy_path(self, work, *, extra_env=None):
        bin_dir = Path(work) / "bin"; bin_dir.mkdir()
        backup_dir = Path(work) / "backups"
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        _make_fake_pg_restore(bin_dir)
        dump_path, manifest = _write_success_manifest(meta_dir, backup_dir)
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)

        result = _run_offsite_script(
            bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
            rclone_config=conf, rclone_remote="testremote",
            rclone_remote_path=remote_dir, extra_env=extra_env,
        )
        return result, bin_dir, backup_dir, meta_dir, remote_dir, dump_path, manifest

    def test_success_writes_valid_offsite_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, dump_path, manifest = self._run_happy_path(work)

            self.assertEqual(result.returncode, 0, result.stderr)
            success_file = meta_dir / "offsite_success.json"
            self.assertTrue(success_file.exists())
            data = json.loads(success_file.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["database_name"], manifest["database_name"])
            self.assertEqual(data["filename"], manifest["filename"])
            self.assertEqual(data["size_bytes"], manifest["size_bytes"])
            self.assertTrue(data["restorability_verified"])
            self.assertIn("testremote", data["remote_target"])
            self.assertIn("hostname", data)
            self.assertIn("timestamp_utc", data)
            self.assertEqual(len(data["sha256"]), 64)
            int(data["sha256"], 16)  # must be valid hex

    def test_success_sha256_matches_local_dump_sha256(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, dump_path, _manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)

            expected = hashlib.sha256(dump_path.read_bytes()).hexdigest()
            data = json.loads((meta_dir / "offsite_success.json").read_text())
            self.assertEqual(data["sha256"], expected)

    def test_success_file_permissions_are_640(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, _dump, _manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            mode = (meta_dir / "offsite_success.json").stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o640)

    def test_success_metadata_contains_no_secrets(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, _dump, _manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "offsite_success.json").read_text().lower()
            for forbidden in ("access_key", "secret_access_key", "password", "endpoint"):
                self.assertNotIn(forbidden, body)

    def test_no_leftover_temp_files_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            scratch_dir = Path(work) / "scratch"; scratch_dir.mkdir()
            result, *_ , meta_dir, _remote, _dump, _manifest = self._run_happy_path(
                work, extra_env={"OFFSITE_SCRATCH_DIR": str(scratch_dir)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(meta_dir.glob(".metadata.*")), [])
            self.assertEqual(list(scratch_dir.iterdir()), [])

    def test_success_never_touches_backup_success_or_failure_json(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            before = (meta_dir / "backup_success.json").read_text()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            after = (meta_dir / "backup_success.json").read_text()
            self.assertEqual(before, after)
            self.assertFalse((meta_dir / "backup_failure.json").exists())

    def test_local_dump_file_untouched_after_run(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, dump_path, _manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(dump_path.exists())
            self.assertEqual(
                dump_path.read_bytes(), b"fake dump bytes for O.5c-1 tests",
            )

    def test_no_extra_files_left_under_backup_dir(self):
        with tempfile.TemporaryDirectory() as work:
            result, *_ , meta_dir, _remote, dump_path, _manifest = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            backup_dir = dump_path.parent
            self.assertEqual(sorted(p.name for p in backup_dir.iterdir()), [dump_path.name])

    def test_rerun_after_success_is_idempotent(self):
        with tempfile.TemporaryDirectory() as work:
            result1, bin_dir, backup_dir, meta_dir, remote_dir, dump_path, manifest = \
                self._run_happy_path(work)
            self.assertEqual(result1.returncode, 0, result1.stderr)
            first_success = json.loads((meta_dir / "offsite_success.json").read_text())

            result2 = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=Path(work) / "rclone.conf", rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            second_success = json.loads((meta_dir / "offsite_success.json").read_text())

            self.assertEqual(first_success["sha256"], second_success["sha256"])
            self.assertEqual(list(meta_dir.glob(".metadata.*")), [])


# ─────────────────────────────────────────────
# Failure paths that require an actual (real, local-backend) transfer
# to occur before failing — e.g. a corrupted remote copy.
# ─────────────────────────────────────────────
@_skip_without_rclone
class OffsiteScriptTransferFailureTests(TestCase):

    def test_pg_restore_failure_on_recovered_copy_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir, ok=False)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("pg_restore", failure["reason"].lower())

    def test_checksum_mismatch_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir)
            counter_file = Path(work) / "copyto_counter"
            _make_corrupting_rclone_wrapper(bin_dir, counter_file)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "offsite_success.json").exists())
            failure = json.loads((meta_dir / "offsite_failure.json").read_text())
            self.assertIn("mismatch", failure["reason"].lower())

    def test_failure_never_overwrites_a_prior_real_success(self):
        """A failed later run must never erase evidence of an earlier
        real offsite success — mirrors O.5a's own guarantee for
        backup_success.json."""
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            ok = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "offsite_success.json").read_text()

            # Now force a failure on a subsequent run (pg_restore now fails).
            _make_fake_pg_restore(bin_dir, ok=False)
            failed = _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )
            self.assertNotEqual(failed.returncode, 0)

            self.assertEqual((meta_dir / "offsite_success.json").read_text(), prior_success)
            self.assertTrue((meta_dir / "offsite_failure.json").exists())

    def test_failure_never_deletes_local_dump(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir, ok=False)
            dump_path, _ = _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            _run_offsite_script(
                bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                rclone_config=conf, rclone_remote="testremote",
                rclone_remote_path=remote_dir,
            )

            self.assertTrue(dump_path.exists())
            self.assertEqual(dump_path.read_bytes(), b"fake dump bytes for O.5c-1 tests")


# ─────────────────────────────────────────────
# Concurrency — own flock domain, independent of O.5b's .backup.lock.
# ─────────────────────────────────────────────
@unittest.skipUnless(shutil.which("flock"), "flock binary not installed on this machine")
class OffsiteScriptConcurrencyTests(TestCase):

    def test_lock_contention_exits_cleanly_without_touching_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            lock_path = meta_dir / ".offsite.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 205>"{lock_path}"; flock -n 205 && sleep 5'],
            )
            try:
                import time
                time.sleep(1)  # let the holder actually acquire the lock

                result = _run_offsite_script(
                    bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                    rclone_config=conf, rclone_remote="testremote",
                    rclone_remote_path=Path(work) / "remote",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((meta_dir / "offsite_success.json").exists())
                self.assertFalse((meta_dir / "offsite_failure.json").exists())
            finally:
                holder.wait(timeout=10)

    def test_offsite_lock_independent_from_backup_lock(self):
        """Holding O.5b's .backup.lock must NOT block backup_offsite.sh —
        separate concurrency domains, per O.5c Fase 0 approved design."""
        if not _RCLONE_AVAILABLE:
            self.skipTest("rclone binary not installed on this machine")
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            _make_fake_pg_restore(bin_dir)
            _write_success_manifest(meta_dir, backup_dir)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            backup_lock_path = meta_dir / ".backup.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 206>"{backup_lock_path}"; flock -n 206 && sleep 5'],
            )
            try:
                import time
                time.sleep(1)

                result = _run_offsite_script(
                    bin_dir=bin_dir, backup_dir=backup_dir, meta_dir=meta_dir,
                    rclone_config=conf, rclone_remote="testremote",
                    rclone_remote_path=remote_dir,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((meta_dir / "offsite_success.json").exists())
            finally:
                holder.wait(timeout=10)
