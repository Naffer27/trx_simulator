"""
BOOK-05d.1 — Liquidity Ledger Foundation tests.

Foundation only: the LiquidityLedger model and its read-only admin
surface. No writer exists yet (BOOK-05d.2) and no integration with any
real close path exists yet (BOOK-05d.3a/b/c) — no test here creates a
row via any writer function, and no test touches
TradingConsumer._db_close_position_atomic(), tasks._close_position_sync(),
or admin.py::force_close.
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.admin import LiquidityLedgerAdmin
from simulator.models import LiquidityDecision, LiquidityLedger, RoutingDecision, Trade
from simulator.routing_engine import Book

from .factories import make_account, make_user


# ─────────────────────────────────────────────────────────────────────────
# 1-6. Model
# ─────────────────────────────────────────────────────────────────────────
class LiquidityLedgerModelTests(TestCase):

    def test_creates_with_minimal_fields(self):
        entry = LiquidityLedger.objects.create(
            symbol="BTCUSD", simulated_pnl=Decimal("10.00"),
        )
        self.assertIsNotNone(entry.id)
        self.assertEqual(entry.symbol, "BTCUSD")
        self.assertEqual(entry.simulated_pnl, Decimal("10.00"))
        self.assertEqual(entry.meta, {})
        self.assertIsNotNone(entry.created_at)
        self.assertIsNone(entry.source_trade_id)
        self.assertIsNone(entry.liquidity_decision_id)

    def test_creates_with_all_fields(self):
        account = make_account(balance=Decimal("100000"))
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("0.1"), entry_price=Decimal("100.00"),
            exit_price=Decimal("110.00"), profit_loss=Decimal("1.00"),
        )
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        entry = LiquidityLedger.objects.create(
            source_trade=trade,
            liquidity_decision=liquidity_decision,
            symbol="BTCUSD",
            simulated_pnl=Decimal("-1.00"),
            meta={"trader_pnl": 1.00, "close_reason": "manual"},
        )

        self.assertEqual(entry.source_trade_id, trade.id)
        self.assertEqual(entry.liquidity_decision_id, liquidity_decision.id)
        self.assertEqual(entry.symbol, "BTCUSD")
        self.assertEqual(entry.simulated_pnl, Decimal("-1.00"))
        self.assertEqual(entry.meta, {"trader_pnl": 1.00, "close_reason": "manual"})

    def test_set_null_on_deleting_trade(self):
        account = make_account(balance=Decimal("100000"))
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("0.1"), entry_price=Decimal("100.00"),
        )
        entry = LiquidityLedger.objects.create(
            source_trade=trade, symbol="BTCUSD", simulated_pnl=Decimal("0.00"),
        )

        trade.delete()
        entry.refresh_from_db()

        self.assertIsNone(entry.source_trade_id)
        self.assertTrue(LiquidityLedger.objects.filter(pk=entry.pk).exists())

    def test_set_null_on_deleting_liquidity_decision(self):
        liquidity_decision = LiquidityDecision.objects.create(symbol="BTCUSD")
        entry = LiquidityLedger.objects.create(
            liquidity_decision=liquidity_decision, symbol="BTCUSD", simulated_pnl=Decimal("0.00"),
        )

        liquidity_decision.delete()
        entry.refresh_from_db()

        self.assertIsNone(entry.liquidity_decision_id)
        self.assertTrue(LiquidityLedger.objects.filter(pk=entry.pk).exists())

    def test_default_ordering_is_newest_first(self):
        d1 = LiquidityLedger.objects.create(symbol="A", simulated_pnl=Decimal("1.00"))
        d2 = LiquidityLedger.objects.create(symbol="B", simulated_pnl=Decimal("2.00"))
        ordered = list(LiquidityLedger.objects.all())
        self.assertEqual(ordered[0].id, d2.id)
        self.assertEqual(ordered[1].id, d1.id)

    def test_expected_indexes(self):
        index_names = {idx.name for idx in LiquidityLedger._meta.indexes}
        self.assertIn("liquidity_ledger_symbol_ts_idx", index_names)
        self.assertIn("liquidity_ledger_dec_ts_idx", index_names)


# ─────────────────────────────────────────────────────────────────────────
# 7-12. Admin
# ─────────────────────────────────────────────────────────────────────────
class LiquidityLedgerAdminTests(TestCase):

    def test_registered(self):
        self.assertIn(LiquidityLedger, admin_site._registry)

    def test_cannot_add(self):
        ma = LiquidityLedgerAdmin(LiquidityLedger, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = LiquidityLedgerAdmin(LiquidityLedger, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = LiquidityLedgerAdmin(LiquidityLedger, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = LiquidityLedgerAdmin(LiquidityLedger, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="book05d1_ro_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_all_fields_readonly(self):
        ma = LiquidityLedgerAdmin(LiquidityLedger, admin_site)
        model_fields = {f.name for f in LiquidityLedger._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        LiquidityLedger.objects.create(symbol="BTCUSD", simulated_pnl=Decimal("5.00"))
        staff = make_user(username="book05d1_ro_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquidityledger_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected_via_permission_check(self):
        staff = make_user(username="book05d1_ro_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquidityledger_add")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_detail_view_loads(self):
        entry = LiquidityLedger.objects.create(symbol="BTCUSD", simulated_pnl=Decimal("5.00"))
        staff = make_user(username="book05d1_ro_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquidityledger_change", args=[entry.pk])
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
