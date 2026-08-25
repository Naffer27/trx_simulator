"""
BOOK-06k.2 — Routing Candidate Policy foundation tests.

Every PolicySpec/PolicyRule fixture here is synthetic
(TEST_POLICY_*/TEST_RULE_*) — none encodes real Money Broker business
rules. This file tests MECHANICS only: that a declarative policy is
evaluated correctly, deterministically, and with a stable, explainable
result — never that "high toxicity means HEDGE_CANDIDATE" or any other
real economic claim.

Pure unit tests — no DB needed; plain unittest.TestCase itself proves
the "no Django dependency" claim.
"""
import unittest
from decimal import Decimal

from simulator.routing_candidate_policy import (
    ENGINE_VERSION,
    CandidatePolicyError,
    Condition,
    PolicyRule,
    PolicySpec,
    resolve_candidate_profile,
)
from simulator.routing_profile_policy import (
    ROUTING_ELITE,
    ROUTING_HEDGE_CANDIDATE,
    ROUTING_INTERNAL,
    ROUTING_REVIEW,
)


def _call(behavioral=None, exposure=None, policy_spec=None):
    return resolve_candidate_profile(
        behavioral=behavioral if behavioral is not None else {},
        exposure=exposure if exposure is not None else {},
        policy_spec=policy_spec,
    )


def _single_condition_spec(source, field, comparator, threshold_key, threshold_value,
                            output_profile=ROUTING_REVIEW, version="TEST_POLICY_A"):
    rule = PolicyRule(
        rule_id="TEST_RULE_ALPHA",
        output_profile=output_profile,
        reason_code="TEST_RULE_ALPHA_MATCHED",
        conditions=(Condition(source=source, field=field, comparator=comparator, threshold_key=threshold_key),),
    )
    return PolicySpec(version=version, rules=(rule,), thresholds={threshold_key: threshold_value})


# ─────────────────────────────────────────────────────────────────────────
# No policy
# ─────────────────────────────────────────────────────────────────────────
class NoPolicyTests(unittest.TestCase):

    def test_policy_spec_none_returns_no_policy_configured(self):
        r = _call(policy_spec=None)
        self.assertIsNone(r["candidate_routing_profile"])
        self.assertEqual(r["reason_code"], "NO_POLICY_CONFIGURED")
        self.assertIsNone(r["policy_version"])
        self.assertIsNone(r["matched_rule"])

    def test_policy_spec_with_empty_rules_returns_no_policy_configured(self):
        spec = PolicySpec(version="TEST_POLICY_EMPTY", rules=(), thresholds={})
        r = _call(policy_spec=spec)
        self.assertEqual(r["reason_code"], "NO_POLICY_CONFIGURED")
        self.assertIsNone(r["candidate_routing_profile"])


# ─────────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────────
class MatchingTests(unittest.TestCase):

    def test_single_condition_matches(self):
        spec = _single_condition_spec("behavioral", "metric_x", ">=", "thr_x", 10)
        r = _call(behavioral={"metric_x": 15}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(r["matched_rule"], "TEST_RULE_ALPHA")

    def test_multiple_and_conditions_all_must_match(self):
        rule = PolicyRule(
            rule_id="TEST_RULE_ALPHA",
            output_profile=ROUTING_HEDGE_CANDIDATE,
            reason_code="TEST_RULE_ALPHA_MATCHED",
            conditions=(
                Condition("behavioral", "metric_a", ">=", "thr_a"),
                Condition("exposure", "metric_b", ">=", "thr_b"),
            ),
        )
        spec = PolicySpec(version="TEST_POLICY_AND", rules=(rule,), thresholds={"thr_a": 5, "thr_b": 100})
        r = _call(behavioral={"metric_a": 10}, exposure={"metric_b": 200}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_HEDGE_CANDIDATE)

    def test_one_failing_and_condition_means_rule_does_not_match(self):
        rule = PolicyRule(
            rule_id="TEST_RULE_ALPHA",
            output_profile=ROUTING_HEDGE_CANDIDATE,
            reason_code="TEST_RULE_ALPHA_MATCHED",
            conditions=(
                Condition("behavioral", "metric_a", ">=", "thr_a"),
                Condition("exposure", "metric_b", ">=", "thr_b"),
            ),
        )
        spec = PolicySpec(version="TEST_POLICY_AND", rules=(rule,), thresholds={"thr_a": 5, "thr_b": 100})
        r = _call(behavioral={"metric_a": 10}, exposure={"metric_b": 1}, policy_spec=spec)
        self.assertEqual(r["reason_code"], "NO_RULE_MATCHED")
        self.assertIsNone(r["candidate_routing_profile"])

    def test_second_rule_matches_when_first_does_not(self):
        rule1 = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "TEST_RULE_ALPHA_MATCHED",
                            (Condition("behavioral", "metric_x", ">=", "thr_x"),))
        rule2 = PolicyRule("TEST_RULE_BETA", ROUTING_ELITE, "TEST_RULE_BETA_MATCHED",
                            (Condition("behavioral", "metric_y", ">=", "thr_y"),))
        spec = PolicySpec("TEST_POLICY_B", (rule1, rule2), {"thr_x": 1000, "thr_y": 1})
        r = _call(behavioral={"metric_x": 0, "metric_y": 5}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_ELITE)
        self.assertEqual(r["matched_rule"], "TEST_RULE_BETA")

    def test_first_rule_wins_over_second_when_both_match(self):
        rule1 = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "TEST_RULE_ALPHA_MATCHED",
                            (Condition("behavioral", "metric_x", ">=", "thr_x"),))
        rule2 = PolicyRule("TEST_RULE_BETA", ROUTING_ELITE, "TEST_RULE_BETA_MATCHED",
                            (Condition("behavioral", "metric_x", ">=", "thr_x"),))
        spec = PolicySpec("TEST_POLICY_PRIORITY", (rule1, rule2), {"thr_x": 1})
        r = _call(behavioral={"metric_x": 5}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(r["matched_rule"], "TEST_RULE_ALPHA")

    def test_no_rule_matches(self):
        spec = _single_condition_spec("behavioral", "metric_x", ">=", "thr_x", 1000)
        r = _call(behavioral={"metric_x": 1}, policy_spec=spec)
        self.assertEqual(r["reason_code"], "NO_RULE_MATCHED")
        self.assertIsNone(r["candidate_routing_profile"])
        self.assertIsNone(r["matched_rule"])
        self.assertEqual(r["policy_version"], "TEST_POLICY_A")  # version preserved even without a match


# ─────────────────────────────────────────────────────────────────────────
# Sources
# ─────────────────────────────────────────────────────────────────────────
class SourceTests(unittest.TestCase):

    def test_behavioral_only_condition(self):
        spec = _single_condition_spec("behavioral", "metric_x", ">=", "thr_x", 1)
        r = _call(behavioral={"metric_x": 5}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)

    def test_exposure_only_condition(self):
        spec = _single_condition_spec("exposure", "metric_x", ">=", "thr_x", 1)
        r = _call(exposure={"metric_x": 5}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)

    def test_combined_behavioral_and_exposure(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_HEDGE_CANDIDATE, "TEST_RULE_ALPHA_MATCHED",
                           (Condition("behavioral", "b", ">=", "thr_b"),
                            Condition("exposure", "e", ">=", "thr_e")))
        spec = PolicySpec("TEST_POLICY_COMBO", (rule,), {"thr_b": 1, "thr_e": 1})
        r = _call(behavioral={"b": 2}, exposure={"e": 2}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_HEDGE_CANDIDATE)


# ─────────────────────────────────────────────────────────────────────────
# Comparators (incluye valores de frontera)
# ─────────────────────────────────────────────────────────────────────────
class ComparatorTests(unittest.TestCase):

    def test_ge_boundary(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 10)
        self.assertEqual(_call({"x": 10}, policy_spec=spec)["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(_call({"x": 9}, policy_spec=spec)["candidate_routing_profile"], None)

    def test_le_boundary(self):
        spec = _single_condition_spec("behavioral", "x", "<=", "t", 10)
        self.assertEqual(_call({"x": 10}, policy_spec=spec)["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(_call({"x": 11}, policy_spec=spec)["candidate_routing_profile"], None)

    def test_gt_boundary(self):
        spec = _single_condition_spec("behavioral", "x", ">", "t", 10)
        self.assertEqual(_call({"x": 11}, policy_spec=spec)["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(_call({"x": 10}, policy_spec=spec)["candidate_routing_profile"], None)

    def test_lt_boundary(self):
        spec = _single_condition_spec("behavioral", "x", "<", "t", 10)
        self.assertEqual(_call({"x": 9}, policy_spec=spec)["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(_call({"x": 10}, policy_spec=spec)["candidate_routing_profile"], None)

    def test_eq_boundary(self):
        spec = _single_condition_spec("behavioral", "x", "==", "t", 10)
        self.assertEqual(_call({"x": 10}, policy_spec=spec)["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(_call({"x": 10.0001}, policy_spec=spec)["candidate_routing_profile"], None)


# ─────────────────────────────────────────────────────────────────────────
# Result contract
# ─────────────────────────────────────────────────────────────────────────
class ResultContractTests(unittest.TestCase):

    EXPECTED_KEYS = {"candidate_routing_profile", "reason_code", "policy_version", "matched_rule"}

    def test_result_shape_always_present(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        for behavioral in ({"x": 5}, {"x": 0}):
            r = _call(behavioral, policy_spec=spec)
            self.assertEqual(set(r.keys()), self.EXPECTED_KEYS)

    def test_policy_version_preserved_on_match(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1, version="TEST_POLICY_VERSION_7")
        r = _call({"x": 5}, policy_spec=spec)
        self.assertEqual(r["policy_version"], "TEST_POLICY_VERSION_7")

    def test_matched_rule_is_rule_id_not_index(self):
        rule1 = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", (Condition("behavioral", "x", ">=", "t"),))
        spec = PolicySpec("TEST_POLICY_C", (rule1,), {"t": 1})
        r = _call({"x": 5}, policy_spec=spec)
        self.assertEqual(r["matched_rule"], "TEST_RULE_ALPHA")
        self.assertNotEqual(r["matched_rule"], 0)


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────
class ValidationTests(unittest.TestCase):

    def test_invalid_source_raises(self):
        with self.assertRaises(CandidatePolicyError):
            Condition(source="not_a_source", field="x", comparator=">=", threshold_key="t")

    def test_invalid_comparator_raises(self):
        with self.assertRaises(CandidatePolicyError):
            Condition(source="behavioral", field="x", comparator="!=", threshold_key="t")

    def test_invalid_output_profile_raises(self):
        with self.assertRaises(CandidatePolicyError):
            PolicyRule("TEST_RULE_ALPHA", "NOT_A_REAL_PROFILE", "R1",
                       (Condition("behavioral", "x", ">=", "t"),))

    def test_empty_rule_id_raises(self):
        with self.assertRaises(CandidatePolicyError):
            PolicyRule("", ROUTING_REVIEW, "R1", ())

    def test_duplicate_rule_id_raises(self):
        rule1 = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", (Condition("behavioral", "x", ">=", "t"),))
        rule2 = PolicyRule("TEST_RULE_ALPHA", ROUTING_ELITE, "R2", (Condition("behavioral", "y", ">=", "t"),))
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_DUP", (rule1, rule2), {"t": 1})

    def test_empty_reason_code_raises(self):
        with self.assertRaises(CandidatePolicyError):
            PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "", ())

    def test_empty_threshold_key_raises(self):
        with self.assertRaises(CandidatePolicyError):
            Condition("behavioral", "x", ">=", "")

    def test_missing_threshold_key_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1",
                           (Condition("behavioral", "x", ">=", "thr_missing"),))
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_MISSING_THR", (rule,), {"other_key": 1})

    def test_invalid_version_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", ())
        with self.assertRaises(CandidatePolicyError):
            PolicySpec(version="", rules=(rule,), thresholds={})
        with self.assertRaises(CandidatePolicyError):
            PolicySpec(version=None, rules=(rule,), thresholds={})
        with self.assertRaises(CandidatePolicyError):
            PolicySpec(version=1.5, rules=(rule,), thresholds={})

    def test_empty_field_raises(self):
        with self.assertRaises(CandidatePolicyError):
            Condition("behavioral", "", ">=", "t")

    def test_field_missing_from_runtime_input_raises(self):
        spec = _single_condition_spec("behavioral", "metric_missing", ">=", "t", 1)
        with self.assertRaises(CandidatePolicyError):
            _call(behavioral={"some_other_field": 5}, policy_spec=spec)

    def test_policy_spec_wrong_type_raises(self):
        with self.assertRaises(CandidatePolicyError):
            _call(policy_spec={"not": "a PolicySpec"})

    def test_rules_as_list_instead_of_tuple_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", ())
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_LIST", [rule], {})

    def test_conditions_as_list_instead_of_tuple_raises(self):
        with self.assertRaises(CandidatePolicyError):
            PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1",
                       [Condition("behavioral", "x", ">=", "t")])

    def test_non_numeric_runtime_value_raises(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        with self.assertRaises(CandidatePolicyError):
            _call(behavioral={"x": "not-a-number"}, policy_spec=spec)

    def test_non_numeric_threshold_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1",
                           (Condition("behavioral", "x", ">=", "t"),))
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_BAD_THR", (rule,), {"t": "not-a-number"})

    def test_behavioral_wrong_type_raises(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        with self.assertRaises(CandidatePolicyError):
            resolve_candidate_profile(behavioral=["not", "a", "dict"], exposure={}, policy_spec=spec)

    def test_exposure_wrong_type_raises(self):
        spec = _single_condition_spec("exposure", "x", ">=", "t", 1)
        with self.assertRaises(CandidatePolicyError):
            resolve_candidate_profile(behavioral={}, exposure=["not", "a", "dict"], policy_spec=spec)

    def test_thresholds_wrong_type_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", ())
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_BAD_THRESHOLDS_TYPE", (rule,), ["not", "a", "dict"])

    def test_boolean_threshold_value_raises(self):
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1",
                           (Condition("behavioral", "x", ">=", "t"),))
        with self.assertRaises(CandidatePolicyError):
            PolicySpec("TEST_POLICY_BOOL_THR", (rule,), {"t": True})


# ─────────────────────────────────────────────────────────────────────────
# Immutability / purity
# ─────────────────────────────────────────────────────────────────────────
class PurityTests(unittest.TestCase):

    def test_does_not_mutate_behavioral_or_exposure(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        behavioral, exposure = {"x": 5}, {}
        b_copy, e_copy = dict(behavioral), dict(exposure)
        resolve_candidate_profile(behavioral=behavioral, exposure=exposure, policy_spec=spec)
        self.assertEqual(behavioral, b_copy)
        self.assertEqual(exposure, e_copy)

    def test_policy_spec_is_frozen(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        with self.assertRaises(Exception):
            spec.version = "MUTATED"

    def test_condition_and_rule_are_frozen(self):
        cond = Condition("behavioral", "x", ">=", "t")
        with self.assertRaises(Exception):
            cond.field = "mutated"
        rule = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", (cond,))
        with self.assertRaises(Exception):
            rule.reason_code = "mutated"

    def test_same_input_same_output(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        self.assertEqual(_call({"x": 5}, policy_spec=spec), _call({"x": 5}, policy_spec=spec))

    def test_no_db_import_no_django_dependency(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        self.assertIsInstance(_call({"x": 5}, policy_spec=spec), dict)


# ─────────────────────────────────────────────────────────────────────────
# OR declarativo (dos reglas distintas, mismo output)
# ─────────────────────────────────────────────────────────────────────────
class OrRepresentationTests(unittest.TestCase):

    def test_two_rules_same_output_act_as_or(self):
        rule1 = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1", (Condition("behavioral", "a", ">=", "thr_a"),))
        rule2 = PolicyRule("TEST_RULE_BETA", ROUTING_REVIEW, "R2", (Condition("behavioral", "b", ">=", "thr_b"),))
        spec = PolicySpec("TEST_POLICY_OR", (rule1, rule2), {"thr_a": 100, "thr_b": 100})

        r_a_only = _call({"a": 200, "b": 0}, policy_spec=spec)
        self.assertEqual(r_a_only["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(r_a_only["matched_rule"], "TEST_RULE_ALPHA")

        r_b_only = _call({"a": 0, "b": 200}, policy_spec=spec)
        self.assertEqual(r_b_only["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(r_b_only["matched_rule"], "TEST_RULE_BETA")

        r_neither = _call({"a": 0, "b": 0}, policy_spec=spec)
        self.assertIsNone(r_neither["candidate_routing_profile"])


# ─────────────────────────────────────────────────────────────────────────
# Subset — policy only touches some fields
# ─────────────────────────────────────────────────────────────────────────
class SubsetTests(unittest.TestCase):

    def test_policy_using_one_field_works_with_extra_unrelated_fields_present(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        r = _call(behavioral={"x": 5, "unrelated_field_1": 0, "unrelated_field_2": "whatever"}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)


# ─────────────────────────────────────────────────────────────────────────
# Decimal / numeric
# ─────────────────────────────────────────────────────────────────────────
class NumericTests(unittest.TestCase):

    def test_decimal_runtime_value_vs_int_threshold(self):
        spec = _single_condition_spec("exposure", "x", ">=", "t", 100)
        r = _call(exposure={"x": Decimal("150.00")}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)

    def test_decimal_threshold_vs_float_runtime_value(self):
        spec = _single_condition_spec("behavioral", "x", ">=", "t", Decimal("10.5"))
        r = _call(behavioral={"x": 10.6}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)
        r2 = _call(behavioral={"x": 10.4}, policy_spec=spec)
        self.assertIsNone(r2["candidate_routing_profile"])

    def test_decimal_equality_against_float_uses_str_roundtrip_not_binary_value(self):
        """Decimal('0.1') == 0.1 is False in raw Python (binary float
        imprecision) — this module must compare via Decimal(str(x)) on
        both sides so the intuitive equality holds."""
        spec = _single_condition_spec("behavioral", "x", "==", "t", Decimal("0.1"))
        r = _call(behavioral={"x": 0.1}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)

    def test_int_runtime_value_vs_decimal_threshold(self):
        spec = _single_condition_spec("behavioral", "x", "==", "t", Decimal("10"))
        r = _call(behavioral={"x": 10}, policy_spec=spec)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)

    def test_boolean_runtime_value_rejected(self):
        """bool is an int subclass in Python — must not silently pass as 0/1."""
        spec = _single_condition_spec("behavioral", "x", ">=", "t", 1)
        with self.assertRaises(CandidatePolicyError):
            _call(behavioral={"x": True}, policy_spec=spec)


# ─────────────────────────────────────────────────────────────────────────
# Stable IDs
# ─────────────────────────────────────────────────────────────────────────
class StableIdTests(unittest.TestCase):

    def test_matched_rule_stable_across_reordering_of_unrelated_rules(self):
        rule_alpha = PolicyRule("TEST_RULE_ALPHA", ROUTING_REVIEW, "R1",
                                 (Condition("behavioral", "a", ">=", "thr_a"),))
        rule_beta = PolicyRule("TEST_RULE_BETA", ROUTING_ELITE, "R2",
                                (Condition("behavioral", "b", ">=", "thr_b"),))
        spec1 = PolicySpec("TEST_POLICY_ORDER_1", (rule_alpha, rule_beta), {"thr_a": 1, "thr_b": 1})
        spec2 = PolicySpec("TEST_POLICY_ORDER_2", (rule_beta, rule_alpha), {"thr_a": 1, "thr_b": 1})

        r1 = _call({"a": 5, "b": 0}, policy_spec=spec1)
        r2 = _call({"a": 5, "b": 0}, policy_spec=spec2)
        self.assertEqual(r1["matched_rule"], "TEST_RULE_ALPHA")
        self.assertEqual(r2["matched_rule"], "TEST_RULE_ALPHA")


if __name__ == "__main__":
    unittest.main()
