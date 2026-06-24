# simulator/tests/test_ops_dashboard.py
"""
Staff Ops Dashboard — access control, context data, and sidebar visibility.

Tests:
  1.  Anonymous GET /staff/ops/ redirects to login.
  2.  Normal (non-staff) user GET redirects to login.
  3.  Staff user GET returns 200.
  4.  Context contains open_tickets_count.
  5.  Context contains kyc_pending_count.
  6.  Context contains withdrawals_pending_count.
  7.  Context contains deposits_confirmed_count.
  8.  Context contains new_users_7d.
  9.  open_tickets_count reflects open/pending support tickets only.
  10. kyc_pending_count reflects only pending KYC profiles.
  11. withdrawals_pending_count reflects pending+processing WRs only.
  12. open_tickets list contains at most 10 items.
  13. kyc_pending list contains at most 10 items.
  14. withdrawals_pending list contains at most 10 items.
  15. deposits_recent list contains at most 10 items.
  16. Sidebar on /support/ shows Ops link for staff users.
  17. Sidebar on /support/ hides Ops link for non-staff users.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import (
    Deposit, KYCProfile, SupportTicket, WithdrawalRequest,
)
from simulator.tests.factories import make_user

OPS_URL = reverse("simulator:ops_panel")
LOGIN_URL = reverse("simulator:login")
SUPPORT_URL = reverse("simulator:support")


def _make_ticket(user, status=SupportTicket.STATUS_OPEN):
    return SupportTicket.objects.create(
        user=user,
        category=SupportTicket.CATEGORY_OTHER,
        subject="Test ticket",
        message="Test message",
        status=status,
        priority=SupportTicket.PRIORITY_NORMAL,
    )


def _make_kyc(user, status=KYCProfile.STATUS_PENDING):
    profile, _ = KYCProfile.objects.get_or_create(user=user)
    profile.status = status
    profile.submitted_at = timezone.now()
    profile.save(update_fields=["status", "submitted_at"])
    return profile


def _make_withdrawal(user, status=WithdrawalRequest.STATUS_PENDING):
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd="100.00",
        crypto_currency="USDT",
        wallet_address="Txxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        status=status,
    )


def _make_deposit(user, credited=True):
    dep = Deposit.objects.create(
        user=user,
        nowpayments_payment_id=f"pay_{user.pk}_{Deposit.objects.count()}",
        amount_usd="50.00",
        crypto_currency="USDT",
        credited=credited,
    )
    if credited:
        dep.credited_at = timezone.now()
        dep.save(update_fields=["credited_at"])
    return dep


# ── Access control ────────────────────────────────────────────────────────────

class OpsAccessControlTests(TestCase):
    def test_anonymous_redirects_to_login(self):
        response = self.client.get(OPS_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(LOGIN_URL, response["Location"])

    def test_normal_user_redirects_to_login(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.get(OPS_URL)
        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)

    def test_staff_user_gets_200(self):
        staff = make_user(is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(OPS_URL)
        self.assertEqual(response.status_code, 200)


# ── Context keys present ──────────────────────────────────────────────────────

class OpsContextKeysTests(TestCase):
    def setUp(self):
        self.staff = make_user(is_staff=True)
        self.client.force_login(self.staff)
        self.ctx = self.client.get(OPS_URL).context

    def test_open_tickets_count_in_context(self):
        self.assertIn("open_tickets_count", self.ctx)

    def test_kyc_pending_count_in_context(self):
        self.assertIn("kyc_pending_count", self.ctx)

    def test_withdrawals_pending_count_in_context(self):
        self.assertIn("withdrawals_pending_count", self.ctx)

    def test_deposits_confirmed_count_in_context(self):
        self.assertIn("deposits_confirmed_count", self.ctx)

    def test_new_users_7d_in_context(self):
        self.assertIn("new_users_7d", self.ctx)


# ── Count accuracy ────────────────────────────────────────────────────────────

class OpsCountAccuracyTests(TestCase):
    def setUp(self):
        self.staff = make_user(is_staff=True)
        self.client.force_login(self.staff)

    def test_open_tickets_count_matches_open_and_pending_only(self):
        user = make_user()
        _make_ticket(user, status=SupportTicket.STATUS_OPEN)
        _make_ticket(user, status=SupportTicket.STATUS_PENDING)
        _make_ticket(user, status=SupportTicket.STATUS_RESOLVED)  # excluded
        _make_ticket(user, status=SupportTicket.STATUS_CLOSED)    # excluded
        ctx = self.client.get(OPS_URL).context
        self.assertEqual(ctx["open_tickets_count"], 2)

    def test_kyc_pending_count_matches_pending_only(self):
        u1 = make_user()
        u2 = make_user()
        u3 = make_user()
        _make_kyc(u1, KYCProfile.STATUS_PENDING)
        _make_kyc(u2, KYCProfile.STATUS_PENDING)
        _make_kyc(u3, KYCProfile.STATUS_APPROVED)  # excluded
        ctx = self.client.get(OPS_URL).context
        self.assertEqual(ctx["kyc_pending_count"], 2)

    def test_withdrawals_pending_count_matches_pending_and_processing(self):
        user = make_user()
        _make_withdrawal(user, WithdrawalRequest.STATUS_PENDING)
        _make_withdrawal(user, WithdrawalRequest.STATUS_PROCESSING)
        _make_withdrawal(user, WithdrawalRequest.STATUS_APPROVED)   # excluded
        _make_withdrawal(user, WithdrawalRequest.STATUS_COMPLETED)  # excluded
        ctx = self.client.get(OPS_URL).context
        self.assertEqual(ctx["withdrawals_pending_count"], 2)


# ── List cap at 10 ────────────────────────────────────────────────────────────

class OpsListCapTests(TestCase):
    def setUp(self):
        self.staff = make_user(is_staff=True)
        self.client.force_login(self.staff)

    def test_open_tickets_list_capped_at_10(self):
        user = make_user()
        for _ in range(12):
            _make_ticket(user, status=SupportTicket.STATUS_OPEN)
        ctx = self.client.get(OPS_URL).context
        self.assertLessEqual(len(ctx["open_tickets"]), 10)

    def test_kyc_pending_list_capped_at_10(self):
        for _ in range(12):
            _make_kyc(make_user(), KYCProfile.STATUS_PENDING)
        ctx = self.client.get(OPS_URL).context
        self.assertLessEqual(len(ctx["kyc_pending"]), 10)

    def test_withdrawals_pending_list_capped_at_10(self):
        user = make_user()
        for _ in range(12):
            _make_withdrawal(user, WithdrawalRequest.STATUS_PENDING)
        ctx = self.client.get(OPS_URL).context
        self.assertLessEqual(len(ctx["withdrawals_pending"]), 10)

    def test_deposits_recent_list_capped_at_10(self):
        user = make_user()
        for i in range(12):
            Deposit.objects.create(
                user=user,
                nowpayments_payment_id=f"pay_cap_{i}",
                amount_usd="10.00",
                crypto_currency="USDT",
                credited=True,
                credited_at=timezone.now(),
            )
        ctx = self.client.get(OPS_URL).context
        self.assertLessEqual(len(ctx["deposits_recent"]), 10)


# ── Sidebar visibility ────────────────────────────────────────────────────────

class OpsSidebarTests(TestCase):
    def test_ops_link_visible_for_staff(self):
        staff = make_user(is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(SUPPORT_URL)
        self.assertContains(response, OPS_URL)

    def test_ops_link_hidden_for_non_staff(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.get(SUPPORT_URL)
        self.assertNotContains(response, OPS_URL)
