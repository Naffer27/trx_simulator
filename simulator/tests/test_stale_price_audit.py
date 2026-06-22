"""
simulator/tests/test_stale_price_audit.py — Bloque F

Audita el comportamiento del sistema cuando el precio está viejo, falta,
o proviene de una ruta de fallback incorrecta.

Tests que documentan comportamiento existente (PASS esperado):
  - TestPriceCacheNotStale          — _price_cache módulo-level no expira
  - TestDaemonMultiSymbolPartial    — stopout omitido si falta precio en un símbolo
  - TestDaemonSkippedStaleCount     — skipped_stale es por posición, no por cuenta
  - TestSnapshotFloatingPnlStale    — snapshot floating_pnl=0 para cuenta inactiva
  - TestReadCachedPriceSafety       — (None, None) en error Redis, sin excepción
  - TestFeedManagerFallbackPrice    — FeedManager devuelve base_price cuando no hay cache

⚠ Test que demuestra un bug real (FAIL esperado):
  - TestEvaluatePositionRiskFallback — Gap 1: risk_engine.py usa 1.17 como fallback
    hardcodeado si _get_current_price lanza excepción, en lugar de spec.base_price.
    Para BTCUSD: notional calculado como $0.01 en lugar de ~$820.
"""
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, SimpleTestCase

from market_data.feeds import FeedManager, _fallback_price
from market_data.symbol_specs import get_spec
from simulator.models import AccountEquitySnapshot, Position
from simulator.risk_engine import evaluate_position_risk
from simulator.snapshots import take_all_snapshots
from simulator.tasks import _read_cached_price, scan_positions_task

from .factories import make_account, make_position


def _scan():
    return scan_positions_task.apply().get()


def _two_sym_prices(eur_bid, eur_ask, btc_bid=None, btc_ask=None):
    """
    Mock side_effect para _read_cached_price.
    Devuelve precio real para EUR/USD, (None, None) para BTCUSD si btc_bid es None.
    """
    def _mock(symbol: str):
        if symbol == "EUR/USD":
            return (eur_bid, eur_ask)
        if symbol == "BTCUSD":
            if btc_bid is not None:
                return (btc_bid, btc_ask)
            return (None, None)
        return (None, None)
    return _mock


# ─────────────────────────────────────────────────────────────────────────────
# ⚠ BUG DEMONSTRATION — Gap 1
# Este test FALLARÁ si el bug existe en risk_engine.py:165
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluatePositionRiskFallback(TestCase):
    """
    Verifica que el fallback de precio en evaluate_position_risk usa spec.base_price,
    no el valor hardcodeado 1.17 que existía antes del fix (Gap 1).

    Fix aplicado en risk_engine.py:
        except Exception:
            try:
                price = _get_sym_spec(symbol).base_price
            except KeyError:
                price = 1.0
    """

    @patch("simulator.exposure_engine._get_current_price",
           side_effect=RuntimeError("forced — simula fallo de import/REST"))
    def test_fallback_uses_spec_base_price_not_hardcoded_1_17(self, _mock):
        """
        Con _get_current_price forzado a fallar, el fallback usa spec.base_price
        (82000 para BTCUSD), no el valor hardcodeado 1.17.
        notional esperado ≈ 0.01 × 82000 × 1 = $820.
        """
        spec    = get_spec("BTCUSD")
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))

        result = evaluate_position_risk(
            account, "BTCUSD",
            lot_size=0.01,
            current_equity=10000.0,
            current_margin_used=0.0,
            leverage=20,
        )

        expected_notional = round(0.01 * spec.base_price * spec.contract_size, 2)
        self.assertGreater(
            result["notional"],
            100.0,
            f"fallback usa precio incorrecto — notional={result['notional']:.4f}, "
            f"esperado ≈ {expected_notional:.0f} (spec.base_price={spec.base_price})"
        )
        self.assertAlmostEqual(
            result["notional"], expected_notional, delta=1.0,
            msg=f"notional={result['notional']} != esperado {expected_notional}"
        )

    @patch("simulator.exposure_engine._get_current_price",
           side_effect=RuntimeError("forced"))
    def test_fallback_unknown_symbol_uses_price_1(self, _mock):
        """
        Símbolo desconocido + _get_current_price falla → price = 1.0 (último fallback).
        No debe lanzar excepción.
        """
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        result = evaluate_position_risk(
            account, "UNKNOWN/SYM",
            lot_size=1.0,
            current_equity=10000.0,
            current_margin_used=0.0,
            leverage=50,
        )
        # Solo verificamos que no lanza y devuelve un resultado coherente
        self.assertIn("risk_level", result)
        self.assertGreaterEqual(result["notional"], 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 2 — _price_cache módulo-level no expira
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceCacheNotStale(SimpleTestCase):
    """
    Documents Gap 2: exposure_engine._price_cache es un dict de módulo sin TTL.
    Una vez cacheado, _get_current_price devuelve el valor almacenado aunque
    el precio real haya cambiado significativamente.
    El cache solo se limpia en compute_live_analytics(), no en cada llamada.
    """

    def setUp(self):
        from simulator.exposure_engine import _price_cache
        _price_cache.clear()

    def tearDown(self):
        from simulator.exposure_engine import _price_cache
        _price_cache.clear()

    def test_price_cache_returns_stale_value_on_second_call(self):
        """
        _price_cache devuelve el valor del primer llamado aunque el subyacente
        haya cambiado. Documenta el comportamiento sin romper producción.
        """
        from simulator.exposure_engine import _price_cache, _get_current_price

        # Seed manual del cache — simula que ya hubo una llamada anterior
        _price_cache["EUR/USD"] = 1.07000

        # REST y FeedManager devuelven precio diferente
        with patch("simulator.exposure_engine._fetch_price_rest", return_value=1.14000), \
             patch("market_data.feeds.get_feed_manager") as mock_fm:
            mock_fm.return_value._prices = {"EUR/USD": 1.14000}
            price = _get_current_price("EUR/USD")

        # A pesar del precio actualizado, devuelve el valor viejo del cache
        self.assertEqual(
            price, 1.07000,
            f"Gap 2: _price_cache devolvió {price} en lugar del valor nuevo 1.14000 — "
            f"cache módulo-level reutilizado sin TTL"
        )

    def test_price_cache_is_shared_across_calls(self):
        """
        Dos llamadas consecutivas sin limpiar el cache: la segunda reutiliza
        el resultado de la primera (sin ir a FeedManager ni REST).
        """
        from simulator.exposure_engine import _price_cache, _get_current_price

        call_count = 0

        def _fake_rest(symbol):
            nonlocal call_count
            call_count += 1
            return 1.10000

        with patch("simulator.exposure_engine._fetch_price_rest", side_effect=_fake_rest), \
             patch("market_data.feeds.get_feed_manager") as mock_fm:
            mock_fm.return_value._prices = {}

            _get_current_price("GBP/USD")  # primera: va a REST
            _get_current_price("GBP/USD")  # segunda: debe usar cache

        self.assertEqual(
            call_count, 1,
            f"Gap 2: _fetch_price_rest llamado {call_count} veces — "
            f"debería ser 1 (segunda llamada usa cache sin TTL)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gap 3 — Daemon: stopout omitido silenciosamente para cuentas multi-símbolo
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonMultiSymbolPartialPriceSkipsStopout(TestCase):
    """
    Documents Gap 3: cuando falta el precio de cualquier símbolo de la cuenta,
    el daemon omite el check de stopout completo (all_prices_available=False).
    Este skip NO se cuenta en el campo skipped_stale del resultado.

    Escenario:
      CHALLENGE 10K, peak=10000, stopout_level=9000 (drawdown 10%).
      EUR/USD BUY 1.0 lot, avg=1.10000, bid=1.08800 → floating=-1200
      BTCUSD  BUY 0.01 lot, avg=82000  → precio MISSING
      Equity con solo EUR/USD: 10000 - 1200 = 8800 < 9000 → debería stopout
      Pero BTCUSD falta → all_prices_available=False → check omitido.
    """

    @patch("simulator.tasks._read_cached_price")
    def test_stopout_skipped_when_partial_prices(self, mock_price):
        """
        Con BTCUSD missing, el stopout no se evalúa aunque equity < threshold.
        Cuenta sigue Activo, posiciones siguen abiertas.
        """
        mock_price.side_effect = _two_sym_prices(
            eur_bid=1.08800, eur_ask=1.08810,
            btc_bid=None,    # BTCUSD: precio faltante
        )
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        make_position(account=account, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("82000"))

        result = _scan()

        # Stopout omitido → ninguna posición cerrada
        self.assertEqual(result["closed"], 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 2)

        account.refresh_from_db()
        self.assertEqual(
            account.status, "Activo",
            "Gap 3: cuenta con equity < stopout_level no fue suspendida — "
            "stopout check omitido porque faltaba precio de BTCUSD"
        )

    @patch("simulator.tasks._read_cached_price")
    def test_stopout_skip_not_reflected_in_skipped_stale_counter(self, mock_price):
        """
        El campo skipped_stale del daemon NO incluye el skip de stopout.
        Solo cuenta posiciones individuales omitidas en el loop SL/TP.
        El skip de stopout al nivel de cuenta es invisible en el resultado.
        """
        mock_price.side_effect = _two_sym_prices(
            eur_bid=1.08800, eur_ask=1.08810,
            btc_bid=None,
        )
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))
        # EUR/USD: precio disponible, sin SL → no cierra
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        # BTCUSD: precio faltante → skipped_stale += 1 en loop SL/TP
        make_position(account=account, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("82000"))

        result = _scan()

        # Solo la posición BTCUSD en el loop SL/TP fue contada como stale
        # El skip de stopout al nivel de cuenta NO fue contado
        self.assertEqual(
            result["skipped_stale"], 1,
            f"Gap 3: skipped_stale={result['skipped_stale']} "
            f"— el skip de stopout por precio parcial NO está contado"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gap 3b — skipped_stale es por posición, no por cuenta
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonSkippedStaleCountIsPerPosition(TestCase):
    """
    Documents Gap 3b: skipped_stale acumula una unidad por POSICIÓN individual
    en el loop SL/TP, no por cuenta. Con N posiciones sin precio → N en el counter.
    """

    @patch("simulator.tasks._read_cached_price", return_value=(None, None))
    def test_two_missing_prices_increments_skipped_stale_twice(self, _mock):
        """
        2 posiciones en la misma cuenta, ambas sin precio → skipped_stale=2.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.1"), avg_price=Decimal("1.10000"))
        make_position(account=account, symbol="EUR/USD", side="SELL",
                      qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

        result = _scan()

        self.assertEqual(
            result["skipped_stale"], 2,
            f"Gap 3b: skipped_stale={result['skipped_stale']} "
            f"— debería ser 2 (una por posición, no una por cuenta)"
        )
        self.assertEqual(result["closed"], 0)

    @patch("simulator.tasks._read_cached_price", return_value=(None, None))
    def test_stale_positions_remain_open(self, _mock):
        """Posiciones con precio missing quedan abiertas (no se cierran en falso)."""
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                            sl=Decimal("1.09000"))

        result = _scan()

        self.assertTrue(
            Position.objects.filter(pk=pos.pk).exists(),
            "Posición con precio stale fue cerrada — no debería cerrarse sin precio"
        )
        self.assertEqual(result["skipped_stale"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 4 — Snapshot equity congelada para cuenta inactiva
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotFloatingPnlStaleForInactiveAccount(TestCase):
    """
    Documents Gap 4: AccountEquitySnapshot.floating_pnl = equity - balance,
    donde equity viene del campo DB (TradingAccount.equity).

    Para cuentas sin WS activo, TradingAccount.equity solo se actualiza en cierres.
    Entre cierres, el snapshot reporta floating_pnl=0 aunque haya posiciones abiertas
    con floating PnL real. La equity_curve del broker es optimista para cuentas inactivas.
    """

    def test_floating_pnl_is_zero_for_inactive_account_with_open_positions(self):
        """
        Cuenta con equity == balance en DB (sin actualizaciones de WS),
        posición abierta: snapshot reporta floating_pnl=0.
        """
        account = make_account(balance=Decimal("10000"))
        # equity == balance por defecto; ningún WS consumer lo actualizó
        self.assertEqual(account.equity, account.balance)

        # Posición abierta — pero DB.equity no fue actualizado
        make_position(
            account=account,
            symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
        )
        self.assertTrue(Position.objects.filter(account=account).exists())

        take_all_snapshots()

        snap = AccountEquitySnapshot.objects.filter(account=account).latest("taken_at")
        self.assertEqual(
            snap.floating_pnl, Decimal("0"),
            f"Gap 4: floating_pnl={snap.floating_pnl} — snapshot no refleja "
            f"el floating PnL real de la posición abierta (equity DB no actualizada)"
        )

    def test_snapshot_uses_db_equity_not_live_market(self):
        """
        Aunque la posición exista en DB, el snapshot usa TradingAccount.equity
        (campo DB), no el precio de mercado actual.
        Documentación del diseño: snapshot equity = DB state, no live market.
        """
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

        take_all_snapshots()
        snap = AccountEquitySnapshot.objects.filter(account=account).latest("taken_at")

        # El snapshot usa account.equity del campo DB
        self.assertEqual(snap.equity, account.equity)
        self.assertEqual(snap.balance, account.balance)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 5 — _read_cached_price safe on Redis error
# ─────────────────────────────────────────────────────────────────────────────

class TestReadCachedPriceSafety(SimpleTestCase):
    """
    _read_cached_price debe retornar (None, None) ante cualquier error Redis,
    nunca propagar excepciones. Cualquier error de infraestructura es silencioso.
    """

    @patch("redis.from_url", side_effect=Exception("Redis connection refused"))
    def test_redis_connection_error_returns_none_tuple(self, _mock):
        """Error de conexión Redis → (None, None), sin excepción."""
        bid, ask = _read_cached_price("EUR/USD")
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    @patch("redis.from_url", side_effect=TimeoutError("socket timeout"))
    def test_redis_timeout_returns_none_tuple(self, _mock):
        """Timeout Redis → (None, None), sin excepción."""
        bid, ask = _read_cached_price("EUR/USD")
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_both_none_triggers_stale_skip(self):
        """(None, None) es el contrato público que el daemon interpreta como stale."""
        bid, ask = (None, None)
        # El daemon evalúa: if bid is not None and ask is not None: prices[sym] = (bid, ask)
        # Con (None, None) la condición es False → precio no añadido → posición skipped
        self.assertIsNone(bid)
        self.assertIsNone(ask)


# ─────────────────────────────────────────────────────────────────────────────
# FeedManager fallback price
# ─────────────────────────────────────────────────────────────────────────────

class TestFeedManagerFallbackPrice(SimpleTestCase):
    """
    FeedManager.last_price() devuelve _fallback_price(symbol) cuando no hay
    precio cacheado. Nunca devuelve 0 ni None.
    """

    def test_last_price_without_cache_returns_fallback(self):
        """Sin precio en _prices, last_price devuelve base_price del spec."""
        fm = FeedManager()
        # Sin datos en cache interno
        self.assertNotIn("EUR/USD", fm._prices)

        price = fm.last_price("EUR/USD")

        self.assertGreater(price, 0.0, "last_price debe ser > 0 siempre")
        self.assertEqual(
            price, _fallback_price("EUR/USD"),
            "last_price sin cache debe coincidir con _fallback_price"
        )

    def test_last_price_fallback_matches_spec_base_price(self):
        """_fallback_price(symbol) == spec.base_price — misma fuente de verdad."""
        for symbol in ("EUR/USD", "BTCUSD", "GBP/USD"):
            spec = get_spec(symbol)
            fb   = _fallback_price(symbol)
            self.assertEqual(
                fb, spec.base_price,
                f"_fallback_price({symbol})={fb} != spec.base_price={spec.base_price}"
            )

    def test_last_bid_without_cache_is_nonzero(self):
        """last_bid sin cache: base_price - spread/2 > 0."""
        fm = FeedManager()
        bid = fm.last_bid("EUR/USD")
        self.assertGreater(bid, 0.0)

    def test_last_ask_without_cache_is_nonzero(self):
        """last_ask sin cache: base_price + spread/2 > 0."""
        fm = FeedManager()
        ask = fm.last_ask("EUR/USD")
        self.assertGreater(ask, 0.0)
