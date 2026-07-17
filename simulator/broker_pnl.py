# simulator/broker_pnl.py
"""
BOOK-03 — single, unified engine for the broker's economic result.

BOOK-01 found several partial, mutually-inconsistent implementations of
"broker PnL" scattered across admin.py, exposure_engine.py, and
broker_monitoring.py — different filters, different signs, one of them
(admin.py's Broker Control Center) silently omitting the realized
counterparty result altogether. BOOK-02 gave the system a real, persisted
accounting fact for that missing piece (BrokerLedger.COUNTERPARTY_PNL,
amount = -Trade.profit_loss, source_trade linked). This module is the one
place that reads BrokerLedger and produces a correct, fully-labeled
breakdown — every other view/task should call into it rather than
re-deriving these numbers with its own ad hoc query.

── Accounting definitions (FASE 1) ─────────────────────────────────────
    fee_revenue        = COMMISSION + SPREAD + CHALLENGE_FEE + WITHDRAW_FEE
                          (non-directional revenue; always >= 0 per entry,
                          the sum itself is therefore always >= 0)
    counterparty_pnl    = sum(BrokerLedger.COUNTERPARTY_PNL.amount)
                          (directional B-Book result; can be negative)
    adjustments         = sum(BrokerLedger.ADJUSTMENT.amount)
                          (real sign, whatever the adjustment recorded)
    broker_net_pnl      = fee_revenue + counterparty_pnl + adjustments

Aliases used across this module and its callers (FASE 1's "separar
además"):
    gross_revenue  == fee_revenue
    directional_pnl == counterparty_pnl
    net_pnl        == broker_net_pnl

Sign convention (FASE 2 — must never invert silently): POSITIVE means the
broker gains; NEGATIVE means the broker loses. fee_revenue is always >= 0.
counterparty_pnl and adjustments carry their true sign as stored.

── Source of truth (FASE 3) ────────────────────────────────────────────
BrokerLedger is authoritative. counterparty_pnl is NEVER recomputed from
Trade.profit_loss when a COUNTERPARTY_PNL row already exists for that
Trade — this module only reads BrokerLedger.amount. For Trades closed
before BOOK-02 (no linked entry), this module does NOT invent a number;
it reports them as missing via the coverage breakdown (FASE 8) and
excludes them from counterparty_pnl entirely — an incomplete-coverage
period's counterparty_pnl is a real (if partial) sum of what BrokerLedger
actually has, never silently topped up.

── Decimal-only ────────────────────────────────────────────────────────
All monetary arithmetic in this module uses Decimal. Nothing here casts
to float internally; conversions to float, if any, are the caller's
choice at the JSON-serialization boundary (admin views already do this).
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from .models import BrokerLedger, Trade

_ZERO = Decimal("0.00")

FEE_REVENUE_TYPES = (
    BrokerLedger.REV_COMMISSION,
    BrokerLedger.REV_SPREAD,
    BrokerLedger.REV_CHALLENGE_FEE,
    BrokerLedger.REV_WITHDRAW_FEE,
)


# ─────────────────────────────────────────────────────────────────────────
# FASE 4 — one shared UTC period-window builder
# ─────────────────────────────────────────────────────────────────────────
PERIOD_TODAY     = "today"
PERIOD_LAST_24H  = "last_24h"
PERIOD_WEEK      = "week"
PERIOD_MONTH     = "month"
PERIOD_LIFETIME  = "lifetime"
PERIOD_CUSTOM    = "custom"

_KNOWN_PERIODS = frozenset({
    PERIOD_TODAY, PERIOD_LAST_24H, PERIOD_WEEK, PERIOD_MONTH, PERIOD_LIFETIME, PERIOD_CUSTOM,
})


def utc_period_window(period: str, *, now=None, start=None, end=None) -> tuple[datetime | None, datetime | None]:
    """
    Return (start, end) as tz-aware UTC datetimes for a named period.
    end is always None (open-ended, "up to now") except for PERIOD_CUSTOM.
    start is None for PERIOD_LIFETIME (no lower bound).

    - today:     UTC midnight of the current day -> now
    - last_24h:  now - 24h -> now
    - week:      UTC midnight, Monday of the current week -> now
    - month:     UTC midnight, day 1 of the current month -> now
    - lifetime:  no bound at all -> (None, None)
    - custom:    caller-supplied (start, end), both optional

    This is the ONLY place period boundaries are computed — admin.py's
    three dashboards and broker_monitoring.py previously each rolled
    their own (see FASE 4); they now all call this.
    """
    if period not in _KNOWN_PERIODS:
        raise ValueError(f"unknown period: {period!r}")

    if period == PERIOD_CUSTOM:
        return start, end

    _now = now or timezone.now()

    if period == PERIOD_LIFETIME:
        return None, None

    if period == PERIOD_LAST_24H:
        return _now - timedelta(hours=24), None

    if period == PERIOD_TODAY:
        midnight = datetime.combine(_now.date(), dt_time.min, tzinfo=_now.tzinfo)
        return midnight, None

    if period == PERIOD_WEEK:
        monday = _now.date() - timedelta(days=_now.weekday())
        midnight = datetime.combine(monday, dt_time.min, tzinfo=_now.tzinfo)
        return midnight, None

    if period == PERIOD_MONTH:
        first_of_month = _now.date().replace(day=1)
        midnight = datetime.combine(first_of_month, dt_time.min, tzinfo=_now.tzinfo)
        return midnight, None

    raise AssertionError("unreachable")  # pragma: no cover


# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — breakdown contract
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class BrokerPnLBreakdown:
    """
    Positive = broker gains. Negative = broker loses. fee_revenue is
    always >= 0 (it is a sum of non-directional revenue entries, each
    individually >= 0); counterparty_pnl and adjustments carry their real
    sign as stored in BrokerLedger.
    """
    commission: Decimal = _ZERO
    spread: Decimal = _ZERO
    challenge_fee: Decimal = _ZERO
    withdraw_fee: Decimal = _ZERO
    fee_revenue: Decimal = _ZERO          # == gross_revenue
    counterparty_pnl: Decimal = _ZERO     # == directional_pnl
    adjustments: Decimal = _ZERO
    broker_net_pnl: Decimal = _ZERO       # == net_pnl == fee_revenue + counterparty_pnl + adjustments

    # FASE 8 — coverage (only meaningful where counterparty_pnl was computed
    # from a Trade population, i.e. broker_pnl_for_period/account/symbol;
    # left at defaults (0/0/100.0/False) for broker_pnl_for_trade, which is
    # single-Trade and reports has_counterparty_entry instead).
    closed_trade_count: int = 0
    counterpart_entry_count: int = 0
    missing_counterpart_count: int = 0
    coverage_pct: float = 100.0
    first_book02_trade_at: "datetime | None" = None
    historical_incomplete: bool = False

    # Filters actually applied — echoed back so a caller/report can't
    # mistake a filtered breakdown for the unfiltered whole.
    period: str | None = None
    period_start: "datetime | None" = None
    period_end: "datetime | None" = None
    account_id: int | None = None
    symbol: str | None = None

    @property
    def gross_revenue(self) -> Decimal:
        return self.fee_revenue

    @property
    def directional_pnl(self) -> Decimal:
        return self.counterparty_pnl

    @property
    def net_pnl(self) -> Decimal:
        return self.broker_net_pnl


def _sum(qs, revenue_type) -> Decimal:
    v = qs.filter(revenue_type=revenue_type).aggregate(t=Sum("amount"))["t"]
    return v if v is not None else _ZERO


# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — core aggregation
# ─────────────────────────────────────────────────────────────────────────
def calculate_broker_pnl(
    *,
    period: str = PERIOD_LIFETIME,
    start=None,
    end=None,
    account_id: int | None = None,
    symbol: str | None = None,
    now=None,
) -> BrokerPnLBreakdown:
    """
    The one aggregation function. Every broker_pnl_for_* helper below is a
    thin wrapper over this. Reads BrokerLedger only (FASE 3) — never
    Trade.profit_loss directly for counterparty_pnl.

    Coverage (closed_trade_count / counterpart_entry_count / coverage_pct)
    is computed over Trade rows in the SAME window/filters, comparing
    against how many of them have a linked COUNTERPARTY_PNL BrokerLedger
    row — this is what lets a caller know whether counterparty_pnl for
    this window is a complete sum or a partial one (pre-BOOK-02 history).
    """
    p_start, p_end = utc_period_window(period, now=now, start=start, end=end)

    ledger_qs = BrokerLedger.objects.all()
    if p_start is not None:
        ledger_qs = ledger_qs.filter(created_at__gte=p_start)
    if p_end is not None:
        ledger_qs = ledger_qs.filter(created_at__lte=p_end)
    if account_id is not None:
        ledger_qs = ledger_qs.filter(source_account_id=account_id)
    if symbol is not None:
        ledger_qs = ledger_qs.filter(symbol=symbol)

    commission    = _sum(ledger_qs, BrokerLedger.REV_COMMISSION)
    spread        = _sum(ledger_qs, BrokerLedger.REV_SPREAD)
    challenge_fee = _sum(ledger_qs, BrokerLedger.REV_CHALLENGE_FEE)
    withdraw_fee  = _sum(ledger_qs, BrokerLedger.REV_WITHDRAW_FEE)
    fee_revenue   = commission + spread + challenge_fee + withdraw_fee
    counterparty_pnl = _sum(ledger_qs, BrokerLedger.REV_COUNTERPARTY_PNL)
    adjustments   = _sum(ledger_qs, BrokerLedger.REV_ADJUSTMENT)
    broker_net_pnl = fee_revenue + counterparty_pnl + adjustments

    # ── Coverage — same window/filters, but over Trade (the population
    # counterparty_pnl SHOULD cover), not over BrokerLedger itself.
    trade_qs = Trade.objects.filter(closed_at__isnull=False)
    if p_start is not None:
        trade_qs = trade_qs.filter(closed_at__gte=p_start)
    if p_end is not None:
        trade_qs = trade_qs.filter(closed_at__lte=p_end)
    if account_id is not None:
        trade_qs = trade_qs.filter(account_id=account_id)
    if symbol is not None:
        trade_qs = trade_qs.filter(symbol=symbol)

    closed_trade_count = trade_qs.count()
    counterpart_entry_count = trade_qs.filter(
        broker_ledger__revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
    ).distinct().count()
    missing_counterpart_count = closed_trade_count - counterpart_entry_count
    coverage_pct = 100.0 if closed_trade_count == 0 else round(
        counterpart_entry_count / closed_trade_count * 100.0, 2,
    )

    first_book02_trade_at = None
    first_cp_row = (
        BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        .order_by("created_at").first()
    )
    if first_cp_row is not None:
        first_book02_trade_at = first_cp_row.created_at

    return BrokerPnLBreakdown(
        commission=commission, spread=spread, challenge_fee=challenge_fee, withdraw_fee=withdraw_fee,
        fee_revenue=fee_revenue, counterparty_pnl=counterparty_pnl, adjustments=adjustments,
        broker_net_pnl=broker_net_pnl,
        closed_trade_count=closed_trade_count,
        counterpart_entry_count=counterpart_entry_count,
        missing_counterpart_count=missing_counterpart_count,
        coverage_pct=coverage_pct,
        first_book02_trade_at=first_book02_trade_at,
        historical_incomplete=missing_counterpart_count > 0,
        period=period, period_start=p_start, period_end=p_end,
        account_id=account_id, symbol=symbol,
    )


# ─────────────────────────────────────────────────────────────────────────
# Convenience wrappers (FASE 2's named contracts)
# ─────────────────────────────────────────────────────────────────────────
def broker_pnl_for_period(period: str = PERIOD_LIFETIME, *, start=None, end=None, now=None) -> BrokerPnLBreakdown:
    return calculate_broker_pnl(period=period, start=start, end=end, now=now)


def broker_pnl_for_account(account_id: int, *, period: str = PERIOD_LIFETIME, start=None, end=None, now=None) -> BrokerPnLBreakdown:
    return calculate_broker_pnl(period=period, start=start, end=end, account_id=account_id, now=now)


def broker_pnl_for_symbol(symbol: str, *, period: str = PERIOD_LIFETIME, start=None, end=None, now=None) -> BrokerPnLBreakdown:
    return calculate_broker_pnl(period=period, start=start, end=end, symbol=symbol, now=now)


@dataclass
class TradeBrokerPnL:
    """
    FASE 7 — per-trade result. Fee rows (commission/spread) are NOT
    attributed to a Trade even when meta correlates them (e.g. same
    db_pos_id) — they are created at OPEN time with source_trade=None,
    before any Trade exists, and BOOK-03 does not invent that association
    (see the module docstring's "Source of truth" section and FASE 7's
    explicit instruction not to). linked_fee_rows lists only BrokerLedger
    rows that DO carry source_trade == this trade (none do today).
    """
    trade_id: int
    counterparty_pnl: "Decimal | None"
    has_counterparty_entry: bool
    linked_fee_rows: list = field(default_factory=list)


def broker_pnl_for_trade(trade) -> TradeBrokerPnL:
    """
    Reads the linked COUNTERPARTY_PNL row for `trade`, if any — never
    recomputes it from trade.profit_loss (FASE 3/7). If no row exists
    (pre-BOOK-02 historical Trade), counterparty_pnl is None and
    has_counterparty_entry is False — not a silently-estimated 0 or
    -trade.profit_loss.
    """
    cp_row = BrokerLedger.objects.filter(
        source_trade=trade, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
    ).first()
    fee_rows = list(
        BrokerLedger.objects.filter(source_trade=trade, revenue_type__in=FEE_REVENUE_TYPES)
    )
    return TradeBrokerPnL(
        trade_id=trade.id,
        counterparty_pnl=cp_row.amount if cp_row is not None else None,
        has_counterparty_entry=cp_row is not None,
        linked_fee_rows=fee_rows,
    )
