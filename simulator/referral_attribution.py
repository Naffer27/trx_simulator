"""
simulator/referral_attribution.py
IB-ATTRIBUTION-FOUNDATION-01 — stateless signed-token referral attribution.

Token = Django signing.dumps(referral_code, salt=_SALT) — expires in 30
days. Same idiom as simulator/email_verification.py: no DB row for the
token itself, tamper-evident (HMAC-signed), never raises.

Two carriers are written on every valid click — session (fast, same-visit,
but capped by this project's default SESSION_COOKIE_AGE of 14 days) and a
cookie (the actually-durable 30-day carrier). Session is checked first at
resolution time only because it's cheaper; the cookie is what makes the
full 30-day window real.

First-valid-referral-wins: record_referral_click() only writes a new
token when no currently-valid one already resolves — a second/different
link clicked later in the window never overrides an existing one.

No commission calculation, no wallet credit, no multi-level/sub-IB logic
lives here or anywhere yet — this module is attribution only.
"""
import logging

from django.core import signing

logger = logging.getLogger("simulator.referral_attribution")

_SALT        = "referral-attribution"
_MAX_AGE     = 30 * 24 * 3600  # 30 days
_SESSION_KEY = "referral_token"
_COOKIE_NAME = "ref_attr"


def _make_token(code: str) -> str:
    return signing.dumps(code, salt=_SALT)


def _verify_token(token: str) -> str | None:
    """Return the referral code encoded in *token* if valid and
    unexpired. Returns None on expired, invalid, or malformed tokens —
    never raises."""
    try:
        return signing.loads(token, salt=_SALT, max_age=_MAX_AGE)
    except signing.SignatureExpired:
        logger.info("[referral_attribution] token expired")
        return None
    except signing.BadSignature:
        logger.warning("[referral_attribution] invalid token signature")
        return None
    except Exception:
        logger.exception("[referral_attribution] unexpected error verifying token")
        return None


def resolve_active_referral_code(request):
    """
    Return (code, source) for the currently-valid referral attribution
    signal on *request* — session first (cheaper), then cookie — or
    (None, None) if neither resolves to a valid, unexpired token.
    Read-only — never mutates session/cookies.
    """
    session_token = request.session.get(_SESSION_KEY)
    if session_token:
        code = _verify_token(session_token)
        if code:
            return code, "session"

    cookie_token = request.COOKIES.get(_COOKIE_NAME)
    if cookie_token:
        code = _verify_token(cookie_token)
        if code:
            return code, "cookie"

    return None, None


def record_referral_click(request, response, code):
    """
    Called from referral_click_view(). First-valid-referral-wins: if a
    currently-valid attribution token already resolves (session or
    cookie), does nothing — never overrides an existing attribution
    signal with a later click. Otherwise mints a fresh signed token for
    *code* and writes it to both the session and a 30-day cookie on
    *response*. Mutates *response* in place; returns nothing.
    """
    existing_code, _source = resolve_active_referral_code(request)
    if existing_code:
        return

    token = _make_token(code)
    request.session[_SESSION_KEY] = token
    from django.conf import settings as _settings
    response.set_cookie(
        _COOKIE_NAME,
        token,
        max_age=_MAX_AGE,
        httponly=True,
        secure=not _settings.DEBUG,
        samesite="Lax",
    )


def attribute_user(user, request):
    """
    Called from register_view() immediately after a new User is created.
    Resolves the active referral signal (if any), looks up the Referral,
    and creates a permanent ReferralAttribution row.

    Fail-open by design: attribution must NEVER block a successful
    registration. Returns the created ReferralAttribution, or None if
    there was nothing to attribute or attribution could not be recorded
    (both are non-fatal — the caller does not need to check the result).
    """
    from django.db import IntegrityError, transaction

    from .models import Referral, ReferralAttribution

    code, source = resolve_active_referral_code(request)
    if not code:
        return None

    try:
        referral = Referral.objects.get(code=code)
    except Referral.DoesNotExist:
        logger.info("[referral_attribution] referral code=%s no longer exists — skipping", code)
        return None

    if referral.user_id == user.pk:
        # Not realistically reachable (user is brand new here), but a
        # free defensive guard against self-referral.
        logger.warning(
            "[referral_attribution] self-referral attempt ignored user=%d", user.pk,
        )
        return None

    try:
        # Its own savepoint — an IntegrityError here must not poison any
        # outer transaction the caller (register_view) is running in.
        with transaction.atomic():
            return ReferralAttribution.objects.create(
                referred_user=user, referral=referral, source=source,
            )
    except IntegrityError:
        # Expected duplicate — referred_user is a OneToOneField, so a
        # second attribution attempt for the same user is a DB-level
        # guarantee, not just an unenforced convention. Never fatal.
        logger.info(
            "[referral_attribution] duplicate attribution ignored (already attributed) user=%d",
            user.pk,
        )
        return None
    except Exception:
        # Unexpected — must never block registration, but must be loud
        # in logs (not silently swallowed like the expected case above).
        logger.exception(
            "[referral_attribution] unexpected error attributing user=%d to referral=%s",
            user.pk, code,
        )
        return None
