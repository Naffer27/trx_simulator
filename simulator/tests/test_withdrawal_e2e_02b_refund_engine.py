# simulator/tests/test_withdrawal_e2e_02b_refund_engine.py
"""
WITHDRAWAL-E2E-02B FASE B — wiring the existing, already-proven-in-
production refund engine (_apply_confirmed_failure_with_refund(),
unmodified) up to two new signal sources it never received before:

  1. REJECTED as a recognized terminal-failure webhook status (Bug A/2
     from the design report — this exact gap silently dropped WR17's
     two real REJECTED webhooks, see WITHDRAWAL-E2E-02A).
  2. A genuine GET /v1/payout/{id} implementation for lookup_payout(),
     confirmed real and working against a live payout in 02A.

Also covers Bug B (create_payout() reading the individual withdrawal's
status from the wrong JSON level) and the "never silently discard a
signature-valid webhook" fix (unrecognized raw_status persists with
normalized_status=None, routed to MANUAL_REVIEW, zero financial
mutation attempted — never guessed).

No real HTTP anywhere in this file — simulator.nowpayments's own
requests calls are mocked, same pattern as test_fix02a2_adapter.py /
test_fix02a4_payout_reconciliation.py.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from simulator.models import PayoutAttempt, PayoutWebhookEvent, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import (
    AttemptMatch, apply_provider_webhook_event, get_or_create_webhook_event,
    process_webhook_event, reconcile_unknown_payout_attempts,
)
from simulator.payout_providers import (
    NowPaymentsAdapter, PayoutLookupOutcome, PayoutLookupResult, ProviderPayoutEvent,
)
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import debit_wallet


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures — same shape as test_fix02a2_webhook.py's own helpers
# ─────────────────────────────────────────────────────────────────────────────

def _event(*, reference="", batch="", normalized_status=PayoutAttempt.STATUS_PROCESSING, raw="ROLLING"):
    return ProviderPayoutEvent(
        provider="nowpayments", provider_reference=reference, provider_batch_id=batch,
        normalized_status=normalized_status, raw_status=raw, provider_amount=None,
        occurred_at=timezone.now(),
    )


def _make_wr(user, amount="200.00"):
    debit_tx = debit_wallet(user.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="t")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="usdttrc20",
        wallet_address="TUFt1PhynXEWJtfQQoyAbgAh5N4tDEQVrB",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _make_attempt(wr, *, status, attempt_number=1, provider_reference=""):
    return PayoutAttempt.objects.create(
        withdrawal_request=wr, provider="nowpayments", attempt_number=attempt_number,
        idempotency_key=f"e02b-{wr.pk}-{attempt_number}-{provider_reference or 'x'}",
        requested_amount_usd=wr.amount_usd, requested_asset="usdttrc20",
        destination_address=wr.wallet_address, status=status,
        submitted_at=timezone.now(), provider_reference=provider_reference,
    )


def _fake_attempt(**overrides):
    """A lightweight stand-in — create_payout()/lookup_payout() only read
    a handful of plain attributes, no DB needed."""
    class _Attempt:
        pass
    a = _Attempt()
    a.pk = overrides.get("pk", 1)
    a.destination_address = overrides.get("destination_address", "TUFt1PhynXEWJtfQQoyAbgAh5N4tDEQVrB")
    a.requested_asset = overrides.get("requested_asset", "usdttrc20")
    a.provider_amount = overrides.get("provider_amount", Decimal("19.65"))
    a.withdrawal_request_id = overrides.get("withdrawal_request_id", 1)
    a.provider_reference = overrides.get("provider_reference", "payout-123")
    return a


# ─────────────────────────────────────────────────────────────────────────────
# Item — REJECTED raw status maps to FAILED (parse_webhook level)
# ─────────────────────────────────────────────────────────────────────────────

class RejectedStatusMappingTests(SimpleTestCase):
    def _body(self, status="REJECTED", wd_id="wd-1"):
        return json.dumps({"id": "batch-1", "withdrawals": [{"id": wd_id, "status": status}]}).encode()

    def test_rejected_raw_status_maps_to_failed(self):
        """Reproduces WR17's exact real webhook shape (WITHDRAWAL-E2E-02A)."""
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(self._body(), {"x-nowpayments-sig": "ok"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_FAILED)
        self.assertEqual(events[0].raw_status, "REJECTED")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 & 2 — REJECTED via webhook -> FAILED + refund exactly once,
# never twice on redelivery
# ─────────────────────────────────────────────────────────────────────────────

class RejectedWebhookRefundTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.wallet.refresh_from_db()

    def test_rejected_webhook_transitions_to_failed_and_refunds_exactly_once(self):
        """Reproduces WR17's exact real scenario: a PROCESSING attempt
        (already accepted by the provider, np_payout_id populated)
        receives a REJECTED webhook — must reach FAILED and refund,
        reusing _apply_confirmed_failure_with_refund() unmodified."""
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="wr17-like")
        before = self.wallet.available_balance
        target = apply_provider_webhook_event(
            _event(reference="wr17-like", normalized_status=PayoutAttempt.STATUS_FAILED, raw="REJECTED"),
        )
        self.assertIsInstance(target, AttemptMatch)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )

    def test_duplicate_rejected_webhook_never_double_refunds(self):
        """A second, identical REJECTED webhook (redelivery) must never
        credit the wallet twice — the TERMINAL_STATUSES guard inside
        the refund engine, unmodified by this block."""
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="dup-ref")
        before = self.wallet.available_balance
        event = _event(reference="dup-ref", normalized_status=PayoutAttempt.STATUS_FAILED, raw="REJECTED")
        apply_provider_webhook_event(event)
        apply_provider_webhook_event(event)  # exact redelivery
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)  # once, not twice
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — unknown status: persisted, MANUAL_REVIEW, zero balance movement
# (the REAL durable-inbox path production uses)
# ─────────────────────────────────────────────────────────────────────────────

class UnknownStatusManualReviewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="150.00")
        self.wallet.refresh_from_db()

    def test_unknown_status_persisted_and_routed_to_manual_review_no_balance_change(self):
        """Via the exact production path: views.py::withdraw_payout_callback
        -> get_or_create_webhook_event() -> process_webhook_event() —
        not the simpler direct apply_provider_webhook_event() used above,
        because only the durable path has a PayoutWebhookEvent row whose
        correlation_status can (and must) become MANUAL_REVIEW."""
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="mystery-ref")
        before = self.wallet.available_balance
        event = _event(reference="mystery-ref", normalized_status=None, raw="SOME_BRAND_NEW_STATUS")

        webhook_event, created = get_or_create_webhook_event(event)
        self.assertTrue(created)
        self.assertEqual(webhook_event.raw_status, "SOME_BRAND_NEW_STATUS")
        self.assertEqual(webhook_event.normalized_status, "", "None persists as blank, never a guessed status")

        process_webhook_event(webhook_event.pk)

        webhook_event.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_MANUAL_REVIEW)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING, "untouched — no transition attempted")
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING, "untouched")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before, "zero balance movement")
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 0,
        )

    def test_replaying_the_same_manual_review_event_stays_manual_review(self):
        """Re-processing (e.g. a future admin action, or the periodic
        replay task if it were ever pointed at MANUAL_REVIEW rows) must
        stay idempotent — never mutate money on a second pass either."""
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="mystery-ref-2")
        before = self.wallet.available_balance
        event = _event(reference="mystery-ref-2", normalized_status=None, raw="ANOTHER_NEW_STATUS")
        webhook_event, _ = get_or_create_webhook_event(event)
        process_webhook_event(webhook_event.pk)
        process_webhook_event(webhook_event.pk)  # second pass
        webhook_event.refresh_from_db()
        self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_MANUAL_REVIEW)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — lookup_payout() over the full status vocabulary
# ─────────────────────────────────────────────────────────────────────────────

class LookupPayoutTests(SimpleTestCase):
    def _mock_response(self, *, status_code=200, wd_status="FINISHED", wd_id="payout-123", batch_id="batch-1"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = 200 <= status_code < 300
        resp.json.return_value = {"id": batch_id, "withdrawals": [{"id": wd_id, "status": wd_status}]}
        return resp

    def test_finished_maps_to_found_completed(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(wd_status="FINISHED")):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.FOUND_COMPLETED)
        self.assertEqual(result.raw_provider_status, "FINISHED")

    def test_failed_maps_to_found_failed(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(wd_status="FAILED")):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.FOUND_FAILED)

    def test_rejected_maps_to_found_failed(self):
        """The exact real status WR17 returned (WITHDRAWAL-E2E-02A)."""
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(wd_status="REJECTED")):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.FOUND_FAILED)
        self.assertEqual(result.raw_provider_status, "REJECTED")

    def test_in_flight_statuses_map_to_found_processing(self):
        for raw in ("CREATING", "ROLLING", "CREATED", "SENDING"):
            with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
                 patch("simulator.payout_providers.requests.get", return_value=self._mock_response(wd_status=raw)):
                result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
            self.assertEqual(result.outcome, PayoutLookupOutcome.FOUND_PROCESSING, f"raw={raw}")

    def test_404_maps_to_not_found(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(status_code=404)):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.NOT_FOUND)

    def test_timeout_maps_to_unavailable(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", side_effect=requests.exceptions.Timeout("slow")):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.UNAVAILABLE)

    def test_5xx_maps_to_unavailable(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(status_code=500)):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.UNAVAILABLE)

    def test_unrecognized_status_maps_to_ambiguous_not_a_guess(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.payout_providers.requests.get", return_value=self._mock_response(wd_status="SOME_FUTURE_STATUS")):
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.AMBIGUOUS)

    def test_no_provider_reference_is_unsupported_no_http_call(self):
        with patch("simulator.payout_providers.requests.get") as get_mock:
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt(provider_reference=""))
        self.assertEqual(result.outcome, PayoutLookupOutcome.UNSUPPORTED)
        get_mock.assert_not_called()

    def test_auth_failure_maps_to_unavailable_no_get_attempted(self):
        with patch("simulator.nowpayments._get_jwt_token", side_effect=requests.exceptions.Timeout("down")), \
             patch("simulator.payout_providers.requests.get") as get_mock:
            result = NowPaymentsAdapter().lookup_payout(_fake_attempt())
        self.assertEqual(result.outcome, PayoutLookupOutcome.UNAVAILABLE)
        get_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Bug B: withdrawals[0].status read from the correct JSON level
# ─────────────────────────────────────────────────────────────────────────────

class CreatePayoutBugBFixTests(SimpleTestCase):
    def test_individual_withdrawal_status_read_from_correct_json_level(self):
        """Reproduces WR17's exact real response body shape
        (WITHDRAWAL-E2E-02A): the outer object has NO "status" key at
        all — only withdrawals[0].status does. Before this fix,
        raw_status was always "" for every successful submission."""
        attempt = _fake_attempt()
        response_body = {
            "id": "5006811751",
            "withdrawals": [{"id": "5007958386", "status": "CREATING"}],
        }
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", return_value=response_body):
            result = NowPaymentsAdapter().create_payout(attempt, callback_url="https://cb")
        self.assertEqual(result.raw_status, "CREATING")
        self.assertEqual(result.provider_reference, "5007958386")
        self.assertEqual(result.provider_batch_id, "5006811751")

    def test_empty_withdrawals_list_yields_empty_raw_status_not_a_crash(self):
        attempt = _fake_attempt()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", return_value={"id": "b1", "withdrawals": []}):
            result = NowPaymentsAdapter().create_payout(attempt, callback_url="https://cb")
        self.assertEqual(result.raw_status, "")
        self.assertEqual(result.provider_reference, "")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — repeated reconciliation refunds exactly once
# ─────────────────────────────────────────────────────────────────────────────

class ReconciliationRepeatedRefundTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="120.00")
        self.wallet.refresh_from_db()

    def test_repeated_reconciliation_call_refunds_exactly_once(self):
        """reconcile_unknown_payout_attempts() called twice in a row —
        the attempt leaves UNKNOWN after the first call (now FAILED,
        terminal), so the second call's own query naturally excludes it;
        the invariant this proves is that calling the whole reconciliation
        pass repeatedly is safe, not just a single lookup."""
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="recon-ref")
        before = self.wallet.available_balance
        with patch(
            "simulator.payout_providers.NowPaymentsAdapter.lookup_payout",
            return_value=PayoutLookupResult(outcome=PayoutLookupOutcome.FOUND_FAILED, raw_provider_status="REJECTED"),
        ):
            reconcile_unknown_payout_attempts()
            reconcile_unknown_payout_attempts()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)

    def test_refund_applier_called_twice_directly_is_idempotent(self):
        """Belt-and-suspenders, at the lowest level: even calling the
        refund applier itself twice for the same attempt (e.g. two
        racing reconciliation workers that both read FOUND_FAILED
        before either commits) must never double-refund. The
        TERMINAL_STATUSES guard inside _apply_confirmed_failure_with_
        refund() — unmodified by this block — is what enforces this."""
        from simulator.payout_orchestrator import _apply_confirmed_failure_with_refund
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="direct-ref")
        before = self.wallet.available_balance
        _apply_confirmed_failure_with_refund(
            attempt.pk, reason="first call", pre_send_rejection=False, raw_provider_status="REJECTED",
        )
        _apply_confirmed_failure_with_refund(
            attempt.pk, reason="second call — must be a no-op", pre_send_rejection=False, raw_provider_status="REJECTED",
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — regression: the pre-existing payout flow keeps working unchanged
# ─────────────────────────────────────────────────────────────────────────────

class RegressionExistingPayoutFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="90.00")
        self.wallet.refresh_from_db()

    def test_finished_still_completes_with_no_refund(self):
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="reg-fin")
        before = self.wallet.available_balance
        apply_provider_webhook_event(
            _event(reference="reg-fin", normalized_status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"),
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_COMPLETED)

    def test_ordinary_failed_raw_status_still_refunds_exactly_once(self):
        """The pre-existing "FAILED" raw status (distinct from the new
        "REJECTED" one added by this block) must keep working exactly
        as it did before."""
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="reg-failed")
        before = self.wallet.available_balance
        apply_provider_webhook_event(
            _event(reference="reg-failed", normalized_status=PayoutAttempt.STATUS_FAILED, raw="FAILED"),
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)

    def test_rolling_and_created_still_map_to_processing_not_failed(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            for raw in ("ROLLING", "CREATED"):
                events = NowPaymentsAdapter().parse_webhook(
                    json.dumps({"id": "b", "withdrawals": [{"id": "w", "status": raw}]}).encode(),
                    {"x-nowpayments-sig": "ok"},
                )
                self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_PROCESSING, raw)
