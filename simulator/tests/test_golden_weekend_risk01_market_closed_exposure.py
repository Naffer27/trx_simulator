"""
simulator/tests/test_golden_weekend_risk01_market_closed_exposure.py
GOLDEN-WEEKEND-RISK-01 — MARKET_CLOSED_FROZEN exposure valuation.

Problem this closes: a Forex Position with no fresh live quote because its
market is legitimately CLOSED (weekend/holiday/maintenance) was previously
indistinguishable from a genuinely broken feed during open market hours —
both were "unpriced", excluded from broker exposure, and could drag
pricing_coverage_pct below 100%, blocking unrelated NEW orders (including
24/7 Crypto) broker-wide. See Design Lock GOLDEN-WEEKEND-RISK-01.

Two independent pieces under test:
  1. market_data/feeds.py — the durable last-known-price WRITE path
     (FeedManager._update_price_state -> throttled upsert into
     simulator.models.LastKnownMarketPrice), provenance-gated, monotonic
     by observed_at, never crashes the feed loop on a DB failure.
  2. simulator/broker_exposure.py — the READ path
     (_resolve_symbol_prices): FRESH / MARKET_CLOSED_FROZEN / STALE /
     UNAVAILABLE, consulting the durable row ONLY when
     evaluate_market_session_for_symbol() confirms OrderPolicy.MARKET_CLOSED
     — never as a generic stale-feed fallback.

No JS/frontend surface touched. No change to the per-symbol trading gate
in consumers.py (untouched, not imported here).
"""
import asyncio
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from market_data.contracts import OrderPolicy
from market_data.feeds import (
    FeedManager,
    _DURABLE_PRICE_VALID_SOURCES,
    _DURABLE_PRICE_WRITE_INTERVAL_SECS,
    _write_durable_price_sync,
    get_feed_manager,
)
from market_data.sessions.models import CalendarId, MarketSessionResult, MarketSessionState, SessionReasonCode

from simulator.broker_exposure import calculate_broker_exposure
from simulator.models import LastKnownMarketPrice

from .factories import make_account, make_position


def _run(coro):
    return asyncio.run(coro)


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


def make_session(symbol="GBP/USD", **overrides):
    """Same shape/convention as test_feeds_market_session_integration.py's
    make_session_result() — a real MarketSessionResult, never a stub."""
    defaults = dict(
        canonical_symbol=symbol, calendar_id=CalendarId.FOREX_24_5,
        state=MarketSessionState.WEEKEND, order_policy=OrderPolicy.MARKET_CLOSED,
        evaluated_at=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc),
        reason_code=SessionReasonCode.WEEKEND_CLOSURE, timezone="UTC",
    )
    defaults.update(overrides)
    return MarketSessionResult(**defaults)


_SESSION_PATCH_TARGET = "market_data.sessions.service.evaluate_market_session_for_symbol"


class _CleanFeedMixin:
    """Same discipline as test_broker_exposure_engine.py's _CleanFeedMixin —
    the FeedManager singleton is process-wide, never let a test leak state."""
    SYMBOLS = ("EUR/USD", "GBP/USD", "BTCUSD", "ETHUSD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


# ─────────────────────────────────────────────────────────────────────────
# A. Write path (market_data/feeds.py) — provenance, throttle, monotonicity
# ─────────────────────────────────────────────────────────────────────────
class DurablePriceWritePathTests(TestCase):

    def test_valid_provider_creates_row(self):
        """Case 24 — DB row created from a valid live provider."""
        now = time.time()
        _write_durable_price_sync("EUR/USD", 1.1000, 1.1002, 1.1001, "massive",
                                   datetime.fromtimestamp(now, tz=dt_timezone.utc))
        row = LastKnownMarketPrice.objects.get(symbol="EUR/USD")
        self.assertEqual(row.source, "massive")
        self.assertEqual(row.mid, Decimal("1.1001"))

    def test_invalid_source_never_persists(self):
        """'sim' is explicitly excluded — never written, not even a first row."""
        _write_durable_price_sync("EUR/USD", 1.1000, 1.1002, 1.1001, "sim",
                                   datetime.now(tz=dt_timezone.utc))
        self.assertFalse(LastKnownMarketPrice.objects.filter(symbol="EUR/USD").exists())

    def test_unknown_source_never_persists(self):
        _write_durable_price_sync("EUR/USD", 1.1000, 1.1002, 1.1001, "some_new_provider",
                                   datetime.now(tz=dt_timezone.utc))
        self.assertFalse(LastKnownMarketPrice.objects.filter(symbol="EUR/USD").exists())

    def test_unknown_provider_does_not_overwrite_valid_durable_row(self):
        """Case 25 — a valid row already exists; an invalid-source write must
        never touch it (the invalid write is rejected before any query)."""
        t0 = datetime(2026, 9, 5, 10, 0, tzinfo=dt_timezone.utc)
        _write_durable_price_sync("EUR/USD", 1.1000, 1.1002, 1.1001, "massive", t0)
        _write_durable_price_sync("EUR/USD", 9.9999, 9.9999, 9.9999, "sim",
                                   datetime(2026, 9, 5, 11, 0, tzinfo=dt_timezone.utc))
        row = LastKnownMarketPrice.objects.get(symbol="EUR/USD")
        self.assertEqual(row.mid, Decimal("1.1001"))
        self.assertEqual(row.source, "massive")

    def test_newer_valid_price_replaces_older(self):
        """Case 27."""
        t0 = datetime(2026, 9, 5, 10, 0, tzinfo=dt_timezone.utc)
        t1 = datetime(2026, 9, 5, 10, 5, tzinfo=dt_timezone.utc)
        _write_durable_price_sync("GBP/USD", 1.3500, 1.3502, 1.3501, "massive", t0)
        _write_durable_price_sync("GBP/USD", 1.3600, 1.3602, 1.3601, "massive", t1)
        row = LastKnownMarketPrice.objects.get(symbol="GBP/USD")
        self.assertEqual(row.mid, Decimal("1.3601"))
        self.assertEqual(row.observed_at, t1)

    def test_older_out_of_order_write_never_overwrites_newer(self):
        """Case 28 — a delayed async write for an OLDER quote completing
        AFTER a newer one must not regress the durable row."""
        t_new = datetime(2026, 9, 5, 10, 5, tzinfo=dt_timezone.utc)
        t_old = datetime(2026, 9, 5, 10, 0, tzinfo=dt_timezone.utc)
        _write_durable_price_sync("GBP/USD", 1.3600, 1.3602, 1.3601, "massive", t_new)
        _write_durable_price_sync("GBP/USD", 1.3500, 1.3502, 1.3501, "massive", t_old)
        row = LastKnownMarketPrice.objects.get(symbol="GBP/USD")
        self.assertEqual(row.mid, Decimal("1.3601"), "an out-of-order older write must not win")
        self.assertEqual(row.observed_at, t_new)

    def test_db_failure_does_not_raise_and_does_not_fake_persist(self):
        """A DB outage must never crash the feed loop, and must never be
        reported as if the price had been durably persisted."""
        with patch(
            "simulator.models.LastKnownMarketPrice.objects.filter",
            side_effect=Exception("db down"),
        ):
            _write_durable_price_sync("EUR/USD", 1.1000, 1.1002, 1.1001, "massive",
                                       datetime.now(tz=dt_timezone.utc))
        self.assertFalse(LastKnownMarketPrice.objects.filter(symbol="EUR/USD").exists())

    def test_throttle_limits_writes_to_one_per_interval(self):
        """Case 26 — many ticks in a burst must not cause unbounded DB
        writes: FeedManager._update_price_state() throttles to at most one
        durable write per symbol per _DURABLE_PRICE_WRITE_INTERVAL_SECS."""
        fm = FeedManager()
        with patch("market_data.feeds._write_durable_price") as mock_write:
            for _ in range(20):
                _run(fm._update_price_state("EUR/USD", 1.1000, 1.1002, source="massive"))
            self.assertEqual(mock_write.call_count, 1)

    def test_throttle_is_per_symbol(self):
        fm = FeedManager()
        with patch("market_data.feeds._write_durable_price") as mock_write:
            _run(fm._update_price_state("EUR/USD", 1.1000, 1.1002, source="massive"))
            _run(fm._update_price_state("GBP/USD", 1.3500, 1.3502, source="massive"))
            self.assertEqual(mock_write.call_count, 2)

    def test_sim_source_never_dispatches_durable_write(self):
        fm = FeedManager()
        with patch("market_data.feeds._write_durable_price") as mock_write:
            _run(fm._update_price_state("EUR/USD", 1.1000, 1.1002, source="sim"))
            mock_write.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# B. Read path (simulator/broker_exposure.py) — FRESH/FROZEN/STALE/UNAVAILABLE
# ─────────────────────────────────────────────────────────────────────────
class BrokerExposureMarketClosedFrozenTests(_CleanFeedMixin, TestCase):

    # ── 1-2: baseline OPEN behavior, unchanged ──────────────────────────
    def test_open_market_fresh_price_is_priced(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        _seed_fresh_price("EUR/USD", 1.1050)
        result = calculate_broker_exposure(account_id=account.id)
        self.assertEqual(result.pricing_coverage_pct, Decimal("100.00"))
        self.assertIn("EUR/USD", result.fresh_symbols)
        self.assertEqual(result.unpriced_position_count, 0)

    def test_open_market_no_fresh_price_is_stale_fail_closed(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        session = make_session(symbol="EUR/USD", state=MarketSessionState.OPEN,
                                order_policy=OrderPolicy.OPEN_NORMAL,
                                reason_code=SessionReasonCode.MARKET_OPEN)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertEqual(result.unpriced_position_count, 1)
        self.assertIn("EUR/USD", result.stale_symbols)
        self.assertNotIn("EUR/USD", result.unavailable_symbols)
        self.assertLess(result.pricing_coverage_pct, Decimal("100.00"))

    # ── 3/13/14: CLOSED (weekend/holiday) + valid durable row → FROZEN ──
    def test_market_closed_weekend_with_valid_durable_price_is_frozen(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertEqual(result.unpriced_position_count, 0)
        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)
        self.assertEqual(result.pricing_coverage_pct, Decimal("100.00"))

    def test_market_closed_holiday_with_valid_durable_price_is_frozen(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 12, 25, 10, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.HOLIDAY,
                                reason_code=SessionReasonCode.HOLIDAY_CLOSURE)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)

    def test_market_closed_maintenance_with_valid_durable_price_is_frozen_eth(self):
        """Case 7 — mechanism is symbol-agnostic; ETHUSD under a mocked
        MAINTENANCE session behaves identically to a Forex pair."""
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="ETHUSD", side="BUY",
                      qty=Decimal("1"), avg_price=Decimal("2500"))
        LastKnownMarketPrice.objects.create(
            symbol="ETHUSD", bid=Decimal("2510"), ask=Decimal("2511"),
            mid=Decimal("2510.5"), source="massive",
            observed_at=datetime(2026, 9, 5, 3, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="ETHUSD", calendar_id=CalendarId.CRYPTO_24_7,
                                state=MarketSessionState.MAINTENANCE,
                                reason_code=SessionReasonCode.DAILY_MAINTENANCE)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("ETHUSD", result.market_closed_frozen_symbols)

    # ── 4/25: CLOSED + no valid durable row → UNAVAILABLE, fail closed ──
    def test_market_closed_no_durable_row_is_unavailable(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        self.assertFalse(LastKnownMarketPrice.objects.filter(symbol="GBP/USD").exists())
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertEqual(result.unpriced_position_count, 1)
        self.assertIn("GBP/USD", result.unavailable_symbols)
        self.assertNotIn("GBP/USD", result.stale_symbols)

    # ── 18/23: invalid source in the durable row → UNAVAILABLE ──────────
    def test_market_closed_durable_row_invalid_source_is_unavailable(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="sim",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("GBP/USD", result.unavailable_symbols)

    # ── 22: durable row NEVER used while market is OPEN ─────────────────
    def test_durable_row_never_used_while_market_open(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.OPEN,
                                order_policy=OrderPolicy.OPEN_NORMAL,
                                reason_code=SessionReasonCode.MARKET_OPEN)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("GBP/USD", result.stale_symbols)
        self.assertNotIn("GBP/USD", result.market_closed_frozen_symbols)
        self.assertEqual(result.unpriced_position_count, 1)

    # ── 15: session UNKNOWN (service "doesn't know") → fail closed ──────
    def test_session_unknown_is_fail_closed(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.UNKNOWN,
                                order_policy=OrderPolicy.HALT_NEW_ORDERS,
                                calendar_id=CalendarId.UNKNOWN,
                                reason_code=SessionReasonCode.EVALUATION_ERROR)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("GBP/USD", result.stale_symbols)
        self.assertEqual(result.unpriced_position_count, 1)

    # ── 9/10: frozen Position still counted in gross/net notional ───────
    def test_frozen_position_included_in_gross_and_net_notional(self):
        account = make_account(balance=Decimal("10000"))
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        expected_notional = Decimal("0.01") * Decimal("1.3539") * Decimal("100000")
        self.assertEqual(result.gross_notional, expected_notional)
        self.assertEqual(result.net_notional, expected_notional)  # BUY -> positive

    # ── 11/12: long and short both valued at the SAME frozen mid ────────
    def test_long_and_short_valued_at_same_frozen_mid_no_side_asymmetry(self):
        acc_long = make_account(balance=Decimal("10000"))
        acc_short = make_account(balance=Decimal("10000"))
        make_position(account=acc_long, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        make_position(account=acc_short, symbol="GBP/USD", side="SELL",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_ids=[acc_long.id, acc_short.id])
        sb = result.by_symbol["GBP/USD"]
        per_side_notional = Decimal("0.01") * Decimal("1.3539") * Decimal("100000")
        self.assertEqual(sb.long_notional, per_side_notional)
        self.assertEqual(sb.short_notional, per_side_notional)
        self.assertEqual(sb.net_notional, Decimal("0"))  # long and short cancel exactly

    # ── 16/17: REAL vs DEMO risk_scope, same MARKET_CLOSED_FROZEN result ─
    def test_market_closed_frozen_applies_under_real_scope(self):
        account = make_account(balance=Decimal("10000"), account_type="RETAIL")
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(risk_scope="real")
        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)

    def test_market_closed_frozen_applies_under_demo_scope(self):
        account = make_account(balance=Decimal("10000"), account_type="DEMO")
        make_position(account=account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        session = make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
        with patch(_SESSION_PATCH_TARGET, return_value=session):
            result = calculate_broker_exposure(account_id=account.id)
        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)

    # ── 5/6/29/30: the actual reported cross-asset bug, reproduced + fixed ─
    def test_btc_fresh_gbp_frozen_coverage_is_complete(self):
        """Case 5/30 — the reported bug: a stale Forex position must no
        longer drag broker-wide coverage below 100% once it resolves to
        MARKET_CLOSED_FROZEN instead of unpriced."""
        acc_gbp = make_account(balance=Decimal("10000"))
        acc_btc = make_account(balance=Decimal("10000"))
        make_position(account=acc_gbp, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        make_position(account=acc_btc, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("60000"))
        LastKnownMarketPrice.objects.create(
            symbol="GBP/USD", bid=Decimal("1.3538"), ask=Decimal("1.3540"),
            mid=Decimal("1.3539"), source="massive",
            observed_at=datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        _seed_fresh_price("BTCUSD", 61000.0)

        def _session_side_effect(symbol, **kw):
            if symbol == "GBP/USD":
                return make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
            return make_session(symbol=symbol, calendar_id=CalendarId.CRYPTO_24_7,
                                 state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                                 reason_code=SessionReasonCode.MARKET_OPEN)

        with patch(_SESSION_PATCH_TARGET, side_effect=_session_side_effect):
            result = calculate_broker_exposure(account_ids=[acc_gbp.id, acc_btc.id])

        self.assertEqual(result.pricing_coverage_pct, Decimal("100.00"))
        self.assertIn("BTCUSD", result.fresh_symbols)
        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)
        self.assertEqual(result.unpriced_position_count, 0)

    def test_btc_fresh_gbp_stale_during_open_market_blocks_coverage(self):
        """Case 6 — the pre-existing, still-correct behavior: a genuinely
        broken feed during OPEN market hours must still fail closed."""
        acc_gbp = make_account(balance=Decimal("10000"))
        acc_btc = make_account(balance=Decimal("10000"))
        make_position(account=acc_gbp, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        make_position(account=acc_btc, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("60000"))
        _seed_fresh_price("BTCUSD", 61000.0)

        def _session_side_effect(symbol, **kw):
            if symbol == "GBP/USD":
                return make_session(symbol="GBP/USD", state=MarketSessionState.OPEN,
                                     order_policy=OrderPolicy.OPEN_NORMAL,
                                     reason_code=SessionReasonCode.MARKET_OPEN)
            return make_session(symbol=symbol, calendar_id=CalendarId.CRYPTO_24_7,
                                 state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                                 reason_code=SessionReasonCode.MARKET_OPEN)

        with patch(_SESSION_PATCH_TARGET, side_effect=_session_side_effect):
            result = calculate_broker_exposure(account_ids=[acc_gbp.id, acc_btc.id])

        self.assertLess(result.pricing_coverage_pct, Decimal("100.00"))
        self.assertIn("GBP/USD", result.stale_symbols)
        self.assertIn("BTCUSD", result.fresh_symbols)

    def test_btc_fresh_gbp_no_durable_row_fails_closed(self):
        """Case 25 (cross-asset framing) — market closed, but no valid
        durable row exists at all yet: UNAVAILABLE, not fabricated."""
        acc_gbp = make_account(balance=Decimal("10000"))
        acc_btc = make_account(balance=Decimal("10000"))
        make_position(account=acc_gbp, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        make_position(account=acc_btc, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("60000"))
        self.assertFalse(LastKnownMarketPrice.objects.filter(symbol="GBP/USD").exists())
        _seed_fresh_price("BTCUSD", 61000.0)

        def _session_side_effect(symbol, **kw):
            if symbol == "GBP/USD":
                return make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
            return make_session(symbol=symbol, calendar_id=CalendarId.CRYPTO_24_7,
                                 state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                                 reason_code=SessionReasonCode.MARKET_OPEN)

        with patch(_SESSION_PATCH_TARGET, side_effect=_session_side_effect):
            result = calculate_broker_exposure(account_ids=[acc_gbp.id, acc_btc.id])

        self.assertIn("GBP/USD", result.unavailable_symbols)
        self.assertIn("BTCUSD", result.fresh_symbols)
        self.assertLess(result.pricing_coverage_pct, Decimal("100.00"))

    # ── 21/29: durable price survives a Redis restart (weekend scenario) ─
    def test_weekend_redis_restart_scenario_end_to_end(self):
        """Case 21/29 — full reproduction of the Design Lock's obligatory
        restart scenario: Friday's tick already persisted the durable row;
        Redis (the in-process FeedManager cache, standing in for Redis
        here — has_price() reads the same in-memory state a real Redis
        read would back) is empty, as after a restart; GBP/USD must value
        frozen and BTCUSD must not be blocked by it."""
        acc_gbp = make_account(balance=Decimal("10000"))
        acc_btc = make_account(balance=Decimal("10000"))
        make_position(account=acc_gbp, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.3500"))
        make_position(account=acc_btc, symbol="BTCUSD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("60000"))

        # Friday: a valid tick was persisted durably.
        _write_durable_price_sync(
            "GBP/USD", 1.3538, 1.3540, 1.3539, "massive",
            datetime(2026, 9, 4, 20, 0, tzinfo=dt_timezone.utc),
        )
        # Saturday: Redis restarted -> no fresh key for GBP/USD (never seeded).
        # BTCUSD's feed is alive and fresh.
        _seed_fresh_price("BTCUSD", 61000.0)

        def _session_side_effect(symbol, **kw):
            if symbol == "GBP/USD":
                return make_session(symbol="GBP/USD", state=MarketSessionState.WEEKEND)
            return make_session(symbol=symbol, calendar_id=CalendarId.CRYPTO_24_7,
                                 state=MarketSessionState.OPEN, order_policy=OrderPolicy.OPEN_NORMAL,
                                 reason_code=SessionReasonCode.MARKET_OPEN)

        with patch(_SESSION_PATCH_TARGET, side_effect=_session_side_effect):
            result = calculate_broker_exposure(account_ids=[acc_gbp.id, acc_btc.id])

        self.assertIn("GBP/USD", result.market_closed_frozen_symbols)
        self.assertIn("BTCUSD", result.fresh_symbols)
        self.assertEqual(result.pricing_coverage_pct, Decimal("100.00"))
        self.assertEqual(result.unpriced_position_count, 0)


# ─────────────────────────────────────────────────────────────────────────
# C. Provenance constant sanity — guards against silent drift
# ─────────────────────────────────────────────────────────────────────────
class ProvenanceConstantTests(TestCase):
    def test_valid_sources_match_model_and_feeds_module(self):
        self.assertEqual(set(_DURABLE_PRICE_VALID_SOURCES), set(LastKnownMarketPrice.VALID_SOURCES))
        self.assertNotIn("sim", _DURABLE_PRICE_VALID_SOURCES)

    def test_throttle_interval_is_60_seconds_by_default(self):
        self.assertEqual(_DURABLE_PRICE_WRITE_INTERVAL_SECS, 60)
