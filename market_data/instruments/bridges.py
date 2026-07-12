"""
market_data/instruments/bridges.py — SymbolSpec/Instrument -> InstrumentProfile
bridges, and the drift comparator (FOUNDATION-06).

Pure functions. No DB access anywhere in this module: profile_from_instrument
takes an already-loaded, duck-typed object (InstrumentLike) — it never
imports simulator.models or django.db, so market_data stays Django-free.

Does not change which source governs live trading: market_data/feeds.py,
simulator/consumers.py, and the rest of the runtime keep reading
market_data/symbol_specs.py directly, exactly as before this block.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from market_data.contracts import OrderPolicy, ProviderCapability
from market_data.providers.mappings import ProviderSymbolMapping
from market_data.symbol_specs import SymbolSpec, normalize_symbol

from .profiles import InstrumentProfile

_PROFILE_VERSION = "1.0"


# ─── SymbolSpec -> InstrumentProfile ────────────────────────────────────────


def _derive_base_currency(symbol: str, quote_currency: str) -> str:
    """'EUR/USD' -> 'EUR'; 'BTCUSD' + quote='USD' -> 'BTC'; 'US30' -> 'US30' (no
    meaningful base/quote split for an index — matches simulator.models.Instrument's
    own seed convention of base_currency=symbol for index rows)."""
    if "/" in symbol:
        return symbol.split("/", 1)[0]
    if quote_currency and symbol.endswith(quote_currency) and len(symbol) > len(quote_currency):
        return symbol[: -len(quote_currency)]
    return symbol


def profile_from_symbol_spec(spec: SymbolSpec) -> InstrumentProfile:
    """
    Convert a SymbolSpec (the live runtime source, market_data/symbol_specs.py)
    into an InstrumentProfile. Derives fields SymbolSpec has no concept of
    (pnl_mode, spread_unit, trading_calendar_id) using rules verified against
    every row simulator/management/commands/seed_instruments.py already seeds.
    """
    pnl_mode = "INVERSE" if spec.quote_currency != "USD" else "STANDARD"
    trading_calendar_id = "24/7" if spec.asset_class == "crypto" else "24/5"
    spread_unit = "points" if spec.asset_class == "index" else "pips"
    default_spread = (spec.spread / spec.pip_size) if spec.pip_size else 0.0

    # Priority is dense (0, 1, 2, ...) among whichever mappings are actually
    # present, in binance -> kraken -> finnhub preference order — not a fixed
    # slot per provider type. A symbol with only finnhub configured must get
    # priority=0 for it, not 2 (matches the real fallback order in
    # market_data/feeds.py::_try_live, and keeps priority dense/comparable
    # against a DB-derived profile that only ever has one mapping).
    candidates = [
        ("binance", spec.exchange_symbol, frozenset({ProviderCapability.BID_ASK})),
        ("kraken", spec.kraken_symbol, frozenset({ProviderCapability.BID_ASK})),
        ("finnhub", spec.finnhub_symbol, frozenset({ProviderCapability.LAST_PRICE})),
    ]
    mappings: list[ProviderSymbolMapping] = [
        ProviderSymbolMapping(
            canonical_symbol=spec.symbol, provider_id=provider_id, provider_symbol=provider_symbol,
            priority=priority, enabled=spec.enabled, required_capabilities=required_capabilities,
        )
        for priority, (provider_id, provider_symbol, required_capabilities) in enumerate(
            (c for c in candidates if c[1])
        )
    ]

    return InstrumentProfile(
        profile_version=_PROFILE_VERSION,
        canonical_symbol=spec.symbol,
        display_name=spec.symbol,
        asset_class=spec.asset_class,
        base_currency=_derive_base_currency(spec.symbol, spec.quote_currency),
        quote_currency=spec.quote_currency,
        pip_size=spec.pip_size,
        tick_size=spec.tick_size,
        price_decimals=spec.price_decimals,
        lot_step=spec.lot_step,
        min_lot=spec.min_lot,
        max_lot=spec.max_lot,
        contract_size=spec.contract_size,
        max_leverage=spec.max_leverage,
        default_spread=default_spread,
        spread_unit=spread_unit,
        commission_per_lot=0.0,
        commission_pct=spec.commission_pct,
        margin_mode=spec.margin_mode,
        pnl_mode=pnl_mode,
        trading_enabled=spec.enabled,
        trading_calendar_id=trading_calendar_id,
        provider_mappings=tuple(mappings),
        required_capabilities=frozenset({ProviderCapability.BID_ASK}),
        simulation_allowed=True,
        default_order_policy_on_degradation=OrderPolicy.CLOSE_ONLY,
    )


# ─── Instrument (DB) -> InstrumentProfile ──────────────────────────────────


class InstrumentLike(Protocol):
    """Structural shape of simulator.models.Instrument — duck-typed so this
    module never imports Django. A real Instrument instance satisfies this
    without any adapter code."""

    symbol: str
    display_name: str
    asset_class: str
    base_currency: str
    quote_currency: str
    pip_size: object
    tick_size: object
    price_decimals: int
    lot_step: object
    min_lot: object
    max_lot: object
    contract_size: object
    default_spread: object
    spread_unit: str
    commission_per_lot: object
    commission_pct: object
    max_leverage: int
    margin_mode: str
    pnl_mode: str
    trading_enabled: bool
    session: str
    market_data_provider: str
    provider_symbol: str


def provider_mapping_from_instrument(instrument: InstrumentLike) -> tuple[ProviderSymbolMapping, ...]:
    """
    Build the (0 or 1) provider mapping described by an Instrument row's own
    market_data_provider/provider_symbol fields. Unlike SymbolSpec, an
    Instrument only ever names a single provider. Does not query the DB —
    reads only attributes already present on the passed-in object.
    """
    provider_id = getattr(instrument, "market_data_provider", "sim") or "sim"
    provider_symbol = getattr(instrument, "provider_symbol", "") or ""
    if provider_id == "sim" or not provider_symbol:
        return ()
    return (
        ProviderSymbolMapping(
            canonical_symbol=normalize_symbol(instrument.symbol),
            provider_id=provider_id,
            provider_symbol=provider_symbol,
            priority=0,
            enabled=bool(getattr(instrument, "trading_enabled", False)),
            required_capabilities=frozenset(),
        ),
    )


# ─── Known secondary providers (FOUNDATION-06b) ─────────────────────────────
#
# simulator.models.Instrument has exactly one (market_data_provider,
# provider_symbol) pair — it cannot yet encode a fallback provider.
# market_data/symbol_specs.py's BTCUSD/ETHUSD entries genuinely have both
# Binance (primary, live in feeds.py::_try_live) and Kraken (secondary
# fallback, same function) — provider_mapping_from_instrument() alone can
# only ever see the primary, which made the audit report a false CRITICAL
# provider_mappings drift for both symbols.
#
# This is a single, explicit, declarative overlay — not a model change, not
# a second hardcoded instrument list (it only carries the one fact the
# Instrument model can't yet hold: the secondary provider symbol) — and it
# is a real, verified fact about production routing, not an invented one.
# It is a stopgap: the correct long-term fix is a real one-to-many
# Instrument -> provider mapping table, out of scope here (tracked as a
# follow-up, not built in this block).
_KNOWN_SECONDARY_PROVIDERS: dict[str, tuple[str, str]] = {
    # canonical_symbol: (provider_id, provider_symbol)
    "BTCUSD": ("kraken", "XBT/USD"),
    "ETHUSD": ("kraken", "ETH/USD"),
}


def provider_mappings_for_instrument(instrument: InstrumentLike) -> tuple[ProviderSymbolMapping, ...]:
    """
    Full provider mapping set for an Instrument row: its own primary mapping
    (provider_mapping_from_instrument) plus any known secondary from the
    overlay above, at the next priority slot. Use this (not the raw
    single-provider function) when building a DB-side InstrumentProfile for
    comparison against the runtime — it reflects what is actually true about
    production routing, not just what one DB column can currently hold.
    """
    primary = provider_mapping_from_instrument(instrument)
    canonical_symbol = normalize_symbol(instrument.symbol)
    secondary_spec = _KNOWN_SECONDARY_PROVIDERS.get(canonical_symbol)
    if secondary_spec is None:
        return primary

    provider_id, provider_symbol = secondary_spec
    secondary = ProviderSymbolMapping(
        canonical_symbol=canonical_symbol,
        provider_id=provider_id,
        provider_symbol=provider_symbol,
        priority=len(primary),
        enabled=bool(getattr(instrument, "trading_enabled", False)),
        required_capabilities=frozenset(),
    )
    return primary + (secondary,)


def profile_from_instrument(
    instrument: InstrumentLike,
    *,
    provider_mappings: tuple[ProviderSymbolMapping, ...] = (),
    required_capabilities: frozenset[ProviderCapability] = frozenset({ProviderCapability.BID_ASK}),
    simulation_allowed: bool = True,
    default_order_policy_on_degradation: OrderPolicy = OrderPolicy.CLOSE_ONLY,
) -> InstrumentProfile:
    """
    Convert an already-loaded Instrument-like object into an InstrumentProfile.
    Never queries the DB itself — provider_mappings must be built by the
    caller (see provider_mapping_from_instrument) and passed in explicitly.
    """
    return InstrumentProfile(
        profile_version=_PROFILE_VERSION,
        canonical_symbol=normalize_symbol(instrument.symbol),
        display_name=instrument.display_name,
        asset_class=instrument.asset_class,
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
        pip_size=float(instrument.pip_size),
        tick_size=float(instrument.tick_size),
        price_decimals=int(instrument.price_decimals),
        lot_step=float(instrument.lot_step),
        min_lot=float(instrument.min_lot),
        max_lot=float(instrument.max_lot),
        contract_size=float(instrument.contract_size),
        max_leverage=int(instrument.max_leverage),
        default_spread=float(instrument.default_spread),
        spread_unit=instrument.spread_unit,
        commission_per_lot=float(instrument.commission_per_lot),
        commission_pct=float(instrument.commission_pct),
        margin_mode=instrument.margin_mode,
        pnl_mode=instrument.pnl_mode,
        trading_enabled=instrument.trading_enabled,
        trading_calendar_id=instrument.session,
        provider_mappings=provider_mappings,
        required_capabilities=required_capabilities,
        simulation_allowed=simulation_allowed,
        default_order_policy_on_degradation=default_order_policy_on_degradation,
    )


# ─── Drift comparison ───────────────────────────────────────────────────────

_CRITICAL_FIELDS = frozenset({
    "contract_size", "pip_size", "tick_size", "lot_step", "min_lot", "max_lot",
    "max_leverage", "trading_enabled", "provider_mappings", "pnl_mode", "margin_mode",
})

_WARNING_FIELDS = frozenset({
    "display_name", "asset_class", "base_currency", "quote_currency", "price_decimals",
    "default_spread", "spread_unit", "commission_per_lot", "commission_pct",
    "trading_calendar_id", "required_capabilities", "simulation_allowed",
    "default_order_policy_on_degradation",
})

_COMPARABLE_FIELDS = _CRITICAL_FIELDS | _WARNING_FIELDS

_NUMERIC_FIELDS = frozenset({
    "pip_size", "tick_size", "price_decimals", "lot_step", "min_lot", "max_lot",
    "contract_size", "max_leverage", "default_spread", "commission_per_lot", "commission_pct",
})

_FLOAT_EPSILON = 1e-9


@dataclass(frozen=True, kw_only=True)
class FieldDifference:
    field: str
    runtime_value: object
    db_value: object
    severity: str  # "critical" | "warning"


@dataclass(frozen=True, kw_only=True)
class DriftReport:
    canonical_symbol: str
    matches: tuple[str, ...]
    differences: tuple[FieldDifference, ...]
    critical_differences: tuple[FieldDifference, ...]
    warning_differences: tuple[FieldDifference, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.differences)


def _mapping_identity(mapping: ProviderSymbolMapping) -> tuple:
    """Projection used to compare provider_mappings across sources that
    annotate them differently. `priority` ordinality and `required_capabilities`
    granularity are bridge-specific metadata (SymbolSpec derives per-provider
    capability requirements; Instrument has no such column at all) — comparing
    them at full dataclass equality would flag drift that isn't really "the
    two sources disagree about who serves this symbol," just "the two bridges
    annotate it differently." Identity is: which provider, which raw symbol,
    and whether it's usable."""
    return (mapping.provider_id, mapping.provider_symbol, mapping.enabled)


def _values_equal(field: str, a: object, b: object) -> bool:
    if field == "provider_mappings":
        return {_mapping_identity(m) for m in a} == {_mapping_identity(m) for m in b}
    if field in _NUMERIC_FIELDS:
        return abs(float(a) - float(b)) < _FLOAT_EPSILON
    return a == b


def compare_profiles(runtime_profile: InstrumentProfile, db_profile: InstrumentProfile) -> DriftReport:
    """
    Compare two InstrumentProfile instances for the SAME canonical_symbol.
    Never raises on drift — drift is data in the returned report, not an
    error. Raises ValueError only on caller misuse (comparing two different
    symbols, which isn't drift — it's an invalid comparison).
    """
    if runtime_profile.canonical_symbol != db_profile.canonical_symbol:
        raise ValueError(
            f"compare_profiles requires the same canonical_symbol on both sides, got "
            f"{runtime_profile.canonical_symbol!r} vs {db_profile.canonical_symbol!r}"
        )

    matches: list[str] = []
    differences: list[FieldDifference] = []

    for field in sorted(_COMPARABLE_FIELDS):
        runtime_value = getattr(runtime_profile, field)
        db_value = getattr(db_profile, field)
        if _values_equal(field, runtime_value, db_value):
            matches.append(field)
        else:
            severity = "critical" if field in _CRITICAL_FIELDS else "warning"
            differences.append(FieldDifference(
                field=field, runtime_value=runtime_value, db_value=db_value, severity=severity,
            ))

    critical = tuple(d for d in differences if d.severity == "critical")
    warning = tuple(d for d in differences if d.severity == "warning")

    return DriftReport(
        canonical_symbol=runtime_profile.canonical_symbol,
        matches=tuple(matches),
        differences=tuple(differences),
        critical_differences=critical,
        warning_differences=warning,
    )
