# simulator/broker_ledger.py
"""
BOOK-02 — single entry point for persisting the broker's B-Book counterparty
result of a closed Trade.

Trade.profit_loss / LedgerEntry(EV_REALIZED) already record the TRADER's
side of a close correctly (pre-existing, unchanged by this module). What
was missing (see BOOK-01 audit) is the mirror fact on the broker's side:
today every trade is executed B-Book (no A-Book/hedge/LP exists), so the
broker is the trader's direct counterparty —

    broker_counterparty_pnl = -trader_pnl

This module writes exactly that, as one BrokerLedger row per Trade, and
nothing else. It does not touch Trade, LedgerEntry, commission, spread,
margin, leverage, or TraderScore — those are all unchanged.
"""

from decimal import Decimal

from .models import BrokerLedger

BOOK_MODE_B_BOOK = "B_BOOK"

# Bump if `meta`'s shape ever changes, so downstream readers (BOOK-03+) can
# branch on old vs new rows instead of guessing from field presence.
_SCHEMA_VERSION = 1


def create_broker_counterparty_entry(trade, account, trader_pnl, reason, *, book_mode=BOOK_MODE_B_BOOK):
    """
    Idempotently record the broker's counterparty result for one closed Trade.

    Must be called from inside the SAME transaction.atomic() block that
    created `trade` (and its LedgerEntry) — this is a fact about that one
    close, not a separately-reconciled side effect. Call this exactly once
    per genuinely-new Trade; never call it for an `already_closed=True`
    result (no Trade was created in that case — there is nothing to link).

    Always creates a row, including break-even (trader_pnl == 0 ->
    amount=Decimal("0.00")). A closed Trade is a fact regardless of its
    PnL; every Trade must have exactly one linked COUNTERPARTY_PNL entry,
    so source_trade reliably identifies "this Trade has been reconciled
    against the broker book" with no special-cased gap at zero. Revenue
    dashboards/snapshots that must ignore this entry type filter on
    revenue_type, not on amount != 0 — a zero amount does not change any
    of those sums either way.

    Idempotency: keyed on (source_trade, revenue_type=COUNTERPARTY_PNL),
    enforced by a DB UniqueConstraint (migration 0048). Uses
    get_or_create so a retry/replay of this exact call is a no-op rather
    than a duplicate row or a hard failure — but the real guarantee is
    upstream: Trade is only ever created once per close (the DB-locked
    atomic close paths return early with already_closed=True and create
    no new Trade on a race), so source_trade is never reused across two
    different close attempts for the same Position.

    Returns the BrokerLedger row (always — never None).
    """
    trader_pnl_d = trader_pnl if isinstance(trader_pnl, Decimal) else Decimal(str(trader_pnl))
    # Normalize away -0 (both a literal -0 input and -Decimal("0.00") below)
    # so amount is always a clean Decimal("0.00") at break-even, never "-0.00".
    if trader_pnl_d == 0:
        trader_pnl_d = Decimal("0.00")
    counterparty_pnl = Decimal("0.00") if trader_pnl_d == 0 else -trader_pnl_d

    entry, _created = BrokerLedger.objects.get_or_create(
        source_trade=trade,
        revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL,
        defaults={
            "amount": counterparty_pnl,
            "source_account": account,
            "symbol": trade.symbol,
            "meta": {
                "trader_pnl": float(trader_pnl_d),
                "broker_counterparty_pnl": float(counterparty_pnl),
                "account_type": getattr(account, "account_type", None),
                "book_mode": book_mode,
                "close_reason": reason,
                "schema_version": _SCHEMA_VERSION,
            },
        },
    )

    # AUDIT-01 — this single writer is called from all four real close
    # paths (WebSocket, daemon, admin force-close, population engine),
    # so it is also the single integration point for both "Position
    # CLOSE" and "Ledger entry importante" — deliberately recorded as
    # ONE audit event, not two, since they are the same real-world
    # moment. Only on a genuine new row: a get_or_create() replay
    # (_created=False) must never produce a second audit event for the
    # same close (FASE 9 — sin duplicados).
    if _created:
        from . import broker_audit as _audit
        _audit.record_trade_event(
            event_type=_audit.close_reason_event_type(reason),
            actor_type=(
                _audit.ActorType.STAFF if reason == "admin_force_close"
                else _audit.ActorType.SYSTEM if (reason or "").startswith(("daemon_", "stopout"))
                else _audit.ActorType.TRADER
            ),
            description=f"Position closed on {trade.symbol} ({reason}) — trader PnL {trader_pnl_d}",
            account=account, trade=trade, symbol=trade.symbol,
            source_module="simulator.broker_ledger",
            metadata={
                "reason": reason,
                "trader_pnl": float(trader_pnl_d),
                "broker_counterparty_pnl": float(counterparty_pnl),
                "book_mode": book_mode,
            },
        )

    return entry
