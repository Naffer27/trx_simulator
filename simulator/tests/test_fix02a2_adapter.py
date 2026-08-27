# simulator/tests/test_fix02a2_adapter.py
"""
FIX-02A.2 — NowPaymentsAdapter contract tests.

No real HTTP — simulator.nowpayments's functions are mocked at their
own module boundary (same pattern already used across this suite, e.g.
test_stale_price_audit.py). Verifies the adapter classifies errors by
WHICH call failed (Design Lock Correction #5), never by exception class
alone, and that raw_status never crosses into normalized_status without
going through the approved mapping.
"""
import json
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from simulator.payout_providers import (
    NowPaymentsAdapter, PayoutSubmissionResult, ProviderAuthError,
    ProviderResponseError, ProviderTimeoutError, ProviderUnavailableError,
)
from simulator.models import PayoutAttempt


def _fake_attempt(**overrides):
    """A lightweight stand-in — create_payout() only reads a handful of
    plain attributes off the attempt, no DB needed for these tests."""
    class _Attempt:
        pass
    a = _Attempt()
    a.destination_address = overrides.get("destination_address", "bc1qtest000000000000000000000000000000000")
    a.requested_asset = overrides.get("requested_asset", "btc")
    a.provider_amount = overrides.get("provider_amount", Decimal("0.001"))
    a.withdrawal_request_id = overrides.get("withdrawal_request_id", 1)
    a.pk = overrides.get("pk", 1)
    return a


class EstimateTests(SimpleTestCase):
    def test_estimate_success_returns_decimal(self):
        with patch("simulator.nowpayments.estimate_price", return_value=Decimal("0.00125")) as m:
            result = NowPaymentsAdapter().estimate(Decimal("100"), "btc")
        m.assert_called_once_with(Decimal("100"), "btc")
        self.assertEqual(result, Decimal("0.00125"))

    def test_estimate_failure_raises_normalized_error(self):
        with patch("simulator.nowpayments.estimate_price", side_effect=requests.exceptions.Timeout("slow")):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().estimate(Decimal("100"), "btc")


class CreatePayoutClassificationTests(SimpleTestCase):
    """The core of Design Lock Correction #5 — classification by call-site."""

    def test_auth_failure_never_attempts_payout_post(self):
        """/v1/auth fails -> ProviderAuthError, and the real payout POST
        (nowpayments.create_payout_with_token) must never be called."""
        with patch("simulator.nowpayments._get_jwt_token", side_effect=requests.exceptions.Timeout("auth down")), \
             patch("simulator.nowpayments.create_payout_with_token") as payout_mock:
            with self.assertRaises(ProviderAuthError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")
        payout_mock.assert_not_called()

    def test_exactly_one_auth_call_and_token_propagated(self):
        """FIX-02A.2 JWT-blocker fix — structural regression guard.
        _get_jwt_token() must be called exactly once on this path, and
        the SAME token it returns must be the one create_payout_with_token()
        receives (no second, independent auth round-trip that could fail
        on its own and be misclassified as ambiguous instead of
        ProviderAuthError — that bug is now structurally impossible
        because create_payout_with_token() never calls _get_jwt_token()
        at all, confirmed separately in test_fix02a2_nowpayments_refactor.py)."""
        np_response = {"id": "b", "status": "CREATED", "withdrawals": [{"id": "w", "status": "CREATED"}]}
        with patch("simulator.nowpayments._get_jwt_token", return_value="the-one-real-token") as auth_mock, \
             patch("simulator.nowpayments.create_payout_with_token", return_value=np_response) as payout_mock:
            NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="cb")

        auth_mock.assert_called_once()
        payout_mock.assert_called_once()
        _, kwargs_or_args = payout_mock.call_args, None
        received_token = payout_mock.call_args.args[-1]
        self.assertEqual(received_token, "the-one-real-token")

    def test_payout_post_timeout_is_ambiguous(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", side_effect=requests.exceptions.Timeout("slow")):
            with self.assertRaises(ProviderTimeoutError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")

    def test_payout_post_connection_error_is_ambiguous(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", side_effect=requests.exceptions.ConnectionError("refused")):
            with self.assertRaises(ProviderTimeoutError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")

    def test_payout_post_4xx_is_ambiguous_not_rejected(self):
        """Narrow classification (Design Lock F): a 4xx from /v1/payout
        itself is NOT treated as a safe pre-send rejection — no evidence
        supports that assumption. It must be ProviderUnavailableError
        (-> UNKNOWN), never ProviderAuthError/ProviderRejectedError."""
        resp = requests.Response()
        resp.status_code = 400
        err = requests.exceptions.HTTPError("400 client error", response=resp)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", side_effect=err):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")

    def test_payout_post_5xx_is_ambiguous(self):
        resp = requests.Response()
        resp.status_code = 500
        err = requests.exceptions.HTTPError("500 server error", response=resp)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", side_effect=err):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")

    def test_malformed_2xx_response_is_response_error(self):
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", return_value="not-even-a-dict"):
            with self.assertRaises(ProviderResponseError):
                NowPaymentsAdapter().create_payout(_fake_attempt(), callback_url="")

    def test_success_returns_normalized_result(self):
        np_response = {
            "id": "batch-123",
            "status": "CREATED",
            "withdrawals": [{"id": "wd-456", "status": "CREATED"}],
        }
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", return_value=np_response):
            result = NowPaymentsAdapter().create_payout(
                _fake_attempt(provider_amount=Decimal("0.001")), callback_url="https://cb"
            )
        self.assertIsInstance(result, PayoutSubmissionResult)
        self.assertTrue(result.accepted)
        self.assertEqual(result.provider_reference, "wd-456")
        self.assertEqual(result.provider_batch_id, "batch-123")
        self.assertEqual(result.provider_amount, Decimal("0.001"))
        self.assertEqual(result.raw_status, "CREATED")

    def test_withdrawal_id_not_sent_to_nowpayments(self):
        """Confirms the audited fact: our withdrawal_request_id is used
        only as a positional arg to nowpayments.create_payout_with_token()
        (which itself only logs it, never sends it) — no new field is
        invented in the outbound payload."""
        np_response = {"id": "b", "status": "CREATED", "withdrawals": [{"id": "w", "status": "CREATED"}]}
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.create_payout_with_token", return_value=np_response) as m:
            NowPaymentsAdapter().create_payout(_fake_attempt(withdrawal_request_id=42), callback_url="cb")
        args, kwargs = m.call_args
        self.assertEqual(args[3], 42)  # positional withdrawal_id, per create_payout_with_token's own signature


class ParseWebhookTests(SimpleTestCase):
    def _body(self, **kw):
        payload = {
            "id": kw.get("batch_id", "batch-1"),
            "status": kw.get("batch_status", "FINISHED"),
            "withdrawals": kw.get("withdrawals", [{"id": "wd-1", "status": "FINISHED"}]),
        }
        return json.dumps(payload).encode()

    def test_invalid_signature_returns_none(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=False):
            result = NowPaymentsAdapter().parse_webhook(self._body(), {"x-nowpayments-sig": "bad"})
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            result = NowPaymentsAdapter().parse_webhook(b"{not json", {"x-nowpayments-sig": "ok"})
        self.assertIsNone(result)

    def test_finished_maps_to_completed(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(
                self._body(withdrawals=[{"id": "wd-1", "status": "FINISHED"}]),
                {"x-nowpayments-sig": "ok"},
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_COMPLETED)
        self.assertEqual(events[0].raw_status, "FINISHED")

    def test_failed_maps_to_failed(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(
                self._body(withdrawals=[{"id": "wd-1", "status": "FAILED"}]),
                {"x-nowpayments-sig": "ok"},
            )
        self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_FAILED)

    def test_rolling_and_created_map_to_processing(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            for raw in ("ROLLING", "CREATED"):
                events = NowPaymentsAdapter().parse_webhook(
                    self._body(withdrawals=[{"id": "wd-1", "status": raw}]),
                    {"x-nowpayments-sig": "ok"},
                )
                self.assertEqual(events[0].normalized_status, PayoutAttempt.STATUS_PROCESSING, raw)

    def test_unknown_raw_status_discarded(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(
                self._body(withdrawals=[{"id": "wd-1", "status": "SOMETHING_NEW"}]),
                {"x-nowpayments-sig": "ok"},
            )
        self.assertEqual(events, [])

    def test_provider_field_is_nowpayments(self):
        with patch("simulator.nowpayments.verify_ipn_signature", return_value=True):
            events = NowPaymentsAdapter().parse_webhook(self._body(), {"x-nowpayments-sig": "ok"})
        self.assertEqual(events[0].provider, "nowpayments")

    def test_capabilities_are_false(self):
        adapter = NowPaymentsAdapter()
        self.assertEqual(adapter.capabilities["status_query"], False)
        self.assertEqual(adapter.capabilities["cancel"], False)
