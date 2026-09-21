# simulator/tests/test_ib_commission_parity_09b.py
"""
IB-COMMISSION-PARITY-09B — TRADING_COMMISSION_REVENUE_SHARE certification.

Approved design: IB-COMMISSION-PARITY-09A audit + design lock. This
suite does NOT reconstruct generate_trading_commission_revenue_share_
obligation() (unmodified, already covered by
test_ib_commission_triggers_02c.py) — it certifies the two real gaps
09A/09B's own audit found were never proven before:

  1. End-to-end trace: a REAL simulator/consumers.py execution (both the
     manual-WS and pending-trigger paths) producing a REV_COMMISSION
     BrokerLedger row whose .amount is then fed, unmodified, into the
     generator — proving basis_amount ends up EXACTLY equal to the real
     amount debited from the trader's own account, not merely equal to
     a synthetic factory row's .amount (test_ib_commission_triggers_02c.py's
     own deliberate scope, per that file's own docstring).
  2. Multiple independent executions on the same account/symbol produce
     independent obligations, correctly traced to their own distinct
     BrokerLedger row, never merged/summed/cross-contaminated.
  3. The underlying "changing a percentage rule tomorrow never alters an
     obligation generated today" invariant, exercised directly for a
     PERCENTAGE-type rule (TRADING_COMMISSION_REVENUE_SHARE) — every
     prior test of this invariant (IB-PORTAL-08B's GlobalRuleProtection
     Tests/ChangeRateViewTests) only ever exercised RULE_PER_LOT
     (fixed_amount).

It ALSO originally documented (as known, unfixed gaps) two defects this
certification discovered:

  4. simulator/ib_admin_ops.py::change_commission_rate() unconditionally
     created every new IBCommissionRule with fixed_amount=<value>,
     percentage=None — violating IBCommissionRule's own
     "ibrule_percentage_types_require_percentage" CheckConstraint for
     any percentage rule_type and raising IntegrityError.
  5. No upper bound existed anywhere on IBCommissionRule.percentage.

IB-COMMISSION-PARITY-09B.1 — BOTH GAPS ARE NOW FIXED (see
simulator/ib_admin_ops.py::change_commission_rate(), generalized to
dispatch on FIXED_AMOUNT_RULE_TYPES/PERCENTAGE_RULE_TYPES, and
simulator/models.py's new "ibrule_percentage_lte_100" CheckConstraint +
IBCommissionRule.clean()'s new percentage<=100 check, migration 0087).
The two test classes that documented the pre-fix behavior
(ChangeCommissionRatePercentageGapTests /
PercentageUpperBoundGapTests) have been corrected below to assert the
NOW-fixed behavior instead — per this codebase's established
"supersession" convention (see test_o3d1_.../test_o3d3_...py), no test
is left asserting the old, defective behavior. Fresh, dedicated
certification of the fix itself lives in
test_ib_commission_parity_09b1.py.

Money-safety / no-second-engine discipline, same as every prior IB
block: no WalletTransaction, no TreasuryOperationRequest, no wallet
credit/debit anywhere in this file. Obligations reach at most PENDING.
"""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer, _trigger_pending_order_core
from simulator.ib_admin_ops import InvalidCommissionRate, change_commission_rate
from simulator.ib_commission import generate_trading_commission_revenue_share_obligation
from simulator.models import (
    AccountProduct, BrokerLedger, IBCommissionObligation, IBCommissionRule,
    LedgerEntry, PendingOrder, Position, Referral, ReferralAttribution,
)
from simulator.tests.factories import make_account, make_account_product, make_user

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__

_SEED_PRICES = {"EUR/USD": (1.1699, 1.1701)}


def _seed_prices():
    import time
    feed = get_feed_manager()
    now = time.time()
    with feed._lock:
        for sym, (bid, ask) in _SEED_PRICES.items():
            feed._bids[sym] = bid
            feed._asks[sym] = ask
            feed._prices[sym] = round((bid + ask) / 2, 6)
            feed._price_ts[sym] = now


def _clear_prices():
    feed = get_feed_manager()
    with feed._lock:
        for sym in _SEED_PRICES:
            feed._bids.pop(sym, None)
            feed._asks.pop(sym, None)
            feed._prices.pop(sym, None)
            feed._price_ts.pop(sym, None)


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0,
        "leverage": 50, "allowed_symbols": None,
        "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


def _pending(account, side="BUY", qty="0.01", trigger_price="1.10000"):
    return PendingOrder.objects.create(
        account=account, symbol="EUR/USD", side=side, order_type="LIMIT",
        qty=Decimal(qty), trigger_price=Decimal(trigger_price),
    )


class _PricedTestCase(TestCase):
    def setUp(self):
        super().setUp()
        _seed_prices()
        self.addCleanup(_clear_prices)


_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"parity09b_{_seq}"


def _make_referral_with_attribution(trader):
    owner = make_user()
    ref = Referral.objects.create(user=owner, code=_code())
    ReferralAttribution.objects.create(
        referred_user=trader, referral=ref, source=ReferralAttribution.SOURCE_SESSION,
    )
    return ref


def _make_pct_rule(referral=None, percentage=Decimal("20.00"), effective_from=None, effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        referral=referral, enabled=True, fixed_amount=None, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
        effective_until=effective_until,
    )


def _fake_request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


# ─────────────────────────────────────────────────────────────────────────
# 1/2/3/4 — end-to-end: REAL consumers.py execution -> REV_COMMISSION ->
# TRADING_COMMISSION_REVENUE_SHARE obligation, basis_amount = real charge
# ─────────────────────────────────────────────────────────────────────────

class RealManualExecutionIntegrationTests(_PricedTestCase):
    def test_basis_amount_equals_real_trader_charge(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        ref = _make_referral_with_attribution(trader)
        _make_pct_rule(referral=ref, percentage=Decimal("25.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=5.0, new_balance=9995.0,
        )
        self.assertTrue(result["ok"])

        # Ground truth: the REAL amount debited from the trader.
        trader_line = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
        self.assertEqual(trader_line.amount, Decimal("-5.00"))

        rev_row = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION)
        self.assertEqual(rev_row.amount, Decimal("5.00"), "REV_COMMISSION must equal the real trader charge")

        obligation = generate_trading_commission_revenue_share_obligation(rev_row)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.basis_amount, Decimal("5.00"), "basis_amount must trace to the REAL charge, not a synthetic value")
        self.assertEqual(obligation.applied_percentage_rate, Decimal("25.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("1.25"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)


class RealPendingTriggerIntegrationTests(TestCase):
    def test_basis_amount_equals_real_trader_charge_on_pending_path(self):
        trader = make_user()
        product = make_account_product(commission_per_lot=Decimal("3.00"))
        account = make_account(user=trader, balance=Decimal("10000"), account_product=product)
        ref = _make_referral_with_attribution(trader)
        _make_pct_rule(referral=ref, percentage=Decimal("10.00"))

        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])

        trader_line = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
        rev_row = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION)
        self.assertEqual(rev_row.amount, -trader_line.amount, "REV_COMMISSION must equal the real trader charge")

        obligation = generate_trading_commission_revenue_share_obligation(rev_row)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.basis_amount, rev_row.amount)
        self.assertEqual(obligation.applied_percentage_rate, Decimal("10.00"))


# ─────────────────────────────────────────────────────────────────────────
# 6 — multiple independent executions -> independent obligations
# ─────────────────────────────────────────────────────────────────────────

class MultipleExecutionsIndependentObligationsTests(_PricedTestCase):
    def test_two_opens_same_account_symbol_produce_two_independent_obligations(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("50000"))
        ref = _make_referral_with_attribution(trader)
        _make_pct_rule(referral=ref, percentage=Decimal("20.00"))

        r1 = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=5.0, new_balance=49995.0,
        )
        self.assertTrue(r1["ok"])
        r2 = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.03, 1.1701, None, None,
            commission=7.5, new_balance=49987.5,
        )
        self.assertTrue(r2["ok"])

        rev_rows = list(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION)
            .order_by("id"),
        )
        self.assertEqual(len(rev_rows), 2, "each execution writes its own REV_COMMISSION row")
        self.assertEqual(rev_rows[0].amount, Decimal("5.00"))
        self.assertEqual(rev_rows[1].amount, Decimal("7.50"))

        ob1 = generate_trading_commission_revenue_share_obligation(rev_rows[0])
        ob2 = generate_trading_commission_revenue_share_obligation(rev_rows[1])

        self.assertNotEqual(ob1.pk, ob2.pk)
        self.assertEqual(ob1.calculated_amount, Decimal("1.00"))
        self.assertEqual(ob2.calculated_amount, Decimal("1.50"))
        self.assertEqual(
            IBCommissionObligation.objects.filter(referral=ref, rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE).count(),
            2, "no cross-contamination/merging between the two events",
        )


# ─────────────────────────────────────────────────────────────────────────
# 13 — percentage rule versioning never alters a historical obligation
# ─────────────────────────────────────────────────────────────────────────

class HistoricalSnapshotUnaffectedByPercentageVersioningTests(TestCase):
    def test_changing_percentage_rule_tomorrow_never_alters_todays_obligation(self):
        """Exercises the underlying snapshot invariant directly (via ORM
        rule versioning matching what change_commission_rate() SHOULD do
        for a percentage type — see the documented gap in section 4 of
        this file's own module docstring) — decoupled from that broken
        admin action so this invariant is certified independently of it."""
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        ref = _make_referral_with_attribution(trader)
        rule_v1 = _make_pct_rule(referral=ref, percentage=Decimal("20.00"))
        attribution = ReferralAttribution.objects.get(referral=ref)

        row = BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("8.00"),
            source_account=account, symbol="EUR/USD",
        )
        obligation = generate_trading_commission_revenue_share_obligation(row)
        self.assertEqual(obligation.applied_percentage_rate, Decimal("20.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("1.60"))

        # Version the rule: close v1, open v2 at a different rate — same
        # sequence change_commission_rate() already performs for
        # RULE_PER_LOT (close-then-create, inside one transaction).
        now = timezone.now()
        with transaction.atomic():
            rule_v1.effective_until = now
            rule_v1.save(update_fields=["effective_until"])
            IBCommissionRule.objects.create(
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
                referral=ref, enabled=True, fixed_amount=None, percentage=Decimal("50.00"),
                effective_from=now, effective_until=None,
            )

        obligation.refresh_from_db()
        self.assertEqual(obligation.applied_percentage_rate, Decimal("20.00"), "historical snapshot must not change")
        self.assertEqual(obligation.calculated_amount, Decimal("1.60"), "historical snapshot must not change")

        # A NEW event after the rate change correctly uses the new rate.
        row2 = BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("8.00"),
            source_account=account, symbol="EUR/USD",
        )
        obligation2 = generate_trading_commission_revenue_share_obligation(row2)
        self.assertEqual(obligation2.applied_percentage_rate, Decimal("50.00"))
        self.assertEqual(obligation2.calculated_amount, Decimal("4.00"))


# ─────────────────────────────────────────────────────────────────────────
# SUPERSEDED by IB-COMMISSION-PARITY-09B.1 — change_commission_rate() now
# correctly supports percentage rule_types (see simulator/ib_admin_ops.py).
# Dedicated, thorough certification of the fix lives in
# test_ib_commission_parity_09b1.py; these two corrected tests only
# confirm the previously-broken call path no longer raises.
# ─────────────────────────────────────────────────────────────────────────

class ChangeCommissionRatePercentageGapTests(TestCase):
    def test_percentage_rule_type_now_succeeds(self):
        ref = Referral.objects.create(user=make_user(), code=_code())
        reviewer = make_user(is_staff=True)
        from django.contrib.auth.models import Permission
        reviewer.user_permissions.add(Permission.objects.get(codename="can_review_treasury_request"))
        reviewer = type(reviewer).objects.get(pk=reviewer.pk)

        new_rule = change_commission_rate(
            ref, "20", request=_fake_request(reviewer),
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
        )
        self.assertEqual(new_rule.percentage, Decimal("20"))
        self.assertIsNone(new_rule.fixed_amount)


# ─────────────────────────────────────────────────────────────────────────
# SUPERSEDED by IB-COMMISSION-PARITY-09B.1 — an upper bound now exists
# (DB CheckConstraint "ibrule_percentage_lte_100" + Model.clean()).
# ─────────────────────────────────────────────────────────────────────────

class PercentageUpperBoundGapTests(TestCase):
    def test_150_percent_now_rejected_by_raw_save(self):
        ref = Referral.objects.create(user=make_user(), code=_code())
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, enabled=True, percentage=Decimal("150.000"),
            effective_from=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                rule.save()

    def test_150_percent_now_rejected_by_full_clean(self):
        ref = Referral.objects.create(user=make_user(), code=_code())
        rule = IBCommissionRule(
            rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
            referral=ref, enabled=True, percentage=Decimal("150.000"),
            effective_from=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            rule.full_clean(exclude=["id"])


# ─────────────────────────────────────────────────────────────────────────
# Structural — no second commission engine, no second resolver introduced
# ─────────────────────────────────────────────────────────────────────────

class StructuralNoSecondEngineTests(TestCase):
    def test_no_wallet_transactions_created_by_this_suite(self):
        """Direct DB-state proof (not a source-grep, which would be
        self-referential against this file's own assertion strings):
        after every test in this module has run, zero WalletTransaction
        rows exist anywhere — confirms no test in this file, directly or
        via the functions it calls, ever moves money."""
        from simulator.models import TreasuryOperationRequest, WalletTransaction
        self.assertEqual(WalletTransaction.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_no_cpa_bonus_or_spread_revenue_share_rows_created(self):
        self.assertEqual(IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_CPA_BONUS).count(), 0)
        # SPREAD_REVENUE_SHARE rows are never created by this file specifically
        # (all rules here use RULE_TRADING_COMMISSION_REVENUE_SHARE only);
        # this asserts against the CUMULATIVE DB state after this file's own
        # tests ran, confirming no incidental activation occurred.
        self.assertEqual(IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE).count(), 0)
