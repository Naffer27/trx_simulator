"""
simulator/tests/test_o6c1b_live_risk_scope_integration.py — O.6c-1b.

Live Risk Scope Integration — wires O.6c-1's risk_scope="real" capability
(simulator/broker_exposure.py) into the ONE live caller that accepts/
rejects real orders: broker_risk.py::validate_new_order(), called from
simulator/consumers.py inside _db_open_position_atomic (RISK-02, step
8.5).

The fix: broker_risk.validate_new_order() now accepts an optional
account_type kwarg. consumers.py passes the already-loaded, already-
locked account's own account_type — no extra query. REAL money account
types (RETAIL/ECN/STANDARD/CRYPTO) get every broker-wide RISK-02 read
(validate_symbol_limit, validate_account_limit, the shared `book` used by
validate_total_limit/validate_position_limit, and margin_after) evaluated
under risk_scope="real": DEMO/CHALLENGE/FUNDED positions — open or
stale-priced — can no longer contaminate a REAL account's pricing
coverage or broker-wide limits. This is the exact fix for the O.6a
incident.

DEMO/CHALLENGE/FUNDED orders themselves get account_type passed through
too, but since none of those three values is in REAL_MONEY_ACCOUNT_TYPES,
_risk_scope_for_account_type() returns None for all of them — the full
legacy, unscoped, whole-book evaluation, completely unchanged. This is a
deliberate design choice (Option A), not an oversight — see
broker_risk.py::_risk_scope_for_account_type()'s docstring and the O.6c-1b
report for the two options considered and why the untouched-legacy one
was chosen.

Coverage layers:
  - IncidentReproductionTests: the O.6a scenario end to end — "before"
    (validate_new_order without account_type) still shows
    RISK_PRICING_INCOMPLETE; "after" (account_type passed) clears that
    specific guard; a full E2E test through the real WS handler
    (TradingConsumer._order_new()) proves the production wiring itself,
    not just the function in isolation.
  - NegativeCaseTests: fail-closed is preserved WITHIN the real scope (a
    stale REAL position still blocks); coverage<100% among REAL accounts
    alone still triggers RISK_PRICING_INCOMPLETE; DEMO/CHALLENGE/FUNDED
    staleness never affects REAL; two different REAL accounts DO
    aggregate together; per-account/per-user independence holds; a small
    REAL order does not skip other legitimate RISK-02 limits.
  - DemoChallengeUnaffectedTests: DEMO/CHALLENGE/FUNDED accounts placing
    orders still get the full legacy unscoped evaluation — proves Option
    A was actually implemented, not silently swapped for Option B.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager

import simulator.broker_risk as br
from simulator.broker_risk import validate_new_order, REASON_PRICING_INCOMPLETE
from simulator.models import Position, Trade

from .factories import make_account, make_position, make_user
from .test_order_ticket_sl_tp_validation import _consumer as _ws_consumer

_run = lambda coro: __import__("asyncio").run(coro)


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


class _CleanFeedMixin:
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


# ─────────────────────────────────────────────────────────────────────────
# O.6a incident reproduction — obligatory E2E test
# ─────────────────────────────────────────────────────────────────────────
class IncidentReproductionTests(_CleanFeedMixin, TestCase):
    def _seed_o6a_scenario(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        make_position(demo, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        make_position(challenge, symbol="EUR/USD", side="SELL", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 63353)
        _clear_price("EUR/USD")  # DEMO/CHALLENGE positions stale, as in the manual incident
        return real

    def test_before_change_behavior_still_reproduces_rejection(self):
        # "Before": validate_new_order() called exactly as every pre-
        # O.6c-1b caller/test still calls it — no account_type at all.
        # This is not a simulation of old code; it is the SAME default
        # path still live today for any caller that doesn't pass
        # account_type, proving the legacy behavior was never removed.
        real = self._seed_o6a_scenario()
        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("63353"), contract_size=Decimal("1"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_after_change_real_account_clears_pricing_guard(self):
        # "After": the live wiring's actual new argument — account_type
        # of the account placing the order, exactly as consumers.py now
        # supplies it from the already-loaded account row.
        real = self._seed_o6a_scenario()
        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("63353"), contract_size=Decimal("1"),
            account_type="STANDARD",
        )
        # Central criterion: DEMO/CHALLENGE staleness no longer causes
        # RISK_PRICING_INCOMPLETE for this healthy REAL order.
        self.assertNotEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)
        gross_check = next(c for c in decision.risk_checks if c.rule == "MAX_GROSS_NOTIONAL")
        net_check = next(c for c in decision.risk_checks if c.rule == "MAX_NET_NOTIONAL")
        self.assertEqual(gross_check.status, br.STATUS_PASS)
        self.assertEqual(net_check.status, br.STATUS_PASS)
        # In this fixture no other legitimate limit is anywhere near its
        # (generous, untouched) default threshold, so the order is fully
        # allowed — but the ASSERTION THAT MATTERS is the one above: the
        # pricing guard specifically is cleared. If some other, unrelated
        # guard were to fire in a differently-sized fixture, that would
        # be a separate, legitimate rejection this test does not force
        # past (per the O.6c-1b brief).
        self.assertTrue(decision.allowed)


# select_for_update() needs a real transaction/lock, not TestCase's outer
# wrapping transaction (SQLite: "database table is locked" otherwise) —
# same TransactionTestCase precedent test_broker_risk_limits_engine.py's
# own IntegrationTests class already uses for this exact reason.
class E2ELiveHandlerTests(_CleanFeedMixin, TransactionTestCase):
    def test_e2e_via_live_ws_handler(self):
        # Full production path: TradingConsumer._order_new() -> ...
        # -> _db_open_position_atomic -> validate_new_order(account_type=...).
        # Proves the wiring in consumers.py itself, not just the function
        # signature in isolation.
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        make_position(demo, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        make_position(challenge, symbol="EUR/USD", side="SELL", qty=Decimal("1"), avg_price=Decimal("1.1"))
        _seed_fresh_price("BTCUSD", 63353)
        _clear_price("EUR/USD")

        c = _ws_consumer(real.id)
        c.account["account_type"] = "STANDARD"
        c.symbol = "BTCUSD"
        c._bid_state["BTCUSD"] = 63353.0
        c._ask_state["BTCUSD"] = 63353.0
        _run(c._order_new({"action": "order:new", "symbol": "BTCUSD", "side": "buy", "qty": 0.01}))

        sent = [call.args[0] for call in c.send_json.call_args_list]
        errors = [m for m in sent if m.get("type") == "error"]
        self.assertFalse(
            any(m.get("code") == REASON_PRICING_INCOMPLETE for m in errors),
            f"pricing-incomplete rejection leaked through despite risk_scope wiring: {errors}",
        )
        self.assertEqual(Position.objects.filter(account=real, symbol="BTCUSD").count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# Mandatory negative / positive cases (1-10 from the O.6c-1b brief)
# ─────────────────────────────────────────────────────────────────────────
class NegativeCaseTests(_CleanFeedMixin, TestCase):
    def test_01_real_position_stale_still_fails_closed(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        make_position(real, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")  # this REAL account's OWN position is stale
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="STANDARD",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_02_coverage_below_100_among_real_accounts_alone_still_fails(self):
        real_a = make_account(account_type="RETAIL", balance=Decimal("10000"))
        real_b = make_account(account_type="ECN", balance=Decimal("10000"))
        make_position(real_b, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real_a.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="RETAIL",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_03_demo_stale_does_not_affect_real(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        make_position(demo, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="STANDARD",
        )
        self.assertNotEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_04_challenge_stale_does_not_affect_real(self):
        real = make_account(account_type="CRYPTO", balance=Decimal("10000"))
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        make_position(challenge, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="CRYPTO",
        )
        self.assertNotEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_05_funded_stale_does_not_affect_real(self):
        real = make_account(account_type="ECN", balance=Decimal("10000"))
        funded = make_account(account_type="FUNDED", balance=Decimal("10000"))
        make_position(funded, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="ECN",
        )
        self.assertNotEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_06_two_different_real_accounts_do_aggregate(self):
        real_a = make_account(account_type="RETAIL", balance=Decimal("1000000"))
        real_b = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(real_a, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        _seed_fresh_price("EUR/USD", 1.1)

        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(
                account_id=real_b.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"),
                price=Decimal("1.1"), contract_size=Decimal("100000"),
                account_type="STANDARD",
            )
        # real_a's 18 lots + real_b's new 3 = 21 > 20 -> real_b's order is
        # blocked by real_a's EXISTING exposure, proving REAL accounts
        # aggregate together under risk_scope="real", never isolated
        # per-account.
        self.assertFalse(decision.allowed)
        symbol_check = next(c for c in decision.risk_checks if c.rule == "MAX_SYMBOL_EXPOSURE")
        self.assertEqual(symbol_check.status, br.STATUS_FAIL)

    def test_07_same_user_two_accounts_margin_independent(self):
        user = make_user()
        acc_a = make_account(user=user, account_type="STANDARD", balance=Decimal("100000"))
        acc_b = make_account(user=user, account_type="ECN", balance=Decimal("100000"))
        make_position(acc_a, symbol="BTCUSD", side="BUY", qty=Decimal("10"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=acc_b.id, symbol="BTCUSD", side="BUY", qty=Decimal("1"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="ECN",
        )
        # acc_b's own margin_after must reflect ONLY acc_b's own position
        # (none yet) + this new order — never acc_a's 10-lot exposure,
        # even though both accounts belong to the same user. Leverage is
        # capped by BTCUSD's own spec max_leverage=20 (min(account
        # leverage=50, spec cap=20)), not the account's raw 50.
        expected_margin = abs(Decimal("100") * Decimal("1") * Decimal("1")) / Decimal("20")
        self.assertEqual(decision.margin_after, expected_margin)

    def test_08_user_a_cannot_consume_user_b_free_margin(self):
        user_a = make_user()
        user_b = make_user()
        acc_a = make_account(user=user_a, account_type="STANDARD", balance=Decimal("100000"))
        acc_b = make_account(user=user_b, account_type="STANDARD", balance=Decimal("100"))
        make_position(acc_a, symbol="BTCUSD", side="BUY", qty=Decimal("50"), avg_price=Decimal("100"))
        _seed_fresh_price("BTCUSD", 100)

        # acc_b, a DIFFERENT user's small account, is evaluated purely on
        # its own (empty) position set -> margin_after is only its own
        # new order's margin, never inflated by acc_a's 50-lot exposure.
        decision = validate_new_order(
            account_id=acc_b.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="STANDARD",
        )
        expected_margin = abs(Decimal("100") * Decimal("0.01") * Decimal("1")) / Decimal("20")
        self.assertEqual(decision.margin_after, expected_margin)

    def test_09_demo_huge_loss_does_not_alter_real_margin_or_exposure(self):
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        # A DEMO position deeply underwater (huge unrealized loss) plus a
        # large DEMO notional footprint.
        make_position(demo, symbol="BTCUSD", side="BUY", qty=Decimal("500"), avg_price=Decimal("100000"))
        _seed_fresh_price("BTCUSD", 1)  # DEMO position catastrophically underwater

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("1"), contract_size=Decimal("1"),
            account_type="STANDARD",
        )
        self.assertTrue(decision.allowed)
        expected_margin = abs(Decimal("1") * Decimal("0.01") * Decimal("1")) / Decimal("20")
        self.assertEqual(decision.margin_after, expected_margin)
        symbol_check = next(c for c in decision.risk_checks if c.rule == "MAX_SYMBOL_EXPOSURE")
        # DEMO's 500 lots must NOT appear in the REAL account's symbol
        # exposure reading.
        self.assertEqual(symbol_check.current_value, Decimal("0"))

    def test_10_small_real_order_does_not_skip_other_legitimate_limits(self):
        real = make_account(account_type="STANDARD", balance=Decimal("1000000"))
        make_position(real, symbol="EUR/USD", side="BUY", qty=Decimal("18"), avg_price=Decimal("1.1"))
        _seed_fresh_price("EUR/USD", 1.1)

        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("20")):
            decision = validate_new_order(
                account_id=real.id, symbol="EUR/USD", side="BUY", qty=Decimal("3"),
                price=Decimal("1.1"), contract_size=Decimal("100000"),
                account_type="STANDARD",
            )
        # 18 + 3 = 21 > 20 -> MAX_SYMBOL_EXPOSURE must still correctly
        # FAIL for this REAL account's own (real-scoped) exposure —
        # risk_scope="real" narrows WHICH accounts count, it does not
        # loosen the limit itself or exempt real accounts from it.
        self.assertFalse(decision.allowed)
        symbol_check = next(c for c in decision.risk_checks if c.rule == "MAX_SYMBOL_EXPOSURE")
        self.assertEqual(symbol_check.status, br.STATUS_FAIL)
        self.assertEqual(decision.reason_code, "MAX_SYMBOL_EXPOSURE")


# ─────────────────────────────────────────────────────────────────────────
# DEMO/CHALLENGE/FUNDED placing orders: full legacy (unscoped) behavior
# preserved — proves Option A, not an accidental Option B.
# ─────────────────────────────────────────────────────────────────────────
class DemoChallengeUnaffectedTests(_CleanFeedMixin, TestCase):
    def test_demo_order_still_contaminated_by_other_account_types(self):
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        make_position(real, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")  # a REAL account's position is stale
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=demo.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="DEMO",
        )
        # DEMO gets the FULL legacy, unscoped book — a REAL account's
        # stale position still blocks it, exactly as before O.6c-1b.
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_challenge_order_still_contaminated_by_other_account_types(self):
        challenge = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        make_position(real, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=challenge.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            account_type="CHALLENGE",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)

    def test_no_account_type_argument_defaults_to_legacy_for_any_type(self):
        # A caller (any future one) that simply never passes account_type
        # gets the identical legacy behavior regardless of what type the
        # account actually is.
        real = make_account(account_type="STANDARD", balance=Decimal("10000"))
        demo = make_account(account_type="DEMO", balance=Decimal("10000"))
        make_position(demo, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("50"))
        _clear_price("ETHUSD")
        _seed_fresh_price("BTCUSD", 100)

        decision = validate_new_order(
            account_id=real.id, symbol="BTCUSD", side="BUY", qty=Decimal("0.01"),
            price=Decimal("100"), contract_size=Decimal("1"),
            # account_type intentionally omitted
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_PRICING_INCOMPLETE)
