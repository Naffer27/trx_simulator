# simulator/tests/test_withdrawal_auto_authorization.py
"""
WITHDRAWAL-POLICY-CORRECTION-01 — auto-authorization (<=$1,000, no staff
approval) + operational retry semantics.

Covers:
  1.  Boundary: $20/$100/$999/$1000 auto-authorized (required_approvals=1,
      no staff approval needed, payout submitted automatically exactly once).
  2.  Boundary: $1000.01/$1500 still require two distinct approvals — the
      auto-path never touches these rows.
  3.  reviewed_by stays None for auto-authorized withdrawals — never
      attributed to the withdrawing user as if they were a staff approver.
  4.  EV_WITHDRAW_AUTO_AUTHORIZED is logged exactly once, with
      authorization_method="kyc+totp+email_otp+verified_wallet".
  5.  The orchestrator's own EV_WITHDRAW_APPROVED audit line reads "system
      (auto-authorized)" for the auto-path, never the user's username —
      and still reads the real admin's username for the >$1,000 path
      (regression, unchanged).
  6.  Wallet is debited exactly once (at WithdrawalRequest creation) —
      the auto-payout trigger never debits again.
  7.  retry_auto_authorized_payout: eligibility is exactly
      status=PENDING + required_approvals=1; excludes required_approvals=2
      rows and rows already PROCESSING (an attempt already exists).
  8.  Retry uses actor=None (never the retrying admin) — reviewed_by stays
      None, EV_WITHDRAW_APPROVED still says "system (auto-authorized)".
  9.  Retry logs EV_WITHDRAW_PAYOUT_RETRY_TRIGGERED with triggered_by=<admin>
      — a distinct, non-approval event.
  10. Retry never creates a second PayoutAttempt nor a second Wallet debit.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import RequestFactory, TestCase

from simulator.admin import WithdrawalRequestAdmin, retry_auto_authorized_payout
from simulator.models import AuditLog, PayoutAttempt, WalletTransaction, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import (
    make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet, make_totp_device,
)
from simulator.tests.withdrawal_flow_helpers import (
    PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, PATCH_AUTO_PAYOUT_ADAPTER,
    full_withdraw_flow,
)
from simulator.wallet_ledger import debit_wallet


# ── End-to-end boundary tests ──────────────────────────────────────────────

class AutoAuthorizedBoundaryTests(TestCase):
    def setUp(self):
        self.user   = make_user(email="autoauth@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_totp_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def _withdraw(self, amount):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            return full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal(amount))

    def test_20_auto_authorized_no_approval(self):
        r1, r2, challenge = self._withdraw("20.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 1)
        self.assertIsNone(wr.reviewed_by)

    def test_100_auto_authorized(self):
        self._withdraw("100.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 1)

    def test_999_auto_authorized(self):
        self._withdraw("999.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 1)

    def test_1000_auto_authorized(self):
        self._withdraw("1000.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 1)

    def test_1000_01_needs_two_approvals_not_auto(self):
        self._withdraw("1000.01")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 2)

    def test_1500_needs_two_approvals_not_auto(self):
        self._withdraw("1500.00")
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertEqual(wr.required_approvals, 2)

    def test_payout_submitted_exactly_once_for_auto_path(self):
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            spy.side_effect = lambda *a, **k: {"outcome": "processing", "attempt_id": 1}
            self._withdraw("20.00")
            spy.assert_called_once()
            _, kwargs = spy.call_args
            self.assertIsNone(kwargs["actor"])

    def test_payout_not_submitted_for_above_1000(self):
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            self._withdraw("1500.00")
            spy.assert_not_called()

    def test_wallet_debited_exactly_once(self):
        self._withdraw("20.00")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("4980.00"))
        tx_count = WalletTransaction.objects.filter(
            wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW,
        ).count()
        self.assertEqual(tx_count, 1)


# ── Audit semantics ─────────────────────────────────────────────────────────

class AutoAuthorizationAuditTests(TestCase):
    def setUp(self):
        self.user   = make_user(email="auditauth@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_totp_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_auto_authorized_event_logged_once_with_method(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("20.00"))
        logs = AuditLog.objects.filter(event_type="withdrawal.auto_authorized")
        self.assertEqual(logs.count(), 1)
        self.assertEqual(
            logs.first().detail.get("authorization_method"),
            "kyc+totp+email_otp+verified_wallet",
        )

    def test_approved_audit_message_says_system_not_username_for_auto_path(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("20.00"))
        approved_logs = AuditLog.objects.filter(event_type="withdrawal.approved")
        self.assertEqual(approved_logs.count(), 1)
        self.assertIn("system (auto-authorized)", approved_logs.first().action)
        self.assertNotIn(self.user.username, approved_logs.first().action)

    def test_no_auto_authorized_event_for_above_1000(self):
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("1500.00"))
        self.assertEqual(AuditLog.objects.filter(event_type="withdrawal.auto_authorized").count(), 0)


# ── Retry: helpers ───────────────────────────────────────────────────────────

def _admin_request(admin_user):
    req = RequestFactory().post("/admin/")
    req.user = admin_user
    req._messages = CookieStorage(req)
    return req


def _make_admin():
    return WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())


def _make_auto_authorized_pending_wr(user, wallet, amount="500.00"):
    """
    Shape of a WithdrawalRequest that failed auto-submission before any
    PayoutAttempt was created (e.g. EstimateFailed) — status stays PENDING,
    reviewed_by stays None, required_approvals=1.
    """
    amount = Decimal(amount)
    debit_tx = debit_wallet(wallet.id, amount, WalletTransaction.TX_WITHDRAW, note="test auto wr")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=amount, crypto_currency="usdttrc20",
        wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        status=WithdrawalRequest.STATUS_PENDING, debit_tx=debit_tx,
        required_approvals=1,
    )


class RetryOperativeTests(TestCase):
    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("10000"))
        self.admin  = make_user(username="retry_admin", is_staff=True, is_superuser=True)

    def _retry(self, wr):
        with patch("simulator.payout_providers.NowPaymentsAdapter") as AdapterCls, \
             patch("simulator.tasks.send_email_async.delay"):
            from simulator.tests.withdrawal_flow_helpers import _FakeAutoPayoutAdapter
            AdapterCls.return_value = _FakeAutoPayoutAdapter()
            retry_auto_authorized_payout(
                _make_admin(), _admin_request(self.admin),
                WithdrawalRequest.objects.filter(pk=wr.pk),
            )

    def test_retry_eligible_pending_required_1_succeeds(self):
        wr = _make_auto_authorized_pending_wr(self.user, self.wallet)
        self._retry(wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)

    def test_retry_uses_actor_none_reviewed_by_stays_none(self):
        wr = _make_auto_authorized_pending_wr(self.user, self.wallet)
        self._retry(wr)
        wr.refresh_from_db()
        self.assertIsNone(wr.reviewed_by)

    def test_retry_logs_retry_event_not_approval(self):
        wr = _make_auto_authorized_pending_wr(self.user, self.wallet)
        self._retry(wr)
        retry_logs = AuditLog.objects.filter(event_type="withdrawal.payout_retry_triggered")
        self.assertEqual(retry_logs.count(), 1)
        self.assertEqual(retry_logs.first().detail.get("triggered_by"), "retry_admin")
        self.assertEqual(
            retry_logs.first().detail.get("reason"), "operational_retry_after_submission_failure",
        )
        approved_logs = AuditLog.objects.filter(event_type="withdrawal.approved")
        self.assertEqual(approved_logs.count(), 1)
        self.assertIn("system (auto-authorized)", approved_logs.first().action)
        self.assertNotIn("retry_admin", approved_logs.first().action)

    def test_retry_excludes_required_approvals_2_rows(self):
        amount = Decimal("2000.00")
        debit_tx = debit_wallet(self.wallet.id, amount, WalletTransaction.TX_WITHDRAW, note="dual")
        wr = WithdrawalRequest.objects.create(
            user=self.user, amount_usd=amount, crypto_currency="usdttrc20",
            wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            status=WithdrawalRequest.STATUS_PENDING, debit_tx=debit_tx,
            required_approvals=2,
        )
        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            retry_auto_authorized_payout(
                _make_admin(), _admin_request(self.admin),
                WithdrawalRequest.objects.filter(pk=wr.pk),
            )
            spy.assert_not_called()
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_retry_excludes_processing_rows(self):
        """A row already PROCESSING (PayoutAttempt exists) is untouched by retry."""
        wr = _make_auto_authorized_pending_wr(self.user, self.wallet)
        self._retry(wr)  # first, legitimate submission
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        attempts_before = PayoutAttempt.objects.filter(withdrawal_request=wr).count()

        with patch("simulator.payout_orchestrator.submit_withdrawal_to_provider") as spy:
            retry_auto_authorized_payout(
                _make_admin(), _admin_request(self.admin),
                WithdrawalRequest.objects.filter(pk=wr.pk),
            )
            spy.assert_not_called()
        self.assertEqual(
            PayoutAttempt.objects.filter(withdrawal_request=wr).count(), attempts_before,
        )

    def test_retry_does_not_double_debit_wallet(self):
        wr = _make_auto_authorized_pending_wr(self.user, self.wallet, amount="500.00")
        self.wallet.refresh_from_db()
        balance_before_retry = self.wallet.available_balance
        self._retry(wr)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before_retry)
