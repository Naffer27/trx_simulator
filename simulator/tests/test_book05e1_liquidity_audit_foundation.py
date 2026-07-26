"""
BOOK-05e.1 — Liquidity Engine Audit Trail foundation.

Covers exactly what this block adds: Category.LIQUIDITY,
EV_LIQUIDITY_DECISION_RECORDED, EV_LIQUIDITY_LEDGER_RECORDED, and
record_liquidity_event() — in complete isolation. No call site exists
yet (that is BOOK-05e.2/3a/3b/3c) — no test here touches consumers.py,
tasks.py, or admin.py.
"""
from decimal import Decimal

from django.db import DatabaseError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from unittest.mock import patch

from simulator import broker_audit as _audit
from simulator.broker_audit import (
    Category, EV_LIQUIDITY_DECISION_RECORDED, EV_LIQUIDITY_LEDGER_RECORDED,
    record_liquidity_event,
)
from simulator.models import BrokerAuditEvent

from .factories import make_account, make_trade


# ─────────────────────────────────────────────────────────────────────────
# 1. Category / constants
# ─────────────────────────────────────────────────────────────────────────
class LiquidityCategoryAndConstantsTests(TestCase):

    def test_liquidity_category_defined_and_distinct_from_routing_and_trading(self):
        self.assertEqual(Category.LIQUIDITY, "LIQUIDITY")
        self.assertNotEqual(Category.LIQUIDITY, Category.ROUTING)
        self.assertNotEqual(Category.LIQUIDITY, Category.TRADING)
        self.assertIn(Category.LIQUIDITY, Category.ALL)

    def test_ev_liquidity_decision_recorded_defined(self):
        self.assertEqual(EV_LIQUIDITY_DECISION_RECORDED, "liquidity.decision_recorded")

    def test_ev_liquidity_ledger_recorded_defined(self):
        self.assertEqual(EV_LIQUIDITY_LEDGER_RECORDED, "liquidity.ledger_recorded")

    def test_two_liquidity_event_types_are_distinct(self):
        self.assertNotEqual(EV_LIQUIDITY_DECISION_RECORDED, EV_LIQUIDITY_LEDGER_RECORDED)


# ─────────────────────────────────────────────────────────────────────────
# 2. record_liquidity_event() — the wrapper, in isolation
# ─────────────────────────────────────────────────────────────────────────
class RecordLiquidityEventUnitTests(TestCase):

    def test_creates_event_under_liquidity_category_account_variant(self):
        account = make_account(balance=Decimal("100000"))
        event = record_liquidity_event(
            event_type=EV_LIQUIDITY_DECISION_RECORDED,
            description="test",
            account_id=account.id, symbol="BTCUSD",
            source_module="simulator.consumers",
            metadata={"liquidity_decision_id": 1, "routing_decision_id": "x"},
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.category, Category.LIQUIDITY)
        self.assertEqual(event.event_type, EV_LIQUIDITY_DECISION_RECORDED)
        self.assertEqual(event.account_id, account.id)

    def test_creates_event_under_liquidity_category_trade_variant(self):
        account = make_account(balance=Decimal("100000"))
        trade = make_trade(account, profit_loss=Decimal("10.00"))
        event = record_liquidity_event(
            event_type=EV_LIQUIDITY_LEDGER_RECORDED,
            description="test",
            trade_id=trade.id, symbol="BTCUSD",
            metadata={"liquidity_ledger_id": 1},
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.category, Category.LIQUIDITY)
        self.assertEqual(event.event_type, EV_LIQUIDITY_LEDGER_RECORDED)
        self.assertEqual(event.trade_id, trade.id)

    def test_default_actor_type_is_system(self):
        account = make_account(balance=Decimal("100000"))
        event = record_liquidity_event(
            event_type=EV_LIQUIDITY_DECISION_RECORDED, description="test",
            account_id=account.id, symbol="BTCUSD",
            metadata={"liquidity_decision_id": 1},
        )
        self.assertEqual(event.actor_type, _audit.ActorType.SYSTEM)

    def test_account_and_trade_objects_accepted_not_just_ids(self):
        account = make_account(balance=Decimal("100000"))
        trade = make_trade(account, profit_loss=Decimal("5.00"))
        event = record_liquidity_event(
            event_type=EV_LIQUIDITY_LEDGER_RECORDED, description="test",
            account=account, trade=trade, symbol="BTCUSD",
            metadata={"liquidity_ledger_id": 2},
        )
        self.assertEqual(event.account_id, account.id)
        self.assertEqual(event.trade_id, trade.id)

    def test_delegates_entirely_to_record_event_no_own_logic(self):
        """record_liquidity_event() must be a thin pass-through — if
        record_event() itself is patched to return a sentinel, the
        wrapper must return exactly that sentinel, proving it adds no
        logic, no transformation, and no independent decision of its own."""
        sentinel = object()
        with patch("simulator.broker_audit.record_event", return_value=sentinel) as mocked:
            result = record_liquidity_event(
                event_type=EV_LIQUIDITY_DECISION_RECORDED, description="test",
                account_id=1, symbol="BTCUSD", metadata={"liquidity_decision_id": 1},
            )
        self.assertIs(result, sentinel)
        mocked.assert_called_once()
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs["category"], Category.LIQUIDITY)
        self.assertEqual(kwargs["event_type"], EV_LIQUIDITY_DECISION_RECORDED)

    def test_fail_open_returns_none_on_write_failure(self):
        with patch(
            "simulator.models.BrokerAuditEvent.objects.create",
            side_effect=DatabaseError("simulated database error"),
        ):
            result = record_liquidity_event(
                event_type=EV_LIQUIDITY_DECISION_RECORDED, description="test",
                account_id=1, symbol="BTCUSD", metadata={"liquidity_decision_id": 1},
            )
        self.assertIsNone(result)

    def test_fail_open_never_raises(self):
        try:
            with patch(
                "simulator.models.BrokerAuditEvent.objects.create",
                side_effect=RuntimeError("simulated unexpected failure"),
            ):
                result = record_liquidity_event(
                    event_type=EV_LIQUIDITY_LEDGER_RECORDED, description="test",
                    trade_id=1, symbol="BTCUSD", metadata={"liquidity_ledger_id": 1},
                )
        except Exception as exc:
            self.fail(f"record_liquidity_event() raised {exc!r} — must never raise")
        self.assertIsNone(result)

    def test_no_database_query_beyond_the_single_insert(self):
        """The wrapper itself issues zero queries of its own — every
        query observed here belongs to record_event()'s single INSERT
        (plus its request_id resolution, which performs no query)."""
        account = make_account(balance=Decimal("100000"))
        with CaptureQueriesContext(connection) as ctx:
            record_liquidity_event(
                event_type=EV_LIQUIDITY_DECISION_RECORDED, description="test",
                account_id=account.id, symbol="BTCUSD",
                metadata={"liquidity_decision_id": 1},
            )
        inserts = [q for q in ctx.captured_queries if "INSERT" in q["sql"].upper()]
        self.assertEqual(len(inserts), 1)
        self.assertIn("simulator_brokerauditevent", inserts[0]["sql"].lower())

    def test_never_modifies_account_or_trade(self):
        account = make_account(balance=Decimal("100000"))
        trade = make_trade(account, profit_loss=Decimal("7.00"))
        acct_before = {f.name: getattr(account, f.name) for f in account._meta.fields}
        trade_before = {f.name: getattr(trade, f.name) for f in trade._meta.fields}

        record_liquidity_event(
            event_type=EV_LIQUIDITY_LEDGER_RECORDED, description="test",
            account=account, trade=trade, symbol="BTCUSD",
            metadata={"liquidity_ledger_id": 3},
        )

        account.refresh_from_db()
        trade.refresh_from_db()
        acct_after = {f.name: getattr(account, f.name) for f in account._meta.fields}
        trade_after = {f.name: getattr(trade, f.name) for f in trade._meta.fields}
        self.assertEqual(acct_before, acct_after)
        self.assertEqual(trade_before, trade_after)

    def test_creates_exactly_one_broker_audit_event_row(self):
        account = make_account(balance=Decimal("100000"))
        before = BrokerAuditEvent.objects.count()

        record_liquidity_event(
            event_type=EV_LIQUIDITY_DECISION_RECORDED, description="test",
            account_id=account.id, symbol="BTCUSD",
            metadata={"liquidity_decision_id": 1},
        )

        self.assertEqual(BrokerAuditEvent.objects.count(), before + 1)
