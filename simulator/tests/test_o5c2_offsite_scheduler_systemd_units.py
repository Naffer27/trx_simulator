# simulator/tests/test_o5c2_offsite_scheduler_systemd_units.py
"""
Microbloque O.5c-2 — Offsite Backup Scheduler (systemd).

Covers deploy/systemd/backup-offsite.{service,timer} — static/regex
validation, the same technique O.5b already established for
backup-postgres.{service,timer} (no systemd on the machine running this
suite — macOS dev / most CI). If `systemd-analyze` happens to be
available, it is used as an ADDITIONAL check; nothing here ever depends
on systemd being active or installed.

Frozen decisions under test (O.5c-2 approval):
  - Type=oneshot, User=Group=trx_sim, script is EXCLUSIVELY
    deploy/scripts/backup_offsite.sh (O.5c-1, unchanged by this
    microbloque — this suite never edits or re-tests that script's own
    behavior, see test_o5c1_offsite_backup_verification.py for that).
  - After=/Wants=network-online.target (the opposite of
    backup-postgres.service, which deliberately omits it) — this is the
    first and only unit in the project that needs outbound network.
  - ReadWritePaths is /var/log/trx_sim ONLY — deliberately NOT
    /var/backups/trx_sim, since backup_offsite.sh only ever reads the
    local dump, never writes/deletes it; under ProtectSystem=strict
    that path remains readable-but-not-writable to this unit with no
    extra directive.
  - No Requires=/After=/OnSuccess=/OnFailure= linking this unit to
    backup-postgres.service/.timer in either direction — the two must
    stay fully independent so an offsite hiccup can never affect the
    local backup and vice versa.
  - backup-offsite.timer fires at 03:30 UTC — 30 minutes after
    backup-postgres.timer's 03:00 UTC fire, RE-VERIFIED against that
    unit's actual current content in this same suite (not assumed).
  - rclone.conf (real credentials) never appears in any tracked file —
    not this service, not this timer, not DEPLOY.md, not this test file.

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, docs/BOOK06_RC1_AUDIT.md, or install anything on this
machine. Does NOT modify or re-test backup_postgres.sh/backup_offsite.sh
themselves — those are frozen as of O.5a/O.5b/O.5c-1 approval.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-offsite.service"
_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-offsite.timer"
_SCRIPT_PATH = _REPO_ROOT / "deploy" / "scripts" / "backup_offsite.sh"
_DEPLOY_MD_PATH = _REPO_ROOT / "DEPLOY.md"
_GITIGNORE_PATH = _REPO_ROOT / ".gitignore"
_RCLONE_EXAMPLE_PATH = _REPO_ROOT / "deploy" / "rclone.conf.example"

_O5B_SERVICE_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-postgres.service"
_O5B_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-postgres.timer"

# Strings that must never appear in any tracked file this suite checks —
# a real credential, or the shape of one, leaking into git.
_FORBIDDEN_SECRET_PATTERNS = (
    "access_key_id = ",
    "secret_access_key = ",
)


def _read(path: Path) -> str:
    return path.read_text()


def _directives_only(content: str) -> str:
    """Strip comment lines and blank lines — mirrors O.5b's own helper,
    since header comments here also legitimately name tokens
    (network-online.target, backup-postgres) being explained rather
    than used as directives."""
    return "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ─────────────────────────────────────────────
# backup-offsite.service — static content
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

    def test_exec_start_runs_exclusively_backup_offsite_script(self):
        self.assertRegex(
            self.content,
            r"(?m)^ExecStart=/bin/bash /opt/trx_sim/deploy/scripts/backup_offsite\.sh$",
        )
        # Exactly one ExecStart= directive — no ExecStartPre/Post chaining
        # in extra commands, no reference to backup_postgres.sh at all.
        exec_lines = [l for l in self.directives.splitlines() if l.startswith("ExecStart")]
        self.assertEqual(len(exec_lines), 1)
        self.assertNotIn("backup_postgres.sh", self.directives)

    def test_timeout_start_sec_set_and_reasonable(self):
        match = re.search(r"(?m)^TimeoutStartSec=(\d+)$", self.content)
        self.assertIsNotNone(match)
        seconds = int(match.group(1))
        # Must cover upload + independent verification download (O.5c-1
        # transfers the dump twice) with real margin, while leaving
        # meaningful room before the next ~03:30 UTC fire (24h later).
        self.assertGreaterEqual(seconds, 900)      # at least 15 min
        self.assertLessEqual(seconds, 21600)        # never exceeds O.5b's own bound

    def test_hardening_directives_present(self):
        for directive in (
            "NoNewPrivileges=true", "PrivateTmp=true",
            "ProtectSystem=strict", "ProtectHome=true",
        ):
            self.assertIn(directive, self.content)

    def test_read_write_paths_correct_and_tighter_than_o5b(self):
        match = re.search(r"^ReadWritePaths=(.+)$", self.content, re.MULTILINE)
        self.assertIsNotNone(match)
        paths = match.group(1).split()
        self.assertEqual(paths, ["/var/log/trx_sim"])
        # backup_offsite.sh only READS the local dump — must never be
        # granted write access to it.
        self.assertNotIn("/var/backups/trx_sim", paths)
        self.assertNotIn("/opt/trx_sim", paths)

    def test_restrict_address_families_scoped(self):
        match = re.search(r"^RestrictAddressFamilies=(.+)$", self.content, re.MULTILINE)
        self.assertIsNotNone(match)
        families = set(match.group(1).split())
        self.assertEqual(families, {"AF_INET", "AF_INET6", "AF_UNIX"})

    def test_network_online_target_present(self):
        self.assertIn("After=network-online.target", self.directives)
        self.assertIn("Wants=network-online.target", self.directives)

    def test_no_dependency_on_backup_postgres_unit(self):
        lowered = self.directives.lower()
        self.assertNotIn("backup-postgres", lowered)
        self.assertNotRegex(self.content, r"(?mi)^(Requires|BindsTo|OnSuccess|OnFailure)=.*backup-postgres")

    def test_no_redis_celery_or_daphne_reference(self):
        lowered = self.directives.lower()
        for forbidden in ("redis", "celery", "daphne"):
            self.assertNotIn(forbidden, lowered)

    def test_no_restart_on_failure(self):
        self.assertNotRegex(self.content, r"(?m)^Restart=on-failure$")

    def test_no_success_exit_status_masking_failure(self):
        # A real backup_offsite.sh failure must propagate as unit
        # failure — SuccessExitStatus= could be used to whitewash a
        # nonzero exit, so it must never appear here.
        self.assertNotIn("SuccessExitStatus", self.content)

    def test_no_django_settings_module_or_venv(self):
        self.assertNotIn("DJANGO_SETTINGS_MODULE", self.directives)
        self.assertNotIn("/opt/trx_sim/venv", self.directives)

    def test_journald_logging_configured(self):
        self.assertIn("StandardOutput=journal", self.content)
        self.assertIn("StandardError=journal", self.content)

    def test_no_secrets_or_credentials_present(self):
        lowered = self.content.lower()
        for forbidden in _FORBIDDEN_SECRET_PATTERNS:
            self.assertNotIn(forbidden, lowered)
        for forbidden in ("r2.cloudflarestorage.com", "amazonaws.com", "backblazeb2.com"):
            self.assertNotIn(forbidden, lowered)

    def test_no_hardcoded_provider_or_remote_name(self):
        # RCLONE_REMOTE/RCLONE_CONFIG/RCLONE_REMOTE_PATH must come
        # exclusively from EnvironmentFile — never a literal remote
        # name baked into an actual ExecStart/Environment= DIRECTIVE.
        # Checked against comment-stripped content: the header's own
        # rationale legitimately discusses these tokens (e.g. "the
        # remote configured in RCLONE_CONFIG", "Cloudflare R2" as the
        # documented initial target) without making the unit itself
        # provider-specific — same distinction O.5b's own suite already
        # draws for redis/celery/daphne mentions in its header comments.
        self.assertNotIn("RCLONE_REMOTE=", self.directives)
        self.assertNotIn("RCLONE_CONFIG=", self.directives)
        self.assertNotRegex(self.directives, r"(?i)cloudflare|backblaze|\bR2\b|\bS3\b")


# ─────────────────────────────────────────────
# backup-offsite.timer — static content
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

    def test_on_calendar_is_daily_0330_utc_explicit(self):
        self.assertRegex(
            self.content, r"(?m)^OnCalendar=\*-\*-\* 03:30:00 UTC$",
        )

    def test_persistent_true(self):
        self.assertRegex(self.content, r"(?m)^Persistent=true$")

    def test_randomized_delay_sec_is_reasonable_jitter(self):
        match = re.search(r"(?m)^RandomizedDelaySec=(\d+)$", self.content)
        self.assertIsNotNone(match)
        seconds = int(match.group(1))
        self.assertGreater(seconds, 0)
        self.assertLess(seconds, 1800)  # jitter, not a second scheduling window

    def test_accuracy_sec_1min(self):
        self.assertRegex(self.content, r"(?m)^AccuracySec=1min$")

    def test_unit_points_at_backup_offsite_service(self):
        self.assertRegex(self.content, r"(?m)^Unit=backup-offsite\.service$")

    def test_wanted_by_timers_target(self):
        self.assertRegex(self.content, r"(?m)^WantedBy=timers\.target$")

    def test_no_dependency_on_backup_postgres_unit(self):
        directives = _directives_only(self.content)
        self.assertNotIn("backup-postgres", directives.lower())


# ─────────────────────────────────────────────
# Cross-file coherence and independence from O.5b
# ─────────────────────────────────────────────
class ServiceTimerCoherenceTests(TestCase):

    def test_timer_unit_directive_matches_service_filename(self):
        timer_content = _read(_TIMER_PATH)
        match = re.search(r"^Unit=(.+)$", timer_content, re.MULTILINE)
        self.assertEqual(match.group(1), _SERVICE_PATH.name)

    def test_service_exec_start_points_at_the_real_script_file(self):
        service_content = _read(_SERVICE_PATH)
        self.assertTrue(_SCRIPT_PATH.exists())
        self.assertIn(
            str(_SCRIPT_PATH).replace(str(_REPO_ROOT), "/opt/trx_sim"), service_content,
        )


class OffsiteTimerOffsetFromLocalBackupTests(TestCase):
    """The 30-minute offset is a documented DESIGN CHOICE, not an
    assumption — this re-reads backup-postgres.timer's ACTUAL current
    OnCalendar= before asserting the offsite timer is correctly offset
    from it, exactly as required before freezing 03:30 UTC."""

    def test_backup_postgres_timer_is_still_0300_utc(self):
        # If this ever fails, backup-offsite.timer's 03:30 UTC offset
        # assumption no longer holds and must be re-derived — this is
        # the guard against silently drifting out of sync.
        content = _read(_O5B_TIMER_PATH)
        self.assertRegex(content, r"(?m)^OnCalendar=\*-\*-\* 03:00:00 UTC$")

    def test_offsite_timer_fires_thirty_minutes_after_local_backup_timer(self):
        local_content = _read(_O5B_TIMER_PATH)
        offsite_content = _read(_TIMER_PATH)
        local_match = re.search(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):\d{2} UTC$", local_content, re.MULTILINE)
        offsite_match = re.search(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):\d{2} UTC$", offsite_content, re.MULTILINE)
        self.assertIsNotNone(local_match)
        self.assertIsNotNone(offsite_match)
        local_minutes = int(local_match.group(1)) * 60 + int(local_match.group(2))
        offsite_minutes = int(offsite_match.group(1)) * 60 + int(offsite_match.group(2))
        self.assertEqual(offsite_minutes - local_minutes, 30)


class PriorMicroblockUnitsIntactTests(TestCase):
    """Cheap same-file self-check that O.5b's units were not touched by
    this microbloque — the full guarantee is the existing
    test_o5b_backup_scheduler_systemd_units.py suite passing unchanged,
    run alongside this one; this class is a lightweight pin so this
    file is also meaningful standalone."""

    def test_backup_postgres_service_frozen_invariants(self):
        content = _read(_O5B_SERVICE_PATH)
        self.assertRegex(content, r"(?m)^Type=oneshot$")
        self.assertRegex(content, r"(?m)^User=trx_sim$")
        match = re.search(r"^ReadWritePaths=(.+)$", content, re.MULTILINE)
        paths = match.group(1).split()
        self.assertIn("/var/backups/trx_sim", paths)
        self.assertIn("/var/log/trx_sim", paths)

    def test_backup_postgres_timer_frozen_invariants(self):
        content = _read(_O5B_TIMER_PATH)
        self.assertRegex(content, r"(?m)^OnCalendar=\*-\*-\* 03:00:00 UTC$")
        self.assertRegex(content, r"(?m)^Persistent=true$")
        self.assertRegex(content, r"(?m)^Unit=backup-postgres\.service$")


class SystemdAnalyzeOptionalTests(TestCase):
    """Additional validation IF systemd-analyze happens to be available.
    Never required — self-skips entirely otherwise, same treatment as
    O.5b."""

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

    def test_rclone_conf_provisioning_commands_present(self):
        for cmd in (
            "mkdir -p /etc/trx_sim",
            "chown trx_sim:trx_sim /etc/trx_sim/rclone.conf",
            "chmod 600 /etc/trx_sim/rclone.conf",
        ):
            self.assertIn(cmd, self.content)

    def test_unit_install_commands_present(self):
        self.assertIn("deploy/systemd/backup-offsite.service", self.content)
        self.assertIn("deploy/systemd/backup-offsite.timer", self.content)
        self.assertIn("systemctl enable --now backup-offsite.timer", self.content)

    def test_env_var_contract_documented(self):
        for var in ("RCLONE_CONFIG", "RCLONE_REMOTE", "RCLONE_REMOTE_PATH"):
            self.assertIn(var, self.content)

    def test_no_secrets_in_deploy_md(self):
        lowered = self.content.lower()
        for forbidden in _FORBIDDEN_SECRET_PATTERNS:
            self.assertNotIn(forbidden, lowered)
        for forbidden in ("r2.cloudflarestorage.com/", "aws_access_key_id ="):
            self.assertNotIn(forbidden, lowered)

    def test_step_numbering_is_sequential_and_unique(self):
        step_numbers = [
            int(n) for n in re.findall(r"(?m)^### (\d+)\.", self.content)
        ]
        self.assertEqual(step_numbers, sorted(step_numbers))
        self.assertEqual(len(step_numbers), len(set(step_numbers)))


# ─────────────────────────────────────────────
# Credentials never in Git
# ─────────────────────────────────────────────
class RcloneConfigOutOfGitTests(TestCase):

    def test_gitignore_excludes_rclone_conf(self):
        content = _read(_GITIGNORE_PATH)
        self.assertIn("rclone.conf", content.splitlines())

    def test_no_file_literally_named_rclone_conf_is_tracked(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git binary not available on this machine")
        result = subprocess.run(
            [git, "ls-files"], cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        tracked = set(result.stdout.splitlines())
        # The example/template IS meant to be tracked; a real,
        # filled-in "rclone.conf" (no suffix) must never be.
        self.assertNotIn("deploy/rclone.conf", tracked)

    def test_rclone_example_contains_only_placeholder_values(self):
        content = _read(_RCLONE_EXAMPLE_PATH)
        for placeholder in ("REPLACE_WITH_REAL", "REPLACE_WITH_YOUR_ACCOUNT_ID"):
            self.assertIn(placeholder, content)


# ─────────────────────────────────────────────
# Failure propagation — a real backup_offsite.sh failure must surface
# as a nonzero exit code (which is all Type=oneshot needs to mark the
# unit failed; no ExecStart wrapper here masks or swallows it).
# ─────────────────────────────────────────────
class ScriptFailurePropagationTests(TestCase):

    def test_exec_start_is_a_direct_bash_invocation_no_wrapper(self):
        # No `|| true`, no `; exit 0`, no subshell swallowing the real
        # exit code — ExecStart must be exactly `bash <script>`.
        content = _read(_SERVICE_PATH)
        match = re.search(r"(?m)^ExecStart=(.+)$", content)
        self.assertIsNotNone(match)
        self.assertEqual(
            match.group(1), "/bin/bash /opt/trx_sim/deploy/scripts/backup_offsite.sh",
        )

    def test_real_script_failure_exits_nonzero_as_execstart_would_invoke_it(self):
        # Reproduces exactly what ExecStart= runs (`bash <script>`) with
        # a forced failure (RCLONE_REMOTE unset) — a Type=oneshot unit
        # is marked failed purely from ExecStart's exit code, so a
        # nonzero exit here IS the proof systemd would report failure.
        import os
        with tempfile.TemporaryDirectory() as work:
            backup_dir = Path(work) / "backups"
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True)
            backup_dir.mkdir(parents=True)
            (meta_dir / "backup_success.json").write_text(
                '{"schema_version":1,"timestamp_utc":"2026-01-01T03:00:00Z",'
                '"database_name":"db","filename":"missing.dump","size_bytes":1,'
                '"integrity_verified":true,"hostname":"h"}'
            )
            env = dict(os.environ)
            env["BACKUP_DIR"] = str(backup_dir)
            env["BACKUP_METADATA_PATH"] = str(meta_dir)
            env["RCLONE_CONFIG"] = str(Path(work) / "does_not_exist.conf")
            env["RCLONE_REMOTE"] = ""
            env["RCLONE_REMOTE_PATH"] = str(Path(work) / "remote")

            result = subprocess.run(
                ["bash", str(_SCRIPT_PATH)], env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
