"""
BOOK-05e.3c — Liquidity Engine Audit Trail: close-time integration in
admin.py::TradingAccountAdmin.dealing_desk_view (action="force_close").

Covers the single integration point authorized for this block: a new,
independent block placed immediately after BOOK-05d.3c's own per-
iteration nested savepoint has already closed, emitting
EV_LIQUIDITY_LEDGER_RECORDED under Category.LIQUIDITY exactly when
record_liquidity_ledger_entry() (BOOK-05d.2, untouched) returns a
non-None instance, once per position inside the force_close queryset
loop. Replicates the exact semantic contract already approved and
implemented in BOOK-05e.3a/3b. Never touches consumers.py or tasks.py.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse

from simulator.broker_audit import ActorType, Category, EV_LIQUIDITY_LEDGER_RECORDED
from simulator.models import (
    BrokerAuditEvent, BrokerLedger, LiquidityDecision, LiquidityLedger, Position, RoutingDecision, Trade,
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


class LiquidityLedgerAuditForceCloseIntegrationTests(TestCase):

    def setUp(self):
        self.account = make_account(balance=Decimal("50000"))
        self.superuser = make_user(
            username="book05e3c_admin", is_staff=True, is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.superuser)

    def _force_close(self, symbol=None, price="110"):
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[self.account.id])
        data = {"action": "force_close", "price": price}
        if symbol is not None:
            data["symbol"] = symbol
        return self.client.post(url, data)

    # ── 1. Éxito ─────────────────────────────────────────────────────────
    def test_success_creates_exactly_one_event(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        trade = Trade.objects.get(account=self.account)
        entry = LiquidityLedger.objects.get()

        events = BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.event_type, EV_LIQUIDITY_LEDGER_RECORDED)
        self.assertEqual(event.trade_id, trade.id)
        self.assertEqual(event.metadata["liquidity_ledger_id"], entry.id)
        self.assertEqual(event.metadata["liquidity_decision_id"], str(liquidity_decision.decision_id))
        self.assertEqual(event.metadata["routing_decision_id"], str(routing_decision.decision_id))
        self.assertEqual(event.metadata["position_id"], pos.id)
        self.assertEqual(event.metadata["close_reason"], "admin_force_close")

    # ── 2. routing_decision_id=None ──────────────────────────────────────
    def test_no_routing_decision_creates_zero_events(self):
        Position.objects.create(
            account=self.account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100.00"),
        )

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 3. Sin LiquidityDecision ─────────────────────────────────────────
    def test_without_liquidity_decision_creates_zero_events(self):
        _make_position_with_routing_decision(self.account)

        resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 4. Writer devuelve None ──────────────────────────────────────────
    def test_writer_returns_none_creates_zero_events(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.liquidity_ledger.record_liquidity_ledger_entry", return_value=None):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 5. Writer falla ──────────────────────────────────────────────────
    def test_writer_raises_creates_zero_events_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.liquidity_ledger.record_liquidity_ledger_entry",
            side_effect=RuntimeError("simulated unexpected failure"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertTrue(Trade.objects.filter(account=self.account).exists())
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 6. record_liquidity_event() devuelve None ───────────────────────
    def test_audit_event_returns_none_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.broker_audit.record_liquidity_event", return_value=None):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 7. record_liquidity_event() lanza inesperadamente ───────────────
    def test_audit_event_raises_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.broker_audit.record_liquidity_event",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 8. Lookup lanza DatabaseError ────────────────────────────────────
    def test_lookup_database_error_creates_zero_events_close_completes_normally(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
            side_effect=DatabaseError("simulated database error"),
        ):
            resp = self._force_close(symbol="BTCUSD", price="110")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 9. Metadata exacta ───────────────────────────────────────────────
    def test_metadata_whitelist_exact_five_keys(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._force_close(symbol="BTCUSD", price="110")

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertEqual(
            set(event.metadata.keys()),
            {
                "liquidity_ledger_id", "liquidity_decision_id",
                "routing_decision_id", "position_id", "close_reason",
            },
        )

    # ── 10. UUIDs correctos ───────────────────────────────────────────────
    def test_uuids_are_strings_matching_real_decision_ids(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        self._force_close(symbol="BTCUSD", price="110")

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertIsInstance(event.metadata["liquidity_decision_id"], str)
        self.assertIsInstance(event.metadata["routing_decision_id"], str)
        self.assertEqual(event.metadata["liquidity_decision_id"], str(liquidity_decision.decision_id))
        self.assertEqual(event.metadata["routing_decision_id"], str(routing_decision.decision_id))

    # ── 11. actor_type correcto ──────────────────────────────────────────
    def test_actor_type_is_system(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._force_close(symbol="BTCUSD", price="110")

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)

    # ── 12. LiquidityDecision/LiquidityLedger no modificados ────────────
    def test_never_modifies_liquidity_decision_or_liquidity_ledger(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        self._force_close(symbol="BTCUSD", price="110")

        entry = LiquidityLedger.objects.get()
        ld_before = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}
        le_before = {f.name: getattr(entry, f.name) for f in LiquidityLedger._meta.fields}

        liquidity_decision.refresh_from_db()
        entry.refresh_from_db()
        ld_after = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}
        le_after = {f.name: getattr(entry, f.name) for f in LiquidityLedger._meta.fields}

        self.assertEqual(ld_before, ld_after)
        self.assertEqual(le_before, le_after)

    # ── 13. BrokerLedger sin escrituras adicionales ─────────────────────
    def test_broker_ledger_unaffected_beyond_normal_close_entry(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._force_close(symbol="BTCUSD", price="110")

        trade = Trade.objects.get(account=self.account)
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)

    # ── 14. No se crean eventos LIQUIDITY duplicados ────────────────────
    def test_no_extra_liquidity_events(self):
        pos, routing_decision = _make_position_with_routing_decision(self.account)
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._force_close(symbol="BTCUSD", price="110")

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 1)

    # ── 15. Multi-posición con fallo aislado en una sola posición ───────
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
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 3)
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)
        # Only the two unaffected positions get a LIQUIDITY audit event.
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 2)
