# simulator/tests/test_ib_commission_triggers_02a.py
"""
IB-COMMISSION-TRIGGERS-02A

Regression coverage for:
  - simulator/ib_commission.py::generate_challenge_percent_obligation()
  - simulator/ib_commission.py::generate_deposit_percent_obligation()
  - simulator/ib_commission_triggers.py (sweep_per_lot, sweep_challenge_percent,
    sweep_deposit_percent)
  - simulator/tasks.py::sweep_ib_commission_triggers_task

This block creates PENDING IBCommissionObligation rows ONLY — it never
credits a wallet, never creates a WalletTransaction/LedgerEntry/
TreasuryOperationRequest, and never modifies BrokerLedger. It also never
touches simulator/consumers.py, simulator/views.py, or
simulator/population_engine.py — every durable source row used here
(LotExecutionEvent, ChallengeEnrollment, Deposit) is created directly by
these tests, not via a real engine call, exactly like
test_ib_commission_engine_01.py's own established pattern.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.ib_commission import (
    generate_challenge_percent_obligation, generate_deposit_percent_obligation,
)
from simulator.ib_commission_triggers import (
    sweep_challenge_percent, sweep_deposit_percent, sweep_per_lot,
)
from simulator.models import (
    BrokerLedger, ChallengeEnrollment, ChallengeProduct, Deposit, IBCommissionObligation,
    IBCommissionRule, LedgerEntry, LotExecutionEvent, Referral, ReferralAttribution,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.tasks import sweep_ib_commission_triggers_task
from simulator.tests.factories import make_account, make_user
from simulator.wallet_ledger import get_or_create_wallet

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"trig02a{_seq}"


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


def _make_lot_event(account, qty="0.50", execution_price="1.10000",
                     symbol="EUR/USD", side="BUY", merged=False,
                     entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
                     created_at=None):
    ev = LotExecutionEvent.objects.create(
        account=account, position=None, symbol=symbol, side=side,
        qty=Decimal(qty), execution_price=Decimal(execution_price),
        merged=merged, entry_path=entry_path,
    )
    if created_at is not None:
        LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
        ev.refresh_from_db()
    return ev


def _make_challenge_product(price_usd="200.00"):
    global _seq
    _seq += 1
    return ChallengeProduct.objects.create(
        name=f"T02A-Product-{_seq}",
        account_size=Decimal("10000.00"),
        price_usd=Decimal(price_usd),
        is_active=True,
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=0,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=0,
        p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"),
        max_open_positions=5,
        profit_split_pct=Decimal("80.00"),
    )


def _make_enrollment(user, product, deposit=None, enrolled_at=None):
    enrollment = ChallengeEnrollment.objects.create(
        user=user, product=product, deposit=deposit,
    )
    if enrolled_at is not None:
        ChallengeEnrollment.objects.filter(pk=enrollment.pk).update(enrolled_at=enrolled_at)
        enrollment.refresh_from_db()
    return enrollment


def _make_deposit(user, amount_usd="1000.00", credited=True,
                   challenge_product=None, credited_at=None):
    deposit = Deposit.objects.create(
        user=user, amount_usd=Decimal(amount_usd), crypto_currency="btc",
        status="finished", credited=credited, challenge_product=challenge_product,
    )
    if credited_at is not None:
        Deposit.objects.filter(pk=deposit.pk).update(credited_at=credited_at)
        deposit.refresh_from_db()
    return deposit


# ─────────────────────────────────────────────────────────────────────────
# PER_LOT — via the new sweep layer
# ─────────────────────────────────────────────────────────────────────────

class PerLotSweepTests(TestCase):
    def _referred_setup(self, fixed_amount="10.00", qty="0.50", referral=None):
        ib_owner = make_user()
        referral = referral or _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        event = _make_lot_event(account, qty=qty)
        return referral, event

    def test_durable_event_creates_exactly_one_obligation(self):
        referral, event = self._referred_setup(fixed_amount="10.00", qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))

        cutoff = timezone.now() - timezone.timedelta(minutes=1)
        result = sweep_per_lot(cutoff)

        self.assertEqual(result["generated"], 1)
        obligations = IBCommissionObligation.objects.filter(
            rule_type=IBCommissionRule.RULE_PER_LOT, source_event_id=event.pk,
        )
        self.assertEqual(obligations.count(), 1)

    def test_correct_qty_basis(self):
        referral, event = self._referred_setup(qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))
        sweep_per_lot(timezone.now() - timezone.timedelta(minutes=1))
        obligation = IBCommissionObligation.objects.get(source_event_id=event.pk)
        self.assertEqual(obligation.basis_quantity, Decimal("0.50"))
        self.assertEqual(obligation.calculated_amount, Decimal("5.00"))

    def test_repeat_sweep_creates_no_duplicate(self):
        referral, event = self._referred_setup(qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))
        cutoff = timezone.now() - timezone.timedelta(minutes=1)

        sweep_per_lot(cutoff)
        second = sweep_per_lot(cutoff)

        self.assertEqual(second["generated"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(
            IBCommissionObligation.objects.filter(source_event_id=event.pk).count(), 1,
        )

    def test_no_attribution_no_obligation(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        event = _make_lot_event(account, qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))

        result = sweep_per_lot(timezone.now() - timezone.timedelta(minutes=1))
        self.assertEqual(result["generated"], 0)
        self.assertFalse(
            IBCommissionObligation.objects.filter(source_event_id=event.pk).exists(),
        )

    def test_no_rule_no_obligation(self):
        referral, event = self._referred_setup(qty="0.50")
        # no IBCommissionRule created at all
        result = sweep_per_lot(timezone.now() - timezone.timedelta(minutes=1))
        self.assertEqual(result["generated"], 0)

    def test_per_ib_override_wins_over_global_through_sweep(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))

        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("2.00"))  # global
        _make_rule(IBCommissionRule.RULE_PER_LOT, referral=referral, fixed_amount=Decimal("10.00"))  # override
        event = _make_lot_event(account, qty="1.00")

        sweep_per_lot(timezone.now() - timezone.timedelta(minutes=1))
        obligation = IBCommissionObligation.objects.get(source_event_id=event.pk)
        self.assertEqual(obligation.applied_fixed_rate, Decimal("10.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("10.00"))


# ─────────────────────────────────────────────────────────────────────────
# CHALLENGE_PERCENT
# ─────────────────────────────────────────────────────────────────────────

class ChallengePercentTests(TestCase):
    def _referred_setup(self, price_usd="200.00", percentage="10.00", with_deposit=True):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        product = _make_challenge_product(price_usd=price_usd)
        deposit = _make_deposit(trader, amount_usd=price_usd, challenge_product=product) if with_deposit else None
        enrollment = _make_enrollment(trader, product, deposit=deposit)
        _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal(percentage))
        return referral, enrollment

    def test_deposit_backed_enrollment_generates_obligation(self):
        referral, enrollment = self._referred_setup(price_usd="200.00", percentage="10.00")
        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.calculated_amount, Decimal("20.00"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_admin_manual_enrollment_deposit_none_generates_no_obligation(self):
        referral, enrollment = self._referred_setup(with_deposit=False)
        self.assertIsNone(enrollment.deposit_id)
        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertIsNone(obligation)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_correct_price_usd_basis(self):
        referral, enrollment = self._referred_setup(price_usd="350.00", percentage="10.00")
        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertEqual(obligation.basis_amount, Decimal("350.00"))
        self.assertEqual(obligation.applied_percentage_rate, Decimal("10.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("35.00"))

    def test_duplicate_sweep_creates_no_duplicate(self):
        referral, enrollment = self._referred_setup()
        cutoff = timezone.now() - timezone.timedelta(minutes=1)
        sweep_challenge_percent(cutoff)
        second = sweep_challenge_percent(cutoff)
        self.assertEqual(second["generated"], 0)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                rule_type=IBCommissionRule.RULE_CHALLENGE_PERCENT,
                source_event_id=enrollment.pk,
            ).count(),
            1,
        )

    def test_no_attribution_no_obligation(self):
        trader = make_user()
        product = _make_challenge_product()
        deposit = _make_deposit(trader, amount_usd="200.00", challenge_product=product)
        enrollment = _make_enrollment(trader, product, deposit=deposit)
        _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal("10.00"))
        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertIsNone(obligation)

    def test_no_applicable_rule_no_obligation(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        product = _make_challenge_product()
        deposit = _make_deposit(trader, amount_usd="200.00", challenge_product=product)
        enrollment = _make_enrollment(trader, product, deposit=deposit)
        # no rule created
        obligation = generate_challenge_percent_obligation(enrollment)
        self.assertIsNone(obligation)


# ─────────────────────────────────────────────────────────────────────────
# DEPOSIT_PERCENT
# ─────────────────────────────────────────────────────────────────────────

class DepositPercentTests(TestCase):
    def _referred_setup(self, amount_usd="1000.00", percentage="1.00", credited=True,
                         challenge_product=None):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        deposit = _make_deposit(
            trader, amount_usd=amount_usd, credited=credited,
            challenge_product=challenge_product,
        )
        _make_rule(IBCommissionRule.RULE_DEPOSIT_PERCENT, percentage=Decimal(percentage))
        return referral, deposit

    def test_credited_normal_deposit_generates_obligation(self):
        referral, deposit = self._referred_setup(amount_usd="1000.00", percentage="1.00")
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.calculated_amount, Decimal("10.00"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

    def test_uncredited_deposit_generates_no_obligation(self):
        referral, deposit = self._referred_setup(credited=False)
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNone(obligation)

    def test_challenge_deposit_generates_no_obligation(self):
        product = _make_challenge_product()
        referral, deposit = self._referred_setup(challenge_product=product)
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNone(obligation)

    def test_correct_amount_usd_basis(self):
        referral, deposit = self._referred_setup(amount_usd="2500.00", percentage="1.00")
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertEqual(obligation.basis_amount, Decimal("2500.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("25.00"))

    def test_duplicate_sweep_creates_no_duplicate(self):
        referral, deposit = self._referred_setup()
        Deposit.objects.filter(pk=deposit.pk).update(credited_at=timezone.now())
        cutoff = timezone.now() - timezone.timedelta(minutes=1)
        sweep_deposit_percent(cutoff)
        second = sweep_deposit_percent(cutoff)
        self.assertEqual(second["generated"], 0)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                rule_type=IBCommissionRule.RULE_DEPOSIT_PERCENT,
                source_event_id=deposit.pk,
            ).count(),
            1,
        )

    def test_no_attribution_no_obligation(self):
        trader = make_user()
        deposit = _make_deposit(trader, amount_usd="1000.00", credited=True)
        _make_rule(IBCommissionRule.RULE_DEPOSIT_PERCENT, percentage=Decimal("1.00"))
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNone(obligation)

    def test_no_applicable_rule_no_obligation(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        deposit = _make_deposit(trader, amount_usd="1000.00", credited=True)
        # no rule created
        obligation = generate_deposit_percent_obligation(deposit)
        self.assertIsNone(obligation)


# ─────────────────────────────────────────────────────────────────────────
# Cross-cutting: no money movement, idempotent reconciliation, Decimal math
# ─────────────────────────────────────────────────────────────────────────

class NoMoneyMovementTests(TestCase):
    def test_full_sweep_task_never_moves_money(self):
        # PER_LOT
        ib_owner_1 = make_user()
        referral_1 = _make_referral(ib_owner_1)
        trader_1 = make_user()
        _make_attribution(trader_1, referral_1)
        account_1 = make_account(user=trader_1, balance=Decimal("10000"))
        _make_lot_event(account_1, qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))

        # CHALLENGE_PERCENT
        ib_owner_2 = make_user()
        referral_2 = _make_referral(ib_owner_2)
        trader_2 = make_user()
        _make_attribution(trader_2, referral_2)
        product = _make_challenge_product(price_usd="200.00")
        deposit_2 = _make_deposit(trader_2, amount_usd="200.00", challenge_product=product)
        _make_enrollment(trader_2, product, deposit=deposit_2)
        _make_rule(IBCommissionRule.RULE_CHALLENGE_PERCENT, percentage=Decimal("10.00"))

        # DEPOSIT_PERCENT
        ib_owner_3 = make_user()
        referral_3 = _make_referral(ib_owner_3)
        trader_3 = make_user()
        _make_attribution(trader_3, referral_3)
        deposit_3 = _make_deposit(trader_3, amount_usd="1000.00", credited=True)
        Deposit.objects.filter(pk=deposit_3.pk).update(credited_at=timezone.now())
        _make_rule(IBCommissionRule.RULE_DEPOSIT_PERCENT, percentage=Decimal("1.00"))

        wtx_before = WalletTransaction.objects.count()
        ledger_before = LedgerEntry.objects.count()
        broker_ledger_before = BrokerLedger.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()

        result = sweep_ib_commission_triggers_task.apply(args=(60,)).get()

        self.assertGreaterEqual(result["per_lot"]["generated"], 1)
        self.assertGreaterEqual(result["challenge_percent"]["generated"], 1)
        self.assertGreaterEqual(result["deposit_percent"]["generated"], 1)

        obligations = IBCommissionObligation.objects.all()
        self.assertTrue(obligations.exists())
        for obligation in obligations:
            self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)
            self.assertIsNone(obligation.treasury_operation)
            self.assertIsNone(obligation.credited_at)

        self.assertEqual(WalletTransaction.objects.count(), wtx_before)
        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        self.assertEqual(BrokerLedger.objects.count(), broker_ledger_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)

        for referral, ib_owner in ((referral_1, ib_owner_1), (referral_2, ib_owner_2), (referral_3, ib_owner_3)):
            wallet, _ = get_or_create_wallet(ib_owner)
            self.assertEqual(wallet.available_balance, Decimal("0"))

    def test_overlapping_reconciliation_is_idempotent(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_lot_event(account, qty="0.50")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("10.00"))

        # Two overlapping windows, back to back.
        r1 = sweep_ib_commission_triggers_task.apply(args=(5,)).get()
        r2 = sweep_ib_commission_triggers_task.apply(args=(60,)).get()

        self.assertEqual(r1["per_lot"]["generated"], 1)
        self.assertEqual(r2["per_lot"]["generated"], 0, "second, wider overlapping sweep must not duplicate")
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_decimal_math_no_float_artifacts(self):
        ib_owner = make_user()
        referral = _make_referral(ib_owner)
        trader = make_user()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        event = _make_lot_event(account, qty="0.10")
        _make_rule(IBCommissionRule.RULE_PER_LOT, fixed_amount=Decimal("3.00"))

        sweep_per_lot(timezone.now() - timezone.timedelta(minutes=1))
        obligation = IBCommissionObligation.objects.get(source_event_id=event.pk)
        self.assertEqual(obligation.calculated_amount, Decimal("0.30"))
        self.assertIsInstance(obligation.calculated_amount, Decimal)
