# simulator/tests/test_withdrawal_otp_stale_pending_fix_02.py
"""
WITHDRAWAL-OTP-STALE-PENDING-FIX-02 — expire_if_stale() central helper,
the resend_challenge() bug fix (a PENDING-but-clock-expired challenge
could be silently revived by Resend, re-extending its expires_at and
keeping it able to block /withdraw/), and the OTP-verify GET views no
longer rendering a code form for a dead challenge.

create_challenge()'s own stale-normalization (WITHDRAWAL-WALLET-OTP-
PENDING-CHALLENGE-01) was already correct and is NOT re-tested in full
here — see ExpiredChallengeNormalizationTests in
test_withdrawal_email_otp_challenge.py, which continues to pass
unchanged since create_challenge() was refactored to delegate to
expire_if_stale() with functionally identical behavior.
"""
import threading
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from simulator.models import TOTPDevice, WithdrawalEmailOTPChallenge
from simulator import withdrawal_otp as otp
from .factories import make_user, make_wallet, make_kyc_approved, make_verified_withdrawal_wallet
from .withdrawal_flow_helpers import PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code


def _make_stale_challenge(user, purpose, **kwargs):
    now = timezone.now()
    defaults = dict(
        asset="USDT", network="TRC20", wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        code_hash="x", max_attempts=5,
        expires_at=now - timezone.timedelta(hours=1, minutes=44),
        last_sent_at=now - timezone.timedelta(hours=1, minutes=44),
        status=WithdrawalEmailOTPChallenge.STATUS_PENDING,
    )
    if purpose == WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL:
        defaults.setdefault("amount_usd", Decimal("50.00"))
    defaults.update(kwargs)
    return WithdrawalEmailOTPChallenge.objects.create(user=user, purpose=purpose, **defaults)


class ExpireIfStaleHelperTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_stale_pending_instance_flips_to_expired(self):
        c = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL)
        self.assertTrue(otp.expire_if_stale(c))
        self.assertEqual(c.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)
        c.refresh_from_db()
        self.assertEqual(c.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)

    def test_active_pending_instance_untouched(self):
        c = _make_stale_challenge(
            self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        self.assertFalse(otp.expire_if_stale(c))
        self.assertEqual(c.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_idempotent_on_already_expired(self):
        c = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL)
        self.assertTrue(otp.expire_if_stale(c))
        # second call — already EXPIRED, no-op, still reports True (inactive)
        self.assertTrue(otp.expire_if_stale(c))
        self.assertEqual(c.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)

    def test_never_revives_a_terminal_status(self):
        c = _make_stale_challenge(
            self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            status=WithdrawalEmailOTPChallenge.STATUS_LOCKED,
        )
        self.assertFalse(otp.expire_if_stale(c))
        self.assertEqual(c.status, WithdrawalEmailOTPChallenge.STATUS_LOCKED)

    def test_queryset_mode_bulk_flips_only_stale_pending(self):
        # wotp_one_active_per_user is purpose-agnostic (one non-terminal
        # row per user, period), so a stale-pending and an active-pending
        # row can never coexist for the SAME user — use two users to
        # exercise the bulk queryset mode's "only touch actually-stale
        # rows" behavior instead.
        other_user = make_user()
        stale = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE)
        active = _make_stale_challenge(
            other_user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        flipped = otp.expire_if_stale(
            WithdrawalEmailOTPChallenge.objects.filter(user__in=[self.user, other_user]),
        )
        self.assertEqual(flipped, 1)
        stale.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)
        self.assertEqual(active.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)


class ResendStaleChallengeTests(TestCase):
    """The actual bug from the Root Cause Report: resend_challenge() must
    refuse to revive a PENDING-but-clock-expired challenge."""

    def setUp(self):
        self.user = make_user()
        self.stale = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL)

    def test_resend_on_stale_pending_raises_challenge_expired(self):
        with self.assertRaises(otp.ChallengeExpired):
            otp.resend_challenge(self.stale)

    def test_resend_on_stale_pending_flips_status_to_expired(self):
        with self.assertRaises(otp.ChallengeExpired):
            otp.resend_challenge(self.stale)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)

    def test_resend_on_stale_pending_does_not_extend_expires_at(self):
        original_expires_at = self.stale.expires_at
        with self.assertRaises(otp.ChallengeExpired):
            otp.resend_challenge(self.stale)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.expires_at, original_expires_at)

    def test_resend_on_stale_pending_does_not_change_code_hash(self):
        original_hash = self.stale.code_hash
        with self.assertRaises(otp.ChallengeExpired):
            otp.resend_challenge(self.stale)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.code_hash, original_hash)

    def test_resend_on_stale_pending_does_not_bump_last_sent_at(self):
        original_last_sent = self.stale.last_sent_at
        with self.assertRaises(otp.ChallengeExpired):
            otp.resend_challenge(self.stale)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.last_sent_at, original_last_sent)

    def test_active_pending_resend_still_works(self):
        """Regression: a genuinely active PENDING challenge can still be
        resent. Uses a separate user — setUp's self.stale already holds
        the one-active-per-user slot for self.user."""
        other_user = make_user()
        active = _make_stale_challenge(
            other_user, WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
            last_sent_at=timezone.now() - timezone.timedelta(seconds=999),
        )
        _, new_code = otp.resend_challenge(active)
        self.assertEqual(len(new_code), 6)
        active.refresh_from_db()
        self.assertEqual(active.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_view_resend_action_on_stale_challenge_sends_no_email(self):
        """End-to-end: POST /withdraw/otp/ action=resend on a stale session
        challenge must not send an email (only ever a possible consequence
        of a successful resend_challenge() call, which now never happens
        for a stale row)."""
        make_kyc_approved(self.user)
        make_wallet(self.user, initial_balance=Decimal("500"))
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.client.force_login(self.user)
        session = self.client.session
        session["withdraw_otp_challenge_id"] = self.stale.id
        session.save()

        with patch("simulator.views.send_withdrawal_otp_email") as mock_send:
            resp = self.client.post("/withdraw/otp/", {"action": "resend"})

        mock_send.assert_not_called()
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)


class StaleChallengeGetRedirectTests(TestCase):
    """GET /withdraw/otp/ and GET /withdraw/wallets/otp/ must not render a
    code-entry form for a challenge that's dead by clock — normalize and
    redirect back to the flow's starting point instead."""

    def setUp(self):
        self.user = make_user()
        make_kyc_approved(self.user)
        make_wallet(self.user, initial_balance=Decimal("500"))
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.client.force_login(self.user)

    def test_get_withdraw_otp_with_stale_session_challenge_redirects(self):
        stale = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL)
        session = self.client.session
        session["withdraw_otp_challenge_id"] = stale.id
        session.save()

        resp = self.client.get("/withdraw/otp/")

        self.assertRedirects(resp, "/withdraw/", fetch_redirect_response=False)
        stale.refresh_from_db()
        self.assertEqual(stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)
        self.assertNotIn("withdraw_otp_challenge_id", self.client.session)

    def test_get_withdraw_otp_with_active_session_challenge_renders_form(self):
        active = _make_stale_challenge(
            self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        session = self.client.session
        session["withdraw_otp_challenge_id"] = active.id
        session.save()

        resp = self.client.get("/withdraw/otp/")

        self.assertEqual(resp.status_code, 200)
        active.refresh_from_db()
        self.assertEqual(active.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_get_withdraw_wallet_otp_with_stale_session_challenge_redirects(self):
        stale = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE)
        session = self.client.session
        session["withdraw_wallet_challenge_id"] = stale.id
        session.save()

        resp = self.client.get("/withdraw/wallets/otp/")

        self.assertRedirects(resp, "/withdraw/wallets/register/", fetch_redirect_response=False)
        stale.refresh_from_db()
        self.assertEqual(stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)
        self.assertNotIn("withdraw_wallet_challenge_id", self.client.session)

    def test_get_withdraw_wallet_otp_with_active_session_challenge_renders_form(self):
        active = _make_stale_challenge(
            self.user, WithdrawalEmailOTPChallenge.PURPOSE_ADDRESS_CHANGE,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        session = self.client.session
        session["withdraw_wallet_challenge_id"] = active.id
        session.save()

        resp = self.client.get("/withdraw/wallets/otp/")

        self.assertEqual(resp.status_code, 200)
        active.refresh_from_db()
        self.assertEqual(active.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)


class CreateAfterStaleEndToEndTests(TestCase):
    """/withdraw/ end-to-end: a stale-by-clock pending challenge must not
    block a fresh POST /withdraw/ — the exact symptom from the Root Cause
    Report, driven through the real view this time (not otp.create_challenge()
    directly, which ExpiredChallengeNormalizationTests already covers)."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("5000"))
        make_kyc_approved(self.user)
        TOTPDevice.objects.create(user=self.user, secret="b64:x", confirmed=True)
        self.vw = make_verified_withdrawal_wallet(self.user)
        self.client.force_login(self.user)

    def test_withdraw_post_succeeds_despite_stale_pending_row(self):
        stale = _make_stale_challenge(self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL)

        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code("123456"):
            resp = self.client.post("/withdraw/", {
                "amount_usd": "100", "crypto_currency": "usdttrc20",
                "wallet_address": str(self.vw.pk), "otp_code": "000000",
            })

        self.assertRedirects(resp, "/withdraw/otp/", fetch_redirect_response=False)
        stale.refresh_from_db()
        self.assertEqual(stale.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)
        new = WithdrawalEmailOTPChallenge.objects.filter(
            user=self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
        ).exclude(pk=stale.pk).first()
        self.assertIsNotNone(new)
        self.assertEqual(new.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_withdraw_post_still_blocked_by_genuinely_active_challenge(self):
        """Regression: the one-active-challenge guard still works for a
        real, non-expired PENDING challenge."""
        _make_stale_challenge(
            self.user, WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        with PATCH_TOTP, PATCH_EMAIL, PATCH_RATELIMIT, fixed_otp_code("123456"):
            resp = self.client.post("/withdraw/", {
                "amount_usd": "100", "crypto_currency": "usdttrc20",
                "wallet_address": str(self.vw.pk), "otp_code": "000000",
            })
        self.assertContains(resp, "Ya tienes una verificaci", status_code=200)
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)


class ConcurrentCreateChallengeTests(TransactionTestCase):
    """Genuine concurrent threads — TransactionTestCase is required for
    real cross-connection locking, same discipline as this session's
    OpsAdminConcurrencyTests (MONEY-INTEGRITY-FIX-02)."""

    @staticmethod
    def _call_with_sqlite_lock_retry(fn, *, attempts=20, delay=0.05):
        """SQLite's shared-cache in-memory test DB has no true MVCC row
        locking — a second writer contending for a row/table raises
        OperationalError immediately instead of blocking-then-succeeding
        the way PostgreSQL (the production target) does. Test-harness-
        only accommodation; changes nothing about create_challenge()
        itself, whose correctness is independently proven by the
        non-threaded IntegrityError-path tests elsewhere in this suite."""
        import time
        from django.db.utils import OperationalError

        last_exc = None
        for _ in range(attempts):
            try:
                return fn()
            except OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                last_exc = exc
                time.sleep(delay)
        raise last_exc

    def test_concurrent_create_maintains_one_active_invariant(self):
        user = make_user()
        results = {}

        def _create(name):
            try:
                results[name] = self._call_with_sqlite_lock_retry(lambda: otp.create_challenge(
                    user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
                    asset="USDT", network="TRC20",
                    wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                    amount_usd=Decimal("50.00"),
                ))
            except otp.ActiveChallengeExists as exc:
                results[name] = exc

        t1 = threading.Thread(target=_create, args=("a",))
        t2 = threading.Thread(target=_create, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes = list(results.values())
        successes = [o for o in outcomes if isinstance(o, tuple)]
        failures = [o for o in outcomes if isinstance(o, otp.ActiveChallengeExists)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            WithdrawalEmailOTPChallenge.objects.filter(
                user=user, status__in=WithdrawalEmailOTPChallenge.NON_TERMINAL_STATUSES,
            ).count(),
            1,
        )
