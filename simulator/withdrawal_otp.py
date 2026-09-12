# simulator/withdrawal_otp.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — email OTP challenge mechanics.

One model (WithdrawalEmailOTPChallenge) serves two purposes (WITHDRAWAL,
ADDRESS_CHANGE — see models.py). This module owns ONLY the OTP mechanics
(generate/hash/verify/expire/lock/resend) — it has no knowledge of
WithdrawalRequest or VerifiedWithdrawalWallet; the caller decides what a
verified challenge produces (see views.py / verified_wallets.py).

code_hash is HMAC-SHA256(server_key, "challenge_id:user_id:purpose:code") —
keyed, not a bare sha256(code). A bare hash of a 6-digit code (10**6
possibilities) is offline-bruteforceable from a DB copy alone in seconds;
keying it to a server-side secret (never stored in the DB) makes that
attack require the secret too, not just the dump. The context string
(challenge_id/user_id/purpose) additionally makes each digest useless
against any OTHER row even if the same code and key were ever reused —
challenge_id serves as the per-row nonce (it's the primary key, unique by
construction, so no new field/migration is needed for it). Never store
the plaintext code. Comparison is constant-time (hmac.compare_digest).
See _get_hash_key() for the key resolution (WITHDRAWAL_OTP_HASH_KEY, with
a SECRET_KEY fallback for dev/test — same discipline as
two_factor.py::_get_fernet()'s TOTP_ENCRYPTION_KEY fallback).

Locking discipline: create_challenge() locks the user's Wallet row as the
stable per-user serialization point — the exact same idiom withdraw_view
already uses for its "one pending WithdrawalRequest" guard (views.py:2370).
The DB partial-unique constraint (wotp_one_active_per_user) is defense in
depth against any path that bypasses this function, same relationship
PayoutAttempt's own guard has to its DB constraint (payout_state_machine.py).
"""
import hashlib
import hmac
import logging
import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger("simulator.withdrawal_otp")


class ActiveChallengeExists(Exception):
    """The user already has a non-terminal (pending/verified) challenge."""


class ChallengeNotFound(Exception):
    pass


class ChallengeExpired(Exception):
    pass


class ChallengeLocked(Exception):
    """max_attempts exceeded."""


class InvalidCode(Exception):
    pass


class ChallengeNotVerifiable(Exception):
    """Challenge is not in a state that can accept a code (already used/expired/locked)."""


class ResendCooldownActive(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Resend cooldown active — retry in {retry_after_seconds}s")


def generate_code() -> str:
    """Cryptographically random 6-digit numeric code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


def expire_if_stale(target, *, now=None):
    """
    WITHDRAWAL-OTP-STALE-PENDING-FIX-02 — the single, central place that
    decides whether a challenge is stale, and normalizes it.

    Rule (identical for both call shapes below): status == PENDING AND
    expires_at <= now  ->  status = EXPIRED. Idempotent (a no-op on a
    challenge that's already EXPIRED or any other terminal status) and
    monotonic (the only transition this ever performs is PENDING ->
    EXPIRED — it never revives a terminal status and never touches a
    still-active PENDING/VERIFIED row). No side effects beyond that one
    column: attempts/code_hash/last_sent_at/purpose/created_at are all
    left untouched.

    Two calling shapes, reused identically by create_challenge()
    (candidate-existence check, no single instance in hand yet),
    resend_challenge() (must refuse to revive a stale row), and the
    OTP-verify GET views (must not render a code form for a dead
    challenge):

      - expire_if_stale(challenge_instance) — normalizes that ONE
        already-fetched instance in place (mutates .status so the
        caller's in-memory object is immediately consistent with the
        DB). Returns True if the challenge is (now, or already was)
        EXPIRED by clock; False if it's genuinely still active or in
        some other terminal status untouched by this helper.

      - expire_if_stale(queryset) — bulk-normalizes every stale PENDING
        row the queryset matches (e.g. all of one user's challenges,
        across every purpose). Returns the number of rows flipped.
    """
    from django.db.models import QuerySet
    from .models import WithdrawalEmailOTPChallenge as _M

    now = now or timezone.now()

    if isinstance(target, QuerySet):
        return target.filter(
            status=_M.STATUS_PENDING, expires_at__lte=now,
        ).update(status=_M.STATUS_EXPIRED)

    challenge = target
    flipped = expire_if_stale(_M.objects.filter(pk=challenge.pk), now=now)
    if flipped:
        challenge.status = _M.STATUS_EXPIRED
    return challenge.status == _M.STATUS_EXPIRED


def _get_hash_key() -> bytes:
    """
    HMAC key for OTP code hashing. Prefers the dedicated
    WITHDRAWAL_OTP_HASH_KEY; if unset, falls back to Django's own
    SECRET_KEY with a warning log — SECRET_KEY is already the implicit
    HMAC key behind django.core.signing, which simulator/email_verification.py
    already relies on for its own token signing, so this fallback reuses
    an existing trust boundary rather than inventing a new one. A
    dedicated key is still preferred and should be set in staging/production.
    """
    key = getattr(settings, "WITHDRAWAL_OTP_HASH_KEY", "").strip()
    if key:
        return key.encode()
    logger.warning(
        "[withdrawal_otp] WITHDRAWAL_OTP_HASH_KEY not set — falling back to "
        "SECRET_KEY for OTP hashing (fine for dev/test; set a dedicated "
        "key in staging/production .env)."
    )
    return settings.SECRET_KEY.encode()


def hash_code(code: str, *, challenge_id: int, user_id: int, purpose: str) -> str:
    """
    Keyed HMAC-SHA256 digest of *code*, bound to (challenge_id, user_id,
    purpose). challenge_id is the row's own primary key — a stable,
    unique-by-construction nonce, so no new field is needed to get
    per-row domain separation.
    """
    msg = f"{challenge_id}:{user_id}:{purpose}:{code.strip()}".encode()
    return hmac.new(_get_hash_key(), msg, hashlib.sha256).hexdigest()


def create_challenge(
    user, *, purpose, asset: str, network: str, wallet_address: str,
    amount_usd=None, withdraw_all: bool = False,
):
    """
    Create a new WithdrawalEmailOTPChallenge with a freshly generated code.

    Raises ActiveChallengeExists if the user already has a non-terminal
    challenge (must be verified/used/expired/locked first).

    Returns (challenge, plaintext_code) — the caller is responsible for
    emailing plaintext_code (see withdrawal_emails.py) and must never persist
    it anywhere.
    """
    from .models import Wallet, WithdrawalEmailOTPChallenge
    from .wallet_ledger import get_or_create_wallet

    wallet, _ = get_or_create_wallet(user)
    now = timezone.now()
    code = generate_code()

    with transaction.atomic():
        Wallet.objects.select_for_update().get(pk=wallet.id)

        # WITHDRAWAL-WALLET-OTP-PENDING-CHALLENGE-01 — a PENDING challenge
        # whose expires_at has already passed by wall clock would otherwise
        # block this user forever: the wotp_one_active_per_user constraint
        # (and the .exists() check right below) key off `status`, not off
        # expires_at, and nothing else ever flips a stale row's status —
        # verify_challenge() only does that lazily, as a side effect of
        # someone actually attempting a code on THAT specific row. Normalize
        # here, BEFORE the active-check, so the row is physically no longer
        # NON_TERMINAL by the time the constraint/check run — same
        # transition verify_challenge() would perform, just materialized
        # proactively instead of waiting for a verify attempt that may
        # never come. Preserves id/created_at/code_hash/attempts/purpose/
        # last_sent_at — only `status` changes. WITHDRAWAL-OTP-STALE-
        # PENDING-FIX-02: delegates the actual PENDING+expired->EXPIRED
        # decision to expire_if_stale(), the single place that rule now
        # lives (also reused by resend_challenge() and the OTP-verify
        # views) — this call is functionally identical to the inline
        # .update() it replaces.
        expire_if_stale(
            WithdrawalEmailOTPChallenge.objects.filter(user=user), now=now,
        )

        if WithdrawalEmailOTPChallenge.objects.filter(
            user=user, status__in=WithdrawalEmailOTPChallenge.NON_TERMINAL_STATUSES,
        ).exists():
            raise ActiveChallengeExists(
                f"User #{user.pk} already has an unresolved withdrawal OTP challenge."
            )

        try:
            # code_hash is computed AFTER the row exists, because it's
            # keyed to challenge.id (the per-row nonce — see hash_code()).
            # The brief empty placeholder is never visible outside this
            # still-open transaction.
            challenge = WithdrawalEmailOTPChallenge.objects.create(
                user=user,
                purpose=purpose,
                amount_usd=amount_usd,
                withdraw_all=withdraw_all,
                asset=asset,
                network=network,
                wallet_address=wallet_address,
                code_hash="",
                expires_at=now + timezone.timedelta(minutes=settings.WITHDRAWAL_OTP_EXPIRY_MINUTES),
                max_attempts=settings.WITHDRAWAL_OTP_MAX_ATTEMPTS,
                last_sent_at=now,
            )
        except IntegrityError as exc:
            # Defense in depth — the pre-check above already covers the
            # common case; this catches the DB constraint if two requests
            # somehow race past it.
            raise ActiveChallengeExists(
                f"User #{user.pk} already has an unresolved withdrawal OTP challenge."
            ) from exc

        challenge.code_hash = hash_code(
            code, challenge_id=challenge.pk, user_id=user.pk, purpose=purpose,
        )
        challenge.save(update_fields=["code_hash"])

    logger.info(
        "[withdrawal_otp] created challenge_id=%d user=%s purpose=%s",
        challenge.id, user.username, purpose,
    )
    return challenge, code


def can_resend(challenge) -> tuple[bool, int]:
    """Returns (can_resend, seconds_remaining_if_not)."""
    elapsed = (timezone.now() - challenge.last_sent_at).total_seconds()
    remaining = settings.WITHDRAWAL_OTP_RESEND_COOLDOWN_SECONDS - elapsed
    if remaining > 0:
        return False, int(remaining) + 1
    return True, 0


def resend_challenge(challenge):
    """
    Regenerate the code + expiry for a still-pending challenge. Does NOT
    reset the attempts counter (a resend must not become a brute-force
    reset lever) — a challenge that's already LOCKED cannot be resent.

    Raises ChallengeNotVerifiable if the challenge isn't PENDING,
    ChallengeExpired if it's PENDING but stale by clock (WITHDRAWAL-OTP-
    STALE-PENDING-FIX-02 — a challenge expired by clock must never be
    revived: no new code, no extended expires_at, no status flip back to
    PENDING, no email sent), ResendCooldownActive if called before the
    configured cooldown elapses.
    """
    from .models import WithdrawalEmailOTPChallenge as _M

    if challenge.status != _M.STATUS_PENDING:
        raise ChallengeNotVerifiable(f"Challenge #{challenge.pk} is {challenge.status}, cannot resend.")

    if expire_if_stale(challenge):
        raise ChallengeExpired(f"Challenge #{challenge.pk} expired at {challenge.expires_at}.")

    ok, retry_after = can_resend(challenge)
    if not ok:
        raise ResendCooldownActive(retry_after)

    now = timezone.now()
    code = generate_code()
    challenge.code_hash = hash_code(
        code, challenge_id=challenge.pk, user_id=challenge.user_id, purpose=challenge.purpose,
    )
    challenge.expires_at = now + timezone.timedelta(minutes=settings.WITHDRAWAL_OTP_EXPIRY_MINUTES)
    challenge.last_sent_at = now
    challenge.save(update_fields=["code_hash", "expires_at", "last_sent_at"])

    logger.info("[withdrawal_otp] resent challenge_id=%d", challenge.pk)
    return challenge, code


def verify_challenge(challenge_id: int, code: str, *, user):
    """
    Verify *code* against the challenge identified by (challenge_id, user).
    Fully self-contained: locks the row, mutates, persists, in its own
    transaction.atomic() — the caller does NOT need to hold a lock or an
    outer atomic() block.

    This is deliberately NOT nested inside the withdrawal/address-change
    creation transaction: a wrong-code attempt must increment
    challenge.attempts and persist that increment even though the overall
    call raises — if the write and the raise shared one atomic() block,
    Django would roll back the very save() that just happened. So every
    write here happens, the `with transaction.atomic()` block is allowed
    to exit NORMALLY (commit), and only THEN, outside the block, do we
    raise — never let an exception escape the atomic() body itself.

    On success: status -> VERIFIED, verified_at set, and the challenge
    instance is returned. Caller is responsible for calling
    mark_challenge_used() once the downstream object (WithdrawalRequest /
    VerifiedWithdrawalWallet) is actually created, in ITS OWN atomic block
    (see views.py) — that step SHOULD roll back together with the object
    it authorizes, unlike this one.

    Raises ChallengeNotFound / ChallengeNotVerifiable / ChallengeExpired /
    ChallengeLocked / InvalidCode.
    """
    from .models import WithdrawalEmailOTPChallenge as _M

    error = None
    with transaction.atomic():
        try:
            challenge = _M.objects.select_for_update().get(pk=challenge_id, user=user)
        except _M.DoesNotExist:
            raise ChallengeNotFound(f"No challenge #{challenge_id} for user #{getattr(user, 'pk', None)}.")

        now = timezone.now()
        if challenge.status != _M.STATUS_PENDING:
            error = ChallengeNotVerifiable(f"Challenge #{challenge.pk} is {challenge.status}, cannot verify.")
        elif challenge.expires_at <= now:
            challenge.status = _M.STATUS_EXPIRED
            challenge.save(update_fields=["status"])
            error = ChallengeExpired(f"Challenge #{challenge.pk} expired at {challenge.expires_at}.")
        elif challenge.attempts >= challenge.max_attempts:
            challenge.status = _M.STATUS_LOCKED
            challenge.save(update_fields=["status"])
            error = ChallengeLocked(f"Challenge #{challenge.pk} locked — max attempts reached.")
        elif not hmac.compare_digest(
            hash_code(code, challenge_id=challenge.pk, user_id=challenge.user_id, purpose=challenge.purpose),
            challenge.code_hash,
        ):
            challenge.attempts += 1
            if challenge.attempts >= challenge.max_attempts:
                challenge.status = _M.STATUS_LOCKED
                challenge.save(update_fields=["attempts", "status"])
                error = ChallengeLocked(f"Challenge #{challenge.pk} locked — max attempts reached.")
            else:
                challenge.save(update_fields=["attempts"])
                error = InvalidCode(f"Wrong code for challenge #{challenge.pk}.")
        else:
            challenge.status = _M.STATUS_VERIFIED
            challenge.verified_at = now
            challenge.save(update_fields=["status", "verified_at"])

    if error is not None:
        raise error
    return challenge


def mark_challenge_used(challenge) -> None:
    """
    Mark a VERIFIED challenge as USED — call exactly once, in the same
    atomic block that creates the WithdrawalRequest/VerifiedWithdrawalWallet
    the challenge authorized.
    """
    from .models import WithdrawalEmailOTPChallenge as _M

    if challenge.status != _M.STATUS_VERIFIED:
        raise ChallengeNotVerifiable(f"Challenge #{challenge.pk} is {challenge.status}, cannot mark used.")
    challenge.status = _M.STATUS_USED
    challenge.used_at = timezone.now()
    challenge.save(update_fields=["status", "used_at"])
