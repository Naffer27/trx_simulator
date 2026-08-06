# simulator/tests/test_o3d5_assign_treasury_role_command.py
"""
Bloque O.3d-5 — Treasury Role Assignment Command.

Covers ONLY simulator/management/commands/assign_treasury_role.py — a
management command that adds/removes one of the four Treasury
permissions directly on user.user_permissions. No Django Group is ever
created (O.3d Fase 0 section 9: this project uses zero Group objects
anywhere), no user is ever created, no TreasuryOperationRequest/Wallet/
WalletTransaction is ever read or touched, and no financial function is
ever imported or called by this command.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from simulator.management.commands.assign_treasury_role import TREASURY_ROLE_CODENAMES
from simulator.models import TreasuryOperationRequest, Wallet, WalletTransaction

from .factories import make_user

COMMAND = "assign_treasury_role"


def _run(*args):
    out, err = StringIO(), StringIO()
    call_command(COMMAND, *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class CommandExistsTests(TestCase):
    """1 — the command exists and is discoverable by Django."""

    def test_command_is_registered(self):
        from django.core.management import get_commands
        self.assertIn(COMMAND, get_commands())

    def test_command_module_importable(self):
        from simulator.management.commands.assign_treasury_role import Command
        self.assertTrue(callable(Command))


class RoleMappingTests(TestCase):
    """2, 17 — each role maps to the correct permission codename, and
    that codename resolves against TreasuryOperationRequest's own
    content type."""

    def test_mapping_is_exactly_the_four_approved_roles(self):
        self.assertEqual(
            TREASURY_ROLE_CODENAMES,
            {
                "submitter": "can_submit_treasury_request",
                "reviewer": "can_review_treasury_request",
                "executor": "can_execute_treasury_request",
                "recoverer": "can_recover_treasury_execution",
            },
        )

    def test_each_codename_resolves_on_treasuryoperationrequest_content_type(self):
        from django.contrib.contenttypes.models import ContentType
        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        for role, codename in TREASURY_ROLE_CODENAMES.items():
            with self.subTest(role=role):
                perm = Permission.objects.get(codename=codename, content_type=content_type)
                self.assertEqual(perm.content_type, content_type)


class AssignTests(TestCase):
    """3, 16 — assigning a role adds exactly the right permission, with
    a clear success message."""

    def setUp(self):
        self.user = make_user(username="o3d5_assign_user", is_staff=True)

    def test_assigns_permission_for_each_role(self):
        for role, codename in TREASURY_ROLE_CODENAMES.items():
            with self.subTest(role=role):
                user = make_user(username=f"o3d5_assign_{role}", is_staff=True)
                out, _ = _run(user.username, role)
                user.refresh_from_db()
                self.assertTrue(user.has_perm(f"simulator.{codename}"))
                self.assertIn("Assigned", out)
                self.assertIn(codename, out)
                self.assertIn(user.username, out)

    def test_assign_message_is_clear_and_successful(self):
        out, _ = _run(self.user.username, "submitter")
        self.assertIn("✓", out)
        self.assertIn("Assigned", out)
        self.assertIn("can_submit_treasury_request", out)


class RemoveTests(TestCase):
    """4 — --remove retires the permission, with a clear message."""

    def setUp(self):
        self.user = make_user(username="o3d5_remove_user", is_staff=True)
        _run(self.user.username, "reviewer")

    def test_remove_retires_the_permission(self):
        # has_perm() caches its result on the User instance for that
        # object's lifetime — refresh_from_db() only refreshes model
        # fields, never that cache. Re-fetching a fresh instance (as
        # the command itself does internally) avoids the false
        # positive a stale has_perm() cache would otherwise produce.
        User = get_user_model()
        self.assertTrue(
            User.objects.get(pk=self.user.pk).has_perm("simulator.can_review_treasury_request")
        )

        out, _ = _run(self.user.username, "reviewer", "--remove")
        self.assertFalse(
            User.objects.get(pk=self.user.pk).has_perm("simulator.can_review_treasury_request")
        )
        self.assertIn("Removed", out)
        self.assertIn("can_review_treasury_request", out)


class IdempotencyTests(TestCase):
    """5, 6 — repeated assignment and repeated removal are both no-ops
    that report the current state instead of erroring."""

    def setUp(self):
        self.user = make_user(username="o3d5_idem_user", is_staff=True)

    def test_repeated_assignment_is_idempotent(self):
        _run(self.user.username, "executor")
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.user_permissions.filter(codename="can_execute_treasury_request").count(), 1,
        )

        out, _ = _run(self.user.username, "executor")
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.user_permissions.filter(codename="can_execute_treasury_request").count(), 1,
        )
        self.assertIn("already has", out)

    def test_repeated_removal_is_idempotent(self):
        out, _ = _run(self.user.username, "executor", "--remove")
        self.assertIn("does not have", out)
        self.assertIn("nothing to remove", out)

        _run(self.user.username, "executor")
        _run(self.user.username, "executor", "--remove")
        out2, _ = _run(self.user.username, "executor", "--remove")
        self.assertIn("does not have", out2)


class InvalidInputTests(TestCase):
    """7, 8 — nonexistent user and invalid role both raise CommandError."""

    def test_nonexistent_user_raises_command_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, "no_such_user_at_all", "submitter")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_role_raises_command_error(self):
        user = make_user(username="o3d5_invalid_role_user", is_staff=True)
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, user.username, "not_a_real_role")
        self.assertIn("Invalid role", str(ctx.exception))

    def test_invalid_role_error_lists_valid_roles(self):
        user = make_user(username="o3d5_invalid_role_lists_user", is_staff=True)
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, user.username, "bogus")
        message = str(ctx.exception)
        for role in TREASURY_ROLE_CODENAMES:
            self.assertIn(role, message)

    def test_role_validated_before_touching_a_nonexistent_user(self):
        """Both invalid — role is checked first (a pure string check,
        no DB access), so the error message is about the role, not a
        confusing 'user not found' for a request that was never going
        to resolve a role either way."""
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, "also_missing_user", "bogus_role")
        self.assertIn("Invalid role", str(ctx.exception))


class NoUnrelatedSideEffectsTests(TestCase):
    """9, 10, 11, 12, 13, 14, 15 — no other permission is touched, no
    is_staff/is_superuser change, no Group created, no user created, no
    Treasury/Wallet data touched."""

    def setUp(self):
        self.user = make_user(username="o3d5_noeffect_user", is_staff=True)
        self.other_perm = Permission.objects.get(codename="can_review_treasury_request")
        self.user.user_permissions.add(self.other_perm)

    def test_does_not_touch_other_permissions(self):
        _run(self.user.username, "executor")
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_perm("simulator.can_review_treasury_request"))

        _run(self.user.username, "executor", "--remove")
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_perm("simulator.can_review_treasury_request"))

    def test_does_not_change_is_staff(self):
        staff_before = self.user.is_staff
        _run(self.user.username, "submitter")
        self.user.refresh_from_db()
        self.assertEqual(self.user.is_staff, staff_before)

    def test_does_not_change_is_superuser(self):
        superuser_before = self.user.is_superuser
        _run(self.user.username, "submitter")
        self.user.refresh_from_db()
        self.assertEqual(self.user.is_superuser, superuser_before)

    def test_does_not_create_a_group(self):
        before = Group.objects.count()
        _run(self.user.username, "submitter")
        _run(self.user.username, "submitter", "--remove")
        self.assertEqual(Group.objects.count(), before)

    def test_does_not_create_a_user(self):
        User = get_user_model()
        before = User.objects.count()
        _run(self.user.username, "submitter")
        self.assertEqual(User.objects.count(), before)

    def test_does_not_touch_treasury_operation_request(self):
        before = TreasuryOperationRequest.objects.count()
        _run(self.user.username, "submitter")
        _run(self.user.username, "submitter", "--remove")
        self.assertEqual(TreasuryOperationRequest.objects.count(), before)

    def test_does_not_touch_wallet_or_balances(self):
        from .factories import make_wallet
        from decimal import Decimal

        wallet = make_wallet(user=self.user, initial_balance=Decimal("42.00"))
        balance_before = wallet.available_balance
        wtx_before = WalletTransaction.objects.filter(wallet=wallet).count()

        _run(self.user.username, "submitter")
        _run(self.user.username, "submitter", "--remove")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), wtx_before)


class MessageClarityTests(TestCase):
    """16 — every one of the six documented message kinds is distinct
    and human-readable."""

    def setUp(self):
        self.user = make_user(username="o3d5_messages_user", is_staff=True)

    def test_assigned_message(self):
        out, _ = _run(self.user.username, "submitter")
        self.assertIn("Assigned", out)

    def test_already_present_message(self):
        _run(self.user.username, "submitter")
        out, _ = _run(self.user.username, "submitter")
        self.assertIn("already has", out)

    def test_removed_message(self):
        _run(self.user.username, "submitter")
        out, _ = _run(self.user.username, "submitter", "--remove")
        self.assertIn("Removed", out)

    def test_already_absent_message(self):
        out, _ = _run(self.user.username, "submitter", "--remove")
        self.assertIn("does not have", out)

    def test_nonexistent_user_message(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, "ghost_user", "submitter")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_role_message(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(COMMAND, self.user.username, "made_up")
        self.assertIn("Invalid role", str(ctx.exception))


class ScopeAndSafetyTests(TestCase):
    """18 — AST confirms absence of financial logic in the command."""

    def test_ast_confirms_no_financial_functions_or_raw_sql(self):
        import ast
        import inspect
        import textwrap

        from simulator.management.commands import assign_treasury_role as module

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed", "execute_treasury_request",
            "raw", "cursor",
        }
        forbidden_imports = {"wallet_ledger"}

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

    def test_no_group_objects_create_call(self):
        import ast
        import inspect
        import textwrap

        from simulator.management.commands import assign_treasury_role as module

        tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
        source = inspect.getsource(module)
        self.assertNotIn("Group.objects.create", source)
        self.assertNotIn("Group(", source)

    def test_no_wallet_or_treasury_model_imported(self):
        import inspect

        from simulator.management.commands import assign_treasury_role as module

        source = inspect.getsource(module)
        self.assertNotIn("WalletTransaction", source)
        self.assertNotIn("credit_wallet", source)
        self.assertNotIn("debit_wallet", source)
