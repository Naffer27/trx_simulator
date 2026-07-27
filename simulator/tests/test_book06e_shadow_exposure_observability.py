"""
BOOK-06e — Shadow Exposure Observability tests.

Covers the single new integration point authorized for this block:
BrokerSnapshotAdmin.shadow_exposure_view (admin:broker_shadow_exposure),
a read-only staff surface for calculate_shadow_broker_exposure()
(BOOK-06d, untouched). Restricted to is_superuser (approved 2026-07-27).
No test here touches consumers.py/tasks.py/broker_risk.py/
broker_exposure.py/dealing_desk.py — none of them are modified by this
block.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import site as admin_site
from django.test import Client, TestCase
from django.urls import reverse

from market_data.feeds import get_feed_manager
from simulator.broker_risk_shadow import ShadowExposureComparison
from simulator.models import DealingDeskDecision, Position, RoutingDecision
from simulator.routing_engine import Book

from .factories import make_account, make_user


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


class ShadowExposureObservabilityTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.account = make_account(balance=Decimal("100000"))
        self.url = reverse("admin:broker_shadow_exposure")

    # ── 1. Registrada, responde 200 para superuser ──────────────────────
    def test_registered_and_accessible_to_superuser(self):
        superuser = make_user(username="book06e_super", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(self.url)

        self.assertEqual(resp.status_code, 200)

    # ── 2. Staff no-superuser → 403 ──────────────────────────────────────
    def test_staff_non_superuser_gets_403(self):
        staff = make_user(username="book06e_staff", is_staff=True, is_superuser=False)
        client = Client()
        client.force_login(staff)

        resp = client.get(self.url)

        self.assertEqual(resp.status_code, 403)

    # ── 3. Usuario anónimo → redirige a login, nunca 200 ────────────────
    def test_anonymous_user_redirected(self):
        client = Client()
        resp = client.get(self.url)

        self.assertNotEqual(resp.status_code, 200)

    # ── 4. Métricas correctas en el contexto ────────────────────────────
    def test_metrics_present_and_correct(self):
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=False)
        _make_position(self.account, qty="3.0", avg_price="100.00", is_simulated_hedge=True)
        superuser = make_user(username="book06e_super2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        comparison = resp.context["comparison"]
        self.assertEqual(comparison.gross_exposure_actual, Decimal("500.00"))
        self.assertEqual(comparison.gross_exposure_shadow, Decimal("200.00"))
        self.assertEqual(comparison.simulated_hedge_excluded_exposure, Decimal("300.00"))
        self.assertEqual(comparison.absolute_difference, Decimal("300.00"))
        self.assertEqual(comparison.excluded_position_count, 1)
        self.assertEqual(comparison.total_position_count, 2)
        self.assertIsNotNone(comparison.generated_at)

    # ── 5. Filtros se propagan ───────────────────────────────────────────
    def test_symbol_filter_propagated(self):
        _seed_price("EUR/USD", 1.08)
        self.addCleanup(_clear_price, "EUR/USD")
        _make_position(self.account, symbol="BTCUSD", qty="1.0", avg_price="100.00")
        _make_position(self.account, symbol="EUR/USD", qty="10.0", avg_price="1.08")
        superuser = make_user(username="book06e_super3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(self.url, {"symbol": "BTCUSD"})

        comparison = resp.context["comparison"]
        self.assertEqual(comparison.gross_exposure_actual, Decimal("100.00"))
        self.assertEqual(comparison.total_position_count, 1)

    # ── 6. Fallo simulado → 200, sin romper ─────────────────────────────
    def test_calculation_failure_returns_200_not_500(self):
        superuser = make_user(username="book06e_super4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        with patch(
            "simulator.broker_risk_shadow.calculate_shadow_broker_exposure",
            side_effect=RuntimeError("simulated failure"),
        ):
            resp = client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["calc_failed"])
        self.assertEqual(resp.context["comparison"], ShadowExposureComparison())
        self.assertIn(b"no pudo completarse", resp.content)

    # ── 7. Banner SHADOW presente siempre ───────────────────────────────
    def test_shadow_banner_present(self):
        superuser = make_user(username="book06e_super5", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(self.url)

        self.assertIn("MODO SHADOW", resp.content.decode())

    # ── 8. Cero escritura sobre DealingDeskDecision ─────────────────────
    def test_zero_writes_to_dealing_desk_decision(self):
        _make_position(self.account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)
        superuser = make_user(username="book06e_super6", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        before = DealingDeskDecision.objects.count()
        for _ in range(3):
            client.get(self.url)
        after = DealingDeskDecision.objects.count()

        self.assertEqual(before, after)

    # ── 9. Prueba estructural — cero interferencia con broker_risk.py ──
    def test_does_not_affect_official_broker_risk_validation(self):
        from simulator import broker_risk
        _make_position(self.account, qty="2.0", avg_price="100.00", is_simulated_hedge=True)
        superuser = make_user(username="book06e_super7", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        before_book = broker_risk._exposure.broker_exposure_snapshot()
        client.get(self.url)
        after_book = broker_risk._exposure.broker_exposure_snapshot()

        self.assertEqual(before_book.gross_notional, after_book.gross_notional)

    # ── 10. Registrada en admin_site (sanity check de URL) ──────────────
    def test_url_resolves(self):
        self.assertEqual(self.url, "/admin/simulator/brokersnapshot/shadow-exposure/")
