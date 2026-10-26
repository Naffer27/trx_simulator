"""Informational statistics: provider is always mocked, no database or orders."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch, Mock

import requests
from django.test import SimpleTestCase, RequestFactory
from django.urls import reverse, resolve
from simulator import market_session_stats as stats

NOW = 1793024245


def payload(symbol='EUR/USD'):
    ticker, kind = stats._TICKERS[symbol]
    return {'status': 'OK', 'results': [{'ticker': ticker, 'type': kind, 'session': {
        'high': 1.1227, 'low': 1.1172, 'volume': 114087,
        'last_updated': NOW * 1_000_000_000,
    }}]}


class SessionStatsTests(SimpleTestCase):
    def setUp(self):
        stats._CACHE.clear()
        self.factory = RequestFactory()

    def tearDown(self):
        stats._CACHE.clear()

    def call(self, symbol='EUR/USD', authenticated=True, method='get'):
        request = getattr(self.factory, method)('/api/market-session-stats/', {'symbol': symbol})
        request.user = SimpleNamespace(is_authenticated=authenticated)
        return stats.market_session_stats(request)

    def test_route_and_auth_and_method(self):
        self.assertEqual(resolve(reverse('simulator:market_session_stats')).func, stats.market_session_stats)
        with patch.object(stats.requests, 'get') as get:
            self.assertEqual(self.call(authenticated=False).status_code, 401)
            self.assertEqual(self.call(method='post').status_code, 405)
            get.assert_not_called()

    def test_invalid_and_disabled_symbols_never_reach_provider(self):
        with patch.object(stats.requests, 'get') as get:
            for symbol in ('https://evil.invalid', 'XAU/USD', 'BTC-USD', ''):
                self.assertEqual(self.call(symbol).status_code, 400)
            with patch.object(stats, 'allowed_symbols', return_value=frozenset()):
                self.assertEqual(self.call().status_code, 400)
            get.assert_not_called()

    def test_fx_ticks_and_crypto_units_and_scope(self):
        for symbol, unit in [('EUR/USD', 'ticks'), ('BTCUSD', 'BTC'), ('ETHUSD', 'ETH')]:
            data = stats._normalize(symbol, payload(symbol), NOW)
            self.assertTrue(data['available'])
            self.assertEqual(data['volume_unit'], unit)
            self.assertEqual(data['scope'], 'aggregate')
            self.assertEqual(data['period'], 'provider_session')
            self.assertEqual(data['high'], 1.1227)
            self.assertEqual(data['expires_at'], NOW + 300)

    def test_stale_future_and_wrong_timestamp_units_unavailable(self):
        for stamp in [(NOW-301)*10**9, (NOW+31)*10**9, NOW*1000, None, True]:
            data = payload(); data['results'][0]['session']['last_updated'] = stamp
            self.assertFalse(stats._normalize('EUR/USD', data, NOW)['available'])

    def test_bad_prices_and_wrong_ticker_unavailable(self):
        for field, value in [('high', float('nan')), ('high', float('inf')), ('high', True),
                             ('low', 0), ('low', 2), ('high', '1.2')]:
            data = payload(); data['results'][0]['session'][field] = value
            self.assertFalse(stats._normalize('EUR/USD', data, NOW)['available'])
        for data in [None, [], {'status':'OK','results':{}}, payload('BTCUSD'), {'status':'DELAYED','results':[]}]:
            self.assertFalse(stats._normalize('EUR/USD', data, NOW)['available'])

    def test_volume_absent_is_not_zero_and_legitimate_zero_is_kept(self):
        for value in [None, -1, True, float('inf'), 0.5]:
            data = payload(); data['results'][0]['session']['volume'] = value
            result = stats._normalize('EUR/USD', data, NOW)
            self.assertTrue(result['available']); self.assertIsNone(result['volume'])
        data = payload(); data['results'][0]['session']['volume'] = 0
        self.assertEqual(stats._normalize('EUR/USD', data, NOW)['volume'], 0)

    @patch.dict(os.environ, {'MASSIVE_API_KEY':'test-only-secret'})
    def test_caching_request_contract_and_no_secret_in_response(self):
        response = Mock(status_code=200); response.json.return_value = payload()
        with patch.object(stats.requests, 'get', return_value=response) as get, patch.object(stats.time, 'time', return_value=NOW):
            first = self.call(); second = self.call()
            self.assertEqual(first.content, second.content)
            self.assertEqual(first['Cache-Control'], 'private, no-store')
            self.assertNotIn(b'test-only-secret', first.content)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args[0], 'https://api.massive.com/v3/snapshot')
            self.assertEqual(get.call_args.kwargs['params'], {'ticker':'C:EURUSD'})
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        with patch.object(stats.time, 'time', return_value=NOW+301):
            self.assertFalse(stats._read_stats('EUR/USD')['available'])

    @patch.dict(os.environ, {'MASSIVE_API_KEY':'test-only-secret'})
    def test_provider_failures_and_failure_cache(self):
        for failure in (requests.Timeout(), requests.ConnectionError(), ValueError()):
            stats._CACHE.clear()
            with patch.object(stats.requests, 'get', side_effect=failure) as get:
                self.assertFalse(stats._read_stats('EUR/USD')['available'])
                self.assertFalse(stats._read_stats('EUR/USD')['available'])
                self.assertEqual(get.call_count, 1)
        for code in (302, 401, 403, 429, 500):
            stats._CACHE.clear()
            with patch.object(stats.requests, 'get', return_value=Mock(status_code=code)):
                self.assertFalse(stats._read_stats('EUR/USD')['available'])

    @patch.dict(os.environ, {'MASSIVE_API_KEY':''})
    def test_no_key_no_request(self):
        with patch.object(stats.requests, 'get') as get:
            self.assertFalse(stats._read_stats('EUR/USD')['available'])
            get.assert_not_called()

    def test_mobile_race_throttle_and_unavailable(self):
        node = shutil.which('node')
        if not node: self.skipTest('Node unavailable')
        source = (Path(__file__).parents[1] / 'templates/simulator/trade/mobile.html').read_text()
        source = source[source.index('  const elStatHigh='):source.index('  function onHistory(')]
        source = re.sub(r'{%.*?%}', 'label', source)
        harness = '''
const assert=require('assert');
const elements={};
const document={hidden:false,getElementById:id=>elements[id]||(elements[id]={textContent:''})};
const session={currentSymbol:'EUR/USD'};
const priceFormatFor=()=>({precision:5});
const deferred=[];
const fetch=()=>new Promise(resolve=>deferred.push(resolve));
''' + source + '''
(async()=>{
 const data=(symbol)=>({symbol,available:true,high:1.1227,low:1.1172,volume:0,volume_unit:'ticks',expires_at:Date.now()/1000+300,updated_at:new Date().toISOString()});
 const first=recomputeDailyStats();
 session.currentSymbol='BTCUSD';resetDailyStats();
 const second=recomputeDailyStats();
 deferred[1]({ok:true,json:async()=>data('BTCUSD')});await second;
 const current=elStatHigh.textContent;
 deferred[0]({ok:true,json:async()=>({...data('EUR/USD'),high:99})});await first;
 assert.equal(elStatHigh.textContent,current);
 assert.equal(elStatVol.textContent,'0');
 await recomputeDailyStats();assert.equal(deferred.length,2);
 statsExpires=1;const expired=recomputeDailyStats();assert.equal(elStatHigh.textContent,'—');
 deferred[2]({ok:true,json:async()=>({symbol:'BTCUSD',available:false})});await expired;
 assert.equal(elStatHigh.textContent,'—');
 document.hidden=true;await recomputeDailyStats();assert.equal(deferred.length,3);
 console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        run = subprocess.run([node, '-e', harness], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('OK', run.stdout)
