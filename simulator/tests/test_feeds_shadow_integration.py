"""
simulator/tests/test_feeds_shadow_integration.py — FOUNDATION-08.

Covers the shadow-mode integration point in
market_data/feeds.py::FeedManager._maybe_run_shadow_evaluation() and its
call site in _ensure_running(): the feature flag gate, that legacy task
creation is completely unaffected by the flag or by shadow failures, that
it runs at most once per cold start (not per tick), and that the emitted
log line is structured and secret-free.
"""

import logging
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from market_data.feeds import FeedManager
from market_data.shadow.models import ShadowResult
from market_data.contracts import OrderPolicy, SourceState
from market_data.router.models import ReasonCode


def make_shadow_result(**overrides):
    defaults = dict(
        canonical_symbol="BTCUSD", legacy_expected_provider="binance",
        shadow_selected_provider="binance", shadow_source_state=SourceState.LIVE,
        shadow_order_policy=OrderPolicy.OPEN_NORMAL, degraded=False,
        reason_code=ReasonCode.PRIMARY_SELECTED, agrees_with_legacy=True,
        evaluated_at=0, error_code=None,
    )
    defaults.update(overrides)
    return ShadowResult(**defaults)


class FeatureFlagGateTests(SimpleTestCase):
    @override_settings(MARKET_DATA_SHADOW_MODE=False)
    def test_flag_false_does_not_evaluate(self):
        fm = FeedManager()
        with patch("market_data.shadow.service.evaluate_shadow_route") as mock_eval:
            fm._maybe_run_shadow_evaluation("BTCUSD")
        mock_eval.assert_not_called()

    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_flag_true_evaluates(self):
        fm = FeedManager()
        with patch(
            "market_data.shadow.service.evaluate_shadow_route",
            return_value=make_shadow_result(),
        ) as mock_eval:
            fm._maybe_run_shadow_evaluation("BTCUSD")
        mock_eval.assert_called_once_with("BTCUSD")

    def test_flag_defaults_false_without_override(self):
        from django.conf import settings
        self.assertFalse(settings.MARKET_DATA_SHADOW_MODE)


class LegacyUnaffectedTests(SimpleTestCase):
    """The core guarantee: whatever shadow mode does, the legacy feed task
    is created — same as before this block existed."""

    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_shadow_exception_does_not_block_legacy_task_creation(self):
        fm = FeedManager()
        with patch(
            "market_data.shadow.service.evaluate_shadow_route",
            side_effect=RuntimeError("simulated shadow crash"),
        ), patch.object(fm, "_feed_loop", new=MagicMock(return_value=MagicMock())), \
             patch("market_data.feeds.asyncio.create_task") as mock_create_task:
            fm._ensure_running("BTCUSD", channel_layer=MagicMock())

        mock_create_task.assert_called_once()  # legacy task still created

    @override_settings(MARKET_DATA_SHADOW_MODE=False)
    def test_legacy_task_created_with_flag_off(self):
        fm = FeedManager()
        with patch.object(fm, "_feed_loop", new=MagicMock(return_value=MagicMock())), \
             patch("market_data.feeds.asyncio.create_task") as mock_create_task:
            fm._ensure_running("BTCUSD", channel_layer=MagicMock())

        mock_create_task.assert_called_once()

    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_provider_selection_unaffected_by_shadow_disagreement(self):
        # Even a shadow result that actively disagrees with legacy must not
        # change what feeds.py actually does — _ensure_running still only
        # starts _feed_loop for the real symbol, never anything shadow-selected.
        disagreeing = make_shadow_result(
            legacy_expected_provider="binance", shadow_selected_provider="kraken", agrees_with_legacy=False,
        )
        fm = FeedManager()
        fake_channel_layer = MagicMock()
        with patch("market_data.shadow.service.evaluate_shadow_route", return_value=disagreeing), \
             patch.object(fm, "_feed_loop", new=MagicMock(return_value=MagicMock())) as mock_feed_loop, \
             patch("market_data.feeds.asyncio.create_task") as mock_create_task:
            fm._ensure_running("BTCUSD", channel_layer=fake_channel_layer)

        mock_feed_loop.assert_called_once_with("BTCUSD", fake_channel_layer)
        mock_create_task.assert_called_once()


class OncePerColdStartTests(SimpleTestCase):
    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_not_evaluated_again_while_task_already_running(self):
        fm = FeedManager()
        running_task = MagicMock()
        running_task.done.return_value = False
        fm._tasks["BTCUSD"] = running_task

        with patch(
            "market_data.shadow.service.evaluate_shadow_route",
            return_value=make_shadow_result(),
        ) as mock_eval:
            fm._ensure_running("BTCUSD", channel_layer=MagicMock())

        mock_eval.assert_not_called()  # already running -> _ensure_running's guard skips everything


class LoggingTests(SimpleTestCase):
    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_structured_log_emitted_on_evaluation(self):
        fm = FeedManager()
        result = make_shadow_result()
        with patch("market_data.shadow.service.evaluate_shadow_route", return_value=result):
            with self.assertLogs("simulator.ws", level="INFO") as captured:
                fm._maybe_run_shadow_evaluation("BTCUSD")

        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_shadow_decision", joined)
        self.assertIn("symbol=BTCUSD", joined)
        self.assertIn("legacy_provider=binance", joined)
        self.assertIn("shadow_provider=binance", joined)
        self.assertIn("agrees=True", joined)
        self.assertIn("reason_code=PRIMARY_SELECTED", joined)

    @override_settings(MARKET_DATA_SHADOW_MODE=True)
    def test_log_contains_no_secrets(self):
        fm = FeedManager()
        with patch("market_data.shadow.service.evaluate_shadow_route", return_value=make_shadow_result()):
            with self.assertLogs("simulator.ws", level="INFO") as captured:
                fm._maybe_run_shadow_evaluation("BTCUSD")

        joined = "\n".join(captured.output).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, joined)

    @override_settings(MARKET_DATA_SHADOW_MODE=False)
    def test_no_log_when_flag_off(self):
        fm = FeedManager()
        logger = logging.getLogger("simulator.ws")
        with self.assertNoLogs(logger, level="INFO"):
            fm._maybe_run_shadow_evaluation("BTCUSD")
