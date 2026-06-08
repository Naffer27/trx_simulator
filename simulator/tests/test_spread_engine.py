"""
simulator/tests/test_spread_engine.py — Bloque 3

Cubre: broker_price y calculate_spread_revenue en spread_engine.py

Convenciones:
  - El módulo spread_engine tiene un _cache dict a nivel de módulo con TTL de 30s.
    Se limpia en setUp()/tearDown() de cada clase para que tests no se contaminen
    entre sí ni contra el TTL real.
  - Los valores esperados se derivan de los specs REALES del registro (symbol_specs.py):
      EUR/USD: pip_size=0.0001, price_decimals=5, contract_size=100_000
      BTCUSD : pip_size=1.0,    price_decimals=2, contract_size=1.0
  - broker_price devuelve floats → assertAlmostEqual con places=5 (forex) o places=2 (crypto).
  - calculate_spread_revenue devuelve float → comparación directa con round().
"""
import simulator.spread_engine as _spread_mod
from django.test import TestCase
from decimal import Decimal

from simulator.spread_engine import broker_price, calculate_spread_revenue

from .factories import make_spread_config


def _clear_cache():
    """Clear the module-level spread config cache."""
    _spread_mod._cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# broker_price
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerPriceWithConfig(TestCase):
    """Tests that require a BrokerSpreadConfig row in DB."""

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_applies_half_spread_symmetrically_eurusd(self):
        """
        EUR/USD: spread_pips=2 → extra = 2 × 0.0001 / 2 = 0.0001 per side.
        bid baja 0.0001, ask sube 0.0001.
        """
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))

        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        # extra = 0.0001 per side
        self.assertAlmostEqual(client_bid, 1.10000 - 0.0001, places=5)
        self.assertAlmostEqual(client_ask, 1.10100 + 0.0001, places=5)

    def test_spread_widens_not_shifts(self):
        """
        El mid-price NO cambia: (client_bid + client_ask) / 2 ≈ (bid + ask) / 2.
        Solo el spread se amplía simétricamente.
        """
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))

        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        raw_mid    = (bid + ask) / 2
        client_mid = (client_bid + client_ask) / 2
        self.assertAlmostEqual(raw_mid, client_mid, places=5)

    def test_btcusd_spread_in_usd(self):
        """
        BTCUSD: pip_size=1.0 → 15-pip spread = $15 total, $7.5 per side.
        client_bid = raw_bid − 7.5 ; client_ask = raw_ask + 7.5
        """
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"))

        bid, ask = 82000.00, 82015.00
        client_bid, client_ask = broker_price("BTCUSD", bid, ask)

        self.assertAlmostEqual(client_bid, 82000.00 - 7.5, places=2)
        self.assertAlmostEqual(client_ask, 82015.00 + 7.5, places=2)

    def test_normalization_eurusd_equals_eur_slash_usd(self):
        """
        'EURUSD' y 'EUR/USD' deben producir exactamente el mismo resultado.
        normalize_symbol('EURUSD') → 'EUR/USD' (canonical form).
        La config se crea como 'EUR/USD' (normalizada por BrokerSpreadConfig.save()).
        """
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))

        bid, ask = 1.10000, 1.10100
        result_slash  = broker_price("EUR/USD", bid, ask)
        result_noslash = broker_price("EURUSD",  bid, ask)

        self.assertEqual(result_slash, result_noslash)

    def test_disabled_config_passthrough(self):
        """BrokerSpreadConfig con enabled=False → passthrough, sin markup."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("5.00"), enabled=False)

        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        self.assertEqual(client_bid, bid)
        self.assertEqual(client_ask, ask)


class TestBrokerPriceNoConfig(TestCase):
    """Tests that run without any BrokerSpreadConfig rows."""

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_passthrough_without_config(self):
        """Sin BrokerSpreadConfig para el símbolo → bid/ask sin cambio."""
        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        self.assertEqual(client_bid, bid)
        self.assertEqual(client_ask, ask)

    def test_unknown_symbol_no_exception_returns_passthrough(self):
        """
        Símbolo completamente desconocido (no en el registry) → sin excepción.
        El spread engine captura internamente el KeyError de get_spec y devuelve passthrough.
        """
        bid, ask = 1.0, 1.1
        client_bid, client_ask = broker_price("XXXXXX", bid, ask)

        # No debe lanzar excepción y debe devolver los valores originales
        self.assertEqual(client_bid, bid)
        self.assertEqual(client_ask, ask)


# ─────────────────────────────────────────────────────────────────────────────
# calculate_spread_revenue
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateSpreadRevenue(TestCase):
    """
    calculate_spread_revenue no usa DB — solo symbol_specs registry.
    No requiere limpiar _cache.
    Fórmula: (spread_pips × pip_size / 2) × qty × contract_size
    """

    def test_eurusd_revenue_2pips_point1_lot(self):
        """
        EUR/USD: spread_pips=2, qty=0.1
        half_spread = 2 × 0.0001 / 2 = 0.0001
        revenue     = 0.0001 × 0.1 × 100_000 = 1.0
        """
        revenue = calculate_spread_revenue("EUR/USD", qty=0.1, spread_pips=2.0)
        self.assertAlmostEqual(revenue, 1.0, places=6)

    def test_eurusd_revenue_1pip_1lot(self):
        """
        EUR/USD: spread_pips=1, qty=1.0
        half_spread = 0.00005
        revenue     = 0.00005 × 1.0 × 100_000 = 5.0
        """
        revenue = calculate_spread_revenue("EUR/USD", qty=1.0, spread_pips=1.0)
        self.assertAlmostEqual(revenue, 5.0, places=6)

    def test_btcusd_revenue_15pips_point01_lot(self):
        """
        BTCUSD: spread_pips=15, qty=0.01
        half_spread = 15 × 1.0 / 2 = 7.5
        revenue     = 7.5 × 0.01 × 1.0 = 0.075
        """
        revenue = calculate_spread_revenue("BTCUSD", qty=0.01, spread_pips=15.0)
        self.assertAlmostEqual(revenue, 0.075, places=6)

    def test_unknown_symbol_returns_zero(self):
        """Símbolo desconocido → 0.0 sin excepción (KeyError capturado internamente)."""
        revenue = calculate_spread_revenue("XXXXXX", qty=1.0, spread_pips=2.0)
        self.assertEqual(revenue, 0.0)

    def test_zero_spread_pips_returns_zero(self):
        """spread_pips=0 → revenue = 0.0 para cualquier símbolo válido."""
        revenue = calculate_spread_revenue("EUR/USD", qty=1.0, spread_pips=0.0)
        self.assertEqual(revenue, 0.0)

    def test_zero_qty_returns_zero(self):
        """qty=0 → revenue = 0.0."""
        revenue = calculate_spread_revenue("EUR/USD", qty=0.0, spread_pips=2.0)
        self.assertEqual(revenue, 0.0)

    def test_revenue_scales_linearly_with_qty(self):
        """Duplicar qty debe duplicar revenue exactamente."""
        rev1 = calculate_spread_revenue("EUR/USD", qty=0.1, spread_pips=2.0)
        rev2 = calculate_spread_revenue("EUR/USD", qty=0.2, spread_pips=2.0)
        self.assertAlmostEqual(rev2, rev1 * 2, places=8)


# ─────────────────────────────────────────────────────────────────────────────
# broker_price — markup_pips (account-level additive spread)
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerPriceMarkup(TestCase):
    """
    Verifies the account-level spread markup (spread_pips_snapshot) wired into
    broker_price() via markup_pips parameter.

    Model: effective_pips = BrokerSpreadConfig.spread_pips + markup_pips
    """

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_old_account_zero_markup_unchanged_vs_config_only(self):
        """Old account (NULL snapshot) → markup_pips=0.0 → identical to legacy call."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        bid, ask = 1.10000, 1.10100

        legacy  = broker_price("EUR/USD", bid, ask)
        with_zero = broker_price("EUR/USD", bid, ask, markup_pips=0.0)

        self.assertEqual(legacy, with_zero)

    def test_ecn_markup_zero_uses_base_spread_only(self):
        """
        ECN product: markup_pips=0.0, BrokerSpreadConfig=2 pips.
        effective = 2 pips → only base spread applied, no extra.
        """
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        bid, ask = 1.10000, 1.10100

        client_bid, client_ask = broker_price("EUR/USD", bid, ask, markup_pips=0.0)

        # extra = 2 × 0.0001 / 2 = 0.0001 per side
        self.assertAlmostEqual(client_bid, bid - 0.0001, places=5)
        self.assertAlmostEqual(client_ask, ask + 0.0001, places=5)

    def test_standard_markup_adds_to_base_spread(self):
        """
        Standard product: markup_pips=1.0 over 2-pip base → effective=3 pips.
        extra = 3 × 0.0001 / 2 = 0.00015 per side.
        """
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        bid, ask = 1.10000, 1.10100

        client_bid, client_ask = broker_price("EUR/USD", bid, ask, markup_pips=1.0)

        self.assertAlmostEqual(client_bid, bid - 0.00015, places=5)
        self.assertAlmostEqual(client_ask, ask + 0.00015, places=5)

    def test_no_config_markup_zero_passthrough(self):
        """No BrokerSpreadConfig + markup_pips=0.0 → passthrough (original behaviour)."""
        bid, ask = 1.10000, 1.10100

        client_bid, client_ask = broker_price("EUR/USD", bid, ask, markup_pips=0.0)

        self.assertEqual(client_bid, bid)
        self.assertEqual(client_ask, ask)

    def test_no_config_markup_positive_applies_markup_only(self):
        """
        No BrokerSpreadConfig + markup_pips=1.5 → applies markup alone.
        EUR/USD: extra = 1.5 × 0.0001 / 2 = 0.000075 per side.
        Result is rounded to price_decimals=5.
        """
        bid, ask = 1.10000, 1.10100

        client_bid, client_ask = broker_price("EUR/USD", bid, ask, markup_pips=1.5)

        # round(1.10000 − 0.000075, 5) = 1.09993
        self.assertAlmostEqual(client_bid, round(bid - 0.000075, 5), places=5)
        self.assertAlmostEqual(client_ask, round(ask + 0.000075, 5), places=5)

    def test_btcusd_markup_coherent_with_pip_size(self):
        """
        BTCUSD pip_size=1.0: markup_pips=1.2 → extra = 1.2 × 1.0 / 2 = $0.60 per side.
        With BrokerSpreadConfig=15 pips: effective=16.2 → extra = 16.2 × 1.0 / 2 = $8.10.
        """
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"))
        bid, ask = 82000.00, 82015.00

        client_bid, client_ask = broker_price("BTCUSD", bid, ask, markup_pips=1.2)

        # effective = 16.2 pips × 1.0 pip_size / 2 = 8.10 per side
        self.assertAlmostEqual(client_bid, bid - 8.10, places=2)
        self.assertAlmostEqual(client_ask, ask + 8.10, places=2)

    def test_btcusd_markup_only_no_config(self):
        """BTCUSD no config + markup_pips=1.2 → extra = 1.2/2 = $0.60 per side."""
        bid, ask = 82000.00, 82015.00

        client_bid, client_ask = broker_price("BTCUSD", bid, ask, markup_pips=1.2)

        self.assertAlmostEqual(client_bid, bid - 0.60, places=2)
        self.assertAlmostEqual(client_ask, ask + 0.60, places=2)

    def test_mid_price_preserved_with_markup(self):
        """Markup widens spread symmetrically; mid-price is unchanged."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        bid, ask = 1.10000, 1.10100

        client_bid, client_ask = broker_price("EUR/USD", bid, ask, markup_pips=1.0)

        raw_mid    = (bid + ask) / 2
        client_mid = (client_bid + client_ask) / 2
        self.assertAlmostEqual(raw_mid, client_mid, places=5)

    def test_effective_spread_revenue_base_plus_markup(self):
        """
        Revenue must reflect effective total (base + markup), not base alone.
        EUR/USD: base=2 pips, markup=1 pip → effective=3 pips, qty=1.0 lot.
        half_spread = 3 × 0.0001 / 2 = 0.00015
        revenue     = 0.00015 × 1.0 × 100_000 = 15.0
        """
        base_rev     = calculate_spread_revenue("EUR/USD", qty=1.0, spread_pips=2.0)
        effective_rev = calculate_spread_revenue("EUR/USD", qty=1.0, spread_pips=3.0)

        self.assertAlmostEqual(base_rev,      10.0, places=6)
        self.assertAlmostEqual(effective_rev, 15.0, places=6)
        self.assertGreater(effective_rev, base_rev)
