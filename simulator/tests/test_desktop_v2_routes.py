from django.test import TestCase, override_settings
from django.urls import reverse
from simulator.tests.factories import make_account, make_user

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DesktopV2RoutesTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type='DEMO', tier=None)

    def test_requires_login(self):
        self.assertEqual(self.client.get(reverse('simulator:dashboard_v2')).status_code, 302)

    def test_new_panel_and_return_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('simulator:dashboard_v2_account', args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'simulator/trade/desktop_v2.html')
        self.assertContains(response, reverse('simulator:home'))

    def test_classic_panel_preserved(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('simulator:dashboard_account', args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'simulator/trade/shell.html')
        self.assertContains(response, 'Panel 2')

    def test_other_users_account_not_accessible(self):
        self.client.force_login(make_user())
        response = self.client.get(reverse('simulator:dashboard_v2_account', args=[self.account.pk]))
        self.assertRedirects(response, reverse('simulator:accounts'), fetch_redirect_response=False)

    def test_home_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('simulator:dashboard_v2_account', args=[self.account.pk]))
        self.assertContains(response, '← Volver al inicio')
        self.assertContains(response, 'href="' + reverse('simulator:home') + '"')

    def test_phone_keeps_native_mobile_and_same_account(self):
        self.client.force_login(self.user)
        for ua in ['Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)', 'Mozilla/5.0 (Linux; Android 14) Mobile']:
            for route in ['simulator:dashboard_account', 'simulator:dashboard_v2_account']:
                with self.subTest(ua=ua, route=route):
                    response = self.client.get(reverse(route, args=[self.account.pk]), HTTP_USER_AGENT=ua)
                    self.assertEqual(response.status_code, 200)
                    self.assertTemplateUsed(response, 'simulator/trade/mobile.html')
                    self.assertEqual(response.context['account_id'], self.account.pk)

    def test_session_account_is_shared_between_routes(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['account_id'] = self.account.pk
        session.save()
        for route in ['simulator:home', 'simulator:dashboard', 'simulator:dashboard_v2']:
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['account'].pk, self.account.pk)

    def test_one_updated_entry_on_desktop_and_mobile(self):
        self.client.force_login(self.user)
        for ua in ['Mozilla/5.0 iPhone', 'Mozilla/5.0 Android Mobile', 'Mozilla/5.0 Macintosh']:
            response = self.client.get(reverse('simulator:home'), HTTP_USER_AGENT=ua)
            self.assertContains(response, 'href="' + reverse('simulator:dashboard_v2') + '"')
            self.assertNotContains(response, 'href="' + reverse('simulator:dashboard') + '"')
            self.assertNotContains(response, '> Panel 2')

    def test_accounts_links_preserve_each_account_in_new_panel(self):
        second = make_account(self.user, account_type='DEMO', tier=None)
        self.client.force_login(self.user)
        response = self.client.get(reverse('simulator:accounts'))
        for account in (self.account, second):
            self.assertContains(response, 'href="' + reverse('simulator:dashboard_v2_account', args=[account.pk]) + '"')
            self.assertNotContains(response, 'href="' + reverse('simulator:dashboard_account', args=[account.pk]) + '"')
