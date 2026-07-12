"""
market_data/sessions — FOUNDATION-11: Market Sessions & Market Status.

Pure, declarative calendar layer: is an instrument OPEN, CLOSED,
PRE_MARKET, AFTER_HOURS, WEEKEND, HOLIDAY, MAINTENANCE, or UNKNOWN right
now — and what OrderPolicy follows from that. No network, no vendor
holiday calendar, no Django dependency.

Used by market_data.runtime_router (FOUNDATION-09/10) to distinguish "the
market is closed" from "the provider is down" for allowlisted symbols
only — everything outside the allowlist, and everything when
MARKET_DATA_ROUTER_ENABLED=False, is completely unaffected.
"""

from .calendars import CALENDAR_RULES
from .models import (
    DEFAULT_CALENDAR_BY_ASSET_CLASS,
    CalendarId,
    MarketSessionResult,
    MarketSessionState,
    SessionReasonCode,
)
from .service import evaluate_market_session, evaluate_market_session_for_symbol

__all__ = [
    "MarketSessionState",
    "SessionReasonCode",
    "CalendarId",
    "MarketSessionResult",
    "DEFAULT_CALENDAR_BY_ASSET_CLASS",
    "CALENDAR_RULES",
    "evaluate_market_session",
    "evaluate_market_session_for_symbol",
]
