"""
simulator/tests/test_funded_payout_internal_approval.py — Bloque H.3

Audits approve_internal_payout() and handle_internal_payout_webhook()
in simulator/funded_payouts.py (FUNDED_INTERNAL flow).

No HTTP — all functions called directly. NowPayments is always mocked.

Coverage:
  - Phase 1 DB writes (debit, ledger, WR, FPR links)
  - Phase 2 NP success (WR → processing, FPR → processing)
  - Phase 2 NP failure compensating transaction (reversal, EV_ADJUST)
  - Webhook COMPLETED (cycle reset, idempotency)
  - Webhook FAILED (reversal, idempotency)
  - Guard: plain WR has no linked FPR
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.funded_payouts import (
    FundedPayoutAlreadyProcessed,
    InsufficientFundedBalance,
    approve_internal_payout,
    handle_internal_payout_webhook,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    TradingAccount,
    WithdrawalRequest,
    Wallet,
)
from simulator.wallet_ledger import get_or_create_wallet

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# NP mock constants
# ─────────────────────────────────────────────────────────────────────────────

_NP_ESTIMATE = "simulator.funded_payouts._np.estimate_price"
_NP_PAYOUT   = "simulator.funded_payouts._np.create_payout"

_NP_ESTIMATE_RET = Decimal("0.000125")
_NP_PAYOUT_RET   = {
    "id": "batch-h3test",
    "status": "CREATED",
    "withdrawals": [{"id": "wd-h3test", "status": "CREATED"}],
}

_seq = 0

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(role="trader"):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h3_{role}_{_seq}",
        email=f"h3_{role}_{_seq}@example.com",
        password="testpass",
    )


def _make_admin():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h3_admin_{_seq}",
        email=f"h3_admin_{_seq}@example.com",
        password="adminpass",
        is_staff=True,
    )


def _make_product():
    global _seq
    return ChallengeProduct.objects.create(
        name=f"H3-Test-{_seq}",
        account_size=Decimal("10000.00"),
        price_usd=Decimal("99.00"),
        is_active=True,
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=0,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=0,
        p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"),
        max_open_positions=5,
        profit_split_pct=Decimal("80.00"),
    )


def _make_funded_enrollment(user):
    product    = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _make_internal_pending_fpr(
    user,
    enrollment,
    funded_account,
    funded_config,
    *,
    profit_usd=Decimal("1000.00"),
):
    """Create FPR in ST_PENDING with funded_type=FUNDED_INTERNAL and crypto fields."""
    initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
    funded_account.balance = initial + profit_usd
    funded_account.equity  = funded_account.balance
    funded_account.save(update_fields=["balance", "equity"])

    cycle_profit = profit_usd
    trader_cut   = (cycle_profit * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut   = cycle_profit - trader_cut

    return FundedPayoutRequest.objects.create(
        user=user,
        enrollment=enrollment,
        funded_account=funded_account,
        funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_INTERNAL,
        cycle_profit=cycle_profit,
        trader_cut=trader_cut,
        broker_cut=broker_cut,
        profit_split_pct=Decimal("80.00"),
        balance_snapshot=funded_account.balance,
        initial_balance_snapshot=initial,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=FundedPayoutRequest.ST_PENDING,
    )


def _setup_approved_state(
    user,
    enrollment,
    funded_account,
    funded_config,
    *,
    profit_usd=Decimal("1000.00"),
):
    """
    Simulate Phase 1 result of approve_internal_payout without calling NP.
    Returns (fpr, wr) with:
      - funded_account.balance already debited
      - funded_account.initial_balance unchanged (not yet reset)
      - FPR.status = ST_APPROVED
      - WR.status = STATUS_APPROVED, WR.debit_tx = None
      - LedgerEntry(EV_FUNDED_PAYOUT) linked on FPR
    """
    initial       = Decimal(str(funded_account.initial_balance or funded_account.balance))
    pre_debit     = initial + profit_usd
    cycle_profit  = profit_usd
    trader_cut    = (cycle_profit * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut    = cycle_profit - trader_cut
    post_debit    = pre_debit - trader_cut

    # Set funded account to post-debit state (initial_balance stays at initial)
    funded_account.balance = post_debit
    funded_account.equity  = post_debit
    funded_account.save(update_fields=["balance", "equity"])

    ledger = LedgerEntry.objects.create(
        account=funded_account,
        event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        amount=-trader_cut,
        balance_after=post_debit,
    )

    wr = WithdrawalRequest.objects.create(
        user=user,
        amount_usd=trader_cut,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=WithdrawalRequest.STATUS_APPROVED,
        debit_tx=None,
    )

    fpr = FundedPayoutRequest.objects.create(
        user=user,
        enrollment=enrollment,
        funded_account=funded_account,
        funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_INTERNAL,
        cycle_profit=cycle_profit,
        trader_cut=trader_cut,
        broker_cut=broker_cut,
        profit_split_pct=Decimal("80.00"),
        balance_snapshot=pre_debit,
        initial_balance_snapshot=initial,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=FundedPayoutRequest.ST_APPROVED,
        withdrawal_request=wr,
        ledger_entry=ledger,
    )

    return fpr, wr


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 DB operations (NP mocked to succeed for full-flow tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutDB(TestCase):
    """Phase 1 DB writes — debit, ledger, WR, FPR links, guard errors."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    # ── Funded account debited, wallet unchanged ──────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_debits_funded_account_not_wallet(self, _est, _pay):
        wallet, _ = get_or_create_wallet(self.user)
        wallet_balance_before = Decimal(str(wallet.available_balance))
        account_balance_before = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))

        approve_internal_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        self.assertEqual(
            self.funded_account.balance,
            account_balance_before - trader_cut,
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, wallet_balance_before)

    # ── LedgerEntry EV_FUNDED_PAYOUT created ─────────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_creates_ev_funded_payout_ledger(self, _est, _pay):
        account_balance_before = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))

        approve_internal_payout(self.fpr, self.admin)

        ledger = LedgerEntry.objects.get(
            account=self.funded_account,
            event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        )
        self.assertEqual(ledger.amount,        -trader_cut)
        self.assertEqual(ledger.balance_after,  account_balance_before - trader_cut)

    # ── WithdrawalRequest created and linked ──────────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_creates_withdrawal_request_linked(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.withdrawal_request_id)

    # ── WR debit_tx is None (no wallet debit) ────────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_wr_debit_tx_is_none(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.withdrawal_request.debit_tx)

    # ── No cycle reset on approval ────────────────────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_no_cycle_reset_on_approval(self, _est, _pay):
        original_initial = Decimal(str(self.funded_account.initial_balance))

        approve_internal_payout(self.fpr, self.admin)

        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.cycle_reset_at)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, original_initial)

    # ── reviewed_by / reviewed_at set on FPR ─────────────────────────────────

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_sets_review_fields(self, _est, _pay):
        before = now()
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.reviewed_by_id, self.admin.pk)
        self.assertIsNotNone(self.fpr.reviewed_at)
        self.assertGreaterEqual(self.fpr.reviewed_at, before)

    # ── Guard: non-pending FPR raises FundedPayoutAlreadyProcessed ───────────

    def test_rejects_non_pending(self):
        FundedPayoutRequest.objects.filter(pk=self.fpr.pk).update(
            status=FundedPayoutRequest.ST_APPROVED
        )
        self.fpr.refresh_from_db()
        with self.assertRaises(FundedPayoutAlreadyProcessed):
            approve_internal_payout(self.fpr, self.admin)

    # ── Guard: insufficient balance raises InsufficientFundedBalance ──────────

    def test_revalidates_balance(self):
        self.funded_account.balance = Decimal("0.01")
        self.funded_account.equity  = Decimal("0.01")
        self.funded_account.save(update_fields=["balance", "equity"])
        with self.assertRaises(InsufficientFundedBalance):
            approve_internal_payout(self.fpr, self.admin)

    # ── Guard: missing crypto fields raises ValueError ────────────────────────

    def test_requires_crypto_fields(self):
        FundedPayoutRequest.objects.filter(pk=self.fpr.pk).update(
            crypto_currency="", wallet_address=""
        )
        self.fpr.refresh_from_db()
        with self.assertRaises(ValueError):
            approve_internal_payout(self.fpr, self.admin)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: NP success — WR and FPR move to processing
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutNPSuccess(TestCase):

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_success_moves_wr_to_processing(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        wr = self.fpr.withdrawal_request
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(wr.np_batch_id,  "batch-h3test")
        self.assertEqual(wr.np_payout_id, "wd-h3test")

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_success_moves_fpr_to_processing(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_PROCESSING)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: NP failure — compensating transaction
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutNPFailure(TestCase):

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_reverses_funded_account(self, _est, _pay):
        balance_before = Decimal(str(self.funded_account.balance))

        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before)
        self.assertEqual(self.funded_account.equity,  balance_before)

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_creates_ev_adjust(self, _est, _pay):
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)

        self.assertTrue(
            LedgerEntry.objects.filter(
                account=self.funded_account,
                event_type=LedgerEntry.EV_ADJUST,
            ).exists()
        )

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_no_cycle_reset(self, _est, _pay):
        original_initial = Decimal(str(self.funded_account.initial_balance))

        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)

        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.cycle_reset_at)
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, original_initial)

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_marks_fpr_failed(self, _est, _pay):
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_marks_wr_failed(self, _est, _pay):
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.withdrawal_request.status, WithdrawalRequest.STATUS_FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook COMPLETED
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookCompleted(TestCase):
    """handle_internal_payout_webhook with STATUS_COMPLETED."""

    def setUp(self):
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr, self.wr   = _setup_approved_state(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def test_webhook_completed_resets_cycle(self):
        post_debit_balance = Decimal(str(self.funded_account.balance))
        before             = now()

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, post_debit_balance)

        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.cycle_reset_at)
        self.assertGreaterEqual(self.fpr.cycle_reset_at, before)

    def test_webhook_completed_sets_fpr_completed(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)

    def test_webhook_completed_idempotent(self):
        """Second COMPLETED webhook is a no-op — no double reset."""
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        first_cycle_reset_at  = self.fpr.cycle_reset_at
        post_debit_initial    = self.funded_account.balance  # captured before second call

        # Second call — must be idempotent
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )

        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status,        FundedPayoutRequest.ST_COMPLETED)
        self.assertEqual(self.fpr.cycle_reset_at, first_cycle_reset_at)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, post_debit_initial)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook FAILED
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookFailed(TestCase):
    """handle_internal_payout_webhook with STATUS_FAILED."""

    def setUp(self):
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr, self.wr   = _setup_approved_state(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def test_webhook_failed_reverses_funded_account(self):
        trader_cut     = Decimal(str(self.fpr.trader_cut))
        balance_before = Decimal(str(self.funded_account.balance))  # post-debit state

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before + trader_cut)
        self.assertEqual(self.funded_account.equity,  balance_before + trader_cut)

    def test_webhook_failed_creates_ev_adjust(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.assertTrue(
            LedgerEntry.objects.filter(
                account=self.funded_account,
                event_type=LedgerEntry.EV_ADJUST,
            ).exists()
        )

    def test_webhook_failed_no_cycle_reset(self):
        original_initial = Decimal(str(self.funded_account.initial_balance))

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )

        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.cycle_reset_at)
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, original_initial)

    def test_webhook_failed_marks_fpr_failed(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)

    def test_webhook_failed_idempotent(self):
        """Second FAILED webhook must not double-reverse the funded account."""
        trader_cut     = Decimal(str(self.fpr.trader_cut))
        balance_before = Decimal(str(self.funded_account.balance))  # post-debit

        # First call — reverses debit
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.funded_account.refresh_from_db()
        balance_after_first = self.funded_account.balance
        self.assertEqual(balance_after_first, balance_before + trader_cut)

        # Second call — must be a no-op (FPR is already ST_FAILED)
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_after_first)


# ─────────────────────────────────────────────────────────────────────────────
# Regular WithdrawalRequest not affected by funded logic
# ─────────────────────────────────────────────────────────────────────────────

class TestRegularWRNotAffected(TestCase):
    """Plain WR (no linked FPR) has no funded_payout_internal relation."""

    def setUp(self):
        self.user = _make_user()

    def test_regular_wr_has_no_funded_payout_internal(self):
        wallet, _ = get_or_create_wallet(self.user)
        wr = WithdrawalRequest.objects.create(
            user=self.user,
            amount_usd=Decimal("50.00"),
            crypto_currency="btc",
            wallet_address="bc1qplainaddress",
            status=WithdrawalRequest.STATUS_PENDING,
        )
        with self.assertRaises(FundedPayoutRequest.DoesNotExist):
            _ = wr.funded_payout_internal
