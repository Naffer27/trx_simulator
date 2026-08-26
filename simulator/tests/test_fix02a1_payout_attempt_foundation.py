# simulator/tests/test_fix02a1_payout_attempt_foundation.py
"""
FIX-02A.1 — Core Safety / PayoutAttempt / State Machine / Internal
Idempotency Foundation.

Tests the foundation built in simulator/payout_state_machine.py and the
PayoutAttempt model — no provider, no HTTP, no admin/views/callback
wiring (those are FIX-02A.2/.3/.4, not touched here).

Covers:
  - PayoutAttempt state machine: every allowed transition, every
    disallowed one, the SUBMITTED->FAILED pre_send_rejection gate.
  - DB constraints: idempotency_key uniqueness, (withdrawal_request,
    attempt_number) uniqueness, at most one non-terminal attempt per
    withdrawal_request (partial unique index) — enforced at the DB
    level, not just by application logic.
  - Real-thread concurrency: two concurrent creation attempts for the
    same WithdrawalRequest — exactly one wins.
  - WithdrawalRequest.status sync: the approved PayoutAttempt-status ->
    WithdrawalRequest-status mapping, and that it never regresses to
    'pending'.
  - Locking: creating/transitioning a PayoutAttempt never touches
    Wallet — no balance change, no WalletTransaction, no refund.
"""
import threading
import time
import random
from decimal import Decimal

from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase

from simulator.models import PayoutAttempt, Wallet, WalletTransaction, WithdrawalRequest
from simulator.payout_state_machine import (
    ActivePayoutAttemptExists,
    InvalidPayoutAttemptTransition,
    create_and_submit_payout_attempt,
    derive_withdrawal_status,
    sync_withdrawal_request_status,
    transition_payout_attempt,
)
from simulator.tests.factories import make_user, make_wallet

S = PayoutAttempt


def _make_withdrawal_request(user=None, amount="500.00", status=None):
    if user is None:
        user = make_user()
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd=Decimal(amount),
        crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=status or WithdrawalRequest.STATUS_APPROVED,
        debit_tx=None,
    )


def _make_attempt(wr, status=S.STATUS_CREATED, attempt_number=1, **overrides):
    """Direct model construction, bypassing the service — used to set up
    a specific PayoutAttempt state for state-machine/constraint tests
    without going through create_and_submit_payout_attempt()."""
    defaults = dict(
        withdrawal_request=wr,
        provider="nowpayments",
        attempt_number=attempt_number,
        idempotency_key=f"test-key-wr{wr.pk}-a{attempt_number}",
        requested_amount_usd=wr.amount_usd,
        requested_asset="btc",
        destination_address=wr.wallet_address,
        status=status,
    )
    defaults.update(overrides)
    return PayoutAttempt.objects.create(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 1. State machine — transition_payout_attempt()
# ─────────────────────────────────────────────────────────────────────────────

class StateMachineValidTransitionsTests(TestCase):
    def setUp(self):
        self.wr = _make_withdrawal_request()

    def test_created_to_submitted(self):
        a = _make_attempt(self.wr, status=S.STATUS_CREATED)
        transition_payout_attempt(a, S.STATUS_SUBMITTED)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_SUBMITTED)

    def test_submitted_to_processing_sets_acknowledged_at(self):
        a = _make_attempt(self.wr, status=S.STATUS_SUBMITTED)
        transition_payout_attempt(a, S.STATUS_PROCESSING, raw_provider_status="CREATED")
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_PROCESSING)
        self.assertIsNotNone(a.acknowledged_at)
        self.assertEqual(a.raw_provider_status, "CREATED")

    def test_submitted_to_unknown_records_error(self):
        a = _make_attempt(self.wr, status=S.STATUS_SUBMITTED)
        transition_payout_attempt(a, S.STATUS_UNKNOWN, reason="ReadTimeout after 30s")
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_UNKNOWN)
        self.assertIn("ReadTimeout", a.last_error)

    def test_submitted_to_failed_requires_pre_send_rejection_flag(self):
        a = _make_attempt(self.wr, status=S.STATUS_SUBMITTED)
        transition_payout_attempt(a, S.STATUS_FAILED, reason="400 invalid address", pre_send_rejection=True)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_FAILED)
        self.assertIsNotNone(a.failed_at)

    def test_processing_to_completed_sets_completed_at(self):
        a = _make_attempt(self.wr, status=S.STATUS_PROCESSING)
        transition_payout_attempt(a, S.STATUS_COMPLETED)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_COMPLETED)
        self.assertIsNotNone(a.completed_at)

    def test_processing_to_failed_sets_failed_at(self):
        a = _make_attempt(self.wr, status=S.STATUS_PROCESSING)
        transition_payout_attempt(a, S.STATUS_FAILED, reason="provider FAILED callback")
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_FAILED)
        self.assertIsNotNone(a.failed_at)

    def test_unknown_to_completed(self):
        a = _make_attempt(self.wr, status=S.STATUS_UNKNOWN)
        transition_payout_attempt(a, S.STATUS_COMPLETED)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_COMPLETED)

    def test_unknown_to_failed_via_reconciliation(self):
        a = _make_attempt(self.wr, status=S.STATUS_UNKNOWN)
        transition_payout_attempt(a, S.STATUS_FAILED, reason="reconciled — provider confirms not received")
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_FAILED)


class StateMachineInvalidTransitionsTests(TestCase):
    def setUp(self):
        self.wr = _make_withdrawal_request()

    def test_submitted_to_failed_without_pre_send_rejection_flag_refused(self):
        a = _make_attempt(self.wr, status=S.STATUS_SUBMITTED)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, S.STATUS_FAILED)  # pre_send_rejection defaults False
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_SUBMITTED, "must not have moved on a refused transition")

    def test_unknown_never_reverts_to_submitted(self):
        a = _make_attempt(self.wr, status=S.STATUS_UNKNOWN)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, S.STATUS_SUBMITTED)

    def test_unknown_never_reverts_to_created(self):
        a = _make_attempt(self.wr, status=S.STATUS_UNKNOWN)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, S.STATUS_CREATED)

    def test_created_cannot_skip_to_processing(self):
        a = _make_attempt(self.wr, status=S.STATUS_CREATED)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, S.STATUS_PROCESSING)

    def test_completed_is_terminal_never_changes(self):
        a = _make_attempt(self.wr, status=S.STATUS_COMPLETED)
        for target in (S.STATUS_FAILED, S.STATUS_PROCESSING, S.STATUS_UNKNOWN, S.STATUS_SUBMITTED, S.STATUS_CREATED):
            with self.assertRaises(InvalidPayoutAttemptTransition):
                transition_payout_attempt(a, target)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_COMPLETED)

    def test_failed_is_terminal_never_changes(self):
        a = _make_attempt(self.wr, status=S.STATUS_FAILED)
        for target in (S.STATUS_COMPLETED, S.STATUS_PROCESSING, S.STATUS_UNKNOWN, S.STATUS_SUBMITTED, S.STATUS_CREATED):
            with self.assertRaises(InvalidPayoutAttemptTransition):
                transition_payout_attempt(a, target)
        a.refresh_from_db()
        self.assertEqual(a.status, S.STATUS_FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# 2. DB constraints
# ─────────────────────────────────────────────────────────────────────────────

class DBConstraintTests(TransactionTestCase):
    """TransactionTestCase — IntegrityError must actually be raised and
    caught mid-test; plain TestCase's wrapping transaction poisons the
    connection after the first IntegrityError, breaking subsequent
    queries in the same test."""

    def setUp(self):
        self.wr = _make_withdrawal_request()

    def test_duplicate_idempotency_key_rejected_by_db(self):
        _make_attempt(self.wr, status=S.STATUS_FAILED, attempt_number=1,
                      idempotency_key="dup-key")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_attempt(self.wr, status=S.STATUS_FAILED, attempt_number=2,
                              idempotency_key="dup-key")

    def test_duplicate_attempt_number_rejected_by_db(self):
        _make_attempt(self.wr, status=S.STATUS_FAILED, attempt_number=1,
                      idempotency_key="key-a")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_attempt(self.wr, status=S.STATUS_FAILED, attempt_number=1,
                              idempotency_key="key-b")

    def test_second_non_terminal_attempt_rejected_by_db_constraint(self):
        """Bypasses the application-level ActivePayoutAttemptExists check
        entirely (direct model creation) to prove the partial unique
        index itself — not just app logic — blocks a second active
        attempt for the same withdrawal_request."""
        _make_attempt(self.wr, status=S.STATUS_SUBMITTED, attempt_number=1,
                      idempotency_key="key-1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_attempt(self.wr, status=S.STATUS_PROCESSING, attempt_number=2,
                              idempotency_key="key-2")

    def test_second_terminal_attempt_after_first_terminal_is_allowed(self):
        """The partial index only restricts NON-terminal rows — a second
        attempt is fine once the first is terminal (FAILED/COMPLETED)."""
        _make_attempt(self.wr, status=S.STATUS_FAILED, attempt_number=1,
                      idempotency_key="key-1")
        second = _make_attempt(self.wr, status=S.STATUS_SUBMITTED, attempt_number=2,
                                idempotency_key="key-2")
        self.assertEqual(second.attempt_number, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Real-thread concurrency — create_and_submit_payout_attempt()
# ─────────────────────────────────────────────────────────────────────────────

def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same idiom as test_atomic_guard_lock_order.py's _run_locked_retry —
    SQLite needs busy_timeout + retry-on-'locked' even with the row lock
    doing the real serialization, because ordinary reads inside a
    transaction can still collide with SQLite's shared-cache table locks."""
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
            except ActivePayoutAttemptExists as exc:
                results[index] = ("blocked", exc)
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


class ConcurrentAttemptCreationTests(TransactionTestCase):
    def test_two_concurrent_creations_for_same_withdrawal_exactly_one_wins(self):
        wr = _make_withdrawal_request()

        def _do():
            return create_and_submit_payout_attempt(
                wr, provider="nowpayments", requested_asset="btc",
                destination_address=wr.wallet_address,
                requested_amount_usd=wr.amount_usd,
            )

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_run_locked_retry, args=(_do, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        outcomes = [r[0] for r in results]
        self.assertEqual(outcomes.count("ok"), 1, f"expected exactly one winner, got {results}")
        self.assertEqual(outcomes.count("blocked"), 1, f"expected exactly one ActivePayoutAttemptExists, got {results}")
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 4. WithdrawalRequest.status sync
# ─────────────────────────────────────────────────────────────────────────────

class WithdrawalStatusSyncTests(TestCase):
    def setUp(self):
        self.wr = _make_withdrawal_request(status=WithdrawalRequest.STATUS_APPROVED)

    def test_submitted_maps_to_processing(self):
        self.assertEqual(derive_withdrawal_status(S.STATUS_SUBMITTED), WithdrawalRequest.STATUS_PROCESSING)

    def test_processing_maps_to_processing(self):
        self.assertEqual(derive_withdrawal_status(S.STATUS_PROCESSING), WithdrawalRequest.STATUS_PROCESSING)

    def test_unknown_maps_to_processing(self):
        self.assertEqual(derive_withdrawal_status(S.STATUS_UNKNOWN), WithdrawalRequest.STATUS_PROCESSING)

    def test_completed_maps_to_completed(self):
        self.assertEqual(derive_withdrawal_status(S.STATUS_COMPLETED), WithdrawalRequest.STATUS_COMPLETED)

    def test_failed_maps_to_failed(self):
        self.assertEqual(derive_withdrawal_status(S.STATUS_FAILED), WithdrawalRequest.STATUS_FAILED)

    def test_created_has_no_approved_mapping(self):
        """CREATED is never expected to be durably observable — deriving
        from it is a programming error, not silently mapped to something."""
        with self.assertRaises(ValueError):
            derive_withdrawal_status(S.STATUS_CREATED)

    def test_no_payout_attempt_status_maps_to_pending(self):
        """Structural regression guard for the original FIX-02 bug
        (APPROVED -> PENDING rollback on exception)."""
        for status in (S.STATUS_SUBMITTED, S.STATUS_PROCESSING, S.STATUS_UNKNOWN,
                       S.STATUS_COMPLETED, S.STATUS_FAILED):
            self.assertNotEqual(derive_withdrawal_status(status), WithdrawalRequest.STATUS_PENDING)

    def test_sync_writes_through_to_db(self):
        sync_withdrawal_request_status(self.wr, S.STATUS_UNKNOWN)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING)

    def test_sync_completed_writes_completed(self):
        sync_withdrawal_request_status(self.wr, S.STATUS_COMPLETED)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_COMPLETED)

    def test_sync_failed_writes_failed(self):
        sync_withdrawal_request_status(self.wr, S.STATUS_FAILED)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Locking + money invariants — create_and_submit_payout_attempt()
# ─────────────────────────────────────────────────────────────────────────────

class LockingAndMoneyInvariantTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_withdrawal_request(self.user, amount="500.00")

    def test_creation_does_not_change_wallet_balance(self):
        before = self.wallet.available_balance
        create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)

    def test_creation_does_not_create_wallet_transaction(self):
        before_count = WalletTransaction.objects.filter(wallet=self.wallet).count()
        create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), before_count
        )

    def test_transition_to_failed_does_not_refund_wallet(self):
        """Confirms the state machine itself never calls credit_wallet() —
        refund is out of scope for FIX-02A.1 by design."""
        attempt = create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        before = self.wallet.available_balance
        transition_payout_attempt(attempt, S.STATUS_UNKNOWN, reason="timeout")
        transition_payout_attempt(attempt, S.STATUS_FAILED, reason="reconciled failed")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)

    def test_creation_sets_withdrawal_request_to_processing_never_pending(self):
        create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING)

    def test_second_creation_attempt_blocked_while_first_active(self):
        create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        with self.assertRaises(ActivePayoutAttemptExists):
            create_and_submit_payout_attempt(
                self.wr, provider="nowpayments", requested_asset="btc",
                destination_address=self.wr.wallet_address,
                requested_amount_usd=self.wr.amount_usd,
            )
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 1)

    def test_new_attempt_allowed_after_first_confirmed_failed(self):
        first = create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        transition_payout_attempt(first, S.STATUS_UNKNOWN, reason="timeout")
        transition_payout_attempt(first, S.STATUS_FAILED, reason="reconciled — confirmed not received")

        second = create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address,
            requested_amount_usd=self.wr.amount_usd,
        )
        self.assertEqual(second.attempt_number, 2)
        self.assertNotEqual(second.idempotency_key, first.idempotency_key)
