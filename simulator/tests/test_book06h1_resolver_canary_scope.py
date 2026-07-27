"""
BOOK-06h.1 — Resolver canary-scope correction.

Covers exactly what this subphase fixes: the exclusion query inside
_resolve_broker_exposure_for_validation() (broker_risk.py) must only
ever exclude positions belonging to accounts listed in
DEALING_DESK_EXPOSURE_ACCOUNT_IDS — never every is_simulated_hedge=True
position broker-wide, which was the BOOK-06g defect identified and
approved for correction in the BOOK-06h RFC (point 2).

validate_new_order() itself is not touched by this subphase and has no
new caller here — see test_book06g_controlled_activation_foundation.py
for the structural proof that it still calls broker_exposure_snapshot()
directly. No flags are activated by these tests beyond the isolated,
already-established @override_settings pattern used since BOOK-06g.
"""
from decimal import Decimal

from django.test import TestCase, override_settings

from simulator import broker_risk
from simulator.models import DealingDeskDecision, Position, RoutingDecision
from simulator.routing_engine import Book

from .factories import make_account


def _make_position(account, symbol="BTCUSD", side="BUY", qty="1.0", avg_price="100.00",
                    is_simulated_hedge=None):
    routing_decision = RoutingDecision.objects.create(
        book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
    )
    pos = Position.objects.create(
        account=account, symbol=symbol, side=side,
        qty=Decimal(qty), avg_price=Decimal(avg_price),
        routing_decision=routing_decision,
    )
    if is_simulated_hedge is not None:
        DealingDeskDecision.objects.create(
            routing_decision=routing_decision, position=pos, symbol=symbol,
            is_simulated_hedge=is_simulated_hedge, routing_profile_snapshot="HEDGE_CANDIDATE",
        )
    return pos, routing_decision


class ResolverExclusionScopeTests(TestCase):

    def setUp(self):
        from market_data.feeds import get_feed_manager
        import time
        feed = get_feed_manager()
        with feed._lock:
            feed._prices["BTCUSD"] = 100.0
            feed._bids["BTCUSD"] = 100.0
            feed._asks["BTCUSD"] = 100.0
            feed._price_ts["BTCUSD"] = time.time()
        self.addCleanup(self._clear_price)

        self.canary_account = make_account(balance=Decimal("100000"))
        self.other_account = make_account(balance=Decimal("100000"))

    def _clear_price(self):
        from market_data.feeds import get_feed_manager
        feed = get_feed_manager()
        with feed._lock:
            feed._prices.pop("BTCUSD", None)
            feed._bids.pop("BTCUSD", None)
            feed._asks.pop("BTCUSD", None)
            feed._price_ts.pop("BTCUSD", None)

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_exclusion_scoped_to_allowlisted_accounts_only(self):
        """A position with is_simulated_hedge=True from an account OUTSIDE
        the allowlist must remain in the aggregate — only the canary
        account's own hedge-classified position is excluded."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="1.0", avg_price="100.00", is_simulated_hedge=True)
            _make_position(self.other_account, qty="3.0", avg_price="100.00", is_simulated_hedge=True)

            result = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)

        # Canary's hedge position excluded (100.00), other account's
        # hedge position (300.00) stays in — it does not belong to any
        # allowlisted account.
        self.assertEqual(result.gross_notional, Decimal("300.00"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_non_canary_hedge_position_never_excluded_even_with_gate_true_elsewhere(self):
        """Symmetric check: querying the resolver for the canary account
        must not accidentally sweep in every is_simulated_hedge=True row
        in the table (the exact BOOK-06g defect)."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.other_account, qty="5.0", avg_price="100.00", is_simulated_hedge=True)

            result = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)

        self.assertEqual(result.gross_notional, Decimal("500.00"))
        self.assertEqual(result.open_position_count, 1)

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_decision_with_null_position_is_safely_ignored(self):
        """A DealingDeskDecision row whose position was later SET_NULL
        (position closed/deleted) must never raise and must not affect
        the aggregate — excluding an absent id is a documented no-op."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _, routing_decision = _make_position(
                self.canary_account, qty="1.0", avg_price="100.00", is_simulated_hedge=False,
            )
            DealingDeskDecision.objects.create(
                routing_decision=routing_decision, position=None, symbol="BTCUSD",
                is_simulated_hedge=True, routing_profile_snapshot="HEDGE_CANDIDATE",
            )

            try:
                result = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
            except Exception as exc:
                self.fail(f"resolver raised {exc!r} on a null-position decision — must never raise")

        self.assertEqual(result.gross_notional, Decimal("100.00"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_decision_with_null_routing_decision_never_matches_allowlist(self):
        """A DealingDeskDecision detached from its RoutingDecision
        (SET_NULL) can never join to an account_id — it must simply not
        match the allowlist filter, not raise or over-exclude."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            pos, _ = _make_position(
                self.canary_account, qty="2.0", avg_price="100.00", is_simulated_hedge=False,
            )
            DealingDeskDecision.objects.create(
                routing_decision=None, position=pos, symbol="BTCUSD",
                is_simulated_hedge=True, routing_profile_snapshot="HEDGE_CANDIDATE",
            )

            result = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)

        # position stays in the aggregate: its only DealingDeskDecision
        # cannot be scoped to any account, so it cannot be excluded.
        self.assertEqual(result.gross_notional, Decimal("200.00"))

    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_duplicate_decisions_for_same_position_deduplicated(self):
        """Two DealingDeskDecision rows pointing at the same position
        (both is_simulated_hedge=True, both in-allowlist) must collapse
        to a single exclusion — frozenset() dedup, no double-counting,
        no error."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            pos, routing_decision = _make_position(
                self.canary_account, qty="1.0", avg_price="100.00", is_simulated_hedge=True,
            )
            DealingDeskDecision.objects.create(
                routing_decision=routing_decision, position=pos, symbol="BTCUSD",
                is_simulated_hedge=True, routing_profile_snapshot="HEDGE_CANDIDATE",
            )
            _make_position(self.other_account, qty="4.0", avg_price="100.00", is_simulated_hedge=False)

            result = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)

        self.assertEqual(result.gross_notional, Decimal("400.00"))
        self.assertEqual(result.open_position_count, 1)
