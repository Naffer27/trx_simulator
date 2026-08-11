# simulator/tests/test_o5b_backup_scheduler_systemd_units.py
"""
Microbloque O.5b — Automated PostgreSQL Backup Scheduler.

Covers two independent things:

1. Static validation of deploy/systemd/backup-postgres.{service,timer} —
   text/regex based, since there is no systemd on the machine running
   this suite (macOS dev / most CI). If `systemd-analyze` happens to be
   available, it is used as an ADDITIONAL check — nothing here ever
   depends on systemd being active or installed.

2. The flock-based mutual-exclusion guard added to
   deploy/scripts/backup_postgres.sh (O.5b) — via real subprocess runs
   with pg_dump/pg_restore stubbed (same technique as O.5a's
   BackupScriptSuccessTests/BackupScriptFailureTests). Confirms a
   concurrent second run is rejected without touching either
   backup_success.json or backup_failure.json, and that a REAL failure
   still behaves exactly as O.5a already established (failure metadata
   written, last success preserved).

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, or install anything on this machine.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from django.test import TestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-postgres.service"
_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-postgres.timer"
_SCRIPT_PATH = _REPO_ROOT / "deploy" / "scripts" / "backup_postgres.sh"
_DEPLOY_MD_PATH = _REPO_ROOT / "DEPLOY.md"


def _read(path: Path) -> str:
    return path.read_text()


def _directives_only(content: str) -> str:
    """Strip comment lines and blank lines, leaving only the actual INI
    directives systemd itself would parse. Several tests below assert
    the ABSENCE of certain tokens (redis, celery, daphne,
    DJANGO_SETTINGS_MODULE, network-online.target) as functional
    directives — the unit files' own header comments legitimately
    discuss and name those same tokens to explain why they were
    deliberately omitted, so checking the raw file content would be a
    false positive against the file's own documentation."""
    return "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ─────────────────────────────────────────────
# backup-postgres.service — static content
# ─────────────────────────────────────────────

class ServiceUnitFileTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _read(_SERVICE_PATH)
        cls.directives = _directives_only(cls.content)

    def test_file_exists(self):
        self.assertTrue(_SERVICE_PATH.exists())

    def test_has_unit_service_sections(self):
        self.assertIn("[Unit]", self.content)
        self.assertIn("[Service]", self.content)

    def test_is_oneshot(self):
        self.assertRegex(self.content, r"(?m)^Type=oneshot$")

    def test_user_and_group_are_trx_sim(self):
        self.assertRegex(self.content, r"(?m)^User=trx_sim$")
        self.assertRegex(self.content, r"(?m)^Group=trx_sim$")

    def test_working_directory_correct(self):
        self.assertRegex(self.content, r"(?m)^WorkingDirectory=/opt/trx_sim$")

    def test_environment_file_correct(self):
        self.assertRegex(self.content, r"(?m)^EnvironmentFile=/opt/trx_sim/\.env$")

    def test_exec_start_correct(self):
        self.assertRegex(
            self.content,
            r"(?m)^ExecStart=/bin/bash /opt/trx_sim/deploy/scripts/backup_postgres\.sh$",
        )

    def test_timeout_start_sec_set(self):
        self.assertRegex(self.content, r"(?m)^TimeoutStartSec=21600$")

    def test_hardening_directives_present(self):
        for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict"):
            self.assertIn(directive, self.content)

    def test_read_write_paths_correct(self):
        match = re.search(r"^ReadWritePaths=(.+)$", self.content, re.MULTILINE)
        self.assertIsNotNone(match)
        paths = match.group(1).split()
        self.assertIn("/var/backups/trx_sim", paths)
        self.assertIn("/var/log/trx_sim", paths)
        # Deliberately tighter than daphne/celery-*: this service never
        # writes under /opt/trx_sim.
        self.assertNotIn("/opt/trx_sim", paths)

    def test_no_redis_celery_or_daphne_reference(self):
        lowered = self.directives.lower()
        for forbidden in ("redis", "celery", "daphne"):
            self.assertNotIn(forbidden, lowered)

    def test_no_hard_requires_on_postgresql(self):
        self.assertNotRegex(self.content, r"(?mi)^Requires=.*postgresql")
        # Soft ordering only.
        self.assertRegex(self.content, r"(?m)^After=postgresql\.service$")

    def test_no_network_online_target(self):
        self.assertNotIn("network-online.target", self.directives)

    def test_no_restart_on_failure(self):
        self.assertNotRegex(self.content, r"(?m)^Restart=on-failure$")

    def test_no_django_settings_module_or_venv(self):
        # Pure bash script — no Python/Django involved.
        self.assertNotIn("DJANGO_SETTINGS_MODULE", self.directives)
        self.assertNotIn("/opt/trx_sim/venv", self.directives)

    def test_journald_logging_configured(self):
        self.assertIn("StandardOutput=journal", self.content)
        self.assertIn("StandardError=journal", self.content)


# ─────────────────────────────────────────────
# backup-postgres.timer — static content
# ─────────────────────────────────────────────

class TimerUnitFileTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _read(_TIMER_PATH)

    def test_file_exists(self):
        self.assertTrue(_TIMER_PATH.exists())

    def test_has_unit_timer_install_sections(self):
        self.assertIn("[Unit]", self.content)
        self.assertIn("[Timer]", self.content)
        self.assertIn("[Install]", self.content)

    def test_on_calendar_is_daily_0300_utc_explicit(self):
        self.assertRegex(
            self.content, r"(?m)^OnCalendar=\*-\*-\* 03:00:00 UTC$",
        )

    def test_persistent_true(self):
        self.assertRegex(self.content, r"(?m)^Persistent=true$")

    def test_randomized_delay_sec_300(self):
        self.assertRegex(self.content, r"(?m)^RandomizedDelaySec=300$")

    def test_accuracy_sec_1min(self):
        self.assertRegex(self.content, r"(?m)^AccuracySec=1min$")

    def test_unit_points_at_backup_postgres_service(self):
        self.assertRegex(self.content, r"(?m)^Unit=backup-postgres\.service$")

    def test_wanted_by_timers_target(self):
        self.assertRegex(self.content, r"(?m)^WantedBy=timers\.target$")


class ServiceTimerCoherenceTests(TestCase):
    """The two unit files must reference each other/the real script consistently."""

    def test_timer_unit_directive_matches_service_filename(self):
        timer_content = _read(_TIMER_PATH)
        match = re.search(r"^Unit=(.+)$", timer_content, re.MULTILINE)
        self.assertEqual(match.group(1), _SERVICE_PATH.name)

    def test_service_exec_start_points_at_the_real_script_file(self):
        service_content = _read(_SERVICE_PATH)
        self.assertTrue(_SCRIPT_PATH.exists())
        self.assertIn(str(_SCRIPT_PATH).replace(str(_REPO_ROOT), "/opt/trx_sim"), service_content)


class SystemdAnalyzeOptionalTests(TestCase):
    """Additional validation IF systemd-analyze happens to be available.

    Never required — this class self-skips entirely otherwise, per O.5b
    Fase 0 §11 ("no dependas obligatoriamente de systemd activo")."""

    def test_systemd_analyze_verify_if_available(self):
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze not available on this machine")
        for unit in (_SERVICE_PATH, _TIMER_PATH):
            result = subprocess.run(
                [analyzer, "verify", str(unit)], capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


# ─────────────────────────────────────────────
# DEPLOY.md — provisioning documented
# ─────────────────────────────────────────────

class ProvisioningDocumentedTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _read(_DEPLOY_MD_PATH)

    def test_backup_dir_provisioning_commands_present(self):
        for cmd in (
            "mkdir -p /var/backups/trx_sim",
            "chown trx_sim:trx_sim /var/backups/trx_sim",
            "chmod 750 /var/backups/trx_sim",
        ):
            self.assertIn(cmd, self.content)

    def test_unit_install_commands_present(self):
        self.assertIn("deploy/systemd/backup-postgres.service", self.content)
        self.assertIn("deploy/systemd/backup-postgres.timer", self.content)
        self.assertIn("systemctl enable --now backup-postgres.timer", self.content)


# ─────────────────────────────────────────────
# backup_postgres.sh — flock mutual exclusion (subprocess, stubbed pg_*)
# ─────────────────────────────────────────────

def _make_fake_pg_tools(bin_dir, *, dump_ok=True, restore_ok=True, dump_sleep=0):
    dump_path = Path(bin_dir) / "pg_dump"
    restore_path = Path(bin_dir) / "pg_restore"

    if dump_ok:
        sleep_line = f"sleep {dump_sleep}\n" if dump_sleep else ""
        dump_path.write_text(
            f"#!/usr/bin/env bash\n{sleep_line}echo 'FAKE DUMP CONTENT FOR O5B TESTS'\n"
        )
    else:
        dump_path.write_text("#!/usr/bin/env bash\necho 'fake pg_dump failure' >&2\nexit 1\n")

    if restore_ok:
        restore_path.write_text("#!/usr/bin/env bash\nexit 0\n")
    else:
        restore_path.write_text("#!/usr/bin/env bash\necho 'fake pg_restore failure' >&2\nexit 1\n")

    for p in (dump_path, restore_path):
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_backup_script(bin_dir, backup_dir, metadata_dir, db_name="trx_sim_o5b_test", extra_path=""):
    env = dict(os.environ)
    path_prefix = f"{extra_path}:" if extra_path else ""
    env["PATH"] = f"{path_prefix}{bin_dir}:{env.get('PATH', '')}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(metadata_dir)
    env["DB_NAME"] = db_name
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH)], env=env, capture_output=True, text=True, timeout=30,
    )


def _start_backup_script(bin_dir, backup_dir, metadata_dir, db_name="trx_sim_o5b_test", extra_path=""):
    env = dict(os.environ)
    path_prefix = f"{extra_path}:" if extra_path else ""
    env["PATH"] = f"{path_prefix}{bin_dir}:{env.get('PATH', '')}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["BACKUP_METADATA_PATH"] = str(metadata_dir)
    env["DB_NAME"] = db_name
    return subprocess.Popen(
        ["bash", str(_SCRIPT_PATH)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


_FLOCK_AVAILABLE = shutil.which("flock") is not None


class LockContentionTests(TestCase):
    """Requires a real `flock` binary — present on any target Linux VPS
    (util-linux), and installable locally via `brew install flock` for
    development. Skips cleanly if genuinely unavailable rather than
    failing the whole suite."""

    def setUp(self):
        if not _FLOCK_AVAILABLE:
            self.skipTest("flock binary not available on this machine")

    def test_concurrent_manual_run_is_rejected_without_touching_metadata(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True, dump_sleep=2)

            proc_a = _start_backup_script(bin_dir, backup_dir, meta_dir)
            time.sleep(0.5)  # let A acquire the lock and start "dumping"

            result_b = _run_backup_script(bin_dir, backup_dir, meta_dir)

            out_a, err_a = proc_a.communicate(timeout=10)

            self.assertEqual(proc_a.returncode, 0, err_a)
            self.assertEqual(result_b.returncode, 0, result_b.stderr)
            self.assertIn("already in progress", result_b.stdout + result_b.stderr)

            # A succeeded and wrote the durability signal.
            self.assertTrue((meta_dir / "backup_success.json").exists())
            # B's rejection must NOT have produced a failure record.
            self.assertFalse((meta_dir / "backup_failure.json").exists())

    def test_rejected_run_never_invokes_pg_dump(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            # dump_sleep long enough to guarantee B observes the lock held.
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True, dump_sleep=3)

            proc_a = _start_backup_script(bin_dir, backup_dir, meta_dir)
            time.sleep(0.5)

            result_b = _run_backup_script(bin_dir, backup_dir, meta_dir)
            proc_a.communicate(timeout=10)

            # B must produce no dump file of its own — only A's file exists.
            dumps = list(backup_dir.glob("*.dump"))
            self.assertEqual(len(dumps), 1)
            self.assertEqual(result_b.returncode, 0)

    def test_lock_contention_does_not_overwrite_existing_success(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"

            # First, a real prior success.
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)
            first = _run_backup_script(bin_dir, backup_dir, meta_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            original_content = (meta_dir / "backup_success.json").read_text()

            # Now simulate a slow second legitimate run holding the lock,
            # and a third manual attempt racing it.
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True, dump_sleep=2)
            proc_a = _start_backup_script(bin_dir, backup_dir, meta_dir)
            time.sleep(0.5)
            result_b = _run_backup_script(bin_dir, backup_dir, meta_dir)
            proc_a.communicate(timeout=10)

            self.assertEqual(result_b.returncode, 0)
            # backup_success.json now reflects A's newer (second) run —
            # not B's, since B never ran pg_dump at all — confirmed by
            # the filename/timestamp differing from the first run.
            final = (meta_dir / "backup_success.json").read_text()
            self.assertNotEqual(final, "")
            self.assertNotEqual(final, original_content)
            self.assertFalse((meta_dir / "backup_failure.json").exists())

    def test_real_failure_after_lock_acquired_still_writes_failure_metadata(self):
        # Regression: the lock guard must not interfere with O.5a's
        # existing failure-metadata behavior for a REAL failure (as
        # opposed to lock contention, which must never write anything).
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=False)

            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((meta_dir / "backup_failure.json").exists())
            self.assertFalse((meta_dir / "backup_success.json").exists())

    def test_real_failure_preserves_prior_success_even_with_lock_guard(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"

            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)
            ok_result = _run_backup_script(bin_dir, backup_dir, meta_dir)
            self.assertEqual(ok_result.returncode, 0, ok_result.stderr)
            original_content = (meta_dir / "backup_success.json").read_text()

            _make_fake_pg_tools(bin_dir, dump_ok=False)
            fail_result = _run_backup_script(bin_dir, backup_dir, meta_dir)
            self.assertNotEqual(fail_result.returncode, 0)

            self.assertEqual((meta_dir / "backup_success.json").read_text(), original_content)

    def test_lock_file_created_under_metadata_path(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)

            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((meta_dir / ".backup.lock").exists())

    def test_sequential_runs_both_succeed_lock_is_released_after_completion(self):
        # The lock must be released once a run finishes — a second run
        # AFTER the first one completes (not concurrent with it) must
        # succeed normally, not be rejected forever.
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)

            first = _run_backup_script(bin_dir, backup_dir, meta_dir)
            # The dump filename has 1-second resolution
            # (date '+%Y%m%d_%H%M%S'); in production runs are a day
            # apart and never collide, but two back-to-back subprocess
            # calls in a fast test can land in the same second and
            # overwrite each other's file. Sleep past the boundary so
            # the "2 distinct files" assertion below is meaningful.
            time.sleep(1.1)
            second = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            dumps = list(backup_dir.glob("*.dump"))
            self.assertEqual(len(dumps), 2)


class MetadataDirCreatedBeforeLockTests(TestCase):
    """BACKUP_METADATA_PATH must exist before the flock is attempted —
    otherwise `exec 200>"$LOCK_FILE"` would fail against a nonexistent
    directory. This never touches a real DB — pg_dump is never reached
    when metadata-dir creation itself is what's being exercised (the
    directory simply not existing yet, then being created by the
    script)."""

    def test_metadata_dir_auto_created_even_when_missing(self):
        with tempfile.TemporaryDirectory() as work:
            bin_dir = Path(work) / "bin"; bin_dir.mkdir()
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "does" / "not" / "exist" / "yet"
            _make_fake_pg_tools(bin_dir, dump_ok=True, restore_ok=True)

            self.assertFalse(meta_dir.exists())
            result = _run_backup_script(bin_dir, backup_dir, meta_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(meta_dir.exists())
            self.assertTrue((meta_dir / "backup_success.json").exists())
