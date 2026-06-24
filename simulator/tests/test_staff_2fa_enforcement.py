# simulator/tests/test_staff_2fa_enforcement.py
"""
Bloque J.3 — Staff 2FA Enforcement.

Covers:
  ops_panel_view (HTML — @staff_require_2fa decorator):
    1. Anonymous → redirect to login.
    2. Non-staff → redirect to login.
    3. Staff + TOTP_STAFF_REQUIRED=True + confirmed device + session not verified → redirect to totp_verify.
    4. Staff + TOTP_STAFF_REQUIRED=True + confirmed device + session verified → 200.
    5. Staff + TOTP_STAFF_REQUIRED=True + no device → 200 (no device = nothing to enforce).
    6. Staff + TOTP_STAFF_REQUIRED=False → 200 regardless of session.

  JSON API views (metrics, broker_monitoring, snapshots, health_detail — inline 2FA check):
    7. Anonymous → JSON 403 {"error": "forbidden"} (existing contract preserved).
    8. Non-staff → JSON 403 {"error": "forbidden"} (existing contract preserved).
    9. Staff + TOTP_STAFF_REQUIRED=True + confirmed device + session not verified → JSON 403 {"error": "2fa_required"}.
   10. Staff + TOTP_STAFF_REQUIRED=True + confirmed device + session verified → 200.
   11. Staff + TOTP_STAFF_REQUIRED=True + no device → 200 (pass-through, no device to check).
   12. Staff + TOTP_STAFF_REQUIRED=False → 200 (feature disabled, no block).
"""
import pyotp

from django.test import TestCase, override_settings
from django.urls import reverse

from simulator.models import TOTPDevice
from simulator.tests.factories import make_user

OPS_URL             = reverse("simulator:ops_panel")
LOGIN_URL           = reverse("simulator:login")
TOTP_VERIFY_URL     = reverse("simulator:totp_verify")
METRICS_URL         = reverse("simulator:metrics")
BROKER_MON_URL      = reverse("simulator:broker_monitoring")
SNAPSHOTS_URL       = reverse("simulator:broker_snapshots") + "?type=broker"
HEALTH_DETAIL_URL   = "/api/health/detail/"

_JSON_API_URLS = [METRICS_URL, BROKER_MON_URL, SNAPSHOTS_URL, HEALTH_DETAIL_URL]


def _make_totp_device(user, confirmed: bool = True) -> TOTPDevice:
    secret = pyotp.random_base32()
    return TOTPDevice.objects.create(
        user=user,
        secret=f"b64:{__import__('base64').b64encode(secret.encode()).decode()}",
        confirmed=confirmed,
    )


def _set_2fa_verified(client):
    session = client.session
    session["2fa_verified"] = True
    session.save()


# ── ops_panel_view — HTML view with @staff_require_2fa ────────────────────────

class OpsPanel2FATests(TestCase):
    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(OPS_URL)
        self.assertIn(resp.status_code, (301, 302))
        self.assertIn(LOGIN_URL, resp["Location"])

    def test_non_staff_redirects_to_login(self):
        user = make_user()
        self.client.force_login(user)
        resp = self.client.get(OPS_URL)
        self.assertIn(resp.status_code, (301, 302))
        self.assertIn(LOGIN_URL, resp["Location"])

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_with_device_and_no_session_redirects_to_verify(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        resp = self.client.get(OPS_URL)
        self.assertIn(resp.status_code, (301, 302))
        self.assertIn(TOTP_VERIFY_URL, resp["Location"])

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_with_device_and_session_verified_gets_200(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        _set_2fa_verified(self.client)
        resp = self.client.get(OPS_URL)
        self.assertEqual(resp.status_code, 200)

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_without_device_gets_200_when_totp_required(self):
        staff = make_user(is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(OPS_URL)
        self.assertEqual(resp.status_code, 200)

    @override_settings(TOTP_STAFF_REQUIRED=False)
    def test_totp_required_false_does_not_block_staff(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        resp = self.client.get(OPS_URL)
        self.assertEqual(resp.status_code, 200)


# ── JSON API views — inline 2FA enforcement ───────────────────────────────────

class JsonApi2FATests(TestCase):
    """
    Tests run against each JSON staff API. setUp populates self.url_list with
    all target URLs, and each test iterates over them so coverage is uniform.
    """

    def test_anonymous_returns_403_json(self):
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403, url)
                data = resp.json()
                self.assertEqual(data.get("error"), "forbidden")

    def test_non_staff_returns_403_json(self):
        user = make_user()
        self.client.force_login(user)
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403, url)
                data = resp.json()
                self.assertEqual(data.get("error"), "forbidden")

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_with_device_and_no_session_returns_2fa_required(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403, url)
                data = resp.json()
                self.assertEqual(data.get("error"), "2fa_required", url)

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_with_device_and_session_verified_passes(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        _set_2fa_verified(self.client)
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (200, 503), url)

    @override_settings(TOTP_STAFF_REQUIRED=True)
    def test_staff_without_device_not_blocked_when_totp_required(self):
        staff = make_user(is_staff=True)
        self.client.force_login(staff)
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (200, 503), url)

    @override_settings(TOTP_STAFF_REQUIRED=False)
    def test_totp_required_false_does_not_block_staff(self):
        staff = make_user(is_staff=True)
        _make_totp_device(staff, confirmed=True)
        self.client.force_login(staff)
        for url in _JSON_API_URLS:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (200, 503), url)
