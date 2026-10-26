"""BTC venue/order regression. No network, database or financial operations."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from django.test import SimpleTestCase
from market_data.feeds import FeedManager
from simulator.tests.test_crypto_quote_dedup_01 import (
    MASSIVE_AUTH_SUCCESS, _make_connect_mock, _settle,
)


def event(kind='XQ', exchange=1, stamp=10000, pair='BTC-USD', price=80738):
    return dict(ev=kind, x=exchange, t=stamp, pair=pair,
                bp=price, ap=price+1, p=price, s=.01)


class BTCVenueTests(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()

    def test_only_coinbase_for_btc(self):
        for exchange in (2, 6, 23, None, '1', True):
            self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(exchange=exchange)))
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event()))

    def test_older_same_millisecond_is_rejected(self):
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event(stamp=10999)))
        self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(stamp=10998)))

    def test_equal_millisecond_is_not_suppressed(self):
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event()))
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event(price=80739)))

    def test_quote_trade_watermarks_independent(self):
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event(stamp=20000)))
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event(kind='XT', stamp=10000)))
        self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(kind='XT', stamp=9999)))

    def test_wrong_venue_cannot_poison_watermark(self):
        self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(exchange=2,stamp=999999)))
        self.assertTrue(self.fm._accept_btc_market_event('BTCUSD', event()))

    def test_bad_metadata_is_rejected(self):
        for stamp in (None,0,-1,True,'10000',float('nan')):
            self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(stamp=stamp)))
        self.assertFalse(self.fm._accept_btc_market_event('BTCUSD', event(kind='other')))

    def test_eth_and_forex_do_not_use_gate(self):
        for symbol in ('ETHUSD','EURUSD'):
            self.assertTrue(self.fm._accept_btc_market_event(symbol, {}))
        self.assertEqual(self.fm._massive_btc_event_ms,{})

    def run_stream(self, events):
        async def run():
            connect = _make_connect_mock([[MASSIVE_AUTH_SUCCESS,json.dumps(events)]])
            quote, trade, refresh = AsyncMock(), AsyncMock(), AsyncMock()
            with patch('market_data.feeds.websockets.connect',connect), \
                 patch('market_data.feeds.MASSIVE_API_KEY','test-only'), \
                 patch.object(self.fm,'_broadcast',quote), \
                 patch.object(self.fm,'_broadcast_trade',trade), \
                 patch.object(self.fm,'_update_price_state',refresh):
                self.fm._massive_crypto_active_symbols.update(('BTCUSD','ETHUSD'))
                task=asyncio.create_task(self.fm._massive_crypto_shared_loop(None))
                await _settle(50)
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            return quote,trade,refresh
        return asyncio.run(run())

    def test_real_loop_does_not_broadcast_other_venues_or_older_quotes(self):
        q,t,r=self.run_stream([event(),event(exchange=2,stamp=11000,price=80860),
            event(stamp=9999,price=80000),event(stamp=11000,price=80739)])
        self.assertEqual([c.args[2] for c in q.await_args_list],[80738.,80739.])
        t.assert_not_awaited();r.assert_not_awaited()

    def test_real_loop_does_not_broadcast_other_venues_or_older_trades(self):
        q,t,r=self.run_stream([event(kind='XT'),event(kind='XT',exchange=2,price=80860),
            event(kind='XT',stamp=9999),event(kind='XT',stamp=11000,price=80739)])
        self.assertEqual([c.args[2] for c in t.await_args_list],[80738.,80739.])
        q.assert_not_awaited();r.assert_not_awaited()

    def test_rejected_events_do_not_refresh_watchdog_or_cache(self):
        q,t,r=self.run_stream([event(exchange=2),event(kind='XT',exchange=2)])
        q.assert_not_awaited();t.assert_not_awaited();r.assert_not_awaited()
        self.assertEqual(self.fm._massive_crypto_last_broadcast_quote,{})
        self.assertEqual(self.fm._massive_btc_event_ms,{})

    def test_existing_duplicate_refresh_preserved(self):
        q,t,r=self.run_stream([event(),event(stamp=10001)])
        q.assert_awaited_once();r.assert_awaited_once();t.assert_not_awaited()

    def test_existing_value_validation_precedes_watermark(self):
        q,t,r=self.run_stream([event(stamp=999999,price=-100),event()])
        q.assert_awaited_once()
        self.assertEqual(self.fm._massive_btc_event_ms['XQ'],10000)

    def test_eth_quotes_and_trades_unchanged(self):
        q,t,r=self.run_stream([event(pair='ETH-USD',exchange=2,price=2500),
            event(kind='XT',pair='ETH-USD',exchange=2,price=2500)])
        q.assert_awaited_once();t.assert_awaited_once()
        self.assertEqual(self.fm._massive_btc_event_ms,{})

    def test_watermark_survives_new_connection(self):
        self.run_stream([event(stamp=20000)])
        q,t,r=self.run_stream([event(stamp=19999),event(stamp=20001)])
        q.assert_awaited_once()
        self.assertEqual(q.await_args.args[4],20)

    def test_video_window_trade_replay(self):
        rows=json.loads((Path(__file__).parent/'fixtures/btc_video_20261024_trades.json').read_text())
        prices=[r['p'] for r in rows]
        q,t,r=self.run_stream(rows)
        selected=[c.args[2] for c in t.await_args_list]
        self.assertEqual(len(prices),222)
        self.assertEqual(len(selected),162)
        self.assertGreater(max(abs(a-b) for a,b in zip(prices,prices[1:])),100)
        self.assertLess(max(abs(a-b) for a,b in zip(selected,selected[1:])),40)
        self.assertEqual(selected,[e['p'] for e in rows if e['x']==1])
