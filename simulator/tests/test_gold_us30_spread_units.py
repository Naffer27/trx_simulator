"""No database or live orders: verify admin units against the current engine."""
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from simulator.spread_admin_preview import spread_units
from simulator.spread_engine import broker_price


def config(symbol, pips):
    return SimpleNamespace(symbol=symbol, spread_pips=Decimal(pips),
                           min_spread=None, max_spread=None, is_dynamic=False)


class GoldUs30SpreadUnitsTests(unittest.TestCase):
    def test_gold_units_and_existing_engine(self):
        cfg=config('XAU/USD','30')
        info=spread_units(cfg)
        self.assertEqual(info['extra'],Decimal('.30'))
        self.assertFalse(info['rounding_required'])
        self.assertFalse(info['instrument_enabled'])
        with patch('simulator.spread_engine._get_config',return_value=cfg):
            bid,ask=broker_price('XAU/USD',2400,2400.2)
        self.assertAlmostEqual(bid,2399.85)
        self.assertAlmostEqual(ask,2400.35)
        self.assertAlmostEqual(ask-bid,.50)  # provider .20 + broker .30

    def test_us30_odd_spread_exposes_current_rounding(self):
        cfg=config('US30','3')
        info=spread_units(cfg)
        self.assertEqual(info['per_side'],Decimal('1.5'))
        self.assertTrue(info['rounding_required'])
        self.assertFalse(info['instrument_enabled'])
        with patch('simulator.spread_engine._get_config',return_value=cfg):
            bid,ask=broker_price('US30',40000,40000)
        self.assertEqual(ask-bid,4)  # cannot promise 3 with current integer rounding

    def test_us30_even_spread_is_exact(self):
        cfg=config('US30','4')
        self.assertFalse(spread_units(cfg)['rounding_required'])
        with patch('simulator.spread_engine._get_config',return_value=cfg):
            self.assertEqual(broker_price('US30',40000,40000),(39998,40002))

    def test_unknown_symbol(self):
        self.assertIsNone(spread_units(config('UNKNOWN','3')))
