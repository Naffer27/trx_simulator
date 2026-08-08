# simulator/tests/test_o4b2_treasury_hardened_user_admin.py
"""
Microbloque O.4b-2 — TreasuryHardenedUserAdmin.

Covers simulator/admin.py::TreasuryHardenedUserAdmin, which replaces
Django's stock auth.UserAdmin registration with one that:

  1. Restricts the four Treasury permissions (user_permissions field)
     and is_superuser to superuser-only editing, server-side —
     formfield_for_manytomany()'s queryset exclusion (Django rejects a
     raw POST containing an excluded permission id, not just hides a
     checkbox) and get_form()'s disabled=True on is_superuser for
     non-superusers.
  2. Even a superuser cannot change either of those two things on
     THEIR OWN account through this form (self-grant prevention).
  3. Every real Treasury permission grant/revoke performed by a
     superuser editing someone else is audited via the exact event
     constants introduced in O.4b-1 (EV_TREASURY_PERMISSION_GRANTED/
     REVOKED), with via="django_admin".
  4. Non-Treasury permissions remain editable normally by anyone who
     already holds auth.change_user — no change in blast radius there.

Every test drives a real POST through the real admin URL (Django test
Client against the real URLconf) — GET/hidden-checkbox behavior is
never relied on as the proof; raw POST payloads are constructed
directly, including attempts to inject a Treasury permission id or an
is_superuser=on value that the rendered form would never have offered,
to prove the protection is server-side validation, not UI cosmetics.
"""
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse

from simulator.admin import TREASURY_PERMISSION_CODENAMES
from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer, TreasuryOperationRequest,
    WalletTransaction,
)

from .factories import make_user, make_wallet


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


def _all_treasury_permission_pks():
    return set(
        Permission.objects.filter(
            codename__in=TREASURY_PERMISSION_CODENAMES,
        ).values_list("pk", flat=True)
    )


_UNSET = object()


def _base_change_payload(user, *, actor, is_active=True, is_staff=None, is_superuser=None,
                          user_permission_pks=_UNSET, **overrides):
    """
    Builds a realistic admin change-form POST payload for `user`, as
    submitted by `actor`.

    A real browser always resubmits the FULL current widget selection —
    including permissions the operator never touched — so when
    user_permission_pks is left unset, this defaults to `user`'s
    CURRENT permissions, minus the four Treasury ones whenever `actor`
    would not actually see them as selectable checkboxes (non-superuser,
    or editing themselves — the exact same rule
    formfield_for_manytomany() applies). Omitting this filtering would
    make an "unrelated field" test accidentally submit a permission id
    the field's queryset excludes, failing form validation for a reason
    unrelated to what the test is checking — or, if actor is editing
    themselves, silently wiping their OWN unrelated permissions (e.g.
    auth.change_user) because an omitted multi-select key means "clear
    everything" to Django, not "leave unchanged".

    Pass user_permission_pks explicitly (including `[]`) to override
    this default when a test wants to submit a specific / injected set.
    """
    if is_staff is None:
        is_staff = user.is_staff
    if is_superuser is None:
        is_superuser = user.is_superuser

    if user_permission_pks is _UNSET:
        pks = set(user.user_permissions.values_list("pk", flat=True))
        is_self_edit = user.pk == actor.pk
        if not actor.is_superuser or is_self_edit:
            pks -= _all_treasury_permission_pks()
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


def _treasury_pks_of(user):
    return set(
        user.user_permissions.filter(
            codename__in=TREASURY_PERMISSION_CODENAMES,
        ).values_list("pk", flat=True)
    )


class SelfAssignmentBlockedTests(TestCase):
    """A non-superuser staff with auth.change_user cannot self-assign
    any of the four Treasury permissions, by raw POST injection."""

    def setUp(self):
        self.actor = make_user(username="o4b2_self_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_self_assign_each_treasury_permission_rejected(self):
        for codename in TREASURY_PERMISSION_CODENAMES:
            with self.subTest(codename=codename):
                perm = _treasury_permission(codename)
                payload = _base_change_payload(
                    self.actor, actor=self.actor, user_permission_pks=[perm.pk],
                )
                resp = self.client.post(_user_change_url(self.actor), data=payload)
                self.assertEqual(resp.status_code, 200)  # re-rendered with a form error
                self.actor.refresh_from_db()
                self.assertFalse(
                    self.actor.user_permissions.filter(pk=perm.pk).exists(),
                )

    def test_self_revoke_own_treasury_permission_has_no_effect(self):
        # Pre-existing Treasury permission (granted by some other means,
        # e.g. the CLI) must survive an unrelated self-edit untouched —
        # not just "cannot be added," but "cannot be removed" either,
        # since it is entirely excluded from this actor's own form.
        perm = _treasury_permission("can_review_treasury_request")
        self.actor.user_permissions.add(perm)
        payload = _base_change_payload(self.actor, actor=self.actor, first_name="Changed")
        resp = self.client.post(_user_change_url(self.actor), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.user_permissions.filter(pk=perm.pk).exists())
        self.assertEqual(self.actor.first_name, "Changed")  # unrelated field DID save


class AssignToOtherUserBlockedTests(TestCase):
    """A non-superuser staff cannot grant or revoke Treasury permissions
    for ANOTHER user either."""

    def setUp(self):
        self.actor = make_user(username="o4b2_other_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.target = make_user(username="o4b2_other_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_grant_treasury_to_other_user_rejected(self):
        perm = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.user_permissions.filter(pk=perm.pk).exists())

    def test_revoke_other_users_existing_treasury_permission_has_no_effect(self):
        perm = _treasury_permission("can_recover_treasury_execution")
        self.target.user_permissions.add(perm)
        payload = _base_change_payload(self.target, actor=self.actor, last_name="Changed")
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.user_permissions.filter(pk=perm.pk).exists())
        self.assertEqual(self.target.last_name, "Changed")

    def test_no_audit_event_for_blocked_attempt(self):
        perm = _treasury_permission("can_submit_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 0,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.permission_granted").count(), 0,
        )


class IsSuperuserSelfElevationBlockedTests(TestCase):

    def setUp(self):
        self.actor = make_user(username="o4b2_su_self_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_self_elevate_is_superuser_rejected(self):
        payload = _base_change_payload(self.actor, actor=self.actor, is_superuser=True)
        resp = self.client.post(_user_change_url(self.actor), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.actor.refresh_from_db()
        self.assertFalse(self.actor.is_superuser)


class IsSuperuserOtherElevationBlockedTests(TestCase):

    def setUp(self):
        self.actor = make_user(username="o4b2_su_other_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.target = make_user(username="o4b2_su_other_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_elevate_other_users_is_superuser_rejected(self):
        payload = _base_change_payload(self.target, actor=self.actor, is_superuser=True)
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_superuser)


class IsSuperuserRetirementBlockedTests(TestCase):

    def setUp(self):
        self.actor = make_user(username="o4b2_su_retire_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.target = make_user(
            username="o4b2_su_retire_target", is_staff=True, is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.actor)

    def test_retire_other_users_is_superuser_has_no_effect(self):
        # is_superuser omitted from the payload entirely (as an
        # unchecked checkbox would be) — a disabled field ignores this
        # too and keeps the instance's current value.
        payload = _base_change_payload(
            self.target, actor=self.actor, is_superuser=False, first_name="Changed",
        )
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_superuser)
        self.assertEqual(self.target.first_name, "Changed")


class SuperuserGrantRevokeOtherUserTests(TestCase):
    """The one path where Treasury permission changes actually succeed:
    a superuser editing someone else."""

    def setUp(self):
        self.actor = make_user(
            username="o4b2_su_grant_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b2_su_grant_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_grant_treasury_permission_succeeds(self):
        perm = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.user_permissions.filter(pk=perm.pk).exists())

    def test_grant_writes_auditlog_and_brokerauditevent(self):
        perm = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.target), data=payload)

        row = AuditLog.objects.get(event_type="treasury.permission_granted")
        self.assertEqual(row.detail["target_user_id"], self.target.pk)
        self.assertEqual(row.detail["target_username"], self.target.username)
        self.assertEqual(row.detail["role"], "executor")
        self.assertEqual(row.detail["codename"], "can_execute_treasury_request")
        self.assertEqual(row.detail["granted_by"], self.actor.pk)
        self.assertEqual(row.detail["via"], "django_admin")
        self.assertFalse(row.detail["is_self_grant"])
        self.assertEqual(row.detail["resulting_treasury_permission_count"], 1)

        bae = BrokerAuditEvent.objects.get(event_type="treasury.permission_granted")
        self.assertEqual(bae.category, "ADMIN")
        self.assertEqual(bae.severity, "WARNING")
        self.assertEqual(bae.metadata, row.detail)

    def test_revoke_treasury_permission_succeeds_and_is_audited(self):
        perm = _treasury_permission("can_recover_treasury_execution")
        self.target.user_permissions.add(perm)

        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertFalse(self.target.user_permissions.filter(pk=perm.pk).exists())

        row = AuditLog.objects.get(event_type="treasury.permission_revoked")
        self.assertEqual(row.detail["codename"], "can_recover_treasury_execution")
        self.assertEqual(row.detail["role"], "recoverer")
        self.assertEqual(row.detail["via"], "django_admin")
        self.assertEqual(row.detail["resulting_treasury_permission_count"], 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.permission_revoked").count(), 1,
        )

    def test_grant_multiple_roles_reports_correct_resulting_count(self):
        # Both permissions are added together in one form submission (one
        # save_m2m() call) — resulting_treasury_permission_count reflects
        # the true final DB state at audit time for both events, not a
        # fabricated incremental count.
        perm1 = _treasury_permission("can_submit_treasury_request")
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.actor, user_permission_pks=[perm1.pk, perm2.pk],
        )
        self.client.post(_user_change_url(self.target), data=payload)
        rows = AuditLog.objects.filter(event_type="treasury.permission_granted").order_by("id")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {r.detail["resulting_treasury_permission_count"] for r in rows}, {2},
        )


class SuperuserSelfGrantBlockedTests(TestCase):
    """Even a superuser cannot self-assign Treasury permissions through
    this form (O.4b Fase 0 decision 4)."""

    def setUp(self):
        self.actor = make_user(
            username="o4b2_su_self_grant_actor", is_staff=True, is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.actor)

    def test_superuser_self_assign_treasury_rejected(self):
        for codename in TREASURY_PERMISSION_CODENAMES:
            with self.subTest(codename=codename):
                perm = _treasury_permission(codename)
                payload = _base_change_payload(
                    self.actor, actor=self.actor, user_permission_pks=[perm.pk],
                )
                resp = self.client.post(_user_change_url(self.actor), data=payload)
                self.assertEqual(resp.status_code, 200)
                self.actor.refresh_from_db()
                self.assertFalse(
                    self.actor.user_permissions.filter(pk=perm.pk).exists(),
                )

    def test_superuser_self_revoke_existing_treasury_has_no_effect(self):
        perm = _treasury_permission("can_submit_treasury_request")
        self.actor.user_permissions.add(perm)
        payload = _base_change_payload(self.actor, actor=self.actor, first_name="Changed")
        resp = self.client.post(_user_change_url(self.actor), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.user_permissions.filter(pk=perm.pk).exists())

    def test_superuser_self_grant_writes_no_audit_event(self):
        perm = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(self.actor, actor=self.actor, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.actor), data=payload)
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 0,
        )


class NoOpNotAuditedTests(TestCase):

    def setUp(self):
        self.actor = make_user(
            username="o4b2_noop_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b2_noop_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_resubmitting_same_permission_set_writes_no_event(self):
        perm = _treasury_permission("can_execute_treasury_request")
        self.target.user_permissions.add(perm)
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(
            AuditLog.objects.filter(event_type__startswith="treasury.permission").count(), 0,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type__startswith="treasury.permission",
            ).count(), 0,
        )


class NonTreasuryPermissionsUnaffectedTests(TestCase):
    """Blast radius check: everything NOT Treasury-specific keeps working
    exactly as it always did for a non-superuser with auth.change_user."""

    def setUp(self):
        self.actor = make_user(username="o4b2_nontreasury_actor", is_staff=True)
        _grant_user_admin_access(self.actor)
        self.target = make_user(username="o4b2_nontreasury_target", is_staff=True)
        self.client = Client()
        self.client.force_login(self.actor)

    def test_non_treasury_permission_grant_to_other_user_succeeds(self):
        perm = _user_content_type_permission("view_user")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.user_permissions.filter(pk=perm.pk).exists())

    def test_non_treasury_permission_grant_to_self_succeeds(self):
        perm = _user_content_type_permission("view_user")
        payload = _base_change_payload(
            self.actor, actor=self.actor,
            user_permission_pks=list(self.actor.user_permissions.values_list("pk", flat=True)) + [perm.pk],
        )
        resp = self.client.post(_user_change_url(self.actor), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.user_permissions.filter(pk=perm.pk).exists())

    def test_ordinary_field_edit_still_works(self):
        payload = _base_change_payload(self.target, actor=self.actor, email="new@example.com")
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "new@example.com")


class InvariantsTests(TestCase):

    def setUp(self):
        self.actor = make_user(
            username="o4b2_inv_actor", is_staff=True, is_superuser=True,
        )
        self.target = make_user(username="o4b2_inv_target", is_staff=True)
        self.wallet = make_wallet()
        self.client = Client()
        self.client.force_login(self.actor)

    def test_no_wallet_or_ledger_mutation_from_permission_grant(self):
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        perm = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(self.target, actor=self.actor, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_admin_module_never_imports_wallet_or_treasury_service_functions(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import TreasuryHardenedUserAdmin

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed", "execute_treasury_request",
        }
        forbidden_imports = {"wallet_ledger", "treasury_requests", "treasury_execution_recovery"}

        for fn in (
            TreasuryHardenedUserAdmin.get_form,
            TreasuryHardenedUserAdmin.formfield_for_manytomany,
            TreasuryHardenedUserAdmin.save_related,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
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
            self.assertFalse(forbidden_calls & called, f"{fn.__name__}: {forbidden_calls & called}")
            self.assertFalse(forbidden_imports & imported, f"{fn.__name__}: {forbidden_imports & imported}")

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
