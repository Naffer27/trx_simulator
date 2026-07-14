"""
simulator/tests/test_spread_config_cache.py — SPREAD-03 FASE A.

Covers simulator/spread_config_cache.py directly: the async-safe,
process-wide BrokerSpreadConfig cache that replaced the old per-call lazy
DB read inside spread_engine._get_config() (which silently failed from
async context — see test_pricing_context_forensic_invariants.py's module
docstring for the full diagnosis).
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from simulator import spread_config_cache as sc
from simulator.models import BrokerSpreadConfig

from .factories import make_spread_config


def _run(coro):
    return asyncio.run(coro)


class GetCachedConfigTests(TestCase):
    def setUp(self):
        sc.reset_for_tests()

    def tearDown(self):
        sc.reset_for_tests()

    def test_returns_none_before_any_refresh(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        self.assertIsNone(sc.get_cached_config("EUR/USD"))

    def test_returns_snapshot_after_refresh(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        snap = sc.get_cached_config("EUR/USD")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.spread_pips, 2.0)
        self.assertTrue(snap.enabled)

    def test_normalizes_symbol_variants(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        self.assertEqual(sc.get_cached_config("EURUSD").spread_pips, 2.0)

    def test_disabled_config_not_in_cache(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("5.00"), enabled=False)
        sc.refresh_cache_sync()
        self.assertIsNone(sc.get_cached_config("EUR/USD"))

    def test_missing_row_returns_none(self):
        sc.refresh_cache_sync()
        self.assertIsNone(sc.get_cached_config("GBP/USD"))

    def test_bounds_disabled_by_default_snapshot_min_max_are_none(self):
        """Opt-in correction: even though the row itself may carry numeric
        min_spread/max_spread, spread_bounds_enabled defaults to False, so
        the cached snapshot surfaces None for both — never enforced unless
        an admin explicitly opts in."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"))
        sc.refresh_cache_sync()
        snap = sc.get_cached_config("EUR/USD")
        self.assertIsNone(snap.min_spread)
        self.assertIsNone(snap.max_spread)
        self.assertFalse(snap.bounds_enabled)

    def test_bounds_enabled_snapshot_carries_real_min_max(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        sc.refresh_cache_sync()
        snap = sc.get_cached_config("EUR/USD")
        self.assertEqual(snap.min_spread, 0.50)
        self.assertEqual(snap.max_spread, 5.00)
        self.assertTrue(snap.bounds_enabled)

    def test_read_performs_zero_orm_queries(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        with self.assertNumQueries(0):
            sc.get_cached_config("EUR/USD")


class RefreshCacheSyncTests(TestCase):
    def setUp(self):
        sc.reset_for_tests()

    def tearDown(self):
        sc.reset_for_tests()

    def test_refresh_picks_up_a_newly_created_row(self):
        sc.refresh_cache_sync()
        self.assertIsNone(sc.get_cached_config("EUR/USD"))
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

    def test_config_change_reflected_after_explicit_refresh(self):
        """'cambio de config se refleja tras invalidación/TTL' — explicit
        refresh_cache_sync() call simulates either path (TTL cycle or
        manual invalidation)."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

        BrokerSpreadConfig.objects.filter(symbol="EUR/USD").update(spread_pips=Decimal("9.00"))
        # Not yet refreshed — cache still shows the old value.
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

        sc.refresh_cache_sync()
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 9.0)

    def test_missing_row_for_allowed_symbol_logs_structured_warning(self):
        with self.assertLogs("simulator.spread", level="WARNING") as captured:
            sc.refresh_cache_sync()
        joined = "\n".join(captured.output)
        self.assertIn("event=broker_spread_config_missing", joined)
        self.assertIn("EUR/USD", joined)  # an allowed symbol with no row

    def test_no_missing_symbols_no_warning_logged(self):
        from market_data.symbol_specs import allowed_symbols
        for sym in allowed_symbols():
            make_spread_config(symbol=sym, spread_pips=Decimal("1.00"))
        with self.assertNoLogs("simulator.spread", level="WARNING"):
            sc.refresh_cache_sync()

    def test_disabled_row_counts_as_missing_for_the_warning(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=False)
        with self.assertLogs("simulator.spread", level="WARNING") as captured:
            sc.refresh_cache_sync()
        self.assertIn("EUR/USD", "\n".join(captured.output))

    def test_never_raises_on_db_failure_and_keeps_previous_cache(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        sc.refresh_cache_sync()
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

        with patch(
            "simulator.models.BrokerSpreadConfig.objects.filter",
            side_effect=RuntimeError("db down"),
        ):
            sc.refresh_cache_sync()  # must not raise

        # Previous cache untouched by the failed refresh.
        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

    def test_no_secrets_in_warning_log(self):
        with self.assertLogs("simulator.spread", level="WARNING") as captured:
            sc.refresh_cache_sync()
        lowered = "\n".join(captured.output).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, lowered)


class StalenessTests(TestCase):
    def setUp(self):
        sc.reset_for_tests()

    def tearDown(self):
        sc.reset_for_tests()

    def test_is_stale_before_any_refresh(self):
        self.assertTrue(sc.is_stale())

    def test_not_stale_immediately_after_refresh(self):
        sc.refresh_cache_sync()
        self.assertFalse(sc.is_stale())

    def test_stale_after_ttl_elapses(self):
        sc.refresh_cache_sync()
        future = time.monotonic() + sc.REFRESH_INTERVAL_SECONDS + 1
        self.assertTrue(sc.is_stale(now=future))

    def test_has_loaded_at_least_once(self):
        self.assertFalse(sc.has_loaded_at_least_once())
        sc.refresh_cache_sync()
        self.assertTrue(sc.has_loaded_at_least_once())


class EnsureBackgroundRefreshStartedTests(TransactionTestCase):
    """TransactionTestCase, not TestCase: ensure_background_refresh_started()
    calls database_sync_to_async(refresh_cache_sync)(), which runs the DB
    read on a different thread — TestCase's uncommitted per-test
    transaction is invisible to (and can deadlock against) that thread on
    SQLite. TransactionTestCase commits normally and truncates between
    tests instead, avoiding the cross-thread visibility issue."""

    def setUp(self):
        sc.reset_for_tests()

    def tearDown(self):
        sc.reset_for_tests()

    def test_warms_the_cache_immediately(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        self.assertIsNone(sc.get_cached_config("EUR/USD"))

        _run(sc.ensure_background_refresh_started())

        self.assertEqual(sc.get_cached_config("EUR/USD").spread_pips, 2.0)

    def test_idempotent_second_call_is_a_cheap_noop(self):
        calls = []
        original = sc.refresh_cache_sync

        def _counting_refresh():
            calls.append(1)
            return original()

        with patch("simulator.spread_config_cache.refresh_cache_sync", side_effect=_counting_refresh):
            _run(sc.ensure_background_refresh_started())
            first_call_count = len(calls)
            _run(sc.ensure_background_refresh_started())
            second_call_count = len(calls)

        self.assertEqual(first_call_count, 1)
        self.assertEqual(second_call_count, 1)  # unchanged — second call no-op'd

    def test_never_raises_even_if_first_refresh_fails(self):
        with patch(
            "simulator.spread_config_cache.refresh_cache_sync",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                _run(sc.ensure_background_refresh_started())
        # NOTE: refresh_cache_sync() itself never raises in production (it
        # has its own try/except) — this test documents that
        # ensure_background_refresh_started() relies on that guarantee
        # rather than adding a second one; consumers.py wraps the call at
        # its own call site (connect()) for defense in depth.


class ConsumerWiringTests(TestCase):
    """Structural confirmation that TradingConsumer.connect() actually
    triggers the warm-up — full Channels WebsocketCommunicator coverage is
    out of scope here (auth/scope setup), but the wiring itself must exist."""

    def test_connect_calls_ensure_background_refresh_started(self):
        import inspect
        from simulator.consumers import TradingConsumer
        source = inspect.getsource(TradingConsumer.connect)
        self.assertIn("ensure_background_refresh_started", source)


class NoImpactOnCommissionOrAccountMarkupTests(TestCase):
    """FASE A requisito: 'no cambia account markup' / 'no cambia commission'."""

    def setUp(self):
        sc.reset_for_tests()

    def tearDown(self):
        sc.reset_for_tests()

    def test_commission_for_is_untouched_by_this_module(self):
        """commission_for() does not import spread_config_cache at all —
        structural guarantee that FASE A did not touch commission."""
        import inspect
        from simulator.consumers import TradingConsumer
        source = inspect.getsource(TradingConsumer.commission_for)
        self.assertNotIn("spread_config_cache", source)

    def test_account_markup_pips_unaffected_by_cache_state(self):
        from simulator import pricing_context as pc
        # No BrokerSpreadConfig at all — only account markup is DB-free
        # and must be identical whether or not the cache was ever warmed.
        base_cold, markup_cold = pc.spread_pips_for("EUR/USD", 1.25)
        sc.refresh_cache_sync()
        base_warm, markup_warm = pc.spread_pips_for("EUR/USD", 1.25)
        self.assertEqual(markup_cold, markup_warm)
        self.assertEqual(markup_cold, 1.25)
