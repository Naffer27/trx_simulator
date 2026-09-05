"""
simulator/tests/test_order_management_v2b_partial_close.py

ORDER-MANAGEMENT-V2B — Partial Close. Covers the design lock's §16 test
matrix (30 points) plus the authorization message's additional required
coverage (BrokerLedger/counterparty scoped to the partial realized_pnl,
one Trade+Ledger+BrokerLedger per realization, no financial duplication,
authoritative PnL under lock, full-close parity with the pre-V2B
behavior).

Both close executors are called directly, same established pattern as
test_atomic_margin_and_position_guard.py (_db_open_sync) and
test_order_management_v2a.py (_trigger_pending_order_core):

  _db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__
      — real async method, unwrapped to a plain sync callable on the
        test's own connection/thread. transaction.on_commit() callbacks
        it registers (ws_events publish, feed sync) never fire inside a
        TestCase's rolled-back transaction — same as every other direct
        _db_*_atomic test in this suite; not asserted on here.

  _close_position_sync — already a plain sync function (Celery-context),
      called directly, no unwrapping needed.

All open-position pricing avoids depending on other-position mark
prices — each test position is closed alone, so neither function's own
"other open positions" pricing branch is exercised except where a test
explicitly sets up two positions.
"""
from decimal import Decimal

from django.test import TestCase

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec
from simulator import pnl_engine
from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position

EURUSD_SPEC = get_spec("EUR/USD")


def pnl_engine_position_pnl_float(side, avg, close_px, qty, symbol, account_currency="USD"):
    """Thin, explicitly-named wrapper so blocker-reproduction tests read
    as "the real formula", matching the naming used throughout this
    file's docstrings/comments."""
    return pnl_engine.position_pnl_float(side, avg, close_px, qty, symbol, account_currency=account_currency)

_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _consumer(account_id, currency="USD", leverage=50):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {"currency": currency, "leverage": leverage}
    c._positions = []
    c._feed = get_feed_manager()
    return c


def _pos_mem(pos: Position) -> dict:
    """The pre-lock in-memory dict shape _order_close/_check_tp_sl/etc.
    pass as pos_mem — id/symbol/side/qty/avg/sl/tp/opened_at."""
    return {
        "id": pos.id, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


def _pre_lock_realized_estimate(pos: Position, close_px: float, close_qty) -> float:
    """Mirrors what a real caller (_order_close, scan_positions_task)
    actually computes BEFORE the lock: PnL over whatever qty this attempt
    intends to close. Authoritative only for a FULL close (design lock
    §3/refinement — see _db_close_position_atomic's own docstring: a
    full close trusts this parameter verbatim, byte-identical to
    pre-V2B); for a partial close this value is discarded and recomputed
    under lock, so its exact accuracy here doesn't matter, but computing
    it properly keeps every test call site realistic."""
    from simulator import pnl_engine
    qty = float(close_qty) if close_qty is not None else float(pos.qty)
    return pnl_engine.position_pnl_float(
        pos.side.lower(), float(pos.avg_price), close_px, qty, pos.symbol,
        account_currency="USD",
    )


def _close_async(pos: Position, close_px: float, close_qty=None, account_id=None):
    """Calls the real, unwrapped _db_close_position_atomic exactly like
    _order_close would: a real pre-lock realized_pnl estimate (used
    verbatim for a full close, discarded/recomputed under lock for a
    partial — see _db_close_position_atomic's own docstring)."""
    account_id = account_id or pos.account_id
    consumer = _consumer(account_id)
    pm = _pos_mem(pos)
    realized_estimate = _pre_lock_realized_estimate(pos, close_px, close_qty)
    return _db_close_sync(
        consumer, pm, close_px, "manual",
        realized_pnl=realized_estimate, new_balance=0.0, new_equity=0.0,
        close_qty=close_qty,
    )


def _close_sync_daemon(pos: Position, close_px: float, close_qty=None):
    """Calls the real _close_position_sync exactly like scan_positions_task
    would — same pre-lock estimate discipline as _close_async above."""
    pm = _pos_mem(pos)
    realized_estimate = _pre_lock_realized_estimate(pos, close_px, close_qty)
    return _close_position_sync(
        pm, pos.account_id, close_px, "daemon_sl",
        realized_pnl=realized_estimate, new_balance=0.0, new_equity=0.0,
        close_qty=close_qty,
    )


# ─────────────────────────────────────────────────────────────────────────
# FINANCIAL RACE BLOCKER — RETRACTED refinement + fix.
#
# An earlier revision of this block special-cased realized_pnl to trust
# the caller's pre-lock parameter verbatim whenever is_full was True
# (close_qty_d == fresh_qty), reasoning that a full close remained
# "binary" post-V2B. That reasoning was WRONG and has been retracted:
# is_full only means close_qty_d equals whatever fresh_qty happens to be
# NOW — which a concurrent PARTIAL close on the SAME Position can have
# already shrunk since a stale full-close caller computed its pre-lock
# estimate against the OLD, larger qty. Trusting that stale realized_pnl
# (and sourcing Trade.lot_size/entry_price from the same stale pos_mem)
# double-realized the portion the partial close had already correctly
# realized — a real, severe money-creation bug, reproduced and confirmed
# via direct DB reproduction (BUY manual + SELL daemon) before this fix.
#
# realized_pnl is now recomputed authoritatively under lock
# UNCONDITIONALLY — full and partial alike (the ORIGINAL design lock
# decision) — using close_qty_d/pos.avg_price (fresh), never pos_mem's
# pre-lock copies, in either branch. AuthoritativePnlBoundaryTests below
# now pins the CORRECTED contract (both branches ignore a wrong caller
# parameter); FinancialRaceBlockerTests reproduces the exact race
# permanently as a regression test.
# ─────────────────────────────────────────────────────────────────────────

class AuthoritativePnlBoundaryTests(TestCase):
    def test_full_close_ignores_caller_realized_pnl_uses_authoritative(self):
        """A full close must NOT trust a wrong caller-supplied
        realized_pnl — it must recompute authoritatively, exactly like
        partial does. This is the corrected contract; the previous
        version of this test asserted the opposite (now-retracted)
        behavior."""
        account = make_account(balance=Decimal("1000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        consumer = _consumer(account.id)
        pm = _pos_mem(pos)
        result = _db_close_sync(
            consumer, pm, 1.10500, "manual",
            realized_pnl=42.00,  # deliberately NOT the real 500.00
            new_balance=0.0, new_equity=0.0, close_qty=None,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        # Real PnL on the full 1.00 lot: (1.10500-1.10000)*1.00*100000 = 500.00
        self.assertAlmostEqual(result["realized_pnl"], 500.00, places=2)
        self.assertNotEqual(result["realized_pnl"], 42.00)
        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 1500.00, places=2)

    def test_partial_close_ignores_caller_realized_pnl_uses_authoritative(self):
        """The same guarantee for a genuine partial close: a deliberately
        wrong caller-supplied realized_pnl must be discarded and replaced
        by the fresh, lock-computed value."""
        account = make_account(balance=Decimal("1000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        consumer = _consumer(account.id)
        pm = _pos_mem(pos)
        result = _db_close_sync(
            consumer, pm, 1.10500, "manual",
            realized_pnl=999999.0,  # deliberately absurd
            new_balance=0.0, new_equity=0.0, close_qty=Decimal("0.30"),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        # Real PnL on 0.30 lots: (1.10500-1.10000)*0.30*100000 = 150.00
        self.assertAlmostEqual(result["realized_pnl"], 150.00, places=2)
        self.assertNotEqual(result["realized_pnl"], 999999.0)
        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 1150.00, places=2)


class FinancialRaceBlockerTests(TestCase):
    """Permanent regression coverage for the retracted refinement's bug:
    a FULL-close request whose pre-lock pos_mem/realized_pnl are STALE
    (computed before a concurrent PARTIAL close on the same Position
    committed) must close only the fresh remainder, use authoritative
    PnL for it, and never double-realize the portion the partial close
    already realized."""

    def test_async_buy_stale_full_after_partial_never_double_realizes(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        close_px_B, close_px_A = 1.10500, 1.10800

        # B: partial 0.70, commits first.
        pm_B = _pos_mem(pos)
        est_B = _pre_lock_realized_estimate(pos, close_px_B, Decimal("0.70"))
        result_B = _close_async(pos, close_px_B, close_qty=Decimal("0.70"))
        pos.refresh_from_db()
        self.assertTrue(result_B["ok"] and result_B["partial"])
        self.assertEqual(pos.qty, Decimal("0.300000"))

        # A: FULL close intent, STALE pos_mem (still shows the ORIGINAL
        # qty=1.00 — this connection never learned about B's partial).
        consumer = _consumer(account.id)
        pm_A_stale = _pos_mem(pos)
        pm_A_stale["qty"] = 1.00  # STALE — the pre-partial qty
        stale_realized_A = pnl_engine_position_pnl_float(
            "buy", 1.10000, close_px_A, 1.00, "EUR/USD",
        )
        result_A = _db_close_sync(
            consumer, pm_A_stale, close_px_A, "manual",
            realized_pnl=stale_realized_A,  # deliberately stale/too-large
            new_balance=0.0, new_equity=0.0, close_qty=None,
        )

        self.assertTrue(result_A["ok"])
        self.assertFalse(result_A["partial"])
        # fresh qty was 0.30 — must close exactly that, not the stale 1.00.
        self.assertEqual(result_A["close_qty"], 0.30)
        correct_A = pnl_engine_position_pnl_float("buy", 1.10000, close_px_A, 0.30, "EUR/USD")
        self.assertAlmostEqual(result_A["realized_pnl"], correct_A, places=2)
        self.assertNotAlmostEqual(result_A["realized_pnl"], stale_realized_A, places=2)

        trades = list(Trade.objects.filter(account=account).order_by("id"))
        self.assertEqual(len(trades), 2)
        self.assertEqual([float(t.lot_size) for t in trades], [0.70, 0.30])
        total_qty = sum(t.lot_size for t in trades)
        self.assertEqual(total_qty, Decimal("1.00"))  # never over-closes the original 1.00

        account.refresh_from_db()
        correct_B = pnl_engine_position_pnl_float("buy", 1.10000, close_px_B, 0.70, "EUR/USD")
        self.assertAlmostEqual(float(account.balance), 10000.00 + correct_B + correct_A, places=2)
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 0)

    def test_async_sell_stale_full_after_partial_never_double_realizes(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="SELL",
                             qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        close_px_B, close_px_A = 1.09500, 1.09000

        result_B = _close_async(pos, close_px_B, close_qty=Decimal("0.40"))
        pos.refresh_from_db()
        self.assertTrue(result_B["ok"] and result_B["partial"])
        self.assertEqual(pos.qty, Decimal("0.600000"))

        consumer = _consumer(account.id)
        pm_A_stale = _pos_mem(pos)
        pm_A_stale["qty"] = 1.00  # STALE
        stale_realized_A = pnl_engine_position_pnl_float("sell", 1.10000, close_px_A, 1.00, "EUR/USD")
        result_A = _db_close_sync(
            consumer, pm_A_stale, close_px_A, "manual",
            realized_pnl=stale_realized_A, new_balance=0.0, new_equity=0.0, close_qty=None,
        )

        self.assertTrue(result_A["ok"])
        self.assertFalse(result_A["partial"])
        self.assertEqual(result_A["close_qty"], 0.60)
        correct_A = pnl_engine_position_pnl_float("sell", 1.10000, close_px_A, 0.60, "EUR/USD")
        self.assertAlmostEqual(result_A["realized_pnl"], correct_A, places=2)
        self.assertNotAlmostEqual(result_A["realized_pnl"], stale_realized_A, places=2)

        trades = list(Trade.objects.filter(account=account).order_by("id"))
        total_qty = sum(t.lot_size for t in trades)
        self.assertEqual(total_qty, Decimal("1.00"))

    def test_sync_daemon_stale_full_after_partial_never_double_realizes(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        close_px_B, close_px_A = 1.10500, 1.10800

        result_B = _close_async(pos, close_px_B, close_qty=Decimal("0.70"))
        pos.refresh_from_db()
        self.assertTrue(result_B["ok"] and result_B["partial"])

        pm_A_stale = _pos_mem(pos)
        pm_A_stale["qty"] = 1.00  # STALE daemon snapshot, taken before B committed
        stale_realized_A = pnl_engine_position_pnl_float("buy", 1.10000, close_px_A, 1.00, "EUR/USD")
        result_A = _close_position_sync(
            pm_A_stale, account.id, close_px_A, "daemon_sl",
            realized_pnl=stale_realized_A, new_balance=0.0, new_equity=0.0, close_qty=None,
        )

        self.assertTrue(result_A["ok"])
        self.assertFalse(result_A["partial"])
        self.assertEqual(result_A["close_qty"], 0.30)
        correct_A = pnl_engine_position_pnl_float("buy", 1.10000, close_px_A, 0.30, "EUR/USD")
        self.assertAlmostEqual(result_A["realized_pnl"], correct_A, places=2)

        trades = list(Trade.objects.filter(account=account).order_by("id"))
        total_qty = sum(t.lot_size for t in trades)
        self.assertEqual(total_qty, Decimal("1.00"))

    def test_lot_size_sum_never_exceeds_original_position_qty_invariant(self):
        """General invariant, independent of staleness scenario: across
        ANY sequence of closes (partial or full) on the same original
        Position, SUM(Trade.lot_size) must never exceed the qty the
        Position was opened with."""
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        original_qty = pos.qty

        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.25"))
        pos.refresh_from_db()
        _close_async(pos, close_px=1.10200, close_qty=Decimal("0.25"))
        pos.refresh_from_db()
        _close_async(pos, close_px=1.10300, close_qty=None)  # closes fresh 0.50 remainder

        trades = Trade.objects.filter(account=account)
        total_qty = sum(t.lot_size for t in trades)
        self.assertLessEqual(total_qty, original_qty)
        self.assertEqual(total_qty, original_qty)  # exact, not just <=

    def test_ledger_and_brokerledger_amounts_match_authoritative_realized_pnl(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))

        result_B = _close_async(pos, close_px=1.10500, close_qty=Decimal("0.70"))
        pos.refresh_from_db()
        pm_A_stale = _pos_mem(pos)
        pm_A_stale["qty"] = 1.00
        consumer = _consumer(account.id)
        stale_realized_A = pnl_engine_position_pnl_float("buy", 1.10000, 1.10800, 1.00, "EUR/USD")
        result_A = _db_close_sync(
            consumer, pm_A_stale, 1.10800, "manual",
            realized_pnl=stale_realized_A, new_balance=0.0, new_equity=0.0, close_qty=None,
        )

        for result in (result_B, result_A):
            trade = Trade.objects.get(pk=result["trade_id"])
            ledger = LedgerEntry.objects.get(account=account, meta__trade_id=trade.id)
            bl = BrokerLedger.objects.get(source_trade=trade)
            self.assertAlmostEqual(float(ledger.amount), result["realized_pnl"], places=2)
            self.assertAlmostEqual(float(bl.amount), -result["realized_pnl"], places=2)
            # Exactly one of each per realization — never shared/duplicated.
            self.assertEqual(LedgerEntry.objects.filter(meta__trade_id=trade.id).count(), 1)
            self.assertEqual(BrokerLedger.objects.filter(source_trade=trade).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 1-8. BUY/SELL partial close — PnL, balance, qty, avg_price, SL/TP, lot_size
# ─────────────────────────────────────────────────────────────────────────

class BuySellPartialCloseTests(TestCase):
    def test_buy_partial_close_realizes_pnl_only_on_close_qty(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.00"), avg_price=Decimal("1.10000"),
                             sl=Decimal("1.09000"), tp=Decimal("1.12000"))
        result = _close_async(pos, close_px=1.10500, close_qty=Decimal("0.30"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertAlmostEqual(result["remaining_qty"], 0.70, places=6)

        # Expected PnL on 0.30 lots: (1.10500-1.10000)*0.30*100000 = 150.00
        self.assertAlmostEqual(result["realized_pnl"], 150.00, places=2)

        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.700000"))
        # avg_price/sl/tp untouched (design lock §3/§10)
        self.assertEqual(pos.avg_price, Decimal("1.10000"))
        self.assertEqual(pos.sl, Decimal("1.09000"))
        self.assertEqual(pos.tp, Decimal("1.12000"))

        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 10000.00 + 150.00, places=2)

        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("0.30"))
        self.assertEqual(trade.entry_price, Decimal("1.10000"))
        self.assertAlmostEqual(float(trade.profit_loss), 150.00, places=2)

    def test_sell_partial_close_realizes_pnl_only_on_close_qty(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="EUR/USD", side="SELL",
                             qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.09500, close_qty=Decimal("0.40"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        # SELL: (entry-close)*qty*contract_size = (1.10000-1.09500)*0.40*100000 = 200.00
        self.assertAlmostEqual(result["realized_pnl"], 200.00, places=2)
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.600000"))

    def test_trade_lot_size_matches_close_qty_not_original_position_qty(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.25"))
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("0.25"))
        self.assertNotEqual(trade.lot_size, Decimal("1.00"))


# ─────────────────────────────────────────────────────────────────────────
# 9. Multiple partial closes → multiple independent Trade rows
# ─────────────────────────────────────────────────────────────────────────

class MultiplePartialClosesTests(TestCase):
    def test_three_sequential_partials_produce_three_trades_summing_correctly(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))

        r1 = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.30"))
        pos.refresh_from_db()
        r2 = _close_async(pos, close_px=1.10200, close_qty=Decimal("0.20"))
        pos.refresh_from_db()
        r3 = _close_async(pos, close_px=1.10300, close_qty=Decimal("0.50"))

        self.assertTrue(r1["ok"] and r1["partial"])
        self.assertTrue(r2["ok"] and r2["partial"])
        self.assertTrue(r3["ok"])
        self.assertFalse(r3["partial"])  # 0.50 was the exact remainder -> full close

        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        trades = Trade.objects.filter(account=account).order_by("id")
        self.assertEqual(trades.count(), 3)
        self.assertEqual([t.lot_size for t in trades],
                          [Decimal("0.30"), Decimal("0.20"), Decimal("0.50")])
        self.assertEqual({t.id for t in trades}, {r1["trade_id"], r2["trade_id"], r3["trade_id"]})

        # No financial duplication: exactly one LedgerEntry EV_REALIZED and
        # one BrokerLedger COUNTERPARTY_PNL per Trade — never more, never
        # shared across the three realizations.
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 3,
        )
        for t in trades:
            self.assertEqual(BrokerLedger.objects.filter(source_trade=t).count(), 1)

    def test_final_partial_that_exhausts_qty_reuses_full_close_path(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.60"))
        pos.refresh_from_db()
        result = _close_async(pos, close_px=1.10200, close_qty=Decimal("0.40"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertIsNone(result["remaining_qty"])
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 11-12. Validation — qty > available / qty <= 0
# ─────────────────────────────────────────────────────────────────────────

class ValidationTests(TestCase):
    def test_close_qty_exceeding_position_is_rejected_never_coerced_to_full(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("0.50"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.80"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "qty_exceeds_position")
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.500000"))  # untouched
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)

    def test_close_qty_zero_is_rejected(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("0.50"))
        result = _close_async(pos, close_px=1.10100, close_qty=Decimal("0"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_qty")

    def test_close_qty_negative_is_rejected(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("0.50"))
        result = _close_async(pos, close_px=1.10100, close_qty=Decimal("-0.10"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_qty")
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 13. qty absent = full close, backward compatible
# ─────────────────────────────────────────────────────────────────────────

class BackwardCompatibilityTests(TestCase):
    def test_qty_absent_closes_in_full_exactly_like_pre_v2b(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.10500, close_qty=None)
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertIsNone(result["remaining_qty"])
        self.assertAlmostEqual(result["realized_pnl"], 500.00, places=2)  # full 1.00 lot
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 0)
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("1.00"))

    def test_close_qty_equal_to_full_qty_takes_the_full_close_branch(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("0.50"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.10500, close_qty=Decimal("0.50"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 0)

    def test_daemon_full_close_unchanged_when_close_qty_omitted(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        result = _close_sync_daemon(pos, close_px=1.09000, close_qty=None)
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertAlmostEqual(result["realized_pnl"], -1000.00, places=2)
        self.assertEqual(Position.objects.filter(pk=pos.pk).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 14-18. Concurrency — sequential calls against the real locked function
# prove the same select_for_update discipline true concurrency would.
# ─────────────────────────────────────────────────────────────────────────

class ConcurrencyTests(TestCase):
    def test_partial_vs_partial_never_realizes_more_than_available(self):
        """Design lock §6's own worked example: qty=1.00, A closes 0.70
        first, B's 0.50 request must then see the FRESH 0.30 remainder
        and be rejected — never 1.20 realized."""
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))

        result_a = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.70"))
        pos.refresh_from_db()
        result_b = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.50"))

        self.assertTrue(result_a["ok"])
        self.assertFalse(result_b["ok"])
        self.assertEqual(result_b["code"], "qty_exceeds_position")
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.300000"))
        total_lot = sum((t.lot_size for t in Trade.objects.filter(account=account)), Decimal("0"))
        self.assertEqual(total_lot, Decimal("0.70"))  # never 1.20

    def test_partial_then_full_close_request_closes_only_the_fresh_remainder(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.40"))
        pos.refresh_from_db()
        # A second "full close" request (qty omitted) closes whatever is
        # actually left (0.60), never the stale original 1.00.
        result = _close_async(pos, close_px=1.10200, close_qty=None)
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("0.60"))

    def test_full_close_after_partial_cannot_double_close(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.40"))
        pos.refresh_from_db()
        _close_async(pos, close_px=1.10200, close_qty=None)  # closes remaining 0.60 fully
        # A third attempt on the now-deleted row must see already_closed,
        # never fabricate a third Trade.
        result = _close_async(pos, close_px=1.10300, close_qty=None)
        self.assertTrue(result.get("already_closed"))
        self.assertEqual(Trade.objects.filter(account=account).count(), 2)

    def test_partial_vs_sltp_style_close_same_shared_executor(self):
        """SL/TP always calls the shared executor with close_qty=None
        (design lock §5) — after a partial close, that full-of-fresh-qty
        call closes exactly the remainder, never the stale original."""
        account = make_account(balance=Decimal("10000"), status="Activo")
        pos = make_position(account, side="BUY", qty=Decimal("1.00"),
                             avg_price=Decimal("1.10000"), sl=Decimal("1.09000"))
        _close_async(pos, close_px=1.10500, close_qty=Decimal("0.30"))
        pos.refresh_from_db()
        # Simulated SL/TP full-close call (close_qty=None, as _check_tp_sl
        # always does) against the fresh remainder.
        sltp_result = _close_async(pos, close_px=1.08900, close_qty=None)
        self.assertTrue(sltp_result["ok"])
        self.assertFalse(sltp_result["partial"])
        trade = Trade.objects.get(pk=sltp_result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("0.70"))

    def test_partial_vs_daemon_race_only_one_realizes_the_shared_remainder(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        live_result = _close_async(pos, close_px=1.10100, close_qty=Decimal("0.70"))
        pos.refresh_from_db()
        daemon_result = _close_sync_daemon(pos, close_px=1.09000, close_qty=Decimal("0.50"))
        self.assertTrue(live_result["ok"])
        self.assertFalse(daemon_result["ok"])
        self.assertEqual(daemon_result["code"], "qty_exceeds_position")


# ─────────────────────────────────────────────────────────────────────────
# 19. Netting after partial close
# ─────────────────────────────────────────────────────────────────────────

class NettingAfterPartialCloseTests(TestCase):
    def test_position_remainder_behaves_like_any_normal_position_for_margin(self):
        """No dedicated netting-merge test here (that engine is
        _db_open_position_atomic's, unchanged by V2B) — this proves the
        remainder Position is ordinary: fresh margin/PnL queries against
        it use its CURRENT (reduced) qty exactly like any other row."""
        from simulator import pnl_engine
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.30"))
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.700000"))
        margin, err = pnl_engine.calculate_required_margin(
            pos.symbol, float(pos.avg_price), float(pos.qty), 50, "USD",
        )
        self.assertIsNone(err)
        expected = 0.70 * 100000 * 1.10000 / 50
        self.assertAlmostEqual(margin, expected, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 20-22. Margin / exposure / snapshots reduce automatically
# ─────────────────────────────────────────────────────────────────────────

class MarginExposureSnapshotTests(TestCase):
    def test_margin_used_reduces_after_partial_close(self):
        from simulator import pnl_engine
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        margin_before, _ = pnl_engine.calculate_required_margin(
            pos.symbol, float(pos.avg_price), float(pos.qty), 50, "USD",
        )
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.40"))
        pos.refresh_from_db()
        margin_after, _ = pnl_engine.calculate_required_margin(
            pos.symbol, float(pos.avg_price), float(pos.qty), 50, "USD",
        )
        self.assertLess(margin_after, margin_before)
        self.assertAlmostEqual(margin_after, margin_before * 0.6, places=2)

    def test_snapshot_position_data_reflects_reduced_qty(self):
        from simulator.snapshots import _position_data
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        _close_async(pos, close_px=1.10100, close_qty=Decimal("0.40"))

        data = _position_data([account.id], {account.id: 50})
        self.assertAlmostEqual(
            float(data[account.id]["total"]), 0.60 * 100000 * 1.10000, delta=1.0,
        )


# ─────────────────────────────────────────────────────────────────────────
# 23-24. WS event semantics — ACTION_UPDATE (partial) vs ACTION_CLOSE (full)
# ─────────────────────────────────────────────────────────────────────────

class PositionChangedEventSemanticsTests(TestCase):
    def test_partial_close_publishes_action_update_with_trade_id(self):
        from simulator import ws_events
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        captured = {}

        def _fake_publish(account_id, *, action, position_id=None, **extra):
            captured["action"] = action
            captured.update(extra)

        orig = ws_events.publish_position_changed
        ws_events.publish_position_changed = _fake_publish
        try:
            consumer = _consumer(account.id)
            pm = _pos_mem(pos)
            # captureOnCommitCallbacks(execute=True) — the documented way
            # to force transaction.on_commit() hooks to actually run
            # inside a TestCase's own, normally-never-committed outer
            # transaction.
            with self.captureOnCommitCallbacks(execute=True):
                result = _db_close_sync(
                    consumer, pm, 1.10100, "manual",
                    realized_pnl=0.0, new_balance=0.0, new_equity=0.0,
                    close_qty=Decimal("0.30"),
                )
        finally:
            ws_events.publish_position_changed = orig

        self.assertTrue(result["partial"])
        self.assertEqual(captured.get("action"), ws_events.ACTION_UPDATE)
        self.assertEqual(captured.get("trade_id"), result["trade_id"])
        self.assertTrue(captured.get("partial"))
        self.assertAlmostEqual(captured.get("remaining_qty"), 0.70, places=6)

    def test_full_close_publishes_action_close(self):
        from simulator import ws_events
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        captured = {}

        def _fake_publish(account_id, *, action, position_id=None, **extra):
            captured["action"] = action
            captured.update(extra)

        orig = ws_events.publish_position_changed
        ws_events.publish_position_changed = _fake_publish
        try:
            consumer = _consumer(account.id)
            pm = _pos_mem(pos)
            with self.captureOnCommitCallbacks(execute=True):
                result = _db_close_sync(
                    consumer, pm, 1.10100, "manual",
                    realized_pnl=0.0, new_balance=0.0, new_equity=0.0,
                    close_qty=None,
                )
        finally:
            ws_events.publish_position_changed = orig

        self.assertFalse(result["partial"])
        self.assertEqual(captured.get("action"), ws_events.ACTION_CLOSE)
        self.assertEqual(captured.get("trade_id"), result["trade_id"])
        self.assertFalse(captured.get("partial"))
        self.assertIsNone(captured.get("remaining_qty"))


# ─────────────────────────────────────────────────────────────────────────
# 28. Admin — qty readonly
# ─────────────────────────────────────────────────────────────────────────

class AdminQtyReadonlyTests(TestCase):
    def test_position_admin_qty_is_readonly(self):
        from simulator.admin import PositionAdmin
        self.assertIn("qty", PositionAdmin.readonly_fields)


# ─────────────────────────────────────────────────────────────────────────
# 29. Decimal precision
# ─────────────────────────────────────────────────────────────────────────

class PrecisionTests(TestCase):
    def test_close_qty_decimal_precision_matches_position_qty_field(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, symbol="BTCUSD", side="BUY",
                             qty=Decimal("0.123456"), avg_price=Decimal("60000.00"))
        result = _close_async(pos, close_px=60100.00, close_qty=Decimal("0.023456"))
        self.assertTrue(result["ok"])
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.100000"))
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.lot_size, Decimal("0.023456"))

    def test_string_qty_is_parsed_as_decimal_never_float(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account, qty=Decimal("1.00"), avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.10100, close_qty="0.30")
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])


# ─────────────────────────────────────────────────────────────────────────
# 30. Full-close regression parity — the pre-V2B financial outcome is
# reproduced exactly (formula, Ledger, BrokerLedger, balance) when no
# qty is involved at all.
# ─────────────────────────────────────────────────────────────────────────

class FullCloseParityRegressionTests(TestCase):
    def test_full_close_produces_identical_ledger_and_brokerledger_shape(self):
        account = make_account(balance=Decimal("5000"))
        pos = make_position(account, side="SELL", qty=Decimal("0.20"),
                             avg_price=Decimal("1.10000"))
        result = _close_async(pos, close_px=1.09000, close_qty=None)

        self.assertTrue(result["ok"])
        expected_pnl = (1.10000 - 1.09000) * 0.20 * 100000
        self.assertAlmostEqual(result["realized_pnl"], expected_pnl, places=2)

        account.refresh_from_db()
        self.assertAlmostEqual(float(account.balance), 5000.00 + expected_pnl, places=2)

        ledger = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_REALIZED)
        self.assertAlmostEqual(float(ledger.amount), expected_pnl, places=2)

        trade = Trade.objects.get(pk=result["trade_id"])
        bl = BrokerLedger.objects.get(source_trade=trade)
        self.assertAlmostEqual(float(bl.amount), -expected_pnl, places=2)
