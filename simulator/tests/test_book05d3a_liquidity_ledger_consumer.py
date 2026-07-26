"""
BOOK-05d.3a — Liquidity Ledger integration into
TradingConsumer._db_close_position_atomic().

Covers the single new integration point authorized for this block: the
nested-savepoint lookup of LiquidityDecision plus the call to
record_liquidity_ledger_entry() (BOOK-05d.2, untouched), placed
immediately after create_broker_counterparty_entry() and before the
negative balance guard / pos.delete(). No test here touches tasks.py or
admin.py — that integration is BOOK-05d.3b/3c, out of scope here.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LiquidityDecision, LiquidityLedger, Position, RoutingDecision, Trade
from simulator.routing_engine import Book

from .factories import make_account

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


class _FakeConsumer:
    """Same minimal stub used by test_pricing_context_persistence.py."""
    def __init__(self, account_id):
        self._db_account_id = account_id
        self.account = {"currency": "USD"}


def _pos_mem(pos) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


class LiquidityLedgerCloseIntegrationTests(TestCase):

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

    def _close(self, pos, close_px=110.0, realized_pnl=10.0, reason="manual"):
        return _db_close_sync(
            _FakeConsumer(self.account.pk), _pos_mem(pos), close_px, reason,
            realized_pnl, float(self.account.balance) + realized_pnl,
            float(self.account.balance) + realized_pnl,
        )

    # ── 1. Con LiquidityDecision previa ─────────────────────────────────
    def test_with_liquidity_decision_creates_exactly_one_entry(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )

        result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 1)
        entry = LiquidityLedger.objects.get()
        trade = Trade.objects.get(pk=result["trade_id"])

        self.assertEqual(entry.source_trade_id, trade.id)
        self.assertEqual(entry.liquidity_decision_id, liquidity_decision.id)
        self.assertEqual(entry.symbol, "BTCUSD")
        self.assertEqual(entry.simulated_pnl, Decimal("-10.00"))
        self.assertEqual(entry.meta, {"trader_pnl": 10.0, "close_reason": "manual"})

    # ── 2. Sin LiquidityDecision ─────────────────────────────────────────
    def test_without_liquidity_decision_creates_zero_entries_close_completes(self):
        pos, _routing_decision = self._make_position_with_routing_decision()
        # No LiquidityDecision created for this routing_decision.

        result = self._close(pos, realized_pnl=10.0)

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    # ── 3. routing_decision_id=None ──────────────────────────────────────
    def test_no_routing_decision_skips_lookup_entirely(self):
        pos = Position.objects.create(
            account=self.account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100.00"),
            # routing_decision left unset (None) — pre-BOOK-04b-equivalent position.
        )

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
        ) as mocked_filter:
            result = self._close(pos, realized_pnl=10.0)
            mocked_filter.assert_not_called()

        self.assertEqual(LiquidityLedger.objects.count(), 0)
        self.assertIsNotNone(result["trade_id"])

    # ── 4. Breakeven ──────────────────────────────────────────────────────
    def test_breakeven_persists_clean_zero_not_negative_zero(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        self._close(pos, close_px=100.0, realized_pnl=0.0)

        entry = LiquidityLedger.objects.get()
        self.assertEqual(entry.simulated_pnl, Decimal("0.00"))
        self.assertEqual(str(entry.simulated_pnl), "0.00")

    # ── 5. Writer devuelve None ──────────────────────────────────────────
    def test_writer_returns_none_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch("simulator.liquidity_ledger.record_liquidity_ledger_entry", return_value=None):
            result = self._close(pos, realized_pnl=10.0)

        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))

    # ── 6. Writer lanza excepción inesperada ────────────────────────────
    def test_writer_raises_unexpected_exception_close_completes_normally(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.liquidity_ledger.record_liquidity_ledger_entry",
            side_effect=RuntimeError("simulated unexpected failure"),
        ):
            result = self._close(pos, realized_pnl=10.0)

        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 7. Lookup lanza DatabaseError ────────────────────────────────────
    def test_lookup_database_error_is_contained_close_completes_normally(self):
        """The critical savepoint guarantee: a DatabaseError during the
        LiquidityDecision lookup must never leave this method's own
        outer transaction.atomic() unusable — if it did, pos.delete()
        and the balance update below would raise
        TransactionManagementError instead of completing."""
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        with patch(
            "simulator.models.LiquidityDecision.objects.filter",
            side_effect=DatabaseError("simulated database error"),
        ):
            # No try/except here on purpose: if the call site let this
            # exception (or a subsequent TransactionManagementError)
            # propagate, this line itself would raise and the test would
            # ERROR — the call completing at all is itself the proof.
            result = self._close(pos, realized_pnl=10.0)

        self.assertIsNotNone(result["trade_id"])
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal("50010.00"))
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 8. No modifica RoutingDecision ni LiquidityDecision ─────────────
    def test_never_modifies_routing_decision_or_liquidity_decision(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )
        rd_before = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        ld_before = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}

        self._close(pos, realized_pnl=10.0)

        routing_decision.refresh_from_db()
        liquidity_decision.refresh_from_db()
        rd_after = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        ld_after = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}

        self.assertEqual(rd_before, rd_after)
        self.assertEqual(ld_before, ld_after)

    # ── 9. No modifica BrokerLedger fuera de la entrada normal ──────────
    def test_broker_ledger_unaffected_beyond_normal_close_entry(self):
        pos, routing_decision = self._make_position_with_routing_decision()
        LiquidityDecision.objects.create(symbol="BTCUSD", routing_decision=routing_decision)

        result = self._close(pos, realized_pnl=10.0)

        trade = Trade.objects.get(pk=result["trade_id"])
        # Exactly the one COUNTERPARTY_PNL row create_broker_counterparty_entry()
        # already produces — nothing more, nothing from the liquidity block.
        self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)
        entry = BrokerLedger.objects.get(source_trade=trade)
        self.assertEqual(entry.revenue_type, BrokerLedger.REV_COUNTERPARTY_PNL)
