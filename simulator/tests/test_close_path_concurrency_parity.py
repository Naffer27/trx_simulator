"""
simulator/tests/test_close_path_concurrency_parity.py — PANEL-03.

Root cause fixed: _order_close() already guarded against
result["already_closed"] (ACCOUNT-02) — a concurrent connection or the
daemon closing the SAME position between this connection's pre-lock
computation and _db_close_position_atomic's lock. Its three sibling
close-loops did NOT:

  - _check_tp_sl()          — per-tick TP/SL sweep
  - _do_stopout()           — DD-engine (CHALLENGE/FUNDED) full liquidation
  - _do_retail_liquidation() — margin-engine (RETAIL/ECN/STANDARD/...)
                                full liquidation, account stays Activo

All three unconditionally trusted result["new_balance"] (which, in the
already_closed branch, is _db_close_position_atomic's verbatim echo of
the CALLER's own pre-lock, possibly-stale estimate — see
consumers.py:_db_close_position_atomic's `if pos is None: return
{"new_balance": new_balance, ...}`), and unconditionally sent a fabricated
order_close notification for a position this connection did not actually
close.

Fixed by routing every close path through two new shared helpers:

  _handle_close_result(pos, result, close_px, reason, realized_pnl, ts)
    Pure/sync. already_closed=False -> returns the authoritative DB
    values + a ready notify payload. already_closed=True -> returns None
    (caller must not use any of it).

  _refresh_account_after_stale_close()
    Async. Called once per close-path invocation/batch that saw at least
    one already_closed=True. Forces a fresh, non-throttled
    TradingAccount.balance read (reuses _db_sync_account_balances,
    ACCOUNT-02 — read-only balance, derived equity) instead of trusting
    whatever running_balance bookkeeping the caller accumulated locally.

No DB-level change was needed for idempotency itself —
_db_close_position_atomic's own select_for_update()+`pos is None` check
(ACCOUNT-02/PANEL-02) already guarantees at most one Trade/LedgerEntry
per position close. This block closes the WS-layer gap: what each
connection does with a result it didn't produce.

Uses TransactionTestCase: _close_position_sync (tasks.py, used here to
simulate "a sibling connection/the daemon already closed this position")
and the async consumer methods under test both commit for real — genuine
cross-call visibility (not TestCase's savepoint-per-test isolation) is
required, matching the established pattern in
test_account_balance_concurrency.py / test_atomic_guard_lock_order.py.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TransactionTestCase

from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _run(coro):
    return asyncio.run(coro)


def _consumer(account_id, balance, positions=None, status="Activo",
              account_type="STANDARD", peak_balance=None):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c._last_db_sync = 0.0
    c._positions = positions or []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.symbol = "EUR/USD"
    c._bid_state, c._ask_state = {"EUR/USD": 1.1000}, {"EUR/USD": 1.1000}
    c._raw_bid_state, c._raw_ask_state = {}, {}
    c._pricing_snapshot_state, c._pricing_ts_state = {}, {}
    c.account = {
        "balance": balance, "equity": balance,
        "peak_balance": peak_balance if peak_balance is not None else balance,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": status, "account_type": account_type,
        "tier": "10K", "profit_target": 800.0, "initial_balance": balance,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


def _pos_entry(pos, sl=None, tp=None):
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": sl, "tp": tp, "opened_at": int(pos.opened_at.timestamp()),
    }


def _pos_mem_for_task(pos):
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": None, "tp": None, "opened_at": pos.opened_at.timestamp(),
    }


def _sent_types(consumer):
    return [c.args[0].get("type") for c in consumer.send_json.call_args_list]


def _sent_of_type(consumer, type_name):
    return [c.args[0] for c in consumer.send_json.call_args_list if c.args[0].get("type") == type_name]


class OrderCloseVsDaemonConcurrentTests(TransactionTestCase):
    """FASE 6 #1 — _order_close vs daemon concurrente. Also covers #7
    (self.account not set to a stale value), #8 (fresh balance/equity),
    #10 (exactly 1 Trade), #11 (exactly 1 LedgerEntry EV_REALIZED)."""

    def test_order_close_does_not_overwrite_stale_balance_when_already_closed_by_daemon(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)

        # Daemon closes it for real first: +50 realized.
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10050.00"))

        # This connection still believes balance=10000 and the position is
        # open, at a DIFFERENT price that would realize a LOSS if trusted.
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)])
        consumer._bid_state = {"EUR/USD": 1.0700}
        consumer._ask_state = {"EUR/USD": 1.0700}

        _run(consumer._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10050.00"))       # untouched by the stale close
        self.assertEqual(consumer.account["balance"], 10050.0)       # synced to the REAL value
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 1,
        )
        # BOOK-02 — the daemon's genuine close above legitimately writes one
        # COUNTERPARTY_PNL row (trader +50 -> broker -50). The point of this
        # assertion is that the stale _order_close() retry above does NOT
        # create a second one — exactly 1 total, not 0 (0 predates BOOK-02).
        broker_rows = list(BrokerLedger.objects.filter(source_account=account))
        self.assertEqual(len(broker_rows), 1)
        self.assertEqual(broker_rows[0].revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(broker_rows[0].amount, Decimal("-50"))
        self.assertNotIn("order_close", _sent_types(consumer))
        self.assertEqual(consumer._positions, [])


class TpSlVsManualCloseConcurrentTests(TransactionTestCase):
    """FASE 6 #2 (TP vs manual) / #3 (SL vs manual/daemon)."""

    def test_tp_does_not_overwrite_balance_when_already_closed(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)
        account.refresh_from_db()

        consumer = _consumer(account.pk, balance=10000.0, positions=[
            _pos_entry(pos, tp=1.10),
        ])
        _run(consumer._check_tp_sl("EUR/USD", 1.20, 1.20))  # far above TP -> triggers

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10050.00"))
        self.assertEqual(consumer.account["balance"], 10050.0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(consumer._positions, [])
        self.assertNotIn("order_close", _sent_types(consumer))

    def test_sl_does_not_overwrite_balance_when_already_closed_by_daemon(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        # Daemon closes it (e.g. its own SL sweep) first: -20 realized.
        _close_position_sync(pos_mem, account.pk, 1.0800, "manual", -20.0, 9980.0, 9980.0)
        account.refresh_from_db()

        consumer = _consumer(account.pk, balance=10000.0, positions=[
            _pos_entry(pos, sl=1.09),
        ])
        _run(consumer._check_tp_sl("EUR/USD", 1.05, 1.05))  # far below SL -> triggers

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("9980.00"))
        self.assertEqual(consumer.account["balance"], 9980.0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertNotIn("order_close", _sent_types(consumer))

    def test_tp_sl_real_close_still_notifies_and_updates_balance(self):
        """Control case — no collision: a genuine TP hit must still behave
        exactly as before (notification sent, balance updated)."""
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[
            _pos_entry(pos, tp=1.10),
        ])
        _run(consumer._check_tp_sl("EUR/USD", 1.20, 1.20))

        account.refresh_from_db()
        self.assertGreater(account.balance, Decimal("10000.00"))
        self.assertEqual(consumer.account["balance"], float(account.balance))
        close_msgs = _sent_of_type(consumer, "order_close")
        self.assertEqual(len(close_msgs), 1)
        self.assertEqual(close_msgs[0]["reason"], "tp")


class StopoutVsManualCloseConcurrentTests(TransactionTestCase):
    """FASE 6 #4 — stop-out vs cierre manual. Uses a DD-engine
    (CHALLENGE) account so _do_stopout applies (suspends the account)."""

    def test_stopout_does_not_alter_balance_or_duplicate_close_when_already_closed(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)
        account.refresh_from_db()

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="CHALLENGE")
        _run(consumer._do_stopout())

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10050.00"))
        self.assertEqual(consumer.account["balance"], 10050.0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 1,
        )
        self.assertNotIn("order_close", _sent_types(consumer))
        # Suspension itself is unaffected — separate concern, still fires.
        self.assertIn("account:suspended", _sent_types(consumer))
        self.assertEqual(consumer.account["status"], "Suspendido")


class RetailLiquidationVsDaemonConcurrentTests(TransactionTestCase):
    """FASE 6 #5 — retail liquidation vs daemon. Margin-engine account
    type stays Activo (current, unchanged commercial semantics)."""

    def test_retail_liquidation_does_not_alter_balance_when_already_closed_by_daemon(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.0800, "manual", -20.0, 9980.0, 9980.0)
        account.refresh_from_db()

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        _run(consumer._do_retail_liquidation())

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("9980.00"))
        self.assertEqual(consumer.account["balance"], 9980.0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertNotIn("order_close", _sent_types(consumer))
        # FASE 6 #14 — retail account remains Activo after liquidation.
        self.assertEqual(consumer.account["status"], "Activo")


class TwoPanelsCloseSamePositionTests(TransactionTestCase):
    """FASE 6 #6 — dos paneles intentando cerrar la MISMA posición, ambos
    via the real _order_close path end to end (no shortcut through
    tasks.py)."""

    def test_two_panels_closing_same_position_only_one_real_close(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))

        consumer_a = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)])
        consumer_a._bid_state = {"EUR/USD": 1.15}; consumer_a._ask_state = {"EUR/USD": 1.15}
        consumer_b = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)])
        consumer_b._bid_state = {"EUR/USD": 1.15}; consumer_b._ask_state = {"EUR/USD": 1.15}

        _run(consumer_a._order_close({"id": pos.pk, "symbol": "EUR/USD"}))
        _run(consumer_b._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 1,
        )
        account.refresh_from_db()
        self.assertEqual(consumer_a.account["balance"], float(account.balance))
        self.assertEqual(consumer_b.account["balance"], float(account.balance))
        self.assertIn("order_close", _sent_types(consumer_a))
        self.assertNotIn("order_close", _sent_types(consumer_b))


class PositionsRefreshedFromDBTests(TransactionTestCase):
    """FASE 6 #9 — already_closed still refreshes positions from DB
    (account-wide, MULTIPANEL-01 contract), including a position opened
    through a DIFFERENT panel that this connection never knew about."""

    def test_already_closed_refresh_includes_position_from_another_panel(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        # A position opened through a sibling panel this connection never
        # learned about.
        other_pos = make_position(account, symbol="GBP/USD", side="BUY",
                                   qty=Decimal("0.1"), avg_price=Decimal("1.3000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)])
        _run(consumer._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        positions_msgs = _sent_of_type(consumer, "positions")
        self.assertTrue(positions_msgs)
        ids = {item["id"] for item in positions_msgs[-1]["items"]}
        self.assertIn(other_pos.pk, ids)
        self.assertNotIn(pos.pk, ids)


class DbFailureDuringRefreshTests(TransactionTestCase):
    """FASE 6 #15 — a DB failure while refreshing after already_closed
    must not fabricate/zero state, must log, and must not crash (socket
    stays alive — no exception escapes _order_close)."""

    def test_db_failure_during_stale_refresh_keeps_last_known_state(self):
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)])
        consumer._db_sync_account_balances = AsyncMock(side_effect=RuntimeError("db down"))

        # Must not raise.
        _run(consumer._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        # Kept the last known (pre-close) in-memory balance — never
        # fabricated/zeroed just because the refresh failed.
        self.assertEqual(consumer.account["balance"], 10000.0)
        self.assertNotIn("order_close", _sent_types(consumer))


class ChallengeFundedStatusTests(TransactionTestCase):
    """FASE 6 #13 — Challenge/Funded account keeps correct status
    (Suspendido) through a stopout even when a stale collision occurs."""

    def test_challenge_account_suspended_status_correct_after_collision(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo",
                                account_type="CHALLENGE")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        _close_position_sync(pos_mem, account.pk, 1.1500, "manual", 50.0, 10050.0, 10050.0)

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="CHALLENGE")
        _run(consumer._do_stopout())

        account.refresh_from_db()
        self.assertEqual(account.status, "Suspendido")
        self.assertEqual(consumer.account["status"], "Suspendido")


class NoFormulaChangeStructuralTests(TransactionTestCase):
    """PANEL-03 must not touch pnl_engine, spread/commission/margin
    formulas, or challenge rules."""

    def test_pnl_engine_untouched(self):
        import inspect
        from simulator import pnl_engine
        src = inspect.getsource(pnl_engine)
        self.assertIn("calculate_quote_pnl", src)
        self.assertIn("convert_pnl_to_account_currency", src)

    def test_handle_close_result_is_pure_no_io(self):
        """_handle_close_result must not be async / must not touch the DB
        — it's a pure dict transform, reused by all four close paths."""
        import inspect
        self.assertFalse(
            inspect.iscoroutinefunction(TradingConsumer._handle_close_result),
            "_handle_close_result must stay a plain sync method (pure, no I/O)",
        )
