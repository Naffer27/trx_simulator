"""
simulator/email_verification.py
Stateless signed-token email verification.

Token = Django signing.dumps(user_pk, salt=_SALT) — expires in 48 h.
No DB row for the token; only the EmailVerification model stores the verified flag.
Never raises — callers handle None return values.
"""
import logging
from django.core import signing

_log = logging.getLogger("simulator.email_verification")

_SALT    = "email-verification"
_MAX_AGE = 48 * 3600  # 48 hours


def make_email_token(user_pk: int) -> str:
    """Return a signed URL-safe token encoding *user_pk*."""
    return signing.dumps(user_pk, salt=_SALT)


def verify_email_token(token: str) -> int | None:
    """
    Return the user_pk encoded in *token* if valid and unexpired.
    Returns None on expired, invalid, or malformed tokens.
    """
    try:
        return signing.loads(token, salt=_SALT, max_age=_MAX_AGE)
    except signing.SignatureExpired:
        _log.info("[email_verification] token expired")
        return None
    except signing.BadSignature:
        _log.warning("[email_verification] invalid token signature")
        return None
    except Exception as exc:
        _log.error("[email_verification] unexpected error: %s", exc)
        return None
