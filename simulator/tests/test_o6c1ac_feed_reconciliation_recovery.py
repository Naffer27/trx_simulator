# simulator/tests/test_o6c1ac_feed_reconciliation_recovery.py
"""
O.6c-1ac — OPEN POSITION FEED RECONCILIATION RECOVERY FIX.

O.6c-1ab's live audit found the exact mechanism behind a real production
symptom (EUR/USD position showing a frozen chart price and a "—" row
P&L after a Daphne restart, while BTCUSD worked fine): the original
ensure_position_feed_reconciliation_started() set its idempotency flag
BEFORE an unguarded `await self._position_feed_reconcile_once()`. Any
exception there (a real DB error, or asyncio.CancelledError from an
interrupted connect()) skipped the `asyncio.create_task(_loop())` line
entirely — the flag stayed True forever, but no retry loop ever existed.
Reconciliation was then dead for the rest of the process's life,
recoverable only by a full restart.

This file tests the fix: the first pass is now wrapped in try/finally,
so the periodic loop task is ALWAYS created exactly once, whatever
happens during the first pass. It also tests a narrower, independent
race O.6c-1ab flagged (point 7 of the O.6c-1ac spec): a symbol whose
default WS subscribe happens before its Position-backed reference has
been marked (bootstrap timing / concurrent connect()) could have its
feed task killed by an immediate unsubscribe(), even though a real
Position exists — closed here by making unsubscribe() re-verify against
the DB (the sole authority, same query sync_position_symbol_from_db()
already used elsewhere) before releasing a symbol's LAST reference,
never introducing a manual counter.

Uses the same patterns already established in
test_o6c1v_open_position_feed_coverage.py: the real, process-global
get_feed_manager() singleton for DB/Redis-integration-style tests
(TransactionTestCase, since @database_sync_to_async methods and
transaction.on_commit() need a real commit), and fresh, disposable
FeedManager() instances for pure-unit lifecycle tests that don't touch
the DB at all (SimpleTestCase).
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, TransactionTestCase

from market_data.feeds import FeedManager, get_feed_manager
from simulator.tasks import _read_cached_price

from .factories import make_account, make_position


def _clear_symbol(symbol: str):
    """Same helper as test_o6c1v — resets every piece of process-global
    FeedManager state O.6c-1v/1ac touch for *symbol*, so tests never leak
    into each other via the real get_feed_manager() singleton."""
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


class _RealSingletonTestCase(TransactionTestCase):
    """Saves/clears/restores the real singleton's reconciliation state
    around every test — same isolation reasoning as O.6c-1v's
    ReconciliationDiscoversOpenPositionsTests, extended to also cover the
    new _position_reconcile_task/started fields this block adds."""

    SYMBOLS = ("EUR/USD", "BTCUSD")

    def setUp(self):
        feed = get_feed_manager()
        with feed._lock:
            self._saved_position_symbols = set(feed._position_symbols)
        self._saved_started = feed._position_reconcile_started
        self._saved_task = feed._position_reconcile_task
        feed.reset_position_tracking_for_tests()
        for sym in self.SYMBOLS:
            _clear_symbol(sym)

    def tearDown(self):
        for sym in self.SYMBOLS:
            _clear_symbol(sym)
        feed = get_feed_manager()
        with feed._lock:
            feed._position_symbols |= self._saved_position_symbols
        feed._position_reconcile_started = self._saved_started
        feed._position_reconcile_task = self._saved_task


# ─────────────────────────────────────────────────────────────────────────
# A / B / C — lifecycle fail-recoverability (pure unit, disposable FeedManager)
# ─────────────────────────────────────────────────────────────────────────
class LifecycleFailRecoverableTests(SimpleTestCase):

    def test_A_first_reconcile_exception_does_not_prevent_loop_creation(self):
        """A — first reconcile pass raises -> the periodic loop task must
        still be created (the exact defect O.6c-1ab found: the original
        code skipped asyncio.create_task() entirely on this path)."""
        fm = FeedManager()

        async def body():
            with patch.object(
                fm, "_position_feed_reconcile_once",
                new=AsyncMock(side_effect=RuntimeError("simulated DB failure")),
            ):
                await fm.ensure_position_feed_reconciliation_started()
            return fm.is_position_reconciliation_alive(), fm._position_reconcile_started

        alive, started = asyncio.run(body())
        self.assertTrue(started, "idempotency flag must be set")
        self.assertTrue(
            alive,
            "the periodic retry loop task must exist and be alive even "
            "though the first pass raised — this is the exact bug O.6c-1ab found",
        )

    def test_B_flag_never_true_without_a_live_task_success_path(self):
        """B — on the success path, the flag being True must always
        coincide with a genuinely alive task (never a zombie flag)."""
        fm = FeedManager()

        async def body():
            with patch.object(fm, "_position_feed_reconcile_once", new=AsyncMock()):
                await fm.ensure_position_feed_reconciliation_started()
            return fm.is_position_reconciliation_alive(), fm._position_reconcile_started

        alive, started = asyncio.run(body())
        self.assertEqual(started, alive, "flag and real task-aliveness must never diverge")

    def test_B_flag_never_true_without_a_live_task_failure_path(self):
        """B — same invariant on the failure path (this is the case that
        was broken before O.6c-1ac: started=True, task=None)."""
        fm = FeedManager()

        async def body():
            with patch.object(
                fm, "_position_feed_reconcile_once",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                await fm.ensure_position_feed_reconciliation_started()
            return fm.is_position_reconciliation_alive(), fm._position_reconcile_started

        alive, started = asyncio.run(body())
        self.assertEqual(started, alive, "flag and real task-aliveness must never diverge")
        self.assertTrue(alive)

    def test_C_multiple_ensure_calls_create_exactly_one_loop_even_after_failure(self):
        """C — idempotency must hold even when the FIRST call's pass
        raised: a second (and third) connect()-triggered call must not
        create a second loop task."""
        fm = FeedManager()
        create_task_calls = []
        real_create_task = asyncio.create_task

        def counting_create_task(coro, **kw):
            create_task_calls.append(1)
            return real_create_task(coro, **kw)

        async def body():
            with patch.object(
                fm, "_position_feed_reconcile_once",
                new=AsyncMock(side_effect=[RuntimeError("boom"), None, None]),
            ), patch("market_data.feeds.asyncio.create_task", side_effect=counting_create_task):
                await fm.ensure_position_feed_reconciliation_started()
                await fm.ensure_position_feed_reconciliation_started()
                await fm.ensure_position_feed_reconciliation_started()

        asyncio.run(body())
        self.assertEqual(
            len(create_task_calls), 1,
            "exactly one reconcile loop task must ever be created, no matter "
            "how many times ensure_position_feed_reconciliation_started() is "
            "called or whether the first pass failed",
        )


# ─────────────────────────────────────────────────────────────────────────
# A (DB half) / E / F / G / H — real singleton + real Position rows
# ─────────────────────────────────────────────────────────────────────────
class ReconciliationRecoveryIntegrationTests(_RealSingletonTestCase):

    def setUp(self):
        super().setUp()
        self.account = make_account(balance=Decimal("10000"))

    def test_A_failed_bootstrap_then_manual_reconcile_recovers_real_position(self):
        """A, full loop — ensure_position_feed_reconciliation_started()'s
        first pass fails against the REAL DB-backed method (patched to
        raise once), the loop survives (per the unit tests above), and a
        subsequent real (unpatched) _position_feed_reconcile_once() call
        recovers the real open Position's symbol — demonstrating actual
        recovery, not just "the task object exists"."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()
        self.assertFalse(fm.has_position_ref("EUR/USD"))

        async def body():
            with patch.object(
                fm, "_position_feed_reconcile_once",
                new=AsyncMock(side_effect=RuntimeError("simulated DB failure")),
            ):
                await fm.ensure_position_feed_reconciliation_started()
            self.assertTrue(fm.is_position_reconciliation_alive())
            self.assertFalse(fm.has_position_ref("EUR/USD"), "first pass failed, nothing marked yet")
            # The loop's next timer tick would call this same real method —
            # simulated directly here instead of sleeping for the real interval.
            await fm._position_feed_reconcile_once()

        asyncio.run(body())
        self.assertTrue(
            fm.has_position_ref("EUR/USD"),
            "the next reconcile pass after a failed first attempt must still "
            "recover a real open Position's symbol",
        )

    def test_E_bootstrap_recovers_preexisting_position_without_any_chart(self):
        """E — a Position that existed in DB before this FeedManager
        instance/process started (simulated: fresh _position_symbols,
        exactly like a real Daphne restart) is discovered by
        ensure_position_feed_reconciliation_started() alone — no
        subscribe()/chart ever called for it."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()
        self.assertFalse(fm.has_position_ref("EUR/USD"))
        self.assertNotIn("EUR/USD", fm._tasks)

        async def body():
            await fm.ensure_position_feed_reconciliation_started()
            # Checked inside the still-running loop — asyncio.run() cancels
            # any pending tasks the instant its own coroutine returns, so a
            # post-return check would always see a cancelled/"done" task
            # regardless of whether bootstrap actually started it correctly.
            self.assertIn("EUR/USD", fm._tasks, "bootstrap must have started the feed task")
            self.assertFalse(fm._tasks["EUR/USD"].done())

        asyncio.run(body())
        self.assertTrue(fm.has_position_ref("EUR/USD"))

    def test_F_temporary_failure_then_next_cycle_recovers_automatically(self):
        """F — a transient DB/reconcile failure (e.g. a momentary DB
        outage) must not prevent the NEXT cycle from correctly
        recovering, whether that failure happened on the bootstrap pass
        or on a later one. Modeled as: first pass fails (bootstrap),
        second real pass (standing in for the loop's next timer tick)
        succeeds and discovers a Position that was already open the
        whole time."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()

        call_count = {"n": 0}
        real = FeedManager._position_feed_reconcile_once

        async def flaky_once():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient DB outage")
            await real(fm)

        async def body():
            with patch.object(fm, "_position_feed_reconcile_once", new=flaky_once):
                await fm.ensure_position_feed_reconciliation_started()
                self.assertFalse(fm.has_position_ref("EUR/USD"))
                await fm._position_feed_reconcile_once()  # next cycle, real logic

        asyncio.run(body())
        self.assertTrue(fm.has_position_ref("EUR/USD"))
        self.assertEqual(call_count["n"], 2)

    def test_G_last_position_closed_and_zero_chart_subscribers_feed_stops(self):
        """G — no regression: once the (now fail-recoverable) bootstrap
        has marked a symbol, closing its only open Position with zero
        chart subscribers must still let a later reconcile pass stop it."""
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()
        asyncio.run(fm.ensure_position_feed_reconciliation_started())
        self.assertTrue(fm.has_position_ref("EUR/USD"))
        self.assertEqual(fm._counts.get("EUR/USD", 0), 0)

        pos.delete()

        with patch.object(fm, "_stop") as mock_stop:
            asyncio.run(fm._position_feed_reconcile_once())
        self.assertFalse(fm.has_position_ref("EUR/USD"))
        mock_stop.assert_called_once_with("EUR/USD")

    def test_H_two_positions_same_symbol_closing_one_keeps_feed_alive(self):
        """H — two open EUR/USD Positions; closing one must not release
        the symbol's keepalive while the other stays open."""
        pos1 = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()
        asyncio.run(fm.ensure_position_feed_reconciliation_started())
        self.assertTrue(fm.has_position_ref("EUR/USD"))

        pos1.delete()

        with patch.object(fm, "_stop") as mock_stop:
            asyncio.run(fm._position_feed_reconcile_once())
        self.assertTrue(fm.has_position_ref("EUR/USD"), "the second position is still open")
        mock_stop.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# D — the subscribe/unsubscribe race O.6c-1ab flagged (point 7 of the spec)
# ─────────────────────────────────────────────────────────────────────────
class UnsubscribeRaceGuardTests(_RealSingletonTestCase):

    def setUp(self):
        super().setUp()
        self.account = make_account(balance=Decimal("10000"))

    def test_D_chart_switch_away_from_position_backed_symbol_survives(self):
        """D — exact O.6c-1ab race: a real open EUR/USD Position exists,
        but _position_symbols has NOT yet been marked for it (simulating
        the reconciliation-hasn't-run-yet / concurrent-connect() window).
        connect()'s default subscribe("EUR/USD") followed immediately by
        change_symbol("BTCUSD")'s unsubscribe("EUR/USD") must NOT kill
        the feed — the new DB race-guard in unsubscribe() must catch it."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()
        self.assertFalse(fm.has_position_ref("EUR/USD"), "reconciliation deliberately not run yet")

        async def body():
            await fm.subscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chanA")
            with patch.object(fm, "_stop") as mock_stop:
                await fm.unsubscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chanA")
            return mock_stop

        mock_stop = asyncio.run(body())
        mock_stop.assert_not_called()
        self.assertTrue(
            fm.has_position_ref("EUR/USD"),
            "unsubscribe()'s DB race-guard must have marked it from the real Position row",
        )

    def test_D_no_position_no_race_guard_still_stops_normally(self):
        """No-regression companion to D — with no open Position at all,
        the DB race-guard must find nothing and the symbol must still
        stop exactly as before O.6c-1ac (byte-identical to the pre-
        O.6c-1v/1ac chart-only behaviour)."""
        fm = get_feed_manager()
        self.assertFalse(fm.has_position_ref("BTCUSD"))

        async def body():
            await fm.subscribe("BTCUSD", channel_layer=AsyncMock(), channel_name="chanA")
            with patch.object(fm, "_stop") as mock_stop:
                await fm.unsubscribe("BTCUSD", channel_layer=AsyncMock(), channel_name="chanA")
            return mock_stop

        mock_stop = asyncio.run(body())
        mock_stop.assert_called_once_with("BTCUSD")

    def test_D_race_guard_only_queries_db_when_last_ref_released(self):
        """The new DB read in unsubscribe() must fire ONLY on the path
        that would otherwise stop the symbol (count<=0 and not already
        known position-backed) — never on a routine unsubscribe that
        still leaves chart subscribers, and never when the cache already
        knows the symbol is position-backed (no redundant query)."""
        fm = get_feed_manager()
        fm.mark_position_symbol("EUR/USD")  # already known -> no DB call needed
        fm._counts["EUR/USD"] = 1  # one more subscriber than about to leave

        async def body():
            with patch.object(
                fm, "sync_position_symbol_from_db_async", new=AsyncMock(),
            ) as mock_sync:
                await fm.unsubscribe("EUR/USD", channel_layer=AsyncMock(), channel_name="chanA")
            return mock_sync

        mock_sync = asyncio.run(body())
        mock_sync.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# I / J / K / L — end-to-end: two symbols simultaneously, Redis, validated
# quote, and _positions_snapshot()'s pnl, all through the fixed lifecycle.
# ─────────────────────────────────────────────────────────────────────────
class EndToEndAfterRecoveryTests(_RealSingletonTestCase):

    def setUp(self):
        super().setUp()
        self.account = make_account(balance=Decimal("10000"))

    def test_I_chart_symbol_and_position_only_symbol_coexist(self):
        """I — BTCUSD has an active chart subscriber, EUR/USD is kept
        alive purely by its open Position — both feed tasks must exist
        and run simultaneously, neither starving the other."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()

        async def body():
            await fm.ensure_position_feed_reconciliation_started()
            await fm.subscribe("BTCUSD", channel_layer=AsyncMock(), channel_name="chanBTC")
            # Checked inside the still-running loop — see test_E's comment
            # on why this can't be checked after asyncio.run() returns.
            self.assertIn("BTCUSD", fm._tasks)
            self.assertFalse(fm._tasks["BTCUSD"].done())
            self.assertIn("EUR/USD", fm._tasks)
            self.assertFalse(fm._tasks["EUR/USD"].done())

        asyncio.run(body())

        self.assertEqual(fm._counts.get("BTCUSD", 0), 1)
        self.assertEqual(fm._counts.get("EUR/USD", 0), 0)
        self.assertTrue(fm.has_position_ref("EUR/USD"))

    def test_J_redis_receives_fresh_bid_ask_for_position_only_symbol(self):
        """J — a symbol kept alive exclusively by a Position (never
        charted) must still reach Redis with a fresh TTL — same
        _broadcast() call path every live/sim tick uses, exercised
        directly rather than waiting on the real network/sim timer."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()

        async def body():
            await fm.ensure_position_feed_reconciliation_started()
            self.assertEqual(fm._counts.get("EUR/USD", 0), 0)
            from market_data.feeds import _redis_write_pool
            await fm._broadcast("EUR/USD", AsyncMock(), bid=1.17010, ask=1.17030,
                                 ts=int(time.time()))
            _redis_write_pool.submit(lambda: None).result(timeout=5)

        asyncio.run(body())

        bid, ask = _read_cached_price("EUR/USD")
        self.assertIsNotNone(bid)
        self.assertAlmostEqual(bid, 1.17010, places=4)
        self.assertAlmostEqual(ask, 1.17030, places=4)

    def test_K_get_validated_quote_available_after_recovery(self):
        """K — once a tick has landed (post-recovery), get_validated_quote()
        — the sole financial-decision authority (O.6c-1w) — must return a
        real Quote for the symbol, not None."""
        make_position(self.account, symbol="EUR/USD", qty=Decimal("0.1"))
        fm = get_feed_manager()

        async def body():
            await fm.ensure_position_feed_reconciliation_started()
            await fm._broadcast("EUR/USD", AsyncMock(), bid=1.17010, ask=1.17030,
                                 ts=int(time.time()))

        asyncio.run(body())

        quote = fm.get_validated_quote("EUR/USD")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.symbol, "EUR/USD")
        self.assertAlmostEqual(quote.bid, 1.17010, places=4)
        self.assertAlmostEqual(quote.ask, 1.17030, places=4)

    def test_L_positions_snapshot_reports_numeric_pnl_after_recovery(self):
        """L — _positions_snapshot() (consumers.py) must degrade back to
        a numeric pnl for this position once a valid quote exists again —
        the exact field that showed "—" for the real O.6c-1ab symptom."""
        from simulator.consumers import TradingConsumer

        pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("1.17017"))
        fm = get_feed_manager()

        async def body():
            await fm.ensure_position_feed_reconciliation_started()
            await fm._broadcast("EUR/USD", AsyncMock(), bid=1.17100, ask=1.17120,
                                 ts=int(time.time()))

        asyncio.run(body())

        panel = TradingConsumer.__new__(TradingConsumer)
        panel._feed = fm
        panel.account = {"currency": "USD"}
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.17017, "sl": None, "tp": None,
        }]
        snapshot = panel._positions_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertIsNotNone(
            snapshot[0]["pnl"],
            "row pnl must be numeric again once a valid quote exists — "
            "this was the exact field the real EUR/USD position showed as null",
        )
        self.assertIsInstance(snapshot[0]["pnl"], float)
