"""
simulator/tests/test_audit04b_password_admin_trail.py — AUDIT-04b

Audits the Password & Admin Access audit trail (simulator/auth_password_
views.py + admin.py::MoneyBrokerAdminSite.login()): auth.password_changed,
auth.password_reset_requested, auth.password_reset_completed,
auth.admin_site_login_success, auth.admin_site_login_failed.

Conventions reused from test_password_reset.py: locmem email backend,
rate_check mocked, _get_confirm_url() extracting the real confirm link
from the outbox and following Django's session-redirect dance.

Scope: event shape, the sha256(token)-keyed get-or-create correlation_id
semantics (same token -> same id, different token -> different id),
Redis key cleanup after a successful reset, fail-open (including Redis
down during lookup/delete), anti-enumeration (no event + identical
response for an unknown email), admin login success/failure/non-staff,
and username_attempted normalization.
"""
import re
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from simulator.auth_password_views import _get_or_create_correlation, _token_key
from simulator.broker_audit import (
    ActorType,
    Category,
    EV_ADMIN_SITE_LOGIN_FAILED,
    EV_ADMIN_SITE_LOGIN_SUCCESS,
    EV_PASSWORD_CHANGED,
    EV_PASSWORD_RESET_COMPLETED,
    EV_PASSWORD_RESET_REQUESTED,
    Severity,
)
from simulator.models import BrokerAuditEvent
from simulator.tests.factories import make_user

_PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))

PASSWORD_CHANGE_URL = "/password-change/"
PASSWORD_RESET_URL  = "/password-reset/"
LOGIN_URL           = "/login/"
ADMIN_LOGIN_URL      = "/admin/login/"


def _get_confirm_url(email):
    mail.outbox.clear()
    from django.test import Client
    c = Client()
    c.post(PASSWORD_RESET_URL, {"email": email})
    assert len(mail.outbox) == 1, "expected exactly one reset email"
    match = re.search(r"(/password-reset/confirm/[^\s]+)", mail.outbox[0].body)
    assert match is not None, "no confirm URL found in email body"
    return match.group(1)


def _complete_reset(email, new_password="BrandNew!Pass99"):
    """Full HTTP round trip: request -> follow session redirect -> POST new password."""
    from django.test import Client
    c = Client()
    url = _get_confirm_url(email)
    resp = c.get(url, follow=True)
    confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url
    return c.post(confirm_url, {"new_password1": new_password, "new_password2": new_password})


# ─────────────────────────────────────────────────────────────────────────────
# password_changed
# ─────────────────────────────────────────────────────────────────────────────

class PasswordChangedAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a04b_change_user", password="OldPass!123")
        self.client.force_login(self.user)

    def _change(self):
        return self.client.post(PASSWORD_CHANGE_URL, {
            "old_password": "OldPass!123",
            "new_password1": "NewPass!456",
            "new_password2": "NewPass!456",
        })

    def test_creates_exactly_one_event(self):
        self._change()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_CHANGED, user=self.user).count(), 1,
        )

    def test_event_shape(self):
        self._change()
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_CHANGED, user=self.user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertEqual(event.actor_type, ActorType.TRADER)
        self.assertIsNone(event.actor_id)
        self.assertIsNone(event.correlation_id)

    def test_metadata_whitelist(self):
        self._change()
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_CHANGED, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"ip"})
        blob = str(event.metadata) + event.description
        self.assertNotIn("OldPass", blob)
        self.assertNotIn("NewPass", blob)

    def test_wrong_old_password_creates_no_event(self):
        self.client.post(PASSWORD_CHANGE_URL, {
            "old_password": "WRONG", "new_password1": "X!123456", "new_password2": "X!123456",
        })
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_CHANGED).count(), 0)

    def test_fail_open(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self._change()
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass!456"))


# ─────────────────────────────────────────────────────────────────────────────
# password_reset_requested — including anti-enumeration
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetRequestedAuditTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)

    def test_creates_exactly_one_event_for_matched_email(self):
        user = make_user(email="a04b_req@test.com")
        self.client.post(PASSWORD_RESET_URL, {"email": "a04b_req@test.com"})
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_RESET_REQUESTED, user=user).count(), 1,
        )

    def test_event_shape(self):
        user = make_user(email="a04b_req2@test.com")
        self.client.post(PASSWORD_RESET_URL, {"email": "a04b_req2@test.com"})
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_REQUESTED, user=user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertIsNotNone(event.correlation_id)

    def test_metadata_whitelist(self):
        user = make_user(email="a04b_req3@test.com")
        self.client.post(PASSWORD_RESET_URL, {"email": "a04b_req3@test.com"})
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_REQUESTED, user=user)
        self.assertEqual(set(event.metadata.keys()), {"ip"})

    def test_unknown_email_creates_no_event(self):
        resp = self.client.post(PASSWORD_RESET_URL, {"email": "nobody_a04b@test.com"})
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_RESET_REQUESTED).count(), 0)
        self.assertRedirects(resp, "/password-reset/done/", fetch_redirect_response=False)

    def test_unknown_email_response_identical_to_known_email(self):
        """The anti-enumeration guarantee: same status/redirect regardless of match."""
        make_user(email="a04b_known@test.com")
        resp_known   = self.client.post(PASSWORD_RESET_URL, {"email": "a04b_known@test.com"})
        resp_unknown = self.client.post(PASSWORD_RESET_URL, {"email": "a04b_unknown@test.com"})
        self.assertEqual(resp_known.status_code, resp_unknown.status_code)
        self.assertEqual(resp_known["Location"], resp_unknown["Location"])

    def test_fail_open_does_not_block_email_send(self):
        make_user(email="a04b_failopen@test.com")
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self.client.post(PASSWORD_RESET_URL, {"email": "a04b_failopen@test.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)


# ─────────────────────────────────────────────────────────────────────────────
# password_reset_completed
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetCompletedAuditTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)

    def test_creates_exactly_one_event(self):
        user = make_user(email="a04b_complete@test.com")
        _complete_reset("a04b_complete@test.com")
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_RESET_COMPLETED, user=user).count(), 1,
        )

    def test_event_shape(self):
        user = make_user(email="a04b_complete2@test.com")
        _complete_reset("a04b_complete2@test.com")
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_COMPLETED, user=user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertIsNotNone(event.correlation_id)

    def test_metadata_whitelist(self):
        user = make_user(email="a04b_complete3@test.com")
        _complete_reset("a04b_complete3@test.com")
        event = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_COMPLETED, user=user)
        self.assertEqual(set(event.metadata.keys()), {"ip"})

    def test_invalid_token_creates_no_event(self):
        """form_valid() never runs for an invalid/expired token — Django
        itself never reaches it — so nothing to record either."""
        from django.test import Client
        c = Client()
        c.post("/password-reset/confirm/bad-uid/bad-token/")
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_PASSWORD_RESET_COMPLETED).count(), 0)

    def test_fail_open_does_not_block_password_set(self):
        user = make_user(email="a04b_failopen2@test.com")
        from django.test import Client
        c = Client()
        url = _get_confirm_url("a04b_failopen2@test.com")
        resp = c.get(url, follow=True)
        confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            c.post(confirm_url, {"new_password1": "Boom!12345", "new_password2": "Boom!12345"})
        user.refresh_from_db()
        self.assertTrue(user.check_password("Boom!12345"))


# ─────────────────────────────────────────────────────────────────────────────
# correlation_id — sha256(token) get-or-create semantics
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationHelperTests(TestCase):
    """Direct unit tests of _get_or_create_correlation() — isolates the
    atomic semantics from Django's own token generation behavior."""

    def test_same_token_reuses_same_correlation_id(self):
        first  = _get_or_create_correlation("token-aaa")
        second = _get_or_create_correlation("token-aaa")
        self.assertEqual(first, second)

    def test_different_token_gets_different_correlation_id(self):
        first  = _get_or_create_correlation("token-bbb")
        second = _get_or_create_correlation("token-ccc")
        self.assertNotEqual(first, second)

    def test_redis_down_returns_a_valid_uuid_without_raising(self):
        with patch("simulator.auth_password_views._redis", side_effect=RuntimeError("redis down")):
            result = _get_or_create_correlation("token-ddd")
        self.assertIsNotNone(result)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CorrelationHttpLevelTests(TestCase):
    """
    Django's default_token_generator is deterministic — two reset requests
    for the same, unchanged user close together produce the IDENTICAL
    token. Two rapid requests here must therefore share one
    correlation_id, and the completed event must carry that same id.
    """
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)

    def test_two_rapid_requests_share_one_correlation_id(self):
        user = make_user(email="a04b_dup@test.com")
        self.client.post(PASSWORD_RESET_URL, {"email": "a04b_dup@test.com"})
        self.client.post(PASSWORD_RESET_URL, {"email": "a04b_dup@test.com"})

        events = list(
            BrokerAuditEvent.objects.filter(
                event_type=EV_PASSWORD_RESET_REQUESTED, user=user
            ).order_by("id")
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].correlation_id, events[1].correlation_id)

    def test_requested_and_completed_share_correlation_id(self):
        user = make_user(email="a04b_e2e@test.com")
        _complete_reset("a04b_e2e@test.com")

        requested = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_REQUESTED, user=user)
        completed = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_COMPLETED, user=user)
        self.assertEqual(requested.correlation_id, completed.correlation_id)


# ─────────────────────────────────────────────────────────────────────────────
# Redis key cleanup
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RedisCleanupTests(TestCase):
    def setUp(self):
        _PATCH_RATELIMIT.start()
        self.addCleanup(_PATCH_RATELIMIT.stop)

    def test_key_deleted_after_successful_completion(self):
        from simulator.auth_password_views import _redis
        make_user(email="a04b_cleanup@test.com")
        url = _get_confirm_url("a04b_cleanup@test.com")
        token = url.rstrip("/").split("/")[-1]

        from django.test import Client
        c = Client()
        resp = c.get(url, follow=True)
        confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url

        self.assertIsNotNone(_redis().get(_token_key(token)))  # present before completion
        c.post(confirm_url, {"new_password1": "Clean!12345", "new_password2": "Clean!12345"})
        self.assertIsNone(_redis().get(_token_key(token)))  # gone after

    def test_redis_down_during_lookup_and_delete_does_not_break_reset(self):
        user = make_user(email="a04b_rdown@test.com")
        url = _get_confirm_url("a04b_rdown@test.com")
        from django.test import Client
        c = Client()
        resp = c.get(url, follow=True)
        confirm_url = resp.redirect_chain[-1][0] if resp.redirect_chain else url

        with patch("simulator.auth_password_views._redis", side_effect=RuntimeError("redis down")):
            post_resp = c.post(confirm_url, {
                "new_password1": "RDown!12345", "new_password2": "RDown!12345",
            })
        self.assertEqual(post_resp.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password("RDown!12345"))
        # Fail-open contract: Redis down still yields a valid (if orphaned,
        # unpersisted) correlation_id — never None, never an exception.
        # It just won't match the "requested" event's id, since that one
        # WAS persisted to Redis before this patch took effect.
        completed = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_COMPLETED, user=user)
        self.assertIsNotNone(completed.correlation_id)
        requested = BrokerAuditEvent.objects.get(event_type=EV_PASSWORD_RESET_REQUESTED, user=user)
        self.assertNotEqual(requested.correlation_id, completed.correlation_id)


# ─────────────────────────────────────────────────────────────────────────────
# Admin site login
# ─────────────────────────────────────────────────────────────────────────────

class AdminSiteLoginAuditTests(TestCase):
    def setUp(self):
        self.staff = make_user(username="a04b_staff", password="StaffPass!1",
                                is_staff=True, is_superuser=True)
        self.non_staff = make_user(username="a04b_nonstaff", password="NonStaff!1")

    def test_successful_login_creates_event(self):
        self.client.post(ADMIN_LOGIN_URL, {
            "username": "a04b_staff", "password": "StaffPass!1", "next": "/admin/",
        })
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_ADMIN_SITE_LOGIN_SUCCESS, user=self.staff).count(), 1,
        )

    def test_successful_login_event_shape(self):
        self.client.post(ADMIN_LOGIN_URL, {
            "username": "a04b_staff", "password": "StaffPass!1", "next": "/admin/",
        })
        event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_SITE_LOGIN_SUCCESS, user=self.staff)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)  # AUDIT-04b correction #2
        self.assertEqual(event.actor_type, ActorType.STAFF)

    def test_wrong_password_creates_failed_event(self):
        self.client.post(ADMIN_LOGIN_URL, {"username": "a04b_staff", "password": "WRONG"})
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_ADMIN_SITE_LOGIN_FAILED).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_ADMIN_SITE_LOGIN_SUCCESS).count(), 0)

    def test_valid_credentials_non_staff_creates_failed_event(self):
        self.client.post(ADMIN_LOGIN_URL, {"username": "a04b_nonstaff", "password": "NonStaff!1"})
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_ADMIN_SITE_LOGIN_FAILED).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_ADMIN_SITE_LOGIN_SUCCESS).count(), 0)

    def test_failed_event_has_no_user_fk(self):
        self.client.post(ADMIN_LOGIN_URL, {"username": "a04b_staff", "password": "WRONG"})
        event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_SITE_LOGIN_FAILED)
        self.assertIsNone(event.user)
        self.assertIsNone(event.actor_id)

    def test_username_attempted_is_stripped(self):
        self.client.post(ADMIN_LOGIN_URL, {"username": "  padded_user  ", "password": "WRONG"})
        event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_SITE_LOGIN_FAILED)
        self.assertEqual(event.metadata["username_attempted"], "padded_user")

    def test_username_attempted_is_truncated_to_150(self):
        long_username = "u" * 500
        self.client.post(ADMIN_LOGIN_URL, {"username": long_username, "password": "WRONG"})
        event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_SITE_LOGIN_FAILED)
        self.assertEqual(len(event.metadata["username_attempted"]), 150)

    def test_failed_metadata_never_includes_password(self):
        self.client.post(ADMIN_LOGIN_URL, {"username": "a04b_staff", "password": "SuperSecretPW!"})
        event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_SITE_LOGIN_FAILED)
        blob = str(event.metadata) + event.description
        self.assertNotIn("SuperSecretPW", blob)
        self.assertEqual(set(event.metadata.keys()), {"username_attempted", "ip"})

    def test_already_authenticated_page_load_creates_no_event(self):
        self.client.force_login(self.staff)
        self.client.get("/admin/")
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.AUTHENTICATION).count(), 0)

    def test_fail_open(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self.client.post(ADMIN_LOGIN_URL, {
                "username": "a04b_staff", "password": "StaffPass!1", "next": "/admin/",
            })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
