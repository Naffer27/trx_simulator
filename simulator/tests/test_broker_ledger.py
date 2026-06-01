"""
simulator/tests/test_broker_ledger.py — Bloque 4

Cubre: modelo BrokerLedger (append-only revenue ledger del broker).

Convenciones:
  - BrokerLedger no tiene lógica de negocio propia — sus invariantes son:
      1. Se crea correctamente para cada revenue_type válido.
      2. amount se almacena como Decimal (max_digits=18, decimal_places=2).
      3. source_account / source_trade / source_ledger son FK nullable (SET_NULL):
         si la FK desaparece, la fila de BrokerLedger SOBREVIVE con FK=NULL.
      4. El ordering por defecto es [-created_at, -id] (más reciente primero).
      5. Los Sum() por revenue_type son correctos y no se contaminan entre tipos.
  - No se toca lógica productiva: BrokerLedger.objects.create() es la única
    vía de escritura (no hay helper de negocio que envuelva esto).
  - Todos los amounts son Decimal, nunca float.
"""
from decimal import Decimal

from django.db.models import Q, Sum
from django.test import TestCase

from simulator.models import BrokerLedger

from .factories import (
    make_account,
    make_broker_ledger,
    make_ledger_entry,
    make_trade,
    make_user,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Creación y tipos de revenue
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerLedgerCreation(TestCase):

    def test_create_commission_entry(self):
        """REV_COMMISSION se guarda con el monto exacto."""
        entry = make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("5.00"))

        self.assertEqual(entry.revenue_type, BrokerLedger.REV_COMMISSION)
        self.assertEqual(entry.amount, Decimal("5.00"))
        self.assertIsNotNone(entry.created_at)

    def test_create_spread_entry(self):
        """REV_SPREAD se guarda correctamente."""
        entry = make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("1.50"))

        self.assertEqual(entry.revenue_type, BrokerLedger.REV_SPREAD)
        self.assertEqual(entry.amount, Decimal("1.50"))

    def test_all_revenue_types_can_be_saved(self):
        """
        Los 5 revenue_type válidos pueden persistirse sin error.
        REVENUE_CHOICES cubre: COMMISSION, SPREAD, CHALLENGE_FEE, WITHDRAW_FEE, ADJUSTMENT.
        """
        all_types = [
            BrokerLedger.REV_COMMISSION,
            BrokerLedger.REV_SPREAD,
            BrokerLedger.REV_CHALLENGE_FEE,
            BrokerLedger.REV_WITHDRAW_FEE,
            BrokerLedger.REV_ADJUSTMENT,
        ]
        for rev_type in all_types:
            with self.subTest(revenue_type=rev_type):
                entry = make_broker_ledger(revenue_type=rev_type, amount=Decimal("10.00"))
                self.assertEqual(entry.revenue_type, rev_type)

    def test_amount_is_decimal_not_float(self):
        """El campo amount se lee de DB como Decimal, no como float."""
        make_broker_ledger(amount=Decimal("3.75"))
        entry = BrokerLedger.objects.first()
        self.assertIsInstance(entry.amount, Decimal)
        self.assertEqual(entry.amount, Decimal("3.75"))

    def test_meta_defaults_to_empty_dict(self):
        """meta JSONField tiene default={} cuando no se especifica."""
        entry = make_broker_ledger()
        self.assertEqual(entry.meta, {})

    def test_symbol_stored(self):
        """El campo symbol opcional se persiste correctamente."""
        entry = make_broker_ledger(symbol="BTCUSD", amount=Decimal("7.50"))
        entry.refresh_from_db()
        self.assertEqual(entry.symbol, "BTCUSD")


# ─────────────────────────────────────────────────────────────────────────────
# 2. FK nullable (SET_NULL on delete) — las filas sobreviven la eliminación FK
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerLedgerFKNullability(TestCase):

    def test_source_account_nullable(self):
        """BrokerLedger se crea sin source_account (null=True)."""
        entry = make_broker_ledger(source_account=None)
        self.assertIsNone(entry.source_account)

    def test_source_account_set_null_on_account_delete(self):
        """
        Eliminar el TradingAccount referenciado → source_account=NULL,
        pero la fila BrokerLedger NO se elimina (on_delete=SET_NULL).
        """
        account = make_account()
        entry = make_broker_ledger(
            revenue_type=BrokerLedger.REV_SPREAD,
            amount=Decimal("2.00"),
            source_account=account,
        )
        entry_id = entry.pk

        account.delete()

        entry.refresh_from_db()
        self.assertIsNone(entry.source_account_id)
        # La fila sigue existiendo
        self.assertTrue(BrokerLedger.objects.filter(pk=entry_id).exists())

    def test_source_trade_set_null_on_trade_delete(self):
        """
        Eliminar el Trade referenciado → source_trade=NULL,
        pero la fila BrokerLedger sobrevive.
        """
        account = make_account()
        trade = make_trade(account=account)
        entry = make_broker_ledger(
            revenue_type=BrokerLedger.REV_COMMISSION,
            amount=Decimal("3.00"),
            source_trade=trade,
        )
        entry_id = entry.pk

        trade.delete()

        entry.refresh_from_db()
        self.assertIsNone(entry.source_trade_id)
        self.assertTrue(BrokerLedger.objects.filter(pk=entry_id).exists())

    def test_source_ledger_set_null_on_ledger_entry_delete(self):
        """
        Eliminar el LedgerEntry referenciado → source_ledger=NULL,
        pero la fila BrokerLedger sobrevive.
        """
        account = make_account()
        ledger_entry = make_ledger_entry(account=account)
        entry = make_broker_ledger(
            revenue_type=BrokerLedger.REV_COMMISSION,
            amount=Decimal("4.00"),
            source_ledger=ledger_entry,
        )
        entry_id = entry.pk

        # LedgerEntry.account usa CASCADE → delete account deletes ledger_entry
        # Borramos directamente el LedgerEntry para aislar el SET_NULL
        ledger_entry.delete()

        entry.refresh_from_db()
        self.assertIsNone(entry.source_ledger_id)
        self.assertTrue(BrokerLedger.objects.filter(pk=entry_id).exists())


# ─────────────────────────────────────────────────────────────────────────────
# 3. Ordering — más reciente primero
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerLedgerOrdering(TestCase):

    def test_default_ordering_newest_first(self):
        """
        BrokerLedger.Meta.ordering = ['-created_at', '-id'].
        El primer elemento del queryset debe ser el de mayor PK
        (insertados en la misma transacción → mismo created_at → desempate por -id).
        """
        first  = make_broker_ledger(amount=Decimal("1.00"))
        second = make_broker_ledger(amount=Decimal("2.00"))
        third  = make_broker_ledger(amount=Decimal("3.00"))

        ids = list(BrokerLedger.objects.values_list("id", flat=True))
        # id más alto primero
        self.assertEqual(ids[0], third.pk)
        self.assertEqual(ids[1], second.pk)
        self.assertEqual(ids[2], first.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Agregación por revenue_type (patrón usado en tasks.py y admin)
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerLedgerAggregation(TestCase):

    def test_sum_single_revenue_type(self):
        """Sum de SPREAD rows solo conta las filas de ese tipo."""
        make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD,      amount=Decimal("1.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD,      amount=Decimal("2.50"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION,  amount=Decimal("10.00"))

        spread_sum = (
            BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_SPREAD)
            .aggregate(total=Sum("amount"))["total"]
        )
        self.assertEqual(spread_sum, Decimal("3.50"))

    def test_types_do_not_bleed_into_each_other(self):
        """
        Dos tipos diferentes con Sum() anotado en un solo queryset
        devuelven valores independientes (patrón de tasks.py con Q()).
        """
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION,  amount=Decimal("5.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION,  amount=Decimal("5.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_SPREAD,      amount=Decimal("3.00"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_CHALLENGE_FEE, amount=Decimal("20.00"))

        agg = BrokerLedger.objects.aggregate(
            commission = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_COMMISSION)),
            spread     = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_SPREAD)),
            challenge  = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)),
            withdraw   = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_WITHDRAW_FEE)),
        )

        self.assertEqual(agg["commission"], Decimal("10.00"))
        self.assertEqual(agg["spread"],     Decimal("3.00"))
        self.assertEqual(agg["challenge"],  Decimal("20.00"))
        self.assertIsNone(agg["withdraw"])  # ninguna fila de ese tipo → NULL

    def test_empty_table_sum_is_none(self):
        """Sum sobre tabla vacía devuelve None (comportamiento estándar de Django ORM)."""
        total = BrokerLedger.objects.aggregate(total=Sum("amount"))["total"]
        self.assertIsNone(total)

    def test_filter_by_symbol(self):
        """Rows pueden filtrarse por símbolo para revenue por instrumento."""
        make_broker_ledger(symbol="EUR/USD", revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("1.00"))
        make_broker_ledger(symbol="EUR/USD", revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("2.00"))
        make_broker_ledger(symbol="BTCUSD",  revenue_type=BrokerLedger.REV_SPREAD, amount=Decimal("7.50"))

        eurusd_sum = (
            BrokerLedger.objects.filter(symbol="EUR/USD")
            .aggregate(total=Sum("amount"))["total"]
        )
        self.assertEqual(eurusd_sum, Decimal("3.00"))
