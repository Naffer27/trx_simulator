"""
simulator/tests/test_market_data_observability.py — FOUNDATION-13.

Covers simulator/market_data_observability.py: the Django/DB-aware
orchestration that combines settings + the FOUNDATION-12 catalog drift
check with the pure market_data.observability.build_snapshot(). Never
raises; the catalog-drift flag gate still means zero DB queries when off.
"""

import pathlib
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from market_data.observability import CatalogDriftLevel, reset_observability_state
from market_data.runtime_router.state import reset_router_state
from simulator.market_data_observability import (
    catalog_drift_level,
    get_all_market_data_health_snapshots,
    get_market_data_health_snapshot,
    router_allowlisted,
    router_enabled,
    stale_after_seconds,
)
from simulator.models import Instrument


def make_instrument(**overrides):
    defaults = dict(
        symbol="TESTUSD", display_name="TEST/USD", asset_class=Instrument.ASSET_FOREX,
        base_currency="TEST", quote_currency="USD",
        pip_size=Decimal("0.0001"), tick_size=Decimal("0.00001"), price_decimals=5,
        lot_step=Decimal("0.01"), min_lot=Decimal("0.01"), max_lot=Decimal("100.00"),
        contract_size=Decimal("100000.0000"), default_spread=Decimal("1.5000"),
        spread_unit=Instrument.SPREAD_PIPS, commission_per_lot=Decimal("0.00"),
        commission_pct=Decimal("0.000000"), max_leverage=500,
        margin_mode=Instrument.MARGIN_LEVERAGE, pnl_mode=Instrument.PNL_STANDARD,
        market_data_provider=Instrument.PROVIDER_SIM, provider_symbol="",
        trading_enabled=False, session="24/5",
    )
    defaults.update(overrides)
    return Instrument.objects.create(**defaults)


class _IsolatedTestCase(TestCase):
    def setUp(self):
        reset_observability_state()
        reset_router_state()


class SettingsReadTests(_IsolatedTestCase):
    @override_settings(MARKET_DATA_ROUTER_ENABLED=True)
    def test_router_enabled_reads_setting(self):
        self.assertTrue(router_enabled())

    def test_router_enabled_defaults_false(self):
        self.assertFalse(router_enabled())

    @override_settings(MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_router_allowlisted_true_for_allowlisted_symbol(self):
        self.assertTrue(router_allowlisted("BTCUSD"))
        self.assertFalse(router_allowlisted("ETHUSD"))

    def test_stale_after_seconds_reuses_price_cache_ttl(self):
        from market_data.feeds import _PRICE_CACHE_TTL
        self.assertEqual(stale_after_seconds(), float(_PRICE_CACHE_TTL))


class CatalogDriftLevelTests(_IsolatedTestCase):
    def test_flag_off_is_not_checked_with_zero_queries(self):
        make_instrument(symbol="EURUSD", display_name="EUR/USD", base_currency="EUR")
        with self.assertNumQueries(0):
            level = catalog_drift_level("EUR/USD")
        self.assertEqual(level, CatalogDriftLevel.NOT_CHECKED)

    @override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
    def test_flag_on_no_matching_row_is_no_data(self):
        self.assertEqual(catalog_drift_level("BTCUSD"), CatalogDriftLevel.NO_DATA)

    @override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
    def test_flag_on_matching_catalog_is_match(self):
        from io import StringIO
        from django.core.management import call_command
        call_command("seed_instruments", stdout=StringIO())
        self.assertEqual(catalog_drift_level("EUR/USD"), CatalogDriftLevel.MATCH)

    @override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
    def test_flag_on_critical_drift(self):
        make_instrument(
            symbol="EURUSD", display_name="EUR/USD", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True, contract_size=Decimal("1.0000"),
        )
        self.assertEqual(catalog_drift_level("EUR/USD"), CatalogDriftLevel.CRITICAL)

    @override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
    def test_internal_error_is_unavailable_not_raised(self):
        with patch(
            "simulator.runtime_instrument_catalog.check_runtime_catalog_drift",
            side_effect=RuntimeError("boom"),
        ):
            level = catalog_drift_level("EUR/USD")  # must not raise
        self.assertEqual(level, CatalogDriftLevel.UNAVAILABLE)


class GetMarketDataHealthSnapshotTests(_IsolatedTestCase):
    def test_returns_a_snapshot_for_a_known_symbol(self):
        snap = get_market_data_health_snapshot("BTCUSD")
        self.assertEqual(snap.canonical_symbol, "BTCUSD")
        self.assertEqual(snap.catalog_drift_level, CatalogDriftLevel.NOT_CHECKED)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_router_flags_flow_through(self):
        snap = get_market_data_health_snapshot("BTCUSD")
        self.assertTrue(snap.router_enabled)
        self.assertTrue(snap.router_allowlisted)

    def test_never_raises_when_settings_gathering_fails(self):
        with patch("simulator.market_data_observability.router_enabled", side_effect=RuntimeError("boom")):
            snap = get_market_data_health_snapshot("BTCUSD")  # must not raise
        self.assertEqual(snap.canonical_symbol, "BTCUSD")
        self.assertFalse(snap.router_enabled)

    def test_unknown_symbol_does_not_raise(self):
        snap = get_market_data_health_snapshot("NOT_A_REAL_SYMBOL")
        self.assertEqual(snap.canonical_symbol, "NOT_A_REAL_SYMBOL")


class GetAllMarketDataHealthSnapshotsTests(_IsolatedTestCase):
    def test_returns_one_snapshot_per_registered_symbol(self):
        from market_data.symbol_specs import get_all_specs
        snaps = get_all_market_data_health_snapshots()
        self.assertEqual(len(snaps), len(get_all_specs()))

    def test_includes_btcusd_and_ethusd(self):
        symbols = {s.canonical_symbol for s in get_all_market_data_health_snapshots()}
        self.assertIn("BTCUSD", symbols)
        self.assertIn("ETHUSD", symbols)


class EnvTemplatesDocumentedTests(SimpleTestCase):
    def test_env_example_documents_observability_flag(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        content = (repo_root / ".env.example").read_text()
        self.assertIn("MARKET_DATA_OBSERVABILITY_ENABLED=False", content)

    def test_staging_template_documents_observability_flag(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        content = (repo_root / "deploy" / ".env.staging.template").read_text()
        self.assertIn("MARKET_DATA_OBSERVABILITY_ENABLED=False", content)
