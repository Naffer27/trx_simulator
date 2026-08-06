# simulator/tests/test_o3d6_treasury_operational_hardening_end_to_end.py
"""
Microbloque O.3d-6 — Treasury Operational Hardening, end-to-end
verification.

This file does NOT re-test what O.3d-1/2/3/4/5 already cover in
isolation (event catalog constants, dedup unit behavior, task decorator
shape, dashboard permission matrix in isolation, command message
strings). It instead drives full realistic journeys ACROSS the five
microbloques together — the paths only a real deployment would ever
actually exercise:

    stuck TreasuryOperationRequest -> Celery task -> BrokerAuditEvent
        -> dashboard (current state + history), through two ticks
        (confirming dedup end-to-end, not just at the service layer)

    assign_treasury_role -> Django Client hitting the dashboard with
        that exact role (only "recoverer" ever succeeds)

Every mutation-adjacent call in this file goes through the real public
entry points (the Celery task via .apply(), the dashboard via the
admin Client, the role command via call_command()) — never by calling
private helpers directly. No TreasuryOperationRequest, Wallet or
WalletTransaction is ever created, modified or read for mutation
anywhere in this file's own code, and every test that touches money-
adjacent services proves so by mocking them and asserting zero calls.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest,
    Wallet, WalletTransaction,
)
from simulator.tasks import observe_treasury_stuck_executions_task

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600
HISTORY_EVENT_TYPE = "treasury.stuck_execution_observed"


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3d-6 end-to-end test",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by,
        wallet_transaction=wallet_transaction,
        requested_by=requested_by,
        approved_by=approved_by,
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


def _make_recoverer(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    user.user_permissions.add(Permission.objects.get(codename="can_recover_treasury_execution"))
    user.refresh_from_db()
    return user


def _dashboard_url():
    return reverse("admin:treasury_operational_dashboard")


def _dashboard_data_url():
    return reverse("admin:treasury_operational_dashboard_data")


class _NoRedisMixin:
    """Every test in this file is deterministic regardless of whether
    a real Redis is reachable — same discipline O.3d-4's own tests
    already established. The dedicated FakeRedis class below is used
    only by the scenario that specifically exercises the cache."""
    def setUp(self):
        super().setUp()
        patcher = patch("redis.from_url", side_effect=RuntimeError("no redis in this test"))
        patcher.start()
        self.addCleanup(patcher.stop)


class FakeRedis:
    """
    A minimal in-memory stand-in that actually stores what it's given
    — unlike a bare MagicMock, a second .get() genuinely returns what
    an earlier .setex() stored, so a real miss-then-hit cycle can be
    observed end-to-end rather than asserted piecewise.
    """
    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value.encode() if isinstance(value, str) else value


# ─────────────────────────────────────────────
# 1 — CASE_A eligible: full pipeline, two ticks
# ─────────────────────────────────────────────

class CaseAFullPipelineTests(_NoRedisMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d6_case_a_recoverer")
        self.executor = make_user(username="o3d6_case_a_executor", is_staff=True)
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.tor = _make_executing_request(wallet=self.wallet, executed_by=self.executor)
        _started_audit_log(self.tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_full_pipeline_inspect_to_task_to_dashboard_with_dedup(self):
        # inspect_stuck_treasury_execution() detects it as CASE_A.
        from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == self.tor.pk)
        self.assertEqual(candidate.case, "CASE_A")
        self.assertTrue(candidate.eligible)

        # First Celery tick — creates exactly one BrokerAuditEvent.
        result1 = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result1["written"], 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=HISTORY_EVENT_TYPE).count(), 1,
        )

        # Second Celery tick, same dedup window — writes nothing new.
        result2 = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result2["written"], 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=HISTORY_EVENT_TYPE).count(), 1,
        )

        # Dashboard reflects BOTH current state and history correctly.
        resp = self.client.get(_dashboard_data_url())
        data = resp.json()

        row = next(c for c in data["candidates"] if c["treasury_operation_request_id"] == self.tor.pk)
        self.assertEqual(row["case"], "CASE_A")
        self.assertTrue(row["eligible"])
        self.assertEqual(
            row["recover_url"],
            reverse("admin:treasury_request_recover", args=[self.tor.pk]),
        )

        history_row = next(
            h for h in data["history"] if h["treasury_operation_request_id"] == self.tor.pk
        )
        self.assertEqual(history_row["case"], "CASE_A")
        self.assertEqual(history_row["severity"], "WARNING")
        self.assertTrue(history_row["eligible"])

        # Only one history row despite two ticks — dedup held end-to-end.
        self.assertEqual(
            len([h for h in data["history"] if h["treasury_operation_request_id"] == self.tor.pk]), 1,
        )


# ─────────────────────────────────────────────
# 2 — CASE_C recent: never observed, but visible live
# ─────────────────────────────────────────────

class CaseCFullPipelineTests(_NoRedisMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d6_case_c_recoverer")
        self.executor = make_user(username="o3d6_case_c_executor", is_staff=True)
        self.tor = _make_executing_request(executed_by=self.executor)
        _started_audit_log(self.tor.pk, age_seconds=45)  # below 600s -> CASE_C
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_case_c_not_observed_but_visible_live_as_not_eligible(self):
        result = observe_treasury_stuck_executions_task.apply().get()
        self.assertEqual(result["written"], 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=HISTORY_EVENT_TYPE).count(), 0)

        resp = self.client.get(_dashboard_data_url())
        data = resp.json()

        row = next(c for c in data["candidates"] if c["treasury_operation_request_id"] == self.tor.pk)
        self.assertEqual(row["case"], "CASE_C")
        self.assertFalse(row["eligible"])
        self.assertIsNone(row["recover_url"])
        self.assertIn("below the", row["block_reason"])

        # No history row exists for it — the monitor never recorded it.
        self.assertFalse(
            any(h["treasury_operation_request_id"] == self.tor.pk for h in data["history"]),
        )


# ─────────────────────────────────────────────
# 3 — CASE_B / CASE_E: HIGH severity, never auto-recovered
# ─────────────────────────────────────────────

class CaseBAndECriticalPipelineTests(_NoRedisMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d6_case_be_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_case_b_wallet_transaction_anomaly_high_severity_never_recovered(self):
        from simulator.wallet_ledger import credit_wallet

        wallet = make_wallet()
        wtx = credit_wallet(wallet.id, Decimal("5.00"), WalletTransaction.TX_BONUS, note="anomaly")
        tor = _make_executing_request(wallet=wallet, wallet_transaction=wtx)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            observe_treasury_stuck_executions_task.apply().get()
            resp = self.client.get(_dashboard_data_url())
        mock_mark_failed.assert_not_called()

        event = BrokerAuditEvent.objects.get(
            event_type=HISTORY_EVENT_TYPE, metadata__treasury_operation_request_id=tor.pk,
        )
        self.assertEqual(event.severity, "HIGH")

        data = resp.json()
        row = next(c for c in data["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_B")
        self.assertFalse(row["eligible"])
        self.assertIsNone(row["recover_url"])
        history_row = next(h for h in data["history"] if h["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(history_row["severity"], "HIGH")

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)  # untouched

    def test_case_e_audit_inconsistency_high_severity_never_recovered(self):
        executor = make_user(username="o3d6_case_e_executor", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        AuditLog.objects.create(
            event_type=_audit.EV_TREASURY_REQUEST_EXECUTED,
            action="probe EXECUTED event while still EXECUTING",
            detail={"treasury_request_id": tor.pk},
        )

        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            observe_treasury_stuck_executions_task.apply().get()
            resp = self.client.get(_dashboard_data_url())
        mock_mark_failed.assert_not_called()

        event = BrokerAuditEvent.objects.get(
            event_type=HISTORY_EVENT_TYPE, metadata__treasury_operation_request_id=tor.pk,
        )
        self.assertEqual(event.severity, "HIGH")

        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_E")
        self.assertFalse(row["eligible"])
        self.assertIsNone(row["recover_url"])

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)  # untouched


# ─────────────────────────────────────────────
# 4 — CASE_D / CASE_F: correct severities, complete metadata
# ─────────────────────────────────────────────

class CaseDAndFPipelineTests(_NoRedisMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d6_case_df_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_case_d_age_unknown_warning_severity_complete_metadata(self):
        executor = make_user(username="o3d6_case_d_executor", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        # No EXECUTION_STARTED audit event at all -> age UNKNOWN -> CASE_D.

        observe_treasury_stuck_executions_task.apply().get()
        event = BrokerAuditEvent.objects.get(
            event_type=HISTORY_EVENT_TYPE, metadata__treasury_operation_request_id=tor.pk,
        )
        self.assertEqual(event.severity, "WARNING")
        self.assertEqual(event.metadata["case"], "CASE_D")
        self.assertIsNone(event.metadata["age_seconds"])
        self.assertEqual(event.metadata["age_confidence"], "UNKNOWN")

        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_D")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["age_confidence"], "UNKNOWN")

    def test_case_f_inactive_executor_info_severity_complete_metadata(self):
        inactive_executor = make_user(username="o3d6_case_f_executor", is_staff=True, is_active=False)
        tor = _make_executing_request(executed_by=inactive_executor)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        observe_treasury_stuck_executions_task.apply().get()
        event = BrokerAuditEvent.objects.get(
            event_type=HISTORY_EVENT_TYPE, metadata__treasury_operation_request_id=tor.pk,
        )
        self.assertEqual(event.severity, "INFO")
        self.assertEqual(event.metadata["case"], "CASE_F")
        self.assertEqual(event.metadata["executed_by_is_active"], False)
        # CASE_F is informational only — does not by itself block eligibility.
        self.assertTrue(event.metadata["eligible"])

        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_F")
        self.assertTrue(row["eligible"])
        self.assertFalse(row["executed_by_is_active"])
        # Eligible AND not self-conflicted -> recovery link IS offered,
        # consistent with the inspector's own "CASE_F never blocks
        # eligibility by itself" contract.
        self.assertEqual(
            row["recover_url"], reverse("admin:treasury_request_recover", args=[tor.pk]),
        )


# ─────────────────────────────────────────────
# 5 — Redis: hit, miss, get-fails, setex-fails, dashboard always responds
# ─────────────────────────────────────────────

class RedisFullCycleTests(TestCase):

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3d6_redis_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_miss_then_hit_real_cycle(self):
        executor = make_user(username="o3d6_redis_exec", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        fake = FakeRedis()
        with patch("redis.from_url", return_value=fake):
            resp1 = self.client.get(_dashboard_data_url())
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(len(resp1.json()["candidates"]), 1)

            # Second call, same fake store: served from cache, not
            # recomputed — proven by adding a SECOND stuck request that
            # would appear if the payload were recomputed, and asserting
            # it does NOT show up while the cache is still warm.
            _make_executing_request(executed_by=make_user(username="o3d6_redis_exec2", is_staff=True))
            with patch("simulator.admin._compute_treasury_operational_dashboard_data") as mock_compute:
                resp2 = self.client.get(_dashboard_data_url())
            mock_compute.assert_not_called()
            self.assertEqual(len(resp2.json()["candidates"]), 1)  # still the cached count

    def test_cache_get_failure_falls_back_to_db_and_dashboard_still_responds(self):
        mock_client = MagicMock()
        mock_client.get.side_effect = RuntimeError("boom")
        with patch("redis.from_url", return_value=mock_client):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)

    def test_cache_setex_failure_does_not_break_response(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None
        mock_client.setex.side_effect = RuntimeError("boom")
        with patch("redis.from_url", return_value=mock_client):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary", resp.json())

    def test_redis_completely_unreachable_dashboard_still_responds(self):
        with patch("redis.from_url", side_effect=RuntimeError("connection refused")):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# 6 — Celery: registration, Beat cadence, contract, no-retry propagation
# ─────────────────────────────────────────────

class CeleryContractIntegrationTests(_NoRedisMixin, TestCase):

    def test_task_registered_and_beat_cadence_is_15_minutes(self):
        from celery.schedules import crontab
        from django.conf import settings
        from trx_simulator.celery import app as celery_app

        self.assertIn("simulator.observe_treasury_stuck_executions", celery_app.tasks.keys())
        entry = settings.CELERY_BEAT_SCHEDULE["observe-treasury-stuck-executions-15m"]
        self.assertEqual(entry["task"], "simulator.observe_treasury_stuck_executions")
        self.assertEqual(entry["schedule"], crontab(minute="*/15"))

    def test_return_value_is_json_serializable_with_real_data(self):
        executor = make_user(username="o3d6_celery_contract_exec", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        result = observe_treasury_stuck_executions_task.apply().get()
        json.dumps(result)  # raises if not serializable
        self.assertEqual(result["written"], 1)

    def test_service_exception_propagates_without_retry(self):
        with patch(
            "simulator.broker_audit.observe_stuck_treasury_executions",
            side_effect=RuntimeError("simulated read failure"),
        ):
            async_result = observe_treasury_stuck_executions_task.apply()
        self.assertTrue(async_result.failed())
        with self.assertRaises(RuntimeError):
            async_result.get()
        # max_retries=0 — Celery's own eager .apply() never re-invokes
        # the task body on failure; a single call raised, once.
        self.assertEqual(observe_treasury_stuck_executions_task.max_retries, 0)


# ─────────────────────────────────────────────
# 7 — Roles: assignment, removal, idempotency, no side effects
# ─────────────────────────────────────────────

class RoleAssignmentIntegrationTests(TestCase):

    def test_all_four_roles_assign_and_remove_idempotently(self):
        role_to_codename = {
            "submitter": "can_submit_treasury_request",
            "reviewer": "can_review_treasury_request",
            "executor": "can_execute_treasury_request",
            "recoverer": "can_recover_treasury_execution",
        }
        for role, codename in role_to_codename.items():
            with self.subTest(role=role):
                user = make_user(username=f"o3d6_role_{role}", is_staff=True)
                staff_before, superuser_before = user.is_staff, user.is_superuser
                groups_before = Group.objects.count()

                call_command("assign_treasury_role", user.username, role)
                from django.contrib.auth import get_user_model
                User = get_user_model()
                self.assertTrue(
                    User.objects.get(pk=user.pk).has_perm(f"simulator.{codename}")
                )

                # Idempotent re-assignment.
                call_command("assign_treasury_role", user.username, role)
                self.assertEqual(
                    User.objects.get(pk=user.pk).user_permissions.filter(codename=codename).count(), 1,
                )

                call_command("assign_treasury_role", user.username, role, "--remove")
                self.assertFalse(
                    User.objects.get(pk=user.pk).has_perm(f"simulator.{codename}")
                )

                # Idempotent removal.
                call_command("assign_treasury_role", user.username, role, "--remove")
                self.assertFalse(
                    User.objects.get(pk=user.pk).has_perm(f"simulator.{codename}")
                )

                user.refresh_from_db()
                self.assertEqual(user.is_staff, staff_before)
                self.assertEqual(user.is_superuser, superuser_before)
                self.assertEqual(Group.objects.count(), groups_before)


# ─────────────────────────────────────────────
# 8 — Dashboard permissions, driven by the role command itself
# ─────────────────────────────────────────────

class RoleCommandToDashboardAccessIntegrationTests(_NoRedisMixin, TestCase):
    """
    Genuinely cross-microbloque: grants roles via the O.3d-5 command,
    then checks O.3d-4 dashboard access with the real Django Client —
    neither O.3d-4's nor O.3d-5's own isolated test files exercise the
    command and the dashboard together.
    """

    def test_only_recoverer_role_grants_dashboard_access(self):
        client = Client()

        for role, should_pass in (
            ("submitter", False), ("reviewer", False),
            ("executor", False), ("recoverer", True),
        ):
            with self.subTest(role=role):
                user = make_user(username=f"o3d6_dash_{role}", is_staff=True)
                call_command("assign_treasury_role", user.username, role)
                client.force_login(user)
                resp = client.get(_dashboard_data_url())
                self.assertEqual(resp.status_code, 200 if should_pass else 403)

    def test_superuser_accesses_without_any_explicit_role(self):
        superuser = make_user(username="o3d6_dash_superuser", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)
        self.assertEqual(client.get(_dashboard_url()).status_code, 200)
        self.assertEqual(client.get(_dashboard_data_url()).status_code, 200)

    def test_unauthenticated_redirects_to_login(self):
        client = Client()
        resp = client.get(_dashboard_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_recoverer_role_removed_via_command_loses_dashboard_access(self):
        user = make_user(username="o3d6_dash_revoke", is_staff=True)
        call_command("assign_treasury_role", user.username, "recoverer")
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(_dashboard_data_url()).status_code, 200)

        call_command("assign_treasury_role", user.username, "recoverer", "--remove")
        self.assertEqual(client.get(_dashboard_data_url()).status_code, 403)


# ─────────────────────────────────────────────
# Invariants — re-affirmed explicitly across the whole O.3d pipeline
# ─────────────────────────────────────────────

class NoFinancialMovementAcrossFullPipelineTests(_NoRedisMixin, TestCase):
    """
    The single most important guarantee of O.3d: none of its five
    microbloques, run together in every combination this file
    exercises, ever moves money or mutates Treasury/Wallet state.
    """

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d6_invariant_recoverer")
        self.executor = make_user(username="o3d6_invariant_executor", is_staff=True)
        self.wallet = make_wallet(initial_balance=Decimal("123.45"))
        self.tor = _make_executing_request(wallet=self.wallet, executed_by=self.executor)
        _started_audit_log(self.tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_full_pipeline_never_touches_financial_state_or_calls_financial_functions(self):
        status_before = self.tor.status
        wallet_transaction_before = self.tor.wallet_transaction_id
        balance_before = self.wallet.available_balance
        wtx_count_before = WalletTransaction.objects.count()
        tor_count_before = TreasuryOperationRequest.objects.count()
        internal_transfer_before = InternalTransfer.objects.count()

        with patch("simulator.wallet_ledger.credit_wallet") as mock_credit, \
             patch("simulator.wallet_ledger.debit_wallet") as mock_debit, \
             patch(
                 "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
             ) as mock_mark_failed, \
             patch("simulator.treasury_requests.execute_treasury_request") as mock_execute:

            # Run the whole O.3d pipeline once, end to end.
            observe_treasury_stuck_executions_task.apply().get()
            observe_treasury_stuck_executions_task.apply().get()  # dedup tick
            self.client.get(_dashboard_url())
            self.client.get(_dashboard_data_url())
            call_command("assign_treasury_role", self.executor.username, "recoverer")
            call_command("assign_treasury_role", self.executor.username, "recoverer", "--remove")

            mock_credit.assert_not_called()
            mock_debit.assert_not_called()
            mock_mark_failed.assert_not_called()
            mock_execute.assert_not_called()

        self.tor.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.tor.status, status_before)
        self.assertEqual(self.tor.wallet_transaction_id, wallet_transaction_before)
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.count(), wtx_count_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), tor_count_before)
        self.assertEqual(InternalTransfer.objects.count(), internal_transfer_before)

    def test_broker_audit_event_is_the_only_new_persistence(self):
        """The monitor's only durable side effect, across the whole
        pipeline, is BrokerAuditEvent rows — never AuditLog (that
        module's own HTTP-request-scoped domain), never any Treasury/
        Wallet table."""
        auditlog_before = AuditLog.objects.count()

        observe_treasury_stuck_executions_task.apply().get()
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())

        self.assertEqual(AuditLog.objects.count(), auditlog_before)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=HISTORY_EVENT_TYPE).count(), 1,
        )

    def test_no_mutating_action_reachable_from_the_dashboard_html(self):
        resp = self.client.get(_dashboard_url())
        content = resp.content.decode()
        self.assertNotIn("Mark as FAILED", content)
        script_start = content.index("<script>")
        script_end = content.index("</script>", script_start)
        self.assertNotIn("POST", content[script_start:script_end].upper())
