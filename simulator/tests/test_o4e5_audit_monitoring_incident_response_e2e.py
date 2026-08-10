# simulator/tests/test_o4e5_audit_monitoring_incident_response_e2e.py
"""
Microbloque O.4e-5 — Audit Trail Monitoring + Treasury Incident
Response, End-to-End Verification + Final Checkpoint Review.

Pure verification: no production code is expected to change here.
Drives the REAL Celery tasks and the REAL /api/health/detail/ view
end-to-end, combining heartbeat + health integration + stuck-execution
escalation in the SAME scenarios where useful, to confirm the three
O.4e-2/3/4 signals coexist correctly without cross-contaminating each
other — on top of (not instead of) each block's own already-thorough
unit coverage in test_o4e2_.../test_o4e3_.../test_o4e4_...py.
"""
import ast
import inspect
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.broker_audit import (
    CELERY_BEAT_HEARTBEAT_STALE_SECONDS,
    EV_CELERY_BEAT_HEARTBEAT,
    EV_CELERY_BEAT_STALE,
    EV_TREASURY_STUCK_EXECUTION_ESCALATED,
    EV_TREASURY_STUCK_EXECUTION_OBSERVED,
    TREASURY_STUCK_EXECUTION_ESCALATION_DEDUP_WINDOW_SECONDS,
    TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS,
    inspect_celery_beat_heartbeat_staleness,
    observe_treasury_stuck_execution_escalations,
    record_celery_beat_heartbeat,
)
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest,
    Wallet, WalletTransaction,
)
from simulator.tasks import (
    observe_treasury_stuck_executions_task,
    record_celery_beat_heartbeat_task,
)
from simulator.views import health_detail_view

from .factories import make_user, make_wallet

HEALTH_DETAIL_URL = "/api/health/detail/"
RECOVERY_MIN_AGE_SECONDS = 600


# ─────────────────────────────────────────────
# Helpers (mirrors test_o4e4's own, kept file-local by convention)
# ─────────────────────────────────────────────

def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet, amount=Decimal("10.00"), reason="O.4e-5 E2E test",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by, wallet_transaction=wallet_transaction,
    )
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _started_audit_log(pk, age_seconds):
    created_at = timezone.now() - timedelta(seconds=age_seconds)
    return AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        action=f"Treasury request #{pk} execution started",
        detail={"treasury_request_id": pk},
        created_at=created_at,
    )


def _backdate_event(event, seconds_ago):
    BrokerAuditEvent.objects.filter(pk=event.pk).update(
        timestamp=timezone.now() - timedelta(seconds=seconds_ago)
    )


def _case_a_request():
    executor = make_user(username=f"o4e5_a_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    return tor


def _case_b_request():
    executor = make_user(username=f"o4e5_b_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    wallet = make_wallet(initial_balance=Decimal("50.00"))
    wtx = WalletTransaction.objects.filter(wallet=wallet).first()
    tor = _make_executing_request(wallet=wallet, executed_by=executor, wallet_transaction=wtx)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    return tor


def _case_c_request():
    executor = make_user(username=f"o4e5_c_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=100)  # under 600s
    return tor


def _case_d_request():
    executor = make_user(username=f"o4e5_d_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    return _make_executing_request(executed_by=executor)  # no STARTED event anywhere


def _case_e_request():
    executor = make_user(username=f"o4e5_e_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTED,
        action=f"Treasury request #{tor.pk} executed",
        detail={"treasury_request_id": tor.pk},
    )
    return tor


def _case_f_request():
    tor = _make_executing_request(executed_by=None)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 500)
    return tor


def _financial_counts():
    return (
        TreasuryOperationRequest.objects.count(),
        Wallet.objects.count(),
        WalletTransaction.objects.count(),
        InternalTransfer.objects.count(),
    )


def _staff_client():
    staff = make_user(username=f"o4e5_staff_{BrokerAuditEvent.objects.count()}", is_staff=True)
    client = Client()
    client.force_login(staff)
    return client


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1 — Heartbeat end-to-end
# ═══════════════════════════════════════════════════════════════════════

class HeartbeatEndToEndTests(TestCase):

    def test_heartbeat_task_registered_every_5_minutes(self):
        from celery.schedules import crontab
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["record-celery-beat-heartbeat-5m"]
        self.assertEqual(entry["task"], "simulator.record_celery_beat_heartbeat")
        self.assertEqual(entry["schedule"], crontab(minute="*/5"))

    def test_heartbeat_task_creates_persistent_evidence(self):
        result = record_celery_beat_heartbeat_task.apply().get()
        self.assertTrue(result["written"])
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_HEARTBEAT).count(), 1,
        )

    def test_inspect_missing_when_never_written(self):
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "missing")

    def test_inspect_fresh_right_after_heartbeat(self):
        record_celery_beat_heartbeat_task.apply().get()
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")

    def test_inspect_stale_past_threshold(self):
        event = record_celery_beat_heartbeat()
        _backdate_event(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "stale")

    def test_threshold_is_exactly_900_seconds(self):
        self.assertEqual(CELERY_BEAT_HEARTBEAT_STALE_SECONDS, 900)
        event = record_celery_beat_heartbeat()
        frozen_now = event.timestamp + timedelta(seconds=900)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            self.assertEqual(inspect_celery_beat_heartbeat_staleness().status, "fresh")
        frozen_now = event.timestamp + timedelta(seconds=901)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            self.assertEqual(inspect_celery_beat_heartbeat_staleness().status, "stale")

    def test_organic_business_activity_never_substitutes_heartbeat(self):
        _broker_audit.record_trade_event(event_type="position.opened", description="unrelated")
        _case_a_request()  # Treasury activity, unrelated to Beat heartbeat
        result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "missing")

    def test_redis_down_does_not_block_staleness_read_from_postgres(self):
        record_celery_beat_heartbeat()
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            result = inspect_celery_beat_heartbeat_staleness()
        self.assertEqual(result.status, "fresh")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2 — Health endpoint
# ═══════════════════════════════════════════════════════════════════════

class HealthEndpointE2ETests(TestCase):

    def test_fresh_ok_200(self):
        client = _staff_client()
        record_celery_beat_heartbeat()
        resp = client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "fresh")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_stale_degraded_503(self):
        client = _staff_client()
        event = record_celery_beat_heartbeat()
        _backdate_event(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        resp = client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "stale")
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(resp.status_code, 503)

    def test_missing_visible_but_ok_200(self):
        client = _staff_client()
        resp = client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertEqual(data["celery_beat"]["status"], "missing")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(resp.status_code, 200)

    def test_staff_totp_gate_unchanged(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)
        non_staff = make_user(username="o4e5_nonstaff")
        self.client.force_login(non_staff)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_public_health_endpoint_contract_byte_for_byte_unchanged(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})
        self.assertEqual(set(resp.json().keys()), {"status"})
        self.assertNotIn(b"celery", resp.content)

    def test_repeated_get_never_writes_broker_audit_event(self):
        client = _staff_client()
        record_celery_beat_heartbeat()
        before = BrokerAuditEvent.objects.count()
        for _ in range(5):
            client.get(HEALTH_DETAIL_URL)
        self.assertEqual(BrokerAuditEvent.objects.count(), before)

    def test_ev_celery_beat_stale_never_fabricated_from_health_even_during_escalation(self):
        # Combined scenario: a stale heartbeat AND an escalating Treasury
        # request at the same time — health GETs must still never write
        # EV_CELERY_BEAT_STALE, regardless of what else is happening.
        client = _staff_client()
        event = record_celery_beat_heartbeat()
        _backdate_event(event, CELERY_BEAT_HEARTBEAT_STALE_SECONDS + 1)
        tor = _case_a_request()
        obs = _broker_audit.record_payment_event(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, severity=_broker_audit.Severity.WARNING,
            actor_type=_broker_audit.ActorType.SYSTEM, description="test",
            metadata={"treasury_operation_request_id": tor.pk, "case": "CASE_A"},
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 1)
        observe_treasury_stuck_execution_escalations()

        for _ in range(3):
            client.get(HEALTH_DETAIL_URL)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_CELERY_BEAT_STALE).count(), 0)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3 — Treasury stuck escalation E2E (real scenarios per case)
# ═══════════════════════════════════════════════════════════════════════

class StuckEscalationE2ETests(TestCase):

    def _run_full_task(self):
        return observe_treasury_stuck_executions_task.apply().get()

    def test_case_a_full_pipeline_observes_then_escalates(self):
        tor = _case_a_request()
        result = self._run_full_task()
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["escalated"], 0)  # just observed, not persisted yet

        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 1)

        result = self._run_full_task()
        self.assertEqual(result["escalated"], 1)
        self.assertTrue(
            BrokerAuditEvent.objects.filter(
                event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED, metadata__treasury_operation_request_id=tor.pk,
            ).exists()
        )

    def _observe_then_backdate_and_escalate(self, tor, seconds_past_threshold=1):
        self._run_full_task()
        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + seconds_past_threshold)
        return self._run_full_task()

    def test_case_b_escalates(self):
        tor = _case_b_request()
        result = self._observe_then_backdate_and_escalate(tor)
        self.assertEqual(result["escalated"], 1)

    def test_case_d_escalates(self):
        tor = _case_d_request()
        result = self._observe_then_backdate_and_escalate(tor)
        self.assertEqual(result["escalated"], 1)

    def test_case_e_escalates(self):
        tor = _case_e_request()
        result = self._observe_then_backdate_and_escalate(tor)
        self.assertEqual(result["escalated"], 1)

    def test_case_f_escalates(self):
        tor = _case_f_request()
        result = self._observe_then_backdate_and_escalate(tor)
        self.assertEqual(result["escalated"], 1)

    def test_case_c_never_escalates_even_after_much_time(self):
        tor = _case_c_request()
        for _ in range(3):
            result = self._run_full_task()
            self.assertEqual(result["written"], 0)  # CASE_C never observed
            self.assertEqual(result["escalated"], 0)
        self.assertFalse(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).exists()
        )

    def test_persistence_below_threshold_does_not_escalate(self):
        tor = _case_a_request()
        self._run_full_task()
        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS - 1)
        result = self._run_full_task()
        self.assertEqual(result["escalated"], 0)

    def test_persistence_exactly_at_threshold_escalates(self):
        tor = _case_a_request()
        self._run_full_task()
        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        frozen_now = obs.timestamp + timedelta(seconds=TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS)
        with patch("simulator.broker_audit.timezone.now", return_value=frozen_now):
            result = observe_treasury_stuck_execution_escalations()
        self.assertEqual(result, 1)

    def test_continued_persistence_dedup_prevents_flooding(self):
        tor = _case_a_request()
        result = self._observe_then_backdate_and_escalate(tor)
        self.assertEqual(result["escalated"], 1)
        for _ in range(5):
            result = self._run_full_task()
            self.assertEqual(result["escalated"], 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED, metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            1,
        )

    def test_re_escalates_after_dedup_window_if_still_executing(self):
        tor = _case_a_request()
        self._observe_then_backdate_and_escalate(tor)
        escalation = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        _backdate_event(escalation, TREASURY_STUCK_EXECUTION_ESCALATION_DEDUP_WINDOW_SECONDS + 1)

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)  # still stuck

        result = self._run_full_task()
        self.assertEqual(result["escalated"], 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 2,
        )

    def test_case_d_persisted_seconds_derives_from_first_observed_not_age(self):
        from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution
        tor = _case_d_request()
        self._run_full_task()
        candidates = {c.instance.pk: c for c in inspect_stuck_treasury_execution()}
        self.assertIsNone(candidates[tor.pk].age_seconds)

        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 1)
        obs.refresh_from_db()  # local instance was stale after the queryset .update()
        result = self._run_full_task()
        self.assertEqual(result["escalated"], 1)
        escalation = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        self.assertEqual(escalation.metadata["first_observed_at"], obs.timestamp.isoformat())


# ═══════════════════════════════════════════════════════════════════════
# SECTION 4 — Escalation event shape / logger / Sentry absence
# ═══════════════════════════════════════════════════════════════════════

class EscalationSignalTests(TestCase):

    def _escalate(self, tor):
        observe_treasury_stuck_executions_task.apply().get()
        obs = BrokerAuditEvent.objects.get(
            event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
        )
        _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 1)

    def test_event_shape_and_severity(self):
        tor = _case_a_request()
        self._escalate(tor)
        observe_treasury_stuck_executions_task.apply().get()
        event = BrokerAuditEvent.objects.get(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED)
        self.assertEqual(event.severity, _broker_audit.Severity.CRITICAL)
        self.assertEqual(event.metadata["treasury_operation_request_id"], tor.pk)
        self.assertIn("persisted_seconds", event.metadata)
        self.assertIn("escalation_threshold_seconds", event.metadata)

    def test_logger_error_only_on_actual_escalation(self):
        tor = _case_a_request()
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_executions_task.apply().get()  # just observes
        mock_error.assert_not_called()

        self._escalate(tor)
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_executions_task.apply().get()  # now escalates
        mock_error.assert_called_once()

    def test_dedup_prevents_repeated_logger_error(self):
        tor = _case_a_request()
        self._escalate(tor)
        observe_treasury_stuck_executions_task.apply().get()  # first escalation
        with patch("simulator.broker_audit.log.error") as mock_error:
            observe_treasury_stuck_executions_task.apply().get()  # deduped
        mock_error.assert_not_called()

    def test_missing_sentry_dsn_does_not_break_the_flow(self):
        from django.conf import settings
        self.assertEqual(getattr(settings, "SENTRY_DSN", ""), "")  # test env has none
        tor = _case_a_request()
        self._escalate(tor)
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result["escalated"], 1)

    def test_no_sentry_sdk_capture_message_anywhere_in_o4e(self):
        import simulator.broker_audit as ba, simulator.tasks as tk, simulator.views as vw
        for module in (ba, tk, vw):
            source = inspect.getsource(module)
            self.assertNotIn("capture_message", source)
            self.assertNotIn("capture_event", source)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 5 — No automatic recovery: AST + runtime, combined sweep
# ═══════════════════════════════════════════════════════════════════════

class NoAutomaticRecoveryTests(SimpleTestCase):

    _FORBIDDEN_CALLS = {
        "credit_wallet", "debit_wallet", "reconcile_wallet",
        "transfer_to_account", "transfer_to_wallet",
        "execute_treasury_request", "mark_treasury_execution_failed",
    }
    _FORBIDDEN_IMPORTS = {"wallet_ledger"}

    _O4E_FUNCTIONS = [
        _broker_audit.record_celery_beat_heartbeat,
        _broker_audit.inspect_celery_beat_heartbeat_staleness,
        _broker_audit.observe_treasury_stuck_execution_escalations,
        _broker_audit._record_treasury_stuck_execution_escalation,
        record_celery_beat_heartbeat_task,
        observe_treasury_stuck_executions_task,
        health_detail_view,
    ]

    def test_no_o4e_function_imports_or_calls_financial_mutators(self):
        for fn in self._O4E_FUNCTIONS:
            with self.subTest(function=fn.__name__):
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
                self.assertFalse(self._FORBIDDEN_CALLS & called, f"{fn.__name__}: found {self._FORBIDDEN_CALLS & called}")
                self.assertFalse(self._FORBIDDEN_IMPORTS & imported, f"{fn.__name__}: found {self._FORBIDDEN_IMPORTS & imported}")


class NoAutomaticRecoveryRuntimeTests(TestCase):
    """Runtime confirmation, not just AST: patch the real financial
    mutators and prove a full combined incident (all 5 escalating cases
    + heartbeat + health checks) never touches them, then confirm zero
    net financial change."""

    def test_full_combined_incident_never_touches_financial_mutators(self):
        requests = [_case_a_request(), _case_b_request(), _case_d_request(),
                    _case_e_request(), _case_f_request(), _case_c_request()]

        # Captured AFTER test-fixture setup — the invariant under test is
        # "the monitor/escalation code introduces zero ADDITIONAL
        # financial mutation", not "nothing exists in the DB at all".
        before = _financial_counts()

        with patch("simulator.treasury_execution_recovery.mark_treasury_execution_failed") as m1, \
             patch("simulator.wallet_ledger.credit_wallet") as m2, \
             patch("simulator.wallet_ledger.debit_wallet") as m3:

            client = _staff_client()
            record_celery_beat_heartbeat_task.apply().get()
            observe_treasury_stuck_executions_task.apply().get()

            for tor in requests:
                obs = BrokerAuditEvent.objects.filter(
                    event_type=EV_TREASURY_STUCK_EXECUTION_OBSERVED, metadata__treasury_operation_request_id=tor.pk,
                ).first()
                if obs:
                    _backdate_event(obs, TREASURY_STUCK_EXECUTION_ESCALATION_THRESHOLD_SECONDS + 1)

            observe_treasury_stuck_executions_task.apply().get()
            client.get(HEALTH_DETAIL_URL)

            m1.assert_not_called()
            m2.assert_not_called()
            m3.assert_not_called()

        after = _financial_counts()
        self.assertEqual(before, after)

        for tor in requests:
            tor.refresh_from_db()
            self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

        # Confirms the incident actually exercised the escalation path
        # for the cases expected to (sanity — a no-op scenario would
        # trivially "pass" the assertions above without proving anything).
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_TREASURY_STUCK_EXECUTION_ESCALATED).count(), 5,
        )
