# simulator/provider_cost_economics.py
"""
simulator/provider_cost_economics.py — BROKER-ECONOMICS-04C.4.

CORE SERVICE for BrokerLedger.REV_PROVIDER_COST. The one and only
sanctioned entry point that may create a REV_PROVIDER_COST row.

── The booking gate (single, non-bypassable) ────────────────────────────
A ProviderCostRecord may economically post ONLY when:
    quality == ACTUAL
    AND is_final == True
    AND usd_value IS NOT NULL
ESTIMATED never posts. UNKNOWN never posts. A non-final ACTUAL never
posts. An ACTUAL, final cost with no authoritative USD valuation never
posts, regardless of how confident the original-currency amount is.
This gate is enforced in exactly this one function — no caller may
bypass or partially reproduce it.

usd_value == Decimal("0.00") is a legitimate, confirmed ACTUAL zero,
fully distinct from UNKNOWN — but, matching challenge_revenue.py's and
funded_economics.py's own "never write a $0.00 BrokerLedger row"
convention, it produces no ledger entry on its own. The one exception is
a correction whose DELTA against an already-posted original is non-zero
even though the correction's own usd_value is exactly 0.00 (e.g. a
provider that reported $5 and later corrects to $0 — a real $5 credit
back must still post) — see the correction logic below.

── Sign convention ───────────────────────────────────────────────────────
amount = -record.usd_value. Matches broker_pnl.py's own locked rule:
positive means the broker gains, negative means the broker loses. A cost
is always a broker economic outflow.

── Corrections — append-only, delta-only ────────────────────────────────
ProviderCostRecord rows are immutable and a correction is always a NEW
row (record.corrects pointing at the one it supersedes — see
provider_cost_adapters.py/provider_cost_inbox.py). This function never
mutates or deletes the ORIGINAL BrokerLedger row. If the corrected
record was already posted, only the DELTA (new signed amount minus the
original posted amount) is booked, as its own new REV_PROVIDER_COST row
linked to the NEW ProviderCostRecord (never re-linked to the original).
If the corrected record was never posted (it was ESTIMATED/UNKNOWN/
non-final at the time), this correction is effectively the first real
posting and the ordinary zero-skip rule applies to it directly.

── Idempotency ────────────────────────────────────────────────────────────
Primarily DB-enforced via BrokerLedger's own
UniqueConstraint(source_provider_cost, revenue_type) — at most one
REV_PROVIDER_COST row can ever exist per ProviderCostRecord, regardless
of retries or concurrent calls against the exact same record. This
function raises a typed exception (DuplicateProviderCostRevenue) when
the constraint is hit, never using exists()/get_or_create() as the
primary guard — same pattern challenge_revenue.py/withdrawal_economics.py/
funded_economics.py already use. A correction's own NEW ProviderCostRecord
has a different pk, so this constraint never blocks a legitimate
correction's own post.

── Economic separation ───────────────────────────────────────────────────
Never reinterprets client principal as expense (reads only
ProviderCostRecord.usd_value — never Deposit.amount_usd/
WithdrawalRequest.amount_usd). Never nets against REV_WITHDRAW_FEE/
REV_CHALLENGE_FEE/REV_FUNDED_PROFIT_SHARE/REV_COMMISSION/REV_SPREAD —
REV_PROVIDER_COST is its own, separately auditable revenue_type, summed
independently in broker_pnl.py.

── Isolation, by construction ─────────────────────────────────────────────
This module imports nothing from wallet_ledger, ib_commission,
ib_commission_triggers, ib_treasury_settlement, challenge_revenue,
withdrawal_economics, funded_economics, payout_orchestrator,
payout_providers, or any Treasury/trading-engine module. Calling
record_provider_cost_revenue() can only ever write a single BrokerLedger
row — never a Wallet, TradingAccount, Deposit, WithdrawalRequest, or
IBCommissionObligation row.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction

from .models import BrokerLedger, ProviderCostRecord

_ZERO = Decimal("0.00")


class DuplicateProviderCostRevenue(Exception):
    """Raised when a REV_PROVIDER_COST row already exists for this
    ProviderCostRecord.

    Signals that BrokerLedger's DB-level UniqueConstraint
    (source_provider_cost, revenue_type) rejected a second write — a
    retry or a genuine concurrent race against this exact
    ProviderCostRecord. Never silently swallowed by this module; the
    caller decides how to react (typically: log at warning level and
    continue — the cost is already correctly booked).
    """


def record_provider_cost_revenue(record: ProviderCostRecord) -> "BrokerLedger | None":
    """
    Record a NEGATIVE-signed REV_PROVIDER_COST BrokerLedger entry for one
    ProviderCostRecord, if — and only if — it passes the booking gate.

    Returns None (no row created, not an error) when:
      - quality != ACTUAL
      - is_final is False
      - usd_value is None
      - the resulting amount to post is exactly Decimal("0.00")
        (a legitimate confirmed zero, or a correction whose delta against
        an already-posted original nets to zero)

    Raises DuplicateProviderCostRevenue if this ProviderCostRecord
    already has a REV_PROVIDER_COST row — safe under concurrency, backed
    by a DB unique constraint, not an application-level check.
    """
    if record.pk is None:
        raise ValueError(
            "record_provider_cost_revenue: ProviderCostRecord must already be saved (pk is None)"
        )

    if record.quality != ProviderCostRecord.QUALITY_ACTUAL:
        return None
    if not record.is_final:
        return None
    if record.usd_value is None:
        return None

    new_signed_amount = -record.usd_value

    meta = {
        "provider_cost_record_id": record.pk,
        "provider": record.provider,
        "operation_type": record.operation_type,
        "cost_type": record.cost_type,
        "provider_reference": record.provider_reference,
        "original_amount": str(record.amount),
        "original_currency": record.currency,
        "occurred_at": record.occurred_at.isoformat(),
    }

    amount_to_post = new_signed_amount

    if record.corrects_id is not None:
        original_ledger_row = BrokerLedger.objects.filter(
            source_provider_cost_id=record.corrects_id,
            revenue_type=BrokerLedger.REV_PROVIDER_COST,
        ).first()
        if original_ledger_row is not None:
            # The original was already booked — post only the DELTA
            # required to reconcile to the new, corrected value. The
            # original ProviderCostRecord and its BrokerLedger row are
            # never touched.
            delta = new_signed_amount - original_ledger_row.amount
            if delta == _ZERO:
                return None
            amount_to_post = delta
            meta["corrects_provider_cost_record_id"] = record.corrects_id
            meta["correction_delta"] = True
        else:
            # The original was never posted (it was ESTIMATED/UNKNOWN/
            # non-final at the time) — this correction is effectively the
            # first real posting for this cost. Ordinary zero-skip rule
            # applies directly to its own value.
            if new_signed_amount == _ZERO:
                return None
            amount_to_post = new_signed_amount
    else:
        if new_signed_amount == _ZERO:
            return None
        amount_to_post = new_signed_amount

    try:
        with transaction.atomic():
            return BrokerLedger.objects.create(
                revenue_type=BrokerLedger.REV_PROVIDER_COST,
                amount=amount_to_post,
                source_provider_cost=record,
                meta=meta,
            )
    except IntegrityError as exc:
        raise DuplicateProviderCostRevenue(
            f"REV_PROVIDER_COST already recorded for provider cost #{record.pk}"
        ) from exc
