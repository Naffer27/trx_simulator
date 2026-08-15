# simulator/tests/test_o6c1q_runtime_accounting_price_authority.py
"""
O.6c-1q — RUNTIME ACCOUNTING PRICE AUTHORITY FIX.

Root cause (O.6c-1p, reproduced exactly): _unrealized_pnl_total() sourced
each position's closing price via close_price() -> get_bid()/get_ask() ->
self._bid_state/self._ask_state — a cache populated ONLY for the symbol(s)
THIS ONE WebSocket connection's own chart has shown (_seed_price_state(),
called only from connect()/change_symbol). A position on any OTHER symbol
silently fell back to base_price_for() (a synthetic seed constant, 82000.0
for BTCUSD) — reproduced bit-for-bit against real account data: 3 BTCUSD
positions (entries 62889.59/63034.70/63034.00) produced exactly +570.42
floating P&L against the fallback, vs the real ~+0.80 against a live price.

Fix: _unrealized_pnl_total() now resolves price via the new
_feed_close_price() helper, which reads self._feed (get_feed_manager(),
the shared process-global FeedManager singleton — the SAME source
RISK-02/broker_exposure.py/the atomic open guard already trust), never
self._bid_state/self._ask_state. When self._feed.has_price(symbol) is
False, that position is EXCLUDED from the floating-P&L sum (never priced
from base_price_for() or any synthetic value) — mirroring
broker_exposure.py's own established FASE 4 policy for the identical
problem at the broker-wide level. _recalc_account_and_push()'s real-time
stopout check is skipped entirely (fail-safe, "refuse to decide") for any
tick where this leaves self.account["equity"] incomplete.

pnl_engine.py, broker_exposure.py, broker_risk.py, models.py, and every
PnL/margin FORMULA are unchanged — only the price SOURCE for account-wide
aggregation changed. close_price() (order:close/SL/TP/stopout EXECUTION
price) is untouched — a separate, out-of-scope surface per the O.6c-1q
report.

Uses TransactionTestCase throughout — same reasoning already established
in test_multipanel_position_sync.py/test_o6c1o_*: @database_sync_to_async
methods run on a separate thread, invisible to TestCase's uncommitted
per-test transaction on SQLite.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase
from django.db import connection, reset_queries

from simulator import pnl_engine
from simulator.consumers import TradingConsumer
from simulator.models import TradingAccount
from market_data.feeds import get_feed_manager

from .factories import make_account, make_position

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _run(coro):
    return __import__("asyncio").run(coro)


def _seed_feed_manager(symbol: str, bid: float, ask: float, *, fresh: bool = True):
    """Writes directly into the real, process-global FeedManager singleton
    — same technique already established in
    test_atomic_margin_and_position_guard.py::_seed_all_prices() — so
    self._feed.has_price(symbol) reflects it for every bare consumer in
    this file (self._feed = get_feed_manager() always, unlike
    self._bid_state which is per-connection)."""
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
    from unittest.mock import AsyncMock

    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = symbol
    c._positions = []
    c._unpriced_pnl_symbols = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    # Deliberately empty — O.6c-1p's exact root cause: a connection whose
    # own chart has never shown the position's symbol.
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._feed = get_feed_manager()  # the REAL global singleton, not a fake
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


def _pos(pos_id, symbol, side, qty, avg):
    return {"id": pos_id, "symbol": symbol, "side": side, "qty": qty, "avg": avg,
            "sl": None, "tp": None, "opened_at": time.time()}


# ─────────────────────────────────────────────────────────────────────────
# 1. Reproducción exacta account 51 — jamás debe producir +570.42
# ─────────────────────────────────────────────────────────────────────────
class ExactAccount51ReproductionTests(TransactionTestCase):
    """3 posiciones BTCUSD reales (mismos entries del informe O.6c-1p),
    caché local (_bid_state) vacía, FeedManager con precio BTCUSD real."""

    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_never_produces_570_42_with_real_feed_price(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        c = _bare_consumer(51, symbol="EUR/USD")  # panel charting a DIFFERENT symbol
        c._positions = [
            _pos(312, "BTCUSD", "buy", 0.01, 62889.59),
            _pos(313, "BTCUSD", "buy", 0.01, 63034.70),
            _pos(314, "BTCUSD", "buy", 0.01, 63034.00),
        ]
        pnl = c._unrealized_pnl_total()
        equity = c.account["balance"] + pnl
        self.assertNotAlmostEqual(pnl, 570.42, places=1)
        self.assertNotAlmostEqual(equity, 1570.24, places=1)
        # Real expected value: 0.01*(63012.60-62889.59) + 0.01*(63012.60-63034.70)
        #                     + 0.01*(63012.60-63034.00) = 0.7951
        self.assertAlmostEqual(pnl, 0.7951, places=3)
        self.assertAlmostEqual(equity, 1000.6151, places=2)

    def test_base_price_for_no_longer_reachable_from_this_path(self):
        """base_price_for(BTCUSD) == 82000.0 — confirm it plays no role
        once a real FeedManager price exists, regardless of what
        self._bid_state/self._ask_state contain (empty here)."""
        from simulator.consumers import base_price_for
        self.assertEqual(base_price_for("BTCUSD"), 82000.0)
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        c = _bare_consumer(51, symbol="EUR/USD")
        c._positions = [_pos(312, "BTCUSD", "buy", 0.01, 62889.59)]
        pnl = c._unrealized_pnl_total()
        # If base_price_for() (82000) had leaked in: pnl would be ~191.10.
        self.assertLess(abs(pnl), 5.0)


# ─────────────────────────────────────────────────────────────────────────
# 2. Verificación matemática — misma fórmula que pnl_engine; BUY=bid, SELL=ask
# ─────────────────────────────────────────────────────────────────────────
class FormulaParityTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_buy_uses_bid_matches_pnl_engine_exactly(self):
        _seed_feed_manager("BTCUSD", bid=63000.0, ask=63010.0)
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.02, 62800.0)]
        pnl = c._unrealized_pnl_total()
        expected = pnl_engine.position_pnl_float("buy", 62800.0, 63000.0, 0.02, "BTCUSD", account_currency="USD")
        self.assertAlmostEqual(pnl, expected, places=6)

    def test_sell_uses_ask_matches_pnl_engine_exactly(self):
        _seed_feed_manager("BTCUSD", bid=63000.0, ask=63010.0)
        c = _bare_consumer(1)
        c._positions = [_pos(2, "BTCUSD", "sell", 0.02, 63200.0)]
        pnl = c._unrealized_pnl_total()
        expected = pnl_engine.position_pnl_float("sell", 63200.0, 63010.0, 0.02, "BTCUSD", account_currency="USD")
        self.assertAlmostEqual(pnl, expected, places=6)


# ─────────────────────────────────────────────────────────────────────────
# 3. Multipanel — mismo Equity/P&L independientemente del símbolo activo
# ─────────────────────────────────────────────────────────────────────────
class MultipanelSamePriceAuthorityTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_panel_a_btcusd_and_panel_b_other_symbol_agree(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        positions = [
            _pos(312, "BTCUSD", "buy", 0.01, 62889.59),
            _pos(313, "BTCUSD", "buy", 0.01, 63034.70),
            _pos(314, "BTCUSD", "buy", 0.01, 63034.00),
        ]

        panel_a = _bare_consumer(51, symbol="BTCUSD")
        panel_a._positions = list(positions)
        panel_a._bid_state["BTCUSD"] = 63012.60  # A HAS charted BTCUSD
        panel_a._ask_state["BTCUSD"] = 63012.80

        panel_b = _bare_consumer(51, symbol="EUR/USD")
        panel_b._positions = list(positions)
        # B has never charted BTCUSD — _bid_state stays empty (the exact
        # O.6c-1p scenario) — must no longer matter.

        pnl_a = panel_a._unrealized_pnl_total()
        pnl_b = panel_b._unrealized_pnl_total()
        self.assertAlmostEqual(pnl_a, pnl_b, places=6)


# ─────────────────────────────────────────────────────────────────────────
# 4. Protección contra falso stopout
# ─────────────────────────────────────────────────────────────────────────
class FalseStopoutProtectionTests(TransactionTestCase):
    """SELL donde base_price_for() produciría una pérdida ficticia enorme
    -> con FeedManager sano, jamás debe disparar stopout/liquidación."""

    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_sell_with_healthy_feed_price_does_not_trigger_stopout(self):
        # base_price_for(BTCUSD)=82000 vs entry 62889.59 on a SELL would
        # be a huge FAKE loss: 0.01*(62889.59-82000) = -191.10.
        _seed_feed_manager("BTCUSD", bid=62880.0, ask=62900.0)  # near entry, healthy
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("1000"))
        c = _bare_consumer(account.pk, symbol="EUR/USD")
        c.account["status"] = "Activo"
        c.account["peak_balance"] = 1000.0
        c.account["stopout_level"] = 50.0
        c._positions = [_pos(1, "BTCUSD", "sell", 0.01, 62889.59)]
        c._do_stopout = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
        c._do_retail_liquidation = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()

        _run(c._recalc_account_and_push())

        c._do_stopout.assert_not_awaited()
        c._do_retail_liquidation.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 5. Precio ausente — fail-safe, nunca fallback sintético
# ─────────────────────────────────────────────────────────────────────────
class MissingPriceFailSafeTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_no_fresh_price_excludes_position_never_uses_synthetic_fallback(self):
        # No _seed_feed_manager call — FeedManager genuinely has no BTCUSD price.
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        pnl = c._unrealized_pnl_total()
        self.assertEqual(pnl, 0.0)  # position excluded entirely, not priced at 0
        self.assertEqual(c._unpriced_pnl_symbols, ["BTCUSD"])

    def test_stale_price_beyond_ttl_is_treated_as_no_price(self):
        _seed_feed_manager("BTCUSD", bid=63000.0, ask=63010.0, fresh=False)
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        pnl = c._unrealized_pnl_total()
        self.assertEqual(pnl, 0.0)
        self.assertEqual(c._unpriced_pnl_symbols, ["BTCUSD"])

    def test_stopout_check_skipped_when_position_unpriced(self):
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("1000"))
        c = _bare_consumer(account.pk)
        c.account["status"] = "Activo"
        c.account["peak_balance"] = 1000.0
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]  # no feed price seeded
        c._do_stopout = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
        c._do_retail_liquidation = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()

        with self.assertLogs("simulator.ws", level="WARNING") as captured:
            _run(c._recalc_account_and_push())

        self.assertTrue(any("skipped this tick" in line for line in captured.output))
        c._do_stopout.assert_not_awaited()
        c._do_retail_liquidation.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 6. Margin — fórmula sin cambios
# ─────────────────────────────────────────────────────────────────────────
class MarginFormulaUnchangedTests(TransactionTestCase):
    def test_btcusd_001_uses_effective_leverage_20_not_50(self):
        c = _bare_consumer(1)
        c.account["leverage"] = 50
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        margin = c._margin_used_total()
        # effective_leverage = min(50, spec.max_leverage=20) = 20
        self.assertAlmostEqual(margin, 62889.59 * 0.01 / 20, places=4)

    def test_margin_uses_avg_price_never_feed_price(self):
        """Margin is entry-price based — must be identical whether or not
        a FeedManager price exists (unaffected by this fix, by design)."""
        _clear_feed_manager("BTCUSD")
        c = _bare_consumer(1)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        margin_no_price = c._margin_used_total()
        _seed_feed_manager("BTCUSD", bid=70000.0, ask=70010.0)
        margin_with_price = c._margin_used_total()
        self.assertAlmostEqual(margin_no_price, margin_with_price, places=6)
        _clear_feed_manager("BTCUSD")


# ─────────────────────────────────────────────────────────────────────────
# 7. Persisted equity — nunca contaminado con precio sintético
# ─────────────────────────────────────────────────────────────────────────
class PersistedEquityNeverSyntheticTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_db_sync_account_balances_persists_real_pnl_not_570_42(self):
        account = make_account(account_type="STANDARD", balance=Decimal("999.82"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"), avg_price=Decimal("62889.59"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"), avg_price=Decimal("63034.70"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"), avg_price=Decimal("63034.00"))
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)

        c = _bare_consumer(account.pk, symbol="EUR/USD")  # never charted BTCUSD
        c._positions = [
            _pos(1, "BTCUSD", "buy", 0.01, 62889.59),
            _pos(2, "BTCUSD", "buy", 0.01, 63034.70),
            _pos(3, "BTCUSD", "buy", 0.01, 63034.00),
        ]
        c.account["pnl_unreal"] = round(c._unrealized_pnl_total(), 2)

        db_sync = TradingConsumer._db_sync_account_balances.__wrapped__
        db_sync(c)

        account.refresh_from_db()
        persisted_pnl = float(account.equity) - float(account.balance)
        self.assertNotAlmostEqual(persisted_pnl, 570.42, places=1)
        self.assertAlmostEqual(persisted_pnl, 0.7951, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 8/9. No regresión MULTIPANEL-01 (O.6c-1o) / candle timeframe (O.6c-1i)
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionOtherBlocksTests(TransactionTestCase):
    """Full coverage lives in test_o6c1o_multipanel_position_state_consistency.py
    and test_o6c1i_candle_timeframe_fix.py (run as part of validation) — this
    class only spot-checks that this fix's own surface (candle_kline/
    position_changed dispatch) is untouched."""

    def test_position_changed_handler_still_present_and_unrelated_to_pricing_fix(self):
        self.assertTrue(hasattr(TradingConsumer, "position_changed"))
        self.assertTrue(hasattr(TradingConsumer, "execution_close"))

    def test_candle_kline_handler_untouched(self):
        self.assertTrue(hasattr(TradingConsumer, "candle_kline"))
        import inspect
        src = inspect.getsource(TradingConsumer.candle_kline)
        self.assertIn("tf_seconds(self.timeframe)", src)


# ─────────────────────────────────────────────────────────────────────────
# 10. Cero query DB adicional por tick
# ─────────────────────────────────────────────────────────────────────────
class NoExtraDbQueryTests(TransactionTestCase):
    def setUp(self):
        _clear_feed_manager("BTCUSD")

    def tearDown(self):
        _clear_feed_manager("BTCUSD")

    def test_unrealized_pnl_total_with_feed_lookup_hits_zero_queries(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        c = _bare_consumer(1)
        c._positions = [
            _pos(i, "BTCUSD", "buy", 0.01, 62889.59 + i) for i in range(5)
        ]
        reset_queries()
        c._unrealized_pnl_total()
        self.assertEqual(len(connection.queries), 0)

    def test_recalc_account_and_push_throttled_still_zero_queries(self):
        _seed_feed_manager("BTCUSD", bid=63012.60, ask=63012.80)
        account = make_account(account_type="STANDARD", balance=Decimal("999.82"))
        c = _bare_consumer(account.pk)
        c._positions = [_pos(1, "BTCUSD", "buy", 0.01, 62889.59)]
        c._last_db_sync = time.time()  # inside PANEL-02's throttle window
        with patch("simulator.risk_engine.check_equity_stopout", return_value=False):
            reset_queries()
            _run(c._recalc_account_and_push())
            self.assertEqual(len(connection.queries), 0)
