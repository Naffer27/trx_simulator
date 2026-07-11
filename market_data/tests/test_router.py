"""
market_data/tests/test_router.py — FOUNDATION-05.

ProviderRouter.decide() end-to-end scenarios, using the real binance/kraken/
finnhub capability profiles registered in FOUNDATION-04. Every `now` is a
plain int chosen by the test — nothing here sleeps or reads a real clock,
and nothing opens a socket or touches a database.

Pure unittest, no Django dependency.
"""

import ast
import pathlib
import unittest

import market_data.providers  # noqa: F401 — triggers binance/kraken/finnhub registration
from market_data.contracts import OrderPolicy, ProviderCapability, ProviderHealthState, SourceState
from market_data.router.models import ProviderRouteEntry, ProviderRoutePlan, ReasonCode
from market_data.router.router import ProviderRouter


def make_entry(**overrides):
    defaults = dict(
        provider_id="binance", canonical_symbol="BTCUSD", provider_symbol="BTCUSDT", priority=0,
        required_capabilities=frozenset({ProviderCapability.BID_ASK}),
    )
    defaults.update(overrides)
    return ProviderRouteEntry(**defaults)


def make_plan(**overrides):
    defaults = dict(
        canonical_symbol="BTCUSD",
        entries=(
            make_entry(provider_id="binance", priority=0),
            make_entry(provider_id="kraken", canonical_symbol="BTCUSD", provider_symbol="XBT/USD", priority=1),
        ),
        simulation_allowed=True,
        default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
        max_failures=3,
        open_cooldown_seconds=60,
        half_open_successes_required=2,
    )
    defaults.update(overrides)
    return ProviderRoutePlan(**defaults)


class PrimarySelectionTests(unittest.TestCase):
    def test_healthy_primary_selected(self):
        router = ProviderRouter()
        plan = make_plan()
        decision = router.decide(plan, now=0)
        self.assertEqual(decision.selected_provider_id, "binance")
        self.assertEqual(decision.selected_provider_symbol, "BTCUSDT")
        self.assertEqual(decision.reason_code, ReasonCode.PRIMARY_SELECTED)
        self.assertEqual(decision.source_state, SourceState.LIVE)
        self.assertEqual(decision.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertFalse(decision.degraded)
        self.assertEqual(decision.tried_providers, ("binance",))


class FailoverTests(unittest.TestCase):
    def test_primary_open_circuit_falls_to_secondary(self):
        router = ProviderRouter()
        plan = make_plan(open_cooldown_seconds=60)
        for t in (0, 1, 2):
            router.provider_failure("binance", plan, now=t)
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.OPEN)

        decision = router.decide(plan, now=10)  # well inside the 60s cooldown
        self.assertEqual(decision.selected_provider_id, "kraken")
        self.assertEqual(decision.reason_code, ReasonCode.SECONDARY_SELECTED)
        self.assertEqual(decision.source_state, SourceState.SECONDARY)
        self.assertEqual(decision.order_policy, OrderPolicy.OPEN_NORMAL)
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.tried_providers, ("binance", "kraken"))

    def test_circuit_opens_after_max_failures(self):
        router = ProviderRouter()
        plan = make_plan(max_failures=3)
        for t in (0, 1):
            router.provider_failure("binance", plan, now=t)
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.CLOSED)
        router.provider_failure("binance", plan, now=2)
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.OPEN)


class SymbolIsolationTests(unittest.TestCase):
    def test_btcusd_failure_does_not_affect_ethusd(self):
        router = ProviderRouter()
        btc_plan = make_plan(canonical_symbol="BTCUSD")
        eth_plan = make_plan(
            canonical_symbol="ETHUSD",
            entries=(
                make_entry(provider_id="binance", canonical_symbol="ETHUSD", provider_symbol="ETHUSDT", priority=0),
                make_entry(provider_id="kraken", canonical_symbol="ETHUSD", provider_symbol="ETH/USD", priority=1),
            ),
        )
        for t in (0, 1, 2):
            router.provider_failure("binance", btc_plan, now=t)

        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.OPEN)
        self.assertEqual(router.get_breaker_state("binance", "ETHUSD").health, ProviderHealthState.CLOSED)

        eth_decision = router.decide(eth_plan, now=10)
        self.assertEqual(eth_decision.selected_provider_id, "binance")
        self.assertEqual(eth_decision.reason_code, ReasonCode.PRIMARY_SELECTED)


class RecoveryCycleTests(unittest.TestCase):
    def _single_entry_plan(self, **overrides):
        defaults = dict(
            canonical_symbol="BTCUSD",
            entries=(make_entry(provider_id="binance", priority=0),),
            simulation_allowed=True,
            default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
            max_failures=3, open_cooldown_seconds=60, half_open_successes_required=2,
        )
        defaults.update(overrides)
        return ProviderRoutePlan(**defaults)

    def test_cooldown_transitions_to_half_open_probe(self):
        router = ProviderRouter()
        plan = self._single_entry_plan()
        for t in (0, 1, 2):
            router.provider_failure("binance", plan, now=t)  # opened_at=2

        decision = router.decide(plan, now=62)  # 62 - 2 >= 60
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.HALF_OPEN)
        self.assertEqual(decision.selected_provider_id, "binance")
        self.assertEqual(decision.reason_code, ReasonCode.HALF_OPEN_PROBE)
        self.assertEqual(decision.source_state, SourceState.RECOVERY)
        self.assertEqual(decision.order_policy, OrderPolicy.CLOSE_ONLY)
        self.assertTrue(decision.degraded)

    def test_successful_recovery_closes_breaker(self):
        router = ProviderRouter()
        plan = self._single_entry_plan(half_open_successes_required=2)
        for t in (0, 1, 2):
            router.provider_failure("binance", plan, now=t)
        router.decide(plan, now=62)  # triggers OPEN -> HALF_OPEN internally
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.HALF_OPEN)

        router.provider_success("binance", plan, now=63)
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.HALF_OPEN)
        router.provider_success("binance", plan, now=64)
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.CLOSED)

        decision = router.decide(plan, now=65)
        self.assertEqual(decision.reason_code, ReasonCode.PRIMARY_SELECTED)
        self.assertEqual(decision.source_state, SourceState.LIVE)
        self.assertFalse(decision.degraded)

    def test_half_open_failure_reopens_and_falls_back_to_simulation(self):
        router = ProviderRouter()
        plan = self._single_entry_plan()
        for t in (0, 1, 2):
            router.provider_failure("binance", plan, now=t)
        router.decide(plan, now=62)  # -> HALF_OPEN
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.HALF_OPEN)

        router.provider_failure("binance", plan, now=63, error_code="TIMEOUT")
        self.assertEqual(router.get_breaker_state("binance", "BTCUSD").health, ProviderHealthState.OPEN)

        decision = router.decide(plan, now=63)  # inside the fresh cooldown
        self.assertEqual(decision.reason_code, ReasonCode.SIMULATION_FALLBACK)
        self.assertEqual(decision.source_state, SourceState.SIMULATION)
        self.assertIsNone(decision.selected_provider_id)


class CapabilityAndDisabledTests(unittest.TestCase):
    def test_capability_mismatch_halts_when_no_simulation(self):
        router = ProviderRouter()
        plan = ProviderRoutePlan(
            canonical_symbol="EUR/USD",
            entries=(
                ProviderRouteEntry(
                    provider_id="finnhub", canonical_symbol="EUR/USD", provider_symbol="FX:EURUSD",
                    priority=0, required_capabilities=frozenset({ProviderCapability.BID_ASK}),
                ),
            ),
            simulation_allowed=False,
            default_order_policy_on_degradation=OrderPolicy.HALT_NEW_ORDERS,
        )
        decision = router.decide(plan, now=0)
        self.assertIsNone(decision.selected_provider_id)
        self.assertEqual(decision.reason_code, ReasonCode.CAPABILITY_MISMATCH)
        self.assertEqual(decision.order_policy, OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(decision.source_state, SourceState.STALE)
        self.assertTrue(decision.degraded)

    def test_disabled_primary_is_skipped_in_favor_of_secondary(self):
        router = ProviderRouter()
        plan = make_plan(
            entries=(
                make_entry(provider_id="binance", priority=0, enabled=False),
                make_entry(provider_id="kraken", canonical_symbol="BTCUSD", provider_symbol="XBT/USD", priority=1),
            ),
            simulation_allowed=False,
        )
        decision = router.decide(plan, now=0)
        self.assertEqual(decision.selected_provider_id, "kraken")
        self.assertEqual(decision.reason_code, ReasonCode.SECONDARY_SELECTED)
        self.assertIn("binance", decision.tried_providers)
        self.assertEqual(decision.tried_providers, ("binance", "kraken"))


class SimulationFallbackTests(unittest.TestCase):
    def test_simulation_fallback_when_all_providers_unavailable(self):
        router = ProviderRouter()
        plan = make_plan(entries=(), simulation_allowed=True, default_order_policy_on_degradation=OrderPolicy.HALT_NEW_ORDERS)
        decision = router.decide(plan, now=0)
        self.assertIsNone(decision.selected_provider_id)
        self.assertEqual(decision.reason_code, ReasonCode.SIMULATION_FALLBACK)
        self.assertEqual(decision.source_state, SourceState.SIMULATION)
        self.assertEqual(decision.order_policy, OrderPolicy.HALT_NEW_ORDERS)

    def test_simulation_order_policy_is_never_open_normal(self):
        for policy in (OrderPolicy.CLOSE_ONLY, OrderPolicy.HALT_NEW_ORDERS):
            with self.subTest(policy=policy):
                router = ProviderRouter()
                plan = make_plan(entries=(), simulation_allowed=True, default_order_policy_on_degradation=policy)
                decision = router.decide(plan, now=0)
                self.assertNotEqual(decision.order_policy, OrderPolicy.OPEN_NORMAL)
                self.assertEqual(decision.order_policy, policy)

    def test_no_provider_and_no_simulation_halts_new_orders(self):
        router = ProviderRouter()
        plan = ProviderRoutePlan(
            canonical_symbol="BTCUSD",
            entries=(
                make_entry(
                    provider_id="kraken", provider_symbol="XBT/USD", priority=0,
                    required_capabilities=frozenset({ProviderCapability.MARKET_DEPTH}),  # kraken doesn't have this
                ),
                make_entry(provider_id="binance", priority=1, enabled=False),
            ),
            simulation_allowed=False,
            default_order_policy_on_degradation=OrderPolicy.HALT_NEW_ORDERS,
        )
        decision = router.decide(plan, now=0)
        self.assertIsNone(decision.selected_provider_id)
        self.assertEqual(decision.order_policy, OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(decision.reason_code, ReasonCode.NO_PROVIDER_AVAILABLE)
        self.assertEqual(decision.tried_providers, ("kraken", "binance"))


class DeterminismTests(unittest.TestCase):
    def test_same_now_sequence_yields_identical_decisions(self):
        def run():
            router = ProviderRouter()
            plan = make_plan()
            router.provider_failure("binance", plan, now=0)
            router.provider_failure("binance", plan, now=1)
            router.provider_failure("binance", plan, now=2)
            return router.decide(plan, now=10)

        self.assertEqual(run(), run())


class NoNetworkOrDjangoDependencyTests(unittest.TestCase):
    """Static guarantee, mirroring FOUNDATION-04's isolation test: parse the
    source of every module in market_data/router/ and confirm it never
    imports anything that could touch the network, a database, Django
    settings, the filesystem/env, or a real clock."""

    _FORBIDDEN_MODULES = frozenset({
        "socket", "ssl", "http", "urllib", "requests", "websockets",
        "django", "asyncio", "os", "time",
    })

    def test_no_forbidden_imports_in_router_package(self):
        import market_data.router as router_pkg

        package_dir = pathlib.Path(router_pkg.__file__).parent
        source_files = sorted(package_dir.glob("*.py"))
        self.assertTrue(source_files, "expected market_data/router/*.py to exist")

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
