"""Exercise provider base-bar limits without network calls or trading writes."""
import asyncio
import json
from urllib.parse import urlparse, parse_qs
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
from market_data.feeds import FeedManager


class HourlyHistoryPageBudgetTests(SimpleTestCase):
    def test_all_assets_receive_dense_first_page_and_preserve_output_limit(self):
        for symbol in ('EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD', 'BTCUSD', 'ETHUSD'):
            for interval, minutes in (('1h', 60), ('4h', 240)):
                with self.subTest(symbol=symbol, interval=interval):
                    feed = FeedManager()
                    method = (feed.fetch_massive_history_first_page if '/' in symbol
                              else feed.fetch_massive_crypto_history_first_page)
                    def response(request, **kwargs):
                        budget = int(parse_qs(urlparse(request.full_url).query)['limit'][0])
                        self.assertLessEqual(budget, 50000)
                        # Simulate provider aggregating input minutes into candles.
                        size = budget // minutes
                        rows = [{'t': (1800000000 - i * minutes * 60) * 1000,
                                 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 1}
                                for i in range(size)]
                        result = MagicMock()
                        result.__enter__.return_value = result
                        result.read.return_value = json.dumps({
                            'status': 'OK', 'results': rows,
                            'next_url': 'https://api.massive.com/next'}).encode()
                        return result
                    with patch('market_data.feeds.MASSIVE_API_KEY', 'test-only'), patch(
                            'urllib.request.urlopen', side_effect=response) as http:
                        bars, cursor = asyncio.run(method(symbol, interval, 240))
                    self.assertEqual(http.call_count, 1)
                    self.assertGreaterEqual(len(bars), 200)
                    self.assertLessEqual(len(bars), 240)
                    self.assertEqual(bars, sorted(bars, key=lambda b: b['time']))
                    if interval == '4h':
                        self.assertEqual(cursor.limit, 240)
                        self.assertEqual(cursor.pages_so_far, 1)
                    else:
                        self.assertIsNone(cursor)
