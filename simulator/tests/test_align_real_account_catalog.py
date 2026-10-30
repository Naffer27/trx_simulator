import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from simulator.models import AccountProduct, TradingAccount, LedgerEntry
from simulator.commercial_pricing import commercial_pricing_fields_from_account_product
from simulator.management.commands.align_real_account_catalog import ECN_BEFORE
from simulator.tests.factories import make_account, make_user, make_wallet


class AlignCatalogueTests(TestCase):
    def setUp(self):
        self.ecn = AccountProduct.objects.create(code='real-ecn', name='Real ECN', **ECN_BEFORE)
        self.standard = AccountProduct.objects.create(code='real-standard', name='Real Standard', family='REAL', product_type='STANDARD')
        self.account = make_account(account_type='STANDARD', account_product=self.standard,
            commercial_profile_snapshot=commercial_pricing_fields_from_account_product(self.standard))
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backup = str(Path(self.tmp.name) / 'before.json')

    def run_command(self, **kwargs):
        call_command('align_real_account_catalog', stdout=StringIO(), **kwargs)

    def test_preview_does_not_write(self):
        before = list(AccountProduct.objects.values())
        self.run_command()
        self.assertEqual(before, list(AccountProduct.objects.values()))
        self.assertFalse(Path(self.backup).exists())

    def test_apply_preserves_existing_accounts_and_is_idempotent(self):
        accounts = list(TradingAccount.objects.values())
        ledgers = list(LedgerEntry.objects.values())
        self.run_command(apply=True, backup=self.backup)
        self.ecn.refresh_from_db(); self.standard.refresh_from_db()
        self.assertEqual(self.ecn.min_deposit, Decimal('10'))
        self.assertEqual(self.ecn.max_leverage, 500)
        self.assertFalse(self.standard.is_active)
        vip = AccountProduct.objects.get(code='real-vip')
        self.assertEqual(vip.commission_per_lot, Decimal('3.50'))
        self.assertEqual(vip.min_deposit, Decimal('100000'))
        self.assertEqual(vip.product_type, 'ECN')
        self.assertEqual(accounts, list(TradingAccount.objects.values()))
        self.assertEqual(ledgers, list(LedgerEntry.objects.values()))
        self.assertEqual(len(json.loads(Path(self.backup).read_text())['before']), 2)
        self.assertEqual(Path(self.backup).stat().st_mode & 0o777, 0o600)
        after = list(AccountProduct.objects.values())
        self.run_command(apply=True, backup=self.backup)
        self.assertEqual(after, list(AccountProduct.objects.values()))

    def test_drift_aborts_without_changes(self):
        self.ecn.commission_per_lot = Decimal('8'); self.ecn.save()
        before = list(AccountProduct.objects.values())
        with self.assertRaises(CommandError):
            self.run_command(apply=True, backup=self.backup)
        self.assertEqual(before, list(AccountProduct.objects.values()))

    def test_missing_backup_aborts(self):
        with self.assertRaises(CommandError): self.run_command(apply=True)
        self.assertFalse(AccountProduct.objects.filter(code='real-vip').exists())
        self.standard.refresh_from_db(); self.assertTrue(self.standard.is_active)

    def test_backup_collision_aborts(self):
        Path(self.backup).write_text('retain')
        with self.assertRaises(CommandError): self.run_command(apply=True, backup=self.backup)
        self.assertEqual(Path(self.backup).read_text(), 'retain')
        self.assertFalse(AccountProduct.objects.filter(code='real-vip').exists())

    @patch('simulator.tasks.send_email_async.delay')
    def test_vip_new_account_freezes_fee_and_rejects_underfunding(self, email):
        self.run_command(apply=True, backup=self.backup)
        user = make_user(); make_wallet(user, initial_balance=Decimal('100000'))
        self.client.force_login(user)
        vip = AccountProduct.objects.get(code='real-vip')
        self.client.post('/accounts/create/', {'product_id':vip.pk, 'amount':'99999'})
        self.assertFalse(TradingAccount.objects.filter(user=user).exists())
        self.client.post('/accounts/create/', {'product_id':vip.pk, 'amount':'100000'})
        acc = TradingAccount.objects.get(user=user)
        self.assertEqual(acc.commission_per_lot_snapshot, Decimal('3.50'))
        self.assertEqual(acc.leverage, 500)
        self.assertEqual(acc.product_name_snapshot, 'Real VIP')

    def test_retired_standard_rejected_for_new_accounts(self):
        self.run_command(apply=True, backup=self.backup)
        user = make_user(); self.client.force_login(user)
        self.client.post('/accounts/create/', {'product_id':self.standard.pk, 'amount':'100'})
        self.assertFalse(TradingAccount.objects.filter(user=user).exists())

    @patch('simulator.tasks.send_email_async.delay')
    def test_ecn_accepts_ten_and_freezes_new_policy(self, email):
        self.run_command(apply=True, backup=self.backup)
        user = make_user(); make_wallet(user, initial_balance=Decimal('10'))
        self.client.force_login(user)
        self.client.post('/accounts/create/', {'product_id':self.ecn.pk, 'amount':'10'})
        acc = TradingAccount.objects.get(user=user)
        self.assertEqual(acc.balance, Decimal('10'))
        self.assertEqual(acc.leverage_snapshot, 500)
        self.assertEqual(acc.commission_per_lot_snapshot, Decimal('7'))

    def test_vip_catalog_displays_cents(self):
        self.run_command(apply=True, backup=self.backup)
        user = make_user(); self.client.force_login(user)
        self.client.cookies['django_language'] = 'en'
        response = self.client.get('/accounts/open/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Real VIP')
        self.assertContains(response, '$3.50')
        self.assertNotContains(response, 'Real Standard')
