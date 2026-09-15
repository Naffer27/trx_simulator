# simulator/support_status.py
"""
CUSTOMER-SUPPORT-01B/01C — centralized SupportTicket status-transition
legality + mutation. The single place that decides "is this status
change legal, and who's allowed to perform it" — no view (staff-side in
support_panel_views.py, customer-side in views.py) mutates
SupportTicket.status directly; every one of them goes through
apply_transition().

Legacy STATUS_PENDING (Design Lock Correction 1) participates as a
source state everywhere STATUS_PENDING_SUPPORT does — it behaves
exactly like PENDING_SUPPORT for legality purposes — but it can NEVER
be a transition destination: no row in the table below targets it, and
apply_transition() rejects any attempt to target it explicitly before
even consulting the table.

CUSTOMER-SUPPORT-01C adds CLIENT-eligible rows (_client_own). A row's
eligibility check now takes (ticket, actor) rather than just (actor),
since "is this actor allowed" depends on ticket ownership for client
rows. Multiple rows may share the same (from-status-membership,
destination) pair with DIFFERENT eligibility checks (e.g. CLOSED ->
OPEN is legal for both staff, via the 01B row, and the ticket's own
client, via the 01C row) — apply_transition()/legal_next_statuses()
both evaluate ALL matching rows for a given (from, to) pair and permit
the action if ANY one of them authorizes the actor (OR semantics),
raising PermissionDenied only when at least one row structurally
matches but none of them authorize this specific actor.
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone


class IllegalTransition(Exception):
    """No row in the table matches this (from_status, to_status) pair at all."""


def _support_or_ops(ticket, actor):
    from .permission_levels import is_customer_support, is_ops_admin
    return is_customer_support(actor) or is_ops_admin(actor)


def _ops_only(ticket, actor):
    from .permission_levels import is_ops_admin
    return is_ops_admin(actor)


def _client_own(ticket, actor):
    """
    True only for the ticket's own owner. The view layer additionally
    guarantees this via get_object_or_404(SupportTicket, pk=pk,
    user=request.user) before apply_transition() is ever called on the
    customer-facing routes — this check is defense in depth, not the
    only guard against acting on someone else's ticket.
    """
    return getattr(actor, "pk", None) == ticket.user_id


def _transitions():
    """
    Built lazily (function call, not a module-level constant) to avoid
    any import-order coupling with models.py. Each row:
    (source statuses, destination status, actor-eligibility check).
    """
    from .models import SupportTicket as T

    return [
        # ── 01B — staff/ops/owner rows ──────────────────────────────
        ({T.STATUS_OPEN}, T.STATUS_PENDING_CUSTOMER, _support_or_ops),
        ({T.STATUS_PENDING, T.STATUS_PENDING_SUPPORT}, T.STATUS_PENDING_CUSTOMER, _support_or_ops),
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

        # ── 01C — client-eligible rows ──────────────────────────────
        # A customer reply on OPEN/legacy-PENDING/PENDING_CUSTOMER means
        # "the customer just spoke, now it's waiting on staff again" —
        # PENDING_SUPPORT. (PENDING_SUPPORT itself needs no row: a reply
        # while already PENDING_SUPPORT is a status no-op — the view
        # simply skips calling apply_transition() in that case.
        # ESCALATED is deliberately absent here too: an Ops-owned ticket
        # stays ESCALATED regardless of who replies, staff or client.)
        ({T.STATUS_OPEN, T.STATUS_PENDING, T.STATUS_PENDING_CUSTOMER}, T.STATUS_PENDING_SUPPORT, _client_own),
        # Customer reopen — via an explicit reopen action, or implicitly
        # by replying to a RESOLVED/CLOSED ticket ("the issue persists"
        # / "I need to reopen this"). Always lands on OPEN, never
        # PENDING_SUPPORT directly, per the Design Lock.
        ({T.STATUS_RESOLVED, T.STATUS_CLOSED}, T.STATUS_OPEN, _client_own),
        # Customer close — every state except ESCALATED (an escalated
        # ticket is Ops-owned; the customer closing it out from under
        # Ops would bypass their handling of it, so it's explicitly
        # excluded here — there is no row taking ESCALATED to CLOSED
        # for ANY actor, staff included, which is intentional: staff
        # must resolve it first, a proper two-step path).
        (
            {T.STATUS_OPEN, T.STATUS_PENDING, T.STATUS_PENDING_CUSTOMER, T.STATUS_PENDING_SUPPORT, T.STATUS_RESOLVED},
            T.STATUS_CLOSED, _client_own,
        ),
    ]


def _matching_rows(from_status, to_status):
    return [
        (from_states, dest, allowed)
        for from_states, dest, allowed in _transitions()
        if dest == to_status and from_status in from_states
    ]


def legal_next_statuses(ticket, actor):
    """
    Read-only: the set of statuses *actor* is currently allowed to move
    *ticket* into. Used by both detail views (staff and customer) to
    render only buttons/actions that would actually succeed — never
    mutates anything.
    """
    return {
        to_state
        for from_states, to_state, allowed in _transitions()
        if ticket.status in from_states and allowed(ticket, actor)
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
      IllegalTransition — no row matches this (from, to) pair at all
        (this includes every attempt to target legacy PENDING, which
        never appears as a destination in any row, and e.g. an attempt
        to close an ESCALATED ticket directly).
      PermissionDenied — at least one row matches the pair, but *actor*
        doesn't satisfy ANY of the matching rows' eligibility checks
        (e.g. Support attempting ESCALATED -> RESOLVED, or a client
        attempting a transition on a ticket they don't own).
    """
    from .models import SupportTicket as T

    if to_status == T.STATUS_PENDING:
        raise IllegalTransition("PENDING is a legacy status and can never be a transition target.")

    from_status = ticket.status
    rows = _matching_rows(from_status, to_status)
    if not rows:
        raise IllegalTransition(f"{from_status} -> {to_status} is not a legal transition.")

    if not any(allowed(ticket, actor) for _, _, allowed in rows):
        raise PermissionDenied(
            f"Not authorized to transition ticket #{ticket.pk} from {from_status} to {to_status}."
        )

    now = timezone.now()
    for field, value in (extra_fields or {}).items():
        setattr(ticket, field, value)

    ticket.status = to_status
    if to_status == T.STATUS_RESOLVED:
        ticket.resolved_at = now
    elif to_status == T.STATUS_CLOSED:
        ticket.closed_at = now
    elif from_status == T.STATUS_CLOSED and to_status == T.STATUS_OPEN:
        # Reopen from CLOSED: clear closed_at, but deliberately PRESERVE
        # the historical resolved_at timestamp (Design Lock: "prefer
        # preserving historical timestamp unless necessary"). A direct
        # RESOLVED -> OPEN reopen never touched closed_at in the first
        # place, so no branch is needed for that case — it's already a
        # no-op on that field.
        ticket.closed_at = None

    with transaction.atomic():
        ticket.save()

    return ticket
