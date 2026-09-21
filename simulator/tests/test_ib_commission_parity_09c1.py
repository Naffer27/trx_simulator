# simulator/tests/test_ib_commission_parity_09c1.py
"""
IB-COMMISSION-PARITY-09C.1 — Spread Revenue Share generator + sweep.

Regression coverage for:
  - simulator/ib_commission.py::generate_spread_revenue_share_obligation()
  - simulator/ib_commission_triggers.py::sweep_spread_revenue_share()
  - simulator/tasks.py::sweep_ib_commission_triggers_task (extended)

Approved design: IB-COMMISSION-PARITY-09C audit + design lock (Option A —
BrokerLedger.REV_SPREAD is already sufficient SSOT, zero migration).
This suite mirrors test_ib_commission_triggers_02c.py's exact shape for
the sibling TRADING_COMMISSION_REVENUE_SHARE generator — same fixture
patterns, same structural proofs, same money-safety discipline — not a
second architecture.

Money-safety / no-second-engine discipline, same as every prior IB
block: no WalletTransaction, no TreasuryOperationRequest created
directly, no wallet credit/debit anywhere in this file except where the
real end-to-end Treasury pipeline is deliberately exercised (Treasury/
reversal compatibility tests), via the existing, unmodified services.

The generator/sweep here never construct qty/price/contract_size/pips —
every test deliberately avoids those, proving the generator cannot be
recomputing spread revenue independently (nothing to recompute it FROM).
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.ib_commission import generate_spread_revenue_share_obligation
from simulator.ib_commission_reversal import (
    approve_adjustment, link_adjustment_to_treasury, remaining_reversible,
    submit_adjustment, sync_adjustment_from_treasury,
)
from simulator.ib_commission_triggers import sweep_spread_revenue_share
from simulator.ib_risk_holds import freeze_referral
from simulator.ib_treasury_settlement import (
    approve_obligation, link_treasury_request, sync_obligation_from_treasury,
)
from simulator.models import (
    AuditLog, BrokerLedger, IBCommissionAdjustment, IBCommissionObligation,
    IBCommissionRule, LedgerEntry, Position, Referral, ReferralAttribution,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests
from simulator.tasks import sweep_ib_commission_triggers_task
from simulator.tests.factories import make_account, make_broker_ledger, make_spread_config, make_user
from simulator.treasury_requests import approve_treasury_request, execute_treasury_request

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"parity09c1_{_seq}"


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


def _make_rule(referral=None, enabled=True, percentage=Decimal("20.00"),
               effective_from=None, effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=referral,
        enabled=enabled, fixed_amount=None, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
        effective_until=effective_until,
    )


def _make_spread_ledger(account, amount="7.00", created_at=None, source_ledger=None):
    row = make_broker_ledger(
        revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal(amount),
        source_account=account, source_ledger=source_ledger, symbol="EUR/USD",
    )
    if created_at is not None:
        BrokerLedger.objects.filter(pk=row.pk).update(created_at=created_at)
        row.refresh_from_db()
    return row


def _referred_setup(percentage="20.00", amount="7.00"):
    """Standard attributed trader + global rule + one REV_SPREAD row,
    rule created BEFORE the row (avoids the row's created_at landing
    before the rule's effective_from — same ordering discipline
    test_ib_commission_triggers_02c.py's own _referred_setup() uses)."""
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    account = make_account(user=trader, balance=Decimal("10000"))
    rule = _make_rule(percentage=Decimal(percentage))
    row = _make_spread_ledger(account, amount=amount)
    return {
        "ib_owner": ib_owner, "referral": referral, "trader": trader,
        "account": account, "rule": rule, "row": row,
    }


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


# ─────────────────────────────────────────────────────────────────────────
# A — Happy path
# ─────────────────────────────────────────────────────────────────────────

class HappyPathTests(TestCase):
    def test_5_dollars_at_20_percent_yields_1_dollar(self):
        ctx = _referred_setup(percentage="20.00", amount="5.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.basis_amount, Decimal("5.00"))
        self.assertEqual(obligation.applied_percentage_rate, Decimal("20.00"))
        self.assertEqual(obligation.calculated_amount, Decimal("1.00"))
        self.assertEqual(obligation.rule_type, IBCommissionRule.RULE_SPREAD_REVENUE_SHARE)
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)
        self.assertEqual(obligation.referral_id, ctx["referral"].pk)

    def test_basis_amount_exactly_equals_broker_ledger_amount(self):
        ctx = _referred_setup(amount="7.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.basis_amount, ctx["row"].amount)

    def test_no_independent_recalculation(self):
        """Deliberately never constructs qty/price/contract_size/pips
        anywhere in this test — proves the generator has nothing to
        recompute spread revenue FROM."""
        ctx = _referred_setup(percentage="33.00", amount="9.17")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        expected = (Decimal("9.17") * Decimal("33.00") / Decimal("100")).quantize(Decimal("0.01"))
        self.assertEqual(obligation.calculated_amount, expected)


# ─────────────────────────────────────────────────────────────────────────
# B — Historical snapshot across a rate version change
# ─────────────────────────────────────────────────────────────────────────

class HistoricalSnapshotTests(TestCase):
    def test_old_obligation_unaffected_by_later_rate_change(self):
        ctx = _referred_setup(percentage="20.00", amount="5.00")
        old_obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(old_obligation.calculated_amount, Decimal("1.00"))

        # Version the rule: close v1, open v2 — same close-then-create
        # sequence change_commission_rate() already performs.
        now = timezone.now()
        ctx["rule"].effective_until = now
        ctx["rule"].save(update_fields=["effective_until"])
        IBCommissionRule.objects.create(
            rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, referral=ctx["referral"],
            enabled=True, fixed_amount=None, percentage=Decimal("30.00"),
            effective_from=now, effective_until=None,
        )

        old_obligation.refresh_from_db()
        self.assertEqual(old_obligation.basis_amount, Decimal("5.00"))
        self.assertEqual(old_obligation.applied_percentage_rate, Decimal("20.00"))
        self.assertEqual(old_obligation.calculated_amount, Decimal("1.00"))

        new_row = _make_spread_ledger(ctx["account"], amount="8.00")
        new_obligation = generate_spread_revenue_share_obligation(new_row)
        self.assertEqual(new_obligation.applied_percentage_rate, Decimal("30.00"))
        self.assertEqual(new_obligation.calculated_amount, Decimal("2.40"))


# ─────────────────────────────────────────────────────────────────────────
# C/D/E — idempotency: generator repeat, sweep repeat, overlapping windows
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def test_repeated_generator_call_no_duplicate(self):
        ctx = _referred_setup()
        ob1 = generate_spread_revenue_share_obligation(ctx["row"])
        ob2 = generate_spread_revenue_share_obligation(ctx["row"])
        ob3 = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(ob1.pk, ob2.pk)
        self.assertEqual(ob2.pk, ob3.pk)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                referral=ctx["referral"], rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            ).count(), 1,
        )

    def test_repeated_sweep_no_duplicate(self):
        ctx = _referred_setup()
        cutoff = timezone.now() - timezone.timedelta(hours=1)
        r1 = sweep_spread_revenue_share(cutoff)
        r2 = sweep_spread_revenue_share(cutoff)
        r3 = sweep_spread_revenue_share(cutoff)
        self.assertEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0)
        self.assertEqual(r3["generated"], 0)
        self.assertEqual(
            IBCommissionObligation.objects.filter(rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE).count(), 1,
        )

    def test_overlapping_sweep_windows_no_duplicate(self):
        ctx = _referred_setup()
        wide_cutoff = timezone.now() - timezone.timedelta(hours=2)
        narrow_cutoff = timezone.now() - timezone.timedelta(minutes=1)
        r1 = sweep_spread_revenue_share(wide_cutoff)
        r2 = sweep_spread_revenue_share(narrow_cutoff)  # same row, overlapping window
        self.assertEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0)
        self.assertEqual(
            IBCommissionObligation.objects.filter(rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────
# F — wrong revenue type
# ─────────────────────────────────────────────────────────────────────────

class RejectionTests(TestCase):
    def test_wrong_revenue_type_rev_commission_no_obligation(self):
        ctx = _referred_setup()
        row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("5.00"),
            source_account=ctx["account"], symbol="EUR/USD",
        )
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_wrong_revenue_type_other_types_no_obligation(self):
        ctx = _referred_setup()
        for rt in (BrokerLedger.REV_CHALLENGE_FEE, BrokerLedger.REV_WITHDRAW_FEE):
            row = make_broker_ledger(
                revenue_type=rt, amount=Decimal("5.00"),
                source_account=ctx["account"], symbol="EUR/USD",
            )
            with self.subTest(revenue_type=rt):
                self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_zero_amount_no_obligation(self):
        ctx = _referred_setup()
        row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("0"),
            source_account=ctx["account"], symbol="EUR/USD",
        )
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_negative_amount_no_obligation(self):
        ctx = _referred_setup()
        row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("-5.00"),
            source_account=ctx["account"], symbol="EUR/USD",
        )
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_missing_source_account_no_obligation(self):
        row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("5.00"),
            source_account=None, symbol="EUR/USD",
        )
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_no_attribution_no_obligation(self):
        trader = make_user()
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(percentage=Decimal("20.00"))
        row = _make_spread_ledger(account, amount="5.00")
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_no_applicable_rule_no_obligation(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        row = _make_spread_ledger(account, amount="5.00")
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_disabled_rule_no_obligation(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(referral=referral, enabled=False, percentage=Decimal("20.00"))
        row = _make_spread_ledger(account, amount="5.00")
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_future_effective_from_no_obligation(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(
            referral=referral, percentage=Decimal("20.00"),
            effective_from=timezone.now() + timezone.timedelta(days=1),
        )
        row = _make_spread_ledger(account, amount="5.00")
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    def test_expired_effective_until_no_obligation(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_rule(
            referral=referral, percentage=Decimal("20.00"),
            effective_from=timezone.now() - timezone.timedelta(days=2),
            effective_until=timezone.now() - timezone.timedelta(days=1),
        )
        row = _make_spread_ledger(account, amount="5.00")
        self.assertIsNone(generate_spread_revenue_share_obligation(row))

    # NOTE: an "ambiguous rule" scenario (two simultaneously-open per-IB
    # rules of the same rule_type) cannot be constructed through the ORM
    # at all — IBCommissionRule's own UniqueConstraint ("at most one
    # open-ended rule per (rule_type, referral)") rejects the second
    # .create() with a DB IntegrityError before resolve_applicable_
    # rule()'s own AmbiguousCommissionRuleError branch could ever be
    # reached. This mirrors test_ib_commission_triggers_02c.py's own
    # RuleResolutionTests, which likewise contains no such test for the
    # identical reason — not a gap, a structural impossibility.


# ─────────────────────────────────────────────────────────────────────────
# K — frozen referral
# ─────────────────────────────────────────────────────────────────────────

class FrozenReferralTests(TestCase):
    def test_frozen_referral_suppresses_generation(self):
        ctx = _referred_setup()
        freeze_referral(ctx["referral"], "test freeze", request=_fake_request(_make_reviewer()))
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertIsNone(obligation)

    def test_unfrozen_referral_resumes_generation(self):
        from simulator.ib_risk_holds import unfreeze_referral
        ctx = _referred_setup()
        reviewer = _make_reviewer()
        freeze_referral(ctx["referral"], "x", request=_fake_request(reviewer))
        unfreeze_referral(ctx["referral"], "y", request=_fake_request(reviewer))
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)


# ─────────────────────────────────────────────────────────────────────────
# L/M/N — multiple IBs, multiple executions, same symbol+amount isolation
# ─────────────────────────────────────────────────────────────────────────

class MultiplicityIsolationTests(TestCase):
    def test_two_different_ibs_no_cross_contamination(self):
        ctx_a = _referred_setup(percentage="20.00", amount="5.00")
        trader_b = make_user()
        referral_b = _make_referral()
        _make_attribution(trader_b, referral_b)
        account_b = make_account(user=trader_b, balance=Decimal("10000"))
        _make_rule(referral=referral_b, percentage=Decimal("40.00"))
        row_b = _make_spread_ledger(account_b, amount="5.00")

        ob_a = generate_spread_revenue_share_obligation(ctx_a["row"])
        ob_b = generate_spread_revenue_share_obligation(row_b)

        self.assertEqual(ob_a.referral_id, ctx_a["referral"].pk)
        self.assertEqual(ob_b.referral_id, referral_b.pk)
        self.assertEqual(ob_a.calculated_amount, Decimal("1.00"))
        self.assertEqual(ob_b.calculated_amount, Decimal("2.00"))

    def test_two_executions_same_account_two_independent_obligations(self):
        ctx = _referred_setup(percentage="20.00", amount="5.00")
        row2 = _make_spread_ledger(ctx["account"], amount="9.00")

        ob1 = generate_spread_revenue_share_obligation(ctx["row"])
        ob2 = generate_spread_revenue_share_obligation(row2)

        self.assertNotEqual(ob1.pk, ob2.pk)
        self.assertEqual(ob1.calculated_amount, Decimal("1.00"))
        self.assertEqual(ob2.calculated_amount, Decimal("1.80"))
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                referral=ctx["referral"], rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            ).count(), 2,
        )

    def test_same_symbol_same_amount_different_pk_still_independent(self):
        """Two DIFFERENT BrokerLedger rows sharing symbol AND amount must
        still produce two independent obligations — identity is the PK,
        never (symbol, amount)."""
        ctx = _referred_setup(percentage="20.00", amount="5.00")
        row2 = _make_spread_ledger(ctx["account"], amount="5.00")  # same symbol+amount
        self.assertNotEqual(ctx["row"].pk, row2.pk)

        ob1 = generate_spread_revenue_share_obligation(ctx["row"])
        ob2 = generate_spread_revenue_share_obligation(row2)

        self.assertNotEqual(ob1.pk, ob2.pk)
        self.assertEqual(ob1.source_event_id, ctx["row"].pk)
        self.assertEqual(ob2.source_event_id, row2.pk)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                referral=ctx["referral"], rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            ).count(), 2,
        )


# ─────────────────────────────────────────────────────────────────────────
# O — Decimal correctness
# ─────────────────────────────────────────────────────────────────────────

class DecimalCorrectnessTests(TestCase):
    def test_no_float_artifacts(self):
        ctx = _referred_setup(percentage="33.333", amount="10.01")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertIsInstance(obligation.calculated_amount, Decimal)
        self.assertIsInstance(obligation.basis_amount, Decimal)
        self.assertIsInstance(obligation.applied_percentage_rate, Decimal)
        # 10.01 * 33.333 / 100 = 3.3366333 -> ROUND_HALF_EVEN to 3.34
        expected = (Decimal("10.01") * Decimal("33.333") / Decimal("100")).quantize(
            Decimal("0.01"), rounding="ROUND_HALF_EVEN",
        )
        self.assertEqual(obligation.calculated_amount, expected)

    def test_zero_percent_yields_zero_amount(self):
        ctx = _referred_setup(percentage="0.00", amount="5.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.calculated_amount, Decimal("0.00"))

    def test_100_percent_yields_full_amount(self):
        ctx = _referred_setup(percentage="100.00", amount="5.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.calculated_amount, Decimal("5.00"))


# ─────────────────────────────────────────────────────────────────────────
# P — Treasury compatibility (real, unmocked pipeline)
# ─────────────────────────────────────────────────────────────────────────

class TreasuryCompatibilityTests(TestCase):
    def test_full_settlement_pipeline_unmodified(self):
        ctx = _referred_setup(percentage="20.00", amount="5.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(obligation.status, IBCommissionObligation.ST_PENDING)

        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()

        obligation = approve_obligation(obligation, request=_fake_request(reviewer))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_APPROVED)

        treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
        self.assertEqual(treasury_request.operation_type, TreasuryOperationRequest.OP_IB_COMMISSION)
        self.assertEqual(treasury_request.amount, Decimal("1.00"))

        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))

        result = sync_obligation_from_treasury(obligation)
        obligation = result["obligation"]
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)


# ─────────────────────────────────────────────────────────────────────────
# Q — Reversal compatibility (real, unmocked pipeline)
# ─────────────────────────────────────────────────────────────────────────

class ReversalCompatibilityTests(TestCase):
    def test_credited_spread_obligation_can_be_reversed(self):
        ctx = _referred_setup(percentage="20.00", amount="8.00")
        obligation = generate_spread_revenue_share_obligation(ctx["row"])
        reviewer = _make_reviewer()
        submitter = _make_submitter()
        executor = _make_executor()

        obligation = approve_obligation(obligation, request=_fake_request(reviewer))
        treasury_request = link_treasury_request(obligation, request=_fake_request(submitter))
        approve_treasury_request(treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(treasury_request, request=_fake_request(executor))
        result = sync_obligation_from_treasury(obligation)
        obligation = result["obligation"]
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)
        self.assertEqual(obligation.calculated_amount, Decimal("1.60"))  # 8.00 * 20%

        adj = submit_adjustment(
            obligation, amount=Decimal("0.60"), reason="chargeback",
            adjustment_type=IBCommissionAdjustment.TYPE_REVERSAL, request=_fake_request(submitter),
        )
        adj = approve_adjustment(adj, request=_fake_request(reviewer))
        adj_treasury_request = link_adjustment_to_treasury(adj, request=_fake_request(submitter))
        approve_treasury_request(adj_treasury_request, request=_fake_request(reviewer))
        execute_treasury_request(adj_treasury_request, request=_fake_request(executor))
        adj_result = sync_adjustment_from_treasury(adj)
        self.assertEqual(adj_result["adjustment"].status, IBCommissionAdjustment.ST_EXECUTED)

        remaining = remaining_reversible(obligation)
        self.assertEqual(remaining, Decimal("1.00"))

        # Original snapshot untouched by the reversal.
        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, Decimal("1.60"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)


# ─────────────────────────────────────────────────────────────────────────
# Section 15 — REAL execution integration (manual/WS, real REV_SPREAD)
# ─────────────────────────────────────────────────────────────────────────

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _seed_raw(symbol, bid, ask):
    import time
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = round((bid + ask) / 2, 6)
        feed._price_ts[symbol] = time.time()


def _clear_symbol(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


def _spread_consumer(account_id):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": False, "spread_pips": 0.0, "leverage": 50,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


class RealExecutionIntegrationTests(TransactionTestCase):
    """No modification to consumers.py — uses the exact, already-proven
    infrastructure (make_spread_config() + refresh_cache_sync() +
    _db_open_position_atomic.__wrapped__) already established in
    test_o6c1aa_unified_raw_execution_spread_fee.py for producing a
    REAL REV_SPREAD row from a real execution, not a synthetic one."""

    def setUp(self):
        reset_for_tests()
        _clear_symbol("EUR/USD")

    def tearDown(self):
        reset_for_tests()
        _clear_symbol("EUR/USD")

    def test_real_execution_produces_matching_basis_amount(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        _make_rule(referral=referral, percentage=Decimal("25.00"))

        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(user=trader, balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        consumer = _spread_consumer(account.pk)

        result = _db_open_sync(
            consumer, symbol="EUR/USD", side="buy", qty=0.02, price=1.17002,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])

        rev_row = BrokerLedger.objects.get(
            source_account=account, revenue_type=BrokerLedger.REV_SPREAD,
        )
        self.assertGreater(rev_row.amount, Decimal("0"))

        # The generator must consume the REAL persisted amount verbatim
        # — never recompute it from qty/price/pips/contract_size (none
        # of those are passed to the generator at all).
        obligation = generate_spread_revenue_share_obligation(rev_row)
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.basis_amount, rev_row.amount)
        self.assertEqual(obligation.applied_percentage_rate, Decimal("25.00"))

        # Cross-check against the trader's own real fee charge — same
        # value by construction, per IB-COMMISSION-PARITY-09C's own
        # confirmed economic-parity chain.
        fee_entry = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertEqual(rev_row.amount, -fee_entry.amount)

    def test_sweep_processes_the_real_row(self):
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        _make_rule(referral=referral, percentage=Decimal("10.00"))

        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(user=trader, balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        consumer = _spread_consumer(account.pk)

        _db_open_sync(
            consumer, symbol="EUR/USD", side="buy", qty=0.02, price=1.17002,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )

        cutoff = timezone.now() - timezone.timedelta(hours=1)
        result = sweep_spread_revenue_share(cutoff)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                referral=referral, rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            ).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────
# Section 16 — pending order: explicit zero-spread contract (not a failure)
# ─────────────────────────────────────────────────────────────────────────

class PendingOrderZeroSpreadTests(TransactionTestCase):
    """IB-COMMISSION-PARITY-09C's certified, deliberate finding: pending/
    stop/limit-trigger executions never write REV_SPREAD at all (no live
    pricing data to compute a markup from at trigger time). This is the
    current, correct, UNMODIFIED engine contract — not a defect this
    block fixes or works around. No change to consumers.py is made or
    needed to make this test pass; it documents existing behavior."""

    def setUp(self):
        reset_for_tests()
        _clear_symbol("EUR/USD")

    def tearDown(self):
        reset_for_tests()
        _clear_symbol("EUR/USD")

    def test_pending_trigger_produces_zero_rev_spread_and_zero_obligations(self):
        from simulator.consumers import _trigger_pending_order_core
        from simulator.models import PendingOrder

        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        _make_rule(referral=referral, percentage=Decimal("20.00"))

        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(user=trader, balance=Decimal("10000"))

        po = PendingOrder.objects.create(
            account=account, symbol="EUR/USD", side="BUY", order_type="LIMIT",
            qty=Decimal("0.01"), trigger_price=Decimal("1.10000"),
        )
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])

        self.assertEqual(
            BrokerLedger.objects.filter(
                source_account=account, revenue_type=BrokerLedger.REV_SPREAD,
            ).count(), 0,
            "pending-trigger path must never produce REV_SPREAD — existing, unmodified contract",
        )

        cutoff = timezone.now() - timezone.timedelta(hours=1)
        sweep_result = sweep_spread_revenue_share(cutoff)
        self.assertEqual(
            IBCommissionObligation.objects.filter(
                referral=referral, rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
            ).count(), 0,
            "no REV_SPREAD row exists -> zero SPREAD_REVENUE_SHARE obligations, by design",
        )


# ─────────────────────────────────────────────────────────────────────────
# Task integration — extended, existing keys untouched
# ─────────────────────────────────────────────────────────────────────────

class TaskExtensionNonRegressionTests(TestCase):
    def test_task_result_includes_spread_key_without_changing_others(self):
        result = sweep_ib_commission_triggers_task.apply(args=(30,)).get()
        self.assertIn("spread_revenue_share", result)
        self.assertIn("scanned", result["spread_revenue_share"])
        self.assertIn("generated", result["spread_revenue_share"])
        self.assertIn("skipped", result["spread_revenue_share"])
        # Pre-existing keys untouched.
        for key in ("per_lot", "challenge_percent", "deposit_percent", "trading_commission_revenue_share", "elapsed_ms"):
            self.assertIn(key, result)


# ─────────────────────────────────────────────────────────────────────────
# Structural — no money movement, no CPA_BONUS activation, no engine touch
# ─────────────────────────────────────────────────────────────────────────

class NoMoneyMovementTests(TestCase):
    def test_generator_moves_no_money(self):
        ctx = _referred_setup()
        wallet_before = WalletTransaction.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()
        generate_spread_revenue_share_obligation(ctx["row"])
        self.assertEqual(WalletTransaction.objects.count(), wallet_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)

    def test_sweep_moves_no_money(self):
        _referred_setup()
        wallet_before = WalletTransaction.objects.count()
        cutoff = timezone.now() - timezone.timedelta(hours=1)
        sweep_spread_revenue_share(cutoff)
        self.assertEqual(WalletTransaction.objects.count(), wallet_before)


class StructuralTests(TestCase):
    def test_no_cpa_bonus_activation(self):
        self.assertEqual(IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_CPA_BONUS).count(), 0)

    def test_generator_never_touches_trading_engine_or_treasury(self):
        import inspect

        from simulator import ib_commission
        source = inspect.getsource(ib_commission.generate_spread_revenue_share_obligation)
        self.assertNotIn("Position.objects", source)
        self.assertNotIn("credit_wallet(", source)
        self.assertNotIn("debit_wallet(", source)
        self.assertNotIn("TreasuryOperationRequest.objects.create", source)

    def test_sweep_never_touches_rev_commission(self):
        ctx = _referred_setup()
        commission_row = make_broker_ledger(
            revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("9.00"),
            source_account=ctx["account"], symbol="EUR/USD",
        )
        cutoff = timezone.now() - timezone.timedelta(hours=1)
        sweep_spread_revenue_share(cutoff)
        self.assertFalse(
            IBCommissionObligation.objects.filter(
                source_event_type="broker_ledger_spread", source_event_id=commission_row.pk,
            ).exists(),
        )
