"""
market_data/sessions/models.py — market session contracts (FOUNDATION-11).

Pure data. No Django dependency, no clock reads, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from market_data.contracts import OrderPolicy


class MarketSessionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    AFTER_HOURS = "AFTER_HOURS"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN = "UNKNOWN"


class SessionReasonCode(str, Enum):
    """Why evaluate_market_session reached the state it did. Distinct from
    market_data.router.models.ReasonCode — that enum explains a *provider*
    selection outcome; this one explains a *calendar* outcome. The router's
    ReasonCode.MARKET_CLOSED is what a caller sets on a provider decision
    once it already knows the session is closed via this module."""

    MARKET_OPEN = "MARKET_OPEN"
    WEEKEND_CLOSURE = "WEEKEND_CLOSURE"
    OVERNIGHT_CLOSURE = "OVERNIGHT_CLOSURE"
    DAILY_MAINTENANCE = "DAILY_MAINTENANCE"
    PRE_MARKET_WINDOW = "PRE_MARKET_WINDOW"
    AFTER_HOURS_WINDOW = "AFTER_HOURS_WINDOW"
    HOLIDAY_CLOSURE = "HOLIDAY_CLOSURE"
    ALWAYS_CLOSED = "ALWAYS_CLOSED"
    UNKNOWN_CALENDAR = "UNKNOWN_CALENDAR"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class CalendarId(str, Enum):
    """The declarative calendars this block ships. Adding a real one (with
    a holiday vendor) later is a new enum member + a new calendars.py rule
    function — never a hardcode keyed off a specific symbol."""

    CRYPTO_24_7 = "CRYPTO_24_7"
    FOREX_24_5 = "FOREX_24_5"
    METALS_23_5 = "METALS_23_5"
    US_INDICES_CFD = "US_INDICES_CFD"
    ALWAYS_CLOSED = "ALWAYS_CLOSED"
    UNKNOWN = "UNKNOWN"


# Declarative asset_class -> calendar default. The ONLY place this mapping
# is allowed to live — no per-symbol hardcodes anywhere else in this package.
# energy has no real calendar modeled yet (no instrument uses it today —
# XAU/USD, USOIL are explicitly not activated by this block either).
DEFAULT_CALENDAR_BY_ASSET_CLASS: dict[str, CalendarId] = {
    "crypto": CalendarId.CRYPTO_24_7,
    "forex": CalendarId.FOREX_24_5,
    "metal": CalendarId.METALS_23_5,
    "index": CalendarId.US_INDICES_CFD,
    "energy": CalendarId.UNKNOWN,
}


@dataclass(frozen=True, kw_only=True)
class MarketSessionResult:
    """One session evaluation for one symbol at one point in time."""

    canonical_symbol: str
    calendar_id: CalendarId
    state: MarketSessionState
    order_policy: OrderPolicy
    evaluated_at: datetime
    reason_code: SessionReasonCode
    timezone: str
    next_open_at: Optional[datetime] = None
    next_close_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if self.next_open_at is not None and self.next_open_at.tzinfo is None:
            raise ValueError("next_open_at must be timezone-aware")
        if self.next_close_at is not None and self.next_close_at.tzinfo is None:
            raise ValueError("next_close_at must be timezone-aware")
