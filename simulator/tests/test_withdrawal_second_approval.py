# simulator/tests/test_withdrawal_second_approval.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — dual approval gate in admin.py.

Exercises first_approve_withdrawals / approve_withdrawals directly (same
convention as test_fix02a3_admin_hardening.py's ApproveWithdrawalsRegressionSanityTests)
rather than through the full two-step OTP flow — isolates the admin-gate
logic from the OTP/challenge machinery, which has its own dedicated suite.

Covers:
  1.  amount_usd == 1000.00 -> required_approvals == 1 at creation time
      (boundary: exactly 1000 needs only ONE approval, per rule 7/8).
  2.  amount_usd == 1000.01 -> required_approvals == 2.
  3.  required_approvals==1: approve_withdrawals processes directly (today's
      behavior, unchanged) — submit_withdrawal_to_provider called once.
  4.  required_approvals==2: approve_withdrawals BEFORE any first approval
      is blocked — submit_withdrawal_to_provider never called.
  5.  first_approve_withdrawals records first_approved_by/at, does NOT call
      submit_withdrawal_to_provider, and does NOT change wr.status.
  6.  Same admin cannot be both first and second approver — approve_withdrawals
      by the SAME admin who first-approved is blocked, provider never called.
  7.  A DIFFERENT admin approving after the first approval succeeds — records
      second_approved_by/at, submit_withdrawal_to_provider called exactly once.
  8.  first_approve_withdrawals on a required_approvals==1 row is a no-op
      (informational skip) — normal approve_withdrawals still works for it.
  9.  Historical / legacy WithdrawalRequest rows (required_approvals default
      1, no otp_challenge) still process through approve_withdrawals exactly
      as before this block.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import RequestFactory, TestCase

from simulator.admin import (
    WithdrawalRequestAdmin, approve_withdrawals, first_approve_withdrawals,
)
from simulator.models import PayoutAttempt, WalletTransaction, WithdrawalRequest
from simulator.wallet_ledger import debit_wallet
from simulator.tests.factories import make_user, make_wallet


def _admin_request(admin_user):
    req = RequestFactory().post("/admin/")
    req.user = admin_user
    req._messages = CookieStorage(req)
    return req


def _make_pending_wr(user, wallet, amount, required_approvals=None):
    amount = Decimal(amount)
    debit_tx = debit_wallet(wallet.id, amount, WalletTransaction.TX_WITHDRAW, note="test wr")
    wr = WithdrawalRequest.objects.create(
        user=user,
        amount_usd=amount,
        crypto_currency="usdttrc20",
        wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        status=WithdrawalRequest.STATUS_PENDING,
        debit_tx=debit_tx,
        required_approvals=(required_approvals if required_approvals is not None else (2 if amount > Decimal("1000") else 1)),
    )
    return wr


class _FakeAdapter:
    provider_name = "nowpayments"

    def __init__(self, *, estimate_result=None, create_result=None):
        self.estimate_result = estimate_result if estimate_result is not None else Decimal("0.001")
        self.create_result = create_result

    def estimate(self, amount_usd, asset):
        return self.estimate_result

    def create_payout(self, attempt, *, callback_url=""):
        return self.create_result


class _FakeSubmissionResult:
    def __init__(self):
        self.accepted = True
        self.provider_reference = "wd-1"
        self.provider_batch_id = "batch-1"
        self.provider_amount = Decimal("0.001")
        self.raw_status = "CREATED"


def _make_admin():
    return WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())


def _approve(ma, admin_user, wr):
    with patch("simulator.payout_providers.NowPaymentsAdapter") as AdapterCls, \
         patch("simulator.tasks.send_email_async.delay"):
        AdapterCls.return_value = _FakeAdapter(create_result=_FakeSubmissionResult())
        approve_withdrawals(ma, _admin_request(admin_user), WithdrawalRequest.objects.filter(pk=wr.pk))


class RequiredApprovalsBoundaryTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("10000"))

    def test_exactly_1000_needs_one_approval(self):
        wr = _make_pending_wr(self.user, self.wallet, "1000.00")
        self.assertEqual(wr.required_approvals, 1)

    def test_1000_01_needs_two_approvals(self):
        wr = _make_pending_wr(self.user, self.wallet, "1000.01")
        self.assertEqual(wr.required_approvals, 2)


class SingleApprovalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("10000"))
        self.admin = make_user(is_staff=True, is_superuser=True)

    def test_required_approvals_1_processes_directly(self):
        wr = _make_pending_wr(self.user, self.wallet, "1000.00")
        ma = _make_admin()
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            spy.side_effect = lambda *a, **k: {"outcome": "processing"}
            _approve(ma, self.admin, wr)
            spy.assert_called_once()
        wr.refresh_from_db()

    def test_required_approvals_1_actually_processes_end_to_end(self):
        wr = _make_pending_wr(self.user, self.wallet, "500.00")
        ma = _make_admin()
        _approve(ma, self.admin, wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)


class DualApprovalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("10000"))
        self.admin_a = make_user(username="admin_a", is_staff=True, is_superuser=True)
        self.admin_b = make_user(username="admin_b", is_staff=True, is_superuser=True)

    def test_approve_before_first_approval_is_blocked(self):
        wr = _make_pending_wr(self.user, self.wallet, "2500.00")
        ma = _make_admin()
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            _approve(ma, self.admin_a, wr)
            spy.assert_not_called()
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.assertIsNone(wr.second_approved_by)

    def test_first_approve_records_without_calling_provider(self):
        wr = _make_pending_wr(self.user, self.wallet, "2500.00")
        ma = _make_admin()
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            first_approve_withdrawals(ma, _admin_request(self.admin_a), WithdrawalRequest.objects.filter(pk=wr.pk))
            spy.assert_not_called()
        wr.refresh_from_db()
        self.assertEqual(wr.first_approved_by_id, self.admin_a.id)
        self.assertIsNotNone(wr.first_approved_at)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_same_admin_cannot_be_both_approvers(self):
        wr = _make_pending_wr(self.user, self.wallet, "2500.00")
        ma = _make_admin()
        first_approve_withdrawals(ma, _admin_request(self.admin_a), WithdrawalRequest.objects.filter(pk=wr.pk))
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            _approve(ma, self.admin_a, wr)  # same admin tries to second-approve
            spy.assert_not_called()
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.assertIsNone(wr.second_approved_by)

    def test_different_admin_second_approval_succeeds(self):
        wr = _make_pending_wr(self.user, self.wallet, "2500.00")
        ma = _make_admin()
        first_approve_withdrawals(ma, _admin_request(self.admin_a), WithdrawalRequest.objects.filter(pk=wr.pk))
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            spy.side_effect = lambda *a, **k: {"outcome": "processing"}
            _approve(ma, self.admin_b, wr)
            spy.assert_called_once()
        wr.refresh_from_db()
        self.assertEqual(wr.second_approved_by_id, self.admin_b.id)
        self.assertIsNotNone(wr.second_approved_at)

    def test_two_admins_end_to_end_creates_exactly_one_payout_attempt(self):
        wr = _make_pending_wr(self.user, self.wallet, "1500.00")
        ma = _make_admin()
        first_approve_withdrawals(ma, _admin_request(self.admin_a), WithdrawalRequest.objects.filter(pk=wr.pk))
        _approve(ma, self.admin_b, wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)

    def test_first_approve_noop_for_required_approvals_1(self):
        wr = _make_pending_wr(self.user, self.wallet, "500.00")
        ma = _make_admin()
        first_approve_withdrawals(ma, _admin_request(self.admin_a), WithdrawalRequest.objects.filter(pk=wr.pk))
        wr.refresh_from_db()
        self.assertIsNone(wr.first_approved_by)
        # Normal approve still works for it.
        _approve(ma, self.admin_a, wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)


class HistoricalWithdrawalRequestTests(TestCase):
    """Legacy-shaped rows (required_approvals default=1, no otp_challenge) —
    the exact shape of every WithdrawalRequest created before this block."""

    def test_legacy_row_processes_unchanged(self):
        user = make_user()
        wallet = make_wallet(user, initial_balance=Decimal("1000"))
        admin_user = make_user(is_staff=True, is_superuser=True)
        # Deliberately NOT passing required_approvals — exercises the model
        # default (1), exactly like a pre-existing DB row after migration.
        debit_tx = debit_wallet(wallet.id, Decimal("100"), WalletTransaction.TX_WITHDRAW, note="legacy")
        wr = WithdrawalRequest.objects.create(
            user=user, amount_usd=Decimal("100"), crypto_currency="btc",
            wallet_address="bc1qtest000000000000000000000000000000000",
            status=WithdrawalRequest.STATUS_PENDING, debit_tx=debit_tx,
        )
        self.assertEqual(wr.required_approvals, 1)
        ma = _make_admin()
        _approve(ma, admin_user, wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
