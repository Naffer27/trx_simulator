"""Isolated demo products inherit the real catalogue economics through public creation."""
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from simulator.models import AccountProduct, TradingAccount, Position, LedgerEntry
from simulator.commercial_pricing import resolve_commercial_pricing_fields
from simulator.management.commands.align_real_account_catalog import ECN_AFTER, VIP
from simulator.consumers import _trigger_pending_order_core, _compute_pretrade_margin_guard
from market_data.symbol_specs import get_spec
from .factories import make_user, make_wallet, make_spread_config
from .test_pending_spread_parity import PendingSpreadParityTests


@override_settings(PENDING_ORDER_SPREAD_FEE_ENABLED=True)
class DemoRealCatalogueRulesTests(TestCase):
    def setUp(self):
        self.products = [AccountProduct.objects.create(code='real-ecn', name='Real ECN', **ECN_AFTER),
                         AccountProduct.objects.create(**VIP)]
        make_spread_config()

    def create_pair(self, product):
        # All persisted commercial/risk parameters are copied, not a second tariff table.
        fields = {f.name: getattr(product, f.name) for f in product._meta.concrete_fields
                  if not f.primary_key and f.name not in {'created_at', 'updated_at'}}
        fields.update(code='demo-'+product.code, name='Demo '+product.name,
                      family='DEMO', product_type='DEMO', default_balance=Decimal('100000'))
        demo_product = AccountProduct.objects.create(**fields)
        result = []
        for p in (product, demo_product):
            user = make_user()
            wallet = make_wallet(user, initial_balance=Decimal('100000'))
            self.client.force_login(user)
            with patch('simulator.tasks.send_email_async.delay'):
                self.client.post('/accounts/create/', {'product_id': p.pk, 'amount': '100000'})
            account = TradingAccount.objects.get(user=user)
            wallet.refresh_from_db()
            if p.family == 'DEMO':
                self.assertEqual(wallet.available_balance, Decimal('100000'))
                self.assertEqual(account.account_type, 'DEMO')
            result.append(account)
        return result

    def test_public_creation_freezes_identical_economic_and_risk_rules(self):
        for product in self.products:
            with self.subTest(product=product.code):
                real, demo = self.create_pair(product)
                for field in ('leverage', 'leverage_snapshot', 'spread_pips_snapshot',
                              'commission_per_lot_snapshot', 'allowed_symbols_snapshot',
                              'max_lot_size_snapshot', 'margin_call_level_snapshot',
                              'stopout_level_snapshot', 'max_margin_per_trade_pct_snapshot',
                              'max_total_margin_pct_snapshot'):
                    self.assertEqual(getattr(real, field), getattr(demo, field), field)
                for key in ('spread_markup_pips', 'commission_per_lot', 'commission_pct',
                            'min_spread_pips', 'max_spread_pips'):
                    self.assertEqual(resolve_commercial_pricing_fields(real)[key],
                                     resolve_commercial_pricing_fields(demo)[key], key)

    def test_real_and_demo_pending_fills_charge_identical_amounts(self):
        for product in self.products:
            real, demo = self.create_pair(product)
            charges = []
            for account in (real, demo):
                order = PendingSpreadParityTests.pending(self, account)
                result = _trigger_pending_order_core(order.pk, 1.1)
                self.assertTrue(result['ok'], result)
                account.refresh_from_db()
                charges.append(Decimal('100000') - account.balance)
                self.assertTrue(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).exists())
                Position.objects.filter(account=account).delete()
            self.assertEqual(charges[0], charges[1])
            self.assertEqual(charges[0], Decimal('1') + product.commission_per_lot * Decimal('.1'))

    def test_later_product_change_does_not_reprice_existing_demo(self):
        real, demo = self.create_pair(self.products[0])
        frozen = resolve_commercial_pricing_fields(demo)
        AccountProduct.objects.filter(pk=demo.account_product_id).update(commission_per_lot=Decimal('99'))
        demo.refresh_from_db()
        self.assertEqual(resolve_commercial_pricing_fields(demo), frozen)

    def test_demo_500_leverage_still_obeys_btc_instrument_cap(self):
        real, demo = self.create_pair(self.products[0])
        spec = get_spec('BTCUSD')
        results = []
        for account in (real, demo):
            result = _compute_pretrade_margin_guard('BTCUSD', .01, 80000, 100000, 0,
                {'leverage': account.leverage}, spec.max_leverage, spec.contract_size)
            self.assertTrue(result[0], result)
            results.append(result[3]['required_margin'])
        self.assertEqual(results, [40.0, 40.0])
