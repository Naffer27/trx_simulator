# simulator/tests/test_o6c1w_price_integrity_gate.py
"""
O.6c-1w — PRICE INTEGRITY / PLAUSIBILITY GATE IMPLEMENTATION.

Closes the residual gap O.6c-1t demonstrated: has_price()==True was
never sufficient on its own to trust a quote financially — a
BTCUSD-magnitude value (~63088) was observed under the EUR/USD key,
has_price() reporting True the whole time, producing a row P&L of
+$63,087,429.83 on a real EUR/USD position.

FeedManager.get_validated_quote(symbol) is now the single authoritative
point deciding whether a quote may be used for ANY financial decision.
It returns a frozen Quote (symbol, bid, ask, mid, timestamp, source) or
None — never a synthetic/fabricated substitute — after:

  1. Structural validation (_validate_quote_values, shared with Celery's
     _read_cached_price so the two processes can never define "valid"
     differently): both finite, both > 0, ask >= bid.
  2. Capa A plausibility (O.6c-1w-approved): within ±1 order of
     magnitude of SymbolSpec.base_price — this is what would have
     rejected the O.6c-1t incident (log10(63088/1.17) ~= 4.7).

Capa B (tick-to-tick deviation vs. last_valid_quote) is explicitly NOT
active — self._last_valid_quote is populated on every success (Capa B
architecture prepared) but nothing reads it back for a rejection
decision yet; that threshold remains an unmade business/risk decision.

consumers.py's _feed_close_price() now routes through
get_validated_quote() instead of has_price()+last_bid()/last_ask() —
its external contract (float | None) is unchanged, so all six of its
existing call sites (row P&L, header P&L/equity, manual close, Close
All, stopout, retail liquidation) are protected with zero changes to
those call sites themselves.

_check_tp_sl() (live WS SL/TP trigger) is DELIBERATELY untouched here —
O.6c-1u documented it as a separate price authority (client-marked-up
tick bid/ask, always freshly-arrived by construction) — resolving that
raw-vs-markup split is explicitly out of scope for O.6c-1w (see the
design report section 7). Only the DAEMON's SL/TP/stopout/margin-call
path (scan_positions_task, via Redis/_read_cached_price) is protected
here, alongside every _feed_close_price() call site.

Uses TransactionTestCase for anything touching @database_sync_to_async
methods or transaction.on_commit() — same established reasoning as
every O.6c-1o/1q/1s/1v test file in this suite.
"""
import math
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TransactionTestCase, SimpleTestCase

from simulator.consumers import TradingConsumer
from simulator.tasks import _read_cached_price
from market_data.feeds import (
    FeedManager, Quote, get_feed_manager, _validate_quote_values, _fallback_price,
)

from .factories import make_account, make_position

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


def _clear_symbol(symbol: str):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)
        feed._price_source.pop(symbol, None)
        feed._last_valid_quote.pop(symbol, None)
        feed._position_symbols.discard(symbol)
    feed._counts.pop(symbol, None)
    task = feed._tasks.pop(symbol, None)
    if task and not task.done():
        task.cancel()


def _seed_raw(symbol: str, bid, ask, *, fresh: bool = True, source: str = "live"):
    """Writes directly into the real, process-global FeedManager
    singleton's internal dicts — bypasses _broadcast() so a test can
    inject deliberately corrupt/implausible/malformed values that no
    real writer would ever produce, exactly what the O.6c-1w test
    matrix requires (contaminated cross-symbol price, NaN, Infinity,
    bid>ask, zero/negative)."""
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol]         = bid
        feed._asks[symbol]         = ask
        feed._prices[symbol]       = (bid + ask) / 2 if _validate_quote_values(symbol, bid, ask) else bid
        feed._price_ts[symbol]     = time.time() if fresh else (time.time() - 3600)
        feed._price_source[symbol] = source


def _bare_consumer(account_id, symbol="EUR/USD") -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.symbol = symbol
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c._last_db_sync = 0.0
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._feed = get_feed_manager()
    c.account = {
        "balance": 10000.0, "equity": 10000.0, "peak_balance": 10000.0,
        "pnl_unreal": 0.0, "margin_used": 0.0, "leverage": 50, "currency": "USD",
        "netting_mode": False, "status": "Activo", "account_type": "CHALLENGE",
        "tier": "", "profit_target": 0.0, "initial_balance": 0.0,
        "product_name": "", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


# ─────────────────────────────────────────────────────────────────────────
# 1/2/3 — correct quotes, per-instrument scale
# ─────────────────────────────────────────────────────────────────────────
class CorrectQuotesTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def test_scenario1_eurusd_correct(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        q = get_feed_manager().get_validated_quote("EUR/USD")
        self.assertIsNotNone(q)
        self.assertEqual(q.symbol, "EUR/USD")
        self.assertAlmostEqual(q.mid, 1.10010, places=5)

    def test_scenario2_btcusd_correct(self):
        _seed_raw("BTCUSD", 63012.60, 63012.80)
        q = get_feed_manager().get_validated_quote("BTCUSD")
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q.mid, 63012.70, places=2)

    def test_scenario3_both_simultaneous_no_cross_contamination(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        _seed_raw("BTCUSD", 63012.60, 63012.80)
        feed = get_feed_manager()
        qe = feed.get_validated_quote("EUR/USD")
        qb = feed.get_validated_quote("BTCUSD")
        self.assertAlmostEqual(qe.mid, 1.10010, places=5)
        self.assertAlmostEqual(qb.mid, 63012.70, places=2)


# ─────────────────────────────────────────────────────────────────────────
# 4/5 — cross-symbol contamination (the exact O.6c-1t reproduction)
# ─────────────────────────────────────────────────────────────────────────
class CrossSymbolContaminationTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("BTCUSD")

    def test_scenario4_btc_price_injected_into_eurusd_rejected(self):
        """The exact O.6c-1t incident, reproduced deliberately."""
        _seed_raw("EUR/USD", 63088.50, 63088.70)
        q = get_feed_manager().get_validated_quote("EUR/USD")
        self.assertIsNone(q)

    def test_scenario5_eur_price_injected_into_btcusd_rejected(self):
        _seed_raw("BTCUSD", 1.17000, 1.17020)
        q = get_feed_manager().get_validated_quote("BTCUSD")
        self.assertIsNone(q)

    def test_capa_a_boundary_within_one_order_of_magnitude_accepted(self):
        base = _fallback_price("EUR/USD")
        candidate = base * 9  # < 10x — still within ±1 order of magnitude
        _seed_raw("EUR/USD", candidate, candidate + 0.0002)
        q = get_feed_manager().get_validated_quote("EUR/USD")
        self.assertIsNotNone(q)

    def test_capa_a_boundary_beyond_one_order_of_magnitude_rejected(self):
        base = _fallback_price("EUR/USD")
        candidate = base * 11  # > 10x — outside ±1 order of magnitude
        _seed_raw("EUR/USD", candidate, candidate + 0.0002)
        q = get_feed_manager().get_validated_quote("EUR/USD")
        self.assertIsNone(q)


# ─────────────────────────────────────────────────────────────────────────
# 6-10 — structural corruption
# ─────────────────────────────────────────────────────────────────────────
class StructuralCorruptionTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario6_bid_greater_than_ask_rejected(self):
        _seed_raw("EUR/USD", 1.10020, 1.10000)  # inverted
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_scenario7_bid_zero_rejected(self):
        _seed_raw("EUR/USD", 0.0, 1.10020)
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_scenario8_ask_negative_rejected(self):
        _seed_raw("EUR/USD", 1.10000, -1.10020)
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_scenario9_nan_rejected(self):
        _seed_raw("EUR/USD", float("nan"), 1.10020)
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_scenario10_infinity_rejected(self):
        _seed_raw("EUR/USD", 1.10000, float("inf"))
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_negative_infinity_rejected(self):
        _seed_raw("EUR/USD", float("-inf"), 1.10020)
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_bool_is_not_a_valid_quote_value(self):
        """bool is a subclass of int in Python — explicitly excluded so
        a stray True/False can never masquerade as bid=1/bid=0."""
        self.assertFalse(_validate_quote_values("EUR/USD", True, 1.10020))


# ─────────────────────────────────────────────────────────────────────────
# 11/12/13 — staleness and absence
# ─────────────────────────────────────────────────────────────────────────
class StalenessAndAbsenceTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario11_stale_timestamp_rejected(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, fresh=False)
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_scenario12_missing_bid_rejected(self):
        feed = get_feed_manager()
        with feed._lock:
            feed._asks["EUR/USD"]     = 1.10020
            feed._price_ts["EUR/USD"] = time.time()
            # _bids["EUR/USD"] deliberately never set
        self.assertIsNone(feed.get_validated_quote("EUR/USD"))

    def test_scenario13_missing_ask_rejected(self):
        feed = get_feed_manager()
        with feed._lock:
            feed._bids["EUR/USD"]     = 1.10000
            feed._price_ts["EUR/USD"] = time.time()
        self.assertIsNone(feed.get_validated_quote("EUR/USD"))

    def test_absent_symbol_entirely_rejected(self):
        self.assertIsNone(get_feed_manager().get_validated_quote("EUR/USD"))

    def test_unknown_symbol_rejected_not_crashed(self):
        self.assertIsNone(get_feed_manager().get_validated_quote("NOTREAL/XYZ"))


# ─────────────────────────────────────────────────────────────────────────
# 14 — reconnect: last_valid_quote never leaks stale state across cycles
# ─────────────────────────────────────────────────────────────────────────
class ReconnectTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario14_stop_clears_last_valid_quote_no_stale_leak(self):
        feed = get_feed_manager()
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        q1 = feed.get_validated_quote("EUR/USD")
        self.assertIsNotNone(q1)
        self.assertIsNotNone(feed.last_valid_quote("EUR/USD"))

        feed._stop("EUR/USD")  # simulates feed teardown (e.g. reconnect cycle)
        self.assertIsNone(feed.last_valid_quote("EUR/USD"))
        self.assertIsNone(feed.get_validated_quote("EUR/USD"))  # no stale fabrication

        # Fresh tick after "reconnect" — works cleanly, no leftover corruption.
        _seed_raw("EUR/USD", 1.10500, 1.10520)
        q2 = feed.get_validated_quote("EUR/USD")
        self.assertIsNotNone(q2)
        self.assertAlmostEqual(q2.mid, 1.10510, places=5)

    def test_capa_b_architecture_prepared_but_not_applied(self):
        """last_valid_quote() is populated on success, but a wildly
        different NEXT valid-by-Capa-A tick is still accepted — Capa B
        (tick-to-tick deviation) is explicitly not wired in yet."""
        feed = get_feed_manager()
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        feed.get_validated_quote("EUR/USD")
        # A large but still Capa-A-plausible jump (< 10x base_price) —
        # would-be Capa B territory, deliberately still accepted today.
        _seed_raw("EUR/USD", 3.50000, 3.50020)
        q = feed.get_validated_quote("EUR/USD")
        self.assertIsNotNone(q)  # Capa B not active — O.6c-1w's approved scope


# ─────────────────────────────────────────────────────────────────────────
# 15 — independence from O.6c-1v's position-only keepalive
# ─────────────────────────────────────────────────────────────────────────
class PositionOnlyFeedIndependenceTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario15_validated_quote_works_with_zero_chart_subscribers(self):
        feed = get_feed_manager()
        feed.mark_position_symbol("EUR/USD")  # O.6c-1v — position-only, no chart
        self.assertEqual(feed._counts.get("EUR/USD", 0), 0)
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        q = feed.get_validated_quote("EUR/USD")
        self.assertIsNotNone(q)  # O.6c-1w gate independent of O.6c-1v's tracking


# ─────────────────────────────────────────────────────────────────────────
# 16 — multipanel: same singleton, same Quote
# ─────────────────────────────────────────────────────────────────────────
class MultipanelTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario16_two_panels_see_identical_validated_quote(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020)
        panel_a = get_feed_manager()
        panel_b = get_feed_manager()  # same process-wide singleton
        qa = panel_a.get_validated_quote("EUR/USD")
        qb = panel_b.get_validated_quote("EUR/USD")
        self.assertEqual(qa, qb)


# ─────────────────────────────────────────────────────────────────────────
# 17 — P&L never fictitious on an invalid quote
# ─────────────────────────────────────────────────────────────────────────
class PnlProtectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario17_row_and_header_pnl_exclude_contaminated_quote(self):
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{
            "id": 1, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.17017, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 63088.50, 63088.70)  # the O.6c-1t magnitude

        snap = panel._positions_snapshot()
        self.assertIsNone(snap[0]["pnl"])  # never the fictitious $63M

        total = panel._unrealized_pnl_total()
        self.assertEqual(total, 0.0)  # position excluded entirely, not zeroed-and-counted
        self.assertIn("EUR/USD", panel._unpriced_pnl_symbols)


# ─────────────────────────────────────────────────────────────────────────
# 18 — manual close rejected safely, no DB write, no fabricated price
# ─────────────────────────────────────────────────────────────────────────
class ManualCloseProtectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario18_order_close_rejects_on_contaminated_quote(self):
        from simulator.models import Position
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"),
                             avg_price=Decimal("1.17017"))
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.17017, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 63088.50, 63088.70)

        import asyncio
        asyncio.run(panel._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        sent = panel.send_json.call_args_list
        self.assertTrue(any(c.args[0].get("code") == "price_unavailable" for c in sent))
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())  # never closed
        self.assertEqual(len(panel._positions), 1)  # memory untouched


# ─────────────────────────────────────────────────────────────────────────
# 19 — SL/TP: daemon path protected; live WS path deliberately unchanged
# ─────────────────────────────────────────────────────────────────────────
class SlTpProtectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario19_daemon_sltp_price_read_rejects_contaminated_redis_value(self):
        """scan_positions_task's SL/TP/margin-call path reads via
        _read_cached_price() — protected by the same
        _validate_quote_values() as get_validated_quote()."""
        import redis as _redis
        from django.conf import settings
        url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        r.setex("trx:price:bid:EUR/USD", 60, "63088.50")
        r.setex("trx:price:ask:EUR/USD", 60, "63088.70")
        try:
            bid, ask = _read_cached_price("EUR/USD")
            self.assertIsNone(bid)
            self.assertIsNone(ask)
        finally:
            r.delete("trx:price:bid:EUR/USD", "trx:price:ask:EUR/USD")

    def test_live_ws_sltp_deliberately_unchanged_out_of_scope(self):
        """O.6c-1u documented _check_tp_sl() as a separate price
        authority (client-marked-up tick bid/ask) — resolving raw-vs-
        markup is explicitly out of scope for O.6c-1w (design report
        section 7). This test only confirms _check_tp_sl's source code
        was not touched by this microblock."""
        import inspect
        from simulator import consumers
        src = inspect.getsource(consumers.TradingConsumer._check_tp_sl)
        self.assertNotIn("get_validated_quote", src)
        self.assertNotIn("_feed_close_price", src)


# ─────────────────────────────────────────────────────────────────────────
# 20 — stopout skips a contaminated position, leaves it open, no fabrication
# ─────────────────────────────────────────────────────────────────────────
class StopoutProtectionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_scenario20_stopout_skips_contaminated_position(self):
        from simulator.models import Position
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"),
                             avg_price=Decimal("1.17017"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.17017, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 63088.50, 63088.70)  # contaminated

        import asyncio
        asyncio.run(panel._do_stopout())

        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())  # never closed
        self.assertEqual(len(panel._positions), 1)  # stays in the retry set


# ─────────────────────────────────────────────────────────────────────────
# 21 — Celery: every structural/plausibility case via the Redis path
# ─────────────────────────────────────────────────────────────────────────
class CeleryRedisProtectionTests(TransactionTestCase):
    """Writes directly to Redis (bypassing FeedManager entirely) to
    simulate a corrupted cache entry, then confirms _read_cached_price
    — the one function scan_positions_task uses — rejects it exactly
    like get_validated_quote() would for the same values in-process."""

    def _write_and_check(self, bid_str, ask_str, *, expect_valid):
        import redis as _redis
        from django.conf import settings
        url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        r.setex("trx:price:bid:EUR/USD", 60, bid_str)
        r.setex("trx:price:ask:EUR/USD", 60, ask_str)
        try:
            bid, ask = _read_cached_price("EUR/USD")
            if expect_valid:
                self.assertIsNotNone(bid)
                self.assertIsNotNone(ask)
            else:
                self.assertIsNone(bid)
                self.assertIsNone(ask)
        finally:
            r.delete("trx:price:bid:EUR/USD", "trx:price:ask:EUR/USD")

    def test_scenario21_valid_price_passes(self):
        self._write_and_check("1.10000", "1.10020", expect_valid=True)

    def test_scenario21_cross_symbol_contamination_rejected(self):
        self._write_and_check("63088.50", "63088.70", expect_valid=False)

    def test_scenario21_nan_rejected(self):
        self._write_and_check("nan", "1.10020", expect_valid=False)

    def test_scenario21_infinity_rejected(self):
        self._write_and_check("1.10000", "inf", expect_valid=False)

    def test_scenario21_bid_greater_than_ask_rejected(self):
        self._write_and_check("1.10020", "1.10000", expect_valid=False)

    def test_scenario21_zero_rejected(self):
        self._write_and_check("0", "1.10020", expect_valid=False)

    def test_scenario21_negative_rejected(self):
        self._write_and_check("-1.10000", "1.10020", expect_valid=False)

    def test_scenario21_garbage_string_never_raises(self):
        import redis as _redis
        from django.conf import settings
        url = (getattr(settings, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        r.setex("trx:price:bid:EUR/USD", 60, "not_a_number")
        r.setex("trx:price:ask:EUR/USD", 60, "1.10020")
        try:
            bid, ask = _read_cached_price("EUR/USD")  # float() raises internally — must not propagate
            self.assertIsNone(bid)
            self.assertIsNone(ask)
        finally:
            r.delete("trx:price:bid:EUR/USD", "trx:price:ask:EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# Shared validator — direct unit coverage (used by both processes)
# ─────────────────────────────────────────────────────────────────────────
class ValidateQuoteValuesUnitTests(SimpleTestCase):
    def test_valid_eurusd(self):
        self.assertTrue(_validate_quote_values("EUR/USD", 1.10000, 1.10020))

    def test_valid_btcusd(self):
        self.assertTrue(_validate_quote_values("BTCUSD", 63012.60, 63012.80))

    def test_unknown_symbol_false(self):
        self.assertFalse(_validate_quote_values("NOTREAL/XYZ", 1.0, 1.1))

    def test_none_values_false(self):
        self.assertFalse(_validate_quote_values("EUR/USD", None, 1.10020))
        self.assertFalse(_validate_quote_values("EUR/USD", 1.10000, None))
