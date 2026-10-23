import base64
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.core import signing
import pyotp
from simulator.models import TOTPDevice
from simulator.login_2fa import KEY, SALT

@override_settings(BROKER_ACCESS_CODE='obsolete')
class LoginTOTPTests(TestCase):
    def setUp(self):
        self.patcher=patch('simulator.ratelimit.rate_check',return_value=(True,0))
        self.patcher.start();self.addCleanup(self.patcher.stop)
        self.user=get_user_model().objects.create_user('loginotp','otp@example.com','StrongPass!77')
        self.raw=pyotp.random_base32()
        self.device=TOTPDevice.objects.create(user=self.user,confirmed=True,secret='b64:'+base64.b64encode(self.raw.encode()).decode())
    def begin(self):
        return self.client.post('/login/',{'username':'loginotp','password':'StrongPass!77'})
    def verify(self,code=None):
        return self.client.post('/login/2fa/',{'code':code or pyotp.TOTP(self.raw).now()})
    def test_password_alone_cannot_access_private_pages(self):
        r=self.begin();self.assertEqual(r.url,'/login/2fa/')
        self.assertNotIn('_auth_user_id',self.client.session)
        self.assertEqual(self.client.get('/accounts/').status_code,302)
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_valid_totp_completes_login(self):
        self.begin();r=self.verify();self.assertEqual(r.status_code,302)
        self.assertEqual(self.client.session['_auth_user_id'],str(self.user.pk))
        self.assertTrue(self.client.session['2fa_verified'])
        self.assertNotIn(KEY,self.client.session)
    def test_wrong_code_does_not_authenticate(self):
        self.begin();self.assertEqual(self.verify('nototp').status_code,200)
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_database_limit_survives_new_password_challenge(self):
        self.begin()
        for _ in range(5):self.verify('nototp')
        self.begin();self.assertEqual(self.verify().status_code,429)
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_used_code_cannot_log_in_again(self):
        self.begin();code=pyotp.TOTP(self.raw).now();self.verify(code)
        self.client.logout();self.begin();self.assertEqual(self.verify(code).status_code,200)
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_missing_challenge_redirects(self):
        self.assertEqual(self.verify().url,'/login/')
    def test_expired_challenge_redirects(self):
        self.begin()
        with patch('django.core.signing.time.time',return_value=__import__('time').time()+301):
            self.assertEqual(self.verify().url,'/login/')
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_password_change_invalidates_pending_challenge(self):
        self.begin();self.user.set_password('OtherStrong!99');self.user.save()
        self.assertEqual(self.verify().url,'/login/')
    def test_disabled_user_cannot_complete(self):
        self.begin();self.user.is_active=False;self.user.save()
        self.assertEqual(self.verify().url,'/login/')
    def test_removed_device_does_not_bypass_second_factor(self):
        self.begin();self.device.delete();self.assertEqual(self.verify().url,'/login/')
    def test_replaced_device_invalidates_pending_challenge(self):
        self.begin();self.device.secret='b64:'+base64.b64encode(pyotp.random_base32().encode()).decode();self.device.save()
        self.assertEqual(self.verify().url,'/login/')
    def test_users_without_confirmed_device_keep_normal_login(self):
        self.device.confirmed=False;self.device.save()
        self.assertNotEqual(self.begin().url,'/login/2fa/')
        self.assertIn('_auth_user_id',self.client.session)
