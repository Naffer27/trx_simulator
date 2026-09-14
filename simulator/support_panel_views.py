# simulator/support_panel_views.py
"""
CUSTOMER-SUPPORT-01B — dedicated Support Panel, entirely OUTSIDE
/admin/. Every view here is gated by support_panel_required(), which
authorizes exclusively through the existing permission_levels.py
hierarchy (is_owner_root / is_ops_admin / is_customer_support) —
NEVER user.is_staff. This is the concrete fix for the Phase A audit's
core finding: a Support agent must never need Django Admin access to
do their job.

Explicitly, structurally, this module:
  - never imports owner_actions.py, wallet_ledger.py's write side,
    treasury_requests.py, or any Wallet/TradingAccount/OwnerRoot/
    OpsAdminProfile admin class.
  - never creates or references a TreasuryOperationRequest,
    ManualBalanceAdjustment, or OwnerWalletAdjustment.
  - performs no financial action of any kind. Escalation to Ops is a
    SupportTicket field flip + one status transition — nothing more.

AuditLog instrumentation is DELIBERATELY DEFERRED to CUSTOMER-SUPPORT-01F
per the phased Design Lock (01F is the dedicated audit/SLA polish
block) — adding it here would be scope expansion beyond what 01B
authorizes. This is a conscious choice, not an oversight; see the
implementation report for the explicit call-out.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import SupportTicket, SupportMessage
from .support_status import apply_transition, IllegalTransition, legal_next_statuses

User = get_user_model()


def support_panel_required(view_fn):
    """
    The single access gate for every /staff/support/ view. Authorization
    comes exclusively from permission_levels.py's existing hierarchy —
    never user.is_staff, which is deliberately insufficient here.

    Anonymous -> redirected to login (via @login_required).
    Authenticated but none of Owner/Ops/Support -> PermissionDenied
    (Django's default 403 handling), same pattern this codebase already
    uses at the service layer (owner_actions.py).
    """
    @wraps(view_fn)
    @login_required
    def wrapper(request, *args, **kwargs):
        from .permission_levels import is_customer_support, is_ops_admin, is_owner_root

        if not (is_owner_root(request.user) or is_ops_admin(request.user) or is_customer_support(request.user)):
            raise PermissionDenied("Support Panel access requires Owner, Ops, or Support authorization.")
        return view_fn(request, *args, **kwargs)
    return wrapper


def _is_assignable(user) -> bool:
    """
    Assignable-user validation — Owner, Ops, or Support ONLY, checked
    via the exact same canonical functions the access guard uses. Never
    trusts a posted user id beyond resolving it to a real User row
    first; this is the actual authorization check on that row.
    """
    from .permission_levels import is_customer_support, is_ops_admin, is_owner_root
    return is_owner_root(user) or is_ops_admin(user) or is_customer_support(user)


def _assignable_users():
    return [u for u in User.objects.filter(is_active=True).order_by("username") if _is_assignable(u)]


# ── Queue ────────────────────────────────────────────────────────────────

_QUEUE_FILTERS = {
    "unassigned":       lambda qs, user: qs.filter(assigned_to__isnull=True),
    "mine":             lambda qs, user: qs.filter(assigned_to=user),
    "open":             lambda qs, user: qs.filter(status=SupportTicket.STATUS_OPEN),
    "waiting_customer": lambda qs, user: qs.filter(status=SupportTicket.STATUS_PENDING_CUSTOMER),
    "waiting_support":  lambda qs, user: qs.filter(
        status__in=(SupportTicket.STATUS_PENDING, SupportTicket.STATUS_PENDING_SUPPORT),
    ),
    "escalated":        lambda qs, user: qs.filter(status=SupportTicket.STATUS_ESCALATED),
    "urgent":           lambda qs, user: qs.filter(priority=SupportTicket.PRIORITY_URGENT),
    "resolved_closed":  lambda qs, user: qs.filter(
        status__in=(SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED),
    ),
    "all":              lambda qs, user: qs,
}

_QUEUE_FILTER_LABELS = [
    ("mine",             "My tickets"),
    ("unassigned",       "Unassigned"),
    ("open",             "Open"),
    ("waiting_customer", "Waiting for customer"),
    ("waiting_support",  "Waiting for support"),
    ("escalated",        "Escalated"),
    ("urgent",           "Urgent"),
    ("resolved_closed",  "Resolved/Closed"),
    ("all",              "All"),
]


@support_panel_required
def support_panel_queue_view(request):
    """
    GET /staff/support/ — the ticket queue. OWNER/OPS/SUPPORT all see
    the same global ticket set (per the locked permission matrix —
    queue visibility is not role-restricted); only mutating actions
    are role-restricted, enforced in the action views below.
    """
    qs = SupportTicket.objects.select_related("user", "assigned_to").order_by("-created_at")

    active_filter = request.GET.get("filter", "").strip()
    if active_filter not in _QUEUE_FILTERS:
        # Default: "My tickets" if the actor has any assigned ticket,
        # else "Unassigned".
        active_filter = "mine" if qs.filter(assigned_to=request.user).exists() else "unassigned"

    qs = _QUEUE_FILTERS[active_filter](qs, request.user)

    category = request.GET.get("category", "").strip()
    priority = request.GET.get("priority", "").strip()
    status = request.GET.get("status", "").strip()
    assigned = request.GET.get("assigned", "").strip()

    if category:
        qs = qs.filter(category=category)
    if priority:
        qs = qs.filter(priority=priority)
    if status:
        qs = qs.filter(status=status)
    if assigned:
        qs = qs.filter(assigned_to_id=assigned)

    return render(request, "simulator/support_panel/queue.html", {
        "tickets": qs[:200],
        "active_filter": active_filter,
        "filters": _QUEUE_FILTER_LABELS,
        "category_choices": SupportTicket.CATEGORY_CHOICES,
        "priority_choices": SupportTicket.PRIORITY_CHOICES,
        "status_choices": SupportTicket.STATUS_CHOICES,
        "assignable_users": _assignable_users(),
        "selected_category": category,
        "selected_priority": priority,
        "selected_status": status,
        "selected_assigned": assigned,
        "active_section": "support_panel",
    })


# ── Detail ───────────────────────────────────────────────────────────────

@support_panel_required
def support_panel_ticket_detail_view(request, pk):
    """
    GET /staff/support/tickets/<pk>/ — full ticket + thread. The thread
    is SupportTicket.message rendered as a synthetic, always-first,
    read-only entry, followed by real SupportMessage rows in
    chronological order (Design Lock: Option A — no data migration of
    the historical message ever happens). Support/Ops/Owner see both
    CUSTOMER_VISIBLE and INTERNAL rows.
    """
    from .permission_levels import is_ops_admin

    ticket = get_object_or_404(
        SupportTicket.objects.select_related("user", "assigned_to", "escalated_by"), pk=pk,
    )

    thread = [{
        "kind": "original",
        "author": ticket.user,
        "author_role": SupportMessage.ROLE_CLIENT,
        "body": ticket.message,
        "visibility": SupportMessage.VISIBILITY_CUSTOMER,
        "created_at": ticket.created_at,
    }]
    for m in ticket.messages.select_related("author").order_by("created_at"):
        thread.append({
            "kind": "message",
            "author": m.author,
            "author_role": m.author_role,
            "body": m.body,
            "visibility": m.visibility,
            "created_at": m.created_at,
        })

    actor_is_ops = is_ops_admin(request.user)
    status_labels = dict(SupportTicket.STATUS_CHOICES)
    next_statuses = [
        (status, status_labels[status])
        for status in legal_next_statuses(ticket, request.user)
    ]

    return render(request, "simulator/support_panel/detail.html", {
        "ticket": ticket,
        "thread": thread,
        "actor_is_ops": actor_is_ops,
        "is_assigned_to_me": ticket.assigned_to_id == request.user.pk,
        "assignable_users": _assignable_users(),
        "legal_next_statuses": next_statuses,
        "active_section": "support_panel",
    })


# ── Reply (CUSTOMER_VISIBLE) ────────────────────────────────────────────

@support_panel_required
@require_POST
def support_panel_reply_view(request, pk):
    from .permission_levels import permission_level

    ticket = get_object_or_404(SupportTicket, pk=pk)
    body = request.POST.get("body", "").strip()
    if not body:
        messages.error(request, "El mensaje no puede estar vacío.")
        return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)

    SupportMessage.objects.create(
        ticket=ticket, author=request.user,
        author_role=permission_level(request.user).value,
        body=body, visibility=SupportMessage.VISIBILITY_CUSTOMER,
    )

    if ticket.first_response_at is None:
        ticket.first_response_at = timezone.now()
        ticket.save(update_fields=["first_response_at"])

    # Status rule: OPEN / legacy PENDING / PENDING_SUPPORT -> PENDING_CUSTOMER
    # after a staff reply. Deliberately NOT applied from any other
    # status (ESCALATED included) — an ESCALATED ticket stays ESCALATED
    # regardless of who replies, per the locked matrix; Support gains no
    # ability to change Ops ownership of an escalated ticket via reply.
    if ticket.status in (SupportTicket.STATUS_OPEN, SupportTicket.STATUS_PENDING, SupportTicket.STATUS_PENDING_SUPPORT):
        apply_transition(ticket, SupportTicket.STATUS_PENDING_CUSTOMER, actor=request.user)

    return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)


# ── Internal note ────────────────────────────────────────────────────────

@support_panel_required
@require_POST
def support_panel_note_view(request, pk):
    """POST an INTERNAL SupportMessage. Never customer-visible, never
    emailed, never changes status."""
    from .permission_levels import permission_level

    ticket = get_object_or_404(SupportTicket, pk=pk)
    body = request.POST.get("body", "").strip()
    if not body:
        messages.error(request, "La nota interna no puede estar vacía.")
        return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)

    SupportMessage.objects.create(
        ticket=ticket, author=request.user,
        author_role=permission_level(request.user).value,
        body=body, visibility=SupportMessage.VISIBILITY_INTERNAL,
    )
    return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)


# ── Assignment ───────────────────────────────────────────────────────────

@support_panel_required
@require_POST
def support_panel_assign_view(request, pk):
    """
    action=claim     — Support/Ops/Owner claims an UNASSIGNED ticket for
                        themselves.
    action=unclaim   — actor unclaims their OWN assignment.
    action=reassign  — Ops/Owner ONLY: assign to any Owner/Ops/Support
                        user_id (validated via _is_assignable(), never
                        trusted from the POST body).
    action=unassign  — Ops/Owner ONLY: clear assignment entirely.
    """
    from .permission_levels import is_ops_admin

    ticket = get_object_or_404(SupportTicket, pk=pk)
    action = request.POST.get("action", "")
    actor_is_ops = is_ops_admin(request.user)

    if action == "claim":
        if ticket.assigned_to_id is not None:
            raise PermissionDenied("Ticket is already assigned.")
        ticket.assigned_to = request.user
        ticket.assigned_at = timezone.now()
        ticket.save(update_fields=["assigned_to", "assigned_at"])

    elif action == "unclaim":
        if ticket.assigned_to_id != request.user.pk:
            raise PermissionDenied("You can only unclaim your own assignment.")
        ticket.assigned_to = None
        ticket.assigned_at = None
        ticket.save(update_fields=["assigned_to", "assigned_at"])

    elif action == "reassign":
        if not actor_is_ops:
            raise PermissionDenied("Only Ops/Owner may reassign a ticket.")
        target = get_object_or_404(User, pk=request.POST.get("user_id"))
        if not _is_assignable(target):
            raise PermissionDenied("Target user is not Support/Ops/Owner-eligible.")
        ticket.assigned_to = target
        ticket.assigned_at = timezone.now()
        ticket.save(update_fields=["assigned_to", "assigned_at"])

    elif action == "unassign":
        if not actor_is_ops:
            raise PermissionDenied("Only Ops/Owner may unassign a ticket.")
        ticket.assigned_to = None
        ticket.assigned_at = None
        ticket.save(update_fields=["assigned_to", "assigned_at"])

    else:
        return HttpResponseBadRequest("Unknown assignment action.")

    return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)


# ── Status ───────────────────────────────────────────────────────────────

@support_panel_required
@require_POST
def support_panel_status_view(request, pk):
    ticket = get_object_or_404(SupportTicket, pk=pk)
    to_status = request.POST.get("status", "")

    try:
        apply_transition(ticket, to_status, actor=request.user)
    except IllegalTransition:
        messages.error(request, "Esa transición de estado no es válida desde el estado actual.")

    return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)


# ── Escalation ───────────────────────────────────────────────────────────

@support_panel_required
@require_POST
def support_panel_escalate_view(request, pk):
    """
    Escalates to Ops. Sets escalated_to_ops/escalated_at/escalated_by/
    escalation_reason atomically together with status=ESCALATED (one
    apply_transition() call, one save()). Grants NO new permission to
    the escalating actor — this is a data flip plus a status
    transition, nothing else. escalated_to_ops stays True permanently
    even after the ticket is later resolved/handed back — it's a
    historical marker, independent of live status.
    """
    ticket = get_object_or_404(SupportTicket, pk=pk)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "La razón de escalamiento es obligatoria.")
        return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)

    try:
        apply_transition(
            ticket, SupportTicket.STATUS_ESCALATED, actor=request.user,
            extra_fields={
                "escalated_to_ops": True,
                "escalated_at": timezone.now(),
                "escalated_by": request.user,
                "escalation_reason": reason,
            },
        )
    except IllegalTransition:
        messages.error(request, "Este ticket no puede ser escalado desde su estado actual.")

    return redirect("simulator:support_panel_ticket_detail", pk=ticket.pk)
