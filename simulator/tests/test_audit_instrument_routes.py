"""
simulator/tests/test_audit_instrument_routes.py — FOUNDATION-07.

Covers the read-only `audit_instrument_routes` management command:
OK/INVALID reporting, --strict exit code, and that it never touches the DB.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


def run_audit(*args):
    out, err = StringIO(), StringIO()
    try:
        call_command("audit_instrument_routes", *args, stdout=out, stderr=err)
        exit_code = 0
    except SystemExit as exc:
        exit_code = exc.code
    return out.getvalue(), err.getvalue(), exit_code


class RealRegistryTests(TestCase):
    def test_every_registered_symbol_is_ok(self):
        out, _err, exit_code = run_audit()
        self.assertEqual(exit_code, 0)
        self.assertIn("EUR/USD", out)
        self.assertIn("BTCUSD", out)
        self.assertIn("OK=15", out)
        self.assertIn("INVALID=0", out)
        for line in out.splitlines():
            tokens = line.split()
            if len(tokens) >= 2 and tokens[1] in ("OK", "INVALID"):
                self.assertEqual(tokens[1], "OK")  # every data row is OK, none INVALID

    def test_strict_passes_when_everything_is_ok(self):
        out, err, exit_code = run_audit("--strict")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("OK=15", out)


class InvalidPlanReportingTests(TestCase):
    def test_a_failing_symbol_is_reported_invalid_others_stay_ok(self):
        from market_data.instruments import routing as routing_module

        real_build = routing_module.build_route_plan

        def fake_build(profile, **kwargs):
            if profile.canonical_symbol == "EUR/USD":
                raise ValueError("EUR/USD: synthetic failure injected for this test")
            return real_build(profile, **kwargs)

        with patch(
            "simulator.management.commands.audit_instrument_routes.build_route_plan",
            side_effect=fake_build,
        ):
            out, _err, exit_code = run_audit()

        self.assertEqual(exit_code, 0)  # non-strict never fails
        eur_section = out.split("EUR/USD")[1].split("\n")[0]
        self.assertIn("INVALID", eur_section)
        btc_section = out.split("BTCUSD")[1].split("\n")[0]
        self.assertIn("OK", btc_section)
        self.assertIn("INVALID=1", out)

    def test_strict_fails_when_a_symbol_is_invalid(self):
        from market_data.instruments import routing as routing_module

        real_build = routing_module.build_route_plan

        def fake_build(profile, **kwargs):
            if profile.canonical_symbol == "EUR/USD":
                raise ValueError("synthetic failure")
            return real_build(profile, **kwargs)

        with patch(
            "simulator.management.commands.audit_instrument_routes.build_route_plan",
            side_effect=fake_build,
        ):
            _out, err, exit_code = run_audit("--strict")

        self.assertEqual(exit_code, 1)
        self.assertIn("route plan", err)


class ReadOnlyGuaranteeTests(TestCase):
    def test_command_never_touches_instrument_table(self):
        from simulator.models import Instrument

        before = list(Instrument.objects.values_list("symbol", flat=True))
        run_audit()
        run_audit("--strict")
        after = list(Instrument.objects.values_list("symbol", flat=True))
        self.assertEqual(before, after)

    def test_command_module_does_not_import_instrument_model(self):
        import inspect

        from simulator.management.commands import audit_instrument_routes as cmd_module

        source = inspect.getsource(cmd_module)
        self.assertNotIn("Instrument", source)
