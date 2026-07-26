"""
BOOK-05d.3c — Liquidity Ledger integration into
admin.py::TradingAccountAdmin.dealing_desk_view (action="force_close").

Covers the single new integration point authorized for this block: the
nested-savepoint lookup of LiquidityDecision plus the call to
record_liquidity_ledger_entry() (BOOK-05d.2, untouched), placed
immediately after create_broker_counterparty_entry() and before
_audit.record_admin_event(), once per position inside the
force_close queryset loop. No test here touches consumers.py or
tasks.py — those are BOOK-05d.3a/3b, already done, out of scope here.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse

from simulator.models import (
    BrokerLedger, LiquidityDecision, LiquidityLedger, Position, RoutingDecision, Trade,
)
from simulator.routing_engine import Book

from .factories import make_account, make_user


def _make_position_with_routing_decision(account, symbol="BTCUSD"):
    routing_decision = RoutingDecision.objects.create(
        book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
    )
    pos = Position.objects.create(
        account=account, symbol=symbol, side="BUY",
        qty=Decimal("1.0"), avg_price=Decimal("100.00"),
        routing_decision=routing_decision,
    )
    return pos, routing_decision


class LiquidityLedgerForceCloseIntegrationTests(TestCase):

    def setUp(self):
        self.account = make_account(balance=Decimal("50000"))
        self.superuser = make_user(
            username="book05d3c_admin", is_staff=True, is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.superuser)

    def _force_close(self, symbol=None, price="110"):
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[self.account.id])
        data = {"action": "force_close", "price": price}
        if symbol is not None:
            data["symbol"] = symbol
        return self.client.post(url, data)

    # ── 1. Con LiquidityDecision previa ─────────────────────────────────
    def test_with_liquidity_decision_creates_exactly_one_entry(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        entry = LiquidityLedger.objects.get()
        trade = Trade.objects.get(account=self.account)

        self.assertEqual(entry.source_trade_id, trade.id)
        self.assertEqual(entry.liquidity_decision_id, liquidity_decision.id)
        self.assertEqual(entry.symbol, "BTCUSD")
        self.assertEqual(entry.simulated_pnl, -trade.profit_loss)
        self.assertEqual(
            entry.meta,
            {"trader_pnl": float(trade.profit_loss), "close_reason": "admin_force_close"},
        )
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 2. Sin LiquidityDecision ─────────────────────────────────────────
    def test_without_liquidity_decision_creates_zero_entries_close_completes(self):
        pos, _routing_decision = _make_position_with_routing_decision(self.account)
        # No LiquidityDecision created for this routing_decision.

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertTrue(Trade.objects.filter(account=self.account).exists())
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 3. routing_decision_id=None ──────────────────────────────────────
    def test_no_routing_decision_skips_lookup_entirely(self):
        pos = Position.objects.create(
            account=self.account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100.00"),
            # routing_decision left unset (None) — legacy position.
        )

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
        ) as mocked_filter:
            resp = self._force_close(symbol="BTCUSD", price="110")
            mocked_filter.assert_not_called()

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 4. Breakeven ──────────────────────────────────────────────────────
    def test_breakeven_persists_clean_zero_not_negative_zero(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        # price == avg_price → pnl == 0.0 exactly.
        resp = self._force_close(symbol="BTCUSD", price="100.00")
        self.assertEqual(resp.status_code, 302)

        entry = LiquidityLedger.objects.get()
        self.assertEqual(entry.simulated_pnl, Decimal("0.00"))
        self.assertEqual(str(entry.simulated_pnl), "0.00")

    # ── 5. Writer devuelve None ──────────────────────────────────────────
    def test_writer_returns_none_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.liquidity_ledger.record_liquidity_ledger_entry", return_value=None):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=self.account)
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 6. Writer lanza excepción inesperada ────────────────────────────
    def test_writer_raises_unexpected_exception_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.liquidity_ledger.record_liquidity_ledger_entry",
            side_effect=RuntimeError("simulated unexpected failure"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=self.account)
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 7. Lookup lanza DatabaseError ────────────────────────────────────
    def test_lookup_database_error_is_contained_close_completes_normally(self):
        """The critical savepoint guarantee: a DatabaseError during the
        LiquidityDecision lookup must never leave the outer db_tx.atomic()
        (shared by the whole queryset loop) unusable — if it did,
        pos.delete() and the balance update below would raise
        TransactionManagementError instead of completing."""
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
            side_effect=DatabaseError("simulated database error"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Trade.objects.filter(account=self.account).exists())
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 8. No modifica RoutingDecision ni LiquidityDecision ─────────────
    def test_never_modifies_routing_decision_or_liquidity_decision(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )
        rd_before = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        ld_before = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}

        self._force_close(symbol="BTCUSD", price="110")

        routing_decision.refresh_from_db()
        liquidity_decision.refresh_from_db()
        rd_after = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        ld_after = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}

        self.assertEqual(rd_before, rd_after)
        self.assertEqual(ld_before, ld_after)

    # ── 9. No modifica BrokerLedger fuera de la entrada normal ──────────
    def test_broker_ledger_unaffected_beyond_normal_close_entry(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._force_close(symbol="BTCUSD", price="110")

        trade = Trade.objects.get(account=self.account)
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        entry = BrokerLedger.objects.get(source_trade=trade)
        self.assertEqual(entry.revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)

    # ── 10. Queryset múltiple con fallo aislado en una sola posición ────
    def test_multi_position_queryset_isolated_lookup_failure(self):
        pos_a, rd_a = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        pos_b, rd_b = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        pos_c, rd_c = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_a)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_b)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_c)

        failing_rd_id = rd_b.id
        real_filter = LiquidityDecision.objects.filter

        def _flaky_filter(*, routing_decision_id):
            if routing_decision_id == failing_rd_id:
                raise DatabaseError("simulated database error")
            return real_filter(routing_decision_id=routing_decision_id)

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
            side_effect=_flaky_filter,
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")

        self.assertEqual(resp.status_code, 302)
        # All three positions close regardless of the isolated lookup failure.
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 3)
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)
        # Only the two unaffected positions get a LiquidityLedger entry.
        self.assertEqual(LiquidityLedger.objects.count(), 2)
        entry_rd_ids = set(LiquidityLedger.objects.values_list("liquidity_decision__routing_decision_id", flat=True))
        self.assertEqual(entry_rd_ids, {rd_a.id, rd_c.id})

    # ── 11. Mensajes y contadores inalterados ───────────────────────────
    def test_messages_and_counters_unaffected_by_liquidity_failure(self):
        pos_a, rd_a = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        pos_b, rd_b = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_a)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_b)

        with patch(
            "simulator.liquidity_ledger.record_liquidity_ledger_entry",
            side_effect=RuntimeError("simulated unexpected failure"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")
        msgs_with_failure = [str(m) for m in get_messages(resp.wsgi_request)]

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50020.00"))
        self.assertEqual(len(msgs_with_failure), 1)
        self.assertIn("Cerradas 2 posición(es)", msgs_with_failure[0])
        self.assertIn("+20.00", msgs_with_failure[0])

        # Now the identical scenario with no liquidity failure at all —
        # the message/counters must be byte-for-byte the same. A fresh
        # Client (fresh cookie jar) avoids the prior request's already-sent
        # messages cookie carrying over — a test-harness artifact of
        # Django's messages framework, unrelated to the code under test.
        pos_c, rd_c = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        pos_d, rd_d = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_c)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_d)

        fresh_client = Client()
        fresh_client.force_login(self.superuser)
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[self.account.id])
        resp2 = fresh_client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        msgs_clean = [str(m) for m in get_messages(resp2.wsgi_request)]

        self.assertEqual(len(msgs_clean), 1)
        self.assertIn("Cerradas 2 posición(es)", msgs_clean[0])
        self.assertIn("+20.00", msgs_clean[0])

    # ── 12. Respeto del filtro symbol ────────────────────────────────────
    def test_symbol_filter_respected_only_matching_position_processed(self):
        pos_btc, rd_btc = _make_position_with_routing_decision(self.account, symbol="BTCUSD")
        pos_eth, rd_eth = _make_position_with_routing_decision(self.account, symbol="ETHUSD")
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=rd_btc)
        LiquidityDecision.objects.create(symbol="ETHUSD", routing_decision=rd_eth)

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        # Only the BTCUSD position was closed and only it produced an entry.
        self.assertFalse(Position.objects.filter(pk=pos_btc.pk).exists())
        self.assertTrue(Position.objects.filter(pk=pos_eth.pk).exists())
        self.assertEqual(LiquidityLedger.objects.count(), 1)
        entry = LiquidityLedger.objects.get()
        self.assertEqual(entry.liquidity_decision_id, LiquidityDecision.objects.get(routing_decision=rd_btc).id)

    # ── 13. Usuario sin permisos ─────────────────────────────────────────
    def test_non_superuser_denied_before_any_liquidity_code_runs(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        staff_user = make_user(username="book05d3c_staff", is_staff=True, is_superuser=False)
        client = Client()
        client.force_login(staff_user)
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[self.account.id])

        resp = client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        self.assertEqual(resp.status_code, 302)

        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertFalse(Trade.objects.filter(account=self.account).exists())
        self.assertEqual(LiquidityLedger.objects.count(), 0)
