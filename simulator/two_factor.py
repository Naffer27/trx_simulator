"""
simulator/two_factor.py
TOTP-based 2FA using pyotp.

Encryption:
  TOTP secrets are stored encrypted using Fernet (AES-128-CBC + HMAC-SHA256).
  Set TOTP_ENCRYPTION_KEY in .env to a Fernet key generated with:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  If the key is not set (dev mode), secrets are base64-only (NOT for production).

Session flags:
  session["2fa_verified"]   = True   — set after successful TOTP code entry
  session["2fa_user_pk"]    = int    — set during the pending-2FA login step

Usage:
  from .two_factor import (
      generate_totp_secret, get_totp_uri, verify_totp_code,
      require_2fa, totp_session_verified,
  )
"""
import base64
import logging
import os

import pyotp

_log = logging.getLogger("simulator.security")

# ── Fernet encryption helper ──────────────────────────────────────────────────

def _get_fernet():
    """Return a Fernet instance from settings, or None if key not configured."""
    from django.conf import settings
    key = getattr(settings, "TOTP_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:
        _log.error("[2fa] TOTP_ENCRYPTION_KEY is invalid — secrets will not be encrypted: %r", exc)
        return None


def _encrypt_secret(plaintext: str) -> str:
    """
    Encrypt a TOTP secret for storage.
    Returns a prefixed string: "fernet:<base64>" or "b64:<base64>" (dev fallback).
    """
    f = _get_fernet()
    if f:
        token = f.encrypt(plaintext.encode()).decode()
        return f"fernet:{token}"
    # Dev fallback — NOT for production
    b64 = base64.b64encode(plaintext.encode()).decode()
    _log.warning("[2fa] TOTP_ENCRYPTION_KEY not set — storing secret as base64 (dev only)")
    return f"b64:{b64}"


def _decrypt_secret(stored: str) -> str:
    """Decrypt a stored TOTP secret string."""
    if stored.startswith("fernet:"):
        f = _get_fernet()
        if not f:
            raise ValueError("TOTP_ENCRYPTION_KEY not set but secret is Fernet-encrypted")
        return f.decrypt(stored[7:].encode()).decode()
    if stored.startswith("b64:"):
        return base64.b64decode(stored[4:]).decode()
    # Legacy: raw base32 secret (no prefix) — pass through
    return stored


# ── TOTP core ─────────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    """Generate a new random TOTP base32 secret (32 chars)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str) -> str:
    """
    Return an otpauth:// URI suitable for QR code generation.
    secret must be the RAW base32 secret (not encrypted).
    """
    from django.conf import settings
    issuer = getattr(settings, "TOTP_ISSUER_NAME", "Money Broker")
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name=issuer)


def verify_totp_code(stored_secret: str, code: str) -> bool:
    """
    Verify a 6-digit TOTP code against the stored (possibly encrypted) secret.
    Accepts b64:<base64> (dev) and fernet:<token> (prod) prefixes.
    Uses ±1 window (30 s drift tolerance — Google Authenticator compatible).
    Returns False on any error.
    """
    try:
        secret = _decrypt_secret(stored_secret)
        totp = pyotp.TOTP(secret)
        return totp.verify(str(code).strip(), valid_window=1)
    except Exception as exc:
        _log.error("[2fa] verify_totp_code error: %r", exc)
        return False


def verify_totp(user, code: str) -> bool:
    """
    High-level helper: look up the user's confirmed TOTPDevice and verify the code.

    Returns False when:
      - the user has no confirmed TOTPDevice
      - the code is blank or wrong
    Uses ±1 window (30 s drift tolerance).
    """
    from .models import TOTPDevice
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    if not device:
        return False
    return verify_totp_code(device.secret, code)


def generate_qr_png(uri: str) -> bytes:
    """Return a PNG QR code image as bytes for the given otpauth:// URI."""
    import qrcode
    import io
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Session helpers ───────────────────────────────────────────────────────────

_SESSION_VERIFIED_KEY = "2fa_verified"
_SESSION_PENDING_KEY  = "2fa_user_pk"


def totp_session_verified(request) -> bool:
    """Return True if the current session has passed 2FA verification."""
    return bool(request.session.get(_SESSION_VERIFIED_KEY))


def mark_session_verified(request) -> None:
    request.session[_SESSION_VERIFIED_KEY] = True
    request.session.modified = True


def set_pending_user(request, user_pk: int) -> None:
    request.session[_SESSION_PENDING_KEY] = user_pk
    request.session.modified = True


def get_pending_user_pk(request) -> int | None:
    return request.session.get(_SESSION_PENDING_KEY)


def clear_pending_user(request) -> None:
    request.session.pop(_SESSION_PENDING_KEY, None)
    request.session.modified = True


# ── Decorators ────────────────────────────────────────────────────────────────

def require_2fa(view_fn):
    """
    Decorator: require an active 2FA session for this view.
    If the user has 2FA enabled but hasn't verified this session,
    redirect to the TOTP verify page.
    If the user doesn't have 2FA enabled, pass through (no enforcement).
    """
    from functools import wraps
    from django.shortcuts import redirect
    from django.contrib.auth.decorators import login_required

    @wraps(view_fn)
    @login_required
    def wrapper(request, *args, **kwargs):
        # Check if user has a confirmed TOTPDevice
        from .models import TOTPDevice
        device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
        if device and not totp_session_verified(request):
            request.session["2fa_next"] = request.path
            return redirect("simulator:totp_verify")
        return view_fn(request, *args, **kwargs)
    return wrapper


def staff_require_2fa(view_fn):
    """
    Decorator: for staff-only views.
    If TOTP_STAFF_REQUIRED=True → enforce 2FA for all staff.
    Always checks login_required first.
    """
    from functools import wraps
    from django.shortcuts import redirect
    from django.conf import settings
    from django.contrib.auth.decorators import login_required

    @wraps(view_fn)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_staff):
            return redirect("simulator:login")

        enforce = getattr(settings, "TOTP_STAFF_REQUIRED", False)
        if enforce and not totp_session_verified(request):
            from .models import TOTPDevice
            device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
            if device:
                request.session["2fa_next"] = request.path
                return redirect("simulator:totp_verify")

        return view_fn(request, *args, **kwargs)
    return wrapper


# ── O.4a — Treasury Admin 2FA gate ─────────────────────────────────────────────
#
# treasury_2fa_required() is a pure predicate, not a decorator — O.4a-1 only
# adds it (and the settings flag it will eventually be paired with,
# TOTP_ADMIN_TREASURY_REQUIRED); nothing calls it yet. O.4a-2 will wire it
# into MoneyBrokerAdminSite.admin_view() (simulator/admin.py) as the single
# choke point for every /admin/ URL. It intentionally does NOT read
# TOTP_ADMIN_TREASURY_REQUIRED itself — that check belongs to the call site
# that decides whether the gate is active at all, keeping this function a
# simple, reusable "is this user a Treasury operator" test.
#
# Deliberately independent of staff_require_2fa() above: that decorator's
# "no confirmed device -> pass through" contract is frozen and tested
# (test_staff_2fa_enforcement.py) for ops_panel_view/the JSON staff APIs —
# it is not modified by this addition. The O.4a admin gate (O.4a-2) will
# treat "no confirmed device" as mandatory enrollment instead, which is why
# this is a new function rather than a change to staff_require_2fa().
_TREASURY_2FA_PERMISSIONS = (
    "simulator.can_submit_treasury_request",
    "simulator.can_review_treasury_request",
    "simulator.can_execute_treasury_request",
    "simulator.can_recover_treasury_execution",
)


def treasury_2fa_required(user) -> bool:
    """
    True if `user` must pass the O.4a Treasury Admin 2FA gate: any staff
    user holding at least one of the four Treasury permissions, or any
    superuser (checked explicitly rather than relying on has_perm()'s
    implicit superuser bypass, so a superuser cannot be used to route
    around the gate — O.4a Fase 0 design, scenario "superuser").

    Never raises: an unauthenticated or non-staff user simply returns
    False, same defensive shape as staff_require_2fa()'s own check.
    """
    if not (getattr(user, "is_authenticated", False) and user.is_staff):
        return False
    if user.is_superuser:
        return True
    return any(user.has_perm(perm) for perm in _TREASURY_2FA_PERMISSIONS)
