"""
simulator/tests/test_feeds_router_integration.py — FOUNDATION-09.

Covers the controlled router integration in market_data/feeds.py::
FeedManager._try_live() / _try_live_via_new_router() / _try_live_legacy():
the feature flag + allowlist gate, explicit provider dispatch, fallback to
legacy on any error, and that every symbol not on the allowlist (or with
the flag off) is completely unaffected.
"""

import pathlib

from django.test import SimpleTestCase, override_settings
from unittest.mock import AsyncMock, MagicMock, patch

from market_data.contracts import SourceState
from market_data.feeds import FeedManager
from market_data.router.models import ReasonCode
from market_data.runtime_router.models import RuntimeSelectionResult


def make_result(**overrides):
    defaults = dict(
        symbol="BTCUSD", selected_provider_id="binance", selected_provider_symbol="BTCUSDT",
        source_state=SourceState.LIVE, reason_code=ReasonCode.PRIMARY_SELECTED,
        used_new_router=True, fallback_to_legacy=False, error_code=None,
    )
    defaults.update(overrides)
    return RuntimeSelectionResult(**defaults)


class FeatureFlagAndAllowlistGateTests(SimpleTestCase):
    @override_settings(MARKET_DATA_ROUTER_ENABLED=False)
    def test_flag_false_uses_legacy_and_never_invokes_router(self):
        fm = FeedManager()
        with patch("market_data.runtime_router.service.select_runtime_provider") as mock_select, \
             patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance:
            result = _run(fm._try_live("BTCUSD", MagicMock()))

        mock_select.assert_not_called()
        mock_binance.assert_called_once()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"ETHUSD"}))
    def test_flag_true_symbol_not_on_allowlist_uses_legacy(self):
        fm = FeedManager()
        with patch("market_data.runtime_router.service.select_runtime_provider") as mock_select, \
             patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance:
            _run(fm._try_live("BTCUSD", MagicMock()))  # BTCUSD not in the allowlist above

        mock_select.assert_not_called()
        mock_binance.assert_called_once()

    def test_settings_default_to_safe_values(self):
        from django.conf import settings
        self.assertFalse(settings.MARKET_DATA_ROUTER_ENABLED)
        self.assertEqual(settings.MARKET_DATA_ROUTER_SYMBOLS, frozenset())


class RouterControlledDispatchTests(SimpleTestCase):
    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_binance_selection_dispatches_to_binance_loop(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(selected_provider_id="binance", selected_provider_symbol="BTCUSDT"),
        ), patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance, \
             patch.object(fm, "_kraken_loop", new=AsyncMock()) as mock_kraken, \
             patch.object(fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub:
            channel_layer = MagicMock()
            result = _run(fm._try_live("BTCUSD", channel_layer))

        mock_binance.assert_called_once_with("BTCUSD", "BTCUSDT", channel_layer)
        mock_kraken.assert_not_called()
        mock_finnhub.assert_not_called()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_kraken_selection_dispatches_to_kraken_loop(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(selected_provider_id="kraken", selected_provider_symbol="XBT/USD"),
        ), patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance, \
             patch.object(fm, "_kraken_loop", new=AsyncMock()) as mock_kraken:
            channel_layer = MagicMock()
            result = _run(fm._try_live("BTCUSD", channel_layer))

        mock_kraken.assert_called_once_with("BTCUSD", "XBT/USD", channel_layer)
        mock_binance.assert_not_called()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_finnhub_selection_dispatches_to_finnhub_loop_without_extra_symbol_arg(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(selected_provider_id="finnhub", selected_provider_symbol="FX:BTCUSD"),
        ), patch.object(fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub:
            channel_layer = MagicMock()
            result = _run(fm._try_live("BTCUSD", channel_layer))

        mock_finnhub.assert_called_once_with("BTCUSD", channel_layer)
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_no_provider_selected_lets_existing_sim_fallback_take_over(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(
                selected_provider_id=None, selected_provider_symbol=None,
                source_state=SourceState.SIMULATION, reason_code=ReasonCode.SIMULATION_FALLBACK,
            ),
        ), patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance, \
             patch.object(fm, "_kraken_loop", new=AsyncMock()) as mock_kraken, \
             patch.object(fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub, \
             patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=True)) as mock_legacy:
            result = _run(fm._try_live("BTCUSD", MagicMock()))

        mock_binance.assert_not_called()
        mock_kraken.assert_not_called()
        mock_finnhub.assert_not_called()
        mock_legacy.assert_not_called()  # a valid "no provider" decision, not an error — no legacy fallback
        self.assertFalse(result)  # exactly what legacy would return for "nothing live"


class FallbackToLegacyTests(SimpleTestCase):
    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_router_reported_failure_falls_back_to_legacy(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(
                selected_provider_id=None, used_new_router=False, fallback_to_legacy=True,
                error_code="unknown_symbol: 'BTCUSD'",
            ),
        ), patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=True)) as mock_legacy:
            result = _run(fm._try_live("BTCUSD", MagicMock()))

        mock_legacy.assert_called_once()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_unrecognized_provider_id_falls_back_to_legacy(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(selected_provider_id="oanda", selected_provider_symbol="EUR_USD"),
        ), patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=True)) as mock_legacy:
            result = _run(fm._try_live("BTCUSD", MagicMock()))

        mock_legacy.assert_called_once()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_dispatched_loop_raising_falls_back_to_legacy(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(selected_provider_id="binance", selected_provider_symbol="BTCUSDT"),
        ), patch.object(fm, "_binance_loop", new=AsyncMock(side_effect=RuntimeError("connection refused"))), \
             patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=True)) as mock_legacy:
            result = _run(fm._try_live("BTCUSD", MagicMock()))

        mock_legacy.assert_called_once()
        self.assertTrue(result)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_legacy_receives_exact_same_symbol_and_channel_layer(self):
        fm = FeedManager()
        channel_layer = MagicMock()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            side_effect=RuntimeError("boom"),
        ), patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=True)) as mock_legacy:
            _run(fm._try_live("BTCUSD", channel_layer))

        mock_legacy.assert_called_once_with("BTCUSD", channel_layer)


class LoggingTests(SimpleTestCase):
    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_structured_selection_log_emitted(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            return_value=make_result(),
        ), patch.object(fm, "_binance_loop", new=AsyncMock()):
            with self.assertLogs("simulator.ws", level="INFO") as captured:
                _run(fm._try_live("BTCUSD", MagicMock()))

        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_router_selection", joined)
        self.assertIn("symbol=BTCUSD", joined)
        self.assertIn("selected_provider=binance", joined)
        self.assertIn("used_new_router=True", joined)
        self.assertIn("fallback_to_legacy=False", joined)
        self.assertIn("reason_code=PRIMARY_SELECTED", joined)

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_error_path_logs_and_contains_no_secrets(self):
        fm = FeedManager()
        with patch(
            "market_data.runtime_router.service.select_runtime_provider",
            side_effect=RuntimeError("boom"),
        ), patch.object(fm, "_try_live_legacy", new=AsyncMock(return_value=False)):
            with self.assertLogs("simulator.ws", level="ERROR") as captured:
                _run(fm._try_live("BTCUSD", MagicMock()))

        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_router_selection_error", joined)
        lowered = joined.lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, lowered)


class EnvTemplatesDocumentedTests(SimpleTestCase):
    def test_env_example_documents_router_flags(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        content = (repo_root / ".env.example").read_text()
        self.assertIn("MARKET_DATA_ROUTER_ENABLED=False", content)
        self.assertIn("MARKET_DATA_ROUTER_SYMBOLS=", content)

    def test_staging_template_documents_router_flags(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        content = (repo_root / "deploy" / ".env.staging.template").read_text()
        self.assertIn("MARKET_DATA_ROUTER_ENABLED=False", content)
        self.assertIn("MARKET_DATA_ROUTER_SYMBOLS=", content)


def _run(coro):
    import asyncio
    return asyncio.run(coro)
