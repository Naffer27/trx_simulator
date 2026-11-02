import base64
import hashlib
import json
import secrets
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import cbor2
import pyotp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, Client
from django.utils import timezone
from webauthn.helpers import bytes_to_base64url as enc, base64url_to_bytes as dec
from simulator.models import LoginSessionLease, LoginPasskey, LoginPasskeyChallenge, TOTPDevice
from simulator.session_security import digest, socket_session_valid


@override_settings(INTERACTIVE_SESSION_ENABLED=True, INTERACTIVE_SESSION_SECONDS=60,
                   LOGIN_PASSKEYS_ENABLED=True, PASSKEY_RP_ID='example.com',
                   PASSKEY_ORIGINS=['https://example.com'])
class PasskeySessionTests(TestCase):
    def setUp(self):
        limiter = patch('simulator.ratelimit.rate_check', return_value=(True, 0))
        limiter.start(); self.addCleanup(limiter.stop)
        self.user = get_user_model().objects.create_user('passkeyuser', password='Strong!Pass88')
        self.raw = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.user, confirmed=True,
            secret='b64:' + base64.b64encode(self.raw.encode()).decode())
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.cid = secrets.token_bytes(32)
        public = self.private.public_key().public_numbers()
        self.cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: public.x.to_bytes(32, 'big'), -3: public.y.to_bytes(32, 'big')})

    def enroll(self):
        self.client.force_login(self.user)
        response = self.client.post('/passkeys/options/', {'purpose': 'register', 'password': 'Strong!Pass88', 'otp_code': pyotp.TOTP(self.raw).now()})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.handle = data['options']['user']['id']
        client = json.dumps({'type': 'webauthn.create', 'challenge': data['options']['challenge'], 'origin': 'https://example.com'}).encode()
        auth = hashlib.sha256(b'example.com').digest() + b'\x45' + (0).to_bytes(4,'big') + bytes(16) + len(self.cid).to_bytes(2,'big') + self.cid + self.cose
        credential = {'id':enc(self.cid), 'rawId':enc(self.cid), 'type':'public-key',
            'response': {'clientDataJSON':enc(client), 'attestationObject':enc(cbor2.dumps({'fmt':'none','attStmt':{},'authData':auth}))}}
        result = self.client.post('/passkeys/verify/', json.dumps({'token':data['token'], 'credential':credential}), content_type='application/json')
        self.assertEqual(result.status_code, 200, result.content)
        self.client.logout()

    def assertion(self, origin='https://example.com', flags=5, count=1, bad_signature=False):
        data = self.client.post('/passkeys/options/', {'purpose':'login'}).json()
        client = json.dumps({'type':'webauthn.get', 'challenge':data['options']['challenge'], 'origin':origin}).encode()
        auth = hashlib.sha256(b'example.com').digest() + bytes([flags]) + count.to_bytes(4,'big')
        signature = self.private.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if bad_signature: signature = bytes(70)
        return {'token':data['token'], 'credential':{'id':enc(self.cid),'rawId':enc(self.cid),'type':'public-key',
            'response':{'clientDataJSON':enc(client),'authenticatorData':enc(auth),'signature':enc(signature),'userHandle':self.handle}}}

    def submit(self, body):
        return self.client.post('/passkeys/verify/', json.dumps(body), content_type='application/json')

    def test_real_crypto_registration_login_and_replay(self):
        self.enroll()
        body=self.assertion()
        self.assertEqual(self.submit(body).status_code,200)
        self.assertEqual(self.client.session['_auth_user_id'],str(self.user.pk))
        self.assertNotIn('2fa_verified',self.client.session)
        self.assertEqual(self.submit(body).status_code,400)
        self.assertEqual(LoginPasskey.objects.get().sign_count,1)

    def test_wrong_origin_signature_and_missing_user_verification(self):
        self.enroll()
        for kwargs in ({'origin':'https://evil.example'}, {'flags':1}, {'bad_signature':True}):
            with self.subTest(kwargs=kwargs):
                body=self.assertion(**kwargs)
                self.assertEqual(self.submit(body).status_code,400)
                self.assertTrue(LoginPasskeyChallenge.objects.get(token=body['token']).used)
                self.assertNotIn('_auth_user_id',self.client.session)

    def test_challenge_expiry_and_browser_binding(self):
        self.enroll();body=self.assertion()
        self.assertEqual(Client().post('/passkeys/verify/',json.dumps(body),content_type='application/json').status_code,400)
        LoginPasskeyChallenge.objects.filter(token=body['token']).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.submit(body).status_code,400)

    def test_user_handle_password_change_disable_and_revocation(self):
        self.enroll();body=self.assertion()
        body['credential']['response']['userHandle']=enc(b'wrong-user')
        self.assertEqual(self.submit(body).status_code,400)
        body=self.assertion();self.user.set_password('New!Password88');self.user.save()
        self.assertEqual(self.submit(body).status_code,400)
        key=LoginPasskey.objects.get();key.auth_hash=self.user.get_session_auth_hash();key.save()
        self.user.is_active=False;self.user.save()
        self.assertEqual(self.submit(self.assertion()).status_code,400)
        self.user.is_active=True;self.user.save();body=self.assertion();key.delete()
        self.assertEqual(self.submit(body).status_code,400)

    def test_enrollment_requires_password_and_totp(self):
        self.assertEqual(self.client.post('/passkeys/options/',{'purpose':'register'}).status_code,403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/passkeys/options/',{'purpose':'register','password':'Strong!Pass88'}).status_code,403)
        self.assertFalse(LoginPasskey.objects.exists())

    def test_password_login_still_requires_totp(self):
        response=self.client.post('/login/',{'username':self.user.username,'password':'Strong!Pass88'})
        self.assertEqual(response.url,'/login/2fa/')
        self.assertNotIn('_auth_user_id',self.client.session)

    def test_polling_does_not_renew_and_expired_post_is_rejected(self):
        self.client.force_login(self.user)
        lease=LoginSessionLease.objects.get()
        self.client.get('/profile/')
        lease.refresh_from_db();self.assertLessEqual(lease.expires_at-timezone.now(),timedelta(seconds=60))
        original=lease.expires_at
        self.client.get('/profile/')
        lease.refresh_from_db();self.assertEqual(lease.expires_at,original)
        lease.expires_at=timezone.now()-timedelta(seconds=1);lease.save()
        response=self.client.post('/session/activity/',{'idle_seconds':'0'})
        self.assertEqual(response.status_code,401)
        self.assertNotIn('_auth_user_id',self.client.session)

    def test_activity_renews_only_live_lease(self):
        self.client.force_login(self.user)
        lease=LoginSessionLease.objects.get();lease.expires_at=timezone.now()+timedelta(seconds=10);lease.save()
        self.assertEqual(self.client.post('/session/activity/',{'idle_seconds':'2'}).status_code,200)
        lease.refresh_from_db();self.assertGreater(lease.expires_at,timezone.now()+timedelta(seconds=55))
        self.assertEqual(self.client.post('/session/activity/',{'idle_seconds':'nan'}).status_code,400)
        lease.absolute_expires_at=timezone.now()-timedelta(seconds=1);lease.save()
        self.assertEqual(self.client.get('/profile/').url,'/login/')

    def test_websocket_checks_expiry_and_logout(self):
        self.client.force_login(self.user)
        session=self.client.session
        scope={'session':session,'user':self.user}
        self.assertTrue(socket_session_valid(scope))
        LoginSessionLease.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertFalse(socket_session_valid(scope))
        LoginSessionLease.objects.update(expires_at=timezone.now()+timedelta(seconds=60))
        self.client.logout()
        self.assertFalse(socket_session_valid(scope))

    def test_old_tab_cannot_logout_new_session(self):
        self.client.force_login(self.user);old=digest(self.client.session.session_key)
        self.client.logout();self.client.force_login(self.user)
        self.client.post('/session/lock/',{'session':old})
        self.assertIn('_auth_user_id',self.client.session)

    def test_csrf_is_required(self):
        guarded=Client(enforce_csrf_checks=True)
        self.assertEqual(guarded.post('/passkeys/options/',{'purpose':'login'}).status_code,403)
        guarded.force_login(self.user)
        self.assertEqual(guarded.post('/session/activity/',{}).status_code,403)

    def test_expired_socket_rejects_order_before_dispatch(self):
        from asgiref.sync import async_to_sync
        from unittest.mock import AsyncMock
        from simulator.consumers import TradingConsumer
        consumer=TradingConsumer()
        consumer._interactive_session_valid=AsyncMock(return_value=False)
        consumer.close=AsyncMock()
        consumer._order_new=AsyncMock()
        async_to_sync(consumer.receive)(json.dumps({'action':'order:new'}))
        consumer.close.assert_awaited_once_with(code=4001)
        consumer._order_new.assert_not_awaited()

    def test_counter_rollback_and_revoke_require_fresh_auth(self):
        self.enroll()
        self.assertEqual(self.submit(self.assertion(count=2)).status_code,200)
        key=LoginPasskey.objects.get()
        self.assertEqual(self.client.post('/passkeys/revoke/',{'key_id':key.pk}).status_code,403)
        self.client.logout()
        self.assertEqual(self.submit(self.assertion(count=1)).status_code,400)

    def test_profile_and_admin_have_idle_guard(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        self.client.force_login(self.user)
        response=self.client.get('/profile/')
        self.assertContains(response,'session_lock.js')
        self.assertContains(response,'/profile/passkeys/')
        request=RequestFactory().get('/admin/')
        request.user=self.user;request.session=self.client.session
        html=render_to_string('admin/base_site.html',{},request=request)
        self.assertIn('session_lock.js',html)
