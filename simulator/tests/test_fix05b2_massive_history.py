# simulator/tests/test_fix05b2_massive_history.py
"""
FIX-05B.2-B/C — Massive Forex Historical Integration.

Design lock: FIX-05B.2-B. Scope: Massive is Forex History ONLY for exactly
_MASSIVE_ENABLED_SYMBOLS (EUR/USD, GBP/USD, USD/JPY, AUD/USD) — no
WebSocket, no quote/resync, no XAU/USD, no historical cache. Finnhub stays
the live-tick provider for these same 4 pairs, untouched. Crypto history
(fetch_kline_history, Binance->Kraken) is a completely disjoint path.

Contract under test:
  - _massive_sym()/_MASSIVE_TF are pure mapping tables — no network.
  - fetch_massive_history() never raises for a normal provider failure
    (missing key, unsupported symbol/timeframe, HTTP 401/403/429/5xx,
    timeout, invalid JSON, status!=OK, missing/empty results, malformed
    row) — every one of those returns [], never a synthetic candle.
    results=[] is a legitimate, confirmed-real outcome (FIX-05B.2-A.1),
    never logged as a warning/error.
  - Pagination is bounded to _MASSIVE_MAX_PAGES, Authorization: Bearer is
    re-attached on every page.
  - generate_history() (consumers.py) dispatches Massive-enabled forex
    symbols to fetch_massive_history(), reuses the SAME _closed_only() the
    crypto path already uses (no per-provider closed-candle filter), and
    never lets a Massive symbol fall through to fetch_kline_history() or
    vice versa.
  - MASSIVE_API_KEY is never logged, in any error branch.

All tests are self-contained (no network) — provider responses are mocked
at the urllib.request.urlopen boundary (fetch_massive_history tests) or by
mocking self._feed.fetch_massive_history directly (generate_history
dispatch tests), same two-layer pattern test_fix05b1_real_history_
integrity.py already established for fetch_kline_history.
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, TestCase

from simulator.consumers import TradingConsumer, tf_seconds
from market_data.feeds import (
    FeedManager,
    _MASSIVE_ENABLED_SYMBOLS,
    _MASSIVE_MAX_PAGES,
    _MASSIVE_TF,
    _massive_range,
    _massive_sym,
)


def _run(coro):
    return asyncio.run(coro)


def _bare_history_consumer(symbol: str = "EUR/USD", timeframe: str = "1m") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._feed = MagicMock()
    c._feed.fetch_kline_history = AsyncMock(return_value=[])
    c._feed.fetch_massive_history = AsyncMock(return_value=[])
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c.account = {}
    c.send_json = AsyncMock()
    return c


def _sent(mock: AsyncMock, msg_type: str) -> "dict | None":
    for call in mock.await_args_list:
        if call.args[0]["type"] == msg_type:
            return call.args[0]
    return None


def _massive_row(t_ms: int, o=1.1578, h=1.1579, l=1.1577, c=1.15785, v=7.0, n=7) -> dict:
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "t": t_ms, "n": n, "vw": o}


def _massive_payload(rows: list, next_url: str = None, status: str = "OK", ticker: str = "C:EURUSD") -> dict:
    d = {
        "ticker": ticker, "queryCount": len(rows), "resultsCount": len(rows),
        "adjusted": True, "status": status, "request_id": "test-request-id",
        "count": len(rows),
    }
    if rows:
        d["results"] = rows
    if next_url:
        d["next_url"] = next_url
    return d


def _mock_urlopen_response(body: bytes, status: int = 200):
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = body
    m.status = status
    return m


# ─────────────────────────────────────────────────────────────────────────
# 1-5. Symbol mapping — pure, no network
# ─────────────────────────────────────────────────────────────────────────
class MassiveSymbolMappingTests(SimpleTestCase):
    def test_eurusd_maps_to_c_eurusd(self):
        self.assertEqual(_massive_sym("EUR/USD"), "C:EURUSD")

    def test_gbpusd_maps_to_c_gbpusd(self):
        self.assertEqual(_massive_sym("GBP/USD"), "C:GBPUSD")

    def test_usdjpy_maps_to_c_usdjpy(self):
        self.assertEqual(_massive_sym("USD/JPY"), "C:USDJPY")

    def test_audusd_maps_to_c_audusd(self):
        self.assertEqual(_massive_sym("AUD/USD"), "C:AUDUSD")

    def test_unsupported_symbol_returns_none(self):
        self.assertIsNone(_massive_sym("USD/CAD"))
        self.assertIsNone(_massive_sym("BTCUSD"))
        self.assertIsNone(_massive_sym("XAU/USD"))


# ─────────────────────────────────────────────────────────────────────────
# 6-11. Timeframe mapping — pure, no network
# ─────────────────────────────────────────────────────────────────────────
class MassiveTimeframeMappingTests(SimpleTestCase):
    def test_1s_maps_to_1_second(self):
        self.assertEqual(_MASSIVE_TF["1s"], (1, "second"))

    def test_1m_maps_to_1_minute(self):
        self.assertEqual(_MASSIVE_TF["1m"], (1, "minute"))

    def test_5m_maps_to_5_minute(self):
        self.assertEqual(_MASSIVE_TF["5m"], (5, "minute"))

    def test_15m_maps_to_15_minute(self):
        self.assertEqual(_MASSIVE_TF["15m"], (15, "minute"))

    def test_1h_maps_to_1_hour(self):
        self.assertEqual(_MASSIVE_TF["1h"], (1, "hour"))

    def test_1d_maps_to_1_day(self):
        self.assertEqual(_MASSIVE_TF["1d"], (1, "day"))

    def test_unsupported_timeframe_range_returns_none(self):
        self.assertIsNone(_massive_range("3m", 240))


# ─────────────────────────────────────────────────────────────────────────
# 12-28. fetch_massive_history() — network mocked at urllib boundary
# ─────────────────────────────────────────────────────────────────────────
class MassiveFetchHistoryTests(TestCase):
    def setUp(self):
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", "test-key-not-real-1234567890ab")
        self._key_patch.start()
        self.feed = FeedManager()

    def tearDown(self):
        self._key_patch.stop()

    def _run_fetch(self, symbol="EUR/USD", interval="1m", limit=5):
        return _run(self.feed.fetch_massive_history(symbol, interval=interval, limit=limit))

    def test_ohlcv_normalization(self):
        row = _massive_row(1788123600000, o=1.15781, h=1.15791, l=1.15771, c=1.15783, v=42.0)
        body = json.dumps(_massive_payload([row])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(len(bars), 1)
        b = bars[0]
        self.assertEqual(set(b.keys()), {"time", "open", "high", "low", "close", "volume"})
        self.assertEqual(b["open"], 1.15781)
        self.assertEqual(b["high"], 1.15791)
        self.assertEqual(b["low"], 1.15771)
        self.assertEqual(b["close"], 1.15783)
        self.assertEqual(b["volume"], 42.0)
        self.assertIsInstance(b["time"], int)
        for k in ("open", "high", "low", "close", "volume"):
            self.assertIsInstance(b[k], float)

    def test_ms_to_seconds(self):
        row = _massive_row(1788123600000)
        body = json.dumps(_massive_payload([row])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(bars[0]["time"], 1788123600)

    def test_ordering_oldest_to_newest(self):
        rows = [_massive_row(3000), _massive_row(1000), _massive_row(2000)]
        body = json.dumps(_massive_payload(rows)).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual([b["time"] for b in bars], [1, 2, 3])

    def test_malformed_row_skipped_others_kept(self):
        good1 = _massive_row(1000)
        bad = {"o": 1.1, "h": 1.2, "l": 1.0, "t": 2000}  # missing 'c'
        good2 = _massive_row(3000)
        body = json.dumps(_massive_payload([good1, bad, good2])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(len(bars), 2)
        self.assertEqual([b["time"] for b in bars], [1, 3])

    def test_empty_results_returns_empty_list(self):
        body = json.dumps(_massive_payload([])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_missing_results_key_returns_empty_list(self):
        payload = {"ticker": "C:EURUSD", "status": "OK", "resultsCount": 0}
        body = json.dumps(payload).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_http_401_returns_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 401, "unauthorized", {}, None)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_http_403_returns_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 403, "forbidden", {}, None)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_http_429_returns_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 429, "rate limited", {}, None)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_timeout_returns_empty_list(self):
        import socket
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_http_500_returns_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 500, "server error", {}, None)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_urlerror_returns_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=URLError("unreachable")):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_invalid_json_returns_empty_list(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(b"not json{{{")):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_status_error_returns_empty_list(self):
        body = json.dumps(_massive_payload([_massive_row(1000)], status="ERROR")).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars = self._run_fetch()
        self.assertEqual(bars, [])

    def test_missing_api_key_returns_empty_list_no_network(self):
        self._key_patch.stop()
        with patch("market_data.feeds.MASSIVE_API_KEY", ""):
            with patch("urllib.request.urlopen") as mock_urlopen:
                bars = self._run_fetch()
            mock_urlopen.assert_not_called()
        self._key_patch.start()
        self.assertEqual(bars, [])

    def test_unsupported_symbol_returns_empty_list_no_network(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            bars = self._run_fetch(symbol="USD/CAD")
            mock_urlopen.assert_not_called()
        self.assertEqual(bars, [])

    def test_unsupported_timeframe_returns_empty_list_no_network(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            bars = self._run_fetch(interval="3m")
            mock_urlopen.assert_not_called()
        self.assertEqual(bars, [])

    def test_pagination_bounded_to_max_pages(self):
        # Every page returns 1 row + a next_url -> would paginate forever
        # without the _MASSIVE_MAX_PAGES cap. limit=100 so len(results)
        # never reaches `limit` on its own, forcing the cap to be what
        # actually stops it.
        responses = [
            _mock_urlopen_response(json.dumps(_massive_payload(
                [_massive_row(1000 * (i + 1))], next_url=f"https://api.massive.com/next?page={i}"
            )).encode())
            for i in range(6)  # far more pages available than the cap allows
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as mock_urlopen:
            bars = self._run_fetch(limit=100)
        self.assertEqual(mock_urlopen.call_count, _MASSIVE_MAX_PAGES)
        self.assertEqual(len(bars), _MASSIVE_MAX_PAGES)

    def test_next_url_reattaches_authorization_header(self):
        page1 = _mock_urlopen_response(json.dumps(_massive_payload(
            [_massive_row(1000)], next_url="https://api.massive.com/next?cursor=abc"
        )).encode())
        page2 = _mock_urlopen_response(json.dumps(_massive_payload([_massive_row(2000)])).encode())
        captured_requests = []

        def _capture(req, timeout=None):
            captured_requests.append(req)
            return [page1, page2][len(captured_requests) - 1]

        with patch("urllib.request.urlopen", side_effect=_capture):
            self._run_fetch(limit=100)
        self.assertEqual(len(captured_requests), 2)
        for req in captured_requests:
            self.assertTrue(req.get_header("Authorization").startswith("Bearer "))

    def test_dedupe_by_timestamp_first_occurrence_wins(self):
        page1 = _mock_urlopen_response(json.dumps(_massive_payload(
            [_massive_row(1000, c=1.1000)], next_url="https://api.massive.com/next"
        )).encode())
        page2 = _mock_urlopen_response(json.dumps(_massive_payload(
            [_massive_row(1000, c=9.9999), _massive_row(2000)]  # same ts=1000 repeated, different close
        )).encode())
        with patch("urllib.request.urlopen", side_effect=[page1, page2]):
            bars = self._run_fetch(limit=100)
        times = [b["time"] for b in bars]
        self.assertEqual(len(times), len(set(times)), "no duplicate timestamps in the final result")
        self.assertEqual(bars[0]["close"], 1.1000, "first occurrence (page 1) must win, not be overwritten")


# ─────────────────────────────────────────────────────────────────────────
# 29-34. generate_history() dispatch — mocking self._feed.fetch_massive_history
# ─────────────────────────────────────────────────────────────────────────
class MassiveGenerateHistoryDispatchTests(TestCase):
    def test_eurusd_dispatches_to_massive_not_kline(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._feed.fetch_massive_history = AsyncMock(return_value=[
            {"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 1.0},
        ])
        result = _run(c.generate_history("EUR/USD", "1m", bars=240))
        c._feed.fetch_massive_history.assert_called_once_with("EUR/USD", interval="1m", limit=240)
        c._feed.fetch_kline_history.assert_not_called()
        self.assertIsNotNone(result)

    def test_gbpusd_dispatches_to_massive(self):
        c = _bare_history_consumer(symbol="GBP/USD", timeframe="1h")
        c._feed.fetch_massive_history = AsyncMock(return_value=[
            {"time": 1000, "open": 1.3, "high": 1.31, "low": 1.29, "close": 1.305, "volume": 5.0},
        ])
        _run(c.generate_history("GBP/USD", "1h", bars=240))
        c._feed.fetch_massive_history.assert_called_once_with("GBP/USD", interval="1h", limit=240)
        c._feed.fetch_kline_history.assert_not_called()

    def test_closed_only_integration_still_forming_bar_excluded(self):
        import time as _time
        now = int(_time.time())
        still_forming = {"time": now, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0}
        closed = {"time": now - 120, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0}
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._feed.fetch_massive_history = AsyncMock(return_value=[closed, still_forming])
        result = _run(c.generate_history("EUR/USD", "1m", bars=240))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["time"], now - 120)

    def test_unsupported_symbol_returns_none_no_massive_call(self):
        c = _bare_history_consumer(symbol="USD/CAD", timeframe="1m")
        result = _run(c.generate_history("USD/CAD", "1m", bars=240))
        self.assertIsNone(result)
        c._feed.fetch_massive_history.assert_not_called()
        _run(c._send_history_or_unavailable("USD/CAD", "1m", result))
        msg = _sent(c.send_json, "history_unavailable")
        self.assertEqual(msg["reason"], "no_real_history")

    def test_massive_enabled_symbol_empty_result_provider_unavailable_reason(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1h")
        c._feed.fetch_massive_history = AsyncMock(return_value=[])
        result = _run(c.generate_history("EUR/USD", "1h", bars=240))
        self.assertIsNone(result)
        _run(c._send_history_or_unavailable("EUR/USD", "1h", result))
        msg = _sent(c.send_json, "history_unavailable")
        self.assertEqual(msg["reason"], "provider_unavailable")
        self.assertEqual(msg["symbol"], "EUR/USD")

    def test_no_synthetic_fallback_for_massive_path(self):
        import inspect
        from simulator import consumers as consumers_module
        src = inspect.getsource(consumers_module.TradingConsumer.generate_history)
        self.assertNotIn("random.Random(", src)
        self.assertNotIn("rnd.random()", src)


# ─────────────────────────────────────────────────────────────────────────
# 35-37. Regressions — Finnhub live / crypto history / FIX-05C liveMid
# ─────────────────────────────────────────────────────────────────────────
class MassiveRegressionTests(SimpleTestCase):
    def test_finnhub_live_path_unaffected(self):
        import inspect
        from market_data import feeds as feeds_module
        src = inspect.getsource(feeds_module)
        # Finnhub's own live-tick loop and symbol helper are untouched —
        # still present, still doing exactly what they did before this block.
        self.assertIn("_finnhub_loop", src)
        self.assertIn("_finnhub_sym", src)
        self.assertIn("FINNHUB_API_KEY", src)

    def test_crypto_kline_symbols_unaffected(self):
        from market_data.symbol_specs import kline_symbols
        syms = kline_symbols()
        self.assertIn("BTCUSD", syms)
        self.assertIn("ETHUSD", syms)
        # Crypto symbols are never in the Massive allowlist, and vice versa —
        # the two dispatch paths in generate_history() are disjoint.
        self.assertTrue(syms.isdisjoint(_MASSIVE_ENABLED_SYMBOLS))


class MassiveFix05cLiveMidRegressionTests(SimpleTestCase):
    """dashboard.html is untouched by this block (FIX-05B.2-C's file
    allowlist), so this re-runs the exact FIX-05C contract check
    (Fix05cContractRegressionTests, test_fix05b1_real_history_integrity.py)
    against the same frontend history handler — confirms the Massive
    branch feeding into the SAME 'history' message type never gains a
    liveMid write, without duplicating a Python-source heuristic that
    would just re-describe generate_history()'s own docstring comment."""

    def test_history_handler_still_does_not_write_live_mid(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            src = f.read()
        i = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        j = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){", i)
        self.assertNotIn("liveMid", src[i:j])


# ─────────────────────────────────────────────────────────────────────────
# 38. API key never logged
# ─────────────────────────────────────────────────────────────────────────
class MassiveSecretNotLoggedTests(TestCase):
    def setUp(self):
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", "FAKEtestONLYnotREALkey1234567xx")
        self._key_patch.start()
        self.feed = FeedManager()

    def tearDown(self):
        self._key_patch.stop()

    def _assert_key_never_logged(self, cm):
        secret = "FAKEtestONLYnotREALkey1234567xx"
        for record in cm.records:
            self.assertNotIn(secret, record.getMessage())
            self.assertNotIn("Bearer", record.getMessage())

    def test_key_not_logged_on_401(self):
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", side_effect=HTTPError("url", 401, "unauthorized", {}, None)):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)

    def test_key_not_logged_on_403(self):
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", side_effect=HTTPError("url", 403, "forbidden", {}, None)):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)

    def test_key_not_logged_on_429(self):
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", side_effect=HTTPError("url", 429, "rate limited", {}, None)):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)

    def test_key_not_logged_on_5xx(self):
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", side_effect=HTTPError("url", 500, "server error", {}, None)):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)

    def test_key_not_logged_on_invalid_json(self):
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(b"{{{not json")):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)

    def test_key_not_logged_on_success(self):
        body = json.dumps(_massive_payload([_massive_row(1000)])).encode()
        with self.assertLogs("simulator.ws", level="DEBUG") as cm:
            with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
                _run(self.feed.fetch_massive_history("EUR/USD", interval="1m", limit=5))
        self._assert_key_never_logged(cm)
