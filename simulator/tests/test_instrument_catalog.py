"""
simulator/tests/test_instrument_catalog.py — Bloque A: Instrument Catalog Foundation

Cubre: modelo Instrument (catálogo aditivo, no conectado aún al runtime) y el
management command seed_instruments.

Convenciones:
  - Instrument no tiene lógica de negocio propia todavía — no reemplaza
    market_data/symbol_specs.py. Estos tests solo verifican el modelo y el
    comando de seed, no el runtime de trading.
  - El comando seed_instruments corre contra la DB de tests (aislada), nunca
    contra datos reales.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.test import TestCase

from simulator.models import Instrument


def make_instrument(**overrides):
    defaults = dict(
        symbol="TESTUSD",
        display_name="TEST/USD",
        asset_class=Instrument.ASSET_FOREX,
        base_currency="TEST",
        quote_currency="USD",
        pip_size=Decimal("0.0001"),
        tick_size=Decimal("0.00001"),
        price_decimals=5,
        lot_step=Decimal("0.01"),
        min_lot=Decimal("0.01"),
        max_lot=Decimal("100.00"),
        contract_size=Decimal("100000.0000"),
        default_spread=Decimal("1.5000"),
        spread_unit=Instrument.SPREAD_PIPS,
        commission_per_lot=Decimal("0.00"),
        commission_pct=Decimal("0.000000"),
        max_leverage=500,
        margin_mode=Instrument.MARGIN_LEVERAGE,
        pnl_mode=Instrument.PNL_STANDARD,
        market_data_provider=Instrument.PROVIDER_SIM,
        provider_symbol="",
        trading_enabled=False,
        session="24/5",
    )
    defaults.update(overrides)
    return Instrument.objects.create(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Creación del modelo
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentCreation(TestCase):

    def test_create_instrument(self):
        instrument = make_instrument()
        self.assertEqual(instrument.symbol, "TESTUSD")
        self.assertEqual(instrument.asset_class, Instrument.ASSET_FOREX)
        self.assertFalse(instrument.trading_enabled)

    def test_str_representation(self):
        instrument = make_instrument(symbol="EURUSD", display_name="EUR/USD", trading_enabled=True)
        s = str(instrument)
        self.assertIn("EURUSD", s)
        self.assertIn("EUR/USD", s)
        self.assertIn("enabled", s)

    def test_timestamps_auto_populated(self):
        instrument = make_instrument()
        self.assertIsNotNone(instrument.created_at)
        self.assertIsNotNone(instrument.updated_at)


# ─────────────────────────────────────────────────────────────────────────────
# 2. symbol único
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentSymbolUnique(TestCase):

    def test_duplicate_symbol_raises(self):
        make_instrument(symbol="DUPUSD")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_instrument(symbol="DUPUSD")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Choices básicos
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentChoices(TestCase):

    def test_asset_class_choices_cover_expected_values(self):
        keys = {c[0] for c in Instrument.ASSET_CLASS_CHOICES}
        self.assertEqual(keys, {"forex", "metal", "energy", "crypto", "index"})

    def test_spread_unit_choices(self):
        keys = {c[0] for c in Instrument.SPREAD_UNIT_CHOICES}
        self.assertEqual(keys, {"pips", "points", "percent"})

    def test_market_data_provider_choices(self):
        keys = {c[0] for c in Instrument.MARKET_DATA_PROVIDER_CHOICES}
        self.assertEqual(keys, {"binance", "kraken", "finnhub", "sim"})

    def test_default_trading_enabled_is_false(self):
        instrument = Instrument(
            symbol="DEFUSD", display_name="DEF/USD", asset_class=Instrument.ASSET_FOREX,
            base_currency="DEF", quote_currency="USD",
        )
        self.assertFalse(instrument.trading_enabled)


# ─────────────────────────────────────────────────────────────────────────────
# 4. seed_instruments — idempotencia
# ─────────────────────────────────────────────────────────────────────────────

class TestSeedInstrumentsCommand(TestCase):

    def test_seed_creates_expected_instruments(self):
        call_command("seed_instruments")
        expected_symbols = {
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "BTCUSD", "ETHUSD",
            "USDCAD", "USDCHF", "NZDUSD", "XAUUSD", "XAGUSD", "US30",
            "US500", "NAS100", "SOLUSD",
        }
        actual_symbols = set(Instrument.objects.values_list("symbol", flat=True))
        self.assertEqual(actual_symbols, expected_symbols)

    def test_seed_does_not_duplicate_on_second_run(self):
        call_command("seed_instruments")
        first_count = Instrument.objects.count()
        call_command("seed_instruments")
        second_count = Instrument.objects.count()
        self.assertEqual(first_count, second_count)

    def test_seed_skips_existing_without_force(self):
        call_command("seed_instruments")
        instrument = Instrument.objects.get(symbol="EURUSD")
        instrument.display_name = "MANUALLY EDITED"
        instrument.save()

        call_command("seed_instruments")

        instrument.refresh_from_db()
        self.assertEqual(instrument.display_name, "MANUALLY EDITED")

    def test_seed_force_update_overwrites_existing(self):
        call_command("seed_instruments")
        instrument = Instrument.objects.get(symbol="EURUSD")
        instrument.display_name = "MANUALLY EDITED"
        instrument.save()

        call_command("seed_instruments", "--force-update")

        instrument.refresh_from_db()
        self.assertEqual(instrument.display_name, "EUR/USD")

    def test_enabled_symbols_match_spec(self):
        call_command("seed_instruments")
        enabled = set(
            Instrument.objects.filter(trading_enabled=True).values_list("symbol", flat=True)
        )
        self.assertEqual(enabled, {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "BTCUSD", "ETHUSD"})

    def test_disabled_symbols_match_spec(self):
        call_command("seed_instruments")
        disabled = set(
            Instrument.objects.filter(trading_enabled=False).values_list("symbol", flat=True)
        )
        self.assertEqual(
            disabled,
            {"USDCAD", "USDCHF", "NZDUSD", "XAUUSD", "XAGUSD", "US30", "US500", "NAS100", "SOLUSD"},
        )
