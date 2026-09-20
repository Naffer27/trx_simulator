# simulator/tests/test_ib_per_lot_execution_event_01.py
"""
IB-PER-LOT-EXECUTION-EVENT-01

Regression coverage for the two unconditional LotExecutionEvent insertion
points added to simulator/consumers.py::_db_open_position_atomic (manual
WS opens + same-side merges) and simulator/consumers.py::
_trigger_pending_order_core (pending/stop/limit triggers, shared by the
live WS tick path and the offline Celery daemon).

Both insertion points are deliberately NOT wrapped in try/except and NOT
a nested transaction.atomic() savepoint — a failure to write
LotExecutionEvent must roll back the entire execution (Position write,
balance, commission, spread), never proceed silently without it.

Uses the same "call the real atomic function directly, .__wrapped__"
pattern already established in test_atomic_margin_and_position_guard.py /
test_order_management_v2a.py — no HTTP, no WS framing.
"""
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer, _trigger_pending_order_core
from simulator.models import (
    BrokerLedger, LedgerEntry, LotExecutionEvent, PendingOrder, Position,
    TradingAccount, Trade,
)

from .factories import make_account, make_position

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__

EURUSD_SPEC = get_spec("EUR/USD")

_SEED_PRICES = {"EUR/USD": (1.1699, 1.1701)}


def _seed_prices():
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


def _consumer(account_id, netting_mode=False, leverage=50):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0,
        "leverage": leverage, "allowed_symbols": None,
        "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


def _pos_mem(pos: Position) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


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


# ─────────────────────────────────────────────────────────────────────────
# 1/2 — manual WS: new open + same-side merge
# ─────────────────────────────────────────────────────────────────────────

class ManualWsNewOpenTests(_PricedTestCase):
    def test_new_open_creates_exactly_one_event(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])

        events = list(LotExecutionEvent.objects.filter(account=account))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.position_id, result["position_id"])
        self.assertEqual(ev.symbol, "EUR/USD")
        self.assertEqual(ev.side, "BUY")
        self.assertEqual(ev.qty, Decimal("0.02"))
        self.assertEqual(ev.execution_price, Decimal("1.1701"))
        self.assertFalse(ev.merged)
        self.assertEqual(ev.entry_path, LotExecutionEvent.ENTRY_MANUAL_WS)
        self.assertIsNone(ev.source_order_id)


class ManualWsSameSideMergeTests(_PricedTestCase):
    def test_merge_creates_one_new_event_with_incremental_qty(self):
        account = make_account(balance=Decimal("10000"))
        consumer = _consumer(account.pk, netting_mode=True)

        r1 = _db_open_sync(
            consumer, "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(r1["ok"])
        self.assertEqual(LotExecutionEvent.objects.filter(account=account).count(), 1)

        r2 = _db_open_sync(
            consumer, "EUR/USD", "buy", 0.03, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(r2["ok"])
        self.assertTrue(r2["merged"])
        # Same underlying Position row — merges never create a new one.
        self.assertEqual(r2["position_id"], r1["position_id"])

        events = list(
            LotExecutionEvent.objects.filter(account=account).order_by("created_at")
        )
        self.assertEqual(len(events), 2, "one NEW event per execution, not overwritten")
        self.assertFalse(events[0].merged)
        self.assertEqual(events[0].qty, Decimal("0.02"))
        self.assertTrue(events[1].merged)
        self.assertEqual(
            events[1].qty, Decimal("0.03"),
            "qty must be the INCREMENTAL amount of this execution, not the "
            "Position's new cumulative total (0.05)",
        )

        # The Position's own cumulative qty is 0.05 — confirms the event's
        # 0.03 really is incremental, not a copy of the position total.
        pos = Position.objects.get(pk=r1["position_id"])
        self.assertEqual(pos.qty, Decimal("0.05"))


# ─────────────────────────────────────────────────────────────────────────
# 3 — pending/stop/limit trigger
# ─────────────────────────────────────────────────────────────────────────

class PendingTriggerTests(TestCase):
    def test_trigger_creates_exactly_one_event_with_source_order_id(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")

        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])

        events = list(LotExecutionEvent.objects.filter(account=account))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.entry_path, LotExecutionEvent.ENTRY_PENDING_TRIGGER)
        self.assertEqual(ev.source_order_id, po.id)
        self.assertEqual(ev.qty, Decimal("0.01"))
        self.assertEqual(ev.execution_price, Decimal("1.0995"))
        self.assertFalse(ev.merged)
        self.assertEqual(ev.position_id, result["position_id"])


# ─────────────────────────────────────────────────────────────────────────
# 4 — forced insert failure rolls back the ENTIRE execution
# ─────────────────────────────────────────────────────────────────────────

class ForcedInsertFailureRollsBackEverythingTests(_PricedTestCase):
    def test_manual_ws_open_forced_failure_rolls_back_everything(self):
        account = make_account(balance=Decimal("10000"))
        balance_before = account.balance

        with patch(
            "simulator.models.LotExecutionEvent.objects.create",
            side_effect=RuntimeError("forced LotExecutionEvent failure"),
        ):
            with self.assertRaises(RuntimeError):
                _db_open_sync(
                    _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
                    commission=5.0, new_balance=9995.0,
                )

        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before, "balance must not move")
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 0)
        self.assertEqual(LotExecutionEvent.objects.filter(account=account).count(), 0)

    def test_pending_trigger_forced_failure_rolls_back_everything(self):
        account = make_account(balance=Decimal("10000"))
        balance_before = account.balance
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")

        with patch(
            "simulator.models.LotExecutionEvent.objects.create",
            side_effect=RuntimeError("forced LotExecutionEvent failure"),
        ):
            with self.assertRaises(RuntimeError):
                _trigger_pending_order_core(po.id, execution_price=1.09950)

        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        po.refresh_from_db()
        self.assertEqual(
            po.status, PendingOrder.PENDING,
            "a rolled-back trigger must leave the PendingOrder untouched, not TRIGGERED",
        )


# ─────────────────────────────────────────────────────────────────────────
# 5/6 — Position close/delete: event survives, position_id -> NULL, no
#        ProtectedError
# ─────────────────────────────────────────────────────────────────────────

class PositionCloseSurvivalTests(_PricedTestCase):
    def test_event_survives_close_with_position_nulled_and_fields_intact(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])
        pos = Position.objects.get(pk=result["position_id"])
        ev_id = LotExecutionEvent.objects.get(account=account).id

        # Close it via the real, authoritative close path — no
        # ProtectedError must be raised (item 6).
        close_result = _db_close_sync(
            _consumer(account.pk), _pos_mem(pos), 1.1705, "manual",
            0.80, 10000.80, 10000.80,
        )
        self.assertTrue(close_result["ok"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

        ev = LotExecutionEvent.objects.get(pk=ev_id)
        self.assertIsNone(ev.position_id, "position FK must be nulled, not the row deleted")
        self.assertEqual(ev.symbol, "EUR/USD")
        self.assertEqual(ev.side, "BUY")
        self.assertEqual(ev.qty, Decimal("0.02"))
        self.assertEqual(ev.execution_price, Decimal("1.1701"))
        self.assertFalse(ev.merged)
        self.assertEqual(ev.entry_path, LotExecutionEvent.ENTRY_MANUAL_WS)


# ─────────────────────────────────────────────────────────────────────────
# 7 — race on the same PendingOrder produces exactly one event
# ─────────────────────────────────────────────────────────────────────────

def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same helper pattern as test_book06j1_population_engine_close_race.py
    / test_fix_orphan_position_close_sync_01.py, duplicated locally so
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


class PendingTriggerRaceTests(TransactionTestCase):
    def test_two_concurrent_triggers_on_same_pending_order_produce_one_event(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n

        def _trigger():
            return _trigger_pending_order_core(po.id, execution_price=1.09950)

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_trigger, barrier, results, 0)),
            threading.Thread(target=_run_locked_retry, args=(_trigger, barrier, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertIsNotNone(results[0], "trigger A did not complete — possible deadlock")
        self.assertIsNotNone(results[1], "trigger B did not complete — possible deadlock")

        self.assertEqual(
            LotExecutionEvent.objects.filter(account=account).count(), 1,
            "the PendingOrder's own row lock must serialize the race down to one event",
        )
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
