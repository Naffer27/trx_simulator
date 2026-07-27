"""
BOOK-06d — Shadow Exposure Consumer tests.

Covers calculate_shadow_broker_exposure() (simulator/broker_risk_shadow.py)
in complete isolation — Option B (approved 2026-07-27): fully isolated
module, zero modification to broker_exposure.py/broker_risk.py. No test
here touches consumers.py/tasks.py/admin.py/dealing_desk.py — no new
call site exists in any of them; this module has no caller yet.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection

from market_data.feeds import get_feed_manager
from simulator import broker_exposure
from simulator.broker_risk_shadow import ShadowExposureComparison, calculate_shadow_broker_exposure
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


class ShadowExposureConsumerTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.account = make_account(balance=Decimal("100000"))

    # ── 1. Sin DealingDeskDecision ────────────────────────────────────────
    def test_no_dealing_desk_decisions_shadow_equals_actual(self):
        _make_position(self.account, qty="2.0", avg_price="100.00")

        result = calculate_shadow_broker_exposure()

        self.assertEqual(result.gross_exposure_actual, Decimal("200.00"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("200.00"))
        self.assertEqual(result.simulated_hedge_excluded_exposure, Decimal("0.00"))
        self.assertEqual(result.absolute_difference, Decimal("0.00"))
        self.assertEqual(result.percentage_difference, Decimal("0.00"))

    # ── 2. Con posiciones is_simulated_hedge=True ───────────────────────
    def test_simulated_hedge_true_positions_excluded_from_shadow(self):
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=False)
        _make_position(self.account, qty="3.0", avg_price="100.00", is_simulated_hedge=True)

        result = calculate_shadow_broker_exposure()

        self.assertEqual(result.gross_exposure_actual, Decimal("500.00"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("200.00"))
        self.assertEqual(result.simulated_hedge_excluded_exposure, Decimal("300.00"))
        self.assertEqual(result.absolute_difference, Decimal("300.00"))
        self.assertEqual(result.excluded_position_count, 1)
        self.assertEqual(result.total_position_count, 2)

    # ── 3. Posición sin DealingDeskDecision cuenta como actual ──────────
    def test_position_without_decision_counts_as_actual(self):
        _make_position(self.account, qty="1.0", avg_price="100.00")  # no decision at all

        result = calculate_shadow_broker_exposure()

        self.assertEqual(result.gross_exposure_actual, Decimal("100.00"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("100.00"))
        self.assertEqual(result.excluded_position_count, 0)

    # ── 4. Mezcla True/False/sin decisión ───────────────────────────────
    def test_mixed_true_false_and_missing_decisions(self):
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=False)
        _make_position(self.account, qty="3.0", avg_price="100.00")  # no decision

        result = calculate_shadow_broker_exposure()

        self.assertEqual(result.gross_exposure_actual, Decimal("600.00"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("500.00"))
        self.assertEqual(result.excluded_position_count, 1)
        self.assertEqual(result.total_position_count, 3)

    # ── 5. Libro vacío ────────────────────────────────────────────────────
    def test_empty_book_zero_percentage_no_crash(self):
        result = calculate_shadow_broker_exposure()

        self.assertEqual(result.gross_exposure_actual, Decimal("0"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("0"))
        self.assertEqual(result.percentage_difference, Decimal("0.00"))
        self.assertEqual(result.total_position_count, 0)

    # ── 6. Fallo simulado — fail-safe ───────────────────────────────────
    def test_failure_returns_zeroed_comparison_never_raises(self):
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)

        with patch(
            "simulator.models.DealingDeskDecision.objects.filter",
            side_effect=RuntimeError("simulated failure"),
        ):
            try:
                result = calculate_shadow_broker_exposure()
            except Exception as exc:
                self.fail(f"calculate_shadow_broker_exposure() raised {exc!r} — must never raise")

        self.assertEqual(result, ShadowExposureComparison())
        self.assertEqual(result.gross_exposure_actual, Decimal("0"))
        self.assertEqual(result.gross_exposure_shadow, Decimal("0"))

    # ── 7. Prueba estructural — cero interferencia con el cálculo oficial ─
    def test_official_calculation_unaffected(self):
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=True)

        before = broker_exposure.calculate_broker_exposure()
        calculate_shadow_broker_exposure()
        after = broker_exposure.calculate_broker_exposure()

        self.assertEqual(before.gross_notional, after.gross_notional)
        self.assertEqual(before.gross_notional, Decimal("200.00"))
        self.assertEqual(after.gross_notional, Decimal("200.00"))

    # ── 8. Determinismo ───────────────────────────────────────────────────
    def test_deterministic_repeated_calls(self):
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=False)

        results = [calculate_shadow_broker_exposure() for _ in range(5)]
        for r in results[1:]:
            self.assertEqual(r.gross_exposure_actual, results[0].gross_exposure_actual)
            self.assertEqual(r.gross_exposure_shadow, results[0].gross_exposure_shadow)

    # ── 9. Filtros propagados correctamente ─────────────────────────────
    def test_symbol_filter_scopes_both_calculations(self):
        _seed_price("EUR/USD", 1.08)
        self.addCleanup(_clear_price, "EUR/USD")
        _make_position(self.account, symbol="BTCUSD", qty="1.0", avg_price="100.00")
        _make_position(self.account, symbol="EUR/USD", qty="10.0", avg_price="1.08")

        result = calculate_shadow_broker_exposure(symbol="BTCUSD")

        self.assertEqual(result.gross_exposure_actual, Decimal("100.00"))
        self.assertEqual(result.total_position_count, 1)

    # ── 10. Cero N+1 ──────────────────────────────────────────────────────
    def test_two_queries_regardless_of_position_count(self):
        for _ in range(5):
            _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)

        with CaptureQueriesContext(connection) as ctx:
            calculate_shadow_broker_exposure()

        select_queries = [q for q in ctx.captured_queries if q["sql"].strip().upper().startswith("SELECT")]
        self.assertEqual(len(select_queries), 2)

    # ── 11. DealingDeskDecision no se modifica ──────────────────────────
    def test_never_modifies_dealing_desk_decision(self):
        pos, routing_decision = _make_position(
            self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True,
        )
        decision = DealingDeskDecision.objects.get(position=pos)
        before = {f.name: getattr(decision, f.name) for f in DealingDeskDecision._meta.fields}

        calculate_shadow_broker_exposure()

        decision.refresh_from_db()
        after = {f.name: getattr(decision, f.name) for f in DealingDeskDecision._meta.fields}
        self.assertEqual(before, after)

    # ── 12. Símbolo sin precio fresco no rompe el cálculo ───────────────
    def test_unpriced_symbol_skipped_gracefully(self):
        _make_position(self.account, symbol="XAUUSD", qty="1.0", avg_price="2000.00")

        try:
            result = calculate_shadow_broker_exposure()
        except Exception as exc:
            self.fail(f"calculate_shadow_broker_exposure() raised {exc!r} on unpriced symbol")

        self.assertEqual(result.gross_exposure_actual, Decimal("0"))
        self.assertEqual(result.total_position_count, 1)
