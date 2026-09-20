# simulator/tests/test_ib_commission_triggers_02c.py
"""
IB-COMMISSION-TRIGGERS-02C

Regression coverage for:
  - simulator/ib_commission.py::generate_trading_commission_revenue_share_obligation()
  - simulator/ib_commission_triggers.py::sweep_trading_commission_revenue_share()
  - simulator/tasks.py::sweep_ib_commission_triggers_task (extended)

This block creates PENDING IBCommissionObligation rows ONLY — it never
credits a wallet, never creates a WalletTransaction/LedgerEntry/
TreasuryOperationRequest, and never creates a new BrokerLedger row. It
also never touches simulator/consumers.py or simulator/population_engine.py
— every BrokerLedger REV_COMMISSION row used here is created directly by
these tests via the existing make_broker_ledger() factory, not via a
real engine call, exactly like test_ib_commission_triggers_02a.py's own
established pattern.

The authoritative economic source is BrokerLedger.amount — these tests
deliberately never construct qty/price/contract_size/commission-rate
anywhere, to prove the generator cannot be recomputing trading
commission independently (it has nothing to recompute it FROM).
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.ib_commission import (
    generate_trading_commission_revenue_share_obligation,
)
from simulator.ib_commission_triggers import sweep_trading_commission_revenue_share
from simulator.models import (
    BrokerLedger, IBCommissionObligation, IBCommissionRule, LedgerEntry,
    Referral, ReferralAttribution, TreasuryOperationRequest, WalletTransaction,
)
from simulator.tasks import sweep_ib_commission_triggers_task
from simulator.tests.factories import make_account, make_broker_ledger, make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers — same shape as test_ib_commission_triggers_02a.py, duplicated
# locally so this file has no import dependency on that module.
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"trig02c{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type, referral=None, enabled=True, fixed_amount=None,
               percentage=None, effective_from=None, effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
        effective_until=effective_until,
    )


def _make_commission_ledger(account, amount="7.00", created_at=None, source_ledger=None):
    row = make_broker_ledger(
        revenue_type=BrokerLedger.REV_COMMISSION,
        amount=Decimal(amount),
        source_account=account,
        source_ledger=source_ledger,
        symbol="EUR/USD",
    )
    if created_at is not None:
        BrokerLedger.objects.filter(pk=row.pk).update(created_at=created_at)
        row.refresh_from_db()
    return row


def _referred_setup(percentage="20.00", amount="7.00"):
    """Standard attributed trader + global rule + one REV_COMMISSION row,
    with the rule created BEFORE the row (see IB-COMMISSION-ENGINE-01's
    own documented effective_from-ordering fix — avoids the row's
    created_at landing before the rule's effective_from)."""
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    account = make_account(user=trader, balance=Decimal("10000"))
    rule = _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal(percentage))
    row = _make_commission_ledger(account, amount=amount)
    return {
        "ib_owner": ib_owner, "referral": referral, "trader": trader,
        "account": account, "rule": rule, "row": row,
    }


# ─────────────────────────────────────────────────────────────────────────
# 1/2/3/4 — valid generation, basis, percentage math, snapshot fields
# ─────────────────────────────────────────────────────────────────────────

class GeneratorSuccessTests(TestCase):
    def test_valid_row_creates_exactly_one_pending_obligation(self):
        ctx = _referred_setup(percentage="20.00", amount="7.00")
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            ).count(),
            1,
        )

    def test_basis_amount_exactly_equals_broker_ledger_amount(self):
        ctx = _referred_setup(percentage="20.00", amount="7.00")
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.basis_amount, Decimal("7.00"))
        self.assertEqual(obligation.basis_amount, ctx["row"].amount)

    def test_percentage_calculation_matches_worked_example(self):
        # BrokerLedger.amount = 7.00, rule = 20% -> obligation = 1.40
        ctx = _referred_setup(percentage="20.00", amount="7.00")
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.calculated_amount, Decimal("1.40"))

    def test_snapshot_fields_correct(self):
        ctx = _referred_setup(percentage="15.500", amount="12.34")
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.applied_percentage_rate, Decimal("15.500"))
        self.assertIsNone(obligation.applied_fixed_rate)
        self.assertIsNone(obligation.basis_quantity)
        self.assertEqual(obligation.attribution.referred_user_id, ctx["trader"].id)
        self.assertEqual(obligation.referral_id, ctx["referral"].id)
        self.assertEqual(obligation.rule_id, ctx["rule"].id)
        self.assertEqual(obligation.rule_type, IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE)
        self.assertEqual(obligation.source_event_type, "broker_ledger_commission")
        self.assertEqual(obligation.source_event_id, ctx["row"].pk)
        self.assertEqual(obligation.currency, "USD")
        self.assertIn(str(ctx["row"].pk), obligation.source_reference)


# ─────────────────────────────────────────────────────────────────────────
# 5 — no independent recalculation from qty/price/contract size
# ─────────────────────────────────────────────────────────────────────────

class NoIndependentRecalculationTests(TestCase):
    def test_obligation_amount_derives_only_from_broker_ledger_amount(self):
        # Two rows with wildly different implied qty/price (unknowable to
        # the generator — no such fields exist on BrokerLedger) but the
        # SAME amount must produce the SAME calculated_amount.
        ctx1 = _referred_setup(percentage="20.00", amount="7.00")
        ob1 = generate_trading_commission_revenue_share_obligation(ctx1["row"])

        ib_owner2 = make_user()
        referral2 = _make_referral(ib_owner2)
        trader2 = make_user()
        _make_attribution(trader2, referral2)
        account2 = make_account(user=trader2, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=referral2, percentage=Decimal("20.00"))
        row2 = _make_commission_ledger(account2, amount="7.00", source_ledger=None)
        ob2 = generate_trading_commission_revenue_share_obligation(row2)

        self.assertEqual(ob1.calculated_amount, ob2.calculated_amount, "amount must derive only from BrokerLedger.amount")


# ─────────────────────────────────────────────────────────────────────────
# 6/7/8/9 — attribution / rule resolution edge cases
# ─────────────────────────────────────────────────────────────────────────

class RuleResolutionTests(TestCase):
    def test_no_attribution_no_obligation(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal("20.00"))
        row = _make_commission_ledger(account, amount="7.00")
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_no_rule_no_obligation(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        row = _make_commission_ledger(account, amount="7.00")
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))

    def test_disabled_rule_no_obligation(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, enabled=False, percentage=Decimal("20.00"))
        row = _make_commission_ledger(account, amount="7.00")
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))

    def test_per_ib_override_beats_global(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, percentage=Decimal("10.00"))
        _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, referral=referral, percentage=Decimal("50.00"))
        row = _make_commission_ledger(account, amount="10.00")
        obligation = generate_trading_commission_revenue_share_obligation(row)
        self.assertEqual(obligation.applied_percentage_rate, Decimal("50.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("5.00"))


# ─────────────────────────────────────────────────────────────────────────
# 10/11/12 — idempotency: repeat generator, repeat sweep, overlapping sweeps
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def test_repeated_generator_call_no_duplicate(self):
        ctx = _referred_setup()
        ob1 = generate_trading_commission_revenue_share_obligation(ctx["row"])
        ob2 = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertEqual(ob1.pk, ob2.pk)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_repeated_sweep_no_duplicate(self):
        ctx = _referred_setup()
        r1 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        r2 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0)
        self.assertEqual(r2["skipped"], 1)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_overlapping_sweep_windows_no_duplicate(self):
        ctx = _referred_setup()
        r1 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=5))
        r2 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0, "wider overlapping sweep must not duplicate")
        self.assertEqual(IBCommissionObligation.objects.count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 13/14/15/16/17 — rejection cases
# ─────────────────────────────────────────────────────────────────────────

class RejectionTests(TestCase):
    def test_wrong_revenue_type_rev_spread_no_obligation(self):
        ctx = _referred_setup()
        spread_row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("3.00"),
            source_account=ctx["account"],
        )
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(spread_row))
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_wrong_revenue_type_other_types_no_obligation(self):
        ctx = _referred_setup()
        for rt in (BrokerLedger.REV_CHALLENGE_FEE, BrokerLedger.REV_WITHDRAW_FEE,
                   BrokerLedger.REV_ADJUSTMENT, BrokerLedger.REV_COUNTERPARTY_PNL):
            row = make_broker_ledger(revenue_type=rt, amount=Decimal("3.00"), source_account=ctx["account"])
            self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_zero_amount_no_obligation(self):
        ctx = _referred_setup()
        row = _make_commission_ledger(ctx["account"], amount="0.00")
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))

    def test_negative_amount_no_obligation(self):
        ctx = _referred_setup()
        row = _make_commission_ledger(ctx["account"], amount="-5.00")
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))

    def test_missing_source_account_no_obligation(self):
        ctx = _referred_setup()
        row = _make_commission_ledger(ctx["account"], amount="7.00")
        BrokerLedger.objects.filter(pk=row.pk).update(source_account=None)
        row.refresh_from_db()
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(row))

    def test_missing_source_ledger_still_creates(self):
        # Design Lock: source_ledger is not used for attribution — a
        # REV_COMMISSION row with no linked trader LedgerEntry must still
        # generate normally.
        ctx = _referred_setup()
        self.assertIsNone(ctx["row"].source_ledger_id)
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)


# ─────────────────────────────────────────────────────────────────────────
# 18-23 — money-safety
# ─────────────────────────────────────────────────────────────────────────

class NoMoneyMovementTests(TestCase):
    def test_generator_moves_no_money(self):
        ctx = _referred_setup()
        wtx_before = WalletTransaction.objects.count()
        ledger_before = LedgerEntry.objects.count()
        broker_ledger_before = BrokerLedger.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()
        account_balance_before = ctx["account"].balance

        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)

        self.assertEqual(WalletTransaction.objects.count(), wtx_before)
        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        self.assertEqual(BrokerLedger.objects.count(), broker_ledger_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)

        ctx["account"].refresh_from_db()
        self.assertEqual(ctx["account"].balance, account_balance_before)

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        self.assertEqual(wallet.available_balance, Decimal("0"))

    def test_sweep_task_moves_no_money(self):
        ctx = _referred_setup()
        wtx_before = WalletTransaction.objects.count()
        ledger_before = LedgerEntry.objects.count()
        broker_ledger_before = BrokerLedger.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()

        result = sweep_ib_commission_triggers_task.apply(args=(60,)).get()
        self.assertGreaterEqual(result["trading_commission_revenue_share"]["generated"], 1)

        self.assertEqual(WalletTransaction.objects.count(), wtx_before)
        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        self.assertEqual(BrokerLedger.objects.count(), broker_ledger_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)

        wallet, _ = get_or_create_wallet(ctx["ib_owner"])
        self.assertEqual(wallet.available_balance, Decimal("0"))


# ─────────────────────────────────────────────────────────────────────────
# 24/25 — existing PER_LOT/CHALLENGE/DEPOSIT + task result shape unchanged
# ─────────────────────────────────────────────────────────────────────────

class TaskExtensionNonRegressionTests(TestCase):
    def test_task_result_includes_new_key_without_changing_others(self):
        result = sweep_ib_commission_triggers_task.apply(args=(30,)).get()
        self.assertIn("per_lot", result)
        self.assertIn("challenge_percent", result)
        self.assertIn("deposit_percent", result)
        self.assertIn("trading_commission_revenue_share", result)
        self.assertIn("elapsed_ms", result)
        for key in ("per_lot", "challenge_percent", "deposit_percent", "trading_commission_revenue_share"):
            self.assertEqual(set(result[key].keys()), {"scanned", "generated", "skipped"})
        # No REV_COMMISSION data present -> the new sweep contributes zero,
        # and the pre-existing three sweeps are entirely unaffected by its
        # presence in the same task body.
        self.assertEqual(result["per_lot"], {"scanned": 0, "generated": 0, "skipped": 0})
        self.assertEqual(result["challenge_percent"], {"scanned": 0, "generated": 0, "skipped": 0})
        self.assertEqual(result["deposit_percent"], {"scanned": 0, "generated": 0, "skipped": 0})
