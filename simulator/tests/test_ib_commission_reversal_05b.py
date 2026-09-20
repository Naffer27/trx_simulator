# simulator/tests/test_ib_commission_reversal_05b.py
"""
IB-REVERSALS-FRAUD-05B

Regression coverage for simulator/ib_commission_reversal.py:
  - submit_adjustment()             (Service 1: CREDITED obligation -> PENDING adjustment)
  - approve_adjustment()            (Service 2: PENDING -> APPROVED)
  - reject_adjustment()             (PENDING -> REJECTED)
  - link_adjustment_to_treasury()   (Service 3: APPROVED -> linked TreasuryOperationRequest)
  - sync_adjustment_from_treasury() (Service 4: APPROVED -> EXECUTED)
  - reconcile_pending_adjustments() (bulk sync sweep)

This suite proves the reversal layer NEVER becomes a second economic
engine and NEVER corrupts the original credited history: the original
IBCommissionObligation's snapshot fields (calculated_amount, status,
credited_at, treasury_operation) must remain byte-identical throughout
every test. Real, unmocked Treasury execution
(approve_treasury_request()/execute_treasury_request()) is exercised
end-to-end to prove the actual wallet debit is exactly correct and
happens exactly once.
"""
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.ib_commission_reversal import (
    AdjustmentExceedsRemaining, AdjustmentNotApproved, AdjustmentNotEligible,
    AdjustmentNotPending, approve_adjustment, link_adjustment_to_treasury,
    reconcile_pending_adjustments, reject_adjustment, remaining_reversible,
    submit_adjustment, sync_adjustment_from_treasury,
)
from simulator.ib_treasury_settlement import approve_obligation, link_treasury_request
from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerLedger, IBCommissionAdjustment,
    IBCommissionObligation, IBCommissionRule, LedgerEntry, LotExecutionEvent,
    Position, Referral, ReferralAttribution, Trade, TreasuryOperationRequest,
    WalletTransaction,
)
from simulator.treasury_requests import approve_treasury_request, execute_treasury_request
from simulator.tests.factories import make_user
from simulator.wallet_ledger import InsufficientFunds, credit_wallet, get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"revb05_{_seq}"


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


def _make_credited_obligation(calculated_amount="40.00"):
    """Full happy-path settlement via the real, unmodified
    IB-TREASURY-CREDIT-03 services, ending in a real CREDITED
    obligation with a real WalletTransaction already posted — exactly
    the starting state 05B operates on."""
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

    return {
        "ib_owner": ib_owner, "referral": referral, "trader": trader, "rule": rule,
        "obligation": obligation,
    }


def _next_seq():
    global _seq
    _seq += 1
    return _seq


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


def _execute_adjustment_fully(adj, reviewer, submitter, executor):
    """Full happy-path adjustment settlement, real Treasury execution."""
    adj = approve_adjustment(adj, request=_fake_request(reviewer))
    treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
    approve_treasury_request(treasury_request, request=_fake_request(reviewer))
    treasury_request = execute_treasury_request(treasury_request, request=_fake_request(executor))
    result = sync_adjustment_from_treasury(adj)
    return result["adjustment"], treasury_request


# ─────────────────────────────────────────────────────────────────────────
# 1/2/3 — full reversal, partial reversal, multiple valid partials
# ─────────────────────────────────────────────────────────────────────────

class ReversalAmountTests(TestCase):
    def test_full_reversal(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance

        adj = submit_adjustment(
            ctx["obligation"], amount=Decimal("40.00"), reason="full reversal test",
            request=_fake_request(submitter),
        )
        adj, treasury_request = _execute_adjustment_fully(adj, reviewer, submitter, executor)

        self.assertEqual(adj.status, IBCommissionAdjustment.ST_EXECUTED)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before - Decimal("40.00"))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("0.00"))

    def test_partial_reversal(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        adj = submit_adjustment(
            ctx["obligation"], amount=Decimal("10.00"), reason="partial",
            request=_fake_request(submitter),
        )
        self.assertEqual(adj.amount, Decimal("10.00"))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("30.00"))

    def test_multiple_valid_partials(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="p1", request=_fake_request(submitter))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("30.00"))
        submit_adjustment(ctx["obligation"], amount=Decimal("15.00"), reason="p2", request=_fake_request(submitter))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("15.00"))
        submit_adjustment(ctx["obligation"], amount=Decimal("15.00"), reason="p3", request=_fake_request(submitter))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("0.00"))


# ─────────────────────────────────────────────────────────────────────────
# 4 — over-reversal prohibition
# ─────────────────────────────────────────────────────────────────────────

class OverReversalProtectionTests(TestCase):
    def test_single_over_reversal_rejected(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        with self.assertRaises(AdjustmentExceedsRemaining):
            submit_adjustment(
                ctx["obligation"], amount=Decimal("40.01"), reason="too much",
                request=_fake_request(submitter),
            )

    def test_cumulative_over_reversal_rejected(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="p1", request=_fake_request(submitter))
        submit_adjustment(ctx["obligation"], amount=Decimal("15.00"), reason="p2", request=_fake_request(submitter))
        # remaining = 15.00 — requesting 15.01 must fail
        with self.assertRaises(AdjustmentExceedsRemaining):
            submit_adjustment(
                ctx["obligation"], amount=Decimal("15.01"), reason="p3 too much",
                request=_fake_request(submitter),
            )

    def test_rejected_adjustment_releases_capacity(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj1 = submit_adjustment(ctx["obligation"], amount=Decimal("40.00"), reason="p1", request=_fake_request(submitter))
        # No capacity left
        with self.assertRaises(AdjustmentExceedsRemaining):
            submit_adjustment(ctx["obligation"], amount=Decimal("1.00"), reason="p2", request=_fake_request(submitter))
        reject_adjustment(adj1, "changed my mind", request=_fake_request(reviewer))
        self.assertEqual(remaining_reversible(ctx["obligation"]), Decimal("40.00"))
        # Now it fits again
        adj2 = submit_adjustment(ctx["obligation"], amount=Decimal("40.00"), reason="p2", request=_fake_request(submitter))
        self.assertIsNotNone(adj2.pk)

    def test_approval_race_reexcludes_self_correctly(self):
        # Approving an adjustment must not double-count its OWN reserved
        # amount when re-checking remaining capacity.
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("40.00"), reason="full", request=_fake_request(submitter))
        approved = approve_adjustment(adj, request=_fake_request(reviewer))
        self.assertEqual(approved.status, IBCommissionAdjustment.ST_APPROVED)


# ─────────────────────────────────────────────────────────────────────────
# 5/6/25 — original obligation and snapshot remain intact
# ─────────────────────────────────────────────────────────────────────────

class OriginalImmutabilityTests(TestCase):
    def test_original_obligation_remains_intact(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()

        original_calculated_amount = ctx["obligation"].calculated_amount
        original_credited_at = ctx["obligation"].credited_at
        original_treasury_op_id = ctx["obligation"].treasury_operation_id
        original_status = ctx["obligation"].status

        adj = submit_adjustment(ctx["obligation"], amount=Decimal("15.00"), reason="test", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)

        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].calculated_amount, original_calculated_amount)
        self.assertEqual(ctx["obligation"].credited_at, original_credited_at)
        self.assertEqual(ctx["obligation"].treasury_operation_id, original_treasury_op_id)
        self.assertEqual(ctx["obligation"].status, original_status, "CREDITED must never revert")

    def test_original_snapshot_fields_intact(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        obligation = ctx["obligation"]
        basis_quantity_before = obligation.basis_quantity
        applied_fixed_rate_before = obligation.applied_fixed_rate
        rule_type_before = obligation.rule_type

        submit_adjustment(obligation, amount=Decimal("5.00"), reason="test", request=_fake_request(submitter))

        obligation.refresh_from_db()
        self.assertEqual(obligation.basis_quantity, basis_quantity_before)
        self.assertEqual(obligation.applied_fixed_rate, applied_fixed_rate_before)
        self.assertEqual(obligation.rule_type, rule_type_before)


# ─────────────────────────────────────────────────────────────────────────
# 7/8/9/10/11/12 — idempotency
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def test_repeated_approval_raises_not_pending(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        with self.assertRaises(AdjustmentNotPending):
            approve_adjustment(adj, request=_fake_request(reviewer))

    def test_repeated_treasury_linking_no_duplicate(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        r1 = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        r2 = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(
            TreasuryOperationRequest.objects.filter(reference=f"IBCommissionAdjustment #{adj.pk}").count(), 1,
        )

    def test_repeated_sync_no_duplicate_money(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        adj, treasury_request = _execute_adjustment_fully(adj, reviewer, submitter, executor)

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_after_first = wallet.available_balance

        for _ in range(3):
            sync_adjustment_from_treasury(adj)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_after_first)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1)

    def test_exactly_one_wallet_transaction_per_execution(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        adj, treasury_request = _execute_adjustment_fully(adj, reviewer, submitter, executor)
        self.assertIsNotNone(treasury_request.wallet_transaction_id)
        self.assertEqual(
            WalletTransaction.objects.filter(pk=treasury_request.wallet_transaction_id).count(), 1,
        )

    def test_treasury_amount_exactly_matches_adjustment(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("12.34"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        self.assertEqual(treasury_request.amount, Decimal("12.34"))
        self.assertEqual(treasury_request.operation_type, TreasuryOperationRequest.OP_MANUAL_DEBIT)

    def test_wallet_delta_exactly_correct(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("17.50"), reason="t", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)
        wallet.refresh_from_db()
        self.assertEqual(balance_before - wallet.available_balance, Decimal("17.50"))


# ─────────────────────────────────────────────────────────────────────────
# 13 — insufficient balance
# ─────────────────────────────────────────────────────────────────────────

class InsufficientBalanceTests(TestCase):
    def test_insufficient_balance_protected(self):
        ctx = _make_credited_obligation("100.00")
        ib_owner = ctx["ib_owner"]
        wallet, _ = get_or_create_wallet(ib_owner)
        # Simulate the IB having withdrawn most of the funds — drain the
        # wallet down to $20 via the real debit primitive (not a raw
        # field write).
        from simulator.wallet_ledger import debit_wallet
        debit_wallet(wallet.id, wallet.available_balance - Decimal("20.00"), WalletTransaction.TX_CORRECTION, note="simulate withdrawal")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"))

        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()

        adj = submit_adjustment(ctx["obligation"], amount=Decimal("50.00"), reason="insufficient test", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))

        with self.assertRaises(InsufficientFunds):
            execute_treasury_request(treasury_request, request=_fake_request(executor))

        treasury_request.refresh_from_db()
        self.assertEqual(treasury_request.status, TreasuryOperationRequest.ST_FAILED)
        self.assertIsNone(treasury_request.wallet_transaction_id)

        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_APPROVED, "must not have moved to EXECUTED")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"), "no partial/silent debit occurred")


# ─────────────────────────────────────────────────────────────────────────
# 14 — permission enforcement
# ─────────────────────────────────────────────────────────────────────────

class PermissionTests(TestCase):
    def test_submit_without_permission_rejected(self):
        ctx = _make_credited_obligation("40.00")
        plain = make_user(is_staff=True)
        with self.assertRaises(PermissionDenied):
            submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(plain))
        self.assertEqual(IBCommissionAdjustment.objects.count(), 0)

    def test_approve_without_permission_rejected(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        plain = make_user(is_staff=True)
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        with self.assertRaises(PermissionDenied):
            approve_adjustment(adj, request=_fake_request(plain))
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)

    def test_link_without_permission_rejected(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        plain = make_user(is_staff=True)
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        with self.assertRaises(PermissionDenied):
            link_adjustment_to_treasury(adj, request=_fake_request(plain))
        adj.refresh_from_db()
        self.assertIsNone(adj.treasury_operation_id)


# ─────────────────────────────────────────────────────────────────────────
# 15/16/17/18 — money moves only at EXECUTED
# ─────────────────────────────────────────────────────────────────────────

class StateMoneyBoundaryTests(TestCase):
    def test_pending_moves_no_money(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

    def test_approved_alone_moves_no_money(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

    def test_rejected_moves_no_money(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        reject_adjustment(adj, "no", request=_fake_request(reviewer))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

    def test_cancelled_treasury_request_moves_no_money(self):
        from simulator.treasury_requests import cancel_treasury_request
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        cancel_treasury_request(treasury_request, request=_fake_request(submitter))

        result = sync_adjustment_from_treasury(adj)
        self.assertEqual(result["outcome"], "treasury_terminal_non_success")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_APPROVED, "stays APPROVED, never silently reversed away")

    def test_executed_is_only_state_reflecting_completed_movement(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance

        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_PENDING)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)

        adj, _ = _execute_adjustment_fully(adj, reviewer, submitter, executor)
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_EXECUTED)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before - Decimal("10.00"))


# ─────────────────────────────────────────────────────────────────────────
# 19 — concurrency/idempotency locking, structural
# ─────────────────────────────────────────────────────────────────────────

class LockingStructureTests(TestCase):
    def test_submit_and_approve_use_select_for_update(self):
        import inspect
        from simulator import ib_commission_reversal
        source = inspect.getsource(ib_commission_reversal)
        # Every service function that touches obligation/adjustment
        # state must lock before deciding — structural proof, same
        # discipline already verified for ib_treasury_settlement.py.
        self.assertIn("select_for_update()", source)
        self.assertGreaterEqual(source.count("select_for_update()"), 6)

    def test_obligation_locked_before_over_reversal_check(self):
        import inspect
        from simulator import ib_commission_reversal
        source = inspect.getsource(ib_commission_reversal.submit_adjustment)
        self.assertIn("select_for_update()", source)


# ─────────────────────────────────────────────────────────────────────────
# 20/21/22 — no unrelated ledger/trading-engine mutation
# ─────────────────────────────────────────────────────────────────────────

class NoSideEffectTests(TestCase):
    def test_no_new_broker_ledger_from_reversal(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        before = BrokerLedger.objects.count()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)
        self.assertEqual(BrokerLedger.objects.count(), before)

    def test_no_ledger_entry_from_reversal(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        before = LedgerEntry.objects.count()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)
        self.assertEqual(LedgerEntry.objects.count(), before)

    def test_no_trading_engine_mutation(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        pos_before = Position.objects.count()
        trade_before = Trade.objects.count()
        lee_before = LotExecutionEvent.objects.count()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)
        self.assertEqual(Position.objects.count(), pos_before)
        self.assertEqual(Trade.objects.count(), trade_before)
        self.assertEqual(LotExecutionEvent.objects.count(), lee_before)

    def test_no_consumer_or_population_engine_dependency(self):
        import inspect
        from simulator import ib_commission_reversal
        source = inspect.getsource(ib_commission_reversal)
        self.assertNotIn("from .consumers", source)
        self.assertNotIn("from .population_engine", source)
        self.assertNotIn("from .payout_orchestrator", source)


# ─────────────────────────────────────────────────────────────────────────
# 23 — Decimal correctness
# ─────────────────────────────────────────────────────────────────────────

class DecimalCorrectnessTests(TestCase):
    def test_decimal_rounding_correct(self):
        ctx = _make_credited_obligation("33.33")
        submitter = _make_submitter()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("11.11"), reason="t", request=_fake_request(submitter))
        self.assertEqual(adj.amount, Decimal("11.11"))
        self.assertIsInstance(adj.amount, Decimal)
        remaining = remaining_reversible(ctx["obligation"])
        self.assertEqual(remaining, Decimal("22.22"))

    def test_amount_must_be_positive(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        with self.assertRaises(ValueError):
            submit_adjustment(ctx["obligation"], amount=Decimal("0.00"), reason="t", request=_fake_request(submitter))
        with self.assertRaises(ValueError):
            submit_adjustment(ctx["obligation"], amount=Decimal("-5.00"), reason="t", request=_fake_request(submitter))


# ─────────────────────────────────────────────────────────────────────────
# 24 — audit trail
# ─────────────────────────────────────────────────────────────────────────

class AuditTrailTests(TestCase):
    def test_audit_trail_reconstructible(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()

        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="audit test", request=_fake_request(submitter))
        _execute_adjustment_fully(adj, reviewer, submitter, executor)

        self.assertTrue(AuditLog.objects.filter(event_type="ib_reversal.adjustment_submitted").exists())
        self.assertTrue(AuditLog.objects.filter(event_type="ib_reversal.adjustment_approved").exists())
        self.assertTrue(AuditLog.objects.filter(event_type="ib_reversal.treasury_debit_linked").exists())
        self.assertTrue(BrokerAuditEvent.objects.filter(event_type="ib_reversal.adjustment_submitted").exists())

        log = AuditLog.objects.filter(event_type="ib_reversal.adjustment_submitted").first()
        self.assertEqual(log.detail["obligation_id"], ctx["obligation"].pk)
        self.assertEqual(log.detail["amount"], "10.00")

    def test_rejection_audited(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        reject_adjustment(adj, "not justified", request=_fake_request(reviewer))
        self.assertTrue(AuditLog.objects.filter(event_type="ib_reversal.adjustment_rejected").exists())


# ─────────────────────────────────────────────────────────────────────────
# Eligibility — only CREDITED obligations may be reversed
# ─────────────────────────────────────────────────────────────────────────

class EligibilityTests(TestCase):
    def test_pending_obligation_not_eligible(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        rule = _make_rule(referral=referral)
        attribution = ReferralAttribution.objects.get(referral=referral)
        obligation = IBCommissionObligation.objects.create(
            attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
            source_event_type="test_event", source_event_id=_next_seq(),
            calculated_amount=Decimal("10.00"), currency="USD",
            status=IBCommissionObligation.ST_PENDING,
        )
        submitter = _make_submitter()
        with self.assertRaises(AdjustmentNotEligible):
            submit_adjustment(obligation, amount=Decimal("5.00"), reason="t", request=_fake_request(submitter))

    def test_link_requires_approved_status(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        with self.assertRaises(AdjustmentNotApproved):
            link_adjustment_to_treasury(adj, request=_fake_request(submitter))


# ─────────────────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────────────────

class ReconciliationTests(TestCase):
    def test_reconciliation_syncs_executed(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        result = reconcile_pending_adjustments()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["executed"], 1)

        adj.refresh_from_db()
        self.assertEqual(adj.status, IBCommissionAdjustment.ST_EXECUTED)

    def test_reconciliation_repeated_run_idempotent(self):
        ctx = _make_credited_obligation("40.00")
        submitter = _make_submitter()
        reviewer = _make_reviewer()
        executor = _make_executor()
        adj = submit_adjustment(ctx["obligation"], amount=Decimal("10.00"), reason="t", request=_fake_request(submitter))
        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        r1 = reconcile_pending_adjustments()
        r2 = reconcile_pending_adjustments()
        self.assertEqual(r1["executed"], 1)
        self.assertEqual(r2["scanned"], 0, "already-EXECUTED adjustments are excluded from the next scan")
