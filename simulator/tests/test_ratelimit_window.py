# simulator/tests/test_ratelimit_window.py
"""
Fixed-window rate limit — correctness tests.

These tests hit real Redis (127.0.0.1:6379) to verify the behavior of the
rate_check function itself. They use isolated keys with a unique prefix so
they never collide with application traffic or other test runs.

Key invariants verified:
  1. First request creates a TTL on the counter key.
  2. Subsequent requests within the same window do NOT reset (extend) the TTL.
  3. Incrementing past the limit returns (False, count).
  4. allow/block logic is correct across a window.
  5. Fail-open: Redis unavailable → (True, 0) returned.
  6. LOAD_TEST_MODE=True → (True, 0) regardless of count.
"""
import time
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator.ratelimit import _RL_PREFIX, _get_rl_redis, rate_check

_TEST_NS = "test_rlw"


def _unique_key() -> str:
    """Return a test-scoped rate-limit key that will not collide with anything."""
    return f"{_TEST_NS}:{uuid.uuid4().hex[:12]}"


def _redis_key(app_key: str) -> str:
    return f"{_RL_PREFIX}{app_key}"


class RateLimitWindowTests(TestCase):
    """Verify TTL behavior of rate_check — the core of the fixed-window contract."""

    def setUp(self):
        self.r = _get_rl_redis()

    def tearDown(self):
        pattern = f"{_RL_PREFIX}{_TEST_NS}:*"
        keys = self.r.keys(pattern)
        if keys:
            self.r.delete(*keys)

    # ── 1. First request creates TTL ─────────────────────────────────────────

    def test_first_request_sets_ttl(self):
        key = _unique_key()
        rate_check(key, limit=10, window=60)
        ttl = self.r.ttl(_redis_key(key))
        self.assertGreater(ttl, 0, "TTL must be positive after the first request")
        self.assertLessEqual(ttl, 60, "TTL must not exceed the configured window")

    def test_first_request_counter_is_one(self):
        key = _unique_key()
        allowed, count = rate_check(key, limit=10, window=60)
        self.assertTrue(allowed)
        self.assertEqual(count, 1)

    # ── 2. Subsequent requests do NOT reset TTL ───────────────────────────────

    def test_second_request_does_not_reset_ttl(self):
        """
        Manually seed a counter at count=5 with a short TTL (30s), then call
        rate_check and confirm the TTL did not increase back toward the window.
        """
        key = _unique_key()
        rk = _redis_key(key)
        self.r.set(rk, 5)
        self.r.expire(rk, 30)

        ttl_before = self.r.ttl(rk)
        self.assertGreater(ttl_before, 0)

        rate_check(key, limit=10, window=60)

        ttl_after = self.r.ttl(rk)
        # TTL must not have grown (buggy code resets it to 60; fixed code leaves it ≤30)
        self.assertLessEqual(
            ttl_after, ttl_before + 1,
            f"TTL should not increase on subsequent requests: before={ttl_before} after={ttl_after}",
        )

    def test_many_requests_ttl_stays_stable(self):
        """Five rapid requests must not push the TTL higher than it was after the first."""
        key = _unique_key()
        rate_check(key, limit=10, window=60)
        ttl_after_first = self.r.ttl(_redis_key(key))

        for _ in range(4):
            rate_check(key, limit=10, window=60)

        ttl_final = self.r.ttl(_redis_key(key))
        self.assertLessEqual(
            ttl_final, ttl_after_first,
            "Repeated requests must not extend the window TTL",
        )

    # ── 3. Block/allow logic ──────────────────────────────────────────────────

    def test_requests_within_limit_are_allowed(self):
        key = _unique_key()
        for i in range(5):
            allowed, count = rate_check(key, limit=5, window=60)
            self.assertTrue(allowed, f"Request {i + 1} of 5 should be allowed")

    def test_request_exceeding_limit_is_blocked(self):
        key = _unique_key()
        for _ in range(5):
            rate_check(key, limit=5, window=60)
        allowed, count = rate_check(key, limit=5, window=60)
        self.assertFalse(allowed, "Request 6 of 5 must be blocked")
        self.assertEqual(count, 6)

    def test_exact_limit_request_is_allowed(self):
        key = _unique_key()
        for _ in range(4):
            rate_check(key, limit=5, window=60)
        allowed, count = rate_check(key, limit=5, window=60)
        self.assertTrue(allowed, "Request exactly at the limit must be allowed")
        self.assertEqual(count, 5)

    # ── 4. Window expiry resets counter ──────────────────────────────────────

    def test_expired_key_allows_fresh_window(self):
        """
        Seed a key at count=limit with a 1-second TTL; after expiry, a new
        request must be allowed (count resets to 1).
        """
        key = _unique_key()
        rk = _redis_key(key)
        self.r.set(rk, 5)
        self.r.expire(rk, 1)

        # Blocked immediately (count=6 > limit=5)
        allowed_before, _ = rate_check(key, limit=5, window=60)
        self.assertFalse(allowed_before)

        time.sleep(1.1)

        # Fresh window after TTL expired
        allowed_after, count_after = rate_check(key, limit=5, window=60)
        self.assertTrue(allowed_after, "Counter must reset after window expires")
        self.assertEqual(count_after, 1)

    # ── 5. Fail-open on Redis error ───────────────────────────────────────────

    def test_redis_failure_returns_fail_open(self):
        key = _unique_key()
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("Redis down")):
            allowed, count = rate_check(key, limit=5, window=60)
        self.assertTrue(allowed, "Fail-open: Redis unavailable must allow the request")
        self.assertEqual(count, 0)

    # ── 6. LOAD_TEST_MODE bypass ──────────────────────────────────────────────

    def test_load_test_mode_bypasses_limit(self):
        key = _unique_key()
        with override_settings(LOAD_TEST_MODE=True):
            for _ in range(20):
                allowed, count = rate_check(key, limit=3, window=60)
                self.assertTrue(allowed, "LOAD_TEST_MODE must bypass rate limiting")
                self.assertEqual(count, 0)


class RateLimitPatchCompatibilityTests(TestCase):
    """
    Confirm the patch path used by existing tests still resolves correctly.
    If this breaks, every test class that patches rate_check will fail.
    """

    def test_patch_path_resolves(self):
        with patch("simulator.ratelimit.rate_check", return_value=(True, 0)) as mock:
            from simulator.ratelimit import rate_check as patched
            result = patched("any_key", 5, 60)
            self.assertEqual(result, (True, 0))
            mock.assert_called_once()
