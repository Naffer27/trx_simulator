"""
simulator/tests/test_challenge_engine.py

Tests for simulator/challenge_engine.py.

Structure:
  Block 1 — TestActivateChallengeEnrollment   (activate_challenge_enrollment)
  Block 2 — TestEvaluatePhase                 (evaluate_phase)
  Block 3 — TestAdvanceToPhase2               (advance_to_phase2)
  Block 4 — TestAdvanceToFunded               (advance_to_funded)
  Block 5 — TestEvaluateEnrollmentNow         (evaluate_enrollment_now)
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.challenge_engine import (
    IN_PROGRESS, PASSED, FAILED,
    EvalResult,
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
    evaluate_enrollment_now,
    evaluate_phase,
)
from simulator.models import (
    ChallengeEnrollment, FundedConfig, LedgerEntry, RiskRule, Trade, TradingAccount,
)
from simulator.tests.factories import (
    make_challenge_enrollment,
    make_challenge_product,
    make_ledger_entry,
    make_user,
)


# ---------------------------------------------------------------------------
# Block 1: activate_challenge_enrollment
# ---------------------------------------------------------------------------

class TestActivateChallengeEnrollment(TestCase):
    """activate_challenge_enrollment creates Phase 1 account and RiskRule."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(
            tier="10K",
            account_size=Decimal("10000.00"),
            p1_profit_target_pct=Decimal("8.00"),
            p1_max_drawdown_pct=Decimal("10.00"),
            p1_max_daily_loss_pct=Decimal("5.00"),
            p1_min_trading_days=5,
            p1_max_duration_days=30,
            max_lot_size=Decimal("5.00"),
            max_open_positions=30,
        )
        self.enrollment = make_challenge_enrollment(
            user=self.user,
            product=self.product,
            status=ChallengeEnrollment.ST_PHASE_1,
        )

    # ── Account creation ──────────────────────────────────────────────────

    def test_returns_trading_account(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertIsInstance(account, TradingAccount)

    def test_account_type_is_challenge(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.account_type, "CHALLENGE")

    def test_account_phase_is_fase1(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.phase, "Fase 1")

    def test_account_status_is_activo(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.status, TradingAccount.STATUS_ACTIVE)

    def test_account_user_matches_enrollment(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.user_id, self.user.pk)

    # ── Balance fields ────────────────────────────────────────────────────

    def test_balance_matches_account_size(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.balance, Decimal("10000.00"))

    def test_initial_balance_matches_account_size(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.initial_balance, Decimal("10000.00"))

    def test_peak_balance_matches_account_size(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.peak_balance, Decimal("10000.00"))

    # ── Profit target ────────────────────────────────────────────────────

    def test_profit_target_is_8_pct_of_10k(self):
        account = activate_challenge_enrollment(self.enrollment)
        # 8% of 10000 = 800
        self.assertEqual(account.profit_target, Decimal("800.00"))

    def test_max_drawdown_is_10_pct_of_10k(self):
        account = activate_challenge_enrollment(self.enrollment)
        # 10% of 10000 = 1000
        self.assertEqual(account.max_drawdown, Decimal("1000.00"))

    # ── Tier mapping ─────────────────────────────────────────────────────

    def test_10k_product_sets_tier_on_account(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account.tier, "10K")

    def test_25k_product_sets_tier_none_on_account(self):
        product_25k = make_challenge_product(
            tier="25K",
            account_size=Decimal("25000.00"),
        )
        enrollment = make_challenge_enrollment(user=self.user, product=product_25k)
        account = activate_challenge_enrollment(enrollment)
        # 25K is not a valid TradingAccount tier — stored as None
        self.assertIsNone(account.tier)

    def test_25k_product_balance_is_25000(self):
        product_25k = make_challenge_product(
            tier="25K",
            account_size=Decimal("25000.00"),
        )
        enrollment = make_challenge_enrollment(user=self.user, product=product_25k)
        account = activate_challenge_enrollment(enrollment)
        self.assertEqual(account.balance, Decimal("25000.00"))
        self.assertEqual(account.initial_balance, Decimal("25000.00"))

    # ── RiskRule ─────────────────────────────────────────────────────────

    def test_risk_rule_created(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.assertTrue(RiskRule.objects.filter(account=account).exists())

    def test_risk_rule_max_drawdown_pct(self):
        account = activate_challenge_enrollment(self.enrollment)
        rule = RiskRule.objects.get(account=account)
        self.assertEqual(rule.max_drawdown_pct, Decimal("10.00"))

    def test_risk_rule_max_daily_loss_pct(self):
        account = activate_challenge_enrollment(self.enrollment)
        rule = RiskRule.objects.get(account=account)
        self.assertEqual(rule.max_daily_loss_pct, Decimal("5.00"))

    def test_risk_rule_max_lot_size_from_product(self):
        account = activate_challenge_enrollment(self.enrollment)
        rule = RiskRule.objects.get(account=account)
        self.assertEqual(rule.max_lot_size, Decimal("5.00"))

    def test_risk_rule_max_open_positions_from_product(self):
        account = activate_challenge_enrollment(self.enrollment)
        rule = RiskRule.objects.get(account=account)
        self.assertEqual(rule.max_open_positions, 30)

    # ── Enrollment link ───────────────────────────────────────────────────

    def test_enrollment_phase1_account_linked(self):
        account = activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.phase1_account_id, account.pk)

    def test_enrollment_status_remains_phase1(self):
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_1)

    # ── Idempotency ───────────────────────────────────────────────────────

    def test_idempotent_returns_same_account(self):
        account1 = activate_challenge_enrollment(self.enrollment)
        account2 = activate_challenge_enrollment(self.enrollment)
        self.assertEqual(account1.pk, account2.pk)

    def test_idempotent_no_duplicate_accounts(self):
        activate_challenge_enrollment(self.enrollment)
        activate_challenge_enrollment(self.enrollment)
        count = TradingAccount.objects.filter(
            user=self.user, account_type="CHALLENGE", phase="Fase 1"
        ).count()
        self.assertEqual(count, 1)

    def test_idempotent_no_duplicate_risk_rules(self):
        activate_challenge_enrollment(self.enrollment)
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        count = RiskRule.objects.filter(account=self.enrollment.phase1_account).count()
        self.assertEqual(count, 1)

    def test_idempotent_with_existing_phase1_account(self):
        """Call activate when phase1_account already set on enrollment."""
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        # A second enrollment object pointing to the same DB row
        enrollment_copy = ChallengeEnrollment.objects.get(pk=self.enrollment.pk)
        account_again = activate_challenge_enrollment(enrollment_copy)
        self.assertEqual(
            TradingAccount.objects.filter(user=self.user, account_type="CHALLENGE").count(), 1
        )
        self.assertIsNotNone(account_again.pk)

    # ── Multi-product / multi-tier edge cases ─────────────────────────────

    def test_50k_product_sets_correct_balance(self):
        product_50k = make_challenge_product(
            tier="50K",
            account_size=Decimal("50000.00"),
            p1_profit_target_pct=Decimal("8.00"),
            p1_max_drawdown_pct=Decimal("8.00"),
        )
        enrollment = make_challenge_enrollment(user=self.user, product=product_50k)
        account = activate_challenge_enrollment(enrollment)
        self.assertEqual(account.balance, Decimal("50000.00"))
        self.assertEqual(account.profit_target, Decimal("4000.00"))   # 8% of 50000
        self.assertEqual(account.max_drawdown, Decimal("4000.00"))    # 8% of 50000

    def test_100k_product_sets_correct_balance(self):
        product_100k = make_challenge_product(
            tier="100K",
            account_size=Decimal("100000.00"),
            p1_profit_target_pct=Decimal("6.00"),
            p1_max_drawdown_pct=Decimal("6.00"),
        )
        enrollment = make_challenge_enrollment(user=self.user, product=product_100k)
        account = activate_challenge_enrollment(enrollment)
        self.assertEqual(account.balance, Decimal("100000.00"))
        self.assertEqual(account.profit_target, Decimal("6000.00"))
        self.assertEqual(account.max_drawdown, Decimal("6000.00"))


# ---------------------------------------------------------------------------
# Shared setup helper for post-activation tests
# ---------------------------------------------------------------------------

def _setup_phase1(user=None, *, profit_target_pct="8.00", max_dd_pct="10.00",
                  max_daily_pct="5.00", min_days=5, max_duration=30):
    """Create enrollment + activate Phase 1. Returns (enrollment, account)."""
    user = user or make_user()
    product = make_challenge_product(
        tier="10K",
        account_size=Decimal("10000.00"),
        p1_profit_target_pct=Decimal(profit_target_pct),
        p1_max_drawdown_pct=Decimal(max_dd_pct),
        p1_max_daily_loss_pct=Decimal(max_daily_pct),
        p1_min_trading_days=min_days,
        p1_max_duration_days=max_duration,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=3,
        p2_max_duration_days=60,
    )
    enrollment = make_challenge_enrollment(user=user, product=product)
    account = activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    return enrollment, account


def _add_closed_trade(account, pnl: Decimal, days_ago: int = 0):
    """Insert a closed Trade + matching LedgerEntry so trading_days counts work."""
    closed = timezone.now() - timezone.timedelta(days=days_ago)
    trade = Trade.objects.create(
        account=account,
        symbol="EUR/USD",
        trade_type="BUY",
        lot_size=Decimal("0.1"),
        entry_price=Decimal("1.10000"),
        exit_price=Decimal("1.11000"),
        profit_loss=pnl,
        closed_at=closed,
    )
    bal_after = Decimal(str(account.balance)) + pnl
    LedgerEntry.objects.create(
        account=account,
        event_type=LedgerEntry.EV_REALIZED,
        amount=pnl,
        balance_after=bal_after,
    )
    # Push balance forward on the account directly (bypass check_rules via update)
    TradingAccount.objects.filter(pk=account.pk).update(balance=bal_after)
    account.refresh_from_db()
    return trade


# ---------------------------------------------------------------------------
# Block 2: evaluate_phase
# ---------------------------------------------------------------------------

class TestEvaluatePhase(TestCase):

    def setUp(self):
        self.user = make_user()
        self.enrollment, self.account = _setup_phase1(self.user)

    # ── Result type ───────────────────────────────────────────────────────

    def test_returns_eval_result(self):
        result = evaluate_phase(self.enrollment)
        self.assertIsInstance(result, EvalResult)

    def test_in_progress_when_no_trades(self):
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, IN_PROGRESS)

    # ── Profit target pass ────────────────────────────────────────────────

    def test_passed_when_profit_target_met_and_min_days_reached(self):
        # Profit target 8% of 10000 = 800; add 5 trades on 5 different days
        for i in range(5):
            _add_closed_trade(self.account, Decimal("200"), days_ago=i)
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, PASSED)

    def test_in_progress_profit_reached_but_not_min_days(self):
        # Profit target met but only 1 trading day
        _add_closed_trade(self.account, Decimal("900"))
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, IN_PROGRESS)

    def test_in_progress_min_days_reached_but_no_profit(self):
        for i in range(5):
            _add_closed_trade(self.account, Decimal("10"), days_ago=i)
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, IN_PROGRESS)

    # ── Drawdown failure ──────────────────────────────────────────────────

    def test_failed_on_max_drawdown_breach(self):
        # Max DD = 10% of 10000 = 1000; breach it via peak_balance update
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, FAILED)
        self.assertIn("Max drawdown", result.fail_reason)

    def test_failed_on_exact_max_drawdown_boundary(self):
        # Exactly at the 10% boundary (1000 loss from peak of 10000)
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("9000")
        )
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, FAILED)

    # ── Daily loss failure ────────────────────────────────────────────────

    def test_failed_on_daily_loss_breach(self):
        # Daily limit = 5% of 10000 = 500; lose 600 today
        _add_closed_trade(self.account, Decimal("-600"))
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, FAILED)
        self.assertIn("Daily loss", result.fail_reason)

    def test_in_progress_daily_loss_below_limit(self):
        # Lose 400 today — below 500 limit
        _add_closed_trade(self.account, Decimal("-400"))
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, IN_PROGRESS)

    # ── Duration failure ──────────────────────────────────────────────────

    def test_failed_when_max_duration_exceeded(self):
        # max_duration = 30 days; simulate account created 31 days ago
        TradingAccount.objects.filter(pk=self.account.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=31)
        )
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, FAILED)
        self.assertIn("Max duration", result.fail_reason)

    def test_in_progress_within_max_duration(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=15)
        )
        self.account.refresh_from_db()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, IN_PROGRESS)

    # ── Metrics dict ──────────────────────────────────────────────────────

    def test_metrics_contains_required_keys(self):
        result = evaluate_phase(self.enrollment)
        for key in ("profit_pct", "realized_dd_pct", "daily_realized_dd_pct",
                    "trading_days", "days_elapsed", "balance"):
            self.assertIn(key, result.metrics)

    def test_metrics_trading_days_counts_closed_trades(self):
        for i in range(3):
            _add_closed_trade(self.account, Decimal("50"), days_ago=i)
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.metrics["trading_days"], 3)

    # ── No active account ─────────────────────────────────────────────────

    def test_failed_when_no_active_account(self):
        enrollment = make_challenge_enrollment(
            user=self.user,
            product=self.enrollment.product,
            status=ChallengeEnrollment.ST_PHASE_1,
        )
        result = evaluate_phase(enrollment)
        self.assertEqual(result.status, FAILED)

    # ── Wrong phase status ────────────────────────────────────────────────

    def test_failed_when_enrollment_is_funded(self):
        self.enrollment.status = ChallengeEnrollment.ST_FUNDED
        self.enrollment.save(update_fields=["status"])
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, FAILED)


# ---------------------------------------------------------------------------
# Block 3: advance_to_phase2
# ---------------------------------------------------------------------------

class TestAdvanceToPhase2(TestCase):

    def setUp(self):
        self.user = make_user()
        self.enrollment, self.p1_account = _setup_phase1(self.user)

    def test_creates_phase2_account(self):
        account2 = advance_to_phase2(self.enrollment)
        self.assertIsInstance(account2, TradingAccount)
        self.assertEqual(account2.phase, "Fase 2")
        self.assertEqual(account2.account_type, "CHALLENGE")

    def test_phase2_account_balance_matches_product_size(self):
        account2 = advance_to_phase2(self.enrollment)
        self.assertEqual(account2.balance, Decimal("10000.00"))

    def test_phase2_profit_target_from_p2_rules(self):
        account2 = advance_to_phase2(self.enrollment)
        # p2_profit_target_pct=5%, account_size=10000 → 500
        self.assertEqual(account2.profit_target, Decimal("500.00"))

    def test_phase1_account_marked_completado(self):
        advance_to_phase2(self.enrollment)
        self.p1_account.refresh_from_db()
        self.assertEqual(self.p1_account.status, TradingAccount.STATUS_FUNDED)  # "Completado"

    def test_enrollment_status_is_phase2(self):
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_2)

    def test_enrollment_phase2_account_linked(self):
        account2 = advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.phase2_account_id, account2.pk)

    def test_phase1_passed_at_set(self):
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.phase1_passed_at)

    def test_risk_rule_created_for_phase2(self):
        account2 = advance_to_phase2(self.enrollment)
        self.assertTrue(RiskRule.objects.filter(account=account2).exists())

    def test_risk_rule_uses_p2_limits(self):
        account2 = advance_to_phase2(self.enrollment)
        rule = RiskRule.objects.get(account=account2)
        self.assertEqual(rule.max_drawdown_pct, Decimal("10.00"))   # p2_max_drawdown_pct
        self.assertEqual(rule.max_daily_loss_pct, Decimal("5.00"))  # p2_max_daily_loss_pct

    # ── Idempotency ───────────────────────────────────────────────────────

    def test_idempotent_returns_same_account(self):
        account2a = advance_to_phase2(self.enrollment)
        account2b = advance_to_phase2(self.enrollment)
        self.assertEqual(account2a.pk, account2b.pk)

    def test_idempotent_no_duplicate_accounts(self):
        advance_to_phase2(self.enrollment)
        advance_to_phase2(self.enrollment)
        self.assertEqual(
            TradingAccount.objects.filter(user=self.user, phase="Fase 2").count(), 1
        )

    # ── Guard: wrong status ───────────────────────────────────────────────

    def test_raises_if_enrollment_not_phase1(self):
        # Put enrollment in PHASE_2 manually
        self.enrollment.status = ChallengeEnrollment.ST_PHASE_2
        self.enrollment.phase2_account = None
        self.enrollment.save(update_fields=["status", "phase2_account"])
        with self.assertRaises(ValueError):
            advance_to_phase2(self.enrollment)

    def test_raises_if_enrollment_failed(self):
        self.enrollment.status = ChallengeEnrollment.ST_FAILED
        self.enrollment.save(update_fields=["status"])
        with self.assertRaises(ValueError):
            advance_to_phase2(self.enrollment)


# ---------------------------------------------------------------------------
# Block 4: advance_to_funded
# ---------------------------------------------------------------------------

class TestAdvanceToFunded(TestCase):

    def setUp(self):
        self.user = make_user()
        self.enrollment, self.p1_account = _setup_phase1(self.user)
        self.p2_account = advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()

    def test_creates_funded_account(self):
        funded = advance_to_funded(self.enrollment)
        self.assertIsInstance(funded, TradingAccount)
        self.assertEqual(funded.account_type, "FUNDED")
        self.assertEqual(funded.phase, "Funded")

    def test_funded_account_balance_matches_product_size(self):
        funded = advance_to_funded(self.enrollment)
        self.assertEqual(funded.balance, Decimal("10000.00"))

    def test_funded_account_status_activo(self):
        funded = advance_to_funded(self.enrollment)
        self.assertEqual(funded.status, TradingAccount.STATUS_ACTIVE)

    def test_phase2_account_marked_completado(self):
        advance_to_funded(self.enrollment)
        self.p2_account.refresh_from_db()
        self.assertEqual(self.p2_account.status, TradingAccount.STATUS_FUNDED)

    def test_enrollment_status_is_funded(self):
        advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_FUNDED)

    def test_enrollment_funded_account_linked(self):
        funded = advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.funded_account_id, funded.pk)

    def test_funded_at_timestamp_set(self):
        advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.funded_at)

    def test_phase2_passed_at_timestamp_set(self):
        advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.phase2_passed_at)

    def test_funded_config_created(self):
        advance_to_funded(self.enrollment)
        self.assertTrue(FundedConfig.objects.filter(enrollment=self.enrollment).exists())

    def test_funded_config_profit_split_from_product(self):
        advance_to_funded(self.enrollment)
        config = FundedConfig.objects.get(enrollment=self.enrollment)
        self.assertEqual(config.profit_split_pct, Decimal("80.00"))

    def test_funded_config_type_is_funded_sim(self):
        advance_to_funded(self.enrollment)
        config = FundedConfig.objects.get(enrollment=self.enrollment)
        self.assertEqual(config.funded_type, FundedConfig.FUNDED_SIM)

    def test_risk_rule_created_for_funded_account(self):
        funded = advance_to_funded(self.enrollment)
        self.assertTrue(RiskRule.objects.filter(account=funded).exists())

    # ── Idempotency ───────────────────────────────────────────────────────

    def test_idempotent_returns_same_funded_account(self):
        funded_a = advance_to_funded(self.enrollment)
        funded_b = advance_to_funded(self.enrollment)
        self.assertEqual(funded_a.pk, funded_b.pk)

    def test_idempotent_no_duplicate_funded_config(self):
        advance_to_funded(self.enrollment)
        advance_to_funded(self.enrollment)
        self.assertEqual(
            FundedConfig.objects.filter(enrollment=self.enrollment).count(), 1
        )

    # ── Guard: wrong status ───────────────────────────────────────────────

    def test_raises_if_enrollment_not_phase2(self):
        fresh_enrollment, _ = _setup_phase1(self.user)
        with self.assertRaises(ValueError):
            advance_to_funded(fresh_enrollment)

    def test_raises_if_enrollment_already_failed(self):
        self.enrollment.status = ChallengeEnrollment.ST_FAILED
        self.enrollment.save(update_fields=["status"])
        with self.assertRaises(ValueError):
            advance_to_funded(self.enrollment)


# ---------------------------------------------------------------------------
# Block 5: evaluate_enrollment_now
# ---------------------------------------------------------------------------

class TestEvaluateEnrollmentNow(TestCase):

    def setUp(self):
        self.user = make_user()
        self.enrollment, self.account = _setup_phase1(self.user)

    # ── IN_PROGRESS path ──────────────────────────────────────────────────

    def test_in_progress_returns_in_progress(self):
        result = evaluate_enrollment_now(self.enrollment.pk)
        self.assertEqual(result.status, IN_PROGRESS)

    def test_in_progress_does_not_advance_enrollment(self):
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_1)

    # ── PASSED Phase 1 → advance to Phase 2 ──────────────────────────────

    def test_phase1_passed_advances_to_phase2(self):
        for i in range(5):
            _add_closed_trade(self.account, Decimal("200"), days_ago=i)
        self.account.refresh_from_db()
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_2)

    def test_phase1_passed_creates_phase2_account(self):
        for i in range(5):
            _add_closed_trade(self.account, Decimal("200"), days_ago=i)
        self.account.refresh_from_db()
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.phase2_account_id)

    def test_phase1_passed_marks_phase1_account_completado(self):
        for i in range(5):
            _add_closed_trade(self.account, Decimal("200"), days_ago=i)
        self.account.refresh_from_db()
        evaluate_enrollment_now(self.enrollment.pk)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, TradingAccount.STATUS_FUNDED)

    # ── PASSED Phase 2 → advance to Funded ───────────────────────────────

    def test_phase2_passed_advances_to_funded(self):
        # Advance to phase 2 first
        p2_account = advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        # Add enough trades on phase 2 account to pass
        for i in range(3):
            _add_closed_trade(p2_account, Decimal("200"), days_ago=i)
        p2_account.refresh_from_db()
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_FUNDED)

    def test_phase2_passed_creates_funded_config(self):
        p2_account = advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        for i in range(3):
            _add_closed_trade(p2_account, Decimal("200"), days_ago=i)
        p2_account.refresh_from_db()
        evaluate_enrollment_now(self.enrollment.pk)
        self.assertTrue(FundedConfig.objects.filter(enrollment=self.enrollment).exists())

    # ── FAILED path ───────────────────────────────────────────────────────

    def test_phase1_failed_marks_enrollment_failed(self):
        # Breach max DD
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_FAILED)

    def test_phase1_failed_sets_failed_at_phase(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.failed_at_phase, ChallengeEnrollment.FAILED_AT_PHASE_1)

    def test_phase1_failed_suspends_active_account(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        evaluate_enrollment_now(self.enrollment.pk)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, TradingAccount.STATUS_SUSPENDED)

    def test_phase1_failed_stores_failure_reason(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        evaluate_enrollment_now(self.enrollment.pk)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.failure_reason)
        self.assertGreater(len(self.enrollment.failure_reason), 0)

    # ── Skipped statuses ──────────────────────────────────────────────────

    def test_returns_in_progress_when_already_funded(self):
        self.enrollment.status = ChallengeEnrollment.ST_FUNDED
        self.enrollment.save(update_fields=["status"])
        result = evaluate_enrollment_now(self.enrollment.pk)
        self.assertEqual(result.status, IN_PROGRESS)

    def test_returns_in_progress_when_already_failed(self):
        self.enrollment.status = ChallengeEnrollment.ST_FAILED
        self.enrollment.save(update_fields=["status"])
        result = evaluate_enrollment_now(self.enrollment.pk)
        self.assertEqual(result.status, IN_PROGRESS)
