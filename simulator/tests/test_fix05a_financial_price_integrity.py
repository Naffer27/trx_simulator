# simulator/tests/test_fix05a_financial_price_integrity.py
"""
FIX-05A — Financial Price Integrity.

Closes three invariants (per the FIX-05A design lock):

  1. quote.source=="sim" (FeedManager's synthetic-continuity fallback,
     used when the market is closed or the real provider is down) is
     never financial authority. Gated at the two boundary functions
     every financial path already funnels through: _raw_exec_price()
     (open) and _feed_close_price() (close/PnL/SL-TP/stopout) — both
     now treat quote.source=="sim" identically to "no quote at all".
     Display (get_validated_quote() itself, price_tick()'s candle/
     ticket path) is UNCHANGED — sim can still feed continuity there.

  2. Market-closed / HALT_NEW_ORDERS / CLOSE_ONLY now gate _order_new()
     via the pre-existing FOUNDATION-02 OrderPolicy contract
     (market_data.sessions.service.evaluate_market_session_for_symbol),
     confirmed by the design lock to have had zero real consumer before
     this block. Never applied to close paths.

  3. The spread fee charged at open (consumers.py::_db_open_position_
     atomic) now reads effective_spread_pips from THIS SAME order's
     already-captured pricing_context instead of independently
     recomputing base_pips+markup_pips — eliminating the divergence
     between the fee and the displayed bid/ask for the same tick.
     pricing_context=None (the ~110 existing test call sites that
     invoke _db_open_position_atomic directly, bypassing _order_new())
     keeps the prior formula verbatim — zero regression for those.

FIX-05A.1 closed both residual gaps left open by this block:

  4. _check_tp_sl() (live WS SL/TP) is now gated on the broadcast
     event's own "source" field (FeedManager._broadcast(), FIX-05A.1) —
     fail-closed: missing source or source=="sim" skips the call
     entirely. No re-query of self._feed (the approach that broke ~20
     pre-existing tests' minimal price_tick() fixtures in the earlier
     attempt) — the event already carries what's needed.
     CheckTpSlCallSiteGatedTests below is a structural regression guard.

  5. The Celery daemon path (tasks.py::scan_positions_task, via
     _read_cached_price()/Redis) now reads a third
     trx:price:source:{symbol} Redis key (feeds.py::
     _write_price_cache_sync(), same TTL/pipeline as bid/ask) and
     fails closed — missing key or source=="sim" returns (None, None),
     the exact same "price unavailable" path scan_positions_task
     already had for stale/missing prices, so neither its stopout nor
     its SL/TP loop needed any change. RedisSourceKeyPersistedTests
     below is a structural regression guard.

See test_fix05a1_source_propagation.py for the dedicated FIX-05A.1
test coverage (WS gate behavior, Redis gate behavior, daemon
integration, provider-recovery).

Reuses the established test_o6c1w_price_integrity_gate.py harness
(_seed_raw/_clear_symbol/_bare_consumer — that file's _seed_raw already
accepts a `source` kwarg) rather than duplicating it.
"""
import time
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase

from market_data.contracts import OrderPolicy
from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator import pricing_context as pc
from simulator.models import BrokerLedger, LedgerEntry, Position
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests

from .factories import make_account, make_position, make_spread_config
from .test_o6c1w_price_integrity_gate import _bare_consumer, _clear_symbol, _seed_raw
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__


def _open_normal_session():
    """market-session policy is real-clock-driven (FIX-05A) — EUR/USD is
    legitimately MARKET_CLOSED on a real weekend. Tests that aren't
    themselves about market-session policy (see MarketClosedGateTests)
    patch it open so they stay deterministic regardless of when the
    suite runs — same technique already established there."""
    return patch(
        "market_data.sessions.service.evaluate_market_session_for_symbol",
        return_value=Mock(order_policy=OrderPolicy.OPEN_NORMAL),
    )


class _FakeConsumer:
    """Minimal consumer stub — mirrors test_pricing_context_persistence.py's
    own fixture (only the attributes _db_open_position_atomic touches)."""
    _should_activate_routing_decision = TradingConsumer._should_activate_routing_decision

    def __init__(self, account_id, netting_mode=False, spread_pips=0.0):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": spread_pips}
        self._feed = get_feed_manager()


# ── 1/2 — the two gated financial boundary functions ────────────────────────

class RawExecPriceSimGateTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_buy_open_returns_none_on_sim_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        panel = _bare_consumer(1)
        self.assertIsNone(panel._raw_exec_price("EUR/USD", "buy"))

    def test_sell_open_returns_none_on_sim_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        panel = _bare_consumer(1)
        self.assertIsNone(panel._raw_exec_price("EUR/USD", "sell"))

    def test_buy_open_uses_raw_ask_on_real_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="finnhub")
        panel = _bare_consumer(1)
        self.assertAlmostEqual(panel._raw_exec_price("EUR/USD", "buy"), 1.10020, places=5)

    def test_sell_open_uses_raw_bid_on_real_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="finnhub")
        panel = _bare_consumer(1)
        self.assertAlmostEqual(panel._raw_exec_price("EUR/USD", "sell"), 1.10000, places=5)


class FeedClosePriceSimGateTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_buy_close_returns_none_on_sim_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        panel = _bare_consumer(1)
        self.assertIsNone(panel._feed_close_price("EUR/USD", "buy"))

    def test_sell_close_returns_none_on_sim_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        panel = _bare_consumer(1)
        self.assertIsNone(panel._feed_close_price("EUR/USD", "sell"))

    def test_buy_close_uses_raw_bid_on_real_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="finnhub")
        panel = _bare_consumer(1)
        self.assertAlmostEqual(panel._feed_close_price("EUR/USD", "buy"), 1.10000, places=5)

    def test_sell_close_uses_raw_ask_on_real_source(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="finnhub")
        panel = _bare_consumer(1)
        self.assertAlmostEqual(panel._feed_close_price("EUR/USD", "sell"), 1.10020, places=5)


# ── 3 — display keeps sim access; financial does not ────────────────────────

class DisplayVsFinancialSeparationTests(SimpleTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_get_validated_quote_still_returns_sim_quote_for_display(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        feed = get_feed_manager()
        q = feed.get_validated_quote("EUR/USD")
        self.assertIsNotNone(q)  # display path unaffected
        self.assertEqual(q.source, "sim")

    def test_same_symbol_financial_accessors_return_none(self):
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        panel = _bare_consumer(1)
        self.assertIsNone(panel._raw_exec_price("EUR/USD", "buy"))
        self.assertIsNone(panel._feed_close_price("EUR/USD", "buy"))


# ── 4 — full order:new BLOCKED on sim (no Position/Trade/fee/commission) ────

class SimOpenBlockedIntegrationTests(TransactionTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def _sim_consumer(self, account_id):
        panel = _consumer(account_id)
        with panel._feed._lock:
            panel._feed._bids["EUR/USD"]         = 1.10000
            panel._feed._asks["EUR/USD"]         = 1.10020
            panel._feed._prices["EUR/USD"]       = 1.10010
            panel._feed._price_ts["EUR/USD"]     = time.time()
            panel._feed._price_source["EUR/USD"] = "sim"
        return panel

    def test_buy_open_rejected_zero_side_effects(self):
        account = make_account(balance=Decimal("10000"))
        panel = self._sim_consumer(account.pk)
        with _open_normal_session():
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))

        self.assertEqual(_first_error(panel)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)
        self.assertEqual(BrokerLedger.objects.filter(source_account_id=account.pk).count(), 0)

    def test_sell_open_rejected_zero_side_effects(self):
        account = make_account(balance=Decimal("10000"))
        panel = self._sim_consumer(account.pk)
        with _open_normal_session():
            _run(panel._order_new({"symbol": "EUR/USD", "side": "sell", "qty": 0.01}))

        self.assertEqual(_first_error(panel)["code"], "price_unavailable")
        self.assertEqual(Position.objects.filter(account=account).count(), 0)


# ── 5 — real quote open economics unchanged (regression) ────────────────────

class RealQuoteOpenUnchangedRegressionTests(TransactionTestCase):
    def setUp(self):
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_buy_still_opens_at_raw_ask(self):
        account = make_account(balance=Decimal("10000"))
        panel = _consumer(account.pk)  # default source="test_seed", bid=ask=1.1000
        with panel._feed._lock:
            panel._feed._bids["EUR/USD"] = 1.09990
            panel._feed._asks["EUR/USD"] = 1.10010
            panel._feed._prices["EUR/USD"] = 1.10000
            panel._feed._price_ts["EUR/USD"] = time.time()
            panel._feed._price_source["EUR/USD"] = "finnhub"
        with _open_normal_session():
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))

        self.assertIsNone(_first_error(panel))
        pos = Position.objects.get(account=account)
        self.assertAlmostEqual(float(pos.avg_price), 1.10010, places=5)

    def test_sell_still_opens_at_raw_bid(self):
        account = make_account(balance=Decimal("10000"))
        panel = _consumer(account.pk)
        with panel._feed._lock:
            panel._feed._bids["EUR/USD"] = 1.09990
            panel._feed._asks["EUR/USD"] = 1.10010
            panel._feed._prices["EUR/USD"] = 1.10000
            panel._feed._price_ts["EUR/USD"] = time.time()
            panel._feed._price_source["EUR/USD"] = "finnhub"
        with _open_normal_session():
            _run(panel._order_new({"symbol": "EUR/USD", "side": "sell", "qty": 0.01}))

        self.assertIsNone(_first_error(panel))
        pos = Position.objects.get(account=account)
        self.assertAlmostEqual(float(pos.avg_price), 1.09990, places=5)


# ── 6 — manual close BLOCKED on sim ──────────────────────────────────────────

class SimCloseBlockedTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_order_close_rejects_on_sim_quote(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"),
                             avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")

        _run(panel._order_close({"id": pos.pk, "symbol": "EUR/USD"}))

        sent = panel.send_json.call_args_list
        self.assertTrue(any(c.args[0].get("code") == "price_unavailable" for c in sent))
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())  # never closed
        self.assertEqual(len(panel._positions), 1)  # memory untouched


# ── 7 — stopout skips a sim-priced position (no fabricated liquidation) ─────

class SimStopoutSkippedTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_do_stopout_skips_sim_priced_position(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"),
                             avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")

        _run(panel._do_stopout())

        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())  # never closed
        self.assertEqual(len(panel._positions), 1)

        # STOPOUT LIQUIDATION OUTCOME INTEGRITY — this is the exact
        # EMPTY-close scenario that bug closed: this call must NOT have
        # suspended the account or written a fake stopout ledger entry,
        # since zero positions actually closed.
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Activo")
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=self.account, event_type=LedgerEntry.EV_ADJUST, meta__reason="stopout",
            ).count(),
            0,
        )
        self.assertEqual(
            [c.args[0] for c in panel.send_json.call_args_list if c.args[0].get("type") == "account:suspended"],
            [],
        )


# ── 8 — market-session policy gates _order_new(), never close ───────────────

class MarketClosedGateTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def _consumer_with_real_quote(self):
        panel = _consumer(self.account.pk)
        with panel._feed._lock:
            panel._feed._bids["EUR/USD"]         = 1.09990
            panel._feed._asks["EUR/USD"]         = 1.10010
            panel._feed._prices["EUR/USD"]       = 1.10000
            panel._feed._price_ts["EUR/USD"]     = time.time()
            panel._feed._price_source["EUR/USD"] = "finnhub"
        return panel

    def _patched_session(self, policy):
        return patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=Mock(order_policy=policy),
        )

    def test_market_closed_rejects_new_order(self):
        panel = self._consumer_with_real_quote()
        with self._patched_session(OrderPolicy.MARKET_CLOSED):
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(panel)["code"], "market_closed")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_halt_new_orders_rejects_new_order(self):
        panel = self._consumer_with_real_quote()
        with self._patched_session(OrderPolicy.HALT_NEW_ORDERS):
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(panel)["code"], "market_closed")

    def test_close_only_rejects_new_open(self):
        panel = self._consumer_with_real_quote()
        with self._patched_session(OrderPolicy.CLOSE_ONLY):
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertEqual(_first_error(panel)["code"], "close_only")
        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)

    def test_open_normal_does_not_block(self):
        panel = self._consumer_with_real_quote()
        with self._patched_session(OrderPolicy.OPEN_NORMAL):
            _run(panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertIsNone(_first_error(panel))
        self.assertEqual(Position.objects.filter(account=self.account).count(), 1)

    def test_close_only_does_not_block_a_legitimate_close(self):
        """CLOSE_ONLY must reject new opens but never impede closing an
        existing position when a real financial quote is available."""
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"),
                             avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel._positions = [{
            "id": pos.pk, "symbol": "EUR/USD", "side": "buy", "qty": 0.01,
            "avg": 1.10000, "sl": None, "tp": None, "opened_at": time.time(),
        }]
        _seed_raw("EUR/USD", 1.09990, 1.10010, source="finnhub")
        with self._patched_session(OrderPolicy.CLOSE_ONLY):
            _run(panel._order_close({"id": pos.pk, "symbol": "EUR/USD"}))
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())  # closed successfully


# ── 9 — single PricingDecision: fee must use pricing_context, not a
#        second, independent recomputation ───────────────────────────────────

class PricingDecisionSingleAuthorityTests(TestCase):
    def test_clamp_divergence_fee_uses_pricing_context_not_base_plus_markup(self):
        """base+markup=5 pips, clamped to max=3 -> fee must reflect 3, not 5.
        Demonstrates the exact FIX-05A bug scenario from the design lock."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, spread_pips=999.0)  # would have poisoned the OLD formula
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, executable_bid=1.0997, executable_ask=1.1003,
            base_spread_pips=4.0, account_markup_pips=1.0,  # 5.0 pre-clamp
            min_spread_pips=0.0, max_spread_pips=3.0,        # clamps to 3.0
            pricing_profile=pc.PROFILE_WS_OPEN,
        )
        self.assertEqual(ctx["effective_spread_pips"], 3.0)  # sanity on the fixture itself

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=50000.0, pricing_context=ctx,
        )
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertAlmostEqual(fee.meta["effective_pips"], 3.0, places=6)
        # 3.0 pips -> half_spread=0.00015 * 1.0 qty * 100000 contract = 15.00
        self.assertAlmostEqual(float(-fee.amount), 15.00, places=2)
        # NOT 5.0 pips (25.00) — the value the old independent recompute
        # would have produced, and NOT poisoned by spread_pips=999.0 either.
        self.assertNotAlmostEqual(float(-fee.amount), 25.00, places=2)

    def test_dynamic_spread_effective_pips_used_verbatim(self):
        """A dynamic-path effective value (not simply base+markup) must
        also be the one charged — not recomputed from its components."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001,
            effective_before_bounds=4.2, effective_after_bounds=4.2,
            dynamic_spread_enabled=True, session_multiplier=1.4,
            pricing_profile=pc.PROFILE_WS_OPEN,
        )
        self.assertEqual(ctx["effective_spread_pips"], 4.2)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=50000.0, pricing_context=ctx,
        )
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertAlmostEqual(fee.meta["effective_pips"], 4.2, places=6)


class PricingContextNoneBackwardCompatTests(TestCase):
    """~110 existing test call sites invoke _db_open_position_atomic
    directly without pricing_context — must keep working unchanged."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()
        _clear_symbol("EUR/USD")

    def test_omitted_pricing_context_uses_prior_formula(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        account = make_account(balance=Decimal("10000"))
        _seed_raw("EUR/USD", 1.17000, 1.17002, source="finnhub")
        panel = _bare_consumer(account.pk)
        panel.account["spread_pips"] = 0.5  # account markup, old-formula input
        _db_open_sync(
            panel, symbol="EUR/USD", side="buy", qty=0.01, price=1.17002,
            sl=None, tp=None, commission=0.0, new_balance=10000.0,
        )
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertAlmostEqual(fee.meta["effective_pips"], 2.5, places=6)  # 2.00 + 0.5


class PricingContextMissingKeyNoFeeTests(TestCase):
    def test_no_broker_spread_config_zero_fee_no_crash(self):
        """pricing_context present but effective_spread_pips is None
        (legitimately — no BrokerSpreadConfig for this symbol at all) ->
        zero fee, no exception, never falls back to an independent
        recompute."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, pricing_profile=pc.PROFILE_WS_OPEN,
        )
        self.assertIsNone(ctx["effective_spread_pips"])  # sanity
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=50000.0, pricing_context=ctx,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).count(), 0)


# ── 10 — commission untouched ────────────────────────────────────────────────

class CommissionUnchangedRegressionTests(TestCase):
    """Two separate test methods (not two opens in one method) — a
    whole-book broker-wide risk check (broker_risk.py, unrelated to
    FIX-05A) can reject a second _FakeConsumer-driven open in the same
    test for reasons having nothing to do with pricing_context; kept
    isolated per Django's own per-test transaction rollback instead."""

    def test_commission_with_pricing_context(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, base_spread_pips=2.0, account_markup_pips=0.0,
            pricing_profile=pc.PROFILE_WS_OPEN,
        )
        _db_open_sync(
            consumer, "EUR/USD", "BUY", 0.01, 1.10000, None, None,
            commission=12.34, new_balance=50000.0 - 12.34, pricing_context=ctx,
        )
        comm = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
        self.assertEqual(-comm.amount, Decimal("12.34"))

    def test_commission_without_pricing_context(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk)
        _db_open_sync(
            consumer, "EUR/USD", "BUY", 0.01, 1.10000, None, None,
            commission=12.34, new_balance=50000.0 - 12.34, pricing_context=None,
        )
        comm = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
        self.assertEqual(-comm.amount, Decimal("12.34"))  # identical to the pricing_context case above


# ── 11 — auditability: Position.pricing_context matches the fee charged ─────

class PricingContextAuditabilityTests(TestCase):
    def test_position_effective_spread_pips_matches_fee_meta(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, base_spread_pips=2.0, account_markup_pips=0.5,
            min_spread_pips=0.0, max_spread_pips=10.0, pricing_profile=pc.PROFILE_WS_OPEN,
        )
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=50000.0, pricing_context=ctx,
        )
        pos = Position.objects.get(pk=result["position_id"])
        fee = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        rev = BrokerLedger.objects.get(revenue_type=BrokerLedger.REV_SPREAD, source_account_id=account.pk)
        self.assertEqual(pos.pricing_context["effective_spread_pips"], 2.5)
        self.assertEqual(fee.meta["effective_pips"], pos.pricing_context["effective_spread_pips"])
        self.assertEqual(rev.meta["spread_pips"], pos.pricing_context["effective_spread_pips"])
        self.assertEqual(-fee.amount, rev.amount)


# ── 12 — FIX-05A.1 closed both residual gaps — structural regression guards ─

class CheckTpSlCallSiteGatedTests(SimpleTestCase):
    def test_check_tp_sl_call_site_now_gated_on_event_source(self):
        """FIX-05A.1 closed the FIX-05A residual gap: _check_tp_sl() is
        now called only when the broadcast event's source is present and
        != "sim". The call itself stays byte-for-byte identical
        ("await self._check_tp_sl(symbol, raw_bid, raw_ask)") — only
        whether it executes changed — so it does not need self._feed and
        does not disturb the ~20 pre-existing tests across 5 files whose
        minimal price_tick() fixtures don't set that attribute."""
        import inspect
        from simulator import consumers
        src = inspect.getsource(consumers.TradingConsumer.price_tick)
        self.assertIn("await self._check_tp_sl(symbol, raw_bid, raw_ask)", src)
        self.assertIn('event.get("source")', src)
        # Gate reads the event's own field — never re-queries FeedManager
        # (the approach that required self._feed and broke ~20 tests).
        code_lines_only = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("self._feed.get_validated_quote", code_lines_only)


class RedisSourceKeyPersistedTests(SimpleTestCase):
    def test_write_price_cache_sync_now_persists_source(self):
        """FIX-05A.1 closed the FIX-05A residual gap: the Celery daemon
        (tasks.py::scan_positions_task, via _read_cached_price()) can now
        distinguish source="sim"/missing from a real provider —
        feeds.py's _write_price_cache_sync() writes a third
        trx:price:source:{symbol} key, same TTL, same pipeline as bid/ask."""
        import inspect
        from market_data import feeds
        src = inspect.getsource(feeds._write_price_cache_sync)
        self.assertIn("source", src)
        self.assertIn(":source:", src)
