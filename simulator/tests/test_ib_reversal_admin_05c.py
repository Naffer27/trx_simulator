# simulator/tests/test_ib_reversal_admin_05c.py
"""
IB-REVERSALS-FRAUD-05C

Regression coverage for simulator/ib_reversal_admin.py — the admin/
operational layer making IB-REVERSALS-FRAUD-05B's reversal/adjustment
services usable by staff. Also covers the small, additive extensions to
simulator/ib_admin_ops.py (Recent Adjustments + remaining_reversible on
the IB detail page and the obligation's own change_form) and the new
Celery task in simulator/tasks.py.

This suite proves the admin layer NEVER duplicates 05B's financial
logic: every mutating view is proven to delegate to the real,
unmodified ib_commission_reversal.py service (via unittest.mock.patch,
asserting called-with-correct-args — mirroring
test_ib_admin_ops_04b.py's own "prove delegation, not just end state"
approach), and a full, real, unmocked end-to-end flow through actual
Treasury execution proves the exact wallet delta.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.ib_commission_reversal import (
    approve_adjustment, link_adjustment_to_treasury, submit_adjustment,
    sync_adjustment_from_treasury,
)
from simulator.ib_treasury_settlement import approve_obligation, link_treasury_request
from simulator.models import (
    BrokerLedger, IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule,
    LedgerEntry, LotExecutionEvent, Position, Referral, ReferralAttribution, Trade,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.tasks import reconcile_ib_commission_adjustments_task
from simulator.treasury_requests import approve_treasury_request, execute_treasury_request
from simulator.tests.factories import make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers — duplicated locally (no cross-test-file import), same shape
# already established throughout this whole IB test suite.
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"revadmin05c_{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(referral=None, fixed_amount=Decimal("10.00")):
    return IBCommissionRule.objects.create(
        rule_type=IBCommissionRule.RULE_PER_LOT, referral=referral, enabled=True,
        fixed_amount=fixed_amount, percentage=None,
        effective_from=timezone.now() - timezone.timedelta(minutes=5),
    )


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _make_credited_obligation(calculated_amount="40.00"):
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    rule = _make_rule(referral=referral)

    attribution = ReferralAttribution.objects.get(referral=referral)
    obligation = IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
        source_event_type="test_event", source_event_id=_next_seq(),
        calculated_amount=Decimal(calculated_amount), currency="USD",
        status=IBCommissionObligation.ST_PENDING,
    )

    reviewer = _make_reviewer()
    submitter = _make_submitter()
    executor = _make_executor()

    obligation = approve_obligation(obligation, request=_fake_request(reviewer))
    treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
    approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    execute_treasury_request(treasury_request, request=_fake_request(executor))
    from simulator.ib_treasury_settlement import sync_obligation_from_treasury
    result = sync_obligation_from_treasury(obligation)
    obligation = result["obligation"]
    assert obligation.status == IBCommissionObligation.ST_CREDITED

    return {"ib_owner": ib_owner, "referral": referral, "trader": trader, "obligation": obligation}


def _grant(user, codename):
    perm = Permission.objects.get(codename=codename)
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _make_submitter(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_submit_treasury_request")


def _make_executor(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_execute_treasury_request")


def _fake_request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _submit_url(obligation_id):
    return reverse("admin:ib_adjustment_submit", args=[obligation_id])


def _approve_url(pk):
    return reverse("admin:ib_adjustment_approve", args=[pk])


def _reject_url(pk):
    return reverse("admin:ib_adjustment_reject", args=[pk])


def _link_url(pk):
    return reverse("admin:ib_adjustment_link_treasury", args=[pk])


def _sync_url(pk):
    return reverse("admin:ib_adjustment_sync", args=[pk])


def _detail_url(referral_id):
    return reverse("admin:ib_detail", args=[referral_id])


def _adjustment_change_url(pk):
    return reverse("admin:simulator_ibcommissionadjustment_change", args=[pk])


def _make_pending_adjustment(ctx, submitter, amount="10.00"):
    return submit_adjustment(
        ctx["obligation"], amount=Decimal(amount), reason="test reason",
        request=_fake_request(submitter),
    )


# ─────────────────────────────────────────────────────────────────────────
# Permissions — one per action
# ─────────────────────────────────────────────────────────────────────────

class PermissionTests(TestCase):
    def test_submit_requires_submit_permission(self):
        ctx = _make_credited_obligation()
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        resp = client.post(_submit_url(ctx["obligation"].pk), {"amount": "10.00", "reason": "t"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(IBCommissionAdjustment.objects.count(), 0)

    def test_approve_requires_review_permission(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        plain = make_user(is_staff=True)
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(plain)
        resp = client.post(_approve_url(adj.pk))
        self.assertEqual(resp.status_code, 403)
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)

    def test_reject_requires_review_permission(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        plain = make_user(is_staff=True)
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(plain)
        resp = client.post(_reject_url(adj.pk), {"rejection_reason": "no"})
        self.assertEqual(resp.status_code, 403)

    def test_link_requires_submit_permission(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        plain = make_user(is_staff=True)
        adj = _make_pending_adjustment(ctx, submitter)
        approve_adjustment(adj, request=_fake_request(reviewer))
        client = Client()
        client.force_login(plain)
        resp = client.post(_link_url(adj.pk))
        self.assertEqual(resp.status_code, 403)
        adj.refresh_from_db()
        self.assertIsNone(adj.treasury_operation_id)

    def test_sync_requires_view_permission(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        adj = _make_pending_adjustment(ctx, submitter)
        anonymous_staff = make_user(is_staff=True)  # no Treasury perms at all
        client = Client()
        client.force_login(anonymous_staff)
        resp = client.post(_sync_url(adj.pk))
        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────
# GET moves no money
# ─────────────────────────────────────────────────────────────────────────

class GetNoMoneyMovementTests(TestCase):
    def test_get_submit_form_moves_no_money(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        client = Client()
        client.force_login(submitter)
        resp = client.get(_submit_url(ctx["obligation"].pk))
        self.assertEqual(resp.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(IBCommissionAdjustment.objects.count(), 0)

    def test_get_approve_reject_link_moves_no_money(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance

        client = Client()
        client.force_login(reviewer)
        client.get(_approve_url(adj.pk))
        client.get(_reject_url(adj.pk))
        client.force_login(submitter)
        client.get(_link_url(adj.pk))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING, "GET must never transition state")


# ─────────────────────────────────────────────────────────────────────────
# POST delegates to the correct service — mock-and-assert-called
# ─────────────────────────────────────────────────────────────────────────

class DelegationTests(TestCase):
    def test_submit_view_delegates_to_submit_adjustment(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        client = Client()
        client.force_login(submitter)
        with patch("simulator.ib_reversal_admin.submit_adjustment") as mocked:
            mocked.return_value = IBCommissionAdjustment(pk=1, obligation=ctx["obligation"])
            client.post(_submit_url(ctx["obligation"].pk), {
                "amount": "10.00", "reason": "t", "adjustment_type": "REVERSAL",
            })
            self.assertTrue(mocked.called)
            _, kwargs = mocked.call_args
            self.assertEqual(kwargs["amount"], Decimal("10.00"))
            self.assertEqual(kwargs["reason"], "t")

    def test_approve_view_delegates_to_approve_adjustment(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(reviewer)
        with patch("simulator.ib_reversal_admin.approve_adjustment") as mocked:
            mocked.return_value = adj
            client.post(_approve_url(adj.pk))
            self.assertTrue(mocked.called)
            called_adj, _ = mocked.call_args[0], mocked.call_args[1]
            self.assertEqual(mocked.call_args[0][0].pk, adj.pk)

    def test_reject_view_delegates_to_reject_adjustment(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(reviewer)
        with patch("simulator.ib_reversal_admin.reject_adjustment") as mocked:
            mocked.return_value = adj
            client.post(_reject_url(adj.pk), {"rejection_reason": "not valid"})
            self.assertTrue(mocked.called)
            self.assertEqual(mocked.call_args[0][1], "not valid")

    def test_link_view_delegates_to_link_adjustment_to_treasury(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        approve_adjustment(adj, request=_fake_request(reviewer))
        client = Client()
        client.force_login(submitter)
        with patch("simulator.ib_reversal_admin.link_adjustment_to_treasury") as mocked:
            mocked.return_value = TreasuryOperationRequest(pk=999, status=TreasuryOperationRequest.ST_PENDING)
            client.post(_link_url(adj.pk))
            self.assertTrue(mocked.called)
            self.assertEqual(mocked.call_args[0][0].pk, adj.pk)

    def test_sync_view_delegates_to_sync_adjustment_from_treasury(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(submitter)
        with patch("simulator.ib_reversal_admin.sync_adjustment_from_treasury") as mocked:
            mocked.return_value = {"adjustment": adj, "outcome": "not_approved", "treasury_status": None}
            client.post(_sync_url(adj.pk))
            self.assertTrue(mocked.called)
            self.assertEqual(mocked.call_args[0][0].pk, adj.pk)


# ─────────────────────────────────────────────────────────────────────────
# Double-click / retry safety
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyViewTests(TestCase):
    def test_double_click_approve_no_double_transition(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        client = Client()
        client.force_login(reviewer)
        r1 = client.post(_approve_url(adj.pk), follow=True)
        r2 = client.post(_approve_url(adj.pk), follow=True)
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_APPROVED)
        self.assertContains(r2, "no longer PENDING")

    def test_double_click_link_no_duplicate_treasury_request(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter)
        approve_adjustment(adj, request=_fake_request(reviewer))
        client = Client()
        client.force_login(submitter)
        client.post(_link_url(adj.pk))
        client.post(_link_url(adj.pk))
        self.assertEqual(
            TreasuryOperationRequest.objects.filter(reference=f"IBCommissionAdjustment #{adj.pk}").count(), 1,
        )

    def test_double_click_sync_no_duplicate_money(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = _make_pending_adjustment(ctx, submitter)
        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        client = Client()
        client.force_login(submitter)
        client.post(_sync_url(adj.pk))
        balance_after_first_sync = wallet.available_balance
        wallet.refresh_from_db()
        client.post(_sync_url(adj.pk))
        client.post(_sync_url(adj.pk))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_after_first_sync)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────
# Adjustment visible in IB detail + remaining_reversible display
# ─────────────────────────────────────────────────────────────────────────

class DetailPageTests(TestCase):
    def test_adjustment_visible_in_ib_detail(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = _make_pending_adjustment(ctx, submitter, amount="10.00")

        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(ctx["referral"].pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{adj.pk}")

    def test_remaining_reversible_correct_in_detail(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        _make_pending_adjustment(ctx, submitter, amount="10.00")

        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(ctx["referral"].pk))
        self.assertContains(resp, "30.00")

    def test_remaining_reversible_shown_on_obligation_change_page(self):
        ctx = _make_credited_obligation("40.00")
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        resp = client.get(reverse("admin:simulator_ibcommissionobligation_change", args=[ctx["obligation"].pk]))
        self.assertContains(resp, "Remaining Reversible")
        self.assertContains(resp, "40.00")

    def test_submit_button_visible_on_credited_obligation(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        client = Client()
        client.force_login(submitter)
        resp = client.get(reverse("admin:simulator_ibcommissionobligation_change", args=[ctx["obligation"].pk]))
        self.assertContains(resp, "Submit Adjustment")


# ─────────────────────────────────────────────────────────────────────────
# Celery reconciliation task
# ─────────────────────────────────────────────────────────────────────────

class CeleryTaskTests(TestCase):
    def test_reconciliation_task_executes_and_syncs(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = _make_pending_adjustment(ctx, submitter, amount="10.00")
        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        result = reconcile_ib_commission_adjustments_task.apply().get()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["executed"], 1)
        self.assertIn("elapsed_ms", result)

        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_EXECUTED)

    def test_reconciliation_task_scheduled_by_06b(self):
        """
        IB-TREASURY-RECONCILIATION-06B.1 — alignment update.

        05C (this file's own original scope) deliberately left
        reconcile_ib_commission_adjustments UNSCHEDULED — the task
        existed and was fully callable, but no Beat entry referenced it
        (settings.py was explicitly out of scope for 05C). This test
        originally asserted exactly that absence.

        IB-TREASURY-RECONCILIATION-06B subsequently audited both
        reconciliation tasks (06A) and, once proven safe, authorized
        connecting them to Celery Beat (06B) — including this one. The
        original "must NOT be scheduled" assertion is now permanently
        obsolete, not a regression: 06B's settings.py change was itself
        an authorized, reviewed change, not an accident this test should
        keep guarding against.

        This test now protects the OPPOSITE, currently-correct
        invariant: the "reconcile-ib-commission-adjustments-5m" Beat
        entry exists and points at exactly the right task, schedule, and
        options — i.e. it guards the 06B integration going forward,
        rather than the pre-06B absence.
        """
        from celery.schedules import crontab
        from django.conf import settings

        beat_schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
        self.assertIn("reconcile-ib-commission-adjustments-5m", beat_schedule)

        entry = beat_schedule["reconcile-ib-commission-adjustments-5m"]
        self.assertEqual(entry["task"], "simulator.reconcile_ib_commission_adjustments")
        self.assertEqual(str(entry["schedule"]), str(crontab(minute="*/5")))
        self.assertEqual(entry["options"], {"expires": 4 * 60})
        self.assertNotIn("args", entry)
        self.assertNotIn("kwargs", entry)


# ─────────────────────────────────────────────────────────────────────────
# Full E2E flow through real, unmocked Treasury execution
# ─────────────────────────────────────────────────────────────────────────

class EndToEndTests(TestCase):
    def test_full_admin_driven_flow_wallet_delta_exact(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance

        client = Client()

        # 1. Submit via admin view
        client.force_login(submitter)
        resp = client.post(_submit_url(ctx["obligation"].pk), {
            "amount": "15.00", "reason": "E2E test reversal", "adjustment_type": "REVERSAL",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        adj = IBCommissionAdjustment.objects.get(obligation=ctx["obligation"])
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)

        # 2. Approve via admin view
        client.force_login(reviewer)
        client.post(_approve_url(adj.pk))
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_APPROVED)

        # 3. Link to Treasury via admin view
        client.force_login(submitter)
        client.post(_link_url(adj.pk))
        adj.refresh_from_db()
        self.assertIsNotNone(adj.treasury_operation_id)
        treasury_request = adj.treasury_operation
        self.assertEqual(treasury_request.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(treasury_request.amount, Decimal("15.00"))
        self.assertEqual(treasury_request.operation_type, TreasuryOperationRequest.OP_MANUAL_DEBIT)

        # 4. Treasury's own approve/execute — real, unmodified, exercised
        #    directly (not through IB Ops admin — IB Ops never executes
        #    Treasury itself).
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        # 5. Sync via admin view
        client.force_login(submitter)
        client.post(_sync_url(adj.pk))
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_EXECUTED)

        # Exact wallet delta
        wallet.refresh_from_db()
        self.assertEqual(balance_before - wallet.available_balance, Decimal("15.00"))

        # Original obligation untouched
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_CREDITED)
        self.assertEqual(ctx["obligation"].calculated_amount, Decimal("40.00"))


# ─────────────────────────────────────────────────────────────────────────
# No trading-engine mutation / no monetary duplication
# ─────────────────────────────────────────────────────────────────────────

class NoSideEffectTests(TestCase):
    def test_no_trading_engine_mutation(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        pos_before = Position.objects.count()
        trade_before = Trade.objects.count()
        lee_before = LotExecutionEvent.objects.count()

        client = Client()
        client.force_login(submitter)
        client.post(_submit_url(ctx["obligation"].pk), {"amount": "10.00", "reason": "t", "adjustment_type": "REVERSAL"})
        adj = IBCommissionAdjustment.objects.get(obligation=ctx["obligation"])
        client.force_login(reviewer)
        client.post(_approve_url(adj.pk))
        client.force_login(submitter)
        client.post(_link_url(adj.pk))
        adj.refresh_from_db()
        treasury_request = TreasuryOperationRequest.objects.get(pk=adj.treasury_operation_id)
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))
        client.post(_sync_url(adj.pk))

        self.assertEqual(Position.objects.count(), pos_before)
        self.assertEqual(Trade.objects.count(), trade_before)
        self.assertEqual(LotExecutionEvent.objects.count(), lee_before)

    def test_no_broker_ledger_or_ledger_entry_from_admin(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        broker_ledger_before = BrokerLedger.objects.count()
        ledger_entry_before = LedgerEntry.objects.count()

        adj = _make_pending_adjustment(ctx, submitter, amount="10.00")
        client = Client()
        client.force_login(reviewer)
        client.post(_approve_url(adj.pk))
        client.force_login(submitter)
        client.post(_link_url(adj.pk))
        adj.refresh_from_db()
        approve_treasury_request(adj.treasury_operation, request=_fake_request(reviewer))
        execute_treasury_request(adj.treasury_operation, request=_fake_request(executor))
        client.post(_sync_url(adj.pk))

        self.assertEqual(BrokerLedger.objects.count(), broker_ledger_before)
        self.assertEqual(LedgerEntry.objects.count(), ledger_entry_before)

    def test_no_direct_service_module_mutation(self):
        # Structural: ib_reversal_admin.py must never construct
        # WalletTransaction/LedgerEntry/BrokerLedger directly, never
        # mutate Wallet.available_balance directly, and never call
        # Treasury's own execute/approve functions itself.
        import inspect
        from simulator import ib_reversal_admin
        source = inspect.getsource(ib_reversal_admin)
        self.assertNotIn("WalletTransaction.objects.create", source)
        self.assertNotIn("LedgerEntry.objects.create", source)
        self.assertNotIn("BrokerLedger.objects.create", source)
        self.assertNotIn(".available_balance =", source)
        self.assertNotIn("execute_treasury_request(", source)
        self.assertNotIn("approve_treasury_request(", source)
        self.assertNotIn("from .consumers", source)
        self.assertNotIn("from .population_engine", source)
