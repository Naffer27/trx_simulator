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

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

from .models import MARGIN_ENGINE_TYPES, DD_ENGINE_TYPES

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

# Exposure thresholds (notional / equity).
# Calibrated for 10K/BTC@82K: 0.01→LOW 0.05→MEDIUM 0.10→HIGH 0.25→EXTREME
_EXPOSURE_THRESHOLDS = {"LOW": 25.0, "MEDIUM": 60.0, "HIGH": 100.0}

# Margin thresholds for RETAIL broker engine (used_margin_pct = margin_used / equity).
# Standard FX broker stopout: margin level < 50% (i.e. margin_used > 200% of maintenance).
_MARGIN_THRESHOLDS = {"WARNING": 20.0, "HIGH": 50.0, "DANGER": 80.0}


def _clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, value))


def _retail_risk_defaults(balance: Decimal) -> dict:
    """Dynamic risk defaults for RETAIL accounts — scales with initial balance."""
    b = Decimal(str(balance))
    return {
        "max_daily_loss_pct":  Decimal("5.00"),
        "max_drawdown_pct":    Decimal("10.00"),
        "max_lot_size":        _clamp(b / Decimal("1000"), Decimal("0.01"),  Decimal("5.0")),
        "max_open_positions":  int(_clamp(b / Decimal("500"),  Decimal("3"),    Decimal("20"))),
    }


def _retail_crypto_max(balance: Decimal) -> Decimal:
    b = Decimal(str(balance))
    return _clamp(b / Decimal("100000"), Decimal("0.001"), Decimal("2.0"))


def _get_account_defaults(account) -> dict:
    """Return tier/type-appropriate risk defaults dict for *account*."""
    if getattr(account, "account_type", "CHALLENGE") in MARGIN_ENGINE_TYPES:
        bal = Decimal(str(account.initial_balance or account.balance or 1000))
        return _retail_risk_defaults(bal)
    tier = getattr(account, "tier", "10K")
    return _TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["10K"])


def _get_crypto_max_lot(account) -> Decimal:
    if getattr(account, "account_type", "CHALLENGE") in MARGIN_ENGINE_TYPES:
        bal = Decimal(str(account.initial_balance or account.balance or 1000))
        return _retail_crypto_max(bal)
    tier = getattr(account, "tier", "10K")
    return _CRYPTO_MAX_LOT.get(tier, Decimal("0.25"))


def compute_margin_state(equity: float, total_margin_used: float, new_margin: float = 0.0) -> dict:
    """
    Pure margin calculator for the RETAIL broker engine. No DB, no side effects.

    Standard broker margin accounting:
      margin_level   = equity / margin_used × 100  (stopout typically at < 50%)
      used_margin_pct = margin_used / equity × 100
      maintenance     = 50% of total margin required
      free_margin     = equity − margin_used
    """
    equity = max(float(equity), 0.01)
    total_margin_used = float(total_margin_used)
    new_margin = float(new_margin)
    margin_after = total_margin_used + new_margin
    free_margin = equity - margin_after
    used_margin_pct = (margin_after / equity * 100.0) if equity > 0 else 100.0
    margin_level = (equity / margin_after * 100.0) if margin_after > 0 else 0.0
    maintenance_margin = margin_after * 0.5
    liquidation_distance = max(0.0, equity - maintenance_margin)
    return {
        "margin_used":          round(total_margin_used, 2),
        "new_margin_required":  round(new_margin, 2),
        "margin_after":         round(margin_after, 2),
        "free_margin":          round(free_margin, 2),
        "used_margin_pct":      round(used_margin_pct, 2),
        "margin_level":         round(margin_level, 2),
        "maintenance_margin":   round(maintenance_margin, 2),
        "liquidation_distance": round(liquidation_distance, 2),
    }


def evaluate_position_risk(account, symbol: str, lot_size: float,
                            current_equity: float, current_margin_used: float,
                            leverage: int = 50) -> dict:
    """
    Pure pre-trade risk calculator. No DB writes, no side effects.
    Returns risk metrics + risk_level: LOW / MEDIUM / HIGH / EXTREME.
    """
    try:
        from .exposure_engine import _get_current_price
        price = _get_current_price(symbol)
    except Exception:
        price = {"BTCUSD": 82000.0, "ETHUSD": 3400.0}.get(symbol, 1.17)
    if price <= 0:
        price = 1.0

    notional = lot_size * price
    new_margin = notional / max(1, leverage)

    balance = float(account.balance)
    peak = float(account.peak_balance) if float(account.peak_balance) > 0 else balance
    equity = max(current_equity, 0.01)

    defaults = _get_account_defaults(account)
    max_dd_pct = float(defaults["max_drawdown_pct"])

    dd_budget = peak * (max_dd_pct / 100.0)
    dd_used = max(0.0, peak - balance)
    dd_remaining = max(0.0, dd_budget - dd_used)

    exposure_pct = (notional / equity * 100.0) if equity > 0 else 999.0
    margin_after_pct = ((current_margin_used + new_margin) / equity * 100.0) if equity > 0 else 100.0

    # Estimated loss on a typical adverse move (2% crypto, 0.5% forex)
    adverse_move_pct = 2.0 if symbol in _CRYPTO_SYMS else 0.5
    est_adverse = notional * (adverse_move_pct / 100.0)
    dd_impact_pct = (est_adverse / peak * 100.0) if peak > 0 else 0.0

    # Recommended lot: target 20% exposure (safe LOW zone)
    recommended = round((equity * 0.20) / price, 3) if price > 0 else 0.01

    is_retail = getattr(account, "account_type", "CHALLENGE") in MARGIN_ENGINE_TYPES

    if is_retail:
        # ── RETAIL: margin-based risk engine ────────────────────────────────
        # Primary metric: used_margin_pct = (margin_used + new_margin) / equity.
        # Exposure is still computed and returned as a secondary analytic.
        margin_data = compute_margin_state(equity, current_margin_used, new_margin)
        used_pct = margin_data["used_margin_pct"]
        if used_pct >= _MARGIN_THRESHOLDS["DANGER"]:    # > 80 %
            risk_level = "EXTREME"
        elif used_pct >= _MARGIN_THRESHOLDS["HIGH"]:    # > 50 %
            risk_level = "HIGH"
        elif used_pct >= _MARGIN_THRESHOLDS["WARNING"]: # > 20 %
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"
        return {
            "risk_level":      risk_level,
            "engine":          "margin",
            "exposure_pct":    round(exposure_pct, 1),
            "notional":        round(notional, 2),
            "est_adverse_loss": round(est_adverse, 2),
            "dd_impact_pct":   round(dd_impact_pct, 2),
            "dd_remaining":    round(dd_remaining, 2),
            "recommended_lot": recommended,
            **margin_data,
        }

    # ── CHALLENGE / FUNDED: exposure-based risk engine ──────────────────────
    if exposure_pct >= _EXPOSURE_THRESHOLDS["HIGH"]:
        risk_level = "EXTREME"
    elif exposure_pct >= _EXPOSURE_THRESHOLDS["MEDIUM"]:
        risk_level = "HIGH"
    elif exposure_pct >= _EXPOSURE_THRESHOLDS["LOW"]:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return {
        "risk_level": risk_level,
        "exposure_pct": round(exposure_pct, 1),
        "margin_required": round(new_margin, 2),
        "margin_after_pct": round(margin_after_pct, 1),
        "est_adverse_loss": round(est_adverse, 2),
        "dd_impact_pct": round(dd_impact_pct, 2),
        "dd_remaining": round(dd_remaining, 2),
        "recommended_lot": recommended,
        "notional": round(notional, 2),
    }


def get_or_create_risk_rule(account):
    from .models import RiskRule
    defaults = _get_account_defaults(account).copy()
    defaults["max_exposure_usd"] = account.balance * Decimal("0.50")
    try:
        rule, _ = RiskRule.objects.get_or_create(account=account, defaults=defaults)
    except IntegrityError:
        # Two concurrent calls raced to create the first RiskRule for this account;
        # the other transaction won — just fetch it.
        rule = RiskRule.objects.get(account=account)
    return rule


# ─────────────────────────────────────────────
# Pre-trade gate — call before accepting orders
# ─────────────────────────────────────────────

def validate_order_risk(account, lot_size: float, open_positions_count: int,
                        symbol: str = "") -> list[dict]:
    """
    Validate an incoming order against all risk rules.
    Returns a list of error dicts — empty means the order is allowed.

    Error dict shape: {"code": str, "message": str, "blocking": bool}
      blocking=True  → order rejected (returned to client as error)
      blocking=False → warning only, order still executes

    CHALLENGE/FUNDED mode: hard violations suspend the account.
    RETAIL mode: no violations recorded, no suspension — only broker-style warnings.
    """
    from .models import TradingViolation, LedgerEntry, TradingAccount

    is_retail = getattr(account, "account_type", "CHALLENGE") in MARGIN_ENGINE_TYPES

    # 1. Account status gate — universal
    if account.status in BLOCKED_STATUSES:
        return [{"code": "account_blocked",
                 "message": f"Cuenta {account.status} — operaciones bloqueadas",
                 "blocking": True}]

    rule = get_or_create_risk_rule(account)
    _now = timezone.now()
    today = _now.date()
    today_start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    errors: list[dict] = []
    hard_violations: list = []  # only populated for CHALLENGE/FUNDED

    # 2. Lot size check
    lot_dec = Decimal(str(lot_size))
    if symbol in _CRYPTO_SYMS:
        effective_max = _get_crypto_max_lot(account)
    else:
        effective_max = rule.max_lot_size

    if lot_dec > effective_max:
        if is_retail:
            # Retail: soft warning — order still proceeds with the requested lot
            errors.append({
                "code": "lot_warning",
                "message": f"Lote {lot_size} supera el recomendado ({effective_max}). Procede bajo riesgo elevado.",
                "blocking": False,
            })
        else:
            # Challenge/Funded: hard rejection, log violation
            TradingViolation.objects.create(
                account=account,
                violation_type=TradingViolation.MAX_LOT_SIZE,
                value_at_violation=lot_dec,
                limit_value=effective_max,
                meta={"requested_lot": str(lot_size), "symbol": symbol},
            )
            errors.append({
                "code": "max_lot_size",
                "message": f"Lote {lot_size} supera el máximo ({effective_max}) para {symbol or 'este símbolo'}",
                "blocking": True,
            })

    # 3. Max open positions — hard rejection for all types (account stays active)
    if open_positions_count >= rule.max_open_positions:
        errors.append({
            "code": "max_positions",
            "message": f"Posiciones abiertas al límite ({rule.max_open_positions})",
            "blocking": True,
        })

    # 4. Daily loss limit
    today_pnl = (
        LedgerEntry.objects
        .filter(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            created_at__gte=today_start,
            created_at__lt=tomorrow_start,
        )
        .aggregate(total=Sum("amount"))["total"]
    ) or Decimal("0")

    if account.peak_balance > 0 and today_pnl < 0:
        daily_loss_pct = _pct(abs(today_pnl), account.peak_balance)
        if daily_loss_pct >= rule.max_daily_loss_pct:
            if is_retail:
                errors.append({
                    "code": "daily_loss_warning",
                    "message": f"Pérdida diaria {daily_loss_pct:.1f}% — margen bajo. Considera reducir exposición.",
                    "blocking": False,
                })
            else:
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
                    "blocking": True,
                })

    # 5. Max total drawdown from peak
    if account.peak_balance > 0:
        dd_pct = _pct(account.peak_balance - account.balance, account.peak_balance)
        if dd_pct >= rule.max_drawdown_pct:
            if is_retail:
                errors.append({
                    "code": "drawdown_warning",
                    "message": f"Drawdown {dd_pct:.1f}% — riesgo de liquidación. Reduce posiciones.",
                    "blocking": False,
                })
            else:
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
                    "blocking": True,
                })

    # Auto-suspend: CHALLENGE/FUNDED only — RETAIL accounts are NEVER suspended by rule violations
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


def check_equity_stopout(equity: float, peak_balance: float, tier: str,
                         account=None, account_type: str = "",
                         margin_used: float = 0.0) -> bool:
    """
    Real-time in-memory equity stopout check (no DB).

    RETAIL: margin-call engine — triggers when margin_level < 50%
            (equity / margin_used × 100 < 50). No DD enforcement.
    CHALLENGE/FUNDED: DD-based stopout from peak balance.
    """
    _is_retail = (account_type in MARGIN_ENGINE_TYPES) or (
        account is not None and getattr(account, "account_type", "") in MARGIN_ENGINE_TYPES
    )
    if _is_retail:
        if margin_used <= 0:
            return False  # no open positions → no margin call possible
        return (equity / margin_used * 100.0) < 50.0

    # CHALLENGE / FUNDED — drawdown from peak
    if account is not None:
        defaults = _get_account_defaults(account)
    else:
        defaults = _TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["10K"])
    max_dd_pct = float(defaults["max_drawdown_pct"])
    stopout_level = peak_balance * (1.0 - max_dd_pct / 100.0)
    return equity <= stopout_level


# ─────────────────────────────────────────────
# Core evaluation — call after every close
# ─────────────────────────────────────────────

def check_and_enforce_risk(account) -> list:
    """
    Evaluate post-trade risk for *account*.
    Returns list of TradingViolation objects created (may be empty).
    Must be called inside a DB transaction.

    CHALLENGE/FUNDED: violations → auto-suspend account.
    RETAIL: no violations created, no suspension. Broker-style margin
    liquidation is handled by the consumer's equity stopout path.
    """
    from .models import TradingViolation, LedgerEntry, TradingAccount

    is_retail = getattr(account, "account_type", "CHALLENGE") in MARGIN_ENGINE_TYPES

    violations: list = []
    rule = get_or_create_risk_rule(account)
    _now = timezone.now()
    today = _now.date()
    today_start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # 1. Update peak_balance (high-water mark) — all account types
    if account.balance > account.peak_balance:
        account.peak_balance = account.balance
        TradingAccount.objects.filter(pk=account.pk).update(peak_balance=account.peak_balance)

    # 2. Max drawdown from peak
    if account.peak_balance > 0:
        dd_pct = _pct(account.peak_balance - account.balance, account.peak_balance)
        if dd_pct >= rule.max_drawdown_pct and not is_retail:
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
        .filter(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            created_at__gte=today_start,
            created_at__lt=tomorrow_start,
        )
        .aggregate(total=Sum("amount"))["total"]
    ) or Decimal("0")

    if account.peak_balance > 0 and today_pnl < 0 and not is_retail:
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

    # 4. Auto-suspend: CHALLENGE/FUNDED only
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

    # 5. Update daily snapshot — all account types
    _upsert_daily_snapshot(account, today_pnl, today)

    return violations


def _upsert_daily_snapshot(account, today_pnl, today) -> None:
    from .models import DrawdownSnapshot
    dd_from_peak = (
        _pct(account.peak_balance - account.balance, account.peak_balance)
        if account.peak_balance > 0 else Decimal("0")
    )
    try:
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
    except IntegrityError:
        # Concurrent transaction already created today's snapshot — fetch it.
        snap = DrawdownSnapshot.objects.get(account=account, date=today)
        created = False
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
    score_fields = {
        "trader_class": trader_class,
        "win_rate": Decimal(str(metrics.get("win_rate", 0))),
        "avg_lot_size": Decimal(str(metrics.get("avg_lot_size", 0))),
        "martingale_rate": Decimal(str(metrics.get("martingale_rate", 0))),
        "profit_factor": Decimal(str(min(metrics.get("profit_factor", 1), 999))),
        "consistency_score": Decimal(str(metrics.get("consistency_score", 0))),
        "last_evaluated": timezone.now(),
    }
    try:
        TraderScore.objects.update_or_create(account=account, defaults=score_fields)
    except IntegrityError:
        # Race: another transaction inserted the row between our UPDATE (0 rows)
        # and our INSERT attempt. Fall back to a plain UPDATE.
        TraderScore.objects.filter(account=account).update(**score_fields)
