# simulator/tests/test_ib_treasury_credit_03.py
"""
IB-TREASURY-CREDIT-03

Regression coverage for simulator/ib_treasury_settlement.py:
  - approve_obligation()             (Service 1: PENDING -> APPROVED)
  - link_treasury_request()          (Service 2: APPROVED -> linked TreasuryOperationRequest)
  - sync_obligation_from_treasury()  (Service 3: APPROVED -> CREDITED)
  - reconcile_approved_obligations() (reconciliation sweep)

Every IBCommissionObligation used here is constructed directly via the
ORM (like test_ib_commission_engine_01.py's own established pattern),
never via a real trading/deposit/challenge event and never via
simulator/consumers.py or simulator/population_engine.py — this suite
certifies the settlement layer in isolation from generation.

The existing Treasury execution engine (treasury_requests.py,
wallet_ledger.py) is exercised for real, unmocked, exactly as a staff
member would drive it through the admin — this suite proves the FULL
chain end-to-end: obligation approval -> Treasury link -> Treasury's own
approve/execute -> real WalletTransaction/Wallet.available_balance ->
reverse sync to CREDITED.
"""
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.utils import timezone

from simulator.ib_treasury_settlement import (
    OUTCOME_ALREADY_CREDITED, OUTCOME_CREDITED, OUTCOME_NOT_APPROVED,
    OUTCOME_NOT_LINKED, OUTCOME_TREASURY_PENDING_EXECUTION,
    OUTCOME_TREASURY_TERMINAL_NON_SUCCESS,
    ObligationInvalidAmount, ObligationNotApproved, ObligationNotPending,
    approve_obligation, link_treasury_request, reconcile_approved_obligations,
    sync_obligation_from_treasury,
)
from simulator.models import (
    BrokerLedger, IBCommissionObligation, IBCommissionRule, LedgerEntry,
    Referral, ReferralAttribution, TreasuryOperationRequest, Wallet,
    WalletTransaction,
)
from simulator.treasury_requests import (
    approve_treasury_request, execute_treasury_request, reject_treasury_request,
)
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"trs03_{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type=IBCommissionRule.RULE_PER_LOT, referral=None,
               fixed_amount=Decimal("10.00"), percentage=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=True,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=timezone.now() - timezone.timedelta(minutes=5),
    )


def _make_obligation(referral, rule, calculated_amount="10.00",
                      rule_type=None, source_event_type="test_event",
                      source_event_id=None, status=IBCommissionObligation.ST_PENDING):
    global _seq
    _seq += 1
    attribution = ReferralAttribution.objects.filter(referral=referral).first()
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule,
        rule_type=rule_type or rule.rule_type,
        source_event_type=source_event_type,
        source_event_id=source_event_id if source_event_id is not None else _seq,
        calculated_amount=Decimal(calculated_amount),
        currency="USD", status=status,
    )


def _referred_setup(rule_type=IBCommissionRule.RULE_PER_LOT, calculated_amount="10.00", **rule_kwargs):
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    rule = _make_rule(rule_type=rule_type, **rule_kwargs)
    obligation = _make_obligation(referral, rule, calculated_amount=calculated_amount, rule_type=rule_type)
    return {"ib_owner": ib_owner, "referral": referral, "trader": trader, "rule": rule, "obligation": obligation}


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


def _request_for(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _settle_fully(ctx, submitter=None, reviewer=None, executor=None):
    """Full happy-path settlement: approve obligation, link Treasury
    request, Treasury-approve it, Treasury-execute it, sync CREDITED.
    Returns (obligation, treasury_request)."""
    reviewer = reviewer or _make_reviewer()
    submitter = submitter or _make_submitter()
    executor = executor or _make_executor()

    obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
    treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
    treasury_request = approve_treasury_request(treasury_request, request=_request_for(reviewer))
    treasury_request = execute_treasury_request(treasury_request, request=_request_for(executor))
    result = sync_obligation_from_treasury(obligation)
    return result["obligation"], treasury_request


# ─────────────────────────────────────────────────────────────────────────
# 1/3 — approve_obligation()
# ─────────────────────────────────────────────────────────────────────────

class ApproveObligationTests(TestCase):
    def test_pending_obligation_approval_works(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)
        self.assertEqual(obligation.approved_by_id, reviewer.pk)
        self.assertIsNotNone(obligation.approved_at)

    def test_non_authorized_approval_rejected(self):
        ctx = _referred_setup()
        plain_user = make_user(is_staff=True)  # no permission granted
        with self.assertRaises(PermissionDenied):
            approve_obligation(ctx["obligation"], request=_request_for(plain_user))
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_PENDING)

    def test_invalid_amount_cannot_proceed(self):
        ctx = _referred_setup(calculated_amount="0.00")
        reviewer = _make_reviewer()
        with self.assertRaises(ObligationInvalidAmount):
            approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_PENDING)

    def test_pending_cannot_be_directly_credited_improperly(self):
        # A PENDING obligation must go through approve_obligation() first
        # — sync_obligation_from_treasury() must refuse to touch it.
        ctx = _referred_setup()
        result = sync_obligation_from_treasury(ctx["obligation"])
        self.assertEqual(result["outcome"], OUTCOME_NOT_APPROVED)
        ctx["obligation"].refresh_from_db()
        self.assertEqual(ctx["obligation"].status, IBCommissionObligation.ST_PENDING)

    def test_repeated_approval_raises_not_pending(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        with self.assertRaises(ObligationNotPending):
            approve_obligation(ctx["obligation"], request=_request_for(reviewer))


# ─────────────────────────────────────────────────────────────────────────
# 4/5/6/7/8/9 — link_treasury_request()
# ─────────────────────────────────────────────────────────────────────────

class LinkTreasuryRequestTests(TestCase):
    def test_approved_obligation_creates_treasury_request(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        self.assertIsNotNone(treasury_request.pk)
        self.assertEqual(treasury_request.status, TreasuryOperationRequest.ST_PENDING)

    def test_operation_type_is_ib_commission(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        self.assertEqual(treasury_request.operation_type, TreasuryOperationRequest.OP_IB_COMMISSION)

    def test_exact_calculated_amount_propagated(self):
        ctx = _referred_setup(calculated_amount="123.45")
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        self.assertEqual(treasury_request.amount, Decimal("123.45"))
        self.assertEqual(treasury_request.amount, obligation.calculated_amount)

    def test_rate_snapshot_preserved_after_rule_change(self):
        # Obligation generated at $10/lot; the rule is later changed to
        # $7/lot. Settlement must still pay the ORIGINAL $10 snapshot.
        ctx = _referred_setup(calculated_amount="10.00")
        rule = ctx["rule"]
        rule.fixed_amount = Decimal("7.00")
        rule.save(update_fields=["fixed_amount"])

        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))

        self.assertEqual(treasury_request.amount, Decimal("10.00"), "must pay the snapshot, not the current rule")
        self.assertEqual(obligation.calculated_amount, Decimal("10.00"))

    def test_repeated_link_call_returns_same_treasury_request(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        r1 = link_treasury_request(obligation, request=_request_for(submitter))
        r2 = link_treasury_request(obligation, request=_request_for(submitter))
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(
            TreasuryOperationRequest.objects.filter(reference=f"IBCommissionObligation #{obligation.pk}").count(),
            1, "exactly one TreasuryOperationRequest",
        )

    def test_link_requires_approved_status(self):
        ctx = _referred_setup()
        submitter = _make_submitter()
        with self.assertRaises(ObligationNotApproved):
            link_treasury_request(ctx["obligation"], request=_request_for(submitter))

    def test_link_non_authorized_rejected(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        plain_user = make_user(is_staff=True)
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        with self.assertRaises(PermissionDenied):
            link_treasury_request(obligation, request=_request_for(plain_user))


# ─────────────────────────────────────────────────────────────────────────
# 10/11/12/13 — Treasury execution -> real wallet credit
# ─────────────────────────────────────────────────────────────────────────

class TreasuryExecutionIntegrationTests(TestCase):
    def test_no_wallet_movement_before_treasury_execution(self):
        ctx = _referred_setup(calculated_amount="50.00")
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance

        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        link_treasury_request(obligation, request=_request_for(submitter))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)

    def test_existing_treasury_execution_credits_correct_ib_wallet(self):
        ctx = _referred_setup(calculated_amount="50.00")
        obligation, treasury_request = _settle_fully(ctx)
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))

    def test_wallet_transaction_created_exactly_once(self):
        ctx = _referred_setup(calculated_amount="50.00")
        obligation, treasury_request = _settle_fully(ctx)
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)
        wtx = WalletTransaction.objects.get(wallet=wallet)
        self.assertEqual(wtx.tx_type, WalletTransaction.TX_REBATE)
        self.assertEqual(wtx.amount, Decimal("50.00"))
        self.assertEqual(treasury_request.wallet_transaction_id, wtx.pk)

    def test_wallet_balance_delta_exactly_equals_calculated_amount(self):
        ctx = _referred_setup(calculated_amount="33.33")
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_before = wallet.available_balance
        obligation, _ = _settle_fully(ctx)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance - balance_before, Decimal("33.33"))
        self.assertEqual(wallet.available_balance - balance_before, obligation.calculated_amount)


# ─────────────────────────────────────────────────────────────────────────
# 14/15/16/17 — sync_obligation_from_treasury()
# ─────────────────────────────────────────────────────────────────────────

class SyncObligationTests(TestCase):
    def test_sync_after_executed_transitions_to_credited(self):
        ctx = _referred_setup()
        obligation, treasury_request = _settle_fully(ctx)
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)

    def test_credited_at_populated(self):
        ctx = _referred_setup()
        obligation, treasury_request = _settle_fully(ctx)
        self.assertIsNotNone(obligation.credited_at)
        self.assertEqual(obligation.credited_at, treasury_request.executed_at)

    def test_repeated_sync_is_idempotent(self):
        ctx = _referred_setup()
        obligation, treasury_request = _settle_fully(ctx)
        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["outcome"], OUTCOME_ALREADY_CREDITED)
        self.assertEqual(result["obligation"].status, IBCommissionObligation.ST_CREDITED)
        self.assertEqual(result["obligation"].credited_at, obligation.credited_at)

    def test_repeated_settlement_does_not_double_credit(self):
        ctx = _referred_setup(calculated_amount="20.00")
        obligation, treasury_request = _settle_fully(ctx)
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        balance_after_first = wallet.available_balance

        # Re-run sync (and re-attempt link — must be a no-op) several times.
        for _ in range(3):
            sync_obligation_from_treasury(obligation)
        reviewer = _make_reviewer()
        with self.assertRaises(ObligationNotPending):
            approve_obligation(obligation, request=_request_for(reviewer))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_after_first)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)

    def test_sync_not_linked_outcome(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["outcome"], OUTCOME_NOT_LINKED)

    def test_sync_pending_execution_outcome(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        link_treasury_request(obligation, request=_request_for(submitter))
        # Treasury request still PENDING — not yet approved/executed.
        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["outcome"], OUTCOME_TREASURY_PENDING_EXECUTION)
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)


# ─────────────────────────────────────────────────────────────────────────
# 18/19/20/21/22 — REJECTED / CANCELLED / FAILED policy
# ─────────────────────────────────────────────────────────────────────────

class TerminalNonSuccessPolicyTests(TestCase):
    def test_rejected_treasury_request_does_not_credit(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        reject_treasury_request(treasury_request, "not eligible", request=_request_for(reviewer))

        result = sync_obligation_from_treasury(obligation)
        self.assertEqual(result["outcome"], OUTCOME_TREASURY_TERMINAL_NON_SUCCESS)
        self.assertEqual(result["treasury_status"], TreasuryOperationRequest.ST_REJECTED)
        self.assertEqual(result["obligation"].status, IBCommissionObligation.ST_APPROVED)

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        self.assertEqual(wallet.available_balance, Decimal("0"))

    def test_unsuccessful_treasury_request_leaves_obligation_approved_and_linked(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        reject_treasury_request(treasury_request, "not eligible", request=_request_for(reviewer))

        sync_obligation_from_treasury(obligation)
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)
        self.assertEqual(obligation.treasury_operation_id, treasury_request.pk, "stays linked for auditability")

    def test_no_automatic_replacement_treasury_request(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        reject_treasury_request(treasury_request, "not eligible", request=_request_for(reviewer))

        sync_obligation_from_treasury(obligation)
        # A second link_treasury_request() call is still a no-op (already
        # linked) — 03 never spins up a replacement automatically.
        result = link_treasury_request(obligation, request=_request_for(submitter))
        self.assertEqual(result.pk, treasury_request.pk)
        self.assertEqual(
            TreasuryOperationRequest.objects.filter(
                reference=f"IBCommissionObligation #{obligation.pk}",
            ).count(),
            1,
        )


# ─────────────────────────────────────────────────────────────────────────
# 23/24/25/26 — all four shipped rule types settle identically
# ─────────────────────────────────────────────────────────────────────────

class RuleTypeAgnosticSettlementTests(TestCase):
    def _settle_rule_type(self, rule_type, amount):
        ctx = _referred_setup(rule_type=rule_type, calculated_amount=amount, percentage=(
            Decimal("20.00") if rule_type != IBCommissionRule.RULE_PER_LOT else None
        ), fixed_amount=(
            Decimal("10.00") if rule_type == IBCommissionRule.RULE_PER_LOT else None
        ))
        obligation, treasury_request = _settle_fully(ctx)
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)
        self.assertEqual(treasury_request.amount, Decimal(amount))
        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal(amount))

    def test_per_lot_obligation_settlement(self):
        self._settle_rule_type(IBCommissionRule.RULE_PER_LOT, "10.00")

    def test_challenge_percent_obligation_settlement(self):
        self._settle_rule_type(IBCommissionRule.RULE_CHALLENGE_PERCENT, "40.00")

    def test_deposit_percent_obligation_settlement(self):
        self._settle_rule_type(IBCommissionRule.RULE_DEPOSIT_PERCENT, "5.00")

    def test_trading_commission_revenue_share_settlement(self):
        self._settle_rule_type(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, "1.40")


# ─────────────────────────────────────────────────────────────────────────
# 27/28/29/30 — no recalculation, no unrelated writes, no engine mutation
# ─────────────────────────────────────────────────────────────────────────

class NoSideEffectTests(TestCase):
    def test_no_current_rule_recalculation(self):
        # Amount snapshot is independent of the rule's CURRENT fields —
        # proven again here by deleting the rule's economic meaning
        # entirely (disabling it) after obligation generation; settlement
        # must still pay the original snapshot.
        ctx = _referred_setup(calculated_amount="10.00")
        ctx["rule"].enabled = False
        ctx["rule"].fixed_amount = Decimal("999.00")
        ctx["rule"].save(update_fields=["enabled", "fixed_amount"])

        obligation, treasury_request = _settle_fully(ctx)
        self.assertEqual(treasury_request.amount, Decimal("10.00"))

    def test_no_broker_ledger_creation(self):
        ctx = _referred_setup()
        before = BrokerLedger.objects.count()
        _settle_fully(ctx)
        self.assertEqual(BrokerLedger.objects.count(), before)

    def test_no_ledger_entry_duplication(self):
        ctx = _referred_setup()
        before = LedgerEntry.objects.count()
        _settle_fully(ctx)
        self.assertEqual(LedgerEntry.objects.count(), before)

    def test_no_trading_engine_mutation(self):
        # Settlement touches no Position/Trade/LotExecutionEvent table at
        # all — proven structurally: this whole suite never imports or
        # references any of those models, and the settlement module
        # itself imports nothing from consumers.py/population_engine.py
        # (verified separately in the pre-git audit's protected-file
        # diff check). This test only re-confirms no exception/side
        # effect leaks from settlement into unrelated trading tables.
        from simulator.models import LotExecutionEvent, Position, Trade
        pos_before = Position.objects.count()
        trade_before = Trade.objects.count()
        lee_before = LotExecutionEvent.objects.count()
        ctx = _referred_setup()
        _settle_fully(ctx)
        self.assertEqual(Position.objects.count(), pos_before)
        self.assertEqual(Trade.objects.count(), trade_before)
        self.assertEqual(LotExecutionEvent.objects.count(), lee_before)


# ─────────────────────────────────────────────────────────────────────────
# 31/32 — reconciliation
# ─────────────────────────────────────────────────────────────────────────

class ReconciliationTests(TestCase):
    def test_reconciliation_sync_works(self):
        ctx = _referred_setup(calculated_amount="15.00")
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        approve_treasury_request(treasury_request, request=_request_for(reviewer))
        execute_treasury_request(treasury_request, request=_request_for(executor))

        result = reconcile_approved_obligations()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["credited"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failed"], 0)

        obligation.refresh_from_db()
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)

    def test_reconciliation_repeated_run_idempotent(self):
        ctx = _referred_setup(calculated_amount="15.00")
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        approve_treasury_request(treasury_request, request=_request_for(reviewer))
        execute_treasury_request(treasury_request, request=_request_for(executor))

        r1 = reconcile_approved_obligations()
        r2 = reconcile_approved_obligations()
        self.assertEqual(r1["credited"], 1)
        # Already-CREDITED obligations are no longer status=APPROVED, so
        # the second run's query excludes them entirely (scanned=0).
        self.assertEqual(r2["scanned"], 0)
        self.assertEqual(r2["credited"], 0)

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)

    def test_reconciliation_reports_rejected_as_failed(self):
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        obligation = approve_obligation(ctx["obligation"], request=_request_for(reviewer))
        treasury_request = link_treasury_request(obligation, request=_request_for(submitter))
        reject_treasury_request(treasury_request, "not eligible", request=_request_for(reviewer))

        result = reconcile_approved_obligations()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["credited"], 0)
