"""
BOOK-04c — Trade Routing Persistence tests.

Covers the three real, authoritative close writers (verified in the
pre-implementation review — each calls create_broker_counterparty_entry()
and shares the same flow: Trade -> LedgerEntry -> BrokerLedger -> Position.delete()):

  - simulator/consumers.py::_db_close_position_atomic()  (WS manual close)
  - simulator/tasks.py::_close_position_sync()            (Celery daemon)
  - simulator/admin.py force_close                        (dealing desk)

simulator/consumers.py::_db_mirror_close_position is explicitly out of
scope — confirmed dead code (zero call sites anywhere in the codebase,
per the "Dead code exclusion" comment in consumers.py's own module-level
LOCK ORDER note) — not touched, not tested here.

Reuses the established BOOK-02 test helpers (_open/_close/_pos_mem,
_consumer) rather than re-inventing them.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from market_data.feeds import get_feed_manager
from simulator import tasks as sim_tasks
from simulator.broker_ledger import BOOK_MODE_B_BOOK, _book_mode_for_trade, create_broker_counterparty_entry
from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, RoutingDecision, Trade, TradingAccount
from simulator.routing_engine import Book

from .factories import make_account, make_user
from .test_broker_counterparty_pnl import _close, _open, _pos_mem
from .test_order_ticket_sl_tp_validation import _consumer

User = get_user_model()


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


# ─────────────────────────────────────────────────────────────────────────
# 1. WS close path (consumers.py::_db_close_position_atomic) — flag on
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class WsCloseRoutingPersistenceTests(TestCase):

    def test_close_copies_principal_decision_to_trade(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos = Position.objects.get(pk=r["position_id"])
        self.assertIsNotNone(pos.routing_decision_id)   # BOOK-04b already linked it

        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        close_result = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0,
                               new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(pk=close_result["trade_id"])
        self.assertEqual(trade.routing_decision_id, pos.routing_decision_id)
        self.assertIsNotNone(trade.routing_decision)
        self.assertEqual(trade.routing_decision.book, Book.INTERNAL)

    def test_close_legacy_position_null_decision_produces_null_trade(self):
        """Position created directly (bypassing _db_open_position_atomic,
        the honest legacy/backfill case) has routing_decision=NULL — the
        resulting Trade must also be NULL, never fabricated."""
        account = make_account(balance=Decimal("100000"))
        pos = Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        self.assertIsNone(pos.routing_decision_id)

        c = _consumer(account.id)
        pos_mem = {"id": pos.id, "symbol": "BTCUSD", "side": "buy", "qty": 1.0,
                   "avg": 100.0, "sl": None, "tp": None, "opened_at": int(time.time())}
        close_result = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0,
                               new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(pk=close_result["trade_id"])
        self.assertIsNone(trade.routing_decision_id)
        self.assertEqual(RoutingDecision.objects.count(), 0)   # nothing fabricated

    def test_book_mode_derived_via_centralized_translator_not_literal_internal(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        close_result = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0,
                               new_balance=100010.0, new_equity=100010.0)

        cp = BrokerLedger.objects.get(
            source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
        )
        self.assertEqual(cp.meta["book_mode"], "B_BOOK")
        self.assertNotEqual(cp.meta["book_mode"], "INTERNAL")


# ─────────────────────────────────────────────────────────────────────────
# 2. Netting — several RoutingDecision on one Position
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class NettingRoutingPersistenceTests(TestCase):

    def setUp(self):
        _seed_price("EUR/USD", 1.0800)
        self.addCleanup(_clear_price, "EUR/USD")

    def test_trade_gets_only_principal_never_last_increment(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id, netting_mode=True)

        r1 = _open(c, symbol="EUR/USD", side="buy", qty=1.0, price=1.0800, commission=0.0)
        pos = Position.objects.get(pk=r1["position_id"])
        principal_id = pos.routing_decision_id
        self.assertIsNotNone(principal_id)

        r2 = _open(c, symbol="EUR/USD", side="buy", qty=1.0, price=1.0900, commission=0.0)
        self.assertTrue(r2["merged"])
        r3 = _open(c, symbol="EUR/USD", side="buy", qty=1.0, price=1.1000, commission=0.0)
        self.assertTrue(r3["merged"])

        self.assertEqual(RoutingDecision.objects.filter(position=pos).count(), 3)
        pos.refresh_from_db()
        self.assertEqual(pos.routing_decision_id, principal_id)   # unchanged by merges

        pos_mem = _pos_mem(r1, "EUR/USD", "buy", 3.0, 1.09)
        pos_mem["id"] = pos.id
        close_result = _close(c, pos_mem, close_px=1.1000, realized_pnl=60.0,
                               new_balance=100060.0, new_equity=100060.0)

        trade = Trade.objects.get(pk=close_result["trade_id"])
        self.assertEqual(trade.routing_decision_id, principal_id)
        self.assertNotEqual(trade.routing_decision_id, r2["routing_decision_id"])
        self.assertNotEqual(trade.routing_decision_id, r3["routing_decision_id"])

    def test_additional_decisions_survive_with_position_null_after_close(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id, netting_mode=True)

        r1 = _open(c, symbol="EUR/USD", side="buy", qty=1.0, price=1.0800, commission=0.0)
        pos = Position.objects.get(pk=r1["position_id"])
        r2 = _open(c, symbol="EUR/USD", side="buy", qty=1.0, price=1.0900, commission=0.0)

        all_decision_ids = list(
            RoutingDecision.objects.filter(position=pos).values_list("id", flat=True)
        )
        self.assertEqual(len(all_decision_ids), 2)

        pos_mem = _pos_mem(r1, "EUR/USD", "buy", 2.0, 1.085)
        pos_mem["id"] = pos.id
        _close(c, pos_mem, close_px=1.0900, realized_pnl=20.0,
               new_balance=100020.0, new_equity=100020.0)

        self.assertFalse(Position.objects.filter(pk=pos.id).exists())
        surviving = RoutingDecision.objects.filter(pk__in=all_decision_ids)
        self.assertEqual(surviving.count(), 2)   # nothing deleted
        for d in surviving:
            self.assertIsNone(d.position_id)      # SET_NULL fired for all of them


# ─────────────────────────────────────────────────────────────────────────
# 3. Rollback — a failure after Trade creation must undo everything
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class RollbackIntegrityTests(TestCase):

    def test_later_failure_rolls_back_trade_and_position_together(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos = Position.objects.get(pk=r["position_id"])
        principal_id = pos.routing_decision_id

        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        with patch(
            "simulator.consumers.LedgerEntry.objects.create",
            side_effect=RuntimeError("simulated post-Trade failure"),
        ):
            with self.assertRaises(RuntimeError):
                _close(c, pos_mem, close_px=110.0, realized_pnl=10.0,
                       new_balance=100010.0, new_equity=100010.0)

        # Nothing committed: Position still open, no new Trade, principal
        # RoutingDecision still linked exactly as before the attempt.
        self.assertTrue(Position.objects.filter(pk=pos.id).exists())
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)
        pos.refresh_from_db()
        self.assertEqual(pos.routing_decision_id, principal_id)
        decision = RoutingDecision.objects.get(pk=principal_id)
        self.assertEqual(decision.position_id, pos.id)


# ─────────────────────────────────────────────────────────────────────────
# 4. Daemon close path (tasks.py::_close_position_sync)
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class DaemonCloseRoutingPersistenceTests(TransactionTestCase):

    def test_daemon_close_copies_principal_decision(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos = Position.objects.get(pk=r["position_id"])
        principal_id = pos.routing_decision_id
        self.assertIsNotNone(principal_id)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)

        sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=110.0, reason="stopout_daemon",
            realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0,
        )

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.routing_decision_id, principal_id)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.meta["book_mode"], "B_BOOK")

    def test_daemon_close_legacy_null_stays_null(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        pos = Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        pos_mem = {"id": pos.id, "symbol": "BTCUSD", "side": "buy", "qty": 1.0,
                   "avg": 100.0, "sl": None, "tp": None, "opened_at": int(time.time())}

        sim_tasks._close_position_sync(
            pos_mem, account.id, close_px=110.0, reason="stopout_daemon",
            realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0,
        )

        trade = Trade.objects.get(account=account)
        self.assertIsNone(trade.routing_decision_id)


# ─────────────────────────────────────────────────────────────────────────
# 5. Admin force-close path
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class AdminForceCloseRoutingPersistenceTests(TestCase):
    def setUp(self):
        self.superuser = make_user(username="book04c_admin", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_force_close_copies_principal_decision(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos = Position.objects.get(pk=r["position_id"])
        principal_id = pos.routing_decision_id
        self.assertIsNotNone(principal_id)

        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.routing_decision_id, principal_id)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.meta["book_mode"], "B_BOOK")

    def test_force_close_legacy_null_stays_null(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=account)
        self.assertIsNone(trade.routing_decision_id)


# ─────────────────────────────────────────────────────────────────────────
# 6. Flag off — no behavior change at all (regression guard)
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=False)
class FlagOffNoRegressionTests(TestCase):

    def test_close_with_flag_off_produces_null_trade_and_default_book_mode(self):
        account = make_account(balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos = Position.objects.get(pk=r["position_id"])
        self.assertIsNone(pos.routing_decision_id)   # flag off -> BOOK-04b never linked one

        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        close_result = _close(c, pos_mem, close_px=110.0, realized_pnl=10.0,
                               new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(pk=close_result["trade_id"])
        self.assertIsNone(trade.routing_decision_id)
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.meta["book_mode"], "B_BOOK")   # unchanged pre-BOOK-04c default


# ─────────────────────────────────────────────────────────────────────────
# 7. _book_mode_for_trade() — the centralized translator, in isolation
# ─────────────────────────────────────────────────────────────────────────
class BookModeTranslatorUnitTests(TestCase):

    def test_no_routing_decision_returns_default(self):
        account = make_account(balance=Decimal("100000"))
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.0"), entry_price=Decimal("100"), exit_price=Decimal("110"),
            profit_loss=Decimal("10"),
        )
        self.assertIsNone(trade.routing_decision_id)
        self.assertEqual(_book_mode_for_trade(trade), BOOK_MODE_B_BOOK)

    def test_internal_book_maps_to_b_book(self):
        account = make_account(balance=Decimal("100000"))
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X")
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.0"), entry_price=Decimal("100"), exit_price=Decimal("110"),
            profit_loss=Decimal("10"), routing_decision=decision,
        )
        self.assertEqual(_book_mode_for_trade(trade), BOOK_MODE_B_BOOK)

    def test_explicit_book_mode_override_is_not_replaced(self):
        """create_broker_counterparty_entry() must still honor an explicit
        caller-supplied book_mode — the derivation only fills in when the
        caller passes nothing (book_mode=None)."""
        account = make_account(balance=Decimal("100000"))
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X")
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.0"), entry_price=Decimal("100"), exit_price=Decimal("110"),
            profit_loss=Decimal("10"), routing_decision=decision,
        )
        entry = create_broker_counterparty_entry(
            trade, account, Decimal("10"), "manual", book_mode="CUSTOM_OVERRIDE",
        )
        self.assertEqual(entry.meta["book_mode"], "CUSTOM_OVERRIDE")
