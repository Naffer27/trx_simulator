from decimal import Decimal
from io import StringIO
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from simulator.models import IBCommissionRule, IBCommissionObligation, TreasuryOperationRequest, WalletTransaction
from simulator.ib_commission import generate_per_lot_obligation
from simulator.tests.factories import make_user, make_account
from simulator.tests.test_ib_commission_engine_01 import _make_referral, _make_attribution, _make_lot_event


class PerLotLaunchTests(TestCase):
    def setUp(self):
        self.ref = _make_referral()
        self.trader = make_user()
        _make_attribution(self.trader, self.ref)

    def configure(self, **kwargs):
        call_command('initialize_ib_per_lot', amount='5', stdout=StringIO(), **kwargs)

    def test_preview_and_repeat_do_not_create_duplicates_or_pay(self):
        self.configure()
        self.assertFalse(IBCommissionRule.objects.exists())
        self.configure(apply=True)
        self.configure(apply=True)
        self.assertEqual(IBCommissionRule.objects.count(), 1)
        self.assertFalse(IBCommissionObligation.objects.exists())
        self.assertFalse(TreasuryOperationRequest.objects.exists())
        self.assertFalse(WalletTransaction.objects.exists())
        with self.assertRaises(CommandError):
            call_command('initialize_ib_per_lot', amount='7', apply=True, stdout=StringIO())
        self.assertEqual(IBCommissionRule.objects.get().fixed_amount, Decimal('5'))

    def test_cash_accounts_amount_duplicate_and_no_payment(self):
        self.configure(apply=True)
        for kind in ('RETAIL', 'STANDARD', 'ECN', 'CRYPTO'):
            with self.subTest(kind=kind):
                account = make_account(user=self.trader, account_type=kind)
                for qty, expected in (('1', '5'), ('.1', '.50'), ('.01', '.05')):
                    event = _make_lot_event(account, qty=qty)
                    ob = generate_per_lot_obligation(event)
                    self.assertEqual(ob.calculated_amount, Decimal(expected))
                    self.assertEqual(ob.status, IBCommissionObligation.ST_PENDING)
                    self.assertEqual(generate_per_lot_obligation(event).pk, ob.pk)
        self.assertEqual(IBCommissionObligation.objects.count(), 12)
        self.assertFalse(WalletTransaction.objects.exists())
        self.assertFalse(TreasuryOperationRequest.objects.exists())

    def test_synthetic_accounts_and_old_executions_excluded(self):
        old = _make_lot_event(make_account(user=self.trader, account_type='STANDARD'))
        self.configure(apply=True)
        self.assertIsNone(generate_per_lot_obligation(old))
        for kind in ('DEMO', 'CHALLENGE', 'FUNDED', 'UNKNOWN'):
            with self.subTest(kind=kind):
                event = _make_lot_event(make_account(user=self.trader, account_type=kind))
                self.assertIsNone(generate_per_lot_obligation(event))
        self.assertFalse(IBCommissionObligation.objects.exists())

    def test_future_rate_change_preserves_old_commission(self):
        from types import SimpleNamespace
        from simulator.ib_admin_ops import change_commission_rate
        self.configure(apply=True)
        account = make_account(user=self.trader, account_type='STANDARD')
        old = generate_per_lot_obligation(_make_lot_event(account, qty='1'))
        admin = make_user(is_staff=True, is_superuser=True)
        change_commission_rate(self.ref, Decimal('7'), request=SimpleNamespace(user=admin))
        new = generate_per_lot_obligation(_make_lot_event(account, qty='1'))
        old.refresh_from_db()
        self.assertEqual(old.calculated_amount, Decimal('5'))
        self.assertEqual(new.calculated_amount, Decimal('7'))
