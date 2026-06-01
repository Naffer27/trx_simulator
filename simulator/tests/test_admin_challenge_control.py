# simulator/tests/test_admin_challenge_control.py
"""
Phase 4C.3 — Django Admin Challenge Control Panel tests.

Coverage:
  - ChallengeProductAdmin: list, search, fieldsets accessible
  - ChallengeEnrollmentAdmin: list, status badge, account links, actions
  - FundedConfigAdmin: list, funded_account_link
  - activate_enrollments action: creates Phase 1 account, idempotent, error handling
  - evaluate_enrollments_now action: PASSED/FAILED/IN_PROGRESS routing
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.admin import (
    ChallengeEnrollmentAdmin,
    ChallengeProductAdmin,
    FundedConfigAdmin,
    activate_enrollments,
    evaluate_enrollments_now,
)
from simulator.challenge_engine import (
    IN_PROGRESS, PASSED, FAILED,
    activate_challenge_enrollment,
    advance_to_phase2,
    advance_to_funded,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    LedgerEntry,
    Trade,
    TradingAccount,
)
from simulator.tests.factories import make_challenge_enrollment, make_challenge_product

User = get_user_model()


def _make_user(username="trader_admin"):
    return User.objects.create_user(username=username, password="pass", email=f"{username}@test.com")


def _make_product(**kwargs):
    return make_challenge_product(**kwargs)


def _make_enrollment(user=None, **kwargs):
    user = user or _make_user("enroll_user")
    product = _make_product()
    return make_challenge_enrollment(user=user, product=product, **kwargs)


def _staff_request(factory, url="/admin/"):
    user = User.objects.create_superuser("admin_staff", "s@t.com", "pass")
    request = factory.post(url)
    request.user = user
    # Attach messages middleware
    setattr(request, "session", {})
    messages = FallbackStorage(request)
    setattr(request, "_messages", messages)
    return request


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


class ChallengeProductAdminTests(TestCase):
    def setUp(self):
        self.site    = AdminSite()
        self.factory = RequestFactory()
        self.admin   = ChallengeProductAdmin(ChallengeProduct, self.site)
        self.product = _make_product(name="Pro 10K", tier="10K")

    def test_list_display_contains_required_fields(self):
        ld = self.admin.list_display
        for field in ("name", "tier", "price_usd", "account_size", "is_active"):
            self.assertIn(field, ld)

    def test_phases_summary_renders(self):
        result = self.admin.phases_summary(self.product)
        self.assertIn(str(self.product.p1_profit_target_pct), result)
        self.assertIn(str(self.product.p2_profit_target_pct), result)

    def test_search_fields_contains_name(self):
        self.assertIn("name", self.admin.search_fields)

    def test_list_filter_contains_is_active_and_tier(self):
        self.assertIn("is_active", self.admin.list_filter)
        self.assertIn("tier", self.admin.list_filter)

    def test_fieldsets_include_p1_and_p2_sections(self):
        titles = [fs[0] for fs in self.admin.fieldsets]
        self.assertIn("Phase 1 Rules", titles)
        self.assertIn("Phase 2 Rules", titles)


class ChallengeEnrollmentAdminTests(TestCase):
    def setUp(self):
        self.site       = AdminSite()
        self.factory    = RequestFactory()
        self.admin_inst = ChallengeEnrollmentAdmin(ChallengeEnrollment, self.site)
        self.user       = _make_user("enroll_user_2")
        self.enrollment = _make_enrollment(user=self.user)

    def test_list_display_contains_user_and_status_badge(self):
        ld = self.admin_inst.list_display
        self.assertIn("user", ld)
        self.assertIn("status_badge", ld)

    def test_status_badge_returns_html_span(self):
        badge = self.admin_inst.status_badge(self.enrollment)
        self.assertIn("<span", badge)
        self.assertIn(self.enrollment.status, badge)

    def test_phase1_link_returns_dash_when_no_account(self):
        result = self.admin_inst.phase1_link(self.enrollment)
        self.assertEqual(result, "—")

    def test_phase1_link_returns_anchor_when_account_exists(self):
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        result = self.admin_inst.phase1_link(self.enrollment)
        self.assertIn("<a", result)
        self.assertIn(str(self.enrollment.phase1_account.pk), result)

    def test_phase2_link_returns_dash_when_no_account(self):
        result = self.admin_inst.phase2_link(self.enrollment)
        self.assertEqual(result, "—")

    def test_funded_link_returns_dash_when_no_account(self):
        result = self.admin_inst.funded_link(self.enrollment)
        self.assertEqual(result, "—")

    def test_actions_registered(self):
        action_names = [a.__name__ if callable(a) else a for a in self.admin_inst.actions]
        self.assertIn("activate_enrollments", action_names)
        self.assertIn("evaluate_enrollments_now", action_names)


class ActivateEnrollmentsActionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site    = AdminSite()

    def test_activate_creates_phase1_account(self):
        enrollment = _make_enrollment()
        request    = _staff_request(self.factory)
        qs         = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        activate_enrollments(None, request, qs)
        enrollment.refresh_from_db()
        self.assertIsNotNone(enrollment.phase1_account_id)
        self.assertEqual(enrollment.phase1_account.account_type, "CHALLENGE")

    def test_activate_is_idempotent(self):
        enrollment = _make_enrollment()
        activate_challenge_enrollment(enrollment)
        enrollment.refresh_from_db()
        first_account_id = enrollment.phase1_account_id
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        activate_enrollments(None, request, qs)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.phase1_account_id, first_account_id)

    def test_activate_success_message_added(self):
        enrollment = _make_enrollment()
        request    = _staff_request(self.factory)
        qs         = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        activate_enrollments(None, request, qs)
        storage  = list(request._messages)
        texts    = [str(m) for m in storage]
        self.assertTrue(any("activated" in t for t in texts))

    def test_activate_multiple_enrollments(self):
        user = _make_user("multi_act")
        product = _make_product()
        e1 = make_challenge_enrollment(user=user, product=product)
        e2 = make_challenge_enrollment(user=user, product=product)
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk__in=[e1.pk, e2.pk])
        activate_enrollments(None, request, qs)
        e1.refresh_from_db()
        e2.refresh_from_db()
        self.assertIsNotNone(e1.phase1_account_id)
        self.assertIsNotNone(e2.phase1_account_id)


class EvaluateEnrollmentsNowActionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site    = AdminSite()

    def _make_activated_enrollment(self, username="eval_user"):
        enrollment = _make_enrollment(user=_make_user(username))
        activate_challenge_enrollment(enrollment)
        enrollment.refresh_from_db()
        return enrollment

    def test_evaluate_in_progress_when_no_trades(self):
        enrollment = self._make_activated_enrollment("eval_ip")
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        texts = [str(m) for m in request._messages]
        self.assertTrue(any("in progress" in t.lower() for t in texts))

    def test_evaluate_failed_on_drawdown(self):
        enrollment = self._make_activated_enrollment("eval_fail")
        account = enrollment.phase1_account
        # Exceed max drawdown: lose 20% of 10K = $2000
        _add_closed_trade(account, Decimal("-2000"))
        TradingAccount.objects.filter(pk=account.pk).update(
            peak_balance=Decimal("10000"), balance=Decimal("8000")
        )
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FAILED)
        texts = [str(m) for m in request._messages]
        self.assertTrue(any("failed" in t.lower() for t in texts))

    def test_evaluate_passed_phase1_advances_to_phase2(self):
        enrollment = self._make_activated_enrollment("eval_p1_pass")
        account = enrollment.phase1_account
        product = enrollment.product
        # Meet profit target
        profit_target = product.p1_profit_target_amount()
        _add_closed_trade(account, profit_target + Decimal("1"))
        bal_after = Decimal(str(account.initial_balance)) + profit_target + Decimal("1")
        TradingAccount.objects.filter(pk=account.pk).update(balance=bal_after)
        account.refresh_from_db()
        # Meet min trading days
        for d in range(1, product.p1_min_trading_days + 1):
            _add_closed_trade(account, Decimal("0"), days_ago=d)
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_PHASE_2)
        self.assertIsNotNone(enrollment.phase2_account_id)
        texts = [str(m) for m in request._messages]
        self.assertTrue(any("passed" in t.lower() for t in texts))

    def test_evaluate_passed_phase2_advances_to_funded(self):
        enrollment = self._make_activated_enrollment("eval_p2_pass")
        advance_to_phase2(enrollment)
        enrollment.refresh_from_db()
        account = enrollment.phase2_account
        product = enrollment.product
        # Meet profit target for phase 2
        profit_target = product.p2_profit_target_amount()
        _add_closed_trade(account, profit_target + Decimal("1"))
        bal_after = Decimal(str(account.initial_balance)) + profit_target + Decimal("1")
        TradingAccount.objects.filter(pk=account.pk).update(balance=bal_after)
        account.refresh_from_db()
        for d in range(1, product.p2_min_trading_days + 1):
            _add_closed_trade(account, Decimal("0"), days_ago=d)
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)
        self.assertIsNotNone(enrollment.funded_account_id)

    def test_evaluate_noop_on_funded_enrollment(self):
        enrollment = self._make_activated_enrollment("eval_funded_noop")
        advance_to_phase2(enrollment)
        enrollment.refresh_from_db()
        advance_to_funded(enrollment)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        enrollment.refresh_from_db()
        # Status must remain FUNDED — action is a no-op
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)

    def test_evaluate_noop_on_failed_enrollment(self):
        enrollment = self._make_activated_enrollment("eval_fail_noop")
        ChallengeEnrollment.objects.filter(pk=enrollment.pk).update(
            status=ChallengeEnrollment.ST_FAILED
        )
        request = _staff_request(self.factory)
        qs = ChallengeEnrollment.objects.filter(pk=enrollment.pk)
        evaluate_enrollments_now(None, request, qs)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FAILED)


class FundedConfigAdminTests(TestCase):
    def setUp(self):
        self.site       = AdminSite()
        self.factory    = RequestFactory()
        self.admin_inst = FundedConfigAdmin(FundedConfig, self.site)
        self.user       = _make_user("funded_admin_user")
        self.enrollment = _make_enrollment(user=self.user)
        activate_challenge_enrollment(self.enrollment)
        self.enrollment.refresh_from_db()
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.funded_account = advance_to_funded(self.enrollment)
        self.enrollment.refresh_from_db()
        self.funded_config = FundedConfig.objects.get(enrollment=self.enrollment)

    def test_list_display_contains_required_fields(self):
        ld = self.admin_inst.list_display
        for field in ("enrollment", "funded_type", "profit_split_pct", "min_payout_usd"):
            self.assertIn(field, ld)

    def test_funded_account_link_returns_anchor(self):
        result = self.admin_inst.funded_account_link(self.funded_config)
        self.assertIn("<a", result)
        self.assertIn(str(self.funded_account.pk), result)

    def test_readonly_fields_include_financial_terms(self):
        rf = self.admin_inst.readonly_fields
        for field in ("profit_split_pct", "min_payout_usd", "payout_cycle_days"):
            self.assertIn(field, rf)

    def test_funded_config_created_with_correct_split(self):
        product = self.enrollment.product
        self.assertEqual(self.funded_config.profit_split_pct, product.profit_split_pct)
