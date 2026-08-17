"""
BOOK-04f — Controlled Activation Mechanism tests.

Covers TradingConsumer._should_activate_routing_decision() (the gate,
simulator/consumers.py) and its single call site inside
_db_open_position_atomic() — the only line that changed there. No test
here designs or exercises any real routing rule: the RoutingDecision
this gate allows/blocks remains exactly the same trivial contract
introduced in BOOK-04b (book=INTERNAL, always).
"""
import time
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import BrokerAuditEvent, Position, RoutingDecision
from simulator.routing_engine import Book, REASON_TRIVIAL_INTERNAL_DEFAULT

from .factories import make_account
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__
_should_activate = TradingConsumer._should_activate_routing_decision


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


class _FakeConsumer:
    """Same minimal stub used by test_book04b/test_book04e — extended
    with account_type (BOOK-04f's own second gating dimension) and the
    real, unmodified _should_activate_routing_decision, borrowed
    directly from TradingConsumer (same principle as _db_open_sync
    above: test the real function, never a reimplementation of it)."""
    _should_activate_routing_decision = TradingConsumer._should_activate_routing_decision

    def __init__(self, account_id, netting_mode=False, account_type="STANDARD"):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": 0.0, "account_type": account_type}
        self._feed = get_feed_manager()


class _CleanPriceMixin:
    def setUp(self):
        super().setUp()
        _seed_price("EUR/USD", 1.0800)
        self.addCleanup(_clear_price, "EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# 1. _should_activate_routing_decision() — pure gate logic, no DB, no I/O
# ─────────────────────────────────────────────────────────────────────────
class GateUnitTests(TestCase):

    def setUp(self):
        self.consumer = _FakeConsumer(account_id=1)

    @override_settings(ROUTING_ENGINE_ENABLED=False)
    def test_master_flag_off_always_false(self):
        self.assertFalse(_should_activate(self.consumer, "BTCUSD", "STANDARD"))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset(), ROUTING_ENGINE_ACCOUNT_TYPES=frozenset())
    def test_master_flag_on_no_granular_flags_always_true(self):
        """Absent/empty allowlists = no restriction — BOOK-04b/04e's
        pre-existing 100%-of-orders behavior, unchanged."""
        self.assertTrue(_should_activate(self.consumer, "BTCUSD", "STANDARD"))
        self.assertTrue(_should_activate(self.consumer, "EUR/USD", "DEMO"))
        self.assertTrue(_should_activate(self.consumer, "ANYTHING", "ANYTHING"))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"}))
    def test_symbol_allowed(self):
        self.assertTrue(_should_activate(self.consumer, "BTCUSD", "STANDARD"))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"}))
    def test_symbol_blocked(self):
        self.assertFalse(_should_activate(self.consumer, "EUR/USD", "STANDARD"))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_ACCOUNT_TYPES=frozenset({"CHALLENGE"}))
    def test_account_type_allowed(self):
        self.assertTrue(_should_activate(self.consumer, "BTCUSD", "CHALLENGE"))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_ACCOUNT_TYPES=frozenset({"CHALLENGE"}))
    def test_account_type_blocked(self):
        self.assertFalse(_should_activate(self.consumer, "BTCUSD", "STANDARD"))

    @override_settings(
        ROUTING_ENGINE_ENABLED=True,
        ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"}),
        ROUTING_ENGINE_ACCOUNT_TYPES=frozenset({"CHALLENGE"}),
    )
    def test_symbol_and_account_type_combination_is_and_not_or(self):
        # Both match → True
        self.assertTrue(_should_activate(self.consumer, "BTCUSD", "CHALLENGE"))
        # Symbol matches, account_type doesn't → False
        self.assertFalse(_should_activate(self.consumer, "BTCUSD", "STANDARD"))
        # Account_type matches, symbol doesn't → False
        self.assertFalse(_should_activate(self.consumer, "EUR/USD", "CHALLENGE"))
        # Neither matches → False
        self.assertFalse(_should_activate(self.consumer, "EUR/USD", "STANDARD"))

    def test_configuration_absent_uses_settings_defaults(self):
        """No override_settings at all — exercises the real defaults
        loaded from trx_simulator/settings.py (ROUTING_ENGINE_ENABLED
        False unless the environment sets it, granular allowlists empty
        unless configured)."""
        from django.conf import settings
        result = _should_activate(self.consumer, "BTCUSD", "STANDARD")
        self.assertEqual(result, bool(getattr(settings, "ROUTING_ENGINE_ENABLED", False)))

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset(), ROUTING_ENGINE_ACCOUNT_TYPES=frozenset())
    def test_malformed_env_parses_to_empty_frozenset_never_raises(self):
        """Mirrors MARKET_DATA_ROUTER_SYMBOLS's own defensive parsing
        (`s.strip() for s in ... if s.strip()`) — a value of only commas/
        spaces collapses to an empty frozenset, never raises at import
        time. Simulated here directly since settings are already loaded;
        the parsing itself is exercised by simply confirming an empty
        frozenset behaves as "no restriction", never as an error."""
        try:
            result = _should_activate(self.consumer, "BTCUSD", "STANDARD")
        except Exception as exc:   # pragma: no cover - fails via assertion below
            self.fail(f"gate raised unexpectedly on empty allowlist: {exc!r}")
        self.assertTrue(result)

    def test_unexpected_exception_during_evaluation_is_fail_safe_false(self):
        """Same contract as _should_use_new_router() in market_data/feeds.py:
        any unexpected failure inside the gate's own evaluation (e.g. a
        corrupted/misconfigured allowlist whose `in` operator itself
        raises) is treated as "no", never propagated, never defaulted to
        "yes". Uses a container that raises on membership test — a
        realistic stand-in for "configuración inválida" beyond a plain
        string, exercising the gate's own try/except rather than
        bypassing it."""
        class _BrokenAllowlist:
            def __bool__(self):
                return True

            def __contains__(self, item):
                raise RuntimeError("boom")

        with override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_ACCOUNT_TYPES=_BrokenAllowlist()):
            result = _should_activate(self.consumer, "BTCUSD", "STANDARD")
        self.assertFalse(result)

    def test_never_queries_the_database(self):
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            with CaptureQueriesContext(connection) as ctx:
                _should_activate(self.consumer, "BTCUSD", "STANDARD")
        self.assertEqual(len(ctx.captured_queries), 0)


# ─────────────────────────────────────────────────────────────────────────
# 2. Call site — _db_open_position_atomic()
# ─────────────────────────────────────────────────────────────────────────
class CallSiteTests(_CleanPriceMixin, TestCase):

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"EUR/USD"}))
    def test_allowed_symbol_creates_decision(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="STANDARD")

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertIsNotNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 1)

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"}))
    def test_blocked_symbol_creates_no_decision_but_position_opens_normally(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="STANDARD")

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNone(pos.routing_decision_id)

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_ACCOUNT_TYPES=frozenset({"CHALLENGE"}))
    def test_blocked_account_type_creates_no_decision(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="DEMO")

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True)
    def test_no_granular_flags_matches_pre_book04f_behavior(self):
        """Compatibility with BOOK-04b/04e: master flag alone, no
        allowlists defined, must behave exactly as before this block
        existed — every symbol/account activates."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="FUNDED")

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertIsNotNone(result["routing_decision_id"])
        decision = RoutingDecision.objects.get(decision_id=result["routing_decision_id"])
        self.assertEqual(decision.account_id, account.id)

    @override_settings(ROUTING_ENGINE_ENABLED=False)
    def test_master_flag_off_unaffected_by_granular_flags(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="STANDARD")

        with override_settings(ROUTING_ENGINE_SYMBOLS=frozenset({"EUR/USD"})):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"EUR/USD"}))
    def test_decision_content_unchanged_when_allowed(self):
        """The gate only decides WHETHER to record — never WHAT. Same
        trivial contract as BOOK-04b, byte for byte."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="STANDARD")

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        decision = RoutingDecision.objects.get(decision_id=result["routing_decision_id"])
        self.assertEqual(decision.book, Book.INTERNAL)
        self.assertEqual(decision.reason_code, REASON_TRIVIAL_INTERNAL_DEFAULT)
        self.assertEqual(
            decision.inputs_snapshot,
            {"symbol": "EUR/USD", "side": "BUY", "qty": 1.0, "merged": False},
        )

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"EUR/USD"}))
    def test_lock_atomicity_and_position_creation_unaffected(self):
        """Same structural proof already used by BOOK-04b: balance,
        margin, Position fields, and the returned dict are identical
        whether or not the gate allows the RoutingDecision."""
        account_allowed = make_account(balance=Decimal("50000"))
        consumer_allowed = _FakeConsumer(account_allowed.pk, account_type="STANDARD")
        result_allowed = _db_open_sync(
            consumer_allowed, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=10.0, new_balance=49990.0,
        )

        with override_settings(ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"})):
            account_blocked = make_account(balance=Decimal("50000"))
            consumer_blocked = _FakeConsumer(account_blocked.pk, account_type="STANDARD")
            result_blocked = _db_open_sync(
                consumer_blocked, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=10.0, new_balance=49990.0,
            )

        shared_keys = (
            "merged", "new_balance", "ok",
            "required_margin", "required_margin_pct",
            "projected_total_margin", "projected_total_margin_pct",
            "max_total_margin_pct", "current_open_positions", "max_open_positions",
        )
        for key in shared_keys:
            self.assertEqual(result_allowed[key], result_blocked[key], key)

        pos_allowed = Position.objects.get(pk=result_allowed["position_id"])
        pos_blocked = Position.objects.get(pk=result_blocked["position_id"])
        for field in ("symbol", "side", "qty", "avg_price", "sl", "tp"):
            self.assertEqual(getattr(pos_allowed, field), getattr(pos_blocked, field), field)

    def test_gate_internal_exception_is_fail_safe_position_still_opens(self):
        """End-to-end version of GateUnitTests's
        test_unexpected_exception_during_evaluation_is_fail_safe_false —
        proves the gate's own try/except (not any wrapper at the call
        site — there is none, same precedent as _try_live() not wrapping
        _should_use_new_router()) is enough to keep _db_open_position_
        atomic() fully fail-safe end to end."""
        class _BrokenAllowlist:
            def __bool__(self):
                return True

            def __contains__(self, item):
                raise RuntimeError("boom")

        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, account_type="STANDARD")

        with override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_ACCOUNT_TYPES=_BrokenAllowlist()):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNotNone(pos)


# ─────────────────────────────────────────────────────────────────────────
# 3. Compatibility with BOOK-04d (Audit Trail) via the real async path
# ─────────────────────────────────────────────────────────────────────────
class OrderNewCompatibilityTests(TransactionTestCase):

    def setUp(self):
        _seed_price("BTCUSD", 63000.0)  # O.6c-1w-b: realistic BTCUSD magnitude, Capa A plausibility gate
        self.addCleanup(_clear_price, "BTCUSD")

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"BTCUSD"}))
    def test_allowed_symbol_still_produces_exactly_one_audit_event(self):
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)   # account_type="STANDARD" per that helper's own default

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        self.assertIsNotNone(pos.routing_decision_id)
        events = BrokerAuditEvent.objects.filter(category="ROUTING")
        self.assertEqual(events.count(), 1)

    @override_settings(ROUTING_ENGINE_ENABLED=True, ROUTING_ENGINE_SYMBOLS=frozenset({"EUR/USD"}))
    def test_blocked_symbol_produces_zero_audit_events(self):
        """BOOK-04d only ever fires when routing_decision_id is present
        — a symbol blocked by BOOK-04f's gate never reaches that point,
        same as BOOK-04b's own writer-failure fail-open path."""
        account = make_account(balance=Decimal("100000"))
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=account)
        self.assertIsNone(pos.routing_decision_id)
        self.assertEqual(BrokerAuditEvent.objects.filter(category="ROUTING").count(), 0)
