"""
BOOK-06b — Dealing Desk Decision Engine tests.

Covers evaluate_dealing_desk_decision() in complete isolation — a pure
function, no DB, no network, no writes. No test here touches
consumers.py/tasks.py/broker_risk.py/broker_audit.py/models.py — no
call site exists yet (that is BOOK-06c, not started).
"""
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from simulator.dealing_desk import (
    DECISION_SOURCE_RULE_ENGINE_V1,
    DEFAULT_QUALIFYING_ROUTING_PROFILES,
    ENGINE_VERSION,
    REASON_INSUFFICIENT_DATA,
    REASON_NO_LIQUIDITY_DECISION,
    REASON_NON_QUALIFYING_PROFILE,
    REASON_QUALIFYING_PROFILE_WITH_LIQUIDITY_DECISION,
    SCHEMA_VERSION,
    evaluate_dealing_desk_decision,
)


class EvaluateDealingDeskDecisionTests(SimpleTestCase):
    """SimpleTestCase — no DB access needed at all, which is itself part
    of the contract this suite verifies."""

    # ── 1. Perfil calificante + LiquidityDecision existente ─────────────
    def test_qualifying_profile_with_liquidity_decision_returns_true(self):
        result = evaluate_dealing_desk_decision(
            routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
        )
        self.assertTrue(result["is_simulated_hedge"])
        self.assertEqual(result["reason_code"], REASON_QUALIFYING_PROFILE_WITH_LIQUIDITY_DECISION)

    # ── 2. Perfil calificante, sin LiquidityDecision ────────────────────
    def test_qualifying_profile_without_liquidity_decision_returns_false(self):
        result = evaluate_dealing_desk_decision(
            routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=False,
        )
        self.assertFalse(result["is_simulated_hedge"])
        self.assertEqual(result["reason_code"], REASON_NO_LIQUIDITY_DECISION)

    # ── 3. Perfil no calificante ─────────────────────────────────────────
    def test_non_qualifying_profiles_return_false(self):
        for profile in ("INTERNAL", "ELITE", "REVIEW"):
            with self.subTest(profile=profile):
                result = evaluate_dealing_desk_decision(
                    routing_profile=profile, has_liquidity_decision=True,
                )
                self.assertFalse(result["is_simulated_hedge"])
                self.assertEqual(result["reason_code"], REASON_NON_QUALIFYING_PROFILE)

    # ── 4. routing_profile None/vacío ───────────────────────────────────
    def test_none_or_empty_routing_profile_returns_false_without_raising(self):
        for profile in (None, ""):
            with self.subTest(profile=profile):
                result = evaluate_dealing_desk_decision(
                    routing_profile=profile, has_liquidity_decision=True,
                )
                self.assertFalse(result["is_simulated_hedge"])
                self.assertEqual(result["reason_code"], REASON_INSUFFICIENT_DATA)

    # ── 5. has_liquidity_decision de tipo incorrecto ────────────────────
    def test_has_liquidity_decision_wrong_type_returns_false_without_raising(self):
        for value in (None, "yes", 1, [], {}):
            with self.subTest(value=value):
                result = evaluate_dealing_desk_decision(
                    routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=value,
                )
                self.assertFalse(result["is_simulated_hedge"])

    # ── 6. routing_profile de tipo incorrecto ───────────────────────────
    def test_routing_profile_wrong_type_returns_false_without_raising(self):
        for value in (42, 3.14, [], {}, object()):
            with self.subTest(value=value):
                result = evaluate_dealing_desk_decision(
                    routing_profile=value, has_liquidity_decision=True,
                )
                self.assertFalse(result["is_simulated_hedge"])
                self.assertEqual(result["reason_code"], REASON_INSUFFICIENT_DATA)

    # ── 6b. Excepción genuina dentro del try — cobertura real del except ─
    def test_unexpected_exception_in_membership_check_is_contained(self):
        """qualifying_profiles not being iterable makes `in` raise
        TypeError — this must be absorbed by the bare except, not
        propagate, proving the internal try/except actually does
        something beyond the type guards above."""
        try:
            result = evaluate_dealing_desk_decision(
                routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
                qualifying_profiles=12345,
            )
        except Exception as exc:
            self.fail(f"evaluate_dealing_desk_decision() raised {exc!r} — must never raise")
        self.assertFalse(result["is_simulated_hedge"])
        self.assertEqual(result["reason_code"], REASON_INSUFFICIENT_DATA)

    # ── 7. Determinismo ──────────────────────────────────────────────────
    def test_deterministic_same_input_same_output_repeated(self):
        results = [
            evaluate_dealing_desk_decision(
                routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
            )
            for _ in range(100)
        ]
        self.assertTrue(all(r == results[0] for r in results))

    # ── 8. qualifying_profiles personalizado ────────────────────────────
    def test_custom_qualifying_profiles_changes_outcome(self):
        result_default = evaluate_dealing_desk_decision(
            routing_profile="ELITE", has_liquidity_decision=True,
        )
        self.assertFalse(result_default["is_simulated_hedge"])

        result_custom = evaluate_dealing_desk_decision(
            routing_profile="ELITE", has_liquidity_decision=True,
            qualifying_profiles=frozenset({"ELITE"}),
        )
        self.assertTrue(result_custom["is_simulated_hedge"])

    # ── 9. Forma exacta del diccionario de retorno ──────────────────────
    def test_return_dict_has_exactly_five_keys_correct_types(self):
        result = evaluate_dealing_desk_decision(
            routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
        )
        self.assertEqual(
            set(result.keys()),
            {"is_simulated_hedge", "reason_code", "decision_source", "engine_version", "schema_version"},
        )
        self.assertIsInstance(result["is_simulated_hedge"], bool)
        self.assertIsInstance(result["reason_code"], str)
        self.assertIsInstance(result["decision_source"], str)
        self.assertIsInstance(result["engine_version"], int)
        self.assertIsInstance(result["schema_version"], int)

    # ── decision_source ──────────────────────────────────────────────────
    def test_decision_source_is_rule_engine_v1_in_every_branch(self):
        scenarios = [
            dict(routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True),
            dict(routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=False),
            dict(routing_profile="INTERNAL", has_liquidity_decision=True),
            dict(routing_profile=None, has_liquidity_decision=True),
        ]
        for kwargs in scenarios:
            with self.subTest(**kwargs):
                result = evaluate_dealing_desk_decision(**kwargs)
                self.assertEqual(result["decision_source"], DECISION_SOURCE_RULE_ENGINE_V1)

    # ── 10. Versionado ───────────────────────────────────────────────────
    def test_engine_and_schema_version_are_one_and_reflected_in_result(self):
        self.assertEqual(ENGINE_VERSION, 1)
        self.assertEqual(SCHEMA_VERSION, 1)
        result = evaluate_dealing_desk_decision(
            routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
        )
        self.assertEqual(result["engine_version"], 1)
        self.assertEqual(result["schema_version"], 1)

    def test_default_qualifying_profiles_is_hedge_candidate_only(self):
        self.assertEqual(DEFAULT_QUALIFYING_ROUTING_PROFILES, frozenset({"HEDGE_CANDIDATE"}))


# ─────────────────────────────────────────────────────────────────────────
# 11. Cero queries — prueba estructural de pureza
# ─────────────────────────────────────────────────────────────────────────
class ZeroQueriesTests(TestCase):

    def test_zero_database_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            evaluate_dealing_desk_decision(
                routing_profile="HEDGE_CANDIDATE", has_liquidity_decision=True,
            )
        self.assertEqual(len(ctx.captured_queries), 0)
