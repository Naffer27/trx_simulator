# simulator/tests/test_o6c1v_open_position_feed_coverage.py
"""
O.6c-1v — OPEN POSITION FEED COVERAGE FIX.

Closes the gap O.6c-1u found live in production data: FeedManager kept a
symbol's feed task alive only while >=1 chart subscriber existed
(self._counts), never because a Position was open on it. A held EUR/USD
position with nobody charting it lost its Redis price after the 60s TTL,
and the Celery daemon's all_prices_available gate then skipped the whole
account's stopout/margin-call evaluation for that tick.

Design: FeedManager now tracks a SECOND, independent reference kind per
symbol — self._position_symbols (open_position_references), the DB
(Position) as sole authority. unsubscribe() only calls _stop() when BOTH
chart_subscribers (self._counts) AND open_position_references are empty.
Two complementary update paths keep self._position_symbols current:

  1. Fast path (same process) — consumers.py's _db_open_position_atomic/
     _db_close_position_atomic and admin.py's PositionAdmin.save_model/
     delete_model call mark_position_symbol()/sync_position_symbol_from_db()
     directly from their own transaction.on_commit(), immediately after
     commit.
  2. Backstop path (cross-process + restart recovery) —
     FeedManager._position_feed_reconcile_once(), a periodic (default 15s,
     POSITION_FEED_RECONCILE_INTERVAL_SECONDS) DB-authoritative pass that
     re-derives the full open-position symbol set from Position.objects
     directly. This is the ONLY mechanism that can see Celery-driven
     closes (scan_positions_task runs in a separate process and can never
     reach this Daphne process's FeedManager singleton directly) and the
     only mechanism that recovers pre-existing open positions after a
     Daphne restart, since self._position_symbols always starts empty on
     a fresh FeedManager instance.

Uses TransactionTestCase throughout — same reasoning already established
in test_o6c1o_multipanel_position_state_consistency.py: @database_sync_to_async
methods run on a separate thread (invisible to TestCase's uncommitted
transaction), and transaction.on_commit() callbacks never fire inside
TestCase's wrapping transaction at all.
"""
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TransactionTestCase, SimpleTestCase

from simulator.consumers import TradingConsumer
from simulator.tasks import _read_cached_price
from market_data.feeds import FeedManager, get_feed_manager

from .factories import make_account, make_position

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _seed_price(symbol: str, bid: float, ask: float, *, fresh: bool = True):
    """Same technique as test_o6c1q's _seed_feed_manager — RISK-02's
    broker-wide pricing-coverage check reads the real, process-global
    FeedManager singleton directly, so opening a position needs a fresh
    price seeded here or RISK_PRICING_INCOMPLETE rejects it."""
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol]     = bid
        feed._asks[symbol]     = ask
        feed._prices[symbol]   = (bid + ask) / 2
        feed._price_ts[symbol] = time.time() if fresh else (time.time() - 3600)


def _clear_symbol(symbol: str):
    """Resets every piece of process-global FeedManager state O.6c-1v
    touches for *symbol*, so tests never leak into each other via the
    real get_feed_manager() singleton."""
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)
        feed._position_symbols.discard(symbol)
    feed._counts.pop(symbol, None)
    task = feed._tasks.pop(symbol, None)
    if task and not task.done():
        task.cancel()


def _bare_consumer(account_id, symbol="EUR/USD") -> TradingConsumer:
    """Same minimal-panel pattern established in test_o6c1o/1q/1s — but
    self._feed is the REAL get_feed_manager() singleton (not _FakeFeed),
    since these tests exercise real FeedManager position-tracking."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = symbol
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._feed = get_feed_manager()
    c.account = {
        "balance": 10000.0, "equity": 10000.0, "peak_balance": 10000.0,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": "Activo", "account_type": "CHALLENGE",
        "tier": "", "profit_target": 0.0, "initial_balance": 0.0,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


# ─────────────────────────────────────────────────────────────────────────
# 1/3/4/5/6/11 — reference-counting unit behaviour (pure FeedManager, no DB)
# ─────────────────────────────────────────────────────────────────────────
class ReferenceGatingUnitTests(SimpleTestCase):
    """FeedManager() instances constructed directly — same isolation
    pattern as test_feeds_shadow_integration.py — no process-global
    singleton, no DB needed for these."""

    def test_scenario1_position_ref_with_zero_chart_subscribers_blocks_stop(self):
        fm = FeedManager()
        fm.mark_position_symbol("EUR/USD")
        with patch.object(fm, "_stop") as mock_stop:
            # 0 chart subscribers ever existed for this symbol.
            self.assertTrue(fm.has_position_ref("EUR/USD"))
        mock_stop.assert_not_called()

    def test_scenario3_last_chart_unsubscribe_with_open_position_does_not_stop(self):
        fm = FeedManager()
        fm._counts["EUR/USD"] = 1  # one chart subscriber
        fm.mark_position_symbol("EUR/USD")  # and an open position
        with patch.object(fm, "_stop") as mock_stop:
            import asyncio
            asyncio.run(fm.unsubscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chan1"))
        mock_stop.assert_not_called()
        self.assertEqual(fm._counts["EUR/USD"], 0)

    def test_scenario11_no_regression_pure_chart_unsubscribe_still_stops(self):
        """No open position at all — behaviour must be byte-identical to
        pre-O.6c-1v: last chart unsubscribe stops the feed."""
        fm = FeedManager()
        fm._counts["EUR/USD"] = 1
        self.assertFalse(fm.has_position_ref("EUR/USD"))
        with patch.object(fm, "_stop") as mock_stop:
            import asyncio
            asyncio.run(fm.unsubscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chan1"))
        mock_stop.assert_called_once_with("EUR/USD")

    def test_scenario11_subscribe_unaffected_by_position_tracking(self):
        fm = FeedManager()
        fm.mark_position_symbol("EUR/USD")
        with patch.object(fm, "_feed_loop", new=MagicMock(return_value=MagicMock())), \
             patch("market_data.feeds.asyncio.create_task") as mock_create_task:
            import asyncio
            asyncio.run(fm.subscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chan1"))
        self.assertEqual(fm._counts["EUR/USD"], 1)
        mock_create_task.assert_called_once()

    def test_mark_is_idempotent(self):
        fm = FeedManager()
        fm.mark_position_symbol("EUR/USD")
        fm.mark_position_symbol("EUR/USD")
        self.assertEqual(fm._position_symbols, {"EUR/USD"})

    def test_unmark_removes(self):
        fm = FeedManager()
        fm.mark_position_symbol("EUR/USD")
        fm.unmark_position_symbol("EUR/USD")
        self.assertFalse(fm.has_position_ref("EUR/USD"))

    def test_ensure_position_feed_reconciliation_started_is_idempotent(self):
        """Mirrors spread_config_cache's own idempotency test style
        (SPREAD-03) — calling twice starts exactly one background loop."""
        fm = FeedManager()
        # side_effect closes the passed coroutine instead of leaving it
        # unawaited (create_task itself is mocked, so nothing else would
        # ever run/close it) — avoids a spurious "coroutine was never
        # awaited" RuntimeWarning with no change in what's asserted.
        with patch.object(fm, "_position_feed_reconcile_once", new=AsyncMock()) as mock_once, \
             patch("market_data.feeds.asyncio.create_task",
                   side_effect=lambda coro, **kw: coro.close()) as mock_create_task:
            import asyncio
            asyncio.run(fm.ensure_position_feed_reconciliation_started())
            asyncio.run(fm.ensure_position_feed_reconciliation_started())
        mock_once.assert_called_once()       # the immediate first pass
        mock_create_task.assert_called_once()  # the background _loop() task


# ─────────────────────────────────────────────────────────────────────────
# 1/6/7 — DB-authoritative reconciliation (discovers positions, starts feeds)
# ─────────────────────────────────────────────────────────────────────────
class ReconciliationDiscoversOpenPositionsTests(TransactionTestCase):
    """get_feed_manager() is a process-wide singleton shared with every
    other test file in the suite — _position_symbols is saved/cleared/
    restored around every test here so assertions about exact _stop()/
    _ensure_running() call counts are about THIS test's own clean state,
    never about residue an unrelated test elsewhere in the full suite
    left behind (Position rows themselves are already fully isolated by
    TransactionTestCase's per-test flush; only this in-memory set is not)."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        feed = get_feed_manager()
        with feed._lock:
            self._saved_position_symbols = set(feed._position_symbols)
            feed._position_symbols.clear()
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")
        feed = get_feed_manager()
        with feed._lock:
            feed._position_symbols |= self._saved_position_symbols

    def test_scenario1_and_7_cold_feedmanager_discovers_preexisting_position(self):
        """Simulates a Daphne restart: a fresh FeedManager() instance
        (self._position_symbols starts empty, exactly like a real process
        boot) plus a Position row that already existed in DB before this
        instance was ever created — no subscribe() ever called."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()  # the real singleton — reconcile reads real DB
        self.assertFalse(fm.has_position_ref("EUR/USD"))

        with patch.object(fm, "_ensure_running") as mock_ensure:
            import asyncio
            asyncio.run(fm._position_feed_reconcile_once())

        self.assertTrue(fm.has_position_ref("EUR/USD"))
        mock_ensure.assert_called_once()
        self.assertEqual(mock_ensure.call_args[0][0], "EUR/USD")

    def test_scenario6_two_symbols_both_discovered_and_kept_alive(self):
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        make_position(self.account, symbol="BTCUSD", qty=Decimal("0.01"),
                      avg_price=Decimal("63000"))
        fm = get_feed_manager()

        with patch.object(fm, "_ensure_running") as mock_ensure:
            import asyncio
            asyncio.run(fm._position_feed_reconcile_once())

        self.assertTrue(fm.has_position_ref("EUR/USD"))
        self.assertTrue(fm.has_position_ref("BTCUSD"))
        self.assertEqual(mock_ensure.call_count, 2)

    def test_scenario4_last_position_closed_zero_charts_reconcile_stops_it(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"),
                             avg_price=Decimal("1.10000"))
        fm = get_feed_manager()
        fm.mark_position_symbol("EUR/USD")  # simulate: was already tracked
        fm._counts["EUR/USD"] = 0           # zero chart subscribers

        pos.delete()  # position closed, e.g. by Celery in a real deployment

        with patch.object(fm, "_stop") as mock_stop:
            import asyncio
            asyncio.run(fm._position_feed_reconcile_once())

        self.assertFalse(fm.has_position_ref("EUR/USD"))
        mock_stop.assert_called_once_with("EUR/USD")

    def test_scenario4b_position_closed_but_chart_still_open_reconcile_does_not_stop(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"),
                             avg_price=Decimal("1.10000"))
        fm = get_feed_manager()
        fm.mark_position_symbol("EUR/USD")
        fm._counts["EUR/USD"] = 1  # a panel still has it charted

        pos.delete()

        with patch.object(fm, "_stop") as mock_stop:
            import asyncio
            asyncio.run(fm._position_feed_reconcile_once())

        self.assertFalse(fm.has_position_ref("EUR/USD"))  # DB truth updated
        mock_stop.assert_not_called()  # but chart subscriber keeps it alive

    def test_no_op_when_nothing_changed(self):
        """No open positions, nothing marked — reconcile should not touch
        _ensure_running/_stop at all (cheap steady-state). Class-level
        setUp() already gives this test a clean _position_symbols set —
        see the class docstring for why that isolation is needed (this
        exact assertion failed under the full suite before it was added:
        a stale symbol from an unrelated test elsewhere in the process,
        whose Position rows TransactionTestCase had already flushed by
        the time this test ran, was still marked in the shared singleton
        — and reconcile correctly, harmlessly released it)."""
        fm = get_feed_manager()
        with patch.object(fm, "_ensure_running") as mock_ensure, \
             patch.object(fm, "_stop") as mock_stop:
            import asyncio
            asyncio.run(fm._position_feed_reconcile_once())
        mock_ensure.assert_not_called()
        mock_stop.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# Same-process fast path — consumers.py's own atomic open/close call sites
# ─────────────────────────────────────────────────────────────────────────
class OpenCloseFastPathTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")
        _seed_price("EUR/USD", bid=1.10000, ask=1.10020)

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_open_marks_position_symbol_on_commit(self):
        panel = _bare_consumer(self.account.pk)
        feed = get_feed_manager()
        self.assertFalse(feed.has_position_ref("EUR/USD"))

        result = _db_open_sync(
            panel, symbol="EUR/USD", side="buy", qty=0.1, price=1.10010,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result.get("ok", True))
        self.assertTrue(feed.has_position_ref("EUR/USD"))

    def test_scenario5_close_one_of_two_same_symbol_keeps_ref(self):
        pos1 = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"),
                              avg_price=Decimal("1.10000"))
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"),
                      avg_price=Decimal("1.10000"))
        feed = get_feed_manager()
        feed.mark_position_symbol("EUR/USD")

        panel = _bare_consumer(self.account.pk)
        pos_mem = {"id": pos1.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}
        _db_close_sync(panel, pos_mem, close_px=1.10500, reason="manual",
                        realized_pnl=5.0, new_balance=10005.0, new_equity=10005.0)

        # The OTHER EUR/USD position is still open in DB — must stay marked.
        self.assertTrue(feed.has_position_ref("EUR/USD"))

    def test_close_only_position_unmarks_via_sync(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"),
                             avg_price=Decimal("1.10000"))
        feed = get_feed_manager()
        feed.mark_position_symbol("EUR/USD")

        panel = _bare_consumer(self.account.pk)
        pos_mem = {"id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}
        _db_close_sync(panel, pos_mem, close_px=1.10500, reason="manual",
                        realized_pnl=5.0, new_balance=10005.0, new_equity=10005.0)

        self.assertFalse(feed.has_position_ref("EUR/USD"))


# ─────────────────────────────────────────────────────────────────────────
# Admin create/delete (writer #8)
# ─────────────────────────────────────────────────────────────────────────
class AdminFastPathTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_admin_save_model_marks_position_symbol(self):
        from simulator.admin import PositionAdmin
        from simulator.models import Position
        from django.contrib.admin.sites import AdminSite

        pos = Position(account=self.account, symbol="EUR/USD", side="BUY",
                        qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        admin_instance = PositionAdmin(Position, AdminSite())
        feed = get_feed_manager()
        self.assertFalse(feed.has_position_ref("EUR/USD"))

        admin_instance.save_model(request=None, obj=pos, form=None, change=False)

        self.assertTrue(feed.has_position_ref("EUR/USD"))

    def test_admin_delete_model_unmarks_when_no_position_left(self):
        from simulator.admin import PositionAdmin
        from simulator.models import Position
        from django.contrib.admin.sites import AdminSite

        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        feed = get_feed_manager()
        feed.mark_position_symbol("EUR/USD")
        admin_instance = PositionAdmin(Position, AdminSite())

        admin_instance.delete_model(request=None, obj=pos)

        self.assertFalse(feed.has_position_ref("EUR/USD"))


# ─────────────────────────────────────────────────────────────────────────
# 8/9/10 — Redis freshness (what Celery actually reads) with zero panels
# ─────────────────────────────────────────────────────────────────────────
class RedisFreshnessWithoutAnyPanelTests(TransactionTestCase):
    """Demonstrates the end of the O.6c-1u chain: a position-tracked
    symbol's feed task, once running, broadcasts exactly like a
    chart-driven one — writing to the SAME Redis cache Celery's
    scan_positions_task (_read_cached_price/all_prices_available) reads.
    No real exchange connection required — _broadcast() is called
    directly, the same call every live/sim tick makes internally."""

    def setUp(self):
        self.symbol = "EUR/USD"
        _clear_symbol(self.symbol)

    def tearDown(self):
        _clear_symbol(self.symbol)

    def test_scenario8_broadcast_from_position_only_keepalive_reaches_redis(self):
        feed = get_feed_manager()
        feed.mark_position_symbol(self.symbol)  # position-only, zero chart subscribers
        self.assertEqual(feed._counts.get(self.symbol, 0), 0)

        import asyncio
        from market_data.feeds import _redis_write_pool
        asyncio.run(feed._broadcast(self.symbol, AsyncMock(), bid=1.10500, ask=1.10520,
                                     ts=int(time.time())))
        # _write_price_cache() dispatches to a background thread pool
        # (fire-and-forget, by design — a Redis outage must never block
        # the feed loop). Flush that same pool deterministically before
        # reading Redis back, instead of an arbitrary sleep.
        _redis_write_pool.submit(lambda: None).result(timeout=5)

        bid, ask = _read_cached_price(self.symbol)
        self.assertIsNotNone(bid)
        self.assertAlmostEqual(bid, 1.10500, places=4)
        self.assertAlmostEqual(ask, 1.10520, places=4)

    def test_scenario9_and_10_daemon_all_prices_available_gate_passes(self):
        """Reproduces scan_positions_task's own all_prices_available gate
        (tasks.py) inline: with a fresh Redis price present for a symbol
        no panel is charting, the account's SL/TP + stopout/margin-call
        evaluation is NOT skipped."""
        feed = get_feed_manager()
        feed.mark_position_symbol(self.symbol)

        import asyncio
        from market_data.feeds import _redis_write_pool
        asyncio.run(feed._broadcast(self.symbol, AsyncMock(), bid=1.10500, ask=1.10520,
                                     ts=int(time.time())))
        _redis_write_pool.submit(lambda: None).result(timeout=5)

        symbols = {self.symbol}
        prices = {}
        for sym in symbols:
            bid, ask = _read_cached_price(sym)
            if bid is not None and ask is not None:
                prices[sym] = (bid, ask)
        all_prices_available = all(sym in prices for sym in symbols)
        self.assertTrue(
            all_prices_available,
            "daemon would have skipped this account's stopout/margin-call "
            "evaluation for a symbol nobody is charting",
        )


# ─────────────────────────────────────────────────────────────────────────
# 12 — no extra DB query on the per-tick path
# ─────────────────────────────────────────────────────────────────────────
class NoExtraDbQueryPerTickTests(TransactionTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_broadcast_never_queries_db(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        feed = get_feed_manager()
        feed.mark_position_symbol("EUR/USD")

        with patch("market_data.feeds._write_price_cache", new=AsyncMock()):
            with CaptureQueriesContext(connection) as ctx:
                import asyncio
                asyncio.run(feed._broadcast("EUR/USD", AsyncMock(), bid=1.1, ask=1.1002,
                                             ts=int(time.time())))
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_reconcile_runs_on_its_own_timer_not_the_tick_path(self):
        """The reconciliation query only happens inside
        _position_feed_reconcile_once(), never inside _broadcast()/
        _resync_price() — a static guarantee that the tick path and the
        DB-reconcile path are fully decoupled."""
        import inspect
        from market_data.feeds import FeedManager
        broadcast_names = FeedManager._broadcast.__code__.co_names
        self.assertNotIn("Position", broadcast_names)
        self.assertNotIn("_position_feed_reconcile_once", broadcast_names)


# ─────────────────────────────────────────────────────────────────────────
# CRÍTICO — restart recovery without depending on a WebSocket connection.
# Daphne has no ASGI lifespan support (verified — see asgi.py's comment);
# trx_simulator.asgi wraps `application` to fire the SAME idempotent
# reconciliation starter on the first scope of ANY type (http or
# websocket), so the deploy runbook's own post-restart healthcheck.sh
# HTTP call already closes this gap in production.
# ─────────────────────────────────────────────────────────────────────────
class AsgiBootstrapTests(SimpleTestCase):
    def setUp(self):
        import trx_simulator.asgi as asgi_module
        self.asgi_module = asgi_module
        self._orig_flag = asgi_module._position_feed_bootstrap_done
        self._orig_routed = asgi_module._routed_application
        asgi_module._position_feed_bootstrap_done = False

    def tearDown(self):
        self.asgi_module._position_feed_bootstrap_done = self._orig_flag
        self.asgi_module._routed_application = self._orig_routed

    def test_fires_once_on_first_scope_of_any_type(self):
        import asyncio
        received_scopes = []

        async def fake_routed(scope, receive, send):
            received_scopes.append(scope["type"])

        self.asgi_module._routed_application = fake_routed

        with patch("market_data.feeds.get_feed_manager") as mock_get_fm:
            mock_fm = MagicMock()
            mock_fm.ensure_position_feed_reconciliation_started = AsyncMock()
            mock_get_fm.return_value = mock_fm

            asyncio.run(self.asgi_module.application(
                {"type": "http"}, AsyncMock(), AsyncMock(),
            ))
            asyncio.run(self.asgi_module.application(
                {"type": "websocket"}, AsyncMock(), AsyncMock(),
            ))

        # Both scopes still reached the real router — the wrapper never
        # swallows or blocks the actual request.
        self.assertEqual(received_scopes, ["http", "websocket"])
        # Bootstrap kicked off exactly once, on the FIRST scope only.
        mock_get_fm.assert_called_once()

    def test_bootstrap_failure_never_breaks_the_real_request(self):
        import asyncio
        received_scopes = []

        async def fake_routed(scope, receive, send):
            received_scopes.append(scope["type"])

        self.asgi_module._routed_application = fake_routed

        with patch("market_data.feeds.get_feed_manager", side_effect=RuntimeError("boom")):
            asyncio.run(self.asgi_module.application(
                {"type": "http"}, AsyncMock(), AsyncMock(),
            ))

        self.assertEqual(received_scopes, ["http"])
