# simulator/tests/test_money_flow_audit.py
"""
Money-flow audit tests — wallet, ledger, deposits, withdrawals, callbacks.

Covers gaps not addressed by existing test_deposit.py / test_withdrawals.py:
  1. debit_wallet raises InsufficientFunds — wallet never goes negative.
  2. credit_wallet + debit_wallet maintain Decimal precision.
  3. Deposit callback creates wallet if none exists.
  4. Deposit "expired" status does not credit wallet.
  5. Double "confirming" IPN increments pending_balance only once (bug fix).
  6. Admin reject_withdrawals: wallet refunded, status set to REJECTED.
  7. Admin reject_withdrawals: idempotent — already-rejected WR not double-refunded.
  8. Admin approve_withdrawals: double-call does not double-approve (status pre-claim).
  9. Payout callback FAILED refunds a PROCESSING WR (not just APPROVED).
 10. Withdrawal moves available_balance only — no ghost balance.
 11. reconcile_wallet passes after all operations.
 12. credit_wallet and debit_wallet both write WalletTransaction records.
 13. Deposit callback: wallet auto-created when missing.
 14. All financial amounts stored as Decimal, not float.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages.storage.cookie import CookieStorage
from django.db import transaction
from django.test import TestCase, RequestFactory
from django.utils import timezone

from simulator.models import (
    Deposit, Wallet, WalletTransaction, WithdrawalRequest, TOTPDevice,
)
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_deposit
from simulator.wallet_ledger import (
    InsufficientFunds, credit_wallet, debit_wallet, reconcile_wallet, get_or_create_wallet,
)

DEPOSIT_CB_URL   = "/deposit/callback/"
PAYOUT_CB_URL    = "/withdraw/callback/"

_PATCH_SIG_OK  = patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
_PATCH_SIG_BAD = patch("simulator.nowpayments.verify_ipn_signature", return_value=False)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _deposit_ipn(payment_id, payment_status, order_id="", amount="100.00"):
    return json.dumps({
        "payment_id":           payment_id,
        "payment_status":       payment_status,
        "order_id":             str(order_id),
        "actually_paid_amount": amount,
        "pay_currency":         "btc",
        "price_currency":       "usd",
        "price_amount":         float(amount),
    })


def _payout_ipn(payout_id, status, batch_id="batch_001"):
    return json.dumps({
        "id":     batch_id,
        "status": status,
        "withdrawals": [{"id": payout_id, "status": status}],
    })


def _make_totp(user):
    import pyotp
    return TOTPDevice.objects.create(
        user=user, secret=pyotp.random_base32(),
        confirmed=True, confirmed_at=timezone.now(),
    )


def _admin_request(admin_user):
    req = RequestFactory().post("/admin/")
    req.user = admin_user
    req._messages = CookieStorage(req)
    return req


def _make_pending_wr(user, wallet, amount="80.00"):
    """Debit wallet and create a PENDING WithdrawalRequest — mirrors withdraw_view."""
    debit_tx = debit_wallet(
        wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="test wr"
    )
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd=Decimal(amount),
        crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PENDING,
        debit_tx=debit_tx,
    )


# ── 1. Wallet balance guard — never goes negative ─────────────────────────────

class WalletNeverNegativeTest(TestCase):
    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("100"))

    def test_debit_more_than_balance_raises(self):
        with self.assertRaises(InsufficientFunds):
            debit_wallet(self.wallet.id, Decimal("101"), WalletTransaction.TX_WITHDRAW)

    def test_balance_unchanged_after_failed_debit(self):
        try:
            debit_wallet(self.wallet.id, Decimal("9999"), WalletTransaction.TX_WITHDRAW)
        except InsufficientFunds:
            pass
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("100"))

    def test_no_tx_written_after_failed_debit(self):
        initial_count = WalletTransaction.objects.filter(wallet=self.wallet).count()
        try:
            debit_wallet(self.wallet.id, Decimal("9999"), WalletTransaction.TX_WITHDRAW)
        except InsufficientFunds:
            pass
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(),
            initial_count,
        )

    def test_exact_balance_debit_succeeds(self):
        debit_wallet(self.wallet.id, Decimal("100"), WalletTransaction.TX_WITHDRAW)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("0"))

    def test_zero_debit_raises_value_error(self):
        with self.assertRaises(ValueError):
            debit_wallet(self.wallet.id, Decimal("0"), WalletTransaction.TX_WITHDRAW)

    def test_zero_credit_raises_value_error(self):
        with self.assertRaises(ValueError):
            credit_wallet(self.wallet.id, Decimal("0"), WalletTransaction.TX_DEPOSIT)


# ── 2. Decimal precision ──────────────────────────────────────────────────────

class DecimalPrecisionTest(TestCase):
    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user)

    def test_credit_decimal_precision_maintained(self):
        credit_wallet(self.wallet.id, Decimal("123.45"), WalletTransaction.TX_DEPOSIT)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("123.45"))

    def test_debit_decimal_precision_maintained(self):
        credit_wallet(self.wallet.id, Decimal("200.00"), WalletTransaction.TX_DEPOSIT)
        debit_wallet(self.wallet.id, Decimal("99.99"), WalletTransaction.TX_WITHDRAW)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("100.01"))

    def test_wallet_transaction_amount_is_decimal(self):
        credit_wallet(self.wallet.id, Decimal("50.00"), WalletTransaction.TX_DEPOSIT)
        tx = WalletTransaction.objects.filter(wallet=self.wallet).first()
        self.assertIsInstance(tx.amount, Decimal)


# ── 3. Wallet ledger entries written on every op ──────────────────────────────

class WalletLedgerEntryTest(TestCase):
    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user)

    def test_credit_writes_positive_tx(self):
        credit_wallet(self.wallet.id, Decimal("100"), WalletTransaction.TX_DEPOSIT)
        tx = WalletTransaction.objects.get(wallet=self.wallet, tx_type=WalletTransaction.TX_DEPOSIT)
        self.assertGreater(tx.amount, 0)

    def test_debit_writes_negative_tx(self):
        credit_wallet(self.wallet.id, Decimal("200"), WalletTransaction.TX_DEPOSIT)
        debit_wallet(self.wallet.id, Decimal("50"), WalletTransaction.TX_WITHDRAW)
        tx = WalletTransaction.objects.get(wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW)
        self.assertLess(tx.amount, 0)

    def test_reconcile_passes_after_credits_and_debits(self):
        credit_wallet(self.wallet.id, Decimal("300"), WalletTransaction.TX_DEPOSIT)
        debit_wallet(self.wallet.id, Decimal("75"), WalletTransaction.TX_WITHDRAW)
        credit_wallet(self.wallet.id, Decimal("25"), WalletTransaction.TX_CORRECTION)
        result = reconcile_wallet(self.wallet.id)
        self.assertTrue(result["ok"], f"Drift detected: {result['drift']}")

    def test_balance_after_field_matches_wallet(self):
        credit_wallet(self.wallet.id, Decimal("100"), WalletTransaction.TX_DEPOSIT)
        tx = WalletTransaction.objects.filter(wallet=self.wallet).order_by("-id").first()
        self.wallet.refresh_from_db()
        self.assertEqual(tx.balance_after, self.wallet.available_balance)


# ── 4. Deposit callback: wallet auto-created if missing ───────────────────────

class DepositCallbackWalletAutoCreateTest(TestCase):
    @_PATCH_SIG_OK
    def test_wallet_created_if_missing(self, _sig):
        user    = make_user()
        # No wallet created for this user
        deposit = make_deposit(user=user, amount_usd=Decimal("50.00"), payment_id="pay_no_wallet")

        body = _deposit_ipn("pay_no_wallet", "finished", deposit.pk, "50.00")
        resp = self.client.post(DEPOSIT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.available_balance, Decimal("50.00"))


# ── 5. Deposit: expired/failed status does not credit ─────────────────────────

class DepositNonCreditStatusTest(TestCase):
    @_PATCH_SIG_OK
    def test_expired_status_no_credit(self, _sig):
        user    = make_user()
        wallet  = make_wallet(user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"), payment_id="pay_exp_001")

        body = _deposit_ipn("pay_exp_001", "expired", deposit.pk)
        resp = self.client.post(DEPOSIT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))

    @_PATCH_SIG_OK
    def test_refunded_status_no_credit(self, _sig):
        user    = make_user()
        wallet  = make_wallet(user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"), payment_id="pay_ref_001")

        body = _deposit_ipn("pay_ref_001", "refunded", deposit.pk)
        self.client.post(DEPOSIT_CB_URL, body, content_type="application/json")

        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))


# ── 6. Double "confirming" IPN — pending_balance incremented only once ─────────

class DepositConfirmingIdempotencyTest(TestCase):
    @_PATCH_SIG_OK
    def test_double_confirming_no_double_pending_balance(self, _sig):
        user    = make_user()
        wallet  = make_wallet(user)
        deposit = make_deposit(user=user, amount_usd=Decimal("200.00"), payment_id="pay_conf2x")

        body = _deposit_ipn("pay_conf2x", "confirming", deposit.pk, "200.00")

        # First confirming IPN
        self.client.post(DEPOSIT_CB_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.pending_balance, Decimal("200.00"))

        # Second confirming IPN (retry / duplicate)
        self.client.post(DEPOSIT_CB_URL, body, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(
            wallet.pending_balance, Decimal("200.00"),
            "pending_balance should not be incremented twice for duplicate confirming IPN",
        )

    @_PATCH_SIG_OK
    def test_confirming_then_finished_drains_pending(self, _sig):
        """After confirming → finished, pending_balance returns to 0."""
        user    = make_user()
        wallet  = make_wallet(user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"), payment_id="pay_drain")

        # Confirming
        body_conf = _deposit_ipn("pay_drain", "confirming", deposit.pk, "100.00")
        self.client.post(DEPOSIT_CB_URL, body_conf, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.pending_balance, Decimal("100.00"))

        # Finished → credits available, drains pending
        body_fin = _deposit_ipn("pay_drain", "finished", deposit.pk, "100.00")
        self.client.post(DEPOSIT_CB_URL, body_fin, content_type="application/json")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))
        self.assertEqual(wallet.pending_balance, Decimal("0.00"))


# ── 7. Admin reject_withdrawals: wallet refunded correctly ────────────────────

class AdminRejectWithdrawalsTest(TestCase):
    def setUp(self):
        self.admin  = make_user(is_staff=True, is_superuser=True)
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr     = _make_pending_wr(self.user, self.wallet, "80.00")
        self.wallet.refresh_from_db()
        # After WR creation, available_balance should be 120
        self.assertEqual(self.wallet.available_balance, Decimal("120.00"))

    @patch("simulator.tasks.send_email_async.delay")
    def test_reject_refunds_wallet(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, _admin_request(self.admin),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"),
                         "Rejected WR should refund wallet to original balance")

    @patch("simulator.tasks.send_email_async.delay")
    def test_reject_sets_status_rejected(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, _admin_request(self.admin),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_REJECTED)

    @patch("simulator.tasks.send_email_async.delay")
    def test_reject_writes_correction_tx(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, _admin_request(self.admin),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        self.assertTrue(
            WalletTransaction.objects.filter(
                wallet=self.wallet,
                tx_type=WalletTransaction.TX_CORRECTION,
                amount=Decimal("80.00"),
            ).exists()
        )

    @patch("simulator.tasks.send_email_async.delay")
    def test_reconcile_passes_after_reject(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        reject_withdrawals(ma, _admin_request(self.admin),
                           WithdrawalRequest.objects.filter(pk=self.wr.pk))

        result = reconcile_wallet(self.wallet.id)
        self.assertTrue(result["ok"], f"Reconcile drift: {result['drift']}")


# ── 8. Admin reject idempotency — already-rejected WR not double-refunded ─────

class AdminRejectIdempotencyTest(TestCase):
    def setUp(self):
        self.admin  = make_user(is_staff=True, is_superuser=True)
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr     = _make_pending_wr(self.user, self.wallet, "80.00")

    @patch("simulator.tasks.send_email_async.delay")
    def test_second_reject_no_double_refund(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        qs = WithdrawalRequest.objects.filter(pk=self.wr.pk)

        # First rejection
        reject_withdrawals(ma, _admin_request(self.admin), qs)
        self.wallet.refresh_from_db()
        balance_after_first = self.wallet.available_balance

        # Second rejection of same WR (now status=REJECTED → should be skipped)
        reject_withdrawals(ma, _admin_request(self.admin), qs)
        self.wallet.refresh_from_db()

        self.assertEqual(
            self.wallet.available_balance, balance_after_first,
            "Second reject must not double-credit the wallet",
        )

    @patch("simulator.tasks.send_email_async.delay")
    def test_second_reject_no_double_correction_tx(self, _email):
        from simulator.admin import reject_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        qs = WithdrawalRequest.objects.filter(pk=self.wr.pk)

        reject_withdrawals(ma, _admin_request(self.admin), qs)
        reject_withdrawals(ma, _admin_request(self.admin), qs)

        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION
            ).count(),
            1,
            "Exactly 1 TX_CORRECTION expected — not 2",
        )


# ── 9. Admin approve idempotency — no double payout ──────────────────────────

class AdminApproveIdempotencyTest(TestCase):
    """
    Verify that the status pre-claim (PENDING → APPROVED) prevents two concurrent
    approve actions from both calling create_payout for the same WR.
    """

    def setUp(self):
        self.admin  = make_user(is_staff=True, is_superuser=True)
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr     = _make_pending_wr(self.user, self.wallet, "80.00")

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.estimate_price", return_value=Decimal("0.001"))
    @patch("simulator.nowpayments.create_payout", return_value={
        "id": "batch_01", "status": "CREATED",
        "withdrawals": [{"id": "pay_01"}],
    })
    def test_approve_already_processing_wr_not_double_approved(self, mock_payout, _price, _email):
        from simulator.admin import approve_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        qs = WithdrawalRequest.objects.filter(pk=self.wr.pk)

        # First approval — succeeds
        approve_withdrawals(ma, _admin_request(self.admin), qs)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING)

        payout_calls_after_first = mock_payout.call_count

        # Second approval of same WR (now PROCESSING — filtered out by PENDING check)
        approve_withdrawals(ma, _admin_request(self.admin), qs)

        self.assertEqual(
            mock_payout.call_count, payout_calls_after_first,
            "create_payout must not be called a second time for an already-processed WR",
        )

    @patch("simulator.tasks.send_email_async.delay")
    @patch("simulator.nowpayments.estimate_price", return_value=Decimal("0.001"))
    @patch("simulator.nowpayments.create_payout", side_effect=Exception("NP down"))
    def test_approve_api_failure_rolls_back_to_pending(self, _payout, _price, _email):
        """If the NP API call fails, WR must be reset to PENDING so admin can retry."""
        from simulator.admin import approve_withdrawals, WithdrawalRequestAdmin
        from django.contrib.admin.sites import AdminSite

        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        qs = WithdrawalRequest.objects.filter(pk=self.wr.pk)
        approve_withdrawals(ma, _admin_request(self.admin), qs)

        self.wr.refresh_from_db()
        self.assertEqual(
            self.wr.status, WithdrawalRequest.STATUS_PENDING,
            "WR must be rolled back to PENDING when NowPayments call fails",
        )


# ── 10. Payout callback FAILED refunds a PROCESSING WR ───────────────────────

class PayoutCallbackProcessingRefundTest(TestCase):
    """
    Verify that the payout callback correctly refunds PROCESSING withdrawals
    (the real production state after approve_withdrawals runs).
    """

    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("300"))
        debit_tx = debit_wallet(
            self.wallet.id, Decimal("100"), WalletTransaction.TX_WITHDRAW, note="test"
        )
        self.wr = WithdrawalRequest.objects.create(
            user=self.user,
            amount_usd=Decimal("100"),
            crypto_currency="btc",
            wallet_address="bc1qtest",
            status=WithdrawalRequest.STATUS_PROCESSING,
            np_payout_id="np_proc_001",
            np_batch_id="batch_proc_001",
            debit_tx=debit_tx,
        )

    @_PATCH_SIG_OK
    def test_failed_payout_refunds_processing_wr(self, _sig):
        body = _payout_ipn("np_proc_001", "FAILED", "batch_proc_001")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("300.00"),
                         "Wallet should be fully refunded after FAILED payout on PROCESSING WR")

    @_PATCH_SIG_OK
    def test_failed_payout_sets_status_failed(self, _sig):
        body = _payout_ipn("np_proc_001", "FAILED", "batch_proc_001")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)

    @_PATCH_SIG_OK
    def test_double_failed_payout_no_double_refund(self, _sig):
        body = _payout_ipn("np_proc_001", "FAILED", "batch_proc_001")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("300.00"),
                         "Double FAILED callback must not double-refund")

    @_PATCH_SIG_OK
    def test_reconcile_passes_after_refund(self, _sig):
        body = _payout_ipn("np_proc_001", "FAILED", "batch_proc_001")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        result = reconcile_wallet(self.wallet.id)
        self.assertTrue(result["ok"], f"Reconcile drift after refund: {result['drift']}")


# ── 11. Withdrawal: available_balance movement ────────────────────────────────

class WithdrawalBalanceMovementTest(TestCase):
    def setUp(self):
        self.user   = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))

    def test_withdrawal_debits_available_balance(self):
        _make_pending_wr(self.user, self.wallet, "150.00")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("350.00"))

    def test_withdrawal_creates_wr_with_debit_tx_linked(self):
        wr = _make_pending_wr(self.user, self.wallet, "100.00")
        self.assertIsNotNone(wr.debit_tx)
        self.assertEqual(wr.debit_tx.tx_type, WalletTransaction.TX_WITHDRAW)

    def test_withdrawal_debit_tx_amount_matches_wr(self):
        wr = _make_pending_wr(self.user, self.wallet, "75.00")
        self.assertEqual(abs(wr.debit_tx.amount), Decimal("75.00"))

    def test_reconcile_passes_after_withdrawal_creation(self):
        _make_pending_wr(self.user, self.wallet, "200.00")
        result = reconcile_wallet(self.wallet.id)
        self.assertTrue(result["ok"], f"Drift: {result['drift']}")
