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
        self.assertContains(response, reverse('simulator:dashboard_account', args=[self.account.pk]))

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
