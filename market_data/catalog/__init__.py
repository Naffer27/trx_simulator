"""
market_data/catalog — FOUNDATION-12: Runtime Instrument Catalog.

Pure indirection layer: get_runtime_instrument(symbol) is the one function
a future runtime call site should use instead of importing
market_data.symbol_specs directly — decoupling the runtime *conceptually*
from SymbolSpec without changing what governs it today.

Nothing in this package is imported by feeds.py, consumers.py,
risk_engine.py, spread_engine.py, exposure_engine.py, or tasks.py in this
block. SymbolSpec remains the sole runtime authority; no loop, no
provider, no trading rule, no price, no payload, and no newly-activated
symbol changes as a result of this package existing.
"""

from .service import ACTIVE_CATALOG_SOURCE, CatalogSource, compare_runtime_instrument, get_runtime_instrument

__all__ = [
    "CatalogSource",
    "ACTIVE_CATALOG_SOURCE",
    "get_runtime_instrument",
    "compare_runtime_instrument",
]
