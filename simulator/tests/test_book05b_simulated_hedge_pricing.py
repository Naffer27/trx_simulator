"""
BOOK-05b — Simulated Hedge Pricing tests.

Covers select_simulated_provider() and evaluate_simulated_hedge()
(simulator/liquidity_engine.py) — both pure functions, no DB writes, no
ORM queries inside either function, no integration with consumers.py.
No test here creates a LiquidityDecision, calls record_liquidity_decision
(does not exist yet — BOOK-05c), or touches routing_engine.py/
broker_ledger.py/broker_audit.py.
"""
import json
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from simulator.liquidity_engine import (
    ENGINE_VERSION,
    SCHEMA_VERSION,
    evaluate_simulated_hedge,
    select_simulated_provider,
)
from simulator.models import LiquidityProvider


def _provider(name, spread, capacity, symbols, enabled=True):
    return LiquidityProvider.objects.create(
        name=name,
        symbols_covered=symbols,
        simulated_spread_markup_pips=Decimal(str(spread)),
        max_capacity_usd=Decimal(str(capacity)),
        enabled=enabled,
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. select_simulated_provider() — pure selection
# ─────────────────────────────────────────────────────────────────────────
class SelectSimulatedProviderTests(TestCase):

    def test_single_qualifying_candidate_selected(self):
        p = _provider("LP-A", spread=2.0, capacity=100000, symbols=["EUR/USD"])
        result = select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertEqual(result, p)

    def test_disabled_provider_excluded(self):
        p = _provider("LP-B", spread=1.0, capacity=100000, symbols=["EUR/USD"], enabled=False)
        result = select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertIsNone(result)

    def test_symbol_not_covered_excluded(self):
        p = _provider("LP-C", spread=1.0, capacity=100000, symbols=["BTCUSD"])
        result = select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertIsNone(result)

    def test_exposure_exceeds_capacity_excluded(self):
        p = _provider("LP-D", spread=1.0, capacity=10000, symbols=["EUR/USD"])
        result = select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertIsNone(result)

    def test_exposure_exactly_at_capacity_included(self):
        p = _provider("LP-E", spread=1.0, capacity=Decimal("50000"), symbols=["EUR/USD"])
        result = select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertEqual(result, p)

    def test_empty_providers_returns_none(self):
        result = select_simulated_provider([], "EUR/USD", Decimal("50000"))
        self.assertIsNone(result)

    def test_no_candidates_qualify_returns_none(self):
        p1 = _provider("LP-F", spread=1.0, capacity=100000, symbols=["EUR/USD"], enabled=False)
        p2 = _provider("LP-G", spread=1.0, capacity=100000, symbols=["BTCUSD"])
        result = select_simulated_provider([p1, p2], "EUR/USD", Decimal("50000"))
        self.assertIsNone(result)

    def test_lowest_spread_wins(self):
        cheap = _provider("LP-Cheap", spread=1.0, capacity=100000, symbols=["EUR/USD"])
        expensive = _provider("LP-Expensive", spread=3.0, capacity=100000, symbols=["EUR/USD"])
        result = select_simulated_provider([expensive, cheap], "EUR/USD", Decimal("50000"))
        self.assertEqual(result, cheap)

    def test_tie_on_spread_lowest_capacity_that_still_covers_wins(self):
        small_capacity = _provider("LP-Small", spread=2.0, capacity=60000, symbols=["EUR/USD"])
        large_capacity = _provider("LP-Large", spread=2.0, capacity=200000, symbols=["EUR/USD"])
        result = select_simulated_provider([large_capacity, small_capacity], "EUR/USD", Decimal("50000"))
        self.assertEqual(result, small_capacity)

    def test_tie_on_spread_and_capacity_name_ascending_wins(self):
        z_provider = _provider("Z-Provider", spread=2.0, capacity=100000, symbols=["EUR/USD"])
        a_provider = _provider("A-Provider", spread=2.0, capacity=100000, symbols=["EUR/USD"])
        result = select_simulated_provider([z_provider, a_provider], "EUR/USD", Decimal("50000"))
        self.assertEqual(result, a_provider)

    def test_never_queries_the_database(self):
        p = _provider("LP-Query", spread=1.0, capacity=100000, symbols=["EUR/USD"])
        with CaptureQueriesContext(connection) as ctx:
            select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_does_not_mutate_providers(self):
        p = _provider("LP-Immutable", spread=2.0, capacity=100000, symbols=["EUR/USD"])
        before = (p.simulated_spread_markup_pips, p.max_capacity_usd, p.enabled, list(p.symbols_covered))
        select_simulated_provider([p], "EUR/USD", Decimal("50000"))
        after = (p.simulated_spread_markup_pips, p.max_capacity_usd, p.enabled, list(p.symbols_covered))
        self.assertEqual(before, after)


# ─────────────────────────────────────────────────────────────────────────
# 2. evaluate_simulated_hedge() — pure calculation
# ─────────────────────────────────────────────────────────────────────────
class EvaluateSimulatedHedgeTests(TestCase):

    def test_eurusd_formula_exact(self):
        """EUR/USD: contract_size=100_000, pip_size=0.0001 (market_data/symbol_specs.py).
        exposure_usd = 1.0 * 1.1000 * 100_000 = 110_000
        simulated_cost = 2.00 * 0.0001 * 1.0 * 100_000 = 20.00"""
        provider = _provider("LP-EUR", spread=2.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        self.assertEqual(result["exposure_usd"], Decimal("110000.00000"))
        self.assertEqual(result["simulated_cost"], Decimal("20.000000"))

    def test_usdjpy_formula_exact_pip_size_divergence(self):
        """USD/JPY: contract_size=100_000, pip_size=0.01 — a materially different
        pip_size from EUR/USD, proving the formula is NOT a basis-points
        shortcut (which would give a wrong answer here).
        exposure_usd = 1.0 * 155.000 * 100_000 = 15_500_000
        simulated_cost = 2.00 * 0.01 * 1.0 * 100_000 = 2000.00"""
        provider = _provider("LP-JPY", spread=2.0, capacity=20000000, symbols=["USD/JPY"])
        result = evaluate_simulated_hedge(
            symbol="USD/JPY", side="SELL", qty=Decimal("1.0"), price=Decimal("155.000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        self.assertEqual(result["exposure_usd"], Decimal("15500000.0000"))
        self.assertEqual(result["simulated_cost"], Decimal("2000.0000"))

    def test_btcusd_formula_exact_crypto_contract_size(self):
        """BTCUSD: contract_size=1.0, pip_size=1.0 — a third, distinct
        combination, proving the formula generalizes across asset classes.
        exposure_usd = 0.1 * 82000 * 1.0 = 8200
        simulated_cost = 5.00 * 1.0 * 0.1 * 1.0 = 0.50"""
        provider = _provider("LP-BTC", spread=5.0, capacity=100000, symbols=["BTCUSD"])
        result = evaluate_simulated_hedge(
            symbol="BTCUSD", side="BUY", qty=Decimal("0.1"), price=Decimal("82000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        self.assertEqual(result["exposure_usd"], Decimal("8200.00"))
        self.assertEqual(result["simulated_cost"], Decimal("0.5000"))

    def test_exposure_usd_uses_abs_regardless_of_side(self):
        provider = _provider("LP-Abs", spread=1.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="SELL", qty=Decimal("-1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="INTERNAL",
        )
        self.assertGreater(result["exposure_usd"], 0)
        self.assertGreater(result["simulated_cost"], 0)

    def test_contract_keys_and_types(self):
        provider = _provider("LP-Types", spread=2.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        expected_keys = {
            "symbol", "side", "qty", "price", "exposure_usd",
            "provider_id", "provider_name", "simulated_spread", "simulated_cost",
            "routing_profile", "engine_version", "schema_version", "inputs_snapshot",
        }
        self.assertEqual(set(result.keys()), expected_keys)

        self.assertIsInstance(result["symbol"], str)
        self.assertIsInstance(result["side"], str)
        self.assertIsInstance(result["qty"], Decimal)
        self.assertIsInstance(result["price"], Decimal)
        self.assertIsInstance(result["exposure_usd"], Decimal)
        self.assertIsInstance(result["provider_id"], int)
        self.assertIsInstance(result["provider_name"], str)
        self.assertIsInstance(result["simulated_spread"], Decimal)
        self.assertIsInstance(result["simulated_cost"], Decimal)
        self.assertIsInstance(result["routing_profile"], str)
        self.assertIsInstance(result["engine_version"], int)
        self.assertIsInstance(result["schema_version"], int)
        self.assertIsInstance(result["inputs_snapshot"], dict)

    def test_engine_and_schema_version_defaults(self):
        provider = _provider("LP-Ver", spread=1.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        self.assertEqual(result["engine_version"], ENGINE_VERSION)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(ENGINE_VERSION, 1)
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_provider_id_and_name_are_flat_primitives(self):
        provider = _provider("LP-Flat", spread=1.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        self.assertEqual(result["provider_id"], provider.id)
        self.assertEqual(result["provider_name"], "LP-Flat")

    def test_inputs_snapshot_is_json_serializable_no_decimal(self):
        provider = _provider("LP-JSON", spread=1.0, capacity=500000, symbols=["EUR/USD"])
        result = evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        try:
            serialized = json.dumps(result["inputs_snapshot"])
        except TypeError as exc:   # pragma: no cover - fails via assertion below
            self.fail(f"inputs_snapshot is not JSON-serializable: {exc!r}")
        self.assertIn("EUR/USD", serialized)
        for value in result["inputs_snapshot"].values():
            self.assertNotIsInstance(value, Decimal)

    def test_deterministic_same_inputs_same_outputs(self):
        provider = _provider("LP-Det", spread=2.0, capacity=500000, symbols=["EUR/USD"])
        kwargs = dict(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        result1 = evaluate_simulated_hedge(**kwargs)
        result2 = evaluate_simulated_hedge(**kwargs)
        self.assertEqual(result1, result2)

    def test_unknown_symbol_raises_keyerror(self):
        provider = _provider("LP-Unknown", spread=1.0, capacity=500000, symbols=["NOTREAL"])
        with self.assertRaises(KeyError):
            evaluate_simulated_hedge(
                symbol="NOTREAL", side="BUY", qty=Decimal("1.0"), price=Decimal("1.0"),
                provider=provider, routing_profile="HEDGE_CANDIDATE",
            )

    def test_never_queries_the_database(self):
        provider = _provider("LP-NoQuery", spread=1.0, capacity=500000, symbols=["EUR/USD"])
        with CaptureQueriesContext(connection) as ctx:
            evaluate_simulated_hedge(
                symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
                provider=provider, routing_profile="HEDGE_CANDIDATE",
            )
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_does_not_mutate_provider(self):
        provider = _provider("LP-Untouched", spread=2.0, capacity=500000, symbols=["EUR/USD"])
        before = (provider.simulated_spread_markup_pips, provider.max_capacity_usd, provider.name)
        evaluate_simulated_hedge(
            symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), price=Decimal("1.1000"),
            provider=provider, routing_profile="HEDGE_CANDIDATE",
        )
        after = (provider.simulated_spread_markup_pips, provider.max_capacity_usd, provider.name)
        self.assertEqual(before, after)
