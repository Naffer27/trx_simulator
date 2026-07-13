"""
simulator/tests/test_pricing_context_daemon.py — SPREAD-02.

Covers the Celery daemon path (simulator/tasks.py::scan_positions_task),
confirmed in this block to be a real production route (offline SL/TP +
stopout/margin-call for accounts without an active WebSocket connection):
  - _daemon_pricing_context(): raw == executable (daemon applies no broker
    markup — pre-existing, unchanged behavior), base/account markup still
    captured for audit, never raises.
  - scan_positions_task's per-position SL/TP branch persists a
    pricing_context_close with the daemon_tp/daemon_sl profile, and copies
    the Position's pricing_context into Trade.pricing_context_open.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from simulator.models import Position, Trade
from simulator.tasks import _daemon_pricing_context, scan_positions_task
from simulator import pricing_context as pc

from .factories import make_account, make_spread_config


def _scan():
    return scan_positions_task.apply().get()


def _price_mock(symbol_prices: dict):
    def _mock(symbol):
        return symbol_prices.get(symbol, (None, None))
    return _mock


class DaemonPricingContextUnitTests(TestCase):
    """SPREAD-02b (INVARIANTE 2): the daemon never calls broker_price(), so
    the persisted context must say so honestly — executable == raw, and
    base/account/effective spread hardcoded to 0.0, never a
    BrokerSpreadConfig/account-snapshot read (see
    test_pricing_context_forensic_invariants.py for the full invariant
    tests, including end-to-end coverage with a real config present)."""

    def test_executable_equals_raw_no_markup_applied(self):
        ctx = _daemon_pricing_context("EUR/USD", 1.0999, 1.1001, profile="daemon_tp")
        self.assertEqual(ctx["raw_bid"], ctx["executable_bid"])
        self.assertEqual(ctx["raw_ask"], ctx["executable_ask"])

    def test_spread_fields_are_always_zero_not_read_from_config(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        ctx = _daemon_pricing_context("EUR/USD", 1.0999, 1.1001, profile="daemon_sl")
        self.assertEqual(ctx["base_spread_pips"], 0.0)
        self.assertEqual(ctx["account_markup_pips"], 0.0)
        self.assertEqual(ctx["effective_spread_pips"], 0.0)

    def test_profile_is_preserved_verbatim(self):
        ctx = _daemon_pricing_context("BTCUSD", 82000.0, 82015.0, profile=pc.PROFILE_DAEMON_STOPOUT)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_DAEMON_STOPOUT)

    def test_never_raises_on_total_failure(self):
        with patch("simulator.pricing_context.build_pricing_context", side_effect=RuntimeError("boom")):
            ctx = _daemon_pricing_context("EUR/USD", 1.1, 1.1002, profile="daemon_tp")
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_CAPTURE_FAILED)


class ScanPositionsTaskTpSlPersistenceTests(TestCase):
    def setUp(self):
        self.account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        self.open_ctx = pc.build_pricing_context(
            raw_bid=1.09990, raw_ask=1.10010, base_spread_pips=1.5,
            pricing_profile=pc.PROFILE_WS_OPEN,
        )

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_tp_close_persists_pricing_context(self, mock_price):
        pos = Position.objects.create(
            account=self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"), tp=Decimal("1.10500"),
            pricing_context=self.open_ctx,
        )
        mock_price.side_effect = _price_mock({"EUR/USD": (1.10600, 1.10620)})

        result = _scan()

        self.assertEqual(result["closed"], 1)
        trade = Trade.objects.get(account=self.account, symbol="EUR/USD")
        self.assertEqual(trade.pricing_context_open, self.open_ctx)
        self.assertEqual(trade.pricing_context_close["pricing_profile"], pc.PROFILE_DAEMON_TP)
        self.assertEqual(trade.pricing_context_close["raw_bid"], 1.10600)
        self.assertEqual(trade.pricing_context_close["executable_bid"], 1.10600)
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_sl_close_persists_pricing_context(self, mock_price):
        Position.objects.create(
            account=self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"), sl=Decimal("1.09500"),
            pricing_context=self.open_ctx,
        )
        mock_price.side_effect = _price_mock({"EUR/USD": (1.09400, 1.09420)})

        result = _scan()

        self.assertEqual(result["closed"], 1)
        trade = Trade.objects.get(account=self.account, symbol="EUR/USD")
        self.assertEqual(trade.pricing_context_close["pricing_profile"], pc.PROFILE_DAEMON_SL)
        self.assertEqual(trade.pricing_context_open["base_spread_pips"], 1.5)

    @patch("simulator.tasks._read_cached_price")
    def test_missing_price_skips_without_creating_a_trade(self, mock_price):
        Position.objects.create(
            account=self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"), tp=Decimal("1.10500"),
            pricing_context=self.open_ctx,
        )
        mock_price.side_effect = _price_mock({})  # no cached price at all

        result = _scan()

        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["skipped_stale"], 1)
        self.assertFalse(Trade.objects.filter(account=self.account).exists())

    @patch("simulator.tasks._read_cached_price")
    def test_no_trigger_leaves_position_open_with_context_intact(self, mock_price):
        pos = Position.objects.create(
            account=self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
            sl=Decimal("1.00000"), tp=Decimal("2.00000"),
            pricing_context=self.open_ctx,
        )
        mock_price.side_effect = _price_mock({"EUR/USD": (1.10010, 1.10030)})

        result = _scan()

        self.assertEqual(result["closed"], 0)
        pos.refresh_from_db()
        self.assertEqual(pos.pricing_context, self.open_ctx)
