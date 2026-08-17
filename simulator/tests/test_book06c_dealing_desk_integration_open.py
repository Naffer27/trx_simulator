"""
BOOK-06c — Dealing Desk Decision Engine: open-time integration.

Covers the single integration point authorized for this block:
TradingConsumer._order_new(), via the new @database_sync_to_async
helper _db_record_dealing_desk_decision() — creates exactly one
DealingDeskDecision per RoutingDecision created, independent of whether
the Liquidity Engine (BOOK-05) is enabled or produced a LiquidityDecision
for this position (BOOK-06c design, approved 2026-07-27). Never touches
tasks.py or admin.py — no equivalent open path exists there.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from market_data.feeds import get_feed_manager
from simulator.models import DealingDeskDecision, LiquidityDecision, LiquidityProvider, Position, RoutingDecision, TraderScore

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


class DealingDeskIntegrationTests(TransactionTestCase):

    def setUp(self):
        _seed_price("BTCUSD", 63000.0)  # O.6c-1w-b: realistic BTCUSD magnitude, Capa A plausibility gate
        self.addCleanup(_clear_price, "BTCUSD")

    # ── 1. Perfil calificante + LiquidityDecision existente ─────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_qualifying_profile_with_liquidity_decision_is_simulated_hedge_true(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        liquidity_decision = LiquidityDecision.objects.get()

        self.assertEqual(DealingDeskDecision.objects.count(), 1)
        decision = DealingDeskDecision.objects.get()
        self.assertTrue(decision.is_simulated_hedge)
        self.assertEqual(decision.routing_profile_snapshot, "HEDGE_CANDIDATE")
        self.assertEqual(decision.routing_decision_id, pos.routing_decision_id)
        self.assertEqual(decision.liquidity_decision_id, liquidity_decision.id)
        self.assertEqual(decision.position_id, pos.id)
        self.assertEqual(decision.symbol, "BTCUSD")

    # ── 2. Perfil no calificante, con LiquidityDecision ─────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_non_qualifying_profile_with_liquidity_decision_is_simulated_hedge_false(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="INTERNAL")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(DealingDeskDecision.objects.count(), 1)
        decision = DealingDeskDecision.objects.get()
        self.assertFalse(decision.is_simulated_hedge)
        self.assertEqual(decision.routing_profile_snapshot, "INTERNAL")
        self.assertIsNotNone(decision.liquidity_decision_id)

    # ── 3. NUEVO comportamiento: sin LiquidityDecision, igual se crea ───
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_without_liquidity_decision_still_creates_dealing_desk_decision(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        self.assertEqual(DealingDeskDecision.objects.count(), 1)
        decision = DealingDeskDecision.objects.get()
        self.assertIsNone(decision.liquidity_decision_id)
        self.assertEqual(decision.routing_profile_snapshot, "HEDGE_CANDIDATE")
        # has_liquidity_decision=False forces is_simulated_hedge=False regardless
        # of the qualifying profile — same rule already closed in BOOK-06b.
        self.assertFalse(decision.is_simulated_hedge)

    # ── 4. Sin RoutingDecision ───────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=False, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_routing_decision_creates_zero_dealing_desk_decisions(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(DealingDeskDecision.objects.count(), 0)

    # ── 5. Motor lanza ────────────────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_engine_raises_creates_zero_decisions_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        with patch(
            "simulator.dealing_desk.evaluate_dealing_desk_decision",
            side_effect=RuntimeError("simulated engine failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(DealingDeskDecision.objects.count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 6. Writer devuelve None ──────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_writer_returns_none_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        with patch("simulator.dealing_desk.record_dealing_desk_decision", return_value=None):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 7. Writer lanza ───────────────────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_writer_raises_open_completes_normally(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        with patch(
            "simulator.dealing_desk.record_dealing_desk_decision",
            side_effect=RuntimeError("simulated write failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(DealingDeskDecision.objects.count(), 0)
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 8. routing_profile_snapshot / fallback INTERNAL ─────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_no_traderscore_defaults_to_internal_routing_profile_snapshot(self):
        account = make_account(balance=Decimal("100000"))
        self.assertFalse(TraderScore.objects.filter(account=account).exists())
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        decision = DealingDeskDecision.objects.get()
        self.assertEqual(decision.routing_profile_snapshot, "INTERNAL")
        self.assertFalse(decision.is_simulated_hedge)

    # ── 9. RoutingDecision/LiquidityDecision no modificados ─────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_never_modifies_routing_decision_or_liquidity_decision(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        routing_decision = RoutingDecision.objects.get(pk=pos.routing_decision_id)
        liquidity_decision = LiquidityDecision.objects.get()

        self.assertEqual(routing_decision.book, "INTERNAL")
        self.assertEqual(routing_decision.reason_code, "TRIVIAL_INTERNAL_DEFAULT")
        self.assertEqual(liquidity_decision.symbol, "BTCUSD")

    # ── 10. order_ack/order_fill siempre enviados ───────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_client_receives_ack_and_fill_regardless_of_dealing_desk_outcome(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    # ── 11. Exactamente una decisión por RoutingDecision, sin duplicados ─
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_exactly_one_decision_per_routing_decision_no_duplicates(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(RoutingDecision.objects.count(), 1)
        self.assertEqual(DealingDeskDecision.objects.count(), 1)
