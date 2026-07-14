"""
simulator/tests/test_dynamic_spread_integration.py — SPREAD-05.

Covers the wiring: spread_engine.broker_price()/compute_effective_spread_pips()
delegation, consumers.py::price_tick() building dynamic inputs once and
reusing them for both the fill and the pricing-context snapshot, zero ORM
per tick, determinism end-to-end, and the seed_broker_spread_configs
management command.
"""
import asyncio
import io
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.core.management import call_command
from django.test import TestCase

from market_data.symbol_specs import allowed_symbols, get_spec
from simulator import commercial_pricing as cp
from simulator import pricing_context as pc
from simulator.consumers import TradingConsumer
from simulator.models import BrokerSpreadConfig
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests
from simulator.spread_engine import broker_price

from .factories import make_spread_config


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer(commercial_fields: dict | None = None) -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = "EUR/USD"
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {
        "balance": 10000.0, "spread_pips": 0.0,
        "commercial_pricing_fields": commercial_fields or {},
    }
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


def _tick(symbol: str, bid: float, ask: float, ts: int = 1_700_000_000) -> dict:
    mid = round((bid + ask) / 2, 5)
    return {"symbol": symbol, "bid": bid, "ask": ask, "mid": mid, "time": ts}


class StaticPathUnchangedByBrokerPriceTests(TestCase):
    """1) is_dynamic=False produce resultado idéntico a SPREAD-04 — through
    the real broker_price() entry point, not just the pure engine."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_static_row_broker_price_unchanged(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=False)
        refresh_cache_sync()
        bid, ask = broker_price("EUR/USD", 1.10000, 1.10100)
        self.assertAlmostEqual(bid, 1.10000 - 0.0001, places=5)
        self.assertAlmostEqual(ask, 1.10100 + 0.0001, places=5)

    def test_no_dynamic_inputs_passed_static_even_if_row_is_dynamic(self):
        """A caller that never passes dynamic_inputs (e.g. legacy/direct
        callers) always gets the static formula, regardless of the row's
        is_dynamic flag — compute_effective_spread_pips() only delegates
        when BOTH is_dynamic AND dynamic_inputs are present."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        bid, ask = broker_price("EUR/USD", 1.10000, 1.10100)  # no dynamic_inputs kwarg
        self.assertAlmostEqual(bid, 1.10000 - 0.0001, places=5)
        self.assertAlmostEqual(ask, 1.10100 + 0.0001, places=5)


class BtcusdBaseIsPreservedTests(TestCase):
    """10) BTCUSD sin bounds conserva base."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_dynamic_enabled_open_session_live_source_keeps_15_pips(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        c.symbol = "BTCUSD"

        with patch("market_data.sessions.evaluate_market_session_for_symbol") as mock_session, \
             patch("market_data.observability.get_symbol_state") as mock_obs:
            from market_data.sessions import MarketSessionResult, MarketSessionState, SessionReasonCode, CalendarId
            from market_data.contracts import OrderPolicy
            from datetime import datetime, timezone
            mock_session.return_value = MarketSessionResult(
                canonical_symbol="BTCUSD", calendar_id=CalendarId.CRYPTO_24_7,
                state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                evaluated_at=datetime.now(timezone.utc), reason_code=SessionReasonCode.MARKET_OPEN,
                timezone="UTC",
            )
            mock_obs.return_value = type("Obs", (), {"source_state": None})()
            _run(c.price_tick(_tick("BTCUSD", bid=82000.00, ask=82015.00)))

        self.assertAlmostEqual(c._bid_state["BTCUSD"], 82000.00 - 7.5, places=2)
        self.assertAlmostEqual(c._ask_state["BTCUSD"], 82015.00 + 7.5, places=2)

    def test_static_default_keeps_15_pips(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"))  # is_dynamic=False default
        refresh_cache_sync()
        c = _bare_consumer()
        c.symbol = "BTCUSD"
        _run(c.price_tick(_tick("BTCUSD", bid=82000.00, ask=82015.00)))
        self.assertAlmostEqual(c._bid_state["BTCUSD"], 82000.00 - 7.5, places=2)
        self.assertAlmostEqual(c._ask_state["BTCUSD"], 82015.00 + 7.5, places=2)


class PricingContextRecordsExactDecisionTests(TestCase):
    """11) pricing context guarda decisión exacta."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_static_row_context_has_none_dynamic_fields(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=False)
        refresh_cache_sync()
        c = _bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertFalse(ctx["dynamic_spread_enabled"])
        self.assertEqual(ctx["session_multiplier"], 1.0)
        self.assertEqual(ctx["reason_codes"], ["dynamic_disabled"])

    def test_dynamic_row_context_has_full_decision(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertTrue(ctx["dynamic_spread_enabled"])
        self.assertIsNotNone(ctx["session_multiplier"])
        self.assertIsNotNone(ctx["decision_id"])
        self.assertIsInstance(ctx["reason_codes"], list)

    def test_context_reflects_tick_time_snapshot_not_a_later_reread(self):
        """Same SPREAD-02b invariant, now for the dynamic decision: config
        changes after the tick must not leak into the captured context."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        ctx_at_tick = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)

        BrokerSpreadConfig.objects.filter(symbol="EUR/USD").update(is_dynamic=False)
        refresh_cache_sync()

        ctx_after_change = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx_at_tick["decision_id"], ctx_after_change["decision_id"])
        self.assertTrue(ctx_after_change["dynamic_spread_enabled"])  # still reflects the tick, not the new state


class ZeroOrmPerTickTests(TestCase):
    """12) cero ORM por tick — including the dynamic path."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_price_tick_zero_queries_when_dynamic(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        with self.assertNumQueries(0):
            _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))


class DeterminismEndToEndTests(TestCase):
    """13) determinismo — same tick replayed twice yields the same decision."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_same_tick_same_decision_id(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c1 = _bare_consumer()
        c2 = _bare_consumer()
        tick = _tick("EUR/USD", bid=1.09990, ask=1.10010, ts=1_700_000_123)
        _run(c1.price_tick(dict(tick)))
        _run(c2.price_tick(dict(tick)))
        ctx1 = c1._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        ctx2 = c2._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx1["decision_id"], ctx2["decision_id"])
        self.assertEqual(ctx1["effective_spread_pips"], ctx2["effective_spread_pips"])


class SessionObservabilityFailureDoesNotBlockTests(TestCase):
    """14) fallo de session/observability no bloquea."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_session_evaluation_exception_does_not_block_tick(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        with patch(
            "market_data.sessions.evaluate_market_session_for_symbol",
            side_effect=RuntimeError("boom"),
        ):
            _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        self.assertIn("EUR/USD", c._bid_state)  # tick still processed

    def test_observability_exception_does_not_block_tick(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        c = _bare_consumer()
        with patch(
            "market_data.observability.get_symbol_state",
            side_effect=RuntimeError("boom"),
        ):
            _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        self.assertIn("EUR/USD", c._bid_state)


class SeedCommandTests(TestCase):
    """15) seed crea exactamente 6. 16) idempotente. 17) sin force no pisa
    EUR/USD existente. 18) con force actualiza. 19) ningún símbolo
    deshabilitado se activa."""

    def test_seed_creates_exactly_the_enabled_symbols(self):
        out = io.StringIO()
        call_command("seed_broker_spread_configs", stdout=out)
        rows = set(BrokerSpreadConfig.objects.values_list("symbol", flat=True))
        self.assertEqual(rows, set(allowed_symbols()))
        self.assertEqual(len(rows), 6)
        self.assertIn("created=6", out.getvalue())

    def test_seeded_rows_are_inert_by_default(self):
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        for row in BrokerSpreadConfig.objects.all():
            self.assertFalse(row.is_dynamic)
            self.assertFalse(row.spread_bounds_enabled)
            self.assertEqual(row.manual_multiplier, Decimal("1.000"))

    def test_seed_is_idempotent(self):
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        out = io.StringIO()
        call_command("seed_broker_spread_configs", stdout=out)
        self.assertIn("skipped=6", out.getvalue())
        self.assertIn("created=0", out.getvalue())

    def test_without_force_does_not_overwrite_existing_eurusd(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True,
                            min_spread=Decimal("0.75"), max_spread=Decimal("9.00"), bounds_enabled=True)
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        row = BrokerSpreadConfig.objects.get(symbol="EUR/USD")
        self.assertEqual(row.spread_pips, Decimal("2.00"))  # untouched
        self.assertTrue(row.is_dynamic)  # untouched
        self.assertTrue(row.spread_bounds_enabled)  # untouched

    def test_with_force_updates_spread_pips_only(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("99.00"), is_dynamic=True,
                            bounds_enabled=True, min_spread=Decimal("1.00"), max_spread=Decimal("2.00"))
        out = io.StringIO()
        call_command("seed_broker_spread_configs", "--force-update", stdout=out)
        row = BrokerSpreadConfig.objects.get(symbol="EUR/USD")
        expected = Decimal(str(round(get_spec("EUR/USD").spread / get_spec("EUR/USD").pip_size, 2)))
        self.assertEqual(row.spread_pips, expected)
        # operator decisions (is_dynamic/bounds) are never touched, even with --force-update
        self.assertTrue(row.is_dynamic)
        self.assertTrue(row.spread_bounds_enabled)
        self.assertIn("updated=1", out.getvalue())

    def test_no_disabled_symbol_gets_seeded(self):
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        seeded = set(BrokerSpreadConfig.objects.values_list("symbol", flat=True))
        for disabled_symbol in ("XAU/USD", "XAG/USD", "US30", "US500", "NAS100", "SOLUSD",
                                 "USD/CAD", "USD/CHF", "NZD/USD"):
            self.assertNotIn(disabled_symbol, seeded)

    def test_no_usoil_symbol_exists_at_all(self):
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        seeded = set(BrokerSpreadConfig.objects.values_list("symbol", flat=True))
        self.assertNotIn("USOIL", seeded)
        self.assertNotIn("USOIL", allowed_symbols())

    def test_seeded_spread_pips_match_symbol_spec_units(self):
        call_command("seed_broker_spread_configs", stdout=io.StringIO())
        btc = BrokerSpreadConfig.objects.get(symbol="BTCUSD")
        self.assertEqual(btc.spread_pips, Decimal("15.00"))
        eur = BrokerSpreadConfig.objects.get(symbol="EUR/USD")
        self.assertEqual(eur.spread_pips, Decimal("1.50"))
