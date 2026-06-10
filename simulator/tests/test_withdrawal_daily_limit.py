# simulator/tests/test_withdrawal_daily_limit.py
"""
Daily withdrawal limit — MAX_WITHDRAWAL_DAILY_USD (default $1 500).

Counting rule: pending, processing, approved, completed count toward the limit.
               rejected, failed do NOT count (money was returned to wallet).

Covers:
  1.  GET /withdraw/ shows daily_limit in page.
  2.  GET /withdraw/ shows daily_used (0 when no withdrawals today).
  3.  GET /withdraw/ shows daily_avail equal to full limit when no prior WRs.
  4.  POST blocked when amount exceeds full daily limit.
  5.  POST blocked when remaining daily quota is insufficient.
  6.  POST succeeds when amount is exactly equal to remaining quota.
  7.  PENDING withdrawals count toward used amount.
  8.  PROCESSING withdrawals count toward used amount.
  9.  APPROVED withdrawals count toward used amount.
  10. COMPLETED withdrawals count toward used amount.
  11. REJECTED withdrawals do NOT count toward used amount.
  12. FAILED withdrawals do NOT count toward used amount.
  13. Daily limit resets next day (yesterday's WRs don't count).
  14. Blocked request does not create a WithdrawalRequest.
  15. Blocked request does not debit wallet.
  16. Error message mentions the daily limit.
  17. Error message mentions the used amount.
  18. Error message mentions the available amount.
  19. Other users' withdrawals do not affect this user's limit.
  20. Limit from settings.MAX_WITHDRAWAL_DAILY_USD is respected.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from simulator.models import Wallet, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved

User = get_user_model()

WITHDRAW_URL = "/withdraw/"

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)
_PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")


def _make_device(user) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        secret="b64:MFSWS3TFMJPXI6TFNFXW4IDXNFXQ====",
        confirmed=True,
    )


def _wr_payload(amount="50.00"):
    return {
        "amount_usd":      amount,
        "crypto_currency": "btc",
        "wallet_address":  "bc1qtest000000000000000000000000000000000",
        "otp_code":        "000000",
    }


def _seed_wr(user, amount, status, days_ago=0):
    """Create a WithdrawalRequest directly (bypassing the view) for limit testing."""
    wallet, _ = Wallet.objects.get_or_create(user=user)
    wr = WithdrawalRequest.objects.create(
        user=user,
        amount_usd=Decimal(str(amount)),
        crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=status,
    )
    if days_ago:
        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
    return wr


# ── GET display ───────────────────────────────────────────────────────────────

class DailyLimitDisplayTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user   = make_user(email="dld@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_get_shows_daily_limit(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "1500")

    def test_get_shows_daily_used_zero_when_no_prior_withdrawals(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "0.00")

    def test_get_shows_daily_avail_equal_to_full_limit_when_unused(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.context["daily_avail"], Decimal("1500"))

    def test_get_daily_used_reflects_existing_pending(self):
        _seed_wr(self.user, "400", WithdrawalRequest.STATUS_PENDING)
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.context["daily_used"], Decimal("400"))

    def test_get_daily_avail_decreases_by_used(self):
        _seed_wr(self.user, "600", WithdrawalRequest.STATUS_PENDING)
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.context["daily_avail"], Decimal("900"))


# ── Gate enforcement ──────────────────────────────────────────────────────────

@override_settings(MAX_WITHDRAWAL_DAILY_USD=1500)
class DailyLimitGateTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="dlg@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def test_amount_over_full_daily_limit_is_blocked(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("1600.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_amount_exceeding_remaining_quota_is_blocked(self):
        _seed_wr(self.user, "1400", WithdrawalRequest.STATUS_PENDING)
        r = self.client.post(WITHDRAW_URL, _wr_payload("200.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            WithdrawalRequest.objects.filter(user=self.user, status=WithdrawalRequest.STATUS_PENDING).count(),
            1,
        )

    def test_amount_equal_to_remaining_quota_succeeds(self):
        _seed_wr(self.user, "1000", WithdrawalRequest.STATUS_COMPLETED)
        r = self.client.post(WITHDRAW_URL, _wr_payload("500.00"))
        self.assertRedirects(r, "/withdraw/history/", fetch_redirect_response=False)
        self.assertEqual(
            WithdrawalRequest.objects.filter(user=self.user, status=WithdrawalRequest.STATUS_PENDING).count(),
            1,
        )

    def test_blocked_request_does_not_create_wr(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("1600.00"))
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    def test_blocked_request_does_not_debit_wallet(self):
        self.client.post(WITHDRAW_URL, _wr_payload("1600.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    def test_error_message_mentions_daily_limit(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("1600.00"))
        self.assertContains(r, "1,500.00")

    def test_error_message_mentions_used_amount(self):
        _seed_wr(self.user, "800", WithdrawalRequest.STATUS_APPROVED)
        r = self.client.post(WITHDRAW_URL, _wr_payload("800.00"))
        self.assertContains(r, "800.00")

    def test_error_message_mentions_available_amount(self):
        _seed_wr(self.user, "900", WithdrawalRequest.STATUS_PENDING)
        r = self.client.post(WITHDRAW_URL, _wr_payload("700.00"))
        self.assertContains(r, "600.00")


# ── Counting rules ────────────────────────────────────────────────────────────

@override_settings(MAX_WITHDRAWAL_DAILY_USD=1500)
class DailyLimitCountingTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="dlc@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    def _daily_used(self):
        return self.client.get(WITHDRAW_URL).context["daily_used"]

    def test_pending_counts_toward_limit(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_processing_counts_toward_limit(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_approved_counts_toward_limit(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_APPROVED)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_completed_counts_toward_limit(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_COMPLETED)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_rejected_does_not_count(self):
        _seed_wr(self.user, "500", WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(self._daily_used(), Decimal("0"))

    def test_failed_does_not_count(self):
        _seed_wr(self.user, "500", WithdrawalRequest.STATUS_FAILED)
        self.assertEqual(self._daily_used(), Decimal("0"))

    def test_yesterdays_withdrawals_do_not_count(self):
        _seed_wr(self.user, "1400", WithdrawalRequest.STATUS_COMPLETED, days_ago=1)
        self.assertEqual(self._daily_used(), Decimal("0"))

    def test_other_users_withdrawals_do_not_count(self):
        other = make_user(email="other@test.com")
        _seed_wr(other, "1400", WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(self._daily_used(), Decimal("0"))

    def test_multiple_statuses_summed_correctly(self):
        _seed_wr(self.user, "200", WithdrawalRequest.STATUS_PENDING)
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_COMPLETED)
        _seed_wr(self.user, "100", WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(self._daily_used(), Decimal("500"))


# ── Custom limit via settings ─────────────────────────────────────────────────

class DailyLimitSettingsTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="dls@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    @override_settings(MAX_WITHDRAWAL_DAILY_USD=100)
    def test_low_custom_limit_blocks_large_withdrawal(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("150.00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.filter(user=self.user).count(), 0)

    @override_settings(MAX_WITHDRAWAL_DAILY_USD=100)
    def test_low_custom_limit_allows_within_limit(self):
        r = self.client.post(WITHDRAW_URL, _wr_payload("50.00"))
        self.assertRedirects(r, "/withdraw/history/", fetch_redirect_response=False)
