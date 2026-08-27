# simulator/tests/test_fix02a2_nowpayments_refactor.py
"""
FIX-02A.2 — JWT-blocker fix: nowpayments.py structural refactor tests.

Confirms the extraction of create_payout_with_token() out of
create_payout() preserves 100% backward compatibility for existing
callers (funded_payouts.py::approve_internal_payout is the only one —
see the audit) while giving NowPaymentsAdapter a way to hold a single
real auth call separate from the payout POST.

No HTTP — simulator.nowpayments's own _get_jwt_token()/requests calls
are mocked, same pattern as the rest of this suite.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase

from simulator import nowpayments as np


class CreatePayoutWithTokenNeverFetchesItsOwnTokenTests(SimpleTestCase):
    def test_create_payout_with_token_never_calls_get_jwt_token(self):
        """The whole point of the extraction — this function must be
        pure with respect to auth: given a token, it uses exactly that
        one, never fetching a second, independent one of its own."""
        response = {"id": "batch-1", "status": "CREATED", "withdrawals": [{"id": "wd-1"}]}
        with patch("simulator.nowpayments._get_jwt_token") as auth_mock, \
             patch("simulator.nowpayments.requests.post") as post_mock:
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = response
            post_mock.return_value.raise_for_status.return_value = None
            np.create_payout_with_token(
                "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "explicit-token",
            )
        auth_mock.assert_not_called()

    def test_create_payout_with_token_uses_the_token_it_was_given(self):
        response = {"id": "batch-1", "status": "CREATED", "withdrawals": [{"id": "wd-1"}]}
        with patch("simulator.nowpayments.requests.post") as post_mock:
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = response
            post_mock.return_value.raise_for_status.return_value = None
            np.create_payout_with_token(
                "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "my-explicit-token",
            )
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer my-explicit-token")


class CreatePayoutBackCompatWrapperTests(SimpleTestCase):
    """create_payout() — the pre-existing public function funded_payouts.py
    calls — must remain a drop-in equivalent of its pre-refactor self."""

    def test_create_payout_calls_get_jwt_token_then_create_payout_with_token(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="fetched-token") as auth_mock, \
             patch("simulator.nowpayments.create_payout_with_token", return_value={"ok": True}) as payout_mock:
            result = np.create_payout("bc1qtest", "btc", Decimal("0.001"), 7, "https://cb")

        auth_mock.assert_called_once()
        payout_mock.assert_called_once_with(
            "bc1qtest", "btc", Decimal("0.001"), 7, "https://cb", "fetched-token",
        )
        self.assertEqual(result, {"ok": True})

    def test_create_payout_signature_unchanged(self):
        """funded_payouts.py calls create_payout() positionally with
        exactly 5 args (address, currency, amount, withdrawal_id,
        callback_url) — must keep working with zero changes on its side."""
        import inspect
        sig = inspect.signature(np.create_payout)
        self.assertEqual(
            list(sig.parameters.keys()),
            ["address", "currency", "amount_crypto", "withdrawal_id", "callback_url"],
        )

    def test_create_payout_propagates_result_unchanged(self):
        response = {"id": "batch-9", "status": "CREATED", "withdrawals": [{"id": "wd-9"}]}
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post") as post_mock:
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = response
            post_mock.return_value.raise_for_status.return_value = None
            result = np.create_payout("bc1qtest", "btc", Decimal("0.001"), 3, "https://cb")
        self.assertEqual(result, response)

    def test_create_payout_propagates_auth_failure(self):
        """If the wrapper's own auth call fails, create_payout_with_token
        must never even be attempted — same pre-send-safe guarantee the
        adapter itself relies on, now also true for this legacy path."""
        import requests
        with patch("simulator.nowpayments._get_jwt_token", side_effect=requests.exceptions.Timeout("down")), \
             patch("simulator.nowpayments.create_payout_with_token") as payout_mock:
            with self.assertRaises(requests.exceptions.Timeout):
                np.create_payout("bc1qtest", "btc", Decimal("0.001"), 3, "https://cb")
        payout_mock.assert_not_called()
