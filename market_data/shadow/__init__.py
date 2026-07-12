"""
market_data/shadow — FOUNDATION-08: Provider Router Shadow Mode.

Runs SymbolSpec -> InstrumentProfile -> ProviderRoutePlan ->
ProviderRouter.decide() in parallel to the live runtime, purely for
observation and comparison. Never controls subscriptions, prices, failover,
or trading — market_data/feeds.py::FeedManager remains the sole runtime
authority.

Gated by settings.MARKET_DATA_SHADOW_MODE (default False). When False,
nothing in this package is even called — see the integration point in
market_data/feeds.py::FeedManager._maybe_run_shadow_evaluation().
"""

from .models import ShadowResult
from .service import evaluate_shadow_route, legacy_expected_provider

__all__ = [
    "ShadowResult",
    "evaluate_shadow_route",
    "legacy_expected_provider",
]
