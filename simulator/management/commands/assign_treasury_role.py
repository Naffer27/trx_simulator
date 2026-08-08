"""
simulator/management/commands/assign_treasury_role.py

Assign or remove one of the four Treasury Private Operations
permissions for a single, existing user — directly on
user.user_permissions, no Django Group involved.

O.3d Fase 0 (section 9) found zero use of django.contrib.auth.models.
Group anywhere in this project, in any domain — introducing Groups for
Treasury alone would be the first such use in the whole codebase, a
deviation from an otherwise universal convention. This command follows
the existing convention instead: per-user permission assignment, made
less tedious with a single command rather than a new pattern.

Usage:
    python manage.py assign_treasury_role <username> <role>
    python manage.py assign_treasury_role <username> <role> --remove

Roles (exact mapping, frozen by O.3d-5's approved design):
    submitter  -> simulator.can_submit_treasury_request
    reviewer   -> simulator.can_review_treasury_request
    executor   -> simulator.can_execute_treasury_request
    recoverer  -> simulator.can_recover_treasury_execution

Idempotent: assigning an already-held permission, or removing an
already-absent one, is a no-op that reports the current state instead
of raising or erroring. Never touches is_staff, is_superuser, any
OTHER permission the user already holds, any Group, or any Treasury/
Wallet data — this command's only possible side effect is a single row
in the user_permissions through-table, added or removed.

O.4b-1 — every REAL grant/revoke (never a no-op) is now audited via
AuditLog + BrokerAuditEvent (EV_TREASURY_PERMISSION_GRANTED/REVOKED),
same fail-open discipline as disable_2fa.py's own AuditLog.objects.
create() (audit failure must never break the underlying operation —
the permission add()/remove() above has already committed by the time
these calls run). via="management_command" always — this command has
no HttpRequest and therefore no Django-authenticated actor identity to
record; "granted_by" reflects that honestly rather than fabricating
one (same idiom as disable_2fa.py's "performed_by": "management_
command"). is_self_grant is recorded as None for the same reason: this
command cannot know who is typing at the shell, so it cannot say
whether that person and the target user are the same identity — this
becomes computable in O.4b-2, where the actor is a real request.user.

O.4b-3 — Treasury Role Concentration Guard. A user is "concentrated"
when they hold MORE THAN ONE of the four Treasury permissions (shared,
pure rule: simulator/treasury_permissions.py — the same function
TreasuryHardenedUserAdmin uses, so the rule is defined exactly once).
Detection and audit of concentration happen unconditionally on every
grant; BLOCKING a concentrating grant only happens when
settings.TREASURY_ROLE_CONCENTRATION_BLOCKING is True (default False).

    python manage.py assign_treasury_role <username> <role> --force

--force overrides a concentration block for a GRANT. It is meaningless
when blocking is off (nothing to override) and meaningless for
--remove (revokes are never blocked) — both are accepted without
error but have no effect on behavior; see handle() for the one
exception (--force together with --remove raises CommandError, since
combining a grant-only flag with the opposite operation is very likely
an operator mistake worth surfacing immediately rather than silently
ignoring). Any grant that succeeds only because of --force is audited
with force_used=True — same fail-open discipline as every other audit
call in this file.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

# Frozen mapping — role name -> TreasuryOperationRequest permission
# codename. Deliberately a plain dict, not derived from the model's
# Meta.permissions list, so a future addition to that list can never
# silently become a selectable role here without this command being
# reviewed and updated on purpose.
TREASURY_ROLE_CODENAMES = {
    "submitter": "can_submit_treasury_request",
    "reviewer": "can_review_treasury_request",
    "executor": "can_execute_treasury_request",
    "recoverer": "can_recover_treasury_execution",
}


def _audit_permission_change(*, action: str, user, role: str, codename: str, content_type,
                              force_used: bool = False):
    """
    O.4b-1 — writes exactly one AuditLog row + one BrokerAuditEvent for a
    REAL grant or revoke (callers never invoke this for a no-op). Never
    raises: an audit failure here must not surface as a command failure,
    since the permission mutation has already committed.

    action: "granted" | "revoked".

    O.4b-3 — also records the resulting Treasury permission combination,
    whether it is concentrated (more than one held), the current value
    of TREASURY_ROLE_CONCENTRATION_BLOCKING, and whether --force was the
    reason a concentrating grant was allowed through. concentration_
    detected/blocking_enabled/force_used are computed here rather than
    passed a pre-built dict from handle(), so this stays the single
    place that decides what "resulting state" means for this audit
    event.
    """
    from django.conf import settings

    from simulator.treasury_permissions import held_treasury_codenames

    resulting_codenames = held_treasury_codenames(user)
    resulting_count = len(resulting_codenames)

    detail = {
        "target_user_id": user.pk,
        "target_username": user.username,
        "role": role,
        "codename": codename,
        "granted_by": "management_command",
        "via": "management_command",
        "is_self_grant": None,  # unknowable from a shell command — see module docstring
        "resulting_treasury_permission_count": resulting_count,
        "treasury_permissions": list(resulting_codenames),
        "concentration_detected": resulting_count > 1,
        "blocking_enabled": getattr(settings, "TREASURY_ROLE_CONCENTRATION_BLOCKING", False),
        "force_used": force_used,
        "actor": None,  # no Django-authenticated identity from a shell command
        "outcome": action,
    }
    event_type = (
        "treasury.permission_granted" if action == "granted" else "treasury.permission_revoked"
    )

    try:
        from simulator.models import AuditLog
        AuditLog.objects.create(
            event_type=event_type,
            action=f"Treasury permission '{codename}' ({role}) {action} for user "
                   f"'{user.username}' via management command",
            detail=detail,
        )
    except Exception:
        pass

    from simulator import broker_audit as _audit
    _audit.record_admin_event(
        event_type=event_type,
        severity=_audit.Severity.WARNING,
        description=f"Treasury permission '{codename}' ({role}) {action} for user "
                    f"#{user.pk} via management command",
        source_module="simulator.management.commands.assign_treasury_role",
        metadata=detail,
    )


class Command(BaseCommand):
    help = (
        "Assign or remove a Treasury Private Operations role "
        "(submitter/reviewer/executor/recoverer) for a single existing "
        "user, via user_permissions directly — no Django Groups, no "
        "Treasury data touched."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username of an existing user")
        parser.add_argument(
            "role", type=str,
            help="One of: " + ", ".join(sorted(TREASURY_ROLE_CODENAMES)),
        )
        parser.add_argument(
            "--remove", action="store_true",
            help="Remove the role's permission instead of assigning it",
        )
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Override a concentration block for a GRANT "
                "(only meaningful with TREASURY_ROLE_CONCENTRATION_BLOCKING=True; "
                "cannot be combined with --remove)"
            ),
        )

    def handle(self, *args, **options):
        username = options["username"]
        role = options["role"]
        remove = options["remove"]
        force = options["force"]

        # O.4b-3 — a pure CLI-argument-shape check, independent of
        # whether username/role are even valid, so it fails fast before
        # any lookup. --force only has meaning for a GRANT (overriding a
        # concentration block); combining it with --remove (which is
        # never blocked) is very likely an operator mistake, so this
        # raises rather than silently ignoring the flag.
        if force and remove:
            raise CommandError(
                "--force cannot be combined with --remove — revokes are never "
                "blocked by Treasury role concentration, so --force has no effect there."
            )

        # Validate the role BEFORE touching the database — a plain
        # string check, deliberately not argparse's own choices=
        # (which would raise SystemExit via argparse.error() instead
        # of the CommandError this command's contract requires).
        codename = TREASURY_ROLE_CODENAMES.get(role)
        if codename is None:
            raise CommandError(
                f"Invalid role '{role}'. Valid roles: "
                + ", ".join(sorted(TREASURY_ROLE_CODENAMES))
            )

        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found.")

        # Resolve by codename AND content_type — codename alone is not
        # guaranteed globally unique across every model in the app;
        # TreasuryOperationRequest is the sole owner of all four
        # Treasury permissions (see its Meta.permissions), so this is
        # the correct, defensive lookup rather than a bare
        # Permission.objects.get(codename=...).
        from simulator.models import TreasuryOperationRequest

        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        try:
            permission = Permission.objects.get(codename=codename, content_type=content_type)
        except Permission.DoesNotExist:
            raise CommandError(
                f"Permission '{codename}' not found on TreasuryOperationRequest's "
                "content type — has a migration been skipped?"
            )

        already_has = user.user_permissions.filter(pk=permission.pk).exists()

        if remove:
            if not already_has:
                self.stdout.write(
                    self.style.WARNING(
                        f"'{username}' does not have '{codename}' ({role}) — nothing to remove."
                    )
                )
                return
            user.user_permissions.remove(permission)
            _audit_permission_change(
                action="revoked", user=user, role=role, codename=codename,
                content_type=content_type,
            )
            self.stdout.write(
                self.style.SUCCESS(f"✓ Removed '{codename}' ({role}) from '{username}'.")
            )
            return

        if already_has:
            self.stdout.write(
                self.style.WARNING(
                    f"'{username}' already has '{codename}' ({role}) — nothing to assign."
                )
            )
            return

        # O.4b-3 — Treasury Role Concentration Guard. Checked only for a
        # genuinely NEW grant (the already_has no-op above already
        # returned) — re-asserting an already-held permission is never
        # "new concentration". Blocked BEFORE user.user_permissions.add()
        # is called, so a blocked attempt produces zero mutation.
        from django.conf import settings

        from simulator.treasury_permissions import (
            held_treasury_codenames, record_concentration_blocked, would_be_concentrated,
        )

        blocking_enabled = getattr(settings, "TREASURY_ROLE_CONCENTRATION_BLOCKING", False)
        concentration = would_be_concentrated(user, granting=codename)

        if concentration.is_concentrated and blocking_enabled and not force:
            record_concentration_blocked(
                actor_id=None, target=user, codename=codename,
                current_codenames=held_treasury_codenames(user), via="management_command",
            )
            raise CommandError(
                f"Blocked: granting '{codename}' ({role}) to '{username}' would result "
                f"in {concentration.count} Treasury permissions "
                f"({', '.join(concentration.codenames)}). Use --force to override "
                "(will be audited)."
            )

        force_used = bool(force and blocking_enabled and concentration.is_concentrated)

        user.user_permissions.add(permission)
        _audit_permission_change(
            action="granted", user=user, role=role, codename=codename,
            content_type=content_type, force_used=force_used,
        )
        self.stdout.write(
            self.style.SUCCESS(f"✓ Assigned '{codename}' ({role}) to '{username}'.")
        )
