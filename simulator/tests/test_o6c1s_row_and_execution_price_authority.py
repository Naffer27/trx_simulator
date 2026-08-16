# simulator/tests/test_o6c1s_row_and_execution_price_authority.py
"""
O.6c-1s — POSITION ROW + CLOSE EXECUTION PRICE AUTHORITY FIX.

Root cause (O.6c-1r): O.6c-1q fixed the account-wide floating P&L/equity
aggregate (_unrealized_pnl_total()) to source price from self._feed
(get_feed_manager(), the shared global singleton) instead of
self._bid_state/self._ask_state (per-connection cache, only ever seeded
for the symbol(s) THIS connection's own chart has shown). But 4 other
call sites still used the OLD close_price() (-> self._bid_state ->
base_price_for() fallback, O.6c-1p's exact root cause) for something
O.6c-1q deliberately left out of scope:
  - _positions_snapshot() — the "pnl" field of every POSITIONS table row.
  - _order_close()        — manual close EXECUTION price.
  - _do_stopout()          — stopout EXECUTION price.
  - _do_retail_liquidation() — margin-call liquidation EXECUTION price.

Fix: all 4 now use self._feed_close_price() (O.6c-1q's helper, reused
verbatim, not reimplemented). No formula changed anywhere.

Fail-safe policy when self._feed.has_price(symbol) is False (mirrors
O.6c-1q's own established precedent, not a new invented policy):
  - _positions_snapshot(): that row's "pnl" is None (same degrade path
    already used for a pnl_engine exception — never a fabricated number).
  - _order_close(): the close is REJECTED before any DB write or memory
    mutation — client gets {"type":"error","code":"price_unavailable"} —
    the position stays open exactly as if the message never arrived.
  - _do_stopout()/_do_retail_liquidation(): that specific position is
    skipped (added to failed_positions, exactly like a DB-close exception
    already is) — it stays open, no Trade/LedgerEntry/balance write for
    it, and the next tick retries once a fresh price exists.

Uses TransactionTestCase throughout — same reasoning as
test_o6c1q_runtime_accounting_price_authority.py:
@database_sync_to_async methods run on a separate thread, invisible to
TestCase's uncommitted per-test transaction on SQLite.
"""
import inspect
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from simulator.consumers import TradingConsumer
from simulator.models import LedgerEntry, Position, Trade, TradingAccount
from market_data.feeds import get_feed_manager

from .factories import make_account, make_position

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _run(coro):
    return __import__("asyncio").run(coro)


def _seed_feed_manager(symbol: str, bid: float, ask: float, *, fresh: bool = True):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = (bid + ask) / 2
        feed._price_ts[symbol] = time.time() if fresh else (time.time() - 3600)


def _clear_feed_manager(symbol: str):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


def _bare_consumer(account_id, symbol="EUR/USD") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = symbol
    c._positions = []
    c._unpriced_pnl_symbols = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    c._bid_state = {}   # deliberately empty — the O.6c-1p scenario
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._feed = get_feed_manager()
    c.account = {
        "balance": 999.82, "equity": 999.82, "peak_balance": 1000.00,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": "Activo", "account_type": "STANDARD",
        "tier": "10K", "profit_target": 0.0, "initial_balance": 1000.00,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


def _pos(pos_id, symbol, side, qty, avg, sl=None, tp=None):
    return {"id": pos_id, "symbol": symbol, "side": side, "qty": qty, "avg": avg,
            "sl": sl, "tp": tp, "opened_at": time.time()}


# ─────────────────────────────────────────────────────────────────────────
# 1. Reproducción exacta Position 313/314 — la fila NUNCA muestra ~189.65
# ─────────────────────────────────────────────────────────────────────────
class RowPnlExactReproductionTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_positions_snapshot_never_shows_189_65(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        c = _bare_consumer(51, symbol="EUR/USD")  # panel que nunca cargó BTCUSD
        c._positions = [
            _pos(312, "BTCUSD", "buy", 0.01, 62889.59),
            _pos(313, "BTCUSD", "buy", 0.01, 63034.70),
            _pos(314, "BTCUSD", "buy", 0.01, 63034.00),
        ]
        snap = c._positions_snapshot()
        pnls = {item["id"]: item["pnl"] for item in snap}
        self.assertNotAlmostEqual(pnls[313], 189.65, places=1)
        self.assertNotAlmostEqual(pnls[314], 189.66, places=1)
        self.assertAlmostEqual(pnls[313], 0.01 * (63012.60 - 63034.70), places=2)
        self.assertAlmostEqual(pnls[314], 0.01 * (63012.60 - 63034.00), places=2)


# ─────────────────────────────────────────────────────────────────────────
# 2/3. _positions_snapshot() BUY=bid global, SELL=ask global
# ─────────────────────────────────────────────────────────────────────────
class PositionsSnapshotSideTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_buy_row_pnl_uses_global_bid(self):
        _seed_feed_manager("BTCUSD", bid=63000.0, ask=63010.0)
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.02, 62800.0)]
        pnl = c._positions_snapshot()[0]["pnl"]
        self.assertAlmostEqual(pnl, round(0.02 * (63000.0 - 62800.0), 2), places=2)

    def test_sell_row_pnl_uses_global_ask(self):
        _seed_feed_manager("BTCUSD", bid=63000.0, ask=63010.0)
        c = _bare_consumer(1)
        c._positions = [_pos(2, "BTCUSD", "sell", 0.02, 63200.0)]
        pnl = c._positions_snapshot()[0]["pnl"]
        self.assertAlmostEqual(pnl, round(0.02 * (63200.0 - 63010.0), 2), places=2)


# ─────────────────────────────────────────────────────────────────────────
# 4. Multipanel — mismo PnL por posición sin importar el símbolo activo
# ─────────────────────────────────────────────────────────────────────────
class MultipanelRowPnlAgreementTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_panel_a_btcusd_and_panel_b_eurusd_agree_on_row_pnl(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        positions = [_pos(312, "BTCUSD", "buy", 0.01, 62889.59)]

        panel_a = _bare_consumer(51, symbol="BTCUSD")
        panel_a._positions = list(positions)
        panel_a._bid_state["BTCUSD"] = 63012.60  # A has charted it

        panel_b = _bare_consumer(51, symbol="EUR/USD")
        panel_b._positions = list(positions)
        # B never charted BTCUSD — _bid_state stays empty, must no longer matter.

        pnl_a = panel_a._positions_snapshot()[0]["pnl"]
        pnl_b = panel_b._positions_snapshot()[0]["pnl"]
        self.assertAlmostEqual(pnl_a, pnl_b, places=6)


# ─────────────────────────────────────────────────────────────────────────
# 5. Reload vs live update — mismo PnL antes/después de refresh
# ─────────────────────────────────────────────────────────────────────────
class ReloadVsLiveUpdateConsistencyTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_snapshot_before_and_after_refresh_is_identical(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        c = _bare_consumer(51, symbol="EUR/USD")
        c._positions = [_pos(313, "BTCUSD", "buy", 0.01, 63034.70)]
        pnl_live = c._positions_snapshot()[0]["pnl"]
        # "Reload" — a brand-new connection, same account, same DB state.
        c2 = _bare_consumer(51, symbol="BTCUSD")
        c2._positions = [_pos(313, "BTCUSD", "buy", 0.01, 63034.70)]
        c2._bid_state["BTCUSD"] = 63012.60
        pnl_reload = c2._positions_snapshot()[0]["pnl"]
        self.assertAlmostEqual(pnl_live, pnl_reload, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 6/7. Manual close — precio real vs sin precio fresco
# ─────────────────────────────────────────────────────────────────────────
class ManualCloseExecutionPriceTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_manual_close_with_empty_local_cache_uses_real_feed_price(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        account = make_account(account_type="STANDARD", balance=Decimal("999.82"))
        pos = make_position(account, symbol="BTCUSD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")  # never charted BTCUSD
        c._positions = [_pos(pos.pk, "BTCUSD", "buy", 0.01, 62889.59)]

        _run(c._order_close({"id": pos.pk, "symbol": "BTCUSD"}))

        trade = Trade.objects.get(account=account)
        self.assertAlmostEqual(float(trade.exit_price), 63012.60, places=2)
        self.assertAlmostEqual(float(trade.profit_loss), 0.01 * (63012.60 - 62889.59), places=2)
        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 999.82 + 0.01 * (63012.60 - 62889.59), places=2)

    def test_manual_close_without_fresh_price_is_safely_rejected(self):
        # No _seed_feed_manager call — FeedManager genuinely has no BTCUSD price.
        account = make_account(account_type="STANDARD", balance=Decimal("999.82"))
        pos = make_position(account, symbol="BTCUSD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c._positions = [_pos(pos.pk, "BTCUSD", "buy", 0.01, 62889.59)]

        _run(c._order_close({"id": pos.pk, "symbol": "BTCUSD"}))

        self.assertFalse(Trade.objects.filter(account=account).exists())
        self.assertFalse(LedgerEntry.objects.filter(account=account).exists())
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("999.82"))
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        # Position never removed from in-memory state either.
        self.assertEqual(len(c._positions), 1)
        sent_types = [call.args[0].get("type") for call in c.send_json.await_args_list]
        self.assertIn("error", sent_types)


# ─────────────────────────────────────────────────────────────────────────
# 8/9. Stopout — cache vacía / sin precio fresco
# ─────────────────────────────────────────────────────────────────────────
class StopoutExecutionPriceTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_stopout_with_empty_local_cache_uses_feed_manager(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("1000"))
        pos = make_position(account, symbol="BTCUSD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c.account["peak_balance"] = 1000.0
        c._positions = [_pos(pos.pk, "BTCUSD", "buy", 0.01, 62889.59)]

        _run(c._do_stopout())

        trade = Trade.objects.get(account=account)
        self.assertAlmostEqual(float(trade.exit_price), 63012.60, places=2)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    def test_stopout_without_fresh_price_does_not_realize_fake_loss(self):
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("1000"))
        pos = make_position(account, symbol="BTCUSD", side="SELL",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c.account["peak_balance"] = 1000.0
        c._positions = [_pos(pos.pk, "BTCUSD", "sell", 0.01, 62889.59)]

        _run(c._do_stopout())

        # base_price_for(BTCUSD)=82000 on a SELL would have booked a huge
        # fake loss (62889.59-82000)*0.01 = -191.10. Confirm no Trade at all.
        self.assertFalse(Trade.objects.filter(account=account).exists())
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("1000"))
        # Position stays in memory too (moved to failed_positions).
        self.assertEqual(len(c._positions), 1)


# ─────────────────────────────────────────────────────────────────────────
# 10. Retail liquidation — mismos dos escenarios
# ─────────────────────────────────────────────────────────────────────────
class RetailLiquidationExecutionPriceTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_retail_liquidation_with_real_price(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        account = make_account(account_type="RETAIL", balance=Decimal("1000"))
        pos = make_position(account, symbol="BTCUSD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c._positions = [_pos(pos.pk, "BTCUSD", "buy", 0.01, 62889.59)]

        _run(c._do_retail_liquidation())

        trade = Trade.objects.get(account=account)
        self.assertAlmostEqual(float(trade.exit_price), 63012.60, places=2)

    def test_retail_liquidation_without_fresh_price_refuses(self):
        account = make_account(account_type="RETAIL", balance=Decimal("1000"))
        pos = make_position(account, symbol="BTCUSD", side="SELL",
                             qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c._positions = [_pos(pos.pk, "BTCUSD", "sell", 0.01, 62889.59)]

        _run(c._do_retail_liquidation())

        self.assertFalse(Trade.objects.filter(account=account).exists())
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("1000"))


# ─────────────────────────────────────────────────────────────────────────
# 11. base_price_for() ya no entra en ningún cálculo de las 5 rutas
# ─────────────────────────────────────────────────────────────────────────
class BasePriceForNoLongerReachableTests(TransactionTestCase):
    """Checks the compiled bytecode's co_names (actual NAME lookups the
    function performs) rather than raw source text — a docstring
    mentioning "base_price_for" (explaining why it's deliberately NOT
    used) is a string constant, never a name the code looks up, so this
    correctly distinguishes "documented as absent" from "still called"."""

    def test_source_of_all_five_paths_never_references_base_price_for(self):
        for fn in (
            TradingConsumer._unrealized_pnl_total,
            TradingConsumer._positions_snapshot,
            TradingConsumer._order_close,
            TradingConsumer._do_stopout,
            TradingConsumer._do_retail_liquidation,
        ):
            names = fn.__code__.co_names
            self.assertNotIn("base_price_for", names, f"{fn.__name__} still calls base_price_for()")
            self.assertNotIn("close_price", names, f"{fn.__name__} still calls self.close_price()")

    def test_feed_close_price_itself_never_falls_back_to_synthetic(self):
        self.assertNotIn("base_price_for", TradingConsumer._feed_close_price.__code__.co_names)


# ─────────────────────────────────────────────────────────────────────────
# 12. No regresión — O.6c-1q/1o/1i, SL/TP, margin, ledger, balance
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_margin_used_formula_still_avg_price_based_unaffected(self):
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        margin = c._margin_used_total()
        self.assertAlmostEqual(margin, 62889.59 * 0.01 / 20, places=4)

    def test_check_tp_sl_still_uses_the_live_tick_not_feed_lookup(self):
        """SL/TP (O.6c-1p confirmed safe — tick-driven, not cache-lookup-
        driven) must remain untouched by this block."""
        src = inspect.getsource(TradingConsumer._check_tp_sl)
        self.assertIn("trigger_px = bid if side == \"buy\" else ask", src)

    def test_position_changed_and_candle_kline_handlers_untouched(self):
        self.assertTrue(hasattr(TradingConsumer, "position_changed"))
        self.assertTrue(hasattr(TradingConsumer, "candle_kline"))
        src = inspect.getsource(TradingConsumer.candle_kline)
        self.assertIn("tf_seconds(self.timeframe)", src)

    def test_stopout_still_suspends_account_when_price_is_healthy(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("1000"))
        make_position(account, symbol="BTCUSD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("70000.00"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c.account["peak_balance"] = 1000.0
        c._positions = [_pos(1, "BTCUSD", "buy", 1.0, 70000.00)]

        _run(c._do_stopout())

        account.refresh_from_db()
        self.assertEqual(account.status, "Suspendido")
