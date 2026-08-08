# simulator/tests/test_o4b1_treasury_permission_assignment_audit.py
"""
Microbloque O.4b-1 — Treasury Permission Assignment Audit.

Makes the existing `assign_treasury_role` management command path
auditable: every REAL grant/revoke (never a no-op) now writes exactly
one AuditLog row + one BrokerAuditEvent (EV_TREASURY_PERMISSION_GRANTED/
REVOKED). This file does not re-test the command's own idempotency,
role mapping, or "does not touch X" guarantees — those are already
covered exhaustively by test_o3d5_assign_treasury_role_command.py and
remain unmodified. It covers only the new audit behavior.

Scope: only the management-command path (via="management_command").
The Django Admin User-form path (O.4b-2), permission-concentration
detection/blocking (O.4b-3), and TreasuryHardenedUserAdmin do not exist
yet — nothing here tests them.
"""
from io import StringIO

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase

from simulator.management.commands.assign_treasury_role import TREASURY_ROLE_CODENAMES
from simulator.models import AuditLog, BrokerAuditEvent, TreasuryOperationRequest

from .factories import make_user


def _codename_for(role):
    return TREASURY_ROLE_CODENAMES[role]


class GrantAuditTests(TestCase):

    def test_real_grant_writes_exactly_one_auditlog_row(self):
        user = make_user(username="o4b1_grant_audit_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        rows = AuditLog.objects.filter(event_type="treasury.permission_granted")
        self.assertEqual(rows.count(), 1)

    def test_real_grant_writes_exactly_one_brokerauditevent(self):
        user = make_user(username="o4b1_grant_bae_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())
        rows = BrokerAuditEvent.objects.filter(event_type="treasury.permission_granted")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.category, "ADMIN")
        self.assertEqual(row.severity, "WARNING")

    def test_grant_auditlog_detail_contains_required_fields(self):
        user = make_user(username="o4b1_grant_detail_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        row = AuditLog.objects.get(event_type="treasury.permission_granted")
        detail = row.detail
        self.assertEqual(detail["target_user_id"], user.pk)
        self.assertEqual(detail["target_username"], user.username)
        self.assertEqual(detail["role"], "submitter")
        self.assertEqual(detail["codename"], "can_submit_treasury_request")
        self.assertEqual(detail["granted_by"], "management_command")
        self.assertEqual(detail["via"], "management_command")
        self.assertIsNone(detail["is_self_grant"])
        self.assertEqual(detail["resulting_treasury_permission_count"], 1)

    def test_grant_brokerauditevent_metadata_matches_auditlog_detail(self):
        user = make_user(username="o4b1_grant_metadata_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "recoverer", stdout=StringIO())
        audit_row = AuditLog.objects.get(event_type="treasury.permission_granted")
        bae_row = BrokerAuditEvent.objects.get(event_type="treasury.permission_granted")
        self.assertEqual(bae_row.metadata, audit_row.detail)

    def test_resulting_count_reflects_multiple_roles_on_same_user(self):
        user = make_user(username="o4b1_grant_count_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        second = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="reviewer",
        ).get()
        self.assertEqual(second.detail["resulting_treasury_permission_count"], 2)

    def test_each_role_grant_is_audited(self):
        for role in TREASURY_ROLE_CODENAMES:
            with self.subTest(role=role):
                user = make_user(username=f"o4b1_grant_role_{role}", is_staff=True)
                call_command("assign_treasury_role", user.username, role, stdout=StringIO())
                row = AuditLog.objects.get(
                    event_type="treasury.permission_granted", detail__target_user_id=user.pk,
                )
                self.assertEqual(row.detail["codename"], _codename_for(role))


class RevokeAuditTests(TestCase):

    def test_real_revoke_writes_exactly_one_auditlog_row(self):
        user = make_user(username="o4b1_revoke_audit_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", "--remove", stdout=StringIO())
        rows = AuditLog.objects.filter(event_type="treasury.permission_revoked")
        self.assertEqual(rows.count(), 1)

    def test_real_revoke_writes_exactly_one_brokerauditevent(self):
        user = make_user(username="o4b1_revoke_bae_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "executor", "--remove", stdout=StringIO())
        rows = BrokerAuditEvent.objects.filter(event_type="treasury.permission_revoked")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().severity, "WARNING")

    def test_revoke_auditlog_detail_contains_required_fields(self):
        user = make_user(username="o4b1_revoke_detail_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "recoverer", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "recoverer", "--remove", stdout=StringIO())
        row = AuditLog.objects.get(event_type="treasury.permission_revoked")
        detail = row.detail
        self.assertEqual(detail["target_user_id"], user.pk)
        self.assertEqual(detail["role"], "recoverer")
        self.assertEqual(detail["codename"], "can_recover_treasury_execution")
        self.assertEqual(detail["granted_by"], "management_command")
        self.assertEqual(detail["via"], "management_command")
        self.assertIsNone(detail["is_self_grant"])
        self.assertEqual(detail["resulting_treasury_permission_count"], 0)

    def test_resulting_count_after_revoke_reflects_remaining_roles(self):
        user = make_user(username="o4b1_revoke_count_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "submitter", "--remove", stdout=StringIO())
        row = AuditLog.objects.get(event_type="treasury.permission_revoked")
        self.assertEqual(row.detail["resulting_treasury_permission_count"], 1)


class NoOpNotAuditedTests(TestCase):
    """
    A no-op (assigning an already-held permission, or removing an
    already-absent one) must never write a fake grant/revoke event —
    only a REAL mutation is audited.
    """

    def test_assigning_already_held_permission_writes_no_event(self):
        user = make_user(username="o4b1_noop_grant_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        AuditLog.objects.filter(event_type="treasury.permission_granted").delete()
        BrokerAuditEvent.objects.filter(event_type="treasury.permission_granted").delete()

        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())  # no-op

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 0,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.permission_granted").count(), 0,
        )

    def test_removing_already_absent_permission_writes_no_event(self):
        user = make_user(username="o4b1_noop_revoke_user", is_staff=True)
        call_command(
            "assign_treasury_role", user.username, "executor", "--remove", stdout=StringIO(),
        )  # already absent -> no-op
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_revoked").count(), 0,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.permission_revoked").count(), 0,
        )

    def test_invalid_role_writes_no_event(self):
        user = make_user(username="o4b1_noop_invalid_role_user", is_staff=True)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("assign_treasury_role", user.username, "bogus_role", stdout=StringIO())
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)

    def test_nonexistent_user_writes_no_event(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command(
                "assign_treasury_role", "no_such_user_o4b1", "reviewer", stdout=StringIO(),
            )
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)


class IdempotencyPreservedTests(TestCase):
    """O.4b-1 must not change the command's existing idempotent contract."""

    def test_repeated_grant_still_idempotent_and_stdout_unchanged(self):
        user = make_user(username="o4b1_idem_grant_user", is_staff=True)
        out1 = StringIO()
        call_command("assign_treasury_role", user.username, "submitter", stdout=out1)
        self.assertIn("Assigned", out1.getvalue())

        out2 = StringIO()
        call_command("assign_treasury_role", user.username, "submitter", stdout=out2)
        self.assertIn("already has", out2.getvalue())

        self.assertEqual(
            user.user_permissions.filter(codename="can_submit_treasury_request").count(), 1,
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 1,
        )

    def test_repeated_removal_still_idempotent_and_stdout_unchanged(self):
        user = make_user(username="o4b1_idem_revoke_user", is_staff=True)
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())

        out1 = StringIO()
        call_command("assign_treasury_role", user.username, "executor", "--remove", stdout=out1)
        self.assertIn("Removed", out1.getvalue())

        out2 = StringIO()
        call_command("assign_treasury_role", user.username, "executor", "--remove", stdout=out2)
        self.assertIn("does not have", out2.getvalue())

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_revoked").count(), 1,
        )


class NoFinancialOrModelSideEffectsTests(TestCase):

    def test_audit_wiring_never_imports_wallet_or_treasury_service_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.management.commands import assign_treasury_role as module

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed", "execute_treasury_request",
        }
        forbidden_imports = {"wallet_ledger", "treasury_requests", "treasury_execution_recovery"}

        tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.rsplit(".", 1)[-1])
                imported.update(a.name for a in node.names)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)

        self.assertFalse(forbidden_calls & called, f"found: {forbidden_calls & called}")
        self.assertFalse(forbidden_imports & imported, f"found: {forbidden_imports & imported}")

    def test_no_new_treasury_permissions_created(self):
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(TreasuryOperationRequest)
        codenames = set(
            Permission.objects.filter(content_type=ct).values_list("codename", flat=True)
        )
        expected = {
            "add_treasuryoperationrequest", "change_treasuryoperationrequest",
            "delete_treasuryoperationrequest", "view_treasuryoperationrequest",
            "can_submit_treasury_request", "can_review_treasury_request",
            "can_execute_treasury_request", "can_recover_treasury_execution",
        }
        self.assertEqual(codenames, expected)
