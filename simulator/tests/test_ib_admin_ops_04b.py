# simulator/tests/test_ib_admin_ops_04b.py
"""
IB-ADMIN-OPS-04B

Regression coverage for simulator/ib_admin_ops.py — the IB Ops admin
control plane (directory, detail page, approve/link/sync/reconcile
views, IBCommissionRuleAdmin, IBCommissionObligationAdmin,
LotExecutionEventAdmin).

This suite proves the admin layer NEVER becomes a second economic
engine: money-moving/state-changing actions must delegate to the real,
unmodified IB-TREASURY-CREDIT-03 services
(simulator/ib_treasury_settlement.py), never reimplement them. Several
tests below patch those services and assert they were called with the
right arguments (mirroring test_o3b3_treasury_review_admin_ui.py's own
"prove delegation, not just end state" approach) — exactly because
"don't duplicate logic" is a correctness requirement of this block, not
merely style.

Every IBCommissionObligation/LotExecutionEvent/ReferralAttribution used
here is constructed directly via the ORM, never via a real trading/
deposit event and never via simulator/consumers.py or
simulator/population_engine.py.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerLedger, IBCommissionObligation,
    IBCommissionRule, LedgerEntry, LotExecutionEvent, Referral,
    ReferralAttribution, TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_requests import approve_treasury_request, execute_treasury_request
from simulator.tests.factories import make_account, make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"ibops04b_{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type=IBCommissionRule.RULE_PER_LOT, referral=None,
               fixed_amount=Decimal("10.00"), percentage=None, enabled=True,
               effective_from=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
    )


def _make_lot_event(account, qty="0.50", created_at=None, symbol="EUR/USD"):
    ev = LotExecutionEvent.objects.create(
        account=account, position=None, symbol=symbol, side="BUY",
        qty=Decimal(qty), execution_price=Decimal("1.10000"),
        merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
    )
    if created_at is not None:
        LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
        ev.refresh_from_db()
    return ev


def _make_obligation(referral, rule, calculated_amount="10.00",
                      status=IBCommissionObligation.ST_PENDING, source_event_id=None):
    global _seq
    _seq += 1
    attribution = ReferralAttribution.objects.filter(referral=referral).first()
    if attribution is None:
        attribution = _make_attribution(make_user(), referral)
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
        source_event_type="test_event",
        source_event_id=source_event_id if source_event_id is not None else _seq,
        calculated_amount=Decimal(calculated_amount), currency="USD", status=status,
    )


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


def _directory_url():
    return reverse("admin:ib_directory")


def _detail_url(referral_id):
    return reverse("admin:ib_detail", args=[referral_id])


def _obligation_change_url(pk):
    return reverse("admin:simulator_ibcommissionobligation_change", args=[pk])


def _approve_url(pk):
    return reverse("admin:ib_obligation_approve", args=[pk])


def _link_url(pk):
    return reverse("admin:ib_obligation_link_treasury", args=[pk])


def _sync_url(pk):
    return reverse("admin:ib_obligation_sync", args=[pk])


def _reconcile_url():
    return reverse("admin:ib_reconcile")


# ─────────────────────────────────────────────────────────────────────────
# 1/3/34 — Directory access + permission + query-count bound
# ─────────────────────────────────────────────────────────────────────────

class DirectoryAccessTests(TestCase):
    def test_directory_accessible_to_authorized_staff(self):
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        resp = client.get(_directory_url())
        self.assertEqual(resp.status_code, 200)

    def test_directory_rejects_unauthorized(self):
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        resp = client.get(_directory_url())
        self.assertEqual(resp.status_code, 403)

    def test_directory_rejects_anonymous(self):
        client = Client()
        resp = client.get(_directory_url())
        self.assertIn(resp.status_code, (302, 403))

    def test_directory_query_count_bounded_for_multiple_ibs(self):
        reviewer = _make_reviewer()
        for _ in range(5):
            ib_owner = make_user()
            referral = _make_referral(ib_owner)
            trader = make_user()
            _make_attribution(trader, referral)
            account = make_account(user=trader)
            _make_lot_event(account, qty="1.00")
            _make_rule(referral=referral, fixed_amount=Decimal("7.00"))
        client = Client()
        client.force_login(reviewer)
        # Bounded: a handful of fixed batched-aggregate queries plus at
        # most one resolve_applicable_rule() call per VISIBLE row (5 IBs
        # here, well under IB_DIRECTORY_PAGE_SIZE) — never unbounded
        # per-row Sum()/Count() queries. 40 is a generous ceiling (admin
        # chrome + session/permission checks add overhead) chosen to
        # catch a true N+1 regression (which would scale with IB count
        # far beyond this) without being a brittle exact-count assertion.
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(_directory_url())
        self.assertEqual(resp.status_code, 200)
        self.assertLess(
            len(ctx.captured_queries), 40,
            f"directory page issued {len(ctx.captured_queries)} queries for 5 IBs — possible N+1 regression",
        )


# ─────────────────────────────────────────────────────────────────────────
# 2/3 — Detail access
# ─────────────────────────────────────────────────────────────────────────

class DetailAccessTests(TestCase):
    def test_detail_accessible_to_authorized_staff(self):
        reviewer = _make_reviewer()
        referral = _make_referral()
        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(referral.pk))
        self.assertEqual(resp.status_code, 200)

    def test_detail_rejects_unauthorized(self):
        plain = make_user(is_staff=True)
        referral = _make_referral()
        client = Client()
        client.force_login(plain)
        resp = client.get(_detail_url(referral.pk))
        self.assertEqual(resp.status_code, 403)

    def test_detail_404_for_missing_referral(self):
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(999999))
        self.assertEqual(resp.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────
# 4/5/6/7/8 — Aggregation correctness
# ─────────────────────────────────────────────────────────────────────────

class AggregationTests(TestCase):
    def setUp(self):
        self.ib_owner = make_user()
        self.referral = _make_referral(self.ib_owner)
        self.trader1 = make_user()
        self.trader2 = make_user()
        _make_attribution(self.trader1, self.referral)
        _make_attribution(self.trader2, self.referral)
        self.account1 = make_account(user=self.trader1)
        self.account2 = make_account(user=self.trader2)

    def test_referred_client_count_correct(self):
        from simulator.ib_admin_ops import ib_directory_queryset
        row = ib_directory_queryset().get(pk=self.referral.pk)
        self.assertEqual(row.client_count, 2)

    def test_today_lots_correct(self):
        from simulator.ib_admin_ops import ib_lot_totals
        _make_lot_event(self.account1, qty="0.30")
        _make_lot_event(self.account2, qty="0.20")
        totals = ib_lot_totals(self.referral)
        self.assertEqual(totals["today"], Decimal("0.50"))

    def test_week_lots_correct(self):
        from simulator.ib_admin_ops import ib_lot_totals
        now = timezone.now()
        _make_lot_event(self.account1, qty="1.00", created_at=now - timezone.timedelta(days=2))
        _make_lot_event(self.account1, qty="5.00", created_at=now - timezone.timedelta(days=20))
        totals = ib_lot_totals(self.referral, at_time=now)
        self.assertEqual(totals["week"], Decimal("1.00"))

    def test_month_lots_correct(self):
        from simulator.ib_admin_ops import ib_lot_totals
        now = timezone.now()
        _make_lot_event(self.account1, qty="2.00", created_at=now - timezone.timedelta(days=10))
        _make_lot_event(self.account1, qty="9.00", created_at=now - timezone.timedelta(days=90))
        totals = ib_lot_totals(self.referral, at_time=now)
        self.assertEqual(totals["month"], Decimal("2.00"))

    def test_lifetime_lots_correct(self):
        from simulator.ib_admin_ops import ib_lot_totals
        now = timezone.now()
        _make_lot_event(self.account1, qty="1.00", created_at=now - timezone.timedelta(days=2))
        _make_lot_event(self.account1, qty="9.00", created_at=now - timezone.timedelta(days=400))
        totals = ib_lot_totals(self.referral, at_time=now)
        self.assertEqual(totals["lifetime"], Decimal("10.00"))

    def test_directory_month_lots_matches_detail(self):
        from simulator.ib_admin_ops import ib_directory_queryset, ib_lot_totals
        _make_lot_event(self.account1, qty="3.00")
        row = ib_directory_queryset().get(pk=self.referral.pk)
        detail_totals = ib_lot_totals(self.referral)
        self.assertEqual(row.month_lots, detail_totals["month"])


# ─────────────────────────────────────────────────────────────────────────
# 9/10/11 — Rate display, override precedence, no hardcoded amount
# ─────────────────────────────────────────────────────────────────────────

class RateDisplayTests(TestCase):
    def test_effective_per_lot_rate_displayed_correctly(self):
        from simulator.ib_admin_ops import ib_effective_rate
        referral = _make_referral()
        _make_rule(referral=None, fixed_amount=Decimal("4.00"))  # global
        rule, source = ib_effective_rate(referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(rule.fixed_amount, Decimal("4.00"))
        self.assertEqual(source, "global")

    def test_per_ib_override_beats_global(self):
        from simulator.ib_admin_ops import ib_effective_rate
        referral = _make_referral()
        _make_rule(referral=None, fixed_amount=Decimal("4.00"))
        _make_rule(referral=referral, fixed_amount=Decimal("15.00"))
        rule, source = ib_effective_rate(referral, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(rule.fixed_amount, Decimal("15.00"))
        self.assertEqual(source, "per-IB")

    def test_configurable_rate_no_hardcoded_amount(self):
        # Three different IBs, three different, arbitrary rates — proves
        # nothing in ib_admin_ops.py assumes/hardcodes $4/$10/$15.
        from simulator.ib_admin_ops import ib_effective_rate
        for amount in (Decimal("2.75"), Decimal("31.10"), Decimal("100.00")):
            referral = _make_referral()
            _make_rule(referral=referral, fixed_amount=amount)
            rule, _ = ib_effective_rate(referral, IBCommissionRule.RULE_PER_LOT)
            self.assertEqual(rule.fixed_amount, amount)
        # Structural proof: no literal 4.00/10.00/15.00 Decimal appears
        # anywhere in the module's source as a rate default.
        import inspect
        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops)
        for literal in ('"4.00"', '"10.00"', '"15.00"', "Decimal('4.00')", "Decimal('10.00')", "Decimal('15.00')"):
            self.assertNotIn(literal, source)


# ─────────────────────────────────────────────────────────────────────────
# 12/13/14 — Obligation totals
# ─────────────────────────────────────────────────────────────────────────

class ObligationTotalsTests(TestCase):
    def setUp(self):
        self.ib_owner = make_user()
        self.referral = _make_referral(self.ib_owner)
        self.rule = _make_rule(referral=self.referral)

    def test_pending_totals_correct(self):
        from simulator.ib_admin_ops import ib_obligation_totals
        _make_obligation(self.referral, self.rule, "10.00", IBCommissionObligation.ST_PENDING)
        _make_obligation(self.referral, self.rule, "5.00", IBCommissionObligation.ST_PENDING)
        totals = ib_obligation_totals(self.referral)
        self.assertEqual(totals[IBCommissionObligation.ST_PENDING]["amount"], Decimal("15.00"))
        self.assertEqual(totals[IBCommissionObligation.ST_PENDING]["count"], 2)

    def test_approved_totals_correct(self):
        from simulator.ib_admin_ops import ib_obligation_totals
        _make_obligation(self.referral, self.rule, "20.00", IBCommissionObligation.ST_APPROVED)
        totals = ib_obligation_totals(self.referral)
        self.assertEqual(totals[IBCommissionObligation.ST_APPROVED]["amount"], Decimal("20.00"))
        self.assertEqual(totals[IBCommissionObligation.ST_APPROVED]["count"], 1)

    def test_credited_totals_correct(self):
        from simulator.ib_admin_ops import ib_obligation_totals
        _make_obligation(self.referral, self.rule, "8.00", IBCommissionObligation.ST_CREDITED)
        totals = ib_obligation_totals(self.referral)
        self.assertEqual(totals[IBCommissionObligation.ST_CREDITED]["amount"], Decimal("8.00"))
        self.assertEqual(totals[IBCommissionObligation.ST_CREDITED]["count"], 1)

    def test_directory_totals_match_detail_totals(self):
        from simulator.ib_admin_ops import ib_directory_queryset, ib_obligation_totals
        _make_obligation(self.referral, self.rule, "12.00", IBCommissionObligation.ST_PENDING)
        row = ib_directory_queryset().get(pk=self.referral.pk)
        detail = ib_obligation_totals(self.referral)
        self.assertEqual(row.pending_amount, detail[IBCommissionObligation.ST_PENDING]["amount"])


# ─────────────────────────────────────────────────────────────────────────
# 15/16 — Wallet visibility
# ─────────────────────────────────────────────────────────────────────────

class WalletVisibilityTests(TestCase):
    def test_wallet_balance_displayed_from_wallet(self):
        reviewer = _make_reviewer()
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        wallet, _ = get_or_create_wallet(ib_owner)
        from simulator.wallet_ledger import credit_wallet
        credit_wallet(wallet.id, Decimal("42.50"), WalletTransaction.TX_REBATE, note="test")

        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(referral.pk))
        self.assertContains(resp, "42.50")

    def test_wallet_transaction_history_displayed(self):
        reviewer = _make_reviewer()
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        wallet, _ = get_or_create_wallet(ib_owner)
        from simulator.wallet_ledger import credit_wallet
        credit_wallet(wallet.id, Decimal("17.00"), WalletTransaction.TX_REBATE, note="commission credit")

        client = Client()
        client.force_login(reviewer)
        resp = client.get(_detail_url(referral.pk))
        self.assertContains(resp, "17.00")
        self.assertContains(resp, WalletTransaction.TX_REBATE)


# ─────────────────────────────────────────────────────────────────────────
# 17/18/19 — Obligation immutability
# ─────────────────────────────────────────────────────────────────────────

class ObligationImmutabilityTests(TestCase):
    def test_historical_fields_read_only(self):
        from simulator.ib_admin_ops import IBCommissionObligationAdmin
        model_admin = IBCommissionObligationAdmin(IBCommissionObligation, None)
        readonly = model_admin.readonly_fields
        all_fields = [f.name for f in IBCommissionObligation._meta.fields]
        self.assertEqual(set(readonly), set(all_fields))

    def test_arbitrary_obligation_add_disabled(self):
        from simulator.ib_admin_ops import IBCommissionObligationAdmin
        model_admin = IBCommissionObligationAdmin(IBCommissionObligation, None)
        self.assertFalse(model_admin.has_add_permission(None))

    def test_arbitrary_obligation_delete_disabled(self):
        from simulator.ib_admin_ops import IBCommissionObligationAdmin
        model_admin = IBCommissionObligationAdmin(IBCommissionObligation, None)
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_arbitrary_obligation_change_disabled(self):
        from simulator.ib_admin_ops import IBCommissionObligationAdmin
        model_admin = IBCommissionObligationAdmin(IBCommissionObligation, None)
        self.assertFalse(model_admin.has_change_permission(None))


# ─────────────────────────────────────────────────────────────────────────
# 20/21 — Approval operation
# ─────────────────────────────────────────────────────────────────────────

class ApprovalOperationTests(TestCase):
    def setUp(self):
        self.ib_owner = make_user()
        self.referral = _make_referral(self.ib_owner)
        self.rule = _make_rule(referral=self.referral)
        self.obligation = _make_obligation(self.referral, self.rule, "10.00", IBCommissionObligation.ST_PENDING)

    def test_approval_invokes_authoritative_service(self):
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        with patch("simulator.ib_admin_ops.approve_obligation") as mocked:
            mocked.return_value = self.obligation
            client.post(_approve_url(self.obligation.pk))
            self.assertTrue(mocked.called)
            called_obligation, kwargs = mocked.call_args
            self.assertEqual(called_obligation[0].pk, self.obligation.pk)

    def test_approval_permission_enforced(self):
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        resp = client.post(_approve_url(self.obligation.pk))
        self.assertEqual(resp.status_code, 403)
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, IBCommissionObligation.ST_PENDING)

    def test_approval_end_to_end(self):
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        resp = client.post(_approve_url(self.obligation.pk), follow=True)
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, IBCommissionObligation.ST_APPROVED)


# ─────────────────────────────────────────────────────────────────────────
# 22/23/24 — Treasury-link operation
# ─────────────────────────────────────────────────────────────────────────

class TreasuryLinkOperationTests(TestCase):
    def setUp(self):
        self.ib_owner = make_user()
        self.referral = _make_referral(self.ib_owner)
        self.rule = _make_rule(referral=self.referral)
        self.obligation = _make_obligation(self.referral, self.rule, "10.00", IBCommissionObligation.ST_APPROVED)

    def test_link_invokes_authoritative_service(self):
        submitter = _make_submitter()
        client = Client()
        client.force_login(submitter)
        with patch("simulator.ib_admin_ops.link_treasury_request") as mocked:
            mocked.return_value = TreasuryOperationRequest(pk=1, status=TreasuryOperationRequest.ST_PENDING)
            mocked.return_value.save = lambda *a, **k: None
            client.post(_link_url(self.obligation.pk))
            self.assertTrue(mocked.called)

    def test_submit_permission_enforced(self):
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        resp = client.post(_link_url(self.obligation.pk))
        self.assertEqual(resp.status_code, 403)
        self.obligation.refresh_from_db()
        self.assertIsNone(self.obligation.treasury_operation_id)

    def test_repeated_link_does_not_duplicate_request(self):
        submitter = _make_submitter()
        client = Client()
        client.force_login(submitter)
        client.post(_link_url(self.obligation.pk))
        self.obligation.refresh_from_db()
        first_treasury_id = self.obligation.treasury_operation_id
        self.assertIsNotNone(first_treasury_id)

        client.post(_link_url(self.obligation.pk))
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.treasury_operation_id, first_treasury_id)
        self.assertEqual(
            TreasuryOperationRequest.objects.filter(
                reference=f"IBCommissionObligation #{self.obligation.pk}",
            ).count(),
            1,
        )


# ─────────────────────────────────────────────────────────────────────────
# 25 — IB Ops cannot execute Treasury payment
# ─────────────────────────────────────────────────────────────────────────

class TreasuryExecutionBoundaryTests(TestCase):
    def test_ib_admin_ops_never_calls_execute_treasury_request(self):
        import inspect
        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops)
        self.assertNotIn("execute_treasury_request(", source)
        self.assertNotIn("can_execute_treasury_request", source)

    def test_no_url_exists_to_execute_treasury_from_ib_ops(self):
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        _grant(reviewer, "can_submit_treasury_request")
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_APPROVED)
        client = Client()
        client.force_login(reviewer)
        client.post(_link_url(obligation.pk))
        obligation.refresh_from_db()
        treasury_request = obligation.treasury_operation
        self.assertEqual(treasury_request.status, TreasuryOperationRequest.ST_PENDING)
        # No ib_admin_ops URL name exists for executing — only Treasury's
        # own admin (simulator.admin) exposes that, gated by
        # can_execute_treasury_request, never reachable via this module.
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("admin:ib_obligation_execute", args=[treasury_request.pk])


# ─────────────────────────────────────────────────────────────────────────
# 26 — Treasury terminal failure displayed safely
# ─────────────────────────────────────────────────────────────────────────

class TerminalFailureDisplayTests(TestCase):
    def test_terminal_failure_displayed_not_acted_on(self):
        from simulator.treasury_requests import reject_treasury_request

        reviewer = _make_reviewer()
        submitter = _make_submitter()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_APPROVED)

        client = Client()
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))
        obligation.refresh_from_db()
        treasury_request = obligation.treasury_operation
        reject_treasury_request(treasury_request, "test rejection", request=_fake_request(reviewer))

        client.force_login(reviewer)
        resp = client.get(_detail_url(referral.pk))
        self.assertContains(resp, "needs attention")

        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED, "no auto-cancel/reverse")
        self.assertIsNotNone(obligation.treasury_operation_id, "stays linked for auditability")


def _fake_request(user):
    from django.test import RequestFactory
    request = RequestFactory().post("/")
    request.user = user
    return request


# ─────────────────────────────────────────────────────────────────────────
# 27 — Sync uses authoritative service
# ─────────────────────────────────────────────────────────────────────────

class SyncOperationTests(TestCase):
    def test_sync_uses_authoritative_service(self):
        reviewer = _make_reviewer()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_APPROVED)

        client = Client()
        client.force_login(reviewer)
        with patch("simulator.ib_admin_ops.sync_obligation_from_treasury") as mocked:
            mocked.return_value = {"obligation": obligation, "outcome": "not_linked", "treasury_status": None}
            client.post(_sync_url(obligation.pk))
            self.assertTrue(mocked.called)

    def test_sync_end_to_end_credits(self):
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_APPROVED)

        client = Client()
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))
        obligation.refresh_from_db()
        treasury_request = obligation.treasury_operation
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        client.force_login(reviewer)
        client.post(_sync_url(obligation.pk))
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)


# ─────────────────────────────────────────────────────────────────────────
# 28 — rate change does not mutate historical snapshot
# ─────────────────────────────────────────────────────────────────────────

class SnapshotImmutabilityTests(TestCase):
    def test_rate_change_does_not_mutate_historical_obligation(self):
        referral = _make_referral()
        rule = _make_rule(referral=referral, fixed_amount=Decimal("10.00"))
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_PENDING)

        rule.fixed_amount = Decimal("999.00")
        rule.save(update_fields=["fixed_amount"])

        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, Decimal("10.00"))

    def test_rule_admin_makes_economic_fields_readonly_on_existing_rule(self):
        from simulator.ib_admin_ops import IBCommissionRuleAdmin
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        model_admin = IBCommissionRuleAdmin(IBCommissionRule, None)
        readonly_on_edit = model_admin.get_readonly_fields(None, obj=rule)
        for field in ("rule_type", "referral", "fixed_amount", "percentage", "cpa_trigger_event"):
            self.assertIn(field, readonly_on_edit)

    def test_rule_admin_allows_new_rule_creation(self):
        from simulator.ib_admin_ops import IBCommissionRuleAdmin
        model_admin = IBCommissionRuleAdmin(IBCommissionRule, None)
        readonly_on_add = model_admin.get_readonly_fields(None, obj=None)
        self.assertNotIn("fixed_amount", readonly_on_add)
        self.assertNotIn("rule_type", readonly_on_add)


# ─────────────────────────────────────────────────────────────────────────
# 29/30 — no money movement from display/admin operations
# ─────────────────────────────────────────────────────────────────────────

class NoMoneyMovementTests(TestCase):
    def test_no_wallet_mutation_from_display(self):
        reviewer = _make_reviewer()
        referral = _make_referral()
        wallet, _ = get_or_create_wallet(referral.user)
        balance_before = wallet.available_balance

        client = Client()
        client.force_login(reviewer)
        client.get(_directory_url())
        client.get(_detail_url(referral.pk))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

    def test_no_wallet_mutation_from_approve_and_link(self):
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_PENDING)
        wallet, _ = get_or_create_wallet(referral.user)
        balance_before = wallet.available_balance

        client = Client()
        client.force_login(reviewer)
        client.post(_approve_url(obligation.pk))
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

    def test_no_ledger_entry_or_broker_ledger_mutation_from_admin_operations(self):
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_PENDING)

        ledger_before = LedgerEntry.objects.count()
        broker_ledger_before = BrokerLedger.objects.count()

        client = Client()
        client.force_login(reviewer)
        client.post(_approve_url(obligation.pk))
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))

        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        self.assertEqual(BrokerLedger.objects.count(), broker_ledger_before)


# ─────────────────────────────────────────────────────────────────────────
# 31/32/33 — engine protection
# ─────────────────────────────────────────────────────────────────────────

class EngineProtectionTests(TestCase):
    def test_no_trading_engine_mutation(self):
        from simulator.models import Position, Trade
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_PENDING)
        pos_before = Position.objects.count()
        trade_before = Trade.objects.count()

        client = Client()
        client.force_login(reviewer)
        client.post(_approve_url(obligation.pk))
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))

        self.assertEqual(Position.objects.count(), pos_before)
        self.assertEqual(Trade.objects.count(), trade_before)

    def test_no_commission_recalculation_in_admin(self):
        # Structural: ib_admin_ops.py must never import IBCommissionRule
        # economic-calculation helpers other than the read-only
        # resolve_applicable_rule() resolver, and must never construct
        # its own commission formula.
        import inspect
        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops)
        self.assertNotIn("* rule.percentage", source)
        self.assertNotIn("* rule.fixed_amount", source)

    def test_no_consumer_or_population_engine_dependency(self):
        import inspect
        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops)
        self.assertNotIn("from .consumers", source)
        self.assertNotIn("from .population_engine", source)
        self.assertNotIn("import consumers", source)
        self.assertNotIn("import population_engine", source)


# ─────────────────────────────────────────────────────────────────────────
# 35 — audit event generated for admin financial transitions
# ─────────────────────────────────────────────────────────────────────────

class AuditabilityTests(TestCase):
    def test_audit_event_generated_for_approval(self):
        reviewer = _make_reviewer()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_PENDING)

        client = Client()
        client.force_login(reviewer)
        client.post(_approve_url(obligation.pk))

        self.assertTrue(
            AuditLog.objects.filter(event_type="ib_admin.obligation_approved").exists(),
        )
        self.assertTrue(
            BrokerAuditEvent.objects.filter(event_type="ib_admin.obligation_approved").exists(),
        )

    def test_audit_event_generated_for_treasury_link(self):
        submitter = _make_submitter()
        referral = _make_referral()
        rule = _make_rule(referral=referral)
        obligation = _make_obligation(referral, rule, "10.00", IBCommissionObligation.ST_APPROVED)

        client = Client()
        client.force_login(submitter)
        client.post(_link_url(obligation.pk))

        self.assertTrue(
            AuditLog.objects.filter(event_type="ib_admin.treasury_request_linked").exists(),
        )

    def test_audit_event_generated_for_rule_creation(self):
        reviewer = _make_reviewer()
        _grant(reviewer, "add_ibcommissionrule")
        referral = _make_referral()
        client = Client()
        client.force_login(reviewer)
        add_url = reverse("admin:simulator_ibcommissionrule_add")
        resp = client.post(add_url, {
            "rule_type": IBCommissionRule.RULE_PER_LOT,
            "referral": referral.pk,
            "enabled": "on",
            "fixed_amount": "6.50",
            "effective_from_0": timezone.now().date().isoformat(),
            "effective_from_1": "00:00:00",
        }, follow=True)
        self.assertTrue(
            AuditLog.objects.filter(event_type="ib_admin.rule_created").exists(),
            f"response status={resp.status_code}",
        )
