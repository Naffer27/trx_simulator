# simulator/consumers.py
import os, json, asyncio, random, time, logging, math
from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec, allowed_symbols, kline_symbols
from .models import TradingAccount, Position, Trade, LedgerEntry, BrokerLedger
from .spread_engine import broker_price, calculate_spread_revenue, _get_config as _get_spread_config
from .observability import security_log
from . import pricing_context as pricing_ctx
from . import dynamic_spread
from . import pnl_engine

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
      1. allowed_symbols_snapshot  — symbol whitelist
      2. max_lot_size_snapshot     — product-level hard lot cap
      3. per-trade margin %        — required_margin / equity ≤ 10 %
      4. total margin after open % — (used + required) / equity ≤ 50 %
      5. margin_level projection   — equity / (used + required) ≥ margin_call_level_snapshot
    """
    account_lev = max(1, int(account_snap.get("leverage", 50)))
    effective_lev = max(1, min(account_lev, spec_max_leverage))
    required_margin = abs(entry_px * qty * spec_contract_size) / effective_lev
    equity_safe = max(float(equity), 0.01)
    per_trade_pct = required_margin / equity_safe * 100.0
    total_margin_after = float(margin_used_now) + required_margin
    total_margin_pct = total_margin_after / equity_safe * 100.0

    details = {
        "required_margin": round(required_margin, 4),
        "required_margin_pct": round(per_trade_pct, 2),
        "projected_total_margin": round(total_margin_after, 4),
        "projected_total_margin_pct": round(total_margin_pct, 2),
        "max_total_margin_pct": _DEFAULT_MAX_TOTAL_MARGIN_PCT,
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
    if per_trade_pct > _DEFAULT_MAX_MARGIN_PER_TRADE_PCT:
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
            _DEFAULT_MAX_MARGIN_PER_TRADE_PCT, _DEFAULT_MAX_TOTAL_MARGIN_PCT,
            account_lev, spec_max_leverage, effective_lev,
        )
        return (
            False,
            "margin_per_trade_exceeded",
            (
                f"Orden rechazada: margen insuficiente. Esta operación requeriría "
                f"{per_trade_pct:.1f}% de tu equity como margen "
                f"(límite: {_DEFAULT_MAX_MARGIN_PER_TRADE_PCT:.0f}%). "
                "Prueba con un lote menor."
            ),
            details,
        )

    # 4 — Total margin cap after this trade
    if total_margin_pct > _DEFAULT_MAX_TOTAL_MARGIN_PCT:
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
            _DEFAULT_MAX_TOTAL_MARGIN_PCT, account_lev, spec_max_leverage, effective_lev,
        )
        return (
            False,
            "total_margin_exceeded",
            (
                f"Orden rechazada: esta operación excedería el uso máximo de margen "
                f"permitido ({_DEFAULT_MAX_TOTAL_MARGIN_PCT:.0f}%). "
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
        "max_total_margin_pct": _DEFAULT_MAX_TOTAL_MARGIN_PCT,
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
    )
    return {
        "ok": guard_ok,
        "error_code": None if guard_ok else guard_code,
        "message": "ok" if guard_ok else guard_msg,
        **guard_details,
        **base,
    }


# ── PANEL-02 INVARIANTE-2 — global TradingAccount/Position lock order ───────
# Audited across every live path in this codebase that locks both models
# with select_for_update() inside a single transaction. All of them lock
# the TradingAccount row FIRST, then Position row(s):
#   - TradingConsumer._db_open_position_atomic  — TradingAccount →
#     Position(all open, this account, .order_by("id"))
#   - TradingConsumer._db_close_position_atomic — TradingAccount →
#     Position(single, by id)
#   - tasks._close_position_sync (Celery daemon TP/SL/stopout/margin-call
#     close)                     — TradingAccount → Position(single, by id)
#   - admin.py force_close (dealing desk)        — TradingAccount →
#     Position(all matching, .order_by("id"))
#
# This is a single, consistent, GLOBAL lock order — Account → Position —
# not a per-function choice. Any new code that locks both models MUST
# follow the same order; reversing it in even one path (Position → Account)
# would create a classic lock-order-inversion deadlock against every path
# above the moment two of them run concurrently on the same account.
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
        self._agg = {}
        self._last_bar_time = {}

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
        await self._recalc_account_and_push()
        await self.send_json({"type":"ack","action":"connected",
                              "timeframe":self.timeframe,"tf_sec":tf_seconds(self.timeframe)})

    async def disconnect(self, close_code):
        await self._ws_counter(-1)
        # Cancel heartbeat
        hb = getattr(self, "_heartbeat_task", None)
        if hb and not hb.done():
            hb.cancel()
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
                self._seed_price_state(new_sym)
                await self._feed.subscribe(new_sym, self.channel_layer, self.channel_name)
            self._last_bar_time.pop(new_sym, None)
            hist = await self.generate_history(new_sym, self.timeframe, bars=240)
            await self.send_json({"type": "history", "symbol": new_sym, "data": hist})
            await self._send_bridge_candle(new_sym, self.timeframe)
            await self.send_json({"type": "ack", "action": "symbol_changed", "symbol": new_sym})
            await self._refresh_and_send_positions()

        elif act == "change_timeframe":
            tf = normalize_tf(data.get("timeframe", self.timeframe))
            self.timeframe = tf
            self._reset_agg(self.symbol)
            self._last_bar_time.pop(self.symbol, None)
            hist = await self.generate_history(self.symbol, tf, bars=240)
            await self.send_json({"type": "history", "symbol": self.symbol, "data": hist})
            await self._send_bridge_candle(self.symbol, tf)
            await self.send_json({"type":"ack","action":"change_timeframe","timeframe":tf,"tf_sec":tf_seconds(tf)})

        elif act == "load_history":
            sym = data.get("symbol", self.symbol)
            tf  = normalize_tf(data.get("timeframe", self.timeframe))
            hist = await self.generate_history(sym, tf, bars=240)
            await self.send_json({"type":"history","symbol":sym,"data":hist})
            await self._send_bridge_candle(sym, tf)

        elif act == "account:get":
            await self._recalc_account_and_push()

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

        else:
            await self.send_json({"type":"ack","ok":True,"action":act})

    # ---------------- Streams ----------------
    # ---------------- Shared feed handler ----------------

    async def execution_close(self, event: dict):
        """Daemon-initiated close pushed via account_{id} channel group.

        Updates in-memory state atomically then pushes order_close + positions
        so the live UI reflects the close without requiring a reconnect.
        """
        pos_id      = event.get("position_id")
        new_balance = event.get("new_balance")
        realized    = float(event.get("realized_pnl", 0.0))
        new_status  = event.get("new_status")

        # Remove from in-memory positions list
        before = len(self._positions)
        self._positions = [p for p in self._positions if p["id"] != pos_id]
        if len(self._positions) == before:
            log.warning("[execution_close] pos %s not found in memory (concurrent close?)", pos_id)

        # Apply authoritative DB result
        if new_balance is not None:
            self.account["balance"] = float(new_balance)
        if new_status:
            self.account["status"] = new_status

        self._track_daily_pnl(realized)
        await self._recalc_account_and_push()

        await self.send_json({
            "type":         "order_close",
            "id":           pos_id,
            "symbol":       event.get("symbol"),
            "side":         event.get("side"),
            "qty":          event.get("qty"),
            "avg":          event.get("avg"),
            "close_px":     event.get("close_px"),
            "reason":       event.get("reason"),
            "realized_pnl": realized,
            "ts":           event.get("ts", int(time.time())),
        })
        await self._refresh_and_send_positions()

        # Stopout / margin-call UI notifications (additive — only for daemon-initiated paths)
        if new_status == "Suspendido":
            await self.send_json({
                "type":   "account:suspended",
                "status": "Suspendido",
                "reason": event.get("reason"),
            })
        elif event.get("reason") == "daemon_margin_call" and not self._positions:
            await self.send_json({
                "type":    "account:margin_call",
                "reason":  "margin_level_below_50pct",
                "balance": float(new_balance) if new_balance is not None else 0.0,
            })

    async def price_tick(self, event: dict):
        """Receives broadcast ticks from FeedManager via channel layer group."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        raw_bid = event["bid"]
        raw_ask = event["ask"]
        mid     = event["mid"]
        ts      = event["time"]

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
        await self.send_json({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "time": ts})
        await self._on_tick(symbol, mid, volume=0.0, ts=ts)
        await self._check_tp_sl(symbol, bid, ask)
        await self._recalc_account_and_push()

    async def candle_kline(self, event: dict):
        """Receives canonical OHLCV from exchange kline stream (Binance @kline_1m).
        Bypasses server-side aggregation — the exchange owns candle lifecycle."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        bar = event["data"]
        t   = int(bar["time"])
        last = self._last_bar_time.get(symbol)
        if last is None or t > last:
            self._last_bar_time[symbol] = t
            msg_type = "candle_new"
        else:
            msg_type = "candle_update"
        await self.send_json({
            "type": msg_type, "symbol": symbol,
            "data": {
                "time":  t,
                "open":  float(bar["open"]),
                "high":  float(bar["high"]),
                "low":   float(bar["low"]),
                "close": float(bar["close"]),
            },
        })
        await self.send_json({
            "type":   "volume_update",
            "symbol": symbol,
            "time":   t,
            "value":  float(bar.get("volume", 0.0)),
            "color":  "#26a69a" if float(bar["close"]) >= float(bar["open"]) else "#f44336",
        })

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
        if symbol in _KLINE_SYMBOLS:
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

    # ---------------- Historia sintética ----------------

    async def _send_bridge_candle(self, symbol: str, timeframe: str) -> None:
        """Send a flat candle at the CURRENT live bucket so the price line
        anchors to the real feed price immediately after history loads,
        eliminating the visual gap between synthetic history and live ticks."""
        px = self._price_state.get(symbol, base_price_for(symbol))
        _, dec = step_decimals_for(symbol)
        tf_sec = tf_seconds(timeframe)
        now = int(time.time())
        bucket = (now // tf_sec) * tf_sec
        px = round(px, dec)
        await self.send_json({
            "type": "candle_update",
            "symbol": symbol,
            "data": {"time": bucket, "open": px, "high": px, "low": px, "close": px},
        })
    async def generate_history(self, symbol, timeframe, bars=200):
        # For exchange-kline symbols, fetch real historical data from Binance REST.
        if symbol in _KLINE_SYMBOLS:
            hist = await self._feed.fetch_kline_history(symbol, interval=timeframe, limit=bars)
            if hist:
                # Snap the in-memory price state to the last closed bar so the
                # bridge candle and bid/ask calculations start at a real price.
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
            log.warning("[consumer] Binance REST history failed for %s — falling back to synthetic", symbol)

        # Synthetic history for non-Binance symbols (and Binance emergency fallback).
        base = self._price_state.get(symbol, base_price_for(symbol))
        step, dec = step_decimals_for(symbol)
        d = drift_for(symbol)
        now = int(time.time())
        tf_sec = tf_seconds(timeframe)
        current_bucket = (now // tf_sec) * tf_sec
        series = []
        price = base
        rnd = random.Random(symbol + timeframe)
        for i in range(1, bars + 1):
            ts = current_bucket - i * tf_sec
            c = price
            o = c + (rnd.random() - 0.5) * d
            h = max(o, c) + abs(rnd.random() - 0.5) * d * 0.6
            l = min(o, c) - abs(rnd.random() - 0.5) * d * 0.6
            price = o
            series.append({"time": ts, "open": round(o, dec), "high": round(h, dec),
                           "low": round(l, dec), "close": round(c, dec)})
        series.reverse()
        return series

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
        """Fill price when OPENING: buy fills at ask, sell fills at bid."""
        return self.get_ask(symbol) if side == "buy" else self.get_bid(symbol)

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

        # PANEL-04 — server-side SL/TP validation. Never trust the
        # frontend: a crafted WS payload can send NaN/Infinity/negative/
        # wrong-direction values regardless of what the <input
        # type=number> element normally constrains in a real browser. No
        # minimum-distance policy is enforced — see _validate_sl_tp's
        # docstring for why.
        _sl_tp_ok, _sl_tp_code, _sl_tp_msg = _validate_sl_tp(
            side, sl, tp, self.exec_price(sym, side),
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
            sym, qty, self.exec_price(sym, side), eq_now, mg_now,
            self.account, _spec.max_leverage, _spec.contract_size,
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
        px_exec = round(self.exec_price(sym, side), dec)

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

        # Step B — compute close values BEFORE any memory mutation
        sym      = found_pos["symbol"]
        dec      = step_decimals_for(sym)[1]
        close_px = round(self.close_price(sym, found_pos["side"]), dec)
        realized = self._realized_pnl_for(found_pos, close_px)
        new_balance = self.account["balance"] + realized
        remaining_floating = (
            self._unrealized_pnl_total()
            - self._unrealized_pnl_for(found_pos, close_px)
        )
        new_equity = round(new_balance + remaining_floating, 2)

        log.info("[close] MATCH pos id=%r sym=%r side=%r close_px=%s realized=%.4f",
                 found_pos["id"], sym, found_pos["side"], close_px, realized)

        pricing_context_close = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_CLOSE)

        # Step C — DB transaction FIRST (Phase 1B: DB-first close)
        try:
            result = await self._db_close_position_atomic(
                found_pos, close_px, "manual", realized, new_balance, new_equity,
                pricing_context_close=pricing_context_close,
            )
        except Exception as exc:
            log.error("[close] DB close failed for pos id=%r: %s", found_pos["id"], exc, exc_info=True)
            await self.send_json({"type": "error", "code": "close_failed",
                                  "message": "no_se_pudo_cerrar_posicion"})
            return  # memory untouched — position still open

        # Step D — DB committed: safe to mutate memory now. The position
        # is gone from DB either way (this call really closed it, OR a
        # concurrent connection/daemon already did) — remove it from
        # memory unconditionally.
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
            self._track_daily_pnl(realized)

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
        await self.send_json({"type": "risk_preview", **assessment})

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
        entry_px = self.exec_price(symbol, side)
        est_margin = abs(entry_px * qty * spec.contract_size) / lev
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
        never a fabricated number."""
        out = []
        for p in self._positions:
            d = dict(p)
            try:
                px = self.close_price(p["symbol"], p["side"])
                d["pnl"] = round(self._unrealized_pnl_for(p, px), 2)
            except Exception as exc:
                log.debug("[positions_snapshot] pnl calc failed for pos=%s: %r", p.get("id"), exc)
                d["pnl"] = None
            out.append(d)
        return out

    def _unrealized_pnl_total(self):
        total = 0.0
        for p in self._positions:
            px = self.close_price(p["symbol"], p["side"])
            total += self._unrealized_pnl_for(p, px)
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
        account_lev = max(1, int(self.account.get("leverage", 50)))
        total = 0.0
        for p in self._positions:
            spec = get_spec(p["symbol"])
            lev = max(1, min(account_lev, spec.max_leverage))
            notional = abs(p["avg"] * p["qty"] * spec.contract_size)
            total += notional / lev
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

        # Real-time stopout — only check if account is currently active
        if self.account.get("status") == "Activo" and self._positions:
            _acct_type = self.account.get("account_type", "CHALLENGE")
            from .risk_engine import check_equity_stopout
            if check_equity_stopout(
                equity=self.account["equity"],
                peak_balance=self.account["peak_balance"],
                tier=self.account.get("tier", "10K"),
                account_type=_acct_type,
                margin_used=self.account.get("margin_used", 0.0),
                stopout_level=self.account.get("stopout_level", 50.0),
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

        from .risk_engine import compute_margin_state
        _ms = compute_margin_state(equity_val, margin_used)
        used_margin_pct   = _ms["used_margin_pct"]
        maintenance_margin = _ms["maintenance_margin"]
        liquidation_distance = _ms["liquidation_distance"]

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
            cpx  = round(self.close_price(sym, p["side"]), dec)
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

        # DB commits done — update memory, then persist suspension.
        self._positions = failed_positions
        # pnl_unreal/margin_used zeroed BEFORE any balance/equity refresh
        # below — _refresh_account_after_stale_close() derives equity as
        # fresh_balance + self.account["pnl_unreal"], so this order
        # matters: a stopout intends equity == balance (any leftover
        # failed_positions notwithstanding, same approximation the
        # non-stale branch below already made), not balance + a
        # still-stale pre-stopout pnl_unreal.
        self.account["pnl_unreal"]  = 0.0
        self.account["margin_used"] = 0.0
        if saw_stale_close:
            # At least one collision — force a fresh, non-throttled
            # balance/equity read rather than trust running_balance (see
            # _refresh_account_after_stale_close's docstring).
            await self._refresh_account_after_stale_close()
        else:
            self.account["balance"] = running_balance
            self.account["equity"]  = round(running_balance, 2)

        try:
            await self._db_suspend_account("stopout")
        except Exception as exc:
            log.error("[stopout] DB suspend failed: %s", exc)

        self.account["status"] = "Suspendido"

        # Notify client
        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self._refresh_and_send_positions()
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
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": self.account["equity"],
            "pnl_unreal": 0.0,
            "upnl": 0.0,
            "margin_used": 0.0,
            "free_margin": self.account["equity"],
            "used_margin_pct": 0.0,
            "maintenance_margin": 0.0,
            "liquidation_distance": self.account["equity"],
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": "Suspendido",
            "account_type": self.account.get("account_type", "CHALLENGE"),
            "total_dd_pct": total_dd_pct,
            "daily_dd_pct": daily_dd_pct,
            "daily_pnl": round(daily_pnl, 2),
            "margin_level": 0.0,
            "bid": round(self.get_bid(self.symbol), step_decimals_for(self.symbol)[1]),
            "ask": round(self.get_ask(self.symbol), step_decimals_for(self.symbol)[1]),
            "spread": 0.0,
            "profit_target": self.account.get("profit_target", 800.0),
            "initial_balance": self.account.get("initial_balance", self.account.get("balance", 0.0)),
        })

    async def _do_retail_liquidation(self) -> None:
        """RETAIL margin call — close all positions, account stays ACTIVE.
        Triggers when margin_level < stopout_level. Unlike _do_stopout, no suspension."""
        _stopout_threshold = self.account.get("stopout_level", 50.0)
        log.warning("[margin_call] margin_level<%.0f%% equity=%.2f margin=%.2f account #%s",
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
            cpx  = round(self.close_price(sym, p["side"]), dec)
            realized = self._realized_pnl_for(p, cpx)
            new_balance = running_balance + realized
            fp_p = self._unrealized_pnl_for(p, cpx)
            remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
            new_equity = round(new_balance + remaining_floating, 2)
            pricing_context_close = self._capture_pricing_context(sym, profile=pricing_ctx.PROFILE_WS_MARGIN_CALL)
            try:
                result = await self._db_close_position_atomic(
                    p, cpx, "margin_call", realized, new_balance, new_equity,
                    pricing_context_close=pricing_context_close,
                )
            except Exception as exc:
                log.error("[margin_call] DB close failed pos %s: %s", p["id"], exc)
                failed_positions.append(p)
                continue

            # PANEL-03 — position is gone from DB either way once we reach
            # here — never re-add to failed_positions.
            outcome = self._handle_close_result(p, result, cpx, "margin_call", realized, now_ts)
            if outcome is None:
                saw_stale_close = True
            else:
                running_balance = outcome["new_balance"]
                accum_floating_closed += fp_p
                self._track_daily_pnl(realized)
                closed_items.append(outcome["notify_item"])

        # DB commits done — update memory. pnl_unreal/margin_used zeroed
        # BEFORE any balance/equity refresh below — see the matching
        # comment in _do_stopout for why the order matters.
        self._positions = failed_positions
        self.account["pnl_unreal"]  = 0.0
        self.account["margin_used"] = 0.0
        if saw_stale_close:
            await self._refresh_account_after_stale_close()
        else:
            self.account["balance"] = running_balance
            self.account["equity"]  = round(self.account["balance"], 2)

        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self._refresh_and_send_positions()
        await self.send_json({
            "type": "account:margin_call",
            "reason": "margin_level_below_stopout",
            "stopout_level": _stopout_threshold,
            "balance": round(self.account["balance"], 2),
        })
        dec = step_decimals_for(self.symbol)[1]
        balance = self.account["balance"]
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": self.account["equity"],
            "pnl_unreal": 0.0, "upnl": 0.0,
            "margin_used": 0.0, "free_margin": self.account["equity"],
            "used_margin_pct": 0.0, "maintenance_margin": 0.0,
            "liquidation_distance": self.account["equity"],
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),  # stays Active
            "account_type": "RETAIL",
            "total_dd_pct": 0.0, "daily_dd_pct": 0.0,
            "daily_pnl": round(self._daily_realized_pnl, 2),
            "margin_level": 0.0,
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
        return {
            "new_balance": result["new_balance"],
            "new_peak": result.get("new_peak"),
            "new_status": result.get("new_status"),
            "notify_item": {
                "id": pos["id"], "symbol": pos["symbol"], "side": pos["side"],
                "qty": pos["qty"], "avg": pos["avg"],
                "close_px": close_px, "reason": reason,
                "realized_pnl": realized_pnl, "ts": ts,
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
        TradingConsumer instance and its own self._positions, hydrated once
        at connect() time and never synced with sibling connections for the
        same account (no group_send exists for manual order events). A
        connection that opens/closes/edits a position, or merely switches
        symbol, could otherwise emit its own possibly-incomplete in-memory
        view — the frontend then propagates that snapshot to every panel,
        silently discarding positions opened through OTHER panels (the
        confirmed root cause of the multipanel "position disappears" bug).

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
        self.account["margin_call_level"]  = acc.get("margin_call_level", 100.0)
        self.account["stopout_level"]      = acc.get("stopout_level", 50.0)
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
            return {"position_id": None, "merged": False, "ok": True}
        from decimal import Decimal
        with transaction.atomic():
            # 1. Lock the TradingAccount row FIRST — this is the account's
            # real mutex (see the module-level LOCK ORDER note above this
            # class: global order is TradingAccount → Position across
            # EVERY live path). Locking Account first — even when the
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
            # _margin_used_total(), just fed DB-fresh data — no price
            # dependency at all (margin uses each position's own entry
            # price, not a live price).
            account_lev = max(1, int(self.account.get("leverage", 50)))
            fresh_margin_used = 0.0
            for _p in open_positions:
                _pspec = get_spec(_p.symbol)
                _plev = max(1, min(account_lev, _pspec.max_leverage))
                fresh_margin_used += (
                    abs(float(_p.avg_price) * float(_p.qty) * _pspec.contract_size) / _plev
                )

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
            }
            guard = _compute_atomic_open_guard(
                symbol, qty, price, account.status, _account_snap, _spec,
                fresh_equity, fresh_margin_used, is_new_position, fresh_open_count,
                _rule.max_open_positions,
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
                    **guard,
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

            _commission_d = Decimal(str(commission)) if commission and commission > 0 else Decimal("0")
            # Authoritative balance: deduct commission from the already-locked
            # account row, not stale memory.
            _auth_balance = account.balance - _commission_d

            if _commission_d > 0:
                trader_ledger = LedgerEntry.objects.create(
                    account_id=self._db_account_id,
                    event_type=LedgerEntry.EV_COMMISSION,
                    amount=-_commission_d,
                    balance_after=_auth_balance,
                    meta={"symbol": symbol, "side": side, "db_pos_id": position_id},
                )
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

            # BrokerLedger SPREAD — revenue = (base_pips + account_markup) × pip_size/2 × qty × contract_size
            # Nested savepoint: spread revenue failure never rolls back the trader transaction.
            _spread_cfg = _get_spread_config(symbol)
            _base_pips     = float(_spread_cfg.spread_pips) if (_spread_cfg is not None and _spread_cfg.enabled) else 0.0
            _markup_pips   = float(self.account.get("spread_pips", 0.0) or 0.0)
            _effective_pips = _base_pips + _markup_pips
            if _effective_pips > 0:
                try:
                    with transaction.atomic():
                        _spread_rev = calculate_spread_revenue(
                            symbol, float(qty), _effective_pips,
                        )
                        if _spread_rev > 0:
                            BrokerLedger.objects.create(
                                revenue_type=BrokerLedger.REV_SPREAD,
                                amount=Decimal(str(_spread_rev)),
                                source_account_id=self._db_account_id,
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

            if _commission_d > 0:
                account.balance = _auth_balance
                account.save(update_fields=["balance"])

        log.info("[db_open] pos_id=%s symbol=%s side=%s qty=%s merged=%s balance=%.4f",
                 position_id, symbol, side, qty, merged, float(_auth_balance))
        return {
            "position_id": position_id, "merged": merged, "new_balance": float(_auth_balance),
            **guard,
        }

    @database_sync_to_async
    def _db_close_position_atomic(self, pos_mem: dict, close_px: float, reason: str,
                                   realized_pnl: float, new_balance: float,
                                   new_equity: float,
                                   pricing_context_close: dict | None = None) -> dict:
        """DB-first order close (Phase 1B).

        Atomically: find+lock Position, create Trade, record LedgerEntry EV_REALIZED,
        delete Position, update TradingAccount balance/equity, run risk+intelligence engines.
        All committed before any memory mutation in the caller.

        Returns final DB state dict. Raises on failure — caller leaves memory untouched.

        pricing_context_close (SPREAD-02): the Trade's pricing_context_open is
        copied verbatim from the locked Position row's pricing_context — never
        recomputed here, so a BrokerSpreadConfig change between open and close
        cannot retroactively alter what was captured at open.
        """
        if not self._db_account_id:
            # Demo/anonymous session — skip DB, return current values for memory mutation.
            return {
                "new_balance": new_balance,
                "new_equity":  new_equity,
                "new_status":  self.account.get("status", "Activo"),
                "new_peak":    self.account.get("peak_balance", new_balance),
                "violations":  [],
                "trade_id":    None,
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
                    "new_balance":    new_balance,
                    "new_equity":     new_equity,
                    "new_status":     self.account.get("status", "Activo"),
                    "new_peak":       self.account.get("peak_balance", new_balance),
                    "violations":     [],
                    "trade_id":       None,
                    "already_closed": True,
                }

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

            # MARGIN-02 — audit-only recompute of the SAME pure function
            # already used to derive realized_pnl (pos_mem's avg/qty/symbol
            # + close_px + account currency, all frozen inputs) — never a
            # second, independently-timed source. See simulator/pnl_engine.py.
            _closed_at = timezone.now()
            _pnl_result = pnl_engine.calculate_position_pnl(
                pos_mem["side"], pos_mem["avg"], close_px, pos_mem["qty"], pos_mem["symbol"],
                account_currency=self.account.get("currency", "USD"),
            )
            _pnl_conversion = _pnl_result.to_dict()
            _pnl_conversion["conversion_timestamp"] = _closed_at.timestamp()

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
                closed_at=_closed_at,
                pricing_context_open=pos.pricing_context,
                pricing_context_close=pricing_context_close,
                pnl_conversion=_pnl_conversion,
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

            # 4. Delete Position if it existed in DB.
            if pos:
                pos.delete()

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

        log.info("[db_close] OK pos_id=%r trade_id=%r realized=%.4f balance=%.2f status=%s",
                 pos_mem["id"], trade.id, realized_pnl, float(_safe_balance), final_status)
        return {
            "new_balance": float(_safe_balance),
            "new_equity":  float(_safe_equity),
            "new_status":  final_status,
            "new_peak":    final_peak,
            "violations":  [v.violation_type for v in violations],
            "trade_id":    trade.id,
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