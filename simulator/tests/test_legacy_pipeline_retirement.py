"""
simulator/tests/test_legacy_pipeline_retirement.py — SPREAD-03 FASE B.

Confirms the retirement of the legacy HTTP order-writing pipeline
(views.py::trading_dashboard POST branch, views.py::api_orden) identified
in SPREAD-01: a second, independent pricing engine (hardcoded global
spread/slippage, a PnL calc missing contract_size, no commission, no risk
engine, no BrokerLedger) that had zero real consumers.

  - Both endpoints now respond safely (HTTP 410 Gone) instead of writing
    a Position/Trade — "route removida o read-only" per SPREAD-03.
  - Neither endpoint can create a Position/Trade/LedgerEntry anymore —
    "no puede saltarse risk/spread/ledger" is trivially true once it
    performs no writes at all.
  - trading_dashboard's GET path (the real, live dashboard) is completely
    unaffected — covered here directly, and already covered exhaustively
    by test_dashboard_intelligence.py / test_dashboard_challenge_lifecycle.py /
    test_dashboard_funded_section.py / test_dashboard_panel_mode.py (170
    tests, all GET, all still passing after this block).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from simulator.models import LedgerEntry, Position, Trade

from .factories import make_account, make_user

User = get_user_model()


class TradingDashboardPostRetiredTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(user=self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.client.force_login(self.user)

    def _post_order(self, url_name, **kwargs):
        url = reverse(url_name, **kwargs)
        return self.client.post(url, {
            "trade_type": "BUY", "symbol": "EUR/USD", "volume": "0.1",
        })

    def test_post_to_dashboard_returns_410_gone(self):
        response = self._post_order("simulator:dashboard")
        self.assertEqual(response.status_code, 410)

    def test_post_to_dashboard_account_returns_410_gone(self):
        response = self._post_order("simulator:dashboard_account", args=[self.account.pk])
        self.assertEqual(response.status_code, 410)

    def test_post_to_dashboard_alt_returns_410_gone(self):
        response = self._post_order("simulator:dashboard_alt")
        self.assertEqual(response.status_code, 410)

    def test_post_does_not_create_a_position(self):
        before = Position.objects.filter(account=self.account).count()
        self._post_order("simulator:dashboard_account", args=[self.account.pk])
        after = Position.objects.filter(account=self.account).count()
        self.assertEqual(before, after)

    def test_post_does_not_create_a_trade(self):
        before = Trade.objects.filter(account=self.account).count()
        self._post_order("simulator:dashboard_account", args=[self.account.pk])
        after = Trade.objects.filter(account=self.account).count()
        self.assertEqual(before, after)

    def test_post_does_not_create_a_ledger_entry(self):
        before = LedgerEntry.objects.filter(account=self.account).count()
        self._post_order("simulator:dashboard_account", args=[self.account.pk])
        after = LedgerEntry.objects.filter(account=self.account).count()
        self.assertEqual(before, after)

    def test_post_does_not_move_account_balance(self):
        before = self.account.balance
        self._post_order("simulator:dashboard_account", args=[self.account.pk])
        self.account.refresh_from_db()
        self.assertEqual(before, self.account.balance)

    def test_get_still_renders_the_real_dashboard(self):
        """The live dashboard GET path is untouched by this retirement."""
        response = self.client.get(reverse("simulator:dashboard_account", args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("open_trades_json", response.context)


class ApiOrdenRetiredTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(user=self.user, account_type="RETAIL", balance=Decimal("10000"))
        self.client.force_login(self.user)
        session = self.client.session
        session["account_id"] = self.account.pk
        session.save()

    def _post_order(self):
        import json
        return self.client.post(
            reverse("simulator:api_orden"),
            data=json.dumps({"tipo": "BUY", "symbol": "EUR/USD", "lot_size": "0.1"}),
            content_type="application/json",
        )

    def test_post_returns_410_gone(self):
        response = self._post_order()
        self.assertEqual(response.status_code, 410)

    def test_response_is_json_with_a_clear_error_code(self):
        response = self._post_order()
        payload = response.json()
        self.assertEqual(payload["error"], "endpoint_retired")

    def test_get_also_returns_410_not_a_405_or_crash(self):
        """No GET path ever existed for this endpoint (POST-only writer) —
        confirm it degrades the same safe way regardless of method."""
        response = self.client.get(reverse("simulator:api_orden"))
        self.assertEqual(response.status_code, 410)

    def test_post_does_not_create_a_position(self):
        before = Position.objects.filter(account=self.account).count()
        self._post_order()
        after = Position.objects.filter(account=self.account).count()
        self.assertEqual(before, after)

    def test_post_does_not_create_a_trade(self):
        before = Trade.objects.filter(account=self.account).count()
        self._post_order()
        after = Trade.objects.filter(account=self.account).count()
        self.assertEqual(before, after)

    def test_post_does_not_move_account_balance(self):
        before = self.account.balance
        self._post_order()
        self.account.refresh_from_db()
        self.assertEqual(before, self.account.balance)


class NoDuplicatePricingHelpersRemainTests(TestCase):
    """Confirms the dead pricing helpers that only the retired endpoints
    used were removed, not just orphaned — 'no dejar dos motores'."""

    def test_legacy_pricing_helpers_no_longer_exist(self):
        import simulator.views as views_mod
        for name in ("apply_spread_and_slippage", "get_base_price", "SYMBOL_BASE_PRICES", "SPREAD", "SLIPPAGE"):
            self.assertFalse(
                hasattr(views_mod, name),
                f"simulator.views.{name} should have been removed with the retired endpoints",
            )
