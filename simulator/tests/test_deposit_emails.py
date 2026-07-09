# simulator/tests/test_deposit_emails.py
"""
Deposit confirmation email notifications.

Covers:
  1.  "finished" IPN queues one confirmation email to the deposit owner.
  2.  "confirmed" IPN queues one confirmation email.
  3.  Email subject contains "Money Broker".
  4.  Email body contains the deposit amount.
  5.  Email body contains the crypto currency.
  6.  Email body does not contain the NowPayments API key or IPN secret.
  7.  Email failure does not break the callback (still returns 200, credited=True).
  8.  Duplicate "finished" IPN (second webhook for already-credited deposit)
      does not queue a second email.
  9.  "waiting" IPN does not trigger an email.
  10. "failed"  IPN does not trigger an email.
  11. Helper send_deposit_confirmed_email queues to user.email.
  12. Helper email body contains "Confirmado".
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.models import Deposit
from simulator.deposit_emails import send_deposit_confirmed_email
from simulator.tests.factories import make_deposit, make_user, make_wallet

CALLBACK_URL = "/deposit/callback/"

_PATCH_SIG   = patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
_PATCH_EMAIL = patch("simulator.tasks.send_email_async.delay")


def _ipn(payment_id: str, status: str, order_id: str = "",
         amount: str = "100.00") -> str:
    return json.dumps({
        "payment_id":           payment_id,
        "payment_status":       status,
        "order_id":             order_id,
        "actually_paid_amount": amount,
        "pay_currency":         "btc",
        "price_currency":       "usd",
        "price_amount":         float(amount),
    })


# ── Callback integration tests ────────────────────────────────────────────────

class DepositConfirmedEmailTests(TestCase):
    def setUp(self):
        self.user    = make_user(email="depositor@test.com")
        self.wallet  = make_wallet(user=self.user)
        self.deposit = make_deposit(
            user=self.user,
            amount_usd=Decimal("150.00"),
            crypto_currency="btc",
            payment_id="pay_email_001",
        )

    def _post(self, status, payment_id=None, amount="150.00"):
        pid = payment_id or "pay_email_001"
        body = _ipn(pid, status, str(self.deposit.pk), amount)
        return self.client.post(CALLBACK_URL, body, content_type="application/json")

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_finished_queues_email(self, mock_email, _mock_sig):
        self._post("finished")
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(user_calls), 1)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_confirmed_queues_email(self, mock_email, _mock_sig):
        body = _ipn("pay_conf_x", "confirmed", str(self.deposit.pk))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(user_calls), 1)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_email_subject_contains_brand(self, mock_email, _mock_sig):
        self._post("finished")
        subj = mock_email.call_args.kwargs["subject"]
        self.assertIn("Money Broker", subj)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_email_body_contains_amount(self, mock_email, _mock_sig):
        self._post("finished")
        body = mock_email.call_args.kwargs["message"]
        self.assertIn("150", body)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_email_body_contains_currency(self, mock_email, _mock_sig):
        self._post("finished")
        body = mock_email.call_args.kwargs["message"]
        self.assertIn("BTC", body)

    @_PATCH_SIG
    @_PATCH_EMAIL
    @override_settings(
        NOWPAYMENTS_API_KEY="TEST_API_KEY_SECRET",
        NOWPAYMENTS_IPN_SECRET="TEST_IPN_SECRET_VALUE",
    )
    def test_email_body_has_no_api_key_or_secret(self, mock_email, _mock_sig):
        self._post("finished")
        body = mock_email.call_args.kwargs["message"]
        self.assertNotIn("TEST_API_KEY_SECRET", body)
        self.assertNotIn("TEST_IPN_SECRET_VALUE", body)

    @_PATCH_SIG
    def test_email_failure_does_not_break_callback(self, _mock_sig):
        with patch("simulator.tasks.send_email_async.delay",
                   side_effect=Exception("Celery down")):
            r = self._post("finished")
        self.assertEqual(r.status_code, 200)
        self.deposit.refresh_from_db()
        self.assertTrue(self.deposit.credited)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_duplicate_ipn_does_not_send_second_email(self, mock_email, _mock_sig):
        # First callback credits the deposit
        self._post("finished")
        first_count = mock_email.call_count

        # Second callback — deposit.credited is already True → idempotency gate
        self._post("finished")
        self.assertEqual(mock_email.call_count, first_count,
                         "Duplicate IPN must not queue a second email")

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_waiting_status_does_not_send_email(self, mock_email, _mock_sig):
        body = _ipn("pay_wait_x", "waiting", str(self.deposit.pk))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(user_calls), 0)

    @_PATCH_SIG
    @_PATCH_EMAIL
    def test_failed_status_does_not_send_email(self, mock_email, _mock_sig):
        body = _ipn("pay_fail_x", "failed", str(self.deposit.pk))
        self.client.post(CALLBACK_URL, body, content_type="application/json")
        user_calls = [
            c for c in mock_email.call_args_list
            if self.user.email in c.kwargs.get("recipient_list", [])
        ]
        self.assertEqual(len(user_calls), 0)


# ── Helper unit tests ─────────────────────────────────────────────────────────

class DepositEmailHelperTests(TestCase):
    def setUp(self):
        self.user    = make_user(email="helper_dep@test.com")
        self.deposit = make_deposit(
            user=self.user,
            amount_usd=Decimal("200.00"),
            crypto_currency="eth",
        )

    @_PATCH_EMAIL
    def test_helper_queues_to_user_email(self, mock_email):
        send_deposit_confirmed_email(self.deposit)
        self.assertEqual(mock_email.call_count, 1)
        self.assertIn(self.user.email, mock_email.call_args.kwargs["recipient_list"])

    @_PATCH_EMAIL
    def test_helper_subject_contains_brand(self, mock_email):
        send_deposit_confirmed_email(self.deposit)
        self.assertIn("Money Broker", mock_email.call_args.kwargs["subject"])

    @_PATCH_EMAIL
    def test_helper_body_contains_confirmado(self, mock_email):
        send_deposit_confirmed_email(self.deposit)
        body = mock_email.call_args.kwargs["message"]
        self.assertIn("Confirmado", body)

    @_PATCH_EMAIL
    def test_helper_body_contains_amount(self, mock_email):
        send_deposit_confirmed_email(self.deposit)
        body = mock_email.call_args.kwargs["message"]
        self.assertIn("200", body)

    @_PATCH_EMAIL
    def test_helper_body_contains_currency_upper(self, mock_email):
        send_deposit_confirmed_email(self.deposit)
        body = mock_email.call_args.kwargs["message"]
        self.assertIn("ETH", body)
