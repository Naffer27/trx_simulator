"""
simulator/tests/test_funded_challenge_audit.py — Bloque G

Auditoría de reglas de cuentas challenge/fondeadas (funded) y proceso de payout.

Gaps documentados (estos tests NO implementan fixes, solo documentan el estado actual):

  Gap A: FundedConfig.max_monthly_drawdown_pct existe pero nunca se evalúa.
          Una cuenta fondeada con pérdida mensual >5% sigue Activo indefinidamente.

  Gap B: No hay reset de ciclo de payout. initial_balance nunca se actualiza tras
          cobrar, por lo que cycle_profit se acumula desde la creación de la cuenta.

  Gap C: funded_payout_eligible=True es solo display. El sistema no crea
          automáticamente WithdrawalRequest ni LedgerEntry de payout fondeado.
          No existe EV_PAYOUT en LedgerEntry.EVENT_CHOICES.

  Gap D: _daily_realized_dd_pct en challenge_engine usa created_at__date=today
          en vez del rango UTC explícito (__gte/__lt) del Bloque D.

  Gap E: FundedConfig.funded_type (FUNDED_SIM vs FUNDED_INTERNAL) no cambia
          ningún flujo de negocio — la distinción solo existe en modelo/admin.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
    evaluate_phase,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    LedgerEntry,
    TradingAccount,
    WithdrawalRequest,
)

User = get_user_model()

_ZERO = Decimal("0")
_PENNY = Decimal("0.01")
_HUNDRED = Decimal("100")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_user_seq = 0


def _make_user():
    global _user_seq
    _user_seq += 1
    return User.objects.create_user(
        username=f"g_audit_{_user_seq}",
        email=f"g_audit_{_user_seq}@example.com",
        password="testpass123",
    )


def _make_product(**overrides):
    """ChallengeProduct with safe test-only defaults."""
    defaults = dict(
        name="Audit-G 10K",
        account_size=Decimal("10000.00"),
        price_usd=Decimal("99.00"),
        is_active=True,
        # Phase 1 rules
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=4,
        p1_max_duration_days=30,
        # Phase 2 rules
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=4,
        p2_max_duration_days=60,
        # Shared across phases
        max_lot_size=Decimal("5.00"),
        max_open_positions=5,
        # Funded
        profit_split_pct=Decimal("80.00"),
    )
    defaults.update(overrides)
    return ChallengeProduct.objects.create(**defaults)


def _make_enrollment(user=None, product=None):
    """Create enrollment at Phase 1 with activated phase1_account."""
    if user is None:
        user = _make_user()
    if product is None:
        product = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _advance_to_funded(enrollment):
    """Force-advance enrollment through Phase 2 to Funded state."""
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _funded_cycle_profit(account):
    """Mirrors the cycle_profit formula in views.py."""
    funded_bal = Decimal(str(account.balance))
    funded_init = Decimal(str(account.initial_balance or account.balance))
    return max(_ZERO, funded_bal - funded_init)


def _compute_split(cycle_profit, profit_split_pct):
    """Mirrors the split formula in views.py:509-511."""
    split_pct = Decimal(str(profit_split_pct))
    trader_cut = (cycle_profit * split_pct / _HUNDRED).quantize(_PENNY)
    broker_cut = (cycle_profit - trader_cut).quantize(_PENNY)
    return trader_cut, broker_cut


# ─────────────────────────────────────────────────────────────────────────────
# Challenge lifecycle: Phase1 → Phase2 → Funded states
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeLifecycleStates(TestCase):

    def setUp(self):
        self.enrollment = _make_enrollment()

    def test_activate_creates_phase1_account(self):
        self.assertIsNotNone(self.enrollment.phase1_account)

    def test_phase1_account_type_is_challenge(self):
        self.assertEqual(self.enrollment.phase1_account.account_type, "CHALLENGE")

    def test_phase1_account_status_is_activo(self):
        self.assertEqual(self.enrollment.phase1_account.status, TradingAccount.STATUS_ACTIVE)

    def test_initial_enrollment_status_is_phase1(self):
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_1)

    def test_advance_to_phase2_creates_phase2_account(self):
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.phase2_account)

    def test_advance_to_phase2_changes_status(self):
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_PHASE_2)

    def test_advance_to_phase2_marks_phase1_completado(self):
        advance_to_phase2(self.enrollment)
        self.enrollment.refresh_from_db()
        self.enrollment.phase1_account.refresh_from_db()
        self.assertEqual(self.enrollment.phase1_account.status, TradingAccount.STATUS_FUNDED)

    def test_advance_to_funded_changes_status_to_funded(self):
        _advance_to_funded(self.enrollment)
        self.assertEqual(self.enrollment.status, ChallengeEnrollment.ST_FUNDED)

    def test_advance_to_funded_creates_funded_account(self):
        _advance_to_funded(self.enrollment)
        self.assertIsNotNone(self.enrollment.funded_account)

    def test_advance_to_funded_account_type_is_funded(self):
        _advance_to_funded(self.enrollment)
        self.assertEqual(self.enrollment.funded_account.account_type, "FUNDED")

    def test_advance_to_funded_account_status_is_activo(self):
        _advance_to_funded(self.enrollment)
        self.assertEqual(
            self.enrollment.funded_account.status, TradingAccount.STATUS_ACTIVE
        )

    def test_advance_to_funded_creates_funded_config(self):
        _advance_to_funded(self.enrollment)
        self.assertTrue(
            FundedConfig.objects.filter(enrollment=self.enrollment).exists()
        )

    def test_funded_config_profit_split_matches_product(self):
        _advance_to_funded(self.enrollment)
        fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.assertEqual(fc.profit_split_pct, self.enrollment.product.profit_split_pct)

    def test_funded_at_timestamp_set(self):
        _advance_to_funded(self.enrollment)
        self.assertIsNotNone(self.enrollment.funded_at)

    def test_funded_account_balance_matches_product_size(self):
        _advance_to_funded(self.enrollment)
        expected = self.enrollment.product.account_size
        self.assertEqual(
            Decimal(str(self.enrollment.funded_account.balance)), expected
        )


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_phase() on ST_FUNDED enrollment → always IN_PROGRESS
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluatePhaseOnFundedEnrollment(TestCase):
    """
    Gap (undocumented): _phase_rules() has no branch for ST_FUNDED.
    evaluate_phase() hits the ``if not rules`` guard and returns FAILED with
    "Enrollment is not in an evaluable phase" — not IN_PROGRESS.
    Production code does not distinguish "funded, skip evaluation" from "bad state".
    """

    def setUp(self):
        self.enrollment = _make_enrollment()
        _advance_to_funded(self.enrollment)
        self.funded_account = self.enrollment.funded_account

    def test_evaluate_phase_returns_failed_not_evaluable(self):
        # Gap: ST_FUNDED has no branch in _phase_rules() → falls through to FAILED.
        # A proper implementation should return IN_PROGRESS (or a dedicated FUNDED status).
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.fail_reason, "Enrollment is not in an evaluable phase")

    def test_evaluate_phase_returns_failed_regardless_of_drawdown(self):
        # Gap: evaluate_phase() exits at the _phase_rules() guard before checking any
        # drawdown rule, so the FAILED reason is always "not evaluable", not drawdown.
        self.funded_account.balance = (
            Decimal(str(self.funded_account.initial_balance)) * Decimal("0.40")
        )
        self.funded_account.save()
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.fail_reason, "Enrollment is not in an evaluable phase")

    def test_funded_account_status_remains_activo_with_severe_drawdown(self):
        """Account stays Activo — no automatic enforcement on funded accounts."""
        self.funded_account.balance = (
            Decimal(str(self.funded_account.initial_balance)) * Decimal("0.40")
        )
        self.funded_account.save()
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.status, TradingAccount.STATUS_ACTIVE)


# ─────────────────────────────────────────────────────────────────────────────
# Gap A — max_monthly_drawdown_pct exists in FundedConfig but is never evaluated
# ─────────────────────────────────────────────────────────────────────────────

class TestGapAMonthlyDrawdownNotEnforced(TestCase):
    """
    Gap A: FundedConfig.max_monthly_drawdown_pct stores the monthly limit but
    no production code compares it against actual monthly drawdown.
    """

    def setUp(self):
        self.enrollment = _make_enrollment()
        _advance_to_funded(self.enrollment)
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.funded_account = self.enrollment.funded_account

    def test_funded_config_has_max_monthly_drawdown_pct(self):
        """Field exists and is non-zero — the limit is defined."""
        self.assertIsNotNone(self.fc.max_monthly_drawdown_pct)
        self.assertGreater(self.fc.max_monthly_drawdown_pct, _ZERO)

    def test_default_max_monthly_drawdown_is_5_pct(self):
        self.assertEqual(self.fc.max_monthly_drawdown_pct, Decimal("5.00"))

    def test_monthly_loss_above_limit_does_not_breach_account(self):
        """
        Gap A: funded account with 8% monthly realized loss (>5% limit) stays Activo.
        There is no evaluator that enforces max_monthly_drawdown_pct.
        """
        monthly_loss = Decimal(str(self.funded_account.initial_balance)) * Decimal("0.08")
        self.funded_account.balance = (
            Decimal(str(self.funded_account.initial_balance)) - monthly_loss
        )
        self.funded_account.save()

        # Simulate monthly realized losses in ledger
        LedgerEntry.objects.create(
            account=self.funded_account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=-monthly_loss,
            balance_after=self.funded_account.balance,
            meta={},
        )

        # Gap: evaluate_phase() returns FAILED ("not evaluable") for funded enrollments,
        # not IN_PROGRESS. The monthly drawdown limit is never read by any evaluator.
        result = evaluate_phase(self.enrollment)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.fail_reason, "Enrollment is not in an evaluable phase")

        # Account stays Activo — no monthly drawdown enforcer runs
        self.funded_account.refresh_from_db()
        self.assertEqual(
            self.funded_account.status,
            TradingAccount.STATUS_ACTIVE,
            "Gap A: funded account exceeding max_monthly_drawdown_pct stays Activo",
        )

    def test_monthly_loss_at_exact_limit_also_not_enforced(self):
        """
        Gap A: even exactly at the limit (5%), no enforcement happens.
        """
        exact_loss = (
            Decimal(str(self.funded_account.initial_balance))
            * self.fc.max_monthly_drawdown_pct
            / _HUNDRED
        )
        self.funded_account.balance = (
            Decimal(str(self.funded_account.initial_balance)) - exact_loss
        )
        self.funded_account.save()

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.status, TradingAccount.STATUS_ACTIVE)


# ─────────────────────────────────────────────────────────────────────────────
# Gap C — no EV_PAYOUT event type exists
# ─────────────────────────────────────────────────────────────────────────────

class TestGapCNoPayoutLedgerEventType(TestCase):
    """
    Gap C: LedgerEntry.EVENT_CHOICES has no PAYOUT type.
    Funded payouts leave no trace in the trading account ledger.
    """

    def test_no_payout_event_type_in_ledger(self):
        ev_types = [c[0] for c in LedgerEntry.EVENT_CHOICES]
        self.assertNotIn("PAYOUT", ev_types)

    def test_no_ev_payout_constant_on_ledger_entry(self):
        self.assertFalse(hasattr(LedgerEntry, "EV_PAYOUT"))

    def test_existing_event_types(self):
        """Document what event types DO exist."""
        ev_types = {c[0] for c in LedgerEntry.EVENT_CHOICES}
        for expected in [
            LedgerEntry.EV_DEPOSIT,
            LedgerEntry.EV_WITHDRAW,
            LedgerEntry.EV_REALIZED,
            LedgerEntry.EV_COMMISSION,
            LedgerEntry.EV_FEE,
            LedgerEntry.EV_ADJUST,
        ]:
            self.assertIn(expected, ev_types)


class TestGapCPayoutEligibleDoesNotTriggerAction(TestCase):
    """
    Gap C: funded_payout_eligible is display-only. Even when all eligibility
    conditions are met, no WithdrawalRequest or LedgerEntry is created automatically.
    """

    def setUp(self):
        self.user = _make_user()
        product = _make_product()
        self.enrollment = _make_enrollment(user=self.user, product=product)
        _advance_to_funded(self.enrollment)
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.account = self.enrollment.funded_account

        # Set balance well above min_payout_usd ($50 default)
        initial = Decimal(str(self.account.initial_balance))
        self.account.balance = initial + Decimal("500.00")
        self.account.status = TradingAccount.STATUS_ACTIVE
        self.account.save()

    def test_no_withdrawal_request_exists(self):
        """
        Gap C: no WithdrawalRequest is created automatically for funded accounts.
        There is no endpoint or task that generates one from funded_payout_eligible.
        """
        self.assertEqual(
            WithdrawalRequest.objects.filter(user=self.user).count(),
            0,
            "Gap C: no automatic WithdrawalRequest for funded payout eligibility",
        )

    def test_no_unknown_ledger_entries_in_funded_account(self):
        """
        Gap C: funded account ledger only has entries from standard event types.
        No payout-specific entries appear automatically.
        """
        known_types = {c[0] for c in LedgerEntry.EVENT_CHOICES}
        unknown_entries = LedgerEntry.objects.filter(
            account=self.account
        ).exclude(event_type__in=known_types)
        self.assertEqual(
            unknown_entries.count(), 0,
            "No unknown ledger entry types should exist",
        )

    def test_cycle_profit_is_positive_confirming_profit_gate_met(self):
        """Confirm profit gate would be met — yet no automatic action occurred."""
        cp = _funded_cycle_profit(self.account)
        self.assertGreater(cp, Decimal(str(self.fc.min_payout_usd)))


# ─────────────────────────────────────────────────────────────────────────────
# Gap B — no payout cycle reset
# ─────────────────────────────────────────────────────────────────────────────

class TestGapBNoCycleReset(TestCase):
    """
    Gap B: initial_balance is never updated after funded account creation.
    There is no 'cycle reset' mechanism after a payout.
    """

    def setUp(self):
        self.enrollment = _make_enrollment()
        _advance_to_funded(self.enrollment)
        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)

    def test_cycle_profit_accumulates_from_initial_balance(self):
        """cycle_profit = balance - initial_balance (views.py formula)."""
        initial = Decimal(str(self.account.initial_balance))
        self.account.balance = initial + Decimal("500.00")
        self.account.save()
        cp = _funded_cycle_profit(self.account)
        self.assertEqual(cp, Decimal("500.00"))

    def test_initial_balance_unchanged_after_balance_drop(self):
        """
        Gap B: if a payout reduces balance, initial_balance stays the same.
        The system has no mechanism to update it to the post-payout balance.
        """
        initial_original = Decimal(str(self.account.initial_balance))

        # Simulate earning $500, then paying out $400 from the balance
        self.account.balance = initial_original + Decimal("500.00")
        self.account.save()
        self.account.balance = self.account.balance - Decimal("400.00")  # post-payout
        self.account.save()

        self.account.refresh_from_db()
        self.assertEqual(
            Decimal(str(self.account.initial_balance)),
            initial_original,
            "Gap B: initial_balance is immutable after funded creation — no cycle reset",
        )

    def test_cycle_profit_after_simulated_payout_still_references_original_initial(self):
        """
        Gap B: after a balance deduction (simulating payout), cycle_profit is
        still calculated against the ORIGINAL initial_balance, not the post-payout level.
        In a proper cycle system, initial_balance would be updated to avoid double-counting.
        """
        initial_original = Decimal(str(self.account.initial_balance))

        # Earn $500 profit, payout $400
        self.account.balance = initial_original + Decimal("100.00")  # after payout
        self.account.save()

        # cycle_profit = 100 (100 above original initial)
        # In a cycle-reset system, it would be 0 (just paid out, fresh start at 10100)
        cp = _funded_cycle_profit(self.account)
        self.assertEqual(cp, Decimal("100.00"))

        # The remaining $100 is already above initial, so it counts toward next payout
        # This is the Gap B behavior: old gains still count
        initial_after = Decimal(str(self.account.initial_balance))
        self.assertEqual(initial_after, initial_original)


# ─────────────────────────────────────────────────────────────────────────────
# Profit split coherence
# ─────────────────────────────────────────────────────────────────────────────

class TestProfitSplitCoherence(TestCase):
    """trader_cut + broker_cut == cycle_profit for any profit_split_pct."""

    def test_split_80_20_sums_to_cycle_profit(self):
        cp = Decimal("500.00")
        tc, bc = _compute_split(cp, Decimal("80.00"))
        self.assertEqual(tc + bc, cp)

    def test_trader_cut_is_80_pct_of_500(self):
        tc, _ = _compute_split(Decimal("500.00"), Decimal("80.00"))
        self.assertEqual(tc, Decimal("400.00"))

    def test_broker_cut_is_20_pct_of_500(self):
        _, bc = _compute_split(Decimal("500.00"), Decimal("80.00"))
        self.assertEqual(bc, Decimal("100.00"))

    def test_split_70_30_sums_to_cycle_profit(self):
        cp = Decimal("300.00")
        tc, bc = _compute_split(cp, Decimal("70.00"))
        self.assertEqual(tc + bc, cp)

    def test_split_90_10_sums_to_cycle_profit(self):
        cp = Decimal("1000.00")
        tc, bc = _compute_split(cp, Decimal("90.00"))
        self.assertEqual(tc + bc, cp)

    def test_split_with_odd_amount_sums_to_cycle_profit(self):
        """Decimal rounding: tc + bc must still equal cycle_profit."""
        cp = Decimal("333.33")
        tc, bc = _compute_split(cp, Decimal("80.00"))
        self.assertEqual(tc + bc, cp)

    def test_zero_cycle_profit_gives_zero_split(self):
        cp = _ZERO
        tc, bc = _compute_split(cp, Decimal("80.00"))
        self.assertEqual(tc, _ZERO)
        self.assertEqual(bc, _ZERO)

    def test_cycle_profit_floored_at_zero_for_loss(self):
        """Negative balance vs initial → cycle_profit = 0 (floored by max(0,...))."""
        cp = max(_ZERO, Decimal("9500.00") - Decimal("10000.00"))
        self.assertEqual(cp, _ZERO)


# ─────────────────────────────────────────────────────────────────────────────
# Payout eligibility gates
# ─────────────────────────────────────────────────────────────────────────────

class TestPayoutEligibilityGates(TestCase):
    """All three gates (profit_ok, days_ok, account_ok) must all be True."""

    def setUp(self):
        self.enrollment = _make_enrollment()
        _advance_to_funded(self.enrollment)
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.account = self.enrollment.funded_account

    def _check_eligible(self, balance, trading_days, account_status):
        cycle_profit = max(
            _ZERO,
            Decimal(str(balance)) - Decimal(str(self.account.initial_balance)),
        )
        profit_ok = cycle_profit >= Decimal(str(self.fc.min_payout_usd))
        days_ok = trading_days >= self.fc.min_trading_days
        account_ok = account_status == TradingAccount.STATUS_ACTIVE
        return profit_ok and days_ok and account_ok

    def test_all_gates_met_is_eligible(self):
        # min_payout_usd=50, min_trading_days=5 (FundedConfig defaults)
        self.assertTrue(
            self._check_eligible(
                balance=Decimal("10060.00"),
                trading_days=5,
                account_status=TradingAccount.STATUS_ACTIVE,
            )
        )

    def test_profit_gate_blocks_when_below_minimum(self):
        self.assertFalse(
            self._check_eligible(
                balance=Decimal("10010.00"),  # only $10, min is $50
                trading_days=5,
                account_status=TradingAccount.STATUS_ACTIVE,
            )
        )

    def test_trading_days_gate_blocks_when_below_minimum(self):
        self.assertFalse(
            self._check_eligible(
                balance=Decimal("10100.00"),
                trading_days=3,  # 3 of 5 required
                account_status=TradingAccount.STATUS_ACTIVE,
            )
        )

    def test_account_ok_gate_blocks_when_suspended(self):
        self.assertFalse(
            self._check_eligible(
                balance=Decimal("10100.00"),
                trading_days=5,
                account_status="Suspendido",
            )
        )

    def test_profit_gate_exactly_at_minimum_is_eligible(self):
        """cycle_profit == min_payout_usd passes the profit gate."""
        min_payout = Decimal(str(self.fc.min_payout_usd))
        balance = Decimal(str(self.account.initial_balance)) + min_payout
        self.assertTrue(
            self._check_eligible(
                balance=balance,
                trading_days=self.fc.min_trading_days,
                account_status=TradingAccount.STATUS_ACTIVE,
            )
        )

    def test_funded_config_min_payout_usd_default(self):
        self.assertEqual(self.fc.min_payout_usd, Decimal("50.00"))

    def test_funded_config_min_trading_days_default(self):
        self.assertEqual(self.fc.min_trading_days, 5)

    def test_funded_config_payout_cycle_days_default(self):
        self.assertEqual(self.fc.payout_cycle_days, 14)


# ─────────────────────────────────────────────────────────────────────────────
# Gap D — daily loss filter approach in challenge_engine
# ─────────────────────────────────────────────────────────────────────────────

class TestGapDDailyLossFilterApproach(TestCase):
    """
    Gap D: challenge_engine._daily_realized_dd_pct uses created_at__date=today
    (Django ORM date filter). This differs from the explicit UTC range
    (created_at__gte / created_at__lt) established in Bloque D for consumers.py.

    With USE_TZ=True both are equivalent in practice, but they are inconsistent
    with the project's documented UTC-aware pattern.
    """

    def setUp(self):
        self.enrollment = _make_enrollment()
        self.account = self.enrollment.phase1_account

    def _add_realized_entry(self, amount, hour_utc):
        entry = LedgerEntry.objects.create(
            account=self.account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=Decimal(str(amount)),
            balance_after=self.account.balance,
            meta={},
        )
        ts = timezone.now().replace(
            hour=hour_utc, minute=0, second=0, microsecond=0
        )
        LedgerEntry.objects.filter(pk=entry.pk).update(created_at=ts)
        return entry

    def test_today_loss_is_counted_in_daily_dd(self):
        """Entries created today UTC are counted in daily drawdown."""
        from simulator.challenge_engine import _daily_realized_dd_pct

        self._add_realized_entry(-300, 12)
        dd = _daily_realized_dd_pct(self.account)
        self.assertGreater(dd, _ZERO)

    def test_positive_pnl_produces_zero_daily_dd(self):
        """Profit entries do not contribute to daily drawdown."""
        from simulator.challenge_engine import _daily_realized_dd_pct

        self._add_realized_entry(500, 10)
        dd = _daily_realized_dd_pct(self.account)
        self.assertEqual(dd, _ZERO)

    def test_daily_dd_uses_orm_date_filter_not_utc_range(self):
        """
        Gap D documentary: _daily_realized_dd_pct source uses created_at__date=
        not the explicit __gte/__lt UTC range used in consumers.py (Bloque D).
        """
        import inspect
        from simulator import challenge_engine

        source = inspect.getsource(challenge_engine._daily_realized_dd_pct)
        self.assertIn(
            "created_at__date",
            source,
            "Gap D: challenge_engine uses __date= ORM filter",
        )
        self.assertNotIn(
            "created_at__gte",
            source,
            "Gap D: explicit UTC __gte range NOT used (inconsistent with Bloque D pattern)",
        )

    def test_daily_dd_calculated_against_peak_balance(self):
        """Daily drawdown is expressed as % of peak_balance (not initial_balance)."""
        from simulator.challenge_engine import _daily_realized_dd_pct

        loss = Decimal("-500.00")
        self._add_realized_entry(float(loss), 9)

        peak = Decimal(str(self.account.peak_balance))
        expected_pct = (abs(loss) / peak * _HUNDRED).quantize(_PENNY)
        dd = _daily_realized_dd_pct(self.account)
        self.assertEqual(dd, expected_pct)


# ─────────────────────────────────────────────────────────────────────────────
# Gap E — FUNDED_SIM vs FUNDED_INTERNAL has no effect on business logic
# ─────────────────────────────────────────────────────────────────────────────

class TestGapEFundedTypeNoFlowDifference(TestCase):
    """
    Gap E: FundedConfig.funded_type (FUNDED_SIM vs FUNDED_INTERNAL) is stored
    but no production code branches on its value to change payout or risk behavior.
    """

    def _make_funded_with_type(self, funded_type):
        enrollment = _make_enrollment()
        _advance_to_funded(enrollment)
        fc = FundedConfig.objects.get(enrollment=enrollment)
        fc.funded_type = funded_type
        fc.save()
        return enrollment, fc

    def test_funded_sim_enrollment_reaches_funded_status(self):
        enrollment, fc = self._make_funded_with_type(FundedConfig.FUNDED_SIM)
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)
        self.assertEqual(fc.funded_type, FundedConfig.FUNDED_SIM)

    def test_funded_internal_enrollment_reaches_funded_status(self):
        enrollment, fc = self._make_funded_with_type(FundedConfig.FUNDED_INTERNAL)
        self.assertEqual(enrollment.status, ChallengeEnrollment.ST_FUNDED)
        self.assertEqual(fc.funded_type, FundedConfig.FUNDED_INTERNAL)

    def test_evaluate_phase_identical_for_both_types(self):
        """
        Gap E: evaluate_phase() returns same result regardless of funded_type.
        No branching on funded_type exists in evaluate_phase.
        """
        enrollment_sim, _ = self._make_funded_with_type(FundedConfig.FUNDED_SIM)
        enrollment_int, _ = self._make_funded_with_type(FundedConfig.FUNDED_INTERNAL)

        result_sim = evaluate_phase(enrollment_sim)
        result_int = evaluate_phase(enrollment_int)

        self.assertEqual(
            result_sim.status,
            result_int.status,
            "Gap E: funded_type has no effect on evaluate_phase() result",
        )

    def test_default_funded_type_is_funded_sim(self):
        """advance_to_funded() always creates FUNDED_SIM by default."""
        enrollment = _make_enrollment()
        _advance_to_funded(enrollment)
        fc = FundedConfig.objects.get(enrollment=enrollment)
        self.assertEqual(fc.funded_type, FundedConfig.FUNDED_SIM)

    def test_funded_type_choices_are_defined(self):
        choices = [c[0] for c in FundedConfig.FUNDED_TYPES]
        self.assertIn(FundedConfig.FUNDED_SIM, choices)
        self.assertIn(FundedConfig.FUNDED_INTERNAL, choices)
