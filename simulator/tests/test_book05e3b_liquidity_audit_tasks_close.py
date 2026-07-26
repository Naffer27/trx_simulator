"""
BOOK-05e.3b — Liquidity Engine Audit Trail: close-time integration in
tasks._close_position_sync().

Covers the single integration point authorized for this block: a new,
independent block placed immediately after BOOK-05d.3b's own nested
savepoint has already closed, emitting EV_LIQUIDITY_LEDGER_RECORDED
under Category.LIQUIDITY exactly when record_liquidity_ledger_entry()
(BOOK-05d.2, untouched) returns a non-None instance. Replicates the
exact semantic contract already approved and implemented in
BOOK-05e.3a (TradingConsumer._db_close_position_atomic). Never touches
consumers.py or admin.py (BOOK-05e.3c, not started).
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from simulator.broker_audit import ActorType, Category, EV_LIQUIDITY_LEDGER_RECORDED
from simulator.models import (
    BrokerAuditEvent, BrokerLedger, LiquidityDecision, LiquidityLedger, Position, RoutingDecision, Trade,
)
from simulator.routing_engine import Book
from simulator.tasks import _close_position_sync

from .factories import make_account


def _pos_mem(pos) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


class LiquidityLedgerAuditTasksIntegrationTests(TestCase):

    def setUp(self):
        self.account = make_account(balance=Decimal("50000"))

    def _make_position_with_routing_decision(self, symbol="BTCUSD"):
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=self.account,
        )
        pos = Position.objects.create(
            account=self.account, symbol=symbol, side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100.00"),
            routing_decision=routing_decision,
        )
        return pos, routing_decision

    def _close(self, pos, close_px=110.0, realized_pnl=10.0, reason="daemon_stopout"):
        return _close_position_sync(
            _pos_mem(pos), self.account.pk, close_px, reason,
            realized_pnl, float(self.account.balance) + realized_pnl,
            float(self.account.balance) + realized_pnl,
        )

    # ── 1. Éxito ─────────────────────────────────────────────────────────
    def test_success_creates_exactly_one_event(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        result = self._close(pos, realized_pnl=10.0)

        trade = Trade.objects.get(pk=result["trade_id"])
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
        self.assertEqual(event.metadata["close_reason"], "daemon_stopout")

    # ── 2. routing_decision_id=None ──────────────────────────────────────
    def test_no_routing_decision_creates_zero_events(self):
        pos = Position.objects.create(
            account=self.account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100.00"),
        )

        self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 3. Sin LiquidityDecision ─────────────────────────────────────────
    def test_without_liquidity_decision_creates_zero_events(self):
        pos, _routing_decision = self._make_position_with_routing_decision()

        self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 4. Writer devuelve None ──────────────────────────────────────────
    def test_writer_returns_none_creates_zero_events(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.liquidity_ledger.record_liquidity_ledger_entry", return_value=None):
            self._close(pos, realized_pnl=10.0)

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 5. Writer falla ──────────────────────────────────────────────────
    def test_writer_raises_creates_zero_events_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.liquidity_ledger.record_liquidity_ledger_entry",
            side_effect=RuntimeError("simulated unexpected failure"),
        ):
            result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 6. record_liquidity_event() devuelve None ───────────────────────
    def test_audit_event_returns_none_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.broker_audit.record_liquidity_event", return_value=None):
            result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 7. record_liquidity_event() lanza inesperadamente ───────────────
    def test_audit_event_raises_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.broker_audit.record_liquidity_event",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 8. Lookup lanza DatabaseError ────────────────────────────────────
    def test_lookup_database_error_creates_zero_events_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
            side_effect=DatabaseError("simulated database error"),
        ):
            result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 9. Metadata exacta ───────────────────────────────────────────────
    def test_metadata_whitelist_exact_five_keys(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._close(pos, realized_pnl=10.0)

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
        pos, routing_decision = self._make_position_with_routing_decision()
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        self._close(pos, realized_pnl=10.0)

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertIsInstance(event.metadata["liquidity_decision_id"], str)
        self.assertIsInstance(event.metadata["routing_decision_id"], str)
        self.assertEqual(event.metadata["liquidity_decision_id"], str(liquidity_decision.decision_id))
        self.assertEqual(event.metadata["routing_decision_id"], str(routing_decision.decision_id))

    # ── 11. actor_type correcto ──────────────────────────────────────────
    def test_actor_type_is_system(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._close(pos, realized_pnl=10.0)

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)

    # ── 12. LiquidityDecision/LiquidityLedger no modificados ────────────
    def test_never_modifies_liquidity_decision_or_liquidity_ledger(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        self._close(pos, realized_pnl=10.0)

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
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        result = self._close(pos, realized_pnl=10.0)

        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)

    # ── 14. No se crean eventos LIQUIDITY duplicados ────────────────────
    def test_no_extra_liquidity_events(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._close(pos, realized_pnl=10.0)

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 1)
