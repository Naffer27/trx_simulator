from decimal import Decimal as D
from io import StringIO
from django.core.management import call_command
from django.test import SimpleTestCase
from market_data.instruments.pilot_rules import PILOT_RULES, preview_economics
from market_data.symbol_specs import allowed_symbols, get_all_specs


class InstrumentPilotRuleTests(SimpleTestCase):
    def test_known_contract_economics(self):
        cases = [('XAU/USD', '.25', '4000', '1000', '.25'),
                 ('SOLUSD', '10', '100', '100', '.1'),
                 ('KASUSD', '1000', '.03', '15', '.001'),
                 ('US30', '1', '40000', '2000', '1'),
                 ('WTI', '1', '70', '7000', '10')]
        for symbol, qty, price, margin, tick in cases:
            with self.subTest(symbol=symbol):
                result = preview_economics(symbol, qty, price, 500)
                self.assertEqual(result['margin_usd'], D(margin))
                self.assertEqual(result['tick_value_usd'], D(tick))
                self.assertIsNone(result['spread_usd'])
                self.assertFalse(result['enabled'])

    def test_crypto_fee_is_not_seven_dollars_per_coin(self):
        for tier in ('ECN', 'VIP'):
            result = preview_economics('KASUSD', '1000', '.03', 500, tier)
            self.assertEqual(result['opening_commission_usd_unrounded'], D('.003'))
            result = preview_economics('SOLUSD', '10', '100', 500, tier)
            self.assertEqual(result['opening_commission_usd_unrounded'], D('.1'))

    def test_existing_account_tier_fee_examples(self):
        for tier, expected in [('ECN', '1.75'), ('VIP', '.875')]:
            result = preview_economics('XAU/USD', '.25', '4000', 500, tier)
            self.assertEqual(result['opening_commission_usd_unrounded'], D(expected))

    def test_account_leverage_can_lower_instrument_cap(self):
        result = preview_economics('SOLUSD', '10', '100', 5)
        self.assertEqual(result['effective_leverage'], D('5'))
        self.assertEqual(result['margin_usd'], D('200'))

    def test_invalid_values_and_lot_steps_rejected(self):
        for qty in ('NaN', 'Infinity', '-1', '0', '.01', '1001', '.15'):
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                preview_economics('SOLUSD', qty, '100', 500)
        for price in ('NaN', 'Infinity', '-1', '0', 'bad'):
            with self.subTest(price=price), self.assertRaises(ValueError):
                preview_economics('SOLUSD', '1', price, 500)
        with self.assertRaises(ValueError):
            preview_economics('SOLUSD', '1', '100', '1.5')

    def test_drafts_never_register_or_activate_instruments(self):
        before = (allowed_symbols(), get_all_specs())
        for symbol, rule in PILOT_RULES.items():
            self.assertFalse(rule.enabled)
            self.assertTrue(rule.blockers)
            preview_economics(symbol, rule.min_lot, 100, 500)
        self.assertEqual((allowed_symbols(), get_all_specs()), before)
        self.assertNotIn('KASUSD', {s.symbol for s in get_all_specs()})
        self.assertNotIn('WTI', {s.symbol for s in get_all_specs()})

    def test_preview_command_does_not_query_database(self):
        out = StringIO()
        call_command('preview_instrument_pilot', symbol='WTI', lots='1', price='70', stdout=out)
        self.assertIn('"enabled": false', out.getvalue())
        self.assertIn('snapshot_http_403', out.getvalue())
