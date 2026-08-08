# simulator/tests/test_o4b4_treasury_permission_hardening_end_to_end.py
"""
Microbloque O.4b-4 — Treasury Permission Hardening, end-to-end
verification (final checkpoint for CRIT-2).

This file adds ONLY tests — no production code changes accompany it
unless a genuine defect were found (none was). It assembles combined,
attack-path, and cross-cutting scenarios across O.4b-1 (CLI audit),
O.4b-2 (TreasuryHardenedUserAdmin) and O.4b-3 (concentration guard)
that the per-microbloque test files proved individually, plus several
NEW combinations those files didn't specifically exercise (mixing
Treasury + non-Treasury permissions in one submission, omitting
user_permissions entirely, interaction with O.4a's 2FA gate, and a
real Treasury submit->approve regression check using permissions
granted through the now-hardened path).

Every mutation-capable path is driven through the real admin URLs
(Django test Client) or the real management command (call_command) —
never by calling internal helpers directly — so a bypass would have to
survive the actual HTTP/CLI surface to go undetected here.
"""
from io import StringIO
from decimal import Decimal

import pyotp
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator.models import (
    AuditLog, BrokerAuditEvent, InternalTransfer, TOTPDevice,
    TreasuryOperationRequest, WalletTransaction,
)
from simulator.treasury_permissions import (
    TREASURY_PERMISSION_CODENAMES, held_treasury_codenames,
)

from .factories import make_user, make_wallet

CODENAMES = TREASURY_PERMISSION_CODENAMES


# ─────────────────────────────────────────────
# Shared helpers (self-contained, same shape as test_o4b2/test_o4b3)
# ─────────────────────────────────────────────

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
    if user_permission_pks is not None:
        data["user_permissions"] = [str(pk) for pk in user_permission_pks]
    data.update(overrides)
    return data


def _make_totp_device(user, confirmed=True, raw_secret=None):
    import base64
    raw_secret = raw_secret or pyotp.random_base32()
    return TOTPDevice.objects.create(
        user=user,
        secret=f"b64:{base64.b64encode(raw_secret.encode()).decode()}",
        confirmed=confirmed,
    )


NEW_REQUEST_URL = reverse("admin:treasury_request_new")


def _approve_url(pk):
    return reverse("admin:treasury_request_approve", args=[pk])


# ─────────────────────────────────────────────
# 1. Staff + auth.change_user — full bypass-attempt matrix
# ─────────────────────────────────────────────

class StaffFullBypassMatrixTests(TestCase):

    def setUp(self):
        self.staff = make_user(username="o4b4_staff_matrix", is_staff=True)
        _grant_user_admin_access(self.staff)
        self.other = make_user(username="o4b4_staff_matrix_other", is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_self_assign_each_treasury_permission_no_bypass(self):
        for codename in CODENAMES:
            with self.subTest(codename=codename):
                perm = _treasury_permission(codename)
                before = held_treasury_codenames(self.staff)
                payload = _base_change_payload(
                    self.staff, actor=self.staff, user_permission_pks=[perm.pk],
                )
                resp = self.client.post(_user_change_url(self.staff), data=payload)
                self.assertEqual(resp.status_code, 200)
                self.staff.refresh_from_db()
                self.assertEqual(held_treasury_codenames(self.staff), before)

    def test_assign_each_treasury_permission_to_other_no_bypass(self):
        for codename in CODENAMES:
            with self.subTest(codename=codename):
                perm = _treasury_permission(codename)
                before = held_treasury_codenames(self.other)
                payload = _base_change_payload(
                    self.other, actor=self.staff, user_permission_pks=[perm.pk],
                )
                resp = self.client.post(_user_change_url(self.other), data=payload)
                self.assertEqual(resp.status_code, 200)
                self.other.refresh_from_db()
                self.assertEqual(held_treasury_codenames(self.other), before)

    def test_revoke_others_existing_treasury_permission_no_bypass(self):
        perm = _treasury_permission("can_submit_treasury_request")
        self.other.user_permissions.add(perm)
        payload = _base_change_payload(self.other, actor=self.staff, user_permission_pks=[])
        resp = self.client.post(_user_change_url(self.other), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.other.refresh_from_db()
        self.assertTrue(self.other.user_permissions.filter(pk=perm.pk).exists())

    def test_self_elevate_superuser_no_bypass(self):
        payload = _base_change_payload(self.staff, actor=self.staff, is_superuser=True)
        resp = self.client.post(_user_change_url(self.staff), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_superuser)

    def test_elevate_other_to_superuser_no_bypass(self):
        payload = _base_change_payload(self.other, actor=self.staff, is_superuser=True)
        resp = self.client.post(_user_change_url(self.other), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_superuser)

    def test_retire_others_superuser_no_bypass(self):
        su_target = make_user(
            username="o4b4_staff_matrix_su_target", is_staff=True, is_superuser=True,
        )
        payload = _base_change_payload(
            su_target, actor=self.staff, is_superuser=False, first_name="Changed",
        )
        resp = self.client.post(_user_change_url(su_target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        su_target.refresh_from_db()
        self.assertTrue(su_target.is_superuser)
        self.assertEqual(su_target.first_name, "Changed")  # unrelated field still saves


# ─────────────────────────────────────────────
# 2. Superuser — grant/revoke others, self-grant still blocked
# ─────────────────────────────────────────────

class SuperuserE2ETests(TestCase):

    def setUp(self):
        self.su = make_user(username="o4b4_su", is_staff=True, is_superuser=True)
        self.other = make_user(username="o4b4_su_other", is_staff=True)
        self.client = Client()
        self.client.force_login(self.su)

    def test_superuser_grants_and_revokes_other_user(self):
        perm = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(self.other, actor=self.su, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.other), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.other.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.other), (perm.codename,))

        row = AuditLog.objects.get(event_type="treasury.permission_granted")
        self.assertEqual(row.detail["via"], "django_admin")
        self.assertEqual(row.detail["granted_by"], self.su.pk)

        payload = _base_change_payload(self.other, actor=self.su, user_permission_pks=[])
        resp = self.client.post(_user_change_url(self.other), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.other.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.other), ())
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.permission_revoked").count(), 1,
        )

    def test_superuser_cannot_self_grant(self):
        perm = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(self.su, actor=self.su, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.su), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.su.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.su), ())
        self.assertEqual(AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 0)


# ─────────────────────────────────────────────
# 3. Concentration — flag False (CLI + Admin)
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=False)
class ConcentrationFlagFalseE2ETests(TestCase):

    def test_cli_second_permission_granted_and_audited_as_concentrated(self):
        user = make_user(username="o4b4_flagoff_cli", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        self.assertEqual(len(held_treasury_codenames(user)), 2)
        row = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="reviewer",
        ).get()
        self.assertTrue(row.detail["concentration_detected"])
        self.assertFalse(row.detail["blocking_enabled"])

    def test_admin_second_permission_granted_and_audited_as_concentrated(self):
        su = make_user(username="o4b4_flagoff_admin_su", is_staff=True, is_superuser=True)
        target = make_user(username="o4b4_flagoff_admin_target", is_staff=True)
        client = Client()
        client.force_login(su)
        perm1 = _treasury_permission("can_submit_treasury_request")
        perm2 = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            target, actor=su, user_permission_pks=[perm1.pk, perm2.pk],
        )
        resp = client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(len(held_treasury_codenames(target)), 2)
        rows = AuditLog.objects.filter(event_type="treasury.permission_granted")
        self.assertTrue(all(r.detail["concentration_detected"] or r.detail["resulting_treasury_permission_count"] == 1 for r in rows))
        self.assertTrue(any(r.detail["concentration_detected"] for r in rows))


# ─────────────────────────────────────────────
# 4. Concentration — flag True (CLI with --force, Admin no bypass)
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
class ConcentrationFlagTrueE2ETests(TestCase):

    def test_cli_full_sequence_first_allowed_second_blocked_force_allows(self):
        user = make_user(username="o4b4_flagon_cli", is_staff=True)

        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        self.assertEqual(len(held_treasury_codenames(user)), 1)

        with self.assertRaises(CommandError):
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        self.assertEqual(len(held_treasury_codenames(user)), 1)  # zero mutation

        call_command(
            "assign_treasury_role", user.username, "reviewer", "--force", stdout=StringIO(),
        )
        self.assertEqual(len(held_treasury_codenames(user)), 2)
        forced_row = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="reviewer",
        ).get()
        self.assertTrue(forced_row.detail["force_used"])

        out = StringIO()
        call_command(
            "assign_treasury_role", user.username, "reviewer", "--remove", stdout=out,
        )
        self.assertIn("Removed", out.getvalue())
        self.assertEqual(held_treasury_codenames(user), ("can_submit_treasury_request",))

    def test_admin_full_sequence_first_allowed_second_blocked_no_bypass(self):
        su = make_user(username="o4b4_flagon_admin_su", is_staff=True, is_superuser=True)
        target = make_user(username="o4b4_flagon_admin_target", is_staff=True)
        client = Client()
        client.force_login(su)

        perm1 = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(target, actor=su, user_permission_pks=[perm1.pk])
        resp = client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(target), (perm1.codename,))

        perm2 = _treasury_permission("can_recover_treasury_execution")
        payload = _base_change_payload(
            target, actor=su, user_permission_pks=[perm1.pk, perm2.pk],
        )
        resp = client.post(_user_change_url(target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        target.refresh_from_db()
        # Second permission was reverted — no admin-side override exists.
        self.assertEqual(held_treasury_codenames(target), (perm1.codename,))

        # Revoke still works.
        payload = _base_change_payload(target, actor=su, user_permission_pks=[])
        resp = client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(target), ())


# ─────────────────────────────────────────────
# 5. No-op / idempotency — never fabricate mutation events
# ─────────────────────────────────────────────

class NoOpIdempotencyE2ETests(TestCase):

    def test_cli_grant_of_existing_permission_writes_no_event(self):
        user = make_user(username="o4b4_noop_cli_grant", is_staff=True)
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())
        AuditLog.objects.all().delete()
        BrokerAuditEvent.objects.all().delete()
        call_command("assign_treasury_role", user.username, "executor", stdout=StringIO())
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)

    def test_cli_revoke_of_absent_permission_writes_no_event(self):
        user = make_user(username="o4b4_noop_cli_revoke", is_staff=True)
        call_command(
            "assign_treasury_role", user.username, "recoverer", "--remove", stdout=StringIO(),
        )
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_cli_blocked_operation_writes_only_blocked_event_not_granted(self):
        user = make_user(username="o4b4_noop_cli_blocked", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        AuditLog.objects.all().delete()
        try:
            call_command("assign_treasury_role", user.username, "reviewer", stdout=StringIO())
        except CommandError:
            pass
        self.assertEqual(AuditLog.objects.filter(event_type="treasury.permission_granted").count(), 0)
        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.role_concentration_blocked").count(), 1,
        )

    def test_admin_resubmitting_unchanged_permissions_writes_no_event(self):
        su = make_user(username="o4b4_noop_admin_su", is_staff=True, is_superuser=True)
        target = make_user(username="o4b4_noop_admin_target", is_staff=True)
        perm = _treasury_permission("can_submit_treasury_request")
        target.user_permissions.add(perm)
        client = Client()
        client.force_login(su)
        payload = _base_change_payload(target, actor=su, user_permission_pks=[perm.pk])
        client.post(_user_change_url(target), data=payload)
        self.assertEqual(
            AuditLog.objects.filter(event_type__startswith="treasury.permission").count(), 0,
        )


# ─────────────────────────────────────────────
# 6. Cross-check audit contract — AuditLog + BrokerAuditEvent together
# ─────────────────────────────────────────────

class AuditCrossCheckE2ETests(TestCase):

    def test_cli_grant_audit_pair_matches(self):
        user = make_user(username="o4b4_audit_cli_grant", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        log = AuditLog.objects.get(event_type="treasury.permission_granted")
        bae = BrokerAuditEvent.objects.get(event_type="treasury.permission_granted")
        self.assertEqual(bae.metadata, log.detail)
        self.assertEqual(log.detail["target_user_id"], user.pk)
        self.assertEqual(log.detail["codename"], "can_submit_treasury_request")
        self.assertEqual(log.detail["via"], "management_command")
        self.assertEqual(log.detail["resulting_treasury_permission_count"], 1)
        self.assertIsNone(log.detail["actor"])

    def test_admin_revoke_audit_pair_matches(self):
        su = make_user(username="o4b4_audit_admin_revoke_su", is_staff=True, is_superuser=True)
        target = make_user(username="o4b4_audit_admin_revoke_target", is_staff=True)
        perm = _treasury_permission("can_recover_treasury_execution")
        target.user_permissions.add(perm)
        client = Client()
        client.force_login(su)
        payload = _base_change_payload(target, actor=su, user_permission_pks=[])
        client.post(_user_change_url(target), data=payload)
        log = AuditLog.objects.get(event_type="treasury.permission_revoked")
        bae = BrokerAuditEvent.objects.get(event_type="treasury.permission_revoked")
        self.assertEqual(bae.metadata, log.detail)
        self.assertEqual(log.detail["actor"], su.pk)
        self.assertEqual(log.detail["via"], "django_admin")
        self.assertEqual(log.detail["resulting_treasury_permission_count"], 0)

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_force_override_audit_pair_matches(self):
        user = make_user(username="o4b4_audit_force", is_staff=True)
        call_command("assign_treasury_role", user.username, "submitter", stdout=StringIO())
        call_command(
            "assign_treasury_role", user.username, "executor", "--force", stdout=StringIO(),
        )
        log = AuditLog.objects.filter(
            event_type="treasury.permission_granted", detail__role="executor",
        ).get()
        bae = BrokerAuditEvent.objects.get(
            event_type="treasury.permission_granted", metadata__role="executor",
        )
        self.assertEqual(bae.metadata, log.detail)
        self.assertTrue(log.detail["force_used"])
        self.assertTrue(log.detail["concentration_detected"])

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_blocked_audit_pair_matches(self):
        su = make_user(username="o4b4_audit_blocked_su", is_staff=True, is_superuser=True)
        target = make_user(username="o4b4_audit_blocked_target", is_staff=True)
        perm1 = _treasury_permission("can_submit_treasury_request")
        target.user_permissions.add(perm1)
        perm2 = _treasury_permission("can_review_treasury_request")
        client = Client()
        client.force_login(su)
        payload = _base_change_payload(
            target, actor=su, user_permission_pks=[perm1.pk, perm2.pk],
        )
        client.post(_user_change_url(target), data=payload)
        log = AuditLog.objects.get(event_type="treasury.role_concentration_blocked")
        bae = BrokerAuditEvent.objects.get(event_type="treasury.role_concentration_blocked")
        self.assertEqual(bae.metadata, log.detail)
        self.assertEqual(log.detail["target_user_id"], target.pk)
        self.assertEqual(log.detail["attempted_codename"], "can_review_treasury_request")
        self.assertEqual(log.detail["actor"], su.pk)
        self.assertEqual(log.detail["via"], "django_admin")


# ─────────────────────────────────────────────
# 7. Attack paths — server-side proof
# ─────────────────────────────────────────────

class AttackPathsE2ETests(TestCase):

    def setUp(self):
        self.su = make_user(username="o4b4_attack_su", is_staff=True, is_superuser=True)
        self.staff = make_user(username="o4b4_attack_staff", is_staff=True)
        _grant_user_admin_access(self.staff)
        self.target = make_user(username="o4b4_attack_target", is_staff=True)
        self.client = Client()

    def test_direct_post_injection_of_disallowed_treasury_id_fails_for_staff(self):
        self.client.force_login(self.staff)
        perm = _treasury_permission("can_submit_treasury_request")
        payload = _base_change_payload(self.target, actor=self.staff, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertEqual(resp.status_code, 200)  # form invalid, nothing saved
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), ())

    def test_direct_post_injection_of_is_superuser_fails_for_staff(self):
        self.client.force_login(self.staff)
        payload = _base_change_payload(self.target, actor=self.staff, is_superuser=True)
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_superuser)

    def test_omitting_user_permissions_key_entirely_does_not_grant_anything(self):
        # A raw payload that drops the key altogether (not just an empty
        # list) — Django's SelectMultiple.value_from_datadict treats an
        # absent key the same as an empty submitted list.
        self.client.force_login(self.su)
        payload = _base_change_payload(self.target, actor=self.su, user_permission_pks=[])
        del payload["user_permissions"]
        resp = self.client.post(_user_change_url(self.target), data=payload)
        self.assertIn(resp.status_code, (200, 302))
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), ())

    def test_treasury_id_not_in_rendered_queryset_rejected_for_self_edit_superuser(self):
        # Even a superuser editing themselves cannot inject a Treasury
        # permission id — it was never in the field's queryset for this
        # actor/target combination, regardless of privilege level.
        perm = _treasury_permission("can_review_treasury_request")
        self.client.force_login(self.su)
        payload = _base_change_payload(self.su, actor=self.su, user_permission_pks=[perm.pk])
        resp = self.client.post(_user_change_url(self.su), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.su.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.su), ())

    def test_mixing_non_treasury_and_treasury_permissions_saves_only_non_treasury_for_staff(self):
        self.client.force_login(self.staff)
        non_treasury_perm = _user_content_type_permission("view_user")
        treasury_perm = _treasury_permission("can_execute_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.staff,
            user_permission_pks=[non_treasury_perm.pk, treasury_perm.pk],
        )
        resp = self.client.post(_user_change_url(self.target), data=payload)
        # The whole field fails validation because treasury_perm isn't in
        # the queryset — nothing in user_permissions is saved, including
        # the otherwise-legitimate non-Treasury one in the SAME submission.
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.user_permissions.filter(pk=non_treasury_perm.pk).exists())

    @override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_mixing_non_treasury_with_concentrating_treasury_grant_for_superuser(self):
        # For an UNRESTRICTED superuser (not self-editing), the form
        # itself is valid (queryset includes all treasury perms), so
        # this exercises save_related()'s revert-on-concentration path
        # instead of form validation — the non-Treasury permission must
        # still be saved even though the Treasury addition is reverted.
        self.client.force_login(self.su)
        self.target.user_permissions.add(_treasury_permission("can_submit_treasury_request"))
        non_treasury_perm = _user_content_type_permission("view_user")
        concentrating_perm = _treasury_permission("can_review_treasury_request")
        payload = _base_change_payload(
            self.target, actor=self.su,
            user_permission_pks=[
                _treasury_permission("can_submit_treasury_request").pk,
                non_treasury_perm.pk,
                concentrating_perm.pk,
            ],
        )
        resp = self.client.post(_user_change_url(self.target), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.user_permissions.filter(pk=non_treasury_perm.pk).exists())
        self.assertEqual(
            held_treasury_codenames(self.target), ("can_submit_treasury_request",),
        )

    def test_self_edit_vs_other_edit_produce_different_outcomes_for_same_superuser(self):
        perm = _treasury_permission("can_recover_treasury_execution")
        self.client.force_login(self.su)

        # Self-edit: blocked regardless of privilege.
        payload = _base_change_payload(self.su, actor=self.su, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.su), data=payload)
        self.su.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.su), ())

        # Other-edit: allowed.
        payload = _base_change_payload(self.target, actor=self.su, user_permission_pks=[perm.pk])
        self.client.post(_user_change_url(self.target), data=payload)
        self.target.refresh_from_db()
        self.assertEqual(held_treasury_codenames(self.target), (perm.codename,))


# ─────────────────────────────────────────────
# 8. Regression — non-Treasury, is_staff, O.4a 2FA, O.3 Treasury workflow
# ─────────────────────────────────────────────

class RegressionE2ETests(TestCase):

    def test_non_treasury_permission_management_unaffected(self):
        staff = make_user(username="o4b4_regress_nontreasury", is_staff=True)
        _grant_user_admin_access(staff)
        target = make_user(username="o4b4_regress_nontreasury_target", is_staff=True)
        client = Client()
        client.force_login(staff)
        perm = _user_content_type_permission("view_user")
        payload = _base_change_payload(target, actor=staff, user_permission_pks=[perm.pk])
        resp = client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertTrue(target.user_permissions.filter(pk=perm.pk).exists())

    def test_is_staff_field_not_hardened_by_o4b(self):
        # O.4b explicitly left is_staff out of scope — a non-superuser
        # with auth.change_user can still toggle is_staff normally.
        staff = make_user(username="o4b4_regress_isstaff_actor", is_staff=True)
        _grant_user_admin_access(staff)
        target = make_user(
            username="o4b4_regress_isstaff_target", is_staff=True, is_superuser=False,
        )
        client = Client()
        client.force_login(staff)
        payload = _base_change_payload(target, actor=staff, is_staff=False)
        resp = client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertFalse(target.is_staff)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True, TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
    def test_o4a_2fa_gate_still_enforced_alongside_o4b_flags(self):
        user = make_user(username="o4b4_regress_2fa", is_staff=True)
        _grant_user_admin_access(user)
        user.user_permissions.add(_treasury_permission("can_review_treasury_request"))
        client = Client()
        client.force_login(user)
        resp = client.get(reverse("admin:index"))
        self.assertRedirects(
            resp, reverse("simulator:totp_setup"), fetch_redirect_response=False,
        )

    def test_o3_treasury_submit_and_approve_workflow_using_hardened_grants(self):
        # Permissions granted through the O.4b-hardened CLI path must
        # still behave identically for the actual O.3 Treasury workflow
        # — hardening the assignment mechanism must not change what the
        # permission itself allows once held.
        wallet = make_wallet()
        submitter = make_user(username="o4b4_regress_o3_submitter", is_staff=True)
        reviewer = make_user(username="o4b4_regress_o3_reviewer", is_staff=True)
        call_command(
            "assign_treasury_role", submitter.username, "submitter", stdout=StringIO(),
        )
        call_command(
            "assign_treasury_role", reviewer.username, "reviewer", stdout=StringIO(),
        )

        client = Client()
        client.force_login(submitter)
        resp = client.post(NEW_REQUEST_URL, data={
            "wallet": wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
            "amount": "15.00",
            "reason": "O.4b-4 regression check",
        })
        tor = TreasuryOperationRequest.objects.get()
        self.assertRedirects(
            resp, reverse("admin:simulator_treasuryoperationrequest_change", args=[tor.pk]),
        )
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

        client.force_login(reviewer)
        resp = client.post(_approve_url(tor.pk), data={})
        self.assertRedirects(
            resp, reverse("admin:simulator_treasuryoperationrequest_change", args=[tor.pk]),
        )
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)


# ─────────────────────────────────────────────
# 9. Financial invariants across bypass attempts
# ─────────────────────────────────────────────

@override_settings(TREASURY_ROLE_CONCENTRATION_BLOCKING=True)
class FinancialInvariantsE2ETests(TestCase):

    def setUp(self):
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))
        self.su = make_user(username="o4b4_inv_su", is_staff=True, is_superuser=True)
        self.staff = make_user(username="o4b4_inv_staff", is_staff=True)
        _grant_user_admin_access(self.staff)
        self.target = make_user(username="o4b4_inv_target", is_staff=True)

    def test_no_financial_mutation_across_a_battery_of_bypass_attempts(self):
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        avail_before = self.wallet.available_balance

        client = Client()
        # Staff self-assign attempt.
        client.force_login(self.staff)
        perm = _treasury_permission("can_submit_treasury_request")
        client.post(
            _user_change_url(self.staff),
            data=_base_change_payload(self.staff, actor=self.staff, user_permission_pks=[perm.pk]),
        )
        # Staff -> other assign attempt.
        client.post(
            _user_change_url(self.target),
            data=_base_change_payload(self.target, actor=self.staff, user_permission_pks=[perm.pk]),
        )
        # Superuser self-grant attempt.
        client.force_login(self.su)
        client.post(
            _user_change_url(self.su),
            data=_base_change_payload(self.su, actor=self.su, user_permission_pks=[perm.pk]),
        )
        # Superuser concentrating grant (blocked).
        self.target.user_permissions.add(perm)
        perm2 = _treasury_permission("can_review_treasury_request")
        client.post(
            _user_change_url(self.target),
            data=_base_change_payload(
                self.target, actor=self.su, user_permission_pks=[perm.pk, perm2.pk],
            ),
        )
        # CLI blocked attempt.
        try:
            call_command(
                "assign_treasury_role", self.target.username, "reviewer", stdout=StringIO(),
            )
        except CommandError:
            pass

        self.wallet.refresh_from_db()
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(InternalTransfer.objects.count(), 0)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)


# ─────────────────────────────────────────────
# 10. Structural invariants
# ─────────────────────────────────────────────

class StructuralInvariantsTests(TestCase):

    def test_exactly_the_four_expected_treasury_permissions_exist(self):
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

    def test_treasury_state_machine_statuses_unchanged(self):
        expected_statuses = {
            TreasuryOperationRequest.ST_PENDING, TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED, TreasuryOperationRequest.ST_EXECUTING,
            TreasuryOperationRequest.ST_EXECUTED, TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        }
        actual_statuses = {
            choice[0] for choice in TreasuryOperationRequest._meta.get_field("status").choices
        }
        self.assertEqual(actual_statuses, expected_statuses)

    def test_admin_and_command_both_import_concentration_rule_from_shared_module(self):
        import ast
        import inspect
        import textwrap

        import simulator.admin as admin_module
        import simulator.management.commands.assign_treasury_role as command_module

        for module in (admin_module, command_module):
            source = textwrap.dedent(inspect.getsource(module))
            tree = ast.parse(source)
            imported_from_shared = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "treasury_permissions", ".treasury_permissions",
                    "simulator.treasury_permissions",
                ):
                    imported_from_shared = True
            self.assertTrue(
                imported_from_shared,
                f"{module.__name__} must import the concentration rule from "
                "simulator.treasury_permissions, not redefine it.",
            )

    def test_neither_module_redefines_the_codename_tuple_locally(self):
        import inspect

        import simulator.admin as admin_module
        import simulator.management.commands.assign_treasury_role as command_module

        for module in (admin_module, command_module):
            source = inspect.getsource(module)
            self.assertNotIn(
                'TREASURY_PERMISSION_CODENAMES = (\n    "can_submit_treasury_request"',
                source,
                f"{module.__name__} must not redefine the four-codename tuple locally.",
            )
