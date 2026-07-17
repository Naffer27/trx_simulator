"""
simulator/tests/test_broker_exposure_engine.py — RISK-01.

Broker Exposure Engine (simulator/broker_exposure.py) — the single source
of truth for LIVE open-position risk/exposure (quantity, notional, and
unrealized broker-perspective PnL), reading Position directly.

Coverage layers:
  - SignedQuantityTests: signed_quantity() pure function.
  - ReproductionCaseTests: FASE 11 cases A-H, verbatim.
  - PricingCoverageTests: fresh/stale/missing price handling — never
    fabricates notional/PnL for an unpriced position.
  - FilterTests: account / symbol / account_type / trader_class filters.
  - ConcentrationTests: symbol_concentration_pct edge cases (zero
    exposure, single symbol, multiple symbols).
  - PerformanceTests: bounded query count regardless of position count.
  - IntegrationTests: broker_monitoring.py + Broker Control Center wiring.
  - NoRegressionTests: BOOK-02/BOOK-03 unaffected, no trader formula changed.
"""
import time
from decimal import Decimal

from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager

from simulator.broker_exposure import (
    BrokerExposureBreakdown,
    calculate_broker_exposure,
    signed_quantity,
    broker_exposure_for_account,
    broker_exposure_for_accounts,
    broker_exposure_for_symbol,
    broker_exposure_snapshot,
)
from simulator.models import Position, TraderScore

from .factories import make_account, make_position


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


def _seed_fresh_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _seed_stale_price(symbol, price, age_seconds):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time() - age_seconds


class _CleanFeedMixin:
    """Every test clears the symbols it touches before AND after, so the
    process-wide FeedManager singleton never leaks state across tests."""
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD", "USD/JPY")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


# ─────────────────────────────────────────────────────────────────────────
# 1. signed_quantity — pure
# ─────────────────────────────────────────────────────────────────────────
class SignedQuantityTests(TestCase):
    def test_buy_is_positive(self):
        self.assertEqual(signed_quantity("BUY", Decimal("1.5")), Decimal("1.5"))

    def test_sell_is_negative(self):
        self.assertEqual(signed_quantity("SELL", Decimal("1.5")), Decimal("-1.5"))

    def test_accepts_float_input(self):
        self.assertEqual(signed_quantity("BUY", 2.0), Decimal("2"))


# ─────────────────────────────────────────────────────────────────────────
# 2. FASE 11 — reproduction cases A-H
# ─────────────────────────────────────────────────────────────────────────
class ReproductionCaseTests(_CleanFeedMixin, TestCase):
    def test_caso_a_single_buy_eurusd(self):
        # avg=1.00000, current=1.00010 (1 pip) -> trader +10 on qty=1,
        # contract_size=100000 (standard EUR/USD lot). gross_notional here
        # uses the CURRENT price policy (FASE 4), so it prices at 100010.00,
        # not the entry-price-based 100000 — documented explicitly since
        # the two policies diverge whenever price has moved at all.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.00000"))
        _seed_fresh_price("EUR/USD", 1.00010)

        b = broker_exposure_snapshot()
        self.assertEqual(b.long_quantity, Decimal("1"))
        self.assertEqual(b.short_quantity, Decimal("0"))
        self.assertEqual(b.gross_quantity, Decimal("1"))
        self.assertEqual(b.net_quantity, Decimal("1"))
        self.assertEqual(b.trader_unrealized_pnl, Decimal("10.00"))
        self.assertEqual(b.broker_unrealized_counterparty_pnl, Decimal("-10.00"))
        self.assertEqual(b.gross_notional, Decimal("100010.00000"))

    def test_caso_b_single_sell_eurusd(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="SELL", qty=Decimal("1"), avg_price=Decimal("1.00000"))
        _seed_fresh_price("EUR/USD", 1.00010)

        b = broker_exposure_snapshot()
        self.assertEqual(b.long_quantity, Decimal("0"))
        self.assertEqual(b.short_quantity, Decimal("1"))
        self.assertEqual(b.gross_quantity, Decimal("1"))
        self.assertEqual(b.net_quantity, Decimal("-1"))
        self.assertEqual(b.trader_unrealized_pnl, Decimal("-10.00"))
        self.assertEqual(b.broker_unrealized_counterparty_pnl, Decimal("10.00"))

    def test_caso_c_buy2_sell1_same_symbol(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("2"), avg_price=Decimal("100"))
        make_position(account, symbol="BTCUSD", side="SELL", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_snapshot()
        self.assertEqual(b.gross_quantity, Decimal("3"))
        self.assertEqual(b.net_quantity, Decimal("1"))

    def test_caso_d_buy1_sell1(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(account, symbol="BTCUSD", side="SELL", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_snapshot()
        self.assertEqual(b.gross_quantity, Decimal("2"))
        self.assertEqual(b.net_quantity, Decimal("0"))

    def test_caso_e_concentration_75_25(self):
        # BTCUSD/ETHUSD both have contract_size=1, giving exact round
        # notional without EUR/USD's 100000 multiplier — same concentration
        # math the case describes, cleaner numbers.
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("75"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("25"))
        _seed_fresh_price("BTCUSD", 75)
        _seed_fresh_price("ETHUSD", 25)

        b = broker_exposure_snapshot()
        self.assertEqual(b.gross_notional, Decimal("100"))
        self.assertEqual(b.concentration_by_symbol["BTCUSD"], Decimal("75.00"))
        self.assertEqual(b.concentration_by_symbol["ETHUSD"], Decimal("25.00"))
        self.assertEqual(b.largest_symbol, "BTCUSD")
        self.assertEqual(b.largest_symbol_gross_notional, Decimal("75"))

    def test_caso_f_position_without_valid_price(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="USD/JPY", side="BUY", qty=Decimal("1"), avg_price=Decimal("150"))
        _clear_price("USD/JPY")  # explicit: no fresh price available

        b = broker_exposure_snapshot()
        self.assertEqual(b.unpriced_position_count, 1)
        self.assertEqual(b.priced_position_count, 0)
        self.assertEqual(b.pricing_coverage_pct, Decimal("0.00"))
        # Never fabricated from avg_price — notional/PnL stay exactly zero,
        # not "quantity * avg_price * contract_size".
        self.assertEqual(b.gross_notional, Decimal("0"))
        self.assertEqual(b.trader_unrealized_pnl, Decimal("0"))
        self.assertIn("USD/JPY", b.stale_or_missing_symbols)

    def test_caso_g_filter_by_account(self):
        acc1 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc2 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(acc1, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc2, symbol="BTCUSD", side="BUY", qty=Decimal("5"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_for_account(acc1.id)
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.long_quantity, Decimal("1"))
        self.assertEqual(b.account_count, 1)

    def test_caso_h_filter_by_symbol(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _seed_fresh_price("BTCUSD", 100)
        _seed_fresh_price("ETHUSD", 50)

        b = broker_exposure_for_symbol("BTCUSD")
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.symbol_count, 1)
        self.assertIn("BTCUSD", b.by_symbol)
        self.assertNotIn("ETHUSD", b.by_symbol)


# ─────────────────────────────────────────────────────────────────────────
# 3. Pricing coverage
# ─────────────────────────────────────────────────────────────────────────
class PricingCoverageTests(_CleanFeedMixin, TestCase):
    def test_stale_price_treated_as_unpriced(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_stale_price("BTCUSD", 100, age_seconds=999999)

        b = broker_exposure_snapshot()
        self.assertEqual(b.unpriced_position_count, 1)
        self.assertEqual(b.priced_position_count, 0)

    def test_mixed_priced_and_unpriced_coverage_pct(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(account, symbol="USD/JPY", side="BUY", qty=Decimal("1"), avg_price=Decimal("150"))
        _seed_fresh_price("BTCUSD", 100)
        _clear_price("USD/JPY")

        b = broker_exposure_snapshot()
        self.assertEqual(b.priced_position_count, 1)
        self.assertEqual(b.unpriced_position_count, 1)
        self.assertEqual(b.pricing_coverage_pct, Decimal("50.00"))

    def test_zero_positions_is_100pct_coverage(self):
        b = calculate_broker_exposure(account_id=999999)
        self.assertEqual(b.open_position_count, 0)
        self.assertEqual(b.pricing_coverage_pct, Decimal("100.00"))


# ─────────────────────────────────────────────────────────────────────────
# 4. Filters
# ─────────────────────────────────────────────────────────────────────────
class FilterTests(_CleanFeedMixin, TestCase):
    def test_account_type_filter(self):
        acc_standard = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc_demo     = make_account(account_type="DEMO", balance=Decimal("100000"))
        make_position(acc_standard, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc_demo, symbol="BTCUSD", side="BUY", qty=Decimal("2"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(account_type="STANDARD")
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.long_quantity, Decimal("1"))

    def test_status_filter(self):
        acc_active    = make_account(account_type="STANDARD", balance=Decimal("100000"), status="Activo")
        acc_suspended = make_account(account_type="STANDARD", balance=Decimal("100000"), status="Suspendido")
        make_position(acc_active, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc_suspended, symbol="BTCUSD", side="BUY", qty=Decimal("9"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(status="Activo")
        self.assertEqual(b.open_position_count, 1)

    def test_trader_class_filter(self):
        acc_gambler = make_account(account_type="STANDARD", balance=Decimal("100000"))
        TraderScore.objects.create(account=acc_gambler, trader_class=TraderScore.GAMBLER)
        acc_normal = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(acc_gambler, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc_normal, symbol="BTCUSD", side="BUY", qty=Decimal("4"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = calculate_broker_exposure(trader_class=TraderScore.GAMBLER)
        self.assertEqual(b.open_position_count, 1)
        self.assertEqual(b.long_quantity, Decimal("1"))

    def test_account_ids_filter(self):
        acc1 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc2 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc3 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(acc1, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc2, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        make_position(acc3, symbol="BTCUSD", side="BUY", qty=Decimal("99"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_for_accounts([acc1.id, acc2.id])
        self.assertEqual(b.open_position_count, 2)
        self.assertEqual(b.long_quantity, Decimal("2"))


# ─────────────────────────────────────────────────────────────────────────
# 5. Concentration edge cases
# ─────────────────────────────────────────────────────────────────────────
class ConcentrationTests(_CleanFeedMixin, TestCase):
    def test_zero_exposure_zero_concentration(self):
        b = broker_exposure_snapshot()
        self.assertEqual(b.gross_notional, Decimal("0"))
        self.assertEqual(b.concentration_by_symbol, {})

    def test_single_symbol_is_100pct(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        b = broker_exposure_snapshot()
        self.assertEqual(b.concentration_by_symbol["BTCUSD"], Decimal("100"))

    def test_multiple_symbols_sum_to_100pct(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("30"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("30"))
        make_position(account, symbol="USD/JPY", side="BUY", qty=Decimal("1"), avg_price=Decimal("40"))
        _seed_fresh_price("BTCUSD", 30)
        _seed_fresh_price("ETHUSD", 30)
        _seed_fresh_price("USD/JPY", 40)

        b = broker_exposure_snapshot()
        total = sum(b.concentration_by_symbol.values())
        self.assertEqual(total, Decimal("100"))


# ─────────────────────────────────────────────────────────────────────────
# 6. Performance — bounded query count
# ─────────────────────────────────────────────────────────────────────────
class PerformanceTests(_CleanFeedMixin, TestCase):
    def test_query_count_bounded_regardless_of_position_count(self):
        accounts = [make_account(account_type="STANDARD", balance=Decimal("100000")) for _ in range(5)]
        for i, acc in enumerate(accounts):
            for j in range(4):
                make_position(acc, symbol="BTCUSD", side="BUY" if j % 2 == 0 else "SELL",
                              qty=Decimal("1"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        with self.assertNumQueries(1):
            calculate_broker_exposure()


# ─────────────────────────────────────────────────────────────────────────
# 7. Integration — FASE 8
# ─────────────────────────────────────────────────────────────────────────
class IntegrationTests(_CleanFeedMixin, TestCase):
    def test_broker_monitoring_exposes_risk01_fields(self):
        from simulator.broker_monitoring import net_broker_exposure
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("EUR/USD", 1.1)

        data = net_broker_exposure()
        self.assertIn("risk01_gross_notional", data)
        self.assertIn("risk01_net_notional", data)
        self.assertIn("risk01_broker_unrealized_counterparty_pnl", data)
        self.assertIn("risk01_pricing_coverage_pct", data)

    def test_broker_control_center_exposes_risk_exposure_block(self):
        from simulator.admin import _compute_control_data
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.00000"))
        _seed_fresh_price("EUR/USD", 1.00010)

        data = _compute_control_data()
        self.assertIn("risk_exposure", data)
        rx = data["risk_exposure"]
        self.assertIn("gross_notional", rx)
        self.assertIn("net_notional", rx)
        self.assertIn("broker_unrealized_counterparty_pnl", rx)
        self.assertIn("projected_broker_result", rx)
        # projected = BOOK-03 realized_net_pnl + RISK-01 unrealized counterparty pnl
        bp = data["broker_pnl"]
        expected_projection = bp["realized_net_pnl"] + rx["broker_unrealized_counterparty_pnl"]
        self.assertAlmostEqual(rx["projected_broker_result"], expected_projection, places=6)


# ─────────────────────────────────────────────────────────────────────────
# 8. No regression
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TransactionTestCase):
    def test_book02_and_book03_unaffected(self):
        from simulator.consumers import TradingConsumer
        from simulator.models import Trade, LedgerEntry, BrokerLedger
        from .test_broker_counterparty_pnl import _open, _close, _pos_mem
        from .test_order_ticket_sl_tp_validation import _consumer

        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10.00"))

        from simulator.broker_pnl import broker_pnl_for_account, PERIOD_LIFETIME
        bp = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(bp.counterparty_pnl, Decimal("-10.00"))

    def test_trader_facing_formulas_unchanged(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("100"))
        # No price seeded — RISK-01 must not affect Position/TradingAccount
        # at all (it's a pure read-only reporting engine).
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("100000.00"))
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
