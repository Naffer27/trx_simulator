# simulator/broker_exposure.py
"""
RISK-01 — single source of truth for the broker's LIVE open-position risk
and exposure. Companion to simulator/broker_pnl.py (BOOK-03, which covers
REALIZED broker economics from BrokerLedger); this module covers the
UNREALIZED side, read directly from currently-open Position rows.

── FASE 1 audit — prior art and inconsistencies found ──────────────────
Before this module, exposure/notional was computed in FOUR different
places, two of them with a real bug:

  1. exposure_engine.py::compute_live_analytics() — notional =
     qty * price * contract_size (CORRECT), price from FeedManager/REST/
     base_price cascading fallback (silent — never reports "no price").
  2. broker_monitoring.py::symbol_exposure()/net_broker_exposure() —
     notional = qty * price, MISSING contract_size. For any instrument
     with contract_size != 1 (every FX pair here is 100000) this
     understates notional by orders of magnitude. Uses avg_price (entry),
     never a current price.
  3. admin.py::dealing_desk_view — qty-only (long_qty/short_qty/net_qty
     per symbol), no notional, no price, no broker PnL at all.
  4. snapshots.py::_position_data() (feeds take_snapshots_task ->
     BrokerEquitySnapshot.gross_long_usd/gross_short_usd/net_exposure_usd,
     which the Broker Control Center's "ops" KPIs read) — notional =
     qty * price, ALSO missing contract_size, despite a comment claiming
     "per exposure_engine convention" (it is not). net_exposure_usd there
     is abs(gross_long - gross_short) — unsigned, a third convention
     versus exposure_engine's signed total_long - total_short.

None of the four exposes pricing coverage (whether the "current price"
used was actually fresh, or a stale/fallback value). None of the four is
touched or altered by this module — RISK-01 is additive: it creates the
correct, fully-labeled engine and (FASE 8) wires NEW, separately-named
fields into broker_monitoring.py and the Broker Control Center, without
changing what those old fields/snapshot rows already compute. The
snapshots.py contract_size bug is a real, pre-existing defect but rewriting
a scheduled task that writes historical time-series rows is a bigger,
separately-scoped change — flagged in the RISK-01 ENTREGA as a follow-up,
not fixed here.

── FASE 2 definitions ───────────────────────────────────────────────────
    signed_quantity   = +qty for BUY, -qty for SELL
    gross_quantity    = sum(abs(qty)) across all open positions in scope
    net_quantity      = sum(signed_quantity)
    long_quantity     = sum(qty where side == BUY)
    short_quantity    = sum(qty where side == SELL)   (reported positive)
    notional_exposure = qty * reference_price * contract_size
    gross_notional    = sum(abs(notional_exposure))
    net_notional      = sum(signed notional_exposure)
    trader_unrealized_pnl            = sum of each position's own
                                        pnl_engine-computed unrealized PnL
                                        (account-currency, Decimal)
    broker_unrealized_counterparty_pnl = -trader_unrealized_pnl
        (positive = broker gains, negative = broker loses — same
        perspective convention as BOOK-03's broker_pnl.py)

Never mixed together: notional, margin_used, trader equity, broker PnL,
and lot size (qty) are always separate fields, never folded into a single
ambiguous number.

── FASE 4 — reference price policy (explicit, no silent fallback) ──────
The reference price for notional/unrealized-PnL purposes is the FeedManager
in-process mid price (`get_feed_manager().last_price(symbol)`), used ONLY
if `has_price(symbol)` is True (fresh within the shared cache TTL — the
same PANEL-02 freshness gate already used to guard real order-open
decisions; no new threshold invented here). If a symbol has no fresh
price, every open position on that symbol is counted in
`unpriced_position_count` and EXCLUDED from notional/PnL sums — never
priced from avg_price (entry) or any other fallback. This is a stricter
policy than exposure_engine.py's cascading fallback (finding #1 above);
existing callers of exposure_engine are unaffected (untouched).
This module does not use bid/ask differentiated by side — one reference
mid price per symbol, same simplification exposure_engine.py already
makes (real execution price differentiation belongs to
consumers.py:exec_price(), not a portfolio-level exposure aggregator).

── FASE 3 source of truth ───────────────────────────────────────────────
Position (currently open only) is the sole input for quantity/notional/
unrealized PnL. margin_used mirrors consumers.py::_margin_used_total()'s
exact formula (entry avg_price, per-position leverage cap) — read-only
here, the real margin calculation in consumers.py is not touched.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from .models import Position, TradingAccount

_ZERO = Decimal("0")


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def signed_quantity(side: str, qty: Decimal) -> Decimal:
    """BUY -> +qty, SELL -> -qty. Pure, no I/O."""
    qty = _d(qty)
    return qty if str(side).upper() == "BUY" else -qty


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — breakdown contracts
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class SymbolExposureBreakdown:
    symbol: str
    position_count: int = 0
    long_quantity: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    gross_quantity: Decimal = _ZERO
    net_quantity: Decimal = _ZERO
    long_notional: Decimal = _ZERO
    short_notional: Decimal = _ZERO
    gross_notional: Decimal = _ZERO
    net_notional: Decimal = _ZERO
    trader_unrealized_pnl: Decimal = _ZERO
    broker_unrealized_counterparty_pnl: Decimal = _ZERO
    priced_position_count: int = 0
    unpriced_position_count: int = 0
    concentration_pct: Decimal = _ZERO


@dataclass
class AccountExposureBreakdown:
    account_id: int
    position_count: int = 0
    long_quantity: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    gross_quantity: Decimal = _ZERO
    net_quantity: Decimal = _ZERO
    long_notional: Decimal = _ZERO
    short_notional: Decimal = _ZERO
    gross_notional: Decimal = _ZERO
    net_notional: Decimal = _ZERO
    trader_unrealized_pnl: Decimal = _ZERO
    broker_unrealized_counterparty_pnl: Decimal = _ZERO
    margin_used: Decimal = _ZERO
    priced_position_count: int = 0
    unpriced_position_count: int = 0


@dataclass
class BrokerExposureBreakdown:
    """Perspective: positive broker_unrealized_counterparty_pnl means the
    broker is currently ahead (traders collectively underwater); negative
    means the broker is currently behind. Mirrors BOOK-03's broker_pnl.py
    sign convention exactly (never inverted between modules — see FASE 7)."""
    open_position_count: int = 0
    account_count: int = 0
    symbol_count: int = 0

    long_quantity: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    gross_quantity: Decimal = _ZERO
    net_quantity: Decimal = _ZERO

    long_notional: Decimal = _ZERO
    short_notional: Decimal = _ZERO
    gross_notional: Decimal = _ZERO
    net_notional: Decimal = _ZERO

    trader_unrealized_pnl: Decimal = _ZERO
    broker_unrealized_counterparty_pnl: Decimal = _ZERO
    margin_used: Decimal = _ZERO

    concentration_by_symbol: dict = field(default_factory=dict)   # {symbol: Decimal pct}
    largest_symbol: "str | None" = None
    largest_symbol_gross_notional: Decimal = _ZERO
    top_symbols: list = field(default_factory=list)               # [(symbol, gross_notional), ...] desc

    # FASE 4 — pricing coverage (never silently fabricated)
    priced_position_count: int = 0
    unpriced_position_count: int = 0
    pricing_coverage_pct: Decimal = Decimal("100.00")
    stale_or_missing_symbols: list = field(default_factory=list)

    # Per-symbol / per-account detail
    by_symbol: dict = field(default_factory=dict)     # {symbol: SymbolExposureBreakdown}
    by_account: dict = field(default_factory=dict)    # {account_id: AccountExposureBreakdown}

    # Filters actually applied — echoed back so a caller can't mistake a
    # filtered breakdown for the whole book.
    account_id: "int | None" = None
    account_ids: "list | None" = None
    symbol: "str | None" = None
    account_type: "str | None" = None
    status: "str | None" = None
    trader_class: "str | None" = None

    generated_at: "datetime | None" = None


# ─────────────────────────────────────────────────────────────────────────
# Price lookup — explicit coverage, never a silent fallback (FASE 4)
# ─────────────────────────────────────────────────────────────────────────
def _price_lookup_for_symbols(symbols: set) -> dict:
    """
    Returns {symbol: Decimal(mid_price)} ONLY for symbols with a fresh
    price (FeedManager.has_price, same TTL PANEL-02 already uses for
    order-open safety). Symbols without a fresh price are simply absent
    from the returned dict — callers must treat a missing key as
    "unpriced", never substitute anything.
    """
    from market_data.feeds import get_feed_manager
    fm = get_feed_manager()
    prices = {}
    for sym in symbols:
        if fm.has_price(sym):
            prices[sym] = _d(fm.last_price(sym))
    return prices


def _spec_cache_for_symbols(symbols: set) -> dict:
    from market_data.symbol_specs import get_spec
    cache = {}
    for sym in symbols:
        try:
            cache[sym] = get_spec(sym)
        except KeyError:
            cache[sym] = None
    return cache


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — core aggregation
# ─────────────────────────────────────────────────────────────────────────
def calculate_broker_exposure(
    *,
    account_id: "int | None" = None,
    account_ids: "list | None" = None,
    symbol: "str | None" = None,
    account_type: "str | None" = None,
    status: "str | None" = None,
    trader_class: "str | None" = None,
) -> BrokerExposureBreakdown:
    """
    The one aggregation function — every broker_exposure_for_* helper
    below is a thin wrapper over this. Single DB query for positions
    (select_related account — FASE 10, no N+1), everything else is
    pure Python/Decimal over the fetched rows plus one FeedManager call
    per DISTINCT symbol (never per position).
    """
    from . import pnl_engine

    # FASE 5 — filters. book_mode/routing is deliberately NOT a filter
    # here: BOOK-01 established no book_mode field is ever persisted on
    # Position/Trade, and this module does not invent one for old or new
    # positions (see module docstring / BOOK-01 audit).
    qs = Position.objects.select_related("account", "account__trader_score").all()
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)
    if symbol is not None:
        qs = qs.filter(symbol=symbol)
    if account_type is not None:
        qs = qs.filter(account__account_type=account_type)
    if status is not None:
        qs = qs.filter(account__status=status)
    if trader_class is not None:
        qs = qs.filter(account__trader_score__trader_class=trader_class)

    positions = list(qs)

    symbols = {p.symbol for p in positions}
    prices = _price_lookup_for_symbols(symbols)
    specs = _spec_cache_for_symbols(symbols)

    by_symbol: dict[str, SymbolExposureBreakdown] = {}
    by_account: dict[int, AccountExposureBreakdown] = {}

    total_long_qty = total_short_qty = _ZERO
    total_long_notional = total_short_notional = _ZERO
    total_trader_upnl = _ZERO
    total_margin = _ZERO
    priced_count = unpriced_count = 0
    stale_symbols = set()

    for pos in positions:
        sym = pos.symbol
        spec = specs.get(sym)
        contract_size = _d(spec.contract_size) if spec is not None else Decimal("1")
        qty = _d(pos.qty)
        avg_price = _d(pos.avg_price)
        side = pos.side
        s_qty = signed_quantity(side, qty)

        if sym not in by_symbol:
            by_symbol[sym] = SymbolExposureBreakdown(symbol=sym)
        sb = by_symbol[sym]

        acc = pos.account
        if acc.id not in by_account:
            by_account[acc.id] = AccountExposureBreakdown(account_id=acc.id)
        ab = by_account[acc.id]

        sb.position_count += 1
        ab.position_count += 1

        if side == "BUY":
            sb.long_quantity += qty
            ab.long_quantity += qty
            total_long_qty += qty
        else:
            sb.short_quantity += qty
            ab.short_quantity += qty
            total_short_qty += qty
        sb.gross_quantity += qty
        ab.gross_quantity += qty
        sb.net_quantity += s_qty
        ab.net_quantity += s_qty

        # ── margin_used — mirrors consumers.py::_margin_used_total()
        # exactly (entry avg_price, per-position leverage cap). Read-only
        # reproduction of the real formula, not a new margin policy.
        account_lev = max(1, int(acc.leverage or 50))
        pos_lev = max(1, min(account_lev, int(spec.max_leverage))) if spec is not None else account_lev
        pos_margin = abs(avg_price * qty * contract_size) / Decimal(pos_lev)
        total_margin += pos_margin
        ab.margin_used += pos_margin

        # ── notional + unrealized PnL — ONLY if this symbol has a fresh price.
        price = prices.get(sym)
        if price is None:
            unpriced_count += 1
            sb.unpriced_position_count += 1
            ab.unpriced_position_count += 1
            stale_symbols.add(sym)
            continue

        priced_count += 1
        sb.priced_position_count += 1
        ab.priced_position_count += 1

        notional = qty * price * contract_size
        signed_notional = notional if side == "BUY" else -notional
        if side == "BUY":
            sb.long_notional += notional
            ab.long_notional += notional
            total_long_notional += notional
        else:
            sb.short_notional += notional
            ab.short_notional += notional
            total_short_notional += notional
        sb.gross_notional += notional
        ab.gross_notional += notional
        sb.net_notional += signed_notional
        ab.net_notional += signed_notional

        account_currency = getattr(acc, "currency", "USD") or "USD"
        pnl_result = pnl_engine.calculate_position_pnl(
            side, avg_price, price, qty, sym, account_currency=account_currency,
        )
        upnl = pnl_result.pnl_account if pnl_result.pnl_account is not None else _ZERO
        sb.trader_unrealized_pnl += upnl
        ab.trader_unrealized_pnl += upnl
        total_trader_upnl += upnl

    # ── finalize per-symbol/per-account broker-perspective PnL + concentration ──
    total_gross_notional = total_long_notional + total_short_notional
    for sb in by_symbol.values():
        sb.broker_unrealized_counterparty_pnl = -sb.trader_unrealized_pnl
        sb.concentration_pct = (
            (sb.gross_notional / total_gross_notional * Decimal("100"))
            if total_gross_notional > 0 else _ZERO
        )
    for ab in by_account.values():
        ab.broker_unrealized_counterparty_pnl = -ab.trader_unrealized_pnl

    top_symbols = sorted(
        ((s, sb.gross_notional) for s, sb in by_symbol.items()),
        key=lambda t: t[1], reverse=True,
    )
    largest_symbol, largest_gross = (top_symbols[0] if top_symbols else (None, _ZERO))

    open_position_count = len(positions)
    pricing_coverage_pct = (
        Decimal("100.00") if open_position_count == 0
        else (Decimal(priced_count) / Decimal(open_position_count) * Decimal("100")).quantize(Decimal("0.01"))
    )

    return BrokerExposureBreakdown(
        open_position_count=open_position_count,
        account_count=len(by_account),
        symbol_count=len(by_symbol),
        long_quantity=total_long_qty, short_quantity=total_short_qty,
        gross_quantity=total_long_qty + total_short_qty,
        net_quantity=total_long_qty - total_short_qty,
        long_notional=total_long_notional, short_notional=total_short_notional,
        gross_notional=total_gross_notional,
        net_notional=total_long_notional - total_short_notional,
        trader_unrealized_pnl=total_trader_upnl,
        broker_unrealized_counterparty_pnl=-total_trader_upnl,
        margin_used=total_margin,
        concentration_by_symbol={s: sb.concentration_pct for s, sb in by_symbol.items()},
        largest_symbol=largest_symbol,
        largest_symbol_gross_notional=largest_gross,
        top_symbols=top_symbols,
        priced_position_count=priced_count,
        unpriced_position_count=unpriced_count,
        pricing_coverage_pct=pricing_coverage_pct,
        stale_or_missing_symbols=sorted(stale_symbols),
        by_symbol=by_symbol,
        by_account=by_account,
        account_id=account_id, account_ids=account_ids, symbol=symbol,
        account_type=account_type, status=status, trader_class=trader_class,
        generated_at=timezone.now(),
    )


# ─────────────────────────────────────────────────────────────────────────
# Convenience wrappers (FASE 3's named contracts)
# ─────────────────────────────────────────────────────────────────────────
def broker_exposure_for_symbol(symbol: str) -> BrokerExposureBreakdown:
    return calculate_broker_exposure(symbol=symbol)


def broker_exposure_for_account(account_id: int) -> BrokerExposureBreakdown:
    return calculate_broker_exposure(account_id=account_id)


def broker_exposure_for_accounts(account_ids: list) -> BrokerExposureBreakdown:
    return calculate_broker_exposure(account_ids=account_ids)


def broker_exposure_snapshot() -> BrokerExposureBreakdown:
    """Whole-book snapshot, no filters — the top-level dashboard figure."""
    return calculate_broker_exposure()
