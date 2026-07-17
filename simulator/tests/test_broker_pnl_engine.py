"""
simulator/tests/test_broker_pnl_engine.py — BOOK-03.

Unified Broker P&L Engine (simulator/broker_pnl.py) — the single source
of truth for fee_revenue, counterparty_pnl, adjustments, and
broker_net_pnl, reading BrokerLedger only (never recomputing counterparty
PnL from Trade.profit_loss when a COUNTERPARTY_PNL row already exists).

Coverage layers:
  - PeriodWindowTests: utc_period_window() boundaries (today/24h/week/
    month/lifetime/custom), UTC-only.
  - BreakdownMathTests: FASE 10 reproduction cases A-F, exact Decimal math,
    no double counting.
  - CoverageTests: closed_trade_count / counterpart_entry_count /
    coverage_pct / historical_incomplete over a mix of BOOK-02-covered and
    pre-BOOK-02 (historical, no counterpart entry) Trades.
  - FilterTests: broker_pnl_for_account / broker_pnl_for_symbol.
  - TradeLevelTests: broker_pnl_for_trade — has_counterparty_entry, no
    silent estimation for historical Trades, fee rows never
    auto-attributed.
  - NoRegressionTests: BOOK-02's BrokerLedger writes unaffected; revenue
    dashboards (admin.py) still exclude COUNTERPARTY_PNL; Broker Control
    Center now sources broker_pnl from this engine; broker_monitoring
    exposes correctly-signed broker-perspective fields; no trader-facing
    formula changed.
"""
import time
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from simulator.broker_pnl import (
    BrokerPnLBreakdown,
    calculate_broker_pnl,
    broker_pnl_for_account,
    broker_pnl_for_period,
    broker_pnl_for_symbol,
    broker_pnl_for_trade,
    utc_period_window,
    PERIOD_TODAY, PERIOD_LAST_24H, PERIOD_WEEK, PERIOD_MONTH, PERIOD_LIFETIME, PERIOD_CUSTOM,
)
from simulator.models import BrokerLedger, LedgerEntry, Trade, TradingAccount

from .factories import make_account
from .test_broker_counterparty_pnl import _open, _close, _pos_mem
from .test_order_ticket_sl_tp_validation import _consumer


# ─────────────────────────────────────────────────────────────────────────
# 1. Period window — pure, no DB
# ─────────────────────────────────────────────────────────────────────────
class PeriodWindowTests(TestCase):
    def test_lifetime_has_no_bounds(self):
        start, end = utc_period_window(PERIOD_LIFETIME)
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_today_is_utc_midnight_to_now(self):
        now = timezone.datetime(2026, 3, 15, 14, 30, tzinfo=dt_timezone.utc)
        start, end = utc_period_window(PERIOD_TODAY, now=now)
        self.assertEqual(start, timezone.datetime(2026, 3, 15, 0, 0, 0, tzinfo=dt_timezone.utc))
        self.assertIsNone(end)

    def test_last_24h_is_rolling(self):
        now = timezone.datetime(2026, 3, 15, 14, 30, tzinfo=dt_timezone.utc)
        start, end = utc_period_window(PERIOD_LAST_24H, now=now)
        self.assertEqual(start, now - timedelta(hours=24))
        self.assertIsNone(end)

    def test_week_is_monday_utc_midnight(self):
        # 2026-03-18 is a Wednesday.
        now = timezone.datetime(2026, 3, 18, 9, 0, tzinfo=dt_timezone.utc)
        start, end = utc_period_window(PERIOD_WEEK, now=now)
        self.assertEqual(start, timezone.datetime(2026, 3, 16, 0, 0, 0, tzinfo=dt_timezone.utc))  # Monday
        self.assertIsNone(end)

    def test_month_is_first_of_month_utc_midnight(self):
        now = timezone.datetime(2026, 3, 18, 9, 0, tzinfo=dt_timezone.utc)
        start, end = utc_period_window(PERIOD_MONTH, now=now)
        self.assertEqual(start, timezone.datetime(2026, 3, 1, 0, 0, 0, tzinfo=dt_timezone.utc))
        self.assertIsNone(end)

    def test_custom_passes_through_verbatim(self):
        s = timezone.datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        e = timezone.datetime(2026, 2, 1, tzinfo=dt_timezone.utc)
        start, end = utc_period_window(PERIOD_CUSTOM, start=s, end=e)
        self.assertEqual(start, s)
        self.assertEqual(end, e)

    def test_unknown_period_raises(self):
        with self.assertRaises(ValueError):
            utc_period_window("not_a_real_period")


# ─────────────────────────────────────────────────────────────────────────
# 2. FASE 10 — reproduction cases A-F
# ─────────────────────────────────────────────────────────────────────────
class BreakdownMathTests(TransactionTestCase):
    def _open_and_close(self, account, *, commission=0.0, spread_pips=0.0, entry=100.0, close_px=100.0, pnl=0.0):
        c = _consumer(account.id)
        if spread_pips:
            c.account["spread_pips"] = spread_pips
        new_balance_after_open = 100000.0 - commission
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=entry, commission=commission, new_balance=new_balance_after_open)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, entry)
        new_balance_after_close = new_balance_after_open + pnl
        _close(c, pos_mem, close_px=close_px, realized_pnl=pnl, new_balance=new_balance_after_close, new_equity=new_balance_after_close)
        return Trade.objects.filter(account=account).latest("id")

    def test_caso_a_commission5_spread1_trader_plus8(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        self._open_and_close(account, commission=5.0, spread_pips=2.0, entry=100.0, close_px=108.0, pnl=8.0)

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b.commission, Decimal("5.00"))
        self.assertEqual(b.spread, Decimal("1.00"))
        self.assertEqual(b.fee_revenue, Decimal("6.00"))
        self.assertEqual(b.counterparty_pnl, Decimal("-8.00"))
        self.assertEqual(b.adjustments, Decimal("0.00"))
        self.assertEqual(b.broker_net_pnl, Decimal("-2.00"))

    def test_caso_b_commission5_spread1_trader_minus8(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        self._open_and_close(account, commission=5.0, spread_pips=2.0, entry=100.0, close_px=92.0, pnl=-8.0)

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b.fee_revenue, Decimal("6.00"))
        self.assertEqual(b.counterparty_pnl, Decimal("8.00"))
        self.assertEqual(b.broker_net_pnl, Decimal("14.00"))

    def test_caso_c_breakeven_commission5_spread1(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        self._open_and_close(account, commission=5.0, spread_pips=2.0, entry=100.0, close_px=100.0, pnl=0.0)

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b.fee_revenue, Decimal("6.00"))
        self.assertEqual(b.counterparty_pnl, Decimal("0.00"))
        self.assertEqual(b.broker_net_pnl, Decimal("6.00"))
        # Break-even still counts as covered (BOOK-02 always creates the row).
        self.assertEqual(b.coverage_pct, 100.0)

    def test_caso_d_adjustment_minus3_fee6_counterparty8(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        self._open_and_close(account, commission=5.0, spread_pips=2.0, entry=100.0, close_px=92.0, pnl=-8.0)
        BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_ADJUSTMENT, amount=Decimal("-3.00"),
            source_account=account, meta={"reason": "test_adjustment"},
        )

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b.fee_revenue, Decimal("6.00"))
        self.assertEqual(b.counterparty_pnl, Decimal("8.00"))
        self.assertEqual(b.adjustments, Decimal("-3.00"))
        self.assertEqual(b.broker_net_pnl, Decimal("11.00"))

    def test_caso_e_historical_without_counterpart_entry_excluded_and_flagged(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        # A genuine BOOK-02 close (has a counterpart entry)...
        self._open_and_close(account, commission=0.0, entry=100.0, close_px=105.0, pnl=5.0)
        # ...and a "historical" Trade created directly, bypassing the close
        # helper entirely — exactly like every pre-BOOK-02 row.
        Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.00"), entry_price=Decimal("100"),
            exit_price=Decimal("200"), profit_loss=Decimal("100.00"),
            closed_at=timezone.now(),
        )

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        # Only the covered trade's -5 counts — the historical +100 is NOT
        # inverted into -100 and added in.
        self.assertEqual(b.counterparty_pnl, Decimal("-5.00"))
        self.assertEqual(b.closed_trade_count, 2)
        self.assertEqual(b.counterpart_entry_count, 1)
        self.assertEqual(b.missing_counterpart_count, 1)
        self.assertEqual(b.coverage_pct, 50.0)
        self.assertTrue(b.historical_incomplete)

    def test_caso_f_mixed_accounts_and_symbols_exact_filters(self):
        acc1 = make_account(account_type="STANDARD", balance=Decimal("100000"))
        acc2 = make_account(account_type="STANDARD", balance=Decimal("100000"))

        # acc1: BTCUSD +10, EURUSD-style symbol via BTCUSD only (helper is
        # BTCUSD-only) — use two separate opens on acc1 to prove symbol
        # aggregation, and one on acc2 to prove account isolation.
        self._open_and_close(acc1, entry=100.0, close_px=110.0, pnl=10.0)
        self._open_and_close(acc1, entry=100.0, close_px=90.0, pnl=-10.0)
        self._open_and_close(acc2, entry=100.0, close_px=105.0, pnl=5.0)

        b1 = broker_pnl_for_account(acc1.id, period=PERIOD_LIFETIME)
        self.assertEqual(b1.counterparty_pnl, Decimal("0.00"))  # -10 + 10
        self.assertEqual(b1.closed_trade_count, 2)

        b2 = broker_pnl_for_account(acc2.id, period=PERIOD_LIFETIME)
        self.assertEqual(b2.counterparty_pnl, Decimal("-5.00"))
        self.assertEqual(b2.closed_trade_count, 1)

        by_symbol = broker_pnl_for_symbol("BTCUSD", period=PERIOD_LIFETIME)
        self.assertEqual(by_symbol.counterparty_pnl, Decimal("-5.00"))  # all three combined
        self.assertEqual(by_symbol.closed_trade_count, 3)

        missing_symbol = broker_pnl_for_symbol("EURUSD_DOES_NOT_EXIST", period=PERIOD_LIFETIME)
        self.assertEqual(missing_symbol.counterparty_pnl, Decimal("0.00"))
        self.assertEqual(missing_symbol.closed_trade_count, 0)
        self.assertEqual(missing_symbol.coverage_pct, 100.0)  # 0/0 -> no false incompleteness


# ─────────────────────────────────────────────────────────────────────────
# 3. Coverage — dedicated
# ─────────────────────────────────────────────────────────────────────────
class CoverageTests(TransactionTestCase):
    def test_zero_trades_has_100pct_coverage_not_incomplete(self):
        b = calculate_broker_pnl(period=PERIOD_LIFETIME, account_id=999999)
        self.assertEqual(b.closed_trade_count, 0)
        self.assertEqual(b.coverage_pct, 100.0)
        self.assertFalse(b.historical_incomplete)

    def test_full_coverage_when_all_trades_have_counterpart_entries(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b.coverage_pct, 100.0)
        self.assertFalse(b.historical_incomplete)
        self.assertIsNotNone(b.first_book02_trade_at)


# ─────────────────────────────────────────────────────────────────────────
# 4. Per-trade — FASE 7
# ─────────────────────────────────────────────────────────────────────────
class TradeLevelTests(TransactionTestCase):
    def test_covered_trade_reads_from_ledger_not_recomputed(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)
        trade = Trade.objects.get(account=account)

        result = broker_pnl_for_trade(trade)
        self.assertTrue(result.has_counterparty_entry)
        self.assertEqual(result.counterparty_pnl, Decimal("-10.00"))
        self.assertEqual(result.linked_fee_rows, [])  # commission/spread never link source_trade

    def test_historical_trade_has_no_entry_and_no_estimate(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        trade = Trade.objects.create(
            account=account, symbol="BTCUSD", trade_type="BUY",
            lot_size=Decimal("1.00"), entry_price=Decimal("100"),
            exit_price=Decimal("110"), profit_loss=Decimal("10.00"),
        )
        result = broker_pnl_for_trade(trade)
        self.assertFalse(result.has_counterparty_entry)
        self.assertIsNone(result.counterparty_pnl)  # NOT -10.00, NOT 0 — genuinely unknown


# ─────────────────────────────────────────────────────────────────────────
# 5. No regression
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TransactionTestCase):
    def test_book02_counterparty_writes_unaffected(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
        ledger = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_REALIZED)
        self.assertEqual(ledger.amount, Decimal("10.00"))
        cp = BrokerLedger.objects.get(source_account=account, revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        self.assertEqual(cp.amount, Decimal("-10.00"))
        self.assertEqual(cp.source_trade_id, trade.id)

    def test_broker_control_center_uses_engine_and_separates_realized_unrealized(self):
        from simulator.admin import _compute_control_data
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        data = _compute_control_data()
        bp = data["broker_pnl"]
        # These must come from the engine, not from BrokerRevenueSnapshot.
        self.assertEqual(round(bp["realized_fee_revenue"], 2), 6.0)
        self.assertEqual(round(bp["realized_counterparty_pnl"], 2), -8.0)
        self.assertEqual(round(bp["realized_net_pnl"], 2), -2.0)
        # Old field names, back-compat, now point at the CORRECT figures —
        # never fee-revenue-only under a "realized" label again.
        self.assertEqual(bp["realized"], bp["realized_net_pnl"])
        self.assertEqual(bp["net"], bp["projected_net_including_open_positions"])
        # Unrealized stays separate — never silently folded into realized_net_pnl.
        self.assertEqual(
            bp["projected_net_including_open_positions"],
            bp["realized_net_pnl"] + bp["unrealized_counterparty_risk"],
        )

    def test_revenue_dashboard_view_still_excludes_counterparty_pnl(self):
        # BOOK-02's exclude, unchanged by BOOK-03 (decision A: revenue
        # dashboards keep showing fee_revenue only).
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        c.account["spread_pips"] = 2.0
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=5.0, new_balance=99995.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=108.0, realized_pnl=8.0, new_balance=100003.0, new_equity=100003.0)

        qs = BrokerLedger.objects.exclude(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL).filter(source_account=account)
        self.assertEqual(qs.count(), 2)
        self.assertEqual(sum(r.amount for r in qs), Decimal("6.00"))

    def test_broker_monitoring_exposes_correctly_signed_fields(self):
        from simulator.broker_monitoring import broker_pnl_summary
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        # Trader wins +20 -> broker loses 20 -> broker_realized_pnl_all_time must be negative.
        _close(c, pos_mem, close_px=120.0, realized_pnl=20.0, new_balance=100020.0, new_equity=100020.0)

        summary = broker_pnl_summary()
        self.assertEqual(summary["realized_pnl_all_time"], 20.0)          # trader perspective, unchanged
        self.assertEqual(summary["broker_realized_pnl_all_time"], -20.0)  # broker perspective, correct sign
        self.assertIn("broker_pnl_coverage_pct", summary)
        self.assertIn("broker_pnl_historical_incomplete", summary)

    def test_no_double_counting_across_repeated_calls(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        b1 = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        b2 = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        self.assertEqual(b1.counterparty_pnl, b2.counterparty_pnl)
        self.assertEqual(b1.broker_net_pnl, b2.broker_net_pnl)

    def test_decimal_types_throughout_no_float_in_breakdown(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        b = broker_pnl_for_account(account.id, period=PERIOD_LIFETIME)
        for field_name in ("commission", "spread", "challenge_fee", "withdraw_fee",
                           "fee_revenue", "counterparty_pnl", "adjustments", "broker_net_pnl"):
            self.assertIsInstance(getattr(b, field_name), Decimal, field_name)

    def test_trader_facing_formulas_unchanged(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        c = _consumer(account.id)
        r = _open(c, symbol="BTCUSD", side="buy", qty=1.0, price=100.0, commission=0.0)
        pos_mem = _pos_mem(r, "BTCUSD", "buy", 1.0, 100.0)
        _close(c, pos_mem, close_px=110.0, realized_pnl=10.0, new_balance=100010.0, new_equity=100010.0)

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("100010.00"))
        trade = Trade.objects.get(account=account)
        self.assertEqual(trade.profit_loss, Decimal("10.00"))
