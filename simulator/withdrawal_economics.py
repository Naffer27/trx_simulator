# simulator/withdrawal_economics.py
"""
simulator/withdrawal_economics.py — WITHDRAWAL-ECONOMICS-01 (FASE B).

CORE SERVICE for BrokerLedger.REV_WITHDRAW_FEE, and for the commercial
fee calculation applied to every new WithdrawalRequest. Two
responsibilities, both here because they are two sides of the same
fact — the fee resolved at request time is exactly the fee the writer
later books at completion time:

  1. calculate_withdrawal_fee(gross_amount) — resolves the CURRENT
     WithdrawalFeeConfig and returns (fee_rate, fee_amount, net_amount)
     for that gross amount, Decimal-only, rounded per the certified
     ROUND_HALF_EVEN convention (ib_commission.py::_money_round, same
     rule, duplicated here rather than imported — this module stays as
     isolated as challenge_revenue.py/broker_economic_adjustment.py are
     from each other, importing no other economics-engine module).
     Called ONCE, inside the same atomic block that creates a
     WithdrawalRequest (views.py::withdraw_otp_verify_view._finalize()),
     and the result is snapshotted onto that row's fee_rate/fee_amount/
     net_amount — never recomputed afterward. Changing
     WithdrawalFeeConfig.percent later has zero effect on any
     already-created WithdrawalRequest.

  2. record_withdrawal_fee_revenue(wr) — the one and only sanctioned
     entry point that may create a REV_WITHDRAW_FEE row. Records a real
     economic event that has ALREADY occurred — a WithdrawalRequest
     that has genuinely reached COMPLETED (crypto verifiably left the
     broker to the client). This function does not verify completion
     and does not decide whether a withdrawal completed — it trusts its
     caller entirely, exactly as record_challenge_fee_revenue() and
     create_broker_economic_adjustment() trust theirs. Call it ONLY
     from the single certified call site — payout_orchestrator.py's
     _apply_result_without_refund(), at the moment PayoutAttempt
     transitions to COMPLETED — inside that same locked transaction.

Legacy compatibility (CRITICAL, per FASE A §Legacy and the Owner's
explicit FASE B instruction): a WithdrawalRequest with fee_amount=None
never had a fee policy evaluated for it (it predates this block, or
predates WithdrawalFeeConfig existing at all). record_withdrawal_fee_
revenue() treats that as "nothing to book" and returns None WITHOUT
creating any row — it never reinterprets a historical withdrawal as if
it had been charged today's rate. A WithdrawalRequest with
fee_amount=Decimal("0.00") (the fee was explicitly disabled at
snapshot time) is likewise a no-op — an explicit zero fee earns
nothing, and this module never writes a $0.00 BrokerLedger row. Amount
booked, when it is booked, is fee_amount exactly — GROSS fee revenue,
never "net withdrawal profit": no provider/network cost is ever
subtracted here, because none is captured anywhere in this codebase
(FASE A §I) — that limitation is surfaced honestly by
broker_economics_summary.py's Retained Economics section, not papered
over by this module inventing a cost.

Idempotency: primarily DB-enforced via BrokerLedger's own
UniqueConstraint(source_withdrawal, revenue_type) (migration
0090_withdrawal_economics_01) — at most one REV_WITHDRAW_FEE row can
ever exist per WithdrawalRequest, regardless of retries, duplicate
webhooks, or concurrent completion. This function raises a typed
exception (DuplicateWithdrawalFeeRevenue) when that constraint is hit,
mirroring record_challenge_fee_revenue()'s DuplicateChallengeRevenue
exactly. It does not use exists()/get_or_create() as its primary
protection for the same reason that module doesn't: under a true
concurrent race, only a DB-level unique constraint is authoritative.
This is an INDEPENDENT layer on top of the pre-existing
PayoutAttempt.TERMINAL_STATUSES idempotency guard already in
_apply_result_without_refund() — not a replacement for it.

Transactional integrity: the write happens inside its own
transaction.atomic() block, which — per Django's re-entrant atomic()
semantics — becomes a SAVEPOINT (not a new independent transaction)
when called from inside an already-open outer atomic() block, exactly
as challenge_revenue.py/wallet_ledger.py already document for
themselves. The certified call site opens its own outer atomic()
before calling this function, so if any later step in that same
transaction fails and the whole thing rolls back, the REV_WITHDRAW_FEE
row rolls back with it — it can never be left orphaned.

Isolation, by construction, not by policy: this module imports nothing
from wallet_ledger, ib_commission, ib_commission_triggers,
ib_treasury_settlement, challenge_revenue, or any Treasury/trading-
engine module. Calling either function here can only ever read
WithdrawalFeeConfig/write BrokerLedger — never a Wallet, TradingAccount,
IBCommissionObligation, or TreasuryOperationRequest row. The Wallet
debit for a withdrawal is, and remains, the FULL gross amount_usd —
this module never creates a second, fee-specific WalletTransaction;
the fee is a portion of the pot already debited, not an additional
deduction (Owner Decision, FASE B authorization).
"""
from decimal import ROUND_HALF_EVEN, Decimal

from django.db import IntegrityError, transaction

from .models import BrokerLedger, WithdrawalFeeConfig, WithdrawalRequest

_CENTS = Decimal("0.01")


class DuplicateWithdrawalFeeRevenue(Exception):
    """Raised when a REV_WITHDRAW_FEE row already exists for this WithdrawalRequest.

    Signals that BrokerLedger's DB-level UniqueConstraint
    (source_withdrawal, revenue_type) rejected a second write — a
    retry, a duplicate webhook delivery, or a genuine concurrent race
    against this exact WithdrawalRequest's completion. Never silently
    swallowed by this module; the caller decides how to react
    (typically: log at warning level and continue — the revenue is
    already correctly booked).
    """


def _money_round(value: Decimal) -> Decimal:
    """ROUND_HALF_EVEN, quantized to the cent — the certified codebase-wide
    convention (ib_commission.py::_money_round, same rule)."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_EVEN)


def calculate_withdrawal_fee(gross_amount: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """
    Resolve the CURRENT WithdrawalFeeConfig and compute
    (fee_rate, fee_amount, net_amount) for *gross_amount*.

    fee_amount = money_round(gross_amount * fee_rate / 100)
    net_amount = gross_amount - fee_amount

    When the config is disabled, fee_rate=Decimal("0.00") explicitly
    (an evaluated, snapshotted zero — never "no snapshot"), fee_amount
    is 0.00, and net_amount equals gross_amount exactly.

    Decimal-only. Never reads or writes anything but WithdrawalFeeConfig
    (a get_or_create() read, never a mutation).
    """
    gross_amount = Decimal(gross_amount)
    config = WithdrawalFeeConfig.get_current()

    if not config.enabled:
        fee_rate = Decimal("0.00")
        fee_amount = Decimal("0.00")
    else:
        fee_rate = config.percent
        fee_amount = _money_round(gross_amount * fee_rate / Decimal("100"))

    net_amount = gross_amount - fee_amount
    return fee_rate, fee_amount, net_amount


def record_withdrawal_fee_revenue(wr: WithdrawalRequest) -> "BrokerLedger | None":
    """
    Record GROSS withdrawal fee revenue for one already-COMPLETED
    WithdrawalRequest.

    Must be called inside the same transaction.atomic() block that just
    transitioned *wr* to COMPLETED, from payout_orchestrator.py's
    _apply_result_without_refund() only.

    Returns None (no row created, not an error) when wr.fee_amount is
    None (legacy — no fee policy was ever evaluated for this request)
    or Decimal("0.00") (fee was explicitly disabled at snapshot time) —
    both are "nothing to book", never treated as a defect.

    Raises DuplicateWithdrawalFeeRevenue if this WithdrawalRequest
    already has a REV_WITHDRAW_FEE row — safe under concurrency, backed
    by a DB unique constraint, not an application-level check.
    """
    if wr.pk is None:
        raise ValueError(
            "record_withdrawal_fee_revenue: WithdrawalRequest must already be saved (pk is None)"
        )

    if wr.fee_amount is None or wr.fee_amount == Decimal("0.00"):
        return None

    try:
        with transaction.atomic():
            return BrokerLedger.objects.create(
                revenue_type=BrokerLedger.REV_WITHDRAW_FEE,
                amount=wr.fee_amount,
                source_withdrawal=wr,
                meta={
                    "withdrawal_id": wr.pk,
                    "user_id": wr.user_id,
                    "gross_amount": str(wr.amount_usd),
                    "fee_rate": str(wr.fee_rate),
                    "net_amount": str(wr.net_amount),
                },
            )
    except IntegrityError as exc:
        raise DuplicateWithdrawalFeeRevenue(
            f"REV_WITHDRAW_FEE already recorded for withdrawal #{wr.pk}"
        ) from exc
