"""
simulator/tests/test_netting_merge_deadlock_guard.py — Bloque C

Tests para la corrección de la race condition en _db_open_position_atomic:
  - El balance se descuenta del valor autoritativo de DB, no del stale en-memoria.
  - La comisión usa select_for_update() en TradingAccount (lock order: Position → Account).
  - El cierre ya-cerrado (already_closed) no crea Trade ni LedgerEntry duplicados.
  - El merge netting consolida posiciones del mismo lado sin duplicar filas.
"""
import time
from decimal import Decimal

from django.test import TestCase

from simulator.consumers import TradingConsumer
from simulator.models import LedgerEntry, Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync

from .factories import make_account

# Underlying sync function — bypass database_sync_to_async for direct DB testing.
_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


class _FakeConsumer:
    """Minimal consumer stub: only the attributes accessed by _db_open_position_atomic."""
    def __init__(self, account_id, netting_mode=False):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": 0.0}


class TestNettingMergeDeadlockGuard(TestCase):

    def test_open_commission_uses_authoritative_db_balance(self):
        """
        Race scenario: Celery cierra una posición y sube DB balance a 10 500,
        pero el consumer en-memoria sigue en 10 000 (stale).

        Código anterior (UPDATE ciego):
          stale_new_balance = 10 000 - 10 = 9 990
          UPDATE account SET balance = 9 990  ← borra los +500 del PnL ya aplicado

        Código nuevo (select_for_update + valor de DB):
          DB balance = 10 500, commission = 10
          _auth_balance = 10 500 - 10 = 10 490
          account.balance = 10 490  ✓
        """
        account = make_account(balance=Decimal("10000"))
        # Simular que Celery actualizó el balance en DB a 10 500 (PnL de cierre previo)
        TradingAccount.objects.filter(pk=account.pk).update(balance=Decimal("10500"))

        stale_new_balance = 9990.0  # lo que el consumer stale calcularía: 10 000 - 10
        consumer = _FakeConsumer(account.pk, netting_mode=False)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800,
            None, None, commission=10.0, new_balance=stale_new_balance,
        )

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10490"))
        self.assertAlmostEqual(result["new_balance"], 10490.0, places=4)

        # LedgerEntry EV_COMMISSION también usa el valor autoritativo
        entry = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_COMMISSION,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.balance_after, Decimal("10490"))

    def test_netting_merge_consolidates_same_side_position(self):
        """
        Netting mode: segundo BUY sobre el mismo símbolo fusiona en la posición existente.
        Una sola fila en Position con qty acumulada y avg_price ponderado.
        No se crea fila duplicada.
        """
        account = make_account(balance=Decimal("10000"))
        existing = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.0800"),
        )

        consumer = _FakeConsumer(account.pk, netting_mode=True)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900,
            None, None, commission=0.0, new_balance=10000.0,
        )

        self.assertTrue(result["merged"])
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

        existing.refresh_from_db()
        self.assertEqual(existing.qty, Decimal("2.0"))
        expected_avg = (Decimal("1.0800") + Decimal("1.0900")) / Decimal("2")
        self.assertAlmostEqual(float(existing.avg_price), float(expected_avg), places=4)

    def test_concurrent_close_creates_exactly_one_trade(self):
        """
        Dos intentos de cerrar la misma posición (consumer manual + Celery SL/TP):
        - Primer close: crea 1 Trade + 1 LedgerEntry EV_REALIZED, borra Position.
        - Segundo close: position is None → already_closed=True, sin artefactos duplicados.
        """
        account = make_account(balance=Decimal("10000"))
        pos = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.0800"),
        )
        pos_mem = {
            "id": pos.id, "symbol": "EUR/USD", "side": "BUY",
            "qty": 1.0, "avg": 1.0800, "sl": None, "tp": None,
            "opened_at": int(time.time()),
        }

        r1 = _close_position_sync(pos_mem, account.pk, 1.0900, "manual",
                                   100.0, 10100.0, 10100.0)
        self.assertFalse(r1.get("already_closed"))

        r2 = _close_position_sync(pos_mem, account.pk, 1.0900, "manual",
                                   100.0, 10100.0, 10100.0)
        self.assertTrue(r2.get("already_closed"))

        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=account, event_type=LedgerEntry.EV_REALIZED,
            ).count(),
            1,
        )

    def test_already_closed_creates_no_trade_no_ledger(self):
        """
        Si la posición no existe en DB, already_closed=True y no se crean artefactos.
        Simula el caso donde el daemon ya cerró la posición antes del intento manual.
        """
        account = make_account(balance=Decimal("10000"))
        pos_mem = {
            "id": 999999, "symbol": "EUR/USD", "side": "BUY",
            "qty": 1.0, "avg": 1.0800, "sl": None, "tp": None,
            "opened_at": int(time.time()),
        }

        result = _close_position_sync(pos_mem, account.pk, 1.0900, "manual",
                                      100.0, 10100.0, 10100.0)

        self.assertTrue(result.get("already_closed"))
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=account, event_type=LedgerEntry.EV_REALIZED,
            ).count(),
            0,
        )
