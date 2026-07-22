"""
simulator/auth_password_views.py
AUDIT-04b — Password change/reset audit trail.

Thin subclasses of Django's own django.contrib.auth.views classes. Every
override calls super() FIRST (or, where order matters — see
AuditedPasswordResetConfirmView — captures state that Django itself is
about to delete, still without altering what Django does), preserving
100% of Django's original behavior: password hashing, token generation,
token validation, and email sending are never reimplemented here. This
module only OBSERVES outcomes and records BrokerAuditEvent rows.

Never modifies simulator/views.py (AUDIT-04b design decision — this
domain gets its own module rather than growing the existing "god file").

Fail-open throughout: record_auth_event() already never raises (see
broker_audit.py), and the Redis correlation helpers below never raise
either — a Redis outage degrades correlation_id to None, it never blocks
a password change or reset.
"""
import hashlib
import logging
import uuid

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import INTERNAL_RESET_SESSION_TOKEN

from .observability import get_client_ip
from . import broker_audit as _audit

log = logging.getLogger("simulator.auth_password_views")

_REDIS_KEY_PREFIX = "trx:pwreset:corr:"


def _redis():
    from .observability import _get_redis
    url = getattr(settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
    return _get_redis(url)


def _token_key(token: str) -> str:
    return _REDIS_KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def _get_or_create_correlation(token: str) -> uuid.UUID:
    """
    Atomic get-or-create for a reset token's correlation_id.

    Django's default_token_generator is DETERMINISTIC — two reset requests
    for the same user, close enough together that nothing about the user
    changed, produce the IDENTICAL token string. This function's job is to
    make that fact visible in the audit trail: the FIRST caller to see a
    given token defines its correlation_id (via SET ... NX EX); every
    subsequent caller for that same token — whether a second "requested"
    fired by a duplicate email submission, or the "completed" event fired
    later when the link is used — reads that same value back. Nothing ever
    overwrites an existing entry.

    Fail-open: any Redis error returns a freshly minted, unpersisted
    uuid4 — the caller's own event still gets a valid correlation_id, it
    just cannot be recovered by a later lookup for the same token.
    """
    key = _token_key(token)
    candidate = uuid.uuid4()
    try:
        r = _redis()
        if r.set(key, str(candidate), nx=True, ex=settings.PASSWORD_RESET_TIMEOUT):
            return candidate
        existing = r.get(key)
        return uuid.UUID(existing.decode()) if existing else candidate
    except Exception as exc:
        log.warning("[auth_password_views] correlation get-or-create failed: %r", exc)
        return candidate


def _delete_correlation(token: str) -> None:
    """Best-effort cleanup after a reset completes. Fail-open: never raises."""
    try:
        _redis().delete(_token_key(token))
    except Exception as exc:
        log.warning("[auth_password_views] correlation cleanup failed: %r", exc)


class AuditedPasswordResetForm(PasswordResetForm):
    """
    Adds exactly one behavior on top of Django's PasswordResetForm:
    after send_mail() successfully queues the reset email for a real,
    matched user, record auth.password_reset_requested for that user.

    Never fires for an email that matches no user — get_users() (Django's
    own method, already used internally by save()) simply yields nothing
    in that case, and this class adds nothing when it isn't called. The
    HTTP response PasswordResetView returns is identical either way,
    entirely unaffected by this class — the anti-enumeration guarantee
    Django already provides is untouched.
    """

    def __init__(self, *args, request_ip=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_ip = request_ip

    def send_mail(
        self, subject_template_name, email_template_name, context,
        from_email, to_email, html_email_template_name=None,
    ):
        super().send_mail(
            subject_template_name, email_template_name, context,
            from_email, to_email, html_email_template_name,
        )
        user  = context["user"]
        token = context["token"]
        correlation_id = _get_or_create_correlation(token)
        _audit.record_auth_event(
            event_type=_audit.EV_PASSWORD_RESET_REQUESTED,
            severity=_audit.Severity.WARNING,
            user=user,
            correlation_id=correlation_id,
            source_module="simulator.auth_password_views",
            description=f"Password reset requested for user #{user.pk}",
            metadata={"ip": self._request_ip},
        )


class AuditedPasswordResetView(auth_views.PasswordResetView):
    """Swaps in AuditedPasswordResetForm and passes the client IP into it
    via get_form_kwargs() — Django's own documented extension point for
    this, not a hack on top of the form's __init__ contract."""
    form_class = AuditedPasswordResetForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request_ip"] = get_client_ip(self.request)
        return kwargs


class AuditedPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """
    Records auth.password_reset_completed — but only after Django's own
    form_valid() actually succeeds (if it raises, this method exits via
    the exception and nothing below is ever reached — no swallowing).

    Order matters: Django's OWN form_valid() deletes
    request.session[INTERNAL_RESET_SESSION_TOKEN] as part of its own
    cleanup, immediately after form.save(). The real token MUST be read
    before calling super() — reading it after would find it already gone.
    """

    def form_valid(self, form):
        real_token = self.request.session.get(INTERNAL_RESET_SESSION_TOKEN)
        correlation_id = _get_or_create_correlation(real_token) if real_token else None

        response = super().form_valid(form)  # Django's own save() + session cleanup + redirect

        _audit.record_auth_event(
            event_type=_audit.EV_PASSWORD_RESET_COMPLETED,
            severity=_audit.Severity.WARNING,
            user=self.user,
            correlation_id=correlation_id,
            source_module="simulator.auth_password_views",
            description=f"Password reset completed for user #{self.user.pk}",
            metadata={"ip": get_client_ip(self.request)},
        )
        if real_token:
            _delete_correlation(real_token)
        return response


class AuditedPasswordChangeView(auth_views.PasswordChangeView):
    """Records auth.password_changed after Django's own form_valid()
    (password set + update_session_auth_hash) succeeds."""

    def form_valid(self, form):
        response = super().form_valid(form)
        _audit.record_auth_event(
            event_type=_audit.EV_PASSWORD_CHANGED,
            severity=_audit.Severity.WARNING,
            user=self.request.user,
            source_module="simulator.auth_password_views",
            description=f"Password changed for user #{self.request.user.pk}",
            metadata={"ip": get_client_ip(self.request)},
        )
        return response
