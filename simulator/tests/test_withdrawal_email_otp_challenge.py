# simulator/tests/test_withdrawal_email_otp_challenge.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — simulator/withdrawal_otp.py +
WithdrawalEmailOTPChallenge model mechanics, exercised directly (not
through the views) for precise control over timing/expiry/attempts.

Covers:
  1.  create_challenge() congeals the full payload and never stores the
      plaintext code (code_hash only).
  2.  Correct code verifies -> status VERIFIED, verified_at set.
  3.  Wrong code -> attempts increments and PERSISTS despite the raise
      (the rollback bug this design explicitly avoids).
  4.  max_attempts reached -> status LOCKED, further verify attempts blocked.
  5.  Expired challenge -> status EXPIRED, verify blocked.
  6.  A challenge that is already VERIFIED/USED cannot be re-verified.
  7.  resend_challenge() regenerates code+expiry, respects the cooldown,
      and does NOT reset attempts (no brute-force reset lever).
  8.  One non-terminal challenge per user — a second create_challenge()
      call raises ActiveChallengeExists (both from the pre-check and,
      via the DB constraint, defense in depth).
  9.  mark_challenge_used() only accepts a VERIFIED challenge.
  10. "Payload mutation invalidates OTP" — verify_challenge() takes ONLY
      (challenge_id, code); there is no code path that lets a caller
      change amount/asset/network/address on an existing challenge. Once
      USED, the challenge cannot be reused for a second WithdrawalRequest.

WITHDRAWAL-SECURITY-EXTENSION-01 SECURITY FIX (OTP hash hardening) —
HashCodeAlgorithmTests below additionally covers:
  11. Correct code verifies; incorrect code fails (via verify_challenge()).
  12. Stored digest is NOT bare sha256(code) — it's keyed + context-bound.
  13. Same key + same context (challenge_id/user_id/purpose) + same code
      -> deterministic, reproducible digest.
  14. Different code -> different digest (no collision under the same context).
  15. Different secret (key rotation) -> old digest no longer matches a
      freshly computed one for the same code/context.
  16. code_hash never contains/equals the plaintext code (still true — see
      test_never_stores_plaintext_code above).
"""
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from simulator.models import WithdrawalEmailOTPChallenge
from simulator.tests.factories import make_user
from simulator import withdrawal_otp as otp


class CreateChallengeTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_congeals_full_payload(self):
        challenge, code = otp.create_challenge(
            self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr123",
            amount_usd=Decimal("1500"), withdraw_all=False,
        )
        self.assertEqual(challenge.asset, "USDT")
        self.assertEqual(challenge.network, "TRC20")
        self.assertEqual(challenge.wallet_address, "Taddr123")
        self.assertEqual(challenge.amount_usd, Decimal("1500"))
        self.assertFalse(challenge.withdraw_all)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_never_stores_plaintext_code(self):
        challenge, code = otp.create_challenge(
            self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="BTC", network="BTC_MAINNET", wallet_address="bc1qtest",
            amount_usd=Decimal("2000"),
        )
        self.assertNotIn(code, challenge.code_hash)
        self.assertEqual(
            challenge.code_hash,
            otp.hash_code(code, challenge_id=challenge.pk, user_id=self.user.pk, purpose=challenge.purpose),
        )
        self.assertEqual(len(challenge.code_hash), 64)  # HMAC-SHA256 hexdigest
        # Never the bare (unkeyed) sha256 of the code — that's exactly the
        # weakness this fix closes.
        import hashlib as _hashlib
        self.assertNotEqual(challenge.code_hash, _hashlib.sha256(code.encode()).hexdigest())

    def test_one_active_challenge_per_user(self):
        otp.create_challenge(
            self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr1", amount_usd=Decimal("1500"),
        )
        with self.assertRaises(otp.ActiveChallengeExists):
            otp.create_challenge(
                self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
                asset="USDT", network="TRC20", wallet_address="Taddr2", amount_usd=Decimal("1600"),
            )
        self.assertEqual(WithdrawalEmailOTPChallenge.objects.filter(user=self.user).count(), 1)


class VerifyChallengeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.challenge, self.code = otp.create_challenge(
            self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr1", amount_usd=Decimal("1500"),
        )

    def test_correct_code_verifies(self):
        verified = otp.verify_challenge(self.challenge.id, self.code, user=self.user)
        self.assertEqual(verified.status, WithdrawalEmailOTPChallenge.STATUS_VERIFIED)
        self.assertIsNotNone(verified.verified_at)

    def test_wrong_code_increments_attempts_and_persists_despite_raise(self):
        with self.assertRaises(otp.InvalidCode):
            otp.verify_challenge(self.challenge.id, "000000", user=self.user)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.attempts, 1)
        self.assertEqual(self.challenge.status, WithdrawalEmailOTPChallenge.STATUS_PENDING)

    def test_max_attempts_locks_challenge(self):
        from django.conf import settings
        for _ in range(settings.WITHDRAWAL_OTP_MAX_ATTEMPTS):
            try:
                otp.verify_challenge(self.challenge.id, "000000", user=self.user)
            except (otp.InvalidCode, otp.ChallengeLocked):
                pass
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.status, WithdrawalEmailOTPChallenge.STATUS_LOCKED)
        with self.assertRaises(otp.ChallengeNotVerifiable):
            otp.verify_challenge(self.challenge.id, self.code, user=self.user)

    def test_expired_challenge_blocks_verification(self):
        WithdrawalEmailOTPChallenge.objects.filter(pk=self.challenge.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        with self.assertRaises(otp.ChallengeExpired):
            otp.verify_challenge(self.challenge.id, self.code, user=self.user)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.status, WithdrawalEmailOTPChallenge.STATUS_EXPIRED)

    def test_already_verified_cannot_be_reverified(self):
        otp.verify_challenge(self.challenge.id, self.code, user=self.user)
        with self.assertRaises(otp.ChallengeNotVerifiable):
            otp.verify_challenge(self.challenge.id, self.code, user=self.user)

    def test_used_challenge_cannot_be_reused(self):
        verified = otp.verify_challenge(self.challenge.id, self.code, user=self.user)
        otp.mark_challenge_used(verified)
        with self.assertRaises(otp.ChallengeNotVerifiable):
            otp.verify_challenge(self.challenge.id, self.code, user=self.user)

    def test_mark_used_requires_verified_status(self):
        with self.assertRaises(otp.ChallengeNotVerifiable):
            otp.mark_challenge_used(self.challenge)  # still PENDING


class ResendChallengeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.challenge, self.code = otp.create_challenge(
            self.user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr1", amount_usd=Decimal("1500"),
        )

    def test_resend_cooldown_blocks_immediate_resend(self):
        with self.assertRaises(otp.ResendCooldownActive):
            otp.resend_challenge(self.challenge)

    @override_settings(WITHDRAWAL_OTP_RESEND_COOLDOWN_SECONDS=60)
    def test_resend_after_cooldown_regenerates_code(self):
        WithdrawalEmailOTPChallenge.objects.filter(pk=self.challenge.pk).update(
            last_sent_at=timezone.now() - timezone.timedelta(seconds=61),
        )
        self.challenge.refresh_from_db()
        _, new_code = otp.resend_challenge(self.challenge)
        _ctx = dict(challenge_id=self.challenge.pk, user_id=self.user.pk, purpose=self.challenge.purpose)
        self.assertNotEqual(otp.hash_code(new_code, **_ctx), otp.hash_code(self.code, **_ctx))
        # Old code no longer verifies; new code does.
        with self.assertRaises(otp.InvalidCode):
            otp.verify_challenge(self.challenge.id, self.code, user=self.user)
        self.challenge.refresh_from_db()
        verified = otp.verify_challenge(self.challenge.id, new_code, user=self.user)
        self.assertEqual(verified.status, WithdrawalEmailOTPChallenge.STATUS_VERIFIED)

    def test_resend_does_not_reset_attempts(self):
        WithdrawalEmailOTPChallenge.objects.filter(pk=self.challenge.pk).update(
            last_sent_at=timezone.now() - timezone.timedelta(seconds=999),
        )
        with self.assertRaises(otp.InvalidCode):
            otp.verify_challenge(self.challenge.id, "000000", user=self.user)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.attempts, 1)

        self.challenge.refresh_from_db()
        otp.resend_challenge(self.challenge)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.attempts, 1)  # unchanged by resend


class HashCodeAlgorithmTests(TestCase):
    """
    Direct unit tests of the keyed HMAC hashing itself (not the DB-backed
    challenge flow) — WITHDRAWAL-SECURITY-EXTENSION-01 OTP hash hardening.
    """

    def test_correct_code_verifies_end_to_end(self):
        user = make_user()
        challenge, code = otp.create_challenge(
            user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr1", amount_usd=Decimal("1500"),
        )
        verified = otp.verify_challenge(challenge.id, code, user=user)
        self.assertEqual(verified.status, WithdrawalEmailOTPChallenge.STATUS_VERIFIED)

    def test_incorrect_code_fails_end_to_end(self):
        user = make_user()
        challenge, code = otp.create_challenge(
            user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL,
            asset="USDT", network="TRC20", wallet_address="Taddr1", amount_usd=Decimal("1500"),
        )
        wrong = "000000" if code != "000000" else "111111"
        with self.assertRaises(otp.InvalidCode):
            otp.verify_challenge(challenge.id, wrong, user=user)

    def test_digest_is_not_bare_sha256(self):
        import hashlib
        digest = otp.hash_code("123456", challenge_id=1, user_id=1, purpose="WITHDRAWAL")
        self.assertNotEqual(digest, hashlib.sha256("123456".encode()).hexdigest())

    def test_same_key_same_context_same_code_is_deterministic(self):
        d1 = otp.hash_code("123456", challenge_id=42, user_id=7, purpose="WITHDRAWAL")
        d2 = otp.hash_code("123456", challenge_id=42, user_id=7, purpose="WITHDRAWAL")
        self.assertEqual(d1, d2)

    def test_different_code_different_digest(self):
        ctx = dict(challenge_id=42, user_id=7, purpose="WITHDRAWAL")
        self.assertNotEqual(otp.hash_code("123456", **ctx), otp.hash_code("654321", **ctx))

    def test_different_context_different_digest(self):
        """Same code, different challenge_id/user_id/purpose -> different digest
        (no cross-challenge / cross-user / cross-purpose replay)."""
        base = otp.hash_code("123456", challenge_id=1, user_id=1, purpose="WITHDRAWAL")
        self.assertNotEqual(base, otp.hash_code("123456", challenge_id=2, user_id=1, purpose="WITHDRAWAL"))
        self.assertNotEqual(base, otp.hash_code("123456", challenge_id=1, user_id=2, purpose="WITHDRAWAL"))
        self.assertNotEqual(
            base, otp.hash_code("123456", challenge_id=1, user_id=1, purpose="ADDRESS_CHANGE"),
        )

    @override_settings(WITHDRAWAL_OTP_HASH_KEY="key-one-for-testing-only")
    def test_key_rotation_invalidates_old_digest(self):
        ctx = dict(challenge_id=42, user_id=7, purpose="WITHDRAWAL")
        old_digest = otp.hash_code("123456", **ctx)
        with override_settings(WITHDRAWAL_OTP_HASH_KEY="key-two-different-secret"):
            new_digest = otp.hash_code("123456", **ctx)
        self.assertNotEqual(old_digest, new_digest)

    def test_falls_back_to_secret_key_when_hash_key_unset(self):
        """WITHDRAWAL_OTP_HASH_KEY='' -> falls back to SECRET_KEY, not a crash."""
        with override_settings(WITHDRAWAL_OTP_HASH_KEY=""):
            digest = otp.hash_code("123456", challenge_id=1, user_id=1, purpose="WITHDRAWAL")
        self.assertEqual(len(digest), 64)
