# simulator/tests/test_o4d1_admin_login_rate_limiting.py
"""
Microbloque O.4d-1 — MoneyBrokerAdminSite Login Rate Limiting
(Admin/TOTP Anti-Brute-Force Hardening, closes HIGH-3).

Covers:
  - simulator/ratelimit.py::rate_peek() — new, strictly read-only
    primitive (does not touch rate_check() at all).
  - MoneyBrokerAdminSite.login() — IP (8/300) + username (5/300)
    blocking, pre-check via rate_peek() (never increments), increment
    only on a genuine authentication failure (via rate_check(),
    unchanged), EV_AUTH_RATE_LIMITED fired exactly once per block-
    window-activation.

All tests drive the REAL admin login URL via the Django test Client —
no internal helper is called directly for the HTTP-level behavior,
so a bypass would have to survive the actual request/response cycle
to go undetected.
"""
import uuid

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator import broker_audit as _audit
from simulator.models import BrokerAuditEvent
from simulator.ratelimit import _get_rl_redis, _RL_PREFIX, rate_check, rate_peek

from .factories import make_user

ADMIN_LOGIN_URL = reverse("admin:login")
ADMIN_INDEX_URL = reverse("admin:index")


def _unique_suffix():
    return uuid.uuid4().hex[:12]


class _RedisKeyCleanupMixin:
    """Every test uses uuid-suffixed usernames/IPs so keys never collide
    across tests, but clean up defensively anyway."""

    def tearDown(self):
        r = _get_rl_redis()
        keys = r.keys(f"{_RL_PREFIX}admin_login_fail:*")
        if keys:
            r.delete(*keys)
        super().tearDown()


# ─────────────────────────────────────────────
# rate_peek() — pure primitive, in isolation
# ─────────────────────────────────────────────

class RatePeekTests(TestCase):

    def setUp(self):
        self.r = _get_rl_redis()
        self.key = f"test_o4d1_peek:{_unique_suffix()}"

    def tearDown(self):
        self.r.delete(f"{_RL_PREFIX}{self.key}")

    def test_nonexistent_key_returns_zero(self):
        self.assertEqual(rate_peek(self.key), 0)

    def test_existing_key_returns_correct_count(self):
        rate_check(self.key, limit=100, window=60)
        rate_check(self.key, limit=100, window=60)
        rate_check(self.key, limit=100, window=60)
        self.assertEqual(rate_peek(self.key), 3)

    def test_two_peeks_do_not_change_count(self):
        rate_check(self.key, limit=100, window=60)
        first = rate_peek(self.key)
        second = rate_peek(self.key)
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(rate_peek(self.key), 1)

    def test_peek_does_not_change_ttl(self):
        rate_check(self.key, limit=100, window=60)
        ttl_before = self.r.ttl(f"{_RL_PREFIX}{self.key}")
        self.assertGreater(ttl_before, 0)
        rate_peek(self.key)
        rate_peek(self.key)
        ttl_after = self.r.ttl(f"{_RL_PREFIX}{self.key}")
        self.assertLessEqual(ttl_after, ttl_before)

    def test_redis_error_returns_zero(self):
        from unittest.mock import patch
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("boom")):
            self.assertEqual(rate_peek(self.key), 0)

    @override_settings(LOAD_TEST_MODE=True)
    def test_load_test_mode_returns_zero(self):
        rate_check(self.key, limit=1, window=60)  # would normally read back as 1
        self.assertEqual(rate_peek(self.key), 0)

    def test_rate_check_behavior_unchanged(self):
        # Same assertions as test_ratelimit_window.py's own coverage —
        # rate_peek()'s addition must not have altered rate_check() at all.
        allowed, count = rate_check(self.key, limit=2, window=60)
        self.assertTrue(allowed)
        self.assertEqual(count, 1)
        allowed, count = rate_check(self.key, limit=2, window=60)
        self.assertTrue(allowed)
        self.assertEqual(count, 2)
        allowed, count = rate_check(self.key, limit=2, window=60)
        self.assertFalse(allowed)
        self.assertEqual(count, 3)


# ─────────────────────────────────────────────
# IP dimension
# ─────────────────────────────────────────────

class AdminLoginIpRateLimitTests(_RedisKeyCleanupMixin, TestCase):

    def setUp(self):
        self.username = f"o4d1_ip_{_unique_suffix()}"
        self.user = User.objects.create_user(
            username=self.username, password="correct-password-123", is_staff=True,
        )
        self.client = Client(REMOTE_ADDR="203.0.113.10")

    def _distinct_username(self, i):
        # A different, unregistered username per attempt — isolates the
        # IP dimension (8/300) from the username dimension (5/300),
        # which would otherwise trip first since it has a lower threshold.
        return f"{self.username}_nope_{i}"

    def test_eighth_failure_still_processed_normally_not_429(self):
        for i in range(8):
            resp = self.client.post(
                ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"},
            )
            self.assertEqual(resp.status_code, 200, f"attempt {i + 1} must not be 429 yet")

    def test_ninth_attempt_from_same_ip_is_blocked(self):
        for i in range(8):
            self.client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
        resp = self.client.post(
            ADMIN_LOGIN_URL, {"username": self._distinct_username(8), "password": "wrong"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_blocked_attempt_never_reaches_authentication(self):
        # A yet-different username on the 9th attempt, from the SAME
        # now-IP-blocked client, must still be blocked — proves the
        # block is keyed by IP, independent of which username is
        # tried, and that authentication is never attempted once blocked.
        for i in range(8):
            self.client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
        resp = self.client.post(
            ADMIN_LOGIN_URL, {"username": "totally-different-user", "password": "whatever"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_generic_message_shown_no_enumeration(self):
        for i in range(9):
            resp = self.client.post(
                ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"},
            )
        content = resp.content.decode()
        self.assertIn("Demasiados intentos", content)
        self.assertNotIn("wrong", content)  # the submitted password never echoed back
        self.assertNotIn("does not exist", content.lower())
        self.assertNotIn("superuser", content.lower())
        self.assertNotIn("correct password", content.lower())

    def test_admin_login_template_is_rendered_on_block(self):
        for i in range(9):
            resp = self.client.post(
                ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"},
            )
        self.assertTemplateUsed(resp, "admin/login.html")

    def test_ip_block_does_not_prevent_login_from_a_different_ip(self):
        for i in range(9):
            self.client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
        other_client = Client(REMOTE_ADDR="198.51.100.20")
        resp = other_client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)


# ─────────────────────────────────────────────
# Username dimension
# ─────────────────────────────────────────────

class AdminLoginUsernameRateLimitTests(_RedisKeyCleanupMixin, TestCase):

    def setUp(self):
        self.username = f"o4d1_user_{_unique_suffix()}"
        self.user = User.objects.create_user(
            username=self.username, password="correct-password-123", is_staff=True,
        )

    def test_fifth_failure_still_processed_normally_not_429(self):
        client = Client(REMOTE_ADDR="203.0.113.10")
        for i in range(5):
            resp = client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
            self.assertEqual(resp.status_code, 200, f"attempt {i + 1} must not be 429 yet")

    def test_sixth_attempt_blocked_even_from_a_different_ip(self):
        # Credential-stuffing simulation: same username, 6 different IPs.
        for i in range(5):
            client = Client(REMOTE_ADDR=f"203.0.113.{i}")
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        attacker_client = Client(REMOTE_ADDR="203.0.113.99")
        resp = attacker_client.post(
            ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_username_normalization_case_insensitive(self):
        client = Client(REMOTE_ADDR="203.0.113.10")
        for _ in range(5):
            client.post(
                ADMIN_LOGIN_URL,
                {"username": self.username.upper(), "password": "wrong"},
            )
        resp = client.post(
            ADMIN_LOGIN_URL, {"username": self.username.lower(), "password": "wrong"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_username_block_does_not_affect_other_usernames(self):
        client = Client(REMOTE_ADDR="203.0.113.10")
        for _ in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        other_user = User.objects.create_user(
            username=f"o4d1_other_{_unique_suffix()}", password="correct-password-123",
            is_staff=True,
        )
        other_client = Client(REMOTE_ADDR="203.0.113.10")  # even from the same IP
        resp = other_client.post(
            ADMIN_LOGIN_URL,
            {"username": other_user.username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)


# ─────────────────────────────────────────────
# Success never consumes the failure budget
# ─────────────────────────────────────────────

class SuccessDoesNotConsumeBudgetTests(_RedisKeyCleanupMixin, TestCase):

    def setUp(self):
        self.username = f"o4d1_success_{_unique_suffix()}"
        self.user = User.objects.create_user(
            username=self.username, password="correct-password-123", is_staff=True,
        )
        self.client = Client(REMOTE_ADDR="203.0.113.50")

    def test_successful_logins_never_trigger_block(self):
        for _ in range(20):
            resp = self.client.post(
                ADMIN_LOGIN_URL,
                {"username": self.username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
            )
            self.assertRedirects(resp, ADMIN_INDEX_URL)
            self.client.post(reverse("admin:logout"))  # log back out for the next iteration

    def test_successful_login_after_some_failures_does_not_reset_or_block(self):
        for _ in range(3):
            self.client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        resp = self.client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)


# ─────────────────────────────────────────────
# Window expiry — block is temporary, never permanent
# ─────────────────────────────────────────────

class WindowExpiryTests(_RedisKeyCleanupMixin, TestCase):

    def test_block_expires_after_window(self):
        from simulator.ratelimit import _RL_PREFIX as _p, _get_rl_redis as _get_r
        username = f"o4d1_expiry_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.77")
        for _ in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        self.assertEqual(resp.status_code, 429)

        # Simulate window expiry by deleting the Redis key directly —
        # equivalent to the TTL elapsing, without an actual 5-minute sleep.
        r = _get_r()
        r.delete(f"{_p}admin_login_fail:user:{username.lower()}")

        resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        self.assertEqual(resp.status_code, 200)  # processed normally again, not blocked


# ─────────────────────────────────────────────
# Redis down / LOAD_TEST_MODE — fail-open, never blocks legitimate login
# ─────────────────────────────────────────────

class FailOpenTests(_RedisKeyCleanupMixin, TestCase):

    def test_redis_down_never_blocks_login(self):
        from unittest.mock import patch
        username = f"o4d1_redisdown_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.88")
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            for _ in range(15):
                resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
                self.assertEqual(resp.status_code, 200)
            resp = client.post(
                ADMIN_LOGIN_URL,
                {"username": username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
            )
            self.assertRedirects(resp, ADMIN_INDEX_URL)

    @override_settings(LOAD_TEST_MODE=True)
    def test_load_test_mode_never_blocks_login(self):
        username = f"o4d1_loadtest_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.89")
        for _ in range(15):
            resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
            self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────
# Auditing — EV_AUTH_RATE_LIMITED, fired once, correct metadata
# ─────────────────────────────────────────────

class AuditingTests(_RedisKeyCleanupMixin, TestCase):

    def setUp(self):
        self.username = f"o4d1_audit_{_unique_suffix()}"
        User.objects.create_user(username=self.username, password="correct-password-123", is_staff=True)

    def test_event_fired_exactly_once_for_username_dimension(self):
        client = Client(REMOTE_ADDR="203.0.113.30")
        for _ in range(9):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        events = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, metadata__dimension="user",
        )
        self.assertEqual(events.count(), 1)

    def test_event_fired_exactly_once_for_ip_dimension(self):
        client = Client(REMOTE_ADDR="203.0.113.31")
        for i in range(9):
            client.post(
                ADMIN_LOGIN_URL, {"username": f"nonexistent_{i}_{self.username}", "password": "wrong"},
            )
        events = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, metadata__dimension="ip",
        )
        self.assertEqual(events.count(), 1)

    def test_metadata_shape(self):
        client = Client(REMOTE_ADDR="203.0.113.32")
        for _ in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        event = BrokerAuditEvent.objects.get(
            event_type=_audit.EV_AUTH_RATE_LIMITED, metadata__dimension="user",
        )
        self.assertEqual(event.metadata["surface"], "admin_login")
        self.assertEqual(event.metadata["threshold"], 5)
        self.assertEqual(event.metadata["window_seconds"], 300)
        self.assertEqual(event.metadata["attempt_count"], 5)
        self.assertEqual(event.metadata["username_attempted"], self.username.lower())
        self.assertEqual(event.severity, "WARNING")
        self.assertEqual(event.category, "AUTHENTICATION")

    def test_no_secrets_ever_recorded(self):
        client = Client(REMOTE_ADDR="203.0.113.33")
        secret_password = "SuperSecretPassword!123"
        for _ in range(6):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": secret_password})
        for event in BrokerAuditEvent.objects.filter(event_type=_audit.EV_AUTH_RATE_LIMITED):
            self.assertNotIn(secret_password, str(event.metadata))
            self.assertNotIn(secret_password, event.description)

    def test_blocked_attempts_do_not_write_additional_events(self):
        client = Client(REMOTE_ADDR="203.0.113.34")
        for _ in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        # Now blocked — hammer it 10 more times.
        for _ in range(10):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        events = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, metadata__dimension="user",
        )
        self.assertEqual(events.count(), 1, "Repeated blocked attempts must not flood the audit trail")


# ─────────────────────────────────────────────
# Regression — existing O.4a/AUDIT-04b behavior unaffected
# ─────────────────────────────────────────────

class RegressionTests(TestCase):

    def test_normal_single_failed_login_still_audited_as_before(self):
        username = f"o4d1_regress_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.40")
        resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            BrokerAuditEvent.objects.filter(event_type=_audit.EV_ADMIN_SITE_LOGIN_FAILED).exists()
        )

    def test_normal_single_successful_login_still_audited_as_before(self):
        username = f"o4d1_regress2_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.41")
        resp = client.post(
            ADMIN_LOGIN_URL,
            {"username": username, "password": "correct-password-123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)
        self.assertTrue(
            BrokerAuditEvent.objects.filter(event_type=_audit.EV_ADMIN_SITE_LOGIN_SUCCESS).exists()
        )

    def test_get_request_to_login_page_never_blocked(self):
        # The pre-check only applies to POST — a plain page view is
        # always allowed regardless of any prior block state.
        username = f"o4d1_regress3_{_unique_suffix()}"
        User.objects.create_user(username=username, password="correct-password-123", is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.42")
        for _ in range(9):
            client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        resp = client.get(ADMIN_LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
