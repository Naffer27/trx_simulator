# simulator/intelligence_engine.py
"""
Trader Intelligence Engine
Behavioral analysis, classification, and routing preparation.

Separate from risk_engine (compliance) — this module is about BEHAVIOR.
Called after every trade close via consumers.py.

All DB operations are synchronous (called inside @database_sync_to_async wrappers).
"""

import datetime
from decimal import Decimal
from django.utils import timezone


# ─────────────────────────────────────────────
# Routing profile constants
# ─────────────────────────────────────────────

ROUTING_INTERNAL        = "INTERNAL"
ROUTING_REVIEW          = "REVIEW"
ROUTING_HEDGE_CANDIDATE = "HEDGE_CANDIDATE"
ROUTING_ELITE           = "ELITE"

_ROUTING_MAP = {
    "ELITE":      ROUTING_ELITE,
    "CONSISTENT": ROUTING_INTERNAL,
    "NORMAL":     ROUTING_INTERNAL,
    "GAMBLER":    ROUTING_INTERNAL,   # house edge is in our favor
    "MARTINGALE": ROUTING_REVIEW,
    "SCALPER":    ROUTING_REVIEW,
    "RISKY":      ROUTING_HEDGE_CANDIDATE,
    "TOXIC":      ROUTING_HEDGE_CANDIDATE,
}


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

def update_intelligence(account) -> None:
    """
    Compute all behavioral metrics, classify trader, assign routing profile,
    and persist to TraderScore.
    Safe to call after every trade close — fast, O(100 trades).
    """
    from .models import TraderScore

    metrics      = compute_metrics(account)
    trader_class = classify_trader(metrics, account)
    routing      = _ROUTING_MAP.get(trader_class, ROUTING_INTERNAL)

    TraderScore.objects.update_or_create(
        account=account,
        defaults={
            "trader_class":           trader_class,
            "routing_profile":        routing,
            "win_rate":               Decimal(str(metrics["win_rate"])),
            "profit_factor":          Decimal(str(min(metrics["profit_factor"], 999))),
            "avg_lot_size":           Decimal(str(metrics["avg_lot_size"])),
            "consistency_score":      Decimal(str(metrics["consistency_score"])),
            "avg_rr":                 Decimal(str(metrics["avg_rr"])),
            "pnl_volatility":         Decimal(str(metrics["pnl_volatility"])),
            "martingale_rate":        Decimal(str(metrics["martingale_rate"])),
            "lot_growth_rate":        Decimal(str(metrics["lot_growth_rate"])),
            "scalping_ratio":         Decimal(str(metrics["scalping_ratio"])),
            "avg_hold_time_seconds":  Decimal(str(metrics["avg_hold_time_seconds"])),
            "toxicity_score":         Decimal(str(metrics["toxicity_score"])),
            "gambler_score":          Decimal(str(metrics["gambler_score"])),
            "trade_frequency":        Decimal(str(metrics["trade_frequency"])),
            "max_consecutive_losses": metrics["max_consecutive_losses"],
            "max_consecutive_wins":   metrics["max_consecutive_wins"],
            "last_evaluated":         timezone.now(),
        },
    )


# ─────────────────────────────────────────────
# Metrics computation
# ─────────────────────────────────────────────

def compute_metrics(account) -> dict:
    """
    Compute all behavioral metrics from the last 100 closed trades.
    Returns a dict of plain Python floats/ints. Safe with 0 trades.
    """
    from .models import Trade

    trades = list(
        Trade.objects.filter(account=account, closed_at__isnull=False)
        .order_by("-closed_at")[:100]
    )
    n = len(trades)

    if n == 0:
        return _empty_metrics()

    pnls      = [float(t.profit_loss or 0) for t in trades]
    lot_sizes = [float(t.lot_size) for t in trades]

    # ── Basic performance ──
    wins         = sum(1 for p in pnls if p > 0)
    win_rate     = wins / n * 100
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
    avg_lot_size  = sum(lot_sizes) / n

    # ── Hold times ──
    hold_times = []
    for t in trades:
        if t.closed_at and t.opened_at:
            dt = (t.closed_at - t.opened_at).total_seconds()
            if dt >= 0:
                hold_times.append(dt)

    avg_hold_time  = sum(hold_times) / len(hold_times) if hold_times else 0.0
    scalping_count = sum(1 for h in hold_times if h < 60)
    scalping_ratio = scalping_count / len(hold_times) if hold_times else 0.0

    # ── Toxicity: win rate on ultra-short trades (< 120 s) ──
    short_pairs = [(h, p) for h, p in zip(hold_times, pnls[:len(hold_times)]) if h < 120]
    if len(short_pairs) >= 5:
        short_wins    = sum(1 for _, p in short_pairs if p > 0)
        win_rate_short = short_wins / len(short_pairs) * 100
    else:
        win_rate_short = 0.0
    toxicity_score = _calc_toxicity(win_rate_short, avg_hold_time, n)

    # ── Martingale detection ──
    martingale_triggers = 0
    for i in range(1, n):
        prev, curr = trades[i], trades[i - 1]       # older, newer
        if float(prev.profit_loss or 0) < 0 and float(curr.lot_size) > float(prev.lot_size) * 1.5:
            martingale_triggers += 1
    martingale_rate = martingale_triggers / max(n - 1, 1)

    # ── Lot growth rate (linear slope on chronological sequence) ──
    lot_growth_rate = _linear_slope_normalized(list(reversed(lot_sizes)))

    # ── Consecutive streaks ──
    max_cons_wins, max_cons_losses = _consecutive_streaks(pnls)

    # ── Trade frequency: trades/day over rolling 7-day window ──
    week_ago    = timezone.now() - datetime.timedelta(days=7)
    week_count  = sum(1 for t in trades if t.opened_at and t.opened_at >= week_ago)
    trade_frequency = week_count / 7.0

    # ── Average R:R (only trades with both SL and TP set) ──
    rr_values = []
    for t in trades:
        if t.stop_loss and t.take_profit and t.entry_price:
            entry  = float(t.entry_price)
            risk   = abs(entry - float(t.stop_loss))
            reward = abs(float(t.take_profit) - entry)
            if risk > 0:
                rr_values.append(reward / risk)
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    # ── PnL volatility (coefficient of variation, normalized) ──
    mean_pnl = sum(pnls) / n
    variance  = sum((p - mean_pnl) ** 2 for p in pnls) / max(n - 1, 1)
    std_pnl   = variance ** 0.5
    pnl_volatility = std_pnl / (abs(mean_pnl) + 1.0)

    # ── Consistency score (inverse of volatility, 0–100) ──
    consistency_score = max(0.0, 100.0 - pnl_volatility * 5)

    # ── Gambler score ──
    no_sl_ratio   = sum(1 for t in trades if not t.stop_loss) / n
    gambler_score = _calc_gambler(no_sl_ratio, trade_frequency, pnl_volatility)

    return {
        "n":                     n,
        "win_rate":              round(win_rate,         2),
        "profit_factor":         round(min(profit_factor, 999.0), 2),
        "avg_lot_size":          round(avg_lot_size,     4),
        "consistency_score":     round(consistency_score, 2),
        "avg_rr":                round(avg_rr,           3),
        "pnl_volatility":        round(pnl_volatility,   3),
        "martingale_rate":       round(martingale_rate,  3),
        "lot_growth_rate":       round(lot_growth_rate,  4),
        "scalping_ratio":        round(scalping_ratio,   3),
        "avg_hold_time_seconds": round(avg_hold_time,    1),
        "toxicity_score":        round(toxicity_score,   2),
        "gambler_score":         round(gambler_score,    2),
        "trade_frequency":       round(trade_frequency,  2),
        "max_consecutive_wins":  max_cons_wins,
        "max_consecutive_losses": max_cons_losses,
        "win_rate_short":        round(win_rate_short,   2),
    }


# ─────────────────────────────────────────────
# Classification — deterministic rules
# ─────────────────────────────────────────────

def classify_trader(metrics: dict, account) -> str:
    """
    Deterministic priority-ordered classification.
    First matching rule wins.
    """
    n = metrics["n"]
    if n < 5:
        return "NORMAL"

    win_rate        = metrics["win_rate"]
    profit_factor   = metrics["profit_factor"]
    martingale_rate = metrics["martingale_rate"]
    lot_growth_rate = metrics["lot_growth_rate"]
    scalping_ratio  = metrics["scalping_ratio"]
    avg_hold        = metrics["avg_hold_time_seconds"]
    toxicity_score  = metrics["toxicity_score"]
    gambler_score   = metrics["gambler_score"]
    avg_lot         = metrics["avg_lot_size"]
    consistency     = metrics["consistency_score"]

    # 1. TOXIC — anomalous win rate on ultra-short entries
    if toxicity_score >= 70 and avg_hold < 120:
        return "TOXIC"

    # 2. SCALPER — majority of trades are very short duration
    if scalping_ratio >= 0.60:
        return "SCALPER"

    # 3. MARTINGALE — aggressive lot size escalation after losses
    if martingale_rate > 0.25 and lot_growth_rate > 0.3:
        return "MARTINGALE"

    # 4. GAMBLER — chaotic behavior: no SL, high frequency, volatile PnL
    if gambler_score >= 60:
        return "GAMBLER"

    # 5. RISKY — consistent overlotting relative to account rules
    try:
        from .risk_engine import get_or_create_risk_rule
        rule    = get_or_create_risk_rule(account)
        max_lot = float(rule.max_lot_size)
    except Exception:
        max_lot = 5.0
    if avg_lot > max_lot * 0.8:
        return "RISKY"

    # 6. ELITE — excellent all-round performance
    if win_rate > 65 and profit_factor > 2.0 and consistency > 75:
        return "ELITE"

    # 7. CONSISTENT — solid disciplined trader
    if win_rate > 55 and profit_factor > 1.5 and consistency > 60:
        return "CONSISTENT"

    return "NORMAL"


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _empty_metrics() -> dict:
    return {
        "n": 0,
        "win_rate": 0.0,        "profit_factor": 0.0,      "avg_lot_size": 0.0,
        "consistency_score": 0.0, "avg_rr": 0.0,           "pnl_volatility": 0.0,
        "martingale_rate": 0.0, "lot_growth_rate": 0.0,    "scalping_ratio": 0.0,
        "avg_hold_time_seconds": 0.0, "toxicity_score": 0.0, "gambler_score": 0.0,
        "trade_frequency": 0.0, "max_consecutive_wins": 0,  "max_consecutive_losses": 0,
        "win_rate_short": 0.0,
    }


def _calc_toxicity(win_rate_short: float, avg_hold: float, n: int) -> float:
    """Score 0–100. High when ultra-short trades have anomalously high win rate."""
    if n < 10 or win_rate_short == 0:
        return 0.0
    # Excess above 50% benchmark, scaled to 0–100
    base         = max(0.0, win_rate_short - 50.0) * 2.0
    hold_factor  = max(0.0, 1.0 - avg_hold / 300.0)   # 1.0 at 0s → 0.0 at 5 min
    return min(100.0, base * (0.6 + 0.4 * hold_factor))


def _calc_gambler(no_sl_ratio: float, trade_freq: float, pnl_vol: float) -> float:
    """Score 0–100. High when trader has no SL, trades chaotically, erratic PnL."""
    sl_component   = no_sl_ratio * 40                          # 0–40
    freq_component = min(trade_freq / 10.0, 1.0) * 30         # 0–30  (10+ trades/day = max)
    vol_component  = min(pnl_vol   / 5.0,  1.0) * 30          # 0–30
    return min(100.0, sl_component + freq_component + vol_component)


def _consecutive_streaks(pnls: list) -> tuple:
    """Returns (max_consecutive_wins, max_consecutive_losses)."""
    max_w = max_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0
        elif p < 0:
            cur_l += 1; cur_w = 0
        else:
            cur_w = cur_l = 0
        max_w = max(max_w, cur_w)
        max_l = max(max_l, cur_l)
    return max_w, max_l


def _linear_slope_normalized(values: list) -> float:
    """
    Normalized linear regression slope of a sequence.
    Positive = growing, negative = shrinking. Roughly -1 to +1.
    """
    n = len(values)
    if n < 3:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0 or mean_y == 0:
        return 0.0
    return (num / den) / (abs(mean_y) + 1e-9)
