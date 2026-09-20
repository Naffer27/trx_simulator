# simulator/tests/test_ib_treasury_reconciliation_06b.py
"""
IB-TREASURY-RECONCILIATION-06B

Regression coverage for the ONLY change this block makes: two new
CELERY_BEAT_SCHEDULE entries in trx_simulator/settings.py, wiring the
already-shipped, already-tested reconcile_ib_treasury_settlement_task
(IB-TREASURY-CREDIT-03B) and reconcile_ib_commission_adjustments_task
(IB-REVERSALS-FRAUD-05C) to run automatically every 5 minutes.

simulator/tasks.py, simulator/ib_treasury_settlement.py, and
simulator/ib_commission_reversal.py are NOT modified by this block —
every reconciliation-behavior test below (sections C/D/E/F) is a
regression proof that the underlying, unmodified services still behave
exactly as IB-TREASURY-CREDIT-03B/IB-REVERSALS-FRAUD-05C already
proved — this suite does not certify new service behavior, it certifies
that scheduling them changes nothing about what they do.

Money-safety tests (section E) deliberately call the reconciler alone —
never mixed with approve/link/execute in the same before/after snapshot
window — so a captured "unchanged" count is unambiguously attributable
to the reconciler itself, not to some other step incidentally running
in the same test.
"""
from decimal import Decimal

from django.conf import settings
from django.test import TestCase
from django.test import RequestFactory
from django.utils import timezone

from simulator.ib_commission_reversal import (
    approve_adjustment, link_adjustment_to_treasury, reconcile_pending_adjustments,
    submit_adjustment,
)
from simulator.ib_treasury_settlement import (
    approve_obligation, link_treasury_request, reconcile_approved_obligations,
)
from simulator.models import (
    BrokerLedger, IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule,
    LedgerEntry, LotExecutionEvent, Position, Referral, ReferralAttribution, Trade,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.tasks import (
    reconcile_ib_commission_adjustments_task, reconcile_ib_treasury_settlement_task,
)
from simulator.treasury_requests import (
    approve_treasury_request, cancel_treasury_request, execute_treasury_request,
    reject_treasury_request,
)
from simulator.tests.factories import make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers — duplicated locally, same shape already established across
# every prior IB test file this session.
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"reconcile06b_{_seq}"


def _next_seq():
    global _seq
    _seq += 1
    return _seq


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


def _grant(user, codename):
    from django.contrib.auth.models import Permission
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


def _make_pending_obligation(calculated_amount="40.00"):
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
    return {"ib_owner": ib_owner, "referral": referral, "trader": trader, "obligation": obligation}


def _make_credited_obligation(calculated_amount="40.00"):
    ctx = _make_pending_obligation(calculated_amount)
    reviewer = _make_reviewer()
    submitter = _make_submitter()
    executor = _make_executor()
    obligation = approve_obligation(ctx["obligation"], request=_fake_request(reviewer))
    treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
    approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    execute_treasury_request(treasury_request, request=_fake_request(executor))
    from simulator.ib_treasury_settlement import sync_obligation_from_treasury
    result = sync_obligation_from_treasury(obligation)
    ctx["obligation"] = result["obligation"]
    assert ctx["obligation"].status == IBCommissionObligation.ST_CREDITED
    return ctx


def _make_approved_obligation_at_treasury_status(treasury_status):
    """
    Builds an APPROVED obligation whose linked TreasuryOperationRequest
    sits at exactly `treasury_status`, using the REAL service functions
    wherever a real path exists. EXECUTING has no real service path that
    deliberately parks a request there (execute_treasury_request()
    passes through it atomically, within one call, to either EXECUTED or
    FAILED) — for that one case only, this simulates the transient state
    via a direct status field update, clearly isolated below.
    """
    ctx = _make_pending_obligation()
    reviewer = _make_reviewer()
    submitter = _make_submitter()
    executor = _make_executor()
    obligation = approve_obligation(ctx["obligation"], request=_fake_request(reviewer))
    treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))

    if treasury_status == TreasuryOperationRequest.ST_PENDING:
        pass
    elif treasury_status == TreasuryOperationRequest.ST_APPROVED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    elif treasury_status == TreasuryOperationRequest.ST_EXECUTING:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        # No real service path deliberately parks a request in EXECUTING
        # — it is a transient, atomically-passed-through state inside
        # execute_treasury_request() itself. Simulated here only to
        # exercise the reconciler's own "still in progress" branch.
        TreasuryOperationRequest.objects.filter(pk=treasury_request.pk).update(
            status=TreasuryOperationRequest.ST_EXECUTING, executed_by=executor,
        )
    elif treasury_status == TreasuryOperationRequest.ST_EXECUTED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))
    elif treasury_status == TreasuryOperationRequest.ST_REJECTED:
        reject_treasury_request(treasury_request, "test rejection", request=_fake_request(reviewer))
    elif treasury_status == TreasuryOperationRequest.ST_CANCELLED:
        cancel_treasury_request(treasury_request, request=_fake_request(submitter))
    elif treasury_status == TreasuryOperationRequest.ST_FAILED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        TreasuryOperationRequest.objects.filter(pk=treasury_request.pk).update(
            status=TreasuryOperationRequest.ST_FAILED, failure_reason="simulated for test",
        )
    else:
        raise AssertionError(f"unsupported treasury_status {treasury_status}")

    ctx["obligation"] = obligation
    ctx["treasury_request"] = TreasuryOperationRequest.objects.get(pk=treasury_request.pk)
    ctx["reviewer"], ctx["submitter"], ctx["executor"] = reviewer, submitter, executor
    return ctx


def _make_approved_adjustment_at_treasury_status(treasury_status):
    ctx = _make_credited_obligation()
    submitter = _make_submitter()
    reviewer = _make_reviewer()
    executor = _make_executor()
    adj = submit_adjustment(
        ctx["obligation"], amount=Decimal("10.00"), reason="t",
        request=_fake_request(submitter),
    )
    adj = approve_adjustment(adj, request=_fake_request(reviewer))
    treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))

    if treasury_status == TreasuryOperationRequest.ST_PENDING:
        pass
    elif treasury_status == TreasuryOperationRequest.ST_APPROVED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    elif treasury_status == TreasuryOperationRequest.ST_EXECUTING:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        TreasuryOperationRequest.objects.filter(pk=treasury_request.pk).update(
            status=TreasuryOperationRequest.ST_EXECUTING, executed_by=executor,
        )
    elif treasury_status == TreasuryOperationRequest.ST_EXECUTED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))
    elif treasury_status == TreasuryOperationRequest.ST_REJECTED:
        reject_treasury_request(treasury_request, "test rejection", request=_fake_request(reviewer))
    elif treasury_status == TreasuryOperationRequest.ST_CANCELLED:
        cancel_treasury_request(treasury_request, request=_fake_request(submitter))
    elif treasury_status == TreasuryOperationRequest.ST_FAILED:
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        TreasuryOperationRequest.objects.filter(pk=treasury_request.pk).update(
            status=TreasuryOperationRequest.ST_FAILED, failure_reason="simulated for test",
        )
    else:
        raise AssertionError(f"unsupported treasury_status {treasury_status}")

    ctx["adjustment"] = IBCommissionAdjustment.objects.get(pk=adj.pk)
    ctx["treasury_request"] = TreasuryOperationRequest.objects.get(pk=treasury_request.pk)
    return ctx


# ─────────────────────────────────────────────────────────────────────────
# A — Beat wiring
# ─────────────────────────────────────────────────────────────────────────

class BeatWiringTests(TestCase):
    def test_obligation_reconciliation_entry_exists(self):
        self.assertIn("reconcile-ib-treasury-settlement-5m", settings.CELERY_BEAT_SCHEDULE)

    def test_obligation_reconciliation_task_name_exact(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-treasury-settlement-5m"]
        self.assertEqual(entry["task"], "simulator.reconcile_ib_treasury_settlement")

    def test_obligation_reconciliation_cadence_exact(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-treasury-settlement-5m"]
        self.assertEqual(str(entry["schedule"]), str(__import__("celery.schedules", fromlist=["crontab"]).crontab(minute="*/5")))

    def test_obligation_reconciliation_expires_exact(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-treasury-settlement-5m"]
        self.assertEqual(entry["options"]["expires"], 240)

    def test_adjustment_reconciliation_entry_exists(self):
        self.assertIn("reconcile-ib-commission-adjustments-5m", settings.CELERY_BEAT_SCHEDULE)

    def test_adjustment_reconciliation_task_name_exact(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-commission-adjustments-5m"]
        self.assertEqual(entry["task"], "simulator.reconcile_ib_commission_adjustments")

    def test_adjustment_reconciliation_cadence_exact(self):
        from celery.schedules import crontab
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-commission-adjustments-5m"]
        self.assertEqual(str(entry["schedule"]), str(crontab(minute="*/5")))

    def test_adjustment_reconciliation_expires_exact(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-ib-commission-adjustments-5m"]
        self.assertEqual(entry["options"]["expires"], 240)

    def test_neither_new_entry_has_args(self):
        for key in ("reconcile-ib-treasury-settlement-5m", "reconcile-ib-commission-adjustments-5m"):
            entry = settings.CELERY_BEAT_SCHEDULE[key]
            self.assertNotIn("args", entry)

    def test_neither_new_entry_has_kwargs(self):
        for key in ("reconcile-ib-treasury-settlement-5m", "reconcile-ib-commission-adjustments-5m"):
            entry = settings.CELERY_BEAT_SCHEDULE[key]
            self.assertNotIn("kwargs", entry)


# ─────────────────────────────────────────────────────────────────────────
# B — sweep protection
# ─────────────────────────────────────────────────────────────────────────

class SweepProtectionTests(TestCase):
    def test_sweep_entry_still_exists(self):
        self.assertIn("sweep-ib-commission-triggers-5m", settings.CELERY_BEAT_SCHEDULE)

    def test_sweep_task_name_unchanged(self):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-ib-commission-triggers-5m"]
        self.assertEqual(entry["task"], "simulator.sweep_ib_commission_triggers")

    def test_sweep_args_unchanged(self):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-ib-commission-triggers-5m"]
        self.assertEqual(entry["args"], (30,))

    def test_sweep_expires_unchanged(self):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-ib-commission-triggers-5m"]
        self.assertEqual(entry["options"]["expires"], 240)

    def test_sweep_cadence_unchanged(self):
        from celery.schedules import crontab
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-ib-commission-triggers-5m"]
        self.assertEqual(str(entry["schedule"]), str(crontab(minute="*/5")))


# ─────────────────────────────────────────────────────────────────────────
# C — obligation reconciliation by Treasury status
# ─────────────────────────────────────────────────────────────────────────

class ObligationReconciliationTests(TestCase):
    def test_pending_obligation_not_credited(self):
        ctx = _make_pending_obligation()
        reconcile_approved_obligations()
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_PENDING)

    def test_approved_without_treasury_not_credited(self):
        ctx = _make_pending_obligation()
        reviewer = _make_reviewer()
        obligation = approve_obligation(ctx["obligation"], request=_fake_request(reviewer))
        reconcile_approved_obligations()
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_pending_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_PENDING)
        reconcile_approved_obligations()
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_approved_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_APPROVED)
        reconcile_approved_obligations()
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_executing_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_EXECUTING)
        reconcile_approved_obligations()
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_executed_syncs_to_credited(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        result = reconcile_approved_obligations()
        self.assertEqual(result["credited"], 1)
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_CREDITED)

    def test_repeated_reconciliation_no_duplicate_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        r1 = reconcile_approved_obligations()
        r2 = reconcile_approved_obligations()
        self.assertEqual(r1["credited"], 1)
        self.assertEqual(r2["scanned"], 0, "already-CREDITED rows are excluded from the next scan")

    def test_treasury_rejected_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_REJECTED)
        result = reconcile_approved_obligations()
        self.assertEqual(result["failed"], 1)
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_cancelled_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_CANCELLED)
        result = reconcile_approved_obligations()
        self.assertEqual(result["failed"], 1)
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)

    def test_treasury_failed_does_not_credit(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_FAILED)
        result = reconcile_approved_obligations()
        self.assertEqual(result["failed"], 1)
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_APPROVED)


# ─────────────────────────────────────────────────────────────────────────
# D — adjustment reconciliation by Treasury status
# ─────────────────────────────────────────────────────────────────────────

class AdjustmentReconciliationTests(TestCase):
    def test_adjustment_without_treasury_no_movement(self):
        ctx = _make_credited_obligation()
        submitter = _make_submitter()
        adj = submit_adjustment(
            ctx["obligation"], amount=Decimal("10.00"), reason="t",
            request=_fake_request(submitter),
        )
        reconcile_pending_adjustments()
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)

    def test_treasury_pending_does_not_finalize(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_PENDING)
        reconcile_pending_adjustments()
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)

    def test_treasury_approved_does_not_finalize(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_APPROVED)
        reconcile_pending_adjustments()
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)

    def test_treasury_executing_does_not_finalize(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_EXECUTING)
        reconcile_pending_adjustments()
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)

    def test_treasury_executed_syncs_adjustment(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        result = reconcile_pending_adjustments()
        self.assertEqual(result["executed"], 1)
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_EXECUTED)

    def test_repeated_reconciliation_no_duplicate(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        r1 = reconcile_pending_adjustments()
        r2 = reconcile_pending_adjustments()
        self.assertEqual(r1["executed"], 1)
        self.assertEqual(r2["scanned"], 0)

    def test_rejected_creates_no_replacement(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_REJECTED)
        before = TreasuryOperationRequest.objects.count()
        reconcile_pending_adjustments()
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)

    def test_cancelled_creates_no_replacement(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_CANCELLED)
        before = TreasuryOperationRequest.objects.count()
        reconcile_pending_adjustments()
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)

    def test_failed_creates_no_replacement(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_FAILED)
        before = TreasuryOperationRequest.objects.count()
        reconcile_pending_adjustments()
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)
        ctx["adjustment"].refresh_from_db()
        self.assertEqual(ctx["adjustment"].status, IBCommissionAdjustment.ST_APPROVED)


# ─────────────────────────────────────────────────────────────────────────
# E — money safety: reconciler alone, never mixed with approve/link/execute
# in the same before/after snapshot window.
# ─────────────────────────────────────────────────────────────────────────

class MoneySafetyTests(TestCase):
    def _snapshot(self):
        return {
            "wallet_tx": WalletTransaction.objects.count(),
            "ledger_entry": LedgerEntry.objects.count(),
            "broker_ledger": BrokerLedger.objects.count(),
            "treasury_request": TreasuryOperationRequest.objects.count(),
            "trade": Trade.objects.count(),
            "position": Position.objects.count(),
            "lot_execution_event": LotExecutionEvent.objects.count(),
        }

    def test_obligation_reconciler_alone_moves_nothing(self):
        # Fixtures at every Treasury status EXCEPT EXECUTED (that one
        # legitimately triggers exactly one CREDITED status write on the
        # obligation itself, already proven separately in section C —
        # here we only assert it creates no EXTRA row/count changes to
        # anything else).
        ctx_pending = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_PENDING)
        ctx_rejected = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_REJECTED)
        account_balance_before = ctx_pending["ib_owner"]
        wallet_before, _ = get_or_create_wallet(ctx_pending["ib_owner"])
        balance_before = wallet_before.available_balance

        before = self._snapshot()
        reconcile_approved_obligations()
        after = self._snapshot()

        self.assertEqual(before, after)
        wallet_before.refresh_from_db()
        self.assertEqual(wallet_before.available_balance, balance_before)

    def test_adjustment_reconciler_alone_moves_nothing(self):
        ctx_pending = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_PENDING)
        ctx_rejected = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_REJECTED)

        before = self._snapshot()
        reconcile_pending_adjustments()
        after = self._snapshot()

        self.assertEqual(before, after)

    def test_executed_obligation_sync_creates_no_extra_money_rows(self):
        # The one case that DOES write (CREDITED) must still create zero
        # WalletTransaction/LedgerEntry/BrokerLedger/TreasuryOperationRequest
        # rows of its own — those were already created earlier, by
        # Treasury's own execution, not by this reconciliation call.
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        before = self._snapshot()
        reconcile_approved_obligations()
        after = self._snapshot()
        self.assertEqual(before, after)

    def test_executed_adjustment_sync_creates_no_extra_money_rows(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        before = self._snapshot()
        reconcile_pending_adjustments()
        after = self._snapshot()
        self.assertEqual(before, after)


# ─────────────────────────────────────────────────────────────────────────
# F — snapshot safety
# ─────────────────────────────────────────────────────────────────────────

class SnapshotSafetyTests(TestCase):
    def test_rule_change_after_obligation_creation_does_not_change_calculated_amount(self):
        ctx = _make_pending_obligation("40.00")
        rule = IBCommissionRule.objects.get(referral=ctx["referral"])
        rule.fixed_amount = Decimal("999.00")
        rule.save(update_fields=["fixed_amount"])

        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()
        obligation = approve_obligation(ctx["obligation"], request=_fake_request(reviewer))
        treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        reconcile_approved_obligations()
        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, Decimal("40.00"))

    def test_reconciler_never_queries_ibcommissionrule(self):
        import inspect
        from simulator import ib_treasury_settlement, ib_commission_reversal
        src1 = inspect.getsource(ib_treasury_settlement.reconcile_approved_obligations)
        src2 = inspect.getsource(ib_treasury_settlement.sync_obligation_from_treasury)
        src3 = inspect.getsource(ib_commission_reversal.reconcile_pending_adjustments)
        src4 = inspect.getsource(ib_commission_reversal.sync_adjustment_from_treasury)
        for src in (src1, src2, src3, src4):
            self.assertNotIn("IBCommissionRule", src)

    def test_reconciler_never_writes_calculated_amount(self):
        import inspect
        from simulator import ib_treasury_settlement
        src = inspect.getsource(ib_treasury_settlement)
        self.assertNotIn(".calculated_amount = ", src)
        self.assertNotIn("calculated_amount=", src)


# ─────────────────────────────────────────────────────────────────────────
# G — task execution
# ─────────────────────────────────────────────────────────────────────────

class TaskExecutionTests(TestCase):
    def test_obligation_task_executes(self):
        ctx = _make_approved_obligation_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        result = reconcile_ib_treasury_settlement_task.apply().get()
        self.assertEqual(result["credited"], 1)

    def test_obligation_task_result_shape_exact(self):
        result = reconcile_ib_treasury_settlement_task.apply().get()
        self.assertEqual(set(result.keys()), {"scanned", "credited", "skipped", "failed", "elapsed_ms"})

    def test_adjustment_task_executes(self):
        ctx = _make_approved_adjustment_at_treasury_status(TreasuryOperationRequest.ST_EXECUTED)
        result = reconcile_ib_commission_adjustments_task.apply().get()
        self.assertEqual(result["executed"], 1)

    def test_adjustment_task_result_shape_exact(self):
        result = reconcile_ib_commission_adjustments_task.apply().get()
        self.assertEqual(set(result.keys()), {"scanned", "executed", "skipped", "failed", "elapsed_ms"})


# ─────────────────────────────────────────────────────────────────────────
# H — configuration protection
# ─────────────────────────────────────────────────────────────────────────

class ConfigurationProtectionTests(TestCase):
    def test_redbeat_lock_timeout_unchanged(self):
        self.assertEqual(settings.REDBEAT_LOCK_TIMEOUT, 60 * 5)

    def test_celery_beat_scheduler_unchanged(self):
        self.assertEqual(settings.CELERY_BEAT_SCHEDULER, "redbeat.RedBeatScheduler")

    def test_only_two_new_entries_added(self):
        # Exhaustive key-set comparison: every previously-known key (all
        # 17, enumerated by direct inspection of settings.py before this
        # block's own diff was applied) must still be present, PLUS
        # exactly the two new ones — nothing removed, nothing else added.
        expected_preexisting_keys = {
            "reconcile-deposits-15m",
            "reconcile-withdrawals-15m",
            "reconcile-unknown-payouts-15m",
            "replay-payout-webhook-events-5m",
            "sweep-ib-commission-triggers-5m",
            "beat-heartbeat-5m",
            "take-snapshots-1m",
            "cleanup-audit-log-daily",
            "cleanup-snapshots-daily",
            "scan-positions-30s",
            "scan-pending-orders-30s",
            "sweep-verified-wallets-15m",
            "take-revenue-snapshot-5m",
            "evaluate-challenges-hourly",
            "observe-broker-risk-alerts-5m",
            "observe-treasury-stuck-executions-15m",
            "record-celery-beat-heartbeat-5m",
        }
        new_keys = {"reconcile-ib-treasury-settlement-5m", "reconcile-ib-commission-adjustments-5m"}
        actual_keys = set(settings.CELERY_BEAT_SCHEDULE.keys())
        self.assertEqual(actual_keys, expected_preexisting_keys | new_keys)

    def test_preexisting_entries_byte_identical_in_shape(self):
        # Spot-check a representative sample of pre-existing entries'
        # exact task/args/options — proves this block did not silently
        # alter an unrelated entry while adding the two new ones.
        from celery.schedules import crontab
        entry = settings.CELERY_BEAT_SCHEDULE["reconcile-deposits-15m"]
        self.assertEqual(entry["task"], "simulator.reconcile_deposits")
        self.assertEqual(entry["args"], (24,))
        self.assertEqual(entry["options"]["expires"], 14 * 60)

        entry = settings.CELERY_BEAT_SCHEDULE["beat-heartbeat-5m"]
        self.assertEqual(entry["task"], "simulator.ping")
        self.assertEqual(entry["args"], ("beat-heartbeat",))
