"""
simulator/routing_engine.py
BOOK-04a — Routing Decision Foundation. BOOK-04b — Shadow Mode Integration.

This module writes RoutingDecision rows and, as of BOOK-04b, builds the
one and only decision contract that exists today — a trivial,
deterministic constant (`book=Book.INTERNAL`, always). No function here
inspects a trader's history, TraderScore, or routing_profile, or picks
between INTERNAL/EXTERNAL based on any real signal — that is explicitly
out of scope for this entire plan (docs/BOOK_04_IMPLEMENTATION_PLAN.md)
and belongs to a future block, outside BOOK-04a..04f.

Design discipline carried over from broker_audit.py's record_event(),
per BOOK-04a's own scope ("mismo contrato que record_event()"):
    - record_routing_decision() never raises — a routing-audit write
      failure must never block anything, exactly like record_event().
      Fail-open is the Audit Trail Engine's non-negotiable default and
      this module inherits it from day one.
    - Writes inside a nested savepoint (transaction.atomic()) so a
      failure here can never poison an outer atomic() block a caller
      may already be inside — proven empirically for BOOK-04a and
      relied upon unchanged by BOOK-04b's real caller,
      _db_open_position_atomic() (consumers.py).
    - FK-like parameters accept either the ORM object or its id (same
      as record_event()'s account/account_id, trade/trade_id, ...) so a
      caller that already holds the object avoids an extra query.

BOOK-04b is Shadow Mode only: the single real caller
(TradingConsumer._db_open_position_atomic, gated by
settings.ROUTING_ENGINE_ENABLED) always builds the same trivial
contract via build_shadow_mode_decision_contract() below. Nothing here
writes to Trade, BrokerLedger, or BrokerAuditEvent, and nothing here
reads TraderScore or routing_profile.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from typing import Optional

from django.db import transaction

log = logging.getLogger("simulator.routing_engine")

_SOURCE = "simulator.routing_engine"


# ─────────────────────────────────────────────────────────────────────────
# Book — plain, open string (no `choices=` at the DB level), same pattern
# as broker_audit.py's Category: a future book value never requires a
# migration to introduce.
# ─────────────────────────────────────────────────────────────────────────
class Book:
    INTERNAL = "INTERNAL"

    ALL = (INTERNAL,)


# ─────────────────────────────────────────────────────────────────────────
# Dual versioning (FASE 3) — see RoutingDecision's class docstring for the
# full rationale. Both start at 1; the Shadow Mode decision BOOK-04b
# introduces is still the trivial v1 — neither axis has ever been bumped.
# ─────────────────────────────────────────────────────────────────────────
ENGINE_VERSION = 1
SCHEMA_VERSION = 1

# BOOK-04b — reason_code for the one decision Shadow Mode ever produces.
# A plain string constant (like Book), not a DB-level choice — a future
# real reason_code never requires a migration to introduce.
REASON_TRIVIAL_INTERNAL_DEFAULT = "TRIVIAL_INTERNAL_DEFAULT"


def record_routing_decision(
    *,
    book: str,
    reason_code: str,
    account_id: int,
    reason_message: str = "",
    engine_version: int = ENGINE_VERSION,
    schema_version: int = SCHEMA_VERSION,
    inputs_snapshot: Optional[dict] = None,
    external_reference: str = "",
    position_id: Optional[int] = None,
    position=None,
    parent_decision_id: Optional[int] = None,
    parent_decision=None,
    override_by_id: Optional[int] = None,
    override_by=None,
    override_reason: str = "",
    correlation_id=None,
):
    """
    Writes exactly one RoutingDecision row. Never raises — a failure
    here is logged and swallowed, never allowed to block or roll back
    the caller's own transaction (matches broker_audit.py's
    record_event() contract exactly).

    Writes inside a nested savepoint (transaction.atomic()) so a
    failure here can never poison an outer atomic() block the caller
    may already be inside.

    position/position_id (BOOK-04b): the Position this specific decision
    was taken for — a brand-new Position, or the existing one an order
    merged into. Optional (defaults to unlinked) so BOOK-04a's original,
    zero-integration callers keep working unchanged.

    account_id (BOOK-04e): mandatory, no default — a caller that omits it
    fails immediately with TypeError rather than silently creating a row
    with an implicit account. Stored directly as the id already handed
    in; no query is made to resolve or validate the TradingAccount.

    Returns the created RoutingDecision, or None if the write failed.
    """
    try:
        from .models import RoutingDecision

        with transaction.atomic():
            decision = RoutingDecision.objects.create(
                decision_id=_uuid.uuid4(),
                book=book,
                reason_code=reason_code,
                reason_message=reason_message,
                engine_version=engine_version,
                schema_version=schema_version,
                inputs_snapshot=inputs_snapshot or {},
                external_reference=external_reference,
                account_id=account_id,
                position_id=(
                    position.id if position is not None else position_id
                ),
                parent_decision_id=(
                    parent_decision.id if parent_decision is not None else parent_decision_id
                ),
                override_by_id=(
                    override_by.id if override_by is not None else override_by_id
                ),
                override_reason=override_reason,
                correlation_id=correlation_id,
            )
        log.info(
            "[routing_engine] decision=%s book=%s reason_code=%s account_id=%s position_id=%s "
            "engine_version=%s schema_version=%s",
            decision.decision_id, book, reason_code, account_id, decision.position_id,
            engine_version, schema_version,
        )
        return decision
    except Exception as exc:
        log.error("[routing_engine] FAILED to record decision book=%s reason_code=%s: %r",
                   book, reason_code, exc, exc_info=True)
        return None


def routing_decisions_for_account(account_id: int):
    """
    BOOK-04e — direct query on the denormalized `account` FK, same mold
    as broker_audit.py's events_for_account(). Works identically for
    decisions tied to open and closed Positions, because `account` never
    depends on Position's lifecycle (unlike `position`, which is
    SET_NULL'd away on every real close — see routing_decisions_for_position
    below). No JOIN through Position/Trade/BrokerAuditEvent.
    """
    from .models import RoutingDecision

    return RoutingDecision.objects.filter(account_id=account_id)


def routing_decisions_for_position(position_id: int):
    """
    BOOK-04e — direct query on `position`. Useful only while the
    Position is still open: RoutingDecision.position is SET_NULL on
    every real close (see RoutingDecision's docstring in models.py), so
    this returns an empty queryset for a closed/deleted Position by
    design, not by defect. Durable, close-surviving lookups must use
    routing_decisions_for_account() instead.
    """
    from .models import RoutingDecision

    return RoutingDecision.objects.filter(position_id=position_id)


def build_shadow_mode_decision_contract(*, symbol: str, side: str, qty, merged: bool) -> dict:
    """
    BOOK-04b — the single, trivial, deterministic decision contract
    Shadow Mode ever produces: always `book=Book.INTERNAL`, always the
    same reason_code. Pure function, no DB access, no exception expected
    from normal inputs — the caller (TradingConsumer._db_open_position_
    atomic) is responsible for wrapping this alongside the writer call in
    its own try/except (see that call site's docstring for why: this
    module's own fail-open only covers record_routing_decision()'s
    internal write, not the caller's contract-construction step).

    inputs_snapshot is deliberately minimal — mechanical facts about this
    order only (symbol/side/qty/merged). No TraderScore, no
    routing_profile, no classification of the trader — introducing any
    of that is explicitly out of scope for every block in this plan
    (BOOK-04a..04f).

    Returns a dict of kwargs ready to pass to record_routing_decision()
    via **contract (position_id/position still need to be added by the
    caller once known — see BOOK-04b's approved sequencing).
    """
    return {
        "book": Book.INTERNAL,
        "reason_code": REASON_TRIVIAL_INTERNAL_DEFAULT,
        "reason_message": (
            "Shadow Mode: no real routing rule is active yet — every order "
            "is routed 100% internal by design (BOOK-04b)."
        ),
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "inputs_snapshot": {
            "symbol": symbol,
            "side": side,
            "qty": float(qty),
            "merged": merged,
        },
        "external_reference": "",
        "parent_decision": None,
        "override_by": None,
        "override_reason": "",
        "correlation_id": None,
    }
