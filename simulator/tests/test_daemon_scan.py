"""
simulator/tests/test_daemon_scan.py — Bloque 7

Cubre: scan_positions_task (simulator/tasks.py) — daemon offline de ejecución.

scan_positions_task:
  1. Lee posiciones abiertas (account__status="Activo").
  2. Lee bid/ask de Redis via _read_cached_price (mockeado).
  3. Si precio disponible: calcula equity/margin offline (Step 5) y evalúa stopout/margin-call.
  4. Si no stopout: revisa SL/TP por posición y cierra si fue alcanzado.
  5. Todos los cierres van por _close_position_sync → already_closed guard previene duplicados.

Mock target: "simulator.tasks._read_cached_price"
  - Signature: (symbol: str) -> (bid: float | None, ask: float | None)
  - (None, None) → precio stale/missing; posición omitida (skipped_stale++).

Lógica de trigger SL/TP (del código):
  BUY  → trigger_px = bid  ; sl_hit si bid ≤ sl  ; tp_hit si bid ≥ tp
  SELL → trigger_px = ask  ; sl_hit si ask ≥ sl  ; tp_hit si ask ≤ tp

Todos los amounts en Decimal. Tests independientes.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from simulator.models import LedgerEntry, Position, Trade, TradingAccount
from simulator.tasks import scan_positions_task

from .factories import make_account, make_position


def _scan():
    """Execute scan_positions_task synchronously without a Celery broker."""
    return scan_positions_task.apply().get()


def _eur_prices(bid: float, ask: float | None = None):
    """Return a mock side_effect that yields (bid, ask) for EUR/USD only."""
    _ask = ask if ask is not None else round(bid + 0.00010, 5)

    def _mock(symbol: str):
        if symbol == "EUR/USD":
            return (bid, _ask)
        return (None, None)

    return _mock


# ─────────────────────────────────────────────────────────────────────────────
# 1. Sin posiciones abiertas
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanEmpty(TestCase):

    def test_no_positions_returns_zero_counts(self):
        """Sin posiciones activas → scanned=0, closed=0, skipped_stale=0."""
        result = _scan()
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["closed"],  0)
        self.assertEqual(result["skipped_stale"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. SL/TP triggers — BUY y SELL
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanSLTP(TestCase):
    """
    EUR/USD specs: pip_size=0.0001, price_decimals=5, contract_size=100_000.
    Posición base: qty=0.1 lot, avg=1.10000.
    """

    # ── BUY ───────────────────────────────────────────────────────────────────

    @patch("simulator.tasks._read_cached_price")
    def test_buy_sl_hit_closes_position(self, mock_price):
        """
        BUY avg=1.10000, sl=1.09000.
        bid=1.08900 ≤ sl=1.09000 → sl_hit → posición cerrada.
        """
        mock_price.side_effect = _eur_prices(bid=1.08900)
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.09000"))

        result = _scan()

        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["closed"],  1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.trade_type, "BUY")
        self.assertLess(float(trade.profit_loss), 0)  # pérdida (SL)

    @patch("simulator.tasks._read_cached_price")
    def test_buy_tp_hit_closes_position(self, mock_price):
        """
        BUY avg=1.10000, tp=1.11000.
        bid=1.11100 ≥ tp=1.11000 → tp_hit → posición cerrada con ganancia.
        """
        mock_price.side_effect = _eur_prices(bid=1.11100)
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            tp=Decimal("1.11000"))

        result = _scan()

        self.assertEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        trade = Trade.objects.get(account=account)
        self.assertGreater(float(trade.profit_loss), 0)  # ganancia (TP)

    @patch("simulator.tasks._read_cached_price")
    def test_buy_sl_not_hit_position_stays_open(self, mock_price):
        """
        BUY avg=1.10000, sl=1.09000.
        bid=1.09500 > sl=1.09000 → no hit → posición queda abierta.
        """
        mock_price.side_effect = _eur_prices(bid=1.09500)
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.09000"))

        result = _scan()

        self.assertEqual(result["closed"], 0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)

    # ── SELL ──────────────────────────────────────────────────────────────────

    @patch("simulator.tasks._read_cached_price")
    def test_sell_sl_hit_closes_position(self, mock_price):
        """
        SELL avg=1.10000, sl=1.11000.
        ask=1.11100 ≥ sl=1.11000 → sl_hit → posición cerrada con pérdida.
        """
        mock_price.side_effect = _eur_prices(bid=1.11090, ask=1.11100)
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="SELL",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.11000"))

        result = _scan()

        self.assertEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.trade_type, "SELL")
        self.assertLess(float(trade.profit_loss), 0)  # pérdida (SL)

    @patch("simulator.tasks._read_cached_price")
    def test_sell_tp_hit_closes_position(self, mock_price):
        """
        SELL avg=1.10000, tp=1.09000.
        ask=1.08900 ≤ tp=1.09000 → tp_hit → posición cerrada con ganancia.
        """
        mock_price.side_effect = _eur_prices(bid=1.08890, ask=1.08900)
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="SELL",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            tp=Decimal("1.09000"))

        result = _scan()

        self.assertEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.trade_type, "SELL")
        self.assertGreater(float(trade.profit_loss), 0)  # ganancia (TP)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Precio stale/missing → skip
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanStalePrice(TestCase):

    @patch("simulator.tasks._read_cached_price", return_value=(None, None))
    def test_stale_price_skips_position(self, mock_price):
        """
        _read_cached_price devuelve (None, None) → posición omitida.
        skipped_stale > 0, posición sigue abierta, sin Trade.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.09000"))

        result = _scan()

        self.assertGreater(result["skipped_stale"], 0)
        self.assertEqual(result["closed"], 0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Idempotencia — segunda corrida no duplica Trade ni LedgerEntry
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanIdempotency(TestCase):

    @patch("simulator.tasks._read_cached_price")
    def test_second_scan_no_duplicate_trade_or_ledger(self, mock_price):
        """
        Primera corrida: SL hit → Trade + LedgerEntry creados, Position eliminada.
        Segunda corrida: posición ya no existe → already_closed, sin duplicados.
        """
        mock_price.side_effect = _eur_prices(bid=1.08900)
        account = make_account(account_type="CHALLENGE", tier="10K")
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                      sl=Decimal("1.09000"))

        _scan()  # primera corrida — cierra posición
        _scan()  # segunda corrida — posición ya eliminada

        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=account, event_type=LedgerEntry.EV_REALIZED
            ).count(), 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cuentas Suspendidas son ignoradas
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanSuspendedAccount(TestCase):

    @patch("simulator.tasks._read_cached_price")
    def test_suspended_account_positions_not_scanned(self, mock_price):
        """
        Cuenta con status=Suspendido → sus posiciones no entran al query
        (filter account__status='Activo'). El daemon las ignora completamente.
        """
        mock_price.side_effect = _eur_prices(bid=1.08900)
        account = make_account(status="Suspendido")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.09000"))

        result = _scan()

        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["closed"],  0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())


# ─────────────────────────────────────────────────────────────────────────────
# 6. CHALLENGE stopout — equity < 90% de peak → cierra todo y suspende
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanChallengeStopout(TestCase):
    """
    CHALLENGE 10K: max_drawdown=10%, peak=10 000, stopout_level=9 000.
    1 lot EUR/USD BUY avg=1.10000, bid=1.08800 →
      floating = (1.08800 − 1.10000) × 1.0 × 100 000 = −1 200
      equity   = 10 000 − 1 200 = 8 800 ≤ 9 000 → stopout.
    """

    @patch("simulator.tasks._read_cached_price")
    def test_stopout_closes_all_positions(self, mock_price):
        """
        Stopout → _daemon_close_all corre → todas las posiciones de la cuenta cerradas.
        """
        mock_price.side_effect = _eur_prices(bid=1.08800, ask=1.08810)
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        result = _scan()

        self.assertGreaterEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.assertGreaterEqual(Trade.objects.filter(account=account).count(), 1)

    @patch("simulator.tasks._read_cached_price")
    def test_stopout_suspends_account(self, mock_price):
        """
        Tras el stopout, la cuenta queda Suspendida (via check_and_enforce_risk
        o via el fallback _db_suspend_account_sync).
        """
        mock_price.side_effect = _eur_prices(bid=1.08800, ask=1.08810)
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        _scan()

        account.refresh_from_db()
        self.assertEqual(account.status, "Suspendido")


# ─────────────────────────────────────────────────────────────────────────────
# 7. RETAIL margin call — equity / margin_used < 50% → cierra todo
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanRetailMarginCall(TestCase):
    """
    RETAIL account: balance=1 000, leverage=50.
    0.5 lot EUR/USD BUY avg=1.10000 →
      margin_used = |1.10 × 0.5 × 100 000| / 50 = 1 100
      bid=1.09000 → floating = (1.09 − 1.10) × 50 000 = −500
      equity = 1 000 − 500 = 500
      margin_level = 500 / 1 100 × 100 = 45.5% < 50% → margin call.
    """

    @patch("simulator.tasks._read_cached_price")
    def test_margin_call_closes_all_positions(self, mock_price):
        """Margin call → todas las posiciones del account cerradas."""
        mock_price.side_effect = _eur_prices(bid=1.09000, ask=1.09010)
        account = make_account(account_type="RETAIL", balance=Decimal("1000"))
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.5"), avg_price=Decimal("1.10000"))

        result = _scan()

        self.assertGreaterEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    @patch("simulator.tasks._read_cached_price")
    def test_retail_no_suspension_after_margin_call(self, mock_price):
        """
        RETAIL margin call: las posiciones se cierran pero la cuenta NO se suspende.
        El risk engine de broker no aplica suspension automática a cuentas RETAIL.
        """
        mock_price.side_effect = _eur_prices(bid=1.09000, ask=1.09010)
        account = make_account(account_type="RETAIL", balance=Decimal("1000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.5"), avg_price=Decimal("1.10000"))

        _scan()

        account.refresh_from_db()
        self.assertNotIn(account.status, {"Suspendido", "Violado"})


# ─────────────────────────────────────────────────────────────────────────────
# 8. Sin ghost positions después del scan completo
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonScanNoGhostPositions(TestCase):

    @patch("simulator.tasks._read_cached_price")
    def test_no_ghost_positions_remain_after_full_scan(self, mock_price):
        """
        Múltiples posiciones con SL/TP alcanzados en distintas cuentas.
        Después del scan: Position.objects.count() == 0.
        No quedan posiciones huérfanas ('ghost positions').
        """
        mock_price.side_effect = _eur_prices(bid=1.11100)  # TP hit para todos los BUY

        # Dos cuentas, cada una con una posición BUY (tp alcanzado)
        for _ in range(2):
            acc = make_account(account_type="CHALLENGE", tier="10K")
            make_position(acc, symbol="EUR/USD", side="BUY",
                          qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                          tp=Decimal("1.11000"))

        self.assertEqual(Position.objects.count(), 2)

        result = _scan()

        self.assertEqual(result["closed"], 2)
        self.assertEqual(Position.objects.count(), 0,
                         "Ghost positions detected: some positions were not closed by the daemon")
