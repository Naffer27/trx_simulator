# simulator/tests/test_fix05a1_source_propagation.py
"""
FIX-05A.1 — Price Source Propagation to Live WS SL/TP and the Celery Daemon.

Closes the two residual gaps FIX-05A left open (see that block's final
report and this block's design lock):

  1. FeedManager._broadcast()'s "price.tick" event now carries "source"
     (feeds.py) — the same value already known at every real call site
     (source="sim"/"binance"/"kraken"/"finnhub"). consumers.py::
     price_tick() gates the call to _check_tp_sl() on it: FAIL-CLOSED —
     a missing source key OR source=="sim" skips the call entirely; only
     an explicit, known-real value allows SL/TP to evaluate. No re-query
     of self._feed (the approach that broke ~20 pre-existing tests in
     the earlier FIX-05A attempt) — the event already carries what's
     needed.

  2. The Redis price cache (feeds.py::_write_price_cache_sync) now
     writes a third trx:price:source:{symbol} key, same TTL, same
     pipeline as bid/ask. tasks.py::_read_cached_price() fails closed —
     missing key or source=="sim" returns (None, None), the exact same
     "price unavailable" path scan_positions_task already had for
     stale/missing prices, so neither its stopout nor its SL/TP loop
     needed any change.

See test_fix05a_financial_price_integrity.py for FIX-05A's own coverage
(open/close/PnL/stopout gates, market-session policy, single
PricingDecision) — unaffected by this block, still green.
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from market_data.feeds import FeedManager, get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import Position, Trade
from simulator.tasks import _read_cached_price, scan_positions_task

from .factories import make_account, make_position
from .test_o6c1w_price_integrity_gate import _bare_consumer, _clear_symbol, _seed_raw
from .test_order_ticket_sl_tp_validation import _run


def _redis_client():
    import redis as _redis
    url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
    return _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)


def _clear_redis_keys(symbol: str):
    r = _redis_client()
    r.delete(f"trx:price:bid:{symbol}", f"trx:price:ask:{symbol}", f"trx:price:source:{symbol}")


def _tick_event(symbol, bid, ask, ts=1_700_000_000, source=None):
    d = {"symbol": symbol, "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 5), "time": ts}
    if source is not None:
        d["source"] = source
    return d


def _tick_consumer(symbol="EUR/USD"):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = symbol
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {"balance": 10000.0, "spread_pips": 0.0, "commercial_pricing_fields": {}}
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


# ── A/B — broadcast payload carries source ───────────────────────────────────

class BroadcastSourceFieldTests(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        from unittest.mock import patch
        patcher = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_provider_source_included_in_event(self):
        asyncio.run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0, source="finnhub"))
        event = self.channel_layer.group_send.call_args.args[1]
        self.assertEqual(event["source"], "finnhub")
        self.assertEqual(event["type"], "price.tick")
        # aditivo — el resto del payload permanece igual
        self.assertIn("bid", event); self.assertIn("ask", event)
        self.assertIn("mid", event); self.assertIn("time", event)

    def test_sim_source_included_in_event(self):
        asyncio.run(self.fm._broadcast("EUR/USD", self.channel_layer, 1.1, 1.1010, 0, source="sim"))
        event = self.channel_layer.group_send.call_args.args[1]
        self.assertEqual(event["source"], "sim")


# ── C/D/E/F — WS live SL/TP gate ─────────────────────────────────────────────

class WsLiveSlTpSourceGateTests(SimpleTestCase):
    def test_sim_source_never_calls_check_tp_sl(self):
        c = _tick_consumer()
        _run(c.price_tick(_tick_event("EUR/USD", 1.09990, 1.10010, source="sim")))
        c._check_tp_sl.assert_not_called()

    def test_missing_source_never_calls_check_tp_sl_fail_closed(self):
        c = _tick_consumer()
        _run(c.price_tick(_tick_event("EUR/USD", 1.09990, 1.10010, source=None)))
        c._check_tp_sl.assert_not_called()

    def test_real_source_calls_check_tp_sl_with_raw_values(self):
        c = _tick_consumer()
        _run(c.price_tick(_tick_event("EUR/USD", 1.09990, 1.10010, source="finnhub")))
        c._check_tp_sl.assert_called_once()
        args = c._check_tp_sl.call_args.args
        self.assertEqual(args[0], "EUR/USD")
        self.assertAlmostEqual(args[1], 1.09990, places=5)
        self.assertAlmostEqual(args[2], 1.10010, places=5)


class WsLiveSlTpEndToEndTests(TransactionTestCase):
    """Real _check_tp_sl (not mocked) — proves a position that WOULD
    close on a real tick does NOT close on a sim/missing-source tick
    crossing the exact same SL/TP thresholds, and DOES close once a
    real-source tick crosses them."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def _consumer_with_position(self, sl=None, tp=None):
        pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("1.10000"),
                             sl=Decimal(str(sl)) if sl else None,
                             tp=Decimal(str(tp)) if tp else None)
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.10000, "sl": sl, "tp": tp, "opened_at": time.time(),
        }]
        panel._on_tick = AsyncMock()
        panel._recalc_account_and_push = AsyncMock()
        return panel, pos

    def test_sl_crossed_by_sim_tick_does_not_close(self):
        panel, pos = self._consumer_with_position(sl=1.09000)
        # bid=1.08900 <= sl=1.09000 would trigger SL if evaluated
        _run(panel.price_tick(_tick_event("EUR/USD", 1.08900, 1.08920, source="sim")))
        self.assertEqual(len(panel._positions), 1)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_tp_crossed_by_sim_tick_does_not_close(self):
        panel, pos = self._consumer_with_position(tp=1.11000)
        _run(panel.price_tick(_tick_event("EUR/USD", 1.11100, 1.11120, source="sim")))
        self.assertEqual(len(panel._positions), 1)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_tp_crossed_by_missing_source_tick_does_not_close(self):
        panel, pos = self._consumer_with_position(tp=1.11000)
        _run(panel.price_tick(_tick_event("EUR/USD", 1.11100, 1.11120, source=None)))
        self.assertEqual(len(panel._positions), 1)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_tp_crossed_by_real_tick_still_closes(self):
        panel, pos = self._consumer_with_position(tp=1.11000)
        _run(panel.price_tick(_tick_event("EUR/USD", 1.11100, 1.11120, source="finnhub")))
        self.assertEqual(len(panel._positions), 0)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())


# ── G/H/I/J — Redis source cache ─────────────────────────────────────────────

class RedisSourceCacheTests(TestCase):
    def tearDown(self):
        _clear_redis_keys("EUR/USD")

    def test_real_source_persisted_and_read(self):
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.10000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        r.setex("trx:price:source:EUR/USD", 60, "finnhub")
        bid, ask = _read_cached_price("EUR/USD")
        self.assertEqual(bid, 1.10000)
        self.assertEqual(ask, 1.10020)

    def test_sim_source_returns_none_none(self):
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.10000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        r.setex("trx:price:source:EUR/USD", 60, "sim")
        bid, ask = _read_cached_price("EUR/USD")
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_legacy_entry_without_source_fails_closed(self):
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.10000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        # no source key written — simulates a pre-FIX-05A.1 cache entry
        bid, ask = _read_cached_price("EUR/USD")
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_source_key_shares_ttl_with_bid_ask(self):
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.10000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        r.setex("trx:price:source:EUR/USD", 60, "finnhub")
        self.assertGreater(r.ttl("trx:price:source:EUR/USD"), 0)
        self.assertLessEqual(r.ttl("trx:price:source:EUR/USD"), 60)

    def test_provider_recovery_legacy_then_real_tick_restores_daemon(self):
        r = _redis_client()
        # legacy/sim state — daemon untrusted
        r.setex("trx:price:bid:EUR/USD", 60, "1.10000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        r.setex("trx:price:source:EUR/USD", 60, "sim")
        self.assertEqual(_read_cached_price("EUR/USD"), (None, None))

        # next real tick overwrites all three keys together (same
        # pipeline as feeds.py::_write_price_cache_sync)
        r.setex("trx:price:bid:EUR/USD", 60, "1.10005")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10025")
        r.setex("trx:price:source:EUR/USD", 60, "finnhub")
        bid, ask = _read_cached_price("EUR/USD")
        self.assertEqual(bid, 1.10005)
        self.assertEqual(ask, 1.10025)

    def test_write_price_cache_sync_writes_all_three_keys_together(self):
        from market_data.feeds import _write_price_cache_sync
        _write_price_cache_sync("EUR/USD", 1.10000, 1.10020, "finnhub")
        r = _redis_client()
        self.assertEqual(r.get("trx:price:bid:EUR/USD").decode(), "1.1")
        self.assertEqual(r.get("trx:price:ask:EUR/USD").decode(), "1.1002")
        self.assertEqual(r.get("trx:price:source:EUR/USD").decode(), "finnhub")


# ── K/L — daemon SL/TP + stopout with sim/missing Redis source ──────────────

def _scan():
    return scan_positions_task.apply().get()


class DaemonSourceGateTests(TestCase):
    """Real Redis (not mocked _read_cached_price) — exercises the full
    fail-closed path end-to-end, mirroring test_daemon_scan.py's own
    BUY sl=1.09000/tp=1.11000 @ avg=1.10000 scenario values."""

    def tearDown(self):
        _clear_redis_keys("EUR/USD")

    def test_sl_crossed_sim_source_daemon_does_not_close(self):
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                             sl=Decimal("1.09000"))
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.08900")  # crosses sl if trusted
        r.setex("trx:price:ask:EUR/USD", 60, "1.08920")
        r.setex("trx:price:source:EUR/USD", 60, "sim")

        result = _scan()

        self.assertEqual(result["closed"], 0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_sl_crossed_missing_source_daemon_does_not_close(self):
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                             sl=Decimal("1.09000"))
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.08900")
        r.setex("trx:price:ask:EUR/USD", 60, "1.08920")
        # no source key at all

        result = _scan()

        self.assertEqual(result["closed"], 0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

    def test_sl_crossed_real_source_daemon_closes_as_before(self):
        account = make_account(account_type="CHALLENGE", tier="10K")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                             sl=Decimal("1.09000"))
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.08900")
        r.setex("trx:price:ask:EUR/USD", 60, "1.08920")
        r.setex("trx:price:source:EUR/USD", 60, "finnhub")

        result = _scan()

        self.assertEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        trade = Trade.objects.get(account=account)
        self.assertLess(float(trade.profit_loss), 0)

    def test_stopout_sim_source_daemon_does_not_liquidate(self):
        """CHALLENGE stopout: peak - balance blows the drawdown budget —
        with the position's own SL/TP silent, the stopout pre-check
        alone must not liquidate on a sim-sourced price either."""
        account = make_account(account_type="CHALLENGE", tier="10K",
                                balance=Decimal("8000"), peak_balance=Decimal("10000"))
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"))
        r = _redis_client()
        r.setex("trx:price:bid:EUR/USD", 60, "1.09000")
        r.setex("trx:price:ask:EUR/USD", 60, "1.09020")
        r.setex("trx:price:source:EUR/USD", 60, "sim")

        result = _scan()

        self.assertEqual(result["closed"], 0)
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.status, "Activo")  # never suspended
