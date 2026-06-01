"""
simulator/tests/test_dashboard_challenge_lifecycle.py — Phase 4C.1

Verifies that trading_dashboard view injects challenge lifecycle context keys
and that their values are correct for accounts linked to ChallengeEnrollments.

Tests context dict values only (not full template rendering).
Template rendering spot-checks are at the end of the file.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.challenge_engine import (
    IN_PROGRESS, PASSED, FAILED,
    activate_challenge_enrollment,
    advance_to_phase2,
    advance_to_funded,
)
from simulator.models import ChallengeEnrollment, FundedConfig, LedgerEntry, Trade, TradingAccount
from simulator.tests.factories import (
    make_account,
    make_challenge_enrollment,
    make_challenge_product,
    make_user,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_product(**kwargs):
    defaults = dict(
        tier="10K",
        account_size=Decimal("10000.00"),
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=5,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=3,
        p2_max_duration_days=60,
        profit_split_pct=Decimal("80.00"),
    )
    defaults.update(kwargs)
    return make_challenge_product(**defaults)


def _add_closed_trade(account, pnl: Decimal, days_ago: int = 0):
    closed = timezone.now() - timezone.timedelta(days=days_ago)
    Trade.objects.create(
        account=account, symbol="EUR/USD", trade_type="BUY",
        lot_size=Decimal("0.1"), entry_price=Decimal("1.10000"),
        exit_price=Decimal("1.11000"), profit_loss=pnl, closed_at=closed,
    )
    new_bal = Decimal(str(account.balance)) + pnl
    LedgerEntry.objects.create(
        account=account, event_type=LedgerEntry.EV_REALIZED,
        amount=pnl, balance_after=new_bal,
    )
    TradingAccount.objects.filter(pk=account.pk).update(balance=new_bal)
    account.refresh_from_db()


# ─────────────────────────────────────────────────────────────────────────────
# 1. All lifecycle keys present in context
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeLifecycleContextKeys(TestCase):
    """All 9 challenge lifecycle keys must be present regardless of enrollment."""

    EXPECTED_KEYS = [
        "challenge_enrollment",
        "challenge_phase_label",
        "challenge_eval_status",
        "challenge_eval_fail_reason",
        "challenge_trading_days",
        "challenge_min_trading_days",
        "challenge_days_remaining",
        "challenge_max_duration_days",
        "challenge_funded_config",
    ]

    def _ctx(self, account, user):
        self.client.force_login(user)
        url = reverse("simulator:dashboard_account", args=[account.pk])
        return self.client.get(url).context

    def test_all_keys_present_for_enrolled_account(self):
        user = make_user()
        product = _make_product()
        enrollment = make_challenge_enrollment(user=user, product=product)
        account = activate_challenge_enrollment(enrollment)
        enrollment.refresh_from_db()
        ctx = self._ctx(account, user)
        for key in self.EXPECTED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")

    def test_all_keys_present_for_non_enrolled_challenge_account(self):
        user = make_user()
        account = make_account(user=user, account_type="CHALLENGE", tier="10K")
        ctx = self._ctx(account, user)
        for key in self.EXPECTED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")

    def test_all_keys_present_for_retail_account(self):
        user = make_user()
        account = make_account(user=user, account_type="RETAIL", tier=None)
        ctx = self._ctx(account, user)
        for key in self.EXPECTED_KEYS:
            self.assertIn(key, ctx, f"Key missing: {key!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Non-enrolled accounts get None values
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeLifecycleNoEnrollment(TestCase):
    """Accounts with no ChallengeEnrollment get all challenge keys as None."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(user=self.user, account_type="CHALLENGE", tier="10K")
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_enrollment_is_none(self):
        self.assertIsNone(self._ctx()["challenge_enrollment"])

    def test_phase_label_is_none(self):
        self.assertIsNone(self._ctx()["challenge_phase_label"])

    def test_eval_status_is_none(self):
        self.assertIsNone(self._ctx()["challenge_eval_status"])

    def test_trading_days_is_none(self):
        self.assertIsNone(self._ctx()["challenge_trading_days"])

    def test_days_remaining_is_none(self):
        self.assertIsNone(self._ctx()["challenge_days_remaining"])

    def test_funded_config_is_none(self):
        self.assertIsNone(self._ctx()["challenge_funded_config"])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Phase 1 context values
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengePhase1Context(TestCase):

    def setUp(self):
        self.user = make_user()
        self.product = _make_product(p1_min_trading_days=5, p1_max_duration_days=30)
        self.enrollment = make_challenge_enrollment(user=self.user, product=self.product)
        self.account = activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_enrollment_is_returned(self):
        self.assertEqual(self._ctx()["challenge_enrollment"].pk, self.enrollment.pk)

    def test_phase_label_is_phase1(self):
        self.assertEqual(self._ctx()["challenge_phase_label"], "Phase 1")

    def test_eval_status_is_in_progress_with_no_trades(self):
        self.assertEqual(self._ctx()["challenge_eval_status"], IN_PROGRESS)

    def test_trading_days_zero_with_no_trades(self):
        self.assertEqual(self._ctx()["challenge_trading_days"], 0)

    def test_min_trading_days_from_product(self):
        self.assertEqual(self._ctx()["challenge_min_trading_days"], 5)

    def test_max_duration_days_from_product(self):
        self.assertEqual(self._ctx()["challenge_max_duration_days"], 30)

    def test_days_remaining_close_to_max_on_fresh_account(self):
        days_remaining = self._ctx()["challenge_days_remaining"]
        # Fresh account: days_elapsed ≈ 0, so remaining ≈ 30
        self.assertGreaterEqual(days_remaining, 29)
        self.assertLessEqual(days_remaining, 30)

    def test_funded_config_none_in_phase1(self):
        self.assertIsNone(self._ctx()["challenge_funded_config"])

    def test_trading_days_increments_with_closed_trades(self):
        _add_closed_trade(self.account, Decimal("50"), days_ago=1)
        _add_closed_trade(self.account, Decimal("50"), days_ago=2)
        self.assertEqual(self._ctx()["challenge_trading_days"], 2)

    def test_eval_status_passed_when_target_and_min_days_met(self):
        for i in range(5):
            _add_closed_trade(self.account, Decimal("200"), days_ago=i)
        self.account.refresh_from_db()
        self.assertEqual(self._ctx()["challenge_eval_status"], PASSED)

    def test_eval_status_failed_on_drawdown_breach(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        self.assertEqual(self._ctx()["challenge_eval_status"], FAILED)

    def test_fail_reason_populated_on_failure(self):
        TradingAccount.objects.filter(pk=self.account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8900")
        )
        ctx = self._ctx()
        self.assertEqual(ctx["challenge_eval_status"], FAILED)
        self.assertIsNotNone(ctx["challenge_eval_fail_reason"])
        self.assertGreater(len(ctx["challenge_eval_fail_reason"]), 0)

    def test_fail_reason_none_when_in_progress(self):
        self.assertIsNone(self._ctx()["challenge_eval_fail_reason"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Phase 2 context values
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengePhase2Context(TestCase):

    def setUp(self):
        self.user = make_user()
        self.product = _make_product(p2_min_trading_days=3, p2_max_duration_days=60)
        self.enrollment = make_challenge_enrollment(user=self.user, product=self.product)
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        self.p2_account = advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.p2_account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_phase_label_is_phase2(self):
        self.assertEqual(self._ctx()["challenge_phase_label"], "Phase 2")

    def test_enrollment_returned_for_phase2_account(self):
        self.assertEqual(self._ctx()["challenge_enrollment"].pk, self.enrollment.pk)

    def test_min_trading_days_from_p2_rules(self):
        self.assertEqual(self._ctx()["challenge_min_trading_days"], 3)

    def test_max_duration_days_from_p2_rules(self):
        self.assertEqual(self._ctx()["challenge_max_duration_days"], 60)

    def test_eval_status_in_progress_with_no_trades(self):
        self.assertEqual(self._ctx()["challenge_eval_status"], IN_PROGRESS)

    def test_funded_config_none_in_phase2(self):
        self.assertIsNone(self._ctx()["challenge_funded_config"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Funded account context values
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeFundedContext(TestCase):

    def setUp(self):
        self.user = make_user()
        self.product = _make_product(profit_split_pct=Decimal("80.00"))
        self.enrollment = make_challenge_enrollment(user=self.user, product=self.product)
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.funded_account = advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.funded_account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_phase_label_is_funded(self):
        self.assertEqual(self._ctx()["challenge_phase_label"], "Funded")

    def test_enrollment_returned_for_funded_account(self):
        self.assertEqual(self._ctx()["challenge_enrollment"].pk, self.enrollment.pk)

    def test_eval_status_none_for_funded(self):
        # evaluate_phase is not called for FUNDED status
        self.assertIsNone(self._ctx()["challenge_eval_status"])

    def test_trading_days_none_for_funded(self):
        self.assertIsNone(self._ctx()["challenge_trading_days"])

    def test_days_remaining_none_for_funded(self):
        self.assertIsNone(self._ctx()["challenge_days_remaining"])

    def test_funded_config_returned(self):
        funded_config = self._ctx()["challenge_funded_config"]
        self.assertIsNotNone(funded_config)
        self.assertIsInstance(funded_config, FundedConfig)

    def test_funded_config_profit_split_pct(self):
        funded_config = self._ctx()["challenge_funded_config"]
        self.assertEqual(funded_config.profit_split_pct, Decimal("80.00"))

    def test_funded_config_type_is_funded_sim(self):
        funded_config = self._ctx()["challenge_funded_config"]
        self.assertEqual(funded_config.funded_type, FundedConfig.FUNDED_SIM)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Failed enrollment — graceful handling
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeFailedEnrollment(TestCase):

    def setUp(self):
        self.user = make_user()
        self.product = _make_product()
        self.enrollment = make_challenge_enrollment(user=self.user, product=self.product)
        self.account = activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        # Manually mark as failed (simulate engine outcome)
        self.enrollment.status = ChallengeEnrollment.ST_FAILED
        self.enrollment.failed_at_phase = ChallengeEnrollment.FAILED_AT_PHASE_1
        self.enrollment.failure_reason = "Max drawdown exceeded"
        self.enrollment.save(update_fields=["status", "failed_at_phase", "failure_reason"])
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _ctx(self):
        return self.client.get(self.url).context

    def test_phase_label_is_failed(self):
        self.assertEqual(self._ctx()["challenge_phase_label"], "Failed")

    def test_eval_status_none_for_failed_enrollment(self):
        # evaluate_phase not called when enrollment.status == FAILED
        self.assertIsNone(self._ctx()["challenge_eval_status"])

    def test_enrollment_still_returned_for_failed(self):
        self.assertIsNotNone(self._ctx()["challenge_enrollment"])

    def test_funded_config_none_for_failed(self):
        self.assertIsNone(self._ctx()["challenge_funded_config"])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Template rendering spot-checks
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeLifecycleTemplate(TestCase):
    """Spot-check that lifecycle values appear in the rendered HTML."""

    def setUp(self):
        self.user = make_user()
        self.product = _make_product()
        self.enrollment = make_challenge_enrollment(user=self.user, product=self.product)
        self.account = activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        self.client.force_login(self.user)
        self.url = reverse("simulator:dashboard_account", args=[self.account.pk])

    def _html(self):
        return self.client.get(self.url).content.decode()

    def test_phase1_label_in_html(self):
        self.assertIn("Phase 1", self._html())

    def test_in_progress_text_in_html(self):
        self.assertIn("In Progress", self._html())

    def test_trading_days_row_in_html(self):
        self.assertIn("Trading Days", self._html())

    def test_days_remaining_row_in_html(self):
        self.assertIn("Days Remaining", self._html())

    def test_chal_lifecycle_block_present(self):
        self.assertIn("chalLifecycle", self._html())

    def test_funded_section_shows_profit_split(self):
        # Advance to funded
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        funded = advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        url = reverse("simulator:dashboard_account", args=[funded.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Profit Split", html)
        self.assertIn("80", html)

    def test_no_lifecycle_block_for_non_enrolled_account(self):
        # Plain challenge account with no enrollment
        plain_account = make_account(user=self.user, account_type="CHALLENGE", tier="10K")
        url = reverse("simulator:dashboard_account", args=[plain_account.pk])
        html = self.client.get(url).content.decode()
        self.assertNotIn("chalLifecycle", html)
