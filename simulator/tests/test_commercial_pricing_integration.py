"""
simulator/tests/test_commercial_pricing_integration.py — SPREAD-04.

Covers the consumers.py wiring: commission_for() decided from the resolved
profile, price_tick() applying the floor/ceiling clamp and capturing
profile_id/min/max/pre-clamp in the pricing-context audit trail, zero DB
access per tick, and confirmation that PnL/margin formulas are untouched.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase

from simulator import commercial_pricing as cp
from simulator import pricing_context as pc
from simulator.consumers import TradingConsumer
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests

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


def _tick(bid: float, ask: float, ts: int = 1_700_000_000) -> dict:
    mid = round((bid + ask) / 2, 5)
    return {"symbol": "EUR/USD", "bid": bid, "ask": ask, "mid": mid, "time": ts}


def _profile_fields(**overrides) -> dict:
    base = dict(
        profile_version=1, profile_id="test_profile", account_type="STANDARD",
        product_type="STANDARD", spread_markup_pips=0.0, commission_per_lot=0.0,
        commission_pct=0.0, min_spread_pips=None, max_spread_pips=None,
        enabled=True, source=cp.SOURCE_ACCOUNT_PRODUCT,
    )
    base.update(overrides)
    return base


class CommissionForResolverTests(TestCase):
    """8) commission per-lot. 9) commission pct. 10) comisión cero explícita.
    14) legacy fallback."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_per_lot_takes_priority(self):
        c = _bare_consumer(_profile_fields(commission_per_lot=5.0, commission_pct=0.01))
        commission = c.commission_for("EUR/USD", qty=2.0, price=1.10000)
        self.assertEqual(commission, 10.0)  # 2.0 lots * 5.0, not the pct

    def test_pct_used_when_no_per_lot(self):
        c = _bare_consumer(_profile_fields(commission_per_lot=0.0, commission_pct=0.0001))
        commission = c.commission_for("EUR/USD", qty=1.0, price=1.10000)
        # notional = 1.0 * 1.10000 * 100_000 = 110000; * 0.0001 = 11.0
        self.assertAlmostEqual(commission, 11.0, places=2)

    def test_explicit_zero_commission_is_truly_zero(self):
        """The core SPREAD-04 fix: an ECN-style profile with commission_per_lot=0
        AND commission_pct=0, resolved from a REAL product (not a fallback),
        must charge exactly $0 — not silently fall through to spec.commission_pct
        like the pre-SPREAD-04 code did."""
        c = _bare_consumer(_profile_fields(
            commission_per_lot=0.0, commission_pct=0.0, source=cp.SOURCE_ACCOUNT_PRODUCT,
        ))
        commission = c.commission_for("EUR/USD", qty=1.0, price=1.10000)
        self.assertEqual(commission, 0.0)

    def test_legacy_fallback_source_uses_spec_commission_pct(self):
        from market_data.symbol_specs import get_spec
        c = _bare_consumer({})  # empty commercial_pricing_fields -> legacy_fallback
        commission = c.commission_for("BTCUSD", qty=0.01, price=82000.0)
        spec = get_spec("BTCUSD")
        expected = max(0.0, 0.01 * 82000.0 * spec.contract_size * spec.commission_pct)
        self.assertAlmostEqual(commission, expected, places=6)
        self.assertGreater(commission, 0.0)  # BTCUSD has a non-zero spec.commission_pct

    def test_commission_rounds_to_cents_for_per_lot(self):
        c = _bare_consumer(_profile_fields(commission_per_lot=3.333))
        commission = c.commission_for("EUR/USD", qty=1.0, price=1.1)
        self.assertEqual(commission, 3.33)


class PriceTickClampAndSnapshotTests(TestCase):
    """11) floor. 12) ceiling. 13) base + markup. 17) pricing context
    registra profile/floor/ceiling."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_effective_pips_is_base_plus_markup_when_within_bounds(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("1.50"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=1.0))

        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips_pre_clamp"], 2.5)  # 1.5 + 1.0
        self.assertEqual(ctx["effective_spread_pips"], 2.5)  # within [0.50, 5.00] — no clamp

    def test_floor_clamps_up(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("0.00"),
                            min_spread=Decimal("1.00"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.0))

        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips_pre_clamp"], 0.0)
        self.assertEqual(ctx["effective_spread_pips"], 1.00)  # floored up

    def test_ceiling_clamps_down(self):
        """An admin who explicitly opts in (spread_bounds_enabled=True) and
        sets a real ceiling gets it enforced. This is now opt-in, not a
        side-effect of the model's old 5.00 default — see
        test_spread_bounds_optin.py for the "default does NOT clamp"
        coverage of the exact BTCUSD scenario this used to (incorrectly)
        trigger silently."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.0))

        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips_pre_clamp"], 15.00)
        self.assertEqual(ctx["effective_spread_pips"], 5.00)  # capped down

    def test_account_level_override_widens_the_ceiling(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.0, max_spread_pips=20.0))

        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips"], 15.00)  # override wins, no clamp

    def test_pricing_context_records_profile_id_and_bounds(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(profile_id="challenge_product:7", spread_markup_pips=0.5))

        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["profile_id"], "challenge_product:7")
        self.assertEqual(ctx["min_spread_pips"], 0.50)
        self.assertEqual(ctx["max_spread_pips"], 5.00)

    def test_executable_price_matches_the_clamped_value(self):
        from market_data.symbol_specs import get_spec
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("15.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.0))

        raw_bid, raw_ask = 1.09990, 1.10010
        _run(c.price_tick(_tick(bid=raw_bid, ask=raw_ask)))

        spec = get_spec("EUR/USD")
        extra = 5.00 * spec.pip_size / 2  # clamped to ceiling, not 15.00
        expected_bid = round(raw_bid - extra, spec.price_decimals)
        expected_ask = round(raw_ask + extra, spec.price_decimals)
        self.assertEqual(c._bid_state["EUR/USD"], expected_bid)
        self.assertEqual(c._ask_state["EUR/USD"], expected_ask)


class ZeroDbPerTickTests(TestCase):
    """18) cero DB por tick."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_price_tick_performs_zero_orm_queries(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.5))

        with self.assertNumQueries(0):
            _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))

    def test_commission_for_performs_zero_orm_queries(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(commission_pct=0.0001))
        with self.assertNumQueries(0):
            c.commission_for("EUR/USD", qty=1.0, price=1.1)

    def test_resolve_commercial_pricing_profile_performs_zero_orm_queries(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"))
        refresh_cache_sync()
        c = _bare_consumer(_profile_fields(spread_markup_pips=0.5))
        with self.assertNumQueries(0):
            c._resolve_commercial_pricing_profile("EUR/USD")


class NoChangeToPnlOrMarginTests(TestCase):
    """19) no cambia PnL/margen — structural guarantee: the PnL/margin
    formulas never import or reference commercial_pricing at all."""

    def test_unrealized_pnl_formula_untouched(self):
        import inspect
        from simulator.consumers import TradingConsumer
        source = inspect.getsource(TradingConsumer._unrealized_pnl_for)
        self.assertNotIn("commercial_pricing", source)
        self.assertNotIn("resolve_commercial", source)

    def test_margin_guard_untouched(self):
        import inspect
        from simulator.consumers import _compute_pretrade_margin_guard
        source = inspect.getsource(_compute_pretrade_margin_guard)
        self.assertNotIn("commercial_pricing", source)
