"""
simulator/tests/test_runtime_instrument_catalog.py — FOUNDATION-12.

Covers simulator/runtime_instrument_catalog.py::check_runtime_catalog_drift():
the feature flag gate (zero DB access when off), drift detection against
real seeded data, logging, and that it never raises.
"""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from simulator.models import Instrument
from simulator.runtime_instrument_catalog import (
    check_runtime_catalog_drift,
    runtime_catalog_drift_check_enabled,
)


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


class FeatureFlagDefaultsTests(TestCase):
    def test_flag_defaults_false(self):
        self.assertFalse(runtime_catalog_drift_check_enabled())

    def test_flag_off_returns_none_without_touching_db(self):
        make_instrument(symbol="EURUSD", display_name="EUR/USD", base_currency="EUR")
        with self.assertNumQueries(0):
            result = check_runtime_catalog_drift("EUR/USD")
        self.assertIsNone(result)


@override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
class DriftDetectionTests(TestCase):
    def test_flag_on_no_matching_instrument_row_returns_none(self):
        # No Instrument seeded at all — nothing to compare against.
        result = check_runtime_catalog_drift("BTCUSD")
        self.assertIsNone(result)

    def test_real_seeded_catalog_matches_symbol_by_symbol(self):
        call_command("seed_instruments", stdout=StringIO())
        report = check_runtime_catalog_drift("EUR/USD")
        self.assertIsNotNone(report)
        self.assertEqual(report.critical_differences, ())

    def test_detects_a_real_critical_drift(self):
        make_instrument(
            symbol="EURUSD", display_name="EUR/USD", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True, contract_size=Decimal("1.0000"),  # wildly different
        )
        report = check_runtime_catalog_drift("EUR/USD")
        self.assertIsNotNone(report)
        fields = {d.field for d in report.critical_differences}
        self.assertIn("contract_size", fields)

    def test_never_raises_on_internal_error(self):
        from unittest.mock import patch
        make_instrument(symbol="EURUSD", display_name="EUR/USD", base_currency="EUR")
        with patch(
            "simulator.runtime_instrument_catalog.compare_runtime_instrument",
            side_effect=RuntimeError("boom"),
        ):
            result = check_runtime_catalog_drift("EUR/USD")  # must not raise
        self.assertIsNone(result)

    def test_symbol_normalization_finds_the_compact_db_row(self):
        make_instrument(
            symbol="BTCUSD", display_name="BTC/USD", asset_class=Instrument.ASSET_CRYPTO,
            base_currency="BTC", contract_size=Decimal("1.0000"),
            market_data_provider=Instrument.PROVIDER_BINANCE, provider_symbol="BTCUSDT",
            trading_enabled=True, session="24/7",
        )
        report = check_runtime_catalog_drift("BTCUSD")  # canonical, no slash for crypto
        self.assertIsNotNone(report)


@override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
class LoggingTests(TestCase):
    def test_critical_drift_logs_a_warning(self):
        make_instrument(
            symbol="EURUSD", display_name="EUR/USD", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True, contract_size=Decimal("1.0000"),
        )
        with self.assertLogs("simulator.ws", level="WARNING") as captured:
            check_runtime_catalog_drift("EUR/USD")
        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_runtime_catalog_drift", joined)
        self.assertIn("symbol=EUR/USD", joined)
        self.assertIn("critical=1", joined)

    def test_no_drift_does_not_log(self):
        call_command("seed_instruments", stdout=StringIO())
        with self.assertNoLogs("simulator.ws", level="WARNING"):
            check_runtime_catalog_drift("EUR/USD")

    def test_log_contains_no_secrets(self):
        make_instrument(
            symbol="EURUSD", display_name="EUR/USD", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True, contract_size=Decimal("1.0000"),
        )
        with self.assertLogs("simulator.ws", level="WARNING") as captured:
            check_runtime_catalog_drift("EUR/USD")
        lowered = "\n".join(captured.output).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, lowered)


class ReadOnlyGuaranteeTests(TestCase):
    @override_settings(MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED=True)
    def test_never_writes_to_instrument_table(self):
        call_command("seed_instruments", stdout=StringIO())
        before = list(Instrument.objects.values_list("symbol", "updated_at").order_by("symbol"))
        check_runtime_catalog_drift("EUR/USD")
        check_runtime_catalog_drift("BTCUSD")
        check_runtime_catalog_drift("UNKNOWN_SYMBOL")
        after = list(Instrument.objects.values_list("symbol", "updated_at").order_by("symbol"))
        self.assertEqual(before, after)
