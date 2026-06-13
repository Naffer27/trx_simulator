"""
simulator/tests/test_peak_balance_atomic_update.py — Bloque B

Verifica que peak_balance se actualiza con CASE/WHEN SQL atómico, garantizando
monotonía a nivel de DB sin depender del select_for_update() del caller.

Nota de setup: TradingAccount.save() en creación fuerza peak_balance = initial_balance.
Para separar balance y peak_balance se usa .update() de Django (bypass de save()),
el mismo patrón que los tests de DD existentes.
"""
from decimal import Decimal

from django.test import TestCase

from simulator.models import TradingAccount
from simulator.risk_engine import check_and_enforce_risk

from .factories import make_account


class TestPeakBalanceAtomicUpdate(TestCase):
    """
    check_and_enforce_risk usa CASE/WHEN SQL para peak_balance.
    La invariante "peak_balance solo puede crecer" se sostiene en DB aunque
    el objeto en memoria tenga un valor stale (caso race condition).
    """

    def test_peak_increases_when_balance_exceeds_peak(self):
        """
        balance=11 000 > peak=10 000 → DB peak_balance actualizado a 11 000.
        Setup: creamos con balance=10 000 (peak=10 000), luego subimos balance
        a 11 000 con .update() para mantener peak=10 000 intacto.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal("11000")
        )
        account.refresh_from_db()  # balance=11000, peak=10000

        check_and_enforce_risk(account)
        account.refresh_from_db()
        self.assertEqual(account.peak_balance, Decimal("11000"))

    def test_peak_not_decreased_by_stale_in_memory_value(self):
        """
        Simula race: DB tiene peak=12 000 (escrito por otra txn), pero el objeto
        en memoria tiene peak=10 000 (stale) y balance=11 000.

        Código anterior (UPDATE ciego): SET peak=11 000 → borraba el 12 000 de DB.
        Código nuevo (CASE/WHEN):       WHEN peak(12 000) < balance(11 000) → False
                                        → ELSE peak_balance → DB queda en 12 000.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        # Subir balance a 11 000 (la cuenta tuvo ganancias)
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal("11000")
        )
        account.refresh_from_db()  # balance=11000, peak=10000 en memoria

        # Otra transacción concurrente ya escribió peak=12 000 en DB
        TradingAccount.objects.filter(pk=account.pk).update(
            peak_balance=Decimal("12000")
        )
        # account en memoria: balance=11 000, peak=10 000 (stale)
        # DB: balance=11 000, peak=12 000

        check_and_enforce_risk(account)
        # Tras la llamada, el objeto en memoria ya debe reflejar el valor authoritative de DB
        self.assertEqual(
            account.peak_balance, Decimal("12000"),
            "in-memory peak_balance debe ser el valor authoritative de DB, no el stale",
        )
        # Confirmación adicional: DB también queda en 12 000
        account.refresh_from_db()
        self.assertEqual(account.peak_balance, Decimal("12000"))

    def test_peak_unchanged_when_balance_below_peak(self):
        """
        balance=9 000 < peak=10 000 → DB peak_balance permanece en 10 000.
        Setup: creamos con balance=10 000 (peak=10 000), luego bajamos balance
        a 9 000 con .update() para simular pérdida sin tocar peak.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        TradingAccount.objects.filter(pk=account.pk).update(
            balance=Decimal("9000")
        )
        account.refresh_from_db()  # balance=9000, peak=10000

        check_and_enforce_risk(account)
        account.refresh_from_db()
        self.assertEqual(account.peak_balance, Decimal("10000"))

    def test_peak_unchanged_when_balance_equals_peak(self):
        """
        balance==peak → WHEN usa __lt (estrictamente menor), igualdad no activa
        el THEN → DB peak permanece igual.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        # peak=10000, balance=10000 (iguales tras make_account)

        check_and_enforce_risk(account)
        account.refresh_from_db()
        self.assertEqual(account.peak_balance, Decimal("10000"))
