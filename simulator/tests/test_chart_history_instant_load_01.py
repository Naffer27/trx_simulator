# simulator/tests/test_chart_history_instant_load_01.py
"""
CHART-HISTORY-INSTANT-LOAD-01 — first-paint history split.

Design lock: fetch_massive_history()/fetch_massive_crypto_history() are
split into a `_first_page` fetcher (exactly ONE REST call, sort=desc)
and a `_remaining` fetcher (continues from an opaque `_MassiveHistoryCursor`
through the rest of the pagination depth), composed back into the
original function names for 100% backward compatibility. consumers.py
mirrors the split with generate_history_first_page()/
_complete_history_depth() — the first is awaited inline by receive() for
a fast paint (~0.3s), the second runs as a detached asyncio.Task so the
consumer's receive() loop is never blocked waiting for full pagination
depth. A depth-completion response is guarded by a
generation/symbol/timeframe check and — on the frontend — only ever
PREPENDS bars strictly older than the earliest bar already on the chart,
never replacing the live-tracked recent portion.

Scope: Forex and Crypto both, mirrored per the "duplicate, don't share"
design decision already established for the fetch logic itself
(_finalize_massive_bars is the one deliberate, provider-agnostic
exception). No WebSocket, tick rate, priceLine, PnL, margin, execution,
stopout, Redis price cache, or provider-routing code is touched by this
block.
"""
import asyncio
import inspect
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, TestCase

from market_data.feeds import (
    FeedManager,
    _finalize_massive_bars,
    _MassiveHistoryCursor,
    _MASSIVE_CRYPTO_MAX_PAGES,
    _MASSIVE_FOREX_MAX_PAGES,
)
import simulator.consumers as consumers_module
from simulator.consumers import TradingConsumer


def _run(coro):
    return asyncio.run(coro)


def _mock_urlopen_response(body: bytes, status: int = 200):
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = body
    m.status = status
    return m


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


def _bare_history_consumer(symbol: str = "EUR/USD", timeframe: str = "1m") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c.symbol = symbol
    c.timeframe = timeframe
    c._feed = MagicMock()
    c._feed.fetch_kline_history = AsyncMock(return_value=[])
    c._feed.fetch_massive_history_first_page = AsyncMock(return_value=([], None))
    c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[])
    c._feed.fetch_massive_crypto_history_first_page = AsyncMock(return_value=([], None))
    c._feed.fetch_massive_crypto_history_remaining = AsyncMock(return_value=[])
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c.account = {}
    c._history_generation = 0
    c._history_depth_task = None
    c.send_json = AsyncMock()
    return c


def _sent(mock: AsyncMock, msg_type: str) -> "dict | None":
    for call in mock.await_args_list:
        if call.args[0]["type"] == msg_type:
            return call.args[0]
    return None


def _all_sent(mock: AsyncMock, msg_type: str) -> list:
    return [call.args[0] for call in mock.await_args_list if call.args[0]["type"] == msg_type]


# ─────────────────────────────────────────────────────────────────────────
# _finalize_massive_bars — shared, pure, provider-agnostic
# ─────────────────────────────────────────────────────────────────────────
class FinalizeMassiveBarsTests(SimpleTestCase):
    def test_dedupes_by_timestamp_first_occurrence_wins(self):
        bars = [
            {"time": 300, "close": 1.0},
            {"time": 300, "close": 9.9},  # duplicate, later in DESC arrival — dropped
            {"time": 200, "close": 2.0},
        ]
        result = _finalize_massive_bars(bars, limit=10)
        self.assertEqual([b["time"] for b in result], [200, 300])
        self.assertEqual(result[1]["close"], 1.0)

    def test_trims_to_limit_keeping_most_recent(self):
        bars = [{"time": t, "close": 0.0} for t in (500, 400, 300, 200, 100)]  # DESC
        result = _finalize_massive_bars(bars, limit=3)
        self.assertEqual([b["time"] for b in result], [300, 400, 500])

    def test_sorts_ascending(self):
        bars = [{"time": 5}, {"time": 1}, {"time": 3}]
        result = _finalize_massive_bars(bars, limit=10)
        self.assertEqual([b["time"] for b in result], [1, 3, 5])

    def test_empty_input(self):
        self.assertEqual(_finalize_massive_bars([], limit=10), [])


class MassiveHistoryCursorTests(SimpleTestCase):
    def test_cursor_carries_expected_fields(self):
        cur = _MassiveHistoryCursor(
            symbol="EUR/USD", interval="15m", limit=200,
            bars_so_far=[{"time": 1}], pages_so_far=1, next_url="https://x",
        )
        self.assertEqual(cur.symbol, "EUR/USD")
        self.assertEqual(cur.interval, "15m")
        self.assertEqual(cur.limit, 200)
        self.assertEqual(cur.pages_so_far, 1)
        self.assertEqual(cur.next_url, "https://x")


# ─────────────────────────────────────────────────────────────────────────
# fetch_massive_history_first_page / _remaining / composed — Forex
# ─────────────────────────────────────────────────────────────────────────
class MassiveForexFirstPageSplitTests(TestCase):
    def setUp(self):
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", "test-key-not-real-1234567890ab")
        self._key_patch.start()
        self.feed = FeedManager()

    def tearDown(self):
        self._key_patch.stop()

    def test_single_page_reaches_limit_no_cursor(self):
        body = json.dumps(_massive_payload([_massive_row(1000 + i * 1000) for i in range(5)])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)) as mock_urlopen:
            bars, cursor = _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1m", limit=5))
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIsNone(cursor)
        self.assertEqual(len(bars), 5)

    def test_short_page_with_next_url_returns_cursor(self):
        body = json.dumps(_massive_payload(
            [_massive_row(1000 + i * 1000) for i in range(3)],
            next_url="https://api.massive.com/next?cursor=abc",
        )).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)) as mock_urlopen:
            bars, cursor = _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1m", limit=200))
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.symbol, "EUR/USD")
        self.assertEqual(cursor.limit, 200)
        self.assertEqual(cursor.pages_so_far, 1)
        self.assertEqual(len(bars), 3)

    def test_short_page_no_next_url_no_cursor(self):
        # Sparse timeframe genuinely exhausted — no more pages exist.
        body = json.dumps(_massive_payload([_massive_row(1000)])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars, cursor = _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1h", limit=200))
        self.assertIsNone(cursor)
        self.assertEqual(len(bars), 1)

    def test_first_page_url_uses_sort_desc(self):
        body = json.dumps(_massive_payload([_massive_row(1000)])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)) as mock_urlopen:
            _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1m", limit=200))
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("sort=desc", called_url)

    def test_first_page_failure_returns_empty_no_cursor(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            bars, cursor = _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1m", limit=200))
        self.assertEqual(bars, [])
        self.assertIsNone(cursor)

    def test_remaining_continues_from_cursor_and_merges(self):
        page1_bars = [_massive_row(3000 + i * 1000) for i in range(2)]  # DESC-arrival raw rows
        cursor = _MassiveHistoryCursor(
            symbol="EUR/USD", interval="1m", limit=10,
            bars_so_far=[{"time": int(r["t"]) // 1000, "open": r["o"], "high": r["h"],
                          "low": r["l"], "close": r["c"], "volume": r["v"]} for r in page1_bars],
            pages_so_far=1, next_url="https://api.massive.com/next?cursor=abc",
        )
        page2_body = json.dumps(_massive_payload([_massive_row(1000), _massive_row(2000)])).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(page2_body)) as mock_urlopen:
            result = _run(self.feed.fetch_massive_history_remaining(cursor))
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual([b["time"] for b in result], [1, 2, 3, 4])

    def test_remaining_bounded_to_max_pages(self):
        cursor = _MassiveHistoryCursor(
            symbol="EUR/USD", interval="1m", limit=1000,
            bars_so_far=[], pages_so_far=1, next_url="https://api.massive.com/next?p=1",
        )
        counter = {"n": 0}

        def side_effect(req, *a, **kw):
            counter["n"] += 1
            t = 9000 + counter["n"] * 60
            body = json.dumps(_massive_payload([_massive_row(t * 1000)],
                               next_url=f"https://api.massive.com/next?p={counter['n']}")).encode()
            return _mock_urlopen_response(body)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = _run(self.feed.fetch_massive_history_remaining(cursor))
        self.assertEqual(counter["n"], _MASSIVE_FOREX_MAX_PAGES - 1)  # page 1 already consumed by cursor
        self.assertEqual(len(result), _MASSIVE_FOREX_MAX_PAGES - 1)

    def test_composed_function_matches_first_page_plus_remaining(self):
        page1_body = json.dumps(_massive_payload(
            [_massive_row(2000), _massive_row(1000)],
            next_url="https://api.massive.com/next?p=1",
        )).encode()
        page2_body = json.dumps(_massive_payload([_massive_row(3000)])).encode()
        with patch("urllib.request.urlopen", side_effect=[
            _mock_urlopen_response(page1_body), _mock_urlopen_response(page2_body),
        ]):
            composed = _run(self.feed.fetch_massive_history("EUR/USD", "1m", limit=3))
        with patch("urllib.request.urlopen", side_effect=[
            _mock_urlopen_response(page1_body), _mock_urlopen_response(page2_body),
        ]):
            first, cursor = _run(self.feed.fetch_massive_history_first_page("EUR/USD", "1m", limit=3))
            split = _run(self.feed.fetch_massive_history_remaining(cursor))
        self.assertEqual(composed, split)
        self.assertEqual([b["time"] for b in composed], [1, 2, 3])


# ─────────────────────────────────────────────────────────────────────────
# fetch_massive_crypto_history_first_page / _remaining / composed — Crypto
# ─────────────────────────────────────────────────────────────────────────
class MassiveCryptoFirstPageSplitTests(TestCase):
    def setUp(self):
        self._key_patch = patch("market_data.feeds.MASSIVE_API_KEY", "test-key-not-real-1234567890ab")
        self._key_patch.start()
        self.feed = FeedManager()

    def tearDown(self):
        self._key_patch.stop()

    def test_single_page_reaches_limit_no_cursor(self):
        body = json.dumps(_massive_payload([_massive_row(1000 + i * 1000) for i in range(5)],
                                            ticker="X:BTCUSD")).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)) as mock_urlopen:
            bars, cursor = _run(self.feed.fetch_massive_crypto_history_first_page("BTCUSD", "1m", limit=5))
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIsNone(cursor)
        self.assertEqual(len(bars), 5)

    def test_short_page_with_next_url_returns_cursor(self):
        body = json.dumps(_massive_payload(
            [_massive_row(1000)], next_url="https://api.massive.com/next?c=1", ticker="X:BTCUSD",
        )).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
            bars, cursor = _run(self.feed.fetch_massive_crypto_history_first_page("BTCUSD", "15m", limit=200))
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.symbol, "BTCUSD")

    def test_remaining_bounded_to_crypto_max_pages(self):
        cursor = _MassiveHistoryCursor(
            symbol="BTCUSD", interval="1m", limit=1000,
            bars_so_far=[], pages_so_far=1, next_url="https://api.massive.com/next?p=1",
        )
        counter = {"n": 0}

        def side_effect(req, *a, **kw):
            counter["n"] += 1
            t = 9000 + counter["n"] * 60
            body = json.dumps(_massive_payload([_massive_row(t * 1000)],
                               next_url=f"https://api.massive.com/next?p={counter['n']}",
                               ticker="X:BTCUSD")).encode()
            return _mock_urlopen_response(body)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = _run(self.feed.fetch_massive_crypto_history_remaining(cursor))
        self.assertEqual(counter["n"], _MASSIVE_CRYPTO_MAX_PAGES - 1)
        self.assertEqual(len(result), _MASSIVE_CRYPTO_MAX_PAGES - 1)

    def test_composed_function_matches_first_page_plus_remaining(self):
        page1_body = json.dumps(_massive_payload(
            [_massive_row(2000), _massive_row(1000)],
            next_url="https://api.massive.com/next?p=1", ticker="X:BTCUSD",
        )).encode()
        page2_body = json.dumps(_massive_payload([_massive_row(3000)], ticker="X:BTCUSD")).encode()
        with patch("urllib.request.urlopen", side_effect=[
            _mock_urlopen_response(page1_body), _mock_urlopen_response(page2_body),
        ]):
            composed = _run(self.feed.fetch_massive_crypto_history("BTCUSD", "1m", limit=3))
        with patch("urllib.request.urlopen", side_effect=[
            _mock_urlopen_response(page1_body), _mock_urlopen_response(page2_body),
        ]):
            first, cursor = _run(self.feed.fetch_massive_crypto_history_first_page("BTCUSD", "1m", limit=3))
            split = _run(self.feed.fetch_massive_crypto_history_remaining(cursor))
        self.assertEqual(composed, split)
        self.assertEqual([b["time"] for b in composed], [1, 2, 3])


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — generate_history_first_page() dispatch (mirrors
# generate_history()'s three-way dispatch, single-call fetchers only)
# ─────────────────────────────────────────────────────────────────────────
class GenerateHistoryFirstPageDispatchTests(TestCase):
    def test_crypto_symbol_dispatches_to_crypto_first_page(self):
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        c._feed.fetch_massive_crypto_history_first_page = AsyncMock(return_value=(
            [{"time": 1000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}], None,
        ))
        hist, cursor = _run(c.generate_history_first_page("BTCUSD", "1m", bars=240))
        c._feed.fetch_massive_crypto_history_first_page.assert_called_once_with("BTCUSD", interval="1m", limit=240)
        c._feed.fetch_massive_history_first_page.assert_not_called()
        c._feed.fetch_kline_history.assert_not_called()
        self.assertIsNotNone(hist)
        self.assertIsNone(cursor)

    def test_forex_symbol_dispatches_to_forex_first_page(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1h")
        c._feed.fetch_massive_history_first_page = AsyncMock(return_value=(
            [{"time": 1000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}], None,
        ))
        _run(c.generate_history_first_page("EUR/USD", "1h", bars=240))
        c._feed.fetch_massive_history_first_page.assert_called_once_with("EUR/USD", interval="1h", limit=240)
        c._feed.fetch_massive_crypto_history_first_page.assert_not_called()
        c._feed.fetch_kline_history.assert_not_called()

    def test_kline_symbol_uses_full_single_call_fetch_no_cursor(self):
        # BTCUSD/ETHUSD remain _KLINE_SYMBOLS members but are now Massive-
        # crypto-first at runtime (checked ahead of the kline chain) — a
        # real exercise of the kline-only branch needs a symbol in
        # _KLINE_SYMBOLS but NOT in _MASSIVE_CRYPTO_ENABLED_SYMBOLS.
        kline_only = consumers_module._KLINE_SYMBOLS - consumers_module._MASSIVE_CRYPTO_ENABLED_SYMBOLS
        if not kline_only:
            self.skipTest("no pure-kline symbol configured in this environment")
        sym = next(iter(kline_only))
        c = _bare_history_consumer(symbol=sym, timeframe="1m")
        c._feed.fetch_kline_history = AsyncMock(return_value=[
            {"time": 1000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        hist, cursor = _run(c.generate_history_first_page(sym, "1m", bars=240))
        c._feed.fetch_kline_history.assert_called_once_with(sym, interval="1m", limit=240)
        self.assertIsNone(cursor)
        self.assertIsNotNone(hist)

    def test_unsupported_symbol_returns_none_none(self):
        c = _bare_history_consumer(symbol="USD/CAD", timeframe="1m")
        hist, cursor = _run(c.generate_history_first_page("USD/CAD", "1m", bars=240))
        self.assertIsNone(hist)
        self.assertIsNone(cursor)
        c._feed.fetch_massive_history_first_page.assert_not_called()
        c._feed.fetch_massive_crypto_history_first_page.assert_not_called()

    def test_closed_only_filters_still_forming_bar(self):
        import time as _time
        now = int(_time.time())
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._feed.fetch_massive_history_first_page = AsyncMock(return_value=([
            {"time": now - 120, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
            {"time": now, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},  # still forming
        ], None))
        hist, cursor = _run(c.generate_history_first_page("EUR/USD", "1m", bars=240))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["time"], now - 120)

    def test_empty_closed_bars_returns_none_but_preserves_cursor(self):
        # A cursor may still be worth chasing (more pages might contain a
        # real closed bar) even though page 1 alone had nothing closed.
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._feed.fetch_massive_history_first_page = AsyncMock(return_value=([], cursor))
        hist, returned_cursor = _run(c.generate_history_first_page("EUR/USD", "1m", bars=240))
        self.assertIsNone(hist)
        self.assertIs(returned_cursor, cursor)

    def test_price_state_snapped_from_first_page_last_close(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._feed.fetch_massive_history_first_page = AsyncMock(return_value=([
            {"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 1.0},
        ], None))
        _run(c.generate_history_first_page("EUR/USD", "1m", bars=240))
        self.assertEqual(c._price_state["EUR/USD"], 1.15)
        self.assertIn("EUR/USD", c._bid_state)
        self.assertIn("EUR/USD", c._ask_state)

    def test_generate_history_and_first_page_dispatch_identically(self):
        # Structural parity check — both methods must check the same three
        # symbol sets in the same order (crypto -> kline -> massive-forex).
        gh_src = inspect.getsource(consumers_module.TradingConsumer.generate_history)
        ghfp_src = inspect.getsource(consumers_module.TradingConsumer.generate_history_first_page)
        for marker in ("_MASSIVE_CRYPTO_ENABLED_SYMBOLS", "_KLINE_SYMBOLS", "_MASSIVE_ENABLED_SYMBOLS"):
            self.assertIn(marker, gh_src)
            self.assertIn(marker, ghfp_src)
        gh_order = [gh_src.index(m) for m in ("_MASSIVE_CRYPTO_ENABLED_SYMBOLS", "_KLINE_SYMBOLS", "_MASSIVE_ENABLED_SYMBOLS")]
        ghfp_order = [ghfp_src.index(m) for m in ("_MASSIVE_CRYPTO_ENABLED_SYMBOLS", "_KLINE_SYMBOLS", "_MASSIVE_ENABLED_SYMBOLS")]
        self.assertEqual(gh_order, sorted(gh_order))
        self.assertEqual(ghfp_order, sorted(ghfp_order))


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — _complete_history_depth() staleness guards + phase="complete"
# ─────────────────────────────────────────────────────────────────────────
class CompleteHistoryDepthTests(TestCase):
    def test_matching_generation_symbol_timeframe_sends_complete(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 5
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        _run(c._complete_history_depth("EUR/USD", "1m", 5, cursor))
        msg = _sent(c.send_json, "history")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["phase"], "complete")
        self.assertEqual(msg["symbol"], "EUR/USD")

    def test_stale_generation_drops_silently(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 7  # a newer switch already happened
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        _run(c._complete_history_depth("EUR/USD", "1m", 5, cursor))  # captured gen=5, stale
        c.send_json.assert_not_awaited()

    def test_stale_symbol_drops_silently(self):
        c = _bare_history_consumer(symbol="GBP/USD", timeframe="1m")  # user already switched
        c._history_generation = 5
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        _run(c._complete_history_depth("EUR/USD", "1m", 5, cursor))
        c.send_json.assert_not_awaited()

    def test_stale_timeframe_drops_silently(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1h")  # user already switched TF
        c._history_generation = 5
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        _run(c._complete_history_depth("EUR/USD", "1m", 5, cursor))
        c.send_json.assert_not_awaited()

    def test_empty_remaining_result_sends_nothing(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 5
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[])
        _run(c._complete_history_depth("EUR/USD", "1m", 5, cursor))
        c.send_json.assert_not_awaited()

    def test_crypto_symbol_dispatches_to_crypto_remaining(self):
        c = _bare_history_consumer(symbol="BTCUSD", timeframe="1m")
        c._history_generation = 1
        cursor = _MassiveHistoryCursor(symbol="BTCUSD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_crypto_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ])
        _run(c._complete_history_depth("BTCUSD", "1m", 1, cursor))
        c._feed.fetch_massive_crypto_history_remaining.assert_called_once_with(cursor)
        c._feed.fetch_massive_history_remaining.assert_not_called()

    def test_does_not_resnap_price_state(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 1
        c._price_state["EUR/USD"] = 1.2345  # set by the earlier first-page phase
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[
            {"time": 500, "open": 0.1, "high": 0.1, "low": 0.1, "close": 0.1, "volume": 1},
        ])
        _run(c._complete_history_depth("EUR/USD", "1m", 1, cursor))
        self.assertEqual(c._price_state["EUR/USD"], 1.2345)  # untouched by the older/depth bars

    def test_cancelled_error_reraised(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 1
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            _run(c._complete_history_depth("EUR/USD", "1m", 1, cursor))

    def test_other_exception_swallowed_not_raised(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._history_generation = 1
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        c._feed.fetch_massive_history_remaining = AsyncMock(side_effect=RuntimeError("boom"))
        _run(c._complete_history_depth("EUR/USD", "1m", 1, cursor))  # must not raise
        c.send_json.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — _start_history_depth() task-creation / cancellation rule
# ─────────────────────────────────────────────────────────────────────────
class StartHistoryDepthTests(TestCase):
    def test_cursor_none_creates_no_task(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        c._start_history_depth("EUR/USD", "1m", None)
        self.assertIsNone(c._history_depth_task)

    async def _create_and_get(self, c, symbol, timeframe, cursor):
        c._start_history_depth(symbol, timeframe, cursor)
        task = c._history_depth_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return task

    def test_cursor_present_creates_task(self):
        c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
        cursor = _MassiveHistoryCursor(symbol="EUR/USD", interval="1m", limit=240,
                                        bars_so_far=[], pages_so_far=1, next_url="https://x")
        task = _run(self._create_and_get(c, "EUR/USD", "1m", cursor))
        self.assertIsInstance(task, asyncio.Task)

    def test_previous_inflight_task_is_cancelled(self):
        async def scenario():
            c = _bare_history_consumer(symbol="EUR/USD", timeframe="1m")
            never_done = asyncio.get_event_loop().create_future()
            c._history_depth_task = asyncio.ensure_future(self._never_resolving(never_done))
            old_task = c._history_depth_task
            cursor = _MassiveHistoryCursor(symbol="GBP/USD", interval="1m", limit=240,
                                            bars_so_far=[], pages_so_far=1, next_url="https://x")
            c._feed.fetch_massive_history_remaining = AsyncMock(return_value=[])
            c._start_history_depth("GBP/USD", "1m", cursor)
            await asyncio.sleep(0)  # let cancellation propagate
            self.assertTrue(old_task.cancelled() or old_task.done())
            new_task = c._history_depth_task
            if new_task:
                new_task.cancel()
                try:
                    await new_task
                except asyncio.CancelledError:
                    pass
        _run(scenario())

    async def _never_resolving(self, fut):
        await fut


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — _send_history_or_unavailable() phase contract
# ─────────────────────────────────────────────────────────────────────────
class SendHistoryPhaseContractTests(TestCase):
    def test_history_message_includes_phase_initial(self):
        c = _bare_history_consumer()
        _run(c._send_history_or_unavailable("EUR/USD", "1m", [{"time": 1}], phase="initial"))
        msg = _sent(c.send_json, "history")
        self.assertEqual(msg["phase"], "initial")

    def test_history_message_defaults_to_complete(self):
        c = _bare_history_consumer()
        _run(c._send_history_or_unavailable("EUR/USD", "1m", [{"time": 1}]))
        msg = _sent(c.send_json, "history")
        self.assertEqual(msg["phase"], "complete")

    def test_history_unavailable_has_no_phase_field(self):
        c = _bare_history_consumer()
        _run(c._send_history_or_unavailable("EUR/USD", "1m", None, phase="initial"))
        msg = _sent(c.send_json, "history_unavailable")
        self.assertNotIn("phase", msg)


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — receive() call-sites (source-inspection): generation
# increment, first-page dispatch, conditional depth-task start
# ─────────────────────────────────────────────────────────────────────────
class ReceiveCallSiteWiringTests(SimpleTestCase):
    def _receive_source(self):
        return inspect.getsource(consumers_module.TradingConsumer.receive)

    def _block(self, src, start_marker, end_marker):
        i = src.index(start_marker)
        j = src.index(end_marker, i)
        return src[i:j]

    def test_change_symbol_uses_first_page_and_conditional_depth(self):
        src = self._receive_source()
        block = self._block(src, 'if act == "change_symbol":', 'elif act == "change_timeframe":')
        self.assertIn("self._history_generation += 1", block)
        self.assertIn("generate_history_first_page(", block)
        self.assertIn("_start_history_depth(", block)
        self.assertNotIn("await self.generate_history(", block)

    def test_change_timeframe_uses_first_page_and_conditional_depth(self):
        src = self._receive_source()
        block = self._block(src, 'elif act == "change_timeframe":', 'elif act == "load_history":')
        self.assertIn("self._history_generation += 1", block)
        self.assertIn("generate_history_first_page(", block)
        self.assertIn("_start_history_depth(", block)
        self.assertNotIn("await self.generate_history(", block)

    def test_load_history_uses_first_page_and_conditional_depth(self):
        src = self._receive_source()
        block = self._block(src, 'elif act == "load_history":', 'elif act == "account:get":')
        self.assertIn("self._history_generation += 1", block)
        self.assertIn("generate_history_first_page(", block)
        self.assertIn("_start_history_depth(", block)
        self.assertNotIn("await self.generate_history(", block)

    def test_generate_history_kept_for_backward_compatibility(self):
        # The original full-fetch method must still exist, unmodified in
        # behavior, even though receive() no longer calls it directly.
        src = inspect.getsource(consumers_module.TradingConsumer.generate_history)
        self.assertIn("async def generate_history(self, symbol, timeframe, bars=200)", src)


# ─────────────────────────────────────────────────────────────────────────
# consumers.py — connect()/disconnect() lifecycle of the depth task
# ─────────────────────────────────────────────────────────────────────────
class DepthTaskLifecycleSourceTests(SimpleTestCase):
    def test_disconnect_cancels_inflight_depth_task(self):
        src = inspect.getsource(consumers_module.TradingConsumer.disconnect)
        self.assertIn("_history_depth_task", src)
        self.assertIn(".cancel()", src)

    def test_connect_initializes_generation_and_task_slot(self):
        src = inspect.getsource(consumers_module.TradingConsumer.connect)
        self.assertIn("self._history_generation = 0", src)
        self.assertIn("self._history_depth_task", src)


# ─────────────────────────────────────────────────────────────────────────
# dashboard.html — phase-aware merge in the "history" message handler
# ─────────────────────────────────────────────────────────────────────────
class FrontendHistoryPhaseMergeTests(SimpleTestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _history_handler_block(self, src):
        start = src.index("if(msg.type==='history'&&Array.isArray(msg.data)){")
        end = src.index("if((msg.type==='candle_update'||msg.type==='candle_new')&&msg.data){", start)
        return src[start:end]

    def test_complete_phase_branch_present(self):
        block = self._history_handler_block(self._template_source())
        self.assertIn("(msg.phase||'complete')==='complete'", block)

    def test_complete_phase_checks_existing_bars_before_merging(self):
        block = self._history_handler_block(self._template_source())
        self.assertIn("this._bars.length){", block)

    def test_complete_phase_only_prepends_strictly_older_bars(self):
        block = self._history_handler_block(self._template_source())
        self.assertIn("this._bars[0].time", block)
        self.assertIn("b.time<earliest", block)
        self.assertIn("older.concat(this._bars)", block)

    def test_complete_phase_merge_precedes_full_replace_path(self):
        block = self._history_handler_block(self._template_source())
        self.assertLess(
            block.index("(msg.phase||'complete')==='complete'"),
            block.index("this._bars=bars;  // canonical store"),
        )

    def test_complete_phase_does_not_touch_price_or_agg_state(self):
        block = self._history_handler_block(self._template_source())
        merge_start = block.index("(msg.phase||'complete')==='complete'")
        merge_end = block.index("return;", merge_start)
        merge_body = block[merge_start:merge_end]
        for forbidden in ("this.setPrice(", "this._resetAgg(", "this._recomputeSpread(", "this._updateBidAsk("):
            self.assertNotIn(forbidden, merge_body)

    def test_complete_phase_does_not_call_fit_content(self):
        block = self._history_handler_block(self._template_source())
        merge_start = block.index("(msg.phase||'complete')==='complete'")
        merge_end = block.index("return;", merge_start)
        merge_body = block[merge_start:merge_end]
        self.assertNotIn("fitContent(", merge_body)

    def test_initial_phase_still_uses_full_replace_path(self):
        block = self._history_handler_block(self._template_source())
        self.assertIn("this._bars=bars;  // canonical store — full replace on every history load", block)
        self.assertIn("this.chart.timeScale().fitContent();", block)

    def test_symbol_and_timeframe_guards_unchanged(self):
        block = self._history_handler_block(self._template_source())
        self.assertIn("if(msg.symbol&&msg.symbol!==this.currentSymbol)return;", block)
        self.assertIn("if(msg.timeframe&&msg.timeframe!==this.currentTF)return;", block)
