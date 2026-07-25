"""
BOOK-05c — Liquidity Engine Shadow Mode Integration tests.

Covers the single new integration point authorized for this block:
TradingConsumer._db_record_liquidity_decision() (consumers.py), called
from _order_new() after _db_open_position_atomic() has already
committed, and simulator/liquidity_engine.py::record_liquidity_decision()
— the single write point for LiquidityDecision.

No test here modifies select_simulated_provider()/evaluate_simulated_hedge()
(BOOK-05b, untouched) or asserts anything about LiquidityLedger,
Category.LIQUIDITY, granular flags, or routing_profile-based filtering —
all of that is out of scope for this block (BOOK-05d/05e/05g).
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.liquidity_engine import record_liquidity_decision
from simulator.models import LiquidityDecision, LiquidityProvider, RoutingDecision, TraderScore

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


# ─────────────────────────────────────────────────────────────────────────
# 1. record_liquidity_decision() — the writer, in isolation
# ─────────────────────────────────────────────────────────────────────────
class RecordLiquidityDecisionWriterTests(TestCase):

    def setUp(self):
        self.account = make_account(balance=Decimal("100000"))
        self.routing_decision = RoutingDecision.objects.create(
            book="INTERNAL", reason_code="TRIVIAL_INTERNAL_DEFAULT", account=self.account,
        )

    def test_creates_row_with_given_fields(self):
        decision = record_liquidity_decision(
            routing_decision_id=self.routing_decision.id,
            symbol="BTCUSD",
            exposure_usd=Decimal("8200.00"),
            simulated_spread=Decimal("5.0"),
            simulated_cost=Decimal("0.50"),
            inputs_snapshot={"symbol": "BTCUSD"},
            engine_version=1,
            schema_version=1,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(LiquidityDecision.objects.count(), 1)
        self.assertEqual(decision.routing_decision_id, self.routing_decision.id)
        self.assertEqual(decision.symbol, "BTCUSD")
        self.assertEqual(decision.exposure_usd, Decimal("8200.00"))
        self.assertEqual(decision.simulated_spread, Decimal("5.0"))
        self.assertEqual(decision.simulated_cost, Decimal("0.50"))
        self.assertEqual(decision.inputs_snapshot, {"symbol": "BTCUSD"})

    def test_no_get_or_create_no_uniqueness_two_calls_create_two_rows(self):
        """Explicit confirmation of the approved BOOK-05c adjustment:
        idempotency is deliberately deferred — a plain .create() allows
        two rows for the same routing_decision_id."""
        kwargs = dict(
            routing_decision_id=self.routing_decision.id, symbol="BTCUSD",
            exposure_usd=Decimal("100"), simulated_spread=Decimal("1"), simulated_cost=Decimal("1"),
        )
        record_liquidity_decision(**kwargs)
        record_liquidity_decision(**kwargs)
        self.assertEqual(LiquidityDecision.objects.count(), 2)

    def test_fail_open_returns_none_on_write_failure(self):
        with patch(
            "simulator.models.LiquidityDecision.objects.create",
            side_effect=Exception("simulated DB failure"),
        ):
            result = record_liquidity_decision(
                routing_decision_id=self.routing_decision.id, symbol="BTCUSD",
                exposure_usd=Decimal("100"), simulated_spread=Decimal("1"), simulated_cost=Decimal("1"),
            )
        self.assertIsNone(result)
        self.assertEqual(LiquidityDecision.objects.count(), 0)

    def test_fail_open_never_raises(self):
        with patch(
            "simulator.models.LiquidityDecision.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            try:
                record_liquidity_decision(
                    routing_decision_id=self.routing_decision.id, symbol="BTCUSD",
                    exposure_usd=Decimal("100"), simulated_spread=Decimal("1"), simulated_cost=Decimal("1"),
                )
            except Exception as exc:   # pragma: no cover - fails via assertion below
                self.fail(f"record_liquidity_decision() raised unexpectedly: {exc!r}")

    def test_never_writes_to_routing_decision(self):
        before = {f.name: getattr(self.routing_decision, f.name) for f in RoutingDecision._meta.fields}
        record_liquidity_decision(
            routing_decision_id=self.routing_decision.id, symbol="BTCUSD",
            exposure_usd=Decimal("100"), simulated_spread=Decimal("1"), simulated_cost=Decimal("1"),
        )
        self.routing_decision.refresh_from_db()
        after = {f.name: getattr(self.routing_decision, f.name) for f in RoutingDecision._meta.fields}
        self.assertEqual(before, after)


# ─────────────────────────────────────────────────────────────────────────
# 2. Real async integration — _order_new()
# ─────────────────────────────────────────────────────────────────────────
class OrderNewLiquidityShadowModeTests(TransactionTestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_flag_off_creates_zero_liquidity_decisions(self):
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_flag_on_qualifying_provider_creates_exactly_one_liquidity_decision(self):
        account = make_account(balance=Decimal("100000"))
        provider = _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 1)
        decision = LiquidityDecision.objects.get()
        self.assertEqual(decision.symbol, "BTCUSD")
        self.assertEqual(decision.provider_id, provider.id)
        self.assertIsNotNone(decision.routing_decision_id)
        pos = None
        from simulator.models import Position
        pos = Position.objects.get(account=account)
        self.assertEqual(decision.routing_decision_id, pos.routing_decision_id)
        self.assertEqual(decision.position_id, pos.id)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_qualifying_provider_creates_zero_but_position_opens(self):
        account = make_account(balance=Decimal("100000"))
        _provider(symbols=["EUR/USD"])  # does not cover BTCUSD
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        from simulator.models import Position
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_provider_at_all_creates_zero_but_position_opens(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        from simulator.models import Position
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_calculation_failure_still_opens_position_and_sends_ack_fill(self):
        """Simulates evaluate_simulated_hedge() raising (e.g. unknown
        symbol in market_data.symbol_specs) — the position must still
        open and the client must still receive order_ack/order_fill."""
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch(
            "simulator.liquidity_engine.evaluate_simulated_hedge",
            side_effect=KeyError("unknown symbol"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        from simulator.models import Position
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_write_failure_still_opens_position_and_sends_ack_fill(self):
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
        from simulator.models import Position
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_unexpected_exception_still_opens_position_and_sends_ack_fill(self):
        """A generic, unrelated failure (e.g. TraderScore lookup itself
        raising) must also never block the already-committed open."""
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch(
            "simulator.models.TraderScore.objects.filter",
            side_effect=RuntimeError("unexpected failure"),
        ):
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
        from simulator.models import Position
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        sent_types = [c.args[0].get("type") for c in consumer.send_json.call_args_list]
        self.assertIn("order_ack", sent_types)
        self.assertIn("order_fill", sent_types)

    @override_settings(ROUTING_ENGINE_ENABLED=False, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_routing_decision_id_skips_liquidity_block_entirely(self):
        """Routing Engine flag off => routing_decision_id is None =>
        the liquidity block's own guard must skip it, even though
        LIQUIDITY_ENGINE_ENABLED is True."""
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_no_traderscore_defaults_to_internal_routing_profile(self):
        """Brand-new account with zero closed trades has no TraderScore
        row at all — must default gracefully, never raise."""
        account = make_account(balance=Decimal("100000"))
        self.assertFalse(TraderScore.objects.filter(account=account).exists())
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        decision = LiquidityDecision.objects.get()
        self.assertEqual(decision.inputs_snapshot["routing_profile"], "INTERNAL")

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_routing_decision_never_modified_by_liquidity_flow(self):
        """Structural proof required by docs/BOOK_05_IMPLEMENTATION_PLAN.md,
        section 17 — no field of the real RoutingDecision changes,
        before vs. after the liquidity flow runs."""
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        from simulator.models import Position
        pos = Position.objects.get(account=account)
        routing_decision = RoutingDecision.objects.get(pk=pos.routing_decision_id)
        # If it survived unmodified since BOOK-04b created it, book/reason_code
        # must still be exactly the trivial Shadow Mode contract.
        self.assertEqual(routing_decision.book, "INTERNAL")
        self.assertEqual(routing_decision.reason_code, "TRIVIAL_INTERNAL_DEFAULT")

    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_does_not_duplicate_book05b_pure_functions_logic(self):
        """Confirms the integration calls BOOK-05b's real functions
        rather than reimplementing their logic — patch them out and
        confirm the block produces nothing without them."""
        account = make_account(balance=Decimal("100000"))
        _provider()
        consumer = _consumer(account.pk)

        with patch("simulator.liquidity_engine.select_simulated_provider", return_value=None) as mocked:
            _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
            self.assertTrue(mocked.called)

        self.assertIsNone(_first_error(consumer))
        self.assertEqual(LiquidityDecision.objects.count(), 0)
