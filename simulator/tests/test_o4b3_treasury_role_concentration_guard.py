# simulator/tests/test_o4b3_treasury_role_concentration_guard.py
"""
Microbloque O.4b-3 — Treasury Role Concentration Guard.

Covers:
  - simulator/treasury_permissions.py — the shared, pure calculation
    (held_treasury_codenames / would_be_concentrated) both the
    management command and TreasuryHardenedUserAdmin now use, so the
    "concentrated = holds more than one of the four" rule is defined
    exactly once.
  - settings.TREASURY_ROLE_CONCENTRATION_BLOCKING (default False).
  - assign_treasury_role.py's new --force flag and blocking behavior.
  - TreasuryHardenedUserAdmin's blocking behavior (no override in
    Admin — O.4b-3 Fase 0 accepted this scope).
  - The new EV_TREASURY_ROLE_CONCENTRATION_BLOCKED event, and the
    extended metadata on the existing GRANTED/REVOKED events from
    O.4b-1/O.4b-2 (which must keep working exactly as before).

This file does not re-test the O.4b-1/O.4b-2 contracts already covered
by test_o4b1_treasury_permission_assignment_audit.py and
test_o4b2_treasury_hardened_user_admin.py (idempotency, self-grant
prevention, is_superuser hardening, etc.) except where this block's
changes touch the same code paths and could plausibly have regressed
them — those specific checks are repeated here deliberately (section E)
to prove O.4b-2's protections are still intact after O.4b-3's edits to
the same save_related() method.
"""
from io import StringIO

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator.models import AuditLog, BrokerAuditEvent, TreasuryOperationRequest
from simulator.treasury_permissions import (
    TREASURY_PERMISSION_CODENAMES, held_treasury_codenames, would_be_concentrated,
)

from .factories import make_user, make_wallet
from simulator.models import InternalTransfer, WalletTransaction

CODENAMES = TREASURY_PERMISSION_CODENAMES  # (submit, review, execute, recover)


def _treasury_permission(codename):
    ct = ContentType.objects.get_for_model(TreasuryOperationRequest)
    return Permission.objects.get(content_type=ct, codename=codename)


def _user_content_type_permission(codename):
    from django.contrib.auth.models import User
    ct = ContentType.objects.get_for_model(User)
    return Permission.objects.get(content_type=ct, codename=codename)


def _grant_user_admin_access(user):
    user.user_permissions.add(
        _user_content_type_permission("view_user"),
        _user_content_type_permission("change_user"),
    )
    user.refresh_from_db()
    return user


def _user_change_url(user):
    return reverse("admin:auth_user_change", args=[user.pk])


_UNSET = object()


def _base_change_payload(user, *, actor, is_active=True, is_staff=None, is_superuser=None,
                          user_permission_pks=_UNSET, **overrides):
    """Same helper shape as test_o4b2 — a real browser always resubmits
    the full current widget selection, so an unset value defaults to
    the user's current permissions minus whatever Treasury permissions
    this actor/target combo would not actually see as selectable."""
    if is_staff is None:
        is_staff = user.is_staff
    if is_superuser is None:
        is_superuser = user.is_superuser

    if user_permission_pks is _UNSET:
        pks = set(user.user_permissions.values_list("pk", flat=True))
        is_self_edit = user.pk == actor.pk
        if not actor.is_superuser or is_self_edit:
            treasury_pks = set(
                Permission.objects.filter(codename__in=CODENAMES).values_list("pk", flat=True)
            )
            pks -= treasury_pks
        user_permission_pks = pks

    data = {
        "username": user.username,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "email": user.email or "",
        "date_joined_0": user.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": user.date_joined.strftime("%H:%M:%S"),
    }
    if is_active:
        data["is_active"] = "on"
    if is_staff:
        data["is_staff"] = "on"
    if is_superuser:
        data["is_superuser"] = "on"
    data["user_permissions"] = [str(pk) for pk in user_permission_pks]
    data.update(overrides)
    return data


# ─────────────────────────────────────────────
# A. Shared helper — pure calculation
# ─────────────────────────────────────────────

class ConcentrationHelperTests(TestCase):

    def test_zero_permissions_not_concentrated(self):
        user = make_user()
        result = would_be_concentrated(user)
        self.assertEqual(result.count, 0)
        self.assertFalse(result.is_concentrated)
        self.assertEqual(result.codenames, ())

    def test_one_permission_not_concentrated(self):
        user = make_user()
        user.user_permissions.add(_treasury_permission("can_submit_treasury_request"))
        result = would_be_concentrated(user)
        self.assertEqual(result.count, 1)
        self.assertFalse(result.is_concentrated)

    def test_two_permissions_concentrated(self):
        user = make_user()
        user.user_permissions.add(
            _treasury_permission("can_submit_treasury_request"),
            _treasury_permission("can_review_treasury_request"),
        )
        result = would_be_concentrated(user)
        self.assertEqual(result.count, 2)
        self.assertTrue(result.is_concentrated)

    def test_four_permissions_concentrated(self):
        user = make_user()
        for codename in CODENAMES:
            user.user_permissions.add(_treasury_permission(codename))
        result = would_be_concentrated(user)
        self.assertEqual(result.count, 4)
        self.assertTrue(result.is_concentrated)
        self.assertEqual(set(result.codenames), set(CODENAMES))

    def test_resulting_combination_is_deterministic_order(self):
        user = make_user()
        user.user_permissions.add(
            _treasury_permission("can_recover_treasury_execution"),
            _treasury_permission("can_submit_treasury_request"),
        )
        result = would_be_concentrated(user)
        # Canonical order = TREASURY_PERMISSION_CODENAMES declaration order,
        # not insertion order.
        self.assertEqual(
            result.codenames,
            ("can_submit_treasury_request", "can_recover_treasury_execution"),
        )

    def test_granting_predicts_without_mutating(self):
        user = make_user()
        user.user_permissions.add(_treasury_permission("can_submit_treasury_request"))
        result = would_be_concentrated(user, granting="can_review_treasury_request")
        self.assertTrue(result.is_concentrated)
        self.assertEqual(result.count, 2)
        # Prediction only — nothing was actually persisted.
        self.assertEqual(len(held_treasury_codenames(user)), 1)

    def test_revoking_predicts_without_mutating(self):
        user = make_user()
        user.user_permissions.add(
            _treasury_permission("can_submit_treasury_request"),
            _treasury_permission("can_review_treasury_request"),
        )
        result = would_be_concentrated(user, revoking="can_review_treasury_request")
        self.assertFalse(result.is_concentrated)
        self.assertEqual(result.count, 1)
        self.assertEqual(len(held_treasury_codenames(user)), 2)  # unchanged


# ─────────────────────────────────────────────
# B. Setting
# ─────────────────────────────────────────────

class ConcentrationBlockingSettingTests(TestCase):

    def test_default_is_false(self):
        from django.conf import settings
        self.assertFalse(settings.TREASURY_ROLE_CONCENTRATION_BLOCKING)

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_can_be_overridden_true(self):
        from django.conf import settings
        self.assertTrue(settings.TREASURY_ROLE_CONCENTRATION_BLOCKING)

    def test_independent_of_totp_admin_treasury_required(self):
        from django.conf import settings
        with override_settings(
            TOTP_ADMIN_TREASURY_REQUIRED=True, TREASURY_ROLE_CONCENTRATION_BLOCKING=False,
        ):
            self.assertTrue(settings.TOTP_ADMIN_TREASURY_REQUIRED)
            self.assertFalse(settings.TREASURY_ROLE_CONCENTRATION_BLOCKING)
        with override_settings(
            TOTP_ADMIN_TREASURY_REQUIRED=False, TREASURY_ROLE_CONCENTRATION_BLOCKING=True,
        ):
            self.assertFalse(settings.TOTP_ADMIN_TREASURY_REQUIRED)
            self.assertTrue(settings.TREASURY_ROLE_CONCENTRATION_BLOCKING)


# ─────────────────────────────────────────────
# C. Command — blocking=False
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=False)
class CommandBlockingDisabledTests(TestCase):

    def test_second_permission_still_allowed(self):
        user = make_user(username="o4b3_c_second_allowed", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        self.assertEqual(len(held_treasury_codenames(user)), 2)

    def test_concentration_is_still_audited_even_though_allowed(self):
        user = make_user(username="o4b3_c_audited", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        row = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="reviewer",
        ).get()
        self.assertTrue(row.detail["concentration_detected"])
        self.assertFalse(row.detail["blocking_enabled"])
        self.assertFalse(row.detail["force_used"])
        self.assertEqual(row.detail["outcome"], "granted")
        self.assertEqual(
            set(row.detail["treasury_permissions"]),
            {"can_submit_treasury_request", "can_review_treasury_request"},
        )

    def test_normal_first_grant_preserved(self):
        user = make_user(username="o4b3_c_first_grant", is_staff=True)
        out = StringIO()
        call_command("assign_treasury_role", user.username, "executor", stdout=out)
        self.assertIn("Assigned", out.getvalue())
        self.assertEqual(held_treasury_codenames(user), ("can_execute_treasury_request",))

    def test_revoke_preserved(self):
        user = make_user(username="o4b3_c_revoke", is_staff=True)
        call_command("assign_treasury_role", user.username, "recoverer", stdout=StringIO())
        out = StringIO()
        call_command(
            "assign_treasury_role", user.username, "recoverer", "--remove", stdout=out,
        )
        self.assertIn("Removed", out.getvalue())
        self.assertEqual(held_treasury_codenames(user), ())


# ─────────────────────────────────────────────
# D. Command — blocking=True
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
class CommandBlockingEnabledTests(TestCase):

    def test_first_permission_still_allowed(self):
        user = make_user(username="o4b3_d_first_allowed", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        self.assertEqual(held_treasury_codenames(user), ("can_submit_treasury_request",))

    def test_second_permission_blocked(self):
        user = make_user(username="o4b3_d_second_blocked", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())

    def test_zero_mutation_when_blocked(self):
        user = make_user(username="o4b3_d_zero_mutation", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        try:
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        except CommandError:
            pass
        self.assertEqual(held_treasury_codenames(user), ("can_submit_treasury_request",))

    def test_force_allows_the_grant(self):
        user = make_user(username="o4b3_d_force_allows", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command(
            "assign_treasury_role", user.username, "reviewer", "--force", stdout=StringIO(),
        )
        self.assertEqual(len(held_treasury_codenames(user)), 2)

    def test_force_grant_is_audited_with_force_used_true(self):
        user = make_user(username="o4b3_d_force_audit", is_staff=True)
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())
        call_command(
            "assign_treasury_role", user.username, "recoverer", "--force", stdout=StringIO(),
        )
        row = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="recoverer",
        ).get()
        self.assertTrue(row.detail["force_used"])
        self.assertTrue(row.detail["blocking_enabled"])
        self.assertTrue(row.detail["concentration_detected"])

    def test_blocked_attempt_is_audited(self):
        user = make_user(username="o4b3_d_blocked_audit", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        try:
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        except CommandError:
            pass
        row = AuditLog.objects.get(event_type="treasury.role_concentration_blocked")
        self.assertEqual(row.detail["target_user_id"], user.pk)
        self.assertEqual(row.detail["attempted_codename"], "can_review_treasury_request")
        self.assertEqual(row.detail["via"], "management_command")
        self.assertIsNone(row.detail["actor"])
        self.assertTrue(row.detail["blocking_enabled"])
        self.assertFalse(row.detail["force_used"])
        self.assertEqual(row.detail["outcome"], "blocked")
        self.assertEqual(row.detail["treasury_permissions"], ["can_submit_treasury_request"])

        bae = BrokerAuditEvent.objects.get(event_type="treasury.role_concentration_blocked")
        self.assertEqual(bae.category, "ADMIN")
        self.assertEqual(bae.severity, "WARNING")

    def test_blocked_grant_writes_no_false_granted_event(self):
        user = make_user(username="o4b3_d_no_false_grant", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        try:
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        except CommandError:
            pass
        self.assertEqual(
            AuditLog.objects.filter(
                event_type="treasury.permission_granted", detail__role="reviewer",
            ).count(),
            0,
        )

    def test_revoke_still_permitted_when_blocking_enabled(self):
        user = make_user(username="o4b3_d_revoke_ok", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", "--force", stdout=StringIO())
        out = StringIO()
        call_command(
            "assign_treasury_role", user.username, "submitter", "--remove", stdout=out,
        )
        self.assertIn("Removed", out.getvalue())
        self.assertEqual(held_treasury_codenames(user), ())

    def test_idempotent_grant_of_already_held_permission_never_blocked(self):
        user = make_user(username="o4b3_d_idempotent", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", "--force", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", "--force", stdout=StringIO())
        out = StringIO()
        call_command("assign_treasury_role", user.username, "submitter", stdout=out)  # already has it
        self.assertIn("already has", out.getvalue())

    def test_force_with_remove_raises_command_error(self):
        user = make_user(username="o4b3_d_force_remove", is_staff=True)
        with self.assertRaises(CommandError):
            call_command(
                "assign_treasury_role", user.username, "submitter", "--remove", "--force",
                stdout=StringIO(),
            )

    def test_force_without_concentration_has_no_effect_and_is_not_marked_used(self):
        # --force on a FIRST grant (no concentration) must succeed exactly
        # like a normal grant, and force_used must be False since nothing
        # was actually overridden.
        user = make_user(username="o4b3_d_force_noop", is_staff=True)
        call_command(
            "assign_treasury_role", user.username, "submitter", "--force", stdout=StringIO(),
        )
        row = AuditLog.objects.get(event_type="treasury.permission_granted")
        self.assertFalse(row.detail["force_used"])


# ─────────────────────────────────────────────
# E. Django Admin
# ─────────────────────────────────────────────

class AdminBlockingDisabledPreservesO4b2Tests(TestCase):
    """blocking=False must leave O.4b-2 behavior completely unchanged."""

    def setUp(self):
        self.actor = make_user(
            username="o4b3_e_off_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b3_e_off_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=False)
    def test_superuser_can_still_grant_multiple_roles(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(len(held_treasury_codenames(self.target)), 2)


@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
class AdminBlockingEnabledTests(TestCase):

    def setUp(self):
        self.actor = make_user(
            username="o4b3_e_on_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b3_e_on_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_first_grant_still_allowed(self):
        perm = _treasury_permission("can_submit_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), ("can_submit_treasury_request",))

    def test_second_grant_blocked_server_side(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        self.target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        # Only the pre-existing permission remains — the new one was reverted.
        self.assertEqual(held_treasury_codenames(self.target), ("can_submit_treasury_request",))

    def test_direct_post_injection_cannot_bypass_block(self):
        # Even a raw POST with both ids explicitly listed (as a curl/
        # scripted attempt might do, bypassing whatever the rendered
        # widget would have offered) is still reverted server-side.
        perm1 = _treasury_permission("can_execute_treasury_request")
        self.target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_recover_treasury_execution")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        self.client.post(_user_change_url(self.target), data=payload)
        self.target.refresh_from_db()
        self.assertEqual(
            held_treasury_codenames(self.target), ("can_execute_treasury_request",),
        )

    def test_revoke_still_works_when_blocking_enabled(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        self.target.user_permissions.add(perm1)
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), ())

    def test_blocked_attempt_writes_concentration_blocked_event(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        self.target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        self.client.post(_user_change_url(self.target), data=payload)

        row = AuditLog.objects.get(event_type="treasury.role_concentration_blocked")
        self.assertEqual(row.detail["target_user_id"], self.target.pk)
        self.assertEqual(row.detail["attempted_codename"], "can_review_treasury_request")
        self.assertEqual(row.detail["via"], "django_admin")
        self.assertEqual(row.detail["actor"], self.actor.pk)
        self.assertEqual(row.detail["outcome"], "blocked")

        bae = BrokerAuditEvent.objects.get(event_type="treasury.role_concentration_blocked")
        self.assertEqual(bae.severity, "WARNING")

    def test_blocked_attempt_writes_no_false_granted_event(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        self.target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(
            AuditLog.objects.filter(
                event_type="treasury.permission_granted", detail__codename="can_review_treasury_request",
            ).count(),
            0,
        )

    def test_staff_non_superuser_still_cannot_manage_treasury_permissions(self):
        # O.4b-2 protection must survive O.4b-3's edits to save_related().
        staff = make_user(username="o4b3_e_staff_still_blocked", is_staff=True)
        _grant_user_admin_access(staff)
        client = Client()
        client.force_login(staff)
        perm = _treasury_permission("can_submit_treasury_request")
        payload = _base_change_payload(self.target, actor=staff, user_permission_pks=[perm.pk])
        resp = client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), ())

    def test_superuser_self_grant_still_blocked(self):
        # O.4b-2's self-grant prevention must survive too.
        perm = _treasury_permission("can_submit_treasury_request")
        payload = _base_change_payload(self.actor, actor=self.actor, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.actor), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.actor.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.actor), ())

    def test_is_superuser_hardening_still_intact(self):
        staff = make_user(username="o4b3_e_su_hardening", is_staff=True)
        _grant_user_admin_access(staff)
        client = Client()
        client.force_login(staff)
        payload = _base_change_payload(self.target, actor=staff, is_superuser=True)
        resp = client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_superuser)


# ─────────────────────────────────────────────
# G. Invariants
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
class InvariantsTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()
        self.actor = make_user(
            username="o4b3_inv_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b3_inv_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_no_wallet_ledger_or_treasury_request_mutation_on_block(self):
        perm1 = _treasury_permission("can_submit_treasury_request")
        self.target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_review_treasury_request")
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        self.client.post(_user_change_url(self.target), data=payload)

        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_command_block_causes_no_financial_mutation(self):
        call_command("assign_treasury_role", self.target.username, "submitter", stdout=StringIO())
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        try:
            call_command(
                "assign_treasury_role", self.target.username, "reviewer", stdout=StringIO(),
            )
        except CommandError:
            pass
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_no_new_treasury_permissions_created(self):
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

    def test_treasury_permissions_module_never_imports_financial_functions(self):
        import ast
        import inspect
        import textwrap

        import simulator.treasury_permissions as module

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
