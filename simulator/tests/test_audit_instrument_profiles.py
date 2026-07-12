"""
simulator/tests/test_audit_instrument_profiles.py — FOUNDATION-06.

Covers the read-only `audit_instrument_profiles` management command:
MATCH/DRIFT/ONLY_RUNTIME/ONLY_DB classification, --strict exit code, and
the "never writes to the DB" guarantee. Runs against the isolated test DB,
never real data.
"""

from decimal import Decimal
from io import StringIO

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


def run_audit(*args):
    out, err = StringIO(), StringIO()
    try:
        call_command("audit_instrument_profiles", *args, stdout=out, stderr=err)
        exit_code = 0
    except SystemExit as exc:
        exit_code = exc.code
    return out.getvalue(), err.getvalue(), exit_code


class NoInstrumentsTests(TestCase):
    def test_empty_db_reports_only_runtime_for_every_symbol(self):
        self.assertEqual(Instrument.objects.count(), 0)
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)
        self.assertIn("EUR/USD", out)
        self.assertIn("ONLY_RUNTIME", out)
        # Never writes — still zero rows after running.
        self.assertEqual(Instrument.objects.count(), 0)


class OnlyDbTests(TestCase):
    def test_unknown_symbol_reports_only_db(self):
        make_instrument(symbol="TESTUSD")  # not in market_data/symbol_specs.py
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)
        self.assertIn("TESTUSD", out)
        self.assertIn("ONLY_DB", out)


class MatchAndDriftTests(TestCase):
    def test_seeded_catalog_against_symbol_specs_has_no_critical_drift(self):
        # Real seed data (simulator/management/commands/seed_instruments.py)
        # against the real SymbolSpec registry — the actual ground truth
        # comparison this command exists to make.
        call_command("seed_instruments", stdout=StringIO())
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)
        self.assertIn("EUR/USD", out)
        self.assertNotIn("ONLY_RUNTIME", out.split("EUR/USD")[1].split("\n")[0])

    def test_contract_size_drift_is_flagged_and_strict_fails(self):
        make_instrument(
            symbol="EURUSD", display_name="EUR/USD", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True,
            contract_size=Decimal("1.0000"),  # wildly different from SymbolSpec's 100000
        )
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)  # non-strict never fails
        self.assertIn("DRIFT", out)
        self.assertIn("contract_size", out)

        _out2, err2, exit_code2 = run_audit("--strict")
        self.assertEqual(exit_code2, 1)
        self.assertIn("critical drift", err2)

    def test_real_seed_data_has_known_pnl_mode_drift_for_usd_jpy(self):
        # Documents a genuine, pre-existing gap this tool is designed to catch:
        # seed_instruments.py correctly seeds USDJPY with PNL_INVERSE, but
        # market_data/symbol_specs.py never sets quote_currency="JPY" for
        # USD/JPY (defaults to "USD" — see
        # market_data/tests/test_instrument_bridges.py::
        # test_usd_jpy_registry_entry_defaults_quote_currency_to_usd), so the
        # runtime-derived profile disagrees with the DB catalog on pnl_mode.
        # This is not something this block fixes — symbol_specs.py is
        # explicitly out of scope for FOUNDATION-06 — it's exactly the kind
        # of drift this audit command exists to surface.
        call_command("seed_instruments", stdout=StringIO())
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)
        jpy_section = out.split("USD/JPY")[1].split("\n")[0]
        self.assertIn("DRIFT", jpy_section)
        self.assertIn("pnl_mode", out)

    def test_display_name_only_drift_does_not_fail_strict(self):
        make_instrument(
            symbol="EURUSD", display_name="Euro Dollar (legacy)", base_currency="EUR",
            market_data_provider=Instrument.PROVIDER_FINNHUB, provider_symbol="FX:EURUSD",
            trading_enabled=True,
            pip_size=Decimal("0.0001"), tick_size=Decimal("0.00001"), price_decimals=5,
            lot_step=Decimal("0.01"), min_lot=Decimal("0.01"), max_lot=Decimal("100.00"),
            contract_size=Decimal("100000.0000"), default_spread=Decimal("1.5000"),
            max_leverage=500,
        )
        out, err, exit_code = run_audit("--strict")
        self.assertEqual(exit_code, 0)  # warning-only drift must not trip --strict
        self.assertIn("DRIFT", out)
        self.assertEqual(err, "")


class ReadOnlyGuaranteeTests(TestCase):
    def test_command_never_writes_to_db(self):
        call_command("seed_instruments", stdout=StringIO())
        before = list(Instrument.objects.values_list("symbol", "updated_at").order_by("symbol"))
        run_audit()
        run_audit("--strict")
        after = list(Instrument.objects.values_list("symbol", "updated_at").order_by("symbol"))
        self.assertEqual(before, after)
