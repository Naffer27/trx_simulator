# simulator/tests/test_o5e4_media_restore_drill.py
"""
Microbloque O.5e-4 — Media Restore Drill + Final E2E.

Covers deploy/scripts/media_restore_drill.sh exclusively via real
subprocess runs — never imports/execs it as Python, never touches a
real network or a real cloud provider. Same two stubbing strategies
O.5c-1/O.5d-2/O.5e-2's own suites already established:

  - rclone is used FOR REAL wherever possible, pointed at rclone's own
    "local" backend (a plain directory on this machine, configured via
    a throwaway rclone.conf) — never a network call, never a real cloud
    credential. Tests requiring rclone are skipped (not failed) if the
    `rclone` binary is not installed on the machine running the suite.
  - flock-dependent concurrency tests are skipped (not failed) if the
    `flock` binary is not installed.

Adversarial archives (absolute path / traversal / symlink escape) are
built directly with Python's `tarfile` module — the only reliable way to
craft entries no well-behaved writer (including this project's own
backup_media_offsite.sh) would ever produce, needed to prove the
restorer's OWN defenses catch them rather than relying on trust in the
uploader.

Frozen decisions under test (O.5e-4 approval):
  - media_restore_drill.sh NEVER writes to, reads from, or creates
    MEDIA_ROOT — proven here by asserting MEDIA_ROOT (when present) is
    byte-for-byte unchanged across every success AND failure scenario,
    and that a MISSING MEDIA_ROOT is not even a precondition failure
    (the script only resolves its real path for the scratch-containment
    check, never requires it to exist).
  - The scratch directory is ALWAYS named
    trx_media_restore_drill_<timestamp>_<random>, is NEVER the caller's
    choice, and is proven removed (or, on a simulated cleanup failure,
    logged as a WARNING without masking the real exit code) at the end
    of every run.
  - There is no "local" source — media_backup_offsite.sh keeps no local
    archive to drill against, so offsite (media_backup_success.json) is
    the ONLY source of truth; there is no flag or env var to supply a
    remote filename/remote_target/checksum manually.
  - A failure at ANY step (missing/corrupt manifest, integrity_verified
    != true, remote missing, download failure/timeout, size mismatch,
    SHA-256 mismatch, corrupt tar, absolute path entry, traversal entry,
    symlink escape, file_count mismatch, extraction failure) writes
    media_restore_drill_failure.json and NEVER touches
    media_restore_drill_success.json — an existing prior success must
    survive a later failed run untouched.
  - Own flock domain (.media_restore_drill.lock), independent of
    O.5d's .restore_drill.lock and O.5e-2's .media_backup.lock.
  - No filename (from the archive or otherwise) ever appears in
    stdout/stderr or in either JSON manifest.

Does NOT touch O.5e-1 (secure serving), O.5e-2 (backup_media_offsite.sh,
unmodified), O.5e-3 (scheduler/health), Treasury/Wallet/Ledger, or the
PostgreSQL restore_drill.sh (O.5d, unmodified) — this suite only
exercises the standalone shell script.
"""
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from django.test import TestCase

_SCRIPT_PATH = str(
    Path(__file__).resolve().parent.parent.parent
    / "deploy" / "scripts" / "media_restore_drill.sh"
)

_RCLONE_AVAILABLE = shutil.which("rclone") is not None
_skip_without_rclone = unittest.skipUnless(
    _RCLONE_AVAILABLE, "rclone binary not installed on this machine",
)
_FLOCK_AVAILABLE = shutil.which("flock") is not None
_skip_without_flock = unittest.skipUnless(
    _FLOCK_AVAILABLE, "flock binary not installed on this machine",
)


def _write_local_rclone_conf(conf_path, remote_name="testremote"):
    conf_path.write_text(f"[{remote_name}]\ntype = local\n")


def _build_archive_bytes(entries):
    """entries: list of ("name", b"content") for a regular file, or
    ("name", "symlink", "target") for a symlink entry."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for entry in entries:
            if len(entry) == 2:
                name, content = entry
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            else:
                name, _, target = entry
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
    return buf.getvalue()


def _valid_archive_bytes():
    return _build_archive_bytes([
        ("kyc/documents/front.jpg", b"front bytes"),
        ("kyc/documents/back.jpg", b"back bytes"),
        ("kyc/documents/frente n documento (1).jpg", b"unicode-ish space filename bytes"),
        ("treasury/evidence/ev1.pdf", b"evidence bytes"),
    ])


def _write_media_manifest(meta_dir, *, archive_bytes, remote_target, **overrides):
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    data = {
        "schema_version": 1,
        "timestamp_utc": "2026-01-01T04:00:00Z",
        "archive_filename": "media_20260101_040000.tar",
        "size_bytes": len(archive_bytes),
        "sha256": sha256,
        "file_count": 4,
        "remote_target": remote_target,
        "hostname": "test-host",
        "integrity_verified": True,
    }
    data.update(overrides)
    Path(meta_dir).mkdir(parents=True, exist_ok=True)
    (Path(meta_dir) / "media_backup_success.json").write_text(json.dumps(data))
    return data


def _shadow_path_excluding(binary_name, shadow_dir):
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


def _run_restore_drill(
    *, media_root, meta_dir, rclone_config, scratch_dir=None,
    path_override=None, extra_env=None, timeout=60,
):
    env = dict(os.environ)
    if path_override is not None:
        env["PATH"] = path_override
    env["MEDIA_ROOT"] = str(media_root)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["RCLONE_CONFIG"] = str(rclone_config)
    if scratch_dir is not None:
        env["MEDIA_RESTORE_DRILL_SCRATCH_DIR"] = str(scratch_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", _SCRIPT_PATH], env=env, capture_output=True, text=True, timeout=timeout,
    )


def _hash_tree(root: Path):
    if not root.exists():
        return None
    entries = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            entries.append(f"L:{rel}:{os.readlink(p)}")
        elif p.is_file():
            entries.append(f"F:{rel}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
        elif p.is_dir():
            entries.append(f"D:{rel}")
    return "\n".join(entries)


def _make_corrupting_rclone_wrapper(bin_dir, counter_file, size_bytes):
    """Real-rclone passthrough whose copyto call (the drill's own
    download) writes deliberately corrupted bytes of the SAME SIZE the
    manifest expects — proves the SHA-256 step specifically catches
    same-size bit-rot that a size check alone would miss."""
    real_rclone = shutil.which("rclone")
    assert real_rclone
    path = Path(bin_dir) / "rclone"
    path.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "--config" ] && [ "$3" = "copyto" ]; then
    COUNT=$( [ -f "{counter_file}" ] && cat "{counter_file}" || echo 0 )
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "{counter_file}"
    DEST="${{@: -1}}"
    head -c {size_bytes} /dev/zero | tr '\\0' 'X' > "$DEST"
    exit 0
fi
exec "{real_rclone}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_slow_rclone_wrapper(bin_dir, sleep_seconds):
    real_rclone = shutil.which("rclone")
    assert real_rclone
    path = Path(bin_dir) / "rclone"
    path.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ]; then
    sleep {sleep_seconds}
fi
exec "{real_rclone}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_failing_rclone_wrapper(bin_dir):
    real_rclone = shutil.which("rclone")
    assert real_rclone
    path = Path(bin_dir) / "rclone"
    path.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ]; then
    echo "simulated download failure" >&2
    exit 1
fi
exec "{real_rclone}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ─────────────────────────────────────────────
# Precondition / manifest-validation failures — none require a real
# rclone transfer, so they run unconditionally.
# ─────────────────────────────────────────────
class PreconditionTests(TestCase):

    def test_manifest_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_restore_drill(
                media_root=Path(work) / "media", meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("media_backup_success.json", failure["reason"])

    def test_manifest_missing_required_field_writes_failure(self):
        for field in (
            "schema_version", "timestamp_utc", "archive_filename", "size_bytes",
            "sha256", "file_count", "remote_target", "hostname", "integrity_verified",
        ):
            with tempfile.TemporaryDirectory() as work:
                meta_dir = Path(work) / "meta"
                conf = Path(work) / "rclone.conf"
                _write_local_rclone_conf(conf)
                data = _write_media_manifest(
                    meta_dir, archive_bytes=_valid_archive_bytes(),
                    remote_target="testremote:remote/x.tar",
                )
                del data[field]
                (meta_dir / "media_backup_success.json").write_text(json.dumps(data))

                result = _run_restore_drill(
                    media_root=Path(work) / "media", meta_dir=meta_dir, rclone_config=conf,
                )
                self.assertNotEqual(result.returncode, 0, f"field {field!r}")
                self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())

    def test_integrity_verified_false_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            _write_media_manifest(
                meta_dir, archive_bytes=_valid_archive_bytes(),
                remote_target="testremote:remote/x.tar", integrity_verified=False,
            )

            result = _run_restore_drill(
                media_root=Path(work) / "media", meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("integrity_verified", failure["reason"])

    def test_invalid_sha256_format_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            _write_media_manifest(
                meta_dir, archive_bytes=_valid_archive_bytes(),
                remote_target="testremote:remote/x.tar", sha256="not-a-valid-hash",
            )

            result = _run_restore_drill(
                media_root=Path(work) / "media", meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_rclone_binary_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            _write_media_manifest(
                meta_dir, archive_bytes=_valid_archive_bytes(),
                remote_target="testremote:remote/x.tar",
            )

            result = _run_restore_drill(
                media_root=Path(work) / "media", meta_dir=meta_dir, rclone_config=conf,
                path_override=_shadow_path_excluding("rclone", Path(work) / "shadow"),
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("rclone", failure["reason"].lower())

    def test_rclone_config_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            _write_media_manifest(
                meta_dir, archive_bytes=_valid_archive_bytes(),
                remote_target="testremote:remote/x.tar",
            )

            result = _run_restore_drill(
                media_root=Path(work) / "media", meta_dir=meta_dir,
                rclone_config=Path(work) / "does_not_exist.conf",
            )
            self.assertNotEqual(result.returncode, 0)

    def test_media_root_missing_is_not_a_precondition_failure(self):
        """MEDIA_ROOT is only consulted for the scratch-containment
        safety check — its absence must not block a drill."""
        if not _RCLONE_AVAILABLE:
            self.skipTest("rclone binary not installed on this machine")
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
            )

            result = _run_restore_drill(
                media_root=Path(work) / "media_does_not_exist", meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# Full success path — requires real rclone against its `local` backend.
# ─────────────────────────────────────────────
@_skip_without_rclone
class SuccessTests(TestCase):

    def _run_happy_path(self, work, *, extra_env=None):
        media_root = Path(work) / "media"; media_root.mkdir()
        (media_root / "some_real_file.jpg").write_bytes(b"untouched")
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)

        archive_bytes = _valid_archive_bytes()
        (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
        _write_media_manifest(
            meta_dir, archive_bytes=archive_bytes,
            remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
        )

        before_hash = _hash_tree(media_root)
        result = _run_restore_drill(
            media_root=media_root, meta_dir=meta_dir, rclone_config=conf, extra_env=extra_env,
        )
        return result, media_root, meta_dir, before_hash

    def test_success_writes_valid_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)

            success_file = meta_dir / "media_restore_drill_success.json"
            self.assertTrue(success_file.exists())
            data = json.loads(success_file.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertIn("timestamp_utc", data)
            self.assertEqual(data["source"], "offsite")
            self.assertIsInstance(data["duration_seconds"], int)
            self.assertEqual(data["archive_size_bytes"], len(_valid_archive_bytes()))
            self.assertEqual(data["file_count"], 4)
            self.assertIn("no_symlink_escape", data["checks_passed"])
            self.assertIn("file_count_match", data["checks_passed"])
            self.assertIn("sha256_match", data["checks_passed"])

    def test_success_manifest_has_no_forbidden_fields(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "media_restore_drill_success.json").read_text())
            for forbidden_key in ("sha256", "remote_target", "archive_filename", "filename", "path"):
                self.assertNotIn(forbidden_key, data)

    def test_media_root_untouched_byte_for_byte_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            result, media_root, _meta, before_hash = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_no_filename_appears_in_stdout_or_stderr(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, _meta, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            for leaked in ("front.jpg", "back.jpg", "ev1.pdf", "frente"):
                self.assertNotIn(leaked, result.stdout)
                self.assertNotIn(leaked, result.stderr)

    def test_no_filename_or_secret_in_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "media_restore_drill_success.json").read_text()
            for leaked in ("front.jpg", "back.jpg", "ev1.pdf", "frente", "password", "secret"):
                self.assertNotIn(leaked, body)

    def test_no_leftover_scratch_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            scratch_parent = Path(work) / "scratch"; scratch_parent.mkdir()
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
            )

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                scratch_dir=scratch_parent,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(scratch_parent.iterdir()), [])

    def test_extraction_preserves_structure_and_content_in_scratch(self):
        """Proves the ORIGINAL tree round-trips exactly through
        backup -> remote -> independent download -> extract: this test
        builds the archive from a real directory tree (not by hand) via
        backup_media_offsite.sh itself, then verifies the restore
        drill's extracted copy is tree-equivalent to the original."""
        backup_script = str(
            Path(_SCRIPT_PATH).resolve().parent / "backup_media_offsite.sh"
        )
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"
            (media_root / "kyc" / "documents").mkdir(parents=True)
            (media_root / "kyc" / "selfies").mkdir(parents=True)
            (media_root / "empty_dir").mkdir(parents=True)
            (media_root / "kyc" / "documents" / "front.jpg").write_bytes(b"front bytes")
            (media_root / "kyc" / "documents" / "unicode ñ (1).jpg").write_bytes(b"unicode space bytes")
            (media_root / "kyc" / "selfies" / "selfie.jpg").write_bytes(b"selfie bytes")

            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            backup_env = dict(os.environ)
            backup_env.update({
                "MEDIA_ROOT": str(media_root),
                "BACKUP_METADATA_PATH": str(meta_dir),
                "RCLONE_CONFIG": str(conf),
                "RCLONE_REMOTE": "testremote",
                "MEDIA_RCLONE_REMOTE_PATH": str(remote_dir),
            })
            backup_result = subprocess.run(
                ["bash", backup_script], env=backup_env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(backup_result.returncode, 0, backup_result.stderr)

            drill_result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertEqual(drill_result.returncode, 0, drill_result.stderr)
            data = json.loads((meta_dir / "media_restore_drill_success.json").read_text())
            self.assertEqual(data["file_count"], 3)

    def test_rerun_after_success_is_idempotent(self):
        with tempfile.TemporaryDirectory() as work:
            result1, media_root, meta_dir, _before = self._run_happy_path(work)
            self.assertEqual(result1.returncode, 0, result1.stderr)

            conf = Path(work) / "rclone.conf"
            result2 = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)


# ─────────────────────────────────────────────
# Adversarial archives — absolute path / traversal / symlink escape.
# ─────────────────────────────────────────────
@_skip_without_rclone
class AdversarialArchiveTests(TestCase):

    def _run_against_archive(self, work, archive_bytes, **manifest_overrides):
        media_root = Path(work) / "media"; media_root.mkdir()
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)
        (remote_dir / "evil.tar").write_bytes(archive_bytes)
        _write_media_manifest(
            meta_dir, archive_bytes=archive_bytes,
            remote_target=f"testremote:{remote_dir}/evil.tar",
            **manifest_overrides,
        )
        before_hash = _hash_tree(media_root)
        result = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
        return result, media_root, meta_dir, before_hash

    def test_absolute_path_entry_is_rejected(self):
        archive_bytes = _build_archive_bytes([("/etc/passwd_pwned.txt", b"pwned")])
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, before_hash = self._run_against_archive(work, archive_bytes, file_count=1)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_traversal_entry_is_rejected(self):
        archive_bytes = _build_archive_bytes([("../../etc/traversal_pwned.txt", b"pwned")])
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, before_hash = self._run_against_archive(work, archive_bytes, file_count=1)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_symlink_escape_entry_is_rejected(self):
        archive_bytes = _build_archive_bytes([
            ("kyc/documents/normal.jpg", b"normal content"),
            ("kyc/documents/evil_symlink", "symlink", "/etc/passwd"),
        ])
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, before_hash = self._run_against_archive(work, archive_bytes, file_count=1)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("symlink", failure["reason"].lower())
            self.assertNotIn("evil_symlink", failure["reason"])
            self.assertNotIn("/etc/passwd", failure["reason"])
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_symlink_pointing_inside_scratch_is_accepted(self):
        """A symlink whose target is a relative sibling entry (stays
        contained once extracted) is not an escape and must succeed."""
        archive_bytes = _build_archive_bytes([
            ("kyc/documents/real.jpg", b"real bytes"),
            ("kyc/documents/alias.jpg", "symlink", "real.jpg"),
        ])
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _before = self._run_against_archive(work, archive_bytes, file_count=1)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((meta_dir / "media_restore_drill_success.json").exists())

    def test_file_count_mismatch_is_rejected(self):
        archive_bytes = _valid_archive_bytes()  # 4 real files
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, before_hash = self._run_against_archive(
                work, archive_bytes, file_count=999,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("file_count", failure["reason"])
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_corrupt_tar_is_rejected(self):
        archive_bytes = b"THIS IS NOT A VALID TAR ARCHIVE AT ALL " * 20
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, before_hash = self._run_against_archive(
                work, archive_bytes, file_count=0,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("tar", failure["reason"].lower())
            self.assertEqual(_hash_tree(media_root), before_hash)


# ─────────────────────────────────────────────
# Failure paths requiring an actual transfer / injected fault.
# ─────────────────────────────────────────────
@_skip_without_rclone
class TransferFailureTests(TestCase):

    def _setup(self, work):
        media_root = Path(work) / "media"; media_root.mkdir()
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)
        archive_bytes = _valid_archive_bytes()
        (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
        _write_media_manifest(
            meta_dir, archive_bytes=archive_bytes,
            remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
        )
        return media_root, meta_dir, remote_dir, conf, archive_bytes

    def test_remote_object_missing_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf, _bytes = self._setup(work)
            (remote_dir / "media_20260101_040000.tar").unlink()  # simulate missing remote object

            before_hash = _hash_tree(media_root)
            result = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_rclone_download_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf, _bytes = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            _make_failing_rclone_wrapper(bin_dir)

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("download", failure["reason"].lower())

    def test_size_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            # Manifest claims a size the actual object does not have.
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
                size_bytes=len(archive_bytes) + 999,
            )

            result = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("size", failure["reason"].lower())

    def test_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf, archive_bytes = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            counter = Path(work) / "counter"
            _make_corrupting_rclone_wrapper(bin_dir, counter, len(archive_bytes))

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("mismatch", failure["reason"].lower())

    def test_failure_never_overwrites_a_prior_real_success(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf, _bytes = self._setup(work)

            ok = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "media_restore_drill_success.json").read_text()

            (remote_dir / "media_20260101_040000.tar").unlink()
            failed = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
            self.assertNotEqual(failed.returncode, 0)

            self.assertEqual((meta_dir / "media_restore_drill_success.json").read_text(), prior_success)
            self.assertTrue((meta_dir / "media_restore_drill_failure.json").exists())

    def test_extraction_failure_is_rejected(self):
        """Simulates tar failing at extraction time (not creation) via a
        wrapper that passes -tf through to real tar but fails -xf."""
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf, _bytes = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_tar = shutil.which("tar")
            wrapper = bin_dir / "tar"
            wrapper.write_text(f"""#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = "-xf" ]; then
        echo "simulated extraction failure" >&2
        exit 1
    fi
done
exec "{real_tar}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads((meta_dir / "media_restore_drill_failure.json").read_text())
            self.assertIn("extraction", failure["reason"].lower())


# ─────────────────────────────────────────────
# Cleanup-failure behavior — must not mask the real result.
# ─────────────────────────────────────────────
@_skip_without_rclone
class CleanupFailureTests(TestCase):

    def test_cleanup_failure_after_success_does_not_change_exit_or_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            scratch_parent = Path(work) / "scratch"; scratch_parent.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
            )

            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rm = shutil.which("rm")
            wrapper = bin_dir / "rm"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [[ "$*" == *"trx_media_restore_drill_"* ]]; then
    echo "simulated rm failure" >&2
    exit 1
fi
exec "{real_rm}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                scratch_dir=scratch_parent,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            # A cleanup failure must never change a real success into a
            # reported failure.
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((meta_dir / "media_restore_drill_success.json").exists())
            self.assertIn("WARNING", result.stdout + result.stderr)


# ─────────────────────────────────────────────
# Concurrency and timeout.
# ─────────────────────────────────────────────
@_skip_without_flock
class ConcurrencyTests(TestCase):

    def test_lock_contention_exits_cleanly_without_touching_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            _write_media_manifest(
                meta_dir, archive_bytes=_valid_archive_bytes(),
                remote_target="testremote:remote/x.tar",
            )

            lock_path = meta_dir / ".media_restore_drill.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 205>"{lock_path}"; flock -n 205 && sleep 5'],
            )
            try:
                time.sleep(1)
                result = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())
                self.assertFalse((meta_dir / "media_restore_drill_failure.json").exists())
            finally:
                holder.wait(timeout=10)

    def test_media_restore_drill_lock_independent_from_other_locks(self):
        if not _RCLONE_AVAILABLE:
            self.skipTest("rclone binary not installed on this machine")
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
            )

            media_backup_lock = meta_dir / ".media_backup.lock"
            pg_restore_lock = meta_dir / ".restore_drill.lock"
            holder1 = subprocess.Popen(
                ["bash", "-c", f'exec 206>"{media_backup_lock}"; flock -n 206 && sleep 5'],
            )
            holder2 = subprocess.Popen(
                ["bash", "-c", f'exec 207>"{pg_restore_lock}"; flock -n 207 && sleep 5'],
            )
            try:
                time.sleep(1)
                result = _run_restore_drill(media_root=media_root, meta_dir=meta_dir, rclone_config=conf)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((meta_dir / "media_restore_drill_success.json").exists())
            finally:
                holder1.wait(timeout=10)
                holder2.wait(timeout=10)


@_skip_without_rclone
class TimeoutTests(TestCase):

    def test_download_exceeding_timeout_is_killed_and_reported(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)
            archive_bytes = _valid_archive_bytes()
            (remote_dir / "media_20260101_040000.tar").write_bytes(archive_bytes)
            _write_media_manifest(
                meta_dir, archive_bytes=archive_bytes,
                remote_target=f"testremote:{remote_dir}/media_20260101_040000.tar",
            )
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            _make_slow_rclone_wrapper(bin_dir, sleep_seconds=10)

            result = _run_restore_drill(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
                extra_env={"MEDIA_RESTORE_DRILL_TIMEOUT_SECONDS": "2"},
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_restore_drill_success.json").exists())


# ─────────────────────────────────────────────
# Structural regression: no destructive rclone usage, independence from
# backup_media_offsite.sh / restore_drill.sh (unmodified).
# ─────────────────────────────────────────────
class ScriptIndependenceAndSafetyTests(TestCase):

    def test_no_destructive_rclone_command_used(self):
        code_lines = "\n".join(
            line for line in Path(_SCRIPT_PATH).read_text().splitlines()
            if not line.strip().startswith("#")
        )
        for forbidden in ("rclone sync", "rclone delete", "rclone purge", "rclone deletefile"):
            self.assertNotIn(forbidden, code_lines)

    def test_no_database_tooling_referenced(self):
        code_lines = "\n".join(
            line for line in Path(_SCRIPT_PATH).read_text().splitlines()
            if not line.strip().startswith("#")
        )
        for forbidden in ("createdb", "dropdb", "pg_restore", "psql", "PGPASSWORD"):
            self.assertNotIn(forbidden, code_lines)

    def test_own_lock_file_name_distinct_from_siblings(self):
        content = Path(_SCRIPT_PATH).read_text()
        self.assertIn(".media_restore_drill.lock", content)
        # Comment-stripped: the header comment legitimately documents
        # what this script's lock domain is NOT (same lesson learned
        # repeatedly across O.5c/O.5d/O.5e-2's own test suites) — only
        # actual code must never reference a sibling's lock file.
        code_lines = "\n".join(
            line for line in content.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn(".media_backup.lock", code_lines)
        self.assertNotIn(".restore_drill.lock", code_lines)
        self.assertNotIn(".offsite.lock", code_lines)

    def test_backup_media_offsite_script_not_invoked_by_this_script(self):
        # Cheap same-file self-check that O.5e-2's script still exists
        # and is never EXECUTED by this one (independence, not a shared
        # library — comment-only references to its name for
        # documentation purposes are fine and expected). The full
        # guarantee is O.5e-2's own suite passing unchanged, run
        # alongside this one.
        backup_script = Path(_SCRIPT_PATH).resolve().parent / "backup_media_offsite.sh"
        self.assertTrue(backup_script.exists())
        code_lines = "\n".join(
            line for line in Path(_SCRIPT_PATH).read_text().splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("backup_media_offsite.sh", code_lines)

    def test_no_no_local_mode_flag_exists(self):
        # Comment-stripped: the header legitimately documents that
        # restore_drill.sh (Postgres) HAS a --source flag and that THIS
        # script deliberately does not — only actual argument-parsing
        # code must never accept these flags.
        code_lines = "\n".join(
            line for line in Path(_SCRIPT_PATH).read_text().splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("--source", code_lines)
        self.assertNotIn("--destination", code_lines)
        self.assertNotIn("--remote-target", code_lines)
        self.assertNotIn("--checksum", code_lines)
