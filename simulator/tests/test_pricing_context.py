"""
simulator/tests/test_pricing_context.py — SPREAD-02.

Pure unit tests for simulator/pricing_context.py: schema shape/versioning,
effective_spread_pips arithmetic, BrokerSpreadConfig read (base pips),
best-effort provider/source_state read from market_data.observability, and
the never-raises guarantee for every public function.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase

from market_data.contracts import OrderPolicy, SourceState
from market_data.observability import record_selection, reset_observability_state
from market_data.router.models import ReasonCode
from simulator import pricing_context as pc

from .factories import make_spread_config


class BuildPricingContextTests(SimpleTestCase):
    def test_shape_has_schema_version(self):
        ctx = pc.build_pricing_context(pricing_profile="ws_manual_open")
        self.assertEqual(ctx["schema_version"], pc.SCHEMA_VERSION)

    def test_all_expected_keys_present(self):
        ctx = pc.build_pricing_context(pricing_profile="ws_manual_open")
        expected = {
            "schema_version", "raw_bid", "raw_ask", "executable_bid", "executable_ask",
            "base_spread_pips", "account_markup_pips", "effective_spread_pips",
            "effective_spread_pips_pre_clamp", "min_spread_pips", "max_spread_pips",
            "spread_bound_applied", "profile_id", "provider_id", "source_state",
            "router_provider", "pricing_timestamp", "pricing_profile",
        }
        self.assertEqual(set(ctx.keys()), expected)

    def test_effective_spread_pips_is_base_plus_markup(self):
        ctx = pc.build_pricing_context(
            base_spread_pips=Decimal("1.5"), account_markup_pips=0.5,
            pricing_profile="ws_manual_open",
        )
        self.assertEqual(ctx["effective_spread_pips"], 2.0)

    def test_effective_spread_pips_none_plus_none_is_none(self):
        ctx = pc.build_pricing_context(pricing_profile="ws_manual_open")
        self.assertIsNone(ctx["effective_spread_pips"])

    def test_effective_spread_pips_only_base_set(self):
        ctx = pc.build_pricing_context(base_spread_pips=2.0, pricing_profile="ws_manual_open")
        self.assertEqual(ctx["effective_spread_pips"], 2.0)

    def test_effective_spread_pips_only_markup_set(self):
        ctx = pc.build_pricing_context(account_markup_pips=1.0, pricing_profile="ws_manual_open")
        self.assertEqual(ctx["effective_spread_pips"], 1.0)

    def test_prices_coerced_to_float(self):
        ctx = pc.build_pricing_context(
            raw_bid=Decimal("1.10000"), raw_ask=Decimal("1.10020"),
            executable_bid=Decimal("1.09990"), executable_ask=Decimal("1.10030"),
            pricing_profile="ws_manual_open",
        )
        for key in ("raw_bid", "raw_ask", "executable_bid", "executable_ask"):
            self.assertIsInstance(ctx[key], float)

    def test_pricing_timestamp_defaults_to_now_when_omitted(self):
        import time
        before = time.time()
        ctx = pc.build_pricing_context(pricing_profile="ws_manual_open")
        after = time.time()
        self.assertTrue(before <= ctx["pricing_timestamp"] <= after)

    def test_pricing_timestamp_explicit_value_preserved(self):
        ctx = pc.build_pricing_context(pricing_timestamp=12345, pricing_profile="ws_manual_open")
        self.assertEqual(ctx["pricing_timestamp"], 12345.0)

    def test_pricing_profile_preserved(self):
        ctx = pc.build_pricing_context(pricing_profile="daemon_stopout")
        self.assertEqual(ctx["pricing_profile"], "daemon_stopout")

    def test_provider_fields_default_none(self):
        ctx = pc.build_pricing_context(pricing_profile="ws_manual_open")
        self.assertIsNone(ctx["provider_id"])
        self.assertIsNone(ctx["source_state"])
        self.assertIsNone(ctx["router_provider"])

    def test_never_raises_on_garbage_input(self):
        # raw_bid is not coercible to float — must degrade, not raise.
        ctx = pc.build_pricing_context(raw_bid=object(), pricing_profile="ws_manual_open")
        self.assertIsNone(ctx["raw_bid"])
        self.assertEqual(ctx["pricing_profile"], "ws_manual_open")


class SpreadPipsForTests(TestCase):
    def setUp(self):
        from simulator.spread_config_cache import reset_for_tests
        reset_for_tests()

    def tearDown(self):
        from simulator.spread_config_cache import reset_for_tests
        reset_for_tests()

    def test_reads_broker_spread_config_base_pips(self):
        from simulator.spread_config_cache import refresh_cache_sync
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        base, markup = pc.spread_pips_for("EUR/USD", 0.5)
        self.assertEqual(base, 2.0)
        self.assertEqual(markup, 0.5)

    def test_no_config_row_returns_none_base(self):
        base, markup = pc.spread_pips_for("GBP/USD", 1.0)
        self.assertIsNone(base)
        self.assertEqual(markup, 1.0)

    def test_disabled_config_returns_none_base(self):
        from simulator.spread_config_cache import refresh_cache_sync
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=False)
        refresh_cache_sync()
        base, _ = pc.spread_pips_for("EUR/USD", 0.0)
        self.assertIsNone(base)

    def test_none_markup_stays_none(self):
        base, markup = pc.spread_pips_for("EUR/USD", None)
        self.assertIsNone(markup)

    def test_never_raises_when_config_lookup_fails(self):
        with patch("simulator.spread_engine._get_config", side_effect=RuntimeError("boom")):
            base, markup = pc.spread_pips_for("EUR/USD", 1.0)
        self.assertIsNone(base)
        self.assertEqual(markup, 1.0)


class ProviderStateForTests(TestCase):
    def setUp(self):
        reset_observability_state()

    def test_no_data_returns_none_none(self):
        provider_id, source_state = pc.provider_state_for("BTCUSD")
        self.assertIsNone(provider_id)
        self.assertIsNone(source_state)

    def test_reads_recorded_selection(self):
        record_selection(
            "BTCUSD", provider_id="binance", provider_symbol="BTCUSDT",
            source_state=SourceState.LIVE, order_policy=OrderPolicy.OPEN_NORMAL,
            degraded=False, reason_code=ReasonCode.PRIMARY_SELECTED,
        )
        provider_id, source_state = pc.provider_state_for("BTCUSD")
        self.assertEqual(provider_id, "binance")
        self.assertEqual(source_state, "LIVE")

    def test_never_raises_when_observability_broken(self):
        with patch("market_data.observability.get_symbol_state", side_effect=RuntimeError("boom")):
            provider_id, source_state = pc.provider_state_for("BTCUSD")
        self.assertIsNone(provider_id)
        self.assertIsNone(source_state)
