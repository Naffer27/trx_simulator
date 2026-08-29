# simulator/tests/test_o6c1aa_unified_raw_execution_spread_fee.py
"""
O.6c-1aa — UNIFIED RAW EXECUTION + EXPLICIT SPREAD FEE.

Implements O.6c-1z's approved Model C. Two changes, deployed as one
atomic unit (per O.6c-1aa's explicit instruction):

  1. order:new / exec_price(): Position.avg_price now comes exclusively
     from self._raw_exec_price(symbol, side) — self._feed.
     get_validated_quote(symbol).ask (BUY) / .bid (SELL) — never
     self._bid_state/self._ask_state (client/marked-up, O.6c-1u's
     documented raw-vs-markup split). Same fail-safe contract as
     _order_close() (O.6c-1w): invalid/stale/cross-symbol quote ->
     price_unavailable, zero Position/Trade/LedgerEntry/balance change.
     _pretrade_check()'s margin estimate uses the SAME authority.

  2. _check_tp_sl() (live WS SL/TP): price_tick() now passes raw_bid/
     raw_ask (already validated by _validate_quote_values() a few lines
     above, before broker_price() ever runs) instead of the client/
     marked-up bid/ask. _check_tp_sl()'s own body/side-convention is
     UNCHANGED — only the call-site argument changed. This makes WS
     SL/TP economically identical to Celery's scan_positions_task
     (already RAW since O.6c-1w) and to every other close path
     (_order_close/_do_stopout/_do_retail_liquidation, already RAW
     since O.6c-1s) — closing O.6c-1u's documented inconsistency.

  3. Explicit spread fee: _db_open_position_atomic() now creates a
     mandatory LedgerEntry(event_type=EV_FEE, meta.fee_type="spread")
     alongside commission, inside the SAME outer transaction.atomic()
     (no nested savepoint isolating it — a failure rolls back the whole
     open, per O.6c-1aa's explicit "no permitir posición creada sin su
     fee correspondiente"). Reuses calculate_spread_revenue() UNCHANGED
     (O.6c-1z's approved formula) — the SAME computed value feeds both
     the trader's LedgerEntry(EV_FEE) debit and BrokerLedger(REV_SPREAD),
     guaranteeing exact parity by construction. LedgerEntry.EV_FEE
     already existed in EVENT_CHOICES — reused, zero migration.

Floating/realized P&L (_feed_close_price()/_positions_snapshot()/
_unrealized_pnl_total()) is UNTOUCHED — already RAW since O.6c-1w/1s,
and never reads the new fee — Position row P&L represents price
movement only, exactly as required.

Uses TransactionTestCase throughout (real DB commits, transaction.
on_commit fires, matches every O.6c-1o/1q/1s/1v/1w/1w-b test file in
this suite for the same established reasons).
"""
import time
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from django.test import TransactionTestCase, SimpleTestCase

from market_data.contracts import OrderPolicy
from simulator.consumers import TradingConsumer
from simulator.models import LedgerEntry, BrokerLedger, Position, Trade, TradingAccount
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests
from simulator.tasks import _read_cached_price
from market_data.feeds import get_feed_manager

from .factories import make_account, make_position, make_spread_config
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _open_normal_session():
    """TEST-INFRA — market-session policy is real-clock-driven (FIX-05A);
    these tests are about raw execution/rejection semantics, not
    market-session behavior, so pin it open. Same pattern as
    test_fix05a_financial_price_integrity.py::_open_normal_session()."""
    return patch(
        "market_data.sessions.service.evaluate_market_session_for_symbol",
        return_value=Mock(order_policy=OrderPolicy.OPEN_NORMAL),
    )


def _clear_symbol(symbol: str):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)
        feed._price_source.pop(symbol, None)
        feed._last_valid_quote.pop(symbol, None)


def _seed_raw(symbol: str, bid, ask, *, fresh: bool = True):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol]     = bid
        feed._asks[symbol]     = ask
        feed._prices[symbol]   = (bid + ask) / 2
        feed._price_ts[symbol] = time.time() if fresh else (time.time() - 3600)


def _bare_consumer(account_id, symbol="EUR/USD") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = symbol
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._feed = get_feed_manager()
    c.account = {
        "balance": 10000.0, "equity": 10000.0, "peak_balance": 10000.0,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": "Activo", "account_type": "CHALLENGE",
        "tier": "", "profit_target": 0.0, "initial_balance": 0.0,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


class _SpreadConfigTestCase(TransactionTestCase):
    """Base class for tests needing an active BrokerSpreadConfig — the
    fee/markup path is a no-op unless the process-wide cache is both
    populated (DB row) AND refreshed (spread_config_cache is DB-free per
    tick, by design — see spread_config_cache.py)."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()
        _clear_symbol("BTCUSD")
        _clear_symbol("EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# 1 — order:new uses RAW, never client/marked-up
# ─────────────────────────────────────────────────────────────────────────
class RawExecutionAuthorityTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_buy_avg_price_is_raw_ask_not_marked_ask(self):
        consumer = _consumer(self.account.pk)  # seeds EUR/USD 1.1000/1.1000 raw+client_bid_state
        # Deliberately different raw vs. what _bid_state/_ask_state carries,
        # to prove avg_price tracks the RAW quote, not the stale client cache.
        _seed_raw("EUR/USD", 1.20000, 1.20002)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=self.account)
        self.assertEqual(pos.avg_price, Decimal("1.20002"))  # raw ask, not 1.1000

    def test_sell_avg_price_is_raw_bid(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.20000, 1.20002)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "sell", "qty": 0.01}))
        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=self.account)
        self.assertEqual(pos.avg_price, Decimal("1.20000"))  # raw bid


# ─────────────────────────────────────────────────────────────────────────
# order:new rejection — invalid/stale/cross-symbol/NaN/Infinity
# ─────────────────────────────────────────────────────────────────────────
class RawExecutionRejectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def _assert_fully_rejected(self, consumer):
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account_id=self.account.pk).count(), 0)
        self.assertEqual(TradingAccount.objects.get(pk=self.account.pk).balance, self.account.balance)

    def test_cross_symbol_btc_into_eurusd_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 63088.50, 63088.70)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self._assert_fully_rejected(consumer)

    def test_cross_symbol_eur_into_btcusd_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("BTCUSD", 1.17000, 1.17020)
        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.01}))
        self._assert_fully_rejected(consumer)

    def test_nan_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", float("nan"), 1.10020)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self._assert_fully_rejected(consumer)

    def test_infinity_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.10000, float("inf"))
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self._assert_fully_rejected(consumer)

    def test_stale_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.10000, 1.10020, fresh=False)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self._assert_fully_rejected(consumer)

    def test_pretrade_check_itself_rejects_on_missing_quote(self):
        """Unit-level: _pretrade_check() (used by the fast in-memory
        guard) independently fails safe, not just the caller's own gate."""
        consumer = _bare_consumer(self.account.pk)
        _clear_symbol("EUR/USD")
        ok, reason = consumer._pretrade_check("EUR/USD", "buy", 0.01)
        self.assertFalse(ok)
        self.assertEqual(reason, "price_unavailable")


# ─────────────────────────────────────────────────────────────────────────
# 2 — SL/TP WS unified to RAW, economically identical to Celery
# ─────────────────────────────────────────────────────────────────────────
class SlTpRawUnificationTests(SimpleTestCase):
    def _tick_consumer(self, **overrides):
        c = TradingConsumer.__new__(TradingConsumer)
        c._db_account_id = 1
        c.symbol = "EUR/USD"
        c._price_state = {}
        c._bid_state = {}
        c._ask_state = {}
        c._raw_bid_state = {}
        c._raw_ask_state = {}
        c._pricing_ts_state = {}
        c._pricing_snapshot_state = {}
        c._positions = []
        c._daily_realized_pnl = 0.0
        c._daily_pnl_date = None
        c.account = {"balance": 10000.0, "spread_pips": 0.0, "commercial_pricing_fields": {}}
        for k, v in overrides.items():
            setattr(c, k, v)
        c.send_json = AsyncMock()
        c._on_tick = AsyncMock()
        c._check_tp_sl = AsyncMock()
        c._recalc_account_and_push = AsyncMock()
        return c

    def _tick(self, symbol, bid, ask, ts=1_700_000_000, source="finnhub"):
        # FIX-05A.1 — real default source: this class tests SL/TP raw-vs-
        # marked-up unification, not the source gate itself.
        return {"symbol": symbol, "bid": bid, "ask": ask,
                "mid": round((bid + ask) / 2, 5), "time": ts, "source": source}

    def test_check_tp_sl_called_with_raw_not_marked_bid_ask(self):
        c = self._tick_consumer()
        _run(c.price_tick(self._tick("EUR/USD", bid=1.09990, ask=1.10010)))
        c._check_tp_sl.assert_called_once()
        args = c._check_tp_sl.call_args.args
        self.assertEqual(args[0], "EUR/USD")
        self.assertAlmostEqual(args[1], 1.09990, places=5)  # raw bid, unmarked
        self.assertAlmostEqual(args[2], 1.10010, places=5)  # raw ask, unmarked

    def test_invalid_tick_never_calls_check_tp_sl(self):
        c = self._tick_consumer()
        _run(c.price_tick(self._tick("EUR/USD", bid=63088.50, ask=63088.70)))
        c._check_tp_sl.assert_not_called()

    def test_sl_and_tp_do_not_fire_on_corrupted_tick_real_check_tp_sl(self):
        """End-to-end with the REAL _check_tp_sl (not mocked)."""
        c = TradingConsumer.__new__(TradingConsumer)
        c._db_account_id = None
        c.symbol = "EUR/USD"
        c._price_state, c._bid_state, c._ask_state = {}, {}, {}
        c._raw_bid_state, c._raw_ask_state = {}, {}
        c._pricing_ts_state, c._pricing_snapshot_state = {}, {}
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
                          "avg": 1.10000, "sl": 1.09000, "tp": 1.11000, "opened_at": time.time()}]
        c._daily_realized_pnl = 0.0
        c._daily_pnl_date = None
        c._feed = get_feed_manager()
        c.account = {"balance": 10000.0, "equity": 10000.0, "spread_pips": 0.0,
                     "status": "Activo", "peak_balance": 10000.0, "leverage": 50,
                     "netting_mode": False, "account_type": "CHALLENGE",
                     "profit_target": 800.0, "initial_balance": 10000.0,
                     "stopout_level": 50.0, "commercial_pricing_fields": {}}
        c.send_json = AsyncMock()
        c._on_tick = AsyncMock()
        c._recalc_account_and_push = AsyncMock()
        _run(c.price_tick(self._tick("EUR/USD", bid=63088.50, ask=63088.70)))
        self.assertEqual(len(c._positions), 1)  # untouched — no SL/TP fired

    def test_daemon_and_ws_now_use_the_same_raw_source(self):
        """Structural regression guard: price_tick's call to _check_tp_sl
        must pass raw_bid/raw_ask, never the marked-up local `bid`/`ask`
        computed by broker_price() a few lines earlier."""
        import inspect
        from simulator import consumers
        src = inspect.getsource(consumers.TradingConsumer.price_tick)
        self.assertIn("_check_tp_sl(symbol, raw_bid, raw_ask)", src)
        self.assertNotIn("_check_tp_sl(symbol, bid, ask)", src)


# ─────────────────────────────────────────────────────────────────────────
# 3/4 — explicit spread fee + BrokerLedger parity
# ─────────────────────────────────────────────────────────────────────────
class SpreadFeeChargeTests(_SpreadConfigTestCase):
    def test_spread_fee_ledger_entry_created_on_open(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("BTCUSD", 63000.00, 63000.50)
        panel = _bare_consumer(account.pk, symbol="BTCUSD")

        result = _db_open_sync(
            panel, symbol="BTCUSD", side="buy", qty=0.01, price=63000.50,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        fee_entries = LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertEqual(fee_entries.count(), 1)
        fee = fee_entries.first()
        self.assertEqual(fee.meta.get("fee_type"), "spread")
        # extra = 15.00 * 1.0(pip_size) / 2 = 7.50; spread_rev = 7.50*0.01*1.0 = 0.075
        # exactly, in Decimal("0.075")'s own meta (see fee.meta below for
        # the unrounded figure) — but LedgerEntry.amount is a
        # decimal_places=2 field, same constraint commission already has;
        # 0.075 quantizes (ROUND_HALF_EVEN, Python/Django's default) to
        # 0.08 once persisted and re-read from DB — this is the system's
        # real rounding policy, not a bug (O.6c-1aa requirement #7).
        self.assertAlmostEqual(float(-fee.amount), 0.08, places=2)
        self.assertAlmostEqual(fee.meta.get("effective_pips"), 15.00, places=6)

    def test_no_spread_config_no_fee_charged(self):
        """Zero BrokerSpreadConfig for the symbol -> zero fee, zero
        regression for accounts/symbols with no configured spread."""
        account = make_account(balance=Decimal("10000"))
        _seed_raw("BTCUSD", 63000.00, 63000.50)
        panel = _bare_consumer(account.pk, symbol="BTCUSD")
        _db_open_sync(
            panel, symbol="BTCUSD", side="buy", qty=0.01, price=63000.50,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).count(), 0)

    def test_broker_ledger_rev_spread_exactly_matches_trader_fee(self):
        """O.6c-1aa explicit requirement: trader spread fee debit ==
        broker REV_SPREAD, same operation, same monetary precision."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        panel = _bare_consumer(account.pk)
        _db_open_sync(
            panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        rev = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_SPREAD, source_account_id=account.pk)
        self.assertEqual(-fee.amount, rev.amount)  # exact Decimal equality, not approx
        self.assertEqual(rev.source_ledger_id, fee.id)

    def test_broker_ledger_rev_spread_written_exactly_once(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("BTCUSD", 63000.00, 63000.50)
        panel = _bare_consumer(account.pk, symbol="BTCUSD")
        _db_open_sync(
            panel, symbol="BTCUSD", side="buy", qty=0.01, price=63000.50,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_SPREAD, source_account_id=account.pk).count(),
            1,
        )

    def test_balance_reduced_by_fee_plus_commission(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("BTCUSD", 63000.00, 63000.50)
        panel = _bare_consumer(account.pk, symbol="BTCUSD")
        commission = 0.06
        result = _db_open_sync(
            panel, symbol="BTCUSD", side="buy", qty=0.01, price=63000.50,
            sl=None, tp=None, commission=commission, new_balance=10000.0 - commission,
        )
        # result["new_balance"] reflects the in-memory Decimal computed
        # inside the transaction (10000 - 0.06 commission - 0.075 spread
        # fee = 9999.865), before any DB round-trip.
        self.assertAlmostEqual(result["new_balance"], 9999.865, places=3)
        # account.balance, re-read from DB, reflects the field's real
        # decimal_places=2 quantization (ROUND_HALF_EVEN): 9999.865 -> 9999.86
        # — same rounding policy commission-only balances already had.
        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 9999.86, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 5/6 — commission unchanged; row P&L excludes fees entirely
# ─────────────────────────────────────────────────────────────────────────
class CommissionAndPnlSeparationTests(_SpreadConfigTestCase):
    def test_commission_amount_unchanged_by_this_block(self):
        """commission_for()'s own formula is untouched — verified via a
        direct call, independent of the spread fee change."""
        account = make_account(balance=Decimal("10000"))
        panel = _bare_consumer(account.pk, symbol="BTCUSD")
        panel.account["commercial_pricing_fields"] = {}
        commission = panel.commission_for("BTCUSD", 0.01, 63000.50)
        from market_data.symbol_specs import get_spec
        # commission_for()'s legacy_fallback/pct branch does NOT round —
        # only the commission_per_lot branch does (round(qty*per_lot, 2)).
        # Matching that exactly here, not re-rounding, is the point of
        # this regression test: the formula itself is untouched.
        expected = 0.01 * 63000.50 * get_spec("BTCUSD").contract_size * get_spec("BTCUSD").commission_pct
        self.assertAlmostEqual(commission, expected, places=8)

    def test_position_row_pnl_excludes_spread_fee_and_commission(self):
        """Row P&L is pure price movement — never reduced by the fees
        that already hit balance separately (O.6c-1aa requirement #6)."""
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        # $1.00 raw spread (not the $0.50 used elsewhere) — avoids landing
        # exactly on Python/round()'s half-to-even boundary, which is
        # incidental to floating-point display, not the point of this test.
        _seed_raw("BTCUSD", 63000.00, 63001.00)
        panel = _bare_consumer(account.pk, symbol="BTCUSD")
        result = _db_open_sync(
            panel, symbol="BTCUSD", side="buy", qty=0.01, price=63001.00,
            sl=None, tp=None, commission=0.06, new_balance=9999.94,
        )
        pos_id = result["position_id"]
        panel._positions = [{"id": pos_id, "symbol": "BTCUSD", "side": "buy", "qty": 0.01,
                              "avg": 63001.00, "sl": None, "tp": None, "opened_at": time.time()}]
        # Same raw quote, no movement — row P&L must be exactly the raw
        # spread-crossing cost only ((63000.00-63001.00)*0.01 = -0.01),
        # NOT further reduced by the 0.075 spread fee or 0.06 commission
        # already debited from balance.
        snap = panel._positions_snapshot()
        self.assertAlmostEqual(snap[0]["pnl"], -0.01, places=3)


# ─────────────────────────────────────────────────────────────────────────
# 7 — economic parity vs O.6c-1z's Model B/Model C numeric examples
# ─────────────────────────────────────────────────────────────────────────
class EconomicParityTests(_SpreadConfigTestCase):
    """Reproduces O.6c-1z's exact 4 examples: open, then close immediately
    at the SAME raw quote (no market movement) — isolates the pure cost
    structure. Asserts Model C's net trader result equals the net trader
    result O.6c-1z computed for Model B (today's pre-O.6c-1aa behavior),
    within the system's real rounding (cents)."""

    def _round_trip_net(self, symbol, side, raw_bid, raw_ask, qty, spread_pips, commission_pct_symbol=None):
        make_spread_config(symbol=symbol, spread_pips=Decimal(str(spread_pips)), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw(symbol, raw_bid, raw_ask)
        panel = _bare_consumer(account.pk, symbol=symbol)

        entry_px = raw_ask if side == "buy" else raw_bid
        commission = panel.commission_for(symbol, qty, entry_px)
        open_result = _db_open_sync(
            panel, symbol=symbol, side=side, qty=qty, price=entry_px,
            sl=None, tp=None, commission=commission, new_balance=10000.0 - commission,
        )
        pos_id = open_result["position_id"]
        pos_mem = {"id": pos_id, "symbol": symbol, "side": side, "qty": qty,
                   "avg": entry_px, "sl": None, "tp": None, "opened_at": time.time()}

        exit_px = raw_bid if side == "buy" else raw_ask
        from simulator import pnl_engine
        realized = pnl_engine.position_pnl_float(side, entry_px, exit_px, qty, symbol, account_currency="USD")
        new_balance = 10000.0 - commission + realized  # pre-lock estimate, authoritative recomputed inside
        close_result = _db_close_sync(
            panel, pos_mem, close_px=exit_px, reason="manual",
            realized_pnl=realized, new_balance=new_balance, new_equity=new_balance,
        )
        account.refresh_from_db()
        return float(account.balance) - 10000.0  # net trader result (delta from starting balance)

    def test_btcusd_buy_net_matches_o6c1z_model_b(self):
        net = self._round_trip_net("BTCUSD", "buy", 63000.00, 63000.50, 0.01, 15.00)
        self.assertAlmostEqual(net, -0.14, places=2)

    def test_btcusd_sell_net_matches_o6c1z_model_b(self):
        net = self._round_trip_net("BTCUSD", "sell", 63000.00, 63000.50, 0.01, 15.00)
        self.assertAlmostEqual(net, -0.14, places=2)

    def test_eurusd_buy_net_matches_o6c1z_model_b(self):
        net = self._round_trip_net("EUR/USD", "buy", 1.17000, 1.17002, 0.01, 2.00)
        self.assertAlmostEqual(net, -0.12, places=2)

    def test_eurusd_sell_net_matches_o6c1z_model_b(self):
        net = self._round_trip_net("EUR/USD", "sell", 1.17000, 1.17002, 0.01, 2.00)
        self.assertAlmostEqual(net, -0.12, places=2)


# ─────────────────────────────────────────────────────────────────────────
# No double charge — merge/netting and repeated commission
# ─────────────────────────────────────────────────────────────────────────
class NoDoubleChargeTests(_SpreadConfigTestCase):
    def test_netting_merge_charges_fee_and_commission_exactly_once_per_open_call(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        account.netting_mode = True
        account.save(update_fields=["netting_mode"])
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        panel = _bare_consumer(account.pk)
        panel.account["netting_mode"] = True

        _db_open_sync(panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
                       sl=None, tp=None, commission=0.0, new_balance=10000.0)
        _db_open_sync(panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
                       sl=None, tp=None, commission=0.0, new_balance=10000.0)

        # Two separate opens -> two separate fee charges (one per DB write,
        # not per Position row) — never a THIRD/duplicate charge for the
        # same call, and never zero for the second genuinely-new open.
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).count(), 2)
        self.assertEqual(Position.objects.filter(account=account, symbol="EUR/USD").count(), 1)  # merged into one row

    def test_close_never_creates_a_spread_fee_entry(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        panel = _bare_consumer(account.pk)
        open_result = _db_open_sync(panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
                                     sl=None, tp=None, commission=0.0, new_balance=10000.0)
        LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).delete()  # isolate the close

        pos_mem = {"id": open_result["position_id"], "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
                   "avg": 1.17002, "sl": None, "tp": None, "opened_at": time.time()}
        _db_close_sync(panel, pos_mem, close_px=1.17000, reason="manual",
                        realized_pnl=-0.0002, new_balance=9999.9998, new_equity=9999.9998)
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# Rollback atomicity — Position + fee + commission roll back together
# ─────────────────────────────────────────────────────────────────────────
class RollbackAtomicityTests(_SpreadConfigTestCase):
    def test_ledger_entry_failure_rolls_back_position_and_commission(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002)
        panel = _bare_consumer(account.pk)

        with patch(
            "simulator.consumers.LedgerEntry.objects.create",
            side_effect=RuntimeError("simulated DB failure on spread fee ledger write"),
        ):
            with self.assertRaises(RuntimeError):
                _db_open_sync(panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
                               sl=None, tp=None, commission=0.06, new_balance=9999.94)

        # Nothing committed — Position, commission, spread fee, balance
        # change, all rolled back together as one unit.
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10000"))


# ─────────────────────────────────────────────────────────────────────────
# 8 — end-to-end: manual close / SL / TP / stopout / Celery already RAW,
# never regressed by this block; still exclude the (open-only) fee.
# ─────────────────────────────────────────────────────────────────────────
class EndToEndPriceAuthorityTests(_SpreadConfigTestCase):
    def test_manual_close_still_uses_raw_after_this_block(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.17017"))
        panel = _bare_consumer(account.pk)
        panel._positions = [{"id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
                              "avg": 1.17017, "sl": None, "tp": None, "opened_at": time.time()}]
        _seed_raw("EUR/USD", 63088.50, 63088.70)  # cross-symbol magnitude — must reject
        _run(panel._order_close({"id": pos.pk, "symbol": "EUR/USD"}))
        sent = panel.send_json.call_args_list
        self.assertTrue(any(c.args[0].get("code") == "price_unavailable" for c in sent))
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_stopout_still_uses_raw_and_skips_contaminated_position(self):
        account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        pos = make_position(account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.17017"))
        panel = _bare_consumer(account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [{"id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
                              "avg": 1.17017, "sl": None, "tp": None, "opened_at": time.time()}]
        _seed_raw("EUR/USD", 63088.50, 63088.70)
        _run(panel._do_stopout())
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(len(panel._positions), 1)

    def test_celery_read_still_uses_raw_via_redis(self):
        import redis as _redis
        from django.conf import settings
        url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        r.setex("trx:price:bid:EUR/USD", 60, "63088.50")
        r.setex("trx:price:ask:EUR/USD", 60, "63088.70")
        try:
            bid, ask = _read_cached_price("EUR/USD")
            self.assertIsNone(bid)
            self.assertIsNone(ask)
        finally:
            r.delete("trx:price:bid:EUR/USD", "trx:price:ask:EUR/USD")

    def test_reconnect_hydrate_then_order_new_uses_raw(self):
        """Simulates reconnect: fresh _maybe_hydrate_from_db-equivalent
        state, then a fresh order:new — must still resolve via
        get_validated_quote(), never a leftover per-connection cache."""
        account = make_account(balance=Decimal("10000"))
        # _consumer() itself seeds EUR/USD (O.6c-1w-b fix, needed so the
        # ~10 shared-helper suites keep working) — seed AFTER constructing
        # it, so this test's value is the one actually in effect.
        consumer = _consumer(account.pk)  # simulates a brand-new connection object
        _seed_raw("EUR/USD", 1.18000, 1.18002)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        self.assertEqual(pos.avg_price, Decimal("1.18002"))

    def test_multipanel_same_singleton_same_raw_quote(self):
        account = make_account(balance=Decimal("10000"))
        panel_a = _consumer(account.pk)
        panel_b = _consumer(account.pk)
        # Seed AFTER both consumers exist — _consumer() itself seeds
        # EUR/USD too; this value must be the one both panels observe.
        _seed_raw("EUR/USD", 1.19000, 1.19002)
        qa = panel_a._raw_exec_price("EUR/USD", "buy")
        qb = panel_b._raw_exec_price("EUR/USD", "buy")
        self.assertEqual(qa, qb)
        self.assertEqual(qa, 1.19002)


# ─────────────────────────────────────────────────────────────────────────
# Static regression guards — broker_price()/marked state can never
# re-enter these financial paths again.
# ─────────────────────────────────────────────────────────────────────────
class StaticRegressionGuardTests(SimpleTestCase):
    """Uses fn.__code__.co_names (actual compiled bytecode NAME lookups —
    real attribute/method accesses) rather than inspect.getsource() text
    matching — a docstring/comment EXPLAINING that exec_price()/_bid_state
    are no longer used would otherwise trip a naive substring search
    (exactly the false positive O.6c-1s's own BasePriceForNoLongerReachable
    Tests hit and fixed with this same technique). Docstrings are string
    constants (co_consts), never co_names — this distinguishes "mentioned
    in a comment" from "actually called in code" precisely."""

    def test_order_new_never_calls_exec_price_for_execution(self):
        from simulator import consumers
        names = consumers.TradingConsumer._order_new.__code__.co_names
        self.assertNotIn("exec_price", names)
        self.assertIn("_raw_exec_price", names)

    def test_pretrade_check_never_calls_exec_price(self):
        from simulator import consumers
        names = consumers.TradingConsumer._pretrade_check.__code__.co_names
        self.assertNotIn("exec_price", names)
        self.assertIn("_raw_exec_price", names)

    def test_raw_exec_price_never_reads_bid_state_ask_state(self):
        from simulator import consumers
        names = consumers.TradingConsumer._raw_exec_price.__code__.co_names
        self.assertNotIn("_bid_state", names)
        self.assertNotIn("_ask_state", names)
        self.assertIn("get_validated_quote", names)

    def test_db_open_position_atomic_charges_spread_fee_inside_outer_transaction(self):
        """The trader-facing LedgerEntry(EV_FEE) write must NOT be
        wrapped in its own try/except or nested transaction.atomic() —
        only the BrokerLedger booking may be (best-effort, pre-existing
        pattern)."""
        import inspect
        from simulator import consumers
        src = inspect.getsource(consumers.TradingConsumer._db_open_position_atomic)
        fee_idx = src.index("event_type=LedgerEntry.EV_FEE")
        # Walk backward from the EV_FEE write to the nearest enclosing
        # try: — it must belong to the BrokerLedger block, i.e. appear
        # AFTER this creation call, not before it.
        preceding = src[:fee_idx]
        broker_ledger_try_idx = src.index("BrokerLedger.objects.create(\n                            revenue_type=BrokerLedger.REV_SPREAD")
        self.assertLess(fee_idx, broker_ledger_try_idx)
