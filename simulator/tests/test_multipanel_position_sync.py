"""
simulator/tests/test_multipanel_position_sync.py — MULTIPANEL-01.

Covers TradingConsumer._refresh_and_send_positions(): the single helper
that replaced every direct `send_json({"type":"positions", ...})` call.
Root cause fixed: each dashboard panel is its own WebSocket connection
(its own TradingConsumer instance) with its own self._positions, hydrated
once at connect() and never synced with sibling connections for the same
account. Opening a position through Panel A, then opening/switching
symbol on Panel B, could push Panel B's incomplete in-memory view — which
the frontend then propagated to every panel, making Panel A's position
"disappear". The fix: always re-hydrate from the DB (the single source of
truth, account-wide) immediately before building any "positions" snapshot.

Uses TransactionTestCase, not TestCase: _refresh_and_send_positions()
awaits a @database_sync_to_async method, which runs the actual query on a
different thread — TestCase's uncommitted per-test transaction is
invisible to (and can deadlock against) that thread on SQLite. This is
the same reasoning documented in test_spread_config_cache.py's
EnsureBackgroundRefreshStartedTests (SPREAD-03).
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from simulator.consumers import TradingConsumer
from simulator.models import Position

from .factories import make_account, make_position, make_user


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer(account_id) -> TradingConsumer:
    """A minimal TradingConsumer standing in for one dashboard panel's own
    WebSocket connection. send_json is a spy so tests can inspect exactly
    what was sent, mirroring one real panel's view."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = "EUR/USD"
    c._positions = []
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


def _sent_positions_items(consumer) -> list:
    """Returns the 'items' payload of the last {"type":"positions",...}
    call captured by the send_json spy."""
    for call in reversed(consumer.send_json.call_args_list):
        msg = call.args[0]
        if msg.get("type") == "positions":
            return msg["items"]
    raise AssertionError("no 'positions' message was sent")


class TwoPanelsSameAccountTests(TransactionTestCase):
    """1-4) Dos instancias TradingConsumer para la misma cuenta. Panel A
    abre USD/JPY. Panel B abre GBP/USD. El snapshot emitido por B contiene
    ambas posiciones."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_panel_b_snapshot_contains_both_positions(self):
        panel_a = _bare_consumer(self.account.pk)
        panel_b = _bare_consumer(self.account.pk)

        # Panel A "opens" USD/JPY — written directly to Position (what
        # _db_open_position_atomic would have committed), then Panel A's
        # own connection refreshes/sends (as _order_new does at the end).
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        _run(panel_a._refresh_and_send_positions())
        items_a = _sent_positions_items(panel_a)
        self.assertEqual({p["symbol"] for p in items_a}, {"USD/JPY"})

        # Panel B — a SEPARATE connection, hydrated independently, never
        # told about Panel A's USD/JPY via any in-memory channel — opens
        # GBP/USD. Before the fix, panel_b.send_json would have emitted
        # only [GBP/USD] (panel_b._positions never knew about USD/JPY).
        make_position(self.account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))
        _run(panel_b._refresh_and_send_positions())
        items_b = _sent_positions_items(panel_b)
        self.assertEqual({p["symbol"] for p in items_b}, {"USD/JPY", "GBP/USD"})

    def test_change_symbol_on_panel_a_still_sends_both(self):
        """5) A continuación, change_symbol en A sigue enviando ambas."""
        panel_a = _bare_consumer(self.account.pk)
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        make_position(self.account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        # Simulates the tail of the change_symbol branch in receive():
        # it now calls _refresh_and_send_positions() instead of a raw send.
        panel_a.symbol = "GBP/USD"
        _run(panel_a._refresh_and_send_positions())
        items = _sent_positions_items(panel_a)
        self.assertEqual({p["symbol"] for p in items}, {"USD/JPY", "GBP/USD"})


class SlTpUpdateTests(TransactionTestCase):
    """6) update SL/TP en una posición no elimina la otra."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_updating_one_positions_sl_keeps_the_other_visible(self):
        pos_jpy = make_position(self.account, symbol="USD/JPY", side="BUY",
                                 qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        make_position(self.account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        Position.objects.filter(pk=pos_jpy.pk).update(sl=Decimal("154.500"))

        panel = _bare_consumer(self.account.pk)
        _run(panel._refresh_and_send_positions())
        items = _sent_positions_items(panel)
        self.assertEqual({p["symbol"] for p in items}, {"USD/JPY", "GBP/USD"})
        jpy_item = next(p for p in items if p["symbol"] == "USD/JPY")
        self.assertAlmostEqual(jpy_item["sl"], 154.5, places=3)


class CloseOnePositionTests(TransactionTestCase):
    """7) close de GBP/USD deja USD/JPY. 11) snapshot no contiene
    posiciones cerradas."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_closing_gbpusd_leaves_usdjpy_visible(self):
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        pos_gbp = make_position(self.account, symbol="GBP/USD", side="BUY",
                                 qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        # A close deletes the Position row (matches _db_close_position_atomic).
        pos_gbp.delete()

        panel = _bare_consumer(self.account.pk)
        _run(panel._refresh_and_send_positions())
        items = _sent_positions_items(panel)
        self.assertEqual({p["symbol"] for p in items}, {"USD/JPY"})
        self.assertNotIn("GBP/USD", {p["symbol"] for p in items})


class ReconnectHydrationTests(TransactionTestCase):
    """8) reconexión hidrata ambas posiciones existentes."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_maybe_hydrate_from_db_loads_both_positions(self):
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        make_position(self.account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        panel = _bare_consumer(self.account.pk)
        _run(panel._maybe_hydrate_from_db())
        self.assertEqual({p["symbol"] for p in panel._positions}, {"USD/JPY", "GBP/USD"})


class DbFailureSafetyTests(TransactionTestCase):
    """9) fallo DB no sustituye el estado por []."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_db_failure_keeps_previous_state_and_logs(self):
        panel = _bare_consumer(self.account.pk)
        panel._positions = [
            {"id": 1, "symbol": "USD/JPY", "side": "buy", "qty": 1.0, "avg": 155.000,
             "sl": None, "tp": None, "opened_at": 0},
        ]

        with patch.object(
            TradingConsumer, "_db_fetch_open_positions",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with self.assertLogs("simulator.ws", level="ERROR") as captured:
                _run(panel._refresh_and_send_positions())

        self.assertIn("refresh failed", "\n".join(captured.output))
        # self._positions was NOT wiped to [] — still the pre-failure state.
        self.assertEqual(len(panel._positions), 1)
        items = _sent_positions_items(panel)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "USD/JPY")

    def test_db_failure_never_sends_fabricated_empty_array(self):
        panel = _bare_consumer(self.account.pk)
        panel._positions = [
            {"id": 1, "symbol": "GBP/USD", "side": "buy", "qty": 1.0, "avg": 1.3,
             "sl": None, "tp": None, "opened_at": 0},
        ]
        with patch.object(
            TradingConsumer, "_db_fetch_open_positions",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            _run(panel._refresh_and_send_positions())
        items = _sent_positions_items(panel)
        self.assertNotEqual(items, [])


class AccountIsolationTests(TransactionTestCase):
    """10) cuentas distintas permanecen aisladas."""

    def test_two_accounts_never_see_each_others_positions(self):
        account_1 = make_account(balance=Decimal("10000"))
        account_2 = make_account(balance=Decimal("10000"))
        make_position(account_1, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        make_position(account_2, symbol="GBP/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.30000"))

        panel_1 = _bare_consumer(account_1.pk)
        panel_2 = _bare_consumer(account_2.pk)
        _run(panel_1._refresh_and_send_positions())
        _run(panel_2._refresh_and_send_positions())

        items_1 = _sent_positions_items(panel_1)
        items_2 = _sent_positions_items(panel_2)
        self.assertEqual({p["symbol"] for p in items_1}, {"USD/JPY"})
        self.assertEqual({p["symbol"] for p in items_2}, {"GBP/USD"})


class GuestSessionTests(TransactionTestCase):
    """Demo/guest session (no _db_account_id) — self._positions is the
    only source of truth and must never be wiped by a DB re-fetch that
    would legitimately return [] for a non-existent account."""

    def test_guest_session_skips_db_refetch_and_keeps_in_memory_positions(self):
        panel = _bare_consumer(None)
        panel._positions = [
            {"id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 1.0, "avg": 1.1,
             "sl": None, "tp": None, "opened_at": 0},
        ]
        _run(panel._refresh_and_send_positions())
        items = _sent_positions_items(panel)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "EUR/USD")


class SendPositionsSnapshotDelegatesTests(TransactionTestCase):
    """send_positions_snapshot() (called at connect()) is now a thin
    delegate to the canonical helper — no duplicated re-fetch logic."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_send_positions_snapshot_delegates_to_refresh_helper(self):
        panel = _bare_consumer(self.account.pk)
        make_position(self.account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("155.000"))
        _run(panel.send_positions_snapshot())
        items = _sent_positions_items(panel)
        self.assertEqual({p["symbol"] for p in items}, {"USD/JPY"})


class FrontendStructuralTests(TestCase):
    """6) Frontend — white-box HTML source inspection (same approach as
    test_dashboard_pnl_contract_size.py). This block deliberately does NOT
    change any frontend code (per its own scope) — these tests only
    confirm the two properties the backend fix depends on staying true:
    the 'positions' handler still filters chart overlays by symbol
    (_renderLines), and the global table still reads the account-wide
    positionsCache across all panels (unfiltered by symbol)."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(reverse("simulator:dashboard_account", args=[self.account.pk]))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_render_lines_still_filters_by_current_symbol(self):
        html = self._html()
        start = html.index("_renderLines(rows){")
        end = html.index("\n  }", start)
        block = html[start:end]
        self.assertIn("p.symbol===this.currentSymbol", block)

    def test_global_positions_table_reads_all_panels_unfiltered_by_symbol(self):
        html = self._html()
        start = html.index("function renderGlobalPositions")
        end = html.index("\n}", start)
        block = html[start:end]
        self.assertIn("for(const panel of allPanels)", block)
        self.assertIn("panel.positionsCache", block)
        # No symbol filter in the aggregation loop — every symbol must appear.
        self.assertNotIn("p.symbol===", block)
        self.assertNotIn("pos.symbol===", block)
