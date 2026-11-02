"""Real gold opt-in, DEMO isolation and authoritative accounting regression."""
import json
import os
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse
from simulator.gold_demo_policy import gold_trading_allowed
from simulator.consumers import TradingConsumer
from simulator.models import Position
from simulator.tests.factories import make_account, make_user
from simulator.tests.test_atomic_guard_lock_order import _consumer, _seed_fresh_price, _pos_mem

ENV = {'MASSIVE_GOLD_DATA_ENABLED': '1', 'GOLD_REAL_TRADING_ENABLED': '1',
       'GOLD_DEMO_ACCOUNT_IDS': '3,4'}

class GoldRealPolicyTests(SimpleTestCase):
    def test_explicit_optin_and_product_restrictions(self):
        with patch.dict(os.environ, ENV):
            for kind in ('RETAIL', 'STANDARD', 'ECN'):
                self.assertTrue(gold_trading_allowed(10, kind))
                self.assertFalse(gold_trading_allowed(10, kind, 'EUR'))
                self.assertFalse(gold_trading_allowed(10, kind, allowed_symbols=['BTCUSD']))
                self.assertFalse(gold_trading_allowed(None, kind))
            for kind in ('CRYPTO', 'CHALLENGE', 'FUNDED', 'UNKNOWN'):
                self.assertFalse(gold_trading_allowed(10, kind))
            self.assertTrue(gold_trading_allowed(3, 'DEMO'))
            self.assertFalse(gold_trading_allowed(10, 'DEMO'))
            for flag in ('MASSIVE_GOLD_DATA_ENABLED', 'GOLD_REAL_TRADING_ENABLED'):
                with patch.dict(os.environ, {flag: '0'}):
                    self.assertFalse(gold_trading_allowed(10, 'ECN'))

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class GoldRealAccountingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type='ECN', balance=Decimal('100000'))
        self.client.force_login(self.user)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_catalog_and_websocket_share_permission(self):
        for route in ('dashboard_account', 'dashboard_v2_account'):
            response = self.client.get(reverse('simulator:' + route, args=[self.account.pk]))
            self.assertEqual(response.status_code, 200)
            symbols = json.loads(response.context['mobile_allowed_symbols_json'])
            self.assertIn('XAU/USD', [s['symbol'] for s in symbols])
        consumer = _consumer(self.account.pk)
        consumer.account = {'account_type': 'ECN', 'currency': 'USD', 'allowed_symbols': None}
        self.assertTrue(consumer._symbol_allowed_for_account('XAU/USD'))

    def test_atomic_gate_rechecks_database_restriction(self):
        self.account.allowed_symbols_snapshot = ['EUR/USD']
        self.account.save(update_fields=['allowed_symbols_snapshot'])
        result = TradingConsumer._db_open_position_atomic.__wrapped__(
            _consumer(self.account.pk), 'XAU/USD', 'buy', .01, 4000, None, None,
            commission=.07, new_balance=99999.93)
        self.assertFalse(result['ok'])
        self.assertFalse(Position.objects.filter(account=self.account).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('100000'))

    def test_real_buy_sell_open_close_without_touching_demo(self):
        demo = make_account(self.user, account_type='DEMO', balance=Decimal('12345'))
        for side, close_price in (('buy', 4002), ('sell', 3998)):
            self.account.balance = Decimal('100000')
            self.account.save(update_fields=['balance'])
            _seed_fresh_price('XAU/USD', 4000, 4000.3)
            c = _consumer(self.account.pk)
            result = TradingConsumer._db_open_position_atomic.__wrapped__(
                c, 'XAU/USD', side, .01, 4000, None, None,
                commission=.07, new_balance=99999.93)
            self.assertTrue(result['ok'], result)
            position = Position.objects.get(account=self.account)
            self.account.refresh_from_db()
            self.assertEqual(self.account.balance, Decimal('99999.93'))
            result = TradingConsumer._db_close_position_atomic.__wrapped__(
                c, _pos_mem(position), close_price, 'manual', realized_pnl=2,
                new_balance=100001.93, new_equity=100001.93)
            self.assertTrue(result['ok'], result)
            self.account.refresh_from_db()
            self.assertEqual(self.account.balance, Decimal('100001.93'))
            self.assertFalse(Position.objects.filter(account=self.account).exists())
        demo.refresh_from_db()
        self.assertEqual(demo.balance, Decimal('12345'))
