"""Optional, verified-user passkeys for LOGIN ONLY. Withdrawal 2FA is unchanged."""
import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login, authenticate
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from webauthn import (generate_registration_options, generate_authentication_options,
                     verify_registration_response, verify_authentication_response, options_to_json)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                    UserVerificationRequirement, PublicKeyCredentialDescriptor)
from .models import LoginPasskey, LoginPasskeyChallenge, TradingAccount, TOTPDevice
from .ratelimit import rate_limit
from .observability import security_log
from .session_security import digest
from .two_factor import verify_totp


def config():
    if not getattr(settings, 'LOGIN_PASSKEYS_ENABLED', False):
        raise Http404()
    return settings.PASSKEY_RP_ID, settings.PASSKEY_ORIGINS


def binding(request):
    if not request.session.get('passkey_binding'):
        request.session['passkey_binding'] = secrets.token_urlsafe(32)
    return digest(request.session['passkey_binding'])


def fresh_auth(request):
    if not request.user.is_authenticated:
        return False
    user = authenticate(request, username=request.user.get_username(), password=request.POST.get('password', ''))
    return bool(user and user.pk == request.user.pk
                and TOTPDevice.objects.filter(user=user, confirmed=True).exists()
                and verify_totp(user, request.POST.get('otp_code', '').strip()))


@login_required
@never_cache
@ensure_csrf_cookie
def manage(request):
    config()
    return render(request, 'simulator/passkeys.html', {'keys': LoginPasskey.objects.filter(user=request.user), 'active_section': 'profile'})


@never_cache
@require_POST
@rate_limit('passkey_options', limit=10, window=60, by='ip')
def options(request):
    rp, _ = config()
    register = request.POST.get('purpose') == 'register'
    if register and not fresh_auth(request):
        return JsonResponse({'error': 'Confirma tu contraseña y el código 2FA.'}, status=403)
    challenge = secrets.token_bytes(32)
    handle = secrets.token_bytes(32)
    if register:
        result = generate_registration_options(
            rp_id=rp, rp_name='Money Broker', user_name=request.user.get_username(), user_id=handle,
            challenge=challenge, timeout=60000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.credential_id))
                                 for k in LoginPasskey.objects.filter(user=request.user)],
        )
    else:
        result = generate_authentication_options(rp_id=rp, challenge=challenge, timeout=60000,
                                                 user_verification=UserVerificationRequirement.REQUIRED)
    bound = binding(request)
    # Only the latest ceremony in this browser may complete.
    LoginPasskeyChallenge.objects.filter(session_digest=bound, used=False).update(used=True)
    record = LoginPasskeyChallenge.objects.create(
        token=secrets.token_hex(32), session_digest=bound, purpose='register' if register else 'login',
        user=request.user if register else None, challenge=challenge,
        user_handle=bytes_to_base64url(handle) if register else '',
        auth_hash=request.user.get_session_auth_hash() if register else '',
        expires_at=timezone.now() + timedelta(seconds=120))
    return JsonResponse({'token': record.token, 'options': json.loads(options_to_json(result))})


@never_cache
@require_POST
@rate_limit('passkey_verify', limit=10, window=60, by='ip')
def verify(request):
    rp, origins = config()
    if len(request.body) > 65536:
        return JsonResponse({'error': 'Respuesta demasiado grande.'}, status=400)
    try:
        body = json.loads(request.body)
        credential = body['credential']
        token = body['token']
        if not isinstance(credential, dict) or not isinstance(token, str):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'Respuesta inválida.'}, status=400)
    user = None
    success = False
    with transaction.atomic():
        ceremony = LoginPasskeyChallenge.objects.select_for_update().filter(token=token, used=False).first()
        if (not ceremony or ceremony.expires_at <= timezone.now()
                or ceremony.session_digest != binding(request)):
            return JsonResponse({'error': 'La solicitud caducó. Inténtalo de nuevo.'}, status=400)
        # A failed signature is also one-shot; returning commits consumption.
        ceremony.used = True
        ceremony.save(update_fields=['used'])
        try:
            if ceremony.purpose == 'register':
                if (not request.user.is_authenticated or request.user.pk != ceremony.user_id
                        or not constant_time_compare(ceremony.auth_hash, request.user.get_session_auth_hash())):
                    raise ValueError()
                verified = verify_registration_response(
                    credential=credential, expected_challenge=bytes(ceremony.challenge), expected_rp_id=rp,
                    expected_origin=origins, require_user_verification=True)
                cid = bytes_to_base64url(verified.credential_id)
                if LoginPasskey.objects.filter(credential_id=cid).exists():
                    raise ValueError()
                LoginPasskey.objects.create(user=request.user, credential_id=cid,
                    public_key=verified.credential_public_key, sign_count=verified.sign_count,
                    user_handle=ceremony.user_handle, auth_hash=ceremony.auth_hash,
                    name=str(body.get('name') or 'Mi dispositivo')[:80])
            else:
                cid = bytes_to_base64url(base64url_to_bytes(credential['rawId']))
                key = LoginPasskey.objects.select_for_update().select_related('user').get(credential_id=cid)
                user = key.user
                if not user.is_active or not constant_time_compare(key.auth_hash, user.get_session_auth_hash()):
                    raise ValueError()
                handle = credential['response'].get('userHandle')
                if not handle or base64url_to_bytes(handle) != base64url_to_bytes(key.user_handle):
                    raise ValueError()
                verified = verify_authentication_response(
                    credential=credential, expected_challenge=bytes(ceremony.challenge), expected_rp_id=rp,
                    expected_origin=origins, credential_public_key=bytes(key.public_key),
                    credential_current_sign_count=key.sign_count, require_user_verification=True)
                key.sign_count = verified.new_sign_count
                key.last_used_at = timezone.now()
                key.save(update_fields=['sign_count', 'last_used_at'])
            success = True
        except Exception:
            # Never log assertions/biometric data, and do not disclose accounts.
            success = False
    if not success:
        security_log("auth.passkey_failed", reason="verification_failed")
        return JsonResponse({'error': 'No se pudo verificar la passkey. Usa tu contraseña y 2FA o vuelve a intentarlo.'}, status=400)
    if user:
        # Do NOT set 2fa_verified: passkey login must not satisfy withdrawal or
        # admin/Treasury TOTP gates. Password login still uses login_2fa.py.
        for name in ('2fa_verified', '2fa_next', '2fa_user_pk', 'pending_login_totp'):
            request.session.pop(name, None)
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        account = TradingAccount.objects.filter(user=user, status='Activo').order_by('-id').first()
        if account:
            request.session['account_id'] = account.pk
    security_log('auth.passkey_login' if user else 'auth.passkey_registered', level='info', user_id=user.pk if user else request.user.pk)
    return JsonResponse({'redirect': reverse('simulator:home' if user else 'simulator:passkeys')})


@never_cache
@require_POST
@rate_limit('passkey_revoke', limit=5, window=60, by='ip')
def revoke(request):
    config()
    if not fresh_auth(request):
        return JsonResponse({'error': 'Confirma tu contraseña y el código 2FA.'}, status=403)
    try:
        key_id = int(request.POST.get('key_id', ''))
    except ValueError:
        return JsonResponse({'error': 'Passkey inválida.'}, status=400)
    LoginPasskey.objects.filter(user=request.user, pk=key_id).delete()
    security_log('auth.passkey_revoked', level='info', user_id=request.user.pk, key_id=key_id)
    return JsonResponse({'redirect': reverse('simulator:passkeys')})
