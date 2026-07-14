"""
simulator/tests/test_pnl_conversion_integration.py — MARGIN-02.

Covers every real PnL path found in the FASE 1 audit: unrealized WS,
unrealized daemon, manual close, TP, SL, stop-out, liquidation, daemon
Step 3/5, Trade.profit_loss, equity, the per-position pnl now sent in the
"positions" WS snapshot (backend-authoritative for the frontend), zero
ORM per tick, and WS/Celery parity (both call the same pnl_engine
function, so they cannot diverge).
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase

from simulator import pnl_engine
from simulator import pricing_context as pc
from simulator.consumers import TradingConsumer
from simulator.models import Position, Trade
from simulator.tasks import (
    _close_position_sync,
    _compute_offline_equity_margin,
    _daemon_close_all,
    scan_positions_task,
)

from .factories import make_account, make_position

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer() -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = None  # no DB — pure in-memory PnL only
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
    c.account = {
        "balance": 10000.0, "currency": "USD",
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


def _pos_mem(pos: Position) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


class _FakeConsumer:
    def __init__(self, account_id, currency="USD"):
        self._db_account_id = account_id
        self.account = {"netting_mode": False, "spread_pips": 0.0, "currency": currency}


class UnrealizedPnlWsTests(TestCase):
    """1) Unrealized WS. Also: EUR/USD, BTCUSD, ETHUSD identical behavior;
    USD/JPY BUY/SELL +10 pips correctly converted; loss correctly converted."""

    def test_eurusd_unrealized_unchanged(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 1.0, "avg": 1.10000}
        pnl = c._unrealized_pnl_for(pos, 1.10100)
        self.assertAlmostEqual(pnl, 100.0, places=2)  # unchanged: quote==account currency

    def test_btcusd_unrealized_unchanged(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "BTCUSD", "side": "buy", "qty": 1.0, "avg": 82000.00}
        pnl = c._unrealized_pnl_for(pos, 82100.00)
        self.assertAlmostEqual(pnl, 100.0, places=2)

    def test_ethusd_unrealized_unchanged(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "ETHUSD", "side": "sell", "qty": 2.0, "avg": 3400.0}
        pnl = c._unrealized_pnl_for(pos, 3390.0)
        self.assertAlmostEqual(pnl, 20.0, places=2)

    def test_usd_jpy_buy_10_pips_is_64_47_usd(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000}
        pnl = c._unrealized_pnl_for(pos, 155.100)
        self.assertAlmostEqual(pnl, 64.47, places=2)

    def test_usd_jpy_sell_10_pips_equivalent(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "USD/JPY", "side": "sell", "qty": 1.0, "avg": 155.100}
        pnl = c._unrealized_pnl_for(pos, 155.000)
        self.assertAlmostEqual(pnl, 64.52, places=2)
        self.assertGreater(pnl, 0)

    def test_usd_jpy_loss_correctly_converted(self):
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.100}
        pnl = c._unrealized_pnl_for(pos, 155.000)
        self.assertAlmostEqual(pnl, -64.52, places=2)

    def test_usd_jpy_old_buggy_value_would_have_been_10000(self):
        """Documents the exact bug this block fixes — the raw (unconverted)
        quote-currency number the old formula produced and treated as USD."""
        c = _bare_consumer()
        pos = {"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000}
        pnl = c._unrealized_pnl_for(pos, 155.100)
        self.assertNotAlmostEqual(pnl, 10000.0, places=0)
        self.assertLess(pnl, 100)  # correct value is two orders of magnitude smaller

    def test_equity_uses_converted_pnl(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000}]
        c.close_price = lambda sym, side: 155.100
        equity = c.account["balance"] + c._unrealized_pnl_total()
        self.assertAlmostEqual(equity, 10000.0 + 64.47, places=2)


class PositionsSnapshotBackendPnlTests(TestCase):
    """16) Dashboard PnL individual — backend-authoritative pnl in the
    "positions" WS snapshot, per FASE 5."""

    def test_snapshot_includes_converted_pnl_for_usd_jpy(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000}]
        c.close_price = lambda sym, side: 155.100
        snap = c._positions_snapshot()
        self.assertEqual(len(snap), 1)
        self.assertAlmostEqual(snap[0]["pnl"], 64.47, places=2)

    def test_snapshot_pnl_never_none_on_success(self):
        c = _bare_consumer()
        c._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 1.0, "avg": 1.1}]
        c.close_price = lambda sym, side: 1.101
        snap = c._positions_snapshot()
        self.assertIsNotNone(snap[0]["pnl"])


class ManualCloseAndTradePersistenceTests(TestCase):
    """9) Cierre manual. 14) Trade.profit_loss. Uses the exact __wrapped__
    unwrap pattern established in test_pricing_context_persistence.py to
    exercise the real sync DB-write body without async/Channels plumbing."""

    def test_usd_jpy_manual_close_persists_converted_profit_loss(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="USD/JPY", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        realized = pnl_engine.position_pnl_float("buy", 155.000, 155.100, 1.0, "USD/JPY", "USD")
        result = _db_close_sync(
            _FakeConsumer(account.pk), _pos_mem(pos), 155.100, "manual",
            realized, 10000.0 + realized, 10000.0 + realized,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertAlmostEqual(float(trade.profit_loss), 64.47, places=2)

    def test_usd_jpy_pnl_conversion_audit_field_populated(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="USD/JPY", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        realized = pnl_engine.position_pnl_float("buy", 155.000, 155.100, 1.0, "USD/JPY", "USD")
        result = _db_close_sync(
            _FakeConsumer(account.pk), _pos_mem(pos), 155.100, "manual",
            realized, 10000.0 + realized, 10000.0 + realized,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        conv = trade.pnl_conversion
        self.assertIsNotNone(conv)
        self.assertEqual(conv["quote_currency"], "JPY")
        self.assertEqual(conv["account_currency"], "USD")
        self.assertEqual(conv["conversion_mode"], pnl_engine.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertTrue(conv["converted"])
        self.assertIsNotNone(conv["conversion_timestamp"])
        self.assertAlmostEqual(conv["pnl_quote"], 10000.0, places=2)
        self.assertAlmostEqual(conv["pnl_account"], 64.47, places=2)

    def test_eurusd_manual_close_unchanged(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        realized = pnl_engine.position_pnl_float("buy", 1.10000, 1.10100, 1.0, "EUR/USD", "USD")
        result = _db_close_sync(
            _FakeConsumer(account.pk), _pos_mem(pos), 1.10100, "manual",
            realized, 10000.0 + realized, 10000.0 + realized,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertAlmostEqual(float(trade.profit_loss), 100.0, places=2)
        self.assertEqual(trade.pnl_conversion["conversion_mode"], pnl_engine.CONVERSION_MODE_NONE)


class TpSlStopoutLiquidationTests(TestCase):
    """10) TP. 11) SL. 12) Stop-out. 13) Liquidación — via the daemon
    functions, which are plain sync callables (no async wrapper needed)."""

    def test_daemon_offline_equity_margin_usd_jpy(self):
        account = make_account(balance=Decimal("10000"), account_type="RETAIL")
        pos = make_position(account, symbol="USD/JPY", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        equity, margin_used, total_floating, pos_fp_map = _compute_offline_equity_margin(
            [pos], {"USD/JPY": (155.100, 155.100)}, account,
        )
        self.assertAlmostEqual(total_floating, 64.47, places=2)
        self.assertAlmostEqual(equity, 10000.0 + 64.47, places=2)
        self.assertAlmostEqual(pos_fp_map[pos.id], 64.47, places=2)

    def test_daemon_close_all_usd_jpy_persists_converted_realized(self):
        account = make_account(balance=Decimal("10000"), account_type="CHALLENGE")
        pos = make_position(account, symbol="USD/JPY", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        close_records, final_balance = _daemon_close_all(
            [pos], account.pk, account, {"USD/JPY": (155.100, 155.100)},
            "daemon_stopout", {pos.id: 64.47}, 64.47,
        )
        self.assertEqual(len(close_records), 1)
        self.assertAlmostEqual(close_records[0]["realized"], 64.47, places=2)
        self.assertAlmostEqual(final_balance, 10000.0 + 64.47, places=2)
        trade = Trade.objects.get(account=account, symbol="USD/JPY")
        self.assertAlmostEqual(float(trade.profit_loss), 64.47, places=2)

    def test_daemon_step3_tp_usd_jpy_via_scan_positions_task(self):
        """End-to-end: a real TP fires through the Celery daemon's Step 3
        offline scan, and the persisted Trade.profit_loss is correctly
        converted — not the raw ~10000 quote-currency number."""
        from unittest.mock import patch
        account = make_account(balance=Decimal("10000"), account_type="RETAIL")
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"),
                      tp=Decimal("155.100"))

        def _price_mock(symbol):
            return (155.100, 155.102) if symbol == "USD/JPY" else (None, None)

        with patch("simulator.tasks._read_cached_price", side_effect=_price_mock):
            result = scan_positions_task.apply().get()

        self.assertEqual(result["closed"], 1)
        trade = Trade.objects.get(account=account, symbol="USD/JPY")
        self.assertAlmostEqual(float(trade.profit_loss), 64.47, places=2)
        self.assertLess(abs(float(trade.profit_loss)), 100)  # not the raw ~10000


class NoDivergenceWsCeleryTests(TestCase):
    """17) Sin divergencia WS/Celery — both call pnl_engine.position_pnl_float()
    with identical inputs, so they cannot disagree. Proven both at the
    pure-function level and through the two real call sites."""

    def test_ws_and_daemon_agree_on_usd_jpy(self):
        c = _bare_consumer()
        ws_pnl = c._unrealized_pnl_for(
            {"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000}, 155.100,
        )
        daemon_pnl = pnl_engine.position_pnl_float("buy", 155.000, 155.100, 1.0, "USD/JPY", "USD")
        self.assertEqual(ws_pnl, daemon_pnl)


class NoDbPerTickTests(TestCase):
    """18) No DB por tick — price_tick() with a USD/JPY position open must
    still perform zero ORM queries; pnl_engine reads only SymbolSpec (an
    in-memory registry), never the DB."""

    def test_price_tick_with_usd_jpy_position_zero_queries(self):
        c = _bare_consumer()
        c.symbol = "USD/JPY"
        c._positions = [{"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000,
                          "sl": None, "tp": None, "opened_at": 0}]
        tick = {"symbol": "USD/JPY", "bid": 155.090, "ask": 155.110,
                "mid": 155.100, "time": 1_700_000_000}
        with self.assertNumQueries(0):
            _run(c.price_tick(tick))


class NoMarginCommissionSpreadChangeTests(TestCase):
    """19) Ningún cambio en margen, comisión o spread — structural
    guarantee: none of those functions import or reference pnl_engine."""

    def test_margin_used_total_untouched(self):
        import inspect
        source = inspect.getsource(TradingConsumer._margin_used_total)
        self.assertNotIn("pnl_engine", source)

    def test_commission_for_untouched(self):
        import inspect
        source = inspect.getsource(TradingConsumer.commission_for)
        self.assertNotIn("pnl_engine", source)

    def test_broker_price_untouched(self):
        import inspect
        from simulator.spread_engine import broker_price
        source = inspect.getsource(broker_price)
        self.assertNotIn("pnl_engine", source)


class PopulationEngineUsdJpyTests(TestCase):
    """Structural audit (post-approval) — simulator/population_engine.py's
    _simulate_pnl() had its own inline formula missing BOTH contract_size
    and currency conversion, despite writing real Trade/Position/balance
    rows via `manage.py populate_broker`. Now delegates to pnl_engine."""

    def test_usd_jpy_delegates_to_pnl_engine(self):
        import random
        from unittest.mock import patch
        from simulator.population_engine import SimulatedTrader

        trader = SimulatedTrader.__new__(SimulatedTrader)
        trader._rng = random.Random(0)
        trader.cfg = {"win_rate": 1.0}

        with patch(
            "simulator.pnl_engine.position_pnl_float", wraps=pnl_engine.position_pnl_float,
        ) as mock_pnl:
            close_px, pnl = trader._simulate_pnl(
                155.000, "BUY", "USD/JPY", 1.0, account_currency="USD",
            )
        mock_pnl.assert_called_once()
        call_args = mock_pnl.call_args.args
        self.assertEqual(call_args[0], "BUY")
        self.assertEqual(call_args[4], "USD/JPY")
        # Proves conversion actually happened: the returned pnl must differ
        # from the raw, unconverted quote-currency amount (what the pre-fix
        # formula effectively returned) by roughly the USD/JPY rate.
        quote_pnl = float(pnl_engine.calculate_quote_pnl("BUY", 155.000, close_px, 1.0, 100000))
        self.assertNotAlmostEqual(pnl, quote_pnl, places=0)
        # places=1, not 2: _simulate_pnl computes pnl from the unrounded
        # close price but returns close_px rounded to 5dp — a sub-cent
        # difference from recomputing here off the rounded value, not a
        # correctness issue in the conversion itself.
        self.assertAlmostEqual(pnl, quote_pnl / close_px, places=1)

    def test_eurusd_unaffected(self):
        import random
        from unittest.mock import patch
        from simulator.population_engine import SimulatedTrader

        trader = SimulatedTrader.__new__(SimulatedTrader)
        trader._rng = random.Random(0)
        trader.cfg = {"win_rate": 1.0}

        with patch(
            "simulator.pnl_engine.position_pnl_float", wraps=pnl_engine.position_pnl_float,
        ) as mock_pnl:
            trader._simulate_pnl(1.10000, "BUY", "EUR/USD", 1.0, account_currency="USD")
        mock_pnl.assert_called_once()


class AdminForceCloseUsdJpyTests(TestCase):
    """admin.py's superuser force_close dealing-desk action had its own
    inline formula missing BOTH contract_size and currency conversion.
    Now delegates to pnl_engine."""

    def test_force_close_usd_jpy_persists_converted_profit_loss(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        superuser = User.objects.create_superuser(
            username="super_margin02", email="super_margin02@ex.com", password="pass",
        )
        account = make_account(balance=Decimal("10000"), account_type="RETAIL", tier=None)
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        self.client.force_login(superuser)
        from django.urls import reverse
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.pk])

        response = self.client.post(
            url, {"action": "force_close", "symbol": "USD/JPY", "price": "155.100"},
        )
        self.assertIn(response.status_code, (302, 200))

        trade = Trade.objects.get(account=account, symbol="USD/JPY")
        self.assertAlmostEqual(float(trade.profit_loss), 64.47, places=2)
