"""Gold data opt-in is independent of all trading permissions."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from market_data.symbol_specs import allowed_symbols, get_spec

ROOT = Path(__file__).resolve().parents[2]


def load_feed(flag):
    name = '_gold_data_test_feed'
    spec = importlib.util.spec_from_file_location(name, ROOT/'market_data/feeds.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'MASSIVE_GOLD_DATA_ENABLED':flag,
                               'MASSIVE_API_KEY':'', 'FINNHUB_API_KEY':''}), \
            patch.dict(sys.modules, {name:module}):
        spec.loader.exec_module(module)
    return module


class GoldDataOptInTests(unittest.TestCase):
    def test_default_and_invalid_values_do_not_expand_routing(self):
        forex=frozenset({'EUR/USD','GBP/USD','USD/JPY','AUD/USD'})
        for value in ('', '0', 'true', 'yes'):
            with self.subTest(value=value):
                feed=load_feed(value)
                self.assertEqual(feed._MASSIVE_ENABLED_SYMBOLS,forex)
                self.assertEqual(feed._MASSIVE_WS_ENABLED_SYMBOLS,forex)
                self.assertIsNone(feed._massive_sym('XAU/USD'))

    def test_optin_routes_gold_bars_and_quotes_together(self):
        feed=load_feed('1')
        self.assertEqual(feed._massive_sym('XAU/USD'),'C:XAUUSD')
        self.assertIn('XAU/USD',feed._MASSIVE_WS_ENABLED_SYMBOLS)
        self.assertEqual(feed._massive_ws_param('XAU/USD'),'C.XAU/USD')
        batch=feed._massive_ws_params_batch({'EUR/USD','XAU/USD'})
        self.assertEqual(batch,'C.EUR/USD,C.XAU/USD')
        self.assertTrue(feed._validate_quote_values('XAU/USD',4000,4000.3))
        self.assertFalse(feed._validate_quote_values('XAU/USD',4000,3999))

    def test_optin_does_not_enable_orders_or_other_assets(self):
        before=allowed_symbols()
        feed=load_feed('1')
        self.assertEqual(allowed_symbols(),before)
        self.assertFalse(get_spec('XAU/USD').enabled)
        self.assertNotIn('XAU/USD',allowed_symbols())
        for sym in ('US30','WTI','SOLUSD','KASUSD'):
            self.assertIsNone(feed._massive_sym(sym))
        self.assertEqual(feed._MASSIVE_CRYPTO_ENABLED_SYMBOLS,frozenset({'BTCUSD','ETHUSD'}))
