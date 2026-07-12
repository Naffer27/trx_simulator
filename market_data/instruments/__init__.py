"""
market_data/instruments — FOUNDATION-06: Instrument Profile Bridge.

Pure translation layer: market_data.symbol_specs.SymbolSpec (the live
runtime source) and simulator.models.Instrument (the DB catalog, currently
disconnected from trading — see docs/MARKET_DATA_ARCHITECTURE.md MD-1 §2)
both translate into the common InstrumentProfile contract, so they can be
compared field-by-field for drift.

This package does NOT change which source governs live trading.
market_data/symbol_specs.py remains the runtime source of truth — nothing
here is imported by feeds.py, consumers.py, risk_engine.py, spread_engine.py,
exposure_engine.py, or tasks.py.
"""

from .bridges import (
    DriftReport,
    FieldDifference,
    InstrumentLike,
    compare_profiles,
    profile_from_instrument,
    profile_from_symbol_spec,
    provider_mapping_from_instrument,
    provider_mappings_for_instrument,
)
from .profiles import InstrumentProfile
from .routing import RouterPolicyConfig, build_route_plan, default_policy_for_asset_class

__all__ = [
    "InstrumentProfile",
    "profile_from_symbol_spec",
    "profile_from_instrument",
    "provider_mapping_from_instrument",
    "provider_mappings_for_instrument",
    "compare_profiles",
    "DriftReport",
    "FieldDifference",
    "InstrumentLike",
    "RouterPolicyConfig",
    "build_route_plan",
    "default_policy_for_asset_class",
]
