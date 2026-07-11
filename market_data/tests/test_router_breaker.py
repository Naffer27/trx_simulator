"""
market_data/tests/test_router_breaker.py — FOUNDATION-05.

Circuit breaker pure transition functions. Every "now" is a plain int
supplied by the test — nothing here sleeps or reads a real clock.

Pure unittest, no Django dependency.
"""

import unittest

from market_data.contracts import OrderPolicy, ProviderHealthState
from market_data.router.breaker import (
    CircuitBreakerState,
    maybe_transition_to_half_open,
    record_failure,
    record_success,
)
from market_data.router.models import ProviderRoutePlan


def make_plan(**overrides):
    defaults = dict(
        canonical_symbol="BTCUSD",
        entries=(),
        simulation_allowed=True,
        default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
        max_failures=3,
        open_cooldown_seconds=60,
        half_open_successes_required=2,
    )
    defaults.update(overrides)
    return ProviderRoutePlan(**defaults)


def make_state(**overrides):
    defaults = dict(provider_id="binance", canonical_symbol="BTCUSD")
    defaults.update(overrides)
    return CircuitBreakerState(**defaults)


class ConstructionTests(unittest.TestCase):
    def test_defaults(self):
        state = make_state()
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.opened_at)

    def test_empty_ids_rejected(self):
        with self.assertRaises(ValueError):
            make_state(provider_id="")
        with self.assertRaises(ValueError):
            make_state(canonical_symbol="")

    def test_negative_counters_rejected(self):
        with self.assertRaises(ValueError):
            make_state(consecutive_failures=-1)
        with self.assertRaises(ValueError):
            make_state(half_open_successes=-1)


class ClosedStateTests(unittest.TestCase):
    def test_success_resets_failures(self):
        state = make_state(consecutive_failures=2)
        plan = make_plan()
        new_state = record_success(state, plan=plan, now=100)
        self.assertEqual(new_state.consecutive_failures, 0)
        self.assertEqual(new_state.health, ProviderHealthState.CLOSED)

    def test_failures_accumulate_below_threshold(self):
        plan = make_plan(max_failures=3)
        state = make_state()
        state = record_failure(state, plan=plan, now=1)
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 1)
        state = record_failure(state, plan=plan, now=2)
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 2)

    def test_closed_to_open_after_max_failures(self):
        plan = make_plan(max_failures=3)
        state = make_state()
        for t in (1, 2, 3):
            state = record_failure(state, plan=plan, now=t, error_code="TIMEOUT")
        self.assertEqual(state.health, ProviderHealthState.OPEN)
        self.assertEqual(state.consecutive_failures, 3)
        self.assertEqual(state.opened_at, 3)
        self.assertEqual(state.last_error_code, "TIMEOUT")


class OpenStateTests(unittest.TestCase):
    def test_open_stays_open_before_cooldown(self):
        plan = make_plan(open_cooldown_seconds=60)
        state = make_state(health=ProviderHealthState.OPEN, opened_at=100, consecutive_failures=3)
        new_state = maybe_transition_to_half_open(state, plan=plan, now=159)
        self.assertEqual(new_state.health, ProviderHealthState.OPEN)

    def test_open_to_half_open_after_cooldown(self):
        plan = make_plan(open_cooldown_seconds=60)
        state = make_state(health=ProviderHealthState.OPEN, opened_at=100, consecutive_failures=3)
        new_state = maybe_transition_to_half_open(state, plan=plan, now=160)
        self.assertEqual(new_state.health, ProviderHealthState.HALF_OPEN)
        self.assertEqual(new_state.half_open_successes, 0)

    def test_failure_while_open_does_not_reopen_or_reset(self):
        plan = make_plan()
        state = make_state(health=ProviderHealthState.OPEN, opened_at=100, consecutive_failures=3)
        new_state = record_failure(state, plan=plan, now=110, error_code="TIMEOUT")
        self.assertEqual(new_state.health, ProviderHealthState.OPEN)
        self.assertEqual(new_state.consecutive_failures, 4)

    def test_success_while_open_is_a_noop(self):
        plan = make_plan()
        state = make_state(health=ProviderHealthState.OPEN, opened_at=100, consecutive_failures=3)
        new_state = record_success(state, plan=plan, now=110)
        self.assertEqual(new_state, state)


class HalfOpenStateTests(unittest.TestCase):
    def test_half_open_success_increments_without_closing_below_threshold(self):
        plan = make_plan(half_open_successes_required=2)
        state = make_state(health=ProviderHealthState.HALF_OPEN, half_open_successes=0)
        new_state = record_success(state, plan=plan, now=200)
        self.assertEqual(new_state.health, ProviderHealthState.HALF_OPEN)
        self.assertEqual(new_state.half_open_successes, 1)

    def test_half_open_to_closed_after_required_successes(self):
        plan = make_plan(half_open_successes_required=2)
        state = make_state(health=ProviderHealthState.HALF_OPEN, half_open_successes=0, opened_at=100)
        state = record_success(state, plan=plan, now=200)
        state = record_success(state, plan=plan, now=201)
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.opened_at)
        self.assertEqual(state.half_open_successes, 0)

    def test_half_open_failure_reopens_immediately(self):
        plan = make_plan(max_failures=3)
        state = make_state(health=ProviderHealthState.HALF_OPEN, half_open_successes=1, consecutive_failures=3)
        new_state = record_failure(state, plan=plan, now=300, error_code="TIMEOUT")
        self.assertEqual(new_state.health, ProviderHealthState.OPEN)
        self.assertEqual(new_state.opened_at, 300)
        self.assertEqual(new_state.half_open_successes, 0)
        self.assertEqual(new_state.last_error_code, "TIMEOUT")


if __name__ == "__main__":
    unittest.main()
