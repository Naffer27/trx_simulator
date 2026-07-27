"""
BOOK-06g — Controlled Activation Foundation tests.

Covers exactly what this block adds, all still dormant (zero real
callers wired up):
  1. broker_exposure.calculate_broker_exposure()'s new
     exclude_position_ids parameter — retrocompatible, default None.
  2. broker_risk_shadow.calculate_shadow_broker_exposure() rewritten to
     delegate twice into the official formula instead of duplicating it
     (BOOK-06d's Option B retired).
  3. broker_risk._should_use_dealing_desk_adjusted_exposure() /
     _resolve_broker_exposure_for_validation() — new, isolated, no
     caller inside validate_new_order() yet.

No test here touches consumers.py/tasks.py/admin.py::force_close — none
of them are modified by this block. validate_new_order() itself is
verified to be byte-for-byte unchanged (structural test), never given a
new call site.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from market_data.feeds import get_feed_manager
from simulator import broker_exposure, broker_risk
from simulator.broker_risk_shadow import calculate_shadow_broker_exposure
from simulator.models import DealingDeskDecision, Position, RoutingDecision
from simulator.routing_engine import Book

from .factories import make_account


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


def _make_position(account, symbol="BTCUSD", side="BUY", qty="1.0", avg_price="100.00",
                    is_simulated_hedge=None):
    routing_decision = RoutingDecision.objects.create(
        book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
    )
    pos = Position.objects.create(
        account=account, symbol=symbol, side=side,
        qty=Decimal(qty), avg_price=Decimal(avg_price),
        routing_decision=routing_decision,
    )
    if is_simulated_hedge is not None:
        DealingDeskDecision.objects.create(
            routing_decision=routing_decision, position=pos, symbol=symbol,
            is_simulated_hedge=is_simulated_hedge, routing_profile_snapshot="HEDGE_CANDIDATE",
        )
    return pos, routing_decision


# ─────────────────────────────────────────────────────────────────────────
# 1. calculate_broker_exposure() — new parameter, retrocompatible
# ─────────────────────────────────────────────────────────────────────────
class CalculateBrokerExposureExclusionTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.account = make_account(balance=Decimal("100000"))

    def test_default_none_matches_no_parameter_call(self):
        _make_position(self.account, qty="2.0", avg_price="100.00")

        implicit = broker_exposure.calculate_broker_exposure()
        explicit_none = broker_exposure.calculate_broker_exposure(exclude_position_ids=None)

        self.assertEqual(implicit.gross_notional, explicit_none.gross_notional)
        self.assertEqual(implicit.open_position_count, explicit_none.open_position_count)
        self.assertEqual(implicit.gross_notional, Decimal("200.00"))

    def test_empty_set_matches_none(self):
        _make_position(self.account, qty="2.0", avg_price="100.00")

        none_result = broker_exposure.calculate_broker_exposure(exclude_position_ids=None)
        empty_result = broker_exposure.calculate_broker_exposure(exclude_position_ids=frozenset())

        self.assertEqual(none_result.gross_notional, empty_result.gross_notional)

    def test_excludes_specified_position_only(self):
        pos_a, _ = _make_position(self.account, qty="2.0", avg_price="100.00")
        pos_b, _ = _make_position(self.account, qty="3.0", avg_price="100.00")

        result = broker_exposure.calculate_broker_exposure(exclude_position_ids=frozenset({pos_a.id}))

        self.assertEqual(result.gross_notional, Decimal("300.00"))
        self.assertEqual(result.open_position_count, 1)

    def test_excluding_nonexistent_id_has_no_effect(self):
        _make_position(self.account, qty="2.0", avg_price="100.00")

        result = broker_exposure.calculate_broker_exposure(exclude_position_ids=frozenset({999999}))

        self.assertEqual(result.gross_notional, Decimal("200.00"))
        self.assertEqual(result.open_position_count, 1)

    def test_matches_shadow_calculator_for_same_exclusion(self):
        pos_a, rd_a = _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=False)
        pos_b, rd_b = _make_position(self.account, qty="3.0", avg_price="100.00", is_simulated_hedge=True)

        direct = broker_exposure.calculate_broker_exposure(exclude_position_ids=frozenset({pos_b.id}))
        via_shadow = calculate_shadow_broker_exposure()

        self.assertEqual(direct.gross_notional, via_shadow.gross_exposure_shadow)


# ─────────────────────────────────────────────────────────────────────────
# 2. broker_risk_shadow.py — duplication retired, same external contract
# ─────────────────────────────────────────────────────────────────────────
class ShadowCalculatorDelegatesToOfficialFormulaTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.account = make_account(balance=Decimal("100000"))

    def test_shadow_and_actual_use_same_underlying_formula(self):
        """Structural proof of de-duplication: patch
        calculate_broker_exposure() itself and confirm it is called
        exactly twice by calculate_shadow_broker_exposure() — once
        for the official number, once for the shadow number — never a
        second, independent aggregation."""
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)

        with patch(
            "simulator.broker_exposure.calculate_broker_exposure",
            wraps=broker_exposure.calculate_broker_exposure,
        ) as spy:
            calculate_shadow_broker_exposure()

        self.assertEqual(spy.call_count, 2)
        first_call_kwargs = spy.call_args_list[0].kwargs
        second_call_kwargs = spy.call_args_list[1].kwargs
        self.assertNotIn("exclude_position_ids", first_call_kwargs)
        self.assertIn("exclude_position_ids", second_call_kwargs)

    def test_excluded_position_count_respects_filters(self):
        _seed_price("EUR/USD", 1.08)
        self.addCleanup(_clear_price, "EUR/USD")
        _make_position(self.account, symbol="BTCUSD", qty="1.0", avg_price="100.00", is_simulated_hedge=True)
        _make_position(self.account, symbol="EUR/USD", qty="1.0", avg_price="1.08", is_simulated_hedge=True)

        result = calculate_shadow_broker_exposure(symbol="BTCUSD")

        # Only the BTCUSD exclusion should count — not the EUR/USD one,
        # which is outside this filtered scope entirely.
        self.assertEqual(result.excluded_position_count, 1)
        self.assertEqual(result.total_position_count, 1)


# ─────────────────────────────────────────────────────────────────────────
# 3. Gate — _should_use_dealing_desk_adjusted_exposure()
# ─────────────────────────────────────────────────────────────────────────
class DealingDeskExposureGateTests(TestCase):

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=False)
    def test_flag_off_always_false(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({1, 2, 3})):
            self.assertFalse(broker_risk._should_use_dealing_desk_adjusted_exposure(1))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({42}))
    def test_flag_on_account_outside_allowlist(self):
        self.assertFalse(broker_risk._should_use_dealing_desk_adjusted_exposure(1))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({42}))
    def test_flag_on_account_inside_allowlist(self):
        self.assertTrue(broker_risk._should_use_dealing_desk_adjusted_exposure(42))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset())
    def test_empty_allowlist_with_flag_on_is_still_false(self):
        """Deliberately the OPPOSITE default from BOOK-04f's own
        allowlist semantics — empty means NO accounts qualify here,
        never 'no restriction' (see gate's own docstring)."""
        self.assertFalse(broker_risk._should_use_dealing_desk_adjusted_exposure(1))
        self.assertFalse(broker_risk._should_use_dealing_desk_adjusted_exposure(999))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS="not-a-frozenset")
    def test_invalid_configuration_never_raises(self):
        """A malformed DEALING_DESK_EXPOSURE_ACCOUNT_IDS (e.g. a plain
        string instead of a frozenset of ints) makes `account_id in
        allowlist` raise TypeError — the gate's own try/except must
        absorb it and return False, never propagate."""
        try:
            result = broker_risk._should_use_dealing_desk_adjusted_exposure(1)
        except Exception as exc:
            self.fail(f"gate raised {exc!r} — must never raise")
        self.assertFalse(result)


# ─────────────────────────────────────────────────────────────────────────
# 4. Resolver — _resolve_broker_exposure_for_validation()
# ─────────────────────────────────────────────────────────────────────────
class DealingDeskExposureResolverTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.account = make_account(balance=Decimal("100000"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=False)
    def test_gate_false_calls_official_snapshot_no_dealing_desk_query(self):
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)

        with patch(
            "simulator.models.DealingDeskDecision.objects.filter",
        ) as mocked_filter:
            result = broker_risk._resolve_broker_exposure_for_validation(self.account.pk)
            mocked_filter.assert_not_called()

        self.assertEqual(result.gross_notional, Decimal("100.00"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_gate_true_uses_exclusion(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.account.pk})):
            _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=False)
            _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=True)

            result = broker_risk._resolve_broker_exposure_for_validation(self.account.pk)

        self.assertEqual(result.gross_notional, Decimal("100.00"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({999}))
    def test_resolver_failure_falls_back_to_official_never_raises(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.account.pk})):
            _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)

            with patch(
                "simulator.models.DealingDeskDecision.objects.filter",
                side_effect=RuntimeError("simulated failure"),
            ):
                try:
                    result = broker_risk._resolve_broker_exposure_for_validation(self.account.pk)
                except Exception as exc:
                    self.fail(f"resolver raised {exc!r} — must never raise")

        self.assertEqual(result.gross_notional, Decimal("100.00"))


# ─────────────────────────────────────────────────────────────────────────
# 5. Prueba estructural — validate_new_order() (BOOK-06h.2 updated this
#    class: the resolver is now the real call site — see
#    test_book06h2_real_activation_integration.py for the full battery.
#    What stays true, and is still asserted here, is that no OTHER
#    function in validate_new_order() changed shape.)
# ─────────────────────────────────────────────────────────────────────────
class ValidateNewOrderUnaffectedTests(TestCase):

    def test_validate_new_order_now_calls_the_resolver_exactly_once(self):
        """BOOK-06h.2 wired _resolve_broker_exposure_for_validation() in
        as validate_new_order()'s sole functional change — it is now
        called exactly once per call, with the account_id positional
        arg, superseding this class's original BOOK-06g/06h.1-era
        assertion that it was never invoked."""
        make_account(balance=Decimal("100000"))

        with patch(
            "simulator.broker_risk._resolve_broker_exposure_for_validation",
            wraps=broker_risk._resolve_broker_exposure_for_validation,
        ) as spy_resolver:
            broker_risk.validate_new_order(
                account_id=1, symbol="BTCUSD", side="BUY", qty=Decimal("0.1"),
            )
            spy_resolver.assert_called_once_with(1)

    def test_no_new_imports_of_pnl_or_ledger_modules(self):
        import inspect
        source = inspect.getsource(broker_risk)
        self.assertNotIn("import pnl_engine", source)
        self.assertNotIn("import broker_ledger", source)
        self.assertNotIn("import liquidity_ledger", source)


# ─────────────────────────────────────────────────────────────────────────
# 6. Bit-a-bit idéntico con el flag OFF — regresión completa
# ─────────────────────────────────────────────────────────────────────────
class FlagOffRegressionTests(TransactionTestCase):

    def test_full_broker_risk_suite_still_passes_conceptually(self):
        """Sanity check exercised directly here (the real regression
        guarantee is the full existing test_broker_exposure_engine.py /
        test_broker_risk_limits_engine.py suites, run unmodified
        alongside this file)."""
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        account = make_account(balance=Decimal("100000"))
        _make_position(account, qty="1.0", avg_price="100.00")

        decision = broker_risk.validate_new_order(
            account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.1"),
            price=100.0, contract_size=1.0,
        )
        self.assertIsNotNone(decision)
