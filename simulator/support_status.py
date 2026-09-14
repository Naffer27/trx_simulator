# simulator/support_status.py
"""
CUSTOMER-SUPPORT-01B — centralized SupportTicket status-transition
legality + mutation. The single place that decides "is this status
change legal, and who's allowed to perform it" — no view in
support_panel_views.py mutates SupportTicket.status directly; every one
of them (reply, status, escalate) goes through apply_transition().

Legacy STATUS_PENDING (Design Lock Correction 1) participates as a
source state everywhere STATUS_PENDING_SUPPORT does — it behaves
exactly like PENDING_SUPPORT for legality purposes — but it can NEVER
be a transition destination: no row in the table below targets it, and
apply_transition() rejects any attempt to target it explicitly before
even consulting the table.
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone


class IllegalTransition(Exception):
    """The (from_status, to_status) pair does not exist in the legality table at all."""


def _support_or_ops(user):
    from .permission_levels import is_customer_support, is_ops_admin
    return is_customer_support(user) or is_ops_admin(user)


def _ops_only(user):
    from .permission_levels import is_ops_admin
    return is_ops_admin(user)


def _transitions():
    """
    Built lazily (function call, not a module-level constant) to avoid
    any import-order coupling with models.py. Each row:
    (source statuses, destination status, actor-eligibility check).
    """
    from .models import SupportTicket as T

    return [
        ({T.STATUS_OPEN}, T.STATUS_PENDING_CUSTOMER, _support_or_ops),
        ({T.STATUS_PENDING, T.STATUS_PENDING_SUPPORT}, T.STATUS_PENDING_CUSTOMER, _support_or_ops),
        # Customer-reply path — not wired to any view in 01B (no
        # customer thread UI yet), but the legality rule already exists
        # so 01C's customer-reply handler can reuse this same helper.
        ({T.STATUS_PENDING_CUSTOMER}, T.STATUS_PENDING_SUPPORT, _support_or_ops),
        ({T.STATUS_OPEN, T.STATUS_PENDING, T.STATUS_PENDING_SUPPORT}, T.STATUS_ESCALATED, _support_or_ops),
        ({T.STATUS_ESCALATED}, T.STATUS_PENDING_SUPPORT, _ops_only),
        ({T.STATUS_ESCALATED}, T.STATUS_RESOLVED, _ops_only),
        (
            {T.STATUS_OPEN, T.STATUS_PENDING, T.STATUS_PENDING_SUPPORT, T.STATUS_PENDING_CUSTOMER},
            T.STATUS_RESOLVED, _support_or_ops,
        ),
        ({T.STATUS_RESOLVED}, T.STATUS_CLOSED, _support_or_ops),
        ({T.STATUS_CLOSED}, T.STATUS_OPEN, _support_or_ops),
    ]


def legal_next_statuses(ticket, actor):
    """
    Read-only: the set of statuses *actor* is currently allowed to move
    *ticket* into. Used by the detail view to render only buttons that
    would actually succeed — never mutates anything.
    """
    return {
        to_state
        for from_states, to_state, allowed in _transitions()
        if ticket.status in from_states and allowed(actor)
    }


def apply_transition(ticket, to_status, *, actor, extra_fields=None):
    """
    Validate and perform ticket.status -> to_status for *actor*.

    extra_fields (optional dict) is set on the ticket in the SAME
    save() call as the status change — this is what lets the escalate
    view set escalated_to_ops/escalated_at/escalated_by/
    escalation_reason atomically together with status=ESCALATED,
    without a second write.

    Raises:
      IllegalTransition — the (from, to) pair isn't in the table at all
        (this includes every attempt to target legacy PENDING, which
        never appears as a destination in any row).
      PermissionDenied — the pair is legal in principle, but *actor*
        doesn't hold the required role for it (e.g. Support attempting
        ESCALATED -> RESOLVED).
    """
    from .models import SupportTicket as T

    if to_status == T.STATUS_PENDING:
        raise IllegalTransition("PENDING is a legacy status and can never be a transition target.")

    from_status = ticket.status
    matched = False
    for from_states, dest, allowed in _transitions():
        if dest != to_status or from_status not in from_states:
            continue
        matched = True
        if not allowed(actor):
            raise PermissionDenied(
                f"Not authorized to transition ticket #{ticket.pk} from {from_status} to {to_status}."
            )
        break

    if not matched:
        raise IllegalTransition(f"{from_status} -> {to_status} is not a legal transition.")

    now = timezone.now()
    for field, value in (extra_fields or {}).items():
        setattr(ticket, field, value)

    ticket.status = to_status
    if to_status == T.STATUS_RESOLVED:
        ticket.resolved_at = now
    elif to_status == T.STATUS_CLOSED:
        ticket.closed_at = now
    elif from_status == T.STATUS_CLOSED and to_status == T.STATUS_OPEN:
        # Reopen: clear closed_at, but deliberately PRESERVE the
        # historical resolved_at timestamp (Design Lock: "prefer
        # preserving historical timestamp unless necessary").
        ticket.closed_at = None

    with transaction.atomic():
        ticket.save()

    return ticket
