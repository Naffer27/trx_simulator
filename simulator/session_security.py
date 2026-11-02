"""Server-enforced interactive session expiry, including existing trading sockets."""
import hashlib
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.contrib.auth import logout, get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from .models import LoginSessionLease


def enabled():
    return getattr(settings, 'INTERACTIVE_SESSION_ENABLED', False)


def seconds():
    return max(30, int(getattr(settings, 'INTERACTIVE_SESSION_SECONDS', 60)))


def digest(key):
    return hashlib.sha256((key or '').encode()).hexdigest()


def create_lease(request, user):
    if not request.session.session_key:
        request.session.save()
    now = timezone.now()
    LoginSessionLease.objects.update_or_create(
        session_digest=digest(request.session.session_key),
        defaults={'user': user, 'expires_at': now + timedelta(seconds=seconds()),
                  'absolute_expires_at': now + timedelta(hours=12)},
    )


@receiver(user_logged_in)
def login_lease(sender, request, user, **kwargs):
    if enabled():
        create_lease(request, user)


@receiver(user_logged_out)
def logout_lease(sender, request, **kwargs):
    if enabled() and request:
        LoginSessionLease.objects.filter(session_digest=digest(request.session.session_key)).update(expires_at=timezone.now())


def lease_valid(session_key, user_id):
    now = timezone.now()
    return bool(session_key and LoginSessionLease.objects.filter(
        session_digest=digest(session_key), user_id=user_id,
        expires_at__gt=now, absolute_expires_at__gt=now,
    ).exists())


def socket_session_valid(scope):
    if not enabled():
        return True
    user = scope.get('user')
    key = getattr(scope.get('session'), 'session_key', None)
    if not user or not user.is_authenticated or not lease_valid(key, user.pk):
        return False
    # Fresh store: the Channels scope otherwise retains a logged-out session.
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=key)
    data = store.load()
    if str(data.get('_auth_user_id')) != str(user.pk):
        return False
    current = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    return bool(current and data.get('_auth_user_hash') == current.get_session_auth_hash())


class InteractiveSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if enabled() and request.user.is_authenticated:
            key = digest(request.session.session_key)
            # One-time bootstrap for sessions already present at deployment.
            if not LoginSessionLease.objects.filter(session_digest=key).exists():
                create_lease(request, request.user)
            if not lease_valid(request.session.session_key, request.user.pk):
                logout(request)
                if request.method != 'GET' or 'application/json' in request.headers.get('Accept', ''):
                    return JsonResponse({'error': 'session_expired', 'login_url': reverse('simulator:login')}, status=401)
                return redirect('simulator:login')
            if request.method == 'GET' and request.headers.get('Sec-Fetch-User') == '?1' and request.headers.get('Sec-Fetch-Mode') == 'navigate':
                LoginSessionLease.objects.filter(session_digest=key).update(expires_at=timezone.now() + timedelta(seconds=seconds()))
        response = self.get_response(request)
        if enabled() and request.user.is_authenticated:
            response['Cache-Control'] = 'no-store, private'
        return response


@never_cache
@require_POST
def activity(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'session_expired'}, status=401)
    if not enabled():
        return JsonResponse({'enabled': False})
    # Browser sends the age of its last genuine interaction, not price updates.
    try:
        age = float(request.POST.get('idle_seconds', '0'))
        if not 0 <= age < seconds():
            raise ValueError()
    except (ValueError, OverflowError):
        return JsonResponse({'error': 'invalid_activity'}, status=400)
    now = timezone.now()
    updated = LoginSessionLease.objects.filter(
        session_digest=digest(request.session.session_key), user=request.user,
        expires_at__gt=now, absolute_expires_at__gt=now,
    ).update(expires_at=now + timedelta(seconds=seconds() - age))
    return JsonResponse({'enabled': True, 'timeout': seconds()}, status=200 if updated else 401)


@never_cache
@require_POST
def lock(request):
    # An expired/background tab cannot log out a newly signed-in tab: it sends
    # the digest of the session it rendered. No secret/session key is exposed.
    if request.POST.get('session') == digest(request.session.session_key):
        logout(request)
    return JsonResponse({'login_url': reverse('simulator:login')})


def context(request):
    active = enabled() and request.user.is_authenticated
    return {'interactive_session_enabled': active,
            'interactive_session_seconds': seconds(),
            'interactive_session_remaining': max(0, int((LoginSessionLease.objects.get(session_digest=digest(request.session.session_key)).expires_at - timezone.now()).total_seconds())) if active else 0,
            'interactive_session_digest': digest(request.session.session_key) if active else '',
            'login_passkeys_enabled': getattr(settings, 'LOGIN_PASSKEYS_ENABLED', False)}
