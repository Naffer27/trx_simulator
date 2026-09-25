# simulator/funded_economics.py
"""
simulator/funded_economics.py — BROKER-ECONOMICS-04B (FASE B).

CORE SERVICE for BrokerLedger.REV_FUNDED_PROFIT_SHARE. The one and only
sanctioned entry point that may create a REV_FUNDED_PROFIT_SHARE row.

── The double-counting proof (FASE A, re-stated here as the load-bearing
   invariant this module exists to preserve) ─────────────────────────────
Every closed Trade on a funded account already produces a real
BrokerLedger.REV_COUNTERPARTY_PNL row (broker_ledger.py::
create_broker_counterparty_entry(), called unconditionally for every
Trade regardless of account_type — confirmed by direct read, no
account_type filter exists anywhere in that call chain). A funded
account's cycle_profit is exactly the sum of realized_pnl across those
same trades, so REV_COUNTERPARTY_PNL already records -cycle_profit as
a broker loss for that trading activity.

At payout, only trader_cut ever leaves the firm (a real Wallet credit
for FUNDED_SIM, a real NowPayments payout for FUNDED_INTERNAL).
broker_cut never leaves — it is absorbed permanently into the funded
account's own balance the instant TradingAccount.initial_balance is
reset (immediately, inside approve_sim_payout()'s single atomic step
for FUNDED_SIM; only inside handle_internal_payout_webhook()'s
NowPayments-confirmed COMPLETED branch for FUNDED_INTERNAL). That reset
is the code's own pre-existing definition of "this retained amount is
now permanently the firm's, never subject to being re-split with the
trader in a future cycle."

Recognizing broker_cut as REV_FUNDED_PROFIT_SHARE at that exact moment
is therefore NOT double-counting: REV_COUNTERPARTY_PNL recorded the
full -cycle_profit as if the entire trading result belonged to the
trader (which, at the moment of trade closure, it structurally did —
the profit-share split had not executed yet); REV_FUNDED_PROFIT_SHARE
is a separate, subsequent economic fact — the firm's contractual
reclaim of broker_cut of that trader-side claim, exactly the same
relationship trading commission (REV_COMMISSION) has to an IB's cut of
it. Combined: -cycle_profit + broker_cut = -(cycle_profit - broker_cut)
= -trader_cut, the broker's true net economic outflow for the cycle.
Worked example: cycle_profit=$10,000, trader_cut=$8,000 (80% split),
broker_cut=$2,000 -> REV_COUNTERPARTY_PNL=-$10,000 (already booked,
untouched) + REV_FUNDED_PROFIT_SHARE=+$2,000 (this module) = -$8,000
net, the broker's real cash outflow. The original REV_COUNTERPARTY_PNL
row is NEVER modified, netted, or replaced by this module — these are
two distinct rows recording two distinct facts.

── Scope: both funded types, kept distinguishable ───────────────────────
FUNDED_SIM (simulated firm capital, per FundedConfig's own docstring)
and FUNDED_INTERNAL (real firm-allocated capital) are BOTH in scope —
Owner-approved FASE B decision. This module never erases that
distinction: fpr.funded_type is always recorded verbatim in the
created row's meta, and every caller/test can tell the two apart from
the row alone, without needing to re-join FundedPayoutRequest.

── Recognition event ─────────────────────────────────────────────────────
Call this ONLY when a FundedPayoutRequest has genuinely reached
ST_COMPLETED — the same instant the code itself resets
TradingAccount.initial_balance:
  - FUNDED_SIM: inside funded_payouts.py::approve_sim_payout()'s single
    atomic block, immediately after the Wallet credit succeeds and the
    cycle reset is applied.
  - FUNDED_INTERNAL: inside funded_payouts.py::
    handle_internal_payout_webhook()'s NowPayments-confirmed COMPLETED
    branch only — never at Phase 1 (APPROVED), which remains fully
    reversible.
Never at PENDING, APPROVED, REJECTED, FAILED, or CANCELLED — none of
those states finalize broker_cut (see the state matrix in the FASE A
report).

The amount booked is read EXCLUSIVELY from the immutable snapshot
FundedPayoutRequest.broker_cut — never recomputed from the current
FundedConfig.profit_split_pct or the account's current balance. A
later change to the profit-split policy has zero retroactive effect on
any already-completed payout's recognized amount.

── Idempotency ────────────────────────────────────────────────────────────
Primarily DB-enforced via BrokerLedger's own
UniqueConstraint(source_funded_payout, revenue_type) (migration
0091_funded_broker_cut_04b) — at most one REV_FUNDED_PROFIT_SHARE row
can ever exist per FundedPayoutRequest, regardless of retries,
duplicate webhooks, or concurrent completion. This is an INDEPENDENT
second layer on top of the pre-existing status/lock protection already
in both approve_sim_payout() and handle_internal_payout_webhook()
(select_for_update() + terminal-state re-check) — not a replacement
for it. This function raises a typed exception
(DuplicateFundedProfitShareRevenue) when the constraint is hit, never
using exists()/get_or_create() as the primary guard.

── Forward-only ───────────────────────────────────────────────────────────
No sweep, no signal, no backfill script exists anywhere that scans
historical COMPLETED FundedPayoutRequest rows. This module has exactly
one way to run: a direct call from the two certified call sites above,
on a row that just became COMPLETED. Any historical reconciliation is
a separate, not-yet-authorized future Owner decision.

── Isolation, by construction ─────────────────────────────────────────────
This module imports nothing from wallet_ledger, ib_commission,
ib_commission_triggers, ib_treasury_settlement, challenge_revenue,
withdrawal_economics, or any Treasury/trading-engine module. Calling
record_funded_broker_cut_revenue() can only ever write a single
BrokerLedger row — never a Wallet, TradingAccount, IBCommissionObligation,
or TreasuryOperationRequest row. trader_cut's own movement (Wallet
credit or NowPayments payout) is, and remains, a capital flow / profit
distribution — this module never touches it and never re-books it.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction

from .models import BrokerLedger, FundedPayoutRequest


class DuplicateFundedProfitShareRevenue(Exception):
    """Raised when a REV_FUNDED_PROFIT_SHARE row already exists for this
    FundedPayoutRequest.

    Signals that BrokerLedger's DB-level UniqueConstraint
    (source_funded_payout, revenue_type) rejected a second write — a
    retry, a duplicate webhook delivery, or a genuine concurrent race
    against this exact FundedPayoutRequest's completion. Never silently
    swallowed by this module; the caller decides how to react
    (typically: log at warning level and continue — the revenue is
    already correctly booked).
    """


def record_funded_broker_cut_revenue(fpr: FundedPayoutRequest) -> "BrokerLedger | None":
    """
    Record GROSS funded profit-share revenue for one FundedPayoutRequest
    that has already reached ST_COMPLETED.

    Must be called inside the same transaction.atomic() block that just
    transitioned *fpr* to COMPLETED (and reset the funded account's
    initial_balance), from one of the two certified call sites in
    funded_payouts.py only.

    Returns None (no row created, not an error) when fpr.broker_cut is
    Decimal("0.00") — a legitimate zero-share cycle earns nothing to
    book, never a defect.

    Raises DuplicateFundedProfitShareRevenue if this FundedPayoutRequest
    already has a REV_FUNDED_PROFIT_SHARE row — safe under concurrency,
    backed by a DB unique constraint, not an application-level check.
    """
    if fpr.pk is None:
        raise ValueError(
            "record_funded_broker_cut_revenue: FundedPayoutRequest must already be saved (pk is None)"
        )

    if fpr.broker_cut == Decimal("0.00"):
        return None

    try:
        with transaction.atomic():
            return BrokerLedger.objects.create(
                revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE,
                amount=fpr.broker_cut,
                source_account=fpr.funded_account,
                source_funded_payout=fpr,
                meta={
                    "funded_payout_request_id": fpr.pk,
                    "funded_type": fpr.funded_type,
                    "user_id": fpr.user_id,
                    "cycle_profit": str(fpr.cycle_profit),
                    "trader_cut": str(fpr.trader_cut),
                    "profit_split_pct": str(fpr.profit_split_pct),
                },
            )
    except IntegrityError as exc:
        raise DuplicateFundedProfitShareRevenue(
            f"REV_FUNDED_PROFIT_SHARE already recorded for funded payout #{fpr.pk}"
        ) from exc
