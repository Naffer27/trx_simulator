"""PostgreSQL-only competing triggers: one fill, one pair of fees."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from simulator.models import PendingOrder, Position, LedgerEntry, BrokerLedger
from simulator.consumers import _trigger_pending_order_core
from .factories import make_account, make_spread_config


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
@override_settings(PENDING_ORDER_SPREAD_FEE_ENABLED=True)
class CompetingDemoTriggersTests(TransactionTestCase):
    def test_two_workers_charge_demo_once(self):
        account = make_account(account_type='DEMO', commercial_profile_snapshot={
            'source': 'account_product', 'commission_per_lot': 7,
            'commission_pct': 0, 'spread_markup_pips': 0})
        make_spread_config()
        po = PendingOrder.objects.create(account=account, symbol='EUR/USD',
            side='BUY', order_type='LIMIT', qty=Decimal('.1'), trigger_price=Decimal('1.1'))
        barrier = Barrier(2)
        def trigger():
            try:
                barrier.wait(timeout=10)
                return _trigger_pending_order_core(po.pk, 1.1)
            finally:
                connections.close_all()
        # Exercise real transactions and row locks; only outbound notifications
        # are silenced so the test has no external messaging side effects.
        with patch('simulator.ws_events.publish_position_changed'), patch('simulator.ws_events.publish_pending_order_changed'):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(trigger) for _ in range(2)]
                results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(bool(r['ok']) for r in results), 1, results)
        account.refresh_from_db(); po.refresh_from_db()
        self.assertEqual(account.balance, Decimal('9998.30'))
        self.assertEqual(po.status, PendingOrder.TRIGGERED)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        for event in (LedgerEntry.EV_FEE, LedgerEntry.EV_COMMISSION):
            self.assertEqual(LedgerEntry.objects.filter(account=account,event_type=event).count(), 1)
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 2)
