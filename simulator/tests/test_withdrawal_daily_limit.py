# simulator/tests/test_withdrawal_daily_limit.py
"""
daily_used — display informativo en /withdraw/.

El límite diario fijo fue eliminado (decisión de producto: el dinero es del usuario).
daily_used se sigue calculando y mostrando como información al usuario,
pero ya NO bloquea el retiro.

Counting rule (para display): pending, processing, approved, completed cuentan.
                               rejected, failed NO cuentan (dinero devuelto a wallet).

Covers:
  1.  GET /withdraw/ muestra daily_used en contexto.
  2.  GET muestra daily_used = 0 cuando no hay retiros hoy.
  3.  GET muestra daily_used refleja WR pendiente existente.
  4.  PENDING cuenta en daily_used.
  5.  PROCESSING cuenta en daily_used.
  6.  APPROVED cuenta en daily_used.
  7.  COMPLETED cuenta en daily_used.
  8.  REJECTED no cuenta en daily_used.
  9.  FAILED no cuenta en daily_used.
  10. WRs de ayer no cuentan en daily_used.
  11. WRs de otros usuarios no afectan daily_used.
  12. Múltiples estados se suman correctamente.
  13. daily_limit ya no está en el contexto (gate eliminado).
  14. daily_avail ya no está en el contexto (gate eliminado).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
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


def _seed_wr(user, amount, status, days_ago=0):
    """Create a WithdrawalRequest directly (bypassing the view) for display testing."""
    Wallet.objects.get_or_create(user=user)
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


# ── GET display — daily_used ──────────────────────────────────────────────────

class DailyUsedDisplayTests(TestCase):
    """
    daily_used is still shown for informational purposes.
    daily_limit and daily_avail are no longer in the context.
    """

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user   = make_user(email="dld@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def test_get_daily_used_in_context(self):
        """daily_used is present in template context."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertIn("daily_used", resp.context)

    def test_get_daily_used_zero_when_no_prior_withdrawals(self):
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.context["daily_used"], Decimal("0"))

    def test_get_daily_used_reflects_existing_pending(self):
        _seed_wr(self.user, "400", WithdrawalRequest.STATUS_PENDING)
        resp = self.client.get(WITHDRAW_URL)
        self.assertEqual(resp.context["daily_used"], Decimal("400"))

    def test_daily_limit_not_in_context(self):
        """daily_limit was removed — gate is gone."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("daily_limit", resp.context)

    def test_daily_avail_not_in_context(self):
        """daily_avail was removed — gate is gone."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("daily_avail", resp.context)


# ── Counting rules for daily_used display ────────────────────────────────────

class DailyUsedCountingTests(TestCase):
    """
    daily_used = sum of today's WRs with active statuses.
    These rules govern what shows in the informational display.
    """

    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.user   = make_user(email="dlc@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()

    def _daily_used(self):
        return self.client.get(WITHDRAW_URL).context["daily_used"]

    def test_pending_counts_toward_daily_used(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_processing_counts_toward_daily_used(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_approved_counts_toward_daily_used(self):
        _seed_wr(self.user, "300", WithdrawalRequest.STATUS_APPROVED)
        self.assertEqual(self._daily_used(), Decimal("300"))

    def test_completed_counts_toward_daily_used(self):
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
