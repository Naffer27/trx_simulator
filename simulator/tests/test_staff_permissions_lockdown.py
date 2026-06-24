"""
simulator/tests/test_staff_permissions_lockdown.py — Bloque I.1

Verifies that non-superuser staff are blocked from critical admin actions:
  - approve_withdrawals, reject_withdrawals, reset_balance (module-level)
  - admin_approve_sim_payout, admin_approve_internal_payout (FundedPayoutRequestAdmin)
  - dealing desk reset and force_close (custom view)
  - LedgerEntry add/change/delete permissions and full readonly for non-superuser
  - ChallengeProduct add/change/delete superuser-only
  - TradingAccount balance/equity/initial_balance readonly for non-superuser
  - np_diagnostics_view superuser-only (403 for staff)
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import TestCase, RequestFactory
from django.urls import reverse

from simulator.admin import (
    approve_withdrawals,
    reject_withdrawals,
    reset_balance,
)
from simulator.admin import (
    TradingAccountAdmin,
    LedgerEntryAdmin,
    ChallengeProductAdmin,
    FundedPayoutRequestAdmin,
)
from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    TradingAccount,
    WithdrawalRequest,
)

User = get_user_model()
_seq = 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _uid():
    global _seq
    _seq += 1
    return _seq


def _make_superuser():
    n = _uid()
    return User.objects.create_superuser(
        username=f"super_{n}",
        email=f"super_{n}@ex.com",
        password="pass",
    )


def _make_staff():
    n = _uid()
    return User.objects.create_user(
        username=f"staff_{n}",
        email=f"staff_{n}@ex.com",
        password="pass",
        is_staff=True,
        is_superuser=False,
    )


def _make_product():
    n = _uid()
    return ChallengeProduct.objects.create(
        name=f"I1-Prod-{n}",
        account_size=Decimal("10000.00"),
        price_usd=Decimal("99.00"),
        is_active=True,
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=0,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=0,
        p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"),
        max_open_positions=5,
        profit_split_pct=Decimal("80.00"),
    )


def _make_funded_account(user):
    product = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _make_wr(user, amount=Decimal("100.00")):
    from simulator.wallet_ledger import get_or_create_wallet, credit_wallet
    from simulator.models import WalletTransaction
    wallet, _ = get_or_create_wallet(user)
    credit_wallet(wallet.id, amount, WalletTransaction.TX_DEPOSIT, note="seed")
    wallet.refresh_from_db()
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd=amount,
        crypto_currency="BTC",
        wallet_address="bc1qtest",
        status=WithdrawalRequest.STATUS_PENDING,
    )


def _admin_request(factory, user):
    """Build a fake admin POST request with message storage attached."""
    req = factory.post("/admin/", {})
    req.user = user
    req.session = "session"
    req._messages = FallbackStorage(req)
    return req


# ─────────────────────────────────────────────────────────────────────────────
# Test: module-level action guards
# ─────────────────────────────────────────────────────────────────────────────

class TestModuleLevelActionGuards(TestCase):
    """approve_withdrawals, reject_withdrawals, reset_balance block non-superuser."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.trader = User.objects.create_user(
            username=f"trader_{_uid()}", email=f"t_{_uid()}@ex.com", password="pass"
        )
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        from simulator.admin import WithdrawalRequestAdmin
        self.wr_admin = WithdrawalRequestAdmin(WithdrawalRequest, self.admin_site)
        from simulator.admin import TradingAccountAdmin
        self.ta_admin = TradingAccountAdmin(TradingAccount, self.admin_site)

    def test_staff_cannot_approve_withdrawals(self):
        wr = _make_wr(self.trader)
        req = _admin_request(self.factory, self.staff)
        approve_withdrawals(self.wr_admin, req, WithdrawalRequest.objects.filter(pk=wr.pk))
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_staff_cannot_reject_withdrawals(self):
        wr = _make_wr(self.trader)
        req = _admin_request(self.factory, self.staff)
        reject_withdrawals(self.wr_admin, req, WithdrawalRequest.objects.filter(pk=wr.pk))
        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PENDING)

    def test_staff_cannot_reset_balance(self):
        enrollment = _make_funded_account(self.trader)
        enrollment.refresh_from_db()
        account = enrollment.funded_account
        original_balance = Decimal(str(account.balance))
        account.balance = original_balance + Decimal("500")
        account.save(update_fields=["balance"])

        req = _admin_request(self.factory, self.staff)
        reset_balance(self.ta_admin, req, TradingAccount.objects.filter(pk=account.pk))

        account.refresh_from_db()
        self.assertEqual(account.balance, original_balance + Decimal("500"))

    def test_superuser_reset_balance_executes(self):
        enrollment = _make_funded_account(self.trader)
        enrollment.refresh_from_db()
        account = enrollment.funded_account
        initial = account.initial_balance or Decimal("10000")
        account.balance = initial + Decimal("999")
        account.save(update_fields=["balance"])

        req = _admin_request(self.factory, self.superuser)
        reset_balance(self.ta_admin, req, TradingAccount.objects.filter(pk=account.pk))

        account.refresh_from_db()
        self.assertEqual(account.balance, initial)


# ─────────────────────────────────────────────────────────────────────────────
# Test: FundedPayoutRequestAdmin method action guards
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedPayoutAdminActionGuards(TestCase):
    """admin_approve_sim_payout and admin_approve_internal_payout block non-superuser."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.trader = User.objects.create_user(
            username=f"trader_{_uid()}", email=f"t_{_uid()}@ex.com", password="pass"
        )
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.fpr_admin = FundedPayoutRequestAdmin(FundedPayoutRequest, self.admin_site)

        enrollment = _make_funded_account(self.trader)
        enrollment.refresh_from_db()
        funded_account = enrollment.funded_account
        funded_config = FundedConfig.objects.get(enrollment=enrollment)

        initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
        profit = Decimal("800.00")
        funded_account.balance = initial + profit
        funded_account.equity = funded_account.balance
        funded_account.save(update_fields=["balance", "equity"])

        trader_cut = (profit * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
        broker_cut = profit - trader_cut

        self.fpr_sim = FundedPayoutRequest.objects.create(
            user=self.trader,
            enrollment=enrollment,
            funded_account=funded_account,
            funded_config=funded_config,
            funded_type=FundedConfig.FUNDED_SIM,
            cycle_profit=profit,
            trader_cut=trader_cut,
            broker_cut=broker_cut,
            profit_split_pct=Decimal("80.00"),
            balance_snapshot=funded_account.balance,
            initial_balance_snapshot=initial,
            status=FundedPayoutRequest.ST_PENDING,
        )

        enrollment2 = _make_funded_account(self.trader)
        enrollment2.refresh_from_db()
        fa2 = enrollment2.funded_account
        fc2 = FundedConfig.objects.get(enrollment=enrollment2)
        i2 = Decimal(str(fa2.initial_balance or fa2.balance))
        p2 = Decimal("600.00")
        fa2.balance = i2 + p2
        fa2.equity = fa2.balance
        fa2.save(update_fields=["balance", "equity"])
        tc2 = (p2 * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
        self.fpr_internal = FundedPayoutRequest.objects.create(
            user=self.trader,
            enrollment=enrollment2,
            funded_account=fa2,
            funded_config=fc2,
            funded_type=FundedConfig.FUNDED_INTERNAL,
            cycle_profit=p2,
            trader_cut=tc2,
            broker_cut=p2 - tc2,
            profit_split_pct=Decimal("80.00"),
            balance_snapshot=fa2.balance,
            initial_balance_snapshot=i2,
            status=FundedPayoutRequest.ST_PENDING,
        )

    def test_staff_cannot_approve_sim_payout(self):
        req = _admin_request(self.factory, self.staff)
        self.fpr_admin.admin_approve_sim_payout(
            req, FundedPayoutRequest.objects.filter(pk=self.fpr_sim.pk)
        )
        self.fpr_sim.refresh_from_db()
        self.assertEqual(self.fpr_sim.status, FundedPayoutRequest.ST_PENDING)

    def test_staff_cannot_approve_internal_payout(self):
        req = _admin_request(self.factory, self.staff)
        self.fpr_admin.admin_approve_internal_payout(
            req, FundedPayoutRequest.objects.filter(pk=self.fpr_internal.pk)
        )
        self.fpr_internal.refresh_from_db()
        self.assertEqual(self.fpr_internal.status, FundedPayoutRequest.ST_PENDING)


# ─────────────────────────────────────────────────────────────────────────────
# Test: LedgerEntry permissions
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerEntryPermissions(TestCase):
    """Non-superuser staff cannot add/change/delete LedgerEntry; all fields read-only."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.le_admin = LedgerEntryAdmin(LedgerEntry, self.admin_site)
        self.trader = User.objects.create_user(
            username=f"trader_{_uid()}", email=f"t_{_uid()}@ex.com", password="pass"
        )

    def _staff_req(self):
        req = self.factory.get("/admin/")
        req.user = self.staff
        return req

    def _super_req(self):
        req = self.factory.get("/admin/")
        req.user = self.superuser
        return req

    def test_staff_has_no_add_permission(self):
        self.assertFalse(self.le_admin.has_add_permission(self._staff_req()))

    def test_staff_has_no_change_permission(self):
        self.assertFalse(self.le_admin.has_change_permission(self._staff_req()))

    def test_staff_has_no_delete_permission(self):
        self.assertFalse(self.le_admin.has_delete_permission(self._staff_req()))

    def test_superuser_has_add_permission(self):
        self.assertTrue(self.le_admin.has_add_permission(self._super_req()))

    def test_superuser_has_change_permission(self):
        self.assertTrue(self.le_admin.has_change_permission(self._super_req()))

    def test_superuser_has_delete_permission(self):
        self.assertTrue(self.le_admin.has_delete_permission(self._super_req()))

    def test_staff_all_fields_readonly(self):
        req = self._staff_req()
        ro = self.le_admin.get_readonly_fields(req, obj=object())
        for field in ("account", "event_type", "amount", "balance_after", "meta", "created_at"):
            self.assertIn(field, ro, msg=f"Expected '{field}' in readonly_fields for staff")

    def test_superuser_amount_not_readonly(self):
        req = self._super_req()
        ro = self.le_admin.get_readonly_fields(req, obj=object())
        self.assertNotIn("amount", ro)
        self.assertNotIn("event_type", ro)


# ─────────────────────────────────────────────────────────────────────────────
# Test: ChallengeProduct permissions
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeProductPermissions(TestCase):
    """Non-superuser staff cannot add/change/delete ChallengeProduct."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.cp_admin = ChallengeProductAdmin(ChallengeProduct, self.admin_site)

    def _staff_req(self):
        req = self.factory.get("/admin/")
        req.user = self.staff
        return req

    def _super_req(self):
        req = self.factory.get("/admin/")
        req.user = self.superuser
        return req

    def test_staff_cannot_add(self):
        self.assertFalse(self.cp_admin.has_add_permission(self._staff_req()))

    def test_staff_cannot_change(self):
        self.assertFalse(self.cp_admin.has_change_permission(self._staff_req()))

    def test_staff_cannot_delete(self):
        self.assertFalse(self.cp_admin.has_delete_permission(self._staff_req()))

    def test_superuser_can_add(self):
        self.assertTrue(self.cp_admin.has_add_permission(self._super_req()))

    def test_superuser_can_change(self):
        self.assertTrue(self.cp_admin.has_change_permission(self._super_req()))

    def test_superuser_can_delete(self):
        self.assertTrue(self.cp_admin.has_delete_permission(self._super_req()))


# ─────────────────────────────────────────────────────────────────────────────
# Test: TradingAccount balance fields readonly for non-superuser
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingAccountBalanceReadonly(TestCase):
    """balance, equity, initial_balance are read-only for non-superuser on change form."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.trader = User.objects.create_user(
            username=f"trader_{_uid()}", email=f"t_{_uid()}@ex.com", password="pass"
        )
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.ta_admin = TradingAccountAdmin(TradingAccount, self.admin_site)
        enrollment = _make_funded_account(self.trader)
        enrollment.refresh_from_db()
        self.account = enrollment.funded_account

    def _staff_req(self):
        req = self.factory.get("/admin/")
        req.user = self.staff
        return req

    def _super_req(self):
        req = self.factory.get("/admin/")
        req.user = self.superuser
        return req

    def test_staff_sees_balance_readonly(self):
        ro = self.ta_admin.get_readonly_fields(self._staff_req(), obj=self.account)
        self.assertIn("balance", ro)
        self.assertIn("equity", ro)
        self.assertIn("initial_balance", ro)

    def test_superuser_balance_not_readonly(self):
        ro = self.ta_admin.get_readonly_fields(self._super_req(), obj=self.account)
        self.assertNotIn("balance", ro)
        self.assertNotIn("equity", ro)
        self.assertNotIn("initial_balance", ro)

    def test_add_form_balance_not_readonly_for_staff(self):
        ro = self.ta_admin.get_readonly_fields(self._staff_req(), obj=None)
        self.assertNotIn("balance", ro)


# ─────────────────────────────────────────────────────────────────────────────
# Test: np_diagnostics_view superuser-only
# ─────────────────────────────────────────────────────────────────────────────

class TestNpDiagnosticsView(TestCase):
    """GET /api/np-check/ must return 403 for is_staff, 200 for superuser."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()

    def test_staff_gets_403(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("simulator:np_check"))
        self.assertEqual(response.status_code, 403)

    @patch("simulator.nowpayments.api_status", return_value={"ok": True})
    def test_superuser_gets_200(self, _mock):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("simulator:np_check"))
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Test: Dealing desk reset/force_close blocked for non-superuser
# ─────────────────────────────────────────────────────────────────────────────

class TestDealingDeskSuperuserGuard(TestCase):
    """POST dealing-desk reset/force_close redirects without changes for non-superuser."""

    def setUp(self):
        self.superuser = _make_superuser()
        self.staff = _make_staff()
        self.trader = User.objects.create_user(
            username=f"trader_{_uid()}", email=f"t_{_uid()}@ex.com", password="pass"
        )
        enrollment = _make_funded_account(self.trader)
        enrollment.refresh_from_db()
        self.account = enrollment.funded_account
        self.original_balance = Decimal(str(self.account.balance))
        self.account.balance = self.original_balance + Decimal("2000")
        self.account.equity = self.account.balance
        self.account.save(update_fields=["balance", "equity"])
        self.desk_url = reverse(
            "admin:simulator_tradingaccount_dealing_desk",
            args=[self.account.pk],
        )

    def test_staff_cannot_reset_via_dealing_desk(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.desk_url, {"action": "reset"}, follow=False)
        self.assertIn(response.status_code, (302, 200))
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, self.original_balance + Decimal("2000"))

    def test_staff_cannot_force_close_via_dealing_desk(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.desk_url, {"action": "force_close"}, follow=False)
        self.assertIn(response.status_code, (302, 200))
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, self.original_balance + Decimal("2000"))

    def test_superuser_can_reset_via_dealing_desk(self):
        self.client.force_login(self.superuser)
        response = self.client.post(self.desk_url, {"action": "reset"}, follow=False)
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, self.original_balance)
