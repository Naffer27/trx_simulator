from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from simulator.tests.factories import make_account, make_user


class DemoAccountCardsTests(TestCase):
    def test_profiles_and_fees_are_visible_without_changing_balances(self):
        user = make_user()
        accounts = []
        for name, fee in [('Demo ECN — reglas reales', '7.00'), ('Demo VIP — reglas reales', '3.50')]:
            acc = make_account(user, account_type='DEMO', balance=Decimal('100000'))
            acc.product_name_snapshot = name
            acc.commission_per_lot_snapshot = Decimal(fee)
            acc.save(update_fields=['product_name_snapshot', 'commission_per_lot_snapshot'])
            accounts.append(acc)
        self.client.force_login(user)
        for language in ('es', 'en'):
            self.client.cookies['django_language'] = language
            response = self.client.get(reverse('simulator:accounts'))
            self.assertEqual(response.status_code, 200)
            for text in ('Demo ECN', 'Demo VIP', 'USD 7.00', 'USD 3.50', '$100,000.00'):
                self.assertContains(response, text)
        for acc in accounts:
            acc.refresh_from_db()
            self.assertEqual(acc.balance, Decimal('100000'))
