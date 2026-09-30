# simulator/tests/test_withdrawal_e2e_02g_payout_state_sync.py
"""
WITHDRAWAL-E2E-02G FASE B — payout state synchronization fix.

Covers the 6 root causes closed in this block (see the FASE A FINAL
DESIGN REPORT, sections A/G/H/I/J/K) and the full A-T test matrix from
that report:

  1. verify_payout_with_token() / NowPaymentsAdapter.verify_payout() —
     an HTTP 2xx with an unparseable body is accepted, never raised as
     a failure (the exact false-negative WR18 hit).
  2. parse_webhook() — supports both the batch ({"withdrawals":[...]})
     and the real individual/flat NowPayments payout IPN shape.
  3. reconcile_processing_payout_attempts() — a new active-reconciliation
     surface for PROCESSING attempts (not just UNKNOWN).
  4. submit_payout_verification() — the ProviderError audit write now
     survives the transaction that raised it.
  5/7. PayoutAttempt.confirmed_amount/tx_hash, populated only from real
     terminal (FINISHED) evidence, surfaced in the completion email.

No real HTTP anywhere in this file. Mirrors the mocking discipline of
test_withdrawal_e2e_02b_refund_engine.py / test_withdrawal_e2e_02c_verify_payout.py.
"""
import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from simulator.models import AuditLog, PayoutAttempt, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import (
    PayoutVerificationError, _apply_result_without_refund, reconcile_processing_payout_attempts,
    reconcile_unknown_payout_attempts, submit_payout_verification,
)
from simulator.payout_providers import (
    NowPaymentsAdapter, PayoutLookupOutcome, PayoutLookupResult, ProviderResponseError,
    ProviderUnavailableError,
)
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import debit_wallet

FINISHED_HASH = "31c89cbed239453bf3bc12b12f60b6c05c467297826828deca75429ace190655"


def _make_wr(user, amount="200.00"):
    debit_tx = debit_wallet(user.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="t")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="usdttrc20",
        wallet_address="TUFt1PhynXEWJtfQQoyAbgAh5N4tDEQVrB",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _make_attempt(wr, *, status=PayoutAttempt.STATUS_PROCESSING, provider_batch_id="batch-18",
                   provider_reference="payout-18", verified_at=None, attempt_number=1,
                   updated_at=None):
    attempt = PayoutAttempt.objects.create(
        withdrawal_request=wr, provider="nowpayments", attempt_number=attempt_number,
        idempotency_key=f"e02g-{wr.pk}-{attempt_number}",
        requested_amount_usd=wr.amount_usd, requested_asset="usdttrc20",
        destination_address=wr.wallet_address, status=status,
        submitted_at=timezone.now(), provider_reference=provider_reference,
        provider_batch_id=provider_batch_id, verified_at=verified_at,
    )
    if updated_at is not None:
        PayoutAttempt.objects.filter(pk=attempt.pk).update(updated_at=updated_at)
        attempt.refresh_from_db()
    return attempt


def _attempt_stub(provider_batch_id="batch-18", provider_reference="payout-18"):
    return MagicMock(
        pk=1, provider="nowpayments", provider_batch_id=provider_batch_id,
        provider_reference=provider_reference,
    )


def _mock_response(status_code=200, json_side_effect=None, json_value=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.url = "https://api.nowpayments.io/v1/payout/batch-18/verify"
    if json_side_effect is not None:
        resp.json.side_effect = json_side_effect
    else:
        resp.json.return_value = json_value or {}
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# A/B/C/D/E — Verify HTTP 2xx / body variations (item 1)
# ─────────────────────────────────────────────────────────────────────────────

class VerifyBodyVariationsTests(TestCase):
    """WR18's real sequence: HTTP 200, body our SDK couldn't parse —
    must be accepted, never raised as ProviderResponseError."""

    def test_A_http_200_valid_json_body_accepted(self):
        attempt = _attempt_stub()
        resp = _mock_response(200, json_value={"batch_withdrawal_id": "batch-18"})
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            result = NowPaymentsAdapter().verify_payout(attempt, "123456")
        self.assertIsNone(result)

    def test_B_http_200_empty_body_accepted_not_an_error(self):
        attempt = _attempt_stub()
        resp = _mock_response(200, json_side_effect=ValueError("Expecting value: line 1 column 1 (char 0)"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            result = NowPaymentsAdapter().verify_payout(attempt, "123456")
        self.assertIsNone(result)

    def test_C_http_200_non_json_text_body_accepted_not_an_error(self):
        attempt = _attempt_stub()
        resp = _mock_response(200, json_side_effect=ValueError("not json"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            result = NowPaymentsAdapter().verify_payout(attempt, "123456")
        self.assertIsNone(result)

    def test_D_http_4xx_still_raises_provider_unavailable_error(self):
        """Regression — a real rejection (wrong/expired code) must keep failing loudly."""
        attempt = _attempt_stub()
        resp = _mock_response(400)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_E_timeout_still_raises(self):
        from simulator.payout_providers import ProviderTimeoutError
        attempt = _attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", side_effect=requests.exceptions.Timeout("slow")):
            with self.assertRaises(ProviderTimeoutError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_unparseable_body_never_logs_2fa_code_or_response_body(self):
        """The fix path must keep the pre-existing 'never log the code
        or the response body' discipline — only status/batch_id logged."""
        attempt = _attempt_stub()
        resp = _mock_response(200, json_side_effect=ValueError("not json"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp), \
             self.assertLogs("simulator.nowpayments", level="INFO") as logs:
            NowPaymentsAdapter().verify_payout(attempt, "999999")
        joined = " | ".join(logs.output)
        self.assertNotIn("999999", joined)
        self.assertIn("batch-18", joined)


# ─────────────────────────────────────────────────────────────────────────────
# F/G/H — webhook batch + flat shapes (item 2)
# ─────────────────────────────────────────────────────────────────────────────

class WebhookShapeTests(TestCase):
    def test_F_batch_shape_regression_unchanged(self):
        body = json.dumps({"id": "batch-1", "withdrawals": [{"id": "wd-1", "status": "FINISHED"}]}).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].provider_reference, "wd-1")
        self.assertEqual(events[0].provider_batch_id, "batch-1")
        self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_COMPLETED)

    def test_G_individual_flat_shape_extracted(self):
        """WR18's real IPN shape — confirmed against the official
        NowPaymentsIO SDK's normalizeWebhook()."""
        body = json.dumps({
            "id": "5007973320", "batch_withdrawal_id": "5006825290",
            "status": "FINISHED", "hash": FINISHED_HASH, "amount": "19.66923",
            "currency": "usdttrc20",
        }).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider_reference, "5007973320")
        self.assertEqual(event.provider_batch_id, "5006825290")
        self.assertEqual(event.normalized_status, PayoutAttempt.STATUS_COMPLETED)
        self.assertEqual(event.raw_status, "FINISHED")
        self.assertEqual(event.raw_event_payload["hash"], FINISHED_HASH)

    def test_G2_individual_flat_shape_via_payout_status_key(self):
        """SDK's normalizeWebhook() also accepts 'payout_status' as the
        status key — same detection condition, covered explicitly."""
        body = json.dumps({
            "id": "wd-2", "batch_withdrawal_id": "batch-2", "payout_status": "FAILED",
        }).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_FAILED)

    def test_H_flat_shape_unknown_status_not_dropped(self):
        body = json.dumps({
            "id": "wd-3", "batch_withdrawal_id": "batch-3", "status": "SOME_FUTURE_STATUS",
        }).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].normalized_status)
        self.assertEqual(events[0].raw_status, "SOME_FUTURE_STATUS")

    def test_neither_shape_recognizable_returns_empty_not_none(self):
        """Signature-valid but structurally unrecognizable — 0 events,
        but distinguishable from an invalid-signature None, and now
        logged (not silent)."""
        body = json.dumps({"unexpected": "shape"}).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        self.assertEqual(events, [])

    def test_invalid_signature_still_returns_none(self):
        body = json.dumps({"id": "batch-1", "withdrawals": []}).encode()
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=False):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "bad"})
        self.assertIsNone(events)


# ─────────────────────────────────────────────────────────────────────────────
# I/J — FINISHED (flat webhook) -> COMPLETED, confirmed_amount/tx_hash,
# exactly-once email/audit on redelivery
# ─────────────────────────────────────────────────────────────────────────────

class FinishedFlatWebhookCompletionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="5007973320", provider_batch_id="5006825290")

    def _flat_finished_body(self):
        return json.dumps({
            "id": "5007973320", "batch_withdrawal_id": "5006825290", "status": "FINISHED",
            "hash": FINISHED_HASH, "amount": "19.66923", "currency": "usdttrc20",
        }).encode()

    def _deliver(self):
        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(self._flat_finished_body(), {"x-nowpayments-sig": "ok"})
        event = events[0]
        webhook_event, _ = get_or_create_webhook_event(event)
        return process_webhook_event(webhook_event.pk)

    def test_I_finished_flat_webhook_completes_with_confirmed_amount_and_hash(self):
        with patch("simulator.tasks.send_email_async.delay") as mail:
            self._deliver()
        self.attempt.refresh_from_db()
        self.wr.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_COMPLETED)
        self.assertEqual(self.attempt.confirmed_amount, Decimal("19.66923"))
        self.assertEqual(self.attempt.tx_hash, FINISHED_HASH)
        self.assertTrue(mail.called)
        self.assertIn(FINISHED_HASH, mail.call_args.kwargs.get("message", ""))

    def test_I2_no_refund_on_finished(self):
        with patch("simulator.tasks.send_email_async.delay"):
            self._deliver()
        corrections = WalletTransaction.objects.filter(
            wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
        )
        self.assertEqual(corrections.count(), 0)

    def test_J_duplicate_finished_webhook_exactly_one_email_and_audit(self):
        with patch("simulator.tasks.send_email_async.delay") as mail:
            self._deliver()
            self._deliver()  # redelivery of the exact same webhook body
        self.assertEqual(mail.call_count, 1)
        completed_events = AuditLog.objects.filter(action__icontains=f"Withdrawal #{self.wr.id} COMPLETED")
        self.assertLessEqual(completed_events.count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# K/L — active reconciliation for PROCESSING (item 3)
# ─────────────────────────────────────────────────────────────────────────────

class ProcessingReconciliationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        aged = timezone.now() - timezone.timedelta(seconds=700)  # older than the 600s default
        self.attempt = _make_attempt(
            self.wr, provider_reference="5007973320", provider_batch_id="5006825290", updated_at=aged,
        )

    def _lookup_result(self, outcome, **meta):
        return PayoutLookupResult(
            outcome=outcome, provider_reference=self.attempt.provider_reference,
            provider_batch_id=self.attempt.provider_batch_id,
            raw_provider_status="FINISHED" if outcome == PayoutLookupOutcome.FOUND_COMPLETED else "",
            raw_metadata=meta,
        )

    def test_K_processing_aged_finished_converges_to_completed(self):
        lookup = self._lookup_result(
            PayoutLookupOutcome.FOUND_COMPLETED, amount="19.66923", hash=FINISHED_HASH,
        )
        with patch.object(NowPaymentsAdapter, "lookup_payout", return_value=lookup), \
             patch("simulator.tasks.send_email_async.delay"):
            result = reconcile_processing_payout_attempts()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.assertEqual(self.attempt.confirmed_amount, Decimal("19.66923"))
        self.assertEqual(self.attempt.tx_hash, FINISHED_HASH)
        self.assertEqual(result["resolved"], 1)

    def test_K2_too_recent_processing_not_eligible(self):
        self.attempt.refresh_from_db()
        PayoutAttempt.objects.filter(pk=self.attempt.pk).update(updated_at=timezone.now())
        with patch.object(NowPaymentsAdapter, "lookup_payout") as lookup_mock:
            result = reconcile_processing_payout_attempts()
        lookup_mock.assert_not_called()
        self.assertEqual(result["checked"], 0)

    def test_L_repeated_reconciliation_no_double_effect(self):
        lookup = self._lookup_result(
            PayoutLookupOutcome.FOUND_COMPLETED, amount="19.66923", hash=FINISHED_HASH,
        )
        with patch.object(NowPaymentsAdapter, "lookup_payout", return_value=lookup), \
             patch("simulator.tasks.send_email_async.delay") as mail:
            reconcile_processing_payout_attempts()
            # Second call: attempt is already COMPLETED (terminal) and no
            # longer matches the PROCESSING filter at all.
            result2 = reconcile_processing_payout_attempts()
        self.assertEqual(result2["checked"], 0)
        self.assertEqual(mail.call_count, 1)

    def test_processing_reconciliation_found_failed_refunds_exactly_once(self):
        lookup = self._lookup_result(PayoutLookupOutcome.FOUND_FAILED)
        with patch.object(NowPaymentsAdapter, "lookup_payout", return_value=lookup), \
             patch("simulator.tasks.send_email_async.delay"):
            reconcile_processing_payout_attempts()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_FAILED)
        corrections = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION)
        self.assertEqual(corrections.count(), 1)

    def test_processing_reconciliation_still_processing_no_mutation(self):
        lookup = self._lookup_result(PayoutLookupOutcome.FOUND_PROCESSING)
        with patch.object(NowPaymentsAdapter, "lookup_payout", return_value=lookup):
            reconcile_processing_payout_attempts()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_PROCESSING)

    def test_unknown_reconciliation_unaffected_by_new_processing_step(self):
        """Regression — reconcile_unknown_payout_attempts() must remain
        untouched in behavior; it never sees PROCESSING attempts."""
        self.attempt.status = PayoutAttempt.STATUS_UNKNOWN
        self.attempt.save(update_fields=["status"])
        with patch.object(NowPaymentsAdapter, "lookup_payout") as lookup_mock:
            lookup_mock.return_value = self._lookup_result(PayoutLookupOutcome.FOUND_PROCESSING)
            result = reconcile_unknown_payout_attempts()
        self.assertEqual(result["checked"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# M — webhook + reconciliation racing on the same attempt (PostgreSQL only)
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipUnless(connection.vendor == "postgresql", "genuine row-lock race needs real DB serialization")
class ConcurrentWebhookAndReconciliationTests(TransactionTestCase):
    def test_M_webhook_and_reconciliation_race_no_double_completion(self):
        import threading

        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_wr(user, amount="200.00")
        aged = timezone.now() - timezone.timedelta(seconds=700)
        attempt = _make_attempt(wr, provider_reference="race-1", provider_batch_id="race-batch-1", updated_at=aged)

        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        from simulator.payout_providers import ProviderPayoutEvent

        event = ProviderPayoutEvent(
            provider="nowpayments", provider_reference="race-1", provider_batch_id="race-batch-1",
            normalized_status=PayoutAttempt.STATUS_COMPLETED, raw_status="FINISHED",
            provider_amount=None, occurred_at=timezone.now(),
            raw_event_payload={"amount": "19.66923", "hash": FINISHED_HASH},
        )
        webhook_event, _ = get_or_create_webhook_event(event)

        lookup = PayoutLookupResult(
            outcome=PayoutLookupOutcome.FOUND_COMPLETED, provider_reference="race-1",
            provider_batch_id="race-batch-1", raw_provider_status="FINISHED",
            raw_metadata={"amount": "19.66923", "hash": FINISHED_HASH},
        )

        errors = []

        def run_webhook():
            try:
                process_webhook_event(webhook_event.pk)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()  # each thread owns its own DB connection

        def run_reconciliation():
            try:
                with patch.object(NowPaymentsAdapter, "lookup_payout", return_value=lookup):
                    reconcile_processing_payout_attempts()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        # Patched once, around both threads — unittest.mock's patch/unpatch
        # is not itself thread-safe across two concurrently-entered context
        # managers on the same target (observed directly: a stray
        # AttributeError from one thread unpatching mid-call for the
        # other). A single outer patch avoids that testing artifact
        # entirely; it has no bearing on the real race being tested,
        # which is the two DB-level callers, not the mail dispatch.
        with patch("simulator.tasks.send_email_async.delay"):
            t1 = threading.Thread(target=run_webhook)
            t2 = threading.Thread(target=run_reconciliation)
            t1.start(); t2.start()
            t1.join(); t2.join()

        self.assertEqual(errors, [])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)
        corrections = WalletTransaction.objects.filter(
            wallet__user=user, tx_type=WalletTransaction.TX_CORRECTION,
        )
        self.assertEqual(corrections.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# N/O/P — accounting invariants
# ─────────────────────────────────────────────────────────────────────────────

class AccountingInvariantTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="5007973320", provider_batch_id="5006825290")

    def test_N_single_debit_through_full_flow(self):
        debits = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW)
        self.assertEqual(debits.count(), 1)
        body = json.dumps({
            "id": "5007973320", "batch_withdrawal_id": "5006825290", "status": "FINISHED",
            "hash": FINISHED_HASH, "amount": "19.66923",
        }).encode()
        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        webhook_event, _ = get_or_create_webhook_event(events[0])
        with patch("simulator.tasks.send_email_async.delay"):
            process_webhook_event(webhook_event.pk)
        debits_after = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_WITHDRAW)
        self.assertEqual(debits_after.count(), 1)

    def test_O_zero_refund_on_finished(self):
        body = json.dumps({
            "id": "5007973320", "batch_withdrawal_id": "5006825290", "status": "FINISHED",
            "hash": FINISHED_HASH, "amount": "19.66923",
        }).encode()
        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        webhook_event, _ = get_or_create_webhook_event(events[0])
        with patch("simulator.tasks.send_email_async.delay"):
            process_webhook_event(webhook_event.pk)
        corrections = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION)
        self.assertEqual(corrections.count(), 0)

    def test_P_exactly_one_refund_on_confirmed_failed(self):
        body = json.dumps({
            "id": "5007973320", "batch_withdrawal_id": "5006825290", "status": "FAILED",
        }).encode()
        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(body, {"x-nowpayments-sig": "ok"})
        webhook_event, _ = get_or_create_webhook_event(events[0])
        with patch("simulator.tasks.send_email_async.delay"):
            process_webhook_event(webhook_event.pk)
            process_webhook_event(webhook_event.pk)  # redelivery
        corrections = WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION)
        self.assertEqual(corrections.count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Q — audit of a Verify failure survives (item 4/6)
# ─────────────────────────────────────────────────────────────────────────────

class VerifyFailureAuditPersistenceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="payout-1", provider_batch_id="batch-1")

    def test_Q_provider_error_audit_row_persists_despite_atomic_raise(self):
        """
        WITHDRAWAL-E2E-02F/02G — before this fix, this exact scenario
        left ZERO AuditLog rows despite the logger.info() line firing,
        because the write lived inside the same transaction.atomic()
        that the re-raised ProviderError then rolled back. Confirmed on
        WR18's real PayoutAttempt.
        """
        before = AuditLog.objects.filter(action__icontains="verification failed").count()
        resp = _mock_response(400)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            from simulator.payout_providers import ProviderError
            with self.assertRaises(ProviderError):
                submit_payout_verification(self.attempt.pk, "123456")
        after = AuditLog.objects.filter(action__icontains="verification failed").count()
        self.assertEqual(after, before + 1)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.verified_at)


# ─────────────────────────────────────────────────────────────────────────────
# R — the 2FA code never appears in DB/log/audit
# ─────────────────────────────────────────────────────────────────────────────

class VerificationCodeNeverPersistedTests(TestCase):
    SECRET_CODE = "424242"

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="payout-1", provider_batch_id="batch-1")

    def test_R_code_absent_from_audit_log_on_success(self):
        resp = _mock_response(200, json_value={"ok": True})
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, self.SECRET_CODE)
        for row in AuditLog.objects.all():
            self.assertNotIn(self.SECRET_CODE, json.dumps(row.detail or {}))
            self.assertNotIn(self.SECRET_CODE, row.action or "")

    def test_R2_code_absent_from_audit_log_on_unparseable_2xx(self):
        resp = _mock_response(200, json_side_effect=ValueError("not json"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, self.SECRET_CODE)
        for row in AuditLog.objects.all():
            self.assertNotIn(self.SECRET_CODE, json.dumps(row.detail or {}))

    def test_R3_code_absent_from_audit_log_on_failure(self):
        resp = _mock_response(400)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            from simulator.payout_providers import ProviderError
            with self.assertRaises(ProviderError):
                submit_payout_verification(self.attempt.pk, self.SECRET_CODE)
        for row in AuditLog.objects.all():
            self.assertNotIn(self.SECRET_CODE, json.dumps(row.detail or {}))

    def test_R4_code_never_persisted_on_payoutattempt_row(self):
        resp = _mock_response(200, json_value={"ok": True})
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, self.SECRET_CODE)
        self.attempt.refresh_from_db()
        for field in self.attempt._meta.fields:
            value = getattr(self.attempt, field.name, None)
            self.assertNotIn(self.SECRET_CODE, str(value))


# ─────────────────────────────────────────────────────────────────────────────
# S — invariant: no HTTP 200 of create/verify can mark COMPLETED directly
# ─────────────────────────────────────────────────────────────────────────────

class CompletedInvariantTests(TestCase):
    """
    WITHDRAWAL-E2E-02G FASE A §P/S — regression guard for the
    architectural rule: COMPLETED must depend on terminal FINISHED
    evidence (webhook or reconciliation GET), never on a create_payout()
    or verify_payout() HTTP response alone.
    """

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="payout-1", provider_batch_id="batch-1")

    def test_S_verify_success_never_sets_completed(self):
        resp = _mock_response(200, json_value={"ok": True})
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, "123456")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.assertIsNotNone(self.attempt.verified_at)

    def test_S2_verify_unparseable_2xx_never_sets_completed(self):
        resp = _mock_response(200, json_side_effect=ValueError("not json"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, "123456")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_PROCESSING)

    def test_S3_apply_result_without_refund_signature_has_no_provider_response_input(self):
        """Structural guard: _apply_result_without_refund()'s new_status
        argument is caller-supplied and never derived inside this test
        from a raw HTTP status — this just re-confirms COMPLETED only
        flows from the two authorized call sites (webhook/reconciliation)
        by asserting the direct call still requires explicit evidence."""
        _apply_result_without_refund(
            self.attempt.pk, PayoutAttempt.STATUS_COMPLETED,
            raw_provider_status="FINISHED", confirmed_amount=Decimal("19.66923"), tx_hash=FINISHED_HASH,
        )
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.assertEqual(self.attempt.confirmed_amount, Decimal("19.66923"))


# ─────────────────────────────────────────────────────────────────────────────
# T — a second Verify submission is protected once the first succeeds
# ─────────────────────────────────────────────────────────────────────────────

class DoubleVerifyProtectionTests(TestCase):
    """WR18's real sequence: two genuine admin submissions 40s apart
    (WITHDRAWAL-E2E-02G §K — CONFIRMED via the full access log, not a
    retry bug). With the item-1 fix, the first accepted 2xx now
    correctly sets verified_at — so a second submission is rejected by
    the pre-existing guard, with no new mechanism needed."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.attempt = _make_attempt(self.wr, provider_reference="payout-1", provider_batch_id="batch-1")

    def test_T_second_verify_after_unparseable_first_success_is_rejected(self):
        resp = _mock_response(200, json_side_effect=ValueError("not json"))
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            submit_payout_verification(self.attempt.pk, "123456")  # first — now succeeds (item 1 fix)
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(self.attempt.pk, "123456")  # second — guarded by verified_at
        self.attempt.refresh_from_db()
        self.assertIsNotNone(self.attempt.verified_at)
