# simulator/tests/test_o2f1_compliance_admin.py
"""
Bloque O.2f-1 — Compliance Center (registro read-only de
EmailVerification, TermsAcceptance, TOTPDevice).

Mirrors the Treasury Audit & Reconciliation block (O.2e-1): pure
observation surface, zero write path, zero new admin actions.

No test here touches broker_audit.py / audit.py / two_factor.py /
email_verification.py, and none of it exercises _is_email_verified(),
_has_accepted_terms(), or the withdrawal 2FA/KYC gates — those remain
exactly as they were in views.py.

KYCProfile and its approve_kyc/reject_kyc actions are explicitly out of
scope for this block and are only checked here to confirm they were
NOT touched.
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.admin import (
    EmailVerificationAdmin,
    KYCProfileAdmin,
    TOTPDeviceAdmin,
    TermsAcceptanceAdmin,
)
from simulator.models import (
    EmailVerification,
    KYCProfile,
    TOTPDevice,
    TermsAcceptance,
)

from .factories import make_user


class EmailVerificationAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(EmailVerification, admin_site._registry)

    def test_cannot_add(self):
        ma = EmailVerificationAdmin(EmailVerification, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = EmailVerificationAdmin(EmailVerification, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = EmailVerificationAdmin(EmailVerification, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_displayed_fields_are_readonly(self):
        ma = EmailVerificationAdmin(EmailVerification, admin_site)
        self.assertEqual(set(ma.readonly_fields), set(ma.fields))

    def test_changelist_loads_without_error(self):
        make_user(username="o2f1_ev_user")
        staff = make_user(username="o2f1_ev_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_emailverification_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2f1_ev_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_emailverification_add"))
        self.assertEqual(resp.status_code, 403)

    def test_delete_view_rejected(self):
        user = make_user(username="o2f1_ev_user2")
        staff = make_user(username="o2f1_ev_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(
            reverse("admin:simulator_emailverification_delete", args=[user.email_verification.pk])
        )
        self.assertEqual(resp.status_code, 403)


class TermsAcceptanceAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(TermsAcceptance, admin_site._registry)

    def test_cannot_add(self):
        ma = TermsAcceptanceAdmin(TermsAcceptance, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = TermsAcceptanceAdmin(TermsAcceptance, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = TermsAcceptanceAdmin(TermsAcceptance, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_displayed_fields_are_readonly(self):
        ma = TermsAcceptanceAdmin(TermsAcceptance, admin_site)
        self.assertEqual(set(ma.readonly_fields), set(ma.fields))

    def test_changelist_loads_without_error(self):
        make_user(username="o2f1_ta_user")
        staff = make_user(username="o2f1_ta_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_termsacceptance_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2f1_ta_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_termsacceptance_add"))
        self.assertEqual(resp.status_code, 403)

    def test_delete_view_rejected(self):
        user = make_user(username="o2f1_ta_user2")
        ta = TermsAcceptance.objects.filter(user=user).first()
        staff = make_user(username="o2f1_ta_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_termsacceptance_delete", args=[ta.pk]))
        self.assertEqual(resp.status_code, 403)


class TOTPDeviceAdminPermissionTests(TestCase):

    def _make_device(self, username="o2f1_totp_user", confirmed=True):
        user = make_user(username=username)
        return TOTPDevice.objects.create(
            user=user, secret="fernet:super-secret-value", confirmed=confirmed,
            confirmed_at=timezone.now() if confirmed else None,
        )

    def test_registered(self):
        self.assertIn(TOTPDevice, admin_site._registry)

    def test_cannot_add(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_displayed_fields_are_readonly(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertEqual(set(ma.readonly_fields), set(ma.fields))

    def test_changelist_loads_without_error(self):
        self._make_device()
        staff = make_user(username="o2f1_totp_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_totpdevice_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2f1_totp_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_totpdevice_add"))
        self.assertEqual(resp.status_code, 403)

    # ── secret exclusion — the core requirement of this block ──────────────

    def test_secret_not_in_fields(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertNotIn("secret", ma.fields)

    def test_secret_not_in_readonly_fields(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertNotIn("secret", ma.readonly_fields)

    def test_secret_not_in_list_display(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertNotIn("secret", ma.list_display)

    def test_secret_absent_from_changelist_html(self):
        self._make_device()
        staff = make_user(username="o2f1_totp_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_totpdevice_changelist"))
        self.assertNotIn(b"super-secret-value", resp.content)
        self.assertNotIn(b"fernet:", resp.content)

    def test_secret_absent_from_detail_view_html(self):
        device = self._make_device()
        staff = make_user(username="o2f1_totp_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_totpdevice_change", args=[device.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"super-secret-value", resp.content)
        self.assertNotIn(b"fernet:", resp.content)
        self.assertNotIn(b'name="secret"', resp.content)

    def test_expected_fields_are_shown(self):
        ma = TOTPDeviceAdmin(TOTPDevice, admin_site)
        self.assertEqual(set(ma.fields), {"user", "confirmed", "created_at", "confirmed_at"})


class KYCProfileUntouchedTests(TestCase):
    """This block must not have modified KYCProfile or its actions."""

    def test_still_registered(self):
        self.assertIn(KYCProfile, admin_site._registry)

    def test_still_fully_editable_by_default_permissions(self):
        ma = KYCProfileAdmin(KYCProfile, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="o2f1_kyc_perm_staff", is_staff=True, is_superuser=True)
        self.assertTrue(ma.has_add_permission(request))
        self.assertTrue(ma.has_change_permission(request))

    def test_approve_and_reject_actions_still_present(self):
        ma = KYCProfileAdmin(KYCProfile, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="o2f1_kyc_actions_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertIn("approve_kyc", actions)
        self.assertIn("reject_kyc", actions)


class ScopeGuardTests(TestCase):
    """No model other than the three named was registered in this block."""

    def test_no_other_compliance_or_security_model_registered(self):
        from simulator.models import (
            AccountEquitySnapshot, BrokerEquitySnapshot, SymbolExposure,
            TraderClassExposure, BrokerRiskLock, BrokerAuditObservationLock,
        )
        for model in (
            AccountEquitySnapshot, BrokerEquitySnapshot, SymbolExposure,
            TraderClassExposure, BrokerRiskLock, BrokerAuditObservationLock,
        ):
            self.assertNotIn(model, admin_site._registry)
