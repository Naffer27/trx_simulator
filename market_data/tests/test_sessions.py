"""
market_data/tests/test_sessions.py — FOUNDATION-11.

Calendar rules, evaluate_market_session(), evaluate_market_session_for_symbol().
Pure unittest, no Django dependency — this package has zero runtime wiring
itself (the wiring lives in market_data/feeds.py, tested separately in
simulator/tests/test_feeds_market_session_integration.py).
"""

import ast
import pathlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import market_data.sessions as sessions_pkg
from market_data.contracts import OrderPolicy
from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.sessions.models import CalendarId, MarketSessionResult, MarketSessionState, SessionReasonCode
from market_data.sessions.service import evaluate_market_session, evaluate_market_session_for_symbol
from market_data.symbol_specs import get_spec


def _dt(*args, **kwargs):
    return datetime(*args, tzinfo=timezone.utc, **kwargs)


class CryptoCalendarTests(unittest.TestCase):
    def test_open_on_saturday(self):
        result = evaluate_market_session_for_symbol("BTCUSD", now=_dt(2026, 7, 11, 3, 0))  # Saturday
        self.assertEqual(result.state, MarketSessionState.OPEN)
        self.assertEqual(result.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertEqual(result.calendar_id, CalendarId.CRYPTO_24_7)

    def test_open_on_sunday(self):
        result = evaluate_market_session_for_symbol("BTCUSD", now=_dt(2026, 7, 12, 3, 0))  # Sunday
        self.assertEqual(result.state, MarketSessionState.OPEN)

    def test_open_on_a_weekday(self):
        result = evaluate_market_session_for_symbol("BTCUSD", now=_dt(2026, 7, 15, 12, 0))  # Wednesday
        self.assertEqual(result.state, MarketSessionState.OPEN)
        self.assertIsNone(result.next_close_at)  # never closes


class ForexCalendarTests(unittest.TestCase):
    def test_open_on_monday(self):
        result = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 13, 12, 0))  # Monday
        self.assertEqual(result.state, MarketSessionState.OPEN)
        self.assertEqual(result.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertEqual(result.calendar_id, CalendarId.FOREX_24_5)

    def test_closed_on_saturday(self):
        result = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 11, 12, 0))  # Saturday
        self.assertEqual(result.state, MarketSessionState.WEEKEND)
        self.assertEqual(result.order_policy, OrderPolicy.MARKET_CLOSED)
        self.assertEqual(result.reason_code, SessionReasonCode.WEEKEND_CLOSURE)

    def test_sunday_evening_transitions_to_open(self):
        before_open = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 12, 21, 59))
        at_open = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 12, 22, 0))
        self.assertEqual(before_open.state, MarketSessionState.WEEKEND)
        self.assertEqual(at_open.state, MarketSessionState.OPEN)
        self.assertEqual(before_open.next_open_at, _dt(2026, 7, 12, 22, 0))

    def test_friday_evening_transitions_to_weekend(self):
        still_open = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 17, 21, 59))
        just_closed = evaluate_market_session_for_symbol("EUR/USD", now=_dt(2026, 7, 17, 22, 0))
        self.assertEqual(still_open.state, MarketSessionState.OPEN)
        self.assertEqual(just_closed.state, MarketSessionState.WEEKEND)
        self.assertEqual(still_open.next_close_at, _dt(2026, 7, 17, 22, 0))


class MetalsCalendarTests(unittest.TestCase):
    def test_open_midweek(self):
        result = evaluate_market_session_for_symbol("XAU/USD", now=_dt(2026, 7, 15, 12, 0))
        self.assertEqual(result.state, MarketSessionState.OPEN)
        self.assertEqual(result.calendar_id, CalendarId.METALS_23_5)

    def test_daily_maintenance_window(self):
        result = evaluate_market_session_for_symbol("XAU/USD", now=_dt(2026, 7, 15, 21, 30))
        self.assertEqual(result.state, MarketSessionState.MAINTENANCE)
        self.assertEqual(result.order_policy, OrderPolicy.MARKET_CLOSED)
        self.assertEqual(result.reason_code, SessionReasonCode.DAILY_MAINTENANCE)
        self.assertEqual(result.next_open_at, _dt(2026, 7, 15, 22, 0))

    def test_closed_on_weekend_too(self):
        result = evaluate_market_session_for_symbol("XAU/USD", now=_dt(2026, 7, 11, 12, 0))  # Saturday
        self.assertEqual(result.state, MarketSessionState.WEEKEND)


class IndexCalendarTests(unittest.TestCase):
    def test_open_regular_hours(self):
        # 14:00 UTC = 10:00 America/New_York (EDT, July) — within 9:30-16:00.
        result = evaluate_market_session_for_symbol("US30", now=_dt(2026, 7, 15, 14, 0))
        self.assertEqual(result.state, MarketSessionState.OPEN)
        self.assertEqual(result.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertEqual(result.timezone, "America/New_York")

    def test_after_hours(self):
        # 21:00 UTC = 17:00 EDT — after the 16:00 close, before 20:00 after-hours end.
        result = evaluate_market_session_for_symbol("US30", now=_dt(2026, 7, 15, 21, 0))
        self.assertEqual(result.state, MarketSessionState.AFTER_HOURS)
        self.assertEqual(result.order_policy, OrderPolicy.CLOSE_ONLY)

    def test_pre_market(self):
        # 08:00 UTC = 04:00 EDT — start of pre-market.
        result = evaluate_market_session_for_symbol("US30", now=_dt(2026, 7, 15, 8, 0))
        self.assertEqual(result.state, MarketSessionState.PRE_MARKET)
        self.assertEqual(result.order_policy, OrderPolicy.CLOSE_ONLY)

    def test_overnight_closed(self):
        # 02:00 UTC = 22:00 EDT (previous day) — after after-hours end.
        result = evaluate_market_session_for_symbol("US30", now=_dt(2026, 7, 16, 2, 0))
        self.assertEqual(result.state, MarketSessionState.CLOSED)
        self.assertEqual(result.order_policy, OrderPolicy.MARKET_CLOSED)

    def test_weekend(self):
        result = evaluate_market_session_for_symbol("US30", now=_dt(2026, 7, 11, 14, 0))  # Saturday
        self.assertEqual(result.state, MarketSessionState.WEEKEND)


class UnknownCalendarTests(unittest.TestCase):
    def test_energy_asset_class_is_safely_halted(self):
        # No energy instrument is registered in symbol_specs.py yet — build
        # a synthetic profile directly to exercise the "energy -> UNKNOWN"
        # default mapping without needing a real registered symbol.
        from market_data.instruments.profiles import InstrumentProfile

        profile = InstrumentProfile(
            canonical_symbol="USOIL", display_name="USOIL", asset_class="energy",
            base_currency="USOIL", quote_currency="USD",
            pip_size=0.01, tick_size=0.01, price_decimals=2,
            lot_step=0.01, min_lot=0.01, max_lot=100.0, contract_size=1000.0, max_leverage=20,
            default_spread=1.0, spread_unit="pips", margin_mode="leverage", pnl_mode="STANDARD",
            trading_enabled=False, simulation_allowed=True,
        )
        result = evaluate_market_session(profile, now=_dt(2026, 7, 15, 12, 0))
        self.assertEqual(result.state, MarketSessionState.UNKNOWN)
        self.assertEqual(result.order_policy, OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(result.calendar_id, CalendarId.UNKNOWN)
        self.assertEqual(result.reason_code, SessionReasonCode.UNKNOWN_CALENDAR)


class TimezoneAwareValidationTests(unittest.TestCase):
    def test_naive_datetime_is_safely_halted_not_raised(self):
        profile = profile_from_symbol_spec(get_spec("BTCUSD"))
        naive = datetime(2026, 7, 15, 12, 0)  # no tzinfo
        result = evaluate_market_session(profile, now=naive)  # must not raise
        self.assertEqual(result.state, MarketSessionState.UNKNOWN)
        self.assertEqual(result.order_policy, OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(result.reason_code, SessionReasonCode.EVALUATION_ERROR)

    def test_result_rejects_naive_evaluated_at_on_direct_construction(self):
        with self.assertRaises(ValueError):
            MarketSessionResult(
                canonical_symbol="BTCUSD", calendar_id=CalendarId.CRYPTO_24_7,
                state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                evaluated_at=datetime(2026, 7, 15, 12, 0),  # naive
                reason_code=SessionReasonCode.MARKET_OPEN, timezone="UTC",
            )

    def test_non_utc_input_is_normalized(self):
        from zoneinfo import ZoneInfo
        ny_time = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))  # 14:00 UTC
        result = evaluate_market_session_for_symbol("EUR/USD", now=ny_time)
        self.assertEqual(result.evaluated_at, _dt(2026, 7, 15, 14, 0))
        self.assertEqual(result.state, MarketSessionState.OPEN)


class EvaluateMarketSessionForSymbolTests(unittest.TestCase):
    def test_unknown_symbol_is_safely_halted_not_raised(self):
        result = evaluate_market_session_for_symbol("NOT_A_REAL_SYMBOL", now=_dt(2026, 7, 15, 12, 0))
        self.assertEqual(result.state, MarketSessionState.UNKNOWN)
        self.assertEqual(result.order_policy, OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(result.canonical_symbol, "NOT_A_REAL_SYMBOL")

    def test_totally_unexpected_failure_still_never_raises(self):
        with patch("market_data.sessions.service.get_spec", side_effect=RuntimeError("boom")):
            result = evaluate_market_session_for_symbol("BTCUSD", now=_dt(2026, 7, 15, 12, 0))
        self.assertEqual(result.state, MarketSessionState.UNKNOWN)

    def test_default_now_uses_real_clock_when_not_supplied(self):
        result = evaluate_market_session_for_symbol("BTCUSD")  # must not raise, crypto always OPEN
        self.assertEqual(result.state, MarketSessionState.OPEN)


class NoNetworkOrDbOrDjangoDependencyTests(unittest.TestCase):
    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os",
    })

    def test_no_forbidden_imports_in_sessions_package(self):
        package_dir = pathlib.Path(sessions_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/sessions/*.py to exist")

        for path in source_files:
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(), filename=str(path))
                imported_roots = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_roots.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and node.level == 0:
                            imported_roots.add(node.module.split(".")[0])
                forbidden_hits = imported_roots & self._FORBIDDEN_MODULES
                self.assertFalse(
                    forbidden_hits,
                    f"{path.name} imports forbidden module(s): {sorted(forbidden_hits)}",
                )


if __name__ == "__main__":
    unittest.main()
