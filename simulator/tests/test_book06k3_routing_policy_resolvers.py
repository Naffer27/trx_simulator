"""
BOOK-06k.3 — Evidence + Exposure Resolvers tests.

These are DB-level tests (TestCase, real fixtures) — the resolvers
themselves are deliberately impure. Query-count assertions
(assertNumQueries) are architectural invariants of this block, not
just documentation — see routing_policy_resolvers.py's own docstrings.
"""
import time
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from market_data.feeds import get_feed_manager
from simulator.broker_exposure import broker_exposure_snapshot
from simulator.models import Trade
from simulator.routing_policy_resolvers import (
    RoutingResolverError,
    resolve_routing_evidence,
    resolve_routing_exposure,
)

from .factories import make_account, make_position


def _make_closed_trade(account, *, closed_at, opened_at=None, symbol="EUR/USD",
                        lot_size="0.1", entry_price="1.10000", profit_loss="0.00"):
    return Trade.objects.create(
        account=account,
        symbol=symbol,
        trade_type="BUY",
        lot_size=Decimal(lot_size),
        entry_price=Decimal(entry_price),
        exit_price=Decimal(entry_price),
        profit_loss=Decimal(profit_loss),
        opened_at=opened_at if opened_at is not None else closed_at,
        closed_at=closed_at,
    )


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


# ─────────────────────────────────────────────────────────────────────────
# Evidence resolver
# ─────────────────────────────────────────────────────────────────────────
class EvidenceResolverTests(TestCase):

    def test_zero_trades(self):
        account = make_account()
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 0)
        self.assertEqual(r["sample_trade_count"], 0)
        self.assertEqual(r["sample_span_days"], 0)

    def test_one_trade(self):
        account = make_account()
        _make_closed_trade(account, closed_at=timezone.now())
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 1)
        self.assertEqual(r["sample_trade_count"], 1)
        self.assertEqual(r["sample_span_days"], 0)

    def test_multiple_trades_within_sample(self):
        account = make_account()
        now = timezone.now()
        for days_ago in (0, 10, 20):
            _make_closed_trade(account, closed_at=now - timedelta(days=days_ago))
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 3)
        self.assertEqual(r["sample_trade_count"], 3)
        self.assertEqual(r["sample_span_days"], 20)

    def test_sample_cap_exactly_100_lifetime_shows_more(self):
        account = make_account()
        now = timezone.now()
        for i in range(150):
            _make_closed_trade(account, closed_at=now - timedelta(days=i))
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 150)
        self.assertEqual(r["sample_trade_count"], 100)
        # sample = the 100 MOST RECENT (days_ago 0..99) -> span = 99
        self.assertEqual(r["sample_span_days"], 99)

    def test_sample_orders_by_closed_at_takes_most_recent(self):
        account = make_account()
        now = timezone.now()
        old_trade_days_ago = 500
        _make_closed_trade(account, closed_at=now - timedelta(days=old_trade_days_ago))
        for i in range(100):
            _make_closed_trade(account, closed_at=now - timedelta(days=i))
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 101)
        self.assertEqual(r["sample_trade_count"], 100)
        # the 500-days-ago trade must NOT be in the sample (only 100 most recent kept)
        self.assertEqual(r["sample_span_days"], 99)

    def test_trades_close_together(self):
        account = make_account()
        now = timezone.now()
        for minutes_ago in (0, 5, 10):
            _make_closed_trade(account, closed_at=now - timedelta(minutes=minutes_ago))
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["sample_trade_count"], 3)
        self.assertEqual(r["sample_span_days"], 0)

    def test_trades_far_apart(self):
        account = make_account()
        now = timezone.now()
        _make_closed_trade(account, closed_at=now)
        _make_closed_trade(account, closed_at=now - timedelta(days=365))
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["sample_span_days"], 365)

    def test_account_age_days(self):
        account = make_account()
        r = resolve_routing_evidence(account=account)
        # account was just created — age should be 0 (created_at ~ now)
        self.assertEqual(r["account_age_days"], 0)

    def test_open_unclosed_trades_never_counted(self):
        account = make_account()
        Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY",
            lot_size=Decimal("0.1"), entry_price=Decimal("1.1"),
            closed_at=None,
        )
        r = resolve_routing_evidence(account=account)
        self.assertEqual(r["lifetime_trade_count"], 0)
        self.assertEqual(r["sample_trade_count"], 0)

    def test_determinism(self):
        account = make_account()
        _make_closed_trade(account, closed_at=timezone.now())
        r1 = resolve_routing_evidence(account=account)
        r2 = resolve_routing_evidence(account=account)
        self.assertEqual(r1, r2)

    def test_does_not_mutate_account(self):
        account = make_account()
        created_at_before = account.created_at
        resolve_routing_evidence(account=account)
        self.assertEqual(account.created_at, created_at_before)

    def test_query_count_exactly_two(self):
        account = make_account()
        now = timezone.now()
        for i in range(5):
            _make_closed_trade(account, closed_at=now - timedelta(days=i))
        with self.assertNumQueries(2):
            resolve_routing_evidence(account=account)


# ─────────────────────────────────────────────────────────────────────────
# Exposure resolver
# ─────────────────────────────────────────────────────────────────────────
class ExposureResolverTests(TestCase):

    def setUp(self):
        _seed_price("EUR/USD", 1.1000)
        _seed_price("GBP/USD", 1.3000)
        self.addCleanup(_clear_price, "EUR/USD")
        self.addCleanup(_clear_price, "GBP/USD")

    def test_zero_positions(self):
        account = make_account()
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertEqual(r["gross_notional"], Decimal("0"))
        self.assertEqual(r["net_notional"], Decimal("0"))
        self.assertEqual(r["relative_weight_pct"], Decimal("0"))

    def test_single_position(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertEqual(r["gross_notional"], Decimal("110000"))
        self.assertEqual(r["net_notional"], Decimal("110000"))

    def test_multiple_positions_multiple_symbols_concentration(self):
        account = make_account(balance=Decimal("1000000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1.10"))
        make_position(account, symbol="GBP/USD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1.30"))
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertIn("EUR/USD", r["concentration_by_symbol"])
        self.assertIn("GBP/USD", r["concentration_by_symbol"])
        total_pct = sum(r["concentration_by_symbol"].values())
        self.assertEqual(total_pct, Decimal("100.00"))

    def test_margin_used_present(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertGreater(r["margin_used"], Decimal("0"))

    def test_pricing_coverage_propagated(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertEqual(r["pricing_coverage_pct"], Decimal("100.00"))

    def test_broker_total_and_relative_weight(self):
        account_a = make_account(balance=Decimal("1000000"))
        account_b = make_account(balance=Decimal("1000000"))
        make_position(account_a, symbol="EUR/USD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1.10"))
        make_position(account_b, symbol="EUR/USD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account_a.pk)
        # EUR/USD contract_size=100_000 (market_data/symbol_specs.py) — each
        # account: 10 lots * 1.10 * 100_000 = 1_100_000; broker total = 2_200_000.
        self.assertEqual(r["broker_total_gross_notional"], Decimal("2200000"))
        self.assertEqual(r["relative_weight_pct"], Decimal("50"))

    def test_broker_total_zero_gives_zero_relative_weight(self):
        account = make_account()
        r = resolve_routing_exposure(account_id=account.pk)
        self.assertEqual(r["relative_weight_pct"], Decimal("0"))
        self.assertNotIsInstance(r["relative_weight_pct"], float)

    def test_decimal_types_never_float(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk)
        for key in ("gross_notional", "net_notional", "margin_used",
                    "broker_total_gross_notional", "relative_weight_pct"):
            self.assertIsInstance(r[key], Decimal, f"{key} must be Decimal")

    def test_risk_scope_forwarded(self):
        account = make_account(account_type="RETAIL", balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk, risk_scope="real")
        self.assertEqual(r["risk_scope"], "real")

    def test_snapshot_scope_mismatch_raises(self):
        account = make_account(balance=Decimal("100000"))
        real_scoped_snapshot = broker_exposure_snapshot(risk_scope="real")
        with self.assertRaises(RoutingResolverError):
            resolve_routing_exposure(account_id=account.pk, risk_scope=None,
                                      broker_snapshot=real_scoped_snapshot)

    def test_relative_weight_can_exceed_100_without_clamp(self):
        """Simulates the known eventual-consistency scenario: pass a
        stale broker_snapshot (taken BEFORE the account's own position
        existed) so account.gross_notional > snapshot.gross_notional."""
        account = make_account(balance=Decimal("1000000"))
        stale_empty_snapshot = broker_exposure_snapshot()  # taken before the position below exists
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1.10"))
        r = resolve_routing_exposure(account_id=account.pk, broker_snapshot=stale_empty_snapshot)
        self.assertEqual(r["relative_weight_pct"], Decimal("0"))  # stale snapshot total was 0 -> 0, not >100 here
        # A genuinely >100 case: stale snapshot has SOME total, account grows past it.
        account2 = make_account(balance=Decimal("1000000"))
        make_position(account2, symbol="GBP/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.30"))
        small_snapshot = broker_exposure_snapshot()  # captures account2's small position only
        make_position(account2, symbol="GBP/USD", side="BUY", qty=Decimal("50"), avg_price=Decimal("1.30"))
        r2 = resolve_routing_exposure(account_id=account2.pk, broker_snapshot=small_snapshot)
        self.assertGreater(r2["relative_weight_pct"], Decimal("100"))

    def test_deterministic_output(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        r1 = resolve_routing_exposure(account_id=account.pk)
        r2 = resolve_routing_exposure(account_id=account.pk)
        self.assertEqual(r1, r2)

    def test_query_count_without_snapshot(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        with self.assertNumQueries(2):
            resolve_routing_exposure(account_id=account.pk)

    def test_query_count_with_snapshot(self):
        account = make_account(balance=Decimal("100000"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1.0"), avg_price=Decimal("1.10"))
        snapshot = broker_exposure_snapshot()
        with self.assertNumQueries(1):
            resolve_routing_exposure(account_id=account.pk, broker_snapshot=snapshot)

    def test_missing_account_returns_zero_not_exception(self):
        """Documented tradeoff: a nonexistent account_id is
        indistinguishable from a legitimately empty one — no defensive
        existence-check query is added."""
        r = resolve_routing_exposure(account_id=999999999)
        self.assertEqual(r["gross_notional"], Decimal("0"))
