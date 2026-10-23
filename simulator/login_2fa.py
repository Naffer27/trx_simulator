"""Password + TOTP login. No authenticated session exists before both factors.

This module does not change the withdrawal/dashboard TOTP verifier.
Challenges expire after five minutes and are bound to password and device.
"""
from datetime import timedelta
import hashlib
import re

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.core import signing
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import TOTPDevice, TradingAccount
from .two_factor import _decrypt_secret, mark_session_verified

KEY = 'pending_login_totp'
SALT = 'moneybroker.login-totp.v1'


def fingerprint(device):
    return hashlib.sha256(device.secret.encode()).hexdigest()


def begin_login_challenge(request, user):
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    if not device:
        # Never carry an earlier verified flag into a new login.
        for key in ('2fa_verified', '2fa_next', '2fa_user_pk', KEY):
            request.session.pop(key, None)
        return None
    backend = user.backend
    logout(request)
    request.session[KEY] = signing.dumps({
        'uid': user.pk, 'hash': user.get_session_auth_hash(),
        'device': device.pk, 'fingerprint': fingerprint(device), 'backend': backend,
    }, salt=SALT)
    return redirect('simulator:login_2fa')


@never_cache
@require_http_methods(['GET', 'POST'])
def login_2fa_view(request):
    try:
        pending = signing.loads(request.session.get(KEY, ''), salt=SALT, max_age=300)
        user = get_user_model().objects.get(pk=pending['uid'], is_active=True)
        if (not constant_time_compare(pending['hash'], user.get_session_auth_hash())
                or pending['backend'] not in settings.AUTHENTICATION_BACKENDS):
            raise ValueError('Stale credentials')
    except (signing.BadSignature, KeyError, ValueError, get_user_model().DoesNotExist):
        request.session.pop(KEY, None)
        return redirect('simulator:login')

    error = None
    status = 200
    verified = False
    # Serialize verification per device; used time steps cannot authenticate twice.
    with transaction.atomic():
        device = TOTPDevice.objects.select_for_update().filter(
            pk=pending['device'], user=user, confirmed=True).first()
        if not device or not constant_time_compare(fingerprint(device), pending['fingerprint']):
            request.session.pop(KEY, None)
            return redirect('simulator:login')
        if request.method == 'POST':
            now = timezone.now()
            if device.login_locked_until and device.login_locked_until > now:
                error, status = 'Demasiados intentos. Espera cinco minutos.', 429
            else:
                if device.login_locked_until:
                    device.login_failures = 0
                    device.login_locked_until = None
                code = request.POST.get('code', '').strip()
                matched = None
                try:
                    totp = pyotp.TOTP(_decrypt_secret(device.secret))
                    step = int(now.timestamp()) // totp.interval
                    if re.fullmatch(r'[0-9]{6}', code):
                        for candidate in (step, step - 1, step + 1):
                            if candidate > device.login_last_step and constant_time_compare(totp.generate_otp(candidate), code):
                                matched = candidate
                                break
                except Exception:
                    # Fail closed if the configured encryption key cannot decrypt.
                    error = 'No se pudo verificar el código. Contacta con soporte.'
                if matched is not None:
                    device.login_last_step = matched
                    device.login_failures = 0
                    device.login_locked_until = None
                    verified = True
                else:
                    device.login_failures += 1
                    if device.login_failures >= 5:
                        device.login_locked_until = now + timedelta(minutes=5)
                    error = error or 'Código incorrecto o ya utilizado. Usa el próximo código de tu app.'
                device.save(update_fields=['login_last_step', 'login_failures', 'login_locked_until'])
    if verified:
        request.session.pop(KEY, None)
        login(request, user, backend=pending['backend'])
        mark_session_verified(request)
        active = TradingAccount.objects.filter(user=user, status='Activo').order_by('-id').first()
        if active:
            request.session['account_id'] = active.id
        from .observability import security_log, get_client_ip
        from .audit import log_audit, EV_AUTH_LOGIN_SUCCESS
        security_log('auth.login_success', level='info', user_id=user.pk, username=user.username)
        log_audit(request, EV_AUTH_LOGIN_SUCCESS, 'Login success after TOTP', detail={'ip': get_client_ip(request)})
        return redirect('simulator:home')
    if request.method == 'POST':
        from .observability import security_log, get_client_ip
        security_log('auth.2fa_failed', user_id=user.pk, ip=get_client_ip(request), reason='rate_limited' if status == 429 else 'invalid_code')
    return render(request, 'simulator/totp_verify.html', {'error': error, 'login_challenge': True}, status=status)
