"""
BOOK-06i — Canary Preconditions + End-to-End Contract.

Closes the two gaps RC-1 (docs/BOOK06_RC1_AUDIT.md) left explicitly open
before a first real canary trial:

  FASE 2 — a permanent regression test pinning the HIGH finding (F-01)
  as ACCEPTED, conservative, intentional behavior: excluding a
  is_simulated_hedge=True position from a directionally-biased book can
  turn an official MAX_NET_NOTIONAL PASS into an adjusted REJECT. This
  is NOT a bug — see RC-1 §6.2 and the BOOK-06i business/risk sign-off.
  Nothing here makes the exclusion more permissive; this test exists to
  make the non-monotonic property a known, tested contract instead of an
  undocumented surprise.

  FASE 3 — the minimum E2E test that RC-1 identified as missing: the
  real order:new -> RoutingDecision -> LiquidityDecision ->
  DealingDeskDecision(is_simulated_hedge=True) -> resolver ->
  exclude_position_ids -> adjusted broker exposure ->
  validate_new_order() chain, exercised through TradingConsumer's real
  code path (not fabricated ORM rows), scoped to a disposable CHALLENGE
  test account — never a real account, never account 51.

Every fixture in this file is CHALLENGE-type (make_account's default,
no real money) and every flag is enabled only inside the specific test
via override_settings — nothing here changes any default in settings.py
or activates anything outside the test process.

Immutability (Scenario E): RoutingDecision/LiquidityDecision are BOOK-04/
BOOK-05's own append-only, never-modified contract — DealingDeskDecision
extends the same rule (see its own docstring). This file re-verifies
that contract at the BOOK-06i integration surface specifically, on top
of (never replacing) BOOK-04a's own UpstreamDecisionsNeverModifiedTests
and BOOK-06a's own equivalent.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings

from market_data.feeds import get_feed_manager
from simulator import broker_exposure as _exposure
from simulator import broker_risk as br
from simulator.models import (
    DealingDeskDecision, LiquidityDecision, LiquidityProvider, Position,
    RoutingDecision, TraderScore,
)
from simulator.routing_engine import Book

from .factories import make_account
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run


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


def _provider(name="LP-BOOK06I", spread="5.0", capacity="1000000", symbols=None, enabled=True):
    return LiquidityProvider.objects.create(
        name=name,
        symbols_covered=symbols if symbols is not None else ["BTCUSD"],
        simulated_spread_markup_pips=Decimal(spread),
        max_capacity_usd=Decimal(capacity),
        enabled=enabled,
    )


def _make_position(account, symbol="BTCUSD", side="BUY", qty="1.0", avg_price="100.00",
                    is_simulated_hedge=None, with_routing_decision=True):
    """Direct-ORM fixture — same shape as
    test_book06h2_real_activation_integration.py's own helper. Used only
    where the test needs precise control over the book (FASE 2, and
    FASE 3's Scenario D2/E setup), never as a substitute for the real
    order:new path exercised by Scenarios A/B/C/D1 below."""
    routing_decision = None
    if with_routing_decision:
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
    pos = Position.objects.create(
        account=account, symbol=symbol, side=side,
        qty=Decimal(qty), avg_price=Decimal(avg_price),
        routing_decision=routing_decision,
    )
    if is_simulated_hedge is not None and routing_decision is not None:
        DealingDeskDecision.objects.create(
            routing_decision=routing_decision, position=pos, symbol=symbol,
            is_simulated_hedge=is_simulated_hedge, routing_profile_snapshot="HEDGE_CANDIDATE",
        )
    return pos, routing_decision


# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — HIGH finding (F-01) regression: MAX_NET_NOTIONAL non-monotonic
# under exclusion is ACCEPTED policy, pinned as a permanent contract.
# Reproduces exactly docs/BOOK06_RC1_AUDIT.md §6.2's numeric scenario.
# ─────────────────────────────────────────────────────────────────────────
class MaxNetNotionalNonMonotonicRegressionTests(TestCase):

    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.canary = make_account(balance=Decimal("10000000"))
        self.other = make_account(balance=Decimal("10000000"))
        # BUY 600 @ 100 = 60,000 long notional (non-canary account).
        _make_position(self.other, side="BUY", qty="600", avg_price="100")
        # SELL 250 @ 100 = 25,000 short notional, not excludable (non-canary).
        _make_position(self.other, side="SELL", qty="250", avg_price="100")
        # SELL 100 @ 100 = 10,000 short notional, canary account, marked
        # is_simulated_hedge=True — the position the canary would exclude.
        _make_position(self.canary, side="SELL", qty="100", avg_price="100", is_simulated_hedge=True)

    def test_official_book_net_notional_is_25000_and_passes_at_30000_threshold(self):
        """Baseline: confirms the fixture itself is not already failing —
        the REJECT in the next test comes from the exclusion, not from
        the book being over-threshold to begin with."""
        official = _exposure.broker_exposure_snapshot()
        self.assertEqual(official.net_notional, Decimal("25000"))

        with patch.object(br, "MAX_NET_NOTIONAL", Decimal("30000")):
            decision = br.validate_new_order(
                account_id=self.canary.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.0001"),
                price=Decimal("100"), contract_size=Decimal("1"),
            )
        net_check = next(c for c in decision.risk_checks if c.rule == "MAX_NET_NOTIONAL")
        self.assertEqual(net_check.status, br.STATUS_PASS)
        self.assertTrue(decision.allowed)

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_exclusion_flips_official_pass_into_adjusted_reject(self):
        """The pinned HIGH finding: with the canary active for this
        account, excluding the 100-lot SELL (is_simulated_hedge=True)
        raises net_notional from 25,000 to 35,000 — a PASS the official
        book would have given becomes a REJECT. ACCEPTED, conservative
        behavior — do not "fix" this by changing the math."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary.pk})), \
             patch.object(br, "MAX_NET_NOTIONAL", Decimal("30000")):

            adjusted_book = br._resolve_broker_exposure_for_validation(self.canary.pk)
            self.assertEqual(adjusted_book.net_notional, Decimal("35000"))

            decision = br.validate_new_order(
                account_id=self.canary.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.0001"),
                price=Decimal("100"), contract_size=Decimal("1"),
            )

        net_check = next(c for c in decision.risk_checks if c.rule == "MAX_NET_NOTIONAL")
        self.assertEqual(net_check.status, br.STATUS_FAIL)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "MAX_NET_NOTIONAL")

    def test_same_order_passes_without_canary_active(self):
        """Control: identical order, identical fixture, canary OFF
        (default) -> official book -> PASS. Isolates that FASE 2's
        REJECT above is caused exclusively by the exclusion, confirmed
        by toggling only the flag, nothing else."""
        with patch.object(br, "MAX_NET_NOTIONAL", Decimal("30000")):
            decision = br.validate_new_order(
                account_id=self.canary.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.0001"),
                price=Decimal("100"), contract_size=Decimal("1"),
            )
        net_check = next(c for c in decision.risk_checks if c.rule == "MAX_NET_NOTIONAL")
        self.assertEqual(net_check.status, br.STATUS_PASS)
        self.assertTrue(decision.allowed)


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — minimum E2E contract RC-1 identified as missing. Scenarios
# A-C and D1 go through the REAL TradingConsumer._order_new() path (same
# harness as test_book06c_dealing_desk_integration_open.py) — never
# fabricated DealingDeskDecision rows. D2 and E use direct-ORM fixtures
# where the scenario specifically requires a state _order_new() cannot
# produce (no RoutingDecision at all) or requires inspecting rows
# untouched by two separate evaluations.
#
# Every account here is CHALLENGE-type (make_account's default) and
# disposable — never account 51, never any pre-existing account.
# ─────────────────────────────────────────────────────────────────────────
class CanaryEndToEndContractTests(TransactionTestCase):

    def setUp(self):
        # Realistic BTCUSD magnitude (base_price=82000 in symbol_specs) —
        # the real order:new path enforces a plausibility gate on the raw
        # feed price (O.6c-1w-b); a round test price like 100 is rejected
        # as implausible and never reaches validate_new_order() at all.
        # Same convention as test_book06c_dealing_desk_integration_open.py.
        _seed_price("BTCUSD", 63000.0)
        self.addCleanup(_clear_price, "BTCUSD")

    # ── A. Cuenta FUERA del canario: la clasificación puede existir,
    #        pero validate_new_order() sigue usando la exposición oficial ──
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_a_account_outside_allowlist_uses_official_exposure_despite_classification(self):
        account = make_account(balance=Decimal("100000"))
        other_allowlisted_account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
        self.assertIsNone(_first_error(consumer))

        dd_decision = DealingDeskDecision.objects.get()
        self.assertTrue(dd_decision.is_simulated_hedge)  # la clasificación real SÍ existe

        official_gross = _exposure.broker_exposure_snapshot().gross_quantity

        with override_settings(
            DEALING_DESK_EXPOSURE_ENABLED=True,
            DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({other_allowlisted_account.pk}),
        ):
            decision = br.validate_new_order(
                account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"),
            )

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.05"))

    # ── B. Cuenta DENTRO del allowlist, flag OFF: sigue usando oficial ──
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_b_allowlisted_account_flag_off_uses_official_exposure(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
        self.assertIsNone(_first_error(consumer))
        self.assertTrue(DealingDeskDecision.objects.get().is_simulated_hedge)

        official_gross = _exposure.broker_exposure_snapshot().gross_quantity

        with override_settings(
            DEALING_DESK_EXPOSURE_ENABLED=False,  # explícito, aunque False ya es el default
            DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({account.pk}),
        ):
            decision = br.validate_new_order(
                account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"),
            )

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.05"))

    # ── C. Cuenta DENTRO del allowlist, flag ON: la cadena completa,
    #        de punta a punta, real ──────────────────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_c_allowlisted_account_flag_on_full_chain_reaches_adjusted_exposure(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        # order:new
        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
        self.assertIsNone(_first_error(consumer))

        # -> RoutingDecision -> LiquidityDecision -> DealingDeskDecision(is_simulated_hedge=True)
        pos = Position.objects.get(account=account)
        routing_decision = RoutingDecision.objects.get(pk=pos.routing_decision_id)
        liquidity_decision = LiquidityDecision.objects.get()
        dd_decision = DealingDeskDecision.objects.get()

        self.assertEqual(routing_decision.book, Book.INTERNAL)
        self.assertTrue(dd_decision.is_simulated_hedge)
        self.assertEqual(dd_decision.routing_decision_id, routing_decision.id)
        self.assertEqual(dd_decision.liquidity_decision_id, liquidity_decision.id)
        self.assertEqual(dd_decision.position_id, pos.id)

        official_gross = _exposure.broker_exposure_snapshot().gross_quantity

        # -> resolver -> exclude_position_ids -> adjusted broker exposure
        #    -> validate_new_order() usando adjusted exposure, SOLO esta cuenta
        with override_settings(
            DEALING_DESK_EXPOSURE_ENABLED=True,
            DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({account.pk}),
        ):
            adjusted_book = br._resolve_broker_exposure_for_validation(account.pk)
            self.assertIsNotNone(getattr(adjusted_book, "_dealing_desk_observability", None))
            self.assertEqual(adjusted_book.gross_quantity, official_gross - Decimal("0.1"))

            decision = br.validate_new_order(
                account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"),
            )

        self.assertEqual(decision.exposure_after, (official_gross - Decimal("0.1")) + Decimal("0.05"))
        self.assertNotEqual(decision.exposure_after, official_gross + Decimal("0.05"))

    # ── D1. Fail-safe: falta LiquidityDecision -> nunca is_simulated_hedge
    #         -> nunca excluida silenciosamente ──────────────────────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=False)
    def test_d1_missing_liquidity_decision_never_silently_excluded(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
        self.assertIsNone(_first_error(consumer))

        self.assertEqual(LiquidityDecision.objects.count(), 0)
        dd_decision = DealingDeskDecision.objects.get()
        self.assertFalse(dd_decision.is_simulated_hedge)  # sin LiquidityDecision -> nunca True

        official_gross = _exposure.broker_exposure_snapshot().gross_quantity
        with override_settings(
            DEALING_DESK_EXPOSURE_ENABLED=True,
            DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({account.pk}),
        ):
            decision = br.validate_new_order(
                account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"),
            )

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.05"))  # NO excluida

    # ── D2. Fail-safe: posición sin RoutingDecision (NULL) -> nunca
    #         puede hacer match con el allowlist -> nunca excluida ───────
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_d2_position_without_routing_decision_never_excluded(self):
        account = make_account(balance=Decimal("100000"))
        _make_position(account, side="BUY", qty="1.0", avg_price="100", with_routing_decision=False)
        self.assertIsNone(Position.objects.get(account=account).routing_decision_id)

        official_gross = _exposure.broker_exposure_snapshot().gross_quantity
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({account.pk})):
            decision = br.validate_new_order(
                account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"),
            )

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.05"))

    # ── E. Inmutabilidad: ninguna RoutingDecision/LiquidityDecision
    #        histórica se modifica para producir el resultado ───────────
    @override_settings(ROUTING_ENGINE_ENABLED=True, LIQUIDITY_ENGINE_ENABLED=True)
    def test_e_canary_evaluation_never_mutates_routing_or_liquidity_decisions(self):
        account = make_account(balance=Decimal("100000"))
        TraderScore.objects.create(account=account, routing_profile="HEDGE_CANDIDATE")
        _provider()
        consumer = _consumer(account.pk)

        _run(consumer._order_new({"symbol": "BTCUSD", "side": "buy", "qty": 0.1}))
        self.assertIsNone(_first_error(consumer))

        pos = Position.objects.get(account=account)
        routing_decision = RoutingDecision.objects.get(pk=pos.routing_decision_id)
        liquidity_decision = LiquidityDecision.objects.get()

        rd_before = (
            routing_decision.book, routing_decision.reason_code, routing_decision.reason_message,
            routing_decision.account_id, routing_decision.position_id, routing_decision.decided_at,
        )
        ld_before = (
            liquidity_decision.symbol, liquidity_decision.exposure_usd, liquidity_decision.simulated_spread,
            liquidity_decision.simulated_cost, liquidity_decision.provider_id, liquidity_decision.position_id,
            liquidity_decision.routing_decision_id, liquidity_decision.decided_at,
        )
        rd_count_before = RoutingDecision.objects.count()
        ld_count_before = LiquidityDecision.objects.count()

        with override_settings(
            DEALING_DESK_EXPOSURE_ENABLED=True,
            DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({account.pk}),
        ):
            # Dos evaluaciones — confirma que ni una sola vez, ni
            # repetida, el resolver o validate_new_order() tocan estas
            # filas (nunca .save()/.update() sobre RoutingDecision o
            # LiquidityDecision — ambas solo se leen con .filter()).
            br.validate_new_order(account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"))
            br.validate_new_order(account_id=account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.05"))

        routing_decision.refresh_from_db()
        liquidity_decision.refresh_from_db()

        rd_after = (
            routing_decision.book, routing_decision.reason_code, routing_decision.reason_message,
            routing_decision.account_id, routing_decision.position_id, routing_decision.decided_at,
        )
        ld_after = (
            liquidity_decision.symbol, liquidity_decision.exposure_usd, liquidity_decision.simulated_spread,
            liquidity_decision.simulated_cost, liquidity_decision.provider_id, liquidity_decision.position_id,
            liquidity_decision.routing_decision_id, liquidity_decision.decided_at,
        )

        self.assertEqual(rd_before, rd_after)
        self.assertEqual(ld_before, ld_after)
        self.assertEqual(RoutingDecision.objects.count(), rd_count_before)
        self.assertEqual(LiquidityDecision.objects.count(), ld_count_before)
