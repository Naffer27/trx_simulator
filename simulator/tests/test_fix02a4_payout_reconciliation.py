# simulator/tests/test_fix02a4_payout_reconciliation.py
"""
FIX-02A.4 — UNKNOWN Reconciliation / Durable Webhook Inbox /
Provider-Agnostic Payout Recovery.

No real HTTP anywhere in this file — a _FakeReconciliationAdapter test
double (same duck-typed contract as NowPaymentsAdapter: provider_name,
capabilities, estimate/create_payout/lookup_payout/parse_webhook) is
registered into payout_providers._PROVIDER_REGISTRY for the duration of
each test via patch.dict, proving the reconciliation core does not
depend on NowPayments specifically.
"""
import json
import threading
import time
import random
from decimal import Decimal
from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from simulator.models import PayoutAttempt, PayoutWebhookEvent, Wallet, WalletTransaction, WithdrawalRequest
from simulator import payout_providers
from simulator.payout_providers import (
    PayoutLookupOutcome, PayoutLookupResult, ProviderPayoutEvent,
    compute_webhook_event_fingerprint, get_adapter_for_provider,
)
from simulator.payout_orchestrator import (
    Ambiguous, DataIntegrityViolation, Orphan,
    get_or_create_webhook_event, process_webhook_event,
    reconcile_unknown_payout_attempts,
)
from simulator.payout_state_machine import (
    ALLOWED_TRANSITIONS, InvalidPayoutAttemptTransition, is_submitted_aged,
    transition_payout_attempt,
)
from simulator.wallet_ledger import debit_wallet

from .factories import make_user, make_wallet


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_pending_wr(user, wallet, amount="80.00"):
    debit_tx = debit_wallet(wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="fix02a4 test wr")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _make_attempt(wr, **overrides):
    n = overrides.get("attempt_number", 1)
    return PayoutAttempt.objects.create(
        withdrawal_request=wr,
        provider=overrides.get("provider", "nowpayments"),
        attempt_number=n,
        idempotency_key=overrides.get("idempotency_key", f"fix02a4-{wr.pk}-{n}"),
        provider_request_id=overrides.get("provider_request_id", f"fix02a4-req-{wr.pk}-{n}"),
        requested_amount_usd=overrides.get("requested_amount_usd", wr.amount_usd),
        requested_asset=overrides.get("requested_asset", wr.crypto_currency),
        destination_address=overrides.get("destination_address", wr.wallet_address),
        provider_reference=overrides.get("provider_reference", "wd-fix02a4"),
        provider_batch_id=overrides.get("provider_batch_id", "batch-fix02a4"),
        status=overrides.get("status", PayoutAttempt.STATUS_UNKNOWN),
        submitted_at=overrides.get("submitted_at", timezone.now()),
    )


def _event(*, provider="nowpayments", reference="", batch="", status=PayoutAttempt.STATUS_COMPLETED,
           raw="FINISHED", raw_event_payload=None):
    return ProviderPayoutEvent(
        provider=provider, provider_reference=reference, provider_batch_id=batch,
        normalized_status=status, raw_status=raw, provider_amount=None,
        occurred_at=timezone.now(),
        raw_event_payload=raw_event_payload if raw_event_payload is not None else {"id": reference, "status": raw},
    )


class _FakeReconciliationAdapter:
    """Second, independent adapter — proves the reconciliation core
    (registry, capabilities gating, lookup dispatch) has zero coupling
    to NowPayments. Configurable outcome/capabilities per test."""
    provider_name = "fake_recon"

    def __init__(self, *, capabilities=None, lookup_result=None):
        self.capabilities = capabilities if capabilities is not None else {
            "supports_external_idempotency": True,
            "supports_lookup_by_provider_reference": True,
            "supports_lookup_by_provider_request_id": True,
            "supports_lookup_by_batch": False,
            "supports_webhooks": True,
        }
        self.lookup_result = lookup_result or PayoutLookupResult(outcome=PayoutLookupOutcome.NOT_FOUND)
        self.lookup_calls = []

    def estimate(self, amount_usd, asset):
        return Decimal("0.001")

    def create_payout(self, attempt, *, callback_url=""):
        raise AssertionError("create_payout must never be called by reconciliation")

    def lookup_payout(self, attempt):
        self.lookup_calls.append(attempt.pk)
        return self.lookup_result

    def parse_webhook(self, raw_body, headers):
        return []


def _with_fake_provider(adapter_instance):
    """Context manager: registers `adapter_instance`'s class under
    provider_name in the real registry for the duration of the block."""
    return patch.dict(
        payout_providers._PROVIDER_REGISTRY,
        {adapter_instance.provider_name: lambda: adapter_instance},
    )


# ── 1. State machine — UNKNOWN transitions ──────────────────────────────

class UnknownTransitionStateMachineTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_sm_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_unknown_to_processing_allowed(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        transition_payout_attempt(a, PayoutAttempt.STATUS_PROCESSING, raw_provider_status="ROLLING")
        a.refresh_from_db()
        self.assertEqual(a.status, PayoutAttempt.STATUS_PROCESSING)

    def test_unknown_to_completed_still_allowed(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        transition_payout_attempt(a, PayoutAttempt.STATUS_COMPLETED)
        a.refresh_from_db()
        self.assertEqual(a.status, PayoutAttempt.STATUS_COMPLETED)

    def test_unknown_to_failed_still_allowed(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        transition_payout_attempt(a, PayoutAttempt.STATUS_FAILED, reason="reconciled")
        a.refresh_from_db()
        self.assertEqual(a.status, PayoutAttempt.STATUS_FAILED)

    def test_unknown_to_created_still_prohibited(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, PayoutAttempt.STATUS_CREATED)

    def test_unknown_to_submitted_still_prohibited(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, PayoutAttempt.STATUS_SUBMITTED)

    def test_processing_to_unknown_prohibited(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING)
        with self.assertRaises(InvalidPayoutAttemptTransition):
            transition_payout_attempt(a, PayoutAttempt.STATUS_UNKNOWN)

    def test_terminal_states_reject_everything(self):
        for terminal in (PayoutAttempt.STATUS_COMPLETED, PayoutAttempt.STATUS_FAILED):
            a = _make_attempt(self.wr, status=terminal, attempt_number=2 if terminal == PayoutAttempt.STATUS_FAILED else 1)
            for target in (PayoutAttempt.STATUS_PROCESSING, PayoutAttempt.STATUS_UNKNOWN,
                           PayoutAttempt.STATUS_COMPLETED, PayoutAttempt.STATUS_FAILED,
                           PayoutAttempt.STATUS_SUBMITTED, PayoutAttempt.STATUS_CREATED):
                with self.assertRaises(InvalidPayoutAttemptTransition):
                    transition_payout_attempt(a, target)

    def test_allowed_transitions_table_exact(self):
        S = PayoutAttempt
        self.assertEqual(ALLOWED_TRANSITIONS[S.STATUS_UNKNOWN],
                          {S.STATUS_PROCESSING, S.STATUS_COMPLETED, S.STATUS_FAILED})
        self.assertEqual(ALLOWED_TRANSITIONS[S.STATUS_COMPLETED], set())
        self.assertEqual(ALLOWED_TRANSITIONS[S.STATUS_FAILED], set())


# ── 2. Provider-agnostic registry / capabilities ────────────────────────

class ProviderRegistryTests(TestCase):
    def test_get_adapter_for_provider_nowpayments(self):
        adapter = get_adapter_for_provider("nowpayments")
        self.assertEqual(adapter.provider_name, "nowpayments")

    def test_get_adapter_for_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            get_adapter_for_provider("does-not-exist")

    def test_second_adapter_can_be_registered_and_resolved(self):
        fake = _FakeReconciliationAdapter()
        with _with_fake_provider(fake):
            adapter = get_adapter_for_provider("fake_recon")
            self.assertEqual(adapter.provider_name, "fake_recon")
            self.assertTrue(adapter.capabilities["supports_lookup_by_provider_reference"])

    def test_nowpayments_lookup_payout_is_unsupported(self):
        adapter = get_adapter_for_provider("nowpayments")
        attempt_stub = type("A", (), {"pk": 1})()
        result = adapter.lookup_payout(attempt_stub)
        self.assertEqual(result.outcome, PayoutLookupOutcome.UNSUPPORTED)

    def test_nowpayments_capabilities_all_lookup_false_webhooks_true(self):
        from simulator.payout_providers import NowPaymentsAdapter
        caps = NowPaymentsAdapter.capabilities
        self.assertFalse(caps["supports_external_idempotency"])
        self.assertFalse(caps["supports_lookup_by_provider_reference"])
        self.assertFalse(caps["supports_lookup_by_provider_request_id"])
        self.assertFalse(caps["supports_lookup_by_batch"])
        self.assertTrue(caps["supports_webhooks"])


# ── 3. UNKNOWN reconciliation ────────────────────────────────────────────

class UnknownReconciliationTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_recon_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def _unknown_attempt(self, **overrides):
        return _make_attempt(self.wr, provider="fake_recon", status=PayoutAttempt.STATUS_UNKNOWN, **overrides)

    def test_found_completed_transitions_and_no_wallet_movement(self):
        attempt = self._unknown_attempt()
        fake = _FakeReconciliationAdapter(lookup_result=PayoutLookupResult(outcome=PayoutLookupOutcome.FOUND_COMPLETED))
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        with _with_fake_provider(fake), patch("simulator.tasks.send_email_async.delay"):
            reconcile_unknown_payout_attempts()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.assertIsNotNone(attempt.reconciled_at)
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_found_failed_refunds_exactly_once(self):
        attempt = self._unknown_attempt()
        fake = _FakeReconciliationAdapter(lookup_result=PayoutLookupResult(outcome=PayoutLookupOutcome.FOUND_FAILED))
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        with _with_fake_provider(fake), patch("simulator.tasks.send_email_async.delay"):
            reconcile_unknown_payout_attempts()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)
        self.assertEqual(self.wallet.available_balance, balance_before + Decimal("100.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )

    def test_found_processing_transitions_no_refund(self):
        attempt = self._unknown_attempt()
        fake = _FakeReconciliationAdapter(lookup_result=PayoutLookupResult(outcome=PayoutLookupOutcome.FOUND_PROCESSING))
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        with _with_fake_provider(fake):
            reconcile_unknown_payout_attempts()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.assertIsNotNone(attempt.reconciled_at)
        self.assertEqual(self.wallet.available_balance, balance_before)

    def _assert_stays_unknown(self, outcome):
        attempt = self._unknown_attempt()
        fake = _FakeReconciliationAdapter(lookup_result=PayoutLookupResult(outcome=outcome))
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        with _with_fake_provider(fake):
            reconcile_unknown_payout_attempts()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_UNKNOWN)
        self.assertIsNone(attempt.reconciled_at)
        self.assertIsNotNone(attempt.reconciliation_checked_at)
        self.assertEqual(self.wallet.available_balance, balance_before)
        return attempt

    def test_not_found_stays_unknown(self):
        self._assert_stays_unknown(PayoutLookupOutcome.NOT_FOUND)

    def test_unavailable_stays_unknown(self):
        self._assert_stays_unknown(PayoutLookupOutcome.UNAVAILABLE)

    def test_ambiguous_stays_unknown(self):
        self._assert_stays_unknown(PayoutLookupOutcome.AMBIGUOUS)

    def test_unsupported_stays_unknown(self):
        self._assert_stays_unknown(PayoutLookupOutcome.UNSUPPORTED)

    def test_nowpayments_unknown_no_capability_never_calls_lookup_no_create_payout(self):
        """The real, currently-connected provider — no fake involved.
        capabilities are all-False, so lookup_payout() is never even
        called; create_payout() is never called either."""
        attempt = _make_attempt(self.wr, provider="nowpayments", status=PayoutAttempt.STATUS_UNKNOWN)
        with patch("simulator.payout_providers.NowPaymentsAdapter.lookup_payout") as lookup_mock, \
             patch("simulator.payout_providers.NowPaymentsAdapter.create_payout") as create_mock:
            reconcile_unknown_payout_attempts()
        lookup_mock.assert_not_called()
        create_mock.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_UNKNOWN)
        self.assertIsNotNone(attempt.reconciliation_checked_at)

    def test_never_creates_second_payout_attempt(self):
        attempt = self._unknown_attempt()
        fake = _FakeReconciliationAdapter(lookup_result=PayoutLookupResult(outcome=PayoutLookupOutcome.FOUND_FAILED))
        with _with_fake_provider(fake), patch("simulator.tasks.send_email_async.delay"):
            reconcile_unknown_payout_attempts()
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 1)


# ── 4. Durable webhook inbox ─────────────────────────────────────────────

class WebhookInboxRaceInsertTests(TestCase):
    def test_first_insert_creates(self):
        row, created = get_or_create_webhook_event(_event(reference="ref-x"))
        self.assertTrue(created)
        self.assertEqual(PayoutWebhookEvent.objects.count(), 1)

    def test_duplicate_insert_fetches_existing_no_second_row(self):
        row1, created1 = get_or_create_webhook_event(_event(reference="ref-y"))
        row2, created2 = get_or_create_webhook_event(_event(reference="ref-y"))
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(row1.pk, row2.pk)
        self.assertEqual(PayoutWebhookEvent.objects.count(), 1)

    def test_valid_event_persisted_pending_before_correlation(self):
        row, created = get_or_create_webhook_event(_event(reference="ref-z"))
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_PENDING)


class FingerprintTests(TestCase):
    def test_deterministic_same_input_same_fingerprint(self):
        e1 = _event(reference="ref-a", batch="b1")
        e2 = _event(reference="ref-a", batch="b1")
        self.assertEqual(compute_webhook_event_fingerprint(e1), compute_webhook_event_fingerprint(e2))

    def test_provider_aware_different_provider_different_fingerprint(self):
        e1 = _event(provider="nowpayments", reference="123")
        e2 = _event(provider="our_treasury", reference="123")
        self.assertNotEqual(compute_webhook_event_fingerprint(e1), compute_webhook_event_fingerprint(e2))

    def test_two_events_same_batch_do_not_collide(self):
        """NowPayments can send multiple withdrawals[] entries in one
        delivery — each individual event must get its own fingerprint."""
        e1 = _event(batch="batch-1", reference="wd-1", raw_event_payload={"id": "wd-1", "status": "FINISHED"})
        e2 = _event(batch="batch-1", reference="wd-2", raw_event_payload={"id": "wd-2", "status": "FINISHED"})
        self.assertNotEqual(compute_webhook_event_fingerprint(e1), compute_webhook_event_fingerprint(e2))

    def test_redelivery_of_same_event_reproduces_same_fingerprint(self):
        payload = {"id": "wd-9", "status": "FAILED"}
        e1 = _event(batch="batch-9", reference="wd-9", raw="FAILED", raw_event_payload=dict(payload))
        e2 = _event(batch="batch-9", reference="wd-9", raw="FAILED", raw_event_payload=dict(payload))
        self.assertEqual(compute_webhook_event_fingerprint(e1), compute_webhook_event_fingerprint(e2))


class ProcessWebhookEventPipelineTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_pipeline_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_immediately_correlatable_event_resolves(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-live")
        row, _ = get_or_create_webhook_event(_event(reference="ref-live", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        webhook_event, target = process_webhook_event(row.pk)
        webhook_event.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)
        self.assertEqual(webhook_event.correlated_attempt_id, attempt.pk)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)

    def test_orphan_event_stays_pending_with_retry_scheduled(self):
        row, _ = get_or_create_webhook_event(_event(reference="ref-nomatch"))
        webhook_event, target = process_webhook_event(row.pk)
        webhook_event.refresh_from_db()
        self.assertIsInstance(target, Orphan)
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_PENDING)
        self.assertEqual(webhook_event.retry_count, 1)
        self.assertIsNotNone(webhook_event.next_retry_at)

    def test_already_resolved_event_is_noop(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-done")
        row, _ = get_or_create_webhook_event(_event(reference="ref-done", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        process_webhook_event(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)
        # Second call — must be a pure no-op, no exception, status unchanged.
        webhook_event2, target2 = process_webhook_event(row.pk)
        self.assertIsNone(target2)
        self.assertEqual(webhook_event2.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)

    def test_late_correlation_replay_resolves_and_applies_failed_refund_exactly_once(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="")  # not yet correlatable
        row, _ = get_or_create_webhook_event(_event(reference="ref-late", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        # First attempt: orphan (attempt has no matching ref yet).
        with patch("simulator.tasks.send_email_async.delay"):
            process_webhook_event(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_PENDING)

        # Reference becomes available (simulates late persistence).
        PayoutAttempt.objects.filter(pk=attempt.pk).update(provider_reference="ref-late")
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance

        with patch("simulator.tasks.send_email_async.delay"):
            webhook_event, target = process_webhook_event(row.pk)
        webhook_event.refresh_from_db()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)
        self.assertEqual(self.wallet.available_balance, balance_before + Decimal("100.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )

    def test_finished_replay_no_wallet_movement(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="ref-fin")
        row, _ = get_or_create_webhook_event(_event(reference="ref-fin", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        webhook_event, target = process_webhook_event(row.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_intermediate_event_on_unknown_moves_to_processing(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="ref-int")
        row, _ = get_or_create_webhook_event(_event(reference="ref-int", status=PayoutAttempt.STATUS_PROCESSING, raw="ROLLING"))
        # Must not raise InvalidPayoutAttemptTransition (the bug this block closes).
        webhook_event, target = process_webhook_event(row.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        webhook_event.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)

    def test_handler_exception_leaves_event_unresolved(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-boom")
        row, _ = get_or_create_webhook_event(_event(reference="ref-boom", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        with patch(
            "simulator.payout_orchestrator._apply_attempt_webhook",
            side_effect=RuntimeError("simulated handler failure"),
        ):
            with self.assertRaises(RuntimeError):
                process_webhook_event(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_PENDING)


class CaseECrashSafetyTests(TestCase):
    """Financial transition succeeds, but the process is simulated to
    have crashed BEFORE marking PayoutWebhookEvent RESOLVED — the event
    row is left PENDING by construction (never advanced to TXN2). A
    later replay must recover safely: no second refund, no second
    completion, no second email, no second audit entry — and the event
    finally becomes RESOLVED."""

    def setUp(self):
        self.user = make_user(username="f24_casee_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_crash_before_resolved_then_replay_no_double_refund_no_double_email(self):
        from simulator.payout_orchestrator import _apply_confirmed_failure_with_refund

        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-crash")
        row, _ = get_or_create_webhook_event(_event(reference="ref-crash", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))

        # Simulates TXN1 having already committed (financial transition
        # applied) — event row deliberately left PENDING (TXN2 never ran).
        with patch("simulator.tasks.send_email_async.delay") as email_mock_txn1:
            _apply_confirmed_failure_with_refund(
                attempt.pk, reason="provider callback: FAILED",
                pre_send_rejection=False, raw_provider_status="FAILED",
            )
        self.assertEqual(email_mock_txn1.call_count, 1)
        row.refresh_from_db()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_PENDING, "event must still be PENDING — simulating the crash window")

        balance_after_txn1 = Wallet.objects.get(pk=self.wallet.pk).available_balance
        correction_count_after_txn1 = WalletTransaction.objects.filter(
            wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
        ).count()

        # Replay re-enters — must be fully idempotent.
        with patch("simulator.tasks.send_email_async.delay") as email_mock_replay:
            webhook_event, target = process_webhook_event(row.pk)

        webhook_event.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)
        self.assertEqual(self.wallet.available_balance, balance_after_txn1, "no second refund")
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(),
            correction_count_after_txn1, "no second correction transaction",
        )
        self.assertEqual(email_mock_replay.call_count, 0, "no second FAILED email on the idempotent replay")


# ── 5. Concurrency — replay/replay, live/replay ─────────────────────────

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


class WebhookConcurrencyTests(TransactionTestCase):
    def test_two_workers_same_event_no_duplicate_money(self):
        user = make_user(username="f24_conc_user")
        wallet = make_wallet(user, initial_balance=Decimal("500"))
        wr = _make_pending_wr(user, wallet, "100.00")
        _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-conc")
        row, _ = get_or_create_webhook_event(_event(reference="ref-conc", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))

        def _do():
            with patch("simulator.tasks.send_email_async.delay"):
                return process_webhook_event(row.pk)

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [threading.Thread(target=_run_locked_retry, args=(_do, barrier, results, i)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for r in results:
            self.assertEqual(r[0], "ok", f"unexpected outcome: {results}")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("500.00"), "exactly one refund")
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )
        row.refresh_from_db()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)


# ── 6. Aged SUBMITTED ─────────────────────────────────────────────────────

class AgedSubmittedTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_aged_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_is_submitted_aged_pure_predicate_below_threshold(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_SUBMITTED,
                           submitted_at=timezone.now() - timezone.timedelta(seconds=10))
        self.assertFalse(is_submitted_aged(a, threshold_seconds=300))

    def test_is_submitted_aged_pure_predicate_at_or_above_threshold(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_SUBMITTED,
                           submitted_at=timezone.now() - timezone.timedelta(seconds=301))
        self.assertTrue(is_submitted_aged(a, threshold_seconds=300))

    def test_below_threshold_stays_submitted_after_reconciliation(self):
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_SUBMITTED,
                           submitted_at=timezone.now() - timezone.timedelta(seconds=10))
        reconcile_unknown_payout_attempts()
        a.refresh_from_db()
        self.assertEqual(a.status, PayoutAttempt.STATUS_SUBMITTED)

    def test_at_or_above_threshold_transitions_to_unknown(self):
        from django.conf import settings as _settings
        threshold = _settings.PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS
        a = _make_attempt(self.wr, status=PayoutAttempt.STATUS_SUBMITTED,
                           submitted_at=timezone.now() - timezone.timedelta(seconds=threshold + 5))
        reconcile_unknown_payout_attempts()
        a.refresh_from_db()
        self.assertEqual(a.status, PayoutAttempt.STATUS_UNKNOWN)

    def test_aged_transition_makes_no_provider_call(self):
        from django.conf import settings as _settings
        threshold = _settings.PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_SUBMITTED,
                      submitted_at=timezone.now() - timezone.timedelta(seconds=threshold + 5))
        with patch("simulator.payout_providers.NowPaymentsAdapter.create_payout") as create_mock:
            reconcile_unknown_payout_attempts()
        create_mock.assert_not_called()

    def test_setting_below_minimum_clamped_with_warning(self):
        import logging
        with patch.dict("os.environ", {"PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS": "10"}):
            from trx_simulator.settings import _payout_reconciliation_int_env
            with self.assertLogs("simulator.payout_reconciliation", level="WARNING"):
                value = _payout_reconciliation_int_env("PAYOUT_SUBMITTED_AGED_THRESHOLD_SECONDS", 300, minimum=60)
            self.assertEqual(value, 60)

    def test_setting_invalid_non_int_falls_back_to_default(self):
        with patch.dict("os.environ", {"PAYOUT_WEBHOOK_MAX_AUTO_RETRIES": "not-a-number"}):
            from trx_simulator.settings import _payout_reconciliation_int_env
            with self.assertLogs("simulator.payout_reconciliation", level="WARNING"):
                value = _payout_reconciliation_int_env("PAYOUT_WEBHOOK_MAX_AUTO_RETRIES", 10, minimum=1)
            self.assertEqual(value, 10)


# ── 7. Email/audit idempotency ──────────────────────────────────────────

class EmailAuditIdempotencyTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_email_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_duplicate_completed_webhook_one_email(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-dup-c")
        row, _ = get_or_create_webhook_event(_event(reference="ref-dup-c", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        with patch("simulator.tasks.send_email_async.delay") as email_mock:
            process_webhook_event(row.pk)
            process_webhook_event(row.pk)  # already RESOLVED — no-op
        self.assertEqual(email_mock.call_count, 1)

    def test_duplicate_failed_webhook_one_refund_one_email(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-dup-f")
        row, _ = get_or_create_webhook_event(_event(reference="ref-dup-f", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        with patch("simulator.tasks.send_email_async.delay") as email_mock:
            process_webhook_event(row.pk)
            process_webhook_event(row.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500.00"))
        self.assertEqual(email_mock.call_count, 1)

    def test_audit_log_reflects_exactly_one_economic_resolution(self):
        from simulator.models import AuditLog
        from simulator.audit import EV_WITHDRAW_FAILED
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-audit")
        row, _ = get_or_create_webhook_event(_event(reference="ref-audit", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        with patch("simulator.tasks.send_email_async.delay"):
            process_webhook_event(row.pk)
            process_webhook_event(row.pk)
        self.assertEqual(
            AuditLog.objects.filter(event_type=EV_WITHDRAW_FAILED, detail__payout_attempt_id=attempt.pk).count(),
            1,
        )


# ── 8. provider_request_id ───────────────────────────────────────────────

class ProviderRequestIdTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_prid_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")

    def test_generated_and_persisted_at_creation(self):
        from simulator.payout_state_machine import create_and_submit_payout_attempt
        attempt = create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address, requested_amount_usd=self.wr.amount_usd,
        )
        self.assertTrue(attempt.provider_request_id)
        self.assertNotEqual(attempt.provider_request_id, "")

    def test_separate_field_from_idempotency_key_value_may_match_by_default(self):
        from simulator.payout_state_machine import create_and_submit_payout_attempt
        attempt = create_and_submit_payout_attempt(
            self.wr, provider="nowpayments", requested_asset="btc",
            destination_address=self.wr.wallet_address, requested_amount_usd=self.wr.amount_usd,
        )
        # Same default string today, but genuinely different fields —
        # changing one does not imply reading the other.
        self.assertEqual(attempt.provider_request_id, attempt.idempotency_key)
        PayoutAttempt.objects.filter(pk=attempt.pk).update(provider_request_id="custom-external-value")
        attempt.refresh_from_db()
        self.assertNotEqual(attempt.provider_request_id, attempt.idempotency_key)

    def test_same_request_id_allowed_across_different_providers(self):
        wr2 = _make_pending_wr(self.user, self.wallet, "20.00")
        _make_attempt(self.wr, provider="nowpayments", provider_request_id="shared-123", attempt_number=1)
        # Must not raise IntegrityError — different provider namespace.
        _make_attempt(wr2, provider="fake_recon", provider_request_id="shared-123", attempt_number=1)
        self.assertEqual(
            PayoutAttempt.objects.filter(provider_request_id="shared-123").count(), 2,
        )

    def test_duplicate_same_provider_request_id_rejected(self):
        from django.db import IntegrityError as _IE
        _make_attempt(self.wr, provider="nowpayments", provider_request_id="dupe-1", attempt_number=1)
        wr2 = _make_pending_wr(self.user, self.wallet, "20.00")
        with self.assertRaises(_IE):
            _make_attempt(wr2, provider="nowpayments", provider_request_id="dupe-1", attempt_number=1)

    def test_legacy_blank_provider_request_id_rows_allowed_multiple(self):
        wr2 = _make_pending_wr(self.user, self.wallet, "20.00")
        _make_attempt(self.wr, provider="nowpayments", provider_request_id="", attempt_number=1)
        # Must not raise — blank is excluded from the unique constraint.
        _make_attempt(wr2, provider="nowpayments", provider_request_id="", attempt_number=1)

    def test_lookup_always_provider_scoped(self):
        wr2 = _make_pending_wr(self.user, self.wallet, "20.00")
        a1 = _make_attempt(self.wr, provider="nowpayments", provider_request_id="scoped-1", attempt_number=1)
        a2 = _make_attempt(wr2, provider="fake_recon", provider_request_id="scoped-1", attempt_number=1)
        found = PayoutAttempt.objects.filter(provider="nowpayments", provider_request_id="scoped-1")
        self.assertEqual(list(found), [a1])


# ── 9. Webhook view — HMAC-first durable persistence ────────────────────

PAYOUT_CB_URL = "/withdraw/callback/"


def _ipn_body(payout_id, status, batch_id="batch_view_1"):
    return json.dumps({
        "id": batch_id, "status": status,
        "withdrawals": [{"id": payout_id, "status": status}],
    })


class WebhookViewDurableInboxTests(TestCase):
    def setUp(self):
        self.user = make_user(username="f24_view_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.wr = _make_pending_wr(self.user, self.wallet, "100.00")
        self.client = Client()

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=False)
    def test_invalid_hmac_never_persisted(self, _sig):
        body = _ipn_body("view-bad-sig", "FINISHED")
        resp = self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PayoutWebhookEvent.objects.count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_valid_orphan_webhook_persisted_pending(self, _sig):
        body = _ipn_body("view-orphan-1", "FINISHED")
        resp = self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(PayoutWebhookEvent.objects.count(), 1)
        row = PayoutWebhookEvent.objects.get()
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_PENDING)
        self.assertEqual(row.provider_reference, "view-orphan-1")

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_valid_correlatable_webhook_resolved_same_request(self, _sig):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="view-live-1")
        body = _ipn_body("view-live-1", "FINISHED")
        with patch("simulator.tasks.send_email_async.delay"):
            resp = self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        row = PayoutWebhookEvent.objects.get(provider_reference="view-live-1")
        self.assertEqual(row.correlation_status, PayoutWebhookEvent.STATUS_RESOLVED)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_live_delivery_creates_only_one_durable_row(self, _sig):
        body = _ipn_body("view-dup-1", "FINISHED")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.client.post(PAYOUT_CB_URL, body, content_type="application/json")
        self.assertEqual(
            PayoutWebhookEvent.objects.filter(provider_reference="view-dup-1").count(), 1,
        )
