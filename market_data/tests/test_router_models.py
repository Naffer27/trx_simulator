"""
market_data/tests/test_router_models.py — FOUNDATION-05.

ProviderRouteEntry / ProviderRoutePlan / ReasonCode. Pure unittest, no
Django dependency — these are plain dataclasses and an enum.
"""

import unittest

from market_data.contracts import OrderPolicy, ProviderCapability
from market_data.router.models import ProviderRouteEntry, ProviderRoutePlan, ReasonCode


def make_entry(**overrides):
    defaults = dict(
        provider_id="binance", canonical_symbol="BTCUSD", provider_symbol="BTCUSDT", priority=0,
    )
    defaults.update(overrides)
    return ProviderRouteEntry(**defaults)


def make_plan(**overrides):
    defaults = dict(
        canonical_symbol="BTCUSD",
        entries=(make_entry(),),
        simulation_allowed=True,
        default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
    )
    defaults.update(overrides)
    return ProviderRoutePlan(**defaults)


class ProviderRouteEntryTests(unittest.TestCase):
    def test_valid_construction(self):
        entry = make_entry()
        self.assertEqual(entry.provider_id, "binance")
        self.assertTrue(entry.enabled)
        self.assertEqual(entry.required_capabilities, frozenset())

    def test_empty_provider_id_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(provider_id="")

    def test_empty_canonical_symbol_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(canonical_symbol="")

    def test_empty_provider_symbol_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(provider_symbol="")

    def test_negative_priority_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(priority=-1)

    def test_invalid_capability_type_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(required_capabilities=frozenset({"BID_ASK"}))  # raw string, not enum

    def test_valid_capabilities_accepted(self):
        entry = make_entry(required_capabilities=frozenset({ProviderCapability.BID_ASK}))
        self.assertIn(ProviderCapability.BID_ASK, entry.required_capabilities)


class ProviderRoutePlanTests(unittest.TestCase):
    def test_valid_construction(self):
        plan = make_plan()
        self.assertEqual(plan.max_failures, 3)
        self.assertEqual(plan.open_cooldown_seconds, 60)
        self.assertEqual(plan.half_open_successes_required, 2)

    def test_empty_canonical_symbol_rejected(self):
        with self.assertRaises(ValueError):
            make_plan(canonical_symbol="")

    def test_entry_symbol_mismatch_rejected(self):
        wrong_entry = make_entry(canonical_symbol="ETHUSD")
        with self.assertRaises(ValueError):
            make_plan(entries=(wrong_entry,))

    def test_duplicate_priorities_rejected(self):
        entries = (
            make_entry(provider_id="binance", priority=0),
            make_entry(provider_id="kraken", priority=0),
        )
        with self.assertRaises(ValueError):
            make_plan(entries=entries)

    def test_no_simulation_no_enabled_entry_rejected(self):
        entries = (make_entry(enabled=False),)
        with self.assertRaises(ValueError):
            make_plan(entries=entries, simulation_allowed=False)

    def test_no_simulation_zero_entries_rejected(self):
        with self.assertRaises(ValueError):
            make_plan(entries=(), simulation_allowed=False)

    def test_no_simulation_with_enabled_entry_is_valid(self):
        plan = make_plan(entries=(make_entry(enabled=True),), simulation_allowed=False)
        self.assertFalse(plan.simulation_allowed)

    def test_simulation_allowed_with_zero_entries_is_valid(self):
        plan = make_plan(entries=(), simulation_allowed=True)
        self.assertEqual(plan.entries, ())

    def test_max_failures_must_be_positive(self):
        with self.assertRaises(ValueError):
            make_plan(max_failures=0)

    def test_open_cooldown_must_be_positive(self):
        with self.assertRaises(ValueError):
            make_plan(open_cooldown_seconds=0)

    def test_half_open_successes_required_must_be_positive(self):
        with self.assertRaises(ValueError):
            make_plan(half_open_successes_required=0)

    def test_default_degradation_policy_rejects_open_normal(self):
        with self.assertRaises(ValueError):
            make_plan(default_order_policy_on_degradation=OrderPolicy.OPEN_NORMAL)

    def test_default_degradation_policy_rejects_market_closed(self):
        with self.assertRaises(ValueError):
            make_plan(default_order_policy_on_degradation=OrderPolicy.MARKET_CLOSED)

    def test_default_degradation_policy_accepts_close_only(self):
        plan = make_plan(default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY)
        self.assertEqual(plan.default_order_policy_on_degradation, OrderPolicy.CLOSE_ONLY)

    def test_default_degradation_policy_accepts_halt_new_orders(self):
        plan = make_plan(default_order_policy_on_degradation=OrderPolicy.HALT_NEW_ORDERS)
        self.assertEqual(plan.default_order_policy_on_degradation, OrderPolicy.HALT_NEW_ORDERS)


class ReasonCodeTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(
            {m.value for m in ReasonCode},
            {
                "PRIMARY_SELECTED", "SECONDARY_SELECTED", "PROVIDER_DISABLED",
                "CAPABILITY_MISMATCH", "CIRCUIT_OPEN", "HALF_OPEN_PROBE",
                "SIMULATION_FALLBACK", "NO_PROVIDER_AVAILABLE", "MARKET_CLOSED",
            },
        )


if __name__ == "__main__":
    unittest.main()
