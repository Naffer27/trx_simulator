"""
simulator/tests/test_withdraw_no_max_limit.py

Verifica el comportamiento tras eliminar el límite máximo diario de retiro
(regla 9/10 del Design Lock WITHDRAWAL-SECURITY-EXTENSION-01 — sin cambios
respecto a la decisión de producto original: el dinero es del usuario, no
hay techo fijo).

WITHDRAWAL-SECURITY-EXTENSION-01 note: el flujo de retiro ahora es de dos
pasos. Los gates (email/términos/KYC/2FA) y el chequeo de "sin límite fijo"
se siguen aplicando en el paso 1 — "bloqueado" significa "no se creó
challenge"; "éxito" significa "se creó el challenge" (completar el paso de
OTP por email crea el WithdrawalRequest real y debita la wallet).

Garantías que deben seguir vigentes:
  - Email verificado
  - Términos aceptados
  - KYC aprobado
  - 2FA válido
  - Balance suficiente (available_balance >= amount)
  - Monto mínimo (MIN_WITHDRAWAL_USD)
  - Rate limit (anti-spam)
  - Pending guard (un solo challenge/WR pendiente a la vez)
  - WithdrawalRequest pendiente creado + admin review

Escenarios cubiertos:
  1.  Usuario con $5 000 puede retirar $3 000 (antes bloqueado por límite $1 500).
  2.  Usuario puede retirar su balance completo si lo desea (monto manual, no Withdraw All).
  3.  Usuario NO puede retirar más que su available_balance.
  4.  Sin KYC → bloqueado (gate vigente).
  5.  Sin email verificado → bloqueado (gate vigente).
  6.  Sin términos aceptados → bloqueado (gate vigente).
  7.  Sin 2FA → bloqueado (gate vigente).
  8.  2FA incorrecto → bloqueado (gate vigente).
  9.  Por debajo del mínimo → bloqueado.
  10. daily_used presente en contexto (display informativo).
  11. daily_limit ausente del contexto (gate eliminado).
  12. Retiro exitoso (flujo completo) crea WithdrawalRequest PENDING y debita wallet.
  13. Aviso de revisión manual aparece en la página.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.models import WithdrawalEmailOTPChallenge, WithdrawalRequest, TOTPDevice
from simulator.tests.factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from simulator.tests.withdrawal_flow_helpers import PATCH_EMAIL as _PATCH_OTP_EMAIL, full_withdraw_flow

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


def _wr_payload(amount="1500.00", otp="000000", wallet_pk=""):
    return {
        "amount_usd":      amount,
        "crypto_currency": "usdttrc20",
        "wallet_address":  str(wallet_pk),
        "otp_code":        otp,
    }


class WithdrawNoMaxLimitTests(TestCase):
    """
    Full-gate user (email ✓, terms ✓, KYC ✓, 2FA ✓) can withdraw any
    amount up to their available_balance without hitting a fixed cap.
    """

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="nolimit@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    # ── Monto superior al antiguo límite de $1 500 ────────────────────────────

    def test_user_with_5000_can_request_3000(self):
        """$3 000 request from a $5 000 wallet must succeed at step 1 (was blocked by $1 500 cap)."""
        r = self.client.post(WITHDRAW_URL, _wr_payload("3000.00", wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 302)  # redirect to /withdraw/otp/
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)

    def test_withdrawal_3000_end_to_end_creates_pending_wr(self):
        """A $3 000 withdrawal (full flow) creates exactly one PENDING WithdrawalRequest."""
        with _PATCH_OTP_EMAIL:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("3000.00"))
        self.assertEqual(
            WithdrawalRequest.objects.filter(
                user=self.user, status=WithdrawalRequest.STATUS_PENDING, amount_usd=Decimal("3000.00"),
            ).count(),
            1,
        )

    def test_withdrawal_3000_debits_wallet(self):
        """Wallet is debited by $3 000 on a successful large withdrawal (full flow)."""
        with _PATCH_OTP_EMAIL:
            full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("3000.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("2000"))

    def test_user_can_request_full_balance_manually(self):
        """User can manually type their entire available_balance as amount_usd."""
        with _PATCH_OTP_EMAIL:
            r1, r2, challenge = full_withdraw_flow(self.client, self.user, verified_wallet=self.vw, amount_usd=Decimal("5000.00"))
        self.assertRedirects(r2, "/withdraw/history/", fetch_redirect_response=False)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("0"))

    # ── Balance insuficiente sigue bloqueando ─────────────────────────────────

    def test_cannot_request_more_than_available_balance(self):
        """Amount > available_balance is still rejected at step 1 (insufficient funds gate)."""
        r = self.client.post(WITHDRAW_URL, _wr_payload("5001.00", wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    def test_insufficient_balance_does_not_debit_wallet(self):
        self.client.post(WITHDRAW_URL, _wr_payload("5001.00", wallet_pk=self.vw.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("5000"))

    # ── Contexto del template ─────────────────────────────────────────────────

    def test_daily_limit_absent_from_context(self):
        """daily_limit was removed from context — gate no longer exists."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertNotIn("daily_limit", resp.context)

    def test_daily_used_present_in_context(self):
        """daily_used is still shown for informational purposes."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertIn("daily_used", resp.context)

    def test_page_shows_manual_review_notice(self):
        """The security notice about manual review is rendered in the page."""
        resp = self.client.get(WITHDRAW_URL)
        self.assertContains(resp, "revisión manual por seguridad")


# ── Gates de seguridad siguen vigentes ───────────────────────────────────────

class WithdrawSecurityGatesStillActiveTests(TestCase):
    """
    All compliance gates (email, terms, KYC, 2FA) must still block
    regardless of the removal of the daily cap.
    """

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL.start()

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_EMAIL.stop()

    def _post(self, user, amount="3000.00"):
        self.client.force_login(user)
        vw = make_verified_withdrawal_wallet(user)
        with patch("simulator.two_factor.verify_totp_code", return_value=True):
            return self.client.post(WITHDRAW_URL, _wr_payload(amount, wallet_pk=vw.pk))

    def test_no_kyc_blocks_withdrawal(self):
        """User without KYC approval is blocked even for large amounts."""
        user = make_user(email="nokyc@test.com")
        make_wallet(user, initial_balance=Decimal("5000"))
        _make_device(user)
        # No KYC created
        r = self._post(user, "3000.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=user).count(), 0)

    def test_no_email_verified_blocks_withdrawal(self):
        """User without verified email is blocked."""
        user = make_user(email="noemail@test.com", email_verified=False)
        make_wallet(user, initial_balance=Decimal("5000"))
        _make_device(user)
        make_kyc_approved(user)
        r = self._post(user, "3000.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=user).count(), 0)

    def test_no_terms_accepted_blocks_withdrawal(self):
        """User without accepted terms is blocked."""
        user = make_user(email="noterms@test.com", terms_accepted=False)
        make_wallet(user, initial_balance=Decimal("5000"))
        _make_device(user)
        make_kyc_approved(user)
        r = self._post(user, "3000.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=user).count(), 0)

    def test_no_2fa_device_blocks_withdrawal(self):
        """User without a confirmed TOTP device is blocked."""
        user = make_user(email="no2fa@test.com")
        make_wallet(user, initial_balance=Decimal("5000"))
        make_kyc_approved(user)
        # No device created
        r = self._post(user, "3000.00")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=user).count(), 0)

    def test_wrong_2fa_code_blocks_withdrawal(self):
        """Invalid 2FA code is rejected."""
        user = make_user(email="bad2fa@test.com")
        make_wallet(user, initial_balance=Decimal("5000"))
        _make_device(user)
        make_kyc_approved(user)
        vw = make_verified_withdrawal_wallet(user)
        self.client.force_login(user)
        with patch("simulator.two_factor.verify_totp_code", return_value=False):
            r = self.client.post(WITHDRAW_URL, _wr_payload("3000.00", wallet_pk=vw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=user).count(), 0)


# ── Mínimo de retiro sigue vigente ───────────────────────────────────────────

class WithdrawMinimumStillEnforcedTests(TestCase):

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_TOTP.start()
        _PATCH_EMAIL.start()
        self.user   = make_user(email="mintest@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        _make_device(self.user)
        make_kyc_approved(self.user)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        _PATCH_RATELIMIT.stop()
        _PATCH_TOTP.stop()
        _PATCH_EMAIL.stop()

    @override_settings(MIN_WITHDRAWAL_USD=25)
    def test_below_minimum_is_blocked(self):
        """Amount below MIN_WITHDRAWAL_USD is rejected regardless of balance."""
        r = self.client.post(WITHDRAW_URL, _wr_payload("10.00", wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 0)

    @override_settings(MIN_WITHDRAWAL_USD=25)
    def test_at_minimum_succeeds(self):
        """Amount exactly equal to MIN_WITHDRAWAL_USD is accepted at step 1."""
        r = self.client.post(WITHDRAW_URL, _wr_payload("25.00", wallet_pk=self.vw.pk))
        self.assertEqual(r.status_code, 302)
