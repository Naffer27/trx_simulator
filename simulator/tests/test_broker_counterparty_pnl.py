"""
simulator/tests/test_broker_counterparty_pnl.py — BOOK-02.

Persists the broker's B-Book counterparty result for every closed Trade as
an explicit BrokerLedger(revenue_type=COUNTERPARTY_PNL) row:

    broker_counterparty_pnl = -Trade.profit_loss

This does NOT change any trader-facing formula — Trade.profit_loss,
LedgerEntry(EV_REALIZED), commission, spread, margin, leverage, and
TraderScore are all unchanged (see NoRegressionStructuralTests).

Break-even (trader_pnl == 0) ALWAYS creates a row too — amount=Decimal("0.00").
A closed Trade is a fact regardless of its PnL; every Trade gets exactly one
linked COUNTERPARTY_PNL entry, zero included, so source_trade reliably means
"this Trade has been reconciled against the broker book."

Coverage layers:
  - CreateBrokerCounterpartyEntryUnitTests: the shared helper in isolation
    (signs, break-even creates a zero-amount row, idempotency, meta, Decimal
    exactness).
  - WsCloseCounterpartyIntegrationTests: the WS canonical close path
    (TradingConsumer._db_close_position_atomic, via .__wrapped__ — same
    pattern as PANEL-02/03/BOOK-01) — covers manual close, TP/SL, stopout,
    retail liquidation (all four delegate to this one method post-PANEL-03).
  - DaemonCloseCounterpartyIntegrationTests: the Celery daemon's own
    canonical close (tasks._close_position_sync).
  - AdminForceCloseCounterpartyIntegrationTests: the admin "force_close"
    dealing-desk action, via the real view (Django test Client).
  - PopulationEngineCounterpartyIntegrationTests: simulated-trader closes
    (population_engine.SimulatedTrader._close_position) — these write real
    Trade/LedgerEntry rows counted everywhere else, so they get a real
    counterparty entry too. _simulate_pnl (randomised) is patched to a
    fixed return so the test is deterministic; _close_position itself is
    exercised unmodified.
  - NoRegressionStructuralTests: commission/spread still fire exactly once
    at open, unaffected by the new counterparty write at close.

Excluded routes (documented, not silently skipped):
  - consumers.py::_db_mirror_close_position — dead code. Confirmed by grep:
    zero callers anywhere in the codebase (superseded by
    _db_close_position_atomic since PANEL-02/03). Not wired, not tested.
  - consumers.py::_order_close / _check_tp_sl / _do_stopout /
    _do_retail_liquidation — per BOOK-02 spec, these must keep delegating
    to _db_close_position_atomic rather than writing BrokerLedger
    themselves. They are covered indirectly: WsCloseCounterpartyIntegrationTests
    exercises the shared method they all call into.
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse

from simulator import tasks as sim_tasks
from simulator.broker_ledger import create_broker_counterparty_entry
from simulator.consumers import TradingConsumer
from simulator.models import (
    BrokerLedger, LedgerEntry, Trade, TradingAccount, TraderScore, Position,
)

from .factories import make_account, make_user
from .test_order_ticket_sl_tp_validation import _consumer

_run = lambda coro: asyncio.run(coro)


def _open(consumer, **kw):
    defaults = dict(
        symbol="BTCUSD", side="buy", qty=1.0, price=100.0,
        sl=None, tp=None, commission=0.0, new_balance=100000.0,
        pricing_context=None,
    )
    defaults.update(kw)
    return TradingConsumer._db_open_position_atomic.__wrapped__(consumer, **defaults)


def _close(consumer, pos_mem, **kw):
    defaults = dict(
        close_px=110.0, reason="manual", realized_pnl=10.0,
        new_balance=100010.0, new_equity=100010.0, pricing_context_close=None,
    )
    defaults.update(kw)
    return TradingConsumer._db_close_position_atomic.__wrapped__(consumer, pos_mem, **defaults)


def _pos_mem(open_result, symbol, side, qty, price, sl=None, tp=None):
    return {
        "id": open_result["position_id"], "symbol": symbol, "side": side,
        "qty": qty, "avg": price, "sl": sl, "tp": tp,
        "opened_at": int(time.time()),
    }


# ─────────────────────────────────────────────────────────────────────────
# 1. Shared helper — pure/isolated
# ─────────────────────────────────────────────────────────────────────────
class CreateBrokerCounterpartyEntryUnitTests(TestCase):
    def setUp(self):
        self.account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        self.trade = Trade.objects.create(
            account=self.account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.00"), entry_price=Decimal("100"),
            exit_price=Decimal("110"), profit_loss=Decimal("10.00"),
        )

    def test_trader_gain_produces_negative_broker_amount(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("10.00"), "manual")
        self.assertEqual(entry.amount, Decimal("-10.00"))
        self.assertEqual(entry.revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)

    def test_trader_loss_produces_positive_broker_amount(self):
        self.trade.profit_loss = Decimal("-10.00")
        self.trade.save(update_fields=["profit_loss"])
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("-10.00"), "manual")
        self.assertEqual(entry.amount, Decimal("10.00"))

    def test_breakeven_creates_exactly_one_row_with_zero_amount(self):
        self.trade.profit_loss = Decimal("0.00")
        self.trade.save(update_fields=["profit_loss"])
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("0.00"), "manual")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount, Decimal("0.00"))
        self.assertEqual(entry.revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(
            BrokerLedger.objects.filter(source_trade=self.trade, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(),
            1,
        )

    def test_breakeven_source_trade_and_source_account_correct(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("0.00"), "manual")
        self.assertEqual(entry.source_trade_id, self.trade.id)
        self.assertEqual(entry.source_account_id, self.account.id)
        self.assertEqual(entry.symbol, self.trade.symbol)

    def test_breakeven_meta_complete(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("0.00"), "manual")
        self.assertEqual(entry.meta["trader_pnl"], 0.0)
        self.assertEqual(entry.meta["broker_counterparty_pnl"], 0.0)
        self.assertEqual(entry.meta["account_type"], "STANDARD")
        self.assertEqual(entry.meta["book_mode"], "B_BOOK")
        self.assertEqual(entry.meta["close_reason"], "manual")
        self.assertEqual(entry.meta["schema_version"], 1)

    def test_breakeven_second_call_does_not_duplicate(self):
        first = create_broker_counterparty_entry(self.trade, self.account, Decimal("0.00"), "manual")
        second = create_broker_counterparty_entry(self.trade, self.account, Decimal("0.00"), "manual")
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            BrokerLedger.objects.filter(source_trade=self.trade, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(),
            1,
        )

    def test_breakeven_accepts_float_zero_and_stores_clean_decimal(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, 0.0, "manual")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount, Decimal("0.00"))
        # Must be a clean positive zero, not "-0.00" (both compare equal to
        # Decimal("0") but the literal string form matters for the report).
        self.assertEqual(str(entry.amount).lstrip("-"), str(entry.amount))

    def test_source_trade_and_source_account_linked(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("10.00"), "manual")
        self.assertEqual(entry.source_trade_id, self.trade.id)
        self.assertEqual(entry.source_account_id, self.account.id)
        self.assertEqual(entry.symbol, self.trade.symbol)

    def test_meta_contents(self):
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("10.00"), "take_profit")
        self.assertEqual(entry.meta["trader_pnl"], 10.0)
        self.assertEqual(entry.meta["broker_counterparty_pnl"], -10.0)
        self.assertEqual(entry.meta["account_type"], "STANDARD")
        self.assertEqual(entry.meta["book_mode"], "B_BOOK")
        self.assertEqual(entry.meta["close_reason"], "take_profit")
        self.assertEqual(entry.meta["schema_version"], 1)

    def test_idempotent_second_call_does_not_duplicate(self):
        first = create_broker_counterparty_entry(self.trade, self.account, Decimal("10.00"), "manual")
        second = create_broker_counterparty_entry(self.trade, self.account, Decimal("10.00"), "manual")
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            BrokerLedger.objects.filter(source_trade=self.trade, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(),
            1,
        )

    def test_decimal_exactness_no_float_drift(self):
        # 33.33 has no exact binary float representation — this must still
        # round-trip exactly through Decimal, not "33.33000000000001"-style drift.
        entry = create_broker_counterparty_entry(self.trade, self.account, Decimal("33.33"), "manual")
        self.assertEqual(entry.amount, Decimal("-33.33"))
        entry.refresh_from_db()
        self.assertEqual(entry.amount, Decimal("-33.33"))

    def test_accepts_float_trader_pnl_input(self):
        # Callers today mostly pass floats (realized_pnl is a float end to
        # end in consumers.py/tasks.py) — must not raise, must convert cleanly.
        entry = create_broker_counterparty_entry(self.trade, self.account, 10.0, "manual")
        self.assertEqual(entry.amount, Decimal("-10"))


# ─────────────────────────────────────────────────────────────────────────
# 2. WS canonical close path — TradingConsumer._db_close_position_atomic
# ─────────────────────────────────────────────────────────────────────────
class WsCloseCounterpartyIntegrationTests(TransactionTestCase):
    def test_caso_a_trader_plus_10(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        ledger = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_REALIZED)
        self.assertEqual(ledger.amount, Decimal("10.00"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10.00"))
        self.assertEqual(cp.source_trade_id, trade.id)

    def test_caso_b_trader_minus_10(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=90.0, realized_pnl=-10.0, new_balance=99990.0, new_equity=99990.0)

        cp = BrokerLedger.objects.get(
            source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
        )
        self.assertEqual(cp.amount, Decimal("10.00"))

    def test_ws_close_breakeven_creates_zero_amount_row(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=100.0, realized_pnl=0.0, new_balance=100000.0, new_equity=100000.0)

        trade = Trade.objects.get(account=account)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("0.00"))
        self.assertEqual(cp.source_trade_id, trade.id)
        self.assertEqual(cp.source_account_id, account.id)
        self.assertEqual(cp.meta["trader_pnl"], 0.0)
        self.assertEqual(cp.meta["book_mode"], "B_BOOK")

    def test_caso_c_commission_and_spread_broker_net(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        rows = list(BrokerLedger.objects.filter(source_account=account).order_by("revenue_type"))
        by_type = {r.revenue_type: r.amount for r in rows}
        self.assertEqual(by_type[BrokerLedger.REV_COMMISSION], Decimal("5.00"))
        self.assertEqual(by_type[BrokerLedger.REV_SPREAD], Decimal("1.00"))
        self.assertEqual(by_type[BrokerLedger.REV_COUNTERPARTY_PNL], Decimal("-8.00"))
        broker_net = sum(by_type.values())
        self.assertEqual(broker_net, Decimal("-2.00"))
        # Each type recorded exactly once — no duplication.
        self.assertEqual(len(rows), 3)

    def test_caso_d_loss_with_commission_and_spread_broker_net(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=92.0, realized_pnl=-8.0, new_balance=99987.0, new_equity=99987.0)

        by_type = {
            row.revenue_type: row.amount
            for row in BrokerLedger.objects.filter(source_account=account)
        }
        self.assertEqual(by_type[BrokerLedger.REV_COMMISSION], Decimal("5.00"))
        self.assertEqual(by_type[BrokerLedger.REV_SPREAD], Decimal("1.00"))
        self.assertEqual(by_type[BrokerLedger.REV_COUNTERPARTY_PNL], Decimal("8.00"))
        self.assertEqual(sum(by_type.values()), Decimal("14.00"))

    def test_caso_e_gambler_classification_no_effect(self):
        # Baseline: unclassified account.
        acc_plain = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c1 = _consumer(acc_plain.id)
        c1.account["spread_pips"] = 2.0
        r1 = _open(c1, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem1 = _pos_mem(r1, "BTCUSD", "buy", 1.0, 100.0)
        _close(c1, pos_mem1, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)
        plain_amounts = sorted(
            float(v) for v in BrokerLedger.objects.filter(source_account=acc_plain).values_list("amount", flat=True)
        )

        # Same operation, GAMBLER-classified account.
        acc_gambler = make_account(account_type="STANDARD", balance=Decimal("100000"))
        TraderScore.objects.create(
            account=acc_gambler, trader_class=TraderScore.GAMBLER,
            routing_profile=TraderScore.ROUTING_INTERNAL,
        )
        c2 = _consumer(acc_gambler.id)
        c2.account["spread_pips"] = 2.0
        r2 = _open(c2, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem2 = _pos_mem(r2, "BTCUSD", "buy", 1.0, 100.0)
        _close(c2, pos_mem2, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)
        gambler_amounts = sorted(
            float(v) for v in BrokerLedger.objects.filter(source_account=acc_gambler).values_list("amount", flat=True)
        )

        self.assertEqual(plain_amounts, gambler_amounts)
        gambler_cp = BrokerLedger.objects.get(source_account=acc_gambler, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(gambler_cp.meta["book_mode"], "B_BOOK")

    def test_caso_f_concurrent_double_close_writes_exactly_one_of_each(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)

        # First close — genuinely closes the Position.
        result1 = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)
        self.assertNotIn("already_closed", result1)

        # Second close attempt for the SAME Position — mirrors a concurrent
        # panel/daemon racing the same close. The DB-locked atomic path
        # (PANEL-02/03) finds the Position already gone and returns
        # already_closed=True WITHOUT creating a new Trade — so there is
        # nothing for the counterparty helper to attach to a second time.
        result2 = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)
        self.assertTrue(result2.get("already_closed"))

        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 1)
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(),
            1,
        )


# ─────────────────────────────────────────────────────────────────────────
# 3. Daemon canonical close path — tasks._close_position_sync
# ─────────────────────────────────────────────────────────────────────────
class DaemonCloseCounterpartyIntegrationTests(TransactionTestCase):
    def _open_via_ws(self, account):
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        return _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)

    def test_daemon_close_trader_plus_10(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos_mem = self._open_via_ws(account)

        sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=110.0, reason="stopout_daemon",
            realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0,
        )

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10.00"))
        self.assertEqual(cp.source_trade_id, trade.id)
        self.assertEqual(cp.meta["close_reason"], "stopout_daemon")

    def test_daemon_close_trader_minus_10(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos_mem = self._open_via_ws(account)

        sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=90.0, reason="stopout_daemon",
            realized_pnl=-10.0, new_balance=99990.0, new_equity=99990.0,
        )

        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("10.00"))

    def test_daemon_close_breakeven_creates_zero_amount_row(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos_mem = self._open_via_ws(account)

        sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=100.0, reason="stopout_daemon",
            realized_pnl=0.0, new_balance=100000.0, new_equity=100000.0,
        )

        trade = Trade.objects.get(account=account)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("0.00"))
        self.assertEqual(cp.source_trade_id, trade.id)

    def test_daemon_double_close_no_duplicate(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos_mem = self._open_via_ws(account)

        r1 = sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=110.0, reason="stopout_daemon",
            realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0,
        )
        r2 = sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=110.0, reason="stopout_daemon",
            realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0,
        )
        self.assertFalse(r1.get("already_closed"))
        self.assertTrue(r2.get("already_closed"))
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(),
            1,
        )


# ─────────────────────────────────────────────────────────────────────────
# 4. Admin force-close path
# ─────────────────────────────────────────────────────────────────────────
class AdminForceCloseCounterpartyIntegrationTests(TestCase):
    def setUp(self):
        self.superuser = make_user(username="book02_admin", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_force_close_writes_counterparty_entry(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10"))
        self.assertEqual(cp.meta["close_reason"], "admin_force_close")
        self.assertEqual(cp.source_trade_id, trade.id)

    def test_force_close_breakeven_writes_zero_amount_counterparty_row(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        # No price posted -> exit_px falls back to avg_price -> pnl == 0.
        resp = self.client.post(url, {"action": "force_close", "symbol": "BTCUSD"})
        self.assertEqual(resp.status_code, 302)
        trade = Trade.objects.get(account=account)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("0.00"))
        self.assertEqual(cp.source_trade_id, trade.id)
        self.assertEqual(cp.meta["close_reason"], "admin_force_close")


# ─────────────────────────────────────────────────────────────────────────
# 5. population_engine simulated closes
# ─────────────────────────────────────────────────────────────────────────
class PopulationEngineCounterpartyIntegrationTests(TestCase):
    def test_simulated_close_writes_counterparty_entry(self):
        from simulator.population_engine import SimulatedTrader

        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos = Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )

        trader = SimulatedTrader(account_id=account.id, profile_name="NORMAL")
        with patch.object(trader, "_simulate_pnl", return_value=(110.0, 10.0)):
            trader._close_position(account, pos)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10"))
        self.assertEqual(cp.meta["close_reason"], "population_sim")
        self.assertEqual(cp.source_trade_id, trade.id)

    def test_simulated_close_breakeven_writes_zero_amount_entry(self):
        from simulator.population_engine import SimulatedTrader

        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos = Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )

        trader = SimulatedTrader(account_id=account.id, profile_name="NORMAL")
        with patch.object(trader, "_simulate_pnl", return_value=(100.0, 0.0)):
            trader._close_position(account, pos)

        trade = Trade.objects.get(account=account)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("0.00"))
        self.assertEqual(cp.source_trade_id, trade.id)


# ─────────────────────────────────────────────────────────────────────────
# 6. No regression — commission/spread/trader formulas unaffected
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionStructuralTests(TransactionTestCase):
    def test_commission_and_spread_still_recorded_once_each_at_open_only(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)

        # Immediately after OPEN — no COUNTERPARTY_PNL row can exist yet
        # (no Trade/close has happened), commission+spread must already
        # be there (unchanged pre-BOOK-02 behavior).
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_SPREAD).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(), 0,
        )

        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        # After close — still exactly one of each, no duplication from the
        # new close-time write.
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COMMISSION).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_SPREAD).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).count(), 1,
        )

    def test_commission_and_spread_entries_have_no_source_trade_link(self):
        # BOOK-01 finding, unchanged by BOOK-02: commission/spread are
        # written at OPEN time, before any Trade exists — this block does
        # not attempt to retroactively link them (see broker_ledger.py docstring).
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        for row in BrokerLedger.objects.filter(
            source_account=account,
            revenue_type__in=[BrokerLedger.REV_COMMISSION, BrokerLedger.REV_SPREAD],
        ):
            self.assertIsNone(row.source_trade_id)

    def test_trade_and_ledgerentry_amounts_unaffected_by_book02(self):
        # The trader-facing numbers must be byte-identical to what BOOK-01's
        # reproduction observed before this block existed.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("100010.00"))
        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        ledger = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_REALIZED)
        self.assertEqual(ledger.amount, Decimal("10.00"))

    def test_revenue_snapshot_task_excludes_counterparty_pnl(self):
        # BOOK-02 introduces a row type that can be negative into a table
        # BrokerRevenueSnapshot.total_revenue sums unfiltered
        # (tasks.take_revenue_snapshot_task). Without the exclude added in
        # that task, a counterparty entry would silently corrupt this
        # persisted, cumulative "revenue" figure. Verify commission+spread
        # alone reach the snapshot, and the counterparty amount does not.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        # Sanity: the counterparty row exists and IS negative (would corrupt
        # total_revenue if it leaked in).
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-8.00"))

        sim_tasks.take_revenue_snapshot_task.apply().get()
        from simulator.models import BrokerRevenueSnapshot
        snap = BrokerRevenueSnapshot.objects.latest("taken_at")
        # 5 (commission) + 1 (spread) = 6 — NOT 6 + (-8) = -2.
        self.assertEqual(snap.total_revenue, Decimal("6.00"))
        self.assertEqual(snap.total_commission, Decimal("5.00"))
        self.assertEqual(snap.total_spread, Decimal("1.00"))

    def test_revenue_dashboard_and_snapshot_unchanged_by_zero_amount_counterparty_row(self):
        # A break-even COUNTERPARTY_PNL row (amount=0.00) must not change
        # the revenue dashboard/snapshot totals either — it's excluded by
        # revenue_type, same as any non-zero counterparty row; the zero
        # amount itself is irrelevant to that exclusion.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=100.0, realized_pnl=0.0, new_balance=99995.0, new_equity=99995.0)

        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("0.00"))

        sim_tasks.take_revenue_snapshot_task.apply().get()
        from simulator.models import BrokerRevenueSnapshot
        snap = BrokerRevenueSnapshot.objects.latest("taken_at")
        # Still exactly commission(5) + spread(1) = 6, unaffected by the
        # zero-amount counterparty row existing in the same table.
        self.assertEqual(snap.total_revenue, Decimal("6.00"))

        # Same invariant on the admin "Broker Revenue Dashboard" queryset
        # exclude (admin.py::revenue_dashboard_view) — spot-check directly.
        from simulator.models import BrokerLedger as BL
        dashboard_qs = BL.objects.exclude(revenue_type=BL.REV_COUNTERPARTY_PNL).filter(source_account=account)
        self.assertEqual(dashboard_qs.count(), 2)  # commission + spread only
        self.assertEqual(sum(r.amount for r in dashboard_qs), Decimal("6.00"))

    def test_historical_trade_without_close_helper_gets_no_backfill(self):
        # BOOK-02 covers new closes only. A Trade created directly (as any
        # pre-BOOK-02 historical row was) must NOT spontaneously get a
        # COUNTERPARTY_PNL entry — there is no signal/hook that reaches
        # backward. No automatic historical PnL is invented.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.00"), entry_price=Decimal("100"),
            exit_price=Decimal("110"), profit_loss=Decimal("10.00"),
        )
        self.assertFalse(
            BrokerLedger.objects.filter(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).exists()
        )
