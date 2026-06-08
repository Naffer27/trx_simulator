# simulator/tests/test_withdrawals.py
"""
Withdrawal safety tests — double-debit prevention, atomicity, idempotency.

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
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from simulator.models import (
    Wallet, WalletTransaction, WithdrawalRequest,
)
from simulator.tests.factories import make_user, make_wallet

WITHDRAW_URL  = "/withdraw/"
CALLBACK_URL  = "/withdraw/callback/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


def _wr_payload(amount="50.00"):
    return {
        "amount_usd":      amount,
        "crypto_currency": "btc",
        "wallet_address":  "bc1qtest000000000000000000000000000000000",
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
        self.user   = make_user(email="wd@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

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
        self.user   = make_user(email="dup@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

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
        self.user   = make_user(email="atomic@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("300"))
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

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
        self.user_a  = make_user()
        self.wallet_a = make_wallet(self.user_a, initial_balance=Decimal("300"))
        self.user_b  = make_user()
        self.wallet_b = make_wallet(self.user_b, initial_balance=Decimal("300"))

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

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
