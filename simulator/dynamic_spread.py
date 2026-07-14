"""
simulator/dynamic_spread.py — SPREAD-05: Dynamic Spread Engine.

BrokerSpreadConfig.is_dynamic has existed since SPREAD-01's original model
but was never wired to anything (confirmed decorative in the audit that
preceded this block — see docs/DYNAMIC_SPREAD_ENGINE.md). This module is
its first real implementation, and the ONLY opt-in it needs: no second
flag was added (spread_bounds_enabled already exists for floor/ceiling and
is reused unchanged here — see below).

Design goals, in order:
  1. Deterministic. Same inputs -> same DynamicSpreadDecision, always. No
     `random`, no `uuid4`. decision_id is a hash of the inputs.
  2. Auditable. Every multiplier that was applied, and why, is a field on
     DynamicSpreadDecision — never an opaque final number.
  3. Compatible. is_dynamic=False (the default for every existing and
     newly-seeded row) produces bit-exact SPREAD-04 behavior: this module
     is not even consulted (spread_engine.compute_effective_spread_pips()
     only delegates here when is_dynamic=True AND dynamic_inputs is given).
     is_dynamic=True with every multiplier at its neutral value (session
     OPEN, source LIVE, not stale, no volatility/liquidity input, no
     manual override) also produces the identical number, since multiplying
     by 1.0 six times is exact in IEEE 754.
  4. Zero DB per tick, zero network, zero randomness. Every input this
     module reads is either a cached BrokerSpreadConfig snapshot field
     (simulator.spread_config_cache — an in-memory dict), a pure
     declarative session evaluation (market_data.sessions — no clock read
     of its own, no network, no vendor calendar), or an in-memory
     observability read (market_data.observability — a plain dict, no DB).

Two-stage pipeline, mirroring every prior SPREAD block's split:
  - build_dynamic_inputs(symbol, profile, ts) — assembles ONE frozen
    DynamicSpreadInputs per tick, called ONCE by consumers.py::price_tick().
    Reads the cache + session + observability (all pure/DB-free). `ts` is
    the tick's own timestamp (from the feed event), used as evaluated_at —
    not a fresh wall-clock read — so the same tick always builds the same
    inputs, whether evaluated at tick time or reconstructed later for audit
    (see pricing_context.py's SPREAD-02 invariant: never re-read mutable
    state at capture time).
  - evaluate_dynamic_spread(inputs) — pure. No I/O of any kind. Called
    independently by both spread_engine.broker_price() (at tick time, to
    price the fill) and simulator.consumers.tick_pricing_snapshot() (at
    the same tick, to freeze the audit record) — since it is a pure
    function of an already-frozen input object, both calls are guaranteed
    bit-identical, with no need to thread a single computed decision
    through both call sites.

Formula (SPREAD-05 §5):
    base = base_spread_pips + account_markup_pips        (unchanged from SPREAD-04)
    dynamic = base × session × source × stale × volatility × liquidity × manual
    effective_before_bounds = dynamic
    effective_after_bounds  = clamp(dynamic, min_spread_pips, max_spread_pips)
        — bounds applied only when spread_bounds_enabled produced a
          non-None min/max on the *symbol's own* BrokerSpreadConfig, or an
          account/product-level override was set (see
          commercial_pricing.build_commercial_pricing_profile() — the same
          resolution SPREAD-04 already established; unchanged here).

Session/source policy tables and the volatility/liquidity formulas are
documented in full in docs/DYNAMIC_SPREAD_ENGINE.md — summarized in the
constants below.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("simulator.spread")

SCHEMA_VERSION = 1

# ── Session multipliers (SPREAD-05 §4) ──────────────────────────────────
# OPEN is the only state priced at parity. PRE_MARKET/AFTER_HOURS are
# thinner-liquidity windows (mirrors F11's own OrderPolicy.CLOSE_ONLY
# treatment for the same states) — wider, not blocked: this module has no
# opinion on whether an order is allowed, only on price. CLOSED-family
# states and UNKNOWN get the same conservative "wide, explicit" multiplier
# — if a tick somehow still arrives while the market is nominally shut or
# unclassifiable, price it defensively rather than at parity; distinct
# reason codes keep the two cases separately auditable.
SESSION_MULTIPLIER_OPEN = 1.00
SESSION_MULTIPLIER_PRE_MARKET = 1.25
SESSION_MULTIPLIER_AFTER_HOURS = 1.35
SESSION_MULTIPLIER_SAFE_DEFAULT = 2.00  # CLOSED/MAINTENANCE/WEEKEND/HOLIDAY/UNKNOWN/unavailable

_SESSION_MULTIPLIERS = {
    "OPEN": (SESSION_MULTIPLIER_OPEN, "session_open"),
    "PRE_MARKET": (SESSION_MULTIPLIER_PRE_MARKET, "session_pre_market"),
    "AFTER_HOURS": (SESSION_MULTIPLIER_AFTER_HOURS, "session_after_hours"),
    "CLOSED": (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_market_closed_wide_spread"),
    "MAINTENANCE": (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_market_closed_wide_spread"),
    "WEEKEND": (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_market_closed_wide_spread"),
    "HOLIDAY": (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_market_closed_wide_spread"),
    "UNKNOWN": (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_unknown_safe_default"),
}

# ── Source multipliers (SPREAD-05 §4) ───────────────────────────────────
# STALE is deliberately NOT priced on this axis (kept neutral here) — its
# risk premium is carried entirely by stale_multiplier below, so it is not
# double-counted across two multiplier axes for the same underlying signal.
# MARKET_CLOSED (a SourceState value distinct from the session axis above)
# is likewise kept neutral here for the same reason: session_multiplier
# already prices a closed/unknown market defensively.
SOURCE_MULTIPLIER_LIVE = 1.00
SOURCE_MULTIPLIER_SECONDARY = 1.05
SOURCE_MULTIPLIER_RECOVERY = 1.10
SOURCE_MULTIPLIER_SIMULATION = 1.15
SOURCE_MULTIPLIER_NEUTRAL = 1.00

_SOURCE_MULTIPLIERS = {
    "LIVE": (SOURCE_MULTIPLIER_LIVE, "source_live"),
    "SECONDARY": (SOURCE_MULTIPLIER_SECONDARY, "source_secondary"),
    "RECOVERY": (SOURCE_MULTIPLIER_RECOVERY, "source_recovery"),
    "SIMULATION": (SOURCE_MULTIPLIER_SIMULATION, "source_simulation"),
    "STALE": (SOURCE_MULTIPLIER_NEUTRAL, "source_stale_handled_by_stale_axis"),
    "MARKET_CLOSED": (SOURCE_MULTIPLIER_NEUTRAL, "source_market_closed_handled_by_session_axis"),
}

STALE_MULTIPLIER = 1.50
STALE_MULTIPLIER_REASON = "source_stale_wide_spread"

# Volatility/liquidity: pure, bounded placeholder formulas. No external
# data provider exists yet for either (SPREAD-05 §4 explicitly defers
# that) — both inputs default to None (neutral) until a real feed exists.
VOLATILITY_MULTIPLIER_MIN = 1.00
VOLATILITY_MULTIPLIER_MAX = 2.00
LIQUIDITY_MULTIPLIER_MIN = 1.00
LIQUIDITY_MULTIPLIER_MAX = 2.00


@dataclass(frozen=True)
class DynamicSpreadInputs:
    """Everything evaluate_dynamic_spread() needs, frozen once per tick by
    build_dynamic_inputs(). No field here is ever re-read from a live
    source by evaluate_dynamic_spread() itself — it is a pure function of
    exactly this object."""

    symbol: str
    base_spread_pips: float
    account_markup_pips: float
    is_dynamic: bool
    session_state: Optional[str]     # market_data.sessions.MarketSessionState value, or None
    source_state: Optional[str]      # market_data.contracts.SourceState value, or None
    stale: bool
    volatility_pips: Optional[float]
    liquidity_score: Optional[float]  # 0.0 (illiquid) .. 1.0 (fully liquid), or None
    manual_multiplier: Optional[float]
    manual_reason: str
    manual_expires_at: Optional[float]  # epoch seconds, or None
    evaluated_at: float                 # epoch seconds — the tick's own timestamp
    min_spread_pips: Optional[float]
    max_spread_pips: Optional[float]


@dataclass(frozen=True)
class DynamicSpreadDecision:
    schema_version: int
    dynamic_spread_enabled: bool
    base_spread_pips: float
    account_markup_pips: float
    session_multiplier: float
    source_multiplier: float
    stale_multiplier: float
    volatility_multiplier: float
    liquidity_multiplier: float
    manual_multiplier: float
    effective_before_bounds: float
    effective_after_bounds: float
    floor_applied: bool
    ceiling_applied: bool
    reason_codes: tuple[str, ...]
    evaluated_at: float
    decision_id: str


def _session_multiplier(session_state: Optional[str]) -> tuple[float, str]:
    if session_state is None:
        return SESSION_MULTIPLIER_SAFE_DEFAULT, "session_state_unavailable_safe_default"
    return _SESSION_MULTIPLIERS.get(
        session_state, (SESSION_MULTIPLIER_SAFE_DEFAULT, "session_unrecognized_safe_default"),
    )


def _source_multiplier(source_state: Optional[str]) -> tuple[float, str]:
    if source_state is None:
        return SOURCE_MULTIPLIER_NEUTRAL, "source_state_unavailable_safe_default"
    return _SOURCE_MULTIPLIERS.get(
        source_state, (SOURCE_MULTIPLIER_NEUTRAL, "source_unrecognized_neutral_default"),
    )


def _stale_multiplier(stale: bool) -> tuple[float, str]:
    if stale:
        return STALE_MULTIPLIER, STALE_MULTIPLIER_REASON
    return 1.00, "not_stale"


def _volatility_multiplier(volatility_pips: Optional[float]) -> tuple[float, str]:
    if volatility_pips is None:
        return 1.00, "volatility_input_absent_neutral"
    try:
        raw = 1.00 + (float(volatility_pips) / 100.0)
    except (TypeError, ValueError):
        return 1.00, "volatility_input_invalid_neutral"
    bounded = max(VOLATILITY_MULTIPLIER_MIN, min(VOLATILITY_MULTIPLIER_MAX, raw))
    return bounded, "volatility_input_applied"


def _liquidity_multiplier(liquidity_score: Optional[float]) -> tuple[float, str]:
    if liquidity_score is None:
        return 1.00, "liquidity_input_absent_neutral"
    try:
        score = max(0.0, min(1.0, float(liquidity_score)))
    except (TypeError, ValueError):
        return 1.00, "liquidity_input_invalid_neutral"
    raw = 1.00 + (1.00 - score)
    bounded = max(LIQUIDITY_MULTIPLIER_MIN, min(LIQUIDITY_MULTIPLIER_MAX, raw))
    return bounded, "liquidity_input_applied"


def _manual_multiplier(
    manual_multiplier: Optional[float], manual_expires_at: Optional[float],
    evaluated_at: float, manual_reason: str,
) -> tuple[float, str]:
    if manual_multiplier is None:
        return 1.00, "manual_override_absent"
    if manual_multiplier <= 0:
        return 1.00, "manual_override_invalid"
    if manual_multiplier == 1.00:
        return 1.00, "manual_override_neutral"
    if manual_expires_at is not None and manual_expires_at <= evaluated_at:
        return 1.00, "manual_override_expired"
    reason = f"manual_override_active:{manual_reason}" if manual_reason else "manual_override_active"
    return float(manual_multiplier), reason


def _decision_id(inputs: DynamicSpreadInputs, effective_after_bounds: float) -> str:
    """Deterministic — a hash of every input that could affect the
    decision, plus the final number. Same tick, same decision_id, always;
    no randomness, no wall-clock read inside this function."""
    parts = [
        inputs.symbol,
        f"{inputs.base_spread_pips:.6f}",
        f"{inputs.account_markup_pips:.6f}",
        str(inputs.is_dynamic),
        str(inputs.session_state),
        str(inputs.source_state),
        str(inputs.stale),
        str(inputs.volatility_pips),
        str(inputs.liquidity_score),
        str(inputs.manual_multiplier),
        str(inputs.manual_expires_at),
        f"{inputs.evaluated_at:.6f}",
        str(inputs.min_spread_pips),
        str(inputs.max_spread_pips),
        f"{effective_after_bounds:.6f}",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def evaluate_dynamic_spread(inputs: DynamicSpreadInputs) -> DynamicSpreadDecision:
    """Pure. No DB, no network, no clock read, no randomness. Never raises
    — any internal failure degrades to a neutral, fully-labeled decision
    (all multipliers 1.0, effective == base+markup) rather than blocking
    pricing."""
    try:
        base = float(inputs.base_spread_pips) + float(inputs.account_markup_pips)

        if not inputs.is_dynamic:
            session_mult, source_mult, stale_mult = 1.00, 1.00, 1.00
            vol_mult, liq_mult, manual_mult = 1.00, 1.00, 1.00
            reason_codes = ["dynamic_disabled"]
        else:
            session_mult, session_reason = _session_multiplier(inputs.session_state)
            source_mult, source_reason = _source_multiplier(inputs.source_state)
            stale_mult, stale_reason = _stale_multiplier(inputs.stale)
            vol_mult, vol_reason = _volatility_multiplier(inputs.volatility_pips)
            liq_mult, liq_reason = _liquidity_multiplier(inputs.liquidity_score)
            manual_mult, manual_reason_code = _manual_multiplier(
                inputs.manual_multiplier, inputs.manual_expires_at,
                inputs.evaluated_at, inputs.manual_reason,
            )
            reason_codes = [
                session_reason, source_reason, stale_reason,
                vol_reason, liq_reason, manual_reason_code,
            ]

        effective_before_bounds = base * session_mult * source_mult * stale_mult * vol_mult * liq_mult * manual_mult

        effective_after_bounds = effective_before_bounds
        floor_applied = False
        ceiling_applied = False
        if inputs.min_spread_pips is not None and effective_after_bounds < inputs.min_spread_pips:
            effective_after_bounds = inputs.min_spread_pips
            floor_applied = True
        if inputs.max_spread_pips is not None and effective_after_bounds > inputs.max_spread_pips:
            effective_after_bounds = inputs.max_spread_pips
            ceiling_applied = True

        decision_id = _decision_id(inputs, effective_after_bounds)

        return DynamicSpreadDecision(
            schema_version=SCHEMA_VERSION,
            dynamic_spread_enabled=inputs.is_dynamic,
            base_spread_pips=float(inputs.base_spread_pips),
            account_markup_pips=float(inputs.account_markup_pips),
            session_multiplier=session_mult,
            source_multiplier=source_mult,
            stale_multiplier=stale_mult,
            volatility_multiplier=vol_mult,
            liquidity_multiplier=liq_mult,
            manual_multiplier=manual_mult,
            effective_before_bounds=effective_before_bounds,
            effective_after_bounds=effective_after_bounds,
            floor_applied=floor_applied,
            ceiling_applied=ceiling_applied,
            reason_codes=tuple(reason_codes),
            evaluated_at=inputs.evaluated_at,
            decision_id=decision_id,
        )
    except Exception as exc:
        logger.warning("[dynamic_spread] evaluate_dynamic_spread failed for %s: %s", inputs.symbol, exc)
        base = float(inputs.base_spread_pips or 0.0) + float(inputs.account_markup_pips or 0.0)
        return DynamicSpreadDecision(
            schema_version=SCHEMA_VERSION, dynamic_spread_enabled=False,
            base_spread_pips=float(inputs.base_spread_pips or 0.0),
            account_markup_pips=float(inputs.account_markup_pips or 0.0),
            session_multiplier=1.0, source_multiplier=1.0, stale_multiplier=1.0,
            volatility_multiplier=1.0, liquidity_multiplier=1.0, manual_multiplier=1.0,
            effective_before_bounds=base, effective_after_bounds=base,
            floor_applied=False, ceiling_applied=False,
            reason_codes=("evaluation_failed_safe_passthrough",),
            evaluated_at=inputs.evaluated_at, decision_id="evaluation_failed",
        )


def build_dynamic_inputs(symbol: str, profile, ts) -> DynamicSpreadInputs:
    """Assembled ONCE per tick by consumers.py::price_tick(). Zero DB: the
    BrokerSpreadConfig read is the process-wide cache
    (spread_config_cache.get_cached_config — an in-memory dict), session
    evaluation is pure/declarative (market_data.sessions), and
    observability is an in-memory dict read (market_data.observability).
    `ts` is the tick's own timestamp — used as evaluated_at instead of a
    fresh wall-clock read, so re-evaluating from the frozen inputs later
    (pricing_context audit) never disagrees with what priced the tick.
    Never raises — degrades to an all-neutral, is_dynamic=False input set
    on any failure, exactly like the rest of this pipeline."""
    evaluated_at = float(ts) if ts is not None else time.time()
    try:
        from .spread_config_cache import get_cached_config
        from market_data.symbol_specs import normalize_symbol

        canonical = normalize_symbol(symbol)
        cfg = get_cached_config(canonical)
        is_dynamic = bool(cfg.is_dynamic) if cfg is not None else False
        base_spread_pips = float(cfg.spread_pips) if cfg is not None else 0.0

        session_state: Optional[str] = None
        source_state: Optional[str] = None
        stale = False

        if is_dynamic:
            try:
                from market_data.sessions import evaluate_market_session_for_symbol
                now_dt = datetime.fromtimestamp(evaluated_at, tz=timezone.utc)
                session_result = evaluate_market_session_for_symbol(canonical, now=now_dt)
                session_state = session_result.state.value
            except Exception as exc:
                logger.debug("[dynamic_spread] session evaluation failed for %s (non-fatal): %r", canonical, exc)

            try:
                from market_data.observability import get_symbol_state
                obs = get_symbol_state(canonical)
                source_state = obs.source_state.value if obs.source_state is not None else None
                stale = source_state == "STALE"
            except Exception as exc:
                logger.debug("[dynamic_spread] observability read failed for %s (non-fatal): %r", canonical, exc)

        return DynamicSpreadInputs(
            symbol=canonical,
            base_spread_pips=base_spread_pips,
            account_markup_pips=float(getattr(profile, "spread_markup_pips", 0.0) or 0.0),
            is_dynamic=is_dynamic,
            session_state=session_state,
            source_state=source_state,
            stale=stale,
            volatility_pips=None,
            liquidity_score=None,
            manual_multiplier=(cfg.manual_multiplier if cfg is not None else None),
            manual_reason=(cfg.manual_reason if cfg is not None else ""),
            manual_expires_at=(cfg.manual_expires_at if cfg is not None else None),
            evaluated_at=evaluated_at,
            min_spread_pips=getattr(profile, "min_spread_pips", None),
            max_spread_pips=getattr(profile, "max_spread_pips", None),
        )
    except Exception as exc:
        logger.warning("[dynamic_spread] build_dynamic_inputs failed for %s: %s", symbol, exc)
        return DynamicSpreadInputs(
            symbol=symbol, base_spread_pips=0.0, account_markup_pips=0.0, is_dynamic=False,
            session_state=None, source_state=None, stale=False,
            volatility_pips=None, liquidity_score=None,
            manual_multiplier=None, manual_reason="", manual_expires_at=None,
            evaluated_at=evaluated_at, min_spread_pips=None, max_spread_pips=None,
        )
