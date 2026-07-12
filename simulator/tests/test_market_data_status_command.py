"""
simulator/tests/test_market_data_status_command.py — FOUNDATION-13.

Covers `python manage.py market_data_status`: --symbol, --all, --json,
read-only guarantee, no secrets in output, and the required mutually
exclusive --symbol/--all gate.
"""

import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from market_data.observability import reset_observability_state
from market_data.runtime_router.state import reset_router_state
from simulator.models import Instrument


class _IsolatedTestCase(TestCase):
    def setUp(self):
        reset_observability_state()
        reset_router_state()

    def _call(self, *args, **kwargs):
        out = StringIO()
        call_command("market_data_status", *args, stdout=out, **kwargs)
        return out.getvalue()


class TableOutputTests(_IsolatedTestCase):
    def test_symbol_outputs_a_table_row(self):
        output = self._call(symbol="BTCUSD")
        self.assertIn("SYMBOL", output)
        self.assertIn("BREAKER", output)
        self.assertIn("BTCUSD", output)

    def test_all_outputs_every_registered_symbol(self):
        from market_data.symbol_specs import get_all_specs
        output = self._call(all=True)
        for spec in get_all_specs():
            self.assertIn(spec.symbol, output)

    def test_unknown_symbol_raises_command_error(self):
        with self.assertRaises(CommandError):
            self._call(symbol="NOT_A_REAL_SYMBOL")

    def test_includes_per_process_limitation_note(self):
        output = self._call(symbol="BTCUSD")
        self.assertIn("OWN process", output)


class JsonOutputTests(_IsolatedTestCase):
    def test_json_output_is_valid_and_structured(self):
        output = self._call(symbol="BTCUSD", json=True)
        payload = json.loads(output)
        self.assertIn("snapshots", payload)
        self.assertEqual(len(payload["snapshots"]), 1)
        self.assertEqual(payload["snapshots"][0]["canonical_symbol"], "BTCUSD")
        self.assertIn("note", payload)

    def test_json_output_all_symbols(self):
        from market_data.symbol_specs import get_all_specs
        output = self._call(all=True, json=True)
        payload = json.loads(output)
        self.assertEqual(len(payload["snapshots"]), len(get_all_specs()))

    def test_json_output_contains_no_secrets(self):
        output = self._call(all=True, json=True).lower()
        for forbidden in ("api_key", "finnhub_api_key", "password", "secret", "token"):
            self.assertNotIn(forbidden, output)


class ReadOnlyGuaranteeTests(_IsolatedTestCase):
    def test_never_writes_to_instrument_table(self):
        before = list(Instrument.objects.values_list("pk", flat=True))
        self._call(all=True)
        self._call(symbol="BTCUSD", json=True)
        after = list(Instrument.objects.values_list("pk", flat=True))
        self.assertEqual(before, after)

    def test_symbol_and_all_are_mutually_exclusive(self):
        with self.assertRaises(CommandError):
            self._call(symbol="BTCUSD", all=True)

    def test_requires_symbol_or_all(self):
        with self.assertRaises(CommandError):
            self._call()
