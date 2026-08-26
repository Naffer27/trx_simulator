# simulator/routing_profile_writer.py
"""
BOOK-06k.4 — Routing Profile V2 Writer.

apply_routing_profile_evaluation() is a persistence layer, not an
engine — it never computes evidence, exposure, candidate policy, or
hysteresis. It receives an already-loaded TraderScore instance and the
exact result dict produced by
routing_profile_policy.py::evaluate_routing_profile(), and persists
only the 6 V2 fields BOOK-06k.1 already added to TraderScore. It never
touches TraderScore.routing_profile (the legacy field), trader_class,
or any other legacy column.

This module performs ZERO reads of its own (no SELECT, no .get(), no
get_or_create(), no update_or_create()) — trader_score must already be
loaded by the caller, the same instance used to build the
current_state that fed evaluate_routing_profile() in the first place.
This is what keeps the writer's own query budget at 0 or 1 (see each
function's docstring) and is why TraderScore creation is explicitly
out of scope here — a future caller (BOOK-06k.5) relies on
intelligence_engine.py::update_intelligence() having already run
update_or_create() earlier in the same close, guaranteeing the row
exists by the time this writer runs.

── Snapshot semantics (locked design decision) ─────────────────────────
routing_profile_evidence_snapshot = the LAST V2 evaluation OBSERVED and
persisted — not "evidence of the last transition" and not "evidence of
the last state modification". It is compared and persisted
independently of candidate_routing_profile/candidate_streak/
v2_routing_profile: if the routing STATE is byte-identical to what is
already persisted but the snapshot content differs, the snapshot (and/
or engine_version) is still written on its own, in the same single
UPDATE. See this block's own design-lock reports for the full
reasoning (in particular: candidate_streak changes on almost every real
evaluation because it increments even after v2_routing_profile is
already confirmed, so "no state change" is realistically a retry/exact-
replay scenario, not the common path).

── No writer-generated timestamp in the snapshot ───────────────────────
This module deliberately never calls timezone.now() to stamp the
snapshot content itself (no "evaluated_at"/"observed_at" key is added
here) — doing so would make exact-replay idempotency structurally
impossible, since the snapshot would never compare equal to what is
already persisted. TraderScore.last_evaluated (legacy-owned, written by
update_intelligence() in the same close cycle) already carries an
approximate "when" for this evaluation without this module needing to
invent a second one.

── Null-state handling (locked design decision) ────────────────────────
evaluate_routing_profile() can legitimately return
candidate_routing_profile=None and v2_routing_profile_proposed=None —
confirmed by direct reading of its evidence-insufficient branch, which
freezes and returns current_state verbatim. This happens for any
account whose TraderScore V2 fields are still at their model defaults
(None/0/None) while evidence keeps being judged insufficient — the
single most common real-world initial state, not an edge case.
Validation here accepts None as a first-class value for both fields.
The reverse (a real profile reverting to None) is proven, by reading
evaluate_routing_profile()'s own code, to be structurally unreachable —
the `candidate_routing_profile` argument it receives is always a real,
non-None profile, so v2_routing_profile_proposed can only ever be
`current_v2` unchanged or that real profile, never None once it was
already real. No defensive code is added here for a case the pure
engine already makes impossible.
"""
from decimal import Decimal

from django.utils import timezone

from .models import TraderScore
from .routing_profile_policy import (
    ROUTING_ELITE,
    ROUTING_HEDGE_CANDIDATE,
    ROUTING_INTERNAL,
    ROUTING_REVIEW,
)

_VALID_PROFILES = frozenset({ROUTING_INTERNAL, ROUTING_REVIEW, ROUTING_HEDGE_CANDIDATE, ROUTING_ELITE})

_REQUIRED_EVALUATION_RESULT_KEYS = (
    "candidate_routing_profile", "candidate_streak", "v2_routing_profile_proposed",
    "evidence_sufficient", "reason_code", "engine_version",
)

_JSON_SCALAR_TYPES = (type(None), bool, int, str)


class RoutingProfileWriterError(ValueError):
    """Raised for structurally invalid input to this module — a
    malformed evaluation_result, an evidence_snapshot containing a type
    this writer cannot safely normalize, or a trader_score that isn't a
    real TraderScore instance. Never raised for a legitimate no-op
    write (see apply_routing_profile_evaluation()'s own "written": False
    result) and never swallowed internally — DB errors and
    RoutingProfileWriterError alike propagate to the caller. Fail-open
    for the shadow pipeline as a whole is BOOK-06k.5's responsibility,
    not this module's."""


def _validate_evaluation_result(evaluation_result) -> None:
    if not isinstance(evaluation_result, dict):
        raise RoutingProfileWriterError(
            f"evaluation_result must be a dict, got {type(evaluation_result).__name__}"
        )

    missing = [k for k in _REQUIRED_EVALUATION_RESULT_KEYS if k not in evaluation_result]
    if missing:
        raise RoutingProfileWriterError(f"evaluation_result is missing required key(s): {missing}")

    candidate = evaluation_result["candidate_routing_profile"]
    if candidate is not None and candidate not in _VALID_PROFILES:
        raise RoutingProfileWriterError(
            f"evaluation_result['candidate_routing_profile'] must be None or one of "
            f"{sorted(_VALID_PROFILES)}, got {candidate!r}"
        )

    v2_proposed = evaluation_result["v2_routing_profile_proposed"]
    if v2_proposed is not None and v2_proposed not in _VALID_PROFILES:
        raise RoutingProfileWriterError(
            f"evaluation_result['v2_routing_profile_proposed'] must be None or one of "
            f"{sorted(_VALID_PROFILES)}, got {v2_proposed!r}"
        )

    streak = evaluation_result["candidate_streak"]
    if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
        raise RoutingProfileWriterError(
            f"evaluation_result['candidate_streak'] must be a non-negative int, got {streak!r}"
        )

    engine_version = evaluation_result["engine_version"]
    if isinstance(engine_version, bool) or not isinstance(engine_version, int) or engine_version < 1:
        raise RoutingProfileWriterError(
            f"evaluation_result['engine_version'] must be an int >= 1, got {engine_version!r}"
        )

    evidence_sufficient = evaluation_result["evidence_sufficient"]
    if not isinstance(evidence_sufficient, bool):
        raise RoutingProfileWriterError(
            f"evaluation_result['evidence_sufficient'] must be a bool, got {evidence_sufficient!r}"
        )

    reason_code = evaluation_result["reason_code"]
    if not isinstance(reason_code, str) or not reason_code:
        raise RoutingProfileWriterError(
            f"evaluation_result['reason_code'] must be a non-empty string, got {reason_code!r}"
        )


def _normalize_for_json(value):
    """Recursive, non-mutating. Decimal -> str(value) (same idiom
    broker_exposure.py's own _d() helper uses, in reverse). dict/list/
    tuple are rebuilt as new containers, never mutated in place — a
    tuple always normalizes to a list (JSON has no tuple type).
    None/bool/int/str pass through unchanged. Anything else raises
    RoutingProfileWriterError explicitly — never a generic str(obj),
    which could silently leak more of an object's contents than
    intended (e.g. an ORM instance's repr) or hide a genuine caller
    bug (e.g. a stray datetime that should never have reached this
    module)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _normalize_for_json(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(v) for v in value]
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    raise RoutingProfileWriterError(
        f"unsupported type in evidence_snapshot: {type(value).__name__} ({value!r})"
    )


def apply_routing_profile_evaluation(
    *,
    trader_score,
    evaluation_result: dict,
    evidence_snapshot: dict,
) -> dict:
    """
    trader_score: an already-loaded TraderScore instance — see module
    docstring for why this function never fetches or creates one
    itself.

    evaluation_result: the exact 6-key result dict from
    evaluate_routing_profile() (BOOK-06k.1) — validated structurally
    here, never recomputed or second-guessed. candidate_routing_profile
    and v2_routing_profile_proposed may legitimately be None (see
    module docstring, null-state handling).

    evidence_snapshot: an opaque context dict (evidence + exposure +
    candidate-policy metadata, assembled by the caller) — this module
    imposes no required keys on it, only normalizes the VALUE types it
    contains for JSONField storage. Never mutated.

    Each of the 6 persisted V2 fields is compared, independently,
    against trader_score's own currently-loaded value; only fields that
    actually differ are included in a single trader_score.save(
    update_fields=[...]) call. If nothing differs, no save() is issued
    at all (0 queries, "written": False) — this is the exact-replay
    idempotency guarantee, not a general "state didn't change" fast
    path (candidate_streak changes on almost every real evaluation —
    see module docstring).

    v2_routing_profile_changed_at is set to timezone.now() if and only
    if trader_score.v2_routing_profile differs from
    v2_routing_profile_proposed — never for any other reason (not for a
    candidate/streak/snapshot/engine_version-only change). None -> None
    is not a difference (ordinary Python equality); None -> a real
    profile IS a difference and counts as a genuine first transition,
    handled by this exact same rule with no special-casing.

    Raises RoutingProfileWriterError for a malformed evaluation_result,
    an unsupported type inside evidence_snapshot, or a trader_score that
    is not a TraderScore instance. Never catches DB errors — they
    propagate to the caller (BOOK-06k.5 owns fail-open for the pipeline
    as a whole, not this function).

    Returns:
        {
            "written": bool,
            "candidate_changed": bool,       # candidate_routing_profile or candidate_streak differed
            "profile_changed": bool,         # v2_routing_profile differed
            "snapshot_changed": bool,
            "engine_version_changed": bool,
            "fields_written": tuple,         # exactly the field names passed to update_fields, () if none
        }
    """
    if not isinstance(trader_score, TraderScore):
        raise RoutingProfileWriterError(
            f"trader_score must be a TraderScore instance, got {type(trader_score).__name__}"
        )

    _validate_evaluation_result(evaluation_result)

    candidate = evaluation_result["candidate_routing_profile"]
    streak = evaluation_result["candidate_streak"]
    v2_proposed = evaluation_result["v2_routing_profile_proposed"]
    engine_version = evaluation_result["engine_version"]
    normalized_snapshot = _normalize_for_json(evidence_snapshot)

    candidate_profile_diff = trader_score.candidate_routing_profile != candidate
    candidate_streak_diff = trader_score.candidate_streak != streak
    profile_diff = trader_score.v2_routing_profile != v2_proposed
    snapshot_diff = trader_score.routing_profile_evidence_snapshot != normalized_snapshot
    engine_version_diff = trader_score.routing_profile_engine_version != engine_version

    update_fields = []

    if candidate_profile_diff:
        trader_score.candidate_routing_profile = candidate
        update_fields.append("candidate_routing_profile")
    if candidate_streak_diff:
        trader_score.candidate_streak = streak
        update_fields.append("candidate_streak")
    if profile_diff:
        trader_score.v2_routing_profile = v2_proposed
        trader_score.v2_routing_profile_changed_at = timezone.now()
        update_fields.append("v2_routing_profile")
        update_fields.append("v2_routing_profile_changed_at")
    if snapshot_diff:
        trader_score.routing_profile_evidence_snapshot = normalized_snapshot
        update_fields.append("routing_profile_evidence_snapshot")
    if engine_version_diff:
        trader_score.routing_profile_engine_version = engine_version
        update_fields.append("routing_profile_engine_version")

    if not update_fields:
        return {
            "written": False,
            "candidate_changed": False,
            "profile_changed": False,
            "snapshot_changed": False,
            "engine_version_changed": False,
            "fields_written": (),
        }

    trader_score.save(update_fields=update_fields)

    return {
        "written": True,
        "candidate_changed": candidate_profile_diff or candidate_streak_diff,
        "profile_changed": profile_diff,
        "snapshot_changed": snapshot_diff,
        "engine_version_changed": engine_version_diff,
        "fields_written": tuple(update_fields),
    }
