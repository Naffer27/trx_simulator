"""
simulator/broker_monitoring.py
READ-ONLY broker intelligence and operational risk monitoring.

Rules (enforced by code review, not just by convention):
- No DB writes, no model mutations, no balance changes
- No automated liquidations, suspensions, or financial actions
- Pure aggregation and threshold detection for staff dashboards
- All public functions return JSON-serialisable dicts/lists
"""
import logging
import time as _t
from decimal import Decimal
from datetime import timedelta

from django.db.models import Sum, Count, Q, F, ExpressionWrapper, DecimalField as DjDecimal
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    TradingAccount, Position, LedgerEntry, Trade,
    Deposit, WithdrawalRequest,
    MARGIN_ENGINE_TYPES, DD_ENGINE_TYPES,
)

log = logging.getLogger("simulator.monitoring")

# ── Operational alert thresholds ───────────────────────────────────────────────
MARGIN_WARN_PCT      = 200.0   # margin level (equity/margin_used %) — warn
MARGIN_CRIT_PCT      = 130.0   # margin level — critical (approaching stopout at 100%)
EQUITY_LOSS_WARN_PCT =  10.0   # equity loss vs initial — warn
EQUITY_LOSS_CRIT_PCT =  25.0   # equity loss vs initial — critical
CONC_WARN_PCT        =  25.0   # single-symbol net exposure > X% of total — concentration warning

# Notional: qty * price for all symbols (confirmed by exposure_engine.py convention)
_ZERO = Decimal("0")


def _f(v) -> float:
    """Safe float conversion for JSON output."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _notional(qty: Decimal, price: Decimal) -> Decimal:
    """USD notional — consistent with exposure_engine convention."""
    return qty * price


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exposure by symbol
# ─────────────────────────────────────────────────────────────────────────────
def symbol_exposure() -> list[dict]:
    """
    Per-symbol gross and net exposure across ALL open positions.
    net_qty > 0  →  traders net LONG  (broker net short)
    net_qty < 0  →  traders net SHORT (broker net long)
    Uses avg_price as proxy for current notional (no live feed call).
    """
    rows = list(
        Position.objects.values("symbol", "side", "qty", "avg_price")
    )

    by_symbol: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {
                "symbol":        sym,
                "buy_qty":       _ZERO,
                "sell_qty":      _ZERO,
                "buy_count":     0,
                "sell_count":    0,
                "buy_notional":  _ZERO,
                "sell_notional": _ZERO,
            }
        qty   = Decimal(str(r["qty"]))
        price = Decimal(str(r["avg_price"]))
        n     = _notional(qty, price)
        d     = by_symbol[sym]
        if r["side"] == "BUY":
            d["buy_qty"]      += qty
            d["buy_count"]    += 1
            d["buy_notional"] += n
        else:
            d["sell_qty"]      += qty
            d["sell_count"]    += 1
            d["sell_notional"] += n

    result = []
    for sym, d in by_symbol.items():
        net_qty      = d["buy_qty"]      - d["sell_qty"]
        net_notional = d["buy_notional"] - d["sell_notional"]
        result.append({
            "symbol":            sym,
            "buy_qty":           _f(d["buy_qty"]),
            "sell_qty":          _f(d["sell_qty"]),
            "buy_count":         d["buy_count"],
            "sell_count":        d["sell_count"],
            "total_positions":   d["buy_count"] + d["sell_count"],
            "net_qty":           _f(net_qty),
            "net_direction":     "LONG" if net_qty > 0 else ("SHORT" if net_qty < 0 else "FLAT"),
            "buy_notional_usd":  _f(d["buy_notional"]),
            "sell_notional_usd": _f(d["sell_notional"]),
            "net_notional_usd":  _f(net_notional),
        })

    result.sort(key=lambda x: abs(x["net_notional_usd"]), reverse=True)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. Net broker exposure (aggregate)
# ─────────────────────────────────────────────────────────────────────────────
def net_broker_exposure() -> dict:
    """
    Aggregate across all symbols. Logs a warning if any symbol exceeds
    the concentration threshold.
    """
    exposures = symbol_exposure()
    total_net_abs = sum(abs(e["net_notional_usd"]) for e in exposures)

    for e in exposures:
        pct = abs(e["net_notional_usd"]) / total_net_abs * 100 if total_net_abs else 0
        e["concentration_pct"] = round(pct, 1)
        if pct > CONC_WARN_PCT:
            log.warning(
                "[exposure.concentration] symbol=%s net_pct=%.1f%% net_notional=%.2f",
                e["symbol"], pct, e["net_notional_usd"],
            )

    return {
        "total_open_positions": sum(e["total_positions"] for e in exposures),
        "gross_long_notional":  round(sum(e["buy_notional_usd"]  for e in exposures), 2),
        "gross_short_notional": round(sum(e["sell_notional_usd"] for e in exposures), 2),
        "total_net_abs_notional": round(total_net_abs, 2),
        "symbol_count":         len(exposures),
        "by_symbol":            exposures,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Risk concentration by trader
# ─────────────────────────────────────────────────────────────────────────────
def trader_risk_concentration(top_n: int = 20) -> list[dict]:
    """
    Ranks active accounts by equity loss %.
    Emits operational WARNING/INFO logs for threshold breaches.
    Returns top_n sorted by highest equity loss.
    """
    accounts = list(
        TradingAccount.objects
        .filter(status="Activo", initial_balance__gt=0)
        .select_related("user")
        .values(
            "id", "user__username", "account_type", "tier", "phase",
            "balance", "equity", "initial_balance", "leverage",
        )
    )

    flagged = []
    for acc in accounts:
        initial = _f(acc["initial_balance"])
        equity  = _f(acc["equity"])
        balance = _f(acc["balance"])
        if initial <= 0:
            continue

        equity_loss_pct = max(0.0, (initial - equity) / initial * 100)
        unrealized_pnl  = equity - balance
        risk_level      = "ok"
        if equity_loss_pct >= EQUITY_LOSS_CRIT_PCT:
            risk_level = "critical"
        elif equity_loss_pct >= EQUITY_LOSS_WARN_PCT:
            risk_level = "warning"

        if risk_level == "critical":
            log.warning(
                "[risk.concentration] CRITICAL account_id=%d user=%s "
                "equity_loss=%.1f%% equity=%.2f initial=%.2f",
                acc["id"], acc["user__username"], equity_loss_pct, equity, initial,
            )
        elif risk_level == "warning":
            log.info(
                "[risk.concentration] WARNING account_id=%d user=%s equity_loss=%.1f%%",
                acc["id"], acc["user__username"], equity_loss_pct,
            )

        flagged.append({
            "account_id":       acc["id"],
            "username":         acc["user__username"] or "—",
            "account_type":     acc["account_type"],
            "tier":             acc["tier"] or "",
            "phase":            acc["phase"] or "",
            "initial_balance":  round(initial, 2),
            "balance":          round(balance, 2),
            "equity":           round(equity, 2),
            "unrealized_pnl":   round(unrealized_pnl, 2),
            "equity_loss_pct":  round(equity_loss_pct, 2),
            "leverage":         acc["leverage"],
            "risk_level":       risk_level,
        })

    flagged.sort(key=lambda x: x["equity_loss_pct"], reverse=True)
    return flagged[:top_n]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Critical margin level accounts (RETAIL / ECN / STANDARD / DEMO / CRYPTO)
# ─────────────────────────────────────────────────────────────────────────────
def critical_margin_accounts() -> list[dict]:
    """
    Approximates margin_level = equity / (position_notional / leverage) × 100
    for MARGIN engine accounts that have open positions.
    Uses avg_price as proxy for current price.
    READ-ONLY — no liquidation, no mutation.
    """
    margin_types = list(MARGIN_ENGINE_TYPES)
    accounts = {
        a["id"]: a
        for a in TradingAccount.objects
        .filter(status="Activo", account_type__in=margin_types)
        .select_related("user")
        .values("id", "user__username", "account_type", "equity", "balance",
                "initial_balance", "leverage")
    }
    if not accounts:
        return []

    # Sum position notional per account
    notional_by_acc: dict[int, Decimal] = {}
    for pos in Position.objects.filter(account_id__in=list(accounts)).values(
        "account_id", "qty", "avg_price"
    ):
        aid = pos["account_id"]
        n   = _notional(Decimal(str(pos["qty"])), Decimal(str(pos["avg_price"])))
        notional_by_acc[aid] = notional_by_acc.get(aid, _ZERO) + n

    result = []
    for aid, acc in accounts.items():
        notional = notional_by_acc.get(aid, _ZERO)
        if notional <= 0:
            continue
        leverage            = acc["leverage"] or 50
        margin_used_approx  = notional / Decimal(str(leverage))
        equity              = Decimal(str(acc["equity"] or 0))
        if margin_used_approx <= 0:
            continue
        margin_level = float(equity / margin_used_approx * 100)

        risk_level = "ok"
        if margin_level <= MARGIN_CRIT_PCT:
            risk_level = "critical"
            log.warning(
                "[margin.level] CRITICAL account_id=%d user=%s margin_level=%.1f%%",
                aid, acc["user__username"], margin_level,
            )
        elif margin_level <= MARGIN_WARN_PCT:
            risk_level = "warning"
            log.info(
                "[margin.level] WARNING account_id=%d user=%s margin_level=%.1f%%",
                aid, acc["user__username"], margin_level,
            )

        result.append({
            "account_id":           aid,
            "username":             acc["user__username"] or "—",
            "account_type":         acc["account_type"],
            "equity":               round(_f(equity), 2),
            "notional_approx":      round(_f(notional), 2),
            "margin_used_approx":   round(_f(margin_used_approx), 2),
            "margin_level_approx":  round(margin_level, 1),
            "leverage":             leverage,
            "risk_level":           risk_level,
        })

    result.sort(key=lambda x: x["margin_level_approx"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 5. Broker PnL summary
# ─────────────────────────────────────────────────────────────────────────────
def broker_pnl_summary() -> dict:
    """
    Aggregated PnL and AUM metrics.
    Realized PnL sourced from LedgerEntry (REALIZED_PNL events).
    Unrealized PnL = Σ(equity - balance) across active accounts.
    """
    # Realized PnL — all time
    realized_all = (
        LedgerEntry.objects
        .filter(event_type=LedgerEntry.EV_REALIZED)
        .aggregate(total=Coalesce(Sum("amount"), _ZERO))
    )["total"]

    # Realized PnL — last 24h
    cutoff_24h = timezone.now() - timedelta(hours=24)
    realized_24h = (
        LedgerEntry.objects
        .filter(event_type=LedgerEntry.EV_REALIZED, created_at__gte=cutoff_24h)
        .aggregate(total=Coalesce(Sum("amount"), _ZERO))
    )["total"]

    # Realized PnL — last 7 days
    cutoff_7d = timezone.now() - timedelta(days=7)
    realized_7d = (
        LedgerEntry.objects
        .filter(event_type=LedgerEntry.EV_REALIZED, created_at__gte=cutoff_7d)
        .aggregate(total=Coalesce(Sum("amount"), _ZERO))
    )["total"]

    # Commission revenue
    commissions = (
        LedgerEntry.objects
        .filter(event_type=LedgerEntry.EV_COMMISSION)
        .aggregate(total=Coalesce(Sum("amount"), _ZERO))
    )["total"]

    # AUM from active accounts
    aum = TradingAccount.objects.filter(status="Activo").aggregate(
        count=Count("id"),
        total_balance=Coalesce(Sum("balance"),         _ZERO),
        total_equity=Coalesce(Sum("equity"),           _ZERO),
        total_initial=Coalesce(Sum("initial_balance"), _ZERO),
    )
    unrealized_pnl = _f(aum["total_equity"]) - _f(aum["total_balance"])

    # Deposit / withdrawal totals
    total_deposited = (
        Deposit.objects.filter(credited=True)
        .aggregate(total=Coalesce(Sum("amount_usd"), _ZERO))
    )["total"]
    total_withdrawn = (
        WithdrawalRequest.objects.filter(status=WithdrawalRequest.STATUS_COMPLETED)
        .aggregate(total=Coalesce(Sum("amount_usd"), _ZERO))
    )["total"]
    pending_deposits = (
        Deposit.objects.filter(credited=False)
        .aggregate(total=Coalesce(Sum("amount_usd"), _ZERO))
    )["total"]

    return {
        "realized_pnl_all_time": round(_f(realized_all),  2),
        "realized_pnl_7d":       round(_f(realized_7d),   2),
        "realized_pnl_24h":      round(_f(realized_24h),  2),
        "unrealized_pnl":        round(unrealized_pnl,    2),
        "commission_revenue":    round(_f(commissions),   2),
        "active_accounts":       aum["count"],
        "aum_balance_usd":       round(_f(aum["total_balance"]),  2),
        "aum_equity_usd":        round(_f(aum["total_equity"]),   2),
        "aum_initial_usd":       round(_f(aum["total_initial"]),  2),
        "total_deposited_usd":   round(_f(total_deposited), 2),
        "total_withdrawn_usd":   round(_f(total_withdrawn), 2),
        "pending_deposits_usd":  round(_f(pending_deposits), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Active position metrics
# ─────────────────────────────────────────────────────────────────────────────
def position_metrics() -> dict:
    """
    Summary of all open positions by side, account type, and symbol.
    """
    total = Position.objects.count()

    by_side = {
        r["side"]: {"count": r["count"], "total_qty": _f(r["total_qty"])}
        for r in Position.objects
        .values("side")
        .annotate(count=Count("id"), total_qty=Sum("qty"))
    }

    by_type = [
        {
            "account_type": r["account__account_type"],
            "count":        r["count"],
            "total_qty":    _f(r["total_qty"]),
        }
        for r in Position.objects
        .select_related("account")
        .values("account__account_type")
        .annotate(count=Count("id"), total_qty=Sum("qty"))
        .order_by("-count")
    ]

    top_symbols = [
        {
            "symbol":    r["symbol"],
            "count":     r["count"],
            "total_qty": _f(r["total_qty"]),
        }
        for r in Position.objects
        .values("symbol")
        .annotate(count=Count("id"), total_qty=Sum("qty"))
        .order_by("-count")[:10]
    ]

    return {
        "total_open":      total,
        "by_side":         by_side,
        "by_account_type": by_type,
        "top_symbols":     top_symbols,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Account operational summary
# ─────────────────────────────────────────────────────────────────────────────
def account_summary() -> dict:
    """
    Account counts by type and status. Active trader count (last 24h).
    """
    cutoff = timezone.now() - timedelta(hours=24)
    active_traders_24h = (
        Trade.objects
        .filter(opened_at__gte=cutoff)
        .values("account_id")
        .distinct()
        .count()
    )
    by_status = list(
        TradingAccount.objects
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )
    by_type = list(
        TradingAccount.objects
        .values("account_type", "status")
        .annotate(count=Count("id"))
        .order_by("account_type", "status")
    )
    total  = TradingAccount.objects.count()
    active = TradingAccount.objects.filter(status="Activo").count()

    # Violations / suspensions (recent)
    cutoff_48h = timezone.now() - timedelta(hours=48)
    recent_suspended = (
        LedgerEntry.objects
        .filter(event_type=LedgerEntry.EV_ADJUST, created_at__gte=cutoff_48h)
        .filter(meta__msg__icontains="suspendida")
        .count()
    )

    return {
        "total":               total,
        "active":              active,
        "active_traders_24h":  active_traders_24h,
        "recent_suspended_48h": recent_suspended,
        "by_status":           [{"status": r["status"], "count": r["count"]} for r in by_status],
        "by_type_status":      [
            {"account_type": r["account_type"], "status": r["status"], "count": r["count"]}
            for r in by_type
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. Alerts summary — threshold breaches across all sections
# ─────────────────────────────────────────────────────────────────────────────
def alerts_summary(
    concentration: list[dict],
    margin: list[dict],
    exposure: dict,
) -> dict:
    """
    Cross-section alert counts — no DB queries, pure in-memory aggregation.
    """
    critical_margin = [m for m in margin       if m["risk_level"] == "critical"]
    warn_margin     = [m for m in margin       if m["risk_level"] == "warning"]
    critical_conc   = [c for c in concentration if c["risk_level"] == "critical"]
    warn_conc       = [c for c in concentration if c["risk_level"] == "warning"]
    conc_symbols    = [e for e in exposure.get("by_symbol", [])
                       if e.get("concentration_pct", 0) > CONC_WARN_PCT]

    has_critical = bool(critical_margin or critical_conc)
    has_warning  = bool(warn_margin or warn_conc or conc_symbols)

    return {
        "overall_status":           "critical" if has_critical else ("warning" if has_warning else "ok"),
        "critical_margin_count":    len(critical_margin),
        "warning_margin_count":     len(warn_margin),
        "critical_equity_loss":     len(critical_conc),
        "warning_equity_loss":      len(warn_conc),
        "concentration_alerts":     len(conc_symbols),
        "critical_accounts":        [{"id": m["account_id"], "user": m["username"],
                                      "margin_level": m["margin_level_approx"]}
                                     for m in critical_margin],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Full report
# ─────────────────────────────────────────────────────────────────────────────
def full_report() -> dict:
    """
    All broker monitoring sections in one call.
    Each section runs independently — failure in one does not block others.
    """
    def _timed(fn, *args, **kwargs):
        t0 = _t.monotonic()
        try:
            data = fn(*args, **kwargs)
            return data, round((_t.monotonic() - t0) * 1000, 1), None
        except Exception as exc:
            log.error("[broker_monitoring.full_report] section=%s error=%r", fn.__name__, exc, exc_info=True)
            return None, round((_t.monotonic() - t0) * 1000, 1), str(exc)

    t_start = _t.monotonic()

    pnl_data,    pnl_ms,    pnl_err    = _timed(broker_pnl_summary)
    exp_data,    exp_ms,    exp_err     = _timed(net_broker_exposure)
    pos_data,    pos_ms,    pos_err     = _timed(position_metrics)
    acc_data,    acc_ms,    acc_err     = _timed(account_summary)
    conc_data,   con_ms,    con_err     = _timed(trader_risk_concentration)
    marg_data,   marg_ms,   marg_err   = _timed(critical_margin_accounts)

    # Alerts cross-section (no DB queries)
    alerts = alerts_summary(
        concentration=conc_data or [],
        margin=marg_data or [],
        exposure=exp_data or {},
    )

    errors = {k: v for k, v in {
        "pnl": pnl_err, "exposure": exp_err, "positions": pos_err,
        "accounts": acc_err, "concentration": con_err, "margin": marg_err,
    }.items() if v}

    if alerts["overall_status"] == "critical":
        log.warning(
            "[broker_monitoring] CRITICAL STATE — "
            "critical_margin=%d critical_equity_loss=%d",
            alerts["critical_margin_count"], alerts["critical_equity_loss"],
        )

    return {
        "generated_at": timezone.now().isoformat(),
        "elapsed_ms":   round((_t.monotonic() - t_start) * 1000, 1),
        "section_ms":   {
            "pnl": pnl_ms, "exposure": exp_ms, "positions": pos_ms,
            "accounts": acc_ms, "concentration": con_ms, "margin": marg_ms,
        },
        "alerts":              alerts,
        "pnl":                 pnl_data,
        "exposure":            exp_data,
        "positions":           pos_data,
        "accounts":            acc_data,
        "concentration_risk":  conc_data,
        "critical_margin":     marg_data,
        "errors":              errors,
    }
