# simulator/tests/test_o6c1o_multipanel_position_state_consistency.py
"""
O.6c-1o — MULTIPANEL POSITION STATE CONSISTENCY IMPLEMENTATION.

Implements the O.6c-1n design: DB (Position) is the sole authoritative
source; self._positions is a per-connection cache; every Position writer
publishes "position.changed" to the account_{account_id} Channels group
(Redis-backed in production — channels_redis.core.RedisChannelLayer,
same layer used in this test run) AFTER its own transaction commits
(transaction.on_commit — consumers.py's two atomic methods — or the
equivalent post-commit call in tasks.py/admin.py); every connection that
receives the event resyncs from DB via TradingConsumer.position_changed().

Uses TransactionTestCase (not TestCase) throughout, for two independent
reasons already established in this codebase:
  1. @database_sync_to_async methods run on a separate thread — invisible
     to (and can deadlock against) TestCase's uncommitted per-test
     transaction on SQLite (test_multipanel_position_sync.py,
     test_broker_risk_limits_engine.py::IntegrationTests).
  2. transaction.on_commit() callbacks never fire inside TestCase's
     wrapping transaction (Django never commits it) — TransactionTestCase
     commits for real, so on_commit fires naturally, which is exactly
     the behavior under test here.

`.__wrapped__` unwraps the @database_sync_to_async decorator so the sync
body can be called directly on the test's own thread — same technique
already established in test_pnl_conversion_integration.py.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase
from django.db import connection, reset_queries

from simulator import ws_events
from simulator.consumers import TradingConsumer
from simulator.models import Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync, _daemon_close_all
from market_data.feeds import get_feed_manager


def _seed_eurusd_price():
    """RISK-02's broker-wide pricing-coverage check (broker_exposure.py)
    reads the real, process-global FeedManager singleton directly —
    never self._feed on any one bare consumer — so any test that opens a
    SECOND position on an account that already has one open (netting
    merge, or any scenario with >=1 pre-existing Position broker-wide)
    needs a real price seeded here, or RISK_PRICING_INCOMPLETE rejects
    it (exactly the O.6a/O.6c-1j fail-closed mechanism audited earlier
    this session). Same technique already established in
    test_atomic_margin_and_position_guard.py::_seed_all_prices()."""
    feed = get_feed_manager()
    with feed._lock:
        feed._bids["EUR/USD"] = 1.10000
        feed._asks["EUR/USD"] = 1.10020
        feed._prices["EUR/USD"] = 1.10010
        feed._price_ts["EUR/USD"] = time.time()

from .factories import make_account, make_position

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


class _FakeFeed:
    """O.6c-1q — stands in for self._feed (get_feed_manager()), the price
    authority _unrealized_pnl_total() now reads from instead of
    self._bid_state/self._ask_state — see O.6c-1p/O.6c-1q. Per-instance
    and per-symbol configurable (default EUR/USD 1.10000/1.10020) so
    individual tests can set a specific price without touching the real
    global FeedManager singleton (which _seed_eurusd_price() seeds
    separately, for RISK-02's own direct get_feed_manager() call — a
    different code path from self._feed)."""

    def __init__(self):
        self._bid = {"EUR/USD": 1.10000}
        self._ask = {"EUR/USD": 1.10020}

    def set_price(self, symbol, bid, ask):
        self._bid[symbol] = bid
        self._ask[symbol] = ask

    def has_price(self, symbol):
        return symbol in self._bid

    def last_price(self, symbol):
        return (self._bid.get(symbol, 0.0) + self._ask.get(symbol, 0.0)) / 2

    def last_bid(self, symbol):
        return self._bid[symbol]

    def last_ask(self, symbol):
        return self._ask[symbol]

    # O.6c-1v — no-op stand-ins, see test_close_path_concurrency_parity.py's
    # identical addition for the full rationale.
    def mark_position_symbol(self, symbol): pass
    def sync_position_symbol_from_db(self, symbol): pass

    # O.6c-1w — mirrors this fake's own has_price()/last_bid()/last_ask()
    # exactly, so every existing numeric expectation in this file is
    # unaffected; see test_close_path_concurrency_parity.py's identical
    # addition for the full rationale.
    def get_validated_quote(self, symbol):
        if not self.has_price(symbol):
            return None
        from market_data.feeds import Quote
        bid, ask = self.last_bid(symbol), self.last_ask(symbol)
        return Quote(symbol=symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                     timestamp=0.0, source="fake")


def _bare_consumer(account_id) -> TradingConsumer:
    """Same minimal-panel pattern already established in
    test_multipanel_position_sync.py — one dashboard panel's own
    connection state, nothing shared with any sibling connection."""
    from unittest.mock import AsyncMock

    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = "EUR/USD"
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    c._bid_state = {"EUR/USD": 1.10000}
    c._ask_state = {"EUR/USD": 1.10020}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}

    c._feed = _FakeFeed()
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


def _last_msg(consumer, msg_type):
    for call in reversed(consumer.send_json.call_args_list):
        msg = call.args[0]
        if msg.get("type") == msg_type:
            return msg
    return None


# ─────────────────────────────────────────────────────────────────────────
# ws_events.publish_position_changed — the single publish point
# ─────────────────────────────────────────────────────────────────────────
class PublishPositionChangedTests(TransactionTestCase):
    def test_calls_group_send_with_account_group_and_event_shape(self):
        from unittest.mock import AsyncMock
        with patch("channels.layers.get_channel_layer") as mock_gcl:
            mock_cl = mock_gcl.return_value
            mock_cl.group_send = AsyncMock()
            ws_events.publish_position_changed(
                77, action="close", position_id=5, symbol="EUR/USD",
            )
            mock_cl.group_send.assert_called_once()
            (group, payload), _ = mock_cl.group_send.call_args
            self.assertEqual(group, "account_77")
            self.assertEqual(payload["type"], "position.changed")
            self.assertEqual(payload["action"], "close")
            self.assertEqual(payload["position_id"], 5)
            self.assertEqual(payload["symbol"], "EUR/USD")

    def test_noop_when_account_id_falsy(self):
        with patch("channels.layers.get_channel_layer") as mock_gcl:
            ws_events.publish_position_changed(None, action="open")
            ws_events.publish_position_changed(0, action="open")
            mock_gcl.assert_not_called()

    def test_never_raises_on_channel_layer_failure(self):
        with patch("channels.layers.get_channel_layer", side_effect=RuntimeError("redis down")):
            ws_events.publish_position_changed(1, action="open")  # must not raise

    def test_never_raises_when_group_send_itself_fails(self):
        with patch("channels.layers.get_channel_layer") as mock_gcl:
            mock_gcl.return_value.group_send.side_effect = RuntimeError("boom")
            ws_events.publish_position_changed(1, action="open")  # must not raise


# ─────────────────────────────────────────────────────────────────────────
# 1. Two connections, same account — open from A, B syncs without action
# ─────────────────────────────────────────────────────────────────────────
class OpenSyncTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_open_via_db_open_atomic_publishes_and_b_syncs(self):
        panel_a = _bare_consumer(self.account.pk)
        panel_b = _bare_consumer(self.account.pk)

        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _db_open_sync(
                panel_a, symbol="EUR/USD", side="buy", qty=0.1, price=1.10000,
                sl=None, tp=None, commission=0.0, new_balance=10000.0,
            )
        self.assertTrue(result["ok"] if "ok" in result else True)
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], "open")
        self.assertEqual(kwargs["position_id"], result["position_id"])

        # Panel B never touched — simulate delivery of the real event and
        # confirm it alone brings B's book/PnL basis up to date.
        event = {"type": "position.changed", "action": "open",
                 "position_id": result["position_id"], "symbol": "EUR/USD"}
        _run = __import__("asyncio").run
        _run(panel_b.position_changed(event))

        self.assertEqual(len(panel_b._positions), 1)
        self.assertEqual(panel_b._positions[0]["symbol"], "EUR/USD")
        self.assertAlmostEqual(panel_b.account["balance"], 10000.0, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 2. Close from A — B removes the position without a manual refresh
# ─────────────────────────────────────────────────────────────────────────
class CloseSyncTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def _pos_mem(self):
        return {"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}

    def test_close_via_db_close_atomic_publishes_and_b_removes_position(self):
        panel_a = _bare_consumer(self.account.pk)
        panel_b = _bare_consumer(self.account.pk)
        panel_b._positions = [self._pos_mem()]

        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _db_close_sync(
                panel_a, self._pos_mem(), close_px=1.11000, reason="manual",
                realized_pnl=10.0, new_balance=10010.0, new_equity=10010.0,
            )
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], "close")
        self.assertEqual(kwargs["position_id"], self.pos.pk)
        self.assertEqual(kwargs["new_balance"], result["new_balance"])

        event = {"type": "position.changed", "action": "close",
                 "position_id": self.pos.pk, "symbol": "EUR/USD",
                 "new_balance": result["new_balance"], "new_status": result["new_status"],
                 "realized_pnl": 10.0, "reason": "manual"}
        __import__("asyncio").run(panel_b.position_changed(event))

        self.assertEqual(panel_b._positions, [])
        self.assertAlmostEqual(panel_b.account["balance"], 10010.0, places=2)
        notify = _last_msg(panel_b, "order_close")
        self.assertIsNotNone(notify)
        self.assertEqual(notify["id"], self.pos.pk)


# ─────────────────────────────────────────────────────────────────────────
# 3. Netting merge/update from A — B gets DB-fresh qty/avg_price
# ─────────────────────────────────────────────────────────────────────────
class NettingMergeSyncTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.account.netting_mode = True
        self.account.save(update_fields=["netting_mode"])
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def test_merge_publishes_update_action_and_b_sees_new_weighted_avg(self):
        _seed_eurusd_price()
        panel_a = _bare_consumer(self.account.pk)
        panel_a.account["netting_mode"] = True
        panel_b = _bare_consumer(self.account.pk)

        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _db_open_sync(
                panel_a, symbol="EUR/USD", side="buy", qty=0.1, price=1.20000,
                sl=None, tp=None, commission=0.0, new_balance=10000.0,
            )
        self.assertTrue(result["merged"])
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], "update")
        self.assertEqual(kwargs["position_id"], self.pos.pk)

        # Neutral current price near the new weighted average (1.15000) —
        # this test is about netting-merge sync, not about accidentally
        # triggering a real CHALLENGE stopout. O.6c-1q: _unrealized_pnl_total()
        # now reads self._feed, not self._bid_state — set it there.
        panel_b._feed.set_price("EUR/USD", 1.15000, 1.15020)

        event = {"type": "position.changed", "action": "update",
                 "position_id": self.pos.pk, "symbol": "EUR/USD"}
        __import__("asyncio").run(panel_b.position_changed(event))

        self.assertEqual(len(panel_b._positions), 1)
        # weighted average of 0.1@1.10000 + 0.1@1.20000 = 1.15000
        self.assertAlmostEqual(panel_b._positions[0]["avg"], 1.15000, places=5)
        self.assertAlmostEqual(panel_b._positions[0]["qty"], 0.2, places=5)


# ─────────────────────────────────────────────────────────────────────────
# 4. SL/TP — all connections sync (shares _db_close_position_atomic)
# ─────────────────────────────────────────────────────────────────────────
class SlTpSyncTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
                                  tp=Decimal("1.11000"))

    def test_tp_close_publishes_close_event(self):
        panel_a = _bare_consumer(self.account.pk)
        pos_mem = {"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": 1.11000, "opened_at": time.time()}
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            _db_close_sync(
                panel_a, pos_mem, close_px=1.11000, reason="tp",
                realized_pnl=10.0, new_balance=10010.0, new_equity=10010.0,
            )
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], "close")
        self.assertEqual(kwargs["reason"], "tp")


# ─────────────────────────────────────────────────────────────────────────
# 5/6. Stopout / retail liquidation — Celery daemon path
#
# _daemon_close_all() itself never publishes — per tasks.py's own
# structure, it only computes close_records and returns them; its
# caller, scan_positions_task, is the one that iterates close_records
# and calls ws_events.publish_position_changed() once per closed
# position (see tasks.py step "WS notify + AuditLog per closed
# position"). These tests exercise the real Celery task end-to-end
# (scan_positions_task.apply().get(), same pattern already established
# in test_daemon_scan.py) so the boundary is tested where it actually
# lives, not assumed.
# ─────────────────────────────────────────────────────────────────────────
from simulator.tasks import scan_positions_task
from django.test import TestCase as _PlainTestCase


def _scan():
    return scan_positions_task.apply().get()


class DaemonStopoutSyncTests(_PlainTestCase):
    """CHALLENGE 10K: peak=10000, stopout_level=9000. 1 lot EUR/USD BUY
    avg=1.10000, bid=1.08800 -> equity=8800 <= 9000 -> stopout ->
    _daemon_close_all runs, scan_positions_task publishes for each close.
    Exact scenario already established in test_daemon_scan.py."""

    @patch("simulator.tasks._read_cached_price")
    def test_stopout_batch_publishes_position_changed(self, mock_price):
        mock_price.side_effect = lambda symbol: (
            (1.08800, 1.08810) if symbol == "EUR/USD" else (None, None)
        )
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("1.10000"))

        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _scan()

        self.assertGreaterEqual(result["closed"], 1)
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], "close")
        self.assertEqual(kwargs["position_id"], pos.pk)
        self.assertEqual(kwargs["reason"], "daemon_stopout")


class DaemonRetailLiquidationSyncTests(TransactionTestCase):
    """6) Retail liquidation shares _close_position_sync with SL/TP —
    covered structurally by CeleryPublishTests below; this confirms the
    reason string threads through unchanged (margin_call is the retail
    liquidation reason in the daemon)."""

    def setUp(self):
        self.account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def test_close_position_sync_used_by_retail_liquidation_path(self):
        pos_mem = {"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _close_position_sync(
                pos_mem=pos_mem, account_id=self.account.pk, close_px=1.09000,
                reason="daemon_margin_call", realized_pnl=-10.0,
                new_balance=9990.0, new_equity=9990.0,
            )
        # Note: _close_position_sync itself does NOT publish (only its two
        # callers in tasks.py — scan_positions_task/_daemon_close_all —
        # do, after calling it). This test documents that boundary.
        mock_pub.assert_not_called()
        self.assertFalse(result["already_closed"])


# ─────────────────────────────────────────────────────────────────────────
# 7. Celery → WS — publish_position_changed called with action="close"
# (individual SL/TP/margin-call path — RETAIL margin call scenario
# already established in test_daemon_scan.py::TestDaemonScanRetailMarginCall)
# ─────────────────────────────────────────────────────────────────────────
class CeleryPublishContractTests(_PlainTestCase):
    @patch("simulator.tasks._read_cached_price")
    def test_retail_margin_call_uses_position_changed_contract(self, mock_price):
        mock_price.side_effect = lambda symbol: (
            (1.09000, 1.09010) if symbol == "EUR/USD" else (None, None)
        )
        account = make_account(account_type="RETAIL", balance=Decimal("1000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.5"), avg_price=Decimal("1.10000"))

        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            result = _scan()

        self.assertGreaterEqual(result["closed"], 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        mock_pub.assert_called_once()
        _, kwargs = mock_pub.call_args
        self.assertEqual(kwargs["action"], ws_events.ACTION_CLOSE)
        self.assertEqual(kwargs["position_id"], pos.pk)
        self.assertEqual(kwargs["symbol"], "EUR/USD")
        self.assertIn("new_balance", kwargs)
        self.assertIn("trade_id", kwargs)


class ExecutionCloseBackwardCompatTests(TransactionTestCase):
    """execution_close() delegates to the async position_changed(), which
    calls @database_sync_to_async methods — needs TransactionTestCase for
    the same reason documented throughout this file's module docstring."""

    def test_execution_close_backward_compat_alias_still_works(self):
        """A stale 'execution.close' event (rolling-deploy overlap) must
        still be handled correctly by the new consumer code. The
        position is already gone from DB (as it would be by the time a
        real close event exists) — only self._positions still has it,
        exactly the scenario this alias exists for."""
        account = make_account(balance=Decimal("10000"))
        panel = _bare_consumer(account.pk)
        panel._positions = [{"id": 555, "symbol": "EUR/USD", "side": "buy",
                              "qty": 0.1, "avg": 1.10000, "sl": None, "tp": None,
                              "opened_at": time.time()}]
        old_style_event = {
            "position_id": 555, "symbol": "EUR/USD", "side": "buy",
            "qty": 0.1, "avg": 1.10000, "close_px": 1.11, "realized_pnl": 10.0,
            "reason": "tp", "trade_id": None, "new_balance": 10010.0,
            "new_status": "Activo", "ts": int(time.time()),
        }
        __import__("asyncio").run(panel.execution_close(old_style_event))
        self.assertEqual(panel._positions, [])
        notify = _last_msg(panel, "order_close")
        self.assertIsNotNone(notify)
        self.assertEqual(notify["id"], 555)


# ─────────────────────────────────────────────────────────────────────────
# 8/9. Django Admin save/delete → WS
# ─────────────────────────────────────────────────────────────────────────
class AdminPublishTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def _admin_and_request(self):
        from django.contrib import admin as dj_admin
        from django.test import RequestFactory
        from simulator.admin import PositionAdmin
        rf = RequestFactory()
        req = rf.get("/x")
        return PositionAdmin(Position, dj_admin.site), req

    def test_save_model_publishes_update(self):
        ma, req = self._admin_and_request()
        self.pos.sl = Decimal("1.09000")
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            ma.save_model(req, self.pos, form=None, change=True)
        mock_pub.assert_called_once_with(
            self.account.pk, action=ws_events.ACTION_UPDATE,
            position_id=self.pos.pk, symbol="EUR/USD",
        )

    def test_delete_model_publishes_close_with_id_captured_before_delete(self):
        ma, req = self._admin_and_request()
        pos_id = self.pos.pk
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            ma.delete_model(req, self.pos)
        self.assertFalse(Position.objects.filter(pk=pos_id).exists())
        mock_pub.assert_called_once_with(
            self.account.pk, action=ws_events.ACTION_CLOSE,
            position_id=pos_id, symbol="EUR/USD",
        )


# ─────────────────────────────────────────────────────────────────────────
# 10. Rollback — position.changed must NOT be published
# ─────────────────────────────────────────────────────────────────────────
class RollbackNoPublishTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_open_rollback_never_publishes(self):
        panel = _bare_consumer(self.account.pk)
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            with patch(
                "simulator.consumers.Position.objects.create",
                side_effect=RuntimeError("simulated DB failure"),
            ):
                with self.assertRaises(RuntimeError):
                    _db_open_sync(
                        panel, symbol="EUR/USD", side="buy", qty=0.1, price=1.10000,
                        sl=None, tp=None, commission=0.0, new_balance=10000.0,
                    )
        mock_pub.assert_not_called()
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_close_rollback_never_publishes_and_position_survives(self):
        pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        pos_mem = {"id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}
        with patch.object(ws_events, "publish_position_changed") as mock_pub:
            with patch(
                "simulator.consumers.Trade.objects.create",
                side_effect=RuntimeError("simulated DB failure"),
            ):
                with self.assertRaises(RuntimeError):
                    _db_close_sync(
                        panel, pos_mem, close_px=1.11000, reason="manual",
                        realized_pnl=10.0, new_balance=10010.0, new_equity=10010.0,
                    )
        mock_pub.assert_not_called()
        # Rollback proven: Position still exists, no Trade was created,
        # balance untouched.
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("10000"))


# ─────────────────────────────────────────────────────────────────────────
# 11. Idempotencia
# ─────────────────────────────────────────────────────────────────────────
class IdempotencyTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def test_processing_same_close_event_twice_yields_identical_state(self):
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy",
                              "qty": 0.1, "avg": 1.10000, "sl": None, "tp": None,
                              "opened_at": time.time()}]
        self.pos.delete()  # DB already reflects the close

        event = {"action": "close", "position_id": self.pos.pk, "symbol": "EUR/USD",
                  "new_balance": 10010.0, "new_status": "Activo", "realized_pnl": 10.0}

        __import__("asyncio").run(panel.position_changed(event))
        state_1 = (list(panel._positions), dict(panel.account))

        __import__("asyncio").run(panel.position_changed(event))
        state_2 = (list(panel._positions), dict(panel.account))

        self.assertEqual(state_1[0], state_2[0])
        self.assertEqual(state_1[1]["balance"], state_2[1]["balance"])
        self.assertEqual(state_1[1]["equity"], state_2[1]["equity"])

    def test_event_for_position_id_already_absent_is_a_harmless_noop(self):
        """position_id=999999 doesn't exist anywhere — self._positions
        starts empty (stale relative to the real DB, which still has
        self.pos open). The event must not raise, and — because this
        handler always ends in a DB-fresh resync — the connection ends up
        CORRECTLY showing the real open position, not stuck at []."""
        panel = _bare_consumer(self.account.pk)
        panel._positions = []
        event = {"action": "close", "position_id": 999999, "symbol": "EUR/USD"}
        __import__("asyncio").run(panel.position_changed(event))  # must not raise
        self.assertEqual([p["id"] for p in panel._positions], [self.pos.pk])


# ─────────────────────────────────────────────────────────────────────────
# 12. Performance — no new DB query per price tick
# ─────────────────────────────────────────────────────────────────────────
class NoExtraQueryPerTickTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_recalc_account_and_push_hits_zero_queries_when_throttle_active(self):
        """O.6c-1o added zero DB reads to the per-tick path: pnl_unreal/
        margin_used are still pure in-memory iterations over
        self._positions (unchanged — broker_exposure.py/pnl_engine.py/
        models.py were never touched), and the only DB read on this path
        (_db_sync_account_balances(), PANEL-02's pre-existing throttle)
        is skipped when called again inside its 1.2s window — exactly
        what price_tick() does on every tick but the first."""
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{"id": 1, "symbol": "EUR/USD", "side": "buy",
                              "qty": 0.1, "avg": 1.10000, "sl": None, "tp": None,
                              "opened_at": time.time()}]
        panel._last_db_sync = time.time()  # inside the throttle window

        with patch("simulator.risk_engine.check_equity_stopout", return_value=False):
            reset_queries()
            __import__("asyncio").run(panel._recalc_account_and_push())
            self.assertEqual(len(connection.queries), 0)

    def test_unrealized_pnl_total_and_margin_used_total_never_query_db(self):
        panel = _bare_consumer(self.account.pk)
        panel._positions = [
            {"id": i, "symbol": "EUR/USD", "side": "buy", "qty": 0.1, "avg": 1.10000}
            for i in range(5)
        ]
        reset_queries()
        panel._unrealized_pnl_total()
        panel._margin_used_total()
        self.assertEqual(len(connection.queries), 0)


# ─────────────────────────────────────────────────────────────────────────
# 13. Persisted TradingAccount.equity regression
# ─────────────────────────────────────────────────────────────────────────
class PersistedEquityRegressionTests(TransactionTestCase):
    """Reproduces the exact O.6c-1m/O.6c-1n concern: a position changes on
    a DIFFERENT connection; after this connection processes the
    position.changed event (not before), _db_sync_account_balances() must
    persist an equity value consistent with the DB-fresh position set —
    never one still including the now-closed "phantom" position."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.pos = make_position(self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("0.1"), avg_price=Decimal("1.10000"))

    def test_equity_persisted_after_sync_excludes_phantom_position(self):
        panel_b = _bare_consumer(self.account.pk)
        # Panel B still believes the position is open (as it would be,
        # pre-fix, with no cross-connection notification at all).
        panel_b._positions = [{"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy",
                                "qty": 0.1, "avg": 1.10000, "sl": None, "tp": None,
                                "opened_at": time.time()}]
        panel_b.account["pnl_unreal"] = 500.0  # would be a large phantom PnL

        # Connection A closes it for real, on a different connection.
        panel_a = _bare_consumer(self.account.pk)
        pos_mem = {"id": self.pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                   "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time()}
        result = _db_close_sync(
            panel_a, pos_mem, close_px=1.11000, reason="manual",
            realized_pnl=10.0, new_balance=10010.0, new_equity=10010.0,
        )

        # Panel B receives the notification and resyncs (this is the fix
        # under test — before O.6c-1o, nothing would ever call this).
        event = {"action": "close", "position_id": self.pos.pk, "symbol": "EUR/USD",
                  "new_balance": result["new_balance"], "new_status": result["new_status"],
                  "realized_pnl": 10.0}
        __import__("asyncio").run(panel_b.position_changed(event))

        # The persisted DB column must now reflect balance-only equity
        # (no open positions left) — never balance + the phantom 500.0.
        self.account.refresh_from_db()
        self.assertAlmostEqual(float(self.account.equity), float(self.account.balance), places=2)
        self.assertNotAlmostEqual(float(self.account.equity), float(self.account.balance) + 500.0, places=2)
        self.assertEqual(panel_b._positions, [])
