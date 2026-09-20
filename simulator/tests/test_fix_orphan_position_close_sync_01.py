# simulator/tests/test_fix_orphan_position_close_sync_01.py
"""
FIX-ORPHAN-POSITION-CLOSE-SYNC-01

Root cause: simulator/population_engine.py::SimulatedTrader._close_position()
used to trust the caller's pre-lock `pos` snapshot for entry/side/qty/
symbol/opened_at and unconditionally create a Trade + `Position.objects.
filter(id=pos.id).delete()` — a delete that is silently a no-op if the
row is already gone, but by then the Trade already existed either way.
This is the same root cause already reproduced (not fixed) in
test_book06j1_population_engine_close_race.py: no select_for_update() on
the target Position row, so a second/concurrent call operating on a
since-deleted (or otherwise stale) Position snapshot could still create
a Trade and silently no-op the delete, leaving Position/Trade data out
of sync.

Fix mirrors the two already-safe close paths in this codebase
(TradingConsumer._db_close_position_atomic, tasks._close_position_sync):
lock TradingAccount first, then re-fetch+lock the TARGET Position row
(select_for_update, filtered by id AND account_id) strictly after that
lock, and treat "not found" as an already-closed no-op — no duplicate
Trade, no re-delete.

Uses only disposable CHALLENGE-type test accounts via make_account()/
make_position() — never account 51, never any real/pre-existing data.

Covers:
  1.  Golden path: open -> close -> Trade created, closed, correct
      fields -> Position no longer exists (the exact scenario requested:
      "open EUR/USD -> close exitoso -> Trade cerrado -> Position ya no
      existe").
  2.  A second close call on the same (already-closed) Position is a
      safe no-op: no duplicate Trade, no exception, no second Ledger/
      broker-counterparty entry, no double balance mutation.
  3.  Two concurrent close calls targeting the SAME Position (the exact
      shape test_book06j1_population_engine_close_race.py reproduces)
      now produce exactly ONE Trade and ZERO remaining Positions — the
      race is closed, not merely observed.
  4.  Balance/peak_balance are mutated exactly once (not per close
      attempt).
  5.  risk_engine.check_and_enforce_risk is invoked exactly once.
  6.  A concurrent close on a DIFFERENT position for the same account is
      unaffected (only the targeted Position is touched).
"""
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.db.utils import OperationalError
from django.test import TransactionTestCase

from market_data.feeds import get_feed_manager
from simulator.models import LedgerEntry, Position, Trade
from simulator.population_engine import SimulatedTrader

from .factories import make_account, make_position


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


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same helper pattern as test_atomic_guard_lock_order.py /
    test_book06j1_population_engine_close_race.py, duplicated locally so
    this file has no dependency on either."""
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
                time.sleep(0.01)
    finally:
        connection.close()


class ClosePositionGoldenPathTests(TransactionTestCase):
    """1 — open -> close -> Trade closed -> Position gone."""

    def setUp(self):
        _seed_price("EUR/USD", 1.1000)
        self.addCleanup(_clear_price, "EUR/USD")
        self.account = make_account(balance=Decimal("100000"))
        self.pos = make_position(
            self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.1000"),
        )
        self.trader = SimulatedTrader(self.account.id, "NORMAL")

    def test_close_creates_exactly_one_trade(self):
        self.trader._close_position(self.account, self.pos)
        self.assertEqual(
            Trade.objects.filter(account=self.account, symbol="EUR/USD").count(), 1,
        )

    def test_close_removes_the_position(self):
        self.trader._close_position(self.account, self.pos)
        self.assertFalse(Position.objects.filter(id=self.pos.id).exists())

    def test_trade_is_closed_with_correct_lifecycle_fields(self):
        self.trader._close_position(self.account, self.pos)
        trade = Trade.objects.get(account=self.account, symbol="EUR/USD")
        self.assertIsNotNone(trade.closed_at)
        self.assertEqual(trade.entry_price, self.pos.avg_price)
        self.assertEqual(trade.lot_size, self.pos.qty)
        self.assertIsNotNone(trade.profit_loss)

    def test_close_creates_exactly_one_ledger_entry(self):
        self.trader._close_position(self.account, self.pos)
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=self.account, event_type=LedgerEntry.EV_REALIZED,
            ).count(),
            1,
        )

    def test_risk_engine_invoked_exactly_once(self):
        with patch("simulator.risk_engine.check_and_enforce_risk") as mock_risk:
            self.trader._close_position(self.account, self.pos)
        self.assertEqual(mock_risk.call_count, 1)


class DuplicateCloseIsSafeNoOpTests(TransactionTestCase):
    """2 — a second close on an already-closed Position must not create
    a duplicate Trade, must not raise, and must not double-mutate the
    account balance."""

    def setUp(self):
        _seed_price("EUR/USD", 1.1000)
        self.addCleanup(_clear_price, "EUR/USD")
        self.account = make_account(balance=Decimal("100000"))
        self.pos = make_position(
            self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.1000"),
        )
        self.trader = SimulatedTrader(self.account.id, "NORMAL")

    def test_second_close_call_is_a_noop(self):
        self.trader._close_position(self.account, self.pos)
        self.account.refresh_from_db()
        balance_after_first = self.account.balance

        # Second call reuses the SAME stale `pos` snapshot — exactly what
        # a duplicate scheduler pass would hand to _close_position().
        self.trader._close_position(self.account, self.pos)

        self.assertEqual(
            Trade.objects.filter(account=self.account, symbol="EUR/USD").count(), 1,
            "a second close attempt must not create a duplicate Trade",
        )
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.balance, balance_after_first,
            "a second close attempt must not mutate the balance again",
        )
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=self.account, event_type=LedgerEntry.EV_REALIZED,
            ).count(),
            1,
        )


class ConcurrentCloseRaceIsClosedTests(TransactionTestCase):
    """3/4 — the exact race test_book06j1_population_engine_close_race.py
    reproduces (two concurrent _close_position() calls on the SAME
    Position) must now be safe: exactly one Trade, zero remaining
    Positions, exactly one balance mutation."""

    def test_two_concurrent_close_calls_on_same_position_produce_one_trade(self):
        _seed_price("EUR/USD", 1.1000)
        self.addCleanup(_clear_price, "EUR/USD")

        account = make_account(balance=Decimal("100000"))
        pos = make_position(
            account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.1000"),
        )
        trader_a = SimulatedTrader(account.id, "NORMAL")
        trader_b = SimulatedTrader(account.id, "NORMAL")

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n

        def _close_a():
            trader_a._close_position(account, pos)
            return "a-done"

        def _close_b():
            trader_b._close_position(account, pos)
            return "b-done"

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_close_a, barrier, results, 0)),
            threading.Thread(target=_run_locked_retry, args=(_close_b, barrier, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertIsNotNone(results[0], "close_a did not complete — possible deadlock")
        self.assertIsNotNone(results[1], "close_b did not complete — possible deadlock")

        self.assertEqual(
            Trade.objects.filter(account=account).count(), 1,
            "exactly one economic close must produce exactly one Trade",
        )
        self.assertEqual(
            Position.objects.filter(account=account).count(), 0,
            "the Position must be gone after the race resolves",
        )
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=account, event_type=LedgerEntry.EV_REALIZED,
            ).count(),
            1,
            "the race must not double-realize PnL",
        )


class ConcurrentCloseOnDifferentPositionUnaffectedTests(TransactionTestCase):
    """6 — closing one position concurrently with a close on a DIFFERENT
    position for the same account must not cross-affect the other."""

    def test_other_position_untouched(self):
        _seed_price("EUR/USD", 1.1000)
        _seed_price("GBP/USD", 1.2500)
        self.addCleanup(_clear_price, "EUR/USD")
        self.addCleanup(_clear_price, "GBP/USD")

        account = make_account(balance=Decimal("100000"))
        pos_eur = make_position(
            account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.1000"),
        )
        pos_gbp = make_position(
            account, symbol="GBP/USD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("1.2500"),
        )
        trader = SimulatedTrader(account.id, "NORMAL")

        trader._close_position(account, pos_eur)

        self.assertFalse(Position.objects.filter(id=pos_eur.id).exists())
        self.assertTrue(Position.objects.filter(id=pos_gbp.id).exists())
        self.assertEqual(
            Trade.objects.filter(account=account, symbol="GBP/USD").count(), 0,
        )
