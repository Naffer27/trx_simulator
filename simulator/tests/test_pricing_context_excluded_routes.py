"""
simulator/tests/test_pricing_context_excluded_routes.py — SPREAD-02.

Confirms the two documented exclusions behave as "context not available"
(null), never fabricated data — neither admin.py nor population_engine.py
was touched by this block; these tests exist to prove that omission is
safe by construction (JSONField null=True, no default), not just assumed.

  - simulator/admin.py::force_close — superuser emergency close with an
    optional hand-typed price (or avg_price fallback); no real tick.
  - simulator/population_engine.py — populate_broker stress-test tool;
    synthetic prices, not FeedManager-derived; support/testing, not a
    production execution path.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from simulator.models import Position, Trade

from .factories import make_account, make_position

User = get_user_model()

_seq = 0


def _uid():
    global _seq
    _seq += 1
    return _seq


def _make_superuser():
    n = _uid()
    return User.objects.create_superuser(
        username=f"super_{n}", email=f"super_{n}@ex.com", password="pass",
    )


class AdminForceCloseExcludedTests(TestCase):
    def setUp(self):
        self.superuser = _make_superuser()
        self.account = make_account(account_type="RETAIL", tier=None, balance=Decimal("10000"))
        self.pos = make_position(account=self.account, symbol="EUR/USD", side="BUY",
                                  qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        self.client.force_login(self.superuser)
        self.url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[self.account.pk])

    def test_force_close_creates_trade_with_null_pricing_context(self):
        self.assertFalse(Position.objects.filter(account=self.account).count() == 0)
        response = self.client.post(self.url, {"action": "force_close", "symbol": "", "price": ""})
        self.assertIn(response.status_code, (302, 200))

        trade = Trade.objects.get(account=self.account, symbol="EUR/USD")
        self.assertIsNone(trade.pricing_context_open)
        self.assertIsNone(trade.pricing_context_close)
        self.assertFalse(Position.objects.filter(pk=self.pos.pk).exists())

    def test_force_close_with_hand_typed_price_still_null_context(self):
        """An admin-typed price is not a market tick — must not be
        represented as raw_bid/raw_ask."""
        response = self.client.post(
            self.url, {"action": "force_close", "symbol": "EUR/USD", "price": "1.25000"},
        )
        self.assertIn(response.status_code, (302, 200))
        trade = Trade.objects.get(account=self.account, symbol="EUR/USD")
        self.assertEqual(trade.exit_price, Decimal("1.25000"))
        self.assertIsNone(trade.pricing_context_open)
        self.assertIsNone(trade.pricing_context_close)


class PopulationEngineExcludedTests(TestCase):
    def test_close_position_creates_trade_with_null_pricing_context(self):
        import random
        from simulator.population_engine import SimulatedTrader

        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        pos = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
        )
        trader = SimulatedTrader.__new__(SimulatedTrader)
        trader.profile_name = "NORMAL"
        trader.account_id = account.pk
        trader.cfg = {"martingale": False, "mart_factor": 2.0, "mart_max": Decimal("1.0"),
                      "base_lot": Decimal("0.01")}
        trader._consec_losses = 0
        trader._cur_lot = Decimal("0.01")
        trader._rng = random.Random(0)

        def _fake_simulate_pnl(entry, side, symbol, qty):
            return 1.10500, 50.0

        trader._simulate_pnl = _fake_simulate_pnl
        trader._close_position(account, pos)

        trade = Trade.objects.get(account=account, symbol="EUR/USD")
        self.assertIsNone(trade.pricing_context_open)
        self.assertIsNone(trade.pricing_context_close)
