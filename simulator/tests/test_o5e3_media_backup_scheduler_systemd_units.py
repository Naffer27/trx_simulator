# simulator/tests/test_o5e3_media_backup_scheduler_systemd_units.py
"""
Microbloque O.5e-3 — Media Backup Scheduler (systemd).

Covers deploy/systemd/backup-media-offsite.{service,timer} — static/regex
validation, the same technique O.5b/O.5c-2 already established (no
systemd on the machine running this suite — macOS dev / most CI). If
`systemd-analyze` happens to be available, it is used as an ADDITIONAL
check; nothing here ever depends on systemd being active or installed.

Frozen decisions under test (O.5e-3 approval):
  - Type=oneshot, User=Group=trx_sim, script is EXCLUSIVELY
    deploy/scripts/backup_media_offsite.sh (O.5e-2, unchanged by this
    microbloque — this suite never edits or re-tests that script's own
    behavior, see test_o5e2_media_offsite_backup.py for that).
  - After=/Wants=network-online.target — same as backup-offsite.service,
    a different unit from backup-postgres.service (which omits it).
  - ReadOnlyPaths is /opt/trx_sim/media ONLY (MEDIA_ROOT — read-only
    input, backup_media_offsite.sh never writes/deletes anything there).
  - ReadWritePaths is /var/log/trx_sim ONLY — deliberately NOT
    /opt/trx_sim/media and NOT /var/backups/trx_sim (unrelated to media).
  - No Requires=/After=/OnSuccess=/OnFailure= linking this unit to EITHER
    backup-postgres.service/.timer OR backup-offsite.service/.timer in
    either direction — all three backup paths must stay fully
    independent.
  - backup-media-offsite.timer fires at 04:00 UTC — 30 minutes after
    backup-offsite.timer's 03:30 UTC fire, RE-VERIFIED against that
    unit's actual current content in this same suite (not assumed).
  - rclone.conf (real credentials) never appears in any tracked file —
    not this service, not this timer, not this test file.

Does NOT touch Treasury, Wallet/Ledger, trading, payments,
treasury_engine, docs/BOOK06_RC1_AUDIT.md, or install anything on this
machine. Does NOT modify or re-test backup_media_offsite.sh itself
(frozen as of O.5e-2 approval), nor backup-postgres.*/backup-offsite.*
(frozen as of O.5b/O.5c-2 approval).
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-media-offsite.service"
_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-media-offsite.timer"
_SCRIPT_PATH = _REPO_ROOT / "deploy" / "scripts" / "backup_media_offsite.sh"
_DEPLOY_MD_PATH = _REPO_ROOT / "DEPLOY.md"

_O5C2_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-offsite.timer"
_O5B_TIMER_PATH = _REPO_ROOT / "deploy" / "systemd" / "backup-postgres.timer"

_FORBIDDEN_SECRET_PATTERNS = (
    "access_key_id = ",
    "secret_access_key = ",
)


def _read(path: Path) -> str:
    return path.read_text()


def _directives_only(content: str) -> str:
    """Strip comment lines and blank lines — header comments here
    legitimately name tokens (network-online.target, backup-postgres,
    backup-offsite) being explained rather than used as directives."""
    return "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ─────────────────────────────────────────────
# backup-media-offsite.service — static content
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

    def test_exec_start_runs_exclusively_media_offsite_script(self):
        self.assertRegex(
            self.content,
            r"(?m)^ExecStart=/bin/bash /opt/trx_sim/deploy/scripts/backup_media_offsite\.sh$",
        )
        exec_lines = [l for l in self.directives.splitlines() if l.startswith("ExecStart")]
        self.assertEqual(len(exec_lines), 1)
        self.assertNotIn("backup_postgres.sh", self.directives)
        self.assertNotIn("backup_offsite.sh", self.directives.replace("backup_media_offsite.sh", ""))

    def test_timeout_start_sec_set_and_reasonable(self):
        match = re.search(r"(?m)^TimeoutStartSec=(\d+)$", self.content)
        self.assertIsNotNone(match)
        seconds = int(match.group(1))
        self.assertGreaterEqual(seconds, 900)      # at least 15 min
        self.assertLessEqual(seconds, 21600)        # never exceeds O.5b's own bound

    def test_hardening_directives_present(self):
        for directive in (
            "NoNewPrivileges=true", "PrivateTmp=true",
            "ProtectSystem=strict", "ProtectHome=true",
        ):
            self.assertIn(directive, self.content)

    def test_read_only_paths_grants_media_root_only(self):
        match = re.search(r"^ReadOnlyPaths=(.+)$", self.content, re.MULTILINE)
        self.assertIsNotNone(match)
        paths = match.group(1).split()
        self.assertEqual(paths, ["/opt/trx_sim/media"])

    def test_read_write_paths_correct_and_excludes_media_root(self):
        match = re.search(r"^ReadWritePaths=(.+)$", self.content, re.MULTILINE)
        self.assertIsNotNone(match)
        paths = match.group(1).split()
        self.assertEqual(paths, ["/var/log/trx_sim"])
        # backup_media_offsite.sh only READS MEDIA_ROOT — must never be
        # granted write access to it.
        self.assertNotIn("/opt/trx_sim/media", paths)
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

    def test_no_dependency_on_other_backup_units(self):
        lowered = self.directives.lower()
        self.assertNotIn("backup-postgres", lowered)
        self.assertNotIn("backup-offsite.service", lowered)
        self.assertNotIn("backup-offsite.timer", lowered)
        self.assertNotRegex(
            self.content,
            r"(?mi)^(Requires|BindsTo|OnSuccess|OnFailure)=.*backup-(postgres|offsite)",
        )

    def test_no_redis_celery_or_daphne_reference(self):
        lowered = self.directives.lower()
        for forbidden in ("redis", "celery", "daphne"):
            self.assertNotIn(forbidden, lowered)

    def test_no_restart_on_failure(self):
        self.assertNotRegex(self.content, r"(?m)^Restart=on-failure$")

    def test_no_success_exit_status_masking_failure(self):
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
        self.assertNotIn("RCLONE_REMOTE=", self.directives)
        self.assertNotIn("RCLONE_CONFIG=", self.directives)
        self.assertNotRegex(self.directives, r"(?i)cloudflare|backblaze|\bR2\b|\bS3\b")


# ─────────────────────────────────────────────
# backup-media-offsite.timer — static content
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

    def test_on_calendar_is_daily_0400_utc_explicit(self):
        self.assertRegex(
            self.content, r"(?m)^OnCalendar=\*-\*-\* 04:00:00 UTC$",
        )

    def test_persistent_true(self):
        self.assertRegex(self.content, r"(?m)^Persistent=true$")

    def test_randomized_delay_sec_is_reasonable_jitter(self):
        match = re.search(r"(?m)^RandomizedDelaySec=(\d+)$", self.content)
        self.assertIsNotNone(match)
        seconds = int(match.group(1))
        self.assertGreater(seconds, 0)
        self.assertLess(seconds, 1800)

    def test_accuracy_sec_1min(self):
        self.assertRegex(self.content, r"(?m)^AccuracySec=1min$")

    def test_unit_points_at_backup_media_offsite_service(self):
        self.assertRegex(self.content, r"(?m)^Unit=backup-media-offsite\.service$")

    def test_wanted_by_timers_target(self):
        self.assertRegex(self.content, r"(?m)^WantedBy=timers\.target$")

    def test_no_dependency_on_other_backup_units(self):
        directives = _directives_only(self.content)
        lowered = directives.lower()
        self.assertNotIn("backup-postgres", lowered)
        self.assertNotIn("backup-offsite.service", lowered)
        self.assertNotIn("backup-offsite.timer", lowered)


# ─────────────────────────────────────────────
# Cross-file coherence and independence from O.5b/O.5c-2
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


class MediaTimerOffsetFromOffsiteBackupTests(TestCase):
    """The 30-minute offset from backup-offsite.timer is a documented
    DESIGN CHOICE, not an assumption — this re-reads backup-offsite.timer
    AND backup-postgres.timer's ACTUAL current OnCalendar= before
    asserting the media timer is correctly staggered, exactly as required
    before freezing 04:00 UTC."""

    def test_backup_offsite_timer_is_still_0330_utc(self):
        # If this ever fails, backup-media-offsite.timer's 04:00 UTC
        # offset assumption no longer holds and must be re-derived.
        content = _read(_O5C2_TIMER_PATH)
        self.assertRegex(content, r"(?m)^OnCalendar=\*-\*-\* 03:30:00 UTC$")

    def test_backup_postgres_timer_is_still_0300_utc(self):
        content = _read(_O5B_TIMER_PATH)
        self.assertRegex(content, r"(?m)^OnCalendar=\*-\*-\* 03:00:00 UTC$")

    def test_media_timer_fires_thirty_minutes_after_offsite_backup_timer(self):
        offsite_content = _read(_O5C2_TIMER_PATH)
        media_content = _read(_TIMER_PATH)
        offsite_match = re.search(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):\d{2} UTC$", offsite_content, re.MULTILINE)
        media_match = re.search(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):\d{2} UTC$", media_content, re.MULTILINE)
        self.assertIsNotNone(offsite_match)
        self.assertIsNotNone(media_match)
        offsite_minutes = int(offsite_match.group(1)) * 60 + int(offsite_match.group(2))
        media_minutes = int(media_match.group(1)) * 60 + int(media_match.group(2))
        self.assertEqual(media_minutes - offsite_minutes, 30)

    def test_media_timer_fires_after_both_postgres_timers_never_before(self):
        postgres_content = _read(_O5B_TIMER_PATH)
        offsite_content = _read(_O5C2_TIMER_PATH)
        media_content = _read(_TIMER_PATH)

        def _minutes(content):
            m = re.search(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):\d{2} UTC$", content, re.MULTILINE)
            return int(m.group(1)) * 60 + int(m.group(2))

        self.assertGreater(_minutes(media_content), _minutes(postgres_content))
        self.assertGreater(_minutes(media_content), _minutes(offsite_content))


class PriorMicroblockUnitsIntactTests(TestCase):
    """Cheap same-file self-check that O.5b/O.5c-2's units were not
    touched by this microbloque — the full guarantee is the existing
    test_o5b_backup_scheduler_systemd_units.py and
    test_o5c2_offsite_scheduler_systemd_units.py suites passing
    unchanged, run alongside this one."""

    def test_backup_postgres_service_frozen_invariants(self):
        content = _read(_REPO_ROOT / "deploy" / "systemd" / "backup-postgres.service")
        self.assertRegex(content, r"(?m)^Type=oneshot$")
        self.assertRegex(content, r"(?m)^User=trx_sim$")

    def test_backup_offsite_service_frozen_invariants(self):
        content = _read(_REPO_ROOT / "deploy" / "systemd" / "backup-offsite.service")
        self.assertRegex(content, r"(?m)^Type=oneshot$")
        match = re.search(r"^ReadWritePaths=(.+)$", content, re.MULTILINE)
        self.assertEqual(match.group(1).split(), ["/var/log/trx_sim"])


class SystemdAnalyzeOptionalTests(TestCase):
    """Additional validation IF systemd-analyze happens to be available.
    Never required — self-skips entirely otherwise."""

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

    def test_unit_install_commands_present(self):
        self.assertIn("deploy/systemd/backup-media-offsite.service", self.content)
        self.assertIn("deploy/systemd/backup-media-offsite.timer", self.content)
        self.assertIn("systemctl enable --now backup-media-offsite.timer", self.content)

    def test_verification_commands_present(self):
        self.assertIn("systemctl status backup-media-offsite.timer", self.content)
        self.assertIn("systemctl list-timers backup-media-offsite.timer", self.content)
        self.assertIn("journalctl -u backup-media-offsite.service", self.content)

    def test_media_backup_health_block_documented(self):
        self.assertIn("media_backup", self.content)

    def test_media_root_stays_private_documented(self):
        lowered = self.content.lower()
        self.assertIn("media", lowered)
        # Must not instruct adding a public Nginx location for media
        # anywhere in this file (RC-02 / O.5e-1 invariant).
        self.assertNotRegex(self.content, r"location\s+/media/\s*\{")

    def test_no_secrets_in_deploy_md(self):
        lowered = self.content.lower()
        for forbidden in _FORBIDDEN_SECRET_PATTERNS:
            self.assertNotIn(forbidden, lowered)

    def test_step_numbering_is_sequential_and_unique(self):
        step_numbers = [
            int(n) for n in re.findall(r"(?m)^### (\d+)\.", self.content)
        ]
        self.assertEqual(step_numbers, sorted(step_numbers))
        self.assertEqual(len(step_numbers), len(set(step_numbers)))


# ─────────────────────────────────────────────
# Failure propagation
# ─────────────────────────────────────────────
class ScriptFailurePropagationTests(TestCase):

    def test_exec_start_is_a_direct_bash_invocation_no_wrapper(self):
        content = _read(_SERVICE_PATH)
        match = re.search(r"(?m)^ExecStart=(.+)$", content)
        self.assertIsNotNone(match)
        self.assertEqual(
            match.group(1), "/bin/bash /opt/trx_sim/deploy/scripts/backup_media_offsite.sh",
        )

    def test_real_script_failure_exits_nonzero_as_execstart_would_invoke_it(self):
        import os
        with tempfile.TemporaryDirectory() as work:
            media_root = Path(work) / "does_not_exist"  # deliberately missing
            meta_dir = Path(work) / "meta"; meta_dir.mkdir(parents=True)

            env = dict(os.environ)
            env["MEDIA_ROOT"] = str(media_root)
            env["BACKUP_METADATA_PATH"] = str(meta_dir)
            env["RCLONE_CONFIG"] = str(Path(work) / "does_not_exist.conf")
            env["RCLONE_REMOTE"] = ""
            env["MEDIA_RCLONE_REMOTE_PATH"] = str(Path(work) / "remote")

            result = subprocess.run(
                ["bash", str(_SCRIPT_PATH)], env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
