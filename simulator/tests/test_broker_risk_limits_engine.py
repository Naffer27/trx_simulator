"""
simulator/tests/test_broker_risk_limits_engine.py — RISK-02 (+ post-review
corrections: atomicity, fail-closed pricing, MAX_OPEN_POSITIONS_BROKER_WIDE
naming).

Broker Risk Limits Engine (simulator/broker_risk.py) — answers exactly
one question, independent of routing: can the broker accept this NEW
exposure? Built on RISK-01's broker_exposure.py for current-state reads;
never re-derives exposure with a new formula. Evaluated INSIDE
TradingConsumer._db_open_position_atomic's single transaction, behind
BrokerRiskLock (models.py) — see that model's docstring and the
module-level LOCK ORDER note in consumers.py for the full design:
BrokerRiskLock -> TradingAccount -> Position, BrokerRiskLock acquired
FIRST, always, only by the open path (never by any close path).

Coverage layers:
  - RuleUnitTests: each of the 9 rules individually, PASS and FAIL.
  - ReproductionCaseTests: FASE 6 cases A-E, verbatim.
  - PricingFailClosedTests: Correction 2 — missing price, stale/missing
    existing-position price, coverage<100%, RISK_PRICING_INCOMPLETE
    reason_code, full coverage evaluates normally.
  - DecisionAggregationTests: risk_checks always lists every rule.
  - ConcurrencyReproductionTests: Correction 1 — genuine OS threads, two
    DIFFERENT accounts, racing MAX_SYMBOL_EXPOSURE / MAX_TOTAL_BROKER_
    EXPOSURE / MAX_GROSS_NOTIONAL. Same established pattern as
    test_atomic_guard_lock_order.py (PANEL-02): TransactionTestCase,
    threading.Barrier for maximum overlap, retry-on-SQLITE_LOCKED (a real,
    documented SQLite shared-cache limitation, not a way of avoiding real
    contention — see that file's module docstring for the full SQLite-vs-
    PostgreSQL caveat, which applies identically here).
  - LockAndRollbackTests: rejected orders release the lock (a subsequent
    call is never blocked), an exception mid-open leaves no partial state
    and still releases the lock.
  - LockOrderStructuralTests: BrokerRiskLock query precedes TradingAccount
    query, source-order verified.
  - IntegrationTests: the real _order_new() WS handler, end-to-end.
  - NoRegressionTests: BOOK-02/BOOK-03/RISK-01/PANEL-02/03/04 unaffected.
"""
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager

import simulator.broker_risk as br
from simulator.broker_risk import (
    RiskLimitDecision, RiskCheckResult,
    validate_new_order, validate_symbol_limit, validate_account_limit,
    validate_total_limit, validate_position_limit,
    STATUS_PASS, STATUS_FAIL, STATUS_WARNING,
    REASON_PRICING_INCOMPLETE, MAX_OPEN_POSITIONS_RULE,
)
from simulator.broker_exposure import broker_exposure_snapshot
from simulator.consumers import TradingConsumer
from simulator.models import BrokerRiskLock, Position, Trade, TradingAccount

from .factories import make_account, make_position
from .test_order_ticket_sl_tp_validation import _consumer as _ws_consumer

_run = lambda coro: __import__("asyncio").run(coro)
_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


class _CleanFeedMixin:
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD", "GBP/USD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0, "leverage": 50,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


# ─────────────────────────────────────────────────────────────────────────
# 1. Individual rules
# ─────────────────────────────────────────────────────────────────────────
class RuleUnitTests(_CleanFeedMixin, TestCase):
    def test_max_symbol_exposure_pass(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("15"), avg_price=Decimal("1.1"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            checks = validate_symbol_limit("EUR/USD", Decimal("3"))
        self.assertEqual(checks[0].status, STATUS_PASS)
        self.assertEqual(checks[0].current_value, Decimal("15"))

    def test_max_symbol_exposure_fail(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            checks = validate_symbol_limit("EUR/USD", Decimal("3"))
        self.assertEqual(checks[0].status, STATUS_FAIL)
        self.assertEqual(checks[0].current_value, Decimal("18"))
        self.assertEqual(checks[0].limit_value, Decimal("20"))

    def test_max_account_exposure_pass_and_fail(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("8"), avg_price=Decimal("100"))
        with patch.object(br, "MAX_ACCOUNT_EXPOSURE_LOTS", Decimal("10")):
            ok = validate_account_limit(account.id, Decimal("1"))
            fail = validate_account_limit(account.id, Decimal("5"))
        self.assertEqual(ok[0].status, STATUS_PASS)
        self.assertEqual(fail[0].status, STATUS_FAIL)

    def test_max_total_broker_exposure(self):
        acc1 = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        acc2 = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(acc1, symbol="BTCUSD", side="BUY", qty=Decimal("6"), avg_price=Decimal("100"))
        make_position(acc2, symbol="ETHUSD", side="BUY", qty=Decimal("6"), avg_price=Decimal("100"))
        book = broker_exposure_snapshot()
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("15")):
            checks = validate_total_limit("BUY", Decimal("1"), book)
        total_check = next(c for c in checks if c.rule == "MAX_TOTAL_BROKER_EXPOSURE")
        self.assertEqual(total_check.status, STATUS_PASS)
        self.assertEqual(total_check.current_value, Decimal("12"))

    def test_max_long_and_short_exposure(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("9"), avg_price=Decimal("100"))
        book = broker_exposure_snapshot()
        with patch.object(br, "MAX_LONG_EXPOSURE_LOTS", Decimal("10")):
            checks = validate_total_limit("BUY", Decimal("2"), book)
        long_check = next(c for c in checks if c.rule == "MAX_LONG_EXPOSURE")
        self.assertEqual(long_check.status, STATUS_FAIL)  # 9+2=11 > 10

        with patch.object(br, "MAX_SHORT_EXPOSURE_LOTS", Decimal("100")):
            checks2 = validate_total_limit("SELL", Decimal("2"), book)
        short_check = next(c for c in checks2 if c.rule == "MAX_SHORT_EXPOSURE")
        self.assertEqual(short_check.status, STATUS_PASS)  # 0+2=2 <= 100

    def test_max_gross_and_net_notional_with_full_coverage(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("50"), avg_price=Decimal("100"))
        _seed_price("BTCUSD", 100)  # full coverage required or this fails closed instead
        book = broker_exposure_snapshot()
        with patch.object(br, "MAX_GROSS_NOTIONAL", Decimal("6000")), \
             patch.object(br, "MAX_NET_NOTIONAL", Decimal("6000")):
            checks = validate_total_limit("BUY", Decimal("20"), book, price=Decimal("100"), contract_size=Decimal("1"))
        gross = next(c for c in checks if c.rule == "MAX_GROSS_NOTIONAL")
        net = next(c for c in checks if c.rule == "MAX_NET_NOTIONAL")
        # current gross_notional=5000 (50*100), +20*100=2000 -> 7000 > 6000
        self.assertEqual(gross.status, STATUS_FAIL)
        self.assertEqual(net.status, STATUS_FAIL)

    def test_max_position_size(self):
        with patch.object(br, "MAX_POSITION_SIZE_LOTS", Decimal("10")):
            ok = validate_position_limit(Decimal("5"), broker_exposure_snapshot())
            fail = validate_position_limit(Decimal("15"), broker_exposure_snapshot())
        ok_check = next(c for c in ok if c.rule == "MAX_POSITION_SIZE")
        fail_check = next(c for c in fail if c.rule == "MAX_POSITION_SIZE")
        self.assertEqual(ok_check.status, STATUS_PASS)
        self.assertEqual(fail_check.status, STATUS_FAIL)

    def test_max_open_positions_broker_wide(self):
        # Correction 3 — this counts positions across ALL accounts, not
        # just the requesting one; rule identifier is MAX_OPEN_POSITIONS_
        # BROKER_WIDE (not "MAX_OPEN_POSITIONS", and not RiskRule's
        # per-account max_open_positions field, which is untouched).
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        for _ in range(5):
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        with patch.object(br, "MAX_OPEN_POSITIONS_BROKER_WIDE", 5):
            checks = validate_position_limit(Decimal("1"), broker_exposure_snapshot())
        pos_check = next(c for c in checks if c.rule == MAX_OPEN_POSITIONS_RULE)
        self.assertEqual(pos_check.status, STATUS_FAIL)  # 5+1=6 > 5
        self.assertEqual(MAX_OPEN_POSITIONS_RULE, "MAX_OPEN_POSITIONS_BROKER_WIDE")


# ─────────────────────────────────────────────────────────────────────────
# 2. FASE 6 — reproduction cases A-E (each PASS scenario needs full
#    pricing coverage now that notional checks fail-closed otherwise)
# ─────────────────────────────────────────────────────────────────────────
class ReproductionCaseTests(_CleanFeedMixin, TestCase):
    def test_caso_a_broker_limit_20_actual_18_new_3_fails(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"))
        self.assertFalse(decision.allowed)
        # No price supplied -> RISK_PRICING_INCOMPLETE would also fail it,
        # but the symbol-exposure breach must independently show up in
        # risk_checks regardless of which reason_code wins top billing.
        symbol_check = next(c for c in decision.risk_checks if c.rule == "MAX_SYMBOL_EXPOSURE")
        self.assertEqual(symbol_check.status, STATUS_FAIL)

    def test_caso_b_actual_15_new_3_passes(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("15"), avg_price=Decimal("1.1"))
        _seed_price("EUR/USD", 1.1)  # full coverage needed for an overall PASS
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(
                account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"),
                price=Decimal("1.1"), contract_size=Decimal("100000"),
            )
        self.assertTrue(decision.allowed)

    def test_caso_c_account_limit_5_positions_has_5_fails(self):
        # Broker-wide MAX_OPEN_POSITIONS_BROKER_WIDE — FASE 3 defines no
        # separate per-account position-count rule (MAX_ACCOUNT_EXPOSURE
        # is lot-based, not count-based), so "cuenta limite: 5 posiciones"
        # maps to the broker-wide open-position-count gate at a broker
        # total of 5. Correction 3 — this mapping is now unambiguous by
        # construction: the rule identifier itself says BROKER_WIDE.
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        for _ in range(5):
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_price("BTCUSD", 100)
        with patch.object(br, "MAX_OPEN_POSITIONS_BROKER_WIDE", 5):
            decision = validate_new_order(
                account_id=account.id, symbol="BTCUSD", side="BUY", qty=Decimal("1"),
                price=Decimal("100"), contract_size=Decimal("1"),
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.blocked_limit, MAX_OPEN_POSITIONS_RULE)

    def test_caso_d_margin_ok_exposure_fail_overall_fails(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        _seed_price("EUR/USD", 1.1)
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(
                account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"),
                price=Decimal("1.1"), contract_size=Decimal("100000"),
            )
        # margin_after is populated (margin itself is never a blocking
        # RISK-02 rule — it's informational here) while the symbol
        # exposure rule still fails the whole decision.
        self.assertIsNotNone(decision.margin_after)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.blocked_limit, "MAX_SYMBOL_EXPOSURE")

    def test_caso_e_everything_ok_passes(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(
            account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"),
            price=Decimal("1.1"), contract_size=Decimal("100000"),
        )
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason_code)
        self.assertTrue(all(c.status != STATUS_FAIL for c in decision.risk_checks))


# ─────────────────────────────────────────────────────────────────────────
# 3. Correction 2 — fail-closed pricing
# ─────────────────────────────────────────────────────────────────────────
class PricingFailClosedTests(_CleanFeedMixin, TestCase):
    def test_existing_position_without_fresh_price_blocks_new_order(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _clear_price("BTCUSD")  # explicit: no fresh price for the existing position
        decision = validate_new_order(
            account_id=account.id, symbol="ETHUSD", side="BUY", qty=Decimal("1"),
            price=Decimal("50"), contract_size=Decimal("1"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_missing_new_order_price_blocks(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_coverage_below_100_gives_pricing_incomplete_reason_code(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _seed_price("BTCUSD", 100)
        _clear_price("ETHUSD")  # 1 of 2 positions unpriced -> coverage 50%
        decision = validate_new_order(
            account_id=account.id, symbol="BTCUSD", side="BUY", qty=Decimal("1"),
            price=Decimal("100"), contract_size=Decimal("1"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)
        gross_check = next(c for c in decision.risk_checks if c.rule == "MAX_GROSS_NOTIONAL")
        self.assertEqual(gross_check.status, STATUS_FAIL)

    def test_full_coverage_evaluates_normally_and_can_pass(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_price("BTCUSD", 100)
        decision = validate_new_order(
            account_id=account.id, symbol="BTCUSD", side="BUY", qty=Decimal("1"),
            price=Decimal("100"), contract_size=Decimal("1"),
        )
        self.assertTrue(decision.allowed)

    def test_pure_quantity_rules_unaffected_by_incomplete_pricing(self):
        # Lot-based checks must still run and can still individually PASS
        # even though the overall decision is forced FAIL by pricing.
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"))
        self.assertFalse(decision.allowed)  # fail-closed on pricing
        symbol_check = next(c for c in decision.risk_checks if c.rule == "MAX_SYMBOL_EXPOSURE")
        self.assertEqual(symbol_check.status, STATUS_PASS)  # still correctly evaluated


# ─────────────────────────────────────────────────────────────────────────
# 4. Decision aggregation
# ─────────────────────────────────────────────────────────────────────────
class DecisionAggregationTests(_CleanFeedMixin, TestCase):
    def test_all_nine_rules_always_evaluated(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(
            account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"),
            price=Decimal("1.1"), contract_size=Decimal("100000"),
        )
        rule_names = {c.rule for c in decision.risk_checks}
        self.assertEqual(rule_names, set(br.RULE_NAMES))

    def test_never_short_circuits_multiple_failures_all_listed(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        _seed_price("EUR/USD", 1.1)
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")), \
             patch.object(br, "MAX_ACCOUNT_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(
                account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"),
                price=Decimal("1.1"), contract_size=Decimal("100000"),
            )
        failing = [c.rule for c in decision.risk_checks if c.status == STATUS_FAIL]
        self.assertIn("MAX_SYMBOL_EXPOSURE", failing)
        self.assertIn("MAX_ACCOUNT_EXPOSURE", failing)
        self.assertEqual(len(failing), 2)

    def test_exposure_after_populated_on_pass_and_fail(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(
            account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"),
            price=Decimal("1.1"), contract_size=Decimal("100000"),
        )
        self.assertEqual(decision.exposure_after, Decimal("1"))

    def test_margin_after_none_without_price(self):
        account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        decision = validate_new_order(account_id=account.id, symbol="EUR/USD", side="BUY", qty=Decimal("1"))
        self.assertIsNone(decision.margin_after)


# ─────────────────────────────────────────────────────────────────────────
# 5. Correction 1 — genuine multi-threaded concurrency reproduction
# ─────────────────────────────────────────────────────────────────────────
def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same technique as test_atomic_guard_lock_order.py's helper — real
    thread contention on every attempt, retried only on SQLite's
    shared-cache SQLITE_LOCKED (not a substitute for busy_timeout, which
    only covers SQLITE_BUSY)."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = fn()
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    raise
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


def _open_in_thread(account_id, symbol, qty, price, barrier, results, index):
    def _do():
        return _db_open_sync(
            _consumer(account_id), symbol, "buy", qty, price, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
    _run_locked_retry(_do, barrier, results, index)


class ConcurrencyReproductionTests(TransactionTestCase):
    """TESTS OBLIGATORIOS — dos cuentas distintas, ambas órdenes pasan
    individualmente, juntas exceden el límite. Uses TransactionTestCase
    (not TestCase): genuine cross-thread visibility requires it — same
    reasoning as test_atomic_guard_lock_order.py / test_account_balance_
    concurrency.py."""

    def test_two_accounts_race_max_symbol_exposure(self):
        _seed_price("EUR/USD", 1.1699)
        acc1 = make_account(balance=Decimal("1000000.00"))
        acc2 = make_account(balance=Decimal("1000000.00"))
        make_position(acc1, symbol="EUR/USD", side="BUY", qty=Decimal("15"), avg_price=Decimal("1.1699"))

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            threads = [
                threading.Thread(target=_open_in_thread,
                                  args=(acc1.pk, "EUR/USD", 3.0, 1.1699, barrier, results, 0)),
                threading.Thread(target=_open_in_thread,
                                  args=(acc2.pk, "EUR/USD", 3.0, 1.1699, barrier, results, 1)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        accepted = [r for r in results if r and r["ok"]]
        rejected = [r for r in results if r and not r["ok"]]
        self.assertEqual(len(accepted), 1, f"results={results}")
        self.assertEqual(len(rejected), 1, f"results={results}")
        self.assertEqual(rejected[0]["error_code"], "MAX_SYMBOL_EXPOSURE")

        symbol_gross = sum(
            float(p.qty) for p in Position.objects.filter(symbol="EUR/USD")
        )
        self.assertLessEqual(symbol_gross, 20.0)

    def test_two_accounts_race_max_total_broker_exposure(self):
        _seed_price("BTCUSD", 100)
        _seed_price("ETHUSD", 50)
        acc1 = make_account(balance=Decimal("1000000.00"))
        acc2 = make_account(balance=Decimal("1000000.00"))
        # Pre-existing broker-wide exposure on a THIRD symbol/account so
        # this is genuinely a broker-wide (not per-symbol) race.
        acc0 = make_account(balance=Decimal("1000000.00"))
        make_position(acc0, symbol="BTCUSD", side="BUY", qty=Decimal("15"), avg_price=Decimal("100"))

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("20")):
            threads = [
                threading.Thread(target=_open_in_thread,
                                  args=(acc1.pk, "BTCUSD", 3.0, 100, barrier, results, 0)),
                threading.Thread(target=_open_in_thread,
                                  args=(acc2.pk, "ETHUSD", 3.0, 50, barrier, results, 1)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        accepted = [r for r in results if r and r["ok"]]
        rejected = [r for r in results if r and not r["ok"]]
        self.assertEqual(len(accepted), 1, f"results={results}")
        self.assertEqual(len(rejected), 1, f"results={results}")
        self.assertEqual(rejected[0]["error_code"], "MAX_TOTAL_BROKER_EXPOSURE")

        total_qty = sum(float(p.qty) for p in Position.objects.all())
        self.assertLessEqual(total_qty, 20.0)

    def test_two_accounts_race_max_gross_notional(self):
        _seed_price("BTCUSD", 100)
        acc1 = make_account(balance=Decimal("1000000.00"))
        acc2 = make_account(balance=Decimal("1000000.00"))
        make_position(acc1, symbol="BTCUSD", side="BUY", qty=Decimal("15"), avg_price=Decimal("100"))

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        # gross_notional limit = 2000: current 1500 (15*100), each new
        # order adds 300 (3*100) -> either alone fits (1800<=2000), both
        # together do not (2100>2000).
        with patch.object(br, "MAX_GROSS_NOTIONAL", Decimal("2000")):
            threads = [
                threading.Thread(target=_open_in_thread,
                                  args=(acc1.pk, "BTCUSD", 3.0, 100, barrier, results, 0)),
                threading.Thread(target=_open_in_thread,
                                  args=(acc2.pk, "BTCUSD", 3.0, 100, barrier, results, 1)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        accepted = [r for r in results if r and r["ok"]]
        rejected = [r for r in results if r and not r["ok"]]
        self.assertEqual(len(accepted), 1, f"results={results}")
        self.assertEqual(len(rejected), 1, f"results={results}")

        total_notional = sum(float(p.qty) * float(p.avg_price) for p in Position.objects.filter(symbol="BTCUSD"))
        self.assertLessEqual(total_notional, 2000.0)


# ─────────────────────────────────────────────────────────────────────────
# 6. Lock release / rollback / exception safety
# ─────────────────────────────────────────────────────────────────────────
class LockAndRollbackTests(TransactionTestCase):
    def test_rejected_order_releases_lock_for_next_call(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))

        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            rejected = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 3.0, 1.1, None, None,
                commission=0.0, new_balance=1000000.0,
            )
        self.assertFalse(rejected["ok"])

        # If BrokerRiskLock were still held, this second call — on a
        # DIFFERENT symbol/account — would hang until the test's own
        # timeout; it must complete immediately.
        _seed_price("BTCUSD", 100)
        other_account = make_account(balance=Decimal("1000000.00"))
        accepted = _db_open_sync(
            _consumer(other_account.pk), "BTCUSD", "buy", 1.0, 100, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(accepted["ok"])

    def test_exception_during_open_leaves_no_partial_state_and_releases_lock(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))

        with patch("simulator.consumers.Position.objects.create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _db_open_sync(
                    _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
                    commission=0.0, new_balance=1000000.0,
                )

        # Nothing partial: no Position, balance untouched.
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("1000000.00"))

        # Lock released despite the exception (transaction.atomic() always
        # rolls back and releases on an unhandled exception) — a fresh
        # call must not hang.
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(result["ok"])


# ─────────────────────────────────────────────────────────────────────────
# 7. Lock order — structural
# ─────────────────────────────────────────────────────────────────────────
class LockOrderStructuralTests(TestCase):
    def test_broker_risk_lock_query_precedes_tradingaccount_query(self):
        from django.test.utils import CaptureQueriesContext
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))

        with CaptureQueriesContext(connection) as ctx:
            _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
                commission=0.0, new_balance=1000000.0,
            )

        sql_upper = [q["sql"].upper() for q in ctx.captured_queries]
        lock_idx = next(i for i, s in enumerate(sql_upper) if "BROKERRISKLOCK" in s)
        account_idx = next(i for i, s in enumerate(sql_upper) if "TRADINGACCOUNT" in s and "SELECT" in s)
        self.assertLess(lock_idx, account_idx,
                         "BrokerRiskLock must be queried/locked before TradingAccount")

    def test_singleton_row_exists(self):
        self.assertEqual(BrokerRiskLock.objects.filter(pk=1).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 8. Integration — the real _order_new() WS handler
# ─────────────────────────────────────────────────────────────────────────
class IntegrationTests(_CleanFeedMixin, TransactionTestCase):
    def test_order_rejected_by_broker_wide_limit_creates_nothing(self):
        blocker_account = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(blocker_account, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1000"))
        _seed_price("EUR/USD", 1.1000)

        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _ws_consumer(account.id)
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            _run(c._order_new({"action": "order:new", "symbol": "EUR/USD", "side": "buy", "qty": 3}))

        sent = [call.args[0] for call in c.send_json.call_args_list]
        errors = [m for m in sent if m.get("type") == "error"]
        self.assertTrue(any(m.get("code") == "MAX_SYMBOL_EXPOSURE" for m in errors))
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)

    def test_order_within_limits_still_opens_normally(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _ws_consumer(account.id)
        _run(c._order_new({"action": "order:new", "symbol": "EUR/USD", "side": "buy", "qty": 0.01}))

        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent = [call.args[0] for call in c.send_json.call_args_list]
        self.assertTrue(any(m.get("type") == "order_ack" for m in sent))

    def test_demo_session_bypasses_broker_wide_gate(self):
        account = make_account(account_type="DEMO", balance=Decimal("100000"))
        c = _ws_consumer(account.id)
        c._db_account_id = None
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("0")):
            # Even an impossible limit must not block a demo session —
            # _db_open_position_atomic returns ok=True before ever
            # touching BrokerRiskLock when _db_account_id is None.
            result = _run(c._db_open_position_atomic(
                "EUR/USD", "buy", 1.0, 1.1, None, None, commission=0.0, new_balance=100000.0,
            ))
        self.assertTrue(result["ok"])


# ─────────────────────────────────────────────────────────────────────────
# 9. No regression
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TransactionTestCase):
    def test_book02_book03_risk01_unaffected_with_default_limits(self):
        from simulator.models import BrokerLedger
        from simulator.broker_pnl import broker_pnl_for_account, PERIOD_LIFETIME
        from simulator.broker_exposure import broker_exposure_for_account
        from .test_broker_counterparty_pnl import _open, _close, _pos_mem

        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _ws_consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10.00"))
        bp = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(bp.counterparty_pnl, Decimal("-10.00"))
        exp = broker_exposure_for_account(account.id)
        self.assertEqual(exp.open_position_count, 0)

    def test_trader_facing_formulas_unchanged(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _ws_consumer(account.id)
        _run(c._order_new({"action": "order:new", "symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        account.refresh_from_db()
        pos = Position.objects.get(account=account)
        self.assertEqual(pos.qty, Decimal("0.01"))
        self.assertEqual(pos.symbol, "EUR/USD")
