"""
market_data/instruments/profiles.py — InstrumentProfile contract (FOUNDATION-06).

Pure data, no Django dependency. Defined in docs/FOUNDATION_02_MARKET_DATA_CORE.md §2.

This does NOT change which source governs live trading. market_data/symbol_specs.py
(SymbolSpec) remains the runtime source of truth, exactly as documented in
docs/MARKET_DATA_ARCHITECTURE.md (MD-1) §2. InstrumentProfile is a common
target both SymbolSpec and simulator.models.Instrument can be translated
into, for comparison and audit only.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_data.contracts import OrderPolicy, ProviderCapability
from market_data.providers.mappings import ProviderSymbolMapping

_VALID_MARGIN_MODES = frozenset({"leverage", "percent"})
_VALID_PNL_MODES = frozenset({"STANDARD", "INVERSE"})
_VALID_SPREAD_UNITS = frozenset({"pips", "points", "percent"})


@dataclass(frozen=True, kw_only=True)
class InstrumentProfile:
    """Common runtime shape both SymbolSpec and Instrument can be bridged into."""

    # ── Identity ──
    canonical_symbol: str
    display_name: str
    asset_class: str
    base_currency: str
    quote_currency: str
    profile_version: str = "1.0"

    # ── Trading ──
    pip_size: float
    tick_size: float
    price_decimals: int
    lot_step: float
    min_lot: float
    max_lot: float
    contract_size: float
    max_leverage: int

    # ── Costs ──
    default_spread: float
    spread_unit: str
    commission_per_lot: float = 0.0
    commission_pct: float = 0.0

    # ── Behavior ──
    margin_mode: str
    pnl_mode: str
    trading_enabled: bool
    trading_calendar_id: str = ""

    # ── Market data ──
    provider_mappings: tuple[ProviderSymbolMapping, ...] = ()
    required_capabilities: frozenset[ProviderCapability] = frozenset()
    simulation_allowed: bool = True
    default_order_policy_on_degradation: OrderPolicy = OrderPolicy.CLOSE_ONLY

    def __post_init__(self) -> None:
        if not self.canonical_symbol:
            raise ValueError("canonical_symbol must not be empty")

        for field_name in ("pip_size", "tick_size", "lot_step", "min_lot", "max_lot", "contract_size"):
            value = getattr(self, field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be positive, got {value!r}")

        if self.price_decimals < 0:
            raise ValueError(f"price_decimals must be >= 0, got {self.price_decimals!r}")

        if self.min_lot > self.max_lot:
            raise ValueError(f"min_lot ({self.min_lot}) must be <= max_lot ({self.max_lot})")

        # "lot_step coherente": positive, and not wider than the whole tradeable
        # range — a step bigger than max_lot could never produce a valid order.
        if self.lot_step > self.max_lot:
            raise ValueError(f"lot_step ({self.lot_step}) must be <= max_lot ({self.max_lot})")

        if self.max_leverage <= 0:
            raise ValueError(f"max_leverage must be > 0, got {self.max_leverage!r}")

        if self.margin_mode not in _VALID_MARGIN_MODES:
            raise ValueError(f"invalid margin_mode: {self.margin_mode!r}")
        if self.pnl_mode not in _VALID_PNL_MODES:
            raise ValueError(f"invalid pnl_mode: {self.pnl_mode!r}")
        if self.spread_unit not in _VALID_SPREAD_UNITS:
            raise ValueError(f"invalid spread_unit: {self.spread_unit!r}")

        for mapping in self.provider_mappings:
            if mapping.canonical_symbol != self.canonical_symbol:
                raise ValueError(
                    f"provider mapping for provider_id={mapping.provider_id!r} has "
                    f"canonical_symbol={mapping.canonical_symbol!r}, expected {self.canonical_symbol!r}"
                )

        priorities = [m.priority for m in self.provider_mappings]
        if len(set(priorities)) != len(priorities):
            raise ValueError(f"provider mapping priorities must be unique, got {priorities}")

        if (
            self.trading_enabled
            and not self.simulation_allowed
            and not any(m.enabled for m in self.provider_mappings)
        ):
            raise ValueError(
                "trading_enabled=True with simulation_allowed=False requires at least "
                "one enabled provider mapping"
            )

        # FOUNDATION-02 §3.5 / FOUNDATION-05 parity: real-money default must
        # never be OPEN_NORMAL when the only thing left to serve is simulation.
        if self.default_order_policy_on_degradation not in (OrderPolicy.CLOSE_ONLY, OrderPolicy.HALT_NEW_ORDERS):
            raise ValueError(
                "default_order_policy_on_degradation must be CLOSE_ONLY or HALT_NEW_ORDERS, "
                f"got {self.default_order_policy_on_degradation!r}"
            )
