# simulator/tests/test_withdrawal_e2e_01b_callback_and_error_detail.py
"""
WITHDRAWAL-E2E-01B FASE B — two independent, narrowly-scoped fixes:

1. Deposit and withdrawal/payout NowPayments IPN callback URLs are now
   resolved via SEPARATE env-var overrides (NOWPAYMENTS_CALLBACK_URL vs
   NOWPAYMENTS_PAYOUT_CALLBACK_URL) — see _resolve_callback_url()'s own
   docstring for the WithdrawalRequest #13 root cause this closes: a
   single override intended for deposit testing was silently replacing
   the correctly-generated withdrawal callback URL on every payout.

2. create_payout_with_token() now preserves the provider's own `code`/
   `message` fields (and ONLY those two) in the exception message on a
   non-2xx response, so PayoutAttempt.last_error carries something more
   useful than a bare "403 Client Error: Forbidden" — never the full
   body, headers, JWT, api-key, or payload.

No HTTP — simulator.nowpayments's own requests calls are mocked, same
pattern as test_fix02a2_nowpayments_refactor.py.
"""
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from simulator import nowpayments as np


def _unset(*names):
    """Context-manager-free helper: patch.dict already handles cleanup,
    this just documents intent at call sites below."""
    return patch.dict(os.environ, {n: "" for n in names})


class ResolveCallbackUrlDefaultIsBackwardCompatibleTests(SimpleTestCase):
    """Deposits must see ZERO behavior change — same env var name, same
    fallback logic, as before this block existed."""

    def test_default_override_env_var_is_still_NOWPAYMENTS_CALLBACK_URL(self):
        with patch.dict(os.environ, {"NOWPAYMENTS_CALLBACK_URL": "https://tunnel.example/deposit/callback/"}):
            result = np._resolve_callback_url("https://public.example/deposit/callback/")
        self.assertEqual(result, "https://tunnel.example/deposit/callback/")

    def test_no_override_set_falls_back_to_generated_public_url(self):
        with _unset("NOWPAYMENTS_CALLBACK_URL"):
            result = np._resolve_callback_url("https://public.example/deposit/callback/")
        self.assertEqual(result, "https://public.example/deposit/callback/")

    def test_localhost_generated_url_with_no_override_returns_none(self):
        with _unset("NOWPAYMENTS_CALLBACK_URL"):
            result = np._resolve_callback_url("http://127.0.0.1:8000/deposit/callback/")
        self.assertIsNone(result)


class ResolveCallbackUrlPerCallerOverrideTests(SimpleTestCase):
    """The actual bug fix: deposit and payout overrides are independent
    — setting one must never affect the other."""

    def test_custom_override_env_var_is_used_instead_of_default(self):
        with _unset("NOWPAYMENTS_CALLBACK_URL"), \
             patch.dict(os.environ, {"NOWPAYMENTS_PAYOUT_CALLBACK_URL": "https://tunnel.example/withdraw/callback/"}):
            result = np._resolve_callback_url(
                "https://public.example/withdraw/callback/",
                override_env_var="NOWPAYMENTS_PAYOUT_CALLBACK_URL",
            )
        self.assertEqual(result, "https://tunnel.example/withdraw/callback/")

    def test_deposit_override_set_does_not_leak_into_payout_resolution(self):
        """This is exactly WithdrawalRequest #13's bug, reproduced and
        proven fixed: NOWPAYMENTS_CALLBACK_URL set to a deposit tunnel
        must NOT be picked up when resolving with the payout override
        var — the payout call must fall through to its own generated
        URL instead."""
        with patch.dict(os.environ, {"NOWPAYMENTS_CALLBACK_URL": "https://tunnel.example/deposit/callback/"}), \
             _unset("NOWPAYMENTS_PAYOUT_CALLBACK_URL"):
            deposit_result = np._resolve_callback_url("https://public.example/deposit/callback/")
            payout_result = np._resolve_callback_url(
                "https://public.example/withdraw/callback/",
                override_env_var="NOWPAYMENTS_PAYOUT_CALLBACK_URL",
            )
        self.assertEqual(deposit_result, "https://tunnel.example/deposit/callback/")
        self.assertEqual(payout_result, "https://public.example/withdraw/callback/")
        self.assertNotEqual(payout_result, deposit_result)

    def test_payout_override_set_does_not_leak_into_deposit_resolution(self):
        """The reverse direction — a payout-specific tunnel must never
        override deposit's callback either."""
        with _unset("NOWPAYMENTS_CALLBACK_URL"), \
             patch.dict(os.environ, {"NOWPAYMENTS_PAYOUT_CALLBACK_URL": "https://tunnel.example/withdraw/callback/"}):
            deposit_result = np._resolve_callback_url("https://public.example/deposit/callback/")
        self.assertEqual(deposit_result, "https://public.example/deposit/callback/")


class CreatePayoutWithTokenUsesPayoutSpecificOverrideTests(SimpleTestCase):
    """End-to-end (mocked HTTP) confirmation that create_payout_with_token
    actually calls _resolve_callback_url with the payout override var —
    not just that the helper supports it in isolation."""

    def test_payout_ipn_callback_url_uses_payout_override_not_deposit_one(self):
        response = {"id": "batch-1", "status": "CREATED", "withdrawals": [{"id": "wd-1"}]}
        with patch.dict(os.environ, {
                "NOWPAYMENTS_CALLBACK_URL": "https://tunnel.example/deposit/callback/",
                "NOWPAYMENTS_PAYOUT_CALLBACK_URL": "https://tunnel.example/withdraw/callback/",
             }), \
             patch("simulator.nowpayments.requests.post") as post_mock:
            post_mock.return_value.ok = True
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = response
            post_mock.return_value.raise_for_status.return_value = None
            np.create_payout_with_token(
                "bc1qtest", "btc", Decimal("0.001"), 13, "https://public.example/withdraw/callback/", "tok",
            )
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["json"]["ipn_callback_url"], "https://tunnel.example/withdraw/callback/")
        self.assertNotEqual(kwargs["json"]["ipn_callback_url"], "https://tunnel.example/deposit/callback/")

    def test_no_payout_override_set_uses_generated_public_withdraw_url(self):
        """With no NOWPAYMENTS_PAYOUT_CALLBACK_URL configured (today's
        real .env state), a public-host callback_url passed in by the
        caller (admin.py's request.build_absolute_uri(...)) is used
        as-is — the fix requires zero .env changes to work correctly
        in production."""
        response = {"id": "batch-2", "status": "CREATED", "withdrawals": [{"id": "wd-2"}]}
        with _unset("NOWPAYMENTS_CALLBACK_URL", "NOWPAYMENTS_PAYOUT_CALLBACK_URL"), \
             patch("simulator.nowpayments.requests.post") as post_mock:
            post_mock.return_value.ok = True
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = response
            post_mock.return_value.raise_for_status.return_value = None
            np.create_payout_with_token(
                "bc1qtest", "btc", Decimal("0.001"), 13,
                "https://real-production-host.example/withdraw/callback/", "tok",
            )
        _, kwargs = post_mock.call_args
        self.assertEqual(
            kwargs["json"]["ipn_callback_url"],
            "https://real-production-host.example/withdraw/callback/",
        )


class CreatePayoutWithTokenErrorDetailPropagationTests(SimpleTestCase):
    """The second fix: provider code/message survive into the raised
    exception's message — narrowly, safely."""

    def _mock_error_response(self, status_code, body):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = status_code
        resp.url = "https://api.nowpayments.io/v1/payout"
        resp.text = str(body)
        resp.json.return_value = body
        return resp

    def test_provider_code_and_message_appear_in_raised_exception(self):
        """Reproduces WithdrawalRequest #13's exact real body."""
        body = {
            "status": False, "statusCode": 403,
            "code": "INVALID_AUTH_TOKEN",
            "message": "Your API-key is taken from another account",
        }
        with patch("simulator.nowpayments.requests.post", return_value=self._mock_error_response(403, body)):
            with self.assertRaises(Exception) as ctx:
                np.create_payout_with_token(
                    "bc1qtest", "usdttrc20", Decimal("19.6"), 13, "https://cb", "tok",
                )
        msg = str(ctx.exception)
        self.assertIn("INVALID_AUTH_TOKEN", msg)
        self.assertIn("Your API-key is taken from another account", msg)
        self.assertIn("403", msg)

    def test_unparseable_body_does_not_crash_and_still_raises(self):
        """A non-JSON error body must never itself become a new failure
        mode — extraction is best-effort, the HTTPError still raises."""
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 500
        resp.url = "https://api.nowpayments.io/v1/payout"
        resp.text = "<html>Internal Server Error</html>"
        resp.json.side_effect = ValueError("not JSON")
        with patch("simulator.nowpayments.requests.post", return_value=resp):
            with self.assertRaises(Exception) as ctx:
                np.create_payout_with_token(
                    "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "tok",
                )
        self.assertIn("500", str(ctx.exception))

    def test_body_with_no_code_or_message_fields_raises_generic_message(self):
        body = {"status": False, "statusCode": 404}
        with patch("simulator.nowpayments.requests.post", return_value=self._mock_error_response(404, body)):
            with self.assertRaises(Exception) as ctx:
                np.create_payout_with_token(
                    "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "tok",
                )
        msg = str(ctx.exception)
        self.assertIn("404", msg)
        self.assertNotIn("provider_code", msg)

    def test_error_message_never_contains_the_jwt_or_api_key(self):
        """Explicit negative assertion — the allowlist is code/message
        ONLY. Even if a future NowPayments error body somehow echoed
        back request data, it must not appear here because we never
        read anything but those two keys."""
        body = {
            "status": False, "statusCode": 403,
            "code": "INVALID_AUTH_TOKEN",
            "message": "denied",
            "echoed_authorization_header": "Bearer super-secret-jwt-value",
            "echoed_api_key": "super-secret-api-key-value",
        }
        with patch("simulator.nowpayments.requests.post", return_value=self._mock_error_response(403, body)):
            with self.assertRaises(Exception) as ctx:
                np.create_payout_with_token(
                    "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "the-real-jwt-token-value",
                )
        msg = str(ctx.exception)
        self.assertNotIn("super-secret-jwt-value", msg)
        self.assertNotIn("super-secret-api-key-value", msg)
        self.assertNotIn("the-real-jwt-token-value", msg)

    def test_success_path_unaffected_by_the_new_error_branch(self):
        response = {"id": "batch-9", "status": "CREATED", "withdrawals": [{"id": "wd-9"}]}
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = response
        resp.raise_for_status.return_value = None
        resp.text = str(response)
        with patch("simulator.nowpayments.requests.post", return_value=resp):
            result = np.create_payout_with_token(
                "bc1qtest", "btc", Decimal("0.001"), 1, "https://cb", "tok",
            )
        self.assertEqual(result, response)


class PayoutProviderAdapterStillClassifiesTheNewExceptionTests(SimpleTestCase):
    """payout_providers.py's create_payout() must keep classifying this
    (now richer-message) HTTPError exactly as before — ProviderUnavailableError,
    with the status code still readable from exc.response.status_code —
    without any change to payout_providers.py itself."""

    def test_403_still_classified_as_provider_unavailable_error_with_detail_in_message(self):
        from simulator.payout_providers import NowPaymentsAdapter, ProviderUnavailableError
        from simulator.models import PayoutAttempt

        attempt = MagicMock(spec=PayoutAttempt)
        attempt.destination_address = "TUFt1P...DEQVrB"
        attempt.requested_asset = "usdttrc20"
        attempt.provider_amount = Decimal("19.6")
        attempt.withdrawal_request_id = 13

        body = {
            "status": False, "statusCode": 403,
            "code": "INVALID_AUTH_TOKEN",
            "message": "Your API-key is taken from another account",
        }
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 403
        resp.url = "https://api.nowpayments.io/v1/payout"
        resp.text = str(body)
        resp.json.return_value = body

        adapter = NowPaymentsAdapter()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            with self.assertRaises(ProviderUnavailableError) as ctx:
                adapter.create_payout(attempt, callback_url="https://cb")
        self.assertIn("INVALID_AUTH_TOKEN", str(ctx.exception))
