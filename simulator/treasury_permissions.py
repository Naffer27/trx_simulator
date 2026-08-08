# simulator/treasury_permissions.py
"""
O.4b-3 — single source of truth for computing Treasury permission
concentration, plus the audit-writing for the one genuinely new event
this block introduces (EV_TREASURY_ROLE_CONCENTRATION_BLOCKED).

Pure and read-only where it matters: current_treasury_codenames() and
would_be_concentrated() never mutate anything — they only ever read
user.user_permissions. Both simulator/admin.py
(TreasuryHardenedUserAdmin) and simulator/management/commands/
assign_treasury_role.py import the concentration CALCULATION from here
so the rule ("concentrated" = holding more than one of the four Treasury
permissions) is defined in exactly one place. Neither of those two
modules imports this concern from the other.

Deliberately does NOT touch or re-derive assign_treasury_role.py's own
TREASURY_ROLE_CODENAMES (role name -> codename mapping, a CLI-naming
concern, already correct and tested since O.3d-5/O.4b-1) — that mapping
is left exactly as it is. This module owns only the four-codename tuple
itself and what it means for a user to hold more than one of them.

Does NOT duplicate the O.4b-1/O.4b-2 grant/revoked audit-writing
functions (assign_treasury_role.py::_audit_permission_change,
admin.py::_audit_admin_treasury_permission_change) — those already
exist, are already tested, and are extended in place with the new
concentration-related metadata fields rather than merged into a shared
function, to avoid touching their already-passing call sites more than
necessary. Only the BLOCKED event (which neither of those two functions
previously had any reason to write) gets a single shared writer here.
"""
from collections import namedtuple

TREASURY_PERMISSION_CODENAMES = (
    "can_submit_treasury_request",
    "can_review_treasury_request",
    "can_execute_treasury_request",
    "can_recover_treasury_execution",
)


Concentration = namedtuple("Concentration", ["codenames", "count", "is_concentrated"])


def treasury_permission_queryset():
    """Permission queryset for exactly the four Treasury codenames,
    content-type-scoped (same defensive lookup style as
    assign_treasury_role.py — codename alone is not guaranteed globally
    unique across every model in the app)."""
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    from .models import TreasuryOperationRequest

    content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
    return Permission.objects.filter(
        content_type=content_type, codename__in=TREASURY_PERMISSION_CODENAMES,
    )


def held_treasury_codenames(user):
    """Tuple (canonical, frozen order — the order TREASURY_PERMISSION_
    CODENAMES is declared in) of Treasury codenames `user` currently
    holds. Read-only: a plain query, never a mutation."""
    held = set(
        user.user_permissions.filter(
            pk__in=treasury_permission_queryset().values_list("pk", flat=True),
        ).values_list("codename", flat=True)
    )
    return tuple(c for c in TREASURY_PERMISSION_CODENAMES if c in held)


def would_be_concentrated(user, *, granting=None, revoking=None):
    """
    Pure prediction of a user's resulting Treasury permission set if
    `granting` were added and/or `revoking` were removed — never writes
    to the database. Concentration is defined as holding MORE THAN ONE
    of the four Treasury permissions (O.4b-3 Fase 0 contract).

    Returns a Concentration(codenames, count, is_concentrated) namedtuple
    describing the RESULTING state, not the current one.
    """
    held = set(held_treasury_codenames(user))
    if granting:
        held.add(granting)
    if revoking:
        held.discard(revoking)
    ordered = tuple(c for c in TREASURY_PERMISSION_CODENAMES if c in held)
    return Concentration(codenames=ordered, count=len(ordered), is_concentrated=len(ordered) > 1)


def record_concentration_blocked(*, actor_id, target, codename, current_codenames, via):
    """
    O.4b-3 — writes exactly one AuditLog row + one BrokerAuditEvent for a
    Treasury permission GRANT that was blocked because it would have
    produced role concentration. Shared by both call sites (management
    command and Django Admin) since this event did not exist before
    O.4b-3 — there is no pre-existing, separately-tested function to
    avoid disturbing here, unlike the granted/revoked writers.

    Never raises: an audit failure must not surface as a command/form
    failure. actor_id is None when the actor's Django identity isn't
    knowable (the management-command path — same honest-unknown
    convention as O.4b-1's "granted_by": "management_command").
    """
    from . import audit as _plain_audit

    resulting = tuple(sorted(set(current_codenames) | {codename}))
    detail = {
        "target_user_id": target.pk,
        "target_username": target.username,
        "treasury_permissions": list(current_codenames),
        "resulting_treasury_permission_count": len(resulting),
        "attempted_codename": codename,
        "via": via,
        "actor": actor_id,
        "blocking_enabled": True,
        "force_used": False,
        "outcome": "blocked",
    }

    try:
        from .models import AuditLog
        AuditLog.objects.create(
            event_type=_plain_audit.EV_TREASURY_ROLE_CONCENTRATION_BLOCKED,
            action=f"Treasury permission '{codename}' grant blocked for user "
                   f"'{target.username}' — would concentrate {len(resulting)} "
                   f"Treasury permissions (via {via})",
            detail=detail,
        )
    except Exception:
        pass

    from . import broker_audit as _audit
    _audit.record_admin_event(
        event_type=_audit.EV_TREASURY_ROLE_CONCENTRATION_BLOCKED,
        severity=_audit.Severity.WARNING,
        description=f"Treasury permission '{codename}' grant blocked for user "
                    f"#{target.pk} — would concentrate {len(resulting)} Treasury "
                    f"permissions (via {via})",
        actor_id=actor_id,
        source_module="simulator.treasury_permissions",
        metadata=detail,
    )
