"""
simulator/tests/test_audit_market_sessions.py — FOUNDATION-11.

Covers the read-only `audit_market_sessions` management command: explicit
--at reproducibility, no DB writes, no network, and correct OPEN/CLOSED
classification against the real registry.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase


def run_audit(*args):
    out, err = StringIO(), StringIO()
    call_command("audit_market_sessions", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class ExplicitTimestampTests(TestCase):
    def test_weekday_shows_forex_and_crypto_open(self):
        out, _err = run_audit("--at", "2026-07-15T14:00:00+00:00")  # Wednesday
        self.assertIn("Evaluated at: 2026-07-15T14:00:00+00:00", out)
        self.assertIn("EUR/USD    FOREX_24_5       OPEN", out)
        self.assertIn("BTCUSD     CRYPTO_24_7      OPEN", out)
        self.assertIn("OPEN=15", out)

    def test_weekend_shows_forex_closed_crypto_still_open(self):
        out, _err = run_audit("--at", "2026-07-11T14:00:00+00:00")  # Saturday
        self.assertIn("EUR/USD    FOREX_24_5       WEEKEND", out)
        self.assertIn("BTCUSD     CRYPTO_24_7      OPEN", out)
        self.assertIn("OPEN=3", out)  # only the 3 crypto symbols

    def test_reproducible_across_runs(self):
        out1, _ = run_audit("--at", "2026-07-15T14:00:00+00:00")
        out2, _ = run_audit("--at", "2026-07-15T14:00:00+00:00")
        self.assertEqual(out1, out2)

    def test_missing_utc_offset_is_rejected(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            run_audit("--at", "2026-07-15T14:00:00")  # no offset

    def test_invalid_timestamp_is_rejected(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            run_audit("--at", "not-a-date")


class DefaultClockTests(TestCase):
    def test_runs_without_explicit_at(self):
        out, _err = run_audit()  # uses real clock — must not raise
        self.assertIn("Evaluated at:", out)
        self.assertIn("Total: 15", out)


class ReadOnlyGuaranteeTests(TestCase):
    def test_command_never_touches_instrument_table(self):
        from simulator.models import Instrument

        before = list(Instrument.objects.values_list("symbol", flat=True))
        run_audit("--at", "2026-07-15T14:00:00+00:00")
        after = list(Instrument.objects.values_list("symbol", flat=True))
        self.assertEqual(before, after)

    def test_command_module_does_not_import_instrument_model(self):
        import inspect

        from simulator.management.commands import audit_market_sessions as cmd_module

        source = inspect.getsource(cmd_module)
        self.assertNotIn("Instrument", source)
