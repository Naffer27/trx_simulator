# simulator/tests/test_o2e1_treasury_admin.py
"""
Bloque O.2e-1 — Treasury Audit & Reconciliation.

Read-only admin surface for Wallet / WalletTransaction / InternalTransfer.
No test here touches wallet_ledger.py's write path (credit_wallet,
debit_wallet, transfer_to_account, transfer_to_wallet) beyond what
reconcile_wallet() itself already does internally (a pure read/compare,
verified exhaustively in test_wallet_ledger.py — not re-tested here).

This block does not implement Treasury Private Operations: no test here
exercises correction, reversal, manual adjustment, or any money-moving
admin action — those are explicitly out of scope.
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.admin import InternalTransferAdmin, WalletAdmin, WalletTransactionAdmin
from simulator.models import InternalTransfer, Wallet, WalletTransaction
from simulator.wallet_ledger import get_or_create_wallet

from .factories import make_account, make_user, make_wallet


class WalletAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(Wallet, admin_site._registry)

    def test_cannot_add(self):
        ma = WalletAdmin(Wallet, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = WalletAdmin(Wallet, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = WalletAdmin(Wallet, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_fields_readonly(self):
        ma = WalletAdmin(Wallet, admin_site)
        model_fields = {f.name for f in Wallet._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_delete_selected_not_available(self):
        ma = WalletAdmin(Wallet, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="o2e1_wallet_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_changelist_loads_without_error(self):
        make_wallet(initial_balance=Decimal("100"))
        staff = make_user(username="o2e1_wallet_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallet_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected_via_permission_check(self):
        staff = make_user(username="o2e1_wallet_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallet_add"))
        self.assertEqual(resp.status_code, 403)

    def test_change_view_read_only(self):
        wallet = make_wallet()
        staff = make_user(username="o2e1_wallet_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallet_change", args=[wallet.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'name="_save"', resp.content)

    def test_delete_view_rejected(self):
        wallet = make_wallet()
        staff = make_user(username="o2e1_wallet_staff5", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallet_delete", args=[wallet.pk]))
        self.assertEqual(resp.status_code, 403)


class WalletTransactionAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(WalletTransaction, admin_site._registry)

    def test_cannot_add(self):
        ma = WalletTransactionAdmin(WalletTransaction, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = WalletTransactionAdmin(WalletTransaction, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = WalletTransactionAdmin(WalletTransaction, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_fields_readonly(self):
        ma = WalletTransactionAdmin(WalletTransaction, admin_site)
        model_fields = {f.name for f in WalletTransaction._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        make_wallet(initial_balance=Decimal("50"))
        staff = make_user(username="o2e1_wtx_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallettransaction_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2e1_wtx_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallettransaction_add"))
        self.assertEqual(resp.status_code, 403)


class InternalTransferAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(InternalTransfer, admin_site._registry)

    def test_cannot_add(self):
        ma = InternalTransferAdmin(InternalTransfer, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = InternalTransferAdmin(InternalTransfer, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = InternalTransferAdmin(InternalTransfer, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_fields_readonly(self):
        ma = InternalTransferAdmin(InternalTransfer, admin_site)
        model_fields = {f.name for f in InternalTransfer._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        wallet = make_wallet(initial_balance=Decimal("200"))
        account = make_account()
        InternalTransfer.objects.create(
            wallet=wallet, trading_account=account,
            direction=InternalTransfer.DIR_TO_ACCOUNT, amount=Decimal("10"),
            status=InternalTransfer.ST_COMPLETED,
        )
        staff = make_user(username="o2e1_itx_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_internaltransfer_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2e1_itx_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_internaltransfer_add"))
        self.assertEqual(resp.status_code, 403)


class WalletTransactionInlineTests(TestCase):

    def test_wallet_change_view_shows_transaction_history_read_only(self):
        wallet = make_wallet(initial_balance=Decimal("75"))
        staff = make_user(username="o2e1_inline_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_wallet_change", args=[wallet.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "DEPOSIT")

    def test_inline_has_no_add_permission(self):
        from simulator.admin import WalletTransactionInline
        inline = WalletTransactionInline(Wallet, admin_site)
        self.assertFalse(inline.has_add_permission(request=None))

    def test_inline_has_no_change_permission(self):
        from simulator.admin import WalletTransactionInline
        inline = WalletTransactionInline(Wallet, admin_site)
        self.assertFalse(inline.has_change_permission(request=None))

    def test_inline_has_no_delete_permission(self):
        from simulator.admin import WalletTransactionInline
        inline = WalletTransactionInline(Wallet, admin_site)
        self.assertFalse(inline.has_delete_permission(request=None))


class VerifyWalletConsistencyActionTests(TestCase):
    """
    The single action authorized in this block. Must call reconcile_wallet()
    (untouched, wallet_ledger.py) and only ever report — never write.
    """

    def setUp(self):
        self.staff = make_user(username="o2e1_action_staff", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_action_registered(self):
        ma = WalletAdmin(Wallet, admin_site)
        request = RequestFactory().get("/")
        request.user = self.staff
        self.assertIn("verify_wallet_consistency", ma.get_actions(request))

    def test_consistent_wallet_reports_success_and_writes_nothing(self):
        wallet = make_wallet(initial_balance=Decimal("300"))
        balance_before = wallet.available_balance
        tx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()

        url = reverse("admin:simulator_wallet_changelist")
        resp = self.client.post(url, {
            "action": "verify_wallet_consistency",
            "_selected_action": [str(wallet.pk)],
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "all consistent")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), tx_count_before,
        )

    def test_drifted_wallet_reports_warning_without_correcting_it(self):
        wallet = make_wallet(initial_balance=Decimal("150"))
        # Simulate a pre-existing drift the SAME way test_wallet_ledger.py's
        # own reconcile_wallet tests do — direct .update(), bypassing the
        # ledger. The action must only REPORT this, never fix it.
        Wallet.objects.filter(pk=wallet.pk).update(available_balance=Decimal("999"))

        url = reverse("admin:simulator_wallet_changelist")
        resp = self.client.post(url, {
            "action": "verify_wallet_consistency",
            "_selected_action": [str(wallet.pk)],
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "with drift")

        wallet.refresh_from_db()
        # Still 999 — the action must NOT have corrected it.
        self.assertEqual(wallet.available_balance, Decimal("999"))

    def test_action_creates_no_wallet_transaction(self):
        wallet = make_wallet(initial_balance=Decimal("60"))
        tx_count_before = WalletTransaction.objects.count()

        url = reverse("admin:simulator_wallet_changelist")
        self.client.post(url, {
            "action": "verify_wallet_consistency",
            "_selected_action": [str(wallet.pk)],
        })

        self.assertEqual(WalletTransaction.objects.count(), tx_count_before)

    def test_action_creates_no_internal_transfer(self):
        wallet = make_wallet(initial_balance=Decimal("60"))
        itx_count_before = InternalTransfer.objects.count()

        url = reverse("admin:simulator_wallet_changelist")
        self.client.post(url, {
            "action": "verify_wallet_consistency",
            "_selected_action": [str(wallet.pk)],
        })

        self.assertEqual(InternalTransfer.objects.count(), itx_count_before)


class ScopeGuardTests(TestCase):
    """No other Treasury model was registered in this block."""

    def test_only_the_three_treasury_models_are_registered(self):
        # NOTE: TOTPDevice/TermsAcceptance/EmailVerification were deliberately
        # registered by the later Compliance Center block (O.2f-1) — removed
        # from this guard list, which only asserts about Treasury's own scope.
        from simulator.models import (
            AccountEquitySnapshot, BrokerEquitySnapshot, SymbolExposure,
            TraderClassExposure,
        )
        for model in (
            AccountEquitySnapshot, BrokerEquitySnapshot, SymbolExposure,
            TraderClassExposure,
        ):
            self.assertNotIn(model, admin_site._registry)
