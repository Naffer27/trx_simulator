"""
simulator/tests/test_spread_bounds_optin.py — pre-commit correction to
SPREAD-04.

BrokerSpreadConfig.min_spread/max_spread were defined with economic
defaults (0.50/5.00) back when they were purely decorative (SPREAD-01
finding). SPREAD-04 activated them as a REAL clamp on broker_price() for
the first time — which meant every row that had never had an admin
explicitly configure bounds (i.e. every row in the system) would suddenly
apply that arbitrary default, silently narrowing spreads like BTCUSD's
realistic 15-pip base down to 5. Confirmed before commit, corrected here:
floor/ceiling are now opt-in via BrokerSpreadConfig.spread_bounds_enabled
(default False); min_spread/max_spread are nullable with no default.

Covers the 5 required test categories from the correction spec:
  1. Config histórica/default no cambia BTCUSD.
  2. Bounds desactivados → no clamp (even with real min/max on the row).
  3. Bounds explícitamente activos → floor y ceiling both enforced.
  4. Migración preserva compatibilidad — an "old" row (created before this
     correction, i.e. with no explicit spread_bounds_enabled) behaves
     exactly like a brand new one: no clamp.
  5. Pricing context distingue bounds no aplicados / floor aplicado /
     ceiling aplicado via the new spread_bound_applied field.

Plus: min > max is rejected at the model level (a separate, additional
guarantee from CommercialPricingProfile.__post_init__'s own check).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from simulator import commercial_pricing as cp
from simulator import pricing_context as pc
from simulator.models import BrokerSpreadConfig
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests
from simulator.spread_engine import broker_price

from .factories import make_spread_config


class HistoricalDefaultDoesNotChangeBtcusdTests(TestCase):
    """1) Config histórica/default no cambia BTCUSD."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_btcusd_realistic_spread_unclamped_by_default(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"))
        refresh_cache_sync()

        bid, ask = 82000.00, 82015.00
        client_bid, client_ask = broker_price("BTCUSD", bid, ask)

        # 15 pips × pip_size(1.0) / 2 = $7.50 per side — NOT narrowed to 5.
        self.assertAlmostEqual(client_bid, bid - 7.50, places=2)
        self.assertAlmostEqual(client_ask, ask + 7.50, places=2)

    def test_forex_spread_unclamped_by_default(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        refresh_cache_sync()

        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        self.assertAlmostEqual(client_bid, bid - 0.0001, places=5)
        self.assertAlmostEqual(client_ask, ask + 0.0001, places=5)


class BoundsDisabledMeansNoClampTests(TestCase):
    """2) Bounds desactivados → no clamp, even when the row carries real
    min/max numbers (e.g. an admin filled them in but never flipped the
    flag, or a historical row still has the old 0.50/5.00 default value)."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_real_min_max_on_row_has_no_effect_while_disabled(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=False)
        refresh_cache_sync()

        bid, ask = 82000.00, 82015.00
        client_bid, client_ask = broker_price("BTCUSD", bid, ask)

        self.assertAlmostEqual(client_bid, bid - 7.50, places=2)
        self.assertAlmostEqual(client_ask, ask + 7.50, places=2)

    def test_cached_snapshot_min_max_are_none_while_disabled(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"))
        refresh_cache_sync()
        from simulator.spread_config_cache import get_cached_config
        snap = get_cached_config("BTCUSD")
        self.assertIsNone(snap.min_spread)
        self.assertIsNone(snap.max_spread)


class BoundsEnabledFloorAndCeilingTests(TestCase):
    """3) Bounds explícitamente activos → floor y ceiling."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_floor_applies_when_enabled(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("0.00"),
                            min_spread=Decimal("1.00"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()

        bid, ask = 1.10000, 1.10100
        client_bid, client_ask = broker_price("EUR/USD", bid, ask)

        # floored to 1.00 pip → extra = 0.0001/2 per side
        self.assertAlmostEqual(client_bid, bid - 0.00005, places=5)
        self.assertAlmostEqual(client_ask, ask + 0.00005, places=5)

    def test_ceiling_applies_when_enabled(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()

        bid, ask = 82000.00, 82015.00
        client_bid, client_ask = broker_price("BTCUSD", bid, ask)

        # ceiling to 5.00 pips → extra = 5.00 * 1.0 / 2 = 2.50 per side
        self.assertAlmostEqual(client_bid, bid - 2.50, places=2)
        self.assertAlmostEqual(client_ask, ask + 2.50, places=2)


class MigrationPreservesCompatibilityTests(TestCase):
    """4) Migración preserva compatibilidad — a row saved without
    explicitly setting spread_bounds_enabled (i.e. relying on the model
    default, exactly what every pre-existing row does after the migration
    runs) behaves identically to a brand-new row: no clamp."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_row_created_without_bounds_flag_defaults_to_no_clamp(self):
        row = BrokerSpreadConfig.objects.create(
            symbol="BTCUSD", spread_pips=Decimal("15.00"),
            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
        )
        self.assertFalse(row.spread_bounds_enabled)  # model default, untouched
        self.assertEqual(row.min_spread, Decimal("0.50"))  # stored value intact
        self.assertEqual(row.max_spread, Decimal("5.00"))  # stored value intact

        refresh_cache_sync()
        bid, ask = 82000.00, 82015.00
        client_bid, client_ask = broker_price("BTCUSD", bid, ask)
        self.assertAlmostEqual(client_bid, bid - 7.50, places=2)
        self.assertAlmostEqual(client_ask, ask + 7.50, places=2)

    def test_row_with_null_min_max_is_valid(self):
        row = BrokerSpreadConfig.objects.create(symbol="GBP/USD", spread_pips=Decimal("1.50"))
        self.assertIsNone(row.min_spread)
        self.assertIsNone(row.max_spread)


class ModelLevelMinGreaterThanMaxRejectedTests(TestCase):
    """min > max se rechaza — at the model level, not just the
    CommercialPricingProfile dataclass."""

    def test_min_greater_than_max_raises_on_save(self):
        row = BrokerSpreadConfig(
            symbol="EUR/USD", spread_pips=Decimal("1.00"),
            min_spread=Decimal("5.00"), max_spread=Decimal("1.00"),
            spread_bounds_enabled=True,
        )
        with self.assertRaises(ValidationError):
            row.save()

    def test_min_equal_max_is_allowed(self):
        row = BrokerSpreadConfig.objects.create(
            symbol="EUR/USD", spread_pips=Decimal("1.00"),
            min_spread=Decimal("3.00"), max_spread=Decimal("3.00"),
            spread_bounds_enabled=True,
        )
        self.assertEqual(row.min_spread, row.max_spread)


class PricingContextDistinguishesBoundApplicationTests(TestCase):
    """5) pricing_context distingue: bounds no aplicados / floor aplicado /
    ceiling aplicado — via the new spread_bound_applied field."""

    def test_no_bounds_configured_is_none(self):
        ctx = pc.build_pricing_context(
            base_spread_pips=2.0, account_markup_pips=0.5,
            pricing_profile="ws_manual_open",
        )
        self.assertIsNone(ctx["spread_bound_applied"])
        self.assertEqual(ctx["effective_spread_pips"], ctx["effective_spread_pips_pre_clamp"])

    def test_bounds_configured_but_within_range_is_within_bounds(self):
        ctx = pc.build_pricing_context(
            base_spread_pips=2.0, account_markup_pips=0.5,
            min_spread_pips=0.5, max_spread_pips=5.0,
            pricing_profile="ws_manual_open",
        )
        self.assertEqual(ctx["spread_bound_applied"], "within_bounds")
        self.assertEqual(ctx["effective_spread_pips"], ctx["effective_spread_pips_pre_clamp"])

    def test_floor_applied_is_floor(self):
        ctx = pc.build_pricing_context(
            base_spread_pips=0.0, account_markup_pips=0.0,
            min_spread_pips=1.0, max_spread_pips=5.0,
            pricing_profile="ws_manual_open",
        )
        self.assertEqual(ctx["spread_bound_applied"], "floor")
        self.assertEqual(ctx["effective_spread_pips"], 1.0)
        self.assertNotEqual(ctx["effective_spread_pips"], ctx["effective_spread_pips_pre_clamp"])

    def test_ceiling_applied_is_ceiling(self):
        ctx = pc.build_pricing_context(
            base_spread_pips=15.0, account_markup_pips=0.0,
            min_spread_pips=0.5, max_spread_pips=5.0,
            pricing_profile="ws_manual_open",
        )
        self.assertEqual(ctx["spread_bound_applied"], "ceiling")
        self.assertEqual(ctx["effective_spread_pips"], 5.0)
        self.assertNotEqual(ctx["effective_spread_pips"], ctx["effective_spread_pips_pre_clamp"])

    def test_end_to_end_btcusd_default_shows_no_bound_applied(self):
        """The full pipeline, not just build_pricing_context() in
        isolation: BTCUSD with no explicit bounds must both keep its raw
        15-pip spread AND report spread_bound_applied=None."""
        reset_for_tests()
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"))
        refresh_cache_sync()
        profile = cp.build_commercial_pricing_profile({}, "BTCUSD")
        snapshot = pc.tick_pricing_snapshot("BTCUSD", profile)
        ctx = pc.build_pricing_context(
            base_spread_pips=snapshot["base_spread_pips"],
            account_markup_pips=snapshot["account_markup_pips"],
            min_spread_pips=snapshot["min_spread_pips"],
            max_spread_pips=snapshot["max_spread_pips"],
            pricing_profile="ws_manual_open",
        )
        self.assertEqual(ctx["effective_spread_pips"], 15.0)
        self.assertIsNone(ctx["spread_bound_applied"])
        reset_for_tests()
