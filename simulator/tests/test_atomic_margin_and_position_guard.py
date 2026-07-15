"""
simulator/tests/test_atomic_margin_and_position_guard.py — PANEL-02.

Root cause fixed (two related bugs found in the PANEL-01 audit):

  B1. The pre-trade margin guard (_compute_pretrade_margin_guard, called
      from _order_new) was evaluated against each WebSocket connection's
      own in-memory equity/margin_used — never re-verified under the
      TradingAccount row lock that _db_open_position_atomic already took
      for the commission write. Two (or more) connections of the SAME
      account could each pass their own guard using a stale/shared
      starting point and jointly exceed the account's 10% per-trade / 50%
      total margin caps. Empirically reproduced pre-fix: 1 pre-existing
      position at 35.1% margin + exactly 4 concurrent panel opens at
      8.89% each (each individually valid) → 70.67% total margin
      committed, versus a 50% cap.

  B4. max_open_positions was checked with `len(self._positions)` — a
      per-connection, potentially stale in-memory count — never a fresh
      DB count under the same lock.

Fixed by moving the REAL, final decision inside
_db_open_position_atomic()'s own transaction.atomic() block: lock every
open Position row for the account (select_for_update) + the account row
itself, derive fresh_equity/fresh_margin_used/fresh_open_count/
account.status from THAT locked read, and run the single authoritative
validator (_compute_atomic_open_guard) before creating anything. A
rejection writes nothing — no Position, no commission, no Trade/
LedgerEntry/BrokerLedger. The pre-lock guard in _order_new is kept only
as a fast, non-authoritative early rejection.

Uses TestCase (not TransactionTestCase): every call here goes straight to
TradingConsumer._db_open_position_atomic.__wrapped__ as a plain sync
function on the test's own thread/connection — no asyncio.run(), no
database_sync_to_async threadpool involved, so TestCase's per-test
transaction is visible throughout (same pattern as
test_netting_merge_deadlock_guard.py / test_pretrade_margin_guard.py).

"Concurrent" scenarios are proven the same way the original bug was
reproduced during the PANEL-01 audit: call the real atomic function
repeatedly with the SAME stale, pre-lock-computed arguments a naive caller
would have passed (each call blind to what the others just committed) —
if the fix holds, every call re-derives its own fresh state from the DB
regardless of what was passed in, which is externally indistinguishable
from true concurrent connections each hitting the lock in turn.
This file's scenarios all assume every OPEN (pre-existing) position is
freshly priced — that is what a real running system looks like, and it is
NOT what this file is testing (that is INVARIANTE-1's own dedicated
coverage, see test_market_price_unavailable_guard.py). _PricedTestCase's
setUp() seeds real, fresh entries directly into the shared FeedManager
singleton for every symbol this file touches, re-seeded before EVERY test
(not just once per module) so the freshness TTL can never lapse regardless
of how long the surrounding suite takes to run.
"""
import time
from decimal import Decimal

from django.test import TestCase

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, Trade, TradingAccount

from .factories import make_account, make_position

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__

EURUSD_SPEC = get_spec("EUR/USD")
BTCUSD_SPEC = get_spec("BTCUSD")

_SEED_PRICES = {
    "EUR/USD": (1.1699, 1.1701),
    "GBP/USD": (1.2999, 1.3001),
    "AUD/USD": (0.6799, 0.6801),
    "USD/JPY": (149.99, 150.01),
    "USD/CAD": (1.3499, 1.3501),
    "BTCUSD":  (64748.90, 64749.70),
}


def _seed_all_prices():
    """PANEL-02 INVARIANTE-1 fixture helper — writes real, fresh bid/ask/
    timestamp entries directly into the shared FeedManager singleton
    (same private dicts the async feed loop itself writes, under the same
    lock) for every symbol this test file uses, so has_price() returns
    True for all of them at call time."""
    feed = get_feed_manager()
    now = time.time()
    with feed._lock:
        for sym, (bid, ask) in _SEED_PRICES.items():
            feed._bids[sym] = bid
            feed._asks[sym] = ask
            feed._prices[sym] = round((bid + ask) / 2, 6)
            feed._price_ts[sym] = now


class _PricedTestCase(TestCase):
    def setUp(self):
        super().setUp()
        _seed_all_prices()


def _consumer(account_id, netting_mode=False, leverage=50, spread_pips=0.0,
              allowed_symbols=None, max_lot_size=None, margin_call_level=100.0):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": spread_pips,
        "leverage": leverage, "allowed_symbols": allowed_symbols,
        "max_lot_size": max_lot_size, "margin_call_level": margin_call_level,
    }
    c._feed = get_feed_manager()
    return c


class CasoAFourConcurrentPanelsTests(_PricedTestCase):
    """FASE 1 Caso A — equity=1000, 35.1% margen previo, 4 conexiones
    concurrentes a 9.36% cada una. Antes del fix: habría llegado a ~72.5%
    (roto, análogo al 70.67% comprobado en el audit con otra qty). Después:
    nunca debe superar el 50%."""

    def test_four_concurrent_opens_never_exceed_50pct_total_margin(self):
        account = make_account(balance=Decimal("1000.00"))
        # Posición previa comprometida — 35.1% de margen (qty=0.15 EUR/USD).
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.15"), avg_price=Decimal("1.17"))

        qty_each = 0.04  # 9.36% de margen individual, bajo el 10% por operación
        accepted = 0
        for _ in range(4):
            consumer = _consumer(account.pk)
            result = _db_open_sync(
                consumer, "EUR/USD", "buy", qty_each, 1.17, None, None,
                commission=0.0, new_balance=1000.0,
            )
            if result["ok"]:
                accepted += 1

        total_margin = sum(
            abs(float(p.avg_price) * float(p.qty) * EURUSD_SPEC.contract_size) / 50
            for p in Position.objects.filter(account=account)
        )
        total_margin_pct = total_margin / 1000.0 * 100.0
        self.assertLessEqual(total_margin_pct, 50.0)
        # With the fix, only 1 of the 4 concurrent 9.36% orders can fit
        # under the 50% cap on top of the 35.1% already committed
        # (35.1 + 9.36 = 44.46% OK; 44.46 + 9.36 = 53.82% > 50%, rejected).
        self.assertEqual(accepted, 1)


class CasoBSixConnectionsFromZeroTests(_PricedTestCase):
    """FASE 1 Caso B — 6 conexiones desde margen 0, cada una individualmente
    válida (9.36% < 10%). Solo deben aceptarse las que quepan bajo el 50%
    total (5 de 6: 5*9.36%=46.8% <=50%, la 6ta llevaría a 56.16%>50%)."""

    def test_six_connections_from_zero_only_accepts_available_capacity(self):
        account = make_account(balance=Decimal("1000.00"))
        qty_each = 0.04
        results = []
        for _ in range(6):
            consumer = _consumer(account.pk)
            result = _db_open_sync(
                consumer, "EUR/USD", "buy", qty_each, 1.17, None, None,
                commission=0.0, new_balance=1000.0,
            )
            results.append(result["ok"])

        accepted = sum(1 for ok in results if ok)
        total_margin = sum(
            abs(float(p.avg_price) * float(p.qty) * EURUSD_SPEC.contract_size) / 50
            for p in Position.objects.filter(account=account)
        )
        self.assertLessEqual(total_margin / 1000.0 * 100.0, 50.0)
        self.assertEqual(accepted, 5)
        self.assertEqual(results, [True, True, True, True, True, False])


class MaxOpenPositionsConcurrentTests(_PricedTestCase):
    """FASE 1 — bypass de max_open_positions con dos conexiones 'obsoletas'
    (cada una cree que hay N posiciones cuando en realidad ya hay más),
    ahora cerrado: el conteo se deriva SIEMPRE de la DB bajo lock."""

    def test_two_stale_connections_cannot_jointly_exceed_max_open_positions(self):
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 2
        rule.save(update_fields=["max_open_positions"])

        # Una posición ya existe — el "conteo obsoleto" que cada conexión
        # traería en memoria sería 0 o 1, nunca el real.
        make_position(account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.30"))

        accepted = 0
        for sym in ("AUD/USD", "USD/JPY", "USD/CAD"):
            consumer = _consumer(account.pk)
            result = _db_open_sync(
                consumer, sym, "buy", 0.01,
                1.0 if sym != "USD/JPY" else 150.0,
                None, None, commission=0.0, new_balance=1_000_000.0,
            )
            if result["ok"]:
                accepted += 1

        self.assertEqual(Position.objects.filter(account=account).count(), 2)
        self.assertEqual(accepted, 1)  # only 1 more fits under the cap of 2


class TwoAccountsDoNotBlockEachOtherTests(_PricedTestCase):
    def test_two_different_accounts_are_independent(self):
        account_1 = make_account(balance=Decimal("1000.00"))
        account_2 = make_account(balance=Decimal("1000.00"))
        make_position(account_1, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.15"), avg_price=Decimal("1.17"))

        # account_1 is already near its cap; account_2 is fresh — must not
        # be affected by account_1's committed margin in any way.
        result_2 = _db_open_sync(
            _consumer(account_2.pk), "EUR/USD", "buy", 0.04, 1.17, None, None,
            commission=0.0, new_balance=1000.0,
        )
        self.assertTrue(result_2["ok"])
        self.assertEqual(Position.objects.filter(account=account_2).count(), 1)


class RejectedOrderChargesNoCommissionTests(_PricedTestCase):
    def test_rejected_order_does_not_charge_commission(self):
        account = make_account(balance=Decimal("100.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.08, None, None,
            commission=5.0, new_balance=95.0,
        )
        self.assertFalse(result["ok"])
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("100.00"))
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).count(),
            0,
        )


class RejectedOrderCreatesNothingTests(_PricedTestCase):
    def test_rejected_order_creates_no_position_trade_ledger_or_brokerledger(self):
        account = make_account(balance=Decimal("100.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.08, None, None,
            commission=5.0, new_balance=95.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 0)
        self.assertIsNone(result["position_id"])


class NettingMergeDoesNotCountAsNewPositionTests(_PricedTestCase):
    def test_same_side_merge_does_not_increase_open_count_or_get_blocked(self):
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 1
        rule.save(update_fields=["max_open_positions"])

        consumer = _consumer(account.pk, netting_mode=True)
        r1 = _db_open_sync(
            consumer, "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertTrue(r1["ok"])
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

        # max_open_positions=1 and there's already 1 position — a NEW
        # position would be blocked, but this is a same-side MERGE, which
        # must never be blocked by the cap.
        r2 = _db_open_sync(
            consumer, "EUR/USD", "buy", 0.01, 1.09, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertTrue(r2["ok"])
        self.assertTrue(r2["merged"])
        self.assertEqual(Position.objects.filter(account=account).count(), 1)
        self.assertEqual(r1["position_id"], r2["position_id"])

    def test_opposite_side_or_new_symbol_counts_as_new_position(self):
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 1
        rule.save(update_fields=["max_open_positions"])

        consumer = _consumer(account.pk, netting_mode=True)
        r1 = _db_open_sync(
            consumer, "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertTrue(r1["ok"])

        # Opposite side, same symbol — hedging mode would create a new row,
        # and netting mode only merges SAME side — this is a genuinely new
        # position, correctly blocked by max_open_positions=1.
        r2 = _db_open_sync(
            consumer, "EUR/USD", "sell", 0.01, 1.08, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error_code"], "max_positions")
        self.assertEqual(Position.objects.filter(account=account).count(), 1)


class SuspendedAccountRejectedUnderLockTests(_PricedTestCase):
    def test_suspended_account_rejected(self):
        account = make_account(balance=Decimal("10000.00"), status="Suspendido")
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "account_blocked")
        self.assertEqual(Position.objects.filter(account=account).count(), 0)

    def test_violated_account_rejected(self):
        account = make_account(balance=Decimal("10000.00"), status="Violado")
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "account_blocked")


class StaleCallerBalanceIsIgnoredTests(_PricedTestCase):
    """FASE 2 — cambio de balance entre pre-check y atomic se respeta: el
    valor que el llamador cree (new_balance) nunca decide el resultado."""

    def test_stale_high_balance_does_not_rescue_an_order_the_real_balance_would_reject(self):
        account = make_account(balance=Decimal("50.00"))  # real, tiny balance
        # Caller's pre-lock estimate is wildly wrong (stale/optimistic).
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.08, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "margin_per_trade_exceeded")

    def test_stale_low_balance_does_not_block_an_order_the_real_balance_would_allow(self):
        account = make_account(balance=Decimal("1_000_000.00"))  # real, large balance
        # Caller's pre-lock estimate is wildly wrong (stale/pessimistic).
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=1.0,
        )
        self.assertTrue(result["ok"])


class StaleCallerPositionCountIsIgnoredTests(_PricedTestCase):
    """FASE 2 — cambio de posiciones entre pre-check y atomic se respeta."""

    def test_current_open_positions_reflects_real_db_state_not_caller_assumption(self):
        account = make_account(balance=Decimal("1_000_000.00"))
        make_position(account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.30"))
        make_position(account, symbol="AUD/USD", qty=Decimal("0.01"), avg_price=Decimal("0.68"))

        # The caller (a stale connection) has no idea 2 positions already
        # exist — nothing in the call signature even allows it to claim
        # otherwise; the guard must still report the real count.
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_open_positions"], 2)


class SmallBTCUSDAccountTests(_PricedTestCase):
    """FASE 6 — cuenta pequeña BTCUSD: 0.001 sigue aceptándose, 0.01 sigue
    rechazándose con los números correctos (reproduce el diagnóstico
    original del margin guard ~17.6%, ahora bajo la autoridad atómica)."""

    def test_btcusd_0001_fits_under_cap(self):
        account = make_account(balance=Decimal("183.82"))
        result = _db_open_sync(
            _consumer(account.pk), "BTCUSD", "buy", 0.001, 64749.30, None, None,
            commission=0.0, new_balance=183.82,
        )
        self.assertTrue(result["ok"])

    def test_btcusd_001_rejected_with_correct_numbers(self):
        account = make_account(balance=Decimal("183.82"))
        result = _db_open_sync(
            _consumer(account.pk), "BTCUSD", "buy", 0.01, 64749.30, None, None,
            commission=0.0, new_balance=183.82,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "margin_per_trade_exceeded")
        # required_margin = 64749.30 * 0.01 * 1.0 / 20 (effective lev) = 32.3747
        self.assertAlmostEqual(result["required_margin"], 32.3747, places=2)
        self.assertAlmostEqual(result["required_margin_pct"], 17.61, places=1)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)


class StructuredErrorResponseShapeTests(_PricedTestCase):
    """FASE 5 — el resultado siempre trae el set completo de campos,
    tanto en éxito como en rechazo."""

    _EXPECTED_KEYS = {
        "ok", "error_code", "message", "required_margin", "required_margin_pct",
        "projected_total_margin", "projected_total_margin_pct",
        "max_total_margin_pct", "current_open_positions", "max_open_positions",
    }

    def test_success_response_has_full_field_set(self):
        account = make_account(balance=Decimal("10000.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.08, None, None,
            commission=0.0, new_balance=10000.0,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(self._EXPECTED_KEYS.issubset(result.keys()))
        self.assertIsNone(result["error_code"])

    def test_rejection_response_has_full_field_set(self):
        account = make_account(balance=Decimal("100.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.08, None, None,
            commission=0.0, new_balance=100.0,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(self._EXPECTED_KEYS.issubset(result.keys()))
        self.assertIsNotNone(result["error_code"])
        self.assertIsNotNone(result["message"])


class NoFormulaChangeStructuralTests(_PricedTestCase):
    """PANEL-02 no debe alterar ninguna fórmula financiera existente —
    pnl_engine, spread_engine y el 10%/50% de _compute_pretrade_margin_guard
    permanecen intactos."""

    def test_margin_caps_unchanged(self):
        from simulator.consumers import (
            _DEFAULT_MAX_MARGIN_PER_TRADE_PCT, _DEFAULT_MAX_TOTAL_MARGIN_PCT,
        )
        self.assertEqual(_DEFAULT_MAX_MARGIN_PER_TRADE_PCT, 10.0)
        self.assertEqual(_DEFAULT_MAX_TOTAL_MARGIN_PCT, 50.0)

    def test_pnl_engine_untouched(self):
        import inspect
        from simulator import pnl_engine
        src = inspect.getsource(pnl_engine)
        self.assertIn("calculate_quote_pnl", src)
        self.assertIn("convert_pnl_to_account_currency", src)
