"""
simulator/tests/test_risk_scope_foundation.py — O.6c-1.

Risk Scope Foundation (simulator/broker_exposure.py) — an explicit,
opt-in `risk_scope="real"` parameter on calculate_broker_exposure() and
its wrapper functions, restricting the aggregate to REAL_MONEY_ACCOUNT_TYPES
(RETAIL/ECN/STANDARD/CRYPTO). DEMO, CHALLENGE, and FUNDED accounts are
excluded from that scope — DEMO and CHALLENGE per the approved O.6c-1
business decision (neither represents real financial risk for Money
Broker); FUNDED because whether funded payouts are financed with real
broker capital is an explicit, unmade business decision (never assumed
here — see the O.6c report's "decisiones de negocio pendientes").

This microblock touches ONLY simulator/broker_exposure.py. broker_risk.py
and broker_pnl.py are deliberately untouched — see the O.6c-1 pre-commit
report for why (broker_pnl.py's CHALLENGE_FEE/WITHDRAW_FEE ambiguity; the
live order-rejection caller in broker_risk.py is explicitly not migrated
in this block).

Coverage layers:
  - LegacyCompatibilityTests: no risk_scope argument -> byte-identical
    aggregate to pre-O.6c-1 behavior (DEMO/CHALLENGE included, exactly as
    today).
  - RealScopeExclusionTests: risk_scope="real" excludes DEMO, CHALLENGE,
    and FUNDED positions from every aggregate field.
  - PricingCoverageIsolationTests: a stale DEMO/CHALLENGE position never
    lowers pricing_coverage_pct for risk_scope="real" (cases 4/5).
  - RealAccountAggregationTests: REAL accounts are included and DO
    aggregate together across different accounts/users (cases 6/7).
  - AccountIndependenceTests: per-account fields (margin_used, balance/
    equity) never leak between accounts, including two accounts owned by
    the same user (cases 8/9).
  - NoFormulaChangeTests: risk_scope changes WHICH positions are counted,
    never HOW a counted position's own PnL/notional is computed (case 10).
  - InvalidScopeTests: an unrecognized risk_scope raises, never silently
    no-ops.
  - O6aIncidentReproductionTests: the O.6a manual-rejection scenario
    (healthy REAL BTCUSD order, stale DEMO/CHALLENGE EURUSD positions
    elsewhere in the book) — legacy behavior (no scope, and the live
    validate_new_order() caller, untouched) still reproduces today's
    coverage<100% rejection; risk_scope="real" excludes the simulated
    stale positions entirely from that same calculation.
"""
import time
from decimal import Decimal

from django.test import TestCase

from market_data.feeds import get_feed_manager

from simulator.broker_exposure import (
    REAL_MONEY_ACCOUNT_TYPES,
    RISK_SCOPE_REAL,
    calculate_broker_exposure,
    broker_exposure_for_account,
    broker_exposure_for_accounts,
    broker_exposure_for_symbol,
    broker_exposure_snapshot,
)
from simulator.broker_risk import validate_new_order, REASON_PRICING_INCOMPLETE

from .factories import make_account, make_position, make_user


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


def _seed_fresh_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


class _CleanFeedMixin:
    """Every test clears the symbols it touches before AND after, so the
    process-wide FeedManager singleton never leaks state across tests."""
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


# ─────────────────────────────────────────────────────────────────────────
# 1. Legacy compatibility — no risk_scope argument at all
# ─────────────────────────────────────────────────────────────────────────
class LegacyCompatibilityTests(_CleanFeedMixin, TestCase):
    def test_no_scope_reproduces_historical_aggregate(self):
        real = make_account(account_type="STANDARD", balance=Decimal("100000"))
        demo = make_account(account_type="DEMO", balance=Decimal("100000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(demo, symbol="BTCUSD", side="BUY", qty=Decimal("2"), avg_price=Decimal("100"))
        make_position(challenge, symbol="BTCUSD", side="BUY", qty=Decimal("3"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_snapshot()  # no risk_scope kwarg at all
        # Historical behavior: every account_type is included, unfiltered.
        self.assertEqual(b.open_position_count, 3)
        self.assertEqual(b.gross_quantity, Decimal("6"))
        self.assertEqual(b.long_quantity, Decimal("6"))
        self.assertIsNone(b.risk_scope)

    def test_explicit_none_is_identical_to_omitted(self):
        acc = make_account(account_type="DEMO", balance=Decimal("100000"))
        make_position(acc, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        omitted = calculate_broker_exposure()
        explicit_none = calculate_broker_exposure(risk_scope=None)
        self.assertEqual(omitted.gross_quantity, explicit_none.gross_quantity)
        self.assertEqual(omitted.open_position_count, explicit_none.open_position_count)

    def test_all_wrappers_default_to_no_scope(self):
        acc = make_account(account_type="DEMO", balance=Decimal("100000"))
        pos = make_position(acc, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        self.assertEqual(broker_exposure_for_symbol("BTCUSD").open_position_count, 1)
        self.assertEqual(broker_exposure_for_account(acc.id).open_position_count, 1)
        self.assertEqual(broker_exposure_for_accounts([acc.id]).open_position_count, 1)
        self.assertEqual(broker_exposure_snapshot().open_position_count, 1)


# ─────────────────────────────────────────────────────────────────────────
# 2/3. risk_scope="real" excludes DEMO / CHALLENGE / FUNDED
# ─────────────────────────────────────────────────────────────────────────
class RealScopeExclusionTests(_CleanFeedMixin, TestCase):
    def test_demo_excluded_from_real_scope(self):
        real = make_account(account_type="STANDARD", balance=Decimal("100000"))
        demo = make_account(account_type="DEMO", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(demo, symbol="BTCUSD", side="BUY", qty=Decimal("99"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.gross_quantity, Decimal("1"))
        self.assertEqual(b.risk_scope, "real")

    def test_challenge_excluded_from_real_scope(self):
        real = make_account(account_type="ECN", balance=Decimal("100000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(challenge, symbol="BTCUSD", side="BUY", qty=Decimal("99"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.gross_quantity, Decimal("1"))

    def test_funded_excluded_from_real_scope_pending_business_decision(self):
        real = make_account(account_type="RETAIL", balance=Decimal("100000"))
        funded = make_account(account_type="FUNDED", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(funded, symbol="BTCUSD", side="BUY", qty=Decimal("99"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.gross_quantity, Decimal("1"))

    def test_all_four_real_types_included(self):
        self.assertEqual(REAL_MONEY_ACCOUNT_TYPES, frozenset({"RETAIL", "ECN", "STANDARD", "CRYPTO"}))
        accounts = [make_account(account_type=t, balance=Decimal("100000")) for t in REAL_MONEY_ACCOUNT_TYPES]
        for acc in accounts:
            make_position(acc, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 4)
        self.assertEqual(b.gross_quantity, Decimal("4"))


# ─────────────────────────────────────────────────────────────────────────
# 4/5. A stale DEMO/CHALLENGE position must never contaminate REAL coverage
# ─────────────────────────────────────────────────────────────────────────
class PricingCoverageIsolationTests(_CleanFeedMixin, TestCase):
    def test_demo_stale_position_does_not_lower_real_coverage(self):
        real = make_account(account_type="STANDARD", balance=Decimal("100000"))
        demo = make_account(account_type="DEMO", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(demo, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 100)
        _clear_price("EUR/USD")  # DEMO's own position is stale/unpriced

        legacy = broker_exposure_snapshot()
        self.assertLess(legacy.pricing_coverage_pct, Decimal("100"))  # contaminated, as today

        real_scope = broker_exposure_snapshot(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(real_scope.pricing_coverage_pct, Decimal("100.00"))
        self.assertEqual(real_scope.unpriced_position_count, 0)
        self.assertEqual(real_scope.stale_or_missing_symbols, [])

    def test_challenge_stale_position_does_not_lower_real_coverage(self):
        real = make_account(account_type="CRYPTO", balance=Decimal("100000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(challenge, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 100)
        _clear_price("EUR/USD")

        legacy = broker_exposure_snapshot()
        self.assertLess(legacy.pricing_coverage_pct, Decimal("100"))

        real_scope = broker_exposure_snapshot(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(real_scope.pricing_coverage_pct, Decimal("100.00"))
        self.assertEqual(real_scope.unpriced_position_count, 0)


# ─────────────────────────────────────────────────────────────────────────
# 6/7. REAL accounts are included, and DO aggregate together
# ─────────────────────────────────────────────────────────────────────────
class RealAccountAggregationTests(_CleanFeedMixin, TestCase):
    def test_single_real_account_included(self):
        real = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 1)

    def test_two_different_real_accounts_aggregate_together(self):
        # Two DIFFERENT real accounts, different account_types, different
        # users — broker-wide exposure is the sum of both, exactly as the
        # legacy (unscoped) aggregate already does for any two accounts.
        retail = make_account(account_type="RETAIL", balance=Decimal("100000"))
        ecn = make_account(account_type="ECN", balance=Decimal("100000"))
        make_position(retail, symbol="BTCUSD", side="BUY", qty=Decimal("2"), avg_price=Decimal("100"))
        make_position(ecn, symbol="BTCUSD", side="BUY", qty=Decimal("3"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 2)
        self.assertEqual(b.account_count, 2)
        self.assertEqual(b.gross_quantity, Decimal("5"))
        self.assertEqual(b.long_quantity, Decimal("5"))


# ─────────────────────────────────────────────────────────────────────────
# 8/9. Per-account independence — never leaks between accounts, including
# two accounts owned by the SAME user.
# ─────────────────────────────────────────────────────────────────────────
class AccountIndependenceTests(_CleanFeedMixin, TestCase):
    def test_margin_used_is_independent_per_account(self):
        acc_a = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc_b = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(acc_a, symbol="BTCUSD", side="BUY", qty=Decimal("10"), avg_price=Decimal("100"))
        make_position(acc_b, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)
        margin_a = b.by_account[acc_a.id].margin_used
        margin_b = b.by_account[acc_b.id].margin_used
        self.assertGreater(margin_a, margin_b)
        # acc_a's position is 10x acc_b's -> its margin must be exactly 10x,
        # never blended/averaged with acc_b's.
        self.assertEqual(margin_a, margin_b * 10)

    def test_same_user_two_accounts_do_not_mix_balances(self):
        user = make_user()
        acc_demo = make_account(user=user, account_type="DEMO", balance=Decimal("50000"))
        acc_real = make_account(user=user, account_type="STANDARD", balance=Decimal("7000"))
        make_position(acc_demo, symbol="BTCUSD", side="BUY", qty=Decimal("5"), avg_price=Decimal("100"))
        make_position(acc_real, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        # Model-level: balances are separate DB rows, never combined.
        acc_demo.refresh_from_db()
        acc_real.refresh_from_db()
        self.assertEqual(acc_demo.balance, Decimal("50000.00"))
        self.assertEqual(acc_real.balance, Decimal("7000.00"))

        # risk_scope="real" for this user's two accounts must see ONLY the
        # real one, never the demo one, even though both belong to the
        # same user.
        b = broker_exposure_for_accounts([acc_demo.id, acc_real.id], risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(b.open_position_count, 1)
        self.assertIn(acc_real.id, b.by_account)
        self.assertNotIn(acc_demo.id, b.by_account)


# ─────────────────────────────────────────────────────────────────────────
# 10. risk_scope changes WHICH positions are counted, never HOW a counted
# position's own PnL/notional formula is computed.
# ─────────────────────────────────────────────────────────────────────────
class NoFormulaChangeTests(_CleanFeedMixin, TestCase):
    def test_per_position_pnl_and_notional_unaffected_by_scope(self):
        real = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(real, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.00000"))
        _seed_fresh_price("EUR/USD", 1.00010)

        unscoped = calculate_broker_exposure()
        scoped = calculate_broker_exposure(risk_scope=RISK_SCOPE_REAL)

        # Same single REAL position in both -> identical PnL/notional math,
        # not just identical counts. The formula itself is untouched.
        self.assertEqual(unscoped.trader_unrealized_pnl, scoped.trader_unrealized_pnl)
        self.assertEqual(unscoped.broker_unrealized_counterparty_pnl, scoped.broker_unrealized_counterparty_pnl)
        self.assertEqual(unscoped.gross_notional, scoped.gross_notional)
        self.assertEqual(unscoped.trader_unrealized_pnl, Decimal("10.00"))


# ─────────────────────────────────────────────────────────────────────────
# Invalid scope must raise, never silently no-op
# ─────────────────────────────────────────────────────────────────────────
class InvalidScopeTests(_CleanFeedMixin, TestCase):
    def test_unknown_scope_raises(self):
        with self.assertRaises(ValueError):
            calculate_broker_exposure(risk_scope="REAL")  # wrong case, not the real value

        with self.assertRaises(ValueError):
            calculate_broker_exposure(risk_scope="demo")  # not a valid scope name


# ─────────────────────────────────────────────────────────────────────────
# O.6a incident reproduction — healthy REAL BTCUSD order, stale DEMO/
# CHALLENGE EURUSD positions elsewhere in the book.
# ─────────────────────────────────────────────────────────────────────────
class O6aIncidentReproductionTests(_CleanFeedMixin, TestCase):
    def test_a_legacy_behavior_still_reproduces_current_rejection(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        make_position(demo, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        make_position(challenge, symbol="EUR/USD", side="SELL", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 63353)
        _clear_price("EUR/USD")  # DEMO/CHALLENGE positions stale, as in the manual incident

        # validate_new_order() is NOT touched in this microblock — it still
        # calls broker_exposure_snapshot() with no risk_scope at all. This
        # test proves that caller's behavior is unchanged, bit for bit.
        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("63353"), contract_size=Decimal("1"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_b_real_scope_excludes_the_simulated_stale_positions(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        make_position(real, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"), avg_price=Decimal("63353"))
        make_position(demo, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        make_position(challenge, symbol="EUR/USD", side="SELL", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 63353)
        _clear_price("EUR/USD")

        legacy = broker_exposure_snapshot()
        self.assertLess(legacy.pricing_coverage_pct, Decimal("100"))
        self.assertIn("EUR/USD", legacy.stale_or_missing_symbols)

        real_scope = broker_exposure_snapshot(risk_scope=RISK_SCOPE_REAL)
        self.assertEqual(real_scope.pricing_coverage_pct, Decimal("100.00"))
        self.assertEqual(real_scope.stale_or_missing_symbols, [])
        self.assertEqual(real_scope.open_position_count, 1)
