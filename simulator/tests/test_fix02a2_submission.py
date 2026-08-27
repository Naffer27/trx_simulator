# simulator/tests/test_fix02a2_submission.py
"""
FIX-02A.2 — submit_withdrawal_to_provider() tests.

Uses a fake adapter test double (same contract as NowPaymentsAdapter:
provider_name, estimate(), create_payout()) instead of mocking HTTP —
the adapter's own translation logic is already covered in
test_fix02a2_adapter.py; here the goal is the orchestrator's sequencing
(estimate -> TXN1 -> create_payout -> TXN2), locking discipline, and
that the original FIX-02 bug (rollback to pending after a payout
attempt exists) cannot recur.
"""
import threading
import time
import random
from decimal import Decimal

from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from simulator.models import PayoutAttempt, Wallet, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import (
    EstimateFailed, WithdrawalAlreadyClaimed, submit_withdrawal_to_provider,
)
from simulator.payout_providers import (
    ProviderAuthError, ProviderResponseError, ProviderTimeoutError, ProviderUnavailableError,
)
from simulator.payout_state_machine import ActivePayoutAttemptExists
from simulator.tests.factories import make_user, make_wallet


class _FakeAdapter:
    """Minimal stand-in matching NowPaymentsAdapter's contract."""
    provider_name = "nowpayments"

    def __init__(self, *, estimate_result=None, estimate_error=None,
                 create_result=None, create_error=None):
        self.estimate_result = estimate_result if estimate_result is not None else Decimal("0.001")
        self.estimate_error = estimate_error
        self.create_result = create_result
        self.create_error = create_error
        self.estimate_calls = []
        self.create_calls = []

    def estimate(self, amount_usd, asset):
        self.estimate_calls.append((amount_usd, asset))
        if self.estimate_error:
            raise self.estimate_error
        return self.estimate_result

    def create_payout(self, attempt, *, callback_url=""):
        self.create_calls.append((attempt.pk, callback_url))
        if self.create_error:
            raise self.create_error
        return self.create_result


def _make_withdrawal_request(user, amount="500.00"):
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd=Decimal(amount),
        crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PENDING,
        debit_tx=None,
    )


class _FakeSubmissionResult:
    def __init__(self, provider_reference="wd-1", provider_batch_id="batch-1",
                 provider_amount=Decimal("0.001"), raw_status="CREATED"):
        self.accepted = True
        self.provider_reference = provider_reference
        self.provider_batch_id = provider_batch_id
        self.provider_amount = provider_amount
        self.raw_status = raw_status


class SubmissionSuccessTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user)

    def test_success_moves_to_processing_and_persists_refs(self):
        adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
        result = submit_withdrawal_to_provider(
            self.wr, adapter=adapter, actor=self.user, callback_url="https://cb",
        )
        self.assertEqual(result["outcome"], "processing")

        attempt = PayoutAttempt.objects.get(pk=result["attempt_id"])
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.assertEqual(attempt.provider_reference, "wd-1")
        self.assertEqual(attempt.provider_batch_id, "batch-1")

        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING)
        # legacy mirror written in the same transaction
        self.assertEqual(self.wr.np_payout_id, "wd-1")
        self.assertEqual(self.wr.np_batch_id, "batch-1")

    def test_success_never_passes_through_approved(self):
        """Design Lock Correction #1 — WithdrawalRequest never gets
        written as 'approved' in the migrated flow."""
        adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
        submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertFalse(
            WithdrawalRequest.objects.filter(pk=self.wr.pk, status=WithdrawalRequest.STATUS_APPROVED).exists()
        )

    def test_success_stamps_reviewed_by(self):
        adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
        submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.reviewed_by_id, self.user.pk)
        self.assertIsNotNone(self.wr.reviewed_at)

    def test_success_does_not_touch_wallet(self):
        before = self.wallet.available_balance
        adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
        submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)


class EstimateFailureTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user)

    def test_estimate_failure_creates_no_attempt(self):
        adapter = _FakeAdapter(estimate_error=ProviderUnavailableError("down"))
        with self.assertRaises(EstimateFailed):
            submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 0)

    def test_estimate_failure_leaves_withdrawal_pending(self):
        adapter = _FakeAdapter(estimate_error=ProviderUnavailableError("down"))
        with self.assertRaises(EstimateFailed):
            submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_estimate_failure_does_not_call_create_payout(self):
        adapter = _FakeAdapter(estimate_error=ProviderUnavailableError("down"))
        with self.assertRaises(EstimateFailed):
            submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(adapter.create_calls, [])


class AuthErrorPreSendRefundTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user, amount="300.00")

    def test_auth_error_marks_failed_and_refunds_exactly_once(self):
        adapter = _FakeAdapter(create_error=ProviderAuthError("bad creds"))
        result = submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(result["outcome"], "failed")

        attempt = PayoutAttempt.objects.get(pk=result["attempt_id"])
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)

        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)

        self.wallet.refresh_from_db()
        # 1000 (initial) - never debited by submission (WR was created directly
        # with debit_tx=None here) + 300 refund = 1300
        self.assertEqual(self.wallet.available_balance, Decimal("1300"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
            ).count(),
            1,
        )


class AmbiguousTimeoutUnknownTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user)

    def _assert_unknown_no_refund_no_rollback(self, error):
        adapter = _FakeAdapter(create_error=error)
        result = submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(result["outcome"], "unknown")

        attempt = PayoutAttempt.objects.get(pk=result["attempt_id"])
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_UNKNOWN)

        self.wr.refresh_from_db()
        self.assertEqual(
            self.wr.status, WithdrawalRequest.STATUS_PROCESSING,
            "must remain 'processing' — never revert to 'pending' (the original FIX-02 bug)",
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("1000"), "UNKNOWN must never refund")

    def test_timeout_produces_unknown(self):
        self._assert_unknown_no_refund_no_rollback(ProviderTimeoutError("timeout"))

    def test_unavailable_5xx_produces_unknown(self):
        self._assert_unknown_no_refund_no_rollback(ProviderUnavailableError("500"))

    def test_response_parse_failure_produces_unknown(self):
        self._assert_unknown_no_refund_no_rollback(ProviderResponseError("bad body"))

    def test_unknown_blocks_second_approval_attempt(self):
        adapter = _FakeAdapter(create_error=ProviderTimeoutError("timeout"))
        submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")

        adapter2 = _FakeAdapter(create_result=_FakeSubmissionResult())
        with self.assertRaises(WithdrawalAlreadyClaimed):
            submit_withdrawal_to_provider(self.wr, adapter=adapter2, actor=self.user, callback_url="cb")
        self.assertEqual(adapter2.estimate_calls, [], "must not even reach estimate() — blocked at the optimistic pre-check")
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 1)


class NoHttpUnderLockTests(TestCase):
    """Confirms adapter calls happen with no DB transaction/lock held —
    verified by asserting the fake adapter's estimate()/create_payout()
    are invoked OUTSIDE of any query captured under an open atomic
    block: since TestCase itself wraps everything in an outer
    transaction, we instead assert indirectly — the fake adapter
    records whether connection.in_atomic_block was True at call time."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user)

    def test_estimate_and_create_payout_run_without_extra_atomic_block(self):
        depths = {}

        class _RecordingAdapter(_FakeAdapter):
            def estimate(self, amount_usd, asset):
                depths["estimate"] = connection.savepoint_ids[:] if hasattr(connection, "savepoint_ids") else None
                return super().estimate(amount_usd, asset)

            def create_payout(self, attempt, *, callback_url=""):
                depths["create_payout"] = connection.savepoint_ids[:] if hasattr(connection, "savepoint_ids") else None
                return super().create_payout(attempt, callback_url=callback_url)

        adapter = _RecordingAdapter(create_result=_FakeSubmissionResult())
        base_depth = len(getattr(connection, "savepoint_ids", []) or [])

        submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")

        # The orchestrator's own TXN1/TXN2 open and close savepoints around
        # the calls, not during them — at the instant estimate()/create_payout()
        # run, no *additional* savepoint beyond whatever TestCase's own outer
        # transaction already holds should be open.
        self.assertEqual(len(depths["estimate"] or []), base_depth)
        self.assertEqual(len(depths["create_payout"] or []), base_depth)


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = ("ok", fn())
                return
            except WithdrawalAlreadyClaimed as exc:
                results[index] = ("claimed", exc)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
            except Exception as exc:  # pragma: no cover - diagnostic safety net
                results[index] = ("error", exc)
                return
    finally:
        connection.close()


class ConcurrentAdminClaimTests(TransactionTestCase):
    def test_two_concurrent_admin_approvals_exactly_one_wins(self):
        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_withdrawal_request(user)

        def _do():
            adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
            return submit_withdrawal_to_provider(wr, adapter=adapter, actor=user, callback_url="cb")

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [threading.Thread(target=_run_locked_retry, args=(_do, barrier, results, i)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        outcomes = [r[0] for r in results]
        self.assertEqual(outcomes.count("ok"), 1, f"expected exactly one winner, got {results}")
        self.assertEqual(outcomes.count("claimed"), 1, f"expected exactly one WithdrawalAlreadyClaimed, got {results}")
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)
