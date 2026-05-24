# simulator/exposure_engine.py
"""
Dealer Analytics / Exposure Engine.
Computes broker-wide risk analytics: global exposure, per-symbol, per-trader-class.

Separate from risk_engine (per-account compliance) and intelligence_engine (per-trader behavior).
This module analyzes the BROKER's aggregated book.

All functions are synchronous — safe to call from Django views/admin.
"""

import json
import logging
import urllib.request
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from market_data.symbol_specs import get_spec as _get_sym_spec

log = logging.getLogger("simulator.exposure")

# Concentration threshold: flag if one symbol > X% of total gross exposure
CONCENTRATION_RISK_PCT = 50.0
# One-sided threshold: flag if one direction is > X% of total side
ONE_SIDED_THRESHOLD = 0.90


def _contract_size(symbol: str) -> float:
    """Return contract_size for *symbol*, defaulting to 1.0 for unknown instruments."""
    try:
        return _get_sym_spec(symbol).contract_size
    except KeyError:
        return 1.0


# ─────────────────────────────────────────────
# Price lookup (sync REST, for admin/batch use)
# ─────────────────────────────────────────────

_price_cache: dict[str, float] = {}


def _get_current_price(symbol: str) -> float:
    """
    Get current market price. Tries FeedManager first (fast, in-process),
    then REST sources. Results cached per call-batch to avoid redundant HTTP.
    """
    if symbol in _price_cache:
        return _price_cache[symbol]

    # Try FeedManager in-process cache (populated if any WS client is connected)
    try:
        from market_data.feeds import get_feed_manager
        fm = get_feed_manager()
        px = fm._prices.get(symbol, 0)
        if px > 0:
            _price_cache[symbol] = px
            return px
    except Exception:
        pass

    # REST fallback
    px = _fetch_price_rest(symbol)
    if px and px > 0:
        _price_cache[symbol] = px
        return px

    try:
        px = _get_sym_spec(symbol).base_price
    except KeyError:
        px = 1.0
    _price_cache[symbol] = px
    return px


def _fetch_price_rest(symbol: str) -> float | None:
    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "trx-sim/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    # Binance (may be geo-blocked)
    try:
        bn = _get_sym_spec(symbol).exchange_symbol
    except KeyError:
        bn = None
    if bn:
        try:
            d = _get(f"https://api.binance.com/api/v3/ticker/price?symbol={bn}")
            return float(d["price"])
        except Exception:
            pass

    # CoinGecko (free, global) — only for crypto assets
    _cg_map = {"BTCUSD": "bitcoin", "ETHUSD": "ethereum", "SOLUSD": "solana"}
    cg = _cg_map.get(symbol)
    if cg:
        try:
            d = _get(f"https://api.coingecko.com/api/v3/simple/price?ids={cg}&vs_currencies=usd")
            return float(d[cg]["usd"])
        except Exception:
            pass

    # Kraken (free, global)
    try:
        kr = _get_sym_spec(symbol).kraken_symbol
    except KeyError:
        kr = None
    if kr:
        try:
            d = _get(f"https://api.kraken.com/0/public/Ticker?pair={kr}")
            result = d.get("result") or {}
            ticker = next(iter(result.values()), None)
            if ticker:
                return float(ticker["c"][0])
        except Exception:
            pass

    return None


# ─────────────────────────────────────────────
# Core analytics computation
# ─────────────────────────────────────────────

def compute_live_analytics() -> dict:
    """
    Compute current broker-wide exposure analytics from live DB state.
    Pure read — no DB writes. Returns a dict with all panels.
    """
    from .models import (
        Position, TradingAccount, TraderScore, LedgerEntry,
    )

    _price_cache.clear()  # fresh prices per compute call

    _now = timezone.now()
    today = _now.date()
    today_start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # ── 1. All open positions with related trader scores ──
    positions = list(
        Position.objects
        .select_related("account", "account__trader_score")
        .all()
    )

    # ── 2. Current prices for every symbol in use ──
    symbols = {p.symbol for p in positions}
    prices  = {sym: _get_current_price(sym) for sym in symbols}

    # ── 3. Per-position notional + UPnL ──
    # Notional = qty * price * contract_size (correct for all instrument classes)
    # UPnL     = direction * (current - entry) * qty * contract_size

    sym_buckets: dict[str, dict]   = {}
    cls_buckets: dict[str, dict]   = {}
    total_long   = 0.0
    total_short  = 0.0
    total_upnl   = 0.0

    for pos in positions:
        sym       = pos.symbol
        price     = prices.get(sym, 1.0)
        qty       = float(pos.qty)
        entry     = float(pos.avg_price)
        direction = 1 if pos.side == "BUY" else -1
        cs        = _contract_size(sym)
        notional  = qty * price * cs
        upnl      = direction * (price - entry) * qty * cs

        # Trader intelligence
        score   = getattr(pos.account, "trader_score", None)
        cls     = score.trader_class    if score else "NORMAL"
        routing = score.routing_profile if score else "INTERNAL"

        # ── Symbol bucket ──
        if sym not in sym_buckets:
            sym_buckets[sym] = {
                "symbol": sym, "price": price,
                "long_qty": 0.0, "short_qty": 0.0,
                "long_usd": 0.0, "short_usd": 0.0,
                "unrealized_pnl": 0.0,
                "_trader_ids": set(),
            }
        b = sym_buckets[sym]
        b["_trader_ids"].add(pos.account_id)
        b["unrealized_pnl"] += upnl
        if pos.side == "BUY":
            b["long_qty"] += qty; b["long_usd"] += notional
            total_long += notional
        else:
            b["short_qty"] += qty; b["short_usd"] += notional
            total_short += notional

        # ── Class bucket ──
        if cls not in cls_buckets:
            cls_buckets[cls] = {
                "trader_class": cls, "routing_profile": routing,
                "long_usd": 0.0, "short_usd": 0.0,
                "unrealized_pnl": 0.0,
                "_account_ids": set(),
            }
        c = cls_buckets[cls]
        c["_account_ids"].add(pos.account_id)
        c["unrealized_pnl"] += upnl
        if pos.side == "BUY":
            c["long_usd"] += notional
        else:
            c["short_usd"] += notional

        total_upnl += upnl

    # ── 4. Finalise symbol buckets ──
    total_gross = sum(b["long_usd"] + b["short_usd"] for b in sym_buckets.values()) or 1.0
    symbol_list = []
    for sym, b in sym_buckets.items():
        net_qty  = b["long_qty"]  - b["short_qty"]
        net_usd  = b["long_usd"]  - b["short_usd"]
        gross    = b["long_usd"]  + b["short_usd"]
        conc_pct = round(gross / total_gross * 100, 2)

        # One-sided flow detection
        is_one_sided = False
        if gross > 0:
            long_ratio  = b["long_usd"]  / gross
            short_ratio = b["short_usd"] / gross
            is_one_sided = long_ratio >= ONE_SIDED_THRESHOLD or short_ratio >= ONE_SIDED_THRESHOLD

        symbol_list.append({
            "symbol":           sym,
            "price":            round(b["price"], 6),
            "long_qty":         round(b["long_qty"],  6),
            "short_qty":        round(b["short_qty"], 6),
            "net_qty":          round(net_qty,         6),
            "long_usd":         round(b["long_usd"],  2),
            "short_usd":        round(b["short_usd"], 2),
            "net_usd":          round(net_usd,         2),
            "trader_count":     len(b["_trader_ids"]),
            "concentration_pct": conc_pct,
            "unrealized_pnl":   round(b["unrealized_pnl"], 2),
            "is_high_risk":     conc_pct >= CONCENTRATION_RISK_PCT or is_one_sided,
            "is_one_sided":     is_one_sided,
        })
    symbol_list.sort(key=lambda x: x["long_usd"] + x["short_usd"], reverse=True)

    # ── 5. Finalise class buckets ──
    class_list = []
    for cls, c in cls_buckets.items():
        class_list.append({
            "trader_class":    cls,
            "routing_profile": c["routing_profile"],
            "account_count":   len(c["_account_ids"]),
            "long_usd":        round(c["long_usd"],        2),
            "short_usd":       round(c["short_usd"],       2),
            "net_usd":         round(c["long_usd"] - c["short_usd"], 2),
            "unrealized_pnl":  round(c["unrealized_pnl"], 2),
        })
    class_list.sort(key=lambda x: x["long_usd"] + x["short_usd"], reverse=True)

    # ── 6. Routing exposure breakdown ──
    internal_exp = sum(
        c["long_usd"] + c["short_usd"] for c in class_list
        if c["routing_profile"] in ("INTERNAL", "ELITE")
    )
    review_exp = sum(
        c["long_usd"] + c["short_usd"] for c in class_list
        if c["routing_profile"] == "REVIEW"
    )
    hedge_exp = sum(
        c["long_usd"] + c["short_usd"] for c in class_list
        if c["routing_profile"] == "HEDGE_CANDIDATE"
    )

    # ── 7. Broker simulated P&L ──
    # Broker is counter-party to INTERNAL + ELITE traders
    internal_upnl = sum(
        c["unrealized_pnl"] for c in class_list
        if c["routing_profile"] in ("INTERNAL", "ELITE")
    )
    broker_pnl_unrealized = -internal_upnl  # broker earns when internal traders lose

    # Today's realized PnL for internal routing traders
    internal_account_ids = {
        pos.account_id for pos in positions
        if getattr(getattr(pos.account, "trader_score", None), "routing_profile", "INTERNAL")
        in ("INTERNAL", "ELITE")
    }
    # Also include accounts with no positions (already closed today)
    all_internal_ids = set(
        TraderScore.objects.filter(
            routing_profile__in=["INTERNAL", "ELITE"]
        ).values_list("account_id", flat=True)
    )
    broker_pnl_today = -float(
        LedgerEntry.objects.filter(
            event_type=LedgerEntry.EV_REALIZED,
            created_at__gte=today_start,
            created_at__lt=tomorrow_start,
            account_id__in=all_internal_ids,
        ).aggregate(t=Sum("amount"))["t"] or 0
    )

    # ── 8. Today's total realized PnL (all accounts) ──
    today_realized = float(
        LedgerEntry.objects.filter(
            event_type=LedgerEntry.EV_REALIZED,
            created_at__gte=today_start,
            created_at__lt=tomorrow_start,
        ).aggregate(t=Sum("amount"))["t"] or 0
    )

    # ── 9. Total active accounts ──
    total_accounts = TradingAccount.objects.filter(status="Activo").count()

    # ── 10. Risk flags ──
    risk_flags = []
    for sym_data in symbol_list:
        if sym_data["concentration_pct"] >= CONCENTRATION_RISK_PCT:
            risk_flags.append({
                "type": "CONCENTRATION", "severity": "HIGH",
                "symbol": sym_data["symbol"],
                "msg": (f"{sym_data['symbol']} representa {sym_data['concentration_pct']:.1f}% "
                        f"de la exposición total bruta"),
            })
        if sym_data["is_one_sided"]:
            dominant = "LONG" if sym_data["long_usd"] > sym_data["short_usd"] else "SHORT"
            risk_flags.append({
                "type": "ONE_SIDED", "severity": "MEDIUM",
                "symbol": sym_data["symbol"],
                "msg": f"{sym_data['symbol']} flujo 100% {dominant} — sin cobertura natural",
            })

    # Toxic/HEDGE_CANDIDATE active exposure
    for cls_data in class_list:
        if cls_data["routing_profile"] == "HEDGE_CANDIDATE":
            gross = cls_data["long_usd"] + cls_data["short_usd"]
            if gross > 0:
                risk_flags.append({
                    "type": "TOXIC_ACTIVE", "severity": "HIGH",
                    "symbol": None,
                    "msg": (f"Traders {cls_data['trader_class']} (HEDGE_CANDIDATE) con "
                            f"${gross:,.2f} de exposición activa"),
                })

    net_exposure = total_long - total_short

    return {
        # ── Global ──
        "total_accounts":           total_accounts,
        "total_open_positions":     len(positions),
        "total_long_usd":           round(total_long,         2),
        "total_short_usd":          round(total_short,        2),
        "net_exposure_usd":         round(net_exposure,       2),
        "total_unrealized_pnl":     round(total_upnl,         2),
        "total_realized_pnl_today": round(today_realized,     2),
        # ── Routing ──
        "internal_exposure_usd":    round(internal_exp,       2),
        "review_exposure_usd":      round(review_exp,         2),
        "hedge_candidate_usd":      round(hedge_exp,          2),
        # ── Broker P&L ──
        "broker_pnl_unrealized":    round(broker_pnl_unrealized, 2),
        "broker_pnl_today":         round(broker_pnl_today,      2),
        # ── Risk ──
        "risk_flags":               risk_flags,
        # ── Breakdowns ──
        "symbol_exposures":         symbol_list,
        "class_exposures":          class_list,
        "computed_at":              timezone.now(),
    }


# ─────────────────────────────────────────────
# Persist snapshot to DB
# ─────────────────────────────────────────────

def save_snapshot() -> "BrokerSnapshot":
    """Compute live analytics and persist as a BrokerSnapshot record."""
    from .models import BrokerSnapshot, SymbolExposure, TraderClassExposure
    from decimal import Decimal as D

    data = compute_live_analytics()

    snap = BrokerSnapshot.objects.create(
        total_accounts           = data["total_accounts"],
        total_open_positions     = data["total_open_positions"],
        total_long_usd           = D(str(data["total_long_usd"])),
        total_short_usd          = D(str(data["total_short_usd"])),
        net_exposure_usd         = D(str(data["net_exposure_usd"])),
        total_unrealized_pnl     = D(str(data["total_unrealized_pnl"])),
        total_realized_pnl_today = D(str(data["total_realized_pnl_today"])),
        internal_exposure_usd    = D(str(data["internal_exposure_usd"])),
        review_exposure_usd      = D(str(data["review_exposure_usd"])),
        hedge_candidate_usd      = D(str(data["hedge_candidate_usd"])),
        broker_pnl_unrealized    = D(str(data["broker_pnl_unrealized"])),
        broker_pnl_today         = D(str(data["broker_pnl_today"])),
        risk_flags               = data["risk_flags"],
    )

    for s in data["symbol_exposures"]:
        SymbolExposure.objects.create(
            snapshot          = snap,
            symbol            = s["symbol"],
            long_qty          = D(str(s["long_qty"])),
            short_qty         = D(str(s["short_qty"])),
            net_qty           = D(str(s["net_qty"])),
            long_usd          = D(str(s["long_usd"])),
            short_usd         = D(str(s["short_usd"])),
            net_usd           = D(str(s["net_usd"])),
            trader_count      = s["trader_count"],
            concentration_pct = D(str(s["concentration_pct"])),
            unrealized_pnl    = D(str(s["unrealized_pnl"])),
            current_price     = D(str(s["price"])),
            is_high_risk      = s["is_high_risk"],
        )

    for c in data["class_exposures"]:
        TraderClassExposure.objects.create(
            snapshot        = snap,
            trader_class    = c["trader_class"],
            routing_profile = c["routing_profile"],
            account_count   = c["account_count"],
            long_usd        = D(str(c["long_usd"])),
            short_usd       = D(str(c["short_usd"])),
            net_usd         = D(str(c["net_usd"])),
            unrealized_pnl  = D(str(c["unrealized_pnl"])),
        )

    log.info("[exposure] snapshot #%d saved — net=$%.2f, positions=%d",
             snap.pk, float(snap.net_exposure_usd), snap.total_open_positions)
    return snap
