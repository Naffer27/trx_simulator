# simulator/tests/test_withdrawal_e2e_02c_verify_payout.py
"""
WITHDRAWAL-E2E-02C FASE B — NowPayments Verify Payout (provider-side
2FA) integration.

Endpoint confirmed against the official NowPaymentsIO Node.js SDK
source (github.com/NowPaymentsIO/nowpayments-sdk-nodejs) — see the
WITHDRAWAL-E2E-02C design report. Covers:

  - nowpayments.py::verify_payout_with_token() — POST /v1/payout/
    {batch_id}/verify, verification_code never logged.
  - payout_providers.py::NowPaymentsAdapter.verify_payout() — uses
    provider_batch_id (never provider_reference), same
    auth/timeout/HTTP error classification discipline as create_payout().
  - payout_orchestrator.py::submit_payout_verification() — the ONLY
    entry point that sets PayoutAttempt.verified_at. Never touches
    Wallet, never calls create_payout(), never refunds directly — the
    existing, unmodified _apply_confirmed_failure_with_refund() remains
    the sole refund path.
  - The admin verify_payout_view — staff/superuser-only, POST-only,
    CSRF-protected, never re-displays a submitted code.

No real HTTP anywhere in this file — simulator.nowpayments's own
requests calls are mocked, same pattern as
test_withdrawal_e2e_02b_refund_engine.py.
"""
import io
import logging
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import AuditLog, PayoutAttempt, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import PayoutVerificationError, submit_payout_verification
from simulator.payout_providers import (
    NowPaymentsAdapter, ProviderAuthError, ProviderError, ProviderResponseError,
    ProviderTimeoutError, ProviderUnavailableError,
)
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import debit_wallet

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_wr(user, amount="200.00"):
    debit_tx = debit_wallet(user.wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="t")
    return WithdrawalRequest.objects.create(
        user=user, amount_usd=Decimal(amount), crypto_currency="usdttrc20",
        wallet_address="TUFt1PhynXEWJtfQQoyAbgAh5N4tDEQVrB",
        status=WithdrawalRequest.STATUS_PROCESSING, debit_tx=debit_tx,
    )


def _make_attempt(wr, *, status=PayoutAttempt.STATUS_PROCESSING, provider_batch_id="batch-1",
                   provider_reference="payout-1", verified_at=None, attempt_number=1):
    return PayoutAttempt.objects.create(
        withdrawal_request=wr, provider="nowpayments", attempt_number=attempt_number,
        idempotency_key=f"e02c-{wr.pk}-{attempt_number}",
        requested_amount_usd=wr.amount_usd, requested_asset="usdttrc20",
        destination_address=wr.wallet_address, status=status,
        submitted_at=timezone.now(), provider_reference=provider_reference,
        provider_batch_id=provider_batch_id, verified_at=verified_at,
    )


def _mock_verify_response(status_code=200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = body if body is not None else {"batch_withdrawal_id": "batch-1", "verified": True}
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# nowpayments.py::verify_payout_with_token — low-level HTTP contract
# ─────────────────────────────────────────────────────────────────────────────

class VerifyPayoutWithTokenTests(TestCase):
    def test_posts_to_correct_endpoint_with_verification_code_body(self):
        from simulator import nowpayments as np
        with patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()) as post_mock:
            np.verify_payout_with_token("batch-42", "123456", "tok")
        args, kwargs = post_mock.call_args
        self.assertEqual(args[0], f"{np._BASE}/payout/batch-42/verify")
        self.assertEqual(kwargs["json"], {"verification_code": "123456"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

    def test_non_2xx_raises_http_error_without_body_derived_detail(self):
        from simulator import nowpayments as np
        with patch("simulator.nowpayments.requests.post",
                    return_value=_mock_verify_response(status_code=400, body={"message": "invalid code 123456"})):
            with self.assertRaises(requests.exceptions.HTTPError) as ctx:
                np.verify_payout_with_token("batch-1", "654321", "tok")
        # The error body is deliberately NEVER parsed/echoed into the exception —
        # confirms the code (or anything from the error body) cannot leak this way.
        self.assertNotIn("654321", str(ctx.exception))
        self.assertNotIn("invalid code", str(ctx.exception))


# ─────────────────────────────────────────────────────────────────────────────
# NowPaymentsAdapter.verify_payout — batch_id usage, error classification
# ─────────────────────────────────────────────────────────────────────────────

class AdapterVerifyPayoutTests(TestCase):
    def _attempt_stub(self, batch_id="batch-9", reference="payout-9"):
        class _A:
            pk = 1
        a = _A()
        a.provider_batch_id = batch_id
        a.provider_reference = reference
        return a

    def test_uses_provider_batch_id_not_provider_reference(self):
        attempt = self._attempt_stub(batch_id="THE-BATCH-ID", reference="THE-PAYOUT-ID")
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()) as post_mock:
            NowPaymentsAdapter().verify_payout(attempt, "123456")
        url = post_mock.call_args[0][0]
        self.assertIn("THE-BATCH-ID", url)
        self.assertNotIn("THE-PAYOUT-ID", url)

    def test_auth_failure_raises_provider_auth_error_no_post_attempted(self):
        attempt = self._attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", side_effect=requests.exceptions.Timeout("down")), \
             patch("simulator.nowpayments.requests.post") as post_mock:
            with self.assertRaises(ProviderAuthError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")
        post_mock.assert_not_called()

    def test_timeout_raises_provider_timeout_error(self):
        attempt = self._attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", side_effect=requests.exceptions.Timeout("slow")):
            with self.assertRaises(ProviderTimeoutError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_http_4xx_raises_provider_unavailable_error(self):
        """Covers 'código incorrecto' and 'HTTP 4xx proveedor' — this
        codebase deliberately does not distinguish the reason (see the
        02C design report: NowPayments' exact wrong-code error contract
        was not confirmed with certainty)."""
        attempt = self._attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_http_404_raises_provider_unavailable_error(self):
        """'batch_id inexistente' — same normalized error family as any
        other non-2xx; no GET-style NOT_FOUND distinction on this POST
        endpoint."""
        attempt = self._attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=404)):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_http_5xx_raises_provider_unavailable_error(self):
        attempt = self._attempt_stub()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=500)):
            with self.assertRaises(ProviderUnavailableError):
                NowPaymentsAdapter().verify_payout(attempt, "123456")

    def test_unparseable_success_body_is_accepted_not_an_error(self):
        """
        WITHDRAWAL-E2E-02G FASE B — this used to assert the opposite
        (ProviderResponseError) — that was the exact bug that made WR18's
        real, provider-accepted Verify call (HTTP 200, body NowPayments'
        own docs don't guarantee the shape of) surface to the Owner as
        "La verificación falló". An HTTP 2xx we can't parse is now
        treated as accepted-but-unconfirmed, never as a failure —
        verify_payout() raises nothing.
        """
        attempt = self._attempt_stub()
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.side_effect = ValueError("not json")
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=resp):
            result = NowPaymentsAdapter().verify_payout(attempt, "123456")
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
# submit_payout_verification — orchestrator-level, the 20 required cases
# ─────────────────────────────────────────────────────────────────────────────

class SubmitPayoutVerificationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="200.00")
        self.wallet.refresh_from_db()

    # 1/3 — código correcto
    def test_correct_code_sets_verified_at_no_wallet_change(self):
        attempt = _make_attempt(self.wr)
        before = self.wallet.available_balance
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
            result = submit_payout_verification(attempt.pk, "123456", actor=self.user)
        self.assertIsNotNone(result.verified_at)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.verified_at)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)  # unchanged — verify never mutates status
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 0,
        )

    # 2 — código incorrecto
    def test_incorrect_code_leaves_attempt_unchanged_no_refund(self):
        attempt = _make_attempt(self.wr)
        before = self.wallet.available_balance
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "000000", actor=self.user)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.verified_at)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before)

    # 3 (expirado) — same code path as #2; NowPayments distinguishes,
    # we deliberately do not (see design report K).
    def test_expired_code_treated_same_as_incorrect_no_special_case(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "111111", actor=self.user)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.verified_at)

    # 4 — doble submit (sequential)
    def test_second_verify_after_success_is_rejected_locally_no_second_http_call(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()) as post_mock:
            submit_payout_verification(attempt.pk, "123456", actor=self.user)
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        self.assertEqual(post_mock.call_count, 1, "the second call must never reach the provider")

    # 5 — terminal attempt (COMPLETED and FAILED)
    def test_terminal_completed_attempt_rejected_before_provider_call(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_COMPLETED)
        with patch("simulator.nowpayments.requests.post") as post_mock:
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        post_mock.assert_not_called()

    def test_terminal_failed_attempt_rejected_before_provider_call(self):
        attempt = _make_attempt(self.wr, status=PayoutAttempt.STATUS_FAILED)
        with patch("simulator.nowpayments.requests.post") as post_mock:
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        post_mock.assert_not_called()

    # 6 — batch inexistente
    def test_nonexistent_batch_id_provider_error_propagates_no_mutation(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=404)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.verified_at)

    # 7 — timeout proveedor
    def test_provider_timeout_no_mutation(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", side_effect=requests.exceptions.Timeout("slow")):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.verified_at)

    # 8 — HTTP 4xx (distinct narrative from "wrong code", same mechanism)
    def test_generic_http_4xx_no_mutation(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=422)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.verified_at)

    # 9 — HTTP 5xx
    def test_http_5xx_no_mutation_reconciliable(self):
        attempt = _make_attempt(self.wr)
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=503)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        attempt.refresh_from_db()
        # Still PROCESSING, still reconciliable by the existing 02B pipeline.
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.assertIsNone(attempt.verified_at)

    # 10 — webhook llega ANTES de Verify (attempt already terminal via webhook)
    def test_webhook_completed_before_verify_attempt_then_verify_is_rejected(self):
        from simulator.payout_orchestrator import apply_provider_webhook_event
        from simulator.payout_providers import ProviderPayoutEvent
        attempt = _make_attempt(self.wr, provider_reference="wh-ref")
        apply_provider_webhook_event(ProviderPayoutEvent(
            provider="nowpayments", provider_reference="wh-ref", provider_batch_id="",
            normalized_status=PayoutAttempt.STATUS_COMPLETED, raw_status="FINISHED",
            provider_amount=None, occurred_at=timezone.now(),
        ))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)
        with patch("simulator.nowpayments.requests.post") as post_mock:
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        post_mock.assert_not_called()

    # 11 — webhook llega DESPUÉS de Verify
    def test_webhook_completed_after_successful_verify_applies_normally(self):
        from simulator.payout_orchestrator import apply_provider_webhook_event
        from simulator.payout_providers import ProviderPayoutEvent
        attempt = _make_attempt(self.wr, provider_reference="wh-ref-2")
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
            submit_payout_verification(attempt.pk, "123456", actor=self.user)
        apply_provider_webhook_event(ProviderPayoutEvent(
            provider="nowpayments", provider_reference="wh-ref-2", provider_batch_id="",
            normalized_status=PayoutAttempt.STATUS_COMPLETED, raw_status="FINISHED",
            provider_amount=None, occurred_at=timezone.now(),
        ))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_COMPLETED)
        self.assertIsNotNone(attempt.verified_at, "verified_at must survive the later webhook untouched")

    # 12/13 — reconciliación detecta REJECTED tras un Verify exitoso; refund exactamente una vez
    def test_reject_after_verify_still_refunds_exactly_once(self):
        from simulator.payout_orchestrator import apply_provider_webhook_event
        from simulator.payout_providers import ProviderPayoutEvent
        attempt = _make_attempt(self.wr, provider_reference="wh-ref-3")
        before = self.wallet.available_balance
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
            submit_payout_verification(attempt.pk, "123456", actor=self.user)

        event = ProviderPayoutEvent(
            provider="nowpayments", provider_reference="wh-ref-3", provider_batch_id="",
            normalized_status=PayoutAttempt.STATUS_FAILED, raw_status="REJECTED",
            provider_amount=None, occurred_at=timezone.now(),
        )
        apply_provider_webhook_event(event)
        apply_provider_webhook_event(event)  # duplicate delivery

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, before + self.wr.amount_usd)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION).count(), 1,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_FAILED)
        self.assertIsNotNone(attempt.verified_at, "verified_at is a historical fact — refund never clears it")

    # 17 — falta provider_batch_id
    def test_missing_provider_batch_id_rejected_locally(self):
        attempt = _make_attempt(self.wr, provider_batch_id="")
        with patch("simulator.nowpayments.requests.post") as post_mock:
            with self.assertRaises(PayoutVerificationError):
                submit_payout_verification(attempt.pk, "123456", actor=self.user)
        post_mock.assert_not_called()

    # 18 — formato de código inválido
    def test_invalid_code_format_rejected_locally_no_http_call(self):
        attempt = _make_attempt(self.wr)
        for bad_code in ("12345", "1234567", "abcdef", "", "12 456"):
            with patch("simulator.nowpayments.requests.post") as post_mock:
                with self.assertRaises(PayoutVerificationError):
                    submit_payout_verification(attempt.pk, bad_code, actor=self.user)
                post_mock.assert_not_called()

    # 15/19 — código NO aparece en logs
    def test_code_never_appears_in_logs(self):
        attempt = _make_attempt(self.wr)
        secret_code = "918273"
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        target_logger = logging.getLogger("simulator.nowpayments")
        target_logger.addHandler(handler)
        try:
            with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
                 patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
                submit_payout_verification(attempt.pk, secret_code, actor=self.user)
        finally:
            target_logger.removeHandler(handler)
        self.assertNotIn(secret_code, log_stream.getvalue())

    def test_code_never_appears_in_logs_on_failure_either(self):
        attempt = _make_attempt(self.wr)
        secret_code = "445566"
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        target_logger = logging.getLogger("simulator.nowpayments")
        target_logger.addHandler(handler)
        try:
            with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
                 patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
                with self.assertRaises(ProviderError):
                    submit_payout_verification(attempt.pk, secret_code, actor=self.user)
        finally:
            target_logger.removeHandler(handler)
        self.assertNotIn(secret_code, log_stream.getvalue())

    # 16/20 — código NO queda persistido en DB/AuditLog
    def test_code_never_persisted_in_payout_attempt_or_audit_log(self):
        attempt = _make_attempt(self.wr)
        secret_code = "775533"
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
            submit_payout_verification(attempt.pk, secret_code, actor=self.user, request=None)

        attempt.refresh_from_db()
        for field in attempt._meta.fields:
            value = getattr(attempt, field.name)
            if isinstance(value, str):
                self.assertNotIn(secret_code, value, f"code leaked into PayoutAttempt.{field.name}")

        for entry in AuditLog.objects.filter(action__icontains=str(attempt.pk)):
            self.assertNotIn(secret_code, entry.action)
            self.assertNotIn(secret_code, str(entry.detail))

    def test_code_never_persisted_in_audit_log_on_failure_either(self):
        attempt = _make_attempt(self.wr)
        secret_code = "662244"
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
            with self.assertRaises(ProviderError):
                submit_payout_verification(attempt.pk, secret_code, actor=self.user, request=None)
        for entry in AuditLog.objects.filter(action__icontains=str(attempt.pk)):
            self.assertNotIn(secret_code, entry.action)
            self.assertNotIn(secret_code, str(entry.detail))


# ─────────────────────────────────────────────────────────────────────────────
# 14 — concurrencia real (select_for_update genuinely serializes)
# ─────────────────────────────────────────────────────────────────────────────

class ConcurrentDoubleSubmitTests(TransactionTestCase):
    @unittest.skipUnless(
        connection.vendor == "postgresql",
        "select_for_update() serialization is a genuine MVCC row-lock "
        "guarantee — SQLite's coarser table/file-level locking cannot "
        "prove or disprove two real threads truly serialize on the same "
        "PayoutAttempt row (same reasoning already established for "
        "test_atomic_guard_lock_order.py's real-concurrency classes, "
        "BBOOK-CLOSE-03). Never silently weakened to an assertion SQLite "
        "can pass by accident — explicit skip instead.",
    )
    def test_two_real_threads_only_one_reaches_the_provider(self):
        import threading

        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_wr(user, amount="100.00")
        attempt = _make_attempt(wr)

        call_count = {"n": 0}
        lock = threading.Lock()

        def fake_post(*args, **kwargs):
            with lock:
                call_count["n"] += 1
            return _mock_verify_response()

        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(index):
            from django.db import connection
            with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
                 patch("simulator.nowpayments.requests.post", side_effect=fake_post):
                barrier.wait(timeout=5)
                try:
                    submit_payout_verification(attempt.pk, "123456", actor=user)
                    results[index] = "ok"
                except Exception as exc:  # noqa: BLE001
                    results[index] = f"error:{type(exc).__name__}"
                finally:
                    connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertIn("ok", results)
        self.assertEqual(call_count["n"], 1, "the provider must be called exactly once across both threads")
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.verified_at)


# ─────────────────────────────────────────────────────────────────────────────
# Status map safety — WITHDRAWAL-E2E-02C's careful, evidence-based decision
# ─────────────────────────────────────────────────────────────────────────────

class StatusMapSafetyTests(TestCase):
    def test_in_flight_statuses_not_in_webhook_map_avoid_same_status_crash(self):
        """The official 'new'/'creating'/'waiting'/'processing'/'sending'
        statuses are deliberately NOT added to _RAW_STATUS_TO_NORMALIZED
        (unlike _RAW_STATUS_TO_LOOKUP_OUTCOME) — see the module comment.
        A webhook reporting one of these while the attempt is already
        PROCESSING must route to MANUAL_REVIEW, never attempt a
        same-status transition (which would raise
        InvalidPayoutAttemptTransition, uncaught, up to the view)."""
        from simulator.payout_orchestrator import get_or_create_webhook_event, process_webhook_event
        from simulator.payout_providers import ProviderPayoutEvent
        from simulator.models import PayoutWebhookEvent

        user = make_user()
        make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_wr(user, amount="50.00")
        attempt = _make_attempt(wr, provider_reference="inflight-ref")

        for raw in ("NEW", "CREATING", "WAITING", "PROCESSING", "SENDING"):
            with self.subTest(raw=raw):
                webhook_event, _ = get_or_create_webhook_event(ProviderPayoutEvent(
                    provider="nowpayments", provider_reference="inflight-ref", provider_batch_id="",
                    normalized_status=None, raw_status=raw, provider_amount=None,
                    occurred_at=timezone.now(), raw_event_payload={"status": raw, "n": raw},
                ))
                # Must never raise — this is the whole point of the guard.
                process_webhook_event(webhook_event.pk)
                webhook_event.refresh_from_db()
                self.assertEqual(webhook_event.correlation_status, PayoutWebhookEvent.STATUS_MANUAL_REVIEW)
                attempt.refresh_from_db()
                self.assertEqual(attempt.status, PayoutAttempt.STATUS_PROCESSING, f"must stay untouched for {raw}")

    def test_in_flight_statuses_are_in_lookup_outcome_map(self):
        """Safe here specifically because reconcile_unknown_payout_attempts()
        only ever calls lookup_payout() for an attempt in STATUS_UNKNOWN,
        and UNKNOWN -> PROCESSING IS an allowed transition."""
        from simulator.payout_providers import _RAW_STATUS_TO_LOOKUP_OUTCOME, PayoutLookupOutcome
        for raw in ("NEW", "WAITING", "PROCESSING", "CREATING", "SENDING"):
            self.assertEqual(_RAW_STATUS_TO_LOOKUP_OUTCOME[raw], PayoutLookupOutcome.FOUND_PROCESSING, raw)

    def test_ambiguous_statuses_not_mapped_anywhere_for_refund_safety(self):
        """rejected_not_checked / cancelled / canceled deliberately absent
        from BOTH maps — genuine fund-movement ambiguity, never
        auto-refunded (design report K)."""
        from simulator.payout_providers import _RAW_STATUS_TO_LOOKUP_OUTCOME, _RAW_STATUS_TO_NORMALIZED
        for raw in ("REJECTED_NOT_CHECKED", "CANCELLED", "CANCELED"):
            self.assertNotIn(raw, _RAW_STATUS_TO_NORMALIZED)
            self.assertNotIn(raw, _RAW_STATUS_TO_LOOKUP_OUTCOME)


# ─────────────────────────────────────────────────────────────────────────────
# Admin view — staff-only, POST-only, CSRF, never re-displays the code
# ─────────────────────────────────────────────────────────────────────────────

class VerifyPayoutAdminViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        make_wallet(self.user, initial_balance=Decimal("1000"))
        self.wr = _make_wr(self.user, amount="30.00")
        self.attempt = _make_attempt(self.wr)
        self.url = reverse("admin:payoutattempt_verify", args=[self.attempt.pk])

    def _superuser_client(self):
        admin = User.objects.create_user(
            username="e02c_admin", email="e02c_admin@x.com", password="p",
            is_staff=True, is_superuser=True,
        )
        client = Client()
        client.force_login(admin)
        return client

    def test_non_staff_cannot_access(self):
        client = Client()
        resp = client.get(self.url)
        self.assertIn(resp.status_code, (302, 403))  # redirected to admin login, or forbidden

    def test_staff_non_superuser_forbidden(self):
        staff = User.objects.create_user(
            username="e02c_staff", email="e02c_staff@x.com", password="p", is_staff=True, is_superuser=False,
        )
        client = Client()
        client.force_login(staff)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_superuser_get_renders_form_without_prefilled_code(self):
        client = self._superuser_client()
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'value="1', resp.content)  # no code value ever pre-filled

    def test_get_request_never_calls_provider(self):
        client = self._superuser_client()
        with patch("simulator.nowpayments.requests.post") as post_mock:
            client.get(self.url)
        post_mock.assert_not_called()

    def test_successful_post_sets_verified_at_and_redirects(self):
        client = self._superuser_client()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response()):
            resp = client.post(self.url, {"verification_code": "123456"})
        self.assertEqual(resp.status_code, 302)
        self.attempt.refresh_from_db()
        self.assertIsNotNone(self.attempt.verified_at)

    def test_response_never_echoes_submitted_code(self):
        client = self._superuser_client()
        with patch("simulator.nowpayments._get_jwt_token", return_value="tok"), \
             patch("simulator.nowpayments.requests.post", return_value=_mock_verify_response(status_code=400)):
            resp = client.post(self.url, {"verification_code": "998877"}, follow=True)
        self.assertNotIn(b"998877", resp.content)

    def test_csrf_protection_active(self):
        csrf_client = Client(enforce_csrf_checks=True)
        admin = User.objects.create_user(
            username="e02c_csrf_admin", email="e02c_csrf_admin@x.com", password="p",
            is_staff=True, is_superuser=True,
        )
        csrf_client.force_login(admin)
        resp = csrf_client.post(self.url, {"verification_code": "123456"})
        self.assertEqual(resp.status_code, 403)

    def test_ineligible_attempt_redirects_without_calling_provider(self):
        self.attempt.status = PayoutAttempt.STATUS_COMPLETED
        self.attempt.save(update_fields=["status"])
        client = self._superuser_client()
        with patch("simulator.nowpayments.requests.post") as post_mock:
            resp = client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        post_mock.assert_not_called()
