"""
simulator/snapshots.py
Equity snapshot computation and persistence.

Rules:
- All computation is from DB state (no live price feed calls)
- The ONLY side effects are INSERT rows into BrokerEquitySnapshot / AccountEquitySnapshot
- No updates to TradingAccount, Wallet, or any financial model
- Cleanup deletes only snapshot rows (never financial data)
"""
import logging
from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    TradingAccount, Position,
    BrokerEquitySnapshot, AccountEquitySnapshot,
    BrokerRevenueSnapshot,
)
from market_data.symbol_specs import get_spec

log = logging.getLogger("simulator.snapshots")

_ZERO = Decimal("0")

# Env-configurable retention — read once at module level.
# Override via SNAPSHOT_RETENTION_DAYS in .env or Django settings.
def _retention_days() -> int:
    from django.conf import settings as _s
    return int(getattr(_s, "SNAPSHOT_RETENTION_DAYS", 7))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _position_data(account_ids: list[int], leverage_by_account: dict[int, int]) -> dict[int, dict]:
    """
    One query: fetch all open positions for the given accounts, and compute
    per-account notional/margin. Uses avg_price as a proxy for current price
    (consistent with broker_monitoring.py).

    FIX-SNAPSHOTS-CONTRACT-SIZE-01 — two bugs fixed here:

    BUG 1: notional was `qty * price`, missing contract_size — silently
    correct only for contract_size==1 instruments (crypto); wrong by
    100,000x for every Forex pair (contract_size=100000). Now
    `qty * contract_size * price`, contract_size resolved per position
    from market_data.symbol_specs — same convention already used by
    exposure_engine.py/broker_risk.py/pnl_engine.py. Prior art: this was a
    known, documented, unfixed finding — see broker_exposure.py's RISK-01
    FASE 1 audit.

    BUG 2: margin_used was reconstructed by summing ALL positions' notional
    first, then dividing the total ONCE by account.leverage — ignoring each
    symbol's own max_leverage cap entirely. Silently correct only when
    every open position's symbol.max_leverage >= account.leverage; wrong
    for any mixed portfolio where a lower-cap symbol (crypto/gold/indices,
    which cap well below typical Forex caps) is capped below the account's
    own leverage. Margin must be computed PER POSITION — effective_leverage
    = max(1, min(account.leverage, symbol.max_leverage)) — then summed,
    exactly mirroring the canonical per-position formula every real margin
    path already uses (see consumers.py's calculate_required_margin() call
    sites, broker_risk.py:701).

    Fallback for a symbol no longer in market_data.symbol_specs
    (deregistered/renamed since the position was opened — not expected for
    a currently-tradeable symbol, defensive only): contract_size defaults
    to 1.0 (same neutral fallback exposure_engine.py::_contract_size()
    already established); symbol_max_leverage defaults to the account's
    OWN leverage, i.e. no additional unknown-instrument cap is invented —
    the same "never fabricate a market-specific number" principle, just
    applied to the leverage side instead of the size side.

    Scope: snapshots/reporting only (AccountEquitySnapshot/
    BrokerEquitySnapshot, both INSERT-only) — never touches TradingAccount,
    Position, Trade, LedgerEntry, execution, runtime margin, risk, stopout,
    or liquidation. gross_long_usd/gross_short_usd (the "long"/"short" keys
    below) are deliberately UNCHANGED in kind: pure notional, never divided
    by leverage — only margin_used required the per-position leverage cap.

    Returns {account_id: {total, long, short, margin_used, count}}.
    """
    if not account_ids:
        return {}

    result: dict[int, dict] = {}
    for pos in Position.objects.filter(account_id__in=account_ids).values(
        "account_id", "symbol", "side", "qty", "avg_price"
    ):
        aid   = pos["account_id"]
        qty   = Decimal(str(pos["qty"]))
        price = Decimal(str(pos["avg_price"]))
        account_leverage = leverage_by_account.get(aid) or 50

        try:
            spec = get_spec(pos["symbol"])
            contract_size = Decimal(str(spec.contract_size))
            symbol_max_leverage = int(spec.max_leverage)
        except KeyError:
            contract_size = Decimal("1")
            symbol_max_leverage = account_leverage

        notional = qty * contract_size * price          # BUG 1 fix: contract_size now applied
        effective_leverage = max(1, min(account_leverage, symbol_max_leverage))
        position_margin = notional / Decimal(str(effective_leverage))   # BUG 2 fix: per-position leverage cap

        if aid not in result:
            result[aid] = {"total": _ZERO, "long": _ZERO, "short": _ZERO, "margin_used": _ZERO, "count": 0}
        result[aid]["total"] += notional
        result[aid]["margin_used"] += position_margin
        result[aid]["count"] += 1
        if pos["side"] == "BUY":
            result[aid]["long"] += notional
        else:
            result[aid]["short"] += notional

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def take_all_snapshots() -> dict:
    """
    Capture broker-wide + per-account equity state in one atomic operation.
    Writes one BrokerEquitySnapshot row and N AccountEquitySnapshot rows
    (one per active account).

    Returns a summary dict (logged by the Celery task).
    """
    taken_at = timezone.now()

    # ── 1. Active accounts — one query ────────────────────────────────────────
    accounts = list(
        TradingAccount.objects
        .filter(status="Activo")
        .values("id", "balance", "equity", "drawdown", "leverage")
    )
    account_ids = [a["id"] for a in accounts]
    leverage_by_account = {a["id"]: (a["leverage"] or 50) for a in accounts}

    # ── 2. Position notionals + per-position margin — one query ───────────────
    pos_data = _position_data(account_ids, leverage_by_account)

    # ── 3. Build per-account snapshots + aggregate broker totals ──────────────
    acc_rows: list[AccountEquitySnapshot] = []
    broker_balance     = _ZERO
    broker_equity      = _ZERO
    broker_margin      = _ZERO
    broker_gross_long  = _ZERO
    broker_gross_short = _ZERO
    broker_pos_count   = 0

    for acc in accounts:
        balance  = Decimal(str(acc["balance"] or 0))
        equity   = Decimal(str(acc["equity"]  or 0))
        drawdown = Decimal(str(acc["drawdown"] or 0))
        pd       = pos_data.get(acc["id"], {"total": _ZERO, "long": _ZERO, "short": _ZERO, "margin_used": _ZERO, "count": 0})

        margin_used  = pd["margin_used"]   # FIX-SNAPSHOTS-CONTRACT-SIZE-01 — summed per-position, not reconstructed from the total
        free_margin  = max(_ZERO, equity - margin_used)
        floating_pnl = equity - balance

        broker_balance     += balance
        broker_equity      += equity
        broker_margin      += margin_used
        broker_gross_long  += pd["long"]
        broker_gross_short += pd["short"]
        broker_pos_count   += pd["count"]

        acc_rows.append(AccountEquitySnapshot(
            account_id=acc["id"],
            taken_at=taken_at,
            balance=balance,
            equity=equity,
            floating_pnl=floating_pnl,
            margin_used=margin_used,
            free_margin=free_margin,
            drawdown=drawdown,
            open_positions=pd["count"],
        ))

    broker_free_margin  = max(_ZERO, broker_equity - broker_margin)
    broker_floating_pnl = broker_equity - broker_balance
    broker_net_exposure = abs(broker_gross_long - broker_gross_short)

    # ── 4. Persist — one atomic transaction, two writes ───────────────────────
    with transaction.atomic():
        broker_snap = BrokerEquitySnapshot.objects.create(
            taken_at=taken_at,
            active_accounts=len(accounts),
            open_positions=broker_pos_count,
            total_balance=broker_balance,
            total_equity=broker_equity,
            floating_pnl=broker_floating_pnl,
            total_margin_used=broker_margin,
            total_free_margin=broker_free_margin,
            gross_long_usd=broker_gross_long,
            gross_short_usd=broker_gross_short,
            net_exposure_usd=broker_net_exposure,
        )
        if acc_rows:
            AccountEquitySnapshot.objects.bulk_create(acc_rows, batch_size=200)

    log.info(
        "[snapshot] taken_at=%s broker_id=%d accounts=%d positions=%d "
        "equity=%.2f pnl=%.2f exposure=%.2f",
        taken_at.isoformat(), broker_snap.id, len(accounts),
        broker_pos_count, float(broker_equity),
        float(broker_floating_pnl), float(broker_net_exposure),
    )

    return {
        "taken_at":           taken_at.isoformat(),
        "broker_snapshot_id": broker_snap.id,
        "account_snapshots":  len(acc_rows),
        "active_accounts":    len(accounts),
        "open_positions":     broker_pos_count,
        "total_equity":       float(broker_equity),
        "floating_pnl":       float(broker_floating_pnl),
        "net_exposure_usd":   float(broker_net_exposure),
    }


def cleanup_old_snapshots(retention_days: int | None = None) -> dict:
    """
    Delete BrokerEquitySnapshot + AccountEquitySnapshot rows older than
    retention_days. Financial data (TradingAccount, LedgerEntry, etc.) is
    never touched.

    BrokerRevenueSnapshot uses a separate, longer retention controlled by
    REVENUE_SNAPSHOT_RETENTION_DAYS (default 90 days). This keeps the broker
    equity-curve history intact long after intraday equity snapshots expire.

    Future path: before deletion, rows older than retention can be exported to
    cold storage (S3/BigQuery) using the taken_at index as a cursor. The delete
    step then becomes a "trim after export" operation with zero data loss.
    """
    from django.conf import settings as _cfg

    days   = retention_days if retention_days is not None else _retention_days()
    cutoff = timezone.now() - timedelta(days=days)

    broker_del, _  = BrokerEquitySnapshot.objects.filter(taken_at__lt=cutoff).delete()
    account_del, _ = AccountEquitySnapshot.objects.filter(taken_at__lt=cutoff).delete()

    # Revenue snapshots: separate retention (default 90d, never shorter than equity retention)
    rev_days   = max(days, int(getattr(_cfg, "REVENUE_SNAPSHOT_RETENTION_DAYS", 90)))
    rev_cutoff = timezone.now() - timedelta(days=rev_days)
    rev_del, _ = BrokerRevenueSnapshot.objects.filter(taken_at__lt=rev_cutoff).delete()

    log.info(
        "[snapshot.cleanup] equity_retention=%dd broker_del=%d account_del=%d "
        "revenue_retention=%dd revenue_del=%d",
        days, broker_del, account_del, rev_days, rev_del,
    )
    return {
        "retention_days":             days,
        "cutoff":                     cutoff.isoformat(),
        "broker_snapshots_deleted":   broker_del,
        "account_snapshots_deleted":  account_del,
        "revenue_retention_days":     rev_days,
        "revenue_snapshots_deleted":  rev_del,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers (used by the snapshots endpoint)
# ─────────────────────────────────────────────────────────────────────────────

def query_broker_snapshots(since, until, limit: int = 1440) -> list[dict]:
    """
    Return BrokerEquitySnapshot rows in [since, until], newest-first, up to limit.
    """
    qs = (
        BrokerEquitySnapshot.objects
        .filter(taken_at__gte=since, taken_at__lte=until)
        .order_by("-taken_at")
        .values(
            "id", "taken_at", "active_accounts", "open_positions",
            "total_balance", "total_equity", "floating_pnl",
            "total_margin_used", "total_free_margin",
            "gross_long_usd", "gross_short_usd", "net_exposure_usd",
        )[:limit]
    )
    return [
        {
            "id":               r["id"],
            "taken_at":         r["taken_at"].isoformat(),
            "active_accounts":  r["active_accounts"],
            "open_positions":   r["open_positions"],
            "total_balance":    float(r["total_balance"]),
            "total_equity":     float(r["total_equity"]),
            "floating_pnl":     float(r["floating_pnl"]),
            "total_margin_used": float(r["total_margin_used"]),
            "total_free_margin": float(r["total_free_margin"]),
            "gross_long_usd":   float(r["gross_long_usd"]),
            "gross_short_usd":  float(r["gross_short_usd"]),
            "net_exposure_usd": float(r["net_exposure_usd"]),
        }
        for r in qs
    ]


def query_account_snapshots(account_id: int, since, until, limit: int = 1440) -> list[dict]:
    """
    Return AccountEquitySnapshot rows for a single account in [since, until].
    """
    qs = (
        AccountEquitySnapshot.objects
        .filter(account_id=account_id, taken_at__gte=since, taken_at__lte=until)
        .order_by("-taken_at")
        .values(
            "id", "taken_at", "balance", "equity", "floating_pnl",
            "margin_used", "free_margin", "drawdown", "open_positions",
        )[:limit]
    )
    return [
        {
            "id":             r["id"],
            "taken_at":       r["taken_at"].isoformat(),
            "balance":        float(r["balance"]),
            "equity":         float(r["equity"]),
            "floating_pnl":   float(r["floating_pnl"]),
            "margin_used":    float(r["margin_used"]),
            "free_margin":    float(r["free_margin"]),
            "drawdown":       float(r["drawdown"]),
            "open_positions": r["open_positions"],
        }
        for r in qs
    ]
