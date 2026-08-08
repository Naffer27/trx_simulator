# simulator/tests/test_o4a3_treasury_admin_2fa_end_to_end.py
"""
Microbloque O.4a-3 — Treasury Admin 2FA, end-to-end verification.

This file does NOT re-test treasury_2fa_required()'s own truth table
(test_o4a1...) or the individual redirect-target unit checks
(test_o4a2...) in isolation. It drives full realistic journeys through
the real admin URLs and the real TOTP setup/verify views (Django test
Client against the real URLconf, real pyotp codes) for the 9 mandatory
scenario groups of the O.4a checkpoint, and re-affirms the required
financial invariants explicitly — mirroring the precedent set by
test_o3c5c_treasury_admin_end_to_end.py and
test_o3e4_treasury_cancel_workflow_end_to_end.py.

No Treasury service function, model, migration, or permission is
touched by O.4a-3 — every mutation-capable Treasury URL below is hit
only to prove the 2FA gate blocks or allows it correctly, never to
complete a real financial mutation through to success (that is already
covered by O.3a-O.3e's own test suites, untouched here).
"""
import base64
from decimal import Decimal

import pyotp
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator.models import (
    InternalTransfer, TOTPDevice, TreasuryOperationRequest, WalletTransaction,
)

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


def _make_totp_device(user, confirmed: bool = True, raw_secret: str | None = None) -> TOTPDevice:
    raw_secret = raw_secret or pyotp.random_base32()
    return TOTPDevice.objects.create(
        user=user,
        secret=f"b64:{base64.b64encode(raw_secret.encode()).decode()}",
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
        "amount": Decimal("25.00"),
        "reason": "O.4a-3 end-to-end test",
        "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _submit_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "40.00",
        "reason": "O.4a-3 end-to-end test",
    }
    data.update(overrides)
    return data


ADMIN_INDEX_URL = reverse("admin:index")
ADMIN_LOGOUT_URL = reverse("admin:logout")
TOTP_SETUP_URL = reverse("simulator:totp_setup")
TOTP_VERIFY_URL = reverse("simulator:totp_verify")
CHANGELIST_URL = reverse("admin:simulator_treasuryoperationrequest_changelist")
NEW_REQUEST_URL = reverse("admin:treasury_request_new")
DASHBOARD_URL = reverse("admin:treasury_operational_dashboard")
DASHBOARD_DATA_URL = reverse("admin:treasury_operational_dashboard_data")


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


# ─────────────────────────────────────────────
# Scenario 1 — flag False: every role enters exactly as before
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=False)
class FlagFalseEveryRoleUnaffectedTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()
        self.requester = make_user(username="o4a3_s1_requester")

    def _assert_full_access(self, user):
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(ADMIN_INDEX_URL).status_code, 200)
        self.assertEqual(client.get(CHANGELIST_URL).status_code, 200)
        return client

    def test_superuser_enters_as_before(self):
        su = make_user(username="o4a3_s1_su", is_staff=True, is_superuser=True)
        self._assert_full_access(su)

    def test_submitter_enters_as_before(self):
        submitter = make_user(username="o4a3_s1_submitter", is_staff=True)
        _grant(submitter, "can_submit_treasury_request")
        client = self._assert_full_access(submitter)
        resp = client.get(NEW_REQUEST_URL)
        self.assertEqual(resp.status_code, 200)

    def test_reviewer_enters_as_before(self):
        reviewer = make_user(username="o4a3_s1_reviewer", is_staff=True)
        _grant(reviewer, "can_review_treasury_request")
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client = self._assert_full_access(reviewer)
        resp = client.get(_approve_url(tor.pk))
        self.assertEqual(resp.status_code, 200)

    def test_executor_enters_as_before(self):
        executor = make_user(username="o4a3_s1_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        client = self._assert_full_access(executor)
        resp = client.get(_execute_url(tor.pk))
        self.assertEqual(resp.status_code, 200)

    def test_recoverer_enters_as_before(self):
        recoverer = make_user(username="o4a3_s1_recoverer", is_staff=True)
        _grant(recoverer, "can_recover_treasury_execution")
        client = self._assert_full_access(recoverer)
        resp = client.get(DASHBOARD_URL)
        self.assertEqual(resp.status_code, 200)

    def test_non_treasury_staff_enters_as_before(self):
        staff = make_user(username="o4a3_s1_plain", is_staff=True)
        _grant(staff, "view_treasuryoperationrequest")
        self._assert_full_access(staff)


# ─────────────────────────────────────────────
# Scenario 2 — flag True, no device: mandatory setup, zero mutation
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class FlagTrueNoDeviceTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()
        self.requester = make_user(username="o4a3_s2_requester")

    def test_any_admin_url_redirects_to_setup(self):
        reviewer = make_user(username="o4a3_s2_reviewer", is_staff=True)
        _grant(reviewer, "can_review_treasury_request")
        client = Client()
        client.force_login(reviewer)
        for url in (ADMIN_INDEX_URL, CHANGELIST_URL, DASHBOARD_URL):
            with self.subTest(url=url):
                resp = client.get(url)
                self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_no_state_mutation_on_blocked_execute_attempt(self):
        executor = make_user(username="o4a3_s2_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        client = Client()
        client.force_login(executor)
        client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertIsNone(tor.wallet_transaction)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before)
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_direct_url_also_blocked_no_prior_navigation(self):
        reviewer = make_user(username="o4a3_s2_direct", is_staff=True)
        _grant(reviewer, "can_review_treasury_request")
        tor = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        client = Client()
        client.force_login(reviewer)
        # No changelist visit first — straight to the deep URL.
        resp = client.get(_approve_url(tor.pk))
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_PENDING)


# ─────────────────────────────────────────────
# Scenario 3 — flag True, device confirmed, session unverified
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class FlagTrueDeviceUnverifiedTests(TestCase):

    def test_redirects_to_verify_and_preserves_path_and_query_string(self):
        reviewer = make_user(username="o4a3_s3_reviewer", is_staff=True)
        _grant(reviewer, "can_review_treasury_request")
        _make_totp_device(reviewer, confirmed=True)
        client = Client()
        client.force_login(reviewer)

        target = CHANGELIST_URL + "?status=PENDING&o=-1"
        resp = client.get(target)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)
        self.assertEqual(client.session.get("2fa_next"), target)


# ─────────────────────────────────────────────
# Scenario 4 — correct TOTP: verified, returns to exact destination
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class CorrectTotpGrantsAccessTests(TestCase):

    def test_correct_code_returns_to_exact_original_destination(self):
        executor = make_user(username="o4a3_s4_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        raw_secret = pyotp.random_base32()
        _make_totp_device(executor, confirmed=True, raw_secret=raw_secret)
        client = Client()
        client.force_login(executor)

        target = CHANGELIST_URL + "?status=APPROVED"
        resp = client.get(target)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        code = pyotp.TOTP(raw_secret).now()
        resp = client.post(TOTP_VERIFY_URL, data={"code": code})
        self.assertRedirects(resp, target, fetch_redirect_response=False)

        resp = client.get(target)
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Scenario 5 — incorrect TOTP: no admin access, no financial change
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class IncorrectTotpDeniesAccessTests(TestCase):

    def test_wrong_code_does_not_grant_admin_access_or_move_money(self):
        executor = make_user(username="o4a3_s5_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        raw_secret = pyotp.random_base32()
        _make_totp_device(executor, confirmed=True, raw_secret=raw_secret)
        wallet = make_wallet()
        requester = make_user(username="o4a3_s5_requester")
        tor = _make_pending_request(
            wallet=wallet, requested_by=requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )

        client = Client()
        client.force_login(executor)
        client.get(_execute_url(tor.pk))  # sets 2fa_next, bounces to verify

        resp = client.post(TOTP_VERIFY_URL, data={"code": "000000"})
        self.assertEqual(resp.status_code, 200)  # re-renders verify form with error
        self.assertContains(resp, "incorrecto")

        # Still not verified -> still blocked from the admin.
        resp = client.get(_execute_url(tor.pk))
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        # And a direct POST bypass attempt while unverified changes nothing.
        client.post(_execute_url(tor.pk), data={})
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertIsNone(tor.wallet_transaction)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)


# ─────────────────────────────────────────────
# Scenario 6 — logout: POST works mid-gate, session cleared, re-login re-gates
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class LogoutEndToEndTests(TestCase):

    def test_post_logout_works_with_totp_pending_and_relogin_re_gates(self):
        executor = make_user(username="o4a3_s6_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        _make_totp_device(executor, confirmed=True)
        client = Client()
        client.force_login(executor)

        # TOTP pending (device exists, session unverified).
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        # Logout still works via POST despite the pending TOTP.
        resp = client.post(ADMIN_LOGOUT_URL)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))

        # Session is genuinely gone — an authenticated-only admin request
        # now redirects to admin:login, not the TOTP flow.
        resp = client.get(ADMIN_INDEX_URL)
        self.assertNotIn(TOTP_SETUP_URL, resp.get("Location", ""))
        self.assertNotIn(TOTP_VERIFY_URL, resp.get("Location", ""))

        # Re-login starts a fresh, unverified session -> gated again.
        client.force_login(executor)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)


# ─────────────────────────────────────────────
# Scenario 7 — superuser: full matrix
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class SuperuserFullMatrixTests(TestCase):

    def test_superuser_no_device_redirects_to_setup(self):
        su = make_user(username="o4a3_s7_su_nodev", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(su)
        resp = client.get(CHANGELIST_URL)
        self.assertRedirects(resp, TOTP_SETUP_URL, fetch_redirect_response=False)

    def test_superuser_device_unverified_redirects_to_verify(self):
        su = make_user(username="o4a3_s7_su_unverified", is_staff=True, is_superuser=True)
        _make_totp_device(su, confirmed=True)
        client = Client()
        client.force_login(su)
        resp = client.get(CHANGELIST_URL)
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

    def test_superuser_verified_gets_full_access(self):
        su = make_user(username="o4a3_s7_su_verified", is_staff=True, is_superuser=True)
        _make_totp_device(su, confirmed=True)
        client = Client()
        client.force_login(su)
        _set_2fa_verified(client)
        resp = client.get(CHANGELIST_URL)
        self.assertEqual(resp.status_code, 200)
        resp = client.get(DASHBOARD_URL)
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Scenario 8 — non-Treasury staff unaffected even with flag True
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class NonTreasuryStaffFlagTrueTests(TestCase):

    def test_plain_staff_full_access_without_any_totp_setup(self):
        staff = make_user(username="o4a3_s8_plain", is_staff=True)
        _grant(staff, "view_treasuryoperationrequest")
        client = Client()
        client.force_login(staff)
        resp = client.get(ADMIN_INDEX_URL)
        self.assertEqual(resp.status_code, 200)
        resp = client.get(CHANGELIST_URL)
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Scenario 9 — direct access to all 9 required Treasury surfaces
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class AllTreasurySurfacesGatedTests(TestCase):
    """
    Every URL explicitly required by the O.4a-3 checklist: changelist,
    detail, submit (new), approve, reject, execute, recover, cancel,
    operational dashboard. All must pass through the identical gate.
    """

    def setUp(self):
        self.wallet = make_wallet()
        self.requester = make_user(username="o4a3_s9_requester")

    def test_all_nine_surfaces_redirect_to_setup_when_unverified(self):
        user = make_user(username="o4a3_s9_operator", is_staff=True, is_superuser=True)
        pending = _make_pending_request(wallet=self.wallet, requested_by=self.requester)
        approved = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        executing = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_EXECUTING,
        )

        surfaces = {
            "changelist": CHANGELIST_URL,
            "detail": _change_url(pending.pk),
            "submit": NEW_REQUEST_URL,
            "approve": _approve_url(pending.pk),
            "reject": _reject_url(pending.pk),
            "execute": _execute_url(approved.pk),
            "recover": _recover_url(executing.pk),
            "cancel": _cancel_url(pending.pk),
            "dashboard": DASHBOARD_URL,
        }

        client = Client()
        client.force_login(user)
        for label, url in surfaces.items():
            with self.subTest(surface=label):
                resp = client.get(url)
                self.assertRedirects(
                    resp, TOTP_SETUP_URL, fetch_redirect_response=False,
                    msg_prefix=f"surface={label} url={url}",
                )

        # Nothing mutated across any of the 9 blocked attempts.
        pending.refresh_from_db()
        approved.refresh_from_db()
        executing.refresh_from_db()
        self.assertEqual(pending.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(approved.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(executing.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), 0)
        self.assertEqual(InternalTransfer.objects.count(), 0)


# ─────────────────────────────────────────────
# Invariants — re-affirmed explicitly, financial and structural
# ─────────────────────────────────────────────

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class InvariantsTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet(initial_balance=Decimal("50.00"))
        self.requester = make_user(username="o4a3_inv_requester")

    def test_wallet_and_wallet_transaction_untouched_across_blocked_attempts(self):
        executor = make_user(username="o4a3_inv_executor", is_staff=True)
        _grant(executor, "can_execute_treasury_request")
        tor = _make_pending_request(
            wallet=self.wallet, requested_by=self.requester,
            status=TreasuryOperationRequest.ST_APPROVED,
        )
        avail_before = self.wallet.available_balance
        # make_wallet(initial_balance=...) already creates one
        # WalletTransaction via credit_wallet() during setUp — compare
        # the count before/after the blocked attempt, not against zero.
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        client = Client()
        client.force_login(executor)
        client.post(_execute_url(tor.pk), data={})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, avail_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before)

    def test_no_internal_transfer_ever(self):
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_execute_and_recover_functions_never_imported_by_admin_view(self):
        import ast
        import inspect
        import textwrap

        from simulator.admin import MoneyBrokerAdminSite

        tree = ast.parse(textwrap.dedent(inspect.getsource(MoneyBrokerAdminSite.admin_view)))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(a.name for a in node.names)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    names.add(name)
        self.assertNotIn("execute_treasury_request", names)
        self.assertNotIn("mark_treasury_execution_failed", names)
        self.assertNotIn("credit_wallet", names)
        self.assertNotIn("debit_wallet", names)

    def test_no_new_treasury_permissions_exist(self):
        from django.contrib.auth.models import Permission
        from simulator.models import TreasuryOperationRequest as TOR
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(TOR)
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

    def test_state_machine_statuses_unchanged(self):
        from simulator.models import TreasuryOperationRequest as TOR
        expected_statuses = {
            TOR.ST_PENDING, TOR.ST_APPROVED, TOR.ST_REJECTED,
            TOR.ST_EXECUTING, TOR.ST_EXECUTED, TOR.ST_FAILED, TOR.ST_CANCELLED,
        }
        actual_statuses = {choice[0] for choice in TOR._meta.get_field("status").choices}
        self.assertEqual(actual_statuses, expected_statuses)
