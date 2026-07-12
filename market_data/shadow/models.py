"""
market_data/shadow/models.py — ShadowResult contract (FOUNDATION-08).

Pure output/reporting data. Constructed only by
market_data/shadow/service.py::evaluate_shadow_route() — trusted internal
construction, so validation here stays minimal (this is a report, not an
input boundary like NormalizedTick).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from market_data.contracts import OrderPolicy, SourceState
from market_data.router.models import ReasonCode


@dataclass(frozen=True, kw_only=True)
class ShadowResult:
    """
    One shadow-mode observation for one symbol at one point in time.

    All shadow_*/degraded/reason_code/agrees_with_legacy fields are None
    when error_code is set — a failed evaluation has nothing to report
    beyond the fact that it failed and, if it got that far, what legacy
    alone would have done.
    """

    canonical_symbol: str
    legacy_expected_provider: Optional[str]
    shadow_selected_provider: Optional[str]
    shadow_source_state: Optional[SourceState]
    shadow_order_policy: Optional[OrderPolicy]
    degraded: Optional[bool]
    reason_code: Optional[ReasonCode]
    agrees_with_legacy: Optional[bool]
    evaluated_at: int
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")
