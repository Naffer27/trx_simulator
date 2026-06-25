# simulator/tests/test_dashboard_history_persistence.py
"""
Bloque K.2D — Persisted Closed Trades History.

Verifies that the trading dashboard seeds closedTradesHistory from the DB
so trade history survives page refresh.
"""
import json
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.models import Trade
from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


def _make_closed_trade(account, symbol="EUR/USD", side="BUY",
                       lot_size="0.01", entry="1.10000", exit_price="1.11000",
                       pnl="100.00", minutes_ago=5):
    closed_at = timezone.now() - timezone.timedelta(minutes=minutes_ago)
    return Trade.objects.create(
        account=account,
        symbol=symbol,
        trade_type=side,
        lot_size=Decimal(lot_size),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_price),
        profit_loss=Decimal(pnl),
        closed_at=closed_at,
    )


def _make_open_trade(account, symbol="EUR/USD"):
    """Trade with no exit_price/closed_at — should NOT appear in history."""
    return Trade.objects.create(
        account=account,
        symbol=symbol,
        trade_type="BUY",
        lot_size=Decimal("0.01"),
        entry_price=Decimal("1.10000"),
    )


class ClosedTradesJsonContextTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _ctx(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r

    def test_closed_trades_json_in_context(self):
        r = self._ctx()
        self.assertIn('closed_trades_json', r.context)

    def test_empty_history_returns_empty_array(self):
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(data, [])

    def test_empty_history_does_not_crash(self):
        r = self._ctx()
        self.assertEqual(r.status_code, 200)

    def test_closed_trade_appears_in_json(self):
        _make_closed_trade(self.account, symbol="BTCUSD", pnl="250.00")
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['symbol'], 'BTCUSD')

    def test_closed_trade_fields_present(self):
        _make_closed_trade(self.account, symbol="EUR/USD", side="SELL",
                           lot_size="0.10", entry="1.10000", exit_price="1.09000",
                           pnl="100.00")
        r = self._ctx()
        t = json.loads(r.context['closed_trades_json'])[0]
        self.assertIn('id', t)
        self.assertEqual(t['symbol'], 'EUR/USD')
        self.assertEqual(t['side'], 'sell')
        self.assertAlmostEqual(t['qty'], 0.10, places=2)
        self.assertAlmostEqual(t['entry'], 1.10000, places=5)
        self.assertAlmostEqual(t['close'], 1.09000, places=5)
        self.assertAlmostEqual(t['pnl'], 100.00, places=2)
        self.assertIsInstance(t['ts'], int)

    def test_only_closed_trades_included(self):
        _make_closed_trade(self.account)
        _make_open_trade(self.account)
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 1)

    def test_most_recent_first(self):
        _make_closed_trade(self.account, symbol="EUR/USD", minutes_ago=10)
        _make_closed_trade(self.account, symbol="BTCUSD",  minutes_ago=2)
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]['symbol'], 'BTCUSD')   # most recent
        self.assertEqual(data[1]['symbol'], 'EUR/USD')  # older

    def test_limit_50_trades(self):
        for i in range(60):
            _make_closed_trade(self.account, symbol="EUR/USD", minutes_ago=i)
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 50)

    def test_other_user_trades_not_included(self):
        other_user = make_user(username="other_trader")
        other_account = make_account(other_user, account_type="DEMO")
        _make_closed_trade(other_account, symbol="BTCUSD", pnl="999.00")
        _make_closed_trade(self.account, symbol="EUR/USD", pnl="10.00")
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['symbol'], 'EUR/USD')

    def test_other_account_same_user_not_included(self):
        other_account = make_account(self.user, account_type="DEMO")
        _make_closed_trade(other_account, symbol="BTCUSD", pnl="999.00")
        _make_closed_trade(self.account,  symbol="EUR/USD", pnl="10.00")
        r = self._ctx()
        data = json.loads(r.context['closed_trades_json'])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['symbol'], 'EUR/USD')


class ClosedTradesHtmlSeedTests(TestCase):
    """Verify the seeded array is rendered into the dashboard HTML."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_empty_seed_renders_empty_array(self):
        html = self._html()
        self.assertIn('closedTradesHistory=[]', html)

    def test_closed_trade_symbol_in_html(self):
        _make_closed_trade(self.account, symbol="BTCUSD")
        html = self._html()
        self.assertIn('BTCUSD', html)
        self.assertIn('closedTradesHistory=[', html)
