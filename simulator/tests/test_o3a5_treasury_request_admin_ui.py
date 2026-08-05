# simulator/tests/test_o3a5_treasury_request_admin_ui.py
"""
Bloque O.3a-5 — Treasury Request Admin UI.

Covers the first real entry point that can create a
TreasuryOperationRequest end-to-end through the browser: the custom
admin view registered at admin:treasury_request_new
(simulator/admin.py::TreasuryOperationRequestAdmin.treasury_request_new_view),
its template (simulator/templates/admin/treasury_request_new.html), and
the "New Treasury Request" header button on the changelist
(simulator/templates/admin/simulator/treasuryoperationrequest/change_list.html).

No approval, rejection, cancellation or execution logic exists or is
exercised here — every successful POST creates exactly one
TreasuryOperationRequest with status=PENDING via submit_treasury_request()
(O.3a-4), which is not reimplemented or duplicated by this view.
"""
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from simulator.admin import _treasury_wallet_confirmation_data
from simulator.models import AuditLog, KYCProfile, TreasuryOperationRequest

from .factories import make_user, make_wallet


def _grant_submit_permission(user):
    perm = Permission.objects.get(codename="can_submit_treasury_request")
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_operator(**kwargs):
    user = make_user(is_staff=True, **kwargs)
    return _grant_submit_permission(user)


def _valid_post_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "77.00",
        "reason": "O.3a-5 admin UI test",
    }
    data.update(overrides)
    return data


class UrlAndAccessTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def test_url_resolves(self):
        url = reverse("admin:treasury_request_new")
        self.assertEqual(url, "/admin/simulator/treasuryoperationrequest/new/")

    def test_anonymous_user_redirected_to_login(self):
        client = Client()
        resp = client.get(reverse("admin:treasury_request_new"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_staff_without_permission_gets_403(self):
        staff = make_user(username="o3a5_no_perm", is_staff=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:treasury_request_new"))
        self.assertEqual(resp.status_code, 403)

    def test_operator_with_permission_gets_200(self):
        operator = _make_operator(username="o3a5_operator_ok")
        client = Client()
        client.force_login(operator)
        resp = client.get(reverse("admin:treasury_request_new"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "New Treasury Request")

    def test_superuser_gets_200(self):
        superuser = make_user(username="o3a5_super", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)
        resp = client.get(reverse("admin:treasury_request_new"))
        self.assertEqual(resp.status_code, 200)


class GetPageContentTests(TestCase):

    def setUp(self):
        self.operator = _make_operator(username="o3a5_get_content")
        self.wallet = make_wallet()
        self.client = Client()
        self.client.force_login(self.operator)

    def test_form_fields_present_in_page(self):
        resp = self.client.get(reverse("admin:treasury_request_new"))
        for field_id in (
            "id_wallet", "id_operation_type", "id_amount",
            "id_reason", "id_reference", "id_category",
            "id_comment", "id_evidence",
        ):
            self.assertContains(resp, field_id)

    def test_disclaimer_message_present(self):
        resp = self.client.get(reverse("admin:treasury_request_new"))
        self.assertContains(resp, "solicitud administrativa de Treasury")
        self.assertContains(resp, "Ningún fondo")

    def test_wallet_confirmation_panel_markup_present(self):
        resp = self.client.get(reverse("admin:treasury_request_new"))
        self.assertContains(resp, "tr-wallet-panel")
        self.assertContains(resp, "wp-username")
        self.assertContains(resp, "wp-kyc")

    def test_summary_block_markup_present(self):
        resp = self.client.get(reverse("admin:treasury_request_new"))
        self.assertContains(resp, "Summary")
        self.assertContains(resp, "sm-wallet")
        self.assertContains(resp, "sm-amount")

    def test_wallet_data_json_embedded_and_valid(self):
        import json
        resp = self.client.get(reverse("admin:treasury_request_new"))
        content = resp.content.decode()
        start = content.index('id="tr-wallet-data">') + len('id="tr-wallet-data">')
        end = content.index("</script>", start)
        blob = json.loads(content[start:end])
        self.assertIn(str(self.wallet.pk), blob)
        row = blob[str(self.wallet.pk)]
        self.assertEqual(set(row.keys()), {
            "username", "email", "wallet",
            "available_balance", "pending_balance", "currency", "kyc_status",
        })

    def test_cancel_link_points_to_changelist(self):
        resp = self.client.get(reverse("admin:treasury_request_new"))
        self.assertContains(resp, reverse("admin:simulator_treasuryoperationrequest_changelist"))


class WalletConfirmationDataTests(TestCase):
    """Adjustment 1 — Username / Email / Wallet / Available / Pending / KYC."""

    def test_confirmation_data_content_with_kyc_profile(self):
        user = make_user(username="o3a5_kyc_user", email="kyc@example.com")
        wallet = make_wallet(user=user, initial_balance=Decimal("500.00"))
        KYCProfile.objects.create(user=user, status=KYCProfile.STATUS_APPROVED)

        data = _treasury_wallet_confirmation_data()
        row = data[str(wallet.pk)]

        self.assertEqual(row["username"], "o3a5_kyc_user")
        self.assertEqual(row["email"], "kyc@example.com")
        self.assertEqual(row["wallet"], str(wallet))
        self.assertEqual(row["available_balance"], "500.00")
        self.assertEqual(row["pending_balance"], "0.00")
        self.assertEqual(row["currency"], wallet.currency)
        self.assertEqual(row["kyc_status"], "Approved")

    def test_confirmation_data_without_kyc_profile_shows_not_started(self):
        wallet = make_wallet()
        self.assertFalse(KYCProfile.objects.filter(user=wallet.user).exists())

        data = _treasury_wallet_confirmation_data()
        row = data[str(wallet.pk)]
        self.assertEqual(row["kyc_status"], "Not Started")

    def test_confirmation_data_never_writes_anything(self):
        make_wallet()
        before = KYCProfile.objects.count()
        _treasury_wallet_confirmation_data()
        after = KYCProfile.objects.count()
        self.assertEqual(before, after)


class SuccessfulSubmissionTests(TestCase):

    def setUp(self):
        self.operator = _make_operator(username="o3a5_submit_ok")
        self.wallet = make_wallet()
        self.client = Client()
        self.client.force_login(self.operator)

    def test_post_valid_creates_request_and_redirects_to_detail(self):
        data = _valid_post_data(self.wallet)
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data)

        self.assertEqual(TreasuryOperationRequest.objects.count(), 1)
        instance = TreasuryOperationRequest.objects.get()
        self.assertEqual(instance.status, TreasuryOperationRequest.ST_PENDING)
        self.assertEqual(instance.requested_by_id, self.operator.pk)

        self.assertRedirects(
            resp,
            reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk]),
        )

    def test_success_message_shown(self):
        data = _valid_post_data(self.wallet)
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data, follow=True)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("creada" in m and "PENDING" in m for m in messages))

    def test_redirect_target_is_viewable_by_the_operator(self):
        # has_view_permission must allow this, or the redirect from
        # the success path dead-ends in a 403 right after creation.
        data = _valid_post_data(self.wallet)
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.redirect_chain), 1)

    def test_auditlog_and_brokerauditevent_created(self):
        from simulator.models import BrokerAuditEvent

        data = _valid_post_data(self.wallet)
        self.client.post(reverse("admin:treasury_request_new"), data=data)

        self.assertEqual(
            AuditLog.objects.filter(event_type="treasury.request_submitted").count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type="treasury.request_submitted").count(), 1,
        )

    def test_get_after_success_shows_readonly_detail_not_403(self):
        data = _valid_post_data(self.wallet)
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Forbidden", status_code=200)


class InvalidSubmissionTests(TestCase):

    def setUp(self):
        self.operator = _make_operator(username="o3a5_submit_invalid")
        self.wallet = make_wallet()
        self.client = Client()
        self.client.force_login(self.operator)

    def test_post_invalid_does_not_create_anything(self):
        data = _valid_post_data(self.wallet)
        del data["amount"]
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_post_invalid_shows_error_message(self):
        data = _valid_post_data(self.wallet)
        del data["amount"]
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Revisa los campos" in m for m in messages))

    def test_post_invalid_preserves_submitted_reason(self):
        data = _valid_post_data(self.wallet, reason="a very specific reason to look for")
        del data["amount"]
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data)
        self.assertContains(resp, "a very specific reason to look for")

    def test_post_missing_category_for_credit_funds_shows_field_error(self):
        data = _valid_post_data(
            self.wallet,
            operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
            category="",
        )
        resp = self.client.post(reverse("admin:treasury_request_new"), data=data)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertContains(resp, "Category es obligatoria")


class PermissionDeniedSubmissionTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def test_post_without_permission_creates_nothing(self):
        staff = make_user(username="o3a5_post_no_perm", is_staff=True)
        client = Client()
        client.force_login(staff)

        resp = client.post(reverse("admin:treasury_request_new"), data=_valid_post_data(self.wallet))

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)


class ChangelistButtonVisibilityTests(TestCase):

    def test_button_visible_with_permission(self):
        operator = _make_operator(username="o3a5_button_yes")
        client = Client()
        client.force_login(operator)
        resp = client.get(reverse("admin:simulator_treasuryoperationrequest_changelist"))
        self.assertContains(resp, "New Treasury Request")
        self.assertContains(resp, reverse("admin:treasury_request_new"))

    def test_button_hidden_without_submit_permission(self):
        # Isolates the button's own condition (can_submit_treasury_request)
        # from general changelist access: this user CAN see the list
        # (granted the standard view_ permission, same as any other
        # read-only admin list in this project) but lacks the submit
        # permission specifically — the button must not render for them.
        staff = make_user(username="o3a5_button_no", is_staff=True)
        view_perm = Permission.objects.get(codename="view_treasuryoperationrequest")
        staff.user_permissions.add(view_perm)
        staff.refresh_from_db()
        self.assertFalse(staff.has_perm("simulator.can_submit_treasury_request"))

        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_treasuryoperationrequest_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "New Treasury Request")


class ViewPermissionTests(TestCase):
    """
    has_view_permission was widened in O.3a-5 so the post-submit redirect
    to the object's own change/detail page resolves for an operator who
    only holds can_submit_treasury_request. has_change_permission stays
    hard-False regardless — this never grants edit access.
    """

    def setUp(self):
        self.wallet = make_wallet()
        self.instance = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
            wallet=self.wallet, amount=Decimal("10.00"), reason="seed",
        )

    def test_submit_permission_holder_can_view_detail(self):
        operator = _make_operator(username="o3a5_view_submit")
        client = Client()
        client.force_login(operator)
        resp = client.get(
            reverse("admin:simulator_treasuryoperationrequest_change", args=[self.instance.pk]),
        )
        self.assertEqual(resp.status_code, 200)

    def test_staff_without_any_treasury_permission_cannot_view_detail(self):
        staff = make_user(username="o3a5_view_none", is_staff=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(
            reverse("admin:simulator_treasuryoperationrequest_change", args=[self.instance.pk]),
        )
        self.assertEqual(resp.status_code, 403)

    def test_change_permission_still_denied_for_everyone(self):
        from simulator.admin import TreasuryOperationRequestAdmin
        operator = _make_operator(username="o3a5_no_change")
        model_admin = TreasuryOperationRequestAdmin(TreasuryOperationRequest, None)
        request = type("R", (), {"user": operator})()
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))


class NoFinancialSideEffectsTests(TestCase):
    """Cierre — ningún movimiento financiero ocurre a través de esta vista."""

    def setUp(self):
        self.operator = _make_operator(username="o3a5_no_money")
        self.wallet = make_wallet(initial_balance=Decimal("100.00"))
        self.client = Client()
        self.client.force_login(self.operator)

    def test_wallet_balance_unchanged_after_submission(self):
        from simulator.models import WalletTransaction, InternalTransfer

        balance_before = self.wallet.available_balance
        wtx_before = WalletTransaction.objects.filter(wallet=self.wallet).count()

        data = _valid_post_data(self.wallet)
        self.client.post(reverse("admin:treasury_request_new"), data=data)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet).count(), wtx_before,
        )
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_pending_balance_unchanged_after_submission(self):
        pending_before = self.wallet.pending_balance
        data = _valid_post_data(self.wallet)
        self.client.post(reverse("admin:treasury_request_new"), data=data)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.pending_balance, pending_before)


# ─────────────────────────────────────────────────────────────────────────
# O.3a pre-commit checkpoint — permission audit (2026-08-01)
#
# Fills the exact gaps identified against the required matrix that were
# NOT already covered above: standard Add/Change/Delete denied at the
# HTTP layer (not just via a direct has_*_permission() call), confirmation
# that no Approve/Reject/Execute/Cancel URL exists anywhere, that
# can_submit_treasury_request grants no access to Wallet/WalletTransaction/
# InternalTransfer/other Admin models, and that the new admin.py code
# never imports wallet_ledger/funded_payouts. No behavior was changed to
# make any of these pass — all were already true, only unverified.
# ─────────────────────────────────────────────────────────────────────────

class StandardAddChangeDeleteDeniedOverHttpTests(TestCase):
    """2a/2b/2c — Add/Change/Delete denied at the actual URL, not just
    via a direct has_*_permission() method call."""

    def setUp(self):
        self.operator = _make_operator(username="o3a_ckpt_add_change_delete")
        self.wallet = make_wallet()
        self.instance = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
            wallet=self.wallet, amount=Decimal("5.00"), reason="seed",
            requested_by=self.operator,
        )
        self.client = Client()
        self.client.force_login(self.operator)

    def test_standard_add_view_denied(self):
        resp = self.client.get(reverse("admin:simulator_treasuryoperationrequest_add"))
        self.assertEqual(resp.status_code, 403)

    def test_change_view_post_denied(self):
        url = reverse("admin:simulator_treasuryoperationrequest_change", args=[self.instance.pk])
        resp = self.client.post(url, data={"amount": "999.00"})
        self.assertEqual(resp.status_code, 403)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.amount, Decimal("5.00"))

    def test_delete_view_denied(self):
        url = reverse("admin:simulator_treasuryoperationrequest_delete", args=[self.instance.pk])
        resp = self.client.post(url, data={"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(TreasuryOperationRequest.objects.filter(pk=self.instance.pk).exists())


class NoExecuteCancelUrlTests(TestCase):
    """
    2d/2e/2f/2g — no Cancel URL exists anywhere; get_urls() exposes
    exactly the five custom URLs authorized so far beyond Django's own
    CRUD routes.

    O.3a-5 originally asserted that treasury_request_approve/reject
    ALSO did not exist yet — that was true at the time (this block only
    built submission) but was superseded on purpose by O.3b-3, which
    legitimately added both. O.3c-5a superseded it again on purpose,
    legitimately adding treasury_request_execute. O.3c-5b superseded it
    once more, legitimately adding treasury_request_recover. This test
    now locks down the current, still-accurate boundary: Approve/
    Reject/Execute/Recover exist, Cancel does not.
    """

    def test_get_urls_exposes_only_the_authorized_custom_urls(self):
        from django.contrib import admin as django_admin
        from django.urls import NoReverseMatch, reverse as _reverse

        from simulator.admin import TreasuryOperationRequestAdmin
        from simulator.models import TreasuryOperationRequest as TOR

        model_admin = TreasuryOperationRequestAdmin(TOR, django_admin.site)
        url_names = {p.name for p in model_admin.get_urls() if getattr(p, "name", None)}

        # Every custom (non-Django-CRUD) name in this ModelAdmin's own
        # get_urls() — the CRUD names are Django's own, not registered
        # by this class's override.
        custom_names = {n for n in url_names if not n.startswith("simulator_treasuryoperationrequest_")}
        self.assertEqual(
            custom_names,
            {
                "treasury_request_new", "treasury_request_approve",
                "treasury_request_reject", "treasury_request_execute",
                "treasury_request_recover",
            },
        )

        for hypothetical in ("treasury_request_cancel",):
            with self.assertRaises(NoReverseMatch):
                _reverse(f"admin:{hypothetical}")


class NoAdditionalModelAccessGrantedTests(TestCase):
    """
    3 — can_submit_treasury_request is scoped to TreasuryOperationRequest
    only. Confirms it grants no view/change access to Wallet,
    WalletTransaction, InternalTransfer, or an unrelated model
    (TradingAccount, as a sanity check against a totally different app
    surface), all of which rely on Django's own default has_view_
    permission() (checked against the standard view_/change_ perms for
    each model), never on this custom permission.
    """

    def setUp(self):
        self.operator = _make_operator(username="o3a_ckpt_no_extra_access")
        self.client = Client()
        self.client.force_login(self.operator)

    def test_no_access_to_wallet_changelist(self):
        resp = self.client.get(reverse("admin:simulator_wallet_changelist"))
        self.assertEqual(resp.status_code, 403)

    def test_no_access_to_wallettransaction_changelist(self):
        resp = self.client.get(reverse("admin:simulator_wallettransaction_changelist"))
        self.assertEqual(resp.status_code, 403)

    def test_no_access_to_internaltransfer_changelist(self):
        resp = self.client.get(reverse("admin:simulator_internaltransfer_changelist"))
        self.assertEqual(resp.status_code, 403)

    def test_no_access_to_unrelated_model_changelist(self):
        resp = self.client.get(reverse("admin:simulator_tradingaccount_changelist"))
        self.assertEqual(resp.status_code, 403)


class AdminModuleFinancialImportAuditTests(TestCase):
    """
    3 (financial invariants) — the O.3a-5 additions to admin.py
    (_treasury_wallet_confirmation_data, TreasuryOperationRequestAdmin.
    has_view_permission/changelist_view/get_urls/treasury_request_new_view)
    never import wallet_ledger/funded_payouts or call credit_wallet()/
    debit_wallet()/reconcile_wallet()/transfer_to_account()/
    transfer_to_wallet(). AST-based and scoped to exactly these members —
    admin.py as a whole legitimately imports wallet_ledger elsewhere
    (WalletAdmin.verify_wallet_consistency, O.2e-1), so a whole-file scan
    would false-positive on that unrelated, pre-existing, already-approved
    code.
    """

    def _members(self):
        from simulator.admin import TreasuryOperationRequestAdmin, _treasury_wallet_confirmation_data
        return [
            _treasury_wallet_confirmation_data,
            TreasuryOperationRequestAdmin.has_view_permission,
            TreasuryOperationRequestAdmin.changelist_view,
            TreasuryOperationRequestAdmin.get_urls,
            TreasuryOperationRequestAdmin.treasury_request_new_view,
        ]

    def test_no_wallet_ledger_or_funded_payouts_import(self):
        import ast
        import inspect
        import textwrap

        for member in self._members():
            tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module)
                    imported.update(a.name for a in node.names)
            self.assertNotIn("wallet_ledger", imported, member.__qualname__)
            self.assertNotIn("funded_payouts", imported, member.__qualname__)

    def test_no_wallet_movement_function_calls(self):
        import ast
        import inspect
        import textwrap

        forbidden = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
        }
        for member in self._members():
            tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if name:
                        called.add(name)
            self.assertEqual(called & forbidden, set(), member.__qualname__)


class ChangelistExplicitStatusTests(TestCase):
    """1a explicit — GET on the changelist itself returns 200 for an
    operator holding only can_submit_treasury_request (previously only
    asserted implicitly via assertContains in ChangelistButtonVisibilityTests)."""

    def test_changelist_returns_200(self):
        operator = _make_operator(username="o3a_ckpt_changelist_200")
        client = Client()
        client.force_login(operator)
        resp = client.get(reverse("admin:simulator_treasuryoperationrequest_changelist"))
        self.assertEqual(resp.status_code, 200)
