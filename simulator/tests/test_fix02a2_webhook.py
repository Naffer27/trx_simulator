# simulator/tests/test_fix02a2_webhook.py
"""
FIX-02A.2 — webhook correlation (resolve_payout_target) and webhook
application (apply_provider_webhook_event) tests.

Covers the full Design Lock Final + both correction passes: precedence
of provider_reference over provider_batch_id, the "never infer identity
by ID union" fix, DataIntegrityViolation vs Ambiguous as distinct
categories, the early-webhook bounded retry (exact query/sleep count),
and FUNDED_INTERNAL / legacy-pre-.2 delegation via the new correlation
path (not just the old direct np_* lookup already covered by
test_withdrawals.py).
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from simulator.challenge_engine import (
    activate_challenge_enrollment, advance_to_funded, advance_to_phase2,
)
from simulator.models import (
    ChallengeEnrollment, ChallengeProduct, FundedConfig, FundedPayoutRequest,
    LedgerEntry, PayoutAttempt, Wallet, WalletTransaction, WithdrawalRequest,
)
from simulator.payout_orchestrator import (
    Ambiguous, AttemptMatch, DataIntegrityViolation, FundedInternalMatch,
    LegacyWithdrawalMatch, Orphan, apply_provider_webhook_event, resolve_payout_target,
)
from simulator.payout_providers import ProviderPayoutEvent
from simulator.tests.factories import make_user, make_wallet

User = get_user_model()
_seq = 0


def _event(*, reference="", batch="", status=PayoutAttempt.STATUS_PROCESSING, raw="ROLLING"):
    return ProviderPayoutEvent(
        provider="nowpayments", provider_reference=reference, provider_batch_id=batch,
        normalized_status=status, raw_status=raw, provider_amount=None,
        occurred_at=timezone.now(),
    )


def _make_wr(user, amount="200.00"):
    from simulator.wallet_ledger import debit_wallet
    debit_tx = debit_wallet(user.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="t")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _make_attempt(wr, *, status, attempt_number=1, provider_reference="", provider_batch_id=""):
    return PayoutAttempt.objects.create(
        withdrawal_request=wr, provider="nowpayments", attempt_number=attempt_number,
        idempotency_key=f"key-{wr.pk}-{attempt_number}-{provider_reference or 'x'}",
        requested_amount_usd=wr.amount_usd, requested_asset="btc",
        destination_address=wr.wallet_address, status=status,
        submitted_at=timezone.now(), provider_reference=provider_reference,
        provider_batch_id=provider_batch_id,
    )


# ── FUNDED_INTERNAL fixture helpers (same shape as test_funded_payout_internal_approval.py) ──

def _make_admin():
    global _seq
    _seq += 1
    return User.objects.create_user(username=f"a2_admin_{_seq}", email=f"a2_admin_{_seq}@x.com",
                                     password="p", is_staff=True)


def _make_product():
    global _seq
    _seq += 1
    return ChallengeProduct.objects.create(
        name=f"A2-{_seq}", account_size=Decimal("10000.00"), price_usd=Decimal("99.00"), is_active=True,
        p1_profit_target_pct=Decimal("8.00"), p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"), p1_min_trading_days=0, p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"), p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"), p2_min_trading_days=0, p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"), max_open_positions=5, profit_split_pct=Decimal("80.00"),
    )


def _make_funded_enrollment(user):
    enrollment = ChallengeEnrollment.objects.create(user=user, product=_make_product())
    activate_challenge_enrollment(enrollment); enrollment.refresh_from_db()
    advance_to_phase2(enrollment); enrollment.refresh_from_db()
    advance_to_funded(enrollment); enrollment.refresh_from_db()
    return enrollment


def _setup_funded_internal(user, *, profit_usd=Decimal("1000.00")):
    enrollment = _make_funded_enrollment(user)
    funded_account = enrollment.funded_account
    funded_config = FundedConfig.objects.get(enrollment=enrollment)

    initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
    pre_debit = initial + profit_usd
    trader_cut = (profit_usd * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    post_debit = pre_debit - trader_cut
    funded_account.balance = post_debit
    funded_account.equity = post_debit
    funded_account.save(update_fields=["balance", "equity"])

    ledger = LedgerEntry.objects.create(
        account=funded_account, event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        amount=-trader_cut, balance_after=post_debit,
    )
    wr = WithdrawalRequest.objects.create(
        user=user, amount_usd=trader_cut, crypto_currency="btc",
        wallet_address="bc1qtestfundedinternal0000000000000000000",
        status=WithdrawalRequest.STATUS_APPROVED, debit_tx=None,
    )
    fpr = FundedPayoutRequest.objects.create(
        user=user, enrollment=enrollment, funded_account=funded_account, funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_INTERNAL, cycle_profit=profit_usd, trader_cut=trader_cut,
        broker_cut=profit_usd - trader_cut, profit_split_pct=Decimal("80.00"),
        balance_snapshot=pre_debit, initial_balance_snapshot=initial,
        crypto_currency="btc", wallet_address=wr.wallet_address,
        status=FundedPayoutRequest.ST_APPROVED, withdrawal_request=wr, ledger_entry=ledger,
    )
    return fpr, wr, funded_account


# ─────────────────────────────────────────────────────────────────────────────
# AttemptMatch transitions
# ─────────────────────────────────────────────────────────────────────────────

class AttemptWebhookTransitionsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.wallet.refresh_from_db()  # _make_wr debits via a separate query — sync the in-memory object

    def test_processing_to_completed_no_refund(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-1")
        target = apply_provider_webhook_event(_event(reference="ref-1", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        self.assertIsInstance(target, AttemptMatch)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_COMPLETED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("800"))  # unchanged by this event

    def test_processing_to_failed_refunds_exactly_once(self):
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-2")
        before = self.wallet.available_balance
        apply_provider_webhook_event(_event(reference="ref-2", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_FAILED)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )

    def test_unknown_to_completed(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="ref-3")
        apply_provider_webhook_event(_event(reference="ref-3", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)

    def test_unknown_to_failed_refunds_exactly_once(self):
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN, provider_reference="ref-4")
        before = self.wallet.available_balance
        apply_provider_webhook_event(_event(reference="ref-4", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)

    def test_duplicate_completed_is_noop(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-5")
        apply_provider_webhook_event(_event(reference="ref-5", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        apply_provider_webhook_event(_event(reference="ref-5", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)

    def test_duplicate_failed_does_not_double_refund(self):
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="ref-6")
        before = self.wallet.available_balance
        apply_provider_webhook_event(_event(reference="ref-6", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        apply_provider_webhook_event(_event(reference="ref-6", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)  # only once
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )

    def test_old_attempt_correlation_acts_on_exact_attempt_not_latest(self):
        """attempt#1 is terminal (FAILED) with ref-old; a new attempt#2
        exists with a different ref. A stale webhook for ref-old must
        resolve to attempt#1 (and be a no-op, terminal) — never touch
        attempt#2."""
        attempt1 = _make_attempt(self.wr, status=PayoutAttempt.STATUS_FAILED, attempt_number=1, provider_reference="ref-old")
        # simulate wr back to processing with a fresh attempt#2 (as if reconciled+retried)
        WithdrawalRequest.objects.filter(pk=self.wr.pk).update(status=WithdrawalRequest.STATUS_PROCESSING)
        attempt2 = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING, attempt_number=2, provider_reference="ref-new")

        target = apply_provider_webhook_event(_event(reference="ref-old", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        self.assertIsInstance(target, AttemptMatch)
        self.assertEqual(target.attempt.pk, attempt1.pk)

        attempt1.refresh_from_db(); attempt2.refresh_from_db()
        self.assertEqual(attempt1.status, PayoutAttempt.STATUS_FAILED, "must stay untouched — terminal, and not the target anyway")
        self.assertEqual(attempt2.status, PayoutAttempt.STATUS_PROCESSING, "must never be touched by a webhook for a different attempt")


# ─────────────────────────────────────────────────────────────────────────────
# Correlation cascade — legacy / FUNDED_INTERNAL / orphan
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationCascadeTests(TestCase):
    def test_legacy_withdrawal_no_attempt_resolves_via_legacy_match(self):
        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = WithdrawalRequest.objects.create(
            user=user, amount_usd=Decimal("50"), crypto_currency="btc",
            wallet_address="bc1qtest", status=WithdrawalRequest.STATUS_PROCESSING,
            np_payout_id="legacy-ref-1", debit_tx=None,
        )
        target = resolve_payout_target(_event(reference="legacy-ref-1"))
        self.assertIsInstance(target, LegacyWithdrawalMatch)
        self.assertEqual(target.withdrawal_request.pk, wr.pk)

    def test_funded_internal_resolves_via_funded_match(self):
        user = make_user()
        make_wallet(user)
        fpr, wr, _acc = _setup_funded_internal(user)
        WithdrawalRequest.objects.filter(pk=wr.pk).update(np_payout_id="fpr-ref-1")
        target = resolve_payout_target(_event(reference="fpr-ref-1"))
        self.assertIsInstance(target, FundedInternalMatch)
        self.assertEqual(target.withdrawal_request.pk, wr.pk)

    def test_apply_funded_internal_delegates_to_untouched_handler(self):
        """Regression: the new correlation path must produce the exact
        same effect as calling handle_internal_payout_webhook() directly
        (test_funded_payout_internal_approval.py already proves that
        function's own correctness — this proves OUR dispatch reaches it)."""
        user = make_user()
        make_wallet(user)
        fpr, wr, funded_account = _setup_funded_internal(user)
        WithdrawalRequest.objects.filter(pk=wr.pk).update(np_payout_id="fpr-ref-2")
        post_debit_balance = Decimal(str(funded_account.balance))

        target = apply_provider_webhook_event(_event(reference="fpr-ref-2", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        self.assertIsInstance(target, FundedInternalMatch)

        fpr.refresh_from_db()
        self.assertEqual(fpr.status, FundedPayoutRequest.ST_COMPLETED)
        funded_account.refresh_from_db()
        self.assertEqual(funded_account.initial_balance, post_debit_balance)
        # no wallet touch — FUNDED_INTERNAL never uses the user's Wallet
        self.assertFalse(WalletTransaction.objects.filter(wallet__user=user).exists())

    def test_orphan_when_nothing_resolves(self):
        target = resolve_payout_target(_event(reference="totally-unknown-ref"))
        self.assertIsInstance(target, Orphan)

    def test_empty_refs_never_query_db(self):
        with CaptureQueriesContext(connection) as ctx:
            target = resolve_payout_target(_event(reference="", batch=""))
        self.assertIsInstance(target, Orphan)
        self.assertEqual(len(ctx.captured_queries), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Ambiguous / DataIntegrityViolation
# ─────────────────────────────────────────────────────────────────────────────

class AmbiguousAndIntegrityTests(TestCase):
    def setUp(self):
        self.user_a = make_user()
        self.user_b = make_user()
        self.wallet_a = make_wallet(self.user_a, initial_balance=Decimal("1000"))
        self.wallet_b = make_wallet(self.user_b, initial_balance=Decimal("1000"))
        self.wr_a = _make_wr(self.user_a, amount="100.00")
        self.wr_b = _make_wr(self.user_b, amount="150.00")
        # _make_wr debits via a separate query/object — resync the explicit
        # references (self.user_a.wallet would otherwise return Django's
        # cached, stale related-object instead of a fresh read).
        self.wallet_a.refresh_from_db()
        self.wallet_b.refresh_from_db()

    def test_duplicate_provider_reference_across_attempts_is_ambiguous(self):
        _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_FAILED, provider_reference="dup-ref")
        _make_attempt(self.wr_b, status=PayoutAttempt.STATUS_FAILED, provider_reference="dup-ref")
        target = resolve_payout_target(_event(reference="dup-ref"))
        self.assertIsInstance(target, Ambiguous)

    def test_ambiguous_never_mutates_state(self):
        a1 = _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_FAILED, provider_reference="dup-ref-2")
        _make_attempt(self.wr_b, status=PayoutAttempt.STATUS_FAILED, provider_reference="dup-ref-2")
        wallet_a_before = self.wallet_a.available_balance
        apply_provider_webhook_event(_event(reference="dup-ref-2", status=PayoutAttempt.STATUS_COMPLETED, raw="FINISHED"))
        a1.refresh_from_db()
        self.assertEqual(a1.status, PayoutAttempt.STATUS_FAILED)  # untouched
        self.wallet_a.refresh_from_db()
        self.assertEqual(self.wallet_a.available_balance, wallet_a_before)

    def test_mirror_pointing_to_different_wr_is_data_integrity_violation(self):
        _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="mismatched-ref")
        # Contrived misconfiguration: WR-B's legacy mirror claims the same reference.
        WithdrawalRequest.objects.filter(pk=self.wr_b.pk).update(np_payout_id="mismatched-ref")
        target = resolve_payout_target(_event(reference="mismatched-ref"))
        self.assertIsInstance(target, DataIntegrityViolation)

    def test_data_integrity_violation_never_mutates_state(self):
        attempt = _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="mismatched-ref-2")
        WithdrawalRequest.objects.filter(pk=self.wr_b.pk).update(np_payout_id="mismatched-ref-2")
        apply_provider_webhook_event(_event(reference="mismatched-ref-2", status=PayoutAttempt.STATUS_FAILED, raw="FAILED"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.wallet_a.refresh_from_db()
        self.assertEqual(self.wallet_a.available_balance, Decimal("900"))  # unchanged (1000 - 100 debit only)

    def test_matching_mirror_is_not_flagged(self):
        """The dual-write mirror agreeing with its own PayoutAttempt must
        NOT be treated as a second candidate or a violation."""
        attempt = _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="agree-ref")
        WithdrawalRequest.objects.filter(pk=self.wr_a.pk).update(np_payout_id="agree-ref")
        target = resolve_payout_target(_event(reference="agree-ref"))
        self.assertIsInstance(target, AttemptMatch)
        self.assertEqual(target.attempt.pk, attempt.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Batch precedence + batch-specific ambiguity
# ─────────────────────────────────────────────────────────────────────────────

class BatchPrecedenceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="100.00")

    @patch("simulator.payout_orchestrator.time.sleep")
    def test_provider_reference_never_falls_back_to_batch(self, mock_sleep):
        """A PayoutAttempt exists ONLY reachable by batch_id; the event
        also carries a provider_reference that matches nothing. Per the
        Design Lock, this must NOT fall back to batch — it must end in
        Orphan (after the bounded retry), never AttemptMatch."""
        _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING,
                       provider_reference="", provider_batch_id="batch-would-match")
        target = resolve_payout_target(_event(reference="ref-matches-nothing", batch="batch-would-match"))
        self.assertIsInstance(target, Orphan)

    def test_batch_used_only_when_reference_absent(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_PROCESSING,
                                 provider_batch_id="batch-only")
        target = resolve_payout_target(_event(reference="", batch="batch-only"))
        self.assertIsInstance(target, AttemptMatch)
        self.assertEqual(target.attempt.pk, attempt.pk)


class BatchAmbiguousTests(TestCase):
    def setUp(self):
        self.user_a = make_user()
        self.user_b = make_user()
        make_wallet(self.user_a, initial_balance=Decimal("1000"))
        make_wallet(self.user_b, initial_balance=Decimal("1000"))
        self.wr_a = _make_wr(self.user_a, amount="100.00")
        self.wr_b = _make_wr(self.user_b, amount="150.00")

    def test_multiple_payout_attempts_same_batch_is_ambiguous(self):
        _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_FAILED, provider_batch_id="shared-batch")
        _make_attempt(self.wr_b, status=PayoutAttempt.STATUS_FAILED, provider_batch_id="shared-batch")
        target = resolve_payout_target(_event(reference="", batch="shared-batch"))
        self.assertIsInstance(target, Ambiguous)

    def test_multiple_withdrawal_requests_same_batch_is_ambiguous(self):
        WithdrawalRequest.objects.filter(pk=self.wr_a.pk).update(np_batch_id="shared-batch-2")
        WithdrawalRequest.objects.filter(pk=self.wr_b.pk).update(np_batch_id="shared-batch-2")
        target = resolve_payout_target(_event(reference="", batch="shared-batch-2"))
        self.assertIsInstance(target, Ambiguous)

    def test_batch_cross_identity_violation(self):
        _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_PROCESSING, provider_batch_id="batch-x")
        WithdrawalRequest.objects.filter(pk=self.wr_b.pk).update(np_batch_id="batch-x")
        target = resolve_payout_target(_event(reference="", batch="batch-x"))
        self.assertIsInstance(target, DataIntegrityViolation)

    def test_never_selects_via_first_when_ambiguous(self):
        """Explicit anti-regression: neither wr_a nor wr_b's PayoutAttempt
        may be silently chosen — the result must be Ambiguous, not
        AttemptMatch(either one)."""
        a1 = _make_attempt(self.wr_a, status=PayoutAttempt.STATUS_FAILED, provider_batch_id="never-pick")
        a2 = _make_attempt(self.wr_b, status=PayoutAttempt.STATUS_FAILED, provider_batch_id="never-pick")
        target = resolve_payout_target(_event(reference="", batch="never-pick"))
        self.assertNotIsInstance(target, AttemptMatch)
        self.assertIsInstance(target, Ambiguous)


# ─────────────────────────────────────────────────────────────────────────────
# Early webhook — exact retry/sleep count
# ─────────────────────────────────────────────────────────────────────────────

class EarlyWebhookRetryTests(TestCase):
    @patch("simulator.payout_orchestrator.time.sleep")
    def test_exactly_two_sleeps_of_015_when_never_resolves(self, mock_sleep):
        target = resolve_payout_target(_event(reference="never-appears"))
        self.assertIsInstance(target, Orphan)
        self.assertEqual(mock_sleep.call_count, 2)
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args[0], 0.15)

    @patch("simulator.payout_orchestrator.time.sleep")
    def test_exactly_three_correlation_query_rounds(self, mock_sleep):
        with CaptureQueriesContext(connection) as ctx:
            resolve_payout_target(_event(reference="never-appears-2"))
        # Each round issues exactly 2 queries (PayoutAttempt filter + WithdrawalRequest filter).
        self.assertEqual(len(ctx.captured_queries), 6, ctx.captured_queries)

    @patch("simulator.payout_orchestrator.time.sleep")
    def test_resolves_within_retry_window_without_exhausting_it(self, mock_sleep):
        """If the row appears on, say, the 2nd query round (simulating
        TXN2 committing mid-retry), the loop must stop early — sleep
        called at most once, not the full budget."""
        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_wr(user, amount="10.00")

        calls = {"n": 0}
        real_filter = PayoutAttempt.objects.filter

        def _delayed_visible(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2 and kwargs.get("provider_reference") == "shows-up-late":
                _make_attempt(wr, status=PayoutAttempt.STATUS_PROCESSING, provider_reference="shows-up-late")
            return real_filter(*args, **kwargs)

        with patch("simulator.models.PayoutAttempt.objects.filter", side_effect=_delayed_visible):
            target = resolve_payout_target(_event(reference="shows-up-late"))
        self.assertIsInstance(target, AttemptMatch)
        self.assertLessEqual(mock_sleep.call_count, 1)
