# simulator/tests/test_probe_market_symbol.py
"""
Bloque C2 — Gold market data readiness.

Tests for the read-only `probe_market_symbol` management command and its
pure `probe_finnhub_quote()` helper. NO real HTTP calls are made — every
network path is mocked via unittest.mock.patch on urllib.request.urlopen.

Covered:
  - probe_finnhub_quote(): success, missing API key, invalid/empty response,
    timeout, generic network error.
  - Command: registry symbol resolution (XAU/USD -> OANDA:XAU_USD), --raw
    mode, unknown registry symbol, registry symbol without finnhub_symbol.
  - XAU/USD stays disabled — this command never touches SymbolSpec.enabled
    or writes to the DB.
"""
import io
import json
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from market_data.symbol_specs import get_spec
from simulator.management.commands.probe_market_symbol import probe_finnhub_quote


def _fake_response(payload: dict):
    """Build a context-manager mock that mimics urllib's response object."""
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    cm.__exit__.return_value = False
    return cm


# ─────────────────────────────────────────────────────────────────────────────
# 1. probe_finnhub_quote() — pure function, HTTP fully mocked
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeFinnhubQuoteSuccess(SimpleTestCase):

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_success_returns_parsed_quote(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({
            "c": 2410.50, "o": 2400.00, "h": 2415.00, "l": 2398.00,
            "pc": 2401.00, "t": 1751500000,
        })
        result = probe_finnhub_quote("OANDA:XAU_USD", "fake-key", timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["price"], 2410.50)
        self.assertEqual(result["open"], 2400.00)
        self.assertEqual(result["high"], 2415.00)
        self.assertEqual(result["low"], 2398.00)
        self.assertEqual(result["prev_close"], 2401.00)
        self.assertEqual(result["timestamp"], 1751500000)

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_success_uses_correct_url_and_symbol(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"c": 1.085, "t": 1})
        probe_finnhub_quote("FX:EURUSD", "fake-key", timeout=5.0)
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("finnhub.io/api/v1/quote", called_url)
        self.assertIn("symbol=FX%3AEURUSD", called_url)
        self.assertIn("token=fake-key", called_url)


class TestProbeFinnhubQuoteNoApiKey(SimpleTestCase):

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_missing_api_key_short_circuits_without_network_call(self, mock_urlopen):
        result = probe_finnhub_quote("OANDA:XAU_USD", "", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("missing_api_key", result["error"])
        mock_urlopen.assert_not_called()


class TestProbeFinnhubQuoteInvalidResponse(SimpleTestCase):

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_empty_payload_is_invalid(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({})
        result = probe_finnhub_quote("OANDA:XAU_USD", "fake-key", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_response", result["error"])

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_zero_price_is_invalid(self, mock_urlopen):
        """Finnhub returns c=0 for a symbol it doesn't recognize/support."""
        mock_urlopen.return_value = _fake_response({"c": 0, "t": 0})
        result = probe_finnhub_quote("UNKNOWN:SYM", "fake-key", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_response", result["error"])

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_malformed_json_is_invalid(self, mock_urlopen):
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = b"not json"
        cm.__exit__.return_value = False
        mock_urlopen.return_value = cm
        result = probe_finnhub_quote("OANDA:XAU_USD", "fake-key", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_json", result["error"])


class TestProbeFinnhubQuoteTimeoutAndErrors(SimpleTestCase):

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        result = probe_finnhub_quote("OANDA:XAU_USD", "fake-key", timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["error"])

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_generic_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionResetError("connection reset")
        result = probe_finnhub_quote("OANDA:XAU_USD", "fake-key", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("network_error", result["error"])

    def test_missing_provider_symbol(self):
        result = probe_finnhub_quote("", "fake-key", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("missing_provider_symbol", result["error"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Command wiring — registry resolution, --raw, error paths
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeMarketSymbolCommand(TestCase):

    def _run(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        call_command("probe_market_symbol", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_resolves_xauusd_to_finnhub_symbol(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"c": 2400.0, "t": 1})
        out, _ = self._run("XAU/USD")
        self.assertIn("OANDA:XAU_USD", out)
        self.assertIn("Status          : OK", out)

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_raw_mode_skips_registry_lookup(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"c": 2400.0, "t": 1})
        out, _ = self._run("OANDA:XAU_USD", raw=True)
        self.assertIn("(raw", out)
        self.assertIn("OANDA:XAU_USD", out)
        self.assertIn("Status          : OK", out)

    def test_unknown_registry_symbol_fails_cleanly_without_network_call(self):
        with mock.patch(
            "simulator.management.commands.probe_market_symbol.urllib.request.urlopen"
        ) as mock_urlopen:
            out, err = self._run("NOT/AREAL")
            self.assertIn("unknown_symbol", err)
            mock_urlopen.assert_not_called()

    def test_symbol_without_finnhub_symbol_fails_cleanly(self):
        # XAG/USD has no finnhub_symbol configured in symbol_specs.py.
        self.assertIsNone(get_spec("XAG/USD").finnhub_symbol)
        with mock.patch(
            "simulator.management.commands.probe_market_symbol.urllib.request.urlopen"
        ) as mock_urlopen:
            out, err = self._run("XAG/USD")
            self.assertIn("no_finnhub_symbol", err)
            mock_urlopen.assert_not_called()

    @mock.patch("simulator.management.commands.probe_market_symbol.urllib.request.urlopen")
    def test_fail_status_printed_on_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        out, _ = self._run("XAU/USD")
        self.assertIn("Status          : FAIL", out)
        self.assertIn("timeout", out)

    def test_command_never_writes_to_db(self):
        """Read-only guarantee: no Instrument/SymbolSpec state changes as a side effect."""
        from simulator.models import Instrument
        before = Instrument.objects.count()
        with mock.patch(
            "simulator.management.commands.probe_market_symbol.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _fake_response({"c": 2400.0, "t": 1})
            self._run("XAU/USD")
        after = Instrument.objects.count()
        self.assertEqual(before, after)

    def test_xauusd_still_disabled_after_probe(self):
        with mock.patch(
            "simulator.management.commands.probe_market_symbol.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _fake_response({"c": 2400.0, "t": 1})
            self._run("XAU/USD")
        self.assertFalse(get_spec("XAU/USD").enabled)
