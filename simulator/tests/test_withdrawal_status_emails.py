# simulator/tests/test_withdrawal_status_emails.py
"""
Withdrawal status notification emails — send_withdrawal_status_email().

Integration tests verify that each lifecycle event causes exactly one
user-facing email to be queued via send_email_async.delay.

Content tests verify subject and body requirements via the helper directly:
  - subject always contains "Money Broker"
  - body always contains: monto, moneda, estado, fecha

Idempotency test: a second NP callback for an already-terminal withdrawal
must NOT queue a second email.

Covers:
  1.  requested — email queued when withdrawal is created via POST /withdraw/
  2.  approved  — email queued when admin action approves the withdrawal
  3.  rejected  — email queued when admin action rejects the withdrawal
  4.  completed — email queued when NP callback marks withdrawal FINISHED
  5.  failed    — email queued when NP callback marks withdrawal FAILED
  6.  no-duplicate — second FINISHED callback after COMPLETED sends no email
  7.  subject contains "Money Broker" for all five events
  8.  body contains amount for all five events
  9.  body contains currency for all five events
  10. body contains status label for all five events
  11. body contains date marker for all five events
  12. rejected email includes admin_note when present
  13. rejected email omits note line when admin_note is blank
"""
import json
from decimal import Decimal
from unittest.mock import patch, call

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import TestCase, RequestFactory
from django.utils import timezone

from simulator.models import Wallet, WalletTransaction, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved
from simulator.withdrawal_emails import (
    send_withdrawal_status_email,
    EVENT_REQUESTED, EVENT_APPROVED, EVENT_REJECTED,
    EVENT_COMPLETED, EVENT_FAILED,
)

WITHDRAW_URL  = "/withdraw/"
CALLBACK_URL  = "/withdraw/callback/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")
_PATCH_SIG       = patch("simulator.nowpayments.verify_ipn_signature", return_value=True)


def _make_device(user) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
        confirmed=True,
    )


def _wr_payload(amount="50.00"):
    return {
        "amount_usd":      amount,
        "crypto_currency": "btc",
        "wallet_address":  "bc1qtest000000000000000000000000000000000",
        "otp_code":        "000000",
    }


def _ipn(payout_id: str, np_status: str, batch_id: str = "batch_x") -> str:
    return json.dumps({
        "id":     batch_id,
        "status": np_status,
        "withdrawals": [{"id": payout_id, "status": np_status}],
    })


def _admin_request(user):
    req = RequestFactory().post("/admin/")
    req.user = user
    req._messages = CookieStorage(req)
    return req


def _make_pending_wr(user, wallet, amount="80.00"):
    from simulator.wallet_ledger import debit_wallet
    debit_tx = debit_wallet(
        wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="test"
    )
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PENDING,
        debit_tx=debit_tx,
    )


def _make_approved_wr(user, wallet, payout_id="pay_001", batch_id="bat_001", amount="80.00"):
    from simulator.wallet_ledger import debit_wallet
    debit_tx = debit_wallet(
        wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="test"
    )
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_APPROVED,
        np_payout_id=payout_id, np_batch_id=batch_id,
        debit_tx=debit_tx,
    )


# ── 1. requested ─────────────────────────────────────────────────────────────

class RequestedEmailTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        self.user   = make_user(email="req@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()

    @_PATCH_EMAIL
    def test_requested_queues_user_email(self, mock_delay):
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertGreaterEqual(len(user_calls), 1)

    @_PATCH_EMAIL
    def test_requested_email_failure_does_not_abort_withdrawal(self, mock_delay):
        mock_delay.side_effect = Exception("Celery down")
        self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 1)


# ── 2. approved ──────────────────────────────────────────────────────────────

class ApprovedEmailTests(TestCase):
    def setUp(self):
        self.admin  = make_user(email="admin@test.com", is_staff=True, is_superuser=True)
        self.user   = make_user(email="approveduser@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr     = _make_pending_wr(self.user, self.wallet)

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.estimate_price", return_value=Decimal("0.001"))
    @patch("simulator.nowpayments.create_payout", return_value={
        "id": "bat_a", "status": "CREATED",
        "withdrawals": [{"id": "pay_a"}],
    })
    def test_approved_queues_user_email(self, _payout, _price, mock_delay):
        from simulator.admin import approve_withdrawals, WithdrawalRequestAdmin
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        approve_withdrawals(ma, _admin_request(self.admin),
                            WithdrawalRequest.objects.filter(pk=self.wr.pk))
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertGreaterEqual(len(user_calls), 1)


# ── 3. rejected ──────────────────────────────────────────────────────────────

class RejectedEmailTests(TestCase):
    def setUp(self):
        self.admin  = make_user(email="admin2@test.com", is_staff=True, is_superuser=True)
        self.user   = make_user(email="rejecteduser@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr     = _make_pending_wr(self.user, self.wallet)

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_queues_user_email(self, mock_delay):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, _admin_request(self.admin),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertGreaterEqual(len(user_calls), 1)


# ── 4. completed ─────────────────────────────────────────────────────────────

class CompletedEmailTests(TestCase):
    def setUp(self):
        self.user   = make_user(email="completed@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr     = _make_approved_wr(self.user, self.wallet,
                                        payout_id="pay_c1", batch_id="bat_c1")

    @patch("simulator.tasks.send_email_async.delay")
    @_PATCH_SIG
    def test_completed_callback_queues_user_email(self, _sig, mock_delay):
        self.client.post(
            CALLBACK_URL, _ipn("pay_c1", "FINISHED", "bat_c1"),
            content_type="application/json",
        )
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertGreaterEqual(len(user_calls), 1)


# ── 5. failed ────────────────────────────────────────────────────────────────

class FailedEmailTests(TestCase):
    def setUp(self):
        self.user   = make_user(email="failed@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr     = _make_approved_wr(self.user, self.wallet,
                                        payout_id="pay_f1", batch_id="bat_f1")

    @patch("simulator.tasks.send_email_async.delay")
    @_PATCH_SIG
    def test_failed_callback_queues_user_email(self, _sig, mock_delay):
        self.client.post(
            CALLBACK_URL, _ipn("pay_f1", "FAILED", "bat_f1"),
            content_type="application/json",
        )
        user_calls = [
            c for c in mock_delay.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertGreaterEqual(len(user_calls), 1)


# ── 6. no-duplicate ───────────────────────────────────────────────────────────

class NoDuplicateEmailTests(TestCase):
    """Second callback for a terminal WR must not queue another email."""

    def setUp(self):
        self.user   = make_user(email="nodup@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr     = _make_approved_wr(self.user, self.wallet,
                                        payout_id="pay_d1", batch_id="bat_d1")

    @patch("simulator.tasks.send_email_async.delay")
    @_PATCH_SIG
    def test_second_finished_callback_sends_no_extra_email(self, _sig, mock_delay):
        # First callback → WR becomes COMPLETED, email queued
        self.client.post(
            CALLBACK_URL, _ipn("pay_d1", "FINISHED", "bat_d1"),
            content_type="application/json",
        )
        calls_after_first = mock_delay.call_count

        # Second callback → WR already COMPLETED (terminal) → skip
        self.client.post(
            CALLBACK_URL, _ipn("pay_d1", "FINISHED", "bat_d1"),
            content_type="application/json",
        )
        self.assertEqual(mock_delay.call_count, calls_after_first,
                         "No additional email should be queued on duplicate callback")

    @patch("simulator.tasks.send_email_async.delay")
    @_PATCH_SIG
    def test_second_failed_callback_sends_no_extra_email(self, _sig, mock_delay):
        self.client.post(
            CALLBACK_URL, _ipn("pay_d1", "FAILED", "bat_d1"),
            content_type="application/json",
        )
        calls_after_first = mock_delay.call_count
        self.client.post(
            CALLBACK_URL, _ipn("pay_d1", "FAILED", "bat_d1"),
            content_type="application/json",
        )
        self.assertEqual(mock_delay.call_count, calls_after_first)


# ── 7-11. Content tests (via helper directly) ─────────────────────────────────

class WithdrawalEmailContentTests(TestCase):
    """
    Verify subject and body requirements for every event without going
    through views — call the helper directly and inspect delay() args.
    """

    def setUp(self):
        self.user   = make_user(email="content@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        from simulator.wallet_ledger import debit_wallet
        debit_tx = debit_wallet(
            self.wallet.id, Decimal("75"), WalletTransaction.TX_WITHDRAW, note="test"
        )
        self.wr = WithdrawalRequest.objects.create(
            user=self.user, amount_usd=Decimal("75"), crypto_currency="btc",
            wallet_address="bc1qtest000000000000000000000000000000000",
            status=WithdrawalRequest.STATUS_PENDING, debit_tx=debit_tx,
            admin_note="",
        )

    def _call_helper(self, mock_delay, event):
        send_withdrawal_status_email(self.wr, event)
        self.assertEqual(mock_delay.call_count, 1)
        kwargs = mock_delay.call_args.kwargs
        return kwargs["subject"], kwargs["message"]

    @patch("simulator.tasks.send_email_async.delay")
    def test_requested_subject_has_money_brokers(self, mock_delay):
        subject, _ = self._call_helper(mock_delay, EVENT_REQUESTED)
        self.assertIn("Money Broker", subject)

    @patch("simulator.tasks.send_email_async.delay")
    def test_approved_subject_has_money_brokers(self, mock_delay):
        subject, _ = self._call_helper(mock_delay, EVENT_APPROVED)
        self.assertIn("Money Broker", subject)

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_subject_has_money_brokers(self, mock_delay):
        subject, _ = self._call_helper(mock_delay, EVENT_REJECTED)
        self.assertIn("Money Broker", subject)

    @patch("simulator.tasks.send_email_async.delay")
    def test_completed_subject_has_money_brokers(self, mock_delay):
        subject, _ = self._call_helper(mock_delay, EVENT_COMPLETED)
        self.assertIn("Money Broker", subject)

    @patch("simulator.tasks.send_email_async.delay")
    def test_failed_subject_has_money_brokers(self, mock_delay):
        subject, _ = self._call_helper(mock_delay, EVENT_FAILED)
        self.assertIn("Money Broker", subject)

    @patch("simulator.tasks.send_email_async.delay")
    def test_all_events_body_contains_amount(self, mock_delay):
        for event in (EVENT_REQUESTED, EVENT_APPROVED, EVENT_REJECTED,
                      EVENT_COMPLETED, EVENT_FAILED):
            mock_delay.reset_mock()
            _, body = self._call_helper(mock_delay, event)
            self.assertIn("75", body, f"amount missing from body for event={event}")

    @patch("simulator.tasks.send_email_async.delay")
    def test_all_events_body_contains_currency(self, mock_delay):
        for event in (EVENT_REQUESTED, EVENT_APPROVED, EVENT_REJECTED,
                      EVENT_COMPLETED, EVENT_FAILED):
            mock_delay.reset_mock()
            _, body = self._call_helper(mock_delay, event)
            self.assertIn("BTC", body, f"currency missing from body for event={event}")

    @patch("simulator.tasks.send_email_async.delay")
    def test_all_events_body_contains_status_label(self, mock_delay):
        expected = {
            EVENT_REQUESTED: "Pendiente",
            EVENT_APPROVED:  "Aprobado",
            EVENT_REJECTED:  "Rechazado",
            EVENT_COMPLETED: "Completado",
            EVENT_FAILED:    "Fallido",
        }
        for event, label in expected.items():
            mock_delay.reset_mock()
            _, body = self._call_helper(mock_delay, event)
            self.assertIn(label, body, f"status label '{label}' missing for event={event}")

    @patch("simulator.tasks.send_email_async.delay")
    def test_all_events_body_contains_date_marker(self, mock_delay):
        year = str(timezone.now().year)
        for event in (EVENT_REQUESTED, EVENT_APPROVED, EVENT_REJECTED,
                      EVENT_COMPLETED, EVENT_FAILED):
            mock_delay.reset_mock()
            _, body = self._call_helper(mock_delay, event)
            self.assertIn(year, body, f"date missing from body for event={event}")

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_body_includes_admin_note_when_present(self, mock_delay):
        self.wr.admin_note = "Documentos inválidos"
        _, body = self._call_helper(mock_delay, EVENT_REJECTED)
        self.assertIn("Documentos inválidos", body)

    @patch("simulator.tasks.send_email_async.delay")
    def test_rejected_body_omits_note_line_when_blank(self, mock_delay):
        self.wr.admin_note = ""
        _, body = self._call_helper(mock_delay, EVENT_REJECTED)
        self.assertNotIn("Motivo:", body)

    @patch("simulator.tasks.send_email_async.delay")
    def test_emails_sent_to_withdrawal_owner(self, mock_delay):
        for event in (EVENT_REQUESTED, EVENT_APPROVED, EVENT_REJECTED,
                      EVENT_COMPLETED, EVENT_FAILED):
            mock_delay.reset_mock()
            send_withdrawal_status_email(self.wr, event)
            recipients = mock_delay.call_args.kwargs["recipient_list"]
            self.assertIn(self.user.email, recipients)
