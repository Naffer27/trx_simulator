"""
BOOK-06h.3 — Canary Observability.

Closes RC-1 Finding F-05 ("no se puede confirmar desde logs con qué
frecuencia se usó el canario"). Covers exactly what this subphase adds:

  1. broker_risk.py's new observability log (_log_dealing_desk_exposure_
     usage), emitted from validate_new_order() exactly once per call,
     ONLY when the adjusted book was genuinely used by
     _resolve_broker_exposure_for_validation() — never on flag OFF,
     outside the allowlist, an empty allowlist, or a resolver fallback.
  2. admin.py::shadow_exposure_view's new read-only canary_enabled/
     canary_account_count context values, rendered by
     shadow_exposure_observability.html as a plain ON/OFF indicator —
     no form, no button, no write path of any kind.

No test here touches the resolver's own return value shape, the 9 risk
rules, validate_new_order()'s decision logic, or any other BOOK-06
behavior — see test_book06g/06h1/06h2 for that coverage, unchanged by
this subphase.
"""
import logging
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import site as admin_site
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from market_data.feeds import get_feed_manager
from simulator import broker_risk
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


class DealingDeskExposureUsageLogTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(self._clear_price)
        self.canary_account = make_account(balance=Decimal("1000000"))
        self.other_account = make_account(balance=Decimal("1000000"))

    def _clear_price(self):
        feed = get_feed_manager()
        with feed._lock:
            feed._prices.pop("BTCUSD", None)
            feed._bids.pop("BTCUSD", None)
            feed._asks.pop("BTCUSD", None)
            feed._price_ts.pop("BTCUSD", None)

    # ── 1. Log generado cuando realmente se usa el libro ajustado ──────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_log_emitted_when_adjusted_book_actually_used(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

            with self.assertLogs("simulator.broker_risk", level="INFO") as cm:
                decision = broker_risk.validate_new_order(
                    account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                    qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
                )

        usage_lines = [line for line in cm.output if "dealing_desk_exposure_used" in line]
        self.assertEqual(len(usage_lines), 1)
        line = usage_lines[0]
        self.assertIn(f"account_id={self.canary_account.pk}", line)
        self.assertIn("mode=adjusted", line)
        self.assertIn("excluded_positions_count=1", line)
        self.assertIn(f"risk_allowed={decision.allowed}", line)
        self.assertIn("reason_code=", line)

    # ── 2. Log NO generado con flag OFF ─────────────────────────────────
    def test_no_log_with_flag_off(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        with self.assertLogs("simulator.broker_risk", level="INFO") as cm:
            # force at least one INFO-level record so assertLogs doesn't
            # error out on "no logs at all" — then assert our line absent.
            logging.getLogger("simulator.broker_risk").info("sentinel")
            broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )

        usage_lines = [line for line in cm.output if "dealing_desk_exposure_used" in line]
        self.assertEqual(len(usage_lines), 0)

    # ── 2b. Log NO generado para cuentas fuera del allowlist ────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_no_log_for_account_outside_allowlist(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

            with self.assertLogs("simulator.broker_risk", level="INFO") as cm:
                logging.getLogger("simulator.broker_risk").info("sentinel")
                broker_risk.validate_new_order(
                    account_id=self.other_account.pk, symbol="BTCUSD", side="BUY",
                    qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
                )

        usage_lines = [line for line in cm.output if "dealing_desk_exposure_used" in line]
        self.assertEqual(len(usage_lines), 0)

    # ── 2c. Log NO generado con allowlist vacío ─────────────────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset())
    def test_no_log_with_empty_allowlist(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        with self.assertLogs("simulator.broker_risk", level="INFO") as cm:
            logging.getLogger("simulator.broker_risk").info("sentinel")
            broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )

        usage_lines = [line for line in cm.output if "dealing_desk_exposure_used" in line]
        self.assertEqual(len(usage_lines), 0)

    # ── 3. Log NO duplicado en fallback por excepción ───────────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_no_usage_log_and_no_duplicate_error_log_on_fallback(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

            with patch(
                "simulator.models.DealingDeskDecision.objects.filter",
                side_effect=RuntimeError("simulated failure"),
            ):
                with self.assertLogs("simulator.broker_risk", level="ERROR") as cm:
                    broker_risk.validate_new_order(
                        account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                        qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
                    )

        usage_lines = [line for line in cm.output if "dealing_desk_exposure_used" in line]
        error_lines = [line for line in cm.output if "dealing desk exposure resolution failed" in line]
        self.assertEqual(len(usage_lines), 0)
        self.assertEqual(len(error_lines), 1)

    # ── extra: la observabilidad nunca cambia la decisión ya tomada ─────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_logging_never_changes_the_decision(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

            with self.assertLogs("simulator.broker_risk", level="INFO"):
                decision = broker_risk.validate_new_order(
                    account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                    qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
                )

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason_code)


class CanaryStatusIndicatorTests(TestCase):

    def setUp(self):
        self.superuser = make_user(username="book06h3_super", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)
        self.url = reverse("admin:broker_shadow_exposure")

    # ── 4. Indicador muestra OFF ─────────────────────────────────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=False, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset())
    def test_indicator_shows_off(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["canary_enabled"])
        self.assertContains(resp, "Canary Status")
        self.assertContains(resp, ">OFF<")

    # ── 5. Indicador muestra ON ───────────────────────────────────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({1, 2, 3}))
    def test_indicator_shows_on(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["canary_enabled"])
        self.assertContains(resp, ">ON<")

    # ── 6. Contador de cuentas correcto ──────────────────────────────────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({10, 20, 30, 40}))
    def test_account_count_correct(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.context["canary_account_count"], 4)
        self.assertContains(resp, "DEALING_DESK_EXPOSURE_ACCOUNT_IDS: 4")

    def test_zero_accounts_when_allowlist_empty(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset()):
            resp = self.client.get(self.url)

        self.assertEqual(resp.context["canary_account_count"], 0)

    # ── read-only: no form/button/action associated with the indicator ──
    def test_indicator_has_no_form_or_action(self):
        resp = self.client.get(self.url)

        content = resp.content.decode()
        canary_bar_start = content.index("canary-bar")
        canary_bar_snippet = content[canary_bar_start:canary_bar_start + 400]
        self.assertNotIn("<form", canary_bar_snippet)
        self.assertNotIn("<button", canary_bar_snippet)
