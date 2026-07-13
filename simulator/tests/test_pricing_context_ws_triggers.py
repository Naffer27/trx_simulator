"""
simulator/tests/test_pricing_context_ws_triggers.py — SPREAD-02.

Covers the WS-consumer call sites that build a pricing context and forward
it into the (separately, thoroughly tested — see
test_pricing_context_persistence.py) DB-atomic methods: _check_tp_sl,
_do_stopout, _do_retail_liquidation, and the shared _capture_pricing_context
helper all five open/close triggers use.

Strategy: a bare TradingConsumer instance (bypassing Channels' connect())
with only the attributes these methods actually touch, and
_db_close_position_atomic replaced with an AsyncMock so this test verifies
"did the caller correctly assemble and forward pricing_context_close",
without re-testing DB persistence (already covered elsewhere) or Channels
networking.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase

from simulator.consumers import TradingConsumer
from simulator import pricing_context as pc


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer(**overrides) -> TradingConsumer:
    """A TradingConsumer instance with only the state these methods read —
    never goes through connect()/Channels scope."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = "EUR/USD"
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {
        "balance": 10000.0, "equity": 10000.0, "spread_pips": 0.0,
        "status": "Activo", "peak_balance": 10000.0, "leverage": 50,
        "netting_mode": False, "account_type": "CHALLENGE",
        "profit_target": 800.0, "initial_balance": 10000.0,
        "stopout_level": 50.0,
    }
    for key, value in overrides.items():
        setattr(c, key, value)
    c.send_json = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    c._db_suspend_account = AsyncMock()
    return c


_CLOSE_RESULT = {
    "new_balance": 10100.0, "new_equity": 10100.0, "new_status": "Activo",
    "new_peak": 10000.0, "violations": [], "trade_id": 1,
}


class CaptureHelperTests(TestCase):
    """_capture_pricing_context is the single method all 5 WS trigger sites
    call — testing it directly covers their shared assembly logic."""

    def test_captures_raw_and_executable_from_state(self):
        c = _bare_consumer()
        c._raw_bid_state["EUR/USD"] = 1.0999
        c._raw_ask_state["EUR/USD"] = 1.1001
        c._bid_state["EUR/USD"] = 1.0997
        c._ask_state["EUR/USD"] = 1.1003
        c._pricing_ts_state["EUR/USD"] = 1700000000

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_TP)

        self.assertEqual(ctx["raw_bid"], 1.0999)
        self.assertEqual(ctx["raw_ask"], 1.1001)
        self.assertEqual(ctx["executable_bid"], 1.0997)
        self.assertEqual(ctx["executable_ask"], 1.1003)
        self.assertEqual(ctx["pricing_timestamp"], 1700000000.0)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_WS_TP)

    def test_missing_tick_state_degrades_to_none_prices_not_an_exception(self):
        c = _bare_consumer()  # no ticks ever recorded for this symbol
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertIsNone(ctx["raw_bid"])
        self.assertIsNone(ctx["executable_bid"])

    def test_reads_markup_from_the_frozen_tick_snapshot_not_live_account(self):
        """SPREAD-02b: _capture_pricing_context reads self._pricing_snapshot_state
        (frozen at the last price_tick()) — not a live re-read of self.account
        or BrokerSpreadConfig. See test_pricing_context_forensic_invariants.py
        for the full invariant this enforces."""
        c = _bare_consumer()
        c._pricing_snapshot_state["EUR/USD"] = {
            "base_spread_pips": None, "account_markup_pips": 1.25,
            "provider_id": None, "source_state": None,
        }
        # Live account value differs from the frozen snapshot — the frozen
        # snapshot must win.
        c.account["spread_pips"] = 9.99
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["account_markup_pips"], 1.25)

    def test_missing_snapshot_degrades_to_none_not_a_live_reread(self):
        """No tick was ever seen for this symbol — base/markup/provider stay
        None; _capture_pricing_context must not fall back to a fresh read."""
        c = _bare_consumer()  # _pricing_snapshot_state has no entry
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertIsNone(ctx["base_spread_pips"])
        self.assertIsNone(ctx["account_markup_pips"])
        self.assertIsNone(ctx["provider_id"])

    def test_never_raises_on_a_malformed_snapshot(self):
        c = _bare_consumer()
        c._pricing_snapshot_state["EUR/USD"] = "not-a-dict"  # corrupted state
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_CAPTURE_FAILED)


class CheckTpSlTests(TestCase):
    def test_tp_hit_forwards_pricing_context_close_with_tp_profile(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy",
                          "qty": 1.0, "avg": 1.10000, "sl": None, "tp": 1.10500}]
        c._raw_bid_state["EUR/USD"] = 1.1049
        c._raw_ask_state["EUR/USD"] = 1.1051
        c._bid_state["EUR/USD"] = 1.10500
        c._ask_state["EUR/USD"] = 1.10520
        c._db_close_position_atomic = AsyncMock(return_value=_CLOSE_RESULT)

        _run(c._check_tp_sl("EUR/USD", bid=1.10500, ask=1.10520))

        c._db_close_position_atomic.assert_awaited_once()
        _, kwargs = c._db_close_position_atomic.call_args
        self.assertEqual(kwargs["pricing_context_close"]["pricing_profile"], pc.PROFILE_WS_TP)
        self.assertEqual(kwargs["pricing_context_close"]["raw_bid"], 1.1049)

    def test_sl_hit_forwards_sl_profile(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy",
                          "qty": 1.0, "avg": 1.10000, "sl": 1.09500, "tp": None}]
        c._db_close_position_atomic = AsyncMock(return_value=_CLOSE_RESULT)

        _run(c._check_tp_sl("EUR/USD", bid=1.09500, ask=1.09520))

        _, kwargs = c._db_close_position_atomic.call_args
        self.assertEqual(kwargs["pricing_context_close"]["pricing_profile"], pc.PROFILE_WS_SL)

    def test_no_trigger_never_calls_close(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy",
                          "qty": 1.0, "avg": 1.10000, "sl": 1.05000, "tp": 1.20000}]
        c._db_close_position_atomic = AsyncMock(return_value=_CLOSE_RESULT)

        _run(c._check_tp_sl("EUR/USD", bid=1.10500, ask=1.10520))

        c._db_close_position_atomic.assert_not_awaited()


class DoStopoutTests(TestCase):
    def test_stopout_forwards_pricing_context_close(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy",
                          "qty": 1.0, "avg": 1.10000, "sl": None, "tp": None}]
        c._bid_state["EUR/USD"] = 1.05000
        c._ask_state["EUR/USD"] = 1.05020
        c._raw_bid_state["EUR/USD"] = 1.04998
        c._raw_ask_state["EUR/USD"] = 1.05022
        c._db_close_position_atomic = AsyncMock(return_value=_CLOSE_RESULT)

        _run(c._do_stopout())

        c._db_close_position_atomic.assert_awaited_once()
        _, kwargs = c._db_close_position_atomic.call_args
        self.assertEqual(kwargs["pricing_context_close"]["pricing_profile"], pc.PROFILE_WS_STOPOUT)
        self.assertEqual(kwargs["pricing_context_close"]["raw_bid"], 1.04998)


class DoRetailLiquidationTests(TestCase):
    def test_margin_call_forwards_pricing_context_close(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "sell",
                          "qty": 1.0, "avg": 1.10000, "sl": None, "tp": None}]
        c._bid_state["EUR/USD"] = 1.15000
        c._ask_state["EUR/USD"] = 1.15020
        c._db_close_position_atomic = AsyncMock(return_value=_CLOSE_RESULT)

        _run(c._do_retail_liquidation())

        _, kwargs = c._db_close_position_atomic.call_args
        self.assertEqual(kwargs["pricing_context_close"]["pricing_profile"], pc.PROFILE_WS_MARGIN_CALL)


class OrderNewCapturesOpenContextTests(TestCase):
    """_order_new: pricing_context is captured before the atomic call — this
    verifies the profile/shape it builds without driving the full risk/margin
    gate chain (covered by test_pretrade_margin_guard.py and friends)."""

    def test_capture_uses_open_profile(self):
        c = _bare_consumer()
        c._bid_state["EUR/USD"] = 1.10030
        c._ask_state["EUR/USD"] = 1.10010
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["executable_ask"], 1.10010)
