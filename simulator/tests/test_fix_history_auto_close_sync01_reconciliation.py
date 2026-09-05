# simulator/tests/test_fix_history_auto_close_sync01_reconciliation.py
"""
FIX-HISTORY-AUTO-CLOSE-SYNC-01 — History reconciliation.

Problem this closes: dashboard.html's closedTradesHistory was seeded once
from the server-rendered dashboard load (views.py) and afterward depended
exclusively on live order_close WS events — no mechanism ever
re-synchronized it against the backend. If a single order_close event was
ever missed on a given browser tab (dropped connection invisible to the
server's own heartbeat, tab backgrounding, or any other transient client-
side failure), the corresponding Trade row disappeared from that tab's
History permanently, with no self-healing path short of a full reload.
Confirmed reproduced with real data: Trade 292 (SL close) and Trade 293
(manual close), Account 51 — both existed correctly in the DB the whole
time; neither appeared in the open dashboard session's History tab.

Fix, per the approved Design Lock:
  1. simulator/services/history.py — closed_trades_for_account(), a
     single shared definition of "closed trades for History", extracted
     verbatim from views.py's pre-existing dashboard seed query. Reused
     by BOTH the HTTP seed (views.py) and the new WS action below — no
     second, independently-drifting definition.
  2. simulator/consumers.py — new `get_closed_trades` WS action ->
     `closed_trades_snapshot` response, account-scoped EXCLUSIVELY via
     self._db_account_id (never a client-supplied account_id), wrapped
     in database_sync_to_async like every other ORM access in this
     consumer.
  3. dashboard.html — reconcileClosedTrades(trades), a single merge/
     dedupe function (key: trade.id, never position_id) shared by the
     live order_close path and the new snapshot path. Triggered on WS
     onopen/reconnect and on entering the History tab — never polling.
     The live path also stopped using Date.now() as the close time —
     msg.ts (the server's own close-commit timestamp) is used when
     present.

Structural/textual JS assertions (dashboard.html) have no JS runtime in
this repo (same situation as every other frontend fix in this session) —
they are not a substitute for a real JS test. Backend pieces (the shared
helper, the consumer action, serialization, async-safety) are exercised
for real against the DB.
"""
import asyncio
import json
from decimal import Decimal

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from simulator.consumers import TradingConsumer
from simulator.models import Trade, TradingAccount
from simulator.services.history import closed_trades_for_account

from .factories import make_account


def _template_source():
    path = get_template("simulator/dashboard.html").origin.name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _slice(src, start_marker, end_marker, start_from=0):
    i = src.index(start_marker, start_from)
    j = src.index(end_marker, i)
    return src[i:j]


def _reconcile_block(src):
    return _slice(src, "function reconcileClosedTrades(trades){", "/* ── Historial panel sync ── */")


def _order_close_block(src):
    return _slice(src, "if(msg.type==='order_close'){", "/* ── Indicator rendering ── */")


def _onopen_block(src):
    return _slice(src, "this.ws.onopen=()=>{", "this.ws.onmessage=")


def _show_tab_block(src):
    return _slice(src, "function showTab(tab){", "document.querySelectorAll('.tabbtn')")


def _handle_msg_signature_block(src):
    return _slice(src, "if(msg.type==='closed_trades_snapshot'){", "if(msg.type==='order_close'){")


def _make_trade(account, *, symbol="BTCUSD", lot_size="0.01", entry="80000.00",
                 exit_="80100.00", pnl="1.00", closed_at=None):
    from django.utils.dateparse import parse_datetime
    t = Trade.objects.create(
        account=account, symbol=symbol, trade_type=Trade.BUY,
        lot_size=Decimal(lot_size), entry_price=Decimal(entry),
        exit_price=Decimal(exit_), profit_loss=Decimal(pnl),
    )
    if closed_at is not None:
        dt = parse_datetime(closed_at) if isinstance(closed_at, str) else closed_at
        Trade.objects.filter(id=t.id).update(closed_at=dt)
        t.refresh_from_db()
    return t


# Same __wrapped__ / bare-instance pattern as test_order_management_v2a.py
# and test_atomic_margin_and_position_guard.py.
_db_get_closed_trades_sync = TradingConsumer._db_get_closed_trades.__wrapped__


def _consumer(account_id):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c._last_msg_ts = 0.0
    c.sent = []

    async def _send_json(payload):
        c.sent.append(payload)
    c.send_json = _send_json
    return c


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# A. Shared backend helper (simulator/services/history.py)
# ─────────────────────────────────────────────────────────────────────────
class ClosedTradesHelperTests(TestCase):
    def test_scoped_to_account_only(self):
        acc1 = make_account(balance=Decimal("10000"))
        acc2 = make_account(balance=Decimal("10000"))
        _make_trade(acc1, closed_at="2026-09-05T19:50:09Z")
        _make_trade(acc2, closed_at="2026-09-05T19:50:09Z")
        result = closed_trades_for_account(acc1)
        self.assertEqual(len(result), 1)

    def test_excludes_still_open_trades(self):
        acc = make_account(balance=Decimal("10000"))
        Trade.objects.create(
            account=acc, symbol="BTCUSD", trade_type=Trade.BUY,
            lot_size=Decimal("0.01"), entry_price=Decimal("80000"),
        )  # closed_at defaults to NULL — never closed
        self.assertEqual(closed_trades_for_account(acc), [])

    def test_newest_first_ordering(self):
        acc = make_account(balance=Decimal("10000"))
        older = _make_trade(acc, closed_at="2026-09-04T10:00:00Z")
        newer = _make_trade(acc, closed_at="2026-09-05T20:00:06Z")
        result = closed_trades_for_account(acc)
        self.assertEqual([r["id"] for r in result], [newer.id, older.id])

    def test_limit_default_50(self):
        acc = make_account(balance=Decimal("10000"))
        for _ in range(55):
            _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        # 55 distinct rows, default limit must cap at 50 regardless of tie timestamps.
        self.assertEqual(len(closed_trades_for_account(acc)), 50)

    def test_explicit_limit_honored(self):
        acc = make_account(balance=Decimal("10000"))
        for _ in range(5):
            _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        self.assertEqual(len(closed_trades_for_account(acc, limit=3)), 3)

    def test_accepts_account_instance_or_bare_id(self):
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        by_instance = closed_trades_for_account(acc)
        by_id = closed_trades_for_account(acc.id)
        self.assertEqual(by_instance, by_id)

    def test_shape_matches_frontend_contract(self):
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, symbol="BTCUSD", lot_size="0.01", entry="79847.70",
                    exit_="79717.87", pnl="-1.30", closed_at="2026-09-05T19:50:09Z")
        row = closed_trades_for_account(acc)[0]
        self.assertEqual(set(row.keys()), {"id", "symbol", "side", "qty", "entry", "close", "pnl", "ts"})
        self.assertEqual(row["symbol"], "BTCUSD")
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["qty"], 0.01)
        self.assertEqual(row["entry"], 79847.70)
        self.assertEqual(row["close"], 79717.87)
        self.assertEqual(row["pnl"], -1.30)

    def test_no_close_reason_field(self):
        # Design lock: "NO agregar close_reason" — Trade has no such field.
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        row = closed_trades_for_account(acc)[0]
        self.assertNotIn("close_reason", row)
        self.assertNotIn("reason", row)

    def test_trade_292_293_like_case_recovered(self):
        # The exact reported scenario: two recent BTCUSD closes for the
        # same account, both must come back, newest first.
        acc = make_account(balance=Decimal("1049.55"), account_type="STANDARD")
        t292 = _make_trade(acc, entry="79847.70", exit_="79717.87", pnl="-1.30",
                            closed_at="2026-09-05T19:50:09Z")
        t293 = _make_trade(acc, entry="79773.70", exit_="79756.61", pnl="-0.17",
                            closed_at="2026-09-05T20:00:06Z")
        result = closed_trades_for_account(acc)
        self.assertEqual([r["id"] for r in result], [t293.id, t292.id])

    def test_partial_close_two_trades_same_position_both_appear(self):
        # Design lock §11/§12 — a Position can yield multiple Trade rows;
        # dedupe/identity must be Trade.id, never position_id (there is
        # no position_id on Trade at all — confirms it structurally).
        acc = make_account(balance=Decimal("10000"))
        t1 = _make_trade(acc, lot_size="0.30", pnl="3.00", closed_at="2026-09-05T10:00:00Z")
        t2 = _make_trade(acc, lot_size="0.70", pnl="7.00", closed_at="2026-09-05T10:05:00Z")
        result = closed_trades_for_account(acc)
        self.assertEqual({r["id"] for r in result}, {t1.id, t2.id})
        self.assertEqual(len(result), 2)

    def test_result_is_json_serializable_without_django_encoder(self):
        # consumers.py's send_json uses plain json.dumps (no
        # DjangoJSONEncoder) — Decimal/datetime must never leak through.
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        result = closed_trades_for_account(acc)
        raw = json.dumps({"type": "closed_trades_snapshot", "trades": result})
        self.assertIn("closed_trades_snapshot", raw)
        for row in result:
            for v in row.values():
                self.assertNotIsInstance(v, Decimal)


# ─────────────────────────────────────────────────────────────────────────
# B. Consumer action (get_closed_trades -> closed_trades_snapshot)
# ─────────────────────────────────────────────────────────────────────────
class ConsumerActionTests(TestCase):
    def test_db_helper_wrapped_returns_same_shape(self):
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, closed_at="2026-09-05T10:00:00Z")
        c = _consumer(acc.id)
        result = _db_get_closed_trades_sync(c)
        self.assertEqual(result, closed_trades_for_account(acc))

    def test_no_account_returns_empty_snapshot_not_error(self):
        c = _consumer(None)
        _run(c._send_closed_trades_snapshot())
        self.assertEqual(c.sent, [{"type": "closed_trades_snapshot", "trades": []}])

    def test_db_failure_degrades_to_empty_snapshot_never_raises(self):
        from unittest.mock import patch
        acc = make_account(balance=Decimal("10000"))
        c = _consumer(acc.id)
        with patch.object(TradingConsumer, "_db_get_closed_trades",
                           side_effect=Exception("db down")):
            _run(c._send_closed_trades_snapshot())
        self.assertEqual(c.sent, [{"type": "closed_trades_snapshot", "trades": []}])

    def test_method_signature_takes_no_client_data(self):
        # Structural guarantee that account scoping cannot be bypassed:
        # _send_closed_trades_snapshot has no parameter a client payload
        # could ever populate.
        import inspect
        sig = inspect.signature(TradingConsumer._send_closed_trades_snapshot)
        self.assertEqual(list(sig.parameters), ["self"])


class ConsumerActionCrossThreadTests(TransactionTestCase):
    """
    database_sync_to_async dispatches the real _db_get_closed_trades to a
    separate thread/connection — plain TestCase's transaction-wrapped
    SQLite connection deadlocks against that ("database table is locked").
    TransactionTestCase (real commits, no wrapping transaction) is the
    same fix this codebase already uses elsewhere for exactly this
    combination (@database_sync_to_async + asyncio.run() in a test).
    """
    def test_snapshot_sent_on_action(self):
        acc = make_account(balance=Decimal("10000"))
        _make_trade(acc, closed_at="2026-09-05T19:50:09Z")
        c = _consumer(acc.id)
        _run(c.receive(json.dumps({"action": "get_closed_trades"})))
        self.assertEqual(len(c.sent), 1)
        self.assertEqual(c.sent[0]["type"], "closed_trades_snapshot")
        self.assertEqual(len(c.sent[0]["trades"]), 1)

    def test_client_supplied_account_id_is_ignored(self):
        # Design lock §4 / authorization §3 — even if the client sends
        # account_id, it must be ignored: only self._db_account_id (this
        # connection's own, already-authorized account) is used.
        real_acc = make_account(balance=Decimal("10000"))
        other_acc = make_account(balance=Decimal("10000"))
        _make_trade(real_acc, closed_at="2026-09-05T10:00:00Z")
        _make_trade(other_acc, closed_at="2026-09-05T10:00:00Z")
        c = _consumer(real_acc.id)
        _run(c.receive(json.dumps({"action": "get_closed_trades", "account_id": other_acc.id})))
        trades = c.sent[0]["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(closed_trades_for_account(real_acc), trades)


# ─────────────────────────────────────────────────────────────────────────
# C. views.py seed — unchanged behavior via the shared helper
# ─────────────────────────────────────────────────────────────────────────
class ViewsSeedRegressionTests(TestCase):
    def test_views_uses_shared_helper_not_inline_query(self):
        import inspect
        from simulator import views
        src = inspect.getsource(views.trading_dashboard)
        self.assertIn("closed_trades_json = json.dumps(\n        closed_trades_for_account(account),", src)


# ─────────────────────────────────────────────────────────────────────────
# D. Frontend structural assertions (dashboard.html) — no JS runtime
# ─────────────────────────────────────────────────────────────────────────
class ReconcileFunctionTests(SimpleTestCase):
    def test_function_exists(self):
        self.assertIn("function reconcileClosedTrades(trades){", _template_source())

    def test_empty_or_missing_trades_is_a_noop(self):
        block = _reconcile_block(_template_source())
        self.assertIn("if(!Array.isArray(trades)||!trades.length)return;", block)

    def test_dedupe_key_is_id_never_position_id(self):
        block = _reconcile_block(_template_source())
        self.assertIn("String(t.id)", block)
        self.assertNotIn("position_id", block)

    def test_newest_first_sort_with_id_tiebreak(self):
        block = _reconcile_block(_template_source())
        self.assertIn("closedTradesHistory.sort(", block)
        self.assertIn("(b.ts||0)-(a.ts||0)", block)
        self.assertIn("Number(b.id)", block)  # deterministic tie-break

    def test_cap_100_preserved(self):
        block = _reconcile_block(_template_source())
        self.assertIn("while(closedTradesHistory.length>100)closedTradesHistory.pop();", block)


class LiveOrderCloseIntegrationTests(SimpleTestCase):
    def test_order_close_delegates_to_reconcile(self):
        block = _order_close_block(_template_source())
        self.assertIn("reconcileClosedTrades([{id:msg.trade_id,", block)
        # No independent dedupe/insert logic left inline.
        self.assertNotIn("closedTradesHistory.some(", block)
        self.assertNotIn("closedTradesHistory.unshift(", block)

    def test_timestamp_prefers_server_ts_over_date_now(self):
        block = _order_close_block(_template_source())
        self.assertIn("const tsV=(msg.ts!=null)?Number(msg.ts)*1000:Date.now();", block)

    def test_snapshot_message_type_handled(self):
        src = _template_source()
        self.assertIn(
            "if(msg.type==='closed_trades_snapshot'){", src,
        )
        self.assertIn("reconcileClosedTrades(msg.trades||[]);", src)


class ReconciliationTriggerTests(SimpleTestCase):
    def test_onopen_requests_snapshot(self):
        block = _onopen_block(_template_source())
        self.assertIn("this.ws.send(JSON.stringify({action:'get_closed_trades'}));", block)

    def test_history_tab_requests_snapshot(self):
        block = _show_tab_block(_template_source())
        self.assertIn("action:'get_closed_trades'", block)
        # Guarded by an open-socket check — never sent blind.
        self.assertIn("readyState===WebSocket.OPEN", block)

    def test_no_polling_timer_introduced(self):
        src = _template_source()
        # The only new send sites are onopen and showTab (already asserted
        # above) — grep the whole file for get_closed_trades and confirm
        # there are exactly 2 send call-sites plus the elif dispatch text.
        self.assertEqual(src.count("action:'get_closed_trades'"), 2)