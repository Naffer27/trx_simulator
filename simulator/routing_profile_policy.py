# simulator/routing_profile_policy.py
"""
BOOK-06k.1 — Routing Profile Policy V2, pure foundation.

evaluate_routing_profile() is a pure function — no DB, no ORM, no
network, no writes, no django.conf.settings, no request/user/account
objects. Same architectural discipline as
dealing_desk.py::evaluate_dealing_desk_decision() and
liquidity_engine.py::evaluate_simulated_hedge(): the caller (a future
BOOK-06k.2, not built yet) is responsible for resolving every input as
a plain value and for deciding what to do with the result. This module
has no writer of its own.

── POLICY-NEUTRAL CORRECTION (2026-08-24) ──────────────────────────────
An earlier version of this module computed candidate_routing_profile
itself, from raw `behavioral`/`exposure` metrics, via a hardcoded
priority-ordered rule table (_classify_candidate()), and distinguished
"promotion" from "demotion" via a pivot on ROUTING_INTERNAL
(_required_streak_for_transition()). A dedicated review demonstrated
both were real economic/risk-policy decisions smuggled into a function
that was supposed to be mechanics-only: the rule table decided WHICH
behavioral+exposure signals combine into WHICH profile (a business
judgment, even with every number externalized via `thresholds`), and
the INTERNAL-pivot bucketing degenerated to "arriving at INTERNAL" vs.
"everything else" — 7 of 12 possible transitions fell into the same
fallback bucket by accident, including every transition starting from
`None` (first-ever evaluation) and every lateral move between REVIEW/
HEDGE_CANDIDATE/ELITE, none of which the codebase's actual semantics
(dealing_desk.py's own set-membership-only treatment of routing
profiles; the complete absence of any ordinal comparison anywhere in
this project outside that removed code) ever supported.

This version removes both. `candidate_routing_profile` is now a
required, externally-resolved INPUT — this module never decides why a
trader is HEDGE_CANDIDATE/REVIEW/ELITE/INTERNAL, never sees the
behavioral or exposure metrics that produced that decision, and never
assumes an order among the 4 profile strings. That resolution belongs
to a future, separate routing policy layer (not built yet). This
module is left with exactly the two things that are genuinely
mechanics, not policy: the evidence gate (is there enough data to act
at all) and the streak/hysteresis state machine (has the SAME candidate
been confirmed enough times in a row to become the new v2_routing_profile).
Every routing profile is compared only for equality, never ranked.

Deliberately does NOT import simulator.models, intelligence_engine.py,
or dealing_desk.py — same discipline routing_engine.py/liquidity_engine.py/
dealing_desk.py already follow. The 4 routing profile strings below are
plain, locally-defined constants kept in sync by convention with
TraderScore.ROUTING_CHOICES (same precedent already established by
dealing_desk.py's own DEFAULT_QUALIFYING_ROUTING_PROFILES).

── V1/V2 isolation (unchanged) ─────────────────────────────────────────
TraderScore.routing_profile (the field the LEGACY engine writes, via
intelligence_engine.py::update_intelligence()/_ROUTING_MAP) is NEVER an
input to this function and never appears anywhere in this module.

── Evidence gate + hysteresis interaction (unchanged, approved) ────────
When evidence is insufficient, candidate_routing_profile/candidate_streak
are returned UNCHANGED from current_state — never advanced, never
recomputed.
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────
# ENGINE_VERSION bumped 1 -> 2 for the policy-neutral correction above —
# same dual-versioning discipline every other engine in this project
# uses to distinguish evaluations produced by materially different
# logic. No real caller has ever consumed version 1 (see this block's
# own delivery report — zero callers existed at any point), so this is
# a traceability practice, not a compatibility requirement.
# ─────────────────────────────────────────────────────────────────────────
ENGINE_VERSION = 2

# Plain string constants — see module docstring for why these are not
# imported from TraderScore.ROUTING_CHOICES. Values matched by convention.
ROUTING_INTERNAL = "INTERNAL"
ROUTING_REVIEW = "REVIEW"
ROUTING_HEDGE_CANDIDATE = "HEDGE_CANDIDATE"
ROUTING_ELITE = "ELITE"
_VALID_PROFILES = frozenset({ROUTING_INTERNAL, ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE})

# Reason codes — the complete, closed set this function ever returns.
# Deterministic: the same (candidate_routing_profile, evidence,
# current_state, thresholds) always produces the same reason_code.
# None of these names reference promotion/demotion/direction — see
# module docstring for why.
REASON_INSUFFICIENT_EVIDENCE_TRADE_COUNT = "INSUFFICIENT_EVIDENCE_TRADE_COUNT"
REASON_INSUFFICIENT_EVIDENCE_ACCOUNT_AGE = "INSUFFICIENT_EVIDENCE_ACCOUNT_AGE"
REASON_INSUFFICIENT_EVIDENCE_WINDOW = "INSUFFICIENT_EVIDENCE_WINDOW"
REASON_STREAK_RESET = "STREAK_RESET"
REASON_STREAK_IN_PROGRESS = "STREAK_IN_PROGRESS"
REASON_STREAK_CONFIRMED_NO_CHANGE = "STREAK_CONFIRMED_NO_CHANGE"
REASON_STREAK_CONFIRMED_TRANSITION = "STREAK_CONFIRMED_TRANSITION"

_REQUIRED_EVIDENCE_KEYS = (
    "lifetime_trade_count", "window_trade_count", "window_days", "account_age_days",
)
_REQUIRED_CURRENT_STATE_KEYS = (
    "v2_routing_profile", "candidate_routing_profile", "candidate_streak",
)
# transition_streak_required — a single, policy-neutral number. No
# promotion/demotion split: the codebase's own routing profiles are
# nominal categories, not ordinal levels (see module docstring), so
# there is no "direction" to have two different requirements for.
_REQUIRED_THRESHOLD_KEYS = (
    "min_window_trade_count", "min_account_age_days", "min_window_days",
    "transition_streak_required",
)

_NUMERIC_TYPES = (int, float)
try:
    from decimal import Decimal as _Decimal
    _NUMERIC_TYPES = (int, float, _Decimal)
except ImportError:  # pragma: no cover — stdlib, always available
    pass


class RoutingProfilePolicyInputError(ValueError):
    """Raised for structurally invalid input — a programming error by
    the caller, never for a legitimate "not enough evidence yet"
    trading outcome (that is evidence_sufficient=False in the returned
    dict, not an exception). See this block's delivery report for why
    this function does not blanket try/except its own body."""


def _require_dict(name: str, value) -> dict:
    if not isinstance(value, dict):
        raise RoutingProfilePolicyInputError(f"{name} must be a dict, got {type(value).__name__}")
    return value


def _require_keys(name: str, value: dict, required_keys: tuple) -> None:
    missing = [k for k in required_keys if k not in value]
    if missing:
        raise RoutingProfilePolicyInputError(f"{name} is missing required key(s): {missing}")


def _require_numeric(name: str, value, *, allow_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, _NUMERIC_TYPES):
        raise RoutingProfilePolicyInputError(f"{name} must be numeric, got {value!r}")
    if not allow_negative and value < 0:
        raise RoutingProfilePolicyInputError(f"{name} must not be negative, got {value!r}")


def _require_int(name: str, value, *, allow_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoutingProfilePolicyInputError(f"{name} must be an int, got {value!r}")
    if not allow_negative and value < 0:
        raise RoutingProfilePolicyInputError(f"{name} must not be negative, got {value!r}")


def _require_profile(name: str, value) -> None:
    """Unlike current_state's stored profiles, the fresh
    candidate_routing_profile argument must always be one of the 4 real
    values — it represents a concrete classification a future policy
    layer has already resolved, never "nothing yet" (that is what
    current_state's own None values represent)."""
    if value not in _VALID_PROFILES:
        raise RoutingProfilePolicyInputError(
            f"{name} must be one of {sorted(_VALID_PROFILES)}, got {value!r}"
        )


def _require_profile_or_none(name: str, value) -> None:
    if value is not None and value not in _VALID_PROFILES:
        raise RoutingProfilePolicyInputError(
            f"{name} must be one of {sorted(_VALID_PROFILES)} or None, got {value!r}"
        )


def _validate_inputs(candidate_routing_profile, evidence: dict,
                      current_state: dict, thresholds: dict) -> None:
    """Structural validation only — never a judgment about whether the
    VALUES represent good or bad evidence (that is _check_evidence()'s
    job, and it never raises)."""
    _require_profile("candidate_routing_profile", candidate_routing_profile)

    _require_dict("evidence", evidence)
    _require_dict("current_state", current_state)
    _require_dict("thresholds", thresholds)

    _require_keys("evidence", evidence, _REQUIRED_EVIDENCE_KEYS)
    _require_keys("current_state", current_state, _REQUIRED_CURRENT_STATE_KEYS)
    _require_keys("thresholds", thresholds, _REQUIRED_THRESHOLD_KEYS)

    for key in ("lifetime_trade_count", "window_trade_count", "window_days", "account_age_days"):
        _require_int(f"evidence[{key!r}]", evidence[key])

    _require_profile_or_none("current_state['v2_routing_profile']", current_state["v2_routing_profile"])
    _require_profile_or_none(
        "current_state['candidate_routing_profile']", current_state["candidate_routing_profile"],
    )
    _require_int("current_state['candidate_streak']", current_state["candidate_streak"])

    for key in _REQUIRED_THRESHOLD_KEYS:
        _require_numeric(f"thresholds[{key!r}]", thresholds[key])


def _check_evidence(evidence: dict, thresholds: dict):
    """Pure comparison, evidence/thresholds only. Returns
    (sufficient: bool, reason_code: str | None). Fixed, documented
    priority order (trade count, then account age, then window) so the
    result is deterministic even when multiple gates fail at once."""
    if evidence["window_trade_count"] < thresholds["min_window_trade_count"]:
        return False, REASON_INSUFFICIENT_EVIDENCE_TRADE_COUNT
    if evidence["account_age_days"] < thresholds["min_account_age_days"]:
        return False, REASON_INSUFFICIENT_EVIDENCE_ACCOUNT_AGE
    if evidence["window_days"] < thresholds["min_window_days"]:
        return False, REASON_INSUFFICIENT_EVIDENCE_WINDOW
    return True, None


def evaluate_routing_profile(
    *,
    candidate_routing_profile: str,
    evidence: dict,
    current_state: dict,
    thresholds: dict,
) -> dict:
    """
    Pure — no DB, no ORM, no network, no writes, no logging, no
    django.conf.settings, no request/user/account objects. Deterministic.

    candidate_routing_profile: the FRESH classification for this
    evaluation, already resolved by a future, separate routing policy
    layer (not built here). This module never knows, and never asks,
    why it is INTERNAL/REVIEW/HEDGE_CANDIDATE/ELITE — no behavioral or
    exposure metric is an input to this function. Must be one of the 4
    known profile strings — never None (None is only a valid value
    inside current_state, meaning "nothing confirmed/tracked yet").

    evidence, current_state, thresholds — see the module-level
    _REQUIRED_*_KEYS tuples for the exact contract each dict must
    satisfy. Raises RoutingProfilePolicyInputError (a ValueError
    subclass) for any structural problem.

    Returns:
        {
            "candidate_routing_profile": str,
            "candidate_streak": int,
            "v2_routing_profile_proposed": str | None,
            "evidence_sufficient": bool,
            "reason_code": str,
            "engine_version": int,
        }

    Mechanics only, no economic interpretation of direction:
      A. candidate == current_state's stored candidate -> streak + 1.
      B. candidate != current_state's stored candidate -> streak = 1.
      C. candidate == current_state's v2_routing_profile -> nothing to
         transition to, regardless of streak.
      D. candidate != v2_routing_profile and streak >=
         thresholds["transition_streak_required"] -> propose candidate.
      E. otherwise -> keep v2_routing_profile unchanged.
    Every profile pair (INTERNAL<->REVIEW<->HEDGE_CANDIDATE<->ELITE, and
    None -> any of the 4 on a first-ever evaluation) goes through the
    exact same rule — no branch anywhere is keyed on a specific profile
    name.

    Evidence-insufficient contract (unchanged): candidate_routing_profile/
    candidate_streak are returned UNCHANGED from current_state and
    v2_routing_profile_proposed equals current_state["v2_routing_profile"]
    unchanged — no transition or streak progress is ever produced on
    data that has not cleared the evidence floor.
    """
    _validate_inputs(candidate_routing_profile, evidence, current_state, thresholds)

    evidence_sufficient, insufficient_reason = _check_evidence(evidence, thresholds)

    if not evidence_sufficient:
        return {
            "candidate_routing_profile": current_state["candidate_routing_profile"],
            "candidate_streak": current_state["candidate_streak"],
            "v2_routing_profile_proposed": current_state["v2_routing_profile"],
            "evidence_sufficient": False,
            "reason_code": insufficient_reason,
            "engine_version": ENGINE_VERSION,
        }

    prev_candidate = current_state["candidate_routing_profile"]
    prev_streak = current_state["candidate_streak"]
    current_v2 = current_state["v2_routing_profile"]

    if candidate_routing_profile == prev_candidate:
        new_streak = prev_streak + 1
        streak_reason = REASON_STREAK_IN_PROGRESS
    else:
        new_streak = 1
        streak_reason = REASON_STREAK_RESET

    if candidate_routing_profile == current_v2:
        v2_proposed = current_v2
        reason = REASON_STREAK_CONFIRMED_NO_CHANGE
    elif new_streak >= thresholds["transition_streak_required"]:
        v2_proposed = candidate_routing_profile
        reason = REASON_STREAK_CONFIRMED_TRANSITION
    else:
        v2_proposed = current_v2
        reason = streak_reason

    return {
        "candidate_routing_profile": candidate_routing_profile,
        "candidate_streak": new_streak,
        "v2_routing_profile_proposed": v2_proposed,
        "evidence_sufficient": True,
        "reason_code": reason,
        "engine_version": ENGINE_VERSION,
    }
