# simulator/consumers.py
import os, json, asyncio, time, logging, math
from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from market_data.feeds import get_feed_manager, _validate_quote_values, _closed_only, _MASSIVE_ENABLED_SYMBOLS, _MASSIVE_CRYPTO_ENABLED_SYMBOLS
from market_data.symbol_specs import get_spec, allowed_symbols, kline_symbols
from .models import TradingAccount, Position, Trade, LedgerEntry, BrokerLedger, PendingOrder
from .spread_engine import broker_price, calculate_spread_revenue, _get_config as _get_spread_config
from .observability import security_log
from . import pricing_context as pricing_ctx
from . import dynamic_spread
from . import pnl_engine
from .broker_ledger import create_broker_counterparty_entry

log = logging.getLogger("simulator.ws")

FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY", "") or "").strip()
DEFAULT_TICK_INTERVAL = float(os.getenv("PRICE_TICK_INTERVAL", "1.0"))

# Derived from symbol registry — no manual maintenance needed.
_KLINE_SYMBOLS   = kline_symbols()   # symbols with exchange kline stream (Binance/Kraken)
_ALLOWED_SYMBOLS = allowed_symbols() # whitelist: rejects unknown symbols at the WS boundary

# ---------------- TF helpers ----------------
def tf_seconds(tf: str) -> int:
    s = str(tf).strip().lower()
    alias = {
        "1": "1s","1sec":"1s","1second":"1s","1s":"1s",
        "60":"1m","60s":"1m","m1":"1m","1m":"1m","1min":"1m",
        "300":"5m","m5":"5m","5m":"5m",
        "900":"15m","m15":"15m","15m":"15m",
        "3600":"1h","h1":"1h","1h":"1h",
        "86400":"1d","d1":"1d","1d":"1d",
    }
    s = alias.get(s, s)
    return {"1s":1,"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}.get(s, 1)

def normalize_tf(tf: str) -> str:
    rev = {1:"1s",60:"1m",300:"5m",900:"15m",3600:"1h",86400:"1d"}
    return rev.get(tf_seconds(tf), "1s")

# ---------------- Símbolos / formatos ----------------
# Thin wrappers — all instrument parameters come from the symbol registry.

def step_decimals_for(symbol: str) -> tuple[float, int]:
    sp = get_spec(symbol)
    return (sp.tick_size, sp.price_decimals)

def spread_for(symbol: str) -> float:
    return get_spec(symbol).spread

def drift_for(symbol: str) -> float:
    return get_spec(symbol).sim_drift

def base_price_for(symbol: str) -> float:
    return get_spec(symbol).base_price


# ── Phase 6B.1 — Pre-Trade Margin Guard ──────────────────────────────────────
# Default caps applied to all accounts. Product snapshots can supply
# tighter per-account values (margin_call_level_snapshot, max_lot_size_snapshot,
# allowed_symbols_snapshot) that override these global defaults.

_DEFAULT_MAX_MARGIN_PER_TRADE_PCT = 10.0   # single-trade margin / equity ≤ 10 %
_DEFAULT_MAX_TOTAL_MARGIN_PCT     = 50.0   # total margin after open / equity ≤ 50 %


def _compute_pretrade_margin_guard(
    symbol: str,
    qty: float,
    entry_px: float,
    equity: float,
    margin_used_now: float,
    account_snap: dict,
    spec_max_leverage: int,
    spec_contract_size: float,
    max_margin_per_trade_pct: float = _DEFAULT_MAX_MARGIN_PER_TRADE_PCT,
    max_total_margin_pct: float = _DEFAULT_MAX_TOTAL_MARGIN_PCT,
    account_currency: str = "USD",
) -> tuple[bool, str, str, dict]:
    """
    Pure pre-trade guard — no I/O, no DB, no side effects.

    Returns (ok, code, user_message, details).
      ok=True  → order may proceed
      ok=False → order rejected; code and message sent to the frontend
      details  → PANEL-02: always-populated numeric breakdown
                 (required_margin, required_margin_pct,
                 projected_total_margin, projected_total_margin_pct,
                 max_total_margin_pct), whether the call passes or fails.
                 This is the ONE place the margin percentages are
                 computed — reused verbatim both by the fast pre-lock
                 estimate (_order_new) and by the authoritative post-lock
                 call inside _db_open_position_atomic()
                 (_compute_atomic_open_guard). No formula is duplicated.

    Checks (in order):
      0. margin currency conversion — required_margin must be computable
         in account_currency (FIX-USDJPY-MARGIN-01-B); fails closed,
         never fabricates a rate or assumes 1:1 (see below)
      1. allowed_symbols_snapshot  — symbol whitelist
      2. max_lot_size_snapshot     — product-level hard lot cap
      3. per-trade margin %        — required_margin / equity ≤ max_margin_per_trade_pct
      4. total margin after open % — (used + required) / equity ≤ max_total_margin_pct
      5. margin_level projection   — equity / (used + required) ≥ margin_call_level_snapshot

    max_margin_per_trade_pct / max_total_margin_pct (O.6c-1e): optional,
    default to the module's own historical constants — every caller that
    predates this parameter (any test, any call site that doesn't pass
    them) gets IDENTICAL behavior to before O.6c-1e, bit for bit. The two
    live call sites (consumers.py's fast pre-lock check and the
    authoritative atomic guard) pass the account's own resolved
    self.account["max_margin_per_trade_pct"]/["max_total_margin_pct"]
    (product snapshot, falling back to 10.0/50.0 — see the O.6c-1e
    hydration comments). No formula changed — only where the two
    thresholds come from.

    FIX-USDJPY-MARGIN-01-B — required_margin is derived from
    pnl_engine.calculate_required_margin(), the single base/quote-aware
    notional helper every real margin path in this codebase now shares
    (Design Lock FIX-USDJPY-MARGIN-01-A). account_currency defaults to
    "USD" for any caller/test that predates this parameter — identical
    behavior to before this block for every account whose currency is
    USD (100% of real accounts today, per the MARGIN-01/02 audit this
    block's Design Lock cites). spec_contract_size is no longer read by
    this function's own math (the helper re-derives contract_size from
    market_data.symbol_specs itself, guaranteeing it can never drift
    from what the rest of the system uses) — kept as a required
    parameter only so every existing positional call site/test keeps
    working unchanged; minimal-diff choice over a signature change that
    would ripple into call sites unrelated to this fix.
    """
    account_lev = max(1, int(account_snap.get("leverage", 50)))
    effective_lev = max(1, min(account_lev, spec_max_leverage))
    equity_safe = max(float(equity), 0.01)

    required_margin, _margin_error_code = pnl_engine.calculate_required_margin(
        symbol, entry_px, qty, effective_lev, account_currency,
    )
    if required_margin is None:
        # Case C (Design Lock §H) — account currency matches neither
        # base nor quote and no explicit conversion rate is available.
        # Unreachable today (every enabled symbol is Case A or B) — but
        # when it does happen, fail closed: never open at a fabricated
        # or 1:1-assumed margin.
        logging.getLogger("simulator.guard").error(
            "[guard] REJECTED margin_currency_conversion_unavailable | sym=%s qty=%s "
            "entry_px=%.5f account_currency=%s error_code=%s",
            symbol, qty, entry_px, account_currency, _margin_error_code,
        )
        return (
            False,
            "margin_currency_conversion_unavailable",
            (
                "Orden rechazada: no se pudo calcular el margen requerido en la "
                "moneda de la cuenta para este símbolo."
            ),
            {
                "required_margin": 0.0, "required_margin_pct": 0.0,
                "projected_total_margin": round(float(margin_used_now), 4),
                "projected_total_margin_pct": round(float(margin_used_now) / equity_safe * 100.0, 2),
                "max_total_margin_pct": max_total_margin_pct,
            },
        )

    per_trade_pct = required_margin / equity_safe * 100.0
    total_margin_after = float(margin_used_now) + required_margin
    total_margin_pct = total_margin_after / equity_safe * 100.0

    details = {
        "required_margin": round(required_margin, 4),
        "required_margin_pct": round(per_trade_pct, 2),
        "projected_total_margin": round(total_margin_after, 4),
        "projected_total_margin_pct": round(total_margin_pct, 2),
        "max_total_margin_pct": max_total_margin_pct,
    }

    # 1 — Symbol whitelist (None = all symbols allowed)
    allowed = account_snap.get("allowed_symbols")
    if allowed is not None and symbol not in allowed:
        return (
            False,
            "symbol_not_allowed",
            "Orden rechazada: símbolo no permitido para esta cuenta.",
            details,
        )

    # 2 — Product max lot size snapshot
    max_lot = account_snap.get("max_lot_size")
    if max_lot is not None and qty > float(max_lot):
        return (
            False,
            "lot_size_exceeds_product_limit",
            (
                f"Orden rechazada: el tamaño es demasiado alto para esta cuenta. "
                f"Máximo permitido: {float(max_lot):.3f} lotes. Prueba con un lote menor."
            ),
            details,
        )

    # 3 — Per-trade margin cap
    if per_trade_pct > max_margin_per_trade_pct:
        _guard_log = logging.getLogger("simulator.guard")
        _guard_log.warning(
            "[guard] REJECTED margin_per_trade_exceeded | sym=%s qty=%s entry_px=%.2f "
            "equity=%.2f margin_used_now=%.2f required_margin=%.4f margin_after=%.4f "
            "free_margin=%.4f margin_level_after=%.2f per_trade_pct=%.2f%% "
            "max_per_trade=%.1f%% max_total=%.1f%% account_lev=%d spec_max_lev=%d "
            "effective_lev=%d",
            symbol, qty, entry_px, equity_safe, float(margin_used_now),
            required_margin, total_margin_after,
            equity_safe - total_margin_after,
            equity_safe / total_margin_after * 100.0 if total_margin_after > 0 else 0.0,
            per_trade_pct,
            max_margin_per_trade_pct, max_total_margin_pct,
            account_lev, spec_max_leverage, effective_lev,
        )
        return (
            False,
            "margin_per_trade_exceeded",
            (
                f"Orden rechazada: margen insuficiente. Esta operación requeriría "
                f"{per_trade_pct:.1f}% de tu equity como margen "
                f"(límite: {max_margin_per_trade_pct:.0f}%). "
                "Prueba con un lote menor."
            ),
            details,
        )

    # 4 — Total margin cap after this trade
    if total_margin_pct > max_total_margin_pct:
        _guard_log = logging.getLogger("simulator.guard")
        _guard_log.warning(
            "[guard] REJECTED total_margin_exceeded | sym=%s qty=%s entry_px=%.2f "
            "equity=%.2f margin_used_now=%.2f required_margin=%.4f margin_after=%.4f "
            "free_margin=%.4f per_trade_pct=%.2f%% total_margin_pct=%.2f%% "
            "max_total=%.1f%% account_lev=%d spec_max_lev=%d effective_lev=%d",
            symbol, qty, entry_px, equity_safe, float(margin_used_now),
            required_margin, total_margin_after,
            equity_safe - total_margin_after,
            per_trade_pct, total_margin_pct,
            max_total_margin_pct, account_lev, spec_max_leverage, effective_lev,
        )
        return (
            False,
            "total_margin_exceeded",
            (
                f"Orden rechazada: esta operación excedería el uso máximo de margen "
                f"permitido ({max_total_margin_pct:.0f}%). "
                f"Margen total proyectado: {total_margin_pct:.1f}%. "
                "Cierra posiciones o usa un lote menor."
            ),
            details,
        )

    # 5 — Margin level projection vs margin_call_level_snapshot
    margin_call_level = float(account_snap.get("margin_call_level") or 100.0)
    if total_margin_after > 0:
        margin_level_after = equity_safe / total_margin_after * 100.0
        if margin_level_after < margin_call_level:
            _guard_log = logging.getLogger("simulator.guard")
            _guard_log.warning(
                "[guard] REJECTED margin_call_level_breach | sym=%s qty=%s entry_px=%.2f "
                "equity=%.2f margin_used_now=%.2f required_margin=%.4f margin_after=%.4f "
                "free_margin=%.4f margin_level_after=%.2f%% margin_call_level_snap=%.2f%% "
                "account_lev=%d spec_max_lev=%d effective_lev=%d",
                symbol, qty, entry_px, equity_safe, float(margin_used_now),
                required_margin, total_margin_after,
                equity_safe - total_margin_after,
                margin_level_after, margin_call_level,
                account_lev, spec_max_leverage, effective_lev,
            )
            return (
                False,
                "margin_call_level_breach",
                (
                    f"Orden rechazada: margen insuficiente. "
                    f"El nivel de margen proyectado ({margin_level_after:.1f}%) quedaría "
                    f"por debajo del límite de tu cuenta ({margin_call_level:.0f}%). "
                    "Prueba con un lote menor."
                ),
                details,
            )

    return True, "ok", "", details


def _check_lot_size(qty: float, spec) -> tuple[bool, str]:
    """Pure — min_lot/lot_step check shared by the fast pre-check
    (_pretrade_check) and the authoritative atomic guard
    (_compute_atomic_open_guard).

    PANEL-02 — the lot_step remainder check is done as steps-from-nearest-
    integer (qty/lot_step vs round(qty/lot_step)) rather than the original
    `qty % lot_step > lot_step * 0.001`. This is a floating-point-safety
    fix, not a rule change: binary floats can't represent lot_step values
    like 0.01 exactly, so the raw modulo of a perfectly valid multiple
    (e.g. 1.0 % 0.01) can land a full 99.9% of one lot_step above zero
    (0.00999999999999998), false-rejecting the majority of whole-lot
    quantities (1.0, 2.0, 5.0, 10.0, ...) as "lot_step_violation". This
    was never exercised before PANEL-02: _pretrade_check (the only
    existing caller) is a fast, non-authoritative pre-lock guard whose
    rejection was never the final word, and _db_open_position_atomic
    never validated lot size at all until now. Wiring a real lot_step
    check into the authoritative path (FASE 2) exposed the bug — same
    0.001-of-one-step tolerance as before, computed in a numerically
    stable way; not a margin/PnL/spread/commission formula."""
    if qty < spec.min_lot:
        return False, "min_qty_violation"
    steps = qty / spec.lot_step
    if abs(steps - round(steps)) > 0.001:
        return False, "lot_step_violation"
    return True, "ok"


def _validate_sl_tp(side: str, sl, tp, exec_price: float) -> tuple[bool, str, str]:
    """
    PANEL-04 — server-side SL/TP validation for new orders. Pure, no I/O.
    Never trusts the frontend: a crafted WS payload can send anything
    (NaN, Infinity, a negative number, a value on the wrong side of the
    executable price) regardless of what the <input type=number> element
    would normally constrain in a real browser.

    Rejects:
      - a value that isn't a real number at all (fails float());
      - non-finite values (NaN, +Infinity, -Infinity);
      - zero or negative values — a price can never be <= 0;
      - SL/TP on the WRONG SIDE of the executable price for the given
        order side:
          BUY  — sl must be strictly BELOW exec_price, tp strictly ABOVE.
          SELL — sl must be strictly ABOVE exec_price, tp strictly BELOW.

    Deliberately does NOT enforce any minimum distance from exec_price —
    no such policy has been approved anywhere in this codebase (no
    existing min-stop-distance constant, config field, or product rule).
    Inventing one here would be a business decision this function has no
    authority to make; a SL/TP that is merely "very close" to the
    executable price is syntactically valid and accepted — only
    non-finite/non-positive/wrong-direction values are rejected.

    sl/tp are optional (None skips validation for that field). Returns
    (ok, error_code, message) — error_code/message pairs are specific per
    failure so the client can render a precise reason, never a generic
    catch-all.
    """
    for label, value, code_prefix in (("sl", sl, "invalid_sl"), ("tp", tp, "invalid_tp")):
        if value is None:
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            return False, f"{code_prefix}_value", f"{label.upper()} inválido: no es un número."
        if not math.isfinite(fval):
            return False, f"{code_prefix}_value", f"{label.upper()} inválido: valor no finito."
        if fval <= 0:
            return False, f"{code_prefix}_value", f"{label.upper()} inválido: debe ser mayor que cero."

    if sl is not None:
        sl_f = float(sl)
        if side == "buy" and sl_f >= exec_price:
            return (
                False, "invalid_sl_direction",
                "Stop Loss inválido: para una orden BUY debe estar por debajo del precio de ejecución.",
            )
        if side == "sell" and sl_f <= exec_price:
            return (
                False, "invalid_sl_direction",
                "Stop Loss inválido: para una orden SELL debe estar por encima del precio de ejecución.",
            )

    if tp is not None:
        tp_f = float(tp)
        if side == "buy" and tp_f <= exec_price:
            return (
                False, "invalid_tp_direction",
                "Take Profit inválido: para una orden BUY debe estar por encima del precio de ejecución.",
            )
        if side == "sell" and tp_f >= exec_price:
            return (
                False, "invalid_tp_direction",
                "Take Profit inválido: para una orden SELL debe estar por debajo del precio de ejecución.",
            )

    return True, "ok", "ok"


def _pending_trigger_condition_met(order_type: str, side: str, trigger_price: float,
                                    bid: float, ask: float) -> bool:
    """ORDER-MANAGEMENT-V2A — design lock section 2/C. Pure trigger-
    condition table, confirmed against _raw_exec_price()'s own side
    convention (BUY opens at ask, SELL opens at bid — see that
    function's docstring): the side a pending order would ACTUALLY fill
    on is the same side that decides whether its condition is met.
    Shared by THREE call sites so the table is defined exactly once:
      - creation-time validation (_order_pending_new — rejects an order
        already in-the-money at the moment it's created; that should be
        a market order instead, not a pending one),
      - the live-tick evaluator (_check_pending_triggers),
      - the offline daemon evaluator (tasks.scan_pending_orders_task).
    order_type/side are the DB's uppercase enum values
    (PendingOrder.LIMIT/STOP/BUY/SELL) — case-insensitive regardless."""
    order_type = str(order_type).upper()
    side = str(side).upper()
    if order_type == "LIMIT" and side == "BUY":
        return ask <= trigger_price
    if order_type == "LIMIT" and side == "SELL":
        return bid >= trigger_price
    if order_type == "STOP" and side == "BUY":
        return ask >= trigger_price
    if order_type == "STOP" and side == "SELL":
        return bid <= trigger_price
    return False


def _compute_atomic_open_guard(
    symbol: str,
    qty: float,
    entry_px: float,
    account_status: str,
    account_snap: dict,
    spec,
    fresh_equity: float,
    fresh_margin_used: float,
    is_new_position: bool,
    fresh_open_count: int,
    max_open_positions: int,
    account_currency: str = "USD",
) -> dict:
    """
    PANEL-02 — the single authoritative order-open validator. Called ONLY
    from inside _db_open_position_atomic()'s transaction.atomic() block,
    strictly AFTER select_for_update() has locked every open Position row
    for this account and the TradingAccount row itself. Every numeric
    input that can change between connections (fresh_equity,
    fresh_margin_used, fresh_open_count, account_status) is a value read
    fresh under that lock — never a value the caller computed beforehand
    from its own (possibly stale, per-connection) in-memory state. The
    pre-lock guard in _order_new (_pretrade_check /
    _compute_pretrade_margin_guard) remains only as a fast, non-
    authoritative early rejection — this function is the real authority.

    account_snap's leverage/allowed_symbols/max_lot_size/margin_call_level
    fields are frozen product snapshots (set once at account creation,
    never mutated per-trade — see TradingAccount.*_snapshot fields) — so
    trusting the caller's in-memory copy of THESE specific fields carries
    none of the staleness risk that made margin_used/equity/position-count
    unsafe to trust; they are not part of what this fix closes.

    Reuses _compute_pretrade_margin_guard() verbatim for the symbol/lot/
    margin-percentage math — no formula is duplicated or reimplemented;
    only the account-status gate and the max_open_positions check (neither
    of which _compute_pretrade_margin_guard covers) are layered around it.

    Returns a structured dict, always with the full field set (ok,
    error_code, message, required_margin, required_margin_pct,
    projected_total_margin, projected_total_margin_pct,
    max_total_margin_pct, current_open_positions, max_open_positions) —
    populated whether the order passes or is rejected. Never raises.
    """
    from .risk_engine import BLOCKED_STATUSES

    base = {
        "current_open_positions": fresh_open_count,
        "max_open_positions": max_open_positions,
    }
    _zero_margin_fields = {
        "required_margin": 0.0, "required_margin_pct": 0.0,
        "projected_total_margin": round(fresh_margin_used, 4),
        "projected_total_margin_pct": 0.0,
        # O.6c-1e — reflects this account's own configured policy (falls
        # back to the historical global default via account_snap's own
        # .get(), same as every other early-rejection info field here).
        "max_total_margin_pct": account_snap.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
    }

    # 0 — Account status gate — the freshest possible read: the very
    # TradingAccount row this transaction just locked, not
    # self.account["status"] cached from before the lock.
    if account_status in BLOCKED_STATUSES:
        return {
            "ok": False, "error_code": "account_blocked",
            "message": f"Cuenta {account_status} — operaciones bloqueadas",
            **_zero_margin_fields, **base,
        }

    # 1 — Lot size (min/step) — pure, symbol-spec-based; not itself a
    # source of staleness, included so this is a single, complete gate.
    _ok, _code = _check_lot_size(qty, spec)
    if not _ok:
        _msgs = {
            "min_qty_violation": "Orden rechazada: tamaño menor al mínimo permitido.",
            "lot_step_violation": "Orden rechazada: el tamaño no es múltiplo del paso permitido.",
        }
        return {
            "ok": False, "error_code": _code, "message": _msgs.get(_code, _code),
            **_zero_margin_fields, **base,
        }

    # 2 — max_open_positions — only a genuinely NEW position row counts
    # against the cap; a same-side netting merge does not increase the
    # account's position count, so it must never be blocked by this cap.
    if is_new_position and (fresh_open_count + 1) > max_open_positions:
        return {
            "ok": False, "error_code": "max_positions",
            "message": f"Posiciones abiertas al límite ({max_open_positions})",
            **_zero_margin_fields, **base,
        }

    # 3 — Symbol whitelist / product lot cap / per-trade margin / total
    # margin / margin-call-level projection — delegated to the SAME
    # function the fast pre-lock check uses, now fed fresh_equity/
    # fresh_margin_used instead of connection-memory values.
    guard_ok, guard_code, guard_msg, guard_details = _compute_pretrade_margin_guard(
        symbol, qty, entry_px, fresh_equity, fresh_margin_used,
        account_snap, spec.max_leverage, spec.contract_size,
        max_margin_per_trade_pct=account_snap.get("max_margin_per_trade_pct", _DEFAULT_MAX_MARGIN_PER_TRADE_PCT),
        max_total_margin_pct=account_snap.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
        account_currency=account_currency,
    )
    return {
        "ok": guard_ok,
        "error_code": None if guard_ok else guard_code,
        "message": "ok" if guard_ok else guard_msg,
        **guard_details,
        **base,
    }


# ── ORDER-MANAGEMENT-V2A — pending-order trigger executor ───────────────────
def _trigger_pending_order_core(pending_order_id: int, execution_price: float) -> dict:
    """
    The single authoritative pending-order trigger executor. Called from
    BOTH the live WS path (TradingConsumer._db_trigger_pending_order_atomic
    — a thin @database_sync_to_async wrapper) and the offline daemon
    (tasks.scan_pending_orders_task) — a plain, undecorated module-level
    function so a thread-pool-executed async wrapper and a genuinely sync
    Celery task can both call it directly, with ZERO duplication of the
    margin/risk formula: _compute_atomic_open_guard and
    broker_risk.validate_new_order are the EXACT SAME functions
    _db_open_position_atomic() uses for a real market order — reused
    verbatim here, never reimplemented (design lock, section E).

    execution_price is the caller's responsibility, resolved BEFORE this
    function is called, from the raw validated bid/ask exactly like a
    market order (_raw_exec_price's side convention: BUY -> ask, SELL ->
    bid — see design lock section 2/C). trigger_price only ever decided
    WHEN to attempt this call (design lock section 3); it never reaches
    this function as a price.

    Lock order (extends the module-level "global lock order" note below):
        PendingOrder (this one row, by id) → BrokerRiskLock →
        TradingAccount → Position
    PendingOrder is locked FIRST and held for the ENTIRE duration of this
    transaction — this is what makes a live-tick trigger and a daemon
    trigger (or two live ticks from two panels of the same account)
    racing for the SAME PendingOrder row safe WITHOUT any optimistic
    check-and-set (design lock section 5, explicitly forbidden): whichever
    transaction acquires this row's lock first runs the entire guard+open
    decision before releasing it; the loser blocks, then observes
    status != PENDING and no-ops. New to this codebase (no other path
    locks PendingOrder) — free to place first since it is the cheapest,
    single-row check that should fail fast before touching the
    broker-wide BrokerRiskLock singleton.

    Deliberately excluded (documented scope decisions, not omissions):
      - BOOK-04b Routing Engine Shadow Mode: off by default
        (ROUTING_ENGINE_ENABLED), non-financial (analysis-only, already
        best-effort/fail-open in _db_open_position_atomic itself), never
        discussed in the design lock. routing_decision_id is always None
        for a pending-order-triggered open.
      - Spread MARKUP fee (BrokerLedger REV_SPREAD / pricing_context):
        this function runs with no live WS connection, so the SPREAD-04/
        05 dynamic/commercial markup pipeline (which resolves per-tick,
        per-connection state) has nothing to read from. Mirrors the
        PRE-EXISTING, already-shipped precedent for daemon-driven SL/TP
        closes (see tasks._daemon_pricing_context's docstring: explicit
        zero markup, never a second spread pipeline) — not a new
        simplification invented for this block.
      - Commission (LedgerEntry EV_COMMISSION / BrokerLedger
        REV_COMMISSION) IS still charged — unlike the spread markup, it
        is deterministic per lot/pct-notional and fully DB-resolvable
        (commercial_pricing.resolve_commercial_pricing_fields(account),
        the exact same resolver TradingConsumer.commission_for() uses,
        just read fresh from the locked account row here instead of a
        connection's cached copy — same formula, not a second one).

    Returns a structured dict: {"ok": bool, "code": str, "position_id":
    int|None, "merged": bool|None}. Never raises.
    """
    from decimal import Decimal
    from .models import BrokerRiskLock, AuditLog
    from . import ws_events, commercial_pricing as _cp, broker_audit as _audit

    with transaction.atomic():
        po = (
            PendingOrder.objects.select_for_update()
            .filter(pk=pending_order_id)
            .first()
        )
        if po is None or po.status != PendingOrder.PENDING:
            return {"ok": False, "code": "not_pending", "position_id": None, "merged": None}

        now = timezone.now()

        # Design lock section 6 — expiry has precedence over trigger,
        # evaluated inside the SAME lock/transaction so live and daemon
        # are deterministically identical regardless of who evaluates
        # first.
        if po.expires_at is not None and po.expires_at <= now:
            po.status = PendingOrder.EXPIRED
            po.save(update_fields=["status", "updated_at"])
            transaction.on_commit(lambda _id=po.account_id, _pid=po.id: ws_events.publish_pending_order_changed(
                _id, action=ws_events.ACTION_PENDING_EXPIRE, pending_order_id=_pid,
            ))
            return {"ok": False, "code": "expired", "position_id": None, "merged": None}

        symbol, side, qty = po.symbol, po.side.lower(), float(po.qty)
        sl = float(po.sl) if po.sl is not None else None
        tp = float(po.tp) if po.tp is not None else None

        def _reject(code: str, message: str, **extra_detail) -> dict:
            po.status = PendingOrder.REJECTED
            po.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                event_type="pending_order.rejected",
                action=f"PendingOrder {po.id} rejected: {code}",
                account_id=po.account_id,
                detail={
                    "pending_order_id": po.id, "symbol": symbol, "side": side,
                    "reason": code, "message": message, **extra_detail,
                },
            )
            transaction.on_commit(lambda _id=po.account_id, _pid=po.id: ws_events.publish_pending_order_changed(
                _id, action=ws_events.ACTION_PENDING_REJECT, pending_order_id=_pid,
            ))
            return {"ok": False, "code": code, "position_id": None, "merged": None}

        # Design lock section 4 — SL/TP revalidated against the REAL fill
        # price (never trigger_price). Same pure validator _order_new()
        # uses for a market order — no second SL/TP policy invented.
        _sl_tp_ok, _sl_tp_code, _sl_tp_msg = _validate_sl_tp(side, sl, tp, execution_price)
        if not _sl_tp_ok:
            return _reject(_sl_tp_code, _sl_tp_msg, execution_price=execution_price)

        # 0 — BrokerRiskLock, same singleton/self-heal as
        # _db_open_position_atomic's own step 0.
        _lock_row, _lock_created = BrokerRiskLock.objects.get_or_create(pk=1)
        if _lock_created:
            _lock_row.last_recreated_at = timezone.now()
            _lock_row.save(update_fields=["last_recreated_at"])
        BrokerRiskLock.objects.select_for_update().get(pk=1)

        # 1 — TradingAccount, this account's own mutex.
        account = (
            TradingAccount.objects.select_for_update()
            .filter(id=po.account_id)
            .first()
        )
        if account is None:
            return _reject("account_not_found", "Cuenta no encontrada")

        # 2 — every open Position for this account, locked.
        open_positions = list(
            Position.objects.select_for_update()
            .filter(account=account)
            .order_by("id")
        )

        # 3 — netting merge target. account.netting_mode is the FRESH DB
        # field, just locked — the only value available in this
        # connection-less context. Documented divergence (not a bug this
        # function fixes): the live WS path's own _order_new() instead
        # reads a per-connection in-memory copy that the "order:mode" WS
        # action can change WITHOUT ever persisting to DB (consumers.py,
        # act == "order:mode") — so a live-triggered and a
        # daemon-triggered fill for the same account could theoretically
        # net differently if that in-memory override is active. DB is the
        # only truthful source available here.
        existing = None
        if account.netting_mode:
            existing = next(
                (p for p in open_positions if p.symbol == symbol and p.side == side.upper()),
                None,
            )
        is_new_position = existing is None
        fresh_open_count = len(open_positions)

        # 4 — fresh margin_used. Same formula as _db_open_position_atomic
        # step 4 / tasks._compute_offline_equity_margin
        # (pnl_engine.calculate_required_margin,
        # min(account.leverage, symbol.max_leverage)).
        account_lev = max(1, int(account.leverage))
        fresh_margin_used = 0.0
        for _p in open_positions:
            _pspec = get_spec(_p.symbol)
            _plev = max(1, min(account_lev, _pspec.max_leverage))
            _pmargin, _pmargin_error = pnl_engine.calculate_required_margin(
                _p.symbol, float(_p.avg_price), float(_p.qty), _plev, account.currency,
            )
            if _pmargin is None:
                log.critical(
                    "[pending_trigger] event=margin_conversion_unsupported symbol=%s "
                    "account_currency=%s error_code=%s — contributing 0.0, NOT a fabricated number.",
                    _p.symbol, account.currency, _pmargin_error,
                )
                continue
            fresh_margin_used += _pmargin

        # 5 — fresh equity. Daemon-context price source: the SAME Redis
        # cache FeedManager writes (tasks._read_cached_price) — the live
        # caller instead sources execution_price via self._feed
        # (FeedManager, shared per-process) BEFORE calling this function;
        # here, only OTHER open positions' mark prices are needed, and
        # this connection-less function has no self._feed to read, so it
        # always uses the cross-process Redis cache — same cache, same
        # validation (_validate_quote_values), just a different accessor
        # for a context with no live connection.
        from .tasks import _read_cached_price
        _unpriced = []
        _prices: dict = {}
        for _p in open_positions:
            _b, _a = _read_cached_price(_p.symbol)
            if _b is None or _a is None:
                _unpriced.append(_p.symbol)
            else:
                _prices[_p.symbol] = (_b, _a)
        if _unpriced:
            log.warning(
                "[pending_trigger] account=%s REJECTED — no fresh price for open position "
                "symbol(s) %s; refusing to compute fresh_equity rather than assume floating PnL=0",
                po.account_id, _unpriced,
            )
            return _reject("market_price_unavailable", "Precio no disponible para posiciones abiertas",
                            unpriced_symbols=_unpriced)

        fresh_floating_pnl = 0.0
        for _p in open_positions:
            _bid, _ask = _prices[_p.symbol]
            _close_px = _bid if _p.side == "BUY" else _ask
            fresh_floating_pnl += pnl_engine.position_pnl_float(
                _p.side.lower(), float(_p.avg_price), _close_px, float(_p.qty), _p.symbol,
                account_currency=account.currency,
            )
        fresh_equity = float(account.balance) + fresh_floating_pnl

        # 6 — the ONE authoritative validation, reused verbatim (see
        # docstring above).
        _spec = get_spec(symbol)
        from .risk_engine import get_or_create_risk_rule
        _rule = get_or_create_risk_rule(account)
        _account_snap = {
            "leverage": account_lev,
            "allowed_symbols": account.allowed_symbols_snapshot,
            "max_lot_size": float(account.max_lot_size_snapshot) if account.max_lot_size_snapshot is not None else None,
            "margin_call_level": float(account.margin_call_level_snapshot) if account.margin_call_level_snapshot is not None else None,
            "max_margin_per_trade_pct": float(account.max_margin_per_trade_pct_snapshot) if account.max_margin_per_trade_pct_snapshot is not None else _DEFAULT_MAX_MARGIN_PER_TRADE_PCT,
            "max_total_margin_pct": float(account.max_total_margin_pct_snapshot) if account.max_total_margin_pct_snapshot is not None else _DEFAULT_MAX_TOTAL_MARGIN_PCT,
        }
        guard = _compute_atomic_open_guard(
            symbol, qty, execution_price, account.status, _account_snap, _spec,
            fresh_equity, fresh_margin_used, is_new_position, fresh_open_count,
            _rule.max_open_positions, account_currency=account.currency,
        )
        if not guard["ok"]:
            return _reject(guard["error_code"], guard["message"])

        # 7 — RISK-02 broker-wide limits, same function
        # _db_open_position_atomic calls at its own step 8.5, under the
        # SAME BrokerRiskLock held since step 0 above.
        from .broker_risk import validate_new_order
        _risk02 = validate_new_order(
            account_id=po.account_id, symbol=symbol, side=side, qty=qty,
            price=execution_price, contract_size=_spec.contract_size,
            account_type=account.account_type,
        )
        if not _risk02.allowed:
            return _reject(_risk02.reason_code, _risk02.reason_message)

        # 8 — create/merge Position, identical logic to
        # _db_open_position_atomic step 9. No pricing_context captured
        # (see "deliberately excluded" note above) — Position.pricing_
        # context stays its documented null default for this path.
        if existing:
            new_qty = existing.qty + Decimal(str(qty))
            new_avg = (
                existing.avg_price * existing.qty + Decimal(str(execution_price)) * Decimal(str(qty))
            ) / new_qty
            existing.avg_price = new_avg.quantize(Decimal("0.000001"))
            existing.qty = new_qty
            if sl is not None:
                existing.sl = Decimal(str(sl))
            if tp is not None:
                existing.tp = Decimal(str(tp))
            existing.save(update_fields=["qty", "avg_price", "sl", "tp"])
            position_id = existing.id
            merged = True
        else:
            pos = Position.objects.create(
                account_id=po.account_id, symbol=symbol, side=side.upper(),
                qty=Decimal(str(qty)), avg_price=Decimal(str(execution_price)),
                sl=Decimal(str(sl)) if sl is not None else None,
                tp=Decimal(str(tp)) if tp is not None else None,
            )
            position_id = pos.id
            merged = False

        # 9 — commission (see "deliberately excluded" note above for why
        # the spread markup fee is skipped but commission is not).
        _cp_fields = _cp.resolve_commercial_pricing_fields(account)
        _profile = _cp.build_commercial_pricing_profile(_cp_fields, symbol)
        if _profile.commission_per_lot > 0:
            _commission = round(qty * _profile.commission_per_lot, 2)
        elif _profile.commission_pct > 0:
            _notional = qty * execution_price * _spec.contract_size
            _commission = max(0.0, _notional * _profile.commission_pct)
        elif _profile.source == _cp.SOURCE_LEGACY_FALLBACK:
            _notional = qty * execution_price * _spec.contract_size
            _commission = max(0.0, _notional * _spec.commission_pct)
        else:
            _commission = 0.0
        _commission_d = Decimal(str(_commission)) if _commission > 0 else Decimal("0")
        _auth_balance = account.balance - _commission_d

        if _commission_d > 0:
            trader_ledger = LedgerEntry.objects.create(
                account_id=po.account_id, event_type=LedgerEntry.EV_COMMISSION,
                amount=-_commission_d, balance_after=_auth_balance,
                meta={"symbol": symbol, "side": side, "db_pos_id": position_id,
                      "source": "pending_order_trigger", "pending_order_id": po.id},
            )
            try:
                BrokerLedger.objects.create(
                    revenue_type=BrokerLedger.REV_COMMISSION, amount=_commission_d,
                    source_account_id=po.account_id, source_ledger=trader_ledger,
                    symbol=symbol, meta={"side": side, "db_pos_id": position_id, "pending_order_id": po.id},
                )
            except Exception as _bl_exc:
                log.warning("[pending_trigger] broker_ledger commission insert failed pos=%s: %s", position_id, _bl_exc)
            account.balance = _auth_balance
            account.save(update_fields=["balance"])

        # 10 — finalize PendingOrder → TRIGGERED, only now that the open
        # has actually succeeded (never before — see docstring).
        po.status = PendingOrder.TRIGGERED
        po.triggered_at = now
        po.triggered_position_id = position_id
        po.save(update_fields=["status", "triggered_at", "triggered_position_id", "updated_at"])

        transaction.on_commit(lambda: ws_events.publish_position_changed(
            po.account_id,
            action=(ws_events.ACTION_UPDATE if merged else ws_events.ACTION_OPEN),
            position_id=position_id, symbol=symbol, side=side, qty=float(qty),
            new_balance=float(_auth_balance),
        ))
        transaction.on_commit(lambda: ws_events.publish_pending_order_changed(
            po.account_id, action=ws_events.ACTION_PENDING_TRIGGER, pending_order_id=po.id,
        ))
        transaction.on_commit(lambda: get_feed_manager().mark_position_symbol(symbol))

    log.info(
        "[pending_trigger] pending_order_id=%s pos_id=%s symbol=%s side=%s qty=%s merged=%s exec_px=%s",
        pending_order_id, position_id, symbol, side, qty, merged, execution_price,
    )
    _audit.record_trade_event(
        event_type=_audit.EV_POSITION_OPENED,
        description=(
            f"Position {'merged' if merged else 'opened'} on {symbol} {side.upper()} "
            f"qty={qty} (pending order #{pending_order_id} trigger)"
        ),
        account_id=po.account_id, symbol=symbol,
        source_module="simulator.consumers",
        metadata={"position_id": position_id, "side": side, "qty": float(qty),
                  "price": float(execution_price), "merged": merged, "pending_order_id": pending_order_id},
    )
    return {"ok": True, "code": "triggered", "position_id": position_id, "merged": merged}


# ── PANEL-02 INVARIANTE-2 / RISK-02 — global lock order ─────────────────────
# Audited across every live path in this codebase that locks any of
# BrokerRiskLock/TradingAccount/Position with select_for_update() inside a
# single transaction. The global order is:
#
#     BrokerRiskLock → TradingAccount → Position
#
#   - TradingConsumer._db_open_position_atomic  — BrokerRiskLock (RISK-02,
#     always — see BrokerRiskLock's docstring) → TradingAccount →
#     Position(all open, this account, .order_by("id"))
#   - TradingConsumer._db_close_position_atomic — TradingAccount →
#     Position(single, by id)   [no BrokerRiskLock — closing only reduces
#     broker-wide exposure, never needs to serialize against the risk gate]
#   - tasks._close_position_sync (Celery daemon TP/SL/stopout/margin-call
#     close)                     — TradingAccount → Position(single, by id)
#   - admin.py force_close (dealing desk)        — TradingAccount →
#     Position(all matching, .order_by("id"))
#   - _trigger_pending_order_core (ORDER-MANAGEMENT-V2A, live WS trigger AND
#     Celery scan_pending_orders_task daemon) — PendingOrder(single, by id,
#     locked and held for the WHOLE transaction) → BrokerRiskLock →
#     TradingAccount → Position(all open, this account, .order_by("id")).
#     PendingOrder is a new lock with no other existing caller, so it was
#     free to place first — never acquired by any OTHER path in this list,
#     so it cannot invert against any of them.
#
# This is a single, consistent, GLOBAL lock order — not a per-function
# choice. Any new code that locks two or more of these models MUST follow
# the same order; reversing it in even one path would create a classic
# lock-order-inversion deadlock against every path above the moment two of
# them run concurrently. In particular: BrokerRiskLock is ONLY ever
# acquired FIRST among {BrokerRiskLock, TradingAccount, Position} (by
# _db_open_position_atomic and _trigger_pending_order_core) and NEVER
# acquired by any code path that has already locked TradingAccount or
# Position — see BrokerRiskLock's model docstring for the full RISK-02
# rationale (the TOCTOU race this closes: two concurrent opens on
# DIFFERENT accounts, so TradingAccount locking alone cannot serialize
# them, could otherwise both read the same broker-wide exposure and
# jointly exceed a broker-wide limit).
#
# WHY ACCOUNT FIRST (not Position first, the original PANEL-02 design):
# TradingAccount is the account's actual mutex — exactly one row exists
# per account, always. Locking it first means a concurrent transaction
# for the SAME account always blocks here, REGARDLESS of how many
# Position rows currently exist. Locking Position first fails exactly at
# zero positions: select_for_update() against an empty queryset locks
# nothing, so two connections could each evaluate positions=[] before
# either held any lock at all, then separately proceed to lock Account in
# turn — the second one would still validate against its own STALE
# (empty) snapshot taken before the first one's commit, even though it
# technically "held a lock" by the time it wrote. Locking Account first
# closes that gap: every subsequent Position query in the same
# transaction is guaranteed to run AFTER any sibling transaction for this
# account has either fully committed (visible now, Read Committed) or is
# still blocked waiting for the very same Account lock (hasn't touched
# anything yet — nothing to miss).
#
# Multi-row Position locks (_db_open_position_atomic, force_close) also
# order by "id" ascending. With Account as the outer mutex, no two
# transactions ever hold overlapping Position locks for the same account
# simultaneously, so this specific ordering no longer prevents a live
# deadlock scenario by itself — kept anyway for deterministic query
# behavior and as a second line of defense if that invariant is ever
# weakened. Structural verification (query order, independent of DB
# backend) lives in simulator/tests/test_atomic_guard_lock_order.py.
#
# Dead code exclusion: TradingConsumer._db_mirror_close_position and
# _db_mirror_open_or_update also touch Position/TradingAccount but have
# ZERO call sites anywhere in this codebase (confirmed by repo-wide grep,
# re-verified for this fix) — unreachable, not part of the audited order,
# intentionally left untouched.
# ──────────────────────────────────────────────────────────────────────────


# ======================================================
#                       CONSUMER
# ======================================================
class TradingConsumer(AsyncWebsocketConsumer):

    # ---------------- Conexión ----------------
    async def connect(self):
        self._db_account_id = None
        self._last_db_sync = 0.0

        user = self.scope.get("user")
        is_auth = bool(user and getattr(user, "is_authenticated", False))

        # Querystring
        try:
            qs = parse_qs(self.scope.get("query_string", b"").decode())
            q_account_raw = qs.get("account",[None])[0] or qs.get("account_id",[None])[0]
            q_account = int(q_account_raw) if q_account_raw else None
            q_tf_raw = qs.get("tf",[None])[0] or qs.get("timeframe",[None])[0]
        except Exception:
            q_account = None
            q_tf_raw = None

        uname = getattr(user, "username", None)
        log.info("[connect] user=%s is_auth=%s q_account=%s", uname, is_auth, q_account)

        if not is_auth:
            client = self.scope.get("client")
            ip = client[0] if isinstance(client, (list, tuple)) and client else str(client)
            log.warning("[connect] rejected unauthenticated WS from %s", ip)
            security_log("ws.rejected_unauthenticated", ip=ip)
            await self.close(code=4001)
            return

        # Priority 0: account_id in WS URL path  ws/trading/<account_id>/
        if is_auth and not self._db_account_id:
            url_account_id = self.scope.get("url_route", {}).get("kwargs", {}).get("account_id")
            if url_account_id:
                acc = await self._db_get_account_for_user(int(url_account_id), user.id)
                if acc:
                    self._db_account_id = acc["id"]
                    log.info("[connect] db_account_id=%s (from URL path)", self._db_account_id)

        # Priority 1: querystring ?account=<id>
        if is_auth and not self._db_account_id and q_account:
            acc = await self._db_get_account_for_user(q_account, user.id)
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from URL param)", self._db_account_id)

        # Fallback 3: account_id stored in Django session by login_view
        if is_auth and not self._db_account_id:
            session = self.scope.get("session", {})
            sess_acc_id = session.get("account_id")
            log.info("[connect] session account_id=%s", sess_acc_id)
            if sess_acc_id:
                acc = await self._db_get_account_for_user(int(sess_acc_id), user.id)
                if acc:
                    self._db_account_id = acc["id"]
                    log.info("[connect] db_account_id=%s (from session)", self._db_account_id)

        # Fallback 4: most-recent active account for this user
        if is_auth and not self._db_account_id:
            acc = await self._db_get_latest_account_for_user(getattr(user, "id", None))
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from DB fallback)", self._db_account_id)

        if not self._db_account_id:
            log.warning("[connect] NO db_account_id resolved — all DB writes will be skipped")

        await self.accept()
        if self._db_account_id and self.channel_layer:
            await self.channel_layer.group_add(
                f"account_{self._db_account_id}", self.channel_name
            )
        await self._ws_counter(1)

        # SPREAD-03 — idempotent: starts the one process-wide, async-safe
        # BrokerSpreadConfig refresh loop on the first connection only; a
        # cheap no-op on every connection after that. Must never block or
        # fail the handshake — see simulator/spread_config_cache.py.
        try:
            from .spread_config_cache import ensure_background_refresh_started
            await ensure_background_refresh_started()
        except Exception as exc:
            log.debug("[connect] spread config cache warm-up failed (non-fatal): %r", exc)

        # O.6c-1v — OPEN POSITION FEED COVERAGE. Idempotent, same pattern
        # as spread_config_cache above — starts the one process-wide
        # position-feed reconciliation loop at most once. self._feed isn't
        # assigned yet at this point in connect() (a few lines below), so
        # this uses get_feed_manager() directly — same singleton either
        # way. Must never block or fail the handshake.
        try:
            await get_feed_manager().ensure_position_feed_reconciliation_started()
        except Exception as exc:
            log.debug("[connect] position feed reconciliation warm-up failed (non-fatal): %r", exc)

        # --- Estado inicial (memoria) ---
        self.symbol = "EUR/USD"
        self.timeframe = normalize_tf(q_tf_raw or "1m")
        self._price_state = {}   # mid price por símbolo
        self._bid_state   = {}   # bid (sell/close-buy) por símbolo
        self._ask_state   = {}   # ask (buy/close-sell) por símbolo
        # SPREAD-02 — raw (pre-markup) tick state, retained only long enough
        # to be captured into a pricing_context at the next open/close.
        self._raw_bid_state    = {}
        self._raw_ask_state    = {}
        self._pricing_ts_state = {}
        # SPREAD-02b — the BrokerSpreadConfig/provider snapshot that
        # actually produced THIS tick's executable bid/ask, captured once
        # per tick in price_tick() — never re-read later at order time.
        self._pricing_snapshot_state = {}
        self._order_seq = 1
        self._positions = []
        # ORDER-MANAGEMENT-V2A — this connection's own in-memory mirror of
        # its account's PENDING PendingOrder rows, hydrated in
        # _maybe_hydrate_from_db() — same "never a DB query in the hot
        # tick path" discipline self._positions already follows for
        # _check_tp_sl().
        self._pending_orders = []
        self._unpriced_pnl_symbols = []
        self._agg = {}
        # MASSIVE-CRYPTO-TRADE-CANDLES-01 — separate, crypto-trade-only
        # accumulator. Deliberately its own dict/lifecycle, never sharing
        # state with self._agg (quote-driven, Forex + legacy) — see
        # _reset_trade_agg()/price_trade()/_emit_trade_bar() below.
        self._trade_agg = {}
        self._last_bar_time = {}
        # CHART-HISTORY-INSTANT-LOAD-01 — first-paint history split.
        # _history_generation increments on every change_symbol/change_
        # timeframe/load_history request; _complete_history_depth()
        # (below) checks its own captured generation against the CURRENT
        # value (plus symbol/timeframe) before sending its "complete"
        # message, so a depth fetch left over from a symbol/timeframe the
        # user already moved past can never apply to the wrong chart —
        # even in the edge case where the user switches back to the same
        # symbol/timeframe before the old depth fetch finishes.
        # _history_depth_task is the ONE in-flight depth fetch for this
        # connection (one panel = one TradingConsumer = one connection) —
        # explicitly cancelled whenever a new one starts or the socket
        # disconnects, never left orphaned.
        self._history_generation = 0
        self._history_depth_task: "asyncio.Task | None" = None

        self.account = {
            "balance":       0.0,
            "equity":        0.0,
            "peak_balance":  0.0,
            "pnl_unreal":    0.0,
            "margin_used":   0.0,
            "leverage":      50,
            "currency":      "USD",
            "netting_mode":  False,
            "status":        "Activo",
            "account_type":  "CHALLENGE",
            "tier":          "",
            "profit_target": 0.0,
            "initial_balance": 0.0,
            # Phase 6B — product rule defaults (overwritten by hydration if snapshot set)
            "product_name":       "",
            "commission_per_lot": 0.0,
            "commission_pct":     0.0,
            "spread_pips":        0.0,
            "allowed_symbols":    None,
            "max_lot_size":       None,
            "margin_call_level":  100.0,
            "stopout_level":      50.0,
            # O.6c-1e — same fallback discipline as the two lines above.
            "max_margin_per_trade_pct": 10.0,
            "max_total_margin_pct":     50.0,
            # SPREAD-04 — account-level commercial pricing fields, resolved
            # once at hydrate time by commercial_pricing.resolve_commercial_
            # pricing_fields(); {} for guest/anonymous sessions (no DB
            # account to resolve against) — build_commercial_pricing_profile()
            # treats an empty dict as an explicit legacy_fallback profile.
            "commercial_pricing_fields": {},
        }
        self._daily_realized_pnl = 0.0
        self._daily_pnl_date = None

        await self._maybe_hydrate_from_db()

        # Shared feed subscription
        self._feed = get_feed_manager()
        self._seed_price_state(self.symbol)
        await self._feed.subscribe(self.symbol, self.channel_layer, self.channel_name)

        # Heartbeat — closes stale connections after 90 s of client silence
        self._last_msg_ts = time.time()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self.send_positions_snapshot()
        await self._refresh_and_send_pending_orders()
        await self._recalc_account_and_push()
        await self.send_json({"type":"ack","action":"connected",
                              "timeframe":self.timeframe,"tf_sec":tf_seconds(self.timeframe)})

    async def disconnect(self, close_code):
        await self._ws_counter(-1)
        # Cancel heartbeat
        hb = getattr(self, "_heartbeat_task", None)
        if hb and not hb.done():
            hb.cancel()
        # CHART-HISTORY-INSTANT-LOAD-01 — never leave a history-depth
        # fetch running past the connection it was started for; nothing
        # would await it, and send_json() on a closed socket would just
        # raise into an unretrieved task exception.
        depth_task = getattr(self, "_history_depth_task", None)
        if depth_task and not depth_task.done():
            depth_task.cancel()
        # Leave daemon notification group
        if getattr(self, "_db_account_id", None) and self.channel_layer:
            await self.channel_layer.group_discard(
                f"account_{self._db_account_id}", self.channel_name
            )
        # Unsubscribe from shared feed
        feed = getattr(self, "_feed", None)
        if feed:
            await feed.unsubscribe(self.symbol, self.channel_layer, self.channel_name)

    # ---------------- Mensajes entrantes ----------------
    async def receive(self, text_data: str):
        self._last_msg_ts = time.time()
        try:
            data = json.loads(text_data)
        except Exception:
            await self.send_json({"type":"error","message":"invalid_json"})
            return

        act = data.get("action")

        if act == "ping":
            await self.send_json({"type": "pong", "ts": int(time.time())})
            return

        if act == "change_symbol":
            new_sym = data.get("symbol", self.symbol)
            if new_sym not in _ALLOWED_SYMBOLS:
                await self.send_json({"type": "error", "code": "invalid_symbol", "message": "simbolo_no_permitido"})
                return
            old_sym = self.symbol
            if new_sym != old_sym:
                await self._feed.unsubscribe(old_sym, self.channel_layer, self.channel_name)
                self.symbol = new_sym
                self._reset_agg(new_sym)
                self._reset_trade_agg(new_sym)
                self._seed_price_state(new_sym)
                await self._feed.subscribe(new_sym, self.channel_layer, self.channel_name)
            self._last_bar_time.pop(new_sym, None)
            self._history_generation += 1
            hist, cursor = await self.generate_history_first_page(new_sym, self.timeframe, bars=240)
            await self._send_history_or_unavailable(new_sym, self.timeframe, hist, phase=("initial" if cursor is not None else "complete"))
            self._start_history_depth(new_sym, self.timeframe, cursor)
            await self.send_json({"type": "ack", "action": "symbol_changed", "symbol": new_sym})
            await self._refresh_and_send_positions()

        elif act == "change_timeframe":
            tf = normalize_tf(data.get("timeframe", self.timeframe))
            self.timeframe = tf
            self._reset_agg(self.symbol)
            self._reset_trade_agg(self.symbol)
            self._last_bar_time.pop(self.symbol, None)
            self._history_generation += 1
            hist, cursor = await self.generate_history_first_page(self.symbol, tf, bars=240)
            await self._send_history_or_unavailable(self.symbol, tf, hist, phase=("initial" if cursor is not None else "complete"))
            self._start_history_depth(self.symbol, tf, cursor)
            await self.send_json({"type":"ack","action":"change_timeframe","timeframe":tf,"tf_sec":tf_seconds(tf)})

        elif act == "load_history":
            sym = data.get("symbol", self.symbol)
            tf  = normalize_tf(data.get("timeframe", self.timeframe))
            self._history_generation += 1
            hist, cursor = await self.generate_history_first_page(sym, tf, bars=240)
            await self._send_history_or_unavailable(sym, tf, hist, phase=("initial" if cursor is not None else "complete"))
            self._start_history_depth(sym, tf, cursor)

        elif act == "account:get":
            await self._recalc_account_and_push()

        elif act == "get_closed_trades":
            # FIX-HISTORY-AUTO-CLOSE-SYNC-01 — on-demand reconciliation
            # snapshot for the frontend's closedTradesHistory. Account
            # scope is EXCLUSIVELY self._db_account_id (this connection's
            # own, already-authorized account) — any account_id the
            # client might send in `data` is ignored, never read.
            await self._send_closed_trades_snapshot()

        elif act == "order:mode":
            nm = data.get("netting_mode", None)
            if isinstance(nm, bool):
                self.account["netting_mode"] = nm
                await self.send_json({"type":"info","message":f"netting_mode={nm}"})

        elif act == "order:risk_preview":
            await self._handle_risk_preview(data)

        elif act == "order:new":
            await self._order_new(data)

        elif act == "order:update":
            await self._order_update(data)

        elif act == "order:close":
            await self._order_close(data)

        # ORDER-MANAGEMENT-V2A
        elif act == "order:pending:new":
            await self._order_pending_new(data)

        elif act == "order:pending:cancel":
            await self._order_pending_cancel(data)

        elif act == "order:pending:update":
            await self._order_pending_update(data)

        else:
            await self.send_json({"type":"ack","ok":True,"action":act})

    # ---------------- Streams ----------------
    # ---------------- Shared feed handler ----------------

    async def position_changed(self, event: dict):
        """O.6c-1o — MULTIPANEL-01 fix. Generalizes the original
        execution_close() (kept below as a thin backward-compat alias) to
        every Position writer identified in O.6c-1n's writer map — WS
        opens/netting-merges/closes/SL/TP/stopout/liquidation, the Celery
        daemon, and Django Admin — not just the 2 Celery daemon close
        paths execution_close originally covered. Pushed via the
        account_{account_id} Channels group (Redis-backed in production,
        so this reaches every connection for this account across every
        Daphne worker) AFTER the writer's own DB transaction has
        committed — transaction.on_commit() (consumers.py's two atomic
        methods) / Celery's own post-commit call / Admin's
        save_model/delete_model — never before, so a rolled-back
        transaction never publishes this event (see O.6c-1n's "DB COMMIT
        -> position.changed -> ... -> DB-fresh estado" contract).

        Per O.6c-1n Option C: this event is NEVER the source of truth.
        action/position_id/symbol/etc are metadata only, used below for a
        fast optimistic patch (so a close/open still feels instant) — but
        this handler ALWAYS finishes by resyncing self._positions from DB
        (_refresh_and_send_positions()) and self.account["balance"] via an
        UN-throttled _db_sync_account_balances() call (bypassing
        _recalc_account_and_push()'s own 1.2s PANEL-02 throttle, since
        this is an explicit invalidation signal, not a routine tick),
        regardless of what the payload said. Idempotent — a duplicate/
        out-of-order event, or one for a position_id already absent from
        self._positions, degrades to a harmless no-op DB resync (see
        test_idempotent_duplicate_event).
        """
        from . import ws_events

        action      = event.get("action")
        pos_id      = event.get("position_id")
        new_balance = event.get("new_balance")
        new_status  = event.get("new_status")
        realized    = event.get("realized_pnl")
        # ORDER-MANAGEMENT-V2B — present for every REAL close, full or
        # partial (see _db_close_position_atomic's own publish call);
        # absent for non-financial Position writers (netting merge,
        # PendingOrder trigger open, admin save_model/delete_model) that
        # never created a Trade. Used below as the TRADE CLOSED EVENT gate
        # (design lock §7/§8) — deliberately never `action` alone, since a
        # partial close publishes ACTION_UPDATE (the Position survives)
        # yet must still feed History exactly like a full close does.
        trade_id    = event.get("trade_id")

        if action == ws_events.ACTION_CLOSE and pos_id is not None:
            # Optimistic in-memory removal — never the final word (see
            # docstring above); _refresh_and_send_positions() below is
            # what actually decides self._positions. Full close only —
            # ACTION_UPDATE (partial close, netting merge) never removes;
            # the position survives with a reduced/changed qty, which the
            # unconditional resync below picks up from DB (design lock
            # §9: "rehidratar self._positions... mediante el mecanismo
            # existente" — no separate optimistic patch for partial).
            before = len(self._positions)
            self._positions = [p for p in self._positions if p["id"] != pos_id]
            if len(self._positions) == before:
                log.info("[position_changed] pos %s not in memory (other panel/already synced)", pos_id)

        # ORDER-MANAGEMENT-V2B — daily PnL tracking keys off realized_pnl
        # actually being present, not off `action`: a partial close
        # (ACTION_UPDATE) realizes money exactly like a full close
        # (ACTION_CLOSE) does, and daily-loss-limit enforcement must see
        # it either way. realized is None for every non-financial writer
        # (netting merge, admin save/delete, PendingOrder open) — same
        # precedent the pre-V2B code already relied on via this same field.
        if realized is not None:
            self._track_daily_pnl(float(realized))

        if new_balance is not None:
            self.account["balance"] = float(new_balance)
        if new_status:
            self.account["status"] = new_status

        # Authoritative DB-fresh resync — always runs, regardless of the
        # payload above. Order matters: positions FIRST (so pnl_unreal,
        # recomputed next, reflects the corrected position set — never
        # the stale/optimistic value still sitting in self.account from
        # before this event), THEN pnl_unreal, THEN balance un-throttled
        # (PANEL-02's throttle is for routine ticks, not explicit
        # invalidation signals like this one — _db_sync_account_balances()
        # persists equity = balance + self.account["pnl_unreal"], so it
        # MUST run after pnl_unreal is correct or a phantom position's
        # PnL could leak into the persisted DB column even after the
        # position itself is gone from self._positions — see
        # test_equity_persisted_after_sync_excludes_phantom_position).
        # _last_db_sync is updated so the immediately-following
        # _recalc_account_and_push() doesn't redundantly re-fetch again.
        await self._refresh_and_send_positions()
        self.account["pnl_unreal"] = round(self._unrealized_pnl_total(), 2)
        try:
            fresh_balance = await self._db_sync_account_balances()
        except Exception as exc:
            fresh_balance = None
            log.error("[position_changed] balance resync failed for account=%s: %r",
                      self._db_account_id, exc, exc_info=True)
        if fresh_balance is not None:
            self.account["balance"] = fresh_balance
            self._last_db_sync = time.time()
        await self._recalc_account_and_push()

        # ORDER-MANAGEMENT-V2B, design lock §7/§8 — TRADE CLOSED EVENT.
        # Gated on (action==ACTION_CLOSE OR trade_id present), never on
        # action alone: preserves the exact pre-V2B behavior for every
        # existing ACTION_CLOSE publisher that carries no trade_id (e.g.
        # admin.py PositionAdmin.delete_model, a raw row delete with no
        # Trade/realized_pnl — still notifies the frontend a position is
        # gone, unchanged), while ALSO firing for a partial close
        # (ACTION_UPDATE + trade_id, the new case) so History gets a Trade
        # event even though the Position itself survives. qty here means
        # "closed in THIS event" (close_qty, not the position's remaining
        # size) — for a full close this is exactly the old value.
        if (action == ws_events.ACTION_CLOSE or trade_id is not None) and pos_id is not None:
            await self.send_json({
                "type":          "order_close",
                "id":            pos_id,
                "trade_id":      trade_id,
                "symbol":        event.get("symbol"),
                "side":          event.get("side"),
                "qty":           event.get("qty"),
                "avg":           event.get("avg"),
                "close_px":      event.get("close_px"),
                "reason":        event.get("reason"),
                "realized_pnl":  realized if realized is not None else 0.0,
                "ts":            event.get("ts", int(time.time())),
                "partial":       event.get("partial", False),
                "remaining_qty": event.get("remaining_qty"),
            })

        # Stopout / margin-call UI notifications (additive — only for daemon-initiated paths)
        if new_status == "Suspendido":
            await self.send_json({
                "type":   "account:suspended",
                "status": "Suspendido",
                "reason": event.get("reason"),
            })
        elif event.get("reason") == "daemon_margin_call" and not self._positions:
            # FIX-03 — the condition still matches the daemon's own reason
            # string ("daemon_margin_call", set in tasks.py — out of this
            # block's authorized scope, unchanged); only the OUTGOING
            # WS event this connection relays to other tabs is corrected:
            # was "account:margin_call" with a hardcoded "50pct" string —
            # the real trigger is stopout_level, not a fixed 50%.
            await self.send_json({
                "type":    "account:stopout",
                "reason":  "margin_level_below_stopout",
                "balance": float(new_balance) if new_balance is not None else 0.0,
            })

    async def execution_close(self, event: dict):
        """Backward-compat alias for the pre-O.6c-1o event type
        ("execution.close", Channels-dispatched via the '.'->'_' method
        name convention). Kept so a stale/in-flight message from a
        rolling deploy (an old Celery worker process briefly overlapping
        with new consumer code, or vice versa) still works — translates
        the old flat payload into position_changed()'s contract and
        delegates to it, no separate logic to drift out of sync."""
        from . import ws_events
        event = dict(event)
        event.setdefault("action", ws_events.ACTION_CLOSE)
        await self.position_changed(event)

    async def price_tick(self, event: dict):
        """Receives broadcast ticks from FeedManager via channel layer group."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        raw_bid = event["bid"]
        raw_ask = event["ask"]
        mid     = event["mid"]
        ts      = event["time"]
        # FIX-05C — read once, up here, purely to hand it to the client in
        # the tick payload below. The financial gate further down (its own
        # `_price_source = event.get("source")`, untouched) reads the SAME
        # key independently — this is display-only, never wired into any
        # execution/SL-TP/pricing decision.
        source  = event.get("source")

        # O.6c-1w-b — RAW MARKET QUOTE -> VALIDATE -> broker markup ->
        # execution. Same structural + Capa A validation as
        # get_validated_quote() (_validate_quote_values — shared, never a
        # second copy of the rules), applied HERE, before broker_price()
        # ever runs. Closes O.6c-1w's residual gap: order:new/exec_price()
        # and _check_tp_sl() (live WS SL/TP) read self._bid_state/
        # self._ask_state — the markup-derived, per-connection cache —
        # rather than self._feed.get_validated_quote() directly (O.6c-1u's
        # documented raw-vs-markup split; that split itself is untouched
        # here, see the O.6c-1w-b report). An invalid raw tick is skipped
        # ENTIRELY: self._bid_state/self._ask_state are not updated (so
        # exec_price() can never read a value derived from it), and
        # _check_tp_sl() — called only from here, its sole call site — is
        # never invoked for this tick, so no SL/TP fires on it. The chart
        # simply does not advance this one tick; the next valid tick
        # resumes normally — same "wait for the next valid quote" policy
        # already established for every other O.6c-1w-protected path.
        if not _validate_quote_values(symbol, raw_bid, raw_ask):
            log.warning(
                "[price_tick] REJECTED invalid/implausible raw quote for %s: "
                "bid=%r ask=%r — markup/execution/SL-TP not applied this tick",
                symbol, raw_bid, raw_ask,
            )
            return

        # SPREAD-04 — one resolution of the commercial pricing profile per
        # tick, DB-free (spread_config_cache + the account-level fields
        # already cached at hydrate time). Both broker_price()'s clamp and
        # the pricing-context snapshot below use this SAME resolved profile,
        # so the price actually applied and the audit record can never
        # disagree.
        profile = self._resolve_commercial_pricing_profile(symbol)
        # SPREAD-05 — one resolution of the dynamic-spread inputs per tick,
        # DB-free (spread_config_cache + pure session/observability reads).
        # Passed to BOTH broker_price() (to price the fill when the
        # symbol's BrokerSpreadConfig.is_dynamic is True) and
        # tick_pricing_snapshot() (to freeze the identical decision for the
        # audit trail) — same "resolve once, reuse twice" pattern as
        # `profile` above.
        dynamic_inputs = dynamic_spread.build_dynamic_inputs(symbol, profile, ts)
        bid, ask = broker_price(
            symbol, raw_bid, raw_ask, markup_pips=profile.spread_markup_pips,
            min_spread_override=profile.min_spread_pips, max_spread_override=profile.max_spread_pips,
            dynamic_inputs=dynamic_inputs,
        )
        self.set_state(symbol, bid, ask, mid)
        # SPREAD-02 — retain the raw (pre-markup) tick for pricing-context
        # capture at the next open/close. Does not affect broker_price(),
        # the broadcast tick, or anything sent to the client.
        self._raw_bid_state[symbol]    = raw_bid
        self._raw_ask_state[symbol]    = raw_ask
        self._pricing_ts_state[symbol] = ts
        # SPREAD-02b — snapshot the exact BrokerSpreadConfig/commercial
        # profile/provider state that produced THIS tick's bid/ask, right
        # now — this is the only place allowed to read any of them;
        # _capture_pricing_context() only ever reads this snapshot back, it
        # never re-queries.
        self._pricing_snapshot_state[symbol] = pricing_ctx.tick_pricing_snapshot(
            symbol, profile, dynamic_inputs=dynamic_inputs,
        )
        await self.send_json({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "time": ts, "source": source})
        # MASSIVE-CRYPTO-TRADE-CANDLES-01 — crypto candles are built
        # exclusively from Massive trades now (price_trade(), fed by the
        # XT channel) — a quote tick (XQ, this method) must never also
        # drive the candle for these symbols, or the same market moment
        # would produce two independent, disagreeing OHLC updates.
        # Forex (and any future non-crypto symbol) is completely
        # unaffected — unchanged, still built from the quote mid here.
        if symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            await self._on_tick(symbol, mid, volume=0.0, ts=ts)
        # O.6c-1aa — UNIFIED RAW EXECUTION. _check_tp_sl() (live WS SL/TP)
        # now evaluates against raw_bid/raw_ask — the SAME values just
        # validated by _validate_quote_values() a few lines above, before
        # broker_price() ever ran — never the client/marked-up bid/ask.
        # This is the one call-site change that unifies WS SL/TP with
        # Celery's scan_positions_task (already RAW since O.6c-1w) and
        # with every other close path (_order_close/_do_stopout/
        # _do_retail_liquidation, already RAW since O.6c-1s). Closes
        # O.6c-1u's documented "WS = client, Celery = RAW" inconsistency.
        # _check_tp_sl()'s own body is unchanged — its side convention
        # (BUY exits at bid, SELL exits at ask) was already correct, only
        # the price authority passed in was wrong.
        #
        # FIX-05A.1 — closes the FIX-05A residual gap: the broadcast
        # event now carries "source" (FeedManager._broadcast(), FIX-05A.1)
        # so this financial trigger can be gated WITHOUT re-querying
        # self._feed (the approach that broke ~20 pre-existing tests'
        # minimal price_tick() fixtures in the earlier FIX-05A attempt —
        # confirmed by actually running the suite, not assumed). Fail-
        # closed, not fail-open: a missing "source" key is NEVER silently
        # treated as trusted/live — only an explicit, known-real value
        # (never "sim") allows SL/TP to evaluate. _check_tp_sl()'s own
        # signature/body/side-convention are unchanged; only whether it
        # is called at all changes.
        _price_source = event.get("source")
        if _price_source is not None and _price_source != "sim":
            await self._check_tp_sl(symbol, raw_bid, raw_ask)
            # ORDER-MANAGEMENT-V2A — same raw-price, same fail-closed
            # "never on a sim tick" gate as _check_tp_sl() immediately
            # above; sibling evaluator for PendingOrder triggers.
            await self._check_pending_triggers(symbol, raw_bid, raw_ask)
        await self._recalc_account_and_push()

    async def candle_kline(self, event: dict):
        """Receives canonical 1-minute OHLCV sub-bars from exchange kline
        streams (Binance @kline_1m / Kraken ohlc-1). Each event is always a
        1-minute bar (open, still forming, or closed) — this aggregates
        those 1-minute sub-bars into the connection's selected timeframe
        using the same bucket formula as the tick aggregator (_on_tick),
        then reuses _emit_bar() to signal candle_new/candle_update exactly
        like the non-kline path. For timeframe=1m this reduces to exactly
        the prior behavior (one sub-bar per bucket, forwarded as-is)."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        bar = event["data"]
        minute_t = int(bar["time"])
        tf_sec = tf_seconds(self.timeframe)
        bucket = (minute_t // tf_sec) * tf_sec

        acc = self._agg.get(symbol)
        if acc is None or acc.get("tf_sec") != tf_sec or acc.get("t0") != bucket:
            acc = {"t0": bucket, "tf_sec": tf_sec, "minute_bars": {}}
            self._agg[symbol] = acc

        # Binance/Kraken repeat updates for the same still-forming minute with
        # cumulative OHLCV for that minute — overwrite (never sum) per minute
        # key, then recompute the bucket's OHLCV from the distinct minutes
        # seen so far so volume isn't double-counted across repeat updates.
        acc["minute_bars"][minute_t] = {
            "o": float(bar["open"]), "h": float(bar["high"]),
            "l": float(bar["low"]),  "c": float(bar["close"]),
            "v": float(bar.get("volume", 0.0)),
        }
        mbs = acc["minute_bars"]
        ordered = sorted(mbs)
        acc["o"] = mbs[ordered[0]]["o"]
        acc["h"] = max(m["h"] for m in mbs.values())
        acc["l"] = min(m["l"] for m in mbs.values())
        acc["c"] = mbs[ordered[-1]]["c"]
        acc["v"] = sum(m["v"] for m in mbs.values())

        await self._emit_bar(symbol, acc)

    # ---------------- Heartbeat ----------------

    async def _heartbeat_loop(self):
        """Send server ping every 30 s; close stale connections after 90 s silence."""
        PING_INTERVAL = 30
        STALE_TIMEOUT = 90
        while True:
            await asyncio.sleep(PING_INTERVAL)
            now = time.time()
            if now - self._last_msg_ts > STALE_TIMEOUT:
                log.warning("[heartbeat] stale connection for account=%s — closing", self._db_account_id)
                await self.close()
                return
            try:
                await self.send_json({"type": "heartbeat", "ts": int(now)})
            except Exception:
                return

    # ---------------- Agregador de velas ----------------
    def _reset_agg(self, symbol: str):
        self._agg[symbol] = {"t0":None,"o":None,"h":None,"l":None,"c":None,"v":0.0,"tf_sec":tf_seconds(self.timeframe)}

    async def _on_tick(self, symbol: str, price: float, volume: float = 0.0, ts: int | None = None):
        # Exchange-kline symbols send canonical OHLCV via candle_kline().
        # Server-side aggregation from price ticks would produce a second, divergent series.
        # GOLDEN-MARKETDATA-CRYPTO-01 — BTCUSD/ETHUSD are still members of
        # _KLINE_SYMBOLS (symbol_specs.py's exchange_symbol/kraken_symbol
        # fields are kept dormant, unchanged) but are now served LIVE by
        # Massive (tick quotes only, never a kline event — Binance/Kraken
        # are functionally unreachable for them, _try_live_legacy) — so
        # candle_kline() never fires for them anymore. Excluding them here
        # restores real-time candle aggregation from their Massive ticks,
        # exactly like every non-kline symbol already gets. Any other
        # _KLINE_SYMBOLS member (none exist today) keeps the original
        # skip-and-wait-for-candle_kline() behavior, unchanged.
        if symbol in _KLINE_SYMBOLS and symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            return
        if ts is None: ts = int(time.time())
        acc = self._agg.get(symbol)
        if acc is None or acc["tf_sec"] != tf_seconds(self.timeframe):
            self._reset_agg(symbol)
            acc = self._agg[symbol]

        tf_sec = acc["tf_sec"]
        bucket = (ts // tf_sec) * tf_sec

        if acc["t0"] is None:
            acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(volume or 0.0)
            await self._emit_bar(symbol, acc); return

        if bucket == acc["t0"]:
            acc["c"]=price; acc["h"]=max(acc["h"],price); acc["l"]=min(acc["l"],price)
            acc["v"]=float(acc["v"])+float(volume or 0.0)
            await self._emit_bar(symbol, acc); return

        # bucket nuevo
        acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(volume or 0.0)
        await self._emit_bar(symbol, acc)

    async def _emit_bar(self, symbol: str, acc: dict):
        bar = {"time":int(acc["t0"]), "open":float(acc["o"]), "high":float(acc["h"]),
               "low":float(acc["l"]), "close":float(acc["c"])}
        last_time = self._last_bar_time.get(symbol)

        if last_time is None or int(acc["t0"]) > last_time:
            await self.send_json({"type":"candle_new","symbol":symbol,"data":bar})
            self._last_bar_time[symbol] = int(acc["t0"])
        else:
            await self.send_json({"type":"candle_update","symbol":symbol,"data":bar})

        await self.send_json({
            "type":"volume_update","symbol":symbol,"time":int(acc["t0"]),
            "value":float(acc.get("v",0.0)),
            "color":"#26a69a" if acc["c"]>=acc["o"] else "#f44336",
        })

    # ---------------- MASSIVE-CRYPTO-TRADE-CANDLES-01 — trade-based candle (crypto only) ----------------

    def _reset_trade_agg(self, symbol: str):
        self._trade_agg[symbol] = {"t0":None,"o":None,"h":None,"l":None,"c":None,"v":0.0,"tf_sec":tf_seconds(self.timeframe)}

    async def price_trade(self, event: dict):
        """Channel handler for FeedManager._broadcast_trade()'s
        "price.trade" messages (Massive XT — crypto only; never sent for
        Forex, see _massive_crypto_shared_loop()'s XT branch). Chart/
        candle/volume ONLY — deliberately never touches self.bid/
        self.ask/self._bid_state/self._ask_state/self._price_state,
        never calls broker_price()/_check_tp_sl(), never sends a "tick"
        message. Execution/PnL/margin/risk/SL-TP stay entirely on
        price_tick() (Massive XQ), completely unchanged by this method's
        existence — this is the OHLC-plumbing mirror of _on_tick()/
        _emit_bar() above, deliberately a separate accumulator
        (self._trade_agg, never self._agg) and a separate method, not a
        shared/parameterized version of the quote path (same "duplicate,
        don't share" boundary already used throughout this project for
        Forex/Crypto)."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        price = event["price"]
        size  = event.get("size", 0.0)
        ts    = event["time"]

        acc = self._trade_agg.get(symbol)
        if acc is None or acc["tf_sec"] != tf_seconds(self.timeframe):
            self._reset_trade_agg(symbol)
            acc = self._trade_agg[symbol]

        tf_sec = acc["tf_sec"]
        bucket = (ts // tf_sec) * tf_sec

        if acc["t0"] is None:
            acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(size or 0.0)
            await self._emit_trade_bar(symbol, acc); return

        if bucket == acc["t0"]:
            acc["c"]=price; acc["h"]=max(acc["h"],price); acc["l"]=min(acc["l"],price)
            acc["v"]=float(acc["v"])+float(size or 0.0)
            await self._emit_trade_bar(symbol, acc); return

        # bucket nuevo — MASSIVE-CRYPTO-TRADE-CANDLES-01 §7: no trade in
        # a bucket simply means no candle_new/candle_update for it — the
        # next real trade, whatever bucket it falls in, opens a fresh
        # one exactly like this branch already does. Never a synthetic/
        # carried-forward bar.
        acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(size or 0.0)
        await self._emit_trade_bar(symbol, acc)

    async def _emit_trade_bar(self, symbol: str, acc: dict):
        # Reuses self._last_bar_time (the "have we already sent this
        # bucket's candle_new" bookkeeping _emit_bar() also uses) — safe
        # because a symbol is either a quote-driven (Forex) or a trade-
        # driven (crypto) candle source, never both, so the two writers
        # can never collide on the same key.
        bar = {"time":int(acc["t0"]), "open":float(acc["o"]), "high":float(acc["h"]),
               "low":float(acc["l"]), "close":float(acc["c"])}
        last_time = self._last_bar_time.get(symbol)

        if last_time is None or int(acc["t0"]) > last_time:
            await self.send_json({"type":"candle_new","symbol":symbol,"data":bar})
            self._last_bar_time[symbol] = int(acc["t0"])
        else:
            await self.send_json({"type":"candle_update","symbol":symbol,"data":bar})

        await self.send_json({
            "type":"volume_update","symbol":symbol,"time":int(acc["t0"]),
            "value":float(acc.get("v",0.0)),
            "color":"#26a69a" if acc["c"]>=acc["o"] else "#f44336",
        })

    # ---------------- Historia ----------------

    async def _send_history_or_unavailable(self, symbol: str, timeframe: str, hist, phase: str = "complete") -> None:
        """FIX-05B.1 — single dispatch point used by all 3 history call-sites
        (change_symbol/change_timeframe/load_history). Never closes the WS,
        never touches the subscription/live-tick flow — this is one more
        send_json() among many on an already-open connection, same as any
        other informational message type.

        CHART-HISTORY-INSTANT-LOAD-01 — `phase` is "initial" for the
        fast-first-paint page-1-only send, "complete" (default —
        unchanged for every pre-existing caller) once full depth has
        been merged in. Only ever attached to a real "history" message;
        "history_unavailable" carries no phase — it's a terminal failure
        signal, not a partial result the frontend should expect a
        follow-up for."""
        if hist is None:
            # FIX-05B.2-C — a symbol with NO real provider configured at all
            # (neither the crypto kline chain nor the Massive forex
            # allowlist) is "no_real_history"; a symbol Massive (or the
            # crypto chain) IS configured for, but which failed/returned
            # empty this time, is "provider_unavailable" — same contract,
            # now covering both provider families.
            unsupported = (
                symbol not in _KLINE_SYMBOLS
                and symbol not in _MASSIVE_ENABLED_SYMBOLS
                and symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS
            )
            reason = "no_real_history" if unsupported else "provider_unavailable"
            await self.send_json({
                "type": "history_unavailable", "symbol": symbol, "timeframe": timeframe, "reason": reason,
            })
        else:
            await self.send_json({"type": "history", "symbol": symbol, "timeframe": timeframe, "phase": phase, "data": hist})

    async def generate_history(self, symbol, timeframe, bars=200) -> "list[dict] | None":
        """FIX-05B.1/FIX-05B.2 — real closed candles only, or None (never
        fabricated).

        None means "no real history available for this symbol/timeframe
        right now" — callers must send history_unavailable, never fall back
        to a synthetic series. Distinguishes cleanly from a real-but-short
        result (fewer than `bars` closed candles after filtering out the
        still-forming one) — that is a valid, real, non-None result, never
        padded back up to `bars`.

        FIX-05B.2-C / GOLDEN-MARKETDATA-CRYPTO-01 — symbol dispatch: three
        disjoint provider paths, never merged or duplicated into a single
        framework. Massive-crypto (_MASSIVE_CRYPTO_ENABLED_SYMBOLS,
        BTCUSD/ETHUSD) is checked FIRST — ahead of the legacy exchange-
        kline chain (_KLINE_SYMBOLS) it now supersedes at runtime — so
        crypto history never falls back to Binance/Kraken while Massive is
        configured; Massive forex (_MASSIVE_ENABLED_SYMBOLS) remains the
        third, unrelated path. A symbol in none of the three returns None
        without attempting any network call."""
        if symbol in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            hist = await self._feed.fetch_massive_crypto_history(symbol, interval=timeframe, limit=bars)
        elif symbol in _KLINE_SYMBOLS:
            hist = await self._feed.fetch_kline_history(symbol, interval=timeframe, limit=bars)
        elif symbol in _MASSIVE_ENABLED_SYMBOLS:
            hist = await self._feed.fetch_massive_history(symbol, interval=timeframe, limit=bars)
        else:
            # No real historical provider configured for this symbol today
            # (e.g. XAU pending a future block) — no random-walk fallback.
            return None

        hist = _closed_only(hist, tf_seconds(timeframe))
        if not hist:
            log.warning("[consumer] real history unavailable for %s %s (provider failure or all-open)", symbol, timeframe)
            return None

        # Snap the in-memory price state to the last CLOSED bar so bid/ask
        # calculations start at a real price. FIX-05C — this is display/
        # bid-ask-seed state only, never the live financial quote authority
        # (liveMid, frontend-only, tick-source-gated) — untouched here.
        last_close = hist[-1]["close"]
        spr = spread_for(symbol)
        _, dec = step_decimals_for(symbol)
        self._price_state[symbol] = last_close
        self._bid_state[symbol], self._ask_state[symbol] = broker_price(
            symbol,
            round(last_close - spr / 2, dec),
            round(last_close + spr / 2, dec),
            markup_pips=float(self.account.get("spread_pips", 0.0) or 0.0),
        )
        return hist

    async def generate_history_first_page(self, symbol, timeframe, bars=200) -> "tuple[list[dict] | None, object]":
        """CHART-HISTORY-INSTANT-LOAD-01 — first-paint half of
        generate_history(). Same three-way dispatch, but issues exactly
        ONE REST call for the Massive-backed paths (via the *_first_page
        fetchers) instead of the full multi-page fetch, so the caller can
        paint a chart in ~0.3s instead of waiting the full pagination
        depth. Returns (closed_bars_or_None, cursor_or_None); cursor is
        None whenever there is nothing left to fetch (kline path,
        unsupported symbol, provider failure, or the first page already
        covered everything) — callers use that to decide whether a depth
        task is worth starting at all.

        Price-state snapping happens here, exactly once, on whichever
        closed bars are freshest — the depth phase (_complete_history_
        depth, below) only ever prepends OLDER bars behind this result
        and must never re-snap."""
        cursor = None
        if symbol in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            hist, cursor = await self._feed.fetch_massive_crypto_history_first_page(symbol, interval=timeframe, limit=bars)
        elif symbol in _KLINE_SYMBOLS:
            # Kline chain is already a single request — no first-page/
            # depth split exists or is needed for it.
            hist = await self._feed.fetch_kline_history(symbol, interval=timeframe, limit=bars)
        elif symbol in _MASSIVE_ENABLED_SYMBOLS:
            hist, cursor = await self._feed.fetch_massive_history_first_page(symbol, interval=timeframe, limit=bars)
        else:
            return None, None

        hist = _closed_only(hist, tf_seconds(timeframe))
        if not hist:
            log.warning("[consumer] real history unavailable for %s %s (provider failure or all-open)", symbol, timeframe)
            return None, cursor

        last_close = hist[-1]["close"]
        spr = spread_for(symbol)
        _, dec = step_decimals_for(symbol)
        self._price_state[symbol] = last_close
        self._bid_state[symbol], self._ask_state[symbol] = broker_price(
            symbol,
            round(last_close - spr / 2, dec),
            round(last_close + spr / 2, dec),
            markup_pips=float(self.account.get("spread_pips", 0.0) or 0.0),
        )
        return hist, cursor

    async def _complete_history_depth(self, symbol, timeframe, generation, cursor) -> None:
        """CHART-HISTORY-INSTANT-LOAD-01 — runs detached (asyncio.create_
        task, never awaited by receive()) to fetch the remaining
        pagination depth after generate_history_first_page() already
        delivered a fast first paint. Only ever created when generate_
        history_first_page() returned a non-None cursor. Guarded by
        generation+symbol+timeframe so a stale depth fetch (superseded
        by another switch before it finished) is silently dropped
        instead of overwriting a chart it no longer applies to — never
        re-snaps price state, which was already set from the fresher
        first page."""
        try:
            if symbol in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
                hist = await self._feed.fetch_massive_crypto_history_remaining(cursor)
            else:
                hist = await self._feed.fetch_massive_history_remaining(cursor)
            hist = _closed_only(hist, tf_seconds(timeframe))
            if (
                hist
                and generation == self._history_generation
                and symbol == self.symbol
                and timeframe == self.timeframe
            ):
                await self._send_history_or_unavailable(symbol, timeframe, hist, phase="complete")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("[consumer] history depth fetch failed for %s %s", symbol, timeframe, exc_info=True)

    def _start_history_depth(self, symbol, timeframe, cursor) -> None:
        """CHART-HISTORY-INSTANT-LOAD-01 — cancels any previous in-flight
        depth fetch and, only if `cursor` is not None (a single-page
        result needs no depth task at all — the first page already is
        the final history for that request), starts a new detached one
        tagged with the current generation."""
        old = self._history_depth_task
        if old and not old.done():
            old.cancel()
        self._history_depth_task = (
            asyncio.create_task(self._complete_history_depth(symbol, timeframe, self._history_generation, cursor))
            if cursor is not None else None
        )

    # ---------------- Estado de precio ----------------

    def _seed_price_state(self, symbol: str) -> None:
        """Seed bid/ask/mid from FeedManager on connect / symbol change."""
        raw_bid = self._feed.last_bid(symbol)
        raw_ask = self._feed.last_ask(symbol)
        self._bid_state[symbol], self._ask_state[symbol] = broker_price(
            symbol, raw_bid, raw_ask,
            markup_pips=float(self.account.get("spread_pips", 0.0) or 0.0),
        )
        self._price_state[symbol] = self._feed.last_price(symbol)

    def set_state(self, symbol, bid: float, ask: float, mid: float):
        self._bid_state[symbol]   = float(bid)
        self._ask_state[symbol]   = float(ask)
        self._price_state[symbol] = float(mid)

    def ensure_state(self, symbol) -> float:
        """Mid price — for candle aggregation and chart line only."""
        return self._price_state.get(symbol, base_price_for(symbol))

    def get_bid(self, symbol) -> float:
        return self._bid_state.get(symbol, base_price_for(symbol))

    def get_ask(self, symbol) -> float:
        return self._ask_state.get(symbol, base_price_for(symbol))

    def exec_price(self, symbol: str, side: str) -> float:
        """Fill price when OPENING: buy fills at ask, sell fills at bid.

        O.6c-1aa — no longer the financial execution authority. Reads
        self._bid_state/self._ask_state (client/marked-up, O.6c-1u's
        documented raw-vs-markup split) — kept defined, unused by any
        money-moving path in this file as of O.6c-1aa (see
        _raw_exec_price() below), for any future non-financial/display
        use. NO call site in this file may use this for a Position,
        Trade, LedgerEntry, or balance decision."""
        return self.get_ask(symbol) if side == "buy" else self.get_bid(symbol)

    def _raw_exec_price(self, symbol: str, side: str) -> "float | None":
        """O.6c-1aa — the single financial authority for OPENING a
        position: BUY -> raw validated ask, SELL -> raw validated bid
        (the side a real market order actually crosses). Mirrors
        _feed_close_price()'s (O.6c-1w) fail-safe contract exactly —
        routes through self._feed.get_validated_quote(symbol), the same
        structural + Capa A plausibility gate — and returns None on ANY
        failure (absent/stale/cross-symbol/malformed), never
        base_price_for(), never self._bid_state/self._ask_state.
        Side convention is the OPEN-side mirror of _feed_close_price()'s
        CLOSE-side convention: opening a BUY crosses ask; closing a BUY
        crosses bid — same symbol, same validated quote, opposite side
        of the same round trip.

        FIX-05A — quote.source=="sim" (FeedManager's synthetic-continuity
        fallback, used when the market is closed or the real provider is
        down) is never financial authority. Treated identically to "no
        quote at all" — same None/price_unavailable contract, never a
        second error path."""
        quote = self._feed.get_validated_quote(symbol)
        if quote is None or quote.source == "sim":
            return None
        return quote.ask if side == "buy" else quote.bid

    def close_price(self, symbol: str, side: str) -> float:
        """Fill price when CLOSING: buy closes at bid, sell closes at ask."""
        return self.get_bid(symbol) if side == "buy" else self.get_ask(symbol)

    def _capture_pricing_context(self, symbol: str, *, profile: str) -> dict:
        """SPREAD-02 — assembles the pricing context for an open/close at
        *symbol* from state already sitting in memory: raw/executable
        bid-ask, and the BrokerSpreadConfig/provider snapshot price_tick()
        already captured for this symbol's LAST tick (self._pricing_snapshot_state).

        Deliberately does NOT call spread_pips_for()/provider_state_for()
        here — doing so would re-read BrokerSpreadConfig/F13 observability
        at order time, which can have changed since the tick that actually
        produced executable_bid/executable_ask, mislabeling an
        already-executed price with a config that never produced it (see
        tick_pricing_snapshot()'s docstring). If no tick was ever seen for
        this symbol, the snapshot is simply empty — base/markup/provider
        stay None, never fabricated from a fresh read.

        Never raises and never affects execution — a failure here yields a
        minimal context dict, never an exception propagated to the caller."""
        try:
            snapshot = self._pricing_snapshot_state.get(symbol) or {}
            provider_id = snapshot.get("provider_id")
            return pricing_ctx.build_pricing_context(
                raw_bid=self._raw_bid_state.get(symbol),
                raw_ask=self._raw_ask_state.get(symbol),
                executable_bid=self._bid_state.get(symbol),
                executable_ask=self._ask_state.get(symbol),
                base_spread_pips=snapshot.get("base_spread_pips"),
                account_markup_pips=snapshot.get("account_markup_pips"),
                min_spread_pips=snapshot.get("min_spread_pips"),
                max_spread_pips=snapshot.get("max_spread_pips"),
                effective_before_bounds=snapshot.get("effective_before_bounds"),
                effective_after_bounds=snapshot.get("effective_after_bounds"),
                dynamic_spread_enabled=snapshot.get("dynamic_spread_enabled"),
                session_multiplier=snapshot.get("session_multiplier"),
                source_multiplier=snapshot.get("source_multiplier"),
                stale_multiplier=snapshot.get("stale_multiplier"),
                volatility_multiplier=snapshot.get("volatility_multiplier"),
                liquidity_multiplier=snapshot.get("liquidity_multiplier"),
                manual_multiplier=snapshot.get("manual_multiplier"),
                reason_codes=snapshot.get("reason_codes"),
                decision_id=snapshot.get("decision_id"),
                profile_id=snapshot.get("profile_id"),
                provider_id=provider_id,
                source_state=snapshot.get("source_state"),
                router_provider=provider_id,
                pricing_timestamp=self._pricing_ts_state.get(symbol),
                pricing_profile=profile,
            )
        except Exception as exc:
            log.debug("[pricing_context] capture failed for %s profile=%s (non-fatal): %r",
                      symbol, profile, exc)
            return {"schema_version": pricing_ctx.SCHEMA_VERSION,
                    "pricing_profile": pricing_ctx.PROFILE_CAPTURE_FAILED}

    # ---------------- Órdenes / Cuenta ----------------
    async def _order_new(self, data: dict):
        sym  = data.get("symbol", self.symbol)
        side = str(data.get("side","")).lower()   # 'buy' | 'sell'  (in-memory stays lowercase)
        qty  = float(data.get("qty",0) or 0)
        sl   = data.get("sl")
        tp   = data.get("tp")

        if sym not in _ALLOWED_SYMBOLS:
            await self.send_json({"type": "error", "code": "invalid_symbol", "message": "simbolo_no_permitido"})
            return

        # FIX-05A — market-session policy gate for NEW orders only (never
        # applied to _order_close()/manual close/SL/TP/stopout — those
        # must keep working under CLOSE_ONLY, and their own financial
        # quote gate above already independently blocks them under a
        # genuinely absent/sim quote). Reuses the existing FOUNDATION-02
        # OrderPolicy contract (evaluate_market_session_for_symbol(),
        # market_data/sessions/service.py) — confirmed by the FIX-05
        # design lock to already exist but have zero real consumer before
        # this change. Pure, synchronous, no I/O, never raises (degrades
        # to HALT_NEW_ORDERS on any internal evaluation error).
        from market_data.contracts import OrderPolicy
        from market_data.sessions.service import evaluate_market_session_for_symbol
        _session = evaluate_market_session_for_symbol(sym)
        if _session.order_policy in (OrderPolicy.MARKET_CLOSED, OrderPolicy.HALT_NEW_ORDERS):
            await self.send_json({
                "type": "error", "code": "market_closed",
                "message": "mercado_cerrado_o_nuevas_ordenes_bloqueadas",
            })
            return
        if _session.order_policy == OrderPolicy.CLOSE_ONLY:
            await self.send_json({
                "type": "error", "code": "close_only",
                "message": "solo_se_permiten_cierres_en_este_momento",
            })
            return

        # O.6c-1w-b — RAW MARKET QUOTE -> VALIDATE -> broker markup ->
        # execution. exec_price() below reads self._bid_state/
        # self._ask_state (markup-derived, per-connection). price_tick()
        # (O.6c-1w-b) already refuses to write into that cache from an
        # invalid raw tick, but this connection may never have received
        # ANY tick yet for *sym* — e.g. immediately after connect/
        # change_symbol, before the first live tick arrives
        # (_seed_price_state() seeds directly from the raw feed,
        # unvalidated). Checking the RAW feed's CURRENT validity here,
        # before anything else, closes that cold-start gap — same
        # "reject if no valid quote" contract _order_close() already has
        # (O.6c-1w), same error code. Returns before touching the rate
        # limiter, margin guard, risk engine, or DB — zero Position,
        # zero Trade, zero LedgerEntry, zero balance/margin change.
        if self._feed.get_validated_quote(sym) is None:
            await self.send_json({
                "type": "error", "code": "price_unavailable",
                "message": "no_se_pudo_abrir_precio_no_disponible",
            })
            return

        # Rate limit: max 10 new orders per 10 seconds per account (Redis sliding window)
        if self._db_account_id:
            import django.conf as _dc
            _redis_url = getattr(_dc.settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
            from .observability import order_rate_check as _rate_check
            loop = asyncio.get_event_loop()
            allowed = await loop.run_in_executor(
                None, _rate_check, _redis_url, self._db_account_id
            )
            if not allowed:
                await self.send_json({"type": "error", "code": "rate_limited", "message": "demasiadas_ordenes"})
                return

        if side not in ("buy","sell") or qty <= 0:
            await self.send_json({"type":"error","code":"invalid_order","message":"orden_invalida"})
            return

        # O.6c-1aa — UNIFIED RAW EXECUTION. self._raw_exec_price() is now
        # the single financial authority for this order — SL/TP
        # validation, the margin guard, and the price ultimately written
        # to Position.avg_price all read this SAME value, fetched ONCE.
        # Never self.exec_price()/self._bid_state/self._ask_state
        # (client/marked-up) for any of these. Re-checked here (side is
        # now known, unlike the symbol-only gate above) — None on any
        # invalid/stale/cross-symbol quote, same fail-safe contract as
        # _order_close() (O.6c-1w): reject before the rate limiter,
        # margin guard, risk engine, or DB. Zero Position, zero Trade,
        # zero LedgerEntry, zero balance/margin change.
        raw_exec_px = self._raw_exec_price(sym, side)
        if raw_exec_px is None:
            await self.send_json({
                "type": "error", "code": "price_unavailable",
                "message": "no_se_pudo_abrir_precio_no_disponible",
            })
            return

        # PANEL-04 — server-side SL/TP validation. Never trust the
        # frontend: a crafted WS payload can send NaN/Infinity/negative/
        # wrong-direction values regardless of what the <input
        # type=number> element normally constrains in a real browser. No
        # minimum-distance policy is enforced — see _validate_sl_tp's
        # docstring for why.
        _sl_tp_ok, _sl_tp_code, _sl_tp_msg = _validate_sl_tp(
            side, sl, tp, raw_exec_px,
        )
        if not _sl_tp_ok:
            await self.send_json({"type": "error", "code": _sl_tp_code, "message": _sl_tp_msg})
            return

        # Fast in-memory check (margin, min qty)
        ok, reason = self._pretrade_check(sym, side, qty)
        if not ok:
            await self.send_json({"type":"error","code":reason,"message":reason})
            await self._recalc_account_and_push()
            await self._refresh_and_send_positions()
            return

        # Phase 6B.1 — per-product margin guard (snapshot-based, pure, no DB)
        eq_now = self.account["balance"] + self._unrealized_pnl_total()
        mg_now = self._margin_used_total()
        _spec  = get_spec(sym)
        _guard_ok, _guard_code, _guard_msg, _guard_details = _compute_pretrade_margin_guard(
            sym, qty, raw_exec_px, eq_now, mg_now,
            self.account, _spec.max_leverage, _spec.contract_size,
            max_margin_per_trade_pct=self.account.get("max_margin_per_trade_pct", _DEFAULT_MAX_MARGIN_PER_TRADE_PCT),
            max_total_margin_pct=self.account.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
            account_currency=self.account.get("currency", "USD"),
        )
        if not _guard_ok:
            await self.send_json({"type": "error", "code": _guard_code, "message": _guard_msg})
            return

        # ── Position risk assessment ──────────────────────────────────
        # eq_now / mg_now already computed above — reuse them.
        lev    = max(1, int(self.account.get("leverage", 50)))
        risk_assessment = await self._db_evaluate_risk(sym, qty, eq_now, mg_now, lev)
        risk_level = risk_assessment.get("risk_level", "LOW")

        if risk_level == "EXTREME":
            # Reject order without suspending account
            await self.send_json({
                "type": "order_rejected",
                "code": "extreme_risk",
                **risk_assessment,
            })
            return

        if risk_level == "HIGH" and not data.get("risk_confirmed"):
            # Require explicit client confirmation before executing
            await self.send_json({
                "type": "risk_warning",
                "requires_confirm": True,
                "pending_side": side,
                "pending_qty": qty,
                "pending_symbol": sym,
                **risk_assessment,
            })
            return
        # ─────────────────────────────────────────────────────────────

        # Risk engine gate (DB: lot size, positions count, daily dd, max dd, account status)
        risk_errors = await self._db_validate_order_risk(qty, len(self._positions), sym)
        _blocking = [e for e in risk_errors if e.get("blocking", True)]
        _warnings  = [e for e in risk_errors if not e.get("blocking", True)]

        if _blocking:
            first = _blocking[0]
            await self.send_json({
                "type": "error",
                "code": first["code"],
                "message": first["message"],
            })
            if self.account.get("status") not in ("Activo",):
                await self.send_json({
                    "type": "account:suspended",
                    "status": self.account["status"],
                    "reason": first["code"],
                })
            return

        # Non-blocking warnings (RETAIL exposure/DD warnings) — order still proceeds
        if _warnings:
            await self.send_json({
                "type": "risk:warning",
                "warnings": [{"code": w["code"], "message": w["message"]} for w in _warnings],
            })

        dec = step_decimals_for(sym)[1]
        px_exec = round(raw_exec_px, dec)

        # RISK-02 — broker-wide risk limits are evaluated INSIDE
        # _db_open_position_atomic below (step 8.5, under BrokerRiskLock),
        # never here. A separate pre-lock check at this point was the
        # original RISK-02 design and had a genuine TOCTOU race: two
        # concurrent opens on different accounts could each read the same
        # broker-wide exposure before either wrote, both pass, and jointly
        # exceed a broker-wide limit. There is exactly ONE place broker-wide
        # risk is evaluated now — see _db_open_position_atomic's docstring.

        commission  = self.commission_for(sym, qty, px_exec)
        new_balance = self.account["balance"] - commission

        pricing_context = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_OPEN)

        try:
            result = await self._db_open_position_atomic(
                sym, side, qty, px_exec, sl, tp, commission, new_balance,
                pricing_context=pricing_context,
            )
        except Exception as exc:
            log.error("[order_new] DB open failed for %s %s: %s", side, sym, exc, exc_info=True)
            await self.send_json({"type": "error", "code": "execution_failed",
                                  "message": "no_se_pudo_abrir_posicion"})
            return

        # PANEL-02 — _db_open_position_atomic() is now the authoritative
        # gate (fresh, lock-protected margin/position-count/status check).
        # A rejection here means NOTHING was written (no Position, no
        # commission, no Trade/LedgerEntry/BrokerLedger) — mirror the
        # fast pre-lock guard's rejection shape (error + return, no
        # memory mutation, no recalc/refresh needed since nothing changed).
        if not result.get("ok", True):
            await self.send_json({
                "type": "error",
                "code": result.get("error_code", "order_rejected"),
                "message": result.get("message", "orden_rechazada"),
            })
            return

        # BOOK-04d — Routing Engine audit trail. Purely observational:
        # "the routing decision associated with an accepted open was
        # recorded" — not the completion of the WebSocket response. Runs
        # strictly after the open above already committed (result["ok"]
        # is True), never inside _db_open_position_atomic()'s transaction
        # or locks, and before the memory mutation/order_ack/order_fill
        # below — a slow or failed audit write here must never delay or
        # affect what the client is about to receive.
        #
        # Uses exclusively what BOOK-04b/04c's result dict already
        # exposes (routing_decision_id/position_id/merged) — never
        # re-reads RoutingDecision or Position, never re-opens the open
        # transaction. If routing_decision_id is None (flag was off, the
        # writer failed, or the principal link failed — all already
        # fail-open in BOOK-04b), no ROUTING event is created at all.
        #
        # record_event() is already fail-open internally, but this own
        # try/except additionally covers argument construction and the
        # database_sync_to_async scheduling/await itself — a failure at
        # any of those points is absorbed here, never allowed to affect
        # Position, RoutingDecision, balance, margin, or the order_ack/
        # order_fill sent further below.
        _routing_decision_id = result.get("routing_decision_id")
        if _routing_decision_id is not None:
            try:
                await self._db_record_routing_audit_event(
                    account_id=self._db_account_id,
                    symbol=sym,
                    routing_decision_id=_routing_decision_id,
                    position_id=result.get("position_id"),
                    merged=bool(result.get("merged")),
                )
            except Exception as _routing_audit_exc:
                log.warning(
                    "[routing_engine] audit event failed decision=%s pos=%s: %s",
                    _routing_decision_id, result.get("position_id"), _routing_audit_exc,
                )

        # BOOK-05c — Liquidity Engine Shadow Mode. Gated by
        # settings.LIQUIDITY_ENGINE_ENABLED (default False — see
        # trx_simulator/settings.py). Purely observational: "a simulated
        # hedge evaluation for this accepted open was recorded" — never
        # affects execution, price, margin, commission, or the routing
        # decision itself (RoutingDecision is never written by this
        # block — see docs/BOOK_05_IMPLEMENTATION_PLAN.md, Principio
        # rector). Runs strictly after the open above already committed,
        # after the routing audit block above (placed after purely to
        # avoid interleaving with already-shipped BOOK-04d code — the
        # order between the two has no functional dependency), and
        # before the memory mutation/order_ack/order_fill below. Same
        # discipline as the routing audit block: a slow or failed
        # evaluation here must never delay or affect what the client is
        # about to receive, and never affects whether the position that
        # already opened continues its normal flow.
        #
        # Only runs when a real routing_decision_id exists (flag off,
        # writer failure, or principal link failure upstream all already
        # leave it None — same guard already used above for the audit
        # event) — a simulated hedge with nothing real to link to would
        # be meaningless.
        from django.conf import settings as _settings
        _liquidity_decision = None
        if getattr(_settings, "LIQUIDITY_ENGINE_ENABLED", False) and _routing_decision_id is not None:
            try:
                _liquidity_decision = await self._db_record_liquidity_decision(
                    routing_decision_uuid=_routing_decision_id,
                    account_id=self._db_account_id,
                    position_id=result.get("position_id"),
                    symbol=sym, side=side, qty=qty, price=px_exec,
                )
            except Exception as _liquidity_exc:
                log.warning(
                    "[liquidity_engine] shadow evaluation failed decision=%s pos=%s: %s",
                    _routing_decision_id, result.get("position_id"), _liquidity_exc,
                )

            # BOOK-05e.2 — Liquidity Engine audit trail. Purely
            # observational: "a LiquidityDecision was actually recorded
            # for this accepted open" — a second, independent try/except
            # from the write above (same rationale BOOK-04d already
            # established: catching an audit-event failure inside the
            # writer's own except would misattribute it as a "shadow
            # evaluation failed" instead of what it really is). Only
            # runs when _liquidity_decision is not None — covers, without
            # needing to distinguish them, every reason the write above
            # could have produced nothing (flag off is already excluded
            # by the outer `if`; no qualifying provider; no resolvable
            # RoutingDecision; the writer's own internal fail-open). No
            # atomic() needed here beyond record_liquidity_event()'s own
            # internal one — this code runs after
            # _db_open_position_atomic()'s transaction already committed,
            # so there is no outer transaction to protect. Return value
            # ignored, same as the routing audit event above.
            if _liquidity_decision is not None:
                try:
                    await self._db_record_liquidity_audit_event(
                        account_id=self._db_account_id,
                        symbol=sym,
                        liquidity_decision_id=_liquidity_decision.decision_id,
                        routing_decision_id=_routing_decision_id,
                        position_id=result.get("position_id"),
                    )
                except Exception as _liquidity_audit_exc:
                    log.warning(
                        "[liquidity_engine] audit event failed decision=%s pos=%s: %s",
                        _liquidity_decision.decision_id, result.get("position_id"),
                        _liquidity_audit_exc, exc_info=True,
                    )

        # BOOK-06c — Dealing Desk Decision Engine integration. Purely
        # observational and simulated: records the internal risk
        # classification of this position's exposure (never a real
        # hedge, never affects execution, price, margin, commission, or
        # P&L — see DealingDeskDecision's own docstring and BOOK-06
        # FASE 0, approved 2026-07-26). Runs strictly after the open
        # above already committed, after the routing/liquidity blocks
        # above (placed after purely to avoid interleaving with already-
        # shipped BOOK-04d/05c/05e.2 code — no functional dependency on
        # their internal ordering), and before the memory mutation/
        # order_ack/order_fill below.
        #
        # Deliberately gated ONLY on `_routing_decision_id is not None`
        # — independent of LIQUIDITY_ENGINE_ENABLED and of whether
        # `_liquidity_decision` ended up None. BOOK-06c design (approved
        # 2026-07-27): exactly one DealingDeskDecision per RoutingDecision
        # created, regardless of the Liquidity Engine's own configuration
        # — LiquidityDecision is an optional INPUT to the decision
        # (has_liquidity_decision), never a precondition for whether a
        # row is written at all. This preserves a complete history of
        # every Dealing Desk evaluation.
        #
        # record_dealing_desk_decision() is already fail-open internally,
        # but this own try/except additionally covers argument
        # construction and the database_sync_to_async scheduling/await
        # itself — a failure at any of those points is absorbed here,
        # never allowed to affect Position, RoutingDecision, balance,
        # margin, or the order_ack/order_fill sent further below.
        if _routing_decision_id is not None:
            try:
                await self._db_record_dealing_desk_decision(
                    routing_decision_uuid=_routing_decision_id,
                    account_id=self._db_account_id,
                    position_id=result.get("position_id"),
                    symbol=sym,
                    has_liquidity_decision=_liquidity_decision is not None,
                    liquidity_decision_id=(
                        _liquidity_decision.id if _liquidity_decision is not None else None
                    ),
                )
            except Exception as _dealing_desk_exc:
                log.warning(
                    "[dealing_desk] decision failed routing_decision=%s pos=%s: %s",
                    _routing_decision_id, result.get("position_id"), _dealing_desk_exc, exc_info=True,
                )

        # DB committed — safe to mutate memory now.
        # Use authoritative balance from DB (returned by _db_open_position_atomic),
        # falling back to pre-computed value only for demo sessions (no _db_account_id).
        self.account["balance"] = result.get("new_balance", new_balance)
        db_pos_id = result["position_id"] or self._order_seq
        self._order_seq += 1

        if self.account.get("netting_mode"):
            self._open_or_update_position(sym, side, qty, px_exec, sl, tp, position_id=db_pos_id)
        else:
            self._create_position(sym, side, qty, px_exec, sl, tp, position_id=db_pos_id)

        await self.send_json({"type":"order_ack","order_id":db_pos_id,"symbol":sym,"side":side,"qty":qty,"status":"accepted"})
        await self.send_json({"type":"order_fill","order_id":db_pos_id,"symbol":sym,"side":side,"qty":qty,"price":px_exec,
                              "commission":commission,"ts":int(time.time())})

        await self._recalc_account_and_push()
        await self._refresh_and_send_positions()

    @database_sync_to_async
    def _db_get_closed_trades(self):
        """
        FIX-HISTORY-AUTO-CLOSE-SYNC-01 — thin sync wrapper around
        services.history.closed_trades_for_account(), decorated
        database_sync_to_async for the same reason every other _db_*
        method in this class is: the ORM query inside is synchronous,
        and every caller here is `async def` — calling it directly would
        raise SynchronousOnlyOperation. Same pattern, no new one.
        """
        from .services.history import closed_trades_for_account
        return closed_trades_for_account(self._db_account_id)

    async def _send_closed_trades_snapshot(self):
        """
        FIX-HISTORY-AUTO-CLOSE-SYNC-01 — the get_closed_trades response.
        Never raises to the client: a DB failure here degrades to an
        empty snapshot (the frontend's reconcileClosedTrades([]) is a
        documented no-op, never a wipe of existing local History) rather
        than dropping the connection or leaving the client's request
        unanswered.
        """
        if not self._db_account_id:
            await self.send_json({"type": "closed_trades_snapshot", "trades": []})
            return
        try:
            trades = await self._db_get_closed_trades()
        except Exception as exc:
            log.error("[get_closed_trades] failed for account=%s: %r",
                      self._db_account_id, exc, exc_info=True)
            trades = []
        await self.send_json({"type": "closed_trades_snapshot", "trades": trades})

    @database_sync_to_async
    def _db_record_routing_audit_event(self, *, account_id, symbol, routing_decision_id,
                                        position_id, merged):
        """
        BOOK-04d — records "a routing decision associated with an accepted
        open was registered" as an institutional BrokerAuditEvent, under
        Category.ROUTING. Deliberately thin: only routing_decision_id/
        position_id/merged — never re-reads RoutingDecision or Position,
        never includes inputs_snapshot/book/reason_code/engine_version
        (see docs/BOOK_04_IMPLEMENTATION_PLAN.md, BOOK-04d contract).

        Sync method, decorated database_sync_to_async precisely because
        _order_new() (its only caller) is `async def` without that
        decorator, while record_event()'s ORM writes are synchronous —
        calling it directly from _order_new()'s body would raise
        SynchronousOnlyOperation. Same pattern already used by every
        other _db_* method in this class.

        routing_decision_id is a uuid.UUID (RoutingDecision.decision_id)
        — stringified before going into `metadata`, a plain JSONField
        (no DjangoJSONEncoder), which cannot serialize a raw UUID object.
        """
        from . import broker_audit as _audit
        _audit.record_routing_event(
            event_type=_audit.EV_ROUTING_DECISION_RECORDED,
            description=(
                f"Routing decision recorded for {symbol} "
                f"(position_id={position_id}, merged={merged})"
            ),
            account_id=account_id, symbol=symbol,
            source_module="simulator.consumers",
            metadata={
                "routing_decision_id": str(routing_decision_id),
                "position_id": position_id,
                "merged": merged,
            },
        )

    @database_sync_to_async
    def _db_record_liquidity_decision(self, *, routing_decision_uuid, account_id,
                                       position_id, symbol, side, qty, price):
        """
        BOOK-05c — resolves the inputs needed to evaluate a simulated
        hedge for this accepted open, and persists the result via
        liquidity_engine.record_liquidity_decision() — the single write
        point for LiquidityDecision (see that function's own docstring).

        Sync method, decorated database_sync_to_async for the same
        reason as _db_record_routing_audit_event above — its caller,
        _order_new(), is `async def`.

        Never writes to RoutingDecision — routing_decision_uuid (a
        uuid.UUID, RoutingDecision.decision_id) is only ever used to
        resolve the real primary key via a read-only lookup, exactly
        once. Never queries Position or TradingAccount beyond the two
        reads below (TraderScore.routing_profile, active
        LiquidityProvider rows) — symbol/side/qty/price are the same
        values _order_new() already validated and used to open the
        position, never re-derived.

        No internal try/except: any exception here (unknown symbol from
        market_data.symbol_specs.get_spec, or anything else) propagates
        to _order_new()'s own try/except around the await of this
        method — same fail-safe boundary already relied upon for the
        routing audit event above. record_liquidity_decision() itself
        never raises (fail-open, matches record_routing_decision()'s
        contract) — the only exceptions that can reach the caller here
        come from the read-only resolution steps below, never from the
        write itself.

        Returns the created LiquidityDecision, or None if no routing
        decision could be resolved, no provider qualified, or the write
        failed.
        """
        from decimal import Decimal

        from market_data.symbol_specs import get_spec

        from . import liquidity_engine as _liquidity_engine
        from .models import LiquidityProvider, RoutingDecision, TraderScore

        routing_decision_pk = (
            RoutingDecision.objects.filter(decision_id=routing_decision_uuid)
            .values_list("id", flat=True).first()
        )
        if routing_decision_pk is None:
            return None

        routing_profile = (
            TraderScore.objects.filter(account_id=account_id)
            .values_list("routing_profile", flat=True).first()
            or "INTERNAL"
        )

        spec = get_spec(symbol)
        exposure_usd = abs(Decimal(str(qty)) * Decimal(str(price)) * Decimal(str(spec.contract_size)))

        providers = list(LiquidityProvider.objects.filter(enabled=True))
        provider = _liquidity_engine.select_simulated_provider(providers, symbol, exposure_usd)
        if provider is None:
            return None

        contract = _liquidity_engine.evaluate_simulated_hedge(
            symbol=symbol, side=side, qty=qty, price=price,
            provider=provider, routing_profile=routing_profile,
        )

        return _liquidity_engine.record_liquidity_decision(
            routing_decision_id=routing_decision_pk,
            position_id=position_id,
            provider_id=contract["provider_id"],
            symbol=contract["symbol"],
            exposure_usd=contract["exposure_usd"],
            simulated_spread=contract["simulated_spread"],
            simulated_cost=contract["simulated_cost"],
            inputs_snapshot=contract["inputs_snapshot"],
            engine_version=contract["engine_version"],
            schema_version=contract["schema_version"],
        )

    @database_sync_to_async
    def _db_record_liquidity_audit_event(self, *, account_id, symbol,
                                          liquidity_decision_id, routing_decision_id,
                                          position_id):
        """
        BOOK-05e.2 — records "a LiquidityDecision associated with an
        accepted open was recorded" as an institutional BrokerAuditEvent,
        under Category.LIQUIDITY. Deliberately thin: only
        liquidity_decision_id/routing_decision_id/position_id — never
        re-reads LiquidityDecision, never includes inputs_snapshot,
        simulated_spread, simulated_cost, or provider details (see
        docs/BOOK_05_IMPLEMENTATION_PLAN.md, Principio rector — this
        event observes that a decision was recorded, it does not
        re-expose its contents).

        Sync method, decorated database_sync_to_async for the same
        reason as _db_record_routing_audit_event above — its caller,
        _order_new(), is `async def` without that decorator, while
        record_event()'s ORM writes are synchronous.

        liquidity_decision_id/routing_decision_id are uuid.UUID values
        (LiquidityDecision.decision_id / RoutingDecision.decision_id) —
        stringified before going into `metadata`, a plain JSONField
        (no DjangoJSONEncoder), which cannot serialize a raw UUID object.
        """
        from . import broker_audit as _audit
        _audit.record_liquidity_event(
            event_type=_audit.EV_LIQUIDITY_DECISION_RECORDED,
            description=(
                f"Liquidity decision recorded for {symbol} "
                f"(position_id={position_id})"
            ),
            account_id=account_id, symbol=symbol,
            source_module="simulator.consumers",
            metadata={
                "liquidity_decision_id": str(liquidity_decision_id),
                "routing_decision_id": str(routing_decision_id),
                "position_id": position_id,
            },
        )

    @database_sync_to_async
    def _db_record_dealing_desk_decision(self, *, routing_decision_uuid, account_id,
                                          position_id, symbol, has_liquidity_decision,
                                          liquidity_decision_id=None):
        """
        BOOK-06c — resolves the inputs needed to evaluate a Dealing Desk
        decision for this accepted open, and persists the result via
        dealing_desk.record_dealing_desk_decision() — the single write
        point for DealingDeskDecision (see that function's own
        docstring).

        Sync method, decorated database_sync_to_async for the same
        reason as _db_record_liquidity_decision above — its caller,
        _order_new(), is `async def`.

        Never writes to RoutingDecision or LiquidityDecision —
        routing_decision_uuid (a uuid.UUID, RoutingDecision.decision_id)
        is only ever used to resolve the real primary key via a
        read-only lookup, exactly once, same pattern as
        _db_record_liquidity_decision. routing_profile is resolved
        independently here (its own TraderScore read, same fallback to
        "INTERNAL" for a brand-new account with no TraderScore row yet)
        — deliberately NOT read from any LiquidityDecision.inputs_snapshot,
        because this decision is recorded regardless of whether a
        LiquidityDecision exists at all (BOOK-06c design, approved
        2026-07-27).

        evaluate_dealing_desk_decision() (BOOK-06b) is a pure function —
        called here, inline, with no DB access of its own.
        record_dealing_desk_decision() itself never raises (fail-open,
        matches record_liquidity_decision()'s contract) — the only
        exceptions that can reach the caller here come from the
        read-only resolution steps below, never from the write itself.

        Returns the created DealingDeskDecision, or None if no
        RoutingDecision could be resolved or the write failed.
        """
        from . import dealing_desk as _dealing_desk
        from .models import RoutingDecision, TraderScore

        routing_decision_pk = (
            RoutingDecision.objects.filter(decision_id=routing_decision_uuid)
            .values_list("id", flat=True).first()
        )
        if routing_decision_pk is None:
            return None

        routing_profile = (
            TraderScore.objects.filter(account_id=account_id)
            .values_list("routing_profile", flat=True).first()
            or "INTERNAL"
        )

        decision_result = _dealing_desk.evaluate_dealing_desk_decision(
            routing_profile=routing_profile,
            has_liquidity_decision=has_liquidity_decision,
        )

        return _dealing_desk.record_dealing_desk_decision(
            routing_decision_id=routing_decision_pk,
            position_id=position_id,
            liquidity_decision_id=liquidity_decision_id,
            symbol=symbol,
            routing_profile_snapshot=routing_profile,
            is_simulated_hedge=decision_result["is_simulated_hedge"],
            engine_version=decision_result["engine_version"],
            schema_version=decision_result["schema_version"],
        )

    async def _order_update(self, data: dict):
        pid = data.get("id")
        try: pid = int(pid)
        except (ValueError, TypeError): pass
        sym = data.get("symbol", self.symbol)
        sl  = data.get("sl", None)
        tp  = data.get("tp", None)

        found = False
        if pid is not None:
            for p in self._positions:
                if str(p.get("id")) == str(pid) and p.get("symbol")==sym:
                    if sl is not None: p["sl"] = float(sl)
                    if tp is not None: p["tp"] = float(tp)
                    found = True
                    await self._db_mirror_update_sl_tp(pid, sym, p.get("sl"), p.get("tp"))
                    break

        if not found:
            log.warning("[order_update] no position matched pid=%r sym=%r — SL/TP update ignored", pid, sym)

        if found:
            await self._refresh_and_send_positions()
        else:
            await self.send_json({"type":"warn","message":"order_update_not_found"})

    async def _order_close(self, data: dict):
        pid      = data.get("id")          # may arrive as str or int
        sym_hint = data.get("symbol", None)

        log.info("[close] received pid=%r sym_hint=%r positions_in_memory=%d ids=%s",
                 pid, sym_hint, len(self._positions),
                 [(p.get("id"), p.get("symbol"), p.get("side")) for p in self._positions])

        # Step A — find position in memory (read-only, no mutation yet)
        found_pos = None
        for p in self._positions:
            id_match  = (pid is not None) and (str(p.get("id")) == str(pid))
            sym_match = (sym_hint is None) or (p.get("symbol") == sym_hint)
            log.debug("[close] checking pos id=%r sym=%r → id_match=%s sym_match=%s",
                      p.get("id"), p.get("symbol"), id_match, sym_match)
            if id_match and sym_match and found_pos is None:
                found_pos = p

        if found_pos is None:
            log.warning("[close] NO MATCH for pid=%r sym_hint=%r — sending order_close_not_found", pid, sym_hint)
            await self.send_json({"type": "warn", "message": "order_close_not_found"})
            return

        # ORDER-MANAGEMENT-V2B — design lock §2. qty absent (None) = full
        # close, exactly the pre-V2B contract. qty present is parsed as
        # Decimal here (never float) purely to fail fast on malformed
        # input, BEFORE touching price/DB — the AUTHORITATIVE validation
        # (<=0, >fresh_position.qty) only ever happens under the lock
        # inside _db_close_position_atomic, against the fresh DB qty,
        # never against this connection's own in-memory found_pos["qty"].
        close_qty = None
        _qty_raw = data.get("qty")
        if _qty_raw is not None:
            from decimal import Decimal, InvalidOperation
            try:
                close_qty = Decimal(str(_qty_raw))
            except InvalidOperation:
                await self.send_json({"type": "error", "code": "invalid_qty",
                                      "message": "cantidad_invalida"})
                return

        # Step B — compute close values BEFORE any memory mutation.
        # O.6c-1s — price authority: self._feed_close_price() (FeedManager
        # global, O.6c-1q), never close_price()/self._bid_state — see
        # O.6c-1r's finding that the EXECUTION price here was still
        # vulnerable to the O.6c-1p fallback. Fail-safe: no fresh price
        # -> reject the close entirely, before any DB write, before any
        # memory mutation — the position stays open, exactly as if this
        # message had never arrived.
        sym = found_pos["symbol"]
        dec = step_decimals_for(sym)[1]
        _raw_close_px = self._feed_close_price(sym, found_pos["side"])
        if _raw_close_px is None:
            log.warning("[close] REJECTED pos id=%r sym=%r — no fresh FeedManager price "
                        "(fail-safe, no synthetic fallback)", found_pos["id"], sym)
            await self.send_json({
                "type": "error", "code": "price_unavailable",
                "message": "no_se_pudo_cerrar_precio_no_disponible",
            })
            return
        close_px = round(_raw_close_px, dec)
        # ORDER-MANAGEMENT-V2B — this estimate (realized/new_balance/
        # new_equity) is PRE-LOCK and no longer authoritative for the real
        # success path (see _db_close_position_atomic's docstring, design
        # lock §3) — it's only used as a fallback value for the demo/
        # anonymous branch and the already_closed early-return, neither of
        # which represents a real financial event for THIS call. Computed
        # against close_qty (the requested partial amount) rather than
        # found_pos["qty"] when qty was provided, purely so those fallback
        # numbers are plausible rather than always reflecting a full close.
        _pos_for_estimate = found_pos
        if close_qty is not None:
            _pos_for_estimate = dict(found_pos)
            _pos_for_estimate["qty"] = float(close_qty)
        realized = self._realized_pnl_for(_pos_for_estimate, close_px)
        new_balance = self.account["balance"] + realized
        remaining_floating = (
            self._unrealized_pnl_total()
            - self._unrealized_pnl_for(_pos_for_estimate, close_px)
        )
        new_equity = round(new_balance + remaining_floating, 2)

        log.info("[close] MATCH pos id=%r sym=%r side=%r close_px=%s realized=%.4f close_qty=%s",
                 found_pos["id"], sym, found_pos["side"], close_px, realized, close_qty)

        pricing_context_close = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_CLOSE)

        # Step C — DB transaction FIRST (Phase 1B: DB-first close)
        try:
            result = await self._db_close_position_atomic(
                found_pos, close_px, "manual", realized, new_balance, new_equity,
                pricing_context_close=pricing_context_close, close_qty=close_qty,
            )
        except Exception as exc:
            log.error("[close] DB close failed for pos id=%r: %s", found_pos["id"], exc, exc_info=True)
            await self.send_json({"type": "error", "code": "close_failed",
                                  "message": "no_se_pudo_cerrar_posicion"})
            return  # memory untouched — position still open

        # ORDER-MANAGEMENT-V2B, design lock §2 — invalid_qty/
        # qty_exceeds_position: the fresh, lock-validated qty rejected
        # this request outright. Position stays open, exactly as if this
        # message had never arrived — same fail-safe contract as the
        # price_unavailable branch above. NEVER silently coerced into a
        # full close.
        if result.get("ok") is False:
            log.warning("[close] REJECTED pos id=%r close_qty=%s: %s",
                        found_pos["id"], close_qty, result.get("code"))
            await self.send_json({
                "type": "error", "code": result.get("code", "close_rejected"),
                "message": result.get("message", "no_se_pudo_cerrar_posicion"),
            })
            return

        # Step D — DB committed: safe to mutate memory now.
        if result.get("already_closed"):
            # The row genuinely doesn't exist anymore (concurrent full
            # close) — remove unconditionally, same as pre-V2B behavior.
            self._positions = [p for p in self._positions if str(p.get("id")) != str(found_pos["id"])]
        elif result.get("partial"):
            # ORDER-MANAGEMENT-V2B — the Position survives with a reduced
            # qty. Patch this connection's own mirror in place rather than
            # removing it; the authoritative DB-fresh resync a few lines
            # below (_refresh_and_send_positions) is what actually decides
            # self._positions going forward (design lock §9) — this is
            # only the fast optimistic patch for THIS connection's
            # immediate view, same spirit as _order_update()'s existing
            # in-place SL/TP patch.
            for p in self._positions:
                if str(p.get("id")) == str(found_pos["id"]):
                    p["qty"] = result.get("remaining_qty", p["qty"])
                    break
        else:
            # Full close, genuinely closed by THIS call.
            self._positions = [p for p in self._positions if str(p.get("id")) != str(found_pos["id"])]

        # PANEL-03 — routed through the same already_closed guard every
        # close path now shares (_handle_close_result); this preserves
        # this function's own original behavior exactly for the real-close
        # case (ACCOUNT-02) while also closing the one small gap it still
        # had: previously _track_daily_pnl(realized) ran even when
        # already_closed=True, folding a stale/unconfirmed realized_pnl
        # into this connection's daily PnL tracking — never correct if
        # this connection didn't actually perform the close.
        outcome = self._handle_close_result(
            found_pos, result, close_px, "manual", realized, int(time.time()),
        )
        if outcome is None:
            # already_closed=True: position was closed by a concurrent
            # connection or the daemon. Do NOT trust the stale
            # new_balance/new_equity this call computed before the lock —
            # force a fresh, non-throttled DB read instead (FASE 4).
            await self._refresh_account_after_stale_close()
        else:
            self.account["balance"]      = outcome["new_balance"]
            self.account["peak_balance"] = outcome["new_peak"]
            self.account["status"]       = outcome["new_status"]
            # ORDER-MANAGEMENT-V2B — the AUTHORITATIVE realized_pnl
            # (outcome["notify_item"]["realized_pnl"], sourced from
            # result["realized_pnl"] — see _handle_close_result), never
            # this method's own pre-lock `realized` estimate.
            self._track_daily_pnl(outcome["notify_item"]["realized_pnl"])

        # Step E — respond to client (same payloads as before)
        await self._recalc_account_and_push()
        log.info(
            "[close] %s. remaining positions=%d",
            "order closed OK" if outcome is not None else "already closed by a concurrent action — synced fresh state",
            len(self._positions),
        )
        if outcome is not None:
            await self.send_json({"type": "order_close", **outcome["notify_item"]})
        await self._refresh_and_send_positions()

    # ---------------- ORDER-MANAGEMENT-V2A — Pending orders ----------------
    async def _order_pending_new(self, data: dict):
        sym        = data.get("symbol", self.symbol)
        side       = str(data.get("side", "")).lower()
        order_type = str(data.get("order_type", "")).lower()
        qty        = float(data.get("qty", 0) or 0)
        sl         = data.get("sl")
        tp         = data.get("tp")
        expires_at_raw = data.get("expires_at")   # absent/None = GTC

        # NOTE — every rejection below uses "error_pending", never the
        # generic "error" type: the frontend's generic 'error' handler
        # unconditionally pops this connection's LAST pendingTmp entry
        # (the optimistic chart-line placeholder for an in-flight MARKET
        # order — see dashboard.html). A pending-order submission never
        # pushes a pendingTmp entry, so reusing "error" here could wrongly
        # discard an unrelated, still-in-flight market order's real
        # placeholder if both happen to race. A distinct message type
        # sidesteps that collision entirely rather than touching the
        # existing, working market-order error handler.
        if sym not in _ALLOWED_SYMBOLS:
            await self.send_json({"type": "error_pending", "code": "invalid_symbol", "message": "simbolo_no_permitido"})
            return
        if side not in ("buy", "sell") or order_type not in ("limit", "stop") or qty <= 0:
            await self.send_json({"type": "error_pending", "code": "invalid_order", "message": "orden_invalida"})
            return

        try:
            trigger_price = float(data.get("trigger_price"))
        except (TypeError, ValueError):
            await self.send_json({"type": "error_pending", "code": "invalid_trigger_price", "message": "precio_de_disparo_invalido"})
            return
        if not math.isfinite(trigger_price) or trigger_price <= 0:
            await self.send_json({"type": "error_pending", "code": "invalid_trigger_price", "message": "precio_de_disparo_invalido"})
            return

        if not self._db_account_id:
            await self.send_json({"type": "error_pending", "code": "demo_not_supported", "message": "las_ordenes_pendientes_requieren_cuenta_real"})
            return

        # Same market-session gate _order_new() applies to new orders.
        from market_data.contracts import OrderPolicy
        from market_data.sessions.service import evaluate_market_session_for_symbol
        _session = evaluate_market_session_for_symbol(sym)
        if _session.order_policy in (OrderPolicy.MARKET_CLOSED, OrderPolicy.HALT_NEW_ORDERS):
            await self.send_json({"type": "error_pending", "code": "market_closed", "message": "mercado_cerrado_o_nuevas_ordenes_bloqueadas"})
            return
        if _session.order_policy == OrderPolicy.CLOSE_ONLY:
            await self.send_json({"type": "error_pending", "code": "close_only", "message": "solo_se_permiten_cierres_en_este_momento"})
            return

        _spec = get_spec(sym)
        _lot_ok, _lot_code = _check_lot_size(qty, _spec)
        if not _lot_ok:
            await self.send_json({"type": "error_pending", "code": _lot_code, "message": _lot_code})
            return

        # O.6c-1w-b raw-quote gate — same contract _order_new()/
        # _order_close() already use: reject before any DB write if the
        # feed has no currently-valid raw quote for this symbol.
        quote = self._feed.get_validated_quote(sym)
        if quote is None or quote.source == "sim":
            await self.send_json({"type": "error_pending", "code": "price_unavailable", "message": "no_se_pudo_crear_precio_no_disponible"})
            return

        # Design lock section 11.L test 1 — reject an order whose trigger
        # condition is ALREADY met at creation time (that's a market
        # order, not a pending one). Same table the live/daemon
        # evaluators use — see _pending_trigger_condition_met's docstring.
        if _pending_trigger_condition_met(order_type, side, trigger_price, quote.bid, quote.ask):
            await self.send_json({
                "type": "error_pending", "code": "trigger_already_met",
                "message": "El precio de disparo ya está del lado alcanzado del mercado actual — use una orden market.",
            })
            return

        # Preliminary SL/TP validation against trigger_price (design lock
        # section 4) — REVALIDATED against the real execution_price at
        # trigger time inside _trigger_pending_order_core; this is only
        # an early, non-authoritative rejection.
        _sl_tp_ok, _sl_tp_code, _sl_tp_msg = _validate_sl_tp(side, sl, tp, trigger_price)
        if not _sl_tp_ok:
            await self.send_json({"type": "error_pending", "code": _sl_tp_code, "message": _sl_tp_msg})
            return

        # Design lock section 7 — advisory-only margin sanity check
        # (never a reservation). Same guard _order_new() runs before its
        # own DB write.
        eq_now = self.account["balance"] + self._unrealized_pnl_total()
        mg_now = self._margin_used_total()
        _guard_ok, _guard_code, _guard_msg, _guard_details = _compute_pretrade_margin_guard(
            sym, qty, trigger_price, eq_now, mg_now,
            self.account, _spec.max_leverage, _spec.contract_size,
            max_margin_per_trade_pct=self.account.get("max_margin_per_trade_pct", _DEFAULT_MAX_MARGIN_PER_TRADE_PCT),
            max_total_margin_pct=self.account.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
            account_currency=self.account.get("currency", "USD"),
        )
        if not _guard_ok:
            await self.send_json({"type": "error_pending", "code": _guard_code, "message": _guard_msg})
            return

        expires_at = None
        if expires_at_raw:
            from django.utils.dateparse import parse_datetime
            try:
                _dt_val = parse_datetime(str(expires_at_raw))
                if _dt_val is not None:
                    expires_at = _dt_val if timezone.is_aware(_dt_val) else timezone.make_aware(_dt_val)
            except Exception:
                expires_at = None

        order_dict = await self._db_create_pending_order(
            sym, side.upper(), order_type.upper(), qty, trigger_price, sl, tp, expires_at,
        )
        self._pending_orders.append(order_dict)
        await self.send_json({"type": "order_pending_new", **order_dict})
        await self._refresh_and_send_pending_orders()

    @database_sync_to_async
    def _db_create_pending_order(self, symbol, side, order_type, qty, trigger_price, sl, tp, expires_at) -> dict:
        from decimal import Decimal
        from . import ws_events
        po = PendingOrder.objects.create(
            account_id=self._db_account_id, symbol=symbol, side=side, order_type=order_type,
            qty=Decimal(str(qty)), trigger_price=Decimal(str(trigger_price)),
            sl=Decimal(str(sl)) if sl is not None else None,
            tp=Decimal(str(tp)) if tp is not None else None,
            expires_at=expires_at,
        )
        transaction.on_commit(lambda: ws_events.publish_pending_order_changed(
            self._db_account_id, action=ws_events.ACTION_PENDING_NEW, pending_order_id=po.id,
        ))
        return {
            "id": po.id, "symbol": po.symbol, "side": po.side, "order_type": po.order_type,
            "qty": float(po.qty), "trigger_price": float(po.trigger_price),
            "sl": float(po.sl) if po.sl is not None else None,
            "tp": float(po.tp) if po.tp is not None else None,
            "status": po.status,
            "expires_at": po.expires_at.timestamp() if po.expires_at else None,
            "created_ts": int(po.created_at.timestamp()),
        }

    async def _order_pending_cancel(self, data: dict):
        pid = data.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            await self.send_json({"type": "warn", "message": "order_pending_cancel_not_found"})
            return
        result = await self._db_cancel_pending_order(pid)
        if not result["ok"]:
            await self.send_json({"type": "warn", "message": f"order_pending_cancel_{result['code']}"})
            return
        self._pending_orders = [p for p in self._pending_orders if p["id"] != pid]
        await self.send_json({"type": "order_pending_cancel", "id": pid})
        await self._refresh_and_send_pending_orders()

    @database_sync_to_async
    def _db_cancel_pending_order(self, pending_order_id: int) -> dict:
        """select_for_update — same pessimistic-lock discipline as
        _trigger_pending_order_core (design lock section 5: NO optimistic
        check-and-set). A cancel racing a trigger loses cleanly: if the
        trigger's own transaction already committed TRIGGERED (or
        EXPIRED/REJECTED), this observes status != PENDING under its own
        lock and returns ok=False — never a false 'cancelled'."""
        from . import ws_events
        with transaction.atomic():
            po = (
                PendingOrder.objects.select_for_update()
                .filter(pk=pending_order_id, account_id=self._db_account_id)
                .first()
            )
            if po is None:
                return {"ok": False, "code": "not_found"}
            if po.status != PendingOrder.PENDING:
                return {"ok": False, "code": "not_pending"}
            po.status = PendingOrder.CANCELLED
            po.cancelled_at = timezone.now()
            po.save(update_fields=["status", "cancelled_at", "updated_at"])
            transaction.on_commit(lambda: ws_events.publish_pending_order_changed(
                self._db_account_id, action=ws_events.ACTION_PENDING_CANCEL, pending_order_id=po.id,
            ))
            return {"ok": True, "code": "cancelled"}

    async def _order_pending_update(self, data: dict):
        pid = data.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            await self.send_json({"type": "warn", "message": "order_pending_update_not_found"})
            return

        try:
            trigger_price = float(data["trigger_price"]) if data.get("trigger_price") is not None else None
            qty           = float(data["qty"]) if data.get("qty") is not None else None
            sl            = float(data["sl"]) if data.get("sl") is not None else None
            tp            = float(data["tp"]) if data.get("tp") is not None else None
        except (TypeError, ValueError):
            await self.send_json({"type": "error_pending", "code": "invalid_order", "message": "orden_invalida"})
            return

        if trigger_price is not None and (not math.isfinite(trigger_price) or trigger_price <= 0):
            await self.send_json({"type": "error_pending", "code": "invalid_trigger_price", "message": "precio_de_disparo_invalido"})
            return
        if qty is not None and qty <= 0:
            await self.send_json({"type": "error_pending", "code": "invalid_order", "message": "orden_invalida"})
            return

        result = await self._db_update_pending_order(pid, trigger_price, qty, sl, tp)
        if not result["ok"]:
            await self.send_json({"type": "warn", "message": f"order_pending_update_{result['code']}"})
            return
        for i, p in enumerate(self._pending_orders):
            if p["id"] == pid:
                self._pending_orders[i] = result["order"]
                break
        await self.send_json({"type": "order_pending_update", **result["order"]})
        await self._refresh_and_send_pending_orders()

    @database_sync_to_async
    def _db_update_pending_order(self, pending_order_id: int, trigger_price, qty, sl, tp) -> dict:
        """Same select_for_update discipline as _db_cancel_pending_order
        above — a modify racing a trigger loses cleanly rather than
        silently editing a row that already triggered."""
        from decimal import Decimal
        from . import ws_events
        with transaction.atomic():
            po = (
                PendingOrder.objects.select_for_update()
                .filter(pk=pending_order_id, account_id=self._db_account_id)
                .first()
            )
            if po is None:
                return {"ok": False, "code": "not_found"}
            if po.status != PendingOrder.PENDING:
                return {"ok": False, "code": "not_pending"}

            _fields = []
            if trigger_price is not None:
                po.trigger_price = Decimal(str(trigger_price)); _fields.append("trigger_price")
            if qty is not None:
                po.qty = Decimal(str(qty)); _fields.append("qty")
            if sl is not None:
                po.sl = Decimal(str(sl)); _fields.append("sl")
            if tp is not None:
                po.tp = Decimal(str(tp)); _fields.append("tp")

            if _fields:
                _fields.append("updated_at")
                po.save(update_fields=_fields)
                transaction.on_commit(lambda: ws_events.publish_pending_order_changed(
                    self._db_account_id, action=ws_events.ACTION_PENDING_UPDATE, pending_order_id=po.id,
                ))

            return {"ok": True, "code": "updated", "order": {
                "id": po.id, "symbol": po.symbol, "side": po.side, "order_type": po.order_type,
                "qty": float(po.qty), "trigger_price": float(po.trigger_price),
                "sl": float(po.sl) if po.sl is not None else None,
                "tp": float(po.tp) if po.tp is not None else None,
                "status": po.status,
                "expires_at": po.expires_at.timestamp() if po.expires_at else None,
                "created_ts": int(po.created_at.timestamp()),
            }}

    async def _check_pending_triggers(self, symbol: str, raw_bid: float, raw_ask: float):
        """ORDER-MANAGEMENT-V2A — live-tick trigger evaluator. Sibling of
        _check_tp_sl(): same call site (price_tick(), after the same raw-
        quote validation + 'never on a sim tick' fail-closed gate), same
        'operates on this connection's own in-memory mirror, never a DB
        query inside the hot tick path' discipline (design lock section
        E's performance note).

        getattr(..., None) or [] — same defensive-default already implied
        for self._positions by every bare-consumer test double across the
        suite (TradingConsumer.__new__(TradingConsumer) + only the
        attributes that ONE test actually exercises, never a full
        connect()); _pending_orders is new and none of those pre-existing
        doubles know to set it, so treat "not set at all" the same as
        "hydrated with zero pending orders" rather than raising."""
        matched = [
            p for p in (getattr(self, "_pending_orders", None) or [])
            if p["symbol"] == symbol
            and _pending_trigger_condition_met(p["order_type"], p["side"], p["trigger_price"], raw_bid, raw_ask)
        ]
        if not matched:
            return

        any_opened = False
        for p in matched:
            side = p["side"].lower()
            execution_price = raw_ask if side == "buy" else raw_bid
            try:
                result = await self._db_trigger_pending_order_atomic(p["id"], execution_price)
            except Exception as exc:
                log.error("[pending_trigger] live trigger FAILED pending_order_id=%s: %s",
                          p["id"], exc, exc_info=True)
                continue
            # Whatever the outcome (triggered / rejected / expired /
            # not_pending — e.g. a concurrent daemon or sibling-panel tick
            # already claimed it under its own lock), this order is no
            # longer PENDING — drop it from the in-memory mirror
            # unconditionally. DB decided; never re-add (same discipline
            # _check_tp_sl() already uses for closed positions).
            self._pending_orders = [x for x in self._pending_orders if x["id"] != p["id"]]
            if result.get("ok"):
                any_opened = True

        if any_opened:
            await self._recalc_account_and_push()
            await self._refresh_and_send_positions()
        await self._refresh_and_send_pending_orders()

    @database_sync_to_async
    def _db_trigger_pending_order_atomic(self, pending_order_id: int, execution_price: float) -> dict:
        return _trigger_pending_order_core(pending_order_id, execution_price)

    async def _refresh_and_send_pending_orders(self):
        if not self._db_account_id:
            await self.send_json({"type": "pending_orders", "items": self._pending_orders})
            return
        try:
            items = await self._db_fetch_pending_orders()
        except Exception as exc:
            log.error("[pending_orders] refresh failed for account=%s — keeping previous state: %r",
                      self._db_account_id, exc, exc_info=True)
            await self.send_json({"type": "pending_orders", "items": self._pending_orders})
            return
        self._pending_orders = items
        await self.send_json({"type": "pending_orders", "items": self._pending_orders})

    async def pending_order_changed(self, event: dict):
        """ORDER-MANAGEMENT-V2A — PendingOrder-book equivalent of
        position_changed(). Simpler contract, deliberately: no optimistic
        in-memory patch — pending orders are a lower-frequency, less
        latency-sensitive UI element than open positions, so this always
        just does a DB-fresh resync."""
        await self._refresh_and_send_pending_orders()

    # ---------------- Risk Preview ----------------
    async def _handle_risk_preview(self, data: dict):
        sym = data.get("symbol", self.symbol)
        qty = float(data.get("qty", 0) or 0)
        if qty <= 0:
            return
        equity = self.account["balance"] + self._unrealized_pnl_total()
        margin = self._margin_used_total()
        lev = max(1, int(self.account.get("leverage", 50)))
        assessment = await self._db_evaluate_risk(sym, qty, equity, margin, lev)
        # FIX-01: echo the request's symbol/qty so the client can drop a
        # response that arrives after the user switched symbol or panel —
        # no formula/business-rule change, just request/response correlation.
        await self.send_json({"type": "risk_preview", "symbol": sym, "qty": qty, **assessment})

    @database_sync_to_async
    def _db_evaluate_risk(self, symbol: str, lot_size: float,
                           equity: float, margin_used: float, leverage: int) -> dict:
        if not self._db_account_id:
            return {"risk_level": "LOW"}
        from .risk_engine import evaluate_position_risk
        account = TradingAccount.objects.filter(id=self._db_account_id).first()
        if not account:
            return {"risk_level": "LOW"}
        return evaluate_position_risk(account, symbol, lot_size, equity, margin_used, leverage)

    # ---------------- Cuenta / PnL ----------------
    def _resolve_commercial_pricing_profile(self, symbol: str):
        """SPREAD-04 — combines the account-level commercial pricing fields
        already resolved once at hydrate time (self.account["commercial_
        pricing_fields"]) with the symbol's live BrokerSpreadConfig
        floor/ceiling. Pure, DB-free — safe to call every tick."""
        from . import commercial_pricing
        return commercial_pricing.build_commercial_pricing_profile(
            self.account.get("commercial_pricing_fields") or {}, symbol,
        )

    def commission_for(self, symbol: str, qty: float, price: float) -> float:
        """SPREAD-04: decided entirely from the resolved commercial pricing
        profile — per-lot if configured, else pct if configured, else an
        explicit zero when the profile says so. Only a profile with
        source=legacy_fallback (no snapshot, no product, no challenge
        relation resolvable — see commercial_pricing.py) falls back to
        spec.commission_pct, matching the original pre-SPREAD-04 behavior
        for accounts with no resolvable commercial policy at all."""
        from . import commercial_pricing
        profile = self._resolve_commercial_pricing_profile(symbol)
        if profile.commission_per_lot > 0:
            return round(qty * profile.commission_per_lot, 2)
        if profile.commission_pct > 0:
            spec = get_spec(symbol)
            notional = qty * price * spec.contract_size
            return max(0.0, notional * profile.commission_pct)
        if profile.source == commercial_pricing.SOURCE_LEGACY_FALLBACK:
            spec = get_spec(symbol)
            notional = qty * price * spec.contract_size
            return max(0.0, notional * spec.commission_pct)
        return 0.0

    def min_qty_for(self, symbol: str) -> float:
        return get_spec(symbol).min_lot

    def _pretrade_check(self, symbol, side, qty):
        spec = get_spec(symbol)
        ok, code = _check_lot_size(qty, spec)
        if not ok:
            return False, code
        account_lev = max(1, int(self.account.get("leverage", 50)))
        lev = max(1, min(account_lev, spec.max_leverage))
        # O.6c-1aa — same RAW validated authority as the rest of
        # _order_new(), never self.exec_price() (client/marked-up).
        # _order_new() already gates on this before calling here, so
        # None should not occur in practice — still handled explicitly
        # (fail-safe, never a synthetic estimate) for any other caller.
        entry_px = self._raw_exec_price(symbol, side)
        if entry_px is None:
            return False, "price_unavailable"
        # FIX-USDJPY-MARGIN-01-B — base/quote-aware notional, same shared
        # helper every other margin path in this file now uses. Fails
        # closed (never assumes 1:1) — unreachable today, every enabled
        # symbol is Case A or B.
        est_margin, _margin_error_code = pnl_engine.calculate_required_margin(
            symbol, entry_px, qty, lev, self.account.get("currency", "USD"),
        )
        if est_margin is None:
            log.error(
                "[guard] REJECTED margin_currency_conversion_unavailable | sym=%s qty=%s "
                "entry_px=%.5f error_code=%s",
                symbol, qty, entry_px, _margin_error_code,
            )
            return False, "margin_currency_conversion_unavailable"
        equity = self.account["balance"] + self._unrealized_pnl_total()
        if est_margin > (equity - self._margin_used_total()):
            return False, "insufficient_margin"
        return True, "ok"

    def _open_or_update_position(self, symbol, side, qty, fill_px, sl=None, tp=None, position_id=None):
        dec = step_decimals_for(symbol)[1]
        for pos in self._positions:
            if pos["symbol"]==symbol and pos["side"]==side:
                new_qty = pos["qty"] + qty
                pos["avg"] = round(((pos["avg"]*pos["qty"])+(fill_px*qty))/new_qty, dec)
                pos["qty"] = new_qty
                if sl is not None: pos["sl"]=sl
                if tp is not None: pos["tp"]=tp
                return
        self._positions.append({"id":position_id or self._order_seq, "symbol":symbol,"side":side,
                                "qty":qty,"avg":round(fill_px,dec),"sl":sl,"tp":tp,
                                "opened_at":int(time.time())})

    def _create_position(self, symbol, side, qty, fill_px, sl=None, tp=None, position_id=None):
        dec = step_decimals_for(symbol)[1]
        self._positions.append({"id":position_id or self._order_seq, "symbol":symbol,"side":side,
                                "qty":qty,"avg":round(fill_px,dec),"sl":sl,"tp":tp,
                                "opened_at":int(time.time())})

    def _positions_snapshot(self):
        """MARGIN-02 — includes backend-authoritative pnl (account currency,
        via the same pnl_engine every close/equity path uses) per position,
        so the frontend does not need its own PnL formula to be correct for
        USD/JPY. Never fails the whole snapshot on one bad position — a
        per-position pnl_engine error degrades that item's "pnl" to None,
        never a fabricated number.

        O.6c-1s — price authority: self._feed_close_price() (FeedManager
        global, O.6c-1q), never close_price()/self._bid_state. O.6c-1r
        found this row-level "pnl" field was still computed via the
        per-connection cache — the exact same fallback-to-base_price_for()
        bug O.6c-1p/O.6c-1q fixed for the header, just not here yet. No
        fresh price -> "pnl": None, same degrade path as an exception —
        never a synthetic value."""
        out = []
        for p in self._positions:
            d = dict(p)
            try:
                px = self._feed_close_price(p["symbol"], p["side"])
                d["pnl"] = round(self._unrealized_pnl_for(p, px), 2) if px is not None else None
            except Exception as exc:
                log.debug("[positions_snapshot] pnl calc failed for pos=%s: %r", p.get("id"), exc)
                d["pnl"] = None
            out.append(d)
        return out

    def _feed_close_price(self, symbol: str, side: str) -> "float | None":
        """Price authority for every financial use of a close/settlement
        price: row P&L, account-wide floating P&L/equity aggregation
        (_unrealized_pnl_total() below), manual close, Close All, SL,
        TP, stopout, and retail liquidation — all six call sites funnel
        through this one function (O.6c-1q established the account-wide
        aggregation case; O.6c-1s migrated the remaining five execution/
        display call sites onto it; this docstring previously claimed
        the opposite for the execution call sites — stale since O.6c-1s,
        corrected here).

        O.6c-1w — routes through self._feed.get_validated_quote(symbol),
        the single authoritative point deciding whether a quote may be
        used financially at all: structural validity (finite, positive,
        ask>=bid) AND Capa A plausibility (within one order of magnitude
        of SymbolSpec.base_price) — not just presence
        (has_price()==True), which O.6c-1t demonstrated is NOT
        sufficient on its own (a BTCUSD-magnitude value was observed
        under the EUR/USD key, has_price()==True the whole time).

        FIX-05A — quote.source=="sim" is never financial authority for
        any of the six call sites above (row P&L, account-wide floating
        P&L/equity, manual close, Close All, SL/TP, stopout/liquidation).
        Treated identically to "no quote at all" — reuses the exact
        existing None fail-safe every one of those callers already has,
        never a new "frozen" model.

        Returns None — never base_price_for(), never any synthetic
        value, never a value that failed validation — on ANY failure.
        Mirrors broker_exposure.py's own FASE 4 reference-price policy
        exactly (exclude, never fabricate), now generalized through one
        choke point rather than repeated per call site."""
        quote = self._feed.get_validated_quote(symbol)
        if quote is None or quote.source == "sim":
            return None
        return quote.bid if side == "buy" else quote.ask

    def _unrealized_pnl_total(self):
        total = 0.0
        unpriced = []
        for p in self._positions:
            px = self._feed_close_price(p["symbol"], p["side"])
            if px is None:
                unpriced.append(p["symbol"])
                continue
            total += self._unrealized_pnl_for(p, px)
        # Side-channel, read by _recalc_account_and_push()'s stopout
        # fail-safe immediately after this call — never a new WS payload
        # field, never persisted; purely in-memory bookkeeping for this
        # one connection's own most recent aggregation.
        self._unpriced_pnl_symbols = sorted(set(unpriced))
        return total

    def _unrealized_pnl_for(self, pos, close_px):
        """MARGIN-02 — the SINGLE PnL formula for every WS path: unrealized
        total, realized-on-close (via _realized_pnl_for's alias below —
        manual close, TP, SL, stop-out, liquidation), Trade.profit_loss.
        Delegates to pnl_engine.position_pnl_float(), which converts the
        instrument's quote-currency PnL into the account's currency
        (self.account["currency"], hydrated from TradingAccount.currency —
        every real account today is USD, verified in the MARGIN-01/02
        audit). Previously this multiplied contract_size straight through
        without converting — correct for quote_currency==USD instruments,
        silently ~155x wrong for USD/JPY (quote=JPY)."""
        return pnl_engine.position_pnl_float(
            pos["side"], pos["avg"], close_px, pos["qty"], pos["symbol"],
            account_currency=self.account.get("currency", "USD"),
        )

    def _realized_pnl_for(self, pos, close_price): return self._unrealized_pnl_for(pos, close_price)

    def _track_daily_pnl(self, amount: float) -> None:
        from django.utils import timezone as _tz
        today = _tz.now().date()
        if self._daily_pnl_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_pnl_date = today
        self._daily_realized_pnl += amount

    def _margin_used_total(self):
        # FIX-USDJPY-MARGIN-01-B — base/quote-aware notional per position,
        # same shared helper every margin path in this file now uses.
        account_lev = max(1, int(self.account.get("leverage", 50)))
        account_currency = self.account.get("currency", "USD")
        total = 0.0
        for p in self._positions:
            spec = get_spec(p["symbol"])
            lev = max(1, min(account_lev, spec.max_leverage))
            margin, error_code = pnl_engine.calculate_required_margin(
                p["symbol"], p["avg"], p["qty"], lev, account_currency,
            )
            if margin is None:
                # Unreachable today (no open position can exist for a
                # Case-C symbol — the pretrade guard already rejects
                # opening one). Mirrors pnl_engine.position_pnl_float's
                # own established precedent for this identical
                # impossible case: log loudly, never fabricate a number.
                log.critical(
                    "[margin] event=margin_conversion_unsupported symbol=%s "
                    "account_currency=%s error_code=%s — contributing 0.0 to "
                    "margin_used_total, NOT a fabricated number. This should be "
                    "unreachable with current account currencies/enabled symbols.",
                    p["symbol"], account_currency, error_code,
                )
                continue
            total += margin
        return total

    async def _recalc_account_and_push(self):
        self.account["pnl_unreal"] = round(self._unrealized_pnl_total(), 2)
        self.account["margin_used"] = round(self._margin_used_total(), 2)

        # ACCOUNT-02 — refresh self.account["balance"] from the DB (never
        # write it) on the same throttled cadence the old buggy sync used,
        # BEFORE computing equity — so a sibling panel's realized close is
        # picked up here instead of this connection continuing to compute
        # equity off its own stale balance. See _db_sync_account_balances()
        # for the full rationale. Any failure here keeps self.account["balance"]
        # exactly as it was — never fabricated, never reverted.
        now = time.time()
        if self._db_account_id and (now - self._last_db_sync) > 1.2:
            try:
                fresh_balance = await self._db_sync_account_balances()
            except Exception as exc:
                log.error("[account] balance refresh failed for account=%s — keeping previous state: %r",
                          self._db_account_id, exc, exc_info=True)
                fresh_balance = None
            if fresh_balance is not None:
                self.account["balance"] = fresh_balance
            self._last_db_sync = now

        self.account["equity"] = round(self.account["balance"] + self.account["pnl_unreal"], 2)
        free_margin = round(self.account["equity"] - self.account["margin_used"], 2)

        # Real-time stopout — only check if account is currently active.
        # O.6c-1q fail-safe: if ANY open position lacks a fresh FeedManager
        # price this tick (self._unpriced_pnl_symbols, set by the
        # _unrealized_pnl_total() call above), self.account["equity"] is
        # necessarily INCOMPLETE (that position's real PnL — profit or
        # loss — is simply absent, not zeroed) — skip evaluating stopout
        # entirely this tick rather than decide on partial data. Mirrors
        # PANEL-02's own established precedent for the identical dilemma
        # in the atomic open guard: "the only safe options are 'use a
        # real, fresh price' or 'refuse to decide' — never invent a
        # number." The next tick that resolves the missing price(s)
        # re-evaluates normally — this is not a permanent bypass.
        if self._unpriced_pnl_symbols:
            log.warning(
                "[stopout] skipped this tick for account=%s — unpriced position "
                "symbol(s) %s (fail-safe: equity is incomplete, not evaluating)",
                self._db_account_id, self._unpriced_pnl_symbols,
            )
        elif self.account.get("status") == "Activo" and self._positions:
            _acct_type = self.account.get("account_type", "CHALLENGE")
            from .risk_engine import check_equity_stopout
            if check_equity_stopout(
                equity=self.account["equity"],
                peak_balance=self.account["peak_balance"],
                tier=self.account.get("tier", "10K"),
                account_type=_acct_type,
                margin_used=self.account.get("margin_used", 0.0),
                stopout_level=self.account.get("stopout_level", 70.0),
            ):
                from .models import MARGIN_ENGINE_TYPES
                if _acct_type in MARGIN_ENGINE_TYPES:
                    await self._do_retail_liquidation()
                else:
                    await self._do_stopout()
                return  # handler pushes its own account:update

        # Risk / challenge metrics
        peak = self.account["peak_balance"]
        balance = self.account["balance"]
        total_dd_pct = round((peak - balance) / peak * 100, 2) if peak > 0 else 0.0

        daily_pnl = self._daily_realized_pnl
        daily_dd_pct = round(abs(daily_pnl) / peak * 100, 2) if (peak > 0 and daily_pnl < 0) else 0.0

        margin_used = self.account["margin_used"]
        equity_val = self.account["equity"]
        margin_level = round(equity_val / margin_used * 100, 2) if margin_used > 0 else 0.0

        # FIX-04: maintenance_margin/liquidation_distance are a RETAIL-only
        # metric derived from the account's real stopout_level — _acct_type
        # above is only assigned inside the elif branch at line ~2277, so it
        # cannot be reused here; re-derive account_type fresh instead.
        from .models import MARGIN_ENGINE_TYPES
        _account_type_for_margin = self.account.get("account_type", "CHALLENGE")
        if _account_type_for_margin in MARGIN_ENGINE_TYPES:
            from .risk_engine import compute_margin_state
            _ms = compute_margin_state(
                equity_val, margin_used,
                stopout_level=self.account.get("stopout_level", 50.0),
            )
            used_margin_pct   = _ms["used_margin_pct"]
            maintenance_margin = _ms["maintenance_margin"]
            liquidation_distance = _ms["liquidation_distance"]
        else:
            used_margin_pct = maintenance_margin = liquidation_distance = 0.0

        dec = step_decimals_for(self.symbol)[1]
        bid = round(self.get_bid(self.symbol), dec)
        ask = round(self.get_ask(self.symbol), dec)
        spread = round(ask - bid, dec)

        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": equity_val,
            "pnl_unreal": self.account["pnl_unreal"],
            "upnl": self.account["pnl_unreal"],
            "margin_used": margin_used,
            "free_margin": free_margin,
            "used_margin_pct": used_margin_pct,
            "maintenance_margin": maintenance_margin,
            "liquidation_distance": liquidation_distance,
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),
            "account_type": self.account.get("account_type", "CHALLENGE"),
            "total_dd_pct": total_dd_pct,
            "daily_dd_pct": daily_dd_pct,
            "daily_pnl": round(daily_pnl, 2),
            "margin_level": margin_level,
            # FIX-03 — authoritative thresholds from THIS account's own
            # frozen snapshot (self.account, hydrated from TradingAccount.
            # margin_call_level_snapshot/stopout_level_snapshot) — never
            # the live AccountProduct, never a hardcoded UI constant.
            "margin_call_level": self.account.get("margin_call_level", 100.0),
            "stopout_level":     self.account.get("stopout_level", 70.0),
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "profit_target": self.account.get("profit_target", 800.0),
            "initial_balance": self.account.get("initial_balance", self.account.get("balance", 0.0)),
            # Phase 6B — product rule info
            "product_name":       self.account.get("product_name", ""),
            "commission_per_lot": self.account.get("commission_per_lot", 0.0),
            "spread_pips":        self.account.get("spread_pips", 0.0),
            "currency":           self.account.get("currency", "USD"),
        })

    async def _do_stopout(self) -> None:
        """Close ALL open positions at current bid/ask and suspend the account."""
        log.warning("[stopout] equity=%.2f triggered for account #%s",
                    self.account["equity"], self._db_account_id)
        closed_items = []
        failed_positions = []
        now_ts = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0
        saw_stale_close = False

        for p in list(self._positions):
            sym  = p["symbol"]
            dec  = step_decimals_for(sym)[1]
            # O.6c-1s — price authority: self._feed_close_price() (FeedManager
            # global, O.6c-1q), never close_price()/self._bid_state — see
            # O.6c-1r. Fail-safe: no fresh price -> refuse to liquidate THIS
            # position (never a fictitious price/loss) — it stays open,
            # collected into failed_positions exactly like a DB-close
            # exception already is below, so the account's status/other
            # positions are unaffected and the next tick retries it.
            _raw_cpx = self._feed_close_price(sym, p["side"])
            if _raw_cpx is None:
                log.warning("[stopout] SKIPPED pos %s sym=%s — no fresh FeedManager price "
                            "(fail-safe, no synthetic fallback)", p["id"], sym)
                failed_positions.append(p)
                continue
            cpx  = round(_raw_cpx, dec)
            realized = self._realized_pnl_for(p, cpx)
            new_balance = running_balance + realized
            fp_p = self._unrealized_pnl_for(p, cpx)
            remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
            new_equity = round(new_balance + remaining_floating, 2)
            pricing_context_close = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_STOPOUT)
            try:
                result = await self._db_close_position_atomic(
                    p, cpx, "stopout", realized, new_balance, new_equity,
                    pricing_context_close=pricing_context_close,
                )
            except Exception as exc:
                log.error("[stopout] DB close failed pos %s: %s", p["id"], exc)
                failed_positions.append(p)
                continue

            # PANEL-03 — position is gone from DB either way once we reach
            # here — never re-add to failed_positions.
            outcome = self._handle_close_result(p, result, cpx, "stopout", realized, now_ts)
            if outcome is None:
                saw_stale_close = True
            else:
                running_balance = outcome["new_balance"]
                accum_floating_closed += fp_p
                self._track_daily_pnl(realized)
                closed_items.append(outcome["notify_item"])

        # DB commits done — update memory with whatever ACTUALLY remains.
        self._positions = failed_positions
        closed_count    = len(closed_items)
        remaining_count = len(failed_positions)
        # STOPOUT LIQUIDATION OUTCOME INTEGRITY — FULL means nothing is
        # left open (remaining_count==0). Deliberately NOT "closed_count
        # > 0 and remaining_count == 0": a position closed by a
        # concurrent connection/daemon between this loop starting and
        # this position's own _db_close_position_atomic call lands in
        # neither closed_items nor failed_positions (PANEL-03's
        # already_closed/"stale close" guard, _handle_close_result
        # returning None — see its own docstring) — it's genuinely gone,
        # just not via THIS call's own atomic close. A batch resolved
        # entirely that way would have closed_count==0 AND
        # remaining_count==0, which must still count as FULL (matches
        # the pre-existing, unrelated stale-close contract already
        # covered by test_close_path_concurrency_parity.py — confirmed
        # by running it, not assumed). self._positions is never empty
        # when this function is entered (see the caller's own
        # `and self._positions` gate), so remaining_count==0 can only
        # mean every position that existed at the start is now
        # genuinely resolved one way or another.
        is_full = remaining_count == 0

        # pnl_unreal/margin_used recalculated from self._positions (now
        # failed_positions) via the SAME pure helpers the per-tick path
        # already uses — never hardcoded to 0.0. When remaining_count==0
        # (FULL) these naturally compute to 0, preserving the exact prior
        # behavior for that case; when positions remain (EMPTY/PARTIAL)
        # they now reflect real exposure instead of a fabricated zero.
        self.account["pnl_unreal"]  = round(self._unrealized_pnl_total(), 2)
        self.account["margin_used"] = round(self._margin_used_total(), 2)
        if saw_stale_close:
            # At least one collision — force a fresh, non-throttled
            # balance/equity read rather than trust running_balance (see
            # _refresh_account_after_stale_close's docstring).
            await self._refresh_account_after_stale_close()
        else:
            self.account["balance"] = running_balance
            self.account["equity"]  = round(running_balance + self.account["pnl_unreal"], 2)

        if is_full:
            try:
                await self._db_suspend_account("stopout")
            except Exception as exc:
                log.error("[stopout] DB suspend failed: %s", exc)
            self.account["status"] = "Suspendido"

        # Notify client — order_close/positions refresh are always
        # truthful regardless of outcome; account:suspended only on FULL.
        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self._refresh_and_send_positions()
        if is_full:
            await self.send_json({
                "type": "account:suspended",
                "status": "Suspendido",
                "reason": "stopout",
            })
        peak = self.account["peak_balance"]
        balance = self.account["balance"]
        total_dd_pct = round((peak - balance) / peak * 100, 2) if peak > 0 else 0.0
        daily_pnl = self._daily_realized_pnl
        daily_dd_pct = round(abs(daily_pnl) / peak * 100, 2) if (peak > 0 and daily_pnl < 0) else 0.0
        margin_used  = self.account["margin_used"]
        equity_val   = self.account["equity"]
        margin_level = round(equity_val / margin_used * 100, 2) if margin_used > 0 else 0.0
        free_margin  = round(equity_val - margin_used, 2)
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": equity_val,
            "pnl_unreal": self.account["pnl_unreal"],
            "upnl": self.account["pnl_unreal"],
            "margin_used": margin_used,
            "free_margin": free_margin,
            # used_margin_pct/maintenance_margin/liquidation_distance stay
            # at the DD engine's existing FIX-04 convention (0.0/0.0/
            # equity) — this engine was never gated into compute_margin_
            # state() (RETAIL-only, see _recalc_account_and_push), and
            # extending that gate is out of this block's scope.
            "used_margin_pct": 0.0,
            "maintenance_margin": 0.0,
            "liquidation_distance": equity_val,
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),
            "account_type": self.account.get("account_type", "CHALLENGE"),
            "total_dd_pct": total_dd_pct,
            "daily_dd_pct": daily_dd_pct,
            "daily_pnl": round(daily_pnl, 2),
            "margin_level": margin_level,
            "bid": round(self.get_bid(self.symbol), step_decimals_for(self.symbol)[1]),
            "ask": round(self.get_ask(self.symbol), step_decimals_for(self.symbol)[1]),
            "spread": 0.0,
            "profit_target": self.account.get("profit_target", 800.0),
            "initial_balance": self.account.get("initial_balance", self.account.get("balance", 0.0)),
        })

    async def _do_retail_liquidation(self) -> None:
        """FIX-03 — RETAIL stop-out (margin-level liquidation): close all
        positions, account stays ACTIVE. Triggers when margin_level <
        stopout_level — this is Stop-Out, not Margin Call (Margin Call is
        the separate order-entry gate in _compute_pretrade_margin_guard();
        it never closes positions). Unlike _do_stopout (DD engine), no
        account suspension."""
        _stopout_threshold = self.account.get("stopout_level", 70.0)
        log.warning("[stopout] margin_level<%.0f%% equity=%.2f margin=%.2f account #%s",
                    _stopout_threshold, self.account["equity"],
                    self.account.get("margin_used", 0.0), self._db_account_id)
        closed_items = []
        failed_positions = []
        now_ts = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0
        saw_stale_close = False

        for p in list(self._positions):
            sym  = p["symbol"]
            dec  = step_decimals_for(sym)[1]
            # O.6c-1s — same price-authority fail-safe as _do_stopout()
            # above: self._feed_close_price() (FeedManager global), never
            # close_price()/self._bid_state — see O.6c-1r. No fresh price
            # -> refuse to liquidate this position (never a fictitious
            # price/loss); it stays open via failed_positions.
            _raw_cpx = self._feed_close_price(sym, p["side"])
            if _raw_cpx is None:
                log.warning("[stopout] SKIPPED pos %s sym=%s — no fresh FeedManager price "
                            "(fail-safe, no synthetic fallback)", p["id"], sym)
                failed_positions.append(p)
                continue
            cpx  = round(_raw_cpx, dec)
            realized = self._realized_pnl_for(p, cpx)
            new_balance = running_balance + realized
            fp_p = self._unrealized_pnl_for(p, cpx)
            remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
            new_equity = round(new_balance + remaining_floating, 2)
            pricing_context_close = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_MARGIN_CALL)
            try:
                result = await self._db_close_position_atomic(
                    p, cpx, "stopout", realized, new_balance, new_equity,
                    pricing_context_close=pricing_context_close,
                )
            except Exception as exc:
                log.error("[stopout] DB close failed pos %s: %s", p["id"], exc)
                failed_positions.append(p)
                continue

            # PANEL-03 — position is gone from DB either way once we reach
            # here — never re-add to failed_positions.
            outcome = self._handle_close_result(p, result, cpx, "stopout", realized, now_ts)
            if outcome is None:
                saw_stale_close = True
            else:
                running_balance = outcome["new_balance"]
                accum_floating_closed += fp_p
                self._track_daily_pnl(realized)
                closed_items.append(outcome["notify_item"])

        # DB commits done — update memory with whatever ACTUALLY remains.
        # STOPOUT LIQUIDATION OUTCOME INTEGRITY — pnl_unreal/margin_used
        # recalculated from self._positions (now failed_positions) via
        # the SAME pure helpers the per-tick path already uses — never
        # hardcoded to 0.0. When remaining_count==0 (FULL) these
        # naturally compute to 0, preserving the exact prior behavior for
        # that case; when positions remain (EMPTY/PARTIAL) they now
        # reflect real exposure instead of a fabricated zero.
        self._positions = failed_positions
        closed_count    = len(closed_items)
        remaining_count = len(failed_positions)
        self.account["pnl_unreal"]  = round(self._unrealized_pnl_total(), 2)
        self.account["margin_used"] = round(self._margin_used_total(), 2)
        if saw_stale_close:
            await self._refresh_account_after_stale_close()
        else:
            self.account["balance"] = running_balance
            self.account["equity"]  = round(self.account["balance"] + self.account["pnl_unreal"], 2)

        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self._refresh_and_send_positions()
        # account:stopout fires when this call actually resolved something:
        # either a real close of its own (closed_count>0) or full resolution
        # via remaining_count==0. Deliberately NOT "closed_count > 0" alone:
        # a position closed by a concurrent connection/daemon between this
        # loop starting and this position's own _db_close_position_atomic
        # call lands in neither closed_items nor failed_positions (PANEL-03's
        # already_closed/"stale close" guard, _handle_close_result returning
        # None) — it's genuinely gone, just not via THIS call's own atomic
        # close. A batch resolved entirely that way would have
        # closed_count==0 AND remaining_count==0, which must still notify as
        # a full resolution (mirrors _do_stopout()'s is_full fix). Never
        # sent on a genuine EMPTY attempt (closed_count==0 and
        # remaining_count>0), which would falsely claim positions were
        # liquidated when none were. partial/closed_count/remaining_count
        # are additive fields — existing consumers reading only
        # reason/stopout_level/balance are unaffected.
        if closed_count > 0 or remaining_count == 0:
            await self.send_json({
                # FIX-03 — was "account:margin_call": the trigger is
                # stopout_level, not margin_call_level — Margin Call never
                # closes positions (see _compute_pretrade_margin_guard). Zero
                # real consumers of the old name existed (confirmed during
                # audit), so no compatibility shim.
                "type": "account:stopout",
                "reason": "margin_level_below_stopout",
                "stopout_level": _stopout_threshold,
                "balance": round(self.account["balance"], 2),
                "partial": remaining_count > 0,
                "closed_count": closed_count,
                "remaining_count": remaining_count,
            })
        dec = step_decimals_for(self.symbol)[1]
        balance = self.account["balance"]
        margin_used = self.account["margin_used"]
        equity_val  = self.account["equity"]
        margin_level = round(equity_val / margin_used * 100, 2) if margin_used > 0 else 0.0
        free_margin  = round(equity_val - margin_used, 2)
        from .risk_engine import compute_margin_state
        _ms = compute_margin_state(
            equity_val, margin_used,
            stopout_level=self.account.get("stopout_level", 70.0),
        )
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": equity_val,
            "pnl_unreal": self.account["pnl_unreal"], "upnl": self.account["pnl_unreal"],
            "margin_used": margin_used, "free_margin": free_margin,
            "used_margin_pct": _ms["used_margin_pct"],
            "maintenance_margin": _ms["maintenance_margin"],
            "liquidation_distance": _ms["liquidation_distance"],
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),  # stays Active
            "account_type": "RETAIL",
            "total_dd_pct": 0.0, "daily_dd_pct": 0.0,
            "daily_pnl": round(self._daily_realized_pnl, 2),
            "margin_level": margin_level,
            "margin_call_level": self.account.get("margin_call_level", 100.0),
            "stopout_level": self.account.get("stopout_level", 70.0),
            "bid": round(self.get_bid(self.symbol), dec),
            "ask": round(self.get_ask(self.symbol), dec),
            "spread": 0.0,
            "profit_target": self.account.get("profit_target", 0.0),
            "initial_balance": self.account.get("initial_balance", balance),
        })

    # ---------------- PANEL-03 — shared close-result handling ----------------
    def _handle_close_result(self, pos: dict, result: dict, close_px: float,
                              reason: str, realized_pnl: float, ts: int) -> dict | None:
        """PANEL-03 — the ONE place every close path (_order_close,
        _check_tp_sl, _do_stopout, _do_retail_liquidation) funnels a
        _db_close_position_atomic result through, so all four apply the
        SAME already_closed guard _order_close already had (ACCOUNT-02)
        instead of four independent, partially-correct copies.

        Real close (result["already_closed"] is falsy): returns a dict
        with the authoritative DB values (new_balance/new_peak/new_status)
        plus a ready-to-send order_close notify payload — the caller
        applies these to self.account and sends the notification.

        Stale close (already_closed=True — a concurrent connection or the
        daemon already closed this exact position before this
        transaction's lock was acquired; see
        _db_close_position_atomic's own already_closed branch): returns
        None. The caller MUST NOT fold this into any balance/equity
        arithmetic, MUST NOT send an order_close for it (nothing was
        closed by THIS action — close_px/realized_pnl here are this
        connection's own stale, pre-lock estimate, not what actually
        happened), and MUST NOT count it toward daily PnL tracking. The
        position is gone from DB either way — the caller removes it from
        self._positions regardless of this return value, and must call
        _refresh_account_after_stale_close() once per batch if this
        returned None for any position, per FASE 4.
        """
        if result.get("already_closed"):
            log.info(
                "[close] pos id=%r already closed by a concurrent connection/daemon "
                "— not fabricating a close event, not trusting stale balance/equity/pnl",
                pos.get("id"),
            )
            return None
        # ORDER-MANAGEMENT-V2B — result["realized_pnl"]/result["close_qty"]
        # are the AUTHORITATIVE, lock-computed values (_db_close_position_
        # atomic's own docstring) — always preferred over this method's
        # own realized_pnl/pos["qty"] parameters (the caller's pre-lock
        # estimate). For every pre-V2B caller (full close only) these are
        # numerically identical to the old parameters in the non-racing
        # case, since both derive from the same pnl_engine formula over
        # the same inputs — never a behavior change for the common case,
        # only a correctness improvement under a genuine race.
        authoritative_realized = result.get("realized_pnl", realized_pnl)
        authoritative_qty      = result.get("close_qty", pos["qty"])
        return {
            "new_balance": result["new_balance"],
            "new_peak": result.get("new_peak"),
            "new_status": result.get("new_status"),
            "notify_item": {
                "id": pos["id"], "symbol": pos["symbol"], "side": pos["side"],
                "qty": authoritative_qty, "avg": pos["avg"],
                "close_px": close_px, "reason": reason,
                "realized_pnl": authoritative_realized, "ts": ts,
                "trade_id": result.get("trade_id"),
                "partial": result.get("partial", False),
                "remaining_qty": result.get("remaining_qty"),
            },
        }

    async def _refresh_account_after_stale_close(self) -> None:
        """PANEL-03 FASE 4 — called once per close-path batch/attempt that
        encountered at least one already_closed collision (see
        _handle_close_result). Forces a fresh, non-throttled read of
        TradingAccount.balance from DB — never trusts whatever
        running_balance the caller accumulated locally across the batch,
        since that bookkeeping silently stops being trustworthy the
        moment even one collision is skipped (the caller correctly no
        longer folds the stale echoed-back value into it, but nothing
        upstream can retroactively prove running_balance still reflects
        every real change once that happens — a fresh read is the only
        way to be sure).

        Reuses _db_sync_account_balances() (ACCOUNT-02) verbatim — reads
        balance fresh (read-only), persists only derived equity — no new
        formula. On DB failure: logs and leaves self.account untouched,
        never fabricates/zeros the last known state (same contract as
        _recalc_account_and_push's own DB-sync failure handling).
        """
        try:
            fresh_balance = await self._db_sync_account_balances()
        except Exception as exc:
            log.error(
                "[close] balance refresh after already_closed FAILED for account=%s "
                "— keeping previous in-memory state: %r",
                self._db_account_id, exc, exc_info=True,
            )
            return
        if fresh_balance is not None:
            self.account["balance"] = fresh_balance
            self.account["equity"] = round(
                fresh_balance + float(self.account.get("pnl_unreal", 0.0) or 0.0), 2,
            )

    async def _check_tp_sl(self, symbol: str, bid: float, ask: float):
        dec = step_decimals_for(symbol)[1]
        remaining, closed = [], []
        now = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0
        saw_stale_close = False

        for p in self._positions:
            if p["symbol"] != symbol:
                remaining.append(p); continue

            side = p["side"]; sl = p.get("sl"); tp = p.get("tp")
            # BUY: triggers checked against BID (the price you'd exit at)
            # SELL: triggers checked against ASK
            trigger_px = bid if side == "buy" else ask
            fill_px    = bid if side == "buy" else ask  # same: close at bid/ask

            trail = p.get("trail_dist")
            if trail and trail > 0:
                if side == "buy":
                    p["best"] = max(p.get("best", p["avg"]), bid)
                    p["sl"] = round(p["best"] - trail, dec)
                    sl = p["sl"]
                else:
                    p["best"] = min(p.get("best", p["avg"]), ask)
                    p["sl"] = round(p["best"] + trail, dec)
                    sl = p["sl"]

            sl_hit = sl is not None and ((side=="buy" and trigger_px<=sl) or (side=="sell" and trigger_px>=sl))
            tp_hit = tp is not None and ((side=="buy" and trigger_px>=tp) or (side=="sell" and trigger_px<=tp))

            if sl_hit or tp_hit:
                close_px    = round(fill_px, dec)
                realized    = self._realized_pnl_for(p, close_px)
                new_balance = running_balance + realized
                fp_p        = self._unrealized_pnl_for(p, close_px)
                remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
                new_equity  = round(new_balance + remaining_floating, 2)
                reason = "tp" if tp_hit else "sl"
                _profile = pricing_ctx.PROFILE_WS_TP if tp_hit else pricing_ctx.PROFILE_WS_SL
                pricing_context_close = self._capture_pricing_context(symbol, profile=_profile)
                try:
                    result = await self._db_close_position_atomic(
                        p, close_px, reason, realized, new_balance, new_equity,
                        pricing_context_close=pricing_context_close,
                    )
                except Exception as exc:
                    log.error("[tp_sl] db close FAILED pos id=%r: %s", p["id"], exc, exc_info=True)
                    remaining.append(p)
                    continue

                # PANEL-03 — the position is gone from DB either way once
                # we reach here (this call really closed it, OR a
                # concurrent connection/daemon already did) — never re-add
                # to remaining. _handle_close_result decides whether it's
                # safe to trust the returned balance/notify a close.
                outcome = self._handle_close_result(p, result, close_px, reason, realized, now)
                if outcome is None:
                    saw_stale_close = True
                else:
                    running_balance = outcome["new_balance"]
                    accum_floating_closed += fp_p
                    closed.append(outcome["notify_item"])
            else:
                remaining.append(p)

        if closed or saw_stale_close:
            self._positions = remaining
            if saw_stale_close:
                # At least one collision — force a fresh, non-throttled
                # balance/equity read rather than trust running_balance,
                # which stops being provably correct the moment even one
                # collision is skipped (see _refresh_account_after_stale_close).
                await self._refresh_account_after_stale_close()
            else:
                self.account["balance"] = running_balance
            if closed:
                self._track_daily_pnl(sum(c["realized_pnl"] for c in closed))
            await self._recalc_account_and_push()
            for c in closed: await self.send_json({"type":"order_close", **c})
            await self._refresh_and_send_positions()

    # ---------------- DB helpers (best-effort) ----------------
    async def _refresh_and_send_positions(self):
        """MULTIPANEL-01 — the ONE place allowed to emit a full 'positions'
        snapshot. Every panel is its own WebSocket connection with its own
        TradingConsumer instance and its own self._positions, hydrated at
        connect() time and re-synced with sibling connections for the same
        account whenever the book changes (O.6c-1o — position_changed(),
        pushed via the account_{account_id} Channels group by every
        Position writer; see ws_events.py). Before O.6c-1o, no such
        propagation existed for WS-originated opens/closes/SL/TP/stopout/
        liquidation, only for the 2 Celery daemon close paths — a
        connection that opened/closed/edited a position, or merely
        switched symbol, could emit its own possibly-incomplete in-memory
        view, and the frontend would propagate that snapshot to every
        panel, silently discarding positions opened through OTHER panels
        (the original root cause of the multipanel "position disappears"
        bug). This function itself is unchanged — it remains the sole
        DB-fresh authority _refresh_and_send_positions()/position_changed()
        funnel through; O.6c-1o only added WHO calls it and WHEN.

        The DB is the single source of truth: this always re-hydrates
        self._positions from Position.objects (account-wide, via the
        existing _db_fetch_open_positions()) immediately before building
        and sending the snapshot — never trusts whatever this connection's
        memory happened to accumulate on its own.

        Demo/guest sessions (no _db_account_id, hence no DB-backed
        account) are the one exception: self._positions is the ONLY
        source of truth for them (positions never persist to DB), so this
        skips the re-fetch entirely and sends the in-memory state as-is —
        re-fetching would incorrectly wipe it to [] every time.

        On DB failure: never wipes self._positions to [] and never sends a
        fabricated empty snapshot — logs the error, sends the last known
        (possibly stale but non-fabricated) state, and leaves the socket
        open. A stale-but-real snapshot is safer than inventing "no
        positions" for an account that may well have some.
        """
        if not self._db_account_id:
            await self.send_json({"type": "positions", "items": self._positions_snapshot()})
            return

        try:
            items = await self._db_fetch_open_positions()
        except Exception as exc:
            log.error("[positions] refresh failed for account=%s — keeping previous state: %r",
                      self._db_account_id, exc, exc_info=True)
            await self.send_json({"type": "positions", "items": self._positions_snapshot()})
            return

        self._positions = [
            {
                "id": it["id"], "symbol": it["symbol"], "side": it["side"].lower(),
                "qty": float(it["qty"]), "avg": float(it["avg_price"]),
                "sl": it.get("sl"), "tp": it.get("tp"),
                "opened_at": it.get("opened_ts", int(time.time())),
            }
            for it in items
        ]
        log.info("[positions] refreshed account=%s: %d position(s) ids=%s",
                 self._db_account_id, len(self._positions), [p["id"] for p in self._positions])
        await self.send_json({"type": "positions", "items": self._positions_snapshot()})

    async def send_positions_snapshot(self):
        """Backwards-compatible name — delegates to the canonical helper."""
        await self._refresh_and_send_positions()

    async def _maybe_hydrate_from_db(self):
        if not self._db_account_id:
            log.warning("[hydrate] SKIPPED — db_account_id is None")
            return
        log.info("[hydrate] loading account #%s from DB", self._db_account_id)
        acc = await self._db_read_account(self._db_account_id)
        if not acc:
            log.warning("[hydrate] account #%s not found in DB", self._db_account_id)
            return

        self.account["balance"]      = float(acc.get("balance",      self.account["balance"]))
        self.account["equity"]       = float(acc.get("equity",       self.account["equity"]))
        self.account["peak_balance"] = float(acc.get("peak_balance", self.account["balance"]))
        self.account["leverage"]     = int(acc.get("leverage",       self.account["leverage"]))
        self.account["currency"]     = acc.get("currency", self.account["currency"])
        self.account["netting_mode"] = bool(acc.get("netting_mode",  self.account["netting_mode"]))
        self.account["status"]          = acc.get("status", "Activo")
        self.account["tier"]            = acc.get("tier", "")
        self.account["account_type"]    = acc.get("account_type", "CHALLENGE")
        self.account["profit_target"]   = float(acc.get("profit_target") or 0.0)
        # Use the stored initial_balance from DB; fall back to current balance, never to a tier dict.
        self.account["initial_balance"] = float(
            acc.get("initial_balance") or self.account["balance"]
        )
        # Phase 6B — product rule snapshots (None = not set, fallback to spec/default)
        self.account["product_name"]      = acc.get("product_name", "")
        self.account["commission_per_lot"] = acc.get("commission_per_lot", 0.0)
        self.account["commission_pct"]     = acc.get("commission_pct", 0.0)
        self.account["spread_pips"]        = acc.get("spread_pips", 0.0)
        self.account["allowed_symbols"]    = acc.get("allowed_symbols", None)
        self.account["max_lot_size"]       = acc.get("max_lot_size", None)
        # FIX-03 — V1 policy is 100/70. These two .get(..., X) fallbacks
        # are purely defensive (acc always carries a real value here —
        # _db_read_account() already resolved obj.stopout_level_snapshot
        # or 50 for a genuinely legacy NULL-snapshot row; that "or 50" is
        # the one place pre-Phase-6B no-retroactivity is preserved and
        # must NOT change). If this key were ever truly absent from acc
        # (a bug, not a data case), defaulting to the current platform
        # policy (70) is the deliberately-chosen, internally-consistent
        # fallback — matches every other stopout_level fallback below.
        self.account["margin_call_level"]  = acc.get("margin_call_level", 100.0)
        self.account["stopout_level"]      = acc.get("stopout_level", 70.0)
        # O.6c-1e — same fallback discipline as the two lines above.
        self.account["max_margin_per_trade_pct"] = acc.get("max_margin_per_trade_pct", 10.0)
        self.account["max_total_margin_pct"]     = acc.get("max_total_margin_pct", 50.0)
        # SPREAD-04 — cached once here; commission_for()/price_tick() read
        # it back, never re-resolving (no DB per-tick).
        self.account["commercial_pricing_fields"] = acc.get("commercial_pricing_fields", {})
        log.info("[hydrate] balance=%.2f equity=%.2f status=%s tier=%s product=%r comm_per_lot=%.2f",
                 self.account["balance"], self.account["equity"],
                 self.account["status"], self.account["tier"],
                 self.account["product_name"], self.account["commission_per_lot"])

        items = await self._db_fetch_open_positions()
        self._positions = []
        for it in items:
            self._positions.append({
                "id":it["id"], "symbol":it["symbol"], "side":it["side"].lower(),
                "qty":float(it["qty"]), "avg":float(it["avg_price"]),
                "sl":it.get("sl"), "tp":it.get("tp"),
                "opened_at":it.get("opened_ts", int(time.time())),
            })
        if self._positions:
            self._order_seq = max(int(p["id"]) for p in self._positions) + 1
        else:
            self._order_seq = 1
        log.info("[hydrate] loaded %d open position(s): %s — _order_seq set to %d",
                 len(self._positions), [(p["id"], p["symbol"], p["side"]) for p in self._positions],
                 self._order_seq)

        daily_pnl = await self._db_fetch_daily_pnl()
        self._daily_realized_pnl = daily_pnl
        from django.utils import timezone as _tz
        self._daily_pnl_date = _tz.now().date()
        log.info("[hydrate] daily_realized_pnl=%.2f for %s", self._daily_realized_pnl, self._daily_pnl_date)

        # ORDER-MANAGEMENT-V2A
        pending_items = await self._db_fetch_pending_orders()
        self._pending_orders = pending_items
        log.info("[hydrate] loaded %d pending order(s): %s",
                 len(self._pending_orders), [(p["id"], p["symbol"], p["order_type"], p["side"]) for p in self._pending_orders])

    @database_sync_to_async
    def _db_suspend_account(self, reason: str) -> None:
        """ACCOUNT-02 — only ever sets status. balance/equity are NOT
        written here: by the time this is called (after _do_stopout's
        position-closing loop), every real balance change already
        persisted correctly via _db_close_position_atomic's own
        fresh-locked-read write. Re-writing balance/equity from
        self.account here added no value and reintroduced exactly the
        same possibly-stale-memory risk this block eliminates elsewhere —
        removed rather than "frozen fresh", since there is nothing left
        for this function to legitimately compute."""
        if not self._db_account_id:
            return
        from django.db import transaction
        from decimal import Decimal
        with transaction.atomic():
            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account:
                account.status  = "Suspendido"
                account.save(update_fields=["status"])
                LedgerEntry.objects.create(
                    account=account,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=Decimal("0"),
                    balance_after=account.balance,
                    meta={"reason": reason},
                )

    @database_sync_to_async
    def _db_get_account_for_user(self, acc_id:int, user_id:int):
        try:
            obj = TradingAccount.objects.get(id=acc_id, user_id=user_id)
            return {"id":obj.id}
        except TradingAccount.DoesNotExist:
            return None

    @database_sync_to_async
    def _db_get_latest_account_for_user(self, user_id):
        if not user_id:
            return None
        obj = (TradingAccount.objects
               .filter(user_id=user_id, status="Activo")
               .order_by("-id")
               .first())
        return {"id": obj.id} if obj else None

    @database_sync_to_async
    def _db_read_account(self, acc_id: int):
        try:
            obj = TradingAccount.objects.get(id=acc_id)
            # SPREAD-04 — single commercial pricing resolver for every
            # account type (see simulator/commercial_pricing.py); replaces
            # the direct obj.spread_pips_snapshot/commission_per_lot_snapshot
            # reads that never resolved anything for CHALLENGE/FUNDED
            # accounts (they never had a snapshot written at all).
            from .commercial_pricing import resolve_commercial_pricing_fields
            commercial_fields = resolve_commercial_pricing_fields(obj)
            return {
                "id":              obj.id,
                "account_type":    obj.account_type,
                "balance":         obj.balance,
                "equity":          obj.equity,
                "peak_balance":    obj.peak_balance,
                "initial_balance": obj.initial_balance,
                "leverage":        getattr(obj, "leverage", 50),
                "currency":        getattr(obj, "currency", "USD"),
                "netting_mode":    getattr(obj, "netting_mode", False),
                "status":          obj.status,
                "tier":            obj.tier or "",
                "profit_target":   float(obj.profit_target) if obj.profit_target is not None else 0.0,
                # Phase 6B — product snapshots (risk/eligibility, unrelated to commercial pricing)
                "product_name":          obj.product_name_snapshot or "",
                "allowed_symbols":       obj.allowed_symbols_snapshot,
                "max_lot_size":          float(obj.max_lot_size_snapshot) if obj.max_lot_size_snapshot is not None else None,
                "margin_call_level":     float(obj.margin_call_level_snapshot or 100),
                "stopout_level":         float(obj.stopout_level_snapshot or 50),
                # O.6c-1e — fallback matches _DEFAULT_MAX_MARGIN_PER_TRADE_PCT/
                # _DEFAULT_MAX_TOTAL_MARGIN_PCT exactly (10.0/50.0) — a NULL
                # snapshot (every account created before this block existed)
                # reproduces today's behavior bit for bit.
                "max_margin_per_trade_pct": float(obj.max_margin_per_trade_pct_snapshot or 10.0),
                "max_total_margin_pct":     float(obj.max_total_margin_pct_snapshot or 50.0),
                # SPREAD-04 — commercial pricing: account-level fields resolved
                # once here (a sync DB context); commission_for()/price_tick()
                # read them back from self.account, never re-resolving.
                "commission_per_lot":       commercial_fields.get("commission_per_lot", 0.0),
                "commission_pct":           commercial_fields.get("commission_pct", 0.0),
                "spread_pips":              commercial_fields.get("spread_markup_pips", 0.0),
                "commercial_pricing_fields": commercial_fields,
            }
        except TradingAccount.DoesNotExist:
            return None

    @database_sync_to_async
    def _db_validate_order_risk(self, lot_size: float, open_positions_count: int,
                                symbol: str = "") -> list[dict]:
        """Returns list of error dicts. Empty = allowed. Creates violations on hard breaches."""
        if not self._db_account_id:
            return []
        from django.db import transaction
        from .risk_engine import validate_order_risk
        with transaction.atomic():
            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if not account:
                return [{"code": "account_not_found", "message": "Cuenta no encontrada"}]
            errors = validate_order_risk(account, lot_size, open_positions_count, symbol)
            # Sync status back to in-memory if account was suspended
            if account.status != self.account.get("status"):
                self.account["status"] = account.status
            return errors

    @database_sync_to_async
    def _db_fetch_open_positions(self):
        if not self._db_account_id: return []
        out=[]
        qs = Position.objects.filter(account_id=self._db_account_id)
        for p in qs:
            out.append({
                "id": p.id, "symbol": p.symbol, "side": p.side,
                "qty": float(p.qty), "avg_price": float(p.avg_price),
                "sl": float(p.sl) if p.sl is not None else None,
                "tp": float(p.tp) if p.tp is not None else None,
                "opened_ts": int(p.opened_at.timestamp()),
            })
        return out

    @database_sync_to_async
    def _db_fetch_pending_orders(self):
        """ORDER-MANAGEMENT-V2A — this connection's PENDING PendingOrder
        rows (only status=PENDING; TRIGGERED/CANCELLED/EXPIRED/REJECTED
        are terminal and irrelevant to the live trigger-check hot path
        that consumes this list)."""
        if not self._db_account_id: return []
        out = []
        qs = PendingOrder.objects.filter(account_id=self._db_account_id, status=PendingOrder.PENDING)
        for p in qs:
            out.append({
                "id": p.id, "symbol": p.symbol, "side": p.side, "order_type": p.order_type,
                "qty": float(p.qty), "trigger_price": float(p.trigger_price),
                "sl": float(p.sl) if p.sl is not None else None,
                "tp": float(p.tp) if p.tp is not None else None,
                "expires_at": p.expires_at.timestamp() if p.expires_at else None,
                "created_ts": int(p.created_at.timestamp()),
            })
        return out

    @database_sync_to_async
    def _db_fetch_daily_pnl(self) -> float:
        if not self._db_account_id:
            return 0.0
        from django.utils import timezone as _tz
        from django.db.models import Sum
        from datetime import timedelta as _td
        today_start = _tz.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + _td(days=1)
        result = (
            LedgerEntry.objects
            .filter(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                created_at__gte=today_start,
                created_at__lt=tomorrow_start,
            )
            .aggregate(total=Sum("amount"))["total"]
        )
        return float(result or 0)

    @database_sync_to_async
    def _db_sync_account_balances(self):
        """ACCOUNT-02 — replaces the old unconditional
        `.update(balance=self.account["balance"], ...)`, which was the
        confirmed root cause of a real lost-update: this method runs on
        every tick (throttled to ~1.2s) for EVERY WebSocket connection of
        an account, and each connection's self.account["balance"] is only
        as fresh as whatever THIS connection itself last processed. A
        sibling panel that opened/closed nothing new could silently
        overwrite another panel's just-realized profit back to a stale
        value — reproduced exactly in the ACCOUNT-01 audit (183.82 ->
        190.82 -> reverted to 183.82).

        balance is realized cash and authoritative in the DB — mutated
        ONLY by real accounting events (close, commission, deposit,
        withdrawal, audited admin adjustment), each already committing
        atomically from a fresh select_for_update() read (see
        _db_close_position_atomic, _db_open_position_atomic). This
        function never writes balance. It READS the fresh balance
        (read-only) and persists ONLY the derived, non-authoritative
        equity = fresh_balance + this connection's own floating PnL —
        safe to overwrite periodically since equity is a display/snapshot
        value, never an input to further balance arithmetic.

        Returns the fresh balance (float) so the caller can refresh
        self.account["balance"] before building any account:update
        payload — or None if there is no DB-backed account, or on any
        failure (never fabricates a value; caller must keep the previous
        state on None).
        """
        if not self._db_account_id:
            return None
        from decimal import Decimal
        account = TradingAccount.objects.filter(id=self._db_account_id).only("balance").first()
        if account is None:
            return None
        fresh_balance = float(account.balance)
        equity = round(fresh_balance + float(self.account.get("pnl_unreal", 0.0) or 0.0), 2)
        TradingAccount.objects.filter(id=self._db_account_id).update(equity=Decimal(str(equity)))
        return fresh_balance

    @database_sync_to_async
    def _db_mirror_open_or_update(self, order_id, symbol, side, qty, price, sl, tp, commission):
        # Deprecated — superseded by _db_open_position_atomic (Phase 1A). No longer called.
        if not self._db_account_id: return
        from decimal import Decimal
        with transaction.atomic():
            if commission and commission>0:
                LedgerEntry.objects.create(
                    account_id=self._db_account_id, event_type=LedgerEntry.EV_COMMISSION,
                    amount=Decimal(-abs(commission)), balance_after=Decimal(self.account["balance"]),
                    meta={"symbol":symbol,"side":side,"client_pos_id":order_id},
                )
            Position.objects.create(
                account_id=self._db_account_id, symbol=symbol, side=side,
                qty=Decimal(qty), avg_price=Decimal(price),
                **({"sl":Decimal(sl)} if sl is not None else {}),
                **({"tp":Decimal(tp)} if tp is not None else {}),
                external_id=str(order_id),
            )

    def _should_activate_routing_decision(self, symbol: str, account_type: str) -> bool:
        """
        BOOK-04f — Controlled Activation Mechanism. Gate for whether
        _db_open_position_atomic() should run the BOOK-04b Shadow Mode
        block at all. Never decides WHAT the decision is — that remains
        the same trivial contract from BOOK-04b regardless of this
        method's answer.

        Lives here, not in routing_engine.py — same caller-owns-the-gate
        precedent already used by FeedManager._should_use_new_router()
        in market_data/feeds.py. No DB access, no new state: reads only
        the symbol/account_type already handed to it and the frozensets
        already loaded into settings at process start. Never raises —
        any unexpected failure here is treated as "no", exactly like
        _should_use_new_router()'s own contract.

        Semantics (see docs/BOOK_04_IMPLEMENTATION_PLAN.md, BOOK-04f —
        "Comportamiento del gate — todos los casos"): an absent/empty
        allowlist means NO restriction on that dimension (opposite of
        MARKET_DATA_ROUTER_SYMBOLS's empty-means-none) — required so
        that any environment with ROUTING_ENGINE_ENABLED already True
        keeps its current 100%-of-orders behavior unchanged until a
        granular allowlist is explicitly set. Both dimensions are ANDed
        when both are non-empty, never ORed.
        """
        try:
            from django.conf import settings as _settings

            if not getattr(_settings, "ROUTING_ENGINE_ENABLED", False):
                return False

            symbols = getattr(_settings, "ROUTING_ENGINE_SYMBOLS", frozenset())
            if symbols and symbol not in symbols:
                return False

            account_types = getattr(_settings, "ROUTING_ENGINE_ACCOUNT_TYPES", frozenset())
            if account_types and account_type not in account_types:
                return False

            return True
        except Exception:
            return False

    @database_sync_to_async
    def _db_open_position_atomic(self, symbol: str, side: str, qty: float, price: float,
                                  sl, tp, commission: float, new_balance: float,
                                  pricing_context: dict | None = None) -> dict:
        """DB-first order open (Phase 1A / PANEL-02).

        Atomically: lock every open Position for this account + the account
        row itself, re-derive margin/position-count/equity FRESH under that
        lock, run the single authoritative validation
        (_compute_atomic_open_guard), and ONLY IF it passes: create/merge
        Position, record commission LedgerEntry/BrokerLedger, update
        TradingAccount.balance — all in the same transaction, before any
        memory mutation in the caller. If it fails, nothing is written —
        no Position, no commission, no Trade/LedgerEntry/BrokerLedger.

        PANEL-02 — this is now the REAL authority for margin/position-count/
        account-status, closing a TOCTOU race: two connections of the same
        account could previously both pass the fast, in-memory pre-lock
        guard (consumers.py:_order_new, using THIS connection's own,
        possibly stale, self.account/self._positions) and both proceed to
        write, jointly exceeding the 10%/50% margin caps or
        max_open_positions — reproduced empirically pre-fix (4 concurrent
        opens + 1 pre-existing position reaching 70.67% total margin
        against the account's own 50% cap). The pre-lock guard in
        _order_new is kept ONLY as a fast, non-authoritative early
        rejection (cheap UX feedback before touching the DB) — it is never
        trusted for the final decision anymore.

        Returns a structured dict — always includes "ok" plus the FASE-5
        field set (error_code, message, required_margin, required_margin_pct,
        projected_total_margin, projected_total_margin_pct,
        max_total_margin_pct, current_open_positions, max_open_positions),
        alongside "position_id"/"merged"/"new_balance". If _db_account_id is
        None (demo session — no DB account to lock/validate against) returns
        {"ok": True, "position_id": None, "merged": False} so the caller
        falls back to _order_seq as a local id, unchanged from before — the
        pre-lock guards remain the sole authority for demo sessions since
        there is no DB account to validate under lock.

        pricing_context (SPREAD-02): stored on the newly-created Position only.
        On a netting merge into an existing Position, the ORIGINAL position's
        pricing_context is left untouched — an averaged fill has no single
        "the" raw/executable price, so preserving the first fill's context is
        more honest than fabricating one for the merge.
        """
        if not self._db_account_id:
            return {"position_id": None, "merged": False, "ok": True, "routing_decision_id": None}
        from decimal import Decimal
        with transaction.atomic():
            # 0. RISK-02 — acquire the broker-wide risk lock FIRST, before
            # ANY other lock in this transaction (see the module-level LOCK
            # ORDER note above this class and BrokerRiskLock's own
            # docstring: BrokerRiskLock -> TradingAccount -> Position).
            # This is what closes the TOCTOU race a pre-lock RISK-02 check
            # would otherwise have: two concurrent opens on DIFFERENT
            # accounts (so TradingAccount locking alone can't serialize
            # them) could each read the same broker-wide exposure, both
            # evaluate PASS, and both write — jointly exceeding a
            # broker-wide limit. Holding this lock from here through commit
            # means only ONE order-open transaction broker-wide is ever
            # "inside" RISK-02 evaluation + Position creation at a time; the
            # exposure recompute at step 8.5 below is therefore guaranteed
            # race-free. Never acquired by any close path — closing only
            # ever REDUCES exposure, so closes don't need to serialize
            # against this gate (see BrokerRiskLock's docstring).
            #
            # get_or_create() first, self-healing: the migration seeds the
            # singleton row once at DB-creation time, but anything that
            # truncates tables without re-running data migrations (Django
            # TransactionTestCase's per-test flush between tests; a manual
            # `flush`/`sqlflush` in ops) would otherwise wipe it and turn
            # every future order-open into a DoesNotExist crash. get_or_create
            # is itself race-safe (Django retries its internal get() once on
            # IntegrityError from a concurrent create) — cheap even in the
            # common case where the row already exists (single-row, PK read).
            from .models import BrokerRiskLock
            _lock_row, _lock_created = BrokerRiskLock.objects.get_or_create(pk=1)
            if _lock_created:
                # RISK-03 — the singleton was missing and had to be
                # recreated here; stamp a durable marker so
                # broker_alerts.py can surface it (see the model
                # docstring's last_recreated_at note). Not part of the
                # mutex itself — a plain field write under the lock we
                # already hold.
                _lock_row.last_recreated_at = timezone.now()
                _lock_row.save(update_fields=["last_recreated_at"])
            BrokerRiskLock.objects.select_for_update().get(pk=1)

            # 1. Lock the TradingAccount row — this account's own mutex
            # (see the module-level LOCK ORDER note above this
            # class: global order is BrokerRiskLock → TradingAccount →
            # Position across EVERY live path that opens a position).
            # Locking Account first — even when the
            # account currently has ZERO open positions — is what makes it
            # an actual mutex: a concurrent transaction for the SAME
            # account blocks here until this one commits, no matter how
            # many Position rows exist right now. The earlier
            # Position-first design failed to serialize exactly that empty
            # case: select_for_update() locks zero rows when there is
            # nothing to lock, so two connections could each read
            # positions=[] before either held any lock at all, then each
            # separately proceed to lock Account in turn — the second one
            # would still validate against its own STALE (empty) snapshot
            # taken before the first one's commit. Locking Account first
            # closes that gap: by the time this transaction reads Position
            # below, any sibling transaction for this account has either
            # already committed (visible now) or is blocked waiting for
            # this lock (hasn't happened yet — nothing to miss).
            account = (
                TradingAccount.objects
                .select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account is None:
                return {
                    "ok": False, "error_code": "account_not_found",
                    "message": "Cuenta no encontrada",
                    "position_id": None, "merged": False, "new_balance": new_balance,
                    "routing_decision_id": None,
                    "required_margin": 0.0, "required_margin_pct": 0.0,
                    "projected_total_margin": 0.0, "projected_total_margin_pct": 0.0,
                    "max_total_margin_pct": _DEFAULT_MAX_TOTAL_MARGIN_PCT,
                    "current_open_positions": 0, "max_open_positions": 0,
                }

            # 2. NOW lock every open Position for this account — issued
            # and evaluated (list(...) forces immediate execution) STRICTLY
            # AFTER the Account lock above is held, so this list is
            # guaranteed fresh: it reflects every write any other
            # transaction for this same account has already committed (see
            # the reasoning in step 1). .order_by("id") keeps multi-row
            # lock acquisition order deterministic — defensive: with
            # Account as the outer mutex, no two transactions ever hold
            # overlapping Position locks for the same account
            # simultaneously, but this remains correct and costs nothing
            # if that invariant is ever weakened. Verified via captured
            # query order in test_atomic_guard_lock_order.py (Account
            # query precedes Position query). This locked list is the
            # fresh, authoritative source for position count,
            # netting-merge-target lookup, and margin_used — never
            # self._positions (this connection's own, possibly stale,
            # in-memory mirror).
            open_positions = list(
                Position.objects.select_for_update()
                .filter(account=account)
                .order_by("id")
            )

            # 3. Netting merge target — same symbol+side, found within the
            # ALREADY-LOCKED open_positions list (no second query).
            # Hedging mode (netting_mode False) never merges — unchanged
            # semantics. FASE 4 — a same-side merge does NOT create a new
            # Position row, so it must not count against max_open_positions;
            # its margin contribution is already linear (weighted-average
            # notional == sum of the two legs' notional), so no separate
            # "projected merged margin" formula is needed — the plain
            # additive required_margin/fresh_margin_used sum below is
            # already exact for both the merge and the new-position case.
            existing = None
            if self.account.get("netting_mode"):
                existing = next(
                    (p for p in open_positions if p.symbol == symbol and p.side == side.upper()),
                    None,
                )
            is_new_position = existing is None
            fresh_open_count = len(open_positions)

            # 4. Fresh margin_used — derived from the locked Position rows'
            # own avg_price/qty (DB Decimal fields), never from
            # self._positions/self._margin_used_total(). Same formula as
            # _margin_used_total() (FIX-USDJPY-MARGIN-01-B: same shared
            # base/quote-aware helper), just fed DB-fresh data — no price
            # dependency at all (margin uses each position's own entry
            # price, not a live price).
            account_lev = max(1, int(self.account.get("leverage", 50)))
            fresh_margin_used = 0.0
            for _p in open_positions:
                _pspec = get_spec(_p.symbol)
                _plev = max(1, min(account_lev, _pspec.max_leverage))
                _pmargin, _pmargin_error = pnl_engine.calculate_required_margin(
                    _p.symbol, float(_p.avg_price), float(_p.qty), _plev, account.currency,
                )
                if _pmargin is None:
                    # Unreachable today — see _margin_used_total()'s
                    # identical comment for the full reasoning.
                    log.critical(
                        "[margin] event=margin_conversion_unsupported symbol=%s "
                        "account_currency=%s error_code=%s — contributing 0.0 to "
                        "fresh_margin_used, NOT a fabricated number.",
                        _p.symbol, account.currency, _pmargin_error,
                    )
                    continue
                fresh_margin_used += _pmargin

            # 5. max_open_positions — fetched now (price-independent) so
            # it's available in the structured response even if step 6
            # below rejects the order for an unpriced/stale symbol.
            from .risk_engine import get_or_create_risk_rule
            _rule = get_or_create_risk_rule(account)

            # 6. INVARIANTE 1 (PANEL-02 correction) — fresh_equity requires
            # a REAL, fresh floating PnL for EVERY open position; a
            # missing or stale price is NEVER treated as floating PnL=0.
            # Zero is not conservative: a losing position with no
            # available price would make fresh_equity look HIGHER than
            # reality (real equity = balance + true_floating, which could
            # be deeply negative), which could let an order through that a
            # correct equity read would have rejected. The only safe
            # options are "use a real, fresh price" or "refuse to decide"
            # — never invent a number. If ANY open position's symbol has
            # no live/cached price, or that price is older than
            # FeedManager.has_price()'s freshness TTL, the ENTIRE order is
            # rejected here — before fresh_equity is even computed — with
            # nothing written (no Position, no commission, no Trade/
            # LedgerEntry/BrokerLedger), same as any other rejection path.
            _unpriced_symbols = [p.symbol for p in open_positions if not self._feed.has_price(p.symbol)]
            if _unpriced_symbols:
                log.warning(
                    "[atomic_guard] account=%s REJECTED — no fresh price for open "
                    "position symbol(s) %s; refusing to compute fresh_equity rather "
                    "than assume floating PnL=0",
                    self._db_account_id, _unpriced_symbols,
                )
                return {
                    "ok": False, "error_code": "market_price_unavailable",
                    "message": (
                        "Orden rechazada: no se pudo verificar el estado de riesgo de la "
                        "cuenta — precio no disponible o desactualizado para: "
                        + ", ".join(_unpriced_symbols) + "."
                    ),
                    "position_id": None, "merged": False, "new_balance": float(account.balance),
                    "routing_decision_id": None,
                    "required_margin": 0.0, "required_margin_pct": 0.0,
                    "projected_total_margin": round(fresh_margin_used, 4),
                    "projected_total_margin_pct": 0.0,
                    "max_total_margin_pct": _DEFAULT_MAX_TOTAL_MARGIN_PCT,
                    "current_open_positions": fresh_open_count,
                    "max_open_positions": _rule.max_open_positions,
                }

            # 7. Fresh equity — PANEL-02 FASE 3: floating PnL of every open
            # position, sourced from the shared, per-process FeedManager
            # (self._feed) — the SAME cache _seed_price_state()/
            # exec_price() already read from — never this connection's own
            # _bid_state/_ask_state (only seeded for symbols THIS
            # connection has viewed). Every position here is guaranteed
            # freshly priced by step 6 above — no fallback/zero case
            # remains to handle.
            fresh_floating_pnl = 0.0
            for _p in open_positions:
                _close_px = (
                    self._feed.last_bid(_p.symbol) if _p.side == "BUY"
                    else self._feed.last_ask(_p.symbol)
                )
                fresh_floating_pnl += pnl_engine.position_pnl_float(
                    _p.side.lower(), float(_p.avg_price), _close_px, float(_p.qty), _p.symbol,
                    account_currency=account.currency,
                )
            fresh_equity = float(account.balance) + fresh_floating_pnl

            # 8. Authoritative validation — the ONE place this order can be
            # accepted or rejected. See _compute_atomic_open_guard's
            # docstring for the full rationale.
            _spec = get_spec(symbol)
            _account_snap = {
                "leverage":          self.account.get("leverage", 50),
                "allowed_symbols":   self.account.get("allowed_symbols"),
                "max_lot_size":      self.account.get("max_lot_size"),
                "margin_call_level": self.account.get("margin_call_level"),
                # O.6c-1e
                "max_margin_per_trade_pct": self.account.get("max_margin_per_trade_pct", _DEFAULT_MAX_MARGIN_PER_TRADE_PCT),
                "max_total_margin_pct":     self.account.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
            }
            guard = _compute_atomic_open_guard(
                symbol, qty, price, account.status, _account_snap, _spec,
                fresh_equity, fresh_margin_used, is_new_position, fresh_open_count,
                _rule.max_open_positions,
                account_currency=account.currency,
            )
            if not guard["ok"]:
                # Rejected under lock — nothing is created, no commission
                # charged, no Trade/LedgerEntry/BrokerLedger written. The
                # transaction commits with zero writes (a no-op).
                log.info(
                    "[db_open] REJECTED account=%s symbol=%s side=%s qty=%s code=%s",
                    self._db_account_id, symbol, side, qty, guard["error_code"],
                )
                return {
                    "position_id": None, "merged": False, "new_balance": float(account.balance),
                    "routing_decision_id": None,
                    **guard,
                }

            # 8.5 RISK-02 — broker-wide risk limits. Runs here, inside the
            # SAME transaction that has held BrokerRiskLock since step 0,
            # AFTER the per-account guard passes and BEFORE Position
            # creation — this is the single transactional point the FASE 5
            # correction requires ("LOCK → recalcular exposición actual
            # desde DB → evaluar las 9 reglas → crear Position → commit").
            # The exposure this reads (via broker_exposure.py) is
            # guaranteed fresh and race-free: every other concurrent
            # order-open transaction broker-wide is either fully committed
            # (visible now) or still blocked on BrokerRiskLock.
            from .broker_risk import validate_new_order
            _risk02 = validate_new_order(
                account_id=self._db_account_id, symbol=symbol, side=side, qty=qty,
                price=price, contract_size=_spec.contract_size,
                # O.6c-1b: the account row is already loaded/locked above
                # (step 1) — reuse it, never a second query. REAL money
                # account types (RETAIL/ECN/STANDARD/CRYPTO) get RISK-02
                # evaluated under risk_scope="real" (DEMO/CHALLENGE/FUNDED
                # can no longer contaminate their pricing coverage or
                # broker-wide limits); every other account_type keeps the
                # full legacy, unscoped evaluation — see
                # broker_risk.py::_risk_scope_for_account_type().
                account_type=account.account_type,
            )
            if not _risk02.allowed:
                log.info(
                    "[db_open] REJECTED account=%s symbol=%s side=%s qty=%s code=%s (RISK-02)",
                    self._db_account_id, symbol, side, qty, _risk02.reason_code,
                )
                # AUDIT-01 — "Broker Risk FAIL". Recorded inside this same
                # nested savepoint (record_event's own transaction.atomic())
                # so a write failure here can never affect the outer
                # transaction, which is about to roll back to a no-op
                # anyway (nothing was created). actor_type=SYSTEM: the
                # rejection is the risk engine's decision, not the
                # trader's action.
                from . import broker_audit as _audit
                _audit.record_risk_event(
                    event_type=_audit.EV_RISK_ORDER_REJECTED,
                    description=f"Order rejected on {symbol} {side.upper()} qty={qty}: {_risk02.reason_code}",
                    account_id=self._db_account_id, symbol=symbol,
                    source_module="simulator.broker_risk",
                    metadata={
                        "side": side, "qty": qty, "price": price,
                        "reason_code": _risk02.reason_code,
                        "reason_message": _risk02.reason_message,
                    },
                )
                return {
                    "ok": False, "error_code": _risk02.reason_code,
                    "message": _risk02.reason_message,
                    "position_id": None, "merged": False, "new_balance": float(account.balance),
                    "routing_decision_id": None,
                    "required_margin": guard.get("required_margin", 0.0),
                    "required_margin_pct": guard.get("required_margin_pct", 0.0),
                    "projected_total_margin": guard.get("projected_total_margin", 0.0),
                    "projected_total_margin_pct": guard.get("projected_total_margin_pct", 0.0),
                    "max_total_margin_pct": guard.get("max_total_margin_pct", _DEFAULT_MAX_TOTAL_MARGIN_PCT),
                    "current_open_positions": guard.get("current_open_positions", fresh_open_count),
                    "max_open_positions": guard.get("max_open_positions", _rule.max_open_positions),
                }

            # 9. Passed — create/merge the Position exactly as before.
            if existing:
                # Merge into the existing row — weighted average price.
                new_qty = existing.qty + Decimal(str(qty))
                new_avg = (
                    existing.avg_price * existing.qty
                    + Decimal(str(price)) * Decimal(str(qty))
                ) / new_qty
                existing.avg_price = new_avg.quantize(Decimal("0.000001"))
                existing.qty = new_qty
                if sl is not None:
                    existing.sl = Decimal(str(sl))
                if tp is not None:
                    existing.tp = Decimal(str(tp))
                existing.save(update_fields=["qty", "avg_price", "sl", "tp"])
                position_id = existing.id
                merged = True
            else:
                pos = Position.objects.create(
                    account_id=self._db_account_id,
                    symbol=symbol,
                    side=side.upper(),
                    qty=Decimal(str(qty)),
                    avg_price=Decimal(str(price)),
                    sl=Decimal(str(sl)) if sl is not None else None,
                    tp=Decimal(str(tp)) if tp is not None else None,
                    pricing_context=pricing_context,
                )
                position_id = pos.id
                merged = False

            # BOOK-04b — Routing Engine Shadow Mode. Gated by
            # _should_activate_routing_decision() (BOOK-04f — master flag
            # settings.ROUTING_ENGINE_ENABLED plus optional granular
            # symbol/account_type allowlists, default: fully off — see
            # trx_simulator/settings.py). Runs strictly AFTER step 9
            # above, now that position_id/merged are known — never
            # before, since RoutingDecision.position needs a real
            # position_id and, for a brand-new Position, that id does
            # not exist until Position.objects.create() has already
            # returned (see docs/BOOK_04_IMPLEMENTATION_PLAN.md, BOOK-04b
            # Alcance, 4-step sequence). Still fully inside the same
            # transaction.atomic() opened at the top of this method —
            # this is deliberate: if anything after this point in the
            # transaction fails and rolls back, the RoutingDecision just
            # written rolls back with it, never left orphaned pointing
            # at a Position that never actually committed.
            #
            # The RoutingDecision write and the (for a brand-new Position
            # only) Position.routing_decision link are wrapped in their
            # OWN explicit nested transaction.atomic() savepoint, as a
            # single logical unit — not two independent steps. Corrected
            # after review found that treating them separately could
            # leave a RoutingDecision fully created and correctly
            # RoutingDecision.position-linked, yet never recognized as
            # the Position's principal (Position.routing_decision stays
            # NULL) if only the link step failed — an ambiguous state a
            # later netting merge could not resolve (no way to tell which
            # of possibly several RoutingDecision rows for that Position
            # was meant to be principal). With this savepoint: if the
            # link fails, the decision this call was about to present as
            # successful rolls back with it — nothing ambiguous survives,
            # and routing_decision_id (never assigned until after the
            # link succeeds) stays None. A merge increment has no link
            # step, so this savepoint is a no-op wrapper for that case —
            # its own RoutingDecision is unaffected and is never removed.
            #
            # routing_decision_id always ends up in the result dict (see
            # the return statements below) — None whenever the flag is
            # off, the contract fails to build, the writer fails, or (for
            # a new Position) the principal link fails; the real
            # decision_id only once the whole unit above succeeded. This
            # try/except protects contract construction, the writer call,
            # AND the optional link — the writer's own internal fail-open
            # (routing_engine.py) only covers its own body, not this call
            # site's surrounding code. A failure at ANY of these points
            # must never affect whether the position opened, the balance,
            # margin, commission, ledger, or audit trail written
            # below/above.
            routing_decision_id = None
            if self._should_activate_routing_decision(symbol, self.account.get("account_type", "CHALLENGE")):
                try:
                    from . import routing_engine as _routing_engine
                    _routing_contract = _routing_engine.build_shadow_mode_decision_contract(
                        symbol=symbol, side=side.upper(), qty=qty, merged=merged,
                    )
                    with transaction.atomic():
                        _routing_decision = _routing_engine.record_routing_decision(
                            account_id=self._db_account_id,
                            position_id=position_id, **_routing_contract,
                        )
                        if _routing_decision is not None:
                            if not merged:
                                pos.routing_decision = _routing_decision
                                pos.save(update_fields=["routing_decision"])
                            routing_decision_id = _routing_decision.decision_id
                except Exception as _routing_exc:
                    log.warning(
                        "[routing_engine] Shadow Mode integration failed pos=%s merged=%s: %s",
                        position_id, merged, _routing_exc,
                    )

            _commission_d = Decimal(str(commission)) if commission and commission > 0 else Decimal("0")

            # O.6c-1aa — EXPLICIT SPREAD FEE. Computed BEFORE any balance
            # mutation, from the exact same calculate_spread_revenue()
            # call the pre-O.6c-1aa code already used for BrokerLedger-
            # only booking — formula unchanged (O.6c-1z's approved
            # decision). _spread_fee_d is reused verbatim for BOTH the
            # trader's LedgerEntry(EV_FEE) debit below and BrokerLedger
            # (REV_SPREAD)'s amount — a single computed value feeding
            # both writes guarantees exact parity by construction.
            #
            # FIX-05A — _effective_pips now comes from THIS SAME order's
            # already-captured pricing_context (line ~1456, PricingDecision
            # per the FIX-05 design lock) instead of independently
            # recomputing base_pips+markup_pips here. That old
            # recomputation bypassed compute_effective_spread_pips()'s
            # min/max clamp and the dynamic-spread multiplier chain — both
            # of which pricing_context["effective_spread_pips"] already
            # applied (built via the SAME compute_effective_spread_pips()
            # broker_price() itself calls, see pricing_context.py's module
            # docstring) — so the fee and the displayed bid/ask could
            # silently diverge for the same tick. One decision, read
            # twice, never recomputed independently.
            #
            # Two distinct "missing" cases, handled differently on purpose
            # (audited before implementing — ~110 existing test call sites
            # across the suite call this method directly, bypassing
            # _order_new(), with pricing_context=None by design, and
            # legitimately expect a real fee):
            #   - pricing_context is None: the caller never attempted a
            #     capture at all (every direct/unit-test call site below
            #     _order_new(); _order_new() itself ALWAYS passes a real
            #     dict). Not the bug this block fixes — preserve the prior
            #     base+markup formula verbatim for these callers, zero
            #     regression.
            #   - pricing_context is a dict but "effective_spread_pips" is
            #     missing/None: build_pricing_context() legitimately
            #     leaves it None whenever base_spread_pips AND
            #     account_markup_pips are BOTH None (no BrokerSpreadConfig
            #     for this symbol at all — a routine, expected state, not
            #     a failure) — same zero-fee outcome the prior formula
            #     already produced for "no config". Only warn for the
            #     genuinely abnormal case: pricing_profile==
            #     PROFILE_CAPTURE_FAILED (_capture_pricing_context()'s own
            #     defensive except-branch; build_pricing_context() itself
            #     is documented to never raise, so this should not occur
            #     for a real _order_new() order that already required a
            #     valid quote to get this far). Never guess a fee in
            #     either sub-case: log-and-zero for the abnormal one,
            #     silently-zero for the routine one — exactly like the
            #     pre-existing "_effective_pips <= 0" no-fee path below
            #     already does for a legitimately-zero spread.
            if pricing_context is None:
                _spread_cfg     = _get_spread_config(symbol)
                _base_pips      = float(_spread_cfg.spread_pips) if (_spread_cfg is not None and _spread_cfg.enabled) else 0.0
                _markup_pips    = float(self.account.get("spread_pips", 0.0) or 0.0)
                _effective_pips = _base_pips + _markup_pips
            else:
                _effective_pips = pricing_context.get("effective_spread_pips")
                if _effective_pips is None:
                    if pricing_context.get("pricing_profile") == pricing_ctx.PROFILE_CAPTURE_FAILED:
                        log.warning(
                            "[spread_fee] pos=%s symbol=%s pricing_context capture failed — "
                            "charging zero spread fee, never recomputing independently",
                            position_id, symbol,
                        )
                    _effective_pips = 0.0
                _base_pips   = pricing_context.get("base_spread_pips") or 0.0
                _markup_pips = pricing_context.get("account_markup_pips") or 0.0
            _spread_rev     = calculate_spread_revenue(symbol, float(qty), _effective_pips) if _effective_pips > 0 else 0.0
            _spread_fee_d   = Decimal(str(_spread_rev)) if _spread_rev > 0 else Decimal("0")

            # Authoritative balance: deduct commission AND the explicit
            # spread fee from the already-locked account row, not stale
            # memory. O.6c-1aa requirement: "NO permitir posición creada
            # sin su fee correspondiente" — both trader-facing LedgerEntry
            # writes below are MANDATORY, not wrapped in their own try/
            # except or nested savepoint (unlike the BrokerLedger writes
            # further down) — a failure here propagates to this method's
            # outer transaction.atomic() exactly like Position.objects.
            # create() earlier in this same method already does: Position
            # + commission + spread fee + accounting roll back together.
            _auth_balance = account.balance - _commission_d - _spread_fee_d

            if _commission_d > 0:
                trader_ledger = LedgerEntry.objects.create(
                    account_id=self._db_account_id,
                    event_type=LedgerEntry.EV_COMMISSION,
                    amount=-_commission_d,
                    balance_after=_auth_balance,
                    meta={"symbol": symbol, "side": side, "db_pos_id": position_id},
                )
                # BrokerLedger REV_COMMISSION — best-effort (same
                # pre-existing isolation: a DB error here must never
                # block the trade, since the trader's own charge above
                # already committed to this transaction's write-set).
                try:
                    BrokerLedger.objects.create(
                        revenue_type=BrokerLedger.REV_COMMISSION,
                        amount=_commission_d,
                        source_account_id=self._db_account_id,
                        source_ledger=trader_ledger,
                        symbol=symbol,
                        meta={"side": side, "db_pos_id": position_id},
                    )
                except Exception as _bl_exc:
                    log.warning("[broker_ledger] commission insert failed pos=%s: %s", position_id, _bl_exc)

            if _spread_fee_d > 0:
                # O.6c-1aa — mandatory trader-facing charge, same
                # EV_FEE choice LedgerEntry.EVENT_CHOICES already defines
                # (reused — no migration needed). meta.fee_type="spread"
                # distinguishes this from any other future EV_FEE use.
                spread_fee_ledger = LedgerEntry.objects.create(
                    account_id=self._db_account_id,
                    event_type=LedgerEntry.EV_FEE,
                    amount=-_spread_fee_d,
                    balance_after=_auth_balance,
                    meta={
                        "fee_type": "spread", "symbol": symbol, "side": side,
                        "db_pos_id": position_id, "effective_pips": _effective_pips,
                        "base_pips": _base_pips, "account_markup_pips": _markup_pips,
                    },
                )
                # BrokerLedger REV_SPREAD — best-effort, SAME nested-
                # savepoint isolation the pre-O.6c-1aa code already used
                # for this exact write (a DB error here must never
                # corrupt the outer transaction) — but now linked via
                # source_ledger to spread_fee_ledger, and using the SAME
                # _spread_fee_d the trader was just charged — O.6c-1aa's
                # explicit "trader spread fee debit == broker REV_SPREAD"
                # requirement, guaranteed by construction, not by a
                # second computation.
                try:
                    with transaction.atomic():
                        BrokerLedger.objects.create(
                            revenue_type=BrokerLedger.REV_SPREAD,
                            amount=_spread_fee_d,
                            source_account_id=self._db_account_id,
                            source_ledger=spread_fee_ledger,
                            symbol=symbol,
                            meta={
                                "side": side,
                                "db_pos_id": position_id,
                                "spread_pips": _effective_pips,
                                "base_pips": _base_pips,
                                "account_markup_pips": _markup_pips,
                            },
                        )
                        log.debug("[broker_ledger] spread pos=%s symbol=%s effective_pips=%.4f "
                                  "(base=%.4f markup=%.4f) rev=%.6f",
                                  position_id, symbol, _effective_pips, _base_pips, _markup_pips, _spread_rev)
                except Exception as _sp_exc:
                    log.warning("[broker_ledger] spread insert failed pos=%s: %s", position_id, _sp_exc)

            if _commission_d > 0 or _spread_fee_d > 0:
                account.balance = _auth_balance
                account.save(update_fields=["balance"])

            # O.6c-1o — MULTIPANEL-01 fix. Registered as the LAST statement
            # inside this transaction.atomic() block so a rollback caused
            # by ANYTHING above (Position write, commission ledger,
            # routing engine, balance update) correctly discards this
            # callback — Django's own on_commit guarantee, not
            # reimplemented here (see test_rollback_no_publish_on_open).
            # Covers writers #1 (new open) and #2 (netting merge/update)
            # from the O.6c-1n writer map in one place, since both share
            # this exact code path (see "existing"/"merged" branch above).
            from . import ws_events
            transaction.on_commit(lambda: ws_events.publish_position_changed(
                self._db_account_id,
                action=(ws_events.ACTION_UPDATE if merged else ws_events.ACTION_OPEN),
                position_id=position_id, symbol=symbol, side=side, qty=float(qty),
                new_balance=float(_auth_balance),
            ))
            # O.6c-1v — OPEN POSITION FEED COVERAGE, writers #1/#2 (new
            # open / netting merge) from the same writer map O.6c-1o used.
            # Registered as its own on_commit (not folded into the
            # ws_events lambda above) so a failure in one callback can
            # never prevent the other from running. mark_position_symbol()
            # is sync and thread-safe — safe to call from the on_commit
            # callback, which Django runs synchronously right here in this
            # same database_sync_to_async thread.
            transaction.on_commit(lambda: self._feed.mark_position_symbol(symbol))

        log.info("[db_open] pos_id=%s symbol=%s side=%s qty=%s merged=%s balance=%.4f",
                 position_id, symbol, side, qty, merged, float(_auth_balance))

        # AUDIT-01 — position opened (or merged into an existing one).
        # Recorded after the transaction above has committed, using the
        # already-authoritative position_id/balance — never inside the
        # lock, and never able to affect whether the open itself succeeds.
        from . import broker_audit as _audit
        _audit.record_trade_event(
            event_type=_audit.EV_POSITION_OPENED,
            description=f"Position {'merged' if merged else 'opened'} on {symbol} {side.upper()} qty={qty}",
            account_id=self._db_account_id, symbol=symbol,
            source_module="simulator.consumers",
            metadata={
                "position_id": position_id, "side": side, "qty": float(qty),
                "price": float(price), "merged": merged,
            },
        )

        return {
            "position_id": position_id, "merged": merged, "new_balance": float(_auth_balance),
            "routing_decision_id": routing_decision_id,
            **guard,
        }

    @database_sync_to_async
    def _db_close_position_atomic(self, pos_mem: dict, close_px: float, reason: str,
                                   realized_pnl: float, new_balance: float,
                                   new_equity: float,
                                   pricing_context_close: dict | None = None,
                                   close_qty=None) -> dict:
        """DB-first order close (Phase 1B; ORDER-MANAGEMENT-V2B — partial close).

        Atomically: find+lock Position, create Trade, record LedgerEntry EV_REALIZED,
        delete-or-reduce Position, update TradingAccount balance/equity, run
        risk+intelligence engines. All committed before any memory mutation
        in the caller.

        close_qty (ORDER-MANAGEMENT-V2B, design lock §2/§4):
          - None (default) — close_qty becomes the FRESH, lock-read pos.qty,
            i.e. exactly today's full-close behavior for every pre-existing
            caller (_check_tp_sl, _do_stopout, _do_retail_liquidation, and
            _order_close when the client sends no qty). These callers never
            pass this parameter — is_full is trivially always True for them
            (a live Position row can never hold qty<=0 — see the invariant
            note at the qty==0 branch below), so the new invalid_qty/
            qty_exceeds_position rejections below are UNREACHABLE for them
            by construction. Zero behavior change for any existing caller.
          - a Decimal/numeric — validated against the FRESH pos.qty (read
            only after the lock below, never pos_mem's pre-lock copy):
            <=0 or >fresh_qty is rejected explicitly (ok=False), NEVER
            silently coerced into a full close (design lock §2's explicit
            rule). ==fresh_qty reuses the exact full-close branch below
            (Position.delete()); the strict remainder becomes a genuine
            partial close (Position.qty -= close_qty, avg_price/sl/tp
            left untouched — design lock §3/§10).

        realized_pnl (parameter) is used ONLY for the demo/anonymous
        branch and the already_closed early-return below (no DB Position
        to price against in either case). Once a real Position is locked,
        realized_pnl is ALWAYS recomputed authoritatively here, from the
        fresh pos.avg_price and the validated close_qty — never trusted
        from the caller's pre-lock estimate (design lock §3: the old
        "trust the caller" pattern was safe only because a close was
        previously binary — exists/doesn't — and partial close breaks
        that; this is the one deliberate, disclosed behavior change from
        the pre-V2B version of this method, applying uniformly to full
        and partial alike rather than maintaining two divergent formulas).

        Returns a dict with "ok" (True unless invalid_qty/qty_exceeds_
        position), "partial" (bool), "remaining_qty" (float|None, None
        for a full close), plus the pre-existing field set. Raises only on
        genuine DB error — caller leaves memory untouched in that case.

        pricing_context_close (SPREAD-02): the Trade's pricing_context_open is
        copied verbatim from the locked Position row's pricing_context — never
        recomputed here, so a BrokerSpreadConfig change between open and close
        cannot retroactively alter what was captured at open.
        """
        if not self._db_account_id:
            # Demo/anonymous session — no DB Position to validate close_qty
            # against; V2B partial close is DB-backed only (design lock
            # never addresses demo sessions — preserving exact pre-V2B
            # full-close-only behavior here is the conservative default).
            return {
                "ok":          True,
                "new_balance": new_balance,
                "new_equity":  new_equity,
                "new_status":  self.account.get("status", "Activo"),
                "new_peak":    self.account.get("peak_balance", new_balance),
                "violations":  [],
                "trade_id":    None,
                "partial":     False,
                "remaining_qty": None,
            }
        from decimal import Decimal
        with transaction.atomic():
            # 1. Lock the TradingAccount row FIRST — global lock order
            # TradingAccount → Position (see the module-level LOCK ORDER
            # note above this class). Account is this account's real
            # mutex, locked here regardless of how many positions exist,
            # so that step 2 below is guaranteed to observe every write
            # any sibling WS/daemon transaction for this SAME account has
            # already committed — see _db_open_position_atomic's step-1
            # docstring for the full staleness-race rationale (identical
            # here: locking Position first would let two closers each
            # read a pre-lock "not yet closed" snapshot before either held
            # any lock at all).
            _acct_row = (
                TradingAccount.objects
                .select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )

            # 2. NOW find and lock the target Position row — issued
            # strictly AFTER the Account lock, so "already closed" reflects
            # the true, currently-committed state: any sibling close that
            # already ran on this position has either committed by now
            # (pos is None below, correctly detected) or is blocked
            # waiting for step 1's lock (hasn't touched this position yet).
            pos = (
                Position.objects
                .select_for_update()
                .filter(id=pos_mem["id"], account_id=self._db_account_id)
                .first()
            )
            if pos is None:
                log.info("[db_close] pos %r already closed by concurrent close — skipping", pos_mem["id"])
                return {
                    "ok":             True,
                    "new_balance":    new_balance,
                    "new_equity":     new_equity,
                    "new_status":     self.account.get("status", "Activo"),
                    "new_peak":       self.account.get("peak_balance", new_balance),
                    "violations":     [],
                    "trade_id":       None,
                    "already_closed": True,
                    "partial":        False,
                    "remaining_qty":  None,
                }

            # ORDER-MANAGEMENT-V2B — design lock §2/§4. fresh_qty is read
            # from the just-locked `pos` row, never from pos_mem (the
            # caller's pre-lock in-memory copy) — this is what makes the
            # validation below race-safe against a concurrent partial
            # close on the SAME position (see design lock §6's worked
            # example: A closes 0.70 first, B's 0.50 request is then
            # validated against the FRESH 0.30 remainder, not the stale
            # 1.00 B originally saw).
            fresh_qty = pos.qty
            if close_qty is None:
                close_qty_d = fresh_qty
            else:
                close_qty_d = close_qty if isinstance(close_qty, Decimal) else Decimal(str(close_qty))

            if close_qty_d <= 0:
                return {"ok": False, "code": "invalid_qty",
                        "message": "La cantidad a cerrar debe ser mayor que cero."}
            if close_qty_d > fresh_qty:
                # NEVER silently coerced into a full close (design lock §2).
                return {"ok": False, "code": "qty_exceeds_position",
                        "message": "La cantidad a cerrar excede el tamaño actual de la posición."}

            is_full = (close_qty_d == fresh_qty)

            # ORDER-MANAGEMENT-V2B — FINANCIAL RACE BLOCKER FIX. realized_pnl
            # is recomputed AUTORITATIVAMENTE, under lock, UNCONDITIONALLY —
            # for FULL and PARTIAL alike. This is the ORIGINAL design lock
            # decision; a prior revision of this method special-cased
            # is_full to trust the caller's pre-lock realized_pnl parameter
            # verbatim, reasoning that "a full close is still binary
            # post-V2B". That reasoning was WRONG and has been retracted:
            # is_full only means close_qty_d == fresh_qty — the FRESH qty,
            # which a concurrent PARTIAL close on the same Position can
            # have already shrunk since the caller computed its pre-lock
            # estimate. A stale full-close caller (its own in-memory
            # mirror still showing the pre-partial qty) would then have
            # its stale, TOO-LARGE realized_pnl trusted verbatim while
            # Trade.lot_size/entry_price were ALSO taken from that same
            # stale pos_mem — double-realizing the portion the concurrent
            # partial close had already correctly realized (reproduced
            # empirically: BUY 1.00, partial 0.70 commits first, a stale
            # full-close request created a SECOND Trade for the full
            # original 1.00 instead of the real 0.30 remainder — 560.00
            # of fabricated balance). See this block's own audit report
            # for the full numeric reproduction (BUY manual + SELL daemon).
            #
            # Reuses EXACTLY pnl_engine.calculate_position_pnl() — never a
            # second formula — with close_qty_d (fresh, already validated
            # against fresh_qty above) and pos.avg_price (fresh, under
            # lock), never pos_mem's pre-lock copies, in EITHER branch.
            _authoritative_pnl_result = pnl_engine.calculate_position_pnl(
                pos_mem["side"], pos.avg_price, close_px, close_qty_d, pos_mem["symbol"],
                account_currency=self.account.get("currency", "USD"),
            )
            if _authoritative_pnl_result.pnl_account is None:
                # Fail-closed — same contract pnl_engine already requires
                # of every other real caller. Unreachable today (every
                # enabled symbol/account_currency combination converts) —
                # same "impossible but never fabricated" precedent as
                # _margin_used_total's own unreachable branch.
                log.critical(
                    "[db_close] event=pnl_conversion_unsupported symbol=%s account_currency=%s "
                    "error_code=%s — refusing to close, NOT a fabricated PnL.",
                    pos_mem["symbol"], self.account.get("currency", "USD"),
                    _authoritative_pnl_result.error_code,
                )
                return {"ok": False, "code": _authoritative_pnl_result.error_code or "pnl_conversion_unsupported",
                        "message": "No se pudo calcular el PnL de forma segura."}
            realized_pnl = float(_authoritative_pnl_result.pnl_account)

            # ACCOUNT-02 — derive balance_after from the FRESH, locked
            # _acct_row read + realized_pnl — never from new_balance/
            # self.account, which the CALLER computed before this lock and
            # which may already be stale (a sibling WebSocket for the same
            # account can have opened/closed something this connection
            # never learned about). realized_pnl itself is never stale —
            # it's derived purely from the closing position's own
            # entry/exit/qty via pnl_engine, independent of account state.
            #
            # remaining_floating (the floating PnL of OTHER still-open
            # positions in this same batch, e.g. mid-stopout) is NOT
            # derivable from a fresh DB read — but new_equity - new_balance
            # cancels out whatever stale starting balance the caller used,
            # leaving exactly that pure, staleness-independent quantity.
            _ZERO = Decimal("0")
            _nb = new_balance if isinstance(new_balance, Decimal) else Decimal(str(new_balance))
            _ne = new_equity  if isinstance(new_equity,  Decimal) else Decimal(str(new_equity))
            _remaining_floating = _ne - _nb

            _fresh_balance_after = (
                _acct_row.balance + Decimal(str(realized_pnl)) if _acct_row is not None else _nb
            )

            trade_type = str(pos_mem.get("side", "")).upper()
            if trade_type not in ("BUY", "SELL"):
                trade_type = "BUY"

            # Guard: prevent writing a negative balance to DB (extreme loss / gap risk).
            _safe_balance = max(_fresh_balance_after, _ZERO)
            _safe_equity  = max(_safe_balance + _remaining_floating, _ZERO)
            _shortfall    = abs(min(_fresh_balance_after, _ZERO))
            if _shortfall > _ZERO:
                log.critical(
                    "[db_close] NEGATIVE BALANCE PREVENTED: account=%s realized=%.4f "
                    "computed_balance=%s shortfall=%s — clamping to 0",
                    self._db_account_id, realized_pnl, _fresh_balance_after, _shortfall,
                )

            # ORDER-MANAGEMENT-V2B — pnl_conversion reuses the SAME
            # authoritative result computed above (_authoritative_pnl_
            # result) UNCONDITIONALLY — full and partial alike — never a
            # second, independently-timed recompute, never pos_mem's
            # pre-lock avg/qty.
            _closed_at = timezone.now()
            _pnl_conversion = _authoritative_pnl_result.to_dict()
            _pnl_conversion["conversion_timestamp"] = _closed_at.timestamp()

            trade = Trade.objects.create(
                account_id=self._db_account_id,
                symbol=pos_mem["symbol"],
                trade_type=trade_type,
                # ORDER-MANAGEMENT-V2B — close_qty_d/pos.avg_price (fresh,
                # under lock) UNCONDITIONALLY, full and partial alike —
                # never pos_mem's pre-lock copies (see this block's own
                # docstring: that was the exact source of the double-
                # realization race this fix retracts).
                lot_size=close_qty_d,
                entry_price=pos.avg_price,
                exit_price=Decimal(str(close_px)),
                stop_loss=Decimal(str(pos_mem["sl"])) if pos_mem.get("sl") is not None else None,
                take_profit=Decimal(str(pos_mem["tp"])) if pos_mem.get("tp") is not None else None,
                profit_loss=Decimal(str(realized_pnl)),
                opened_at=datetime.fromtimestamp(int(pos_mem.get("opened_at", time.time())), tz=dt_timezone.utc),
                closed_at=_closed_at,
                pricing_context_open=pos.pricing_context,
                pricing_context_close=pricing_context_close,
                pnl_conversion=_pnl_conversion,
                # BOOK-04c — verbatim copy of the PRINCIPAL decision,
                # read from the still-locked `pos` before pos.delete()
                # below. NULL propagates honestly (flag was off at open,
                # or a pre-BOOK-04b Position) — never fabricated.
                routing_decision=pos.routing_decision,
            )

            # 3. Record LedgerEntry EV_REALIZED (balance_after = post-close balance).
            LedgerEntry.objects.create(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                amount=Decimal(str(realized_pnl)),
                balance_after=_safe_balance,
                meta={"symbol": pos_mem["symbol"], "side": pos_mem["side"],
                      "reason": reason, "trade_id": trade.id},
            )

            # BOOK-02 — broker's B-Book counterparty result for this same
            # Trade, same transaction. See simulator/broker_ledger.py.
            if _acct_row is not None:
                create_broker_counterparty_entry(trade, _acct_row, realized_pnl, reason)

            # BOOK-05d.3a — Liquidity Ledger. Purely observational,
            # simulated — never affects Trade, BrokerLedger, the negative
            # balance guard below, Position deletion, or balance/equity
            # updates. Only runs if this Trade's principal RoutingDecision
            # (already copied verbatim onto trade.routing_decision at
            # creation, above) has a LiquidityDecision associated with
            # it — if the Liquidity Engine (or the Routing Engine itself)
            # was off at open time, there is nothing to settle, and
            # neither the lookup nor the writer runs at all.
            #
            # Two independent nested savepoints, not one:
            #   - the one below protects the LiquidityDecision lookup —
            #     a DatabaseError here must never leave this method's own
            #     outer transaction.atomic() (opened at the top of this
            #     function) marked as needing a rollback;
            #   - record_liquidity_ledger_entry()'s own internal
            #     transaction.atomic() (BOOK-05d.2) separately protects
            #     just its .create() call.
            # The except below is deliberately OUTSIDE this savepoint —
            # catching inside it would risk leaving the savepoint itself
            # in a broken state; catching outside it, after the `with`
            # has already unwound, is what guarantees this method's own
            # outer transaction remains fully usable for pos.delete() and
            # the balance/equity update that follow, regardless of what
            # happened here. The writer's return value is never checked —
            # a fail-open write's only observable outcome is "row exists"
            # or "row doesn't exist", never a value this call site acts on.
            # BOOK-05e.3a — the two ids below (liquidity_decision's own
            # decision_id and RoutingDecision's own decision_id) are
            # resolved HERE, still inside this same pre-existing nested
            # savepoint, precisely so the extra RoutingDecision lookup
            # this block adds is covered by it too — a raw ORM query's
            # DatabaseError corrupts the surrounding transaction's DB-
            # level state even if a bare try/except catches it; only a
            # savepoint recovers cleanly. Resolving it here reuses the
            # savepoint BOOK-05d.3a already opened for the LiquidityDecision
            # lookup, rather than requiring a second, new one — "no atomic()
            # adicional" is satisfied by scope, not by skipping protection.
            _liquidity_ledger_entry = None
            _liquidity_decision_uuid = None
            _routing_decision_uuid = None
            if trade.routing_decision_id is not None:
                try:
                    with transaction.atomic():
                        from .models import LiquidityDecision, RoutingDecision
                        liquidity_decision = (
                            LiquidityDecision.objects
                            .filter(routing_decision_id=trade.routing_decision_id)
                            .order_by("-decided_at")
                            .first()
                        )

                        if liquidity_decision is not None:
                            from .liquidity_ledger import record_liquidity_ledger_entry
                            _liquidity_ledger_entry = record_liquidity_ledger_entry(
                                source_trade_id=trade.id,
                                liquidity_decision_id=liquidity_decision.id,
                                symbol=trade.symbol,
                                simulated_pnl=(
                                    Decimal("0.00") if trade.profit_loss == 0
                                    else -trade.profit_loss
                                ),
                                meta={
                                    "trader_pnl": float(trade.profit_loss),
                                    "close_reason": reason,
                                },
                            )
                            if _liquidity_ledger_entry is not None:
                                _liquidity_decision_uuid = liquidity_decision.decision_id
                                _routing_decision_uuid = (
                                    RoutingDecision.objects
                                    .filter(pk=trade.routing_decision_id)
                                    .values_list("decision_id", flat=True)
                                    .first()
                                )
                except Exception as _liquidity_ledger_exc:
                    log.warning(
                        "[liquidity_ledger] entry failed trade=%s: %s",
                        trade.id, _liquidity_ledger_exc, exc_info=True,
                    )

            # BOOK-05e.3a — Liquidity Ledger audit trail. A second,
            # independent try/except from the write above (same
            # rationale BOOK-05e.2 already established: catching an
            # audit-event failure inside the writer's own except would
            # misattribute it as a "liquidity_ledger entry failed"
            # instead of what it really is). Deliberately placed AFTER
            # the nested `with transaction.atomic()` above has already
            # closed — never inside it — so that a failure constructing
            # or sending this event can never roll back the
            # LiquidityLedger row that was already committed a moment
            # earlier, relative to its own savepoint. Still runs inside
            # this method's own OUTER transaction.atomic() (opened at
            # the top of this function) — no additional atomic() needed
            # here: record_liquidity_event() (-> record_event()) already
            # opens and fully contains its own internal savepoint and
            # never raises: the only residual risk here is a plain
            # Python bug in this call site's own argument construction,
            # which a bare except already fully contains (no SQL
            # statement is at risk of failing at this point). Only runs
            # when the writer above actually produced a row — covers,
            # without needing to distinguish them, every reason it could
            # have produced nothing. Return value ignored, same as every
            # other record_*_event() call site in this file.
            if _liquidity_ledger_entry is not None:
                try:
                    from . import broker_audit as _audit
                    _audit.record_liquidity_event(
                        event_type=_audit.EV_LIQUIDITY_LEDGER_RECORDED,
                        description=(
                            f"Liquidity ledger entry recorded for {trade.symbol} "
                            f"(trade_id={trade.id})"
                        ),
                        account_id=self._db_account_id,
                        trade_id=trade.id,
                        symbol=trade.symbol,
                        source_module="simulator.consumers",
                        metadata={
                            "liquidity_ledger_id": _liquidity_ledger_entry.id,
                            "liquidity_decision_id": str(_liquidity_decision_uuid),
                            "routing_decision_id": (
                                str(_routing_decision_uuid) if _routing_decision_uuid else None
                            ),
                            "position_id": pos.id,
                            "close_reason": reason,
                        },
                    )
                except Exception as _liquidity_ledger_audit_exc:
                    log.warning(
                        "[liquidity_ledger] audit event failed trade=%s: %s",
                        trade.id, _liquidity_ledger_audit_exc, exc_info=True,
                    )

            if _shortfall > _ZERO:
                LedgerEntry.objects.create(
                    account_id    = self._db_account_id,
                    event_type    = LedgerEntry.EV_ADJUST,
                    amount        = _ZERO,
                    balance_after = _safe_balance,
                    meta          = {
                        "reason":                    "negative_balance_guard",
                        "shortfall":                 float(_shortfall),           # float for JSON
                        "original_computed_balance": float(_fresh_balance_after),   # float for JSON
                        "realized_pnl":              realized_pnl,
                    },
                )

            # 4. ORDER-MANAGEMENT-V2B, design lock §4.F — full vs partial.
            # is_full was decided above (close_qty_d == fresh_qty), from
            # values already validated under this same lock — never
            # re-derived here. A full close reuses the exact pre-V2B
            # branch (pos.delete()) unchanged; a partial close reduces
            # qty in place and leaves avg_price/sl/tp untouched (design
            # lock §3/§10) — never persists qty<=0 (is_full is exactly
            # True whenever close_qty_d would bring it to zero).
            remaining_qty = None
            if is_full:
                pos.delete()
            else:
                pos.qty = fresh_qty - close_qty_d
                pos.save(update_fields=["qty"])
                remaining_qty = float(pos.qty)

            # 5. Update TradingAccount balance + equity — _acct_row was already
            # locked above (before Trade/LedgerEntry creation), so this reuses
            # that same locked row instead of a second fetch.
            account = _acct_row
            if account:
                account.balance = _safe_balance
                account.equity  = _safe_equity
                account.save(update_fields=["balance", "equity"])

                # 6. Risk engine — challenge/funded compliance checks.
                from .risk_engine import check_and_enforce_risk
                violations = check_and_enforce_risk(account)
                if violations:
                    log.warning("[db_close] risk violations account #%s: %s",
                                self._db_account_id, [v.violation_type for v in violations])

                # 7. Intelligence engine — behavioral classification.
                from .intelligence_engine import update_intelligence
                update_intelligence(account)

                final_status = account.status
                final_peak   = float(account.peak_balance)
            else:
                violations   = []
                final_status = self.account.get("status", "Activo")
                final_peak   = self.account.get("peak_balance", float(_nb))

            # O.6c-1o — MULTIPANEL-01 fix. Registered as the LAST statement
            # inside this transaction.atomic() block (after Trade/Ledger/
            # BrokerLedger/LiquidityLedger writes, Position delete, balance/
            # equity update, and the risk/intelligence engine calls) so a
            # rollback caused by ANY of those correctly discards this
            # callback — Django's own on_commit guarantee, not
            # reimplemented here (see test_rollback_no_publish_on_close).
            # Covers writers #3-7 from the O.6c-1n writer map in one place:
            # manual close (_order_close), Close All (N× _order_close),
            # SL/TP (_check_tp_sl), stopout (_do_stopout), retail
            # liquidation (_do_retail_liquidation) — all four already
            # converge on this single function, so one call site here is
            # correct and avoids duplicate events per O.6c-1o's own
            # instruction to "emitir una sola vez en el punto común".
            # ORDER-MANAGEMENT-V2B, design lock §7/§8 — action distinguishes
            # the POSITION STATE (ACTION_CLOSE: gone; ACTION_UPDATE: still
            # alive with a reduced qty — reusing the SAME action netting
            # merges already use, no new action invented). qty in this
            # event means "closed in THIS event" (close_qty_d) — for a
            # full close this equals the old pos_mem["qty"] value exactly
            # (no behavior change there); trade_id is what position_changed()
            # now gates the TRADE CLOSED (order_close/History) message on,
            # never the action — see that handler's own updated docstring.
            from . import ws_events
            transaction.on_commit(lambda: ws_events.publish_position_changed(
                self._db_account_id,
                action=(ws_events.ACTION_UPDATE if not is_full else ws_events.ACTION_CLOSE),
                position_id=pos_mem["id"], symbol=pos_mem["symbol"],
                side=pos_mem["side"], qty=float(close_qty_d), avg=pos_mem["avg"],
                close_px=close_px, realized_pnl=realized_pnl, reason=reason,
                trade_id=trade.id, new_balance=float(_safe_balance),
                new_status=final_status, ts=int(time.time()),
                partial=not is_full, remaining_qty=remaining_qty,
            ))
            # O.6c-1v — OPEN POSITION FEED COVERAGE, writers #3-7 (manual
            # close, Close All, SL, TP, stopout, retail liquidation — all
            # four call sites of this function). sync_position_symbol_from_db
            # re-derives from DB rather than decrementing, so a second open
            # position on the same symbol correctly keeps the feed alive
            # (test scenario #5). Own on_commit, same isolation rationale
            # as _db_open_position_atomic's mark_position_symbol above.
            transaction.on_commit(lambda: self._feed.sync_position_symbol_from_db(pos_mem["symbol"]))

        log.info("[db_close] OK pos_id=%r trade_id=%r realized=%.4f balance=%.2f status=%s partial=%s remaining=%s",
                 pos_mem["id"], trade.id, realized_pnl, float(_safe_balance), final_status,
                 not is_full, remaining_qty)
        return {
            "ok":            True,
            "new_balance":   float(_safe_balance),
            "new_equity":    float(_safe_equity),
            "new_status":    final_status,
            "new_peak":      final_peak,
            "violations":    [v.violation_type for v in violations],
            "trade_id":      trade.id,
            "realized_pnl":  realized_pnl,
            "close_qty":     float(close_qty_d),
            "partial":       not is_full,
            "remaining_qty": remaining_qty,
        }

    @database_sync_to_async
    def _db_mirror_update_sl_tp(self, pos_id, symbol, sl, tp):
        if not self._db_account_id or not pos_id: return
        try:
            pos = Position.objects.get(id=pos_id, account_id=self._db_account_id)
        except Position.DoesNotExist:
            log.warning("[db_update_sl_tp] no DB Position for id=%r sym=%r — SL/TP DB update skipped", pos_id, symbol)
            return
        changed=False
        from decimal import Decimal
        if sl is not None: pos.sl = Decimal(sl); changed=True
        if tp is not None: pos.tp = Decimal(tp); changed=True
        if changed: pos.save()

    @database_sync_to_async
    def _db_mirror_close_position(self, pos_mem, close_px, reason, realized_pnl):
        # Deprecated for manual close — superseded by _db_close_position_atomic (Phase 1B).
        # Still called by _check_tp_sl, _do_stopout, _do_retail_liquidation (best-effort paths).
        if not self._db_account_id:
            log.warning("[db_close] SKIPPED — db_account_id is None")
            return
        from decimal import Decimal
        log.info("[db_close] starting for pos id=%r sym=%r side=%r close_px=%s realized=%.4f reason=%s",
                 pos_mem.get("id"), pos_mem.get("symbol"), pos_mem.get("side"),
                 close_px, realized_pnl, reason)
        with transaction.atomic():
            # look up the DB Position by in-memory id first, then by symbol+side
            pos = Position.objects.filter(id=pos_mem["id"], account_id=self._db_account_id).first()
            if pos:
                log.info("[db_close] found Position by id=%r", pos_mem["id"])
            else:
                log.warning("[db_close] no DB Position for id=%r — Trade will still be created", pos_mem["id"])

            # Normalise side to uppercase for trade_type field
            raw_side = str(pos_mem.get("side", "")).upper()
            trade_type = raw_side if raw_side in ("BUY", "SELL") else ("BUY" if raw_side == "BUY" else "SELL")

            log.info("[db_close] creating Trade: sym=%s type=%s qty=%s entry=%s exit=%s pnl=%s",
                     pos_mem["symbol"], trade_type, pos_mem["qty"], pos_mem["avg"], close_px, realized_pnl)
            trade = Trade.objects.create(
                account_id=self._db_account_id,
                symbol=pos_mem["symbol"],
                trade_type=trade_type,
                lot_size=Decimal(str(pos_mem["qty"])),
                entry_price=Decimal(str(pos_mem["avg"])),
                exit_price=Decimal(str(close_px)),
                stop_loss=Decimal(str(pos_mem["sl"])) if pos_mem.get("sl") is not None else None,
                take_profit=Decimal(str(pos_mem["tp"])) if pos_mem.get("tp") is not None else None,
                profit_loss=Decimal(str(realized_pnl)),
                opened_at=datetime.fromtimestamp(int(pos_mem.get("opened_at", time.time())), tz=dt_timezone.utc),
                closed_at=timezone.now(),
            )
            log.info("[db_close] Trade created id=%r", trade.id)

            ledger = LedgerEntry.objects.create(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                amount=Decimal(str(realized_pnl)),
                balance_after=Decimal(str(self.account["balance"])),
                meta={"symbol": pos_mem["symbol"], "side": pos_mem["side"], "reason": reason},
            )
            log.info("[db_close] LedgerEntry created id=%r", ledger.id)

            if pos:
                pos.delete()
                log.info("[db_close] Position deleted")

            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account:
                account.balance = Decimal(str(self.account["balance"]))
                account.equity  = Decimal(str(self.account["equity"]))
                account.save(update_fields=["balance", "equity"])
                log.info("[db_close] TradingAccount balance synced to %.2f", self.account["balance"])

                # Risk engine — compliance violations + drawdown
                from .risk_engine import check_and_enforce_risk
                violations = check_and_enforce_risk(account)
                if violations:
                    log.warning(
                        "[risk] account #%s suspended: %s",
                        self._db_account_id,
                        [v.violation_type for v in violations],
                    )
                # Sync DB state back to memory (status + peak_balance updated by risk engine)
                self.account["status"]       = account.status
                self.account["peak_balance"] = float(account.peak_balance)

                # Intelligence engine — behavioral analysis + classification + routing
                from .intelligence_engine import update_intelligence
                update_intelligence(account)

    # ---------------- Observability ----------------
    async def _ws_counter(self, delta: int) -> None:
        """Increment (+1) or decrement (-1) the active WS connections counter in Redis."""
        try:
            from django.conf import settings as _s
            import asyncio
            from .observability import ws_incr, ws_decr
            url = getattr(_s, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
            loop = asyncio.get_event_loop()
            if delta > 0:
                await loop.run_in_executor(None, ws_incr, url)
            else:
                await loop.run_in_executor(None, ws_decr, url)
        except Exception:
            pass  # counter failure must never break WS

    # ---------------- Util: enviar JSON ----------------
    async def send_json(self, payload: dict):
        await self.send(text_data=json.dumps(payload))