# simulator/tests/test_o4d3_admin_totp_bruteforce_e2e.py
"""
Microbloque O.4d-3 — Admin/TOTP Anti-Brute-Force End-to-End Verification
(closes HIGH-3, final checkpoint before commit/tag).

Pure verification: no production code is expected to change here. Every
test drives real URLs via the Django test Client against real Redis and
the real TOTP verification path (pyotp) — no internal helper is
patched for the "happy path" assertions, only for the explicit Redis-
down/LOAD_TEST_MODE scenarios in section 5.

Reuses exactly the fixtures/helpers established in
test_o4a3_treasury_admin_2fa_end_to_end.py (Treasury role grants,
TOTPDevice creation, the 9 Treasury admin URLs) — no new Treasury
service function, model, migration, or permission is introduced.
"""
import base64
import uuid
from decimal import Decimal
from unittest.mock import patch

import pyotp
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator import broker_audit as _audit
from simulator.models import (
    BrokerAuditEvent, InternalTransfer, TOTPDevice, TreasuryOperationRequest,
    Wallet, WalletTransaction,
)
from simulator.ratelimit import _get_rl_redis, _RL_PREFIX

from .factories import make_user, make_wallet

TREASURY_PERMISSIONS = (
    "can_submit_treasury_request",
    "can_review_treasury_request",
    "can_execute_treasury_request",
    "can_recover_treasury_execution",
)

ADMIN_LOGIN_URL = reverse("admin:login")
ADMIN_INDEX_URL = reverse("admin:index")
ADMIN_LOGOUT_URL = reverse("admin:logout")
TOTP_SETUP_URL = reverse("simulator:totp_setup")
TOTP_VERIFY_URL = reverse("simulator:totp_verify")
CHANGELIST_URL = reverse("admin:simulator_treasuryoperationrequest_changelist")


def _change_url(pk):
    return reverse("admin:simulator_treasuryoperationrequest_change", args=[pk])


def _approve_url(pk):
    return reverse("admin:treasury_request_approve", args=[pk])


def _execute_url(pk):
    return reverse("admin:treasury_request_execute", args=[pk])


def _recover_url(pk):
    return reverse("admin:treasury_request_recover", args=[pk])


def _cancel_url(pk):
    return reverse("admin:treasury_request_cancel", args=[pk])


DASHBOARD_URL = reverse("admin:treasury_operational_dashboard")


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _make_totp_device(user, confirmed=True, raw_secret=None):
    raw_secret = raw_secret or pyotp.random_base32()
    return TOTPDevice.objects.create(
        user=user, secret=f"b64:{base64.b64encode(raw_secret.encode()).decode()}",
        confirmed=confirmed,
    )


def _valid_code(raw_secret):
    return pyotp.TOTP(raw_secret).now()


def _make_pending_request(wallet, requested_by, **overrides):
    data = {
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "wallet": wallet, "amount": Decimal("25.00"),
        "reason": "O.4d-3 end-to-end test", "requested_by": requested_by,
    }
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _RedisCleanupMixin:
    def tearDown(self):
        r = _get_rl_redis()
        keys = r.keys(f"{_RL_PREFIX}admin_login_fail:*") + r.keys(f"{_RL_PREFIX}2fa_verify_fail:*")
        if keys:
            r.delete(*keys)
        super().tearDown()


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1 — Admin login E2E matrix
# ═══════════════════════════════════════════════════════════════════════

class AdminLoginE2EMatrixTests(_RedisCleanupMixin, TestCase):

    def setUp(self):
        self.username = _unique("o4d3_login")
        self.user = make_user(username=self.username, is_staff=True)

    def _distinct_username(self, i):
        return f"{self.username}_nope_{i}"

    # A. IP limit
    def test_ip_limit_full_matrix(self):
        client = Client(REMOTE_ADDR="203.0.113.11")
        for i in range(8):
            resp = client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
            self.assertEqual(resp.status_code, 200, f"attempt {i + 1} must be processed normally")
        resp = client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(8), "password": "wrong"})
        self.assertEqual(resp.status_code, 429, "9th attempt from same IP must be blocked")

        r = _get_rl_redis()
        key = f"{_RL_PREFIX}admin_login_fail:ip:203.0.113.11"
        count_at_block = int(r.get(key) or 0)
        for _ in range(5):  # hammer while blocked
            client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(99), "password": "wrong"})
        self.assertEqual(int(r.get(key) or 0), count_at_block, "counter frozen while blocked")

        # Blocked request must never reach authenticate(): a CORRECT
        # password for a real user, from the blocked IP, must still 429.
        resp = client.post(
            ADMIN_LOGIN_URL, {"username": self.username, "password": "testpass123"},
        )
        self.assertEqual(resp.status_code, 429)

    # B. Username limit — distributed credential stuffing
    def test_username_limit_distributed_credential_stuffing(self):
        for i in range(5):
            client = Client(REMOTE_ADDR=f"198.51.100.{i}")
            resp = client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
            self.assertEqual(resp.status_code, 200)
        attacker = Client(REMOTE_ADDR="198.51.100.250")
        resp = attacker.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        self.assertEqual(resp.status_code, 429)

    # C. Rotation cannot evade either dimension
    def test_ip_rotation_does_not_evade_username_limit(self):
        for i in range(5):
            client = Client(REMOTE_ADDR=f"198.51.100.{20 + i}")
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        fresh_ip_client = Client(REMOTE_ADDR="198.51.100.99")
        resp = fresh_ip_client.post(
            ADMIN_LOGIN_URL, {"username": self.username, "password": "testpass123"},
        )
        self.assertEqual(resp.status_code, 429, "username limit must hold even from a never-seen-before IP")

    def test_username_rotation_does_not_evade_ip_limit(self):
        client = Client(REMOTE_ADDR="203.0.113.55")
        for i in range(8):
            client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
        resp = client.post(
            ADMIN_LOGIN_URL, {"username": self._distinct_username(999), "password": "wrong"},
        )
        self.assertEqual(resp.status_code, 429, "IP limit must hold even for a never-seen-before username")

    # D. Correct login behavior
    def test_correct_login_never_increments_and_survives_partial_failures(self):
        client = Client(REMOTE_ADDR="203.0.113.66")
        for _ in range(3):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        resp = client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "testpass123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)

    def test_correct_login_works_after_window_expiry(self):
        client = Client(REMOTE_ADDR="203.0.113.77")
        for i in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        resp = client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "wrong"})
        self.assertEqual(resp.status_code, 429)

        r = _get_rl_redis()
        r.delete(f"{_RL_PREFIX}admin_login_fail:user:{self.username.lower()}")

        resp = client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "testpass123", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)

    # E. Information disclosure
    def test_429_response_reveals_nothing(self):
        superuser = make_user(username=_unique("o4d3_su"), is_staff=True, is_superuser=True)
        _grant_all = superuser  # just for readability
        client = Client(REMOTE_ADDR="203.0.113.88")
        for i in range(8):
            client.post(ADMIN_LOGIN_URL, {"username": self._distinct_username(i), "password": "wrong"})
        resp = client.post(
            ADMIN_LOGIN_URL, {"username": superuser.username, "password": "wrong-but-real-user"},
        )
        self.assertEqual(resp.status_code, 429)
        content = resp.content.decode().lower()
        self.assertIn("demasiados intentos", content)
        self.assertNotIn(superuser.username.lower(), content)
        self.assertNotIn("superuser", content)
        self.assertNotIn("staff", content)
        self.assertNotIn("does not exist", content)
        self.assertNotIn("permission", content)
        self.assertNotIn("wrong-but-real-user", content)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2 — TOTP E2E matrix
# ═══════════════════════════════════════════════════════════════════════

class TotpE2EMatrixTests(_RedisCleanupMixin, TestCase):

    def setUp(self):
        self.raw_secret = pyotp.random_base32()
        # is_staff=True: the navigation tests below drive real /admin/
        # URLs, which require Django's own has_permission() to pass
        # before the O.4a TOTP gate is even reached — including
        # admin:logout, which silently no-ops (never flushes the
        # session) for a non-staff user instead of actually logging out.
        self.user = make_user(username=_unique("o4d3_totp"), is_staff=True)
        self.device = _make_totp_device(self.user, raw_secret=self.raw_secret)
        self.client.force_login(self.user)

    def _wrong(self):
        return self.client.post(TOTP_VERIFY_URL, {"code": "111111"})

    def _correct(self):
        return self.client.post(TOTP_VERIFY_URL, {"code": _valid_code(self.raw_secret)})

    # A. Failures
    def test_five_failures_then_block(self):
        for i in range(5):
            resp = self._wrong()
            self.assertEqual(resp.status_code, 200, f"attempt {i + 1} must be processed")
        resp = self._wrong()
        self.assertEqual(resp.status_code, 429)

    # B. Real block — correct code while blocked still rejected
    def test_correct_code_while_blocked_still_429_no_verify_no_increment(self):
        for _ in range(5):
            self._wrong()
        r = _get_rl_redis()
        key = f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}"
        count_at_block = int(r.get(key) or 0)

        resp = self._correct()
        self.assertEqual(resp.status_code, 429, "correct code must still be blocked once over threshold")
        self.assertEqual(int(r.get(key) or 0), count_at_block, "blocked attempt must not increment")
        self.assertFalse(self.client.session.get("2fa_verified", False))

    # C. Expiration
    def test_after_expiry_correct_code_works_and_session_verified(self):
        for _ in range(6):
            self._wrong()
        r = _get_rl_redis()
        r.delete(f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}")

        resp = self._correct()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.client.session.get("2fa_verified", False))

    # D. Navigation — 2fa_next, query string, logout/relogin
    def test_2fa_next_and_query_string_preserved_through_block_and_expiry(self):
        wallet = make_wallet()
        requester = make_user(username=_unique("o4d3_req"))
        _grant(self.user, "can_execute_treasury_request")
        tor = _make_pending_request(wallet, requester, status=TreasuryOperationRequest.ST_APPROVED)

        with self.settings(TOTP_ADMIN_TREASURY_REQUIRED=True):
            target = _execute_url(tor.pk) + "?ref=audit-trace-123"
            resp = self.client.get(target)
            self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)
            self.assertEqual(self.client.session.get("2fa_next"), target)

            for _ in range(6):
                self._wrong()
            self.assertEqual(self.client.session.get("2fa_next"), target, "block must not consume 2fa_next")

            r = _get_rl_redis()
            r.delete(f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}")

            resp = self._correct()
            self.assertRedirects(resp, target, fetch_redirect_response=False)

    def test_logout_then_relogin_requires_totp_again(self):
        self._correct()
        self.assertTrue(self.client.session.get("2fa_verified", False))
        self.client.post(ADMIN_LOGOUT_URL)
        self.client.force_login(self.user)
        self.assertFalse(self.client.session.get("2fa_verified", False))
        resp = self.client.get(TOTP_VERIFY_URL)
        self.assertEqual(resp.status_code, 200)  # form shown again, not auto-passed


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3 — Combined password + TOTP attack, independent counters
# ═══════════════════════════════════════════════════════════════════════

class CombinedPasswordTotpAttackTests(_RedisCleanupMixin, TestCase):

    def setUp(self):
        self.raw_secret = pyotp.random_base32()
        self.username = _unique("o4d3_combo")
        self.user = make_user(username=self.username, is_staff=True, password="RealPassword!1")
        _grant(self.user, "can_execute_treasury_request")
        self.device = _make_totp_device(self.user, raw_secret=self.raw_secret)
        self.wallet = make_wallet()
        self.requester = make_user(username=_unique("o4d3_combo_req"))
        self.tor = _make_pending_request(
            self.wallet, self.requester, status=TreasuryOperationRequest.ST_APPROVED,
        )

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_full_attack_chain_password_then_totp_block_prevents_treasury_access(self):
        client = Client(REMOTE_ADDR="203.0.113.200")

        # 1-2: a few wrong passwords (under the 8/5 thresholds), then correct.
        client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "guess1"})
        client.post(ADMIN_LOGIN_URL, {"username": self.username, "password": "guess2"})
        resp = client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "RealPassword!1", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL, fetch_redirect_response=False)

        # 3: gated to TOTP (confirmed device, unverified session).
        resp = client.get(_execute_url(self.tor.pk))
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        # 4-5: fail TOTP repeatedly until blocked.
        for _ in range(5):
            resp = client.post(TOTP_VERIFY_URL, {"code": "000000"})
            self.assertEqual(resp.status_code, 200)
        resp = client.post(TOTP_VERIFY_URL, {"code": "000000"})
        self.assertEqual(resp.status_code, 429)

        # 6: cannot reach the Treasury surface — not even with the
        # correct code, while blocked.
        resp = client.post(TOTP_VERIFY_URL, {"code": _valid_code(self.raw_secret)})
        self.assertEqual(resp.status_code, 429)
        resp = client.get(_execute_url(self.tor.pk))
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        self.tor.refresh_from_db()
        self.assertEqual(self.tor.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), 0)

    def test_login_counter_independent_of_totp_counter(self):
        # Drive the TOTP counter to its threshold first.
        self.client.force_login(self.user)
        for _ in range(6):
            self.client.post(TOTP_VERIFY_URL, {"code": "000000"})
        r = _get_rl_redis()
        totp_key = f"{_RL_PREFIX}2fa_verify_fail:u{self.user.pk}"
        self.assertGreaterEqual(int(r.get(totp_key) or 0), 5)

        # A fresh, independent login attempt for this same user must be
        # entirely unaffected by the TOTP counter.
        login_client = Client(REMOTE_ADDR="203.0.113.201")
        resp = login_client.post(
            ADMIN_LOGIN_URL,
            {"username": self.username, "password": "RealPassword!1", "next": ADMIN_INDEX_URL},
        )
        self.assertRedirects(resp, ADMIN_INDEX_URL)

    def test_totp_counter_independent_of_login_counter(self):
        # Drive the IP login counter to its threshold — distinct
        # usernames per attempt so the (lower) username threshold
        # doesn't freeze the IP counter first.
        client = Client(REMOTE_ADDR="203.0.113.202")
        for i in range(9):
            client.post(ADMIN_LOGIN_URL, {"username": f"{self.username}_nope_{i}", "password": "wrong"})
        r = _get_rl_redis()
        ip_key = f"{_RL_PREFIX}admin_login_fail:ip:203.0.113.202"
        self.assertGreaterEqual(int(r.get(ip_key) or 0), 8)

        # The TOTP counter for this user must be completely untouched —
        # a legitimate, already-authenticated session can still submit
        # up to 5 wrong TOTP codes normally.
        self.client.force_login(self.user)
        for i in range(5):
            resp = self.client.post(TOTP_VERIFY_URL, {"code": "000000"})
            self.assertEqual(resp.status_code, 200, f"TOTP attempt {i + 1} must be unaffected by login block")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 4 — O.4a + O.4d combined: role matrix over representative surfaces
# ═══════════════════════════════════════════════════════════════════════

@override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
class TreasuryRoleGateWithBruteForceHardeningTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()
        self.requester = make_user(username=_unique("o4d3_role_req"))

    def _surfaces(self):
        pending = _make_pending_request(self.wallet, self.requester)
        approved = _make_pending_request(self.wallet, self.requester, status=TreasuryOperationRequest.ST_APPROVED)
        executing = _make_pending_request(self.wallet, self.requester, status=TreasuryOperationRequest.ST_EXECUTING)
        return {
            "changelist": CHANGELIST_URL,
            "detail": _change_url(pending.pk),
            "execute": _execute_url(approved.pk),
            "recover": _recover_url(executing.pk),
            "cancel": _cancel_url(pending.pk),
            "dashboard": DASHBOARD_URL,
        }, (pending, approved, executing)

    def test_no_role_can_reach_treasury_surfaces_without_completing_totp(self):
        roles = {
            "superuser": dict(is_superuser=True, is_staff=True, codename=None),
            "submitter": dict(is_staff=True, codename="can_submit_treasury_request"),
            "reviewer": dict(is_staff=True, codename="can_review_treasury_request"),
            "executor": dict(is_staff=True, codename="can_execute_treasury_request"),
            "recoverer": dict(is_staff=True, codename="can_recover_treasury_execution"),
        }
        for role_name, spec in roles.items():
            with self.subTest(role=role_name):
                user = make_user(
                    username=_unique(f"o4d3_{role_name}"),
                    is_staff=spec["is_staff"], is_superuser=spec.get("is_superuser", False),
                )
                if spec["codename"]:
                    _grant(user, spec["codename"])

                surfaces, statuses = self._surfaces()
                client = Client()
                client.force_login(user)  # direct authenticated session, no button navigation
                for surface_name, url in surfaces.items():
                    with self.subTest(role=role_name, surface=surface_name):
                        resp = client.get(url)
                        self.assertIn(
                            resp.status_code, (302,),
                            f"role={role_name} surface={surface_name} must be redirected, got {resp.status_code}",
                        )
                        self.assertIn(resp.url, (TOTP_SETUP_URL, TOTP_VERIFY_URL))

                pending, approved, executing = statuses
                pending.refresh_from_db()
                approved.refresh_from_db()
                executing.refresh_from_db()
                self.assertEqual(pending.status, TreasuryOperationRequest.ST_PENDING)
                self.assertEqual(approved.status, TreasuryOperationRequest.ST_APPROVED)
                self.assertEqual(executing.status, TreasuryOperationRequest.ST_EXECUTING)

    def test_totp_rate_limit_block_prevents_treasury_access_even_with_correct_password(self):
        raw_secret = pyotp.random_base32()
        user = make_user(username=_unique("o4d3_role_exec"), is_staff=True, password="RealPassword!1")
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, raw_secret=raw_secret)
        approved = _make_pending_request(self.wallet, self.requester, status=TreasuryOperationRequest.ST_APPROVED)

        client = Client()
        client.force_login(user)
        client.get(_execute_url(approved.pk))  # sets 2fa_next
        for _ in range(6):
            client.post(TOTP_VERIFY_URL, {"code": "000000"})
        resp = client.get(_execute_url(approved.pk))
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        approved.refresh_from_db()
        self.assertEqual(approved.status, TreasuryOperationRequest.ST_APPROVED)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 5 — Redis down / LOAD_TEST_MODE
# ═══════════════════════════════════════════════════════════════════════

class RedisDownAndLoadTestModeTests(_RedisCleanupMixin, TestCase):

    def test_redis_down_admin_login_rate_limit_fail_open(self):
        username = _unique("o4d3_redisdown_login")
        make_user(username=username, is_staff=True, password="RealPassword!1")
        client = Client(REMOTE_ADDR="203.0.113.230")
        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            for _ in range(15):
                resp = client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
                self.assertEqual(resp.status_code, 200)
            resp = client.post(
                ADMIN_LOGIN_URL,
                {"username": username, "password": "RealPassword!1", "next": ADMIN_INDEX_URL},
            )
            self.assertRedirects(resp, ADMIN_INDEX_URL)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_redis_down_totp_rate_limit_fail_open_but_totp_still_required(self):
        raw_secret = pyotp.random_base32()
        user = make_user(username=_unique("o4d3_redisdown_totp"), is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, raw_secret=raw_secret)
        wallet = make_wallet()
        requester = make_user(username=_unique("o4d3_redisdown_req"))
        approved = _make_pending_request(wallet, requester, status=TreasuryOperationRequest.ST_APPROVED)

        client = Client()
        client.force_login(user)

        with patch("simulator.ratelimit._get_rl_redis", side_effect=Exception("redis down")):
            # Rate limiter is degraded — wrong codes are never blocked...
            for _ in range(15):
                resp = client.post(TOTP_VERIFY_URL, {"code": "000000"})
                self.assertEqual(resp.status_code, 200)
            # ...but Treasury access still REQUIRES a correct TOTP code:
            # Redis being down must never grant a bypass of the 2FA
            # requirement itself, only of the anti-brute-force counter.
            resp = client.get(_execute_url(approved.pk))
            self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

            resp = client.post(TOTP_VERIFY_URL, {"code": _valid_code(raw_secret)})
            self.assertEqual(resp.status_code, 302)

        resp = client.get(_execute_url(approved.pk))
        self.assertNotEqual(resp.status_code, 302)  # no longer gated to verify; reached the real view

    @override_settings(LOAD_TEST_MODE=True, TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_load_test_mode_bypasses_rate_limit_but_not_totp_requirement(self):
        raw_secret = pyotp.random_base32()
        user = make_user(username=_unique("o4d3_loadtest"), is_staff=True)
        _grant(user, "can_execute_treasury_request")
        _make_totp_device(user, raw_secret=raw_secret)
        wallet = make_wallet()
        requester = make_user(username=_unique("o4d3_loadtest_req"))
        approved = _make_pending_request(wallet, requester, status=TreasuryOperationRequest.ST_APPROVED)

        client = Client()
        client.force_login(user)
        for _ in range(15):
            resp = client.post(TOTP_VERIFY_URL, {"code": "000000"})
            self.assertEqual(resp.status_code, 200)  # never 429 under LOAD_TEST_MODE

        # Still gated — a wrong code never grants access regardless of
        # rate-limit bypass.
        resp = client.get(_execute_url(approved.pk))
        self.assertRedirects(resp, TOTP_VERIFY_URL, fetch_redirect_response=False)

        resp = client.post(TOTP_VERIFY_URL, {"code": _valid_code(raw_secret)})
        self.assertEqual(resp.status_code, 302)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 6 — Joint auditing
# ═══════════════════════════════════════════════════════════════════════

class JointAuditingTests(_RedisCleanupMixin, TestCase):

    def test_admin_login_event_cardinality_and_metadata(self):
        username = _unique("o4d3_audit_login")
        make_user(username=username, is_staff=True, password="RealPassword!1")
        client = Client(REMOTE_ADDR="203.0.113.240")
        for _ in range(4):
            client.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})
        client.post(
            ADMIN_LOGIN_URL,
            {"username": username, "password": "RealPassword!1", "next": ADMIN_INDEX_URL},
        )
        # A separate, never-authenticated client continues the attack —
        # reusing the now-logged-in `client` here would make every
        # subsequent POST report request.user.is_authenticated=True from
        # the EXISTING session regardless of the submitted credentials
        # (a pre-existing quirk of the is_authenticated-based audit
        # branch, unrelated to O.4d and not a brute-force vector since a
        # real attacker never holds a valid session cookie).
        attacker = Client(REMOTE_ADDR="203.0.113.240")
        for _ in range(10):  # push well past the username threshold, then hammer while blocked
            attacker.post(ADMIN_LOGIN_URL, {"username": username, "password": "wrong"})

        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=_audit.EV_ADMIN_SITE_LOGIN_SUCCESS).count(), 1,
        )
        failed_events = BrokerAuditEvent.objects.filter(event_type=_audit.EV_ADMIN_SITE_LOGIN_FAILED)
        self.assertGreater(failed_events.count(), 0)
        rate_limited = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, metadata__surface="admin_login",
        )
        self.assertEqual(rate_limited.count(), 1, "no flooding across repeated blocked attempts")
        event = rate_limited.get()
        self.assertIn(event.metadata["dimension"], ("ip", "user"))
        self.assertIn("attempt_count", event.metadata)
        self.assertIn("threshold", event.metadata)
        self.assertIn("window_seconds", event.metadata)

    def test_totp_event_cardinality_and_metadata(self):
        raw_secret = pyotp.random_base32()
        user = make_user(username=_unique("o4d3_audit_totp"))
        _make_totp_device(user, raw_secret=raw_secret)
        self.client.force_login(user)
        for _ in range(10):
            self.client.post(TOTP_VERIFY_URL, {"code": "000000"})

        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=_audit.EV_2FA_VERIFY_FAILED, user=user).count(), 1,
        )
        rate_limited = BrokerAuditEvent.objects.filter(
            event_type=_audit.EV_AUTH_RATE_LIMITED, user=user, metadata__surface="totp_verify",
        )
        self.assertEqual(rate_limited.count(), 1)
        event = rate_limited.get()
        self.assertEqual(event.metadata["dimension"], "user")
        self.assertEqual(event.metadata["attempt_count"], 5)
        self.assertEqual(event.metadata["threshold"], 5)
        self.assertEqual(event.metadata["window_seconds"], 300)

    def test_no_events_for_requests_that_never_verified_credentials(self):
        # Every request in this test is blocked before authenticate()/
        # verify_totp_code() is ever called — no EV_ADMIN_SITE_LOGIN_*
        # or EV_2FA_VERIFY_FAILED may be fabricated for them.
        username = _unique("o4d3_audit_noverif")
        make_user(username=username, is_staff=True)
        client = Client(REMOTE_ADDR="203.0.113.241")
        for i in range(8):
            client.post(ADMIN_LOGIN_URL, {"username": f"{username}_{i}", "password": "wrong"})
        BrokerAuditEvent.objects.all().delete()  # isolate what happens strictly AFTER the block
        for _ in range(5):
            client.post(ADMIN_LOGIN_URL, {"username": f"{username}_blocked", "password": "wrong"})
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type__in=[_audit.EV_ADMIN_SITE_LOGIN_FAILED, _audit.EV_ADMIN_SITE_LOGIN_SUCCESS],
            ).count(),
            0,
        )

    def test_no_secrets_anywhere_in_audit_after_full_attack_simulation(self):
        raw_secret = pyotp.random_base32()
        secret_password = "TotallySecretPassw0rd!"
        username = _unique("o4d3_audit_secrets")
        user = make_user(username=username, is_staff=True, password=secret_password)
        _make_totp_device(user, raw_secret=raw_secret)

        login_client = Client(REMOTE_ADDR="203.0.113.242")
        for _ in range(9):
            login_client.post(ADMIN_LOGIN_URL, {"username": username, "password": secret_password})

        self.client.force_login(user)
        for _ in range(10):
            self.client.post(TOTP_VERIFY_URL, {"code": "999999"})

        for event in BrokerAuditEvent.objects.all():
            blob = str(event.metadata) + event.description
            self.assertNotIn(secret_password, blob)
            self.assertNotIn(raw_secret, blob)
            self.assertNotIn("999999", blob)
            self.assertNotIn("otpauth://", blob)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 7 — Rate limit primitive invariants
# ═══════════════════════════════════════════════════════════════════════

class RateLimitPrimitiveInvariantsTests(TestCase):

    def setUp(self):
        self.key = f"o4d3_primitive_{uuid.uuid4().hex[:10]}"

    def tearDown(self):
        r = _get_rl_redis()
        r.delete(f"{_RL_PREFIX}{self.key}")

    def test_rate_peek_is_readonly(self):
        from simulator.ratelimit import rate_check, rate_peek
        rate_check(self.key, limit=100, window=60)
        before = rate_peek(self.key)
        rate_peek(self.key)
        rate_peek(self.key)
        after = rate_peek(self.key)
        self.assertEqual(before, after)
        self.assertEqual(after, 1)

    def test_rate_peek_does_not_change_ttl(self):
        from simulator.ratelimit import rate_check, rate_peek
        rate_check(self.key, limit=100, window=60)
        r = _get_rl_redis()
        ttl_before = r.ttl(f"{_RL_PREFIX}{self.key}")
        rate_peek(self.key)
        ttl_after = r.ttl(f"{_RL_PREFIX}{self.key}")
        self.assertLessEqual(ttl_after, ttl_before)
        self.assertGreater(ttl_after, 0)

    def test_rate_check_behavior_unchanged(self):
        from simulator.ratelimit import rate_check
        for expected_count in (1, 2, 3):
            allowed, count = rate_check(self.key, limit=2, window=60)
            self.assertEqual(count, expected_count)
            self.assertEqual(allowed, expected_count <= 2)

    def test_keys_carry_ttl_never_permanent(self):
        from simulator.ratelimit import rate_check
        rate_check(self.key, limit=5, window=60)
        r = _get_rl_redis()
        ttl = r.ttl(f"{_RL_PREFIX}{self.key}")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 60)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 8 — Financial invariants across the full adversarial battery
# ═══════════════════════════════════════════════════════════════════════

class FinancialInvariantsAcrossAdversarialBatteryTests(_RedisCleanupMixin, TestCase):

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_wallet_ledger_and_treasury_state_unchanged_after_full_battery(self):
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        requester = make_user(username=_unique("o4d3_fin_req"))
        approved = _make_pending_request(wallet, requester, status=TreasuryOperationRequest.ST_APPROVED)

        tor_count_before = TreasuryOperationRequest.objects.count()
        wallet_count_before = Wallet.objects.count()
        wtx_count_before = WalletTransaction.objects.filter(wallet=wallet).count()
        balance_before = wallet.available_balance
        it_count_before = InternalTransfer.objects.count()

        raw_secret = pyotp.random_base32()
        executor = make_user(username=_unique("o4d3_fin_exec"), is_staff=True, password="RealPassword!1")
        _grant(executor, "can_execute_treasury_request")
        _make_totp_device(executor, raw_secret=raw_secret)

        login_client = Client(REMOTE_ADDR="203.0.113.250")
        for _ in range(9):
            login_client.post(ADMIN_LOGIN_URL, {"username": executor.username, "password": "wrong"})
        login_client.post(ADMIN_LOGIN_URL, {"username": executor.username, "password": "RealPassword!1"})

        client = Client()
        client.force_login(executor)
        client.get(_execute_url(approved.pk))
        for _ in range(10):
            client.post(TOTP_VERIFY_URL, {"code": "000000"})
        client.post(_execute_url(approved.pk), data={})  # still gated, must be a no-op

        approved.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(approved.status, TreasuryOperationRequest.ST_APPROVED)
        self.assertEqual(TreasuryOperationRequest.objects.count(), tor_count_before)
        self.assertEqual(Wallet.objects.count(), wallet_count_before)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), wtx_count_before)
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(InternalTransfer.objects.count(), it_count_before)


# ═══════════════════════════════════════════════════════════════════════
# SECTION 9 — Structural invariants
# ═══════════════════════════════════════════════════════════════════════

class StructuralInvariantsTests(TestCase):

    def test_no_new_treasury_permissions_exist(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(TreasuryOperationRequest)
        codenames = set(
            Permission.objects.filter(content_type=ct).values_list("codename", flat=True)
        )
        expected = {
            "add_treasuryoperationrequest", "change_treasuryoperationrequest",
            "delete_treasuryoperationrequest", "view_treasuryoperationrequest",
            *TREASURY_PERMISSIONS,
        }
        self.assertEqual(codenames, expected)

    def test_state_machine_statuses_unchanged(self):
        expected_statuses = {
            TreasuryOperationRequest.ST_PENDING, TreasuryOperationRequest.ST_APPROVED,
            TreasuryOperationRequest.ST_REJECTED, TreasuryOperationRequest.ST_EXECUTING,
            TreasuryOperationRequest.ST_EXECUTED, TreasuryOperationRequest.ST_FAILED,
            TreasuryOperationRequest.ST_CANCELLED,
        }
        actual_statuses = {c[0] for c in TreasuryOperationRequest._meta.get_field("status").choices}
        self.assertEqual(actual_statuses, expected_statuses)

    def test_admin_view_gate_never_imports_money_moving_functions(self):
        import ast
        import inspect
        import textwrap
        from simulator.admin import MoneyBrokerAdminSite

        tree = ast.parse(textwrap.dedent(inspect.getsource(MoneyBrokerAdminSite.admin_view)))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(a.name for a in node.names)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    names.add(name)
        self.assertNotIn("execute_treasury_request", names)
        self.assertNotIn("mark_treasury_execution_failed", names)
        self.assertNotIn("credit_wallet", names)
        self.assertNotIn("debit_wallet", names)
