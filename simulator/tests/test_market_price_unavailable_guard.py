"""
simulator/tests/test_market_price_unavailable_guard.py — PANEL-02 INVARIANTE-1.

Root cause fixed: _db_open_position_atomic's fresh_equity computation
treated any open position whose symbol had no cached price
(FeedManager.has_price()==False) as contributing 0 to floating PnL. That
is NOT conservative — a position that is actually deeply underwater would
make fresh_equity look HIGHER than the account's real equity (real equity
= balance + true_floating, which could be very negative), letting an
order through that a correct equity read would have rejected. The
direction of the hidden position's real PnL is irrelevant: zero is wrong
either way, just wrong in different directions.

Fixed by making price availability a HARD requirement: if ANY open
position lacks a real, sufficiently fresh price
(FeedManager.has_price(symbol)), the ENTIRE order is rejected with
error_code="market_price_unavailable" — nothing is computed, nothing is
written (no Position, no commission, no Trade/LedgerEntry/BrokerLedger).
has_price() itself was extended from "presence" to "presence AND fresh
within FeedManager._PRICE_CACHE_TTL (60s)" — a stalled feed (no ticks for
minutes) or a fallback-seeded entry (REST resync failed with nothing
prior) both fail the check, exactly like an absent price.

Uses TestCase — same rationale as test_atomic_margin_and_position_guard.py
(plain sync .__wrapped__ call, no threadpool/asyncio involved).
"""
import time
from decimal import Decimal

from django.test import TestCase

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, Trade, TradingAccount

from .factories import make_account, make_position

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _consumer(account_id, netting_mode=False, leverage=50):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0, "leverage": leverage,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


def _clear_price(symbol):
    """Ensure *symbol* has NO cached price at all (absent, not just stale)."""
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


def _seed_fresh_price(symbol, bid, ask):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = round((bid + ask) / 2, 6)
        feed._price_ts[symbol] = time.time()


def _seed_stale_price(symbol, bid, ask, age_seconds):
    """Seed a price whose timestamp is *age_seconds* in the past — present,
    but older than has_price()'s freshness TTL."""
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = round((bid + ask) / 2, 6)
        feed._price_ts[symbol] = time.time() - age_seconds


class LosingPositionMissingPriceTests(TestCase):
    """1) Posición abierta perdiendo + precio ausente → orden rechazada."""

    def test_underwater_position_with_no_price_rejects_new_order(self):
        _clear_price("USD/JPY")
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        # avg_price far above any plausible current USD/JPY level — framed
        # as "if it had a price right now, it would be a heavy loser" —
        # but the mechanism must reject regardless of direction (see
        # WinningPositionMissingPriceTests below for the mirror case).
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("500.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("USD/JPY", result["message"])


class WinningPositionMissingPriceTests(TestCase):
    """2) Posición abierta ganando + precio ausente → también rechazada.

    Mirrors LosingPositionMissingPriceTests with the opposite framing
    (avg_price implausibly LOW, "as if it were now deep in profit") to
    prove the rejection is unconditional — the guard structurally cannot
    see the hidden position's true direction (there is no price to
    compute it from), so it must refuse identically either way."""

    def test_profitable_position_with_no_price_still_rejects_new_order(self):
        _clear_price("USD/JPY")
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("0.01"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("USD/JPY", result["message"])


class StalePriceRejectedTests(TestCase):
    """3) Precio stale (más viejo que el TTL de frescura) → rechazada."""

    def test_stale_price_beyond_ttl_rejects_new_order(self):
        from market_data.feeds import _PRICE_CACHE_TTL
        _seed_stale_price("USD/JPY", 149.99, 150.01, age_seconds=_PRICE_CACHE_TTL + 30)
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("150.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")
        self.assertIn("USD/JPY", result["message"])

    def test_price_just_within_ttl_is_accepted(self):
        from market_data.feeds import _PRICE_CACHE_TTL
        _seed_stale_price("USD/JPY", 149.99, 150.01, age_seconds=_PRICE_CACHE_TTL - 5)
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        # Small qty — this test is about the TTL boundary, not margin
        # sizing; a 1.0-lot USD/JPY position would itself blow the 50%
        # total-margin cap on a $10,000 account (100,000 contract size).
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("150.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])


class AllFreshPricesComputeCorrectlyTests(TestCase):
    """4) Todos los precios frescos → cálculo correcto (sin rechazo por
    market_price_unavailable, y fresh_equity refleja el floating PnL real
    de las posiciones abiertas)."""

    def test_all_fresh_prices_no_rejection_and_correct_equity(self):
        _seed_fresh_price("USD/JPY", 149.99, 150.01)
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        # BUY closes at bid (149.99); avg_price=149.00 → position is
        # genuinely profitable: (149.99-149.00)*0.01*100000/leverage —
        # exact number not asserted here (pnl_engine's own formula is out
        # of scope for this test), only that it is NOT zero and NOT
        # rejected. qty kept small (0.01) so the position's OWN margin
        # doesn't itself blow the 50% total-margin cap.
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("149.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])
        self.assertNotEqual(result.get("error_code"), "market_price_unavailable")

    def test_fresh_equity_reflects_real_floating_pnl_not_zero(self):
        """Directly proves fresh_equity is NOT computed as balance+0 when a
        real, priced floating gain exists — reject/accept boundary shifts
        exactly as the real (non-zero) equity dictates."""
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        # A small account where a large real floating GAIN is the only
        # thing that makes the new order's margin % fit under the 10% cap.
        account = make_account(balance=Decimal("100.00"))
        # BUY EUR/USD, opened far below the current bid — a large real
        # unrealized gain once priced.
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("1.00"))
        # True floating ≈ (1.1699-1.00)*1.0*100000 = 16,990 — real equity
        # ≈ 17,090, comfortably clearing the margin cap for a further
        # 0.01-lot order that would be rejected against balance=100 alone.
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.1701, None, None,
            commission=0.0, new_balance=100.0,
        )
        self.assertTrue(result["ok"])
        self.assertNotEqual(result.get("error_code"), "market_price_unavailable")
        self.assertNotEqual(result.get("error_code"), "margin_per_trade_exceeded")


class RejectionProducesNoFinancialWriteTests(TestCase):
    """5) Rechazo por market_price_unavailable no produce ningún write
    financiero — ni Position, ni comisión, ni Trade/LedgerEntry/
    BrokerLedger, y el balance de la cuenta queda intacto."""

    def test_no_writes_on_market_price_unavailable_rejection(self):
        _clear_price("USD/JPY")
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("150.00"))
        positions_before = Position.objects.filter(account=account).count()

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=5.0, new_balance=9995.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "market_price_unavailable")

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("10000.00"))
        self.assertEqual(Position.objects.filter(account=account).count(), positions_before)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).count(),
            0,
        )
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 0)
        self.assertEqual(result["position_id"], None)
        self.assertEqual(result["new_balance"], 10000.0)


class ResponseShapeAndNoSensitiveLeakTests(TestCase):
    def test_response_has_full_field_set_and_names_only_the_symbol(self):
        _clear_price("USD/JPY")
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)

        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="USD/JPY", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("150.00"))

        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
            commission=0.0, new_balance=10000.0,
        )
        expected_keys = {
            "ok", "error_code", "message", "required_margin", "required_margin_pct",
            "projected_total_margin", "projected_total_margin_pct",
            "max_total_margin_pct", "current_open_positions", "max_open_positions",
        }
        self.assertTrue(expected_keys.issubset(result.keys()))
        # Names the affected symbol (public trading-pair name, not
        # sensitive) — does not leak balance/margin numbers in the message.
        self.assertIn("USD/JPY", result["message"])
        self.assertNotIn(str(account.balance), result["message"])
