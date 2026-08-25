# simulator/routing_policy_resolvers.py
"""
BOOK-06k.3 — Evidence + Exposure Resolvers.

Deliberately DB-level and impure — unlike routing_profile_policy.py's
evaluate_routing_profile() and routing_candidate_policy.py's
resolve_candidate_profile(), which stay 100% pure. These two resolvers
are the "glue" that gathers real values FOR those pure engines; they
are never imported by either pure engine, and neither pure engine is
imported here — this module has no opinion about evidence sufficiency,
candidate classification, or hysteresis, only about correctly reading
what already exists.

No caller wires these into consumers.py/tasks.py yet — that is
BOOK-06k.5's job. No TraderScore write happens here — that is
BOOK-06k.4's job. No migration, no settings, no models defined here.

── sample_trade_count / sample_span_days — NOT a fixed analysis window ──
compute_metrics() (intelligence_engine.py) contains two genuinely
different, non-equivalent temporal concepts: (a) a SAMPLE CAP — the
last <=100 closed trades by closed_at, no calendar bound at all — and
(b) a FIXED 7-day CALENDAR WINDOW anchored to timezone.now(), filtered
on opened_at (not closed_at), used exclusively for trade_frequency.
sample_trade_count/sample_span_days below reproduce ONLY the sample-cap
concept (a), applied to the exact same filter/order/limit criteria
compute_metrics() itself uses (closed_at__isnull=False, order by
-closed_at, [:100]) — never (b). sample_span_days is the closed_at
range WITHIN that capped sample (newest minus oldest), computed fresh
here since compute_metrics() itself never derives this quantity. A
fixed, now()-anchored analysis window (trades_in_analysis_window /
analysis_window_days) is explicitly NOT implemented in this block —
out of scope for BOOK-06k.3 (see the design lock report for this
block).

── Mapping to BOOK-06k.1's evidence contract (documentation only) ──────
routing_profile_policy.py::evaluate_routing_profile() is NOT modified
by this block and is not called from here. Its existing
_REQUIRED_EVIDENCE_KEYS ("lifetime_trade_count", "window_trade_count",
"window_days", "account_age_days") map, without any value translation,
onto this resolver's own "lifetime_trade_count"/"sample_trade_count"/
"sample_span_days"/"account_age_days" — window_trade_count means
sample_trade_count, window_days means sample_span_days. That mapping
is a documentation fact for whichever future block (BOOK-06k.5) wires
this resolver's output into evaluate_routing_profile()'s `evidence`
dict — it is not performed here.
"""
from decimal import Decimal

from django.utils import timezone

from .broker_exposure import broker_exposure_for_account, broker_exposure_snapshot


class RoutingResolverError(ValueError):
    """Raised only for a genuine structural/invariant violation this
    module can actually detect (currently: a risk_scope mismatch
    between a supplied broker_snapshot and the requested risk_scope —
    see resolve_routing_exposure()). Never raised for legitimate empty
    states (0 trades, 0 exposure, 0 broker total) — those are valid
    results, not errors. DB errors and malformed dependency return
    values are never caught here; they propagate — see module
    docstring and this block's design report §15 (fail-open belongs to
    BOOK-06k.5, not to these resolvers)."""


def resolve_routing_evidence(*, account) -> dict:
    """
    account: an already-loaded TradingAccount ORM instance (same style
    as intelligence_engine.py::compute_metrics(account)) — never an id,
    to avoid a redundant reload and to read account.created_at directly
    without an extra query.

    Exactly 2 queries: one COUNT (lifetime_trade_count) and one SELECT
    of closed_at values only, capped at 100, matching compute_metrics()'s
    own sample criteria exactly (closed_at__isnull=False, order by
    -closed_at, [:100]) — this resolver selects only the closed_at
    column (not full Trade rows, unlike compute_metrics(), which needs
    every field) since that is all sample_span_days requires.

    Never touches account.user or any other related field — no lazy
    relation traversal, no extra query beyond the two above.

    Returns:
        {
            "lifetime_trade_count": int,   # ALL closed trades ever, no cap
            "sample_trade_count":   int,   # <=100, same cap as compute_metrics()
            "sample_span_days":     int,   # closed_at range within that sample; 0 for 0 or 1 trades
            "account_age_days":     int,   # now() - account.created_at, in days
        }

    account_age_days is NEVER clamped to 0 if it comes out negative
    (e.g. a test fixture with a future created_at, or genuine clock
    skew) — that would silently hide an impossible, worth-investigating
    state. The raw value is returned as-is, same "no ocultar datos
    imposibles silenciosamente" principle already applied to
    relative_weight_pct in resolve_routing_exposure() below.
    """
    from .models import Trade

    lifetime_trade_count = Trade.objects.filter(
        account=account, closed_at__isnull=False,
    ).count()

    sample_closed_ats = list(
        Trade.objects.filter(account=account, closed_at__isnull=False)
        .order_by("-closed_at")
        .values_list("closed_at", flat=True)[:100]
    )
    sample_trade_count = len(sample_closed_ats)

    if sample_trade_count == 0:
        sample_span_days = 0
    else:
        newest_closed_at = sample_closed_ats[0]
        oldest_closed_at = sample_closed_ats[-1]
        sample_span_days = (newest_closed_at - oldest_closed_at).days

    account_age_days = (timezone.now() - account.created_at).days

    return {
        "lifetime_trade_count": lifetime_trade_count,
        "sample_trade_count": sample_trade_count,
        "sample_span_days": sample_span_days,
        "account_age_days": account_age_days,
    }


def resolve_routing_exposure(
    *,
    account_id: int,
    risk_scope: "str | None" = None,
    broker_snapshot=None,
) -> dict:
    """
    Reuses broker_exposure_for_account()/broker_exposure_snapshot() —
    the ONE aggregation formula this project already trusts
    (calculate_broker_exposure()) — never re-derives notional/margin
    with a second, parallel computation.

    account_id: an int, unlike resolve_routing_evidence()'s `account`
    object — this function delegates entirely to broker_exposure.py
    helpers that already accept ids; no other field of TradingAccount
    is needed here.

    broker_snapshot (BOOK-06k.5 decoupling point, not used yet by any
    caller): a pre-computed BrokerExposureBreakdown for the broker-wide
    side. None (default) preserves today's only possible behavior —
    compute it fresh. Passing one lets a future caller reuse a
    snapshot across many accounts/closes instead of recomputing the
    O(all open broker positions) aggregate on every single call — the
    hot path already identified in this block's own design report —
    without this function's signature or either pure engine's contract
    ever changing.

    Query contract (tested explicitly, see the test suite — an
    architectural invariant of this block, not just documentation):
        broker_snapshot=None      -> exactly 2 queries (account + broker-wide)
        broker_snapshot=<given>   -> exactly 1 query (account only)

    risk_scope invariant: if broker_snapshot is supplied, its own
    .risk_scope must equal the risk_scope requested here — mixing a
    "real"-scoped account read against an unscoped (or differently
    scoped) broker-wide total would silently produce a meaningless
    ratio. Raises RoutingResolverError, never silently proceeds with
    mismatched scopes.

    Returns (using the BrokerExposureBreakdown's own real field names —
    no invented aliases):
        {
            "gross_notional":              Decimal,   # this account's own
            "net_notional":                Decimal,   # this account's own
            "margin_used":                 Decimal,   # this account's own
            "concentration_by_symbol":     dict,       # {symbol: Decimal pct}, already
                                                         # scoped to this account
            "pricing_coverage_pct":        Decimal,   # this account's own — propagated
                                                         # as-is, never hidden or "fixed up"
            "broker_total_gross_notional": Decimal,
            "relative_weight_pct":         Decimal,   # see below
            "risk_scope":                  str | None,
        }

    relative_weight_pct = (gross_notional / broker_total_gross_notional) * 100,
    always Decimal, never float. If broker_total_gross_notional == 0,
    the result is Decimal("0") (the correct mathematical limit — no
    book, no relative weight — same convention broker_exposure.py's own
    concentration_pct already uses for a zero total). If the two
    underlying reads are temporarily inconsistent (no lock protects
    them — see this block's design report §13) and the account's own
    gross_notional exceeds the broker-wide total, the result CAN
    legitimately exceed 100 — this is NEVER clamped. Clamping would
    hide the one honest signal that the two reads diverged; in shadow
    mode nothing acts on this number, so preserving it as-is is strictly
    more useful for future observability than a "nicer-looking" but
    false value.

    account_id referring to a nonexistent account is NOT validated here
    (no extra existence-check query is added purely for this) —
    broker_exposure_for_account() already returns a zero-valued
    breakdown for such an id, indistinguishable from a legitimate,
    currently-empty account. This is a deliberate, documented tradeoff:
    callers are assumed to pass a valid, already-verified account_id
    (e.g. one just read from an ORM object) — see this block's design
    report §11.
    """
    if broker_snapshot is not None and broker_snapshot.risk_scope != risk_scope:
        raise RoutingResolverError(
            f"broker_snapshot.risk_scope={broker_snapshot.risk_scope!r} does not match "
            f"the requested risk_scope={risk_scope!r} — refusing to mix scopes"
        )

    account_breakdown = broker_exposure_for_account(account_id, risk_scope=risk_scope)

    broker_breakdown = (
        broker_snapshot if broker_snapshot is not None
        else broker_exposure_snapshot(risk_scope=risk_scope)
    )

    broker_total_gross_notional = broker_breakdown.gross_notional
    if broker_total_gross_notional == 0:
        relative_weight_pct = Decimal("0")
    else:
        relative_weight_pct = (
            account_breakdown.gross_notional / broker_total_gross_notional
        ) * Decimal("100")

    return {
        "gross_notional": account_breakdown.gross_notional,
        "net_notional": account_breakdown.net_notional,
        "margin_used": account_breakdown.margin_used,
        "concentration_by_symbol": account_breakdown.concentration_by_symbol,
        "pricing_coverage_pct": account_breakdown.pricing_coverage_pct,
        "broker_total_gross_notional": broker_total_gross_notional,
        "relative_weight_pct": relative_weight_pct,
        "risk_scope": risk_scope,
    }
