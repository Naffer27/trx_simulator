"""
BOOK-06k.1 — Routing Profile Policy V2 foundation tests.

POLICY-NEUTRAL CORRECTION (2026-08-24): this file previously tested a
version of evaluate_routing_profile() that classified
candidate_routing_profile itself from behavioral/exposure metrics via a
hardcoded rule table, and distinguished "promotion" from "demotion" via
an ROUTING_INTERNAL pivot. Both were real economic/risk-policy
decisions that did not belong in a mechanics-only foundation — see
simulator/routing_profile_policy.py's own module docstring and this
block's delivery report for the full reasoning. Every test that
exercised that removed logic (toxicity->HEDGE_CANDIDATE,
profit_factor->HEDGE_CANDIDATE, martingale/scalping->REVIEW,
win_rate/profit_factor/consistency->ELITE, promotion/demotion
thresholds, INTERNAL-as-pivot) is gone. What replaces it: symmetry
tests proving every profile pair is handled by the exact same
mechanics, keyed on nothing but equality.

Pure unit tests only — no DB, no Django TestCase machinery needed;
plain unittest.TestCase itself proves the "no DB" claim.
"""
import unittest

from simulator.routing_profile_policy import (
    ENGINE_VERSION,
    ROUTING_ELITE,
    ROUTING_HEDGE_CANDIDATE,
    ROUTING_INTERNAL,
    ROUTING_REVIEW,
    RoutingProfilePolicyInputError,
    evaluate_routing_profile,
)

ALL_PROFILES = (ROUTING_INTERNAL, ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE)


# ─────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────
def _evidence(**overrides):
    base = {
        "lifetime_trade_count": 100, "window_trade_count": 100,
        "window_days": 90, "account_age_days": 200,
    }
    base.update(overrides)
    return base


def _current_state(**overrides):
    base = {
        "v2_routing_profile": None, "candidate_routing_profile": None, "candidate_streak": 0,
    }
    base.update(overrides)
    return base


def _thresholds(**overrides):
    base = {
        "min_window_trade_count": 10, "min_account_age_days": 7, "min_window_days": 30,
        "transition_streak_required": 3,
    }
    base.update(overrides)
    return base


def _call(candidate_routing_profile=ROUTING_INTERNAL, **over):
    return evaluate_routing_profile(
        candidate_routing_profile=candidate_routing_profile,
        evidence=over.pop("evidence", _evidence()),
        current_state=over.pop("current_state", _current_state()),
        thresholds=over.pop("thresholds", _thresholds()),
    )


# ─────────────────────────────────────────────────────────────────────────
# Pureza / contrato
# ─────────────────────────────────────────────────────────────────────────
class PurityContractTests(unittest.TestCase):

    def test_same_input_same_output(self):
        self.assertEqual(_call(ROUTING_HEDGE_CANDIDATE), _call(ROUTING_HEDGE_CANDIDATE))

    def test_no_db_import_no_django_dependency(self):
        """This test module itself never configures Django settings —
        if the engine touched the ORM or the app registry, importing or
        calling it here would raise before this assertion runs."""
        self.assertIsInstance(_call(), dict)

    def test_result_never_mutates_inputs(self):
        evidence, current_state, thresholds = _evidence(), _current_state(), _thresholds()
        e_copy, cs_copy, t_copy = dict(evidence), dict(current_state), dict(thresholds)
        evaluate_routing_profile(
            candidate_routing_profile=ROUTING_REVIEW,
            evidence=evidence, current_state=current_state, thresholds=thresholds,
        )
        self.assertEqual(evidence, e_copy)
        self.assertEqual(current_state, cs_copy)
        self.assertEqual(thresholds, t_copy)

    def test_engine_no_longer_accepts_behavioral_or_exposure(self):
        """Structural proof the removed inputs are gone from the
        contract — passing them must fail as an unexpected keyword
        argument, not be silently ignored."""
        with self.assertRaises(TypeError):
            evaluate_routing_profile(
                candidate_routing_profile=ROUTING_INTERNAL,
                behavioral={}, evidence=_evidence(),
                current_state=_current_state(), thresholds=_thresholds(),
            )
        with self.assertRaises(TypeError):
            evaluate_routing_profile(
                candidate_routing_profile=ROUTING_INTERNAL,
                exposure={}, evidence=_evidence(),
                current_state=_current_state(), thresholds=_thresholds(),
            )


# ─────────────────────────────────────────────────────────────────────────
# Evidence gate — unchanged semantics, still purely mechanical
# ─────────────────────────────────────────────────────────────────────────
class EvidenceGateTests(unittest.TestCase):

    def test_sufficient_evidence_passes_gate(self):
        r = _call(evidence=_evidence(window_trade_count=50, account_age_days=100, window_days=60))
        self.assertTrue(r["evidence_sufficient"])

    def test_insufficient_trade_count(self):
        r = _call(evidence=_evidence(window_trade_count=1))
        self.assertFalse(r["evidence_sufficient"])
        self.assertEqual(r["reason_code"], "INSUFFICIENT_EVIDENCE_TRADE_COUNT")

    def test_insufficient_account_age(self):
        r = _call(evidence=_evidence(account_age_days=1))
        self.assertFalse(r["evidence_sufficient"])
        self.assertEqual(r["reason_code"], "INSUFFICIENT_EVIDENCE_ACCOUNT_AGE")

    def test_insufficient_window_days(self):
        r = _call(evidence=_evidence(window_days=1))
        self.assertFalse(r["evidence_sufficient"])
        self.assertEqual(r["reason_code"], "INSUFFICIENT_EVIDENCE_WINDOW")

    def test_boundary_exactly_at_minimum_passes(self):
        t = _thresholds(min_window_trade_count=10, min_account_age_days=7, min_window_days=30)
        r = _call(evidence=_evidence(window_trade_count=10, account_age_days=7, window_days=30), thresholds=t)
        self.assertTrue(r["evidence_sufficient"])

    def test_boundary_one_below_minimum_fails(self):
        t = _thresholds(min_window_trade_count=10)
        r = _call(evidence=_evidence(window_trade_count=9), thresholds=t)
        self.assertFalse(r["evidence_sufficient"])

    def test_multiple_gates_failing_returns_deterministic_first_reason(self):
        r = _call(evidence=_evidence(window_trade_count=1, account_age_days=1, window_days=1))
        self.assertEqual(r["reason_code"], "INSUFFICIENT_EVIDENCE_TRADE_COUNT")

    def test_insufficient_evidence_freezes_candidate_and_streak(self):
        cs = _current_state(v2_routing_profile=ROUTING_INTERNAL,
                             candidate_routing_profile=ROUTING_REVIEW, candidate_streak=2)
        r = _call(ROUTING_HEDGE_CANDIDATE, evidence=_evidence(window_trade_count=1), current_state=cs)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_REVIEW)
        self.assertEqual(r["candidate_streak"], 2)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_INTERNAL)


# ─────────────────────────────────────────────────────────────────────────
# Candidate streak — mechanics only, candidate is now a direct input
# ─────────────────────────────────────────────────────────────────────────
class CandidateStreakTests(unittest.TestCase):

    def test_initial_candidate_from_no_prior_state(self):
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=_current_state())
        self.assertEqual(r["candidate_routing_profile"], ROUTING_HEDGE_CANDIDATE)
        self.assertEqual(r["candidate_streak"], 1)

    def test_same_candidate_consecutive_increments_streak(self):
        cs = _current_state(candidate_routing_profile=ROUTING_HEDGE_CANDIDATE, candidate_streak=1)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs)
        self.assertEqual(r["candidate_streak"], 2)

    def test_different_candidate_resets_streak_to_one(self):
        cs = _current_state(candidate_routing_profile=ROUTING_HEDGE_CANDIDATE, candidate_streak=5)
        r = _call(ROUTING_INTERNAL, current_state=cs)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_INTERNAL)
        self.assertEqual(r["candidate_streak"], 1)
        self.assertEqual(r["reason_code"], "STREAK_RESET")

    def test_streak_just_below_required_does_not_confirm(self):
        t = _thresholds(transition_streak_required=5)
        cs = _current_state(v2_routing_profile=ROUTING_INTERNAL,
                             candidate_routing_profile=ROUTING_HEDGE_CANDIDATE, candidate_streak=3)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs, thresholds=t)
        self.assertEqual(r["candidate_streak"], 4)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_INTERNAL)
        self.assertEqual(r["reason_code"], "STREAK_IN_PROGRESS")

    def test_streak_exactly_at_required_confirms(self):
        t = _thresholds(transition_streak_required=5)
        cs = _current_state(v2_routing_profile=ROUTING_INTERNAL,
                             candidate_routing_profile=ROUTING_HEDGE_CANDIDATE, candidate_streak=4)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs, thresholds=t)
        self.assertEqual(r["candidate_streak"], 5)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_HEDGE_CANDIDATE)
        self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_TRANSITION")

    def test_streak_above_required_stays_confirmed(self):
        t = _thresholds(transition_streak_required=3)
        cs = _current_state(v2_routing_profile=ROUTING_HEDGE_CANDIDATE,
                             candidate_routing_profile=ROUTING_HEDGE_CANDIDATE, candidate_streak=10)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs, thresholds=t)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_HEDGE_CANDIDATE)
        self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_NO_CHANGE")


# ─────────────────────────────────────────────────────────────────────────
# Symmetry / neutrality — the core of this correction. Every pair of
# profiles (including the None -> X first-ever-evaluation case) must go
# through IDENTICAL mechanics, using only transition_streak_required.
# No branch anywhere may be keyed on a specific profile's name.
# ─────────────────────────────────────────────────────────────────────────
class SymmetryNeutralityTests(unittest.TestCase):

    ALL_TRANSITIONS = (
        (None, ROUTING_INTERNAL), (None, ROUTING_REVIEW),
        (None, ROUTING_HEDGE_CANDIDATE), (None, ROUTING_ELITE),
        (ROUTING_INTERNAL, ROUTING_REVIEW), (ROUTING_REVIEW, ROUTING_INTERNAL),
        (ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE), (ROUTING_HEDGE_CANDIDATE, ROUTING_REVIEW),
        (ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE), (ROUTING_ELITE, ROUTING_HEDGE_CANDIDATE),
        (ROUTING_INTERNAL, ROUTING_ELITE), (ROUTING_ELITE, ROUTING_INTERNAL),
    )

    def test_every_pair_requires_exactly_transition_streak_required_to_confirm(self):
        """For every (FROM, TO) pair above: with streak one below
        threshold, no transition; with streak exactly at threshold,
        transition confirmed — same number, same outcome, regardless of
        which two profiles are involved."""
        required = 4
        t = _thresholds(transition_streak_required=required)
        for current_v2, candidate in self.ALL_TRANSITIONS:
            with self.subTest(current_v2=current_v2, candidate=candidate):
                cs_below = _current_state(
                    v2_routing_profile=current_v2, candidate_routing_profile=candidate,
                    candidate_streak=required - 2,
                )
                r_below = _call(candidate, current_state=cs_below, thresholds=t)
                self.assertEqual(r_below["v2_routing_profile_proposed"], current_v2,
                                  f"{current_v2} -> {candidate} confirmed too early")

                cs_at = _current_state(
                    v2_routing_profile=current_v2, candidate_routing_profile=candidate,
                    candidate_streak=required - 1,
                )
                r_at = _call(candidate, current_state=cs_at, thresholds=t)
                self.assertEqual(r_at["v2_routing_profile_proposed"], candidate,
                                  f"{current_v2} -> {candidate} did not confirm at threshold")
                self.assertEqual(r_at["reason_code"], "STREAK_CONFIRMED_TRANSITION")

    def test_no_special_case_for_any_profile_name(self):
        """Same scenario (fresh candidate, no prior streak, threshold=1)
        run for every possible FROM/TO pair — all must confirm
        immediately and identically; nothing about a specific name
        (e.g. INTERNAL, ELITE) may change the outcome shape."""
        t = _thresholds(transition_streak_required=1)
        for current_v2, candidate in self.ALL_TRANSITIONS:
            with self.subTest(current_v2=current_v2, candidate=candidate):
                cs = _current_state(v2_routing_profile=current_v2)
                r = _call(candidate, current_state=cs, thresholds=t)
                self.assertEqual(r["v2_routing_profile_proposed"], candidate)
                self.assertEqual(r["candidate_streak"], 1)
                self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_TRANSITION")

    def test_lateral_moves_between_any_pair_use_same_mechanic_as_internal_moves(self):
        """Specifically isolates the old bug: lateral moves that never
        touch INTERNAL (e.g. REVIEW<->HEDGE_CANDIDATE,
        HEDGE_CANDIDATE<->ELITE) must behave exactly like moves that do
        touch INTERNAL, given the same streak/threshold."""
        t = _thresholds(transition_streak_required=2)
        lateral_pairs = (
            (ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE), (ROUTING_HEDGE_CANDIDATE, ROUTING_REVIEW),
            (ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE), (ROUTING_ELITE, ROUTING_HEDGE_CANDIDATE),
        )
        internal_pairs = (
            (ROUTING_INTERNAL, ROUTING_REVIEW), (ROUTING_REVIEW, ROUTING_INTERNAL),
        )
        for pairs, label in ((lateral_pairs, "lateral"), (internal_pairs, "internal")):
            for current_v2, candidate in pairs:
                with self.subTest(kind=label, current_v2=current_v2, candidate=candidate):
                    cs = _current_state(v2_routing_profile=current_v2,
                                         candidate_routing_profile=candidate, candidate_streak=1)
                    r = _call(candidate, current_state=cs, thresholds=t)
                    self.assertEqual(r["v2_routing_profile_proposed"], candidate)
                    self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_TRANSITION")

    def test_x_to_x_never_produces_a_transition_regardless_of_streak(self):
        for profile in ALL_PROFILES:
            with self.subTest(profile=profile):
                cs = _current_state(v2_routing_profile=profile,
                                     candidate_routing_profile=profile, candidate_streak=50)
                r = _call(profile, current_state=cs, thresholds=_thresholds(transition_streak_required=1))
                self.assertEqual(r["v2_routing_profile_proposed"], profile)
                self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_NO_CHANGE")


# ─────────────────────────────────────────────────────────────────────────
# V1/V2 isolation
# ─────────────────────────────────────────────────────────────────────────
class V1V2IsolationTests(unittest.TestCase):

    def test_legacy_routing_profile_field_never_referenced(self):
        cs = _current_state()
        cs["routing_profile"] = "SOMETHING_LEGACY_SET"
        r1 = _call(ROUTING_REVIEW, current_state=cs)
        cs2 = _current_state()
        cs2["routing_profile"] = "SOMETHING_ELSE_ENTIRELY"
        r2 = _call(ROUTING_REVIEW, current_state=cs2)
        self.assertEqual(r1, r2)

    def test_v2_state_independent_of_arbitrary_legacy_value(self):
        cs = _current_state(v2_routing_profile=ROUTING_REVIEW,
                             candidate_routing_profile=ROUTING_REVIEW, candidate_streak=2)
        r = _call(ROUTING_INTERNAL, current_state=cs)
        self.assertEqual(r["candidate_routing_profile"], ROUTING_INTERNAL)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_REVIEW)  # not yet confirmed


# ─────────────────────────────────────────────────────────────────────────
# First V2 state (v2_routing_profile is None)
# ─────────────────────────────────────────────────────────────────────────
class FirstV2StateTests(unittest.TestCase):

    def test_none_v2_routing_profile_requires_same_streak_as_any_other_transition(self):
        t = _thresholds(transition_streak_required=3)
        cs = _current_state(v2_routing_profile=None, candidate_routing_profile=ROUTING_HEDGE_CANDIDATE,
                             candidate_streak=2)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs, thresholds=t)
        self.assertEqual(r["candidate_streak"], 3)
        self.assertEqual(r["v2_routing_profile_proposed"], ROUTING_HEDGE_CANDIDATE)
        self.assertEqual(r["reason_code"], "STREAK_CONFIRMED_TRANSITION")

    def test_none_v2_routing_profile_below_streak_confirms_nothing(self):
        t = _thresholds(transition_streak_required=3)
        cs = _current_state(v2_routing_profile=None, candidate_routing_profile=ROUTING_HEDGE_CANDIDATE,
                             candidate_streak=1)
        r = _call(ROUTING_HEDGE_CANDIDATE, current_state=cs, thresholds=t)
        self.assertEqual(r["v2_routing_profile_proposed"], None)
        self.assertEqual(r["reason_code"], "STREAK_IN_PROGRESS")


# ─────────────────────────────────────────────────────────────────────────
# Result contract
# ─────────────────────────────────────────────────────────────────────────
class ResultContractTests(unittest.TestCase):

    EXPECTED_KEYS = {
        "candidate_routing_profile", "candidate_streak", "v2_routing_profile_proposed",
        "evidence_sufficient", "reason_code", "engine_version",
    }

    def test_all_expected_fields_always_present(self):
        for evidence in (_evidence(), _evidence(window_trade_count=0)):
            r = _call(evidence=evidence)
            self.assertEqual(set(r.keys()), self.EXPECTED_KEYS)

    def test_engine_version_matches_module_constant(self):
        self.assertEqual(_call()["engine_version"], ENGINE_VERSION)

    def test_reason_codes_are_stable_known_strings_and_direction_free(self):
        known = {
            "INSUFFICIENT_EVIDENCE_TRADE_COUNT", "INSUFFICIENT_EVIDENCE_ACCOUNT_AGE",
            "INSUFFICIENT_EVIDENCE_WINDOW", "STREAK_RESET", "STREAK_IN_PROGRESS",
            "STREAK_CONFIRMED_NO_CHANGE", "STREAK_CONFIRMED_TRANSITION",
        }
        for name in known:
            self.assertNotIn("PROMOTION", name)
            self.assertNotIn("DEMOTION", name)
        r = _call()
        self.assertIn(r["reason_code"], known)


# ─────────────────────────────────────────────────────────────────────────
# Defensive inputs
# ─────────────────────────────────────────────────────────────────────────
class DefensiveInputTests(unittest.TestCase):

    def test_invalid_candidate_string_raises(self):
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call("NOT_A_REAL_PROFILE")

    def test_none_candidate_raises(self):
        """None is valid for current_state's stored profiles (nothing
        confirmed yet) but not for the fresh candidate — a policy layer
        must always resolve a concrete classification."""
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(None)

    def test_missing_key_in_thresholds_raises(self):
        t = _thresholds()
        del t["transition_streak_required"]
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(thresholds=t)

    def test_promotion_demotion_keys_no_longer_accepted_or_required(self):
        """Extraneous legacy keys, if a caller still passes them, are
        simply ignored (only the declared required keys are ever read)
        — never required, never consulted."""
        t = _thresholds()
        t["promotion_streak_required"] = 999
        t["demotion_streak_required"] = 1
        r = _call(ROUTING_HEDGE_CANDIDATE, thresholds=t)
        self.assertIsInstance(r, dict)  # must not raise, must not behave differently

    def test_wrong_type_for_dict_param_raises(self):
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(ROUTING_INTERNAL, evidence=["not", "a", "dict"])

    def test_negative_trade_count_raises(self):
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(evidence=_evidence(window_trade_count=-1))

    def test_none_for_required_numeric_field_raises(self):
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(evidence=_evidence(account_age_days=None))

    def test_invalid_profile_string_in_current_state_raises(self):
        cs = _current_state(v2_routing_profile="NOT_A_REAL_PROFILE")
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(ROUTING_INTERNAL, current_state=cs)

    def test_candidate_streak_negative_in_current_state_raises(self):
        cs = _current_state(candidate_streak=-1)
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(ROUTING_INTERNAL, current_state=cs)

    def test_boolean_is_not_accepted_as_numeric(self):
        with self.assertRaises(RoutingProfilePolicyInputError):
            _call(evidence=_evidence(window_trade_count=True))

    def test_zero_trades_does_not_raise_and_is_deterministic(self):
        evidence = _evidence(lifetime_trade_count=0, window_trade_count=0, account_age_days=0, window_days=0)
        r1 = _call(ROUTING_INTERNAL, evidence=evidence)
        r2 = _call(ROUTING_INTERNAL, evidence=evidence)
        self.assertEqual(r1, r2)
        self.assertFalse(r1["evidence_sufficient"])


if __name__ == "__main__":
    unittest.main()
