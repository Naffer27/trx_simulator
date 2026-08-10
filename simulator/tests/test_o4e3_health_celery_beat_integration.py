# simulator/tests/test_o4e3_health_celery_beat_integration.py
"""
Microbloque O.4e-3 — Production Health Integration for Celery Beat
Heartbeat (closes the health/metrics half of HIGH-4, together with
O.4e-2's foundation).

Integrates ONLY inspect_celery_beat_heartbeat_staleness() (O.4e-2,
unmodified) into GET /api/health/detail/ — a strictly additional
"celery_beat" section. Frozen decision (confirmed before
implementation): "stale" degrades the endpoint's overall status/HTTP
code exactly like a db/redis error; "missing" does not (avoids a false
positive in the first ~5 minutes after a fresh deploy, before Beat's
first tick lands).

Does NOT touch /api/health/ (the public liveness probe — its own
test_health_check.py::PublicHealthCheckTests locks its contract to
{"status"} only and explicitly forbids the word "celery" in its body;
untouched here), record_event(), log_audit(),
record_celery_beat_heartbeat(), or any Treasury/Wallet workflow.
"""
import ast
import inspect
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from simulator import broker_audit as _audit
from simulator.broker_audit import (
    CELERY_BEAT_HEARTBEAT_STALE_SECONDS,
    EV_CELERY_BEAT_HEARTBEAT,
    EV_CELERY_BEAT_STALE,
    record_celery_beat_heartbeat,
)
from simulator.models import (
    BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest, Wallet,
    WalletTransaction,
)
from simulator.tests.factories import make_user
from simulator.views import health_detail_view

HEALTH_DETAIL_URL = "/api/health/detail/"


def _backdate(event, seconds_ago):
    BrokerAuditEvent.objects.filter(pk=event.pk).update(
        timestamp=timezone.now() - timezone.timedelta(seconds=seconds_ago)
    )


class _StaffClientMixin:
    def setUp(self):
        self.staff = make_user(username=f"o4e3_staff_{id(self)}")
        self.staff.is_staff = True
        self.staff.save()
        self.client.force_login(self.staff)
        super().setUp()


# ─────────────────────────────────────────────
# 1-3, 12 — status reflection: fresh / stale / missing
# ─────────────────────────────────────────────

class StatusReflectionTests(_StaffClientMixin, TestCase):

    def test_fresh_heartbeat_reflected_and_does_not_degrade(self):
        record_celery_beat_heartbeat()
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "fresh")
        self.assertEqual(data["celery_beat"]["threshold_seconds"], CELERY_BEAT_HEARTBEAT_STALE_SECONDS)
        # fresh must not by itself force degraded, given db/redis are fine
        # in the test environment.
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_stale_heartbeat_reflected_and_degrades_overall_status(self):
        event = record_celery_beat_heartbeat()
        _backdate(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "stale")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_missing_heartbeat_reflected_but_does_not_degrade(self):
        # No heartbeat ever written — frozen decision: "missing" must NOT
        # force degraded/503 by itself (fresh-deploy grace period).
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "missing")
        self.assertIsNone(data["celery_beat"]["age_seconds"])
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_missing_does_not_regress_pre_existing_db_redis_only_contract(self):
        # Prior contract (O.4e-2 and earlier): with db/redis both healthy
        # and nothing else checked, status was "ok"/200. Confirms adding
        # celery_beat (as "missing") does not silently regress that.
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["db"]["status"], "ok")
        self.assertEqual(data["redis"]["status"], "ok")
        self.assertEqual(data["status"], "ok")


# ─────────────────────────────────────────────
# 4-5 — threshold boundary + deterministic age
# ─────────────────────────────────────────────

class ThresholdAndAgeTests(_StaffClientMixin, TestCase):

    def test_exact_threshold_boundary_is_still_fresh(self):
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=CELERY_BEAT_HEARTBEAT_STALE_SECONDS)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.json()["celery_beat"]["status"], "fresh")
        self.assertEqual(resp.status_code, 200)

    def test_one_second_past_threshold_is_stale(self):
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.json()["celery_beat"]["status"], "stale")
        self.assertEqual(resp.status_code, 503)

    def test_age_seconds_is_deterministic(self):
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timezone.timedelta(seconds=123)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertAlmostEqual(resp.json()["celery_beat"]["age_seconds"], 123, delta=1)


# ─────────────────────────────────────────────
# 6-8 — GET is strictly read-only
# ─────────────────────────────────────────────

class ReadOnlyTests(_StaffClientMixin, TestCase):

    def test_repeated_get_creates_no_broker_audit_event(self):
        record_celery_beat_heartbeat()
        before = BrokerAuditEvent.objects.count()
        for _ in range(5):
            self.client.get(HEALTH_DETAIL_URL)
        after = BrokerAuditEvent.objects.count()
        self.assertEqual(before, after)

    def test_repeated_get_does_not_touch_the_heartbeat_row(self):
        event = record_celery_beat_heartbeat()
        original_timestamp = event.timestamp
        for _ in range(5):
            self.client.get(HEALTH_DETAIL_URL)
        event.refresh_from_db()
        self.assertEqual(event.timestamp, original_timestamp)

    def test_stale_response_never_writes_ev_celery_beat_stale(self):
        event = record_celery_beat_heartbeat()
        _backdate(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        for _ in range(3):
            resp = self.client.get(HEALTH_DETAIL_URL)
            self.assertEqual(resp.json()["celery_beat"]["status"], "stale")
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_STALE).count(), 0,
        )

    def test_missing_response_never_writes_anything(self):
        for _ in range(3):
            self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)


# ─────────────────────────────────────────────
# 9 — organic activity is not a heartbeat substitute
# ─────────────────────────────────────────────

class OrganicActivityTests(_StaffClientMixin, TestCase):

    def test_organic_events_do_not_produce_false_fresh(self):
        _audit.record_trade_event(event_type="position.opened", description="unrelated")
        _audit.record_auth_event(
            event_type=_audit.EV_ADMIN_SITE_LOGIN_SUCCESS, description="unrelated",
        )
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.json()["celery_beat"]["status"], "missing")


# ─────────────────────────────────────────────
# 10 — Redis down does not block reading the PostgreSQL-backed heartbeat
# ─────────────────────────────────────────────

class RedisDownTests(_StaffClientMixin, TestCase):

    def test_redis_down_celery_beat_still_reads_from_postgres(self):
        record_celery_beat_heartbeat()
        with patch("redis.from_url", side_effect=Exception("redis down")):
            resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        # The pre-existing "redis" check correctly reports the outage —
        # already-existing behavior, not touched by this block.
        self.assertEqual(data["redis"]["status"], "error")
        # But celery_beat, being Postgres-backed, is entirely unaffected.
        self.assertEqual(data["celery_beat"]["status"], "fresh")
        # Overall status is still "degraded" — because of redis, not
        # because of celery_beat.
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)


# ─────────────────────────────────────────────
# 11 — no financial mutation
# ─────────────────────────────────────────────

class NoFinancialMutationTests(_StaffClientMixin, TestCase):

    def test_no_financial_side_effects_across_all_states(self):
        def _counts():
            return (
                TreasuryOperationRequest.objects.count(),
                Wallet.objects.count(),
                WalletTransaction.objects.count(),
                InternalTransfer.objects.count(),
            )

        before = _counts()
        self.client.get(HEALTH_DETAIL_URL)  # missing
        record_celery_beat_heartbeat()
        self.client.get(HEALTH_DETAIL_URL)  # fresh
        event = BrokerAuditEvent.objects.get(event_type=EV_CELERY_BEAT_HEARTBEAT)
        _backdate(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        self.client.get(HEALTH_DETAIL_URL)  # stale
        after = _counts()
        self.assertEqual(before, after)


# ─────────────────────────────────────────────
# 13 — authentication/permissions unchanged (regression)
# ─────────────────────────────────────────────

class AuthRegressionTests(TestCase):

    def test_anonymous_still_forbidden(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_still_forbidden(self):
        user = make_user(username="o4e3_nonstaff")
        self.client.force_login(user)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_staff_still_allowed(self):
        staff = make_user(username="o4e3_staff_regress", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIn(resp.status_code, (200, 503))

    def test_db_and_redis_keys_still_present(self):
        staff = make_user(username="o4e3_staff_keys", is_staff=True)
        self.client.force_login(staff)
        data = self.client.get(HEALTH_DETAIL_URL).json()
        self.assertIn("db", data)
        self.assertIn("redis", data)
        self.assertIn("channel_layer", data)


# ─────────────────────────────────────────────
# 14 — AST: health_detail_view imports nothing financial
# ─────────────────────────────────────────────

class ScopeAndSafetyTests(TestCase):

    _FORBIDDEN_CALLS = {
        "credit_wallet", "debit_wallet", "reconcile_wallet",
        "transfer_to_account", "transfer_to_wallet",
        "execute_treasury_request", "mark_treasury_execution_failed",
        "record_celery_beat_heartbeat",
    }
    _FORBIDDEN_IMPORTS = {"wallet_ledger", "treasury_execution_recovery"}

    def test_ast_confirms_no_financial_imports_or_calls(self):
        tree = ast.parse(inspect.getsource(health_detail_view))
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
        self.assertFalse(self._FORBIDDEN_CALLS & called, f"found: {self._FORBIDDEN_CALLS & called}")
        self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"found: {self._FORBIDDEN_IMPORTS & imported}")

    def test_only_reads_via_inspect_function_never_the_writer(self):
        source = inspect.getsource(health_detail_view)
        self.assertIn("inspect_celery_beat_heartbeat_staleness", source)
        self.assertNotIn("record_celery_beat_heartbeat(", source)
