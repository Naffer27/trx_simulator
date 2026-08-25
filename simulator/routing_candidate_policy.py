# simulator/routing_candidate_policy.py
"""
BOOK-06k.2 — Routing Candidate Policy, pure mechanics foundation.

resolve_candidate_profile() evaluates a declarative PolicySpec against
plain `behavioral`/`exposure` input dicts and returns which
candidate_routing_profile (if any) the policy's content produced, why,
and under which policy version. It is pure — no DB, no ORM, no
network, no django.conf.settings, no logging with side effects, no
current time, no randomness — same discipline as
routing_profile_policy.py::evaluate_routing_profile() and
dealing_desk.py::evaluate_dealing_desk_decision().

── STRICT SEPARATION: POLICY MECHANICS vs. POLICY CONTENT ─────────────
This module is POLICY MECHANICS only — the generic infrastructure to
evaluate ANY set of declarative rules. It contains ZERO real Money
Broker business rules: no toxicity/profit_factor/martingale/notional
thresholds, no opinion about what makes a trader HEDGE_CANDIDATE/
REVIEW/ELITE/INTERNAL. Every PolicySpec used anywhere in this module's
own tests is synthetic (TEST_POLICY_*/TEST_RULE_*), never real Money
Broker calibration. Real POLICY CONTENT is a deliberately separate,
future, not-yet-built concern (calibration, after BOOK-06k.6) that
would construct a real PolicySpec instance elsewhere and hand it to
this module's evaluator — this module never constructs one itself.

This corrects a documented mistake from BOOK-06k.1's first attempt
(see routing_profile_policy.py's own module docstring): externalizing
every numeric threshold is NOT sufficient for policy neutrality if the
RULE SHAPE (which signals combine, in what priority) is still fixed in
code. Here, the rule shape itself is DATA (a PolicySpec instance), not
code — the evaluator has no branch anywhere that mentions a specific
metric name or a specific routing profile's meaning.

── Design deliberately kept minimal — no rule engine ──────────────────
No parser, no DSL, no eval(), no dynamically-resolved operators, no
nested AND/OR/NOT expression trees. A PolicyRule's conditions are a
flat AND (all must hold). OR is expressed declaratively by two separate
PolicyRule entries pointing at the same output_profile — evaluated in
declared order, first full match wins (same "first match wins"
discipline intelligence_engine.py::classify_trader() already uses, but
now the rules are inspectable data, not code branches).

── Numeric strategy (Decimal/float/int) ────────────────────────────────
behavioral (from a future compute_metrics()-shaped input) is typically
float; exposure (from a future broker_exposure.py-shaped input) is
typically Decimal; policy_spec.thresholds may be authored as either.
Comparing Decimal to float directly is dangerous for equality — Python
compares against the float's EXACT binary value, so
Decimal('0.1') == 0.1 is False even though both "mean" 0.1. This module
converts every value on both sides of a comparison to Decimal via
Decimal(str(x)) before comparing — the exact same float->Decimal
conversion idiom broker_exposure.py's own _d() helper already uses
elsewhere in this project — never comparing a raw float against a raw
Decimal, and never silently truncating/rounding.

── Runtime dependency, not model dependency ────────────────────────────
Imports the 4 routing profile string constants from
routing_profile_policy.py (a pure, dependency-free module — zero Django
imports) rather than redefining them a third time (they already exist
in TraderScore.ROUTING_CHOICES and are independently redefined in
routing_profile_policy.py per that module's own documented rationale
for never importing simulator.models). This is a light,
one-directional, pure-Python-to-pure-Python dependency — never the
reverse, no circular import, and it does not make this module aware of
Django, the ORM, or BOOK-06k.1's evidence/hysteresis mechanics.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from decimal import Decimal

from .routing_profile_policy import (
    ROUTING_ELITE,
    ROUTING_HEDGE_CANDIDATE,
    ROUTING_INTERNAL,
    ROUTING_REVIEW,
)

ENGINE_VERSION = 1

_VALID_PROFILES = frozenset({ROUTING_INTERNAL, ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE})
_VALID_SOURCES = frozenset({"behavioral", "exposure"})

# Closed, static dispatch table — NOT dynamic operator resolution. Only
# these 5 exact strings are ever accepted; nothing here evaluates a
# comparator string as code.
_COMPARATOR_FUNCS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}

_NUMERIC_TYPES = (int, float, Decimal)

REASON_NO_POLICY_CONFIGURED = "NO_POLICY_CONFIGURED"
REASON_NO_RULE_MATCHED = "NO_RULE_MATCHED"


class CandidatePolicyError(ValueError):
    """Raised for structurally invalid PolicySpec/Condition/PolicyRule
    construction, or for a runtime behavioral/exposure input missing a
    field a rule actually references. Never raised for "no rule
    matched" or "no policy configured" — those are valid, expected
    results (see REASON_NO_POLICY_CONFIGURED/REASON_NO_RULE_MATCHED),
    not errors."""


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class Condition:
    """One atomic comparison: sources[source][field] <comparator> thresholds[threshold_key].

    source must be exactly "behavioral" or "exposure" — never an
    arbitrary string. comparator must be one of the 5 closed operators
    below — never eval()'d, never a lambda, never dynamically resolved
    from an open set. field/threshold_key are plain, caller-defined
    names — this module imposes no economic catalog of valid field
    names; a future real policy decides which fields it reads.
    """
    source: str
    field: str
    comparator: str
    threshold_key: str

    def __post_init__(self):
        if self.source not in _VALID_SOURCES:
            raise CandidatePolicyError(
                f"Condition.source must be one of {sorted(_VALID_SOURCES)}, got {self.source!r}"
            )
        if not isinstance(self.field, str) or not self.field:
            raise CandidatePolicyError(f"Condition.field must be a non-empty string, got {self.field!r}")
        if self.comparator not in _COMPARATOR_FUNCS:
            raise CandidatePolicyError(
                f"Condition.comparator must be one of {sorted(_COMPARATOR_FUNCS)}, got {self.comparator!r}"
            )
        if not isinstance(self.threshold_key, str) or not self.threshold_key:
            raise CandidatePolicyError(
                f"Condition.threshold_key must be a non-empty string, got {self.threshold_key!r}"
            )


@dataclass(frozen=True)
class PolicyRule:
    """A single named rule: if every condition holds (AND, no nesting),
    the rule matches and contributes output_profile/reason_code.

    rule_id is an explicit, stable identifier chosen by whoever defines
    the policy content — never a list index or an auto-generated
    "rule_0" — because it is what a future evaluation result's
    matched_rule reports, and that value must remain meaningful across
    reordering or editing of unrelated rules. Must be unique within a
    PolicySpec (enforced there, since uniqueness is a cross-rule,
    spec-level property).
    """
    rule_id: str
    output_profile: str
    reason_code: str
    conditions: tuple

    def __post_init__(self):
        if not isinstance(self.rule_id, str) or not self.rule_id:
            raise CandidatePolicyError(f"PolicyRule.rule_id must be a non-empty string, got {self.rule_id!r}")
        if self.output_profile not in _VALID_PROFILES:
            raise CandidatePolicyError(
                f"PolicyRule.output_profile must be one of {sorted(_VALID_PROFILES)}, got {self.output_profile!r}"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise CandidatePolicyError(
                f"PolicyRule.reason_code must be a non-empty string, got {self.reason_code!r}"
            )
        if not isinstance(self.conditions, tuple) or not all(isinstance(c, Condition) for c in self.conditions):
            raise CandidatePolicyError(
                f"PolicyRule.conditions must be a tuple of Condition, got {self.conditions!r}"
            )


@dataclass(frozen=True)
class PolicySpec:
    """version identifies WHICH content is active — independent of
    ENGINE_VERSION above (which tracks this module's own mechanics).
    rules — declared order IS priority; the first fully-matching rule
    wins, never reordered internally. thresholds — a flat dict; every
    threshold_key any condition in any rule references must exist here,
    checked eagerly at construction, never fabricated as 0/None at
    evaluation time.
    """
    version: object
    rules: tuple
    thresholds: dict

    def __post_init__(self):
        if isinstance(self.version, bool) or not isinstance(self.version, (str, int)):
            raise CandidatePolicyError(f"PolicySpec.version must be a non-empty str or an int, got {self.version!r}")
        if isinstance(self.version, str) and not self.version:
            raise CandidatePolicyError("PolicySpec.version must not be an empty string")

        if not isinstance(self.rules, tuple) or not all(isinstance(r, PolicyRule) for r in self.rules):
            raise CandidatePolicyError(f"PolicySpec.rules must be a tuple of PolicyRule, got {self.rules!r}")

        if not isinstance(self.thresholds, dict):
            raise CandidatePolicyError(f"PolicySpec.thresholds must be a dict, got {self.thresholds!r}")

        seen_ids = set()
        for rule in self.rules:
            if rule.rule_id in seen_ids:
                raise CandidatePolicyError(f"Duplicate PolicyRule.rule_id: {rule.rule_id!r}")
            seen_ids.add(rule.rule_id)
            for condition in rule.conditions:
                if condition.threshold_key not in self.thresholds:
                    raise CandidatePolicyError(
                        f"threshold_key {condition.threshold_key!r} (rule {rule.rule_id!r}) "
                        f"not found in PolicySpec.thresholds"
                    )
                threshold_value = self.thresholds[condition.threshold_key]
                if isinstance(threshold_value, bool) or not isinstance(threshold_value, _NUMERIC_TYPES):
                    raise CandidatePolicyError(
                        f"thresholds[{condition.threshold_key!r}] must be numeric, got {threshold_value!r}"
                    )


def _no_policy_result():
    return {
        "candidate_routing_profile": None,
        "reason_code": REASON_NO_POLICY_CONFIGURED,
        "policy_version": None,
        "matched_rule": None,
    }


def _rule_matches(rule: PolicyRule, sources: dict, thresholds: dict) -> bool:
    for condition in rule.conditions:
        source_dict = sources[condition.source]
        if condition.field not in source_dict:
            raise CandidatePolicyError(
                f"field {condition.field!r} (source={condition.source!r}) required by rule "
                f"{rule.rule_id!r} is missing from the runtime input"
            )
        value = source_dict[condition.field]
        if isinstance(value, bool) or not isinstance(value, _NUMERIC_TYPES):
            raise CandidatePolicyError(
                f"{condition.source}[{condition.field!r}] must be numeric for rule {rule.rule_id!r}, "
                f"got {value!r}"
            )
        threshold = thresholds[condition.threshold_key]
        compare = _COMPARATOR_FUNCS[condition.comparator]
        if not compare(_to_decimal(value), _to_decimal(threshold)):
            return False
    return True


def resolve_candidate_profile(*, behavioral: dict, exposure: dict, policy_spec) -> dict:
    """
    Pure — no DB, no ORM, no network, no settings, no side-effecting
    logging, no current time, no randomness. Never mutates behavioral/
    exposure/policy_spec. Deterministic: identical inputs always
    produce an identical result dict.

    behavioral, exposure: plain dicts, supplied entirely by the caller
    — this module never imports or calls compute_metrics() or
    broker_exposure_for_account(), never queries anything. No fixed key
    set is required — only the fields a given policy_spec's rules
    actually reference need to be present (see CandidatePolicyError for
    what happens when one is missing).

    policy_spec: a PolicySpec instance, or None. None (or a PolicySpec
    with zero rules) is a valid, expected state — never an error —
    meaning "no candidate policy content is configured yet" (e.g.
    during shadow mode before calibration). Result:
        {candidate_routing_profile: None, reason_code: "NO_POLICY_CONFIGURED",
         policy_version: None, matched_rule: None}

    Does NOT know about evidence sufficiency, streaks, or
    v2_routing_profile — those belong exclusively to
    routing_profile_policy.py::evaluate_routing_profile(), unchanged by
    this module. Computing a candidate here even when evidence would
    later be judged insufficient is deliberate (BOOK-06k.1's evidence
    gate already handles freezing on its own, downstream).

    Returns:
        {
            "candidate_routing_profile": str | None,
            "reason_code": str,
            "policy_version": object | None,
            "matched_rule": str | None,   # the matching rule's rule_id, never an index
        }

    Raises CandidatePolicyError for any structural problem: wrong
    container types for behavioral/exposure, a policy_spec that is
    neither None nor a PolicySpec instance, or a rule referencing a
    behavioral/exposure field that is genuinely absent from the runtime
    input. Never silently substitutes 0/None for a missing field.
    """
    if not isinstance(behavioral, dict):
        raise CandidatePolicyError(f"behavioral must be a dict, got {type(behavioral).__name__}")
    if not isinstance(exposure, dict):
        raise CandidatePolicyError(f"exposure must be a dict, got {type(exposure).__name__}")

    if policy_spec is None:
        return _no_policy_result()
    if not isinstance(policy_spec, PolicySpec):
        raise CandidatePolicyError(f"policy_spec must be a PolicySpec or None, got {policy_spec!r}")
    if not policy_spec.rules:
        return _no_policy_result()

    sources = {"behavioral": behavioral, "exposure": exposure}

    for rule in policy_spec.rules:
        if _rule_matches(rule, sources, policy_spec.thresholds):
            return {
                "candidate_routing_profile": rule.output_profile,
                "reason_code": rule.reason_code,
                "policy_version": policy_spec.version,
                "matched_rule": rule.rule_id,
            }

    return {
        "candidate_routing_profile": None,
        "reason_code": REASON_NO_RULE_MATCHED,
        "policy_version": policy_spec.version,
        "matched_rule": None,
    }
