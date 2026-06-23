"""
simulator/tests/test_funded_payout_sim_approval.py — Bloque H.2

Audits _approve_sim_payout() in simulator/admin.py (FUNDED_SIM flow).
No HTTP — calls the helper directly so each test is fast and deterministic.

Scope: atomic approval, ledger debit, wallet credit, cycle reset,
       idempotency / exception guards, rollback on downstream failure.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from simulator.admin import (
    FundedPayoutAlreadyProcessed,
    InsufficientFundedBalance,
    _approve_sim_payout,
)
from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    TradingAccount,
    WalletTransaction,
)

User = get_user_model()

_seq = 0

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(role="trader"):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h2_{role}_{_seq}",
        email=f"h2_{role}_{_seq}@example.com",
        password="testpass",
    )


def _make_admin():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h2_admin_{_seq}",
        email=f"h2_admin_{_seq}@example.com",
        password="adminpass",
        is_staff=True,
    )


def _make_product():
    global _seq
    return ChallengeProduct.objects.create(
        name=f"H2-Test-{_seq}",
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
    """Force-advance enrollment through both phases to ST_FUNDED."""
    product = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _make_pending_fpr(
    user,
    enrollment,
    funded_account,
    funded_config,
    *,
    profit_usd=Decimal("1000.00"),
    split_pct=Decimal("80.00"),
):
    """
    Set funded_account.balance to produce the given cycle_profit,
    then create a FundedPayoutRequest in ST_PENDING with frozen monetary snapshot.
    """
    initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
    funded_account.balance = initial + profit_usd
    funded_account.equity  = funded_account.balance
    funded_account.save(update_fields=["balance", "equity"])

    cycle_profit = profit_usd
    trader_cut   = (cycle_profit * split_pct / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut   = (cycle_profit - trader_cut).quantize(Decimal("0.01"))

    return FundedPayoutRequest.objects.create(
        user=user,
        enrollment=enrollment,
        funded_account=funded_account,
        funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_SIM,
        cycle_profit=cycle_profit,
        trader_cut=trader_cut,
        broker_cut=broker_cut,
        profit_split_pct=split_pct,
        balance_snapshot=funded_account.balance,
        initial_balance_snapshot=initial,
        status=FundedPayoutRequest.ST_PENDING,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test suite
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveSimpayout(TestCase):
    """
    H.2: _approve_sim_payout(fpr, admin_user)

    Each test sets up a fresh enrollment so there is no shared state.
    """

    def setUp(self):
        self.admin     = _make_admin()
        self.user      = _make_user()
        self.enrollment = _make_funded_enrollment(self.user)
        self.enrollment.refresh_from_db()
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_pending_fpr(
            self.user,
            self.enrollment,
            self.funded_account,
            self.funded_config,
        )

    # ── 1. Happy path: status transitions to completed ───────────────────────

    def test_fpr_status_completed(self):
        _approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)

    # ── 2. Funded account balance is debited by trader_cut ───────────────────

    def test_funded_account_balance_decremented(self):
        initial_balance = Decimal(str(self.funded_account.balance))
        trader_cut      = Decimal(str(self.fpr.trader_cut))

        _approve_sim_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        expected = initial_balance - trader_cut
        self.assertEqual(self.funded_account.balance, expected)
        self.assertEqual(self.funded_account.equity,  expected)

    # ── 3. LedgerEntry created with correct fields ───────────────────────────

    def test_ledger_entry_created(self):
        initial_balance = Decimal(str(self.funded_account.balance))
        trader_cut      = Decimal(str(self.fpr.trader_cut))

        _approve_sim_payout(self.fpr, self.admin)

        ledger = LedgerEntry.objects.get(
            account=self.funded_account,
            event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        )
        self.assertEqual(ledger.amount,        -trader_cut)
        self.assertEqual(ledger.balance_after,  initial_balance - trader_cut)

    # ── 4. WalletTransaction created and credited to user ────────────────────

    def test_wallet_credited(self):
        trader_cut = Decimal(str(self.fpr.trader_cut))

        _approve_sim_payout(self.fpr, self.admin)

        tx = WalletTransaction.objects.get(
            wallet__user=self.user,
            tx_type=WalletTransaction.TX_FUNDED_PAYOUT,
        )
        self.assertEqual(tx.amount, trader_cut)

    # ── 5. FPR references (ledger_entry, wallet_credit_tx) are saved ─────────

    def test_fpr_references_saved(self):
        _approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.ledger_entry_id)
        self.assertIsNotNone(self.fpr.wallet_credit_tx_id)

    # ── 6. reviewed_by and reviewed_at are set ───────────────────────────────

    def test_reviewed_by_and_at_set(self):
        before = now()
        _approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.reviewed_by_id, self.admin.pk)
        self.assertIsNotNone(self.fpr.reviewed_at)
        self.assertGreaterEqual(self.fpr.reviewed_at, before)

    # ── 7. cycle_reset_at is set ─────────────────────────────────────────────

    def test_cycle_reset_at_set(self):
        before = now()
        _approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.cycle_reset_at)
        self.assertGreaterEqual(self.fpr.cycle_reset_at, before)

    # ── 8. initial_balance reset to post-debit balance ───────────────────────

    def test_initial_balance_reset(self):
        initial_balance = Decimal(str(self.funded_account.balance))
        trader_cut      = Decimal(str(self.fpr.trader_cut))
        expected_new_initial = initial_balance - trader_cut

        _approve_sim_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, expected_new_initial)

    # ── 9. ledger balance_after matches new funded account balance ────────────

    def test_ledger_balance_after_matches_account_balance(self):
        _approve_sim_payout(self.fpr, self.admin)
        self.funded_account.refresh_from_db()
        ledger = LedgerEntry.objects.get(account=self.funded_account,
                                         event_type=LedgerEntry.EV_FUNDED_PAYOUT)
        self.assertEqual(ledger.balance_after, self.funded_account.balance)

    # ── 10. Already-processed FPR raises FundedPayoutAlreadyProcessed ─────────

    def test_already_processed_raises(self):
        _approve_sim_payout(self.fpr, self.admin)  # first approval succeeds
        self.fpr.refresh_from_db()

        with self.assertRaises(FundedPayoutAlreadyProcessed):
            _approve_sim_payout(self.fpr, self.admin)  # second must fail

    # ── 11. Insufficient balance raises InsufficientFundedBalance ─────────────

    def test_insufficient_balance_raises(self):
        # Force account balance below trader_cut
        self.funded_account.balance = Decimal("0.01")
        self.funded_account.equity  = Decimal("0.01")
        self.funded_account.save(update_fields=["balance", "equity"])

        with self.assertRaises(InsufficientFundedBalance):
            _approve_sim_payout(self.fpr, self.admin)

        # FPR must still be pending — nothing committed
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_PENDING)

    # ── 12. Wrong funded_type raises ValueError ───────────────────────────────

    def test_wrong_funded_type_raises(self):
        # Mutate funded_type snapshot to FUNDED_INTERNAL without touching funded_config
        FundedPayoutRequest.objects.filter(pk=self.fpr.pk).update(
            funded_type=FundedConfig.FUNDED_INTERNAL
        )
        self.fpr.refresh_from_db()

        with self.assertRaises(ValueError):
            _approve_sim_payout(self.fpr, self.admin)

        # FPR remains pending
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_PENDING)

    # ── 13. Rollback: if credit_wallet raises, no DB writes survive ───────────

    def test_atomic_rollback_on_credit_wallet_failure(self):
        initial_balance = Decimal(str(self.funded_account.balance))

        with patch("simulator.admin.credit_wallet", side_effect=RuntimeError("mock failure")):
            with self.assertRaises(RuntimeError):
                _approve_sim_payout(self.fpr, self.admin)

        # FPR still pending
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_PENDING)

        # Funded account balance unchanged
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, initial_balance)

        # No LedgerEntry was persisted
        self.assertFalse(
            LedgerEntry.objects.filter(
                account=self.funded_account,
                event_type=LedgerEntry.EV_FUNDED_PAYOUT,
            ).exists()
        )

    # ── 14. Second concurrent call raises after first commits ─────────────────

    def test_second_approval_raises_already_processed(self):
        """
        Simulates two admin users approving the same FPR sequentially.
        The first succeeds; the second must raise FundedPayoutAlreadyProcessed
        because select_for_update sees status='completed' after the first commit.
        """
        admin2 = _make_admin()

        _approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)

        with self.assertRaises(FundedPayoutAlreadyProcessed):
            _approve_sim_payout(self.fpr, admin2)
