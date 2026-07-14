"""
simulator/tasks.py
Celery tasks.

Layers:
  Infrastructure (ping, email, reconcile, snapshots, cleanup) — no financial writes.
  Phase 2A price helpers (_read_cached_price, test_price_cache_read) — read-only Redis.
  Phase 2A execution daemon (scan_positions) — added in Step 3, writes Trades/Ledger.
"""
import logging
from celery import shared_task
from django.conf import settings

logger = logging.getLogger("simulator.tasks")

# ── Key prefix must match the writer in market_data/feeds.py ──────────────────
_PRICE_CACHE_KEY_PREFIX = "trx:price"


def _read_cached_price(symbol: str) -> tuple[float | None, float | None]:
    """
    Read bid/ask written by FeedManager from Redis.
    Returns (bid, ask) as floats, or (None, None) if keys are missing or stale.
    Must never raise — any failure is safe-returns (None, None).
    """
    try:
        import redis as _redis
        url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        bid_raw = r.get(f"{_PRICE_CACHE_KEY_PREFIX}:bid:{symbol}")
        ask_raw = r.get(f"{_PRICE_CACHE_KEY_PREFIX}:ask:{symbol}")
        if bid_raw is None or ask_raw is None:
            logger.debug("[price_cache] stale/missing keys for %s", symbol)
            return (None, None)
        return (float(bid_raw), float(ask_raw))
    except Exception as exc:
        logger.debug("[price_cache] read failed for %s: %r", symbol, exc)
        return (None, None)


# ──────────────────────────────────────────────────────
# PING — infrastructure health test
# ──────────────────────────────────────────────────────
@shared_task(name="simulator.ping", bind=True, max_retries=0)
def ping_task(self, payload: str = "pong") -> dict:
    """Simple round-trip task to verify workers are processing."""
    import time
    logger.info("[ping_task] worker=%s payload=%s", self.request.hostname, payload)
    return {
        "status": "ok",
        "payload": payload,
        "worker": self.request.hostname,
        "task_id": self.request.id,
        "timestamp": time.time(),
    }


# ──────────────────────────────────────────────────────
# EMAIL ASYNC — fire-and-forget email delivery
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.send_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,        # retry after 60s
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_email_async(
    self,
    subject: str,
    message: str,
    recipient_list: list[str],
    html_message: str | None = None,
    from_email: str | None = None,
) -> dict:
    """
    Send an email via Django's email backend.
    Drop-in replacement for synchronous send_mail() calls.
    """
    from django.core.mail import send_mail as _send
    from_email = from_email or settings.DEFAULT_FROM_EMAIL
    logger.info("[send_email_async] to=%s subject=%r", recipient_list, subject[:60])
    _send(
        subject=subject,
        message=message,
        from_email=from_email,
        recipient_list=recipient_list,
        html_message=html_message,
        fail_silently=False,
    )
    logger.info("[send_email_async] sent OK → %s", recipient_list)
    return {"sent": True, "recipients": recipient_list}


# ──────────────────────────────────────────────────────
# DEPOSIT RECONCILIATION — safe read-only audit
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.reconcile_deposits",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def reconcile_deposits_task(self, hours_back: int = 24) -> dict:
    """
    Audit unconfirmed deposits older than `hours_back` hours.
    READ-ONLY: logs anomalies, does NOT modify wallet or ledger.
    A human (admin) must act on the log output.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import Deposit

    cutoff = timezone.now() - timedelta(hours=hours_back)
    pending = Deposit.objects.filter(
        created_at__lte=cutoff,
        credited=False,
    ).values("id", "order_id", "amount_usd", "created_at")

    ids = [d["id"] for d in pending]
    if ids:
        logger.warning(
            "[reconcile_deposits] %d unconfirmed deposit(s) older than %dh: IDs=%s",
            len(ids), hours_back, ids,
        )
    else:
        logger.info("[reconcile_deposits] all deposits confirmed within %dh window", hours_back)

    return {"checked": True, "stale_count": len(ids), "stale_ids": ids}


# ──────────────────────────────────────────────────────
# WITHDRAWAL RECONCILIATION — safe read-only audit
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.reconcile_withdrawals",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def reconcile_withdrawals_task(self, hours_back: int = 48) -> dict:
    """
    Audit pending/processing withdrawals stuck for too long.
    READ-ONLY: logs anomalies, does NOT modify wallet or ledger.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import WithdrawalRequest

    cutoff = timezone.now() - timedelta(hours=hours_back)
    stuck = WithdrawalRequest.objects.filter(
        created_at__lte=cutoff,
        status__in=["pending", "processing"],
    ).values("id", "amount_usd", "status", "created_at")

    ids = [w["id"] for w in stuck]
    if ids:
        logger.warning(
            "[reconcile_withdrawals] %d stuck withdrawal(s) older than %dh: IDs=%s",
            len(ids), hours_back, ids,
        )
    else:
        logger.info("[reconcile_withdrawals] no stuck withdrawals in %dh window", hours_back)

    return {"checked": True, "stuck_count": len(ids), "stuck_ids": ids}


# ──────────────────────────────────────────────────────
# EQUITY SNAPSHOTS — time-series financial state capture
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.take_snapshots",
    bind=True,
    max_retries=1,
    default_retry_delay=20,
    acks_late=True,          # only ack after the task completes (prevents duplicate snapshots)
    soft_time_limit=55,      # raises SoftTimeLimitExceeded before the next minute tick
    time_limit=90,           # hard kill — prevents a hung task from blocking the worker
)
def take_snapshots_task(self) -> dict:
    """
    Capture broker-wide + per-account equity state.
    Writes BrokerEquitySnapshot + AccountEquitySnapshot rows.
    No financial mutations — snapshot rows only.
    """
    import time as _t
    from .snapshots import take_all_snapshots
    logger.info("[take_snapshots] starting worker=%s", self.request.hostname)
    t0 = _t.monotonic()
    result = take_all_snapshots()
    duration_s = _t.monotonic() - t0
    logger.info(
        "[take_snapshots] done broker_id=%s accounts=%d equity=%.2f duration_s=%.2f",
        result.get("broker_snapshot_id"), result.get("account_snapshots", 0),
        result.get("total_equity", 0.0), duration_s,
    )
    try:
        from django.conf import settings as _s
        from .observability import peak_update
        _redis_url = getattr(_s, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        peak_update(_redis_url, "snapshot_duration_s", duration_s)
    except Exception:
        pass
    result["duration_s"] = round(duration_s, 3)
    return result


@shared_task(
    name="simulator.take_revenue_snapshot",
    bind=True,
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=50,
    time_limit=80,
    ignore_result=False,
)
def take_revenue_snapshot_task(self) -> dict:
    """
    Capture a point-in-time broker revenue snapshot every 5 minutes.

    Computation is incremental: reads only BrokerLedger rows written SINCE
    the previous snapshot, then adds the delta to the previous cumulative
    totals. This keeps the per-run cost O(new entries in last 5 min) rather
    than O(all-time entries).

    No financial mutations — INSERT into BrokerRevenueSnapshot only.
    Operational state (active_accounts, open_positions) uses indexed COUNT.
    Exposure fields are copied from the latest BrokerEquitySnapshot (already
    computed by the 1-min take_snapshots task) to avoid re-running the full
    live-analytics engine here.

    Failure recovery: if this task is missed for any period, the next run
    computes the incremental delta from the last successfully-written snapshot,
    producing an accurate cumulative total with a gap in the time-series only.
    """
    import time as _t
    from decimal import Decimal
    from django.db.models import Sum, Q
    from .models import (
        BrokerLedger, BrokerEquitySnapshot, TradingAccount, Position,
        BrokerRevenueSnapshot,
    )

    t0 = _t.monotonic()

    def _d(v) -> Decimal:
        return Decimal(str(v or 0))

    # ── 1. Previous snapshot as running-total base ────────────────────
    prev = BrokerRevenueSnapshot.objects.order_by("-taken_at").first()
    since = prev.taken_at if prev else None

    # ── 2. Incremental BrokerLedger delta since last snapshot ─────────
    inc_qs = (
        BrokerLedger.objects.filter(created_at__gt=since)
        if since else BrokerLedger.objects.all()
    )
    inc = inc_qs.aggregate(
        revenue    = Sum("amount"),
        commission = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_COMMISSION)),
        spread     = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_SPREAD)),
        challenge  = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)),
        withdraw   = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_WITHDRAW_FEE)),
        adjustment = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_ADJUSTMENT)),
    )

    # ── 3. New cumulative totals ──────────────────────────────────────
    prev_total      = _d(prev.total_revenue    if prev else 0)
    prev_commission = _d(prev.total_commission if prev else 0)
    prev_spread     = _d(prev.total_spread     if prev else 0)
    prev_challenge  = _d(prev.total_challenge  if prev else 0)
    prev_withdraw   = _d(prev.total_withdraw   if prev else 0)
    prev_adjustment = _d(prev.total_adjustment if prev else 0)

    d_revenue    = _d(inc["revenue"])
    d_commission = _d(inc["commission"])
    d_spread     = _d(inc["spread"])
    d_challenge  = _d(inc["challenge"])
    d_withdraw   = _d(inc["withdraw"])
    d_adjustment = _d(inc["adjustment"])

    # ── 4. Operational state — two indexed COUNT queries ─────────────
    active_accounts = TradingAccount.objects.filter(status="Activo").count()
    open_positions  = Position.objects.count()

    # ── 5. Exposure from latest equity snapshot (0 DB round-trips if cached) ─
    eq_snap = BrokerEquitySnapshot.objects.order_by("-taken_at").first()
    net_exposure = _d(eq_snap.net_exposure_usd if eq_snap else 0)
    gross_long   = _d(eq_snap.gross_long_usd   if eq_snap else 0)
    gross_short  = _d(eq_snap.gross_short_usd  if eq_snap else 0)

    # ── 6. Write snapshot row ─────────────────────────────────────────
    snap = BrokerRevenueSnapshot.objects.create(
        total_revenue    = prev_total      + d_revenue,
        total_commission = prev_commission + d_commission,
        total_spread     = prev_spread     + d_spread,
        total_challenge  = prev_challenge  + d_challenge,
        total_withdraw   = prev_withdraw   + d_withdraw,
        total_adjustment = prev_adjustment + d_adjustment,
        period_revenue    = d_revenue,
        period_commission = d_commission,
        period_spread     = d_spread,
        active_accounts  = active_accounts,
        open_positions   = open_positions,
        net_exposure_usd = net_exposure,
        gross_long_usd   = gross_long,
        gross_short_usd  = gross_short,
    )

    duration_ms = round((_t.monotonic() - t0) * 1000, 1)
    logger.info(
        "[revenue_snapshot] id=%d total=%.2f period=%.2f accounts=%d positions=%d duration_ms=%.1f",
        snap.pk, float(snap.total_revenue), float(snap.period_revenue),
        active_accounts, open_positions, duration_ms,
    )
    return {
        "snapshot_id":    snap.pk,
        "taken_at":       snap.taken_at.isoformat(),
        "total_revenue":  float(snap.total_revenue),
        "period_revenue": float(snap.period_revenue),
        "active_accounts": active_accounts,
        "open_positions":  open_positions,
        "duration_ms":     duration_ms,
    }


@shared_task(
    name="simulator.cleanup_audit_log",
    bind=True,
    max_retries=1,
    default_retry_delay=120,
)
def cleanup_audit_log_task(self, retention_days: int = 30) -> dict:
    """
    Delete AuditLog rows older than retention_days (default 30).
    Never touches financial data — audit rows only.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import AuditLog

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _ = AuditLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info(
        "[cleanup_audit_log] retention=%dd cutoff=%s deleted=%d",
        retention_days, cutoff.isoformat(), deleted,
    )
    return {"retention_days": retention_days, "deleted": deleted}


@shared_task(
    name="simulator.cleanup_snapshots",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
)
def cleanup_snapshots_task(self, retention_days: int | None = None) -> dict:
    """
    Delete equity snapshot rows older than retention_days (default: SNAPSHOT_RETENTION_DAYS env).
    Never touches financial data.
    """
    from .snapshots import cleanup_old_snapshots
    result = cleanup_old_snapshots(retention_days)
    logger.info(
        "[cleanup_snapshots] retention=%dd broker_del=%d account_del=%d",
        result["retention_days"], result["broker_snapshots_deleted"],
        result["account_snapshots_deleted"],
    )
    return result


# ──────────────────────────────────────────────────────
# PHASE 2A — Sync position close primitive
# Called by scan_positions_task (Celery sync context).
# Mirrors _db_close_position_atomic logic without @database_sync_to_async.
# ──────────────────────────────────────────────────────

def _close_position_sync(
    pos_mem: dict,
    account_id: int,
    close_px: float,
    reason: str,
    realized_pnl: float,
    new_balance: float,
    new_equity: float,
    pricing_context: dict | None = None,
    account_currency: str = "USD",
) -> dict:
    """
    Atomic DB-first position close for sync (Celery) callers.
    Same guarantees as TradingConsumer._db_close_position_atomic:
      - select_for_update prevents duplicate closes
      - already_closed=True returned (no Trade/LedgerEntry) if position gone
      - raises on unexpected DB error (caller logs and skips)

    pricing_context (SPREAD-02): the Trade's pricing_context_open is copied
    verbatim from the locked Position row's pricing_context — never
    recomputed here. pricing_context is stored as pricing_context_close.
    """
    from decimal import Decimal
    from datetime import datetime as _dt, timezone as _dt_tz
    import time as _time
    from django.db import transaction as _tx
    from django.utils import timezone as _django_tz
    from .models import Position, Trade, LedgerEntry, TradingAccount

    with _tx.atomic():
        pos = (
            Position.objects
            .select_for_update()
            .filter(id=pos_mem["id"], account_id=account_id)
            .first()
        )
        if pos is None:
            logger.info("[close_sync] pos %r already closed — skipping duplicate", pos_mem["id"])
            return {
                "already_closed": True,
                "new_balance":    new_balance,
                "new_equity":     new_equity,
                "new_status":     None,
                "new_peak":       None,
                "violations":     [],
                "trade_id":       None,
            }

        # ACCOUNT-02 — lock TradingAccount now (lock order Position → Account) and
        # derive balance_after from a FRESH, locked read + realized_pnl — never
        # from new_balance (a parameter the caller computed by accumulating
        # running_balance across this batch, starting from account.balance read
        # at the TOP of the daemon scan — which a concurrent WS close on the same
        # account could have already changed by the time this write happens).
        # remaining_floating (other still-open positions' floating PnL in this
        # same batch) is staleness-independent: new_equity - new_balance cancels
        # out whatever starting balance the caller used. See
        # TradingConsumer._db_close_position_atomic for the identical pattern.
        _ZERO = Decimal("0")
        _nb = new_balance if isinstance(new_balance, Decimal) else Decimal(str(new_balance))
        _ne = new_equity  if isinstance(new_equity,  Decimal) else Decimal(str(new_equity))
        _remaining_floating = _ne - _nb

        _acct_row = (
            TradingAccount.objects
            .select_for_update()
            .filter(id=account_id)
            .first()
        )
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
            logger.critical(
                "[close_sync] NEGATIVE BALANCE PREVENTED: account=%s realized=%.4f "
                "computed_balance=%s shortfall=%s — clamping to 0",
                account_id, realized_pnl, _fresh_balance_after, _shortfall,
            )

        # MARGIN-02 — audit-only recompute of the SAME pure function already
        # used to derive realized_pnl (same frozen inputs), mirroring
        # TradingConsumer._db_close_position_atomic. See simulator/pnl_engine.py.
        from . import pnl_engine as _pnl_engine
        _closed_at = _django_tz.now()
        _pnl_result = _pnl_engine.calculate_position_pnl(
            pos_mem["side"], pos_mem["avg"], close_px, pos_mem["qty"], pos_mem["symbol"],
            account_currency=account_currency,
        )
        _pnl_conversion = _pnl_result.to_dict()
        _pnl_conversion["conversion_timestamp"] = _closed_at.timestamp()

        trade = Trade.objects.create(
            account_id    = account_id,
            symbol        = pos_mem["symbol"],
            trade_type    = trade_type,
            lot_size      = Decimal(str(pos_mem["qty"])),
            entry_price   = Decimal(str(pos_mem["avg"])),
            exit_price    = Decimal(str(close_px)),
            stop_loss     = Decimal(str(pos_mem["sl"]))   if pos_mem.get("sl") is not None else None,
            take_profit   = Decimal(str(pos_mem["tp"]))   if pos_mem.get("tp") is not None else None,
            profit_loss   = Decimal(str(realized_pnl)),
            opened_at     = _dt.fromtimestamp(int(pos_mem.get("opened_at", _time.time())), tz=_dt_tz.utc),
            closed_at     = _closed_at,
            pricing_context_open  = pos.pricing_context,
            pricing_context_close = pricing_context,
            pnl_conversion         = _pnl_conversion,
        )

        LedgerEntry.objects.create(
            account_id    = account_id,
            event_type    = LedgerEntry.EV_REALIZED,
            amount        = Decimal(str(realized_pnl)),
            balance_after = _safe_balance,
            meta          = {"symbol": pos_mem["symbol"], "side": pos_mem["side"],
                             "reason": reason, "trade_id": trade.id},
        )

        if _shortfall > _ZERO:
            LedgerEntry.objects.create(
                account_id    = account_id,
                event_type    = LedgerEntry.EV_ADJUST,
                amount        = _ZERO,
                balance_after = _safe_balance,
                meta          = {
                    "reason":                    "negative_balance_guard",
                    "shortfall":                 float(_shortfall),           # float for JSON
                    "original_computed_balance": float(_fresh_balance_after),  # float for JSON
                    "realized_pnl":              realized_pnl,
                },
            )

        pos.delete()

        # _acct_row was already locked above (before Trade/LedgerEntry creation) —
        # reuse it instead of a second fetch.
        account = _acct_row
        if account:
            account.balance = _safe_balance
            account.equity  = _safe_equity
            account.save(update_fields=["balance", "equity"])

            from .risk_engine import check_and_enforce_risk
            violations = check_and_enforce_risk(account)
            if violations:
                logger.warning("[close_sync] risk violations account #%s: %s",
                               account_id, [v.violation_type for v in violations])

            from .intelligence_engine import update_intelligence
            update_intelligence(account)

            final_status = account.status
            final_peak   = float(account.peak_balance)
        else:
            violations   = []
            final_status = "Activo"
            final_peak   = float(_safe_balance)

    logger.info("[close_sync] OK pos=%r trade=%r realized=%.4f balance=%.2f status=%s",
                pos_mem["id"], trade.id, realized_pnl, float(_safe_balance), final_status)
    return {
        "already_closed": False,
        "new_balance":    float(_safe_balance),
        "new_equity":     float(_safe_equity),
        "new_status":     final_status,
        "new_peak":       final_peak,
        "violations":     [v.violation_type for v in violations],
        "trade_id":       trade.id,
    }


def _daemon_pricing_context(symbol: str, bid: float, ask: float, *, profile: str) -> dict:
    """
    SPREAD-02 — pricing context for a daemon-driven close (stopout,
    margin-call, or SL/TP evaluated offline in scan_positions_task).

    The daemon reads bid/ask straight from the Redis price cache (raw
    FeedManager output — see market_data/feeds.py's _write_price_cache)
    and uses them directly as close_px — it never calls broker_price().
    So this context must say, explicitly and honestly, that no spread
    participated in the fill: executable_bid/executable_ask are set equal
    to the raw values actually used, and base_spread_pips/
    account_markup_pips/effective_spread_pips are hardcoded to 0.0 —
    NOT a BrokerSpreadConfig/account-snapshot read. Reporting a configured-
    but-unapplied pips value here would misrepresent what actually produced
    close_px; see docs/PRICING_CONTEXT.md.

    provider_id/source_state are still read best-effort from F13
    observability — they describe the tick's origin, independent of
    whether any markup was applied to it.

    Never raises.
    """
    try:
        from . import pricing_context as _pc

        provider_id, source_state = _pc.provider_state_for(symbol)
        return _pc.build_pricing_context(
            raw_bid=bid,
            raw_ask=ask,
            executable_bid=bid,
            executable_ask=ask,
            base_spread_pips=0.0,
            account_markup_pips=0.0,
            provider_id=provider_id,
            source_state=source_state,
            router_provider=provider_id,
            pricing_timestamp=None,
            pricing_profile=profile,
        )
    except Exception as exc:
        logger.debug("[pricing_context] daemon capture failed for %s profile=%s (non-fatal): %r",
                     symbol, profile, exc)
        from . import pricing_context as _pc
        return {"schema_version": _pc.SCHEMA_VERSION, "pricing_profile": _pc.PROFILE_CAPTURE_FAILED}


# ──────────────────────────────────────────────────────
# PHASE 2A — Step 5 helpers (stopout / margin-call daemon)
# ──────────────────────────────────────────────────────

def _compute_offline_equity_margin(pos_list: list, prices: dict, account) -> tuple:
    """
    Pure offline calculator — no DB reads.
    Requires all symbols in pos_list to exist in prices (caller must verify).

    Returns (equity, margin_used, total_floating, pos_fp_map) where:
      pos_fp_map: dict[pos.id -> floating PnL at current bid/ask]
    """
    from market_data.symbol_specs import get_spec
    from . import pnl_engine

    account_lev      = max(1, int(account.leverage))
    account_currency = getattr(account, "currency", "USD") or "USD"
    total_floating   = 0.0
    margin_used      = 0.0
    pos_fp_map: dict = {}

    for pos in pos_list:
        bid, ask = prices[pos.symbol]
        spec     = get_spec(pos.symbol)
        avg      = float(pos.avg_price)
        qty      = float(pos.qty)
        close_px = bid if pos.side == "BUY" else ask

        # MARGIN-02 — same conversion as consumers.py::_unrealized_pnl_for();
        # see simulator/pnl_engine.py for the fix (quote-currency PnL was
        # never converted to account_currency before this block).
        fp = pnl_engine.position_pnl_float(
            pos.side, avg, close_px, qty, pos.symbol, account_currency=account_currency,
        )

        total_floating     += fp
        pos_fp_map[pos.id]  = fp

        lev      = max(1, min(account_lev, spec.max_leverage))
        notional = abs(avg * qty * spec.contract_size)
        margin_used += notional / lev

    equity = float(account.balance) + total_floating
    return equity, margin_used, total_floating, pos_fp_map


def _db_suspend_account_sync(account_id: int, reason: str, balance: float) -> None:
    """
    Sync: set account status → Suspendido + write ADJUSTMENT LedgerEntry.
    Idempotent — no-ops if already Suspendido.
    """
    from decimal import Decimal
    from django.db import transaction as _tx
    from .models import TradingAccount, LedgerEntry

    with _tx.atomic():
        account = (
            TradingAccount.objects
            .select_for_update()
            .filter(id=account_id)
            .first()
        )
        if account and account.status != "Suspendido":
            account.status = "Suspendido"
            account.save(update_fields=["status"])
            LedgerEntry.objects.create(
                account       = account,
                event_type    = LedgerEntry.EV_ADJUST,
                amount        = Decimal("0"),
                balance_after = Decimal(str(balance)),
                meta          = {"reason": reason},
            )
            logger.info("[daemon] account %s suspended (fallback): %s", account_id, reason)


def _daemon_close_all(
    pos_list: list,
    account_id: int,
    account,
    prices: dict,
    reason: str,
    pos_fp_map: dict,
    total_floating: float,
) -> tuple:
    """
    DB-first close of ALL positions for one account (stopout or margin_call path).

    Uses running_balance + floating-PnL accumulator for accurate new_equity per close.
    already_closed guard silently skips positions closed by a concurrent consumer.

    Returns (close_records, final_running_balance).
    close_records: list of dicts with pos_id, symbol, side, qty, avg, close_px, realized, result.
    """
    import time as _time
    from market_data.symbol_specs import get_spec
    from . import pnl_engine

    running_balance       = float(account.balance)
    account_currency      = getattr(account, "currency", "USD") or "USD"
    accum_floating_closed = 0.0
    close_records: list   = []

    for pos in pos_list:
        spec       = get_spec(pos.symbol)
        bid, ask   = prices[pos.symbol]
        close_px   = bid if pos.side == "BUY" else ask
        close_px_r = round(close_px, spec.price_decimals)
        avg        = float(pos.avg_price)
        qty        = float(pos.qty)

        # MARGIN-02 — see simulator/pnl_engine.py; same conversion as the
        # WS path, so the daemon's realized PnL can never diverge from it.
        realized = pnl_engine.position_pnl_float(
            pos.side, avg, close_px_r, qty, pos.symbol, account_currency=account_currency,
        )
        realized = round(realized, 8)

        new_balance = running_balance + realized
        fp_p        = pos_fp_map[pos.id]
        remaining_f = total_floating - accum_floating_closed - fp_p
        new_equity  = round(new_balance + remaining_f, 2)

        sl = float(pos.sl) if pos.sl is not None else None
        tp = float(pos.tp) if pos.tp is not None else None
        pos_mem = {
            "id":        pos.id,
            "symbol":    pos.symbol,
            "side":      pos.side.lower(),
            "qty":       qty,
            "avg":       avg,
            "sl":        sl,
            "tp":        tp,
            "opened_at": pos.opened_at.timestamp() if pos.opened_at else _time.time(),
        }

        # SPREAD-02 — daemon closes do NOT apply broker_price() markup (this
        # block does not change that behavior): executable == raw here,
        # honestly reflecting what was actually used for close_px_r.
        # base/account markup are still captured for audit even though not
        # applied — see docs/PRICING_CONTEXT.md.
        pricing_context = _daemon_pricing_context(pos.symbol, bid, ask, profile=reason)

        try:
            result = _close_position_sync(
                pos_mem, account_id, close_px_r, reason, realized, new_balance, new_equity,
                pricing_context=pricing_context, account_currency=account_currency,
            )
        except Exception as exc:
            logger.error("[daemon/%s] close failed pos=%s: %s", reason, pos.id, exc, exc_info=True)
            continue

        if result.get("already_closed"):
            logger.info("[daemon/%s] pos %s already closed (race), skipping", reason, pos.id)
            continue

        running_balance        = result["new_balance"]
        accum_floating_closed += fp_p

        close_records.append({
            "pos_id":   pos.id,
            "symbol":   pos.symbol,
            "side":     pos.side.lower(),
            "qty":      qty,
            "avg":      avg,
            "close_px": close_px_r,
            "realized": realized,
            "result":   result,
        })
        logger.info(
            "[daemon/%s] closed pos=%s sym=%s side=%s px=%s realized=%.2f bal=%.2f",
            reason, pos.id, pos.symbol, pos.side, close_px_r, realized, running_balance,
        )

    return close_records, running_balance


# ──────────────────────────────────────────────────────
# PHASE 2A — Offline SL/TP execution daemon (Step 3)
# Closes positions whose SL/TP has been breached while
# the trader's WebSocket was disconnected.
# Beat schedule added in Step 4 (after WS notification).
# ──────────────────────────────────────────────────────

@shared_task(
    name="simulator.scan_positions",
    bind=True,
    max_retries=0,
    acks_late=True,
    soft_time_limit=25,
    time_limit=29,
)
def scan_positions_task(self) -> dict:
    """
    Offline execution daemon (Steps 3–5).
    Per tick cycle:
      1. Computes offline equity/margin for each account (Step 5).
         If stopout (CHALLENGE/FUNDED) or margin-call (RETAIL) triggered:
         closes ALL positions, suspends if appropriate, notifies via WS group.
      2. For accounts not in stopout/liquidation: checks per-position SL/TP (Steps 3–4).
    All closes are DB-first atomic via _close_position_sync (already_closed guard).
    """
    import time as _time
    from collections import defaultdict
    from market_data.symbol_specs import get_spec
    from .models import Position, AuditLog

    t0 = _time.monotonic()

    positions = list(
        Position.objects
        .filter(account__status="Activo")
        .select_related("account")
        .only(
            "id", "symbol", "side", "qty", "avg_price", "sl", "tp", "opened_at",
            "account__id", "account__balance", "account__equity",
            "account__peak_balance", "account__leverage", "account__tier",
            "account__account_type", "account__netting_mode", "account__status",
        )
        .order_by("account_id", "id")
    )

    if not positions:
        logger.debug("[daemon] no open positions on active accounts")
        return {"scanned": 0, "closed": 0, "skipped_stale": 0, "elapsed_ms": 0}

    by_account: dict = defaultdict(list)
    for pos in positions:
        by_account[pos.account_id].append(pos)

    scanned      = len(positions)
    closed       = 0
    skipped_stale = 0

    for account_id, pos_list in by_account.items():
        # One Redis GET per unique symbol in this account (not per position)
        symbols = {p.symbol for p in pos_list}
        prices: dict = {}
        for sym in symbols:
            bid, ask = _read_cached_price(sym)
            if bid is not None and ask is not None:
                prices[sym] = (bid, ask)

        account         = pos_list[0].account
        running_balance = float(account.balance)

        # ── Step 5: offline stopout / liquidation pre-check ─────────────
        all_prices_available = all(sym in prices for sym in symbols)

        if all_prices_available:
            from .models import MARGIN_ENGINE_TYPES as _MARGIN_ENGINE_TYPES
            equity, margin_used, total_floating, pos_fp_map = _compute_offline_equity_margin(
                pos_list, prices, account
            )
            is_retail         = account.account_type in _MARGIN_ENGINE_TYPES
            stopout_triggered = False
            liq_triggered     = False

            if is_retail:
                if margin_used > 0 and (equity / margin_used * 100.0) < 50.0:
                    liq_triggered = True
            else:
                from .risk_engine import check_equity_stopout as _ces
                stopout_triggered = _ces(
                    equity       = equity,
                    peak_balance = float(account.peak_balance),
                    tier         = getattr(account, "tier", "10K"),
                    account_type = account.account_type,
                    margin_used  = margin_used,
                )

            if stopout_triggered or liq_triggered:
                reason_all = "daemon_stopout" if stopout_triggered else "daemon_margin_call"
                logger.warning(
                    "[daemon/%s] triggered: acc=%s equity=%.2f margin_used=%.2f type=%s",
                    reason_all, account_id, equity, margin_used, account.account_type,
                )
                close_records, final_balance = _daemon_close_all(
                    pos_list, account_id, account, prices,
                    reason_all, pos_fp_map, total_floating,
                )

                # CHALLENGE/FUNDED: ensure suspension even if check_and_enforce_risk
                # didn't trigger on the last close (edge: equity exactly at threshold)
                effective_final_status = (
                    close_records[-1]["result"]["new_status"] if close_records else "Activo"
                )
                if stopout_triggered and effective_final_status != "Suspendido":
                    try:
                        _db_suspend_account_sync(account_id, reason_all, final_balance)
                        effective_final_status = "Suspendido"
                    except Exception as _susp_exc:
                        logger.error("[daemon/%s] suspend fallback failed acc=%s: %s",
                                     reason_all, account_id, _susp_exc)

                # WS notify + AuditLog per closed position
                n = len(close_records)
                for i, rec in enumerate(close_records):
                    closed += 1
                    # Last close carries the authoritative final status
                    ws_status = effective_final_status if (i == n - 1) else rec["result"].get("new_status", "Activo")

                    try:
                        from asgiref.sync import async_to_sync as _a2s
                        from channels.layers import get_channel_layer as _gcl
                        _cl = _gcl()
                        if _cl:
                            _a2s(_cl.group_send)(
                                f"account_{account_id}",
                                {
                                    "type":         "execution.close",
                                    "position_id":  rec["pos_id"],
                                    "symbol":       rec["symbol"],
                                    "side":         rec["side"],
                                    "qty":          rec["qty"],
                                    "avg":          rec["avg"],
                                    "close_px":     rec["close_px"],
                                    "realized_pnl": rec["realized"],
                                    "reason":       reason_all,
                                    "trade_id":     rec["result"].get("trade_id"),
                                    "new_balance":  rec["result"]["new_balance"],
                                    "new_status":   ws_status,
                                    "ts":           int(_time.time()),
                                },
                            )
                    except Exception as _ws_exc:
                        logger.warning("[daemon/%s] WS notify failed pos=%s: %s",
                                       reason_all, rec["pos_id"], _ws_exc)

                    try:
                        AuditLog.objects.create(
                            event_type = f"daemon.{reason_all}",
                            action     = (
                                f"Daemon {reason_all.upper()} close: "
                                f"pos {rec['pos_id']} {rec['symbol']} {rec['side'].upper()}"
                            ),
                            account    = account,
                            detail     = {
                                "position_id":             rec["pos_id"],
                                "symbol":                  rec["symbol"],
                                "side":                    rec["side"],
                                "close_px":                rec["close_px"],
                                "realized_pnl":            rec["realized"],
                                "reason":                  reason_all,
                                "trade_id":                rec["result"].get("trade_id"),
                                "equity_at_trigger":       round(equity, 2),
                                "margin_used_at_trigger":  round(margin_used, 2),
                            },
                        )
                    except Exception as _al_exc:
                        logger.warning("[daemon/%s] AuditLog failed pos=%s: %s",
                                       reason_all, rec["pos_id"], _al_exc)

                continue  # skip the per-position SL/TP loop for this account

        else:
            missing = symbols - prices.keys()
            logger.debug(
                "[daemon] acc=%s skipping stopout/liq check — missing prices: %s",
                account_id, missing,
            )
        # ────────────────────────────────────────────────────────────────

        for pos in pos_list:
            if pos.symbol not in prices:
                skipped_stale += 1
                continue

            bid, ask   = prices[pos.symbol]
            side       = pos.side          # 'BUY' or 'SELL' (DB uppercase)
            trigger_px = bid if side == "BUY" else ask
            close_px   = bid if side == "BUY" else ask

            sl = float(pos.sl) if pos.sl is not None else None
            tp = float(pos.tp) if pos.tp is not None else None

            sl_hit = sl is not None and (
                (side == "BUY"  and trigger_px <= sl) or
                (side == "SELL" and trigger_px >= sl)
            )
            tp_hit = tp is not None and (
                (side == "BUY"  and trigger_px >= tp) or
                (side == "SELL" and trigger_px <= tp)
            )

            if not sl_hit and not tp_hit:
                continue

            reason = "daemon_tp" if tp_hit else "daemon_sl"
            spec   = get_spec(pos.symbol)
            dec    = spec.price_decimals
            close_px_r = round(close_px, dec)

            # MARGIN-02 — see simulator/pnl_engine.py; same formula as
            # every other close path (WS and the Step 5 daemon block above).
            from . import pnl_engine as _pnl_engine
            realized = _pnl_engine.position_pnl_float(
                side, float(pos.avg_price), close_px_r, float(pos.qty), pos.symbol,
                account_currency=getattr(account, "currency", "USD") or "USD",
            )
            realized = round(realized, 8)

            new_balance = running_balance + realized
            new_equity  = new_balance  # simplified: remaining floating not computed in Step 3

            pos_mem = {
                "id":        pos.id,
                "symbol":    pos.symbol,
                "side":      pos.side.lower(),
                "qty":       float(pos.qty),
                "avg":       float(pos.avg_price),
                "sl":        sl,
                "tp":        tp,
                "opened_at": pos.opened_at.timestamp() if pos.opened_at else _time.time(),
            }

            # SPREAD-02 — see _daemon_pricing_context docstring: executable
            # == raw here, no markup applied by this path (unchanged).
            pricing_context = _daemon_pricing_context(pos.symbol, bid, ask, profile=reason)

            try:
                result = _close_position_sync(
                    pos_mem, account_id, close_px_r, reason, realized, new_balance, new_equity,
                    pricing_context=pricing_context,
                    account_currency=getattr(account, "currency", "USD") or "USD",
                )
            except Exception as exc:
                logger.error("[daemon] close failed pos=%s: %s", pos.id, exc, exc_info=True)
                continue

            if result.get("already_closed"):
                logger.info("[daemon] pos %s already closed (race), skipping", pos.id)
                continue

            running_balance = result["new_balance"]
            closed += 1

            # Step 4 — push live WS notification to any connected consumer for this account
            try:
                from asgiref.sync import async_to_sync as _a2s
                from channels.layers import get_channel_layer as _gcl
                _cl = _gcl()
                if _cl:
                    _a2s(_cl.group_send)(
                        f"account_{account_id}",
                        {
                            "type":         "execution.close",
                            "position_id":  pos.id,
                            "symbol":       pos.symbol,
                            "side":         pos.side.lower(),
                            "qty":          float(pos.qty),
                            "avg":          float(pos.avg_price),
                            "close_px":     close_px_r,
                            "realized_pnl": realized,
                            "reason":       reason,
                            "trade_id":     result.get("trade_id"),
                            "new_balance":  result["new_balance"],
                            "new_status":   result.get("new_status", "Activo"),
                            "ts":           int(_time.time()),
                        },
                    )
                    logger.debug("[daemon] WS notify sent account_%s pos=%s", account_id, pos.id)
            except Exception as _ws_exc:
                logger.warning("[daemon] WS notify failed pos=%s: %s", pos.id, _ws_exc)

            try:
                AuditLog.objects.create(
                    event_type = f"daemon.{reason}",
                    action     = f"Daemon {reason.upper()} close: pos {pos.id} {pos.symbol} {pos.side}",
                    account    = account,
                    detail     = {
                        "position_id":  pos.id,
                        "symbol":       pos.symbol,
                        "side":         pos.side,
                        "close_px":     close_px_r,
                        "realized_pnl": realized,
                        "reason":       reason,
                        "trade_id":     result.get("trade_id"),
                    },
                )
            except Exception as exc:
                logger.warning("[daemon] AuditLog failed pos=%s: %s", pos.id, exc)

            logger.info(
                "[daemon] pos=%s sym=%s side=%s close_px=%s realized=%.2f reason=%s bal=%.2f",
                pos.id, pos.symbol, pos.side, close_px_r, realized, reason, running_balance,
            )

    elapsed_ms = round((_time.monotonic() - t0) * 1000)
    logger.info(
        "[daemon] scan done: scanned=%d closed=%d skipped_stale=%d elapsed=%dms worker=%s",
        scanned, closed, skipped_stale, elapsed_ms, self.request.hostname,
    )
    return {
        "scanned":        scanned,
        "closed":         closed,
        "skipped_stale":  skipped_stale,
        "elapsed_ms":     elapsed_ms,
    }


# ──────────────────────────────────────────────────────
# PHASE 2A — Cross-process price cache read validation
# Read-only. No DB writes. No execution logic.
# Remove or keep as smoke test after Phase 2A ships.
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.test_price_cache_read",
    bind=True,
    max_retries=0,
)
def test_price_cache_read(self, symbol: str = "EUR/USD") -> dict:
    """
    Validate that this Celery worker process can read FeedManager-written
    prices from Redis. No DB access, no side effects.
    """
    bid, ask = _read_cached_price(symbol)
    stale = bid is None or ask is None
    logger.info(
        "[test_price_cache_read] symbol=%s bid=%s ask=%s stale=%s worker=%s",
        symbol, bid, ask, stale, self.request.hostname,
    )
    return {
        "symbol": symbol,
        "bid":    bid,
        "ask":    ask,
        "stale":  stale,
    }


# ──────────────────────────────────────────────────────
# PHASE 4B.3 — Challenge periodic evaluator
# Reads ChallengeEnrollment rows, delegates to challenge_engine.
# No execution engine, no consumers, no risk_engine writes.
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.evaluate_all_challenges",
    bind=True,
    max_retries=0,
    acks_late=True,
    soft_time_limit=5 * 60,   # 5 min soft — warn before hard kill
    time_limit=8 * 60,        # 8 min hard kill
)
def evaluate_all_challenges_task(self) -> dict:
    """
    Evaluate every active ChallengeEnrollment (PHASE_1 or PHASE_2).

    Delegates entirely to challenge_engine.evaluate_enrollment_now(), which
    handles atomic state transitions. This task only orchestrates the batch
    and counts outcomes — it never mutates enrollment state directly.

    One enrollment failure never aborts the rest of the batch.
    """
    import time as _t
    from .models import ChallengeEnrollment
    from . import challenge_engine

    t0 = _t.monotonic()
    active_statuses = (ChallengeEnrollment.ST_PHASE_1, ChallengeEnrollment.ST_PHASE_2)
    enrollment_ids = list(
        ChallengeEnrollment.objects
        .filter(status__in=active_statuses)
        .values_list("id", flat=True)
    )

    processed = advanced = failed = errors = 0
    logger.info(
        "[evaluate_challenges] starting batch count=%d worker=%s",
        len(enrollment_ids), self.request.hostname,
    )

    for enrollment_id in enrollment_ids:
        try:
            result = challenge_engine.evaluate_enrollment_now(enrollment_id)
            processed += 1
            if result.status == challenge_engine.PASSED:
                advanced += 1
                logger.info(
                    "[evaluate_challenges] enrollment=%d PASSED — advanced",
                    enrollment_id,
                )
            elif result.status == challenge_engine.FAILED:
                failed += 1
                logger.info(
                    "[evaluate_challenges] enrollment=%d FAILED reason=%r",
                    enrollment_id, result.fail_reason,
                )
            else:
                logger.debug(
                    "[evaluate_challenges] enrollment=%d IN_PROGRESS metrics=%s",
                    enrollment_id, result.metrics,
                )
        except Exception as exc:
            errors += 1
            logger.error(
                "[evaluate_challenges] enrollment=%d unhandled error: %r",
                enrollment_id, exc,
                exc_info=True,
            )

    elapsed_ms = round((_t.monotonic() - t0) * 1000)
    summary = {
        "processed": processed,
        "advanced":  advanced,
        "failed":    failed,
        "errors":    errors,
        "elapsed_ms": elapsed_ms,
    }
    logger.info("[evaluate_challenges] done %s", summary)
    return summary
