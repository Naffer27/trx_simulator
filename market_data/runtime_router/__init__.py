"""
market_data/runtime_router — FOUNDATION-09: Controlled Provider Router
Integration.

Lets the new ProviderRouter control *initial* provider selection for
symbols explicitly on settings.MARKET_DATA_ROUTER_SYMBOLS, only when
settings.MARKET_DATA_ROUTER_ENABLED=True. Every other symbol, and every
symbol at all when the flag is off, runs the exact original legacy logic
in market_data/feeds.py::FeedManager._try_live_legacy() — unchanged,
unduplicated.

Any error anywhere in this package's decision path falls back to legacy
immediately — see market_data/feeds.py::FeedManager._try_live() for the
integration point and market_data/runtime_router/service.py for the
never-raises guarantee.
"""

from .models import RuntimeSelectionResult
from .service import select_runtime_provider

__all__ = [
    "RuntimeSelectionResult",
    "select_runtime_provider",
]
