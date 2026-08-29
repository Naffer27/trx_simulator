"""
BOOK-04d — Routing Engine Audit Trail tests.

Covers the single integration point authorized for this block:
TradingConsumer._order_new(), via the new @database_sync_to_async
helper _db_record_routing_audit_event(). Never touches
_db_open_position_atomic() — that remains BOOK-04b's exclusive scope.

Reuses the established _order_new integration-test infrastructure
(_consumer/_run/_first_error) rather than re-inventing it — the same
pattern test_order_ticket_sl_tp_validation.py already uses to prove
_order_new's real async wiring, TransactionTestCase included (SQLite
visibility across the database_sync_to_async thread requires it — see
that file's own module docstring for the full reasoning).
"""
import time
import uuid
from decimal import Decimal
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from market_data.contracts import OrderPolicy
from market_data.feeds import get_feed_manager
from simulator import broker_audit as _audit
from simulator.broker_audit import Category, EV_ROUTING_DECISION_RECORDED, record_routing_event
from simulator.consumers import TradingConsumer
from simulator.models import BrokerAuditEvent, Position, RoutingDecision, TradingAccount

from .factories import make_account
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run

_db_record_routing_audit_event = TradingConsumer._db_record_routing_audit_event.__wrapped__


def _open_normal_session():
    """TEST-INFRA — market-session policy is real-clock-driven (FIX-05A);
    this test is about routing-audit merge behavior, not market-session
    behavior, so pin it open. Same pattern as test_fix05a_financial_
    price_integrity.py::_open_normal_session()."""
    return patch(
        "market_data.sessions.service.evaluate_market_session_for_symbol",
        return_value=Mock(order_policy=OrderPolicy.OPEN_NORMAL),
    )


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


# ─────────────────────────────────────────────────────────────────────────
# 1. Category / constants
# ─────────────────────────────────────────────────────────────────────────
class RoutingCategoryAndConstantsTests(TestCase):

    def test_routing_category_defined_and_distinct_from_monitoring(self):
        self.assertEqual(Category.ROUTING, "ROUTING")
        self.assertNotEqual(Category.ROUTING, Category.MONITORING)
        self.assertIn(Category.ROUTING, Category.ALL)

    def test_ev_routing_decision_recorded_defined(self):
        self.assertEqual(EV_ROUTING_DECISION_RECORDED, "routing.decision_recorded")


# ─────────────────────────────────────────────────────────────────────────
# 2. record_routing_event() — the wrapper, in isolation
# ─────────────────────────────────────────────────────────────────────────
class RecordRoutingEventUnitTests(TestCase):

    def test_creates_event_under_routing_category(self):
        account = make_account(balance=Decimal("100000"))
        event = record_routing_event(
            event_type=EV_ROUTING_DECISION_RECORDED,
            description="test",
            account_id=account.id, symbol="BTCUSD",
            source_module="simulator.consumers",
            metadata={"routing_decision_id": "x", "position_id": 1, "merged": False},
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.category, Category.ROUTING)
        self.assertEqual(event.event_type, EV_ROUTING_DECISION_RECORDED)

    def test_default_actor_type_is_system(self):
        account = make_account(balance=Decimal("100000"))
        event = record_routing_event(
            event_type=EV_ROUTING_DECISION_RECORDED, description="test",
            account_id=account.id, symbol="BTCUSD",
            metadata={"routing_decision_id": "x", "position_id": 1, "merged": False},
        )
        self.assertEqual(event.actor_type, _audit.ActorType.SYSTEM)


# ─────────────────────────────────────────────────────────────────────────
# 3. _db_record_routing_audit_event() — the sync helper, in isolation
# ─────────────────────────────────────────────────────────────────────────
class DbRecordRoutingAuditEventUnitTests(TestCase):

    def test_metadata_whitelist_exact_three_keys(self):
        account = make_account(balance=Decimal("100000"))
        decision_id = uuid.uuid4()

        _db_record_routing_audit_event(
            None, account_id=account.id, symbol="BTCUSD",
            routing_decision_id=decision_id, position_id=42, merged=True,
        )

        event = BrokerAuditEvent.objects.get(category=Category.ROUTING)
        self.assertEqual(set(event.metadata.keys()), {"routing_decision_id", "position_id", "merged"})

    def test_uuid_stringified_no_serialization_crash(self):
        """RoutingDecision.decision_id is a uuid.UUID — BrokerAuditEvent.metadata
        is a plain JSONField (no DjangoJSONEncoder). Storing the raw UUID
        object would raise; this proves it is stringified first."""
        account = make_account(balance=Decimal("100000"))
        decision_id = uuid.uuid4()

        _db_record_routing_audit_event(
            None, account_id=account.id, symbol="BTCUSD",
            routing_decision_id=decision_id, position_id=1, merged=False,
        )

        event = BrokerAuditEvent.objects.get(category=Category.ROUTING)
        self.assertEqual(event.metadata["routing_decision_id"], str(decision_id))
        self.assertIsInstance(event.metadata["routing_decision_id"], str)

    def test_no_extra_queries_against_routingdecision_or_position(self):
        """The helper must never re-read RoutingDecision or Position —
        it only writes BrokerAuditEvent (plus its own nested audit
        savepoint), using values already handed to it."""
        account = make_account(balance=Decimal("100000"))
        decision_id = uuid.uuid4()

        with CaptureQueriesContext(connection) as ctx:
            _db_record_routing_audit_event(
                None, account_id=account.id, symbol="BTCUSD",
                routing_decision_id=decision_id, position_id=1, merged=False,
            )

        queries = [q["sql"].lower() for q in ctx.captured_queries]
        self.assertFalse(any("simulator_routingdecision" in q for q in queries))
        self.assertFalse(any("simulator_position" in q for q in queries))
        self.assertTrue(any("simulator_brokerauditevent" in q and q.strip().startswith("insert") for q in queries))

    def test_no_inputs_snapshot_or_book_fields_present(self):
        account = make_account(balance=Decimal("100000"))
        _db_record_routing_audit_event(
            None, account_id=account.id, symbol="BTCUSD",
            routing_decision_id=uuid.uuid4(), position_id=1, merged=False,
        )
        event = BrokerAuditEvent.objects.get(category=Category.ROUTING)
        for forbidden in ("inputs_snapshot", "book", "reason_code", "engine_version"):
            self.assertNotIn(forbidden, event.metadata)


# ─────────────────────────────────────────────────────────────────────────
# 4. _order_new() — the real async path (proves no SynchronousOnlyOperation)
# ─────────────────────────────────────────────────────────────────────────
class OrderNewRoutingAuditIntegrationTests(TransactionTestCase):

    def setUp(self):
        _seed_price("BTCUSD", 63000.0)  # O.6c-1w-b: realistic BTCUSD magnitude, Capa A plausibility gate
        _seed_price("EUR/USD", 1.0800)
        self.addCleanup(_clear_price, "BTCUSD")
        self.addCleanup(_clear_price, "EUR/USD")

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_new_position_flag_on_creates_exactly_one_event(self):
        """Runs the REAL async _order_new() end to end. If the
        integration were wired as a direct synchronous call instead of
        via @database_sync_to_async, this would raise
        SynchronousOnlyOperation instead of completing — the test
        passing at all is itself the proof."""
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        self.assertIsNotNone(pos.routing_decision_id)

        events = BrokerAuditEvent.objects.filter(category=Category.ROUTING)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.metadata["routing_decision_id"], str(pos.routing_decision.decision_id))
        self.assertEqual(event.metadata["position_id"], pos.id)
        self.assertFalse(event.metadata["merged"])

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_merge_flag_on_creates_exactly_one_additional_event(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk, netting_mode=True)

        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.1}))
            self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 1)

            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.1}))

        events = BrokerAuditEvent.objects.filter(category=Category.ROUTING).order_by("id")
        self.assertEqual(events.count(), 2)
        self.assertTrue(events.last().metadata["merged"])

    @override_settings(ROUTING_ENGINE_ENABLED=False)
    def test_flag_off_creates_zero_events(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_routing_decision_id_none_creates_zero_events_even_with_flag_on(self):
        """Simulates BOOK-04b's own fail-open (writer/link failure) —
        result["routing_decision_id"] ends up None even with the flag
        active. BOOK-04d must not create an event for a call that
        recorded no decision."""
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        with patch("simulator.routing_engine.record_routing_decision", return_value=None):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_rejected_order_creates_zero_events(self):
        account = make_account(balance=Decimal("100"))   # too small for qty below
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 1000.0}))

        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_audit_failure_does_not_affect_open_or_client_messages(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        with patch(
            "simulator.broker_audit.record_routing_event",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 0)

        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)
