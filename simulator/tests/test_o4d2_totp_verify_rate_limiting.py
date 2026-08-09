# simulator/tests/test_o4d2_totp_verify_rate_limiting.py
"""
Microbloque O.4d-2 — TOTP Verification Rate Limiting Enforcement
(Admin/TOTP Anti-Brute-Force Hardening, closes HIGH-3, part 2).

Converts the O.4a observe-only counter in totp_verify_view into real
enforcement, reusing exactly:
  - AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD / WINDOW_SECONDS (5 / 300)
  - rate_peek() / rate_check() (from O.4d-1, unmodified here)
  - EV_AUTH_RATE_LIMITED (from O.4d-1)

All tests drive the REAL view via the Django test Client against a
real Redis (same discipline as O.4d-1's admin login tests) — no
internal helper is called directly, so a bypass would have to survive
the actual request/response cycle to go undetected.
"""
import base64
import uuid

import pyotp
from django.test import TestCase, override_settings

from simulator import broker_audit as _audit
from simulator.models import BrokerAuditEvent, TOTPDevice
from simulator.ratelimit import _get_rl_redis, _RL_PREFIX
from simulator.tests.factories import make_user

TOTP_VERIFY_URL = "/account/2fa/verify/"


def _raw_secret():
    return pyotp.random_base32()


def _b64_secret(raw):
    return "b64:" + base64.b64encode(raw.encode()).decode()


def _valid_code(raw_secret):
    return pyotp.TOTP(raw_secret).now()


class _RedisKeyCleanupMixin:
    def tearDown(self):
        r = _get_rl_redis()
        keys = r.keys(f"{_RL_PREFIX}2fa_verify_fail:*")
        if keys:
            r.delete(*keys)
        super().tearDown()


class TOTPVerifyRateLimitTests(_RedisKeyCleanupMixin, TestCase):

    def setUp(self):
        self.raw_secret = _raw_secret()
        self.user = make_user(username=f"o4d2_{uuid.uuid4().hex[:12]}")
        self.device = TOTPDevice.objects.create(
            user=self.user, secret=_b64_secret(self.raw_secret), confirmed=True,
        )
        from django.utils import timezone
        self.device.confirmed_at = timezone.now()
        self.device.save(update_fields=["confirmed_at"])
        self.client.force_login(self.user)

    def _post_wrong_code(self):
        return self.client.post(TOTP_VERIFY_URL, {"code": "000000"})

    def _post_correct_code(self):
        return self.client.post(TOTP_VERIFY_URL, {"code": _valid_code(self.raw_secret)})

    # ── Core enforcement semantics ──────────────────────────────

    def test_fifth_failure_still_processed_normally_not_429(self):
        for i in range(5):
            resp = self._post_wrong_code()
            self.assertEqual(resp.status_code, 200, f"attempt {i + 1} must not be 429 yet")
            self.assertContains(resp, "Código incorrecto", status_code=200)

    def test_sixth_attempt_blocked_with_429(self):
        for _ in range(5):
            self._post_wrong_code()
        resp = self._post_wrong_code()
        self.assertEqual(resp.status_code, 429)

    def test_blocked_attempt_does_not_call_verifier(self):
        # If the verifier were still being called, a CORRECT code on the
        # 6th attempt would succeed (302 redirect). It must not.
        for _ in range(5):
            self._post_wrong_code()
        resp = self._post_correct_code()
        self.assertEqual(resp.status_code, 429)
        # And the session must still be unverified.
        self.assertFalse(self.client.session.get("2fa_verified", False))

    def test_blocked_attempt_does_not_increment_counter(self):
        for _ in range(5):
            self._post_wrong_code()
        r = _get_rl_redis()
        key = f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}"
        count_before = int(r.get(key) or 0)
        self.assertEqual(count_before, 5)
        for _ in range(5):  # hammer it while blocked
            self._post_wrong_code()
        count_after = int(r.get(key) or 0)
        self.assertEqual(count_after, 5, "blocked attempts must never call rate_check()")

    def test_correct_code_does_not_increment_counter(self):
        resp = self._post_correct_code()
        self.assertEqual(resp.status_code, 302)
        r = _get_rl_redis()
        key = f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}"
        self.assertIsNone(r.get(key))

    def test_correct_code_after_some_failures_still_succeeds(self):
        for _ in range(3):
            self._post_wrong_code()
        resp = self._post_correct_code()
        self.assertEqual(resp.status_code, 302)

    def test_correct_code_after_window_expiry_succeeds(self):
        for _ in range(5):
            self._post_wrong_code()
        resp = self._post_wrong_code()
        self.assertEqual(resp.status_code, 429)  # confirm blocked first

        r = _get_rl_redis()
        r.delete(f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}")  # simulate TTL elapsed

        resp = self._post_correct_code()
        self.assertEqual(resp.status_code, 302)

    def test_get_request_never_blocked_and_never_consumes_counter(self):
        for _ in range(5):
            self._post_wrong_code()
        r = _get_rl_redis()
        key = f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}"
        count_before = int(r.get(key) or 0)
        resp = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp.status_code, 200)
        count_after = int(r.get(key) or 0)
        self.assertEqual(count_before, count_after)

    def test_generic_message_no_leak(self):
        for _ in range(6):
            resp = self._post_wrong_code()
        content = resp.content.decode()
        self.assertIn("Demasiados intentos", content)
        self.assertNotIn(self.raw_secret, content)
        self.assertNotIn("otpauth://", content)
        self.assertNotIn('value="000000"', content)  # submitted code never echoed back

    # ── Fail-open / bypass ───────────────────────────────────────

    def test_redis_down_never_blocks_verification(self):
        from unittest.mock import patch
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            for _ in range(10):
                resp = self._post_wrong_code()
                self.assertEqual(resp.status_code, 200)
            resp = self._post_correct_code()
            self.assertEqual(resp.status_code, 302)

    @override_settings(LOAD_TEST_MODE=True)
    def test_load_test_mode_bypasses_enforcement(self):
        for _ in range(10):
            resp = self._post_wrong_code()
            self.assertEqual(resp.status_code, 200)

    # ── 2fa_next / redirect regression ──────────────────────────

    def test_2fa_next_redirect_preserved_on_success(self):
        session = self.client.session
        session["2fa_next"] = "/dashboard/"
        session.save()
        resp = self._post_correct_code()
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)

    def test_2fa_next_not_consumed_by_blocked_attempt(self):
        session = self.client.session
        session["2fa_next"] = "/dashboard/"
        session.save()
        for _ in range(6):
            self._post_wrong_code()
        self.assertEqual(self.client.session.get("2fa_next"), "/dashboard/")
        r = _get_rl_redis()
        r.delete(f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}")
        resp = self._post_correct_code()
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)

    # ── Auditing ─────────────────────────────────────────────────

    def test_ev_2fa_verify_failed_fires_once_at_threshold(self):
        for _ in range(7):
            self._post_wrong_code()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=_audit.EV_2FA_VERIFY_FAILED, user=self.user,
            ).count(),
            1,
        )

    def test_ev_auth_rate_limited_fires_once(self):
        for _ in range(10):
            self._post_wrong_code()
        events = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, user=self.user,
            metadata__surface="totp_verify",
        )
        self.assertEqual(events.count(), 1)

    def test_ev_auth_rate_limited_metadata_shape(self):
        for _ in range(5):
            self._post_wrong_code()
        event = BrokerAuditEvent.objects.get(
            event_type=_audit.EV_AUTH_RATE_LIMITED, user=self.user,
        )
        self.assertEqual(event.metadata["surface"], "totp_verify")
        self.assertEqual(event.metadata["dimension"], "user")
        self.assertEqual(event.metadata["attempt_count"], 5)
        self.assertEqual(event.metadata["threshold"], 5)
        self.assertEqual(event.metadata["window_seconds"], 300)
        self.assertEqual(event.severity, _audit.Severity.WARNING)

    def test_ev_2fa_verify_failed_only_for_genuinely_incorrect_codes(self):
        # Blocked (429) attempts never call verify_totp_code(), so they
        # must never fabricate an additional EV_2FA_VERIFY_FAILED beyond
        # the single one fired at the exact threshold crossing.
        for _ in range(15):
            self._post_wrong_code()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=_audit.EV_2FA_VERIFY_FAILED, user=self.user,
            ).count(),
            1,
        )

    def test_no_secrets_in_any_audit_event(self):
        for _ in range(10):
            self._post_wrong_code()
        for event in BrokerAuditEvent.objects.filter(user=self.user):
            blob = str(event.metadata) + event.description
            self.assertNotIn(self.raw_secret, blob)
            self.assertNotIn("000000", blob)
            self.assertNotIn("otpauth://", blob)

    def test_correct_code_creates_no_rate_limit_event(self):
        for _ in range(3):
            self._post_wrong_code()
        self._post_correct_code()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=_audit.EV_AUTH_RATE_LIMITED).count(), 0,
        )


class TOTPVerifyRegressionTests(TestCase):
    """O.4a end-to-end flows must remain intact."""

    def setUp(self):
        self.raw_secret = _raw_secret()
        self.user = make_user(username=f"o4d2_regress_{uuid.uuid4().hex[:12]}")
        self.device = TOTPDevice.objects.create(
            user=self.user, secret=_b64_secret(self.raw_secret), confirmed=True,
        )
        from django.utils import timezone
        self.device.confirmed_at = timezone.now()
        self.device.save(update_fields=["confirmed_at"])
        self.client.force_login(self.user)

    def test_get_shows_verify_form(self):
        resp = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Verificación 2FA")

    def test_correct_code_marks_session_verified_and_redirects_home(self):
        resp = self.client.post(TOTP_VERIFY_URL, {"code": _valid_code(self.raw_secret)})
        self.assertEqual(resp.status_code, 302)
        # A follow-up GET should now short-circuit past the verify form
        resp2 = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp2.status_code, 302)

    def test_no_device_passes_through(self):
        self.device.delete()
        resp = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp.status_code, 302)

    def test_already_verified_session_redirects_without_reverifying(self):
        self.client.post(TOTP_VERIFY_URL, {"code": _valid_code(self.raw_secret)})
        resp = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp.status_code, 302)

    def test_wrong_code_shows_error_without_rate_limiting_on_first_attempt(self):
        resp = self.client.post(TOTP_VERIFY_URL, {"code": "111111"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Código incorrecto")
