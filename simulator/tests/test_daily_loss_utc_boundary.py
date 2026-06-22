"""
simulator/tests/test_daily_loss_utc_boundary.py — Bloque D

Verifica que toda la lógica de daily PnL usa fecha UTC, no fecha local del servidor:
  1. _track_daily_pnl resetea el acumulador usando fecha UTC.
  2. _db_fetch_daily_pnl no incluye entradas de días UTC anteriores ni posteriores.
  3. Trade.opened_at y Trade.closed_at creados por _close_position_sync son timezone-aware.
  4. No se emite RuntimeWarning por datetime naive al crear Trades.
"""
import warnings
from datetime import datetime, timezone as dt_timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from simulator.models import LedgerEntry, Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync

from .factories import make_user


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_account(balance=10_000):
    user = make_user()
    return TradingAccount.objects.create(
        user=user,
        account_type="CHALLENGE",
        balance=Decimal(str(balance)),
        equity=Decimal(str(balance)),
        peak_balance=Decimal(str(balance)),
        initial_balance=Decimal(str(balance)),
        status="Activo",
        tier="10K",
    )


def _make_ledger_entry(account, amount, created_at):
    """Create an EV_REALIZED LedgerEntry then override its created_at timestamp."""
    entry = LedgerEntry.objects.create(
        account=account,
        event_type=LedgerEntry.EV_REALIZED,
        amount=Decimal(str(amount)),
        balance_after=account.balance,
        meta={},
    )
    LedgerEntry.objects.filter(pk=entry.pk).update(created_at=created_at)
    return entry


def _make_consumer(account_id):
    from simulator.consumers import TradingConsumer
    consumer = TradingConsumer.__new__(TradingConsumer)
    consumer._db_account_id = account_id
    return consumer


# ─────────────────────────────────────────────────────────────────────────────
# 1. _track_daily_pnl — frontera UTC
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackDailyPnlUTCBoundary(TestCase):

    def test_accumulator_resets_on_utc_midnight(self):
        """_track_daily_pnl resetea solo cuando cambia la fecha UTC."""
        consumer = _make_consumer(None)
        consumer._daily_realized_pnl = 0.0
        consumer._daily_pnl_date = None

        day1_utc = datetime(2026, 1, 1, 23, 59, 0, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=day1_utc):
            consumer._track_daily_pnl(100.0)

        self.assertAlmostEqual(consumer._daily_realized_pnl, 100.0)
        self.assertEqual(consumer._daily_pnl_date, day1_utc.date())

        # Un segundo después: UTC midnight → fecha cambia → debe resetear
        day2_utc = datetime(2026, 1, 2, 0, 0, 1, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=day2_utc):
            consumer._track_daily_pnl(-50.0)

        # Solo debe contener el PnL del día 2, no el del día 1
        self.assertAlmostEqual(consumer._daily_realized_pnl, -50.0)
        self.assertEqual(consumer._daily_pnl_date, day2_utc.date())

    def test_accumulator_does_not_reset_within_same_utc_day(self):
        """_track_daily_pnl acumula PnL dentro del mismo día UTC."""
        consumer = _make_consumer(None)
        consumer._daily_realized_pnl = 0.0
        consumer._daily_pnl_date = None

        same_day_morning = datetime(2026, 6, 15, 8, 0, 0, tzinfo=dt_timezone.utc)
        same_day_evening = datetime(2026, 6, 15, 20, 0, 0, tzinfo=dt_timezone.utc)

        with patch("django.utils.timezone.now", return_value=same_day_morning):
            consumer._track_daily_pnl(200.0)

        with patch("django.utils.timezone.now", return_value=same_day_evening):
            consumer._track_daily_pnl(-80.0)

        self.assertAlmostEqual(consumer._daily_realized_pnl, 120.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _db_fetch_daily_pnl — filtro UTC correcto
# ─────────────────────────────────────────────────────────────────────────────

class TestDbFetchDailyPnlUTCFilter(TestCase):

    def test_excludes_entry_from_previous_utc_day(self):
        """_db_fetch_daily_pnl no suma entradas del día UTC anterior."""
        account = _make_account()

        # Entrada de ayer 23:59 UTC — NO debe incluirse
        yesterday_end = timezone.now().replace(
            hour=23, minute=59, second=0, microsecond=0
        ) - timedelta(days=1)
        _make_ledger_entry(account, -500.0, yesterday_end)

        # Entrada de hoy 00:01 UTC — SÍ debe incluirse
        today_morning = timezone.now().replace(
            hour=0, minute=1, second=0, microsecond=0
        )
        _make_ledger_entry(account, -200.0, today_morning)

        consumer = _make_consumer(account.id)
        result = async_to_sync(consumer._db_fetch_daily_pnl)()

        self.assertAlmostEqual(result, -200.0, places=2)

    def test_excludes_entry_from_next_utc_day(self):
        """_db_fetch_daily_pnl no suma entradas del día UTC siguiente."""
        account = _make_account()

        # Entrada de mañana 00:01 UTC — NO debe incluirse
        tomorrow_start = timezone.now().replace(
            hour=0, minute=1, second=0, microsecond=0
        ) + timedelta(days=1)
        _make_ledger_entry(account, -999.0, tomorrow_start)

        # Entrada de hoy 12:00 UTC — SÍ debe incluirse
        today_noon = timezone.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        _make_ledger_entry(account, -100.0, today_noon)

        consumer = _make_consumer(account.id)
        result = async_to_sync(consumer._db_fetch_daily_pnl)()

        self.assertAlmostEqual(result, -100.0, places=2)

    def test_sums_multiple_entries_within_utc_day(self):
        """_db_fetch_daily_pnl suma correctamente múltiples entradas del día UTC."""
        account = _make_account()

        for hour, amount in [(1, -150.0), (9, 80.0), (18, -40.0)]:
            ts = timezone.now().replace(hour=hour, minute=0, second=0, microsecond=0)
            _make_ledger_entry(account, amount, ts)

        consumer = _make_consumer(account.id)
        result = async_to_sync(consumer._db_fetch_daily_pnl)()

        self.assertAlmostEqual(result, -110.0, places=2)

    def test_returns_zero_when_no_entries_today(self):
        """_db_fetch_daily_pnl devuelve 0.0 si no hay entradas hoy UTC."""
        account = _make_account()
        consumer = _make_consumer(account.id)
        result = async_to_sync(consumer._db_fetch_daily_pnl)()
        self.assertEqual(result, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Trade.opened_at / closed_at — timezone-aware
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeDatetimesAreAware(TestCase):

    def _make_pos_mem(self, pos):
        return {
            "id":        pos.id,
            "symbol":    pos.symbol,
            "side":      pos.side.lower(),
            "qty":       float(pos.qty),
            "avg":       float(pos.avg_price),
            "sl":        None,
            "tp":        None,
            "opened_at": pos.opened_at.timestamp(),
        }

    def test_close_position_sync_creates_aware_datetimes(self):
        """Trade creado por _close_position_sync tiene opened_at y closed_at con tzinfo."""
        account = _make_account()
        pos = Position.objects.create(
            account=account,
            symbol="EUR/USD",
            side="BUY",
            qty=Decimal("0.01"),
            avg_price=Decimal("1.10000"),
        )

        _close_position_sync(
            pos_mem=self._make_pos_mem(pos),
            account_id=account.id,
            close_px=1.10050,
            reason="manual",
            realized_pnl=0.50,
            new_balance=10_000.50,
            new_equity=10_000.50,
        )

        trade = Trade.objects.filter(account=account).last()
        self.assertIsNotNone(trade, "Trade debe haberse creado")
        self.assertIsNotNone(trade.opened_at.tzinfo, "opened_at debe ser timezone-aware")
        self.assertIsNotNone(trade.closed_at.tzinfo, "closed_at debe ser timezone-aware")
        self.assertEqual(trade.opened_at.tzinfo, dt_timezone.utc)

    def test_consumer_close_creates_aware_datetimes(self):
        """Trade creado por _db_close_position_atomic tiene opened_at y closed_at con tzinfo."""
        from simulator.consumers import TradingConsumer

        account = _make_account()
        pos = Position.objects.create(
            account=account,
            symbol="EUR/USD",
            side="BUY",
            qty=Decimal("0.01"),
            avg_price=Decimal("1.10000"),
        )

        consumer = _make_consumer(account.id)
        consumer.account = {"status": "Activo", "peak_balance": float(account.peak_balance)}

        pos_mem = {
            "id":        pos.id,
            "symbol":    "EUR/USD",
            "side":      "buy",
            "qty":       0.01,
            "avg":       1.10000,
            "sl":        None,
            "tp":        None,
            "opened_at": pos.opened_at.timestamp(),
        }

        async_to_sync(consumer._db_close_position_atomic)(
            pos_mem=pos_mem,
            close_px=1.10050,
            reason="manual",
            realized_pnl=0.50,
            new_balance=10_000.50,
            new_equity=10_000.50,
        )

        trade = Trade.objects.filter(account=account).last()
        self.assertIsNotNone(trade, "Trade debe haberse creado")
        self.assertIsNotNone(trade.opened_at.tzinfo, "opened_at debe ser timezone-aware")
        self.assertIsNotNone(trade.closed_at.tzinfo, "closed_at debe ser timezone-aware")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Sin RuntimeWarning por naive datetime
# ─────────────────────────────────────────────────────────────────────────────

class TestNoNaiveDatetimeWarning(TestCase):

    def test_no_runtime_warning_on_close_position_sync(self):
        """_close_position_sync no emite RuntimeWarning por datetime naive."""
        account = _make_account()
        pos = Position.objects.create(
            account=account,
            symbol="EUR/USD",
            side="SELL",
            qty=Decimal("0.01"),
            avg_price=Decimal("1.10000"),
        )
        pos_mem = {
            "id":        pos.id,
            "symbol":    "EUR/USD",
            "side":      "sell",
            "qty":       0.01,
            "avg":       1.10000,
            "sl":        None,
            "tp":        None,
            "opened_at": pos.opened_at.timestamp(),
        }

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _close_position_sync(
                pos_mem=pos_mem,
                account_id=account.id,
                close_px=1.09950,
                reason="manual",
                realized_pnl=0.50,
                new_balance=10_000.50,
                new_equity=10_000.50,
            )

        runtime_warnings = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and "naive" in str(w.message).lower()
        ]
        self.assertEqual(
            runtime_warnings, [],
            f"RuntimeWarning(s) inesperado(s): {[str(w.message) for w in runtime_warnings]}",
        )
