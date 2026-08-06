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

    def handle(self, *args, **options):
        username = options["username"]
        role = options["role"]
        remove = options["remove"]

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

        user.user_permissions.add(permission)
        self.stdout.write(
            self.style.SUCCESS(f"✓ Assigned '{codename}' ({role}) to '{username}'.")
        )
