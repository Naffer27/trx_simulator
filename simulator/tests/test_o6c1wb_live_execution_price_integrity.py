# simulator/tests/test_o6c1wb_live_execution_price_integrity.py
"""
O.6c-1w-b — LIVE EXECUTION PRICE INTEGRITY COMPLETION.

O.6c-1w protected every accounting/close path (row/header P&L, manual
close, Close All, stopout, retail liquidation, Celery) via
FeedManager.get_validated_quote(). This block closes the two remaining
live-execution paths O.6c-1u/1w's own reports flagged as still reading
self._bid_state/self._ask_state (the broker_price()-marked-up,
per-connection cache) without ever checking the RAW input behind it:

  1. order:new / exec_price() — _order_new() now rejects with the same
     price_unavailable contract _order_close() already has if
     self._feed.get_validated_quote(sym) is None, BEFORE touching the
     rate limiter, margin guard, risk engine, or DB. Closes the
     cold-start gap (no tick has ever arrived for *sym* on this
     connection — e.g. right after connect()/change_symbol(),
     _seed_price_state() seeds directly from the raw feed, unvalidated).

  2. price_tick() (the ONE place broker_price() markup is applied, and
     the ONE call site of _check_tp_sl() — live WS SL/TP) now validates
     the RAW incoming tick (_validate_quote_values, same shared
     function get_validated_quote() and Celery's _read_cached_price()
     use) BEFORE calling broker_price() at all. An invalid raw tick is
     skipped entirely: self._bid_state/self._ask_state are not
     updated, and _check_tp_sl() is never invoked for it.

Sequence preserved exactly as required: RAW MARKET QUOTE -> VALIDATE ->
broker markup (unchanged formula/spread/policy) -> execution. Neither
change touches broker_price(), exec_price(), commission_for(), A/B
Book, LP, routing, or any model/migration.

Uses the shared _consumer() helper from test_order_ticket_sl_tp_
validation.py for order:new tests (already seeds a real, valid
EUR/USD quote in FeedManager as of O.6c-1w-b's fix to that helper —
see that file's own comment). Uses a lightweight bare consumer with
_check_tp_sl mocked out (same technique as test_commercial_pricing_
integration.py) for the price_tick()/SL-TP tests — proving the gate by
whether _check_tp_sl is invoked at all, never re-testing its own
pre-existing trigger logic.
"""
import time
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from django.test import TestCase, TransactionTestCase

from market_data.contracts import OrderPolicy
from simulator.consumers import TradingConsumer
from simulator.models import LedgerEntry, Position, Trade, TradingAccount
from market_data.feeds import get_feed_manager

from .factories import make_account
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run


def _open_normal_session():
    """TEST-INFRA — market-session policy is real-clock-driven (FIX-05A);
    these tests are about raw execution price-integrity gating, not
    market-session behavior, so pin it open. Same pattern as
    test_fix05a_financial_price_integrity.py::_open_normal_session()."""
    return patch(
        "market_data.sessions.service.evaluate_market_session_for_symbol",
        return_value=Mock(order_policy=OrderPolicy.OPEN_NORMAL),
    )


def _clear_symbol(symbol: str):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)
        feed._price_source.pop(symbol, None)
        feed._last_valid_quote.pop(symbol, None)


def _seed_raw(symbol: str, bid, ask, *, fresh: bool = True):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol]     = bid
        feed._asks[symbol]     = ask
        feed._prices[symbol]   = (bid + ask) / 2 if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid == bid and ask == ask else bid
        feed._price_ts[symbol] = time.time() if fresh else (time.time() - 3600)


def _tick_bare_consumer(**overrides) -> TradingConsumer:
    """Mirrors test_commercial_pricing_integration.py's _bare_consumer —
    _check_tp_sl/_on_tick/_recalc_account_and_push mocked out, so
    price_tick() can be exercised without the candle/agg/account
    machinery this test file has no interest in re-testing."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = "EUR/USD"
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {
        "balance": 10000.0, "spread_pips": 0.0,
        "commercial_pricing_fields": {},
    }
    for key, value in overrides.items():
        setattr(c, key, value)
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


def _tick(symbol: str, bid: float, ask: float, ts: int = 1_700_000_000,
          source: str = "finnhub") -> dict:
    """FIX-05A.1 — source defaults to a real provider name: every test in
    this file exercises structural/plausibility validation or SL/TP
    firing on an otherwise-valid tick, none of them are about the source
    gate itself (that has its own dedicated coverage in
    test_fix05a1_source_propagation.py) — a real default here keeps
    their original intent intact under the new fail-closed contract."""
    mid = round((bid + ask) / 2, 5)
    return {"symbol": symbol, "bid": bid, "ask": ask, "mid": mid, "time": ts, "source": source}


# ─────────────────────────────────────────────────────────────────────────
# order:new — cross-symbol / structural rejection, cold-start gap
# ─────────────────────────────────────────────────────────────────────────
class OrderNewRejectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def test_eurusd_rejects_btc_magnitude_injected(self):
        # _consumer() seeds a VALID EUR/USD quote as of its own O.6c-1w-b
        # fix — construct it first, then overwrite with the deliberately
        # corrupted value, exactly like a real feed corrupting an
        # already-live symbol would.
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 63088.50, 63088.70)  # the O.6c-1t magnitude
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))

        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)
        self.assertEqual(Trade.objects.filter(account=self.account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account_id=self.account.pk).count(), 0)
        self.assertEqual(
            TradingAccount.objects.get(pk=self.account.pk).balance,
            self.account.balance,
        )

    def test_btcusd_rejects_eur_magnitude_injected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("BTCUSD", 1.17000, 1.17020)  # EUR/USD magnitude under BTCUSD's key
        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.01}))

        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_nan_at_open_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", float("nan"), 1.10020)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(consumer)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_infinity_at_open_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.10000, float("inf"))
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(consumer)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_zero_at_open_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 0.0, 1.10020)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(consumer)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_stale_at_open_rejected(self):
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.10000, 1.10020, fresh=False)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(consumer)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_cold_start_no_tick_ever_arrived_rejected(self):
        """Simulates a connection right after connect()/change_symbol(),
        before the first live tick ever arrives — the exact gap this
        microblock closes. _consumer() itself now seeds a valid quote
        (O.6c-1w-b fix, needed so the ~10 other suites that share this
        helper keep working) — explicitly clear it here to reproduce the
        true cold-start state this test is named for."""
        consumer = _consumer(self.account.pk)
        _clear_symbol("EUR/USD")
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(consumer)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# order:new — valid quote: same execution price/markup as before
# ─────────────────────────────────────────────────────────────────────────
class OrderNewUnchangedOnValidQuoteTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))

    def test_valid_quote_same_execution_price_as_before(self):
        """_consumer() seeds real FeedManager EUR/USD at 1.1000/1.1000
        (O.6c-1w-b fix) AND self._bid_state/self._ask_state at the same
        1.1000/1.1000 (pre-existing). exec_price() logic is completely
        untouched by this microblock — this proves the new gate is a
        pure pass/fail check that never alters the computed price."""
        consumer = _consumer(self.account.pk)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))

        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=self.account)
        self.assertEqual(pos.avg_price, Decimal("1.1000"))  # ask, buy fills at ask — unchanged

    def test_valid_quote_sell_fills_at_bid_unchanged(self):
        consumer = _consumer(self.account.pk)
        with _open_normal_session():
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "sell", "qty": 0.01}))
        self.assertIsNone(_first_error(consumer))
        pos = Position.objects.get(account=self.account)
        self.assertEqual(pos.avg_price, Decimal("1.1000"))


# ─────────────────────────────────────────────────────────────────────────
# price_tick() -> _check_tp_sl(): cross-symbol quote never fires SL/TP
# ─────────────────────────────────────────────────────────────────────────
class LiveSlTpGateTests(TestCase):
    def test_cross_symbol_tick_never_calls_check_tp_sl(self):
        c = _tick_bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=63088.50, ask=63088.70)))
        c._check_tp_sl.assert_not_called()

    def test_nan_tick_never_calls_check_tp_sl(self):
        c = _tick_bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=float("nan"), ask=1.10020)))
        c._check_tp_sl.assert_not_called()

    def test_bid_greater_than_ask_tick_never_calls_check_tp_sl(self):
        c = _tick_bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=1.10020, ask=1.10000)))
        c._check_tp_sl.assert_not_called()

    def test_invalid_tick_never_updates_bid_ask_state(self):
        """The chart/exec_price() cache itself must never absorb a
        corrupted tick — proven directly, not just via _check_tp_sl."""
        c = _tick_bare_consumer()
        c._bid_state["EUR/USD"] = 1.09000  # a prior, legitimate value
        c._ask_state["EUR/USD"] = 1.09020
        _run(c.price_tick(_tick("EUR/USD", bid=63088.50, ask=63088.70)))
        self.assertEqual(c._bid_state["EUR/USD"], 1.09000)  # untouched
        self.assertEqual(c._ask_state["EUR/USD"], 1.09020)

    def test_valid_tick_still_calls_check_tp_sl_with_marked_up_bid_ask(self):
        """Regression: a genuinely valid tick must behave exactly as
        before — _check_tp_sl still fires, with the SAME broker_price()-
        marked-up values (markup_pips=0.0 here, so bid/ask pass through
        unchanged — the markup FORMULA itself is out of scope/untouched)."""
        c = _tick_bare_consumer()
        _run(c.price_tick(_tick("EUR/USD", bid=1.09990, ask=1.10010)))
        c._check_tp_sl.assert_called_once()
        args = c._check_tp_sl.call_args.args
        self.assertEqual(args[0], "EUR/USD")
        self.assertAlmostEqual(args[1], 1.09990, places=5)  # bid, unchanged formula
        self.assertAlmostEqual(args[2], 1.10010, places=5)  # ask, unchanged formula

    def test_valid_btcusd_tick_calls_check_tp_sl(self):
        c = _tick_bare_consumer(symbol="BTCUSD")
        _run(c.price_tick(_tick("BTCUSD", bid=63012.60, ask=63012.80)))
        c._check_tp_sl.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: an SL/TP that WOULD trigger on a corrupted tick does not
# ─────────────────────────────────────────────────────────────────────────
class SlTpDoesNotFireOnCorruptedTickTests(TestCase):
    """Uses the REAL _check_tp_sl (not mocked) — proves the position
    stays open through a full price_tick() call with a cross-symbol
    quote, even though the (huge, corrupted) trigger_px would trip both
    SL and TP if it were ever evaluated."""

    def _bare_consumer_real_sltp(self) -> TradingConsumer:
        c = TradingConsumer.__new__(TradingConsumer)
        c._db_account_id = None  # demo path — no DB writes needed for this assertion
        c.symbol = "EUR/USD"
        c._price_state = {}
        c._bid_state = {}
        c._ask_state = {}
        c._raw_bid_state = {}
        c._raw_ask_state = {}
        c._pricing_ts_state = {}
        c._pricing_snapshot_state = {}
        c._positions = [{
            "id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.10000, "sl": 1.09000, "tp": 1.11000, "opened_at": time.time(),
        }]
        c._daily_realized_pnl = 0.0
        c._daily_pnl_date = None
        c._feed = get_feed_manager()
        c.account = {
            "balance": 10000.0, "equity": 10000.0, "spread_pips": 0.0,
            "status": "Activo", "peak_balance": 10000.0, "leverage": 50,
            "netting_mode": False, "account_type": "CHALLENGE",
            "profit_target": 800.0, "initial_balance": 10000.0,
            "stopout_level": 50.0, "commercial_pricing_fields": {},
        }
        c.send_json = AsyncMock()
        c._on_tick = AsyncMock()
        c._recalc_account_and_push = AsyncMock()
        return c

    def test_sl_and_tp_both_within_range_of_corrupted_tick_but_neither_fires(self):
        c = self._bare_consumer_real_sltp()
        # 63088 blows past BOTH sl=1.09 and tp=1.11 if it were evaluated.
        _run(c.price_tick(_tick("EUR/USD", bid=63088.50, ask=63088.70)))
        self.assertEqual(len(c._positions), 1)  # never closed

    def test_valid_tp_hit_still_closes_normally(self):
        """Regression control: a genuinely valid tick that crosses TP
        still closes the position — the gate does not silently disable
        SL/TP altogether."""
        c = self._bare_consumer_real_sltp()
        _run(c.price_tick(_tick("EUR/USD", bid=1.11500, ask=1.11520)))  # crosses tp=1.11
        self.assertEqual(len(c._positions), 0)  # closed by TP as before
