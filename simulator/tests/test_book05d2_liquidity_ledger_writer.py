"""
BOOK-05d.2 — Liquidity Ledger writer tests.

Covers record_liquidity_ledger_entry() (simulator/liquidity_ledger.py)
in complete isolation. No test here touches consumers.py, tasks.py, or
admin.py — this writer has no real caller yet; that integration is
BOOK-05d.3a/b/c, out of scope for this block.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from simulator.liquidity_ledger import record_liquidity_ledger_entry
from simulator.models import (
    BrokerLedger,
    LiquidityDecision,
    LiquidityLedger,
    LiquidityProvider,
    RoutingDecision,
    Trade,
)
from simulator.routing_engine import Book

from .factories import make_account


def _make_trade(account, symbol="BTCUSD", profit_loss=Decimal("5.00")):
    return Trade.objects.create(
        account=account, symbol=symbol, trade_type="BUY",
        lot_size=Decimal("0.1"), entry_price=Decimal("100.00"),
        exit_price=Decimal("100.50"), profit_loss=profit_loss,
    )


class RecordLiquidityLedgerEntryWriterTests(TestCase):

    def setUp(self):
        self.account = make_account(balance=Decimal("100000"))
        self.trade = _make_trade(self.account)
        self.routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=self.account,
        )
        self.liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=self.routing_decision,
        )

    # ── 1. Crea una fila con todos los campos ──────────────────────────
    def test_creates_row_with_all_fields(self):
        entry = record_liquidity_ledger_entry(
            source_trade_id=self.trade.id,
            symbol="BTCUSD",
            simulated_pnl=Decimal("-5.00"),
            liquidity_decision_id=self.liquidity_decision.id,
            meta={"trader_pnl": 5.00, "close_reason": "manual"},
        )
        self.assertIsNotNone(entry)
        self.assertEqual(LiquidityLedger.objects.count(), 1)
        self.assertEqual(entry.source_trade_id, self.trade.id)
        self.assertEqual(entry.liquidity_decision_id, self.liquidity_decision.id)
        self.assertEqual(entry.symbol, "BTCUSD")
        self.assertEqual(entry.simulated_pnl, Decimal("-5.00"))
        self.assertEqual(entry.meta, {"trader_pnl": 5.00, "close_reason": "manual"})

    # ── 2. Crea una fila con parámetros mínimos ────────────────────────
    def test_creates_row_with_minimal_fields(self):
        entry = record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("0.00"),
        )
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.liquidity_decision_id)
        self.assertEqual(entry.meta, {})

    # ── 3. meta=None persiste {} ────────────────────────────────────────
    def test_meta_none_persists_empty_dict(self):
        entry = record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD",
            simulated_pnl=Decimal("1.00"), meta=None,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.meta, {})

    # ── 4. Dos llamadas con el mismo Trade crean dos filas ─────────────
    def test_two_calls_same_trade_create_two_rows(self):
        kwargs = dict(source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"))
        record_liquidity_ledger_entry(**kwargs)
        record_liquidity_ledger_entry(**kwargs)
        self.assertEqual(LiquidityLedger.objects.filter(source_trade_id=self.trade.id).count(), 2)

    # ── 5. Fallo de escritura devuelve None ─────────────────────────────
    def test_fail_open_returns_none_on_write_failure(self):
        with patch(
            "simulator.models.LiquidityLedger.objects.create",
            side_effect=Exception("simulated DB failure"),
        ):
            result = record_liquidity_ledger_entry(
                source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
            )
        self.assertIsNone(result)
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 6. Fallo de escritura nunca propaga ─────────────────────────────
    def test_fail_open_never_raises(self):
        with patch(
            "simulator.models.LiquidityLedger.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            try:
                record_liquidity_ledger_entry(
                    source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
                )
            except Exception as exc:   # pragma: no cover - fails via assertion below
                self.fail(f"record_liquidity_ledger_entry() raised unexpectedly: {exc!r}")

    # ── 7. IntegrityError es fail-open ──────────────────────────────────
    def test_integrity_error_is_fail_open(self):
        """Does not rely on SQLite's own FK-check timing — the failure
        is injected directly as the exact exception class an invalid
        FK would realistically raise."""
        with patch(
            "simulator.models.LiquidityLedger.objects.create",
            side_effect=IntegrityError("simulated FK violation"),
        ):
            result = record_liquidity_ledger_entry(
                source_trade_id=999999, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
            )
        self.assertIsNone(result)
        self.assertEqual(LiquidityLedger.objects.count(), 0)

    # ── 8. La transacción exterior sigue utilizable tras el fallo ──────
    def test_outer_transaction_remains_usable_after_failure(self):
        """
        Critical savepoint recovery test — the real fail-open guarantee.
        A failure inside record_liquidity_ledger_entry() must never
        poison a live outer transaction.atomic() block the caller may
        already be inside (the exact situation this writer will face
        once integrated in BOOK-05d.3, running inside the same
        transaction as a real close).
        """
        with transaction.atomic():
            before = LiquidityProvider.objects.create(name="LP-Before")

            with patch(
                "simulator.models.LiquidityLedger.objects.create",
                side_effect=IntegrityError("simulated FK violation"),
            ):
                # No try/except here on purpose: if the writer let this
                # exception propagate, it would abort this `with
                # transaction.atomic():` block right here and the test
                # would ERROR (not just fail an assertion).
                result = record_liquidity_ledger_entry(
                    source_trade_id=999999, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
                )

            self.assertIsNone(result)

            # If the writer's internal savepoint had left this outer
            # transaction.atomic() marked as needing a rollback, ANY
            # further ORM write here would raise
            # TransactionManagementError instead of succeeding.
            after = LiquidityProvider.objects.create(name="LP-After")

        # The outer `with transaction.atomic():` above committed cleanly
        # (no exception reached this point) — both markers exist.
        self.assertTrue(LiquidityProvider.objects.filter(pk=before.pk).exists())
        self.assertTrue(LiquidityProvider.objects.filter(pk=after.pk).exists())

    # ── 9. simulated_pnl cero es válido ─────────────────────────────────
    def test_simulated_pnl_zero_is_valid(self):
        entry = record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("0.00"),
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.simulated_pnl, Decimal("0.00"))

    # ── 10. Devuelve la instancia creada ────────────────────────────────
    def test_returns_created_instance_on_success(self):
        entry = record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
        )
        self.assertIsInstance(entry, LiquidityLedger)
        self.assertIsNotNone(entry.pk)
        self.assertEqual(LiquidityLedger.objects.get(pk=entry.pk), entry)

    # ── 11. source_trade_id es obligatorio ──────────────────────────────
    def test_source_trade_id_is_mandatory(self):
        with self.assertRaises(TypeError):
            record_liquidity_ledger_entry(symbol="BTCUSD", simulated_pnl=Decimal("1.00"))

    # ── 12. No modifica Trade ───────────────────────────────────────────
    def test_never_modifies_trade(self):
        before = {f.name: getattr(self.trade, f.name) for f in Trade._meta.fields}
        record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
        )
        self.trade.refresh_from_db()
        after = {f.name: getattr(self.trade, f.name) for f in Trade._meta.fields}
        self.assertEqual(before, after)

    # ── 13. No modifica LiquidityDecision ───────────────────────────────
    def test_never_modifies_liquidity_decision(self):
        before = {f.name: getattr(self.liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}
        record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
            liquidity_decision_id=self.liquidity_decision.id,
        )
        self.liquidity_decision.refresh_from_db()
        after = {f.name: getattr(self.liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}
        self.assertEqual(before, after)

    # ── 14. No escribe BrokerLedger ──────────────────────────────────────
    def test_never_writes_to_broker_ledger(self):
        before_count = BrokerLedger.objects.count()
        record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
        )
        self.assertEqual(BrokerLedger.objects.count(), before_count)

    # ── 15. No modifica balances ────────────────────────────────────────
    def test_never_modifies_account_balance(self):
        before_balance = self.account.balance
        before_equity = self.account.equity
        record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, before_balance)
        self.assertEqual(self.account.equity, before_equity)

    # ── 16. No elimina registros ─────────────────────────────────────────
    def test_never_deletes_records(self):
        trade_count = Trade.objects.count()
        decision_count = LiquidityDecision.objects.count()
        ledger_count = BrokerLedger.objects.count()
        record_liquidity_ledger_entry(
            source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
        )
        self.assertEqual(Trade.objects.count(), trade_count)
        self.assertEqual(LiquidityDecision.objects.count(), decision_count)
        self.assertEqual(BrokerLedger.objects.count(), ledger_count)

    # ── 17. Solo existe un INSERT en LiquidityLedger en éxito ──────────
    def test_exactly_one_insert_on_liquidity_ledger_table(self):
        with CaptureQueriesContext(connection) as ctx:
            record_liquidity_ledger_entry(
                source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
            )
        insert_queries = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].strip().upper().startswith("INSERT")
            and "simulator_liquidityledger" in q["sql"].lower()
        ]
        self.assertEqual(len(insert_queries), 1)

    # ── 18. No realiza escrituras en otras tablas ──────────────────────
    def test_no_writes_to_other_tables(self):
        with CaptureQueriesContext(connection) as ctx:
            record_liquidity_ledger_entry(
                source_trade_id=self.trade.id, symbol="BTCUSD", simulated_pnl=Decimal("1.00"),
                liquidity_decision_id=self.liquidity_decision.id,
            )
        write_verbs = ("INSERT", "UPDATE", "DELETE")
        other_tables = ("simulator_trade", "simulator_liquiditydecision",
                        "simulator_brokerledger", "simulator_tradingaccount")
        offending = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].strip().upper().startswith(write_verbs)
            and any(t in q["sql"].lower() for t in other_tables)
        ]
        self.assertEqual(offending, [])
