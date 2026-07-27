# simulator/dealing_desk.py
"""
BOOK-06b — Dealing Desk Decision Engine.

evaluate_dealing_desk_decision() is a pure function — no DB, no network,
no writes, no side effects of any kind. It never touches RoutingDecision,
LiquidityDecision, DealingDeskDecision, TraderScore, Position, or any
other model — the caller (a future BOOK-06c integration, not yet built)
is responsible for resolving `routing_profile` and
`has_liquidity_decision` and passing them in as plain values. Same role
in this module as evaluate_simulated_hedge()/select_simulated_provider()
already play in liquidity_engine.py (BOOK-05b).

Output contract: a plain dict, never a model instance, never persisted
here. Whatever future writer BOOK-06c introduces copies these values
verbatim onto DealingDeskDecision's own fields — this module has no
writer of its own.

is_simulated_hedge is a risk classification, not an event — never a
real hedge executed against a real liquidity provider (see
DealingDeskDecision's own docstring and BOOK-06 FASE 0, approved
2026-07-26).

Deliberately does not import simulator.models — same discipline already
followed by routing_engine.py and liquidity_engine.py, which never
import models at module level either. `qualifying_profiles` is received
as a plain frozenset of strings, not read from TraderScore.ROUTING_CHOICES
or from django.conf.settings, so this module stays fully decoupled and
trivially testable without Django app registry or settings overrides.
The real activation-flag wiring (which profiles actually qualify in
production) is BOOK-06c/06g's responsibility, not this module's.
"""

# Same dual-versioning rationale as routing_engine.py/liquidity_engine.py:
# ENGINE_VERSION tracks the classification LOGIC (the rule in
# evaluate_dealing_desk_decision's body), SCHEMA_VERSION tracks the SHAPE
# of the returned dict — independent axes, both start at 1.
ENGINE_VERSION = 1
SCHEMA_VERSION = 1

# BOOK-06b's own initial rule engine — see DECISION_SOURCE_RULE_ENGINE_V1
# below. A future hybrid/ML/manual-override engine would introduce its
# own distinct decision_source constant, never reuse this one.
DECISION_SOURCE_RULE_ENGINE_V1 = "RULE_ENGINE_V1"

# Plain string constant, not imported from TraderScore.ROUTING_CHOICES —
# see module docstring for why this module never imports simulator.models.
# Matches TraderScore.ROUTING_HEDGE_CANDIDATE's own string value exactly;
# the two are independent by design, kept in sync by convention, same as
# routing_engine.py's own Book class is a plain string constant unrelated
# to any model field's choices list.
DEFAULT_QUALIFYING_ROUTING_PROFILES = frozenset({"HEDGE_CANDIDATE"})

REASON_QUALIFYING_PROFILE_WITH_LIQUIDITY_DECISION = "QUALIFYING_PROFILE_WITH_LIQUIDITY_DECISION"
REASON_NON_QUALIFYING_PROFILE = "NON_QUALIFYING_PROFILE"
REASON_NO_LIQUIDITY_DECISION = "NO_LIQUIDITY_DECISION"
REASON_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def evaluate_dealing_desk_decision(
    *,
    routing_profile: str,
    has_liquidity_decision: bool,
    qualifying_profiles: frozenset = DEFAULT_QUALIFYING_ROUTING_PROFILES,
) -> dict:
    """
    Pure classification — no DB, no network, no writes. Never raises:
    the entire body runs inside a single try/except, so any unexpected
    input (wrong type, unhashable value) falls through to the same safe
    default a legitimate "does not qualify" result would produce —
    is_simulated_hedge=False. "Fail-open" here means the system behaves
    as if this function did not exist at all: the default always
    preserves today's actual behavior (100% B-Book), never fails toward
    True.

    Rule (BOOK-06b, approved 2026-07-26): is_simulated_hedge is True if
    and only if `routing_profile` is a member of `qualifying_profiles`
    AND `has_liquidity_decision` is True. Requiring both — never just
    one — is deliberate: a qualifying behavioral profile with no
    LiquidityDecision means BOOK-05 found no qualifying simulated
    provider (or the flag was off) at open time, so there is no
    simulated economic basis at all to justify treating this exposure
    as anything other than B-Book.

    routing_profile/has_liquidity_decision are plain values the caller
    already resolved — this function never queries TraderScore or
    LiquidityDecision itself (see module docstring).

    Returns a plain dict:
        {
            "is_simulated_hedge": bool,
            "reason_code": str,
            "decision_source": str,
            "engine_version": int,
            "schema_version": int,
        }

    decision_source identifies which engine produced this decision —
    always DECISION_SOURCE_RULE_ENGINE_V1 for this module today. Present
    from day one so a future hybrid/ML/manual-override engine can be
    introduced without breaking this contract's shape — callers should
    branch on decision_source, never assume it is always this constant.
    """
    try:
        if not isinstance(routing_profile, str) or not routing_profile:
            return {
                "is_simulated_hedge": False,
                "reason_code": REASON_INSUFFICIENT_DATA,
                "decision_source": DECISION_SOURCE_RULE_ENGINE_V1,
                "engine_version": ENGINE_VERSION,
                "schema_version": SCHEMA_VERSION,
            }

        if routing_profile not in qualifying_profiles:
            return {
                "is_simulated_hedge": False,
                "reason_code": REASON_NON_QUALIFYING_PROFILE,
                "decision_source": DECISION_SOURCE_RULE_ENGINE_V1,
                "engine_version": ENGINE_VERSION,
                "schema_version": SCHEMA_VERSION,
            }

        if has_liquidity_decision is not True:
            return {
                "is_simulated_hedge": False,
                "reason_code": REASON_NO_LIQUIDITY_DECISION,
                "decision_source": DECISION_SOURCE_RULE_ENGINE_V1,
                "engine_version": ENGINE_VERSION,
                "schema_version": SCHEMA_VERSION,
            }

        return {
            "is_simulated_hedge": True,
            "reason_code": REASON_QUALIFYING_PROFILE_WITH_LIQUIDITY_DECISION,
            "decision_source": DECISION_SOURCE_RULE_ENGINE_V1,
            "engine_version": ENGINE_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
    except Exception:
        return {
            "is_simulated_hedge": False,
            "reason_code": REASON_INSUFFICIENT_DATA,
            "decision_source": DECISION_SOURCE_RULE_ENGINE_V1,
            "engine_version": ENGINE_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
