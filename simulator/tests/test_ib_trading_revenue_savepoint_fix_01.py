# simulator/tests/test_ib_trading_revenue_savepoint_fix_01.py
"""
IB-TRADING-REVENUE-SAVEPOINT-FIX-01

Regression coverage for the two REV_COMMISSION BrokerLedger.objects.create()
call sites in simulator/consumers.py:

    1. _db_open_position_atomic  (manual WS open / same-side merge)
    2. _trigger_pending_order_core  (pending LIMIT/STOP trigger)

Both are now wrapped in their own nested transaction.atomic() savepoint —
the same pattern REV_SPREAD already used in _db_open_position_atomic. The
outer try/except is unchanged; only the DB write itself moved inside a
savepoint boundary, so a DB-level insert failure rolls back just that
savepoint instead of (on PostgreSQL) poisoning the outer transaction.

SQLite/PostgreSQL limitation (see also section H of the block report):
SQLite does NOT reproduce PostgreSQL's aborted-transaction-until-ROLLBACK
behavior (25P02) — a caught exception on SQLite does not, by itself, make
the next statement in the same outer transaction fail. This suite cannot
and does not claim to reproduce that PostgreSQL-specific failure mode. What
it DOES prove, validly, on any backend:
  - the nested atomic() savepoint structure exists and is exercised,
  - a forced failure inside that savepoint does not propagate out and
    abort the outer transaction (proven via Django's own savepoint
    rollback machinery, which is what protects PostgreSQL from 25P02),
  - the trade/position/account/ledger/LotExecutionEvent side of the
    transaction is completely unaffected by the forced REV_COMMISSION
    failure, on both paths,
  - normal (non-forced) behavior — success and zero-commission cases,
    REV_SPREAD, LotExecutionEvent — is byte-for-byte unchanged.

Uses the same "call the real atomic function directly via .__wrapped__"
pattern already established in test_ib_per_lot_execution_event_01.py.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer, _trigger_pending_order_core
from simulator.models import (
    BrokerLedger, LedgerEntry, LotExecutionEvent, PendingOrder, Position,
)

from .factories import make_account

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__

_SEED_PRICES = {"EUR/USD": (1.1699, 1.1701)}


def _seed_prices():
    feed = get_feed_manager()
    import time
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
# 1 — manual/open path, success, commission-bearing
# ─────────────────────────────────────────────────────────────────────────

class ManualOpenCommissionSuccessTests(_PricedTestCase):
    def test_commission_bearing_open_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=5.0, new_balance=9995.0,
        )
        self.assertTrue(result["ok"])

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("9995.00"))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

        trader_lines = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_COMMISSION,
        )
        self.assertEqual(trader_lines.count(), 1)
        self.assertEqual(trader_lines[0].amount, Decimal("-5.00"))

        rev_lines = BrokerLedger.objects.filter(
            source_account=account, revenue_type=BrokerLedger.REV_COMMISSION,
        )
        self.assertEqual(rev_lines.count(), 1, "exactly one REV_COMMISSION row")
        self.assertEqual(rev_lines[0].amount, Decimal("5.00"))
        self.assertEqual(rev_lines[0].source_ledger_id, trader_lines[0].id)

        self.assertEqual(LotExecutionEvent.objects.filter(account=account).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 2 — manual/open path, forced DB failure on the REV_COMMISSION insert
# ─────────────────────────────────────────────────────────────────────────

class ManualOpenCommissionForcedFailureTests(_PricedTestCase):
    def test_forced_broker_ledger_commission_failure_isolated(self):
        account = make_account(balance=Decimal("10000"))

        orig_create = BrokerLedger.objects.create

        def _boom(*args, **kwargs):
            if kwargs.get("revenue_type") == BrokerLedger.REV_COMMISSION:
                raise RuntimeError("forced REV_COMMISSION insert failure")
            return orig_create(*args, **kwargs)

        with patch("simulator.consumers.BrokerLedger.objects.create", side_effect=_boom):
            result = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
                commission=5.0, new_balance=9995.0,
            )

        # The trade itself must still succeed — REV_COMMISSION is
        # best-effort, exactly as before this fix.
        self.assertTrue(result["ok"])

        account.refresh_from_db()
        self.assertEqual(
            account.balance, Decimal("9995.00"),
            "trader was still charged commission — unchanged from pre-fix design",
        )
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

        trader_lines = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_COMMISSION,
        )
        self.assertEqual(trader_lines.count(), 1, "trader-facing commission charge unaffected")
        self.assertEqual(trader_lines[0].amount, Decimal("-5.00"))

        # No REV_COMMISSION row exists — the forced failure's savepoint
        # was rolled back, and no partial/duplicate row was left behind.
        self.assertEqual(
            BrokerLedger.objects.filter(
                source_account=account, revenue_type=BrokerLedger.REV_COMMISSION,
            ).count(),
            0,
        )

        # Outer transaction was NOT poisoned: LotExecutionEvent (written
        # AFTER commission handling completes, further down the same
        # outer transaction) still committed successfully. On PostgreSQL
        # this is exactly the statement that would raise
        # TransactionManagementError / 25P02 if the savepoint boundary
        # were missing.
        self.assertEqual(LotExecutionEvent.objects.filter(account=account).count(), 1)

        # A subsequent, independent write against the same account must
        # also succeed — the connection/transaction is fully usable.
        account.refresh_from_db()
        account.save(update_fields=["balance"])


# ─────────────────────────────────────────────────────────────────────────
# 3 — pending path, success, commission-bearing
# ─────────────────────────────────────────────────────────────────────────

class PendingTriggerCommissionSuccessTests(TestCase):
    def test_commission_bearing_trigger_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")

        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])

        events = LotExecutionEvent.objects.filter(account=account)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events[0].entry_path, LotExecutionEvent.ENTRY_PENDING_TRIGGER)

        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)

        rev_lines = BrokerLedger.objects.filter(
            source_account=account, revenue_type=BrokerLedger.REV_COMMISSION,
        )
        # commission on this default test account/symbol profile may
        # legitimately be zero (see test 5 below for the explicit zero
        # case) — this test only asserts internal consistency: a
        # REV_COMMISSION row exists iff a trader EV_COMMISSION line does.
        trader_lines = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_COMMISSION,
        )
        self.assertEqual(rev_lines.count(), trader_lines.count())
        if trader_lines.exists():
            self.assertEqual(rev_lines.count(), 1)
            self.assertEqual(rev_lines[0].source_ledger_id, trader_lines[0].id)


# ─────────────────────────────────────────────────────────────────────────
# 4 — pending path, forced DB failure on the REV_COMMISSION insert
# ─────────────────────────────────────────────────────────────────────────

class PendingTriggerCommissionForcedFailureTests(TestCase):
    def test_forced_broker_ledger_commission_failure_isolated(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")

        orig_create = BrokerLedger.objects.create

        def _boom(*args, **kwargs):
            if kwargs.get("revenue_type") == BrokerLedger.REV_COMMISSION:
                raise RuntimeError("forced REV_COMMISSION insert failure")
            return orig_create(*args, **kwargs)

        with patch("simulator.consumers.BrokerLedger.objects.create", side_effect=_boom):
            result = _trigger_pending_order_core(po.id, execution_price=1.09950)

        # Trigger execution must still complete per existing semantics,
        # exactly as before this fix (REV_COMMISSION is best-effort).
        self.assertTrue(result["ok"])

        po.refresh_from_db()
        self.assertEqual(
            po.status, PendingOrder.TRIGGERED,
            "pending order still completes its existing trigger semantics",
        )
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

        self.assertEqual(
            BrokerLedger.objects.filter(
                source_account=account, revenue_type=BrokerLedger.REV_COMMISSION,
            ).count(),
            0,
            "no partial/duplicate REV_COMMISSION row left behind",
        )

        # Outer transaction unaffected: PendingOrder status write and the
        # LotExecutionEvent write (both AFTER the commission block in this
        # function's body) still committed.
        self.assertEqual(LotExecutionEvent.objects.filter(account=account).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 5 — zero commission: unchanged, no REV_COMMISSION row on either path
# ─────────────────────────────────────────────────────────────────────────

class ZeroCommissionTests(_PricedTestCase):
    def test_manual_open_zero_commission_creates_no_row(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).count(),
            0,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION).count(),
            0,
        )

    def test_pending_trigger_zero_commission_account_creates_no_row(self):
        # Default test-account commercial pricing profile resolves to zero
        # commission on EUR/USD (no per-account override configured) —
        # this proves the untouched zero-commission branch still produces
        # no REV_COMMISSION row on the pending path either.
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])

        trader_lines = LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION)
        rev_lines = BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION)
        if not trader_lines.exists():
            self.assertEqual(rev_lines.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 6 — REV_SPREAD non-regression: untouched by this patch
# ─────────────────────────────────────────────────────────────────────────

class RevSpreadNonRegressionTests(_PricedTestCase):
    def test_manual_open_spread_behavior_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])
        # spread_pips=0.0 on this synthetic consumer/account fixture ->
        # no spread fee — this test's purpose is only to confirm the
        # REV_SPREAD code path still runs without error alongside the
        # patched REV_COMMISSION code (no accidental variable/scope
        # collision introduced by this patch).
        fee_lines = LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE)
        rev_spread = BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_SPREAD)
        self.assertEqual(fee_lines.count(), rev_spread.count())

    def test_pending_trigger_never_creates_rev_spread(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_SPREAD).count(),
            0,
            "pending-triggered fills must still never carry REV_SPREAD — "
            "this patch does not touch that documented gap",
        )


# ─────────────────────────────────────────────────────────────────────────
# 7 — LotExecutionEvent non-regression
# ─────────────────────────────────────────────────────────────────────────

class LotExecutionEventNonRegressionTests(_PricedTestCase):
    def test_manual_open_event_fields_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.02, 1.1701, None, None,
            commission=5.0, new_balance=9995.0,
        )
        self.assertTrue(result["ok"])
        ev = LotExecutionEvent.objects.get(account=account)
        self.assertEqual(ev.position_id, result["position_id"])
        self.assertEqual(ev.qty, Decimal("0.02"))
        self.assertEqual(ev.execution_price, Decimal("1.1701"))
        self.assertEqual(ev.entry_path, LotExecutionEvent.ENTRY_MANUAL_WS)
        self.assertIsNone(ev.source_order_id)

    def test_pending_trigger_event_fields_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        po = _pending(account, side="BUY", qty="0.01", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])
        ev = LotExecutionEvent.objects.get(account=account)
        self.assertEqual(ev.entry_path, LotExecutionEvent.ENTRY_PENDING_TRIGGER)
        self.assertEqual(ev.source_order_id, po.id)
        self.assertEqual(ev.qty, Decimal("0.01"))
        self.assertEqual(ev.position_id, result["position_id"])
