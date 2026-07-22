"""
management/commands/disable_2fa.py
Emergency command to disable 2FA for a user who has lost access to their TOTP device.

Usage:
    python manage.py disable_2fa <username>

This is the ONLY way to recover from a locked-out account.
Always log this action and investigate why the user lost access.
"""
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction


class Command(BaseCommand):
    help = "Emergency: disable 2FA for a user who has lost their TOTP device."

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username to disable 2FA for")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required flag to confirm the action (prevents accidental runs)",
        )

    def handle(self, *args, **options):
        username = options["username"]
        confirmed = options["confirm"]

        if not confirmed:
            self.stderr.write(
                self.style.ERROR(
                    f"This will permanently delete the TOTP device for '{username}'.\n"
                    f"Re-run with --confirm to proceed:\n"
                    f"  python manage.py disable_2fa {username} --confirm"
                )
            )
            return

        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found.")

        from simulator.models import TOTPDevice

        # AUDIT-04a — locked: same discipline as views.py::totp_disable_view.
        # Closes the cross-flow race against a user's own self-service
        # disable happening on the same TOTPDevice at nearly the same time
        # — whichever commits first wins, the other sees no row and skips.
        with transaction.atomic():
            device = TOTPDevice.objects.select_for_update().filter(user=user).first()
            if not device:
                self.stdout.write(self.style.WARNING(f"No TOTP device found for '{username}' — nothing to do."))
                return

            confirmed_at = device.confirmed_at
            was_confirmed = device.confirmed
            device.delete()

            # Write to audit log
            try:
                from simulator.models import AuditLog
                AuditLog.objects.create(
                    event_type="admin.2fa_disabled_emergency",
                    action=f"Emergency 2FA disable for user {username} (management command)",
                    detail={
                        "username": username,
                        "user_id": user.pk,
                        "was_confirmed": was_confirmed,
                        "confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
                        "performed_by": "management_command",
                    },
                )
            except Exception:
                pass

            from simulator import broker_audit as _audit
            _audit.record_auth_event(
                event_type=_audit.EV_2FA_DISABLED_EMERGENCY,
                severity=_audit.Severity.CRITICAL,
                actor_type=_audit.ActorType.SYSTEM,
                user=user,
                source_module="simulator.management.commands.disable_2fa",
                description=f"2FA emergency-disabled for user #{user.pk} via management command",
                metadata={
                    "was_confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
                    "performed_by": "management_command",
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ 2FA disabled for '{username}'. "
                f"The user can now log in with password only and re-enable 2FA at /account/2fa/setup/."
            )
        )
        self.stderr.write(
            self.style.WARNING(
                f"⚠ AUDIT: 2FA was emergency-disabled for '{username}'. "
                f"Investigate and document this action."
            )
        )
