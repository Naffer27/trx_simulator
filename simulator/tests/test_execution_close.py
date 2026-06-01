"""
simulator/tests/test_execution_close.py — Bloque 6

Cubre: _close_position_sync (simulator/tasks.py) — cierre atómico de posiciones.

Garantías que esta función debe cumplir:
  1. Crea un Trade con los campos correctos.
  2. Crea un LedgerEntry EV_REALIZED con amount=realized_pnl.
  3. Elimina la Position de la DB.
  4. Actualiza TradingAccount.balance al new_balance recibido.
  5. LedgerEntry.balance_after == TradingAccount.balance (invariante de ledger).
  6. El resultado contiene trade_id y already_closed=False.
  7. BUY profit  → Trade.profit_loss > 0
  8. BUY loss    → Trade.profit_loss < 0
  9. SELL profit → Trade.profit_loss > 0
  10. SELL loss  → Trade.profit_loss < 0
  11. Segunda llamada con posición ya eliminada → already_closed=True, sin Trade ni LedgerEntry extra.
  12. Drawdown ≥ max_drawdown_pct → check_and_enforce_risk dispara, cuenta Suspendida.

Convenciones:
  - _close_position_sync recibe pos_mem (dict) con los datos en memoria de la posición;
    se construye con el helper _pos_mem() definido aquí.
  - realized_pnl y new_balance se calculan fuera de la función (como en producción el daemon).
  - Los amounts en Decimal, float solo donde la API lo exige (close_px, realized_pnl, etc.).
  - Ningún test toca lógica productiva.
"""
import time
from decimal import Decimal

from django.test import TestCase

from simulator.models import LedgerEntry, Position, Trade, TradingViolation
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position


def _pos_mem(pos, opened_at: float | None = None) -> dict:
    """Build the pos_mem dict that _close_position_sync expects from a Position instance."""
    return {
        "id":        pos.pk,
        "symbol":    pos.symbol,
        "side":      pos.side,
        "qty":       float(pos.qty),
        "avg":       float(pos.avg_price),
        "sl":        float(pos.sl)  if pos.sl  is not None else None,
        "tp":        float(pos.tp)  if pos.tp  is not None else None,
        "opened_at": opened_at if opened_at is not None else time.time(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Garantías fundamentales del cierre atómico
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePositionBasic(TestCase):
    """
    Todos los tests usan el mismo escenario base:
      EUR/USD BUY 0.1 lot, entry=1.10000, close=1.11000, pnl=+100, new_balance=10100.
    """

    def setUp(self):
        self.account  = make_account(account_type="CHALLENGE", tier="10K",
                                     balance=Decimal("10000"))
        self.pos      = make_position(account=self.account, symbol="EUR/USD",
                                      side="BUY", qty=Decimal("0.1"),
                                      avg_price=Decimal("1.10000"))
        self.pos_mem  = _pos_mem(self.pos)
        self.close_px = 1.11000
        self.pnl      = 100.0
        self.new_bal  = 10100.0

    def _close(self):
        return _close_position_sync(
            pos_mem     = self.pos_mem,
            account_id  = self.account.pk,
            close_px    = self.close_px,
            reason      = "tp",
            realized_pnl= self.pnl,
            new_balance = self.new_bal,
            new_equity  = self.new_bal,
        )

    def test_trade_created_with_correct_fields(self):
        """Trade creado con symbol, trade_type, lot_size, entry_price, exit_price, profit_loss."""
        result = self._close()
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.symbol,     "EUR/USD")
        self.assertEqual(trade.trade_type, "BUY")
        self.assertEqual(trade.lot_size,   Decimal("0.1"))
        self.assertAlmostEqual(float(trade.entry_price), 1.10000, places=5)
        self.assertAlmostEqual(float(trade.exit_price),  1.11000, places=5)
        self.assertAlmostEqual(float(trade.profit_loss), 100.0,   places=2)
        self.assertIsNotNone(trade.closed_at)

    def test_ledger_entry_ev_realized_created(self):
        """LedgerEntry EV_REALIZED creada con amount=realized_pnl."""
        self._close()
        le = LedgerEntry.objects.filter(
            account=self.account, event_type=LedgerEntry.EV_REALIZED
        ).first()
        self.assertIsNotNone(le)
        self.assertAlmostEqual(float(le.amount), self.pnl, places=2)

    def test_position_deleted(self):
        """Position eliminada de la DB tras el cierre."""
        self._close()
        self.assertFalse(Position.objects.filter(pk=self.pos.pk).exists())

    def test_account_balance_updated(self):
        """TradingAccount.balance == new_balance después del cierre."""
        self._close()
        self.account.refresh_from_db()
        self.assertAlmostEqual(float(self.account.balance), self.new_bal, places=2)

    def test_ledger_balance_after_matches_account_balance(self):
        """
        LedgerEntry.balance_after == TradingAccount.balance — invariante de ledger.
        Garantiza que el ledger no se desincroniza del account.
        """
        self._close()
        le = LedgerEntry.objects.get(
            account=self.account, event_type=LedgerEntry.EV_REALIZED
        )
        self.account.refresh_from_db()
        self.assertEqual(le.balance_after, self.account.balance)

    def test_result_contains_trade_id_and_not_already_closed(self):
        """El resultado tiene already_closed=False y trade_id válido."""
        result = self._close()
        self.assertFalse(result["already_closed"])
        self.assertIsNotNone(result["trade_id"])
        self.assertIsInstance(result["trade_id"], int)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Escenarios de PnL — BUY/SELL × profit/loss
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePositionPnL(TestCase):
    """
    _close_position_sync recibe realized_pnl calculado externamente (daemon).
    Los tests verifican que Trade.profit_loss refleja exactamente lo que se pasó
    y que el signo es correcto para cada caso.
    """

    def _do_close(self, account, pos, realized_pnl: float, close_px: float = 1.10500):
        return _close_position_sync(
            pos_mem      = _pos_mem(pos),
            account_id   = account.pk,
            close_px     = close_px,
            reason       = "manual",
            realized_pnl = realized_pnl,
            new_balance  = float(account.balance) + realized_pnl,
            new_equity   = float(account.balance) + realized_pnl,
        )

    def test_buy_profit(self):
        """BUY cerrado con ganancia → Trade.profit_loss > 0."""
        account = make_account()
        pos     = make_position(account=account, side="BUY",
                                avg_price=Decimal("1.10000"))
        result  = self._do_close(account, pos, realized_pnl=50.0, close_px=1.10500)
        trade   = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.trade_type, "BUY")
        self.assertAlmostEqual(float(trade.profit_loss), 50.0,  places=2)
        self.assertGreater(float(trade.profit_loss), 0)

    def test_buy_loss(self):
        """BUY cerrado con pérdida → Trade.profit_loss < 0."""
        account = make_account()
        pos     = make_position(account=account, side="BUY",
                                avg_price=Decimal("1.10000"))
        result  = self._do_close(account, pos, realized_pnl=-50.0, close_px=1.09500)
        trade   = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.trade_type, "BUY")
        self.assertAlmostEqual(float(trade.profit_loss), -50.0, places=2)
        self.assertLess(float(trade.profit_loss), 0)

    def test_sell_profit(self):
        """SELL cerrado con ganancia → Trade.profit_loss > 0."""
        account = make_account()
        pos     = make_position(account=account, side="SELL",
                                avg_price=Decimal("1.10000"))
        result  = self._do_close(account, pos, realized_pnl=50.0, close_px=1.09500)
        trade   = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.trade_type, "SELL")
        self.assertAlmostEqual(float(trade.profit_loss), 50.0,  places=2)
        self.assertGreater(float(trade.profit_loss), 0)

    def test_sell_loss(self):
        """SELL cerrado con pérdida → Trade.profit_loss < 0."""
        account = make_account()
        pos     = make_position(account=account, side="SELL",
                                avg_price=Decimal("1.10000"))
        result  = self._do_close(account, pos, realized_pnl=-50.0, close_px=1.10500)
        trade   = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.trade_type, "SELL")
        self.assertAlmostEqual(float(trade.profit_loss), -50.0, places=2)
        self.assertLess(float(trade.profit_loss), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Idempotencia — already_closed guard
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePositionIdempotency(TestCase):
    """
    Segunda llamada con la misma posición (ya eliminada) → already_closed=True.
    No se crean Trade ni LedgerEntry adicionales.
    """

    def setUp(self):
        self.account = make_account()
        self.pos     = make_position(account=self.account, symbol="EUR/USD",
                                     side="BUY", qty=Decimal("0.1"),
                                     avg_price=Decimal("1.10000"))
        self.kwargs  = dict(
            pos_mem      = _pos_mem(self.pos),
            account_id   = self.account.pk,
            close_px     = 1.11000,
            reason       = "tp",
            realized_pnl = 100.0,
            new_balance  = 10100.0,
            new_equity   = 10100.0,
        )

    def test_second_call_returns_already_closed(self):
        """Primera llamada: already_closed=False. Segunda: already_closed=True."""
        r1 = _close_position_sync(**self.kwargs)
        r2 = _close_position_sync(**self.kwargs)
        self.assertFalse(r1["already_closed"])
        self.assertTrue(r2["already_closed"])

    def test_second_call_no_duplicate_trade(self):
        """Dos llamadas → exactamente 1 Trade en DB."""
        _close_position_sync(**self.kwargs)
        _close_position_sync(**self.kwargs)
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 1)

    def test_second_call_no_duplicate_ledger_entry(self):
        """Dos llamadas → exactamente 1 LedgerEntry EV_REALIZED en DB."""
        _close_position_sync(**self.kwargs)
        _close_position_sync(**self.kwargs)
        count = LedgerEntry.objects.filter(
            account=self.account, event_type=LedgerEntry.EV_REALIZED
        ).count()
        self.assertEqual(count, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Integración con risk engine — drawdown dispara check_and_enforce_risk
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePositionRiskCheck(TestCase):
    """
    _close_position_sync llama check_and_enforce_risk tras actualizar el balance.
    Si el drawdown supera el límite, la cuenta queda Suspendida y el resultado
    incluye el tipo de violación en result["violations"].
    """

    def test_drawdown_triggers_violation_and_suspension(self):
        """
        CHALLENGE 10K: peak=10 000, new_balance=8 800 → DD=12% ≥ 10%.
        TradingViolation MAX_DRAWDOWN creada + cuenta Suspendida.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))  # peak_balance=10000 por save()
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        result = _close_position_sync(
            pos_mem      = _pos_mem(pos),
            account_id   = account.pk,
            close_px     = 1.08800,
            reason       = "sl",
            realized_pnl = -1200.0,   # 12% de drawdown sobre peak=10000
            new_balance  = 8800.0,
            new_equity   = 8800.0,
        )

        self.assertIn("MAX_DRAWDOWN", result["violations"])
        account.refresh_from_db()
        self.assertEqual(account.status, "Suspendido")
        self.assertEqual(
            TradingViolation.objects.filter(
                account=account,
                violation_type=TradingViolation.MAX_DRAWDOWN,
            ).count(), 1
        )

    def test_retail_drawdown_no_violation(self):
        """
        RETAIL con mismo drawdown → risk engine no suspende ni crea TradingViolation.
        result["violations"] debe estar vacío.
        """
        account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        result = _close_position_sync(
            pos_mem      = _pos_mem(pos),
            account_id   = account.pk,
            close_px     = 1.08800,
            reason       = "sl",
            realized_pnl = -1200.0,
            new_balance  = 8800.0,
            new_equity   = 8800.0,
        )

        self.assertEqual(result["violations"], [])
        account.refresh_from_db()
        self.assertNotIn(account.status, {"Violado", "Suspendido"})
