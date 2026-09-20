# simulator/tests/test_ib_attribution_foundation_01.py
"""
IB-ATTRIBUTION-FOUNDATION-01

Covers:
  1.  A valid click sets a resolvable session token.
  2.  A valid click sets a resolvable 30-day cookie.
  3.  A second click (different code) while a valid attribution already
      exists does not override it — first-valid-referral-wins.
  4.  An expired token resolves to no attribution.
  5.  A tampered/garbage cookie value resolves to no attribution.
  6.  Registration with a valid referral signal creates exactly one
      ReferralAttribution, correctly linked, with the right source.
  7.  Registration with no referral signal creates no attribution and
      does not error.
  8.  Registration when the referral code no longer exists creates no
      attribution and does not error.
  9.  A forced duplicate attribution attempt is caught (IntegrityError)
      and does not raise — exactly one row survives.
  10. Self-referral is ignored.
  11. referral.attributions.count() reflects real rows — not the frozen
      Referral.registrations field.
  12. ReferralAttributionAdmin is fully read-only (no add/change/delete).
  13. End-to-end via the real views: GET /ref/<code>/ then POST
      /register/ with the client's carried cookie creates the correct
      attribution; a second /ref/<other>/ click first does not steal it.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.models import Referral, ReferralAttribution
from simulator.referral_attribution import (
    _COOKIE_NAME, _SESSION_KEY, _make_token, attribute_user,
    record_referral_click, resolve_active_referral_code,
)
from simulator.tests.factories import make_user

User = get_user_model()

_PATCH_RATELIMIT   = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
_PATCH_EMAIL_ASYNC = patch("simulator.tasks.send_email_async.delay")


def _make_referral(owner=None, code="testref1"):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=code)


class ResolveAndClickTests(TestCase):
    """Unit-level coverage of the signed-token session/cookie mechanics."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request_with_session(self, cookies=None):
        req = self.factory.get("/")
        # Attach a real session store (RequestFactory doesn't add one).
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req.COOKIES = cookies or {}
        return req

    def test_click_sets_session_token(self):
        referral = _make_referral()
        req = self._request_with_session()
        from django.http import HttpResponse
        resp = HttpResponse()
        record_referral_click(req, resp, referral.code)
        self.assertIn(_SESSION_KEY, req.session)

    def test_click_sets_cookie_token(self):
        referral = _make_referral()
        req = self._request_with_session()
        from django.http import HttpResponse
        resp = HttpResponse()
        record_referral_click(req, resp, referral.code)
        self.assertIn(_COOKIE_NAME, resp.cookies)
        self.assertEqual(resp.cookies[_COOKIE_NAME]["max-age"], 30 * 24 * 3600)
        self.assertTrue(resp.cookies[_COOKIE_NAME]["httponly"])

    def test_session_token_resolves_to_code(self):
        referral = _make_referral()
        req = self._request_with_session()
        req.session[_SESSION_KEY] = _make_token(referral.code)
        code, source = resolve_active_referral_code(req)
        self.assertEqual(code, referral.code)
        self.assertEqual(source, "session")

    def test_cookie_token_resolves_to_code(self):
        referral = _make_referral()
        req = self._request_with_session(cookies={_COOKIE_NAME: _make_token(referral.code)})
        code, source = resolve_active_referral_code(req)
        self.assertEqual(code, referral.code)
        self.assertEqual(source, "cookie")

    def test_second_click_does_not_override_existing_attribution(self):
        """First-valid-referral-wins."""
        ref_a = _make_referral(code="reffirst")
        ref_b = _make_referral(code="refsecond")
        req = self._request_with_session()
        from django.http import HttpResponse

        resp1 = HttpResponse()
        record_referral_click(req, resp1, ref_a.code)
        code_after_first, _ = resolve_active_referral_code(req)
        self.assertEqual(code_after_first, ref_a.code)

        resp2 = HttpResponse()
        record_referral_click(req, resp2, ref_b.code)
        code_after_second, _ = resolve_active_referral_code(req)
        self.assertEqual(code_after_second, ref_a.code, "first click must still win")
        self.assertNotIn(_COOKIE_NAME, resp2.cookies, "no new cookie written on the second click")

    def test_expired_token_resolves_to_none(self):
        referral = _make_referral()
        token = _make_token(referral.code)
        req = self._request_with_session(cookies={_COOKIE_NAME: token})
        with patch("simulator.referral_attribution._MAX_AGE", -1):
            code, source = resolve_active_referral_code(req)
        self.assertIsNone(code)
        self.assertIsNone(source)

    def test_tampered_cookie_resolves_to_none(self):
        req = self._request_with_session(cookies={_COOKIE_NAME: "not-a-valid-signed-token"})
        code, source = resolve_active_referral_code(req)
        self.assertIsNone(code)
        self.assertIsNone(source)

    def test_wrong_salt_resolves_to_none(self):
        """A validly-signed token for a different purpose must not be
        accepted as a referral token — salts must be distinct."""
        forged = signing.dumps("someref", salt="some-other-purpose")
        req = self._request_with_session(cookies={_COOKIE_NAME: forged})
        code, source = resolve_active_referral_code(req)
        self.assertIsNone(code)
        self.assertIsNone(source)


class AttributeUserTests(TestCase):
    """attribute_user() — the registration-time binding."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request_with_token(self, code):
        req = self.factory.get("/")
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req.session[_SESSION_KEY] = _make_token(code)
        req.COOKIES = {}
        return req

    def test_valid_signal_creates_attribution(self):
        referral = _make_referral()
        new_user = make_user()
        req = self._request_with_token(referral.code)

        result = attribute_user(new_user, req)

        self.assertIsNotNone(result)
        self.assertEqual(ReferralAttribution.objects.count(), 1)
        attr = ReferralAttribution.objects.get()
        self.assertEqual(attr.referred_user_id, new_user.pk)
        self.assertEqual(attr.referral_id, referral.pk)
        self.assertEqual(attr.source, "session")

    def test_no_signal_creates_no_attribution(self):
        new_user = make_user()
        req = self.factory.get("/")
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req.COOKIES = {}

        result = attribute_user(new_user, req)

        self.assertIsNone(result)
        self.assertEqual(ReferralAttribution.objects.count(), 0)

    def test_nonexistent_referral_code_creates_no_attribution(self):
        new_user = make_user()
        req = self._request_with_token("code-that-does-not-exist")

        result = attribute_user(new_user, req)

        self.assertIsNone(result)
        self.assertEqual(ReferralAttribution.objects.count(), 0)

    def test_duplicate_attribution_is_caught_and_ignored(self):
        referral = _make_referral()
        new_user = make_user()
        req = self._request_with_token(referral.code)

        first = attribute_user(new_user, req)
        self.assertIsNotNone(first)

        # Force a second, duplicate attempt for the same user — must hit
        # the OneToOneField IntegrityError path and be swallowed, not
        # raise and not create a second row.
        second = attribute_user(new_user, req)

        self.assertIsNone(second)
        self.assertEqual(ReferralAttribution.objects.count(), 1)

    def test_self_referral_is_ignored(self):
        owner = make_user()
        referral = _make_referral(owner=owner)
        req = self._request_with_token(referral.code)

        result = attribute_user(owner, req)

        self.assertIsNone(result)
        self.assertEqual(ReferralAttribution.objects.count(), 0)

    def test_registrations_count_reflects_real_rows_not_frozen_field(self):
        referral = _make_referral()
        self.assertEqual(referral.registrations, 0)  # frozen field, untouched
        self.assertEqual(referral.attributions.count(), 0)

        u1, u2 = make_user(), make_user()
        attribute_user(u1, self._request_with_token(referral.code))
        attribute_user(u2, self._request_with_token(referral.code))

        referral.refresh_from_db()
        self.assertEqual(referral.registrations, 0, "frozen field must remain untouched")
        self.assertEqual(referral.attributions.count(), 2, "real count must reflect both attributions")


class ReferralAttributionAdminPermissionsTests(TestCase):
    def test_admin_is_fully_read_only(self):
        from django.contrib import admin as django_admin

        from simulator.admin import ReferralAttributionAdmin

        model_admin = ReferralAttributionAdmin(ReferralAttribution, django_admin.site)
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))


class EndToEndViewTests(TestCase):
    """Full round trip through the real views: GET /ref/<code>/ then POST
    /register/, using a single Client so cookies carry over naturally."""

    def setUp(self):
        _PATCH_RATELIMIT.start()
        _PATCH_EMAIL_ASYNC.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)
        self.addCleanup(_PATCH_EMAIL_ASYNC.stop)
        self.client = Client()

    def test_click_then_register_creates_attribution(self):
        referral = _make_referral(code="e2eref1")
        click_url = reverse("simulator:referral_click", args=[referral.code])

        resp = self.client.get(click_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(_COOKIE_NAME, resp.cookies)

        reg_resp = self.client.post(reverse("simulator:register"), {
            "username": "e2e_attributed_user",
            "email": "e2e_attributed@example.com",
            "password1": "E2eTest!Pass1",
            "password2": "E2eTest!Pass1",
        })
        self.assertEqual(reg_resp.status_code, 302)

        user = User.objects.get(username="e2e_attributed_user")
        attr = ReferralAttribution.objects.get(referred_user=user)
        self.assertEqual(attr.referral_id, referral.pk)
        # Both session and cookie are set by the click and both survive
        # this short same-client round trip — session is checked first
        # (cheaper), so it wins here. The cookie-only scenario (session
        # expired, cookie still valid) is covered precisely by
        # ResolveAndClickTests.test_cookie_token_resolves_to_code.
        self.assertEqual(attr.source, "session")

        referral.refresh_from_db()
        self.assertEqual(referral.attributions.count(), 1)

    def test_second_click_before_registration_does_not_steal_attribution(self):
        ref_a = _make_referral(code="e2ereffirst")
        ref_b = _make_referral(code="e2erefsecond")

        self.client.get(reverse("simulator:referral_click", args=[ref_a.code]))
        self.client.get(reverse("simulator:referral_click", args=[ref_b.code]))

        reg_resp = self.client.post(reverse("simulator:register"), {
            "username": "e2e_firstwins_user",
            "email": "e2e_firstwins@example.com",
            "password1": "E2eTest!Pass1",
            "password2": "E2eTest!Pass1",
        })
        self.assertEqual(reg_resp.status_code, 302)

        user = User.objects.get(username="e2e_firstwins_user")
        attr = ReferralAttribution.objects.get(referred_user=user)
        self.assertEqual(attr.referral_id, ref_a.pk, "first click must win, not the second")

    def test_registration_without_any_click_creates_no_attribution(self):
        reg_resp = self.client.post(reverse("simulator:register"), {
            "username": "e2e_no_referral_user",
            "email": "e2e_no_referral@example.com",
            "password1": "E2eTest!Pass1",
            "password2": "E2eTest!Pass1",
        })
        self.assertEqual(reg_resp.status_code, 302)
        user = User.objects.get(username="e2e_no_referral_user")
        self.assertFalse(ReferralAttribution.objects.filter(referred_user=user).exists())

    def test_click_on_invalid_code_does_not_set_cookie(self):
        resp = self.client.get(reverse("simulator:referral_click", args=["not-a-real-code"]))
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(_COOKIE_NAME, resp.cookies)
