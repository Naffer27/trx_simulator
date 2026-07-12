"""
market_data/catalog/service.py — Runtime Instrument Catalog (FOUNDATION-12).

The single indirection point a future runtime call site (eventually
FeedManager) would use instead of importing market_data.symbol_specs
directly. Today it always answers from SymbolSpec — SymbolSpec remains the
runtime authority, unchanged. The point of this module existing is that a
future Foundation can change get_runtime_instrument()'s *internals* (e.g.
to source from a DB-backed InstrumentProfile once FOUNDATION-06's MD-5
"single source of truth" decision is made) without touching any caller —
because callers only ever import get_runtime_instrument(), never
SymbolSpec/get_spec directly.

Deliberately NOT a "never raises" boundary like market_data/shadow/service.py
or market_data/runtime_router/service.py: get_runtime_instrument() is meant
to be a transparent stand-in for market_data.symbol_specs.get_spec(), which
itself raises KeyError for an unknown symbol today. Swallowing that here
would be an actual behavior change relative to today's call sites — exactly
what this block promises not to do.

No Django dependency, no DB, no network — same isolation guarantee as every
other market_data/* package. The DB side of drift comparison (reading
simulator.models.Instrument) intentionally lives in simulator/, not here —
see simulator/runtime_instrument_catalog.py. compare_runtime_instrument()
below only ever receives an already-built InstrumentProfile, never queries
anything itself.
"""

from __future__ import annotations

from enum import Enum

from market_data.instruments.bridges import DriftReport, compare_profiles, profile_from_symbol_spec
from market_data.instruments.profiles import InstrumentProfile
from market_data.symbol_specs import get_spec


class CatalogSource(str, Enum):
    """Which representation actually answered a get_runtime_instrument()
    call. Exactly one member is live today — the enum exists so a future
    Foundation's swap is a one-line, discoverable, testable change here,
    not a hunt through call sites."""

    SYMBOL_SPEC = "SYMBOL_SPEC"
    INSTRUMENT_PROFILE = "INSTRUMENT_PROFILE"  # not produced by this block yet


ACTIVE_CATALOG_SOURCE = CatalogSource.SYMBOL_SPEC


def get_runtime_instrument(symbol: str) -> InstrumentProfile:
    """
    The runtime's one way to ask "what is this instrument?" — today,
    always market_data.symbol_specs.SymbolSpec, converted to the modern
    InstrumentProfile shape via the existing FOUNDATION-06 bridge.

    Raises KeyError for an unrecognized symbol — same as get_spec() does
    today, deliberately not swallowed, so a future caller that switches
    from get_spec(symbol) to this function sees identical error behavior.
    """
    spec = get_spec(symbol)
    return profile_from_symbol_spec(spec)


def compare_runtime_instrument(symbol: str, alternate_profile: InstrumentProfile) -> DriftReport:
    """
    Pure comparison: get_runtime_instrument(symbol) vs an already-built
    alternate InstrumentProfile (e.g. DB-derived) the caller supplies.
    Reuses market_data.instruments.bridges.compare_profiles() as-is — no
    new drift-classification logic invented here. Raises KeyError if
    `symbol` isn't known to SymbolSpec (same transparency guarantee as
    get_runtime_instrument()); raises ValueError if the two profiles are
    for different canonical symbols (compare_profiles' own guard).
    """
    runtime_profile = get_runtime_instrument(symbol)
    return compare_profiles(runtime_profile, alternate_profile)
