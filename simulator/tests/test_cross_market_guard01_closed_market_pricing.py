# simulator/tests/test_cross_market_guard01_closed_market_pricing.py
"""
CROSS-MARKET-GUARD-01 — pruebas dirigidas.

Problema cerrado: _db_open_position_atomic's INVARIANTE-1 (PANEL-02)
trataba "sin tick vivo en los últimos 60s" como un único estado binario
para CUALQUIER posición abierta, sin importar si el motivo era que el
mercado de esa posición estaba legítimamente cerrado (Forex fin de
semana) o si el feed estaba genuinamente averiado durante horario de
mercado abierto. Una posición EUR/USD abierta durante el cierre normal
de Forex bloqueaba así CUALQUIER orden nueva de la cuenta, incluida
BTCUSD (24/7).

Fix: reutiliza, sin duplicarla, la MISMA política ya auditada en
simulator/broker_exposure.py (GOLDEN-WEEKEND-RISK-01) —
_resolve_symbol_prices() — dentro de _db_open_position_atomic. Solo
FRESH/MARKET_CLOSED_FROZEN cuentan como "preciado"; STALE/UNAVAILABLE
siguen rechazando exactamente igual que antes. La fórmula de PnL
(pnl_engine.position_pnl_float) no cambia — solo qué precio real la
alimenta para una posición en mercado cerrado.

El gate de mercado-cerrado para ÓRDENES NUEVAS (_order_new, su propio
evaluate_market_session_for_symbol()) es un camino completamente
distinto y no se tocó — ver
test_fix05a_financial_price_integrity.py::MarketClosedGateTests,
reejecutado sin cambios como prueba dirigida #5 (ver informe).

Reutiliza los helpers ya establecidos: _consumer/_clear_price/
_seed_fresh_price/_seed_stale_price (test_market_price_unavailable_guard.py)
y make_session/_SESSION_PATCH_TARGET
(test_golden_weekend_risk01_market_closed_exposure.py) — ningún helper
nuevo duplica lógica ya existente.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from market_data.contracts import OrderPolicy
from market_data.feeds import _PRICE_CACHE_TTL, get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import LastKnownMarketPrice, Position

from .factories import make_account, make_position
from .test_golden_weekend_risk01_market_closed_exposure import (
    _SESSION_PATCH_TARGET,
    make_session,
)
from .test_market_price_unavailable_guard import (
    _clear_price,
    _consumer,
    _seed_fresh_price,
    _seed_stale_price,
)

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _make_durable_row(symbol, mid, source="massive", age_seconds=3600):
    from django.utils import timezone as dj_tz
    LastKnownMarketPrice.objects.update_or_create(
        symbol=symbol,
        defaults={
            "bid": Decimal(str(mid)) - Decimal("0.0001"),
            "ask": Decimal(str(mid)) + Decimal("0.0001"),
            "mid": Decimal(str(mid)),
            "source": source,
            "observed_at": dj_tz.now() - dj_tz.timedelta(seconds=age_seconds),
        },
    )


class ClosedMarketWithDurablePriceAllowsOtherMarketTests(TestCase):
    """1) EUR/USD abierto + Forex cerrado + LastKnownMarketPrice válido +
    orden BTCUSD → el guard permite continuar."""

    def setUp(self):
        _clear_price("EUR/USD")
        _clear_price("BTCUSD")

    def test_btcusd_order_proceeds_despite_closed_forex_position(self):
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        _make_durable_row("EUR/USD", 1.17000)

        session = make_session(symbol="EUR/USD")
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = _db_open_sync(
                _consumer(account.pk), "BTCUSD", "buy", 0.01, 63000.0, None, None,
                commission=0.0, new_balance=10000.0,
            )
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result.get("error_code"), "market_price_unavailable")


class ClosedMarketWithoutDurablePriceStillRejectsTests(TestCase):
    """2) EUR/USD abierto + Forex cerrado + SIN LastKnownMarketPrice →
    rechazo (UNAVAILABLE, nunca se inventa un precio)."""

    def setUp(self):
        _clear_price("EUR/USD")
        _clear_price("BTCUSD")
        LastKnownMarketPrice.objects.filter(symbol="EUR/USD").delete()

    def test_rejected_when_no_durable_row_exists(self):
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.10000"))

        session = make_session(symbol="EUR/USD")
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = _db_open_sync(
                _consumer(account.pk), "BTCUSD", "buy", 0.01, 63000.0, None, None,
                commission=0.0, new_balance=10000.0,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("EUR/USD", result["message"])


class OpenMarketStaleFeedStillRejectsTests(TestCase):
    """3) EUR/USD abierto + Forex ABIERTO + feed stale → rechazo (STALE
    nunca consulta LastKnownMarketPrice, aunque exista una fila válida)."""

    def setUp(self):
        _clear_price("EUR/USD")
        _clear_price("BTCUSD")

    def test_rejected_when_market_open_but_feed_stale_even_with_durable_row(self):
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        # Fila durable presente — debe ser IGNORADA porque el mercado no
        # está cerrado.
        _make_durable_row("EUR/USD", 1.17000)
        _seed_stale_price("EUR/USD", 1.1699, 1.1701, age_seconds=_PRICE_CACHE_TTL + 30)

        session = make_session(symbol="EUR/USD", order_policy=OrderPolicy.OPEN_NORMAL)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = _db_open_sync(
                _consumer(account.pk), "BTCUSD", "buy", 0.01, 63000.0, None, None,
                commission=0.0, new_balance=10000.0,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("EUR/USD", result["message"])


class FreshPathUnchangedTests(TestCase):
    """4) BTCUSD abierto/fresco → comportamiento existente intacto (la
    rama FRESH sigue usando last_bid()/last_ask() del feed en vivo, no
    el precio congelado — misma fórmula, mismo resultado de antes de
    este fix)."""

    def setUp(self):
        _clear_price("BTCUSD")
        _clear_price("EUR/USD")

    def test_fresh_btcusd_position_prices_via_live_feed_not_frozen(self):
        _seed_fresh_price("BTCUSD", 62990.0, 63010.0)
        account = make_account(balance=Decimal("10000.00"))
        # BUY cierra al bid (62990); avg_price=62000 -> ganancia real.
        make_position(account, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("62000.00"))

        # evaluate_market_session_for_symbol NO se mockea aquí — BTCUSD
        # es CRYPTO_24_7, siempre OPEN con el reloj real; nunca debería
        # necesitar consultar _resolve_symbol_prices más allá del estado
        # FRESH ya presente.
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.1700, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result.get("error_code"), "market_price_unavailable")

    def test_rejection_paths_for_missing_or_stale_price_unchanged(self):
        """No regresión de los escenarios YA cubiertos por
        test_market_price_unavailable_guard.py — ausencia total de
        precio sigue rechazando igual tras este cambio."""
        _clear_price("USD/JPY")
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("150.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("USD/JPY", result["message"])


class NoFinancialWriteOnRejectionTests(TestCase):
    """Verificación adicional (no listada, de bajo costo): el rechazo del
    escenario 2 sigue sin escribir nada — mismo contrato que
    test_market_price_unavailable_guard.py."""

    def setUp(self):
        _clear_price("EUR/USD")
        LastKnownMarketPrice.objects.filter(symbol="EUR/USD").delete()

    def test_no_writes_when_closed_market_position_has_no_durable_price(self):
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        positions_before = Position.objects.filter(account=account).count()

        session = make_session(symbol="EUR/USD")
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = _db_open_sync(
                _consumer(account.pk), "BTCUSD", "buy", 0.01, 63000.0, None, None,
                commission=5.0, new_balance=9995.0,
            )
        self.assertFalse(result["ok"])
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10000.00"))
        self.assertEqual(Position.objects.filter(account=account).count(), positions_before)
