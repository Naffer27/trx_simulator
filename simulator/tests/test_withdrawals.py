# simulator/tests/test_withdrawals.py
"""
Withdrawal safety tests — double-debit prevention, atomicity, idempotency,
audit logging, email notifications, and 2FA enforcement.

Covers:
  - Sufficient balance required to create withdrawal
  - Successful withdrawal creates pending WR and debits wallet once
  - Duplicate PENDING withdrawal rejected with clear error
  - Double-click (two requests in sequence while first still pending) rejected
  - Failed atomic creation does not debit wallet
  - payout_callback: FAILED status refunds wallet
  - payout_callback: COMPLETED status does NOT refund wallet
  - payout_callback: idempotent — second FAILED IPN does not double-refund
  - User cannot see/affect another user's withdrawals
  - Audit log created at every lifecycle stage
  - User and admin emails queued at request; user email queued at approve/reject/callback
  - Wallet address masked in all outbound emails
  - 2FA required for all withdrawal requests
"""
import json
from decimal import Decimal
from unittest.mock import patch, call

from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model

from simulator.models import (
    AuditLog, Wallet, WalletTransaction, WithdrawalRequest, TOTPDevice,
)
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved

User = get_user_model()

WITHDRAW_URL  = "/withdraw/"
CALLBACK_URL  = "/withdraw/callback/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")
# Bypass the real TOTP verification for tests that are not testing 2FA itself
_PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)


def _make_device(user) -> TOTPDevice:
    """Create a confirmed TOTPDevice for *user* with a dummy secret."""
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",  # base64-safe dummy
        confirmed=True,
    )


def _wr_payload(amount="50.00"):
    return {
        "amount_usd":      amount,
        "crypto_currency": "btc",
        "wallet_address":  "bc1qtest000000000000000000000000000000000",
        "otp_code":        "000000",  # dummy; real verification patched in tests
    }


def _payout_ipn(wr_id: int, np_status: str, payout_id: str = "np_pay_001", batch_id: str = "batch_001"):
    return json.dumps({
        "id":     batch_id,
        "status": np_status,
        "withdrawals": [
            {"id": payout_id, "status": np_status},
        ],
    })


# ── Happy path ────────────────────────────────────────────────────────────────

class WithdrawalCreationTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="wd@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    def test_withdrawal_requires_sufficient_balance(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("300.00"))
        self.assertEqual(r.status_code, 200)  # re-renders form with error
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_creates_pending_request(self):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        wr = WithdrawalRequest.objects.filter(user=self.user).first()
        self.assertIsNotNone(wr)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(wr.amount_usd, Decimal("50.00"))

    def test_withdrawal_debits_wallet_exactly_once(self):
        self.client.post(WITHDRAW_URL, _wr_payload("75.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("125.00"))

    def test_withdrawal_creates_debit_tx(self):
        self.client.post(WITHDRAW_URL, _wr_payload("40.00"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        self.assertIsNotNone(wr.debit_tx_id)
        self.assertEqual(wr.debit_tx.tx_type, WalletTransaction.TX_WITHDRAW)

    def test_insufficient_balance_does_not_debit_wallet(self):
        self.client.post(WITHDRAW_URL, _wr_payload("9999.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200"))

    def test_successful_withdrawal_redirects(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertRedirects(r, reverse("simulator:withdraw_history"), fetch_redirect_response=False)


# ── Duplicate PENDING guard ───────────────────────────────────────────────────

class DuplicatePendingWithdrawalTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="dup@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    def test_duplicate_pending_withdrawal_rejected(self):
        """Second withdrawal request while first is PENDING must be rejected."""
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(r.status_code, 200)  # re-renders form with error
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)

    def test_duplicate_pending_does_not_double_debit_wallet(self):
        """Wallet must be debited only once even if user submits twice."""
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("450.00"))

    def test_duplicate_pending_shows_clear_error_message(self):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertContains(r, "pendiente")

    def test_can_withdraw_again_after_first_is_no_longer_pending(self):
        """Once the first WR is no longer PENDING, a new one can be created."""
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        wr.status = WithdrawalRequest.STATUS_COMPLETED
        wr.save()

        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 2)

    def test_rejected_wr_allows_new_withdrawal(self):
        """After admin rejects, user should be able to request again."""
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        wr = WithdrawalRequest.objects.get(user=self.user)
        wr.status = WithdrawalRequest.STATUS_REJECTED
        wr.save()

        self.client.post(WITHDRAW_URL, _wr_payload("30.00"))
        self.assertEqual(
            WithdrawalRequest.objects.filter(user=self.user, status=WithdrawalRequest.STATUS_PENDING).count(),
            1,
        )


# ── Atomic rollback ───────────────────────────────────────────────────────────

class WithdrawalAtomicRollbackTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="atomic@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("300"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    def test_failed_wr_create_does_not_debit_wallet(self):
        """If WithdrawalRequest.create() fails, the wallet debit must roll back."""
        with patch(
            "simulator.views.WithdrawalRequest.objects.create",
            side_effect=Exception("DB error"),
        ):
            self.client.post(WITHDRAW_URL, _wr_payload("100.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("300"))

    def test_failed_wr_create_does_not_create_wr(self):
        with patch(
            "simulator.views.WithdrawalRequest.objects.create",
            side_effect=Exception("DB error"),
        ):
            self.client.post(WITHDRAW_URL, _wr_payload("100.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_failed_wr_create_shows_generic_error(self):
        with patch(
            "simulator.views.WithdrawalRequest.objects.create",
            side_effect=Exception("DB error"),
        ):
            r = self.client.post(WITHDRAW_URL, _wr_payload("100.00"))
        self.assertEqual(r.status_code, 200)


# ── Payout callback — refund on FAILED ───────────────────────────────────────

_PATCH_PAYOUT_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


class PayoutCallbackRefundTests(TestCase):
    def setUp(self):
        _PATCH_PAYOUT_RATELIMIT.start()
        self.user   = make_user(email="refund@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))

    def tearDown(self):
        _PATCH_PAYOUT_RATELIMIT.stop()

    def _make_wr(self, amount="80.00", status=WithdrawalRequest.STATUS_APPROVED):
        """Create a WR that's already been debited (simulating post-approval state)."""
        from simulator.wallet_ledger import debit_wallet
        from simulator.models import WalletTransaction
        debit_tx = debit_wallet(
            self.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW,
            note="test wr debit",
        )
        return WithdrawalRequest.objects.create(
            user=self.user,
            amount_usd=Decimal(amount),
            crypto_currency="btc",
            wallet_address="bc1qtest",
            status=status,
            np_payout_id="np_pay_001",
            np_batch_id="batch_001",
            debit_tx=debit_tx,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_rejected_withdrawal_refunds_wallet(self, _sig):
        wr = self._make_wr()
        body = _payout_ipn(wr.id, "FAILED")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.wallet.refresh_from_db()
        # original 200 - 80 (debit) + 80 (refund) = 200
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_failed_wr_status_updated(self, _sig):
        wr = self._make_wr()
        body = _payout_ipn(wr.id, "FAILED")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_FAILED)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_completed_withdrawal_does_not_refund_wallet(self, _sig):
        wr = self._make_wr()
        body = _payout_ipn(wr.id, "FINISHED")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.wallet.refresh_from_db()
        # original 200 - 80 (debit); no refund on FINISHED
        self.assertEqual(self.wallet.available_balance, Decimal("120.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_double_failed_ipn_does_not_double_refund(self, _sig):
        """Second FAILED IPN for already-failed WR must not credit wallet twice."""
        wr = self._make_wr()
        body = _payout_ipn(wr.id, "FAILED")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.wallet.refresh_from_db()
        # No double credit — idempotency guard in withdraw_payout_callback
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))


# ── User isolation ────────────────────────────────────────────────────────────

class WithdrawalUserIsolationTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user_a  = make_user()
        self.wallet_a = make_wallet(self.user_a, initial_balance=Decimal("300"))
        self.user_b  = make_user()
        self.wallet_b = make_wallet(self.user_b, initial_balance=Decimal("300"))
        _make_device(self.user_a)
        _make_device(self.user_b)
        make_kyc_approved(self.user_a)
        make_kyc_approved(self.user_b)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    def test_user_a_pending_does_not_block_user_b(self):
        """User A's pending withdrawal must not prevent User B from withdrawing."""
        # Create pending for User A
        WithdrawalRequest.objects.create(
            user=self.user_a,
            amount_usd=Decimal("50"),
            crypto_currency="btc",
            wallet_address="bc1qtest",
            status=WithdrawalRequest.STATUS_PENDING,
        )
        # User B should still be able to withdraw
        self.client.force_login(self.user_b)
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(
            WithdrawalRequest.objects.filter(
                user=self.user_b, status=WithdrawalRequest.STATUS_PENDING
            ).count(),
            1,
        )

    def test_withdraw_only_debits_requesting_user_wallet(self):
        self.client.force_login(self.user_a)
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.wallet_b.refresh_from_db()
        self.assertEqual(self.wallet_b.available_balance, Decimal("300"))


# ── Audit log ────────────────────────────────────────────────────────────────

_PATCH_EMAIL = patch("simulator.tasks.send_email_async.delay")


class WithdrawalAuditLogTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="audit@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()
        _PATCH_TOTP.stop()

    def test_requested_creates_audit_log(self):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.requested").exists()
        )

    def test_requested_audit_log_has_correct_user(self):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        log = AuditLog.objects.get(event_type="withdrawal.requested")
        self.assertEqual(log.user, self.user)

    def test_requested_audit_log_detail_has_withdrawal_id(self):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        log = AuditLog.objects.get(event_type="withdrawal.requested")
        self.assertIn("withdrawal_id", log.detail)

    def _make_approved_wr(self, payout_id="np_pay_001", batch_id="batch_001", amount="80.00"):
        from simulator.wallet_ledger import debit_wallet
        debit_tx = debit_wallet(
            self.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="test"
        )
        return WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal(amount), crypto_currency="btc",
            wallet_address="bc1qtest", status=WithdrawalRequest.STATUS_APPROVED,
            np_payout_id=payout_id, np_batch_id=batch_id, debit_tx=debit_tx,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_failed_callback_creates_failed_audit_log(self, _sig):
        """FAILED IPN creates withdrawal.failed audit entry."""
        wr = self._make_approved_wr()
        self.client.post(CALLBACK_URL, _payout_ipn(wr.id, "FAILED"),
                         content_type="application/json")
        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.failed").exists()
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_failed_callback_creates_refunded_audit_log(self, _sig):
        """FAILED IPN also creates withdrawal.refunded audit entry (funds returned)."""
        wr = self._make_approved_wr(payout_id="np_pay_002", batch_id="batch_002")
        self.client.post(CALLBACK_URL, _payout_ipn(wr.id, "FAILED", "np_pay_002", "batch_002"),
                         content_type="application/json")
        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.refunded").exists()
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_completed_callback_creates_audit_log(self, _sig):
        wr = self._make_approved_wr(payout_id="np_pay_003", batch_id="batch_003")
        self.client.post(CALLBACK_URL, _payout_ipn(wr.id, "FINISHED", "np_pay_003", "batch_003"),
                         content_type="application/json")
        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.completed").exists()
        )


class WithdrawalAdminAuditLogTests(TestCase):
    """Audit logs for admin approve and reject actions."""

    def setUp(self):
        self.admin = make_user(email="admin@test.com", is_staff=True, is_superuser=True)
        self.user  = make_user(email="auser@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        from simulator.wallet_ledger import debit_wallet
        self.debit_tx = debit_wallet(
            self.wallet.id, Decimal("80"), WalletTransaction.TX_WITHDRAW, note="test"
        )
        self.wr = WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("80"), crypto_currency="btc",
            wallet_address="bc1qtest000000000000000000000000000000000",
            status=WithdrawalRequest.STATUS_PENDING,
            debit_tx=self.debit_tx,
        )

    def _admin_request(self):
        from django.contrib.messages.storage.cookie import CookieStorage
        request = RequestFactory().post("/admin/")
        request.user = self.admin
        request._messages = CookieStorage(request)
        return request

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.estimate_price", return_value=Decimal("0.001"))
    @patch("simulator.nowpayments.create_payout", return_value={
        "id": "batch_01", "status": "CREATED",
        "withdrawals": [{"id": "pay_01"}],
    })
    def test_approved_creates_audit_log(self, _payout, _price, _email):
        from simulator.admin import approve_withdrawals
        from django.contrib.admin.sites import AdminSite
        from simulator.admin import WithdrawalRequestAdmin

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        approve_withdrawals(ma, self._admin_request(),
                            WithdrawalRequest.objects.filter(pk=self.wr.pk))

        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.approved").exists()
        )

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_creates_audit_log(self, _email):
        from simulator.admin import reject_withdrawals
        from django.contrib.admin.sites import AdminSite
        from simulator.admin import WithdrawalRequestAdmin

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, self._admin_request(),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        self.assertTrue(
            AuditLog.objects.filter(event_type="withdrawal.rejected").exists()
        )

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_audit_log_detail_has_reviewed_by(self, _email):
        from simulator.admin import reject_withdrawals
        from django.contrib.admin.sites import AdminSite
        from simulator.admin import WithdrawalRequestAdmin

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, self._admin_request(),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        log = AuditLog.objects.get(event_type="withdrawal.rejected")
        self.assertEqual(log.detail.get("reviewed_by"), self.admin.username)


# ── Email notifications ───────────────────────────────────────────────────────

class WithdrawalEmailTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="emailtest@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    @patch("simulator.tasks.send_email_async.delay")
    def test_user_confirmation_email_queued_on_request(self, mock_delay):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        # At least one call whose recipient_list includes the user's email
        user_email_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertTrue(len(user_email_calls) >= 1)

    @patch("simulator.tasks.send_email_async.delay")
    def test_admin_notification_email_queued_on_request(self, mock_delay):
        from django.conf import settings
        admin_email = settings.ADMINS[0][1] if settings.ADMINS else None
        if not admin_email:
            self.skipTest("ADMINS not configured")
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        admin_calls = [
            c for c in mock_delay.call_args_list
            if admin_email in c.kwargs.get("recipient_list", [])
        ]
        self.assertTrue(len(admin_calls) >= 1)

    @patch("simulator.tasks.send_email_async.delay")
    def test_wallet_address_masked_in_user_email(self, mock_delay):
        """Full wallet address must NOT appear in any outbound email body."""
        full_addr = "bc1qtest000000000000000000000000000000000"
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        for c in mock_delay.call_args_list:
            body = c.kwargs.get("message", "")
            self.assertNotIn(full_addr, body,
                             "Full wallet address leaked in email body")

    @patch("simulator.tasks.send_email_async.delay")
    def test_wallet_address_masked_format_in_user_email(self, mock_delay):
        """Masked address in the format 'bc1qte...0000' must appear in user email."""
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertTrue(len(user_calls) >= 1)
        body = user_calls[0].kwargs.get("message", "")
        self.assertIn("bc1qte...0000", body)

    @patch("simulator.tasks.send_email_async.delay")
    def test_email_failure_does_not_break_withdrawal_creation(self, mock_delay):
        """If email queuing raises, the WR must still be created."""
        mock_delay.side_effect = Exception("Celery down")
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_completed_callback_email_queued(self, _sig, mock_delay):
        from simulator.wallet_ledger import debit_wallet
        debit_tx = debit_wallet(
            self.wallet.id, Decimal("80"), WalletTransaction.TX_WITHDRAW, note="test"
        )
        wr = WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("80"), crypto_currency="btc",
            wallet_address="bc1qtest000000000000000000000000000000000",
            status=WithdrawalRequest.STATUS_APPROVED,
            np_payout_id="np_pay_099", np_batch_id="batch_099", debit_tx=debit_tx,
        )
        mock_delay.reset_mock()
        self.client.post(CALLBACK_URL,
                         _payout_ipn(wr.id, "FINISHED", "np_pay_099", "batch_099"),
                         content_type="application/json")
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertTrue(len(user_calls) >= 1)

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_completed_callback_wallet_address_masked(self, _sig, mock_delay):
        """Full wallet address must not appear in the COMPLETED callback email."""
        from simulator.wallet_ledger import debit_wallet
        full_addr = "bc1qtest000000000000000000000000000000000"
        debit_tx = debit_wallet(
            self.wallet.id, Decimal("80"), WalletTransaction.TX_WITHDRAW, note="test"
        )
        wr = WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("80"), crypto_currency="btc",
            wallet_address=full_addr,
            status=WithdrawalRequest.STATUS_APPROVED,
            np_payout_id="np_pay_100", np_batch_id="batch_100", debit_tx=debit_tx,
        )
        mock_delay.reset_mock()
        self.client.post(CALLBACK_URL,
                         _payout_ipn(wr.id, "FINISHED", "np_pay_100", "batch_100"),
                         content_type="application/json")
        for c in mock_delay.call_args_list:
            body = c.kwargs.get("message", "")
            self.assertNotIn(full_addr, body)


# ── 2FA enforcement ───────────────────────────────────────────────────────────

class WithdrawalTwoFATests(TestCase):
    """Verify that TOTP 2FA is enforced for every withdrawal request."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.mock_email = _PATCH_EMAIL.start()
        self.user   = make_user(email="2fa@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()

    def test_withdrawal_without_2fa_device_rejected(self):
        """No confirmed TOTPDevice → rejected with 2FA error, no WR created."""
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "2FA")
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_with_2fa_enabled_requires_code(self):
        """Device present but code is wrong → rejected, no WR created."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_with_missing_otp_rejected_no_debit(self):
        """POST without otp_code field → verify fails → no WR, wallet unchanged."""
        _make_device(self.user)
        payload = _wr_payload("50.00")
        del payload["otp_code"]
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            self.client.post(WITHDRAW_URL, payload)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_with_invalid_otp_rejected_no_debit(self):
        """Invalid TOTP code → no WR created, wallet not debited."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_withdrawal_with_valid_otp_creates_pending_request(self):
        """Valid TOTP code → WR created as PENDING."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=True):
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        wr = WithdrawalRequest.objects.filter(user=self.user).first()
        self.assertIsNotNone(wr)
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_withdrawal_with_valid_otp_debits_wallet_once(self):
        """Valid TOTP code → wallet debited exactly once."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=True):
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("150.00"))

    def test_duplicate_pending_still_rejected_with_valid_otp(self):
        """Even with valid 2FA, duplicate PENDING WR is blocked; wallet debited once."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=True):
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("150.00"))

    def test_failed_otp_does_not_send_emails(self):
        """Invalid TOTP code → no user or admin email queued."""
        _make_device(self.user)
        self.mock_email.reset_mock()
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.mock_email.assert_not_called()

    def test_failed_otp_creates_security_log(self):
        """Invalid TOTP code → security warning emitted to simulator.security logger."""
        _make_device(self.user)
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            with self.assertLogs("simulator.security", level="WARNING") as cm:
                self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertTrue(any("withdrawal.2fa_failed" in line for line in cm.output))
