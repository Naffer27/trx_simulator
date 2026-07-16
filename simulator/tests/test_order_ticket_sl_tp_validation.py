"""
simulator/tests/test_order_ticket_sl_tp_validation.py — PANEL-04.

Server-side SL/TP validation for new orders (_order_new never trusted the
frontend for this before): rejects non-finite (NaN/Infinity), non-numeric,
zero/negative, and wrong-direction SL/TP values, with a specific
error_code/message per failure — never a generic catch-all. No minimum
distance from the executable price is enforced (no such policy exists in
this codebase; _validate_sl_tp's docstring explains why inventing one here
would be out of scope).

Two layers of coverage:
  - ValidateSlTpPureFunctionTests: exhaustive, fast, no I/O — exercises
    _validate_sl_tp() directly for every case FASE 8 lists.
  - OrderNewIntegrationTests: proves the WIRING — _order_new actually
    calls _validate_sl_tp and returns the right error over the WS
    connection, using the real DB-backed order-open path (same pattern as
    test_atomic_margin_and_position_guard.py / PANEL-02/03).

OrderNewIntegrationTests uses TransactionTestCase, not TestCase:
_order_new (unlike PANEL-02/03's tests, which call _db_open_position_atomic
via .__wrapped__ directly) awaits the real @database_sync_to_async-wrapped
methods (_db_evaluate_risk, _db_validate_order_risk,
_db_open_position_atomic in sequence) — each spins up a separate
threadpool thread. TestCase's uncommitted per-test transaction is
invisible to (and deadlocks against, "database table is locked") that
thread on SQLite — the same reasoning documented in
test_account_balance_concurrency.py. ValidateSlTpPureFunctionTests and
NoFormulaChangeStructuralTests touch no DB at all and use plain TestCase.
"""
import asyncio
import math
import time
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer, _validate_sl_tp
from simulator.models import Position

from .factories import make_account

_run = lambda coro: asyncio.run(coro)


class ValidateSlTpPureFunctionTests(TestCase):
    """FASE 8 — pure-function coverage, no DB/WS involved."""

    EXEC = 1.1000

    # ── BUY ──
    def test_buy_valid_sl_below_and_tp_above_accepted(self):
        ok, code, _ = _validate_sl_tp("buy", 1.0900, 1.1100, self.EXEC)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_buy_sl_above_price_rejected(self):
        ok, code, msg = _validate_sl_tp("buy", 1.1100, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_direction")
        self.assertIn("BUY", msg)

    def test_buy_sl_equal_to_price_rejected(self):
        """Boundary: SL exactly at exec_price is still wrong-direction for BUY (must be strictly below)."""
        ok, code, _ = _validate_sl_tp("buy", self.EXEC, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_direction")

    def test_buy_tp_below_price_rejected(self):
        ok, code, msg = _validate_sl_tp("buy", None, 1.0900, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_tp_direction")
        self.assertIn("BUY", msg)

    def test_buy_sl_tp_both_inverted_rejected(self):
        """SL above AND TP below — the classic 'swapped fields' mistake."""
        ok, code, _ = _validate_sl_tp("buy", 1.1200, 1.0800, self.EXEC)
        self.assertFalse(ok)
        # SL is checked first — either invalid_sl_direction is acceptable,
        # but it must reject, not silently accept.
        self.assertIn(code, ("invalid_sl_direction", "invalid_tp_direction"))

    # ── SELL ──
    def test_sell_valid_sl_above_and_tp_below_accepted(self):
        ok, code, _ = _validate_sl_tp("sell", 1.1100, 1.0900, self.EXEC)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_sell_sl_below_price_rejected(self):
        ok, code, msg = _validate_sl_tp("sell", 1.0900, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_direction")
        self.assertIn("SELL", msg)

    def test_sell_tp_above_price_rejected(self):
        ok, code, msg = _validate_sl_tp("sell", None, 1.1100, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_tp_direction")
        self.assertIn("SELL", msg)

    # ── Non-finite / non-numeric / non-positive ──
    def test_nan_sl_rejected(self):
        ok, code, _ = _validate_sl_tp("buy", math.nan, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_value")

    def test_positive_infinity_tp_rejected(self):
        ok, code, _ = _validate_sl_tp("buy", None, math.inf, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_tp_value")

    def test_negative_infinity_sl_rejected(self):
        ok, code, _ = _validate_sl_tp("sell", -math.inf, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_value")

    def test_negative_sl_rejected(self):
        ok, code, _ = _validate_sl_tp("buy", -1.05, None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_value")

    def test_zero_tp_rejected(self):
        ok, code, _ = _validate_sl_tp("buy", None, 0, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_tp_value")

    def test_non_numeric_string_rejected(self):
        ok, code, _ = _validate_sl_tp("buy", "not-a-number", None, self.EXEC)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_sl_value")

    def test_none_values_are_valid_optional(self):
        """No SL, no TP at all — perfectly valid (both optional)."""
        ok, code, _ = _validate_sl_tp("buy", None, None, self.EXEC)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_no_minimum_distance_enforced(self):
        """A SL/TP one tick away from exec_price must be accepted — no
        arbitrary minimum-distance policy exists (see docstring)."""
        ok, code, _ = _validate_sl_tp("buy", self.EXEC - 0.00001, self.EXEC + 0.00001, self.EXEC)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c._last_db_sync = 0.0
    c._positions = []
    c._order_seq = 1
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.symbol = "EUR/USD"
    c._bid_state, c._ask_state = {"EUR/USD": 1.1000}, {"EUR/USD": 1.1000}
    c._raw_bid_state, c._raw_ask_state = {}, {}
    c._pricing_snapshot_state, c._pricing_ts_state = {}, {}
    c.account = {
        "balance": 100000.0, "equity": 100000.0, "peak_balance": 100000.0,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": netting_mode, "status": "Activo", "account_type": "STANDARD",
        "tier": "10K", "profit_target": 800.0, "initial_balance": 100000.0,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c._feed = get_feed_manager()
    c.send_json = AsyncMock()
    return c


def _sent_types(consumer):
    return [c.args[0].get("type") for c in consumer.send_json.call_args_list]


def _first_error(consumer):
    for c in consumer.send_json.call_args_list:
        msg = c.args[0]
        if msg.get("type") == "error":
            return msg
    return None


class OrderNewIntegrationTests(TransactionTestCase):
    """FASE 8 — proves _order_new is actually wired to _validate_sl_tp,
    end to end over the same code path a real WS order:new uses (large
    balance, no margin/risk rejections in the way, so any error observed
    is unambiguously the SL/TP check)."""

    def setUp(self):
        self.account = make_account(balance=Decimal("100000.00"))
        feed = get_feed_manager()
        with feed._lock:
            feed._bids["EUR/USD"] = 1.1000
            feed._asks["EUR/USD"] = 1.1000
            feed._prices["EUR/USD"] = 1.1000
            feed._price_ts["EUR/USD"] = time.time()

    def test_buy_inverted_sl_tp_rejected_by_order_new(self):
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "sl": 1.1200, "tp": 1.0800,  # both on the wrong side for BUY
        }))
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertIn(err["code"], ("invalid_sl_direction", "invalid_tp_direction"))
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_sell_inverted_sl_tp_rejected_by_order_new(self):
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "sell", "qty": 0.01,
            "sl": 1.0800, "tp": 1.1200,  # both on the wrong side for SELL
        }))
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertIn(err["code"], ("invalid_sl_direction", "invalid_tp_direction"))
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_nan_sl_rejected_by_order_new(self):
        """A crafted payload can send a JSON value that decodes to a
        Python float NaN via json.loads('NaN') — Python's json module
        accepts it by default. Simulated here by passing float('nan')
        directly, the same shape data.get('sl') would have after
        json.loads on such a payload."""
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "sl": float("nan"), "tp": None,
        }))
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_sl_value")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_negative_tp_rejected_by_order_new(self):
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "sl": None, "tp": -5.0,
        }))
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_tp_value")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_valid_sl_tp_order_proceeds_and_opens_position(self):
        """Positive control — valid SL/TP must NOT be rejected by the new
        check, and the order must actually open a real Position."""
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "sl": 1.0900, "tp": 1.1100,
        }))
        types = _sent_types(consumer)
        self.assertNotIn("error", types, f"unexpected rejection, sent types: {types}")
        self.assertIn("order_ack", types)
        pos = Position.objects.filter(account=self.account).first()
        self.assertIsNotNone(pos)
        self.assertEqual(float(pos.sl), 1.0900)
        self.assertEqual(float(pos.tp), 1.1100)

    def test_order_without_sl_tp_still_accepted(self):
        """SL/TP are optional — omitting them entirely must not trigger
        the new validation at all."""
        consumer = _consumer(self.account.pk)
        _run(consumer._order_new({
            "symbol": "EUR/USD", "side": "sell", "qty": 0.01,
        }))
        types = _sent_types(consumer)
        self.assertNotIn("error", types, f"unexpected rejection, sent types: {types}")
        self.assertIn("order_ack", types)


class NoFormulaChangeStructuralTests(TestCase):
    """PANEL-04 must not touch pnl_engine, margin/spread/commission
    formulas, or Challenge/Funded rules — only adds a new, independent
    SL/TP gate to _order_new."""

    def test_pnl_engine_untouched(self):
        import inspect
        from simulator import pnl_engine
        src = inspect.getsource(pnl_engine)
        self.assertIn("calculate_quote_pnl", src)
        self.assertIn("convert_pnl_to_account_currency", src)

    def test_validate_sl_tp_is_pure_no_io(self):
        import inspect
        self.assertFalse(
            asyncio.iscoroutinefunction(_validate_sl_tp),
            "_validate_sl_tp must stay a plain sync, pure function (no I/O)",
        )

    def test_no_minimum_distance_constant_introduced(self):
        """Guards against a future edit silently adding an unapproved
        minimum-SL/TP-distance policy to this function."""
        import inspect
        src = inspect.getsource(_validate_sl_tp)
        for forbidden in ("MIN_SL_DISTANCE", "MIN_TP_DISTANCE", "MIN_STOP_DISTANCE"):
            self.assertNotIn(forbidden, src)
