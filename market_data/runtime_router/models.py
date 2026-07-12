"""
market_data/runtime_router/models.py — RuntimeSelectionResult (FOUNDATION-09).

Pure output/reporting data. Constructed only by
market_data/runtime_router/service.py::select_runtime_provider() — trusted
internal construction, minimal validation (this is a report, not an input
boundary like NormalizedTick).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from market_data.contracts import OrderPolicy, SourceState
from market_data.router.models import ReasonCode


@dataclass(frozen=True, kw_only=True)
class RuntimeSelectionResult:
    """
    One provider-selection outcome for one symbol, from the new
    ProviderRouter pipeline.

    fallback_to_legacy=True means select_runtime_provider() could not
    produce a usable decision at all (unknown symbol, or an error building
    the profile/plan/decision) — the caller (FeedManager) must run its
    original legacy logic instead. This is distinct from
    selected_provider_id=None, which is a *valid* outcome (the router ran
    fine and decided nothing live is available — let the existing
    simulation fallback take over, same as legacy would).

    order_policy and degraded (FOUNDATION-13) passthrough the same-named
    fields RouteDecision already computes inside decide() — not new logic,
    just exposing what the router already decided so
    market_data/observability doesn't have to re-derive them.
    """

    symbol: str
    selected_provider_id: Optional[str]
    selected_provider_symbol: Optional[str]
    source_state: Optional[SourceState]
    reason_code: Optional[ReasonCode]
    used_new_router: bool
    fallback_to_legacy: bool
    error_code: Optional[str] = None
    order_policy: Optional[OrderPolicy] = None
    degraded: bool = False

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
