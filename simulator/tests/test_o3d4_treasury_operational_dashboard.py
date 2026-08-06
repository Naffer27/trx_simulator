# simulator/tests/test_o3d4_treasury_operational_dashboard.py
"""
Bloque O.3d-4 — Treasury Operational Dashboard.

Covers ONLY the two new read-only admin endpoints registered on
TreasuryOperationRequestAdmin:

    GET /admin/simulator/treasuryoperationrequest/operational-dashboard/
    GET /admin/simulator/treasuryoperationrequest/operational-dashboard/data/

and their shared data builder, simulator/admin.py::
_compute_treasury_operational_dashboard_data(). Current-state candidates
come straight from inspect_stuck_treasury_execution() (O.3c-4b,
unmodified); history comes straight from BrokerAuditEvent rows already
persisted by observe_stuck_treasury_executions() (O.3d-2/3, unmodified).

This block NEVER calls mark_treasury_execution_failed(), NEVER calls
execute_treasury_request(), NEVER imports wallet_ledger.py, NEVER
creates or modifies a WalletTransaction, NEVER touches a Wallet
balance, and NEVER creates an AuditLog or BrokerAuditEvent row. No
model, no migration exist — this is a pure read-only view layer over
data two prior, unmodified blocks already produce.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600  # settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS default
HISTORY_EVENT_TYPE = "treasury.stuck_execution_observed"


def _grant_recover_permission(user):
    user.user_permissions.add(Permission.objects.get(codename="can_recover_treasury_execution"))
    user.refresh_from_db()
    return user


def _make_recoverer(**kwargs):
    return _grant_recover_permission(make_user(is_staff=True, **kwargs))


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3d-4 dashboard test",
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


def _make_eligible_executing_request(**overrides):
    executor = make_user(username=f"o3d4_exec_{TreasuryOperationRequest.objects.count()}", is_staff=True)
    tor = _make_executing_request(executed_by=executor, **overrides)
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
    return tor


def _dashboard_url():
    return reverse("admin:treasury_operational_dashboard")


def _dashboard_data_url():
    return reverse("admin:treasury_operational_dashboard_data")


def _changelist_url():
    return reverse("admin:simulator_treasuryoperationrequest_changelist")


class _NoRedisMixin:
    """
    Patches redis.from_url to always fail, so every test in this class
    exercises the DB-fallback path deterministically regardless of
    whether a real Redis is reachable in this environment — same
    discipline the CACHÉ requirement demands ("la caché nunca es fuente
    de verdad"). Caching itself is covered by its own dedicated test
    class below, which patches redis.from_url explicitly per test.
    """
    def setUp(self):
        super().setUp()
        patcher = patch("redis.from_url", side_effect=RuntimeError("no redis in this test"))
        patcher.start()
        self.addCleanup(patcher.stop)


class EndpointExistenceTests(_NoRedisMixin, TestCase):
    """1, 2 — both endpoints exist and return 200 for an authorized user."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_exists_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_html_endpoint_exists(self):
        resp = self.client.get(_dashboard_url())
        self.assertEqual(resp.status_code, 200)

    def test_json_endpoint_exists(self):
        resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")


class PermissionTests(_NoRedisMixin, TestCase):
    """3, 4, 5 — correct permission grants access; missing permission
    -> 403; unauthenticated -> redirect to admin login. Both endpoints."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_perm_recoverer")
        self.superuser = make_user(username="o3d4_perm_superuser", is_staff=True, is_superuser=True)
        self.no_perms = make_user(username="o3d4_perm_none", is_staff=True)
        self.client = Client()

    def test_recoverer_can_access_both_endpoints(self):
        self.client.force_login(self.recoverer)
        self.assertEqual(self.client.get(_dashboard_url()).status_code, 200)
        self.assertEqual(self.client.get(_dashboard_data_url()).status_code, 200)

    def test_superuser_can_access_both_endpoints(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(_dashboard_url()).status_code, 200)
        self.assertEqual(self.client.get(_dashboard_data_url()).status_code, 200)

    def test_staff_without_permission_gets_403_on_html(self):
        self.client.force_login(self.no_perms)
        self.assertEqual(self.client.get(_dashboard_url()).status_code, 403)

    def test_staff_without_permission_gets_403_on_json(self):
        self.client.force_login(self.no_perms)
        self.assertEqual(self.client.get(_dashboard_data_url()).status_code, 403)

    def test_other_treasury_roles_alone_do_not_grant_access(self):
        """submit/review/execute permissions alone are NOT enough —
        this dashboard is narrower than has_view_permission()."""
        other_role = make_user(username="o3d4_perm_other_role", is_staff=True)
        other_role.user_permissions.add(Permission.objects.get(codename="can_execute_treasury_request"))
        other_role.refresh_from_db()
        self.client.force_login(other_role)
        self.assertEqual(self.client.get(_dashboard_url()).status_code, 403)
        self.assertEqual(self.client.get(_dashboard_data_url()).status_code, 403)

    def test_unauthenticated_html_redirects_to_login(self):
        resp = self.client.get(_dashboard_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_unauthenticated_json_redirects_to_login(self):
        resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])


class CurrentStateSourceTests(_NoRedisMixin, TestCase):
    """6 — current state comes from inspect_stuck_treasury_execution()."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_source_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_calls_inspect_stuck_treasury_execution(self):
        with patch(
            "simulator.treasury_execution_recovery.inspect_stuck_treasury_execution",
        ) as mock_inspect:
            mock_inspect.return_value = []
            self.client.get(_dashboard_data_url())
        mock_inspect.assert_called_once()

    def test_candidate_fields_come_from_the_real_inspection_dataclass(self):
        tor = _make_eligible_executing_request()
        resp = self.client.get(_dashboard_data_url())
        data = resp.json()
        row = next(c for c in data["candidates"] if c["treasury_operation_request_id"] == tor.pk)

        real_candidate = next(
            c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk
        )
        self.assertEqual(row["case"], real_candidate.case)
        self.assertEqual(row["eligible"], real_candidate.eligible)
        self.assertEqual(row["age_confidence"], real_candidate.age_confidence)
        self.assertEqual(row["has_wallet_transaction"], real_candidate.has_wallet_transaction)


class HistorySourceTests(_NoRedisMixin, TestCase):
    """7, 23 — history comes from BrokerAuditEvent with the approved
    event_type, limited to TREASURY_DASHBOARD_HISTORY_LIMIT rows."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_history_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_history_reflects_a_real_broker_audit_event(self):
        tor = _make_eligible_executing_request()
        _broker_audit.observe_stuck_treasury_executions()

        resp = self.client.get(_dashboard_data_url())
        data = resp.json()

        self.assertEqual(len(data["history"]), 1)
        row = data["history"][0]
        self.assertEqual(row["treasury_operation_request_id"], tor.pk)
        self.assertEqual(row["case"], "CASE_A")
        self.assertEqual(row["severity"], "WARNING")
        self.assertTrue(row["eligible"])

    def test_history_only_reads_the_approved_event_type(self):
        BrokerAuditEvent.objects.create(
            event_type="treasury.request_executed",  # unrelated Treasury event
            category="PAYMENTS", severity="INFO", actor_type="SYSTEM",
            description="unrelated", metadata={"treasury_operation_request_id": 999},
        )
        resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.json()["history"], [])

    def test_history_limit_is_applied_and_documented(self):
        from simulator.admin import TREASURY_DASHBOARD_HISTORY_LIMIT
        self.assertEqual(TREASURY_DASHBOARD_HISTORY_LIMIT, 25)

        for i in range(TREASURY_DASHBOARD_HISTORY_LIMIT + 5):
            BrokerAuditEvent.objects.create(
                event_type=HISTORY_EVENT_TYPE,
                category="PAYMENTS", severity="WARNING", actor_type="SYSTEM",
                description=f"observation {i}",
                metadata={
                    "treasury_operation_request_id": i, "case": "CASE_A",
                    "eligible": True, "age_seconds": 700.0, "block_reason": None,
                    "executed_by_id": None,
                },
            )

        resp = self.client.get(_dashboard_data_url())
        data = resp.json()
        self.assertEqual(len(data["history"]), TREASURY_DASHBOARD_HISTORY_LIMIT)
        self.assertEqual(data["history_limit"], TREASURY_DASHBOARD_HISTORY_LIMIT)


class CachingTests(TestCase):
    """8, 9, 10 — Redis hit avoids recomputation; Redis down falls back
    to DB; a failed cache write does not break the response."""

    def setUp(self):
        self.recoverer = _make_recoverer(username="o3d4_cache_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_redis_hit_skips_recomputation(self):
        cached_payload = {
            "ts": timezone.now().isoformat(),
            "summary": {"total_executing": 0, "total_eligible": 0,
                        "case_counts": {"CASE_A": 0, "CASE_B": 0, "CASE_C": 0,
                                        "CASE_D": 0, "CASE_E": 0, "CASE_F": 0}},
            "candidates": [], "history": [], "history_limit": 25,
        }
        mock_client = MagicMock()
        mock_client.get.return_value = json.dumps(cached_payload).encode()

        with patch("redis.from_url", return_value=mock_client), \
             patch("simulator.admin._compute_treasury_operational_dashboard_data") as mock_compute:
            resp = self.client.get(_dashboard_data_url())

        self.assertEqual(resp.status_code, 200)
        mock_compute.assert_not_called()
        mock_client.get.assert_called_once()

    def test_redis_down_falls_back_to_db(self):
        _make_eligible_executing_request()
        with patch("redis.from_url", side_effect=RuntimeError("redis down")):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["candidates"]), 1)

    def test_cache_get_failure_falls_back_to_db(self):
        mock_client = MagicMock()
        mock_client.get.side_effect = RuntimeError("get failed")
        with patch("redis.from_url", return_value=mock_client):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)

    def test_cache_set_failure_does_not_break_the_response(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None
        mock_client.setex.side_effect = RuntimeError("set failed")
        with patch("redis.from_url", return_value=mock_client):
            resp = self.client.get(_dashboard_data_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary", resp.json())

    def test_cache_key_is_versioned(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None
        with patch("redis.from_url", return_value=mock_client):
            self.client.get(_dashboard_data_url())
        key_used = mock_client.get.call_args[0][0]
        self.assertTrue(key_used.startswith("trx:treasury:operational_dashboard:v"))

    def test_cache_ttl_is_30_seconds(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None
        with patch("redis.from_url", return_value=mock_client):
            self.client.get(_dashboard_data_url())
        ttl_used = mock_client.setex.call_args[0][1]
        self.assertEqual(ttl_used, 30)


class SerializationAndSummaryTests(_NoRedisMixin, TestCase):
    """11, 12 — JSON serializable; summary counts correct."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_summary_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_response_is_valid_json(self):
        _make_eligible_executing_request()
        resp = self.client.get(_dashboard_data_url())
        json.loads(resp.content)  # raises if not valid JSON

    def test_summary_counts_are_correct(self):
        _make_eligible_executing_request()  # CASE_A
        tor_young = _make_executing_request(
            executed_by=make_user(username="o3d4_summary_young_exec", is_staff=True),
        )
        _started_audit_log(tor_young.pk, age_seconds=50)  # CASE_C, not eligible

        resp = self.client.get(_dashboard_data_url())
        summary = resp.json()["summary"]
        self.assertEqual(summary["total_executing"], 2)
        self.assertEqual(summary["total_eligible"], 1)
        self.assertEqual(summary["case_counts"]["CASE_A"], 1)
        self.assertEqual(summary["case_counts"]["CASE_C"], 1)


class CaseVisibilityTests(_NoRedisMixin, TestCase):
    """13, 14 — CASE_A-F all visible correctly; eligible/block_reason correct."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_cases_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_all_six_cases_are_represented_in_case_counts_keys(self):
        resp = self.client.get(_dashboard_data_url())
        counts = resp.json()["summary"]["case_counts"]
        for case in ("CASE_A", "CASE_B", "CASE_C", "CASE_D", "CASE_E", "CASE_F"):
            self.assertIn(case, counts)

    def test_case_b_wallet_transaction_anomaly_visible_with_block_reason(self):
        from simulator.wallet_ledger import credit_wallet

        wallet = make_wallet()
        wtx = credit_wallet(wallet.id, Decimal("5.00"), WalletTransaction.TX_BONUS, note="anomaly")
        tor = _make_executing_request(wallet=wallet, wallet_transaction=wtx)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_B")
        self.assertFalse(row["eligible"])
        self.assertIsNotNone(row["block_reason"])
        self.assertTrue(row["has_wallet_transaction"])

    def test_case_c_not_eligible_with_threshold_block_reason(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d4_case_c_exec", is_staff=True))
        _started_audit_log(tor.pk, age_seconds=50)

        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(row["case"], "CASE_C")
        self.assertFalse(row["eligible"])
        self.assertIn("below the", row["block_reason"])


class UrlTests(_NoRedisMixin, TestCase):
    """15, 16 — detail_url is correct; recover_url only when the
    existing recovery UI would itself authorize it."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_url_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_detail_url_points_to_the_real_change_view(self):
        tor = _make_eligible_executing_request()
        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(
            row["detail_url"],
            reverse("admin:simulator_treasuryoperationrequest_change", args=[tor.pk]),
        )

    def test_recover_url_present_for_eligible_non_conflicted_candidate(self):
        tor = _make_eligible_executing_request()
        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(
            row["recover_url"],
            reverse("admin:treasury_request_recover", args=[tor.pk]),
        )

    def test_recover_url_absent_when_not_eligible(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d4_url_young_exec", is_staff=True))
        _started_audit_log(tor.pk, age_seconds=50)  # CASE_C
        resp = self.client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertIsNone(row["recover_url"])

    def test_recover_url_absent_when_viewer_is_the_requester(self):
        requester_recoverer = _make_recoverer(username="o3d4_url_self_req")
        tor = _make_eligible_executing_request(requested_by=requester_recoverer)
        client = Client()
        client.force_login(requester_recoverer)
        resp = client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertIsNone(row["recover_url"])

    def test_recover_url_absent_when_viewer_is_the_approver(self):
        approver_recoverer = _make_recoverer(username="o3d4_url_self_appr")
        tor = _make_eligible_executing_request(approved_by=approver_recoverer)
        client = Client()
        client.force_login(approver_recoverer)
        resp = client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertIsNone(row["recover_url"])

    def test_recover_url_present_when_viewer_is_the_original_executor(self):
        """mark_treasury_execution_failed() explicitly permits the
        recovering user to BE the executed_by — the dashboard must not
        add a stricter rule than the recovery UI it links to."""
        recoverer = _make_recoverer(username="o3d4_url_self_exec")
        tor = _make_executing_request(executed_by=recoverer)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        client = Client()
        client.force_login(recoverer)
        resp = client.get(_dashboard_data_url())
        row = next(c for c in resp.json()["candidates"] if c["treasury_operation_request_id"] == tor.pk)
        self.assertEqual(
            row["recover_url"],
            reverse("admin:treasury_request_recover", args=[tor.pk]),
        )


class NoMutatingUiTests(_NoRedisMixin, TestCase):
    """17 — no mutating buttons/forms anywhere in the HTML shell."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_nomutate_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_html_contains_no_mark_as_failed_button(self):
        _make_eligible_executing_request()
        resp = self.client.get(_dashboard_url())
        self.assertNotContains(resp, "Mark as FAILED")

    def test_html_contains_no_form_targeting_a_treasury_mutation(self):
        """
        Django's own admin chrome always renders one <form> (the
        header's CSRF-protected logout form, action="/admin/logout/")
        and its own theme/nav-sidebar <button> elements — asserting
        their blanket absence would false-positive on every admin page,
        not just this one (confirmed empirically). What actually
        matters here: this page's client-side row-rendering code (the
        JS template literals in the static HTML shell this test client
        actually receives — per-candidate data itself is fetched by
        the browser at runtime, which Django's test client never
        executes) builds every link with an <a href="..."> anchor, and
        never issues anything but a GET: no "POST" string appears
        anywhere in the page's own <script> block.
        """
        resp = self.client.get(_dashboard_url())
        content = resp.content.decode()

        # This page's own row-building templates use <a href="...">
        # for both links it ever renders — never a <form> or <button>.
        self.assertIn('<a class="td-a" href="${c.recover_url}">Recover', content)
        self.assertIn('<a class="td-a" href="${c.detail_url}">Detail', content)

        script_start = content.index("<script>")
        script_end = content.index("</script>", script_start)
        script_source = content[script_start:script_end]
        self.assertNotIn("POST", script_source.upper())
        self.assertNotIn("<form", script_source)
        self.assertNotIn("<button", script_source)


class NoServiceCallsOrSideEffectsTests(_NoRedisMixin, TestCase):
    """18, 19, 20, 21 — never calls the two mutating Treasury services,
    never touches Wallet/WalletTransaction/balances, never creates
    AuditLog or BrokerAuditEvent."""

    def setUp(self):
        super().setUp()
        self.recoverer = _make_recoverer(username="o3d4_noeffect_recoverer")
        self.wallet = make_wallet(initial_balance=Decimal("50.00"))
        self.tor = _make_eligible_executing_request(wallet=self.wallet)
        self.client = Client()
        self.client.force_login(self.recoverer)

    def test_never_calls_mark_treasury_execution_failed(self):
        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            self.client.get(_dashboard_url())
            self.client.get(_dashboard_data_url())
        mock_mark_failed.assert_not_called()

    def test_never_calls_execute_treasury_request(self):
        with patch(
            "simulator.treasury_requests.execute_treasury_request",
        ) as mock_execute:
            self.client.get(_dashboard_url())
            self.client.get(_dashboard_data_url())
        mock_execute.assert_not_called()

    def test_never_touches_wallet_balance(self):
        balance_before = self.wallet.available_balance
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_never_creates_wallet_transaction(self):
        before = WalletTransaction.objects.count()
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())
        self.assertEqual(WalletTransaction.objects.count(), before)

    def test_never_modifies_the_treasury_operation_request(self):
        status_before = self.tor.status
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())
        self.tor.refresh_from_db()
        self.assertEqual(self.tor.status, status_before)

    def test_never_creates_auditlog(self):
        before = AuditLog.objects.count()
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())
        self.assertEqual(AuditLog.objects.count(), before)

    def test_never_creates_broker_audit_event(self):
        before = BrokerAuditEvent.objects.count()
        self.client.get(_dashboard_url())
        self.client.get(_dashboard_data_url())
        self.assertEqual(BrokerAuditEvent.objects.count(), before)


class PollingAndChangelistLinkTests(_NoRedisMixin, TestCase):
    """22 — polling is present in the HTML shell. Bonus: changelist
    link visible only to authorized users."""

    def test_html_contains_polling_interval(self):
        recoverer = _make_recoverer(username="o3d4_poll_recoverer")
        client = Client()
        client.force_login(recoverer)
        resp = client.get(_dashboard_url())
        self.assertContains(resp, "setInterval")
        self.assertContains(resp, "POLL_MS")

    def test_changelist_shows_dashboard_link_for_recoverer(self):
        recoverer = _make_recoverer(username="o3d4_poll_changelist_recoverer")
        client = Client()
        client.force_login(recoverer)
        resp = client.get(_changelist_url())
        self.assertContains(resp, "Operational Dashboard")

    def test_changelist_hides_dashboard_link_for_non_recoverer(self):
        submitter = make_user(username="o3d4_poll_changelist_submitter", is_staff=True)
        submitter.user_permissions.add(Permission.objects.get(codename="can_submit_treasury_request"))
        submitter.refresh_from_db()
        client = Client()
        client.force_login(submitter)
        resp = client.get(_changelist_url())
        self.assertNotContains(resp, "Operational Dashboard")


class ScopeAndSafetyTests(TestCase):
    """24 — AST confirms absolute read-only-ness of the three new
    functions this block introduces."""

    def test_ast_confirms_no_financial_functions_anywhere(self):
        import ast
        import inspect
        import textwrap

        from simulator import admin as admin_module

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed", "execute_treasury_request",
        }
        forbidden_imports = {"wallet_ledger"}

        for fn in (
            admin_module._compute_treasury_operational_dashboard_data,
            admin_module.TreasuryOperationRequestAdmin.treasury_operational_dashboard_data,
            admin_module.TreasuryOperationRequestAdmin.treasury_operational_dashboard_view,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            imported = set()
            called = set()
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

            with self.subTest(fn=fn.__qualname__):
                self.assertFalse(forbidden_calls & called, f"found: {forbidden_calls & called}")
                self.assertFalse(forbidden_imports & imported, f"found: {forbidden_imports & imported}")

    def test_no_save_or_status_assignment_anywhere(self):
        import ast
        import inspect
        import textwrap

        from simulator import admin as admin_module

        for fn in (
            admin_module._compute_treasury_operational_dashboard_data,
            admin_module.TreasuryOperationRequestAdmin.treasury_operational_dashboard_data,
            admin_module.TreasuryOperationRequestAdmin.treasury_operational_dashboard_view,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    self.assertNotEqual(name, "save", f"{fn.__qualname__} must not call .save()")
                if isinstance(node, ast.Attribute) and node.attr in ("status", "wallet_transaction"):
                    self.assertNotIsInstance(
                        node.ctx, ast.Store,
                        f"{fn.__qualname__} must not assign .{node.attr} directly",
                    )
