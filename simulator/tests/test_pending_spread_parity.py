"""Opt-in pending spread charges: economics, atomicity and legacy isolation."""
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.db.models import Sum
from simulator.consumers import _trigger_pending_order_core, TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, PendingOrder, Position
from simulator.opening_charges import pending_static_spread_charge, PendingSpreadUnavailable
from .factories import make_account, make_spread_config
from .test_ledger_rounding_reconciliation01 import _make_consumer


@override_settings(PENDING_ORDER_SPREAD_FEE_ENABLED=True)
class PendingSpreadParityTests(TestCase):
    def account(self, rate=7, markup=0):
        return make_account(commercial_profile_snapshot={
            'source': 'account_product_snapshot', 'commission_per_lot': rate,
            'commission_pct': 0, 'spread_markup_pips': markup,
        })

    def pending(self, account, symbol='EUR/USD', qty='0.1', price='1.1'):
        return PendingOrder.objects.create(account=account, symbol=symbol,
            side='BUY', order_type='LIMIT', qty=Decimal(qty), trigger_price=Decimal(price))

    def totals(self, account):
        account.refresh_from_db()
        trader = LedgerEntry.objects.filter(account=account, event_type__in=[LedgerEntry.EV_FEE, LedgerEntry.EV_COMMISSION]).aggregate(v=Sum('amount'))['v'] or Decimal('0')
        broker = BrokerLedger.objects.filter(source_account=account).aggregate(v=Sum('amount'))['v'] or Decimal('0')
        return account.balance, trader, broker

    def test_manual_and_pending_actual_paths_match_for_ecn_vip_and_symbols(self):
        for symbol, qty, price, pips in [('EUR/USD', '0.1', '1.1', 2), ('BTCUSD', '0.01', '80000', 15)]:
            make_spread_config(symbol=symbol, spread_pips=Decimal(pips))
            for rate in (7, 3.5):
                with self.subTest(symbol=symbol, rate=rate):
                    manual, pending = self.account(rate), self.account(rate)
                    po = self.pending(pending, symbol, qty, price)
                    result = _trigger_pending_order_core(po.id, float(price))
                    self.assertTrue(result['ok'], result)
                    # Independent fills: remove the other account's exposure so this
                    # pricing test does not require a live broker-wide quote feed.
                    Position.objects.filter(account=pending).delete()
                    consumer = _make_consumer(manual)
                    commission = round(float(qty) * rate, 2)
                    manual_result = TradingConsumer._db_open_position_atomic.__wrapped__(consumer,
                        symbol, 'buy', float(qty), float(price), None, None,
                        commission, float(manual.balance)-commission,
                        pricing_context={'effective_spread_pips': pips})
                    self.assertTrue(manual_result.get('ok', True), manual_result)
                    self.assertTrue(Position.objects.filter(account=manual).exists())
                    self.assertEqual(self.totals(manual), self.totals(pending))
                    Position.objects.filter(account=manual).delete()
                    balance, trader, broker = self.totals(pending)
                    self.assertEqual(Decimal('10000')-balance, -trader)
                    self.assertEqual(-trader, broker)

    def test_static_fee_exact_amount_and_repeat_is_idempotent(self):
        make_spread_config()
        account = self.account()
        po = self.pending(account)
        self.assertTrue(_trigger_pending_order_core(po.id, 1.1)['ok'])
        self.assertEqual(self.totals(account), (Decimal('9998.30'), Decimal('-1.70'), Decimal('1.70')))
        before = self.totals(account)
        _trigger_pending_order_core(po.id, 1.1)
        self.assertEqual(self.totals(account), before)
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).count(), 1)

    @override_settings(PENDING_ORDER_SPREAD_FEE_ENABLED=False)
    def test_flag_off_preserves_commission_only_even_with_dynamic_config(self):
        make_spread_config(is_dynamic=True)
        account = self.account()
        self.assertTrue(_trigger_pending_order_core(self.pending(account).id, 1.1)['ok'])
        self.assertEqual(self.totals(account), (Decimal('9999.30'), Decimal('-0.70'), Decimal('0.70')))

    def test_dynamic_refuses_before_any_mutation(self):
        make_spread_config(is_dynamic=True)
        account = self.account()
        po = self.pending(account)
        with self.assertRaises(PendingSpreadUnavailable):
            _trigger_pending_order_core(po.id, 1.1)
        self.assert_untouched(account, po)

    def assert_untouched(self, account, po):
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.PENDING)
        self.assertFalse(Position.objects.filter(account=account).exists())
        self.assertEqual(self.totals(account), (Decimal('10000'), Decimal('0'), Decimal('0')))

    def test_broker_spread_failure_rolls_back_commission_position_and_status(self):
        make_spread_config()
        account = self.account()
        po = self.pending(account)
        original = BrokerLedger.objects.create
        def insert(**kwargs):
            if kwargs.get('revenue_type') == BrokerLedger.REV_SPREAD:
                raise RuntimeError('injected ledger failure')
            return original(**kwargs)
        with patch.object(BrokerLedger.objects, 'create', side_effect=insert):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                _trigger_pending_order_core(po.id, 1.1)
        self.assert_untouched(account, po)

    def test_trader_fee_failure_rolls_back_everything(self):
        make_spread_config()
        account = self.account()
        po = self.pending(account)
        original = LedgerEntry.objects.create
        def insert(**kwargs):
            if kwargs.get('event_type') == LedgerEntry.EV_FEE:
                raise RuntimeError('injected trader failure')
            return original(**kwargs)
        with patch.object(LedgerEntry.objects, 'create', side_effect=insert):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                _trigger_pending_order_core(po.id, 1.1)
        self.assert_untouched(account, po)

    def test_bounds_opt_in_and_frozen_account_markup(self):
        cfg = make_spread_config(min_spread=Decimal('5'), bounds_enabled=False)
        account = self.account(markup=1)
        fee, _ = pending_static_spread_charge(account, 'EURUSD', .1)
        self.assertEqual(fee, Decimal('1.5'))
        cfg.spread_bounds_enabled = True
        cfg.save()
        fee, _ = pending_static_spread_charge(account, 'EUR/USD', .1)
        self.assertEqual(fee, Decimal('2.5'))

    def test_missing_config_zero_markup_has_no_spread_fee(self):
        account = self.account()
        self.assertTrue(_trigger_pending_order_core(self.pending(account).id, 1.1)['ok'])
        self.assertEqual(self.totals(account)[0], Decimal('9999.30'))
        self.assertFalse(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).exists())

    @patch('simulator.tasks._read_cached_price', return_value=(1.09945, 1.09950))
    def test_daemon_applies_same_fee_without_browser(self, quote):
        from simulator.tasks import scan_pending_orders_task
        make_spread_config()
        account = self.account()
        po = self.pending(account)
        result = scan_pending_orders_task.apply().get()
        self.assertEqual(result['triggered'], 1)
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)
        self.assertEqual(self.totals(account), (Decimal('9998.30'), Decimal('-1.70'), Decimal('1.70')))
        self.assertEqual(scan_pending_orders_task.apply().get()['triggered'], 0)

    def test_invalid_bounds_leave_order_untouched(self):
        make_spread_config()
        account = self.account()
        account.commercial_profile_snapshot.update(min_spread_pips=5, max_spread_pips=1)
        account.save(update_fields=['commercial_profile_snapshot'])
        po = self.pending(account)
        with self.assertRaises(PendingSpreadUnavailable):
            _trigger_pending_order_core(po.id, 1.1)
        self.assert_untouched(account, po)

    def test_subcent_fee_reconciles_to_persisted_balance(self):
        make_spread_config(symbol='BTCUSD', spread_pips=Decimal('15'))
        account = self.account(rate=0)
        po = self.pending(account, 'BTCUSD', '.01', '80000')
        self.assertTrue(_trigger_pending_order_core(po.id, 80000)['ok'])
        balance, trader, broker = self.totals(account)
        self.assertEqual(Decimal('10000') - balance, -trader)
        self.assertEqual(-trader, broker)
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertGreater(Decimal(fee.meta['exact_amount']), 0)


class DemoPendingSpreadParityTests(PendingSpreadParityTests):
    """Run the complete fee/rollback suite with actual DEMO account semantics.

    Isolated Django test database only; this is not a live UI/quote certification.
    """
    def account(self, rate=7, markup=0):
        return make_account(account_type='DEMO', commercial_profile_snapshot={
            'source': 'account_product_snapshot', 'commission_per_lot': rate,
            'commission_pct': 0, 'spread_markup_pips': markup,
        })
