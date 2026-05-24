# market_data/symbol_specs.py
"""
Instrument Specification Registry — single source of truth for all tradeable symbols.

Every system component (consumers, feeds, risk engine, exposure engine) must derive
instrument parameters from here. No symbol-specific hardcodes elsewhere.

Import pattern:
    from market_data.symbol_specs import get_spec, allowed_symbols, kline_symbols
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SymbolSpec:
    """Immutable specification for one tradeable instrument."""

    # ── Identity ──────────────────────────────────────────────────────────────
    symbol: str
    asset_class: str        # "forex" | "crypto" | "metal" | "index"

    # ── Contract definition ───────────────────────────────────────────────────
    contract_size: float    # units per 1.0 lot
                            #   forex  → 100_000  (1 standard lot = 100K base-currency units)
                            #   crypto → 1.0      (1 lot = 1 coin)
                            #   metals → 100.0    (XAU = 100 troy oz per lot)
                            #   index  → 1.0      (1 contract = 1 point unless noted)
    min_lot: float          # minimum tradeable lot size
    max_lot: float          # maximum lot size per order
    lot_step: float         # minimum lot increment

    # ── Pricing ───────────────────────────────────────────────────────────────
    tick_size: float        # minimum price movement
    pip_size: float         # one pip in price terms (0.0001 majors, 0.01 JPY, 1.0 BTC)
    price_decimals: int     # display precision
    base_price: float       # seed / last-resort fallback price
    sim_drift: float        # synthetic bar body (price units) — sim fallback only

    # ── Execution costs ───────────────────────────────────────────────────────
    spread: float           # typical full bid-ask spread in price units (ask − bid)
                            #   EUR/USD 0.00015 = 1.5 pips; BTC 15.0 = $15 total spread
    commission_pct: float   # on-open commission as fraction of notional
                            #   0.0 = pure-spread model (forex ECN/STP)
                            #   0.0001 = 0.01% (crypto, charged on open)

    # ── Margin ────────────────────────────────────────────────────────────────
    max_leverage: int       # per-instrument cap; effective = min(account.leverage, max_leverage)
    margin_mode: str = "leverage"  # "leverage" | "percent" (future fixed-% margin)

    # ── Market-data routing ───────────────────────────────────────────────────
    kline_source: Optional[str] = None     # None | "binance" | "kraken"
    exchange_symbol: Optional[str] = None  # Binance/exchange symbol (e.g. "BTCUSDT")
    finnhub_symbol: Optional[str] = None   # Finnhub format (e.g. "FX:EURUSD")
    kraken_symbol: Optional[str] = None    # Kraken format (e.g. "XBT/USD")

    # ── Settlement ────────────────────────────────────────────────────────────
    quote_currency: str = "USD"

    # ── Backend gate ──────────────────────────────────────────────────────────
    enabled: bool = True    # False = spec defined for future use; backend rejects orders


# ─── Registry ─────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, SymbolSpec] = {}


def _reg(spec: SymbolSpec) -> None:
    _REGISTRY[spec.symbol] = spec


# ─── Forex — Major Pairs ─────────────────────────────────────────────────────

_reg(SymbolSpec(
    symbol="EUR/USD", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=1.17000, sim_drift=0.0008,
    spread=0.00015,     # 1.5 pips — competitive retail ECN
    commission_pct=0.0, # pure-spread model
    max_leverage=500,
    finnhub_symbol="FX:EURUSD",
))

_reg(SymbolSpec(
    symbol="GBP/USD", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=1.30000, sim_drift=0.0010,
    spread=0.00018,     # 1.8 pips
    commission_pct=0.0,
    max_leverage=500,
    finnhub_symbol="FX:GBPUSD",
))

_reg(SymbolSpec(
    symbol="USD/JPY", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.001, pip_size=0.01, price_decimals=3,
    base_price=155.000, sim_drift=0.08,
    spread=0.018,       # 1.8 JPY pips
    commission_pct=0.0,
    max_leverage=500,
    finnhub_symbol="FX:USDJPY",
))

_reg(SymbolSpec(
    symbol="AUD/USD", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=0.68000, sim_drift=0.0008,
    spread=0.00017,     # 1.7 pips
    commission_pct=0.0,
    max_leverage=500,
    finnhub_symbol="FX:AUDUSD",
))

# ─── Forex — Additional Pairs (defined, not yet enabled in backend) ───────────

_reg(SymbolSpec(
    symbol="USD/CAD", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=1.37000, sim_drift=0.0008,
    spread=0.00020, commission_pct=0.0, max_leverage=500,
    finnhub_symbol="FX:USDCAD", enabled=False,
))

_reg(SymbolSpec(
    symbol="USD/CHF", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=0.90000, sim_drift=0.0008,
    spread=0.00018, commission_pct=0.0, max_leverage=500,
    finnhub_symbol="FX:USDCHF", enabled=False,
))

_reg(SymbolSpec(
    symbol="NZD/USD", asset_class="forex",
    contract_size=100_000, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.00001, pip_size=0.0001, price_decimals=5,
    base_price=0.62000, sim_drift=0.0007,
    spread=0.00020, commission_pct=0.0, max_leverage=500,
    finnhub_symbol="FX:NZDUSD", enabled=False,
))

# ─── Crypto ───────────────────────────────────────────────────────────────────

_reg(SymbolSpec(
    symbol="BTCUSD", asset_class="crypto",
    contract_size=1.0, min_lot=0.001, max_lot=10.0, lot_step=0.001,
    tick_size=0.01, pip_size=1.0, price_decimals=2,
    base_price=82000.0, sim_drift=12.0,
    spread=15.0,           # $15 total bid-ask spread — realistic retail crypto CFD
    commission_pct=0.0001, # 0.01% on open (on top of spread)
    max_leverage=20,       # crypto cap: 20x (VT Markets=10x, FTMO=2x, Pepperstone=5x)
    kline_source="binance",
    exchange_symbol="BTCUSDT",
    kraken_symbol="XBT/USD",
))

_reg(SymbolSpec(
    symbol="ETHUSD", asset_class="crypto",
    contract_size=1.0, min_lot=0.01, max_lot=100.0, lot_step=0.01,
    tick_size=0.01, pip_size=1.0, price_decimals=2,
    base_price=3400.0, sim_drift=2.0,
    spread=3.0,            # $3 spread
    commission_pct=0.0001,
    max_leverage=20,
    kline_source="binance",
    exchange_symbol="ETHUSDT",
    kraken_symbol="ETH/USD",
))

_reg(SymbolSpec(
    symbol="SOLUSD", asset_class="crypto",
    contract_size=1.0, min_lot=0.1, max_lot=1000.0, lot_step=0.1,
    tick_size=0.01, pip_size=0.01, price_decimals=2,
    base_price=170.0, sim_drift=0.5,
    spread=0.20, commission_pct=0.0001, max_leverage=10,
    enabled=False,
))

# ─── Metals ───────────────────────────────────────────────────────────────────

_reg(SymbolSpec(
    symbol="XAU/USD", asset_class="metal",
    contract_size=100.0,    # 1 lot = 100 troy oz
    min_lot=0.01, max_lot=50.0, lot_step=0.01,
    tick_size=0.01, pip_size=0.01, price_decimals=2,
    base_price=2400.0, sim_drift=1.0,
    spread=0.30,            # $0.30/oz = $30/lot round trip
    commission_pct=0.0, max_leverage=100,
    finnhub_symbol="OANDA:XAU_USD", enabled=False,
))

_reg(SymbolSpec(
    symbol="XAG/USD", asset_class="metal",
    contract_size=5000.0,   # 1 lot = 5000 troy oz
    min_lot=0.01, max_lot=50.0, lot_step=0.01,
    tick_size=0.001, pip_size=0.001, price_decimals=3,
    base_price=30.0, sim_drift=0.02,
    spread=0.03, commission_pct=0.0, max_leverage=100,
    enabled=False,
))

# ─── Indices ──────────────────────────────────────────────────────────────────

_reg(SymbolSpec(
    symbol="US30", asset_class="index",
    contract_size=1.0,      # 1 point = $1 per contract
    min_lot=0.1, max_lot=100.0, lot_step=0.1,
    tick_size=1.0, pip_size=1.0, price_decimals=0,
    base_price=40000.0, sim_drift=50.0,
    spread=3.0,             # 3-point spread — realistic Dow CFD
    commission_pct=0.0, max_leverage=20,
    enabled=False,
))

_reg(SymbolSpec(
    symbol="US500", asset_class="index",
    contract_size=10.0,     # $10 per point (S&P 500 E-mini equivalent)
    min_lot=0.1, max_lot=100.0, lot_step=0.1,
    tick_size=0.25, pip_size=0.25, price_decimals=2,
    base_price=5200.0, sim_drift=5.0,
    spread=0.50, commission_pct=0.0, max_leverage=20,
    enabled=False,
))

_reg(SymbolSpec(
    symbol="NAS100", asset_class="index",
    contract_size=1.0,
    min_lot=0.1, max_lot=100.0, lot_step=0.1,
    tick_size=0.25, pip_size=0.25, price_decimals=2,
    base_price=18000.0, sim_drift=20.0,
    spread=1.50, commission_pct=0.0, max_leverage=20,
    enabled=False,
))


# ─── Public API ───────────────────────────────────────────────────────────────

def get_spec(symbol: str) -> SymbolSpec:
    """Return spec for *symbol*. Raises KeyError with diagnostic message if unknown."""
    spec = _REGISTRY.get(symbol)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown symbol {symbol!r}. Registered: {known}")
    return spec


def get_all_specs() -> list[SymbolSpec]:
    """All registered specs (enabled and disabled)."""
    return list(_REGISTRY.values())


def allowed_symbols() -> frozenset[str]:
    """Symbols the backend accepts orders for (enabled=True)."""
    return frozenset(s for s, sp in _REGISTRY.items() if sp.enabled)


def kline_symbols() -> frozenset[str]:
    """Symbols that stream canonical OHLCV from an exchange (kline_source set and enabled)."""
    return frozenset(s for s, sp in _REGISTRY.items() if sp.kline_source and sp.enabled)
