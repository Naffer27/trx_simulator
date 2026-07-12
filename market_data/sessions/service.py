"""
market_data/sessions/service.py — market session evaluator (FOUNDATION-11).

evaluate_market_session(profile, *, now) never reads a real clock itself —
`now` is a required, timezone-aware datetime. evaluate_market_session_for_symbol()
is the boundary wrapper (symbol -> spec -> profile -> evaluate) that
defaults to the real clock, mirroring market_data/shadow/service.py and
market_data/runtime_router/service.py's established pattern: pure core,
real clock only at the integration edge.

Both never raise to their caller — any failure (naive datetime, unknown
symbol, unrecognized calendar, or anything else) is reported as
MarketSessionState.UNKNOWN + OrderPolicy.HALT_NEW_ORDERS +
SessionReasonCode.EVALUATION_ERROR, never propagated.

No network, no DB, no Django dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from market_data.contracts import OrderPolicy
from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.instruments.profiles import InstrumentProfile
from market_data.symbol_specs import get_spec

from .calendars import CALENDAR_RULES, unknown_calendar
from .models import (
    DEFAULT_CALENDAR_BY_ASSET_CLASS,
    CalendarId,
    MarketSessionResult,
    MarketSessionState,
    SessionReasonCode,
)

_ORDER_POLICY_BY_STATE: dict[MarketSessionState, OrderPolicy] = {
    MarketSessionState.OPEN: OrderPolicy.OPEN_NORMAL,
    # Extended-hours trading is real but thinner-liquidity, and this system
    # has no real extended-hours data source yet — allow managing existing
    # risk, not opening new exposure into that window.
    MarketSessionState.PRE_MARKET: OrderPolicy.CLOSE_ONLY,
    MarketSessionState.AFTER_HOURS: OrderPolicy.CLOSE_ONLY,
    # These are all "the market is genuinely not trading right now, and
    # that's expected" — OrderPolicy.MARKET_CLOSED (not HALT_NEW_ORDERS)
    # communicates that distinction, per FOUNDATION-02 §3.5.
    MarketSessionState.WEEKEND: OrderPolicy.MARKET_CLOSED,
    MarketSessionState.HOLIDAY: OrderPolicy.MARKET_CLOSED,
    MarketSessionState.MAINTENANCE: OrderPolicy.MARKET_CLOSED,
    MarketSessionState.CLOSED: OrderPolicy.MARKET_CLOSED,
    # UNKNOWN is different: we are not confident the market is closed —
    # HALT_NEW_ORDERS is restrictive because of uncertainty, not because we
    # can assert closure.
    MarketSessionState.UNKNOWN: OrderPolicy.HALT_NEW_ORDERS,
}


def evaluate_market_session(profile: InstrumentProfile, *, now: datetime) -> MarketSessionResult:
    """Never raises. `now` must be timezone-aware; anything else (including
    a naive datetime) safely degrades to UNKNOWN + HALT_NEW_ORDERS."""
    try:
        return _evaluate_market_session_inner(profile, now)
    except Exception:
        safe_now = now if isinstance(now, datetime) and now.tzinfo is not None else datetime.now(timezone.utc)
        return MarketSessionResult(
            canonical_symbol=profile.canonical_symbol,
            calendar_id=CalendarId.UNKNOWN,
            state=MarketSessionState.UNKNOWN,
            order_policy=OrderPolicy.HALT_NEW_ORDERS,
            evaluated_at=safe_now,
            reason_code=SessionReasonCode.EVALUATION_ERROR,
            timezone="UTC",
        )


def _evaluate_market_session_inner(profile: InstrumentProfile, now: datetime) -> MarketSessionResult:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)

    calendar_id = DEFAULT_CALENDAR_BY_ASSET_CLASS.get(profile.asset_class, CalendarId.UNKNOWN)
    rule = CALENDAR_RULES.get(calendar_id, unknown_calendar)
    raw = rule(now_utc)

    order_policy = _ORDER_POLICY_BY_STATE.get(raw.state, OrderPolicy.HALT_NEW_ORDERS)

    return MarketSessionResult(
        canonical_symbol=profile.canonical_symbol,
        calendar_id=calendar_id,
        state=raw.state,
        order_policy=order_policy,
        evaluated_at=now_utc,
        reason_code=raw.reason_code,
        timezone=raw.timezone,
        next_open_at=raw.next_open_at,
        next_close_at=raw.next_close_at,
    )


def evaluate_market_session_for_symbol(symbol: str, *, now: Optional[datetime] = None) -> MarketSessionResult:
    """Boundary wrapper: symbol -> SymbolSpec -> InstrumentProfile ->
    evaluate_market_session(). Defaults `now` to the real UTC clock when
    not supplied. Never raises."""
    evaluated_at = now if (now is not None and now.tzinfo is not None) else datetime.now(timezone.utc)
    try:
        spec = get_spec(symbol)
        profile = profile_from_symbol_spec(spec)
    except Exception:
        return MarketSessionResult(
            canonical_symbol=symbol,
            calendar_id=CalendarId.UNKNOWN,
            state=MarketSessionState.UNKNOWN,
            order_policy=OrderPolicy.HALT_NEW_ORDERS,
            evaluated_at=evaluated_at,
            reason_code=SessionReasonCode.EVALUATION_ERROR,
            timezone="UTC",
        )
    return evaluate_market_session(profile, now=evaluated_at)
