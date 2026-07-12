"""
simulator/tests/test_feeds_market_session_integration.py — FOUNDATION-11.

Covers the market-session check in FeedManager._try_live_via_new_router():
a closed market short-circuits before any provider selection or circuit
breaker touch, an open market proceeds exactly as F09/F10 already did, and
symbols outside the allowlist (or with the flag off) never evaluate a
session at all.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, override_settings

from market_data.contracts import ProviderHealthState
from market_data.feeds import FeedManager
from market_data.runtime_router.state import get_circuit_breaker_state, reset_router_state
from market_data.sessions.models import CalendarId, MarketSessionResult, MarketSessionState, SessionReasonCode
from market_data.contracts import OrderPolicy


def _run(coro):
    return asyncio.run(coro)


def make_session_result(**overrides):
    defaults = dict(
        canonical_symbol="EUR/USD", calendar_id=CalendarId.FOREX_24_5,
        state=MarketSessionState.WEEKEND, order_policy=OrderPolicy.MARKET_CLOSED,
        evaluated_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        reason_code=SessionReasonCode.WEEKEND_CLOSURE, timezone="UTC",
    )
    defaults.update(overrides)
    return MarketSessionResult(**defaults)


@override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"EUR/USD"}))
class ClosedMarketShortCircuitTests(SimpleTestCase):
    def setUp(self):
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()

    def test_closed_market_returns_false_without_selecting_provider(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(),
        ), patch("market_data.runtime_router.service.select_runtime_provider") as mock_select:
            result = _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        self.assertFalse(result)
        mock_select.assert_not_called()

    def test_closed_market_never_touches_the_circuit_breaker(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(),
        ):
            _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        # No entry was ever created — the breaker store for this pair is
        # untouched (get_circuit_breaker_state lazily creates a default
        # CLOSED entry on first access, which is exactly the "never
        # touched" state — consecutive_failures must be 0, never having
        # been incremented).
        state = get_circuit_breaker_state("EUR/USD", "finnhub")
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 0)

    def test_closed_market_never_attempts_a_provider_loop(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(),
        ), patch.object(self.fm, "_finnhub_loop", new=AsyncMock()) as mock_finnhub:
            _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        mock_finnhub.assert_not_called()

    def test_unknown_state_also_short_circuits(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(
                state=MarketSessionState.UNKNOWN, order_policy=OrderPolicy.HALT_NEW_ORDERS,
                reason_code=SessionReasonCode.UNKNOWN_CALENDAR, calendar_id=CalendarId.UNKNOWN,
            ),
        ), patch("market_data.runtime_router.service.select_runtime_provider") as mock_select:
            result = _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        self.assertFalse(result)
        mock_select.assert_not_called()


@override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
class OpenMarketProceedsNormallyTests(SimpleTestCase):
    """Crypto (BTCUSD, the real canary) is always OPEN — proves the F09/F10
    router path still runs exactly as before this block for it."""

    def setUp(self):
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()

    def test_open_market_proceeds_to_provider_selection(self):
        with patch.object(self.fm, "_binance_loop", new=AsyncMock()) as mock_binance:
            result = _run(self.fm._try_live_via_new_router("BTCUSD", self.channel_layer))
        mock_binance.assert_called_once()
        self.assertTrue(result)


class AllowlistAndFlagUnaffectedTests(SimpleTestCase):
    def setUp(self):
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()

    @override_settings(MARKET_DATA_ROUTER_ENABLED=False)
    def test_flag_off_never_evaluates_session(self):
        # EUR/USD's legacy path has no Binance/Kraken symbol, so it falls
        # through to Finnhub — mock every loop it could reach so this stays
        # a pure unit test with zero real network.
        with patch("market_data.sessions.service.evaluate_market_session_for_symbol") as mock_eval, \
             patch.object(self.fm, "_binance_loop", new=AsyncMock()), \
             patch.object(self.fm, "_kraken_loop", new=AsyncMock()), \
             patch.object(self.fm, "_finnhub_loop", new=AsyncMock()):
            _run(self.fm._try_live("EUR/USD", self.channel_layer))
        mock_eval.assert_not_called()

    @override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"BTCUSD"}))
    def test_symbol_outside_allowlist_never_evaluates_session(self):
        with patch("market_data.sessions.service.evaluate_market_session_for_symbol") as mock_eval, \
             patch.object(self.fm, "_binance_loop", new=AsyncMock()), \
             patch.object(self.fm, "_kraken_loop", new=AsyncMock()), \
             patch.object(self.fm, "_finnhub_loop", new=AsyncMock()):
            _run(self.fm._try_live("EUR/USD", self.channel_layer))  # EUR/USD not allowlisted
        mock_eval.assert_not_called()


@override_settings(MARKET_DATA_ROUTER_ENABLED=True, MARKET_DATA_ROUTER_SYMBOLS=frozenset({"EUR/USD"}))
class LoggingTests(SimpleTestCase):
    def setUp(self):
        reset_router_state()
        self.fm = FeedManager()
        self.channel_layer = MagicMock()

    def test_structured_session_log_emitted(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(),
        ):
            with self.assertLogs("simulator.ws", level="INFO") as captured:
                _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_market_session", joined)
        self.assertIn("symbol=EUR/USD", joined)
        self.assertIn("calendar_id=FOREX_24_5", joined)
        self.assertIn("state=WEEKEND", joined)
        self.assertIn("order_policy=MARKET_CLOSED", joined)
        self.assertIn("reason_code=WEEKEND_CLOSURE", joined)

    def test_log_contains_no_secrets(self):
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=make_session_result(),
        ):
            with self.assertLogs("simulator.ws", level="INFO") as captured:
                _run(self.fm._try_live_via_new_router("EUR/USD", self.channel_layer))

        lowered = "\n".join(captured.output).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, lowered)
