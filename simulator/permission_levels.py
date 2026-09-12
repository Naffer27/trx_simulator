"""
simulator/permission_levels.py

MONEY-INTEGRITY-FIX-02 — single source of truth for the authority
hierarchy: OWNER ROOT > OPS ADMIN > CUSTOMER SUPPORT > (none).

Never duplicate this logic in admin.py/views.py/owner_actions.py — always
call these functions. In particular:

  - Owner Root status is NEVER inferred from user.is_superuser. A second
    (or future) superuser must never appear to hold Owner powers just
    because Django's own is_superuser flag happens to be True — Owner
    status is data-driven (OwnerRoot table), not permission-flag-driven.
  - Ops Admin status is either (a) being Owner Root (Owner always has
    every capability Ops has), or (b) being the single non-Owner user
    currently recorded in OpsAdminProfile.
  - Customer Support status is the plain Django permission
    "simulator.is_customer_support" (SupportTicket.Meta.permissions) —
    the only one of the three that is a normal, multi-holder, grantable
    permission (no singleton semantics), since multiple support agents
    are expected by design.
"""
from enum import Enum


class PermissionLevel(str, Enum):
    OWNER = "OWNER"
    OPS = "OPS"
    SUPPORT = "SUPPORT"
    NONE = "NONE"


def is_owner_root(user) -> bool:
    """
    True iff *user* is recorded as the (singleton) OwnerRoot. Never checks
    user.is_superuser — that flag has no bearing on Owner status.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from .models import OwnerRoot

    return OwnerRoot.objects.filter(user_id=user.pk).exists()


def is_ops_admin(user) -> bool:
    """
    True iff *user* is Owner Root, OR is the single user currently
    recorded in OpsAdminProfile. Never checks user.is_staff/is_superuser.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if is_owner_root(user):
        return True
    from .models import OpsAdminProfile

    return OpsAdminProfile.objects.filter(user_id=user.pk, singleton_enforcer=True).exists()


def is_customer_support(user) -> bool:
    """
    True iff *user* holds the plain Django permission
    "simulator.is_customer_support". Owner/Ops are NOT automatically
    Support — this level is orthogonal to the hierarchy above (a
    Support-only agent never gets Ops/Owner capabilities from this
    function, and Owner/Ops do not need this permission to access
    anything Ops-level already covers).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.has_perm("simulator.is_customer_support")


def permission_level(user) -> PermissionLevel:
    """
    Resolve *user* to exactly one level: OWNER, OPS, SUPPORT, or NONE.
    Highest applicable level wins (Owner Root is also, structurally,
    OPS-capable, but this function reports OWNER for them — callers that
    need "can this user do what Ops can do" should call is_ops_admin()
    directly rather than comparing this enum, since is_ops_admin()
    correctly returns True for Owner too).
    """
    if is_owner_root(user):
        return PermissionLevel.OWNER
    if is_ops_admin(user):
        return PermissionLevel.OPS
    if is_customer_support(user):
        return PermissionLevel.SUPPORT
    return PermissionLevel.NONE
