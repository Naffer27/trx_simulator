# simulator/risk_engine.py
"""
Core risk engine — called after every trade close.

Responsibilities:
  - Update peak_balance (high-water mark)
  - Check max drawdown from peak
  - Check daily loss limit
  - Record violations and auto-suspend account
  - Maintain daily DrawdownSnapshot
  - Classify trader (normal / risky / martingale / toxic / consistent / elite)
  - Update TraderScore

All DB operations are synchronous (called inside @database_sync_to_async wrappers).
"""

from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from django.db.models import Sum

# ─────────────────────────────────────────────
# Account status constants (mirror TradingAccount)
# ─────────────────────────────────────────────

STATUS_ACTIVE    = "Activo"
STATUS_SUSPENDED = "Suspendido"
STATUS_VIOLATED  = "Violado"
STATUS_CLOSED    = "Cerrado"
STATUS_FUNDED    = "Completado"

BLOCKED_STATUSES = {STATUS_SUSPENDED, STATUS_VIOLATED, STATUS_CLOSED}


def _pct(numerator, denominator) -> Decimal:
    if not denominator or denominator == 0:
        return Decimal("0")
    return (Decimal(str(numerator)) / Decimal(str(denominator)) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


# ─────────────────────────────────────────────
# Risk rule defaults per tier
# ─────────────────────────────────────────────

_TIER_DEFAULTS = {
    "10K":  {"max_daily_loss_pct": Decimal("5.00"),  "max_drawdown_pct": Decimal("10.00"), "max_lot_size": Decimal("5.00"),  "max_open_positions": 10},
    "50K":  {"max_daily_loss_pct": Decimal("4.00"),  "max_drawdown_pct": Decimal("8.00"),  "max_lot_size": Decimal("20.00"), "max_open_positions": 15},
    "100K": {"max_daily_loss_pct": Decimal("3.00"),  "max_drawdown_pct": Decimal("6.00"),  "max_lot_size": Decimal("40.00"), "max_open_positions": 20},
}

# Crypto symbols trade 1 lot = 1 BTC/ETH (no multiplier), so $82K/BTC means
# even 1 lot at leverage 50x gives $1/point PnL — max lots must be much smaller
# than forex to give the DD limits meaningful room.
#   10K account, 0.25 BTC max → DD blown by $4,000 BTC move (4.9%) — fair challenge
#   50K account, 1.00 BTC max → DD blown by $4,000 BTC move (2.0%)
#   100K account, 2.00 BTC max → DD blown by $3,000 BTC move (1.2%)
_CRYPTO_SYMS = {"BTCUSD", "ETHUSD"}
_CRYPTO_MAX_LOT = {
    "10K":  Decimal("0.25"),
    "50K":  Decimal("1.00"),
    "100K": Decimal("2.00"),
}

def get_or_create_risk_rule(account):
    from .models import RiskRule
    tier = getattr(account, "tier", "10K")
    defaults = _TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["10K"]).copy()
    defaults["max_exposure_usd"] = account.balance * Decimal("0.50")
    rule, _ = RiskRule.objects.get_or_create(account=account, defaults=defaults)
    return rule


# ─────────────────────────────────────────────
# Pre-trade gate — call before accepting orders
# ─────────────────────────────────────────────

def validate_order_risk(account, lot_size: float, open_positions_count: int,
                        symbol: str = "") -> list[dict]:
    """
    Validate an incoming order against all risk rules.
    Returns a list of error dicts — empty means the order is allowed.
    Hard violations (lot size, drawdown, daily loss) create TradingViolation
    records and trigger account suspension to STATUS_VIOLATED.
    Must be called inside a DB transaction.

    Error dict shape: {"code": str, "message": str}
    """
    from .models import TradingViolation, LedgerEntry, TradingAccount

    # 1. Account status gate — no DB query needed
    if account.status in BLOCKED_STATUSES:
        return [{"code": "account_blocked",
                 "message": f"Cuenta {account.status} — operaciones bloqueadas"}]

    rule = get_or_create_risk_rule(account)
    today = timezone.now().date()
    errors: list[dict] = []
    hard_violations: list = []

    # 2. Max lot size — crypto uses tighter per-tier limits (1 lot = 1 BTC/ETH ≈ $82K)
    lot_dec = Decimal(str(lot_size))
    tier = getattr(account, "tier", "10K")
    if symbol in _CRYPTO_SYMS:
        effective_max = _CRYPTO_MAX_LOT.get(tier, Decimal("0.25"))
    else:
        effective_max = rule.max_lot_size

    if lot_dec > effective_max:
        v = TradingViolation.objects.create(
            account=account,
            violation_type=TradingViolation.MAX_LOT_SIZE,
            value_at_violation=lot_dec,
            limit_value=effective_max,
            meta={"requested_lot": str(lot_size), "symbol": symbol},
        )
        hard_violations.append(v)
        errors.append({
            "code": "max_lot_size",
            "message": f"Lote {lot_size} supera el máximo permitido ({effective_max}) para {symbol or 'este símbolo'}",
        })

    # 3. Max open positions (soft — no violation record, just reject)
    if open_positions_count >= rule.max_open_positions:
        errors.append({
            "code": "max_positions",
            "message": f"Posiciones abiertas al límite ({rule.max_open_positions})",
        })

    # 4. Daily loss limit
    today_pnl = (
        LedgerEntry.objects
        .filter(account=account, event_type=LedgerEntry.EV_REALIZED, created_at__date=today)
        .aggregate(total=Sum("amount"))["total"]
    ) or Decimal("0")

    if account.peak_balance > 0 and today_pnl < 0:
        daily_loss_pct = _pct(abs(today_pnl), account.peak_balance)
        if daily_loss_pct >= rule.max_daily_loss_pct:
            v = TradingViolation.objects.create(
                account=account,
                violation_type=TradingViolation.MAX_DAILY_LOSS,
                value_at_violation=daily_loss_pct,
                limit_value=rule.max_daily_loss_pct,
                meta={"today_pnl": str(today_pnl), "date": str(today)},
            )
            hard_violations.append(v)
            errors.append({
                "code": "daily_loss_limit",
                "message": (
                    f"Pérdida diaria {daily_loss_pct:.1f}% supera el límite "
                    f"({rule.max_daily_loss_pct}%)"
                ),
            })

    # 5. Max total drawdown from peak
    if account.peak_balance > 0:
        dd_pct = _pct(account.peak_balance - account.balance, account.peak_balance)
        if dd_pct >= rule.max_drawdown_pct:
            v = TradingViolation.objects.create(
                account=account,
                violation_type=TradingViolation.MAX_DRAWDOWN,
                value_at_violation=dd_pct,
                limit_value=rule.max_drawdown_pct,
                meta={
                    "peak_balance": str(account.peak_balance),
                    "current_balance": str(account.balance),
                },
            )
            hard_violations.append(v)
            errors.append({
                "code": "max_drawdown",
                "message": (
                    f"Drawdown {dd_pct:.1f}% supera el máximo permitido "
                    f"({rule.max_drawdown_pct}%)"
                ),
            })

    # Auto-suspend on hard violations
    if hard_violations:
        TradingAccount.objects.filter(pk=account.pk).update(status=STATUS_VIOLATED)
        account.status = STATUS_VIOLATED
        LedgerEntry.objects.create(
            account=account,
            event_type=LedgerEntry.EV_ADJUST,
            amount=Decimal("0"),
            balance_after=account.balance,
            meta={
                "reason": "pre_trade_violation",
                "violations": [v.violation_type for v in hard_violations],
            },
        )

    return errors


def check_equity_stopout(equity: float, peak_balance: float, tier: str) -> bool:
    """
    Real-time in-memory equity stopout check (no DB).
    Returns True if equity has breached the max drawdown level
    calculated against the peak balance for this tier.
    """
    defaults = _TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["10K"])
    max_dd_pct = float(defaults["max_drawdown_pct"])
    stopout_level = peak_balance * (1.0 - max_dd_pct / 100.0)
    return equity <= stopout_level


# ─────────────────────────────────────────────
# Core evaluation — call after every close
# ─────────────────────────────────────────────

def check_and_enforce_risk(account) -> list:
    """
    Evaluate risk rules for *account* and suspend if any are violated.
    Returns list of TradingViolation objects created (may be empty).
    Must be called inside a DB transaction.
    """
    from .models import TradingViolation, LedgerEntry, TradingAccount

    violations: list = []
    rule = get_or_create_risk_rule(account)
    today = timezone.now().date()

    # 1. Update peak_balance (high-water mark)
    if account.balance > account.peak_balance:
        account.peak_balance = account.balance
        TradingAccount.objects.filter(pk=account.pk).update(peak_balance=account.peak_balance)

    # 2. Max drawdown from peak
    if account.peak_balance > 0:
        dd_pct = _pct(account.peak_balance - account.balance, account.peak_balance)
        if dd_pct >= rule.max_drawdown_pct:
            violations.append(
                TradingViolation.objects.create(
                    account=account,
                    violation_type=TradingViolation.MAX_DRAWDOWN,
                    value_at_violation=dd_pct,
                    limit_value=rule.max_drawdown_pct,
                    meta={
                        "peak_balance": str(account.peak_balance),
                        "current_balance": str(account.balance),
                    },
                )
            )

    # 3. Daily loss limit
    today_pnl = (
        LedgerEntry.objects
        .filter(account=account, event_type=LedgerEntry.EV_REALIZED, created_at__date=today)
        .aggregate(total=Sum("amount"))["total"]
    ) or Decimal("0")

    if account.peak_balance > 0 and today_pnl < 0:
        daily_loss_pct = _pct(abs(today_pnl), account.peak_balance)
        if daily_loss_pct >= rule.max_daily_loss_pct:
            violations.append(
                TradingViolation.objects.create(
                    account=account,
                    violation_type=TradingViolation.MAX_DAILY_LOSS,
                    value_at_violation=daily_loss_pct,
                    limit_value=rule.max_daily_loss_pct,
                    meta={"today_pnl": str(today_pnl), "date": str(today)},
                )
            )

    # 4. Auto-suspend on any violation
    if violations:
        TradingAccount.objects.filter(pk=account.pk).update(status="Suspendido")
        account.status = "Suspendido"
        LedgerEntry.objects.create(
            account=account,
            event_type=LedgerEntry.EV_ADJUST,
            amount=Decimal("0"),
            balance_after=account.balance,
            meta={
                "reason": "auto_suspend",
                "violations": [v.violation_type for v in violations],
            },
        )

    # 5. Update daily snapshot
    _upsert_daily_snapshot(account, today_pnl, today)

    return violations


def _upsert_daily_snapshot(account, today_pnl, today) -> None:
    from .models import DrawdownSnapshot
    dd_from_peak = (
        _pct(account.peak_balance - account.balance, account.peak_balance)
        if account.peak_balance > 0 else Decimal("0")
    )
    snap, created = DrawdownSnapshot.objects.get_or_create(
        account=account,
        date=today,
        defaults={
            "balance_start": account.balance,
            "balance_end": account.balance,
            "daily_pnl": today_pnl,
            "daily_pnl_pct": Decimal("0"),
            "peak_balance": account.peak_balance,
            "drawdown_from_peak": dd_from_peak,
        },
    )
    if not created:
        snap.balance_end = account.balance
        snap.daily_pnl = today_pnl
        snap.peak_balance = account.peak_balance
        snap.drawdown_from_peak = dd_from_peak
        if snap.balance_start and snap.balance_start != 0:
            snap.daily_pnl_pct = _pct(today_pnl, snap.balance_start)
        snap.save(update_fields=["balance_end", "daily_pnl", "daily_pnl_pct",
                                  "peak_balance", "drawdown_from_peak"])


# ─────────────────────────────────────────────
# Trader classification
# ─────────────────────────────────────────────

def classify_trader(account) -> tuple[str, dict]:
    """
    Analyze last 50 closed trades and return (class_str, metrics_dict).

    Classes (in priority order):
      TOXIC       — win_rate < 30% AND profit_factor < 0.7
      MARTINGALE  — lot size grows >50% after losses >25% of the time
      RISKY       — avg lot > tier limit OR max_loss per trade too large
      ELITE       — win_rate > 65% AND profit_factor > 2.0 AND consistency > 75
      CONSISTENT  — win_rate > 55% AND profit_factor > 1.5 AND consistency > 60
      NORMAL      — everything else
    """
    from .models import Trade

    trades = list(
        Trade.objects.filter(account=account, closed_at__isnull=False)
        .order_by("-closed_at")[:50]
    )
    n = len(trades)

    if n < 5:
        return "NORMAL", {"trades_analyzed": n, "reason": "insufficient_data"}

    wins = sum(1 for t in trades if float(t.profit_loss or 0) > 0)
    win_rate = wins / n * 100

    gross_profit = sum(float(t.profit_loss or 0) for t in trades if float(t.profit_loss or 0) > 0)
    gross_loss = abs(sum(float(t.profit_loss or 0) for t in trades if float(t.profit_loss or 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0

    lot_sizes = [float(t.lot_size) for t in trades]
    avg_lot = sum(lot_sizes) / n

    # Martingale detection
    martingale_triggers = 0
    for i in range(1, len(trades)):
        prev = trades[i]   # older
        curr = trades[i - 1]  # more recent
        if float(prev.profit_loss or 0) < 0 and float(curr.lot_size) > float(prev.lot_size) * 1.5:
            martingale_triggers += 1
    martingale_rate = martingale_triggers / max(n - 1, 1)

    # Consistency: coefficient of variation of P&L
    pnls = [float(t.profit_loss or 0) for t in trades]
    mean_pnl = sum(pnls) / n
    variance = sum((p - mean_pnl) ** 2 for p in pnls) / max(n - 1, 1)
    std_pnl = variance ** 0.5
    consistency = max(0.0, 100.0 - (std_pnl / (abs(mean_pnl) + 1) * 5))

    metrics = {
        "win_rate": round(win_rate, 2),
        "profit_factor": round(min(profit_factor, 999.0), 2),
        "avg_lot_size": round(avg_lot, 4),
        "martingale_rate": round(martingale_rate, 3),
        "consistency_score": round(consistency, 2),
        "trades_analyzed": n,
    }

    if win_rate < 30 and profit_factor < 0.7:
        return "TOXIC", metrics
    if martingale_rate > 0.25:
        return "MARTINGALE", metrics
    if avg_lot > float(get_or_create_risk_rule(account).max_lot_size) * 0.8:
        return "RISKY", metrics
    if win_rate > 65 and profit_factor > 2.0 and consistency > 75:
        return "ELITE", metrics
    if win_rate > 55 and profit_factor > 1.5 and consistency > 60:
        return "CONSISTENT", metrics

    return "NORMAL", metrics


def update_trader_score(account) -> None:
    """Refresh TraderScore for *account*. Safe to call after every close."""
    from .models import TraderScore
    trader_class, metrics = classify_trader(account)
    TraderScore.objects.update_or_create(
        account=account,
        defaults={
            "trader_class": trader_class,
            "win_rate": Decimal(str(metrics.get("win_rate", 0))),
            "avg_lot_size": Decimal(str(metrics.get("avg_lot_size", 0))),
            "martingale_rate": Decimal(str(metrics.get("martingale_rate", 0))),
            "profit_factor": Decimal(str(min(metrics.get("profit_factor", 1), 999))),
            "consistency_score": Decimal(str(metrics.get("consistency_score", 0))),
            "last_evaluated": timezone.now(),
        },
    )
