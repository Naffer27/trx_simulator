"""
simulator/routing_engine.py
BOOK-04a — Routing Decision Foundation.

Infrastructure only. This module writes RoutingDecision rows; it does
not decide anything. No function here inspects an order, a Position, a
TraderScore, or any other trading state and picks a book — that
decision logic is explicitly out of scope for BOOK-04a (see
docs/BOOK_04_IMPLEMENTATION_PLAN.md) and lands in BOOK-04b, wired to
the single real integration point (_db_open_position_atomic) under its
own Shadow Mode flag, not here.

Design discipline carried over from broker_audit.py's record_event(),
per BOOK-04a's own scope ("mismo contrato que record_event()"):
    - record_routing_decision() never raises — a routing-audit write
      failure must never block anything, exactly like record_event().
      Fail-open is the Audit Trail Engine's non-negotiable default and
      this module inherits it from day one, before any real caller
      exists.
    - Writes inside a nested savepoint (transaction.atomic()) so a
      failure here can never poison an outer atomic() block a future
      caller may already be inside.
    - FK-like parameters accept either the ORM object or its id (same
      as record_event()'s account/account_id, trade/trade_id, ...) so a
      caller that already holds the object avoids an extra query.

Zero integration with consumers.py, TradingConsumer, Position, Trade,
BrokerLedger, or BrokerAuditEvent. Nothing in this module is imported
by any of those today.
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
# full rationale. Both start at 1; BOOK-04a introduces no decision logic
# and no snapshot shape yet, so neither has ever been bumped.
# ─────────────────────────────────────────────────────────────────────────
ENGINE_VERSION = 1
SCHEMA_VERSION = 1


def record_routing_decision(
    *,
    book: str,
    reason_code: str,
    reason_message: str = "",
    engine_version: int = ENGINE_VERSION,
    schema_version: int = SCHEMA_VERSION,
    inputs_snapshot: Optional[dict] = None,
    external_reference: str = "",
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
            "[routing_engine] decision=%s book=%s reason_code=%s engine_version=%s schema_version=%s",
            decision.decision_id, book, reason_code, engine_version, schema_version,
        )
        return decision
    except Exception as exc:
        log.error("[routing_engine] FAILED to record decision book=%s reason_code=%s: %r",
                   book, reason_code, exc, exc_info=True)
        return None
