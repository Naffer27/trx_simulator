# simulator/tests/test_o4e2_celery_beat_heartbeat_foundation.py
"""
Microbloque O.4e-2 — Celery Beat Heartbeat Staleness Foundation
(closes half of HIGH-4, signal "A" only — see the O.4e Fase 0 report
and docs/TREASURY_INCIDENT_RUNBOOK.md §2 for the A/B split this
deliberately keeps separate).

Covers ONLY:
  - EV_CELERY_BEAT_HEARTBEAT / EV_CELERY_BEAT_STALE catalog constants
    (the latter is catalog-only in this block — no call site writes it
    yet, confirmed here structurally).
  - record_celery_beat_heartbeat() — writes exactly one row per call,
    no dedup, fail-open via record_event()'s own unmodified contract.
  - inspect_celery_beat_heartbeat_staleness() — pure read, deterministic
    fresh/stale/missing output.
  - simulator.record_celery_beat_heartbeat Celery task + its Beat
    schedule entry.

Does NOT touch, wrap, or test: /api/health/detail/, /api/metrics/,
Sentry, Treasury escalation, record_event()'s or log_audit()'s own
fail-open contract (unmodified, not re-tested here), or any financial
workflow.
"""
import ast
import inspect
import pathlib
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from simulator import broker_audit as _audit
from simulator.broker_audit import (
    CELERY_BEAT_HEARTBEAT_INTERVAL_SECONDS,
    CELERY_BEAT_HEARTBEAT_STALE_SECONDS,
    EV_CELERY_BEAT_HEARTBEAT,
    EV_CELERY_BEAT_STALE,
    inspect_celery_beat_heartbeat_staleness,
    record_celery_beat_heartbeat,
)
from simulator.models import (
    BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest, Wallet,
    WalletTransaction,
)
from simulator.tasks import record_celery_beat_heartbeat_task


# ─────────────────────────────────────────────
# Event catalog
# ─────────────────────────────────────────────

class EventCatalogTests(SimpleTestCase):

    def test_heartbeat_constant_value(self):
        self.assertEqual(EV_CELERY_BEAT_HEARTBEAT, "system.celery_beat_heartbeat")

    def test_stale_constant_value(self):
        self.assertEqual(EV_CELERY_BEAT_STALE, "system.celery_beat_stale")

    def test_constants_are_distinct(self):
        self.assertNotEqual(EV_CELERY_BEAT_HEARTBEAT, EV_CELERY_BEAT_STALE)

    def test_default_interval_is_300_seconds(self):
        self.assertEqual(CELERY_BEAT_HEARTBEAT_INTERVAL_SECONDS, 300)

    def test_default_stale_threshold_is_900_seconds(self):
        self.assertEqual(CELERY_BEAT_HEARTBEAT_STALE_SECONDS, 900)

    def test_stale_threshold_is_three_times_the_interval(self):
        self.assertEqual(
            CELERY_BEAT_HEARTBEAT_STALE_SECONDS,
            3 * CELERY_BEAT_HEARTBEAT_INTERVAL_SECONDS,
        )

    def test_prior_event_catalog_values_untouched(self):
        # Regression: adding this block's constants must not have
        # altered any pre-existing event_type string.
        self.assertEqual(_audit.EV_RISK_ALERT_OBSERVED, "risk.alert_observed")
        self.assertEqual(
            _audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED,
            "treasury.stuck_execution_observed",
        )
        self.assertEqual(_audit.EV_AUTH_RATE_LIMITED, "auth.rate_limited")

    def test_stale_event_has_no_call_site_yet(self):
        """
        O.4e-2 scope: EV_CELERY_BEAT_STALE is catalog-only. Grep-based
        (no behavior exists yet to test) — mirrors the precedent already
        used by test_o3d1_...::NoProductiveUseYetTests for the same
        "constant declared, not wired yet" shape.
        """
        simulator_dir = pathlib.Path(_audit.__file__).resolve().parent
        hits = []
        for path in simulator_dir.rglob("*.py"):
            if "tests" in path.parts or path.name == "broker_audit.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "EV_CELERY_BEAT_STALE" in text:
                hits.append(str(path))
        self.assertEqual(hits, [])


# ─────────────────────────────────────────────
# record_celery_beat_heartbeat()
# ─────────────────────────────────────────────

class RecordHeartbeatTests(TestCase):

    def test_writes_exactly_one_row(self):
        record_celery_beat_heartbeat()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_HEARTBEAT).count(), 1,
        )

    def test_two_calls_write_two_rows_no_dedup(self):
        record_celery_beat_heartbeat()
        record_celery_beat_heartbeat()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_HEARTBEAT).count(), 2,
        )

    def test_event_shape(self):
        event = record_celery_beat_heartbeat()
        self.assertEqual(event.event_type, EV_CELERY_BEAT_HEARTBEAT)
        self.assertEqual(event.category, _audit.Category.SYSTEM)
        self.assertEqual(event.severity, _audit.Severity.INFO)
        self.assertEqual(event.actor_type, _audit.ActorType.SYSTEM)
        self.assertIsNone(event.actor_id)

    def test_no_financial_side_effects(self):
        before = (
            TreasuryOperationRequest.objects.count(),
            Wallet.objects.count(),
            WalletTransaction.objects.count(),
            InternalTransfer.objects.count(),
        )
        record_celery_beat_heartbeat()
        after = (
            TreasuryOperationRequest.objects.count(),
            Wallet.objects.count(),
            WalletTransaction.objects.count(),
            InternalTransfer.objects.count(),
        )
        self.assertEqual(before, after)

    def test_fail_open_on_write_failure(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            result = record_celery_beat_heartbeat()
        self.assertIsNone(result)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_HEARTBEAT).count(), 0,
        )


# ─────────────────────────────────────────────
# inspect_celery_beat_heartbeat_staleness() — pure read
# ─────────────────────────────────────────────

class StalenessInspectionTests(TestCase):

    def _backdate(self, event, seconds_ago):
        BrokerAuditEvent.objects.filter(pk=event.pk).update(
            timestamp=timezone.now() - timezone.timedelta(seconds=seconds_ago)
        )

    def test_missing_when_no_heartbeat_ever_written(self):
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.last_heartbeat_at)
        self.assertIsNone(result.age_seconds)
        self.assertEqual(result.stale_after_seconds, CELERY_BEAT_HEARTBEAT_STALE_SECONDS)

    def test_fresh_when_heartbeat_recent(self):
        record_celery_beat_heartbeat()
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")
        self.assertIsNotNone(result.last_heartbeat_at)
        self.assertLess(result.age_seconds, 5)

    def test_stale_when_heartbeat_older_than_threshold(self):
        event = record_celery_beat_heartbeat()
        self._backdate(event, 901)
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "stale")

    def test_exact_threshold_boundary_is_still_fresh(self):
        # Real wall-clock backdating can't hit an exact boundary (time
        # keeps passing between the backdate and the read) — freeze
        # timezone.now() as read by the inspection function itself for
        # a deterministic exact-equality check: age == threshold is
        # "fresh" (strict > is what flips it to "stale").
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=CELERY_BEAT_HEARTBEAT_STALE_SECONDS)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")

    def test_one_second_past_threshold_is_stale(self):
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "stale")

    def test_custom_threshold_override(self):
        event = record_celery_beat_heartbeat()
        self._backdate(event, 61)
        result = inspect_celery_beat_heartbeat_staleness(stale_after_seconds=60)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.stale_after_seconds, 60)

    def test_only_the_most_recent_heartbeat_counts(self):
        old_event = record_celery_beat_heartbeat()
        self._backdate(old_event, 2000)
        record_celery_beat_heartbeat()  # fresh one, written second
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")

    def test_organic_business_activity_does_not_substitute_heartbeat(self):
        # Writing OTHER BrokerAuditEvent types (organic trading/auth
        # events) must never be mistaken for a heartbeat — staleness is
        # keyed strictly on EV_CELERY_BEAT_HEARTBEAT, nothing else. A
        # quiet system with zero heartbeats but plenty of organic
        # activity must still read "missing", never "fresh".
        _audit.record_trade_event(
            event_type="position.opened", description="unrelated organic event",
        )
        _audit.record_auth_event(
            event_type=_audit.EV_ADMIN_SITE_LOGIN_SUCCESS, description="unrelated organic event",
        )
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "missing")

    def test_redis_down_does_not_affect_staleness_read(self):
        record_celery_beat_heartbeat()
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")

    def test_inspection_never_writes(self):
        record_celery_beat_heartbeat()
        with patch("simulator.models.BrokerAuditEvent.objects.create") as mock_create:
            inspect_celery_beat_heartbeat_staleness()
        mock_create.assert_not_called()

    def test_inspection_causes_no_financial_side_effects(self):
        record_celery_beat_heartbeat()
        before = (
            TreasuryOperationRequest.objects.count(),
            Wallet.objects.count(),
            WalletTransaction.objects.count(),
            InternalTransfer.objects.count(),
        )
        inspect_celery_beat_heartbeat_staleness()
        after = (
            TreasuryOperationRequest.objects.count(),
            Wallet.objects.count(),
            WalletTransaction.objects.count(),
            InternalTransfer.objects.count(),
        )
        self.assertEqual(before, after)

    def test_deterministic_age_computation_with_frozen_now(self):
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=123)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            result = inspect_celery_beat_heartbeat_staleness()
        self.assertAlmostEqual(result.age_seconds, 123, delta=1)

    def test_returns_namedtuple_with_exactly_four_fields(self):
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(
            result._fields, ("status", "last_heartbeat_at", "age_seconds", "stale_after_seconds"),
        )


# ─────────────────────────────────────────────
# AST — structural invariants (no financial imports/calls, read-only)
# ─────────────────────────────────────────────

class ScopeAndSafetyTests(SimpleTestCase):

    _FORBIDDEN_CALLS = {
        "credit_wallet", "debit_wallet", "reconcile_wallet",
        "transfer_to_account", "transfer_to_wallet",
        "execute_treasury_request", "mark_treasury_execution_failed",
    }
    _FORBIDDEN_IMPORTS = {"wallet_ledger", "treasury_execution_recovery"}

    def _walk(self, fn):
        tree = ast.parse(inspect.getsource(fn))
        imported, called = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.rsplit(".", 1)[-1])
                imported.update(a.name for a in node.names)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)
        return imported, called

    def test_record_heartbeat_has_no_financial_imports_or_calls(self):
        imported, called = self._walk(record_celery_beat_heartbeat)
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")

    def test_inspection_has_no_financial_imports_or_calls(self):
        imported, called = self._walk(inspect_celery_beat_heartbeat_staleness)
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")

    def test_inspection_never_calls_create_update_or_delete(self):
        _, called = self._walk(inspect_celery_beat_heartbeat_staleness)
        self.assertNotIn("create", called)
        self.assertNotIn("update", called)
        self.assertNotIn("delete", called)
        self.assertNotIn("bulk_create", called)

    def test_task_has_no_financial_imports_or_calls(self):
        imported, called = self._walk(record_celery_beat_heartbeat_task)
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")


# ─────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────

class HeartbeatTaskTests(TestCase):

    def test_task_writes_one_heartbeat_and_reports_written_true(self):
        result = record_celery_beat_heartbeat_task.apply().get()
        self.assertEqual(result["written"], True)
        self.assertIn("elapsed_ms", result)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_HEARTBEAT).count(), 1,
        )

    def test_task_reports_written_false_on_underlying_failure(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            result = record_celery_beat_heartbeat_task.apply().get()
        self.assertEqual(result["written"], False)

    def test_task_registered_under_expected_name(self):
        self.assertEqual(record_celery_beat_heartbeat_task.name, "simulator.record_celery_beat_heartbeat")


# ─────────────────────────────────────────────
# Beat schedule
# ─────────────────────────────────────────────

class BeatScheduleTests(SimpleTestCase):

    def test_beat_schedule_registers_the_periodic_task(self):
        from django.conf import settings
        matching = [
            v for v in settings.CELERY_BEAT_SCHEDULE.values()
            if v["task"] == "simulator.record_celery_beat_heartbeat"
        ]
        self.assertEqual(len(matching), 1)

    def test_cadence_is_exactly_every_5_minutes(self):
        from celery.schedules import crontab
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["record-celery-beat-heartbeat-5m"]
        self.assertEqual(entry["schedule"], crontab(minute="*/5"))

    def test_expires_option_is_just_under_the_tick_interval(self):
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["record-celery-beat-heartbeat-5m"]
        self.assertEqual(entry["options"]["expires"], 4 * 60)

    def test_pre_existing_entries_untouched(self):
        from celery.schedules import crontab
        from django.conf import settings
        schedule = settings.CELERY_BEAT_SCHEDULE
        self.assertEqual(schedule["beat-heartbeat-5m"]["task"], "simulator.ping")
        self.assertEqual(
            schedule["observe-treasury-stuck-executions-15m"]["task"],
            "simulator.observe_treasury_stuck_executions",
        )
        self.assertEqual(
            schedule["observe-treasury-stuck-executions-15m"]["schedule"], crontab(minute="*/15"),
        )

    def test_distinct_from_pre_existing_ping_heartbeat_entry(self):
        from django.conf import settings
        schedule = settings.CELERY_BEAT_SCHEDULE
        self.assertNotEqual(
            schedule["beat-heartbeat-5m"]["task"],
            schedule["record-celery-beat-heartbeat-5m"]["task"],
        )
