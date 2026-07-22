"""
simulator/tests/test_audit04a_2fa_trail.py — AUDIT-04a

Audits the 2FA Lifecycle audit trail (Category.AUTHENTICATION, first real
call sites since AUDIT-01): auth.2fa_enabled, auth.2fa_disabled,
auth.2fa_disabled_emergency, auth.2fa_verify_failed.

Conventions reused from test_withdraw_2fa.py: pyotp for deterministic TOTP
codes, b64:-prefixed secret (dev fallback — no TOTP_ENCRYPTION_KEY in test
settings), rate_check mocked explicitly (never rely on real Redis).

Scope: event shape, privacy whitelist, fail-open, locking/no-duplication
(self-service disable vs. emergency command cross-flow race), and the
threshold-gated verify_failed event (never one row per brute-force guess).
"""
import base64
from unittest.mock import patch

import pyotp
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from simulator.broker_audit import (
    ActorType,
    AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD,
    AUDIT04A_2FA_VERIFY_FAIL_WINDOW_SECONDS,
    Category,
    EV_2FA_DISABLED,
    EV_2FA_DISABLED_EMERGENCY,
    EV_2FA_ENABLED,
    EV_2FA_VERIFY_FAILED,
    Severity,
    events_for_user,
)
from simulator.models import BrokerAuditEvent, TOTPDevice
from simulator.tests.factories import make_user

TOTP_SETUP_URL  = "/account/2fa/setup/"
TOTP_VERIFY_URL = "/account/2fa/verify/"
TOTP_DISABLE_URL = "/account/2fa/disable/"

_RAW_SECRET = pyotp.random_base32()
_B64_SECRET = "b64:" + base64.b64encode(_RAW_SECRET.encode()).decode()


def _valid_code(raw_secret: str = _RAW_SECRET) -> str:
    return pyotp.TOTP(raw_secret).now()


def _make_confirmed_device(user, secret: str = _B64_SECRET) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user, secret=secret, confirmed=True, confirmed_at=timezone.now(),
    )


def _assert_no_forbidden_data(test, event):
    blob = str(event.metadata) + event.description
    for forbidden in (_RAW_SECRET, _B64_SECRET, "otpauth://"):
        test.assertNotIn(forbidden, blob)


# ─────────────────────────────────────────────────────────────────────────────
# Enable
# ─────────────────────────────────────────────────────────────────────────────

class TOTPEnableAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a04a_enable_user")
        self.client.force_login(self.user)

    def _post_setup(self, secret=_RAW_SECRET):
        return self.client.post(TOTP_SETUP_URL, {
            "secret": secret, "code": _valid_code(secret),
        })

    def test_creates_exactly_one_event(self):
        self._post_setup()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_2FA_ENABLED, user=self.user).count(), 1,
        )

    def test_event_shape(self):
        self._post_setup()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_ENABLED, user=self.user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.INFO)
        self.assertEqual(event.actor_type, ActorType.TRADER)
        self.assertIsNone(event.actor_id)  # self-service
        self.assertEqual(event.event_version, 1)
        self.assertIsNone(event.correlation_id)

    def test_fresh_enable_had_existing_device_false(self):
        self._post_setup()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_ENABLED, user=self.user)
        self.assertFalse(event.metadata["had_existing_device"])
        self.assertIsNone(event.metadata["previous_confirmed_at"])

    def test_reenable_captures_previous_confirmed_at(self):
        existing = _make_confirmed_device(self.user)
        new_raw_secret = pyotp.random_base32()
        self._post_setup(secret=new_raw_secret)

        events = list(BrokerAuditEvent.objects.filter(event_type=EV_2FA_ENABLED, user=self.user))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].metadata["had_existing_device"])
        self.assertEqual(
            events[0].metadata["previous_confirmed_at"], existing.confirmed_at.isoformat(),
        )

    def test_metadata_whitelist(self):
        self._post_setup()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_ENABLED, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"had_existing_device", "previous_confirmed_at"})
        _assert_no_forbidden_data(self, event)

    def test_fail_open_does_not_block_enable(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self._post_setup()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOTPDevice.objects.filter(user=self.user, confirmed=True).exists()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Disable (self-service)
# ─────────────────────────────────────────────────────────────────────────────

class TOTPDisableAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a04a_disable_user")
        self.client.force_login(self.user)
        self.device = _make_confirmed_device(self.user)

    def _post_disable(self, code=None):
        return self.client.post(TOTP_DISABLE_URL, {"code": code or _valid_code()})

    def test_creates_exactly_one_event(self):
        self._post_disable()
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED, user=self.user).count(), 1,
        )

    def test_event_shape(self):
        self._post_disable()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_DISABLED, user=self.user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertEqual(event.actor_type, ActorType.TRADER)
        self.assertIsNone(event.actor_id)

    def test_was_confirmed_at_captured(self):
        confirmed_at = self.device.confirmed_at
        self._post_disable()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_DISABLED, user=self.user)
        self.assertEqual(event.metadata["was_confirmed_at"], confirmed_at.isoformat())

    def test_device_actually_deleted(self):
        self._post_disable()
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_metadata_whitelist(self):
        self._post_disable()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_DISABLED, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"was_confirmed_at"})
        _assert_no_forbidden_data(self, event)

    def test_wrong_code_does_not_disable_or_create_event(self):
        self._post_disable(code="000000")
        self.assertTrue(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED).count(), 0)

    def test_fail_open_does_not_block_disable(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self._post_disable()
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_recheck_skips_when_already_deleted(self):
        """
        Deterministic simulation of the exact race the lock closes: by the
        time the view's own select_for_update() runs, another process (the
        emergency command, or a second concurrent disable) already deleted
        the row. No exception, no event, harmless redirect.
        """
        TOTPDevice.objects.filter(user=self.user).delete()  # "another process" already won
        resp = self._post_disable()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED).count(), 0)

    def test_cross_flow_race_with_emergency_command(self):
        """approve_kyc-vs-reject_kyc-style cross-flow race, here between
        self-service disable and the emergency command — whichever wins
        leaves exactly one event, never two."""
        call_command("disable_2fa", self.user.username, "--confirm")  # wins the race
        resp = self._post_disable()  # loses — device already gone
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED).count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED_EMERGENCY).count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Disable — emergency (management command)
# ─────────────────────────────────────────────────────────────────────────────

class TOTPDisableEmergencyAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a04a_emergency_user")
        self.device = _make_confirmed_device(self.user)

    def test_creates_exactly_one_event(self):
        call_command("disable_2fa", self.user.username, "--confirm")
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=EV_2FA_DISABLED_EMERGENCY, user=self.user
            ).count(), 1,
        )

    def test_event_shape(self):
        call_command("disable_2fa", self.user.username, "--confirm")
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_DISABLED_EMERGENCY, user=self.user)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.CRITICAL)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)
        self.assertIsNone(event.actor_id)

    def test_metadata_whitelist(self):
        call_command("disable_2fa", self.user.username, "--confirm")
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_DISABLED_EMERGENCY, user=self.user)
        self.assertEqual(set(event.metadata.keys()), {"was_confirmed_at", "performed_by"})
        self.assertEqual(event.metadata["performed_by"], "management_command")
        _assert_no_forbidden_data(self, event)

    def test_no_device_creates_no_event(self):
        TOTPDevice.objects.filter(user=self.user).delete()
        call_command("disable_2fa", self.user.username, "--confirm")
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED_EMERGENCY).count(), 0)

    def test_without_confirm_flag_creates_no_event(self):
        call_command("disable_2fa", self.user.username)  # no --confirm
        self.assertTrue(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED_EMERGENCY).count(), 0)

    def test_cross_flow_race_with_self_service_disable(self):
        self.client.force_login(self.user)
        self.client.post(TOTP_DISABLE_URL, {"code": _valid_code()})  # wins
        call_command("disable_2fa", self.user.username, "--confirm")  # loses — nothing to do

        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_DISABLED_EMERGENCY).count(), 0)

    def test_fail_open_does_not_block_command(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            call_command("disable_2fa", self.user.username, "--confirm")
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())


# ─────────────────────────────────────────────────────────────────────────────
# Verify failed — threshold-gated, never one row per attempt
# ─────────────────────────────────────────────────────────────────────────────

class TOTPVerifyFailedAuditTests(TestCase):
    def setUp(self):
        self.user = make_user(username="a04a_verify_user")
        self.client.force_login(self.user)
        self.device = _make_confirmed_device(self.user)

    def _post_wrong_code(self):
        return self.client.post(TOTP_VERIFY_URL, {"code": "000000"})

    @patch("simulator.ratelimit.rate_check")
    def test_below_threshold_creates_no_event(self, mock_rate_check):
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD - 1)
        self._post_wrong_code()
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_VERIFY_FAILED).count(), 0)

    @patch("simulator.ratelimit.rate_check")
    def test_at_threshold_creates_exactly_one_event(self, mock_rate_check):
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD)
        self._post_wrong_code()
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_VERIFY_FAILED).count(), 1)

    @patch("simulator.ratelimit.rate_check")
    def test_past_threshold_creates_no_additional_event(self, mock_rate_check):
        """Only the exact crossing point fires — not every attempt after it."""
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD + 3)
        self._post_wrong_code()
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_VERIFY_FAILED).count(), 0)

    @patch("simulator.ratelimit.rate_check")
    def test_full_sequence_fires_exactly_once(self, mock_rate_check):
        counts = list(range(1, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD + 3))
        mock_rate_check.side_effect = [(True, c) for c in counts]
        for _ in counts:
            self._post_wrong_code()
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_VERIFY_FAILED).count(), 1)

    @patch("simulator.ratelimit.rate_check")
    def test_event_shape(self, mock_rate_check):
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD)
        self._post_wrong_code()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_VERIFY_FAILED)
        self.assertEqual(event.category, Category.AUTHENTICATION)
        self.assertEqual(event.severity, Severity.WARNING)
        self.assertEqual(event.actor_type, ActorType.TRADER)
        self.assertEqual(event.user_id, self.user.pk)
        self.assertIsNone(event.actor_id)

    @patch("simulator.ratelimit.rate_check")
    def test_metadata_whitelist(self, mock_rate_check):
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD)
        self._post_wrong_code()
        event = BrokerAuditEvent.objects.get(event_type=EV_2FA_VERIFY_FAILED)
        self.assertEqual(set(event.metadata.keys()), {"consecutive_failures", "window_seconds"})
        self.assertEqual(event.metadata["consecutive_failures"], AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD)
        self.assertEqual(event.metadata["window_seconds"], AUDIT04A_2FA_VERIFY_FAIL_WINDOW_SECONDS)
        _assert_no_forbidden_data(self, event)
        self.assertNotIn("000000", str(event.metadata) + event.description)

    @patch("simulator.ratelimit.rate_check")
    def test_redis_down_fail_open_never_fires(self, mock_rate_check):
        """
        rate_check() already fail-opens internally on a real Redis failure,
        returning (True, 0) (see ratelimit.py) — never raising. This test
        simulates exactly that documented return value: the threshold
        simply never crosses, no event is created, and the view still
        renders normally (no crash, no new behavior introduced by
        AUDIT-04a on top of ratelimit.py's existing fail-open contract).
        """
        mock_rate_check.return_value = (True, 0)
        resp = self._post_wrong_code()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BrokerAuditEvent.objects.filter(event_type=EV_2FA_VERIFY_FAILED).count(), 0)

    @patch("simulator.ratelimit.rate_check")
    def test_audit_fail_open_does_not_block_view(self, mock_rate_check):
        mock_rate_check.return_value = (True, AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD)
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            resp = self._post_wrong_code()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Código incorrecto")


# ─────────────────────────────────────────────────────────────────────────────
# events_for_user() — reused from AUDIT-03, confirm it works across categories
# ─────────────────────────────────────────────────────────────────────────────

class EventsForUserCrossCategoryTests(TestCase):
    def test_returns_authentication_events_alongside_others(self):
        user = make_user(username="a04a_efu_user")
        _make_confirmed_device(user)
        from simulator import broker_audit as _audit
        _audit.record_auth_event(
            event_type=EV_2FA_ENABLED, user=user, description="test",
            metadata={"had_existing_device": False, "previous_confirmed_at": None},
        )
        found = events_for_user(user.pk)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].category, Category.AUTHENTICATION)
