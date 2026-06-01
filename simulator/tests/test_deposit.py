"""
simulator/tests/test_deposit.py — Bloque 2 (parte A)

Cubre: idempotencia del IPN callback de NowPayments (deposit_callback en views.py)

Convenciones:
  - Se mockea simulator.nowpayments.verify_ipn_signature en cada test.
  - El cuerpo del IPN es JSON real (mismo formato que NowPayments envía).
  - El test client de Django no hace CSRF porque la vista es @csrf_exempt.
  - El rate limiter es fail-open si Redis no responde — no bloquea los tests.
  - Todos los amounts son Decimal, nunca float.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from simulator.models import Deposit, WalletTransaction

from .factories import make_deposit, make_user, make_wallet

CALLBACK_URL = "/deposit/callback/"

# ── IPN body factory ─────────────────────────────────────────────────────────

def _ipn_body(payment_id: str, payment_status: str, order_id: str = "",
              actually_paid_amount: str = "100.00") -> str:
    """Return a realistic NowPayments IPN JSON string."""
    return json.dumps({
        "payment_id":            payment_id,
        "payment_status":        payment_status,
        "order_id":              order_id,
        "actually_paid_amount":  actually_paid_amount,
        "pay_currency":          "btc",
        "price_currency":        "usd",
        "price_amount":          float(actually_paid_amount),
    })


# ─────────────────────────────────────────────
# 1. Callback con status "finished" acredita wallet
# ─────────────────────────────────────────────

class TestDepositCallbackCredited(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_finished_status_credits_wallet(self, _mock_sig):
        """
        Un IPN con payment_status=finished debe:
          - Retornar HTTP 200
          - Poner Deposit.credited=True
          - Acreditar Wallet.available_balance con el monto del depósito
          - Escribir exactamente 1 WalletTransaction TX_DEPOSIT
          - Establecer credited_at
        """
        user    = make_user()
        wallet  = make_wallet(user=user)          # balance = 0
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               payment_id="pay_fin_001")

        body = _ipn_body("pay_fin_001", "finished", str(deposit.pk))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)

        # Deposit marcado como acreditado
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertIsNotNone(deposit.credited_at)

        # Wallet acreditado con el monto correcto
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

        # Exactamente 1 transacción de tipo DEPOSIT
        tx_count = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT
        ).count()
        self.assertEqual(tx_count, 1)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_confirmed_status_also_credits_wallet(self, _mock_sig):
        """payment_status=confirmed también está en CREDITED_STATUSES y debe acreditar."""
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("250.00"),
                               payment_id="pay_conf_001")

        body = _ipn_body("pay_conf_001", "confirmed", str(deposit.pk),
                         actually_paid_amount="250.00")
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)

        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("250.00"))


# ─────────────────────────────────────────────
# 2. Duplicate callback — idempotencia
# ─────────────────────────────────────────────

class TestDepositCallbackIdempotency(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_callback_no_double_credit(self, _mock_sig):
        """
        Dos llamadas idénticas al callback deben acreditar el wallet UNA SOLA VEZ.
        La segunda debe retornar {"ok": True, "idempotent": True}.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               payment_id="pay_dup_001")

        body = _ipn_body("pay_dup_001", "finished", str(deposit.pk))

        # Primera llamada — acredita
        resp1 = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(resp1.status_code, 200)

        # Segunda llamada idéntica — idempotente
        resp2 = self.client.post(CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json().get("idempotent"),
                        "Segunda llamada debería devolver idempotent=True")

        # Wallet acreditado exactamente una vez
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

        tx_count = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT
        ).count()
        self.assertEqual(tx_count, 1,
                         f"Esperado 1 WalletTransaction, encontrado {tx_count} — posible double-credit")


# ─────────────────────────────────────────────
# 3. Firma inválida rechazada
# ─────────────────────────────────────────────

class TestDepositCallbackSecurity(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=False)
    def test_invalid_signature_rejected(self, _mock_sig):
        """
        Una firma HMAC inválida debe retornar HTTP 400 sin modificar la DB.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               payment_id="pay_badsig_001")

        body = _ipn_body("pay_badsig_001", "finished", str(deposit.pk))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 400)

        # Deposit sin cambios
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        self.assertIsNone(deposit.credited_at)

        # Wallet sin cambios
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))

        # Ninguna transacción escrita
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet).count(), 0
        )


# ─────────────────────────────────────────────
# 4. Status no acreditable no toca wallet
# ─────────────────────────────────────────────

class TestDepositCallbackNonCreditedStatus(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_non_credited_status_no_credit(self, _mock_sig):
        """
        payment_status=waiting no está en CREDITED_STATUSES.
        El Deposit debe actualizarse (status cambia) pero el wallet NO se toca.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               payment_id="pay_wait_001", status="pending")

        body = _ipn_body("pay_wait_001", "waiting", str(deposit.pk))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)

        # Deposit actualizado pero NO acreditado
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        self.assertIsNone(deposit.credited_at)
        self.assertEqual(deposit.status, "waiting")

        # Wallet intacto
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_failed_status_no_credit(self, _mock_sig):
        """payment_status=failed — no acredita, marca status en Deposit."""
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, payment_id="pay_fail_001")

        body = _ipn_body("pay_fail_001", "failed", str(deposit.pk))
        response = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)

        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        self.assertEqual(deposit.status, "failed")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))
