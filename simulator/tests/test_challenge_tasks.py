# simulator/tests/test_challenge_tasks.py
"""
Phase 4B.3 — Celery evaluate_all_challenges_task tests.

Strategy: call the task function directly (not via .delay()/.apply()) so tests
run synchronously without a broker. challenge_engine.evaluate_enrollment_now is
patched where needed so task-level orchestration is tested independently of the
engine's evaluation logic. Engine integration is covered by test_challenge_engine.py.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from simulator.challenge_engine import (
    IN_PROGRESS, PASSED, FAILED,
    EvalResult,
    activate_challenge_enrollment,
    advance_to_phase2,
)
from simulator.models import (
    ChallengeEnrollment,
    LedgerEntry,
    Trade,
    TradingAccount,
)
from simulator.tasks import evaluate_all_challenges_task
from simulator.tests.factories import make_challenge_enrollment, make_challenge_product

User = get_user_model()

# Patch target: evaluate_enrollment_now on the real module object (imported by reference inside the task).
_PATCH_EVAL = "simulator.challenge_engine.evaluate_enrollment_now"


def _make_user(username):
    return User.objects.create_user(username=username, password="pass", email=f"{username}@t.com")


def _make_product(**kwargs):
    return make_challenge_product(**kwargs)


def _make_activated_enrollment(username):
    user = _make_user(username)
    product = _make_product()
    enrollment = make_challenge_enrollment(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _add_closed_trade(account, pnl: Decimal, days_ago: int = 0):
    closed = timezone.now() - timezone.timedelta(days=days_ago)
    Trade.objects.create(
        account=account, symbol="EUR/USD", trade_type="BUY",
        lot_size=Decimal("0.1"), entry_price=Decimal("1.10000"),
        exit_price=Decimal("1.11000"), profit_loss=pnl, closed_at=closed,
    )
    bal_after = Decimal(str(account.balance)) + pnl
    LedgerEntry.objects.create(
        account=account, event_type=LedgerEntry.EV_REALIZED,
        amount=pnl, balance_after=bal_after,
    )
    TradingAccount.objects.filter(pk=account.pk).update(balance=bal_after)
    account.refresh_from_db()


def _run_task():
    """Invoke the task synchronously via .apply() — no broker needed."""
    return evaluate_all_challenges_task.apply().get()


class TaskNoEnrollmentsTest(TestCase):
    def test_returns_zero_counts_when_no_active_enrollments(self):
        result = _run_task()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["advanced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], 0)

    def test_result_contains_elapsed_ms(self):
        result = _run_task()
        self.assertIn("elapsed_ms", result)
        self.assertIsInstance(result["elapsed_ms"], int)


class TaskInProgressTest(TestCase):
    def test_enrollment_in_progress_counted_as_processed_not_advanced(self):
        enrollment = _make_activated_enrollment("ip_user")
        # No trades → IN_PROGRESS
        result = _run_task()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["advanced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], 0)

    def test_inactive_enrollments_not_included(self):
        enrollment = _make_activated_enrollment("noop_user")
        # Force to FUNDED so it's excluded from the batch
        ChallengeEnrollment.objects.filter(pk=enrollment.pk).update(
            status=ChallengeEnrollment.ST_FUNDED
        )
        result = _run_task()
        self.assertEqual(result["processed"], 0)


class TaskAdvancesPhase1Test(TestCase):
    def test_passed_phase1_counts_as_advanced(self):
        enrollment = _make_activated_enrollment("p1_pass")
        product = enrollment.product
        account = enrollment.phase1_account

        # Satisfy profit target + min trading days
        profit_target = product.p1_profit_target_amount()
        _add_closed_trade(account, profit_target + Decimal("1"))
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal(str(account.initial_balance)) + profit_target + Decimal("1")
        )
        for d in range(1, product.p1_min_trading_days + 1):
            _add_closed_trade(account, Decimal("0"), days_ago=d)

        result = _run_task()
        self.assertEqual(result["advanced"], 1)
        self.assertEqual(result["errors"], 0)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_PHASE_2)

    def test_passed_phase1_sets_phase2_account(self):
        enrollment = _make_activated_enrollment("p1_acct")
        product = enrollment.product
        account = enrollment.phase1_account

        profit_target = product.p1_profit_target_amount()
        _add_closed_trade(account, profit_target + Decimal("1"))
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal(str(account.initial_balance)) + profit_target + Decimal("1")
        )
        for d in range(1, product.p1_min_trading_days + 1):
            _add_closed_trade(account, Decimal("0"), days_ago=d)

        _run_task()
        enrollment.refresh_from_db()
        self.assertIsNotNone(enrollment.phase2_account_id)


class TaskAdvancesPhase2Test(TestCase):
    def test_passed_phase2_counts_as_advanced_and_creates_funded(self):
        enrollment = _make_activated_enrollment("p2_pass")
        advance_to_phase2(enrollment)
        enrollment.refresh_from_db()

        product = enrollment.product
        account = enrollment.phase2_account

        profit_target = product.p2_profit_target_amount()
        _add_closed_trade(account, profit_target + Decimal("1"))
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal(str(account.initial_balance)) + profit_target + Decimal("1")
        )
        for d in range(1, product.p2_min_trading_days + 1):
            _add_closed_trade(account, Decimal("0"), days_ago=d)

        result = _run_task()
        self.assertEqual(result["advanced"], 1)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)
        self.assertIsNotNone(enrollment.funded_account_id)


class TaskFailsOnRulesViolationTest(TestCase):
    def test_drawdown_breach_marks_failed_and_counts(self):
        enrollment = _make_activated_enrollment("dd_fail")
        account = enrollment.phase1_account

        # Lose 20% of 10K — exceeds p1_max_drawdown_pct (default 10%)
        _add_closed_trade(account, Decimal("-2000"))
        TradingAccount.objects.filter(pk=account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8000")
        )

        result = _run_task()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["errors"], 0)

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FAILED)


class TaskContinuesOnExceptionTest(TestCase):
    def test_one_enrollment_exception_does_not_abort_batch(self):
        e1 = _make_activated_enrollment("exc_user1")
        e2 = _make_activated_enrollment("exc_user2")

        call_count = {"n": 0}
        original_ids = sorted([e1.pk, e2.pk])

        def _side_effect(enrollment_id):
            call_count["n"] += 1
            if enrollment_id == original_ids[0]:
                raise RuntimeError("simulated DB failure")
            return EvalResult(status=IN_PROGRESS)

        with patch(_PATCH_EVAL, side_effect=_side_effect):
            result = _run_task()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["processed"], 1)   # only the one that didn't raise
        self.assertEqual(call_count["n"], 2)        # both were attempted

    def test_all_errors_still_returns_summary(self):
        _make_activated_enrollment("all_err_user")

        with patch(_PATCH_EVAL, side_effect=Exception("boom")):
            result = _run_task()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["processed"], 0)
        self.assertIn("elapsed_ms", result)


class TaskReturnsSummaryTest(TestCase):
    def test_summary_keys_always_present(self):
        result = _run_task()
        for key in ("processed", "advanced", "failed", "errors", "elapsed_ms"):
            self.assertIn(key, result, f"Key '{key}' missing from summary")

    def test_multiple_enrollments_all_counted(self):
        for i in range(3):
            _make_activated_enrollment(f"multi_{i}")

        with patch(_PATCH_EVAL, return_value=EvalResult(status=IN_PROGRESS)):
            result = _run_task()

        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["advanced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], 0)
