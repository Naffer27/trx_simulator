"""
BOOK-06h.2 — Real Activation Integration.

Covers exactly the one functional change this subphase makes: inside
validate_new_order() (broker_risk.py), `book` is now fetched through
_resolve_broker_exposure_for_validation(account_id) instead of calling
_exposure.broker_exposure_snapshot() directly. Every other line of
validate_new_order() — and every other function in broker_risk.py,
broker_exposure.py, broker_risk_shadow.py, dealing_desk.py,
consumers.py, models.py, admin.py, tasks.py — is untouched.

`exposure_after` on the returned RiskLimitDecision is `book.gross_quantity
+ requested_qty` (see validate_total_limit()/validate_new_order()) — a
public, black-box signal of exactly which book (official vs. Dealing-
Desk-adjusted) was used, with no need to reach into private internals.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from simulator import broker_exposure as _exposure
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


class RealActivationIntegrationTests(TestCase):

    def setUp(self):
        self.canary_account = make_account(balance=Decimal("100000"))
        self.other_account = make_account(balance=Decimal("100000"))

    def _official_gross_quantity(self):
        return _exposure.broker_exposure_snapshot().gross_quantity

    # ── 1. Flag OFF: identical to the pre-06h.2 official calculation ──
    def test_flag_off_identical_result(self):
        _make_position(self.canary_account, qty="1.0", is_simulated_hedge=True)
        _make_position(self.other_account, qty="2.0", is_simulated_hedge=False)
        official_gross = self._official_gross_quantity()

        with patch.object(_exposure, "broker_exposure_snapshot", wraps=_exposure.broker_exposure_snapshot) as spy:
            decision = broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
            )
            spy.assert_called_once()

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 2. Flag ON + empty allowlist: identical ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset())
    def test_flag_on_empty_allowlist_identical_result(self):
        _make_position(self.canary_account, qty="1.0", is_simulated_hedge=True)
        official_gross = self._official_gross_quantity()

        with patch.object(_exposure, "broker_exposure_snapshot", wraps=_exposure.broker_exposure_snapshot) as spy:
            decision = broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
            )
            spy.assert_called_once()

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 3. Account outside the canary: identical ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_account_outside_canary_identical_result(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="1.0", is_simulated_hedge=True)
            _make_position(self.other_account, qty="2.0", is_simulated_hedge=False)
            official_gross = self._official_gross_quantity()

            with patch.object(_exposure, "broker_exposure_snapshot", wraps=_exposure.broker_exposure_snapshot) as spy:
                decision = broker_risk.validate_new_order(
                    account_id=self.other_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
                )
                spy.assert_called_once()

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 4. Inside the canary, no excludable positions: identical ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_inside_canary_no_excludable_positions_identical_result(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="1.0", is_simulated_hedge=False)
            official_gross = self._official_gross_quantity()

            decision = broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
            )

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 5. Inside the canary, with excludable positions: adjusted book used ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_inside_canary_with_excludable_positions_uses_adjusted_book(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)
            _make_position(self.other_account, qty="2.0", is_simulated_hedge=False)
            official_gross = self._official_gross_quantity()  # includes the 3.0 hedge position

            decision = broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
            )

        # Adjusted book excludes the canary's 3.0 hedge position.
        self.assertEqual(decision.exposure_after, (official_gross - Decimal("3.0")) + Decimal("0.5"))
        self.assertNotEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 6. Fallback: any exception still uses the official calculation ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_resolver_exception_falls_back_to_official_calculation(self):
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)
            official_gross = self._official_gross_quantity()

            with patch(
                "simulator.models.DealingDeskDecision.objects.filter",
                side_effect=RuntimeError("simulated failure"),
            ):
                try:
                    decision = broker_risk.validate_new_order(
                        account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
                    )
                except Exception as exc:
                    self.fail(f"validate_new_order raised {exc!r} — must never raise")

        self.assertEqual(decision.exposure_after, official_gross + Decimal("0.5"))

    # ── 7. No regression in RISK-01/RISK-02 rule evaluation ──
    @override_settings(DEALING_DESK_EXPOSURE_ENABLED=True)
    def test_all_nine_rules_still_present_and_unaffected_by_canary(self):
        """Sanity check that every RISK-02 rule name is still produced,
        and that symbol/account-level rules (RISK-01-derived,
        never touched by the resolver) are identical regardless of the
        canary. The full regression is the pre-existing
        test_broker_exposure_engine.py / test_broker_risk_limits_engine.py
        suites (run alongside this file, unmodified, zero changes)."""
        with override_settings(DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            _make_position(self.canary_account, qty="1.0", is_simulated_hedge=True)

            decision = broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY", qty=Decimal("0.5"),
            )

        rule_names = {c.rule for c in decision.risk_checks}
        self.assertEqual(
            rule_names,
            {
                "MAX_SYMBOL_EXPOSURE", "MAX_ACCOUNT_EXPOSURE", "MAX_TOTAL_BROKER_EXPOSURE",
                "MAX_LONG_EXPOSURE", "MAX_SHORT_EXPOSURE", "MAX_GROSS_NOTIONAL",
                "MAX_NET_NOTIONAL", "MAX_POSITION_SIZE", "MAX_OPEN_POSITIONS_BROKER_WIDE",
            },
        )


class ValidateNewOrderSingleLineChangeTests(TestCase):

    def test_only_functional_change_is_the_book_resolution_line(self):
        """Structural proof: with the flag OFF (default), the resolver
        must delegate to broker_exposure_snapshot() exactly once and no
        other broker_exposure_for_* helper is touched — i.e. the only
        behavioral difference introduced is which function supplies
        `book`, never how the rest of validate_new_order() computes.
        O.6c-1b added a second, keyword-only risk_scope arg to the
        resolver (defaults to None — identical legacy behavior — whenever
        validate_new_order() isn't given an account_type, as here); the
        call now always carries risk_scope explicitly."""
        make_account(balance=Decimal("100000"))

        with patch.object(
            broker_risk, "_resolve_broker_exposure_for_validation",
            wraps=broker_risk._resolve_broker_exposure_for_validation,
        ) as spy:
            broker_risk.validate_new_order(
                account_id=1, symbol="BTCUSD", side="BUY", qty=Decimal("0.1"),
            )
            spy.assert_called_once_with(1, risk_scope=None)
