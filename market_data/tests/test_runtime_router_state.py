"""
market_data/tests/test_runtime_router_state.py — FOUNDATION-10.

market_data/runtime_router/state.py: the process-wide ProviderRouter
singleton, record_provider_success/failure, evaluate_recovery, and
failover-change logging. Pure unittest, no Django dependency — this state
store is plain in-process Python, no DB, no network.
"""

import logging
import unittest

import market_data.providers  # noqa: F401 — triggers binance/kraken/finnhub registration
from market_data.contracts import ProviderHealthState
from market_data.router.models import ReasonCode
from market_data.runtime_router.state import (
    build_plan_for_symbol,
    evaluate_recovery,
    get_circuit_breaker_state,
    get_router,
    record_provider_failure,
    record_provider_success,
    record_selection,
    reset_router_state,
)


class SingletonTests(unittest.TestCase):
    def setUp(self):
        reset_router_state()

    def test_get_router_returns_same_instance_across_calls(self):
        self.assertIs(get_router(), get_router())

    def test_reset_gives_a_fresh_instance(self):
        first = get_router()
        reset_router_state()
        self.assertIsNot(first, get_router())

    def test_reset_clears_prior_failure_state(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", error_code="TIMEOUT", now=t)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)
        reset_router_state()
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.CLOSED)


class BuildPlanForSymbolTests(unittest.TestCase):
    def test_builds_a_real_plan(self):
        plan = build_plan_for_symbol("BTCUSD")
        provider_ids = {e.provider_id for e in plan.entries}
        self.assertEqual(provider_ids, {"binance", "kraken"})


class RecordProviderSuccessFailureTests(unittest.TestCase):
    def setUp(self):
        reset_router_state()

    def test_success_keeps_circuit_closed(self):
        record_provider_success("BTCUSD", "binance", now=0)
        state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state.health, ProviderHealthState.CLOSED)
        self.assertEqual(state.consecutive_failures, 0)

    def test_three_failures_open_the_circuit(self):
        for t in (0, 1):
            record_provider_failure("BTCUSD", "binance", error_code="TIMEOUT", now=t)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.CLOSED)
        record_provider_failure("BTCUSD", "binance", error_code="TIMEOUT", now=2)
        state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state.health, ProviderHealthState.OPEN)
        self.assertEqual(state.consecutive_failures, 3)
        self.assertEqual(state.last_error_code, "TIMEOUT")

    def test_success_after_failures_resets_consecutive_count(self):
        record_provider_failure("BTCUSD", "binance", now=0)
        record_provider_failure("BTCUSD", "binance", now=1)
        record_provider_success("BTCUSD", "binance", now=2)
        state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.health, ProviderHealthState.CLOSED)

    def test_symbols_are_isolated(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)
        self.assertEqual(get_circuit_breaker_state("ETHUSD", "binance").health, ProviderHealthState.CLOSED)

    def test_providers_are_isolated_within_a_symbol(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "kraken").health, ProviderHealthState.CLOSED)

    def test_unknown_symbol_does_not_raise(self):
        record_provider_success("NOT_A_REAL_SYMBOL", "binance", now=0)  # must not raise
        record_provider_failure("NOT_A_REAL_SYMBOL", "binance", now=0)  # must not raise


class RecoveryCycleTests(unittest.TestCase):
    def setUp(self):
        reset_router_state()

    def test_cooldown_transitions_open_to_half_open(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)  # opened_at=2, crypto cooldown=10s
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.OPEN)

        state = evaluate_recovery("BTCUSD", "binance", now=12)  # 12 - 2 >= 10
        self.assertEqual(state.health, ProviderHealthState.HALF_OPEN)

    def test_before_cooldown_stays_open(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)
        state = evaluate_recovery("BTCUSD", "binance", now=5)  # 5 - 2 < 10
        self.assertEqual(state.health, ProviderHealthState.OPEN)

    def test_half_open_probe_success_then_required_successes_closes(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)
        evaluate_recovery("BTCUSD", "binance", now=12)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.HALF_OPEN)

        record_provider_success("BTCUSD", "binance", now=13)
        state_after_one = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state_after_one.health, ProviderHealthState.HALF_OPEN)  # crypto default requires 2

        record_provider_success("BTCUSD", "binance", now=14)
        state_after_two = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state_after_two.health, ProviderHealthState.CLOSED)
        self.assertEqual(state_after_two.consecutive_failures, 0)

    def test_half_open_probe_failure_reopens(self):
        for t in (0, 1, 2):
            record_provider_failure("BTCUSD", "binance", now=t)
        evaluate_recovery("BTCUSD", "binance", now=12)
        self.assertEqual(get_circuit_breaker_state("BTCUSD", "binance").health, ProviderHealthState.HALF_OPEN)

        record_provider_failure("BTCUSD", "binance", error_code="TIMEOUT", now=13)
        state = get_circuit_breaker_state("BTCUSD", "binance")
        self.assertEqual(state.health, ProviderHealthState.OPEN)
        self.assertEqual(state.opened_at, 13)


class RecordSelectionFailoverLoggingTests(unittest.TestCase):
    def setUp(self):
        reset_router_state()

    def test_first_ever_selection_does_not_log_failover(self):
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_selection("BTCUSD", "binance", ReasonCode.PRIMARY_SELECTED)
            logging.getLogger("simulator.ws").info("sentinel")  # ensure assertLogs has something to capture
        self.assertFalse(any("market_data_router_failover" in line for line in captured.output))

    def test_provider_change_logs_failover(self):
        record_selection("BTCUSD", "binance", ReasonCode.PRIMARY_SELECTED)
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_selection("BTCUSD", "kraken", ReasonCode.SECONDARY_SELECTED)
        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_router_failover", joined)
        self.assertIn("symbol=BTCUSD", joined)
        self.assertIn("from_provider=binance", joined)
        self.assertIn("to_provider=kraken", joined)
        self.assertIn("reason_code=SECONDARY_SELECTED", joined)

    def test_same_provider_again_does_not_log_failover(self):
        record_selection("BTCUSD", "binance", ReasonCode.PRIMARY_SELECTED)
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_selection("BTCUSD", "binance", ReasonCode.PRIMARY_SELECTED)
            logging.getLogger("simulator.ws").info("sentinel")
        self.assertFalse(any("market_data_router_failover" in line for line in captured.output))

    def test_symbols_tracked_independently(self):
        record_selection("BTCUSD", "binance", ReasonCode.PRIMARY_SELECTED)
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_selection("ETHUSD", "binance", ReasonCode.PRIMARY_SELECTED)  # first time for ETHUSD
            logging.getLogger("simulator.ws").info("sentinel")
        self.assertFalse(any("market_data_router_failover" in line for line in captured.output))


class StateTransitionLoggingTests(unittest.TestCase):
    def setUp(self):
        reset_router_state()

    def test_open_transition_is_logged(self):
        record_provider_failure("BTCUSD", "binance", now=0)
        record_provider_failure("BTCUSD", "binance", now=1)
        with self.assertLogs("simulator.ws", level="INFO") as captured:
            record_provider_failure("BTCUSD", "binance", error_code="TIMEOUT", now=2)
        joined = "\n".join(captured.output)
        self.assertIn("event=market_data_router_state_transition", joined)
        self.assertIn("symbol=BTCUSD", joined)
        self.assertIn("provider=binance", joined)
        self.assertIn("from_state=CLOSED", joined)
        self.assertIn("to_state=OPEN", joined)
        self.assertIn("consecutive_failures=3", joined)

    def test_no_health_change_does_not_log_a_transition(self):
        record_provider_failure("BTCUSD", "binance", now=0)  # CLOSED -> CLOSED (below threshold)
        with self.assertNoLogs("simulator.ws", level="INFO"):
            record_provider_success("BTCUSD", "binance", now=1)  # still CLOSED -> CLOSED


if __name__ == "__main__":
    unittest.main()
