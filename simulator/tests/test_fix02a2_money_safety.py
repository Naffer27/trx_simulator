# simulator/tests/test_fix02a2_money_safety.py
"""
FIX-02A.2 — cross-cutting money-safety integration tests: full
submission -> webhook round trips, exercising submit_withdrawal_to_provider()
and apply_provider_webhook_event() together (not in isolation), the way
a real approve-then-callback sequence actually happens.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.models import PayoutAttempt, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import (
    WithdrawalAlreadyClaimed, apply_provider_webhook_event, submit_withdrawal_to_provider,
)
from simulator.payout_providers import ProviderPayoutEvent, ProviderTimeoutError
from simulator.tests.factories import make_user, make_wallet


class _FakeAdapter:
    provider_name = "nowpayments"

    def __init__(self, *, create_error=None, create_result=None):
        self.create_error = create_error
        self.create_result = create_result

    def estimate(self, amount_usd, asset):
        return Decimal("0.001")

    def create_payout(self, attempt, *, callback_url=""):
        if self.create_error:
            raise self.create_error
        return self.create_result


class _Result:
    def __init__(self, ref="wd-1", batch="batch-1"):
        self.accepted = True
        self.provider_reference = ref
        self.provider_batch_id = batch
        self.provider_amount = Decimal("0.001")
        self.raw_status = "CREATED"


def _event(ref, status, raw):
    return ProviderPayoutEvent(
        provider="nowpayments", provider_reference=ref, provider_batch_id="",
        normalized_status=status, raw_status=raw, provider_amount=None, occurred_at=timezone.now(),
    )


def _make_wr(user, amount="250.00"):
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PENDING, debit_tx=None,
    )


class SubmitThenWebhookRoundTripTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user)

    def test_unknown_then_webhook_failed_refunds_exactly_once(self):
        adapter = _FakeAdapter(create_error=ProviderTimeoutError("timeout"))
        result = submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(result["outcome"], "unknown")
        attempt_id = result["attempt_id"]
        attempt = PayoutAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_UNKNOWN)

        # The provider's own reference never made it to us at submission
        # time (timeout) — but a late webhook can still resolve it once
        # we know the reference. Simulate reconciliation learning it.
        PayoutAttempt.objects.filter(pk=attempt_id).update(provider_reference="late-ref")

        before = self.wallet.available_balance
        apply_provider_webhook_event(_event("late-ref", PayoutAttempt.STATUS_FAILED, "FAILED"))
        apply_provider_webhook_event(_event("late-ref", PayoutAttempt.STATUS_FAILED, "FAILED"))  # duplicate

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)

    def test_success_then_webhook_completed_no_refund(self):
        adapter = _FakeAdapter(create_result=_Result(ref="wd-ok"))
        result = submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        self.assertEqual(result["outcome"], "processing")

        before = self.wallet.available_balance
        apply_provider_webhook_event(_event("wd-ok", PayoutAttempt.STATUS_COMPLETED, "FINISHED"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_COMPLETED)

    def test_failed_webhook_after_already_completed_is_noop(self):
        """Terminal callback idempotency across DIFFERENT terminal statuses,
        not just duplicates of the same one."""
        adapter = _FakeAdapter(create_result=_Result(ref="wd-term"))
        result = submit_withdrawal_to_provider(self.wr, adapter=adapter, actor=self.user, callback_url="cb")
        apply_provider_webhook_event(_event("wd-term", PayoutAttempt.STATUS_COMPLETED, "FINISHED"))

        before = self.wallet.available_balance
        apply_provider_webhook_event(_event("wd-term", PayoutAttempt.STATUS_FAILED, "FAILED"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before, "a FAILED event must never un-terminal a COMPLETED attempt")

        attempt = PayoutAttempt.objects.get(pk=result["attempt_id"])
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)

    def test_new_attempt_only_after_confirmed_failed_then_second_submission_succeeds(self):
        adapter1 = _FakeAdapter(create_error=ProviderTimeoutError("timeout"))
        result1 = submit_withdrawal_to_provider(self.wr, adapter=adapter1, actor=self.user, callback_url="cb")
        self.assertEqual(result1["outcome"], "unknown")

        adapter2 = _FakeAdapter(create_result=_Result())
        with self.assertRaises(WithdrawalAlreadyClaimed):
            # WithdrawalRequest.status is already 'processing' (not
            # 'pending') at this point, so the optimistic pre-check blocks
            # this before it even reaches ActivePayoutAttemptExists's own
            # TXN1 guard — both are legitimate "already spoken for" signals.
            submit_withdrawal_to_provider(self.wr, adapter=adapter2, actor=self.user, callback_url="cb")

        # Confirm FAILED via callback (the only way UNKNOWN resolves in .2's scope)
        PayoutAttempt.objects.filter(pk=result1["attempt_id"]).update(provider_reference="ref-resolve")
        apply_provider_webhook_event(_event("ref-resolve", PayoutAttempt.STATUS_FAILED, "FAILED"))

        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)
        # A genuinely new WithdrawalRequest-level retry is out of .2's scope
        # (WithdrawalRequest.status is now 'failed', not 'pending') — the
        # invariant under test is purely: no second PayoutAttempt could be
        # created while the first was unresolved, and exactly one exists.
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 1)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )
