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
)

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

def _position_data(account_ids: list[int]) -> dict[int, dict]:
    """
    One query: fetch all open positions for the given accounts.
    Returns {account_id: {total_notional, long_notional, short_notional, count}}.
    Uses avg_price as a proxy for current price (consistent with broker_monitoring.py).
    """
    if not account_ids:
        return {}

    result: dict[int, dict] = {}
    for pos in Position.objects.filter(account_id__in=account_ids).values(
        "account_id", "side", "qty", "avg_price"
    ):
        aid = pos["account_id"]
        qty   = Decimal(str(pos["qty"]))
        price = Decimal(str(pos["avg_price"]))
        n     = qty * price          # notional: qty * price (per exposure_engine convention)

        if aid not in result:
            result[aid] = {"total": _ZERO, "long": _ZERO, "short": _ZERO, "count": 0}
        result[aid]["total"] += n
        result[aid]["count"] += 1
        if pos["side"] == "BUY":
            result[aid]["long"] += n
        else:
            result[aid]["short"] += n

    return result


def _margin_from_notional(notional: Decimal, leverage: int) -> Decimal:
    """Approximate margin_used from position notional and account leverage."""
    if leverage <= 0:
        leverage = 50
    return notional / Decimal(str(leverage))


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

    # ── 2. Position notionals — one query ─────────────────────────────────────
    pos_data = _position_data(account_ids)

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
        leverage = acc["leverage"] or 50
        drawdown = Decimal(str(acc["drawdown"] or 0))
        pd       = pos_data.get(acc["id"], {"total": _ZERO, "long": _ZERO, "short": _ZERO, "count": 0})

        margin_used  = _margin_from_notional(pd["total"], leverage)
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
            AccountEquitySnapshot.objects.bulk_create(acc_rows)

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
    """
    days   = retention_days if retention_days is not None else _retention_days()
    cutoff = timezone.now() - timedelta(days=days)

    broker_del, _  = BrokerEquitySnapshot.objects.filter(taken_at__lt=cutoff).delete()
    account_del, _ = AccountEquitySnapshot.objects.filter(taken_at__lt=cutoff).delete()

    log.info(
        "[snapshot.cleanup] retention=%dd cutoff=%s broker_deleted=%d account_deleted=%d",
        days, cutoff.isoformat(), broker_del, account_del,
    )
    return {
        "retention_days":           days,
        "cutoff":                   cutoff.isoformat(),
        "broker_snapshots_deleted": broker_del,
        "account_snapshots_deleted": account_del,
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
