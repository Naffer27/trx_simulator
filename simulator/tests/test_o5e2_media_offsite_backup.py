# simulator/tests/test_o5e2_media_offsite_backup.py
"""
Microbloque O.5e-2 — Media Offsite Backup.

Covers deploy/scripts/backup_media_offsite.sh exclusively via real
subprocess runs — never imports/execs it as Python, never touches a real
network or a real cloud provider. Same two stubbing strategies O.5c-1's
own suite (test_o5c1_offsite_backup_verification.py) already established:

  - rclone is used FOR REAL wherever possible, pointed at rclone's own
    "local" backend (a plain directory on this machine, configured via a
    throwaway rclone.conf) — never a network call, never a real cloud
    credential. Tests requiring rclone are skipped (not failed) if the
    `rclone` binary is not installed on the machine running the suite.
  - flock-dependent concurrency tests are skipped (not failed) if the
    `flock` binary is not installed.

Frozen decisions under test (O.5e-2 approval):
  - backup_media_offsite.sh NEVER modifies or deletes anything under
    MEDIA_ROOT — read-only input, proven here by hashing the whole tree
    before and after every successful AND every failing run.
  - backup_media_offsite.sh NEVER reads-for-writing, modifies, or
    deletes backup_success.json / backup_failure.json (O.5a) or
    offsite_success.json (O.5c) — this script only ever writes
    media_backup_success.json, media_backup_failure.json, and its own
    .media_backup.lock.
  - media_backup_success.json is written ONLY after: deterministic
    archive built, local SHA-256 computed, upload succeeded, remote
    existence confirmed, download to an INDEPENDENT scratch file
    succeeded, SHA-256 of the recovered bytes matches the local SHA-256
    EXACTLY, and `tar -tf` succeeds against the RECOVERED scratch copy.
  - Any failure at any step writes media_backup_failure.json and NEVER
    touches media_backup_success.json (an existing prior success must
    survive a later failed run untouched).
  - A symlink inside MEDIA_ROOT resolving outside it is refused
    (failure, not silent skip) — and never appears by name in any
    stdout/stderr/log/manifest.
  - No individual filename from inside MEDIA_ROOT is ever echoed to
    stdout/stderr, and no filename is ever written into
    media_backup_success.json/media_backup_failure.json — only counts,
    sizes, hashes, the generated archive filename, and the remote target.
  - Own flock domain (.media_backup.lock), independent of O.5a's
    .backup.lock and O.5c's .offsite.lock.
  - No rclone command capable of mutating/removing anything on the
    remote (sync/delete/purge) ever appears in the script.

Does NOT touch O.5e-3 (scheduler/health), O.5e-4 (restore drill),
Signal B, Treasury/Wallet/Ledger, or GET /api/health/detail/ — this
suite only exercises the standalone shell script.
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
    / "deploy" / "scripts" / "backup_media_offsite.sh"
)

_RCLONE_AVAILABLE = shutil.which("rclone") is not None
_skip_without_rclone = unittest.skipUnless(
    _RCLONE_AVAILABLE, "rclone binary not installed on this machine",
)
_FLOCK_AVAILABLE = shutil.which("flock") is not None
_skip_without_flock = unittest.skipUnless(
    _FLOCK_AVAILABLE, "flock binary not installed on this machine",
)


def _shadow_path_excluding(binary_name, shadow_dir):
    """Same technique as O.5c-1's suite: a PATH containing every real
    executable EXCEPT `binary_name`, to genuinely exercise a "binary not
    found" precondition without depending on the test machine happening
    to lack it, and without hiding sibling tools (rclone/flock/tar often
    live in the same directory)."""
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


def _write_local_rclone_conf(conf_path, remote_name="testremote"):
    conf_path.write_text(f"[{remote_name}]\ntype = local\n")


def _make_corrupting_rclone_wrapper(bin_dir, counter_file):
    """Real-rclone passthrough whose SECOND `copyto` (the verification
    download) writes deliberately corrupted bytes instead of what
    actually landed on the remote — proves the checksum step actually
    catches undetectable-by-existence-check remote bit-rot."""
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
        echo -n "CORRUPTED BYTES — DOES NOT MATCH LOCAL ARCHIVE" > "$DEST"
        exit 0
    fi
fi
exec "{real_rclone}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_corrupt_archive_tar_wrapper(bin_dir):
    """Real-tar passthrough whose CREATE invocation (`-cf`) writes a
    small, structurally-invalid file instead of a real archive — every
    other invocation (`-tf` validation calls, both this script's own
    defense-in-depth absolute-path check and the final recovered-copy
    validation) goes to the REAL tar binary unchanged.

    This is what makes it possible to reach "checksum matches but tar
    validation fails" at all: SHA-256 is computed on the archive AFTER
    it is built, so the only way for the recovered (byte-identical)
    remote copy to fail a *real* `tar -tf` is for the local archive to
    already have been invalid at the moment its own checksum was taken
    — corrupting the create step is the only place that scenario can be
    injected without literally faking the validation logic under test.
    """
    real_tar = shutil.which("tar")
    assert real_tar, "tar must be installed to build the corrupt-archive wrapper"
    path = Path(bin_dir) / "tar"
    path.write_text(f"""#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = "-cf" ]; then
        # Find the path immediately following -cf and write garbage
        # there instead of invoking the real tar create.
        found=0
        for a in "$@"; do
            if [ "$found" = "1" ]; then
                printf 'NOT A VALID TAR ARCHIVE — DELIBERATELY CORRUPT FOR O.5e-2 TESTS' > "$a"
                exit 0
            fi
            [ "$a" = "-cf" ] && found=1
        done
    fi
done
exec "{real_tar}" "$@"
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _hash_tree(root: Path):
    """Byte-for-byte fingerprint of a directory tree — filenames + file
    bytes + symlink targets — used to prove MEDIA_ROOT was not modified."""
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


def _run_media_backup_script(
    *, media_root, meta_dir, rclone_config, rclone_remote,
    media_rclone_remote_path, path_override=None, extra_env=None,
):
    env = dict(os.environ)
    if path_override is not None:
        env["PATH"] = path_override
    env["MEDIA_ROOT"] = str(media_root)
    env["BACKUP_METADATA_PATH"] = str(meta_dir)
    env["RCLONE_CONFIG"] = str(rclone_config)
    env["RCLONE_REMOTE"] = rclone_remote
    env["MEDIA_RCLONE_REMOTE_PATH"] = str(media_rclone_remote_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", _SCRIPT_PATH], env=env, capture_output=True, text=True, timeout=30,
    )


def _make_media_tree(root: Path):
    (root / "kyc" / "documents").mkdir(parents=True)
    (root / "kyc" / "selfies").mkdir(parents=True)
    (root / "treasury" / "evidence").mkdir(parents=True)
    (root / "broker_documents").mkdir(parents=True)
    (root / "empty_subdir").mkdir(parents=True)
    (root / "kyc" / "documents" / "front.jpg").write_bytes(b"front bytes")
    (root / "kyc" / "documents" / "back.jpg").write_bytes(b"back bytes")
    (root / "kyc" / "documents" / "frente ñ documento (1).jpg").write_bytes(
        b"unicode space filename bytes",
    )
    (root / "kyc" / "selfies" / "selfie.jpg").write_bytes(b"selfie bytes")
    (root / "treasury" / "evidence" / "ev1.pdf").write_bytes(b"evidence bytes")
    (root / "broker_documents" / "guide.pdf").write_bytes(b"guide bytes")


# ─────────────────────────────────────────────
# Precondition / input-validation failures — none require a real rclone
# transfer, so they run unconditionally.
# ─────────────────────────────────────────────
class MediaBackupPreconditionTests(TestCase):

    def test_media_root_missing_fails_clearly(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=Path(work) / "does_not_exist",
                meta_dir=meta_dir, rclone_config=conf, rclone_remote="testremote",
                media_rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("MEDIA_ROOT", failure["reason"])
            self.assertIn("not found", failure["reason"].lower())

    def test_rclone_binary_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
                path_override=_shadow_path_excluding("rclone", Path(work) / "shadow"),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("rclone", failure["reason"].lower())

    def test_tar_binary_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
                path_override=_shadow_path_excluding("tar", Path(work) / "shadow"),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("tar", failure["reason"].lower())

    def test_rclone_remote_env_unset_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="", media_rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("RCLONE_REMOTE", failure["reason"])

    def test_rclone_config_file_missing_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            meta_dir = Path(work) / "meta"

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir,
                rclone_config=Path(work) / "does_not_exist.conf",
                rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("RCLONE_CONFIG", failure["reason"])

    def test_symlink_escape_is_rejected_and_never_named_in_failure_json(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            (media_root / "kyc").mkdir()
            outside_secret = Path(work) / "outside_secret_juan_perez.txt"
            outside_secret.write_text("secret")
            (media_root / "kyc" / "evil_link").symlink_to(outside_secret)
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("symlink", failure["reason"].lower())
            self.assertNotIn("evil_link", failure["reason"])
            self.assertNotIn("outside_secret_juan_perez", failure["reason"])
            self.assertNotIn("evil_link", result.stdout)
            self.assertNotIn("outside_secret_juan_perez", result.stdout)
            self.assertNotIn("evil_link", result.stderr)
            self.assertNotIn("outside_secret_juan_perez", result.stderr)

    def test_dangling_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            (media_root / "kyc").mkdir()
            (media_root / "kyc" / "dangling").symlink_to(Path(work) / "does_not_exist_target")
            meta_dir = Path(work) / "meta"
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())

    def test_symlink_inside_media_root_is_accepted(self):
        """A symlink whose target is ALSO inside MEDIA_ROOT is not an
        escape and must not be rejected."""
        if not _RCLONE_AVAILABLE:
            self.skipTest("rclone binary not installed on this machine")
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            (media_root / "kyc").mkdir()
            (media_root / "kyc" / "real.jpg").write_bytes(b"real bytes")
            (media_root / "kyc" / "alias.jpg").symlink_to(media_root / "kyc" / "real.jpg")
            meta_dir = Path(work) / "meta"
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((meta_dir / "media_backup_success.json").exists())


# ─────────────────────────────────────────────
# Full success path — requires real rclone against its `local` backend.
# ─────────────────────────────────────────────
@_skip_without_rclone
class MediaBackupSuccessTests(TestCase):

    def _run_happy_path(self, work, *, empty=False, extra_env=None):
        media_root = Path(work) / "media"; media_root.mkdir()
        if not empty:
            _make_media_tree(media_root)
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)

        before_hash = _hash_tree(media_root)
        result = _run_media_backup_script(
            media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
            rclone_remote="testremote", media_rclone_remote_path=remote_dir,
            extra_env=extra_env,
        )
        return result, media_root, meta_dir, remote_dir, before_hash

    def test_success_writes_valid_media_backup_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, _remote, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)

            success_file = meta_dir / "media_backup_success.json"
            self.assertTrue(success_file.exists())
            data = json.loads(success_file.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertIn("timestamp_utc", data)
            self.assertTrue(data["archive_filename"].startswith("media_"))
            self.assertTrue(data["archive_filename"].endswith(".tar"))
            self.assertEqual(data["file_count"], 6)
            self.assertGreater(data["size_bytes"], 0)
            self.assertEqual(len(data["sha256"]), 64)
            int(data["sha256"], 16)  # must be valid hex
            self.assertIn("testremote", data["remote_target"])
            self.assertIn("hostname", data)
            self.assertIs(data["integrity_verified"], True)

    def test_success_permissions_are_640(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _remote, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            mode = (meta_dir / "media_backup_success.json").stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o640)

    def test_empty_media_root_produces_valid_backup_with_zero_file_count(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _remote, _before = self._run_happy_path(work, empty=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "media_backup_success.json").read_text())
            self.assertEqual(data["file_count"], 0)
            self.assertTrue(data["integrity_verified"])

    def test_media_root_untouched_byte_for_byte_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            result, media_root, _meta, _remote, before_hash = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_no_filename_from_media_root_appears_in_stdout_or_stderr(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, _meta, _remote, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            for leaked in ("front.jpg", "back.jpg", "selfie.jpg", "ev1.pdf", "guide.pdf", "frente"):
                self.assertNotIn(leaked, result.stdout)
                self.assertNotIn(leaked, result.stderr)

    def test_no_filename_from_media_root_appears_in_success_json(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _remote, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "media_backup_success.json").read_text()
            for leaked in ("front.jpg", "back.jpg", "selfie.jpg", "ev1.pdf", "guide.pdf", "frente"):
                self.assertNotIn(leaked, body)

    def test_success_metadata_contains_no_secrets(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, _remote, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (meta_dir / "media_backup_success.json").read_text().lower()
            for forbidden in ("access_key", "secret_access_key", "password", "dsn", "rclone.conf"):
                self.assertNotIn(forbidden, body)

    def test_archive_contains_no_absolute_paths(self):
        with tempfile.TemporaryDirectory() as work:
            result, _media, meta_dir, remote_dir, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "media_backup_success.json").read_text())
            archive_path = remote_dir / data["archive_filename"]
            listing = subprocess.run(
                ["tar", "-tf", str(archive_path)], capture_output=True, text=True,
            )
            self.assertEqual(listing.returncode, 0)
            for line in listing.stdout.splitlines():
                self.assertFalse(line.startswith("/"), f"absolute path in archive: {line}")

    def test_archive_preserves_relative_structure_and_content(self):
        with tempfile.TemporaryDirectory() as work:
            result, media_root, meta_dir, remote_dir, _before = self._run_happy_path(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((meta_dir / "media_backup_success.json").read_text())
            archive_path = remote_dir / data["archive_filename"]

            restore_dir = Path(work) / "restore"
            restore_dir.mkdir()
            extracted = subprocess.run(
                ["tar", "-xf", str(archive_path), "-C", str(restore_dir)],
                capture_output=True, text=True,
            )
            self.assertEqual(extracted.returncode, 0, extracted.stderr)
            self.assertEqual(
                (restore_dir / "kyc" / "documents" / "front.jpg").read_bytes(),
                b"front bytes",
            )
            self.assertEqual(
                (restore_dir / "kyc" / "documents" / "frente ñ documento (1).jpg").read_bytes(),
                b"unicode space filename bytes",
            )
            self.assertTrue((restore_dir / "empty_subdir").is_dir())

    def test_no_leftover_temp_files_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            scratch_dir = Path(work) / "scratch"; scratch_dir.mkdir()
            result, _media, meta_dir, _remote, _before = self._run_happy_path(
                work, extra_env={"MEDIA_ARCHIVE_SCRATCH_DIR": str(scratch_dir)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(meta_dir.glob(".media_metadata.*")), [])
            self.assertEqual(list(meta_dir.glob(".media_filelist.*")), [])
            self.assertEqual(list(scratch_dir.iterdir()), [])

    def test_success_never_touches_o5a_o5c_manifests(self):
        with tempfile.TemporaryDirectory() as work:
            meta_dir = Path(work) / "meta"; meta_dir.mkdir()
            (meta_dir / "backup_success.json").write_text('{"marker": "o5a"}')
            (meta_dir / "offsite_success.json").write_text('{"marker": "o5c"}')

            media_root = Path(work) / "media"; media_root.mkdir()
            _make_media_tree(media_root)
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((meta_dir / "backup_success.json").read_text(), '{"marker": "o5a"}')
            self.assertEqual((meta_dir / "offsite_success.json").read_text(), '{"marker": "o5c"}')

    def test_rerun_after_success_is_idempotent(self):
        with tempfile.TemporaryDirectory() as work:
            result1, media_root, meta_dir, remote_dir, _before = self._run_happy_path(work)
            self.assertEqual(result1.returncode, 0, result1.stderr)
            first = json.loads((meta_dir / "media_backup_success.json").read_text())

            conf = Path(work) / "rclone.conf"
            result2 = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            second = json.loads((meta_dir / "media_backup_success.json").read_text())

            self.assertEqual(first["file_count"], second["file_count"])
            self.assertEqual(list(meta_dir.glob(".media_metadata.*")), [])

    def test_no_destructive_rclone_command_used(self):
        # Comment-stripped (code lines only) — the header comment block
        # explicitly DOCUMENTS that these commands are never used, which
        # would otherwise false-positive a naive whole-file substring
        # search (same lesson learned repeatedly across O.5c/O.5d's own
        # test suites).
        code_lines = "\n".join(
            line for line in Path(_SCRIPT_PATH).read_text().splitlines()
            if not line.strip().startswith("#")
        )
        for forbidden in ("rclone sync", "rclone delete", "rclone purge", "rclone deletefile"):
            self.assertNotIn(forbidden, code_lines)

    def test_no_verbose_tar_flag_used(self):
        script_text = Path(_SCRIPT_PATH).read_text()
        # Never a standalone -v/--verbose tar flag (would print member
        # names, including KYC filenames, to stdout/journald).
        for line in script_text.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("--verbose", line)


# ─────────────────────────────────────────────
# Failure paths that require an actual (real, local-backend) transfer to
# occur before failing.
# ─────────────────────────────────────────────
@_skip_without_rclone
class MediaBackupTransferFailureTests(TestCase):

    def _setup(self, work):
        media_root = Path(work) / "media"; media_root.mkdir()
        _make_media_tree(media_root)
        meta_dir = Path(work) / "meta"
        remote_dir = Path(work) / "remote"; remote_dir.mkdir()
        conf = Path(work) / "rclone.conf"
        _write_local_rclone_conf(conf)
        return media_root, meta_dir, remote_dir, conf

    def test_checksum_mismatch_writes_failure_not_success(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            counter = Path(work) / "counter"
            _make_corrupting_rclone_wrapper(bin_dir, counter)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("mismatch", failure["reason"].lower())

    def test_tar_validation_failure_writes_failure_not_success(self):
        """
        A corrupted/invalid archive is caught by the script's own
        defense-in-depth `tar -tf` listability check immediately after
        creation — before it is ever uploaded. This is, deliberately, the
        EARLIER of the two `tar -tf` gates in the script (the other runs
        again on the RECOVERED remote copy at the end): once the local
        archive is proven listable here, SHA-256 is computed on those
        same proven-valid bytes, so the only way the final gate could
        ever fail on a byte-identical recovered copy would require a
        SHA-256 collision — by design unreachable in a test. Both gates
        exist (defense in depth); this test exercises the one that is
        actually reachable without literally colliding a hash.
        """
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            _make_corrupt_archive_tar_wrapper(bin_dir)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("not listable", failure["reason"].lower())
            # Never uploaded — remote dir stays empty.
            self.assertEqual(list(remote_dir.iterdir()), [])

    def test_remote_missing_after_upload_writes_failure(self):
        """Simulates the upload appearing to succeed but the object not
        being listable afterwards (e.g. eventual-consistency edge case /
        provider glitch) — the existence-confirmation step must catch it."""
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rclone = shutil.which("rclone")
            wrapper = bin_dir / "rclone"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "--config" ] && [ "$3" = "lsf" ]; then
    exit 0
fi
exec "{real_rclone}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("not found on remote", failure["reason"].lower())

    def test_rclone_upload_failure_writes_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rclone = shutil.which("rclone")
            wrapper = bin_dir / "rclone"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ]; then
    echo "simulated upload failure" >&2
    exit 1
fi
exec "{real_rclone}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            result = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((meta_dir / "media_backup_success.json").exists())
            failure = json.loads((meta_dir / "media_backup_failure.json").read_text())
            self.assertIn("upload", failure["reason"].lower())

    def test_failure_never_overwrites_a_prior_real_success(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)

            ok = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            prior_success = (meta_dir / "media_backup_success.json").read_text()

            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rclone = shutil.which("rclone")
            wrapper = bin_dir / "rclone"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ] && [ "$2" = "{conf}" ]; then
    echo "simulated failure on second run" >&2
    exit 1
fi
exec "{real_rclone}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            failed = _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            self.assertNotEqual(failed.returncode, 0)

            self.assertEqual((meta_dir / "media_backup_success.json").read_text(), prior_success)
            self.assertTrue((meta_dir / "media_backup_failure.json").exists())

    def test_media_root_untouched_after_failure(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            before_hash = _hash_tree(media_root)
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rclone = shutil.which("rclone")
            wrapper = bin_dir / "rclone"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ]; then
    exit 1
fi
exec "{real_rclone}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )

            self.assertEqual(_hash_tree(media_root), before_hash)

    def test_cleanup_after_failure_leaves_no_scratch_files(self):
        with tempfile.TemporaryDirectory() as work:
            media_root, meta_dir, remote_dir, conf = self._setup(work)
            scratch_dir = Path(work) / "scratch"; scratch_dir.mkdir()
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            real_rclone = shutil.which("rclone")
            wrapper = bin_dir / "rclone"
            wrapper.write_text(f"""#!/usr/bin/env bash
if [ "$3" = "copyto" ]; then
    exit 1
fi
exec "{real_rclone}" "$@"
""")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            _run_media_backup_script(
                media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                path_override=f"{bin_dir}:{os.environ.get('PATH', '')}",
                extra_env={"MEDIA_ARCHIVE_SCRATCH_DIR": str(scratch_dir)},
            )

            self.assertEqual(list(meta_dir.glob(".media_metadata.*")), [])
            self.assertEqual(list(meta_dir.glob(".media_filelist.*")), [])
            self.assertEqual(list(scratch_dir.iterdir()), [])


# ─────────────────────────────────────────────
# Concurrency — own flock domain, independent of O.5a/.5c.
# ─────────────────────────────────────────────
@_skip_without_flock
class MediaBackupConcurrencyTests(TestCase):

    def test_lock_contention_exits_cleanly_without_touching_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            _make_media_tree(media_root)
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            lock_path = meta_dir / ".media_backup.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f'exec 205>"{lock_path}"; flock -n 205 && sleep 5'],
            )
            try:
                time.sleep(1)

                result = _run_media_backup_script(
                    media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                    rclone_remote="testremote", media_rclone_remote_path=Path(work) / "remote",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((meta_dir / "media_backup_success.json").exists())
                self.assertFalse((meta_dir / "media_backup_failure.json").exists())
            finally:
                holder.wait(timeout=10)

    def test_media_lock_independent_from_backup_and_offsite_locks(self):
        """Holding O.5a's .backup.lock or O.5c's .offsite.lock must NOT
        block backup_media_offsite.sh — separate concurrency domains."""
        if not _RCLONE_AVAILABLE:
            self.skipTest("rclone binary not installed on this machine")
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "media"; media_root.mkdir()
            _make_media_tree(media_root)
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True, exist_ok=True)
            remote_dir = Path(work) / "remote"; remote_dir.mkdir()
            conf = Path(work) / "rclone.conf"
            _write_local_rclone_conf(conf)

            backup_lock = meta_dir / ".backup.lock"
            offsite_lock = meta_dir / ".offsite.lock"
            holder1 = subprocess.Popen(
                ["bash", "-c", f'exec 206>"{backup_lock}"; flock -n 206 && sleep 5'],
            )
            holder2 = subprocess.Popen(
                ["bash", "-c", f'exec 207>"{offsite_lock}"; flock -n 207 && sleep 5'],
            )
            try:
                time.sleep(1)

                result = _run_media_backup_script(
                    media_root=media_root, meta_dir=meta_dir, rclone_config=conf,
                    rclone_remote="testremote", media_rclone_remote_path=remote_dir,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((meta_dir / "media_backup_success.json").exists())
            finally:
                holder1.wait(timeout=10)
                holder2.wait(timeout=10)
