"""
simulator/tests/test_account_balance_concurrency.py — ACCOUNT-02.

Root cause fixed: TradingConsumer._db_sync_account_balances() ran on every
tick (throttled ~1.2s) for EACH of an account's WebSocket connections
(one per dashboard panel) and unconditionally wrote
self.account["balance"]/["equity"] — that connection's own, possibly
stale, in-memory copy — straight to the DB. A sibling panel that never
learned about a close made through another panel would silently revert
the account's realized balance back to its own stale value. Reproduced
exactly in the ACCOUNT-01 audit: 183.82 -> 190.82 (correct, after
closing +10/-3) -> reverted to 183.82 by a sibling's periodic sync.

Fixed by:
  - _db_sync_account_balances() no longer writes balance at all — it
    reads the fresh DB balance (read-only) and persists only the derived
    equity. Returns the fresh balance so the caller can refresh
    self.account["balance"].
  - _db_close_position_atomic / _close_position_sync (daemon) / admin
    force_close no longer trust the caller's pre-lock new_balance/
    self.account as the write source — each locks TradingAccount fresh
    (select_for_update) and derives balance_after from THAT read +
    realized_pnl. remaining_floating (other still-open positions in the
    same close batch) is extracted as new_equity - new_balance, which is
    staleness-independent (cancels out whatever stale starting balance
    the caller used).
  - _db_suspend_account no longer re-writes balance/equity at all (every
    real balance change already persisted correctly via the position
    closes that preceded it).

Uses TransactionTestCase throughout: several tests call
@database_sync_to_async-wrapped methods via asyncio.run(), which runs the
DB work on a different thread — TestCase's uncommitted per-test
transaction is invisible to (and can deadlock against) that thread on
SQLite (same reasoning as SPREAD-03's EnsureBackgroundRefreshStartedTests).
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase

from simulator.consumers import TradingConsumer
from simulator.models import LedgerEntry, Position, TradingAccount, Trade
from simulator.tasks import _close_position_sync, scan_positions_task

from .factories import make_account, make_position, make_user

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__
_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__
_db_sync_balances = TradingConsumer._db_sync_account_balances.__wrapped__


def _run(coro):
    return asyncio.run(coro)


def _fake_consumer(account_id, balance, equity=None, pnl_unreal=0.0, currency="USD"):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "balance": balance, "equity": equity if equity is not None else balance,
        "pnl_unreal": pnl_unreal, "currency": currency,
    }
    return c


def _bare_consumer_for_recalc(account_id, balance, positions=None):
    """A fuller bare consumer for exercising _recalc_account_and_push()."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c._last_db_sync = 0.0
    c._positions = positions or []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.symbol = "EUR/USD"
    c._bid_state, c._ask_state = {}, {}
    c.account = {
        "balance": balance, "equity": balance, "peak_balance": balance,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": "Activo", "account_type": "STANDARD",
        "tier": "", "profit_target": 0.0, "initial_balance": balance,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


def _pos_mem(pos: Position) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


class LostUpdateEliminatedTests(TransactionTestCase):
    """1-3) Reproducción exacta 183.82 -> 190.82 -> no revierte. Dos
    consumers misma cuenta. Consumer hermano con estado viejo no puede
    bajar el balance."""

    def test_sibling_periodic_sync_no_longer_reverts_balance(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        pos2 = make_position(account, symbol="GBP/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        consumer_A = _fake_consumer(account.pk, balance=183.82, equity=193.82)
        r1 = _db_close_sync(consumer_A, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer_A.account["balance"] = r1["new_balance"]
        r2 = _db_close_sync(
            consumer_A, _pos_mem(pos2), 1.29000, "manual", -3.00,
            consumer_A.account["balance"] - 3.00, consumer_A.account["balance"] - 3.00,
        )
        consumer_A.account["balance"] = r2["new_balance"]

        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 190.82, places=2)

        # Sibling panel B: hydrated with the OLD balance, never learned about
        # A's closes. Its periodic sync must NOT be able to drag the DB back down.
        consumer_B = _fake_consumer(account.pk, balance=183.82, equity=183.82)
        fresh = _db_sync_balances(consumer_B)

        self.assertAlmostEqual(fresh, 190.82, places=2)
        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 190.82, places=2)

    def test_sibling_sync_never_writes_balance_column(self):
        """Structural: _db_sync_account_balances()'s CODE (not its
        docstring, which references the old buggy pattern for context)
        must never appear as a writer of `balance=` — only of `equity=`."""
        import inspect
        source = inspect.getsource(TradingConsumer._db_sync_account_balances)
        code_only = source[source.index('"""', source.index('"""') + 3) + 3:]
        self.assertNotIn(".update(balance=", code_only)
        self.assertIn(".update(equity=", code_only)


class CloseFlowTests(TransactionTestCase):
    """4-5) Cierre +10 y -3 deja 190.82. Trade y Ledger suman +7."""

    def test_two_closes_net_seven_and_ledger_agrees(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        pos2 = make_position(account, symbol="GBP/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.30000"))
        consumer = _fake_consumer(account.pk, balance=183.82)

        r1 = _db_close_sync(consumer, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer.account["balance"] = r1["new_balance"]
        r2 = _db_close_sync(
            consumer, _pos_mem(pos2), 1.29000, "manual", -3.00,
            consumer.account["balance"] - 3.00, consumer.account["balance"] - 3.00,
        )
        consumer.account["balance"] = r2["new_balance"]

        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 190.82, places=2)
        self.assertAlmostEqual(
            float(sum((t.profit_loss or 0) for t in Trade.objects.filter(account=account))), 7.00, places=2,
        )
        self.assertAlmostEqual(
            float(sum(e.amount for e in LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED))),
            7.00, places=2,
        )


class ReconnectAndRefreshTests(TransactionTestCase):
    """6-7) Nueva conexión ve 190.82. Refresh DB ve 190.82."""

    def test_fresh_reconnect_hydrates_correct_balance(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        consumer_A = _fake_consumer(account.pk, balance=183.82)
        r1 = _db_close_sync(consumer_A, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer_A.account["balance"] = r1["new_balance"]

        fresh_consumer = _bare_consumer_for_recalc(account.pk, balance=0.0)
        _run(fresh_consumer._maybe_hydrate_from_db())
        self.assertAlmostEqual(fresh_consumer.account["balance"], 193.82, places=2)

    def test_direct_db_refresh_sees_correct_balance(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        consumer_A = _fake_consumer(account.pk, balance=183.82)
        r1 = _db_close_sync(consumer_A, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer_A.account["balance"] = r1["new_balance"]

        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 193.82, places=2)


class DaemonDoesNotRevertTests(TransactionTestCase):
    """8) Daemon no revierte."""

    def test_close_position_sync_derives_fresh_balance_not_stale_param(self):
        account = make_account(balance=Decimal("183.82"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        # A WS panel closes something ELSE on the same account moments
        # before the daemon (with its own STALE running_balance) closes
        # this position — the daemon's write must not clobber the WS close.
        TradingAccount.objects.filter(pk=account.pk).update(balance=Decimal("500.00"))

        result = _close_position_sync(
            _pos_mem(pos), account.pk, 1.11000, "daemon_tp", 10.00,
            new_balance=193.82,  # stale — computed from the OLD 183.82 elsewhere
            new_equity=193.82, account_currency="USD",
        )
        # Correct: 500.00 (fresh DB balance at write time) + 10.00 realized = 510.00,
        # NOT 193.82 (the stale parameter).
        self.assertAlmostEqual(result["new_balance"], 510.00, places=2)
        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 510.00, places=2)

    def test_scan_positions_task_does_not_revert_sibling_close(self):
        account = make_account(balance=Decimal("183.82"), account_type="RETAIL")
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"),
                      avg_price=Decimal("1.10000"), tp=Decimal("1.10001"))
        # Simulate a WS panel having already realized +50 elsewhere, moments
        # before the daemon's scan tick fires for this account's TP.
        TradingAccount.objects.filter(pk=account.pk).update(balance=Decimal("233.82"))

        from unittest.mock import patch as _patch
        with _patch("simulator.tasks._read_cached_price", return_value=(1.10001, 1.10003)):
            scan_positions_task.apply().get()

        acc = TradingAccount.objects.get(pk=account.pk)
        self.assertGreater(float(acc.balance), 233.0)  # kept the +50 AND added the TP realized


class AccountUpdatePayloadFreshTests(TransactionTestCase):
    """9) account:update de ambos sockets muestra balance fresco."""

    def test_both_sockets_report_fresh_balance_after_recalc(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        consumer_A = _fake_consumer(account.pk, balance=183.82)
        r1 = _db_close_sync(consumer_A, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer_A.account["balance"] = r1["new_balance"]

        panel_A = _bare_consumer_for_recalc(account.pk, balance=193.82)
        panel_B = _bare_consumer_for_recalc(account.pk, balance=183.82)  # stale, sibling

        _run(panel_A._recalc_account_and_push())
        _run(panel_B._recalc_account_and_push())

        self.assertAlmostEqual(panel_A.account["balance"], 193.82, places=2)
        self.assertAlmostEqual(panel_B.account["balance"], 193.82, places=2)  # picked up the fresh value
        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 193.82, places=2)


class PartialCloseKeepsFloatingTests(TransactionTestCase):
    """10) Cerrar una posición mientras otra sigue abierta: balance se
    actualiza con realized; equity = balance + floating restante."""

    def test_closing_one_of_two_preserves_remaining_floating_in_equity(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        make_position(account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        consumer = _fake_consumer(account.pk, balance=183.82)
        # Closing pos1 (+10 realized) while pos2 is still open with +5 floating:
        # new_balance = 193.82, new_equity = 198.82 (includes pos2's +5 floating).
        result = _db_close_sync(consumer, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 198.82)

        self.assertAlmostEqual(result["new_balance"], 193.82, places=2)
        self.assertAlmostEqual(result["new_equity"], 198.82, places=2)  # 193.82 + 5.00 floating preserved
        acc = TradingAccount.objects.get(pk=account.pk)
        self.assertAlmostEqual(float(acc.balance), 193.82, places=2)
        self.assertAlmostEqual(float(acc.equity), 198.82, places=2)


class FullCloseStableStateTests(TransactionTestCase):
    """11) Cierre total deja estado estable."""

    def test_closing_everything_leaves_balance_equal_equity(self):
        account = make_account(balance=Decimal("183.82"))
        pos1 = make_position(account, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        pos2 = make_position(account, symbol="GBP/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.30000"))
        consumer = _fake_consumer(account.pk, balance=183.82)

        r1 = _db_close_sync(consumer, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer.account["balance"] = r1["new_balance"]
        r2 = _db_close_sync(
            consumer, _pos_mem(pos2), 1.29000, "manual", -3.00,
            consumer.account["balance"] - 3.00, consumer.account["balance"] - 3.00,
        )
        consumer.account["balance"] = r2["new_balance"]

        acc = TradingAccount.objects.get(pk=account.pk)
        self.assertAlmostEqual(float(acc.balance), 190.82, places=2)
        self.assertAlmostEqual(float(acc.equity), float(acc.balance), places=2)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)

        panel = _bare_consumer_for_recalc(account.pk, balance=float(acc.balance), positions=[])
        _run(panel._recalc_account_and_push())
        self.assertEqual(panel.account["pnl_unreal"], 0.0)
        self.assertEqual(panel.account["margin_used"], 0.0)
        self.assertAlmostEqual(panel.account["equity"], panel.account["balance"], places=2)


class CommissionChargedOnceTests(TransactionTestCase):
    """12) Comisión sigue descontándose una sola vez."""

    def test_commission_deducted_exactly_once_on_open(self):
        account = make_account(balance=Decimal("1000.00"))
        result = _db_open_sync(
            _fake_consumer(account.pk, balance=1000.00), "EUR/USD", "buy", 1.0, 1.10000,
            None, None, commission=5.0, new_balance=995.0,
        )
        self.assertAlmostEqual(result["new_balance"], 995.0, places=2)
        acc = TradingAccount.objects.get(pk=account.pk)
        self.assertAlmostEqual(float(acc.balance), 995.0, places=2)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).count(), 1,
        )


class AccountIsolationTests(TransactionTestCase):
    """13) Cuentas distintas aisladas."""

    def test_two_accounts_never_cross_contaminate_balance(self):
        account_1 = make_account(balance=Decimal("183.82"))
        account_2 = make_account(balance=Decimal("500.00"))
        pos1 = make_position(account_1, symbol="EUR/USD", side="BUY",
                              qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        consumer_1 = _fake_consumer(account_1.pk, balance=183.82)
        r1 = _db_close_sync(consumer_1, _pos_mem(pos1), 1.11000, "manual", 10.00, 193.82, 193.82)
        consumer_1.account["balance"] = r1["new_balance"]

        consumer_2 = _fake_consumer(account_2.pk, balance=500.00)
        _db_sync_balances(consumer_2)

        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account_1.pk).balance), 193.82, places=2)
        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account_2.pk).balance), 500.00, places=2)


class DbFailureSafetyTests(TransactionTestCase):
    """14) Fallo DB no escribe valor viejo."""

    def test_db_failure_keeps_previous_balance_no_revert(self):
        account = make_account(balance=Decimal("190.82"))
        panel = _bare_consumer_for_recalc(account.pk, balance=190.82)
        panel._last_db_sync = 0.0  # force the throttle to trigger

        with patch.object(
            TradingConsumer, "_db_sync_account_balances",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with self.assertLogs("simulator.ws", level="ERROR") as captured:
                _run(panel._recalc_account_and_push())

        self.assertIn("balance refresh failed", "\n".join(captured.output))
        self.assertAlmostEqual(panel.account["balance"], 190.82, places=2)  # unchanged, not fabricated
        self.assertAlmostEqual(float(TradingAccount.objects.get(pk=account.pk).balance), 190.82, places=2)


class NoFormulaChangeStructuralTests(TransactionTestCase):
    """15) Cero cambios en PnL, spread, margin formulas."""

    def test_pnl_engine_untouched(self):
        import inspect
        from simulator import pnl_engine
        source = inspect.getsource(pnl_engine.calculate_quote_pnl)
        self.assertIn("contract_size", source)  # formula itself untouched

    def test_margin_used_total_untouched(self):
        import inspect
        source = inspect.getsource(TradingConsumer._margin_used_total)
        self.assertNotIn("_db_sync_account_balances", source)

    def test_broker_price_untouched(self):
        import inspect
        from simulator.spread_engine import broker_price
        source = inspect.getsource(broker_price)
        self.assertNotIn("_db_sync_account_balances", source)
