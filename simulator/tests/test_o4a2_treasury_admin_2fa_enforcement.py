# simulator/tests/test_o4a2_treasury_admin_2fa_enforcement.py
"""
Microbloque O.4a-2 — Treasury Admin 2FA Enforcement.

Covers the MoneyBrokerAdminSite.admin_view() override (simulator/admin.py)
that wires treasury_2fa_required() (O.4a-1, simulator/two_factor.py) into
every /admin/ URL. This file does not re-test treasury_2fa_required()'s
own truth table (already covered exhaustively in
test_o4a1_treasury_admin_2fa_helper.py) — it drives real requests through
the real admin URLs (Django test Client against the real URLconf) to
prove the gate itself: flag off leaves everything unchanged, flag on
enforces TOTP for superusers and the four Treasury permissions, staff
without those permissions are unaffected, direct URL access is equally
protected (not just hidden buttons), admin:logout stays reachable, and
2fa_next correctly returns the operator to the exact URL they requested.

No Treasury service function, model, migration, or existing Treasury
view is touched by O.4a-2 — every mutation-capable Treasury URL below is
hit only to prove it redirects (never that it succeeds), so this file
never calls approve/reject/execute/recover/cancel through to completion.
"""
import base64

import pyotp
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator.models import TOTPDevice, TreasuryOperationRequest

from .factories import make_user, make_wallet

TREASURY_PERMISSIONS = (
    "can_submit_treasury_request",
    "can_review_treasury_request",
    "can_execute_treasury_request",
    "can_recover_treasury_execution",
)


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _make_totp_device(user, confirmed: bool = True) -> TOTPDevice:
    secret = pyotp.random_base32()
    return TOTPDevice.objects.create(
        user=user,
        secret=f"b64:{base64.b64encode(secret.encode()).decode()}",
        confirmed=confirmed,
    )


def _set_2fa_verified(client):
    session = client.session
    session["2fa_verified"] = True
    session.save()


def _make_pending_request(wallet=None, requested_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet,
        "amount": 25,
        "reason": "O.4a-2 admin 2FA enforcement test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


ADMIN_INDEX_URL = reverse("admin:index")
ADMIN_LOGOUT_URL = reverse("admin:logout")
TOTP_SETUP_URL = reverse("simulator:totp_setup")
TOTP_VERIFY_URL = reverse("simulator:totp_verify")
CHANGELIST_URL = reverse("admin:simulator_treasuryoperationrequest_changelist")


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _approve_url(pk):
    return reverse("admin:treasury_request_approve", args=[pk])


def _reject_url(pk):
    return reverse("admin:treasury_request_reject", args=[pk])


def _execute_url(pk):
    return reverse("admin:treasury_request_execute", args=[pk])


def _recover_url(pk):
    return reverse("admin:treasury_request_recover", args=[pk])


def _cancel_url(pk):
    return reverse("admin:treasury_request_cancel", args=[pk])


DASHBOARD_URL = reverse("admin:treasury_operational_dashboard")
DASHBOARD_DATA_URL = reverse("admin:treasury_operational_dashboard_data")


# ─────────────────────────────────────────────
# Flag OFF (default) — behavior must be byte-for-byte unchanged
# ─────────────────────────────────────────────

class FlagOffRegressionTests(TestCase):
    """
    TOTP_ADMIN_TREASURY_REQUIRED defaults to False. With it False (or
    explicitly False), admin access for superusers, each of the four
    Treasury permissions, and plain staff must be completely unaffected
    by O.4a-2 — no TOTPDevice, no session flag, straight 200.
    """

    def test_superuser_unaffected_by_default(self):
        su = make_user(username="o4a2_flagoff_su", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(su)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)

    def test_each_treasury_permission_unaffected_by_default(self):
        for codename in TREASURY_PERMISSIONS:
            with self.subTest(codename=codename):
                user = make_user(username=f"o4a2_flagoff_{codename}", is_staff=True)
                _grant(user, codename)
                client = Client()
                client.force_login(user)
                resp = client.get(ADMIN_INDEX_URL)
                self.assertEqual(resp.status_code, 200)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=False)
    def test_explicit_flag_false_unaffected(self):
        user = make_user(username="o4a2_flagoff_explicit", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        client = Client()
        client.force_login(user)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)

    def test_plain_staff_unaffected_by_default(self):
        staff = make_user(username="o4a2_flagoff_staff", is_staff=True)
        _grant(staff, "view_treasuryoperationrequest")
        client = Client()
        client.force_login(staff)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Flag ON — no TOTPDevice enrolled -> mandatory setup
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class FlagOnNoDeviceTests(TestCase):

    def test_superuser_without_device_redirected_to_setup(self):
        su = make_user(username="o4a2_nodev_su", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(su)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_each_treasury_permission_without_device_redirected_to_setup(self):
        for codename in TREASURY_PERMISSIONS:
            with self.subTest(codename=codename):
                user = make_user(username=f"o4a2_nodev_{codename}", is_staff=True)
                _grant(user, codename)
                client = Client()
                client.force_login(user)
                resp = client.get(ADMIN_INDEX_URL)
                self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_2fa_next_preserved_on_setup_redirect(self):
        user = make_user(username="o4a2_nodev_2fanext", is_staff=True)
        _grant(user, "can_review_treasury_request")
        client = Client()
        client.force_login(user)
        client.get(CHANGELIST_URL)
        self.assertEqual(client.session.get("2fa_next"), CHANGELIST_URL)


# ─────────────────────────────────────────────
# Flag ON — TOTPDevice confirmed, session not verified -> totp_verify
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class FlagOnDeviceUnverifiedTests(TestCase):

    def test_superuser_with_device_unverified_redirected_to_verify(self):
        su = make_user(username="o4a2_unverified_su", is_staff=True, is_superuser=True)
        _make_totp_device(su, confirmed=True)
        client = Client()
        client.force_login(su)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

    def test_each_treasury_permission_with_device_unverified_redirected_to_verify(self):
        for codename in TREASURY_PERMISSIONS:
            with self.subTest(codename=codename):
                user = make_user(username=f"o4a2_unverified_{codename}", is_staff=True)
                _grant(user, codename)
                _make_totp_device(user, confirmed=True)
                client = Client()
                client.force_login(user)
                resp = client.get(ADMIN_INDEX_URL)
                self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

    def test_unconfirmed_device_treated_as_no_device(self):
        # A device row exists but confirmed=False (setup started, QR not
        # yet scanned/verified) — must be treated exactly like no device
        # at all: mandatory setup, not verify.
        user = make_user(username="o4a2_unconfirmed_device", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, confirmed=False)
        client = Client()
        client.force_login(user)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_2fa_next_preserved_with_query_string(self):
        user = make_user(username="o4a2_unverified_2fanext", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, confirmed=True)
        client = Client()
        client.force_login(user)
        target = CHANGELIST_URL + "?status=PENDING"
        client.get(target)
        self.assertEqual(client.session.get("2fa_next"), target)


# ─────────────────────────────────────────────
# Flag ON — session verified -> normal access
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class FlagOnSessionVerifiedTests(TestCase):

    def test_superuser_verified_gets_normal_access(self):
        su = make_user(username="o4a2_verified_su", is_staff=True, is_superuser=True)
        _make_totp_device(su, confirmed=True)
        client = Client()
        client.force_login(su)
        _set_2fa_verified(client)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)

    def test_each_treasury_permission_verified_gets_normal_access(self):
        for codename in TREASURY_PERMISSIONS:
            with self.subTest(codename=codename):
                user = make_user(username=f"o4a2_verified_{codename}", is_staff=True)
                _grant(user, codename)
                _make_totp_device(user, confirmed=True)
                client = Client()
                client.force_login(user)
                _set_2fa_verified(client)
                resp = client.get(ADMIN_INDEX_URL)
                self.assertEqual(resp.status_code, 200)

    def test_verify_view_redirects_back_to_original_admin_url(self):
        user = make_user(username="o4a2_verify_roundtrip", is_staff=True)
        _grant(user, "can_review_treasury_request")
        device_secret = pyotp.random_base32()
        TOTPDevice.objects.create(
            user=user,
            secret=f"b64:{base64.b64encode(device_secret.encode()).decode()}",
            confirmed=True,
        )
        client = Client()
        client.force_login(user)

        # First hit sets 2fa_next and bounces to verify.
        resp = client.get(CHANGELIST_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        # Completing verification with the correct code redirects back
        # to the exact original destination.
        code = pyotp.TOTP(device_secret).now()
        resp = client.post(TOTP_VERIFY_URL, data={"code": code})
        self.assertRedirects(resp, CHANGELIST_URL, fetch_redirect_response=False)

        # And the admin is now actually reachable.
        resp = client.get(CHANGELIST_URL)
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Staff without any Treasury permission — unaffected even with flag True
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class NonTreasuryStaffUnaffectedTests(TestCase):

    def test_plain_staff_no_device_no_session_gets_normal_access(self):
        staff = make_user(username="o4a2_nontreasury_staff", is_staff=True)
        _grant(staff, "view_treasuryoperationrequest")
        client = Client()
        client.force_login(staff)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)

    def test_plain_staff_never_redirected_to_totp_pages(self):
        staff = make_user(username="o4a2_nontreasury_staff2", is_staff=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertNotEqual(resp.status_code, 302)


# ─────────────────────────────────────────────
# Direct URL access / bypass prevention — server-side, not button-hiding
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class DirectUrlBypassPreventionTests(TestCase):
    """
    An unverified Treasury operator hitting a Treasury admin URL directly
    (bookmark, curl, typed URL) — with no changelist/button click in
    between — must be blocked exactly as if they had come from the
    normal navigation path. GET and POST are both tested; POST proves a
    bypass attempt cannot mutate anything before the gate fires.
    """

    def setUp(self):
        self.requester = make_user(username="o4a2_bypass_requester")
        self.wallet = make_wallet()

    def _unverified_client(self, *codenames):
        user = make_user(username=f"o4a2_bypass_{'_'.join(codenames) or 'plain'}", is_staff=True)
        for codename in codenames:
            _grant(user, codename)
        client = Client()
        client.force_login(user)
        return client, user

    def test_direct_get_to_changelist_blocked(self):
        client, _ = self._unverified_client("can_review_treasury_request")
        resp = client.get(CHANGELIST_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_direct_get_to_change_view_blocked(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client, _ = self._unverified_client("can_review_treasury_request")
        resp = client.get(_change_url(tor.pk))
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_direct_get_and_post_to_approve_blocked(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client, _ = self._unverified_client("can_review_treasury_request")
        resp = client.get(_approve_url(tor.pk))
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        resp = client.post(_approve_url(tor.pk), data={})
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_direct_get_and_post_to_reject_blocked(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client, _ = self._unverified_client("can_review_treasury_request")
        resp = client.post(_reject_url(tor.pk), data={"rejection_reason": "x"})
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_direct_get_and_post_to_execute_blocked(self):
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        client, _ = self._unverified_client("can_execute_treasury_request")
        resp = client.post(_execute_url(tor.pk), data={})
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertIsNone(tor.wallet_transaction)

    def test_direct_get_and_post_to_recover_blocked(self):
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_EXECUTING,
        )
        client, _ = self._unverified_client("can_recover_treasury_execution")
        resp = client.post(_recover_url(tor.pk), data={"recovery_reason": "x"})
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_direct_get_and_post_to_cancel_blocked(self):
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client, _ = self._unverified_client("can_review_treasury_request")
        resp = client.post(_cancel_url(tor.pk), data={})
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)

    def test_direct_get_to_dashboard_and_dashboard_data_blocked(self):
        client, _ = self._unverified_client("can_recover_treasury_execution")
        resp = client.get(DASHBOARD_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        resp = client.get(DASHBOARD_DATA_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_superuser_direct_access_equally_blocked(self):
        su = make_user(username="o4a2_bypass_su", is_staff=True, is_superuser=True)
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client = Client()
        client.force_login(su)
        resp = client.get(_change_url(tor.pk))
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)


# ─────────────────────────────────────────────
# admin:logout must remain reachable mid-gate
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class LogoutAccessibleMidGateTests(TestCase):

    def test_logout_reachable_without_device(self):
        user = make_user(username="o4a2_logout_nodev", is_staff=True)
        _grant(user, "can_review_treasury_request")
        client = Client()
        client.force_login(user)
        resp = client.get(ADMIN_LOGOUT_URL)
        self.assertNotEqual(resp.status_code, 302)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))

    def test_logout_reachable_with_unverified_device(self):
        user = make_user(username="o4a2_logout_unverified", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, confirmed=True)
        client = Client()
        client.force_login(user)
        resp = client.get(ADMIN_LOGOUT_URL)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))

    def test_post_logout_actually_logs_out_without_totp_detour(self):
        # The real logout path (Django 5's LogoutView only accepts POST) —
        # proves the admin_view() exemption also holds for the request
        # that actually performs the logout, not just a GET probe.
        user = make_user(username="o4a2_logout_post", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, confirmed=True)
        client = Client()
        client.force_login(user)
        resp = client.post(ADMIN_LOGOUT_URL)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))
        # Session truly ended — next admin request is anonymous, not
        # gated by TOTP at all, just the ordinary login requirement.
        resp = client.get(ADMIN_INDEX_URL)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))

    def test_logout_then_login_again_requires_reverification(self):
        user = make_user(username="o4a2_logout_relogin", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, confirmed=True)
        client = Client()
        client.force_login(user)
        _set_2fa_verified(client)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)

        # Django 5's LogoutView only accepts POST (http_method_names =
        # ["post", "options"]) — a GET here would 405 without logging
        # out at all. client.logout() is the test Client's own idiomatic
        # equivalent of a real logout (flushes the session), independent
        # of that HTTP-method detail.
        client.logout()

        client.force_login(user)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)
