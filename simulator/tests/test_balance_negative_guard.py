"""
simulator/tests/test_balance_negative_guard.py — Bloque A

Verifica que _close_position_sync NUNCA escribe un balance negativo en DB.

Garantías que debe cumplir el guard:
  1. account.balance >= 0 siempre, incluso si realized_pnl > account.balance.
  2. account.equity  >= 0 siempre en el mismo escenario.
  3. El resultado dict devuelve new_balance=0.0 (no el valor negativo).
  4. Se crea LedgerEntry EV_ADJUSTMENT con meta["reason"]=="negative_balance_guard"
     y meta["shortfall"] > 0 cuando se activa el guard.
  5. LedgerEntry EV_REALIZED.balance_after == account.balance == 0 (invariante).
  6. Pérdida que no lleva el balance a negativo → sin EV_ADJUSTMENT (guard no activa).
  7. new_balance exactamente 0 → sin EV_ADJUSTMENT (guard no activa).
  8. El guard usa aritmética Decimal-nativa: un shortfall < 1e-8 no se pierde
     (el código anterior usaba round(float, 8) que convertía 1e-9 → 0.0 en silencio).
"""
import time
from decimal import Decimal

from django.test import TestCase

from simulator.models import LedgerEntry, Position
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position


def _pos_mem(pos: Position) -> dict:
    return {
        "id":        pos.pk,
        "symbol":    pos.symbol,
        "side":      pos.side,
        "qty":       float(pos.qty),
        "avg":       float(pos.avg_price),
        "sl":        None,
        "tp":        None,
        "opened_at": time.time(),
    }


def _close(account, pos, realized_pnl: float, close_px: float = 1.05000) -> dict:
    new_balance = float(account.balance) + realized_pnl
    return _close_position_sync(
        pos_mem      = _pos_mem(pos),
        account_id   = account.pk,
        close_px     = close_px,
        reason       = "daemon_sl",
        realized_pnl = realized_pnl,
        new_balance  = new_balance,
        new_equity   = new_balance,
    )


class TestBalanceNegativeGuard(TestCase):

    # ── 1. Guard activa: balance clamped a 0 ──────────────────────────────────

    def test_extreme_loss_balance_never_goes_negative(self):
        """
        realized_pnl=-200 con balance=100 → new_balance computado=-100.
        Después del close account.balance debe ser 0, jamás negativo.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-200.0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("0"))
        self.assertGreaterEqual(float(account.balance), 0.0)

    def test_extreme_loss_equity_never_goes_negative(self):
        """
        Mismo escenario: account.equity también queda en 0, no negativo.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-200.0)
        account.refresh_from_db()
        self.assertGreaterEqual(float(account.equity), 0.0)

    def test_extreme_loss_result_new_balance_is_zero(self):
        """
        El resultado que retorna _close_position_sync debe tener new_balance=0.0,
        nunca el valor negativo computado externamente.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        result = _close(account, pos, realized_pnl=-200.0)
        self.assertEqual(result["new_balance"], 0.0)

    # ── 2. EV_ADJUSTMENT creado con shortfall ────────────────────────────────

    def test_ev_adjust_created_on_negative_balance(self):
        """
        Cuando el guard activa, debe existir exactamente 1 LedgerEntry EV_ADJUSTMENT
        con reason=="negative_balance_guard".
        Nota: TradingAccount.check_rules() también crea EV_ADJUST cuando balance<=0
        (suspensión), por eso filtramos por meta__reason específico.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-200.0)
        guard_entries = LedgerEntry.objects.filter(
            account=account,
            event_type=LedgerEntry.EV_ADJUST,
            meta__reason="negative_balance_guard",
        )
        self.assertEqual(
            guard_entries.count(), 1,
            "Debe existir exactamente 1 EV_ADJUSTMENT del guard de balance negativo",
        )

    def test_ev_adjust_meta_shortfall_is_correct(self):
        """
        meta["shortfall"] debe ser el monto exacto que se descartó
        (abs(new_balance) cuando new_balance < 0).
        balance=100, realized=-200 → new_balance=-100 → shortfall=100.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-200.0)
        adj = LedgerEntry.objects.get(
            account=account,
            event_type=LedgerEntry.EV_ADJUST,
            meta__reason="negative_balance_guard",
        )
        self.assertAlmostEqual(adj.meta["shortfall"], 100.0, places=4)
        self.assertAlmostEqual(adj.meta["original_computed_balance"], -100.0, places=4)

    # ── 3. Invariante ledger tras clamp ──────────────────────────────────────

    def test_ledger_invariant_after_clamp(self):
        """
        Invariante: LedgerEntry EV_REALIZED.balance_after == account.balance.
        Después del clamp ambos deben ser 0.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-200.0)
        le = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_REALIZED)
        account.refresh_from_db()
        self.assertEqual(le.balance_after, account.balance)

    # ── 4. Guard NO activa en pérdidas normales ───────────────────────────────

    def test_normal_loss_no_guard_triggered(self):
        """
        Pérdida que deja balance positivo (9500) → sin EV_ADJUSTMENT del guard, balance correcto.
        """
        account = make_account(balance=Decimal("10000"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-500.0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("9500"))
        self.assertFalse(
            LedgerEntry.objects.filter(
                account=account,
                event_type=LedgerEntry.EV_ADJUST,
                meta__reason="negative_balance_guard",
            ).exists(),
            "No debe crearse EV_ADJUSTMENT del guard cuando el balance queda positivo",
        )

    def test_new_balance_exactly_zero_no_guard(self):
        """
        new_balance exactamente 0 (pérdida igual al balance) → guard NO activa (condición es
        new_balance < 0, no <= 0), sin EV_ADJUSTMENT del guard.
        Nota: check_rules() puede crear su propio EV_ADJUST por balance<=0; aquí solo
        verificamos que el guard de balance negativo NO se activó.
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))
        _close(account, pos, realized_pnl=-100.0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("0"))
        self.assertFalse(
            LedgerEntry.objects.filter(
                account=account,
                event_type=LedgerEntry.EV_ADJUST,
                meta__reason="negative_balance_guard",
            ).exists(),
            "Guard no debe activarse cuando new_balance es exactamente 0",
        )

    # ── 5. Precisión Decimal-nativa (no float leakage) ────────────────────────

    def test_guard_decimal_native_catches_sub_cent_shortfall(self):
        """
        El guard usa aritmética Decimal-nativa, no float.

        Bug concreto del código anterior:
          round(abs(float(-1e-9)), 8) == 0.0  → guard nunca disparaba para
          shortfalls menores a 1e-8, silenciando el EV_ADJUSTMENT.

        Con Decimal-native:
          abs(min(Decimal("-1E-9"), Decimal("0"))) == Decimal("1E-9") > 0
          → guard dispara correctamente y registra el shortfall.

        También verifica que account.balance queda en Decimal("0") exacto
        (no un artefacto como Decimal("0.00000001")).
        """
        account = make_account(balance=Decimal("100"), account_type="RETAIL")
        pos = make_position(account=account, side="BUY", avg_price=Decimal("1.10000"))

        # Pasamos new_balance con shortfall de 1 nanounidad (9 decimales)
        # El guard debe dispararlo aunque sea subcentavo
        result = _close_position_sync(
            pos_mem      = _pos_mem(pos),
            account_id   = account.pk,
            close_px     = 1.09,
            reason       = "daemon_sl",
            realized_pnl = -100.000000001,
            new_balance  = -0.000000001,   # < 0 en 9 decimales
            new_equity   = -0.000000001,
        )

        # 1. Resultado devuelve 0.0, no el float negativo
        self.assertEqual(result["new_balance"], 0.0)

        # 2. DB: balance exactamente 0, sin artefactos de float
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("0"))

        # 3. El guard disparó y registró el shortfall (no lo silenciló)
        adj = LedgerEntry.objects.filter(
            account=account,
            event_type=LedgerEntry.EV_ADJUST,
            meta__reason="negative_balance_guard",
        )
        self.assertEqual(adj.count(), 1, "Guard debe disparar para shortfall sub-cent")
        self.assertGreater(
            adj.first().meta["shortfall"], 0,
            "shortfall en meta debe ser > 0, no fue silenciado por rounding",
        )
