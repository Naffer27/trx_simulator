"""
market_data/sessions/calendars.py — declarative calendar rules (FOUNDATION-11).

Six calendars, each a pure function (now_utc: datetime) -> _RawEvaluation.
No network, no vendor holiday calendar — HOLIDAY exists as a state value
this block can produce (see US_INDICES_CFD's docstring) but there is no
external holiday feed wired in; everything here is a documented
approximation, explicitly not claiming precision no data source backs yet.

next_open_at is populated whenever the state isn't OPEN (when will regular
trading next begin); next_close_at is populated only when the state is
OPEN (when does the current open window end). Every other combination is
None — deliberately not overclaiming a boundary that isn't the caller's
likely next question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .models import CalendarId, MarketSessionState, SessionReasonCode

_NY_TZ = ZoneInfo("America/New_York")
_UTC = timezone.utc


@dataclass(frozen=True, kw_only=True)
class _RawEvaluation:
    state: MarketSessionState
    reason_code: SessionReasonCode
    timezone: str
    next_open_at: Optional[datetime] = None
    next_close_at: Optional[datetime] = None


# ─── shared boundary helpers ────────────────────────────────────────────────


def _most_recent_weekday_at_hour(now_utc: datetime, *, weekday: int, hour: int) -> datetime:
    """Most recent occurrence of `weekday` (Mon=0..Sun=6) at `hour`:00 UTC,
    at or before now_utc."""
    days_since = (now_utc.weekday() - weekday) % 7
    candidate = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=days_since)
    if candidate > now_utc:
        candidate -= timedelta(days=7)
    return candidate


def _next_weekday_at(now_local: datetime, *, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Next occurrence of `weekday` at hour:minute local time, strictly
    after now_local (same-day match only counts if that time hasn't
    passed yet)."""
    days_ahead = (weekday - now_local.weekday()) % 7
    candidate = (now_local + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate


def _next_weekday_open(now_local: datetime, *, hour: int, minute: int = 0) -> datetime:
    """Next Mon-Fri occurrence of hour:minute local time, strictly after
    now_local."""
    candidate = now_local
    while True:
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.weekday() < 5:
            return candidate


# ─── weekly Sun 22:00 UTC -> Fri 22:00 UTC window (forex, metals) ──────────

_WEEK_OPEN_WEEKDAY = 6   # Sunday
_WEEK_OPEN_HOUR = 22
_WEEK_SPAN_DAYS = 5      # Sunday + 5 days = Friday


def _weekly_window(now_utc: datetime) -> tuple[datetime, datetime]:
    """(week_open, week_close) — the Sunday 22:00 UTC .. Friday 22:00 UTC
    boundaries of the trading week now_utc falls into (or just missed)."""
    week_open = _most_recent_weekday_at_hour(now_utc, weekday=_WEEK_OPEN_WEEKDAY, hour=_WEEK_OPEN_HOUR)
    week_close = week_open + timedelta(days=_WEEK_SPAN_DAYS)
    return week_open, week_close


def crypto_24_7(now_utc: datetime) -> _RawEvaluation:
    return _RawEvaluation(
        state=MarketSessionState.OPEN, reason_code=SessionReasonCode.MARKET_OPEN, timezone="UTC",
    )


def forex_24_5(now_utc: datetime) -> _RawEvaluation:
    """Documented approximation: open Sunday 22:00 UTC through Friday 22:00
    UTC, closed on weekends. No holiday calendar."""
    week_open, week_close = _weekly_window(now_utc)
    if week_open <= now_utc < week_close:
        return _RawEvaluation(
            state=MarketSessionState.OPEN, reason_code=SessionReasonCode.MARKET_OPEN,
            timezone="UTC", next_close_at=week_close,
        )
    next_open = week_open + timedelta(days=7)
    return _RawEvaluation(
        state=MarketSessionState.WEEKEND, reason_code=SessionReasonCode.WEEKEND_CLOSURE,
        timezone="UTC", next_open_at=next_open,
    )


# 21:00-22:00 UTC daily settlement gap, Mon-Fri, in addition to the weekly window.
_METALS_MAINT_START_HOUR = 21
_METALS_MAINT_END_HOUR = 22


def metals_23_5(now_utc: datetime) -> _RawEvaluation:
    """Documented approximation: same weekly window as forex, plus a short
    daily maintenance gap. No holiday calendar."""
    week_open, week_close = _weekly_window(now_utc)
    if not (week_open <= now_utc < week_close):
        next_open = week_open + timedelta(days=7)
        return _RawEvaluation(
            state=MarketSessionState.WEEKEND, reason_code=SessionReasonCode.WEEKEND_CLOSURE,
            timezone="UTC", next_open_at=next_open,
        )

    maint_start = now_utc.replace(hour=_METALS_MAINT_START_HOUR, minute=0, second=0, microsecond=0)
    maint_end = now_utc.replace(hour=_METALS_MAINT_END_HOUR, minute=0, second=0, microsecond=0)

    if maint_start <= now_utc < maint_end:
        return _RawEvaluation(
            state=MarketSessionState.MAINTENANCE, reason_code=SessionReasonCode.DAILY_MAINTENANCE,
            timezone="UTC", next_open_at=maint_end,
        )

    next_close = maint_start if now_utc < maint_start else maint_start + timedelta(days=1)
    next_close = min(next_close, week_close)
    return _RawEvaluation(
        state=MarketSessionState.OPEN, reason_code=SessionReasonCode.MARKET_OPEN,
        timezone="UTC", next_close_at=next_close,
    )


# ─── US indices CFD — regular hours in America/New_York, DST-aware ────────
#
# Pre-market 04:00-09:30, regular 09:30-16:00, after-hours 16:00-20:00,
# closed overnight 20:00-04:00, closed all day Sat/Sun. No external holiday
# calendar — a real NYSE holiday today would incorrectly show OPEN/CLOSED
# by the clock alone. HOLIDAY as a *state* exists in MarketSessionState for
# when a real vendor calendar is wired in; this calendar never produces it.

_PRE_MARKET_START_HOUR = 4
_MARKET_OPEN_HOUR, _MARKET_OPEN_MINUTE = 9, 30
_MARKET_CLOSE_HOUR = 16
_AFTER_HOURS_END_HOUR = 20


def us_indices_cfd(now_utc: datetime) -> _RawEvaluation:
    now_ny = now_utc.astimezone(_NY_TZ)

    if now_ny.weekday() >= 5:  # Saturday=5, Sunday=6
        next_open_ny = _next_weekday_at(now_ny, weekday=0, hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE)
        return _RawEvaluation(
            state=MarketSessionState.WEEKEND, reason_code=SessionReasonCode.WEEKEND_CLOSURE,
            timezone="America/New_York", next_open_at=next_open_ny.astimezone(_UTC),
        )

    pre_market_start = now_ny.replace(hour=_PRE_MARKET_START_HOUR, minute=0, second=0, microsecond=0)
    market_open = now_ny.replace(hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE, second=0, microsecond=0)
    market_close = now_ny.replace(hour=_MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    after_hours_end = now_ny.replace(hour=_AFTER_HOURS_END_HOUR, minute=0, second=0, microsecond=0)

    if pre_market_start <= now_ny < market_open:
        return _RawEvaluation(
            state=MarketSessionState.PRE_MARKET, reason_code=SessionReasonCode.PRE_MARKET_WINDOW,
            timezone="America/New_York", next_open_at=market_open.astimezone(_UTC),
        )
    if market_open <= now_ny < market_close:
        return _RawEvaluation(
            state=MarketSessionState.OPEN, reason_code=SessionReasonCode.MARKET_OPEN,
            timezone="America/New_York", next_close_at=market_close.astimezone(_UTC),
        )
    if market_close <= now_ny < after_hours_end:
        next_open_ny = _next_weekday_open(now_ny, hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE)
        return _RawEvaluation(
            state=MarketSessionState.AFTER_HOURS, reason_code=SessionReasonCode.AFTER_HOURS_WINDOW,
            timezone="America/New_York", next_open_at=next_open_ny.astimezone(_UTC),
        )

    # Overnight: before pre-market start, or after after-hours end.
    if now_ny < pre_market_start:
        next_open_ny = market_open
    else:
        next_open_ny = _next_weekday_open(now_ny, hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE)
    return _RawEvaluation(
        state=MarketSessionState.CLOSED, reason_code=SessionReasonCode.OVERNIGHT_CLOSURE,
        timezone="America/New_York", next_open_at=next_open_ny.astimezone(_UTC),
    )


def always_closed(now_utc: datetime) -> _RawEvaluation:
    return _RawEvaluation(
        state=MarketSessionState.CLOSED, reason_code=SessionReasonCode.ALWAYS_CLOSED, timezone="UTC",
    )


def unknown_calendar(now_utc: datetime) -> _RawEvaluation:
    return _RawEvaluation(
        state=MarketSessionState.UNKNOWN, reason_code=SessionReasonCode.UNKNOWN_CALENDAR, timezone="UTC",
    )


CALENDAR_RULES: dict[CalendarId, Callable[[datetime], _RawEvaluation]] = {
    CalendarId.CRYPTO_24_7: crypto_24_7,
    CalendarId.FOREX_24_5: forex_24_5,
    CalendarId.METALS_23_5: metals_23_5,
    CalendarId.US_INDICES_CFD: us_indices_cfd,
    CalendarId.ALWAYS_CLOSED: always_closed,
    CalendarId.UNKNOWN: unknown_calendar,
}
