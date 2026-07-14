"""
simulator/challenge_engine.py

Challenge evaluation engine — Phase 1 / Phase 2 / Funded progression.

Public API:
  activate_challenge_enrollment(enrollment)  → TradingAccount (phase 1)
  evaluate_phase(enrollment)                 → EvalResult
  advance_to_phase2(enrollment)              → TradingAccount (phase 2)
  advance_to_funded(enrollment)              → TradingAccount (funded)
  evaluate_enrollment_now(enrollment_id)     → EvalResult

Constraints:
  - No changes to consumers.py, tasks.py daemon, risk_engine.py, or execution engine.
  - All mutations are wrapped in transaction.atomic() + select_for_update().
  - All amounts are Decimal; no float arithmetic on money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from simulator.models import (
    ChallengeEnrollment,
    FundedConfig,
    LedgerEntry,
    RiskRule,
    Trade,
    TradingAccount,
)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_PENNY = Decimal("0.01")


def _commercial_snapshot_kwargs(product) -> dict:
    """SPREAD-04 — the commercial pricing snapshot every CHALLENGE/FUNDED
    TradingAccount gets frozen with at creation (Phase 1, Phase 2, and
    Funded all call this — same product, same computation, never
    duplicated). See simulator/commercial_pricing.py."""
    from .commercial_pricing import commercial_pricing_fields_from_challenge_product

    fields = commercial_pricing_fields_from_challenge_product(product)
    return {
        "commercial_profile_snapshot": fields,
        "spread_pips_snapshot": product.spread_markup_pips,
        "commission_per_lot_snapshot": product.commission_per_lot,
    }

# Public result constants
IN_PROGRESS = "IN_PROGRESS"
PASSED = "PASSED"
FAILED = "FAILED"

# TradingAccount tiers recognised by the model (25K is not a valid TradingAccount tier)
_TA_VALID_TIERS = frozenset({"10K", "50K", "100K"})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    status: str                          # IN_PROGRESS | PASSED | FAILED
    fail_reason: Optional[str] = None   # human-readable failure cause
    metrics: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ta_tier_for_product(product) -> Optional[str]:
    """Map ChallengeProduct.tier → TradingAccount.tier, or None for 25K."""
    return product.tier if product.tier in _TA_VALID_TIERS else None


def _max_dd_usd(account_size: Decimal, max_dd_pct: Decimal) -> Decimal:
    return (account_size * max_dd_pct / _HUNDRED).quantize(_PENNY)


def _trading_days(account: TradingAccount) -> int:
    """Count distinct calendar days with at least one closed trade on *account*."""
    return (
        Trade.objects.filter(account=account, closed_at__isnull=False)
        .dates("closed_at", "day")
        .count()
    )


def _realized_dd_pct(account: TradingAccount) -> Decimal:
    """Peak-to-balance realized drawdown as a percentage."""
    peak = Decimal(str(account.peak_balance or account.balance or 1))
    bal = Decimal(str(account.balance or 0))
    if peak <= _ZERO:
        return _ZERO
    return max(_ZERO, (peak - bal) / peak * _HUNDRED)


def _daily_realized_dd_pct(account: TradingAccount) -> Decimal:
    """Today's realized PnL expressed as a drawdown percentage of peak_balance."""
    today = timezone.now().date()
    today_pnl = LedgerEntry.objects.filter(
        account=account,
        event_type=LedgerEntry.EV_REALIZED,
        created_at__date=today,
    ).aggregate(t=Sum("amount"))["t"] or _ZERO

    today_pnl = Decimal(str(today_pnl))
    if today_pnl >= _ZERO:
        return _ZERO
    peak = Decimal(str(account.peak_balance or account.balance or 1))
    if peak <= _ZERO:
        return _ZERO
    return (abs(today_pnl) / peak * _HUNDRED).quantize(_PENNY)


def _days_elapsed(account: TradingAccount) -> int:
    return (timezone.now().date() - account.created_at.date()).days


def _profit_pct(account: TradingAccount) -> Decimal:
    """Progress toward profit target as a percentage (0–100+)."""
    pt = account.profit_target
    if pt is None or pt <= _ZERO:
        return _ZERO
    initial = Decimal(str(account.initial_balance or account.balance))
    gained = Decimal(str(account.balance)) - initial
    return (gained / Decimal(str(pt)) * _HUNDRED).quantize(_PENNY)


# ---------------------------------------------------------------------------
# Phase rule lookup
# ---------------------------------------------------------------------------

def _phase_rules(enrollment: ChallengeEnrollment) -> dict:
    """Return the active phase's rule parameters from the product."""
    product = enrollment.product
    if enrollment.status == ChallengeEnrollment.ST_PHASE_1:
        return {
            "profit_target_pct": product.p1_profit_target_pct,
            "max_drawdown_pct":   product.p1_max_drawdown_pct,
            "max_daily_loss_pct": product.p1_max_daily_loss_pct,
            "min_trading_days":   product.p1_min_trading_days,
            "max_duration_days":  product.p1_max_duration_days,
        }
    if enrollment.status == ChallengeEnrollment.ST_PHASE_2:
        return {
            "profit_target_pct": product.p2_profit_target_pct,
            "max_drawdown_pct":   product.p2_max_drawdown_pct,
            "max_daily_loss_pct": product.p2_max_daily_loss_pct,
            "min_trading_days":   product.p2_min_trading_days,
            "max_duration_days":  product.p2_max_duration_days,
        }
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def activate_challenge_enrollment(enrollment: ChallengeEnrollment) -> TradingAccount:
    """
    Create the Phase 1 TradingAccount + RiskRule for *enrollment*.

    Idempotent: if phase1_account already exists, returns it without side-effects.
    Returns the phase1_account.
    """
    with transaction.atomic():
        enrollment = (
            ChallengeEnrollment.objects
            .select_for_update()
            .select_related("product", "phase1_account")
            .get(pk=enrollment.pk)
        )

        if enrollment.phase1_account_id is not None:
            return enrollment.phase1_account

        product = enrollment.product
        size = Decimal(str(product.account_size))
        profit_target_usd = product.p1_profit_target_amount()
        max_dd_abs = _max_dd_usd(size, product.p1_max_drawdown_pct)

        account = TradingAccount.objects.create(
            user=enrollment.user,
            account_type="CHALLENGE",
            tier=_ta_tier_for_product(product),
            phase="Fase 1",
            initial_balance=size,
            profit_target=profit_target_usd,
            max_drawdown=max_dd_abs,
            leverage=50,
            status=TradingAccount.STATUS_ACTIVE,
            **_commercial_snapshot_kwargs(product),
        )

        RiskRule.objects.create(
            account=account,
            max_drawdown_pct=product.p1_max_drawdown_pct,
            max_daily_loss_pct=product.p1_max_daily_loss_pct,
            max_lot_size=product.max_lot_size,
            max_open_positions=product.max_open_positions,
            max_exposure_usd=size,
        )

        enrollment.phase1_account = account
        enrollment.status = ChallengeEnrollment.ST_PHASE_1
        enrollment.save(update_fields=["phase1_account", "status"])

        return account


def evaluate_phase(enrollment: ChallengeEnrollment) -> EvalResult:
    """
    Evaluate the trader's current phase against product rules.

    Returns EvalResult(status=IN_PROGRESS|PASSED|FAILED, fail_reason, metrics).
    Does NOT create accounts or advance the enrollment — callers do that.
    """
    account = enrollment.active_account
    if account is None:
        return EvalResult(status=FAILED, fail_reason="No active account for this phase")

    account.refresh_from_db()
    rules = _phase_rules(enrollment)
    if not rules:
        return EvalResult(status=FAILED, fail_reason="Enrollment is not in an evaluable phase")

    product = enrollment.product
    size = Decimal(str(product.account_size))

    # ── Compute metrics ────────────────────────────────────────────────────
    realized_dd = _realized_dd_pct(account)
    daily_dd = _daily_realized_dd_pct(account)
    trading_days = _trading_days(account)
    days_elapsed = _days_elapsed(account)
    p_pct = _profit_pct(account)

    metrics = {
        "profit_pct":          float(p_pct),
        "realized_dd_pct":     float(realized_dd),
        "daily_realized_dd_pct": float(daily_dd),
        "trading_days":        trading_days,
        "days_elapsed":        days_elapsed,
        "balance":             float(account.balance),
        "initial_balance":     float(account.initial_balance or size),
        "profit_target_usd":   float(account.profit_target or 0),
    }

    # ── Failure checks (order matters: most critical first) ─────────────
    max_dd_limit = Decimal(str(rules["max_drawdown_pct"]))
    if realized_dd >= max_dd_limit:
        return EvalResult(
            status=FAILED,
            fail_reason=f"Max drawdown exceeded: {float(realized_dd):.2f}% >= {float(max_dd_limit):.2f}%",
            metrics=metrics,
        )

    max_daily_limit = Decimal(str(rules["max_daily_loss_pct"]))
    if daily_dd >= max_daily_limit:
        return EvalResult(
            status=FAILED,
            fail_reason=f"Daily loss exceeded: {float(daily_dd):.2f}% >= {float(max_daily_limit):.2f}%",
            metrics=metrics,
        )

    max_duration = rules["max_duration_days"]
    if days_elapsed > max_duration:
        return EvalResult(
            status=FAILED,
            fail_reason=f"Max duration exceeded: {days_elapsed} days > {max_duration} days",
            metrics=metrics,
        )

    # ── Pass check ────────────────────────────────────────────────────────
    min_trading_days = rules["min_trading_days"]
    profit_target_pct = Decimal(str(rules["profit_target_pct"]))

    if p_pct >= profit_target_pct and trading_days >= min_trading_days:
        return EvalResult(status=PASSED, metrics=metrics)

    return EvalResult(status=IN_PROGRESS, metrics=metrics)


def advance_to_phase2(enrollment: ChallengeEnrollment) -> TradingAccount:
    """
    Create the Phase 2 TradingAccount + RiskRule. Mark Phase 1 account as Completado.

    Idempotent: if phase2_account already exists, returns it.
    Raises ValueError if enrollment is not in PHASE_1 status (unless phase2 already exists).
    """
    with transaction.atomic():
        enrollment = (
            ChallengeEnrollment.objects
            .select_for_update()
            .select_related("product", "phase1_account", "phase2_account")
            .get(pk=enrollment.pk)
        )

        if enrollment.phase2_account_id is not None:
            return enrollment.phase2_account

        if enrollment.status != ChallengeEnrollment.ST_PHASE_1:
            raise ValueError(
                f"Cannot advance to Phase 2: enrollment #{enrollment.pk} is {enrollment.status!r}"
            )

        product = enrollment.product
        size = Decimal(str(product.account_size))
        profit_target_usd = product.p2_profit_target_amount()
        max_dd_abs = _max_dd_usd(size, product.p2_max_drawdown_pct)

        account = TradingAccount.objects.create(
            user=enrollment.user,
            account_type="CHALLENGE",
            tier=_ta_tier_for_product(product),
            phase="Fase 2",
            initial_balance=size,
            profit_target=profit_target_usd,
            max_drawdown=max_dd_abs,
            leverage=50,
            status=TradingAccount.STATUS_ACTIVE,
            **_commercial_snapshot_kwargs(product),
        )

        RiskRule.objects.create(
            account=account,
            max_drawdown_pct=product.p2_max_drawdown_pct,
            max_daily_loss_pct=product.p2_max_daily_loss_pct,
            max_lot_size=product.max_lot_size,
            max_open_positions=product.max_open_positions,
            max_exposure_usd=size,
        )

        # Mark Phase 1 account as completed
        if enrollment.phase1_account_id is not None:
            TradingAccount.objects.filter(pk=enrollment.phase1_account_id).update(
                status=TradingAccount.STATUS_FUNDED  # "Completado"
            )

        enrollment.phase2_account = account
        enrollment.status = ChallengeEnrollment.ST_PHASE_2
        enrollment.phase1_passed_at = timezone.now()
        enrollment.save(update_fields=["phase2_account", "status", "phase1_passed_at"])

        return account


def advance_to_funded(enrollment: ChallengeEnrollment) -> TradingAccount:
    """
    Create the Funded TradingAccount + RiskRule + FundedConfig. Mark Phase 2 as Completado.

    Idempotent: if funded_account already exists, returns it.
    Raises ValueError if enrollment is not in PHASE_2 status (unless funded already exists).
    """
    with transaction.atomic():
        enrollment = (
            ChallengeEnrollment.objects
            .select_for_update()
            .select_related("product", "phase2_account", "funded_account")
            .get(pk=enrollment.pk)
        )

        if enrollment.funded_account_id is not None:
            return enrollment.funded_account

        if enrollment.status != ChallengeEnrollment.ST_PHASE_2:
            raise ValueError(
                f"Cannot advance to Funded: enrollment #{enrollment.pk} is {enrollment.status!r}"
            )

        product = enrollment.product
        size = Decimal(str(product.account_size))
        max_dd_abs = _max_dd_usd(size, product.p2_max_drawdown_pct)

        account = TradingAccount.objects.create(
            user=enrollment.user,
            account_type="FUNDED",
            tier=_ta_tier_for_product(product),
            phase="Funded",
            initial_balance=size,
            max_drawdown=max_dd_abs,
            leverage=50,
            status=TradingAccount.STATUS_ACTIVE,
            **_commercial_snapshot_kwargs(product),
        )

        RiskRule.objects.create(
            account=account,
            max_drawdown_pct=product.p2_max_drawdown_pct,
            max_daily_loss_pct=product.p2_max_daily_loss_pct,
            max_lot_size=product.max_lot_size,
            max_open_positions=product.max_open_positions,
            max_exposure_usd=size,
        )

        FundedConfig.objects.create(
            enrollment=enrollment,
            funded_type=FundedConfig.FUNDED_SIM,
            profit_split_pct=product.profit_split_pct,
        )

        # Mark Phase 2 account as completed
        if enrollment.phase2_account_id is not None:
            TradingAccount.objects.filter(pk=enrollment.phase2_account_id).update(
                status=TradingAccount.STATUS_FUNDED  # "Completado"
            )

        enrollment.funded_account = account
        enrollment.status = ChallengeEnrollment.ST_FUNDED
        enrollment.phase2_passed_at = timezone.now()
        enrollment.funded_at = timezone.now()
        enrollment.save(update_fields=[
            "funded_account", "status", "phase2_passed_at", "funded_at"
        ])

        return account


def _mark_failed(enrollment: ChallengeEnrollment, reason: str, failed_at: str) -> None:
    """Suspend the active account and mark enrollment as FAILED."""
    account = enrollment.active_account
    if account is not None:
        TradingAccount.objects.filter(pk=account.pk).update(
            status=TradingAccount.STATUS_SUSPENDED
        )
    enrollment.status = ChallengeEnrollment.ST_FAILED
    enrollment.failed_at_phase = failed_at
    enrollment.failure_reason = reason[:256]
    enrollment.save(update_fields=["status", "failed_at_phase", "failure_reason"])


def evaluate_enrollment_now(enrollment_id: int) -> EvalResult:
    """
    Manual trigger: evaluate the enrollment and advance or fail it.

    - Phase 1 PASSED  → advance_to_phase2
    - Phase 2 PASSED  → advance_to_funded
    - FAILED          → suspend active account, mark enrollment failed
    - IN_PROGRESS     → no change

    Returns the EvalResult from evaluate_phase.
    """
    with transaction.atomic():
        enrollment = (
            ChallengeEnrollment.objects
            .select_for_update()
            .select_related("product", "phase1_account", "phase2_account", "funded_account")
            .get(pk=enrollment_id)
        )

        if enrollment.status not in (
            ChallengeEnrollment.ST_PHASE_1,
            ChallengeEnrollment.ST_PHASE_2,
        ):
            return EvalResult(
                status=IN_PROGRESS,
                fail_reason=f"Enrollment is {enrollment.status!r} — nothing to evaluate",
            )

        result = evaluate_phase(enrollment)

        if result.status == PASSED:
            if enrollment.status == ChallengeEnrollment.ST_PHASE_1:
                advance_to_phase2(enrollment)
            elif enrollment.status == ChallengeEnrollment.ST_PHASE_2:
                advance_to_funded(enrollment)

        elif result.status == FAILED:
            failed_at = (
                ChallengeEnrollment.FAILED_AT_PHASE_1
                if enrollment.status == ChallengeEnrollment.ST_PHASE_1
                else ChallengeEnrollment.FAILED_AT_PHASE_2
            )
            _mark_failed(enrollment, result.fail_reason or "Evaluation failed", failed_at)

        return result
