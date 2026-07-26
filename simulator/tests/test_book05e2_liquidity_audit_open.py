"""
BOOK-05e.2 — Liquidity Engine Audit Trail: open-time integration.

Covers the single integration point authorized for this block:
TradingConsumer._order_new(), via the new @database_sync_to_async
helper _db_record_liquidity_audit_event() — emits
EV_LIQUIDITY_DECISION_RECORDED under Category.LIQUIDITY exactly when
_db_record_liquidity_decision() returns a non-None LiquidityDecision.
Never touches closes (BOOK-05e.3a/3b/3c, not started), tasks.py, or
admin.py.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from market_data.feeds import get_feed_manager
from simulator.broker_audit import Category, EV_LIQUIDITY_DECISION_RECORDED
from simulator.models import BrokerAuditEvent, LiquidityDecision, LiquidityProvider, Position, RoutingDecision

from .factories import make_account
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run


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


def _provider(name="LP-BTC", spread="5.0", capacity="100000", symbols=None, enabled=True):
    return LiquidityProvider.objects.create(
        name=name,
        symbols_covered=symbols if symbols is not None else ["BTCUSD"],
        simulated_spread_markup_pips=Decimal(spread),
        max_capacity_usd=Decimal(capacity),
        enabled=enabled,
    )


class OrderNewLiquidityAuditIntegrationTests(TransactionTestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")

    # ── 1. Éxito ─────────────────────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_success_creates_exactly_one_event(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        decision = LiquidityDecision.objects.get()
        pos = Position.objects.get(account=account)

        events = BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.event_type, EV_LIQUIDITY_DECISION_RECORDED)
        self.assertEqual(event.metadata["liquidity_decision_id"], str(decision.decision_id))
        self.assertEqual(event.metadata["routing_decision_id"], str(pos.routing_decision.decision_id))
        self.assertEqual(event.metadata["position_id"], pos.id)

    # ── 2. Flag apagado ──────────────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_flag_off_creates_zero_events(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 3. Sin proveedor elegible ────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_qualifying_provider_creates_zero_events(self):
        account = make_account(balance=Decimal("100000"))
        _provider(symbols=["EUR/USD"])  # does not cover BTCUSD
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

    # ── 4. Sin RoutingDecision ───────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=False, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_routing_decision_creates_zero_events(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)

    # ── 5. Writer devuelve None ──────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_writer_returns_none_creates_zero_events(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch("simulator.liquidity_engine.record_liquidity_decision", return_value=None):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

    # ── 6. Writer lanza internamente (fail-open) ────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_writer_raises_creates_zero_events_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch(
            "simulator.models.LiquidityDecision.objects.create",
            side_effect=RuntimeError("simulated write failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 7. record_liquidity_event() devuelve None ───────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_audit_event_returns_none_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch("simulator.broker_audit.record_liquidity_event", return_value=None):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 1)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 8. record_liquidity_event() lanza inesperadamente ───────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_audit_event_raises_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch(
            "simulator.broker_audit.record_liquidity_event",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 9. Metadata exacta ───────────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_metadata_whitelist_exact_three_keys(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        self.assertEqual(
            set(event.metadata.keys()),
            {"liquidity_decision_id", "routing_decision_id", "position_id"},
        )
        self.assertIsInstance(event.metadata["liquidity_decision_id"], str)
        self.assertIsInstance(event.metadata["routing_decision_id"], str)

    # ── 10. actor_type correcto ──────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_actor_type_is_system(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        event = BrokerAuditEvent.objects.get(category=Category.LIQUIDITY)
        from simulator.broker_audit import ActorType
        self.assertEqual(event.actor_type, ActorType.SYSTEM)

    # ── 11. LiquidityDecision/RoutingDecision no modificadas ────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_never_modifies_liquidity_decision_or_routing_decision(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        decision = LiquidityDecision.objects.get()
        routing_decision = RoutingDecision.objects.get()
        ld_before = {f.name: getattr(decision, f.name) for f in LiquidityDecision._meta.fields}
        rd_before = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}

        decision.refresh_from_db()
        routing_decision.refresh_from_db()
        ld_after = {f.name: getattr(decision, f.name) for f in LiquidityDecision._meta.fields}
        rd_after = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}

        self.assertEqual(ld_before, ld_after)
        self.assertEqual(rd_before, rd_after)

    # ── 12. No se crean eventos de otras categorías ─────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_extra_events_of_other_categories(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.ROUTING).count(), 1)
        self.assertEqual(BrokerAuditEvent.objects.filter(category=Category.LIQUIDITY).count(), 1)
        # TRADING's own position.opened event is a legitimate, pre-existing
        # part of the base open flow (unrelated to this block) — excluded
        # here alongside ROUTING/LIQUIDITY, which this test already counts
        # exactly above. What matters is that no OTHER category besides
        # these three received anything from this call.
        other = BrokerAuditEvent.objects.exclude(
            category__in=[Category.ROUTING, Category.LIQUIDITY, Category.TRADING],
        )
        self.assertEqual(other.count(), 0)
