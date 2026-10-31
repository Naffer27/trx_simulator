import json
import os
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from simulator.gold_demo_policy import gold_demo_allowed
from simulator.consumers import TradingConsumer
from simulator.models import Position
from simulator.tests.factories import make_account, make_user


class GoldDemoPolicyTests(SimpleTestCase):
    def test_fail_closed_and_exact_account_type(self):
        with patch.dict(os.environ, {'MASSIVE_GOLD_DATA_ENABLED':'1','GOLD_DEMO_ACCOUNT_IDS':'3,4'}):
            self.assertTrue(gold_demo_allowed(3,'DEMO'))
            for kind in ('ECN','STANDARD','RETAIL','CHALLENGE','FUNDED','CRYPTO'):
                self.assertFalse(gold_demo_allowed(3,kind))
            self.assertFalse(gold_demo_allowed(5,'DEMO'))
            self.assertFalse(gold_demo_allowed(None,'DEMO'))
            self.assertFalse(gold_demo_allowed(3,'DEMO','EUR'))
            self.assertFalse(gold_demo_allowed(3,'DEMO',allowed_symbols=['EUR/USD']))
            self.assertTrue(gold_demo_allowed(3,'DEMO',allowed_symbols=['XAU/USD']))

    def test_off_and_malformed_configuration(self):
        for ids in ('','3,abc','0,3','3,','-3'):
            with patch.dict(os.environ, {'MASSIVE_GOLD_DATA_ENABLED':'1','GOLD_DEMO_ACCOUNT_IDS':ids}):
                self.assertFalse(gold_demo_allowed(3,'DEMO'))
        with patch.dict(os.environ, {'MASSIVE_GOLD_DATA_ENABLED':'0','GOLD_DEMO_ACCOUNT_IDS':'3'}):
            self.assertFalse(gold_demo_allowed(3,'DEMO'))

    def test_websocket_boundary_uses_server_account(self):
        consumer=TradingConsumer.__new__(TradingConsumer)
        consumer._db_account_id=3
        consumer.account={'account_type':'DEMO','currency':'USD','allowed_symbols':None}
        with patch.dict(os.environ, {'MASSIVE_GOLD_DATA_ENABLED':'1','GOLD_DEMO_ACCOUNT_IDS':'3'}):
            self.assertTrue(consumer._symbol_allowed_for_account('XAU/USD'))
            consumer.account['account_type']='ECN'
            self.assertFalse(consumer._symbol_allowed_for_account('XAU/USD'))
            self.assertTrue(consumer._symbol_allowed_for_account('BTCUSD'))
            self.assertFalse(consumer._symbol_allowed_for_account('US30'))


class GoldDemoIsolationTests(TestCase):
    def setUp(self):
        self.user=make_user()
        self.demo=make_account(self.user,account_type='DEMO',balance=Decimal('100000'))
        self.real=make_account(self.user,account_type='ECN',balance=Decimal('100000'))
        self.client.force_login(self.user)
        self.env=patch.dict(os.environ,{'MASSIVE_GOLD_DATA_ENABLED':'1',
                            'GOLD_DEMO_ACCOUNT_IDS':f'{self.demo.pk},{self.real.pk}'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_dashboard_catalog_is_account_scoped(self):
        for account,expected in ((self.demo,True),(self.real,False)):
            response=self.client.get(reverse('simulator:dashboard_account',args=[account.pk]))
            self.assertEqual(response.status_code,200)
            symbols=json.loads(response.context['mobile_allowed_symbols_json'])
            self.assertEqual(any(s['symbol']=='XAU/USD' for s in symbols),expected)
            self.assertEqual(response.context['gold_demo_enabled'],expected)

    def test_atomic_gate_rejects_real_account_even_with_stale_demo_snapshot(self):
        c=TradingConsumer.__new__(TradingConsumer)
        c._db_account_id=self.real.pk
        c.account={'account_type':'DEMO','currency':'USD'}
        result=TradingConsumer._db_open_position_atomic.__wrapped__(
            c,'XAU/USD','buy',.01,4000,None,None,commission=.07,new_balance=99999.93)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'],'symbol_not_allowed')
        self.assertFalse(Position.objects.filter(account=self.real).exists())
        self.real.refresh_from_db()
        self.assertEqual(self.real.balance,Decimal('100000'))

    def test_anonymous_atomic_open_denied(self):
        c=TradingConsumer.__new__(TradingConsumer);c._db_account_id=None
        result=TradingConsumer._db_open_position_atomic.__wrapped__(
            c,'XAU/USD','buy',.01,4000,None,None,commission=.07,new_balance=0)
        self.assertFalse(result['ok'])

    def test_authorized_demo_market_open_and_close_accounting(self):
        from simulator.tests.test_atomic_guard_lock_order import _consumer, _seed_fresh_price, _pos_mem
        _seed_fresh_price('XAU/USD',4000,4000.3)
        c=_consumer(self.demo.pk)
        result=TradingConsumer._db_open_position_atomic.__wrapped__(
            c,'XAU/USD','buy',.01,4000,None,None,commission=.07,new_balance=99999.93)
        self.assertTrue(result['ok'],result)
        position=Position.objects.get(account=self.demo)
        self.assertEqual(position.qty,Decimal('.01'))
        self.demo.refresh_from_db()
        self.assertEqual(self.demo.balance,Decimal('99999.93'))
        result=TradingConsumer._db_close_position_atomic.__wrapped__(
            c,_pos_mem(position),4002,'manual',realized_pnl=2,
            new_balance=100001.93,new_equity=100001.93)
        self.assertTrue(result['ok'],result)
        self.assertFalse(Position.objects.filter(account=self.demo).exists())
        self.demo.refresh_from_db()
        self.assertEqual(self.demo.balance,Decimal('100001.93'))
