# simulator/tests/test_o3c5c_treasury_admin_end_to_end.py
"""
Microbloque O.3c-5c — Treasury Execution/Recovery Admin UI, end-to-end
verification.

This file does NOT re-test what O.3a-5/O.3b-3/O.3c-5a/O.3c-5b already
cover in isolation (button visibility per view, individual permission
checks, individual message strings, AST-based delegation proofs). It
instead drives full realistic journeys through the actual admin URLs,
with distinct users playing distinct roles across MULTIPLE views in
sequence, and asserts the cross-cutting invariants that only show up
when the whole pipeline runs together:

  Submit -> Approve -> Execute -> exactly one WalletTransaction
  EXECUTING (stuck) -> Recovery -> FAILED -> no money ever moved

Every mutation here goes through the real admin views (Django test
Client against the real URLconf, permissions, templates and messages
framework) — never by calling treasury_requests.py /
treasury_execution_recovery.py functions directly. Concurrency/locking
itself (select_for_update, the conditional UPDATE-based failure path)
is already unit-tested exhaustively in test_o3c3_treasury_execution_
service.py (StepBRevalidationTests, StatusTransitionTests.
test_double_execution_second_call_raises) and test_o3c4c_treasury_
execution_recovery_service.py — this file only proves the admin view
layer surfaces that existing protection correctly end-to-end (a
second, sequential POST through the browser is refused safely, with
no duplicate WalletTransaction and a safe user-facing message), not
that the lock itself works.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator import audit as _audit
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.wallet_ledger import reconcile_wallet

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600  # settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS default


# ─────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────

def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _make_submitter(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_submit_treasury_request")


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _make_executor(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_execute_treasury_request")


def _make_recoverer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_recover_treasury_execution")


def _submit_url():
    return reverse("admin:treasury_request_new")


def _approve_url(pk):
    return reverse("admin:treasury_request_approve", args=[pk])


def _reject_url(pk):
    return reverse("admin:treasury_request_reject", args=[pk])


def _execute_url(pk):
    return reverse("admin:treasury_request_execute", args=[pk])


def _recover_url(pk):
    return reverse("admin:treasury_request_recover", args=[pk])


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _submit_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "50.00",
        "reason": "O.3c-5c end-to-end test",
    }
    data.update(overrides)
    return data


def _debit_submit_data(wallet, amount):
    return {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_MANUAL_DEBIT,
        "amount": amount,
        "reason": "O.3c-5c insufficient funds scenario",
        "reference": "REF-O3C5C-DEBIT",
        "category": TreasuryOperationRequest.CAT_OTHER,
        "comment": "manual debit test comment",
    }


def _started_audit_log(pk, age_seconds):
    created_at = timezone.now() - timedelta(seconds=age_seconds)
    return AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        action=f"Treasury request #{pk} execution started",
        detail={"treasury_request_id": pk},
        created_at=created_at,
    )


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3c-5c recovery scenario",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by,
        wallet_transaction=wallet_transaction,
        requested_by=requested_by,
        approved_by=approved_by,
    )
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


# ─────────────────────────────────────────────
# Scenario 1 — full successful lifecycle: Submit -> Approve -> Execute
# ─────────────────────────────────────────────

class FullSuccessfulLifecycleTests(TestCase):

    def setUp(self):
        self.submitter = _make_submitter(username="o3c5c_s1_submitter")
        self.reviewer = _make_reviewer(username="o3c5c_s1_reviewer")
        self.executor = _make_executor(username="o3c5c_s1_executor")
        self.wallet = make_wallet()
        self.client = Client()

    def test_full_lifecycle_submit_approve_execute(self):
        # 1. Submit — distinct operator, through the real admin view.
        self.client.force_login(self.submitter)
        resp = self.client.post(_submit_url(), data=_submit_data(self.wallet, amount="120.00"))
        self.assertEqual(TreasuryOperationRequest.objects.count(), 1)
        tor = TreasuryOperationRequest.objects.get()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(tor.requested_by_id, self.submitter.pk)
        self.assertRedirects(resp, _change_url(tor.pk))

        # Approve/Execute buttons must not exist yet at PENDING.
        resp = self.client.get(_change_url(tor.pk))
        self.assertNotContains(resp, "Execute →")

        # 2. Approve — distinct supervisor.
        self.client.force_login(self.reviewer)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Approve →")
        resp = self.client.post(_approve_url(tor.pk), data={})
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(tor.approved_by_id, self.reviewer.pk)

        # Approve/Reject buttons gone now that it's APPROVED; Execute
        # button appears for a distinct, authorized executor.
        resp = self.client.get(_change_url(tor.pk))
        self.assertNotContains(resp, "Approve →")
        self.assertNotContains(resp, "Reject →")
        self.assertNotContains(resp, "Execute →")  # reviewer lacks execute perm

        self.client.force_login(self.executor)
        resp = self.client.get(_change_url(tor.pk))
        self.assertContains(resp, "Execute →")

        # 3. Execute — distinct executor.
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(_execute_url(tor.pk), data={"execution_notes": "confirmed"})
        self.assertRedirects(resp, _change_url(tor.pk))

        tor.refresh_from_db()
        self.wallet.refresh_from_db()

        # Final state.
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTED)
        self.assertEqual(tor.executed_by_id, self.executor.pk)
        self.assertIsNotNone(tor.wallet_transaction_id)

        # Exactly one WalletTransaction for this request/wallet.
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before + 1,
        )
        wtx = tor.wallet_transaction
        self.assertEqual(wtx.amount, Decimal("120.00"))
        self.assertEqual(self.wallet.available_balance, Decimal("120.00"))
        self.assertEqual(wtx.balance_after, Decimal("120.00"))

        # No InternalTransfer, ever.
        self.assertEqual(InternalTransfer.objects.count(), 0)

        # reconcile_wallet() must be consistent after a successful execution.
        recon = reconcile_wallet(self.wallet.id)
        self.assertTrue(recon["ok"], recon)
        self.assertEqual(recon["drift"], Decimal("0"))

        # Detail page: EXECUTED panel visible, Execute button gone.
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("Execution", content)
        self.assertIn(f"#{wtx.pk}", content)
        self.assertIn(str(wtx.balance_after), content)
        self.assertIn(self.executor.username, content)
        self.assertNotIn("Execute →", content)


# ─────────────────────────────────────────────
# Scenario 2 — insufficient funds
# ─────────────────────────────────────────────

class InsufficientFundsLifecycleTests(TestCase):

    def setUp(self):
        self.submitter = _make_submitter(username="o3c5c_s2_submitter")
        self.reviewer = _make_reviewer(username="o3c5c_s2_reviewer")
        self.executor = _make_executor(username="o3c5c_s2_executor")
        self.wallet = make_wallet(initial_balance=Decimal("5.00"))
        self.client = Client()

    def test_insufficient_funds_fails_cleanly_end_to_end(self):
        self.client.force_login(self.submitter)
        resp = self.client.post(
            _submit_url(), data=_debit_submit_data(self.wallet, amount="999.00"),
        )
        tor = TreasuryOperationRequest.objects.get()
        self.assertRedirects(resp, _change_url(tor.pk))

        self.client.force_login(self.reviewer)
        self.client.post(_approve_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        self.client.force_login(self.executor)
        resp = self.client.post(_execute_url(tor.pk), data={}, follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("fondos insuficientes" in m for m in msgs))

        tor.refresh_from_db()
        self.wallet.refresh_from_db()

        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIsNone(tor.wallet_transaction)
        self.assertTrue(tor.failure_reason)
        self.assertNotIn("[MANUAL RECOVERY]", tor.failure_reason)

        # Balance untouched, no WalletTransaction created.
        self.assertEqual(self.wallet.available_balance, Decimal("5.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

        content = resp.content.decode()
        self.assertIn("Execution Failure", content)
        self.assertIn(tor.failure_reason, content)
        self.assertIn("Financial engine failure", content)
        self.assertNotIn("Manual recovery (not a financial engine failure)", content)


# ─────────────────────────────────────────────
# Scenario 3 — double-click / double POST through the admin view
# ─────────────────────────────────────────────

class DoubleClickDoublePostTests(TestCase):

    def setUp(self):
        self.submitter = _make_submitter(username="o3c5c_s3_submitter")
        self.reviewer = _make_reviewer(username="o3c5c_s3_reviewer")
        self.executor = _make_executor(username="o3c5c_s3_executor")
        self.wallet = make_wallet()
        self.client = Client()

        self.client.force_login(self.submitter)
        self.client.post(_submit_url(), data=_submit_data(self.wallet, amount="30.00"))
        self.tor = TreasuryOperationRequest.objects.get()
        self.client.force_login(self.reviewer)
        self.client.post(_approve_url(self.tor.pk), data={})
        self.tor.refresh_from_db()

    def test_second_post_does_not_duplicate_wallet_transaction_or_move_balance_again(self):
        self.client.force_login(self.executor)

        resp1 = self.client.post(_execute_url(self.tor.pk), data={}, follow=True)
        self.tor.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.tor.status, TreasuryOperationRequest.ST_EXECUTED)
        first_wtx_id = self.tor.wallet_transaction_id
        balance_after_first = self.wallet.available_balance
        msgs1 = [str(m) for m in resp1.context["messages"]]
        self.assertTrue(any("ejecutada" in m for m in msgs1))

        # Second POST — same request, now already EXECUTED.
        resp2 = self.client.post(_execute_url(self.tor.pk), data={}, follow=True)
        self.tor.refresh_from_db()
        self.wallet.refresh_from_db()

        self.assertEqual(self.tor.status, TreasuryOperationRequest.ST_EXECUTED)  # unchanged
        self.assertEqual(self.tor.wallet_transaction_id, first_wtx_id)  # unchanged
        self.assertEqual(self.wallet.available_balance, balance_after_first)  # unchanged
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), 1,
        )
        msgs2 = [str(m) for m in resp2.context["messages"]]
        self.assertTrue(any("ya no está aprobada" in m for m in msgs2))
        # Safe message — never leaks internals of the original attempt.
        self.assertFalse(any("Traceback" in m or "Exception" in m for m in msgs2))

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_executed").count(), 1,
        )


# ─────────────────────────────────────────────
# Scenario 4 — separation of duties, across submit/approve/execute/recover
# ─────────────────────────────────────────────

class SeparationOfDutiesTests(TestCase):

    def setUp(self):
        self.requester = _make_reviewer(username="o3c5c_s4_requester")  # also holds review, to isolate self-review
        self.approver = _make_executor(username="o3c5c_s4_approver")    # also holds execute, to isolate self-execute
        self.other_reviewer = _make_reviewer(username="o3c5c_s4_other_reviewer")
        self.executor = _make_executor(username="o3c5c_s4_executor")
        self.recoverer = _make_recoverer(username="o3c5c_s4_recoverer")
        self.wallet = make_wallet()
        self.client = Client()

    def test_requested_by_cannot_approve_own_request(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.requester,
        )
        self.client.force_login(self.requester)
        resp = self.client.post(_approve_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_requested_by_cannot_execute_own_request(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x",
            requested_by=self.approver, status=TreasuryOperationRequest.ST_APPROVED,
            approved_by=self.other_reviewer, approved_at=timezone.now(),
        )
        self.client.force_login(self.approver)  # also holds can_execute
        resp = self.client.post(_execute_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def test_approved_by_cannot_execute_own_approval(self):
        requester_plain = make_user(username="o3c5c_s4_plain_requester")
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x",
            requested_by=requester_plain, status=TreasuryOperationRequest.ST_APPROVED,
            approved_by=self.approver, approved_at=timezone.now(),
        )
        self.client.force_login(self.approver)
        resp = self.client.post(_execute_url(tor.pk), data={})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)

    def _eligible_stuck_request(self, requested_by, approved_by, executed_by):
        tor = _make_executing_request(
            wallet=self.wallet, executed_by=executed_by,
            requested_by=requested_by, approved_by=approved_by,
        )
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        return tor

    def test_requested_by_cannot_recover(self):
        tor = self._eligible_stuck_request(
            requested_by=self.recoverer, approved_by=self.other_reviewer, executed_by=self.executor,
        )
        self.client.force_login(self.recoverer)
        resp = self.client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_approved_by_cannot_recover(self):
        recoverer2 = _make_recoverer(username="o3c5c_s4_recoverer2")
        tor = self._eligible_stuck_request(
            requested_by=self.other_reviewer, approved_by=recoverer2, executed_by=self.executor,
        )
        self.client.force_login(recoverer2)
        resp = self.client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertEqual(resp.status_code, 403)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_original_executed_by_can_recover_eligible_stuck_execution(self):
        recoverer3 = _make_recoverer(username="o3c5c_s4_recoverer3")
        tor = self._eligible_stuck_request(
            requested_by=self.other_reviewer, approved_by=self.other_reviewer, executed_by=recoverer3,
        )
        self.client.force_login(recoverer3)
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "confirmed dead worker, self-recovering"},
        )
        self.assertRedirects(resp, _change_url(tor.pk))
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIn("[MANUAL RECOVERY]", tor.failure_reason)


# ─────────────────────────────────────────────
# Scenario 5 — eligible recovery, full flow
# ─────────────────────────────────────────────

class EligibleRecoveryFullFlowTests(TestCase):

    def setUp(self):
        self.executor = make_user(username="o3c5c_s5_executor", is_staff=True)
        self.requester = make_user(username="o3c5c_s5_requester")
        self.approver = make_user(username="o3c5c_s5_approver")
        self.recoverer = _make_recoverer(username="o3c5c_s5_recoverer")
        self.wallet = make_wallet(initial_balance=Decimal("60.00"))
        self.client = Client()

    def test_eligible_recovery_end_to_end(self):
        tor = _make_executing_request(
            wallet=self.wallet, executed_by=self.executor,
            requested_by=self.requester, approved_by=self.approver,
            wallet_transaction=None,
        )
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 250)

        self.client.force_login(self.recoverer)

        # Banner + button visible on the detail page.
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertIn("CASE_A", content)
        self.assertIn("Mark as FAILED", content)

        # Confirmation screen.
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Recovery Reason")

        wtx_before = WalletTransaction.objects.count()
        balance_before = self.wallet.available_balance

        # POST recovery_reason.
        resp = self.client.post(
            _recover_url(tor.pk),
            data={"recovery_reason": "worker crashed, confirmed via infra logs"},
            follow=True,
        )
        self.assertRedirects(resp, _change_url(tor.pk))

        tor.refresh_from_db()
        self.wallet.refresh_from_db()

        self.assertEqual(tor.status, TreasuryOperationRequest.ST_FAILED)
        self.assertTrue(tor.failure_reason.startswith("[MANUAL RECOVERY]"))
        self.assertIn("worker crashed, confirmed via infra logs", tor.failure_reason)

        # No money moved, no WalletTransaction created, no linkage.
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.count(), wtx_before)
        self.assertIsNone(tor.wallet_transaction)
        self.assertEqual(InternalTransfer.objects.count(), 0)

        # Panel now identifies manual recovery, not a financial failure.
        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("Execution Failure", content)
        self.assertIn("Manual recovery (not a financial engine failure)", content)
        self.assertNotIn("Mark as FAILED", content)


# ─────────────────────────────────────────────
# Scenario 6 — not-eligible recovery, every blocking case
# ─────────────────────────────────────────────

class NotEligibleRecoveryTests(TestCase):

    def setUp(self):
        self.executor = make_user(username="o3c5c_s6_executor", is_staff=True)
        self.requester = make_user(username="o3c5c_s6_requester")
        self.approver = make_user(username="o3c5c_s6_approver")
        self.recoverer = _make_recoverer(username="o3c5c_s6_recoverer")
        self.client = Client()
        self.client.force_login(self.recoverer)

    def _assert_not_eligible_and_untouched(self, tor):
        wallet_transaction_id_before = tor.wallet_transaction_id
        wtx_count_before = WalletTransaction.objects.count()

        resp = self.client.get(_change_url(tor.pk))
        content = resp.content.decode()
        self.assertIn("STUCK IN EXECUTING", content)
        self.assertIn("✗ NO", content)
        self.assertNotIn("Mark as FAILED", content)

        # Direct URL access must not mutate anything — including, for
        # the CASE_B scenario, not touching the (anomalous, pre-existing)
        # wallet_transaction link either.
        resp = self.client.post(
            _recover_url(tor.pk), data={"recovery_reason": "trying anyway"}, follow=True,
        )
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("no es elegible" in m for m in msgs))

        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(tor.wallet_transaction_id, wallet_transaction_id_before)
        self.assertEqual(WalletTransaction.objects.count(), wtx_count_before)
        return content

    def test_recent_execution_case_c_not_eligible(self):
        tor = _make_executing_request(executed_by=self.executor, requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=45)
        content = self._assert_not_eligible_and_untouched(tor)
        self.assertIn("CASE_C", content)

    def test_age_unknown_case_d_not_eligible(self):
        tor = _make_executing_request(executed_by=self.executor, requested_by=self.requester, approved_by=self.approver)
        # No EXECUTION_STARTED audit event at all -> age UNKNOWN.
        content = self._assert_not_eligible_and_untouched(tor)
        self.assertIn("CASE_D", content)

    def test_wallet_transaction_linked_case_b_not_eligible(self):
        from simulator.wallet_ledger import credit_wallet

        wallet = make_wallet()
        wtx = credit_wallet(wallet.id, Decimal("5.00"), WalletTransaction.TX_BONUS, note="anomaly setup")
        tor = _make_executing_request(
            wallet=wallet, executed_by=self.executor, requested_by=self.requester,
            approved_by=self.approver, wallet_transaction=wtx,
        )
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        content = self._assert_not_eligible_and_untouched(tor)
        self.assertIn("CASE_B", content)

    def test_executed_event_inconsistency_case_e_not_eligible(self):
        tor = _make_executing_request(executed_by=self.executor, requested_by=self.requester, approved_by=self.approver)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        AuditLog.objects.create(
            event_type=_audit.EV_TREASURY_REQUEST_EXECUTED,
            action="probe EXECUTED event while still EXECUTING",
            detail={"treasury_request_id": tor.pk},
        )
        content = self._assert_not_eligible_and_untouched(tor)
        self.assertIn("CASE_E", content)


# ─────────────────────────────────────────────
# Scenario 7 — permission matrix across roles
# ─────────────────────────────────────────────

class PermissionMatrixTests(TestCase):

    def setUp(self):
        self.submit_only = _make_submitter(username="o3c5c_s7_submit_only")
        self.review_only = _make_reviewer(username="o3c5c_s7_review_only")
        self.execute_only = _make_executor(username="o3c5c_s7_execute_only")
        self.recover_only = _make_recoverer(username="o3c5c_s7_recover_only")
        self.no_perms = make_user(username="o3c5c_s7_no_perms", is_staff=True)
        self.superuser = make_user(username="o3c5c_s7_superuser", is_staff=True, is_superuser=True)
        self.other = make_user(username="o3c5c_s7_other")
        self.wallet = make_wallet()
        self.client = Client()

    def test_staff_without_any_treasury_permission_cannot_view_detail(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
        )
        self.client.force_login(self.no_perms)
        resp = self.client.get(_change_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_each_role_only_sees_its_own_action_pending(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
        )
        for user, expect_approve in (
            (self.submit_only, False), (self.review_only, True),
            (self.execute_only, False), (self.recover_only, False),
            (self.superuser, True),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                resp = self.client.get(_change_url(tor.pk))
                self.assertEqual(resp.status_code, 200)
                if expect_approve:
                    self.assertContains(resp, "Approve →")
                else:
                    self.assertNotContains(resp, "Approve →")

    def test_each_role_only_sees_its_own_action_approved(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_APPROVED, approved_by=self.other,
            approved_at=timezone.now(),
        )
        for user, expect_execute in (
            (self.submit_only, False), (self.review_only, False),
            (self.execute_only, True), (self.recover_only, False),
            (self.superuser, True),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                resp = self.client.get(_change_url(tor.pk))
                self.assertEqual(resp.status_code, 200)
                if expect_execute:
                    self.assertContains(resp, "Execute →")
                else:
                    self.assertNotContains(resp, "Execute →")

    def test_each_role_only_sees_its_own_action_executing_eligible(self):
        tor = _make_executing_request(
            wallet=self.wallet, executed_by=self.other,
            requested_by=self.other, approved_by=self.other,
        )
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        for user, expect_recover in (
            (self.submit_only, False), (self.review_only, False),
            (self.execute_only, False), (self.recover_only, True),
            (self.superuser, True),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                resp = self.client.get(_change_url(tor.pk))
                self.assertEqual(resp.status_code, 200)
                if expect_recover:
                    self.assertContains(resp, "Mark as FAILED")
                else:
                    self.assertNotContains(resp, "Mark as FAILED")

    def test_post_actions_denied_outside_role(self):
        pending = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
        )
        self.client.force_login(self.execute_only)
        self.assertEqual(
            self.client.post(_approve_url(pending.pk), data={}).status_code, 403,
        )
        self.assertEqual(
            self.client.post(_reject_url(pending.pk), data={"rejection_reason": "x"}).status_code, 403,
        )

        approved = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_APPROVED, approved_by=self.other,
            approved_at=timezone.now(),
        )
        self.client.force_login(self.review_only)
        self.assertEqual(
            self.client.post(_execute_url(approved.pk), data={}).status_code, 403,
        )


# ─────────────────────────────────────────────
# Scenario 8 — direct URL access
# ─────────────────────────────────────────────

class DirectUrlAccessTests(TestCase):

    def setUp(self):
        self.executor = _make_executor(username="o3c5c_s8_executor")
        self.recoverer = _make_recoverer(username="o3c5c_s8_recoverer")
        self.no_perms = make_user(username="o3c5c_s8_no_perms", is_staff=True)
        self.other = make_user(username="o3c5c_s8_other")
        self.wallet = make_wallet()
        self.client = Client()

    def test_execute_without_permission_403(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_APPROVED, approved_by=self.other,
            approved_at=timezone.now(),
        )
        self.client.force_login(self.no_perms)
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_recover_without_permission_403(self):
        tor = _make_executing_request(wallet=self.wallet, executed_by=self.other,
                                       requested_by=self.other, approved_by=self.other)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        self.client.force_login(self.no_perms)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 403)

    def test_execute_nonexistent_pk_404(self):
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_recover_nonexistent_pk_404(self):
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_execute_stale_state_warns_and_redirects(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_EXECUTED, executed_by=self.other,
            executed_at=timezone.now(),
        )
        self.client.force_login(self.executor)
        resp = self.client.get(_execute_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está aprobada" in m for m in msgs))

    def test_recover_stale_state_warns_and_redirects(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_FAILED, executed_by=self.other,
            executed_at=timezone.now(), failure_reason="already resolved",
        )
        self.client.force_login(self.recoverer)
        resp = self.client.get(_recover_url(tor.pk), follow=True)
        self.assertRedirects(resp, _change_url(tor.pk))
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("ya no está en EXECUTING" in m for m in msgs))

    def test_execute_unauthenticated_redirects_to_login(self):
        tor = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT, wallet=self.wallet,
            amount=Decimal("10.00"), reason="x", requested_by=self.other,
            status=TreasuryOperationRequest.ST_APPROVED, approved_by=self.other,
            approved_at=timezone.now(),
        )
        resp = self.client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_recover_unauthenticated_redirects_to_login(self):
        tor = _make_executing_request(wallet=self.wallet, executed_by=self.other,
                                       requested_by=self.other, approved_by=self.other)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        resp = self.client.get(_recover_url(tor.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])


# ─────────────────────────────────────────────
# Invariants — re-affirmed explicitly across the two main pipelines
# ─────────────────────────────────────────────

class InvariantsTests(TestCase):
    """
    These invariants are already exercised implicitly by the scenario
    classes above; this class asserts them explicitly, once each, as a
    single point of reference for what O.3c-5c was required to confirm.
    """

    def setUp(self):
        self.submitter = _make_submitter(username="o3c5c_inv_submitter")
        self.reviewer = _make_reviewer(username="o3c5c_inv_reviewer")
        self.executor = _make_executor(username="o3c5c_inv_executor")
        self.recoverer = _make_recoverer(username="o3c5c_inv_recoverer")
        self.wallet = make_wallet()
        self.client = Client()

    def test_never_more_than_one_wallet_transaction_per_request(self):
        self.client.force_login(self.submitter)
        self.client.post(_submit_url(), data=_submit_data(self.wallet, amount="15.00"))
        tor = TreasuryOperationRequest.objects.get()
        self.client.force_login(self.reviewer)
        self.client.post(_approve_url(tor.pk), data={})
        self.client.force_login(self.executor)
        self.client.post(_execute_url(tor.pk), data={})
        self.client.post(_execute_url(tor.pk), data={})  # repeat attempt
        tor.refresh_from_db()
        self.assertEqual(
            WalletTransaction.objects.filter(treasury_request=tor).count(), 1,
        )

    def test_no_internal_transfer_anywhere_in_either_pipeline(self):
        self.client.force_login(self.submitter)
        self.client.post(_submit_url(), data=_submit_data(self.wallet, amount="15.00"))
        tor = TreasuryOperationRequest.objects.get()
        self.client.force_login(self.reviewer)
        self.client.post(_approve_url(tor.pk), data={})
        self.client.force_login(self.executor)
        self.client.post(_execute_url(tor.pk), data={})

        stuck = _make_executing_request(
            wallet=self.wallet, executed_by=self.executor,
            requested_by=self.submitter, approved_by=self.reviewer,
        )
        _started_audit_log(stuck.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        self.client.force_login(self.recoverer)
        self.client.post(_recover_url(stuck.pk), data={"recovery_reason": "x"})

        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_recovery_never_moves_money_or_links_wallet_transaction(self):
        stuck = _make_executing_request(
            wallet=self.wallet, executed_by=self.executor,
            requested_by=self.submitter, approved_by=self.reviewer,
        )
        _started_audit_log(stuck.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        balance_before = self.wallet.available_balance
        wtx_before = WalletTransaction.objects.count()

        self.client.force_login(self.recoverer)
        self.client.post(_recover_url(stuck.pk), data={"recovery_reason": "x"})

        stuck.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(stuck.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIsNone(stuck.wallet_transaction)
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.count(), wtx_before)
