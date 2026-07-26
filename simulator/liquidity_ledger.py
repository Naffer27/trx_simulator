# simulator/liquidity_ledger.py
"""
BOOK-05d.2 — Liquidity Ledger writer.

record_liquidity_ledger_entry() is the single write point for
LiquidityLedger — a plain persistence layer, nothing more. It does not
compute simulated_pnl (the caller, BOOK-05d.3, derives it from
Trade.profit_loss with a sign flip), does not resolve a LiquidityDecision
from a Trade (that lookup lives in the caller too — this module never
imports or queries LiquidityDecision beyond storing the id it was
handed), and is completely isolated from consumers.py, tasks.py, and
admin.py — BOOK-05d.2 has no real caller yet; that integration is
BOOK-05d.3a/b/c.

Fail-open by design (docs/BOOK_05_IMPLEMENTATION_PLAN.md, BOOK-05d):
this writer will eventually run inside the same transaction as a real
position close (Trade, LedgerEntry, BrokerLedger, Position deletion,
balance update). A failure here must never be allowed to abort that
transaction — it would revert a real, legitimate close because of a
purely simulated, observational side-write. Same reasoning already
established for routing_engine.record_routing_decision() (BOOK-04a) and
liquidity_engine.record_liquidity_decision() (BOOK-05c); this module
replicates that exact contract, it does not invent a new one.

No get_or_create, no UniqueConstraint — idempotency is deliberately
deferred (FASE 2 architectural decision, approved after verifying the
three real close writers' own already_closed guard already makes a
duplicate row for the same Trade unreachable in the current flow).
"""
import logging
from decimal import Decimal
from typing import Optional

from django.db import transaction

log = logging.getLogger("simulator.liquidity_ledger")


def record_liquidity_ledger_entry(
    *,
    source_trade_id: int,
    symbol: str,
    simulated_pnl,
    liquidity_decision_id: Optional[int] = None,
    meta: Optional[dict] = None,
):
    """
    Writes exactly one LiquidityLedger row via a plain .create() — no
    get_or_create, no uniqueness constraint. Never raises: the
    transaction.atomic() savepoint below is opened and closed entirely
    inside the try block, so any exception it raises (an IntegrityError
    from an invalid FK, or anything else) is caught by the except clause
    OUTSIDE that nested atomic() — never inside it. Catching inside the
    savepoint block itself would risk leaving that savepoint (or the
    caller's own already-open outer transaction) in a broken state;
    catching outside it, after the `with` has already unwound, is what
    guarantees the caller's outer transaction remains fully usable for
    every subsequent operation, regardless of what happened here.

    source_trade_id is mandatory, no default — a LiquidityLedger row
    with no Trade represents nothing (there is no close to settle);
    omitting it fails immediately with TypeError, same discipline
    already used for RoutingDecision.account_id (BOOK-04e) and
    LiquidityDecision's own writer (BOOK-05c).

    liquidity_decision_id defaults to None — the model already allows
    NULL here (no LiquidityDecision existed for this close, e.g. the
    Liquidity Engine flag was off at open time); this is the normal,
    expected case, not a failure.

    meta=None is normalized to {} before the write — never persisted as
    NULL, same pattern as record_liquidity_decision()'s inputs_snapshot.

    simulated_pnl=Decimal("0.00") is a perfectly valid value (a
    break-even close) — no special handling here. This function does
    NOT normalize a "-0.00" artifact; if the caller's own sign-flip
    calculation ever produces one, normalizing it is the caller's
    responsibility (BOOK-05d.3), not this writer's — this module never
    computes simulated_pnl, it only persists the value it is handed.

    No pre-write validation query against Trade or LiquidityDecision —
    this writer trusts the ids it receives, same as
    record_liquidity_decision() trusts routing_decision_id. An invalid
    id surfaces, if at all, as a database-level failure on the INSERT
    itself, handled by the same fail-open contract as any other write
    failure — never a special case.

    Returns the created LiquidityLedger instance, or None if the write
    failed for any reason.
    """
    try:
        from .models import LiquidityLedger

        with transaction.atomic():
            entry = LiquidityLedger.objects.create(
                source_trade_id=source_trade_id,
                liquidity_decision_id=liquidity_decision_id,
                symbol=symbol,
                simulated_pnl=simulated_pnl,
                meta=meta or {},
            )
        log.info(
            "[liquidity_ledger] entry=%s symbol=%s source_trade_id=%s "
            "liquidity_decision_id=%s simulated_pnl=%s",
            entry.id, symbol, source_trade_id, liquidity_decision_id, simulated_pnl,
        )
        return entry
    except Exception as exc:
        log.error(
            "[liquidity_ledger] FAILED to record ledger entry symbol=%s "
            "source_trade_id=%s: %r",
            symbol, source_trade_id, exc, exc_info=True,
        )
        return None
