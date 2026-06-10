# simulator/tests/test_health_check.py
"""
Health check endpoint hardening — regression tests.

Public endpoint /api/health/:
  - Anonymous access allowed (liveness probe for load balancers).
  - Returns only {"status": "ok"} — no internal subsystem details.

Staff-only endpoint /api/health/detail/:
  - Anonymous → 403.
  - Authenticated non-staff → 403.
  - Staff → 200 with subsystem details (db, redis, channel_layer).
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.tests.factories import make_user

User = get_user_model()

HEALTH_URL        = "/api/health/"
HEALTH_DETAIL_URL = "/api/health/detail/"

_FORBIDDEN_KEYS = {"db", "redis", "celery", "channel_layer", "database",
                   "version", "debug", "hostname", "detail", "ms"}


class PublicHealthCheckTests(TestCase):
    """Anyone can reach /api/health/ and the response leaks nothing internal."""

    def test_anonymous_can_access_health(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_status_ok(self):
        resp = self.client.get(HEALTH_URL)
        data = resp.json()
        self.assertEqual(data.get("status"), "ok")

    def test_health_response_contains_only_status(self):
        resp = self.client.get(HEALTH_URL)
        data = resp.json()
        self.assertEqual(set(data.keys()), {"status"},
                         f"Public health must only expose 'status', got: {set(data.keys())}")

    def test_health_contains_no_internal_keys(self):
        resp = self.client.get(HEALTH_URL)
        data = resp.json()
        leaked = set(data.keys()) & _FORBIDDEN_KEYS
        self.assertFalse(leaked, f"Public health leaks internal keys: {leaked}")

    def test_health_body_has_no_db_string(self):
        resp = self.client.get(HEALTH_URL)
        body = resp.content.decode()
        for word in ("\"db\"", "\"redis\"", "\"celery\"", "\"channel_layer\"",
                     "\"database\"", "\"version\"", "\"debug\""):
            self.assertNotIn(word, body,
                             f"Public health body must not contain {word!r}")


class HealthDetailAnonTests(TestCase):
    """Anonymous and non-staff users must not reach /api/health/detail/."""

    def test_anonymous_get_returns_403(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_returns_403(self):
        resp = self.client.post(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_user_returns_403(self):
        user = make_user(username="normal_health_user")
        self.client.force_login(user)
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_response_body(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"], "forbidden")


class HealthDetailStaffTests(TestCase):
    """Staff users must get subsystem details from /api/health/detail/."""

    def setUp(self):
        self.staff = make_user(username="staff_health_user")
        self.staff.is_staff = True
        self.staff.save()
        self.client.force_login(self.staff)

    def test_staff_can_access_health_detail(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        self.assertIn(resp.status_code, (200, 503))

    def test_staff_response_contains_status_key(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn(data["status"], ("ok", "degraded"))

    def test_staff_response_contains_db_key(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("db", data, "Health detail must include 'db' check for staff")

    def test_staff_response_contains_redis_key(self):
        resp = self.client.get(HEALTH_DETAIL_URL)
        data = resp.json()
        self.assertIn("redis", data, "Health detail must include 'redis' check for staff")
