# simulator/tests/test_broker_economics_03_summary.py
"""
BROKER-ECONOMICS-03 FASE B — broker_economics_summary.py

Adversarial suite for the read-only Broker Economics SSOT composition
layer. Proves: no double counting, no deposit/withdrawal-principal
counted as revenue, exact cross-check against broker_pnl.py, correct
IB expense lifecycle classification (pending/approved/credited/
reversed), signed adjustments, coverage/completeness honesty (including
the two Owner-approved-but-pending policy categories), deterministic
repeated queries, and zero DB writes from the service itself.
"""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.broker_economics_summary import (
    COVERAGE_COMPLETE, COVERAGE_NOT_APPLICABLE, COVERAGE_PARTIAL,
    COVERAGE_POLICY_APPROVED_PENDING, CategoryCoverage,
    broker_economics_summary, ib_expense_breakdown,
)
from simulator.models import (
    BrokerEconomicAdjustment, BrokerLedger, ChallengeProduct, Deposit,
    IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule,
    LotExecutionEvent, Referral, ReferralAttribution, WithdrawalRequest,
)
from simulator.tests.factories import (
    make_account, make_broker_ledger, make_deposit, make_spread_config, make_user,
)


def _key():
    return uuid.uuid4().hex


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=f"e03{uuid.uuid4().hex[:8]}")


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type, referral=None, percentage=None, fixed_amount=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=True,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=timezone.now() - timezone.timedelta(minutes=5),
    )


def _make_obligation(
    referral, attribution, rule, rule_type, amount, status=IBCommissionObligation.ST_PENDING,
    source_event_id=None, source_event_type="test_event",
):
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule_type,
        source_event_type=source_event_type, source_event_id=source_event_id or 1,
        calculated_amount=amount, status=status,
        credited_at=timezone.now() if status == IBCommissionObligation.ST_CREDITED else None,
    )


def _make_lot_event(account, symbol="EUR/USD", entry_path=LotExecutionEvent.ENTRY_PENDING_TRIGGER, qty=Decimal("1.0")):
    return LotExecutionEvent.objects.create(
        account=account, symbol=symbol, side="buy", qty=qty,
        execution_price=Decimal("1.1000"), merged=False, entry_path=entry_path,
    )


class ZeroDataTests(TestCase):
    def test_zero_data_period_returns_clean_zero_state(self):
        s = broker_economics_summary()
        self.assertEqual(s.gross_broker_economic_result, Decimal("0.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))
        self.assertEqual(s.capital_flows.deposits_total, Decimal("0.00"))
        self.assertEqual(s.retained.retained_economics, Decimal("0.00"))


class GrossEconomicsCrossCheckTests(TestCase):
    """Item: cross-check broker_pnl."""

    def test_commission_only_matches_broker_pnl_exactly(self):
        from simulator import broker_pnl
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("12.50"))
        s = broker_economics_summary()
        real = broker_pnl.calculate_broker_pnl()
        self.assertEqual(s.commission_revenue, real.commission)
        self.assertEqual(s.gross_broker_economic_result, real.broker_net_pnl)

    def test_spread_only(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("8.00"))
        s = broker_economics_summary()
        self.assertEqual(s.spread_revenue, Decimal("8.00"))
        self.assertEqual(s.commission_revenue, Decimal("0.00"))

    def test_positive_bbook_period(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, amount=Decimal("500.00"))
        s = broker_economics_summary()
        self.assertEqual(s.counterparty_pnl, Decimal("500.00"))
        self.assertEqual(s.gross_broker_economic_result, Decimal("500.00"))

    def test_negative_bbook_period(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, amount=Decimal("-500.00"))
        s = broker_economics_summary()
        self.assertEqual(s.counterparty_pnl, Decimal("-500.00"))
        self.assertEqual(s.gross_broker_economic_result, Decimal("-500.00"))

    def test_adjustments_positive(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("50.00"))
        s = broker_economics_summary()
        self.assertEqual(s.adjustments, Decimal("50.00"))

    def test_adjustments_negative(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("-20.00"))
        s = broker_economics_summary()
        self.assertEqual(s.adjustments, Decimal("-20.00"))

    def test_mixed_revenue_worked_example(self):
        """Mirrors broker_pnl.py's own certified example: 6 commission +
        8 counterparty - 3 adjustment = 11."""
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("6.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, amount=Decimal("8.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("-3.00"))
        s = broker_economics_summary()
        self.assertEqual(s.gross_broker_economic_result, Decimal("11.00"))

    def test_aggregate_equals_sum_of_components(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("10.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("5.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL, amount=Decimal("-2.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("1.00"))
        s = broker_economics_summary()
        component_sum = (
            s.commission_revenue + s.spread_revenue + s.challenge_revenue
            + s.withdrawal_fee_revenue + s.counterparty_pnl + s.adjustments
        )
        self.assertEqual(s.gross_broker_economic_result, component_sum)


class DoubleCountingTests(TestCase):
    def test_no_brokerledger_row_counted_twice_across_categories(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("10.00"))
        s = broker_economics_summary()
        self.assertEqual(s.commission_revenue, Decimal("10.00"))
        self.assertEqual(s.spread_revenue, Decimal("0.00"))
        self.assertEqual(s.counterparty_pnl, Decimal("0.00"))
        self.assertEqual(s.adjustments, Decimal("0.00"))

    def test_repeated_query_same_range_deterministic(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("7.00"))
        s1 = broker_economics_summary()
        s2 = broker_economics_summary()
        self.assertEqual(s1.gross_broker_economic_result, s2.gross_broker_economic_result)
        self.assertEqual(s1.ib_expense.net_paid, s2.ib_expense.net_paid)


class CapitalFlowsSeparationTests(TestCase):
    """Deposits/withdrawals must never leak into revenue."""

    def test_deposit_never_counted_as_revenue(self):
        user = make_user()
        make_deposit(user, amount_usd=Decimal("1000.00"), status=Deposit.STATUS_CONFIRMED)
        s = broker_economics_summary()
        self.assertEqual(s.gross_broker_economic_result, Decimal("0.00"))
        self.assertEqual(s.capital_flows.deposits_total, Decimal("1000.00"))

    def test_uncredited_deposit_not_counted_in_capital_flows(self):
        user = make_user()
        make_deposit(user, amount_usd=Decimal("500.00"), status=Deposit.STATUS_PENDING)
        s = broker_economics_summary()
        self.assertEqual(s.capital_flows.deposits_total, Decimal("0.00"))

    def test_withdrawal_principal_never_counted_as_revenue(self):
        user = make_user()
        WithdrawalRequest.objects.create(
            user=user, amount_usd=Decimal("200.00"), crypto_currency="btc",
            wallet_address="addr", status=WithdrawalRequest.STATUS_COMPLETED,
        )
        s = broker_economics_summary()
        self.assertEqual(s.gross_broker_economic_result, Decimal("0.00"))
        self.assertEqual(s.withdrawal_fee_revenue, Decimal("0.00"))
        self.assertEqual(s.capital_flows.withdrawals_total, Decimal("200.00"))

    def test_capital_flows_never_merges_into_retained_economics(self):
        user = make_user()
        make_deposit(user, amount_usd=Decimal("9999.00"), status=Deposit.STATUS_CONFIRMED)
        s = broker_economics_summary()
        self.assertEqual(s.retained.retained_economics, Decimal("0.00"))


class IBExpenseLifecycleTests(TestCase):
    def setUp(self):
        self.ib_owner = make_user()
        self.referral = _make_referral(self.ib_owner)
        self.trader = make_user()
        self.attribution = _make_attribution(self.trader, self.referral)
        self.rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))

    def test_ib_pending_only(self):
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("5.00"), status=IBCommissionObligation.ST_PENDING)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.pending, Decimal("5.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))

    def test_ib_paid_only(self):
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("5.00"), status=IBCommissionObligation.ST_CREDITED)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.credited, Decimal("5.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("5.00"))

    def test_pending_obligation_never_reported_as_paid(self):
        """Item 9 — pending/unpaid IB obligation is not falsely reported
        as paid expense."""
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("100.00"), status=IBCommissionObligation.ST_PENDING)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))
        self.assertEqual(s.ib_expense.credited, Decimal("0.00"))

    def test_approved_not_credited_not_counted_as_paid(self):
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("30.00"), status=IBCommissionObligation.ST_APPROVED)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.approved, Decimal("30.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))

    def test_cancelled_obligation_not_counted_anywhere(self):
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("40.00"), status=IBCommissionObligation.ST_CANCELLED)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.pending, Decimal("0.00"))
        self.assertEqual(s.ib_expense.approved, Decimal("0.00"))
        self.assertEqual(s.ib_expense.credited, Decimal("0.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))

    def test_ib_adjustment_reversal(self):
        """Credited obligation, then an EXECUTED reversal — net_paid
        must reflect the clawback exactly once."""
        ob = _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                               Decimal("100.00"), status=IBCommissionObligation.ST_CREDITED)
        IBCommissionAdjustment.objects.create(
            obligation=ob, referral=self.referral, adjustment_type=IBCommissionAdjustment.TYPE_REVERSAL,
            amount=Decimal("100.00"), reason="test reversal",
            status=IBCommissionAdjustment.ST_EXECUTED, executed_at=timezone.now(),
        )
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.credited, Decimal("100.00"))
        self.assertEqual(s.ib_expense.reversed_or_adjusted, Decimal("100.00"))
        self.assertEqual(s.ib_expense.net_paid, Decimal("0.00"))

    def test_ib_partial_adjustment(self):
        ob = _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                               Decimal("100.00"), status=IBCommissionObligation.ST_CREDITED)
        IBCommissionAdjustment.objects.create(
            obligation=ob, referral=self.referral, adjustment_type=IBCommissionAdjustment.TYPE_ADJUSTMENT,
            amount=Decimal("25.00"), reason="partial correction",
            status=IBCommissionAdjustment.ST_EXECUTED, executed_at=timezone.now(),
        )
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.net_paid, Decimal("75.00"))

    def test_pending_adjustment_not_yet_executed_does_not_reduce_net_paid(self):
        """A PENDING/APPROVED adjustment must NOT reduce net_paid — only
        EXECUTED adjustments have actually moved money."""
        ob = _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                               Decimal("100.00"), status=IBCommissionObligation.ST_CREDITED)
        IBCommissionAdjustment.objects.create(
            obligation=ob, referral=self.referral, adjustment_type=IBCommissionAdjustment.TYPE_REVERSAL,
            amount=Decimal("100.00"), reason="not yet executed",
            status=IBCommissionAdjustment.ST_PENDING,
        )
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.net_paid, Decimal("100.00"))

    def test_ib_expense_not_double_counted(self):
        """Item 8 — an IB obligation is not an expense twice."""
        _make_obligation(self.referral, self.attribution, self.rule, IBCommissionRule.RULE_PER_LOT,
                          Decimal("50.00"), status=IBCommissionObligation.ST_CREDITED)
        s1 = broker_economics_summary()
        s2 = broker_economics_summary()
        self.assertEqual(s1.ib_expense.net_paid, Decimal("50.00"))
        self.assertEqual(s2.ib_expense.net_paid, Decimal("50.00"))


class MultipleIBsAndClientsTests(TestCase):
    def test_multiple_ibs_isolated_in_breakdown(self):
        owner_a, owner_b = make_user(), make_user()
        ref_a, ref_b = _make_referral(owner_a), _make_referral(owner_b)
        trader_a, trader_b = make_user(), make_user()
        attr_a = _make_attribution(trader_a, ref_a)
        attr_b = _make_attribution(trader_b, ref_b)
        rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))
        _make_obligation(ref_a, attr_a, rule, IBCommissionRule.RULE_PER_LOT, Decimal("10.00"),
                          status=IBCommissionObligation.ST_CREDITED, source_event_id=1)
        _make_obligation(ref_b, attr_b, rule, IBCommissionRule.RULE_PER_LOT, Decimal("20.00"),
                          status=IBCommissionObligation.ST_CREDITED, source_event_id=2)
        rows = ib_expense_breakdown(group_by="referral")
        totals = {r.group_key: r.credited for r in rows}
        self.assertEqual(totals[str(ref_a.pk)], Decimal("10.00"))
        self.assertEqual(totals[str(ref_b.pk)], Decimal("20.00"))

    def test_multiple_clients_same_ib_summed_correctly(self):
        owner = make_user()
        ref = _make_referral(owner)
        trader_a, trader_b = make_user(), make_user()
        attr_a = _make_attribution(trader_a, ref)
        attr_b = _make_attribution(trader_b, ref)
        rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))
        _make_obligation(ref, attr_a, rule, IBCommissionRule.RULE_PER_LOT, Decimal("10.00"),
                          status=IBCommissionObligation.ST_CREDITED, source_event_id=1)
        _make_obligation(ref, attr_b, rule, IBCommissionRule.RULE_PER_LOT, Decimal("15.00"),
                          status=IBCommissionObligation.ST_CREDITED, source_event_id=2)
        s = broker_economics_summary()
        self.assertEqual(s.ib_expense.credited, Decimal("25.00"))


class AttributionLimitationTests(TestCase):
    """Item: unsupported symbol attribution — must return N/A, never a
    fabricated guess."""

    def test_challenge_percent_symbol_breakdown_is_not_attributable(self):
        owner = make_user()
        ref = _make_referral(owner)
        trader = make_user()
        attr = _make_attribution(trader, ref)
        rule = _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal("10.00"))
        _make_obligation(ref, attr, rule, IBCommissionRule.RULE_CHALLENGE_PERCENT, Decimal("50.00"),
                          status=IBCommissionObligation.ST_CREDITED,
                          source_event_type="challenge_enrollment", source_event_id=999)
        rows = ib_expense_breakdown(group_by="symbol")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].group_key, "NOT_ATTRIBUTABLE")
        self.assertEqual(rows[0].credited, Decimal("50.00"))

    def test_per_lot_symbol_breakdown_resolves_real_symbol(self):
        owner = make_user()
        ref = _make_referral(owner)
        trader = make_user()
        account = make_account(user=trader)
        attr = _make_attribution(trader, ref)
        rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))
        event = _make_lot_event(account, symbol="EUR/USD")
        _make_obligation(ref, attr, rule, IBCommissionRule.RULE_PER_LOT, Decimal("5.00"),
                          status=IBCommissionObligation.ST_CREDITED,
                          source_event_type="per_lot_execution", source_event_id=event.pk)
        rows = ib_expense_breakdown(group_by="symbol")
        self.assertEqual(rows[0].group_key, "EUR/USD")

    def test_unsupported_group_by_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            ib_expense_breakdown(group_by="account")


class ChallengeWithdrawEnumTests(TestCase):
    """BROKER-ECONOMICS-04A — Coverage Truth Correction. Both categories
    now have certified, LIVE, forward-only writers (challenge_revenue.py,
    withdrawal_economics.py) — the stale COVERAGE_POLICY_APPROVED_PENDING
    status ("no writer exists") is gone; COVERAGE_PARTIAL correctly
    reflects that a real writer exists but historical/legacy rows of
    unverifiable provenance may still be present in the sum."""

    def test_challenge_fee_zero_with_correct_coverage_status(self):
        s = broker_economics_summary()
        self.assertEqual(s.challenge_revenue, Decimal("0.00"))
        self.assertEqual(s.challenge_coverage.status, COVERAGE_PARTIAL)

    def test_withdraw_fee_zero_with_correct_coverage_status(self):
        s = broker_economics_summary()
        self.assertEqual(s.withdrawal_fee_revenue, Decimal("0.00"))
        self.assertEqual(s.withdrawal_fee_coverage.status, COVERAGE_PARTIAL)

    def test_challenge_coverage_no_longer_claims_no_writer(self):
        s = broker_economics_summary()
        note = s.challenge_coverage.note
        self.assertNotIn("No current production code path writes", note)
        self.assertIn("record_challenge_fee_revenue", note)
        self.assertIn("LIVE", note)

    def test_withdrawal_coverage_no_longer_claims_no_writer(self):
        s = broker_economics_summary()
        note = s.withdrawal_fee_coverage.note
        self.assertNotIn("No current production code path writes", note)
        self.assertNotIn("until a future WITHDRAWAL-ECONOMICS block", note)
        self.assertIn("record_withdrawal_fee_revenue", note)
        self.assertIn("LIVE", note)

    def test_withdrawal_coverage_reflects_04c4_provider_cost_capability(self):
        """BROKER-ECONOMICS-04C.4 — the coverage note no longer claims
        provider/network cost unconditionally "remains UNKNOWN": 04C.4
        gave it a real, certified recognition path (provider_cost_coverage).
        This note must still say the withdrawal fee figure itself is GROSS
        revenue, never withdrawal profit."""
        s = broker_economics_summary()
        note = s.withdrawal_fee_coverage.note
        self.assertNotIn("Provider/network cost remains UNKNOWN", note)
        self.assertIn("BROKER-ECONOMICS-04C.4", note)
        self.assertIn("does NOT mean the actual cost is", note)
        self.assertIn("never withdrawal profit", note)

    def test_retained_status_still_partial(self):
        """Confirms the coverage-note correction never flips Retained
        Economics from PARTIAL to an implied NET PROFIT claim."""
        s = broker_economics_summary()
        self.assertEqual(s.retained.status, COVERAGE_PARTIAL)

    def test_coverage_notes_invent_no_historical_date_or_backfill_claim(self):
        s = broker_economics_summary()
        for note in (s.challenge_coverage.note, s.withdrawal_fee_coverage.note):
            self.assertNotIn("backfill", note.lower())
            # No hardcoded calendar date (e.g. "2026-05-24") — provenance
            # is expressed via the source_*_enrollment/source_withdrawal
            # FK partition, never a fabricated go-live date.
            self.assertNotRegex(note, r"\b20\d{2}-\d{2}-\d{2}\b")

    def test_1_5_percent_policy_never_appears_as_computed_number(self):
        """The intended 1.5% withdrawal fee policy must never be
        hardcoded/computed anywhere in this module."""
        import inspect

        from simulator import broker_economics_summary as mod
        source = inspect.getsource(mod)
        self.assertNotIn("0.015", source)
        self.assertNotIn("Decimal(\"1.5\")", source)
        self.assertNotIn("* 0.015", source)


class RetainedEconomicsPartialTests(TestCase):
    def test_retained_economics_always_partial_today(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("100.00"))
        s = broker_economics_summary()
        self.assertEqual(s.retained.status, COVERAGE_PARTIAL)
        self.assertEqual(s.retained.coverage.status, COVERAGE_PARTIAL)

    def test_retained_economics_formula_exact(self):
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("100.00"))
        owner = make_user()
        ref = _make_referral(owner)
        trader = make_user()
        attr = _make_attribution(trader, ref)
        rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))
        _make_obligation(ref, attr, rule, IBCommissionRule.RULE_PER_LOT, Decimal("30.00"),
                          status=IBCommissionObligation.ST_CREDITED)
        s = broker_economics_summary()
        self.assertEqual(s.retained.retained_economics, Decimal("70.00"))

    def test_missing_variable_costs_never_hidden(self):
        s = broker_economics_summary()
        self.assertEqual(s.retained.known_execution_liquidity_costs, Decimal("0.00"))
        self.assertEqual(s.retained.known_payment_transaction_costs, Decimal("0.00"))
        self.assertEqual(s.retained.other_captured_variable_costs, Decimal("0.00"))
        self.assertIn("no data source", s.retained.coverage.note)


class PendingSpreadEstimateTests(TestCase):
    def test_no_trigger_opens_zero_estimate_complete_coverage(self):
        s = broker_economics_summary()
        self.assertEqual(s.pending_spread_estimate.trigger_open_count, 0)
        self.assertEqual(s.pending_spread_estimate.estimated_foregone_revenue, Decimal("0.00"))
        self.assertEqual(s.pending_spread_estimate.coverage.status, COVERAGE_COMPLETE)

    def test_estimate_is_partial_by_design_never_complete_when_nonzero(self):
        account = make_account()
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        _make_lot_event(account, symbol="EUR/USD", qty=Decimal("1.0"))
        s = broker_economics_summary()
        self.assertEqual(s.pending_spread_estimate.trigger_open_count, 1)
        self.assertEqual(s.pending_spread_estimate.coverage.status, COVERAGE_PARTIAL)
        self.assertIn("ESTIMATE", s.pending_spread_estimate.coverage.note)
        self.assertIn("NOT BOOKED REVENUE", s.pending_spread_estimate.coverage.note)

    def test_estimate_matches_certified_formula_exactly(self):
        account = make_account()
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        _make_lot_event(account, symbol="EUR/USD", qty=Decimal("1.0"))
        from simulator.spread_engine import calculate_spread_revenue
        expected = Decimal(str(calculate_spread_revenue("EUR/USD", 1.0, 2.0)))
        s = broker_economics_summary()
        self.assertEqual(s.pending_spread_estimate.estimated_foregone_revenue, expected)

    def test_estimate_never_added_to_gross_broker_economic_result(self):
        account = make_account()
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        _make_lot_event(account, symbol="EUR/USD", qty=Decimal("10.0"))
        s = broker_economics_summary()
        self.assertGreater(s.pending_spread_estimate.estimated_foregone_revenue, Decimal("0.00"))
        self.assertEqual(s.gross_broker_economic_result, Decimal("0.00"))

    def test_missing_spread_config_marks_partial_not_fabricated(self):
        account = make_account()
        # No BrokerSpreadConfig row for this symbol at all.
        _make_lot_event(account, symbol="XAU/USD", qty=Decimal("1.0"))
        s = broker_economics_summary()
        self.assertEqual(s.pending_spread_estimate.trigger_open_count, 1)
        self.assertIn("No config found", s.pending_spread_estimate.coverage.note)

    def test_manual_open_path_excluded_from_estimate(self):
        """Only ENTRY_PENDING_TRIGGER counts — a manual open must never
        inflate this estimate."""
        account = make_account()
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        _make_lot_event(account, symbol="EUR/USD", entry_path=LotExecutionEvent.ENTRY_MANUAL_WS)
        s = broker_economics_summary()
        self.assertEqual(s.pending_spread_estimate.trigger_open_count, 0)


class DateBoundaryTests(TestCase):
    def test_custom_period_excludes_rows_outside_range(self):
        old = make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("10.00"))
        BrokerLedger.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=30),
        )
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("5.00"))
        from simulator.broker_economics_summary import PERIOD_CUSTOM
        s = broker_economics_summary(
            period=PERIOD_CUSTOM, start=timezone.now() - timezone.timedelta(days=1), end=timezone.now(),
        )
        self.assertEqual(s.commission_revenue, Decimal("5.00"))

    def test_ib_expense_respects_same_date_boundary(self):
        owner = make_user()
        ref = _make_referral(owner)
        trader = make_user()
        attr = _make_attribution(trader, ref)
        rule = _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("5.00"))
        old_ob = _make_obligation(ref, attr, rule, IBCommissionRule.RULE_PER_LOT, Decimal("10.00"),
                                   status=IBCommissionObligation.ST_CREDITED)
        IBCommissionObligation.objects.filter(pk=old_ob.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=30),
        )
        from simulator.broker_economics_summary import PERIOD_CUSTOM
        s = broker_economics_summary(
            period=PERIOD_CUSTOM, start=timezone.now() - timezone.timedelta(days=1), end=timezone.now(),
        )
        self.assertEqual(s.ib_expense.credited, Decimal("0.00"))


class ZeroWritesTests(TestCase):
    def test_summary_call_creates_zero_rows(self):
        before_ledger = BrokerLedger.objects.count()
        before_obligation = IBCommissionObligation.objects.count()
        before_adjustment = BrokerEconomicAdjustment.objects.count()
        broker_economics_summary()
        ib_expense_breakdown(group_by="referral")
        ib_expense_breakdown(group_by="rule_type")
        ib_expense_breakdown(group_by="symbol")
        self.assertEqual(BrokerLedger.objects.count(), before_ledger)
        self.assertEqual(IBCommissionObligation.objects.count(), before_obligation)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), before_adjustment)


class CoverageStatusValidationTests(TestCase):
    def test_invalid_coverage_status_rejected(self):
        with self.assertRaises(ValueError):
            CategoryCoverage(status="MADE_UP_STATUS", note="x")

    def test_coverage_statuses_used_in_a_real_summary(self):
        """Confirms COMPLETE and PARTIAL — the statuses a real summary
        currently produces — actually appear, not vestigial. As of
        BROKER-ECONOMICS-04A, challenge/withdrawal coverage report
        PARTIAL (a certified writer exists; historical provenance is the
        only remaining ambiguity) rather than
        POLICY_APPROVED_IMPLEMENTATION_PENDING."""
        s = broker_economics_summary()
        statuses_seen = {
            s.commission_coverage.status, s.spread_coverage.status,
            s.challenge_coverage.status, s.withdrawal_fee_coverage.status,
            s.counterparty_coverage.status, s.adjustments_coverage.status,
            s.retained.coverage.status,
        }
        self.assertIn(COVERAGE_COMPLETE, statuses_seen)
        self.assertIn(COVERAGE_PARTIAL, statuses_seen)

    def test_policy_approved_pending_remains_valid_vocabulary(self):
        """POLICY_APPROVED_IMPLEMENTATION_PENDING is no longer produced by
        any current summary field (both categories that used it now have
        real writers — BROKER-ECONOMICS-04A), but it remains a legitimate,
        constructible status for a future category that is Owner-approved
        with no writer yet. Not removed, just currently unused."""
        cc = CategoryCoverage(status=COVERAGE_POLICY_APPROVED_PENDING, note="future category")
        self.assertEqual(cc.status, COVERAGE_POLICY_APPROVED_PENDING)
        s = broker_economics_summary()
        statuses_seen = {
            s.commission_coverage.status, s.spread_coverage.status,
            s.challenge_coverage.status, s.withdrawal_fee_coverage.status,
            s.counterparty_coverage.status, s.adjustments_coverage.status,
            s.retained.coverage.status,
        }
        self.assertNotIn(COVERAGE_POLICY_APPROVED_PENDING, statuses_seen)
