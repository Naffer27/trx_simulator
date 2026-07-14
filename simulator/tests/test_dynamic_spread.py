"""
simulator/tests/test_dynamic_spread.py — SPREAD-05.

Pure unit tests for simulator/dynamic_spread.py: no DB, no Django TestCase
needed for the engine itself (evaluate_dynamic_spread is a pure function),
though build_dynamic_inputs() touches the (in-memory, DB-free)
spread_config_cache so those specific tests use SimpleTestCase/TestCase as
needed for cache isolation via reset_for_tests().
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from simulator import dynamic_spread as ds
from simulator.spread_config_cache import reset_for_tests, refresh_cache_sync

from .factories import make_spread_config


def _inputs(**overrides) -> ds.DynamicSpreadInputs:
    base = dict(
        symbol="EUR/USD", base_spread_pips=2.0, account_markup_pips=0.0,
        is_dynamic=True, session_state="OPEN", source_state="LIVE", stale=False,
        volatility_pips=None, liquidity_score=None,
        manual_multiplier=None, manual_reason="", manual_expires_at=None,
        evaluated_at=1_700_000_000.0, min_spread_pips=None, max_spread_pips=None,
    )
    base.update(overrides)
    return ds.DynamicSpreadInputs(**base)


class StaticPathBitExactTests(SimpleTestCase):
    """1) is_dynamic=False produce resultado idéntico a SPREAD-04."""

    def test_dynamic_disabled_all_multipliers_neutral(self):
        decision = ds.evaluate_dynamic_spread(_inputs(is_dynamic=False))
        self.assertEqual(decision.session_multiplier, 1.0)
        self.assertEqual(decision.source_multiplier, 1.0)
        self.assertEqual(decision.stale_multiplier, 1.0)
        self.assertEqual(decision.volatility_multiplier, 1.0)
        self.assertEqual(decision.liquidity_multiplier, 1.0)
        self.assertEqual(decision.manual_multiplier, 1.0)
        self.assertFalse(decision.dynamic_spread_enabled)

    def test_dynamic_disabled_effective_equals_base_plus_markup(self):
        decision = ds.evaluate_dynamic_spread(
            _inputs(is_dynamic=False, base_spread_pips=15.0, account_markup_pips=1.5),
        )
        self.assertEqual(decision.effective_before_bounds, 16.5)
        self.assertEqual(decision.effective_after_bounds, 16.5)

    def test_dynamic_disabled_reason_codes(self):
        decision = ds.evaluate_dynamic_spread(_inputs(is_dynamic=False))
        self.assertEqual(decision.reason_codes, ("dynamic_disabled",))


class DynamicEnabledNeutralIsIdenticalTests(SimpleTestCase):
    """2) is_dynamic=True + todo neutral produce resultado idéntico."""

    def test_open_live_not_stale_no_manual_matches_static(self):
        static = ds.evaluate_dynamic_spread(_inputs(is_dynamic=False, base_spread_pips=15.0, account_markup_pips=0.5))
        dynamic = ds.evaluate_dynamic_spread(_inputs(
            is_dynamic=True, base_spread_pips=15.0, account_markup_pips=0.5,
            session_state="OPEN", source_state="LIVE", stale=False,
        ))
        self.assertEqual(static.effective_before_bounds, dynamic.effective_before_bounds)
        self.assertEqual(static.effective_after_bounds, dynamic.effective_after_bounds)


class SourceMultiplierTests(SimpleTestCase):
    """3) secondary/recovery/simulation."""

    def test_live_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state="LIVE"))
        self.assertEqual(d.source_multiplier, 1.00)

    def test_secondary(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state="SECONDARY"))
        self.assertEqual(d.source_multiplier, 1.05)

    def test_recovery(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state="RECOVERY"))
        self.assertEqual(d.source_multiplier, 1.10)

    def test_simulation(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state="SIMULATION"))
        self.assertEqual(d.source_multiplier, 1.15)

    def test_unrecognized_source_is_safe_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state="SOMETHING_NEW"))
        self.assertEqual(d.source_multiplier, 1.00)
        self.assertIn("source_unrecognized_neutral_default", d.reason_codes)

    def test_source_state_none_is_safe_neutral_with_reason(self):
        d = ds.evaluate_dynamic_spread(_inputs(source_state=None))
        self.assertEqual(d.source_multiplier, 1.00)
        self.assertIn("source_state_unavailable_safe_default", d.reason_codes)


class SessionMultiplierTests(SimpleTestCase):
    """4) session multipliers."""

    def test_open(self):
        d = ds.evaluate_dynamic_spread(_inputs(session_state="OPEN"))
        self.assertEqual(d.session_multiplier, 1.00)

    def test_pre_market(self):
        d = ds.evaluate_dynamic_spread(_inputs(session_state="PRE_MARKET"))
        self.assertEqual(d.session_multiplier, 1.25)

    def test_after_hours(self):
        d = ds.evaluate_dynamic_spread(_inputs(session_state="AFTER_HOURS"))
        self.assertEqual(d.session_multiplier, 1.35)

    def test_closed_family_gets_safe_wide_multiplier(self):
        for state in ("CLOSED", "MAINTENANCE", "WEEKEND", "HOLIDAY"):
            with self.subTest(state=state):
                d = ds.evaluate_dynamic_spread(_inputs(session_state=state))
                self.assertEqual(d.session_multiplier, 2.00)
                self.assertIn("session_market_closed_wide_spread", d.reason_codes)

    def test_unknown_gets_safe_explicit_policy(self):
        d = ds.evaluate_dynamic_spread(_inputs(session_state="UNKNOWN"))
        self.assertEqual(d.session_multiplier, 2.00)
        self.assertIn("session_unknown_safe_default", d.reason_codes)

    def test_session_state_none_is_safe_default(self):
        d = ds.evaluate_dynamic_spread(_inputs(session_state=None))
        self.assertEqual(d.session_multiplier, 2.00)
        self.assertIn("session_state_unavailable_safe_default", d.reason_codes)


class StaleSafeBehaviorTests(SimpleTestCase):
    """5) stale/closed safe behavior."""

    def test_not_stale_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(stale=False))
        self.assertEqual(d.stale_multiplier, 1.00)

    def test_stale_widens_defensively(self):
        d = ds.evaluate_dynamic_spread(_inputs(stale=True))
        self.assertEqual(d.stale_multiplier, 1.50)
        self.assertIn("source_stale_wide_spread", d.reason_codes)

    def test_stale_source_state_not_double_counted_on_source_axis(self):
        """STALE as a source_state value stays neutral on the source axis —
        the risk premium lives entirely in stale_multiplier, never both."""
        d = ds.evaluate_dynamic_spread(_inputs(source_state="STALE", stale=True))
        self.assertEqual(d.source_multiplier, 1.00)
        self.assertEqual(d.stale_multiplier, 1.50)


class VolatilityLiquidityTests(SimpleTestCase):
    """6) volatility/liquidity."""

    def test_volatility_absent_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(volatility_pips=None))
        self.assertEqual(d.volatility_multiplier, 1.00)

    def test_volatility_present_widens_boundedly(self):
        d = ds.evaluate_dynamic_spread(_inputs(volatility_pips=50.0))
        self.assertEqual(d.volatility_multiplier, 1.50)

    def test_volatility_clamped_at_max(self):
        d = ds.evaluate_dynamic_spread(_inputs(volatility_pips=10_000.0))
        self.assertEqual(d.volatility_multiplier, ds.VOLATILITY_MULTIPLIER_MAX)

    def test_liquidity_absent_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(liquidity_score=None))
        self.assertEqual(d.liquidity_multiplier, 1.00)

    def test_liquidity_full_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(liquidity_score=1.0))
        self.assertEqual(d.liquidity_multiplier, 1.00)

    def test_liquidity_zero_is_max_widening(self):
        d = ds.evaluate_dynamic_spread(_inputs(liquidity_score=0.0))
        self.assertEqual(d.liquidity_multiplier, ds.LIQUIDITY_MULTIPLIER_MAX)

    def test_liquidity_out_of_range_is_clamped_not_rejected(self):
        d = ds.evaluate_dynamic_spread(_inputs(liquidity_score=5.0))
        self.assertEqual(d.liquidity_multiplier, 1.00)  # clamped to 1.0 -> neutral


class ManualOverrideTests(SimpleTestCase):
    """7) manual override válido. 8) override expirado."""

    def test_valid_manual_override_applies(self):
        d = ds.evaluate_dynamic_spread(_inputs(manual_multiplier=1.20, manual_reason="news event"))
        self.assertEqual(d.manual_multiplier, 1.20)
        self.assertIn("manual_override_active:news event", d.reason_codes)

    def test_manual_override_neutral_value_is_a_noop(self):
        d = ds.evaluate_dynamic_spread(_inputs(manual_multiplier=1.00))
        self.assertEqual(d.manual_multiplier, 1.00)
        self.assertIn("manual_override_neutral", d.reason_codes)

    def test_manual_override_not_expired_applies(self):
        d = ds.evaluate_dynamic_spread(_inputs(
            manual_multiplier=1.30, manual_expires_at=2_000_000_000.0, evaluated_at=1_700_000_000.0,
        ))
        self.assertEqual(d.manual_multiplier, 1.30)

    def test_manual_override_expired_falls_back_to_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(
            manual_multiplier=1.30, manual_expires_at=1_600_000_000.0, evaluated_at=1_700_000_000.0,
        ))
        self.assertEqual(d.manual_multiplier, 1.00)
        self.assertIn("manual_override_expired", d.reason_codes)

    def test_manual_override_invalid_value_falls_back_to_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(manual_multiplier=-1.0))
        self.assertEqual(d.manual_multiplier, 1.00)
        self.assertIn("manual_override_invalid", d.reason_codes)

    def test_manual_override_absent_is_neutral(self):
        d = ds.evaluate_dynamic_spread(_inputs(manual_multiplier=None))
        self.assertEqual(d.manual_multiplier, 1.00)
        self.assertIn("manual_override_absent", d.reason_codes)


class BoundsOptInTests(SimpleTestCase):
    """9) bounds opt-in."""

    def test_no_bounds_no_clamp(self):
        d = ds.evaluate_dynamic_spread(_inputs(
            base_spread_pips=15.0, session_state="AFTER_HOURS",  # 15 * 1.35 = 20.25
            min_spread_pips=None, max_spread_pips=None,
        ))
        self.assertFalse(d.floor_applied)
        self.assertFalse(d.ceiling_applied)
        self.assertEqual(d.effective_after_bounds, d.effective_before_bounds)

    def test_ceiling_applies_when_configured(self):
        d = ds.evaluate_dynamic_spread(_inputs(
            base_spread_pips=15.0, session_state="AFTER_HOURS",
            max_spread_pips=10.0,
        ))
        self.assertTrue(d.ceiling_applied)
        self.assertEqual(d.effective_after_bounds, 10.0)

    def test_floor_applies_when_configured(self):
        d = ds.evaluate_dynamic_spread(_inputs(
            base_spread_pips=0.0, account_markup_pips=0.0, session_state="OPEN",
            min_spread_pips=1.0,
        ))
        self.assertTrue(d.floor_applied)
        self.assertEqual(d.effective_after_bounds, 1.0)


class DeterminismTests(SimpleTestCase):
    """13) determinismo."""

    def test_same_inputs_same_decision_id(self):
        inputs = _inputs(manual_multiplier=1.10, session_state="PRE_MARKET")
        d1 = ds.evaluate_dynamic_spread(inputs)
        d2 = ds.evaluate_dynamic_spread(inputs)
        self.assertEqual(d1.decision_id, d2.decision_id)
        self.assertEqual(d1, d2)

    def test_different_inputs_different_decision_id(self):
        d1 = ds.evaluate_dynamic_spread(_inputs(session_state="OPEN"))
        d2 = ds.evaluate_dynamic_spread(_inputs(session_state="PRE_MARKET"))
        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_no_randomness_no_uuid(self):
        import inspect
        source = inspect.getsource(ds)
        self.assertNotIn("import random", source)
        self.assertNotIn("import uuid", source)
        self.assertNotIn("uuid.uuid4", source)


class NeverRaisesTests(SimpleTestCase):
    """14) fallo de session/observability no bloquea — evaluate_dynamic_spread
    itself never raises even given garbage inputs (build_dynamic_inputs()'s
    own try/except is covered in the integration test file)."""

    def test_garbage_volatility_does_not_raise(self):
        d = ds.evaluate_dynamic_spread(_inputs(volatility_pips="not-a-number"))
        self.assertEqual(d.volatility_multiplier, 1.00)

    def test_garbage_liquidity_does_not_raise(self):
        d = ds.evaluate_dynamic_spread(_inputs(liquidity_score="not-a-number"))
        self.assertEqual(d.liquidity_multiplier, 1.00)


class BuildDynamicInputsTests(TestCase):
    """build_dynamic_inputs() — zero DB, reads the process-wide cache."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_no_config_row_returns_safe_non_dynamic_inputs(self):
        from .factories import make_account
        account = make_account(balance=Decimal("10000"))
        from simulator import commercial_pricing as cp
        profile = cp.build_commercial_pricing_profile({}, "EUR/USD")
        inputs = ds.build_dynamic_inputs("EUR/USD", profile, 1_700_000_000)
        self.assertFalse(inputs.is_dynamic)
        self.assertEqual(inputs.base_spread_pips, 0.0)

    def test_is_dynamic_false_row_produces_non_dynamic_inputs(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=False)
        refresh_cache_sync()
        from simulator import commercial_pricing as cp
        profile = cp.build_commercial_pricing_profile({}, "EUR/USD")
        inputs = ds.build_dynamic_inputs("EUR/USD", profile, 1_700_000_000)
        self.assertFalse(inputs.is_dynamic)
        self.assertIsNone(inputs.session_state)  # never evaluated when not dynamic
        self.assertIsNone(inputs.source_state)

    def test_is_dynamic_true_row_evaluates_session_and_source(self):
        make_spread_config(symbol="BTCUSD", spread_pips=Decimal("15.00"), is_dynamic=True)
        refresh_cache_sync()
        from simulator import commercial_pricing as cp
        profile = cp.build_commercial_pricing_profile({}, "BTCUSD")
        inputs = ds.build_dynamic_inputs("BTCUSD", profile, 1_700_000_000)
        self.assertTrue(inputs.is_dynamic)
        self.assertIsNotNone(inputs.session_state)  # BTCUSD is 24/7 -> OPEN

    def test_zero_orm_queries(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), is_dynamic=True)
        refresh_cache_sync()
        from simulator import commercial_pricing as cp
        profile = cp.build_commercial_pricing_profile({}, "EUR/USD")
        with self.assertNumQueries(0):
            ds.build_dynamic_inputs("EUR/USD", profile, 1_700_000_000)
