"""
simulator/tests/test_deposit_credit_amount_fix.py

FIX-NOWPAYMENTS-DEPOSIT-CREDIT-AMOUNT-01 — regression tests.

Root cause: deposit_callback() looked up a field name that does not exist
in real NowPayments payloads ("actually_paid_amount" — the real field is
"actually_paid"), silently falling back to "outcome_amount". outcome_amount
is NowPayments' internal settlement leg and can be denominated in a
completely unrelated currency (e.g. BTC, for a "crypto2crypto" payment)
even when the deposit itself was paid in USDT. Deposit #45 (real production
money, payment_id 6129376005) was credited $0.00 instead of $20.00 because
of this — 0.00020823 (BTC) was treated as USD and rounded to zero at the
model's 2-decimal precision.

Fix: credit_amount is now always deposit.amount_usd — the fixed USD
obligation Money Broker sent to NowPayments as price_amount/price_currency
=usd at deposit-creation time. actually_paid, actually_paid_amount,
outcome_amount, outcome_currency and actually_paid_at_fiat are NEVER used
as a USD credit source, regardless of pay_currency or payment type.

These tests build IPN bodies shaped like the REAL NowPayments payload
(field name "actually_paid", not "actually_paid_amount") to prove the fix
against the actual contract, not just the old mocked-test shape.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from simulator.models import Deposit, WalletTransaction
from simulator.nowpayments import verify_ipn_signature

from .factories import make_deposit, make_user, make_wallet
from .test_nowpayments_secret import _sign, _IPN_SECRET

CALLBACK_URL = "/deposit/callback/"


def _real_ipn_body(payment_id: str, payment_status: str, order_id: str,
                    price_amount: float, pay_currency: str,
                    actually_paid=None, outcome_amount=None,
                    outcome_currency=None, ptype=None) -> str:
    """
    Build an IPN body using the REAL NowPayments field name ("actually_paid",
    never "actually_paid_amount"), mirroring the real Deposit #45 payload
    shape. Fields are omitted when None, matching how NowPayments only sends
    outcome_amount/outcome_currency for crypto2crypto-type payments.
    """
    body = {
        "payment_id":     payment_id,
        "payment_status": payment_status,
        "order_id":       order_id,
        "price_amount":   price_amount,
        "price_currency": "usd",
        "pay_currency":   pay_currency,
    }
    if actually_paid is not None:
        body["actually_paid"] = actually_paid
    if outcome_amount is not None:
        body["outcome_amount"] = outcome_amount
    if outcome_currency is not None:
        body["outcome_currency"] = outcome_currency
    if ptype is not None:
        body["type"] = ptype
    return json.dumps(body)


# ─────────────────────────────────────────────
# 1/2. Exact payment — USDT and BTC — credits deposit.amount_usd
# ─────────────────────────────────────────────

class ExactPaymentCreditsAmountUsdTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_usdt_exact_payment_credits_amount_usd(self, _mock_sig):
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("20.00"),
                               crypto_currency="usdttrc20", payment_id="pay_usdt_exact")

        body = _real_ipn_body("pay_usdt_exact", "finished", str(deposit.pk),
                              price_amount=20.0, pay_currency="usdttrc20",
                              actually_paid=20)
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_btc_exact_payment_credits_amount_usd(self, _mock_sig):
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("50.00"),
                               crypto_currency="btc", payment_id="pay_btc_exact")

        # actually_paid here is in BTC units (tiny number) — must NOT end up
        # in Wallet.available_balance.
        body = _real_ipn_body("pay_btc_exact", "finished", str(deposit.pk),
                              price_amount=50.0, pay_currency="btc",
                              actually_paid=0.0008405200)
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))


# ─────────────────────────────────────────────
# 3. outcome_amount (crypto2crypto settlement leg) never used as USD
#    — this is the exact Deposit #45 regression.
# ─────────────────────────────────────────────

class OutcomeAmountNeverUsedAsUsdTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_outcome_amount_btc_truthy_never_used_as_usd(self, _mock_sig):
        """
        Exact real Deposit #45 shape: finished, actually_paid=20 (USDT),
        outcome_amount=0.00020823 (BTC, crypto2crypto settlement leg).
        Wallet must be credited $20.00 (deposit.amount_usd), never
        0.00020823 and never $0.00.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("20.00"),
                               crypto_currency="usdttrc20", payment_id="pay_45_repro")

        body = _real_ipn_body(
            "pay_45_repro", "finished", str(deposit.pk),
            price_amount=20.0, pay_currency="usdttrc20",
            actually_paid=20, outcome_amount=0.00020823,
            outcome_currency="btc", ptype="crypto2crypto",
        )
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"))
        self.assertNotEqual(wallet.available_balance, Decimal("0.00"))

        tx = WalletTransaction.objects.get(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT)
        self.assertEqual(tx.amount, Decimal("20.00"))

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_confirmed_amount_usd_field_not_populated_from_outcome_amount(self, _mock_sig):
        """
        confirmed_amount_usd must stay untouched (None) — the field is kept
        (no migration), it is simply no longer fed from a crypto-denominated
        field. Nothing else in the codebase reads it.
        """
        user    = make_user()
        make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("20.00"),
                               crypto_currency="usdttrc20", payment_id="pay_45_repro_2")

        body = _real_ipn_body(
            "pay_45_repro_2", "finished", str(deposit.pk),
            price_amount=20.0, pay_currency="usdttrc20",
            actually_paid=20, outcome_amount=0.00020823,
            outcome_currency="btc", ptype="crypto2crypto",
        )
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        deposit.refresh_from_db()
        self.assertIsNone(deposit.confirmed_amount_usd)


# ─────────────────────────────────────────────
# 4. actually_paid (crypto units) never used directly as USD
# ─────────────────────────────────────────────

class ActuallyPaidNeverUsedDirectlyAsUsdTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_actually_paid_crypto_never_used_directly_as_usd(self, _mock_sig):
        """
        Even with NO outcome_amount at all (plain, non-crypto2crypto BTC
        deposit), actually_paid is still crypto units and must not leak
        into Wallet.available_balance as if it were USD.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("50.00"),
                               crypto_currency="btc", payment_id="pay_ap_btc")

        body = _real_ipn_body("pay_ap_btc", "finished", str(deposit.pk),
                              price_amount=50.0, pay_currency="btc",
                              actually_paid=0.0008405200)
        self.client.post(CALLBACK_URL, body, content_type="application/json")

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))
        self.assertNotEqual(wallet.available_balance, Decimal("0.00"))


# ─────────────────────────────────────────────
# 5. partially_paid never credits, never mislabels crypto as USD
# ─────────────────────────────────────────────

class PartiallyPaidDoesNotCreditTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_partially_paid_does_not_credit(self, _mock_sig):
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               crypto_currency="usdttrc20", payment_id="pay_partial_001")

        body = _real_ipn_body("pay_partial_001", "partially_paid", str(deposit.pk),
                              price_amount=100.0, pay_currency="usdttrc20",
                              actually_paid=80)
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)
        self.assertIsNone(deposit.credited_at)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)


# ─────────────────────────────────────────────
# 6. Overpayment (finished) credits only deposit.amount_usd
# ─────────────────────────────────────────────

class OverpaymentCreditsOnlyAmountUsdTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_overpayment_finished_credits_only_amount_usd(self, _mock_sig):
        """
        Provider marks finished even though actually_paid (120) exceeds the
        requested amount (100) — Wallet must receive only the invoiced
        $100.00, never the surplus.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("100.00"),
                               crypto_currency="usdttrc20", payment_id="pay_over_001")

        body = _real_ipn_body("pay_over_001", "finished", str(deposit.pk),
                              price_amount=100.0, pay_currency="usdttrc20",
                              actually_paid=120)
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

        tx = WalletTransaction.objects.get(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT)
        self.assertEqual(tx.amount, Decimal("100.00"))


# ─────────────────────────────────────────────
# 7/8. Duplicate finished callback still idempotent; credited=True only
#      after a successful credit.
# ─────────────────────────────────────────────

class DuplicateCallbackAndCreditedFlagTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_finished_callback_does_not_duplicate_real_shape(self, _mock_sig):
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("20.00"),
                               crypto_currency="usdttrc20", payment_id="pay_dup_real")

        body = _real_ipn_body("pay_dup_real", "finished", str(deposit.pk),
                              price_amount=20.0, pay_currency="usdttrc20",
                              actually_paid=20, outcome_amount=0.00020823,
                              outcome_currency="btc", ptype="crypto2crypto")

        resp1 = self.client.post(CALLBACK_URL, body, content_type="application/json")
        resp2 = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json().get("idempotent"))

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT).count(),
            1,
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_credited_true_only_after_successful_credit(self, _mock_sig):
        user    = make_user()
        make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("30.00"),
                               crypto_currency="usdttrc20", payment_id="pay_waiting_001",
                               status="pending")

        # Non-credited status first — must not flip credited.
        body_waiting = _real_ipn_body("pay_waiting_001", "waiting", str(deposit.pk),
                                       price_amount=30.0, pay_currency="usdttrc20")
        self.client.post(CALLBACK_URL, body_waiting, content_type="application/json")
        deposit.refresh_from_db()
        self.assertFalse(deposit.credited)

        # Now finished — must flip credited exactly once.
        body_finished = _real_ipn_body("pay_waiting_001", "finished", str(deposit.pk),
                                        price_amount=30.0, pay_currency="usdttrc20",
                                        actually_paid=30)
        self.client.post(CALLBACK_URL, body_finished, content_type="application/json")
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)
        self.assertIsNotNone(deposit.credited_at)


# ─────────────────────────────────────────────
# 9. WalletTransaction.amount == deposit.amount_usd across a currency matrix
# ─────────────────────────────────────────────

class WalletTransactionAmountMatchesAmountUsdTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_wallet_transaction_amount_equals_deposit_amount_usd_matrix(self, _mock_sig):
        cases = [
            ("usdttrc20", Decimal("20.00"), 20, None, None),
            ("btc",       Decimal("50.00"), 0.00084052, None, None),
            ("usdttrc20", Decimal("20.00"), 20, 0.00020823, "btc"),  # crypto2crypto
        ]
        for i, (currency, amount_usd, actually_paid, outcome_amount, outcome_currency) in enumerate(cases):
            with self.subTest(currency=currency, amount_usd=amount_usd):
                user    = make_user()
                wallet  = make_wallet(user=user)
                pid     = f"pay_matrix_{i}"
                deposit = make_deposit(user=user, amount_usd=amount_usd,
                                       crypto_currency=currency, payment_id=pid)

                body = _real_ipn_body(pid, "finished", str(deposit.pk),
                                      price_amount=float(amount_usd), pay_currency=currency,
                                      actually_paid=actually_paid,
                                      outcome_amount=outcome_amount,
                                      outcome_currency=outcome_currency,
                                      ptype="crypto2crypto" if outcome_amount else None)
                self.client.post(CALLBACK_URL, body, content_type="application/json")

                tx = WalletTransaction.objects.get(wallet=wallet, tx_type=WalletTransaction.TX_DEPOSIT)
                self.assertEqual(tx.amount, amount_usd)


# ─────────────────────────────────────────────
# 10. Historical-deposit-shape regression — payloads with NEITHER
#     actually_paid_amount NOR outcome_amount (the #34/#35 shape) must
#     keep crediting deposit.amount_usd, same as before the fix.
# ─────────────────────────────────────────────

class HistoricalDepositShapeRegressionTests(TestCase):

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_historical_style_payload_still_credits_full_amount(self, _mock_sig):
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("50.00"),
                               crypto_currency="btc", payment_id="pay_hist_style")

        body = json.dumps({
            "payment_id":     "pay_hist_style",
            "payment_status": "finished",
            "order_id":       str(deposit.pk),
            "price_amount":   50.0,
            "price_currency": "usd",
            "pay_currency":   "btc",
            # deliberately no actually_paid / actually_paid_amount / outcome_amount —
            # matches the real #34/#35 callback shape.
        })
        resp = self.client.post(CALLBACK_URL, body, content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))


# ─────────────────────────────────────────────
# 11. End-to-end with the REAL raw-body HMAC signature path
#     (FIX-NOWPAYMENTS-IPN-RAW-BODY-01 integration) — no signature mock.
# ─────────────────────────────────────────────

class RawBodySignatureIntegrationTests(TestCase):

    def setUp(self):
        import os
        self._env_patch = patch.dict(os.environ, {"NOWPAYMENTS_IPN_SECRET": _IPN_SECRET})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_real_signature_plus_credit_amount_fix_work_together(self):
        """
        Signs the exact Deposit #45-shaped body with the real raw-body HMAC
        contract (no mock), posts it to the live endpoint, and confirms both
        fixes hold together: signature verifies AND the credited amount is
        deposit.amount_usd, not outcome_amount.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(user=user, amount_usd=Decimal("20.00"),
                               crypto_currency="usdttrc20", payment_id="pay_e2e_sig")

        body_bytes = _real_ipn_body(
            "pay_e2e_sig", "finished", str(deposit.pk),
            price_amount=20.0, pay_currency="usdttrc20",
            actually_paid=20, outcome_amount=0.00020823,
            outcome_currency="btc", ptype="crypto2crypto",
        ).encode("utf-8")
        sig = _sign(body_bytes)

        # Sanity: the signature this test computes really does verify against
        # the production verify_ipn_signature() contract before we rely on it.
        self.assertTrue(verify_ipn_signature(body_bytes, sig))

        resp = self.client.post(
            CALLBACK_URL, body_bytes, content_type="application/json",
            HTTP_X_NOWPAYMENTS_SIG=sig,
        )

        self.assertEqual(resp.status_code, 200)
        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("20.00"))
